"""Shared learning: what every model has learned about doing tasks on this computer, for every model.

Each finished task leaves a run record in data/experience.json, whichever model did it: the model, the agent's
name (its kind of work: "Photo Renamer", "Bug Fixer"), the request, every tool call with whether it worked, was
refused by the user or failed (and the error), and the model's speed. No model call is needed for that.

When a run had a failed or refused tool, or the user's message reads like feedback ("don't", "always", "I prefer"),
the loaded model also looks back at it and writes short notes: what worked, what failed and why, and any lasting
preference the user stated. Preferences go into long-term memory (kind "preference"), which every model's system
prompt already carries. Forever-loop rounds share their journal entries here instead of making a second call.

Before each task, block() picks the notes and tool trouble from earlier runs that look like this task (BM25 over
request + agent name) and puts them in the model's context, labelled with the model that learned each one. So a
model loaded tomorrow starts from what today's model found out, and nobody repeats a known failure.
"""
import json
import re
import threading
import time
import uuid

import httpx

from . import memory
from .config import DATA

PATH = DATA / "experience.json"
MAX_RUNS = 2000
MAX_NOTES = 1500
_lock = threading.RLock()

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["worked", "failed", "preferences"],
          "properties": {"worked": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 220}},
                         "failed": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 220}},
                         "preferences": {"type": "array", "maxItems": 2,
                                         "items": {"type": "string", "maxLength": 200}}}}

# the user's message reads like feedback on how they want things done
FEEDBACK = re.compile(r"\b(don'?t|do not|never|always|stop|instead|prefer|i (?:like|want|hate|need)|please (?:use|don'?t)|"
                      r"wrong|not what i|too (?:long|short|slow|verbose)|from now on|next time)\b", re.I)


# ------------------------------------------------------------------------------------------------ store

def _load():
    try:
        d = json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d.setdefault("runs", [])
    d.setdefault("notes", [])
    return d


def _save(d):
    d["runs"] = d["runs"][-MAX_RUNS:]
    d["notes"] = d["notes"][-MAX_NOTES:]
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(PATH)


def all_items():
    with _lock:
        return _load()


def _text(content):
    if isinstance(content, list):                    # a message with images: keep its text parts
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


# ------------------------------------------------------------------------------------------------ recording

def tool_calls(history, start=0):
    """[(name, args text, result message)] for this run's tool calls, in order."""
    results = {m.get("tool_call_id"): m for m in history[start:] if m.get("role") == "tool"}
    out = []
    for m in history[start:]:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for c in m["tool_calls"]:
                f = c.get("function") or {}
                out.append((f.get("name") or "?", f.get("arguments") or "", results.get(c.get("id")) or {}))
    return out


def record_run(turn, agent_name, outcome, task):
    """Store what this run did. Returns the run record, or None for a run without tool calls."""
    calls = tool_calls(turn.history, turn.start_index)
    if not calls:
        return None
    tools = []
    for name, args, res in calls:
        t = {"name": name, "ok": bool(res) and not res.get("error")}
        if res.get("denied"):
            t["denied"] = True
        if res.get("error"):
            t["err"] = re.sub(r"\s+", " ", _text(res.get("content")))[:160]
            t["args"] = args[:120]
        tools.append(t)
    speeds = [m["stats"]["tg"] for m in turn.history[turn.start_index:]
              if m.get("role") == "assistant" and (m.get("stats") or {}).get("tg")]
    run = {"id": uuid.uuid4().hex[:10], "ts": time.time(), "chat_id": turn.chat_id, "model": turn.model_name or "",
           "agent": agent_name or "", "task": re.sub(r"\s+", " ", _text(task)).strip()[:300], "tools": tools,
           "outcome": outcome, "tok_s": round(sum(speeds) / len(speeds), 1) if speeds else None}
    with _lock:
        d = _load()
        d["runs"].append(run)
        _save(d)
    return run


def wants_reflection(run, task):
    """A model call is spent only where there is something to learn: a tool failed or was refused, or the user
    gave feedback."""
    if run and any(not t["ok"] for t in run["tools"]):
        return True
    return bool(FEEDBACK.search(_text(task)))


