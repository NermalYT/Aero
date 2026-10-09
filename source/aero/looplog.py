"""Forever-loop journal: what the model learned while repeating one task.

After every round of a forever-loop, the local model looks back at that round (its tool calls, which failed, its
final answer) and writes down what worked, what didn't, and what it plans next. The journal goes into the next
round's <turn_context>, so the model keeps what works, stops repeating what failed, and picks up its own plan.

One journal per task (the loop's prompt), in data/loops/<hash>.json, so starting the same loop again later (or the
chat moving to a fresh one when it compacts) keeps everything learned so far. Settings > Forever-loop can switch it
off; the loop bar's Journal button shows it.
"""
import hashlib
import json
import re
import time

import httpx

from .config import DATA

LOOPS = DATA / "loops"
MAX_ENTRIES = 200
SHOW_ITEMS = 8                    # per list in the block the model sees
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["worked", "failed", "next", "status"],
          "properties": {"worked": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 220}},
                         "failed": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 220}},
                         "next": {"type": "string", "maxLength": 300},
                         "status": {"type": "string", "maxLength": 200}}}


def key(task):
    return hashlib.sha1(re.sub(r"\s+", " ", (task or "").strip()).encode("utf-8")).hexdigest()[:12]


def path(task):
    return LOOPS / f"{key(task)}.json"


def load(task):
    try:
        return json.loads(path(task).read_text(encoding="utf-8"))
    except Exception:
        return {"key": key(task), "task": (task or "").strip()[:4000], "entries": [], "created": time.time()}


def save(j):
    LOOPS.mkdir(parents=True, exist_ok=True)
    j["entries"] = j.get("entries", [])[-MAX_ENTRIES:]
    j["updated"] = time.time()
    p = LOOPS / f"{j['key']}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(j, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def summary(j):
    """Lists for the model: newest first, no repeats, and an item that appears on both sides (it failed once and
    worked later, or the other way round) only on its newest side."""
    entries = j.get("entries") or []
    last = {}                                         # normalized item -> (round index, side, text)
    for i, e in enumerate(entries):
        for side in ("worked", "failed"):
            for item in e.get(side) or []:
                n = _norm(item)
                if n:
                    last[n] = (i, side, item)
    ordered = sorted(last.values(), key=lambda v: -v[0])
    return {"rounds": len(entries),
            "worked": [t for _, side, t in ordered if side == "worked"][:SHOW_ITEMS],
            "failed": [t for _, side, t in ordered if side == "failed"][:SHOW_ITEMS],
            "next": next((e.get("next") for e in reversed(entries) if e.get("next")), ""),
            "status": next((e.get("status") for e in reversed(entries) if e.get("status")), "")}


def block(task):
    """The journal as text for the next round's <turn_context>, or "" before the first note."""
    j = load(task)
    if not j.get("entries"):
        return ""
    s = summary(j)
    lines = [f"Your forever-loop journal ({s['rounds']} earlier round{'s' if s['rounds'] != 1 else ''} of this task). "
             "Use what worked, avoid what failed, and start from your planned next step unless it no longer fits."]
    if s["status"]:
        lines.append(f"Where things stand: {s['status']}")
    if s["worked"]:
        lines.append("Worked well:\n" + "\n".join(f"- {w}" for w in s["worked"]))
    if s["failed"]:
        lines.append("Didn't work (don't repeat):\n" + "\n".join(f"- {f}" for f in s["failed"]))
    if s["next"]:
        lines.append(f"Planned next step: {s['next']}")
    return "\n".join(lines)


def _round_text(history, start):
    """This round's work, compact: tool calls with outcome, and the final answer."""
    lines = []
    for m in history[start:]:
        r = m.get("role")
        if r == "assistant" and m.get("tool_calls"):
            for c in m["tool_calls"]:
                f = c.get("function") or {}
                lines.append(f"CALL {f.get('name')}({(f.get('arguments') or '')[:200]})")
        elif r == "tool":
            body = (m.get("content") or "").strip().replace("\n", " ")
            lines.append(f"  -> {'ERROR' if m.get('error') else 'ok'}{' (denied)' if m.get('denied') else ''}: {body[:260]}")
        elif r == "assistant" and (m.get("content") or "").strip():
            lines.append(f"ANSWER ({m.get('lane') or 'local'}): {m['content'].strip()[:1500]}")
        elif r == "review":
            lines.append(f"REVIEW by {m.get('model')}: {m.get('verdict')}: {(m.get('summary') or '')[:400]}")
    text = "\n".join(lines)
    return text[-9000:]


async def reflect(turn, task, iteration):
    """Ask the local model what this round taught it; store it in the journal. Returns the entry, or None."""
    work = _round_text(turn.history, turn.start_index)
    if not work.strip():
        return None
    prev = block(task)
    prompt = ("You are the local AI model inside Aero, running a forever-loop: the same task repeats round after "
              f"round so you can keep improving the result.\n\nTASK:\n{task[:3000]}\n\n"
              + (f"YOUR JOURNAL SO FAR:\n{prev}\n\n" if prev else "")
              + f"WHAT YOU DID IN ROUND {iteration}:\n{work}\n\n"
              "Write this round's journal entry as JSON:\n"
              "- worked: up to 3 approaches, tools, commands or settings that worked well and are worth reusing. "
              "Be specific (name the tool, command, path or setting).\n"
              "- failed: up to 3 things that failed or wasted time, with the reason when you know it, so the next "
              "round avoids them.\n"
              "- next: the most useful next step for the next round.\n"
              "- status: one line on where the task stands now.\n"
              "Only write what this round's work shows; leave a list empty when there is nothing new.")
    body = {"messages": [{"role": "user", "content": prompt}], "temperature": 0.2, "max_tokens": 600,
            "chat_template_kwargs": {"enable_thinking": False}, "stream": False,
            "response_format": {"type": "json_schema", "json_schema": {"name": "loop_journal", "schema": SCHEMA}}}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(240, connect=10)) as c:
            r = await c.post(turn.engine["url"] + "/v1/chat/completions", json=body)
            txt = r.json()["choices"][0]["message"].get("content") or "{}"
        data = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
    except Exception:
        return None
    clean = lambda xs: [re.sub(r"\s+", " ", str(x)).strip()[:220] for x in (xs or []) if str(x).strip()][:3]  # noqa: E731
    entry = {"iteration": int(iteration or 0), "ts": time.time(), "chat_id": turn.chat_id,
             "worked": clean(data.get("worked")), "failed": clean(data.get("failed")),
             "next": re.sub(r"\s+", " ", str(data.get("next") or "")).strip()[:300],
             "status": re.sub(r"\s+", " ", str(data.get("status") or "")).strip()[:200]}
    if not (entry["worked"] or entry["failed"] or entry["next"]):
        return None
    j = load(task)
    j.setdefault("entries", []).append(entry)
    save(j)
    return entry


def clear(task):
    path(task).unlink(missing_ok=True)


def listing():
    out = []
    if LOOPS.exists():
        for p in LOOPS.glob("*.json"):
            try:
                j = json.loads(p.read_text(encoding="utf-8"))
                out.append({"key": j.get("key"), "task": (j.get("task") or "")[:200], "rounds": len(j.get("entries") or []),
                            "updated": j.get("updated")})
            except Exception:
                pass
    return sorted(out, key=lambda r: -(r.get("updated") or 0))
