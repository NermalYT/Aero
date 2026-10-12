"""Every integration comes with Aero: no "Install" buttons to find later.

The installer sets them all up after llama.cpp (python -m aero.integrations.bundle), and on startup Aero finishes any
that are missing in the background: an update from an older version, an install that was offline, a download that
failed. Order matters: Node.js and the small tools first (the npm packs need Node), the multi-GB downloads (image
model, AirLLM's PyTorch, Flutter) last, so the useful things are ready soonest.

Not bundled: xtool (it changes the user's WSL; installed from its pack when asked) and anything the user has to do
themselves (Xcode, Git, signing in). Strict offline mode skips the whole thing.
"""
import json
import sys
import threading
import time

from . import PACKS, load_all
from . import toolchain as tc

SKIP = {("xtool", "xtool")}
FIRST = ["essentials:node", "essentials:uv", "essentials:ruff", "essentials:pyright", "essentials:biome",
         "essentials:typescript", "essentials:bcd"]
LAST = ["essentials:go", "essentials:rust", "flutter:flutter", "bigmodels:airllm", "image:models"]
STATE = tc.ROOT / "bundle.json"
MAX_TRIES = 3                  # background attempts per requirement before it waits for the user (pack card)
_run = {"thread": None, "current": None, "cancel": threading.Event()}


def _key(p, r):
    return f"{p.id}:{r.id}"


def _read():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(d):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, indent=1), encoding="utf-8")


def items():
    """Every bundled requirement in install order: [(key, pack, requirement)]."""
    load_all()
    out = [(_key(p, r), p, r) for p in PACKS.values() for r in p.requirements
           if r.install and (p.id, r.id) not in SKIP]
    rank = {k: i for i, k in enumerate(FIRST)}
    last = {k: i for i, k in enumerate(LAST)}
    out.sort(key=lambda x: (0, rank[x[0]]) if x[0] in rank else (2, last[x[0]]) if x[0] in last else (1, 0))
    seen, uniq = set(), []
    for k, p, r in out:                      # the same tool required by two packs (node) installs once
        if r.id in seen and r.id in ("node",):
            continue
        seen.add(r.id)
        uniq.append((k, p, r))
    return uniq


def status():
    st = _read()
    rows, total, done = [], 0, 0
    for k, p, r in items():
        try:
            ok = r.check()[0]
        except Exception:  # noqa: BLE001
            ok = False
        s = st.get(k, {})
        state = "done" if ok else ("running" if _run["current"] == k else
                                   "failed" if s.get("tries", 0) >= MAX_TRIES else "pending")
        total += r.size_mb
        done += r.size_mb if ok else 0
        rows.append({"key": k, "pack": p.name, "label": r.label, "size_mb": r.size_mb, "state": state,
                     "error": s.get("error") if not ok else None})
    return {"items": rows, "total_mb": total, "done_mb": done, "running": bool(_run["thread"] and
                                                                             _run["thread"].is_alive()),
            "current": _run["current"]}


def run(emit=None, cancel=None, background=False, busy=lambda: False):
    """Install everything that's missing. Returns (installed, failed) counts."""
    emit = emit or (lambda e: None)
    cancel = cancel or _run["cancel"]
    st = _read()
    todo = []
    for k, p, r in items():
        try:
            if r.check()[0]:
                continue
        except Exception:  # noqa: BLE001
            pass
        if background and st.get(k, {}).get("tries", 0) >= MAX_TRIES:
            continue
        todo.append((k, p, r))
    ok_n = fail_n = 0
    for i, (k, p, r) in enumerate(todo, 1):
        if cancel.is_set():
            break
        while background and busy() and not cancel.is_set():       # a model is loading or tuning: don't compete
            time.sleep(5)
        _run["current"] = k
        emit({"type": "bundle_item", "n": i, "of": len(todo), "key": k, "label": r.label, "size_mb": r.size_mb})
        rec = st.setdefault(k, {})
        rec["tries"] = rec.get("tries", 0) + 1
        rec["at"] = time.strftime("%Y-%m-%d %H:%M")
        try:
            detail = r.install(emit, cancel)
            ok = r.check()[0]
            rec.update(ok=ok, error=None if ok else f"installed but not found afterwards ({detail})")
        except tc.Offline as e:
            rec["tries"] -= 1                                   # offline isn't the requirement's fault
            rec["error"] = str(e)
            ok = False
            emit({"type": "bundle_done", "key": k, "ok": False, "error": str(e)})
            break
        except Exception as e:  # noqa: BLE001
            ok = False
            rec.update(ok=False, error=f"{type(e).__name__}: {e}"[:600])
        _write(st)
        ok_n += ok
        fail_n += not ok
        emit({"type": "bundle_done", "key": k, "ok": ok, "error": rec.get("error")})
    _run["current"] = None
    if todo:
        try:
            from . import _states
            _states.clear()
        except Exception:  # noqa: BLE001
            pass
    return ok_n, fail_n


def start_background(busy=lambda: False, log=None):
    """On startup: finish what's missing, quietly, one at a time."""
    from .. import localonly
    if localonly.strict() or (_run["thread"] and _run["thread"].is_alive()):
        return False

    def go():
        try:
            run(lambda e: log and e.get("type") == "bundle_done" and log(e), background=True, busy=busy)
        except Exception:  # noqa: BLE001
            pass
    _run["cancel"].clear()
    _run["thread"] = threading.Thread(target=go, name="aero-bundle", daemon=True)
    _run["thread"].start()
    return True


def stop():
    _run["cancel"].set()


def main():
    """The installer's step: every integration, with a line per item."""
    def emit(e):
        t = e.get("type")
        if t == "bundle_item":
            size = f"{e['size_mb'] / 1024:.1f} GB" if e["size_mb"] >= 1024 else f"{e['size_mb']} MB"
            print(f"  [{e['n']}/{e['of']}] {e['label']} ({size})...", flush=True)
        elif t == "bundle_done" and not e["ok"]:
            print(f"        not installed: {e['error']}. Aero retries it in the background.", flush=True)
        elif t == "progress" and e.get("total"):
            print(f"\r        {e['done'] / 2**20:8.1f} / {e['total'] / 2**20:.1f} MB", end="", flush=True)
            if e["done"] >= e["total"]:
                print(flush=True)
    s = status()
    left = [x for x in s["items"] if x["state"] != "done"]
    print(f"  {len(left)} integrations to set up, about {sum(x['size_mb'] for x in left) / 1024:.1f} GB.", flush=True)
    ok, failed = run(emit)
    print(f"  Integrations: {ok} installed, {failed} left for the background.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
