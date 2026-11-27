"""Telegram bot: answers stock questions and posts the "refill picking" list to a group.

Outbound only (long polling to api.telegram.org); nothing here listens on the internet.
The bot token is saved in Settings (settings table) or given as TELEGRAM_BOT_TOKEN(_FILE); it is never sent back to any client.
Read-only: the bot can look stock up but never changes the ledger.
"""
import datetime
import difflib
import html as _h
import json
import re
import os
import threading
import time
import urllib.request
from zoneinfo import ZoneInfo

from . import creds, db

BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
STATE = {"bot": "", "err": "", "ok": False, "post_err": ""}  # post_err: last problem posting to the group (kept until a post succeeds)
_stop = threading.Event()
_thread = None


# ---------- settings ----------
def setting(k, default=""):
    c = db.connect()
    r = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    c.close()
    return r[0] if r else default


def bot_token():
    """The bot token: the one saved in Settings, else TELEGRAM_BOT_TOKEN / TELEGRAM_BOT_TOKEN_FILE from the container."""
    return setting("tg_token") or creds.from_env("TELEGRAM_BOT_TOKEN")


def put(k, v):
    put_many({k: v})


def put_many(kv):
    """Save several settings in one transaction (blank or None deletes the setting)."""
    c = db.connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        for k, v in kv.items():
            if v == "" or v is None:
                c.execute("DELETE FROM settings WHERE k=?", (k,))
            else:
                c.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (k, str(v)))
        c.execute("COMMIT")
    finally:
        if c.in_transaction:
            c.execute("ROLLBACK")
        c.close()


def is_chat_id(s):
    """A Telegram user or chat ID: ASCII digits with an optional leading minus. str.isdigit() is not enough,
    because it is also true for characters such as "²" that int() refuses."""
    return bool(re.fullmatch(r"-?[0-9]+", s or ""))


def allowed_users():
    out = set()
    for p in re.split(r"[,;\s]+", setting("tg_users")):
        if is_chat_id(p.strip()):  # a bad entry saved by an older version is skipped instead of breaking every message
            out.add(int(p))
    return out


