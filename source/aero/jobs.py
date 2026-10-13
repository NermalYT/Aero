"""Tasks that outlive the window (1.2.75): interrupted work can be resumed, and tasks can run on a schedule.

Interrupted work. Every top-level chat turn is recorded while it runs (track_start / track_end, from pipeline.run_turn;
forever-loop rounds carry their round and loop settings, and a loop between two rounds is "loop_wait"). When Aero
starts and finds a record still "running" or "loop_wait", Aero was closed (or crashed) in the middle of it: it becomes
"interrupted", and the UI offers Resume. Resuming a turn sends the chat a continuation that says what the task graph
finished and what it didn't (resume_text); resuming a forever-loop restarts its rounds with the same prompt. Nothing
resumes on its own: the user decides. Scheduled runs are not offered for resuming (their task shows "interrupted"
and runs again at its next time; Run now runs it at once).

Scheduled tasks. A task has a prompt and a schedule:
    {"type": "once", "at": <epoch>}            {"type": "every", "minutes": N}        (N >= 5)
    {"type": "daily", "time": "HH:MM"}          {"type": "weekdays", "time": "HH:MM"}
    {"type": "weekly", "day": 0-6 (Mon=0), "time": "HH:MM"}
The scheduler (a thread started with the server) runs a task when it is due, the model is loaded and no other turn
is running (it waits otherwise, so it never competes with the user's own chat). Each run is one turn in a chat of
its own ("<name> · <date>"), or always the same chat when the task says so, saved like any chat, with the
forever-loop approval mode (Settings > Autonomy): nobody is watching, so actions that need an OK are queued (the
chat shows the queue card; approving them there runs them), questions get no card (the model picks a sensible
default) and sign-in pages are skipped. A run's next time is set before it starts, so a run that fails or is cut off
is never repeated at once. A run that was due while Aero was closed runs once when Aero is back (not once per missed
slot).

Store: data/jobs.json {"turns": {id: record}, "scheduled": {id: task}}. Records keep the task text, never tool
output.
"""
import asyncio
import concurrent.futures
import datetime as dt
import json
import logging
import os
import threading
import time
import uuid

from .config import CHATS, read_store, write_store

log = logging.getLogger("aero.jobs")

STORE = "jobs.json"
KEEP_TURNS = 200
MIN_EVERY = 5
RETRY_S = 300                  # a run that could not start (its chat was busy) tries again after this
_lock = threading.RLock()
_runner = {"thread": None, "running": None, "chat": None, "stop": threading.Event(), "hooks": None}


def _load():
    d = read_store(STORE, {})
    d.setdefault("turns", {})
    d.setdefault("scheduled", {})
    return d


def _save(d):
    turns = d["turns"]
    if len(turns) > KEEP_TURNS:                    # keep the newest; interrupted ones until dismissed
        old = sorted((t for t in turns.values() if t["status"] != "interrupted"), key=lambda t: t.get("started", 0))
        for t in old[:len(turns) - KEEP_TURNS]:
            turns.pop(t["id"], None)
    write_store(STORE, d)


def _chat_exists(cid):
    return bool(cid) and (CHATS / f"{cid}.json").exists()


# ---- interrupted work -----------------------------------------------------------------------------------------

def track_start(chat_id, title, task, loop=None, job=None):
    """Record a turn as running. Never fails the turn: a data folder that can't be written only loses the record."""
    rid = uuid.uuid4().hex[:12]
    try:
        with _lock:
            d = _load()
            for t in d["turns"].values():          # the loop's next round (or a new message) supersedes the wait
                if t["chat_id"] == chat_id and t["status"] == "loop_wait":
                    t["status"] = "done"
            d["turns"][rid] = {"id": rid, "chat_id": chat_id, "title": (title or "")[:120],
                               "task": (task or "")[:4000 if loop else 400], "loop": loop or None, "job": job,
                               "status": "running", "started": time.time()}
            _save(d)
    except Exception as e:  # noqa: BLE001
        log.warning("could not record the turn: %s", e)
    return rid


