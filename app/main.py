"""Stock app API. The ledger (movements) is append-only; stock is always derived."""
import asyncio
import base64
import datetime
import io
import ipaddress
import os
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import creds, db, tg

REQUIRE_TS = os.environ.get("REQUIRE_TAILSCALE_HEADER", "0") == "1"
# ALLOWED_LOGINS: comma list of Tailscale logins allowed in (e.g. "yourname@github"). Empty = anyone on the tailnet.
# Requests without a Tailscale login are only let in from the local network (the server's LAN / the host itself).
ALLOWED_LOGINS = {x.strip().lower() for x in os.environ.get("ALLOWED_LOGINS", "").split(",") if x.strip()}
# Host names the app answers to, besides IP addresses, *.ts.net, *.local and single-word names (blocks DNS rebinding).
ALLOWED_HOSTS = {x.strip().lower() for x in os.environ.get("ALLOWED_HOSTS", "").split(",") if x.strip()}
PHOTOS = os.path.join(db.DATA, "photos")
MAPS = os.path.join(db.DATA, "maps")
KINDS = {"receive", "sale", "adjust", "transfer", "move"}
IMAGE_MAGIC = (b"\xff\xd8", b"\x89PNG", b"GIF8", b"RIFF")  # first bytes of JPEG, PNG, GIF, WebP
VERSION = os.environ.get("APP_VERSION", "dev")  # git commit of the image, stamped at build time


async def _daily_backup():
    try:
        await asyncio.to_thread(db.scrub_old_backups)  # copies made by older versions still hold the key and token
    except Exception:
        pass
    while True:  # checked every hour; makes today's copy once (backup_daily records success or the error)
        try:
            await asyncio.to_thread(db.backup_daily)
        except Exception:
            pass
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app):
    os.umask(0o077)  # every file the app writes (database, backups, photos, maps) is readable by its own user only
    db.migrate()
    os.makedirs(PHOTOS, exist_ok=True)
    db.lock_down()  # and the same for files written before this version
    task = asyncio.create_task(_daily_backup())
    tg.start()
    yield
    tg.stop()
    task.cancel()