# ---------- Telegram HTTP ----------
def api(token, method, params=None, timeout=15):
    req = urllib.request.Request("%s/bot%s/%s" % (BASE, token, method), json.dumps(params or {}).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
    except Exception as e:  # never let the token leak into an error message
        raise RuntimeError(str(e).replace(token, "<token>"))
    if not d.get("ok"):
        raise RuntimeError(str(d.get("description", "Telegram error")).replace(token, "<token>"))
    return d["result"]


def send(token, chat, text, html=False, markup=None):
    p = {"chat_id": chat, "text": text[:4096]}
    if html:
        p["parse_mode"] = "HTML"
    if markup:
        p["reply_markup"] = markup
    api(token, "sendMessage", p)


# ---------- stock queries (read-only) ----------
def _stock(c):
    locs = {r["id"]: (r["name"], r["role"], r["active"]) for r in c.execute("SELECT * FROM locations")}
    st = {}
    for r in c.execute("SELECT variant_id, location_id, SUM(qty) q FROM movements GROUP BY variant_id, location_id"):
        if r["q"]:
            st.setdefault(r["variant_id"], {})[r["location_id"]] = r["q"]
    return locs, st


def local_day(ts):
    """Movement timestamp (UTC ISO) -> local date as dd/mm/yy."""
    try:
        d = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        now = now_local()
        d = d.astimezone(now.tzinfo)
    except Exception:
        return "-"
    return d.strftime("%d/%m/%y")


def _last(c):
    """variant_id -> timestamp of its most recent movement of any kind (receive, sale, adjust, transfer, recount)."""
    return {r["variant_id"]: r["t"] for r in c.execute("SELECT variant_id, MAX(ts) t FROM movements WHERE type<>'move' GROUP BY variant_id")}


def refill(c):
    """Variants whose picking-warehouse stock is at or below their picking minimum, with what holding has."""
    locs, st = _stock(c)
    out = []
    first_pick = min((l for l, x in locs.items() if x[1] == "picking" and x[2]), default=None)
    if first_pick is None:  # nothing to refill *into*: every line would be listed with no destination
        return []
    q = """SELECT v.id, v.colour, v.size, v.pick_min, v.pick_max, p.name, p.pick_loc, p.group_by FROM variants v JOIN products p ON p.id=v.product_id
           WHERE v.pick_min>0 AND v.discontinued=0 AND p.pending=0 ORDER BY p.name, v.colour, v.size"""
    for v in c.execute(q):
        by = st.get(v["id"], {})
        pick = sum(n for l, n in by.items() if locs[l][1] == "picking" and locs[l][2])
        if pick > v["pick_min"]:
            continue
        hold = [(locs[l][0], n) for l, n in by.items() if locs[l][1] == "holding" and locs[l][2] and n > 0]
        here = sorted(((n, -l) for l, n in by.items() if locs[l][1] == "picking" and locs[l][2] and n > 0), reverse=True)
        home = v["pick_loc"] if v["pick_loc"] in locs and locs[v["pick_loc"]][1] == "picking" and locs[v["pick_loc"]][2] else None
        dest = locs[home][0] if home else locs[-here[0][1]][0] if here else (locs[first_pick][0] if first_pick else "")
        mx = v["pick_max"] if v["pick_max"] > v["pick_min"] else 0  # a maximum not above the minimum counts as no maximum
        need = max(mx - pick, 0) if mx else 0
        out.append({"name": v["name"], "colour": v["colour"], "size": v["size"], "pick": pick,
                    "min": v["pick_min"], "max": mx, "need": need, "dest": dest,
                    "bring": min(need, sum(q for _, q in hold)), "holding": hold, "grp": v["group_by"]})
    out.sort(key=lambda x: (_natural(x["name"]), size_key(x["size"]), _natural(x["colour"])) if x["grp"] == "size"
             else (_natural(x["name"]), _natural(x["colour"]), size_key(x["size"])))
    return out


def now_local():
    try:
        zone = ZoneInfo(os.environ.get("TZ") or "UTC")
    except Exception:
        zone = ZoneInfo("UTC")
    return datetime.datetime.now(zone)


def request_lines(c, default_dest=None):
    """What to carry from holding to picking, lot by lot (blank lot first). Same rule as refill(): picking stock <= minimum,
    fill up to the maximum (qty None when no maximum is set). Also returns lines holding can't supply at all."""
    locs, st = _stock(c)
    lots = {}
    for r in c.execute("SELECT variant_id v, location_id l, lot, SUM(qty) q FROM movements GROUP BY variant_id, location_id, lot HAVING q>0"):
        lots.setdefault((r["v"], r["l"]), []).append((r["lot"], r["q"]))
    moves, short = [], []
    q = """SELECT v.id, v.colour, v.size, v.pick_min, v.pick_max, p.name, p.pick_loc, p.group_by FROM variants v JOIN products p ON p.id=v.product_id
           WHERE v.pick_min>0 AND v.discontinued=0 AND p.pending=0"""
    for v in c.execute(q):
        by = st.get(v["id"], {})
        pick = sum(n for l, n in by.items() if locs[l][1] == "picking" and locs[l][2])
        if pick > v["pick_min"]:
            continue
        here = sorted(((n, -l) for l, n in by.items() if locs[l][1] == "picking" and locs[l][2] and n > 0), reverse=True)
        home = v["pick_loc"] if v["pick_loc"] in locs and locs[v["pick_loc"]][1] == "picking" and locs[v["pick_loc"]][2] else None
        dest = locs[home][0] if home else locs[-here[0][1]][0] if here else (locs[default_dest][0] if default_dest in locs else "")
        base = {"name": v["name"], "colour": v["colour"], "size": v["size"], "dest": dest, "grp": v["group_by"]}
        hold = sorted(l for l, n in by.items() if locs[l][1] == "holding" and locs[l][2] and n > 0)
        if not hold:
            short.append(dict(base, pick=pick, min=v["pick_min"]))
            continue
        mx = v["pick_max"] if v["pick_max"] > v["pick_min"] else 0  # same rule as refill()
        left = max(mx - pick, 0) if mx else None
        for l in hold:
            if left is not None and left <= 0:
                break
            if left is None:  # no maximum set: list what's there, quantity to be written in by hand
                moves.append(dict(base, src=locs[l][0], lot="", qty=None))
                continue
            take = min(left, by[l])
            left -= take
            for lot, n in sorted(lots.get((v["id"], l), []), key=lambda x: (x[0] != "", _natural(x[0]))):
                if take <= 0:
                    break
                t = min(take, n)
                moves.append(dict(base, src=locs[l][0], lot=lot, qty=t))
                take -= t
    key = lambda x: (_natural(x["name"]), _natural(x["colour"]), size_key(x["size"]))
    moves.sort(key=lambda x: (_natural(x["dest"]), _natural(x["src"])) + key(x) + (_natural(x["lot"]),))
    short.sort(key=key)
    return moves, short


NO_PICKING = "No active picking warehouse: mark one as Picking on the Warehouses tab first (or restore an archived one)."


def has_picking(c):
    return bool(c.execute("SELECT 1 FROM locations WHERE role='picking' AND active=1").fetchone())


def refill_blocks(items):
    """Refill list as Telegram-HTML blocks: one header + table per item/colour/destination (sizes already in order)."""
    if not items:
        return ["Picking warehouses are fine. Nothing needs refilling."]
    groups = {}
    for x in items:
        sw = x.get("grp") == "size"  # items grouped size-then-colour get one table per size, a row per colour
        groups.setdefault((x["name"], x["size"] if sw else x["colour"], x["dest"], sw), []).append(x)
    e = lambda t: _h.escape(str(t), quote=False)
    blocks = ["<b>Pending transfers to picking</b> (%d line%s)" % (len(items), "" if len(items) == 1 else "s")]
    for (name, colour, dest, sw), xs in groups.items():
        rows = [((x["colour"] if sw else x["size"]) or "-", x["pick"],
                 x["bring"] if x["need"] and x["holding"] else "-",
                 sum(q for _, q in x["holding"])) for x in xs]
        src = sorted({n for x in xs for n, _ in x["holding"]}, key=str.lower)
        head = "<b>%s</b>%s" % (e(name), " · %s" % e(colour) if colour else "")
        if any(x["need"] and x["holding"] for x in xs):
            head += "\n→ into %s" % e(dest)
            if src:
                head += " · from %s" % e(", ".join(src))
        notes = ""
        if any(not x["holding"] for x in xs):
            notes = "\n- = nothing in holding, needs reordering"
        elif any(x["bring"] < x["need"] for x in xs if x["holding"]):
            notes = "\nBring is capped at what holding has"
        blocks.append(head + "\n<pre>" + e(_table(("Colour" if sw else "Size", "Pick", "Bring", "Hold"), rows)) + "</pre>" + e(notes))
    return blocks


def refill_text(items):  # plain-text view (tests / fallback)
    return re.sub(r"<[^>]+>", "", _h.unescape("\n\n".join(refill_blocks(items))))


_BED = ["cot", "crib", "single", "single xl", "twin", "twin xl", "small double", "three quarter", "double", "full", "queen",
        "king", "super king", "emperor"]
_WORDS = {"small": "s", "medium": "m", "med": "m", "large": "l", "extra small": "xs", "extra large": "xl"}


def _garment(t):
    """XXS..XS, S, M, L, XL..5XL in any spelling (XXXL = 3XL, XXXXXL = 5XL); None if it isn't one."""
    t = _WORDS.get(t, t).replace(" ", "")
    m = re.fullmatch(r"(\d*)(x*)([sml])", t)
    if not m:
        return None
    d, xs, base = m.groups()
    if d and xs != "x":
        return None
    n = int(d) if d else len(xs)
    if base == "m":
        return 1 if n == 0 else None
    return 2 + n if base == "l" else -n


def size_key(s):
    """Sort key for sizes: numbers and measurements by number (4, 6, 10; 50x90 before 70x140), then garment sizes
    XXS..5XL, then bed sizes Single..Super King, then anything else alphabetically; blank last."""
    s = (s or "").strip()
    if not s:
        return (4, 0, 0, "")
    # '.' is a separator like a space, '-' or '_' (e.g. "x.l"), except between two digits, where it is a decimal point:
    # 70x140 < 70.5x140 and 1.25 < 1.5 compare as numbers
    t = re.sub(r"[\s\-_]+", " ", re.sub(r"(?<![0-9])\.|\.(?![0-9])", " ", s.lower())).strip()
    g = _garment(t)
    if g is not None:
        return (1, g, 0, t)
    if t in _BED or t.replace(" ", "") in [b.replace(" ", "") for b in _BED]:
        return (2, [b.replace(" ", "") for b in _BED].index(t.replace(" ", "")), 0, t)
    m = re.match(r"^(\d+(?:\.\d+)?)[^\dx×*]*(?:[x×*]\s*(\d+(?:\.\d+)?))?", t)
    if m:
        return (0, float(m.group(1)), float(m.group(2) or 0), t)
    return (3, 0, 0, t)


def _natural(s):
    return [int(t) if i % 2 else t for i, t in enumerate(re.split(r"([0-9]+)", (s or "").lower()))]  # ASCII digits only


def _short_names(names):
    """Short, unique column headers: 'Main: Ground' -> 'Groun', 'Shop Upstairs' -> 'Shop'."""
    out = []
    for n in names:
        w = (n.split(":")[-1].strip().split() or [n])[0]
        out.append(w[:5])
    if len(set(out)) != len(out):
        out = [n.split(":")[-1].replace(" ", "")[:5] for n in names]
    if len(set(out)) != len(out):
        out = ["%s%d" % (o[:4], k + 1) for k, o in enumerate(out)]
    return out


def _table(head, rows):
    w = [max(len(str(r[k])) for r in [head] + rows) for k in range(len(head))]
    fmt = lambda r: "  ".join(str(r[0]).ljust(w[0]) if k == 0 else str(v).rjust(w[k]) for k, v in enumerate(r))
    return "\n".join([fmt(head)] + [fmt(r) for r in rows])


def lookup_blocks(c, query, pid=None):
    """Stock lookup as Telegram-HTML blocks: per item a header with its total, then one small table per colour (or per size) with totals only.
    Warehouse breakdown is in the PDF ("/stock <words> pdf")."""
    words = [w for w in query.lower().split() if w]
    if not words and pid is None:
        return ["Send part of an item name, colour or size, e.g. 'viscose double'."]
    locs, st = _stock(c)
    last = _last(c)
    prods = {}
    for v in c.execute("""SELECT v.id, v.colour, v.size, p.id pid, p.name, p.alias, p.group_by, v.discontinued FROM variants v
                          JOIN products p ON p.id=v.product_id WHERE p.pending=0"""):
        hay = " ".join((v["name"], v["alias"], v["colour"], v["size"])).lower()
        if (v["pid"] == pid) if pid is not None else match_words(words, hay):
            prods.setdefault(v["name"], []).append(v)
    if not prods:
        return ["Nothing found for '%s'." % _h.escape(query, quote=False)]
    blocks = []
    for name in sorted(prods, key=_natural):
        vs = prods[name]
        total = sum(sum(st.get(v["id"], {}).values()) for v in vs)
        head = "<b>%s</b>: %d ctn total" % (_h.escape(name, quote=False), total)
        first = True
        sw = vs[0]["group_by"] == "size"  # outer group is the size, rows are colours
        OF, IF = ("size", "colour") if sw else ("colour", "size")
        for col in sorted({v[OF] for v in vs}, key=size_key if sw else _natural):
            cv = sorted((v for v in vs if v[OF] == col), key=(lambda v: _natural(v["colour"])) if sw else (lambda v: size_key(v["size"])))
            rows = []
            for v in cv:
                rows.append([(v[IF] or "n/a") + (" (disc.)" if v["discontinued"] else ""), sum(st.get(v["id"], {}).values()), local_day(last.get(v["id"]))])
            if len(rows) > 1:
                rows.append(["Total", sum(r[1] for r in rows), ""])
            tbl = "<b>%s</b>\n<pre>%s</pre>" % (_h.escape(col or "n/a", quote=False), _h.escape(_table(["Colour" if sw else "Size", "Tot", "Upd"], rows), quote=False))
            blocks.append((head + "\n" if first else "") + tbl)
            first = False
    blocks.append("<i>Warehouse breakdown: add pdf, e.g. /stock %s pdf</i>" % _h.escape(query.strip() if pid is None else next(iter(prods)), quote=False))
    return blocks


# ---------- "did you mean" ----------
def _toks(s):
    out = []
    for w in re.split(r"[^a-z0-9]+", (s or "").lower()):
        if w:
            out.append(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)  # boys -> boy, shirts -> shirt
    return out


def _word_score(q, toks):
    best = 0.0
    for t in toks:
        if q == t:
            return 1.0
        s = 0.9 if len(q) >= 3 and t.startswith(q) else difflib.SequenceMatcher(None, q, t).ratio()
        best = max(best, s)
    return best


def suggest(c, query, n=3, cutoff=0.72):
    """Items whose name roughly matches the words (typos, plurals, 'long sleeve' vs 'longsleeve', missing words).
    Returns [(pid, name, score)], best first."""
    qt = _toks(query)
    if not qt:
        return []
    items = {}
    for r in c.execute("SELECT p.id, p.name, p.alias, v.colour, v.size FROM products p LEFT JOIN variants v ON v.product_id=p.id WHERE p.pending=0"):
        d = items.setdefault(r[0], {"name": r[1], "t": set(), "n": set()})
        base = _toks("%s %s" % (r[1], r[2] or ""))
        d["t"].update(base)
        d["n"].update(base)
        d["n"].update(a + b for a, b in zip(base, base[1:]))
        d["t"].update(a + b for a, b in zip(base, base[1:]))  # joined neighbours: "long sleeve" also matches "longsleeve"
        d["t"].update(_toks("%s %s" % (r[3] or "", r[4] or "")))
    qj = qt + [a + b for a, b in zip(qt, qt[1:])]
    out = []
    for pid, d in items.items():
        sc = sum(_word_score(q, d["t"]) for q in qt) / len(qt)
        sc = max(sc, sum(_word_score(q, d["t"]) for q in qj) / len(qj))
        if sc >= cutoff and max(_word_score(q, d["n"]) for q in qj) >= 0.75:
            out.append((pid, d["name"], sc))
    out.sort(key=lambda x: (-x[2], _natural(x[1])))
    if out:
        out = [x for x in out if x[2] >= out[0][2] - 0.12]
    return out[:n]


def did_you_mean(c, query, want_pdf=False):
    """When nothing matches exactly: a message with buttons to confirm the closest items, or None."""
    cands = suggest(c, query)
    if not cands:
        return None
    f = "1" if want_pdf else "0"
    q = _h.escape(query.strip(), quote=False)
    if len(cands) == 1:
        pid, name, _ = cands[0]
        text = "Nothing called '%s'. Did you mean <b>%s</b>?" % (q, _h.escape(name, quote=False))
        kb = [[{"text": "Yes, show it", "callback_data": "sk:%d:%s" % (pid, f)}, {"text": "No", "callback_data": "sk:0:0"}]]
    else:
        text = "Nothing called '%s'. Did you mean one of these?" % q
        kb = [[{"text": name[:60], "callback_data": "sk:%d:%s" % (pid, f)}] for pid, name, _ in cands] + [[{"text": "None of these", "callback_data": "sk:0:0"}]]
    return {"text": text, "markup": {"inline_keyboard": kb}}


def _no_hits(c, query):
    words = [w for w in query.lower().split() if w]
    if not words:
        return False
    for v in c.execute("SELECT p.name, p.alias, v.colour, v.size FROM variants v JOIN products p ON p.id=v.product_id WHERE p.pending=0"):
        if match_words(words, " ".join(x or "" for x in v)):
            return False
    return True


def pack(blocks, limit=3500, max_msgs=6):
    """Group blocks into messages under Telegram's size limit; never splits a table."""
    msgs, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) + 2 > limit:
            msgs.append(cur)
            cur = ""
        cur = (cur + "\n\n" if cur else "") + b
    if cur:
        msgs.append(cur)
    if len(msgs) > max_msgs:
        msgs = msgs[:max_msgs] + ["Too many results to show. Add a colour, size or more of the name to narrow it down."]
    return msgs


