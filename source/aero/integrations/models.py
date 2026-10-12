"""Big models workspace: run models bigger than the GPU.

Two engines:
  llama.cpp (GGUF)   bigmodel.py plans where every tensor goes for the VRAM you have: whole layers, feed-forward
                     weights in RAM with attention and KV on the GPU (--override-tensor), or MoE experts in RAM
                     (--n-cpu-moe); validation/bench_bigmodel.py measures the plans. This is the fast path: weights stay
                     resident in VRAM and RAM.
  AirLLM             lyogavin/airllm (Apache-2.0) for full-precision Hugging Face checkpoints that don't fit in VRAM +
                     RAM: it splits the model into per-layer files and streams one layer at a time through the GPU,
                     so a 70B model runs in about 4 GB of VRAM. Every token reads every layer from disk, so it is slow
                     (seconds per token on NVMe); it is for quality-critical or offline work, not chat speed. AirLLM
                     runs in its own virtual environment (torch, transformers) behind an OpenAI-compatible shim
                     (airllm_shim.py) on Aero's model port, so the whole agent works with it unchanged.
"""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import httpx

from . import Pack, Requirement, register, toolchain as tc
from .. import bigmodel, capabilities
from ..config import IS_WIN, models_dir
from ..engine import LlamaServer
from ..models import _parts
from ..tools import tool

HOME = tc.ROOT / "airllm"
VENV = HOME / "venv"
SHARDS = HOME / "shards"
SHIM = Path(__file__).resolve().parent / "airllm_shim.py"
PACKAGES = ["airllm", "transformers>=4.49,<6", "accelerate>=1.0", "safetensors", "sentencepiece", "scipy",
            "huggingface_hub", "tqdm"]


def _py():
    return VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")


def _nvidia():
    try:
        return subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=10,
                              creationflags=tc.NO_WINDOW).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def install(emit=None, cancel=None):
    tc.require_online("install AirLLM and PyTorch")
    if not _py().exists():
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True, creationflags=tc.NO_WINDOW)
    torch_idx = "https://download.pytorch.org/whl/cu128" if _nvidia() else "https://download.pytorch.org/whl/cpu"
    if emit:
        emit({"type": "log", "text": f"Installing PyTorch ({'CUDA 12.8' if _nvidia() else 'CPU'}) into AirLLM's own "
                                     "environment (a few GB)…"})
    r = tc.run([str(_py()), "-m", "pip", "install", "--disable-pip-version-check", "-q", "torch",
                "--index-url", torch_idx], timeout=7200, cancel=cancel, network=True)
    if r["rc"] != 0:
        raise RuntimeError("PyTorch install failed: " + (r["err"] or r["out"])[-800:])
    if emit:
        emit({"type": "log", "text": "Installing AirLLM and transformers…"})
    extra = ["bitsandbytes"] if _nvidia() else []         # AirLLM's 4-bit / 8-bit layer compression (CUDA only)
    r = tc.run([str(_py()), "-m", "pip", "install", "--disable-pip-version-check", "-q", *PACKAGES, *extra],
               timeout=3600, cancel=cancel, network=True)
    if r["rc"] != 0:
        raise RuntimeError("AirLLM install failed: " + (r["err"] or r["out"])[-800:])
    v = tc.run([str(_py()), "-c", "import importlib.metadata as m, torch; print(m.version('airllm'), torch.__version__, "
                                  "torch.cuda.is_available())"], timeout=300)
    tc.mark("airllm", version=v["out"].strip())
    return "AirLLM " + v["out"].strip()


def _ok():
    if not _py().exists():
        return False, "not installed"
    v = tc.installed().get("airllm", {}).get("version")
    return bool(v), (f"airllm, torch, CUDA: {v}" if v else "environment exists, packages missing")


def shard_dir(model):
    """AirLLM writes a model's per-layer files to <folder>/splitted_model[.compression]; one folder per model, so a
    second model never picks up the first one's layers."""
    key = str(model).rstrip("/\\")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", key.replace("\\", "/").split("/")[-1])[:60]
    return SHARDS / f"{name or 'model'}-{hashlib.sha1(key.encode()).hexdigest()[:8]}"


