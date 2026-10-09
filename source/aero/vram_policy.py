"""Remote Mode: while someone views or controls this PC remotely, run the main model with part of its weights on the
CPU, so the remote session's video encoding and the remote desktop have GPU memory to work with; when the last viewer
has been gone for a while, go back to the normal profile.

States:
    NORMAL           the model runs with its tuned / HAPO profile
    SWITCHING        reloading into the Remote Mode profile
    REMOTE           running the Remote Mode profile
    RESTORE_PENDING  the last viewer left; waiting out the cooldown (a reconnect inside it changes nothing)
    RECOVERING       reloading the normal profile
    FAILED_SAFE      a reload failed; the last working configuration was put back and no further automatic reloads
                     happen until the remote state changes or the user retries

What it changes and what it doesn't:
  - The target is a share of the model's *weight bytes* on the GPU (default 80%), computed from the GGUF's real
    tensor sizes: llama.cpp's -ngl N puts the last N layers on the GPU (and the output layer when N exceeds the layer
    count), so nonuniform layers are counted as they are. MoE models first move expert tensors (--n-cpu-moe).
  - After the reload the share is *measured* from llama.cpp's own buffer report ("CUDA0 model buffer size" vs
    "CPU_Mapped model buffer size"), and VRAM in use is read again. Target, measured share, the model's GPU
    allocation and free VRAM are reported separately: moving 20% of the weights never frees 20% of the card,
    because the KV cache, compute buffers and the driver context stay or move with their layers.
  - llama-server cannot move layers while it runs, so every switch is a restart of the model server. It waits for
    replies in progress to finish, holds new ones until the model is back, and keeps chats (they are files).
    The KV cache does not survive a restart.
  - The tuned profile and HAPO choice are never rewritten: Remote Mode is a temporary derived configuration.
  - Nothing happens without a GPU, on Apple Silicon (unified memory: there is no separate VRAM to free), for a model
    already at or below the target share, or when RAM can't hold the moved weights.
"""
import asyncio
import json
import re
import threading
import time

from .config import DATA, load_settings

STORE = DATA / "remote_mode.json"
STATES = ("NORMAL", "SWITCHING", "REMOTE", "RESTORE_PENDING", "RECOVERING", "FAILED_SAFE")
ACTIVE = ("remote_active", "remote_reconnecting")


# ------------------------------------------------------------------------------------------------ planning

def gpu_bytes(lb, ngl, ncmoe=0):
    """Weight bytes llama.cpp puts on the GPU for -ngl ngl (and --n-cpu-moe ncmoe). Layer n_layer is the output
    layer, offloaded first: -ngl N covers layers n_layer+1-N .. n_layer (measured on llama.cpp 0.6.0: "-ngl 30" on a
    42-layer model logs "offloading output layer to GPU", "offloading 29 repeating layers", "offloaded 30/43")."""
    n = lb["n_layer"]
    start = max(0, n + 1 - int(ngl))
    on = sum(lb["layers"][start:n])
    if int(ngl) >= 1:
        on += lb["output"]
    if ncmoe:
        on -= sum(e for i, e in enumerate(lb["experts"]) if start <= i < int(ncmoe))
    return max(0, on)


def fraction(lb, ngl, ncmoe=0):
    return gpu_bytes(lb, ngl, ncmoe) / max(1, lb["total"])


