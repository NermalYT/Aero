"""Hardware detection for any PC: GPUs (NVIDIA through nvidia-smi; AMD and Intel through the Windows display
driver registry or Linux sysfs), RAM and CPU through psutil.

NVIDIA cards report live VRAM use. For other cards Windows gives no cheap system-wide reading, so "used" is an
estimate (what the desktop typically holds) and Aero measures its own models from llama.cpp's buffer report
instead (see engine.LlamaServer.device_mb). Several GPUs are pooled: llama.cpp splits layers across them.
"""
import os
import platform
import re
import shutil
import subprocess
import time

import psutil

from .config import IS_WIN

_NO_WINDOW = 0x08000000 if IS_WIN else 0
DESKTOP_EST_MB = 1024        # what Windows and open apps usually keep on a GPU we can't read live
MIN_DEDICATED_MB = 2048      # less dedicated memory than this is an integrated GPU: plan for system RAM
# integrated GPUs that can report a large BIOS carve-out as "dedicated" memory
_IGPU = re.compile(r"^(AMD )?Radeon(\(TM\))? (\d+M )?Graphics$|Vega \d+ Graphics|Radeon \d{3}M|UHD Graphics|Iris|"
                   r"^Intel\(R\) (HD )?Graphics|^Intel\(R\) Arc\(TM\) Graphics$", re.I)
_cache = {"t": 0.0, "other": None}


def _nvidia_smi():
    exe = shutil.which("nvidia-smi")
    if not exe and IS_WIN:
        for c in (r"C:\Windows\System32\nvidia-smi.exe",
                  r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
            if os.path.exists(c):
                return c
    return exe


def _run(args, timeout=10):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              creationflags=_NO_WINDOW).stdout
    except Exception:
        return ""


def _nvidia():
    exe = _nvidia_smi()
    if not exe:
        return []
    out = _run([exe, "--query-gpu=index,name,memory.total,memory.used,memory.free,driver_version",
                "--format=csv,noheader,nounits"])
    res = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 6:
            continue
        try:
            res.append({"index": int(p[0]), "name": p[1], "total_mb": int(float(p[2])),
                        "used_mb": int(float(p[3])), "free_mb": int(float(p[4])), "driver": p[5],
                        "vendor": "nvidia", "measured": True})
        except ValueError:
            continue
    return res


def _vendor(name):
    n = name.lower()
    if "nvidia" in n or "geforce" in n or "rtx" in n or "quadro" in n:
        return "nvidia"
    if "amd" in n or "radeon" in n:
        return "amd"
    if "intel" in n or "arc" in n:
        return "intel"
    return "other"


