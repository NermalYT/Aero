"""What each tool can do, what it touches and how it is controlled: typed metadata on top of the tool registry.

The registry (tools/__init__.py) knows a tool's name, JSON schema and permission category. This module adds what the
rest of Aero needs to choose and run tools safely:

  capabilities   what the tool achieves ("app.launch", "browser.read", "email.read" ...), so a request can be routed by
                 what it needs rather than by tool names
  side_effect    none | local_read | local_write | local_app_write | external_read | external_write | physical_input
                 | destructive: drives approvals, retries (verify-before-repeat) and what may run in parallel
  network        local | internet: Local Only and strict offline already hide network categories; this is the per-tool
                 record of it
  background     True when the tool works without the user's foreground window, mouse or keyboard
  physical       True when the tool moves the user's real mouse or types on the real keyboard (pyautogui); those calls
                 go through input_guard (consent, idle wait, exclusive lock) or are refused under Strict Background Only
  locks          resource kinds the call must own while it runs ("window", "browser_tab", "physical_input",
                 "clipboard", "file")
  verification   how the result can be checked from a source of truth

Control modes say honestly how an app is being driven (see tools/apps.py). FOREGROUND_CONSENT_REQUIRED is never shown
as background work.
"""
from dataclasses import asdict, dataclass

SIDE_EFFECTS = ("none", "local_read", "local_write", "local_app_write", "external_read", "external_write",
                "physical_input", "destructive")
# side effects that must not be repeated blindly after a timeout (verify first)
NON_IDEMPOTENT = {"external_write", "destructive", "local_app_write", "physical_input"}

CONTROL_MODES = {
    "API_BACKGROUND": "a structured API changes the app's data; the app is not touched on screen",
    "ACCESSIBILITY_BACKGROUND": "UI Automation patterns act on controls directly; your mouse and keyboard are untouched",
    "BROWSER_ISOLATED": "Aero's own browser profile in its own process; not your browser window",
    "WINDOW_MESSAGE_BACKGROUND": "window messages sent to the app without focus; the app may ignore them, so results "
                                 "are read back",
    "ISOLATED_SESSION": "a separate OS session or VM that the user configured",
    "FOREGROUND_CONSENT_REQUIRED": "needs your real mouse or keyboard: Aero asks first, waits until you stop typing, "
                                   "then hands focus back",
    "OBSERVE_ONLY": "Aero can read this window but cannot act on it safely",
    "UNSUPPORTED": "no safe way to control this target",
}


@dataclass(frozen=True)
class ToolCaps:
    capabilities: tuple = ()
    side_effect: str = "none"
    network: str = "local"
    background: bool = True
    physical: bool = False
    locks: tuple = ()
    verification: str = ""
    cancellable: bool = True
    overhead: str = "low"            # low | medium | high: rough cost per call (time, tokens, CPU)

    def public(self):
        d = asdict(self)
        d["capabilities"] = list(self.capabilities)
        d["locks"] = list(self.locks)
        return d


def _c(caps, side="none", net="local", bg=True, phys=False, locks=(), verify="", cancel=True, cost="low"):
    return ToolCaps(tuple(caps), side, net, bg, phys, tuple(locks), verify, cancel, cost)


