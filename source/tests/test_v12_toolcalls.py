"""Tool calls printed as text (toolcall_text) and what the agent does with them: run them, hide the markup, and say so
when tools are off instead of finishing with nothing done. Run from source/:
    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_v11_agent import Model, results, run_turn, settings  # noqa: E402

from aero import toolcall_text as tct  # noqa: E402


class Formats(unittest.TestCase):
    def test_hermes(self):
        # the reply from the bug report (Qwen3.8-27B, tools off)
        t = ('I\'ll check your GitHub for notifications and open pull requests that need your attention.\n\n'
             '<tool_call>\n{"name": "mcp_github_get_user", "arguments": {"username": "someone"}}\n</tool_call>')
        calls, cleaned = tct.parse(t)
        self.assertEqual(calls, [("mcp_github_get_user", {"username": "someone"})])
        self.assertNotIn("tool_call", cleaned)
        self.assertTrue(cleaned.startswith("I'll check your GitHub"))

    def test_unterminated_and_several(self):
        t = '<tool_call>{"name": "a", "arguments": {}}</tool_call>\n<tool_call>{"name": "b", "arguments": {"x": 1}}'
        self.assertEqual(tct.parse(t)[0], [("a", {}), ("b", {"x": 1})])

    def test_qwen_coder_xml(self):
        t = '<tool_call>\n<function=read_file>\n<parameter=path>\nC:/x.txt\n</parameter>\n</function>\n</tool_call>'
        self.assertEqual(tct.parse(t)[0], [("read_file", {"path": "C:/x.txt"})])

    def test_glm(self):
        t = "<tool_call>web_search\n<arg_key>query</arg_key>\n<arg_value>weather</arg_value>\n</tool_call>"
        self.assertEqual(tct.parse(t)[0], [("web_search", {"query": "weather"})])

    def test_functionary(self):
        t = 'Sure. <function=list_dir>{"path": "."}</function>'
        calls, cleaned = tct.parse(t)
        self.assertEqual(calls, [("list_dir", {"path": "."})])
        self.assertEqual(cleaned, "Sure.")

    def test_mistral_both_forms(self):
        self.assertEqual(tct.parse('[TOOL_CALLS][{"name": "a", "arguments": {"k": 2}}]')[0], [("a", {"k": 2})])
        self.assertEqual(tct.parse('[TOOL_CALLS]a[ARGS]{"k": 2}')[0], [("a", {"k": 2})])

    def test_llama_python_tag_and_granite(self):
        self.assertEqual(tct.parse('<|python_tag|>{"name": "a", "parameters": {"q": "x"}}')[0], [("a", {"q": "x"})])
        self.assertEqual(tct.parse('<|tool_call|>[{"name": "a", "arguments": {}}]')[0], [("a", {})])

    def test_deepseek(self):
        t = ("<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>web_search\n```json\n"
             '{"query": "x"}\n```<｜tool▁call▁end｜><｜tool▁calls▁end｜>')
        self.assertEqual(tct.parse(t)[0], [("web_search", {"query": "x"})])

    def test_gemma_tool_code(self):
        t = "```tool_code\nweb_search(query='rain tomorrow', max_results=3)\n```"
        self.assertEqual(tct.parse(t)[0], [("web_search", {"query": "rain tomorrow", "max_results": 3})])

    def test_bare_json_only_at_the_end_and_enabled(self):
        t = 'Checking. {"name": "list_dir", "parameters": {"path": "."}}'
        self.assertEqual(tct.parse(t, {"list_dir"})[0], [("list_dir", {"path": "."})])
        self.assertEqual(tct.parse(t)[0], [])                                   # no enabled set: never
        self.assertEqual(tct.parse('Like {"name": "list_dir", "parameters": {}} this.', {"list_dir"})[0], [])
        self.assertEqual(tct.parse(t, {"read_file"})[0], [])

    def test_plain_text_untouched(self):
        t = "Here is a table:\n\n| a | b |\n|---|---|\n| 1 | 2 |"
        self.assertEqual(tct.parse(t, {"x"}), ([], t))


class Agent(unittest.TestCase):
    def test_printed_call_runs_and_markup_is_hidden(self):
        d = tempfile.mkdtemp()
        Path(d, "a.txt").write_text("x", encoding="utf-8")
        printed = "Looking.\n<tool_call>\n" + json.dumps({"name": "list_dir", "arguments": {"path": d}}) + "\n</tool_call>"
        model = Model([(printed, []), ("One file: a.txt.", [])])
        ev = run_turn(model, settings(), text="what's in that folder?", chat_id="v12-hermes")
        self.assertEqual(len(results(ev, "list_dir")), 1)
        done = [e["message"] for e in ev if e["t"] == "assistant_done"]
        self.assertEqual(done[0]["content"], "Looking.")
        self.assertEqual(done[0]["tool_calls"][0]["function"]["name"], "list_dir")

    def test_tools_off_says_so(self):
        printed = "I'll check.\n<tool_call>\n" + json.dumps({"name": "mcp_github_get_user", "arguments": {}}) + \
            "\n</tool_call>"
        model = Model([(printed, [])])
        ev = run_turn(model, settings(tools_enabled=False), text="check my github", chat_id="v12-toolsoff")
        notes = [e for e in ev if e["t"] == "notice" and e.get("kind") == "tools_off"]
        self.assertEqual(len(notes), 1)
        self.assertIn("mcp_github_get_user", notes[0]["text"])
        self.assertEqual(notes[0]["action"]["settings"], {"tools_enabled": True})
        done = [e["message"] for e in ev if e["t"] == "assistant_done"]
        self.assertEqual(done[-1]["content"], "I'll check.")


if __name__ == "__main__":
    unittest.main()
