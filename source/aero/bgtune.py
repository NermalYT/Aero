"""Background tuning (1.2.80): while the PC is idle, Aero keeps tuning the loaded model, deeper than the last time.

When the loaded model's tuning is Short or Medium, nobody has chatted for a while (Settings > Model & tuning, default
20 minutes), no scheduled task is due within 30 minutes, nothing else is running, no HAPO profile is chosen for it and
the PC isn't on battery, Aero runs the next depth (Short -> Medium -> Long) for that model, warm-started from its
current tuning (tuner.warm_start: the previous best is the first trial), then loads the result.

While it runs it keeps checking: unplugging the PC, a scheduled task falling due or a download / load you start stop
it, and a message stops it at once. The trial in progress is abandoned, the model comes back with its saved tuning,
and a waiting message runs as soon as it is back (vram_policy.hold). Loading, selecting or unloading a model stops it
without putting the old model back. A result that is clearly worse than the saved tuning (less context, or slower
beyond measurement noise; e.g. another app took VRAM meanwhile) is not kept: the saved tuning goes back.

Each run's log is in data/logs/bgtune.log. Nothing runs for AirLLM, Remote Mode, or when the user turned it off.
"""
import threading
import time

from .config import LOGS

NEXT = {"short": "medium", "medium": "long"}
IDLE_MIN = 20
SOON_S = 1800            # a scheduled task due within this many seconds: don't start
WATCH_S = 10             # how often a run checks that it may go on
LOG = LOGS / "bgtune.log"
_state = {"thread": None, "running": False, "cancel": None, "reload": True, "last": None, "stop": threading.Event(),
          "depth": None, "since": None}


class Busy(RuntimeError):
    """A load or download the user started holds the model: background tuning doesn't start."""


def running():
    return _state["running"]


def interrupt(reload=True):
    """Stop the background run now. reload: put the model back with its saved tuning (a chat is waiting); False when
    the caller loads or unloads a model itself."""
    c = _state["cancel"]
    if _state["running"] and c is not None:
        _state["reload"] = _state["reload"] and reload
        _state["why"] = _state.get("why") or ("stopped for a message" if reload else "stopped for another model")
        c.set()
        return True
    return False


def status():
    return {"running": _state["running"], "depth": _state.get("depth"), "since": _state.get("since"),
            "last": _state["last"]}


def on_battery():
    try:
        import psutil
        b = psutil.sensors_battery()
        return bool(b and not b.power_plugged)
    except Exception:  # noqa: BLE001
        return False


def due(hooks, now=None):
    """The run to do now, or None: {"model", "depth", "from", "limit_gb", "tg", "ctx", "key"}."""
    now = time.time() if now is None else now
    s = hooks["settings"]()
    if not s.get("bg_tune", True):
        return None
    st = hooks["state"]
    m = st.get("model")
    if st.get("status") != "ready" or not m or st.get("remote") or not hooks["engine_ok"]():
        return None
    tune = st.get("tune") or {}
    if tune.get("depth") not in NEXT or not tune.get("key") or tune.get("profile") or hooks["profile"](tune["key"]):
        return None                          # a chosen HAPO profile is loaded instead of the tuner's pick
    try:
        idle_min = max(5.0, float(s.get("bg_tune_idle_min") or IDLE_MIN))
    except (TypeError, ValueError):
        idle_min = IDLE_MIN
    if now - max(hooks["last_activity"](), hooks["started"]) < idle_min * 60:
        return None
    if hooks["busy"]() or on_battery() or hooks["task_soon"](now + SOON_S):
        return None
    limit = tune.get("limit_gb") or 0
    return {"model": m, "depth": NEXT[tune["depth"]], "from": tune["depth"], "key": tune["key"],
            "limit_gb": limit if limit and limit > 0 else "auto", "tg": tune.get("tg"), "ctx": st.get("ctx")}


def worse(new, old_tg, old_ctx):
    """A finished run that should not replace the saved tuning: less context, or slower beyond noise (a bigger
    context may cost up to 20 % speed, as the Balanced goal allows)."""
    tg, ctx = float(new.get("tg") or 0), int(new.get("ctx") or 0)
    if not old_tg or not old_ctx:
        return False
    if ctx < old_ctx:
        return True
    return tg < float(old_tg) * (0.8 if ctx > old_ctx else 0.92)