def lookup_text(c, query):  # plain-text view of the same data (used by tests and as a fallback)
    return re.sub(r"<[^>]+>", "", _h.unescape("\n\n".join(lookup_blocks(c, query))))


def match_words(words, hay):
    """Every word must appear. Short letter words (S, M, XL) must be a whole word, so 'polo s' doesn't match 'shirt';
    short numbers must start a word, so '50' finds 50x90 but not 150x200."""
    hay = hay.lower()
    toks = [t for t in re.split(r"[^a-z0-9]+", hay) if t]
    for w in words:
        if re.fullmatch(r"[a-z]{1,2}", w):
            if w not in toks:
                return False
        elif re.fullmatch(r"\d{1,3}", w):
            if not any(t.startswith(w) for t in toks):
                return False
        elif w not in hay:
            return False
    return True


# ---------- message handling ----------
def reply_for(text, bot=""):
    """Returns (list of messages, is_html) for an authorised message, or None to stay silent."""
    t = text.strip()
    first = t.split(" ", 1)[0]
    if "@" in first and first.startswith("/") and first.split("@", 1)[1].lower() != (bot or "").lower():
        return None  # a command addressed to another bot
    if bot:
        t = re.sub(re.escape("@" + bot), "", t, flags=re.I).strip()
    cmd, _, rest = t.partition(" ")
    cmd = cmd.lower().split("@")[0]
    c = db.connect()
    try:
        if cmd in ("/start", "/help"):
            return ["Ask me about stock: send part of an item name (e.g. 'viscose double'), or /stock <words>. Add 'pdf' at the end (/stock <words> pdf) to get the printable PDF too. Not sure of the exact name? Send what you remember and I'll suggest the closest items to confirm. /low shows what picking needs refilled."], False
        if cmd == "/low":
            return pack(refill_blocks(refill(c)) if has_picking(c) else [NO_PICKING]), True
        if cmd == "/stock":
            q = pdf_query(t)
            query = rest if q is None else q
        elif t.startswith("/"):
            return None
        else:
            q, query = None, t
        if _no_hits(c, query):
            m = did_you_mean(c, query, q is not None)
            if m:
                return [m], True
        return pack(lookup_blocks(c, query)), True
    finally:
        c.close()


