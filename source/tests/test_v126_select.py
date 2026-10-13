"""1.2.6 Select: one click from a search result to a planned, downloaded, tuned and loaded model, with the VRAM limit
chosen automatically. The planner prices every quant and placement before anything is downloaded.
Run from source/:
    python -m unittest discover -s tests -v
"""
import io
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aero import gguf, planner  # noqa: E402

GB = 2**30
DENSE = {"arch": "qwen3", "name": "Qwen3.8-27B", "n_layer": 64, "n_embd": 5120, "n_head": 40, "n_head_kv": 8,
         "kv_layers": 64, "kv_heads": 512, "head_k": 128, "head_v": 128, "ctx_train": 262144, "experts": 0,
         "experts_used": 0, "sliding_window": 0, "swa_global": 1.0, "mtp_layers": 0, "params": 27e9, "size_bytes": 0}
MOE = dict(DENSE, name="Qwen3-30B-A3B", n_layer=48, n_embd=2048, n_head=32, n_head_kv=4, kv_heads=192, experts=128,
           experts_used=8, params=30.5e9)
QUANTS = [("Q8_0", 8.5), ("Q6_K", 6.56), ("Q5_K_M", 5.69), ("Q4_K_M", 4.89), ("IQ4_XS", 4.25), ("Q3_K_M", 3.91),
          ("IQ3_XXS", 3.06), ("IQ2_M", 2.7), ("IQ2_XXS", 2.06)]


def files(params):
    return [{"name": f"m-{q}", "quant": q, "bpw": b, "size": int(params * b / 8), "parts": [f"m-{q}.gguf"]}
            for q, b in QUANTS]


def hw(gpu, vram_mb, ram_total, ram_free):
    return {"gpus": [{"name": gpu, "total_mb": vram_mb}] if gpu else [], "ram_total_mb": ram_total,
            "ram_avail_mb": ram_free}


def run(meta, h, budget):
    planner._RAM["gbps"] = 50.0
    t = planner.template_for(meta, None, int(meta["params"] * 4.89 / 8))
    return planner.plan(files(meta["params"]), t, h, budget, planner.ram_for_weights(h), 5.0)


class Planner(unittest.TestCase):
    def test_a_27b_model_on_a_4gb_gpu_just_works(self):
        """The owner's example: a 4 GB GPU user selects Qwen3.8-27B."""
        r = run(DENSE, hw("NVIDIA GeForce GTX 1650", 4096, 16384, 12000), 3500)
        p = r["pick"]
        self.assertIn(r["level"], ("ok", "slow"))
        self.assertIn(p["strategy"], ("ffn", "layers"))
        self.assertTrue(p["fits_ram"])
        self.assertGreaterEqual(p["bpw"], planner.MIN_BPW)
        self.assertGreaterEqual(p["ctx"], 16384)                 # Aero's own prompt is ~7.4k tokens
        self.assertLessEqual(p["gpu_mb"], 3500)
        self.assertGreater(p["est_tg"], 1.0)

    def test_a_big_gpu_runs_it_fast_not_at_max_quality(self):
        """1.2.6 first draft picked Q5_K_M split at 5.5 tok/s on a 16 GB card; full-GPU low-bit quants run ~10x faster."""
        r = run(DENSE, hw("NVIDIA GeForce RTX 5080", 16303, 65536, 48000), 14800)
        self.assertEqual(r["level"], "ok")
        self.assertGreaterEqual(r["pick"]["est_tg"], 20)
        self.assertGreaterEqual(r["pick"]["ctx"], 16384)

    def test_moe_keeps_experts_in_ram_and_stays_quick(self):
        r = run(MOE, hw("NVIDIA GeForce GTX 1650", 4096, 16384, 12000), 3500)
        self.assertEqual(r["level"], "ok")
        self.assertGreater(r["pick"]["est_tg"], 10)

    def test_too_little_ram_is_said_plainly_with_the_fastest_option(self):
        r = run(DENSE, hw("NVIDIA GeForce GTX 1650", 4096, 8192, 5000), 3500)
        self.assertEqual(r["level"], "very_slow")
        self.assertIn("read from disk", r["warning"])
        self.assertGreater(r["pick"]["disk_mb"], 0)

    def test_cpu_only(self):
        r = run(MOE, hw(None, 0, 32768, 26000), 0)
        self.assertEqual(r["pick"]["strategy"], "stream")
        self.assertGreater(r["pick"]["est_tg"], 5)

    def test_quality_curve_and_bandwidth_table(self):
        q = [planner.quality(b) for b in (2.0, 2.7, 3.06, 3.91, 4.89, 5.69, 6.56, 8.5)]
        self.assertEqual(q, sorted(q))
        self.assertLess(planner.quality(4.5, "Q4_0"), planner.quality(4.5, "Q4_K_M"))
        self.assertEqual(planner.gpu_gbps({"gpus": [{"name": "NVIDIA GeForce RTX 5080", "total_mb": 16303}]}), 960)
        self.assertLess(planner.gpu_gbps({"gpus": [{"name": "NVIDIA GeForce RTX 4060 Laptop GPU"}]}), 272)
        self.assertEqual(planner.gpu_gbps({"gpus": [{"name": "Some GPU", "total_mb": 4096}]}), 128)

    def test_names(self):
        self.assertEqual(planner.size_b("unsloth/Qwen3-30B-A3B-GGUF"), 30)
        self.assertEqual(planner.size_b("Llama-3.3-70B-Instruct"), 70)
        self.assertEqual(planner.family_query("bartowski/Qwen_Qwen3-32B-GGUF"), "Qwen Qwen3")
        self.assertEqual(planner.family_query("unsloth/Qwen3-30B-A3B-GGUF"), "Qwen3")


