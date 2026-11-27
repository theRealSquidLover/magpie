"""Fixes from the 2026-11-27 audit, in a headless phone browser. Every check asserts; exits non-zero on failure.
- sync #1: an item page and its open sheet keep pointing at the same new item when an earlier new item syncs
  (temp ids used to be renumbered on every rebuild, so the sheet saved on another item)
- ui H1-H3 / frontend #5: the item grid wraps long colour names, shows more columns to the right, flags lines below
  zero, and follows the warehouse filter (counts and taps)
- sync #2: Receive tapped while a photo is still being prepared: leaving the tab doesn't lose it silently, and a photo
  that finishes after leaving doesn't attach to the next item
Run from the repo root: python3 tests/e2e_audit2.py"""
import os, tempfile, subprocess, time, sys, requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("E2E_PORT", "18993"))
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


def stock(name, colour=None):
    s = st()
    p = [x["id"] for x in s["prods"] if x["name"] == name]
    v = [x["id"] for x in s["vars"] if x["prod"] in p and (colour is None or x["colour"] == colour)]
    return sum(m["qty"] for m in s["mv"] if m["v"] in v)


# a photo whose decoding takes 1.5 s (Image.src is delayed), as on a slow phone
SLOW = """(()=>{var I=window.Image;window.Image=function(){var im=new I();var d=Object.getOwnPropertyDescriptor(HTMLImageElement.prototype,'src');
 Object.defineProperty(im,'src',{set:function(v){setTimeout(function(){d.set.call(im,v)},1500)},get:function(){return d.get.call(im)}});return im}})()"""
PICK = """(async()=>{var c=document.createElement('canvas');c.width=300;c.height=300;var x=c.getContext('2d');for(var i=0;i<300;i++){x.fillStyle='hsl('+i+',60%,50%)';x.fillRect(Math.random()*300,Math.random()*300,30,30)}
 var b=await new Promise(r=>c.toBlob(r,'image/jpeg',0.8));onPick({files:[new File([b],'a.jpg',{type:'image/jpeg'})],value:''})})()"""