def pdf_query(text, bot=""):
    """'/stock viscose double pdf' -> 'viscose double'; None when the message isn't a /stock ... pdf request."""
    t = text.strip()
    if bot:
        t = re.sub(re.escape("@" + bot), "", t, flags=re.I).strip()
    cmd, _, rest = t.partition(" ")
    if "@" in cmd and cmd.split("@", 1)[1].lower() != (bot or "").lower():
        return None
    if cmd.lower().split("@")[0] != "/stock":
        return None
    w = rest.split()
    if w and w[-1].lower() == "pdf":
        return " ".join(w[:-1])
    return None


def item_pdfs(query, limit=3, pid=None):
    """Full stock PDF (same as the app's export with default options) for the items matching the words.
    Returns ([(file name, bytes, item name)], extra_count). An item whose PDF can't be made comes back as
    (reason, None, item name), so the others are still sent and the chat is told which one failed."""
    from . import main  # late import: main imports this module
    words = [w for w in query.lower().split() if w]
    if not words and pid is None:
        return [], 0
    c = db.connect()
    hit = {}
    for v in c.execute("SELECT p.id pid, p.name, p.alias, p.group_by, v.colour, v.size FROM variants v JOIN products p ON p.id=v.product_id WHERE p.pending=0"):
        if (v["pid"] == pid) if pid is not None else match_words(words, " ".join((v["name"], v["alias"], v["colour"], v["size"]))):
            hit[v["pid"]] = (v["name"], v["group_by"])
    c.close()
    order = sorted(hit.items(), key=lambda kv: _natural(kv[1][0]))
    out = []
    for pid, (name, grp) in order[:limit]:
        try:
            r = main.export(pid, split="size" if grp == "size" else "")  # same default as the app: size-first items get a table per size
        except Exception as e:
            out.append((getattr(e, "detail", None) or "the PDF could not be made (%s)" % type(e).__name__, None, name))
            continue
        m = re.search(r'filename="([^"]+)"', r.headers.get("content-disposition", ""))
        out.append((m.group(1) if m else "stock.pdf", r.body, name))
    return out, max(len(order) - limit, 0)


