"""1.2.5: Aero has its own browser and never uses the user's. 1.2.3's screen mode opened a window of the owner's own
Chrome and, at the end of the request, the owner's Chrome closed. Signing in happens in a plain window of Aero's own
profile (no automation, so Google accepts the sign-in); Aero's automated browser then uses those sign-ins.
Run from source/:
    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aero import config, control, osinfo  # noqa: E402
from aero.tools import browser  # noqa: E402


class NeverTheUsersBrowser(unittest.TestCase):
    def test_no_code_path_reaches_the_users_browser(self):
        src = Path(browser.__file__).read_text(encoding="utf-8")
        self.assertNotIn("connect_over_cdp", src)
        self.assertNotIn("DevToolsActivePort", src)
        self.assertFalse(hasattr(osinfo, "user_browser"))
        self.assertFalse((Path(browser.__file__).parent / "browser_screen.py").exists())
        self.assertNotIn("browser_session", config.DEFAULTS if hasattr(config, "DEFAULTS") else {})
        self.assertIn("Never use the user's own browser", config.DEFAULT_SYSTEM_PROMPT)

    def test_the_banner_names_aeros_browser(self):
        self.assertEqual(control.target("browser_click", {}, "chat"), ("Aero's browser", None))


class SignIn(unittest.TestCase):
    def setUp(self):
        self.popen = mock.Mock()
        self.proc = mock.Mock(pid=4242)
        self.proc.poll.return_value = None
        self.popen.return_value = self.proc
        p = [mock.patch("subprocess.Popen", self.popen),
             mock.patch.object(browser, "_plain_exe", lambda: r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
             mock.patch.object(browser.B, "thread", None)]
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        self.addCleanup(lambda: browser._SIGNIN_WIN.update(proc=None, owner=None, urls={}))

    def test_a_plain_window_of_aeros_profile_opens_on_the_page(self):
        with mock.patch.object(browser, "page_url", lambda owner: "https://github.com/login"):
            self.assertEqual(browser.show_for_signin("chat"), "https://github.com/login")
        cmd = self.popen.call_args[0][0]
        self.assertEqual(cmd[-1], "https://github.com/login")
        self.assertIn(f"--user-data-dir={config.DATA / 'browser-profile'}", cmd)
        self.assertFalse(any("enable-automation" in c or "remote-debugging" in c for c in cmd))
        self.assertTrue(browser.signin_open())

    def test_the_automated_browser_waits_while_the_window_is_open(self):
        browser.open_signin("", "chat")
        b = browser._Browser()
        b.pw = mock.Mock()
        b.pw.chromium.launch_persistent_context.side_effect = RuntimeError("profile in use")
        fake = {"playwright": mock.Mock(), "playwright.sync_api": mock.Mock()}     # CI has no Playwright
        with mock.patch.object(osinfo, "browser_executable", lambda: None), mock.patch.dict(sys.modules, fake):
            with self.assertRaises(RuntimeError) as e:
                b._context(show=True)
        self.assertIn("open for signing in", str(e.exception))

    def test_done_closes_only_the_window_aero_opened(self):
        browser.open_signin("", "chat")
        if config.IS_WIN:
            from aero.tools import apps
            posted = []
            wins = [{"id": 11, "pid": 4242}, {"id": 12, "pid": 999}]          # 999: the user's own Chrome
            with mock.patch.object(apps, "windows", lambda: wins), \
                    mock.patch.object(apps, "user32", mock.Mock(PostMessageW=lambda h, *a: posted.append(h))):
                browser.end_signin()
            self.assertEqual(posted, [11])
        else:
            browser.end_signin()
            self.proc.terminate.assert_called()
        self.assertFalse(browser.signin_open())

    def test_turn_end_closes_its_sign_in_window_not_others(self):
        browser.open_signin("", "someone-else")
        with mock.patch.object(browser, "end_signin") as end:
            browser.stop_owner("chat")
        end.assert_not_called()


class OverlayOnlyInTheApp(unittest.TestCase):
    def test_tool_code_outside_the_app_shows_nothing(self):
        """The owner saw "... is using ..." banners pop up at random: test runs started the overlay."""
        from aero import overlay
        with mock.patch.dict(os.environ, {"AERO_OVERLAY": ""}), mock.patch.object(overlay.sys, "platform", "win32"), \
                mock.patch.object(overlay.subprocess, "Popen") as popen:
            self.assertFalse(overlay._ensure())
            overlay.send("click", 10, 10, "x")
            overlay.banner("Qwen3-4B is using Notepad")
        popen.assert_not_called()


class ShortNames(unittest.TestCase):
    def test_banner_model_names(self):
        self.assertEqual(control.short_model(
            "Qwen3.8-27B-TURBO-Fable-Cold-Fusion-735-882-Heretic-Uncensored-NEO-CODER-MAX-MTP-GGUF · IQ2_M"),
            "Qwen3.8-27B")
        self.assertEqual(control.short_model("Qwen3-4B · Q4_K_M"), "Qwen3-4B")
        self.assertEqual(control.short_model("MiniCPM5-2B"), "MiniCPM5-2B")
        t = type("T", (), {"settings": {}, "model_name": "Llama-3.3-70B-Instruct-abliterated-GGUF · Q4_K_M"})()
        self.assertEqual(control.actor(t, "local"), "Llama-3.3-70B")


if __name__ == "__main__":
    unittest.main()
