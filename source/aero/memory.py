"""Long-term memory that survives across chats.

Two kinds of things are remembered, both in data/memory.json:
  facts      short lasting notes ("the user's GPU is an RX 7800 XT", "prefers PowerShell") saved by the model's
             remember tool, by the user in Settings > Memory, or pulled out of a chat when it is compacted
  summaries  one hand-off summary per compacted or finished chat (what was done, decisions, open tasks)

Every chat's system prompt gets the user's profile: what they wrote about themselves in Settings > Memory, every
learned preference and personal fact, and a short timeline of recent chats. The newest message also gets a recall
block: pinned facts, the facts most relevant to it, then the newest ones, plus summaries of related past chats,
all inside a token budget. Compacting
a chat summarizes it with the loaded model, saves the summary and new facts, and starts a fresh chat that
carries the summary, so a long-running session never runs out of context.
"""
import difflib
import json
import math
import re
import threading
import time
import uuid

import httpx

from .config import CHATS, DATA

PATH = DATA / "memory.json"
MAX_FACTS = 3000
MAX_SUMMARIES = 2000
_lock = threading.RLock()

STOP = set("""a an and are as at be but by can could did do does for from had has have he her his how i if in into is
it its just me my of on or our she so than that the their them then there these they this to too us was we were what
when where which who why will with would you your yours i'm it's don't im dont yes no ok okay also not all any
some more most very really get got like one two use used using""".split())


# ---- store ---------------------------------------------------------------------------------

def _load():
    try:
        d = json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d.setdefault("facts", [])
    d.setdefault("summaries", [])
    return d


def _save(d):
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(PATH)


def all_items():
    with _lock:
        return _load()


def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s.%/:-]", "", (s or "").lower())).strip()


def add_fact(text, kind="fact", source=None, pinned=False):
    """Add a fact, or refresh a near-duplicate instead of storing it twice. Returns (fact, 'added'|'updated')."""
    text = re.sub(r"\s+", " ", (text or "").strip())[:600]
    if not text:
        raise ValueError("empty memory")
    now = time.time()
    with _lock:
        d = _load()
        n = _norm(text)
        for f in d["facts"]:
            if difflib.SequenceMatcher(None, _norm(f["text"]), n).ratio() >= 0.86:
                f.update(text=text, updated=now)
                f["pinned"] = f.get("pinned") or pinned
                _save(d)
                return f, "updated"
        f = {"id": uuid.uuid4().hex[:10], "text": text, "kind": kind or "fact", "created": now, "updated": now,
             "source": source, "pinned": bool(pinned)}
        d["facts"].append(f)
        if len(d["facts"]) > MAX_FACTS:       # drop the oldest unpinned ones
            unpinned = sorted((x for x in d["facts"] if not x.get("pinned")), key=lambda x: x["updated"])
            drop = {x["id"] for x in unpinned[:len(d["facts"]) - MAX_FACTS]}
            d["facts"] = [x for x in d["facts"] if x["id"] not in drop]
        _save(d)
        return f, "added"


def update_fact(fid, **fields):
    with _lock:
        d = _load()
        for f in d["facts"]:
            if f["id"] == fid:
                for k in ("text", "kind", "pinned"):
                    if k in fields and fields[k] is not None:
                        f[k] = fields[k]
                f["updated"] = time.time()
                _save(d)
                return f
    return None


def delete_fact(fid):
    with _lock:
        d = _load()
        n = len(d["facts"])
        d["facts"] = [f for f in d["facts"] if f["id"] != fid]
        _save(d)
        return len(d["facts"]) < n


def save_summary(chat_id, title, summary, open_tasks=None, next_chat=None):
    with _lock:
        d = _load()
        d["summaries"] = [s for s in d["summaries"] if s["chat_id"] != chat_id]
        d["summaries"].append({"chat_id": chat_id, "title": title or "Chat", "summary": summary,
                               "open_tasks": open_tasks or [], "created": time.time(), "next_chat": next_chat})
        d["summaries"] = d["summaries"][-MAX_SUMMARIES:]
        _save(d)


def delete_summary(chat_id):
    with _lock:
        d = _load()
        d["summaries"] = [s for s in d["summaries"] if s["chat_id"] != chat_id]
        _save(d)