pr = boot()
try:
    W2 = requests.post(U + "/api/location", json={"name": "Yellow", "role": "holding"}).json()
    W2 = W2.get("id") or [l["id"] for l in st()["locs"] if l["name"] == "Yellow"][0]
    W3 = requests.post(U + "/api/location", json={"name": "Shop", "role": "holding"}).json()
    W3 = W3.get("id") or [l["id"] for l in st()["locs"] if l["name"] == "Shop"][0]
    LONG = "Printed design 12 (flowers, blue background)"
    SIZES = ["XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL", "5XL"]
    ops = [{"kind": "receive", "uuid": "t0", "product": "Towel", "colour": "Red", "size": "1", "lot": "", "loc": 1, "qty": 10}]
    ops += [{"kind": "receive", "uuid": "p%d" % i, "product": "Polo", "colour": "Navy", "size": z, "lot": "", "loc": 1, "qty": 3} for i, z in enumerate(SIZES)]
    ops += [{"kind": "receive", "uuid": "q1", "product": "Polo", "colour": "Navy", "size": "M", "lot": "", "loc": W2, "qty": 6},
            {"kind": "receive", "uuid": "q2", "product": "Polo", "colour": "Red", "size": "S", "lot": "", "loc": W2, "qty": 4},
            {"kind": "receive", "uuid": "q3", "product": "Polo", "colour": "Navy", "size": "M", "lot": "", "loc": W3, "qty": 2},
            {"kind": "sale", "uuid": "q4", "product": "Polo", "colour": "Navy", "size": "M", "lot": "", "loc": W3, "qty": 7},  # Shop: -5
            {"kind": "receive", "uuid": "b1", "product": "Blanket", "colour": LONG, "size": "Single", "lot": "", "loc": 1, "qty": 4},
            {"kind": "receive", "uuid": "b2", "product": "Blanket", "colour": LONG, "size": "Double", "lot": "", "loc": 1, "qty": 2}]
    requests.post(U + "/api/sync", json={"ops": ops})
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(**p.devices["iPhone 13"])
        ctx.add_init_script("try{localStorage.setItem('ifirst','1')}catch(e){}")
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(U + "/")
        pg.wait_for_timeout(1500)
        pid = lambda n: pg.evaluate("S.prods.find(p=>p.name==%r).id" % n)
        cell = lambda c, z: pg.locator("#ipg .icl[aria-label^='Polo · %s · %s:']" % (c, z))

        # ---- sync #1: temp ids stay put while an earlier new item syncs ----
        held = []
        pg.route("**/api/sync", lambda r: held.append(r))
        pg.evaluate("queue({kind:'receive',product:'Alpha',colour:'Red',size:'',lot:'',loc:1,qty:5})")
        pg.wait_for_timeout(300)
        pg.evaluate("queue({kind:'receive',product:'Bravo',colour:'Blue',size:'',lot:'',loc:1,qty:5})")
        pg.evaluate("queue({kind:'receive',product:'Charlie',colour:'Green',size:'',lot:'',loc:1,qty:5})")
        bid = pid("Bravo")
        check("new items have temp ids", bid < 0 and pid("Charlie") < 0, bid)
        pg.evaluate("itemOpen(%d)" % bid)
        pg.wait_for_timeout(200)
        pg.evaluate("cellTap(S.vars.find(v=>v.prod==%d).id)" % bid)
        pg.wait_for_timeout(200)
        check("sheet opened on Bravo", "Bravo · Blue" in pg.inner_text("#isd .isl"), pg.inner_text("#isd .isl"))
        held.pop(0).continue_()  # Alpha's sync goes through; Bravo and Charlie are still waiting
        pg.wait_for_function("P(S.prods.find(p=>p.name=='Alpha').id).id>0", timeout=10000)
        pg.wait_for_timeout(500)
        check("item page still shows Bravo after Alpha synced", pg.inner_text("#ipg h2") == "Bravo" and pg.evaluate("P(IP).name") == "Bravo", pg.inner_text("#ipg h2"))
        check("open sheet still points at Bravo", pg.evaluate("ISH&&P(V(ISH.v).prod).name") == "Bravo", pg.evaluate("ISH&&vl(ISH.v)"))
        pg.fill("#isq", "2")
        pg.evaluate("sheetSave()")
        pg.wait_for_timeout(200)
        check("the sale is queued for Bravo", pg.evaluate("JSON.stringify(PEND.filter(o=>o.kind=='sale').map(o=>[o.product,o.colour,o.qty]))") == '[["Bravo","Blue",2]]',
              pg.evaluate("JSON.stringify(PEND.filter(o=>o.kind=='sale').map(o=>[o.product,o.colour,o.qty]))"))
        # Bravo's own sync: the page moves from the temp id to the real one, and a sheet opened on it keeps working
        pg.evaluate("cellTap(S.vars.find(v=>v.prod==IP).id)")
        pg.wait_for_timeout(200)
        pg.unroute_all(behavior="ignoreErrors")
        for r in held:
            try:
                r.continue_()
            except Exception:
                pass
        pg.wait_for_function("PEND.length==0&&!SY&&IP>0", timeout=30000)
        pg.wait_for_timeout(300)
        check("item page follows Bravo to its real id", pg.inner_text("#ipg h2") == "Bravo" and pg.evaluate("IP") == pg.evaluate("S.prods.find(p=>p.name=='Bravo').id"), pg.evaluate("IP"))
        check("open sheet moved to the real line", pg.evaluate("ISH&&ISH.v>0&&vl(ISH.v)") == "Bravo · Blue" and pg.evaluate("ISH.key.split('|')[0]") == str(pg.evaluate("ISH.v")),
              pg.evaluate("ISH&&[ISH.v,ISH.key]"))
        pg.fill("#isq", "1")
        pg.evaluate("sheetSave()")
        pg.wait_for_function("PEND.length==0&&!SY", timeout=30000)
        pg.evaluate("sync()")
        pg.wait_for_timeout(1500)
        check("server: Bravo 5-2-1, Charlie untouched", stock("Bravo") == 2 and stock("Charlie") == 5, (stock("Bravo"), stock("Charlie")))
        pg.evaluate("sheetClose();itemClose()")
        # a sheet whose line is gone closes instead of saving somewhere else
        pg.evaluate("itemOpen(%d);cellTap(S.vars.find(v=>v.prod==IP).id);ISH.nm='something else'" % pid("Charlie"))
        pg.fill("#isq", "1")
        n0 = pg.evaluate("PEND.length")
        pg.evaluate("sheetSave()")
        pg.wait_for_timeout(200)
        check("changed line: sheet closes, nothing saved", pg.evaluate("!ISH&&!document.getElementById('isd')") and pg.evaluate("PEND.length") == n0 and "Nothing was saved" in pg.evaluate("($('toast')||{}).textContent||''"))
        pg.evaluate("itemClose()")

        # ---- item grid (ui H1-H3, frontend #5) ----
        pg.evaluate("itemOpen(%d)" % pid("Blanket"))
        pg.wait_for_timeout(200)
        g = pg.evaluate("""()=>{const w=document.querySelector('#ipg .igw'),th=document.querySelector('#ipg .igr th[scope=row]'),irl=th.querySelector('.irl').getBoundingClientRect(),c=[...document.querySelectorAll('#ipg .icl')].map(b=>b.getBoundingClientRect());
          return {name:irl.width, lines:Math.round(irl.height/20), th:th.getBoundingClientRect().width, vw:innerWidth, client:w.clientWidth, scroll:w.scrollWidth, cells:c.filter(r=>r.left>=th.getBoundingClientRect().right-1&&r.right<=innerWidth).length}}""")
        check("long colour name wraps within ~38% of the screen", g["name"] <= g["vw"] * 0.38 + 1 and g["lines"] >= 2 and g["scroll"] <= g["client"] + 1, g)
        check("no 'more to the right' when everything fits", not pg.is_visible("#ipg .igh"))
        check("long colour name: both numbers visible", g["cells"] == 2, g)
        pg.evaluate("itemClose();itemOpen(%d)" % pid("Polo"))
        pg.wait_for_timeout(300)
        check("9 sizes: the grid says it scrolls (fade + caption)", pg.evaluate("document.querySelector('#ipg .igo').classList.contains('mr')") and pg.is_visible("#ipg .igh"), pg.inner_text("#ipg"))
        pg.evaluate("var w=document.querySelector('#ipg .igw');w.scrollLeft=w.scrollWidth;w.dispatchEvent(new Event('scroll'))")
        pg.wait_for_timeout(200)
        check("scrolled to the end: fade and caption go", not pg.evaluate("document.querySelector('#ipg .igo').classList.contains('mr')") and not pg.is_visible("#ipg .igh"))
        sl = pg.evaluate("document.querySelector('#ipg .igw').scrollLeft")
        pg.evaluate("itemDraw()")
        check("a redraw (sync) keeps the sideways scroll", pg.evaluate("document.querySelector('#ipg .igw').scrollLeft") == sl, (sl, pg.evaluate("document.querySelector('#ipg .igw').scrollLeft")))
        nm = cell("Navy", "M")
        if os.environ.get("SHOTS"):
            pg.screenshot(path=os.path.join(os.environ["SHOTS"], "audit2-polo.png"))
        check("all warehouses: Navy M is 3+6+2-7 = 4 and shows the -5", nm.inner_text().replace("\n", " ") == "4 (-5)" and "neg" in nm.get_attribute("class"), nm.inner_text())
        red = pg.evaluate("(()=>{var e=document.createElement('span');e.style.color='var(--bad)';document.body.appendChild(e);var c=getComputedStyle(e).color;e.remove();return c})()")
        check("negative cell is red", pg.evaluate("getComputedStyle(document.querySelector('#ipg .icl.neg')).color") == red, red)
        check("negative cell's label says which line", "below zero: -5 in Shop" in nm.get_attribute("aria-label"), nm.get_attribute("aria-label"))
        pg.click("#ipg .chip:has-text('Shop')")
        pg.wait_for_timeout(200)
        check("Shop chip: Navy M is -5, red", nm.inner_text() == "-5" and "neg" in nm.get_attribute("class"), nm.inner_text())
        if os.environ.get("SHOTS"):
            pg.screenshot(path=os.path.join(os.environ["SHOTS"], "audit2-polo-shop.png"))
        nm.click()
        pg.wait_for_timeout(300)
        check("tapping the negative cell opens Recount on the Shop line", pg.evaluate("ISH&&ISH.k") == "count" and "Shop" in pg.evaluate("$('isf').selectedOptions[0].textContent"),
              pg.evaluate("ISH&&[ISH.k,$('isf')&&$('isf').selectedOptions[0].textContent]"))
        pg.evaluate("sheetClose()")
        pg.click("#ipg .chip:has-text('Yellow')")
        pg.wait_for_timeout(200)
        cell("Navy", "S").click()
        pg.wait_for_timeout(300)
        check("Yellow chip: a '–' cell doesn't open another warehouse's line", pg.locator("#isd").count() == 0 and "in Yellow" in pg.evaluate("($('toast')||{}).textContent||''"),
              pg.evaluate("($('toast')||{}).textContent||''"))
        cell("Navy", "M").click()
        pg.wait_for_timeout(300)
        check("Yellow chip: a cell opens the Yellow line", "Yellow" in pg.evaluate("$('isf')?$('isf').selectedOptions[0].textContent:''"), pg.evaluate("$('isf')?$('isf').selectedOptions[0].textContent:''"))
        pg.evaluate("sheetClose();itemClose()")
        # the Stock warehouse filter carries into the item page
        pg.evaluate("WF=%d;render()" % W2)
        pg.wait_for_timeout(200)
        row = pg.inner_text("#main .row:has-text('Polo')")
        pg.click("#main .row:has-text('Polo')")
        pg.wait_for_timeout(300)
        check("Stock filter Yellow: item page opens on Yellow", pg.evaluate("IW") == W2 and pg.inner_text("#ipg .chip.on") == "Yellow", pg.evaluate("IW"))
        check("Stock filter Yellow: list row and item page count the same lines", "2 lines" in row and pg.inner_text("#ipg .itot").split()[:2] == ["2", "lines"], (row, pg.inner_text("#ipg .itot")))
        pg.evaluate("itemClose();WF=0;render()")
        ctx.close()

        # ---- sync #2: Receive waiting for a photo, then leaving the tab ----
        ctx = b.new_context(viewport={"width": 390, "height": 844})
        ctx.add_init_script(SLOW)
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(U + "/")
        pg.wait_for_timeout(1500)
        ctx.set_offline(True)
        fill = lambda n: [pg.fill("#nm", n), pg.fill("#col", "Red"), pg.fill("#sz", "1"), pg.fill("#q", "1")]
        outbox = lambda: pg.evaluate("PEND.map(o=>o.kind+':'+o.product)")
        # (a) a photo that finishes after leaving the form doesn't come back on the next item
        pg.evaluate("go('Receive')")
        pg.evaluate(PICK)
        pg.wait_for_timeout(100)
        pg.evaluate("go('Stock')")
        pg.evaluate("go('Receive')")
        pg.wait_for_timeout(2200)
        check("late photo is dropped, not put on the next form", not pg.evaluate("!!PH") and not pg.evaluate("!!($('pv')&&$('pv').getAttribute('src'))"))
        fill("Sheet")
        pg.evaluate("rcvGo()")
        pg.wait_for_timeout(300)
        check("next item's Receive saves without the old photo", outbox() == ["receive:Sheet"], outbox())
        pg.evaluate("PEND=[];SENT=[];persist();rebuild();go('Receive')")
        # (b) Receive tapped while the photo is preparing, then Stock tapped: it stays and saves with the photo
        pg.evaluate(PICK)
        pg.wait_for_timeout(100)
        fill("Blanket")
        pg.evaluate("rcvGo()")
        check("says it is waiting for the photo", "Preparing the photo" in pg.inner_text("#mb"), pg.inner_text("#mb"))
        pg.evaluate("go('Stock')")
        check("leaving asks to wait instead of dropping the entry", pg.evaluate("tab") == "Receive" and "Tap again to leave without saving" in pg.inner_text("#mb"), (pg.evaluate("tab"), pg.inner_text("#mb")))
        pg.wait_for_timeout(2500)
        check("then it saves, with the photo", outbox() == ["receive:Blanket", "photo:Blanket"], outbox())
        check("and says so", "Received 1 cartons of Blanket" in pg.inner_text("body"), pg.inner_text("#main")[:300])
        pg.evaluate("go('Stock')")
        check("after saving, tabs switch normally", pg.evaluate("tab") == "Stock")
        pg.evaluate("PEND=[];SENT=[];persist();rebuild();go('Receive')")
        # (c) leaving anyway (second tap): nothing saved, and it says so; the late photo is dropped
        pg.evaluate(PICK)
        pg.wait_for_timeout(100)
        fill("Quilt")
        pg.evaluate("rcvGo()")
        pg.evaluate("go('Stock')")
        pg.evaluate("go('Stock')")
        check("second tap leaves and says nothing was saved", pg.evaluate("tab") == "Stock" and "Not saved" in pg.evaluate("($('toast')||{}).textContent||''"), pg.evaluate("($('toast')||{}).textContent||''"))
        pg.wait_for_timeout(2500)
        pg.evaluate("go('Receive')")
        check("nothing queued, no photo carried over", outbox() == [] and not pg.evaluate("!!PH"), outbox())
        # (d) an error in the waiting save is shown, not swallowed as "photo failed"
        pg.evaluate(PICK)
        pg.wait_for_timeout(100)
        pg.evaluate("phWait(function(){throw new Error('boom')})")
        pg.wait_for_timeout(2500)
        check("error in the waiting save is shown", "Not saved: boom" in pg.inner_text("body"), pg.inner_text("#mb"))
        b.close()
        check("no page errors", not errs, errs[:3])
finally:
    pr.terminate()

print("\n%d failed" % len(FAILS) if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
