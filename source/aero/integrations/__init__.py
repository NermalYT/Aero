"""Integrations: other projects' functions built into Aero, grouped into workspaces.

A pack is one integrated project (Flutter, open-code-review, diagram-design, AirLLM, …). It brings:

  tools        registered in Aero's tool registry like any built-in tool (same permissions, evidence, Stop)
  skills       instruction packs in aero/skills_builtin (vendored with their licences where the project is a skill)
  requirements what it needs on this computer (an SDK, a binary, Node, a model) with a check and, where Aero can do
               it, a consent-gated install into Aero's own folder (data/toolchains), never onto the system PATH
  triggers     patterns that say a request needs it, so the router hands the model its tools and the workspace opens
  panel        what its workspace shows (status, results), built from the pack's own state

Workspaces are tabs in the UI (Code, Mobile, Create, Diagrams, Review & test, Debug, Finance, Library, Big models).
A request like "I want to build a mobile app" matches the Mobile packs: the model gets their tools and a line about
what is installed, and the Mobile workspace opens next to the chat. Licences and sources: THIRD_PARTY_NOTICES.md and
docs/V1.2_INTEGRATIONS.md.
"""
import importlib
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

STATE_TTL = 60                 # requirement checks run programs (node --version, git --version): cache them a minute
_states = {}

WORKSPACES = [
    {"id": "code", "name": "Code", "icon": "code", "blurb": "Projects, toolchains, checks and tests for Python, "
     "JavaScript/TypeScript, Rust, Go, C/C++ and CSS"},
    {"id": "mobile", "name": "Mobile apps", "icon": "mobile", "blurb": "Flutter apps for Android, iOS, web and "
     "desktop, with a live preview; iOS builds with xtool"},
    {"id": "create", "name": "Create", "icon": "create", "blurb": "Images made on this PC with stable-diffusion.cpp"},
    {"id": "diagrams", "name": "Diagrams", "icon": "diagram", "blurb": "Editorial diagrams (44 types) as HTML + SVG, "
     "imports from Mermaid, draw.io and Excalidraw, PNG/SVG export"},
    {"id": "review", "name": "Review & test", "icon": "review", "blurb": "Code review with open-code-review, end-to-end "
     "tests and app exploration with e2e"},
    {"id": "debug", "name": "Debug & reverse", "icon": "debug", "blurb": "RAD Debugger tools and reverse engineering "
     "with rea"},
    {"id": "finance", "name": "Finance", "icon": "finance", "blurb": "Models, memos and checks from Anthropic's "
     "financial-services skills (drafts for professional review, never advice or trades)"},
    {"id": "library", "name": "Library", "icon": "library", "blurb": "Searchable awesome-python, awesome-selfhosted "
     "and the-book-of-secret-knowledge"},
    {"id": "memory", "name": "Memory+", "icon": "memory", "blurb": "Hindsight: banks, recall and reflection over what "
     "agents learned"},
    {"id": "models", "name": "Big models", "icon": "models", "blurb": "Run models bigger than your GPU: the best "
     "llama.cpp split for any GGUF, and AirLLM layer streaming for full-precision checkpoints"},
]
WS_IDS = {w["id"] for w in WORKSPACES}


@dataclass
class Requirement:
    id: str
    label: str
    check: Callable                      # () -> (ok: bool, detail: str)
    install: Callable = None             # (emit, cancel) -> detail; None = the user has to do it (see how)
    how: str = ""                        # what the user does when Aero can't install it
    size_mb: int = 0
    optional: bool = False


@dataclass
class Pack:
    id: str
    name: str
    workspace: str
    summary: str
    source: str                          # upstream repository URL
    license: str
    triggers: list = field(default_factory=list)     # regexes (case-insensitive) on the user's request
    tools: list = field(default_factory=list)        # tool names it registers
    skills: list = field(default_factory=list)       # skill names in skills_builtin
    requirements: list = field(default_factory=list)
    panel: Callable = None               # () -> dict for the workspace UI
    notes: str = ""                      # what is not built in, said plainly
    skill_hint: Callable = None          # (text) -> skill names this request needs (packs with many skills)

    def matches(self, text):
        return sum(1 for t in self.triggers if re.search(t, text or "", re.I))

    def state(self, fresh=False):
        hit = _states.get(self.id)
        if hit and not fresh and time.time() - hit[0] < STATE_TTL:
            return hit[1]
        reqs = []
        for r in self.requirements:
            try:
                ok, detail = r.check()
            except Exception as e:  # noqa: BLE001
                ok, detail = False, f"{type(e).__name__}: {e}"
            reqs.append({"id": r.id, "label": r.label, "ok": bool(ok), "detail": detail, "installable": bool(r.install),
                         "how": r.how, "size_mb": r.size_mb, "optional": r.optional})
        ready = all(x["ok"] for x in reqs if not x["optional"])
        st = {"ready": ready, "requirements": reqs}
        _states[self.id] = (time.time(), st)
        return st

    def public(self, with_state=True):
        d = {"id": self.id, "name": self.name, "workspace": self.workspace, "summary": self.summary,
             "source": self.source, "license": self.license, "tools": list(self.tools), "skills": list(self.skills),
             "notes": self.notes}
        if with_state:
            d.update(self.state())
        return d


PACKS = {}
MODULES = ["code", "mobile", "diagrams", "review", "testing", "debug", "finance", "library", "memory", "create",
           "models"]
_loaded = {"done": False}
_lock = threading.Lock()


def register(pack):
    PACKS[pack.id] = pack
    return pack


