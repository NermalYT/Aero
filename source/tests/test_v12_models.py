"""1.2 model pulling and the built-in integrations: any Hugging Face model resolves to the best GGUF (or Aero makes
one), downloads are checked against the Hub's SHA-256, files Aero makes are downloadable from the chat, and every
integration is set up with Aero in a sensible order.
Run from source/:
    python -m unittest discover -s tests -v
"""
import hashlib
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aero import hf, made_files, quantize  # noqa: E402
from aero.integrations import Pack, Requirement, bundle  # noqa: E402


class QuantPick(unittest.TestCase):
    def test_sizes(self):
        vram, ram = 15700, 64 * 1024
        self.assertEqual(quantize.pick(8e9, vram, ram)[0], "Q8_0")          # small: best quality, all on the GPU
        q, imx, why = quantize.pick(70e9, vram, ram)
        self.assertEqual((q, imx), ("Q4_K_M", False))                       # big: Q4_K_M split with RAM
        self.assertIn("RAM", why)
        q, imx, _ = quantize.pick(235e9, vram, ram)
        self.assertTrue(imx)                                                # huge: low-bit with an importance matrix
        self.assertTrue(q.startswith("IQ"))
        q, _, why = quantize.pick(1000e9, vram, 16 * 1024)
        self.assertEqual(q, quantize.LADDER[-1][0])
        self.assertIn("AirLLM", why)

    def test_converter_files_checked_by_git_hash(self):
        self.assertEqual(quantize._blob_sha(b"hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a")   # git hash-object
        tree = {"tree": [{"type": "blob", "path": "convert_hf_to_gguf.py", "sha": quantize._blob_sha(b"main\n")},
                         {"type": "blob", "path": "conversion/llama.py", "sha": quantize._blob_sha(b"x\n")},
                         {"type": "blob", "path": "gguf-py/gguf/gguf.py", "sha": quantize._blob_sha(b"g\n")},
                         {"type": "blob", "path": "gguf-py/gguf/scripts/dump.py", "sha": "0"},
                         {"type": "blob", "path": "ggml/src/big.c", "sha": "0"}]}
        files = {"convert_hf_to_gguf.py": b"main\n", "conversion/llama.py": b"x\n", "gguf-py/gguf/gguf.py": b"g\n"}
        got = []

        class C:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def get(self, url, params=None):
                if "api.github.com" in url:
                    return mock.Mock(json=lambda: tree, raise_for_status=lambda: None)
                path = url.split("/b1/", 1)[1]
                got.append(path)
                return mock.Mock(content=files[path])
        d = Path(tempfile.mkdtemp())
        with mock.patch.object(quantize.httpx, "Client", C):
            quantize.fetch_converter("b1", d)
        self.assertEqual(sorted(got), sorted(files))                 # no scripts/, no C sources
        self.assertEqual((d / "conversion" / "llama.py").read_bytes(), b"x\n")
        self.assertTrue((d / ".complete").exists())

    def test_cpu_only(self):
        self.assertEqual(quantize.pick(8e9, 0, 64 * 1024, gpu=False)[0], "Q8_0")


class Versions(unittest.TestCase):
    def test_same_model_from_known_quantizers_first(self):
        rows = [{"id": "someone/Model-7B-Instruct-roleplay-GGUF", "downloads": 9000, "tags": ["gguf"]},
                {"id": "nobody/Model-7B-Instruct-GGUF", "downloads": 5000, "tags": ["gguf"]},
                {"id": "bartowski/Model-7B-Instruct-GGUF", "downloads": 100, "tags": ["gguf"]}]
        r = mock.Mock(status_code=200, json=lambda: rows)
        r.raise_for_status = lambda: None
        with mock.patch.object(hf.httpx, "get", return_value=r):
            v = hf.gguf_versions("org/Model-7B-Instruct")
        self.assertEqual([x["id"] for x in v], ["bartowski/Model-7B-Instruct-GGUF", "nobody/Model-7B-Instruct-GGUF",
                                               "someone/Model-7B-Instruct-roleplay-GGUF"])
        self.assertFalse(v[-1]["same"])

    def test_search_marks_gguf_and_size(self):
        rows = [{"id": "meta/Llama-3.3-70B-Instruct", "tags": ["transformers"], "pipeline_tag": "text-generation"},
                {"id": "x/Llama-3.3-70B-Instruct-GGUF", "tags": ["gguf"], "pipeline_tag": "text-generation"},
                {"id": "y/whisper-large", "tags": [], "pipeline_tag": "automatic-speech-recognition"}]
        r = mock.Mock(status_code=200, json=lambda: rows)
        r.raise_for_status = lambda: None
        with mock.patch.object(hf.httpx, "get", return_value=r) as g:
            out = hf.search("llama 70b")
        self.assertNotIn("filter", g.call_args.kwargs["params"])            # any model, not only GGUF repos
        self.assertEqual([(x["gguf"], x["size_b"]) for x in out], [(False, 70.0), (True, 70.0)])   # speech model dropped


class DownloadCheck(unittest.TestCase):
    def test_sha256(self):
        p = Path(tempfile.mkdtemp()) / "f.bin"
        p.write_bytes(b"abc" * 1000)
        self.assertEqual(hf.sha256_file(p), hashlib.sha256(b"abc" * 1000).hexdigest())
        ev = threading.Event()
        ev.set()
        with self.assertRaises(RuntimeError):
            hf.sha256_file(p, ev)


class MadeFiles(unittest.TestCase):
    def test_register_and_serve_only_registered(self):
        d = Path(tempfile.mkdtemp())
        doc = d / "Meetings 2026-10-12.docx"
        doc.write_bytes(b"PK..")
        f = made_files.register(doc)
        self.assertEqual(f["name"], doc.name)
        self.assertEqual(made_files.path_of(f["id"]), doc.resolve())
        self.assertIsNone(made_files.register(d / "tool.exe"))
        self.assertIsNone(made_files.path_of("0" * 16))
        env = {"changed_state": {"path": str(doc)}}
        self.assertEqual(made_files.from_envelope(env)["id"], f["id"])

    def test_endpoint(self):
        from fastapi.testclient import TestClient

        from aero import server
        d = Path(tempfile.mkdtemp())
        doc = d / "schedule.csv"
        doc.write_text("day,time\n", encoding="utf-8")
        f = made_files.register(doc)
        c = TestClient(server.app)
        r = c.get("/api/made/" + f["id"])
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertEqual(c.get("/api/made/../../etc").status_code, 404)
        self.assertEqual(c.get("/api/made/" + "f" * 16).status_code, 404)

    def test_ui_files_revalidate(self):
        """After an update the window must not run an older app.js against the new server."""
        from fastapi.testclient import TestClient

        from aero import server
        c = TestClient(server.app)
        r = c.get("/app.js")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["cache-control"], "no-cache")
        for path in ("/", "/index.html"):
            page = c.get(path)
            self.assertEqual(page.headers["cache-control"], "no-cache")
            v = server.VERSION
            for asset in ("app.js", "workspaces.js", "app.css", "vendor/marked.js"):
                self.assertIn(f'"{asset}?v={v}"', page.text, (path, asset))   # a new version = new URLs
            self.assertIn('href="#cloud"', page.text)                            # in-page refs untouched
        self.assertEqual(c.get("/app.js?v=x").status_code, 200)