app = FastAPI(title="Magpie", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# Tailscale's own addresses are "private" to Python but are other devices on the tailnet, not the local network.
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def _local_ip(h):
    try:
        a = ipaddress.ip_address(h or "")
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:  # ::ffff:10.0.0.5 is the IPv4 address 10.0.0.5
        a = a.ipv4_mapped
    if a.is_unspecified or any(a in n for n in TAILNET if n.version == a.version):
        return False
    return a.is_private or a.is_loopback or a.is_link_local


def _host_ok(host):
    h = (host or "").rsplit(":", 1)[0].strip("[]").lower() if not (host or "").startswith("[") else host.split("]")[0][1:].lower()
    if not h or h in ALLOWED_HOSTS or "." not in h or h.endswith(".ts.net") or h.endswith(".local") or h == "localhost":
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


# The app's own artwork and manifest, identical to the public repo. iOS fetches the home-screen icon in a separate request that
# may come without the Tailscale identity, and Unraid fetches the container icon from the server itself; a refused
# icon makes iOS draw a plain letter instead. These hold no data, so they skip the login checks (not the host check).
ICONS = {"/icon-180.png", "/icon-192.png", "/icon-512.png", "/icon-maskable-512.png", "/favicon.ico", "/favicon-32.png",
         "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/manifest.webmanifest"}


@app.middleware("http")
async def gate(request: Request, call_next):
    if request.url.path in ICONS:
        if not _host_ok(request.headers.get("host")):
            return JSONResponse({"detail": "Unknown host name"}, status_code=403)
    elif request.url.path != "/api/health":
        if not _host_ok(request.headers.get("host")):  # a page on some other domain pointed at this server (DNS rebinding)
            return JSONResponse({"detail": "Unknown host name"}, status_code=403)
        login = request.headers.get("tailscale-user-login", "").strip().lower()
        if REQUIRE_TS and not login:
            return JSONResponse({"detail": "Tailscale identity required"}, status_code=403)
        if ALLOWED_LOGINS:
            if login and login not in ALLOWED_LOGINS:
                return JSONResponse({"detail": "This Tailscale login isn't allowed"}, status_code=403)
            if not login and not _local_ip(request.client.host if request.client else ""):
                return JSONResponse({"detail": "Only from the local network or an allowed Tailscale login"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        # Writes only from the app's own pages: another website opened on a staff phone could otherwise post here
        # through the phone's tailnet connection. Two independent checks:
        # 1. Browsers mark cross-site requests (Sec-Fetch-Site); those are refused.
        # 2. Every write to /api/ must say Content-Type: application/json, except the two picture uploads. A page on
        #    another site can only send JSON-typed bodies after a CORS preflight, which this server never grants
        #    (text/plain, form posts and multipart/form-data need no preflight, so they are refused here).
        #    A DELETE without a body is allowed: DELETE itself always needs a preflight.
        site = request.headers.get("sec-fetch-site", "")
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        path = request.url.path
        upload = request.method == "POST" and (path == "/api/extract" or re.fullmatch(r"/api/location/[^/]+/map", path))
        allowed = ("multipart/form-data",) if upload else ("application/json",) if path.startswith("/api/") else ("application/json", "multipart/form-data")
        if site in ("cross-site", "same-site") or (ctype not in allowed and not (ctype == "" and request.method == "DELETE")):
            return JSONResponse({"detail": "Request refused (not from this app)"}, status_code=403)
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")  # never guess a file's type (a photo can't run as a script)
    resp.headers.setdefault("X-Frame-Options", "DENY")  # the app can't be framed by another page (click-jacking)
    # No script rules (the page's code is inline): only what can't break the app. No framing, no <base>/<object>
    # tricks, and forms can only post back here.
    resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")  # links out never tell the other site this server's address
    # other sites can't load photos, maps or PDFs from here; the icons may be loaded from anywhere (iOS, Unraid)
    resp.headers.setdefault("Cross-Origin-Resource-Policy", "cross-origin" if request.url.path in ICONS else "same-origin")
    return resp


def who(request: Request):
    return request.headers.get("tailscale-user-login", "") or "local"


def nat(s):
    # split on ASCII digits only: str.isdigit() is also true for "²", which int() refuses
    return [int(t) if i % 2 else t.lower() for i, t in enumerate(re.split(r"([0-9]+)", s or ""))]


def parse_pack(text):
    """What's in one carton, as typed: '24 doz', '24x6 rolls', '24 by 6', '60', '60 pcs'.
    Returns (text to show, pieces). Pieces is 0 when it can't be worked out; the text is still kept."""
    t = " ".join(str(text or "").split())[:40]
    if not t:
        return "", 0
    if any(len(n) > 7 for n in re.findall(r"\d+", t)):
        return t, 0  # not a real carton count; keep the text
    if re.fullmatch(r"\d+", t):  # a bare number is pieces, shown as such; 0 clears
        return (t + " pcs", int(t)) if int(t) else ("", 0)
    lo = t.lower()
    m = re.fullmatch(r"(\d+)\s*(?:x|×|\*|by)\s*(\d+)\s*(doz|dozen|dozens|dz)\.?(?:\s*[a-z]+)?", lo)
    if m:
        return t, int(m.group(1)) * int(m.group(2)) * 12
    m = re.fullmatch(r"(\d+)\s*(?:x|×|\*|by)\s*(\d+)(?:\s*[a-z]+)?", lo)
    if m:
        return t, int(m.group(1)) * int(m.group(2))
    m = re.fullmatch(r"(\d+)\s*(doz|dozen|dozens|dz)\.?(?:\s*[a-z]+)?", lo)
    if m:
        return t, int(m.group(1)) * 12
    m = re.fullmatch(r"(\d+)\s*(?:pcs|pc|pieces|piece)\.?", lo)
    if m:
        return t, int(m.group(1))
    return t, 0


def _pack_of(o):
    """Pack text from an op/body: new clients send 'pack' (text), older ones 'ppc' (a number of pieces)."""
    t = _text(o.get("pack"), "pack")
    if t.strip():
        return parse_pack(t)
    v = _text(o.get("ppc"), "ppc")
    try:
        n = int(float(v or 0))
    except (TypeError, ValueError, OverflowError):
        return parse_pack(v)
    return (parse_pack(str(n)) if n > 0 else ("", 0))


@app.get("/api/health")
def health():
    return {"ok": True, "version": VERSION, "backup": db.backup_status()}


@app.get("/api/state")
def state(request: Request):
    c = db.connect()
    c.execute("BEGIN")  # one read transaction: everything below comes from the same moment
    secs = {}
    for r in c.execute("SELECT * FROM sections ORDER BY location_id, name"):
        secs.setdefault(r["location_id"], []).append({"id": r["id"], "name": r["name"], "x": r["x"], "y": r["y"], "w": r["w"], "h": r["h"]})
    locs = [{"id": r["id"], "name": r["name"], "on": bool(r["active"]), "role": r["role"], "hs": bool(r["has_sections"]),
             "map": "/maps/" + r["map"] if r["map"] else None, "secs": secs.get(r["id"], [])}
            for r in c.execute("SELECT * FROM locations ORDER BY id")]
    homes = [[r[0], r[1], r[2]] for r in c.execute("SELECT product_id, location_id, section_id FROM homes")]
    cph = [[r[0], r[1], "/photos/" + r[2]] for r in c.execute("SELECT product_id, colour, photo FROM colour_photos")]
    prods = [{"id": r["id"], "name": r["name"], "alias": r["alias"], "ppc": r["ppc"], "pack": r["pack"],
              "photo": "/photos/" + r["photo"] if r["photo"] else None, "pickloc": r["pick_loc"], "grp": r["group_by"], "cat": r["category"], "cp": bool(r["colour_photos"]), "pend": bool(r["pending"])}
             for r in c.execute("SELECT * FROM products ORDER BY id")]
    vars_ = [{"id": r["id"], "prod": r["product_id"], "colour": r["colour"], "size": r["size"], "reorder": r["reorder_at"], "pickmin": r["pick_min"], "pickmax": r["pick_max"], "ppc": r["ppc"], "pack": r["pack"], "disc": r["discontinued"]}
             for r in c.execute("SELECT * FROM variants ORDER BY id")]
    mv = [{"v": r["variant_id"], "lot": r["lot"], "loc": r["location_id"], "qty": r["qty"], "type": r["type"],
           "ts": r["ts"], "note": r["note"], "tid": r["transfer_id"], "user": r["user"], "flag": r["flag"], "s": r["section_id"], "a": r["arrived"],
           "op": _op_of(r), "rv": r["reverses"]}
          for r in c.execute("SELECT * FROM movements ORDER BY id")]
    c.execute("COMMIT")
    c.close()
    return {"locs": locs, "prods": prods, "vars": vars_, "mv": mv, "homes": homes, "cph": cph, "user": who(request), "build": VERSION}


# ---------- sync (idempotent, one transaction per operation) ----------

def piles(c, vid, lot, loc):
    """What's on the shelf for one stock line, as {(section_id, arrived): cartons}.
    Every carton keeps the date it was first received; stock going out is taken oldest-first.
    Rows from before sections existed carry no arrival date: incoming ones are dated by their own time,
    outgoing ones are worked out oldest-first here (the same rule new rows are written with).
    A row whose section no longer exists in this warehouse counts as Unsorted (0), the same as the app shows it, so stock
    left on a deleted section by an older version can still be moved or sold from Unsorted. (Section ids are never reused
    since migration 17, so such a row can't turn up in a new section later.)"""
    p = {}
    live = {r[0] for r in c.execute("SELECT id FROM sections WHERE location_id=?", (loc,))}

    def take(n, ts):
        for k in sorted((k for k, v in p.items() if v > 0), key=lambda k: (k[1], p[k], k[0])):
            t = min(n, p[k])
            p[k] -= t
            n -= t
            if not n:
                return
        p[(0, ts)] = p.get((0, ts), 0) - n

    for r in c.execute("SELECT qty, section_id, arrived, ts FROM movements WHERE variant_id=? AND lot=? AND location_id=? ORDER BY ts, id",
                       (vid, lot, loc)):
        if r["qty"] >= 0 or r["arrived"]:
            k = (r["section_id"] if r["section_id"] in live else 0, r["arrived"] or r["ts"])
            p[k] = p.get(k, 0) + r["qty"]
        else:
            take(-r["qty"], r["ts"])
    for k in [k for k, v in p.items() if v < 0]:  # stock that went below zero is settled against whatever is there
        n = -p.pop(k)
        take(n, k[1])
    return {k: v for k, v in p.items() if v}


def _oldest_first(c, vid, lot, loc, n, ts):
    """Split taking n cartons into [(section, arrived, cartons)], oldest first. More than the system holds goes into Unsorted (and shows below zero)."""
    p = piles(c, vid, lot, loc)
    out = []
    for k in sorted((k for k, v in p.items() if v > 0), key=lambda k: (k[1], p[k], k[0])):
        t = min(n, p[k])
        out.append((k[0], k[1], t))
        n -= t
        if not n:
            break
    if n:
        out.append((0, ts, n))
    return out


def _home(c, pid, loc):
    r = c.execute("SELECT h.section_id FROM homes h JOIN locations l ON l.id=h.location_id WHERE h.product_id=? AND h.location_id=? AND l.has_sections=1",
                  (pid, loc)).fetchone()
    return r[0] if r else 0


MAX_QTY = 100000
MAX_ID = 2 ** 53  # ids and numbers beyond this can't be stored (sqlite would fail) and a phone can't even hold them exactly


def _text(v, what):
    """A text field: missing/null = ''. Numbers are taken as their digits (a size of 8); a list, object or true/false is refused,
    never saved as its Python spelling."""
    if v is None:
        return ""
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise ValueError("%s must be text" % what)
    return str(v)


def _whole(v, what, lo=0, hi=MAX_ID):
    """A whole number given as a JSON number or digits ('3'); true/false, fractions and anything out of range are refused."""
    if isinstance(v, bool):
        raise ValueError("%s must be a whole number" % what)
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, str) and re.fullmatch(r"\s*-?[0-9]{1,16}\s*", v):
        v = int(v)
    if not isinstance(v, int):
        raise ValueError("%s must be a whole number" % what)
    if not lo <= v <= hi:
        raise ValueError("%s is out of range" % what)
    return v


def _flag_of(v, what):
    """A yes/no field: true/false (or 1/0); left out = no."""
    if v is None or isinstance(v, bool):
        return bool(v)
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    raise ValueError("%s must be true or false" % what)


def _h422(fn, *a, **k):
    """The helpers above, for the online edit endpoints: a bad field is a 422 with a readable message."""
    try:
        return fn(*a, **k)
    except ValueError as e:
        raise HTTPException(422, str(e))


async def _body(request):
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(422, "Body must be JSON")
    if not isinstance(b, dict):
        raise HTTPException(422, "Body must be a JSON object")
    return b


def _limits(o):
    for k, n in (("product", 120), ("alias", 120), ("colour", 80), ("size", 80), ("lot", 80), ("note", 500), ("pack", 40)):
        if len(_text(o.get(k), k)) > n:
            raise ValueError("%s is too long (max %d characters)" % (k, n))


def _ts(v):
    """Every timestamp stored the same way (UTC, milliseconds, Z) so time order is plain text order."""
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        d = datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else now
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        d = now
    return d.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)


def _rollback(c):
    """Undo the open transaction, if sqlite hasn't already (it rolls back by itself after some disk errors); the original
    error is what gets reported, never a 'no transaction is active' from here."""
    if c.in_transaction:
        try:
            c.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def _flag_negative(c, mid, vid, lot, loc):
    q = c.execute("SELECT COALESCE(SUM(qty),0) FROM movements WHERE variant_id=? AND lot=? AND location_id=?",
                  (vid, lot, loc)).fetchone()[0]
    if q < 0:  # accepted (the physical event happened) but flagged for review
        c.execute("UPDATE movements SET flag='negative' WHERE id=?", (mid,))


def _opt_uuid(o):
    """photo and prepare: the id is optional (older clients), but when given it is checked like any other."""
    return _uuid(o) if o.get("uuid") not in (None, "") else ""


def _photo_op(c, name, o):
    uid = _opt_uuid(o)
    if uid and c.execute("SELECT 1 FROM op_log WHERE uuid=?", (uid,)).fetchone():
        return {"status": "dup"}  # a resend after a lost answer: already saved
    r = _photo_save(c, name, o)
    if uid:
        c.execute("INSERT OR IGNORE INTO op_log(uuid) VALUES(?)", (uid,))
    return r


def _photo_save(c, name, o):
    p = c.execute("SELECT id, photo FROM products WHERE name=?", (name,)).fetchone()
    if not p:
        raise ValueError("unknown item for photo: " + name)
    data = _text(o.get("data"), "photo data")
    if "," in data:
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data, validate=True)
    except ValueError:
        raise ValueError("photo data is damaged (not base64)")
    if len(raw) > 8_000_000:
        raise ValueError("photo too large")
    if not raw.startswith(IMAGE_MAGIC):
        # a phone that ran out of memory hands over an empty picture ("data:,"): refuse it rather than save a blank file
        raise ValueError("photo is empty or not a picture")
    col = _text(o.get("colour"), "colour").strip()
    if col:  # one photo per colour (printed designs); every size of that colour shares it
        if not c.execute("SELECT 1 FROM variants WHERE product_id=? AND colour=?", (p["id"], col)).fetchone():
            raise ValueError("no colour called %s on this item" % col)
        fn = "c%d-%d-%s.jpg" % (p["id"], int(time.time() * 1000), uuid.uuid4().hex[:6])
        old = c.execute("SELECT photo FROM colour_photos WHERE product_id=? AND colour=?", (p["id"], col)).fetchone()
        with open(os.path.join(PHOTOS, fn), "wb") as f:
            f.write(raw)
        c.execute("INSERT OR REPLACE INTO colour_photos(product_id,colour,photo) VALUES(?,?,?)", (p["id"], col, fn))
        if old:
            try:
                os.remove(os.path.join(PHOTOS, old[0]))
            except OSError:
                pass
        return {"status": "ok"}
    fn = "%d-%d-%s.jpg" % (p["id"], int(time.time() * 1000), uuid.uuid4().hex[:6])
    with open(os.path.join(PHOTOS, fn), "wb") as f:
        f.write(raw)
    c.execute("UPDATE products SET photo=? WHERE id=?", (fn, p["id"]))
    if p["photo"]:
        try:
            os.remove(os.path.join(PHOTOS, p["photo"]))
        except OSError:
            pass
    return {"status": "ok"}


