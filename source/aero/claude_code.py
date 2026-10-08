"""Claude through the user's own Claude plan (Pro / Max / Team), by driving the official, unmodified Claude Code
program (claude.exe) headless with Anthropic's claude-agent-sdk.

The rules this module keeps, from Anthropic's published terms for personal use of `claude -p` and the Agent SDK
with a subscription (code.claude.com/docs/en/legal-and-compliance, support.claude.com articles 15036540, 13189465):
- Sign-in happens only in Anthropic's own flow: `claude auth login` opens claude.ai in the browser. Aero has no
  login form of its own.
- Aero never reads, copies or stores the Claude Code login token. It only runs `claude auth status`, which
  reports whether you are signed in, and reads the account fields that command prints.
- The binary is not modified and no client identity is faked. Usage counts against the plan's normal limits.
- An Anthropic API key in the environment would take priority over the plan login, so it is blanked for the child.

Fable reviews with read-only tools; Opus executes with Claude Code's own tools plus Aero's screen, app,
browser and MCP tools, and every tool that would need permission goes through Aero's approval cards.
"""
import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from . import agent, cloud, tools
from .config import IS_WIN, VERSION

_NO_WINDOW = 0x08000000 if IS_WIN else 0

# Claude Code's built-in tools, mapped to Aero's permission categories (Settings → Tools).
CC_CATEGORY = {
    "Bash": "shell", "PowerShell": "shell", "BashOutput": "shell", "KillShell": "shell", "KillBash": "shell",
    "Edit": "files_write", "MultiEdit": "files_write", "Write": "files_write", "NotebookEdit": "files_write",
    "Read": "files_read", "Glob": "files_read", "Grep": "files_read", "LS": "files_read",
    "WebSearch": "web", "WebFetch": "web",
}
CC_DESCRIPTIONS = {
    "Bash": "Runs a shell command (Git Bash) on your PC.",
    "PowerShell": "Runs a PowerShell command on your PC.",
    "BashOutput": "Reads output from a command running in the background.",
    "KillShell": "Stops a background command.",
    "Read": "Reads a file (text, images, PDFs, notebooks).",
    "Edit": "Replaces an exact piece of text in a file.",
    "MultiEdit": "Makes several exact replacements in one file.",
    "Write": "Creates or overwrites a whole file.",
    "NotebookEdit": "Edits a cell in a Jupyter notebook.",
    "Glob": "Finds files by name pattern, like **/*.py.",
    "Grep": "Searches file contents with a regular expression (ripgrep).",
    "WebSearch": "Searches the web and returns result snippets.",
    "WebFetch": "Downloads a web page and reads it.",
    "Agent": "Starts a sub-agent to work on part of the task in its own context.",
    "Task": "Starts a sub-agent to work on part of the task in its own context.",
    "TodoWrite": "Keeps Claude's step-by-step to-do list for the task.",
    "Skill": "Loads one of your Claude Code skills.",
    "submit_review": "The reviewer's verdict: ok, minor fixes or major re-plan.",
}
READ_ONLY_BUILTINS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch"]
# Aero tools Claude Code has no equivalent for (its own Read/Edit/Bash/Web tools cover the rest).
EXEC_CATEGORIES = {"screen", "desktop", "browser", "memory", "mcp"}
REVIEW_CATEGORIES = {"screen"}
REVIEW_EXTRA = {"recall"}
MIN_VERSION = (2, 1, 257)        # first Claude Code release with Fable 5.1

REVIEW_APPEND = cloud.REVIEW_SYSTEM.replace(
    "You are Claude Fable 5.1, the reviewer in Aero",
    "In this session you are not a coding assistant: you are Claude Fable 5.1, the reviewer in Aero").replace(
    "Finish by calling submit_review exactly once.", "Finish by calling the mcp__aero__submit_review tool exactly once.")
EXECUTE_APPEND = ("\n\nIn this session you run inside Aero, the user's local AI app, as Claude Opus 5.5 taking over a "
                  "task. Besides your usual tools you have Aero's tools (prefixed mcp__aero__) for looking at the "
                  "screen, controlling app windows and the browser, and the user's MCP servers.\n\n" + cloud.EXECUTE_SYSTEM)


