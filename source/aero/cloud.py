"""Claude in the cloud, reached one of two legitimate ways (Settings → Claude → Connection):
- "api": the official Anthropic API with the user's own Console API key (pay as you go).
- "plan": the user's Claude subscription, through the official Claude Code program signed in with Anthropic's own
  login (see claude_code.py). "auto" uses the API key when one is saved, otherwise the plan sign-in.

- Claude Fable 5.1 reviews the local model's finished work (read-only tools: it may open files, look at the
  screen and search the web to check claims) and returns a verdict through the submit_review tool.
- Claude Opus 5.5 takes over when a review finds major problems: it gets Fable's plan and runs with the same
  local tools as the local model, under the same approval rules.

Everything both models do streams into the chat on its own lane (thinking summaries, text, tool calls).
Spending is tracked per day in data/cloud_usage.json and capped by Settings → Claude → daily budget.
"""
import json
import time
import uuid

from . import agent, memory, tools, vault
from .config import read_store, write_store

KEY_NAME = "anthropic_api_key"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# USD per million tokens: input, output, cache read, 5-minute cache write
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-8": (5.0, 25.0, 0.50, 6.25),
}
NAMES = {"claude-fable-5-1": "Claude Fable 5.1", "claude-opus-5-5": "Claude Opus 5.5",
         "claude-sonnet-5-5": "Claude Sonnet 5.5", "claude-opus-5": "Claude Opus 5", "claude-opus-4-8": "Claude Opus 4.8"}

READ_ONLY_CATEGORIES = {"files_read", "web", "screen", "skills"}
READ_ONLY_EXTRA = {"recall"}

_clients = {}
_no_fallbacks = set()        # models whose requests rejected the fallbacks parameter (fall back to plain requests)


# ------------------------------------------------------------------------------------------------ key & client

def has_key():
    return bool(vault.get(KEY_NAME))


def key_status():
    k = vault.get(KEY_NAME)
    return {"set": bool(k), "masked": vault.mask(k) if k else ""}


def set_key(value):
    vault.put(KEY_NAME, (value or "").strip())
    _clients.clear()


def client():
    import anthropic
    k = vault.get(KEY_NAME)
    if not k:
        raise RuntimeError("No Anthropic API key. Add one in Settings → Claude.")
    if k not in _clients:
        _clients.clear()
        _clients[k] = anthropic.AsyncAnthropic(api_key=k, max_retries=3, timeout=600)
    return _clients[k]


async def test_key():
    """Cheapest possible authenticated call: list models. Returns the Claude models this key can use."""
    c = client()
    page = await c.models.list(limit=50)
    return [m.id for m in page.data]


# ------------------------------------------------------------------------------------------------ spend

def _today():
    return time.strftime("%Y-%m-%d")


def cost_of(model, inp, out, cache_read=0, cache_write=0):
    p = PRICES.get(model) or PRICES["claude-opus-5-5"]
    return (inp * p[0] + out * p[1] + cache_read * p[2] + cache_write * p[3]) / 1e6


def usage_cost(model, usage):
    """Cost of one response. With server-side fallbacks, usage.iterations lists every attempt with its model."""
    its = getattr(usage, "iterations", None) or []
    rows = []
    if its:
        for it in its:
            rows.append((getattr(it, "model", None) or model, it.input_tokens, it.output_tokens,
                         getattr(it, "cache_read_input_tokens", 0) or 0, getattr(it, "cache_creation_input_tokens", 0) or 0))
    else:
        rows.append((model, usage.input_tokens, usage.output_tokens, getattr(usage, "cache_read_input_tokens", 0) or 0,
                     getattr(usage, "cache_creation_input_tokens", 0) or 0))
    total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "usd": 0.0}
    for m, i, o, cr, cw in rows:
        total["input"] += i
        total["output"] += o
        total["cache_read"] += cr
        total["cache_write"] += cw
        total["usd"] += cost_of(m, i, o, cr, cw)
    return total


def note_usage(model, u):
    d = read_store("cloud_usage.json", {})
    day = d.setdefault(_today(), {})
    m = day.setdefault(model, {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "usd": 0.0})
    m["calls"] += 1
    for k in ("input", "output", "cache_read", "cache_write", "usd", "est_usd"):
        if k in u:
            m[k] = round(m.get(k, 0) + u[k], 6) if k.endswith("usd") else m.get(k, 0) + u[k]
    for old in sorted(d)[:-60]:          # keep two months
        d.pop(old, None)
    write_store("cloud_usage.json", d)


