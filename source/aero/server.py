"""FastAPI backend: serves the UI and the JSON/SSE API."""
import asyncio
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import (advisor, agent, agents, app_registry, attachments, bench, claude_code, clarifications, cloud, experience, fit,
               gguf, github, hapo, hardware, hf, input_guard, localonly, looplog, memory, migrate, models, mods, osinfo,
               pipeline, remote_sessions, resources, router, skills, stats, task_graph, tools, tuner, updater, vault,
               vram_policy)
from .config import (APP_NAME, CHATS, DATA, LLAMA_PORT, LOGS, PKG_DIR, VERSION, load_settings, save_settings)
from .engine import LlamaServer, build_args, server_binary, server_version, spec_args
from .tools import mcp_client, mcp_import

localonly.install()          # every outbound request from this process is checked (strict offline) and audited
app = FastAPI(title=APP_NAME)
ENGINE = LlamaServer(LLAMA_PORT, "main")
STATE = {"status": "idle", "model": None, "ctx": 0, "vision": False, "desc": "", "error": None,
         "url": ENGINE.url, "tune": None, "warning": None, "cfg": None, "remote": False}
_job = {"cancel": threading.Event(), "busy": False}
_hw_cache = {"t": 0, "v": None}
_last_ping = {"t": time.time(), "ui": 0.0, "bye": 0.0, "chat": 0.0}   # ui/bye: heartbeat from the app window
BOOT_ID = uuid.uuid4().hex[:12]      # changes on every start: the UI reloads when it sees a new one after a restart


def hw(force=False):
    if force or not _hw_cache["v"] or time.time() - _hw_cache["t"] > 30:
        _hw_cache.update(t=time.time(), v=hardware.snapshot())
    return _hw_cache["v"]


def sse(obj):
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def stream_job(fn):
    """Run blocking fn(emit, cancel) in a thread and stream its events as SSE."""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    if _job["busy"]:
        raise HTTPException(409, "Another download/tune/load is already running.")
    _job["busy"] = True
    _job["cancel"] = threading.Event()
    cancel = _job["cancel"]

    def emit(ev):
        loop.call_soon_threadsafe(q.put_nowait, ev)

    def runner():
        try:
            res = fn(emit, cancel)
            emit({"type": "result", "result": res})
        except tuner.Cancelled:
            emit({"type": "error", "error": "Cancelled"})
        except Exception as e:  # noqa: BLE001
            emit({"type": "error", "error": f"{e}"})
        finally:
            _job["busy"] = False
            emit(None)

    threading.Thread(target=runner, daemon=True).start()

    async def gen():
        while True:
            ev = await q.get()
            if ev is None:
                break
            yield sse(ev)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ---- state / settings ------------------------------------------------------------------------

@app.get("/api/state")
def get_state(ui: int = 0):
    _last_ping["t"] = time.time()
    if ui:
        _last_ping["ui"] = time.time()
    if STATE["status"] == "ready" and not ENGINE.running():
        STATE.update(status="error", error="The model server stopped unexpectedly. See data/logs/llama-main.log.\n"
                     + ENGINE.log_tail(12))
    return {"app": APP_NAME, "version": VERSION, "boot": BOOT_ID, "engine": {k: v for k, v in STATE.items() if k != "url"},
            "busy": _job["busy"], "llama": {"found": bool(server_binary()), "path": str(server_binary() or "")},
            "settings": load_settings(), "hw": hw(), "os": {"name": osinfo.name(), "kind": osinfo.kind()},
            "secrets_where": vault.where(), "update": _update_brief(), "stopping": _last_ping.get("exit"),
            "paths": {"root": str(DATA.parent), "data": str(DATA)}}


def _update_brief():
    u = updater.STATE
    return {k: u.get(k) for k in ("current", "latest", "available", "skipped", "checked", "checking", "error",
                                  "installable", "phase")}


@app.get("/api/hw")
def get_hw():
    return hw(force=True)


ROUTER_KEYS = {"router_enabled", "router_model", "router_threads"}
GITHUB_KEYS = {"github_toolsets", "github_read_only"}


@app.get("/api/settings/defaults")
def settings_defaults():
    from .config import DEFAULTS
    return {"user_profile": DEFAULTS["user_profile"], "system_prompt": DEFAULTS["system_prompt"]}


@app.put("/api/settings")
async def put_settings(req: Request):
    patch = await req.json()
    before = load_settings()
    out = save_settings(patch)
    if any(k in patch and patch[k] != before.get(k) for k in ROUTER_KEYS):
        router.start()
    if any(k in patch and patch[k] != before.get(k) for k in GITHUB_KEYS):
        await asyncio.to_thread(github.refresh_entry)
    if "skills_disabled" in patch:
        skills.catalog(force=True)
    return out


# ---- models ----------------------------------------------------------------------------------

def _tuned_info(m, h, ver):
    try:
        c = tuner.cached(tuner.cache_key(m["path"], m.get("mmproj"), h, spec_args(m["path"], load_settings())))
        if c:
            return {"desc": c["desc"], "tg": c["tg"], "pp": c["pp"], "ctx": c["config"]["ctx"], "at": c["tuned_at"],
                    "limit_gb": round(c.get("limit_mb", 0) / 1024, 2), "depth": c.get("depth"),
                    "mode": c.get("mode"), "trials": len(c.get("trials", [])),
                    "advisor": (c.get("advisor") or {}).get("name"),
                    "stale": bool(ver and c.get("llama_version") and c["llama_version"] != ver)}
    except Exception:
        pass
    return None


@app.get("/api/models")
def list_models():
    h, ver = hw(), server_version()
    out = []
    for m in models.all_models():
        m = dict(m)
        if m.get("exists"):
            m["tuned"] = _tuned_info(m, h, ver)
            m["caps"] = _local_caps(m)
        out.append(m)
    return out


_CAPS_CACHE = {}


def _local_caps(m):
    """Capabilities of a downloaded model, from its own header and its projector's (cached per file)."""
    key = (m["path"], m.get("mmproj"))
    try:
        stamp = Path(m["path"]).stat().st_mtime
    except OSError:
        return {}
    hit = _CAPS_CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    caps = {}
    try:
        meta = gguf.summarize(m["path"])
        caps = {"tools": meta["has_tools_template"], "thinking": meta["has_thinking_template"],
                "moe": bool(meta["experts"]), "ctx_train": meta["ctx_train"],
                "mtp": bool(meta["mtp_layers"]) or bool(re.search(r"(^|[-_.])MTP([-_.]|$)", Path(m["path"]).name, re.I)),
                "params": meta["params"]}
        if m.get("mmproj") and Path(m["mmproj"]).exists():
            caps.update({k: v for k, v in gguf.mmproj_caps(gguf.read_metadata(m["mmproj"])).items() if k != "projector"})
    except Exception:  # noqa: BLE001
        pass
    _CAPS_CACHE[key] = (stamp, caps)
    return caps


@app.post("/api/models/add")
async def add_model(req: Request):
    body = await req.json()
    p = Path(body["path"].strip().strip('"'))
    if not p.is_file() or p.suffix.lower() != ".gguf":
        raise HTTPException(400, f"Not a .gguf file: {p}")
    return models.add(p)


@app.post("/api/models/browse")
def browse_model():
    """Native file picker (runs on this PC, the same machine as the UI)."""
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(title="Choose a GGUF model", filetypes=[("GGUF models", "*.gguf")])
        root.destroy()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"File dialog unavailable: {e}")
    if not path:
        return {"cancelled": True}
    return models.add(path)


@app.delete("/api/models/{mid}")
def delete_model(mid: str, delete_files: bool = False):
    m = models.get(mid)
    if m and STATE.get("model") and STATE["model"].get("id") == mid:
        unload()
    if m:
        tuner.forget(m["path"])
    return {"removed": bool(models.remove(mid, delete_files))}


@app.post("/api/models/{mid}/forget_tune")
def forget_tune(mid: str):
    m = models.get(mid)
    if m:
        try:
            hapo.forget([_tune_key(m, load_settings(), hw())])
        except Exception:
            pass
        tuner.forget(m["path"])
    return {"ok": True}


# ---- Hugging Face ----------------------------------------------------------------------------

@app.get("/api/hf/search")
def hf_search(q: str = ""):
    try:
        return hf.search(q)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Hugging Face search failed: {e}")


