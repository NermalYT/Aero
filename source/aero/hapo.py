"""HAPO: hardware-aware performance optimization.

HAPO turns the tuner's measured trials into named profiles and remembers which one each model runs with.

  fingerprint   what the measurements depend on: GPU, driver, CUDA, CPU, RAM, OS, the llama-server build and the
                model file. A profile measured under a different fingerprint is flagged stale.
  profiles      six goals, each picked by a fixed rule from trials that really ran on this PC. A goal no measured
                trial satisfies says "not measured"; nothing is estimated or extrapolated.
  apply         the chosen profile's llama-server config is used the next time the model loads (the Performance
                Lab reloads it right away). The previous choice goes on a history stack.
  rollback      "Restore previous" pops that stack. If a profile's config fails to load, Aero falls back to the
                tuner's own pick and drops the profile automatically (see server.load).

Store: data/hapo.json  {"active": {tune_key: entry}, "history": {tune_key: [entry, ...]}, "failures": [...]}
tune_key is the tuner's cache key (model file + vision projector + GPU + RAM + speculative decoding).
"""
import hashlib
import json
import re
import time
from pathlib import Path

from . import gguf
from .config import read_store, write_store
from .engine import server_binary, server_help, server_version, spec_types

STORE = "hapo.json"
HISTORY_MAX = 20

PROFILES = [
    ("max_speed", "Maximum Speed",
     "The fastest measured generation (within 5% of the best), then the most context up to 32k."),
    ("balanced", "Balanced",
     "The largest context that keeps 80% of the best generation speed; if that is under 32k, a 32k+ config "
     "that keeps 50% wins instead."),
    ("max_context", "Maximum Context",
     "The largest context that keeps at least 50% of the best generation speed."),
    ("max_quality", "Maximum Quality",
     "The most precise KV cache (f16, then q8_0, then q4_0) with every layer on the GPU, then context up to 32k, "
     "then speed. Needs at least 40% of the best speed."),
    ("agent", "Agent Optimized",
     "At least 32k context for long tool chains, ranked by generation speed plus half the weight on prompt "
     "speed, because tool results are re-read every step. Needs at least 50% of the best speed."),
    ("efficiency", "Efficiency",
     "The least VRAM that still keeps 70% of the best speed and 16k context (or the largest measured), leaving "
     "room for games and other apps."),
]
NAMES = {k: n for k, n, _ in PROFILES}
KV_RANK = {"q4_0": 0, "q8_0": 1, "bf16": 2, "f16": 2}


# ---- fingerprint ------------------------------------------------------------------------------------------

def _sha(obj, n=12):
    return hashlib.sha1(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:n]


def model_fingerprint(path):
    """Cheap and exact enough: file name, total bytes, and a hash of the first 4 MB (the GGUF header and
    metadata), so a re-downloaded or re-quantized file with the same name is told apart."""
    p = Path(path)
    parts = gguf.split_parts(p)
    size = sum(x.stat().st_size for x in parts)
    h = hashlib.sha256()
    with open(parts[0], "rb") as f:
        h.update(f.read(4 << 20))
    out = {"file": p.name, "bytes": size, "head_sha256": h.hexdigest()[:16]}
    try:
        meta = gguf.summarize(p)
        out.update(arch=meta.get("arch"), layers=meta.get("n_layer"), ctx_train=meta.get("ctx_train"))
    except Exception:
        pass
    return out


def fingerprint(hw, model_path=None):
    g = (hw.get("gpus") or [{}])[0]
    hardware = {"gpu": hw.get("gpu_name"), "vram_mb": hw.get("vram_total_mb"), "driver": g.get("driver"),
                "cuda": hw.get("cuda"), "cpu": hw.get("cpu"), "cores": hw.get("cores"), "threads": hw.get("threads"),
                "ram_gb": round((hw.get("ram_total_mb") or 0) / 1024), "os": hw.get("os")}
    exe = server_binary()
    engine = {"binary": str(exe) if exe else None, "version": server_version(),
              "spec_types": sorted(spec_types()), "flags_sha": hashlib.sha1(server_help().encode()).hexdigest()[:12]}
    fp = {"hardware": hardware, "engine": engine, "id": _sha({"h": hardware, "e": engine})}
    if model_path:
        try:
            fp["model"] = model_fingerprint(model_path)
        except OSError as e:
            fp["model"] = {"error": str(e)}
    return fp


# ---- trials -> candidates -----------------------------------------------------------------------------------

