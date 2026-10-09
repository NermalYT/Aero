"""Tests for agents, subagents and the "is controlling" signal: job-title names for agents, the dashboard listing
(working agents first, each with its subagents, live state merged with saved chats), the system prompt for a chat
with a subagent, a whole turn where the agent starts a subagent and then opens an app, Stop for every chat, and the
banner's placement over the controlled window. The model is a scripted fake (tests/fake_llama.py) behind an
in-process mock transport; desktop actions are faked, nothing is clicked or opened.
Run from source/:
    python -m unittest discover -s tests -v
"""
import asyncio
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

import httpx  # noqa: E402

import fake_llama  # noqa: E402
from aero import agent, agents, config, control, overlay, pipeline, tools  # noqa: E402
from aero.server import parse_title  # noqa: E402

tools.load_all()


def sse_events(chunks):
    return [json.loads(c[6:]) for c in chunks if c.startswith("data: ")]


class Names(unittest.TestCase):
    def test_job_title_from_request(self):
        cases = {"Tidy my Desktop": "Desktop Organizer", "Rename the .jpeg photos to .jpg": "Photo Renamer",
                 "Fix the login bug in app.py": "Bug Fixer", "why is my GPU hot": "GPU Helper",
                 "Summarize these meeting notes": "Note Summarizer", "": "Task Agent"}
        for text, want in cases.items():
            self.assertEqual(agents.name_from_text(text), want, text)

    def test_clean_name(self):
        self.assertEqual(agents.clean_name("desktop organizer!!"), "Desktop Organizer")
        self.assertEqual(agents.clean_name('agent: "Bug Fixer"'), "Bug Fixer")
        self.assertEqual(agents.clean_name("C++ Builder 🚀"), "C++ Builder")
        self.assertEqual(agents.clean_name("a very long name that goes on and on"), "A Very Long Name")
        self.assertEqual(agents.clean_name(None), "")

    def test_title_reply_parsing(self):
        self.assertEqual(parse_title('{"title": "Rename photos", "agent": "photo renamer"}'),
                         {"title": "Rename photos", "agent": "Photo Renamer"})
        self.assertEqual(parse_title('<think>hm</think>```json\n{"title":"Tidy desktop","agent":"Desktop Organizer"}\n```'),
                         {"title": "Tidy desktop", "agent": "Desktop Organizer"})
        self.assertEqual(parse_title("Fixing the login bug"), {"title": "Fixing the login bug", "agent": None})
        self.assertEqual(parse_title('{"title": "Cut off'), {"title": "Cut off", "agent": None})


class Control(unittest.TestCase):
    def test_target_names(self):
        self.assertEqual(control.target("open_app", {"target": "notepad"}), ("Notepad", None))
        self.assertEqual(control.target("open_app", {"target": r"C:\Windows\notepad.exe"}), ("Notepad", None))
        self.assertEqual(control.target("browser_click", {}), ("the browser", None))
        self.assertEqual(control.target("focus_window", {"title": "notes.txt - Notepad"}), ("Notepad", None))
        self.assertEqual(control.short_app("Inbox (3) - me@example.com - Outlook"), "Outlook")

    def test_actor_names_the_model(self):
        t = type("T", (), {"settings": {}, "model_name": "Qwen3.8-27B"})()
        self.assertEqual(control.actor(t, "local"), "Qwen3.8-27B")
        self.assertEqual(control.actor(t, "opus"), "Claude Opus 5.5")
        self.assertEqual(control.actor(type("T", (), {"settings": {}})(), "local"), "Aero")

    def test_only_actions_count(self):
        for name in ("mouse_click", "type_text", "press_keys", "open_app", "app_click", "browser_click"):
            self.assertIn(name, control.CONTROL_TOOLS)
        for name in ("screenshot", "read_file", "list_dir", "app_read", "browser_read"):
            self.assertNotIn(name, control.CONTROL_TOOLS)

    def test_banner_sits_on_the_controlled_window(self):
        self.assertEqual(overlay.banner_pos((100, 100, 900, 700), (300, 34), (1920, 1080)), (350, 108))
        self.assertEqual(overlay.banner_pos(None, (300, 34), (1920, 1080)), (810, 8))
        self.assertEqual(overlay.banner_pos(None, (300, 34), (1920, 1080), low=True), (810, 990))
        self.assertTrue(overlay.covers((350, 108), (300, 34), (400, 120)))
        self.assertFalse(overlay.covers((350, 108), (300, 34), (400, 600)))