def summary_of(chat_id):
    return next((s for s in all_items()["summaries"] if s["chat_id"] == chat_id), None)


# ---- search --------------------------------------------------------------------------------

def _terms(s):
    return [w for w in re.findall(r"[a-z0-9][a-z0-9_.+-]*", (s or "").lower()) if len(w) > 1 and w not in STOP]


def _rank(query, docs):
    """BM25 over short documents. docs: list of strings. Returns scores in the same order."""
    q = set(_terms(query))
    if not q or not docs:
        return [0.0] * len(docs)
    toks = [_terms(x) for x in docs]
    N = len(docs)
    avg = sum(len(t) for t in toks) / N or 1
    df = {w: sum(1 for t in toks if w in t) for w in q}
    out = []
    for t in toks:
        tf = {}
        for w in t:
            if w in q:
                tf[w] = tf.get(w, 0) + 1
        s = 0.0
        for w, c in tf.items():
            idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * c * 2.2 / (c + 1.2 * (0.25 + 0.75 * len(t) / avg))
        out.append(s)
    return out


def search(query, limit=8, chats=True):
    """Facts, chat summaries and (optionally) raw text of recent chats that match the query."""
    d = all_items()
    facts, sums = d["facts"], d["summaries"]
    hits = []
    for f, s in zip(facts, _rank(query, [f["text"] for f in facts])):
        if s > 0:
            hits.append((s, {"type": "fact", "id": f["id"], "text": f["text"], "when": f["updated"]}))
    for m, s in zip(sums, _rank(query, [f"{x['title']} {x['summary']} {' '.join(x.get('open_tasks') or [])}" for x in sums])):
        if s > 0:
            hits.append((s * 0.9, {"type": "chat_summary", "chat_id": m["chat_id"], "title": m["title"],
                                   "text": m["summary"], "when": m["created"]}))
    if chats:
        hits += _search_chat_text(query)
    hits.sort(key=lambda x: -x[0])
    return [h for _, h in hits[:limit]]


def _search_chat_text(query, max_files=300):
    """Snippets from the raw messages of recent chats (newest files first)."""
    q = _terms(query)
    if not q:
        return []
    files = sorted(CHATS.glob("*.json"), key=lambda p: -p.stat().st_mtime)[:max_files]
    snippets, texts = [], []
    for p in files:
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for m in c.get("messages") or []:
            if m.get("role") in ("user", "assistant") and m.get("content"):
                t = m["content"]
                low = t.lower()
                if any(w in low for w in q):
                    i = min((low.find(w) for w in q if w in low), default=0)
                    snip = t[max(0, i - 200): i + 400].strip()
                    snippets.append({"type": "chat_message", "chat_id": c.get("id"), "title": c.get("title") or "Chat",
                                     "role": m["role"], "text": snip, "when": m.get("ts") or c.get("updated", 0)})
                    texts.append(snip)
    return [(s * 0.7, h) for s, h in zip(_rank(query, texts), snippets) if s > 0][:40]


# ---- what goes into the system prompt ----------------------------------------------------------

def _tok(s):
    return len(s) // 3 + 2


PROFILE_KINDS = ("preference", "user")


def _first_sentence(text, n=170):
    t = re.sub(r"\s+", " ", text or "").strip()
    m = re.match(r"(.{20,}?[.!?])(\s|$)", t)
    t = m.group(1) if m else t
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "..."


