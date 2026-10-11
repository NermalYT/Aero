"""Tests for the email-to-meeting-document path (meetings.py, tools/doc_tools.py) with sample emails only: week ranges
in the user's time zone (Sundays and zone boundaries included), invitation updates and cancellations, no invented
times, conflicts, a real .docx that is reopened and counted, and never overwriting a file. Run from source/:
    python -m unittest discover -s tests -v
"""
import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aero import config, meetings, tools  # noqa: E402

tools.load_all()
NY = ZoneInfo("America/New_York")


def ics(uid, start, end, title, seq=0, method="REQUEST", status="CONFIRMED", tzid="America/New_York", extra=""):
    return (f"BEGIN:VCALENDAR\r\nMETHOD:{method}\r\nBEGIN:VEVENT\r\nUID:{uid}\r\nSEQUENCE:{seq}\r\n"
            f"DTSTART;TZID={tzid}:{start}\r\nDTEND;TZID={tzid}:{end}\r\nSUMMARY:{title}\r\nSTATUS:{status}\r\n"
            f"ORGANIZER;CN=Pat Lee:mailto:pat@example.com\r\nATTENDEE;CN=Sam:mailto:sam@example.com\r\n"
            f"LOCATION:Room 4\r\nDESCRIPTION:Agenda: budget\\nJoin https://meet.google.com/abc-defg-hij\r\n{extra}"
            f"END:VEVENT\r\nEND:VCALENDAR\r\n")


class Week(unittest.TestCase):
    def test_monday_week_and_sunday(self):
        now = dt.datetime(2026, 10, 11, 22, 30, tzinfo=NY)          # a Sunday evening
        s, e = meetings.week_range("this", NY, "monday", now)
        self.assertEqual((s.date(), e.date()), (dt.date(2026, 10, 5), dt.date(2026, 10, 12)))
        s, e = meetings.week_range("this", NY, "sunday", now)
        self.assertEqual((s.date(), e.date()), (dt.date(2026, 10, 11), dt.date(2026, 10, 18)))
        s, _ = meetings.week_range("next", NY, "monday", now)
        self.assertEqual(s.date(), dt.date(2026, 10, 12))
        self.assertIn("(America/New_York)", meetings.range_text(s, s + dt.timedelta(days=7), "America/New_York"))

    def test_zone_boundary(self):
        # 03:30 UTC on Monday is still Sunday evening in New York: that is last week there
        now = dt.datetime(2026, 10, 12, 3, 30, tzinfo=dt.timezone.utc)
        s, _ = meetings.week_range("this", NY, "monday", now)
        self.assertEqual(s.date(), dt.date(2026, 10, 5))
        s, _ = meetings.week_range("this", ZoneInfo("Europe/Berlin"), "monday", now)
        self.assertEqual(s.date(), dt.date(2026, 10, 12))