def _gguf_header(tensors, align=32):
    """A minimal GGUF v3 header: general.alignment and the tensor table (offsets relative to the data section)."""
    def s(x):
        b = x.encode()
        return struct.pack("<Q", len(b)) + b
    out = b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", len(tensors), 1)
    out += s("general.alignment") + struct.pack("<I", 4) + struct.pack("<I", align)
    off = 0
    for name, size in tensors:
        out += s(name) + struct.pack("<I", 1) + struct.pack("<Q", size) + struct.pack("<I", 0) + struct.pack("<Q", off)
        off += size
    return out, off


class RemoteHeader(unittest.TestCase):
    def test_tensor_sizes_from_the_header_alone(self):
        t = [("token_embd.weight", 1000), ("blk.0.attn_q.weight", 300), ("blk.0.ffn_up.weight", 700),
             ("output.weight", 500)]
        head, data = _gguf_header(t)
        start = (len(head) + 31) // 32 * 32
        got = dict(gguf.tensors_from(io.BytesIO(head), start + data))
        self.assertEqual(got, dict(t))
        with self.assertRaises(EOFError):
            gguf.tensors_from(io.BytesIO(head[:-10]), start + data)


class Resolve(unittest.TestCase):
    def test_original_weights_use_the_best_published_gguf(self):
        from aero import select_model
        listings = {"Qwen/Qwen3-8B": {"files": [], "mmproj": []},
                    "Qwen/Qwen3-8B-GGUF": {"files": [{"name": "Qwen3-8B-Q4_K_M", "size": 5 * GB,
                                                      "parts": ["Qwen3-8B-Q4_K_M.gguf"], "quant": "Q4_K_M", "bpw": 4.85}],
                                           "mmproj": []}}
        with mock.patch.object(select_model.hf, "list_files", lambda r: listings[r]), \
                mock.patch.object(select_model.hf, "gguf_versions", lambda r: [{"id": "Qwen/Qwen3-8B-GGUF"}]):
            rid, d, note = select_model.resolve("Qwen/Qwen3-8B")
        self.assertEqual(rid, "Qwen/Qwen3-8B-GGUF")
        self.assertIn("only the original weights", note)

    def test_the_models_own_gguf_beats_a_community_copy(self):
        from aero import select_model
        one = {"files": [{"name": "a-Q4_K_M", "size": GB, "parts": ["a.gguf"], "quant": "Q4_K_M", "bpw": 4.85}],
               "mmproj": []}
        with mock.patch.object(select_model.hf, "list_files", lambda r: {"files": [], "mmproj": []} if r == "Qwen/Qwen3-1.7B" else one),                 mock.patch.object(select_model.hf, "gguf_versions",
                                  lambda r: [{"id": "MaziyarPanahi/Qwen3-1.7B-GGUF"}, {"id": "Qwen/Qwen3-1.7B-GGUF"}]):
            rid, _, _ = select_model.resolve("Qwen/Qwen3-1.7B")
        self.assertEqual(rid, "Qwen/Qwen3-1.7B-GGUF")

    def test_nothing_published_makes_one_unless_llama_cpp_cannot(self):
        from aero import select_model
        h = hw("NVIDIA GeForce RTX 3060", 12288, 32768, 24000)
        src = {"params": 8e9, "size": 16 * GB, "arch": "Qwen3ForCausalLM"}
        with mock.patch.object(select_model.hf, "source_info", lambda r: src), \
                mock.patch.object(select_model.quantize, "arch_supported", lambda a: None):
            r = select_model._no_gguf("x/y", h, 11000, 20000)
        self.assertEqual(r["route"], "make")
        with mock.patch.object(select_model.hf, "source_info", lambda r: src), \
                mock.patch.object(select_model.quantize, "arch_supported", lambda a: False):
            r = select_model._no_gguf("x/y", h, 11000, 20000)
        self.assertEqual(r["route"], "airllm")