def _windows_adapters():
    """Display adapters from the driver registry. HardwareInformation.qwMemorySize is the 64-bit dedicated VRAM
    size (the 32-bit MemorySize value tops out at 4 GB)."""
    try:
        import winreg
    except ImportError:
        return []
    out = []
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return []
    for i in range(64):
        try:
            sub = winreg.EnumKey(root, i)
        except OSError:
            break
        if not sub.isdigit():
            continue
        try:
            k = winreg.OpenKey(root, sub)
            name = str(winreg.QueryValueEx(k, "DriverDesc")[0])
            mem = 0
            for v in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                try:
                    raw = winreg.QueryValueEx(k, v)[0]
                    mem = int.from_bytes(raw, "little") if isinstance(raw, (bytes, bytearray)) else int(raw)
                    if mem:
                        break
                except OSError:
                    continue
            try:
                drv = str(winreg.QueryValueEx(k, "DriverVersion")[0])
            except OSError:
                drv = ""
        except OSError:
            continue
        if "basic display" in name.lower() or "remote" in name.lower() or "virtual" in name.lower():
            continue
        out.append({"name": name, "total_mb": mem // 2**20, "driver": drv, "vendor": _vendor(name)})
    return out


def _linux_amd():
    out = []
    try:
        cards = sorted(p for p in os.listdir("/sys/class/drm") if re.fullmatch(r"card\d+", p))
    except OSError:
        return []
    for c in cards:
        d = f"/sys/class/drm/{c}/device"
        try:
            total = int(open(f"{d}/mem_info_vram_total").read()) // 2**20
            used = int(open(f"{d}/mem_info_vram_used").read()) // 2**20
        except (OSError, ValueError):
            continue
        name = "AMD GPU"
        try:
            name = open(f"{d}/product_name").read().strip() or name
        except OSError:
            pass
        out.append({"name": name, "total_mb": total, "used_mb": used, "free_mb": total - used, "driver": "amdgpu",
                    "vendor": "amd", "measured": True, "_sysfs": d})
    return out


def _other_gpus(nvidia):
    """Non-NVIDIA GPUs big enough to run models on (integrated GPUs share system RAM and are left out)."""
    if _cache["other"] is not None and time.time() - _cache["t"] < 300:
        return _cache["other"]
    found = []
    if IS_WIN:
        for a in _windows_adapters():
            if a["vendor"] == "nvidia" and nvidia:
                continue                     # already listed with live numbers
            if a["total_mb"] < MIN_DEDICATED_MB or _IGPU.search(a["name"]):
                continue
            used = min(DESKTOP_EST_MB, a["total_mb"] // 4)
            found.append({**a, "used_mb": used, "free_mb": a["total_mb"] - used, "measured": False})
    else:
        found = [g for g in _linux_amd() if g["total_mb"] >= MIN_DEDICATED_MB]
    _cache.update(t=time.time(), other=found)
    return found


def gpus():
    res = _nvidia()
    for g in _other_gpus(bool(res)):
        res.append({**g, "index": len(res)})
    return res


def vram_measurable():
    """True when every GPU reports live usage (NVIDIA, or AMD on Linux)."""
    g = gpus()
    return bool(g) and all(x.get("measured") for x in g)


def cuda_driver_version():
    exe = _nvidia_smi()
    if not exe:
        return None
    m = re.search(r"CUDA(?:\s+UMD)?\s+Version:\s*([\d.]+)", _run([exe]))
    return m.group(1) if m else None


def gpu_used_mb(index=None):
    """VRAM in use: one GPU by index, or all of them together (index None). None when it can't be read."""
    g = [x for x in gpus() if index is None or x["index"] == index]
    if not g:
        return None
    total = 0
    for x in g:
        if not x.get("measured"):
            return None
        if x.get("_sysfs"):
            try:
                total += int(open(f"{x['_sysfs']}/mem_info_vram_used").read()) // 2**20
            except (OSError, ValueError):
                return None
        else:
            total += x["used_mb"]
    return total


def cpu_name():
    if IS_WIN:
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except Exception:
            pass
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "CPU"


def snapshot():
    vm = psutil.virtual_memory()
    g = gpus()
    names = sorted({x["name"] for x in g})
    return {
        "os": f"{platform.system()} {platform.release()}",
        "cpu": cpu_name(),
        "cores": psutil.cpu_count(logical=False) or 4,
        "threads": psutil.cpu_count(logical=True) or 8,
        "ram_total_mb": vm.total // 2**20,
        "ram_avail_mb": vm.available // 2**20,
        "gpus": g,
        "vram_total_mb": sum(x["total_mb"] for x in g),
        "vram_free_mb": sum(x["free_mb"] for x in g),
        "gpu_name": (names[0] if len(g) == 1 else f"{len(g)} GPUs: " + " + ".join(x["name"] for x in g)) if g else None,
        "gpu_vendor": g[0]["vendor"] if g else None,
        "vram_measured": bool(g) and all(x.get("measured") for x in g),
        "cuda": cuda_driver_version() if any(x["vendor"] == "nvidia" for x in g) else None,
    }


def summary(hw=None):
    """One line for logs and the setup screen, e.g. "RTX 4070 12 GB · 32 GB RAM · Ryzen 7 7800X3D (8 cores)"."""
    hw = hw or snapshot()
    parts = []
    if hw["gpus"]:
        parts.append(" + ".join(f"{x['name']} {round(x['total_mb'] / 1024)} GB" for x in hw["gpus"]))
    else:
        parts.append("no dedicated GPU (models run on the CPU)")
    parts.append(f"{round(hw['ram_total_mb'] / 1024)} GB RAM")
    parts.append(f"{hw['cpu']} ({hw['cores']} cores)")
    return " · ".join(parts)


def settle_vram(index=None, timeout=4.0):
    """Wait until VRAM usage stops changing (after a server exits) and return it in MB. GPUs that can't be read
    live return the desktop estimate."""
    last, t0 = None, time.time()
    while time.time() - t0 < timeout:
        cur = gpu_used_mb(index)
        if cur is None:
            g = gpus()
            return sum(x["used_mb"] for x in g) if g else 0
        if last is not None and abs(cur - last) <= 32:
            return cur
        last = cur
        time.sleep(0.35)
    return last or 0
