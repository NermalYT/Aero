"""Tests for the move from C:\\Halcyon (or C:\\VRAMpire) to C:\\Aero: saved paths are rewritten, model ids follow their
new paths, the old built-in "About you" text carries over only when the user never saved their own, and old settings
are upgraded once. Every test works in its own throwaway data folder.
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

from aero import migrate, models  # noqa: E402

OLD, NEW = "C:\\Halcyon", "C:\\Aero"


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="aero-migrate-"))
        self.patch = mock.patch.object(migrate, "DATA", self.data)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def write(self, name, obj):
        (self.data / name).write_text(json.dumps(obj), encoding="utf-8")

    def read(self, name):
        return json.loads((self.data / name).read_text(encoding="utf-8"))

    def test_paths_rewritten_and_model_ids_follow(self):
        old_model = OLD + "\\models\\Qwen\\q.gguf"
        old_id = models._id(old_model)
        self.write("settings.json", {"models_dir": OLD + "\\models", "workdir": "c:\\halcyon\\work", "theme": "day"})
        self.write("models.json", {old_id: {"id": old_id, "path": old_model, "name": "q"}})
        self.write("mcp.json", {"servers": {"fs": {"args": [OLD + "\\data\\files"]}}})
        n = migrate.run(OLD, NEW, log=lambda *_: None)
        self.assertEqual(n, 3)
        s = self.read("settings.json")
        self.assertEqual(s["models_dir"], NEW + "\\models")
        self.assertEqual(s["workdir"], NEW + "\\work")          # case-insensitive match, Windows paths
        self.assertEqual(s["theme"], "day")
        new_model = NEW + "\\models\\Qwen\\q.gguf"
        m = self.read("models.json")
        self.assertEqual(list(m), [models._id(new_model)])
        self.assertEqual(m[models._id(new_model)]["path"], new_model)
        self.assertEqual(m[models._id(new_model)]["id"], models._id(new_model))
        self.assertEqual(self.read("mcp.json")["servers"]["fs"]["args"], [NEW + "\\data\\files"])
        self.assertEqual(migrate.run(OLD, NEW, log=lambda *_: None), 0)   # safe to run again

    def test_same_folder_is_a_no_op(self):
        self.write("settings.json", {"models_dir": NEW + "\\models"})
        self.assertEqual(migrate.run(NEW, NEW, log=lambda *_: None), 0)

    def test_old_profile_carried_when_never_saved(self):
        self.write("settings.json", {"theme": "night"})
        (self.data / "legacy-config.py.txt").write_text('X = 1\nSTARTER_PROFILE = """I like fast models."""\n', encoding="utf-8")
        self.assertTrue(migrate.carry_profile(log=lambda *_: None))
        self.assertEqual(self.read("settings.json")["user_profile"], "I like fast models.")
        self.assertFalse((self.data / "legacy-config.py.txt").exists())

    def test_saved_profile_never_overwritten(self):
        self.write("settings.json", {"user_profile": ""})                # an explicitly emptied profile stays empty
        (self.data / "legacy-config.py.txt").write_text('STARTER_PROFILE = "old text"\n', encoding="utf-8")
        self.assertFalse(migrate.carry_profile(log=lambda *_: None))
        self.assertEqual(self.read("settings.json")["user_profile"], "")

    def test_unreadable_legacy_file_is_kept(self):
        self.write("settings.json", {})
        (self.data / "legacy-config.py.txt").write_text("STARTER_PROFILE = (\n", encoding="utf-8")
        self.assertFalse(migrate.carry_profile(log=lambda *_: None))
        self.assertTrue((self.data / "legacy-config.py.txt").exists())

    def test_settings_upgraded_once(self):
        self.write("settings.json", {"thinking": True})
        self.assertTrue(migrate.upgrade_settings(log=lambda *_: None))
        s = self.read("settings.json")
        self.assertEqual((s["thinking"], s["settings_version"]), ("auto", 2))
        self.assertFalse(migrate.upgrade_settings(log=lambda *_: None))


if __name__ == "__main__":
    unittest.main()
