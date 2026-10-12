"""Create workspace: local image generation with leejet/stable-diffusion.cpp (MIT), the ggml engine for diffusion
models, built into Aero.

storytold/artcraft (MIT/Apache-2.0) is the reference for the workspace (a prompt box, size presets, a gallery of
generations with their settings); its own generation goes to paid cloud services and some of its clients scrape
web apps, so none of that code is used. Here everything runs on this PC.

Model: Z-Image-Turbo (Tongyi-MAI, Apache-2.0), 8 steps. Weights stay in RAM (--offload-to-cpu) and Aero gives the
engine a VRAM budget (--max-vram) from the GPU's free memory, so it fits a 4 GB card (see vram_budget_gb).
Files: the diffusion model as GGUF (leejet/Z-Image-Turbo-GGUF, Q4_K, 3.6 GB), its VAE (Comfy-Org/z_image_turbo,
320 MB) and Qwen3-4B-Instruct-2507 as the text encoder (2.4 GB, the same file Aero's
authority model uses). The engine is stable-diffusion.cpp's Vulkan build (NVIDIA, AMD and Intel GPUs), Metal on
macOS, CPU otherwise.
"""
import json
import platform
import re
import shutil
import sys
import time
from pathlib import Path

from . import Pack, Requirement, register, toolchain as tc
from .. import action_results, attachments, capabilities, hf
from ..config import DATA, IS_WIN, models_dir
from ..tools import tool

OUT = DATA / "images"
ENGINE = tc.ROOT / "sdcpp"
MODEL_DIR = "_create"
FILES = {
    "diffusion": ("leejet/Z-Image-Turbo-GGUF", "z_image_turbo-Q4_K.gguf"),
    "vae": ("Comfy-Org/z_image_turbo", "split_files/vae/ae.safetensors"),
    "llm": ("unsloth/Qwen3-4B-Instruct-2507-GGUF", "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"),
}
SIZES = {"square": (1024, 1024), "portrait": (768, 1280), "landscape": (1280, 768), "small": (512, 512)}


def engine_bin():
    for name in ("sd-cli.exe", "sd.exe") if IS_WIN else ("sd-cli", "sd"):
        hits = sorted(ENGINE.rglob(name)) if ENGINE.exists() else []
        if hits:
            return hits[0]
    return None


def install_engine(emit=None, cancel=None):
    rel = tc.github_release("leejet/stable-diffusion.cpp")
    if IS_WIN:
        pat = r"bin-win-vulkan-x64\.zip$"
    elif sys.platform == "darwin":
        pat = r"bin-Darwin-.*-arm64\.zip$" if platform.machine() == "arm64" else r"bin-Darwin-.*\.zip$"
    else:
        pat = r"bin-Linux-.*-x86_64-vulkan\.zip$"
    a = next((a for a in rel["assets"] if re.search(pat, a["name"])), None)
    if not a:
        raise RuntimeError(f"stable-diffusion.cpp {rel['tag']} has no build for this computer.")
    arc = tc.ROOT / "_dl" / a["name"]
    tc.download(a["url"], arc, emit, cancel, sha256=a.get("digest"), label=a["name"])
    shutil.rmtree(ENGINE, ignore_errors=True)
    tc.extract(arc, ENGINE)
    arc.unlink(missing_ok=True)
    b = engine_bin()
    if not b:
        raise RuntimeError("The stable-diffusion.cpp archive had no sd-cli program.")
    if not IS_WIN:
        b.chmod(0o755)
    tc.mark("sdcpp", version=rel["tag"], build=a["name"])
    return f"stable-diffusion.cpp {rel['tag']} ({a['name']})"


def _local(kind):
    repo, path = FILES[kind]
    p = hf.local_path(repo, path, models_dir() / MODEL_DIR)
    if kind == "llm" and not p.exists():                       # reuse the authority model's copy
        for q in models_dir().rglob(Path(path).name):
            return q
    return p


def install_models(emit=None, cancel=None):
    import threading
    for kind, (repo, path) in FILES.items():
        if _local(kind).exists():
            continue
        hf.download(repo, [path], emit or (lambda e: None), cancel or threading.Event(), models_dir() / MODEL_DIR)
    tc.mark("zimage", version="Z-Image-Turbo Q4_K")
    return "Z-Image-Turbo (diffusion Q4_K, VAE, Qwen3-4B text encoder)"


def _engine_ok():
    b = engine_bin()
    v = tc.installed().get("sdcpp", {}).get("version")
    return bool(b), f"{v} ({b.name})" if b else "not installed"


def _models_ok():
    missing = [k for k in FILES if not _local(k).exists()]
    return not missing, "Z-Image-Turbo ready" if not missing else "missing: " + ", ".join(missing)


NEED_MB = 4600       # free VRAM for the fast setting (6 GiB budget: 4.5 GB used, 14 s); less and the model steps aside


def vram_budget_gb(free_mb=None):
    """stable-diffusion.cpp's --max-vram (GiB) for the first GPU's free memory: free minus 1.2 GB (the process needs
    about that on top of the budget), between 2 and 6. Measured on an RTX 5080, 1024 x 1024, 8 steps (VRAM the process
    used / time): no budget 8.8 GB / 16 s, 6 GiB 4.5 GB / 14 s, 2.5 GiB 3.7 GB / 39 s, 2 GiB 3.2 GB / 57 s; 1.5 GiB
    fails. None when there is no discrete GPU to budget (unified memory, CPU)."""
    if free_mb is None:
        from .. import hardware
        g = [x for x in hardware.gpus() if not x.get("unified")]
        if not g:
            return None
        free_mb = g[0].get("free_mb") or 0
    return round(max(2.0, min(6.0, free_mb / 1024 - 1.2)), 1)