def add_notes(worked, failed, model, agent_name, task, source="reflect"):
    """Save notes; a note already known (same words) is refreshed and counted instead of repeated. Returns the new
    or refreshed notes."""
    saved = []
    with _lock:
        d = _load()
        index = {(n["side"], _norm(n["text"])): n for n in d["notes"]}
        for side, items in (("worked", worked), ("failed", failed)):
            for text in items or []:
                text = re.sub(r"\s+", " ", str(text)).strip()[:220]
                k = (side, _norm(text))
                if not k[1]:
                    continue
                other = index.get(("failed" if side == "worked" else "worked", k[1]))
                if other:                              # it failed before and works now (or the other way round)
                    d["notes"].remove(other)
                    index.pop(("failed" if side == "worked" else "worked", k[1]), None)
                n = index.get(k)
                if n:
                    n.update(ts=time.time(), hits=n.get("hits", 1) + 1, model=model or n.get("model"))
                    d["notes"].remove(n)
                    d["notes"].append(n)
                else:
                    n = {"id": uuid.uuid4().hex[:10], "ts": time.time(), "side": side, "text": text,
                         "model": model or "", "agent": agent_name or "", "task": _text(task)[:300].strip(),
                         "hits": 1, "source": source}
                    d["notes"].append(n)
                    index[k] = n
                saved.append(n)
        _save(d)
    return saved


def _round_text(history, start):
    lines = []
    for name, args, res in tool_calls(history, start):
        state = "REFUSED BY THE USER" if res.get("denied") else "ERROR" if res.get("error") else "ok" if res else "no result"
        body = re.sub(r"\s+", " ", _text(res.get("content")))[:240]
        lines.append(f"CALL {name}({args[:200]}) -> {state}: {body}")
    final = next((m.get("content") for m in reversed(history[start:]) if m.get("role") == "assistant"
                  and not m.get("tool_calls") and (m.get("content") or "").strip()), "")
    if final:
        lines.append(f"FINAL ANSWER: {_text(final)[:1200]}")
    return "\n".join(lines)[-8000:]


async def reflect(turn, task, agent_name):
    """Ask the loaded model what this run taught it. Saves notes and preferences; returns
    {"worked", "failed", "preferences"} with what was saved, or None."""
    work = _round_text(turn.history, turn.start_index)
    if not work.strip():
        return None
    prompt = ("You are a local AI model inside Aero. Other models (and you, later) will read your notes before "
              "similar tasks, so they reuse what worked and avoid what failed.\n\n"
              f"THE USER'S REQUEST:\n{_text(task)[:3000]}\n\nWHAT YOU DID:\n{work}\n\n"
              "Reply as JSON:\n"
              "- worked: up to 3 approaches, tools, commands, paths or settings that worked and are worth reusing on "
              "similar tasks. Be specific (name the tool and what to pass it).\n"
              "- failed: up to 3 things that failed or that the user refused, with the reason and what to do "
              "instead when you know it.\n"
              "- preferences: up to 2 lasting preferences the user stated about how they want things done (\"Prefers "
              "PowerShell over cmd\", \"Wants short answers\"). Only what the user actually said; not the task itself.\n"
              "Leave a list empty when there is nothing real to put in it.")
    body = {"messages": [{"role": "user", "content": prompt}], "temperature": 0.2, "max_tokens": 600,
            "chat_template_kwargs": {"enable_thinking": False}, "stream": False,
            "response_format": {"type": "json_schema", "json_schema": {"name": "experience", "schema": SCHEMA}}}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(240, connect=10)) as c:
            r = await c.post(turn.engine["url"] + "/v1/chat/completions", json=body)
            txt = r.json()["choices"][0]["message"].get("content") or "{}"
        data = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
    except Exception:
        return None
    clean = lambda xs, n: [re.sub(r"\s+", " ", str(x)).strip() for x in (xs or []) if str(x).strip()][:n]  # noqa: E731
    notes = add_notes(clean(data.get("worked"), 3), clean(data.get("failed"), 3), turn.model_name, agent_name, task)
    prefs = []
    for p in clean(data.get("preferences"), 2):
        try:
            f, how = memory.add_fact(p[:200], kind="preference", source=turn.chat_id)
            if f and how == "added":
                prefs.append(p[:200])
        except Exception:
            pass
    if not notes and not prefs:
        return None
    return {"worked": [n["text"] for n in notes if n["side"] == "worked"],
            "failed": [n["text"] for n in notes if n["side"] == "failed"], "preferences": prefs}


# ------------------------------------------------------------------------------------------------ recall

def _doc(x):
    return f"{x.get('agent', '')} {x.get('task', '')} {x.get('text', '')}"


