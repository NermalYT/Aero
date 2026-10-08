"""Before downloading: how much memory each quant of a model needs, how much context it can hold on
the GPU, and which quant to recommend. Everything that lands in VRAM is counted: the weights, the
vision/audio projector, the KV cache for the context, llama.cpp's compute buffers and CUDA overhead."""
from . import gguf

CTX_STEPS = [4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576]
AGENT_CTX = 32768          # agent work (files, screenshots, tool results) needs about this much
OVERHEAD_MB = 250          # CUDA context + llama-server
RAM_SHARE = 0.7            # how much system RAM may hold offloaded weights


def compute_mb(meta, ctx, ub=512):
    """llama.cpp compute buffers; same formula the tuner calibrates against real measurements."""
    return 300 + ub / 512 * 180 * (meta["n_embd"] / 4096) + ctx / 1024 * 1.5


def need_mb(meta, weights_mb, proj_mb, ctx):
    return weights_mb + proj_mb + gguf.kv_mb(meta, ctx) + compute_mb(meta, ctx) + OVERHEAD_MB


def max_gpu_ctx(meta, weights_mb, proj_mb, budget_mb):
    """Largest context (from the usual steps, capped at the trained context) that fits fully on the GPU."""
    best = 0
    for c in CTX_STEPS + [meta["ctx_train"]]:
        if c <= meta["ctx_train"] and c > best and need_mb(meta, weights_mb, proj_mb, c) <= budget_mb:
            best = c
    return best


def plan_file(f, meta, proj_mb, budget_mb, ram_mb):
    """Fit facts for one quant at one projector size."""
    w = f["size"] / 2**20
    target = min(AGENT_CTX, meta["ctx_train"])
    ctx = max_gpu_ctx(meta, w, proj_mb, budget_mb) if budget_mb > 0 else 0
    out = {"max_ctx": ctx, "need_mb": round(need_mb(meta, w, proj_mb, target)), "target_ctx": target}
    room = budget_mb + ram_mb * RAM_SHARE
    if ctx >= target:
        out["fit"] = "gpu"
    elif ctx >= 4096:
        out["fit"] = "gpu_short"
    elif meta["experts"] and need_mb(meta, w * 0.12, proj_mb, target) <= budget_mb and w <= room:
        out["fit"] = "moe"      # experts in RAM, attention + context on the GPU: still quick
    elif w + proj_mb <= room:
        out["fit"] = "split"
    else:
        out["fit"] = "no"
    return out


def _bpw(f):
    return f["bpw"] or 4.5          # unknown quant and unknown parameter count: assume a mid quant


def _quality(f):
    """Rank by bits per weight, but legacy Q4_0/Q4_1/Q5_0/Q5_1 lose to K- and I-quants of similar size."""
    legacy = f.get("quant", "").upper() in ("Q4_0", "Q4_1", "Q5_0", "Q5_1")
    return _bpw(f) - (0.45 if legacy else 0)


def _best(cands, cap_bpw=8.5):
    cands = [f for f in cands if _bpw(f) <= cap_bpw]
    return max(cands, key=lambda f: (_quality(f), f["size"])) if cands else None


def recommend(files, key, gpu=True):
    """Highest quality first, but only where the whole thing still fits: full GPU with agent-size context,
    then MoE expert offload (still fast), then full GPU with 16k+, then low-bit full GPU, then short context,
    then a split.
    F16/BF16 are never recommended: Q8_0 is indistinguishable and half the size."""
    q = lambda f: f[key]
    tiers = [
        ([f for f in files if q(f)["fit"] == "gpu" and _bpw(f) >= 3.5], 8.5,
         "best quality that runs fully on your GPU with {ctx} context"),
        ([f for f in files if q(f)["fit"] == "moe" and _bpw(f) >= 4.0], 5.0,
         "MoE model: some experts sit in system RAM while attention and {target} context stay on the GPU, so it stays fast"),
        ([f for f in files if q(f)["max_ctx"] >= 16384 and _bpw(f) >= 3.5], 8.5,
         "best quality that runs fully on your GPU; context tops out at {ctx}"),
        ([f for f in files if q(f)["fit"] == "gpu" and _bpw(f) >= 2.5], 8.5,
         "the only quants that fit fully on your GPU with {ctx} context are low-bit, so expect some quality loss"),
        ([f for f in files if q(f)["max_ctx"] >= 8192 and _bpw(f) >= 3.0], 8.5,
         "only a short {ctx} context fits fully on your GPU at this size"),
        ([f for f in files if q(f)["fit"] == "split" and _bpw(f) >= 3.0], 4.85,
         "too big for the GPU alone: some layers run on the CPU, so expect it to be slow"),
    ]
    for cands, cap, why in tiers:
        f = _best(cands, cap)
        if f:
            why = why.format(ctx=_k(q(f)["max_ctx"]), target=_k(q(f)["target_ctx"]))
            return f["name"], why if gpu else why.replace("on your GPU", "in your RAM").replace("for the GPU alone", "for your RAM")
    if files:
        return files[0]["name"], "nothing fits comfortably; this is the smallest file"
    return None, ""


def _k(n):
    return f"{n // 1024}k" if n >= 1024 else str(n)


def plan(files, meta, mmproj, budget_mb, ram_mb, gpu=True):
    """Annotate files in place; returns (recommended name, reason) with and without the projector."""
    proj_mb = (mmproj or {}).get("size", 0) / 2**20
    params = meta.get("params") or 0
    for f in files:
        if not f["bpw"] and params:
            f["bpw"] = round(f["size"] * 8 / params, 2)       # unknown quant name: measure it
        f["with_proj"] = plan_file(f, meta, proj_mb, budget_mb, ram_mb)
        f["text_only"] = plan_file(f, meta, 0, budget_mb, ram_mb)
    rec_text = recommend(files, "text_only", gpu)
    rec = recommend(files, "with_proj", gpu) if mmproj else rec_text
    return {"with_proj": rec, "text_only": rec_text}
