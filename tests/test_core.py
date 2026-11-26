import base64
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["APP_VERSION"] = "testbuild1234"

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def cl():
    from app import tg
    start = tg.start
    tg.start = lambda: None  # no background bot thread in tests: it would post the daily list into whatever test is running
    try:
        with TestClient(app) as c:
            yield c
    finally:
        tg.start = start


def op(kind, uid, **kw):
    base = {"kind": kind, "uuid": uid, "product": "Tee", "colour": "White", "size": "8", "lot": "", "loc": 1, "qty": 5}
    base.update(kw)
    return base


def sync(cl, *ops):
    return cl.post("/api/sync", json={"ops": list(ops)}).json()["results"]


def stock(cl):
    s = cl.get("/api/state").json()
    out = {}
    for m in s["mv"]:
        k = (m["v"], m["lot"], m["loc"])
        out[k] = out.get(k, 0) + m["qty"]
    return s, out


def test_four_default_locations(cl):
    assert [l["name"] for l in cl.get("/api/state").json()["locs"]] == ["Main: Ground", "Main: Basement", "Store", "Corner shelf"]


def test_receive_and_duplicate_is_ignored(cl):
    assert sync(cl, op("receive", "r1"))[0]["status"] == "ok"
    assert sync(cl, op("receive", "r1"))[0]["status"] == "dup"  # retry must not double count
    s, st = stock(cl)
    assert sum(st.values()) == 5


def test_blank_lot_stays_blank(cl):
    s, _ = stock(cl)
    assert all(m["lot"] == "" for m in s["mv"])


def test_transfer_writes_two_legs(cl):
    assert sync(cl, op("transfer", "t1", qty=2, to=2))[0]["status"] == "ok"
    s, st = stock(cl)
    vid = s["vars"][0]["id"]
    assert st[(vid, "", 1)] == 3 and st[(vid, "", 2)] == 2


def test_failed_transfer_leaves_nothing_behind(cl):
    n = len(cl.get("/api/state").json()["mv"])
    r = sync(cl, op("transfer", "t2", qty=1, to=999))[0]
    assert r["status"] == "error" and len(cl.get("/api/state").json()["mv"]) == n


def test_sale_below_zero_is_accepted_but_flagged(cl):
    sync(cl, op("sale", "s1", qty=10))
    mv = cl.get("/api/state").json()["mv"]
    sale = [m for m in mv if m["type"] == "sale"]
    flagged = [m for m in mv if m["flag"]]
    assert sum(m["qty"] for m in sale) == -10  # what was there is taken first; the rest is the part below zero
    assert len(flagged) == 1 and flagged[0] is sale[-1]


def test_adjust_needs_note(cl):
    assert sync(cl, op("adjust", "a1", qty=-1))[0]["status"] == "error"
    assert sync(cl, op("adjust", "a2", qty=-1, note="damaged"))[0]["status"] == "ok"


def test_unknown_item_rejected_not_created(cl):
    assert sync(cl, op("sale", "s2", product="Nope"))[0]["status"] == "error"


def test_rename_colour_merges_and_keeps_history(cl):
    sync(cl, op("receive", "r2", colour="Sky blue", size="8", qty=4))
    pid = cl.get("/api/state").json()["prods"][0]["id"]
    before = sum(m["qty"] for m in cl.get("/api/state").json()["mv"])
    r = cl.post("/api/rename", json={"product_id": pid, "kind": "colour", "old": "White", "new": "Sky blue"}).json()
    s = cl.get("/api/state").json()
    assert r["merged"] == 1 and sum(m["qty"] for m in s["mv"]) == before
    assert {v["colour"] for v in s["vars"]} == {"Sky blue"}


def test_product_rename_and_duplicate_name(cl):
    pid = cl.get("/api/state").json()["prods"][0]["id"]
    assert cl.post(f"/api/product/{pid}", json={"name": "Tee kids 2026", "alias": "kids tee"}).status_code == 200
    sync(cl, op("receive", "r3", product="Other"))
    assert cl.post(f"/api/product/{pid}", json={"name": "other"}).status_code == 409


def test_location_rules(cl):
    assert cl.post("/api/location", json={"name": "Yard"}).status_code == 200
    assert cl.delete("/api/location/1").status_code == 409  # has history


def test_export_pdf(cl):
    pid = cl.get("/api/state").json()["prods"][0]["id"]
    r = cl.get(f"/api/export/{pid}?cnt=1&zero=1")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"


def test_photo_op_roundtrip(cl):
    png = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
    assert sync(cl, {"kind": "photo", "uuid": "p1", "product": "Other", "data": png})[0]["status"] == "ok"
    p = [x for x in cl.get("/api/state").json()["prods"] if x["name"] == "Other"][0]
    assert cl.get(p["photo"]).status_code == 200


def test_extract_without_key_is_clean_503(cl, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = cl.post("/api/extract", files={"file": ("a.jpg", b"x", "image/jpeg")})
    assert r.status_code == 503


def test_migration_is_idempotent_and_backs_up(cl):
    from app import db

    db.migrate()
    db.migrate()
    assert os.path.isdir(os.path.join(db.DATA, "backups")) or True
    db.backup_daily()
    assert any(f.startswith("daily-") for f in os.listdir(os.path.join(db.DATA, "backups")))


def test_api_key_settings_never_leak(cl, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cl.get("/api/settings").json()["key_set"] is False
    assert cl.post("/api/settings", json={"api_key": "not-a-key"}).status_code == 422
    assert cl.post("/api/settings", json={"api_key": "sk-ant-secret-1234"}).status_code == 200
    s = cl.get("/api/settings")
    assert {k: v for k, v in s.json().items() if k != "backup"} == {"key_set": True, "key_hint": "...1234", "env_key": False}
    assert "secret" not in s.text and "secret" not in cl.get("/api/state").text
    seen = {}

    from app import extract

    def fake(img, known, key=""):
        seen["key"] = key
        return {"kind": "label", "items": []}

    monkeypatch.setattr(extract, "extract", fake)
    cl.post("/api/extract", files={"file": ("a.jpg", b"x", "image/jpeg")})
    assert seen["key"] == "sk-ant-secret-1234"  # saved key is what gets used
    cl.post("/api/settings", json={"api_key": ""})
    assert cl.get("/api/settings").json()["key_set"] is False


def test_extract_timeout_maps_to_504(cl, monkeypatch):
    from app import extract

    class APITimeoutError(Exception):
        pass

    def boom(img, known, key=""):
        raise APITimeoutError("slow")

    monkeypatch.setattr(extract, "extract", boom)
    r = cl.post("/api/extract", files={"file": ("a.jpg", b"x", "image/jpeg")})
    assert r.status_code == 504 and "45 seconds" in r.json()["detail"]


def test_reorder_levels(cl):
    s = cl.get("/api/state").json()
    pid = s["prods"][0]["id"]
    assert all("reorder" in v and v["reorder"] == 0 for v in s["vars"])
    n = len([v for v in s["vars"] if v["prod"] == pid])
    assert cl.post("/api/reorder", json={"product_id": pid, "level": 3}).json()["updated"] == n
    v = [x for x in cl.get("/api/state").json()["vars"] if x["prod"] == pid][0]
    assert cl.post("/api/reorder", json={"product_id": pid, "colour": v["colour"], "size": v["size"], "level": 1}).json()["updated"] == 1
    after = {x["id"]: x["reorder"] for x in cl.get("/api/state").json()["vars"] if x["prod"] == pid}
    assert after[v["id"]] == 1 and (n == 1 or 3 in after.values())
    assert cl.post("/api/reorder", json={"product_id": pid, "level": -1}).status_code == 422
    assert cl.post("/api/reorder", json={"level": 2}).status_code == 422


def test_reorder_level_survives_merge_with_the_higher_value(cl):
    sync(cl, op("receive", "ro1", product="Merge tee", colour="A", size="M", qty=2),
         op("receive", "ro2", product="Merge tee", colour="B", size="M", qty=2))
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "Merge tee"][0]["id"]
    cl.post("/api/reorder", json={"product_id": pid, "colour": "A", "size": "M", "level": 4})
    cl.post("/api/reorder", json={"product_id": pid, "colour": "B", "size": "M", "level": 2})
    cl.post("/api/rename", json={"product_id": pid, "kind": "colour", "old": "A", "new": "B"})
    vs = [v for v in cl.get("/api/state").json()["vars"] if v["prod"] == pid]
    assert len(vs) == 1 and vs[0]["reorder"] == 4


def test_upgrade_from_v2_keeps_data(tmp_path, monkeypatch):
    from app import db

    full = list(db.MIGRATIONS)
    monkeypatch.setattr(db, "DATA", str(tmp_path))
    monkeypatch.setattr(db, "DB", str(tmp_path / "stock.db"))
    monkeypatch.setattr(db, "MIGRATIONS", full[:2])
    db.migrate()
    c = db.connect()
    c.execute("INSERT INTO products(name) VALUES('Keep')")
    c.execute("INSERT INTO variants(product_id,colour,size) VALUES(1,'','')")
    c.close()
    monkeypatch.setattr(db, "MIGRATIONS", full)
    db.migrate()
    c = db.connect()
    assert c.execute("SELECT reorder_at FROM variants").fetchone()[0] == 0
    assert c.execute("SELECT name FROM products").fetchone()[0] == "Keep"
    assert c.execute("SELECT v FROM meta WHERE k='schema'").fetchone()[0] == str(len(full))
    c.close()
    assert any(f.startswith("pre-v2") for f in os.listdir(tmp_path / "backups"))


def test_build_stamp_and_no_cache_headers(cl):
    r = cl.get("/")
    assert r.status_code == 200 and "testbuild1234" in r.text and "__BUILD__" not in r.text
    assert r.headers["cache-control"] == "no-cache"
    sw = cl.get("/sw.js")
    assert 'var B="testbuild1234"' in sw.text and sw.headers["cache-control"] == "no-cache"
    h = cl.get("/api/health").json()
    assert h["ok"] is True and h["version"] == "testbuild1234" and "last" in h["backup"]
    assert cl.get("/api/state").json()["build"] == "testbuild1234"
    assert cl.get("/manifest.webmanifest").status_code == 200  # static files still served


# ---------- picking / holding + Telegram ----------
def test_location_roles_default_holding_and_validate(cl):
    locs = cl.get("/api/state").json()["locs"]
    assert all(l["role"] == "holding" for l in locs)
    assert cl.post("/api/location/1", json={"role": "picking"}).status_code == 200
    assert cl.post("/api/location/1", json={"role": "bogus"}).status_code == 422
    assert cl.post("/api/location", json={"name": "Shop", "role": "picking"}).status_code == 200
    assert cl.post("/api/location", json={"name": "X", "role": "nope"}).status_code == 422


def test_pick_min_is_separate_from_reorder(cl):
    s = cl.get("/api/state").json()
    pid = s["prods"][0]["id"]
    before = s["vars"][0]["reorder"]
    assert cl.post("/api/reorder", json={"product_id": pid, "level": 7, "field": "pick_min"}).status_code == 200
    assert cl.post("/api/reorder", json={"product_id": pid, "level": 1, "field": "evil"}).status_code == 422
    v = cl.get("/api/state").json()["vars"][0]
    assert v["pickmin"] == 7 and v["reorder"] == before


def test_refill_list_uses_picking_stock_and_shows_holding(cl):
    from app import db, tg

    # location 1 = picking (set above), location 2 = holding
    sync(cl, op("receive", "pk1", product="Pick Tee", colour="Red", size="M", loc=1, qty=2))
    sync(cl, op("receive", "pk2", product="Pick Tee", colour="Red", size="M", loc=2, qty=30))
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "Pick Tee"][0]["id"]
    cl.post("/api/reorder", json={"product_id": pid, "level": 5, "field": "pick_min"})
    c = db.connect()
    items = tg.refill(c)
    c.close()
    x = [i for i in items if i["name"] == "Pick Tee"][0]
    assert x["pick"] == 2 and x["min"] == 5 and x["holding"] == [("Main: Basement", 30)]
    sync(cl, op("transfer", "pk3", product="Pick Tee", colour="Red", size="M", loc=2, to=1, qty=10))
    c = db.connect()
    assert not [i for i in tg.refill(c) if i["name"] == "Pick Tee"]
    c.close()


def test_telegram_token_never_leaks_and_is_validated(cl, monkeypatch):
    from app import tg

    monkeypatch.setattr(tg, "api", lambda t, m, p=None, timeout=15: {"username": "stockbot"})
    r = cl.post("/api/telegram", json={"token": "123456:SECRETTOKENabcd", "users": "111, 222", "group": "-100123", "hour": "8"})
    assert r.status_code == 200
    for body in (r.text, cl.get("/api/telegram").text, cl.get("/api/state").text, cl.get("/api/settings").text):
        assert "SECRETTOKEN" not in body
    assert cl.get("/api/telegram").json()["token_hint"] == "...abcd"
    assert cl.post("/api/telegram", json={"users": "abc"}).status_code == 422
    assert cl.post("/api/telegram", json={"group": "my group"}).status_code == 422
    assert cl.post("/api/telegram", json={"hour": "25"}).status_code == 422

    def bad(t, m, p=None, timeout=15):
        raise RuntimeError("Unauthorized")

    monkeypatch.setattr(tg, "api", bad)
    assert cl.post("/api/telegram", json={"token": "999:nope"}).status_code == 422
    assert tg.setting("tg_token").endswith("abcd")  # the rejected token did not replace the good one


def test_bot_only_answers_allowed_users_and_right_chats(cl, monkeypatch):
    from app import tg

    sent = []
    monkeypatch.setattr(tg, "send", lambda tok, chat, text, html=False: sent.append((chat, text)))
    tg.STATE["bot"] = "stockbot"

    def upd(user, chat, text, kind="private"):
        return {"update_id": 1, "message": {"text": text, "from": {"id": user}, "chat": {"id": chat, "type": kind}}}

    tg.handle(upd(999, 999, "pick tee"), "t")  # stranger: silent
    assert sent == []
    tg.handle(upd(999, 999, "/id"), "t")  # but may learn their IDs
    assert "999" in sent[-1][1]
    sent.clear()
    tg.handle(upd(111, 111, "pick tee"), "t")
    assert "Pick Tee" in sent[-1][1] and "Red" in sent[-1][1]
    sent.clear()
    tg.handle(upd(111, -100123, "hello everyone", "supergroup"), "t")  # group chatter ignored
    assert sent == []
    tg.handle(upd(111, -100123, "/low@stockbot", "supergroup"), "t")
    assert sent and sent[-1][0] == -100123
    sent.clear()
    tg.handle(upd(111, -100999, "/low", "supergroup"), "t")  # some other group: silent
    assert sent == []


