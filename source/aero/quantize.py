"""Make a GGUF of any Hugging Face model that has none: download the original weights, convert them with
llama.cpp's own converter (convert_hf_to_gguf.py, MIT) and shrink them with llama-quantize to the quant that fits
this PC best. Low-bit quants (IQ1-IQ3, Q2_K) get an importance matrix (llama-imatrix) first, so the bits go where
they matter most.

Converter: the convert_hf_to_gguf.py, conversion/ and gguf-py of the same llama.cpp build Aero runs (fetched once,
file by file, checked against their git hashes), in a Python environment with PyTorch: AirLLM's when it is installed (integrations/models.py), else
a small CPU-only one of its own (data/toolchains/convert).
"""
import hashlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from . import fit, hardware, hf, models
from .config import IS_WIN, LLAMA_DIR, load_settings, models_dir
from .engine import run_tool, server_binary
from .integrations import toolchain as tc

HOME = tc.ROOT / "convert"
VENV = HOME / "venv"
PACKAGES = ["numpy<3", "sentencepiece", "safetensors", "transformers>=4.49,<6", "protobuf", "tiktoken", "pyyaml",
            "requests", "tqdm"]
# quants Aero makes, best first; imatrix = needs an importance matrix to be any good
LADDER = [("Q8_0", 8.5, False), ("Q6_K", 6.6, False), ("Q5_K_M", 5.7, False), ("Q4_K_M", 4.85, False),
          ("IQ4_XS", 4.25, True), ("Q3_K_M", 3.9, True), ("IQ3_M", 3.7, True), ("IQ3_XXS", 3.1, True),
          ("IQ2_M", 2.7, True), ("Q2_K", 2.6, True), ("IQ2_XXS", 2.1, True), ("IQ1_M", 1.75, True)]


def _exe(name):
    n = name + (".exe" if IS_WIN else "")
    srv = server_binary()
    for d in ([srv.parent] if srv else []) + [LLAMA_DIR]:
        p = Path(d) / n
        if p.exists():
            return p
    hits = sorted(LLAMA_DIR.rglob(n)) if LLAMA_DIR.exists() else []
    return hits[0] if hits else None


def build_tag():
    exe = server_binary()
    if not exe:
        return None
    try:
        out = run_tool(exe, ["--version"])
        m = re.search(r"build (\d+)", out.stdout + out.stderr)
        return f"b{m.group(1)}" if m else None
    except Exception:  # noqa: BLE001
        return None


# ---- picking the quant -------------------------------------------------------------------------------------

def pick(params, budget_mb, ram_mb, gpu=True):
    """The quant to make for a model with this many parameters: the best one that runs fully on the GPU with room
    for an agent-size context; else Q4_K_M split between GPU and RAM (or the best that fits there), else the
    smallest. Returns (quant, needs_imatrix, why)."""
    room = budget_mb if gpu else ram_mb * 0.6

    def size_mb(bpw):
        return params * bpw / 8 / 2**20
    for q, bpw, imx in LADDER:
        if bpw < 3.5:
            break
        if size_mb(bpw) * 1.12 + 1200 <= room:          # weights + KV for ~32k + buffers, like fit.need_mb
            return q, imx, f"{q} runs fully on your {'GPU' if gpu else 'RAM'} with room for a long context"
    split = budget_mb + ram_mb * fit.RAM_SHARE
    for q, bpw, imx in LADDER:
        if 3.5 <= bpw <= 4.85 and size_mb(bpw) * 1.05 <= split:
            return q, imx, f"{q}: part of it runs from system RAM (the tuner places the layers), so expect it to be slower"
    for q, bpw, imx in LADDER:
        if bpw < 3.5 and size_mb(bpw) * 1.05 <= split:
            return q, imx, f"{q}: a low-bit quant with an importance matrix is the biggest that fits GPU + RAM"
    q, bpw, imx = LADDER[-1]
    return q, imx, f"even {q} is bigger than GPU + RAM; it will stream from disk (very slow). AirLLM may suit it better"


# ---- the converter -------------------------------------------------------------------------------------------

def _airllm_py():
    from .integrations import models as big
    ok, _ = big._ok()
    return big._py() if ok else None


def _py():
    return VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")


def converter_ready():
    tag = build_tag()
    return bool(tag and (HOME / tag / "convert_hf_to_gguf.py").exists() and (_airllm_py() or _py().exists()))


