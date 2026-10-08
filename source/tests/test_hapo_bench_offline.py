"""Unit tests for HAPO profile picking, benchmark scoring and strict offline. Run from source/:
    python -m unittest discover -s tests -v
They use a throwaway AERO_HOME, so they never touch real settings, models or data."""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from aero import bench, config, hapo, localonly  # noqa: E402


def trial(n, ctx, tg, pp=1000, mem=8000, kv="q8_0", k=0, ok=True, retest=False):
    return {"n": n, "ok": ok, "retest": retest, "tg": tg, "pp": pp, "mem_mb": mem,
            "knobs": {"ctx": ctx, "k": k, "kv": kv, "ub": 512, "t": 16},
            "desc": f"ctx {ctx:,} | gpu {64 - k}/64 | kv {kv} | ub 512"}


ENTRY = {"meta": {"n_layer": 64}, "trial_n": 4, "desc": "x", "tg": 60, "pp": 900, "config": {"ctx": 32768},
         "trials": [trial(1, 16384, 64, mem=12000), trial(2, 32768, 60, mem=13500), trial(3, 65536, 41, mem=15400),
                    trial(4, 32768, 58, mem=13400, kv="f16"), trial(5, 8192, 65, mem=10900),
                    trial(6, 131072, 20, mem=15900, kv="q4_0"), trial(7, 32768, 61, ok=False),
                    trial(9, 16384, 50, mem=14000),
                    trial(8, 32768, 99, retest=True)]}
HW = {"cores": 16}


class HapoTests(unittest.TestCase):
    def setUp(self):
        self.c = hapo.candidates(ENTRY, HW)

    def test_candidates_skip_failed_and_retests(self):
        self.assertEqual(sorted(c["n"] for c in self.c), [1, 2, 3, 4, 5, 6, 9])

    def test_rules(self):
        self.assertEqual(hapo.pick("max_speed", self.c)["n"], 1)      # 64 tok/s is within 5% of 65 and has 16k
        self.assertEqual(hapo.pick("balanced", self.c)["n"], 4)       # largest ctx keeping 52 tok/s is 32k; f16 KV breaks the tie
        self.assertEqual(hapo.pick("max_context", self.c)["n"], 3)    # 41 >= 32.5 at 64k; 131k at 20 is below
        self.assertEqual(hapo.pick("max_quality", self.c)["n"], 4)    # f16 KV wins
        self.assertEqual(hapo.pick("agent", self.c)["n"], 2)          # >=32k, fastest
        self.assertEqual(hapo.pick("efficiency", self.c)["n"], 1)     # least VRAM at >=16k and >=70% speed

    def test_unmeasured_goal_says_so(self):
        only_short = [c for c in self.c if c["knobs"]["ctx"] < 32768]
        self.assertIsNone(hapo.pick("agent", only_short))
        p = hapo.profiles({**ENTRY, "trials": [t for t in ENTRY["trials"] if t["knobs"]["ctx"] < 32768]}, HW, "k0")
        agent = next(x for x in p["profiles"] if x["id"] == "agent")
        self.assertFalse(agent["measured"])
        self.assertIn("32k", agent["why"])

    def test_pareto(self):
        front = set(hapo.pareto(self.c))
        self.assertIn(5, front)         # fastest
        self.assertIn(6, front)         # most context
        self.assertNotIn(9, front)      # trial 1 is faster, same context and leaner
        self.assertTrue({2, 4} <= front)  # 2 is faster, 4 is leaner: neither dominates the other

    def test_old_cache_desc_parses(self):
        k = hapo.knobs_of({"desc": "ctx 32,768 | gpu 60/64 | kv q8_0 | ub 512 | 12 threads"}, ENTRY, HW)
        self.assertEqual(k, {"ctx": 32768, "k": 4, "kv": "q8_0", "ub": 512, "t": 12})
        k = hapo.knobs_of({"desc": "ctx 8,192 | CPU only | kv f16 | ub 256 | 4 threads"}, ENTRY, HW)
        self.assertEqual((k["ctx"], k["kv"], k["t"]), (8192, "f16", 4))

    def test_apply_restore_failed(self):
        key = "test-key"
        cfg = lambda knobs: {"ctx": knobs["ctx"], "kv": knobs["kv"]}      # noqa: E731
        a = hapo.apply(key, "max_context", ENTRY, HW, cfg, "fp1")
        self.assertEqual(a["trial"], 3)
        self.assertEqual(hapo.apply(key, "max_context", ENTRY, HW, cfg, "fp1")["trial"], 3)   # no-op, no history
        hapo.apply(key, "max_quality", ENTRY, HW, cfg, "fp1")
        self.assertEqual(hapo.restore(key)["profile"], "max_context")
        self.assertIsNone(hapo.restore(key))                                # back to the tuner's pick
        with self.assertRaises(ValueError):
            hapo.restore(key)
        hapo.apply(key, "agent", ENTRY, HW, cfg, "fp1")
        bad = hapo.failed(key, "out of memory")
        self.assertEqual(bad["profile"], "agent")
        self.assertIsNone(hapo.active(key))


