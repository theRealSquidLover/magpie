"""The phone's pilesOf() (index.html) must place stock in sections exactly like the server's piles() (app/main.py).
Runs the ledger audit scenarios (audit 2026-11-27: oversell then receive then move, a backdated receive, a transfer of
older stock into a line with a debt, reversing a receive after its cartons were moved, a reversed oversell) and compares,
for every stock line, the server's piles with what the phone computes from the same snapshot. Then the same kind of
sequence is queued on the phone while offline, and what the phone showed must match what the server has after the sync.
Every check asserts; exits non-zero on failure. Run from the repo root: python3 tests/e2e_piles.py"""
import os, tempfile, subprocess, time, sys, requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("E2E_PORT", "18994"))
U = "http://127.0.0.1:%d" % PORT
FAILS = []
D = tempfile.mkdtemp()
os.environ["DATA_DIR"] = D  # the server's own piles() is read from the same database, in this process
sys.path.insert(0, ROOT)


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  -> " + str(detail)))
    if not ok:
        FAILS.append(name)


def boot():
    env = dict(os.environ, DATA_DIR=D, TZ="Europe/Athens", APP_VERSION="v1")
    try:
        requests.get(U + "/api/health", timeout=1)
        sys.exit("port %d is already in use; set E2E_PORT" % PORT)
    except requests.RequestException:
        pass
    pr = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
    for _ in range(300):
        try:
            requests.get(U + "/api/health", timeout=1)
            return pr
        except requests.RequestException:
            time.sleep(.1)
    pr.terminate()
    sys.exit("server did not start")


def st():
    return requests.get(U + "/api/state").json()


def op(kind, uid, **kw):
    base = {"kind": kind, "uuid": uid, "product": "Tee", "colour": "", "size": "", "lot": "", "loc": 1, "qty": 5}
    base.update(kw)
    return base


def sync(*ops):
    r = requests.post(U + "/api/sync", json={"ops": list(ops)}).json()["results"]
    return r


def wh(name, *secs):
    requests.post(U + "/api/location", json={"name": name})
    lid = [l for l in st()["locs"] if l["name"] == name][0]["id"]
    r = requests.post(U + "/api/location/%d/sections" % lid, json={"on": True, "sections": [{"name": n} for n in secs]})
    assert r.status_code == 200, r.text
    ids = {s["name"]: s["id"] for l in st()["locs"] if l["id"] == lid for s in l["secs"]}
    return lid, ids


def server_piles():
    """{"v|lot|loc": {"s|arrived": cartons}} from the server's own piles(), for every line in the ledger"""
    from app import db, main
    c = db.connect()
    try:
        out = {}
        for v, lot, loc in c.execute("SELECT DISTINCT variant_id, lot, location_id FROM movements").fetchall():
            out["%s|%s|%s" % (v, lot, loc)] = {"%s|%s" % k: n for k, n in main.piles(c, v, lot, loc).items()}
        return out
    finally:
        c.close()


CLIENT = """() => {const o={};new Set(S.mv.map(m=>m.v+'|'+m.lot+'|'+m.loc)).forEach(k=>{const [v,lot,loc]=[k.slice(0,k.indexOf('|')),k.slice(k.indexOf('|')+1,k.lastIndexOf('|')),k.slice(k.lastIndexOf('|')+1)];
  const r={};pilesOf(+v,lot,+loc).forEach(x=>{r[x.s+'|'+x.a]=x.q});o[k]=r});return o}"""


def names():
    s = st()
    pn = {p["id"]: p["name"] for p in s["prods"]}
    vn = {v["id"]: pn[v["prod"]] for v in s["vars"]}
    ln = {l["id"]: l["name"] for l in s["locs"]}
    return lambda k: "%s @ %s" % (vn.get(int(k.split("|")[0]), k), ln.get(int(k.rsplit("|", 1)[1]), k))