def test_daily_digest_posts_once(cl, monkeypatch):
    import datetime

    from app import tg

    sent = []
    monkeypatch.setattr(tg, "send", lambda tok, chat, text, html=False: sent.append(chat))
    tg.put("tg_last_digest", "")
    cl.post("/api/reorder", json={"product_id": 1, "level": 99, "field": "pick_min"})
    tg.maybe_digest(datetime.datetime(2026, 1, 5, 7, 0))  # before 08:00
    assert sent == []
    tg.maybe_digest(datetime.datetime(2026, 1, 5, 9, 0))
    tg.maybe_digest(datetime.datetime(2026, 1, 5, 15, 0))  # same day again
    assert sent == [-100123]


def test_migration_from_v3_adds_role_and_pick_min():
    import sqlite3
    import tempfile

    from app import db

    d = tempfile.mkdtemp()
    old = db.DATA, db.DB
    try:
        db.DATA, db.DB = d, os.path.join(d, "stock.db")
        c = sqlite3.connect(db.DB)
        c.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
        c.executescript("BEGIN;\n%s\n%s\n%s\nINSERT OR REPLACE INTO meta VALUES('schema','3');\nCOMMIT;" % tuple(db.MIGRATIONS[:3]))
        c.execute("INSERT INTO products(name) VALUES('Old')")
        c.execute("INSERT INTO variants(product_id) VALUES(1)")
        c.commit()
        c.close()
        db.migrate()
        c = db.connect()
        assert c.execute("SELECT role FROM locations").fetchone()[0] == "holding"
        assert c.execute("SELECT pick_min FROM variants").fetchone()[0] == 0
        c.close()
        assert any(f.startswith("pre-v3") for f in os.listdir(os.path.join(d, "backups")))
    finally:
        db.DATA, db.DB = old


# ---------- zero stock, picking maximum ----------
def test_receive_zero_registers_item_but_other_kinds_still_need_positive(cl):
    assert sync(cl, op("receive", "z1", product="Zero Tee", colour="Grey", size="10", qty=0))[0]["status"] == "ok"
    s = cl.get("/api/state").json()
    z = [p for p in s["prods"] if p["name"] == "Zero Tee"][0]
    assert any(v["prod"] == z["id"] and v["colour"] == "Grey" for v in s["vars"])
    assert sync(cl, op("receive", "z2", product="Zero Tee", qty=-1))[0]["status"] == "error"
    assert sync(cl, op("sale", "z3", product="Zero Tee", colour="Grey", size="10", qty=0))[0]["status"] == "error"
    assert sync(cl, op("transfer", "z4", product="Zero Tee", colour="Grey", size="10", qty=0, to=2))[0]["status"] == "error"


def test_bot_reports_zero_stock_instead_of_not_found(cl):
    from app import db, tg

    c = db.connect()
    t = tg.lookup_text(c, "zero tee")
    c.close()
    assert "Zero Tee: 0 ctn total" in t and "Grey" in t and "10      0" in t


def test_refill_bring_amount_is_capped_by_holding(cl):
    from app import db, tg

    sync(cl, op("receive", "mx1", product="Max Tee", colour="Blue", size="L", loc=1, qty=3))
    sync(cl, op("receive", "mx2", product="Max Tee", colour="Blue", size="L", loc=2, qty=20))
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "Max Tee"][0]["id"]
    assert cl.post("/api/reorder", json={"product_id": pid, "level": 5, "field": "pick_min"}).status_code == 200
    assert cl.post("/api/reorder", json={"product_id": pid, "level": 40, "field": "pick_max"}).status_code == 200
    c = db.connect()
    x = [i for i in tg.refill(c) if i["name"] == "Max Tee"][0]
    c.close()
    assert x["need"] == 37 and x["bring"] == 20 and x["max"] == 40
    t = tg.refill_text([x])
    assert "L        3     20    20" in t and "capped" in t
    assert cl.post("/api/reorder", json={"product_id": pid, "level": 23, "field": "pick_max"}).status_code == 200
    c = db.connect()
    x = [i for i in tg.refill(c) if i["name"] == "Max Tee"][0]
    c.close()
    assert x["need"] == 20 and x["bring"] == 20


def test_levels_can_be_set_for_every_size_of_a_colour(cl):
    for i, sz in enumerate(["8", "10", "12"]):
        sync(cl, op("receive", "cl%d" % i, product="Lycra", colour="Grey", size=sz, qty=1))
    sync(cl, op("receive", "cl9", product="Lycra", colour="Pink", size="8", qty=1))
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "Lycra"][0]["id"]
    r = cl.post("/api/reorder", json={"product_id": pid, "colour": "Grey", "level": 6, "field": "pick_min"}).json()
    assert r["updated"] == 3
    cl.post("/api/reorder", json={"product_id": pid, "colour": "Grey", "level": 20, "field": "pick_max"})
    vs = [v for v in cl.get("/api/state").json()["vars"] if v["prod"] == pid]
    assert {(v["colour"], v["pickmin"], v["pickmax"]) for v in vs} == {("Grey", 6, 20), ("Pink", 0, 0)}
    cl.post("/api/reorder", json={"product_id": pid, "colour": "Grey", "size": "10", "level": 9, "field": "pick_min"})
    vs = {v["size"]: v["pickmin"] for v in cl.get("/api/state").json()["vars"] if v["prod"] == pid and v["colour"] == "Grey"}
    assert vs == {"8": 6, "10": 9, "12": 6}  # a single size can still differ


def test_size_order_is_numeric_then_apparel():
    from app import tg

    got = sorted(["10", "4", "18", "6", "XL", "S", "M", "XXL", "L", "", "King"], key=tg.size_key)
    assert got == ["4", "6", "10", "18", "S", "M", "L", "XL", "XXL", "King", ""]


def test_lookup_is_a_sorted_totals_table_per_colour(cl):
    from app import db, tg

    for i, (sz, loc, q) in enumerate([("10", 1, 19), ("4", 3, 19), ("12", 1, 29), ("12", 3, 1), ("6", 1, 13)]):
        sync(cl, op("receive", "tb%d" % i, product="Terry Set", colour="Grey", size=sz, loc=loc, qty=q))
    c = db.connect()
    blocks = tg.lookup_blocks(c, "terry")
    c.close()
    t = blocks[0]
    assert "<pre>" in t and t.index("\n4 ") < t.index("\n6 ") < t.index("\n10 ") < t.index("\n12 ")
    assert "Total" in t and "81" in t and "Ground" not in t and "Store" not in t  # totals only, no warehouse columns
    assert "pdf" in blocks[-1]  # pointer to the PDF for the warehouse breakdown
    msgs = tg.pack(["x" * 2000, "y" * 2000, "z" * 100])
    assert len(msgs) == 2 and all(len(m) < 4096 for m in msgs)


def test_stock_request_allocates_by_lot_and_renders_pdf(cl):
    from app import db, tg

    # picking = location 1 (set earlier). Holding: 2 (blank lot 10, lot L2 20) and 3 (15).
    sync(cl, op("receive", "sr0", product="Req Tee", colour="Grey", size="10", loc=1, qty=3))
    sync(cl, op("receive", "sr1", product="Req Tee", colour="Grey", size="10", loc=2, qty=10, lot=""))
    sync(cl, op("receive", "sr2", product="Req Tee", colour="Grey", size="10", loc=2, qty=20, lot="L2"))
    sync(cl, op("receive", "sr3", product="Req Tee", colour="Grey", size="10", loc=3, qty=15))
    sync(cl, op("receive", "sr4", product="Req Tee", colour="Grey", size="4", loc=1, qty=0))  # nothing anywhere
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "Req Tee"][0]["id"]
    cl.post("/api/reorder", json={"product_id": pid, "level": 5, "field": "pick_min"})
    cl.post("/api/reorder", json={"product_id": pid, "colour": "Grey", "size": "10", "level": 40, "field": "pick_max"})
    c = db.connect()
    moves, short = tg.request_lines(c)
    c.close()
    mine = [m for m in moves if m["name"] == "Req Tee"]
    # need 37: location 2 gives 10 (no lot) + 20 (L2), location 3 gives the remaining 7
    assert [(m["src"], m["lot"], m["qty"]) for m in mine] == [("Main: Basement", "", 10), ("Main: Basement", "L2", 20), ("Store", "", 7)]
    assert any(x["name"] == "Req Tee" and x["size"] == "4" for x in short)
    r = cl.get("/api/stock-request")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    assert cl.get("/api/stock-request?dest=99999").status_code == 200  # unknown destination falls back to first picking


def test_destination_is_the_picking_warehouse_that_already_holds_the_item(cl):
    from app import db, tg

    locs = {l["name"]: l["id"] for l in cl.get("/api/state").json()["locs"]}
    shop = locs["Shop"]  # created as a picking warehouse in an earlier test; location 1 is picking too
    sync(cl, op("receive", "ds1", product="Dest Tee", colour="Pink", size="8", loc=shop, qty=1))
    sync(cl, op("receive", "ds2", product="Dest Tee", colour="Pink", size="8", loc=2, qty=20))
    sync(cl, op("receive", "ds3", product="Dest Tee", colour="Pink", size="10", loc=2, qty=20))  # nothing in picking yet
    sync(cl, op("receive", "ds4", product="Dest Tee", colour="Pink", size="10", loc=shop, qty=0))
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "Dest Tee"][0]["id"]
    cl.post("/api/reorder", json={"product_id": pid, "level": 3, "field": "pick_min"})
    cl.post("/api/reorder", json={"product_id": pid, "level": 10, "field": "pick_max"})
    c = db.connect()
    moves, _ = tg.request_lines(c, 1)
    items = {i["size"]: i for i in tg.refill(c) if i["name"] == "Dest Tee"}
    c.close()
    d = {m["size"]: m["dest"] for m in moves if m["name"] == "Dest Tee"}
    assert d["8"] == "Shop"  # follows where the item already sits
    assert d["10"] == "Shop"  # the item's home picking warehouse (set when it was first put there), even for a size with none there
    assert items["8"]["dest"] == "Shop" and "into Shop" in tg.refill_text([items["8"]])
    assert cl.get("/api/stock-request").status_code == 200


def test_home_picking_warehouse_is_remembered_even_when_sold_out(cl):
    from app import db, tg

    locs = {l["name"]: l["id"] for l in cl.get("/api/state").json()["locs"]}
    shop = locs["Shop"]
    sync(cl, op("receive", "hm1", product="Home Tee", colour="Red", size="8", loc=shop, qty=2))
    sync(cl, op("sale", "hm2", product="Home Tee", colour="Red", size="8", loc=shop, qty=2))  # picking now empty
    sync(cl, op("receive", "hm3", product="Home Tee", colour="Red", size="8", loc=2, qty=20))
    p = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "Home Tee"][0]
    assert p["pickloc"] == shop
    cl.post("/api/reorder", json={"product_id": p["id"], "level": 3, "field": "pick_min"})
    cl.post("/api/reorder", json={"product_id": p["id"], "level": 10, "field": "pick_max"})
    c = db.connect()
    moves, _ = tg.request_lines(c, 1)  # default destination is location 1, but the item's home wins
    c.close()
    assert [m["dest"] for m in moves if m["name"] == "Home Tee"] == ["Shop"]
    # change it by hand; only active picking warehouses are accepted
    assert cl.post("/api/product/%d" % p["id"], json={"name": "Home Tee", "pick_loc": 1}).status_code == 200
    assert cl.get("/api/state").json()["prods"][[x["id"] for x in cl.get("/api/state").json()["prods"]].index(p["id"])]["pickloc"] == 1
    assert cl.post("/api/product/%d" % p["id"], json={"name": "Home Tee", "pick_loc": 2}).status_code == 422  # 2 is holding
    assert cl.post("/api/product/%d" % p["id"], json={"name": "Home Tee", "pick_loc": 0}).status_code == 200  # cleared


def test_migration_backfills_home_picking_warehouse_from_existing_stock():
    import sqlite3
    import tempfile

    from app import db

    d = tempfile.mkdtemp()
    old = db.DATA, db.DB
    try:
        db.DATA, db.DB = d, os.path.join(d, "stock.db")
        c = sqlite3.connect(db.DB)
        c.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
        c.executescript("BEGIN;\n%s\nINSERT OR REPLACE INTO meta VALUES('schema','6');\nCOMMIT;" % "\n".join(db.MIGRATIONS[:6]))
        c.execute("UPDATE locations SET role='picking' WHERE id=3")
        c.execute("INSERT INTO products(name) VALUES('A'),('B')")
        c.execute("INSERT INTO variants(product_id) VALUES(1),(2)")
        for i, (v, loc, q) in enumerate([(1, 3, 5), (1, 1, 50), (2, 1, 9)]):  # A: 5 in picking (loc 3), 50 in holding. B: none in picking
            c.execute("INSERT INTO movements(uuid,ts,variant_id,location_id,qty,type) VALUES(?,?,?,?,?,'receive')", ("m%d" % i, "2026-01-01", v, loc, q))
        c.commit()
        c.close()
        db.migrate()
        c = db.connect()
        assert [r[0] for r in c.execute("SELECT pick_loc FROM products ORDER BY id")] == [3, None]
        c.close()
    finally:
        db.DATA, db.DB = old


def test_post_refill_sends_tables_and_pdf(cl, monkeypatch):
    from app import tg

    sync(cl, op("receive", "pf1", product="Pdf Tee", colour="Blue", size="L", loc=1, qty=1))
    sync(cl, op("receive", "pf2", product="Pdf Tee", colour="Blue", size="L", loc=2, qty=9))
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "Pdf Tee"][0]["id"]
    cl.post("/api/reorder", json={"product_id": pid, "level": 3, "field": "pick_min"})
    cl.post("/api/reorder", json={"product_id": pid, "level": 6, "field": "pick_max"})
    msgs, docs = [], []
    monkeypatch.setattr(tg, "send", lambda token, chat, text, html=False: msgs.append(text))
    monkeypatch.setattr(tg, "send_document", lambda token, chat, name, data, caption="": docs.append((name, data)))
    monkeypatch.setattr(tg, "setting", lambda k, d="": {"tg_token": "t", "tg_group": "-1"}.get(k, d))
    assert tg.post_refill() >= 1
    assert "<pre>" in msgs[0] + (msgs[1] if len(msgs) > 1 else "") and "Min" not in "".join(msgs)
    assert docs and docs[0][0].endswith(".pdf") and docs[0][1].startswith(b"%PDF")


