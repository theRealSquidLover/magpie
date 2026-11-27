"""SQLite storage: schema, versioned migrations, automatic backups."""
import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time

from .creds import SECRET_SETTINGS

DATA = os.environ.get("DATA_DIR", "/data")
DB = os.path.join(DATA, "stock.db")
KEEP_DAILY, KEEP_PRE = 14, 5

# Append new migrations to the END of this list. Never edit an existing one.
MIGRATIONS = [
    """
CREATE TABLE locations(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  alias TEXT NOT NULL DEFAULT '', ppc INTEGER NOT NULL DEFAULT 0, photo TEXT);
CREATE TABLE variants(id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
  colour TEXT NOT NULL DEFAULT '' COLLATE NOCASE, size TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
  UNIQUE(product_id, colour, size));
CREATE TABLE movements(id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, ts TEXT NOT NULL,
  received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  variant_id INTEGER NOT NULL REFERENCES variants(id), lot TEXT NOT NULL DEFAULT '',
  location_id INTEGER NOT NULL REFERENCES locations(id), qty INTEGER NOT NULL, type TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '', transfer_id TEXT, user TEXT NOT NULL DEFAULT '', flag TEXT);
CREATE INDEX mv_key ON movements(variant_id, lot, location_id);
CREATE VIEW stock AS SELECT variant_id, lot, location_id, SUM(qty) AS qty
  FROM movements GROUP BY variant_id, lot, location_id;
INSERT INTO locations(name) VALUES ('Main: Ground'), ('Main: Basement'), ('Store'), ('Corner shelf');
""",
    "CREATE TABLE settings(k TEXT PRIMARY KEY, v TEXT NOT NULL);",
    "ALTER TABLE variants ADD COLUMN reorder_at INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE locations ADD COLUMN role TEXT NOT NULL DEFAULT 'holding';",
    "ALTER TABLE variants ADD COLUMN pick_min INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE variants ADD COLUMN pick_max INTEGER NOT NULL DEFAULT 0;",
    """
ALTER TABLE products ADD COLUMN pick_loc INTEGER REFERENCES locations(id);
UPDATE products SET pick_loc=(SELECT s.location_id FROM stock s JOIN variants v ON v.id=s.variant_id
  JOIN locations l ON l.id=s.location_id WHERE v.product_id=products.id AND l.role='picking' AND s.qty>0
  GROUP BY s.location_id ORDER BY SUM(s.qty) DESC, s.location_id LIMIT 1);
""",
    """
ALTER TABLE products ADD COLUMN group_by TEXT NOT NULL DEFAULT 'colour';
""",
    """
ALTER TABLE variants ADD COLUMN ppc INTEGER NOT NULL DEFAULT 0;
ALTER TABLE variants ADD COLUMN discontinued INTEGER NOT NULL DEFAULT 0;
""",
    """
ALTER TABLE products ADD COLUMN category TEXT NOT NULL DEFAULT '';
""",
    """
ALTER TABLE products ADD COLUMN pack TEXT NOT NULL DEFAULT '';
ALTER TABLE variants ADD COLUMN pack TEXT NOT NULL DEFAULT '';
""",
    """
ALTER TABLE locations ADD COLUMN has_sections INTEGER NOT NULL DEFAULT 0;
ALTER TABLE locations ADD COLUMN map TEXT NOT NULL DEFAULT '';
CREATE TABLE sections(id INTEGER PRIMARY KEY, location_id INTEGER NOT NULL REFERENCES locations(id),
  name TEXT NOT NULL COLLATE NOCASE, x REAL NOT NULL DEFAULT 0, y REAL NOT NULL DEFAULT 0,
  w REAL NOT NULL DEFAULT 0, h REAL NOT NULL DEFAULT 0, UNIQUE(location_id, name));
CREATE TABLE homes(product_id INTEGER NOT NULL REFERENCES products(id), location_id INTEGER NOT NULL REFERENCES locations(id),
  section_id INTEGER NOT NULL REFERENCES sections(id), PRIMARY KEY(product_id, location_id));
ALTER TABLE movements ADD COLUMN section_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE movements ADD COLUMN arrived TEXT;
""",
    """
ALTER TABLE products ADD COLUMN colour_photos INTEGER NOT NULL DEFAULT 0;
CREATE TABLE colour_photos(product_id INTEGER NOT NULL REFERENCES products(id), colour TEXT NOT NULL COLLATE NOCASE,
  photo TEXT NOT NULL, PRIMARY KEY(product_id, colour));
""",
    "ALTER TABLE products ADD COLUMN pending INTEGER NOT NULL DEFAULT 0;",
    "CREATE TABLE op_log(uuid TEXT PRIMARY KEY, ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);",
    """
ALTER TABLE movements ADD COLUMN reverses TEXT;
CREATE INDEX mv_rev ON movements(reverses);
""",
    # 17: warehouse and section ids are never handed out again after a delete (AUTOINCREMENT). Without it a new section
    # (or warehouse) could get the id of a deleted one and inherit its old ledger rows, or an op queued offline for the
    # deleted one. SQLite can't add AUTOINCREMENT to a table, so both are rebuilt with the same columns and data.
    # The section counter starts above every section id the ledger has ever used, deleted ones included.
    """-- rebuild (runs with foreign keys off, checked before commit)
CREATE TABLE locations_new(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  active INTEGER NOT NULL DEFAULT 1, role TEXT NOT NULL DEFAULT 'holding', has_sections INTEGER NOT NULL DEFAULT 0,
  map TEXT NOT NULL DEFAULT '');
INSERT INTO locations_new(id,name,active,role,has_sections,map) SELECT id,name,active,role,has_sections,map FROM locations;
DROP TABLE locations;
ALTER TABLE locations_new RENAME TO locations;
CREATE TABLE sections_new(id INTEGER PRIMARY KEY AUTOINCREMENT, location_id INTEGER NOT NULL REFERENCES locations(id),
  name TEXT NOT NULL COLLATE NOCASE, x REAL NOT NULL DEFAULT 0, y REAL NOT NULL DEFAULT 0,
  w REAL NOT NULL DEFAULT 0, h REAL NOT NULL DEFAULT 0, UNIQUE(location_id, name));
INSERT INTO sections_new(id,location_id,name,x,y,w,h) SELECT id,location_id,name,x,y,w,h FROM sections;
DROP TABLE sections;
ALTER TABLE sections_new RENAME TO sections;
DELETE FROM sqlite_sequence WHERE name IN ('locations','sections');
INSERT INTO sqlite_sequence(name,seq) VALUES('locations', (SELECT COALESCE(MAX(id),0) FROM locations));
INSERT INTO sqlite_sequence(name,seq) VALUES('sections', MAX((SELECT COALESCE(MAX(id),0) FROM sections),
  (SELECT COALESCE(MAX(section_id),0) FROM movements), (SELECT COALESCE(MAX(section_id),0) FROM homes)));
""",
]
# A migration that starts with this marker rebuilds tables, which SQLite only allows with foreign keys switched off
# (otherwise dropping the old table would cascade). It runs inside one transaction like the others, and every
# foreign key is checked before it commits: a broken reference makes it fail and roll back instead.
REBUILD = "-- rebuild"