def spent_today(prefix=None):
    """API dollars spent today, for every provider or only models whose name starts with prefix ("claude", "gpt")."""
    day = read_store("cloud_usage.json", {}).get(_today(), {})
    return round(sum(v.get("usd", 0) for k, v in day.items() if not prefix or k.startswith(prefix)), 4)


def usage_summary():
    d = read_store("cloud_usage.json", {})
    day = d.get(_today(), {})
    month = time.strftime("%Y-%m")
    return {"today": day, "today_usd": spent_today(), "today_claude_usd": spent_today("claude"),
            "today_gpt_usd": spent_today("gpt"),
            "today_plan_est_usd": round(sum(v.get("est_usd", 0) for v in day.values()), 4),
            "month_usd": round(sum(v.get("usd", 0) for k, dd in d.items() if k.startswith(month) for v in dd.values()), 4),
            "days": {k: round(sum(v.get("usd", 0) for v in dd.values()), 4) for k, dd in sorted(d.items())[-14:]}}


def budget_ok(settings):
    if backend(settings) == "plan":
        return True                       # the plan has its own limits; nothing is billed per call
    cap = float(settings.get("cloud_daily_budget_usd") or 0)
    return cap <= 0 or spent_today("claude") < cap


def backend(settings):
    """Which connection to use for this request: 'api', 'plan' or None (nothing set up)."""
    want = settings.get("cloud_backend") or "auto"
    if want == "api":
        return "api" if has_key() else None
    from . import claude_code
    if want == "plan":
        return "plan" if claude_code.sdk_installed() and claude_code.cli_path(settings) else None
    if has_key():
        return "api"
    if claude_code.sdk_installed() and claude_code.cli_path(settings) and settings.get("claude_plan_signed_in"):
        return "plan"
    return None


def not_ready_text(settings):
    want = settings.get("cloud_backend") or "auto"
    if want == "plan":
        return "Review is on, but Claude Code is not installed. Re-run Update-Aero.bat."
    if want == "api":
        return "Review is on, but no Anthropic API key is saved. Add one in Settings → Claude."
    return "Review is on, but Claude is not connected. Sign in with your Claude plan or add an API key in Settings → Claude."


# ------------------------------------------------------------------------------------------------ tools

def _tool_defs(settings, names):
    out = []
    for s in tools.schemas(settings):
        f = s["function"]
        if f["name"] not in names:
            continue
        out.append({"name": f["name"], "description": f["description"][:1024] or f["name"],
                    "input_schema": f["parameters"] or {"type": "object", "properties": {}},
                    "eager_input_streaming": True})
    return out


def read_only_names(settings):
    pol = settings.get("tool_policy", {})
    return [n for n, t in tools.REGISTRY.items()
            if (t.category in READ_ONLY_CATEGORIES or n in READ_ONLY_EXTRA) and pol.get(t.category, "ask") != "off"]


def all_names(settings):
    """Every enabled tool for a takeover model, except subagents (those run the local model's own loop)."""
    pol = settings.get("tool_policy", {})
    return [n for n, t in tools.REGISTRY.items() if pol.get(t.category, "ask") != "off" and t.category != "agents"]


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


def validate(schema, args):
    """Light check of a streamed tool input (eager streaming means the server no longer validates it)."""
    if not isinstance(args, dict):
        return "input is not a JSON object"
    props = (schema or {}).get("properties") or {}
    for r in (schema or {}).get("required") or []:
        if r not in args:
            return f"missing required field '{r}'"
    for k, v in args.items():
        t = (props.get(k) or {}).get("type")
        types = t if isinstance(t, list) else [t] if t else []
        if types and v is not None and not any(isinstance(v, _TYPES.get(x, object)) and
                                              not (x in ("integer", "number") and isinstance(v, bool)) for x in types):
            return f"field '{k}' should be {t}"
    return None


