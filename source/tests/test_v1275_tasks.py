"""1.2.75: work interrupted by closing Aero can be resumed, and tasks can run on a schedule.
Run from source/:
    python -m unittest discover -s tests -v
"""
import datetime as dt
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aero import jobs  # noqa: E402


def at(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi).timestamp()


class Store(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self.chats = Path(tempfile.mkdtemp())
        p = [mock.patch.object(jobs, "read_store", lambda n, default: self.store.get(n, default)),
             mock.patch.object(jobs, "write_store", lambda n, o: self.store.__setitem__(n, o)),
             mock.patch.object(jobs, "CHATS", self.chats)]
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        for c in ("chat-a", "chat-b", "c", "loop-chat"):
            (self.chats / f"{c}.json").write_text("{}", encoding="utf-8")
        jobs._runner.update(running=None, chat=None)


class Interrupted(Store):
    def test_a_turn_still_running_at_startup_was_interrupted(self):
        a = jobs.track_start("chat-a", "Photo renamer", "rename my photos by date")
        b = jobs.track_start("chat-b", "Notes", "summarise notes")
        jobs.track_end(b, "done")
        self.assertEqual(jobs.mark_interrupted(), 1)
        got = jobs.interrupted()
        self.assertEqual([t["id"] for t in got], [a])
        self.assertEqual(got[0]["chat_id"], "chat-a")

    def test_resume_says_what_finished_and_what_did_not(self):
        rid = jobs.track_start("chat-a", "t", "do things")
        jobs.mark_interrupted()
        graph = {"nodes": [{"label": "Read the folder", "state": "completed"},
                           {"label": "Rename 40 files", "state": "running"}]}
        with mock.patch("aero.task_graph.latest_for_chat", lambda cid, limit=1: [graph]):
            rec = jobs.take_for_resume(rid)
            text = jobs.resume_text(rec)
        self.assertIn("Aero was closed", text)
        self.assertIn("Already done: Read the folder", text)
        self.assertIn("Not finished: Rename 40 files", text)
        self.assertIsNone(jobs.take_for_resume(rid))                     # only once
        self.assertEqual(jobs.interrupted(), [])

    def test_dismiss(self):
        rid = jobs.track_start("c", "t", "x")
        jobs.mark_interrupted()
        self.assertTrue(jobs.dismiss(rid))
        self.assertEqual(jobs.interrupted(), [])

    def test_scheduled_runs_and_deleted_chats_are_not_offered(self):
        jobs.track_start("sched-1", "Mail", "check mail", job="abc")
        gone = jobs.track_start("deleted-chat", "t", "x")
        jobs.mark_interrupted()
        self.assertEqual(jobs.interrupted(), [])
        self.assertIsNone(jobs.take_for_resume(gone))

    def test_a_forever_loop_between_rounds_is_interrupted_unless_it_was_stopped(self):
        loop = {"iteration": 3}
        r1 = jobs.track_start("loop-chat", "Loop", "x" * 1000, loop=loop)
        jobs.track_end(r1, "done")
        self.assertEqual(self.store[jobs.STORE]["turns"][r1]["status"], "loop_wait")
        r2 = jobs.track_start("loop-chat", "Loop", "x" * 1000, loop={"iteration": 4})
        self.assertEqual(self.store[jobs.STORE]["turns"][r1]["status"], "done")     # the next round took over
        jobs.track_end(r2, "done")
        jobs.mark_interrupted()
        got = jobs.interrupted()
        self.assertEqual([t["id"] for t in got], [r2])
        self.assertEqual(len(got[0]["task"]), 1000)                                 # a loop keeps its whole prompt
        r3 = jobs.track_start("loop-chat", "Loop", "y", loop=loop)
        jobs.track_end(r3, "done")
        jobs.loop_stopped()
        self.assertEqual(self.store[jobs.STORE]["turns"][r3]["status"], "done")

    def test_tracking_never_breaks_a_turn(self):
        with mock.patch.object(jobs, "write_store", side_effect=PermissionError("read-only")):
            rid = jobs.track_start("c", "t", "x")
            jobs.track_end(rid, "done")


class Schedules(Store):
    def test_next_run(self):
        mon_7 = at(2026, 10, 12, 7, 0)                                     # a Monday
        self.assertEqual(jobs.next_run({"type": "daily", "time": "08:00"}, mon_7), at(2026, 10, 12, 8, 0))
        self.assertEqual(jobs.next_run({"type": "daily", "time": "06:30"}, mon_7), at(2026, 10, 13, 6, 30))
        fri_9 = at(2026, 10, 16, 9, 0)
        self.assertEqual(jobs.next_run({"type": "weekdays", "time": "08:00"}, fri_9), at(2026, 10, 19, 8, 0))
        self.assertEqual(jobs.next_run({"type": "weekly", "day": 4, "time": "17:00"}, mon_7), at(2026, 10, 16, 17, 0))
        self.assertEqual(jobs.next_run({"type": "every", "minutes": 30}, mon_7), mon_7 + 1800)
        self.assertIsNone(jobs.next_run({"type": "once", "at": mon_7 - 60}, mon_7))

    def test_bad_schedules_are_refused(self):
        for bad in ({"type": "every", "minutes": 1}, {"type": "daily", "time": "25:00"}, {"type": "weekly", "day": 9,
                    "time": "08:00"}, {"type": "hourly"}, "daily"):
            with self.assertRaises((ValueError, KeyError, TypeError)):
                jobs.clean_schedule(bad)
        with self.assertRaises(ValueError):
            jobs.add("", {"type": "daily", "time": "08:00"})
        with self.assertRaises(ValueError):
            jobs.clean_schedule({"type": "once", "at": time.time() - 60})            # already passed
        self.assertEqual(jobs.clean_schedule({"type": "daily", "time": "8:5"})["time"], "08:05")

    def test_updates_are_cleaned_and_a_broken_task_does_not_break_the_list(self):
        t = jobs.add("Check my email", {"type": "daily", "time": "08:00"}, "Mail")
        jobs.update(t["id"], name="  " + "n" * 200, same_chat=1)
        s = jobs.scheduled()[0]
        self.assertEqual((len(s["name"]), s["same_chat"]), (80, True))
        with self.assertRaises(ValueError):
            jobs.update(t["id"], prompt="   ")
        self.store[jobs.STORE]["scheduled"][t["id"]]["schedule"] = {"type": "weekly"}
        self.assertIn("invalid", jobs.scheduled()[0]["when"])

    def test_add_due_and_run_bookkeeping(self):
        t = jobs.add("Check my email", {"type": "every", "minutes": 30}, "Mail")
        self.assertEqual(jobs.due(), [])
        self.assertEqual(len(jobs.due(time.time() + 3600)), 1)
        jobs._finish_run(t["id"], "done", "sched-1", time.time())
        s = jobs.scheduled()[0]
        self.assertEqual((s["runs"], s["last_status"]), (1, "done"))
        self.assertGreater(s["next_run"], time.time() + 1700)
        self.assertEqual(s["when"], "every 30 min")

    def test_a_one_off_switches_itself_off(self):
        t = jobs.add("Remind me", {"type": "once", "at": time.time() + 60})
        jobs._finish_run(t["id"], "done", "c", time.time())
        self.assertFalse(jobs.scheduled()[0]["enabled"])

    def test_a_run_that_fails_is_not_repeated_at_once(self):
        t = jobs.add("Send the report", {"type": "every", "minutes": 30})
        self.store[jobs.STORE]["scheduled"][t["id"]]["next_run"] = 1
        hooks = {"state": {"status": "ready"}, "settings": lambda: {}, "loop": None}
        calls = []

        def boom(t, h):
            calls.append(t)
            raise RuntimeError("disk full")
        with mock.patch("aero.agents.busy", lambda: []), mock.patch.object(jobs, "run_task", boom), \
                mock.patch.object(jobs._runner["stop"], "wait", side_effect=[False, False, True]):
            jobs._loop(hooks)
        self.assertEqual(len(calls), 1)                          # claimed: its next time moved on before it ran
        self.assertGreater(jobs.scheduled()[0]["next_run"], time.time() + 1700)
        self.assertIsNone(jobs._runner["running"])
        self.assertEqual(jobs.mark_interrupted(), 1)             # it never finished: "interrupted", not "running"
        self.assertEqual(jobs.scheduled()[0]["last_status"], "interrupted")

    def test_run_now_waits_for_the_users_chat_and_runs_one_at_a_time(self):
        t = jobs.add("x", {"type": "every", "minutes": 5})
        jobs._runner["hooks"] = {"state": {}, "settings": lambda: {}, "loop": None}
        self.addCleanup(jobs._runner.update, hooks=None, running=None)
        with mock.patch("aero.agents.busy", lambda: [{"status": "working"}]):
            self.assertIn("working on a chat", jobs.run_now(t["id"]))
        jobs._runner["running"] = "other"
        with mock.patch("aero.agents.busy", lambda: []):
            self.assertIn("Another", jobs.run_now(t["id"]))
            self.assertEqual(jobs.run_now("nope"), "No such task.")

    def test_a_same_chat_run_skips_a_chat_the_user_is_working_in(self):
        t = jobs.add("x", {"type": "daily", "time": "08:00"}, same_chat=True)
        t["chat_id"] = "c"
        with mock.patch.dict("aero.agent._cancel", {"c": object()}):
            self.assertEqual(jobs.run_task(t, {}), ("c", "skipped"))
        s = jobs.scheduled()[0]
        self.assertEqual((s["runs"], s["last_status"]), (0, "waiting"))
        self.assertLess(s["next_run"], time.time() + jobs.RETRY_S + 5)

    def test_a_run_waits_for_the_users_own_chat(self):
        jobs.add("x", {"type": "every", "minutes": 5})
        self.store[jobs.STORE]["scheduled"][next(iter(self.store[jobs.STORE]["scheduled"]))]["next_run"] = 1
        hooks = {"state": {"status": "ready"}, "settings": lambda: {}, "loop": None}
        ran = []
        with mock.patch("aero.agents.busy", lambda: [{"status": "working"}]), \
                mock.patch.object(jobs, "run_task", lambda t, h: ran.append(t)), \
                mock.patch.object(jobs._runner["stop"], "wait", side_effect=[False, True]):
            jobs._loop(hooks)
        self.assertEqual(ran, [])
        with mock.patch("aero.agents.busy", lambda: []), \
                mock.patch.object(jobs, "run_task", lambda t, h: ran.append(t)), \
                mock.patch.object(jobs._runner["stop"], "wait", side_effect=[False, True]):
            jobs._loop(hooks)
        self.assertEqual(len(ran), 1)

    def test_unattended_runs_never_ask_with_a_card_or_use_the_real_input(self):
        captured = {}

        async def fake_turn(chat_id, history, settings, state, title="", opts=None):
            captured.update(settings=settings, opts=opts)
            opts["_sink"](type("T", (), {"history": history})())
            yield 'data: {"t": "done"}\n\n'
        import asyncio
        loop = asyncio.new_event_loop()
        th = __import__("threading").Thread(target=loop.run_forever, daemon=True)
        th.start()
        d = Path(tempfile.mkdtemp())
        try:
            with mock.patch("aero.pipeline.run_turn", fake_turn), mock.patch.object(jobs, "CHATS", d):
                t = jobs.add("Check my GitHub", {"type": "daily", "time": "08:00"}, "GitHub")
                cid, status = jobs.run_task(t, {"state": {}, "settings": lambda: {"loop_approval_mode": "ask"},
                                                "loop": loop})
        finally:
            loop.call_soon_threadsafe(loop.stop)
        self.assertEqual(status, "done")
        self.assertEqual(captured["settings"]["approval_mode"], "batch")
        self.assertTrue(captured["settings"]["strict_background"])
        saved = json.loads((d / f"{cid}.json").read_text(encoding="utf-8"))
        self.assertEqual((saved["job"], saved["messages"][0]["content"]), (t["id"], "Check my GitHub"))
        self.assertTrue(saved["sched_at"])

    def test_actions_left_waiting_get_the_queue_card(self):
        from aero import autonomy
        it, _ = autonomy.enqueue("sched-q", "call1", "send_email", {"to": "me"}, "send mail", "integrations", False)
        self.addCleanup(autonomy._items.pop, it["id"], None)
        jobs._save_chat("sched-q", "Mail", [{"role": "user", "content": "x"}], "j1")
        saved = json.loads((self.chats / "sched-q.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["messages"][-1]["role"], "queue")
        self.assertEqual(saved["messages"][-1]["items"][0]["label"], "send mail")


class ChatMerge(unittest.TestCase):
    def test_a_stale_ui_copy_does_not_overwrite_a_scheduled_run(self):
        from fastapi.testclient import TestClient

        from aero import server
        c = TestClient(server.app)
        cid = "sched-merge"
        server._chat_path(cid).write_text(json.dumps({"id": cid, "job": "j", "sched_at": 200.0, "messages": [
            {"role": "user", "content": "run 1", "ts": 100}, {"role": "user", "content": "run 2", "ts": 190},
            {"role": "assistant", "content": "done 2", "ts": 195}]}), encoding="utf-8")
        stale = {"id": cid, "job": "j", "sched_at": 120.0, "messages": [
            {"role": "user", "content": "run 1", "ts": 100}, {"role": "user", "content": "mine", "ts": 300}]}
        self.assertEqual(c.put(f"/api/chats/{cid}", json=stale).status_code, 200)
        got = c.get(f"/api/chats/{cid}").json()
        self.assertEqual([m["content"] for m in got["messages"]], ["run 1", "run 2", "done 2", "mine"])
        server._chat_path(cid).unlink()


class Tool(unittest.TestCase):
    def test_the_model_can_schedule_list_and_cancel(self):
        store = {}
        with mock.patch.object(jobs, "read_store", lambda n, default: store.get(n, default)), \
                mock.patch.object(jobs, "write_store", lambda n, o: store.__setitem__(n, o)):
            from aero.tools import schedule_tools as st
            out = st.schedule_task(None, "create", prompt="Check my email", when="weekdays", time="08:00",
                                   name="Mail")
            self.assertIn("Monday to Friday at 08:00", out)
            sid = next(iter(store[jobs.STORE]["scheduled"]))
            self.assertIn(sid, st.schedule_task(None, "list"))
            self.assertTrue(st.schedule_task(None, "create", prompt="x", when="once", time="2020-01-01 08:00")["error"])
            label = st._summary({"action": "create", "prompt": "Check my email", "when": "daily", "time": "08:00"})
            self.assertIn("daily 08:00: Check my email", label)
            self.assertIn("Cancelled", st.schedule_task(None, "cancel", id=sid))
            self.assertTrue(st.schedule_task(None, "create", prompt="x", when="every", minutes=1)["error"])



class Unattended(unittest.TestCase):
    """A scheduled run has nobody at the PC: no waiting for decisions, no questions, no sign-in cards, no new
    schedules; and a schedule created in a chat always asks first."""
    ARGS = {"action": "create", "prompt": "Check my email", "when": "daily", "time": "08:00"}

    def turn(self, settings=None):
        from aero import agent, config
        s = {**config.load_settings(), **(settings or {})}
        t = agent.Turn("u-" + str(time.time()), [], s, {"url": "x", "model": {}})
        self.addCleanup(t.close)
        return t

    def events(self, gen, stop_at=None):
        import asyncio

        async def go():
            out = []
            async for ev in gen:
                out.append(ev)
                if stop_at and ev.get("t") == stop_at:
                    await gen.aclose()
                    break
            return out
        return asyncio.run(go())

    def test_a_scheduled_run_cannot_create_schedules(self):
        from aero import agent
        t = self.turn()
        t.unattended, t.approval_mode = True, "batch"
        out = {}
        with mock.patch.object(jobs, "add") as add:
            self.events(agent.exec_tool(t, "c1", "schedule_task", dict(self.ARGS), out=out))
        add.assert_not_called()
        self.assertTrue(out["res"]["error"])
        self.assertIn("can't set up more scheduled tasks", out["res"]["text"])

    def test_creating_a_schedule_asks_even_when_setup_is_auto(self):
        from aero import agent
        t = self.turn({"tool_policy": {"setup": "auto"}})
        ev = self.events(agent.exec_tool(t, "c2", "schedule_task", dict(self.ARGS), out={}), stop_at="tool_start")
        start = next(e for e in ev if e["t"] == "tool_start")
        self.assertTrue(start["needs_approval"])
        self.assertIn("Check my email", start["label"])
        agent._approvals.pop("c2", None)

    def test_no_waiting_for_a_decision_or_an_answer(self):
        from aero import agent
        t = self.turn()
        t.unattended = True
        with mock.patch.object(agent.autonomy, "pending", lambda cid: [{"id": "x"}]), \
                mock.patch.object(agent.autonomy, "wait") as wait:
            self.events(agent._batch_end(t, "local", {}))
        wait.assert_not_called()
        ev = self.events(agent.ask_user(t, "q1", {"question": "Which folder?"}))
        res = next(e for e in ev if e["t"] == "tool_result")["message"]
        self.assertTrue(res["error"])
        self.assertIn("Nobody is watching", res["content"])


if __name__ == "__main__":
    unittest.main()
