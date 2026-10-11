"""Remote Mode tests (remote_sessions.py, vram_policy.py). Every remote session here is SIMULATED: RDP session lists,
RustDesk process lists and logind answers are fed in, and the model "reloads" are fakes that record the config and
return llama.cpp log text in the real format (copied from llama.cpp 0.6.0 on an RTX 5080). The real-hardware
measurement is validation/measure_remote_mode.py (see docs/V1.1_PERFORMANCE_REPORT.md). Run from source/:
    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aero import remote_sessions as rs, vram_policy as vp  # noqa: E402

# llama.cpp 0.6.0 (build 11501) with -lv 4, Qwen3.8-27B IQ2_M, normal (all on GPU) and -ngl 53
LOG_NORMAL = """ARGS: -m model.gguf -ngl 999
0.00.388.472 I load_tensors: offloaded 66/66 layers to GPU
0.00.388.475 I load_tensors:   CPU_Mapped model buffer size =   521.00 MiB
0.00.388.476 I load_tensors:        CUDA0 model buffer size = 10601.05 MiB
0.00.654.238 I llama_context:  CUDA_Host  output buffer size =     0.95 MiB
0.00.658.310 I llama_kv_cache:      CUDA0 KV buffer size =   272.00 MiB
0.00.665.238 I sched_reserve:      CUDA0 compute buffer size =   150.29 MiB
0.00.665.242 I sched_reserve:  CUDA_Host compute buffer size =    48.29 MiB
"""
LOG_REMOTE = """ARGS: -m model.gguf -ngl 53
0.00.388.472 I load_tensors: offloaded 53/66 layers to GPU
0.00.388.475 I load_tensors:   CPU_Mapped model buffer size =  2243.80 MiB
0.00.388.476 I load_tensors:        CUDA0 model buffer size =  8878.25 MiB
0.00.658.310 I llama_kv_cache:      CUDA0 KV buffer size =   221.00 MiB
0.00.658.311 I llama_kv_cache:        CPU KV buffer size =    51.00 MiB
0.00.665.238 I sched_reserve:      CUDA0 compute buffer size =   207.21 MiB
"""

S = {"remote_mode": "auto", "remote_debounce_s": 8, "remote_restore_cooldown_s": 90, "remote_gpu_weight_fraction": 0.8,
     "remote_min_free_vram_mb": 0, "remote_providers": {"rdp": True, "rustdesk": True, "logind": True},
     "remote_allow_probable": False}


def rdp(*states):
    """A WTS session list: the console plus RDP sessions in the given states."""
    out = [{"id": 1, "station": "Console", "state": "active", "protocol": 0, "mine": True}]
    for i, st in enumerate(states):
        out.append({"id": 2 + i, "station": f"RDP-Tcp#{i}", "state": st, "protocol": 2, "mine": False})
    return out


def snap(rdp_list=(), procs=(), settings=None, rustdesk_installed=False):
    return rs.snapshot({**S, **(settings or {})}, procs=list(procs), rdp_list=list(rdp_list) or rdp(),
                       logind_run=None, rustdesk_installed=rustdesk_installed)


class Detection(unittest.TestCase):
    def setUp(self):
        rs.clear_pushed()

    def test_rdp_states(self):
        self.assertEqual(snap(rdp())["state"], "local")
        s = snap(rdp("active"))
        self.assertEqual((s["state"], s["viewers"], s["verified"]), ("remote_active", 1, 1))
        self.assertEqual(snap(rdp("connect_query"))["state"], "remote_connecting")
        self.assertEqual(snap(rdp("disconnected"))["state"], "local")       # a dropped session is not a viewer
        self.assertEqual(snap(rdp("active", "active"))["viewers"], 1)        # one provider, two sessions
        p = next(x for x in snap(rdp("active", "active"))["providers"] if x["name"] == "rdp")
        self.assertEqual(len(p["sessions"]), 2)

    def test_rustdesk_running_is_not_a_viewer(self):
        installed = snap(rdp(), procs=[], rustdesk_installed=True)
        self.assertEqual(installed["state"], "local")
        service = snap(rdp(), procs=[("rustdesk.exe", []), ("rustdesk.exe", ["rustdesk.exe", "--tray"])])
        p = next(x for x in service["providers"] if x["name"] == "rustdesk")
        self.assertEqual((p["running"], p["viewer"], p["confidence"]), (True, None, "unknown"))
        self.assertEqual(service["state"], "local")                         # RDP says local; RustDesk can't tell

    def test_rustdesk_connection_window_is_only_probable(self):
        procs = [("rustdesk.exe", ["rustdesk.exe", "--cm"])]
        s = snap(rdp(), procs=procs)
        self.assertEqual((s["state"], s["probable"], s["viewers"]), ("local", 1, 0))
        s = snap(rdp(), procs=procs, settings={"remote_allow_probable": True})
        self.assertEqual(s["state"], "remote_active")
        self.assertIn("RustDesk (probable)", s["counted"])

    def test_verified_event_from_an_integration(self):
        rs.push_event("rustdesk", True, "peer 123", verified=True)
        s = snap(rdp(), procs=[("rustdesk.exe", [])])
        self.assertEqual((s["state"], s["verified"]), ("remote_active", 1))
        rs.push_event("rustdesk", False, verified=True)
        self.assertEqual(snap(rdp(), procs=[("rustdesk.exe", [])])["state"], "local")

    def test_unknown_is_never_active(self):
        s = rs.snapshot({**S, "remote_providers": {"rdp": False, "logind": False}},
                        procs=[("anydesk.exe", []), ("rustdesk.exe", [])], rdp_list=[], rustdesk_installed=True)
        self.assertEqual(s["state"], "unknown")
        self.assertEqual(s["viewers"], 0)

    def test_logind_remote_session(self):
        class R:
            def __init__(self, out):
                self.stdout = out
        old = os.environ.get("XDG_SESSION_ID")
        os.environ["XDG_SESSION_ID"] = "c7"
        try:
            p = rs._logind(lambda args: R("Remote=yes\nState=active\nType=x11\n"))
            self.assertEqual((p["viewer"], p["confidence"]), (True, "verified"))
            p = rs._logind(lambda args: R("Remote=no\nState=active\n"))
            self.assertFalse(p["viewer"])
        finally:
            if old is None:
                os.environ.pop("XDG_SESSION_ID", None)
            else:
                os.environ["XDG_SESSION_ID"] = old


# ------------------------------------------------------------------------------------------------ planning

def layers(n=64, per=128 << 20, output=2425 << 20, embd=521 << 20, experts=None, nextn=1):
    """A layer table shaped like the measured Qwen3.8-27B (64 layers + 1 unused MTP block)."""
    ls = [per] * n + [0] * nextn
    return {"n_layer": n + nextn, "layers": ls, "experts": experts or [0] * (n + nextn), "output": output, "embd": embd,
            "other": 0, "nextn": 450 << 20, "total": sum(ls) + output + embd}


class Planning(unittest.TestCase):
    def test_semantics_match_llama_cpp(self):
        lb = layers()
        self.assertAlmostEqual(vp.fraction(lb, 999), (lb["total"] - lb["embd"]) / lb["total"])
        self.assertEqual(vp.gpu_bytes(lb, 1), lb["output"])            # the output layer goes first
        self.assertEqual(vp.gpu_bytes(lb, 0), 0)

    def test_plan_lands_on_the_target(self):
        lb = layers()
        p = vp.plan(lb, {"ctx": 8192, "ngl": 999, "kv": "q8_0"}, 0.8)
        self.assertLessEqual(p["after"], 0.805)
        self.assertGreater(p["after"], 0.75)
        self.assertNotEqual(p["cfg"]["ngl"], int(65 * 0.8))           # not floor(0.8 * layers)

    def test_nonuniform_layers(self):
        lb = layers()
        lb["layers"] = [64 << 20] * 32 + [400 << 20] * 32 + [0]
        lb["total"] = sum(lb["layers"]) + lb["output"] + lb["embd"]
        p = vp.plan(lb, {"ngl": 999, "ctx": 4096}, 0.8)
        self.assertLessEqual(p["after"], 0.805)
        self.assertGreater(p["after"], 0.7)

    def test_moe_moves_experts_first(self):
        n = 48
        ex = [300 << 20] * n + [0]
        lb = layers(n=n, per=360 << 20, experts=ex)
        p = vp.plan(lb, {"ngl": 999, "ctx": 8192}, 0.8)
        self.assertEqual(p["cfg"]["ngl"], 999)
        self.assertGreater(p["cfg"]["ncmoe"], 0)
        self.assertLessEqual(p["after"], 0.805)

    def test_nothing_to_do(self):
        lb = layers()
        self.assertIsNone(vp.plan(lb, {"ngl": 0, "ctx": 4096})["cfg"])            # CPU only
        self.assertIsNone(vp.plan(lb, {"ngl": 40, "ctx": 4096})["cfg"])           # already below 80%

    def test_buffers_from_the_real_log_format(self):
        b = vp.buffers(LOG_REMOTE)
        self.assertEqual(b["gpu"]["model"], 8878.25)
        self.assertEqual(b["cpu"]["model"], 2243.8)
        self.assertEqual(b["offloaded"], (53, 66))
        self.assertAlmostEqual(b["weights_gpu_share"], 0.7983, places=3)
        self.assertEqual(vp.buffers(LOG_NORMAL)["weights_gpu_share"], round(10601.05 / (10601.05 + 521), 4))

    def test_buffers_per_device(self):
        b = vp.buffers("CUDA0 model buffer size = 6000.00 MiB\nCUDA1 model buffer size = 4000.00 MiB\n"
                       "CPU_Mapped model buffer size = 1000.00 MiB\n")
        self.assertEqual(b["devices"], {"CUDA0": 6000.0, "CUDA1": 4000.0})
        self.assertAlmostEqual(b["weights_gpu_share"], 10000 / 11000, places=4)


# ------------------------------------------------------------------------------------------------ the policy

class FakeHooks:
    def __init__(self, gpu=True, unified=False, fail=(), ram=64000, busy=False):
        self.cfg = {"ctx": 8192, "ngl": 999, "kv": "q8_0", "fa": True, "ub": 512}
        self.loads, self.held, self.gpu, self.unified, self.fail, self.ram, self.is_busy = [], [], gpu, unified, \
            list(fail), ram, busy

    def engine(self):
        return {"ready": True, "model_id": "m1", "gpu": self.gpu, "unified": self.unified}

    def current(self):
        return {"model_path": "model.gguf", "cfg": dict(self.cfg), "log": LOG_NORMAL if self.cfg["ngl"] == 999 else LOG_REMOTE}

    def reload(self, cfg):
        self.loads.append(dict(cfg))
        if self.fail and self.fail.pop(0):
            return False, {"error": "out of memory", "log": ""}
        self.cfg = dict(cfg)
        return True, {"log": LOG_NORMAL if cfg["ngl"] == 999 else LOG_REMOTE, "load_s": 4.2}

    def busy(self):
        return self.is_busy

    def hold(self, on):
        self.held.append(on)

    def vram(self):
        return {"used_mb": 12141 if self.cfg["ngl"] == 999 else 10393, "free_mb": 4000, "total_mb": 16303, "per_gpu": []}

    def ram_free_mb(self):
        return self.ram


class Det:
    """A detector replaying a list of states."""

    def __init__(self):
        self.state = "local"

    def read(self):
        return {"state": self.state, "counted": [], "providers": []}


def runner(hooks, settings=None):
    r = vp.Runner(hooks, Det(), vp.Policy(None))
    r.layer_cache[("model.gguf", False)] = layers()
    vp.load_settings = lambda: {**S, **(settings or {})}           # the runner reads settings each step
    return r


class Policy(unittest.TestCase):
    def setUp(self):
        self._ls = vp.load_settings

    def tearDown(self):
        vp.load_settings = self._ls

    def steps(self, r, plan):
        """plan: [(seconds, detector state)]; returns actions."""
        acts = []
        for t, state in plan:
            r.detector.state = state
            acts.append(r.step(now=t))
        return acts

    def test_connect_once_reconnect_no_pingpong_restore_exact(self):
        h = FakeHooks()
        r = runner(h)
        acts = self.steps(r, [(0, "remote_active"), (3, "remote_active"), (9, "remote_active"), (12, "remote_active")])
        self.assertEqual(acts, [None, None, "enter", None])            # debounced: one transition
        self.assertEqual(r.policy.state, "REMOTE")
        self.assertEqual(h.loads[-1]["ngl"], 53)
        self.assertAlmostEqual(r.policy.measured["weights_gpu_share"], 0.7983, places=3)
        self.assertEqual(r.policy.measured["vram_freed_mb"], 12141 - 10393)
        self.assertEqual(h.held, [True, False])                          # chats held during the reload only
        # the viewer drops and comes back within the cooldown: no reload either way
        acts = self.steps(r, [(20, "remote_disconnected"), (40, "remote_reconnecting"), (50, "remote_active")])
        self.assertEqual(acts, [None, None, None])
        self.assertEqual(len(h.loads), 1)
        # last viewer leaves for good: restore after the cooldown, to exactly the profile from before
        acts = self.steps(r, [(60, "local"), (100, "local"), (151, "local")])
        self.assertEqual(acts, [None, None, "restore"])
        self.assertEqual(h.loads[-1], {"ctx": 8192, "ngl": 999, "kv": "q8_0", "fa": True, "ub": 512})
        self.assertEqual(r.policy.state, "NORMAL")

    def test_two_viewers_one_leaves(self):
        h = FakeHooks()
        r = runner(h)
        self.steps(r, [(0, "remote_active"), (9, "remote_active")])
        # the detector still counts one verified viewer: state stays remote_active
        self.assertEqual(self.steps(r, [(30, "remote_active"), (200, "remote_active")]), [None, None])
        self.assertEqual(r.policy.state, "REMOTE")

    def test_unknown_never_switches_or_restores(self):
        h = FakeHooks()
        r = runner(h)
        self.assertEqual(self.steps(r, [(0, "unknown"), (100, "unknown")]), [None, None])
        self.steps(r, [(200, "remote_active"), (209, "remote_active")])
        self.assertEqual(self.steps(r, [(300, "unknown"), (1000, "unknown")]), [None, None])
        self.assertEqual(r.policy.state, "REMOTE")

    def test_no_gpu_or_unified_memory(self):
        for h in (FakeHooks(gpu=False), FakeHooks(unified=True)):
            r = runner(h)
            self.assertEqual(self.steps(r, [(0, "remote_active"), (20, "remote_active")]), [None, None])
            self.assertEqual(h.loads, [])

    def test_too_little_ram_fails_safe(self):
        h = FakeHooks(ram=1500)
        r = runner(h)
        self.steps(r, [(0, "remote_active"), (9, "remote_active")])
        self.assertEqual(r.policy.state, "FAILED_SAFE")
        self.assertIn("not enough free RAM", r.policy.error)
        self.assertEqual(h.loads, [])
        self.assertEqual(self.steps(r, [(20, "remote_active"), (40, "remote_active")]), [None, None])   # no retry loop
        self.steps(r, [(60, "local")])
        self.assertEqual(r.policy.state, "NORMAL")                      # next remote session may try again

    def test_failed_load_rolls_back(self):
        h = FakeHooks(fail=[True, False])                               # Remote profile fails, rollback works
        r = runner(h)
        self.steps(r, [(0, "remote_active"), (9, "remote_active")])
        self.assertEqual(r.policy.state, "FAILED_SAFE")
        self.assertEqual([c["ngl"] for c in h.loads], [53, 999])
        self.assertEqual(h.cfg["ngl"], 999)
        self.assertEqual(h.held, [True, False])

    def test_waits_for_the_reply_in_progress(self):
        h = FakeHooks(busy=True)
        r = runner(h)
        r._wait_idle = lambda limit=600: False                         # a reply that never ends
        self.steps(r, [(0, "remote_active"), (9, "remote_active")])
        self.assertEqual(h.loads, [])                                    # nothing interrupted
        self.assertEqual(r.policy.state, "NORMAL")                       # still waiting, not failed
        self.assertIn("reply in progress", r.policy.wait_note)
        r._wait_idle = lambda limit=600: True                          # the reply finished
        self.assertEqual(self.steps(r, [(30, "remote_active")]), ["enter"])
        self.assertEqual(r.policy.state, "REMOTE")

    def test_manual_on_and_off(self):
        h = FakeHooks()
        r = runner(h, {"remote_mode": "on"})
        self.assertEqual(self.steps(r, [(0, "local")]), ["enter"])
        vp.load_settings = lambda: {**S, "remote_mode": "off"}
        self.assertEqual(self.steps(r, [(1, "remote_active")]), ["restore"])
        self.assertEqual(h.cfg["ngl"], 999)

    def test_min_free_vram_lowers_further(self):
        h = FakeHooks()
        h.vram = lambda: {"used_mb": 10000, "free_mb": 1000, "total_mb": 16303, "per_gpu": []}
        r = runner(h, {"remote_min_free_vram_mb": 2000})
        self.steps(r, [(0, "remote_active"), (9, "remote_active")])
        self.assertEqual(len(h.loads), 2)
        self.assertLess(h.loads[1]["ngl"], h.loads[0]["ngl"])


class Hold(unittest.TestCase):
    def test_new_chats_wait_while_reloading(self):
        import asyncio
        vp.hold(True)

        async def go():
            evs = []
            t = asyncio.get_running_loop().time()

            async def release():
                await asyncio.sleep(0.6)
                vp.hold(False)
            asyncio.create_task(release())
            async for ev in vp.wait_for_model():
                evs.append(ev)
            return evs, asyncio.get_running_loop().time() - t
        evs, waited = asyncio.run(go())
        self.assertEqual(len(evs), 1)
        self.assertGreaterEqual(waited, 0.5)


if __name__ == "__main__":
    unittest.main()
