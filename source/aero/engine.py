"""llama-server process control.

Each llama-server child is placed in a Windows Job Object with KILL_ON_JOB_CLOSE, so if
Aero crashes or is killed the model is unloaded and VRAM is freed automatically.
"""
import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

import httpx

from .config import IS_WIN, LLAMA_DIR, LOGS

_NO_WINDOW = 0x08000000 if IS_WIN else 0
_flags_cache = {}


# ---- Windows job object ------------------------------------------------------
_job = None


def _job_handle():
    global _job
    if not IS_WIN or _job is not None:
        return _job
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32.CreateJobObjectW.restype = wintypes.HANDLE
        h = k32.CreateJobObjectW(None, None)
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info))
        _job = (k32, h)
    except Exception:
        _job = False
    return _job


def _attach(proc):
    j = _job_handle()
    if not j:
        return
    try:
        import ctypes
        k32, h = j
        PROCESS_ALL_ACCESS = 0x1F0FFF
        k32.OpenProcess.restype = ctypes.c_void_p
        ph = k32.OpenProcess(PROCESS_ALL_ACCESS, False, proc.pid)
        k32.AssignProcessToJobObject(ctypes.c_void_p(h), ctypes.c_void_p(ph))
        k32.CloseHandle(ctypes.c_void_p(ph))
    except Exception:
        pass


# ---- binary discovery --------------------------------------------------------

def server_binary():
    env = os.environ.get("AERO_LLAMA_SERVER")
    if env and Path(env).exists():
        return Path(env)
    name = "llama-server.exe" if IS_WIN else "llama-server"
    if LLAMA_DIR.exists():
        hits = sorted(LLAMA_DIR.rglob(name))
        if hits:
            return hits[0]
    return None


def server_help():
    exe = server_binary()
    if not exe:
        return ""
    key = str(exe)
    if key not in _flags_cache:
        try:
            out = subprocess.run([str(exe), "--help"], capture_output=True, text=True, timeout=30,
                                 creationflags=_NO_WINDOW, cwd=str(exe.parent))
            _flags_cache[key] = out.stdout + out.stderr
        except Exception:
            _flags_cache[key] = ""
    return _flags_cache[key]


def supports(flag):
    return re.search(r"(^|[\s,])" + re.escape(flag) + r"([\s,=]|$)", server_help(), re.M) is not None


def server_version():
    exe = server_binary()
    if not exe:
        return "missing"
    try:
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=30,
                             creationflags=_NO_WINDOW, cwd=str(exe.parent))
        m = re.search(r"version:\s*(\S+)", out.stdout + out.stderr)
        return m.group(1) if m else "unknown"
    except Exception:
        return "unknown"


_SPEC_FILES = (("mtp", "draft-mtp"), ("eagle3", "draft-eagle3"), ("dflash", "draft-dflash"), ("dspark", "draft-dspark"))
_mtp = {}


def spec_types():
    """Speculative decoding types this llama-server build offers (empty on builds without --spec-type)."""
    m = re.search(r"--spec-type\s+([\w,-]+)", server_help())
    return set(m.group(1).split(",")) if m else set()


def has_mtp(model):
    """True when the GGUF carries its own multi-token-prediction layer (<arch>.nextn_predict_layers > 0)."""
    key = str(model)
    if key not in _mtp:
        try:
            from . import gguf
            kv = gguf.read_metadata(gguf.split_parts(Path(model))[0])
            _mtp[key] = any(k.endswith(".nextn_predict_layers") and int(gguf._first(v) or 0) > 0 for k, v in kv.items())
        except Exception:
            _mtp[key] = False
    return _mtp[key]


def spec_args(model, settings):
    """Speculative decoding for the main model. Measured on an RTX 5080 16 GB with Qwen3.8-27B (IQ2_M, 32K q8_0 KV,
    temperature 0.6): no speculation 59.5 tok/s; MTP 108 tok/s editing code / 66 on free text (+1.1 GB VRAM);
    n-gram 123 / 59 (no VRAM); both together 168 / 69. So "auto" uses the model's own MTP layer (or a draft model
    when one is set) plus n-gram lookup. The tuner launches every trial with the same arguments, so the extra VRAM
    is measured against the user's limit like everything else."""
    s = settings or {}
    mode = str(s.get("spec_mode") or "auto").strip().lower()
    have = spec_types()
    if mode == "off" or not have:
        return []
    types, extra = [], []
    if mode == "auto":
        draft = str(s.get("draft_model") or "").strip().strip('"')
        if draft and Path(draft).is_file():
            name = Path(draft).name.lower()
            t = next((v for k, v in _SPEC_FILES if name.startswith(k)), "draft-simple")
            if t in have:
                types.append(t)
                extra = ["-md", draft]
        elif "draft-mtp" in have and has_mtp(model):
            types.append("draft-mtp")
        mode = "ngram"
    if mode == "ngram":
        types += ["ngram-mod"] if "ngram-mod" in have else []
    elif not types:
        types = [t for t in mode.split(",") if t.strip() in have]      # an explicit --spec-type list
    return (["--spec-type", ",".join(types)] + extra) if types else []