def _related(query, docs):
    """[(score, index)] of docs about the same kind of task, best first. A doc counts when it shares at least two of
    the query's words, or most of them for a short query; BM25 orders them (its scores alone mean little when there
    are only a few notes)."""
    q = set(memory._terms(query))
    if not q:
        return []
    need = min(2, len(q))
    out = []
    for i, (doc, score) in enumerate(zip(docs, memory._rank(query, docs))):
        if len(q & set(memory._terms(doc))) >= need:
            out.append((score, i))
    return sorted(out, key=lambda x: -x[0])


def block(task, agent_name="", budget_tokens=700, now=None):
    """Notes and tool trouble from earlier runs like this one, as text for the model's context ("" when none)."""
    d = all_items()
    query = f"{agent_name} {_text(task)}"
    lines, used = [], 0
    notes = d["notes"]
    if notes:
        scored = [(s, notes[i]) for s, i in _related(query, [_doc(n) for n in notes])]
        for side, head in (("worked", "Worked:"), ("failed", "Didn't work (don't repeat):")):
            got = []
            for _, n in scored:
                if n["side"] != side or len(got) >= 6:
                    continue
                who = n.get("model") or "a model"
                line = f"- {n['text']} ({who}{', ' + n['agent'] if n.get('agent') else ''})"
                t = len(line) // 4 + 1
                if used + t > budget_tokens * 0.8:
                    break
                got.append(line)
                used += t
            if got:
                lines += [head] + got
    runs = d["runs"]
    if runs:
        rel = [runs[i] for i in sorted(i for _, i in _related(query, [_doc(r) for r in runs]))][-60:]
        stats = {}
        for r in rel:
            for t in r["tools"]:
                st = stats.setdefault(t["name"], {"n": 0, "bad": 0, "denied": 0, "last": ""})
                st["n"] += 1
                if not t["ok"]:
                    st["bad"] += 1
                    st["denied"] += 1 if t.get("denied") else 0
                    st["last"] = t.get("err") or st["last"]
        trouble = []
        for name, st in sorted(stats.items(), key=lambda kv: -kv[1]["bad"]):
            if st["bad"] >= 2 and st["bad"] * 2 >= st["n"]:
                what = f"the user refused it {st['denied']} times" if st["denied"] * 2 >= st["bad"] else \
                    f"last error: {st['last'][:120]}"
                trouble.append(f"- {name} failed {st['bad']} of {st['n']} times ({what})")
        if trouble:
            lines += ["Tool trouble on tasks like this:"] + trouble[:5]
    if not lines:
        return ""
    return "\n".join(["# What earlier runs learned (shared by every model)",
                      "Notes from earlier tasks like this one, written by the models that did them. Reuse what "
                      "worked, don't repeat what failed, and still check the result yourself."] + lines)


# ------------------------------------------------------------------------------------------------ UI

def overview():
    """For Memory > Learned: the notes, and per-model numbers from the run records."""
    d = all_items()
    models = {}
    for r in d["runs"]:
        m = models.setdefault(r.get("model") or "unknown", {"runs": 0, "calls": 0, "failed": 0, "refused": 0,
                                                             "speeds": [], "last": 0})
        m["runs"] += 1
        m["calls"] += len(r["tools"])
        m["failed"] += sum(1 for t in r["tools"] if not t["ok"] and not t.get("denied"))
        m["refused"] += sum(1 for t in r["tools"] if t.get("denied"))
        if r.get("tok_s"):
            m["speeds"].append(r["tok_s"])
        m["last"] = max(m["last"], r["ts"])
    tools = {}
    for r in d["runs"]:
        for t in r["tools"]:
            st = tools.setdefault(t["name"], {"calls": 0, "failed": 0})
            st["calls"] += 1
            st["failed"] += 0 if t["ok"] else 1
    return {"notes": sorted(d["notes"], key=lambda n: -n["ts"]),
            "models": [{"model": k, "runs": v["runs"], "calls": v["calls"], "failed": v["failed"],
                        "refused": v["refused"], "last": v["last"],
                        "tok_s": round(sum(v["speeds"]) / len(v["speeds"]), 1) if v["speeds"] else None}
                       for k, v in sorted(models.items(), key=lambda kv: -kv[1]["last"])],
            "tools": [{"name": k, **v} for k, v in sorted(tools.items(), key=lambda kv: -kv[1]["calls"])][:40],
            "runs": len(d["runs"])}


def delete_note(note_id):
    with _lock:
        d = _load()
        before = len(d["notes"])
        d["notes"] = [n for n in d["notes"] if n["id"] != note_id]
        _save(d)
        return len(d["notes"]) < before


def clear():
    with _lock:
        _save({"runs": [], "notes": []})