def track_end(rid, status):
    try:
        with _lock:
            d = _load()
            t = d["turns"].get(rid)
            if t:
                if status == "done" and t.get("loop") and not t.get("job"):
                    status = "loop_wait"           # the UI starts the next round, or /api/loop/stop ends it
                t.update(status=status, ended=time.time())
                if status == "interrupted":
                    t["interrupted_at"] = time.time()
                _save(d)
    except Exception as e:  # noqa: BLE001
        log.warning("could not record the end of the turn: %s", e)


def track_rebind(rid, new_chat_id):
    """Compaction moved the turn to a new chat id."""
    try:
        with _lock:
            d = _load()
            t = d["turns"].get(rid)
            if t:
                t["chat_id"] = new_chat_id
                _save(d)
    except Exception:  # noqa: BLE001
        pass


def loop_stopped():
    """The forever-loop ended on purpose (Stop, its round limit, an error): nothing to resume."""
    with _lock:
        d = _load()
        n = 0
        for t in d["turns"].values():
            if t["status"] == "loop_wait":
                t["status"] = "done"
                n += 1
        if n:
            _save(d)


def mark_interrupted():
    """At startup: whatever was still running (or a forever-loop between rounds) when Aero stopped was interrupted;
    so was a scheduled run."""
    n = 0
    with _lock:
        d = _load()
        for t in d["turns"].values():
            if t["status"] in ("running", "loop_wait"):
                t.update(status="interrupted", interrupted_at=time.time())
                n += 1
        for s in d["scheduled"].values():
            if s.get("last_status") == "running":
                s["last_status"] = "interrupted"
                n += 1
        if n:
            _save(d)
    return n


def interrupted():
    """What the UI offers to resume: interrupted chat turns whose chat still exists (not scheduled runs)."""
    d = _load()
    return sorted((t for t in d["turns"].values() if t["status"] == "interrupted" and not t.get("job")
                   and _chat_exists(t["chat_id"])), key=lambda t: -t["started"])


def dismiss(rid):
    with _lock:
        d = _load()
        t = d["turns"].get(rid)
        if not t:
            return False
        t["status"] = "dismissed"
        _save(d)
        return True


def resume_text(rec):
    """The continuation a resumed chat gets: what the task graph finished and what it didn't."""
    from . import task_graph
    done, rest = [], []
    for g in task_graph.latest_for_chat(rec["chat_id"], limit=1):
        done = [n.get("label") or n.get("intent") for n in g.get("nodes", []) if n.get("state") == "completed"]
        rest = [n.get("label") or n.get("intent") for n in g.get("nodes", [])
                if n.get("state") not in ("completed", "skipped")]
    when = dt.datetime.fromtimestamp(rec.get("interrupted_at") or time.time()).strftime("%H:%M")
    s = (f"Aero was closed while you were working on this task (noticed at {when}). Continue from where you stopped "
         "and finish it. Check the current state before repeating any action that changes things (a file you were "
         "writing, a message you were sending).")
    if done:
        s += "\nAlready done: " + "; ".join(x for x in done[-10:] if x) + "."
    if rest:
        s += "\nNot finished: " + "; ".join(x for x in rest[-10:] if x) + "."
    return s


def take_for_resume(rid):
    """The record to resume (marked resumed), or None (unknown, already resumed, a scheduled run, or its chat was
    deleted)."""
    with _lock:
        d = _load()
        t = d["turns"].get(rid)
        if not t or t["status"] != "interrupted" or t.get("job"):
            return None
        if not _chat_exists(t["chat_id"]):
            t["status"] = "dismissed"
            _save(d)
            return None
        t["status"] = "resumed"
        _save(d)
        return dict(t)


# ---- schedules ------------------------------------------------------------------------------------------------

def _hm(s):
    h, m = str(s or "").strip().split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError
    return h, m