def plan(lb, cfg, target=0.8):
    """The Remote Mode config for a model's layer byte table and current config. Returns a dict with cfg (or None
    when nothing should change), the estimated GPU share before and after, and why."""
    n = lb["n_layer"]
    ngl = int(cfg.get("ngl", 999))
    ncmoe = int(cfg.get("ncmoe") or 0)
    now = fraction(lb, ngl, ncmoe)
    out = {"before": round(now, 4), "target": target, "n_layer": n}
    if ngl == 0 or now <= 0:
        return {**out, "cfg": None, "after": now, "why": "the model already runs on the CPU"}
    if now <= target + 0.01:
        return {**out, "cfg": None, "after": now, "why": f"only {now:.0%} of the model is on the GPU already"}
    moe = any(lb["experts"])
    best = None
    if moe:                                        # experts first: they are the bulk of a MoE model's weights
        for k in range(ncmoe, n + 1):
            f = fraction(lb, ngl, k)
            if f <= target + 0.005:
                best = {"ngl": ngl, "ncmoe": k, "after": f}
                break
    if best is None:
        start = min(ngl, n + 1)
        for k in range(start, -1, -1):
            f = fraction(lb, k, ncmoe if moe else 0)
            if f <= target + 0.005:
                best = {"ngl": k, "ncmoe": ncmoe if moe else 0, "after": f}
                break
    if best is None:
        return {**out, "cfg": None, "after": now, "why": "no layer split reaches the target"}
    new = dict(cfg)
    new["ngl"] = best["ngl"]
    if best.get("ncmoe"):
        new["ncmoe"] = best["ncmoe"]
    elif "ncmoe" in new and not moe:
        new.pop("ncmoe")
    moved = gpu_bytes(lb, ngl, ncmoe) - gpu_bytes(lb, best["ngl"], best.get("ncmoe", 0))
    return {**out, "cfg": new, "after": round(best["after"], 4), "moved_bytes": moved,
            "why": f"{best['ngl']} of {n} layers on the GPU" + (f", experts of {best['ncmoe']} layers on the CPU"
                                                                 if best.get("ncmoe") else "")}


def step_down(lb, cfg, need_bytes):
    """A config with at least need_bytes more weight moved to the CPU (the minimum-free-VRAM rule), or None."""
    ngl = int(cfg.get("ngl", 999))
    ncmoe = int(cfg.get("ncmoe") or 0)
    base = gpu_bytes(lb, ngl, ncmoe)
    for k in range(min(ngl, lb["n_layer"] + 1), -1, -1):
        if base - gpu_bytes(lb, k, ncmoe) >= need_bytes:
            return {**cfg, "ngl": k}
    return None


# ------------------------------------------------------------------------------------------------ measuring

_BUF = re.compile(r"(\b[A-Za-z_]+\d*(?:_[A-Za-z]+)?)\s+(model|KV|compute|RS|output|recurrent)\s+buffer size\s*=\s*"
                  r"([\d.]+)\s*MiB", re.I)
_OFFL = re.compile(r"offloaded (\d+)/(\d+) layers to GPU")


def buffers(log_text):
    """llama.cpp's own allocation report from a llama-server log: {gpu: {kind: MiB}, cpu: {kind: MiB}, devices:
    {name: MiB}, offloaded: (n, total) | None, weights_gpu_share: measured share of weight bytes on GPUs | None}."""
    gpu, cpu, dev = {}, {}, {}
    for name, kind, mb in _BUF.findall(log_text or ""):
        kind, mb = kind.lower(), float(mb)
        host = name.upper().startswith("CPU") or "HOST" in name.upper()
        side = cpu if host else gpu
        side[kind] = round(side.get(kind, 0.0) + mb, 2)
        if not host:
            dev[name] = round(dev.get(name, 0.0) + mb, 2)
    m = _OFFL.findall(log_text or "")
    g, c = gpu.get("model", 0.0), cpu.get("model", 0.0)
    return {"gpu": gpu, "cpu": cpu, "devices": dev, "offloaded": (int(m[-1][0]), int(m[-1][1])) if m else None,
            "weights_gpu_share": round(g / (g + c), 4) if g + c > 0 else None}


# ------------------------------------------------------------------------------------------------ the policy