class Listing(unittest.TestCase):
    def setUp(self):
        agents.LIVE.clear()
        agents._recent["t"] = 0       # files are written per test; don't reuse the cached folder listing
        for p in config.CHATS.glob("*.json"):
            p.unlink()

    def _save(self, cid, msgs, title, meta=None, age=0):
        c = {"id": cid, "title": title, "messages": msgs, "updated": time.time() - age}
        if meta:
            c["agent"] = meta
        p = config.CHATS / f"{cid}.json"
        p.write_text(json.dumps(c), encoding="utf-8")
        os.utime(p, (time.time() - age, time.time() - age))

    def test_saved_and_live_agents_with_subagents(self):
        sub = {"role": "subagent", "id": "s1", "name": "Photo Scout", "task": "List the .jpeg files", "status": "done",
               "result": "Found 3 photos.", "messages": [{"role": "user", "content": "List"}, {"role": "tool", "content": "x"}]}
        self._save("old", [{"role": "user", "content": "Tidy my Desktop"},
                           {"role": "assistant", "content": "Sorted 12 files into folders.", "lane": "local"}],
                   "Tidy Desktop", {"name": "Desktop Organizer"}, age=600)
        self._save("photos", [{"role": "user", "content": "Rename the .jpeg photos"}, sub,
                              {"role": "assistant", "content": "Renamed 3 photos.", "lane": "local"}], "Rename photos", age=300)
        self._save("talk", [{"role": "user", "content": "what did you find?"}], "Photo Scout · Rename photos",
                   {"name": "Photo Scout", "kind": "subagent", "parent": "photos", "sub_id": "s1"}, age=100)
        agents._recent["t"] = 0
        agents.start("new", "Bug Fixer", "Fix login", "Qwen3.8-27B", "Fix the login bug", {"named": True})
        agents.note("new", {"t": "tool_start", "name": "read_file", "label": "app.py"})
        rows = agents.listing()
        self.assertEqual([r["id"] for r in rows], ["new", "photos", "old"])      # working first, then newest
        self.assertEqual(rows[0]["status"], "working")
        self.assertEqual(rows[0]["doing"], "Reading app.py")
        self.assertEqual(rows[2]["name"], "Desktop Organizer")
        self.assertEqual(rows[2]["summary"], "Sorted 12 files into folders.")
        self.assertEqual(rows[1]["name"], "Photo Renamer")                       # from the request's wording
        s = rows[1]["subs"][0]
        self.assertEqual((s["name"], s["status"], s["steps"], s["chat"]), ("Photo Scout", "done", 1, "talk"))
        self.assertNotIn("talk", [r["id"] for r in rows])                        # a subagent chat is not an agent

    def test_live_subagent_and_finish(self):
        agents.start("c1", "Photo Renamer", "", "m", "Rename photos")
        agents.note("c1", {"t": "subagent_start", "sub": {"id": "s9", "name": "Photo Scout", "task": "List them"}})
        agents.note("c1", {"t": "tool_start", "name": "list_dir", "label": "Desktop", "sub": "s9"})
        agents.note("c1", {"t": "control", "by": "Qwen", "target": "Notepad"})
        row = agents.listing()[0]
        self.assertEqual(row["doing"], "Waiting for Photo Scout")
        self.assertEqual(row["controlling"], {"by": "Qwen", "target": "Notepad"})
        self.assertEqual((row["subs"][0]["status"], row["subs"][0]["doing"]), ("working", "Listing Desktop"))
        agents.finish("c1", "stopped", "")
        row = agents.listing()[0]
        self.assertEqual((row["status"], row["controlling"], row["subs"][0]["status"]), ("stopped", None, "stopped"))

    def test_model_name_survives_a_new_turn(self):
        agents.start("c2", "Photo Agent", "", "m", "x")
        agents.set_name("c2", "Photo Renamer")
        agents.start("c2", "Photo Agent", "", "m", "y")                          # heuristic name on the next turn
        self.assertEqual(agents.LIVE["c2"]["name"], "Photo Renamer")

    def test_persona_for_a_subagent_chat(self):
        rec = {"role": "subagent", "id": "s1", "name": "Photo Scout", "task": "List every .jpeg file", "status": "done",
               "result": "Found beach.jpeg and dog.jpeg.",
               "messages": [{"role": "user", "content": "List every .jpeg file"},
                            {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "type": "function",
                             "function": {"name": "list_dir", "arguments": "{\"path\": \"Desktop\"}"}}]},
                            {"role": "tool", "tool_call_id": "c", "name": "list_dir", "content": "beach.jpeg dog.jpeg"}]}
        self._save("photos", [{"role": "user", "content": "Rename the .jpeg photos"}, rec], "Rename photos",
                   {"name": "Photo Renamer"})
        p = agents.persona({"name": "Photo Scout", "kind": "subagent", "parent": "photos", "sub_id": "s1"})
        for want in ("Photo Scout", "Photo Renamer", "List every .jpeg file", "Found beach.jpeg and dog.jpeg.", "list_dir"):
            self.assertIn(want, p)
        gone = agents.persona({"name": "Photo Scout", "kind": "subagent", "parent": "deleted", "sub_id": "s1"})
        self.assertIn("no longer saved", gone)


