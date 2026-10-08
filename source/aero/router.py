"""The decision router: a small model on the CPU that reads each request first and decides
which tools the main model gets, whether it should think, whether vision is needed, a short plan,
and whether a cloud review is worth it.

Why: tool schemas are the biggest fixed cost in every agent step (40+ tools is 6-9k tokens that
the GPU model re-reads on a cold cache and that take context away from the chat). The router runs
on the CPU and system RAM, so it costs no VRAM and runs while the GPU is idle, and its answers are
forced into a JSON grammar so every decision is valid.

The router's own prompt (rules + tool catalog) is fixed, so llama-server keeps it in its prompt cache
and each decision only reads the new request (a few hundred tokens).
"""
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import httpx

from .config import IS_WIN, LLAMA_DIR, ROUTER_PORT, load_settings, read_store, write_store
from .engine import LlamaServer, build_args, server_help, supports

ROUTER_DIR = "_router"     # models\_router: hidden from "Your models"
CTX = 8192
_NO_WINDOW = 0x08000000 if IS_WIN else 0

# When one of these is chosen, the tools it depends on come with it.
COMPANIONS = {
    "app_click": ["app_view"], "app_type": ["app_view"], "app_keys": ["app_view"], "app_scroll": ["app_view"],
    "app_read": ["app_view"], "app_view": ["app_list", "app_click", "app_type", "app_keys"],
    "edit_file": ["read_file"], "write_file": ["read_file", "list_dir"], "delete_path": ["list_dir"],
    "move_path": ["list_dir"], "read_file": ["list_dir", "find_files"], "search_files": ["read_file"],
    "browser_click": ["browser_open", "browser_snapshot"], "browser_type": ["browser_open", "browser_snapshot"],
    "browser_open": ["browser_snapshot", "browser_click", "browser_type", "browser_read"],
    "mouse_click": ["screenshot"], "type_text": ["screenshot"], "press_keys": ["screenshot"],
    "web_search": ["fetch_url"], "fetch_url": ["web_search"], "run_command": ["read_file", "list_dir"],
}

RULES = """You are the decision router of Aero, a local AI agent on the user's Windows PC. You never answer the user. You read the request and decide how the main model should handle it, by filling in the JSON form.

Fields
- intent: what the user wants, in under 15 words.
- complexity: trivial (greeting, one-line fact, small talk), simple (one or two steps), moderate (several steps or tools), complex (long multi-step work, code changes across files, research, debugging).
- tools: the tools the main model needs for THIS request, from the catalog only. Pick everything the task plausibly needs (reading before writing, looking before clicking, searching before fetching); a missing tool costs much more than an extra one. Use [] for chat that needs no tools.
- think: true when careful reasoning helps (math, code, planning, debugging, comparisons, tricky questions); false for chat, lookups and simple actions.
- vision: true only when images, screenshots or looking at app windows matter.
- plan: up to 5 short imperative steps for the main model; [] for trivial requests.
- review: true when a second opinion from a stronger cloud model would clearly help (substantial code changes, important documents, risky system changes, long research); false otherwise.
- preference: when the new request states a LASTING preference about how the assistant should work or write (tone, length, format, level of detail, things to avoid, what to call the user), restate it as one short sentence about the user, e.g. "Prefers short answers without headers." or "Wants code fully commented.". An instruction for this one task only ("make this one shorter") is not a preference. Otherwise "".

Rules
- Only the catalog's tool names are valid. Never invent tools.
- Files on this PC: list_dir, find_files, search_files, read_file; changes: write_file, edit_file, move_path, delete_path.
- Commands, installs, git, scripts, system info: run_command.
- An app window ("in Notepad", "click", "this window", "my app"): app_list, app_view, app_click, app_type, app_keys, app_read.
- The web: web_search and fetch_url for facts and pages; browser_* to log in, click or fill forms on websites.
- Earlier chats or personal facts: recall (and remember to save new lasting facts).
- If the user continues an earlier task ("do it again", "now fix that", "continue"), choose the same kind of tools as the context shows."""

_state = {"server": None, "ready": False, "error": None, "model": None, "threads": None, "warm": False,
          "loading": False, "last": None, "prefix_key": None}
_lock = threading.Lock()


# ---------------------------------------------------------------------------- config

def config():
    """Router choice and benchmark results (data/router.json), written by the setup chooser and Settings."""
    return read_store("router.json", {})


def save_config(patch):
    c = config()
    c.update(patch)
    write_store("router.json", c)
    return c


def model_path():
    s = load_settings()
    p = (s.get("router_model") or "").strip() or config().get("path") or ""
    return p if p and Path(p).exists() else None


# ---------------------------------------------------------------------------- server