SUBMIT_REVIEW = {
    "name": "submit_review",
    "description": "Submit your verdict on the local model's work. Call this exactly once, as your last action.",
    "strict": True,
    "input_schema": {
        "type": "object", "additionalProperties": False,
        "required": ["verdict", "summary", "issues", "plan", "improved_prompt"],
        "properties": {
            "verdict": {"type": "string", "enum": ["ok", "minor", "major"],
                        "description": "ok: correct and complete, nothing to fix. minor: mostly right; specific "
                                       "fixable problems the local model can repair itself. major: wrong approach, "
                                       "wrong or unverified result, broken changes, or missing large parts."},
            "summary": {"type": "string", "description": "Two to four sentences for the user: what was asked, what "
                                                         "was delivered, and your judgement."},
            "issues": {"type": "array", "description": "Every problem found, most severe first. Empty when ok.",
                       "items": {"type": "object", "additionalProperties": False,
                                 "required": ["severity", "where", "problem", "fix"],
                                 "properties": {"severity": {"type": "string", "enum": ["critical", "major", "minor", "nit"]},
                                                "where": {"type": "string"},
                                                "problem": {"type": "string"},
                                                "fix": {"type": "string"}}}},
            "plan": {"type": "string", "description": "minor: numbered steps for the local model to fix every issue "
                                                      "and verify. major: a complete re-plan of the whole task - the "
                                                      "approach, the methods and tools to use, every step, how to "
                                                      "verify, and what the final answer must contain. ok: empty."},
            "improved_prompt": {"type": "string", "description": "The user's request rewritten as a clearer, "
                                                                 "complete prompt that would have avoided the problems "
                                                                 "(empty when the original was already clear)."},
        },
    },
}

REVIEW_SYSTEM = """You are Claude Fable 5.1, the reviewer in Aero, a local AI agent app on the user's Windows PC. A local model running on the user's GPU has just worked on the user's request with real tools (files, PowerShell, apps, browser, web). You check its work before the user relies on it.

How to review
- Judge the result against what the user actually asked, not against what the local model claimed. Local models often claim success without verifying, invent file contents or command output, stop early, or answer a different question.
- Verify instead of trusting: you have read-only tools on the same PC (read files, list folders, search, look at the screen and windows, search and fetch the web). Use them to check claims that matter: open the changed files, confirm paths exist, check facts. Do not change anything; you cannot run commands or write files.
- Keep tool use proportional: a short factual answer needs a quick check at most; code changes deserve reading the changed files.
- Be specific: every issue names where it is (file:line, step, claim) and the concrete fix.

Verdicts
- ok: correct and complete for the request. Small style nits alone do not block an ok.
- minor: right approach, but specific problems the local model can fix itself with a clear plan (a bug, a missed requirement, an unverified step).
- major: the approach is wrong, the result is wrong or fabricated, changes are broken or risky, or large parts are missing. Your plan must then be a complete re-plan of the whole task that a stronger model (Claude Opus 5.5) will execute with the same tools: approach, methods, tools, steps, verification and the expected final answer.

Finish by calling submit_review exactly once. Do not write the verdict as plain text."""

EXECUTE_SYSTEM = """You are Claude Opus 5.5, working inside Aero, a local AI agent app on the user's Windows PC. The user's local model attempted a task and a reviewer (Claude Fable 5.1) found major problems and wrote a new plan. You now carry out the task yourself.

- Your tools run on the user's own PC (files, PowerShell, app windows, browser, web, MCP servers). Some need the user's approval; if one is denied, do not retry it, find another way or explain.
- Follow the reviewer's plan unless you find a better way; verify every step with your tools and never claim something worked unless a tool showed it.
- The local model's attempt may have left things half-done or broken: inspect the current state first and repair it.
- Never run destructive commands, type passwords or payment details, or send messages to other people unless the user asked for exactly that.
- Final answer, addressed to the user: lead with the result, then what you changed (paths, commands, values), then one short section "What the local model got wrong" with the key mistakes. Be direct and concise; use Markdown."""


def with_profile(turn, system, role):
    """The cloud prompt plus the user's profile (the same one the local model gets), so the reviewer judges
    against how the user likes things done and Opus writes its answer that way."""
    prof = agent.profile_for(turn.settings, turn.chat_id, turn.ctx_size)[0]
    if not prof:
        return system
    if role == "review":
        note = ("The user's profile below says how they like answers. Check the final answer against it too, but a "
                "style mismatch alone is a minor issue at most and never a reason for major.")
    else:
        note = "Write your final answer the way the user's profile below asks."
    return system + "\n\n" + note + "\n\n" + prof


# ------------------------------------------------------------------------------------------------ packets