class AirServer(LlamaServer):
    """llama-server's interface (start/wait_ready/running/stop/log_tail/url) over the AirLLM shim."""

    def __init__(self, port):
        super().__init__(port, "airllm")
        self.spec = None

    def launch(self, model, compression="none", max_seq=4096):
        self.stop()
        self.spec = {"model": model, "compression": compression, "max_seq": max_seq}
        shards = shard_dir(model)
        shards.mkdir(parents=True, exist_ok=True)
        logf = open(self.log_path, "w", encoding="utf-8", errors="replace")
        cmd = [str(_py()), "-I", str(SHIM), "--model", str(model), "--port", str(self.port), "--shards", str(shards),
               "--compression", compression, "--max-seq", str(max_seq), "--hf-home", str(HOME / "hf")]
        logf.write("ARGS: " + " ".join(cmd) + "\n")
        logf.flush()
        self.proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     env=tc.env({"PYTHONUNBUFFERED": "1"}), creationflags=tc.NO_WINDOW)
        self._logf = logf

    def start(self, args, env_extra=None):                      # a reload (Remote Mode) relaunches the same spec
        if self.spec:
            self.launch(**self.spec)

    def device_mb(self):
        return 0


def ready():
    try:
        return httpx.get(f"http://127.0.0.1:{_server_port()}/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _server_port():
    from ..config import LLAMA_PORT
    return LLAMA_PORT


@tool("bigmodel_plan", "Plan how a GGUF model bigger than the GPU runs best here: whole layers on the GPU, "
      "feed-forward weights in RAM with attention on the GPU, or MoE experts in RAM; with the VRAM and RAM each plan "
      "takes and an estimated speed. budget_gb: the VRAM to use (default: free VRAM).", "integrations",
      {"path": {"type": "string"}, "budget_gb": {"type": "number"}, "context_tokens": {"type": "integer"}}, ["path"],
      summary=lambda a: a.get("path", ""))
def bigmodel_plan(ctx, path, budget_gb=None, context_tokens=8192):
    p = ctx.path(path)
    if not p.exists():
        return {"text": f"No file at {p}.", "error": True}
    from .. import hardware
    import psutil
    budget = int((budget_gb or 0) * 1024) or max(512, (hardware.snapshot().get("vram_total_mb") or 0)
                                                  - (hardware.gpu_used_mb() or 0) - 300)
    info = bigmodel.inspect(p)
    plans = bigmodel.plan(info, budget, psutil.virtual_memory().available // 2**20, int(context_tokens or 8192))
    lines = [f"{p.name}: {info['total'] / 2**30:.1f} GB, {len(info['layers'])} layers"
             + (", mixture of experts" if info["moe"] else "") + f"; VRAM budget {budget:,} MB"]
    for x in plans:
        lines.append(f"- {x['strategy']}: GPU {x['gpu_mb']:,} MB, RAM {x['cpu_mb']:,} MB, about "
                     f"{bigmodel.estimate_tg(x)} tok/s (estimate) — {x['note']}")
    return "\n".join(lines)


capabilities.register("bigmodel_plan", ["model.plan"], "local_read")


def _panel():
    out = {"airllm": {"installed": _ok()[0], "version": tc.installed().get("airllm", {}).get("version"),
                      "shards": str(SHARDS)}}
    ev = Path(__file__).resolve().parent.parent.parent / "docs" / "evidence"
    out["measured"] = []
    for f in sorted(ev.glob("bigmodel_*.json")) if ev.exists() else []:
        if "before_fix" in f.name:                      # superseded by the corrected planner's runs
            continue
        try:
            out["measured"].append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    gg = []
    root = models_dir()
    for f in sorted(root.rglob("*.gguf")) if root.exists() else []:
        hidden = any(part.startswith("_") for part in f.relative_to(root).parts)
        extra_part = "-000" in f.name and "-00001-" not in f.name
        if hidden or extra_part or "mmproj" in f.name.lower():
            continue
        size = sum(x.stat().st_size for x in _parts(f) if x.exists())      # every part of a split GGUF
        gg.append({"path": str(f), "name": f.name, "gb": round(size / 2**30, 1)})
    out["ggufs"] = gg[:200]
    return out


register(Pack("bigmodels", "Big models", "models",
              "Run models bigger than the GPU: the best llama.cpp split for any GGUF, AirLLM layer streaming for "
              "full-precision checkpoints",
              "https://github.com/lyogavin/airllm (AirLLM) · https://github.com/ggml-org/llama.cpp (offload)",
              "Apache-2.0 (AirLLM) · MIT (llama.cpp)",
              triggers=[r"\b(70b|72b|120b|235b|405b|671b|bigger than (my|the) (gpu|vram)|too big for (my|the) (gpu|vram)|"
                        r"full precision|fp16|bf16|unquanti[sz]ed|airllm|layer streaming|offload)\b"],
              tools=["bigmodel_plan"],
              requirements=[Requirement("airllm", "AirLLM + PyTorch (own environment, about 4 GB)", _ok,
                                        install=install, size_mb=4000, optional=True)],
              panel=_panel,
              notes="AirLLM reads every layer from disk for every token: expect seconds per token. For speed use a "
                    "GGUF quant with the llama.cpp plan; AirLLM is for full precision when nothing else fits."))
