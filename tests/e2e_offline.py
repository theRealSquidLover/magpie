"""Offline/sync robustness in a headless browser: outbox kept in IndexedDB, server errors shown as a sync problem
(not 'offline'), a refused photo doesn't block the queue, whole-number quantities, n/a rows editable.
Run from the repo root: python3 tests/e2e_offline.py"""
import os,tempfile,subprocess,time,sys,requests,json
D=tempfile.mkdtemp();env=dict(os.environ,DATA_DIR=D,TZ="Europe/Athens");U="http://127.0.0.1:18988"
pr=subprocess.Popen([sys.executable,"-m","uvicorn","app.main:app","--port","18988"],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _ in range(300):
    try: requests.get(U+"/api/health",timeout=1);break
    except requests.RequestException: time.sleep(.1)
def st(): return requests.get(U+"/api/state").json()
try:
    requests.post(U+"/api/sync",json={"ops":[{"kind":"receive","uuid":"a","product":"Towel","colour":"Red","size":"1","lot":"","loc":1,"qty":10},
        {"kind":"receive","uuid":"b","product":"Plain","colour":"","size":"","lot":"","loc":1,"qty":4}]})
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b=p.chromium.launch();ctx=b.new_context(viewport={"width":390,"height":844});pg=ctx.new_page();errs=[];pg.on("pageerror",lambda e:errs.append(str(e)))
        pg.goto(U+"/");time.sleep(1.5)
        # 1 outbox survives losing localStorage: kept in IndexedDB
        block={"on":True}
        def h(route):
            if block["on"]: route.abort()
            else: route.continue_()
        pg.route("**/api/sync",h)
        pg.evaluate("queue({kind:'sale',product:'Towel',colour:'Red',size:'1',lot:'',loc:1,qty:2})");time.sleep(.8)
        pg.evaluate("localStorage.clear()");pg.reload();time.sleep(2)
        n=pg.evaluate("PEND.length");print("1 restored from IndexedDB:",n,"| header:",pg.inner_text("#hdr")[:40]);assert n==1,n
        block["on"]=False;pg.evaluate("sync()");time.sleep(2);s1=[m["qty"] for m in st()["mv"] if m["type"]=="sale"];print("  synced once:",s1);assert s1==[-2],s1
        pg.unroute("**/api/sync")
        # 2 a server error is a sync problem, not 'offline'
        pg.route("**/api/state",lambda r: r.fulfill(status=500,body='{"detail":"database is locked"}',content_type="application/json"))
        pg.evaluate("sync()");time.sleep(1.5);hd=pg.inner_text("#hdr");print("2 header on server error:",hd[:60]);assert "sync problem" in hd and "database is locked" in hd,hd;pg.unroute("**/api/state")
        pg.evaluate("sync()");time.sleep(1.5);hd=pg.inner_text("#hdr");print("  recovered:",hd[:20]);assert "online" in hd and "problem" not in hd,hd
        # 3 a refused photo doesn't block the queue
        def h3(route):
            body=json.loads(route.request.post_data or "{}")
            if any(o.get("kind")=="photo" for o in body.get("ops",[])): route.fulfill(status=413,body='{"detail":"Picture too large"}',content_type="application/json")
            else: route.continue_()
        pg.route("**/api/sync",h3)
        pg.evaluate("queue({kind:'photo',product:'Towel',data:'data:image/jpeg;base64,/9j/'});queue({kind:'sale',product:'Towel',colour:'Red',size:'1',lot:'',loc:1,qty:1})");pg.wait_for_timeout(4000)
        n3,r3,s3=pg.evaluate("PEND.length"),pg.evaluate("REJ.map(x=>x.op.kind+': '+x.msg)"),[m["qty"] for m in st()["mv"] if m["type"]=="sale"];print("3 pending:",n3,"| rejected:",r3,"| sales:",s3)
        assert n3==0 and r3==["photo: Picture too large"] and s3==[-2,-1],(n3,r3,s3)
        pg.unroute("**/api/sync")
        # 4 decimals rounded down; minus in Sale refused; n/a edit saves the levels
        pg.click("#nav >> text=Sale/Adjust");time.sleep(.3);pg.fill("#fl","towel");time.sleep(.3);pg.locator("#sel .ln").first.click();pg.fill("#q","2.9");pg.click("button:has-text('Record')");time.sleep(1.5)
        s4=[m["qty"] for m in st()["mv"] if m["type"]=="sale"][-1:];print("4 2.9 sold as:",s4);assert s4==[-2],s4
        assert pg.evaluate("whole('2.9')")==2
        pg.fill("#fl","towel");time.sleep(.3);pg.locator("#sel .ln").first.click();pg.fill("#q","-5");pg.click("button:has-text('Record')");time.sleep(.5);mm=pg.inner_text("#mb");print("  minus:",mm[:60]);assert "positive" in mm,mm
        pg.click("#nav >> text=Stock");time.sleep(.4);pg.locator(".row:has-text('Plain')").first.click();time.sleep(.2)
        na=pg.locator(".sub").filter(has_text="n/a").count()>0;print("  n/a label:",na);assert na
        pg.locator(".sub:has-text('n/a') button:has-text('Edit')").first.click();time.sleep(.2);pg.fill("#er","3");pg.click(".sub button:has-text('Save')");time.sleep(1.5)
        ro=[v["reorder"] for v in st()["vars"] if v["colour"]=="" and v["size"]==""];print("  saved:",pg.inner_text("#mb")[:50],"| reorder:",ro);assert ro==[3],ro
        print("errors:",errs);assert not errs,errs;b.close()
finally: pr.terminate()
print("all passed")