# ------------------------------------------------------------------------------------------------ finding claude.exe

def sdk_installed():
    try:
        import claude_agent_sdk  # noqa: F401
        return True
    except Exception:
        return False


def cli_path(settings=None):
    """The official Claude Code binary: an explicit path from settings, the one bundled with claude-agent-sdk,
    the native installer's claude.exe, or claude on PATH (npm's claude.cmd shim is never used)."""
    p = ((settings or {}).get("claude_cli_path") or "").strip().strip('"')
    if p and Path(p).is_file():
        return p
    try:
        import claude_agent_sdk
        b = Path(claude_agent_sdk.__file__).parent / "_bundled" / ("claude.exe" if IS_WIN else "claude")
        if b.is_file():
            return str(b)
    except Exception:
        pass
    for cand in ([str(Path.home() / ".local" / "bin" / "claude.exe"), shutil.which("claude.exe")] if IS_WIN
                 else [shutil.which("claude"), str(Path.home() / ".local" / "bin" / "claude")]):
        if cand and Path(cand).is_file() and not cand.lower().endswith((".cmd", ".bat")):
            return cand
    return None


def _env():
    """Child environment for plan mode: an API key would outrank the plan login, so blank it."""
    e = {k: v for k, v in os.environ.items()}
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        e.pop(k, None)
    e.pop("CLAUDECODE", None)
    return e


def _run_cli(args, timeout=30):
    exe = cli_path(_settings())
    if not exe:
        raise RuntimeError("Claude Code is not installed.")
    return subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=_env(), creationflags=_NO_WINDOW)


def _settings():
    from .config import load_settings
    return load_settings()


def version():
    try:
        out = _run_cli(["--version"], 20).stdout.strip()
        return out.split()[0] if out else None
    except Exception:
        return None


def _vtuple(v):
    try:
        return tuple(int(x) for x in v.split(".")[:3])
    except Exception:
        return (0, 0, 0)


# Only these fields of `claude auth status --json` are passed on; nothing token-like ever leaves this function.
_STATUS_FIELDS = ("loggedIn", "authMethod", "apiProvider", "email", "emailAddress", "organizationName",
                  "subscriptionType", "plan", "accountUuid")


def status():
    exe = cli_path(_settings())
    st = {"sdk": sdk_installed(), "cli": exe, "installed": bool(exe), "loggedIn": False}
    if not exe:
        return st
    if _settings().get("strict_offline"):
        st["offline"] = True            # `claude auth status` may contact Anthropic; skip it while offline
        return st
    v = version()
    st["version"] = v
    st["version_ok"] = bool(v) and _vtuple(v) >= MIN_VERSION
    try:
        p = _run_cli(["auth", "status", "--json"], 30)
        d = json.loads(p.stdout or "{}")
        for k in _STATUS_FIELDS:
            if k in d and isinstance(d[k], (str, bool, int)) and "token" not in str(k).lower():
                st[k] = d[k]
        st["email"] = st.get("email") or st.pop("emailAddress", None)
    except Exception as e:  # noqa: BLE001
        st["error"] = f"{type(e).__name__}: {e}"[:300]
    return st


