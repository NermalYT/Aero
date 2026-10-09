"""v1.1 agent loop tests against a scripted model (no real model, no real mouse or keyboard): Strict Background Only,
the foreground-control grant and its exclusive input lock, Stop releasing everything, verify-before-repeat after a
timeout, read-only calls running side by side, questions that don't block other work, one agent per window, evidence
notes, and app names in the turn context. Run from source/:
    python -m unittest discover -s tests -v
"""
import asyncio
import json
import os
import re
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

import httpx  # noqa: E402

import fake_llama  # noqa: E402
from aero import (action_results, agent, agents, app_registry, clarifications, config, input_guard, pipeline,  # noqa: E402
                  resources, tools)
from aero.tools import Tool  # noqa: E402

tools.load_all()
REAL_CLIENT = httpx.AsyncClient


class Model:
    """Plays a list of steps: each is (content, [(tool name, args or callable(messages) -> args)])."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.bodies = []

    async def handle(self, request):
        body = json.loads(request.content or b"{}")
        if not body.get("stream"):
            return httpx.Response(200, json=fake_llama.title_reply(body))
        self.bodies.append(body)
        content, calls = self.steps.pop(0) if self.steps else ("Done.", [])
        out = []

        def emit(delta, finish=None, **extra):
            out.append("data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                                              **extra}) + "\n\n")
        if content:
            emit({"content": content})
        for i, (name, args) in enumerate(calls):
            a = args(body["messages"]) if callable(args) else args
            emit({"tool_calls": [{"index": i, "id": f"call_{name}_{len(self.bodies)}_{i}", "type": "function",
                                  "function": {"name": name, "arguments": json.dumps(a)}}]})
        emit({}, "tool_calls" if calls else "stop", usage={"prompt_tokens": 100, "completion_tokens": 10},
             timings={"predicted_per_second": 50.0, "prompt_per_second": 900.0, "prompt_n": 100, "predicted_n": 10})
        out.append("data: [DONE]\n\n")
        return httpx.Response(200, content="".join(out).encode(), headers={"content-type": "text/event-stream"})


def settings(**kw):
    s = config.load_settings()
    s.update({"router_enabled": False, "memory_enabled": False, "profile_enabled": False, "shared_learning": False,
              "local_only": False, "strict_offline": False, "strict_background": False})
    pol = dict(s["tool_policy"])
    pol.update(kw.pop("tool_policy", {}))
    s.update(kw)
    s["tool_policy"] = pol
    return s


def run_turn(model, s, text="do it", chat_id="v11-chat", on_event=None, run_patch=None):
    """Run one turn; on_event(ev) may react (approve, answer, stop). Returns all events."""
    def client(*a, **k):
        return REAL_CLIENT(transport=httpx.MockTransport(model.handle), timeout=30)
    hist = [{"role": "user", "content": text, "ts": time.time()}]
    events = []

    async def go():
        async for chunk in pipeline.run_turn(chat_id, hist, s, {"url": "http://fake", "ctx": 32768,
                                                                "model": {"name": "Qwen3.8-27B"}}):
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    events.append(ev)
                    if on_event:
                        r = on_event(ev)
                        if asyncio.iscoroutine(r):
                            await r
    with mock.patch.object(agent.httpx, "AsyncClient", client):
        asyncio.run(go())
    return events


def results(events, name=None):
    return [e["message"] for e in events if e["t"] == "tool_result" and (name is None or e["message"]["name"] == name)]


class Physical(unittest.TestCase):
    def setUp(self):
        agents.LIVE.clear()
        self.ran = []
        real = tools.run

        def run(name, args, ctx):
            self.ran.append((name, getattr(ctx, "physical_ok", False), resources.holder(resources.EXCLUSIVE_INPUT)))
            if name in ("type_text", "mouse_click", "press_keys"):
                return {"text": f"did {name}", "error": False}
            return real(name, args, ctx)
        self.ps = [mock.patch.object(agent.tools, "run", run),
                   mock.patch.object(input_guard, "idle_ms", lambda: 10 ** 6),
                   mock.patch.object(input_guard, "restore_foreground", lambda h: True),
                   mock.patch.object(agent.control, "banner_on", lambda *a, **k: None),
                   mock.patch.object(agent.control, "banner_off", lambda *a, **k: None)]
        for p in self.ps:
            p.start()

    def tearDown(self):
        for p in self.ps:
            p.stop()
        resources.release_owner("v11-chat")
        input_guard.clear("v11-chat")

    def test_strict_background_refuses_real_input(self):
        m = Model([("", [("mouse_click", {"x": 5, "y": 5})]), ("I could not click.", [])])
        evs = run_turn(m, settings(strict_background=True, tool_policy={"desktop": "auto"}))
        r = results(evs, "mouse_click")[0]
        self.assertTrue(r["error"])
        self.assertIn("Strict Background Only", r["content"])
        self.assertFalse([x for x in self.ran if x[0] == "mouse_click"])

    def test_foreground_grant_lock_and_release(self):
        m = Model([("", [("type_text", {"text": "hello"})]), ("Typed it.", [])])
        seen = {}

        def on(ev):
            if ev["t"] == "foreground_request":
                seen["req"] = ev
                agent.resolve_approval(ev["call_id"], "allow", "v11-chat")
        evs = run_turn(m, settings(tool_policy={"desktop": "ask"}), on_event=on)
        self.assertEqual(seen["req"]["name"], "type_text")
        ran = [x for x in self.ran if x[0] == "type_text"]
        self.assertEqual(len(ran), 1)
        self.assertTrue(ran[0][1])                                    # ran with foreground control granted
        self.assertEqual(ran[0][2]["owner"], "v11-chat")              # holding the exclusive input lock
        self.assertIsNone(resources.holder(resources.EXCLUSIVE_INPUT))  # and released afterwards
        self.assertFalse(input_guard.has_grant("v11-chat"))            # a one-time grant is used up
        ctl = next(e for e in evs if e["t"] == "control")
        self.assertEqual(ctl["mode"], "FOREGROUND_CONSENT_REQUIRED")
        self.assertIn("with your mouse and keyboard", ctl["text"])

    def test_deny_and_auto(self):
        m = Model([("", [("press_keys", {"keys": "enter"})]), ("ok", [])])
        run_turn(m, settings(tool_policy={"desktop": "ask"}),
                 on_event=lambda ev: ev["t"] == "foreground_request" and agent.resolve_approval(ev["call_id"], "deny"))
        self.assertFalse([x for x in self.ran if x[0] == "press_keys"])
        m = Model([("", [("press_keys", {"keys": "enter"})]), ("ok", [])])
        evs = run_turn(m, settings(tool_policy={"desktop": "auto"}))       # a standing grant from Settings
        self.assertFalse([e for e in evs if e["t"] == "foreground_request"])
        self.assertTrue([x for x in self.ran if x[0] == "press_keys" and x[1]])

    def test_user_still_typing_is_not_interrupted(self):
        m = Model([("", [("type_text", {"text": "x"})]), ("ok", [])])
        with mock.patch.object(input_guard, "idle_ms", lambda: 0), mock.patch.object(input_guard, "IDLE_WAIT_S", 0.6):
            evs = run_turn(m, settings(tool_policy={"desktop": "auto"}))
        self.assertIn("kept using the mouse and keyboard", results(evs, "type_text")[0]["content"])
        self.assertFalse([x for x in self.ran if x[0] == "type_text"])

    def test_stop_while_asking_releases_everything(self):
        m = Model([("", [("list_dir", {"path": "."}), ("type_text", {"text": "x"})]), ("never", [])])

        def on(ev):
            if ev["t"] == "foreground_request":
                agent.stop("v11-chat")
        evs = run_turn(m, settings(tool_policy={"desktop": "ask", "files_read": "auto"}), on_event=on)
        self.assertFalse([x for x in self.ran if x[0] == "type_text"])
        self.assertEqual(resources.held_by("v11-chat"), [])
        note = next(e for e in evs if e["t"] == "notice" and e["text"].startswith("Stopped."))
        self.assertIn("Not finished", note["text"])
        self.assertEqual(evs[-1]["t"], "done")


class Concurrency(unittest.TestCase):
    def setUp(self):
        agents.LIVE.clear()

    def test_read_only_calls_run_side_by_side_in_order(self):
        starts = {}

        def run(name, args, ctx):
            starts[args["path"]] = time.monotonic()
            time.sleep(0.4)
            return {"text": f"listed {args['path']}", "error": False}
        m = Model([("", [("list_dir", {"path": "a"}), ("list_dir", {"path": "b"}), ("list_dir", {"path": "c"})]),
                   ("ok", [])])
        with mock.patch.object(agent.tools, "run", run):
            t0 = time.monotonic()
            run_turn(m, settings(tool_policy={"files_read": "auto"}))
            took = time.monotonic() - t0
        self.assertLess(max(starts.values()) - min(starts.values()), 0.3)
        self.assertLess(took, 1.1)                                     # not 3 x 0.4 s one after another
        tool_msgs = [x for x in m.bodies[-1]["messages"] if x["role"] == "tool"]
        self.assertEqual([x["content"].split()[1] for x in tool_msgs], ["a", "b", "c"])

    def test_one_agent_per_window(self):
        resources.acquire(["window:4242"], "someone-else")
        agents.LIVE["someone-else"] = {"name": "Spreadsheet Fixer", "status": "working", "updated": time.time()}
        ran = []
        m = Model([("", [("app_click", {"element": 3})]), ("ok", [])])
        app_click = tools.REGISTRY["app_click"]
        with mock.patch.object(app_click, "available", lambda: True), \
                mock.patch("aero.tools.apps.selected_hwnd", lambda owner: 4242), \
                mock.patch.object(agent.tools, "run", lambda n, a, c: ran.append(n) or {"text": "x"}):
            evs = run_turn(m, settings(tool_policy={"desktop": "auto"}))
        r = results(evs, "app_click")[0]
        self.assertTrue(r["error"])
        self.assertIn("Spreadsheet Fixer is using that window", r["content"])
        self.assertEqual(ran, [])
        resources.release_owner("someone-else")


class RepeatGuard(unittest.TestCase):
    def setUp(self):
        agents.LIVE.clear()
        self.sent = []

        def send(ctx, to=""):
            self.sent.append(to)
            if len(self.sent) == 1:
                raise TimeoutError("the server did not answer in 30 s")
            return "sent"
        tools.REGISTRY["mcp_mail_send_message"] = Tool("mcp_mail_send_message", "Send an email.",
                                                       {"to": {"type": "string"}}, "mcp", send)

    def tearDown(self):
        tools.REGISTRY.pop("mcp_mail_send_message", None)

    def test_timeout_needs_a_look_before_repeating(self):
        call = ("mcp_mail_send_message", {"to": "pat@example.com"})
        m = Model([("", [call]), ("", [call]), ("", [("list_dir", {"path": "."})]), ("", [call]), ("Sent.", [])])
        evs = run_turn(m, settings(tool_policy={"mcp": "auto", "files_read": "auto"}), chat_id="v11-guard")
        rs = results(evs, "mcp_mail_send_message")
        self.assertIn("TimeoutError", rs[0]["content"])
        self.assertIn("result is unknown", rs[1]["content"])           # refused without running
        self.assertEqual(rs[2]["content"].split("\n")[0], "sent")      # after looking, it may run
        self.assertEqual(self.sent, ["pat@example.com", "pat@example.com"])


class Questions(unittest.TestCase):
    def setUp(self):
        agents.LIVE.clear()
        clarifications.reset_cache()

    def test_question_does_not_block_other_work(self):
        def qid(msgs):
            for x in reversed(msgs):
                mm = re.search(r"question id (q_[0-9a-f]+)", str(x.get("content") or ""))
                if mm:
                    return {"id": mm.group(1)}
            return {"id": "?"}
        m = Model([("", [("ask_user", {"question": "Which Gmail account should I check?",
                                       "choices": ["me@home.example", "me@work.example"]})]),
                   ("", [("list_dir", {"path": "."})]),
                   ("", [("get_answer", qid)]),
                   ("Checking me@work.example.", [])])
        order = []

        def on(ev):
            order.append(ev["t"] + ":" + ev.get("name", ev.get("message", {}).get("name", "") if isinstance(ev.get("message"), dict) else ""))
            if ev["t"] == "waiting":
                clarifications.answer(ev["question_id"], "me@work.example")
        evs = run_turn(m, settings(tool_policy={"files_read": "auto"}), on_event=on, chat_id="v11-q")
        q = next(e for e in evs if e["t"] == "question")["question"]
        self.assertEqual(q["choices"], ["me@home.example", "me@work.example"])
        i_q = next(i for i, e in enumerate(evs) if e["t"] == "question")
        i_list = next(i for i, e in enumerate(evs) if e["t"] == "tool_result" and e["message"]["name"] == "list_dir")
        i_wait = next(i for i, e in enumerate(evs) if e["t"] == "waiting")
        self.assertLess(i_q, i_list)
        self.assertLess(i_list, i_wait)                                  # other work ran before waiting
        self.assertIn("me@work.example", results(evs, "get_answer")[0]["content"])
        self.assertEqual(clarifications.get(q["id"])["status"], "answered")


class Evidence(unittest.TestCase):
    def test_unverified_and_failed_notes(self):
        agents.LIVE.clear()
        env = action_results.make("window_message_background", verified=False,
                                  method="the field's text read back", mode="WINDOW_MESSAGE_BACKGROUND")

        def run(name, args, ctx):
            return action_results.finish(action_results.attach({"text": "Sent 5 chars."}, dict(env)), name, 3,
                                         "local_app_write")
        m = Model([("", [("app_type", {"text": "hello"})]), ("It did not work.", [])])
        app_type = tools.REGISTRY["app_type"]
        with mock.patch.object(app_type, "available", lambda: True), mock.patch.object(agent.tools, "run", run), \
                mock.patch("aero.tools.apps.selected_hwnd", lambda owner: None), \
                mock.patch.object(agent.control, "banner_on", lambda *a, **k: None), \
                mock.patch.object(agent.control, "banner_off", lambda *a, **k: None):
            evs = run_turn(m, settings(tool_policy={"desktop": "auto"}), chat_id="v11-ev")
        r = results(evs, "app_type")[0]
        self.assertTrue(r["content"].endswith("[FAILED verification: the field's text read back · window messages]"))
        self.assertIs(r["verified"], False)
        self.assertTrue(r["error"])


class Context(unittest.TestCase):
    def test_app_mentions_reach_the_model(self):
        agents.LIVE.clear()
        fake = [{"id": "bloxstrap", "name": "Bloxstrap", "confidence": 0.95, "reason": "exact name", "installed": True,
                 "running": False, "catalog": "bloxstrap", "handler": None, "related": ["roblox"], "launch": ["exe"],
                 "phrase": "bloxstrap"}]
        m = Model([("Opening it.", [])])
        with mock.patch.object(app_registry, "mentions", lambda text, limit=4: fake):
            run_turn(m, settings(), text="Open up my Bloxstrap and find me a fun game", chat_id="v11-ctx")
        last_user = [x for x in m.bodies[0]["messages"] if x["role"] == "user"][-1]["content"]
        self.assertIn("Apps: Bloxstrap (installed", last_user)
        self.assertIn('app_launch "bloxstrap"', last_user)

    def test_local_only_hides_network_tools_but_not_app_or_question_tools(self):
        names = {x["function"]["name"] for x in tools.schemas(settings(local_only=True))}
        for hidden in ("browser_read_sections", "browser_extract", "browser_tabs", "fetch_url"):
            self.assertNotIn(hidden, names)
        for shown in ("app_find", "ask_user", "get_answer", "meeting_doc"):
            self.assertIn(shown, names)


if __name__ == "__main__":
    unittest.main()