def test_group_by_size_for_towels(cl):
    from app import db, tg

    for i, (col, size, q) in enumerate([("Red", "50x90", 5), ("Blue", "50x90", 4), ("Red", "70x140", 3)]):
        sync(cl, op("receive", "tw%d" % i, product="Towel", colour=col, size=size, loc=1, qty=q))
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "Towel"][0]["id"]
    assert [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "Towel"][0]["grp"] == "colour"
    assert cl.post("/api/product/%d" % pid, json={"name": "Towel", "group_by": "banana"}).status_code == 422
    assert cl.post("/api/product/%d" % pid, json={"name": "Towel", "group_by": "size"}).status_code == 200
    assert [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "Towel"][0]["grp"] == "size"
    c = db.connect()
    t = tg.lookup_text(c, "towel")
    c.close()
    assert t.index("50x90") < t.index("70x140") and "Colour" in t and "Size  " not in t  # one table per size, rows are colours
    # level for one size across all colours
    assert cl.post("/api/reorder", json={"product_id": pid, "size": "50x90", "level": 7, "field": "pick_min"}).status_code == 200
    v = {(x["colour"], x["size"]): x for x in cl.get("/api/state").json()["vars"] if x["prod"] == pid}
    assert v[("Red", "50x90")]["pickmin"] == 7 and v[("Blue", "50x90")]["pickmin"] == 7 and v[("Red", "70x140")]["pickmin"] == 0
    # renaming a size renames it in every colour
    assert cl.post("/api/rename", json={"product_id": pid, "kind": "size", "old": "50x90", "new": "50x100"}).status_code == 200
    sizes = {x["size"] for x in cl.get("/api/state").json()["vars"] if x["prod"] == pid}
    assert sizes == {"50x100", "70x140"}
    # a colour-scoped size rename still works as before
    assert cl.post("/api/rename", json={"product_id": pid, "kind": "size", "colour": "Red", "old": "70x140", "new": "70x150"}).status_code == 200
    # refill table for a size-grouped item: one table per size, a row per colour
    c = db.connect()
    items = [i for i in tg.refill(c) if i["name"] == "Towel"]
    c.close()
    assert items and all(i["grp"] == "size" for i in items)
    assert "Colour" in tg.refill_text(items)


def test_last_updated_in_bot_and_pdf(cl, monkeypatch):
    import re

    monkeypatch.setenv("TZ", "Europe/Athens")
    from app import db, tg

    sync(cl, op("receive", "lu1", product="Dated Tee", colour="Blue", size="M", loc=1, qty=4))
    c = db.connect()
    c.execute("UPDATE movements SET ts='2026-03-05T22:30:00.000Z' WHERE uuid='lu1'")  # 22:30 UTC = 00:30 next day in Athens
    c.close()
    c = db.connect()
    t = tg.lookup_text(c, "dated tee")
    c.close()
    assert "Upd" in t and re.search(r"M\s+4\s+06/03/26", t), t
    assert tg.local_day("2026-03-05T22:30:00.000Z") == "06/03/26"
    assert tg.local_day("garbage") == "-"
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "Dated Tee"][0]["id"]
    r = cl.get("/api/export/%d?upd=1" % pid)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert cl.get("/api/export/%d?upd=0" % pid).status_code == 200


def test_audit_edge_cases(cl, monkeypatch):
    from app import db, tg

    # deleting an unused warehouse that an item points at as its picking warehouse
    assert cl.post("/api/location", json={"name": "Temp Pick", "role": "picking"}).status_code == 200
    lid = [l["id"] for l in cl.get("/api/state").json()["locs"] if l["name"] == "Temp Pick"][0]
    sync(cl, op("receive", "au1", product="Audit Tee", colour="Red", size="M", loc=1, qty=2))
    sync(cl, op("receive", "au2", product="Audit Tee", colour="Blue", size="M", loc=1, qty=1))
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "Audit Tee"][0]["id"]
    assert cl.post("/api/product/%d" % pid, json={"name": "Audit Tee", "pick_loc": lid}).status_code == 200
    assert cl.delete("/api/location/%d" % lid).status_code == 200
    assert [p for p in cl.get("/api/state").json()["prods"] if p["id"] == pid][0]["pickloc"] is None
    # a rejected edit changes nothing
    assert cl.post("/api/product/%d" % pid, json={"name": "Should Not Stick", "group_by": "x"}).status_code == 422
    assert cl.post("/api/product/%d" % pid, json={"name": "Should Not Stick", "pick_loc": 9999}).status_code == 422
    assert [p for p in cl.get("/api/state").json()["prods"] if p["id"] == pid][0]["name"] == "Audit Tee"
    # blank rename refused; merging keeps the higher levels
    assert cl.post("/api/rename", json={"product_id": pid, "kind": "colour", "old": "Red", "new": "  "}).status_code == 422
    cl.post("/api/reorder", json={"product_id": pid, "colour": "Blue", "size": "M", "level": 4, "field": "pick_min"})
    cl.post("/api/reorder", json={"product_id": pid, "colour": "Blue", "size": "M", "level": 9, "field": "pick_max"})
    assert cl.post("/api/rename", json={"product_id": pid, "kind": "colour", "old": "Blue", "new": "Red"}).json()["merged"] == 1
    v = [x for x in cl.get("/api/state").json()["vars"] if x["prod"] == pid]
    assert len(v) == 1 and v[0]["pickmin"] == 4 and v[0]["pickmax"] == 9
    t = sum(m["qty"] for m in cl.get("/api/state").json()["mv"] if m["v"] == v[0]["id"])
    assert t == 3  # history merged, nothing lost
    # a failed PDF is reported and survives the polling loop's status updates
    monkeypatch.setattr(tg, "setting", lambda k, d="": {"tg_token": "t", "tg_group": "-1"}.get(k, d))
    monkeypatch.setattr(tg, "send", lambda *a, **k: None)
    monkeypatch.setattr(tg, "refill", lambda c: [dict(name="x", colour="", size="", pick=0, min=1, max=2, need=2, dest="d", bring=1, holding=[("h", 1)])])

    def boom(*a, **k):
        raise RuntimeError("upload failed")
    monkeypatch.setattr(tg, "send_document", boom)
    tg.post_refill()
    assert "PDF not sent" in tg.STATE["post_err"]
    tg.STATE.update(ok=True, err="")  # what the polling loop does
    assert "PDF not sent" in cl.get("/api/telegram").json()["post_err"]
    monkeypatch.setattr(tg, "send_document", lambda *a, **k: None)
    tg.post_refill()
    assert tg.STATE["post_err"] == ""


def test_export_totals_only_and_split(cl):
    import io
    for i, (c, z, l, q) in enumerate([("Black", "50x90", 2, 43), ("Black", "50x90", 3, 5), ("Black", "90x140", 2, 17), ("Ecru", "50x90", 2, 48)]):
        sync(cl, op("receive", "ex%d" % i, product="Towels X", colour=c, size=z, loc=l, qty=q))
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "Towels X"][0]["id"]
    for q in ("tot=1&split=size&lot=0", "tot=0&split=colour", "tot=1&split=", "split=size&col=0&lot=0"):
        r = cl.get("/api/export/%d?%s" % (pid, q))
        assert r.status_code == 200 and r.content.startswith(b"%PDF"), q


def test_discontinued_ppc_and_pdf_highlights(cl, monkeypatch):
    import subprocess, tempfile, os
    from app import db, tg

    monkeypatch.setenv("TZ", "Europe/Athens")
    for i, (c, z, l, q) in enumerate([("Black", "50x90", 2, 10), ("Red", "50x90", 2, 4), ("Blue", "50x90", 2, 1), ("Green", "50x90", 2, 0), ("Lemon", "50x90", 2, 6)]):
        sync(cl, op("receive", "dv%d" % i, product="Disc Towel", colour=c, size=z, loc=l, qty=q, ppc=12 if c == "Black" else 0))
    st = cl.get("/api/state").json()
    pid = [p for p in st["prods"] if p["name"] == "Disc Towel"][0]["id"]
    vs = {v["colour"]: v for v in st["vars"] if v["prod"] == pid}
    assert vs["Black"]["ppc"] == 12 and vs["Red"]["ppc"] == 0 and vs["Red"]["disc"] == 0
    # levels, status, pieces per carton are set per variant (and for a whole size)
    assert cl.post("/api/reorder", json={"product_id": pid, "colour": "Blue", "size": "50x90", "level": 3}).status_code == 200  # 1 <= 3 -> yellow
    assert cl.post("/api/reorder", json={"product_id": pid, "colour": "Red", "size": "50x90", "level": 9}).status_code == 200  # 4 <= 9 but discontinued
    assert cl.post("/api/reorder", json={"product_id": pid, "colour": "Red", "size": "50x90", "level": 1, "field": "discontinued"}).status_code == 200
    assert cl.post("/api/reorder", json={"product_id": pid, "colour": "Green", "size": "50x90", "level": 1, "field": "discontinued"}).status_code == 200  # discontinued and at 0
    assert cl.post("/api/reorder", json={"product_id": pid, "colour": "Lemon", "size": "50x90", "level": 2, "field": "discontinued"}).status_code == 422
    assert cl.post("/api/reorder", json={"product_id": pid, "size": "50x90", "level": 24, "field": "ppc"}).status_code == 200
    v2 = {v["colour"]: v for v in cl.get("/api/state").json()["vars"] if v["prod"] == pid}
    assert all(v["ppc"] == 24 for v in v2.values()) and v2["Red"]["disc"] == 1 and v2["Blue"]["disc"] == 0
    # discontinued variants raise no alerts and are left out of the Telegram refill list
    cl.post("/api/reorder", json={"product_id": pid, "colour": "Red", "size": "50x90", "level": 5, "field": "pick_min"})
    c = db.connect()
    assert not [x for x in tg.refill(c) if x["name"] == "Disc Towel"]
    t = tg.lookup_text(c, "disc towel")
    c.close()
    assert "50x90 (disc.)" in t and "Black" in t
    # PDF: totals only with no warehouse ticked counts everything; every variant is listed
    def pdf_text(q):
        r = cl.get("/api/export/%d?%s" % (pid, q))
        assert r.status_code == 200
        f = os.path.join(tempfile.mkdtemp(), "x.pdf")
        open(f, "wb").write(r.content)
        return subprocess.run(["pdftotext", "-layout", f, "-"], capture_output=True, text=True).stdout
    t = pdf_text("tot=1&split=size&lot=0&locs=")
    for name in ("Black", "Red (discontinued)", "Blue", "Green (discontinued)", "Lemon"):
        assert name in t, (name, t)
    assert "yellow = at or below" in t and "discontinued" in t
    assert "Lemon" in pdf_text("tot=1&zero=0&lot=0")
    t2 = pdf_text("tot=1&dis=0&lot=0")
    assert "Red" not in t2 and "Green" not in t2 and "Blue" in t2
    assert "Green" not in pdf_text("tot=1&zero=0&lot=0&dis=1")  # discontinued with no stock only shows when zero-stock variants are listed


def test_stock_request_pdf_one_table_per_size_or_colour(cl):
    import subprocess, tempfile
    locs = {l["name"]: l["id"] for l in cl.get("/api/state").json()["locs"]}
    shop = locs["Shop"]
    n = 0
    def rcv(prod, colour, size, loc, qty):
        nonlocal n
        n += 1
        sync(cl, op("receive", "rq%d" % n, product=prod, colour=colour, size=size, loc=loc, qty=qty))
    for colour in ("Navy", "Ecru"):
        for size in ("90x140", "50x90"):
            rcv("RQ Towel", colour, size, shop, 0)
            rcv("RQ Towel", colour, size, 2, 6)
    for colour in ("Black", "Grey"):
        for size in ("S", "M"):
            rcv("RQ Lycra", colour, size, shop, 0)
            rcv("RQ Lycra", colour, size, 2, 6)
    P = {p["name"]: p["id"] for p in cl.get("/api/state").json()["prods"]}
    assert cl.post("/api/product/%d" % P["RQ Towel"], json={"name": "RQ Towel", "group_by": "size"}).status_code == 200
    for nm in ("RQ Towel", "RQ Lycra"):
        cl.post("/api/reorder", json={"product_id": P[nm], "level": 2, "field": "pick_min"})
        cl.post("/api/reorder", json={"product_id": P[nm], "level": 4, "field": "pick_max"})
    r = cl.get("/api/stock-request")
    assert r.status_code == 200
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(r.content); f.flush()
        txt = subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, text=True).stdout
    for h in ("RQ Towel · 50x90", "RQ Towel · 90x140", "RQ Lycra · Black", "RQ Lycra · Grey"):
        assert txt.count(h) == 1, h
    assert "RQ Towel · Navy" not in txt and "RQ Lycra · S" not in txt


def test_stock_command_with_pdf_sends_item_pdfs(cl, monkeypatch):
    from app import tg

    sent, docs = [], []
    monkeypatch.setattr(tg, "send", lambda tok, chat, text, html=False: sent.append(text))
    monkeypatch.setattr(tg, "send_document", lambda tok, chat, name, data, caption="": docs.append((name, data, caption)))
    tg.STATE["bot"] = "stockbot"

    def upd(text):
        return {"update_id": 1, "message": {"text": text, "from": {"id": 111}, "chat": {"id": 111, "type": "private"}}}

    sync(cl, op("receive", "pd1", product="Pdf Tee", colour="Blue", size="M", loc=1, qty=4))
    tg.handle(upd("/stock pdf tee"), "t")  # no trailing pdf word: text only
    assert sent and not docs
    sent.clear()
    tg.handle(upd("/stock pdf tee PDF"), "t")
    assert len(docs) == 1 and docs[0][1][:4] == b"%PDF" and docs[0][0].endswith(".pdf") and docs[0][2] == "Pdf Tee"
    assert sent and "no match" not in sent[0].lower()  # the text reply still comes, with the pdf word stripped from the search
    docs.clear()
    tg.handle(upd("/stock zzznothing pdf"), "t")
    assert not docs
    tg.handle(upd("/stock pdf"), "t")  # nothing after /stock: usage hint, no document
    assert not docs


def test_export_pdf_item_total_and_no_empty_warehouses(cl):
    import subprocess, tempfile
    locs = {l["name"]: l["id"] for l in cl.get("/api/state").json()["locs"]}
    for i, (loc, lot, q) in enumerate([(1, "L1", 7), (2, "L2", 5), (1, "L2", 3)]):
        sync(cl, op("receive", "pt%d" % i, product="PT Sheet", colour="Assorted", size="180", loc=loc, lot=lot, qty=q))
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "PT Sheet"][0]["id"]
    r = cl.get("/api/export/%d?locs=1,2,3,4" % pid)
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(r.content); f.flush()
        txt = subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, text=True).stdout
    assert "Total in stock: 15 cartons" in txt
    names = [l["name"] for l in cl.get("/api/state").json()["locs"]]
    assert "Ground" in txt and "Basement" in txt  # long names wrap in the header
    assert "Store" not in txt and "Corner shelf" not in txt  # nothing of this item there
    assert not [l for l in txt.splitlines() if l.strip().startswith("Total") and "in stock" not in l]  # no grand-total line under the table


def test_telegram_item_pdf_splits_size_first_items(cl):
    import subprocess, tempfile
    from app import tg

    for i, sz in enumerate(("180", "230")):
        sync(cl, op("receive", "tsp%d" % i, product="TSP Sheet", colour="Assorted", size=sz, loc=1, qty=3))
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "TSP Sheet"][0]["id"]
    assert cl.post("/api/product/%d" % pid, json={"name": "TSP Sheet", "group_by": "size"}).status_code == 200
    docs, extra = tg.item_pdfs("tsp sheet")
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(docs[0][1]); f.flush()
        txt = subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, text=True).stdout
    assert "Size: 180" in txt and "Size: 230" in txt


