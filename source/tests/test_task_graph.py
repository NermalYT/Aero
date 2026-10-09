"""Tests for task graphs, questions that don't block other work, resource locks and the operation ledger
(task_graph.py, clarifications.py, resources.py). Run from source/:
    python -m unittest discover -s tests -v
"""
import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aero import clarifications, resources, task_graph  # noqa: E402
from aero.task_graph import TaskGraph, WaitingForUser  # noqa: E402


class Graph(unittest.TestCase):
    def test_dependencies_concurrency_and_order(self):
        g = TaskGraph("t", "chat-g1")
        a = g.add("a")
        b = g.add("b")
        c = g.add("c", deps=[a.id, b.id])
        running, peak, order = set(), [0], []

        async def work(n):
            running.add(n.id)
            peak[0] = max(peak[0], len(running))
            await asyncio.sleep(0.05)
            running.discard(n.id)
            order.append(n.intent)
            return n.intent.upper()
        summ = asyncio.run(g.run(work, concurrency=2))
        self.assertEqual(peak[0], 2)                         # a and b side by side
        self.assertEqual(order[-1], "c")                     # c only after both
        self.assertEqual(summ["not_completed"], [])
        self.assertEqual(g.nodes[c.id].result, "C")
        self.assertEqual(g.status, "done")

    def test_failure_blocks_dependents_not_siblings(self):
        g = TaskGraph()
        a = g.add("a")
        b = g.add("b", deps=[a.id])
        s = g.add("sibling")

        async def work(n):
            if n.intent == "a":
                raise RuntimeError("boom")
            return "ok"
        asyncio.run(g.run(work))
        self.assertEqual((g.nodes[a.id].state, g.nodes[b.id].state, g.nodes[s.id].state),
                         ("failed", "blocked", "completed"))
        self.assertEqual(g.status, "partial")

    def test_cycle_is_rejected(self):
        g = TaskGraph()
        a = g.add("a")
        b = g.add("b", deps=[a.id])
        g.nodes[a.id].deps.append(b.id)
        with self.assertRaises(ValueError):
            g.check_acyclic()

    def test_cancel_stops_everything(self):
        g = TaskGraph()
        for i in range(4):
            g.add(f"n{i}")
        ev = asyncio.Event()

        async def work(n):
            ev.set()
            await asyncio.sleep(5)
        start = time.time()

        async def go():
            t = asyncio.create_task(g.run(work, concurrency=4, cancel=ev))
            return await t
        summ = asyncio.run(go())
        self.assertLess(time.time() - start, 3)
        self.assertEqual(summ["completed"], [])
        self.assertTrue(all(n.state == "cancelled" for n in g.nodes.values()))

    def test_saved_and_restored_without_pretending_running_work_finished(self):
        g = TaskGraph("save me", "chat-save")
        a = g.add("done step")
        b = g.add("running step")
        g.set(a.id, "running")
        g.set(a.id, "completed", result="fine")
        g.set(b.id, "running")
        g.save(force=True)
        g2 = TaskGraph.load(g.id)
        self.assertEqual(g2.nodes[a.id].state, "completed")
        self.assertEqual(g2.nodes[b.id].state, "failed")          # unknown after a restart, never "done"
        self.assertIn("restarted", g2.nodes[b.id].error)
        self.assertEqual(task_graph.latest_for_chat("chat-save")[0]["id"], g.id)


class Questions(unittest.TestCase):
    def setUp(self):
        clarifications.reset_cache()

    def test_ask_dedupes_and_validates(self):
        q, new = clarifications.ask("chat-q1", "Which Gmail account should I check?", ["me@home.example", "me@work.example"],
                                    allow_free=False)
        self.assertTrue(new)
        q2, new2 = clarifications.ask("chat-q1", "which gmail account should I check", ["x"])
        self.assertEqual((q2["id"], new2), (q["id"], False))
        with self.assertRaises(ValueError):
            clarifications.answer(q["id"], "someone@else.example")
        self.assertEqual(clarifications.answer(q["id"], "ME@WORK.EXAMPLE")["answer"], "me@work.example")
        yn, _ = clarifications.ask("chat-q1", "Launch it now?", kind="yes_no", allow_free=False)
        self.assertEqual(clarifications.answer(yn["id"], "Y")["answer"], "yes")

    def test_persisted_across_restart(self):
        q, _ = clarifications.ask("chat-q2", "Which file?")
        clarifications.reset_cache()
        self.assertEqual([x["id"] for x in clarifications.pending("chat-q2")], [q["id"]])

    def test_wait_wakes_on_answer_and_honours_stop(self):
        q, _ = clarifications.ask("chat-q3", "Which folder?")

        async def go():
            t = asyncio.create_task(clarifications.wait(q["id"], None, timeout=5))
            await asyncio.sleep(0.2)
            threading.Thread(target=lambda: clarifications.answer(q["id"], "Documents")).start()
            return await t
        self.assertEqual(asyncio.run(go()), "Documents")
        q2, _ = clarifications.ask("chat-q3", "Another?")

        async def stopped():
            ev = asyncio.Event()
            t = asyncio.create_task(clarifications.wait(q2["id"], ev, timeout=5))
            await asyncio.sleep(0.2)
            ev.set()
            return await t
        self.assertIsNone(asyncio.run(stopped()))
        self.assertEqual(clarifications.get(q2["id"])["status"], "pending")   # still answerable later

    def test_expiry(self):
        q, _ = clarifications.ask("chat-q4", "Quick one?", timeout_s=1)
        time.sleep(1.1)
        self.assertEqual(clarifications.pending("chat-q4"), [])
        self.assertEqual(clarifications.get(q["id"])["status"], "expired")


