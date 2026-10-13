"""1.3.1: found on the owner's PC right after the 1.3 update. A chat looked frozen: 212 skills imported from Claude,
Codex and plugins pushed the router's prompt past its 8,192-token context (400 Bad Request), the fallback offered every
tool (a 25,461-token first prompt), and two slots beside a 64k-context 27B left 445 MB of VRAM, so the prompt read at
~200 tok/s instead of ~1,260.
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

from aero import router  # noqa: E402

SKILLS = [{"name": f"skill-{i}", "description": f"Does thing number {i} with spreadsheets, invoices and reports in "
           "a careful, well documented way for finance teams. " * 2} for i in range(200)] + [
    {"name": "modrinth-publisher", "description": "Publish and update Minecraft mods on Modrinth: project pages, "
     "versions, changelogs."},
    {"name": "docx", "description": "Create and edit Word documents."}]
TOOLS = [(f"tool_{i}", "files_read", "Reads something useful from somewhere on this computer. " * 3) for i in range(55)]


class Resp:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": json.dumps({"intent": "x", "complexity": "simple", "tools": [],
                                                               "think": False, "vision": False, "review": False,
                                                               "skills": ["modrinth-publisher"]})}}], "timings": {}}


class Router(unittest.TestCase):
    def run_decide(self, text):
        seen = {}

        def post(url, json=None, timeout=None):
            seen["body"] = json
            return Resp(json)
        with mock.patch.object(router, "ready", lambda: True), \
                mock.patch.dict(router._state, {"server": mock.Mock(url="http://x"), "model": "m", "threads": 4}), \
                mock.patch.object(router.httpx, "post", post):
            d, _ = router.decide([{"role": "user", "content": text}], TOOLS, SKILLS)
        return d, seen["body"]

    def test_hundreds_of_skills_stay_inside_the_routers_context(self):
        d, body = self.run_decide("open my modrinth page for my mediacraft mod and update its changelog")
        total = sum(router._tokens(m["content"]) for m in body["messages"])
        self.assertLess(total, router.CTX - 500)
        enum = body["response_format"]["json_schema"]["schema"]["properties"]["skills"]["items"]["enum"]
        self.assertIn("modrinth-publisher", enum)
        self.assertLessEqual(len(enum), router.SKILLS_IN_PROMPT)
        self.assertNotIn("skill", body["messages"][0]["content"].split("# Tool catalog")[0][-50:])
        self.assertNotIn("modrinth-publisher", body["messages"][0]["content"])      # the cached part stays fixed
        self.assertEqual(d["skills"], ["modrinth-publisher"])

    def test_no_matching_skill_no_skills_field(self):
        _, body = self.run_decide("what time is it in Tokyo")
        self.assertNotIn("skills", body["response_format"]["json_schema"]["schema"]["properties"])

    def test_a_huge_message_is_cut_to_fit(self):
        _, body = self.run_decide("please summarise this: " + "lorem ipsum dolor " * 9000)
        total = sum(router._tokens(m["content"]) for m in body["messages"])
        self.assertLess(total, router.CTX)

    def test_shortlist_ranks_and_caps(self):
        got = router.shortlist(SKILLS, "make a Word document with the docx skill")
        self.assertEqual(got[0]["name"], "docx")
        self.assertLessEqual(len(router.shortlist(SKILLS, "spreadsheets invoices reports finance teams")),
                             router.SKILLS_IN_PROMPT)
        self.assertEqual(router.shortlist(SKILLS, "hi there"), [])

    def test_shortlist_matches_word_forms_and_names(self):
        skills = SKILLS + [{"name": "documentation", "description": "Write project docs."},
                           {"name": "diagram-design", "description": "Draw clear figures."}]
        got = [s["name"] for s in router.shortlist(skills, "Which skills do you have for making documents?")]
        self.assertIn("documentation", got)
        got = [s["name"] for s in router.shortlist(skills, "Make a simple diagram of a pipeline: fetch, parse, store")]
        self.assertIn("diagram-design", got)


class SkillFiles(unittest.TestCase):
    def test_a_skills_reference_is_found_from_a_relative_path(self):
        from aero import tools
        from aero.tools import files, skill_tools
        root = Path(tempfile.mkdtemp(prefix="skill131-"))
        (root / "references").mkdir()
        (root / "references" / "type-process.md").write_text("Boxes left to right.", encoding="utf-8")
        (root / "SKILL.md").write_text("Read references/type-process.md first.", encoding="utf-8")
        ctx = tools.Ctx({}, chat_id="sk131")
        ctx.work_dir = Path(tempfile.mkdtemp(prefix="work131-"))
        with mock.patch.object(skill_tools.skills, "load", lambda n: (
                {"name": n, "path": str(root / "SKILL.md")}, "Read references/type-process.md first.",
                ["references/type-process.md"])):
            skill_tools._use(ctx, "diagram-design")
        r = files.read_file(ctx, "references/type-process.md")
        self.assertIn("Boxes left to right", r if isinstance(r, str) else r.get("text", ""))
        self.assertTrue(files.read_file(ctx, "references/missing.md")["error"])


class Circling(unittest.TestCase):
    """Seen in the sweep: one paragraph of thinking repeated for 73,755 tokens, then an empty reply."""

    def test_repeated_thinking_is_caught(self):
        from aero import agent
        para = ("I don't see a specific Word document skill. Let me inform the user about this and suggest "
                "alternatives. Actually, let me try loading a skill that might be relevant. ")
        text, hit = "The user wants a document skill. ", False
        while len(text) < 20000 and not hit:
            text += para[:37]
            para = para[37:] + para[:37]
            hit = agent.looping(text, 37)
        self.assertTrue(hit)
        self.assertLess(len(text), 6000)

    def test_ordinary_thinking_is_not(self):
        from aero import agent
        import random
        rnd = random.Random(1)
        words = "file read the user wants page open check result then answer list error path value".split()
        text = ""
        for _ in range(4000):
            piece = rnd.choice(words) + " "
            text += piece
            self.assertFalse(agent.looping(text, len(piece)))


class FailedRouter(unittest.TestCase):
    def test_a_failed_router_offers_the_usual_few_tools_not_all(self):
        from aero import agent, config, pipeline, tools
        tools.load_all()
        t = agent.Turn("rf131", [{"role": "user", "content": "open my modrinth page in the browser"}],
                       config.load_settings(), {"url": "x", "model": {}})
        self.addCleanup(t.close)

        def boom(*a, **k):
            raise RuntimeError("400 Bad Request")

        async def go():
            return [ev async for ev in pipeline.route(t, {})]
        with mock.patch.object(pipeline.router, "ready", lambda: True), \
                mock.patch.object(pipeline.router, "trivial", lambda h: False), \
                mock.patch.object(pipeline.router, "decide", boom):
            evs = asyncio.run(go())
        self.assertTrue(any(e.get("t") == "router_error" for e in evs))
        self.assertIsNotNone(t.exposed)
        everything = [c[0] for c in pipeline.tool_catalog(t.settings)]
        self.assertLess(len(t.exposed), len(everything) / 2)
        self.assertIn("browser_open", t.exposed)                         # what the request hints at


class Slots(unittest.TestCase):
    def test_a_nearly_full_card_means_one_slot(self):
        from aero import server
        h = {"gpus": [{"name": "RTX 5080"}], "vram_total_mb": 16303}
        with mock.patch.object(server.hardware, "gpu_used_mb", lambda: 15858):
            self.assertIn("445 MB", server._vram_too_low(h))
        with mock.patch.object(server.hardware, "gpu_used_mb", lambda: 14500):
            self.assertIsNone(server._vram_too_low(h))
        with mock.patch.object(server.hardware, "gpu_used_mb", lambda: None):
            self.assertIsNone(server._vram_too_low(h))
        self.assertIsNone(server._vram_too_low({**h, "vram_measured": False}))


class Launching(unittest.TestCase):
    """Replaying the owner's request: app_launch opened the owner's own Chrome 7 times and the Modrinth App,
    alternating with app_find for 40 calls, and the run ended with no answer."""

    def test_a_web_browser_is_never_launched(self):
        from aero import tools
        from aero.tools import app_tools
        ctx = tools.Ctx({}, chat_id="br131")
        with mock.patch.object(app_tools.app_registry, "launch") as launch:
            for app in ("chrome", "Google Chrome", "msedge.exe", "Firefox", "my browser"):
                r = app_tools.app_launch(ctx, app)
                self.assertTrue(r["error"])
                self.assertIn("browser_open", r["text"])
        launch.assert_not_called()

    def test_an_app_is_started_once_per_request(self):
        from aero import tools
        from aero.tools import app_tools
        ctx = tools.Ctx({}, chat_id="once131")
        ok = {"ok": True, "verified": True, "app": "Modrinth App", "windows": [{"id": 5, "title": "Modrinth App"}]}
        with mock.patch.object(app_tools.app_registry, "launch", return_value=ok) as launch, \
                mock.patch.object(app_tools, "_alive", lambda w: True), \
                mock.patch("aero.tools.apps.on_screen", lambda ctx: False):
            app_tools.app_launch(ctx, "modrinth")
            again = app_tools.app_launch(ctx, "modrinth")
        self.assertEqual(launch.call_count, 1)
        self.assertIn("already started", again["text"])

    def test_the_same_action_at_most_three_times_per_request(self):
        from aero import agent, config
        t = agent.Turn("cap131", [], config.load_settings(), {"url": "x", "model": {}})
        self.addCleanup(t.close)
        args = {"app": "modrinth"}
        for i in range(3):                         # alternating with something else: never "in a row"
            self.assertIsNone(agent._stuck(t, "app_launch", args))
            agent._note_same(t, "app_launch", args, {"text": f"started, window {i}"})
            agent._note_same(t, "app_find", {"query": "x"}, {"text": "found"})
        r = agent._stuck(t, "app_launch", args)
        self.assertTrue(r["error"])
        self.assertIn("3 times in this request", r["text"])
        self.assertIsNone(agent._stuck(t, "app_find", {"query": "x"}))       # looking is never capped

    def test_an_unchanged_file_is_read_at_most_three_times(self):
        from aero import agent, config
        t = agent.Turn("rd131", [], config.load_settings(), {"url": "x", "model": {}})
        self.addCleanup(t.close)
        args = {"path": "template.html"}
        for _ in range(3):
            self.assertIsNone(agent._stuck(t, "read_file", args))
            agent._note_same(t, "read_file", args, {"text": "<html>same</html>"})
        r = agent._stuck(t, "read_file", args)
        self.assertTrue(r["error"])
        agent._note_same(t, "read_file", args, r)                      # the refusal doesn't reset the count
        self.assertTrue(agent._stuck(t, "read_file", args)["error"])
        agent._note_same(t, "write_file", {"path": "template.html"}, {"text": "written"})
        self.assertIsNone(agent._stuck(t, "read_file", args))           # after a write it may have changed
        agent._note_same(t, "read_file", args, {"text": "<html>new</html>"})
        self.assertIsNone(agent._stuck(t, "read_file", args))

    def test_hints_send_my_chrome_to_the_browser_tools(self):
        from aero import capabilities
        names = ["browser_open", "browser_read", "app_launch", "read_file"]
        got = capabilities.hint_tools("open up my chrome and pull up my modrinth page", names)
        self.assertIn("browser_open", got)


class Compaction(unittest.TestCase):
    """A 64k-token tool run compacted its own request away: llama-server answered 500 "No user query found"."""

    def test_the_request_survives_mid_turn_compaction(self):
        from aero import agent, config, pipeline
        hist = [{"role": "user", "content": "open my modrinth page"}]
        for i in range(30):
            hist += [{"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "function": {
                "name": "browser_open", "arguments": "{}"}}]}, {"role": "tool", "tool_call_id": f"c{i}",
                                                                  "content": "page text " * 400}]
        t = agent.Turn("cm131", hist, config.load_settings(), {"url": "x", "model": {}, "ctx": 8192})
        self.addCleanup(t.close)
        t.start_index, t.calib = 1, 1.0

        async def summarize(*a, **k):
            return {"summary": "s", "open_tasks": [], "facts": []}

        async def go():
            return [ev async for ev in pipeline.compact_midturn(t)]
        with mock.patch.object(pipeline.memory, "summarize", summarize), \
                mock.patch.object(pipeline.memory, "store_result", lambda *a, **k: 0), \
                mock.patch.object(pipeline.agent, "memory_recall", lambda *a, **k: ""), \
                mock.patch.object(t, "rebind", lambda new_id: None):
            evs = asyncio.run(go())
        self.assertTrue(any(e.get("t") == "compacted" for e in evs))
        self.assertLess(len(t.history), len(hist))
        self.assertEqual(t.history[0], {"role": "user", "content": "open my modrinth page"})
        self.assertNotEqual(t.history[1].get("role"), "tool")
        self.assertEqual(t.start_index, 1)


class Tabs(unittest.TestCase):
    """The same request opened 15 tabs, guessing at profile addresses."""

    class Page:
        def __init__(self):
            self.closed, self.url = False, "about:blank"

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

        def bring_to_front(self):
            pass

    def test_an_agent_keeps_its_newest_few_tabs(self):
        from aero.tools import browser
        b = browser._Browser()
        made = []

        class Ctx:
            pages = made

            def new_page(self):
                made.append(Tabs.Page())
                return made[-1]
        b._context = lambda *a, **k: Ctx()
        user_tab = Tabs.Page()
        user_tab.url = "https://example.com"
        made.append(user_tab)
        for _ in range(10):
            b.page("o1", new_tab=True)
        open_now = [p for p in made if not p.closed]
        self.assertEqual(len(open_now), browser.TABS_PER_OWNER + 1)
        self.assertFalse(user_tab.closed)
        self.assertIs(b.pages["o1"], made[-1])
        b.close_owner("o1")
        self.assertEqual([p for p in made if not p.closed], [user_tab])

    def test_many_opens_get_a_nudge(self):
        from aero import tools
        from aero.tools import browser
        ctx = tools.Ctx({}, chat_id="nudge131")
        with mock.patch.object(browser, "_go", lambda c, fn, timeout=120: "URL: x"), \
                mock.patch.dict(browser._opens, {}, clear=True):
            texts = [browser.browser_open(ctx, f"https://modrinth.com/user/{i}") for i in range(browser.OPENS_NUDGE)]
        self.assertNotIn("Stop guessing", texts[0])
        self.assertIn("Stop guessing", texts[-1])


class Progress(unittest.TestCase):
    def test_prompt_progress_becomes_prefill_events(self):
        from aero import agent
        src = Path(agent.__file__).read_text(encoding="utf-8")
        self.assertIn('"return_progress": True', src)
        self.assertIn('"t": "prefill"', src)
        js = (Path(agent.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("case 'prefill':", js)


if __name__ == "__main__":
    unittest.main()