class Policy:
    """Decides; does not reload by itself. tick() returns an action ("enter" / "restore") or None; the runner
    executes it and reports back with done()."""

    def __init__(self, store=None):
        self.store = store
        self.state = "NORMAL"
        self.remote_since = None          # when a counted viewer was first seen (debounce)
        self.left_at = None               # when the last viewer left (cooldown)
        self.busy = False                 # a transition is running
        self.normal_cfg = None
        self.remote_cfg = None
        self.model_id = None
        self.plan = None
        self.measured = {}
        self.error = None
        self.wait_note = ""
        self.history = []
        self.failed_for = None            # the remote episode a failure belongs to (no retry inside it)

    def public(self):
        return {"state": self.state, "busy": self.busy, "model_id": self.model_id, "plan": self.plan,
                "measured": self.measured, "error": self.error, "wait_note": self.wait_note,
                "remote_since": self.remote_since, "left_at": self.left_at, "history": self.history[-12:],
                "normal_cfg": self.normal_cfg, "remote_cfg": self.remote_cfg}

    def note(self, what, **kw):
        self.history.append({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "event": what, **kw})
        self.history = self.history[-40:]

    def tick(self, now, det_state, settings, engine):
        """engine: {"ready": bool, "model_id": str|None, "gpu": bool, "unified": bool}."""
        mode = str(settings.get("remote_mode") or "auto")
        debounce = float(settings.get("remote_debounce_s") or 8)
        cool = float(settings.get("remote_restore_cooldown_s") or 90)
        want = mode == "on" or (mode == "auto" and det_state in ACTIVE)
        if self.busy:
            return None
        if engine.get("model_id") != self.model_id:
            # a different model was loaded (or the model was unloaded): it runs its normal profile now
            if self.state != "NORMAL":
                self.note("model changed: back to normal bookkeeping")
            self.state, self.model_id, self.remote_cfg, self.normal_cfg = "NORMAL", engine.get("model_id"), None, None
            self.plan, self.error, self.failed_for = None, None, None
            self.measured = {}
        if not engine.get("ready") or not engine.get("gpu") or engine.get("unified"):
            self.remote_since = None if not want else self.remote_since
            return None
        if self.state == "FAILED_SAFE":
            if not want and self.failed_for == "enter":
                self.state, self.error, self.failed_for = "NORMAL", None, None
                self.note("remote session over: cleared the failure")
            return None
        if self.state == "NORMAL":
            if not want:
                self.remote_since = None
                return None
            if self.remote_since is None:
                self.remote_since = now
                self.note("remote viewer seen" if mode == "auto" else "Remote Mode turned on")
            if mode == "on" or now - self.remote_since >= debounce:
                return "enter"
            return None
        if self.state == "REMOTE":
            if want:
                self.left_at = None
                return None
            if mode == "off" or det_state in ("local", "remote_disconnected"):
                self.state, self.left_at = "RESTORE_PENDING", now
                self.note("last viewer left: waiting before restoring" if mode != "off" else "Remote Mode turned off")
                if mode == "off":
                    return "restore"
            return None                            # unknown: neither active nor gone, keep Remote Mode
        if self.state == "RESTORE_PENDING":
            if want:
                self.state, self.left_at = "REMOTE", None
                self.note("viewer back within the cooldown: no reload")
                return None
            if mode == "off" or now - (self.left_at or now) >= cool:
                return "restore"
        return None

    def done(self, action, ok, **kw):
        self.busy = False
        if action == "enter":
            if ok:
                self.state, self.error = "REMOTE", None
                self.note("Remote Mode on", **kw)
            elif kw.get("noop"):
                self.state = "REMOTE"                  # nothing to move: count it as Remote Mode without a reload
                self.note("Remote Mode on without a reload", why=kw.get("why"))
            else:
                self.state, self.error, self.failed_for = "FAILED_SAFE", kw.get("error"), "enter"
                self.note("switch failed: kept the normal profile", error=kw.get("error"))
        elif action == "restore":
            if ok:
                self.state, self.error, self.remote_since, self.left_at = "NORMAL", None, None, None
                self.remote_cfg = None
                self.note("normal profile restored", **kw)
            else:
                self.state, self.error, self.failed_for = "FAILED_SAFE", kw.get("error"), "restore"
                self.note("restore failed", error=kw.get("error"))
        self.save()

    def save(self):
        if not self.store:
            return
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            self.store.write_text(json.dumps({"schema": 1, **self.public()}, default=str), encoding="utf-8")
        except OSError:
            pass


