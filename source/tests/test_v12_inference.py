"""Prompt inference (intents.py) and the forced first action (forced_call.py) in the agent loop: a request that plainly
needs an action gets a plan, the right tools and, when the model answers "I can't", a grammar-forced call instead.
Run from source/:
    python -m unittest discover -s tests -v
"""
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

from test_v11_agent import Model, results, run_turn, settings  # noqa: E402

from aero import forced_call, intents, tools  # noqa: E402


class Intents(unittest.TestCase):
    def test_matches(self):
        cases = {
            "Look at my GitHub notifications and open pull requests and summarize what needs my attention.": "github",
            "check my emails and look for any meetings i have this week": "mail",
            "Do I have any unread email in Outlook?": "mail",
            "What's on my calendar this week?": "calendar",
            "what's the weather in Paris tomorrow": "lookup",
            "what's my ip address": "system",
            "what's in my downloads folder?": "folder",
            "open spotify": "open_app",
        }
        for text, want in cases.items():
            it = intents.match(text, {"use_bookmarks_for_accounts": False})
            self.assertIsNotNone(it, text)
            self.assertEqual(it.id, want, text)
        for text in ("hi, how are you?", "write a poem about frogs", "explain how email works", "mail merge in Word"):
            self.assertIsNone(intents.match(text, {"use_bookmarks_for_accounts": False}), text)

    def test_visibility(self):
        self.assertTrue(intents.match("check my github notifications", {}).visible)
        self.assertFalse(intents.match("check my github notifications in the background", {}).visible)

    def test_mail_checks_every_account_and_bookmarks(self):
        d = Path(tempfile.mkdtemp())
        bm = {"roots": {"bookmark_bar": {"children": [
            {"type": "url", "name": "Yahoo Mail", "url": "https://mail.yahoo.com/d/folders/1"},
            {"type": "url", "name": "News", "url": "https://news.example.com"},
            {"type": "folder", "children": [{"type": "url", "name": "Work mail", "url": "https://outlook.office.com/mail/"}]}]}}}
        (d / "Default").mkdir()
        (d / "Default" / "Bookmarks").write_text(json.dumps(bm), encoding="utf-8")
        with mock.patch.object(intents, "_bookmark_files", lambda: [d / "Default" / "Bookmarks"]):
            sites = intents.mail_sites({"mail_accounts": ["https://mail.proton.me/u/0/inbox"]})
        urls = [u for _, u in sites]
        self.assertEqual(urls[0], "https://mail.proton.me/u/0/inbox")          # the user's own list first
        self.assertIn("https://mail.yahoo.com/d/folders/1", urls)               # from bookmarks
        self.assertIn("https://mail.google.com/mail/u/0/", urls)                # always Gmail and Outlook
        self.assertEqual(sum("outlook.office.com" in u for u in urls), 1)        # each site once
        self.assertNotIn("https://news.example.com", urls)
        it = intents.match("check my emails and look for meetings this week", {})
        self.assertIn("meeting_doc", it.tools)
        self.assertEqual(it.skill, "meetings-from-email")
        self.assertTrue(it.close_browser)


class ForcedSchema(unittest.TestCase):
    def test_schema_is_an_anyof_of_tools(self):
        tl = [{"type": "function", "function": {"name": "a", "parameters": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x", "gone"]}}},
              {"type": "function", "function": {"name": "b", "parameters": {"type": "object", "properties": {}}}}]
        s = forced_call.schema(tl)
        self.assertEqual([o["properties"]["tool"]["const"] for o in s["anyOf"]], ["a", "b"])
        self.assertEqual(s["anyOf"][0]["properties"]["arguments"]["required"], ["x"])     # unknown required dropped

    def test_plain_messages_alternate(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
                {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "f", "arguments": "{}"}}]},
                {"role": "tool", "name": "f", "content": "r"}]
        out = forced_call._plain(msgs)
        self.assertEqual([m["role"] for m in out], ["system", "user", "assistant", "user"])
        self.assertIn("[result of f]", out[-1]["content"])


class ForcedFirstAction(unittest.TestCase):
    def setUp(self):
        self.ran = []
        real = tools.run

        def run(name, args, ctx):
            if name.startswith("browser_"):
                self.ran.append((name, args, getattr(ctx, "visible", None)))
                return {"text": "URL: https://github.com/notifications\nTitle: Notifications\n\nPage text:\nNothing new."}
            return real(name, args, ctx)
        self.p = mock.patch.object(tools, "run", run)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_refusal_becomes_the_planned_action(self):
        model = Model([("I don't have direct access to your GitHub notifications or pull requests.", []),
                       ("Nothing needs your attention on GitHub right now.", [])])
        seen = {}

        def fake_ask(url, messages, tl, temperature=0.2, hint="", **kw):
            seen["tools"] = [t["function"]["name"] for t in tl]
            seen["hint"] = hint
            return "browser_open", {"url": "https://github.com/notifications", "show": True}
        with mock.patch.object(forced_call, "ask", fake_ask):
            ev = run_turn(model, settings(), text="Look at my GitHub notifications and summarize what needs my attention.",
                          chat_id="v12-github")
        self.assertEqual(seen["tools"], ["browser_open"])
        self.assertIn("github.com/notifications", seen["hint"])
        self.assertEqual(self.ran[0][0], "browser_open")
        self.assertTrue(self.ran[0][2])                                   # on screen: the user watches
        done = [e["message"] for e in ev if e["t"] == "assistant_done"]
        self.assertEqual(done[0]["content"], "")                          # the refusal is gone from the chat
        self.assertEqual(done[0]["forced"], "intent")
        self.assertTrue(any(e["t"] == "intent" and e["intent"]["id"] == "github" for e in ev))

    def test_capability_refusal_without_intent(self):
        model = Model([("I can't read files on your computer, as an AI I don't have access to your folders.", []),
                       ("It holds notes.txt.", [])])
        d = tempfile.mkdtemp()
        Path(d, "notes.txt").write_text("x", encoding="utf-8")
        with mock.patch.object(forced_call, "ask", lambda *a, **k: ("list_dir", {"path": d})):
            ev = run_turn(model, settings(), text=f"look in {d} please", chat_id="v12-refusal")
        self.assertEqual(len(results(ev, "list_dir")), 1)

    def test_tuned_act_first_forces_before_asking(self):
        # the tuner found forced first actions work better for this model: no "I can't" round at all
        model = Model([("Nothing needs your attention on GitHub right now.", [])])
        with mock.patch.object(forced_call, "ask", lambda *a, **k: ("browser_open", {"url": "https://github.com/notifications"})):
            ev = run_turn(model, settings(), text="check my github notifications", chat_id="v12-actfirst",
                          engine={"tune": {"tools": {"act_first": True, "temperature": 0.2}}})
        self.assertEqual(len(model.bodies), 1)                            # only the answer after the tool result
        self.assertEqual(model.bodies[0]["temperature"], 0.2)            # tuned tool temperature
        self.assertEqual(self.ran[0][0], "browser_open")
        done = [e["message"] for e in ev if e["t"] == "assistant_done"]
        self.assertEqual(done[0]["forced"], "intent")

    def test_no_forcing_for_plain_chat(self):
        model = Model([("Hello! I'm doing well.", [])])
        with mock.patch.object(forced_call, "ask", side_effect=AssertionError("must not force")):
            run_turn(model, settings(), text="hi, how are you?", chat_id="v12-plain")


if __name__ == "__main__":
    unittest.main()