def _clip(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n] + f"\n... [{len(s) - n:,} more characters cut]"


def _request_of(turn):
    hist = turn.history[:turn.start_index]
    u = next((m for m in reversed(hist) if m.get("role") == "user" and m.get("from") not in REVIEWER_FROM), {})
    text = u.get("content") or ""
    for p in u.get("pastes") or []:
        text += f"\n\n[pasted text: {p.get('name') or ''}]\n{_clip(p.get('text', ''), 6000)}"
    for a in u.get("attachments") or []:
        text += f"\n[attached {a.get('kind', 'file')}: {a.get('name')}]"
    if u.get("target_app"):
        text += f"\n[selected app window: {u['target_app'].get('title')}]"
    return text


REVIEWER_FROM = {"fable", "astra"}             # fix requests written by a reviewer, not by the user
WHO = {"opus": "CLAUDE OPUS", "sol": "GPT-6.1 SOL (ChatGPT, after GPT-6 Astra's review)", "local": "LOCAL MODEL"}


def _render_work(msgs, per_tool=1500, skip_lanes=("fable", "astra")):
    out = []
    for m in msgs:
        r, lane = m.get("role"), m.get("lane") or "local"
        if r in ("router", "review", "lesson", "notice") or lane in skip_lanes:
            continue
        who = WHO.get(lane, "LOCAL MODEL")
        if r == "user":
            tag = ("REVIEWER'S FIX REQUEST" if m.get("from") == "fable" else "GPT-6 ASTRA'S FIX REQUEST"
                   if m.get("from") == "astra" else "USER")
            out.append(f"{tag}: {_clip(m.get('content'), 4000)}")
        elif r == "assistant":
            if m.get("reasoning"):
                out.append(f"{who} (thinking): {_clip(m['reasoning'], 1200)}")
            if (m.get("content") or "").strip():
                out.append(f"{who}: {m['content']}")
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                out.append(f"{who} CALLED {fn.get('name')}({_clip(fn.get('arguments'), 1500)})")
        elif r == "tool":
            flag = " [ERROR]" if m.get("error") else ""
            out.append(f"TOOL RESULT {m.get('name')}{flag}: {_clip(m.get('content'), per_tool)}")
        elif r == "subagent":
            inner = _render_work((m.get("messages") or [])[1:], per_tool=min(per_tool, 600), skip_lanes=skip_lanes)
            out.append(f"SUBAGENT {m.get('name')} ({m.get('status')}) worked on: {_clip(m.get('task'), 800)}\n"
                       + _clip(inner, 6000).replace("LOCAL MODEL", "SUBAGENT"))
    return "\n\n".join(out)


def _images_of(msgs, limit=2):
    from . import attachments
    blocks = []
    for m in reversed(msgs):
        if m.get("role") == "tool" and m.get("image"):
            try:
                url = attachments.image_data_url(m["image"])
                head, data = url.split(",", 1)
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": head[5:].split(";")[0],
                                                           "data": data}})
            except Exception:
                continue
            if len(blocks) >= limit:
                break
    return list(reversed(blocks))


def _decision_text(turn):
    d = turn.decision or {}
    if not d:
        return ""
    s = f"Router's reading of the request: {d.get('intent', '')} (complexity: {d.get('complexity', '?')})"
    if d.get("plan"):
        s += "\nRouter's plan: " + " | ".join(d["plan"])
    return s


def review_packet(turn, round_no, previous=None):
    earlier = memory._render(agent.local_view(turn.history[:max(0, turn.start_index - 1)]))
    work = turn.history[turn.start_index:]
    parts = [f"# The user's request\n{_request_of(turn)}"]
    if earlier.strip():
        parts.append("# Earlier in this chat (for context)\n" + _clip(earlier[-9000:], 9000))
    dt = _decision_text(turn)
    if dt:
        parts.append("# " + dt)
    parts.append("# What the local model did (tool calls, results, final answer)\n" + _clip(_render_work(work), 60000))
    diffs = agent.change_diffs(turn)
    if diffs:
        parts.append("# Files changed this turn (before → after)\n" + diffs)
    others = getattr(turn, "prior_reviews", None) or []
    if others:
        parts.append("# A first review pass by ChatGPT already ran on this work (GPT-6 Astra reviewed; GPT-6.1 Sol "
                     "repaired when it found major problems). Its verdicts, oldest first:\n" +
                     "\n\n".join(f"Round {i} ({r.get('verdict')}): {r.get('summary', '')}\n{_issues_text(r)}"
                                  for i, r in enumerate(others, 1)) +
                     "\n\nJudge the work as it stands now, including anything GPT-6.1 Sol changed. Their verdict is a "
                     "second opinion, not proof: verify it like any other claim.")
    if previous:
        parts.append(f"# This is review round {round_no}. Your previous review asked for these fixes:\n"
                     + _issues_text(previous) + "\n\nCheck whether they were fixed and nothing else broke.")
    parts.append(f"Working directory on the PC: {turn.settings.get('work_dir')}\n\nReview the work now. Use read-only "
                 "tools where verification matters, then call submit_review.")
    content = [{"type": "text", "text": "\n\n".join(parts)}]
    content += _images_of(work)
    return content