def ensure_converter(emit=None, cancel=None):
    """The converter script for this llama.cpp build plus a Python that can run it. Returns (python, script)."""
    tag = build_tag()
    if not tag:
        raise RuntimeError("llama.cpp isn't installed, so there is nothing to convert with.")
    src = HOME / tag
    script = src / "convert_hf_to_gguf.py"
    if not (src / ".complete").exists():
        tc.require_online("download llama.cpp's converter")
        if emit:
            emit({"type": "log", "text": f"Fetching llama.cpp's converter for build {tag} (about 2 MB)…"})
        fetch_converter(tag, src, cancel)
    py = _airllm_py()
    own = py is None
    if own:
        py = _py()
        if not py.exists():
            subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True, creationflags=tc.NO_WINDOW)
    marker = HOME / ("pkgs-own.ok" if own else "pkgs-airllm.ok")
    if not marker.exists():
        tc.require_online("set up the converter's Python packages")
        if own:
            if emit:
                emit({"type": "log", "text": "Setting up the converter's own Python (PyTorch for the CPU, about 250 MB)…"})
            tc.pip_install(py, ["torch"], "PyTorch (CPU)", cancel, index_url="https://download.pytorch.org/whl/cpu")
        tc.pip_install(py, PACKAGES, "the converter's packages", cancel)
        marker.write_text("ok", encoding="utf-8")
    return Path(py), script


def _blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def fetch_converter(tag, dest, cancel=None):
    """convert_hf_to_gguf.py, conversion/ and gguf-py/gguf/ of that tag, file by file (about 2 MB), each checked
    against its git blob hash. A whole source archive is ~50x bigger and was cut off mid-download in testing."""
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30, read=60),
                      headers={"Accept": "application/vnd.github+json", "User-Agent": "Aero"}) as c:
        r = c.get(f"https://api.github.com/repos/ggml-org/llama.cpp/git/trees/{tag}", params={"recursive": "1"})
        r.raise_for_status()
        want = [x for x in r.json()["tree"] if x["type"] == "blob" and (
            x["path"] == "convert_hf_to_gguf.py" or x["path"].startswith("conversion/")
            or (x["path"].startswith("gguf-py/gguf/") and "/scripts/" not in x["path"]) or x["path"] == "gguf-py/LICENSE")]
        if not any(x["path"] == "convert_hf_to_gguf.py" for x in want):
            raise RuntimeError(f"llama.cpp {tag} has no convert_hf_to_gguf.py")
        root = dest.resolve()
        for x in want:
            out = (dest / x["path"]).resolve()
            if root not in out.parents:
                continue
            if out.exists() and _blob_sha(out.read_bytes()) == x["sha"]:
                continue
            for attempt in range(5):
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("Cancelled")
                try:
                    data = c.get(f"https://raw.githubusercontent.com/ggml-org/llama.cpp/{tag}/{x['path']}").content
                    if _blob_sha(data) == x["sha"]:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(2 * (attempt + 1))
            else:
                raise RuntimeError(f"Couldn't download {x['path']} from llama.cpp {tag}")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
    (dest / ".complete").write_text(tag, encoding="utf-8")


# ---- the whole job ----------------------------------------------------------------------------------------------

def _calibration():
    """Real text for llama-imatrix, shipped with Aero: its docs and skills (prose, tables, lists) and its own source
    (code), about 400 KB, in a fixed order (no network, no user data). Synthetic word lists gave a 0.6B IQ3_M that
    answered "Bordez" for the capital of France."""
    from .config import PKG_DIR
    from .tuner import bench_prompt
    root = PKG_DIR.parent
    files = sorted((root / "docs").glob("*.md")) + [root / "README.md", root / "CHANGELOG.md"] + \
        sorted((PKG_DIR / "skills_builtin").rglob("SKILL.md")) + sorted(PKG_DIR.glob("*.py"))
    parts, size = [], 0
    for f in files:
        try:
            t = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        t = t[:40000]
        parts.append(t)
        size += len(t)
        if size > 400_000:
            break
    if size < 50_000:
        parts += [bench_prompt(900, s) for s in range(0, 40, 3)]
    parts.append("def merge(a, b):\n    out = []\n    while a and b:\n        out.append(a.pop(0) if a[0] < b[0] else "
                 "b.pop(0))\n    return out + a + b\n")
    parts.append("Step 1: open the browser. Step 2: read the inbox. Step 3: list every meeting with its time and "
                 "place.\n| day | time | meeting |\n|---|---|---|\n| Mon | 09:00 | standup |\n")
    return "\n\n".join(parts)


