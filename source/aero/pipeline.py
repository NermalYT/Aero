"""One chat turn, end to end:

    router (CPU) → local model (GPU) → [ChatGPT pass: GPT-6 Astra review] → [Claude pass: Claude Fable 5.1 review]
                                         ok: done                             ok: done
                                         minor: local model fixes, re-check   minor: local model fixes, re-check
                                         major: GPT-6.1 Sol repairs           major: Claude Opus 5.5 takes over
    → lessons for the local model from every review and takeover

Each pass is switched on by its own composer button (ChatGPT, Claude). With both on, the ChatGPT pass runs first and
Claude reviews the result with Astra's verdicts and Sol's work in its packet, so Claude has the final word.
Every stage streams its events to the UI on its own lane (router, local, astra, sol, fable, opus).
"""
import asyncio
import json
import re
import time
import uuid

import httpx

from . import (agent, agents, app_catalog, app_registry, chatgpt, cloud, control, experience, looplog, memory, router,
               stats, tools)
from .config import DATA

CORE_TOOLS = ["list_dir", "read_file", "find_files", "search_files", "run_command", "web_search", "fetch_url",
              "recall", "remember"]
MAX_EXPOSED = 32


def _sse(obj):
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def extra(turn):
    """Text appended to the system prompt: only things that stay fixed for the whole chat."""
    parts = [agent.profile_for(turn.settings, turn.chat_id, turn.ctx_size)[0],
             memory.carry_block(turn.carry) if turn.carry else "", getattr(turn, "persona", "")]
    return "\n\n".join(p for p in parts if p)


# ------------------------------------------------------------------------------------------------ compaction

async def compact_midturn(turn):
    ctx_size = turn.ctx_size
    cut = agent._split_point(turn.history, int(ctx_size * 0.3 / turn.calib))
    if cut <= 0:
        return
    yield {"t": "compacting"}
    res = await memory.summarize(turn.engine["url"], ctx_size, turn.history[:cut], turn.title, turn.carry)
    new_id = uuid.uuid4().hex[:12]
    added = memory.store_result(turn.chat_id, turn.title, res, next_chat=new_id)
    turn.carry = {"from": turn.chat_id, "from_title": turn.title, "summary": res["summary"],
                  "open_tasks": res["open_tasks"], "at": time.time()}
    turn.history = turn.history[cut:]
    turn.start_index = max(0, turn.start_index - cut)
    yield {"t": "compacted", "new_chat_id": new_id, "carry": turn.carry, "keep": turn.history, "facts_added": added}
    turn.rebind(new_id)
    turn.memory_text = agent.memory_recall(turn.settings, turn.history, turn.carry, turn.chat_id, ctx_size)
    for extra in (getattr(turn, "journal_text", ""), getattr(turn, "experience_text", "")):
        if extra:
            turn.memory_text = (turn.memory_text + "\n\n" + extra).strip()


# ------------------------------------------------------------------------------------------------ routing

def tool_catalog(settings):
    """[(name, category, description)] of every enabled tool, in registry order (stable prompt prefix)."""
    from . import localonly
    pol = settings.get("tool_policy", {})
    return [(t.name, t.category, t.description) for t in tools.REGISTRY.values()
            if pol.get(t.category, "ask") != "off" and localonly.tool_allowed(t.category, settings)]


def _schema_tokens(settings, names=None):
    n = 0
    for s in tools.schemas(settings):
        if names is None or s["function"]["name"] in names:
            n += len(json.dumps(s)) / 3.6
    return int(n)


def _prev_exposed(history):
    for m in reversed(history):
        if m.get("role") == "router" and isinstance(m.get("exposed"), list):
            return m["exposed"]
    return None


def choose_exposure(decision, settings, prev, available, text=""):
    from . import capabilities
    picked = [n for n in decision.get("tools") or [] if n in available]
    picked += [n for n in capabilities.hint_tools(text, available) if n not in picked]   # see capabilities.HINTS
    chosen = router.expand(picked, available)
    floor = int(settings.get("router_tools_min") or 0)
    if decision.get("complexity") != "trivial" and len(chosen) < floor:
        for n in CORE_TOOLS:
            if len(chosen) >= floor:
                break
            if n in available and n not in chosen:
                chosen.append(n)
    exposed = chosen
    if prev is not None:
        prev = [n for n in prev if n in available]
        if set(chosen) <= set(prev):
            exposed = prev                       # nothing new: same tools, the prompt cache stays valid
        elif len(set(prev) | set(chosen)) <= MAX_EXPOSED:
            exposed = list(set(prev) | set(chosen))
    order = {n: i for i, n in enumerate(available)}
    return sorted(set(exposed), key=lambda n: order.get(n, 1e9))