_DESC = re.compile(r"ctx ([\d,]+) \| (?:CPU only|gpu (?:experts )?(\d+)/(\d+)) \| kv (\w+) \| ub (\d+)(?: \| (\d+) threads)?")


def knobs_of(trial, entry, hw):
    """The tuner's knob dict for a trial. Older tune caches stored only the description, so parse it."""
    if trial.get("knobs"):
        return dict(trial["knobs"])
    m = _DESC.search(trial.get("desc") or "")
    if not m:
        return None
    n = (entry.get("meta") or {}).get("n_layer") or int(m.group(3) or 0)
    on_gpu = int(m.group(2)) if m.group(2) else 0
    return {"ctx": int(m.group(1).replace(",", "")), "k": (n - on_gpu) if m.group(2) else n, "kv": m.group(4),
            "ub": int(m.group(5)), "t": int(m.group(6)) if m.group(6) else (hw.get("cores") or 8)}


def candidates(entry, hw):
    """Passing, measured trials (re-test records folded into their originals by the tuner)."""
    out = []
    for t in entry.get("trials") or []:
        if not t.get("ok") or t.get("retest") or not t.get("tg"):
            continue
        k = knobs_of(t, entry, hw)
        if not k:
            continue
        out.append({"n": t["n"], "desc": t["desc"], "knobs": k, "tg": float(t["tg"]), "pp": float(t.get("pp") or 0),
                    "mem_mb": t.get("mem_mb"), "runs": len(t.get("runs") or []) or 1})
    return out


def pareto(cands):
    """Trials no other trial beats on speed, context and VRAM at once."""
    def dom(a, b):      # a dominates b
        ge = a["tg"] >= b["tg"] and a["knobs"]["ctx"] >= b["knobs"]["ctx"] and (a["mem_mb"] or 0) <= (b["mem_mb"] or 0)
        gt = a["tg"] > b["tg"] or a["knobs"]["ctx"] > b["knobs"]["ctx"] or (a["mem_mb"] or 0) < (b["mem_mb"] or 0)
        return ge and gt
    return [c["n"] for c in cands if not any(dom(o, c) for o in cands if o is not c)]


def pick(goal, cands):
    """One measured trial for a goal, or None. Mirrors the tuner's own ranking for the three tuner goals."""
    if not cands:
        return None
    mx = max(c["tg"] for c in cands)
    mxp = max(c["pp"] for c in cands) or 1
    kvr = lambda c: KV_RANK.get(c["knobs"]["kv"], 1)          # noqa: E731
    ctx = lambda c: c["knobs"]["ctx"]                           # noqa: E731
    if goal == "max_speed":
        pool = [c for c in cands if c["tg"] >= mx * 0.95]
        return max(pool, key=lambda c: (min(ctx(c), 32768), kvr(c), c["tg"], c["pp"]))
    if goal in ("balanced", "max_context"):
        floor = 0.8 if goal == "balanced" else 0.5
        pool = [c for c in cands if c["tg"] >= mx * floor]
        best = max(pool, key=lambda c: (ctx(c), kvr(c), c["tg"], c["pp"]))
        if goal == "balanced" and ctx(best) < 32768:
            agent = [c for c in cands if ctx(c) >= 32768 and c["tg"] >= mx * 0.5]
            if agent:
                return min(agent, key=lambda c: (ctx(c), -c["tg"]))
        return best
    if goal == "max_quality":
        pool = [c for c in cands if c["tg"] >= mx * 0.4]
        return max(pool, key=lambda c: (kvr(c), c["knobs"]["k"] == 0, min(ctx(c), 32768), c["tg"])) if pool else None
    if goal == "agent":
        pool = [c for c in cands if ctx(c) >= 32768 and c["tg"] >= mx * 0.5]
        return max(pool, key=lambda c: c["tg"] / mx + 0.5 * c["pp"] / mxp) if pool else None
    if goal == "efficiency":
        need = min(16384, max(ctx(c) for c in cands))
        pool = [c for c in cands if c["tg"] >= mx * 0.7 and ctx(c) >= need and c["mem_mb"]]
        return min(pool, key=lambda c: (c["mem_mb"], -c["tg"])) if pool else None
    raise ValueError(goal)


def _why_missing(goal, cands):
    if not cands:
        return "No passing trial is saved for this model yet. Run Quick Tune or Deep Tune."
    return {"agent": "No measured config reached 32k context at half the best speed. Raise the VRAM limit or "
                     "run Deep Tune.",
            "max_quality": "No measured config kept 40% of the best speed.",
            "efficiency": "VRAM use was not measured for any passing trial (CPU-only runs)."}.get(goal, "Not measured.")