def login():
    """Open Anthropic's own sign-in (claude auth login → claude.ai in the browser) in a console window."""
    from . import localonly
    localonly.guard("signing in to Claude")
    exe = cli_path(_settings())
    if not exe:
        raise RuntimeError("Claude Code is not installed. Re-run Update-Aero.bat, or install it from claude.ai/code.")
    if IS_WIN:
        subprocess.Popen([exe, "auth", "login", "--claudeai"], env=_env(), creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        subprocess.Popen([exe, "auth", "login", "--claudeai"], env=_env())
    return {"started": True}


def logout():
    p = _run_cli(["auth", "logout"], 30)
    return {"ok": p.returncode == 0, "output": (p.stdout or p.stderr or "").strip()[:300]}


# ------------------------------------------------------------------------------------------------ helpers

def _patch_spawn():
    """claude.exe is a console program; Aero runs windowless (pythonw), so spawn it without a console window."""
    if not IS_WIN:
        return
    import anyio
    if getattr(anyio.open_process, "_aero", False):
        return
    orig = anyio.open_process

    async def open_process(*a, **kw):
        kw.setdefault("creationflags", _NO_WINDOW)
        return await orig(*a, **kw)
    open_process._aero = True
    anyio.open_process = open_process


def _label(name, inp):
    inp = inp or {}
    for k in ("command", "file_path", "path", "pattern", "url", "query", "notebook_path", "description"):
        v = inp.get(k)
        if isinstance(v, str) and v:
            return (v if len(v) <= 90 else v[:87] + "…").replace("\n", " ")
    return ""


def _text_of(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    out = []
    for b in content:
        if isinstance(b, dict):
            if b.get("type") == "text":
                out.append(b.get("text") or "")
            elif b.get("type") == "image":
                out.append("[image]")
    return "\n".join(out)


def _aero_tool_names(settings, read_only):
    pol = settings.get("tool_policy", {})
    cats = REVIEW_CATEGORIES if read_only else EXEC_CATEGORIES
    return [n for n, t in tools.REGISTRY.items()
            if (t.category in cats or (read_only and n in REVIEW_EXTRA)) and pol.get(t.category, "ask") != "off"]


def _sdk_tools(turn, lane, names, q, allowed_categories):
    """Expose Aero tools to Claude Code as an in-process MCP server. exec_tool does the approval and the
    UI events itself, so these are pre-allowed on the Claude Code side."""
    from claude_agent_sdk import tool as sdk_tool
    out = []
    for s in tools.schemas(turn.settings):
        f = s["function"]
        n = f["name"]
        if n not in names:
            continue

        async def handler(args, _n=n):
            call_id = "cc" + uuid.uuid4().hex[:10]
            o = {}
            async for ev in agent.exec_tool(turn, call_id, _n, args, lane, allowed_categories=allowed_categories, out=o):
                await q.put(ev)
            turn.history.append(o["msg"])
            res = o["res"]
            content = [{"type": "text", "text": res.get("text") or "(no output)"}]
            if res.get("image"):
                try:
                    from . import attachments
                    head, data = attachments.image_data_url(res["image"]).split(",", 1)
                    content.append({"type": "image", "data": data, "mimeType": head[5:].split(";")[0]})
                except Exception:
                    pass
            r = {"content": content}
            if res.get("error"):
                r["is_error"] = True
            return r
        out.append(sdk_tool(n, (f["description"] or n)[:1024], f["parameters"] or {"type": "object", "properties": {}})(handler))
    return out


def _user_message(blocks):
    """A stream-json user message from Anthropic content blocks (text and base64 images)."""
    return {"type": "user", "message": {"role": "user", "content": blocks}, "parent_tool_use_id": None}


async def _one(msg):
    yield msg


# ------------------------------------------------------------------------------------------------ the session

async def _session(turn, lane, model, append, blocks, effort, read_only, max_turns, extra_tools=(), nudge=None):
    """Run one Claude Code session and yield Aero UI events. Returns when the session's result arrives."""
    _patch_spawn()
    import warnings
    warnings.filterwarnings("ignore", message="can_use_tool will not be invoked")  # intended: exec_tool gates those
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow,
                                  PermissionResultDeny, RateLimitEvent, ResultMessage, StreamEvent, SystemMessage,
                                  TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock, UserMessage,
                                  create_sdk_mcp_server)
    s = turn.settings
    q = asyncio.Queue()
    END = object()
    loop = asyncio.get_running_loop()
    allowed_cats = (cloud.READ_ONLY_CATEGORIES | {"memory"}) if read_only else None
    names = _aero_tool_names(s, read_only)
    htools = _sdk_tools(turn, lane, names, q, allowed_cats) + list(extra_tools)
    pol = s.get("tool_policy", {})

    disallowed = []
    if read_only:
        builtins = list(READ_ONLY_BUILTINS)
        if pol.get("web", "auto") == "off":
            builtins = [b for b in builtins if CC_CATEGORY.get(b) != "web"]
    else:
        builtins = {"type": "preset", "preset": "claude_code"}
        for b, c in CC_CATEGORY.items():
            if pol.get(c, "ask") == "off":
                disallowed.append(b)

    pending_denied = set()

    async def can_use(name, inp, ctx):
        if name.startswith("mcp__aero__"):
            return PermissionResultAllow(updated_input=inp)
        cat = CC_CATEGORY.get(name) or ("mcp" if name.startswith("mcp__") else None)
        if read_only and name not in READ_ONLY_BUILTINS:
            return PermissionResultDeny(message="The reviewer has read-only tools.")
        p = pol.get(cat, "ask") if cat else "auto"
        if p == "off":
            return PermissionResultDeny(message=f"{name} is turned off in Aero's settings.")
        if p == "auto" or (cat and cat in agent._chat_allow.get(turn.chat_id, set())):
            return PermissionResultAllow(updated_input=inp)
        call_id = getattr(ctx, "tool_use_id", None) or ("cc" + uuid.uuid4().hex[:10])
        await q.put({"t": "tool_start", "call_id": call_id, "name": name, "args": inp, "label": _label(name, inp),
                     "category": cat, "needs_approval": True, "lane": lane})
        fut = loop.create_future()
        agent._approvals[call_id] = fut
        decision = await fut
        if decision == "deny" or turn.cancel.is_set():
            pending_denied.add(call_id)
            return PermissionResultDeny(message="The user denied this action. Try another way or explain.")
        return PermissionResultAllow(updated_input=inp)

    stderr_tail = []

    def on_stderr(line):
        stderr_tail.append(line)
        del stderr_tail[:-20]

    opts = ClaudeAgentOptions(
        model=model,
        system_prompt={"type": "preset", "preset": "claude_code", "append": append},
        tools=builtins,
        disallowed_tools=disallowed,
        allowed_tools=[f"mcp__aero__{t.name}" for t in htools],
        mcp_servers={"aero": create_sdk_mcp_server("aero", VERSION, htools)} if htools else {},
        permission_mode="default",
        can_use_tool=can_use,
        cwd=s.get("work_dir") or str(Path.home()),
        include_partial_messages=True,
        thinking={"type": "adaptive", "display": "summarized"},
        effort=effort,
        max_turns=max_turns,
        setting_sources=[] if read_only else ["user"],
        env={"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": "", "CLAUDE_AGENT_SDK_CLIENT_APP": f"aero/{VERSION}"},
        cli_path=cli_path(s),
        strict_mcp_config=read_only,
        stderr=on_stderr,
    )
    state = {"msg": None, "tools": [], "t0": None, "first": None, "result": None, "calls": {}, "noted": {}}

    def start_msg(mid=None):
        m = {"role": "assistant", "content": "", "reasoning": "", "id": uuid.uuid4().hex[:10], "lane": lane,
             "model": cloud.NAMES.get(model, model), "api_id": mid}
        state.update(msg=m, tools=[], t0=time.time(), first=None)
        return {"t": "assistant_start", "id": m["id"], "lane": lane, "model": m["model"]}

    async def finish_msg(usage=None, stop_reason=None):
        m = state["msg"]
        if not m:
            return
        state["msg"] = None
        if state["tools"]:
            m["tool_calls"] = [{"id": b.id, "type": "function", "function": {"name": b.name, "arguments": json.dumps(b.input)}}
                               for b in state["tools"]]
        dt = time.time() - (state["t0"] or time.time())
        st = {"time": round(dt, 1), "finish": stop_reason, "via": "plan"}
        if state["first"]:
            st["ttft"] = round(state["first"] - state["t0"], 2)
        if usage:
            st["prompt_tokens"] = (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0) + \
                                  (usage.get("cache_creation_input_tokens") or 0)
            st["completion_tokens"] = usage.get("output_tokens") or 0
            st["cached_tokens"] = usage.get("cache_read_input_tokens") or 0
        m["stats"] = st
        if not m["reasoning"]:
            m.pop("reasoning")
        turn.history.append(m)
        await q.put({"t": "assistant_done", "message": m})
        for b in state["tools"]:
            if b.name.startswith("mcp__aero__"):
                continue
            state["calls"][b.id] = (b.name, _label(b.name, b.input))
            await q.put({"t": "tool_start", "call_id": b.id, "name": b.name, "args": b.input, "label": _label(b.name, b.input),
                         "category": CC_CATEGORY.get(b.name) or ("mcp" if b.name.startswith("mcp__") else None),
                         "needs_approval": False, "lane": lane})

    async def pump(client):
        usage_by_msg = {}
        async for m in client.receive_response():
            if isinstance(m, StreamEvent):
                if m.parent_tool_use_id:
                    continue
                ev = m.event or {}
                et = ev.get("type")
                if et == "message_start":
                    if state["msg"]:
                        await finish_msg()
                    mm = ev.get("message") or {}
                    await q.put(start_msg(mm.get("id")))
                    usage_by_msg["cur"] = dict(mm.get("usage") or {})
                elif et == "content_block_delta":
                    d = ev.get("delta") or {}
                    if not state["msg"]:
                        await q.put(start_msg())
                    if d.get("type") == "thinking_delta" and d.get("thinking"):
                        state["first"] = state["first"] or time.time()
                        state["msg"]["reasoning"] += d["thinking"]
                        await q.put({"t": "reasoning", "d": d["thinking"], "lane": lane})
                    elif d.get("type") == "text_delta" and d.get("text"):
                        state["first"] = state["first"] or time.time()
                        state["msg"]["content"] += d["text"]
                        await q.put({"t": "content", "d": d["text"], "lane": lane})
                elif et == "content_block_start":
                    cb = ev.get("content_block") or {}
                    if cb.get("type") == "tool_use":
                        await q.put({"t": "tool_pending", "name": cb.get("name"), "lane": lane})
                elif et == "message_delta":
                    u = usage_by_msg.setdefault("cur", {})
                    u.update({k: v for k, v in (ev.get("usage") or {}).items() if v is not None})
                    usage_by_msg["stop"] = (ev.get("delta") or {}).get("stop_reason")
                elif et == "message_stop":
                    await finish_msg(usage_by_msg.pop("cur", None), usage_by_msg.pop("stop", None))
            elif isinstance(m, AssistantMessage):
                if m.parent_tool_use_id:
                    for b in m.content:
                        if isinstance(b, ToolUseBlock):
                            await q.put({"t": "notice", "lane": lane, "level": "info",
                                         "text": f"Sub-agent used {b.name} {_label(b.name, b.input)}".strip()})
                    continue
                if m.error:
                    await q.put({"t": "notice", "lane": lane, "level": "error",
                                 "text": f"Claude Code reported: {m.error}" + _hint(m.error)})
                if not state["msg"]:            # no partial stream for this message: render it whole
                    await q.put(start_msg(m.message_id))
                    for b in m.content:
                        if isinstance(b, ThinkingBlock) and b.thinking:
                            state["msg"]["reasoning"] += b.thinking
                            await q.put({"t": "reasoning", "d": b.thinking, "lane": lane})
                        elif isinstance(b, TextBlock) and b.text:
                            state["msg"]["content"] += b.text
                            await q.put({"t": "content", "d": b.text, "lane": lane})
                    state["tools"] += [b for b in m.content if isinstance(b, ToolUseBlock)]
                    await finish_msg(m.usage, m.stop_reason)
                else:
                    state["tools"] += [b for b in m.content if isinstance(b, ToolUseBlock)
                                       and b.id not in {t.id for t in state["tools"]}]
            elif isinstance(m, UserMessage):
                if m.parent_tool_use_id or not isinstance(m.content, list):
                    continue
                for b in m.content:
                    if not isinstance(b, ToolResultBlock):
                        continue
                    if b.tool_use_id not in state["calls"]:
                        continue                # a Aero tool: exec_tool already reported it
                    name, label = state["calls"].pop(b.tool_use_id)
                    text = _text_of(b.content)
                    tmsg = {"role": "tool", "tool_call_id": b.tool_use_id, "name": name, "content": text[:20000],
                            "error": bool(b.is_error), "label": label, "lane": lane}
                    if b.tool_use_id in pending_denied:
                        tmsg["denied"] = True
                    turn.history.append(tmsg)
                    await q.put({"t": "tool_result", "message": tmsg})
            elif isinstance(m, SystemMessage):
                if m.subtype == "init":
                    src = (m.data or {}).get("apiKeySource")
                    if src and src not in ("none", "/login managed key"):
                        await q.put({"t": "notice", "lane": lane, "level": "warn",
                                     "text": f"Claude Code is billing an API key ({src}), not your plan."})
                    bad = [e.get("name") for e in (m.data or {}).get("mcp_server_errors") or []]
                    if bad:
                        await q.put({"t": "notice", "lane": lane, "level": "info",
                                     "text": "Some of your Claude Code MCP servers did not start: " + ", ".join(bad)})
                elif m.subtype == "api_retry":
                    d = m.data or {}
                    await q.put({"t": "notice", "lane": lane, "level": "info",
                                 "text": f"Claude is busy; retrying ({d.get('attempt')}/{d.get('max_retries')})."})
                elif m.subtype == "compact_boundary":
                    await q.put({"t": "notice", "lane": lane, "level": "info", "text": "Claude Code compacted its context."})
            elif isinstance(m, RateLimitEvent):
                info = m.rate_limit_info
                st = getattr(info, "status", None)
                if st in ("allowed_warning", "rejected"):
                    when = getattr(info, "resets_at", None)
                    txt = "Your Claude plan's usage limit is " + ("reached" if st == "rejected" else "nearly reached")
                    if when:
                        try:
                            txt += " (resets " + time.strftime("%a %H:%M", time.localtime(float(when))) + ")"
                        except Exception:
                            pass
                    await q.put({"t": "notice", "lane": lane, "level": "warn" if st != "rejected" else "error",
                                 "text": txt + "."})
            elif isinstance(m, ResultMessage):
                state["result"] = m
                if state["msg"]:
                    await finish_msg()
                row = _note(m, model, state["noted"])
                await q.put({"t": "cloud_usage", "model": model, "via": "plan", "usage": row,
                             "today_usd": cloud.spent_today()})
                if m.is_error:
                    errs = "; ".join((m.errors or [])[:3]) or (m.result or m.subtype)
                    await q.put({"t": "notice", "lane": lane, "level": "error",
                                 "text": f"Claude Code stopped: {errs}"[:500] + _hint(errs)})
                elif m.subtype == "error_max_turns":
                    await q.put({"t": "notice", "lane": lane, "text": f"Stopped after {max_turns} steps."})

    async def run():
        try:
            async with ClaudeSDKClient(options=opts) as client:
                state["client"] = client
                await client.query(_one(_user_message(blocks)))
                await pump(client)
                if nudge and nudge() and not turn.cancel.is_set():
                    await client.query(_one(_user_message([{"type": "text", "text": nudge()}])))
                    await pump(client)
        except Exception as e:  # noqa: BLE001
            tail = " | ".join(x.strip() for x in stderr_tail[-3:] if x.strip())
            await q.put({"t": "notice", "lane": lane, "level": "error",
                         "text": (f"Claude Code failed: {type(e).__name__}: {e}" + (f" ({tail})" if tail else ""))[:600]
                         + _hint(str(e) + tail)})
        finally:
            await q.put(END)

    task = asyncio.create_task(run())
    try:
        while True:
            get = asyncio.create_task(q.get())
            while not get.done():
                done, _ = await asyncio.wait({get}, timeout=0.25)
                if turn.cancel.is_set() and state.get("client") and not state.get("interrupted"):
                    state["interrupted"] = True
                    try:
                        await state["client"].interrupt()
                    except Exception:
                        pass
            ev = get.result()
            if ev is END:
                break
            yield ev
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass


def _hint(text):
    t = (text or "").lower()
    if any(k in t for k in ("authentication", "not logged", "login", "oauth", "401")):
        return " Sign in again under Settings → Claude → Claude plan."
    if "model_not_found" in t or "model not found" in t:
        return " Your plan may not include this model; pick another in Settings → Claude."
    if "credits_required" in t or "billing" in t:
        return " This model needs usage credits on your plan."
    return ""


def _note(m, model, noted):
    """Plan usage is tracked like API usage, but its dollar figure is Claude Code's own estimate and is not billed,
    so it is kept apart from the daily API budget. Result totals are cumulative within a session: note the delta."""
    rows = {}
    for mid, v in (m.model_usage or {}).items():
        v = v if isinstance(v, dict) else {}
        rows[mid] = {"input": v.get("inputTokens") or 0, "output": v.get("outputTokens") or 0,
                     "cache_read": v.get("cacheReadInputTokens") or 0, "cache_write": v.get("cacheCreationInputTokens") or 0,
                     "est_usd": float(v.get("costUSD") or 0)}
    if not rows:
        u = m.usage or {}
        rows[model] = {"input": u.get("input_tokens") or 0, "output": u.get("output_tokens") or 0,
                       "cache_read": u.get("cache_read_input_tokens") or 0,
                       "cache_write": u.get("cache_creation_input_tokens") or 0, "est_usd": float(m.total_cost_usd or 0)}
    total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "usd": 0.0, "est_usd": 0.0}
    for mid, r in rows.items():
        prev = noted.get(mid) or {}
        d = {k: max(0, r[k] - prev.get(k, 0)) for k in r}
        noted[mid] = r
        d["usd"] = 0.0
        if d["input"] or d["output"] or d["cache_read"] or d["cache_write"]:
            cloud.note_usage(mid + " (plan)", d)
        for k in total:
            total[k] += d.get(k, 0)
    total["est_usd"] = round(total["est_usd"], 4)
    return total


