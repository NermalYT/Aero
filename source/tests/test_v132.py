"""1.3.2: Aero's own window and title bar, projects, the Files and Changes views, and a settings window where edits
survive switching tabs.
Run from source/:
    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aero import projects  # noqa: E402

STATIC = Path(projects.__file__).parent / "static"


class Projects(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="proj132-"))
        self.file = Path(tempfile.mkdtemp(prefix="pstore132-")) / "projects.json"
        p = mock.patch.object(projects, "FILE", self.file)
        p.start()
        self.addCleanup(p.stop)

    def test_create_update_delete(self):
        p = projects.create("", str(self.dir), "Run the tests with pytest.")
        self.assertEqual(p["name"], self.dir.name)               # the folder's name when none is given
        self.assertEqual(projects.get(p["id"])["path"], str(self.dir.resolve()))
        projects.update(p["id"], name="Mod", instructions="Use Gradle.")
        self.assertEqual(projects.get(p["id"])["name"], "Mod")
        self.assertTrue(projects.delete(p["id"]))
        self.assertEqual(projects.all(), [])
        self.assertTrue(self.dir.is_dir())                          # the folder is never touched

    def test_a_folder_must_exist_and_be_absolute(self):
        with self.assertRaises(ValueError):
            projects.create("x", str(self.dir / "missing"))
        with self.assertRaises(ValueError):
            projects.create("x", "relative/folder")
        with self.assertRaises(ValueError):
            projects.create("x", "")

    def test_a_chat_in_a_project_works_in_its_folder_with_its_instructions(self):
        p = projects.create("Mod", str(self.dir), "Never edit build.gradle.")
        s = projects.chat_settings({"system_prompt": "You are Aero.", "work_dir": "C:/elsewhere"}, "c1", p["id"])
        self.assertEqual(s["work_dir"], p["path"])
        self.assertTrue(s["system_prompt"].startswith("You are Aero."))
        self.assertIn("# Project: Mod", s["system_prompt"])
        self.assertIn("Never edit build.gradle.", s["system_prompt"])
        same = projects.chat_settings({"system_prompt": "You are Aero.", "work_dir": "C:/elsewhere"}, "c1", p["id"])
        self.assertEqual(s["system_prompt"], same["system_prompt"])  # fixed per project: the prompt cache holds
        plain = {"system_prompt": "x", "work_dir": "w"}
        self.assertIs(projects.chat_settings(plain, "nochat", None), plain)
        self.assertIs(projects.chat_settings(plain, "nochat", "deleted-project"), plain)

    def test_the_chat_file_says_which_project(self):
        p = projects.create("Mod", str(self.dir))
        chats = Path(tempfile.mkdtemp(prefix="chats132-"))
        (chats / "c9.json").write_text(json.dumps({"id": "c9", "project": p["id"], "messages": []}), encoding="utf-8")
        with mock.patch.object(projects, "CHATS", chats):
            self.assertEqual(projects.chat_settings({"work_dir": "w"}, "c9")["work_dir"], p["path"])


class FileView(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="fv132-")).resolve()
        (self.root / "src").mkdir()
        (self.root / "src" / "main.py").write_bytes(b"print('hi')\n")
        (self.root / "notes.md").write_text("# Notes\n", encoding="utf-8")
        (self.root / "blob.bin").write_bytes(b"\0\1\2" * 100)
        (self.root / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 20)
        (self.root / "node_modules").mkdir()
        self.outside = Path(tempfile.mkdtemp(prefix="secret132-")).resolve()
        (self.outside / "secret.txt").write_text("no", encoding="utf-8")

    def test_listing_puts_folders_first_and_marks_heavy_ones(self):
        d = projects.listing(self.root, "")
        names = [e["name"] for e in d["entries"]]
        self.assertEqual(names[:2], ["node_modules", "src"])
        self.assertTrue(next(e for e in d["entries"] if e["name"] == "node_modules")["skip"])
        self.assertEqual([e["name"] for e in projects.listing(self.root, "src")["entries"]], ["main.py"])

    def test_reading_kinds(self):
        t = projects.read(self.root, "src/main.py")
        self.assertEqual((t["kind"], t["ext"], t["text"]), ("text", "py", "print('hi')\n"))
        self.assertEqual(projects.read(self.root, "blob.bin")["kind"], "binary")
        self.assertEqual(projects.read(self.root, "pic.png")["kind"], "media")
        self.assertEqual(projects.read(self.root, str(self.root / "notes.md"))["path"], "notes.md")   # absolute inside

    def test_nothing_outside_the_folder(self):
        for bad in ("../" + self.outside.name + "/secret.txt", str(self.outside / "secret.txt"),
                    "src/../../" + self.outside.name + "/secret.txt"):
            with self.assertRaises(ValueError, msg=bad):
                projects.read(self.root, bad)
        with self.assertRaises(ValueError):
            projects.listing(self.root, "..")

    def test_raw_files_are_served_sandboxed(self):
        from fastapi.testclient import TestClient
        from aero import server
        c = TestClient(server.app)
        with mock.patch.object(projects, "root_of", lambda pid, s: self.root):
            r = c.get("/api/files/raw", params={"path": "pic.png"})
            self.assertEqual(r.status_code, 200)
            self.assertIn("sandbox", r.headers["content-security-policy"])
            self.assertEqual(c.get("/api/files/read", params={"path": "../x"}).status_code, 403)
            self.assertEqual(c.get("/api/files/list", params={"path": "src"}).json()["entries"][0]["name"], "main.py")


class Window(unittest.TestCase):
    def test_the_title_bar_comes_off_the_top_only(self):
        from aero import shell
        self.assertEqual(shell.caption_off_top(100, False, 8), 100)
        self.assertEqual(shell.caption_off_top(-8, True, 8), 0)     # a maximized window hangs its frame off-screen

    def test_the_first_window_fits_the_main_screen(self):
        from aero import shell
        self.assertEqual(shell.first_size(2560, 1400), (1280, 860))
        self.assertEqual(shell.first_size(1080, 1872), (993, 860))     # a portrait screen: never wider than it

    def test_without_webview2_aero_uses_a_browser_window(self):
        from aero import shell
        with mock.patch.object(shell, "IS_WIN", False):
            self.assertFalse(shell.available())
        with mock.patch.dict(os.environ, {"AERO_NATIVE_WINDOW": "0"}):
            self.assertFalse(shell.available())
        with mock.patch.object(shell, "IS_WIN", True), mock.patch.object(shell, "_lib", lambda: None):
            self.assertFalse(shell.available())

    def test_open_window_falls_back(self):
        from aero import __main__ as m, shell
        with mock.patch.object(shell, "available", lambda: True), \
                mock.patch.object(shell, "launch", side_effect=OSError("no token")), \
                mock.patch.object(m, "open_browser_window", return_value="browser") as b:
            self.assertEqual(m.open_window(), "browser")
            b.assert_called_once()

    def test_the_window_runs_in_its_own_process(self):
        from aero import shell
        argv = shell.command("http://127.0.0.1:8180")
        self.assertEqual(argv[1:], ["-m", "aero.shell", "http://127.0.0.1:8180"])

    def test_the_page_has_its_own_header(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        css = (STATIC / "app.css").read_text(encoding="utf-8")
        js = (STATIC / "projects.js").read_text(encoding="utf-8")
        self.assertIn('id="titlebar"', html)
        self.assertLess(html.index("projects.js"), html.index('src="app.js"'))    # defines what boot() calls
        self.assertIn("app-region: drag", css)
        self.assertIn("app-region: no-drag", css)
        for msg in ("'min'", "'max'", "'close'"):
            self.assertIn(msg, js)


class SettingsWindow(unittest.TestCase):
    """Before 1.3.2 each tab re-rendered from the saved settings, so switching tabs lost what you typed, and the
    window changed size with every tab."""

    def test_tabs_are_kept_and_one_save_sends_every_change(self):
        js = (STATIC / "app.js").read_text(encoding="utf-8")
        body = js[js.index("async function openSettings"):js.index("function browserSessionField")]
        self.assertIn("const panes = {}", body)
        self.assertNotIn("pane.innerHTML = ''", body)               # nothing is thrown away on a tab switch
        self.assertIn("base.get(el) !== JSON.stringify(v)", body)   # only real changes are sent
        self.assertIn("S.beforeClose", body)                        # closing with unsaved edits asks first

    def test_one_size_and_only_the_right_side_scrolls(self):
        css = (STATIC / "app.css").read_text(encoding="utf-8")
        self.assertIn(".modal.settings { width: min(1080px", css)
        rule = css[css.index(".set-pane {"):].split("}", 1)[0]
        self.assertIn("overflow-y: auto", rule)
        self.assertIn("overflow-x: hidden", rule)


if __name__ == "__main__":
    unittest.main()