def _issues_text(rv):
    lines = []
    for i, it in enumerate(rv.get("issues") or [], 1):
        lines.append(f"{i}. [{it.get('severity')}] {it.get('where')}: {it.get('problem')} → Fix: {it.get('fix')}")
    return "\n".join(lines) or "(none listed)"


def fix_request_text(rv, round_no):
    return (f"Review of your work (round {round_no}): {rv.get('summary', '')}\n\nProblems to fix:\n{_issues_text(rv)}"
            f"\n\nPlan:\n{rv.get('plan') or '(fix the problems above)'}\n\nFix every problem, verify each fix with your "
            "tools, then give your complete final answer again.")


def execute_packet(turn, rv):
    earlier = memory._render(agent.local_view(turn.history[:max(0, turn.start_index - 1)]))
    parts = [f"# The user's request\n{_request_of(turn)}"]
    if rv.get("improved_prompt"):
        parts.append(f"# The request, clarified by the reviewer\n{rv['improved_prompt']}")
    if earlier.strip():
        parts.append("# Earlier in this chat (for context)\n" + _clip(earlier[-9000:], 9000))
    parts.append("# The local model's attempt\n" + _clip(_render_work(turn.history[turn.start_index:]), 40000))
    diffs = agent.change_diffs(turn)
    if diffs:
        parts.append("# Files the local model changed\n" + diffs)
    parts.append(f"# Claude Fable 5.1's review: {rv.get('verdict')}\n{rv.get('summary', '')}\n\nProblems:\n"
                 f"{_issues_text(rv)}\n\n# The plan to execute\n{rv.get('plan') or ''}")
    parts.append(f"Working directory on the PC: {turn.settings.get('work_dir')}. Carry out the task now.")
    return [{"type": "text", "text": "\n\n".join(parts)}]


# ------------------------------------------------------------------------------------------------ streaming loop

