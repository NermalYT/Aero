"""1.3: models without vision read the text of images, screenshots and scanned PDFs (Windows OCR).
Run from source/:
    python -m unittest discover -s tests -v
"""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aero import agent, attachments, ocr  # noqa: E402


def picture(lines, size=(1200, 400)):
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    try:
        f = ImageFont.truetype("arial.ttf", 36)
    except OSError:
        f = ImageFont.load_default()
    for (x, y), t in lines:
        d.text((x, y), t, fill="black", font=f)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


@unittest.skipUnless(ocr.available(), "Windows OCR not available here")
class RealOcr(unittest.TestCase):
    def test_reads_text_and_where_it_is(self):
        png = picture([((40, 40), "File  Edit  View"), ((700, 300), "Save changes")])
        got = ocr.lines(png)
        self.assertEqual([t for t, _ in got], ["File Edit View", "Save changes"])
        x0, y0, x1, y1 = got[1][1]
        self.assertTrue(700 <= (x0 + x1) / 2 <= 940 and 300 <= (y0 + y1) / 2 <= 345)

    def test_a_single_line_with_a_skew_guess_still_lands_on_the_text(self):
        """Windows OCR may guess a small skew; the boxes come back in the original picture."""
        (t, (x0, y0, x1, y1)), = ocr.lines(picture([((40, 40), "File  Edit  View")]))
        self.assertTrue(30 <= x0 <= 60 and 40 <= (y0 + y1) / 2 <= 80, (x0, y0, x1, y1))

    def test_from_inside_an_event_loop(self):
        import asyncio

        async def go():
            return ocr.image_text(picture([((40, 40), "Invoice 2209")]))
        self.assertIn("2209", asyncio.run(go()))


class TextModels(unittest.TestCase):
    def setUp(self):
        self.lines = [("Invoice 2209", (40, 40, 300, 80)), ("Total 1,250 USD", (40, 120, 360, 160))]
        p = [mock.patch.object(ocr, "available", lambda: True), mock.patch.object(ocr, "lines", lambda data: self.lines)]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def test_an_attached_image_becomes_text_for_a_model_without_vision(self):
        meta = attachments.save_upload("scan.png", picture([((40, 40), "x")]))
        msg = {"role": "user", "content": "What is the total?", "attachments": [{**meta, "kind": "image"}]}
        text = agent._user_content(msg, vision=False)
        self.assertIn("<image_text>\nInvoice 2209\nTotal 1,250 USD\n</image_text>", text)
        self.lines = []                                                       # cached: OCR runs once per image
        self.assertIn("Total 1,250 USD", agent._user_content(msg, vision=False))
        off = agent._user_content(msg, vision=False, ocr_on=False)
        self.assertIn("can't be seen", off)

    def test_ocr_happens_at_upload_never_on_the_event_loop(self):
        import asyncio
        meta = attachments.save_upload("a.png", picture([((40, 40), "x")]))
        self.assertTrue((attachments.UPLOADS / meta["id"] / "ocr.txt").exists())        # read when it arrived
        shot = attachments.save_image_bytes(picture([((40, 40), "y")]))               # screenshots: not at upload
        self.assertFalse((attachments.UPLOADS / shot["id"] / "ocr.txt").exists())

        async def on_loop():
            return attachments.ocr_text(shot["id"])
        self.assertEqual(asyncio.run(on_loop()), "")                                  # would block every request
        self.assertIn("Invoice 2209", attachments.ocr_text(shot["id"]))               # in a thread it's fine

    def test_read_file_on_an_image_gives_its_text(self):
        from aero import tools
        from aero.tools import files
        d = Path(tempfile.mkdtemp())
        (d / "scan.png").write_bytes(picture([((40, 40), "x")]))
        ctx = tools.Ctx({"work_dir": str(d)}, vision=False)
        self.assertIn("Total 1,250 USD", files.read_file(ctx, "scan.png")["text"])
        ctx.vision = True
        self.assertNotIn("OCR", files.read_file(ctx, "scan.png")["text"])

    def test_screen_text_gives_centres_in_shown_coordinates(self):
        self.assertEqual(ocr.screen_text(b"png").splitlines(), ["Invoice 2209 @ (170, 60)", "Total 1,250 USD @ (200, 140)"])
        self.assertEqual(ocr.screen_text(b"png", scale=2).splitlines()[0], "Invoice 2209 @ (85, 30)")

    def test_a_scanned_pdf_page_is_read(self):
        from pypdf import PdfWriter
        from PIL import Image
        im = Image.new("RGB", (400, 200), "white")
        b = io.BytesIO()
        im.save(b, "PDF")                                                      # an image-only page, like a scan
        w = PdfWriter()
        w.append(io.BytesIO(b.getvalue()))
        out = io.BytesIO()
        w.write(out)
        with mock.patch.object(ocr, "image_text", lambda data, limit=20000: "Invoice 2209\nTotal 1,250 USD"):
            txt = attachments._pdf(out.getvalue())
        self.assertIn("(text read with OCR)", txt)
        self.assertIn("Total 1,250 USD", txt)

    def test_screenshot_for_a_text_model(self):
        from aero import tools
        from aero.tools import desktop
        ctx = tools.Ctx({"ocr_for_text_models": True}, vision=False)
        png = picture([((10, 10), "x")], (800, 600))

        class Shot:
            size = (800, 600)
            bgra = __import__("PIL.Image", fromlist=["Image"]).open(io.BytesIO(png)).convert("RGBA").tobytes("raw", "BGRA")

        class Sct:
            monitors = [{"left": 0, "top": 0, "width": 800, "height": 600}] * 2

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def grab(self, m):
                return Shot()
        with mock.patch("mss.mss", Sct):
            r = desktop.screenshot(ctx, 1)
        self.assertIn("Invoice 2209 @ (170, 60)", r["text"])
        ctx.settings["ocr_for_text_models"] = False
        with mock.patch("mss.mss", Sct):
            self.assertIn("cannot see this image", desktop.screenshot(ctx, 1)["text"])


if __name__ == "__main__":
    unittest.main()