class SelectJob(unittest.TestCase):
    def test_plan_download_tune_load(self):
        """The job downloads the planned file, registers it and loads it with the automatic VRAM limit."""
        from fastapi.testclient import TestClient

        from aero import server
        plan = {"route": "gguf", "repo": "o/m-GGUF", "asked": "o/m-GGUF", "level": "ok", "summary": "Q4_K_M …",
                "warning": None, "with_proj": False, "mmproj": None, "download_mb": 10, "depth": "short",
                "pick": {"file": "m-Q4_K_M", "quant": "Q4_K_M", "parts": ["m-Q4_K_M.gguf"], "size": 10 * 2**20,
                         "strategy": "gpu", "ctx": 32768, "kv": "q8_0", "est_tg": 40.0}}
        d = Path(tempfile.mkdtemp())
        f = d / "m-Q4_K_M.gguf"
        calls = {}

        def download(repo, paths, emit, cancel, root=None):
            calls["download"] = (repo, list(paths))
            f.write_bytes(b"GGUF")
            return [f]

        def load_job(m, retune, limit, depth, emit, cancel):
            calls["load"] = (m["path"], retune, limit, depth)
            return {"ready": True, "ctx": 32768, "tg": 41.0}
        with mock.patch.object(server, "_select_plan", lambda repo, h: plan), \
                mock.patch.object(server.hf, "download", download), \
                mock.patch.object(server.hf, "local_path", lambda r, p: d / "missing" / p), \
                mock.patch.object(server, "_load_job", load_job):
            c = TestClient(server.app)
            r = c.post("/api/select", json={"repo": "o/m-GGUF"})
            events = [json.loads(x[6:]) for x in r.text.splitlines() if x.startswith("data: ")]
        types = [e["type"] for e in events]
        self.assertIn("plan", types)
        self.assertEqual(calls["download"], ("o/m-GGUF", ["m-Q4_K_M.gguf"]))
        self.assertEqual(calls["load"], (str(f), False, "auto", "short"))
        res = next(e["result"] for e in events if e["type"] == "result")
        self.assertTrue(res["ready"])
        self.assertTrue(res["model_id"])
        self.assertEqual(c.post("/api/select", json={"repo": "../etc"}).status_code, 400)

    def test_load_accepts_an_automatic_limit(self):
        from aero import server
        h = {"gpus": [{"used_mb": 1000}], "vram_total_mb": 16000, "vram_measured": True}
        with mock.patch.object(server, "_model_mb", lambda m: 9000), mock.patch.object(server.ENGINE, "running",
                                                                                         lambda: False):
            gb = server.auto_limit_gb({"path": "x"}, h)
        self.assertAlmostEqual(gb, (16000 - 1000 - server.ADVISOR_MARGIN_MB - 256) / 1024, places=1)


