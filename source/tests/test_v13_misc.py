"""1.3: smaller fixes and improvements found while testing every feature.
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

from aero import attachments, bgtune  # noqa: E402


class BackgroundTuningLog(unittest.TestCase):
    def test_trials_and_the_result_are_in_the_log(self):
        log = Path(tempfile.mkdtemp()) / "bgtune.log"
        with mock.patch.object(bgtune, "LOG", log):
            emit, close = bgtune._logger()
            emit({"type": "log", "text": "Warm start from x."})
            emit({"type": "trial_done", "n": 1, "desc": "ctx 16,384", "ok": True, "tg": 29.2, "pp": 2301.0})
            emit({"type": "trial_done", "n": 2, "desc": "ctx 32,768", "ok": False, "error": "out of memory"})
            emit({"type": "tune_done", "n": 1, "desc": "ctx 16,384", "tg": 29.2, "trials": 2, "elapsed_s": 40})
            emit({"type": "progress"})
            close()
        lines = [x[9:] for x in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines, ["Warm start from x.", "#1 ctx 16,384: 29.2 tok/s, prompt 2301.0 tok/s",
                                 "#2 ctx 32,768: failed (out of memory)",
                                 "Best: #1 ctx 16,384, 29.2 tok/s (2 trials, 40 s)"])


class SecondSlot(unittest.TestCase):
    """Found on the owner's PC: a 27B tuned to fill a 16 GB card ran out of VRAM on its second request with two slots
    (the tuning measured one; the second took 350 MB more)."""
    H = {"gpus": [{"name": "RTX 5080"}], "vram_total_mb": 16303}

    def test_a_full_card_gets_one_slot(self):
        from aero import server
        self.assertEqual(server.slots_for({"parallel_slots": 2}, self.H, 15154, 705), 1)
        # the case that crashed: other apps used little right then, but the tuning had filled its 15.2 GB limit
        self.assertEqual(server.slots_for({"parallel_slots": 2}, self.H, 15154, 135, limit_mb=15155), 1)
        self.assertEqual(server.slots_for({"parallel_slots": 2}, self.H, 9000, 135, limit_mb=15155), 2)

    def test_room_for_more_keeps_them(self):
        from aero import server
        self.assertEqual(server.slots_for({"parallel_slots": 2}, self.H, 9000, 705), 2)
        self.assertEqual(server.slots_for({"parallel_slots": 4}, self.H, 14500, 705), 2)

    def test_cpu_only_and_unknown_sizes_are_left_alone(self):
        from aero import server
        self.assertEqual(server.slots_for({"parallel_slots": 2}, {"gpus": []}, 15154, 0), 2)
        self.assertEqual(server.slots_for({"parallel_slots": 2}, self.H, None, 705), 2)
        self.assertEqual(server.slots_for({}, self.H, 15154, 705), 1)


class ToolErrors(unittest.TestCase):
    """Found on the owner's PC: app_click before app_view failed with "No app selected", but the traceback under that
    message contained "timeout", so the repeat guard called it a timed-out click and hid the hint from the model,
    which then spent 40 calls without ever calling app_view."""

    def test_expected_errors_carry_no_traceback(self):
        from aero import tools
        tools.tool("t13_value", "x", "files_read")(lambda ctx: (_ for _ in ()).throw(ValueError("No app selected.")))
        tools.tool("t13_bug", "x", "files_read")(lambda ctx: {}["missing"])
        self.addCleanup(tools.REGISTRY.pop, "t13_value", None)
        self.addCleanup(tools.REGISTRY.pop, "t13_bug", None)
        ctx = tools.Ctx({})
        self.assertEqual(tools._run("t13_value", {}, ctx)["text"], "ValueError: No app selected.")
        self.assertIn("Traceback", tools._run("t13_bug", {}, ctx)["text"])        # a real bug still shows where

    def test_a_traceback_mentioning_timeouts_is_not_a_timeout(self):
        from aero import agent, config
        t = agent.Turn("guard13", [], config.load_settings(), {"url": "x", "model": {}})
        self.addCleanup(t.close)
        res = {"text": "ValueError: No app selected.\nTraceback ...\n  r = run(cmd, timeout=600)", "error": True}
        agent._note_outcome(t, "app_click", {"element": 3}, res)
        self.assertEqual(t.ambiguous, {})
        self.assertIsNone(agent._repeat_guard(t, "app_click", {"element": 3}))
        agent._note_outcome(t, "app_click", {"element": 3}, {"text": "TimeoutError: no answer in 30 s", "error": True})
        self.assertTrue(agent._repeat_guard(t, "app_click", {"element": 3})["denied"])

    def test_the_picked_window_reaches_the_tools(self):
        from aero import agent, config
        hist = [{"role": "user", "content": "type hi", "target_app": {"id": 12386740, "title": "Form", "app": "x"}}]
        t = agent.Turn("pick13", hist, config.load_settings(), {"url": "x", "model": {}})
        self.addCleanup(t.close)
        self.assertEqual(t.tctx.target_window, 12386740)

    def test_acting_before_looking_selects_and_shows_instead_of_clicking(self):
        from aero import tools
        from aero.tools import apps
        ctx = tools.Ctx({}, chat_id="pick13b")
        ctx.target_window = 777
        sess = {"hwnd": None}

        def observe(c, hwnd, **k):
            sess["hwnd"] = hwnd
            return {"text": "[1] Edit 'Entry'\n[2] Button 'Add'"}
        with mock.patch.object(apps, "session", lambda owner: sess), mock.patch.object(apps, "_find", lambda w, s: 777), \
                mock.patch.object(apps, "_observe", observe), \
                mock.patch.object(apps, "user32", mock.Mock(IsWindow=lambda h: bool(h)), create=True):
            with self.assertRaises(apps.JustSelected) as e:
                apps._selected(ctx)
            self.assertIn("[2] Button 'Add'", str(e.exception))
            self.assertEqual(apps._selected(ctx), (sess, 777))                     # the next call just goes on


class Stuck(unittest.TestCase):
    def turn(self):
        from aero import agent, config
        t = agent.Turn("stuck13-" + str(id(self)), [], config.load_settings(), {"url": "x", "model": {}})
        self.addCleanup(t.close)
        return t

    def test_the_third_identical_call_with_the_same_result_is_not_run(self):
        from aero import agent
        t = self.turn()
        same = {"text": "Pressed [1] \"Entry\" via default action.", "error": False}
        for _ in range(2):
            self.assertIsNone(agent._stuck(t, "app_click", {"element": 1}))
            agent._note_same(t, "app_click", {"element": 1}, same)
        r = agent._stuck(t, "app_click", {"element": 1})
        self.assertTrue(r["error"])
        self.assertIn("app_type", r["text"])
        agent._note_same(t, "app_click", {"element": 1}, r)                     # the refusal doesn't reset it
        self.assertIsNotNone(agent._stuck(t, "app_click", {"element": 1}))
        self.assertIsNone(agent._stuck(t, "app_type", {"element": 1, "text": "hi"}))     # something else: fine

    def test_looking_again_is_not_stuck(self):
        from aero import agent
        t = self.turn()
        for _ in range(4):
            self.assertIsNone(agent._stuck(t, "screenshot", {"monitor": 1}))
            agent._note_same(t, "screenshot", {"monitor": 1}, {"text": "Screenshot of monitor 1: real 2560x1440."})

    def test_a_changing_result_or_a_wait_is_not_stuck(self):
        from aero import agent
        t = self.turn()
        for i in range(4):
            self.assertIsNone(agent._stuck(t, "app_read", {}))
            agent._note_same(t, "app_read", {}, {"text": f"progress {i}0%"})
        for _ in range(4):
            self.assertIsNone(agent._stuck(t, "browser_wait_for", {"text": "Done"}))
            agent._note_same(t, "browser_wait_for", {"text": "Done"}, {"text": "not yet"})


class WinFormsFields(unittest.TestCase):
    def test_windows_forms_text_boxes_are_edit_controls(self):
        """They are Win32 EDIT controls underneath, so WM_SETTEXT replaces their text (posted characters only add)."""
        from aero.tools import apps
        for cls, want in (("WindowsForms10.EDIT.app.0.117763a_r8_ad1", True), ("Edit", True),
                          ("WindowsForms10.RichEdit20W.app.0.1", True), ("WindowsForms10.BUTTON.app.0.1", False),
                          ("Chrome_RenderWidgetHostHWND", False)):
            with mock.patch.object(apps, "_class", lambda h, c=cls: c):
                self.assertEqual(apps._is_classic_edit(5), want, cls)
        self.assertFalse(apps._is_classic_edit(0))


class Exposure(unittest.TestCase):
    """Found on the owner's PC: the router chose app_click and app_read for "type into the Entry box"; app_view came
    along but app_type didn't, so the model could only press the field, 39 times."""
    AVAILABLE = ["list_dir", "read_file", "app_list", "app_view", "app_click", "app_type", "app_keys", "app_scroll",
                 "app_read", "app_find"]

    def test_companions_of_companions_come_too(self):
        from aero import router
        got = router.expand(["app_read", "app_click"], self.AVAILABLE)
        self.assertIn("app_type", got)

    def test_a_picked_window_gets_every_app_tool(self):
        from aero import pipeline
        d = {"tools": ["list_dir"], "complexity": "simple"}
        got = pipeline.choose_exposure(d, {}, None, self.AVAILABLE, "fill in the form", picked_app=True)
        self.assertTrue(set(pipeline.APP_TOOLS) <= set(got))
        self.assertNotIn("app_type", pipeline.choose_exposure(d, {}, None, self.AVAILABLE, "list my files"))