async def route(turn, opts):
    s = turn.settings
    think_pref = opts.get("think") or s.get("thinking", True)
    think_pref = {"on": True, "off": False}.get(think_pref, think_pref)
    review_pref = opts.get("review") or s.get("review_mode") or "off"
    gpt_pref = opts.get("chatgpt_review") or s.get("chatgpt_review_mode") or "off"
    loop = opts.get("loop") or {}
    turn.think = True if think_pref == "auto" else bool(think_pref)
    turn.review = review_pref == "on"
    turn.gpt_review = gpt_pref == "on"
    if loop.get("iteration", 0) > 1 and not s.get("review_in_loop"):
        turn.review = turn.gpt_review = False
    if s.get("local_only"):
        turn.review = turn.gpt_review = False
        review_pref = gpt_pref = "off"

    use_router = s.get("router_enabled", True)
    catalog = tool_catalog(s) if s.get("tools_enabled", True) else []
    available = [c[0] for c in catalog]
    prev = _prev_exposed(turn.history)
    rec = None

    # a forever-loop repeats the same prompt: reuse the last decision instead of asking again
    if use_router and loop.get("iteration", 0) > 1:
        last = next((m for m in reversed(turn.history) if m.get("role") == "router" and m.get("decision")), None)
        if last:
            rec = {**last, "id": uuid.uuid4().hex[:10], "ts": time.time(), "reused": True}

    if use_router and rec is None:
        if not router.ready() and router.status().get("loading"):
            for _ in range(40):                 # the router is still starting: wait a little
                await asyncio.sleep(0.25)
                if router.ready():
                    break
        if router.ready():
            yield {"t": "router_start"}
            from . import skills
            sk = skills.catalog()
            try:
                d, info = await asyncio.to_thread(router.decide, turn.history, catalog, sk)
                rec = {"role": "router", "id": uuid.uuid4().hex[:10], "ts": time.time(), "decision": d, "info": info}
                stats.SESSION["router_calls"] += 1
                stats.SESSION["router_ms_total"] += info.get("ms") or 0
            except Exception as e:  # noqa: BLE001
                yield {"t": "router_error", "error": f"{type(e).__name__}: {e}"[:300]}
        elif router.model_path():
            yield {"t": "router_error", "error": router.status().get("error") or "The router is not running."}

    if rec:
        d = rec["decision"]
        turn.decision = d
        exposed = choose_exposure(d, s, prev, available, agent._last_user_text(turn.history)) if catalog else []
        if think_pref == "auto":
            turn.think = bool(d.get("think"))
        in_loop = loop.get("iteration", 0) > 1 and not s.get("review_in_loop")
        if review_pref == "auto":
            turn.review = bool(d.get("review")) and not in_loop
        if gpt_pref == "auto":
            turn.gpt_review = bool(d.get("review")) and not in_loop
        full = _schema_tokens(s)
        saved = max(0, full - _schema_tokens(s, set(exposed)))
        rec.update(exposed=exposed, think=turn.think, review=turn.review, gpt_review=turn.gpt_review, saved_tokens=saved,
                   all_tools=len(available))
        if not rec.get("reused"):
            learned = learn_preference(turn, d)
            if learned:
                rec["learned"] = learned
        turn.exposed = list(exposed)
        if not rec.get("reused"):
            stats.SESSION["router_tokens_saved"] += saved
        turn.history.append(rec)
        turn.start_index = len(turn.history)
        yield {"t": "router", "message": rec}
    else:
        turn.exposed = None if prev is None else list(prev)      # no router: everything (or what this chat had)


# ------------------------------------------------------------------------------------------------ apps

BACKEND_TOOLS = {"accessibility": ["app_view", "app_click", "app_type", "app_read", "app_keys"],
                 "browser": ["browser_open", "browser_read_sections", "browser_snapshot", "browser_click",
                             "browser_type"],
                 "file": ["read_file", "edit_file", "write_file", "find_files"], "terminal": ["run_command"],
                 "protocol": ["app_launch"], "media_session": ["app_launch"]}


