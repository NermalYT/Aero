"""Review & test workspace, part 2: tester-army/e2e (Apache-2.0) built into Aero.

e2e mixes natural-language agent steps with Playwright locators. Aero installs it (npm: e2e, @e2e-dev/web, ai,
@ai-sdk/openai-compatible) into its own toolchains folder, always runs it with E2E_TELEMETRY_DISABLED=1 (e2e's
telemetry is on by default), and points its agent at Aero's local model through @ai-sdk/openai-compatible.

  e2e_explore  explore a web app at a URL toward a goal and report findings (no tests needed): Aero writes a minimal
               e2e project for it under data/integrations/e2e
  e2e_run      run a project's own e2e tests (its e2e.config.ts), JSON reporter, failures summarised
"""
import json
import re
import time
from pathlib import Path

from . import Pack, Requirement, register, toolchain as tc
from .review import local_llm
from .. import action_results, capabilities
from ..config import IS_WIN
from ..tools import tool

PREFIX = "e2e"
PACKAGES = ["e2e@0.19", "@e2e-dev/web", "ai@^7", "@ai-sdk/openai-compatible"]
PROJECTS = tc.npm_prefix(PREFIX) / "projects"         # inside the prefix so node resolves its node_modules
CONFIG = """import type {{ E2EConfig }} from 'e2e';
import {{ web }} from '@e2e-dev/web';
import {{ createOpenAICompatible }} from '@ai-sdk/openai-compatible';

// Written by Aero: explore {url} with Aero's local model.
const aero = createOpenAICompatible({{ name: 'aero', baseURL: {base}, apiKey: 'aero-local' }});

export default {{
  tests: 'tests/**/*.e2e.ts',
  targets: [{{ engine: web(), app: {{ url: {url_js} }} }}],
  agents: {{ default: {{ model: aero.chatModel('local'), system: 'You are a careful QA agent. Verify outcomes on screen.' }} }},
  reporters: ['json'],
  cache: 'off',
}} satisfies E2EConfig;
"""


def e2e_bin():
    return tc.npm_bin(PREFIX, "e2e")


def install(emit=None, cancel=None):
    ok, det = tc.node_ok(22)
    if not ok:
        raise RuntimeError(det + ". Install Node.js 22+ (Code workspace: toolchain node).")
    tc.npm_install(PREFIX, PACKAGES, emit, cancel)
    tc.mark("e2e", version=",".join(PACKAGES))
    return "e2e and its web engine"


def _project(url):
    slug = re.sub(r"[^a-z0-9]+", "-", url.lower())[:60].strip("-") or "app"
    d = PROJECTS / slug
    (d / "tests").mkdir(parents=True, exist_ok=True)
    base, _ = local_llm()
    (d / "e2e.config.ts").write_text(CONFIG.format(url=url, url_js=json.dumps(url), base=json.dumps(base)),
                                     encoding="utf-8")
    pj = d / "package.json"
    if not pj.exists():
        pj.write_text(json.dumps({"name": f"aero-e2e-{slug}", "private": True, "type": "module"}), encoding="utf-8")
    return d