@app.get("/api/hf/files")
def hf_files(repo: str):
    try:
        d = hf.list_files(repo)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Could not list files: {e}")
    h = hw(force=True)
    vram, ram = h["vram_total_mb"], h["ram_total_mb"]
    for f in d["files"]:
        f["local"] = hf.local_path(repo, f["path"]).exists()
    d["vram_mb"], d["ram_mb"] = vram, ram
    d["budget_mb"], d["budget_note"] = _plan_budget(h)
    d["model"] = _repo_model_info(repo, d)
    meta = d["model"].get("meta")
    if meta:
        mm = d["mmproj"][0] if d["mmproj"] else None
        rec = fit.plan(d["files"], meta, mm, d["budget_mb"], ram, gpu=bool(h["gpus"]))
        d["recommended"], d["recommended_reason"] = rec["with_proj"]
        d["recommended_text"], d["recommended_text_reason"] = rec["text_only"]
        for f in d["files"]:
            f["fit"] = (f["with_proj"] if mm else f["text_only"])["fit"]
    else:                                     # header unreadable: size-only fallback
        for f in d["files"]:
            mb = f["size"] / 2**20
            f["fit"] = "gpu" if vram and mb + 1500 <= vram else ("split" if mb <= vram + ram * 0.75 else "no")
        d["recommended"] = d["recommended_text"] = hf.recommend(d["files"], vram, ram)
        d["recommended_reason"] = d["recommended_text_reason"] = "estimated from file sizes only"
    return d


def _plan_budget(h):
    """VRAM a model may use: the limit typed last time, else what's free right now (minus a little)."""
    if not h["gpus"]:
        return int(h["ram_total_mb"] * 0.6), "no dedicated GPU: planning for system RAM"
    total = h["vram_total_mb"]
    lim = load_settings().get("last_vram_limit_gb")
    if lim:
        return int(min(float(lim) * 1024, total - 64)), f"the {lim:g} GB VRAM limit you typed last time"
    used = sum(x.get("used_mb") or 0 for x in h["gpus"])
    others = max(0, used - (ENGINE_VRAM["mb"] if ENGINE.running() and h.get("vram_measured", True) else 0))
    what = "other apps use now" if h.get("vram_measured", True) else "the desktop and other apps usually use (estimated)"
    return int(total - others - 300), f"{total / 1024:.1f} GB of VRAM minus {others / 1024:.1f} GB {what}"


def _repo_model_info(repo, d):
    """Capabilities and architecture, read from the smallest quant's header plus the projector's."""
    out = {"caps": {"text": True}, "notes": []}
    try:
        info = hf.model_info(repo)
    except Exception:  # noqa: BLE001
        info = {}
    out.update(pipeline=info.get("pipeline"), license=info.get("license"), base_model=info.get("base_model"))
    meta = None
    if d["files"]:
        try:
            meta = gguf.summarize_kv(hf.remote_metadata(repo, d["files"][0]["parts"][0]))
        except Exception as e:  # noqa: BLE001
            out["notes"].append(f"Could not read the model header ({e}); fit is estimated from file sizes.")
    if meta:
        if not meta["params"]:
            meta["params"] = int((info.get("gguf") or {}).get("total") or 0)
        out["meta"] = meta
        mtp_files = any(re.search(r"(^|[-_.])MTP([-_.]|$)", f["name"], re.I) for f in d["files"])
        out["caps"].update(tools=meta["has_tools_template"], thinking=meta["has_thinking_template"],
                           moe=bool(meta["experts"]), mtp=bool(meta["mtp_layers"]) or mtp_files)
        out["kv_mb_32k"] = round(gguf.kv_mb(meta, 32768))
        out["kv_mb_32k_f16"] = round(gguf.kv_mb(meta, 32768, "f16"))
        out["license"] = out["license"] or meta["license"]
    if d["mmproj"]:
        mm = d["mmproj"][0]
        try:
            pc = gguf.mmproj_caps(hf.remote_metadata(repo, mm["path"]))
        except Exception:  # noqa: BLE001
            pc = {"vision": True, "audio": False, "projector": ""}
        out["caps"].update(vision=pc["vision"], audio=pc["audio"])
        out["projector"] = {"file": mm["path"], "size": mm["size"], "type": pc["projector"]}
    elif (out.get("pipeline") or "") in ("image-text-to-text", "any-to-any", "audio-text-to-text"):
        out["notes"].append("The original model takes images or audio, but this repo has no mmproj projector "
                            "file, so only text will work.")
    return out


@app.post("/api/hf/download")
async def hf_download(req: Request):
    body = await req.json()
    repo, name = body["repo"], body["name"]

    def job(emit, cancel):
        d = hf.list_files(repo)
        f = next((x for x in d["files"] if x["name"] == name), None)
        if not f:
            raise RuntimeError("File not found in repo")
        paths = list(f["parts"])
        mm = d["mmproj"][0]["path"] if (d["mmproj"] and body.get("mmproj", True)) else None
        if mm:
            paths.append(mm)
        local = hf.download(repo, paths, emit, cancel)
        m = models.add(local[0], repo=repo, mmproj=str(hf.local_path(repo, mm)) if mm else None,
                       name=f"{repo.split('/')[-1]} · {f['quant']}")
        return m
    return stream_job(job)


@app.post("/api/cancel")
def cancel_job():
    _job["cancel"].set()
    return {"ok": True}


# ---- load / tune -----------------------------------------------------------------------------

def unload():
    ENGINE.stop()
    STATE.update(status="idle", model=None, ctx=0, vision=False, desc="", warning=None, cfg=None, remote=False)


@app.post("/api/unload")
def api_unload():
    unload()
    return {"ok": True}


@app.get("/api/models/{mid}/tune_info")
def tune_info(mid: str):
    """Everything the VRAM-limit and depth prompts need."""
    m = models.get(mid)
    if not m:
        raise HTTPException(404, "Unknown model")
    h = hw(force=True)
    others = 0
    if h["gpus"]:
        others = sum(x["used_mb"] for x in h["gpus"]) - \
            (ENGINE_VRAM["mb"] if ENGINE.running() and h.get("vram_measured", True) else 0)
    size_mb = 0
    try:
        size_mb = sum(p.stat().st_size for p in gguf.split_parts(Path(m["path"]))) / 2**20
    except Exception:
        pass
    mm = 0
    if m.get("mmproj") and Path(m["mmproj"]).exists():
        mm = Path(m["mmproj"]).stat().st_size / 2**20
    gpu_total = h["vram_total_mb"]
    # seconds per trial: load from NVMe + warm-up + ~1k-token prompt + 96 tokens; slower when it can't fit
    partial = gpu_total and (size_mb + mm) > gpu_total * 0.85
    per_trial = 8 + (size_mb + mm) / 1024 * (0.6 if h["gpus"] else 1.5) + (12 if partial or not h["gpus"] else 3)
    s = load_settings()
    return {"model": m["name"], "size_mb": round(size_mb + mm), "tuned": _tuned_info(m, h, server_version()),
            "gpu": h["gpu_name"], "vram_total_mb": gpu_total, "others_mb": max(0, others),
            "ram_total_mb": h["ram_total_mb"], "ram_avail_mb": h["ram_avail_mb"], "cpu_only": not h["gpus"],
            "vram_measured": h.get("vram_measured", True), "suggest": suggest_limit(h, size_mb + mm, others),
            "last_limit_gb": s.get("last_vram_limit_gb"), "mode": s.get("tune_mode", "balanced"),
            "advisor": s.get("advisor_model", "auto"), "per_trial_s": round(per_trial),
            "depths": {k: v for k, v in tuner.DEPTHS.items()}, "full_cap": tuner.FULL_CAP,
            "advisor_margin_mb": ADVISOR_MARGIN_MB}


ENGINE_VRAM = {"mb": 0}


def suggest_limit(h, model_mb, others_mb):
    """A starting VRAM (or RAM) limit for this PC, in GB, with the reason. The user still confirms it."""
    if not h["gpus"]:
        gb = max(2.0, (h["ram_total_mb"] - 6144) * 0.75 / 1024)          # leave the OS and apps 6 GB and a margin
        return {"gb": round(gb, 1), "why": f"75% of the RAM left after 6 GB for the OS and apps "
                                            f"({h['ram_total_mb'] / 1024:.0f} GB installed)"}
    free = h["vram_total_mb"] - max(0, others_mb) - ADVISOR_MARGIN_MB - 256
    gb = max(1.0, free / 1024)
    why = (f"{h['vram_total_mb'] / 1024:.1f} GB of VRAM minus {max(0, others_mb) / 1024:.1f} GB in use by other apps"
           f"{'' if h.get('vram_measured', True) else ' (estimated: this GPU does not report live use)'}, "
           f"the tuning advisor's share and a small safety margin")
    return {"gb": round(gb, 1), "why": why}
ADVISOR_MARGIN_MB = 150


class NeedsTune(Exception):
    pass


