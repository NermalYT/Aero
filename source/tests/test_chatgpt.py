"""Tests for the ChatGPT review pass (GPT-6 Astra reviews, GPT-6.1 Sol repairs) and its order with Claude's pass.
Run from source/:
    python -m unittest discover -s tests -v
Nothing here reaches the network: the OpenAI API is answered by an in-process mock transport, and the Codex CLI is a
small fake script that prints the JSONL events the real `codex exec --json` prints. Everything uses a throwaway
AERO_HOME."""
import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import openai  # noqa: E402

from aero import agent, chatgpt, cloud, config, pipeline, tools, vault  # noqa: E402

tools.load_all()
IS_WIN = os.name == "nt"


def collect(agen):
    async def run():
        return [ev async for ev in agen]
    return asyncio.run(run())


def make_turn(settings=None, history=None):
    s = config.load_settings()
    s.update(settings or {})
    hist = history or [{"role": "user", "content": "What does notes.txt say?"},
                       {"role": "assistant", "content": "It says hello.", "lane": "local"}]
    t = agent.Turn("chat-test", hist, s, {"url": "http://127.0.0.1:1", "ctx": 8192, "model": {"name": "local"}})
    t.start_index = 1
    return t


# ------------------------------------------------------------------------------------------------ fake Responses API

def _response(rid, model, output, status="completed", usage=(1000, 200, 300)):
    return {"id": rid, "object": "response", "created_at": 0, "model": model, "status": status, "output": output,
            "parallel_tool_calls": False, "tool_choice": "auto", "tools": [], "incomplete_details": None,
            "usage": {"input_tokens": usage[0], "input_tokens_details": {"cached_tokens": usage[1]},
                      "output_tokens": usage[2], "output_tokens_details": {"reasoning_tokens": 50},
                      "total_tokens": usage[0] + usage[2]}}


