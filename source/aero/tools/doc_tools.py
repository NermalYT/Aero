"""meeting_doc: turn meeting emails (or meetings the agent already structured) into a checked Word document
(meetings.py). week_only=true just reports the week's date range, so the agent can prepare while it still waits
for the user to pick an account."""
from . import tool
from .. import action_results, meetings


@tool("meeting_doc", "Make a Word document of the user's meetings for a week from emails you already read. Pass "
      "messages=[{id, thread_id, subject, from, date, body, ics}] (ics = the invitation's iCalendar text when the "
      "email has one) and/or meetings=[{title, start, end, location, link, organizer, attendees, agenda, prep, "
      "source}] (ISO times). Follows invitation updates and cancellations, flags conflicts, never invents times, "
      "never overwrites a file, then reopens the document and counts the meetings. week: 'this', 'next', 'last' or "
      "a date in the week. week_only=true only returns the week's date range.", "files_write",
      {"messages": {"type": "array", "items": {"type": "object"}},
       "meetings": {"type": "array", "items": {"type": "object"}},
       "week": {"type": "string"}, "week_start": {"type": "string", "enum": ["monday", "sunday"]},
       "timezone": {"type": "string", "description": "IANA zone, e.g. America/New_York (default: this computer's)"},
       "path": {"type": "string", "description": "Where to save the .docx (default: the working folder)"},
       "title": {"type": "string"}, "week_only": {"type": "boolean"}},
      summary=lambda a: "week range" if a.get("week_only") else (a.get("path") or f"{len(a.get('messages') or [])} emails"))
def meeting_doc(ctx, messages=None, meetings_=None, week="this", week_start="monday", timezone="", path="",
                title="", week_only=False, **kw):
    recs = kw.get("meetings", meetings_)
    try:
        tz = meetings.local_zone(timezone or None)
    except Exception:
        return {"text": f"Unknown time zone '{timezone}'. Use an IANA name like America/New_York.", "error": True}
    tz_name = timezone or getattr(tz, "key", None) or str(tz)
    try:
        start, end = meetings.week_range(week or "this", tz, week_start or "monday")
    except ValueError:
        return {"text": f"'{week}' is not a week: use this, next, last or a date like 2026-10-12.", "error": True}
    rng = meetings.range_text(start, end, tz_name)
    if week_only:
        return f"Week: {rng} (from {start.isoformat()} up to {end.isoformat()})."
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    data = meetings.collect(msgs, [m for m in (recs or []) if isinstance(m, dict)], start, end, tz)
    if not data["active"] and not data["cancelled"] and not data["mentions"]:
        return {"text": f"No meetings found for {rng} in the {len(msgs)} email(s) given. No document was made.",
                "error": False}
    target = ctx.path(path or f"Meetings {start.strftime('%Y-%m-%d')}.docx")
    if target.suffix.lower() != ".docx":
        target = target.with_suffix(".docx")
    target = meetings.free_path(target)
    meetings.write_docx(target, data, start, end, tz_name, title or None, len(msgs))
    excluded = [m.get("subject") for m in msgs if m.get("id") in set(data["skipped"])]
    ok, det = meetings.verify_docx(target, data, excluded)
    lines = [f"Saved {target}", f"Week: {rng}",
             f"Meetings: {len(data['active'])} active, {len(data['cancelled'])} cancelled, "
             f"{len(data['conflicts'])} conflicts, {len(data['mentions'])} mentioned without calendar details",
             f"Emails not about meetings (left out): {len(data['skipped'])}"]
    for e in data["active"]:
        lines.append(f"- {meetings._when(e, start.tzinfo)}: {e['title']}")
    env = action_results.make("file", verified=ok, method=f"reopened the .docx: {det['meetings_in_document']} of "
                              f"{det['expected']} meetings found" + (", unrelated email text found!"
                                                                    if det["unrelated_found"] else ""),
                              changed={"path": str(target)})
    return action_results.attach({"text": "\n".join(lines)}, env)
