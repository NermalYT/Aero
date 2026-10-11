"""The local agent loop: stream a reply from llama-server, execute tool calls (with approval), repeat.

Token economics that shape this file:
- The system prompt is byte-for-byte stable for the whole chat (no clock, no per-turn memory), so
  llama-server's prompt cache can reuse it and every earlier turn; only new tokens are read each step.
- Per-turn context (time, the router's plan, recalled memories) rides on the user message instead.
  Time and plan are stored with the turn, so they render identically on later turns; recalled memory
  only accompanies the newest message.
- The router picks which tool schemas the model sees. Tool sets only grow within a chat ("sticky"),
  so the cached prefix survives from one turn to the next as long as nothing new is needed.
"""
import asyncio
import copy
import difflib
import getpass
import json
import re
import time
import uuid
from pathlib import Path

import httpx

from . import (action_results, agents, attachments, capabilities, clarifications, control, input_guard, memory,
               osinfo, resources, stats, task_graph, tools)

_approvals = {}        # call_id -> Future
_approval_chat = {}    # call_id -> chat id it belongs to (Stop in one chat must not answer another chat's cards)
_chat_allow = {}       # chat_id -> set(categories) allowed for the rest of the chat
_cancel = {}           # chat_id -> asyncio.Event

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
WRITE_TOOLS = {"write_file", "edit_file", "move_path", "delete_path"}
LOOP_TOOLS = {"run_subagent", "load_tools", "ask_user", "get_answer"}     # run by the loop itself, not tools.run
PARALLEL_MAX = 4               # read-only tool calls from one step that may run at the same time
TIMEOUT_RE = re.compile(r"time(d)? ?out|TimeoutError|timeout", re.I)


def _ask(call_id, chat_id):
    fut = asyncio.get_running_loop().create_future()
    _approvals[call_id] = fut
    _approval_chat[call_id] = chat_id
    return fut


def resolve_approval(call_id, decision, chat_id=None, category=None):
    fut = _approvals.pop(call_id, None)
    _approval_chat.pop(call_id, None)
    if decision == "allow_chat" and chat_id and category:
        _chat_allow.setdefault(chat_id, set()).add(category)
    if fut and not fut.done():
        fut.set_result(decision)
        return True
    return False


def active():
    """True while any reply is streaming (background memory work waits for this)."""
    return bool(_cancel)


def stop(chat_id):
    ev = _cancel.get(chat_id)
    if ev:
        ev.set()
    for cid, fut in list(_approvals.items()):
        if _approval_chat.get(cid) in (chat_id, None) and not fut.done():
            fut.set_result("deny")
    release(chat_id, close_browser=True)


def release(chat_id, close_browser=False):
    """Everything a chat's agents hold: resource locks (windows, the real input), foreground grants, and on Stop the
    agent's browser tab."""
    resources.release_owner(chat_id)
    input_guard.clear(chat_id)
    if close_browser:
        try:
            from .tools import browser
            browser.stop_owner(chat_id)
        except Exception:
            pass


def stop_all():
    """Stop every running task (the Stop button on the "is controlling" banner)."""
    for ev in list(_cancel.values()):
        ev.set()
    for fut in list(_approvals.values()):
        if not fut.done():
            fut.set_result("deny")
    for cid in list(_cancel):
        release(cid, close_browser=True)
    return len(_cancel)


_screen = {}


def _screen_size():
    if "v" not in _screen:
        try:
            import mss
            with mss.mss() as s:
                mon = s.monitors[1]
                _screen["v"] = f"{mon['width']}x{mon['height']}"
        except Exception:
            _screen["v"] = "unknown"
    return _screen["v"]


def _env_block(settings, vision, ctx_size):
    """Static facts only: anything that changes per turn would invalidate the prompt cache."""
    return (f"\n\n# Environment\n- OS: {osinfo.name()} ({osinfo.shell_name()} is the shell)\n"
            f"- User: {getpass.getuser()}\n"
            f"- Working directory: {settings.get('work_dir')}\n- Primary screen: {_screen_size()}\n"
            f"- Vision: {'yes, you can see images and screenshots' if vision else 'no (text only)'}\n"
            f"- Context window: {ctx_size:,} tokens\n"
            "- Each user message ends with a <turn_context> block (time sent, the router's plan, relevant "
            "memories). It is written by Aero, not typed by the user." + _local_only_line(settings))


def _local_only_line(settings):
    if settings.get("strict_offline"):
        return ("\n- Strict offline is on: you have no internet access (no web search, web pages, browser or MCP "
                "servers). Work from what you know and the user's local files and apps.")
    if settings.get("local_only"):
        return ("\n- Local Only is on: you have no internet access (no web search, web pages, browser or MCP "
                "servers). Answer from what you know and the user's local files and apps. When a request needs "
                "current information from the internet, say so, and that the user can turn Local Only off in the "
                "composer to let you search.")
    return ""


