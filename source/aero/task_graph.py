"""Tasks as dependency graphs: what has to happen, what is waiting on what, and what already happened.

Every agent turn keeps one graph (agent.py adds a node per tool call), so the UI can show progress, a reload can show
what a task was doing, and Stop can say what finished and what didn't. The same graph also runs work on its own
(TaskGraph.run): independent nodes run side by side (bounded), a node that needs an answer from the user waits
without holding up the others, and the graph resumes that node when the answer arrives.

Node states:
    pending -> ready -> running -> verifying -> completed
                         |-> waiting_for_user | waiting_for_auth | waiting_for_resource -> ready
                         |-> failed | cancelled | blocked (a dependency failed) | skipped

Graphs are saved to data/tasks/<graph id>.json with a schema version, holding only what is needed to show and resume
the task: intents, tool names, states, short results. No page text, email bodies or file contents.

The operation ledger (data/tasks/ops.json) remembers consequential actions (sends, posts, writes to other services)
by a key, so after a timeout the same action is not repeated blindly: begin() says whether it already completed or
ended in an unknown state that has to be checked first.
"""
import asyncio
import hashlib
import json
import os
import threading
import time
import uuid

from .config import DATA

DIR = DATA / "tasks"
SCHEMA = 1
STATES = ("pending", "ready", "running", "waiting_for_user", "waiting_for_auth", "waiting_for_resource", "verifying",
          "completed", "failed", "cancelled", "blocked", "skipped")
DONE = {"completed", "failed", "cancelled", "blocked", "skipped"}
WAITING = {"waiting_for_user", "waiting_for_auth", "waiting_for_resource"}
RESULT_MAX = 400
_lock = threading.RLock()


class WaitingForUser(Exception):
    """Raised by a node's work when it needs an answer first: question id in .qid."""

    def __init__(self, qid, text=""):
        super().__init__(text or qid)
        self.qid = qid


class WaitingForResource(Exception):
    def __init__(self, key, text=""):
        super().__init__(text or key)
        self.key = key


def _short(v):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= RESULT_MAX else s[:RESULT_MAX - 1] + "…"


class Node:
    __slots__ = ("id", "intent", "action", "capability", "target", "deps", "outputs", "state", "retry", "max_s",
                 "risk", "locks", "verify", "result", "error", "created", "started", "ended", "waiting_on", "attempts",
                 "lane", "label", "op_key")

    def __init__(self, intent, action="", deps=(), capability="", target=None, risk="read", locks=(), verify="",
                 retry="none", max_s=0, lane="local", label="", node_id=None, op_key=""):
        self.id = node_id or uuid.uuid4().hex[:8]
        self.intent, self.action, self.capability = str(intent)[:200], action, capability
        self.target, self.deps, self.outputs = target, list(deps), {}
        self.state, self.retry, self.max_s, self.risk = "pending", retry, max_s, risk
        self.locks, self.verify = list(locks), verify
        self.result = self.error = None
        self.created, self.started, self.ended = time.time(), None, None
        self.waiting_on, self.attempts, self.lane, self.label, self.op_key = None, 0, lane, label[:160], op_key

    def to_json(self):
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_json(cls, d):
        n = cls(d.get("intent", ""), d.get("action", ""), d.get("deps", ()), node_id=d.get("id"))
        for k in cls.__slots__:
            if k in d:
                setattr(n, k, d[k])
        return n