@app.post("/api/load")
async def load(req: Request):
    body = await req.json()
    mid, retune = body["id"], bool(body.get("retune"))
    m = models.get(mid)
    if not m:
        raise HTTPException(404, "Unknown model")
    limit_gb = body.get("vram_limit_gb")
    depth = body.get("depth") or "medium"
    if limit_gb is not None:
        try:
            limit_gb = float(limit_gb)
        except (TypeError, ValueError):
            raise HTTPException(400, "vram_limit_gb must be a number")
        if not (0.25 <= limit_gb <= 4096):
            raise HTTPException(400, "vram_limit_gb out of range")
    if depth not in tuner.DEPTHS:
        raise HTTPException(400, "depth must be short, medium, long or full")
    if body.get("mode") in tuner.MODE_TEXT:
        save_settings({"tune_mode": body["mode"]})

    def job(emit, cancel):
        s = load_settings()
        unload()
        h = hw(force=True)
        ver = server_version()
        spec = spec_args(m["path"], s)
        key = tuner.cache_key(m["path"], m.get("mmproj"), h, spec)
        entry = None if retune else tuner.cached(key)

        def do_tune():
            if limit_gb is None:
                raise NeedsTune()
            STATE.update(status="tuning", model=m, error=None)
            others = hardware.settle_vram(timeout=6) if h["gpus"] else 0
            limit_mb = limit_gb * 1024
            if h["gpus"]:
                headroom = h["vram_total_mb"] - others - limit_mb - ADVISOR_MARGIN_MB
                emit({"type": "log", "text": f"VRAM: {h['vram_total_mb']:,} MB total, other apps {others:,} MB, "
                                             f"model limit {limit_mb:,.0f} MB, left for the advisor "
                                             f"{max(0, headroom):,.0f} MB."})
                if others + limit_mb > h["vram_total_mb"] - 64:
                    emit({"type": "log", "text": "Your limit plus what other apps use is more than the card has, so "
                                                 "free VRAM will be the real limit."})
            else:
                headroom = 0
            save_settings({"last_vram_limit_gb": limit_gb})
            adv = advisor.Advisor(s, h, headroom, emit, cancel)
            try:
                emit({"type": "phase", "phase": "advisor"})
                adv.prepare()
                tuner.Tuner.check_cancel(cancel)
                emit({"type": "phase", "phase": "trials"})
                t = tuner.Tuner(m["path"], m.get("mmproj"), s, limit_mb, depth, adv, emit, cancel, others_mb=others)
                e = t.run()
            finally:
                adv.stop()
            e["others_mb"] = others
            tuner.store(key, e)
            return e

        try:
            if entry:
                emit({"type": "log", "text": f"Using saved tuning from {entry['tuned_at']}: {entry['desc']} "
                                             f"({entry['tg']} tok/s). Skipping trials."})
            else:
                entry = do_tune()

            prof = hapo.active(key)
            attempt = 0
            while True:
                use = prof.get("config") if prof else None
                if not use:
                    attempt += 1
                cfg = use or entry["config"]
                STATE.update(status="loading", model=m, error=None)
                emit({"type": "loading", "desc": prof["desc"] if use else entry["desc"],
                      "profile": prof.get("name") if use else None})
                before = hardware.settle_vram() if h["gpus"] else 0
                ENGINE.start(build_args(m["path"], cfg, LLAMA_PORT, m.get("mmproj"), final=True,
                                        settings=s, spec=spec))
                ok, why = ENGINE.wait_ready(900, cancel)
                if ok:
                    if not h["gpus"]:
                        ENGINE_VRAM["mb"] = 0
                    elif h.get("vram_measured", True):
                        ENGINE_VRAM["mb"] = max(0, (hardware.gpu_used_mb() or 0) - before)
                    else:
                        ENGINE_VRAM["mb"] = ENGINE.device_mb()
                    break
                if cancel.is_set():
                    ENGINE.stop()
                    raise tuner.Cancelled()
                tail = ENGINE.log_tail(25)
                ENGINE.stop()
                if use:                 # the chosen HAPO profile no longer loads: drop it, use the tuner's pick
                    hapo.failed(key, tail)
                    emit({"type": "log", "text": f"The {prof.get('name')} profile failed to load "
                                                 f"({tuner.classify_failure(tail)}), so Aero went back to the "
                                                 "tuner's pick and removed that profile choice."})
                    prof = None
                    continue
                if attempt == 1 and limit_gb is None:
                    raise NeedsTune()
                if attempt == 1:
                    emit({"type": "log", "text": "The tuned settings failed to load (other apps took VRAM or "
                                                 "llama.cpp changed). Re-tuning with your limit."})
                    entry = do_tune()
                    key = tuner.cache_key(m["path"], m.get("mmproj"), h, spec)
                    prof = None
                else:
                    STATE.update(status="error", error=tail)
                    raise RuntimeError("llama-server failed to start:\n" + tail)
        except Exception as e:      # NeedsTune asks for a tune; a failed or cancelled tune must not leave the UI on "tuning"
            if not isinstance(e, NeedsTune):
                if STATE["status"] in ("tuning", "loading"):
                    STATE.update(status="idle", model=None)
                raise
            STATE.update(status="idle", model=None)
            return {"needs_tune": True, "model_id": m["id"],
                    "reason": "This model needs tuning first." if not entry else
                              "The saved tuning no longer loads (VRAM in use by other apps, or llama.cpp changed)."}

        warning = None
        g = hardware.gpus()
        if g and all(x.get("measured") for x in g) and sum(x["used_mb"] for x in g) > sum(x["total_mb"] for x in g) - 200:
            warning = ("VRAM is almost full; other apps are using GPU memory, so generation may spill into "
                       "slow shared memory. Close them or re-tune.")
        desc = prof["desc"] if prof else entry["desc"]
        tg, pp = (prof["tg"], prof["pp"]) if prof else (entry["tg"], entry["pp"])
        STATE.update(status="ready", model=m, ctx=cfg["ctx"], vision=bool(m.get("mmproj")), desc=desc, cfg=dict(cfg),
                     remote=False,
                     tune={"tg": tg, "pp": pp, "at": entry["tuned_at"],
                           "trials": len(entry["trials"]), "limit_gb": round(entry.get("limit_mb", 0) / 1024, 2),
                           "depth": entry.get("depth"), "advisor": (entry.get("advisor") or {}).get("name"),
                           "profile": prof.get("profile") if prof else None,
                           "profile_name": prof.get("name") if prof else None, "key": key},
                     warning=warning, error=None)
        models.update(m["id"], last_used=time.time())
        return {"ready": True, "desc": desc, "ctx": cfg["ctx"], "tg": tg, "pp": pp, "warning": warning,
                "profile": prof.get("name") if prof else None}
    return stream_job(job)


# ---- chats -----------------------------------------------------------------------------------

def _chat_path(cid):
    return CHATS / (re.sub(r"[^\w-]", "", cid) + ".json")


@app.get("/api/chats")
def list_chats():
    out = []
    for p in CHATS.glob("*.json"):
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": c["id"], "title": c.get("title") or "New chat", "updated": c.get("updated", 0),
                        "continued": bool(c.get("carry")), "compacted": bool(c.get("compacted_into"))})
        except Exception:
            continue
    return sorted(out, key=lambda c: -c["updated"])


@app.get("/api/chats/{cid}")
def get_chat(cid: str):
    p = _chat_path(cid)
    if not p.exists():
        raise HTTPException(404)
    return json.loads(p.read_text(encoding="utf-8"))


@app.put("/api/chats/{cid}")
async def put_chat(cid: str, req: Request):
    c = await req.json()
    c["id"] = cid
    c["updated"] = time.time()
    old = _read_chat(cid)
    for k in ("compacted_into", "carry", "memorized_n"):     # server-owned fields survive a stale client copy
        if old.get(k) and not c.get(k):
            c[k] = old[k]
    _chat_path(cid).write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


@app.delete("/api/chats/{cid}")
def delete_chat(cid: str):
    p = _chat_path(cid)
    if p.exists():
        p.unlink()
    memory.delete_summary(cid)          # facts learned from it stay; its summary goes
    return {"ok": True}


