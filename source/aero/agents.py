"""Agents and subagents: who is working on what, for the dashboard and for talking to one directly.

An agent is the local model working on one chat's task. It gets a short job title that says what it does ("Photo
Renamer"): at once from the request's wording, then a better one from the model with the chat title. An agent can
hand a self-contained part of its task to a subagent (the run_subagent tool): a fresh copy of the local model with
its own context, which works with the same tools and reports back.

Live state comes from the running turns (note() sees every UI event); finished agents and their subagents are read
from the saved chats. A chat started from a subagent row talks to that subagent: its system prompt carries the
subagent's task, its work and its report (persona()).
"""
import json
import re
import threading
import time

from .config import CHATS

LIVE = {}               # chat id -> live agent record
_lock = threading.Lock()
_files = {}             # chat file path -> (mtime, summary)
KEEP_FINISHED_S = 1800  # finished live records stay this long (the saved chat has the rest)

# verb in a request -> the job title of whoever does it
_ROLE = {
    "rename": "Renamer", "find": "Finder", "search": "Searcher", "look": "Viewer", "fix": "Fixer", "repair": "Fixer",
    "debug": "Debugger", "write": "Writer", "draft": "Writer", "read": "Reader", "check": "Checker", "verify": "Checker",
    "summarize": "Summarizer", "summarise": "Summarizer", "tidy": "Organizer", "clean": "Cleaner", "organize": "Organizer",
    "organise": "Organizer", "sort": "Sorter", "build": "Builder", "make": "Maker", "create": "Creator", "test": "Tester",
    "analyze": "Analyst", "analyse": "Analyst", "research": "Researcher", "install": "Installer", "download": "Downloader",
    "move": "Mover", "copy": "Copier", "delete": "Cleaner", "remove": "Cleaner", "review": "Reviewer", "edit": "Editor",
    "update": "Updater", "upgrade": "Updater", "translate": "Translator", "compare": "Comparer", "plan": "Planner",
    "monitor": "Monitor", "watch": "Monitor", "explain": "Explainer", "convert": "Converter", "scan": "Scanner",
    "count": "Counter", "list": "Lister", "open": "Opener", "refactor": "Refactorer", "optimize": "Optimizer",
    "optimise": "Optimizer", "tune": "Tuner", "benchmark": "Benchmarker", "schedule": "Scheduler", "backup": "Backup Agent",
    "back": "Backup Agent", "deploy": "Deployer", "configure": "Configurator", "set": "Configurator", "send": "Sender",
    "reply": "Responder", "answer": "Responder", "calculate": "Calculator", "measure": "Meter", "generate": "Generator",
    "design": "Designer", "document": "Documenter", "label": "Labeler", "tag": "Tagger", "merge": "Merger",
    "extract": "Extractor", "parse": "Parser", "collect": "Collector", "gather": "Collector", "fetch": "Fetcher",
    "catch": "Catch-up Agent", "inspect": "Inspector", "audit": "Auditor", "diagnose": "Diagnostician",
}
_STOP = {"the", "my", "a", "an", "all", "every", "each", "these", "this", "that", "those", "some", "any", "our", "your",
         "me", "it", "them", "up", "out", "please", "now", "just", "and", "then", "also", "of", "at"}
_ASK = {"why", "what", "whats", "how", "hows", "when", "where", "who", "which", "is", "are", "was", "were", "can", "could",
        "should", "would", "will", "do", "does", "did", "i", "im", "ive", "hey", "hi", "hello", "yo", "help", "tell", "show",
        "give", "need", "want", "get", "got", "have", "has", "there", "you", "u", "pls", "ok", "okay", "so", "much", "many",
        "best", "good", "new", "really", "very", "not", "dont", "doesnt", "isnt", "cant", "wont", "keeps", "keep", "be"}
_PREP = {"to", "in", "on", "from", "with", "for", "into", "at", "by", "so", "and", "then", "that", "which", "about",
         "using", "via", "inside", "under", "over", "if", "when", "because", "as", "than", "but", "or"}


