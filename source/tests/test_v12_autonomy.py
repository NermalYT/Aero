"""v1.2 autonomy tests against a scripted model: the batch queue (collect, keep working, approve in one go, run the
approved actions in the same turn or at the start of the next), full automation with an authority model (allowed,
denied, never allowed when the authority can't answer, physical input), Stop dropping queued actions, and the router's
1.2 decision form. Run from source/:
    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_v11_agent import Model, results, run_turn, settings  # noqa: E402

from aero import agent, agents, autonomy, input_guard, resources, router, tools  # noqa: E402


def _reset():
    autonomy._items.clear()
    autonomy._waiters.clear()
    agents.LIVE.clear()


class Queue(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_same_action_is_queued_once_and_numbered_per_chat(self):
        a, c1 = autonomy.enqueue("c1", "x1", "write_file", {"path": "a"}, "a", "files_write")
        b, c2 = autonomy.enqueue("c1", "x2", "write_file", {"path": "a"}, "a", "files_write")
        c, _ = autonomy.enqueue("c1", "x3", "write_file", {"path": "b"}, "b", "files_write")
        d, _ = autonomy.enqueue("c2", "x4", "write_file", {"path": "a"}, "a", "files_write")
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual((a["n"], c["n"], d["n"]), (1, 2, 1))
        self.assertIn("NOT run", autonomy.queued_text(a, True))

    def test_decide_take_and_cancel(self):
        a, _ = autonomy.enqueue("c1", "x1", "write_file", {"path": "a"}, "a", "files_write")
        b, _ = autonomy.enqueue("c1", "x2", "delete_path", {"path": "b"}, "b", "files_write")
        c, _ = autonomy.enqueue("c1", "x3", "move_path", {"src": "c"}, "c", "files_write")
        autonomy.decide("c1", {a["id"]: "approve", b["id"]: "deny"})
        got = autonomy.take_decided("c1")
        self.assertEqual([(x["id"], x["status"]) for x in got], [(a["id"], "approved"), (b["id"], "denied")])
        self.assertEqual(autonomy.take_decided("c1"), [])            # handed out once
        self.assertEqual([x["id"] for x in autonomy.pending("c1")], [c["id"]])
        autonomy.cancel_chat("c1")
        self.assertEqual(autonomy.pending("c1"), [])
        self.assertEqual(autonomy._items[c["id"]]["status"], "cancelled")

    def test_mode_per_chat_and_loop(self):
        s = {"approval_mode": "batch", "loop_approval_mode": "auto"}
        self.assertEqual(autonomy.mode_for(s), "batch")
        self.assertEqual(autonomy.mode_for(s, loop=True), "auto")
        self.assertEqual(autonomy.mode_for({"approval_mode": "nonsense"}), "ask")

    def test_authority_prompt_names_untrusted_content_and_the_action(self):
        p = autonomy.prompt("Summarize this page", {"name": "send_email", "category": "mcp", "side_effect":
                                                     "external_write", "label": "to x@y", "args": {"to": "x@y"}},
                            ["fetch_url https://a.example: 3,000 words"], ["https://a.example"])
        self.assertIn("Untrusted content read this turn (web pages, email, downloaded files): https://a.example", p)
        self.assertIn("tool: send_email", p)
        self.assertIn('"to": "x@y"', p)

    def test_exam_has_traps_and_ordinary_work(self):
        wants = [c[-1] for c in autonomy.EXAM]
        self.assertGreaterEqual(len(wants), 30)
        self.assertGreaterEqual(wants.count("deny"), 12)
        self.assertGreaterEqual(wants.count("allow"), 12)


class Batch(unittest.TestCase):
    def setUp(self):
        _reset()
        self.dir = Path(tempfile.mkdtemp(prefix="aero-batch-"))
        self.target = self.dir / "notes.txt"

    def _model(self):
        return Model([
            ("", [("write_file", {"path": str(self.target), "content": "hello"})]),
            ("", [("list_dir", {"path": str(self.dir)})]),       # keeps working while the write waits
            ("Wrote the notes once you approved.", []),
            ("All done.", []),
        ])

    def test_queued_action_runs_in_the_same_turn_after_approval(self):
        seen = {}

        def on(ev):
            if ev["t"] == "queued":
                seen["queued_exists"] = self.target.exists()
            if ev["t"] == "batch_wait":
                seen["wait_exists"] = self.target.exists()
                autonomy.decide("b-chat", {"*": "approve"})
        s = settings(approval_mode="batch", batch_wait_s=30, work_dir=str(self.dir))
        evs = run_turn(self._model(), s, chat_id="b-chat", on_event=on)
        self.assertFalse(seen["queued_exists"])
        self.assertFalse(seen["wait_exists"])                     # nothing ran before the user said yes
        self.assertEqual(self.target.read_text(encoding="utf-8"), "hello")
        writes = results(evs, "write_file")
        self.assertTrue(writes[0].get("queued"))
        self.assertIn("NOT run", writes[0]["content"])
        self.assertIn("approved by the user and run now", writes[1]["content"])
        self.assertTrue(writes[1].get("approved_batch"))
        self.assertTrue(any(e["t"] == "tool_result" and e["message"]["name"] == "list_dir" for e in evs))
        self.assertFalse(any(e["t"] == "tool_start" and e.get("needs_approval") for e in evs))  # no per-action card

    def test_denied_action_never_runs(self):
        def on(ev):
            if ev["t"] == "batch_wait":
                autonomy.decide("b-chat2", {"*": "deny"})
        s = settings(approval_mode="batch", batch_wait_s=30, work_dir=str(self.dir))
        evs = run_turn(self._model(), s, chat_id="b-chat2", on_event=on)
        self.assertFalse(self.target.exists())
        last = results(evs, "write_file")[-1]
        self.assertTrue(last.get("denied"))

    def test_loop_round_does_not_wait_and_next_turn_runs_the_approved_action(self):
        s = settings(approval_mode="ask", loop_approval_mode="batch", batch_wait_s=30, work_dir=str(self.dir))
        t0 = time.time()
        evs = _run_loop_turn(self._model(), s, "loop-chat")
        self.assertLess(time.time() - t0, 20)
        self.assertFalse(any(e["t"] == "batch_wait" for e in evs))
        self.assertFalse(self.target.exists())
        self.assertEqual(len(autonomy.pending("loop-chat")), 1)
        autonomy.decide("loop-chat", {"*": "approve"})
        evs2 = _run_loop_turn(Model([("Done.", [])]), s, "loop-chat")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "hello")
        self.assertIn("approved by the user and run now", results(evs2, "write_file")[0]["content"])

    def test_stop_drops_queued_actions(self):
        def on(ev):
            if ev["t"] == "batch_wait":
                agent.stop("b-chat3")
        s = settings(approval_mode="batch", batch_wait_s=30, work_dir=str(self.dir))
        run_turn(self._model(), s, chat_id="b-chat3", on_event=on)
        self.assertEqual(autonomy.pending("b-chat3"), [])
        self.assertFalse(self.target.exists())


def _run_loop_turn(model, s, chat_id):
    """run_turn with opts.loop (a forever-loop round)."""
    import asyncio

    import httpx

    from aero import pipeline
    from test_v11_agent import REAL_CLIENT

    def client(*a, **k):
        return REAL_CLIENT(transport=httpx.MockTransport(model.handle), timeout=30)
    hist = [{"role": "user", "content": "keep the notes file up to date", "ts": time.time(),
             "loop": {"iteration": 1}}]
    events = []

    async def go():
        async for chunk in pipeline.run_turn(chat_id, hist, s, {"url": "http://fake", "ctx": 32768,
                                                                "model": {"name": "Qwen3.8-27B"}},
                                             opts={"loop": {"iteration": 1, "started": time.time()}}):
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
    with mock.patch.object(agent.httpx, "AsyncClient", client):
        asyncio.run(go())
    return events


class Auto(unittest.TestCase):
    def setUp(self):
        _reset()
        self.dir = Path(tempfile.mkdtemp(prefix="aero-auto-"))
        self.target = self.dir / "out.txt"
        self.model = Model([("", [("write_file", {"path": str(self.target), "content": "x"})]), ("Done.", [])])

    def _turn(self, verdict=None, error=None):
        calls = []

        def judge(task, action, recent=(), untrusted=(), url=None, timeout=60):
            calls.append((task, action))
            if error:
                raise error
            return {"decision": verdict, "reason": f"because {verdict}", "ms": 5, "model": "test-authority"}
        s = settings(approval_mode="auto", work_dir=str(self.dir))
        with mock.patch.object(autonomy, "judge", judge), mock.patch.object(autonomy, "log", lambda e: None):
            evs = run_turn(self.model, s, text="save x to out.txt", chat_id="a-chat")
        return evs, calls

    def test_allowed_by_the_authority(self):
        evs, calls = self._turn("allow")
        self.assertEqual(calls[0][0], "save x to out.txt")
        self.assertEqual(calls[0][1]["name"], "write_file")
        self.assertTrue(self.target.exists())
        r = results(evs, "write_file")[0]
        self.assertEqual(r["authority"]["decision"], "allow")
        self.assertTrue(any(e["t"] == "authority" and e["decision"] == "allow" for e in evs))
        self.assertFalse(any(e["t"] == "tool_start" and e.get("needs_approval") for e in evs))

    def test_denied_by_the_authority(self):
        evs, _ = self._turn("deny")
        self.assertFalse(self.target.exists())
        r = results(evs, "write_file")[0]
        self.assertTrue(r["denied"])
        self.assertIn("authority model denied", r["content"])
        self.assertIn("because deny", r["content"])

    def test_authority_unavailable_means_denied(self):
        evs, _ = self._turn(error=RuntimeError("no model"))
        self.assertFalse(self.target.exists())
        self.assertTrue(results(evs, "write_file")[0]["denied"])

    def test_physical_input_goes_through_the_authority_and_a_one_time_grant(self):
        ran = []
        real = tools.run

        def run(name, args, ctx):
            if name == "type_text":
                ran.append((getattr(ctx, "physical_ok", False), resources.holder(resources.EXCLUSIVE_INPUT)))
                return {"text": "typed", "error": False}
            return real(name, args, ctx)
        model = Model([("", [("type_text", {"text": "hi"})]), ("Done.", [])])
        s = settings(approval_mode="auto")
        judge = mock.Mock(return_value={"decision": "allow", "reason": "asked", "ms": 3, "model": "t"})
        with mock.patch.object(tools.REGISTRY["type_text"], "available", lambda: True), \
                mock.patch.object(agent.tools, "run", run), \
                mock.patch.object(input_guard, "idle_ms", lambda: 10 ** 6), \
                mock.patch.object(input_guard, "restore_foreground", lambda h: True), \
                mock.patch.object(agent.control, "banner_on", lambda *a, **k: None), \
                mock.patch.object(agent.control, "banner_off", lambda *a, **k: None), \
                mock.patch.object(autonomy, "judge", judge), mock.patch.object(autonomy, "log", lambda e: None):
            evs = run_turn(model, s, text="type hi", chat_id="a-phys")
        self.assertEqual(len(ran), 1)
        self.assertTrue(ran[0][0])                                # physical_ok
        self.assertEqual(ran[0][1]["owner"], "a-phys")            # held the exclusive input lock
        self.assertTrue(judge.call_args[0][1]["physical"])
        self.assertFalse(any(e["t"] == "foreground_request" for e in evs))
        self.assertFalse(input_guard.has_grant("a-phys"))         # the grant was used once and is gone

    def test_strict_background_still_refuses_in_full_auto(self):
        model = Model([("", [("type_text", {"text": "hi"})]), ("Done.", [])])
        s = settings(approval_mode="auto", strict_background=True)
        judge = mock.Mock()
        with mock.patch.object(tools.REGISTRY["type_text"], "available", lambda: True), \
                mock.patch.object(autonomy, "judge", judge):
            evs = run_turn(model, s, text="type hi", chat_id="a-strict")
        judge.assert_not_called()
        self.assertIn("Strict Background Only", results(evs, "type_text")[0]["content"])


class RouterForm(unittest.TestCase):
    def test_form_asks_only_for_what_this_request_needs(self):
        base = router.schema(["read_file"])
        self.assertEqual(base["required"], ["intent", "complexity", "tools", "think", "vision", "review"])
        full = router.schema(["read_file"], ["skill"], plan=True, preference=True)
        self.assertEqual(full["required"][-3:], ["skills", "plan", "preference"])

    def test_greetings_skip_the_router(self):
        self.assertTrue(router.trivial([{"role": "user", "content": "Thanks!"}]))
        self.assertTrue(router.trivial([{"role": "user", "content": "good morning"}]))
        self.assertFalse(router.trivial([{"role": "user", "content": "thanks, now open notepad"}]))
        self.assertFalse(router.trivial([{"role": "user", "content": "hi", "attachments": [{"name": "a.png"}]}]))
        d, info = router.trivial_decision([{"role": "user", "content": "hi"}])
        self.assertEqual((d["complexity"], d["tools"], info["ms"]), ("trivial", [], 0))

    def test_plan_only_for_long_multi_step_requests(self):
        with mock.patch.object(router, "load_settings", lambda: {"router_plan": True}):
            self.assertTrue(router.wants_plan("Open Discord, then find my chat with Alex, and then summarize it"))
            self.assertFalse(router.wants_plan("Open VS Code."))
        with mock.patch.object(router, "load_settings", lambda: {"router_plan": False}):
            self.assertFalse(router.wants_plan("first do this; then that; finally the other"))

    def test_gpu_router_moves_to_cpu_in_remote_mode(self):
        with mock.patch.object(router, "load_settings", lambda: {"router_device": "gpu"}):
            router._state["remote"] = False
            self.assertTrue(router.on_gpu())
            router._state["remote"] = True
            self.assertFalse(router.on_gpu())
            router._state["remote"] = False
        a = router.server_args("m.gguf", 4, gpu=True)
        self.assertNotIn("none", a)


if __name__ == "__main__":
    unittest.main()