class GmailScenario(unittest.TestCase):
    """The account question blocks only the branch that reads mail; everything else runs, and the answer resumes
    the same graph without starting over (mocked mailbox, nothing real is read)."""

    def test_independent_work_continues_while_waiting(self):
        clarifications.reset_cache()
        g = TaskGraph("meetings this week", "chat-gmail")
        acct = g.add("resolve Gmail account")
        week = g.add("work out this week's dates")
        tmpl = g.add("prepare document headings")
        read = g.add("read meeting emails", deps=[acct.id, week.id])
        doc = g.add("write and check the document", deps=[read.id, tmpl.id])
        log, asked = [], {}

        async def work(n):
            if n is g.nodes[acct.id] or n.id == acct.id:
                if "answer" not in n.outputs:
                    q, _ = clarifications.ask("chat-gmail", "Which Gmail account should I check?",
                                              ["me@home.example", "me@work.example"])
                    asked["q"] = q["id"]
                    raise WaitingForUser(q["id"])
                log.append(("account", n.outputs["answer"]))
                return n.outputs["answer"]
            if n.id == read.id:
                assert g.nodes[acct.id].state == "completed", "mail was read before the account was chosen"
            log.append((n.intent, time.monotonic()))
            await asyncio.sleep(0.02)
            return "ok"

        async def answers(qid):
            return await clarifications.wait(qid, None, timeout=5)

        async def go():
            t = asyncio.create_task(g.run(work, concurrency=3, answers=answers))
            await asyncio.sleep(0.3)
            # while the question is open, the independent branches already finished and mail was not touched
            self.assertEqual(g.nodes[acct.id].state, "waiting_for_user")
            self.assertEqual((g.nodes[week.id].state, g.nodes[tmpl.id].state), ("completed", "completed"))
            self.assertEqual(g.nodes[read.id].state, "pending")
            clarifications.answer(asked["q"], "me@work.example")
            return await t
        summ = asyncio.run(go())
        self.assertEqual(summ["not_completed"], [])
        self.assertIn(("account", "me@work.example"), log)
        self.assertEqual(g.nodes[doc.id].state, "completed")
        self.assertEqual(g.nodes[acct.id].attempts, 2)     # resumed, not restarted from the prompt


class Locks(unittest.TestCase):
    def tearDown(self):
        for o in ("a1", "a2", "a3"):
            resources.release_owner(o)

    def test_all_or_nothing_and_reentry(self):
        self.assertTrue(resources.acquire(["window:1", "clipboard"], "a1"))
        self.assertTrue(resources.acquire(["window:1"], "a1"))             # same owner again
        self.assertFalse(resources.acquire(["window:2", "clipboard"], "a2"))
        self.assertIsNone(resources.holder("window:2"))                     # nothing half-taken
        resources.release(["window:1"], "a1")
        self.assertEqual(resources.holder("window:1")["owner"], "a1")       # still held once
        self.assertEqual(sorted(resources.release_owner("a1")), ["clipboard", "window:1"])
        self.assertTrue(resources.acquire(["window:2", "clipboard"], "a2"))

    def test_three_agents_one_window_are_serialized(self):
        inside, peak, done = [0], [0], []
        lock = threading.Lock()

        def agent(name):
            if resources.acquire(["window:7"], name, timeout=5):
                with lock:
                    inside[0] += 1
                    peak[0] = max(peak[0], inside[0])
                time.sleep(0.05)
                with lock:
                    inside[0] -= 1
                done.append(name)
                resources.release_owner(name)
        ts = [threading.Thread(target=agent, args=(n,)) for n in ("a1", "a2", "a3")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(peak[0], 1)
        self.assertEqual(sorted(done), ["a1", "a2", "a3"])

    def test_timeout_and_cancel(self):
        resources.acquire([resources.EXCLUSIVE_INPUT], "a1")
        t0 = time.monotonic()
        self.assertFalse(resources.acquire([resources.EXCLUSIVE_INPUT], "a2", timeout=0.3))
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)
        ev = threading.Event()
        ev.set()
        self.assertFalse(resources.acquire([resources.EXCLUSIVE_INPUT], "a2", timeout=5, cancel=ev))


class Ledger(unittest.TestCase):
    def test_unknown_outcome_is_remembered(self):
        k = task_graph.op_key("chat-l", "mcp_gmail_send", {"to": "x@example.com"})
        self.assertIsNone(task_graph.begin(k, "send"))
        task_graph.end(k, "unknown", "timed out")
        self.assertEqual(task_graph.peek(k)["status"], "unknown")
        self.assertEqual(task_graph.begin(k)["status"], "unknown")
        task_graph.end(k, "failed")                                      # certainly didn't happen: may retry
        self.assertIsNone(task_graph.peek(k))


if __name__ == "__main__":
    unittest.main()