def _prepare_op(c, name, o):
    """Set an item (and a colour/size of it) up without any stock. A new item stays pending, off the stock list, alerts
    and bot, until it is first received. A resend after a lost answer is recognised by its uuid (op_log) and not run again:
    re-running it would put back the alias/carton contents it set, undoing any edit made since."""
    if not name:
        raise ValueError("item name missing")
    uid = _opt_uuid(o)
    _limits(o)
    col, size = _text(o.get("colour"), "colour").strip(), _text(o.get("size"), "size").strip()
    alias = _text(o.get("alias"), "alias").strip()
    pk, pn = _pack_of(o)
    c.execute("BEGIN IMMEDIATE")
    try:
        if uid and c.execute("SELECT 1 FROM op_log WHERE uuid=?", (uid,)).fetchone():
            c.execute("ROLLBACK")
            return {"status": "dup"}
        if uid:
            c.execute("INSERT INTO op_log(uuid) VALUES(?)", (uid,))
        p = c.execute("SELECT id FROM products WHERE name=?", (name,)).fetchone()
        pid = p["id"] if p else c.execute("INSERT INTO products(name,alias,ppc,pack,pending) VALUES(?,?,?,?,1)", (name, alias, pn, pk)).lastrowid
        if p and alias:
            c.execute("UPDATE products SET alias=? WHERE id=?", (alias, pid))
        if p and pk:
            c.execute("UPDATE products SET ppc=?, pack=? WHERE id=?", (pn, pk, pid))
        v = c.execute("SELECT id FROM variants WHERE product_id=? AND colour=? AND size=?", (pid, col, size)).fetchone()
        vid = v["id"] if v else c.execute("INSERT INTO variants(product_id,colour,size) VALUES(?,?,?)", (pid, col, size)).lastrowid
        if pk:
            c.execute("UPDATE variants SET ppc=?, pack=? WHERE id=?", (pn, pk, vid))
        c.execute("COMMIT")
    except Exception:
        _rollback(c)
        raise
    return {"status": "ok"}


def _uuid(o):
    """The op's own id: text, without ':' (extra rows of one op are stored as id:2, id:b, ...)."""
    if not isinstance(o.get("uuid"), str) or not o["uuid"].strip():
        raise ValueError("missing operation id")
    u = o["uuid"]
    if ":" in u or len(u) > 80:
        raise ValueError("bad operation id")
    return u


def _op_of(r):
    """The operation a ledger row belongs to. One operation can write several rows (a sale split across sections is
    uuid, uuid:2, ...; a transfer's second leg is uuid:b; a move's rows share transfer_id), and all of them share this id."""
    return r["transfer_id"] or r["uuid"].split(":")[0]


def _reverse_op(c, o, user):
    """Undo a mistaken operation with counter-entries: every row it wrote gets an opposite row (same item, lot, warehouse,
    section and arrival date, same type, so reports net it out), linked back by `reverses`. Nothing is edited or deleted.
    An operation can be reversed once; a reversal can't be reversed; a reversal that would take a stock line below zero
    is refused (that stock has already gone, so the record needs a recount, not a reversal)."""
    uid = _uuid(o)
    of = _text(o.get("of"), "of").strip()
    if not of:
        raise ValueError("which movement to reverse is missing")
    if of == uid:
        raise ValueError("bad operation id (a reversal needs its own id)")
    _limits(o)
    note = _text(o.get("note"), "note").strip()
    ts = _ts(o.get("ts"))
    c.execute("BEGIN IMMEDIATE")
    try:
        if c.execute("SELECT 1 FROM movements WHERE uuid=?", (uid,)).fetchone():
            c.execute("ROLLBACK")
            return {"status": "dup"}
        rows = [r for r in c.execute("SELECT * FROM movements WHERE uuid=? OR substr(uuid,1,?)=? OR transfer_id=? ORDER BY id",
                                     (of, len(of) + 1, of + ":", of)).fetchall() if _op_of(r) == of]
        if not rows:
            raise ValueError("that movement isn't on the server (yet)")
        if any(r["reverses"] for r in rows):
            raise ValueError("that is already a reversal - record a new movement instead")
        if c.execute("SELECT 1 FROM movements WHERE reverses=?", (of,)).fetchone():
            raise ValueError("that movement has already been reversed")
        rows = [r for r in rows if r["qty"]]
        if not rows:
            raise ValueError("nothing to reverse: that movement changed no stock")
        delta = {}
        for r in rows:
            k = (r["variant_id"], r["lot"], r["location_id"])
            delta[k] = delta.get(k, 0) - r["qty"]
        for (vid, lot, loc), d in delta.items():
            if d >= 0:
                continue
            have = c.execute("SELECT COALESCE(SUM(qty),0) FROM movements WHERE variant_id=? AND lot=? AND location_id=?",
                             (vid, lot, loc)).fetchone()[0]
            if have + d < 0:
                ln = c.execute("SELECT name FROM locations WHERE id=?", (loc,)).fetchone()[0]
                raise ValueError("can't reverse: only %d cartons left in %s and reversing takes %d. "
                                 "The stock has moved on since, so recount it instead" % (have, ln, -d))
        net = {}  # stock coming back into a warehouse that has been archived since: refused, like a receive would be
        for (vid, lot, loc), d in delta.items():
            net[loc] = net.get(loc, 0) + d
        for loc, d in net.items():
            if d > 0 and not c.execute("SELECT active FROM locations WHERE id=?", (loc,)).fetchone()[0]:
                ln = c.execute("SELECT name FROM locations WHERE id=?", (loc,)).fetchone()[0]
                raise ValueError("can't reverse: %s is archived - restore it first" % ln)
        live = {}  # sections that still exist, per warehouse; a counter-entry for a deleted section goes to Unsorted

        def sec_of(r):
            if r["location_id"] not in live:
                live[r["location_id"]] = {x[0] for x in c.execute("SELECT id FROM sections WHERE location_id=?", (r["location_id"],))}
            return r["section_id"] if r["section_id"] in live[r["location_id"]] else 0

        # A move nets to zero on its stock line, so the check above never stops it. Undoing it takes the cartons back out
        # of the section they were moved into: they must still be there, or the record would put them in the wrong place.
        need = {}
        for r in rows:
            if r["type"] == "move" and r["qty"] > 0:
                k = (r["variant_id"], r["lot"], r["location_id"], sec_of(r))
                need[k] = need.get(k, 0) + r["qty"]
        for (vid, lot, loc, sec), n in need.items():
            have = sum(q for (s_, _), q in piles(c, vid, lot, loc).items() if s_ == sec and q > 0)
            if have < n:
                sn = (c.execute("SELECT name FROM sections WHERE id=?", (sec,)).fetchone() or ["Unsorted"])[0]
                raise ValueError("can't reverse: only %d cartons left in %s and reversing takes %d. "
                                 "The stock has moved on since, so move it with a new movement instead" % (have, sn, n))
        tid = uid if rows[0]["transfer_id"] else None
        for i, r in enumerate(rows):
            c.execute("INSERT INTO movements(uuid,ts,variant_id,lot,location_id,qty,type,note,transfer_id,user,section_id,arrived,reverses)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (uid if i == 0 else "%s:%d" % (uid, i + 1), ts, r["variant_id"], r["lot"], r["location_id"], -r["qty"],
                       r["type"], note, tid, user, sec_of(r), r["arrived"] or r["ts"], of))
        c.execute("COMMIT")
    except Exception:
        _rollback(c)
        raise
    return {"status": "ok"}


