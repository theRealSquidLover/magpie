"""Sync safety and form checks in a headless browser (audit 2026-11-26). Every check asserts; exits non-zero on failure.
- a sale the server accepted stays on the phone when the follow-up /api/state download fails, and a recount after it is right
- a 200 HTML page instead of /api/state doesn't wipe the app or its offline copy
- an op recorded before IndexedDB has loaded doesn't overwrite the stored outbox
- a receive waiting on a photo that fails is still saved (without the photo)
- "retry" answers keep the op in the outbox
- the form refuses what the server would refuse (long per carton, huge quantity, 0.5 cartons)
- the Log tab refreshes after a sync; a stale snapshot doesn't raise "new version"
Run from the repo root: python3 tests/e2e_sync.py"""
import os, tempfile, subprocess, time, sys, json, requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("E2E_PORT", "18991"))
U = "http://127.0.0.1:%d" % PORT
FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  -> " + str(detail)))
    if not ok:
        FAILS.append(name)


def boot():
    D = tempfile.mkdtemp()
    env = dict(os.environ, DATA_DIR=D, TZ="Europe/Athens", APP_VERSION="v1")
    try:
        requests.get(U + "/api/health", timeout=1)
        sys.exit("port %d is already in use; set E2E_PORT" % PORT)
    except requests.RequestException:
        pass
    pr = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
    for _ in range(100):
        try:
            requests.get(U + "/api/health", timeout=1)
            return pr
        except requests.RequestException:
            time.sleep(.1)
    pr.terminate()
    sys.exit("server did not start")


def st():
    return requests.get(U + "/api/state").json()


def stock():
    return sum(m["qty"] for m in st()["mv"] if m["v"] == 1 and m["lot"] == "" and m["loc"] == 1)


SALE = "queue({kind:'sale',product:'Towel',colour:'Red',size:'1',lot:'',loc:1,qty:%d})"
INIT_LSFULL = """(()=>{var s=Storage.prototype.setItem;Storage.prototype.setItem=function(k,v){if(k=='pend'&&v.length>200)throw new DOMException('quota','QuotaExceededError');return s.call(this,k,v)}})()"""
INIT_SLOWIDB = """(()=>{var o=indexedDB.open.bind(indexedDB);indexedDB.open=function(){var a=arguments,fake={};setTimeout(function(){var q=o.apply(null,a);q.onupgradeneeded=function(e){fake.result=q.result;fake.onupgradeneeded&&fake.onupgradeneeded(e)};q.onsuccess=function(e){fake.result=q.result;fake.onsuccess&&fake.onsuccess(e)};q.onerror=function(e){fake.error=q.error;fake.onerror&&fake.onerror(e)}},1500);return fake}})()"""