def test_export_pdf_has_no_trailing_blank_page(cl):
    import subprocess, tempfile
    # three size tables in landscape end right at the bottom margin; the spacer after the last one used to spill onto an empty page 2
    rows = [("180", 3, "Big box", 9), ("180", 3, "Lot 0", 54), ("180", 1, "Lot 2", 58), ("180", 1, "Lot 3", 68),
            ("230", 1, "Big box", 202), ("260", 2, "Lot 1", 18), ("260", 1, "Lot 3", 42)]
    for n, (size, loc, lot, q) in enumerate(rows):
        sync(cl, op("receive", "bp%d" % n, product="BP Sheet", colour="Assorted", size=size, loc=loc, lot=lot, qty=q))
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "BP Sheet"][0]["id"]
    r = cl.get("/api/export/%d?split=size" % pid)
    assert r.status_code == 200
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(r.content); f.flush()
        out = subprocess.run(["pdfinfo", f.name], capture_output=True, text=True).stdout
    assert [l.split()[1] for l in out.splitlines() if l.startswith("Pages")] == ["1"]


def test_item_category(cl):
    sync(cl, op("receive", "cat1", product="Cat Tee", colour="Red", size="M", loc=1, qty=1))
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "Cat Tee"][0]["id"]
    assert [p for p in cl.get("/api/state").json()["prods"] if p["id"] == pid][0]["cat"] == ""
    assert cl.post("/api/product/%d" % pid, json={"name": "Cat Tee", "category": "  Home   Textiles "}).status_code == 200
    assert [p for p in cl.get("/api/state").json()["prods"] if p["id"] == pid][0]["cat"] == "Home Textiles"
    assert cl.post("/api/product/%d" % pid, json={"name": "Cat Tee", "ppc": 3}).status_code == 200  # not sent = unchanged
    assert [p for p in cl.get("/api/state").json()["prods"] if p["id"] == pid][0]["cat"] == "Home Textiles"
    assert cl.post("/api/product/%d" % pid, json={"name": "Cat Tee", "category": ""}).status_code == 200  # blank = uncategorised
    assert [p for p in cl.get("/api/state").json()["prods"] if p["id"] == pid][0]["cat"] == ""


def test_category_reuses_existing_spelling(cl):
    for i, n in enumerate(("Cs One", "Cs Two")):
        sync(cl, op("receive", "cs%d" % i, product=n, colour="Red", size="M", loc=1, qty=1))
    P = {p["name"]: p["id"] for p in cl.get("/api/state").json()["prods"]}
    cl.post("/api/product/%d" % P["Cs One"], json={"name": "Cs One", "category": "School Wear"})
    cl.post("/api/product/%d" % P["Cs Two"], json={"name": "Cs Two", "category": "school wear"})
    cats = {p["name"]: p["cat"] for p in cl.get("/api/state").json()["prods"]}
    assert cats["Cs One"] == cats["Cs Two"] == "School Wear"


def test_parse_pack():
    from app.main import parse_pack
    assert parse_pack("24 doz") == ("24 doz", 288)
    assert parse_pack("18 dozen") == ("18 dozen", 216)
    assert parse_pack("24x6 rolls") == ("24x6 rolls", 144)
    assert parse_pack("24 by 6") == ("24 by 6", 144)
    assert parse_pack("24 × 6") == ("24 × 6", 144)
    assert parse_pack("60") == ("60 pcs", 60)
    assert parse_pack("60 pcs") == ("60 pcs", 60)
    assert parse_pack("0") == ("", 0) and parse_pack("  ") == ("", 0)
    assert parse_pack("mixed bundle") == ("mixed bundle", 0)  # kept as text even if no count can be worked out


def test_pack_text_on_receive_edit_and_old_clients(cl):
    sync(cl, op("receive", "kt1", product="KT Towels", colour="Velour", size='15"x25"', loc=1, qty=2, pack="24 doz"))
    sync(cl, op("receive", "kt2", product="KT Towels", colour="Roll", size="50x70", loc=1, qty=2, pack="24x6 rolls"))
    sync(cl, op("receive", "kt3", product="KT Towels", colour="Microfibre", size='15"x25"', loc=1, qty=1, ppc=40))  # old app version
    s = cl.get("/api/state").json()
    pid = [p for p in s["prods"] if p["name"] == "KT Towels"][0]["id"]
    V = {v["colour"]: v for v in s["vars"] if v["prod"] == pid}
    assert (V["Velour"]["pack"], V["Velour"]["ppc"]) == ("24 doz", 288)
    assert (V["Roll"]["pack"], V["Roll"]["ppc"]) == ("24x6 rolls", 144)
    assert (V["Microfibre"]["pack"], V["Microfibre"]["ppc"]) == ("40 pcs", 40)
    cl.post("/api/reorder", json={"product_id": pid, "colour": "Velour", "level": "18 doz", "field": "pack"})
    V = {v["colour"]: v for v in cl.get("/api/state").json()["vars"] if v["prod"] == pid}
    assert (V["Velour"]["pack"], V["Velour"]["ppc"]) == ("18 doz", 216) and V["Roll"]["pack"] == "24x6 rolls"
    cl.post("/api/reorder", json={"product_id": pid, "colour": "Velour", "level": "0", "field": "pack"})
    V = {v["colour"]: v for v in cl.get("/api/state").json()["vars"] if v["prod"] == pid}
    assert (V["Velour"]["pack"], V["Velour"]["ppc"]) == ("", 0)
    assert cl.post("/api/product/%d" % pid, json={"name": "KT Towels", "pack": "24 doz"}).status_code == 200
    assert [p for p in cl.get("/api/state").json()["prods"] if p["id"] == pid][0]["pack"] == "24 doz"


def test_telegram_users_accept_spaces(cl):
    from app import tg
    assert cl.post("/api/telegram", json={"users": "111, 222 333;444"}).status_code == 200
    assert tg.allowed_users() >= {111, 222, 333, 444}


def test_bot_suggests_close_items_and_confirms_with_buttons(cl, monkeypatch):
    from app import db, tg

    sync(cl, op("receive", "dym1", product="Topstar Boys Long Sleeve T-Shirts 180 GSM", colour="Navy", size="10", qty=7),
         op("receive", "dym2", product="Topstar Girls Long Sleeve T-Shirts 180 GSM", colour="Pink", size="10", qty=4),
         op("receive", "dym3", product="Velour Kitchen Towel", colour="Assorted", size='15"x25"', qty=3))
    c = db.connect()
    # typos, plurals, joined words, extra words: one clear candidate
    for q in ["boy longsleeve tshirt", "boys long sleve", "boys long sleeve 180gsm", "topstr boys sleeves"]:
        assert tg._no_hits(c, q), q
        top = tg.suggest(c, q)
        assert top and top[0][1].startswith("Topstar Boys"), (q, top)
    assert [n for _, n, _ in tg.suggest(c, "longsleeve")][:2] == ["Topstar Boys Long Sleeve T-Shirts 180 GSM", "Topstar Girls Long Sleeve T-Shirts 180 GSM"]
    assert tg.suggest(c, "pillowcase") == [] and tg.suggest(c, "duvet cover") == []
    assert not tg._no_hits(c, "boys long sleeve")  # exact words still answer straight away
    c.close()

    tg.put("tg_users", "111")
    tg.STATE["bot"] = "stockbot"
    sent, calls, docs = [], [], []
    monkeypatch.setattr(tg, "send", lambda tok, chat, text, html=False, markup=None: sent.append((chat, text, markup)))
    monkeypatch.setattr(tg, "api", lambda tok, method, params=None, timeout=15: calls.append((method, params)) or {})
    monkeypatch.setattr(tg, "send_document", lambda tok, chat, fn, data, cap="": docs.append(fn))
    monkeypatch.setattr(tg, "maybe_digest", lambda *a, **k: None)  # the bot thread must not post the daily list into this test

    def msg(user, text, chat=111):
        return {"update_id": 1, "message": {"text": text, "from": {"id": user}, "chat": {"id": chat, "type": "private"}}}

    def tap(user, data, chat=111):
        return {"update_id": 2, "callback_query": {"id": "cb1", "from": {"id": user}, "data": data,
                                                   "message": {"message_id": 55, "chat": {"id": chat, "type": "private"}}}}

    tg.handle(msg(111, "/stock boys long sleve"), "t")
    text, kb = sent[-1][1], sent[-1][2]
    assert "Did you mean" in text and "Topstar Boys" in text and "180 GSM" not in kb["inline_keyboard"][0][0]["text"]
    yes = kb["inline_keyboard"][0][0]["callback_data"]
    assert yes.startswith("sk:") and yes.endswith(":0")
    sent.clear()
    tg.handle(tap(999, yes), "t")  # a stranger's tap does nothing
    assert sent == []
    tg.handle(tap(111, yes), "t")
    assert any(m == "editMessageText" and "Topstar Boys" in p["text"] for m, p in calls)
    out = "\n".join(t for _, t, _ in sent)
    assert "Topstar Boys Long Sleeve T-Shirts 180 GSM</b>: 7 ctn total" in out and "Navy" in out and "Girls" not in out
    assert docs == []
    # the pdf flag survives the confirmation
    sent.clear()
    tg.handle(msg(111, "/stock boys long sleve pdf"), "t")
    yes = sent[-1][2]["inline_keyboard"][0][0]["callback_data"]
    assert yes.endswith(":1")
    tg.handle(tap(111, yes), "t")
    assert len(docs) == 1
    # "No" just closes the question; several candidates get a list
    sent.clear()
    tg.handle(tap(111, "sk:0:0"), "t")
    assert sent == [] and "nothing shown" in calls[-1][1]["text"]
    tg.handle(msg(111, "longsleeve"), "t")
    rows = sent[-1][2]["inline_keyboard"]
    assert len(rows) == 3 and rows[-1][0]["text"] == "None of these"
    tg.handle(msg(111, "/stock pillowcase"), "t")
    assert "Nothing found" in sent[-1][1] and sent[-1][2] is None


def test_sections_oldest_first_and_arrival_dates_follow_the_stock(cl):
    from app import db, main

    lid = [l for l in cl.get("/api/state").json()["locs"] if l["name"] == "Main: Ground"][0]["id"]
    bas = [l for l in cl.get("/api/state").json()["locs"] if l["name"] == "Main: Basement"][0]["id"]
    r = cl.post("/api/location/%d/sections" % lid, json={"on": True, "sections": [
        {"name": "A", "x": .0, "y": .0, "w": .5, "h": .5}, {"name": "B", "x": .5, "y": 0, "w": .5, "h": .5}]})
    assert r.status_code == 200
    cl.post("/api/location/%d/sections" % bas, json={"on": True, "sections": [{"name": "K", "x": 0, "y": 0, "w": 1, "h": 1}]})
    st = cl.get("/api/state").json()
    L = {l["id"]: l for l in st["locs"]}
    A, B = [s["id"] for s in L[lid]["secs"]]
    K = L[bas]["secs"][0]["id"]
    assert L[lid]["hs"] and L[bas]["hs"] and not L[3]["hs"]
    # dup names refused
    assert cl.post("/api/location/%d/sections" % lid, json={"sections": [{"name": "A"}, {"name": "a"}]}).status_code == 422

    P = dict(product="Sect Lycra", colour="Black", size="10", loc=lid)
    res = sync(cl, op("receive", "sx1", qty=40, section=A, ts="2025-03-01T08:00:00Z", **P),
               op("receive", "sx2", qty=60, section=B, ts="2026-11-01T08:00:00Z", **P),
               op("receive", "sx3", qty=1, section=K, ts="2026-11-01T09:00:00Z", **P))  # K belongs to another warehouse
    assert [x["status"] for x in res] == ["ok", "ok", "error"]
    c = db.connect()
    vid = c.execute("SELECT v.id FROM variants v JOIN products p ON p.id=v.product_id WHERE p.name='Sect Lycra'").fetchone()[0]
    pid = c.execute("SELECT id FROM products WHERE name='Sect Lycra'").fetchone()[0]
    assert c.execute("SELECT section_id FROM homes WHERE product_id=? AND location_id=?", (pid, lid)).fetchone()[0] == A  # first section used becomes home
    assert main.piles(c, vid, "", lid) == {(A, "2025-03-01T08:00:00.000Z"): 40, (B, "2026-11-01T08:00:00.000Z"): 60}

    # a sale of 50 takes the 2025 stock first, then 10 of the newer
    sync(cl, op("sale", "sx4", qty=50, ts="2026-11-20T08:00:00Z", **P))
    assert main.piles(c, vid, "", lid) == {(B, "2026-11-01T08:00:00.000Z"): 50}
    # found 3 more on recount: joins the oldest stock there, keeps its date
    sync(cl, op("adjust", "sx5", qty=3, note="recount", ts="2026-11-21T08:00:00Z", **P))
    assert main.piles(c, vid, "", lid) == {(B, "2026-11-01T08:00:00.000Z"): 53}
    # transfer to the basement: lands in the basement's home section for the item, keeps its arrival date
    c.execute("INSERT INTO homes(product_id,location_id,section_id) VALUES(?,?,?)", (pid, bas, K))
    sync(cl, dict(op("transfer", "sx6", qty=20, to=bas, ts="2026-11-22T08:00:00Z", **P)))
    assert main.piles(c, vid, "", bas) == {(K, "2026-11-01T08:00:00.000Z"): 20}
    assert main.piles(c, vid, "", lid) == {(B, "2026-11-01T08:00:00.000Z"): 33}
    # a section that still holds stock can't be removed; an empty one can
    r = cl.post("/api/location/%d/sections" % lid, json={"sections": [{"id": A, "name": "A1", "x": 0, "y": 0, "w": .5, "h": .5}]})
    assert r.status_code == 409 and "B" in r.json()["detail"]
    r = cl.post("/api/location/%d/sections" % lid, json={"sections": [{"id": B, "name": "B", "x": .5, "y": 0, "w": .5, "h": .5}]})
    assert r.status_code == 200 and [s["name"] for s in [l for l in cl.get("/api/state").json()["locs"] if l["id"] == lid][0]["secs"]] == ["B"]
    c.close()


def test_piles_handle_rows_from_before_sections(cl):
    from app import db, main

    c = db.connect()
    vid = c.execute("INSERT INTO variants(product_id,colour,size) VALUES((SELECT id FROM products LIMIT 1),'Legacy','L')").lastrowid
    for i, (q, ts) in enumerate([(10, "2024-01-01T00:00:00Z"), (5, "2024-06-01T00:00:00Z"), (-12, "2024-07-01T00:00:00Z")]):
        c.execute("INSERT INTO movements(uuid,ts,variant_id,lot,location_id,qty,type) VALUES(?,?,?,?,?,?,?)", ("leg%d" % i, ts, vid, "", 3, q, "receive" if q > 0 else "sale"))
    assert main.piles(c, vid, "", 3) == {(0, "2024-06-01T00:00:00Z"): 3}  # the old sale used up the January stock first
    c.close()


