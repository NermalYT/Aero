"""schedule_task: let the model set up, list and cancel scheduled tasks when the user asks ("every morning at 8, check
my email and GitHub"). Creating one is a lasting change that runs later on its own, so it always needs the user's
explicit OK (agent.exec_tool), its card shows the schedule and the prompt, and a scheduled run can't create more."""
import datetime as dt

from . import tool
from .. import jobs


def _at(s):
    """'2026-10-14 08:00' or '08:00' (today, or tomorrow when that has passed) -> epoch seconds."""
    s = str(s or "").strip()
    now = dt.datetime.now()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%H:%M"):
        try:
            t = dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":
            t = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if t <= now:
                t += dt.timedelta(days=1)
        return t.timestamp()
    raise ValueError("time must look like 2026-10-14 08:00 or 08:00")


def _schedule(a):
    kind = a.get("when")
    if kind == "once":
        return {"type": "once", "at": _at(a.get("time"))}
    if kind == "every":
        return {"type": "every", "minutes": int(a.get("minutes") or 0)}
    if kind == "weekly":
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        d = str(a.get("day") or "").strip().lower()
        return {"type": "weekly", "day": days.index(d) if d in days else int(d), "time": a.get("time")}
    return {"type": kind, "time": a.get("time")}


def _summary(a):
    """The approval card's line: what runs, and when."""
    if a.get("action") != "create":
        return f"{a.get('action') or 'list'} {a.get('id') or ''}".strip()
    when = a.get("when") or ""
    when += f" {a.get('minutes')} min" if when == "every" else f" {a.get('day') or ''} {a.get('time') or ''}"
    prompt = " ".join(str(a.get("prompt") or "").split())
    return f"create \"{a.get('name') or prompt[:40]}\", {' '.join(when.split())}: {prompt[:160]}"


def schedule_task(ctx, action="list", prompt="", when="", time="", minutes=0, day="", name="", same_chat=False, id=""):
    if action == "list":
        rows = jobs.scheduled()
        if not rows:
            return "No scheduled tasks."
        return "\n".join(f"[{r['id']}] {r['name']}: {r['when']}" + ("" if r.get("enabled", True) else " (off)") +
                         (f", next {dt.datetime.fromtimestamp(r['next_run']).strftime('%a %H:%M')}" if r.get('next_run') else "")
                         for r in rows)
    if action == "cancel":
        return f"Cancelled {id}." if jobs.remove(str(id)) else {"text": f"No scheduled task {id}.", "error": True}
    if action != "create":
        return {"text": "action must be create, list or cancel", "error": True}
    try:
        t = jobs.add(prompt, _schedule({"when": when, "time": time, "minutes": minutes, "day": day}), name, same_chat)
    except (ValueError, KeyError, TypeError) as e:
        return {"text": f"Could not schedule it: {e}", "error": True}
    nxt = dt.datetime.fromtimestamp(t["next_run"]).strftime("%a %Y-%m-%d %H:%M") if t.get("next_run") else "never"
    return (f"Scheduled [{t['id']}] \"{t['name']}\": {jobs.describe(t['schedule'])}. First run: {nxt}. Each run is a "
            "chat of its own; actions that need an OK are queued in that chat for the user. The user can change it "
            "under Tasks.")


tool("schedule_task",
     "Set up a task that Aero runs on its own later or on a schedule, list the scheduled tasks, or cancel one. Use it "
     "when the user asks for something to happen at a time or repeatedly (\"every morning\", \"each Friday\", \"in "
     "an hour\"). The prompt is what Aero will be asked each time, written as a complete request.", "setup",
     {"action": {"type": "string", "enum": ["create", "list", "cancel"]},
      "prompt": {"type": "string", "description": "create: the full request to run each time"},
      "when": {"type": "string", "enum": ["once", "every", "daily", "weekdays", "weekly"]},
      "time": {"type": "string", "description": "HH:MM (24 h), or 'YYYY-MM-DD HH:MM' for once"},
      "minutes": {"type": "integer", "description": "every: interval in minutes (at least 5)"},
      "day": {"type": "string", "description": "weekly: monday ... sunday"},
      "name": {"type": "string", "description": "short name shown in the task list"},
      "same_chat": {"type": "boolean", "description": "true: every run continues one chat; false: a new chat each run"},
      "id": {"type": "string", "description": "cancel: the task id from list"}},
     ["action"], summary=lambda a: _summary(a))(schedule_task)
