"""Tests for whole-page reading (extraction.py), fetch_url's sections, and the browser's strict-offline request
blocking. The extraction runs on fixed HTML fixtures (no network). An optional real-browser check runs the in-page
extractor in Aero's Playwright browser on a local page when Playwright and a Chromium-family browser are installed.
Run from source/:
    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from aero import config, extraction, localonly, tools  # noqa: E402

tools.load_all()


def long_doc(sections=40, para=6):
    nav = "<nav>" + "".join(f'<a href="/doc{i}">Doc page {i}</a> ' for i in range(30)) + \
          "<p>Home Guides API Reference Changelog Community Support Status Pricing Blog Careers</p></nav>"
    body = []
    for s in range(1, sections + 1):
        body.append(f"<h2 id='s{s}'>Section {s}: topic {s}</h2>")
        for p in range(para):
            body.append(f"<p>Paragraph {p} of section {s}. The setting maxRetries{s} defaults to {s * 10 + p} and "
                        f"controls how often requests are retried before giving up.</p>")
    body.append("<h2>Limits</h2><table><caption>Rate limits</caption><tr><th>Plan</th><th>Requests</th><th>Docs</th></tr>"
                "<tr><td>Free</td><td>60/min</td><td><a href='/free'>free</a></td></tr>"
                "<tr><td>Team</td><td>600/min</td><td><a href='/team'>team</a></td></tr></table>")
    body.append("<p>The secret value at the very bottom is 7781.</p>")
    foot = "<footer><p>Copyright 2026 Example Inc. All rights reserved. Terms Privacy Cookies Sitemap.</p></footer>"
    return (f"<html lang='en'><head><title>Retry guide</title><link rel='canonical' href='https://docs.example/retry'>"
            f"</head><body><header>{nav}</header><main><h1>Retry guide</h1>{''.join(body)}</main>{foot}</body></html>")


class Extraction(unittest.TestCase):
    def test_whole_long_page_with_provenance(self):
        raw = extraction.from_html(long_doc(), "https://docs.example/retry?x=1")
        page = extraction.build(raw, "test")
        self.assertGreaterEqual(page["total_chars"], 40 * 6 * 100)
        ids = [s["id"] for s in page["sections"]]
        # everything is there, including what a first screen would never show
        self.assertTrue(any("7781" in s["text"] for s in page["sections"]))
        self.assertEqual(page["canonical"], "https://docs.example/retry")
        chosen, info = extraction.select(page, budget=6000)
        self.assertFalse(info["complete"])
        self.assertTrue(info["left_out"])
        text = extraction.render(page, chosen, info)
        self.assertIn("Source: https://docs.example/retry?x=1", text)
        self.assertIn("partial:", text)
        self.assertIn("not shown:", text)
        # the exact text of any section can be asked for by id
        last = ids[-1]
        chosen, info = extraction.select(page, ids=[last])
        self.assertIn("7781", chosen[0]["text"])

    def test_query_finds_deep_sections(self):
        page = extraction.build(extraction.from_html(long_doc(), "https://docs.example/retry"), "test")
        chosen, info = extraction.select(page, query="maxRetries37 default", budget=4000)
        self.assertTrue(chosen)
        self.assertIn("Section 37", chosen[0]["heading"])
        self.assertEqual(info["matched"] is not None, True)

    def test_navigation_and_footer_left_out_but_counted(self):
        page = extraction.build(extraction.from_html(long_doc(4, 2), "https://docs.example/retry"), "test")
        allt = " ".join(s["text"] for s in page["sections"])
        self.assertNotIn("Careers", allt)
        self.assertNotIn("All rights reserved", allt)
        self.assertGreaterEqual(page["dropped"]["boilerplate"], 2)

    def test_table_cells_and_links(self):
        raw = extraction.from_html(long_doc(2, 1), "https://docs.example/retry")
        t = extraction.tables(raw)[0]
        self.assertEqual(t["caption"], "Rate limits")
        self.assertEqual(t["rows"][0], ["Plan", "Requests", "Docs"])
        self.assertEqual(t["rows"][2][:2], ["Team", "600/min"])
        page = extraction.build(raw, "test")
        sec = next(s for s in page["sections"] if s["heading"] == "Limits")
        self.assertIn("| Team | 600/min | team |", sec["text"])

    def test_pagination_without_repeats_or_omissions(self):
        seen = set()
        items = []
        for pg in range(3):
            html = "<main><h1>Results</h1>" + "".join(
                f"<li>Result {i}: product number {i} with a description long enough to count</li>"
                for i in range(pg * 10, pg * 10 + 12)) + "</main>"        # each page repeats two items of the next
            page = extraction.build(extraction.from_html(html, f"https://shop.example/?page={pg}"), "test", seen=seen)
            for s in page["sections"]:
                items += [ln for ln in s["text"].splitlines() if ln.startswith("- Result")]
        nums = [int(x.split()[2].rstrip(":")) for x in items]
        self.assertEqual(sorted(set(nums)), list(range(32)))
        self.assertEqual(len(nums), len(set(nums)))                     # no item twice

    def test_unreadable_frames_and_cut_pages_are_flagged(self):
        raw = extraction.from_html("<main><h1>A</h1><p>Short text that is still a paragraph of words.</p>"
                                   "<iframe src='https://ads.example/x'></iframe></main>", "https://a.example/")
        raw["truncated"] = True
        page = extraction.build(raw, "test")
        chosen, info = extraction.select(page)
        text = extraction.render(page, chosen, info)
        self.assertIn("could not be read: https://ads.example/x", text)
        self.assertIn("content after the limit was NOT read", text)
        self.assertFalse(info["complete"])


class FetchUrl(unittest.TestCase):
    def test_sections_and_cache(self):
        html = long_doc(20, 3)
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})
        real = httpx.get

        def get(url, **kw):
            with httpx.Client(transport=httpx.MockTransport(handler)) as c:
                return c.get(url, follow_redirects=True)
        ctx = tools.Ctx(config.load_settings(), chat_id="fetch-test")
        with mock.patch("aero.tools.web.httpx.get", get):
            out = tools.run("fetch_url", {"url": "https://docs.example/retry", "max_chars": 3000}, ctx)
            self.assertIn("Outline:", out["text"])
            self.assertIn("partial:", out["text"])
            out2 = tools.run("fetch_url", {"url": "https://docs.example/retry", "sections": ["s7"]}, ctx)
        self.assertEqual(len(calls), 1)                               # sections came from the cached page
        self.assertIn("cached from the earlier fetch", out2["text"])
        self.assertIs(real, httpx.get)

    def test_strict_offline_blocks_at_the_transport(self):
        localonly.install()
        s = config.load_settings()
        config.save_settings({"strict_offline": True})
        try:
            ctx = tools.Ctx(config.load_settings(), chat_id="offline-test")
            out = tools.run("fetch_url", {"url": "https://example.com/"}, ctx)
            self.assertTrue(out["error"])
            self.assertIn("strict offline", out["text"].lower())             # refused before it runs
            self.assertIn("strict offline", tools.run("browser_read_sections", {}, ctx)["text"].lower())
            with self.assertRaises(localonly.OfflineBlocked):                # and blocked at the transport anyway
                httpx.get("https://example.com/", timeout=5)
        finally:
            config.save_settings({"strict_offline": s.get("strict_offline", False)})


class BrowserRouting(unittest.TestCase):
    """The browser's own request filter: under strict offline, nothing but this computer is reached."""

    class Route:
        def __init__(self, url):
            self.request = type("R", (), {"url": url, "method": "GET"})()
            self.did = None

        def continue_(self):
            self.did = "continue"

        def abort(self, why=""):
            self.did = "abort"

    def test_offline_route(self):
        from aero.tools import browser
        with mock.patch.object(localonly, "strict", lambda: True):
            for url, want in (("https://example.com/a", "abort"), ("http://127.0.0.1:8180/", "continue"),
                              ("http://localhost/x", "continue"), ("data:text/plain,hi", "continue")):
                r = self.Route(url)
                browser._offline_route(r)
                self.assertEqual(r.did, want, url)
        with mock.patch.object(localonly, "strict", lambda: False):
            r = self.Route("https://example.com/a")
            browser._offline_route(r)
            self.assertEqual(r.did, "continue")


