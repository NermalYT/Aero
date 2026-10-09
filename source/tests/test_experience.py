"""Tests for shared learning (aero/experience.py): run records from tool results, notes that every model gets
before a similar task, preferences going into the profile, and the whole flow through pipeline.run_turn with the
scripted fake model (whose Notepad call fails here, which is what triggers the notes).
Run from source/:
    python -m unittest discover -s tests -v
"""
import asyncio
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import fake_llama  # noqa: E402
from aero import agent, config, experience, memory, pipeline, tools  # noqa: E402

tools.load_all()


def sse_events(chunks):
    out = []
    for c in chunks:
        for line in c.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


class FakeTurn:
    def __init__(self, history, model="Qwen3.8-27B"):
        self.history, self.start_index, self.chat_id, self.model_name = history, 0, "c1", model
        self.engine = {"url": "http://fake"}


def call(cid, name, args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


HISTORY = [
    {"role": "user", "content": "Rename the photos on my Desktop to .jpg"},
    {"role": "assistant", "content": "", "tool_calls": [call("a", "list_dir", {"path": "~/Desktop"})],
     "stats": {"tg": 60.0}},
    {"role": "tool", "tool_call_id": "a", "name": "list_dir", "content": "beach.jpeg\ndog.jpeg"},
    {"role": "assistant", "content": "", "tool_calls": [call("b", "open_app", {"target": "notepad"}),
                                                       call("c", "run_command", {"command": "rm -rf ~/Desktop"})],
     "stats": {"tg": 50.0}},
    {"role": "tool", "tool_call_id": "b", "name": "open_app", "content": "RuntimeError: No program called 'notepad'",
     "error": True},
    {"role": "tool", "tool_call_id": "c", "name": "run_command", "content": "The user said no.", "error": True,
     "denied": True},
    {"role": "assistant", "content": "Renamed 2 photos."},
]


class SharedLearning(unittest.TestCase):
    def setUp(self):
        experience.clear()

    def tearDown(self):
        experience.clear()

    def test_run_record_from_tool_results(self):
        run = experience.record_run(FakeTurn(HISTORY), "Photo Renamer", "done", HISTORY[0]["content"])
        self.assertEqual([t["name"] for t in run["tools"]], ["list_dir", "open_app", "run_command"])
        self.assertEqual([t["ok"] for t in run["tools"]], [True, False, False])
        self.assertTrue(run["tools"][2]["denied"])
        self.assertIn("notepad", run["tools"][1]["err"])
        self.assertEqual((run["model"], run["agent"], run["tok_s"]), ("Qwen3.8-27B", "Photo Renamer", 55.0))
        self.assertEqual(len(experience.all_items()["runs"]), 1)
        # no tool calls: nothing to record
        self.assertIsNone(experience.record_run(FakeTurn([{"role": "user", "content": "hi"},
                                                          {"role": "assistant", "content": "Hello"}]), "", "done", "hi"))

    def test_when_a_model_call_is_spent(self):
        run = experience.record_run(FakeTurn(HISTORY), "Photo Renamer", "done", "x")
        self.assertTrue(experience.wants_reflection(run, "Rename the photos"))               # a tool failed
        clean = {"tools": [{"name": "list_dir", "ok": True}]}
        self.assertFalse(experience.wants_reflection(clean, "Rename the photos"))
        self.assertTrue(experience.wants_reflection(clean, "Don't use Notepad, I prefer gedit"))  # feedback
        self.assertTrue(experience.wants_reflection(None, "From now on keep answers short"))

    def test_notes_dedupe_and_flip_sides(self):
        experience.add_notes(["Use list_dir before renaming"], ["open_app notepad fails on Linux"], "Qwen", "Photo Renamer", "t")
        experience.add_notes(["use list_dir before renaming!"], [], "Gemma", "Photo Renamer", "t")
        notes = experience.all_items()["notes"]
        self.assertEqual(len(notes), 2)
        n = next(x for x in notes if x["side"] == "worked")
        self.assertEqual((n["hits"], n["model"]), (2, "Gemma"))       # counted, and credited to the latest model
        experience.add_notes(["open_app notepad fails on Linux"], [], "Gemma", "Photo Renamer", "t")
        sides = {x["text"]: x["side"] for x in experience.all_items()["notes"]}
        self.assertEqual(sides["open_app notepad fails on Linux"], "worked")   # only its newest side is kept
        self.assertEqual(len(sides), 2)

    def test_block_for_similar_tasks_only(self):
        experience.add_notes(["Write the file list to a text file instead of opening an editor"],
                             ["open_app 'notepad' doesn't exist on Linux"], "Qwen3.8-27B", "Photo Renamer",
                             "Rename the photos on my Desktop to .jpg")
        for _ in range(3):
            experience.record_run(FakeTurn(HISTORY), "Photo Renamer", "done", HISTORY[0]["content"])
        b = experience.block("Rename my Desktop photos to png", "Photo Renamer")
        self.assertIn("shared by every model", b)
        self.assertIn("Write the file list to a text file", b)
        self.assertIn("(Qwen3.8-27B, Photo Renamer)", b)                    # who learned it
        self.assertIn("open_app failed 3 of 3 times (last error: RuntimeError: No program called 'notepad')", b)
        self.assertIn("run_command failed 3 of 3 times (the user refused it 3 times)", b)
        self.assertNotIn("list_dir failed", b)
        self.assertEqual(experience.block("Write a haiku about autumn", "Poet"), "")      # unrelated: nothing

    def test_reflect_saves_notes_and_preferences(self):
        async def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body["response_format"]["json_schema"]["name"], "experience")
            self.assertIn("REFUSED BY THE USER", body["messages"][0]["content"])
            return httpx.Response(200, json=fake_llama.title_reply(body))
        real = httpx.AsyncClient
        client = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)  # noqa: E731
        with mock.patch.object(experience.httpx, "AsyncClient", client):
            got = asyncio.run(experience.reflect(FakeTurn(HISTORY), HISTORY[0]["content"], "Photo Renamer"))
        self.assertEqual(got["worked"], fake_llama.EXPERIENCE["worked"])
        self.assertEqual(got["failed"], fake_llama.EXPERIENCE["failed"])
        pref = fake_llama.EXPERIENCE["preferences"][0]
        facts = [f for f in memory.all_items()["facts"] if f["text"] == pref]
        try:
            self.assertEqual([f["kind"] for f in facts], ["preference"])          # in every model's profile now
            self.assertIn(pref, memory.profile_block("")[0])
        finally:
            for f in facts:
                memory.delete_fact(f["id"])

    def test_overview_per_model(self):
        experience.record_run(FakeTurn(HISTORY, "Qwen"), "Photo Renamer", "done", "a")
        experience.record_run(FakeTurn(HISTORY[:3], "Gemma"), "Photo Renamer", "done", "b")
        o = experience.overview()
        by = {m["model"]: m for m in o["models"]}
        self.assertEqual((by["Qwen"]["calls"], by["Qwen"]["failed"], by["Qwen"]["refused"]), (3, 1, 1))
        self.assertEqual((by["Gemma"]["calls"], by["Gemma"]["failed"]), (1, 0))
        self.assertEqual(o["runs"], 2)