def _bench_exe():
    name = "llama-bench.exe" if IS_WIN else "llama-bench"
    hits = sorted(LLAMA_DIR.rglob(name)) if LLAMA_DIR.exists() else []
    return hits[0] if hits else None


def physical_cores():
    import psutil
    return psutil.cpu_count(logical=False) or 8


def logical_cores():
    import psutil
    return psutil.cpu_count(logical=True) or physical_cores()


def default_threads():
    # Zen 5 desktop: half the physical cores (one CCD) is usually the sweet spot for small models;
    # the benchmark replaces this with a measured value.
    return max(2, min(8, physical_cores() // 2))


def server_args(path, threads, batch_threads=None):
    cfg = {"ctx": CTX, "ngl": 0, "kv": "f16", "fa": True, "threads": threads, "ub": 512}
    a = build_args(path, cfg, ROUTER_PORT)
    a += ["--jinja"]
    if batch_threads and supports("--threads-batch"):
        a += ["--threads-batch", str(batch_threads)]
    if supports("--device"):
        a += ["--device", "none"]
    if supports("--reasoning-budget"):
        a += ["--reasoning-budget", "0"]
    if supports("--mlock"):
        a += ["--mlock"]                 # stay in RAM while idle between turns (no page-in stall)
    elif supports("--load-mode") and re.search(r"^\s*-\s*mlock\b", server_help(), re.M):
        a += ["--load-mode", "mlock"]    # llama.cpp v0.5.0 replaced --mlock with --load-mode
    if supports("--cache-ram"):
        a += ["--cache-ram", "256"]      # small host-side prompt cache for the fixed rules + catalog
    return a


def status():
    s = _state
    srv = s["server"]
    return {"enabled": bool(load_settings().get("router_enabled", True)), "ready": s["ready"] and bool(srv and srv.running()),
            "loading": s["loading"], "error": s["error"], "model": s["model"], "threads": s["threads"],
            "last": s["last"], "pid": srv.proc.pid if srv and srv.running() else None,
            "configured": bool(model_path()), "bench": config().get("bench")}


def start(emit=None):
    """Start (or restart) the router server in the background. Returns immediately."""
    def run():
        with _lock:
            _start_blocking(emit)
    threading.Thread(target=run, daemon=True, name="router-start").start()


def _start_blocking(emit=None):
    s = load_settings()
    path = model_path()
    if not s.get("router_enabled", True) or not path:
        stop()
        _state["error"] = None if path else "No router model chosen yet (run Update-Aero.bat or pick one in Settings)."
        return False
    cfg = config()
    threads = int(s.get("router_threads") or cfg.get("threads") or default_threads())
    tb = int(cfg.get("batch_threads") or physical_cores())
    srv = _state["server"] or LlamaServer(ROUTER_PORT, "router")
    _state.update(server=srv, loading=True, ready=False, error=None, model=Path(path).stem, threads=threads, warm=False)
    try:
        srv.start(server_args(path, threads, tb), {"CUDA_VISIBLE_DEVICES": "-1", "GGML_VK_VISIBLE_DEVICES": ""})
        ok, why = srv.wait_ready(180)
        if not ok:
            raise RuntimeError(f"router server {why}: " + srv.log_tail(8)[-600:])
        _state.update(ready=True, loading=False)
        return True
    except Exception as e:  # noqa: BLE001
        _state.update(ready=False, loading=False, error=str(e)[:600])
        srv.stop()
        return False


def stop():
    srv = _state.get("server")
    if srv:
        srv.stop()
    _state.update(ready=False, warm=False)


def ready():
    srv = _state.get("server")
    return bool(_state["ready"] and srv and srv.running())


# ---------------------------------------------------------------------------- deciding

def _first_sentence(text, n=110):
    t = re.sub(r"\s+", " ", text or "").strip()
    t = re.sub(r"^\[MCP [^\]]+\]\s*", "", t)
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    t = m.group(1) if m else t
    return t[:n].rstrip()


def catalog(tool_list, skills=None):
    """tool_list: [(name, category, description)] in a stable order."""
    lines = ["# Tool catalog"]
    for name, cat, desc in tool_list:
        lines.append(f"- {name} [{cat}]: {_first_sentence(desc)}")
    if skills:
        lines.append("\n# Skills (instructions the main model can load with use_skill)")
        for sk in skills:
            lines.append(f"- {sk['name']}: {_first_sentence(sk.get('description', ''), 140)}")
    return "\n".join(lines)


def schema(tool_names, skill_names=None):
    props = {
        "intent": {"type": "string", "maxLength": 120},
        "complexity": {"type": "string", "enum": ["trivial", "simple", "moderate", "complex"]},
        "tools": {"type": "array", "items": {"type": "string", "enum": list(tool_names) or ["none"]}, "maxItems": 14},
        "think": {"type": "boolean"},
        "vision": {"type": "boolean"},
        "plan": {"type": "array", "items": {"type": "string", "maxLength": 140}, "maxItems": 5},
        "review": {"type": "boolean"},
    }
    order = ["intent", "complexity", "tools", "think", "vision", "plan", "review"]
    if skill_names:
        props["skills"] = {"type": "array", "items": {"type": "string", "enum": list(skill_names)}, "maxItems": 3}
        order.append("skills")
    props["preference"] = {"type": "string", "maxLength": 200}
    order.append("preference")
    return {"type": "object", "additionalProperties": False, "required": order, "properties": props}


def _context_text(history, max_chars=1800):
    """The last user message plus a little earlier context (previous request, tools used)."""
    users = [m for m in history if m.get("role") == "user"]
    last = users[-1] if users else {}
    parts = []
    if len(users) > 1:
        prev = (users[-2].get("content") or "")[:400]
        used = []
        seen = False
        for m in history:
            if m is users[-2]:
                seen = True
            elif m is last:
                break
            elif seen and m.get("role") == "assistant":
                used += [tc["function"]["name"] for tc in m.get("tool_calls") or []]
        parts.append(f"Previous request: {prev}")
        if used:
            parts.append("Tools used for it: " + ", ".join(dict.fromkeys(used)))
    text = last.get("content") or ""
    extras = []
    if last.get("attachments"):
        extras.append("attached: " + ", ".join(f"{a.get('name')} ({a.get('kind')})" for a in last["attachments"]))
    if last.get("pastes"):
        extras.append(f"{len(last['pastes'])} pasted text block(s)")
    if last.get("target_app"):
        extras.append(f"selected app window: {last['target_app'].get('title')}")
    parts.append("New request: " + text[:max_chars])
    if extras:
        parts.append("(" + "; ".join(extras) + ")")
    return "\n".join(parts)


def decide(history, tool_list, skills=None, timeout=45):
    """Returns (decision dict, info dict). Raises on failure."""
    srv = _state["server"]
    if not ready():
        raise RuntimeError(_state.get("error") or "router not running")
    names = [t[0] for t in tool_list]
    skill_names = [s["name"] for s in (skills or [])]
    sys_text = RULES + "\n\n" + catalog(tool_list, skills)
    body = {"messages": [{"role": "system", "content": sys_text},
                         {"role": "user", "content": _context_text(history)}],
            "temperature": 0, "top_k": 1, "max_tokens": 420, "cache_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "route", "schema": schema(names, skill_names)}}}
    t0 = time.time()
    r = httpx.post(srv.url + "/v1/chat/completions", json=body, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    txt = j["choices"][0]["message"].get("content") or ""
    txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()
    d = json.loads(txt)
    ms = (time.time() - t0) * 1000
    tm = j.get("timings") or {}
    d["tools"] = [n for n in dict.fromkeys(d.get("tools") or []) if n in names]
    d["skills"] = [n for n in dict.fromkeys(d.get("skills") or []) if n in skill_names]
    info = {"ms": round(ms), "prompt_n": tm.get("prompt_n"), "cached_n": tm.get("cache_n"),
            "pp": round(tm.get("prompt_per_second") or 0, 1), "tg": round(tm.get("predicted_per_second") or 0, 1),
            "gen_n": tm.get("predicted_n"), "model": _state["model"], "threads": _state["threads"]}
    _state["last"] = {"at": time.time(), "ms": info["ms"], "intent": d.get("intent"), "tools": d["tools"]}
    return d, info


def expand(chosen, available):
    """Add companion tools so a chosen tool is never stranded without the one it depends on."""
    out = list(dict.fromkeys(chosen))
    for n in list(out):
        for c in COMPANIONS.get(n, []):
            if c in available and c not in out:
                out.append(c)
    return out


def warm(tool_list, skills=None):
    """Prime the prompt cache with the fixed rules + catalog (one tiny request)."""
    key = hash((tuple(t[0] for t in tool_list), tuple(s["name"] for s in skills or [])))
    if _state.get("prefix_key") == key and _state.get("warm"):
        return
    try:
        decide([{"role": "user", "content": "hi"}], tool_list, skills, timeout=120)
        _state.update(warm=True, prefix_key=key)
    except Exception:
        pass


# ---------------------------------------------------------------------------- benchmark

def benchmark(path, emit=print, threads=None, cancel=None):
    """Measure prompt and generation speed on the CPU at several thread counts with llama-bench.
    Generating is limited by memory bandwidth and peaks well below the core count; reading the prompt scales
    with every core, SMT threads included (measured on a 9950X3D2: generation best at 10-12 threads, prompt at 32).
    So the router generates with the fewest threads within 3% of the best generation speed and reads prompts
    with the fastest prompt thread count. A decision is costed as 300 new prompt tokens + 120 generated tokens.
    Returns {"threads", "batch_threads", "rows": [...], "decision_ms", "tg", "pp"}."""
    exe = _bench_exe()
    cores, logical = physical_cores(), logical_cores()
    cand = threads or sorted({t for t in (2, 4, 6, 8, 10, 12, 16, cores, logical * 3 // 4, logical) if 1 < t <= logical})
    rows = []
    if exe:
        cmd = [str(exe), "-m", str(path), "-ngl", "0", "-t", ",".join(map(str, cand)), "-p", "512", "-n", "64",
               "-r", "2", "-o", "json"]
        if IS_WIN:
            cmd += ["-dev", "none"] if "-dev" in _bench_help(exe) else []
        emit(f"Benchmarking the router on the CPU with {', '.join(map(str, cand))} threads (llama-bench)…")
        env = dict(os.environ, CUDA_VISIBLE_DEVICES="-1")
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, cwd=str(exe.parent),
                           creationflags=_NO_WINDOW, env=env)
        try:
            data = json.loads(p.stdout[p.stdout.index("["):])
        except Exception:
            raise RuntimeError("llama-bench failed: " + (p.stderr or p.stdout)[-800:])
        by = {}
        for r in data:
            t = int(r.get("n_threads") or 0)
            d = by.setdefault(t, {"threads": t, "pp": 0.0, "tg": 0.0})
            if r.get("n_prompt") and not r.get("n_gen"):
                d["pp"] = round(float(r.get("avg_ts") or 0), 1)
            elif r.get("n_gen") and not r.get("n_prompt"):
                d["tg"] = round(float(r.get("avg_ts") or 0), 1)
        rows = sorted(by.values(), key=lambda d: d["threads"])
    else:
        emit("llama-bench not found; measuring through llama-server instead…")
        rows = _bench_server(path, cand, emit, cancel)
    for r in rows:
        r["decision_ms"] = round((300 / r["pp"] + 120 / r["tg"]) * 1000) if r["pp"] and r["tg"] else None
        emit(f"  {r['threads']:>2} threads: prompt {r['pp']:>7.1f} tok/s · generate {r['tg']:>6.1f} tok/s"
             + (f" · decision ≈ {r['decision_ms']} ms" if r["decision_ms"] else ""))
    good = [r for r in rows if r["decision_ms"]]
    if not good:
        raise RuntimeError("benchmark produced no usable numbers")
    tg_max = max(r["tg"] for r in good)
    gen = min((r for r in good if r["tg"] >= tg_max * 0.97), key=lambda r: r["threads"])
    pp_best = max(good, key=lambda r: r["pp"])
    decision = round((300 / pp_best["pp"] + 120 / gen["tg"]) * 1000)
    emit(f"Using {gen['threads']} threads to generate and {pp_best['threads']} to read prompts: "
         f"one decision ≈ {decision} ms")
    return {"threads": gen["threads"], "batch_threads": pp_best["threads"], "rows": rows,
            "decision_ms": decision, "tg": gen["tg"], "pp": pp_best["pp"], "at": time.strftime("%Y-%m-%d %H:%M")}


_bh = {}


def _bench_help(exe):
    if exe not in _bh:
        try:
            _bh[exe] = subprocess.run([str(exe), "--help"], capture_output=True, text=True, timeout=30,
                                      creationflags=_NO_WINDOW, cwd=str(Path(exe).parent)).stdout
        except Exception:
            _bh[exe] = ""
    return _bh[exe]


def _bench_server(path, cand, emit, cancel=None):
    """Fallback benchmark through llama-server's own timings."""
    rows = []
    srv = LlamaServer(ROUTER_PORT + 10, "router-bench")
    prompt = "Explain how a CPU cache works. " * 60
    try:
        for t in cand:
            if cancel is not None and cancel.is_set():
                break
            srv.start(server_args(path, t, t), {"CUDA_VISIBLE_DEVICES": "-1"})
            ok, _ = srv.wait_ready(180)
            if not ok:
                continue
            r = httpx.post(srv.url + "/completion", json={"prompt": prompt, "n_predict": 64, "cache_prompt": False,
                                                         "temperature": 0}, timeout=300).json()
            tm = r.get("timings") or {}
            rows.append({"threads": t, "pp": round(tm.get("prompt_per_second") or 0, 1),
                         "tg": round(tm.get("predicted_per_second") or 0, 1)})
    finally:
        srv.stop()
    return rows