def apply_op(c, o, user):
    kind = o.get("kind")
    name = _text(o.get("product"), "product").strip()
    if kind == "reverse":
        return _reverse_op(c, o, user)
    if kind == "photo":
        return _photo_op(c, name, o)
    if kind == "prepare":
        return _prepare_op(c, name, o)
    if kind not in KINDS:
        raise ValueError("unknown operation")
    uid = _uuid(o)
    if c.execute("SELECT 1 FROM movements WHERE uuid=?", (uid,)).fetchone():
        return {"status": "dup"}
    q = o.get("qty")
    if isinstance(q, bool):
        raise ValueError("quantity must be a number")
    try:
        fq = float(q)
        qty = int(fq)  # whole cartons: a decimal is rounded down (towards zero)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("quantity must be a number")
    if abs(qty) > MAX_QTY:
        raise ValueError("quantity too large (over %d cartons) - check the number" % MAX_QTY)
    if qty == 0 and fq != 0:  # 0.9 typed by mistake: refused rather than silently recorded as nothing
        raise ValueError("quantity must be at least 1 carton (%s rounds down to 0)" % q)
    note = _text(o.get("note"), "note").strip()
    _limits(o)
    if kind == "adjust":
        if qty == 0:
            raise ValueError("adjustment cannot be zero")
        if not note:
            raise ValueError("adjustments need a note")
    elif qty < 0 or (qty == 0 and kind != "receive"):  # receiving 0 registers an item with no stock yet
        raise ValueError("quantity must be positive")
    if not name:
        raise ValueError("item name missing")
    col = _text(o.get("colour"), "colour").strip()
    size = _text(o.get("size"), "size").strip()
    lot = _text(o.get("lot"), "lot").strip()
    if o.get("loc") is None:
        raise ValueError("warehouse missing")
    loc = _whole(o["loc"], "warehouse", -MAX_ID)
    if kind == "transfer" and o.get("to") is None:
        raise ValueError("destination warehouse missing")
    to = _whole(o["to"], "destination warehouse", -MAX_ID) if kind == "transfer" else None
    if kind == "move" and qty == 0:
        raise ValueError("quantity must be positive")
    if kind == "transfer" and to == loc:
        raise ValueError("destination is the same warehouse")
    sfrom = _whole(o.get("from_section") or 0, "section") if kind == "move" else 0
    sto = _whole(o.get("section") or 0, "section") if kind in ("move", "receive") else 0
    home = _flag_of(o.get("home"), "home") if kind == "move" else False
    alias = _text(o.get("alias"), "alias").strip() if kind == "receive" else ""
    ts = _ts(o.get("ts"))
    c.execute("BEGIN IMMEDIATE")
    try:
        for l in [loc] + ([to] if kind == "transfer" else []):
            r = c.execute("SELECT active FROM locations WHERE id=?", (l,)).fetchone()
            if not r:
                raise ValueError("unknown warehouse")
            if not r["active"] and (l == to or kind == "receive"):
                raise ValueError("that warehouse is archived - restore it first")
        pk, pn = _pack_of(o) if kind == "receive" else ("", 0)
        p = c.execute("SELECT id FROM products WHERE name=?", (name,)).fetchone()
        if not p:
            if kind != "receive":
                raise ValueError("unknown item: " + name)
            pid = c.execute("INSERT INTO products(name,alias,ppc,pack) VALUES(?,?,?,?)",
                            (name, alias, pn, pk)).lastrowid
        else:
            pid = p["id"]
            if kind == "receive":
                if alias:
                    c.execute("UPDATE products SET alias=? WHERE id=?", (alias, pid))
                if pk:
                    c.execute("UPDATE products SET ppc=?, pack=? WHERE id=?", (pn, pk, pid))
        v = c.execute("SELECT id FROM variants WHERE product_id=? AND colour=? AND size=?",
                      (pid, col, size)).fetchone()
        if not v:
            if kind != "receive":
                raise ValueError("unknown colour/size for " + name)
            vid = c.execute("INSERT INTO variants(product_id,colour,size) VALUES(?,?,?)", (pid, col, size)).lastrowid
        else:
            vid = v["id"]
            if lot:  # lots match whatever the case, like item, colour and size: 'lot1' joins 'LOT1' (first spelling kept)
                r = c.execute("SELECT lot FROM movements WHERE variant_id=? AND lot=? COLLATE NOCASE ORDER BY id LIMIT 1", (vid, lot)).fetchone()
                lot = r[0] if r else lot
        if pk:  # what's in a carton belongs to the size/colour it was typed for
            c.execute("UPDATE variants SET ppc=?, pack=? WHERE id=?", (pn, pk, vid))

        def ins(l, q, t, u, sec=0, arr=None):
            mid = c.execute(
                "INSERT INTO movements(uuid,ts,variant_id,lot,location_id,qty,type,note,transfer_id,user,section_id,arrived)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (u, ts, vid, lot, l, q, t, note, uid if kind == "transfer" else None, user, sec, arr)).lastrowid
            if q < 0:
                _flag_negative(c, mid, vid, lot, l)

        def out(t):  # stock leaving: split oldest-first across sections; returns the parts
            parts = _oldest_first(c, vid, lot, loc, qty if kind != "adjust" else -qty, ts)
            for i, (sec, arr, n) in enumerate(parts):
                ins(loc, -n, t, uid if i == 0 else "%s:%d" % (uid, i + 1), sec, arr)
            return parts

        # any movement (a receive, even of 0, a found-stock adjust, ...) makes a prepared item live: it now has history,
        # and pending items are hidden from the stock list, alerts and bot
        c.execute("UPDATE products SET pending=0 WHERE id=? AND pending=1", (pid,))
        if kind == "move":  # between sections of one warehouse: same cartons, same arrival dates, just a different spot
            for x in (sfrom, sto):
                if x and not c.execute("SELECT 1 FROM sections WHERE id=? AND location_id=?", (x, loc)).fetchone():
                    raise ValueError("that section isn't in this warehouse")
            if sfrom == sto:
                raise ValueError("it is already in that section")
            p = piles(c, vid, lot, loc)
            have = sorted((k for k, v in p.items() if v > 0 and k[0] == sfrom), key=lambda k: (k[1], p[k]))
            if sum(p[k] for k in have) < qty:
                raise ValueError("only %d cartons there to move" % sum(p[k] for k in have))
            n, parts = qty, []
            for k in have:
                t = min(n, p[k])
                parts.append((k[1], t))
                n -= t
                if not n:
                    break
            for i, (arr, t) in enumerate(parts):
                sfx = "" if i == 0 else ":%d" % (i + 1)
                c.execute("INSERT INTO movements(uuid,ts,variant_id,lot,location_id,qty,type,note,transfer_id,user,section_id,arrived) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (uid + sfx, ts, vid, lot, loc, -t, "move", note, uid, user, sfrom, arr))
                c.execute("INSERT INTO movements(uuid,ts,variant_id,lot,location_id,qty,type,note,transfer_id,user,section_id,arrived) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (uid + ":b" + sfx, ts, vid, lot, loc, t, "move", note, uid, user, sto, arr))
            if sto and home:
                c.execute("INSERT OR REPLACE INTO homes(product_id,location_id,section_id) VALUES(?,?,?)", (pid, loc, sto))
        elif kind == "receive":
            sec = sto
            if sec:
                if not c.execute("SELECT 1 FROM sections WHERE id=? AND location_id=?", (sec, loc)).fetchone():
                    raise ValueError("that section isn't in this warehouse")
                c.execute("INSERT OR IGNORE INTO homes(product_id,location_id,section_id) VALUES(?,?,?)", (pid, loc, sec))
            ins(loc, qty, "receive", uid, sec, ts)
        elif kind == "sale":
            out("sale")
        elif kind == "adjust" and qty < 0:
            out("adjust")
        elif kind == "adjust":  # found more than recorded: joins the oldest stock already there (it's an update, not an arrival)
            p = piles(c, vid, lot, loc)
            old = sorted((k for k, v in p.items() if v > 0), key=lambda k: (k[1], k[0]))
            sec, arr = old[0] if old else (_home(c, pid, loc), ts)
            ins(loc, qty, "adjust", uid, sec, arr)
        else:  # both legs commit together or not at all; each part keeps its arrival date
            dsec = _home(c, pid, to)
            for i, (sec, arr, n) in enumerate(out("transfer")):
                ins(to, n, "transfer", uid + ":b" + ("" if i == 0 else str(i + 1)), dsec, arr)
        target = to if kind == "transfer" else (loc if kind == "receive" else None)
        if target and c.execute("SELECT 1 FROM locations WHERE id=? AND role='picking'", (target,)).fetchone():
            # first time an item is put into a picking warehouse, that becomes where it is picked from
            c.execute("UPDATE products SET pick_loc=? WHERE id=? AND pick_loc IS NULL", (target, pid))
        c.execute("COMMIT")
    except Exception:
        _rollback(c)
        raise
    return {"status": "ok"}


def _temporary(e):
    """A database problem that is expected to pass (another writer holds the lock, the disk is full or briefly failing):
    the op itself is fine and should be sent again later, not shown as refused."""
    m = str(e).lower()
    return isinstance(e, sqlite3.OperationalError) and any(
        w in m for w in ("locked", "busy", "disk i/o", "disk is full", "database or disk is full", "unable to open"))


@app.post("/api/sync")
async def sync(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(422, "Body must be JSON")
    ops = body.get("ops") if isinstance(body, dict) else None
    if not isinstance(ops, list):
        raise HTTPException(422, "Expected {ops: [...]}")
    user = who(request)
    try:
        c = db.connect()
    except sqlite3.OperationalError as e:
        raise HTTPException(503, "The database is busy or unavailable, try again shortly (%s)" % e)
    out = []
    retry = ""
    for o in ops:
        if not isinstance(o, dict):
            out.append({"uuid": None, "status": "error", "msg": "not an operation"})
            continue
        if retry:  # keep the order: nothing after an op that must be retried is applied before it
            out.append({"uuid": o.get("uuid"), "status": "retry", "msg": "not applied yet: an earlier operation has to be sent again first"})
            continue
        try:
            out.append({"uuid": o.get("uuid"), **apply_op(c, o, user)})
        except Exception as e:  # one bad op never blocks the others
            if c.in_transaction:  # the op's own ROLLBACK can fail too (disk error); never leave it half open for the next op
                try:
                    c.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            if _temporary(e):
                retry = str(e)
                out.append({"uuid": o.get("uuid"), "status": "retry", "msg": "server busy, will be sent again: %s" % e})
            else:
                out.append({"uuid": o.get("uuid"), "status": "error", "msg": str(e)})
    c.close()
    return {"results": out}


# ---------- online-only edits ----------

def _id_ok(x, what="Not found"):
    """A path id that can't exist (0, negative, beyond what sqlite stores) is simply not found."""
    if not 0 < x <= MAX_ID:
        raise HTTPException(404, what)
    return x


def _t(b, k, what=None):
    return _h422(_text, b.get(k), what or k)


@app.post("/api/product/{pid}")
async def product_update(pid: int, request: Request):
    _id_ok(pid, "Item not found")
    b = await _body(request)
    name = _t(b, "name").strip()
    if not name:
        raise HTTPException(422, "Name required")
    if len(name) > 120:
        raise HTTPException(422, "Name is too long (max 120 characters)")
    if "group_by" in b and b["group_by"] not in ("colour", "size"):  # validate everything before writing anything
        raise HTTPException(422, "group_by must be colour or size")
    cat = " ".join(_t(b, "category").split())[:40]
    alias = _t(b, "alias").strip()[:120]
    pl = _h422(_whole, b.get("pick_loc") or 0, "pick_loc")
    pend = _h422(_flag_of, b.get("pending"), "pending")
    cph = _h422(_flag_of, b.get("colour_photos"), "colour_photos")
    pk, pn = _h422(_pack_of, b) if ("pack" in b or "ppc" in b) else ("", 0)
    c = db.connect()
    try:
        if not c.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone():
            raise HTTPException(404, "Item not found (it may have been merged or deleted)")
        if pl and not c.execute("SELECT 1 FROM locations WHERE id=? AND role='picking' AND active=1", (pl,)).fetchone():
            raise HTTPException(422, "That isn't an active picking warehouse")
        c.execute("BEGIN IMMEDIATE")
        try:
            if pend and c.execute("SELECT 1 FROM movements m JOIN variants v ON v.id=m.variant_id WHERE v.product_id=? LIMIT 1", (pid,)).fetchone():
                raise HTTPException(409, "This item already has stock history, so it can't go back to pending")
            c.execute("UPDATE products SET name=? WHERE id=?", (name, pid))
            if "alias" in b:  # left out (e.g. Activate / Move back to pending) = keep the internal name
                c.execute("UPDATE products SET alias=? WHERE id=?", (alias, pid))
            if "pack" in b or "ppc" in b:
                c.execute("UPDATE products SET ppc=?, pack=? WHERE id=?", (pn, pk, pid))
            if "group_by" in b:  # how the Stock tab and bot nest this item: colour then size, or size then colour
                c.execute("UPDATE products SET group_by=? WHERE id=?", (b["group_by"], pid))
            if "category" in b:  # free-text label used for the category chips on the Stock tab ('' = uncategorised)
                same = cat and c.execute("SELECT category FROM products WHERE category=? COLLATE NOCASE AND id<>? LIMIT 1", (cat, pid)).fetchone()
                c.execute("UPDATE products SET category=? WHERE id=?", (same["category"] if same else cat, pid))  # reuse existing spelling
            if "pending" in b:  # Activate (or put back to pending)
                c.execute("UPDATE products SET pending=? WHERE id=?", (1 if pend else 0, pid))
            if "colour_photos" in b:  # printed designs: a photo per colour
                c.execute("UPDATE products SET colour_photos=? WHERE id=?", (1 if cph else 0, pid))
            if "pick_loc" in b:  # the one picking warehouse this item is picked from (0/blank = work it out from stock)
                c.execute("UPDATE products SET pick_loc=? WHERE id=?", (pl or None, pid))
            c.execute("COMMIT")
        except Exception:
            _rollback(c)
            raise
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Another item already has that name")
    finally:
        c.close()
    return {"ok": True}


@app.post("/api/product/{pid}/delete")
async def pending_delete(pid: int, request: Request):
    """Delete a pending item, or some of its colours/sizes: set up by mistake and never received, so nothing is lost.
    Body: {} = the whole item; {"colour": c} / {"size": s} / both = the lines that match. Refused once it has history."""
    b = await request.json()
    c = db.connect()
    try:
        p = c.execute("SELECT id, pending, photo FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "No such item")
        if not p["pending"]:
            raise HTTPException(409, "Only pending items can be deleted. An active item keeps its lines; mark them discontinued instead")
        where, args = "product_id=?", [pid]
        for k in ("colour", "size"):
            if k in b:
                where += " AND %s=?" % k
                args.append(str(b.get(k) or "").strip())
        vids = [r["id"] for r in c.execute("SELECT id FROM variants WHERE " + where, args)]
        if not vids and ("colour" in b or "size" in b):
            raise HTTPException(404, "Nothing matches that colour/size on this item")
        q = ",".join("?" * len(vids)) or "NULL"
        if c.execute("SELECT 1 FROM movements WHERE variant_id IN (%s) LIMIT 1" % q, vids).fetchone():
            raise HTTPException(409, "This already has stock history, so it can't be deleted")
        files = []
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("DELETE FROM variants WHERE id IN (%s)" % q, vids)
            gone = not c.execute("SELECT 1 FROM variants WHERE product_id=? LIMIT 1", (pid,)).fetchone()
            # a colour photo goes when no line of that colour is left
            for r in c.execute("SELECT colour, photo FROM colour_photos WHERE product_id=?", (pid,)).fetchall():
                if gone or not c.execute("SELECT 1 FROM variants WHERE product_id=? AND colour=?", (pid, r["colour"])).fetchone():
                    c.execute("DELETE FROM colour_photos WHERE product_id=? AND colour=?", (pid, r["colour"]))
                    files.append(r["photo"])
            if gone:  # its last line went (or the whole item was asked for): the item goes too, or it would sit there invisible
                c.execute("DELETE FROM homes WHERE product_id=?", (pid,))
                c.execute("DELETE FROM products WHERE id=?", (pid,))
                if p["photo"]:
                    files.append(p["photo"])
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    finally:
        c.close()
    for f in files:
        try:
            os.remove(os.path.join(PHOTOS, os.path.basename(f)))
        except OSError:
            pass
    return {"deleted": len(vids), "item_deleted": gone}


@app.post("/api/rename")
async def rename(request: Request):
    b = await _body(request)
    if b.get("product_id") is None or "kind" not in b:
        raise HTTPException(422, "product_id and kind are required")
    pid, kind, new = _h422(_whole, b["product_id"], "product_id"), b["kind"], _t(b, "new").strip()
    if kind not in ("colour", "size"):
        raise HTTPException(422, "bad kind")
    if not new:
        raise HTTPException(422, "The new name can't be blank")
    if len(new) > 80:
        raise HTTPException(422, "The new name is too long (max 80 characters)")
    old, colour = _t(b, "old"), _t(b, "colour")
    c = db.connect()
    try:
        if kind == "colour":
            rows = c.execute("SELECT * FROM variants WHERE product_id=? AND colour=?", (pid, old)).fetchall()
        elif "colour" in b:  # one size of one colour
            rows = c.execute("SELECT * FROM variants WHERE product_id=? AND colour=? AND size=?", (pid, colour, old)).fetchall()
        else:  # a size in every colour
            rows = c.execute("SELECT * FROM variants WHERE product_id=? AND size=?", (pid, old)).fetchall()
        if not rows:
            raise HTTPException(404, "Nothing to rename: that %s isn't on this item (any more)" % kind)
        merged = 0
        c.execute("BEGIN IMMEDIATE")
        for v in rows:
            nc, ns = (new, v["size"]) if kind == "colour" else (v["colour"], new)
            t = c.execute("SELECT id FROM variants WHERE product_id=? AND colour=? AND size=? AND id!=?",
                          (pid, nc, ns, v["id"])).fetchone()
            if t:  # merging into an existing variant: its history moves over, the higher of each level is kept
                c.execute("UPDATE movements SET variant_id=? WHERE variant_id=?", (t["id"], v["id"]))
                c.execute("UPDATE variants SET reorder_at=MAX(reorder_at, ?), pick_min=MAX(pick_min, ?), pick_max=MAX(pick_max, ?),"
                          " discontinued=MIN(discontinued, ?) WHERE id=?",
                          (v["reorder_at"], v["pick_min"], v["pick_max"], v["discontinued"], t["id"]))
                c.execute("UPDATE variants SET ppc=?, pack=? WHERE id=? AND pack='' AND ppc=0",  # keep the target's carton contents if it has any
                          (v["ppc"], v["pack"], t["id"]))
                c.execute("DELETE FROM variants WHERE id=?", (v["id"],))
                merged += 1
            else:
                c.execute("UPDATE variants SET colour=?, size=? WHERE id=?", (nc, ns, v["id"]))
        if kind == "colour" and old.lower() != new.lower():
            if c.execute("SELECT 1 FROM colour_photos WHERE product_id=? AND colour=?", (pid, new)).fetchone():
                c.execute("DELETE FROM colour_photos WHERE product_id=? AND colour=?", (pid, old))
            else:
                c.execute("UPDATE colour_photos SET colour=? WHERE product_id=? AND colour=?", (new, pid, old))
        c.execute("COMMIT")
    finally:
        _rollback(c)
        c.close()
    return {"merged": merged}


LEVEL_MAX = {"reorder_at": MAX_QTY, "pick_min": MAX_QTY, "pick_max": MAX_QTY, "ppc": 1_000_000, "discontinued": 1}


@app.post("/api/reorder")
async def reorder(request: Request):
    """Set a per-variant number (a level in cartons, 0 = off; also pieces per carton and the discontinued flag). With colour+size: one variant. With colour only: every size of that colour. Size only: every colour of that size. Neither: every variant of the item."""
    b = await _body(request)
    if b.get("product_id") is None:
        raise HTTPException(422, "product_id required")
    pid = _h422(_whole, b["product_id"], "product_id")
    where, args = "product_id=?", [pid]
    if "colour" in b and "size" in b:
        where, args = where + " AND colour=? AND size=?", args + [_t(b, "colour"), _t(b, "size")]
    elif "colour" in b:  # every size of one colour
        where, args = where + " AND colour=?", args + [_t(b, "colour")]
    elif "size" in b:  # every colour of one size
        where, args = where + " AND size=?", args + [_t(b, "size")]
    if b.get("field") == "pack":  # carton contents as text ('24 doz'); pieces worked out from it
        sets, vals = "pack=?, ppc=?", list(parse_pack(_t(b, "level", "pack")))
    else:
        col = {"reorder": "reorder_at", "pick_min": "pick_min", "pick_max": "pick_max", "ppc": "ppc",
               "discontinued": "discontinued"}.get(b.get("field", "reorder")) if isinstance(b.get("field", "reorder"), str) else None
        if not col:
            raise HTTPException(422, "Unknown level type")
        try:
            level = _whole(b.get("level"), "level", -MAX_ID)
        except ValueError:
            raise HTTPException(422, "product_id and a whole-number level are required")
        if level < 0:
            raise HTTPException(422, "Level can't be negative")
        if col == "discontinued" and level not in (0, 1):
            raise HTTPException(422, "discontinued is 0 or 1")
        if level > LEVEL_MAX[col]:
            raise HTTPException(422, "That number is too large (max %d)" % LEVEL_MAX[col])
        sets, vals = "%s=?" % col, [level]  # col is one of the fixed names above
    c = db.connect()
    try:
        n = c.execute("UPDATE variants SET %s WHERE %s" % (sets, where), vals + args).rowcount
    finally:
        c.close()
    if not n:
        raise HTTPException(404, "Nothing to update: that item or colour/size doesn't exist (any more)")
    return {"updated": n}


ROLES = ("picking", "holding")


@app.post("/api/location")
async def location_add(request: Request):
    b = await _body(request)
    name = _t(b, "name").strip()
    role = b.get("role", "holding")
    if not name:
        raise HTTPException(422, "Name required")
    if len(name) > 60:
        raise HTTPException(422, "Name is too long (max 60 characters)")
    if role not in ROLES:
        raise HTTPException(422, "Role must be picking or holding")
    c = db.connect()
    try:
        c.execute("INSERT INTO locations(name, role) VALUES(?,?)", (name, role))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Name already used")
    finally:
        c.close()
    return {"ok": True}


@app.post("/api/location/{lid}")
async def location_edit(lid: int, request: Request):
    _id_ok(lid, "Unknown warehouse")
    b = await _body(request)
    upd = {}  # validate every field first, then write them all in one transaction, so a rejected edit changes nothing
    if "name" in b:
        if not _t(b, "name").strip():
            raise HTTPException(422, "Name required")
        upd["name"] = _t(b, "name").strip()[:60]
    if "role" in b:
        if b["role"] not in ROLES:
            raise HTTPException(422, "Role must be picking or holding")
        upd["role"] = b["role"]
    if "active" in b:
        upd["active"] = 1 if _h422(_flag_of, b["active"], "active") else 0
    c = db.connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        if not c.execute("SELECT 1 FROM locations WHERE id=?", (lid,)).fetchone():
            raise HTTPException(404, "Unknown warehouse (it may have been deleted)")
        # a negative line is stock the records say went missing: it has to be recounted before the warehouse is put away
        if upd.get("active") == 0 and c.execute("SELECT 1 FROM stock WHERE location_id=? AND qty<>0", (lid,)).fetchone():
            raise HTTPException(409, "Still holds stock (or negative lines): fix them first")
        for k, v in upd.items():  # k is one of the three fixed names above
            c.execute("UPDATE locations SET %s=? WHERE id=?" % k, (v, lid))
        c.execute("COMMIT")
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Name already used")
    finally:
        _rollback(c)
        c.close()
    return {"ok": True}


@app.post("/api/location/{lid}/sections")
async def sections_save(lid: int, request: Request):
    """Replace a warehouse's sections (drawn on its map, positions as fractions of the picture) and its on/off switch."""
    _id_ok(lid, "Unknown warehouse")
    b = await _body(request)
    want = b.get("sections") or []
    if not isinstance(want, list) or not all(isinstance(s, dict) for s in want):
        raise HTTPException(422, "sections must be a list")
    names = [_t(s, "name", "section name").strip() for s in want]
    if any(not n for n in names):
        raise HTTPException(422, "Every section needs a name")
    if any(len(n) > 60 for n in names):
        raise HTTPException(422, "Section name is too long (max 60 characters)")
    if len({n.lower() for n in names}) != len(names):
        raise HTTPException(422, "Two sections have the same name")
    try:
        for x in want:
            [float(_text(x.get(k), "position") or 0) for k in ("x", "y", "w", "h")]
    except (TypeError, ValueError):
        raise HTTPException(422, "Section position must be numbers")
    ids = [_h422(_whole, s.get("id") or 0, "section id") for s in want]
    on = _h422(_flag_of, b.get("on"), "on")
    c = db.connect()
    try:
        if not c.execute("SELECT 1 FROM locations WHERE id=?", (lid,)).fetchone():
            raise HTTPException(404, "Unknown warehouse")
        have = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM sections WHERE location_id=?", (lid,))}
        keep = {i for i in ids if i in have}
        gone = [i for i in have if i not in keep]
        held = {}
        for r in c.execute("SELECT DISTINCT variant_id, lot FROM movements WHERE location_id=?", (lid,)).fetchall():
            for (sec, _), n in piles(c, r[0], r[1], lid).items():
                held[sec] = held.get(sec, 0) + n
        busy = [have[i] for i in gone if held.get(i, 0) > 0]
        if busy:
            raise HTTPException(409, "Still holds stock, move it first: " + ", ".join(sorted(busy)))
        c.execute("BEGIN IMMEDIATE")
        for i in gone:
            c.execute("DELETE FROM homes WHERE section_id=?", (i,))
            c.execute("DELETE FROM sections WHERE id=?", (i,))
        for i in keep:  # park kept names first, so sections can swap names in one save
            c.execute("UPDATE sections SET name=? WHERE id=?", ("~%d~" % i, i))
        for s, n, i in zip(want, names, ids):
            box = [min(1.0, max(0.0, float(_text(s.get(k), "position") or 0))) for k in ("x", "y", "w", "h")]
            if i in keep:
                c.execute("UPDATE sections SET name=?, x=?, y=?, w=?, h=? WHERE id=?", (n, *box, i))
            else:
                c.execute("INSERT INTO sections(location_id,name,x,y,w,h) VALUES(?,?,?,?,?,?)", (lid, n, *box))
        if "on" in b:
            c.execute("UPDATE locations SET has_sections=? WHERE id=?", (1 if on else 0, lid))
        c.execute("COMMIT")
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Two sections have the same name")
    finally:
        _rollback(c)
        c.close()
    return {"ok": True}


@app.post("/api/location/{lid}/map")
def map_upload(lid: int, file: UploadFile = File(...)):
    _id_ok(lid, "Unknown warehouse")
    data = file.file.read(8 * 1024 * 1024 + 1)  # never more than the limit into memory
    if not data:
        raise HTTPException(422, "Empty picture")
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(413, "Picture too large (max 8 MB)")
    if not data.startswith(IMAGE_MAGIC):
        raise HTTPException(422, "That file isn't a picture")
    os.makedirs(MAPS, exist_ok=True)
    name = "loc%d-%d.jpg" % (lid, int(time.time() * 1000))
    c = db.connect()
    try:
        r = c.execute("SELECT map FROM locations WHERE id=?", (lid,)).fetchone()
        if not r:
            raise HTTPException(404, "Unknown warehouse")
        with open(os.path.join(MAPS, name), "wb") as f:
            f.write(data)
        c.execute("UPDATE locations SET map=? WHERE id=?", (name, lid))
        if r["map"]:
            try:
                os.remove(os.path.join(MAPS, os.path.basename(r["map"])))
            except OSError:
                pass
    finally:
        c.close()
    return {"ok": True, "map": "/maps/" + name}


@app.get("/maps/{name}")
def map_file(name: str):
    path = os.path.join(MAPS, os.path.basename(name))
    if not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=31536000"})


@app.delete("/api/location/{lid}")
def location_del(lid: int):
    _id_ok(lid, "Unknown warehouse")
    c = db.connect()
    try:
        c.execute("BEGIN IMMEDIATE")  # checked inside the transaction: a receive can't slip in between check and delete
        if not c.execute("SELECT 1 FROM locations WHERE id=?", (lid,)).fetchone():
            raise HTTPException(404, "Unknown warehouse (it may have been deleted already)")
        if c.execute("SELECT 1 FROM movements WHERE location_id=?", (lid,)).fetchone():
            raise HTTPException(409, "Warehouse has history; archive it instead")
        c.execute("UPDATE products SET pick_loc=NULL WHERE pick_loc=?", (lid,))  # items set to pick from it fall back to automatic
        c.execute("DELETE FROM homes WHERE location_id=?", (lid,))  # sections drawn on a warehouse that never held stock go with it
        c.execute("DELETE FROM sections WHERE location_id=?", (lid,))
        m = c.execute("SELECT map FROM locations WHERE id=?", (lid,)).fetchone()
        c.execute("DELETE FROM locations WHERE id=?", (lid,))
        c.execute("COMMIT")
        if m and m["map"]:
            try:
                os.remove(os.path.join(MAPS, os.path.basename(m["map"])))
            except OSError:
                pass
    finally:
        _rollback(c)
        c.close()
    return {"ok": True}


def get_setting(k, default=""):
    c = db.connect()
    r = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    c.close()
    return r[0] if r else default


@app.get("/api/settings")
def settings_get():
    key = get_setting("anthropic_api_key")  # the key itself is never sent back to any client
    return {"key_set": bool(key), "key_hint": ("..." + key[-4:]) if key else "",
            "env_key": bool(creds.from_env("ANTHROPIC_API_KEY")), "backup": db.backup_status()}


@app.post("/api/settings")
async def settings_set(request: Request):
    b = await _body(request)
    if "api_key" in b:
        k = _t(b, "api_key").strip()
        if k and not k.startswith("sk-ant-"):
            raise HTTPException(422, "That doesn't look like an Anthropic key (it should start with sk-ant-)")
        c = db.connect()
        if k:
            c.execute("INSERT OR REPLACE INTO settings(k,v) VALUES('anthropic_api_key',?)", (k,))
        else:
            c.execute("DELETE FROM settings WHERE k='anthropic_api_key'")
        c.close()
    return {"ok": True}


def _tg_view():
    t = tg.setting("tg_token")  # the token itself is never sent back to any client
    return {"token_set": bool(t), "token_hint": ("..." + t[-4:]) if t else "", "env_token": bool(creds.from_env("TELEGRAM_BOT_TOKEN")),
            "users": tg.setting("tg_users"),
            "group": tg.setting("tg_group"), "hour": tg.setting("tg_hour"), "bot": tg.STATE["bot"],
            "running": tg.STATE["ok"], "err": tg.STATE["err"], "post_err": tg.STATE["post_err"]}


@app.get("/api/telegram")
def telegram_get():
    return _tg_view()


@app.post("/api/telegram")
async def telegram_set(request: Request):
    b = await _body(request)
    upd = {}  # validate everything (token included) before saving anything, so a rejected request changes nothing
    if "users" in b:
        parts = [p for p in re.split(r"[,;\s]+", _t(b, "users")) if p]  # commas, semicolons or spaces
        if not all(tg.is_chat_id(p) for p in parts):
            raise HTTPException(422, "User IDs are numbers, separated by commas or spaces")
        upd["tg_users"] = ", ".join(parts)
    if "group" in b:
        g = _t(b, "group").strip()
        if g and not tg.is_chat_id(g):
            raise HTTPException(422, "The group chat ID is a number (groups usually start with -)")
        upd["tg_group"] = g
    if "hour" in b:
        h = _t(b, "hour").strip()
        if h and not (re.fullmatch(r"[0-9]{1,2}", h) and int(h) <= 23):
            raise HTTPException(422, "Daily post hour must be 0 to 23, or blank for off")
        upd["tg_hour"] = h
    me = None
    if "token" in b:
        t = _t(b, "token").strip()
        if t:
            try:
                me = await asyncio.to_thread(tg.api, t, "getMe")
            except RuntimeError as e:
                raise HTTPException(422, "Telegram rejected that token: " + str(e))
        upd["tg_token"] = t
    tg.put_many(upd)
    if "tg_token" in upd:
        if me is not None:
            tg.STATE.update(bot=me.get("username", ""), err="")
        else:
            tg.STATE.update(bot="", ok=False, err="")
    return _tg_view()


@app.post("/api/telegram/test")
async def telegram_test():
    try:
        n = await asyncio.to_thread(tg.post_refill)
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    return {"posted": n}


@app.get("/photos/{name}")
def photo(name: str):
    path = os.path.join(PHOTOS, os.path.basename(name))
    if not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=31536000"})


@app.post("/api/extract")
def extract_ep(file: UploadFile = File(...)):
    from . import extract

    data = file.file.read(8_000_001)  # never more than the limit into memory
    if len(data) > 8_000_000:
        raise HTTPException(413, "Image too large")
    c = db.connect()
    names = [r["name"] + (" | " + r["alias"] if r["alias"] else "") for r in c.execute("SELECT name, alias FROM products")]
    c.close()
    try:
        return extract.extract(data, names, get_setting("anthropic_api_key"))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        if "Timeout" in type(e).__name__:
            raise HTTPException(504, "Claude didn't answer within 45 seconds")
        raise HTTPException(502, "Extraction failed: " + str(e)[:200])


def _x(t):
    """Text for a reportlab Paragraph (which reads it as markup)."""
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pdf_item(c, p, col, size, lot, zero, cnt, locs, upd, tot, split, dis, ss, dated=True):
    """One item's part of a stock PDF: heading, total, legend and its table(s).
    Returns (flowables, number of table columns), or (None, 0) when there is nothing in stock to show."""
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    pid = p["id"]
    ids = [int(x) for x in locs.split(",") if re.fullmatch(r"[0-9]+", x.strip())]
    lrows = [r for r in c.execute("SELECT * FROM locations ORDER BY id") if (r["id"] in ids if ids else r["active"])]
    lids = [r["id"] for r in lrows]
    if not (col or size or lot) or not lids:
        raise HTTPException(422, "Pick at least one of colour/size/lot and one warehouse")
    vrows = c.execute("SELECT id, colour, size, reorder_at, discontinued FROM variants WHERE product_id=?", (pid,)).fetchall()
    vm = {v["id"]: v for v in vrows}
    st = c.execute("SELECT s.variant_id vid, s.lot, s.location_id l, s.qty q FROM stock s "
                   "JOIN variants v ON v.id=s.variant_id WHERE v.product_id=?", (pid,)).fetchall()
    G = {}

    def key(cc, ss_, ll):
        return (cc if col else "", ss_ if size else "", ll if lot else "")

    TOT = {}  # real stock per variant over ALL warehouses (highlights don't depend on which warehouse columns are shown)
    for r in st:
        TOT[r["vid"]] = TOT.get(r["vid"], 0) + r["q"]
        v = vm[r["vid"]]
        if r["l"] in lids and r["q"] != 0 and (dis or not v["discontinued"]):  # a negative (flagged) line counts too, like everywhere else
            d = G.setdefault(key(v["colour"], v["size"], r["lot"]), {})
            d[r["l"]] = d.get(r["l"], 0) + r["q"]
    LT, LT2 = {}, {}  # most recent movement per row (any kind: receive, sale, adjust, transfer, recount)
    for r in c.execute("SELECT v.colour, v.size, m.lot, m.location_id l, MAX(m.ts) t FROM movements m JOIN variants v ON v.id=m.variant_id "
                       "WHERE v.product_id=? AND m.type<>'move' GROUP BY v.colour, v.size, m.lot, m.location_id", (pid,)):
        if r["l"] in lids:
            k = key(r["colour"], r["size"], r["lot"])
            LT[k] = max(LT.get(k, ""), r["t"])
            LT2[k[:2]] = max(LT2.get(k[:2], ""), r["t"])
    M = {}  # per displayed (colour, size) row: stock, reorder level, whether every variant in it is discontinued
    for v in vrows:
        if not dis and v["discontinued"]:
            continue
        m = M.setdefault((v["colour"] if col else "", v["size"] if size else ""), {"tot": 0, "lvl": 0, "act": 0})
        if not v["discontinued"]:
            m["act"] += 1
            m["tot"] += TOT.get(v["id"], 0)
            m["lvl"] += v["reorder_at"]
    has = {l for d in G.values() for l in d}  # a warehouse with no stock of this item gets no column
    lrows = [r for r in lrows if r["id"] in has]
    lids = [r["id"] for r in lrows]
    if zero:  # every variant that exists is listed, even with nothing in stock
        have = {k[:2] for k in G}
        for kk in M:
            if kk not in have:
                G.setdefault(kk + ("",), {})
                have.add(kk)
    if not G:
        return None, 0
    if lot and not any(k[2] for k in G):  # no lots on this item: no column full of dashes
        lot = 0
    # split: one table per size (or per colour); that column then becomes the table's heading.
    # tot: totals only, no column per warehouse.
    sp = {"size": 1, "colour": 0}.get(split) if (split == "size" and size) or (split == "colour" and col) else None
    wl = [] if tot else lids
    show = [on and i != sp for i, on in enumerate((col, size, lot))]
    npre = sum(show)
    first = show.index(True) if npre else None  # the leading column: merged into one cell per value, thick line between values
    head = [h for h, on in zip(("Colour", "Size", "Lot"), show) if on] + [r["name"].replace(" ", "\n") if len(r["name"]) > 9 else r["name"] for r in lrows if not tot] \
        + (["Last updated"] if upd else []) + ["Total"] + (["Counted"] if cnt else [])
    order = (lambda k: (tg.size_key(k[1]), nat(k[0]), nat(k[2]))) if sp == 1 else (lambda k: (nat(k[0]), tg.size_key(k[1]), nat(k[2])))
    groups = {}
    for k in sorted(G, key=order):
        groups.setdefault(k[sp] if sp is not None else None, []).append(k)
    els = [Paragraph(_x(p["name"] + (" (%s)" % p["alias"] if p["alias"] else "")), ss["Heading2"])]
    if dated:
        els.append(Paragraph("Stock count as of %s - quantities in cartons" % tg.now_local().strftime("%d/%m/%y"), ss["Normal"]))
    els += [Paragraph("<b>Total in stock: %d cartons</b>" % sum(sum(d.values()) for d in G.values()), ss["Heading3"]), Spacer(1, 6)]
    used = set()
    for g, ks in groups.items():
        body, marks, lead_vals = [], [], []
        for k in ks:
            cells = [G[k].get(l, "-") for l in wl]
            pre = [x or ("-" if i == 2 else "n/a") for i, (x, on) in enumerate(zip(k, show)) if on]  # no colour/size = n/a; no lot = -
            m = M.get(k[:2], {"act": 1, "tot": 1, "lvl": 0})
            if m["act"] == 0:  # discontinued: stays in the table, never highlighted
                used.add("disc")
                if pre:
                    si = sum(show[:1]) if show[1] else 0  # the size column if shown, else the first column
                    pre[si] += " (discontinued)"
            elif m["tot"] <= 0:
                used.add("red")
                marks.append((len(body) + 1, "#f5a3a3"))
            elif m["lvl"] > 0 and m["tot"] <= m["lvl"]:
                used.add("yellow")
                marks.append((len(body) + 1, "#ffe680"))
            lead_vals.append((k[first], m["act"] == 0 and not show[1]) if first is not None else None)
            body.append(pre + cells + ([tg.local_day(LT.get(k) or LT2.get(k[:2])) if (k in LT or k[:2] in LT2) else "-"] if upd else []) + [sum(G[k].values())] + ([""] if cnt else []))
        tc = len(head) - (2 if cnt else 1)  # the Total column: bold on every row (no grand-total line at the bottom)
        extra = [("FONTNAME", (tc, 1), (tc, -1), "Helvetica-Bold")]
        if npre >= 2:  # same colour (or size) over several rows: one merged cell, a thick line where the next one starts
            i = 0
            while i < len(lead_vals):
                j = i
                while j + 1 < len(lead_vals) and lead_vals[j + 1] == lead_vals[i]:
                    j += 1
                if i > 0 and lead_vals[i - 1][0] != lead_vals[i][0]:
                    extra.append(("LINEABOVE", (0, i + 1), (-1, i + 1), 1.8, colors.black))
                for a in range(i, j + 1, 20):  # a merged cell can't break across pages: keep each one to 20 rows
                    z = min(a + 19, j)
                    if z > a:
                        extra.append(("SPAN", (0, a + 1), (0, z + 1)))
                i = j + 1
            extra.append(("VALIGN", (0, 1), (0, -1), "MIDDLE"))
        t = _fit_table([head] + body, npre, [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3c3c3c")),
                                             ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                                             ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                                             ("ALIGN", (npre, 0), (-1, -1), "RIGHT")] +
                       [("BACKGROUND", (1 if npre >= 2 else 0, i), (-1, i), colors.HexColor(cl)) for i, cl in marks] + extra)
        if g is not None:
            els.append(Paragraph(("Size: " if sp == 1 else "Colour: ") + _x(g or "(none)"), ss["Heading3"]))
        els += [t, Spacer(1, 12)]
    leg = [x for f, x in (("yellow", "yellow = at or below the reorder level"), ("red", "red = out of stock"),
                          ("disc", "(discontinued) = not being reordered, never highlighted")) if f in used]
    if leg:
        at = 3 if dated else 2
        els.insert(at, Paragraph("<i>" + "; ".join(leg) + "</i>", ss["Normal"]))
        els.insert(at + 1, Spacer(1, 6))
    while els and isinstance(els[-1], Spacer):
        els.pop()
    return els, len(head)


PAGE_W = (595.27 - 72, 841.89 - 72)  # usable width of an A4 page with 36pt margins: portrait, landscape


def _fit_table(data, npre, style):
    """A stock table that fits the page. Portrait when it fits at the normal 9pt, else landscape; when even landscape is
    too narrow (many warehouses, long colours or lots), the font gets smaller and the leading Colour/Size/Lot columns wrap
    their text inside a narrower column, until every column, Total included, is on the page."""
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import Paragraph, Table, TableStyle

    t = None
    for fs, cap, pad in ((9, None, 6), (8, None, 6), (8, 120, 4), (7, 100, 3), (6, 80, 2), (6, 60, 2), (5, 50, 1.5)):
        rows, widths = data, None
        if cap:
            ps = ParagraphStyle("k%d" % fs, fontName="Helvetica", fontSize=fs, leading=fs + 2)
            rows = [list(r) for r in data]
            # every width is given: next to a wrapped cell, reportlab would stretch a column left open to the whole page
            widths = [max(stringWidth(ln, "Helvetica-Bold", fs) for r in data for ln in str(r[i]).split("\n")) + 2 * pad
                      for i in range(len(data[0]))]
            for i in range(npre):
                widths[i] = min(cap, widths[i])
                for r in rows[1:]:
                    r[i] = Paragraph(_x(r[i]), ps)
        t = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
        t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), fs), ("LEADING", (0, 0), (-1, -1), fs + 2 if fs < 9 else 12),
                               ("LEFTPADDING", (0, 0), (-1, -1), pad), ("RIGHTPADDING", (0, 0), (-1, -1), pad)] + style))
        if t.wrap(10000, 10000)[0] <= PAGE_W[1]:
            break
    return t