class WordDocuments(unittest.TestCase):
    def test_markdown_becomes_a_checked_docx(self):
        from aero import tools
        tools.load_all()
        from aero.tools import doc_tools
        d = Path(tempfile.mkdtemp())
        ctx = tools.Ctx({"work_dir": str(d)})
        md = ("# Report\nRevenue was **4.2 million**, up *12 %*.\n\n- one\n- two\n\n1. first\n\n"
              "| Region | Revenue |\n|---|---|\n| EU | 2.1 |\n")
        res = doc_tools.write_docx(ctx, "q3", md, title="Acme")
        out = res["text"]
        self.assertEqual(Path(res["envelope"]["changed_state"]["path"]).resolve(), (d / "q3.docx").resolve())
        self.assertIn("1 tables", out)
        self.assertIn("headings: Acme, Report", out)
        text = attachments._docx((d / "q3.docx").read_bytes())
        for want in ("Revenue was 4.2 million, up 12 %.", "one", "first", "EU | 2.1"):
            self.assertIn(want, text)
        self.assertTrue(doc_tools.write_docx(ctx, "q3.docx", "x")["error"])          # never overwrites by default
        self.assertFalse(doc_tools.write_docx(ctx, "q3.docx", "x", overwrite=True).get("error"))
        self.assertIn("report.v2.docx", doc_tools.write_docx(ctx, "report.v2", "x")["text"])
        doc_tools.write_docx(ctx, "names.docx", "Call get_user_name and _private_ now")
        self.assertIn("Call get_user_name and private now", attachments._docx((d / "names.docx").read_bytes()))