def _sse(events):
    out = []
    for i, e in enumerate(events):
        e = {**e, "sequence_number": i}
        out.append(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n")
    return "".join(out).encode()


def call_stream(rid, model, name, args, summary="Checking."):
    reasoning = {"type": "reasoning", "id": "rs_" + rid, "summary": [{"type": "summary_text", "text": summary}],
                 "encrypted_content": "ENC-" + rid}
    fc = {"type": "function_call", "id": "fc_" + rid, "call_id": "call_" + rid, "name": name,
          "arguments": json.dumps(args), "status": "completed"}
    return _sse([
        {"type": "response.created", "response": _response(rid, model, [], "in_progress")},
        {"type": "response.output_item.added", "output_index": 0, "item": {**reasoning, "summary": []}},
        {"type": "response.reasoning_summary_text.delta", "item_id": reasoning["id"], "output_index": 0,
         "summary_index": 0, "delta": summary},
        {"type": "response.reasoning_summary_part.done", "item_id": reasoning["id"], "output_index": 0,
         "summary_index": 0, "part": {"type": "summary_text", "text": summary}},
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
        {"type": "response.output_item.added", "output_index": 1, "item": {**fc, "arguments": "", "status": "in_progress"}},
        {"type": "response.function_call_arguments.delta", "item_id": fc["id"], "output_index": 1, "delta": fc["arguments"]},
        {"type": "response.output_item.done", "output_index": 1, "item": fc},
        {"type": "response.completed", "response": _response(rid, model, [reasoning, fc])},
    ])


def text_stream(rid, model, text):
    msg = {"type": "message", "id": "msg_" + rid, "role": "assistant", "status": "completed",
           "content": [{"type": "output_text", "text": text, "annotations": []}]}
    return _sse([
        {"type": "response.created", "response": _response(rid, model, [], "in_progress")},
        {"type": "response.output_item.added", "output_index": 0, "item": {**msg, "content": [], "status": "in_progress"}},
        {"type": "response.content_part.added", "item_id": msg["id"], "output_index": 0, "content_index": 0,
         "part": {"type": "output_text", "text": "", "annotations": []}},
        {"type": "response.output_text.delta", "item_id": msg["id"], "output_index": 0, "content_index": 0,
         "delta": text, "logprobs": []},
        {"type": "response.output_item.done", "output_index": 0, "item": msg},
        {"type": "response.completed", "response": _response(rid, model, [msg])},
    ])


class FakeOpenAI:
    """Serves queued replies to POST /v1/responses and records each request body."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def handler(self, request):
        body = json.loads(request.content.decode())
        self.requests.append(body)
        status, payload = self.replies.pop(0)
        if status != 200:
            return httpx.Response(status, json=payload)
        return httpx.Response(200, content=payload, headers={"content-type": "text/event-stream"})

    def install(self, key="sk-test-0000000000000000"):
        vault.put(chatgpt.KEY_NAME, key)
        chatgpt._clients.clear()
        chatgpt._clients[key] = openai.AsyncOpenAI(
            api_key=key, max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler), base_url="https://api.openai.com/v1"))


VERDICT_MAJOR = {"verdict": "major", "summary": "The answer was not checked against the file.",
                 "issues": [{"severity": "major", "where": "answer", "problem": "Guessed the content.",
                             "fix": "Read the file."}],
                 "plan": "1. Read notes.txt. 2. Quote it.", "improved_prompt": "Quote notes.txt exactly."}
VERDICT_OK = {"verdict": "ok", "summary": "Correct.", "issues": [], "plan": "", "improved_prompt": ""}


class ApiReviewTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="aero-work-"))
        (self.work / "notes.txt").write_text("hello from the notes file\n", encoding="utf-8")
        (config.DATA / "cloud_usage.json").unlink(missing_ok=True)     # each test starts with nothing spent today

    def tearDown(self):
        vault.put(chatgpt.KEY_NAME, "")
        chatgpt._clients.clear()
        chatgpt._no_summary.clear()

    def test_review_runs_read_tool_then_submits_verdict(self):
        fake = FakeOpenAI([
            (200, call_stream("r1", "gpt-6-astra", "read_file", {"path": str(self.work / "notes.txt")})),
            (200, call_stream("r2", "gpt-6-astra", "submit_review", VERDICT_MAJOR)),
        ])
        fake.install()
        turn = make_turn({"chatgpt_backend": "api", "work_dir": str(self.work)})
        events = collect(chatgpt.review(turn, 1, None))

        self.assertEqual(events[0]["t"], "lane")
        self.assertEqual(events[0]["lane"], "astra")
        self.assertEqual(events[0]["via"], "api")
        self.assertEqual(turn.cloud_result["verdict"], "major")
        self.assertEqual(len(fake.requests), 2)
        first = fake.requests[0]
        self.assertEqual(first["model"], "gpt-6-astra")
        self.assertIs(first["store"], False)
        self.assertIn("reasoning.encrypted_content", first["include"])
        self.assertEqual(first["reasoning"]["summary"], "auto")
        tool_names = {t["name"] for t in first["tools"]}
        self.assertIn("submit_review", tool_names)
        self.assertIn("read_file", tool_names)
        self.assertNotIn("write_file", tool_names)          # the reviewer only gets read-only tools
        self.assertNotIn("run_command", tool_names)
        # the second request carries the encrypted reasoning, the call and the real file content back
        second = fake.requests[1]["input"]
        kinds = [x.get("type") for x in second]
        self.assertIn("reasoning", kinds)
        self.assertIn("function_call", kinds)
        out = next(x for x in second if x.get("type") == "function_call_output")
        self.assertIn("hello from the notes file", out["output"])
        reasoning = next(x for x in second if x.get("type") == "reasoning")
        self.assertEqual(reasoning["encrypted_content"], "ENC-r1")
        # the tool ran in the astra lane and the reasoning summary streamed to the UI
        self.assertTrue(any(e["t"] == "tool_result" and e["message"]["lane"] == "astra" for e in events))
        self.assertTrue(any(e["t"] == "reasoning" and e["lane"] == "astra" for e in events))
        # usage was recorded under the model and priced
        usage = [e for e in events if e["t"] == "cloud_usage"]
        self.assertEqual(usage[0]["model"], "gpt-6-astra")
        self.assertAlmostEqual(usage[0]["usage"]["usd"], chatgpt.cost_of("gpt-6-astra", 1000, 300, 200))
        self.assertGreater(chatgpt.spent_today(), 0)

    def test_reviewer_cannot_write(self):
        target = self.work / "evil.txt"
        fake = FakeOpenAI([
            (200, call_stream("w1", "gpt-6-astra", "write_file", {"path": str(target), "content": "x"})),
            (200, call_stream("w2", "gpt-6-astra", "submit_review", VERDICT_OK)),
        ])
        fake.install()
        turn = make_turn({"chatgpt_backend": "api", "work_dir": str(self.work),
                          "tool_policy": {"files_write": "auto"}})
        collect(chatgpt.review(turn))
        self.assertFalse(target.exists())
        out = next(x for x in fake.requests[1]["input"] if x.get("type") == "function_call_output")
        self.assertIn("read-only", out["output"])
        self.assertEqual(turn.cloud_result["verdict"], "ok")

    def test_unverified_org_retries_without_reasoning_summary(self):
        err = {"error": {"message": "Your organization must be verified to generate reasoning summaries.",
                         "type": "invalid_request_error", "param": "reasoning.summary", "code": "unsupported_value"}}
        fake = FakeOpenAI([(400, err), (200, call_stream("s1", "gpt-6-astra", "submit_review", VERDICT_OK))])
        fake.install()
        turn = make_turn({"chatgpt_backend": "api", "work_dir": str(self.work)})
        collect(chatgpt.review(turn))
        self.assertEqual(len(fake.requests), 2)
        self.assertNotIn("summary", fake.requests[1]["reasoning"])
        self.assertEqual(turn.cloud_result["verdict"], "ok")

    def test_plain_text_verdict_is_nudged_into_submit_review(self):
        fake = FakeOpenAI([(200, text_stream("t1", "gpt-6-astra", "Looks fine to me.")),
                           (200, call_stream("t2", "gpt-6-astra", "submit_review", VERDICT_OK))])
        fake.install()
        turn = make_turn({"chatgpt_backend": "api", "work_dir": str(self.work)})
        collect(chatgpt.review(turn))
        self.assertEqual(turn.cloud_result["verdict"], "ok")
        self.assertEqual([t["name"] for t in fake.requests[1]["tools"]], ["submit_review"])

    def test_sol_repairs_with_write_tools_under_approval_rules(self):
        target = self.work / "fixed.txt"
        fake = FakeOpenAI([
            (200, call_stream("e1", "gpt-6.1-sol", "write_file", {"path": str(target), "content": "fixed"})),
            (200, text_stream("e2", "gpt-6.1-sol", "Done: wrote fixed.txt.")),
        ])
        fake.install()
        turn = make_turn({"chatgpt_backend": "api", "work_dir": str(self.work),
                          "tool_policy": {"files_write": "auto"}})
        events = collect(chatgpt.execute(turn, VERDICT_MAJOR))
        self.assertEqual(events[0]["lane"], "sol")
        self.assertEqual(fake.requests[0]["model"], "gpt-6.1-sol")
        self.assertIn("GPT-6 Astra's review", fake.requests[0]["input"][0]["content"][0]["text"])
        self.assertEqual(target.read_text(encoding="utf-8"), "fixed")
        final = [m for m in turn.history if m.get("lane") == "sol" and m.get("role") == "assistant"]
        self.assertEqual(final[-1]["content"], "Done: wrote fixed.txt.")
        self.assertEqual(final[-1]["model"], "GPT-6.1 Sol")

    def test_budget_cap_stops_before_spending(self):
        fake = FakeOpenAI([])
        fake.install()
        cloud.note_usage("gpt-6-astra", {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "usd": 5.0})
        turn = make_turn({"chatgpt_backend": "api", "openai_daily_budget_usd": 1.0})
        events = collect(chatgpt.review(turn))
        self.assertEqual(fake.requests, [])
        self.assertTrue(any(e["t"] == "notice" and "budget" in e["text"] for e in events))


class CostTests(unittest.TestCase):
    def test_prices(self):
        self.assertAlmostEqual(chatgpt.cost_of("gpt-6-astra", 100_000, 100_000), 6.0)
        self.assertAlmostEqual(chatgpt.cost_of("gpt-6.1-sol", 100_000, 100_000), 1.2)
        self.assertAlmostEqual(chatgpt.cost_of("gpt-6.1-sol", 100_000, 0, cached=100_000), 0.01)

    def test_long_prompt_multiplier(self):
        n = 300_000
        self.assertAlmostEqual(chatgpt.cost_of("gpt-6-astra", n, 1000), (n * 10 * 2 + 1000 * 50 * 1.5) / 1e6)


# ------------------------------------------------------------------------------------------------ fake Codex CLI

FAKE_CODEX = r'''
import json, os, sys
log = os.environ.get("FAKE_CODEX_LOG") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls.jsonl")
argv = sys.argv[1:]
if argv[:1] == ["--version"]:
    print("codex-cli 9.9.9"); sys.exit(0)
if argv[:2] == ["login", "status"]:
    mode = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mode.txt")).read().strip()
    if mode == "out":
        print("Not logged in"); sys.exit(1)
    if mode == "key":
        print("Logged in using an API key - sk-proj-***ABCD"); sys.exit(0)
    print("Logged in using ChatGPT"); sys.exit(0)
prompt = sys.stdin.read()
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv, "prompt": prompt, "env_key": "OPENAI_API_KEY" in os.environ}) + "\n")
def ev(o): print(json.dumps(o), flush=True)
ev({"type": "thread.started", "thread_id": "t1"})
ev({"type": "turn.started"})
ev({"type": "item.completed", "item": {"id": "i0", "type": "reasoning", "text": "**Checking the file**"}})
ev({"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "cat notes.txt",
    "aggregated_output": "", "status": "in_progress"}})
ev({"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "cat notes.txt",
    "aggregated_output": "hello from the notes file\n", "exit_code": 0, "status": "completed"}})
if "--output-schema" in argv:
    schema = json.load(open(argv[argv.index("--output-schema") + 1]))
    assert schema["required"][0] == "verdict"
    text = json.dumps({"verdict": "minor", "summary": "Close, one detail is off.",
        "issues": [{"severity": "minor", "where": "answer", "problem": "Missing quote.", "fix": "Quote it."}],
        "plan": "1. Quote the file.", "improved_prompt": ""})
else:
    if "workspace-write" in argv:
        ev({"type": "item.completed", "item": {"id": "i2", "type": "file_change", "status": "completed",
            "changes": [{"path": "notes.txt", "kind": "update"}]}})
    text = "Fixed it."
ev({"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": text}})
ev({"type": "turn.completed", "usage": {"input_tokens": 5000, "cached_input_tokens": 1000, "output_tokens": 400}})
'''


def make_fake_codex(folder: Path, mode="chatgpt"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "fake_codex.py").write_text(FAKE_CODEX, encoding="utf-8")
    (folder / "mode.txt").write_text(mode, encoding="utf-8")
    if IS_WIN:
        exe = folder / "codex.cmd"
        exe.write_text(f'@"{sys.executable}" "{folder / "fake_codex.py"}" %*\r\n', encoding="utf-8")
    else:
        exe = folder / "codex"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{folder / "fake_codex.py"}" "$@"\n', encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


class CodexPlanTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="aero-codex-test-"))
        self.exe = make_fake_codex(self.dir)
        self.work = Path(tempfile.mkdtemp(prefix="aero-work-"))
        self.base = {"chatgpt_backend": "plan", "codex_cli_path": str(self.exe), "work_dir": str(self.work),
                     "chatgpt_plan_signed_in": True}
        config.save_settings(self.base)
        os.environ["OPENAI_API_KEY"] = "sk-should-not-reach-codex"

    def tearDown(self):
        os.environ.pop("OPENAI_API_KEY", None)
        config.save_settings({"chatgpt_backend": "auto", "codex_cli_path": "", "chatgpt_plan_signed_in": False,
                              "strict_offline": False})

    def calls(self):
        p = self.dir / "calls.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def test_status_keeps_only_the_method(self):
        st = chatgpt.status()
        self.assertTrue(st["installed"])
        self.assertTrue(st["loggedIn"])
        self.assertEqual(st["method"], "chatgpt")
        self.assertEqual(st["version"], "9.9.9")
        (self.dir / "mode.txt").write_text("key")
        st = chatgpt.status()
        self.assertEqual(st["method"], "api_key")
        self.assertNotIn("ABCD", json.dumps(st))             # the masked key is never passed on
        (self.dir / "mode.txt").write_text("out")
        self.assertFalse(chatgpt.status()["loggedIn"])

    def test_status_does_not_run_codex_in_strict_offline(self):
        config.save_settings({"strict_offline": True})
        st = chatgpt.status()
        self.assertTrue(st.get("offline"))
        self.assertNotIn("version", st)

    def test_backend_auto_prefers_api_key_then_plan(self):
        s = config.load_settings()
        self.assertEqual(chatgpt.backend({**s, "chatgpt_backend": "auto"}), "plan")
        self.assertIsNone(chatgpt.backend({**s, "chatgpt_backend": "auto", "chatgpt_plan_signed_in": False}))
        vault.put(chatgpt.KEY_NAME, "sk-x")
        try:
            self.assertEqual(chatgpt.backend({**s, "chatgpt_backend": "auto"}), "api")
            self.assertEqual(chatgpt.backend({**s, "chatgpt_backend": "plan"}), "plan")
        finally:
            vault.put(chatgpt.KEY_NAME, "")
        self.assertIsNone(chatgpt.backend({**s, "chatgpt_backend": "api"}))

    def test_plan_review_is_read_only_and_parses_verdict(self):
        turn = make_turn(self.base)
        events = collect(chatgpt.review(turn))
        self.assertEqual(events[0]["via"], "plan")
        self.assertEqual(turn.cloud_result["verdict"], "minor")
        self.assertEqual(turn.cloud_result["issues"][0]["fix"], "Quote it.")
        call = self.calls()[-1]
        argv = call["argv"]
        self.assertEqual(argv[:2], ["exec", "--json"])
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-astra")
        self.assertIn("--ephemeral", argv)
        self.assertEqual(argv[-1], "-")                     # the prompt goes through stdin, not the command line
        self.assertIn("What does notes.txt say?", call["prompt"])
        self.assertIn("GPT-6 Astra", call["prompt"])
        self.assertFalse(call["env_key"])                   # an API key in the environment would replace the sign-in
        tools_seen = [e for e in events if e["t"] == "tool_result"]
        self.assertEqual(tools_seen[0]["message"]["name"], "Codex: command")
        self.assertIn("hello from the notes file", tools_seen[0]["message"]["content"])
        # the JSON verdict is shown as a review card, not as chat text
        self.assertFalse(any(e["t"] == "content" for e in events))
        usage = [e for e in events if e["t"] == "cloud_usage"][0]
        self.assertEqual(usage["via"], "plan")
        self.assertEqual(usage["usage"]["usd"], 0.0)

    def test_plan_repair_asks_before_workspace_write(self):
        turn = make_turn(self.base)

        async def run(decision):
            evs = []
            async for ev in chatgpt.execute(turn, VERDICT_MAJOR):
                evs.append(ev)
                if ev["t"] == "tool_start" and ev.get("needs_approval"):
                    agent._approvals.pop(ev["call_id"]).set_result(decision)
            return evs

        events = asyncio.run(run("allow"))
        ask = next(e for e in events if e["t"] == "tool_start" and e["name"] == "codex_workspace")
        self.assertIn(str(self.work), ask["label"])
        argv = self.calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.1-sol")
        self.assertTrue(any(e["t"] == "tool_result" and e["message"]["name"] == "Codex: file change" for e in events))
        final = [m for m in turn.history if m.get("lane") == "sol" and m.get("role") == "assistant" and m.get("content")]
        self.assertEqual(final[-1]["content"], "Fixed it.")

        turn2 = make_turn(self.base)
        turn = turn2
        events = asyncio.run(run("deny"))
        argv = self.calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn("read-only", self.calls()[-1]["prompt"])

    def test_plan_repair_read_only_when_writing_is_off(self):
        turn = make_turn({**self.base, "tool_policy": {"files_write": "off"}})
        events = collect(chatgpt.execute(turn, VERDICT_MAJOR))
        self.assertFalse(any(e["t"] == "tool_start" and e.get("needs_approval") for e in events))
        argv = self.calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")

    def test_unsafe_model_name_is_not_passed_to_codex(self):
        turn = make_turn({**self.base, "gpt_review_model": "x --dangerously-bypass"})
        collect(chatgpt.review(turn))
        argv = self.calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-astra")


# ------------------------------------------------------------------------------------------------ pass order

class PassOrderTests(unittest.TestCase):
    """Both review buttons on: ChatGPT's pass runs first, Claude's second with ChatGPT's verdicts in its packet, and
    the local model gets one lessons step covering both."""

    def setUp(self):
        self.saved = {}
        self.log = []
        self.lessons_args = None
        self.claude_packet = None

        def patch(mod, name, fn):
            self.saved[(mod, name)] = getattr(mod, name)
            setattr(mod, name, fn)

        log = self.log
        test = self

        async def gpt_review(turn, round_no=1, previous=None):
            log.append(("astra", round_no))
            yield {"t": "lane", "lane": "astra"}
            turn.cloud_result = dict(VERDICT_MAJOR)

        async def gpt_execute(turn, rv):
            log.append(("sol", rv["verdict"]))
            yield {"t": "lane", "lane": "sol"}
            turn.history.append({"role": "assistant", "lane": "sol", "content": "Sol's repaired answer."})

        async def claude_review(turn, round_no=1, previous=None):
            log.append(("fable", round_no))
            test.claude_packet = cloud.review_packet(turn, round_no, previous)[0]["text"]
            yield {"t": "lane", "lane": "fable"}
            turn.cloud_result = dict(VERDICT_OK)

        async def claude_execute(turn, rv):
            log.append(("opus", rv["verdict"]))
            yield {"t": "lane", "lane": "opus"}

        async def make_lessons(turn, reviews, takeovers=()):
            test.lessons_args = (list(reviews), list(takeovers))
            return [{"id": "l1", "text": "Read the file before answering."}]

        patch(chatgpt, "review", gpt_review)
        patch(chatgpt, "execute", gpt_execute)
        patch(chatgpt, "backend", lambda s: "api")
        patch(chatgpt, "budget_ok", lambda s: True)
        patch(cloud, "review", claude_review)
        patch(cloud, "execute", claude_execute)
        patch(cloud, "backend", lambda s: "api")
        patch(cloud, "budget_ok", lambda s: True)
        patch(pipeline, "make_lessons", make_lessons)

    def tearDown(self):
        for (mod, name), fn in self.saved.items():
            setattr(mod, name, fn)

    def test_chatgpt_first_then_claude_with_full_review(self):
        turn = make_turn()
        turn.gpt_review = turn.review = True
        events = collect(pipeline.review_passes(turn))
        self.assertEqual(self.log, [("astra", 1), ("sol", "major"), ("fable", 1)])
        # Claude saw Astra's verdict and Sol's repaired work
        self.assertIn("A first review pass by ChatGPT", self.claude_packet)
        self.assertIn("The answer was not checked against the file.", self.claude_packet)
        self.assertIn("Sol's repaired answer.", self.claude_packet)
        # one lessons step with the reviews that found problems and Sol's takeover
        reviews, takeovers = self.lessons_args
        self.assertEqual([r["pass"] for r in reviews], ["chatgpt"])
        self.assertEqual(reviews[0]["reviewer"], "GPT-6 Astra")
        self.assertEqual(takeovers, [("GPT-6.1 Sol", "Sol's repaired answer.")])
        cards = [e["message"] for e in events if e["t"] == "review"]
        self.assertEqual([(c["lane"], c["verdict"]) for c in cards], [("astra", "major"), ("fable", "ok")])
        self.assertEqual(cards[0]["fixer"], "GPT-6.1 Sol")
        self.assertTrue(any(e["t"] == "lesson" for e in events))

    def test_only_chatgpt(self):
        turn = make_turn()
        turn.gpt_review, turn.review = True, False
        collect(pipeline.review_passes(turn))
        self.assertEqual([x[0] for x in self.log], ["astra", "sol"])

    def test_only_claude_has_no_chatgpt_section(self):
        turn = make_turn()
        turn.gpt_review, turn.review = False, True
        collect(pipeline.review_passes(turn))
        self.assertEqual([x[0] for x in self.log], ["fable"])
        self.assertNotIn("first review pass by ChatGPT", self.claude_packet)
        self.assertIsNone(self.lessons_args)                 # nothing found, nothing to learn

    def test_minor_verdict_goes_back_to_local_model(self):
        async def gpt_review(turn, round_no=1, previous=None):
            self.log.append(("astra", round_no))
            yield {"t": "lane", "lane": "astra"}
            turn.cloud_result = {**VERDICT_MAJOR, "verdict": "minor"} if round_no == 1 else dict(VERDICT_OK)

        async def run_local(turn):
            self.log.append(("local", None))
            yield {"t": "lane", "lane": "local"}

        chatgpt.review = gpt_review
        saved_local = agent.run_local
        agent.run_local = run_local
        try:
            turn = make_turn()
            turn.gpt_review, turn.review = True, False
            events = collect(pipeline.review_passes(turn))
        finally:
            agent.run_local = saved_local
        self.assertEqual(self.log, [("astra", 1), ("local", None), ("astra", 2)])
        fix = next(e["message"] for e in events if e["t"] == "fix_request")
        self.assertEqual(fix["from"], "astra")
        self.assertIn("GPT-6 Astra's review of your work", fix["content"])
        # the local model learns from the minor review even though the second round passed
        self.assertEqual(self.lessons_args[0][0]["verdict"], "minor")

    def test_strict_offline_skips_both_passes(self):
        turn = make_turn({"strict_offline": True})
        turn.gpt_review = turn.review = True
        events = collect(pipeline.review_passes(turn))
        self.assertEqual(self.log, [])
        notes = [e["text"] for e in events if e["t"] == "notice"]
        self.assertEqual(len(notes), 2)
        self.assertTrue(all("Strict offline" in n for n in notes))

    def test_chatgpt_failure_still_runs_claude(self):
        async def broken(turn, round_no=1, previous=None):
            raise RuntimeError("boom")
            yield  # pragma: no cover

        chatgpt.review = broken
        turn = make_turn()
        turn.gpt_review = turn.review = True
        events = collect(pipeline.review_passes(turn))
        self.assertIn(("fable", 1), self.log)
        self.assertTrue(any(e["t"] == "notice" and "ChatGPT review failed" in e["text"] for e in events))


if __name__ == "__main__":
    unittest.main()
