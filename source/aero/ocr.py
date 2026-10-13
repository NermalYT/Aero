"""Text in images for models that can't see (1.3): Windows' own OCR engine (Windows.Media.Ocr, through the MIT
`winrt` packages; nothing to download, it uses the languages installed in Windows).

Used for image attachments when the loaded model has no vision, for scanned PDF pages (pypdf finds no text, so each
page's embedded image is read), and by the screenshot tools for text-only models. Elsewhere (macOS, Linux, or the
packages missing) available() is False and Aero says the image can't be read, as before.
"""
import asyncio
import io
import math
import sys
import threading

MAX_SIDE = 4000                 # Windows OCR's limit is about 10,000 px; text this size is still readable
_lock = threading.Lock()
_state = {"ok": None, "why": ""}


def available():
    if _state["ok"] is None:
        if sys.platform != "win32":
            _state.update(ok=False, why="Windows only")
        else:
            try:
                from winrt.windows.media.ocr import OcrEngine  # noqa: F401
                _state.update(ok=OcrEngine.try_create_from_user_profile_languages() is not None,
                              why="no OCR language installed in Windows")
            except Exception as e:  # noqa: BLE001
                _state.update(ok=False, why=f"winrt OCR packages missing ({type(e).__name__})")
    return _state["ok"]


def _png(data):
    """Any image Pillow reads -> (PNG bytes no larger than MAX_SIDE, scale from the original to the PNG)."""
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.load()
    if im.mode not in ("RGB", "RGBA", "L"):
        im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
    w, h = im.size
    s = 1.0
    if max(w, h) > MAX_SIDE:
        s = MAX_SIDE / max(w, h)
    elif max(w, h) < 600:               # tiny crops: OCR reads text better at a larger size
        s = 600 / max(w, h)
    if s != 1.0:
        im = im.resize((max(1, int(w * s)), max(1, int(h * s))))
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue(), s


async def _recognize(png):
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
    stream = InMemoryRandomAccessStream()
    w = DataWriter(stream)
    w.write_bytes(png)
    await w.store_async()
    w.detach_stream()
    stream.seek(0)
    dec = await BitmapDecoder.create_async(stream)
    bmp = await dec.get_software_bitmap_async()
    eng = OcrEngine.try_create_from_user_profile_languages()
    res = await eng.recognize_async(bmp)
    # with a skew angle, the boxes are in the deskewed image (rotated about its centre): turn them back
    a = math.radians(res.text_angle or 0.0)
    cx, cy = bmp.pixel_width / 2, bmp.pixel_height / 2

    def back(x, y):
        dx, dy = x - cx, y - cy
        return cx + dx * math.cos(a) - dy * math.sin(a), cy + dx * math.sin(a) + dy * math.cos(a)
    out = []
    for line in res.lines:
        rs = [wd.bounding_rect for wd in line.words]
        if not rs:
            continue
        x0, y0 = min(r.x for r in rs), min(r.y for r in rs)
        x1, y1 = max(r.x + r.width for r in rs), max(r.y + r.height for r in rs)
        if a:
            mx, my = back((x0 + x1) / 2, (y0 + y1) / 2)
            hw, hh = (x1 - x0) / 2, (y1 - y0) / 2
            x0, y0, x1, y1 = mx - hw, my - hh, mx + hw, my + hh
        out.append((line.text, (x0, y0, x1, y1)))
    return out


def _run(png):
    box = {}

    def go():
        try:
            box["r"] = asyncio.run(_recognize(png))
        except Exception:  # noqa: BLE001
            box["r"] = []
    with _lock:                          # one at a time: the engine is cheap, the bitmaps are not
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            go()
        else:                            # called from an event loop: OCR on a thread of its own
            t = threading.Thread(target=go, daemon=True)
            t.start()
            t.join(120)
    return box.get("r", [])


def lines(data):
    """[(text, (x0, y0, x1, y1))] in the original image's pixels, top to bottom; [] when OCR isn't available."""
    if not data or not available():
        return []
    try:
        png, s = _png(data)
    except Exception:  # noqa: BLE001
        return []
    return [(t, tuple(round(v / s) for v in box)) for t, box in _run(png)]


def image_text(data, limit=20000):
    """The text in an image (bytes of any common format), or "" when there is none or OCR isn't available."""
    return "\n".join(t for t, _ in lines(data)).strip()[:limit]


def screen_text(data, limit=6000, scale=1.0):
    """Text lines with their centre point, for a model that can't see a screenshot: 'text @ (x, y)'. scale divides
    the coordinates (screenshots shown smaller than the screen)."""
    out = []
    for t, (x0, y0, x1, y1) in lines(data):
        out.append(f"{t} @ ({round((x0 + x1) / 2 / scale)}, {round((y0 + y1) / 2 / scale)})")
    return "\n".join(out)[:limit]


def status():
    available()
    return {"available": bool(_state["ok"]), "why": "" if _state["ok"] else _state["why"], "engine": "Windows OCR"}