def _read_chat(cid):
    try:
        return json.loads(_chat_path(cid).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_chat(c):
    _chat_path(c["id"]).write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")


def _part_title(title):
    """'Fix BIOS' -> 'Fix BIOS · part 2' -> 'Fix BIOS · part 3'."""
    m = re.match(r"^(.*) · part (\d+)$", title or "")
    return f"{m.group(1)} · part {int(m.group(2)) + 1}" if m else f"{title or 'Chat'} · part 2"


_mem_lock = asyncio.Lock()


@app.post("/api/chats/{cid}/compact")
async def compact_chat(cid: str):
    """Summarize a chat into memory and open a fresh chat that carries the summary."""
    if STATE["status"] != "ready":
        raise HTTPException(409, "Load a model first: compacting uses it to write the summary.")
    c = _read_chat(cid)
    if not c.get("messages"):
        raise HTTPException(400, "Nothing to compact yet.")
    if c.get("compacted_into") and _chat_path(c["compacted_into"]).exists():
        return {"new_chat": _read_chat(c["compacted_into"]), "facts_added": 0, "already": True}
    async with _mem_lock:
        res = await memory.summarize(STATE["url"], int(STATE["ctx"] or 8192), c["messages"], c.get("title") or "",
                                     c.get("carry"))
    new = {"id": uuid.uuid4().hex[:12], "title": _part_title(c.get("title")), "messages": [], "created": time.time(),
           "updated": time.time(), "carry": {"from": cid, "from_title": c.get("title") or "", "summary": res["summary"],
                                             "open_tasks": res["open_tasks"], "at": time.time()}}
    added = memory.store_result(cid, c.get("title") or "", res, next_chat=new["id"])
    _write_chat(new)
    c.update(compacted_into=new["id"], memorized_n=len(c["messages"]))
    _write_chat(c)
    return {"new_chat": new, "facts_added": added, "summary": res["summary"]}


_memorize_q: list = []


@app.post("/api/chats/{cid}/memorize")
async def memorize_chat(cid: str):
    """Queue a chat the user just left for summarizing into memory once the model is idle."""
    c = _read_chat(cid)
    users = sum(1 for m in c.get("messages") or [] if m.get("role") == "user")
    if users < 2 or c.get("compacted_into") or c.get("memorized_n") == len(c["messages"]):
        return {"queued": False}
    if cid not in _memorize_q:
        _memorize_q.append(cid)
        if len(_memorize_q) == 1:
            asyncio.create_task(_memorize_worker())
    return {"queued": True}


async def _memorize_worker():
    while _memorize_q:
        cid = _memorize_q[0]
        try:
            while agent.active() or STATE["status"] != "ready" or time.time() - _last_ping["chat"] < 20:
                await asyncio.sleep(3)
                if STATE["status"] in ("idle", "error"):
                    _memorize_q.clear()
                    return
            c = _read_chat(cid)
            if c.get("messages"):
                async with _mem_lock:
                    res = await memory.summarize(STATE["url"], int(STATE["ctx"] or 8192), c["messages"],
                                                 c.get("title") or "", c.get("carry"))
                memory.store_result(cid, c.get("title") or "", res)
                c2 = _read_chat(cid) or c
                c2["memorized_n"] = len(c["messages"])
                _write_chat(c2)
        except Exception as e:  # noqa: BLE001
            print("memorize failed:", cid, e, flush=True)
        finally:
            if _memorize_q and _memorize_q[0] == cid:
                _memorize_q.pop(0)


@app.get("/api/windows")
def list_windows():
    """Open app windows for the composer's App picker."""
    from .config import IS_WIN
    if not IS_WIN:
        return []
    from .tools import apps
    return [w for w in apps.windows() if not w["mine"]]


# ---- memory ----------------------------------------------------------------------------------

@app.get("/api/memory")
def get_memory():
    d = memory.all_items()
    return {"facts": sorted(d["facts"], key=lambda f: (not f.get("pinned"), -f["updated"])),
            "summaries": sorted(d["summaries"], key=lambda x: -x["created"]), "path": str(memory.PATH),
            "queue": len(_memorize_q)}


@app.post("/api/memory/profile")
async def memory_profile(req: Request):
    """What every model gets in its system prompt about the user (Settings > Memory > preview). The body may
    carry the unsaved profile text and toggles from the open Settings window."""
    b = await req.json()
    s = load_settings()
    for k in ("user_profile", "profile_enabled", "profile_budget_tokens", "memory_enabled"):
        if k in b:
            s[k] = b[k]
    ctx = int(STATE.get("ctx") or 32768)
    agent._profiles.pop("_preview", None)
    text, ids = agent.profile_for(s, "_preview", ctx)
    return {"text": text, "tokens": len(text) // 3, "facts": len(ids), "ctx": ctx}


@app.get("/api/memory/search")
def search_memory(q: str):
    return memory.search(q, 20)


@app.post("/api/memory")
async def add_memory(req: Request):
    b = await req.json()
    try:
        f, how = memory.add_fact(b.get("text", ""), b.get("kind") or "user", "manual", bool(b.get("pinned")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"fact": f, "how": how}


@app.put("/api/memory/{fid}")
async def edit_memory(fid: str, req: Request):
    b = await req.json()
    f = memory.update_fact(fid, text=b.get("text"), kind=b.get("kind"), pinned=b.get("pinned"))
    if not f:
        raise HTTPException(404)
    return f


@app.delete("/api/memory/{fid}")
def del_memory(fid: str):
    return {"ok": memory.delete_fact(fid)}


@app.delete("/api/memory/summary/{cid}")
def del_memory_summary(cid: str):
    memory.delete_summary(cid)
    return {"ok": True}


@app.post("/api/chat")
async def chat(req: Request):
    body = await req.json()
    if STATE["status"] != "ready" and not vram_policy.holding():
        raise HTTPException(409, "No model loaded")
    _last_ping["chat"] = time.time()
    opts = body.get("opts") or {}
    if (opts.get("loop") or {}).get("stop"):
        stats.SESSION["loop"]["active"] = False
    gen = pipeline.run_turn(body.get("chat_id") or uuid.uuid4().hex, body["messages"], load_settings(), STATE,
                            carry=body.get("carry"), title=body.get("title") or "", opts=opts,
                            agent_meta=body.get("agent"))
    return StreamingResponse(gen, media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/api/loop/stop")
def stop_loop():
    """The UI ended a forever-loop (Stop button, max rounds reached, or an error)."""
    stats.SESSION["loop"]["active"] = False
    return {"ok": True}


@app.post("/api/approve")
async def approve(req: Request):
    b = await req.json()
    return {"ok": agent.resolve_approval(b["call_id"], b["decision"], b.get("chat_id"), b.get("category"))}


@app.post("/api/stop")
async def stop_chat(req: Request):
    b = await req.json()
    agent.stop(b.get("chat_id"))
    return {"ok": True}


@app.post("/api/stop_all")
def stop_everything():
    """Stop every running chat: the Stop button on the "is controlling" banner over another app."""
    return {"ok": True, "stopped": agent.stop_all()}


_TITLE_PROMPT = ("A chat starts with the message below. Reply with JSON only: {\"title\": a 3-6 word title for the "
                 "chat, \"agent\": a 1-3 word job title for whoever does this task, naming what it does (e.g. "
                 "\"Photo Renamer\", \"Desktop Organizer\", \"Bug Fixer\", \"Trip Planner\")}\n\nMessage:\n")


def parse_title(t):
    """{"title", "agent"} from the model's reply: JSON, or a plain title line when it ignored the format."""
    t = re.sub(r"<think>.*?</think>", "", t or "", flags=re.S).strip()
    m = re.search(r"\{.*\}", t, flags=re.S)
    if m:
        try:
            j = json.loads(m.group(0))
            if isinstance(j, dict):
                title = str(j.get("title") or "").strip().strip('"')[:60]
                return {"title": title or None, "agent": agents.clean_name(j.get("agent")) or None}
        except ValueError:
            pass
    title = re.search(r'"?title"?\s*:\s*"([^"]+)', t, flags=re.I)
    agent_ = re.search(r'"?agent"?\s*:\s*"([^"]+)', t, flags=re.I)
    line = title.group(1) if title else next((x for x in t.splitlines() if x.strip()), "").strip().strip('"')
    return {"title": line.strip()[:60] or None, "agent": agents.clean_name(agent_.group(1)) if agent_ else None}


@app.post("/api/title")
async def make_title(req: Request):
    """Ask the loaded model for a 3-6 word chat title and a job title for the chat's agent (non-streaming, tiny)."""
    import httpx
    b = await req.json()
    text = b.get("text", "")
    fallback = {"title": None, "agent": agents.name_from_text(text) if text else None}
    if STATE["status"] != "ready":
        return fallback
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(ENGINE.url + "/v1/chat/completions", json={
                "messages": [{"role": "user", "content": _TITLE_PROMPT + text[:1500]}],
                "max_tokens": 60, "temperature": 0.3, "chat_template_kwargs": {"enable_thinking": False}})
            out = parse_title(r.json()["choices"][0]["message"]["content"])
    except Exception:
        return fallback
    out["agent"] = out["agent"] or fallback["agent"]
    if b.get("chat_id") and out["agent"]:
        agents.set_name(b["chat_id"], out["agent"])
    return out


# ---- uploads ---------------------------------------------------------------------------------

@app.post("/api/upload")
async def upload(req: Request, name: str):
    data = await req.body()
    if len(data) > 512 * 2**20:
        raise HTTPException(413, "File too large (512 MB max)")
    return await asyncio.to_thread(attachments.save_upload, name, data)


@app.get("/api/uploads/{uid}")
def get_upload(uid: str):
    p = attachments.file_path(uid)
    if not p or not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/api/uploads/{uid}/text")
def get_upload_text(uid: str):
    return {"text": attachments.text_of(uid)[:200000]}


# ---- tools / MCP -----------------------------------------------------------------------------

@app.get("/api/tools")
def list_tools():
    s = load_settings()
    by = {}
    for t in tools.REGISTRY.values():
        by.setdefault(t.category, []).append({"name": t.name, "description": t.description[:400]})
    by.setdefault("meta", []).append({"name": "load_tools", "description": "Loads more tools when the router's "
                                      "selection is not enough for the task (Aero built-in)."})
    by.setdefault("cloud", []).append({"name": "submit_review", "description": "How Claude Fable 5.1 hands back its "
                                       "verdict (ok / minor / major), the problems it found and a fix plan."})
    by["claude_code"] = [{"name": n, "description": d} for n, d in claude_code.CC_DESCRIPTIONS.items()]
    return {"categories": {**tools.CATEGORIES, "meta": "Aero built-ins", "cloud": "Cloud review",
                           "claude_code": "Claude Code (used by Claude on your plan)"}, "tools": by,
            "policy": s["tool_policy"], "mcp": mcp_client.status()}


@app.get("/api/mcp")
def get_mcp():
    mcp_client.read_config()
    return {"text": mcp_client.CONFIG.read_text(encoding="utf-8"), "status": mcp_client.status()}


@app.put("/api/mcp")
async def put_mcp(req: Request):
    b = await req.json()
    try:
        json.loads(b["text"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Invalid JSON: {e}")
    mcp_client.CONFIG.write_text(b["text"], encoding="utf-8")
    await asyncio.to_thread(mcp_client.start_all)
    return {"status": mcp_client.status()}


@app.post("/api/open_path")
async def open_path(req: Request):
    b = await req.json()
    target = {"data": str(DATA), "models": load_settings()["models_dir"], "logs": str(LOGS),
              "skills": str(skills.SKILLS_DIR), "training": str(DATA / "training"), "mods": str(mods.MODS)}.get(b.get("what"), "")
    opened = False
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        opened = osinfo.open_with_default(target)
    return {"path": target, "opened": bool(opened)}


@app.post("/api/bye")
def bye():
    """The app window is closing (or reloading: a reload pings again within a second)."""
    _last_ping["bye"] = time.time()
    return {"ok": True}


# ---- restart, updates, mods ----------------------------------------------------------------

def _headless():
    import sys
    return "--no-window" in sys.argv


def _spawn_aero():
    """Start a fresh Aero (detached) that waits for this one to exit and reuses the open window."""
    import subprocess
    import sys
    args = [sys.executable, "-m", "aero", "--reopen"] + (["--no-window"] if _headless() else [])
    env = {k: v for k, v in os.environ.items() if k != "AERO_MOD_RETRY"}
    if osinfo.IS_WIN:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x08) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        subprocess.Popen(args, cwd=str(PKG_DIR.parent), env=env, creationflags=flags, close_fds=True)
    else:
        subprocess.Popen(args, cwd=str(PKG_DIR.parent), env=env, start_new_session=True, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


RUN = {"main": False}                 # True when __main__ watches _last_ping["exit"] (it does in normal use)


def _exit_then(reason, then):
    """Stop Aero, then run `then` (a fresh Aero, or the updater's installer). __main__ does it once the reply to
    this request is out; without __main__ (Aero served some other way) it happens here."""
    _last_ping["then"] = then
    _last_ping["exit"] = reason
    if not RUN["main"]:
        def later():
            time.sleep(1.0)
            then()
            shutdown()
        threading.Thread(target=later, daemon=True).start()


@app.post("/api/restart")
def restart():
    if _last_ping.get("exit"):
        return {"ok": True, "already": _last_ping["exit"]}
    _exit_then("a restart", _spawn_aero)
    return {"ok": True}


@app.get("/api/update")
def get_update():
    return updater.public()


@app.post("/api/update/check")
def check_update():
    return updater.public() if updater.check(force=True) else {}


@app.post("/api/update/skip")
async def skip_update(req: Request):
    b = await req.json()
    v = str(b.get("version") or "")
    save_settings({"update_skip": v})
    updater.STATE["skipped"] = bool(v) and v == updater.STATE.get("latest")
    return updater.public()


@app.post("/api/update/install")
async def install_update():
    """Download and verify the release (streamed progress), then hand over to its installer and stop Aero."""
    if _last_ping.get("exit"):
        raise HTTPException(409, "Aero is already stopping.")

    def job(emit, cancel):
        updater.STATE.update(phase="download", message="")
        try:
            root = updater.prepare(lambda ev: (updater.STATE.update(**ev), emit({"type": "progress", **ev})))
        except Exception as e:
            updater.STATE.update(phase="error", message=str(e))
            raise
        if cancel.is_set():
            raise tuner.Cancelled()
        go = updater.handoff(root, headless=_headless())
        updater.STATE.update(phase="installing", message=f"Installing Aero {updater.STATE.get('latest')}")
        _exit_then("an update", go)
        return {"version": updater.STATE.get("latest"), "installing": True}
    return stream_job(job)


def _mod_or_404(mid):
    try:
        return mods.get(mid)
    except (KeyError, ValueError):
        raise HTTPException(404, "No such mod")


@app.get("/api/mods")
def list_mods():
    return {"mods": mods.listing(), "app_dir": str(mods.APP_DIR), "boot": mods._read(mods.BOOT, None)}


@app.post("/api/mods")
async def create_mod(req: Request):
    b = await req.json()
    prompt = (b.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "Describe the change you want.")
    rec = await asyncio.to_thread(mods.new, prompt, b.get("chat_id"))
    return mods.public(rec)


@app.get("/api/mods/{mid}")
def get_mod(mid: str):
    rec = _mod_or_404(mid)
    out = mods.public(rec)
    if rec["status"] == "draft":
        out["files"] = mods.changes(mid)
        out["checked"] = mods.checked(rec)
    return out


@app.get("/api/mods/{mid}/diff")
def mod_diff(mid: str, path: str = ""):
    rec = _mod_or_404(mid)
    if rec["status"] == "draft":
        return {"diff": mods.diff_text(mid, path or None)}
    p = mods._dir(mid) / "patch.diff"
    return {"diff": p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""}


@app.post("/api/mods/{mid}/check")
async def check_mod(mid: str):
    _mod_or_404(mid)
    res = await asyncio.to_thread(mods.check, mid)
    return {**mods.public(mods.get(mid)), "checks": res}


def _after_mod_change(res):
    if res.get("restart"):
        _exit_then("a mod change", _spawn_aero)
    return res


@app.post("/api/mods/{mid}/apply")
async def apply_mod(mid: str):
    _mod_or_404(mid)
    try:
        res = await asyncio.to_thread(mods.apply, mid)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return _after_mod_change(res)


@app.post("/api/mods/{mid}/off")
async def mod_off(mid: str):
    _mod_or_404(mid)
    return _after_mod_change(await asyncio.to_thread(mods.undo, mid))


@app.post("/api/mods/{mid}/on")
async def mod_on(mid: str):
    _mod_or_404(mid)
    try:
        res = await asyncio.to_thread(mods.turn_on, mid)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return _after_mod_change(res)


@app.delete("/api/mods/{mid}")
async def delete_mod(mid: str):
    _mod_or_404(mid)
    return _after_mod_change(await asyncio.to_thread(mods.delete, mid))


@app.get("/api/loop/journal")
def loop_journal(task: str = "", key: str = ""):
    if key and not task:
        p = looplog.LOOPS / f"{re.sub(r'[^0-9a-f]', '', key)}.json"
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            j = {"entries": []}
    else:
        j = looplog.load(task)
    return {**j, "summary": looplog.summary(j)}


@app.delete("/api/loop/journal")
def clear_loop_journal(task: str = "", key: str = ""):
    if key and not task:
        (looplog.LOOPS / f"{re.sub(r'[^0-9a-f]', '', key)}.json").unlink(missing_ok=True)
    else:
        looplog.clear(task)
    return {"ok": True}


@app.get("/api/experience")
def get_experience():
    """Shared learning: notes every model wrote, per-model numbers and tool results (experience.py)."""
    return experience.overview()


@app.delete("/api/experience/notes/{nid}")
def delete_experience_note(nid: str):
    if not experience.delete_note(nid):
        raise HTTPException(404, "No such note")
    return {"ok": True}


@app.delete("/api/experience")
def clear_experience():
    experience.clear()
    return {"ok": True}


@app.post("/api/shutdown")
def shutdown():
    ENGINE.stop()
    router.stop()
    mcp_client.stop_all()
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return {"ok": True}


# ---- dashboard -------------------------------------------------------------------------------

@app.get("/api/stats")
def get_stats():
    rs = router.status()
    snap = stats.snapshot({"main": ENGINE.proc.pid if ENGINE.running() else None, "router": rs.get("pid")})
    d = memory.all_items()
    ses = dict(stats.SESSION)
    return {"sys": snap, "session": ses, "router": rs, "cloud": {**cloud.usage_summary(), "key": cloud.has_key(),
            "budget": load_settings().get("cloud_daily_budget_usd")},
            "engine": {"status": STATE["status"], "model": (STATE.get("model") or {}).get("name"), "ctx": STATE.get("ctx"),
                       "vision": STATE.get("vision"), "desc": STATE.get("desc"), "tune": STATE.get("tune"),
                       "vram_mb": ENGINE_VRAM.get("mb")},
            "memory": {"facts": sum(1 for f in d["facts"] if f.get("kind") != "lesson"),
                       "lessons": sum(1 for f in d["facts"] if f.get("kind") == "lesson"), "chats": len(d["summaries"])},
            "mcp": {k: v.get("state") for k, v in mcp_client.status().items()},
            "tools": len(tools.REGISTRY), "skills": len(skills.enabled()), "agents": agents.listing(),
            "remote": _remote_brief()}


def _remote_brief():
    r = _REMOTE.get("runner")
    if r is None:
        return {}
    snap = r.snapshot or {}
    return {"policy": r.policy.public(), "detector": {k: snap.get(k) for k in ("state", "counted", "providers")},
            "vram": _vram() if r.policy.state != "NORMAL" else None}


# ---- router ----------------------------------------------------------------------------------

@app.get("/api/router")
def get_router():
    from . import catalog
    rec = catalog.recommend_router(hw())["id"]
    return {"status": router.status(), "config": router.config(),
            "candidates": [{**c, "recommended": c["id"] == rec} for c in catalog.ROUTERS],
            "cores": router.physical_cores()}


@app.get("/api/catalog")
def get_catalog():
    """Both lists, with each main model fitted to this PC (quant, where it runs) and the pick for this PC."""
    from . import catalog
    h = hw()
    rec, _ = catalog.recommend_main(h)
    rrec = catalog.recommend_router(h)["id"]
    return {"routers": [{**c, "recommended": c["id"] == rrec} for c in catalog.ROUTERS],
            "main": [{**m, "plan": catalog.plan(m, h), "recommended": m["id"] == rec["id"]} for m in catalog.MAIN_MODELS],
            "hardware": hardware.summary(h)}


@app.post("/api/router/restart")
def restart_router():
    router.start()
    return {"ok": True}


@app.post("/api/router/benchmark")
def bench_router():
    path = router.model_path()
    if not path:
        raise HTTPException(409, "No router model chosen yet.")

    def job(emit, cancel):
        router.stop()
        res = router.benchmark(path, lambda t: emit({"type": "log", "text": t}), cancel=cancel)
        router.save_config({"threads": res["threads"], "batch_threads": res["batch_threads"], "bench": res})
        router.start()
        return res
    return stream_job(job)


@app.post("/api/router/choose")
async def choose_router(req: Request):
    from . import catalog
    b = await req.json()
    cand = next((c for c in catalog.ROUTERS if c["id"] == b.get("id")), None)
    if not cand:
        raise HTTPException(404, "Unknown router model")

    def job(emit, cancel):
        root = Path(load_settings()["models_dir"]) / router.ROUTER_DIR
        parts, _ = catalog.resolve(cand)
        emit({"type": "log", "text": f"Downloading {cand['name']} ({parts[0].split('/')[-1]})…"})
        paths = hf.download(cand["repo"], parts, emit, cancel, root=root)
        router.stop()
        res = router.benchmark(paths[0], lambda t: emit({"type": "log", "text": t}), cancel=cancel)
        router.save_config({"id": cand["id"], "name": cand["name"], "path": str(paths[0]), "threads": res["threads"],
                            "batch_threads": res["batch_threads"], "bench": res})
        save_settings({"router_model": ""})
        router.start()
        return res
    return stream_job(job)


# ---- Claude (cloud) --------------------------------------------------------------------------

@app.get("/api/cloud")
def get_cloud():
    s = load_settings()
    return {"key": cloud.key_status(), "usage": cloud.usage_summary(), "prices": cloud.PRICES, "names": cloud.NAMES,
            "backend": cloud.backend(s), "backend_setting": s.get("cloud_backend") or "auto"}


@app.get("/api/claude/plan")
async def get_plan():
    st = await asyncio.to_thread(claude_code.status)
    if bool(st.get("loggedIn")) != bool(load_settings().get("claude_plan_signed_in")):
        save_settings({"claude_plan_signed_in": bool(st.get("loggedIn"))})
    return st


@app.post("/api/claude/plan/login")
def plan_login():
    try:
        return claude_code.login()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)[:400])


@app.post("/api/claude/plan/logout")
async def plan_logout():
    r = await asyncio.to_thread(claude_code.logout)
    save_settings({"claude_plan_signed_in": False})
    return r


@app.put("/api/cloud/key")
async def put_cloud_key(req: Request):
    b = await req.json()
    cloud.set_key(b.get("key") or "")
    return cloud.key_status()


@app.post("/api/cloud/test")
async def test_cloud():
    try:
        ids = await cloud.test_key()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"{type(e).__name__}: {e}"[:400])
    want = [load_settings().get("review_model"), load_settings().get("fix_model")]
    return {"ok": True, "models": ids, "missing": [m for m in want if m not in ids]}


# ---- ChatGPT (cloud) -------------------------------------------------------------------------

@app.get("/api/chatgpt")
def get_chatgpt():
    from . import chatgpt
    s = load_settings()
    return {"key": chatgpt.key_status(), "prices": chatgpt.PRICES, "names": chatgpt.NAMES,
            "backend": chatgpt.backend(s), "backend_setting": s.get("chatgpt_backend") or "auto",
            "today_usd": chatgpt.spent_today()}


@app.get("/api/chatgpt/plan")
async def get_chatgpt_plan():
    from . import chatgpt
    st = await asyncio.to_thread(chatgpt.status)
    signed = bool(st.get("loggedIn")) and st.get("method") == "chatgpt"
    if "offline" not in st and signed != bool(load_settings().get("chatgpt_plan_signed_in")):
        save_settings({"chatgpt_plan_signed_in": signed})
    return st


@app.post("/api/chatgpt/plan/login")
async def chatgpt_login(req: Request):
    from . import chatgpt
    try:
        b = await req.json()
    except Exception:  # noqa: BLE001
        b = {}
    try:
        return chatgpt.login(device=bool((b or {}).get("device")))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)[:400])


@app.post("/api/chatgpt/plan/logout")
async def chatgpt_logout():
    from . import chatgpt
    r = await asyncio.to_thread(chatgpt.logout)
    save_settings({"chatgpt_plan_signed_in": False})
    return r


@app.put("/api/chatgpt/key")
async def put_chatgpt_key(req: Request):
    from . import chatgpt
    b = await req.json()
    chatgpt.set_key(b.get("key") or "")
    return chatgpt.key_status()


@app.post("/api/chatgpt/test")
async def test_chatgpt():
    from . import chatgpt
    try:
        ids = await chatgpt.test_key()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"{type(e).__name__}: {e}"[:400])
    s = load_settings()
    want = [s.get("gpt_review_model"), s.get("gpt_fix_model")]
    return {"ok": True, "models": [i for i in ids if i.startswith("gpt-6")] or ids[:20],
            "missing": [m for m in want if m not in ids]}