def _pdf_build(els, wide, title, note):
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    from reportlab.platypus import Table

    def tables(xs):
        for x in xs:
            if isinstance(x, Table):
                yield x
            elif hasattr(x, "_content"):  # KeepTogether
                yield from tables(x._content)

    # portrait unless a table really is too wide for it (36pt margins each side)
    fits = all(t.wrap(10000, 10000)[0] <= A4[0] - 72 for t in tables(els))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4 if fits else landscape(A4), title=title,
                            leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    if note.strip():
        els += [Spacer(1, 10), Paragraph(_x(note.strip()), getSampleStyleSheet()["Normal"])]
    while els and isinstance(els[-1], Spacer):  # a trailing spacer that doesn't fit the page would push an empty page out
        els.pop()
    doc.build(els)
    return buf.getvalue()


@app.get("/api/export/{pid}")
def export(pid: int, col: int = 1, size: int = 1, lot: int = 1, zero: int = 1, cnt: int = 0,
           locs: str = "", note: str = "", upd: int = 1, tot: int = 0, split: str = "", dis: int = 1):
    from reportlab.lib.styles import getSampleStyleSheet

    _id_ok(pid, "Item not found")
    c = db.connect()
    try:
        p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "Item not found")
        els, n = _pdf_item(c, p, col, size, lot, zero, cnt, locs, upd, tot, split, dis, getSampleStyleSheet())
    finally:
        c.close()
    if not els:
        raise HTTPException(404, "No stock for this item")
    data = _pdf_build(els, n, p["name"], note)
    fn = re.sub(r"[^A-Za-z0-9]+", "-", p["name"]).strip("-") + "-" + tg.now_local().strftime("%Y-%m-%d") + ".pdf"
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="%s"' % fn})