def app_context(turn, task):
    """Apps and services the request names (app_registry.mentions: deterministic, no model call). Adds a line to the
    turn context and, when the router narrowed the tools, the tools that fit those apps."""
    try:
        ms = app_registry.mentions(task)
    except Exception:  # noqa: BLE001
        return ""
    if not ms:
        return ""
    line = app_registry.context_line(task, ms)
    if turn.exposed is not None:
        available = [c[0] for c in tool_catalog(turn.settings)]
        want = ["app_find"]
        for m in ms:
            e = app_catalog.entry(m.get("catalog") or m.get("id")) or {}
            if m.get("installed"):
                want.append("app_launch")
            for b in e.get("backends", []):
                want += BACKEND_TOOLS.get(b, [])
                if b == "mcp":
                    key = (m.get("catalog") or m.get("id") or "").replace("_", "")
                    want += [n for n in available if n.startswith("mcp_") and key and key in n.lower().replace("_", "")]
        add = [n for n in dict.fromkeys(want) if n in available and n not in turn.exposed]
        if add:
            order = {n: i for i, n in enumerate(available)}
            turn.exposed = sorted(set(turn.exposed) | set(add), key=lambda n: order.get(n, 1e9))
    return line


# The router fills in "preference" when it reads one, but a 2B model also finds preferences in plain requests
# ("make it blue"), so one is only kept when the message itself sounds like a standing preference.
PREF_CUE = re.compile(
    r"\b(i (really |much |would )?(prefer|like|love|hate|dislike|can'?t stand|want you|need you|don'?t want|do not want)"
    r"|(don'?t|do not|never|always|stop|quit) (use|using|add|adding|write|writing|give|giving|say|saying|call|ask|asking|be)"
    r"|from now on|going forward|in (the )?future|every time|next time|too (long|short|wordy|verbose|formal|casual|basic|technical)"
    r"|call me|my name is|keep (it|them|answers|responses|replies) (short|brief|simple|detailed)"
    r"|(no|less|fewer|more) (fluff|filler|emojis?|detail|bullets?|headers?|jargon|explanation|comments)"
    r"|my (writing )?style|the way i like)", re.I)


def learn_preference(turn, d):
    """Save a preference the user just stated (router field "preference") as memory. Returns its text or ""."""
    s = turn.settings
    pref = re.sub(r"\s+", " ", (d.get("preference") or "")).strip().strip('"')
    if not pref or len(pref) < 8 or not s.get("learn_preferences", True) or not s.get("memory_enabled", True):
        return ""
    if not PREF_CUE.search(agent._last_user_text(turn.history)) or memory.SECRET_RE.search(pref):
        return ""
    try:
        memory.add_fact(pref, "preference", source=turn.chat_id)
    except Exception:  # noqa: BLE001
        return ""
    return pref


# ------------------------------------------------------------------------------------------------ lessons

LESSON_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["lessons"],
                 "properties": {"lessons": {"type": "array", "maxItems": 3,
                                            "items": {"type": "string", "maxLength": 240}}}}