def clean_schedule(sc, now=None):
    """A valid schedule dict, or ValueError with what is wrong."""
    if not isinstance(sc, dict):
        raise ValueError("schedule must be an object")
    t = sc.get("type")
    if t == "once":
        at = float(sc["at"])
        if at <= (time.time() if now is None else now):
            raise ValueError("once: that time has already passed")
        return {"type": "once", "at": at}
    if t == "every":
        m = int(sc.get("minutes") or 0)
        if m < MIN_EVERY:
            raise ValueError(f"every: at least {MIN_EVERY} minutes")
        return {"type": "every", "minutes": m}
    try:
        if t in ("daily", "weekdays"):
            h, m = _hm(sc.get("time"))
            return {"type": t, "time": f"{h:02d}:{m:02d}"}
        if t == "weekly":
            day = int(sc.get("day"))
            if not 0 <= day <= 6:
                raise ValueError("weekly: day 0 (Monday) to 6 (Sunday)")
            h, m = _hm(sc.get("time"))
            return {"type": "weekly", "day": day, "time": f"{h:02d}:{m:02d}"}
    except (ValueError, TypeError) as e:
        raise ValueError(str(e) or "time must be HH:MM (24 h)") from None
    raise ValueError("schedule type must be once, every, daily, weekdays or weekly")


def next_run(sc, after=None):
    """The next time (epoch seconds, local time) the schedule fires after `after`; None when it never will again."""
    after = time.time() if after is None else after
    t = sc["type"]
    if t == "once":
        return sc["at"] if sc["at"] > after else None
    if t == "every":
        return after + sc["minutes"] * 60
    h, m = _hm(sc["time"])
    base = dt.datetime.fromtimestamp(after)
    for add in range(0, 8):
        day = (base + dt.timedelta(days=add)).replace(hour=h, minute=m, second=0, microsecond=0)
        if day.timestamp() <= after:
            continue
        if t == "weekdays" and day.weekday() >= 5:
            continue
        if t == "weekly" and day.weekday() != sc["day"]:
            continue
        return day.timestamp()
    return None


def describe(sc):
    t = sc["type"]
    if t == "once":
        return "once at " + dt.datetime.fromtimestamp(sc["at"]).strftime("%a %Y-%m-%d %H:%M")
    if t == "every":
        m = sc["minutes"]
        return f"every {m // 60} h" + (f" {m % 60} min" if m % 60 else "") if m >= 60 else f"every {m} min"
    if t == "daily":
        return f"every day at {sc['time']}"
    if t == "weekdays":
        return f"Monday to Friday at {sc['time']}"
    return f"every {['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'][sc['day']]} at {sc['time']}"


def scheduled():
    d = _load()
    out = []
    for s in d["scheduled"].values():
        try:
            when = describe(s["schedule"])
        except Exception:  # noqa: BLE001
            when = "invalid schedule: edit or delete it"
        out.append({**s, "when": when})
    return sorted(out, key=lambda s: (not s.get("enabled", True), s.get("next_run") or 1e18))


def _clean_fields(name, prompt, same_chat):
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("a scheduled task needs a prompt")
    return {"name": (str(name or "").strip()[:80] or prompt[:48]), "prompt": prompt[:4000],
            "same_chat": bool(same_chat)}


def add(prompt, schedule, name="", same_chat=False):
    f = _clean_fields(name, prompt, same_chat)
    sc = clean_schedule(schedule)
    sid = uuid.uuid4().hex[:10]
    task = {"id": sid, **f, "schedule": sc, "chat_id": None, "enabled": True, "created": time.time(),
            "next_run": next_run(sc), "last_run": None, "last_status": None, "runs": 0}
    with _lock:
        d = _load()
        d["scheduled"][sid] = task
        _save(d)
    return task