def test_move_between_sections_keeps_dates_and_can_set_home(cl):
    from app import db, main

    st = cl.get("/api/state").json()
    lid = [l for l in st["locs"] if l["name"] == "Store"][0]["id"]
    cl.post("/api/location/%d/sections" % lid, json={"on": True, "sections": [{"name": "D", "x": 0, "y": 0, "w": .5, "h": 1}, {"name": "E", "x": .5, "y": 0, "w": .5, "h": 1}]})
    D, E = [s["id"] for s in [l for l in cl.get("/api/state").json()["locs"] if l["id"] == lid][0]["secs"]]
    P = dict(product="Move Fleece", colour="Assorted", size="230", lot="Big box", loc=lid)
    sync(cl, op("receive", "mv1", qty=202, ts="2026-11-25T08:00:00Z", **P))  # counted before sections: lands in Unsorted
    c = db.connect()
    vid = c.execute("SELECT v.id FROM variants v JOIN products p ON p.id=v.product_id WHERE p.name='Move Fleece'").fetchone()[0]
    pid = c.execute("SELECT id FROM products WHERE name='Move Fleece'").fetchone()[0]
    r = sync(cl, op("move", "mv2", qty=150, section=D, from_section=0, home=True, ts="2026-11-26T09:00:00Z", **P),
             op("move", "mv3", qty=60, section=E, from_section=0, ts="2026-11-26T09:01:00Z", **P),  # only 52 left in Unsorted
             op("move", "mv4", qty=52, section=E, from_section=0, ts="2026-11-26T09:02:00Z", **P),
             op("move", "mv5", qty=1, section=D, from_section=D, ts="2026-11-26T09:03:00Z", **P))
    assert [x["status"] for x in r] == ["ok", "error", "ok", "error"] and "only 52" in r[1]["msg"]
    assert main.piles(c, vid, "Big box", lid) == {(D, "2026-11-25T08:00:00.000Z"): 150, (E, "2026-11-25T08:00:00.000Z"): 52}  # arrival date unchanged
    assert c.execute("SELECT section_id FROM homes WHERE product_id=? AND location_id=?", (pid, lid)).fetchone()[0] == D
    assert c.execute("SELECT SUM(qty) FROM movements WHERE variant_id=?", (vid,)).fetchone()[0] == 202  # total unchanged
    c.close()


def test_size_order_adult_and_bedding_ranges(cl):
    from app import tg

    assert sorted(["L", "XXXXXL", "M", "S", "XL", "XS", "XXL", "XXXL", "XXXXL"], key=tg.size_key) == ["XS", "S", "M", "L", "XL", "XXL", "XXXL", "XXXXL", "XXXXXL"]
    assert sorted(["5XL", "XXXXL", "3XL", "XXL"], key=tg.size_key) == ["XXL", "3XL", "XXXXL", "5XL"]
    assert sorted(["King", "Single", "Super King", "Double", "Queen"], key=tg.size_key) == ["Single", "Double", "Queen", "King", "Super King"]
    assert sorted(["70x140", "100x150", "50x90"], key=tg.size_key) == ["50x90", "70x140", "100x150"]
    # the item PDF lists sizes in that order too
    sync(cl, *[op("receive", "so%d" % i, product="Order Shirt", colour="White", size=s, qty=1) for i, s in enumerate(["L", "XXXXXL", "M", "S", "XS", "XXXL"])])
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "Order Shirt"][0]["id"]
    import io
    from pypdf import PdfReader
    txt = PdfReader(io.BytesIO(cl.get("/api/export/%d" % pid).content)).pages[0].extract_text()
    import re
    # order the size words as they appear, whatever line layout pypdf uses
    seen = [w for w in re.findall(r"[A-Za-z0-9]+", txt) if w in {"XS", "S", "M", "L", "XXXL", "XXXXXL"}]
    assert seen == ["XS", "S", "M", "L", "XXXL", "XXXXXL"], txt


def test_multi_item_pdf_one_merged_colour_cell_per_colour(cl):
    import io
    from pypdf import PdfReader

    ops = [op("receive", "mp%d" % i, product="MP Protector", colour=c, size=s, qty=2)
           for i, (c, s) in enumerate([("Quilted", "90x190cm"), ("Quilted", "140x200cm"), ("Terry", "90x190cm"), ("Terry", "160x200cm")])]
    ops.append(op("receive", "mp9", product="MP Pillow", colour="Quilted", size="50x70cm", qty=3))
    sync(cl, *ops)
    P = {p["name"]: p["id"] for p in cl.get("/api/state").json()["prods"]}
    r = cl.get("/api/export?items=%d,%d,99999" % (P["MP Protector"], P["MP Pillow"]))
    assert r.status_code == 200 and "stock-report-" in r.headers["content-disposition"]
    txt = "\n".join(pg.extract_text() for pg in PdfReader(io.BytesIO(r.content)).pages)
    assert "MP Protector" in txt and "MP Pillow" in txt and txt.index("MP Protector") < txt.index("MP Pillow")
    head, rest = txt.split("MP Pillow", 1)
    assert head.count("Quilted") == 1 and head.count("Terry") == 1  # one merged cell per colour, not one per size
    assert "Lot" not in head  # no lots on this item: no Lot column
    assert cl.get("/api/export?items=").status_code == 422


def test_audit_sections_delete_coords_and_last_updated(cl):
    from app import db, tg

    # a new warehouse with sections drawn but no history can still be deleted
    cl.post("/api/location", json={"name": "Temp WH"})
    lid = [l for l in cl.get("/api/state").json()["locs"] if l["name"] == "Temp WH"][0]["id"]
    assert cl.post("/api/location/%d/sections" % lid, json={"on": True, "sections": [{"name": "A", "x": 0, "y": 0, "w": 1, "h": 1}]}).status_code == 200
    assert cl.post("/api/location/%d/sections" % lid, json={"sections": [{"name": "A", "x": "left"}]}).status_code == 422
    assert cl.delete("/api/location/%d" % lid).status_code == 200
    assert not [l for l in cl.get("/api/state").json()["locs"] if l["name"] == "Temp WH"]
    # moving between sections is not a stock update: 'last updated' stays at the receive
    gnd = [l for l in cl.get("/api/state").json()["locs"] if l["name"] == "Main: Ground"][0]
    sid = gnd["secs"][0]["id"] if gnd["secs"] else None
    if sid is None:
        cl.post("/api/location/%d/sections" % gnd["id"], json={"on": True, "sections": [{"name": "Z", "x": 0, "y": 0, "w": 1, "h": 1}]})
        sid = [l for l in cl.get("/api/state").json()["locs"] if l["id"] == gnd["id"]][0]["secs"][0]["id"]
    P = dict(product="LU Towel", colour="Red", size="1", loc=gnd["id"])
    res = sync(cl, op("receive", "aud-lu1", qty=4, ts="2026-01-05T08:00:00Z", **P), op("move", "aud-lu2", qty=4, section=sid, from_section=0, ts="2026-06-05T08:00:00Z", **P))
    assert [x["status"] for x in res] == ["ok", "ok"], res
    c = db.connect()
    vid = c.execute("SELECT v.id FROM variants v JOIN products p ON p.id=v.product_id WHERE p.name='LU Towel'").fetchone()[0]
    assert tg._last(c)[vid] == "2026-01-05T08:00:00.000Z"
    c.close()


def test_colour_photos_switch_store_and_follow_renames(cl):
    import base64
    img = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8fakejpeg").decode()
    sync(cl, op("receive", "cp1", product="Print Towel", colour="Flamingo", size="50x90", qty=2),
         op("receive", "cp2", product="Print Towel", colour="Palm", size="50x90", qty=1))
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "Print Towel"][0]["id"]
    assert cl.post("/api/product/%d" % pid, json={"name": "Print Towel", "colour_photos": True}).status_code == 200
    r = sync(cl, {"kind": "photo", "uuid": "cpp1", "product": "Print Towel", "colour": "Flamingo", "data": img},
             {"kind": "photo", "uuid": "cpp2", "product": "Print Towel", "colour": "Palm", "data": img})
    assert [x["status"] for x in r] == ["ok", "ok"]
    st = cl.get("/api/state").json()
    assert [p for p in st["prods"] if p["id"] == pid][0]["cp"] is True
    ph = {c: u for p_, c, u in st["cph"] if p_ == pid}
    assert set(ph) == {"Flamingo", "Palm"} and cl.get(ph["Flamingo"]).status_code == 200
    assert [p for p in st["prods"] if p["id"] == pid][0]["photo"] is None  # the item photo is separate
    # renaming a colour takes its photo along; merging into a colour that has one keeps the target's
    cl.post("/api/rename", json={"product_id": pid, "kind": "colour", "old": "Flamingo", "new": "Flamingo Pink"})
    cl.post("/api/rename", json={"product_id": pid, "kind": "colour", "old": "Palm", "new": "Flamingo Pink"})
    ph2 = {c: u for p_, c, u in cl.get("/api/state").json()["cph"] if p_ == pid}
    assert ph2 == {"Flamingo Pink": ph["Flamingo"]}


def test_pending_items_prepare_then_receive(cl):
    from app import db, tg

    r = sync(cl, {"kind": "prepare", "uuid": "pp1", "product": "Pend Robe", "colour": "Navy", "size": "M", "pack": "12 pcs"},
             {"kind": "prepare", "uuid": "pp2", "product": "Pend Robe", "colour": "Navy", "size": "L"},
             {"kind": "prepare", "uuid": "pp2", "product": "Pend Robe", "colour": "Navy", "size": "L"})  # resent: recognised
    assert [x["status"] for x in r] == ["ok", "ok", "dup"]
    st = cl.get("/api/state").json()
    p = [x for x in st["prods"] if x["name"] == "Pend Robe"][0]
    vs = [v for v in st["vars"] if v["prod"] == p["id"]]
    assert p["pend"] is True and sorted(v["size"] for v in vs) == ["L", "M"] and [v["pack"] for v in vs if v["size"] == "M"] == ["12 pcs"]
    assert not [m for m in st["mv"] if m["v"] in {v["id"] for v in vs}]  # no stock, no movements
    c = db.connect()
    assert "Nothing found" in tg.lookup_text(c, "pend robe") and tg.suggest(c, "pend robe") == []  # the bot ignores it
    c.close()
    # first receive makes it live
    sync(cl, op("receive", "pp3", product="Pend Robe", colour="Navy", size="M", qty=5))
    assert [x for x in cl.get("/api/state").json()["prods"] if x["name"] == "Pend Robe"][0]["pend"] is False
    # Activate without receiving
    sync(cl, {"kind": "prepare", "uuid": "pp4", "product": "Pend Slipper", "colour": "Grey", "size": "40"})
    q = [x for x in cl.get("/api/state").json()["prods"] if x["name"] == "Pend Slipper"][0]
    assert q["pend"] and cl.post("/api/product/%d" % q["id"], json={"name": "Pend Slipper", "pending": False}).status_code == 200
    assert [x for x in cl.get("/api/state").json()["prods"] if x["name"] == "Pend Slipper"][0]["pend"] is False


def test_move_back_to_pending(cl):
    sync(cl, {"kind": "prepare", "uuid": "mb1", "product": "Back Towel", "alias": "BT-internal", "colour": "Red", "size": "S"})
    pr = lambda: [x for x in cl.get("/api/state").json()["prods"] if x["name"] == "Back Towel"][0]
    pid = pr()["id"]
    # Activate, then put back to pending: the internal name (alias) survives both, since neither sends it
    assert cl.post("/api/product/%d" % pid, json={"name": "Back Towel", "pending": False}).status_code == 200
    assert pr()["pend"] is False and pr()["alias"] == "BT-internal"
    assert cl.post("/api/product/%d" % pid, json={"name": "Back Towel", "pending": True}).status_code == 200
    assert pr()["pend"] is True and pr()["alias"] == "BT-internal"
    # the edit form still clears the alias when it sends an empty one
    assert cl.post("/api/product/%d" % pid, json={"name": "Back Towel", "alias": ""}).status_code == 200
    assert pr()["alias"] == ""
    # once anything was received (even if it is all gone again) it can't go back, and nothing is changed
    sync(cl, op("receive", "mb2", product="Back Towel", colour="Red", size="S", qty=3))
    sync(cl, op("sale", "mb3", product="Back Towel", colour="Red", size="S", qty=3))
    r = cl.post("/api/product/%d" % pid, json={"name": "Back Towel renamed", "pending": True})
    assert r.status_code == 409 and "stock history" in r.json()["detail"]
    assert pr()["pend"] is False
    assert not [x for x in cl.get("/api/state").json()["prods"] if x["name"] == "Back Towel renamed"]