def send_item_pdfs(token, chat, docs):
    for fn, data, name in docs:
        if data is None:
            send(token, chat, "No PDF for %s: %s" % (name, fn))
        else:
            send_document(token, chat, fn, data, name)


def _chat_ok(chat, user):
    if user not in allowed_users():
        return False
    return chat.get("type") == "private" or str(chat.get("id")) == setting("tg_group").strip()


def handle_button(cq, token):
    """A tap on a 'did you mean' button: confirm the item, then show its stock (and PDF if it was asked for)."""
    try:
        api(token, "answerCallbackQuery", {"callback_query_id": cq["id"]})
    except Exception:
        pass
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    if not _chat_ok(chat, (cq.get("from") or {}).get("id")):
        return
    m = re.match(r"^sk:(\d+):([01])$", cq.get("data") or "")
    if not m:
        return
    pid, want_pdf = int(m.group(1)), m.group(2) == "1"
    c = db.connect()
    try:
        row = c.execute("SELECT name FROM products WHERE id=? AND pending=0", (pid,)).fetchone() if pid else None
        note = ("Showing <b>%s</b>" % _h.escape(row[0], quote=False)) if row else ("OK, nothing shown. Try other words." if not pid else "That item no longer exists.")
        try:
            api(token, "editMessageText", {"chat_id": chat["id"], "message_id": msg.get("message_id"), "text": note, "parse_mode": "HTML"})  # also removes the buttons
        except Exception:
            pass
        if not row:
            return
        for t in pack(lookup_blocks(c, "", pid)):
            send(token, chat["id"], t, html=True)
    finally:
        c.close()
    if want_pdf:
        send_item_pdfs(token, chat["id"], item_pdfs("", pid=pid)[0])


