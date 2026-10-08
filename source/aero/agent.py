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
import difflib
import getpass
import json
import platform
import re
import time
import uuid
from pathlib import Path

import httpx

from . import attachments, memory, stats, tools
from .config import IS_WIN

_approvals = {}        # call_id -> Future
_chat_allow = {}       # chat_id -> set(categories) allowed for the rest of the chat
_cancel = {}           # chat_id -> asyncio.Event

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
WRITE_TOOLS = {"write_file", "edit_file", "move_path", "delete_path"}


def resolve_approval(call_id, decision, chat_id=None, category=None):
    fut = _approvals.pop(call_id, None)
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
        if not fut.done():
            fut.set_result("deny")


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
    return (f"\n\n# Environment\n- OS: {platform.system()} {platform.release()}"
            f"{' (PowerShell is the shell)' if IS_WIN else ''}\n- User: {getpass.getuser()}\n"
            f"- Working directory: {settings.get('work_dir')}\n- Primary screen: {_screen_size()}\n"
            f"- Vision: {'yes, you can see images and screenshots' if vision else 'no (text only)'}\n"
            f"- Context window: {ctx_size:,} tokens\n"
            "- Each user message ends with a <turn_context> block (time sent, the router's plan, relevant "
            "memories). It is written by Aero, not typed by the user.")


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
        if r in ("router", "review", "lesson", "notice"):
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
        self.tctx = tools.Ctx(settings, self.vision, chat_id)
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

    def rebind(self, new_id):
        self.chat_id = new_id
        _cancel[new_id] = self.cancel
        self.tctx.chat_id = new_id

    def close(self):
        for k in [k for k, v in _cancel.items() if v is self.cancel]:
            _cancel.pop(k, None)


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


async def exec_tool(turn, call_id, name, args, lane="local", allowed_categories=None, out=None):
    """Run one tool call with the user's approval policy. Yields UI events; the result lands in out['res']."""
    t = tools.REGISTRY.get(name)
    category = t.category if t else None
    pol = tools.policy(name, turn.settings)
    if allowed_categories is not None and category not in allowed_categories:
        pol = "off"
    needs = pol == "ask" and category not in _chat_allow.get(turn.chat_id, set())
    label = tools.label(name, args or {})
    yield {"t": "tool_start", "call_id": call_id, "name": name, "args": args, "label": label, "category": category,
           "needs_approval": needs and args is not None, "lane": lane}
    if args is None:
        res = {"text": "Could not parse the arguments as JSON. Send valid JSON matching the tool's schema.", "error": True}
    elif pol == "off" or not t:
        res = {"text": f"Tool '{name}' is not available here.", "error": True}
    else:
        decision = "allow"
        if needs:
            fut = asyncio.get_running_loop().create_future()
            _approvals[call_id] = fut
            decision = await fut
        if decision == "deny" or turn.cancel.is_set():
            res = {"text": "The user denied this action. Ask what they want instead, or try another approach.",
                   "error": True, "denied": True}
        else:
            if name in WRITE_TOOLS:
                _track(turn, name, args, before=True)
            res = await asyncio.to_thread(tools.run, name, args, turn.tctx)
            if name in WRITE_TOOLS:
                _track(turn, name, args, before=False)
            stats.note_tool(category)
    tmsg = {"role": "tool", "tool_call_id": call_id, "name": name, "content": res.get("text", ""),
            "error": bool(res.get("error")), "image": res.get("image"), "label": label, "lane": lane}
    if res.get("denied"):
        tmsg["denied"] = True
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
    allv = tools.schemas(turn.settings)
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
    compact_at = float(settings.get("auto_compact_at") or 0)
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

        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
                if not isinstance(args, dict):
                    args = {"value": args}
            except Exception:
                args = None
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