def _playwright_ready():
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    from aero import osinfo
    return bool(osinfo.browser_executable()) or os.environ.get("AERO_LIVE_BROWSER") == "1"


@unittest.skipUnless(_playwright_ready() and os.environ.get("AERO_LIVE_BROWSER") == "1",
                     "real browser check: set AERO_LIVE_BROWSER=1 with Playwright and Chrome/Edge installed")
class RealBrowser(unittest.TestCase):
    """Aero's own browser, in the background, on a page served from this computer."""

    @classmethod
    def setUpClass(cls):
        page = long_doc(60, 5).encode()
        spa = (b"<html><body><main><h1>Form</h1><input id='name' aria-label='Name'><button id='save' "
               b"onclick=\"document.getElementById('out').textContent='Saved: '+document.getElementById('name').value\">"
               b"Save</button><p id='out'>Nothing saved</p></main></body></html>")

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = spa if self.path.startswith("/form") else page
                self.send_response(200)
                self.send_header("content-type", "text/html")
                self.end_headers()
                self.wfile.write(body)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.ctx = tools.Ctx(config.load_settings(), chat_id="live-browser")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_long_page_below_the_fold(self):
        out = tools.run("browser_open", {"url": self.base + "/doc"}, self.ctx)
        self.assertFalse(out["error"], out["text"][:400])
        out = tools.run("browser_read_sections", {"query": "secret value bottom"}, self.ctx)
        self.assertIn("7781", out["text"])
        self.assertIn(f"Source: {self.base}/doc", out["text"])
        tab = json.loads(tools.run("browser_extract", {"kind": "tables"}, self.ctx)["text"].split("all rows:\n", 1)[1])
        self.assertEqual(tab[0]["rows"][2][:2], ["Team", "600/min"])

    def test_form_with_refs_and_stale_ref(self):
        snap = tools.run("browser_open", {"url": self.base + "/form"}, self.ctx)["text"]
        ref = int(next(ln for ln in snap.splitlines() if 'aria-label' in ln or '"Name"' in ln).split("]")[0][1:])
        r = tools.run("browser_type", {"ref": ref, "text": "Pat"}, self.ctx)
        self.assertIs(r["envelope"]["verified"], True)
        save = int(next(ln for ln in r["text"].splitlines() if '"Save"' in ln).split("]")[0][1:])
        r = tools.run("browser_click", {"ref": save}, self.ctx)
        self.assertIn("Saved: Pat", r["text"])
        from aero.tools.browser import B
        # the page navigates by itself (a redirect, a timer) after the model's last snapshot
        B.call(lambda: B.page("live-browser").goto(self.base + "/doc", wait_until="domcontentloaded"))
        stale = tools.run("browser_click", {"ref": save}, self.ctx)
        self.assertTrue(stale["error"])
        self.assertIn("not on the page", stale["text"])


if __name__ == "__main__":
    unittest.main()