class Bundle(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp()) / "bundle.json"
        self.p = mock.patch.object(bundle, "STATE", self.state)
        self.p.start()
        self.installed = []

    def tearDown(self):
        self.p.stop()

    def fake_packs(self, fail=()):
        have = set()

        def req(pid, rid, size=10):
            def install(emit, cancel):
                if rid in fail:
                    raise RuntimeError("network down")
                self.installed.append(f"{pid}:{rid}")
                have.add(rid)
                return "ok"
            return Requirement(rid, rid, lambda: (rid in have, ""), install, size_mb=size)
        return {"image": Pack("image", "Image", "create", "", "", "", requirements=[req("image", "models", 6400),
                                                                                    req("image", "engine")]),
                "hindsight": Pack("hindsight", "Hindsight", "memory", "", "", "",
                                  requirements=[req("hindsight", "hindsight", 600)]),
                "e2e": Pack("e2e", "e2e", "review", "", "", "", requirements=[req("e2e", "e2e")]),
                "essentials": Pack("essentials", "Essentials", "code", "", "", "",
                                   requirements=[req("essentials", "rust", 300), req("essentials", "node")]),
                "xtool": Pack("xtool", "xtool", "mobile", "", "", "", requirements=[req("xtool", "xtool")])}

    def test_order_and_skip(self):
        with mock.patch.object(bundle, "PACKS", self.fake_packs()), mock.patch.object(bundle, "load_all", lambda: None):
            keys = [k for k, _, _ in bundle.items()]
            self.assertEqual(keys[0], "essentials:node")                      # npm packs need Node first
            self.assertNotIn("xtool:xtool", keys)                             # changes WSL: opt-in only
            self.assertEqual(keys[-1], "image:models")                        # the 6 GB download last
            ok, failed = bundle.run()
            self.assertEqual((ok, failed), (6, 0))
            self.assertEqual(self.installed[0], "essentials:node")
            self.assertEqual(bundle.status()["done_mb"], bundle.status()["total_mb"])

    def test_failures_retry_a_few_times_then_wait(self):
        with mock.patch.object(bundle, "PACKS", self.fake_packs(fail={"hindsight"})), \
                mock.patch.object(bundle, "load_all", lambda: None):
            for _ in range(bundle.MAX_TRIES):
                bundle.run(background=True)
            st = {x["key"]: x for x in bundle.status()["items"]}
            self.assertEqual(st["hindsight:hindsight"]["state"], "failed")
            self.assertIn("network down", st["hindsight:hindsight"]["error"])
            self.installed.clear()
            self.assertEqual(bundle.run(background=True), (0, 0))            # left for the user (or Retry)
            self.assertEqual(bundle.run(), (0, 1))                           # the installer's run still tries