def profile_block(profile_text, budget_tokens=1200, learned=True, recent_chats=6):
    """The always-on 'about the user' part of every model's system prompt: the profile the user wrote, every
    learned preference and personal fact (pinned first, then newest), and a short timeline of recent chats.
    Returns (text, ids of the facts it holds) so per-turn recall doesn't repeat them."""
    profile_text = (profile_text or "").strip()
    d = all_items() if learned else {"facts": [], "summaries": []}
    prefs = [f for f in d["facts"] if f.get("kind") in PROFILE_KINDS]
    if not profile_text and not prefs and not d["summaries"]:
        return "", set()
    lines = ["# About the user",
             "Who the user is and how they want you to work and write. Follow this in every answer, whatever the "
             "task, without mentioning it. Anything the user says in this chat wins over it."]
    used = _tok(lines[1]) + 10
    if profile_text:
        cap = int(budget_tokens * 0.6) * 3
        txt = profile_text if len(profile_text) <= cap else profile_text[:cap].rsplit("\n", 1)[0] + "\n..."
        lines += ["", "## In their own words", txt]
        used += _tok(txt) + 6
    ids = set()
    for kind, head in (("preference", "## Preferences learned from earlier chats"), ("user", "## Facts about them")):
        group = sorted((f for f in prefs if f.get("kind") == kind), key=lambda f: (not f.get("pinned"), -f["updated"]))
        got = []
        for f in group:
            t = _tok(f["text"]) + 2
            if used + t > budget_tokens * 0.85:
                break
            got.append(f)
            used += t
        if got:
            lines += ["", head] + [f"- {f['text']}" for f in sorted(got, key=lambda f: f["created"])]
            ids |= {f["id"] for f in got}
    sums = sorted(d["summaries"], key=lambda x: -x["created"])[:recent_chats]
    hist = []
    for x in sums:
        when = time.strftime("%Y-%m-%d", time.localtime(x["created"]))
        line = f"- {when} · {x['title']}: {_first_sentence(x['summary'])}"
        if used + _tok(line) > budget_tokens:
            break
        hist.append(line)
        used += _tok(line)
    if hist:
        lines += ["", "## Recent chats (newest first; use recall for details)"] + hist
    return "\n".join(lines), ids


def context_block(query, budget_tokens=1500, skip_chats=(), skip_facts=()):
    """Memory text for the newest message: pinned facts, then the most relevant, then the newest; plus
    summaries of related past chats. Stays inside budget_tokens. skip_facts: ids the profile block already holds."""
    d = all_items()
    facts, sums = [f for f in d["facts"] if f["id"] not in skip_facts], d["summaries"]
    if not facts and not sums:
        return ""
    picked, used = [], 0
    fact_budget = int(budget_tokens * 0.7)

    def take(f):
        nonlocal used
        if f in picked:
            return True
        t = _tok(f["text"]) + 2
        if used + t > fact_budget:
            return False
        picked.append(f)
        used += t
        return True

    for f in sorted((f for f in facts if f.get("pinned")), key=lambda f: -f["updated"]):
        take(f)
    scores = _rank(query, [f["text"] for f in facts])
    for s, f in sorted(zip(scores, facts), key=lambda x: -x[0]):
        if s <= 0 or not take(f):
            break
    for f in sorted(facts, key=lambda f: -f["updated"]):
        if not take(f):
            break

    lines = ["# Long-term memory",
             "Facts remembered from earlier chats with this user. Use them naturally, don't recite them, and "
             "trust newer information from the user over them. Save new lasting facts with the remember tool; "
             "search older chats with the recall tool."]
    lines += [f"- {'Lesson from a past review: ' if f.get('kind') == 'lesson' else ''}{f['text']}"
              for f in sorted(picked, key=lambda f: f["created"])]
    left = budget_tokens - used - 80
    cands = [s for s in sums if s["chat_id"] not in skip_chats]
    if cands and left > 150:
        sc = _rank(query, [f"{x['title']} {x['summary']}" for x in cands])
        ranked = [x for s, x in sorted(zip(sc, cands), key=lambda z: -z[0]) if s > 0][:2]
        newest = sorted(cands, key=lambda x: -x["created"])[:1]
        rel = []
        for x in ranked + newest:
            if x not in rel:
                rel.append(x)
        if rel:
            lines.append("\n## Related past chats")
            for x in rel:
                txt = x["summary"]
                room = max(60, min(len(txt), (left // len(rel)) * 3))
                when = time.strftime("%Y-%m-%d", time.localtime(x["created"]))
                lines.append(f"- {x['title']} ({when}): {txt[:room]}{'...' if len(txt) > room else ''}")
    return "\n".join(lines)


def carry_block(carry):
    if not carry or not carry.get("summary"):
        return ""
    out = ["# Continuing an earlier chat",
           f"This chat continues \"{carry.get('from_title') or 'an earlier chat'}\", which was compacted to free "
           "context. Pick up where it left off; the user may refer to things from it.",
           "", "Summary so far:", carry["summary"]]
    if carry.get("open_tasks"):
        out += ["", "Open tasks:"] + [f"- {t}" for t in carry["open_tasks"]]
    return "\n".join(out)


# ---- compaction --------------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "open_tasks": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "facts": {"type": "array", "maxItems": 15, "items": {
            "type": "object",
            "properties": {"text": {"type": "string"},
                           "kind": {"type": "string", "enum": ["user", "preference", "project", "environment", "other"]}},
            "required": ["text", "kind"]}},
    },
    "required": ["summary", "open_tasks", "facts"],
}