# ---- GitHub ----------------------------------------------------------------------------------

@app.get("/api/github")
def get_github():
    return github.status()


@app.post("/api/github/connect")
async def connect_github(req: Request):
    b = await req.json()
    try:
        if b.get("cli"):
            info = await asyncio.to_thread(github.connect_with_cli)
        else:
            info = await asyncio.to_thread(github.connect, b.get("token") or "")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)[:400])
    return {"user": info, "status": github.status()}


@app.post("/api/github/disconnect")
async def disconnect_github():
    await asyncio.to_thread(github.disconnect)
    return github.status()


# ---- MCP import / OAuth / skills -------------------------------------------------------------

@app.get("/api/mcp/importable")
def mcp_importable():
    return {"servers": mcp_import.discover()}


@app.post("/api/mcp/import")
async def mcp_do_import(req: Request):
    b = await req.json()
    added = mcp_import.import_servers(b.get("items") or [])
    for n in added:
        await asyncio.to_thread(mcp_client.start_one, n)
    return {"added": added, "status": mcp_client.status()}


@app.post("/api/mcp/{name}/restart")
async def mcp_restart(name: str):
    await asyncio.to_thread(mcp_client.start_one, name)
    return {"status": mcp_client.status()}


@app.post("/api/mcp/{name}/auth")
async def mcp_auth(name: str):
    try:
        url = await asyncio.to_thread(mcp_client.oauth_begin, name)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)[:400])
    if os.name == "nt":
        os.startfile(url)              # the user's default browser, where they are already signed in
    return {"url": url, "opened": os.name == "nt"}