class ModCopies(unittest.TestCase):
    def test_package_data_is_copied_but_the_users_data_is_not(self):
        """Found by a live mod: aero/integrations/data (bundled library snapshots) was skipped like the user's data/
        folder, so the copy's unit tests failed and every mod check did too."""
        import shutil

        from aero import mods
        app = Path(tempfile.mkdtemp()) / "app"
        for f in ("aero/integrations/data/awesome-python.json", "aero/x.py", "data/chats/c.json", "models/m.gguf",
                  "aero/__pycache__/x.pyc", "venv/lib.py"):
            (app / f).parent.mkdir(parents=True, exist_ok=True)
            (app / f).write_text("x", encoding="utf-8")
        dest = app.parent / "copy"
        shutil.copytree(app, dest, ignore=mods._ignore_under(app))
        got = mods._files(dest)
        self.assertEqual(got, {"aero/integrations/data/awesome-python.json", "aero/x.py"})
        self.assertEqual(mods._files(app), got)


class Echo(unittest.TestCase):
    def test_a_repeated_turn_context_is_cut_even_across_pieces(self):
        from aero.agent import EchoGuard
        g = EchoGuard()
        out = "".join(g.feed(x) for x in ["The total is 980 EUR.", " <turn", "_context>\nSent: Tue", " more"])
        self.assertEqual((out + g.flush()).strip(), "The total is 980 EUR.")
        self.assertTrue(g.hit)

    def test_ordinary_angle_brackets_pass(self):
        from aero.agent import EchoGuard
        g = EchoGuard()
        out = "".join(g.feed(x) for x in ["if a < b and <tu", "rbo> then </tu", "rn>"]) + g.flush()
        self.assertEqual(out, "if a < b and <turbo> then </turn>")
        self.assertFalse(g.hit)


class Monitors(unittest.TestCase):
    def test_monitor_1_is_the_primary(self):
        """The owner's PC: Windows lists the vertical side monitor first, the primary third."""
        from aero.tools.desktop import primary_first
        allm = {"left": -1080, "top": -1080, "width": 3640, "height": 2520}
        side = {"left": -1080, "top": -645, "width": 1080, "height": 1920, "is_primary": False}
        top = {"left": 244, "top": -1080, "width": 1920, "height": 1080, "is_primary": False}
        prim = {"left": 0, "top": 0, "width": 2560, "height": 1440, "is_primary": True}
        self.assertEqual(primary_first([allm, side, top, prim]), [allm, prim, side, top])
        old_mss = [allm, side, {**top}, {"left": 0, "top": 0, "width": 2560, "height": 1440}]   # no is_primary key
        self.assertEqual(primary_first(old_mss)[1]["width"], 2560)
        self.assertEqual(primary_first([allm, side]), [allm, side])


class Media(unittest.TestCase):
    def test_recordings_are_never_decoded_as_text(self):
        """An MP3's bytes decode as cp1252 'text'; without voice typing it says what the file is instead."""
        from aero.integrations import load_all
        load_all()
        from aero.integrations import voice
        with mock.patch.object(voice, "ready", lambda: False):
            kind, text, _ = attachments.extract("song.mp3", b"ID3\x04" + bytes(range(256)) * 40)
        self.assertTrue(text.startswith("[audio/mpeg file"))


if __name__ == "__main__":
    unittest.main()