def run(repo, quant=None, emit=None, cancel=None, keep_source=None):
    """Download repo's original weights, convert, quantize and register the result. Returns the model entry."""
    emit = emit or (lambda e: None)
    s = load_settings()
    keep_source = s.get("keep_quant_source", False) if keep_source is None else keep_source
    info = hf.source_info(repo)
    if not info["weights"]:
        raise RuntimeError("This repo has no safetensors or PyTorch weights to convert.")
    h = hardware.snapshot()
    gpu = bool(h["gpus"])
    budget = (h["vram_total_mb"] - 600) if gpu else 0
    params = info["params"] or int(info["size"] / 2)            # bf16 = 2 bytes per weight
    q, imx, why = (quant, any(x[0] == quant and x[2] for x in LADDER), "chosen by you") if quant else \
        pick(params, budget, h["ram_total_mb"], gpu)
    emit({"type": "log", "text": f"{repo}: {params / 1e9:.1f}B parameters ({info['arch'] or 'unknown architecture'}). "
                                 f"Making {q}: {why}."})
    root = models_dir()
    src_root = root / "_src"
    out_dir = root / (repo.replace("/", "__") + "-GGUF")
    out_dir.mkdir(parents=True, exist_ok=True)
    name = repo.split("/")[-1]
    f16 = out_dir / f"{name}-F16.gguf"
    final = out_dir / f"{name}-{q}.gguf"
    if final.exists():
        return models.add(final, repo=repo, name=f"{name} · {q} (made here)")
    need = info["size"] * (0 if keep_source else 1) + params * 2 + params * dict((x[0], x[1]) for x in LADDER)[q] / 8
    free = shutil.disk_usage(root).free
    if free < need * 1.05:
        raise RuntimeError(f"Not enough disk space: this needs about {need / 2**30:.0f} GB free in {root} "
                           f"({free / 2**30:.0f} GB is).")
    py, script = ensure_converter(emit, cancel)
    # 1. the original weights
    emit({"type": "stage", "stage": "download", "text": f"Downloading the original weights ({info['size'] / 2**30:.1f} GB)…"})
    paths = hf.download(repo, info["files"], emit, cancel, root=src_root)
    src = paths[0].parent
    # 2. convert to a 16-bit GGUF
    if not f16.exists():
        emit({"type": "stage", "stage": "convert", "text": "Converting to GGUF (16-bit)…"})
        outtype = "bf16" if "BF16" in (info["dtypes"] or {}) else "f16"
        # -E -s, not -I: the script imports its own conversion/ package from its folder, which -I leaves off sys.path
        r = tc.run([str(py), "-E", "-s", str(script), str(src), "--outfile", str(f16), "--outtype", outtype],
                   timeout=6 * 3600, cancel=cancel)         # the script puts its own gguf-py first on the path
        if r["rc"] != 0 or not f16.exists():
            tail = (r["err"] or r["out"])[-900:]
            hint = " llama.cpp doesn't support this architecture yet; try AirLLM instead." \
                if re.search(r"not supported|NotImplementedError|Model .* is not supported", tail) else ""
            f16.unlink(missing_ok=True)
            raise RuntimeError("Conversion failed." + hint + "\n" + tail)
    # 3. importance matrix for low-bit quants
    imatrix = None
    if imx:
        imatrix = out_dir / f"{name}.imatrix"
        if not imatrix.exists():
            emit({"type": "stage", "stage": "imatrix", "text": "Measuring which weights matter most (importance matrix)…"})
            calib = out_dir / "calibration.txt"
            calib.write_text(_calibration(), encoding="utf-8")
            exe = _exe("llama-imatrix")
            r = tc.run([str(exe), "-m", str(f16), "-f", str(calib), "-o", str(imatrix), "-ngl", "999" if gpu else "0",
                        "--chunks", "64", "-c", "512"], timeout=6 * 3600, cancel=cancel)
            if r["rc"] != 0 or not imatrix.exists():
                emit({"type": "log", "text": "The importance matrix didn't finish; quantizing without it."})
                imatrix = None
                if q.startswith("IQ1") or q.startswith("IQ2"):
                    q = "Q2_K"
                    final = out_dir / f"{name}-{q}.gguf"
    # 4. quantize
    emit({"type": "stage", "stage": "quantize", "text": f"Quantizing to {q}…"})
    exe = _exe("llama-quantize")
    if not exe:
        raise RuntimeError("llama-quantize is missing from the llama.cpp install.")
    cmd = [str(exe)] + (["--imatrix", str(imatrix)] if imatrix else []) + [str(f16), str(final), q]
    r = tc.run(cmd, timeout=6 * 3600, cancel=cancel)
    if r["rc"] != 0 or not final.exists():
        final.unlink(missing_ok=True)
        raise RuntimeError("Quantizing failed:\n" + (r["err"] or r["out"])[-900:])
    # 5. tidy up: the 16-bit file and (unless kept) the original weights
    f16.unlink(missing_ok=True)
    if not keep_source:
        shutil.rmtree(src, ignore_errors=True)
    emit({"type": "log", "text": f"Made {final.name} ({final.stat().st_size / 2**30:.1f} GB)."})
    return models.add(final, repo=repo, name=f"{name} · {q} (made here)")
