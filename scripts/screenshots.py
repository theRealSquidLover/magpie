"""Take the README screenshots from a throwaway server filled with made-up demo data (never real data).
Run from the repo root:  python3 scripts/screenshots.py   (needs: pip install playwright requests; playwright install chromium)
Writes PNGs to docs/screenshots/."""
import base64, io, os, random, subprocess, sys, tempfile, time
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "screenshots")
PORT = 18990
U = "http://127.0.0.1:%d" % PORT
D = tempfile.mkdtemp()
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)], cwd=ROOT,
                       env=dict(os.environ, DATA_DIR=D, TZ="UTC"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
n = [0]


def op(kind, **k):
    n[0] += 1
    return dict(kind=kind, uuid="demo%d" % n[0], **k)


def swatch(rgb, seed):
    """A made-up fabric picture: woven texture plus a soft fold, drawn from scratch (no real photos)."""
    from PIL import Image, ImageDraw, ImageFilter
    rnd = random.Random(seed)
    w = h = 360
    im = Image.new("RGB", (w, h), rgb)
    d = ImageDraw.Draw(im)
    for i in range(0, w, 4):  # weave
        k = rnd.randint(-10, 10)
        d.line([(i, 0), (i, h)], fill=tuple(max(0, min(255, c + k)) for c in rgb))
    for j in range(0, h, 4):
        k = rnd.randint(-8, 8)
        d.line([(0, j), (w, j)], fill=tuple(max(0, min(255, c + k)) for c in rgb), width=1)
    shade = Image.new("L", (w, h), 0)
    sd = ImageDraw.Draw(shade)
    for x in range(w):  # fold: darker band across the cloth
        v = int(70 * max(0, 1 - abs(x - w * .62) / (w * .18)) ** 2)
        sd.line([(x, 0), (x, h)], fill=v)
    im = Image.composite(Image.new("RGB", (w, h), (0, 0, 0)), im, shade.filter(ImageFilter.GaussianBlur(12)))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()


COLOURS = {"White": (236, 234, 228), "Stone Grey": (150, 146, 140), "Navy": (38, 52, 92), "Sage": (150, 170, 140),
           "Ivory": (238, 230, 210), "Natural": (214, 200, 176), "Terracotta": (190, 100, 70), "Mustard": (214, 166, 50),
           "Teal": (30, 120, 128), "Blush": (232, 180, 176), "Denim": (70, 100, 140), "Black": (40, 40, 42)}


def seed():
    L = {l["name"]: l["id"] for l in requests.get(U + "/api/state").json()["locs"]}
    gnd, bas, store = L["Main: Ground"], L["Main: Basement"], L["Store"]
    requests.post(U + "/api/location/%d" % gnd, json={"role": "picking"})
    ops = []
    items = {  # product: {colour: [sizes]}, made-up textiles
        "Bath Towel": {"White": ["50x90", "70x140"], "Stone Grey": ["50x90", "70x140"], "Navy": ["70x140"]},
        "Duvet Cover": {"Sage": ["Single", "Double", "King"], "Ivory": ["Double", "King"]},
        "Linen Tablecloth": {"Natural": ["140x180", "140x240"], "Terracotta": ["140x180"]},
        "Cushion Cover": {"Mustard": ["45x45"], "Teal": ["45x45"], "Blush": ["45x45"]},
        "Kitchen Apron": {"Denim": [""], "Black": [""]},
    }
    q = 3
    for p, cols in items.items():
        for c, sizes in cols.items():
            for s in sizes:
                q = (q * 7) % 23 + 4
                ops.append(op("receive", product=p, colour=c, size=s, lot="L1", loc=bas, qty=q * 2, note="Container from supplier"))
                ops.append(op("receive", product=p, colour=c, size=s, lot="L2", loc=store, qty=q))
                ops.append(op("transfer", product=p, colour=c, size=s, lot="L1", loc=bas, to=gnd, qty=q // 2 + 1))
                ops.append(op("sale", product=p, colour=c, size=s, lot="L1", loc=gnd, qty=q // 3 + 1))
    r = requests.post(U + "/api/sync", json={"ops": ops}).json()
    bad = [x for x in r["results"] if x.get("status") not in ("ok", "dup", None)]
    assert not bad, bad[:3]
    for p, cols in items.items():
        c = list(cols)[0]
        requests.post(U + "/api/sync", json={"ops": [op("photo", product=p, data=swatch(COLOURS[c], p))]})
    requests.post(U + "/api/product/%d" % [x for x in requests.get(U + "/api/state").json()["prods"] if x["name"] == "Duvet Cover"][0]["id"],
                  json={"name": "Duvet Cover", "colour_photos": True})
    requests.post(U + "/api/sync", json={"ops": [op("photo", product="Duvet Cover", colour=c, data=swatch(COLOURS[c], c)) for c in ("Sage", "Ivory")]})
    st = requests.get(U + "/api/state").json()
    pid = {p["name"]: p["id"] for p in st["prods"]}
    requests.post(U + "/api/reorder", json={"product_id": pid["Bath Towel"], "field": "pick_min", "level": 12})
    requests.post(U + "/api/reorder", json={"product_id": pid["Duvet Cover"], "colour": "Sage", "field": "pick_min", "level": 8})


def main():
    from playwright.sync_api import sync_playwright
    os.makedirs(OUT, exist_ok=True)
    for _ in range(40):
        try:
            requests.get(U + "/api/health", timeout=1); break
        except Exception:
            time.sleep(.25)
    seed()
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, color_scheme="light", locale="en-GB")
        pg = ctx.new_page()
        pg.on("dialog", lambda d: d.dismiss())
        pg.goto(U + "/")
        pg.wait_for_selector("#nav button")
        time.sleep(1.2)

        def shot(name):
            time.sleep(.6)
            pg.screenshot(path=os.path.join(OUT, name + ".png"))

        def tab(t):
            if t in ("Stock", "Receive", "Sale/Adjust", "Transfer", "Report", "Log"):
                pg.click("#bnav >> text=Stock"); time.sleep(.2)
                pg.click("#nav >> text=" + t)
            else:
                pg.click("#bnav >> text=" + t)
            time.sleep(.5)

        tab("Stock"); shot("stock")
        pg.locator(".row:has-text('Duvet Cover')").first.click(); time.sleep(.4)
        shot("item")
        for t, f in (("Receive", "receive"), ("Sale/Adjust", "sale")):
            tab(t); shot(f)
        for t, f in (("Alerts", "alerts"), ("Warehouses", "warehouses")):
            tab(t); shot(f)
        b.close()


try:
    main()
finally:
    srv.terminate()