async def _stream(turn, lane, model, system, messages, tool_defs, effort, max_tokens, on_tool, step_budget):
    """A manual agent loop with streaming. Yields UI events. on_tool(block) is an async generator that yields
    UI events and finally sets box['result'] (tool_result block) or box['stop'] = value to end the loop."""
    c = client()
    s = turn.settings
    total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "usd": 0.0}
    for step in range(step_budget):
        if turn.cancel.is_set():
            return
        if not budget_ok(s):
            yield {"t": "notice", "text": f"Daily Claude budget (${s.get('cloud_daily_budget_usd')}) reached; "
                                          f"{NAMES.get(model, model)} stopped. Raise it in Settings → Claude."}
            return
        msg = {"role": "assistant", "content": "", "reasoning": "", "id": uuid.uuid4().hex[:10], "lane": lane,
               "model": NAMES.get(model, model)}
        yield {"t": "assistant_start", "id": msg["id"], "lane": lane, "model": msg["model"]}
        kw = dict(model=model, max_tokens=max_tokens, system=system, messages=messages,
                  thinking={"type": "adaptive", "display": "summarized"}, output_config={"effort": effort},
                  cache_control={"type": "ephemeral"})
        if tool_defs:
            kw["tools"] = tool_defs
        if model not in _no_fallbacks:
            kw["betas"] = [FALLBACK_BETA]
            kw["fallbacks"] = "default"
        t0 = time.time()
        first = None
        final = None
        import anthropic
        try:
            async with c.beta.messages.stream(**kw) as stream:
                async for ev in stream:
                    if turn.cancel.is_set():
                        break
                    et = ev.type
                    if et == "content_block_start":
                        b = ev.content_block
                        if b.type == "tool_use":
                            yield {"t": "tool_pending", "name": b.name, "lane": lane}
                        elif b.type == "fallback":
                            fm = getattr(getattr(b, "to", None), "model", "") or "another model"
                            yield {"t": "notice", "lane": lane,
                                   "text": f"{NAMES.get(model, model)} declined part of this; {NAMES.get(fm, fm)} continued."}
                    elif et == "content_block_delta":
                        d = ev.delta
                        if d.type == "thinking_delta" and d.thinking:
                            first = first or time.time()
                            msg["reasoning"] += d.thinking
                            yield {"t": "reasoning", "d": d.thinking, "lane": lane}
                        elif d.type == "text_delta" and d.text:
                            first = first or time.time()
                            msg["content"] += d.text
                            yield {"t": "content", "d": d.text, "lane": lane}
                if not turn.cancel.is_set():
                    final = await stream.get_final_message()
        except anthropic.BadRequestError as e:
            if "fallback" in str(e).lower() and model not in _no_fallbacks:
                _no_fallbacks.add(model)          # this account/model does not take the parameter: retry plain
                yield {"t": "assistant_done", "message": {**msg, "content": "", "discard": True}}
                continue
            raise
        except ValueError as e:                   # tool input JSON the SDK could not parse at all: re-issue
            yield {"t": "notice", "lane": lane, "text": f"Retrying a garbled tool call ({e})."}
            yield {"t": "assistant_done", "message": {**msg, "discard": True}}
            continue
        if final is None:
            msg["stats"] = {"finish": "stopped"}
            turn.history.append(msg)
            yield {"t": "assistant_done", "message": msg}
            return

        served = final.model or model
        u = usage_cost(served, final.usage)
        note_usage(served, u)
        for k in total:
            total[k] += u[k]
        tool_uses = [b for b in final.content if b.type == "tool_use"]
        tool_calls = [{"id": b.id, "type": "function", "function": {"name": b.name, "arguments": json.dumps(b.input)}}
                      for b in tool_uses]
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if served != model:
            msg["model"] = NAMES.get(served, served) + " (fallback)"
        dt = time.time() - t0
        msg["stats"] = {"prompt_tokens": u["input"] + u["cache_read"] + u["cache_write"], "completion_tokens": u["output"],
                        "cached_tokens": u["cache_read"], "usd": round(u["usd"], 4), "time": round(dt, 1),
                        "ttft": round(first - t0, 2) if first else None, "finish": final.stop_reason,
                        "tg": round(u["output"] / max(0.1, dt - ((first or t0) - t0)), 1)}
        if not msg["reasoning"]:
            msg.pop("reasoning")
        turn.history.append(msg)
        yield {"t": "assistant_done", "message": msg}
        yield {"t": "cloud_usage", "model": served, "usage": u, "today_usd": spent_today()}

        if final.stop_reason == "refusal":
            yield {"t": "notice", "lane": lane, "text": f"{NAMES.get(served, served)} declined this request."}
            return
        if final.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": final.content})
            continue
        if not tool_uses:
            return
        if final.stop_reason == "max_tokens":
            yield {"t": "notice", "lane": lane, "text": "A tool call was cut off at the output limit; stopping."}
            return
        messages.append({"role": "assistant", "content": final.content})
        results = []
        stop = None
        for b in tool_uses:
            box = {}
            async for ev in on_tool(b, box):
                yield ev
            if "stop" in box:
                stop = box["stop"]
            results.append(box.get("result") or {"type": "tool_result", "tool_use_id": b.id, "content": "ok"})
        if stop is not None:
            turn.cloud_result = stop
            return
        messages.append({"role": "user", "content": results})
        if turn.cancel.is_set():
            return
    yield {"t": "notice", "lane": lane, "text": f"{NAMES.get(model, model)} stopped after {step_budget} steps."}


def _result_block(tool_use_id, res):
    content = [{"type": "text", "text": res.get("text") or "(no output)"}]
    if res.get("image"):
        from . import attachments
        try:
            url = attachments.image_data_url(res["image"])
            head, data = url.split(",", 1)
            content.append({"type": "image", "source": {"type": "base64", "media_type": head[5:].split(";")[0],
                                                        "data": data}})
        except Exception:
            pass
    blk = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if res.get("error"):
        blk["is_error"] = True
    return blk