def generate(prompt, width=1024, height=1024, steps=8, seed=-1, cancel=None):
    b = engine_bin()
    if not b:
        raise RuntimeError("stable-diffusion.cpp isn't installed (integration_install image).")
    ok, det = _models_ok()
    if not ok:
        raise RuntimeError(f"The image model isn't downloaded ({det}).")
    OUT.mkdir(parents=True, exist_ok=True)
    stem = time.strftime("%Y%m%d-%H%M%S") + "-" + (re.sub(r"[^a-z0-9]+", "-", prompt.lower())[:40].strip("-") or "image")
    out = OUT / f"{stem}.png"
    w, h = max(256, min(2048, int(width) // 64 * 64)), max(256, min(2048, int(height) // 64 * 64))
    cmd = [str(b), "--diffusion-model", str(_local("diffusion")), "--vae", str(_local("vae")), "--llm",
           str(_local("llm")), "-p", prompt[:2000], "--cfg-scale", "1.0", "--steps", str(max(1, min(50, int(steps)))),
           "-W", str(w), "-H", str(h), "--seed", str(int(seed)), "--offload-to-cpu", "--diffusion-fa", "-o", str(out)]
    from .. import gpu_lend
    t0 = time.time()
    # a model filling the card left 1.2.1 at 8 minutes per image; lend the GPU instead (gpu_lend.py)
    with gpu_lend.lend(NEED_MB) as lent:
        budget = vram_budget_gb()
        if budget:
            cmd[-2:-2] = ["--max-vram", str(budget)]
        r = tc.run(cmd, cwd=b.parent, timeout=1800, cancel=cancel)
    if r["rc"] != 0 or not out.exists():
        raise RuntimeError("stable-diffusion.cpp failed: " + (r["err"] or r["out"])[-800:])
    meta = {"prompt": prompt, "width": w, "height": h, "steps": steps, "seed": seed, "model": "Z-Image-Turbo Q4_K",
            "seconds": round(time.time() - t0, 1), "engine": tc.installed().get("sdcpp", {}).get("build"),
            "vram_budget_gb": budget, "model_unloaded": lent}
    out.with_suffix(".json").write_text(json.dumps(meta), encoding="utf-8")
    return out, meta


@tool("image_generate", "Make an image on this PC from a text prompt (Z-Image-Turbo with stable-diffusion.cpp, 8 "
      "steps). Describe subject, style, lighting and composition; text in quotes is drawn. size: square, portrait, "
      "landscape or small, or width/height. The image appears in the chat and the Create workspace.", "create",
      {"prompt": {"type": "string"}, "size": {"type": "string", "enum": list(SIZES)},
       "width": {"type": "integer"}, "height": {"type": "integer"}, "steps": {"type": "integer"},
       "seed": {"type": "integer"}}, ["prompt"], summary=lambda a: a.get("prompt", "")[:80])
def image_generate(ctx, prompt, size="square", width=None, height=None, steps=8, seed=-1):
    w, h = SIZES.get(size or "square", SIZES["square"])
    try:
        out, meta = generate(prompt, width or w, height or h, steps or 8, -1 if seed is None else seed, ctx.cancel)
    except (RuntimeError, tc.Offline) as e:
        return {"text": str(e), "error": True}
    up = attachments.save_image_bytes(out.read_bytes(), out.name)
    env = action_results.make("file", verified=True, method=f"{out.name} written, {out.stat().st_size:,} bytes",
                              changed={"path": str(out)})
    return action_results.attach({"text": f"Made {out} ({meta['width']}x{meta['height']}, {meta['seconds']} s).",
                                  "image": up["id"] if isinstance(up, dict) else up,
                                  "preview": {"kind": "image", "file": out.name}}, env)


def gallery(limit=60):
    if not OUT.exists():
        return []
    out = []
    for p in sorted(OUT.glob("*.png"), key=lambda p: -p.stat().st_mtime)[:limit]:
        try:
            meta = json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        out.append({"file": p.name, **meta})
    return out


capabilities.register("image_generate", ["image.create"], "local_write", cost="high", verify="the PNG exists")

register(Pack("image", "Image studio", "create",
              "Images from text, made on this PC (Z-Image-Turbo with stable-diffusion.cpp)",
              "https://github.com/leejet/stable-diffusion.cpp (engine) · https://github.com/storytold/artcraft (reference)",
              "MIT (engine) · Apache-2.0 (Z-Image-Turbo, its VAE, Qwen3-4B)",
              triggers=[r"\b(generate|make|create|draw|paint|render|design)\b.{0,40}\b(image|picture|photo|illustration|"
                        r"artwork|logo|icon|poster|wallpaper|concept art|thumbnail)s?\b",
                        r"\b(text[- ]to[- ]image|image generation|stable diffusion|ai art)\b"],
              tools=["image_generate"],
              requirements=[Requirement("engine", "stable-diffusion.cpp (Vulkan build, about 30 MB)", _engine_ok,
                                        install=install_engine, size_mb=30),
                            Requirement("models", "Z-Image-Turbo model files (about 6.3 GB)", _models_ok,
                                        install=install_models, size_mb=6400)],
              panel=lambda: {"gallery": gallery()},
              notes="ArtCraft's cloud generators (and its video/3D tools that depend on them) are not built in: they "
                    "need paid accounts and some scrape services against their terms."))
