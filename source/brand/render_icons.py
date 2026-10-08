"""Render the bubble icon masters to PNGs with Chromium, then build a multi-size .ico and a preview sheet."""
import sys, io, base64
from pathlib import Path
from playwright.sync_api import sync_playwright
from PIL import Image
brand = Path(sys.argv[1]); out = brand / "png"; out.mkdir(exist_ok=True)
big, small = (brand / "aero-bubble.svg").read_text(), (brand / "aero-bubble-small.svg").read_text()
SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 180, 192, 256, 512, 1024]
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
    pg = b.new_page()
    for s in SIZES:
        svg = small if s <= 32 else big
        uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
        pg.set_viewport_size({"width": s, "height": s})
        pg.set_content(f"<html><body style='margin:0;background:transparent'><img src='{uri}' style='width:{s}px;height:{s}px;display:block'></body></html>")
        pg.wait_for_timeout(80)
        pg.screenshot(path=str(out / f"aero-{s}.png"), omit_background=True)
    b.close()
imgs = {s: Image.open(out / f"aero-{s}.png").convert("RGBA") for s in SIZES}
ico_sizes = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
imgs[256].save(brand / "aero.ico", format="ICO", sizes=[(s, s) for s in ico_sizes],
               append_images=[imgs[s] for s in ico_sizes if s != 256])
# preview sheet: every size on light, taskbar-dark and Aero-blue backgrounds
row_h = 140; sheet = Image.new("RGBA", (1500, row_h * 3), (0, 0, 0, 0))
for r, bg in enumerate([(245, 248, 252, 255), (22, 28, 38, 255), (40, 120, 210, 255)]):
    band = Image.new("RGBA", (1500, row_h), bg); x = 12
    for s in [16, 20, 24, 32, 40, 48, 64, 96, 128]:
        band.alpha_composite(imgs[s], (x, (row_h - s) // 2)); x += s + 26
    sheet.alpha_composite(band, (0, r * row_h))
sheet.save(brand / "png" / "preview-sizes.png")
ico = Image.open(brand / "aero.ico"); print("ico sizes:", sorted(ico.info.get("sizes", [])))