class Turn(unittest.TestCase):
    """A whole turn against the scripted model: a subagent, then an app opened (a control action)."""

    def setUp(self):
        agents.LIVE.clear()
        self.folder = tempfile.mkdtemp(prefix="aero-desk-")
        for n in ("beach.jpeg", "dog.jpeg", "notes.txt"):
            Path(self.folder, n).write_text("x")
        self.banners = []
        real_client, real_run = httpx.AsyncClient, tools.run
        handler = fake_llama.mock_handler(self.folder)

        def client(**kw):
            return real_client(transport=httpx.MockTransport(handler), **kw)

        def run(name, args, ctx):
            if name in control.CONTROL_TOOLS:
                return {"text": f"Opened {args.get('target')}", "error": False}
            return real_run(name, args, ctx)
        self.patches = [mock.patch.object(agent.httpx, "AsyncClient", client), mock.patch.object(agent.tools, "run", run),
                        mock.patch.object(control, "banner_on", lambda text, rect=None: self.banners.append(text)),
                        mock.patch.object(control, "banner_off", lambda: self.banners.append(None))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def run_turn(self, settings=None):
        s = config.load_settings()
        s.update({"router_enabled": False, "memory_enabled": False, "profile_enabled": False, **(settings or {})})
        s["tool_policy"] = {**s["tool_policy"], "desktop": "auto", **((settings or {}).get("tool_policy") or {})}
        hist = [{"role": "user", "content": "Rename the .jpeg photos on my Desktop to .jpg and open Notepad"}]

        async def go():
            out = []
            async for chunk in pipeline.run_turn("turn-1", hist, s, {"url": "http://fake", "ctx": 32768,
                                                                      "model": {"name": "Qwen3.8-27B"}},
                                                  agent_meta={"name": "Photo Renamer"}):
                out.append(chunk)
            return out
        return sse_events(asyncio.run(go()))

    def test_subagent_then_control(self):
        evs = self.run_turn()
        kinds = [e["t"] for e in evs]
        self.assertNotIn("error", kinds, [e for e in evs if e["t"] == "error"])
        start = next(e for e in evs if e["t"] == "subagent_start")["sub"]
        done = next(e for e in evs if e["t"] == "subagent_done")["sub"]
        self.assertEqual((start["name"], done["status"], done["steps"]), ("Photo Scout", "done", 1))
        self.assertIn("beach.jpeg", done["result"])
        self.assertIn("dog.jpeg", done["result"])
        sub_evs = [e for e in evs if e.get("sub") == start["id"]]
        self.assertTrue(any(e["t"] == "tool_result" and e["message"]["name"] == "list_dir" for e in sub_evs))
        self.assertTrue(all(e["t"] not in ("subagent_start", "control_end", "done") for e in sub_evs))
        report = next(e for e in evs if e["t"] == "tool_result" and e["message"]["name"] == "run_subagent" and not e.get("sub"))
        self.assertTrue(report["message"]["content"].startswith("Report from subagent Photo Scout:"))
        ctl = next(e for e in evs if e["t"] == "control")
        self.assertEqual((ctl["by"], ctl["target"], ctl["tool"]), ("Qwen3.8-27B", "Notepad", "open_app"))
        self.assertEqual(kinds[-2:], ["control_end", "done"])
        self.assertEqual(self.banners, ["Qwen3.8-27B is controlling Notepad", None])
        live = agents.LIVE["turn-1"]
        self.assertEqual((live["name"], live["status"], live["controlling"]), ("Photo Renamer", "done", None))
        self.assertEqual(live["subs"][start["id"]]["status"], "done")

    def test_subagents_off(self):
        evs = self.run_turn({"tool_policy": {"agents": "off"}})
        self.assertFalse(any(e["t"] == "subagent_start" for e in evs))
        res = next(e for e in evs if e["t"] == "tool_result" and e["message"]["name"] == "run_subagent")
        self.assertTrue(res["message"]["error"])

    def test_subagent_cannot_start_subagents(self):
        parent = agent.Turn("p", [], config.load_settings(), {"url": "x", "ctx": 8192, "model": {"name": "m"}})
        sub = agent.SubTurn(parent, "s", "Scout", "task")
        self.assertIn("run_subagent", sub.blocked)
        self.assertNotIn("run_subagent", [s["function"]["name"] for s in agent.current_schemas(sub)])
        self.assertIn("Your role: subagent", sub.persona)
        parent.close()


class StopAll(unittest.TestCase):
    def test_stops_every_chat_and_denies_waiting_approvals(self):
        async def go():
            a = agent.Turn("a", [], config.load_settings(), {"url": "x", "model": {}})
            b = agent.Turn("b", [], config.load_settings(), {"url": "x", "model": {}})
            fut = asyncio.get_running_loop().create_future()
            agent._approvals["call"] = fut
            n = agent.stop_all()
            res = (n >= 2, a.cancel.is_set(), b.cancel.is_set(), fut.result())
            a.close(), b.close()
            agent._approvals.pop("call", None)
            return res
        self.assertEqual(asyncio.run(go()), (True, True, True, "deny"))


if __name__ == "__main__":
    unittest.main()