def connect():
    c = sqlite3.connect(DB, isolation_level=None, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def app_version():
    """The build this server runs (the git commit, stamped into the image), or "dev"."""
    return os.environ.get("APP_VERSION", "dev") or "dev"


def short_version(v):
    """A version as it goes into a file name: the first 7 letters/digits, which is also the image tag `sha-<this>`."""
    v = re.sub(r"[^A-Za-z0-9]", "", v or "")[:7]
    return v or "unknown"


def _stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def backup_name(kind, schema, version):
    """daily-20261127-073012-schema19-app-f5b0216.db: when it was made, the database schema inside it, and the app
    build that wrote it (roll back to the image tagged sha-f5b0216 to run that exact version again)."""
    return "%s-%s-schema%d-app-%s.db" % (kind, _stamp(), schema, short_version(version))


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _counts(path):
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        out = {}
        for t in ("products", "variants", "locations", "movements"):
            try:
                out[t] = c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            except sqlite3.Error:
                pass
        return out
    finally:
        c.close()


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _backup_to(c, path):
    dst = sqlite3.connect(path)
    c.backup(dst)
    dst.close()
    scrub(path)


def _snapshot(c, path, kind, schema, version):
    """Copy the database through connection c into path: written to a temporary file, scrubbed of secrets, checked
    with PRAGMA integrity_check, then moved into place, so a crash can never leave a half-written backup. Beside it
    goes path + ".json": when, which app build and schema, a SHA-256 of the file and row counts, which the restore
    command uses to refuse a damaged or altered copy."""
    tmp = path + ".tmp"
    _remove(tmp)  # left over from a crash: sqlite would open it as a broken database and every try that day would fail
    try:
        _backup_to(c, tmp)
        chk = sqlite3.connect(tmp)
        try:
            ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            chk.close()
        if ok != "ok":
            raise RuntimeError("backup failed its integrity check: " + str(ok))
        os.replace(tmp, path)
    except Exception:
        _remove(tmp)
        raise
    _write_json(path + ".json", {
        "file": os.path.basename(path), "kind": kind,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "schema": schema, "app_version": version, "image_tag": "sha-" + short_version(version),
        "sha256": _sha256(path), "bytes": os.path.getsize(path), "rows": _counts(path),
    })


def scrub(path):
    """Take the saved API key and bot token out of a backup copy, so a backup that gets copied somewhere never carries
    them. The file is rewritten (VACUUM) so the deleted text doesn't linger in its free pages, and it is left in
    rollback-journal mode: a single self-contained file, with no -wal beside it. Returns True if anything was removed."""
    d = sqlite3.connect(path, isolation_level=None)
    try:
        d.execute("PRAGMA journal_mode=DELETE")
        if not d.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
            return False
        q = ",".join("?" * len(SECRET_SETTINGS))
        if not d.execute("SELECT 1 FROM settings WHERE k IN (%s)" % q, SECRET_SETTINGS).fetchone():
            return False
        d.execute("PRAGMA secure_delete=ON")
        d.execute("DELETE FROM settings WHERE k IN (%s)" % q, SECRET_SETTINGS)
        d.execute("VACUUM")
        return True
    finally:
        d.close()


def scrub_old_backups():
    """Backups made by older versions still hold the key and token: clean them once (keeping each file's date,
    which decides which pre-update copies are kept). A file that can't be opened is left as it is."""
    for path in glob.glob(os.path.join(DATA, "backups", "*.db")):
        try:
            st = os.stat(path)
            if scrub(path):
                os.utime(path, (st.st_atime, st.st_mtime))
        except (OSError, sqlite3.Error) as e:
            print("magpie: couldn't clean secrets out of backup %s: %s" % (os.path.basename(path), e), flush=True)


def lock_down():
    """Only the app's own user may read or change its data: stock.db, backups, photos, maps (files 600, folders 700).
    New files get the same through the umask the app sets at start. A file the app doesn't own (or a read-only
    volume) is left as it is."""
    def fix(p, mode):
        try:
            if not os.path.islink(p) and (os.stat(p).st_mode & 0o777) != mode:
                os.chmod(p, mode)
        except OSError:
            pass
    fix(DATA, 0o700)
    for root, dirs, files in os.walk(DATA):
        for n in dirs:
            fix(os.path.join(root, n), 0o700)
        for n in files:
            fix(os.path.join(root, n), 0o600)


def migrate():
    os.makedirs(DATA, exist_ok=True)
    c = connect()
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    r = c.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
    cur = int(r[0]) if r else 0
    r = c.execute("SELECT v FROM meta WHERE k='app'").fetchone()
    prev = r[0] if r else ""  # the build that last ran on this data
    if cur > 0 and (cur < len(MIGRATIONS) or (prev and prev != app_version())):
        # existing data and a new schema or a new build: take a safety copy first, named after the build that wrote it
        d = os.path.join(DATA, "backups")
        os.makedirs(d, exist_ok=True)
        _snapshot(c, os.path.join(d, backup_name("pre-update", cur, prev)), "pre-update", cur, prev or "unknown")
    if cur < len(MIGRATIONS):
        for i in range(cur, len(MIGRATIONS)):
            rebuild = MIGRATIONS[i].startswith(REBUILD)
            if rebuild:
                c.execute("PRAGMA foreign_keys=OFF")  # has no effect inside a transaction, so it is set before BEGIN
            check = ("CREATE TEMP TABLE fk_ok(n INTEGER CHECK(n=0));\n"  # fails the script if any reference is broken
                     "INSERT INTO fk_ok SELECT COUNT(*) FROM pragma_foreign_key_check;\nDROP TABLE fk_ok;\n") if rebuild else ""
            try:
                c.executescript(
                    "BEGIN;\n%s\n%sINSERT OR REPLACE INTO meta VALUES('schema','%d');\nCOMMIT;" % (MIGRATIONS[i], check, i + 1)
                )
            except Exception:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                c.close()
                raise
            if rebuild:
                c.execute("PRAGMA foreign_keys=ON")
    if prev != app_version():
        c.execute("INSERT OR REPLACE INTO meta VALUES('app',?)", (app_version(),))
    c.close()


def _remove(path):
    """Delete a scratch database and any journal/WAL files sqlite left beside it (a stale journal would be replayed into a new file)."""
    for p in (path, path + "-journal", path + "-wal", path + "-shm"):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def _is_backup(n):
    return n.endswith(".db") and (n.startswith("daily-") or n.startswith("pre-"))


def prune(d, keep=None, keep_pre=None):
    """Keep the newest KEEP_DAILY daily copies and KEEP_PRE pre-update copies in folder d (with their .json).
    Daily names start with the date in both the old (daily-20261127.db) and new formats, so they sort by name;
    pre-update copies (old pre-v3-1700000000.db or new pre-update-...) sort by file date."""
    keep = KEEP_DAILY if keep is None else keep
    keep_pre = KEEP_PRE if keep_pre is None else keep_pre
    names = [n for n in os.listdir(d) if _is_backup(n)]
    daily = sorted((n for n in names if n.startswith("daily-")), key=lambda n: n[6:21])
    pre = sorted((n for n in names if n.startswith("pre-")), key=lambda n: os.path.getmtime(os.path.join(d, n)))
    for n in daily[:-keep] + pre[:-keep_pre]:
        for p in (n, n + ".json"):
            try:
                os.remove(os.path.join(d, p))
            except FileNotFoundError:
                pass


def mirror_dir():
    """BACKUP_MIRROR_DIR: a second folder, ideally on another disk or machine, that gets a copy of every backup plus
    the photos and maps. Empty means off."""
    return os.environ.get("BACKUP_MIRROR_DIR", "").strip()


def _copy_if_changed(src, dst):
    try:
        a, b = os.stat(src), os.stat(dst)
        if a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime):
            return False
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)
    return True


