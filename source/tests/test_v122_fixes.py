"""Fixes from hands-on testing of 1.2.1 on the owner's PC.
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

from aero import app_registry, config  # noqa: E402


class NotInstalledApp(unittest.TestCase):
    def test_a_known_app_that_isnt_installed_says_so(self):
        """Calculator removed from Windows: 1.2.1 said "no launch method", the model ran calc.exe (a stub with no
        window) and claimed Calculator was open."""
        with mock.patch.object(app_registry, "get", lambda x: {"id": "calculator", "name": "Calculator"}), \
                mock.patch.object(app_registry, "_methods", lambda r: []), \
                mock.patch.object(app_registry, "_expected_procs", lambda r: set()), \
                mock.patch.object(app_registry, "scan_running", lambda: []):
            r = app_registry.launch("calculator")
        self.assertFalse(r["ok"])
        self.assertTrue(r["not_installed"])
        self.assertIn("doesn't seem to be installed", r["error"])
        self.assertIn("don't start it another way", r["error"])

    def test_prompt_asks_to_check_programs_started_by_command(self):
        self.assertIn("After starting a program with run_command, check that its window appeared",
                      config.DEFAULT_SYSTEM_PROMPT)


class LessonsNeedProof(unittest.TestCase):
    def test_reflection_sees_what_was_verified(self):
        """1.2.1 learned "run_command Start-Process calc.exe opened Calculator successfully" from a stub that never
        showed a window: the reflection only saw "ok" and the model's own claim."""
        from aero import experience
        hist = [{"role": "user", "content": "open calculator"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "a", "function": {"name": "run_command", "arguments": '{"command": "Start-Process calc.exe"}'}},
                    {"id": "b", "function": {"name": "app_launch", "arguments": '{"app": "notepad"}'}},
                    {"id": "c", "function": {"name": "app_click", "arguments": '{"element": 3}'}}]},
                {"role": "tool", "tool_call_id": "a", "name": "run_command", "content": "exit 0"},
                {"role": "tool", "tool_call_id": "b", "name": "app_launch", "content": "Notepad started", "verified": True},
                {"role": "tool", "tool_call_id": "c", "name": "app_click", "content": "clicked", "verified": False},
                {"role": "assistant", "content": "Calculator is open."}]
        t = experience._round_text(hist, 0)
        self.assertIn("run_command", t)
        self.assertIn("ran (effect not checked)", t)
        self.assertIn("ok, effect verified", t)
        self.assertIn("did NOT take effect", t)


class AdvisorRemembers(unittest.TestCase):
    def test_an_advisor_that_cant_load_is_deleted_and_skipped_next_time(self):
        """Found on the owner's PC: 17 GB of Bonsai advisor files that llama.cpp couldn't load, tried on every tune."""
        import threading

        from aero import advisor, hf
        from aero.config import models_dir
        f = {"parts": ["Bonsai-27B-Q4_1.gguf"], "quant": "Q4_1", "size": 2 ** 20, "path": "Bonsai-27B-Q4_1.gguf"}
        a = advisor.Advisor.__new__(advisor.Advisor)
        a.settings, a.hw, a.cancel, a.headroom = {"advisor_model": "auto"}, {"gpus": []}, threading.Event(), 0
        a.logs = []
        a.log = a.logs.append
        a.emit = lambda e: None
        a.server = mock.Mock()
        downloads = []

        def download(repo, ff):
            p = hf.local_path(repo, ff["parts"][0], models_dir() / hf.ADVISOR_DIR)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * 1024)
            downloads.append(p)
            return p
        with mock.patch.object(advisor.Advisor, "_candidates", lambda self: [("repo", "prism-ml/Bonsai-27B-gguf", None)]), \
                mock.patch.object(advisor.Advisor, "_pick_file", lambda self, r, l: (f, "CPU")), \
                mock.patch.object(advisor.Advisor, "_download", lambda self, r, ff: download(r, ff)), \
                mock.patch.object(advisor.Advisor, "_start", mock.Mock(side_effect=RuntimeError(
                    "model failed to load (unsupported architecture or corrupt file)"))), \
                mock.patch.object(advisor, "server_version", lambda: "b11575"):
            self.assertFalse(a.prepare())
            self.assertEqual(len(downloads), 1)
            self.assertFalse(downloads[0].exists())                           # deleted: it can't be used
            self.assertTrue(any("Deleted" in x for x in a.logs))
            a.logs.clear()
            self.assertFalse(a.prepare())
            self.assertEqual(len(downloads), 1)                               # not downloaded again
            self.assertTrue(any("Skipping" in x for x in a.logs))
        with mock.patch.object(advisor.Advisor, "_candidates", lambda self: [("repo", "prism-ml/Bonsai-27B-gguf", None)]), \
                mock.patch.object(advisor.Advisor, "_pick_file", lambda self, r, l: (f, "CPU")), \
                mock.patch.object(advisor.Advisor, "_download", lambda self, r, ff: download(r, ff)), \
                mock.patch.object(advisor.Advisor, "_start", mock.Mock(side_effect=RuntimeError("model failed to load"))), \
                mock.patch.object(advisor, "server_version", lambda: "b11700"):
            a.prepare()
            self.assertEqual(len(downloads), 2)                               # a new llama.cpp build tries again


class AdvisorOnTheCpu(unittest.TestCase):
    """With the main model filling the GPU the advisor runs on the CPU; 1.2.1 picked a 27B there (7.8 tokens/s,
    minutes per decision) and Cancel waited for it."""

    def _advisor(self):
        import threading

        from aero import advisor
        a = advisor.Advisor({"advisor_model": "auto"}, {"gpus": [{}]}, 300, lambda e: None, threading.Event())
        a.server = mock.Mock()
        return a

    def test_big_models_are_not_advisors_on_the_cpu(self):
        a = self._advisor()
        bonsai27 = {"files": [{"size": int(1.7 * 2**30), "bpw": 0, "quant": "Q4_1", "parts": ["a.gguf"]}]}
        self.assertIsNone(a._pick_file("prism-ml/Bonsai-27B-gguf", bonsai27))          # 1.7 GB but 27B
        small = {"files": [{"size": int(0.24 * 2**30), "bpw": 0, "quant": "Q1_0", "parts": ["b.gguf"]}]}
        f, where = a._pick_file("prism-ml/Bonsai-1.7B-gguf", small)
        self.assertEqual(where, "CPU")
        qwen = {"files": [{"size": int(1.8 * 2**30), "bpw": 8.5, "quant": "Q8_0", "parts": ["c.gguf"]},
                          {"size": int(9 * 2**30), "bpw": 16, "quant": "F16", "parts": ["d.gguf"]}]}
        f, where = a._pick_file("Qwen/Qwen3-1.7B-GGUF", qwen)
        self.assertEqual((f["quant"], where), ("Q8_0", "CPU"))

    def test_cancel_stops_the_advisor_server(self):
        import time
        a = self._advisor()
        a.cancel.set()
        for _ in range(50):
            if a.server.stop.called:
                break
            time.sleep(0.02)
        self.assertTrue(a.server.stop.called)
        self.assertEqual(a._exam(), 0)                                       # and the exam returns at once


if __name__ == "__main__":
    unittest.main()