def _findings(out_dir):
    """Every finding object in the run's JSON output (the report format is e2e's; we only read it)."""
    found = []

    def walk(x):
        if isinstance(x, dict):
            if "severity" in x and ("title" in x or "issue" in x) and ("actual" in x or "expected" in x):
                found.append(x)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    for p in Path(out_dir).rglob("*.json") if Path(out_dir).exists() else []:
        try:
            walk(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return found


def explore(url, goal="", max_steps=6, cancel=None):
    exe = e2e_bin()
    if not exe:
        raise RuntimeError("e2e isn't installed (integration_install e2e).")
    _, ok = local_llm()
    if not ok:
        raise RuntimeError("No local model is loaded; e2e's agent steps use Aero's model. Load a model first.")
    d = _project(url)
    out = d / ".e2e" / f"explore-{int(time.time())}"
    cmd = [str(exe), "explore"] + ([goal] if goal else []) + ["--max-steps", str(max(1, min(12, int(max_steps or 6)))),
                                                           "--output", str(out)]
    r = tc.run(cmd, cwd=d, timeout=1200, cancel=cancel, env_extra={"CI": "1"})
    return {"rc": r["rc"], "stdout": r["out"][-6000:], "stderr": r["err"][-2000:], "findings": _findings(out),
            "output": str(out), "project": str(d)}


@tool("e2e_explore", "Explore a web app in a headless browser toward a goal and report what's broken (title, "
      "severity 1-5, expected vs actual, steps to reproduce), using e2e's explorer and Aero's local model. No tests "
      "needed. Use for 'test my site', 'find bugs in this app', 'bug bash'.", "dev",
      {"url": {"type": "string"}, "goal": {"type": "string", "description": "One sentence: the area and the posture"},
       "max_steps": {"type": "integer", "description": "1-12, default 6"}}, ["url"],
      summary=lambda a: f"{a.get('url', '')} · {a.get('goal', '')}"[:120])
def e2e_explore(ctx, url, goal="", max_steps=6):
    try:
        r = explore(url, goal, max_steps, ctx.cancel)
    except (RuntimeError, tc.Offline) as e:
        return {"text": str(e), "error": True}
    lines = [f"e2e explore {url}: exit {r['rc']}, {len(r['findings'])} findings (output {r['output']})"]
    for f in r["findings"][:20]:
        lines.append(f"- [{f.get('severity')}] {f.get('title') or f.get('issue')}: expected {f.get('expected')}; "
                     f"actual {f.get('actual')}")
    if not r["findings"]:
        lines.append(r["stdout"][-3000:] or r["stderr"][-1500:])
    env = action_results.make("terminal", verified=r["rc"] in (0, 1), method=f"e2e exit {r['rc']}", changed={})
    return action_results.attach({"text": "\n".join(lines), "error": r["rc"] not in (0, 1)}, env)


@tool("e2e_run", "Run a project's e2e tests (its e2e.config.ts) with the JSON reporter and summarise what failed.",
      "dev", {"path": {"type": "string"}, "grep": {"type": "string", "description": "Only tests matching this"}},
      summary=lambda a: a.get("path") or ".")
def e2e_run(ctx, path=".", grep=""):
    exe = e2e_bin() or tc.which("e2e")
    root = ctx.path(path)
    local = root / "node_modules" / ".bin" / ("e2e.cmd" if IS_WIN else "e2e")
    if local.exists():
        exe = local
    if not exe:
        return {"text": "e2e isn't installed here or in Aero (integration_install e2e).", "error": True}
    if not list(root.glob("e2e.config.*")):
        return {"text": f"No e2e.config.ts in {root}. e2e_explore works without one.", "error": True}
    cmd = [str(exe), "run", "--reporter", "json"] + (["--grep", grep] if grep else [])
    r = tc.run(cmd, cwd=root, timeout=3600, cancel=ctx.cancel, env_extra={"CI": "1"})
    text = r["out"]
    try:
        j = json.loads(text[text.index("{"):])
        tests = j.get("tests") or j.get("results") or []
        failed = [t for t in tests if str(t.get("status", "")).lower() in ("failed", "timedout", "error")]
        summary = f"{len(tests)} tests, {len(failed)} failed" + "".join(
            f"\n- {t.get('title') or t.get('name')}: {str(t.get('error') or '')[:300]}" for t in failed[:20])
    except (ValueError, AttributeError):
        summary = (text or r["err"])[-4000:]
    env = action_results.make("terminal", verified=True, method=f"e2e exit {r['rc']}", changed={})
    return action_results.attach({"text": f"e2e run in {root}: exit {r['rc']}\n{summary}", "error": r["rc"] != 0}, env)


capabilities.register("e2e_explore", ["test.explore", "browser.read"], "local_write", "internet", cost="high")
capabilities.register("e2e_run", ["test.run"], "local_write", cost="high")


def _ok():
    b = e2e_bin()
    return bool(b), str(b) if b else "not installed"


register(Pack("e2e", "e2e", "review", "End-to-end tests and app exploration with natural-language agent steps",
              "https://github.com/tester-army/e2e", "Apache-2.0 (agent-device MIT)",
              triggers=[r"\b(e2e|end[- ]to[- ]end|ui tests?|integration tests?|bug ?bash|smoke tests?)\b",
                        r"\b(test|explore|qa)\b.{0,30}\b(my |the )?(web ?app|website|site|checkout|signup|login flow)\b",
                        r"\bfind (bugs|issues|problems) (in|on) (my|the|this)\b"],
              tools=["e2e_explore", "e2e_run", "browser_open", "browser_read_sections"], skills=["e2e"],
              requirements=[Requirement("node", "Node.js 22+", lambda: tc.node_ok(22),
                                        how="install Node.js (Code workspace: toolchain node)"),
                            Requirement("e2e", "e2e + web engine (npm, about 120 MB with the browser)", _ok,
                                        install=install, size_mb=120)],
              notes="Agent-step reliability with small local models is unknown; mobile testing needs Xcode or the "
                    "Android SDK and is not wired up in Aero."))