def load_all():
    """Import every pack module (each registers its packs and tools). Safe to call more than once."""
    with _lock:
        if _loaded["done"]:
            return
        for m in MODULES:
            importlib.import_module(f"{__name__}.{m}")
        _loaded["done"] = True


def requirement(pack_id, req_id):
    p = PACKS.get(pack_id)
    return next((r for r in (p.requirements if p else []) if r.id == req_id), None)


def infer(text, limit=4):
    """Packs a request needs, best first: [(pack, score)]."""
    load_all()
    hits = [(p, p.matches(text)) for p in PACKS.values()]
    hits = [(p, s) for p, s in hits if s > 0]
    hits.sort(key=lambda x: -x[1])
    return hits[:limit]


def context(text, available):
    """For the pipeline: (turn-context line, tools to add, workspace id, pack ids). Deterministic, no model call."""
    hits = infer(text)
    if not hits:
        return "", [], None, []
    lines, want = [], []
    for p, _ in hits:
        st = p.state()
        missing = [r["label"] + (f" ({r['how']})" if not r["installable"] and r["how"] else "")
                   for r in st["requirements"] if not r["ok"] and not r["optional"]]
        tools_here = [t for t in p.tools if t in available]
        want += tools_here
        bit = f"{p.name} ({p.summary})"
        if tools_here:
            bit += ": tools " + ", ".join(tools_here)
        skills = list(p.skills) + (p.skill_hint(text) if p.skill_hint else [])
        if skills:
            bit += "; load the skill" + ("s " if len(skills) > 1 else " ") + ", ".join(skills) + " with use_skill"
            if "use_skill" in available:
                want.append("use_skill")
        if missing:
            bit += ". Not installed yet: " + "; ".join(missing) + (". Offer integration_install, which asks the "
                                                                    "user first." if any(r["installable"] for r in
                                                                                         st["requirements"] if not
                                                                                         r["ok"]) else ".")
        lines.append("- " + bit)
    if "integration_install" in available:
        want.append("integration_install")
    line = "Integrations for this request (built into Aero, shown in the " + \
           WS_NAME.get(hits[0][0].workspace, hits[0][0].workspace) + " workspace):\n" + "\n".join(lines)
    return line, list(dict.fromkeys(want)), hits[0][0].workspace, [p.id for p, _ in hits]


WS_NAME = {w["id"]: w["name"] for w in WORKSPACES}


def overview():
    load_all()
    by = {w["id"]: {**w, "packs": []} for w in WORKSPACES}
    for p in PACKS.values():
        by.setdefault(p.workspace, {"id": p.workspace, "name": p.workspace, "packs": []})["packs"].append(p.public())
    return [by[w["id"]] for w in WORKSPACES]


def install(pack_id, req_id=None, emit=None, cancel=None):
    """Install a pack's missing requirements (or one of them). Returns [(requirement, result text)]."""
    load_all()
    p = PACKS.get(pack_id)
    if not p:
        raise KeyError(pack_id)
    done = []
    for r in p.requirements:
        if req_id and r.id != req_id:
            continue
        ok, _ = r.check()
        if ok:
            continue
        if not r.install:
            if req_id or not r.optional:
                done.append((r.label, f"needs the user: {r.how or 'install it yourself'}"))
            continue
        if r.optional and not req_id:
            continue
        done.append((r.label, r.install(emit, cancel)))
    _states.pop(pack_id, None)
    return done


def panel(ws_id):
    load_all()
    out = {"workspace": next((w for w in WORKSPACES if w["id"] == ws_id), None), "packs": []}
    for p in PACKS.values():
        if p.workspace != ws_id:
            continue
        d = p.public()
        if p.panel:
            try:
                d["panel"] = p.panel()
            except Exception as e:  # noqa: BLE001
                d["panel"] = {"error": f"{type(e).__name__}: {e}"}
        out["packs"].append(d)
    return out


def _register_tools():
    from .. import capabilities
    from ..tools import tool

    @tool("integration_install", "Install what a built-in integration needs (an SDK, a toolchain, a model, a server) "
          "into Aero's own folder: e.g. pack='flutter', 'open-code-review', 'e2e', 'raddebugger', 'rea', 'image', "
          "'hindsight', 'bigmodels', 'awesome-selfhosted', 'essentials' with requirement='node'. Asks the user first; "
          "sizes are in the integration's description.", "setup",
          {"pack": {"type": "string"}, "requirement": {"type": "string"}}, ["pack"],
          summary=lambda a: f"{a.get('pack')} {a.get('requirement') or ''}".strip())
    def integration_install(ctx, pack, requirement=None):
        from . import toolchain
        try:
            done = install(pack, requirement, cancel=ctx.cancel)
        except KeyError:
            return {"text": f"No integration '{pack}'. Known: {', '.join(sorted(PACKS))}.", "error": True}
        except toolchain.Offline as e:
            return {"text": str(e), "error": True}
        except Exception as e:  # noqa: BLE001
            return {"text": f"Installing {pack} failed: {e}", "error": True}
        st = PACKS[pack].state(fresh=True)
        lines = [f"- {label}: {res}" for label, res in done] or ["Everything it needs was already installed."]
        lines.append("Ready." if st["ready"] else "Still missing: " + ", ".join(
            r["label"] for r in st["requirements"] if not r["ok"] and not r["optional"]))
        return {"text": "\n".join(lines), "error": not st["ready"] and not done}

    capabilities.register("integration_install", ["integration.install"], "local_write", "internet", cost="high")


_register_tools()
