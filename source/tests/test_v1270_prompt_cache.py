"""1.2.70: the chat keeps its own llama-server slot (side jobs use another), its prompt cache is saved to disk right
after the answer and restored after a restart, and cache reuse / the RAM prompt cache are on.
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

from aero import engine, kvcache  # noqa: E402

MODEL = Path(tempfile.mkdtemp()) / "q.gguf"
MODEL.write_bytes(b"GGUF" * 10)
ENG = {"url": "http://127.0.0.1:8181", "model": {"path": str(MODEL), "mmproj": None},
       "cfg": {"ctx": 16384, "kv": "q8_0/q4_0"}}


class Resp:
    def __init__(self, code=200, body=None):
        self.status_code, self._b = code, body or {}

    def json(self):
        return self._b


class Flags(unittest.TestCase):
    def test_final_launch_gets_slots_and_caches(self):
        with mock.patch.object(engine, "supports", lambda f: True), \
                mock.patch.object(engine, "server_help", lambda: "[on|off|auto]"):
            a = " ".join(engine.build_args("m.gguf", {"ctx": 16384}, 8181, final=True,
                                           settings={"parallel_slots": 2, "cache_reuse": 256,
                                                     "prompt_cache_ram_mb": 4096, "save_chat_cache": True}))
            t = " ".join(engine.build_args("m.gguf", {"ctx": 16384}, 8182, settings={"parallel_slots": 4}))
            d = " ".join(engine.build_args("m.gguf", {"ctx": 16384}, 8181, final=True, settings={"parallel_slots": 2},
                                           spec=["--spec-type", "draft-simple", "-md", "d.gguf"]))
        for want in ("-np 2", "--kv-unified", "--cache-reuse 256", "--cache-ram 4096", "--slot-save-path"):
            self.assertIn(want, a)
        self.assertIn("-np 1", t)                           # tuning trials stay single-slot
        self.assertNotIn("--slot-save-path", t)
        self.assertIn("-np 1", d)                           # a separate draft model would get ctx / slots

    def test_older_builds_get_one_slot_and_nothing_they_do_not_know(self):
        """Without --kv-unified every slot would get ctx / slots, and Aero sizes prompts to the whole context."""
        with mock.patch.object(engine, "supports", lambda f: False), mock.patch.object(engine, "server_help", lambda: ""):
            a = " ".join(engine.build_args("m.gguf", {"ctx": 4096}, 8181, final=True, settings={"parallel_slots": 2}))
        self.assertIn("-np 1", a)
        for flag in ("--kv-unified", "--cache-reuse", "--cache-ram", "--slot-save-path"):
            self.assertNotIn(flag, a)


class Slots(unittest.TestCase):
    def test_side_jobs_use_the_running_servers_slots_not_the_setting(self):
        with mock.patch.dict(engine._SLOTS, {8181: 2}):
            self.assertEqual(kvcache.side_slot("http://127.0.0.1:8181"), {"id_slot": 1})
        with mock.patch.dict(engine._SLOTS, {8181: 1}):
            self.assertEqual(kvcache.side_slot("http://127.0.0.1:8181"), {})
        self.assertEqual(kvcache.side_slot(None), {})


class SaveRestore(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.boot = [1.0]
        p = [mock.patch.object(kvcache, "DIR", self.dir), mock.patch.object(kvcache, "_boot", lambda url: self.boot[0]),
]
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        kvcache._state.update(slot0=None, boot=None, seen=set(), forgotten=set(), last_turn=0.0)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url.split("8181")[1], json))
        if "save" in url:
            (self.dir / json["filename"]).write_bytes(b"kv" * 100)
            return Resp(200, {"n_saved": 9000})
        return Resp(200, {"n_restored": 9000})

    def test_saved_after_the_answer_and_restored_once_after_a_restart(self):
        with mock.patch.object(kvcache.httpx, "post", self.post):
            self.assertIsNone(kvcache.before_turn("chat1", ENG, {}))            # nothing saved yet
            self.assertEqual(kvcache.after_turn("chat1", ENG, {}, 9000), 9000)
            self.assertTrue((self.dir / "chat1.bin").exists())
            self.assertIsNone(kvcache.before_turn("chat1", ENG, {}))
            kvcache.before_turn("chat2", ENG, {})                               # another chat in between:
            self.assertIsNone(kvcache.before_turn("chat1", ENG, {}))            # the RAM cache handles it
            self.assertFalse(any("restore" in c[0] for c in self.calls))
            self.boot[0] = 2.0                                                   # the server restarted
            note = kvcache.before_turn("chat1", ENG, {})
            self.assertIn("Restored", note)
            self.assertIsNone(kvcache.before_turn("chat1", ENG, {}))            # only once per server run
        self.assertEqual([c for c in self.calls if "restore" in c[0]], [("/slots/0?action=restore", {"filename": "chat1.bin"})])

    def test_saved_again_only_after_it_grew(self):
        with mock.patch.object(kvcache.httpx, "post", self.post):
            kvcache.before_turn("c", ENG, {})
            kvcache.after_turn("c", ENG, {}, 9000)
            self.assertEqual(kvcache.after_turn("c", ENG, {}, 9500), 0)
            self.assertEqual(kvcache.after_turn("c", ENG, {}, 9000 + kvcache.SAVE_GROWTH), 9000)
        self.assertEqual(sum("save" in c[0] for c in self.calls), 2)

    def test_short_unattended_and_other_models_are_skipped(self):
        with mock.patch.object(kvcache.httpx, "post", self.post):
            kvcache.before_turn("c2", ENG, {})
            self.assertFalse(kvcache.after_turn("c2", ENG, {}, 200))
            self.assertFalse(kvcache.after_turn("c2", ENG, {}, 9000, unattended=True))
            kvcache.after_turn("c2", ENG, {}, 9000)
            self.boot[0] = 3.0
            self.assertIsNone(kvcache.before_turn("c2", {**ENG, "cfg": {"ctx": 32768, "kv": "q8_0"}}, {}))
        self.assertFalse(any("restore" in c[0] for c in self.calls))

    def test_a_replaced_model_file_is_a_different_model(self):
        a = kvcache.fingerprint(ENG)
        os.utime(MODEL, (1, 1))
        self.assertNotEqual(a, kvcache.fingerprint(ENG))

    def test_only_the_chat_still_in_slot_0_is_saved(self):
        with mock.patch.object(kvcache.httpx, "post", self.post):
            kvcache.before_turn("a", ENG, {})
            kvcache.before_turn("b", ENG, {})
            self.assertEqual(kvcache.after_turn("a", ENG, {}, 9000), 0)
        self.assertEqual(self.calls, [])

    def test_compaction_moves_the_cache_to_the_new_chat(self):
        with mock.patch.object(kvcache.httpx, "post", self.post):
            kvcache.before_turn("old", ENG, {})
            (self.dir / "old.bin").write_bytes(b"x")
            kvcache.rebind("old", "new")
            self.assertFalse((self.dir / "old.bin").exists())
            self.assertTrue(kvcache.after_turn("new", ENG, {}, 9000))

    def test_a_deleted_chat_is_not_saved(self):
        with mock.patch.object(kvcache.httpx, "post", self.post):
            kvcache.before_turn("d", ENG, {})
            kvcache.forget("d")
            self.assertFalse(kvcache.after_turn("d", ENG, {}, 9000))

    def test_off_means_off(self):
        with mock.patch.object(kvcache.httpx, "post", self.post):
            kvcache.before_turn("c3", ENG, {"save_chat_cache": False})
            kvcache.after_turn("c3", ENG, {"save_chat_cache": False}, 9000)
        self.assertEqual(self.calls, [])

    def test_folder_limit_least_recent_first_never_the_new_file(self):
        for i, used in enumerate((3, 1, 2)):
            (self.dir / f"c{i}.bin").write_bytes(b"x" * 600_000)
            (self.dir / f"c{i}.json").write_text(json.dumps({"used_at": used}), encoding="utf-8")
        kvcache.prune(1.2e6)
        self.assertEqual(sorted(p.name for p in self.dir.glob("*.bin")), ["c0.bin", "c2.bin"])
        kvcache.prune(1, keep="c2")
        self.assertEqual(sorted(p.name for p in self.dir.glob("*.bin")), ["c2.bin"])

    def test_chat_ids_cannot_escape_the_folder(self):
        self.assertEqual(kvcache._name("../../etc/passwd"), "etcpasswd")
        kvcache.forget("../x")


if __name__ == "__main__":
    unittest.main()