pr = boot()
try:
    requests.post(U + "/api/sync", json={"ops": [{"kind": "receive", "uuid": "a", "product": "Towel", "colour": "Red", "size": "1", "lot": "", "loc": 1, "qty": 10}]})
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 390, "height": 844})
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(U + "/")
        pg.wait_for_function("S.prods.length>0")

        # 1 accepted sale + failed state download: the phone keeps showing it, a recount stays right
        pg.route("**/api/state", lambda r: r.abort())
        pg.evaluate(SALE % 4)
        pg.wait_for_function("PEND.length==0", timeout=10000)
        pg.wait_for_timeout(300)
        check("1 server has the sale", stock() == 6, stock())
        check("1 phone still shows the sale after the state download failed", pg.evaluate("cur(1,'',1)") == 6, pg.evaluate("cur(1,'',1)"))
        ctx.set_offline(True)
        pg.reload()
        pg.wait_for_function("S.prods.length>0")
        pg.wait_for_timeout(500)
        check("1 ...and after an offline reload", pg.evaluate("cur(1,'',1)") == 6, pg.evaluate("cur(1,'',1)"))
        ctx.set_offline(False)
        pg.unroute("**/api/state")
        pg.evaluate("sync()")
        pg.wait_for_function("SENT.length==0", timeout=10000)
        check("1 SENT cleared once a snapshot has it", pg.evaluate("cur(1,'',1)") == 6, pg.evaluate("cur(1,'',1)"))

        # 2 an HTML page answering /api/state
        pg.route("**/api/state", lambda r: r.fulfill(status=200, body="<html><body>Please log in</body></html>", content_type="text/html"))
        pg.evaluate("sync()")
        pg.wait_for_timeout(1500)
        check("2 snapshot kept", pg.evaluate("okState(SV)&&S.prods.length>0"), pg.evaluate("JSON.stringify(SV).slice(0,80)"))
        check("2 shows a sync problem", "sync problem" in pg.inner_text("#hdr"), pg.inner_text("#hdr"))
        pg.unroute("**/api/state")
        ctx.set_offline(True)
        pg.reload()
        pg.wait_for_function("S.prods.length>0", timeout=5000)
        check("2 offline copy still has the data", pg.evaluate("S.prods.length") > 0)
        ctx.set_offline(False)
        pg.evaluate("sync()")
        pg.wait_for_timeout(1000)

        # 3 "retry" from the server keeps the op in the outbox
        def h3(route):
            body = json.loads(route.request.post_data or "{}")
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"results": [{"uuid": o["uuid"], "status": "retry", "msg": "database is locked"} for o in body.get("ops", [])]}))
        pg.route("**/api/sync", h3)
        pg.evaluate(SALE % 1)
        pg.wait_for_timeout(1500)
        check("3 retry keeps the op pending", pg.evaluate("PEND.length") == 1 and pg.evaluate("REJ.length") == 0, pg.evaluate("[PEND.length,REJ.length]"))
        pg.unroute("**/api/sync")
        pg.evaluate("sync()")
        pg.wait_for_function("PEND.length==0", timeout=10000)
        check("3 sent once the server recovers", stock() == 5, stock())

        # 4 Log refreshes after a sync
        pg.click("#nav >> text=Log")
        requests.post(U + "/api/sync", json={"ops": [{"kind": "sale", "uuid": "other-device", "product": "Towel", "colour": "Red", "size": "1", "lot": "", "loc": 1, "qty": 1, "note": "from the other phone"}]})
        pg.evaluate("sync()")
        pg.wait_for_timeout(1500)
        check("4 Log shows another device's entry without leaving the tab", "from the other phone" in pg.inner_text("#main"))

        # 5 form refuses what the server would refuse
        pg.click("#nav >> text=Receive")
        pg.fill("#nm", "Duvet")
        pg.fill("#col", "White")
        pg.fill("#sz", "King")
        pg.fill("#q", "3")
        pg.evaluate("document.querySelector('details.more')&&(document.querySelector('details.more').open=true)")
        pg.fill("#ppc", "24 doz in 6 bags of 4 pcs, white polybag w/ label")
        pg.click("#rgo")
        pg.wait_for_timeout(500)
        check("5 long per carton refused at the form", pg.evaluate("PEND.length") == 0 and "too long" in pg.inner_text("#mb"), pg.inner_text("#mb"))
        pg.fill("#ppc", "24 doz")
        pg.fill("#q", "0.5")
        pg.click("#rgo")
        pg.wait_for_timeout(500)
        check("5 0.5 cartons refused", pg.evaluate("PEND.length") == 0 and "less than 1" in pg.inner_text("#mb"), pg.inner_text("#mb"))
        pg.fill("#q", "200000")
        pg.click("#rgo")
        pg.wait_for_timeout(500)
        check("5 huge quantity refused", pg.evaluate("PEND.length") == 0 and "100000" in pg.inner_text("#mb"), pg.inner_text("#mb"))

        # 6 a receive waiting for a photo that fails is still saved
        pg.evaluate("PHBUSY=1")
        pg.fill("#q", "2")
        pg.click("#rgo")
        pg.wait_for_timeout(200)
        check("6 receive waits for the photo", pg.evaluate("PEND.length") == 0 and pg.evaluate("!!PHWQ"))
        # a picture the browser can't decode
        pg.evaluate("PHBUSY=0;readPhoto(new Blob(['not an image'],{type:'image/jpeg'}),function(){},null)")
        pg.wait_for_function("PHBUSY==0", timeout=10000)
        pg.wait_for_timeout(1500)
        names = [x["name"] for x in st()["prods"]]
        check("6 the receive was saved without the photo", "Duvet" in names, names)
        check("6 and it says so", "saved without it" in (pg.inner_text("#mb") + pg.inner_text("#main")), pg.inner_text("#mb"))

        # 7 a stale saved snapshot from another build doesn't raise the update banner
        pg.evaluate("SV.build='v0';SVFRESH=false;verBar()")
        check("7 no update banner from a stale snapshot", pg.inner_text("#verbar").strip() == "", pg.inner_text("#verbar"))
        b.close()

        # 8 an op recorded before IndexedDB has loaded doesn't overwrite the stored outbox
        b = p.chromium.launch()
        ctx = b.new_context()
        ctx.add_init_script(INIT_LSFULL)
        pg = ctx.new_page()
        pg.goto(U + "/")
        pg.wait_for_function("S.prods.length>0")
        ctx.set_offline(True)
        pg.evaluate("for(var i=0;i<3;i++)" + SALE % 1)
        pg.wait_for_timeout(800)
        pg.close()
        ctx.add_init_script(INIT_SLOWIDB)
        pg = ctx.new_page()
        pg.goto(U + "/", wait_until="domcontentloaded")
        pg.evaluate("queue({kind:'adjust',product:'Towel',colour:'Red',size:'1',lot:'',loc:1,qty:1,note:'found one'})")
        pg.wait_for_timeout(2500)
        pg.close()
        pg = ctx.new_page()
        pg.goto(U + "/")
        pg.wait_for_timeout(3000)
        kinds = pg.evaluate("PEND.map(o=>o.kind).sort()")
        check("8 queued ops survive a cold start", kinds == ["adjust", "sale", "sale", "sale"], kinds)
        b.close()
        check("no page errors", not errs, errs[:3])
finally:
    pr.terminate()

print("\n%d failed" % len(FAILS) if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