# ------------------------------------------------------------------------------------------------ runner

class Runner:
    """Polls the detector and runs the policy's transitions through the server's engine hooks:
        hooks.engine()          -> {"ready", "model_id", "gpu", "unified"}
        hooks.current()         -> {"model_path", "cfg", "log"}
        hooks.reload(cfg)       -> (ok, info) restarts llama-server with cfg (blocking)
        hooks.busy()            -> True while a reply is being written
        hooks.hold(on)          -> new chats wait while on
        hooks.vram()            -> {"used_mb", "free_mb", "total_mb", "per_gpu": [...]} or None
        hooks.ram_free_mb()     -> int
    """

    def __init__(self, hooks, detector, policy=None, interval=2.0):
        self.hooks, self.detector, self.interval = hooks, detector, interval
        self.policy = policy or Policy(STORE)
        self.snapshot = None
        self.thread = None
        self.stop_ev = threading.Event()
        self.layer_cache = {}

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, daemon=True, name="remote-mode")
        self.thread.start()

    def _loop(self):
        while not self.stop_ev.wait(self.interval):
            try:
                self.step()
            except Exception as e:  # noqa: BLE001
                self.policy.error = f"{type(e).__name__}: {e}"

    def step(self, now=None):
        s = load_settings()
        self.snapshot = self.detector.read()
        eng = self.hooks.engine()
        act = self.policy.tick(time.time() if now is None else now, self.snapshot["state"], s, eng)
        if act:
            self.policy.busy = True
            try:
                self.run(act, s)
            except Exception as e:  # noqa: BLE001
                self.policy.done(act, False, error=f"{type(e).__name__}: {e}")
        return act

    def _layers(self, path, mtp_used=False):
        key = (path, mtp_used)
        if key not in self.layer_cache:
            from . import gguf
            self.layer_cache[key] = gguf.layer_bytes(path, mtp_used)
        return self.layer_cache[key]

    def _wait_idle(self, limit=600):
        t0 = time.time()
        while self.hooks.busy():
            self.policy.wait_note = "waiting for the reply in progress to finish"
            if time.time() - t0 > limit:
                return False
            time.sleep(1.0)
        self.policy.wait_note = ""
        return True

    def run(self, act, s):
        p = self.policy
        cur = self.hooks.current()
        if act == "enter":
            lb = self._layers(cur["model_path"], "draft-mtp" in (cur.get("log") or "").split("\n", 1)[0])
            target = max(0.05, min(1.0, float(s.get("remote_gpu_weight_fraction") or 0.8)))
            pl = plan(lb, cur["cfg"], target)
            before = buffers(cur.get("log", ""))
            measured_before = before.get("weights_gpu_share")
            if measured_before is not None and measured_before <= target + 0.01 and pl.get("cfg"):
                pl = {**pl, "cfg": None, "why": f"measured {measured_before:.0%} of the weights on the GPU already"}
            p.plan = pl
            if not pl.get("cfg"):
                return p.done("enter", False, noop=True, why=pl["why"])
            need = (pl.get("moved_bytes") or 0) / 2**20
            free = self.hooks.ram_free_mb()
            if free is not None and need + 1024 > free:
                return p.done("enter", False, error=f"not enough free RAM for the moved weights: needs about "
                                                     f"{need + 1024:,.0f} MB, {free:,.0f} MB free")
            p.normal_cfg = dict(cur["cfg"])
            vram_before = self.hooks.vram()
            lb_target = target
            if not self._wait_idle():
                p.busy = False                          # keep waiting: the next step tries again (no failure)
                p.wait_note = "waiting for the reply in progress to finish"
                return None
            self.hooks.hold(True)
            try:
                ok, info = self.hooks.reload(pl["cfg"])
                share = buffers((info or {}).get("log", "")).get("weights_gpu_share") if ok else None
                if ok and share is not None and share > lb_target + 0.05:
                    # this llama.cpp build places layers differently than planned: correct once from the measurement
                    lower = step_down(lb, pl["cfg"], (share - lb_target) * lb["total"])
                    if lower:
                        ok2, info2 = self.hooks.reload(lower)
                        if ok2:
                            pl["cfg"], info = lower, info2
                            pl["why"] += f"; corrected to {lower['ngl']} layers after measuring {share:.0%}"
                if ok:
                    min_free = int(s.get("remote_min_free_vram_mb") or 0)
                    v = self.hooks.vram()
                    if min_free and v and v.get("free_mb") is not None and v["free_mb"] < min_free:
                        lower = step_down(lb, pl["cfg"], (min_free - v["free_mb"]) * 2**20)
                        if lower:
                            ok2, info2 = self.hooks.reload(lower)
                            if ok2:
                                pl["cfg"], info, v = lower, info2, self.hooks.vram()
                                pl["why"] += f"; lowered to {lower['ngl']} layers for {min_free} MB free VRAM"
                    p.remote_cfg = dict(pl["cfg"])
                    p.measured = self._measure(info, vram_before, v)
                    return p.done("enter", True, measured=p.measured.get("weights_gpu_share"))
                rb_ok, _ = self.hooks.reload(p.normal_cfg)            # roll back to what worked
                return p.done("enter", False, error=(info or {}).get("error", "the Remote Mode profile failed to load")
                              + ("" if rb_ok else "; rolling back also failed: load the model again"))
            finally:
                self.hooks.hold(False)
        if act == "restore":
            if not p.normal_cfg:
                return p.done("restore", True, note="nothing was changed")
            vram_before = self.hooks.vram()
            if not self._wait_idle():
                p.busy = False
                return None
            self.hooks.hold(True)
            try:
                ok, info = self.hooks.reload(p.normal_cfg)
                if ok:
                    p.measured = {**self._measure(info, vram_before, self.hooks.vram()), "restored": True}
                    return p.done("restore", True)
                rb_ok, _ = self.hooks.reload(p.remote_cfg) if p.remote_cfg else (False, None)
                return p.done("restore", False, error=(info or {}).get("error", "the normal profile failed to load")
                              + ("; Remote Mode's profile is running again" if rb_ok else "; load the model again"))
            finally:
                self.hooks.hold(False)

    @staticmethod
    def _measure(info, vram_before, vram_after):
        b = buffers((info or {}).get("log", ""))
        out = {"weights_gpu_share": b["weights_gpu_share"], "gpu_model_mb": b["gpu"].get("model"),
               "cpu_model_mb": b["cpu"].get("model"), "gpu_kv_mb": b["gpu"].get("kv"),
               "gpu_compute_mb": b["gpu"].get("compute"), "devices": b["devices"], "offloaded": b["offloaded"],
               "at": time.strftime("%Y-%m-%d %H:%M:%S"), "load_s": (info or {}).get("load_s")}
        if vram_before and vram_after and vram_before.get("used_mb") is not None and vram_after.get("used_mb") is not None:
            out.update(vram_used_before_mb=vram_before["used_mb"], vram_used_after_mb=vram_after["used_mb"],
                       vram_freed_mb=vram_before["used_mb"] - vram_after["used_mb"], vram_free_mb=vram_after.get("free_mb"),
                       per_gpu=vram_after.get("per_gpu"))
        return out


# ------------------------------------------------------------------------------------------------ holding chats

_hold = {"on": False, "since": 0.0}


def hold(on):
    _hold.update(on=bool(on), since=time.time())


def holding():
    return _hold["on"]


async def wait_for_model(limit=600):
    """For a new chat turn: yields one notice and waits while Remote Mode is reloading the model."""
    if not _hold["on"]:
        return
    yield {"t": "notice", "text": "The model is restarting for Remote Mode; this message runs as soon as it is back."}
    t0 = time.time()
    while _hold["on"] and time.time() - t0 < limit:
        await asyncio.sleep(0.5)