async def make_lessons(turn, reviews, takeovers=()):
    """Ask the local model what it should do differently next time, store it as memory (kind "lesson", which every
    later chat and model sees) and as a training example (data/training/corrections.jsonl). reviews: every review
    that found problems, from either pass; takeovers: [(model name, final answer)] from Sol and/or Opus."""
    s = turn.settings
    if not s.get("lessons_enabled", True) or not reviews:
        return []
    req = cloud._request_of(turn)
    local_final = next((m.get("content") for m in reversed(turn.history)
                        if m.get("role") == "assistant" and (m.get("lane") or "local") == "local" and m.get("content")), "")
    rv_text = "\n\n".join(f"Review {i} by {r.get('reviewer') or 'the reviewer'}, round {r.get('round', 1)} "
                           f"({r.get('verdict')}): {r.get('summary')}\n{cloud._issues_text(r)}"
                           for i, r in enumerate(reviews, 1))
    who = " and ".join(n for n, _ in takeovers)
    prompt = ("You are the local AI model inside Aero. Stronger cloud models reviewed your work on a task and found "
              "problems" + (f", then {who} redid the task." if takeovers else ".") +
              f"\n\nTASK:\n{cloud._clip(req, 3000)}\n\nYOUR FINAL ANSWER:\n{cloud._clip(local_final, 3000)}\n\n"
              f"REVIEWS:\n{cloud._clip(rv_text, 7000)}\n\n" +
              "".join(f"{n.upper()}'S RESULT:\n{cloud._clip(a, 4000)}\n\n" for n, a in takeovers) +
              "Write 1 to 3 lessons for yourself that would have prevented these mistakes on future, different tasks. "
              "Each is one imperative sentence, general rather than about this one task (good: \"Re-read a file after "
              "editing it before saying the edit worked.\"; bad: \"Fix line 12 of app.py\"). Reply as JSON.")
    body = {"messages": [{"role": "user", "content": prompt}], "temperature": 0.2, "max_tokens": 400,
            "chat_template_kwargs": {"enable_thinking": False}, "stream": False,
            "response_format": {"type": "json_schema", "json_schema": {"name": "lessons", "schema": LESSON_SCHEMA}}}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(240, connect=10)) as c:
            r = await c.post(turn.engine["url"] + "/v1/chat/completions", json=body)
            txt = r.json()["choices"][0]["message"].get("content") or "{}"
        lessons = [x.strip() for x in json.loads(txt[txt.index("{"):]).get("lessons", []) if x.strip()][:3]
    except Exception:
        lessons = []
    saved = []
    for text in lessons:
        try:
            f, _how = memory.add_fact(text, kind="lesson", source=turn.chat_id)
            saved.append({"id": (f or {}).get("id"), "text": text})
        except Exception:
            saved.append({"id": None, "text": text})
    if s.get("share_lessons_as_training", True):
        try:
            d = DATA / "training"
            d.mkdir(exist_ok=True)
            with open(d / "corrections.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "chat_id": turn.chat_id, "request": req,
                                    "local_answer": local_final, "reviews": reviews,
                                    "takeovers": [{"model": n, "answer": a} for n, a in takeovers],
                                    "opus_answer": next((a for n, a in takeovers if "Opus" in n), ""),
                                    "lessons": lessons}, ensure_ascii=False) + "\n")
        except Exception:
            pass
    return saved


# ------------------------------------------------------------------------------------------------ review passes

class Pass:
    """One review pass: who reviews, who takes over, and where its settings live."""

    def __init__(self, key):
        self.key = key
        if key == "chatgpt":
            self.mod, self.rev_lane, self.fix_lane, self.label = chatgpt, "astra", "sol", "ChatGPT"
            self.where, self.budget_key, self.model_key = "Settings → ChatGPT", "openai_daily_budget_usd", "gpt_review_model"
            self.fix_key, self.names = "gpt_fix_model", chatgpt.NAMES
            self.budget_text = "OpenAI"
        else:
            self.mod, self.rev_lane, self.fix_lane, self.label = cloud, "fable", "opus", "Claude"
            self.where, self.budget_key, self.model_key = "Settings → Claude", "cloud_daily_budget_usd", "review_model"
            self.fix_key, self.names = "fix_model", cloud.NAMES
            self.budget_text = "Claude"

    def reviewer(self, s):
        m = s.get(self.model_key)
        return self.names.get(m, m)

    def fixer(self, s):
        m = s.get(self.fix_key)
        return self.names.get(m, m)


