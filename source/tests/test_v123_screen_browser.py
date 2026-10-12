"""1.2.3: the browser tools work in a window of the user's own browser, by sight, when its remote-debugging switch is
off (tools/browser_screen.py). The UI Automation parts were tested live on Chrome and Edge (throwaway profiles);
these tests cover the choices around them and run on every OS.
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

from aero import config  # noqa: E402
from aero.tools import browser, browser_screen  # noqa: E402


class WhichProfile(unittest.TestCase):
    def _state(self, profiles, last):
        d = Path(tempfile.mkdtemp(prefix="aero-ud-"))
        (d / "Local State").write_text(json.dumps({"profile": {"last_used": last, "info_cache": {
            p: {"name": p} for p in profiles}}}), encoding="utf-8")
        return str(d)

    def test_one_profile_needs_no_choice(self):
        self.assertEqual(browser_screen.profile_args(self._state(["Default"], "Default")), [])

    def test_several_profiles_open_the_last_used_one(self):
        ud = self._state(["Default", "Profile 1"], "Profile 1")
        self.assertEqual(browser_screen.profile_args(ud), ["--profile-directory=Profile 1"])

    def test_no_local_state(self):
        self.assertEqual(browser_screen.profile_args(tempfile.mkdtemp()), [])


class WhenScreenModeIsUsed(unittest.TestCase):
    def setUp(self):
        self.ud = Path(tempfile.mkdtemp(prefix="aero-ud-"))
        self.ub = {"exe": "chrome.exe", "user_data": str(self.ud), "name": "Chrome", "inspect_url": "chrome://inspect"}
        self.settings = {"browser_session": "mine", "browser_mode": "visible"}
        p = [mock.patch.object(browser, "IS_WIN", True),
             mock.patch.object(browser_screen, "supported", lambda: self.ub),
             mock.patch.object(config, "load_settings", lambda: self.settings),
             mock.patch.object(browser.B, "_want_headed", lambda show=False, hide=False: not hide),
             mock.patch.object(browser.B, "kind", None), mock.patch.object(browser.B, "mine_failed", 0.0)]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def test_switch_off_uses_a_window_of_the_users_browser(self):
        self.assertTrue(browser._use_screen())

    def test_switch_on_connects_over_it_instead(self):
        (self.ud / "DevToolsActivePort").write_text("9222\n/devtools/browser/x", encoding="utf-8")
        self.assertFalse(browser._use_screen())
        browser.B.mine_failed = __import__("time").time()          # the connection was just refused
        self.assertTrue(browser._use_screen())

    def test_not_for_background_requests_aeros_profile_setting_or_strict_offline(self):
        self.assertFalse(browser._use_screen(hide=True))
        self.settings["browser_session"] = "aero"
        self.assertFalse(browser._use_screen())
        self.settings["browser_session"] = "mine"
        with mock.patch("aero.localonly.strict", lambda: True):
            self.assertFalse(browser._use_screen())

    def test_not_off_windows_or_without_a_chromium_default(self):
        with mock.patch.object(browser, "IS_WIN", False):
            self.assertFalse(browser._use_screen())
        with mock.patch.object(browser_screen, "supported", lambda: None):
            self.assertFalse(browser._use_screen())


class ToolsGoToTheWindow(unittest.TestCase):
    def setUp(self):
        self.calls = []
        win = {"hwnd": 1, "name": "Chrome", "url": "https://example.com/"}
        p = [mock.patch.object(browser, "IS_WIN", True),
             mock.patch.object(browser_screen, "active", lambda owner: win if owner == "chat" else None)]
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        self.ctx = mock.Mock(owner="chat", visible=True, vision=True, cancel=None)

    def _rec(self, name):
        return lambda *a, **k: self.calls.append((name, a[2:] if len(a) > 2 else a[1:])) or {"text": name}

    def test_each_tool_uses_the_window(self):
        with mock.patch.multiple(browser_screen, click=self._rec("click"), type_text=self._rec("type"),
                                 view=self._rec("view"), back=self._rec("back"), scroll=self._rec("scroll"),
                                 press=self._rec("press"), select=self._rec("select")), \
                mock.patch.object(browser.B, "call", side_effect=AssertionError("Playwright must not be used")):
            browser.browser_click(self.ctx, 7)
            browser.browser_type(self.ctx, 8, "frogs", submit=True)
            browser.browser_snapshot(self.ctx)
            browser.browser_back(self.ctx)
            browser.browser_scroll(self.ctx, "up")
            browser.browser_press(self.ctx, "Enter")
            browser.browser_select(self.ctx, 9, "Large")
        self.assertEqual([c[0] for c in self.calls], ["click", "type", "view", "back", "scroll", "press", "select"])
        self.assertEqual(self.calls[1][1], (8, "frogs", True))
        self.assertEqual(self.calls[4][1], (False,))
        self.assertEqual(browser.page_url("chat"), "https://example.com/")

    def test_extract_says_what_to_use_instead(self):
        r = browser.browser_extract(self.ctx, "tables")
        self.assertTrue(r["error"])
        self.assertIn("browser_read", r["text"])

    def test_the_window_closes_when_the_request_is_done(self):
        closed = []
        with mock.patch.object(browser_screen, "close", lambda owner: closed.append(owner)):
            self.assertTrue(browser.finish_owner("chat"))
            for _ in range(100):
                if closed:
                    break
                __import__("time").sleep(0.01)
        self.assertEqual(closed, ["chat"])
        self.assertTrue(browser.in_users_browser("chat"))


class TypingIsCareful(unittest.TestCase):
    def setUp(self):
        win = {"hwnd": 1, "name": "Chrome", "url": "https://github.com/login"}
        p = [mock.patch.object(browser_screen, "active", lambda owner: win),
             mock.patch.object(browser_screen.apps, "selected_hwnd", lambda owner: 1),
             mock.patch.object(browser_screen, "view", lambda ctx, owner, elements=True: {"text": "view"})]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def test_never_a_password(self):
        with mock.patch.object(browser_screen, "_in_page", lambda o, h, r: True), \
                mock.patch.object(browser_screen, "_set_field", lambda *a: ("password", None)):
            r = browser_screen.type_text(None, "chat", 27, "hunter2")
        self.assertTrue(r["error"])
        self.assertIn("never types passwords", r["text"])

    def test_only_into_fields(self):
        with mock.patch.object(browser_screen, "_in_page", lambda o, h, r: True), \
                mock.patch.object(browser_screen, "_set_field", lambda *a: ("nofield", None)):
            r = browser_screen.type_text(None, "chat", 28, "x")
        self.assertTrue(r["error"])
        self.assertIn("isn't a text field", r["text"])

    def test_never_the_browsers_own_address_bar_or_menus(self):
        """Seen live: an id from the snapshot was Chrome's address bar, and typing there searched Google."""
        why = "[9] is part of the browser itself (address bar, toolbar or menus), not the page."
        set_field = mock.Mock()
        with mock.patch.object(browser_screen, "_in_page", lambda o, h, r: why), \
                mock.patch.object(browser_screen, "_set_field", set_field):
            r = browser_screen.type_text(None, "chat", 9, "example.org")
            c = browser_screen.click(None, "chat", 9)
        self.assertEqual((r["text"], c["text"]), (why, why))
        set_field.assert_not_called()

    def test_a_typed_value_is_read_back(self):
        with mock.patch.object(browser_screen, "_in_page", lambda o, h, r: True), \
                mock.patch.object(browser_screen, "_set_field", lambda *a: ("ok", "tree frog")):
            r = browser_screen.type_text(None, "chat", 26, "tree frog")
        self.assertIn("Set [26] to 'tree frog' (read back)", r["text"])


class Prompt(unittest.TestCase):
    def test_the_model_knows_about_the_window(self):
        self.assertIn("a window of it Aero opens and reads as a picture with numbered controls",
                      config.DEFAULT_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