TOOL_CAPS = {
    # files
    "list_dir": _c(["filesystem.search"], "local_read"),
    "read_file": _c(["filesystem.read", "document.read", "spreadsheet.read"], "local_read"),
    "find_files": _c(["filesystem.search"], "local_read"),
    "search_files": _c(["filesystem.search", "document.read"], "local_read"),
    "write_file": _c(["filesystem.modify", "document.create"], "local_write", locks=["file"],
                     verify="re-read the written file"),
    "edit_file": _c(["filesystem.modify", "document.edit"], "local_write", locks=["file"], verify="re-read the edited part"),
    "move_path": _c(["filesystem.modify"], "local_write", locks=["file"], verify="destination exists, source gone"),
    "delete_path": _c(["filesystem.modify"], "destructive", locks=["file"], verify="path no longer exists"),
    "meeting_doc": _c(["document.create", "calendar.read"], "local_write", locks=["file"],
                      verify="reopen the .docx and count the meetings in it"),
    # shell
    "run_command": _c(["terminal.execute"], "local_write", cancel=False, cost="medium", verify="exit code and output"),
    # screen and desktop (shared physical input)
    "screenshot": _c(["screen.observe"], "none", cost="medium"),
    "list_windows": _c(["app.inspect"], "none"),
    "wait": _c(["screen.observe"], "none"),
    "mouse_click": _c(["screen.physical_input"], "physical_input", bg=False, phys=True, locks=["physical_input"]),
    "mouse_move": _c(["screen.physical_input"], "physical_input", bg=False, phys=True, locks=["physical_input"]),
    "mouse_drag": _c(["screen.physical_input"], "physical_input", bg=False, phys=True, locks=["physical_input"]),
    "scroll": _c(["screen.physical_input"], "physical_input", bg=False, phys=True, locks=["physical_input"]),
    "type_text": _c(["screen.physical_input"], "physical_input", bg=False, phys=True,
                    locks=["physical_input", "clipboard"]),
    "press_keys": _c(["screen.physical_input"], "physical_input", bg=False, phys=True, locks=["physical_input"]),
    "focus_window": _c(["app.interact"], "physical_input", bg=False, phys=True, locks=["physical_input"]),
    "open_app": _c(["app.launch"], "local_app_write", verify="a new process or window of the app appears"),
    # apps (Windows UI Automation and window messages)
    "app_find": _c(["app.discover", "app.inspect"], "none"),
    "app_launch": _c(["app.launch"], "local_app_write", verify="a new process or window of the app appears"),
    "app_list": _c(["app.inspect"], "none"),
    "app_view": _c(["app.inspect", "app.read", "screen.observe"], "none", cost="medium"),
    "app_read": _c(["app.read", "document.read"], "none"),
    "app_click": _c(["app.interact"], "local_app_write", locks=["window"],
                    verify="UI Automation state read back, or the window picture compared before and after"),
    "app_type": _c(["app.interact", "document.edit"], "local_app_write", locks=["window"],
                   verify="the field's value read back"),
    "app_keys": _c(["app.interact"], "local_app_write", locks=["window"], verify="the window picture compared"),
    "app_scroll": _c(["app.interact"], "none", locks=["window"]),
    # web and browser (internet)
    "web_search": _c(["browser.search"], "external_read", "internet"),
    "fetch_url": _c(["browser.read", "browser.extract"], "external_read", "internet"),
    "browser_open": _c(["browser.navigate", "browser.read"], "external_read", "internet", locks=["browser_tab"],
                       cost="medium"),
    "browser_snapshot": _c(["browser.read"], "none", "internet"),
    "browser_read": _c(["browser.read"], "none", "internet"),
    "browser_read_sections": _c(["browser.read", "browser.extract"], "none", "internet"),
    "browser_extract": _c(["browser.extract"], "none", "internet"),
    "browser_tabs": _c(["browser.tabs"], "none", "internet", locks=["browser_tab"]),
    "browser_wait_for": _c(["browser.read"], "none", "internet"),
    "browser_click": _c(["browser.interact"], "external_write", "internet", locks=["browser_tab"],
                        verify="page state after the click"),
    "browser_type": _c(["browser.interact"], "external_write", "internet", locks=["browser_tab"],
                       verify="the field's value read back"),
    "browser_select": _c(["browser.interact"], "external_write", "internet", locks=["browser_tab"]),
    "browser_press": _c(["browser.interact"], "external_write", "internet", locks=["browser_tab"]),
    "browser_scroll": _c(["browser.read"], "none", "internet", locks=["browser_tab"]),
    "browser_back": _c(["browser.navigate"], "none", "internet", locks=["browser_tab"]),
    "browser_screenshot": _c(["browser.read", "screen.observe"], "none", "internet", cost="medium"),
    # memory, skills, agents, questions
    "remember": _c(["memory.learn"], "local_write"),
    "recall": _c(["memory.recall"], "local_read"),
    "forget": _c(["memory.learn"], "local_write"),
    "use_skill": _c(["skills.load"], "local_read"),
    "run_subagent": _c(["agents.delegate"], "none", cost="high"),
    "ask_user": _c(["user.ask"], "none"),
    "get_answer": _c(["user.ask"], "none"),
    "mod_check": _c(["mods.check"], "local_read", cost="high"),
}