async def _review_flow(turn, p):
    """One pass. Leaves turn.flow = {"problems": [reviews that found something], "takeover": (name, answer) or None}."""
    s = turn.settings
    turn.flow = {"problems": [], "takeover": None, "reviews": []}
    if s.get("strict_offline"):
        yield {"t": "notice", "text": f"Strict offline mode is on, so the {p.label} review was skipped. "
                                      "Everything in this answer came from your local model."}
        return
    if not p.mod.backend(s):
        yield {"t": "notice", "text": p.mod.not_ready_text(s)}
        return
    if not p.mod.budget_ok(s):
        yield {"t": "notice", "text": f"Today's {p.budget_text} budget (${s.get(p.budget_key)}) is used up; "
                                      f"the {p.label} review was skipped. Raise it in {p.where}."}
        return
    reviews = turn.flow["reviews"]
    max_rounds = max(1, int(s.get("review_max_rounds") or 2))
    rnd = 1
    while True:
        prev = reviews[-1] if reviews else None
        async for ev in p.mod.review(turn, rnd, prev):
            yield ev
        rv = turn.cloud_result
        if turn.cancel.is_set():
            return
        if not rv:
            yield {"t": "notice", "lane": p.rev_lane, "text": "The reviewer did not return a verdict; keeping the result."}
            return
        rv = {**rv, "reviewer": p.reviewer(s), "round": rnd, "pass": p.key}
        reviews.append(rv)
        stats.SESSION["reviews"][rv["verdict"]] = stats.SESSION["reviews"].get(rv["verdict"], 0) + 1
        rec = {"role": "review", "id": uuid.uuid4().hex[:10], "ts": time.time(), "round": rnd, "lane": p.rev_lane,
               "model": p.reviewer(s), "pass": p.key, **{k: v for k, v in rv.items() if k not in ("reviewer", "round", "pass")},
               "files": [f for f, c in turn.changes.items() if c.get("before") != c.get("after")]}
        if rv["verdict"] == "minor" and rnd >= max_rounds:
            rec["escalated"] = True
            rec["fixer"] = p.fixer(s)
        elif rv["verdict"] == "major":
            rec["fixer"] = p.fixer(s)
        turn.history.append(rec)
        yield {"t": "review", "message": rec}
        if rv["verdict"] == "ok":
            turn.flow["problems"] = reviews[:-1]
            return
        if rv["verdict"] == "minor" and rnd < max_rounds:
            fix = {"role": "user", "from": p.rev_lane, "id": uuid.uuid4().hex[:10], "ts": time.time(),
                   "content": cloud.fix_request_text(rv, rnd).replace("Review of your work", f"{p.reviewer(s)}'s review of your work")}
            turn.history.append(fix)
            yield {"t": "fix_request", "message": fix}
            yield {"t": "lane", "lane": "local", "phase": "fix", "round": rnd}
            async for ev in agent.run_local(turn):
                yield ev
            if turn.cancel.is_set():
                return
            rnd += 1
            continue
        # major, or minor problems the local model could not fix: the pass's stronger model takes over with the plan
        if rv["verdict"] == "minor":
            rv = {**rv, "plan": rv.get("plan") or "", "verdict": "minor (unresolved)"}
        async for ev in p.mod.execute(turn, rv):
            yield ev
        turn.flow["problems"] = list(reviews)
        if turn.cancel.is_set():
            return
        final = next((m.get("content") for m in reversed(turn.history)
                      if m.get("lane") == p.fix_lane and m.get("role") == "assistant" and m.get("content")), "")
        if final:
            turn.flow["takeover"] = (p.fixer(s), final)
        return


async def _lessons(turn, reviews, takeovers=()):
    if not turn.settings.get("lessons_enabled", True) or not reviews:
        return
    yield {"t": "lane", "lane": "local", "phase": "lesson"}
    saved = await make_lessons(turn, reviews, list(takeovers))
    if saved:
        rec = {"role": "lesson", "id": uuid.uuid4().hex[:10], "ts": time.time(), "lessons": saved,
               "from": sorted({r.get("reviewer") for r in reviews if r.get("reviewer")})}
        turn.history.append(rec)
        yield {"t": "lesson", "message": rec}


async def review_passes(turn):
    """ChatGPT first, then Claude (who sees ChatGPT's verdicts and Sol's work), then one lessons step for both."""
    problems, takeovers = [], []
    turn.prior_reviews = []
    for key, on in (("chatgpt", turn.gpt_review), ("claude", turn.review)):
        if not on or turn.cancel.is_set():
            continue
        p = Pass(key)
        try:
            async for ev in _review_flow(turn, p):
                yield ev
        except Exception as e:  # noqa: BLE001  (a cloud failure must not lose the local result or the other pass)
            yield {"t": "notice", "level": "error", "text": f"{p.label} review failed: {type(e).__name__}: {e}"[:600]}
            turn.flow = getattr(turn, "flow", None) or {"problems": [], "takeover": None, "reviews": []}
        flow = getattr(turn, "flow", None) or {}
        problems += flow.get("problems") or []
        if flow.get("takeover"):
            takeovers.append(flow["takeover"])
        if key == "chatgpt":
            turn.prior_reviews = list(flow.get("reviews") or [])
    if problems and not turn.cancel.is_set():
        async for ev in _lessons(turn, problems, takeovers):
            yield ev