pr = boot()
try:
    # ---- the ledger audit scenarios, written on the server ----
    lid, s = wh("F1Wh", "A", "C")
    A, C = s["A"], s["C"]
    sync(op("receive", "f1-r0", product="F1", qty=0, loc=lid))
    sync(op("sale", "f1-s", product="F1", qty=2, loc=lid, ts="2026-11-01T09:00:00Z"))
    sync(op("receive", "f1-r", product="F1", qty=3, loc=lid, section=A, ts="2026-11-01T10:00:00Z"))
    sync(op("move", "f1-m", product="F1", qty=1, loc=lid, from_section=A, section=C))

    lid, s = wh("F2Wh", "A", "B", "C")
    A, B, C = s["A"], s["B"], s["C"]
    sync(op("receive", "f2-r0", product="F2", qty=0, loc=lid))
    sync(op("sale", "f2-s", product="F2", qty=2, loc=lid, ts="2026-11-01T08:00:00Z"))
    sync(op("receive", "f2-ra", product="F2", qty=3, loc=lid, section=A, ts="2026-11-01T10:00:00Z"))
    sync(op("move", "f2-m", product="F2", qty=1, loc=lid, from_section=A, section=C))
    sync(op("receive", "f2-rb", product="F2", qty=5, loc=lid, section=B, ts="2026-11-01T09:00:00Z"))

    sync(op("receive", "f3-r1", product="F3", colour="Navy", lot="LOT1", qty=5),
         op("receive", "f3-r2", product="F3", colour="Dark blue", lot="lot1", qty=3))

    lid, s = wh("F4Wh", "A", "B", "C")
    A, B, C = s["A"], s["B"], s["C"]
    sync(op("receive", "f4-c", product="F4", qty=5, loc=lid, section=C, ts="2026-10-01T00:00:00Z"))
    sync(op("receive", "f4-a", product="F4", qty=5, loc=lid, section=A, ts="2026-11-01T00:00:00Z"))
    sync(op("move", "f4-m", product="F4", qty=5, loc=lid, from_section=A, section=B))
    sync({"kind": "reverse", "uuid": "f4-x", "of": "f4-a", "note": "received twice"})

    lid, s = wh("F2bWh", "A", "C", "H")
    A, C, H = s["A"], s["C"], s["H"]
    sync(op("receive", "f2b-w1", product="F2b", qty=5, loc=1, ts="2025-06-01T00:00:00Z"))
    sync(op("receive", "f2b-r0", product="F2b", qty=0, loc=lid))
    sync(op("sale", "f2b-s", product="F2b", qty=2, loc=lid))
    sync(op("receive", "f2b-ra", product="F2b", qty=3, loc=lid, section=A))
    sync(op("move", "f2b-m1", product="F2b", qty=1, loc=lid, from_section=A, section=C))
    sync(op("move", "f2b-m2", product="F2b", qty=1, loc=lid, from_section=A, section=C))
    sync(op("transfer", "f2b-t", product="F2b", qty=5, loc=1, to=lid))

    # an oversell paid by a delivery, then the oversell reversed: the cartons go back where they were
    lid, s = wh("RvWh", "A", "B")
    A, B = s["A"], s["B"]
    sync(op("receive", "rv-r0", product="Rv", qty=0, loc=lid))
    sync(op("sale", "rv-s", product="Rv", qty=3, loc=lid))
    sync(op("receive", "rv-a", product="Rv", qty=2, loc=lid, section=A))
    sync(op("receive", "rv-b", product="Rv", qty=4, loc=lid, section=B))
    sync({"kind": "reverse", "uuid": "rv-x", "of": "rv-s"})
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(**p.devices["iPhone 13"])
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(U + "/")
        pg.wait_for_timeout(1500)
        nm = names()
        sv, cl = server_piles(), pg.evaluate(CLIENT)
        check("the phone sees every line the server has", set(sv) == set(cl), set(sv) ^ set(cl))
        for k in sorted(sv):
            check("same sections: " + nm(k), sv[k] == cl.get(k), (sv[k], cl.get(k)))

        # ---- queued on the phone while offline: what it shows before the sync = what the server has after ----
        lid, s = wh("OffWh", "A", "C")
        A, C = s["A"], s["C"]
        sync(op("receive", "of-r0", product="Off", qty=0, loc=lid))
        pg.evaluate("sync()")
        pg.wait_for_timeout(1000)
        ctx.set_offline(True)
        q = lambda o: pg.evaluate("queue(%s)" % o)
        q("{kind:'sale',product:'Off',colour:'',size:'',lot:'',loc:%d,qty:2}" % lid)
        pg.wait_for_timeout(20)
        q("{kind:'receive',product:'Off',colour:'',size:'',lot:'',loc:%d,section:%d,qty:3}" % (lid, A))
        pg.wait_for_timeout(20)
        q("{kind:'move',product:'Off',colour:'',size:'',lot:'',loc:%d,from_section:%d,section:%d,qty:1}" % (lid, A, C))
        pg.wait_for_timeout(20)
        q("{kind:'receive',product:'Off',colour:'',size:'',lot:'',loc:%d,section:%d,qty:4}" % (lid, C))
        pg.wait_for_timeout(20)
        q("{kind:'sale',product:'Off',colour:'',size:'',lot:'',loc:%d,qty:1}" % lid)
        vid = pg.evaluate("S.vars.find(v=>v.prod==S.prods.find(p=>p.name=='Off').id).id")
        key = "%d||%d" % (vid, lid)
        before = pg.evaluate(CLIENT)[key]
        sec_before = pg.evaluate("secTot(%d,'',%d)" % (vid, lid))
        ctx.set_offline(False)
        pg.evaluate("sync()")
        pg.wait_for_function("PEND.length==0&&!SY", timeout=30000)
        after = server_piles()[key]
        sec_srv = {}
        for k, n in after.items():
            sec_srv[k.split("|")[0]] = sec_srv.get(k.split("|")[0], 0) + n
        check("offline: the sections the phone showed are what the server has after the sync",
              {k: v for k, v in sec_before.items() if v} == {k: v for k, v in sec_srv.items() if v}, (sec_before, sec_srv))
        check("after the sync: phone and server agree", pg.evaluate(CLIENT)[key] == after, (pg.evaluate(CLIENT)[key], after))
        b.close()
        check("no page errors", not errs, errs[:3])
finally:
    pr.terminate()

print("\n%d failed" % len(FAILS) if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