# defaults for tools this table does not list (MCP tools, tools added by mods)
_BY_CATEGORY = {
    "mcp": _c(["mcp.call"], "external_write", "internet", cost="medium"),
    "web": _c(["browser.read"], "external_read", "internet"),
    "browser": _c(["browser.interact"], "external_write", "internet", locks=["browser_tab"]),
    "desktop": _c(["screen.physical_input"], "physical_input", bg=False, phys=True, locks=["physical_input"]),
    "screen": _c(["screen.observe"], "none"),
    "files_read": _c(["filesystem.read"], "local_read"),
    "files_write": _c(["filesystem.modify"], "local_write", locks=["file"]),
    "shell": _c(["terminal.execute"], "local_write", cancel=False),
    "memory": _c(["memory.recall"], "local_read"),
}
_UNKNOWN = _c([], "external_write")
# MCP tools whose name says they only read
_READ_WORDS = ("get", "list", "search", "read", "fetch", "find", "query", "view", "show", "describe", "status")


def caps_of(name, category=None):
    """ToolCaps for a tool name (registry category used for tools not in the table)."""
    if name in TOOL_CAPS:
        return TOOL_CAPS[name]
    if category is None:
        from . import tools
        t = tools.REGISTRY.get(name)
        category = t.category if t else None
    base = _BY_CATEGORY.get(category, _UNKNOWN)
    if category == "mcp":
        tail = name.split("_", 2)[-1].lower()
        if tail.startswith(_READ_WORDS):
            return ToolCaps(base.capabilities, "external_read", base.network, True, False, (), "", True, base.overhead)
    return base


def tools_for(capability, available=None):
    """Tool names that provide a capability, in registry order (stable for prompt caching)."""
    from . import tools
    names = [n for n, t in tools.REGISTRY.items() if capability in caps_of(n, t.category).capabilities]
    if available is not None:
        av = set(available)
        names = [n for n in names if n in av]
    return names


def is_physical(name, args=None):
    """True when this call would move the user's real mouse or type on the real keyboard."""
    args = args or {}
    c = caps_of(name)
    if c.physical:
        return True
    if name in ("app_click", "app_type", "app_keys", "app_scroll") and str(args.get("input") or "").lower() == "real":
        return True
    if name == "app_keys":                     # ctrl/alt/shift/win chords only work with the real keyboard
        mods = {"ctrl", "control", "alt", "shift", "win", "windows", "cmd"}
        return any(set(k.lower() for k in ch.split("+")) & mods for ch in str(args.get("keys") or "").split())
    return False


def read_only(name, args=None):
    """Safe to run alongside other read-only calls: no side effect, no locks."""
    c = caps_of(name)
    return c.side_effect in ("none", "local_read", "external_read") and not c.locks and not is_physical(name, args)


def public(name, category=None):
    return caps_of(name, category).public()


# ---- capability hints: a safety net under the router ------------------------------------------------------------
# The CPU router (a 1-3B model) often returns no tools for plain action requests (measured: 17 of 50 benchmark
# requests, docs/V1.1_PERFORMANCE_REPORT.md). These patterns name the capability a request plainly needs; the tools
# that provide it are added to the router's pick. They only ever add tools (the model can still load_tools), cost no
# model call, and app names are handled separately by app_registry.mentions.
import re as _re  # noqa: E402