MIRROR_MARKER = ".magpie-mirror"


def mirror():
    """Copy backups, photos and maps to BACKUP_MIRROR_DIR. Only adds and updates: a photo deleted in the app stays in
    the mirror, and the mirror keeps its own 14 + 5 backups.
    The folder must hold the marker file MIRROR_MARKER, made once by hand on the real disk. When the disk isn't mounted,
    Docker creates the bind path as an empty folder (on Unraid in RAM), which exists but has no marker: nothing is
    written there. (Not refused: a folder that looks like the same device as the data folder. On Unraid every
    /mnt/user share reports one device, so appdata on the cache and a backup share on the array would look alike.)"""
    m = mirror_dir()
    if not m:
        return 0
    m = os.path.abspath(m)
    if not os.path.isdir(m):
        raise RuntimeError("mirror folder %s doesn't exist (is the disk or share mounted?)" % m)
    if os.path.commonpath([m, os.path.abspath(DATA)]) == os.path.abspath(DATA):
        raise RuntimeError("mirror folder %s is inside the data folder; point it at another disk" % m)
    if not os.path.isfile(os.path.join(m, MIRROR_MARKER)):
        raise RuntimeError("Mirror folder %s has no %s file - is the disk mounted? (If it is, create that empty file "
                           "once in the folder on the backup disk.)" % (m, MIRROR_MARKER))
    n = 0
    src = os.path.join(DATA, "backups")
    for name in os.listdir(src) if os.path.isdir(src) else []:
        if _is_backup(name.removesuffix(".json")) and os.path.isfile(os.path.join(src, name)):
            n += _copy_if_changed(os.path.join(src, name), os.path.join(m, "backups", name))
    if os.path.isdir(os.path.join(m, "backups")):
        prune(os.path.join(m, "backups"))
    for sub in ("photos", "maps"):
        for root, _, files in os.walk(os.path.join(DATA, sub)):
            for f in files:
                if f.endswith(".tmp"):
                    continue
                p = os.path.join(root, f)
                n += _copy_if_changed(p, os.path.join(m, os.path.relpath(p, DATA)))
    return n