INSTRUCT = """You are compacting a conversation between a user and their local AI agent so the agent can continue \
in a fresh chat with an empty context. Write JSON with:
- summary: a hand-off for the agent, written to it ("You were..."). Cover the goal, what was done and found, \
decisions and the user's stated preferences, the current state (files, paths, commands, settings, numbers, errors, \
URLs that matter) and what was about to happen next. Be specific and dense; no filler. Up to about 450 words.
- open_tasks: unfinished work or promises, each one short line. Empty if none.
- facts: only LASTING facts worth remembering in every future chat: who the user is, their hardware and software, \
preferences, ongoing projects, names, accounts, recurring decisions. Use kind "preference" for how the user wants \
the assistant to work and write (tone, length, format, level of detail, what annoys them, words they like), \
including preferences shown by their corrections ("too long", "just do it") and not only ones stated outright. Each one short sentence that makes sense on its \
own. Skip anything only relevant to this one task, anything already in the known facts, and all secrets \
(passwords, keys, tokens)."""


_CLOUD_WHO = {"fable": "REVIEWER (Claude Fable 5.1)", "opus": "CLAUDE OPUS", "astra": "REVIEWER (GPT-6 Astra)",
              "sol": "GPT-6.1 SOL"}


def _render(history, per_tool=700, per_file=1500):
    """Plain-text transcript of a stored chat for summarizing (no images, long outputs cut)."""
    out = []
    for m in history:
        r = m.get("role")
        lane = m.get("lane") or "local"
        if r in ("router", "lesson", "notice"):
            continue
        if r == "review":
            out.append(f"CLOUD REVIEW by {m.get('model') or 'the reviewer'} ({m.get('verdict')}): {m.get('summary', '')}")
            continue
        if lane in _CLOUD_WHO and r == "assistant":
            t = (m.get("content") or "").strip()
            if t:
                out.append(f"{_CLOUD_WHO[lane]}: {t}")
            continue
        if lane in _CLOUD_WHO and r == "tool":
            continue
        if r == "user" and m.get("from") in ("fable", "astra"):
            out.append(f"REVIEWER'S FIX REQUEST: {(m.get('content') or '')[:per_file]}")
            continue
        if r == "user":
            t = m.get("content") or ""
            for p in m.get("pastes") or []:
                pt = p.get("text", "")
                t += f"\n[pasted text: {pt[:per_file]}{'...' if len(pt) > per_file else ''}]"
            for a in m.get("attachments") or []:
                t += f"\n[attached {a.get('kind', 'file')}: {a.get('name')}]"
            out.append(f"USER: {t}")
        elif r == "assistant":
            t = (m.get("content") or "").strip()
            if t:
                out.append(f"ASSISTANT: {t}")
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                out.append(f"ASSISTANT CALLED {fn.get('name')}({(fn.get('arguments') or '')[:400]})")
        elif r == "tool":
            c = m.get("content") or ""
            out.append(f"TOOL RESULT ({m.get('name')}): {c[:per_tool]}{'...' if len(c) > per_tool else ''}")
        elif r == "summary":
            out.append(f"EARLIER PART OF THIS CHAT (already summarized): {m.get('content', '')}")
    return "\n\n".join(out)


async def _ask(url, prompt, max_tokens=1800, schema=None):
    body = {"messages": [{"role": "user", "content": prompt}], "temperature": 0.2, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False}, "stream": False}
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "compaction", "schema": schema}}
    async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=10)) as c:
        r = await c.post(url + "/v1/chat/completions", json=body)
        if r.status_code != 200:
            raise RuntimeError(f"llama-server error {r.status_code}: {r.text[:300]}")
        t = r.json()["choices"][0]["message"].get("content") or ""
    return re.sub(r"<think>.*?</think>", "", t, flags=re.S).strip()