def _logger():
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        f = open(LOG, "w", encoding="utf-8")
    except OSError:
        return lambda ev: None, lambda: None

    def emit(ev):
        if not isinstance(ev, dict):
            return
        t = ev.get("type")
        if t == "log":
            line = ev.get("text") or ""
        elif t == "trial_done":
            line = (f"#{ev.get('n')} {ev.get('desc')}: " + (f"{ev.get('tg')} tok/s, prompt {ev.get('pp')} tok/s"
                                                          if ev.get("ok") else f"failed ({ev.get('error')})"))
        elif t == "tune_done":
            line = (f"Best: #{ev.get('n')} {ev.get('desc')}, {ev.get('tg')} tok/s ({ev.get('trials')} trials, "
                    f"{ev.get('elapsed_s')} s)")
        else:
            return
        try:
            f.write(time.strftime("%H:%M:%S ") + line + "\n")
            f.flush()
        except (OSError, ValueError):
            pass
    return emit, f.close


def _watch(hooks, cancel, done):
    """Stop the run when it may no longer go on (unplugged, a task due, the user started a load or download)."""
    while not done.wait(WATCH_S):
        try:
            why = "the PC is on battery" if on_battery() else "a scheduled task is due" if hooks["task_soon"](
                time.time()) else "a load or download started" if hooks["user_job"]() else ""
        except Exception:  # noqa: BLE001
            why = ""
        if why:
            _state["why"] = why
            cancel.set()
            return


def run(job, hooks):
    cancel, done = threading.Event(), threading.Event()
    hooks["hold"](True)                      # first: a message arriving from now on waits for the model
    _state.update(running=True, cancel=cancel, reload=True, depth=job["depth"], since=time.time(), why="")
    emit, close = _logger()
    emit({"type": "log", "text": f"Background tuning: {job['model'].get('name')} {job['from']} -> {job['depth']}"})
    t0 = time.time()
    saved = hooks["saved"](job["key"])
    threading.Thread(target=_watch, args=(hooks, cancel, done), daemon=True).start()
    try:
        res = hooks["load"](job["model"], True, job["limit_gb"], job["depth"], emit, cancel)
        if not (res or {}).get("ready"):
            raise RuntimeError("the model did not load after tuning")
        last = {"at": time.time(), "model": job["model"].get("name"), "depth": job["depth"], "ok": True,
                "tg_before": job.get("tg"), "tg": res.get("tg"), "desc": res.get("desc"),
                "minutes": round((time.time() - t0) / 60, 1)}
        if saved and worse(res, job.get("tg"), job.get("ctx")):
            emit({"type": "log", "text": f"Result {res.get('desc')} ({res.get('tg')} tok/s) is worse than the saved "
                                         "tuning: keeping the saved one."})
            hooks["restore"](job["key"], saved)
            hooks["load"](job["model"], False, None, job["from"], emit, threading.Event())
            last.update(ok=False, why="the result was worse than the saved tuning, so the saved one stayed")
        _state["last"] = last
    except Busy:
        emit({"type": "log", "text": "A load or download you started holds the model: not now."})
    except Exception as e:  # noqa: BLE001  (stopped, or a failure): back to the saved tuning
        stopped = cancel.is_set()
        why = _state.get("why") or "stopped"
        emit({"type": "log", "text": f"Stopped: {why}." if stopped else f"Failed: {type(e).__name__}: {e}"})
        _state["last"] = {"at": time.time(), "model": job["model"].get("name"), "depth": job["depth"], "ok": False,
                          "why": why if stopped else f"{type(e).__name__}: {e}"[:200]}
        if _state["reload"]:
            try:
                hooks["load"](job["model"], False, None, job["from"], emit, threading.Event())
            except Exception as e2:  # noqa: BLE001
                emit({"type": "log", "text": f"Putting the saved tuning back failed: {e2}"})
    finally:
        done.set()
        close()
        _state.update(running=False, cancel=None, depth=None, since=None)
        hooks["hold"](False)


def _loop(hooks):
    while not _state["stop"].wait(60):
        try:
            job = due(hooks)
            if job:
                run(job, hooks)
        except Exception:  # noqa: BLE001
            continue


def start(hooks):
    """hooks: state (STATE), settings(), load(m, retune, limit_gb, depth, emit, cancel) -> result (raises Busy),
    hold(on), busy(), user_job(), engine_ok(), profile(key), saved(key) -> entry, restore(key, entry),
    last_activity(), task_soon(before_epoch), started (epoch)."""
    if _state["thread"] and _state["thread"].is_alive():
        return
    _state["stop"].clear()
    _state["thread"] = threading.Thread(target=_loop, args=(hooks,), daemon=True, name="aero-bgtune")
    _state["thread"].start()