def backup_daily(keep=None):
    """Today's copy of the database, made once a day (see _snapshot), then old copies pruned and, if set up, the
    mirror brought up to date. The result (or the error) is recorded for Settings."""
    d = os.path.join(DATA, "backups")
    try:
        os.makedirs(d, exist_ok=True)
        day = time.strftime("%Y%m%d")
        made = [n for n in os.listdir(d) if n.startswith("daily-" + day) and n.endswith(".db")]
        path = os.path.join(d, made[0]) if made else ""
        if not made:
            c = connect()
            try:
                schema = int(c.execute("SELECT v FROM meta WHERE k='schema'").fetchone()[0])
                path = os.path.join(d, backup_name("daily", schema, app_version()))
                _snapshot(c, path, "daily", schema, app_version())
            finally:
                c.close()
        prune(d, keep)
        _status(path, "")
    except Exception as e:
        print("magpie: backup failed:", e, flush=True)
        _status("", str(e))
        raise
    if mirror_dir():
        last = _setting("backup_mirror_last")
        if not made or _setting("backup_mirror_err") or not last.startswith(time.strftime("%Y-%m-%d")):
            try:
                mirror()
                _set("backup_mirror_last", time.strftime("%Y-%m-%d %H:%M"))
                _set("backup_mirror_err", "")
            except Exception as e:
                print("magpie: backup mirror failed:", e, flush=True)
                _set("backup_mirror_err", str(e))


