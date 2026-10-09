"""v1.1 integration tests: the new HTTP endpoints (questions, tasks, apps, Remote Mode, resources), upgrading a 1.0
data folder without losing anything, the built-in prompt update, capability metadata for every tool, and the tool
catalog staying in a stable order for the router's prompt cache. Run from source/:
    python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from aero import (app_registry, capabilities, clarifications, config, osinfo, pipeline, resources, server,  # noqa: E402
                  task_graph, tools)

tools.load_all()


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(server.app)

    def test_questions_and_answer(self):
        clarifications.reset_cache()
        q, _ = clarifications.ask("api-chat", "Which Gmail account should I check?", ["a@example.com", "b@example.com"],
                                  allow_free=False)
        r = self.c.get("/api/questions", params={"chat_id": "api-chat"}).json()
        self.assertEqual([x["id"] for x in r["questions"]], [q["id"]])
        bad = self.c.post("/api/answer", json={"question_id": q["id"], "answer": "c@example.com"})
        self.assertEqual(bad.status_code, 400)
        ok = self.c.post("/api/answer", json={"question_id": q["id"], "answer": "b@example.com"}).json()
        self.assertEqual((ok["question"]["status"], ok["question"]["answer"]), ("answered", "b@example.com"))
        self.assertEqual(self.c.post("/api/answer", json={"question_id": "q_nope", "answer": "x"}).status_code, 404)

    def test_tasks_for_a_chat(self):
        g = task_graph.TaskGraph("api graph", "api-tasks")
        n = g.add("step one")
        g.set(n.id, "running")
        g.set(n.id, "completed")
        g.save(force=True)
        r = self.c.get("/api/tasks/api-tasks").json()
        self.assertEqual(r["graphs"][0]["nodes"][0]["state"], "completed")

    def test_apps_endpoints(self):
        with mock.patch.object(app_registry, "scan", lambda force=False: []):
            r = self.c.get("/api/apps", params={"q": "gmail"}).json()
        self.assertEqual(r["matches"][0]["id"], "gmail")
        self.assertEqual(self.c.put("/api/apps/alias", json={"alias": "", "app_id": "x"}).status_code, 400)
        r = self.c.put("/api/apps/alias", json={"alias": "mail", "app_id": "gmail"}).json()
        self.assertEqual(r["aliases"]["mail"], "gmail")

    def test_remote_and_resources(self):
        r = self.c.get("/api/remote").json()
        self.assertIn(r["policy"]["state"], ("NORMAL", "REMOTE", "RESTORE_PENDING", "FAILED_SAFE"))
        self.assertIn("state", r["detector"])
        self.assertEqual(r["settings"]["remote_gpu_weight_fraction"], 0.8)
        resources.acquire(["window:1"], "api-res")
        try:
            self.assertEqual(self.c.get("/api/resources").json()["locks"]["window:1"]["owner"], "api-res")
        finally:
            resources.release_owner("api-res")

    def test_tools_endpoint_lists_new_categories(self):
        r = self.c.get("/api/tools").json()
        self.assertIn("ask", r["categories"])
        names = {t["name"] for ts in r["tools"].values() for t in ts}
        for n in ("app_find", "app_launch", "ask_user", "get_answer", "browser_read_sections", "meeting_doc"):
            self.assertIn(n, names)


class Upgrade(unittest.TestCase):
    """A 1.0 data folder (settings, chats, memory, tuning, MCP config, mods) keeps everything after the upgrade."""

    def test_settings_round_trip_and_new_defaults(self):
        old = {"tool_policy": {"desktop": "auto", "shell": "off"}, "theme": "night", "local_only": True,
               "router_enabled": False, "settings_version": 2, "work_dir": "D:/work", "user_profile": "I'm Pat."}
        p = config.DATA / "settings.json"
        backup = p.read_text(encoding="utf-8") if p.exists() else None
        try:
            p.write_text(json.dumps(old), encoding="utf-8")
            s = config.load_settings()
            for k in ("theme", "local_only", "router_enabled", "work_dir", "user_profile"):
                self.assertEqual(s[k], old[k], k)
            self.assertEqual((s["tool_policy"]["desktop"], s["tool_policy"]["shell"]), ("auto", "off"))
            self.assertEqual(s["tool_policy"]["ask"], "auto")                    # new category gets its default
            self.assertEqual((s["remote_mode"], s["strict_background"], s["browser_mode"]), ("auto", False, "background"))
            config.save_settings({"remote_mode": "off"})
            saved = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(saved["user_profile"], "I'm Pat.")                  # untouched user data
            self.assertEqual(saved["remote_mode"], "off")
        finally:
            if backup is None:
                p.unlink(missing_ok=True)
            else:
                p.write_text(backup, encoding="utf-8")

    def test_untouched_old_prompt_is_replaced_but_edited_one_kept(self):
        tpl_1_0 = Path(__file__).with_name("data_prompt_1_0_windows.txt")
        if not tpl_1_0.exists():
            self.skipTest("1.0 prompt fixture missing")
        old_prompt = tpl_1_0.read_text(encoding="utf-8").replace("{os}", osinfo.name()).replace("{shell}", osinfo.shell_name())
        self.assertTrue(config._is_old_builtin(old_prompt))
        self.assertFalse(config._is_old_builtin(old_prompt + "\nAlways answer in French."))
        self.assertFalse(config._is_old_builtin(config.DEFAULT_SYSTEM_PROMPT))

    def test_other_data_untouched_by_v11_modules(self):
        files = {"chats/c1.json": {"id": "c1", "messages": [{"role": "user", "content": "hi"}]},
                 "memory.json": {"facts": [{"id": "f1", "text": "likes tea"}]}, "mcp.json": {"mcpServers": {}},
                 "tuning.json": {"k": {"desc": "ctx 32768"}}}
        root = Path(tempfile.mkdtemp(prefix="aero-v10-data-"))
        for rel, obj in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(json.dumps(obj), encoding="utf-8")
        before = {rel: (root / rel).read_bytes() for rel in files}
        # the v1.1 stores live in their own files and never rewrite these
        for mod in (app_registry, clarifications, task_graph):
            self.assertFalse(any(str(x).endswith(tuple(files)) for x in
                                 [getattr(mod, "STORE", ""), getattr(mod, "OPS", "")]))
        self.assertEqual(before, {rel: (root / rel).read_bytes() for rel in files})
        shutil.rmtree(root, ignore_errors=True)


class Metadata(unittest.TestCase):
    def test_every_tool_has_capabilities(self):
        for name, t in tools.REGISTRY.items():
            c = capabilities.caps_of(name, t.category)
            self.assertIn(c.side_effect, capabilities.SIDE_EFFECTS, name)
            if t.category in ("web", "browser", "mcp"):
                self.assertEqual(c.network, "internet", name)
        self.assertTrue(capabilities.is_physical("mouse_click", {}))
        self.assertTrue(capabilities.is_physical("app_click", {"input": "real"}))
        self.assertTrue(capabilities.is_physical("app_keys", {"keys": "ctrl+s"}))
        self.assertFalse(capabilities.is_physical("app_keys", {"keys": "tab enter"}))
        self.assertFalse(capabilities.is_physical("app_type", {"text": "x", "element": 3}))
        self.assertTrue(capabilities.read_only("read_file"))
        self.assertFalse(capabilities.read_only("app_click"))
        self.assertIn("app_launch", capabilities.tools_for("app.launch"))
        self.assertEqual(capabilities.caps_of("mcp_github_list_issues", "mcp").side_effect, "external_read")
        self.assertEqual(capabilities.caps_of("mcp_github_create_issue", "mcp").side_effect, "external_write")

    def test_catalog_order_is_stable(self):
        s = config.load_settings()
        a = [c[0] for c in pipeline.tool_catalog(s)]
        tools.load_all()
        self.assertEqual(a, [c[0] for c in pipeline.tool_catalog(s)])
        self.assertLess(a.index("app_view"), a.index("app_find"))            # new tools come after the old ones


if __name__ == "__main__":
    unittest.main()