def _when(ts):
    try:
        return time.strftime("%a %Y-%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return ""


REVIEWER_NAMES = {"fable": "Claude Fable 5.1", "astra": "GPT-6 Astra"}       # lanes that review (read-only)
TAKEOVER_NAMES = {"opus": "Claude Opus 5.5", "sol": "GPT-6.1 Sol"}          # lanes that redo the task


def _user_content(m, vision, note=""):
    text = m.get("content") or ""
    if m.get("from") in REVIEWER_NAMES:
        text = f"[Message from the cloud reviewer ({REVIEWER_NAMES[m['from']]}), not from the user]\n" + text
    for i, p in enumerate(m.get("pastes") or [], 1):
        text += f'\n\n<pasted_text title="{p.get("name") or f"Pasted text {i}"}">\n{p.get("text", "")}\n</pasted_text>'
    lp = m.get("loop")
    if lp:
        text += (f"\n\n[Forever-loop iteration {lp.get('iteration', 1)}: the user set this prompt to repeat until they "
                 "stop it. Continue from where the previous iteration left off and make the next real improvement or "
                 "update; do not redo finished work. End with a one-line status of what changed this iteration.]")
    ta = m.get("target_app")
    if ta:
        text += (f'\n\n[The user selected the app window "{ta.get("title")}" ({ta.get("app")}, window id {ta.get("id")}). '
                 f'Start with app_view window="{ta.get("id")}" to see inside it, then use the app_* tools.]')
    images = []
    for a in m.get("attachments") or []:
        if a.get("kind") == "image":
            if vision:
                try:
                    images.append(attachments.image_data_url(a["id"]))
                    continue
                except Exception as e:  # noqa: BLE001
                    text += f"\n\n[Attached image {a['name']} could not be decoded: {e}]"
                    continue
            text += f"\n\n[Attached image '{a['name']}' - the current model has no vision support, so it can't be seen]"
        else:
            body = attachments.text_of(a["id"])
            meta = attachments.load_meta(a["id"]) or {}
            nt = " (truncated)" if meta.get("truncated") else ""
            text += f'\n\n<file name="{a["name"]}" size="{a.get("size", 0)}"{nt}>\n{body}\n</file>'
    if note:
        text += "\n\n" + note
    if not images:
        return text
    return [{"type": "text", "text": text}] + [{"type": "image_url", "image_url": {"url": u}} for u in images]


def _est_tokens(content):
    if isinstance(content, list):
        return sum(_est_tokens(c.get("text", "")) if c.get("type") == "text" else 900 for c in content)
    return len(content or "") // 3 + 4


def _last_user_text(history):
    m = next((m for m in reversed(history) if m.get("role") == "user" and m.get("from") not in REVIEWER_NAMES), None)
    return (m or {}).get("content") or ""


def _turn_note(user_msg, router_rec, memory_text=""):
    """The <turn_context> block for one user message. Deterministic for stored data, so it renders the same
    way every time (prompt-cache friendly); memory_text is only passed for the newest message."""
    lines = []
    if user_msg.get("ts"):
        lines.append(f"Sent: {_when(user_msg['ts'])}")
    d = (router_rec or {}).get("decision") or {}
    if d.get("plan"):
        lines.append(f"Router plan ({d.get('complexity', '?')}): " + " | ".join(d["plan"]))
    if d.get("skills"):
        lines.append("Relevant skills (load with use_skill): " + ", ".join(d["skills"]))
    if memory_text:
        lines.append(memory_text)
    if not lines:
        return ""
    return "<turn_context>\n" + "\n".join(lines) + "\n</turn_context>"


def local_view(history):
    """What the local model sees of a chat that also contains router records, reviews and other models' work."""
    out = []
    for i, m in enumerate(history):
        r, lane = m.get("role"), m.get("lane") or "local"
        if r in ("router", "review", "lesson", "notice", "subagent", "loopnote", "mod", "learned"):
            continue
        if lane in REVIEWER_NAMES:
            continue
        if lane in TAKEOVER_NAMES:
            if r == "assistant" and not m.get("tool_calls") and (m.get("content") or "").strip():
                out.append({"role": "assistant", "content": f"[{TAKEOVER_NAMES[lane]} took over this task after a review "
                            "found problems in my attempt. Its final answer:]\n" + m["content"], "_i": i})
            continue
        out.append({**m, "_i": i})
    return out


def build_messages(history, settings, vision, ctx_size, extra="", trim=True, calib=1.0, memory_text=""):
    sys_text = (settings.get("system_prompt") or "") + _env_block(settings, vision, ctx_size)
    if extra:
        sys_text += "\n\n" + extra
    sys_msg = {"role": "system", "content": sys_text}
    keep_imgs = int(settings.get("keep_screenshots") or 2)
    view = local_view(history)
    img_tools = [k for k, m in enumerate(view) if m.get("role") == "tool" and m.get("image")]
    live_imgs = set(img_tools[-keep_imgs:]) if vision else set()
    # router record that follows each user message (if any)
    routers = {}
    last_user = None
    for i, m in enumerate(history):
        if m.get("role") == "user":
            last_user = i
        elif m.get("role") == "router" and last_user is not None:
            routers[last_user] = m
    newest_user = max((m["_i"] for m in view if m.get("role") == "user" and m.get("from") not in REVIEWER_NAMES), default=None)

    out, pending_imgs = [], []

    def flush():
        if pending_imgs:
            parts = [{"type": "text", "text": "[Images returned by the tool calls above]"}]
            for uid in pending_imgs:
                try:
                    parts.append({"type": "image_url", "image_url": {"url": attachments.image_data_url(uid)}})
                except Exception:
                    pass
            out.append({"role": "user", "content": parts})
            pending_imgs.clear()

    for k, m in enumerate(view):
        r = m.get("role")
        if r != "tool":
            flush()
        if r == "user":
            note = _turn_note(m, routers.get(m["_i"]), memory_text if m["_i"] == newest_user else "")
            out.append({"role": "user", "content": _user_content(m, vision, note)})
        elif r == "assistant":
            a = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                a["tool_calls"] = m["tool_calls"]
            out.append(a)
        elif r == "tool":
            c = m.get("content") or ""
            if m.get("image") and k not in live_imgs:
                c += " [older image omitted to save context]"
            out.append({"role": "tool", "tool_call_id": m.get("tool_call_id"), "content": c})
            if k in live_imgs:
                pending_imgs.append(m["image"])
    flush()
    out = _merge_same_role(out)

    if not trim:
        return [sys_msg] + out
    # keep within ~85% of the context window: shrink old tool outputs first, then drop old turns
    budget = int(ctx_size * 0.85 / calib) - _est_tokens(sys_msg["content"]) - 600
    total = lambda: sum(_est_tokens(x.get("content")) for x in out)
    if total() > budget:
        for x in out[:-6]:
            if x["role"] == "tool" and isinstance(x["content"], str) and len(x["content"]) > 1500:
                x["content"] = x["content"][:1200] + "\n... [trimmed to save context]"
    while total() > budget and len(out) > 2:
        out.pop(0)
        while out and out[0]["role"] == "tool":
            out.pop(0)
    return [sys_msg] + out


def _merge_same_role(msgs):
    """Strict chat templates reject two user or two plain assistant messages in a row; join them."""
    res = []
    for m in msgs:
        p = res[-1] if res else None
        if p and p["role"] == m["role"] and m["role"] in ("user", "assistant") \
                and not p.get("tool_calls") and not m.get("tool_calls"):
            a, b = p["content"], m["content"]
            if isinstance(a, str) and isinstance(b, str):
                p["content"] = (a + "\n\n" + b).strip()
            else:
                la = a if isinstance(a, list) else [{"type": "text", "text": a}]
                lb = b if isinstance(b, list) else [{"type": "text", "text": b}]
                p["content"] = la + lb
            continue
        res.append(dict(m))
    return res


def est_tokens(msgs):
    return sum(_est_tokens(x.get("content")) for x in msgs)


_profiles = {}      # chat id -> (key, text, fact ids): built once per chat so the system prompt stays cached


def profile_for(settings, chat_id, ctx_size):
    """The user's profile block for this chat's system prompt (see memory.profile_block) and the fact ids in it.
    Fixed for the whole chat; editing the profile text in Settings rebuilds it."""
    if not settings.get("profile_enabled", True):
        return "", set()
    budget = min(int(settings.get("profile_budget_tokens") or 1200), max(250, ctx_size // 8))
    learned = bool(settings.get("memory_enabled", True))
    key = (hash(settings.get("user_profile") or ""), budget, learned)
    hit = _profiles.get(chat_id)
    if hit and hit[0] == key:
        return hit[1], hit[2]
    text, ids = memory.profile_block(settings.get("user_profile") or "", budget, learned)
    _profiles[chat_id] = (key, text, ids)
    while len(_profiles) > 64:
        _profiles.pop(next(iter(_profiles)))
    return text, ids


def memory_recall(settings, history, carry, chat_id, ctx_size):
    if not settings.get("memory_enabled", True):
        return ""
    budget = min(int(settings.get("memory_budget_tokens") or 1500), max(300, ctx_size // 10))
    skip = {chat_id} | ({carry.get("from")} if carry else set())
    return memory.context_block(_last_user_text(history), budget, skip, profile_for(settings, chat_id, ctx_size)[1])


def _split_point(history, keep_tokens):
    """Index where the kept tail starts: newest messages worth ~keep_tokens, never starting on a tool result."""
    used, i = 0, len(history)
    while i > 0 and used + _est_tokens(history[i - 1].get("content")) <= keep_tokens:
        i -= 1
        used += _est_tokens(history[i].get("content"))
    i = min(i, len(history) - 1)
    while i < len(history) - 1 and history[i].get("role") == "tool":
        i += 1
    return i


# ------------------------------------------------------------------------------------------------ turn state

class Turn:
    """Everything one turn shares between the router, the local loop and the cloud models."""

    def __init__(self, chat_id, history, settings, engine_state, carry=None, title=""):
        self.chat_id = chat_id
        self.history = list(history)
        self.settings = settings
        self.engine = engine_state
        self.carry = carry
        self.title = title
        self.cancel = asyncio.Event()
        _cancel[chat_id] = self.cancel
        self.vision = bool(engine_state.get("vision"))
        self.ctx_size = int(engine_state.get("ctx") or 8192)
        self.tctx = tools.Ctx(settings, self.vision, chat_id, self.cancel)
        self.graph = task_graph.TaskGraph(title, chat_id)
        self.ambiguous = {}          # op key -> observation count when a consequential call ended in an unknown state
        self.observed = 0            # read-only calls so far (an observation between attempts allows a repeat)
        self.exposed = None          # None = every enabled tool; else a list of names (router focus)
        self.think = settings.get("thinking", True)
        self.memory_text = ""
        self.changes = {}            # path -> {"before": str|None, "after": str|None}
        self.calib = 1.0
        self.decision = None
        self.start_index = len(self.history)
        self.model_name = ((engine_state.get("model") or {}).get("name") or "local model")
        self.review = False
        self.gpt_review = False
        self.prior_reviews = []
        self.flow = None
        self.codex_final = ""
        self.cloud_result = None
        self.persona = ""            # system-prompt block when the user talks to a subagent directly
        self.controlling = None      # {"by", "target"} once a model has driven an app in this turn
        self.blocked = set()         # tools this turn may not use
        self.sandbox = None          # Mod Aero chat: the only folder writes may touch (mods.prepare_turn)

    def rebind(self, new_id):
        self.chat_id = new_id
        _cancel[new_id] = self.cancel
        self.tctx.chat_id = new_id
        self.graph.chat_id = new_id

    def close(self):
        for k in [k for k, v in _cancel.items() if v is self.cancel]:
            _cancel.pop(k, None)


class SubTurn:
    """A subagent's turn: a fresh context with its own task, sharing the parent turn's model, tools, settings,
    approvals, file-change tracking and Stop."""
    is_sub = True

    def __init__(self, parent, sid, name, task):
        self.root = getattr(parent, "root", parent)
        self.chat_id = parent.chat_id
        self.history = [{"role": "user", "content": task, "ts": time.time(), "id": uuid.uuid4().hex[:10]}]
        self.settings = parent.settings
        self.engine = parent.engine
        self.carry = None
        self.title = name
        self.cancel = parent.cancel
        self.vision = parent.vision
        self.ctx_size = parent.ctx_size
        self.tctx = parent.tctx
        self.exposed = None if parent.exposed is None else [n for n in parent.exposed if n != "run_subagent"]
        self.blocked = {"run_subagent"}             # one level deep: a subagent can't start subagents
        self.think = parent.think
        self.memory_text = ""
        self.changes = parent.changes
        self.calib = parent.calib
        self.decision = None
        self.start_index = 1
        self.model_name = parent.model_name
        self.persona = agents.SUB_PERSONA.format(name=name)
        self.sub_id, self.sub_name = sid, name
        self.graph = self.root.graph
        self.ambiguous = self.root.ambiguous

    @property
    def observed(self):
        return self.root.observed

    @observed.setter
    def observed(self, v):
        self.root.observed = v

    @property
    def controlling(self):
        return self.root.controlling

    @controlling.setter
    def controlling(self, v):
        self.root.controlling = v


# ------------------------------------------------------------------------------------------------ tools

def _snapshot(path: Path):
    try:
        if path.is_file() and path.stat().st_size < 400_000:
            b = path.read_bytes()
            if b"\x00" not in b[:4096]:
                return b.decode("utf-8", "replace")
            return f"[binary file, {len(b):,} bytes]"
        if path.is_dir():
            return "[folder]"
    except Exception:
        pass
    return None


def _track(turn, name, args, before):
    paths = []
    if name in ("write_file", "edit_file", "delete_path"):
        paths = [args.get("path")]
    elif name == "move_path":
        paths = [args.get("src") or args.get("source"), args.get("dst") or args.get("dest") or args.get("destination")]
    out = []
    for p in paths:
        if not p:
            continue
        try:
            rp = turn.tctx.path(p)
        except Exception:
            continue
        if before:
            turn.changes.setdefault(str(rp), {"before": _snapshot(rp), "after": None})
        else:
            turn.changes.setdefault(str(rp), {"before": None, "after": None})["after"] = _snapshot(rp)
        out.append(rp)
    return out


def change_diffs(turn, limit_chars=60000):
    """Unified diffs of files the turn changed (for reviewers)."""
    chunks, used = [], 0
    for path, c in turn.changes.items():
        a, b = c.get("before"), c.get("after")
        if a == b:
            continue
        if a is None and b is not None:
            body = f"(new file)\n{b[:8000]}"
        elif b is None:
            body = "(deleted or moved away)"
        else:
            d = "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), "before", "after", n=3))
            body = d[:12000] + ("\n... [diff truncated]" if len(d) > 12000 else "")
        piece = f"### {path}\n```diff\n{body}\n```"
        if used + len(piece) > limit_chars:
            chunks.append(f"... and {len(turn.changes) - len(chunks)} more changed paths (not shown)")
            break
        chunks.append(piece)
        used += len(piece)
    return "\n\n".join(chunks)


def sandbox_ok(turn, name, args):
    """A Mod Aero chat (turn.sandbox set) may only write inside its copy of Aero."""
    sb = getattr(turn, "sandbox", None)
    if not sb or name not in WRITE_TOOLS:
        return True
    keys = ("source", "src", "destination", "dst", "dest") if name == "move_path" else ("path",)
    for k in keys:
        p = (args or {}).get(k)
        if not p:
            continue
        try:
            turn.tctx.path(p).relative_to(sb)
        except (ValueError, OSError):
            return False
    return True


def _root(turn):
    return getattr(turn, "root", turn)


def _graph_node(turn, name, label, lane, category):
    """A task-graph node for this call (the UI's progress list and Stop's summary come from these)."""
    g = getattr(turn, "graph", None)
    if g is None:
        return None
    caps = capabilities.caps_of(name, category)
    who = getattr(turn, "sub_name", "")
    return g.add(f"{tools_name(name)} {label}".strip(), name, capability=(caps.capabilities or ("",))[0],
                 risk=caps.side_effect, locks=caps.locks, lane=lane, label=(f"{who}: " if who else "") +
                 f"{tools_name(name)} {label}".strip())


def tools_name(name):
    return agents.doing_text(name, "") if name else ""


def _node_event(turn, node):
    if node is None:
        return None
    g = turn.graph
    g.save()
    return {"t": "task", "graph": g.id, "node": {k: getattr(node, k) for k in ("id", "label", "state", "action", "lane",
                                                                               "error", "waiting_on")}}


def _lock_keys(turn, name, args, physical):
    keys = []
    caps = capabilities.caps_of(name)
    if "window" in caps.locks:
        try:
            from .tools import apps
            h = apps.selected_hwnd(_root(turn).chat_id)
            if h:
                keys.append(f"window:{h}")
        except Exception:
            pass
    if physical:
        keys.append(resources.EXCLUSIVE_INPUT)
    return keys


def _lock_text(keys, owner):
    for k in keys:
        h = resources.holder(k)
        if h and h["owner"] != owner:
            other = agents.LIVE.get(h["owner"], {}).get("name") or "another Aero agent"
            what = "your real mouse and keyboard" if k == resources.EXCLUSIVE_INPUT else "that window"
            return (f"{other} is using {what} right now, so this call did not run (no two agents act on the same "
                    "window or input at once). Wait for it to finish, work on something else, or pick another window.")
    return "The resource this call needs is busy."


async def _acquire(keys, owner, cancel, timeout):
    if not keys:
        return True
    t0 = time.monotonic()
    while True:
        if resources.acquire(keys, owner, 0):
            return True
        if cancel.is_set() or time.monotonic() - t0 >= timeout:
            return False
        await asyncio.sleep(0.2)


def _repeat_guard(turn, name, args):
    """Verify before repeating: a consequential call whose last attempt ended in an unknown state (a timeout) may
    only run again after the agent has looked at something in between (this turn or an earlier one)."""
    caps = capabilities.caps_of(name)
    if caps.side_effect not in capabilities.NON_IDEMPOTENT or args is None:
        return None
    key = task_graph.op_key(_root(turn).chat_id, name, args)
    if key not in turn.ambiguous and caps.side_effect in ("external_write", "destructive"):
        prev = task_graph.peek(key)
        if prev and prev.get("status") == "unknown":
            turn.ambiguous[key] = turn.observed                       # from an earlier turn: look first in this one
    seen = turn.ambiguous.get(key)
    if seen is not None and turn.observed <= seen:
        return {"text": f"The same {name} call ran before and its result is unknown (it timed out), so it may already "
                        "have happened. Check first (read the page, the sent folder, the file or the app), then try "
                        "again only if it didn't.", "error": True, "denied": True}
    return None


def _note_outcome(turn, name, args, res):
    caps = capabilities.caps_of(name)
    if capabilities.read_only(name, args):
        turn.observed += 1
        return
    if caps.side_effect not in capabilities.NON_IDEMPOTENT or args is None or res.get("denied"):
        return
    key = task_graph.op_key(_root(turn).chat_id, name, args)
    text = (res.get("text") or "") + " " + str((res.get("envelope") or {}).get("error") or "")
    if res.get("error") and TIMEOUT_RE.search(text):
        turn.ambiguous[key] = turn.observed
        if caps.side_effect in ("external_write", "destructive"):
            task_graph.end(key, "unknown", "timed out")
    else:
        if turn.ambiguous.pop(key, None) is not None or caps.side_effect in ("external_write", "destructive"):
            task_graph.clear_op(key)

async def _handoff(turn, lane):
    """Wait until the user has stopped typing or moving the mouse. Yields notices; leaves turn.handoff True/False."""
    turn.handoff = True
    idle = input_guard.idle_ms()
    if idle is None or idle >= input_guard.IDLE_MS:
        return
    yield {"t": "notice", "lane": lane, "text": "Waiting for you to stop typing and moving the mouse before Aero "
                                                "takes them."}
    t0 = time.monotonic()
    while time.monotonic() - t0 < input_guard.IDLE_WAIT_S:
        await asyncio.sleep(0.2)
        if turn.cancel.is_set():
            turn.handoff = False
            return
        i = input_guard.idle_ms()
        if i is None or i >= input_guard.IDLE_MS:
            return
    turn.handoff = False


async def exec_tool(turn, call_id, name, args, lane="local", allowed_categories=None, out=None):
    """Run one tool call with the user's approval policy. Yields UI events; the result lands in out['res'].

    On the way: Strict Background Only and the foreground-control grant for anything that would use the user's real
    mouse or keyboard (input_guard.py), the exclusive locks for that input and for the agent's app window
    (resources.py), the verify-before-repeat check for consequential calls, a task-graph node, and the evidence
    note the model sees (action_results.py)."""
    t = tools.REGISTRY.get(name)
    category = t.category if t else None
    pol = tools.policy(name, turn.settings)
    if allowed_categories is not None and category not in allowed_categories:
        pol = "off"
    if name in (getattr(turn, "blocked", None) or ()):
        pol = "off"
    owner = _root(turn).chat_id
    physical = bool(t) and isinstance(args, dict) and capabilities.is_physical(name, args)
    strict = physical and input_guard.strict(turn.settings)
    fg_needed = physical and not strict and pol == "ask" and not input_guard.has_grant(owner)
    needs = pol == "ask" and category not in _chat_allow.get(turn.chat_id, set()) and not physical
    label = tools.label(name, args or {})
    node = _graph_node(turn, name, label, lane, category) if t else None
    ask_fut = None
    if needs and args is not None:          # registered before the card goes out, so an instant answer or Stop finds it
        ask_fut = _ask(call_id, _root(turn).chat_id)
    yield {"t": "tool_start", "call_id": call_id, "name": name, "args": args, "label": label, "category": category,
           "needs_approval": needs and args is not None, "foreground": fg_needed and args is not None, "lane": lane}
    if node is not None:
        turn.graph.set(node.id, "running")
        ev = _node_event(turn, node)
        if ev:
            yield ev
    res = None
    if args is None:
        res = {"text": "Could not parse the arguments as JSON. Send valid JSON matching the tool's schema.", "error": True}
    elif pol == "off" or not t:
        res = {"text": f"Tool '{name}' is not available here.", "error": True}
    elif not sandbox_ok(turn, name, args):
        res = {"text": f"In a Mod Aero chat only files inside your copy of Aero ({turn.sandbox}) can be changed.",
               "error": True}
    elif strict:
        res = {"text": f"Strict Background Only is on (Settings > Tools), so Aero does not use the real mouse or "
                       f"keyboard, not even for {name} {label}. Use element ids (app_view, app_click, app_type), "
                       "files, the shell or the browser tools, or tell the user what would need their own input.",
               "error": True, "denied": True}
    elif (guard := _repeat_guard(turn, name, args)) is not None:
        res = guard
    else:
        decision = "allow"
        if ask_fut is not None:
            decision = "deny" if turn.cancel.is_set() else await ask_fut
        if fg_needed and decision != "deny" and not turn.cancel.is_set():
            what, _rect = await asyncio.to_thread(control.target, name, args, owner)
            fut = _ask(call_id, owner)
            yield {"t": "foreground_request", "call_id": call_id, "name": name, "label": label, "target": what,
                   "by": control.actor(turn, lane), "lane": lane}
            decision = "deny" if turn.cancel.is_set() else await fut
            if decision in ("allow", "allow_task"):
                input_guard.grant(owner, "task" if decision == "allow_task" else "once")
        if decision == "deny" or turn.cancel.is_set():
            res = {"text": "The user denied this action. Ask what they want instead, or try another approach.",
                   "error": True, "denied": True}
        else:
            keys = _lock_keys(turn, name, args, physical)
            if physical:
                async for ev in _handoff(turn, lane):
                    yield ev
            if physical and not getattr(turn, "handoff", True):
                res = {"text": "You kept using the mouse and keyboard, so Aero did not take them. It can try again "
                               "when you stop, or use a background route.", "error": True}
            elif not await _acquire(keys, owner, turn.cancel, 30 if physical else 0):
                res = {"text": _lock_text(keys, owner), "error": True}
            else:
                input_keys = [k for k in keys if k == resources.EXCLUSIVE_INPUT]
                try:
                    if name in control.CONTROL_TOOLS:
                        by = control.actor(turn, lane)
                        what, rect = await asyncio.to_thread(control.target, name, args, owner)
                        mode = control.mode_of(name, args, physical)
                        text = control.phrase(by, what, mode)
                        turn.controlling = {"by": by, "target": what, "mode": mode, "text": text}
                        if control.on_screen(name, mode):
                            await asyncio.to_thread(control.banner_on, text, rect)
                        else:
                            await asyncio.to_thread(control.banner_off)
                        yield {"t": "control", "by": by, "target": what, "tool": name, "lane": lane, "mode": mode,
                               "text": text}
                    if name in WRITE_TOOLS:
                        _track(turn, name, args, before=True)
                    cctx = turn.tctx
                    if physical:
                        cctx = copy.copy(turn.tctx)
                        cctx.physical_ok = True
                        with input_guard.Session(owner) as gs:
                            res = await asyncio.to_thread(tools.run, name, args, cctx)
                        if gs.note() and isinstance(res, dict):
                            res["text"] = (res.get("text") or "") + gs.note()
                        input_guard.consume(owner)
                    else:
                        res = await asyncio.to_thread(tools.run, name, args, cctx)
                    if name in WRITE_TOOLS:
                        _track(turn, name, args, before=False)
                    stats.note_tool(category)
                finally:
                    if input_keys:
                        resources.release(input_keys, owner)
    if ask_fut is not None and not ask_fut.done():
        _approvals.pop(call_id, None)
        ask_fut.cancel()
    if t:
        _note_outcome(turn, name, args, res)
    evidence = action_results.evidence(res.get("envelope"))
    content = res.get("text", "") + (("\n" + evidence) if evidence else "")
    tmsg = {"role": "tool", "tool_call_id": call_id, "name": name, "content": content,
            "error": bool(res.get("error")), "image": res.get("image"), "label": label, "lane": lane}
    env = res.get("envelope") or {}
    if "verification_method" in env:
        tmsg["verified"] = env.get("verified")
        if env.get("control_mode"):
            tmsg["mode"] = env["control_mode"]
    if res.get("denied"):
        tmsg["denied"] = True
    if node is not None:
        st = "cancelled" if res.get("denied") or turn.cancel.is_set() else "failed" if res.get("error") else "completed"
        turn.graph.set(node.id, st, result=(res.get("text") or "")[:200] if st == "completed" else None,
                       error=(res.get("text") or "")[:200] if st != "completed" else None)
        ev = _node_event(turn, node)
        if ev:
            yield ev
    if out is not None:
        out["res"] = res
        out["msg"] = tmsg
    yield {"t": "tool_result", "message": tmsg}


# ------------------------------------------------------------------------------------------------ exposure

def load_tools_schema(turn, all_schemas):
    exposed = set(turn.exposed or [])
    rest = {}
    for s in all_schemas:
        n = s["function"]["name"]
        if n not in exposed:
            cat = tools.REGISTRY[n].category if n in tools.REGISTRY else "other"
            rest.setdefault(cat, []).append(n)
    if not rest:
        return None
    listing = "; ".join(f"{c}: {', '.join(ns)}" for c, ns in rest.items())
    return {"type": "function", "function": {
        "name": "load_tools",
        "description": "Load more tools for this task when the ones you have are not enough. Not loaded yet: "
                       + listing + ". Pass tool names or whole category names.",
        "parameters": {"type": "object", "properties": {"names": {"type": "array", "items": {"type": "string"}}},
                       "required": ["names"]}}}


def current_schemas(turn):
    if not turn.settings.get("tools_enabled", True):
        return []
    blocked = getattr(turn, "blocked", None) or ()
    allv = [x for x in tools.schemas(turn.settings) if x["function"]["name"] not in blocked]
    if turn.exposed is None:
        return allv
    keep = set(turn.exposed)
    sel = [s for s in allv if s["function"]["name"] in keep]
    lt = load_tools_schema(turn, allv)
    return sel + ([lt] if lt else [])


def _do_load_tools(turn, args):
    allv = {s["function"]["name"]: s for s in tools.schemas(turn.settings)}
    want = [str(x) for x in (args or {}).get("names") or []]
    added = []
    for w in want:
        for n in allv:
            cat = tools.REGISTRY[n].category if n in tools.REGISTRY else ""
            if (n == w or cat == w) and n not in (turn.exposed or []):
                turn.exposed.append(n)
                added.append(n)
    known = set(turn.exposed or [])
    turn.exposed = [n for n in allv if n in known]          # keep registry order (stable prefix)
    if not added:
        return f"Nothing new loaded. Valid names: {', '.join(allv)}"
    return "Loaded: " + ", ".join(added) + ". They are available from your next step."


# ------------------------------------------------------------------------------------------------ local loop

async def run_local(turn, lane="local", max_steps=None):
    """The local model's agent loop. Yields UI events (dicts). Appends to turn.history."""
    from . import pipeline   # compaction lives with the turn orchestration
    url = turn.engine["url"] + "/v1/chat/completions"
    settings = turn.settings
    max_steps = int(max_steps or settings.get("agent_max_steps") or 40)
    compact_at = 0 if getattr(turn, "is_sub", False) else float(settings.get("auto_compact_at") or 0)
    overflow = 0
    tools_rejected = False
    for step in range(max_steps):
        if turn.cancel.is_set():
            return
        full = build_messages(turn.history, settings, turn.vision, turn.ctx_size, pipeline.extra(turn), trim=False,
                              memory_text=turn.memory_text)
        if compact_at and len(turn.history) > 4 and est_tokens(full) * turn.calib > turn.ctx_size * max(compact_at, 0.8):
            async for ev in pipeline.compact_midturn(turn):
                yield ev
        sent = build_messages(turn.history, settings, turn.vision, turn.ctx_size, pipeline.extra(turn),
                              calib=turn.calib, memory_text=turn.memory_text)
        schemas = [] if tools_rejected else current_schemas(turn)
        think = turn.think
        body = {"messages": sent, "stream": True,
                "temperature": settings["temperature"], "top_p": settings["top_p"], "top_k": settings["top_k"],
                "min_p": settings["min_p"], "repeat_penalty": settings["repeat_penalty"],
                "chat_template_kwargs": {"enable_thinking": bool(think)},
                "stream_options": {"include_usage": True}, "cache_prompt": True}
        if settings.get("max_tokens"):
            body["max_tokens"] = int(settings["max_tokens"])
        if schemas:
            body["tools"] = schemas
            body["parallel_tool_calls"] = True

        msg = {"role": "assistant", "content": "", "reasoning": "", "id": uuid.uuid4().hex[:10], "lane": lane,
               "model": turn.model_name}
        calls = {}
        timings, usage, finish = {}, {}, None
        t0 = time.time()
        first_tok = None
        yield {"t": "assistant_start", "id": msg["id"], "lane": lane, "model": turn.model_name}
        retry = False
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10)) as client:
            async with client.stream("POST", url, json=body) as resp:
                if resp.status_code != 200:
                    err = (await resp.aread()).decode("utf-8", "replace")
                    n_prompt = 0
                    try:
                        ej = json.loads(err).get("error", {})
                        err, n_prompt = ej.get("message", err), int(ej.get("n_prompt_tokens") or 0)
                    except Exception:
                        pass
                    if n_prompt and overflow < 3:      # our estimate was low for this tokenizer: learn and retry
                        overflow += 1
                        turn.calib = max(turn.calib, n_prompt / max(1, est_tokens(sent)) * 1.05)
                        retry = True
                    elif schemas and step == 0 and "tool" in err.lower():
                        yield {"t": "notice", "text": "This model's chat template rejected tools; continuing without tools."}
                        tools_rejected = True
                        retry = True
                    else:
                        raise RuntimeError(f"llama-server error {resp.status_code}: {err[:800]}")
                else:
                    async for line in resp.aiter_lines():
                        if turn.cancel.is_set():
                            break
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except Exception:
                            continue
                        if chunk.get("timings"):
                            timings = chunk["timings"]
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        for ch in chunk.get("choices") or []:
                            d = ch.get("delta") or {}
                            if ch.get("finish_reason"):
                                finish = ch["finish_reason"]
                            if d.get("reasoning_content"):
                                first_tok = first_tok or time.time()
                                msg["reasoning"] += d["reasoning_content"]
                                yield {"t": "reasoning", "d": d["reasoning_content"], "lane": lane}
                            if d.get("content"):
                                first_tok = first_tok or time.time()
                                msg["content"] += d["content"]
                                yield {"t": "content", "d": d["content"], "lane": lane}
                            for tc in d.get("tool_calls") or []:
                                c = calls.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                                if tc.get("id"):
                                    c["id"] = tc["id"]
                                f = tc.get("function") or {}
                                if f.get("name"):
                                    c["name"] += f["name"]
                                    yield {"t": "tool_pending", "name": c["name"], "lane": lane}
                                if f.get("arguments"):
                                    c["args"] += f["arguments"]
        if retry:
            continue

        # fallback: models that print <tool_call>{...}</tool_call> without the server parsing it
        if not calls and schemas and "<tool_call>" in msg["content"]:
            for i, mm in enumerate(TOOL_CALL_RE.finditer(msg["content"])):
                try:
                    j = json.loads(mm.group(1))
                    calls[i] = {"id": None, "name": j.get("name", ""),
                                "args": json.dumps(j.get("arguments") or j.get("parameters") or {})}
                except Exception:
                    pass
            if calls:
                msg["content"] = TOOL_CALL_RE.sub("", msg["content"]).strip()

        if msg["reasoning"] == "" and "<think>" in msg["content"]:
            m = re.search(r"<think>(.*?)(</think>|$)", msg["content"], re.S)
            msg["reasoning"] = m.group(1).strip()
            msg["content"] = re.sub(r"<think>.*?(</think>|$)", "", msg["content"], flags=re.S).strip()

        tool_calls = []
        for i in sorted(calls):
            c = calls[i]
            tool_calls.append({"id": c["id"] or f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                               "function": {"name": c["name"], "arguments": c["args"] or "{}"}})
        if tool_calls:
            msg["tool_calls"] = tool_calls
        dt = time.time() - t0
        msg["stats"] = {
            "tg": round(timings.get("predicted_per_second") or 0, 1),
            "pp": round(timings.get("prompt_per_second") or 0, 1),
            "prompt_tokens": usage.get("prompt_tokens") or timings.get("prompt_n"),
            "completion_tokens": usage.get("completion_tokens") or timings.get("predicted_n"),
            "cached_tokens": timings.get("cache_n"),
            "ttft": round((first_tok - t0), 2) if first_tok else None, "time": round(dt, 1),
            "finish": "stopped" if turn.cancel.is_set() else finish, "ctx": turn.ctx_size,
            "tools_sent": len(schemas), "think": bool(think),
        }
        if msg["stats"]["prompt_tokens"]:
            turn.calib = max(0.5, min(4.0, msg["stats"]["prompt_tokens"] / max(1, est_tokens(sent))))
        stats.note_reply(msg["stats"])
        if not msg["reasoning"]:
            msg.pop("reasoning")
        turn.history.append(msg)
        yield {"t": "assistant_done", "message": msg}

        if turn.cancel.is_set() or not tool_calls:
            return

        parsed = []
        for tc in tool_calls:
            try:
                a = json.loads(tc["function"]["arguments"] or "{}")
                if not isinstance(a, dict):
                    a = {"value": a}
            except Exception:
                a = None
            parsed.append((tc, tc["function"]["name"], a))
        i = 0
        while i < len(parsed):
            tc, name, args = parsed[i]
            batch = []
            j = i
            while j < len(parsed) and len(batch) < PARALLEL_MAX and parsed[j][1] not in LOOP_TOOLS and \
                    parsed[j][2] is not None and capabilities.read_only(parsed[j][1], parsed[j][2]):
                batch.append(parsed[j])
                j += 1
            if len(batch) > 1:                      # independent lookups from one step run side by side
                async for ev in _run_parallel(turn, batch, lane):
                    yield ev
                i = j
                if turn.cancel.is_set():
                    return
                continue
            i += 1
            if name == "ask_user":
                async for ev in ask_user(turn, tc["id"], args, lane):
                    yield ev
                continue
            if name == "get_answer":
                async for ev in get_answer(turn, tc["id"], args, lane):
                    yield ev
                if turn.cancel.is_set():
                    return
                continue
            if name == "run_subagent":
                async for ev in run_subagent(turn, tc["id"], args, lane):
                    yield ev
                if turn.cancel.is_set():
                    return
                continue
            if name == "load_tools" and turn.exposed is not None:
                text = _do_load_tools(turn, args or {})
                tmsg = {"role": "tool", "tool_call_id": tc["id"], "name": name, "content": text, "error": False,
                        "label": ", ".join((args or {}).get("names") or []), "lane": lane}
                yield {"t": "tool_start", "call_id": tc["id"], "name": name, "args": args, "label": tmsg["label"],
                       "category": "meta", "needs_approval": False, "lane": lane}
                turn.history.append(tmsg)
                yield {"t": "tool_result", "message": tmsg}
                yield {"t": "exposure", "exposed": list(turn.exposed)}
                continue
            box = {}
            async for ev in exec_tool(turn, tc["id"], name, args, lane, out=box):
                yield ev
            turn.history.append(box["msg"])
            if turn.cancel.is_set():
                return
    yield {"t": "notice", "text": f"Stopped after {max_steps} tool steps (Settings → Agent max steps)."}


async def _run_parallel(turn, batch, lane):
    """Run read-only calls at the same time; their events stream as they come, their results enter the history in
    the order the model asked for them."""
    q = asyncio.Queue()
    boxes = [{} for _ in batch]

    async def one(k, tc, name, args):
        try:
            async for ev in exec_tool(turn, tc["id"], name, args, lane, out=boxes[k]):
                await q.put(ev)
        except Exception as e:  # noqa: BLE001
            boxes[k]["msg"] = {"role": "tool", "tool_call_id": tc["id"], "name": name, "content": f"{type(e).__name__}: {e}",
                               "error": True, "label": "", "lane": lane}
            await q.put({"t": "tool_result", "message": boxes[k]["msg"]})
        finally:
            await q.put(None)
    tasks = [asyncio.create_task(one(k, tc, n, a)) for k, (tc, n, a) in enumerate(batch)]
    left = len(tasks)
    while left:
        ev = await q.get()
        if ev is None:
            left -= 1
            continue
        yield ev
    for k, (tc, name, _a) in enumerate(batch):
        msg = boxes[k].get("msg") or {"role": "tool", "tool_call_id": tc["id"], "name": name, "content": "Not run.",
                                      "error": True, "label": "", "lane": lane}
        turn.history.append(msg)


# ------------------------------------------------------------------------------------------------ questions

async def ask_user(turn, call_id, args, lane="local"):
    """ask_user: post a question card and return at once (clarifications.py)."""
    args = args if isinstance(args, dict) else {}
    who = getattr(turn, "sub_name", "")
    label = str(args.get("question") or "")[:160]
    yield {"t": "tool_start", "call_id": call_id, "name": "ask_user", "args": args, "label": label, "category": "ask",
           "needs_approval": False, "lane": lane}
    pol = tools.policy("ask_user", turn.settings)
    try:
        if pol == "off":
            raise ValueError("Questions are turned off in Settings → Tools. Pick a sensible default and say which.")
        q, created = clarifications.ask(_root(turn).chat_id, args.get("question"), args.get("choices"),
                                        args.get("kind") or "", args.get("free_text", True), owner=who)
        if created:
            yield {"t": "question", "question": clarifications.public(q), "lane": lane}
            node = turn.graph.add(f"Question: {q['text']}", "ask_user", label=f"Asked you: {q['text']}", lane=lane)
            turn.graph.set(node.id, "waiting_for_user", waiting_on=q["id"])
            ev = _node_event(turn, node)
            if ev:
                yield ev
        text = (f"Asked the user (question id {q['id']}): {q['text']}" +
                (f" Choices: {', '.join(q['choices'])}." if q["choices"] else "") +
                ("" if created else " (already asked; still the same question)") +
                "\nKeep working on everything that doesn't depend on the answer. Don't open, read or change anything "
                "that depends on it until you have it: call get_answer with this id when you need it.")
        if q["status"] == "answered":
            text = f"The user already answered question {q['id']}: {q['answer']}"
        err = False
    except ValueError as e:
        text, err = str(e), True
    tmsg = {"role": "tool", "tool_call_id": call_id, "name": "ask_user", "content": text, "error": err,
            "label": label, "lane": lane}
    turn.history.append(tmsg)
    yield {"t": "tool_result", "message": tmsg}


def _q_node(turn, qid):
    for n in turn.graph.nodes.values():
        if n.action == "ask_user" and n.waiting_on == qid:
            return n
    return None


async def get_answer(turn, call_id, args, lane="local"):
    """get_answer: the answer, waiting for it when needed (Stop still works)."""
    args = args if isinstance(args, dict) else {}
    qid = str(args.get("id") or "").strip()
    yield {"t": "tool_start", "call_id": call_id, "name": "get_answer", "args": args, "label": qid, "category": "ask",
           "needs_approval": False, "lane": lane}
    q = clarifications.get(qid)
    err = False
    if q is None or q.get("chat_id") != _root(turn).chat_id:
        text, err = f"No question with id '{qid}' in this chat. Ask with ask_user first.", True
    elif q["status"] == "answered":
        text = f"The user answered: {q['answer']}"
    elif q["status"] != "pending":
        text, err = f"That question was {q['status']} without an answer.", True
    elif args.get("wait") is False:
        text = "No answer yet. Keep working on other parts and check again later."
    else:
        yield {"t": "waiting", "question_id": qid, "text": q["text"], "lane": lane}
        ans = await clarifications.wait(qid, turn.cancel, timeout=900)
        if ans is not None:
            text = f"The user answered: {ans}"
        elif turn.cancel.is_set():
            text, err = "Stopped while waiting for the answer.", True
        else:
            text = ("No answer after 15 minutes. Finish what you can, say clearly what is waiting on the user's "
                    "answer, and stop there; when they answer, this chat continues from it.")
    n = _q_node(turn, qid)
    if n is not None and n.state == "waiting_for_user":
        q2 = clarifications.get(qid) or {}
        if q2.get("status") == "answered":
            turn.graph.set(n.id, "completed", result=f"Answered: {q2.get('answer')}")
            ev = _node_event(turn, n)
            if ev:
                yield ev
    tmsg = {"role": "tool", "tool_call_id": call_id, "name": "get_answer", "content": text, "error": err,
            "label": qid, "lane": lane}
    turn.history.append(tmsg)
    yield {"t": "tool_result", "message": tmsg}


# ------------------------------------------------------------------------------------------------ subagents

def _final_reply(history):
    return next((m.get("content") for m in reversed(history) if m.get("role") == "assistant"
                 and not m.get("tool_calls") and (m.get("content") or "").strip()), "") or ""


def _lean(msgs, per_tool=4000):
    """A subagent's transcript as stored in the chat: tool outputs cut, images kept by id."""
    out = []
    for m in msgs:
        m = dict(m)
        if m.get("role") == "tool" and len(m.get("content") or "") > per_tool:
            m["content"] = m["content"][:per_tool] + "\n... [cut]"
        out.append(m)
    return out


async def run_subagent(turn, call_id, args, lane="local"):
    """The run_subagent tool: a fresh copy of the local model does one part of the task and reports back. Its steps
    stream to the UI tagged with "sub"; the parent only gets the report as the tool result."""
    args = args if isinstance(args, dict) else {}
    task = str(args.get("task") or "").strip()
    name = agents.clean_name(args.get("name")) or agents.name_from_text(task)
    label = f"{name}: {task}"[:160]
    pol = tools.policy("run_subagent", turn.settings)
    needs = pol == "ask" and "agents" not in _chat_allow.get(turn.chat_id, set())
    yield {"t": "tool_start", "call_id": call_id, "name": "run_subagent", "args": args, "label": label,
           "category": "agents", "needs_approval": needs and bool(task), "lane": lane}
    err = None
    if getattr(turn, "is_sub", False):
        err = "A subagent can't start its own subagents. Do this part yourself."
    elif not task:
        err = "run_subagent needs a task: the full brief for the subagent (it can't see this chat)."
    elif pol == "off":
        err = "Subagents are turned off in Settings → Tools."
    elif needs:
        fut = _ask(call_id, _root(turn).chat_id)
        if await fut == "deny" or turn.cancel.is_set():
            err = "The user denied starting this subagent. Do the work yourself or ask what they want."
    if err:
        tmsg = {"role": "tool", "tool_call_id": call_id, "name": "run_subagent", "content": err, "error": True,
                "label": label, "lane": lane}
        turn.history.append(tmsg)
        yield {"t": "tool_result", "message": tmsg}
        return
    sid = uuid.uuid4().hex[:8]
    rec = {"role": "subagent", "id": sid, "name": name, "task": task, "call_id": call_id, "lane": lane,
           "model": turn.model_name, "status": "working", "ts": time.time()}
    yield {"t": "subagent_start", "sub": dict(rec)}
    sub = SubTurn(turn, sid, name, task)
    failure = ""
    try:
        async for ev in run_local(sub, lane="local", max_steps=int(turn.settings.get("subagent_max_steps") or 20)):
            yield {**ev, "sub": sid}
    except Exception as e:  # noqa: BLE001  (a failed subagent is reported to the parent, not fatal)
        failure = f"{type(e).__name__}: {e}"[:600]
    report = _final_reply(sub.history)
    status = "stopped" if turn.cancel.is_set() else "error" if failure else "done"
    rec.update(status=status, result=report or failure, ended=time.time(), messages=_lean(sub.history),
               steps=sum(1 for m in sub.history if m.get("role") == "tool"))
    turn.history.append(rec)
    yield {"t": "subagent_done", "sub": rec}
    if status == "done":
        text = f"Report from subagent {name}:\n{report or '(it finished without a report)'}"
    elif status == "stopped":
        text = f"Subagent {name} was stopped before it finished." + (f" Its last words:\n{report}" if report else "")
    else:
        text = f"Subagent {name} failed: {failure}" + (f"\nIts last words:\n{report}" if report else "")
    tmsg = {"role": "tool", "tool_call_id": call_id, "name": "run_subagent", "content": text,
            "error": status != "done", "label": label, "lane": lane}
    turn.history.append(tmsg)
    yield {"t": "tool_result", "message": tmsg}