class ReviewFixes(unittest.TestCase):
    """Found by a code review of 1.2.6 before release."""

    def test_a_high_minimum_speed_does_not_break_the_score(self):
        c = {"bpw": 4.5, "quant": "Q4_K_M", "est_tg": 30.0, "ctx": 32768, "kv": "q8_0"}
        for m in (5, 40, 45, 100):
            v = planner.score(c, m, 32768)
            self.assertGreaterEqual(v, 0)
        slow, fast = dict(c, est_tg=50.0), dict(c, est_tg=90.0)
        self.assertGreaterEqual(planner.score(fast, 45, 32768), planner.score(slow, 45, 32768))

    def test_moe_without_an_active_count_is_not_free(self):
        m = dict(MOE, experts_used=0)
        r = run(m, hw("NVIDIA GeForce GTX 1650", 4096, 16384, 12000), 3500)
        r2 = run(MOE, hw("NVIDIA GeForce GTX 1650", 4096, 16384, 12000), 3500)
        self.assertLess(r["pick"]["est_tg"], r2["pick"]["est_tg"])

    def test_the_projector_costs_at_most_a_quant_step(self):
        planner._RAM["gbps"] = 50.0
        t = planner.template_for(DENSE, None, int(DENSE["params"] * 4.89 / 8))
        h = hw("NVIDIA GeForce GTX 1650", 4096, 16384, 12000)
        base = planner.plan(files(DENSE["params"]), t, h, 3500, planner.ram_for_weights(h), 5.0)
        withp = planner.plan(files(DENSE["params"]), t, h, 3500, planner.ram_for_weights(h), 5.0, proj_mb=1500)
        if withp["with_proj"]:
            self.assertGreaterEqual(withp["pick"]["quality"], base["pick"]["quality"] - 0.06)
        else:
            self.assertEqual(withp["pick"]["file"], base["pick"]["file"])

    def test_header_cache_holds_the_header_not_the_window(self):
        from aero import hf
        head, data = _gguf_header([("blk.0.ffn_up.weight", 4096)])
        window = head + b"\0" * (6 * 2**20 - len(head))

        class R:
            status_code = 206

            def raise_for_status(self):
                pass

            def iter_bytes(self, n):
                yield window

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class C:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def stream(self, *a, **k):
                return R()
        hf._HEAD_CACHE.clear()
        with mock.patch.object(hf.httpx, "Client", C):
            got = hf.remote_header("o/r", "x.gguf")
        self.assertLess(len(got), 64 * 1024)
        self.assertEqual(dict(gguf.tensors_from(io.BytesIO(got), len(head) + 64 + 4096))["blk.0.ffn_up.weight"] > 0, True)

    def test_registration_keeps_selects_projector_choice(self):
        from aero import models
        d = Path(tempfile.mkdtemp())
        f = d / "m-Q4_K_M.gguf"
        f.write_bytes(b"GGUF")
        (d / "mmproj-F16.gguf").write_bytes(b"GGUF")
        self.assertIsNone(models.add(f, repo="o/m", mmproj=None, mmproj_auto=False)["mmproj"])
        self.assertTrue(models.add(f, repo="o/m")["mmproj"])                      # the old behaviour elsewhere

    def test_bad_depth_setting_falls_back(self):
        from aero import server
        with mock.patch.object(server, "load_settings", lambda: {"select_depth": "deep", "select_min_tps": "x"}),                 mock.patch("aero.select_model.plan_for", lambda repo, h, b, m: {"route": "gguf", "min": m}):
            r = server._select_plan("o/m", {"gpus": [], "vram_total_mb": 0})
        self.assertEqual((r["depth"], r["min"]), ("medium", 5.0))

    def test_everything_local_needs_no_network(self):
        from fastapi.testclient import TestClient

        from aero import server
        d = Path(tempfile.mkdtemp())
        (d / "m.gguf").write_bytes(b"GGUF")
        plan = {"route": "gguf", "repo": "o/m", "level": "ok", "summary": "", "warning": None, "mmproj": None,
                "download_mb": 0, "depth": "short", "pick": {"file": "m", "quant": "Q4_K_M", "parts": ["m.gguf"],
                                                              "size": 4, "strategy": "gpu", "ctx": 32768, "kv": "q8_0",
                                                              "est_tg": 9}}
        with mock.patch.object(server, "_select_plan", lambda repo, h: plan),                 mock.patch.object(server.hf, "download", mock.Mock(side_effect=AssertionError("network"))),                 mock.patch.object(server.hf, "local_path", lambda r, p: d / p),                 mock.patch.object(server, "_load_job", lambda *a: {"ready": True}):
            r = TestClient(server.app).post("/api/select", json={"repo": "o/m"})
        self.assertIn('"ready": true', r.text)


if __name__ == "__main__":
    unittest.main()
