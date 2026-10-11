"""Who owns what right now: named resource locks shared by every agent, subagent and background job.

Keys look like "window:12345", "browser_tab:<chat>", "input:physical", "clipboard", "file:<path>", "model". A
holder is an owner id (a chat id for agents). Rules that keep this deadlock-free and honest:

  - acquire() takes every key it needs at once or none of them (no partial holding, so no lock-order cycles);
  - the same owner may re-take what it already holds (an agent acting twice on its own window);
  - waiting is bounded: acquire returns False after the timeout and holder() says who has it;
  - release_owner() frees everything an owner holds; Stop and the end of every turn call it, so a stopped or crashed
    turn never leaves a stale lock.
"""
import threading
import time

_cond = threading.Condition()
_held = {}          # key -> {"owner", "since", "what", "count"}
EXCLUSIVE_INPUT = "input:physical"


def acquire(keys, owner, timeout=0.0, what="", cancel=None):
    """Take all keys for owner. timeout 0 = try once. cancel: object with is_set(); returns False when it is set."""
    keys = sorted(set(k for k in keys if k))
    if not keys:
        return True
    deadline = time.monotonic() + max(0.0, float(timeout))
    with _cond:
        while True:
            if cancel is not None and cancel.is_set():
                return False
            if all(k not in _held or _held[k]["owner"] == owner for k in keys):
                now = time.time()
                for k in keys:
                    h = _held.get(k)
                    if h:
                        h["count"] += 1
                    else:
                        _held[k] = {"owner": owner, "since": now, "what": what, "count": 1}
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            _cond.wait(min(left, 0.25))


def release(keys, owner):
    """Drop one hold of each key (a key held twice by the same owner needs two releases)."""
    with _cond:
        for k in set(keys):
            h = _held.get(k)
            if h and h["owner"] == owner:
                h["count"] -= 1
                if h["count"] <= 0:
                    _held.pop(k, None)
        _cond.notify_all()


def release_owner(owner):
    """Free everything owner holds. Returns the keys that were freed."""
    with _cond:
        gone = [k for k, h in _held.items() if h["owner"] == owner]
        for k in gone:
            _held.pop(k, None)
        _cond.notify_all()
    return gone


def holder(key):
    with _cond:
        h = _held.get(key)
        return dict(h) if h else None


def held_by(owner):
    with _cond:
        return [k for k, h in _held.items() if h["owner"] == owner]


def snapshot():
    with _cond:
        return {k: {**h, "age_s": round(time.time() - h["since"], 1)} for k, h in _held.items()}


class Hold:
    """with Hold(keys, owner, timeout) as ok: ... (ok is False when the keys could not be taken)."""

    def __init__(self, keys, owner, timeout=0.0, what="", cancel=None):
        self.keys, self.owner, self.timeout, self.what, self.cancel = list(keys), owner, timeout, what, cancel
        self.ok = False

    def __enter__(self):
        self.ok = acquire(self.keys, self.owner, self.timeout, self.what, self.cancel)
        return self.ok

    def __exit__(self, *a):
        if self.ok:
            release(self.keys, self.owner)
        return False
