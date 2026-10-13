"""The 1.2 tuner on a simulated PC: big dense models get the feed-forward split as well as whole-layer offload,
Full keeps sweeping until nothing improves for a while, and a crash under load near the limit counts as memory.
Run from source/:
    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aero import tuner  # noqa: E402

GB = 1024
META = {"arch": "gemma3", "name": "Big-27B", "n_layer": 62, "n_embd": 5376, "n_head": 32, "n_head_kv": 16,
        "kv_layers": 62, "kv_heads": 992, "head_k": 128, "head_v": 128, "ctx_train": 131072, "experts": 0,
        "experts_used": 0, "sliding_window": 1024, "swa_global": 0.1666, "size_bytes": 27 * 2**30, "params": 27e9,
        "has_tools_template": True, "has_thinking_template": False, "mtp_layers": 0, "license": ""}
HW = {"gpus": [{"name": "Sim GPU", "used_mb": 600}], "gpu_name": "Sim GPU", "vram_total_mb": 16 * GB,
      "ram_total_mb": 64 * GB, "cores": 16, "threads": 32, "vram_measured": True}


def make(depth="medium", meta=META, ffn_frac=0.62):
    info = {"layers": [{"attn": 1, "ffn": 1, "exps": 0, "other": 0}] * meta["n_layer"], "total": 1}
    with mock.patch.object(tuner.gguf, "summarize", lambda p: dict(meta)), \
            mock.patch.object(tuner.hardware, "snapshot", lambda: dict(HW)), \
            mock.patch.object(tuner.bigmodel, "inspect", lambda p: {**info, "layers": [
                {"attn": 0, "ffn": ffn_frac, "exps": 0, "other": 0}] * meta["n_layer"], "total": meta["n_layer"]}), \
            mock.patch.object(tuner, "spec_args", lambda *a: []), \
            mock.patch.object(tuner, "bench_prompt", lambda *a, **k: "x"):
        t = tuner.Tuner("C:/models/big.gguf", None, {"tune_tool_eval": False, "tune_mode": "balanced"},
                        15 * GB, depth, None, lambda e: None, threading.Event(), others_mb=600)
    return t


def simulate(t, ffn_speedup=1.25):
    """Memory follows the tuner's own estimate; speed falls as weights leave the GPU, less so for the ffn split."""
    def run_config(c):
        mem = t.est_raw(c)
        if mem > t.cap:
            return {"ok": False, "error": "out of memory", "kind": "mem"}
        n = t.space.n
        tg = 40 / (1 + 9 * c["k"] / n)
        if c.get("f"):
            tg = 40 / (1 + 9 * c["k"] * t.ffn_frac / n) * ffn_speedup
        tg *= {"q4_0": 1.0, "q8_0/q4_0": 0.995, "q8_0": 0.99, "f16": 0.97}[c["kv"]] * (1 + c["ub"] / 20000)
        return {"ok": True, "pp": tg * 30, "tg": round(tg, 2), "mem_mb": round(mem), "load_s": 1}
    t.run_config = run_config
    t.deep_check = lambda b: {"ok": True, "tg_deep": b["tg"], "pp_deep": b["pp"], "filled": 4096}
    t.server = mock.Mock()
    with mock.patch.object(tuner, "server_version", lambda: "sim"), mock.patch.object(tuner, "store", lambda *a: None):
        return t.run()


class FeedForwardSplit(unittest.TestCase):
    def test_seeds_include_the_split_for_a_big_dense_model(self):
        t = make()
        self.assertTrue(t.space.ffn)
        seeds = t.seeds()
        self.assertTrue(any(s.get("f") for s in seeds))
        self.assertTrue(any(not s.get("f") and s["k"] > 0 for s in seeds))

    def test_server_args_and_description(self):
        t = make()
        c = {"ctx": 8192, "k": 3, "kv": "q8_0", "ub": 512, "t": 16, "f": True}
        cfg = t.space.server_cfg(c)
        self.assertEqual(cfg["ngl"], 999)
        self.assertEqual(cfg["ot"], ["blk\\.(0|1|2)\\.ffn_(up|down|gate)\\.weight=CPU"])
        self.assertIn("ffn 59/62", t.space.describe(c))

    def test_equiv_keeps_vram_and_flips_kind(self):
        t = make()
        c = {"ctx": 8192, "k": 30, "kv": "q8_0", "ub": 512, "t": 16}
        e = t.equiv(c)
        self.assertTrue(e.get("f"))
        self.assertLessEqual(t.est_raw(e), t.est_raw(c))
        back = t.equiv(e)
        self.assertNotIn("f", back)

    def test_faster_split_wins(self):
        r = simulate(make())
        self.assertIn("ffn", r["desc"])

    def test_moe_and_small_models_skip_it(self):
        self.assertFalse(make(meta={**META, "experts": 8, "experts_used": 2}).space.ffn)
        small = make(meta={**META, "size_bytes": 4 * 2**30})
        self.assertFalse(any(s.get("f") for s in small.seeds()))     # fits: no split to seed


class FullDepth(unittest.TestCase):
    def test_full_keeps_going_past_the_policy_until_patience(self):
        t = make("full", meta={**META, "size_bytes": 4 * 2**30, "n_layer": 36})
        r = simulate(t)
        self.assertGreater(len(r["trials"]), 14)                         # 1.2.0 stopped after ~9
        self.assertTrue(any(x.get("by") == "sweep" for x in r["trials"]))
        self.assertGreaterEqual(t.best_since, tuner.PATIENCE)
        self.assertLessEqual(len(r["trials"]), tuner.FULL_CAP)
        kvs = {x["cfg"]["kv"] for x in t.trials}
        self.assertIn("q4_0", kvs)                                       # Full always tries q4_0 KV


class CrashUnderLoad(unittest.TestCase):
    def test_reset_near_the_limit_is_memory(self):
        t = make()
        t.server = mock.Mock()
        t.server.wait_ready.return_value = (True, "")
        t.server.log_tail.return_value = "llama_server: something happened"
        c = t.seeds()[0]
        c = {**c, "ctx": 131072}
        t.bench = mock.Mock(side_effect=ConnectionResetError("reset"))
        with mock.patch.object(tuner.hardware, "settle_vram", lambda: 0), \
                mock.patch.object(tuner.hardware, "gpu_used_mb", lambda: 0), \
                mock.patch.object(tuner, "build_args", lambda *a, **k: []):
            r = t.run_config(c)
        self.assertEqual(r["kind"], "mem")


if __name__ == "__main__":
    unittest.main()
