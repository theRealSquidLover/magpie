"""Syntax-check the JavaScript inside app/static/index.html with `node --check` (a stray bracket would otherwise only show
up as a blank app on the phone). Exits non-zero on a syntax error. Run: python tests/check_js.py [file.html]"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(path=os.path.join(ROOT, "app", "static", "index.html")):
    html = open(path, encoding="utf-8").read()
    # inline scripts only (no src=): those are the app's own code
    scripts = [m.group(1) for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I)]
    if not scripts:
        print("check_js: no inline <script> found in", path)
        return 1
    bad = 0
    for i, js in enumerate(scripts):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(js)
        try:
            r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
        finally:
            os.remove(f.name)
        if r.returncode:
            bad += 1
            print("check_js: script %d of %s has a syntax error:\n%s" % (i + 1, path, (r.stderr or r.stdout).replace(f.name, "<script %d>" % (i + 1))))
    if not bad:
        print("check_js: %d inline script(s) OK" % len(scripts))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
