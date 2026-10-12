"""Run models bigger than the GPU: plan where every tensor of a GGUF goes, for a given VRAM budget.

llama.cpp can keep any tensor on the CPU. The plain way (-ngl N) moves whole layers: the output layer and the last
N-1 layers go to the GPU, everything else (attention, feed-forward, the KV cache of those layers) stays in RAM. The
planner also prices two finer splits and picks by measured-style estimates:

  ffn      every layer "on the GPU" (-ngl all) but the feed-forward weights of the first K layers kept in RAM
           (--override-tensor "blk.(0|1|…).ffn_(up|down|gate)=CPU"). Attention and the whole KV cache stay on the
           GPU, so long prompts and long contexts stay fast; the CPU only does the big matrix-vector work it is
           best at. Same VRAM, usually more speed than -ngl for dense models.
  moe      for mixture-of-experts models, the experts of the first K layers in RAM (--n-cpu-moe K); attention,
           shared experts and the router weights stay on the GPU. Experts are most of a MoE model's bytes but only a
           few run per token, which is why a 100B+ MoE runs usefully on a 16 GB card.
  stream   the model is bigger than VRAM plus free RAM: llama.cpp maps the file (mmap) and the OS reads what each
           token needs from disk. It works, slowly; for full-precision checkpoints AirLLM (integrations/models.py)
           streams layer by layer instead.

  draft    any of the above plus a small model of the same family on the GPU (speculative decoding): the big
           model checks several drafted tokens in one pass over its weights, so the weights it reads from RAM are
           shared by several tokens. Measured on an RTX 5080 + DDR5 with Llama-3.3-70B Q6_K: 1.0 tok/s alone, 4.85 with
           Llama-3.2-1B as the draft (docs/V1.2_BIG_MODELS.md). The draft needs the same tokenizer, so it is picked by
           model family (DRAFTS) and downloaded once into models/_drafts.

Plans are estimates from the GGUF's real tensor sizes; validation/bench_bigmodel.py measures them on the hardware.
"""
import re
from pathlib import Path

from . import gguf

GPU_OVERHEAD_MB = 350          # CUDA context, llama-server, small buffers
DRAFT_DIR = "_drafts"
# family -> (repo, quant) of a small model with the same tokenizer (llama.cpp refuses a draft whose vocab differs)
DRAFTS = {"llama3": ("bartowski/Llama-3.2-1B-Instruct-GGUF", "Q8_0"),
          "gemma3": ("unsloth/gemma-3-1b-it-GGUF", "Q8_0"),
          "qwen2": ("Qwen/Qwen2.5-0.5B-Instruct-GGUF", "q8_0"),
          "qwen3": ("unsloth/Qwen3-0.6B-GGUF", "Q8_0")}


def family(meta):
    arch, name = (meta.get("arch") or "").lower(), (meta.get("name") or "").lower()
    if arch == "llama" and re.search(r"llama[- ]?3", name):
        return "llama3"
    if arch.startswith("gemma3"):
        return "gemma3"
    if arch == "qwen2":
        return "qwen2"
    if arch in ("qwen3", "qwen3moe"):
        return "qwen3"
    return None


def draft_for(info, models_root):
    """(repo, quant, local path or None) of the draft model for this model's family, or None."""
    fam = family(info["meta"])
    if not fam or info["total"] < 6 * 2**30:          # small models gain little and the draft costs VRAM
        return None
    repo, quant = DRAFTS[fam]
    d = Path(models_root) / DRAFT_DIR
    hits = [p for p in d.rglob("*.gguf") if quant.lower() in p.name.lower() and repo.split("/")[1].lower().replace(
        "-gguf", "") in str(p).lower().replace("__", "/")] if d.exists() else []
    return repo, quant, (str(hits[0]) if hits else None)


def spec_for(draft_path, n_max=8):
    return ["--spec-type", "draft-simple", "-md", str(draft_path), "-ngld", "99", "--spec-draft-n-max", str(n_max)]
LAYER = re.compile(r"^blk\.(\d+)\.")


def inspect(path):
    """Per-layer bytes by kind, plus the non-layer tensors. Returns a dict the planner and the UI use."""
    tensors = gguf.read_tensors(path)
    meta = gguf.summarize(path)
    layers = {}
    other = {"token_embd": 0, "output": 0, "rest": 0}
    for name, size in tensors:
        m = LAYER.match(name)
        if not m:
            if name.startswith("token_embd"):
                other["token_embd"] += size
            elif name.startswith("output.") or name == "output":
                other["output"] += size
            else:
                other["rest"] += size
            continue
        i = int(m.group(1))
        L = layers.setdefault(i, {"attn": 0, "ffn": 0, "exps": 0, "other": 0})
        if "_exps" in name:
            L["exps"] += size
        elif "attn" in name:
            L["attn"] += size
        elif "ffn_" in name and not any(t in name for t in ("norm", "_inp", "shexp")):
            L["ffn"] += size
        else:
            L["other"] += size
    n = max(layers) + 1 if layers else 0
    if not other["output"]:            # tied embeddings (Gemma, many small models): llama.cpp puts a copy of
        other["output"] = other["token_embd"]      # token_embd on the GPU as the output layer (measured: 1.42 GB, Gemma-3-27B Q8)
        other["tied"] = True
    return {"path": str(path), "meta": meta, "layers": [layers.get(i, {"attn": 0, "ffn": 0, "exps": 0, "other": 0})
                                                          for i in range(n)],
            "other": other, "total": sum(s for _, s in tensors), "moe": any(L["exps"] for L in layers.values())}


def _mb(b):
    return b / 2**20