def handle(update, token):
    if update.get("callback_query"):
        return handle_button(update["callback_query"], token)
    m = update.get("message") or {}
    text = m.get("text") or ""
    if not text:
        return
    chat, user = m["chat"], (m.get("from") or {}).get("id")
    if text.strip().lower().split("@")[0].split(" ")[0] == "/id":  # anyone may ask, so you can find IDs to enter in Settings
        send(token, chat["id"], "Your user ID: %s\nThis chat ID: %s" % (user, chat["id"]))
        return
    if user not in allowed_users():
        return
    bot = STATE.get("bot", "")
    if chat.get("type") == "private":
        pass
    elif str(chat["id"]) == setting("tg_group").strip():
        if not text.startswith("/") and not (bot and "@" + bot.lower() in text.lower()):
            return  # in the group, plain chatter is ignored; commands and @mentions are answered
    else:
        return
    r = reply_for(text, bot)
    if r:
        for m in r[0]:
            if isinstance(m, dict):
                send(token, chat["id"], m["text"], html=r[1], markup=m["markup"])
            else:
                send(token, chat["id"], m, html=r[1])
    q = pdf_query(text, bot)
    if q is not None:
        docs, extra = item_pdfs(q) if q.strip() else ([], 0)
        send_item_pdfs(token, chat["id"], docs)
        if extra:
            send(token, chat["id"], "%d more item%s matched. Add more words to narrow it down." % (extra, "" if extra == 1 else "s"))