# ---- store ------------------------------------------------------------------------------------------------

def _load():
    s = read_store(STORE, {})
    s.setdefault("active", {})
    s.setdefault("history", {})
    s.setdefault("failures", [])
    return s


def active(key):
    return _load()["active"].get(key)


def profiles(entry, hw, key):
    """Everything the Performance Lab shows for one tuned model."""
    cands = candidates(entry, hw)
    front = set(pareto(cands))
    act = active(key)
    out = []
    for goal, name, rule in PROFILES:
        c = pick(goal, cands)
        p = {"id": goal, "name": name, "rule": rule, "measured": bool(c)}
        if c:
            p.update(trial=c["n"], desc=c["desc"], knobs=c["knobs"], tg=c["tg"], pp=c["pp"], ctx=c["knobs"]["ctx"],
                     kv=c["knobs"]["kv"], mem_mb=c["mem_mb"], runs=c["runs"], pareto=c["n"] in front)
        else:
            p["why"] = _why_missing(goal, cands)
        p["active"] = bool(act and act.get("profile") == goal)
        out.append(p)
    return {"profiles": out, "candidates": [{**c, "pareto": c["n"] in front} for c in cands],
            "tuner_pick": {"trial": entry.get("trial_n"), "desc": entry.get("desc"), "tg": entry.get("tg"),
                           "pp": entry.get("pp"), "tg_deep": entry.get("tg_deep"), "mode": entry.get("mode"),
                           "ctx": (entry.get("config") or {}).get("ctx")},
            "active": act, "history": _load()["history"].get(key, [])[-HISTORY_MAX:][::-1],
            "tuned_at": entry.get("tuned_at"), "llama_version": entry.get("llama_version")}


def apply(key, goal, entry, hw, server_cfg, fp_id):
    """Make `goal` the profile this model loads with. server_cfg(knobs) -> llama-server config."""
    if goal == "tuner":
        return clear(key)
    c = pick(goal, candidates(entry, hw))
    if not c:
        raise ValueError(f"{NAMES.get(goal, goal)} has no measured config for this model yet.")
    s = _load()
    prev = s["active"].get(key)
    if prev and prev.get("profile") == goal and prev.get("trial") == c["n"]:
        return prev                                   # already in use: nothing to undo later
    s["history"].setdefault(key, []).append(prev or {"profile": "tuner", "applied_at": None})
    s["history"][key] = s["history"][key][-HISTORY_MAX:]
    s["active"][key] = {"profile": goal, "name": NAMES[goal], "trial": c["n"], "desc": c["desc"], "knobs": c["knobs"],
                        "config": server_cfg(c["knobs"]), "tg": c["tg"], "pp": c["pp"], "mem_mb": c["mem_mb"],
                        "applied_at": time.strftime("%Y-%m-%d %H:%M"), "fingerprint": fp_id,
                        "tuned_at": entry.get("tuned_at")}
    write_store(STORE, s)
    return s["active"][key]


def clear(key):
    s = _load()
    prev = s["active"].pop(key, None)
    if prev:
        s["history"].setdefault(key, []).append(prev)
        s["history"][key] = s["history"][key][-HISTORY_MAX:]
    write_store(STORE, s)
    return None


def restore(key):
    """Undo the last apply: the previous profile (or the tuner's pick) becomes active again."""
    s = _load()
    hist = s["history"].get(key) or []
    if not hist:
        raise ValueError("There is no earlier profile to go back to.")
    prev = hist.pop()
    if prev and prev.get("profile") not in (None, "tuner"):
        s["active"][key] = {**prev, "restored_at": time.strftime("%Y-%m-%d %H:%M")}
    else:
        s["active"].pop(key, None)
    write_store(STORE, s)
    return s["active"].get(key)


def failed(key, error):
    """The active profile's config would not load: drop it so the tuner's pick is used, and remember why."""
    s = _load()
    bad = s["active"].pop(key, None)
    if bad:
        s["failures"].append({"at": time.strftime("%Y-%m-%d %H:%M"), "profile": bad.get("profile"),
                              "desc": bad.get("desc"), "error": str(error)[:400]})
        s["failures"] = s["failures"][-30:]
    write_store(STORE, s)
    return bad


def forget(keys):
    s = _load()
    for k in keys:
        s["active"].pop(k, None)
        s["history"].pop(k, None)
    write_store(STORE, s)