def _local_tool_runner(turn, lane, defs, allowed_categories=None):
    schemas = {d["name"]: d["input_schema"] for d in defs}

    async def run(b, box):
        args = b.input
        why = validate(schemas.get(b.name), args)
        if why:
            res = {"text": json.dumps({"INVALID_JSON": why}), "error": True}
            tmsg = {"role": "tool", "tool_call_id": b.id, "name": b.name, "content": res["text"], "error": True,
                    "label": "", "lane": lane}
            yield {"t": "tool_start", "call_id": b.id, "name": b.name, "args": args, "label": "",
                   "category": None, "needs_approval": False, "lane": lane}
            turn.history.append(tmsg)
            yield {"t": "tool_result", "message": tmsg}
            box["result"] = _result_block(b.id, res)
            return
        out = {}
        async for ev in agent.exec_tool(turn, b.id, b.name, args, lane, allowed_categories=allowed_categories, out=out):
            yield ev
        turn.history.append(out["msg"])
        box["result"] = _result_block(b.id, out["res"])
    return run


# ------------------------------------------------------------------------------------------------ review / execute

async def review(turn, round_no=1, previous=None):
    """Fable reviews the turn's work. Yields UI events; the verdict lands in turn.cloud_result (or None)."""
    s = turn.settings
    if backend(s) == "plan":
        from . import claude_code
        async for ev in claude_code.review(turn, round_no, previous):
            yield ev
        return
    model = s.get("review_model") or "claude-fable-5-1"
    names = read_only_names(s) if s.get("cloud_fable_tools", True) else []
    defs = _tool_defs(s, set(names))
    allowed = READ_ONLY_CATEGORIES | {"memory"}
    local_run = _local_tool_runner(turn, "fable", defs, allowed)

    async def on_tool(b, box):
        if b.name == "submit_review":
            v = b.input if isinstance(b.input, dict) else {}
            if v.get("verdict") not in ("ok", "minor", "major"):
                box["result"] = {"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                 "content": "verdict must be ok, minor or major"}
                return
            box["stop"] = v
            return
        if b.name not in names:
            box["result"] = {"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                             "content": "Reviewers have read-only tools only."}
            return
        async for ev in local_run(b, box):
            yield ev

    turn.cloud_result = None
    messages = [{"role": "user", "content": review_packet(turn, round_no, previous)}]
    yield {"t": "lane", "lane": "fable", "phase": "review", "round": round_no, "model": NAMES.get(model, model)}
    system = with_profile(turn, REVIEW_SYSTEM, "review")
    async for ev in _stream(turn, "fable", model, system, messages, defs + [SUBMIT_REVIEW],
                            s.get("review_effort") or "high", 32000, on_tool, 16):
        yield ev
    if turn.cloud_result is None and not turn.cancel.is_set():
        # Fable answered in text without calling the tool (tool_choice can't be forced on this model): ask once.
        last = next((m for m in reversed(turn.history) if m.get("lane") == "fable" and m.get("role") == "assistant"), None)
        if last and (last.get("content") or "").strip():
            messages.append({"role": "assistant", "content": [{"type": "text", "text": last["content"]}]})
            messages.append({"role": "user", "content": "Now call submit_review with your verdict."})
            async for ev in _stream(turn, "fable", model, system, messages, [SUBMIT_REVIEW],
                                    "low", 8000, on_tool, 2):
                yield ev


async def execute(turn, rv):
    """Opus carries out Fable's plan with the local tools. Yields UI events."""
    s = turn.settings
    if backend(s) == "plan":
        from . import claude_code
        async for ev in claude_code.execute(turn, rv):
            yield ev
        return
    model = s.get("fix_model") or "claude-opus-5-5"
    names = all_names(s)
    defs = _tool_defs(s, set(names))
    run = _local_tool_runner(turn, "opus", defs)
    messages = [{"role": "user", "content": execute_packet(turn, rv)}]
    yield {"t": "lane", "lane": "opus", "phase": "execute", "model": NAMES.get(model, model)}
    async for ev in _stream(turn, "opus", model, with_profile(turn, EXECUTE_SYSTEM, "execute"), messages, defs,
                            s.get("fix_effort") or "high",
                            64000, run, int(s.get("agent_max_steps") or 40)):
        yield ev