class SharedLearningInATurn(unittest.TestCase):
    """A whole turn with the scripted model: its Notepad call fails, the model writes notes, and the next turn on a
    similar task (any model) starts with them."""

    def setUp(self):
        experience.clear()

    def tearDown(self):
        experience.clear()
        for f in memory.all_items()["facts"]:
            if f["text"] in fake_llama.EXPERIENCE["preferences"]:
                memory.delete_fact(f["id"])

    def _turn(self, chat_id, text, model, seen, settings):
        handler = fake_llama.mock_handler()

        async def spy(request):
            seen.append(json.loads(request.content or b"{}"))
            return await handler(request)
        real = httpx.AsyncClient
        client = lambda **kw: real(transport=httpx.MockTransport(spy), **kw)  # noqa: E731
        hist = [{"role": "user", "content": text}]

        async def go():
            return [c async for c in pipeline.run_turn(chat_id, hist, settings, {"url": "http://fake", "ctx": 32768,
                                                       "model": {"name": model}})]
        with mock.patch.object(agent.httpx, "AsyncClient", client), \
                mock.patch.object(experience.httpx, "AsyncClient", client):
            return sse_events(asyncio.run(go()))

    def test_one_models_mistake_reaches_the_next_model(self):
        s = config.load_settings()
        s.update({"router_enabled": False, "memory_enabled": False, "profile_enabled": False,
                  "tool_policy": {**s["tool_policy"], "agents": "off", "desktop": "off"}})
        seen = []
        evs = self._turn("exp-1", "Find the photos on my Desktop and list them", "Qwen3.8-27B", seen, s)
        learned = next(e for e in evs if e["t"] == "learned")["message"]
        self.assertEqual(learned["failed"], fake_llama.EXPERIENCE["failed"])
        self.assertEqual(learned["model"], "Qwen3.8-27B")
        self.assertTrue(any(e["t"] == "lane" and e.get("phase") == "learn" for e in evs))
        self.assertGreaterEqual(len(experience.all_items()["runs"]), 1)
        seen.clear()
        self._turn("exp-2", "List the photos on my Desktop again", "Gemma 4 26B", seen, s)
        first = next(b for b in seen if b.get("stream"))
        text = json.dumps(first["messages"])
        self.assertIn("What earlier runs learned (shared by every model)", text)
        self.assertIn("use gedit or write the list to a file", text)
        self.assertIn("Qwen3.8-27B", text)                                   # the note says who learned it

    def test_off(self):
        s = config.load_settings()
        s.update({"shared_learning": False, "router_enabled": False, "memory_enabled": False,
                  "profile_enabled": False, "tool_policy": {**s["tool_policy"], "agents": "off", "desktop": "off"}})
        evs = self._turn("exp-3", "Find the photos on my Desktop", "Qwen", [], s)
        self.assertFalse(any(e["t"] == "learned" for e in evs))
        self.assertEqual(experience.all_items(), {"runs": [], "notes": []})


if __name__ == "__main__":
    unittest.main()