async def loop_journal(turn, task, iteration):
    """Forever-loop: the local model writes down what this round taught it (looplog.py)."""
    yield {"t": "lane", "lane": "local", "phase": "journal"}
    entry = await looplog.reflect(turn, task, iteration)
    turn.journal_entry = entry
    if entry:
        rec = {"role": "loopnote", "id": uuid.uuid4().hex[:10], "ts": time.time(), "iteration": iteration,
               "worked": entry["worked"], "failed": entry["failed"], "next": entry["next"], "status": entry["status"],
               "key": looplog.key(task)}
        turn.history.append(rec)
        yield {"t": "loopnote", "message": rec}


async def learn_from_run(turn, task, agent_name, outcome):
    """Shared learning (experience.py): record what this run's tools did, and when something went wrong or the user
    gave feedback, let the model write notes every model gets before similar tasks."""
    try:
        run = experience.record_run(turn, agent_name, outcome, task)
    except Exception:  # noqa: BLE001
        return
    if outcome != "done":
        return
    entry = getattr(turn, "journal_entry", None)
    if entry:                                      # a forever-loop round: share its journal entry, no second call
        experience.add_notes(entry.get("worked"), entry.get("failed"), turn.model_name, agent_name, task, source="loop")
        return
    if not experience.wants_reflection(run, task):
        return
    yield {"t": "lane", "lane": "local", "phase": "learn"}
    got = await experience.reflect(turn, task, agent_name)
    if got:
        rec = {"role": "learned", "id": uuid.uuid4().hex[:10], "ts": time.time(), "model": turn.model_name,
               "agent": agent_name, **got}
        turn.history.append(rec)
        yield {"t": "learned", "message": rec}


def mod_prefs(turn, opts):
    """Thinking and review choices for a mod chat, where no router runs: the composer buttons decide ("auto" thinks,
    and leaves the cloud reviews off unless their button is on)."""
    s = turn.settings
    think = opts.get("think") or s.get("thinking", True)
    think = {"on": True, "off": False, "auto": True}.get(think, think)
    turn.think = bool(think)
    turn.review = (opts.get("review") or s.get("review_mode") or "off") == "on" and not s.get("local_only")
    turn.gpt_review = (opts.get("chatgpt_review") or s.get("chatgpt_review_mode") or "off") == "on" and not s.get("local_only")


async def mod_finish(mod_id):
    """After a mod chat's turn: check the copy (unless the model's own mod_check already covered this exact state)
    and hand the UI the mod card."""
    from . import mods
    rec = mods.get(mod_id)
    if mods.changes(mod_id) and (rec.get("checks") or {}).get("fp") != mods.fingerprint(mod_id):
        yield {"t": "mod_checking", "mod": mod_id}
        await asyncio.to_thread(mods.check, mod_id)
    else:
        rec["files"] = mods.changes(mod_id)
        mods.save(rec)
    yield {"t": "mod_ready", "mod": mods.public(mods.get(mod_id))}