# ------------------------------------------------------------------------------------------------ review / execute

def _blocks(content):
    return [b for b in content if b.get("type") in ("text", "image")]


async def review(turn, round_no=1, previous=None):
    from claude_agent_sdk import tool as sdk_tool
    s = turn.settings
    model = s.get("review_model") or "claude-fable-5-1"
    turn.cloud_result = None
    schema = cloud.SUBMIT_REVIEW["input_schema"]

    @sdk_tool("submit_review", cloud.SUBMIT_REVIEW["description"], schema)
    async def submit(args):
        v = args if isinstance(args, dict) else {}
        if v.get("verdict") not in ("ok", "minor", "major"):
            return {"content": [{"type": "text", "text": "verdict must be ok, minor or major"}], "is_error": True}
        why = cloud.validate(schema, v)
        if why:
            return {"content": [{"type": "text", "text": why}], "is_error": True}
        turn.cloud_result = {k: v.get(k) or ([] if k == "issues" else "") for k in schema["properties"]}
        return {"content": [{"type": "text", "text": "Review recorded. You are done; end your turn now."}]}

    def nudge():
        return None if turn.cloud_result else "Now call mcp__aero__submit_review with your verdict."

    yield {"t": "lane", "lane": "fable", "phase": "review", "round": round_no, "model": cloud.NAMES.get(model, model),
           "via": "plan"}
    async for ev in _session(turn, "fable", model, cloud.with_profile(turn, REVIEW_APPEND, "review"), _blocks(cloud.review_packet(turn, round_no, previous)),
                             s.get("review_effort") or "high", True, 24, [submit], nudge):
        yield ev


async def execute(turn, rv):
    s = turn.settings
    model = s.get("fix_model") or "claude-opus-5-5"
    yield {"t": "lane", "lane": "opus", "phase": "execute", "model": cloud.NAMES.get(model, model), "via": "plan"}
    async for ev in _session(turn, "opus", model, cloud.with_profile(turn, EXECUTE_APPEND, "execute"), _blocks(cloud.execute_packet(turn, rv)),
                             s.get("fix_effort") or "high", False, int(s.get("agent_max_steps") or 40)):
        yield ev