def clean_name(s, limit=32):
    """A model- or user-supplied agent name, cleaned to a short title ("photo renamer" -> "Photo Renamer")."""
    s = str(s or "").strip()
    s = re.sub(r"^\W*(name|agent|title)\W*:\s*", "", s.splitlines()[0] if s else "", flags=re.I)
    s = re.sub(r"\s+", " ", re.sub(r"[^\w\s&+#./-]|_", "", s)).strip()
    s = " ".join(s.split(" ")[:4])[:limit].strip(" .,:;-")
    if s and s == s.lower():
        s = s.title()
    return s


def _singular(w):
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def name_from_text(text):
    """A job title from a request's own words, used until the model names the agent: "Rename the .jpeg photos to
    .jpg" -> "Photo Renamer", "Tidy my Desktop" -> "Desktop Organizer"."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9+#-]*", (text or "")[:300])
    low = [w.lower() for w in words]
    verb_i = next((i for i, w in enumerate(low[:6]) if w in _ROLE), None)
    if verb_i is None:
        obj = next((w for w in words[:10] if w.lower() not in _STOP and w.lower() not in _PREP
                    and w.lower() not in _ASK and len(w) > 2), "")
        if not obj:
            return "Task Agent"
        obj = obj if obj.isupper() else obj[:1].upper() + obj[1:]
        return obj + (" Helper" if low and low[0] in _ASK else " Agent")
    role = _ROLE[low[verb_i]]
    obj = ""
    for w, lw in zip(words[verb_i + 1:verb_i + 7], low[verb_i + 1:verb_i + 7]):
        if lw in _PREP:
            if obj:
                break
            continue
        if lw in _STOP or len(lw) < 2:
            continue
        obj = w
    if not obj or role.endswith("Agent"):
        return role if role.endswith("Agent") else role + (" Agent" if " " not in role else "")
    obj = _singular(obj)
    obj = obj if obj.isupper() else obj[:1].upper() + obj[1:]
    return f"{obj} {role}"


# ---------------------------------------------------------------------------- live state

_DOING = {
    "list_dir": "Listing", "read_file": "Reading", "write_file": "Writing", "edit_file": "Editing", "move_path": "Moving",
    "delete_path": "Deleting", "find_files": "Finding files", "search_files": "Searching files", "run_command": "Running",
    "web_search": "Searching the web", "fetch_url": "Reading a page", "screenshot": "Looking at the screen",
    "list_windows": "Listing windows", "focus_window": "Switching windows", "open_app": "Opening", "wait": "Waiting",
    "mouse_click": "Clicking", "mouse_move": "Moving the mouse", "mouse_drag": "Dragging", "scroll": "Scrolling",
    "type_text": "Typing", "press_keys": "Pressing keys", "app_list": "Listing apps", "app_view": "Looking at",
    "app_click": "Clicking in", "app_type": "Typing in", "app_keys": "Pressing keys in", "app_scroll": "Scrolling",
    "app_read": "Reading", "browser_open": "Opening", "browser_snapshot": "Reading the page",
    "browser_click": "Clicking", "browser_type": "Typing", "browser_select": "Choosing", "browser_press": "Pressing",
    "browser_scroll": "Scrolling", "browser_back": "Going back", "browser_screenshot": "Looking at the page",
    "browser_read": "Reading the page", "recall": "Recalling", "remember": "Remembering", "use_skill": "Loading skill",
    "run_subagent": "Waiting for", "load_tools": "Loading tools",
}


def _clip(s, n):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def doing_text(name, label):
    verb = _DOING.get(name) or name.replace("_", " ").capitalize()
    return _clip(f"{verb} {label}".strip() if label else verb, 140)


def summary_of(text, n=160):
    """The first real sentence or line of a reply, without markdown."""
    t = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    t = re.sub(r"[#>*_`|]+", "", t)
    for line in t.splitlines():
        line = line.strip(" -•\t")
        if len(line) > 3:
            return _clip(line, n)
    return ""


def start(chat_id, name, title, model, task, meta=None):
    meta = meta or {}
    now = time.time()
    with _lock:
        old = LIVE.get(chat_id) or {}
        if old.get("named") and not meta.get("named"):     # the model already named it (the title call can finish first)
            name, meta = old["name"], {**meta, "named": True}
        LIVE[chat_id] = {"id": chat_id, "name": name or old.get("name") or "Agent", "title": title or old.get("title") or "",
                         "task": _clip(task, 300), "model": model, "status": "working", "doing": "Starting",
                         "started": now, "updated": now, "subs": dict(old.get("subs") or {}), "kind": meta.get("kind") or "agent",
                         "parent": meta.get("parent"), "sub_id": meta.get("sub_id"), "named": bool(meta.get("named"))}
        _prune(now)


def set_name(chat_id, name):
    name = clean_name(name)
    if not name:
        return
    with _lock:
        a = LIVE.get(chat_id)
        if a and a.get("kind") != "subagent":
            a["name"], a["named"] = name, True


def _prune(now):
    for k in [k for k, a in LIVE.items() if a["status"] not in ("working", "waiting") and now - a["updated"] > KEEP_FINISHED_S]:
        LIVE.pop(k, None)
    while len(LIVE) > 60:
        LIVE.pop(min(LIVE, key=lambda k: LIVE[k]["updated"]))


def note(chat_id, ev):
    """Update a live agent from one UI event of its turn."""
    t = ev.get("t")
    with _lock:
        a = LIVE.get(chat_id)
        if not a:
            return
        if t == "compacted" and ev.get("new_chat_id"):
            LIVE[ev["new_chat_id"]] = {**a, "id": ev["new_chat_id"]}
            LIVE.pop(chat_id, None)
            return
        a["updated"] = time.time()
        sid = ev.get("sub")
        if t == "subagent_start":
            s = ev["sub"]
            a["subs"][s["id"]] = {"id": s["id"], "name": s["name"], "task": _clip(s.get("task"), 300), "status": "working",
                                  "doing": "Starting", "updated": a["updated"]}
            a["doing"] = f"Waiting for {s['name']}"
            return
        if t == "subagent_done":
            s = ev["sub"]
            sub = a["subs"].setdefault(s["id"], {"id": s["id"], "name": s["name"], "task": _clip(s.get("task"), 300)})
            sub.update(status=s.get("status") or "done", doing="", result=summary_of(s.get("result")), updated=a["updated"])
            return
        target = a["subs"].get(sid) if sid else a
        if target is None:
            return
        target["updated"] = a["updated"]
        if t == "router":
            intent = ((ev.get("message") or {}).get("decision") or {}).get("intent")
            if intent:
                a["intent"] = _clip(intent, 140)
            a["doing"] = "Planning"
        elif t == "lane" and not sid:
            lane, phase, model = ev.get("lane"), ev.get("phase"), ev.get("model") or ""
            if lane == "local":
                a["doing"] = {"fix": "Fixing what the review found", "lesson": "Learning from the review"}.get(phase, "Working")
            elif phase == "review":
                a["doing"] = f"{model or 'The reviewer'} is checking the work"
            elif phase == "execute":
                a["doing"] = f"{model or 'The cloud model'} is redoing the task"
        elif t == "assistant_start":
            target["doing"] = "Thinking"
        elif t == "content" and target.get("doing") != "Writing the answer":
            target["doing"] = "Writing the answer"
        elif t == "tool_start":
            label = ev.get("label") or ""
            if ev.get("name") == "run_subagent":
                label = label.split(":")[0]
            target["doing"] = doing_text(ev.get("name"), label)
            if ev.get("needs_approval"):
                target["status"] = "waiting"
                target["doing"] = "Waiting for your approval: " + target["doing"]
        elif t == "tool_result":
            if target.get("status") == "waiting":
                target["status"] = "working"
        elif t == "control":
            a["controlling"] = {"by": ev.get("by"), "target": ev.get("target")}
        elif t == "review" and not sid:
            m = ev.get("message") or {}
            a["doing"] = f"Review by {m.get('model') or 'the reviewer'}: {m.get('verdict')}"
        elif t == "error":
            target["status"] = "error"
            target["doing"] = _clip(ev.get("error"), 140)


def finish(chat_id, status, summary=""):
    with _lock:
        a = LIVE.get(chat_id)
        if not a:
            return
        a.update(status=status, doing="", summary=summary_of(summary), updated=time.time(), controlling=None)
        for s in a["subs"].values():
            if s.get("status") in ("working", "waiting"):
                s.update(status="stopped", doing="")


def busy():
    with _lock:
        return [dict(a) for a in LIVE.values() if a["status"] in ("working", "waiting")]


# ---------------------------------------------------------------------------- saved chats

def _final_status(msgs):
    for m in reversed(msgs):
        r = m.get("role")
        if r == "notice" and m.get("error"):
            return "error"
        if r == "assistant":
            return "stopped" if (m.get("stats") or {}).get("finish") == "stopped" else "done"
        if r in ("user",) and m.get("from") not in ("fable", "astra"):
            return "idle"
    return "idle"


def _chat_summary(p):
    try:
        mt = p.stat().st_mtime
    except OSError:
        return None
    hit = _files.get(str(p))
    if hit and hit[0] == mt:
        return hit[1]
    try:
        c = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    msgs = c.get("messages") or []
    meta = c.get("agent") or {}
    first = next((m for m in msgs if m.get("role") == "user" and m.get("from") not in ("fable", "astra")), {})
    task = first.get("content") or ""
    final = next((m.get("content") for m in reversed(msgs) if m.get("role") == "assistant" and not m.get("tool_calls")
                  and (m.get("content") or "").strip() and (m.get("lane") or "local") in ("local", "opus", "sol")), "")
    subs = [{"id": m.get("id"), "name": m.get("name") or "Subagent", "task": _clip(m.get("task"), 300),
             "status": m.get("status") or "done", "result": summary_of(m.get("result")),
             "steps": sum(1 for x in m.get("messages") or [] if x.get("role") == "tool"), "updated": m.get("ended") or m.get("ts")}
            for m in msgs if m.get("role") == "subagent"]
    s = {"id": c.get("id") or p.stem, "name": clean_name(meta.get("name")) or name_from_text(task or c.get("title")),
         "title": c.get("title") or "", "task": _clip(task, 300), "kind": meta.get("kind") or "agent",
         "parent": meta.get("parent"), "sub_id": meta.get("sub_id"), "status": _final_status(msgs),
         "summary": summary_of(final), "subs": subs, "updated": c.get("updated") or mt, "empty": not msgs}
    _files[str(p)] = (mt, s)
    if len(_files) > 400:
        _files.pop(next(iter(_files)))
    return s


_recent = {"t": 0.0, "files": []}


def _recent_chats(n=60, ttl=4.0):
    """The newest chat files. The dashboard asks every 1.5 s; with thousands of chats, listing the folder that often
    would cost more than the rest of the stats call, so the list is reused for a few seconds."""
    now = time.time()
    if now - _recent["t"] > ttl:
        def mtime(p):
            try:
                return p.stat().st_mtime
            except OSError:
                return 0
        try:
            _recent["files"] = sorted(CHATS.glob("*.json"), key=mtime, reverse=True)[:n]
        except OSError:
            _recent["files"] = []
        _recent["t"] = now
    return _recent["files"]


def listing(limit=8):
    """Agents for the dashboard, working ones first, each with its subagents. A subagent that has its own chat
    (someone talked to it) carries that chat's id in "chat"."""
    files = _recent_chats()
    rows = {}
    for p in files:
        s = _chat_summary(p)
        if s and not s["empty"]:
            rows[s["id"]] = {**s, "subs": [dict(x) for x in s["subs"]]}
    with _lock:
        live = {k: dict(v, subs={i: dict(x) for i, x in v["subs"].items()}) for k, v in LIVE.items()}
    for cid, a in live.items():
        r = rows.setdefault(cid, {"id": cid, "name": a["name"], "title": a.get("title") or "", "task": a.get("task") or "",
                                  "kind": a.get("kind") or "agent", "parent": a.get("parent"), "sub_id": a.get("sub_id"),
                                  "subs": [], "summary": "", "updated": a["updated"]})
        if a.get("named") or not r.get("name"):
            r["name"] = a["name"]
        r.update(status=a["status"], doing=a.get("doing") or "", intent=a.get("intent") or "", model=a.get("model"),
                 updated=max(r.get("updated") or 0, a["updated"]), controlling=a.get("controlling"), live=True)
        if a.get("summary"):
            r["summary"] = a["summary"]
        by_id = {s["id"]: s for s in r["subs"]}
        for sid, s in a["subs"].items():
            if sid in by_id:
                if s.get("status") in ("working", "waiting") or not by_id[sid].get("status"):
                    by_id[sid].update(status=s["status"], doing=s.get("doing") or "")
            else:
                r["subs"].append({**s, "steps": 0})
    sub_chats = {}
    for r in rows.values():
        if r["kind"] == "subagent" and r.get("parent") and r.get("sub_id"):
            prev = sub_chats.get((r["parent"], r["sub_id"]))
            if not prev or (r.get("updated") or 0) > (prev.get("updated") or 0):
                sub_chats[(r["parent"], r["sub_id"])] = r
    agents = [r for r in rows.values() if r["kind"] != "subagent"]
    agents.sort(key=lambda r: (r.get("status") not in ("working", "waiting"), -(r.get("updated") or 0)))
    out = agents[:limit]
    for a in out:
        for s in a["subs"]:
            ch = sub_chats.get((a["id"], s["id"]))
            if ch:
                s["chat"] = ch["id"]
                if ch.get("status") in ("working", "waiting"):
                    s.update(status=ch["status"], doing=ch.get("doing") or "")
    return out