def build_args(model, cfg, port, mmproj=None, final=False, settings=None, spec=None):
    """Translate a tuning config into llama-server arguments."""
    a = ["-m", str(model), "--host", "127.0.0.1", "--port", str(port),
         "-c", str(cfg["ctx"]), "-ngl", str(cfg.get("ngl", 999)), "-np", "1"]
    if cfg.get("ncmoe"):
        a += ["--n-cpu-moe", str(cfg["ncmoe"])]
    if cfg.get("threads"):
        a += ["-t", str(cfg["threads"])]
    if cfg.get("ub"):
        a += ["-ub", str(cfg["ub"]), "-b", str(max(cfg["ub"], cfg.get("b", 2048)))]
    help_txt = server_help()
    if "[on|off|auto]" in help_txt and supports("--flash-attn"):
        a += ["-fa", "on" if cfg.get("fa", True) else "off"]
    elif cfg.get("fa", True) and supports("--flash-attn"):
        a += ["-fa"]
    kv = cfg.get("kv", "q8_0")
    if kv != "f16":
        a += ["-ctk", kv, "-ctv", kv]
    if supports("--fit"):
        a += ["--fit", "off"]           # we size things ourselves
    if mmproj:
        a += ["--mmproj", str(mmproj)]
    if supports("--no-webui"):
        a += ["--no-webui"]
    if supports("--offline"):
        a += ["--offline"]              # local files only: llama-server must never download or phone home
    if spec:
        a += list(spec)
    if cfg.get("no_mmap"):
        if supports("--no-mmap"):
            a += ["--no-mmap"]
        elif supports("--load-mode"):           # llama.cpp v0.5.0 replaced --no-mmap/--mlock with --load-mode
            a += ["--load-mode", "none"]
    if final:
        a += ["--jinja"]
        if supports("--reasoning-format"):
            a += ["--reasoning-format", "deepseek"]
        if supports("--metrics"):
            a += ["--metrics"]
        extra = (settings or {}).get("extra_server_args", "").strip()
        if extra:
            a += shlex.split(extra, posix=not IS_WIN)
    return a


# "llama_kv_cache:    Vulkan0 KV buffer size =  2176.00 MiB" (host-side buffers such as CUDA_Host are not GPU memory)
_DEV_BUF = re.compile(r"\b(?:CUDA|Vulkan|ROCm|HIP|SYCL|MUSA|CANN|OpenCL|Metal)\d*\s+(?:model|KV|compute|RS|output|recurrent)"
                      r"\s+buffer size\s*=\s*([\d.]+)\s*MiB", re.I)
DRIVER_CTX_MB = 300


class LlamaServer:
    def __init__(self, port, tag="server"):
        self.port = port
        self.tag = tag
        self.proc = None
        self.log_path = LOGS / f"llama-{tag}.log"
        self.args = []
        self._lock = threading.Lock()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, args, env_extra=None):
        exe = server_binary()
        if not exe:
            raise RuntimeError("llama-server binary not found. Re-run the installer.")
        self.stop()
        self.args = args
        logf = open(self.log_path, "w", encoding="utf-8", errors="replace")
        logf.write("ARGS: " + " ".join(args) + "\n")
        logf.flush()
        env = dict(os.environ)
        env.update(env_extra or {})
        self.proc = subprocess.Popen([str(exe)] + args, stdout=logf, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, cwd=str(exe.parent), env=env,
                                     creationflags=_NO_WINDOW)
        self._logf = logf
        _attach(self.proc)

    def wait_ready(self, timeout=600, cancel=None):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if cancel is not None and cancel.is_set():
                return False, "cancelled"
            if self.proc is None or self.proc.poll() is not None:
                return False, "exited"
            try:
                r = httpx.get(self.url + "/health", timeout=2)
                if r.status_code == 200:
                    time.sleep(0.3)         # another server already on this port answers too; ours must still be alive
                    if self.proc is None or self.proc.poll() is not None:
                        return False, "exited"
                    return True, "ok"
            except Exception:
                pass
            time.sleep(0.25)
        return False, "timeout"

    def rss_mb(self):
        """Resident memory of the server process (used to measure CPU-only trials)."""
        try:
            import psutil
            return psutil.Process(self.proc.pid).memory_info().rss // 2**20 if self.running() else 0
        except Exception:
            return 0

    def device_mb(self):
        """GPU memory this server allocated, from llama.cpp's own buffer report (model weights, KV cache, compute
        and recurrent-state buffers on CUDA/Vulkan/ROCm/SYCL devices), plus a fixed allowance for the driver
        context. Used where VRAM can't be read live (AMD and Intel GPUs on Windows). 0 when nothing is reported."""
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return 0
        total = sum(float(m.group(1)) for m in _DEV_BUF.finditer(text))
        return int(total + DRIVER_CTX_MB) if total else 0

    def log_tail(self, n=40):
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-n:])
        except Exception:
            return ""

    def stop(self):
        with self._lock:
            p = self.proc
            self.proc = None
            if p and p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=8)
                except Exception:
                    try:
                        p.kill()
                        p.wait(timeout=5)
                    except Exception:
                        pass
            try:
                self._logf.close()
            except Exception:
                pass


def classify_failure(log: str) -> str:
    l = log.lower()
    if "out of memory" in l or "cudamalloc failed" in l or "failed to allocate" in l or "unable to allocate" in l:
        return "out of memory"
    if "no kernel image" in l or "unsupported gpu" in l:
        return "this llama.cpp build does not support the GPU"
    if "failed to load model" in l or "error loading model" in l:
        return "model failed to load (unsupported architecture or corrupt file)"
    return "server exited"