@app.post("/api/mcp/{name}/signout")
async def mcp_signout(name: str):
    mcp_client.oauth_forget(name)
    await asyncio.to_thread(mcp_client.start_one, name)
    return {"status": mcp_client.status()}


@app.get("/oauth/callback")
async def oauth_callback(code: str = "", state: str = "", error: str = "", error_description: str = ""):
    from fastapi.responses import HTMLResponse
    if error:
        msg, ok = f"Sign-in failed: {error} {error_description}", False
    else:
        try:
            name = await asyncio.to_thread(mcp_client.oauth_finish, state, code)
            msg, ok = f"Signed in to {name}. You can close this window and go back to Aero.", True
        except Exception as e:  # noqa: BLE001
            msg, ok = str(e), False
    color = "#18c6c0" if ok else "#ff6b6b"
    html = (f"<!doctype html><meta charset=utf-8><title>Aero</title><body style=\"font:16px Segoe UI,sans-serif;"
            f"background:linear-gradient(#0b2a4a,#0e4f7a);color:#eaf6ff;display:grid;place-items:center;height:100vh;margin:0\">"
            f"<div style=\"padding:28px 36px;border-radius:18px;background:rgba(255,255,255,.12);border:1px solid "
            f"rgba(255,255,255,.3)\"><b style=\"color:{color}\">{'✓' if ok else '✕'}</b> {msg}</div>"
            f"<script>setTimeout(()=>window.close(),2500)</script>")
    return HTMLResponse(html)


