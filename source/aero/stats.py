"""Live numbers for the dashboard: GPU (NVML, nvidia-smi fallback), CPU, RAM, model processes, session tokens."""
import shutil
import subprocess
import threading
import time

import psutil

from .config import IS_WIN

_NO_WINDOW = 0x08000000 if IS_WIN else 0
_nvml = {"ok": None, "h": None}
_lock = threading.Lock()
_cache = {"t": 0.0, "v": None}

# Session counters (since the app started); updated by agent/router/cloud
SESSION = {
    "started": time.time(),
    "turns": 0,
    "local_prompt_tokens": 0, "local_completion_tokens": 0, "local_reasoning_chars": 0,
    "tool_calls": {},             # category -> count
    "router_calls": 0, "router_ms_total": 0.0, "router_tokens_saved": 0,
    "tg_history": [],             # last 40 generation speeds (tok/s)
    "pp_history": [],             # last 40 prompt speeds (tok/s)
    "ttft_history": [],
    "reviews": {"ok": 0, "minor": 0, "major": 0},
    "loop": {"active": False, "iteration": 0, "started": 0.0, "chat_id": None},
}


def note_reply(stats):
    if not stats:
        return
    SESSION["local_prompt_tokens"] += int(stats.get("prompt_tokens") or 0)
    SESSION["local_completion_tokens"] += int(stats.get("completion_tokens") or 0)
    for key, hist in (("tg", "tg_history"), ("pp", "pp_history"), ("ttft", "ttft_history")):
        v = stats.get(key)
        if v:
            SESSION[hist] = (SESSION[hist] + [v])[-40:]


def note_tool(category):
    c = SESSION["tool_calls"]
    c[category or "other"] = c.get(category or "other", 0) + 1


def _init_nvml():
    if _nvml["ok"] is not None:
        return _nvml["ok"]
    try:
        import pynvml
        pynvml.nvmlInit()
        _nvml["h"] = pynvml.nvmlDeviceGetHandleByIndex(0)
        _nvml["mod"] = pynvml
        _nvml["ok"] = True
    except Exception:
        _nvml["ok"] = False
    return _nvml["ok"]


def _gpu_nvml():
    p, h = _nvml["mod"], _nvml["h"]
    mem = p.nvmlDeviceGetMemoryInfo(h)
    util = p.nvmlDeviceGetUtilizationRates(h)
    out = {"name": p.nvmlDeviceGetName(h), "used_mb": mem.used // 2**20, "total_mb": mem.total // 2**20,
           "util": util.gpu, "mem_util": util.memory}
    for key, fn in (("temp", lambda: p.nvmlDeviceGetTemperature(h, 0)),
                    ("power_w", lambda: p.nvmlDeviceGetPowerUsage(h) / 1000),
                    ("power_limit_w", lambda: p.nvmlDeviceGetEnforcedPowerLimit(h) / 1000),
                    ("sm_mhz", lambda: p.nvmlDeviceGetClockInfo(h, 1)),
                    ("mem_mhz", lambda: p.nvmlDeviceGetClockInfo(h, 2)),
                    ("fan", lambda: p.nvmlDeviceGetFanSpeed(h))):
        try:
            out[key] = round(fn(), 1)
        except Exception:
            pass
    if isinstance(out["name"], bytes):
        out["name"] = out["name"].decode()
    return out


def _gpu_smi():
    exe = shutil.which("nvidia-smi") or (r"C:\Windows\System32\nvidia-smi.exe" if IS_WIN else None)
    if not exe:
        return None
    try:
        q = subprocess.run([exe, "--query-gpu=name,memory.used,memory.total,utilization.gpu,utilization.memory,"
                                 "temperature.gpu,power.draw,power.limit,clocks.sm,clocks.mem,fan.speed",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=4,
                           creationflags=_NO_WINDOW).stdout.strip().splitlines()
    except Exception:
        return None
    if not q:
        return None
    p = [x.strip() for x in q[0].split(",")]
    keys = ["name", "used_mb", "total_mb", "util", "mem_util", "temp", "power_w", "power_limit_w", "sm_mhz", "mem_mhz", "fan"]
    out = {}
    for k, v in zip(keys, p):
        if k == "name":
            out[k] = v
            continue
        try:
            out[k] = float(v)
        except ValueError:
            pass
    return out


def gpu():
    if _init_nvml():
        try:
            return _gpu_nvml()
        except Exception:
            pass
    return _gpu_smi()


def _proc_mb(pid):
    try:
        return psutil.Process(pid).memory_info().rss // 2**20
    except Exception:
        return 0


def snapshot(procs=None):
    """procs: {"main": pid, "router": pid} of running llama-servers. Cached for 0.8 s."""
    with _lock:
        if _cache["v"] and time.time() - _cache["t"] < 0.8:
            return _cache["v"]
        vm = psutil.virtual_memory()
        try:
            per = psutil.cpu_percent(percpu=True)
        except Exception:
            per = []
        out = {
            "t": time.time(),
            "cpu": round(sum(per) / len(per), 1) if per else psutil.cpu_percent(),
            "cpu_per_core": per,
            "cpu_mhz": round((psutil.cpu_freq() or type("f", (), {"current": 0})).current or 0),
            "ram_used_mb": (vm.total - vm.available) // 2**20, "ram_total_mb": vm.total // 2**20,
            "gpu": gpu(),
            "procs": {k: _proc_mb(v) for k, v in (procs or {}).items() if v},
            "uptime_s": round(time.time() - SESSION["started"]),
        }
        _cache.update(t=time.time(), v=out)
        return out