class TaskGraph:
    def __init__(self, title="", chat_id=None, graph_id=None):
        self.id = graph_id or uuid.uuid4().hex[:12]
        self.title, self.chat_id = str(title or "")[:200], chat_id
        self.nodes = {}
        self.order = []
        self.created = self.updated = time.time()
        self.status = "running"
        self.listeners = []                  # fn(event dict)
        self._save_at = 0.0

    # ---- building
    def add(self, intent, action="", deps=(), **kw):
        n = Node(intent, action, deps, **kw)
        for d in n.deps:
            if d not in self.nodes:
                raise KeyError(f"unknown dependency {d}")
        self.nodes[n.id] = n
        self.order.append(n.id)
        self._emit("add", n)
        return n

    def check_acyclic(self):
        seen, stack = set(), set()

        def visit(i):
            if i in stack:
                raise ValueError(f"dependency cycle through {i}")
            if i in seen:
                return
            stack.add(i)
            for d in self.nodes[i].deps:
                visit(d)
            stack.discard(i)
            seen.add(i)
        for i in self.order:
            visit(i)

    # ---- state
    def set(self, node_id, state, result=None, error=None, waiting_on=None):
        if state not in STATES:
            raise ValueError(state)
        n = self.nodes[node_id]
        n.state = state
        if state == "running" and n.started is None:
            n.started = time.time()
            n.attempts += 1
        if state in DONE:
            n.ended = time.time()
        if result is not None:
            n.result = _short(result)
        if error is not None:
            n.error = _short(error)
        n.waiting_on = waiting_on if state in WAITING else None
        self.updated = time.time()
        self._emit("state", n)
        return n

    def ready(self):
        """Nodes whose dependencies all completed; nodes behind a failed dependency become blocked."""
        out = []
        for i in self.order:
            n = self.nodes[i]
            if n.state not in ("pending", "ready"):
                continue
            deps = [self.nodes[d] for d in n.deps]
            if any(d.state in ("failed", "cancelled", "blocked") for d in deps):
                self.set(i, "blocked", error="a step it depends on did not complete")
                continue
            if all(d.state in ("completed", "skipped") for d in deps):
                if n.state != "ready":
                    self.set(i, "ready")
                out.append(n)
        return out

    def counts(self):
        c = {}
        for n in self.nodes.values():
            c[n.state] = c.get(n.state, 0) + 1
        return c

    def finished(self):
        return all(n.state in DONE for n in self.nodes.values())

    def summary(self):
        """{'completed': [...], 'not_completed': [...]} labels, for Stop and failures."""
        done = [n.label or n.intent for n in self._ordered() if n.state == "completed"]
        rest = [f"{n.label or n.intent} ({n.state.replace('_', ' ')})" for n in self._ordered()
                if n.state not in ("completed", "skipped")]
        return {"completed": done, "not_completed": rest}

    def _ordered(self):
        return [self.nodes[i] for i in self.order]

    def cancel_open(self, why="stopped"):
        for n in self._ordered():
            if n.state not in DONE:
                self.set(n.id, "cancelled", error=why)
        self.status = "stopped"

    # ---- events and storage
    def _emit(self, kind, node):
        for fn in list(self.listeners):
            try:
                fn({"kind": kind, "graph": self.id, "node": node.to_json()})
            except Exception:
                pass

    def to_json(self):
        return {"schema": SCHEMA, "id": self.id, "title": self.title, "chat_id": self.chat_id, "created": self.created,
                "updated": self.updated, "status": self.status, "order": self.order,
                "nodes": [self.nodes[i].to_json() for i in self.order]}

    @classmethod
    def from_json(cls, d):
        if d.get("schema") != SCHEMA:
            raise ValueError("unknown task graph format")
        g = cls(d.get("title", ""), d.get("chat_id"), d.get("id"))
        g.created, g.updated, g.status = d.get("created", time.time()), d.get("updated", time.time()), d.get("status")
        for nd in d.get("nodes", []):
            n = Node.from_json(nd)
            g.nodes[n.id] = n
            g.order.append(n.id)
        return g

    def save(self, force=False):
        now = time.time()
        if not force and now - self._save_at < 0.5:
            return
        self._save_at = now
        DIR.mkdir(parents=True, exist_ok=True)
        p = DIR / f"{self.id}.json"
        tmp = p.with_suffix(".json.tmp")
        with _lock:
            tmp.write_text(json.dumps(self.to_json(), ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)

    @classmethod
    def load(cls, graph_id):
        p = DIR / f"{graph_id}.json"
        g = cls.from_json(json.loads(p.read_text(encoding="utf-8")))
        # after a restart nothing is really running: running nodes are unknown, not done
        for n in g.nodes.values():
            if n.state in ("running", "verifying"):
                n.state, n.error = "failed", "Aero restarted while this step was running; check its result"
        return g

    # ---- running work
    async def run(self, work, concurrency=3, cancel=None, answers=None, on_wait=None):
        """Run every node with work(node) -> result (async). Independent nodes run at the same time, at most
        `concurrency` at once. work may raise WaitingForUser(qid): that node waits for answers(qid) (an async
        function returning the answer or None) while the others go on; when the answer comes it is put in
        node.outputs["answer"] and the node runs again. Returns summary()."""
        self.check_acyclic()
        sem = asyncio.Semaphore(max(1, int(concurrency)))
        running = {}
        waits = {}

        async def one(n):
            async with sem:
                if cancel is not None and cancel.is_set():
                    return
                self.set(n.id, "running")
                try:
                    res = await work(n)
                    self.set(n.id, "completed", result=res)
                    n.outputs["result"] = res
                except WaitingForUser as e:
                    self.set(n.id, "waiting_for_user", waiting_on=e.qid)
                    if on_wait:
                        on_wait(n, e.qid)
                except WaitingForResource as e:
                    self.set(n.id, "waiting_for_resource", waiting_on=e.key)
                except asyncio.CancelledError:
                    self.set(n.id, "cancelled", error="stopped")
                    raise
                except Exception as e:  # noqa: BLE001
                    self.set(n.id, "failed", error=f"{type(e).__name__}: {e}")
                finally:
                    self.save()

        async def wait_answer(n):
            ans = await answers(n.waiting_on) if answers else None
            if ans is None:
                self.set(n.id, "cancelled" if cancel is not None and cancel.is_set() else "failed",
                         error="no answer from the user")
            else:
                n.outputs["answer"] = ans
                self.set(n.id, "pending")

        while True:
            if cancel is not None and cancel.is_set():
                for t in list(running.values()) + list(waits.values()):
                    t.cancel()
                self.cancel_open()
                break
            for n in self.ready():
                if n.id not in running:
                    running[n.id] = asyncio.create_task(one(n))
            for n in self._ordered():
                if n.state == "waiting_for_user" and n.id not in waits:
                    waits[n.id] = asyncio.create_task(wait_answer(n))
            live = [t for t in list(running.values()) + list(waits.values()) if not t.done()]
            if not live:
                for k in [k for k, t in running.items() if t.done()]:
                    running.pop(k)
                for k in [k for k, t in waits.items() if t.done()]:
                    waits.pop(k)
                if not self.ready():
                    break
                continue
            await asyncio.wait(live, return_when=asyncio.FIRST_COMPLETED, timeout=0.5)
            for k in [k for k, t in running.items() if t.done()]:
                running.pop(k)
            for k in [k for k, t in waits.items() if t.done()]:
                waits.pop(k)
        for n in self._ordered():
            if n.state in ("pending", "ready"):
                self.set(n.id, "skipped", error="never became ready")
        if self.status == "running":
            self.status = "done" if all(n.state in ("completed", "skipped") for n in self.nodes.values()) else "partial"
        self.save(force=True)
        return self.summary()


def latest_for_chat(chat_id, limit=3):
    """The newest saved graphs of a chat (for the UI after a reload)."""
    out = []
    if not DIR.is_dir():
        return out
    files = sorted(DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[:200]:
        if p.name in ("questions.json", "ops.json"):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("chat_id") == chat_id:
            out.append(d)
            if len(out) >= limit:
                break
    return out


def prune(max_files=300):
    if not DIR.is_dir():
        return
    files = sorted((p for p in DIR.glob("*.json") if p.name not in ("questions.json", "ops.json")),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[max_files:]:
        try:
            p.unlink()
        except OSError:
            pass


# ---- operation ledger: verify before repeating -----------------------------------------------------------------

OPS = DIR / "ops.json"
OPS_KEEP_S = 3 * 86400


def op_key(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20]


def _ops():
    try:
        d = json.loads(OPS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    now = time.time()
    return {k: v for k, v in d.items() if now - v.get("at", 0) < OPS_KEEP_S}


def _ops_save(d):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = OPS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    os.replace(tmp, OPS)


def peek(key):
    """The ledger's record for key, or None (reading only)."""
    with _lock:
        rec = _ops().get(key)
        return dict(rec) if rec else None


def begin(key, what=""):
    """Start a consequential operation. Returns the earlier record when this key already ran
    ({"status": "done" | "unknown" | "started"}), else None (go ahead; the key is now 'started')."""
    with _lock:
        d = _ops()
        prev = d.get(key)
        if prev and prev["status"] in ("done", "unknown", "started"):
            return dict(prev)
        d[key] = {"status": "started", "at": time.time(), "what": str(what)[:120]}
        _ops_save(d)
        return None


def end(key, status, note=""):
    """status: done (it happened), failed (it certainly did not happen: may be retried), unknown (timed out)."""
    with _lock:
        d = _ops()
        if status == "failed":
            d.pop(key, None)
        else:
            d[key] = {**d.get(key, {}), "status": status, "at": time.time(), "note": str(note)[:200]}
        _ops_save(d)


def clear_op(key):
    with _lock:
        d = _ops()
        if key in d:
            d.pop(key, None)
            _ops_save(d)