class DefaultBrowser(unittest.TestCase):
    """Aero's browser and its window use the user's default browser when it is Chromium-based (owner: "mine is
    literally set to chrome", and Aero opened Edge)."""

    @unittest.skipUnless(sys.platform == "win32", "Windows registry")
    def test_windows_user_choice(self):
        import winreg

        from aero import osinfo
        d = Path(tempfile.mkdtemp())
        chrome = d / "chrome.exe"
        chrome.write_bytes(b"")
        firefox = d / "firefox.exe"
        firefox.write_bytes(b"")
        choice = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
        vals = {}

        class K:
            def __init__(self, path):
                self.path = path

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        def open_key(root, path):
            if path not in vals:
                raise OSError(path)
            return K(path)

        def query(k, name):
            return vals[k.path][name], 1
        with mock.patch.object(winreg, "OpenKey", open_key), mock.patch.object(winreg, "QueryValueEx", query):
            vals.update({choice: {"ProgId": "ChromeHTML"},
                         r"ChromeHTML\shell\open\command": {"": f'"{chrome}" --single-argument %1'}})
            self.assertEqual(osinfo.default_browser(), str(chrome))
            self.assertEqual(osinfo.find_browser(), ([str(chrome)], False))
            vals.update({choice: {"ProgId": "FirefoxURL"},
                         r"FirefoxURL\shell\open\command": {"": f'"{firefox}" -osint -url "%1"'}})
            self.assertIsNone(osinfo.default_browser())                    # not Chromium: the usual search instead

    def test_signin_pages(self):
        from aero.tools import browser
        for u in ("https://accounts.google.com/v3/signin/identifier", "https://login.microsoftonline.com/common/oauth2",
                  "https://github.com/login?return_to=%2Fnotifications",
                  "https://outlook.live.com/mail/?prompt=select_account&deeplink=mail%2F0%2F",
                  "https://www.microsoft.com/en-us/microsoft-365/outlook/email-and-calendar-software-microsoft-outlook"
                  "?deeplink=%2Fmail%2F0%2F&sdf=0"):
            self.assertTrue(browser._SIGNIN.search(u), u)
        for u in ("https://github.com/notifications", "https://mail.google.com/mail/u/0/#inbox",
                  "https://www.microsoft.com/en-us/microsoft-365/outlook"):
            self.assertFalse(browser._SIGNIN.search(u), u)


if __name__ == "__main__":
    unittest.main()