def update(sid, **kw):
    with _lock:
        d = _load()
        t = d["scheduled"].get(sid)
        if not t:
            return None
        if "schedule" in kw:
            t["schedule"] = clean_schedule(kw["schedule"])
            t["next_run"] = next_run(t["schedule"])
        if any(kw.get(k) is not None for k in ("name", "prompt", "same_chat")):
            t.update(_clean_fields(kw["name"] if kw.get("name") is not None else t.get("name"),
                                   kw["prompt"] if kw.get("prompt") is not None else t.get("prompt"),
                                   kw["same_chat"] if kw.get("same_chat") is not None else t.get("same_chat")))
        if "enabled" in kw:
            t["enabled"] = bool(kw["enabled"])
            if t["enabled"] and not t.get("next_run"):
                t["next_run"] = next_run(t["schedule"])
        _save(d)
        return t


def remove(sid):
    with _lock:
        d = _load()
        ok = d["scheduled"].pop(sid, None) is not None
        _save(d)
        return ok


def due(now=None):
    now = time.time() if now is None else now
    d = _load()
    return [s for s in d["scheduled"].values() if s.get("enabled", True) and s.get("next_run") and s["next_run"] <= now]


def _claim(sid, now=None):
    """Take a task for running, one at a time: its next time is set now (so a run that fails or is cut off isn't
    repeated at once). Returns a copy of the task, or None when another run holds the runner or the task is gone."""
    now = time.time() if now is None else now
    with _lock:
        if _runner["running"]:
            return None
        d = _load()
        t = d["scheduled"].get(sid)
        if not t:
            return None
        _runner["running"] = sid
        try:
            nxt = next_run(t["schedule"], now)
        except Exception:  # noqa: BLE001
            nxt = None
        t.update(last_run=now, last_status="running", next_run=nxt)
        _save(d)
        return dict(t)


def _release():
    with _lock:
        _runner["running"] = None


def _finish_run(sid, status, chat_id, ran_at):
    with _lock:
        d = _load()
        t = d["scheduled"].get(sid)
        if not t:
            return
        if status == "skipped":                    # its chat was busy: try again soon, it didn't run
            t.update(last_status="waiting", next_run=time.time() + RETRY_S)
            _save(d)
            return
        try:
            nxt = next_run(t["schedule"], max(ran_at, time.time()))
        except Exception:  # noqa: BLE001
            nxt = None
        t.update(last_run=ran_at, last_status=status, runs=t.get("runs", 0) + 1, next_run=nxt)
        if t["same_chat"]:
            t["chat_id"] = chat_id
        if t["schedule"].get("type") == "once":
            t["enabled"] = False
        _save(d)


# ---- the runner -----------------------------------------------------------------------------------------------