class BenchTests(unittest.TestCase):
    def test_summary(self):
        s = bench.summary([10, 12, 11, 30, None])
        self.assertEqual((s["n"], s["median"], s["p95"], s["max"]), (4, 11.5, 30.0, 30.0))
        self.assertIsNone(bench.summary([]))

    def test_json_parsing_and_matching(self):
        self.assertEqual(bench.parse_json_reply('<think>x</think>```json\n{"a": 1}\n```'), {"a": 1})
        self.assertTrue(bench._matches("Dana Whitfield", "Dana Whitfield"))
        self.assertTrue(bench._matches(" thursday ", "Thursday"))
        self.assertTrue(bench._matches("16", 16))
        self.assertFalse(bench._matches("seventeen", 17))

    def test_router_suite_scoring(self):
        def decide(hist, cat):
            q = hist[-1]["content"].lower()
            tools = ["read_file"] if "read" in q else ["run_command"] if "disk" in q else []
            return {"tools": tools}, {"ms": 40}
        cat = [("read_file", "files_read", ""), ("run_command", "shell", "")]
        r = bench.router_suite(decide, cat, lambda e: None)
        rows = {x["request"]: x for x in r["rows"]}
        self.assertTrue(rows["hi there!"]["ok"])
        self.assertTrue(rows["Read C:\\notes\\todo.txt and tell me what's left on it."]["ok"])
        self.assertIn("skipped", rows["Search the web for the newest llama.cpp release notes."])
        self.assertEqual(r["errors"], 0)
        self.assertGreater(r["accuracy"], 0.5)

    def test_report_markdown_marks_missing(self):
        md = bench.to_markdown({"kind": "model", "depth": "quick", "results": {"chat": {}, "memory": {}}})
        self.assertIn("not measured", md)


class OfflineTests(unittest.TestCase):
    def setUp(self):
        localonly.install()
        config.save_settings({"strict_offline": False})

    def tearDown(self):
        config.save_settings({"strict_offline": False})

    def test_loopback(self):
        for h in ("127.0.0.1", "localhost", "::1", "[::1]", "127.8.9.10"):
            self.assertTrue(localonly.is_loopback(h), h)
        for h in ("192.168.1.10", "huggingface.co", "0.0.0.0", ""):
            self.assertFalse(localonly.is_loopback(h), h)

    def test_strict_blocks_remote_allows_loopback(self):
        config.save_settings({"strict_offline": True})
        with self.assertRaises(localonly.OfflineBlocked):
            httpx.get("https://example.com/", timeout=5)
        # loopback still works: a tiny local HTTP server
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        self.assertEqual(httpx.get(f"http://127.0.0.1:{srv.server_port}/", timeout=5).text, "ok")
        srv.server_close()
        last = localonly.recent(5)[-1]
        self.assertEqual((last["host"], last["allowed"]), ("example.com", False))

    def test_strict_hides_network_tools(self):
        s = config.load_settings()
        s["strict_offline"] = True
        self.assertFalse(localonly.tool_allowed("web", s))
        self.assertFalse(localonly.tool_allowed("mcp", s))
        self.assertTrue(localonly.tool_allowed("files_read", s))
        from aero import tools
        tools.REGISTRY.clear()
        tools.tool("web_search", "search", "web")(lambda ctx, query: "results")
        tools.tool("read_file", "read", "files_read")(lambda ctx, path: "text")
        names = [x["function"]["name"] for x in tools.schemas(s)]
        self.assertEqual(names, ["read_file"])
        r = tools.run("web_search", {"query": "x"}, tools.Ctx(s))
        self.assertTrue(r["error"])
        self.assertIn("strict offline", r["text"])

    def test_async_client_blocked(self):
        import asyncio
        config.save_settings({"strict_offline": True})

        async def go():
            async with httpx.AsyncClient() as c:
                await c.get("https://example.org/")
        with self.assertRaises(localonly.OfflineBlocked):
            asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