def stock_request_pdf(dest=0):
    """Printable list of what to carry from holding to picking. Returns (pdf bytes, filename); LookupError('code|message') if there is nothing to print."""
    import io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    c = db.connect()
    picks = [r for r in c.execute("SELECT * FROM locations WHERE role='picking' AND active=1 ORDER BY id")]
    if not picks:
        c.close()
        raise LookupError("422|" + NO_PICKING)
    d = ([r for r in picks if r["id"] == dest] or picks)[0]
    moves, short = request_lines(c, d["id"])
    c.close()
    if not moves and not short:
        raise LookupError("404|Nothing needs to be moved right now")
    ss = getSampleStyleSheet()
    cell = ss["BodyText"].clone("cell", fontSize=9, leading=11)
    esc = lambda t: (t or "-").replace("&", "&amp;").replace("<", "&lt;")
    now = now_local()
    els = [Paragraph("Stock request", ss["Title"]),
           Paragraph("<b>Date:</b> %s &nbsp;&nbsp; Quantities in cartons. Each item goes into the picking warehouse that already holds it."
                     % now.strftime("%Y-%m-%d %H:%M"), ss["Normal"]), Spacer(1, 12)]
    style = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3c3c3c")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
             ("FONTSIZE", (0, 0), (-1, -1), 9), ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]

    def split(xs):
        """One table per item and per size (items grouped size-first) or per colour (the default), in a stable order.
        Rows inside are the other dimension: colours for a size table, sizes for a colour table."""
        out = {}
        for x in xs:
            sw = x.get("grp") == "size"
            out.setdefault((x["name"], x["size"] if sw else x["colour"], sw), []).append(x)
        return dict(sorted(out.items(), key=lambda kv: (_natural(kv[0][0]), size_key(kv[0][1]) if kv[0][2] else _natural(kv[0][1]))))

    def title(name, g):
        return Paragraph("<b>%s</b>%s" % (esc(name), " &middot; %s" % esc(g) if g and g != "-" else ""), ss["Heading4"])

    for dst, src in dict.fromkeys((m["dest"], m["src"]) for m in moves):
        ms = [m for m in moves if m["src"] == src and m["dest"] == dst]
        lead = [Paragraph("From: <b>%s</b> &nbsp;&rarr;&nbsp; Into: <b>%s</b>" % (esc(src), esc(dst)), ss["Heading3"])]
        for (name, g, sw), xs in split(ms).items():
            lot = any(m["lot"] for m in xs)
            rows = [["Colour" if sw else "Size"] + (["Lot"] if lot else []) + ["Ctns", "Picked"]]
            for m in xs:
                rows.append([Paragraph(esc(m["colour"] if sw else m["size"]), cell)] + ([Paragraph(esc(m["lot"]), cell)] if lot else [])
                            + ["" if m["qty"] is None else m["qty"], ""])
            tot = sum(m["qty"] or 0 for m in xs)
            rows.append(["Total"] + [""] * lot + [tot or "", ""])
            n = 2 + lot
            # long colours, sizes and lots wrap inside their cell and the row grows to fit (never printed over the
            # quantity); the padding keeps room to write in the Picked column
            t = Table(rows, colWidths=[150] + [90] * lot + [60, 80], repeatRows=1, hAlign="LEFT")
            t.setStyle(TableStyle(style + [("ALIGN", (n - 1, 0), (n - 1, -1), "RIGHT"), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                                           ("TOPPADDING", (0, 1), (-1, -1), 6), ("BOTTOMPADDING", (0, 1), (-1, -1), 6)]))
            # heading + table stay on one page; a table longer than a page still flows over (header row repeats)
            els += [KeepTogether(lead + [title(name, g), t]), Spacer(1, 10)]
            lead = []
    if any(m["qty"] is None for m in moves):
        els.append(Paragraph("Blank quantities: no picking maximum is set for those lines, write in how many you moved.", ss["Italic"]))
    if short:
        els.append(Spacer(1, 8))
        lead = [Paragraph("Not in any holding warehouse (needs reordering)", ss["Heading3"])]
        for (name, g, sw), xs in split(short).items():
            rows = [["Colour" if sw else "Size", "In picking", "Min"]]
            for x in xs:
                rows.append([Paragraph(esc(x["colour"] if sw else x["size"]), cell), x["pick"], x["min"]])
            t = Table(rows, colWidths=[150, 70, 60], repeatRows=1, hAlign="LEFT")
            t.setStyle(TableStyle(style + [("ALIGN", (1, 0), (-1, -1), "RIGHT")]))
            els += [KeepTogether(lead + [title(name, g), t]), Spacer(1, 10)]
            lead = []
    els += [Spacer(1, 24), Paragraph("Prepared by: _______________ &nbsp; Moved by: _______________ &nbsp; Checked: _______________", ss["Normal"])]
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, title="Stock request", topMargin=36, bottomMargin=36).build(els)
    return buf.getvalue(), "stock-request-%s.pdf" % now.strftime("%Y-%m-%d")