def kv_layer_mb(info, ctx, kv="q8_0"):
    n = max(1, len(info["layers"]))
    return gguf.kv_mb(info["meta"], ctx, kv) / n


def compute_mb(info, ctx, ub=512):
    from .fit import compute_mb as cm
    return cm(info["meta"], ctx, ub)


def plan(info, budget_mb, ram_free_mb, ctx=8192, kv="q8_0", draft_path=None):
    """Every strategy that fits the budget, best estimate first: [{strategy, cfg, gpu_mb, cpu_mb, kv_gpu_mb, note}].
    draft_path: a same-family draft model; its VRAM is reserved first and every plan gets the speculative args."""
    if draft_path and Path(draft_path).exists():
        dmb = Path(draft_path).stat().st_size / 2**20 + 450          # weights + its KV cache and buffers
        plans = plan(info, budget_mb - dmb, ram_free_mb, ctx, kv)
        for p in plans:
            p["spec"] = spec_for(draft_path)
            p["gpu_mb"] = round(p["gpu_mb"] + dmb)
            p["note"] += f"; plus the draft model {Path(draft_path).name} on the GPU (speculative decoding)"
        return plans
    L = info["layers"]
    n = len(L)
    fixed = compute_mb(info, ctx) + GPU_OVERHEAD_MB
    kvl = kv_layer_mb(info, ctx, kv)
    out_b = info["other"]["output"]
    total = info["total"]
    tied = out_b if info["other"].get("tied") else 0       # the output copy of tied embeddings: on the GPU, and the
                                                           # embedding table stays in RAM as well
    layer_b = [sum(x.values()) for x in L]
    plans = []
    # all on the GPU
    full = _mb(total - info["other"]["token_embd"] + tied) + kvl * n + fixed
    if full <= budget_mb:
        plans.append({"strategy": "gpu", "cfg": {"ngl": 999}, "gpu_mb": round(full), "cpu_mb": round(_mb(info["other"]["token_embd"])),
                      "kv_gpu_mb": round(kvl * n), "note": "everything fits on the GPU"})
        return _finish(plans, info, ram_free_mb)
    # whole layers (llama.cpp: output first, then the last N-1 layers)
    best = None
    for ngl in range(n + 1, 0, -1):
        k = ngl - 1
        on = layer_b[n - k:] if k > 0 else []
        g = _mb(out_b + sum(on)) + kvl * k + fixed
        if g <= budget_mb:
            best = {"strategy": "layers", "cfg": {"ngl": ngl}, "gpu_mb": round(g),
                    "cpu_mb": round(_mb(total - (0 if tied else out_b) - sum(on))), "kv_gpu_mb": round(kvl * k),
                    "note": f"{k} of {n} layers and the output layer on the GPU"}
            break
    if best:
        plans.append(best)
    # feed-forward of the first K layers in RAM, attention and KV of every layer on the GPU
    if not info["moe"]:
        base = _mb(total - info["other"]["token_embd"] + tied) + kvl * n + fixed
        moved = 0
        for k in range(1, n + 1):
            moved += L[k - 1]["ffn"]
            g = base - _mb(moved)
            if g <= budget_mb:
                plans.append({"strategy": "ffn", "cfg": {"ngl": 999, "ot": [ffn_override(range(k))]}, "gpu_mb": round(g),
                              "cpu_mb": round(_mb(moved + info["other"]["token_embd"])), "kv_gpu_mb": round(kvl * n),
                              "note": f"attention and KV of all {n} layers on the GPU, feed-forward of {k} layers in RAM"})
                break
    else:
        base = _mb(total - info["other"]["token_embd"] + tied) + kvl * n + fixed
        moved = 0
        for k in range(1, n + 1):
            moved += L[k - 1]["exps"]
            g = base - _mb(moved)
            if g <= budget_mb:
                plans.append({"strategy": "moe", "cfg": {"ngl": 999, "ncmoe": k}, "gpu_mb": round(g),
                              "cpu_mb": round(_mb(moved + info["other"]["token_embd"])), "kv_gpu_mb": round(kvl * n),
                              "note": f"experts of {k} of {n} layers in RAM, everything else on the GPU"})
                break
    if not plans:
        plans.append({"strategy": "stream", "cfg": {"ngl": 0}, "gpu_mb": round(fixed), "cpu_mb": round(_mb(total)),
                      "kv_gpu_mb": 0, "note": "nothing useful fits on the GPU: CPU only"})
    return _finish(plans, info, ram_free_mb)


def ffn_override(layer_ids):
    ids = "|".join(str(i) for i in layer_ids)
    return f"blk\\.({ids})\\.ffn_(up|down|gate)\\.weight=CPU"


def _finish(plans, info, ram_free_mb):
    for p in plans:
        p["fits_ram"] = p["cpu_mb"] <= ram_free_mb * 0.85
        if not p["fits_ram"]:
            p["note"] += f"; {p['cpu_mb']:,} MB in RAM is more than is free, so part streams from disk (mmap): slow"
            p["cfg"]["no_mmap"] = False
    order = {"gpu": 0, "moe": 1, "ffn": 2, "layers": 3, "stream": 4}
    plans.sort(key=lambda p: (not p["fits_ram"], order[p["strategy"]]))
    return plans


def estimate_tg(p, gpu_gbps=800.0, cpu_gbps=60.0):
    """Rough tokens/s: every generated token reads the weights once (dense); bandwidth-bound."""
    g, c = p["gpu_mb"] / 1024, p["cpu_mb"] / 1024
    t = g / gpu_gbps + c / cpu_gbps
    return round(1 / t, 1) if t > 0 else None