HINTS = [
    (r"\b(remember (that|this|my|i)|don'?t forget|keep in mind|note that|save (this|that) to memory)\b",
     ["memory.learn"]),
    (r"\b(yesterday|last (time|week|chat|session)|earlier (chat|today|conversation)|we (talked|discussed|spoke)|you "
     r"(said|told me)|do you remember|remember when)\b", ["memory.recall"]),
    (r"(https?://\S+|\b(browser|web ?page|website|web site|this site|the site|tab|sign ?in to|log ?in to|scroll (down|up)"
     r"|go back|previous page|the form|on the page)\b)", ["browser.navigate", "browser.read", "browser.interact"]),
    (r"\b(click|press|tap|type|toggle|tick|untick|select)\b.{0,60}\b(button|field|box|window|menu|dialog|tab|app|"
     r"checkbox)\b", ["app.inspect", "app.interact", "app.read"]),
    (r"^\s*(please\s+)?(open|start|launch|run|fire up|boot up)\s+(?!the\s+(file|folder|tests?))\w", ["app.launch", "app.discover"]),
    (r"\b(e-?mails?|inbox|mailbox|gmail|outlook)\b", ["email.read", "browser.navigate"]),
    (r"\b(meetings?|calendar|schedule)\b.{0,80}\b(doc|document|report|summary|list)\b|\b(doc|document)\b.{0,80}"
     r"\bmeetings?\b", ["document.create", "calendar.read"]),
    (r"\b(organi[sz]e|sort|tidy|clean up|move|rename|delete|remove|archive)\b.{0,60}\b(files?|folders?|desktop|"
     r"downloads|documents|photos|pictures)\b", ["filesystem.search", "filesystem.modify"]),
    (r"\b(run|re-?run)\b.{0,30}\btests?\b|\b(pip|npm|git|winget|apt|brew)\b", ["terminal.execute"]),
    (r"\b(find|look up|search( for)?|recommend|suggest|what'?s new|latest)\b.{0,50}\b(game|experience|song|playlist|"
     r"video|article|news|review|price|place|restaurant|release)s?\b", ["browser.search"]),
]
_HINTS = [(_re.compile(p, _re.I), caps) for p, caps in HINTS]
# what each hinted capability brings, in addition to the router's pick (the most useful few, not everything)
HINT_TOOLS = {
    "memory.learn": ["remember"], "memory.recall": ["recall"],
    "browser.navigate": ["browser_open", "browser_back"], "browser.read": ["browser_snapshot", "browser_read_sections"],
    "browser.interact": ["browser_click", "browser_type", "browser_scroll"],
    "app.inspect": ["app_list", "app_view"], "app.interact": ["app_click", "app_type", "app_keys"], "app.read": ["app_read"],
    "app.launch": ["app_launch", "open_app"], "app.discover": ["app_find"],
    "email.read": [], "document.create": ["meeting_doc", "write_file"], "calendar.read": [],
    "filesystem.search": ["list_dir", "find_files"], "filesystem.modify": ["move_path", "write_file", "delete_path"],
    "terminal.execute": ["run_command"], "browser.search": ["web_search", "fetch_url"],
}


def hint_tools(text, available):
    """Tools to add for the capabilities a request plainly needs (registry order). MCP tools whose names mention mail
    come with email requests."""
    av = list(available or [])
    want = []
    for rx, caps in _HINTS:
        if rx.search(text or ""):
            for c in caps:
                want += HINT_TOOLS.get(c, [])
                if c == "email.read":
                    want += [n for n in av if n.startswith("mcp_") and ("mail" in n.lower() or "gmail" in n.lower())]
    s = set(want)
    return [n for n in av if n in s]