@app.get("/api/skills")
def get_skills():
    return {"skills": skills.catalog(force=True), "dir": str(skills.SKILLS_DIR)}


@app.put("/api/skills/{name}")
async def put_skill(name: str, req: Request):
    b = await req.json()
    dis = set(load_settings().get("skills_disabled") or [])
    (dis.discard if b.get("enabled") else dis.add)(name)
    save_settings({"skills_disabled": sorted(dis)})
    return {"skills": skills.catalog(force=True)}


# ---- local-only status ------------------------------------------------------------------------

@app.get("/api/local_status")
def local_status():
    rs = router.status()
    return localonly.status({"llama-server (main model)": ENGINE.proc.pid if ENGINE.running() else None,
                             "llama-server (router)": rs.get("pid")})


# ---- HAPO profiles ------------------------------------------------------------------------------

def _tune_key(m, s, h):
    return tuner.cache_key(m["path"], m.get("mmproj"), h, spec_args(m["path"], s))


def _hapo_ctx(mid):
    m = models.get(mid)
    if not m:
        raise HTTPException(404, "Unknown model")
    s, h = load_settings(), hw()
    key = _tune_key(m, s, h)
    return m, s, h, key, tuner.cached(key)


@app.get("/api/hapo/{mid}")
def hapo_get(mid: str):
    m, s, h, key, entry = _hapo_ctx(mid)
    fp = hapo.fingerprint(h, m["path"])
    out = {"model": {"id": m["id"], "name": m["name"]}, "fingerprint": fp, "key": key, "tuned": bool(entry),
           "loaded": bool(STATE.get("model") and STATE["model"].get("id") == mid and STATE["status"] == "ready")}
    if entry:
        out.update(hapo.profiles(entry, h, key))
        act = out.get("active")
        out["stale"] = bool(act and act.get("fingerprint") and act["fingerprint"] != fp["id"])
        out["llama_changed"] = bool(entry.get("llama_version") and entry["llama_version"] != fp["engine"]["version"])
    return out


@app.post("/api/hapo/apply")
async def hapo_apply(req: Request):
    b = await req.json()
    m, s, h, key, entry = _hapo_ctx(b.get("id"))
    if not entry:
        raise HTTPException(409, "Tune this model first (Quick Tune or Deep Tune).")
    meta = entry.get("meta") or gguf.summarize(Path(m["path"]))
    space = tuner.Space(meta, h, s, bool(h["gpus"]))
    try:
        act = hapo.apply(key, b.get("profile"), entry, h, space.server_cfg, hapo.fingerprint(h)["id"])
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"active": act}


@app.post("/api/hapo/restore")
async def hapo_restore(req: Request):
    b = await req.json()
    m, s, h, key, entry = _hapo_ctx(b.get("id"))
    try:
        return {"active": hapo.restore(key)}
    except ValueError as e:
        raise HTTPException(409, str(e))


# ---- benchmarks ---------------------------------------------------------------------------------

def _bench_meta(kind, depth, scenery):
    m = STATE.get("model") or {}
    tune = STATE.get("tune") or {}
    fp = hapo.fingerprint(hw(force=True), m.get("path"))
    return {"kind": kind, "depth": depth, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "scenery": scenery,
            "model": {"id": m.get("id"), "name": m.get("name"), "file": Path(m["path"]).name if m.get("path") else None},
            "desc": STATE.get("desc"), "ctx": STATE.get("ctx"), "profile": tune.get("profile_name"),
            "fingerprint": fp, "settings": {k: load_settings().get(k) for k in ("temperature", "top_p", "top_k",
                                                                              "spec_mode", "strict_offline")}}


def _bench_ready():
    if STATE["status"] != "ready" or not ENGINE.running():
        raise HTTPException(409, "Load a model first.")
    if agent.active():
        raise HTTPException(409, "A reply is still being written. Wait for it to finish, then run the benchmark.")


@app.post("/api/bench/run")
async def bench_run(req: Request):
    b = await req.json()
    depth = b.get("suite") or "quick"
    if depth not in ("quick", "deep", "decode"):
        raise HTTPException(400, "suite must be quick, deep or decode")
    _bench_ready()
    scenery = b.get("scenery") or "unknown"

    def job(emit, cancel):
        meta = _bench_meta("model", depth, scenery)
        runner = bench.Bench(ENGINE.url, STATE.get("ctx"), load_settings(), emit, cancel,
                             pid=ENGINE.proc.pid if ENGINE.proc else None, gpu=bool(hw()["gpus"]))
        try:
            res = runner.run(depth)
        except bench.Cancelled:
            raise tuner.Cancelled()
        report = {**meta, "results": res}
        if b.get("save", True):
            bench.save(report)
        return report
    return stream_job(job)


@app.post("/api/bench/router")
async def bench_router_suite(req: Request):
    if not router.ready():
        raise HTTPException(409, "The router is not running. Turn it on in Settings > Router.")
    if agent.active():
        raise HTTPException(409, "A reply is still being written. Wait for it to finish.")

    def job(emit, cancel):
        meta = _bench_meta("router", "suite", "unknown")
        meta["router"] = {k: router.status().get(k) for k in ("model", "threads")}
        try:
            res = bench.router_suite(lambda hist, cat: router.decide(hist, cat), pipeline.tool_catalog(load_settings()),
                                     emit, cancel)
        except bench.Cancelled:
            raise tuner.Cancelled()
        report = {**meta, "results": {"router": res}}
        bench.save(report)
        return report
    return stream_job(job)


@app.post("/api/bench/scenery")
async def bench_scenery(req: Request):
    """The Performance Lab measures decode speed and frame times with the scenery on Full and then Off (the page
    switches the scenery itself, so it posts both halves here to be saved as one report)."""
    b = await req.json()
    full, off = b.get("full") or {}, b.get("off") or {}
    f, o = (full.get("decode_tps") or {}).get("median"), (off.get("decode_tps") or {}).get("median")
    report = {**_bench_meta("scenery", "ab", "full vs off"),
              "results": {"full": full, "off": off, "delta_pct": round((f - o) / o * 100, 2) if f and o else None}}
    bench.save(report)
    return report


@app.get("/api/bench")
def bench_list():
    return {"reports": bench.listing()}


