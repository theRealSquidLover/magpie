"""List, check and restore backups of stock.db.

    python -m app.restore              list the backups, newest first, and check each one
    python -m app.restore NAME         put backup NAME back as the live database (the app must be stopped)
    python -m app.restore NAME --files also copy back photos and maps that are missing, from the mirror
    python -m app.restore NAME --force restore even if the backup is from a newer schema than this app knows

NAME is a file in backups/ (or in the mirror's backups/), or a path. The current database, with its -wal and -shm,
is moved into backups/replaced-<time>/ first, never deleted. Run it in a one-off container with the same data folder,
e.g. `docker run --rm -v /path/to/data:/data ghcr.io/therealsquidlover/magpie:latest python -m app.restore`.
"""
import json
import os
import shutil
import sqlite3
import sys
import time

from . import db


def _folders():
    out = [os.path.join(db.DATA, "backups")]
    if db.mirror_dir():
        out.append(os.path.join(db.mirror_dir(), "backups"))
    return [d for d in out if os.path.isdir(d)]


def _manifest(path):
    try:
        with open(path + ".json") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _schema(path):
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        return int(c.execute("SELECT v FROM meta WHERE k='schema'").fetchone()[0])
    finally:
        c.close()


def check(path):
    """(ok, reason, manifest): the checksum matches its .json (when there is one) and the file opens as a database."""
    m = _manifest(path)
    if m and m.get("sha256") and db._sha256(path) != m["sha256"]:
        return False, "checksum doesn't match (file changed or damaged)", m
    try:
        _schema(path)
    except (sqlite3.Error, TypeError, ValueError) as e:
        return False, "not a readable Magpie database: %s" % e, m
    return True, "ok" if m else "ok (older backup, no checksum)", m


def find(name):
    if os.path.isfile(name):
        return os.path.abspath(name)
    for d in _folders():
        p = os.path.join(d, os.path.basename(name))
        if os.path.isfile(p):
            return p
    return None


def listing():
    rows = []
    for d in _folders():
        where = "mirror" if d != os.path.join(db.DATA, "backups") else "local"
        for n in os.listdir(d):
            if db._is_backup(n):
                p = os.path.join(d, n)
                ok, why, m = check(p)
                rows.append((os.path.getmtime(p), where, n, ok, why, m or {}))
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows


def _print_list():
    rows = listing()
    if not rows:
        print("No backups found in", ", ".join(_folders()) or os.path.join(db.DATA, "backups"))
        return 1
    print("%-6s  %-16s  %-6s  %-9s  %-58s  %s" % ("where", "made", "schema", "app", "file", "check"))
    for t, where, n, ok, why, m in rows:
        print("%-6s  %-16s  %-6s  %-9s  %-58s  %s" % (
            where, time.strftime("%Y-%m-%d %H:%M", time.localtime(t)), m.get("schema", "?"),
            db.short_version(m["app_version"]) if m.get("app_version") else "?", n, why if ok else "BAD: " + why))
    return 0


def restore(name, files=False, force=False, out=print):
    src = find(name)
    if not src:
        out("No backup called %s. Run without arguments to list them." % name)
        return 2
    if db.app_running():
        out("Magpie is still running on %s. Stop the container first (and wait until it has stopped)." % db.DATA)
        return 3
    ok, why, m = check(src)
    if not ok:
        out("Not restoring %s: %s" % (os.path.basename(src), why))
        return 4
    schema = _schema(src)
    if schema > len(db.MIGRATIONS) and not force:
        tag = (m or {}).get("image_tag", "the version that made it")
        out("This backup has database schema %d, but this app only knows up to %d. Run the image tagged %s "
            "(or newer) to restore it, or pass --force." % (schema, len(db.MIGRATIONS), tag))
        return 5
    os.umask(0o077)
    tmp = db.DB + ".restoring"
    db._remove(tmp)
    shutil.copyfile(src, tmp)
    c = sqlite3.connect(tmp)
    try:
        res = c.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        c.close()
    if res != "ok":
        db._remove(tmp)
        out("Not restoring: the copy failed its integrity check: %s" % res)
        return 4
    keep = os.path.join(db.DATA, "backups", "replaced-" + db._stamp())
    moved = []
    for p in (db.DB, db.DB + "-wal", db.DB + "-shm", db.DB + "-journal"):
        if os.path.exists(p):  # the -wal holds the newest changes of the database being replaced: it goes with it
            os.makedirs(keep, exist_ok=True)
            shutil.move(p, os.path.join(keep, os.path.basename(p)))
            moved.append(os.path.basename(p))
    os.replace(tmp, db.DB)
    out("Restored %s (schema %d%s)." % (os.path.basename(src), schema,
                                         ", app " + db.short_version(m["app_version"]) if m and m.get("app_version") else ""))
    if moved:
        out("The database it replaced is in %s (%s)." % (keep, ", ".join(moved)))
    if files:
        n = copy_files_back()
        out("Copied back %d photo/map files from the mirror." % n)
    if m and m.get("image_tag") and m.get("app_version") != db.app_version():
        out("To run the app version that made this backup, set the image tag to :%s; otherwise the current app "
            "upgrades the database on start." % m["image_tag"])
    out("Start the container, then paste the API key and Telegram bot token into Settings again "
        "(not needed if they come from the container's environment).")
    return 0


def copy_files_back():
    """Photos and maps from the mirror that are missing in the data folder (nothing that exists is overwritten)."""
    m = db.mirror_dir()
    n = 0
    for sub in ("photos", "maps"):
        for root, _, names in os.walk(os.path.join(m, sub)) if m else []:
            for f in names:
                p = os.path.join(root, f)
                dst = os.path.join(db.DATA, os.path.relpath(p, m))
                if not os.path.exists(dst):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(p, dst)
                    n += 1
    return n


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    if flags - {"--files", "--force", "--list"} or len(args) > 1:
        print(__doc__)
        return 2
    if not args:
        return _print_list()
    return restore(args[0], files="--files" in flags, force="--force" in flags)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