def send_document(token, chat, name, data, caption=""):
    b = "----stock%d" % int(time.time() * 1000)
    parts = []
    for k, v in (('chat_id', str(chat)), ('caption', caption)):
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v)).encode())
    parts.append(('--%s\r\nContent-Disposition: form-data; name="document"; filename="%s"\r\nContent-Type: application/pdf\r\n\r\n' % (b, name)).encode() + data + b'\r\n')
    parts.append(('--%s--\r\n' % b).encode())
    req = urllib.request.Request('%s/bot%s/sendDocument' % (BASE, token), b''.join(parts), {'Content-Type': 'multipart/form-data; boundary=' + b})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
    except Exception as e:
        raise RuntimeError(str(e).replace(token, '<token>'))
    if not d.get('ok'):
        raise RuntimeError(str(d.get('description', 'Telegram error')).replace(token, '<token>'))


def post_refill(token=None):
    """Post the refill list to the group. Returns number of lines posted (0 = nothing needed, nothing sent)."""
    token = token or bot_token()
    group = setting("tg_group").strip()
    if not token or not group:
        raise RuntimeError("Set the bot token and the group chat ID first")
    if not is_chat_id(group):
        raise RuntimeError("The group chat ID is not a number: fix it in Settings")
    c = db.connect()
    try:
        if not has_picking(c):
            STATE["post_err"] = "Refill list not posted: " + NO_PICKING
            raise RuntimeError(NO_PICKING)
        items = refill(c)
    finally:
        c.close()
    if not items:
        STATE["post_err"] = ""  # nothing needed, nothing sent: an old problem is no longer current
    if items:
        try:
            for m in pack(refill_blocks(items)):
                send(token, int(group), m, html=True)
        except Exception as e:
            STATE["post_err"] = "Refill list not posted: %s" % e
            raise
        try:
            data, name = stock_request_pdf()
            send_document(token, int(group), name, data, "Stock request (printable)")
            STATE["post_err"] = ""
        except Exception as e:  # the text list already went out; don't fail the whole post over the PDF
            msg = str(e.args[0]) if isinstance(e, LookupError) and e.args else str(e)
            STATE["post_err"] = "PDF not sent: %s" % msg.split("|", 1)[-1]  # LookupError carries 'code|message'; show the message
    return len(items)


def maybe_digest(now=None):
    h = setting("tg_hour").strip()
    if not re.fullmatch(r"[0-9]{1,2}", h) or not setting("tg_group").strip() or not bot_token():
        return
    now = now or now_local()
    today = now.strftime("%Y-%m-%d")
    if now.hour < int(h) or setting("tg_last_digest") == today:
        return
    put("tg_last_digest", today)  # marked first so a failure can't cause a flood of retries
    try:
        post_refill()
    except RuntimeError:
        pass  # already recorded in STATE["post_err"] for Settings; the bot itself is fine


def _loop():
    offset, last = 0, None
    while not _stop.is_set():
        token = bot_token()
        if token != last:  # a different bot has its own update numbers; an old offset would silently drop its messages
            offset, last = 0, token
        if not token:
            STATE.update(ok=False, bot="", err="")
            _stop.wait(2)
            continue
        try:
            if not STATE["bot"]:
                STATE["bot"] = api(token, "getMe").get("username", "")
            ups = api(token, "getUpdates", {"offset": offset, "timeout": 10, "allowed_updates": ["message", "callback_query"]}, timeout=20)
            STATE.update(ok=True, err="")
            for u in ups:
                offset = u["update_id"] + 1
                try:
                    handle(u, token)
                except Exception as e:
                    STATE["err"] = str(e).replace(token, "<token>")
            maybe_digest()
        except Exception as e:
            STATE.update(ok=False, err=str(e).replace(token, "<token>"))
            _stop.wait(10)


def start():
    global _thread
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="telegram")
    _thread.start()


def stop():
    _stop.set()
