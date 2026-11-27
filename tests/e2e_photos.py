"""Photo uploads never vanish silently: Receive waits for a photo that is still being prepared, a photo the phone can't
open says so, a refused upload shows as failed with Retry (and survives a reload), and the bar shows uploading/saved.
Run from the repo root: python3 tests/e2e_photos.py"""
import os,tempfile,subprocess,time,sys,requests,json,zlib,struct
D=tempfile.mkdtemp();env=dict(os.environ,DATA_DIR=D,TZ="Europe/Athens");U="http://127.0.0.1:18989"
pr=subprocess.Popen([sys.executable,"-m","uvicorn","app.main:app","--port","18989"],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
time.sleep(3)
def png(w,h):  # a big valid PNG so the phone-side shrink takes a moment
    raw=b"".join(b"\x00"+bytes((x*7+y*3)%256 for x in range(w))*3 for y in range(h))
    ch=lambda t,d:struct.pack(">I",len(d))+t+d+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
    return b"\x89PNG\r\n\x1a\n"+ch(b"IHDR",struct.pack(">IIBBBBB",w,h,8,2,0,0,0))+ch(b"IDAT",zlib.compress(raw,1))+ch(b"IEND",b"")
def until(pg,js,ms=10000):
    try: pg.wait_for_function(js,timeout=ms)
    except Exception: pass
def st(): return requests.get(U+"/api/state").json()
def prod(n): return [p for p in st()["prods"] if p["name"]==n]
ok=[];bad=[]
def check(name,cond,info=""):
    (ok if cond else bad).append(name);print(("PASS " if cond else "FAIL ")+name,("" if cond else info))
try:
    requests.post(U+"/api/sync",json={"ops":[{"kind":"receive","uuid":"a","product":"Towel","colour":"Red","size":"","lot":"","loc":1,"qty":10}]})
    big=png(2400,2400);small=png(40,40)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b=p.chromium.launch();ctx=b.new_context(viewport={"width":390,"height":844});pg=ctx.new_page();errs=[];pg.on("pageerror",lambda e:errs.append(str(e)))
        pg.goto(U+"/");time.sleep(1.5)
        # 1 Receive tapped while the photo is still being prepared: it waits, and the photo is saved with the item
        pg.click("#nav >> text=Receive");time.sleep(.3)
        pg.fill("#nm","Pic Item");pg.fill("#q","3")
        pg.evaluate("document.querySelector('details.more').open=true")
        pg.set_input_files("details.more input[type=file]",files=[{"name":"big.png","mimeType":"image/png","buffer":big}])
        pg.click("#rgo");time.sleep(4)
        check("receive waited for the photo",bool(prod("Pic Item")) and bool(prod("Pic Item")[0]["photo"]),str(prod("Pic Item")))
        # 2 a file the phone can't open is reported, nothing is queued
        pg.click("#nav >> text=Receive");time.sleep(.3);pg.evaluate("document.querySelector('details.more').open=true")
        pg.set_input_files("details.more input[type=file]",files=[{"name":"x.jpg","mimeType":"image/jpeg","buffer":b"not a picture"}]);time.sleep(1)
        check("unreadable photo is reported","couldn’t be opened" in pg.inner_text("#mb"),pg.inner_text("#mb"))
        check("  and Receive isn't left waiting",pg.evaluate("PHBUSY")==0 and pg.evaluate("PH")==None)
        # 3 a refused upload: failed bar with Retry, the picture is kept (also across a reload), Retry saves it
        def refuse(route):
            body=json.loads(route.request.post_data or "{}")
            if any(o.get("kind")=="photo" for o in body.get("ops",[])): route.fulfill(status=413,body='{"detail":"Picture too large"}',content_type="application/json")
            else: route.continue_()
        pg.route("**/api/sync",refuse)
        pg.click("#nav >> text=Stock");time.sleep(.5)
        pg.evaluate("chg(S.prods.find(p=>p.name=='Towel').id)");time.sleep(.3)
        pg.set_input_files("input.pickimg",files=[{"name":"s.png","mimeType":"image/png","buffer":small}]);until(pg,"!SY&&!PEND.length&&REJ.length")
        bar=pg.inner_text("#phbar")
        check("failed photo shows in the bar","didn’t upload" in bar and "Retry" in bar,bar)
        check("  Towel thumbnail marked failed",pg.locator("button.ph[title='Photo didn’t upload']").count()+pg.locator("img.th[title='Photo didn’t upload']").count()>0)
        pg.unroute("**/api/sync")
        pg.evaluate("localStorage.clear()");pg.reload();time.sleep(2.5)
        check("failed photo kept after reload",pg.evaluate("REJ.filter(x=>x.op.kind=='photo'&&x.op.data.indexOf('data:image')==0).length")==1)
        pg.click("#phbar button:has-text('Details')");time.sleep(.3)
        check("details list shows the reason","Picture too large" in pg.inner_text("#phov"),pg.inner_text("#phov")[:200])
        pg.click("#phov button:has-text('Retry')");time.sleep(2.5)
        check("retry saved it",bool(prod("Towel")[0]["photo"]) and pg.evaluate("REJ.length")==0)
        # the list re-renders after the retry's sync round-trip ends; wait for it instead of a fixed sleep
        until(pg,"(document.getElementById('phov')||{}).innerText.indexOf('Saved \u2713')>=0")
        check("  list now says saved","Saved ✓" in pg.inner_text("#phov"),pg.inner_text("#phov")[:200])
        pg.click("#phov button:has-text('Close')")
        check("  bar says saved","saved ✓" in pg.inner_text("#phbar"),pg.inner_text("#phbar"))
        # 4 offline: the photo waits (bar + dashed mark), then uploads on its own
        pg.route("**/api/sync",lambda r:r.abort())
        pg.evaluate("chg(S.prods.find(p=>p.name=='Pic Item').id)");time.sleep(.3)
        pg.set_input_files("input.pickimg",files=[{"name":"s2.png","mimeType":"image/png","buffer":small}]);until(pg,"PEND.length&&!SY&&!ONLINE")
        bar=pg.inner_text("#phbar")
        check("offline photo shows as waiting","waiting" in bar.lower(),bar)
        check("  thumbnail marked uploading",pg.locator("img.th[title='Photo still uploading']").count()>0)
        old=prod("Pic Item")[0]["photo"];pg.unroute("**/api/sync");pg.evaluate("sync()");time.sleep(2.5)
        check("  uploads once back online",prod("Pic Item")[0]["photo"]!=old and pg.evaluate("PEND.length")==0)
        # 5 a network-level 403 (e.g. an expired login in front of the app) is a sync problem, not a refusal
        pg.route("**/api/sync",lambda r:r.fulfill(status=403,body="forbidden"))
        pg.evaluate("queue({kind:'photo',product:'Towel',data:'data:image/jpeg;base64,/9j/'+'A'.repeat(200)})");time.sleep(1.5)
        check("403 keeps the photo pending",pg.evaluate("PEND.length")==1 and pg.evaluate("REJ.length")==0)
        pg.unroute("**/api/sync")
        check("no page errors",not errs,errs)
        b.close()
finally: pr.terminate()
print("\n%d/%d passed"%(len(ok),len(ok)+len(bad)));sys.exit(1 if bad else 0)