# ---------------------------------------------------------------------------- talking to a subagent

SUB_PERSONA = (
    "# Your role: subagent \"{name}\"\n"
    "You are {name}, a subagent: the main agent working on the user's task handed you one self-contained part of it. "
    "Do only that part, with your tools, and check your results. When you are done, reply with a short report of "
    "what you did, what you found and anything that failed. The main agent sees only that final reply, not your "
    "steps, so put every fact it needs in it.")

CHAT_PERSONA = (
    "# You are talking to the user as subagent \"{name}\"\n"
    "Earlier, the main agent \"{parent}\" (working on: {parent_task}) started you, {name}, as a subagent with this "
    "task:\n{task}\n\nWhat you did then:\n{work}\n\nYour report was:\n{result}\n\n"
    "The user opened a chat with you directly to ask about that work or to have you continue it. Answer as {name}, "
    "from that work; use your tools again when the user wants more done.")


def persona(meta):
    """The system-prompt block for a chat with a subagent (meta: the chat's "agent" field)."""
    name = clean_name(meta.get("name")) or "Subagent"
    from .cloud import _clip as clip, _render_work
    parent = {}
    try:
        parent = json.loads((CHATS / (re.sub(r"[^\w-]", "", meta.get("parent") or "") + ".json")).read_text(encoding="utf-8"))
    except Exception:
        pass
    rec = next((m for m in parent.get("messages") or [] if m.get("role") == "subagent" and m.get("id") == meta.get("sub_id")), None)
    if not rec:
        return SUB_PERSONA.format(name=name) + ("\n\nThe chat this subagent came from is no longer saved, so its "
                                                 "earlier work is not available.")
    pmeta = parent.get("agent") or {}
    first = next((m for m in parent.get("messages") or [] if m.get("role") == "user"), {})
    return CHAT_PERSONA.format(
        name=name, parent=clean_name(pmeta.get("name")) or name_from_text(first.get("content") or parent.get("title")),
        parent_task=clip(first.get("content") or parent.get("title") or "", 600), task=clip(rec.get("task"), 3000),
        work=clip(_render_work(rec.get("messages") or [], per_tool=700) or "(no steps recorded)", 12000),
        result=clip(rec.get("result") or "(no report)", 4000))
