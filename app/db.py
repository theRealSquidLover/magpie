"""SQLite storage: schema, versioned migrations, automatic backups."""
import glob
import os
import sqlite3
import time

DATA = os.environ.get("DATA_DIR", "/data")
DB = os.path.join(DATA, "stock.db")

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


def _backup_to(c, path):
    dst = sqlite3.connect(path)
    c.backup(dst)
    dst.close()


def migrate():
    os.makedirs(DATA, exist_ok=True)
    c = connect()
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    r = c.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
    cur = int(r[0]) if r else 0
    if cur < len(MIGRATIONS):
        if cur > 0:  # existing data: take a safety copy before changing anything
            os.makedirs(os.path.join(DATA, "backups"), exist_ok=True)
            _backup_to(c, os.path.join(DATA, "backups", "pre-v%d-%d.db" % (cur, int(time.time()))))
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
    c.close()


def _remove(path):
    """Delete a scratch database and any journal/WAL files sqlite left beside it (a stale journal would be replayed into a new file)."""
    for p in (path, path + "-journal", path + "-wal", path + "-shm"):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def backup_daily(keep=14):
    """Today's copy of the database, made once a day: written to a temporary file, checked, then moved into place,
    so a crash can never leave a half-written backup. The result (or the error) is recorded for Settings."""
    d = os.path.join(DATA, "backups")
    try:
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "daily-%s.db" % time.strftime("%Y%m%d"))
        if not os.path.exists(path):
            tmp = path + ".tmp"
            _remove(tmp)  # left over from a crash: sqlite would open it as a broken database and every try that day would fail
            try:
                c = connect()
                try:
                    _backup_to(c, tmp)
                finally:
                    c.close()
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
        for old in sorted(glob.glob(os.path.join(d, "daily-*.db")))[:-keep]:
            os.remove(old)
        for old in sorted(glob.glob(os.path.join(d, "pre-v*.db")), key=os.path.getmtime)[:-5]:  # keep the last 5 pre-update backups
            os.remove(old)
        _status(path, "")
    except Exception as e:
        print("stockapp: backup failed:", e, flush=True)
        _status("", str(e))
        raise


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
                "count": len(glob.glob(os.path.join(DATA, "backups", "daily-*.db")))}
    finally:
        c.close()