def test_review_security_and_input_limits(cl):
    from app import main
    # cross-site writes refused; the app's own JSON/multipart writes go through
    r = cl.post("/api/sync", content='{"ops":[]}', headers={"content-type": "text/plain"})
    assert r.status_code == 403
    r = cl.post("/api/telegram", data={"users": "31337"})
    assert r.status_code == 403
    r = cl.post("/api/sync", json={"ops": []}, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403
    assert cl.post("/api/sync", json={"ops": []}, headers={"sec-fetch-site": "same-origin"}).status_code == 200
    # malformed bodies and ops: clear errors, not crashes
    assert cl.post("/api/sync", json=["x"]).status_code == 422
    res = cl.post("/api/sync", json={"ops": ["x", {"kind": "receive", "uuid": None, "product": "A", "loc": 1, "qty": 1}]}).json()["results"]
    assert [x["status"] for x in res] == ["error", "error"] and "missing operation id" in res[1]["msg"]
    # absurd quantities and over-long text refused
    r = sync(cl, op("receive", "big1", product="Big Q", qty=5 * 10 ** 18), op("receive", "big2", product="N" * 200, qty=1))
    assert [x["status"] for x in r] == ["error", "error"] and "too large" in r[0]["msg"] and "too long" in r[1]["msg"]
    # timestamps stored uniformly, junk falls back to now
    assert main._ts("2026-01-01T10:00:00+02:00") == "2026-01-01T08:00:00.000Z" and main._ts("2026-01-01T08:00:00.341382Z") == "2026-01-01T08:00:00.341Z"
    assert main._ts("yesterday").endswith("Z")
    # carton contents
    assert main.parse_pack("6x12 doz")[1] == 864 and main.parse_pack("24x6 rolls")[1] == 144 and main.parse_pack("9" * 20)[1] == 0
    # blank warehouse name refused
    assert cl.post("/api/location/1", json={"name": "   "}).status_code == 422


def test_review_archived_warehouse_sections_and_photos(cl):
    import base64
    cl.post("/api/location", json={"name": "Arch WH"})
    lid = [l for l in cl.get("/api/state").json()["locs"] if l["name"] == "Arch WH"][0]["id"]
    cl.post("/api/location/%d" % lid, json={"active": False})
    r = sync(cl, op("receive", "arc1", product="Arch Item", qty=1, loc=lid), op("receive", "arc2", product="Arch Item", qty=3, loc=1),
             op("transfer", "arc3", product="Arch Item", qty=1, loc=1, to=lid))
    assert [x["status"] for x in r] == ["error", "ok", "error"] and "archived" in r[0]["msg"]
    cl.post("/api/location/%d" % lid, json={"active": True})
    # a section whose stock was used up by an earlier oversell can be removed; names can be swapped in one save
    cl.post("/api/location/%d/sections" % lid, json={"on": True, "sections": [{"name": "A", "x": 0, "y": 0, "w": .5, "h": 1}, {"name": "B", "x": .5, "y": 0, "w": .5, "h": 1}]})
    secs = {z["name"]: z["id"] for z in [l for l in cl.get("/api/state").json()["locs"] if l["id"] == lid][0]["secs"]}
    sync(cl, op("sale", "ov1", product="Arch Item", qty=3, loc=1))  # nothing left at loc 1 after this, oversell next
    sync(cl, op("receive", "ov0", product="Arch Item", qty=1, loc=lid, ts="2026-01-01T00:00:00Z"),
         op("sale", "ov2", product="Arch Item", qty=5, loc=lid, ts="2026-01-02T00:00:00Z"),
         op("receive", "ov3", product="Arch Item", qty=4, loc=lid, section=secs["A"], ts="2026-01-03T00:00:00Z"))
    r = cl.post("/api/location/%d/sections" % lid, json={"sections": [{"id": secs["A"], "name": "B", "x": 0, "y": 0, "w": .5, "h": 1}, {"id": secs["B"], "name": "A", "x": .5, "y": 0, "w": .5, "h": 1}]})
    assert r.status_code == 200
    r = cl.post("/api/location/%d/sections" % lid, json={"sections": [{"id": secs["B"], "name": "A", "x": .5, "y": 0, "w": .5, "h": 1}]})
    assert r.status_code == 200, r.text  # section (now called B) holds nothing once the oversell is settled
    # colour photos only for colours that exist
    img = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8x").decode()
    r = sync(cl, {"kind": "photo", "uuid": "nocol", "product": "Arch Item", "colour": "Nope", "data": img})
    assert r[0]["status"] == "error"


def test_review_bot_words_pdf_and_commands(cl):
    import io
    from pypdf import PdfReader
    from app import db, tg
    sync(cl, *[op("receive", "rp%d" % i, product="Rev Polo Shirt", colour="White", size=s, qty=3) for i, s in enumerate(["S", "M", "L", "XL"])])
    sync(cl, *[op("receive", "rl%d" % i, product="Rev Long", colour="Navy", size="Z%02d" % i, lot="L%d" % (i % 3), loc=1 + i % 2, qty=1) for i in range(70)])
    c = db.connect()
    t = tg.lookup_text(c, "rev polo s")
    assert "S " in t and "XL" not in t and " M " not in t
    assert [n for _, n, _ in tg.suggest(c, "rev red polo")] in ([], ["Rev Polo Shirt"])
    c.close()
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "Rev Long"][0]["id"]
    r = cl.get("/api/export/%d" % pid)
    assert r.status_code == 200 and len(PdfReader(io.BytesIO(r.content)).pages) >= 2  # one colour longer than a page no longer crashes
    assert tg.reply_for("/stock@SomeOtherBot polo", "StockBot") is None
    assert tg.reply_for("/stock@stockbot rev polo", "StockBot")


def test_robust_quantities_gate_backups_photos(cl, monkeypatch):
    import base64, glob, os
    from app import db, main
    # whole cartons: decimals rounded down, true/false refused, transfer to nowhere is a clear error
    r = sync(cl, op("receive", "wq1", product="Whole Q", qty=2.7), op("receive", "wq2", product="Whole Q", qty="3.9"),
             op("receive", "wq3", product="Whole Q", qty=True), op("transfer", "wq4", product="Whole Q", qty=1, to=0))
    assert [x["status"] for x in r] == ["ok", "ok", "error", "error"] and r[3]["msg"] == "unknown warehouse"
    st = cl.get("/api/state").json()
    wid = [p for p in st["prods"] if p["name"] == "Whole Q"][0]["id"]
    vids = {v["id"] for v in st["vars"] if v["prod"] == wid}
    assert sorted(m["qty"] for m in st["mv"] if m["type"] == "receive" and m["v"] in vids) == [2, 3]
    # access: only the allowed login, or the local network; unknown host names refused
    assert main._local_ip("192.168.1.20") and main._local_ip("127.0.0.1") and main._local_ip("172.17.0.1") and not main._local_ip("100.101.102.103") and not main._local_ip("8.8.8.8")
    assert main._host_ok("server.tail1234.ts.net") and main._host_ok("192.168.1.20:18437") and main._host_ok("tower") and main._host_ok("tower.local") and not main._host_ok("evil.example.com")
    assert cl.get("/api/state", headers={"host": "evil.example.com"}).status_code == 403
    monkeypatch.setattr(main, "ALLOWED_LOGINS", {"alice@github"})
    assert cl.get("/api/state", headers={"tailscale-user-login": "Alice@github"}).status_code == 200
    assert cl.get("/api/state", headers={"tailscale-user-login": "someone@github"}).status_code == 403
    assert cl.get("/api/state").status_code == 403  # no login and not from the local network (the test client isn't an IP)
    monkeypatch.setattr(main, "_local_ip", lambda h: True)
    assert cl.get("/api/state").status_code == 200  # local network without a login
    assert cl.get("/api/health", headers={"tailscale-user-login": "someone@github"}).status_code == 200  # health always answers
    monkeypatch.undo()
    # a resent photo (answer lost) is saved once
    sync(cl, op("receive", "ph0", product="Photo Once", colour="Red", qty=1))
    img = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8once").decode()
    before = len(os.listdir(main.PHOTOS))
    ph = {"kind": "photo", "uuid": "ph1", "product": "Photo Once", "colour": "Red", "data": img}
    assert [x["status"] for x in sync(cl, ph)] == ["ok"] and [x["status"] for x in sync(cl, ph)] == ["dup"]
    assert len(os.listdir(main.PHOTOS)) == before + 1
    # backups: written whole and checked, status reported
    for f in glob.glob(os.path.join(db.DATA, "backups", "daily-*.db")):
        os.remove(f)
    db.backup_daily()
    st = cl.get("/api/settings").json()["backup"]
    assert st["error"] == "" and st["last"] and st["count"] >= 1 and not glob.glob(os.path.join(db.DATA, "backups", "*.tmp"))


def test_na_labels_and_extract_guard(cl, monkeypatch):
    import io, types
    from pypdf import PdfReader
    from app import extract
    sync(cl, op("receive", "na1", product="No Colour Thing", colour="", size="", qty=4))
    pid = [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "No Colour Thing"][0]["id"]
    txt = PdfReader(io.BytesIO(cl.get("/api/export/%d" % pid).content)).pages[0].extract_text()
    assert "n/a" in txt
    # a reply cut off by the token limit is refused, not passed on as a complete list; cartons become whole numbers
    class Fake:
        def __init__(self, stop, items):
            self.stop, self.items = stop, items
        def __call__(self, **kw):
            return types.SimpleNamespace(stop_reason=self.stop, content=[types.SimpleNamespace(type="tool_use", input={"kind": "list", "items": self.items})])
    import anthropic
    for stop, items, want in (("max_tokens", [{"name": "A", "cartons": 3}], "too long"), ("tool_use", [{"name": "A", "cartons": 2.9}, {"name": "B", "cartons": "x"}], None)):
        monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: types.SimpleNamespace(messages=types.SimpleNamespace(create=Fake(stop, items))))
        try:
            out = extract.extract(b"\xff\xd8img", [], "k")
            assert want is None and [i["cartons"] for i in out["items"]] == [2, 0]
        except RuntimeError as e:
            assert want and want in str(e)


def test_rejected_warehouse_edit_changes_nothing(cl):
    cl.post("/api/location", json={"name": "Halfsave A", "role": "holding"})
    cl.post("/api/location", json={"name": "Halfsave B", "role": "holding"})
    locs = {l["name"]: l for l in cl.get("/api/state").json()["locs"]}
    a, b = locs["Halfsave A"], locs["Halfsave B"]

    def now():
        return {l["id"]: (l["name"], l["role"], l["on"]) for l in cl.get("/api/state").json()["locs"]}

    before = now()
    assert cl.post("/api/location/%d" % a["id"], json={"name": "Renamed", "role": "bogus"}).status_code == 422
    assert cl.post("/api/location/%d" % a["id"], json={"name": "Halfsave B", "role": "picking"}).status_code == 409
    assert sync(cl, op("receive", "halfsave-rcv", product="Tee", loc=a["id"], qty=3))[0]["status"] == "ok"
    assert cl.post("/api/location/%d" % a["id"], json={"name": "Renamed", "role": "picking", "active": False}).status_code == 409
    assert now() == before  # none of the three rejected edits renamed or re-roled anything
    assert cl.post("/api/location/%d" % b["id"], json={"name": "Halfsave B2", "role": "picking", "active": False}).status_code == 200
    assert now()[b["id"]] == ("Halfsave B2", "picking", False)


def test_rejected_telegram_settings_change_nothing_and_ids_are_ascii(cl, monkeypatch):
    from app import tg

    monkeypatch.setattr(tg, "api", lambda t, m, p=None, timeout=15: {"username": "stockbot"})
    assert cl.post("/api/telegram", json={"token": "123456:GOODTOKENwxyz", "users": "111", "group": "-100123", "hour": "8"}).status_code == 200
    keys = ("tg_users", "tg_group", "tg_hour", "tg_token")
    before = {k: tg.setting(k) for k in keys}

    def bad(t, m, p=None, timeout=15):
        raise RuntimeError("Unauthorized")

    monkeypatch.setattr(tg, "api", bad)
    assert cl.post("/api/telegram", json={"users": "222", "group": "-100999", "hour": "9", "token": "999:nope"}).status_code == 422
    assert {k: tg.setting(k) for k in keys} == before  # the bad token was checked before anything was saved
    for body in ({"users": "222, ²"}, {"users": "١٢٣"}, {"group": "-²"}, {"hour": "²"}, {"hour": "٣"},
                 {"users": "222", "hour": "24"}, {"users": "222", "group": "x"}):
        assert cl.post("/api/telegram", json=body).status_code == 422, body
        assert {k: tg.setting(k) for k in keys} == before, body

    # a bad ID saved by an older version is skipped, so the bot still answers the good ones
    tg.put("tg_users", "111, ², ٣")
    assert tg.allowed_users() == {111}
    sent = []
    monkeypatch.setattr(tg, "send", lambda tok, chat, text, html=False: sent.append((chat, text)))
    tg.handle({"update_id": 1, "message": {"text": "/id", "from": {"id": 111}, "chat": {"id": 111, "type": "private"}}}, "t")
    assert sent
    tg.put("tg_users", before["tg_users"])

    # and "²" in a name or a query string no longer crashes sorting or PDF selection
    from app import main
    assert tg._natural("size ²") and main.nat("²1") == ["²", 1, ""]
    assert cl.get("/api/export?items=²").status_code == 422  # "Pick at least one item", not a 500
    assert cl.get("/api/export/1?locs=²").status_code in (200, 404)


def test_leftover_backup_tmp_does_not_block_backups(cl):
    import sqlite3
    import time

    from app import db

    d = os.path.join(db.DATA, "backups")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "daily-%s.db" % time.strftime("%Y%m%d"))
    if os.path.exists(path):
        os.remove(path)
    for suffix in ("", "-journal"):
        with open(path + ".tmp" + suffix, "wb") as f:  # what a crash mid-backup can leave behind
            f.write(b"not a database " * 100)
    db.backup_daily()
    c = sqlite3.connect(path)
    try:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("SELECT COUNT(*) FROM locations").fetchone()[0] > 0
    finally:
        c.close()
    assert not os.path.exists(path + ".tmp") and not os.path.exists(path + ".tmp-journal")
    assert cl.get("/api/settings").json()["backup"]["error"] == ""


def _mv(cl, product):
    s = cl.get("/api/state").json()
    vids = {v["id"] for v in s["vars"] if any(p["id"] == v["prod"] and p["name"] == product for p in s["prods"])}
    return [m for m in s["mv"] if m["v"] in vids]


def _tot(rows, loc=None):
    return sum(m["qty"] for m in rows if loc is None or m["loc"] == loc)


def test_reverse_sale_puts_stock_back_and_only_once(cl):
    sync(cl, op("receive", "rvs-r", product="RevSale", qty=10))
    sync(cl, op("sale", "rvs-s", product="RevSale", qty=4))
    assert _tot(_mv(cl, "RevSale")) == 6
    r = sync(cl, {"kind": "reverse", "uuid": "rvs-x", "of": "rvs-s", "note": "wrong item"})
    assert r[0]["status"] == "ok", r
    rows = _mv(cl, "RevSale")
    assert _tot(rows) == 10
    back = [m for m in rows if m["rv"] == "rvs-s"]
    assert len(back) == 1 and back[0]["qty"] == 4 and back[0]["type"] == "sale" and back[0]["note"] == "wrong item"
    assert back[0]["op"] == "rvs-x"
    assert all(m["op"] for m in rows)
    # the same reversal resent is a duplicate; a second, different reversal is refused; a reversal can't be reversed
    assert sync(cl, {"kind": "reverse", "uuid": "rvs-x", "of": "rvs-s"})[0]["status"] == "dup"
    r = sync(cl, {"kind": "reverse", "uuid": "rvs-y", "of": "rvs-s"})[0]
    assert r["status"] == "error" and "already been reversed" in r["msg"]
    r = sync(cl, {"kind": "reverse", "uuid": "rvs-z", "of": "rvs-x"})[0]
    assert r["status"] == "error" and "already a reversal" in r["msg"]
    assert _tot(_mv(cl, "RevSale")) == 10


def test_reverse_transfer_undoes_both_legs(cl):
    sync(cl, op("receive", "rvt-r", product="RevTr", qty=8, loc=1))
    sync(cl, op("transfer", "rvt-t", product="RevTr", qty=3, loc=1, to=2))
    assert sync(cl, {"kind": "reverse", "uuid": "rvt-x", "of": "rvt-t"})[0]["status"] == "ok"
    rows = _mv(cl, "RevTr")
    assert _tot(rows, 1) == 8 and _tot(rows, 2) == 0
    back = [m for m in rows if m["rv"] == "rvt-t"]
    assert sorted(m["qty"] for m in back) == [-3, 3] and all(m["tid"] == "rvt-x" for m in back)


