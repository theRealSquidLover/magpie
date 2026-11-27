"""Item-first trial layout (Settings -> Layout) in a headless phone browser. Every check asserts; exits non-zero on failure.
- the switch is per phone and off by default; Classic is unchanged with it off
- item page: colour x size grid with totals, warehouse chips, swatches (photo / name / rainbow / grey)
- Sell, Move and Recount sheets save the same ops as Sale/Adjust and Transfer, and refuse what those refuse
- the + button finds a line; back closes the item page
Run from the repo root: python3 tests/e2e_itemfirst.py"""
import os, tempfile, subprocess, time, sys, json, requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("E2E_PORT", "18992"))
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


def stock():
    return sum(m["qty"] for m in st()["mv"] if m["v"] == 1 and m["lot"] == "" and m["loc"] == 1)



def tot(colour, size, loc=None):
    s = st()
    v = [x["id"] for x in s["vars"] if x["colour"] == colour and x["size"] == size]
    return sum(m["qty"] for m in s["mv"] if m["v"] in v and (loc is None or m["loc"] == loc))


pr = boot()
try:
    W2 = requests.post(U + "/api/location", json={"name": "Yellow", "role": "holding"}).json()
    W2 = W2.get("id") or [l["id"] for l in st()["locs"] if l["name"] == "Yellow"][0]
    ops = []
    for i, (c, z, q, loc) in enumerate([("Navy", "S", 10, 1), ("Navy", "M", 6, 1), ("Navy", "M", 4, W2), ("Light grey", "S", 3, 1), ("Assorted", "M", 8, W2), ("Design 9", "S", 5, 1), ("Zzz", "S", 2, 1)]):
        ops.append({"kind": "receive", "uuid": "r%d" % i, "product": "Polo shirt", "colour": c, "size": z, "lot": "", "loc": loc, "qty": q})
    ops.append({"kind": "receive", "uuid": "r0", "product": "Polo shirt", "colour": "Navy", "size": "XL", "lot": "", "loc": 1, "qty": 0})  # registered with 0: not a line in stock
    requests.post(U + "/api/sync", json={"ops": ops})
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(**p.devices["iPhone 13"])
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(U + "/")
        pg.wait_for_timeout(1500)
        # a colour photo for Design 9
        pid = pg.evaluate("S.prods.find(p=>p.name=='Polo shirt').id")
        pg.evaluate("S.cph=(S.cph||[]).concat([[%d,'Design 9','data:image/gif;base64,R0lGODlhAQABAAAAACw=']]);SV.cph=S.cph" % pid)

        check("off by default", pg.evaluate("IFIRST") is False)
        pg.click("#main .row:has-text('Polo shirt')")
        pg.wait_for_timeout(200)
        check("classic: tapping an item expands it", pg.locator("#ipg").count() == 0 and pg.locator("#main .sub").count() > 0)
        check("classic: classic bottom bar", pg.locator("#bn-plus").count() == 0 and pg.locator("#bn-set").count() == 1)

        pg.click("#bn-set")
        pg.wait_for_timeout(300)
        pg.select_option("#lay", "1")
        check("switch saved on this phone", pg.evaluate("localStorage.getItem('ifirst')") == "1")
        pg.click("#bn-stock")
        pg.wait_for_timeout(300)
        check("one bottom bar: Stock, Receive, +, Alerts, More", pg.locator("#bnav button").all_inner_texts() == ["Stock", "Receive", "+", "Alerts", "More"], pg.locator("#bnav button").all_inner_texts())
        check("status chip instead of the status line", pg.inner_text("#schip").strip().endswith("Online"), pg.inner_text("#hdr"))
        pg.evaluate("O={}")
        pg.evaluate("render()")
        pg.click("#main .row:has-text('Polo shirt')")
        pg.wait_for_timeout(300)
        check("item page opens", pg.locator("#ipg").count() == 1)
        txt = pg.inner_text("#ipg")
        check("header shows lines in stock, not a carton total", pg.inner_text("#ipg .itot").split()[:4] == ["6", "lines", "in", "stock"], pg.inner_text("#ipg .itot"))
        check("warehouse chips without totals", pg.locator("#ipg .chip").all_inner_texts() == ["All warehouses", "Main: Ground", "Yellow"], pg.locator("#ipg .chip").all_inner_texts())
        cell = lambda c, z: pg.locator("#ipg .icl[aria-label^='Polo shirt · %s · %s:']" % (c, z))
        check("grid cell Navy M = 10 (both warehouses)", cell("Navy", "M").inner_text() == "10", cell("Navy", "M").inner_text())
        check("grid has a blank where a line doesn't exist", pg.locator("#ipg td.ix").count() > 0)
        sw = pg.evaluate("[...document.querySelectorAll('#ipg th[scope=row]')].map(t=>[t.innerText.trim(),(t.querySelector('.isw').style.background||t.querySelector('.isw').tagName)])")
        swd = dict(sw)
        check("swatch: photo for Design 9", swd.get("Design 9") == "IMG", swd)
        check("swatch: Navy from the name", "36, 50, 79" in swd.get("Navy", "") or "#24324f" in swd.get("Navy", ""), swd)
        check("swatch: light grey is lighter", "color-mix" in swd.get("Light grey", ""), swd)
        check("swatch: rainbow for Assorted", "conic-gradient" in swd.get("Assorted", ""), swd)
        check("swatch: grey card colour when nothing matches", "var(--card)" in swd.get("Zzz", ""), swd)
        pg.click("#ipg .chip:has-text('Yellow')")
        pg.wait_for_timeout(200)
        check("warehouse chip filters the grid", cell("Navy", "M").inner_text() == "4" and pg.inner_text("#ipg .itot").startswith("2"), (cell("Navy", "M").inner_text(), pg.inner_text("#ipg .itot")))
        pg.click("#ipg .chip:has-text('All warehouses')")
        pg.wait_for_timeout(200)

        # sell from a cell
        cell("Navy", "S").click()
        pg.wait_for_timeout(300)
        check("sheet is a modal dialog", pg.evaluate("document.getElementById('isd').matches(':modal')"))
        check("cartons field focused", pg.evaluate("document.activeElement.id") == "isq")
        pg.fill("#isq", "12")
        pg.click("#isd button[type=submit]")
        pg.wait_for_timeout(200)
        check("sell refuses more than there is", "Only 10" in pg.inner_text("#ise") and tot("Navy", "S") == 10, pg.inner_text("#ise"))
        pg.fill("#isq", "3")
        pg.fill("#isn", "Shop A")
        pg.click("#isd button[type=submit]")
        pg.wait_for_timeout(1500)
        check("sale saved and synced", tot("Navy", "S") == 7, tot("Navy", "S"))
        check("sale op like Sale/Adjust", pg.evaluate("S.mv.some(m=>m.type=='sale'&&m.note=='Shop A')"))
        check("confirmation banner", "Sold 3 cartons" in pg.inner_text("#toast"))
        check("grid updated", cell("Navy", "S").inner_text() == "7", cell("Navy", "S").inner_text())

        # move
        cell("Navy", "M").click()
        pg.wait_for_timeout(300)
        pg.click("#isd .seg button:has-text('Move')")
        pg.select_option("#ist", str(W2))
        frm = pg.evaluate("document.getElementById('isf').selectedOptions[0].text")
        pg.fill("#isq", "2")
        pg.click("#isd button[type=submit]")
        pg.wait_for_timeout(1500)
        check("move saved", (tot("Navy", "M", 1), tot("Navy", "M", W2)) in ((4, 6), (6, 2)) and tot("Navy", "M") == 10 and pg.evaluate("S.mv.some(m=>m.type=='transfer')"), (frm, tot("Navy", "M", 1), tot("Navy", "M", W2)))

        # recount
        cell("Light grey", "S").click()
        pg.wait_for_timeout(300)
        pg.click("#isd .seg button:has-text('Recount')")
        pg.fill("#isq", "5")
        pg.click("#isd button[type=submit]")
        pg.wait_for_timeout(1500)
        check("recount up records an adjustment", tot("Light grey", "S") == 5, tot("Light grey", "S"))

        # zero cell
        pg.evaluate("toast('',0)")
        # cancel closes, Escape closes
        cell("Navy", "S").click()
        pg.wait_for_timeout(200)
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(200)
        check("Escape closes the sheet", pg.locator("#isd").count() == 0)

        # item page buttons: Sell opens a search scoped to this item
        pg.click("#ipg .iac button:has-text('Sell')")
        pg.wait_for_timeout(300)
        rows_ = pg.locator("#iql .iqr").count()
        check("Sell on the item page lists this item's lines", rows_ >= 5 and all("Polo shirt" in t for t in pg.locator("#iql .iqr").all_inner_texts()), rows_)
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(200)

        # back closes the item page
        pg.go_back()
        pg.wait_for_timeout(300)
        check("back closes the item page", pg.locator("#ipg").count() == 0 and pg.evaluate("IP") is None)

        # + button
        check("item-first: no top tabs on Stock", not pg.is_visible("#nav"))
        sw = pg.evaluate("[...document.querySelectorAll('#main .row .rsw .isw')].length")
        check("Stock rows show one swatch per colour", sw == 5, sw)
        check("no + photo box on rows without a photo", pg.locator("#main .row .ph").count() == 0)
        pg.click("#bn-plus")
        pg.wait_for_timeout(300)
        labels = pg.locator("#isd .iam b").all_inner_texts()
        check("+ menu: Sell, Move, Recount", labels == ["Sell", "Move", "Recount"], labels)
        pg.keyboard.press("Escape")
        pg.click("#bn-more")
        pg.wait_for_timeout(300)
        labels = pg.locator("#isd .iam b").all_inner_texts()
        check("More has every other screen", labels == ["Report", "Log", "Warehouses", "Settings", "Sale / Adjust form", "Transfer form"], labels)
        pg.click("#mm-rep")
        pg.wait_for_timeout(300)
        check("Report opens under More", pg.inner_text("#ttl") == "Report" and "on" in pg.get_attribute("#bn-more", "class"))
        pg.click("#bn-rcv")
        pg.wait_for_timeout(300)
        check("Receive is a tab of its own", pg.evaluate("tab") == "Receive" and pg.locator("#loc").count() == 1)
        rc = pg.locator("#main .rrc .chip").all_inner_texts()
        check("Receive starts with recent items, Read box photo and New item", rc[0] == "Polo shirt" and rc[-2:] == ["📷 Read box photo", "+ New item"], rc)
        check("Receiving several? comes before colour and size", pg.evaluate("!!(document.getElementById('rmode').compareDocumentPosition(document.getElementById('col')) & 4)"))
        check("Per carton sits in More details", pg.evaluate("!!document.getElementById('ppc').closest('details')"))
        pg.click("#main .rrc .chip:has-text('Polo shirt')")
        pg.wait_for_timeout(400)  # a pick ignores taps for 300 ms (ghost-click guard)
        check("tapping a recent item picks it", pg.input_value("#nm") == "Polo shirt", pg.input_value("#nm"))
        pg.click("#main .rrc .chip:has-text('New item')")
        check("New item clears the field", pg.input_value("#nm") == "" and pg.evaluate("PSEL") is None, (pg.input_value("#nm"), pg.evaluate("PSEL"), pg.evaluate("tab")))
        pg.click("#bn-stock")
        pg.wait_for_timeout(300)
        pg.click("#bn-plus")
        pg.wait_for_timeout(300)
        pg.click("#isd .iam:has-text('Sell')")
        pg.wait_for_timeout(300)
        pg.fill("#iqq", "assorted")
        pg.wait_for_timeout(100)
        check("+ finds a line", pg.locator("#iql .iqr").count() == 1)
        pg.click("#iql .iqr")
        pg.wait_for_timeout(200)
        pg.fill("#isq", "1")
        pg.click("#isd button[type=submit]")
        pg.wait_for_timeout(1500)
        check("+ sale saved", tot("Assorted", "M") == 7, tot("Assorted", "M"))

        # a prepared (pending) item opens its own page
        requests.post(U + "/api/sync", json={"ops": [{"kind": "prepare", "uuid": "pp1", "product": "Duvet cover", "colour": "Sage", "size": "Double"}]})
        pg.evaluate("sync()")
        pg.wait_for_timeout(1500)
        pg.evaluate("render()")
        if pg.locator("#main .row:has-text('Duvet cover')").count() == 0:
            pg.click("#main .cath:has-text('Pending')")
        pg.click("#main .row:has-text('Duvet cover')")
        pg.wait_for_timeout(300)
        check("pending item opens an item page", pg.locator("#ipg").count() == 1 and "not received yet" in pg.inner_text("#ipg"))
        check("pending item page has Receive now and Activate", pg.locator("#ipg .iac button").all_inner_texts() == ["Receive now", "Activate"], pg.locator("#ipg .iac button").all_inner_texts())
        pg.click("#ipg .iac button:has-text('Receive now')")
        pg.wait_for_timeout(500)
        check("Receive now opens Receive with the item", pg.evaluate("tab") == "Receive" and "Duvet cover" in pg.input_value("#nm"), pg.evaluate("tab"))
        pg.click("#bn-stock")
        pg.wait_for_timeout(300)

        # classic tabs untouched
        pg.click("#bn-more")
        pg.wait_for_timeout(300)
        pg.click("#mm-set")
        pg.wait_for_timeout(200)
        pg.select_option("#lay", "0")
        pg.click("#bn-stock")
        pg.wait_for_timeout(200)
        pg.click("#bn-stock") if pg.locator("#bn-stock").count() else None
        pg.evaluate("go('Receive')")
        pg.wait_for_timeout(200)
        check("switch off: classic Receive (no recent strip, Per carton outside More details)", pg.locator("#main .rrc").count() == 0 and not pg.evaluate("!!document.getElementById('ppc').closest('details')"))
        pg.evaluate("go('Stock')")
        pg.wait_for_timeout(200)
        check("switch off: classic bottom bar again", pg.locator("#bn-plus").count() == 0 and pg.locator("#bn-set").count() == 1)
        check("switch off: tabs and no swatches", pg.is_visible("#nav") and "Receive" in pg.inner_text("#nav") and pg.locator("#main .rsw").count() == 0)
        b.close()
        check("no page errors", not errs, errs[:3])
finally:
    pr.terminate()

print("\n%d failed" % len(FAILS) if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
