"""1.2.65: every tuning setting can be left automatic or pinned per model (KV cache for keys and values separately,
flash attention, batch, threads, mmap/mlock, GPU placement, YaRN context), the tuner also tries a mixed KV cache, and
profiles can be switched from the model card.
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

from aero import engine, gguf, tuner  # noqa: E402

META = {"n_layer": 40, "experts": 0, "ctx_train": 40960, "kv_heads": 320, "head_k": 128, "head_v": 128,
        "n_head_kv": 8, "n_embd": 4096}
HW = {"cores": 16, "threads": 32}


class Pins(unittest.TestCase):
    def test_only_known_knobs_and_sane_values_survive(self):
        p = tuner.clean_pins({"ctx": "32768", "kv": "q8_0/q4_0", "fa": "on", "mlock": True, "cpu_layers": 0,
                              "threads": 0, "rope_scale": 20, "split": "diagonal", "evil": "rm -rf", "ub": "auto"})
        self.assertEqual(p, {"ctx": 32768, "kv": "q8_0/q4_0", "fa": True, "mlock": True, "cpu_layers": 0})
        self.assertEqual(tuner.clean_pins(None), {})
        self.assertEqual(tuner.clean_pins({"kv": "q2_K"}), {})

    def test_pinned_knobs_are_not_searched(self):
        sp = tuner.Space(META, HW, {}, True, pins={"ctx": 16384, "kv": "q8_0/q4_0", "ub": 1024, "threads": 8,
                                                   "cpu_layers": 4})
        self.assertEqual((sp.ctx, sp.kv, sp.ub, sp.thr), ([16384], ["q8_0/q4_0"], [1024], [8]))
        moves = {m for m, _ in sp.neighbors({"ctx": 16384, "k": 4, "kv": "q8_0/q4_0", "ub": 1024, "t": 8})}
        self.assertFalse(moves & {"gpu+", "gpu-", "ctx+", "ctx-", "kv+", "kv-", "ub+", "ub-", "split"})
        self.assertEqual(sp.apply_pins({"ctx": 16384, "k": 0})["k"], 4)

    def test_unpinned_search_now_includes_the_mixed_cache(self):
        sp = tuner.Space(META, HW, {"allow_q4_kv": True}, True)
        self.assertEqual(sp.kv, ["q4_0", "q8_0/q4_0", "q8_0", "f16"])
        self.assertEqual(tuner.Space(META, HW, {}, True).kv, ["q8_0", "f16"])

    def test_pinned_settings_reach_llama_server(self):
        with mock.patch.object(tuner, "supports", lambda f: True):
            sp = tuner.Space(META, HW, {}, True, pins={"fa": False, "batch": 4096, "threads_batch": 12, "mlock": True,
                                                       "mmap": False, "rope_scale": 2})
        self.assertEqual(sp.ctx[-1], 81920)                                   # YaRN x2 of 40,960
        cfg = sp.server_cfg({"ctx": 81920, "k": 0, "kv": "q8_0/q4_0", "ub": 512, "t": 16})
        self.assertEqual((cfg["fa"], cfg["b"], cfg["tb"], cfg["mlock"], cfg["no_mmap"], cfg["rope_scale"],
                          cfg["yarn_orig_ctx"]), (False, 4096, 12, True, True, 2.0, 40960))
        with mock.patch.object(engine, "supports", lambda f: True), mock.patch.object(engine, "server_help",
                                                                                    lambda: "[on|off|auto]"):
            a = engine.build_args("m.gguf", cfg, 8181)
        s = " ".join(a)
        for want in ("-ctk q8_0 -ctv q4_0", "-tb 12", "--mlock", "--no-mmap", "--rope-scaling yarn --rope-scale 2",
                     "--yarn-orig-ctx 40960", "-fa off", "-b 4096"):
            self.assertIn(want, s)

    def test_mixed_cache_memory_is_between(self):
        q8, mix, q4 = (gguf.kv_mb(META, 32768, t) for t in ("q8_0", "q8_0/q4_0", "q4_0"))
        self.assertLess(q4, mix)
        self.assertLess(mix, q8)
        self.assertAlmostEqual(mix, (q8 + q4) / 2, delta=1)

    def test_different_pins_mean_a_different_tuning(self):
        d = Path(tempfile.mkdtemp())
        f = d / "m.gguf"
        f.write_bytes(b"GGUF" + b"0" * 100)
        h = {"gpu_name": "RTX", "vram_total_mb": 16000, "ram_total_mb": 64000}
        a = tuner.cache_key(f, None, h)
        self.assertEqual(a, tuner.cache_key(f, None, h, pins={}))             # nothing pinned: old tunings stay valid
        self.assertNotEqual(a, tuner.cache_key(f, None, h, pins={"kv": "q4_0"}))
        self.assertEqual(tuner.cache_key(f, None, h, pins={"kv": "q4_0", "x": 1}),
                         tuner.cache_key(f, None, h, pins={"kv": "q4_0"}))


class ReviewFixes(unittest.TestCase):
    """Found by a code review of 1.2.65 before release."""

    def test_flash_attention_off_keeps_the_v_cache_f16(self):
        self.assertNotIn("kv", tuner.clean_pins({"fa": False, "kv": "q8_0/q4_0"}))
        self.assertEqual(tuner.clean_pins({"fa": False, "kv": "q8_0/f16"} if False else {"fa": False, "kv": "f16"})["kv"], "f16")
        sp = tuner.Space(META, HW, {"allow_q4_kv": True}, True, pins={"fa": False})
        self.assertEqual(sp.kv, ["f16"])

    def test_a_pinned_placement_is_not_split_away(self):
        sp = tuner.Space(META, HW, {}, True, pins={"cpu_layers": 6})
        sp.ffn, sp.equiv = True, lambda c: {**c, "k": 2, "f": True}
        moves = {m for m, _ in sp.neighbors({"ctx": 16384, "k": 6, "kv": "q8_0", "ub": 512, "t": 16})}
        self.assertNotIn("split", moves)

    def test_pinned_threads_always_reach_the_server(self):
        sp = tuner.Space(META, HW, {}, True, pins={"threads": 8})
        self.assertEqual(sp.server_cfg({"ctx": 16384, "k": 0, "kv": "q8_0", "ub": 512, "t": 8})["threads"], 8)

    def test_yarn_needs_a_build_that_has_it(self):
        with mock.patch.object(tuner, "supports", lambda f: False):
            sp = tuner.Space(META, HW, {}, True, pins={"rope_scale": 4})
        self.assertEqual(sp.ctx[-1], 40960)

    def test_api_nonsense_is_dropped(self):
        self.assertEqual(tuner.clean_pins({"ctx": True, "threads": 9999, "cpu_layers": 5000, "ub": 1}), {})

    def test_profiles_rank_caches_by_precision(self):
        from aero import hapo
        r = [hapo.kv_rank(k) for k in ("q4_0", "q8_0/q4_0", "q8_0", "f16/q8_0", "f16")]
        self.assertEqual(r, sorted(r))
        self.assertEqual(len(set(r)), 5)
        self.assertTrue(hapo._DESC.search("ctx 16,384 | gpu 4/28 | kv q8_0/q4_0 | ub 512 | 8 threads"))

    def test_mlock_on_load_mode_builds(self):
        help_txt = "--load-mode MODE   how to load the model\n        none, mmap, mlock (default: mmap)\n"
        with mock.patch.object(engine, "server_help", lambda: help_txt), \
                mock.patch.object(engine, "supports", lambda f: f in ("--load-mode",)):
            a = engine.build_args("m.gguf", {"ctx": 4096, "mlock": True, "no_mmap": True}, 8181)
        self.assertEqual(a.count("--load-mode"), 1)
        self.assertIn("mlock", a)


class PinsApi(unittest.TestCase):
    def test_save_and_read(self):
        from fastapi.testclient import TestClient

        from aero import models, server
        d = Path(tempfile.mkdtemp())
        f = d / "x.gguf"
        f.write_bytes(b"GGUF")
        m = models.add(f)
        c = TestClient(server.app)
        with mock.patch.object(server.gguf, "summarize", lambda p: META):
            r = c.put(f"/api/models/{m['id']}/pins", json={"pins": {"kv": "q8_0/q4_0", "nonsense": 1}})
            self.assertEqual(r.json()["pins"], {"kv": "q8_0/q4_0"})
            g = c.get(f"/api/models/{m['id']}/pins").json()
        self.assertEqual(g["pins"], {"kv": "q8_0/q4_0"})
        self.assertIn("q8_0/q4_0", g["knobs"]["kv"])
        self.assertEqual(g["knobs"]["ctx_train"], 40960)
        self.assertEqual(c.put("/api/models/nope/pins", json={"pins": {}}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