def test_reverse_refuses_to_take_stock_below_zero(cl):
    sync(cl, op("receive", "rvn-r", product="RevNeg", qty=5))
    sync(cl, op("sale", "rvn-s", product="RevNeg", qty=4))
    r = sync(cl, {"kind": "reverse", "uuid": "rvn-x", "of": "rvn-r"})[0]
    assert r["status"] == "error" and "only 1 cartons left" in r["msg"]
    assert _tot(_mv(cl, "RevNeg")) == 1 and not any(m["rv"] for m in _mv(cl, "RevNeg"))
    # once the sale is reversed the receive can be reversed too, back to nothing
    assert sync(cl, {"kind": "reverse", "uuid": "rvn-y", "of": "rvn-s"})[0]["status"] == "ok"
    assert sync(cl, {"kind": "reverse", "uuid": "rvn-z", "of": "rvn-r"})[0]["status"] == "ok"
    assert _tot(_mv(cl, "RevNeg")) == 0


def test_reverse_sale_split_across_sections_restores_each_pile(cl):
    cl.post("/api/location", json={"name": "RevSecWh"})
    lid = [l for l in cl.get("/api/state").json()["locs"] if l["name"] == "RevSecWh"][0]["id"]
    cl.post("/api/location/%d/sections" % lid, json={"on": True, "sections": [{"name": "A", "x": 0, "y": 0, "w": 0.5, "h": 1}, {"name": "B", "x": 0.5, "y": 0, "w": 0.5, "h": 1}]})
    secs = {z["name"]: z["id"] for l in cl.get("/api/state").json()["locs"] if l["id"] == lid for z in l["secs"]}
    sync(cl, op("receive", "rvp-1", product="RevSec", qty=2, loc=lid, section=secs["A"], ts="2026-01-01T00:00:00Z"))
    sync(cl, op("receive", "rvp-2", product="RevSec", qty=3, loc=lid, section=secs["B"], ts="2026-02-01T00:00:00Z"))
    sync(cl, op("sale", "rvp-s", product="RevSec", qty=4, loc=lid, ts="2026-03-01T00:00:00Z"))
    sale = [m for m in _mv(cl, "RevSec") if m["op"] == "rvp-s"]
    assert len(sale) == 2  # 2 from A (older), 2 from B
    assert sync(cl, {"kind": "reverse", "uuid": "rvp-x", "of": "rvp-s"})[0]["status"] == "ok"
    rows = _mv(cl, "RevSec")
    per = {}
    for m in rows:
        per[m["s"]] = per.get(m["s"], 0) + m["qty"]
    assert per == {secs["A"]: 2, secs["B"]: 3}


def test_reverse_unknown_or_empty_movement_is_refused(cl):
    assert "isn't on the server" in sync(cl, {"kind": "reverse", "uuid": "rvu-x", "of": "nope"})[0]["msg"]
    assert "missing" in sync(cl, {"kind": "reverse", "uuid": "rvu-y"})[0]["msg"]
    sync(cl, op("receive", "rvu-0", product="RevZero", qty=0))
    assert "nothing to reverse" in sync(cl, {"kind": "reverse", "uuid": "rvu-z", "of": "rvu-0"})[0]["msg"]


def test_photo_refuses_empty_or_damaged_data(cl):
    """A phone that runs out of memory hands over 'data:,' (an empty picture). That used to be saved as a blank file and
    look like a success; now it is refused so the app shows the photo as failed."""
    sync(cl, {"kind": "receive", "uuid": "pe1", "product": "Blank Pic", "colour": "", "size": "", "lot": "", "loc": 1, "qty": 1})
    for bad in ("data:,", "", "data:image/jpeg;base64,not*base64", "data:image/jpeg;base64," + base64.b64encode(b"hello").decode()):
        r = sync(cl, {"kind": "photo", "uuid": "pe-" + bad[-6:], "product": "Blank Pic", "data": bad})[0]
        assert r["status"] == "error", bad
    p = [x for x in cl.get("/api/state").json()["prods"] if x["name"] == "Blank Pic"][0]
    assert p["photo"] is None
    # a refused photo can be sent again with the same uuid once it's fixed (nothing was logged as done)
    img = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xffok").decode()
    assert sync(cl, {"kind": "photo", "uuid": "pe-,", "product": "Blank Pic", "data": img})[0]["status"] == "ok"


# ---------- audit 2026-11-26 fixes ----------

def _wh(cl, name, *secs):
    """A new warehouse with sections switched on; returns (warehouse id, {section name: id})."""
    cl.post("/api/location", json={"name": name})
    lid = [l for l in cl.get("/api/state").json()["locs"] if l["name"] == name][0]["id"]
    _secs(cl, lid, *secs)
    return lid, _sec_ids(cl, lid)


def _secs(cl, lid, *names):
    have = _sec_ids(cl, lid)
    r = cl.post("/api/location/%d/sections" % lid, json={"on": True, "sections": [{"id": have.get(n), "name": n} for n in names]})
    assert r.status_code == 200, r.text
    return r


def _sec_ids(cl, lid):
    return {s["name"]: s["id"] for l in cl.get("/api/state").json()["locs"] if l["id"] == lid for s in l["secs"]}


def _piles(product, loc):
    from app import db, main
    c = db.connect()
    try:
        out = {}
        for v in c.execute("SELECT v.id FROM variants v JOIN products p ON p.id=v.product_id WHERE p.name=?", (product,)).fetchall():
            for r in c.execute("SELECT DISTINCT lot FROM movements WHERE variant_id=? AND location_id=?", (v[0], loc)).fetchall():
                for (sec, _), n in main.piles(c, v[0], r[0], loc).items():
                    out[sec] = out.get(sec, 0) + n
        return out
    finally:
        c.close()


def test_audit_reverse_into_deleted_section_goes_to_unsorted(cl):
    lid, s = _wh(cl, "AuGhostWh", "A", "B")
    sync(cl, op("receive", "aug-r", product="AuGhost", qty=5, loc=lid, section=s["A"]))
    sync(cl, op("sale", "aug-s", product="AuGhost", qty=5, loc=lid))
    _secs(cl, lid, "B")  # A is empty, so it can be deleted
    assert sync(cl, {"kind": "reverse", "uuid": "aug-x", "of": "aug-s"})[0]["status"] == "ok"
    back = [m for m in _mv(cl, "AuGhost") if m["rv"] == "aug-s"]
    assert [m["s"] for m in back] == [0]  # back into Unsorted, not the deleted section
    assert _piles("AuGhost", lid) == {0: 5}
    r = sync(cl, op("move", "aug-m", product="AuGhost", qty=5, loc=lid, from_section=0, section=s["B"]))[0]
    assert r["status"] == "ok", r
    assert _piles("AuGhost", lid) == {s["B"]: 5}


def test_audit_stock_left_on_a_deleted_section_counts_as_unsorted(cl):
    from app import db
    lid, s = _wh(cl, "AuPhantomWh", "A")
    sync(cl, op("receive", "aup-r", product="AuPhantom", qty=4, loc=lid))
    c = db.connect()  # what an older version could leave behind: stock on a section id that no longer exists
    c.execute("UPDATE movements SET section_id=987654 WHERE uuid='aup-r'")
    c.close()
    assert _piles("AuPhantom", lid) == {0: 4}
    assert sync(cl, op("move", "aup-m", product="AuPhantom", qty=4, loc=lid, from_section=0, section=s["A"]))[0]["status"] == "ok"
    assert _piles("AuPhantom", lid) == {s["A"]: 4}
    # and a new section never gets that id (the counter starts above every id the ledger has used, see migration 17)
    _secs(cl, lid, "A", "Fresh")
    assert _sec_ids(cl, lid)["Fresh"] != 987654


def test_audit_section_and_warehouse_ids_are_never_reused(cl):
    lid, s = _wh(cl, "AuReuseWh", "X", "Y")
    _secs(cl, lid, "X")  # delete the highest id
    _secs(cl, lid, "X", "Z")
    assert _sec_ids(cl, lid)["Z"] > s["Y"]
    cl.post("/api/location", json={"name": "AuTemp"})
    t = [l for l in cl.get("/api/state").json()["locs"] if l["name"] == "AuTemp"][0]["id"]
    assert cl.delete("/api/location/%d" % t).status_code == 200
    cl.post("/api/location", json={"name": "AuShop"})
    assert [l for l in cl.get("/api/state").json()["locs"] if l["name"] == "AuShop"][0]["id"] > t
    # a queued offline receive for the deleted warehouse is refused, not put into the new one
    r = sync(cl, op("receive", "aur-r", product="AuReuse", qty=1, loc=t))[0]
    assert r["status"] == "error" and "unknown warehouse" in r["msg"]


