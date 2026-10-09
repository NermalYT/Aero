"""Live background-control checks on a real Windows desktop (opt-in: set AERO_LIVE_UI=1). A small throwaway window
(tests/toy_win32_app.py) is driven through Aero's app tools while the test checks that the window in front, the mouse
cursor and the clipboard never change. Windows only; skipped everywhere else and in CI.

    set AERO_LIVE_UI=1
    python -m unittest tests.test_app_background -v
"""
import ctypes
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LIVE = sys.platform == "win32" and os.environ.get("AERO_LIVE_UI") == "1"


def clipboard_text():
    """The clipboard's text, read with the Win32 API (nothing is changed)."""
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.GetClipboardData.restype = ctypes.c_void_p
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    for _ in range(10):
        if u.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return None
    try:
        h = u.GetClipboardData(13)                   # CF_UNICODETEXT
        if not h:
            return ""
        p = k.GlobalLock(h)
        try:
            return ctypes.wstring_at(p) if p else ""
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def element_id(text, pattern):
    for line in text.splitlines():
        if re.search(pattern, line):
            m = re.search(r"\[(\d+)\]", line)
            if m:
                return int(m.group(1))
    raise AssertionError(f"no element matching {pattern!r} in:\n{text[:2000]}")


@unittest.skipUnless(LIVE, "live Windows desktop check: set AERO_LIVE_UI=1 on Windows")
class Background(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # real pixels, like Aero itself (__main__._dpi_aware)
        except Exception:
            pass
        from aero import config, tools
        tools.load_all()
        cls.tools = tools
        cls.title = f"Aero toy app {uuid.uuid4().hex[:6]}"
        cls.proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name("toy_win32_app.py")), cls.title])
        from aero.tools import apps
        cls.apps = apps
        for _ in range(60):
            if any(w["title"] == cls.title for w in apps.windows()):
                break
            time.sleep(0.1)
        cls.hwnd = next(w["id"] for w in apps.windows() if w["title"] == cls.title)
        s = config.load_settings()
        s["screenshot_max_side"] = 800
        cls.ctx = tools.Ctx(s, vision=False, chat_id="live-ui")

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()

    def setUp(self):
        from aero import input_guard
        self.t0 = time.monotonic()
        self.fg = input_guard.foreground()
        self.cursor = input_guard.cursor()
        self.clip = clipboard_text()

    def assert_user_untouched(self):
        """The window in front, the cursor and the clipboard are what they were. If the person at the PC used the mouse
        or keyboard during the test, a change can't be pinned on Aero: the check is reported as inconclusive."""
        from aero import input_guard
        idle = input_guard.idle_ms()
        if idle is not None and idle < (time.monotonic() - self.t0) * 1000:
            self.skipTest(f"someone used the mouse or keyboard during this test (idle {idle} ms): inconclusive")
        fg = input_guard.foreground()
        self.assertEqual(fg, self.fg, f"the window in front changed: {self.apps._title(self.fg)!r} -> "
                                      f"{self.apps._title(fg)!r}")
        self.assertEqual(input_guard.cursor(), self.cursor, "the mouse cursor moved")
        self.assertEqual(clipboard_text(), self.clip, "the clipboard changed")

    def view(self, **kw):
        r = self.tools.run("app_view", {"window": str(self.hwnd), **kw}, self.ctx)
        self.assertFalse(r["error"], r["text"][:500])
        return r["text"]

    def test_1_type_into_a_field_and_read_it_back(self):
        v = self.view()
        self.assertIn("Control mode: ACCESSIBILITY_BACKGROUND", v)
        edit = element_id(v, r"\] Edit ")
        r = self.tools.run("app_type", {"element": edit, "text": "written by Aero", "mode": "replace"}, self.ctx)
        self.assertIs(r["envelope"]["verified"], True, r["text"][:400])
        h = next(c for c in self.child_windows() if self.apps._class(c) == "Edit")
        self.assertEqual(self.apps.get_text(h), "written by Aero")
        self.assert_user_untouched()

    def test_2_press_a_button_and_see_the_effect(self):
        v = self.view()
        btn = element_id(v, r'Button "Press me"')
        r = self.tools.run("app_click", {"element": btn}, self.ctx)
        self.assertIn("Pressed 1", r["text"])
        self.assertIs(r["envelope"]["verified"], True, r["text"][:400])
        self.assert_user_untouched()

    def test_3_posted_input_that_is_ignored_is_a_failure(self):
        self.view()
        panel = next(c for c in self.child_windows() if self.apps._class(c) == "AeroToyDeaf")
        l, t, r_, b = self.apps._rect(panel)
        sess = self.apps.session("live-ui")
        x, y = ((l + r_) / 2 - sess["left"]) / sess["scale"], ((t + b) / 2 - sess["top"]) / sess["scale"]
        r = self.tools.run("app_click", {"x": x, "y": y}, self.ctx)
        self.assertIs(r["envelope"]["verified"], False, r["text"][:500])
        self.assertIn("FAILED", self.tools_evidence(r))
        self.assert_user_untouched()

    def test_4_stale_element_is_refused(self):
        v = self.view()
        edit = element_id(v, r"\] Edit ")
        remove = element_id(v, r'Button "Remove field"')
        self.tools.run("app_click", {"element": remove, "look": False}, self.ctx)
        time.sleep(0.3)
        r = self.tools.run("app_type", {"element": edit, "text": "x"}, self.ctx)
        self.assertTrue(r["error"])
        self.assertRegex(r["text"], r"no longer|not in|gone|latest app_view")
        self.assert_user_untouched()

    def test_5_minimized_window_still_works_without_a_picture(self):
        self.apps.user32.ShowWindow(self.hwnd, 7)            # SW_SHOWMINNOACTIVE
        time.sleep(0.3)
        try:
            r = self.tools.run("app_view", {"window": str(self.hwnd)}, self.ctx)
            self.assertIsNone(r.get("image"))
            self.assertIn("No picture", r["text"])
            self.assertIn("minimized", r["text"])
            self.assertTrue(self.apps.user32.IsIconic(self.hwnd))    # Aero did not restore it
            btn = element_id(r["text"], r'Button "Press me"')
            out = self.tools.run("app_click", {"element": btn}, self.ctx)
            self.assertFalse(out["error"], out["text"][:300])
            self.assert_user_untouched()
        finally:
            self.apps.user32.ShowWindow(self.hwnd, 4)

    def test_6_chord_needs_foreground_control(self):
        self.view()
        r = self.tools.run("app_keys", {"keys": "ctrl+a"}, self.ctx)        # no grant on this context
        self.assertTrue(r["error"])
        self.assertIn("real mouse and keyboard", r["text"])
        self.assert_user_untouched()

    def child_windows(self):
        out = []
        cb = self.apps.WNDENUMPROC(lambda h, _: out.append(h) or True)
        self.apps.user32.EnumChildWindows(self.hwnd, cb, 0)
        return out

    @staticmethod
    def tools_evidence(r):
        from aero import action_results
        return action_results.evidence(r.get("envelope"))


if __name__ == "__main__":
    unittest.main()