def _setting(k):
    c = connect()
    try:
        return (c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone() or [""])[0]
    finally:
        c.close()


def _set(k, v):
    c = connect()
    try:
        c.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (k, v))
    finally:
        c.close()


def _status(path, err):
    c = connect()
    try:
        if path:
            c.execute("INSERT OR REPLACE INTO settings(k,v) VALUES('backup_last',?)", (time.strftime("%Y-%m-%d %H:%M"),))
        c.execute("INSERT OR REPLACE INTO settings(k,v) VALUES('backup_err',?)", (err,))
    finally:
        c.close()


def backup_status():
    c = connect()
    try:
        g = lambda k: (c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone() or [""])[0]
        return {"last": g("backup_last"), "error": g("backup_err"),
                "count": len(glob.glob(os.path.join(DATA, "backups", "daily-*.db"))),
                "mirror": bool(mirror_dir()), "mirror_last": g("backup_mirror_last"), "mirror_error": g("backup_mirror_err")}
    finally:
        c.close()

LOCK = ".magpie.lock"


def hold_lock():
    """Held for as long as the server runs, so the restore command can tell that the app is still using the database
    (it works across containers that share the data folder). Returns the open file, or None if it couldn't be taken
    (then a warning is printed and the server starts anyway); closing the file releases the lock."""
    try:
        os.makedirs(DATA, exist_ok=True)
        f = open(os.path.join(DATA, LOCK), "a")
    except OSError as e:
        print("magpie: couldn't create the lock file:", e, flush=True)
        return None
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        print("magpie: another server seems to be using %s already" % DATA, flush=True)
        return None
    return f


def app_running():
    try:
        f = open(os.path.join(DATA, LOCK), "a")
    except OSError:
        return False
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        f.close()
