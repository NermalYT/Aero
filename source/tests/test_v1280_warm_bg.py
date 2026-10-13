"""1.2.80: tuning starts from the last tuning of the same (or a similar) model, and keeps going deeper in the
background while the PC is idle.
Run from source/:
    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aero import bgtune, tuner  # noqa: E402

META = {"arch": "qwen3", "name": "Q", "n_layer": 40, "experts": 0, "ctx_train": 40960, "n_embd": 4096,
        "n_head_kv": 8, "head_k": 128, "head_v": 128, "kv_heads": 320, "size_bytes": 8 * 2**30}
HW = {"gpus": [{"name": "RTX"}], "gpu_name": "RTX", "cores": 16, "threads": 32, "vram_total_mb": 16384,
      "ram_total_mb": 65536}


class WarmStart(unittest.TestCase):
    def cache(self, entries):
        return mock.patch.object(tuner, "read_store", lambda n, default: entries if n == "tune_cache.json" else default)

    def test_own_earlier_tuning_first(self):
        own = {"model_file": "q8.gguf", "knobs": {"ctx": 32768, "k": 3}, "tuned_at": "2026-10-01 10:00", "desc": "d",
               "tg": 50}
        other = {"model_file": "q4.gguf", "knobs": {"ctx": 16384, "k": 0}, "tuned_at": "2026-10-11 10:00",
                 "meta": {"arch": "qwen3", "n_layer": 40, "size_bytes": 1}, "hw": {"gpu_name": "RTX"}}
        with self.cache({"a": own, "b": other}):
            e, why = tuner.warm_start("C:/m/q8.gguf", META, {"gpu_name": "RTX"})
        self.assertIs(e, own)
        self.assertIn("earlier tuning", why)

    def test_a_similar_model_on_this_gpu(self):
        other = {"model_file": "q4.gguf", "knobs": {"ctx": 16384, "k": 0}, "tuned_at": "2026-10-11 10:00",
                 "meta": {"arch": "qwen3", "n_layer": 40, "size_bytes": 4 * 2**30}, "hw": {"gpu_name": "RTX"}}
        with self.cache({"b": other}):
            e, _ = tuner.warm_start("C:/m/q8.gguf", META, {"gpu_name": "RTX"})
            self.assertIs(e, other)
            self.assertEqual(tuner.warm_start("C:/m/q8.gguf", META, {"gpu_name": "Other GPU"}), (None, ""))
            self.assertEqual(tuner.warm_start("C:/m/q8.gguf", {**META, "n_layer": 32}, {"gpu_name": "RTX"}), (None, ""))

    def tuner_for(self, warm, cap=1e9):
        t = tuner.Tuner.__new__(tuner.Tuner)
        t.space = tuner.Space(META, HW, {}, True)
        t.space.ffn = False
        t.gpu, t.meta, t.size_mb, t.mmproj_mb, t.ffn_frac, t.calib, t.cap = True, META, META["size_bytes"] / 2**20, 0, 0, 1.0, cap
        t.warm = warm
        return t

    def test_the_warm_seed_moves_layers_for_a_bigger_file(self):
        t = self.tuner_for({"knobs": {"ctx": 32768, "k": 0, "kv": "q8_0", "ub": 512, "t": 16},
                            "meta": {"size_bytes": 4 * 2**30}})
        c = t.warm_seed()
        self.assertEqual((c["ctx"], c["kv"], c["ub"]), (32768, "q8_0", 512))
        self.assertEqual(c["k"], 20)                                  # twice the size: half the layers move off

    def test_the_warm_seed_fits_this_tunes_limit(self):
        warm = {"knobs": {"ctx": 32768, "k": 0, "kv": "q8_0", "ub": 512, "t": 16}, "meta": {"size_bytes": 8 * 2**30}}
        roomy = self.tuner_for(warm)
        tight = self.tuner_for(warm, cap=roomy.est(roomy.warm_seed()) * 0.6)
        c = tight.warm_seed()
        self.assertGreater(c["k"], 0)
        self.assertLessEqual(tight.est(c), tight.cap * 0.97)

    def test_pinned_placement_wins_and_a_smaller_context_makes_it_fit(self):
        warm = {"knobs": {"ctx": 32768, "k": 0, "kv": "q8_0", "ub": 512, "t": 16}, "meta": {"size_bytes": 8 * 2**30}}
        t = self.tuner_for(warm)
        t.space = tuner.Space(META, HW, {}, True, pins={"cpu_layers": 4})
        t.space.ffn = False
        roomy = t.est(t.warm_seed())
        t.cap = roomy * 0.9
        c = t.warm_seed()
        self.assertEqual(c["k"], 4)                                   # the pin stays
        self.assertLess(c["ctx"], 32768)                              # the context gives way instead
        self.assertLessEqual(t.est(c), t.cap * 0.97)

    def test_the_setting_turns_it_off(self):
        seen = []
        with mock.patch.object(tuner.gguf, "summarize", lambda p: dict(META)), \
                mock.patch.object(tuner.hardware, "snapshot", lambda: dict(HW)), \
                mock.patch.object(tuner.bigmodel, "inspect", lambda p: {"layers": [], "total": 1}), \
                mock.patch.object(tuner, "LlamaServer", lambda *a: None), mock.patch.object(tuner, "spec_args", lambda *a: []), \
                mock.patch.object(tuner, "warm_start", lambda *a: seen.append(a) or ({"knobs": {"ctx": 4096}}, "w")):
            off = tuner.Tuner("m.gguf", None, {"tune_warm_start": False}, 8000, "short", None, lambda e: None,
                              threading.Event(), others_mb=0)
            on = tuner.Tuner("m.gguf", None, {}, 8000, "short", None, lambda e: None, threading.Event(), others_mb=0)
        self.assertIsNone(off.warm)
        self.assertEqual((on.warm_why, len(seen)), ("w", 1))


class Background(unittest.TestCase):
    def hooks(self, **kw):
        h = {"state": {"status": "ready", "model": {"id": "m", "name": "Q"},
                       "tune": {"depth": "medium", "limit_gb": 14, "tg": 50, "key": "k1"}},
             "settings": lambda: {"bg_tune": True, "bg_tune_idle_min": 20}, "started": 0,
             "last_activity": lambda: time.time() - 3600, "busy": lambda: False, "task_soon": lambda before: False,
             "engine_ok": lambda: True, "hold": lambda on: None, "load": lambda *a: {"ready": True, "tg": 55, "ctx": 16384},
             "user_job": lambda: False, "profile": lambda key: False, "saved": lambda key: {"tg": 50},
             "restore": lambda key, e: None}
        h.update(kw)
        return h

    def test_only_when_idle_tuned_shallow_and_plugged_in(self):
        with mock.patch.object(bgtune, "on_battery", lambda: False):
            job = bgtune.due(self.hooks())
            self.assertEqual((job["depth"], job["from"], job["limit_gb"]), ("long", "medium", 14))
            self.assertIsNone(bgtune.due(self.hooks(last_activity=lambda: time.time() - 60)))
            self.assertIsNone(bgtune.due(self.hooks(busy=lambda: True)))
            self.assertIsNone(bgtune.due(self.hooks(task_soon=lambda before: True)))
            self.assertIsNone(bgtune.due(self.hooks(engine_ok=lambda: False)))           # AirLLM / Remote Mode
            self.assertIsNone(bgtune.due(self.hooks(profile=lambda key: True)))          # a HAPO profile is chosen
            self.assertIsNone(bgtune.due(self.hooks(settings=lambda: {"bg_tune": False})))
            st = self.hooks()["state"]
            st["tune"]["depth"] = "long"
            self.assertIsNone(bgtune.due(self.hooks(state=st)))
        with mock.patch.object(bgtune, "on_battery", lambda: True):
            self.assertIsNone(bgtune.due(self.hooks()))

    JOB = {"model": {"id": "m", "name": "Q"}, "depth": "long", "from": "medium", "limit_gb": 14, "tg": 50, "ctx": 16384,
           "key": "k1"}

    def test_a_finished_run_reports_old_and_new_speed(self):
        bgtune.run(dict(self.JOB), self.hooks())
        last = bgtune.status()["last"]
        self.assertEqual((last["ok"], last["tg_before"], last["tg"]), (True, 50, 55))

    def test_a_worse_result_gives_the_saved_tuning_back(self):
        calls, restored = [], []

        def load(m, retune, limit, depth, emit, cancel):
            calls.append((retune, depth))
            return {"ready": True, "tg": 30, "ctx": 16384}               # another app took VRAM meanwhile
        bgtune.run(dict(self.JOB), self.hooks(load=load, restore=lambda key, e: restored.append((key, e))))
        self.assertEqual(calls, [(True, "long"), (False, "medium")])
        self.assertEqual(restored, [("k1", {"tg": 50})])
        self.assertIn("worse", bgtune.status()["last"]["why"])
        self.assertTrue(bgtune.worse({"tg": 40, "ctx": 32768}, 48, 16384) is False)  # more context, 17 % slower: fine
        self.assertTrue(bgtune.worse({"tg": 60, "ctx": 8192}, 50, 16384))           # less context: never

    def test_a_busy_model_is_left_alone(self):
        holds, before = [], bgtune.status()["last"]

        def load(*a):
            raise bgtune.Busy("x")
        bgtune.run(dict(self.JOB), self.hooks(load=load, hold=lambda on: holds.append(on)))
        self.assertEqual(holds, [True, False])
        self.assertIs(bgtune.status()["last"], before)                   # no "didn't finish" toast

    def test_unplugging_stops_it(self):
        started = threading.Event()

        def load(m, retune, limit, depth, emit, cancel):
            if retune:
                started.set()
                if not cancel.wait(5):
                    return {"ready": True, "tg": 50, "ctx": 16384}
                raise tuner.Cancelled()
            return {"ready": True}
        battery = [False]
        with mock.patch.object(bgtune, "WATCH_S", 0.05), mock.patch.object(bgtune, "on_battery", lambda: battery[0]):
            th = threading.Thread(target=bgtune.run, args=(dict(self.JOB), self.hooks(load=load)))
            th.start()
            self.assertTrue(started.wait(5))
            battery[0] = True
            th.join(5)
        self.assertEqual(bgtune.status()["last"]["why"], "the PC is on battery")

    def run_and_stop(self, reload):
        calls, started, holds = [], threading.Event(), []

        def load(m, retune, limit, depth, emit, cancel):
            calls.append((retune, depth))
            if retune:
                started.set()
                cancel.wait(5)
                raise tuner.Cancelled()
            return {"ready": True, "tg": 50}
        h = self.hooks(load=load, hold=lambda on: holds.append(on))
        th = threading.Thread(target=bgtune.run, args=(dict(self.JOB), h))
        th.start()
        self.assertTrue(started.wait(5))
        self.assertTrue(bgtune.running())
        self.assertTrue(bgtune.interrupt(reload=reload))
        th.join(5)
        self.assertFalse(bgtune.running())
        self.assertEqual(holds, [True, False])
        self.assertEqual(bgtune.status()["last"]["why"], "stopped for a message" if reload else "stopped for another model")
        return calls

    def test_a_chat_stops_it_and_the_saved_tuning_comes_back(self):
        self.assertEqual(self.run_and_stop(True), [(True, "long"), (False, "medium")])

    def test_loading_another_model_stops_it_without_the_reload(self):
        self.assertEqual(self.run_and_stop(False), [(True, "long")])

    def test_holds_have_owners(self):
        from aero import vram_policy
        vram_policy.hold(True, "tuning", owner="bgtune")
        vram_policy.hold(True)                                              # Remote Mode reloading at the same time
        vram_policy.hold(False, owner="bgtune")
        self.assertTrue(vram_policy.holding())                              # still Remote Mode's
        vram_policy.hold(False)
        self.assertFalse(vram_policy.holding())

    def test_a_chat_waits_with_a_reason(self):
        import asyncio

        from aero import vram_policy
        vram_policy.hold(True, "tuning in the background", owner="bgtune")
        self.addCleanup(vram_policy.hold, False, owner="bgtune")

        async def first():
            gen = vram_policy.wait_for_model(limit=0.1)
            ev = await gen.__anext__()
            await gen.aclose()
            return ev
        self.assertEqual(asyncio.run(first())["text"], "tuning in the background")


class Serial(unittest.TestCase):
    def test_background_loads_never_wait_for_the_users(self):
        from aero import server
        with server._LOAD_LOCK:
            with self.assertRaises(bgtune.Busy):
                server._bg_load({"id": "m"}, True, 14, "long", lambda e: None, threading.Event())
        with mock.patch.dict(server._job, {"busy": True}):
            with self.assertRaises(bgtune.Busy):
                server._bg_load({"id": "m"}, True, 14, "long", lambda e: None, threading.Event())
            # putting the saved tuning back only needs the model free (a download may be running)
            with mock.patch.object(server._load_job, "__wrapped__", lambda *a: {"ready": True}):
                self.assertEqual(server._bg_load({"id": "m"}, False, None, "medium", lambda e: None,
                                                 threading.Event()), {"ready": True})

    def test_a_stopped_load_leaves_the_model_alone(self):
        from aero import server
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(server, "unload") as unload:
            with self.assertRaises(tuner.Cancelled):
                server._load_job({"id": "m", "path": "x"}, True, 14, "long", lambda e: None, cancel)
        unload.assert_not_called()

    def test_a_busy_load_request_does_not_stop_background_tuning(self):
        from fastapi.testclient import TestClient

        from aero import models, server
        f = Path(tempfile.mkdtemp()) / "x.gguf"
        f.write_bytes(b"GGUF")
        m = models.add(f)
        with mock.patch.dict(server._job, {"busy": True}), mock.patch.object(bgtune, "interrupt") as stop:
            self.assertEqual(TestClient(server.app).post("/api/load", json={"id": m["id"]}).status_code, 409)
        stop.assert_not_called()


class HeldChat(unittest.TestCase):
    def test_a_turn_that_waited_uses_the_model_that_came_back(self):
        from aero import agent, config
        t = agent.Turn("held", [], config.load_settings(), {"url": "x", "ctx": 0, "vision": False, "model": None})
        self.addCleanup(t.close)
        self.assertEqual(t.ctx_size, 8192)
        t.refresh_engine({"url": "x", "ctx": 65536, "vision": True, "model": {"name": "Q"}})
        self.assertEqual((t.ctx_size, t.vision, t.tctx.vision, t.model_name), (65536, True, True, "Q"))


if __name__ == "__main__":
    unittest.main()
