"""End-to-end check of every user-facing feature against a fresh server, in a headless browser (Playwright + Chromium).
Not collected by pytest. Run from the repo root:  python3 tests/e2e_smoke.py   (prints PASS/FAIL per check)."""
import os, subprocess, sys, tempfile, time, base64, io
import requests

PORT = 18986
D = tempfile.mkdtemp()
env = dict(os.environ, DATA_DIR=D, TZ="Europe/Athens")
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
time.sleep(3)
U = "http://127.0.0.1:%d" % PORT
R = []


def chk(name, ok, info=""):
    R.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  -> " + str(info)[:300]))


def st():
    return requests.get(U + "/api/state").json()


def tot(name, colour=None, size=None, loc=None, lot=None):
    s = st()
    P = {p["id"]: p["name"] for p in s["prods"]}
    V = {v["id"]: v for v in s["vars"]}
    n = 0
    for m in s["mv"]:
        v = V[m["v"]]
        if P[v["prod"]] != name or (colour is not None and v["colour"] != colour) or (size is not None and v["size"] != size):
            continue
        if (loc is not None and m["loc"] != loc) or (lot is not None and m["lot"] != lot):
            continue
        n += m["qty"]
    return n


try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark")
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("dialog", lambda d: d.accept())
        pg.goto(U + "/")
        pg.wait_for_selector("#nav button")
        time.sleep(1)

        def tab(t):
            if t in ("Stock", "Receive", "Sale/Adjust", "Transfer", "Report", "Log"):
                pg.click("#bnav >> text=Stock")
                time.sleep(.2)
                pg.click("#nav >> text=" + t)
            else:
                pg.click("#bnav >> text=" + t)
            time.sleep(.4)

        def msg():
            # success messages show at the top: on Receive in #mbt, elsewhere in the #toast banner; errors stay in #mb
            return " ".join(pg.locator("#mbt, #mb, #toast").all_inner_texts())

        def sync():
            pg.evaluate("sync()")
            time.sleep(1.5)

        # ---------- warehouses ----------
        tab("Warehouses")
        pg.fill("#nl", "Shop Upstairs")
        pg.select_option("#nr", "picking")
        pg.click("button:has-text('Add warehouse')")
        time.sleep(1)
        L = {l["name"]: l for l in st()["locs"]}
        chk("add picking warehouse", "Shop Upstairs" in L and L["Shop Upstairs"]["role"] == "picking", L.keys())
        GND, BAS, YEL, SHOP = L["Main: Ground"]["id"], L["Main: Basement"]["id"], L["Store"]["id"], L["Shop Upstairs"]["id"]
        requests.post(U + "/api/location/%d" % YEL, json={"role": "picking"})
        sync()

        # ---------- receive: single, sums, per carton, invalid sum ----------
        tab("Receive")
        pg.select_option("#loc", str(GND))
        pg.fill("#nm", "Waterproof Mattress Protector")
        pg.fill("#col", "Quilted")
        pg.fill("#sz", "140x200+35cm")
        pg.fill("#q", "32+34+36")
        pg.fill("#ppc", "20 doz")
        pg.click("#rgo")
        time.sleep(.8)
        chk("receive with a sum", "Received 102" in msg(), msg())
        pg.fill("#nm", "Waterproof Mattress Protector")
        pg.evaluate("typed()")
        pg.fill("#col", "Terry")
        pg.fill("#sz", "140x200+35cm")
        pg.fill("#q", "1+abc")
        pg.click("#rgo")
        time.sleep(.5)
        chk("invalid sum refused", "doesn’t work out" in msg(), msg())
        pg.fill("#q", "40")
        pg.fill("#ppc", "40 doz")
        pg.click("#rgo")
        time.sleep(.8)
        pg.fill("#nm", "Waterproof Mattress Protector")
        pg.evaluate("typed()")
        pg.fill("#col", "Quilted")
        pg.evaluate("chips('col')")
        chk("per carton follows the colour", pg.input_value("#ppc") == "20 doz", pg.input_value("#ppc"))
        pg.fill("#col", "Terry")
        pg.evaluate("chips('col')")
        chk("per carton follows the other colour", pg.input_value("#ppc") == "40 doz", pg.input_value("#ppc"))

        # ---------- receive: many sizes grid, size order, +/- buttons, save-then-add-another ----------
        pg.fill("#nm", "Mens Shirt")
        pg.evaluate("typed()")
        pg.fill("#col", "White")
        pg.select_option("#rmode", "size")
        pg.click("#rgo")
        time.sleep(.4)
        for z in ["L", "XS", "XXXXXL", "M", "S"]:
            pg.fill("#gnew", z)
            pg.keyboard.press("Enter")
        names = [t.split("\n")[0].split(" ·")[0].strip() for t in pg.locator("#gov .gr > span:first-child").all_inner_texts()]
        chk("grid sizes in garment order", names == ["XS", "S", "M", "L", "XXXXXL"], names)
        q = pg.locator("#gov .gq")
        q.nth(0).click()
        pg.keyboard.type("10")
        pg.click("#gov button[aria-label=Plus]")
        pg.keyboard.type("5")
        pg.click("#gov button[aria-label=Minus]")
        pg.keyboard.type("3")
        q.nth(2).fill("7")
        pg.click("#gsave")
        time.sleep(1)
        chk("grid saved with a sum", tot("Mens Shirt", size="XS") == 12 and tot("Mens Shirt", size="M") == 7, (tot("Mens Shirt", size="XS"), tot("Mens Shirt", size="M")))
        pg.fill("#nm", "Lycra Set")
        pg.select_option("#rmode", "more")
        for sz, n in [("8", "5"), ("10", "6")]:
            pg.fill("#col", "Black")
            pg.fill("#sz", sz)
            pg.fill("#q", n)
            pg.click("#rgo")
            time.sleep(.5)
        pg.select_option("#rmode", "")
        sync()
        chk("save, then add another", tot("Lycra Set") == 11, tot("Lycra Set"))

        # ---------- sale / recount / adjust ----------
        tab("Sale/Adjust")
        pg.fill("#fl", "lycra 8")
        time.sleep(.3)
        pg.locator("#sel .ln").first.click()
        pg.fill("#q", "2")
        pg.click("button:has-text('Record')")
        time.sleep(.8)
        chk("sale", tot("Lycra Set", size="8") == 3, tot("Lycra Set", size="8"))
        pg.fill("#fl", "lycra 10")
        time.sleep(.3)
        pg.locator("#sel .ln").first.click()
        pg.select_option("#t", "count")
        pg.fill("#q", "4")
        pg.click("button:has-text('Record')")
        time.sleep(.8)
        chk("recount", tot("Lycra Set", size="10") == 4, tot("Lycra Set", size="10"))
        pg.fill("#fl", "lycra 10")
        time.sleep(.3)
        pg.locator("#sel .ln").first.click()
        pg.select_option("#t", "adjust")
        pg.select_option("#adir", "1")
        pg.fill("#q", "3")
        pg.fill("#note", "found")
        pg.click("button:has-text('Record')")
        time.sleep(.8)
        chk("adjust up with direction", tot("Lycra Set", size="10") == 7, tot("Lycra Set", size="10"))

        # ---------- transfer (from warehouse selector) ----------
        tab("Transfer")
        pg.select_option("#fw", str(GND))
        pg.fill("#fl", "quilted")
        time.sleep(.3)
        pg.locator("#sel .ln").first.click()
        pg.select_option("#to", str(BAS))
        pg.fill("#q", "20")
        pg.click("#main button.btn:has-text('Transfer')")
        time.sleep(.8)
        sync()
        chk("transfer", tot("Waterproof Mattress Protector", colour="Quilted", loc=BAS) == 20 and tot("Waterproof Mattress Protector", colour="Quilted", loc=GND) == 82, (msg(), tot("Waterproof Mattress Protector", colour="Quilted", loc=BAS), tot("Waterproof Mattress Protector", colour="Quilted", loc=GND), pg.locator("#sel .ln.on").all_inner_texts()))

        # ---------- sections: set up, receive into one, oldest first, move ----------
        requests.post(U + "/api/location/%d/sections" % BAS, json={"on": True, "sections": [
            {"name": n, "x": i * .25, "y": 0, "w": .24, "h": 1} for i, n in enumerate("ABCD")]})
        sync()
        S = {z["name"]: z["id"] for z in [l for l in st()["locs"] if l["id"] == BAS][0]["secs"]}
        tab("Receive")
        pg.select_option("#loc", str(BAS))
        pg.evaluate("secField()")
        pg.fill("#nm", "Fleece Bedsheets")
        pg.evaluate("typed()")
        pg.fill("#col", "Assorted")
        pg.fill("#sz", "230")
        pg.fill("#q", "30")
        pg.click("button[aria-label='Pick the section on the map']")
        time.sleep(.3)
        pg.locator("#mov .zone").nth(3).click()
        chk("section picked on the map", pg.input_value("#sec") == "D", pg.input_value("#sec"))
        pg.click("#rgo")
        time.sleep(.8)
        sync()
        tab("Stock")
        pg.select_option("#wfc select", str(BAS))
        time.sleep(.3)
        pg.locator(".row:has-text('Fleece')").first.click()
        pg.locator(".sub:has-text('Assorted')").first.click()
        time.sleep(.2)
        chk("stock line shows the section", "D: 30" in pg.locator(".mapl").first.inner_text())
        # the quilted 20 transferred in earlier sit in Unsorted: move them to A
        pg.locator(".row:has-text('Fleece')").first.click()
        pg.locator(".row:has-text('Mattress')").first.click()
        pg.locator(".sub:has-text('Quilted')").first.click()
        time.sleep(.2)
        pg.locator(".mapl").first.click()
        time.sleep(.3)
        pg.locator("#mov .zone").nth(0).click()
        pg.click("#mov .btn")
        time.sleep(.3)
        pg.click("#mov button:has-text('Close')")
        sync()
        mv = [m for m in st()["mv"] if m["type"] == "move"]
        chk("move between sections", len(mv) == 2 and sum(m["qty"] for m in mv) == 0 and any(m["s"] == S["A"] and m["qty"] == 20 for m in mv), mv)
        pg.select_option("#wfc select", "0")

        # ---------- several lines to one section at once ----------
        ops = [{"kind": "receive", "uuid": "bm%d" % i, "product": "Bulk Towel", "colour": "White", "size": z, "lot": "", "loc": BAS, "qty": q}
               for i, (z, q) in enumerate([("12", 6), ("14", 12), ("16", 10)])]
        ops.append({"kind": "receive", "uuid": "bm9", "product": "Bulk Towel", "colour": "White", "size": "18", "lot": "", "loc": GND, "qty": 4})
        requests.post(U + "/api/sync", json={"ops": ops})
        sync()
        tab("Stock")
        pg.locator(".row:has-text('Bulk Towel')").first.click()
        pg.click("button:has-text('Move several to a section')")
        time.sleep(.2)
        chk("select mode opens every size, ticks only sectioned lines", pg.locator(".sz:has-text('16')").count() == 1 and pg.locator(".bml").count() == 3, pg.locator(".bml").count())
        pg.locator(".bml:has-text('lot · 6')").first.click()
        pg.locator(".bml:has-text('lot · 6')").first.click()
        chk("tick and untick a line", pg.locator(".bml input:checked").count() == 0)
        pg.click("#bmbar button:has-text('All')")
        chk("all ticks every line in that warehouse", pg.locator(".bml input:checked").count() == 3 and "28 ctn" in pg.inner_text("#bmbar"), pg.inner_text("#bmbar"))
        pg.click("#bmbar button:has-text('Move')")
        time.sleep(.3)
        pg.locator("#mov .zone").nth(1).click()
        chk("bulk move button names the total", "Move 28 ctn to B" in pg.inner_text("#bmgo"), pg.inner_text("#bmgo"))
        pg.click("#bmgo")
        time.sleep(.3)
        sync()
        s = st()
        bt = {v["id"]: v["size"] for v in s["vars"] if [p for p in s["prods"] if p["id"] == v["prod"]][0]["name"] == "Bulk Towel"}
        inB = {bt[m["v"]]: m["qty"] for m in s["mv"] if m["v"] in bt and m["type"] == "move" and m["s"] == S["B"]}
        pidb = [p["id"] for p in s["prods"] if p["name"] == "Bulk Towel"][0]
        chk("bulk move: every ticked line moved to B, home set", inB == {"12": 6, "14": 12, "16": 10} and [pidb, BAS, S["B"]] in s["homes"] and pg.locator("#bmbar").count() == 0, (inB, s["homes"]))
        chk("bulk move confirmation", "Moved 28 ctn (3 lines) from Unsorted to B" in msg(), msg())

        # ---------- filters ----------
        pid = {x["name"]: x["id"] for x in st()["prods"]}
        requests.post(U + "/api/product/%d" % pid["Lycra Set"], json={"name": "Lycra Set", "category": "School Wear"})
        sync()
        pg.evaluate("render()")
        time.sleep(.3)
        pg.select_option("#catc", "School Wear")
        time.sleep(.3)
        chk("category filter", [t.split("\n")[0] for t in pg.locator("#res .row b").all_inner_texts()] == ["Lycra Set"])
        pg.select_option("#catc", "*")
        pg.select_option("#wfc select", str(BAS))
        time.sleep(.3)
        rows = [t.split("\n")[0] for t in pg.locator("#res .row b").all_inner_texts()]
        chk("warehouse filter", set(rows) == {"Fleece Bedsheets", "Waterproof Mattress Protector", "Bulk Towel"}, rows)
        pg.select_option("#wfc select", "0")

        # ---------- alerts: reorder + refill ----------
        requests.post(U + "/api/reorder", json={"product_id": pid["Lycra Set"], "level": 10})
        sync()
        chk("reorder alert", pg.evaluate("alerts().length") >= 1)
        tab("Alerts")
        chk("alerts tab renders", "Lycra" in pg.inner_text("#main"), pg.inner_text("#main")[:200])
        r = requests.get(U + "/api/stock-request")
        chk("stock request PDF", r.status_code in (200, 404))

        # ---------- reports: yearly + multi-item PDF ----------
        tab("Report")
        chk("yearly report", "Lycra" in pg.inner_text("#rep"))
        pg.click("#xri")
        pg.keyboard.type("mattress")
        time.sleep(.2)
        pg.locator("#xrs .opt").first.click()
        pg.click("#xri")
        pg.keyboard.type("fleece")
        time.sleep(.2)
        pg.locator("#xrs .opt").first.click()
        items = pg.evaluate("XR.items")
        pg.click("#xr .btn")
        time.sleep(2)
        fn = pg.evaluate("PDFB&&PDFB.file.name")
        r = requests.get(U + "/api/export?items=" + ",".join(map(str, items)))
        from pypdf import PdfReader
        txt = "\n".join(x.extract_text() for x in PdfReader(io.BytesIO(r.content)).pages) if r.status_code == 200 else ""
        chk("multi-item PDF", pg.locator("#pdfov").count() == 1 and "Waterproof Mattress Protector" in txt and "Fleece Bedsheets" in txt and txt.count("Quilted") == 1, (fn, txt[:300]))
        pg.evaluate("var o=document.getElementById('pdfov');if(o)o.remove()")

        # ---------- pending items ----------
        tab("Receive")
        pg.check("#prep")
        pg.fill("#nm", "Kids Robe")
        pg.fill("#col", "Blue")
        pg.fill("#sz", "4")
        pg.click("#rgo")
        time.sleep(.8)
        sync()
        kp = [x for x in st()["prods"] if x["name"] == "Kids Robe"]
        chk("prepare pending item", kp and kp[0]["pend"] and tot("Kids Robe") == 0)
        pg.uncheck("#prep") if pg.locator("#prep").is_checked() else None
        pg.evaluate("PREP=false;prepUpd()")
        pg.fill("#nm", "Kids Robe")
        pg.evaluate("typed()")
        pg.fill("#col", "Blue")
        pg.fill("#sz", "4")
        pg.fill("#q", "6")
        pg.select_option("#loc", str(GND))
        pg.evaluate("secField()")
        pg.click("#rgo")
        time.sleep(.8)
        sync()
        chk("receiving activates it", not [x for x in st()["prods"] if x["name"] == "Kids Robe"][0]["pend"] and tot("Kids Robe") == 6)
        # pending item grouped by size, colours only on its one size: "Set up many sizes" with no colour must not add a
        # no-colour line to the existing size, and a size typed in "New size" but not yet added is still set up
        TC = "Tablecloth PU"
        requests.post(U + "/api/sync", json={"ops": [{"kind": "prepare", "uuid": "tc%d" % i, "product": TC, "colour": c, "size": "150x220cm"} for i, c in enumerate(["Design 1", "Design 2"])]})
        tcid = [x for x in st()["prods"] if x["name"] == TC][0]["id"]
        requests.post(U + "/api/product/%d" % tcid, json={"name": TC, "group_by": "size", "pending": True})
        sync()
        tab("Receive")
        pg.check("#prep")
        pg.fill("#nm", TC)
        pg.evaluate("typed()")
        pg.select_option("#rmode", "size")
        pg.click("#rgo")
        time.sleep(.4)
        tc_rows = pg.locator("#gov .gr").count()
        pg.fill("#gnew", "180 Round")
        tc_btn = pg.locator("#gsave").inner_text()
        pg.click("#gsave")
        time.sleep(.8)
        sync()
        tcv = sorted((v["colour"], v["size"]) for v in st()["vars"] if v["prod"] == tcid)
        chk("set up many sizes, no colour: only the new size", tc_rows == 0 and tc_btn == "Set up 1 size" and tcv == [("", "180 Round"), ("Design 1", "150x220cm"), ("Design 2", "150x220cm")], (tc_rows, tc_btn, tcv))
        pg.fill("#nm", TC)
        pg.evaluate("typed()")
        pg.fill("#col", "Design 1")
        pg.select_option("#rmode", "size")
        pg.click("#rgo")
        time.sleep(.4)
        chk("set up many sizes lists what that colour has", pg.locator("#gov .gr").all_inner_texts() == ["150x220cm\nalready set up"], pg.locator("#gov .gr").all_inner_texts())
        pg.evaluate("gridClose();RM='';PREP=false;prepUpd()")
        pg.uncheck("#prep") if pg.locator("#prep").is_checked() else None
        # a pending item's line can be deleted from its Edit form; an active item has no Delete button
        tab("Stock")
        pg.evaluate("CO['c:__pend']=false;O['p%d']=1;edt('z%d|180 Round')" % (tcid, tcid))
        pg.locator("button:has-text('Delete')").first.click()
        # call() clears ED once the save round-trip ends; opening the next form before that lets it be wiped
        pg.wait_for_function("ED===null", timeout=10000)
        tcv = sorted((v["colour"], v["size"]) for v in st()["vars"] if v["prod"] == tcid)
        robe = [x for x in st()["prods"] if x["name"] == "Kids Robe"][0]["id"]
        pg.evaluate("O['p%d']=1;edt('p%d')" % (robe, robe))
        has_en = pg.locator("#en").count()
        chk("delete a pending item's size", tcv == [("Design 1", "150x220cm"), ("Design 2", "150x220cm")] and has_en == 1 and pg.locator("button:has-text('Delete')").count() == 0, (tcv, has_en, pg.locator("button:has-text('Delete')").count()))
        pg.evaluate("edt(null)")
        # Move back to pending: only offered on an active item that never had stock
        requests.post(U + "/api/sync", json={"ops": [{"kind": "prepare", "uuid": "kslp", "product": "Kids Slipper", "alias": "KS-int", "colour": "Pink", "size": "28"}]})
        ks = [x for x in st()["prods"] if x["name"] == "Kids Slipper"][0]
        requests.post(U + "/api/product/%d" % ks["id"], json={"name": "Kids Slipper", "pending": False})
        sync()
        tab("Stock")
        pg.locator(".row:has-text('Kids Robe')").first.click()
        robe_btn = pg.locator("button:has-text('Move back to pending')").count()
        pg.locator(".row:has-text('Kids Robe')").first.click()
        pg.locator(".row:has-text('Kids Slipper')").first.click()
        pg.locator("button:has-text('Move back to pending')").first.click()
        try:  # an online call plus a state reload: wait for the redraw instead of a fixed sleep (0.8 s was too short on CI)
            pg.wait_for_selector(".row:has-text('Kids Slipper'):has-text('pending')", timeout=10000)
        except Exception:
            pass
        ks = [x for x in st()["prods"] if x["name"] == "Kids Slipper"][0]
        chk("move back to pending", robe_btn == 0 and ks["pend"] and ks["alias"] == "KS-int" and pg.locator(".row:has-text('Kids Slipper'):has-text('pending')").count() == 1, (robe_btn, ks))

        # ---------- colour photos ----------
        img = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0 test image").decode()
        requests.post(U + "/api/product/%d" % pid["Fleece Bedsheets"], json={"name": "Fleece Bedsheets", "colour_photos": True})
        requests.post(U + "/api/sync", json={"ops": [{"kind": "photo", "uuid": "cph", "product": "Fleece Bedsheets", "colour": "Assorted", "data": img}]})
        sync()
        tab("Stock")
        if not pg.locator(".sub:has-text('Assorted')").count():
            pg.locator(".row:has-text('Fleece')").first.click()
            time.sleep(.2)
        chk("colour photo on stock", pg.locator(".sub:has-text('Assorted') img.th").count() == 1)

        # ---------- offline queue ----------
        ctx.set_offline(True)
        tab("Sale/Adjust")
        pg.fill("#fl", "mens shirt xs")
        time.sleep(.3)
        pg.locator("#sel .ln").first.click()
        pg.fill("#q", "2")
        pg.click("button:has-text('Record')")
        time.sleep(.5)
        pend = pg.evaluate("PEND.length")
        ctx.set_offline(False)
        sync()
        chk("offline sale queued then synced", pend == 1 and pg.evaluate("PEND.length") == 0 and tot("Mens Shirt", size="XS") == 10, (pend, tot("Mens Shirt", size="XS")))

        # ---------- log + settings ----------
        tab("Log")
        lg = pg.inner_text("#main")
        chk("log shows entries and the move", "moved 20" in lg and "sale" in lg, lg[:200])
        # reverse the offline sale from the log: stock goes back, the original is marked, it can't be reversed twice
        row = pg.locator("#main .row:has-text('sale -2')").first
        row.locator("button:has-text('Reverse')").click()
        time.sleep(.2)
        pg.fill("#rvn", "typed the wrong size")
        pg.click("button:has-text('Confirm reverse')")
        time.sleep(.3)
        sync()
        tab("Log")
        lg = pg.inner_text("#main")
        orig = pg.locator("#main .row:has-text('sale -2')").first
        chk("reverse puts the sale back", tot("Mens Shirt", size="XS") == 12, tot("Mens Shirt", size="XS"))
        chk("log shows the reversal and marks the original",
            "↩ sale +2" in lg and "reversal of the sale" in lg and "typed the wrong size" in lg and "reversed " in orig.inner_text()
            and orig.locator("button:has-text('Reverse')").count() == 0, lg[:400])
        chk("the reversal itself offers no Reverse", pg.locator("#main .row:has-text('↩ sale +2') button:has-text('Reverse')").count() == 0)
        tab("Settings")
        chk("settings renders", "Telegram" in pg.inner_text("#main") or "API" in pg.inner_text("#main"))
        chk("no page errors", not errs, errs)
        b.close()
finally:
    srv.terminate()
print("\n%d/%d passed" % (sum(ok for _, ok in R), len(R)))
sys.exit(0 if all(ok for _, ok in R) else 1)
