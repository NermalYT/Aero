"""Questions to the user that don't stop the rest of the work.

An agent missing one detail ("Which Gmail account should I check?") calls ask_user. The question shows as a card in
the chat right away and the tool returns at once, so the agent keeps doing everything that doesn't depend on the
answer. When it reaches the part that does, it calls get_answer, which waits (Stop still works) until the user
answers. A question the user answers after the turn ended is not lost: it stays in data/tasks/questions.json, and
the answer is sent into the same chat as a reply to that question, so the task continues without the request being
typed again.

Each question has an id, the chat it belongs to, the agent that asked (main agent or a subagent), the question text,
optional choices (with or without a free-text answer), the expected kind of answer, and a status: pending,
answered, expired or cancelled. Asking the same question twice in a chat while the first is pending returns the
first one instead of a second card.
"""
import asyncio
import json
import os
import re
import threading
import time
import uuid

from .config import DATA

DIR = DATA / "tasks"
STORE = DIR / "questions.json"
KINDS = ("text", "choice", "yes_no", "number", "date")
KEEP_S = 7 * 86400                 # answered or expired questions are kept a week, then dropped
_lock = threading.RLock()
_waiters = {}                      # question id -> (loop, future)
_mem = {"q": None}


def _load():
    if _mem["q"] is None:
        try:
            _mem["q"] = json.loads(STORE.read_text(encoding="utf-8")).get("questions", [])
        except (OSError, ValueError):
            _mem["q"] = []
    return _mem["q"]


def _save():
    now = time.time()
    qs = [q for q in _load() if q["status"] == "pending" or now - (q.get("closed_at") or q["created"]) < KEEP_S]
    _mem["q"] = qs
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"schema": 1, "questions": qs}, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STORE)


def _key(text):
    return re.sub(r"\W+", " ", str(text or "").lower()).strip()


def ask(chat_id, text, choices=None, kind="text", allow_free=True, owner="", timeout_s=0, context=""):
    """Post a question. Returns (question, created). A pending question with the same text in this chat is reused."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()[:400]
    if not text:
        raise ValueError("ask_user needs the question text.")
    choices = [str(c).strip()[:120] for c in (choices or []) if str(c).strip()][:8]
    kind = kind if kind in KINDS else ("choice" if choices else "text")
    with _lock:
        for q in _load():
            if q["chat_id"] == chat_id and q["status"] == "pending" and q["key"] == _key(text):
                return dict(q), False
        q = {"id": "q_" + uuid.uuid4().hex[:8], "chat_id": chat_id, "text": text, "choices": choices, "kind": kind,
             "allow_free": bool(allow_free or not choices), "owner": owner or "", "context": str(context or "")[:300],
             "created": time.time(), "timeout_s": int(timeout_s or 0), "status": "pending", "answer": None,
             "key": _key(text)}
        _load().append(q)
        _save()
        return dict(q), True


def get(qid):
    with _lock:
        q = next((q for q in _load() if q["id"] == qid), None)
        return dict(q) if q else None


def pending(chat_id=None):
    with _lock:
        _expire()
        return [dict(q) for q in _load() if q["status"] == "pending" and (chat_id is None or q["chat_id"] == chat_id)]


def _expire():
    now, changed = time.time(), False
    for q in _load():
        if q["status"] == "pending" and q.get("timeout_s") and now - q["created"] > q["timeout_s"]:
            q.update(status="expired", closed_at=now)
            changed = True
    if changed:
        _save()


def validate(q, value):
    """The answer as stored, or raise ValueError when it doesn't fit the question."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError("The answer is empty.")
    v = str(value).strip()[:2000]
    if q["kind"] == "yes_no":
        low = v.lower()
        if low in ("y", "yes", "true", "ok", "sure"):
            return "yes"
        if low in ("n", "no", "false", "nope"):
            return "no"
        if not q["allow_free"]:
            raise ValueError("Answer yes or no.")
    if q["kind"] == "number":
        try:
            float(v.replace(",", ""))
        except ValueError:
            if not q["allow_free"]:
                raise ValueError("Answer with a number.")
    if q["choices"] and not q["allow_free"]:
        m = next((c for c in q["choices"] if c.lower() == v.lower()), None)
        if m is None:
            raise ValueError("Pick one of: " + ", ".join(q["choices"]))
        return m
    if q["choices"]:
        m = next((c for c in q["choices"] if c.lower() == v.lower()), None)
        return m or v
    return v


def answer(qid, value):
    """Record the user's answer and wake the agent waiting for it. Returns the updated question."""
    with _lock:
        q = next((q for q in _load() if q["id"] == qid), None)
        if q is None:
            raise KeyError(qid)
        if q["status"] != "pending":
            return dict(q)
        q["answer"] = validate(q, value)
        q["status"] = "answered"
        q["closed_at"] = time.time()
        _save()
        out = dict(q)
    w = _waiters.pop(qid, None)
    if w:
        loop, fut = w
        loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(out["answer"]))
    return out


def cancel(qid, why="cancelled"):
    with _lock:
        q = next((q for q in _load() if q["id"] == qid), None)
        if q and q["status"] == "pending":
            q.update(status="cancelled", closed_at=time.time(), why=why)
            _save()
    w = _waiters.pop(qid, None)
    if w:
        loop, fut = w
        loop.call_soon_threadsafe(lambda: fut.done() or fut.cancel())


async def wait(qid, cancel_event=None, timeout=900.0):
    """The answer, or None when Stop was pressed, the question was cancelled, or timeout passed."""
    q = get(qid)
    if q is None:
        return None
    if q["status"] == "answered":
        return q["answer"]
    if q["status"] != "pending":
        return None
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _waiters[qid] = (loop, fut)
    try:
        q = get(qid)                                   # answered between the first look and registering
        if q and q["status"] == "answered":
            return q["answer"]
        t0 = time.monotonic()
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return None
            left = timeout - (time.monotonic() - t0)
            if left <= 0:
                return None
            try:
                return await asyncio.wait_for(asyncio.shield(fut), timeout=min(0.5, left))
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                if fut.cancelled():
                    return None
                raise
    finally:
        if _waiters.get(qid, (None, None))[1] is fut:
            _waiters.pop(qid, None)


def public(q):
    return {k: q.get(k) for k in ("id", "chat_id", "text", "choices", "kind", "allow_free", "owner", "context",
                                  "created", "status", "answer")}


def reset_cache():
    _mem["q"] = None