async def run_turn(chat_id, history, settings, engine_state, carry=None, title="", opts=None, agent_meta=None):
    """Async generator of SSE strings for one user message. agent_meta: the chat's "agent" field (its name, and for
    a chat with a subagent which one)."""
    opts = opts or {}
    meta = agent_meta if isinstance(agent_meta, dict) else {}
    mod, mod_events = None, []
    if opts.get("mod"):
        from . import mods
        mod, created = mods.for_turn(opts["mod"], chat_id, agent._last_user_text(history))
        if created:
            mod_events.append({"t": "mod_new", "mod": mods.public(mod)})
        settings = mods.chat_settings(settings, mod["id"])
    turn = agent.Turn(chat_id, history, settings, engine_state, carry, title)
    if mod:
        mods.prepare_turn(turn, mod["id"])
    elif meta.get("kind") == "subagent":
        turn.persona = agents.persona(meta)
    task = agent._last_user_text(history)
    first = next((m.get("content") for m in history if m.get("role") == "user" and m.get("from") not in
                  agent.REVIEWER_NAMES and m.get("content")), "") or task
    name = agents.clean_name(meta.get("name")) or agents.name_from_text(first or title)
    agents.start(chat_id, name, title, turn.model_name, task, {**meta, "named": bool(meta.get("name"))})
    stats.SESSION["turns"] += 1
    loop = opts.get("loop") or {}
    if loop:
        stats.SESSION["loop"] = {"active": True, "iteration": int(loop.get("iteration") or 1),
                                 "started": loop.get("started") or time.time(), "chat_id": chat_id}
    ids = {"chat": chat_id}
    failed = False

    def out(ev):
        agents.note(ids["chat"], ev)
        if ev.get("t") == "compacted" and ev.get("new_chat_id"):
            ids["chat"] = ev["new_chat_id"]
        return _sse(ev)

    try:
        from . import vram_policy
        async for ev in vram_policy.wait_for_model():
            yield out(ev)
        for ev in mod_events:
            yield out(ev)
        if mod:
            mod_prefs(turn, opts)                  # a mod chat has a fixed tool set: no router
        else:
            async for ev in route(turn, opts):
                yield out(ev)
        turn.memory_text = agent.memory_recall(settings, turn.history, carry, chat_id, turn.ctx_size)
        if not mod and settings.get("tools_enabled", True):
            apps_line = await asyncio.to_thread(app_context, turn, task)
            if apps_line:
                turn.memory_text = (apps_line + "\n\n" + turn.memory_text).strip()
                if turn.exposed is not None:
                    yield out({"t": "exposure", "exposed": list(turn.exposed)})
        journal = bool(loop) and settings.get("loop_journal", True)
        if journal:
            turn.journal_text = looplog.block(task)
            if turn.journal_text:
                turn.memory_text = (turn.memory_text + "\n\n" + turn.journal_text).strip()
        learn = not mod and settings.get("shared_learning", True)
        if learn:
            turn.experience_text = experience.block(task, name)
            if turn.experience_text:
                turn.memory_text = (turn.memory_text + "\n\n" + turn.experience_text).strip()
        yield out({"t": "lane", "lane": "local", "phase": "work", "model": turn.model_name,
                   "think": turn.think, "tools": len(turn.exposed) if turn.exposed is not None else None})
        async for ev in agent.run_local(turn):
            yield out(ev)
        if (turn.review or turn.gpt_review) and not turn.cancel.is_set():
            async for ev in review_passes(turn):
                yield out(ev)
        if mod and not turn.cancel.is_set():
            async for ev in mod_finish(mod["id"]):
                yield out(ev)
        if journal and not turn.cancel.is_set():
            async for ev in loop_journal(turn, task, int(loop.get("iteration") or 1)):
                yield out(ev)
        if learn:
            async for ev in learn_from_run(turn, task, name, "stopped" if turn.cancel.is_set() else "done"):
                yield out(ev)
    except httpx.ConnectError:
        failed = True
        yield out({"t": "error", "error": "The model server is not running. Load a model first."})
    except Exception as e:  # noqa: BLE001
        failed = True
        yield out({"t": "error", "error": f"{type(e).__name__}: {e}"})
    finally:
        stopped = turn.cancel.is_set()
        summary = turn.graph.summary() if stopped else None
        if stopped:
            turn.graph.cancel_open("stopped")
        turn.graph.status = "stopped" if stopped else "error" if failed else "done"
        try:
            turn.graph.save(force=True)
        except OSError:
            pass
        turn.close()
        agent.release(ids["chat"])
        if ids["chat"] != chat_id:
            agent.release(chat_id)
        if turn.controlling:
            control.banner_off()
        final = next((m.get("content") for m in reversed(turn.history) if m.get("role") == "assistant"
                      and not m.get("tool_calls") and (m.get("content") or "").strip()), "")
        agents.finish(ids["chat"], "stopped" if stopped else "error" if failed else "done", final)
    if summary and (summary["completed"] or summary["not_completed"]):
        done = "; ".join(summary["completed"][-8:]) or "nothing yet"
        rest = "; ".join(summary["not_completed"][-8:]) or "nothing"
        yield _sse({"t": "notice", "text": f"Stopped. Finished: {done}. Not finished: {rest}. Nothing else will run "
                                           "for this task, and anything Aero was holding (app windows, your mouse and "
                                           "keyboard, its browser tab) was released."})
    if turn.controlling:
        yield _sse({"t": "control_end"})
    yield _sse({"t": "done", "today_usd": cloud.spent_today()})