@app.get("/api/export")
def export_many(items: str = "", col: int = 1, size: int = 1, lot: int = 1, zero: int = 1, cnt: int = 0,
                locs: str = "", note: str = "", upd: int = 1, tot: int = 0, split: str = "auto", dis: int = 1):
    """Several items in one PDF, one after the other (each kept on one page where it fits).
    split=auto: each item uses its own grouping (size-first items get a table per size)."""
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import KeepTogether, Paragraph, Spacer

    pids = [int(x) for x in items.split(",") if re.fullmatch(r"[0-9]{1,15}", x.strip())]
    if not pids:
        raise HTTPException(422, "Pick at least one item")
    ss = getSampleStyleSheet()
    els = [Paragraph("Stock report", ss["Title"]),
           Paragraph("Stock count as of %s - quantities in cartons" % tg.now_local().strftime("%d/%m/%y"), ss["Normal"]), Spacer(1, 10)]
    wide, empty, names = 0, [], []
    c = db.connect()
    try:
        for pid in dict.fromkeys(pids):
            p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            if not p:
                continue
            sp = ("size" if p["group_by"] == "size" else "") if split == "auto" else split
            e, n = _pdf_item(c, p, col, size, lot, zero, cnt, locs, upd, tot, sp, dis, ss, dated=False)
            if not e:
                empty.append(p["name"])
                continue
            names.append(p["name"])
            wide = max(wide, n)
            els += [KeepTogether(e), Spacer(1, 18)]
    finally:
        c.close()
    if not names:
        raise HTTPException(404, "None of these items has stock to show")
    if empty:
        els.append(Paragraph("<i>No stock to show: %s</i>" % _x(", ".join(empty)), ss["Normal"]))
    data = _pdf_build(els, wide, "Stock report", note)
    fn = (re.sub(r"[^A-Za-z0-9]+", "-", names[0]).strip("-") if len(names) == 1 else "stock-report") + "-" + tg.now_local().strftime("%Y-%m-%d") + ".pdf"
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="%s"' % fn})


@app.get("/api/stock-request")
def stock_request(dest: int = 0):
    """Printable list of what to carry from holding to picking (same rule as the bell's pending list)."""
    try:
        data, name = tg.stock_request_pdf(dest)
    except LookupError as e:
        code, msg = str(e.args[0]).split("|", 1)
        raise HTTPException(int(code), msg)
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="%s"' % name})


STATIC = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC, exist_ok=True)
NOCACHE = {"Cache-Control": "no-cache"}


def _stamped(name, media):
    text = open(os.path.join(STATIC, name), encoding="utf-8").read().replace("__BUILD__", VERSION)
    return Response(text, media_type=media, headers=NOCACHE)


@app.get("/")
@app.get("/index.html")
def index():
    return _stamped("index.html", "text/html")


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
def touch_icon():  # iOS asks for these by default when a page names no icon it can load
    return FileResponse(os.path.join(STATIC, "icon-180.png"), media_type="image/png")


@app.get("/sw.js")
def service_worker():
    return _stamped("sw.js", "application/javascript")  # changes with every build, so phones pick up a new one


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
