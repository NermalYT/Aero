"""Built-in MCP-style tool registry.

Every tool has a JSON-schema signature (sent to the model through llama-server's OpenAI tool
calling), a category that maps to an approval policy (ask / auto / off), and a Python function.
External MCP servers (data/mcp.json) register into the same registry at runtime.
"""
import os
import inspect
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class Tool:
    name: str
    description: str
    params: dict
    category: str
    fn: Callable
    required: list = field(default_factory=list)
    summary: Callable = None   # args -> short human label for the UI
    available: Callable = None  # () -> bool: False hides the tool on this computer (e.g. Windows-only app control)

    def usable(self):
        try:
            return self.available is None or bool(self.available())
        except Exception:
            return False

    @property
    def caps(self):
        from ..capabilities import caps_of
        return caps_of(self.name, self.category)


REGISTRY: dict = {}

CATEGORIES = {
    "files_read": "Read files and folders",
    "files_write": "Create, edit and delete files",
    "shell": "Run terminal commands",
    "screen": "Screenshots and window list",
    "desktop": "Mouse and keyboard control",
    "browser": "Automated web browser",
    "web": "Web search and page fetch",
    "memory": "Long-term memory (remember, recall, forget)",
    "mcp": "External MCP servers (GitHub, plugins, apps)",
    "skills": "Skills (task instructions loaded on demand)",
    "agents": "Subagents (the local model hands part of a task to a fresh copy of itself)",
    "ask": "Questions to you while the rest of the task keeps going",
}


def tool(name, description, category, params=None, required=None, summary=None, available=None):
    def deco(fn):
        REGISTRY[name] = Tool(name, description, params or {}, category, fn, required or [], summary, available)
        return fn
    return deco


class Ctx:
    """Per-call context handed to tools. cancel: the turn's Stop flag (anything with is_set()); long tools check
    cancelled() between steps so Stop takes effect mid-action. owner: who holds resource locks for this call."""
    def __init__(self, settings, vision=False, chat_id=None, cancel=None):
        self.settings = settings
        self.vision = vision
        self.chat_id = chat_id
        self.cancel = cancel
        self.work_dir = Path(os.path.expandvars(os.path.expanduser(settings.get("work_dir") or "~")))

    @property
    def owner(self):
        return self.chat_id or "aero"

    def cancelled(self):
        try:
            return bool(self.cancel is not None and self.cancel.is_set())
        except Exception:
            return False

    def path(self, p):
        p = os.path.expandvars(os.path.expanduser(str(p or ".")))
        q = Path(p)
        return (q if q.is_absolute() else self.work_dir / q).resolve()


def schemas(settings):
    from .. import localonly
    pol = settings.get("tool_policy", {})
    out = []
    for t in REGISTRY.values():
        if pol.get(t.category, "ask") == "off" or not localonly.tool_allowed(t.category, settings) or not t.usable():
            continue
        out.append({"type": "function", "function": {
            "name": t.name, "description": t.description,
            "parameters": {"type": "object", "properties": t.params, "required": t.required}}})
    return out


def policy(name, settings):
    from .. import localonly
    t = REGISTRY.get(name)
    if not t or not localonly.tool_allowed(t.category, settings) or not t.usable():
        return "off"
    return settings.get("tool_policy", {}).get(t.category, "ask")


def label(name, args):
    t = REGISTRY.get(name)
    try:
        if t and t.summary:
            return t.summary(args)
    except Exception:
        pass
    first = next((str(v) for v in (args or {}).values()), "")
    return first[:80]


def run(name, args, ctx):
    """Returns {'text': str, 'image': upload_id | None, 'error': bool, 'envelope': {...}} (see action_results)."""
    t0 = time.monotonic()
    res = _run(name, args, ctx)
    from ..action_results import finish
    t = REGISTRY.get(name)
    return finish(res, name, (time.monotonic() - t0) * 1000, t.caps.side_effect if t else "none")


def _run(name, args, ctx):
    from .. import localonly
    t = REGISTRY.get(name)
    if not t:
        return {"text": f"Unknown tool '{name}'. Available: {', '.join(sorted(REGISTRY))}", "error": True}
    if not t.usable():
        from .. import osinfo
        return {"text": f"{name} doesn't work on this computer ({osinfo.name()}).", "error": True}
    if not localonly.tool_allowed(t.category, ctx.settings):
        return {"text": localonly.refusal(name, ctx.settings), "error": True}
    args = dict(args or {})
    try:
        sig = inspect.signature(t.fn).parameters
        if not any(p.kind == p.VAR_KEYWORD for p in sig.values()):
            args = {k: v for k, v in args.items() if k in sig}     # models sometimes add extra keys (e.g. "description")
    except (TypeError, ValueError):
        pass
    try:
        res = t.fn(ctx, **args)
    except TypeError as e:
        return {"text": f"Bad arguments for {name}: {e}", "error": True}
    except localonly.OfflineBlocked as e:
        return {"text": str(e), "error": True}
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc(limit=3)
        return {"text": f"{type(e).__name__}: {e}\n{tb[-1500:]}", "error": True}
    if isinstance(res, str):
        res = {"text": res}
    res.setdefault("error", False)
    return res


def load_all():
    from . import files, shell, desktop, apps, app_tools, web, browser, memory_tools, skill_tools, agent_tools, ask_tools, mod_tools, doc_tools  # noqa: F401,E501  (registration side effects)
    from . import mcp_client
    mcp_client.start_all_async()