def _save_chat(chat_id, title, messages, job_id):
    """Write the chat the way the UI does (server.put_chat), so it shows in the chat list. Actions still waiting for
    an OK get the queue card the UI would have shown, so they can be approved from the chat."""
    from . import autonomy
    p = CHATS / (chat_id + ".json")
    c = {}
    if p.exists():
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            c = {}
    messages = list(messages)
    waiting = [autonomy.public(x) for x in autonomy.pending(chat_id)]
    if waiting:
        messages.append({"role": "queue", "id": uuid.uuid4().hex[:10], "items": waiting, "ts": time.time()})
    now = time.time()
    c.update(id=chat_id, title=c.get("title") or title, messages=messages, updated=now, job=job_id, sched_at=now)
    CHATS.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _read_chat(chat_id):
    p = CHATS / (chat_id + ".json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def run_task(task, hooks):
    """One run of a scheduled task: a turn in its chat, saved. hooks: {"state": STATE dict, "settings": fn, "loop":
    the server's event loop}. The caller claimed the task (_claim); this always records how the run ended."""
    from . import agent, pipeline
    ran_at = time.time()
    if task["same_chat"] and task.get("chat_id"):
        chat_id = task["chat_id"]
        if chat_id in agent._cancel:               # the user is working in that chat right now
            _finish_run(task["id"], "skipped", chat_id, ran_at)
            return chat_id, "skipped"
        old = _read_chat(chat_id)
        history, title = list(old.get("messages") or []), old.get("title") or task["name"]
        history = [m for m in history if m.get("role") != "queue"]     # the UI's card records, not conversation
    else:
        chat_id = "sched-" + uuid.uuid4().hex[:10]
        history, title = [], f"{task['name']} · {dt.datetime.now().strftime('%a %d %b %H:%M')}"
    history.append({"role": "user", "content": task["prompt"], "ts": time.time(), "id": uuid.uuid4().hex[:10],
                    "scheduled": task["id"]})
    status, box = "error", {}
    _runner["chat"] = chat_id
    try:
        settings = dict(hooks["settings"]())
        # nobody is watching: actions that need an OK go to the approval queue (never a card that would wait for ever),
        # and the real mouse and keyboard are never used
        mode = settings.get("loop_approval_mode") or "ask"
        settings["approval_mode"] = "batch" if mode == "ask" else mode
        settings["strict_background"] = True

        async def go():
            st = "done"
            async for ev in pipeline.run_turn(chat_id, history, settings, hooks["state"], title=title,
                                              opts={"_sink": lambda turn: box.update(turn=turn), "_job": task["id"]}):
                if '"t": "error"' in ev:
                    st = "error"
            return st
        # on the server's own event loop: approvals and questions are answered there (agent.resolve_approval)
        fut = asyncio.run_coroutine_threadsafe(go(), hooks["loop"])
        try:
            status = fut.result(timeout=6 * 3600)
        except concurrent.futures.TimeoutError:
            fut.cancel()                            # don't leave it running while the next run starts
            status = "error"
    except Exception as e:  # noqa: BLE001
        log.warning("scheduled task %s failed: %s", task.get("id"), e)
        status = "error"
    finally:
        try:
            turn = box.get("turn")
            _save_chat(chat_id, title, (turn.history if turn is not None else history), task["id"])
        except Exception as e:  # noqa: BLE001
            log.warning("could not save the scheduled chat %s: %s", chat_id, e)
        _runner["chat"] = None
        _finish_run(task["id"], status, chat_id, ran_at)
    return chat_id, status


def _loop(hooks):
    while not _runner["stop"].wait(20):
        try:
            ready = (hooks["state"].get("status") == "ready")
            from . import agents
            if not ready or agents.busy() or _runner["running"]:
                continue
            for t in due():
                got = _claim(t["id"])
                if got is None:
                    break
                try:
                    run_task(got, hooks)
                finally:
                    _release()
                break                                  # one at a time; the next waits for the next tick
        except Exception:  # noqa: BLE001
            continue


def start(state, settings_fn, loop):
    """Called once by the server at startup, on its event loop."""
    mark_interrupted()
    hooks = {"state": state, "settings": settings_fn, "loop": loop}
    _runner["hooks"] = hooks
    if _runner["thread"] and _runner["thread"].is_alive():
        return
    _runner["stop"].clear()
    _runner["thread"] = threading.Thread(target=_loop, args=(hooks,), daemon=True, name="aero-scheduler")
    _runner["thread"].start()


def run_now(sid):
    """Run a scheduled task now (in the background). Returns None when it started, else why it didn't."""
    from . import agents
    if not _runner["hooks"]:
        return "The scheduler isn't running."
    if sid not in _load()["scheduled"]:
        return "No such task."
    if agents.busy():
        return "Aero is working on a chat: run it when that has finished."
    t = _claim(sid)
    if t is None:
        return "Another scheduled task is running."

    def go():
        try:
            run_task(t, _runner["hooks"])
        finally:
            _release()
    threading.Thread(target=go, daemon=True).start()
    return None


def chat_running(cid):
    """A scheduled run is working in this chat right now (the UI must not start a second turn in it)."""
    return bool(cid) and _runner["running"] is not None and _runner["chat"] == cid


def status():
    return {"running": _runner["running"], "scheduler": bool(_runner["thread"] and _runner["thread"].is_alive())}