@app.get("/api/bench/{rid}")
def bench_get(rid: str, fmt: str = "json", download: int = 0):
    try:
        r = bench.load(rid)
    except (OSError, ValueError):
        raise HTTPException(404, "No such report")
    if fmt == "md":
        from fastapi.responses import Response
        return Response(bench.to_markdown(r), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{rid}.md"'} if download else {})
    if download:
        return JSONResponse(r, headers={"Content-Disposition": f'attachment; filename="{rid}.json"'})
    return r


@app.get("/api/lab/report")
def lab_report(mid: str, fmt: str = "md"):
    """One file with the fingerprint, every profile and the latest benchmark of each kind for a model."""
    from fastapi.responses import Response
    info = hapo_get(mid)
    latest = {}
    for row in bench.listing(200):
        if row.get("model") == Path(models.get(mid)["path"]).name and row["kind"] not in latest:
            try:
                latest[row["kind"]] = bench.load(row["id"])
            except (OSError, ValueError):
                pass
    doc = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "lab": info, "benchmarks": latest}
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", info["model"]["name"])[:50]
    if fmt == "json":
        return JSONResponse(doc, headers={"Content-Disposition": f'attachment; filename="aero-lab-{name}.json"'})
    fp = info["fingerprint"]
    L = [f"# Aero Performance Lab: {info['model']['name']}", "", f"Generated {doc['generated']}.", "",
         "## Fingerprint", "", "| | |", "|---|---|"]
    for k, v in fp["hardware"].items():
        L.append(f"| {k} | {v} |")
    for k, v in fp["engine"].items():
        L.append(f"| llama.cpp {k} | {v} |")
    for k, v in (fp.get("model") or {}).items():
        L.append(f"| model {k} | {v} |")
    L += ["", "## Profiles", ""]
    if not info.get("tuned"):
        L.append("Not measured: this model has not been tuned on this PC yet.")
    else:
        L += ["| Profile | Config | Decode tok/s | Prompt tok/s | VRAM MB | Rule |", "|---|---|---|---|---|---|"]
        for p in info["profiles"]:
            if p["measured"]:
                L.append(f"| {p['name']}{' (active)' if p['active'] else ''} | {p['desc']} | {p['tg']} | {p['pp']} | "
                         f"{p.get('mem_mb') or 'n/a'} | {p['rule']} |")
            else:
                L.append(f"| {p['name']} | not measured | | | | {p['why']} |")
    for kind, r in latest.items():
        L += ["", "---", "", bench.to_markdown(r)]
    return Response("\n".join(L) + "\n", media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="aero-lab-{name}.md"'})


# ---- v1.1: questions, tasks, apps, Remote Mode ------------------------------------------------------------

@app.get("/api/questions")
def list_questions(chat_id: str = ""):
    return {"questions": [clarifications.public(q) for q in clarifications.pending(chat_id or None)]}


@app.post("/api/answer")
async def answer_question(req: Request):
    b = await req.json()
    try:
        q = clarifications.answer(str(b.get("question_id") or ""), b.get("answer"))
    except KeyError:
        raise HTTPException(404, "No such question")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"question": clarifications.public(q)}


@app.post("/api/questions/{qid}/cancel")
def cancel_question(qid: str):
    clarifications.cancel(qid, "dismissed by the user")
    return {"ok": True}


@app.get("/api/tasks/{cid}")
def chat_tasks(cid: str):
    return {"graphs": task_graph.latest_for_chat(cid), "questions": [clarifications.public(q) for q in
                                                                     clarifications.pending(cid)]}


@app.get("/api/resources")
def resource_state():
    return {"locks": resources.snapshot(), "foreground_grants": input_guard.grants(),
            "input_owner": input_guard.owner_of_input(), "idle_ms": input_guard.idle_ms()}


@app.get("/api/apps")
def apps_overview(q: str = ""):
    out = app_registry.overview()
    if q:
        out["matches"] = [{k: v for k, v in c.items() if k != "record"} for c in app_registry.resolve(q)]
    return out


@app.post("/api/apps/scan")
async def apps_scan():
    await asyncio.to_thread(app_registry.scan, True)
    return app_registry.overview()


@app.put("/api/apps/alias")
async def apps_alias(req: Request):
    b = await req.json()
    alias = str(b.get("alias") or "").strip()
    if not alias or len(alias) > 60:
        raise HTTPException(400, "An alias is a short name, 1 to 60 characters.")
    app_registry.set_alias(alias, str(b.get("app_id") or ""))
    return app_registry.overview()


@app.post("/api/apps/{aid}/disable")
async def apps_disable(aid: str, req: Request):
    b = await req.json()
    app_registry.set_disabled(aid, bool(b.get("off", True)))
    return app_registry.overview()


@app.delete("/api/apps/learned")
def apps_forget(app_id: str = ""):
    app_registry.forget_learned(app_id or None)
    return app_registry.overview()


def _vram():
    g = hardware.gpus()
    meas = [x for x in g if x.get("measured")]
    if not g or len(meas) != len(g):
        return None
    return {"used_mb": sum(x["used_mb"] for x in g), "free_mb": sum(x["free_mb"] for x in g),
            "total_mb": sum(x["total_mb"] for x in g),
            "per_gpu": [{"index": x["index"], "name": x["name"], "used_mb": x["used_mb"], "free_mb": x["free_mb"],
                         "total_mb": x["total_mb"]} for x in g]}


def _engine_reload(cfg):
    """Restart the loaded model with another llama-server config (Remote Mode). Blocking. (ok, info)."""
    m = STATE.get("model")
    if not m:
        return False, {"error": "no model loaded"}
    if _job["busy"]:
        return False, {"error": "another download, tune or load is running"}
    _job["busy"] = True
    t0 = time.time()
    try:
        s = load_settings()
        spec = spec_args(m["path"], s)
        h = hw(force=True)
        STATE.update(status="loading", error=None)
        ENGINE.stop()
        before = hardware.settle_vram() if h["gpus"] else 0
        ENGINE.start(build_args(m["path"], cfg, LLAMA_PORT, m.get("mmproj"), final=True, settings=s, spec=spec))
        ok, why = ENGINE.wait_ready(900)
        try:
            log = ENGINE.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            log = ""
        if not ok:
            tail = ENGINE.log_tail(25)
            ENGINE.stop()
            STATE.update(status="error", error="Remote Mode reload failed: " + tuner.classify_failure(tail))
            return False, {"error": tuner.classify_failure(tail) + f" ({why})", "log": log}
        if h["gpus"] and h.get("vram_measured", True):
            ENGINE_VRAM["mb"] = max(0, (hardware.gpu_used_mb() or 0) - before)
        elif h["gpus"]:
            ENGINE_VRAM["mb"] = ENGINE.device_mb()
        normal = vram_policy_runner().policy.normal_cfg
        remote = bool(normal) and cfg != normal
        desc = re.sub(r" · Remote Mode.*$", "", STATE.get("desc") or "")
        STATE.update(status="ready", cfg=dict(cfg), ctx=cfg["ctx"], error=None, remote=remote,
                     desc=desc + (f" · Remote Mode ({cfg.get('ngl')} GPU layers)" if remote else ""))
        return True, {"log": log, "load_s": round(time.time() - t0, 1)}
    finally:
        _job["busy"] = False


class _Hooks:
    def engine(self):
        h = hw()
        return {"ready": STATE["status"] == "ready" and ENGINE.running(), "model_id": (STATE.get("model") or {}).get("id"),
                "gpu": bool(h["gpus"]), "unified": bool(h.get("unified_memory"))}

    def current(self):
        try:
            log = ENGINE.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            log = ""
        return {"model_path": (STATE.get("model") or {}).get("path"), "cfg": dict(STATE.get("cfg") or {}), "log": log}

    def reload(self, cfg):
        return _engine_reload(cfg)

    def busy(self):
        return agent.active()

    def hold(self, on):
        vram_policy.hold(on)

    def vram(self):
        return _vram()

    def ram_free_mb(self):
        import psutil
        return psutil.virtual_memory().available // 2**20


_REMOTE = {"runner": None}


def vram_policy_runner():
    if _REMOTE["runner"] is None:
        det = remote_sessions.Detector(load_settings)
        _REMOTE["runner"] = vram_policy.Runner(_Hooks(), det)
    return _REMOTE["runner"]


@app.get("/api/remote")
def remote_state():
    r = vram_policy_runner()
    s = load_settings()
    snap = r.snapshot or remote_sessions.snapshot(s)
    return {"detector": snap, "policy": r.policy.public(), "vram": _vram(),
            "settings": {k: s.get(k) for k in ("remote_mode", "remote_gpu_weight_fraction", "remote_min_free_vram_mb",
                                               "remote_debounce_s", "remote_restore_cooldown_s", "remote_providers",
                                               "remote_allow_probable")},
            "engine": {"cfg": STATE.get("cfg"), "remote": STATE.get("remote"), "model": (STATE.get("model") or {}).get("name")}}


@app.post("/api/remote/retry")
def remote_retry():
    p = vram_policy_runner().policy
    if p.state == "FAILED_SAFE":
        p.state, p.error, p.failed_for = ("REMOTE" if STATE.get("remote") else "NORMAL"), None, None
        p.note("retry requested")
    return {"policy": p.public()}


@app.on_event("startup")
async def _startup():
    try:
        migrate.upgrade_settings(log=lambda *_: None)
    except Exception:
        pass
    tools.load_all()
    router.start()
    app_registry.scan_async()
    task_graph.prune()
    vram_policy_runner().start()


app.mount("/", StaticFiles(directory=str(PKG_DIR / "static"), html=True), name="static")