class Collect(unittest.TestCase):
    def setUp(self):
        self.start, self.end = meetings.week_range("2026-10-14", NY, "monday")

    def test_updates_cancellations_and_conflicts(self):
        msgs = [
            {"id": "m1", "subject": "Invitation: Budget review", "from": "pat@example.com", "date": "Oct 8",
             "ics": ics("u1", "20261013T140000", "20261013T150000", "Budget review")},
            {"id": "m2", "subject": "Updated invitation: Budget review", "from": "pat@example.com", "date": "Oct 9",
             "ics": ics("u1", "20261013T160000", "20261013T170000", "Budget review", seq=2)},
            {"id": "m3", "subject": "Canceled: Team lunch", "from": "sam@example.com",
             "ics": ics("u2", "20261014T120000", "20261014T130000", "Team lunch", method="CANCEL", status="CANCELLED")},
            {"id": "m4", "subject": "Invitation: Vendor call", "from": "x@vendor.example",
             "ics": ics("u3", "20261013T163000", "20261013T173000", "Vendor call")},
            {"id": "m5", "subject": "Invitation: Next month", "ics": ics("u4", "20261120T090000", "20261120T100000", "Later")},
            {"id": "m6", "subject": "Quick sync?", "from": "alex@example.com",
             "body": "Hi! Could we have a quick call sometime about the launch? Thanks."},
            {"id": "m7", "subject": "Your receipt", "body": "Thanks for your order of 3 lamps."},
        ]
        d = meetings.collect(msgs, start=self.start, end=self.end, tz=NY)
        self.assertEqual([e["title"] for e in d["active"]], ["Budget review", "Vendor call"])
        budget = d["active"][0]
        self.assertEqual(budget["start"].astimezone(NY).hour, 16)          # the update wins
        self.assertTrue(budget["history"])
        self.assertEqual([e["title"] for e in d["cancelled"]], ["Team lunch"])
        self.assertEqual(len(d["conflicts"]), 1)                           # 16:00-17:00 overlaps 16:30
        self.assertEqual(d["mentions"][0]["source"]["subject"], "Quick sync?")
        self.assertNotIn("start", d["mentions"][0])                        # no made-up time
        self.assertEqual(d["skipped"], ["m7"])
        self.assertEqual(budget["links"], ["https://meet.google.com/abc-defg-hij"])

    def test_utc_and_agent_records(self):
        msgs = [{"id": "z", "subject": "Invitation: Standup",
                 "ics": "BEGIN:VEVENT\r\nUID:z1\r\nDTSTART:20261015T130000Z\r\nDTEND:20261015T131500Z\r\n"
                        "SUMMARY:Standup\r\nEND:VEVENT\r\n"}]
        recs = [{"title": "Standup", "start": "2026-10-15T09:00:00-04:00"},             # the same meeting
                {"title": "Dentist", "start": "2026-10-16T08:00:00", "location": "Main St"},
                {"title": "Broken", "start": "next tuesday-ish"}]
        d = meetings.collect(msgs, recs, self.start, self.end, NY)
        self.assertEqual([e["title"] for e in d["active"]], ["Standup", "Dentist"])
        self.assertEqual(d["active"][0]["start"].astimezone(NY).hour, 9)
        self.assertIn("Broken", d["skipped"])


class Document(unittest.TestCase):
    def test_tool_writes_checks_and_never_overwrites(self):
        folder = tempfile.mkdtemp(prefix="aero-docs-")
        s = config.load_settings()
        s["work_dir"] = folder
        ctx = tools.Ctx(s, chat_id="docs")
        msgs = [{"id": "m1", "subject": "Invitation: Budget review", "from": "pat@example.com",
                 "ics": ics("u1", "20261013T140000", "20261013T150000", "Budget review")},
                {"id": "m2", "subject": "Your receipt", "body": "Order 1234 shipped."}]
        args = {"messages": msgs, "week": "2026-10-14", "timezone": "America/New_York", "path": "Meetings.docx"}
        r1 = tools.run("meeting_doc", args, ctx)
        self.assertFalse(r1["error"], r1["text"])
        self.assertIs(r1["envelope"]["verified"], True)
        self.assertTrue((Path(folder) / "Meetings.docx").exists())
        r2 = tools.run("meeting_doc", args, ctx)
        self.assertIn("Meetings (2).docx", r2["text"])                     # a second run never overwrites
        from docx import Document as D
        text = "\n".join(p.text for p in D(str(Path(folder) / "Meetings.docx")).paragraphs)
        self.assertIn("Budget review", text)
        self.assertNotIn("Order 1234", text)                               # unrelated mail stays out
        self.assertIn("Mon Oct 12", text)
        week = tools.run("meeting_doc", {"week_only": True, "week": "2026-10-14", "timezone": "America/New_York"}, ctx)
        self.assertIn("Mon Oct 12 – Sun Oct 18, 2026 (America/New_York)", week["text"])

    def test_nothing_found_makes_no_file(self):
        folder = tempfile.mkdtemp(prefix="aero-docs-")
        s = config.load_settings()
        s["work_dir"] = folder
        r = tools.run("meeting_doc", {"messages": [{"id": "x", "subject": "Hello", "body": "Just saying hi."}],
                                      "week": "2026-10-14"}, tools.Ctx(s))
        self.assertIn("No meetings found", r["text"])
        self.assertEqual(list(Path(folder).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