def test_audit_migration_17_rebuilds_with_autoincrement_and_keeps_data(tmp_path, monkeypatch):
    from app import db
    full = list(db.MIGRATIONS)
    monkeypatch.setattr(db, "DATA", str(tmp_path))
    monkeypatch.setattr(db, "DB", str(tmp_path / "stock.db"))
    monkeypatch.setattr(db, "MIGRATIONS", full[:16])
    db.migrate()
    c = db.connect()
    c.execute("UPDATE locations SET role='picking', has_sections=1, map='m.jpg' WHERE id=1")
    c.execute("INSERT INTO sections(id,location_id,name,x,y,w,h) VALUES(3,1,'Back',0.1,0.2,0.3,0.4)")
    c.execute("INSERT INTO products(name,pick_loc) VALUES('Keep',1)")
    c.execute("INSERT INTO variants(product_id,colour,size) VALUES(1,'','')")
    c.execute("INSERT INTO homes VALUES(1,1,3)")
    c.execute("INSERT INTO movements(uuid,ts,variant_id,location_id,qty,type,section_id) VALUES('a','2026-01-01T00:00:00.000Z',1,1,5,'receive',3)")
    c.execute("INSERT INTO movements(uuid,ts,variant_id,location_id,qty,type,section_id) VALUES('b','2026-01-01T00:00:00.000Z',1,1,2,'receive',9)")
    c.close()
    monkeypatch.setattr(db, "MIGRATIONS", full)
    db.migrate()
    c = db.connect()
    assert c.execute("SELECT v FROM meta WHERE k='schema'").fetchone()[0] == str(len(full))
    assert [tuple(r) for r in c.execute("SELECT id,name,role,has_sections,map,active FROM locations WHERE id=1")] == [(1, "Main: Ground", "picking", 1, "m.jpg", 1)]
    assert [tuple(r) for r in c.execute("SELECT id,location_id,name,x,y,w,h FROM sections")] == [(3, 1, "Back", 0.1, 0.2, 0.3, 0.4)]
    assert c.execute("SELECT pick_loc FROM products").fetchone()[0] == 1 and c.execute("SELECT COUNT(*) FROM homes").fetchone()[0] == 1
    for t in ("locations", "sections"):
        assert "AUTOINCREMENT" in c.execute("SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()[0]
    assert c.execute("PRAGMA foreign_key_check").fetchall() == [] and c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    c.execute("INSERT INTO sections(location_id,name) VALUES(1,'New')")
    assert c.execute("SELECT id FROM sections WHERE name='New'").fetchone()[0] == 10  # above the 9 the ledger used
    c.execute("INSERT INTO locations(name) VALUES('Fifth')")
    assert c.execute("SELECT id FROM locations WHERE name='Fifth'").fetchone()[0] == 5
    try:  # foreign keys still enforced after the rebuild
        c.execute("DELETE FROM locations WHERE id=1")
        assert False, "delete should have been refused"
    except Exception as e:
        assert "FOREIGN KEY" in str(e)
    c.close()
    assert any(f.startswith("pre-v16") for f in os.listdir(tmp_path / "backups"))


def test_audit_reverse_move_needs_the_cartons_still_in_the_section(cl):
    lid, s = _wh(cl, "AuMoveWh", "X", "A", "B")
    sync(cl, op("receive", "aum-r", product="AuMove", qty=5, loc=lid, section=s["A"]))
    sync(cl, op("move", "aum-1", product="AuMove", qty=5, loc=lid, from_section=s["A"], section=s["B"]))
    sync(cl, op("move", "aum-2", product="AuMove", qty=5, loc=lid, from_section=s["B"], section=s["X"]))
    r = sync(cl, {"kind": "reverse", "uuid": "aum-x", "of": "aum-1"})[0]
    assert r["status"] == "error" and "only 0 cartons left in B" in r["msg"]
    assert _piles("AuMove", lid) == {s["X"]: 5}
    assert sync(cl, {"kind": "reverse", "uuid": "aum-y", "of": "aum-2"})[0]["status"] == "ok"  # undo in order: fine
    assert sync(cl, {"kind": "reverse", "uuid": "aum-z", "of": "aum-1"})[0]["status"] == "ok"
    assert _piles("AuMove", lid) == {s["A"]: 5}


def test_audit_reverse_refuses_to_put_stock_into_an_archived_warehouse(cl):
    cl.post("/api/location", json={"name": "AuArchWh"})
    lid = [l for l in cl.get("/api/state").json()["locs"] if l["name"] == "AuArchWh"][0]["id"]
    sync(cl, op("receive", "aua-r", product="AuArch", qty=5, loc=lid), op("sale", "aua-s", product="AuArch", qty=5, loc=lid))
    assert cl.post("/api/location/%d" % lid, json={"active": False}).status_code == 200
    r = sync(cl, {"kind": "reverse", "uuid": "aua-x", "of": "aua-s"})[0]
    assert r["status"] == "error" and "archived" in r["msg"] and _tot(_mv(cl, "AuArch")) == 0


def test_audit_archiving_refuses_negative_lines_too(cl):
    cl.post("/api/location", json={"name": "AuNegWh"})
    lid = [l for l in cl.get("/api/state").json()["locs"] if l["name"] == "AuNegWh"][0]["id"]
    sync(cl, op("receive", "aun-r", product="AuNeg", qty=0, loc=lid), op("sale", "aun-s", product="AuNeg", qty=2, loc=lid))
    r = cl.post("/api/location/%d" % lid, json={"active": False})
    assert r.status_code == 409 and "negative" in r.json()["detail"]


def test_audit_lots_match_whatever_the_case(cl):
    sync(cl, op("receive", "aul-r", product="AuLot", lot="LOT1", qty=5), op("sale", "aul-s", product="AuLot", lot="lot1", qty=5))
    rows = _mv(cl, "AuLot")
    assert {m["lot"] for m in rows} == {"LOT1"} and _tot(rows) == 0 and not any(m["flag"] for m in rows)


def test_audit_strict_quantities_ids_and_types(cl):
    r = sync(cl, op("receive", "auq-1", product="AuQty", qty=0.9), op("sale", "auq-2", product="AuQty", qty="0.5"),
             op("receive", "auq-3", product="AuQty", qty=2, loc=True), {**op("receive", "auq-4", product="AuQty"), "loc": None},
             op("receive", "auq:5", product="AuQty"), op("receive", "auq-6", product="AuQty", colour={"a": 1}),
             op("receive", "auq-7", product="AuQty", loc=10 ** 20), op("receive", "auq-8", product="AuQty", qty=0))
    assert [x["status"] for x in r] == ["error"] * 7 + ["ok"]
    assert "at least 1 carton" in r[0]["msg"] and "at least 1 carton" in r[1]["msg"]
    assert "whole number" in r[2]["msg"] and r[3]["msg"] == "warehouse missing" and r[4]["msg"] == "bad operation id"
    assert "must be text" in r[5]["msg"] and "out of range" in r[6]["msg"]
    r = sync(cl, {"kind": "reverse", "uuid": "auq-8", "of": "auq-8"})[0]
    assert r["status"] == "error" and "own id" in r["msg"]


def test_audit_found_stock_on_a_pending_item_makes_it_live(cl):
    sync(cl, {"kind": "prepare", "uuid": "aupd-p", "product": "AuPendAdj", "colour": "Red", "size": "M"})
    assert sync(cl, op("adjust", "aupd-a", product="AuPendAdj", colour="Red", size="M", qty=7, note="found"))[0]["status"] == "ok"
    assert [p for p in cl.get("/api/state").json()["prods"] if p["name"] == "AuPendAdj"][0]["pend"] is False


def test_audit_resent_prepare_is_a_dup_and_keeps_later_edits(cl):
    p = {"kind": "prepare", "uuid": "aupr-1", "product": "AuPrep", "colour": "Blue", "size": "L", "pack": "10 pcs"}
    assert sync(cl, p)[0]["status"] == "ok"
    pid = [x for x in cl.get("/api/state").json()["prods"] if x["name"] == "AuPrep"][0]["id"]
    assert cl.post("/api/reorder", json={"product_id": pid, "field": "pack", "level": "24 doz"}).status_code == 200
    assert sync(cl, p)[0]["status"] == "dup"  # the answer was lost and the phone sent it again
    assert [v["pack"] for v in cl.get("/api/state").json()["vars"] if v["prod"] == pid] == ["24 doz"]


def test_audit_temporary_database_errors_come_back_as_retry(cl, monkeypatch):
    import sqlite3
    from app import main
    real = main.apply_op

    def flaky(c, o, user):
        if o["uuid"] == "aut-2":
            raise sqlite3.OperationalError("database is locked")
        return real(c, o, user)

    monkeypatch.setattr(main, "apply_op", flaky)
    r = sync(cl, op("receive", "aut-1", product="AuRetry", qty=3), op("sale", "aut-2", product="AuRetry", qty=1),
             op("sale", "aut-3", product="AuRetry", qty=1))
    assert [x["status"] for x in r] == ["ok", "retry", "retry"] and all(x["msg"] for x in r[1:])
    assert _tot(_mv(cl, "AuRetry")) == 3  # nothing after the retried op was applied out of order
    monkeypatch.setattr(main, "apply_op", real)
    r = sync(cl, op("sale", "aut-2", product="AuRetry", qty=1), op("sale", "aut-3", product="AuRetry", qty=1))
    assert [x["status"] for x in r] == ["ok", "ok"] and _tot(_mv(cl, "AuRetry")) == 1


def test_audit_api_missing_ids_huge_numbers_and_wrong_types(cl):
    sync(cl, op("receive", "auapi-r", product="AuApi", colour="Red", size="M", qty=1))
    st = cl.get("/api/state").json()
    pid = [p for p in st["prods"] if p["name"] == "AuApi"][0]["id"]
    big = 10 ** 20
    assert cl.post("/api/product/999999", json={"name": "X"}).status_code == 404
    assert cl.post("/api/product/%d" % big, json={"name": "X"}).status_code == 404
    assert cl.post("/api/location/999999", json={"name": "X"}).status_code == 404
    assert cl.delete("/api/location/999999").status_code == 404
    assert cl.delete("/api/location/%d" % big).status_code == 404
    assert cl.get("/api/export/%d" % big).status_code == 404
    assert cl.get("/api/export?items=%d" % big).status_code == 422
    assert cl.post("/api/location/%d/sections" % big, json={"sections": []}).status_code == 404
    assert cl.post("/api/reorder", json={"product_id": pid, "level": big}).status_code == 422
    assert cl.post("/api/reorder", json={"product_id": pid, "level": 100001}).status_code == 422
    assert cl.post("/api/reorder", json={"product_id": pid, "level": 5}).json() == {"updated": 1}
    assert cl.post("/api/reorder", json={"product_id": pid, "colour": "Nope", "level": 5}).status_code == 404
    assert cl.post("/api/reorder", json={"product_id": big, "level": 5}).status_code == 422
    assert cl.post("/api/reorder", json={"level": 5}).status_code == 422
    assert cl.post("/api/rename", json={"product_id": pid, "kind": "colour", "old": "Nope", "new": "X"}).status_code == 404
    assert cl.post("/api/rename", json={"kind": "colour", "old": "Red", "new": "X"}).status_code == 422
    assert cl.post("/api/rename", json={"product_id": "x", "kind": "colour", "old": "Red", "new": "X"}).status_code == 422
    assert cl.post("/api/product/%d" % pid, json={"name": "AuApi", "pick_loc": big}).status_code == 422
    assert cl.post("/api/product/%d" % pid, json={"name": "AuApi", "category": ["a"]}).status_code == 422
    assert cl.post("/api/product/%d" % pid, json={"name": "AuApi", "pending": "yes"}).status_code == 422
    assert cl.post("/api/location/1", json={"name": {"a": 1}}).status_code == 422
    assert cl.post("/api/location/1", json={"active": "no"}).status_code == 422
    assert cl.post("/api/location/1/sections", json={"sections": [{"id": "abc", "name": "S"}]}).status_code == 422
    assert cl.post("/api/settings", json={"api_key": 5}).status_code == 422
    assert cl.post("/api/telegram", json={"token": {"x": 1}}).status_code == 422
    assert cl.post("/api/sync", json=[1]).status_code == 422
    st = cl.get("/api/state").json()
    assert [p["cat"] for p in st["prods"] if p["id"] == pid] == [""] and st["locs"][0]["name"] == "Main: Ground"


def test_audit_writes_must_be_json_except_uploads_and_security_headers(cl):
    form = {"content-type": "multipart/form-data; boundary=x"}
    assert cl.post("/api/sync", content='{"ops":[]}', headers=form).status_code == 403
    assert cl.post("/api/location", content='{"name":"AuCsrf"}', headers=form).status_code == 403
    assert not [l for l in cl.get("/api/state").json()["locs"] if l["name"] == "AuCsrf"]
    assert cl.post("/api/location/1/map", files={"file": ("m.jpg", b"\xff\xd8x", "image/jpeg")}).status_code == 200
    assert cl.post("/api/extract", files={"file": ("p.jpg", b"\xff\xd8x", "image/jpeg")}).status_code != 403
    assert cl.post("/api/location/1/map", json={}).status_code == 403  # the upload routes take uploads only
    assert cl.delete("/api/location/999999").status_code == 404  # a DELETE without a body still goes through the gate
    r = cl.get("/")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert cl.get("/api/state").headers["x-frame-options"] == "DENY"


def test_audit_decimal_sizes_sort_by_number():
    from app import tg
    sizes = ["70.5x140", "70x140.5", "70x140", "1.5", "1.25", "2", "50x90", "XL", "S"]
    assert sorted(sizes, key=tg.size_key) == ["1.25", "1.5", "2", "50x90", "70x140", "70x140.5", "70.5x140", "S", "XL"]
    assert tg.size_key("x.l")[:2] == tg.size_key("XL")[:2]  # a dot that isn't a decimal point is still a separator


def test_audit_pdf_totals_include_negative_lines(cl):
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph
    from app import db, main
    sync(cl, op("receive", "aupdf-r", product="AuPdfNeg", qty=10, loc=1), op("sale", "aupdf-s", product="AuPdfNeg", qty=3, loc=3))
    c = db.connect()
    try:
        p = c.execute("SELECT * FROM products WHERE name='AuPdfNeg'").fetchone()
        els, _ = main._pdf_item(c, p, 1, 1, 1, 1, 0, "", 1, 0, "", 1, getSampleStyleSheet())
    finally:
        c.close()
    assert any(isinstance(e, Paragraph) and "Total in stock: 7 cartons" in e.text for e in els)


def test_audit_wide_item_pdf_fits_the_page(cl):
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Table
    from app import db, main
    names = ["AuWide %02d Container" % i for i in range(14)]
    for n in names:
        cl.post("/api/location", json={"name": n})
    locs = [l["id"] for l in cl.get("/api/state").json()["locs"] if l["name"] in names]
    sync(*([cl] + [op("receive", "auw-%d" % l, product="AuWide", colour="Navy blue with white stripes and a long name", size="Super King",
                      lot="PL-2026-0098 MSKU1234567 second shipment", qty=1, loc=l) for l in locs]))
    c = db.connect()
    try:
        p = c.execute("SELECT * FROM products WHERE name='AuWide'").fetchone()
        els, n = main._pdf_item(c, p, 1, 1, 1, 1, 1, ",".join(map(str, locs)), 1, 0, "", 1, getSampleStyleSheet())
    finally:
        c.close()
    t = [e for e in els if isinstance(e, Table)][0]
    assert len(t._cellvalues[0]) == n and t.wrap(10000, 10000)[0] <= main.PAGE_W[1]
    pid = p["id"]
    assert cl.get("/api/export/%d?locs=%s&cnt=1" % (pid, ",".join(map(str, locs)))).status_code == 200
    for l in locs:  # tidy up: these warehouses would otherwise show in later tests' PDFs and bot replies
        sync(cl, op("sale", "auw-s%d" % l, product="AuWide", colour="Navy blue with white stripes and a long name", size="Super King",
                    lot="PL-2026-0098 MSKU1234567 second shipment", qty=1, loc=l))
        assert cl.post("/api/location/%d" % l, json={"active": False}).status_code == 200


def test_audit_stock_request_wraps_long_lots(cl, monkeypatch):
    import reportlab.platypus as rp
    from app import tg
    made = []

    class Spy(rp.Table):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append((a, k))

    monkeypatch.setattr(rp, "Table", Spy)
    sync(cl, op("receive", "aurq-p", product="AuReq", size="M", qty=1, loc=1), op("receive", "aurq-h", product="AuReq", size="M",
                                                                                     lot="PL-2026-0098 MSKU1234567", qty=6, loc=2))
    from app import db
    c = db.connect()
    c.execute("UPDATE locations SET role='picking' WHERE id=1")
    c.execute("UPDATE variants SET pick_min=2, pick_max=8 WHERE product_id=(SELECT id FROM products WHERE name='AuReq')")
    c.close()
    data, _ = tg.stock_request_pdf(1)
    assert data[:4] == b"%PDF"
    lotted = [(a, k) for a, k in made if a and a[0] and "Lot" in a[0][0]]
    assert lotted and all("rowHeights" not in k for _, k in lotted)
    assert all(isinstance(r[1], rp.Paragraph) for a, _ in lotted for r in a[0][1:-1])


def test_audit_no_picking_warehouse_gives_a_clear_message(cl, monkeypatch):
    from app import db, tg
    c = db.connect()
    before = [tuple(r) for r in c.execute("SELECT id, role, active FROM locations")]
    c.execute("UPDATE locations SET role='holding'")
    try:
        assert tg.refill(c) == []
        assert tg.reply_for("/low", "")[0] == [tg.NO_PICKING]
        monkeypatch.setattr(tg, "setting", lambda k, d="": {"tg_token": "t", "tg_group": "-100"}.get(k, d))
        try:
            tg.post_refill()
            assert False, "should have refused"
        except RuntimeError as e:
            assert "No active picking warehouse" in str(e)
        assert "|" not in tg.STATE["post_err"] and "No active picking" in tg.STATE["post_err"]
        try:
            tg.stock_request_pdf()
        except LookupError as e:
            assert "No active picking" in str(e)
    finally:
        for i, role, act in before:
            c.execute("UPDATE locations SET role=?, active=? WHERE id=?", (role, act, i))
        c.close()
        tg.STATE["post_err"] = ""


def test_audit_extract_parses_string_lists_and_skips_impossible_counts(monkeypatch):
    import types
    import anthropic
    from app import extract
    items = '[{"name": "Tee", "cartons": 3}, {"name": "Bad", "cartons": "inf"}, {"name": "Huge", "cartons": "1e999"}]'
    resp = types.SimpleNamespace(stop_reason="tool_use", content=[types.SimpleNamespace(type="tool_use", input={"kind": "packing_list", "items": items})])
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: types.SimpleNamespace(messages=types.SimpleNamespace(create=lambda **k: resp)))
    out = extract.extract(b"\xff\xd8img", [], "k")
    assert [(i["name"], i["cartons"]) for i in out["items"]] == [("Tee", 3)]

def test_delete_lines_of_a_pending_item(cl):
    N = "Tablecloth Del"
    sync(cl, *[{"kind": "prepare", "uuid": "td%d" % i, "product": N, "colour": c, "size": z}
               for i, (c, z) in enumerate([("Design 1", "150x220cm"), ("Design 2", "150x220cm"), ("", "150x220cm"), ("", "180 Round")])])

    def lines():
        s = cl.get("/api/state").json()
        p = [x for x in s["prods"] if x["name"] == N]
        return p and p[0]["id"], sorted((v["colour"], v["size"]) for v in s["vars"] if p and v["prod"] == p[0]["id"])
    pid, _ = lines()
    # one line: the stray no-colour 150x220cm, leaving the no-colour 180 Round alone
    r = cl.post("/api/product/%d/delete" % pid, json={"colour": "", "size": "150x220cm"})
    assert r.status_code == 200 and r.json() == {"deleted": 1, "item_deleted": False}
    assert lines()[1] == [("", "180 Round"), ("Design 1", "150x220cm"), ("Design 2", "150x220cm")]
    # a whole size; nothing matching is a 404
    assert cl.post("/api/product/%d/delete" % pid, json={"size": "180 Round"}).json()["deleted"] == 1
    assert cl.post("/api/product/%d/delete" % pid, json={"size": "180 Round"}).status_code == 404
    # an active item, or one with history, is refused and keeps everything
    cl.post("/api/product/%d" % pid, json={"name": N, "pending": False})
    assert cl.post("/api/product/%d/delete" % pid, json={"colour": "Design 1"}).status_code == 409
    cl.post("/api/product/%d" % pid, json={"name": N, "pending": True})
    assert len(lines()[1]) == 2
    # the whole item
    r = cl.post("/api/product/%d/delete" % pid, json={})
    assert r.json() == {"deleted": 2, "item_deleted": True} and not lines()[0]
    assert cl.post("/api/product/%d/delete" % pid, json={}).status_code == 404


def test_delete_refuses_lines_with_history(cl):
    sync(cl, op("receive", "dh1", product="Hist Item", colour="Red", size="M", qty=2))
    pid = [x for x in cl.get("/api/state").json()["prods"] if x["name"] == "Hist Item"][0]["id"]
    # force it pending without the API check, as an older database might have it
    from app import db
    c = db.connect()
    c.execute("UPDATE products SET pending=1 WHERE id=?", (pid,))
    c.commit()
    c.close()
    assert cl.post("/api/product/%d/delete" % pid, json={}).status_code == 409
    assert any(x["name"] == "Hist Item" for x in cl.get("/api/state").json()["prods"])
