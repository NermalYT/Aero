"""Tests for Local Only (the composer's button): no web, browser or MCP tools for any model, no cloud reviews, and the
model is told it has no internet. Strict offline implies it. Run from source/:
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
from aero import agent, config, localonly, pipeline, tools  # noqa: E402

tools.load_all()
NETWORK = ("web", "browser", "mcp")


def settings(**kw):
    s = config.load_settings()
    s.update(kw)
    return s


def sse_events(chunks):
    out = []
    for c in chunks:
        for line in c.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


class LocalOnlyGate(unittest.TestCase):
    def test_hides_network_tools_only(self):
        on, off = settings(local_only=True), settings(local_only=False, strict_offline=False)
        for cat in NETWORK:
            self.assertFalse(localonly.tool_allowed(cat, on), cat)
            self.assertTrue(localonly.tool_allowed(cat, off), cat)
        for cat in ("files_read", "files_write", "shell", "memory"):
            self.assertTrue(localonly.tool_allowed(cat, on), cat)
        self.assertTrue(localonly.local_only(settings(local_only=False, strict_offline=True)))   # strict implies it

    def test_models_never_see_web_tools(self):
        cats = {t.name: t.category for t in tools.REGISTRY.values()}
        self.assertIn("web_search", cats)
        names = {x["function"]["name"] for x in tools.schemas(settings(local_only=True))}
        self.assertFalse({n for n in names if cats[n] in NETWORK})
        self.assertIn("read_file", names)
        self.assertFalse({c[1] for c in pipeline.tool_catalog(settings(local_only=True))} & set(NETWORK))
        self.assertIn("web_search", {x["function"]["name"] for x in tools.schemas(settings(local_only=False))})

    def test_a_web_call_anyway_is_refused_with_the_reason(self):
        r = tools.run("web_search", {"query": "weather"}, tools.Ctx(settings(local_only=True)))
        self.assertTrue(r["error"])
        self.assertIn("Local Only is on", r["text"])
        r = tools.run("web_search", {"query": "weather"}, tools.Ctx(settings(strict_offline=True)))
        self.assertIn("strict offline", r["text"])

    def test_the_model_is_told(self):
        line = "Local Only is on: you have no internet access"
        self.assertIn(line, agent._env_block(settings(local_only=True), False, 8192))
        self.assertNotIn(line, agent._env_block(settings(local_only=False, strict_offline=False), False, 8192))
        self.assertIn("Strict offline is on", agent._env_block(settings(strict_offline=True), False, 8192))


class LocalOnlyTurn(unittest.TestCase):
    """A whole turn with the scripted model and both cloud reviews switched on."""

    def _turn(self, chat_id, s):
        handler = fake_llama.mock_handler()
        seen = []

        async def spy(request):
            seen.append(json.loads(request.content or b"{}"))
            return await handler(request)
        real = httpx.AsyncClient
        client = lambda **kw: real(transport=httpx.MockTransport(spy), **kw)  # noqa: E731

        async def go():
            hist = [{"role": "user", "content": "What's the weather in Pittsburgh today?"}]
            return [c async for c in pipeline.run_turn(chat_id, hist, s, {"url": "http://fake", "ctx": 32768,
                                                       "model": {"name": "Qwen"}})]
        with mock.patch.object(agent.httpx, "AsyncClient", client):
            return sse_events(asyncio.run(go())), seen

    def _settings(self, local):
        s = config.load_settings()
        s.update({"local_only": local, "review_mode": "on", "chatgpt_review_mode": "on", "router_enabled": False,
                  "memory_enabled": False, "profile_enabled": False, "shared_learning": False,
                  "tool_policy": {**s["tool_policy"], "agents": "off", "desktop": "off"}})
        return s

    def test_no_reviews_and_no_web_tools(self):
        evs, seen = self._turn("lo-1", self._settings(True))
        self.assertTrue(any(e["t"] == "done" for e in evs))
        self.assertFalse([e for e in evs if "review" in json.dumps(e).lower()])        # neither pass ran or was mentioned
        first = next(b for b in seen if b.get("stream"))
        sent = {t["function"]["name"] for t in first.get("tools") or []}
        cats = {t.name: t.category for t in tools.REGISTRY.values()}
        self.assertTrue(sent)
        self.assertFalse({n for n in sent if cats.get(n) in NETWORK})
        self.assertIn("Local Only is on", first["messages"][0]["content"])

    def test_off_still_reaches_the_reviews(self):
        evs, seen = self._turn("lo-2", self._settings(False))           # the same turn with Local Only off
        self.assertTrue([e for e in evs if e["t"] == "notice" and "review" in e.get("text", "").lower()]
                        or [e for e in evs if e.get("lane") in ("fable", "astra")])
        first = next(b for b in seen if b.get("stream"))
        self.assertIn("web_search", {t["function"]["name"] for t in first.get("tools") or []})


if __name__ == "__main__":
    unittest.main()