async def _chars_per_token(url, text):
    """Characters per token for this model and this kind of text, measured with llama-server's tokenizer."""
    sample = text[:24000] or "hello"
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(url + "/tokenize", json={"content": sample})
            n = len(r.json().get("tokens") or [])
        return max(1.0, min(6.0, len(sample) / max(1, n)))
    except Exception:
        return 2.5


async def summarize(url, ctx, history, title="", carry=None, progress=None):
    """Summarize a chat with the loaded model. Long chats are read in pieces with a rolling summary, so
    this works even when the chat is far bigger than the context window. Returns dict(summary, open_tasks, facts)."""
    transcript = _render(history)
    known = "\n".join(f"- {f['text']}" for f in all_items()["facts"][-60:]) or "(none yet)"
    prior = carry_block(carry) if carry else ""
    cpt = await _chars_per_token(url, transcript)
    fixed = (len(INSTRUCT) + len(known) + len(prior)) / cpt + 1200       # prompt text around the transcript
    room_tok = ctx - 2000 - fixed - 300                                    # minus the reply and a margin
    room = max(1500, int(room_tok * cpt * 0.9))                            # characters of transcript per request
    if len(transcript) > room * 6:                       # very long: keep tool output and pastes shorter
        transcript = _render(history, per_tool=250, per_file=500)
    pieces = [transcript[i:i + room] for i in range(0, len(transcript), room)] or [""]
    rolling = ""
    for i, piece in enumerate(pieces[:-1]):
        if progress:
            progress(f"Reading part {i + 1} of {len(pieces)}")
        rolling = await _ask(url, (f"{prior}\n\n" if prior else "") +
                             ("Running notes so far:\n" + rolling + "\n\n" if rolling else "") +
                             f"Next part of the conversation \"{title}\":\n{piece}\n\n"
                             "Update the running notes so they cover everything above that matters for continuing "
                             "the work: goal, decisions, facts about the user, current state, open tasks. Plain text, "
                             "at most 600 words.", max_tokens=1200)
    if progress:
        progress("Writing the summary")
    prompt = (INSTRUCT + f"\n\nKnown facts (don't repeat these):\n{known}\n\n" +
              (f"{prior}\n\n" if prior else "") +
              (f"Notes on the earlier part of this conversation:\n{rolling}\n\n" if rolling else "") +
              f"Conversation \"{title}\"{' (final part)' if rolling else ''}:\n{pieces[-1]}")
    try:
        raw = await _ask(url, prompt, max_tokens=2000, schema=SCHEMA)
    except RuntimeError as e:                   # some models/builds can't build the grammar: ask for JSON plainly
        if "400" not in str(e) or "context" in str(e):
            raise
        raw = await _ask(url, prompt + '\n\nReply with only the JSON object: {"summary": "...", "open_tasks": '
                         '["..."], "facts": [{"text": "...", "kind": "user"}]}', max_tokens=2000)
    try:
        j = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception:
        j = {"summary": raw[:4000], "open_tasks": [], "facts": []}
    return {"summary": (j.get("summary") or "").strip(),
            "open_tasks": [t.strip() for t in j.get("open_tasks") or [] if str(t).strip()],
            "facts": [f for f in j.get("facts") or [] if isinstance(f, dict) and str(f.get("text", "")).strip()]}


SECRET_RE = re.compile(r"(password|passwd|passcode|api[_ -]?key|secret[_ -]?key|access[_ -]?token|auth[_ -]?token)\s*(is|:|=)"
                       r"|\b(hf|sk|ghp|gho|xox[bp])[_-][A-Za-z0-9]{12,}", re.I)


def store_result(chat_id, title, res, next_chat=None):
    """Save a summarize() result: the chat summary plus any new facts (secrets are never stored)."""
    save_summary(chat_id, title, res["summary"], res["open_tasks"], next_chat)
    added = 0
    for f in res["facts"]:
        if SECRET_RE.search(f["text"]):
            continue
        _, how = add_fact(f["text"], f.get("kind") or "fact", source=chat_id)
        added += how == "added"
    return added
