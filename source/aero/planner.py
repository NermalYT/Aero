"""Select (1.2.6): decide how to run any model on this PC before anything is downloaded.

For every quant of a repo the planner prices every way to place it, using the tensor layout of one quant's header
scaled to each file's size (bigmodel.plan does the placement):
  gpu      everything on the GPU
  moe      mixture-of-experts: the experts of some layers in RAM, attention and the rest on the GPU
  ffn      dense: the feed-forward weights of some layers in RAM, attention and the KV cache on the GPU
  layers   whole layers on the CPU
  stream   CPU only, or more than fits in RAM: llama.cpp maps the file and reads the rest from disk while it runs
at 8k, 16k and 32k context with a q8_0 or q4_0 KV cache. Speed is estimated from where the bytes each new token reads
live (VRAM, RAM or disk) and how fast each is on this PC (GPU memory bandwidth from a table of known cards, RAM from
the installed modules); a mixture-of-experts model only reads its active experts. The pick is the best quality that
reaches the minimum speed (Settings, default 5 tokens/s), preferring 32k context for agent work; when nothing is that
fast, the near-fastest plan that runs, with a warning. The tuner then measures and refines it on the real hardware.

AirLLM (layer-by-layer streaming from disk) is the last resort for repos with original weights that llama.cpp can't
convert: for anything llama.cpp runs, a low-bit quant with mmap reads less per token than AirLLM's per-layer reload.
"""
import math
import re
import subprocess

from . import bigmodel
from .config import IS_MAC, IS_WIN

MIN_BPW = 2.4                 # below this (IQ2_XXS, IQ1) answers fall apart; used only when nothing else fits
CTX_STEPS = (8192, 16384, 32768)
KV_TYPES = ("q8_0", "q4_0")
EFF = {"gpu": 0.85, "ram": 0.4, "disk": 0.7}      # share of peak bandwidth reached: RTX 5080 ~0.85; CPU 0.4 (Qwen3-1.7B Q8_0 on a Ryzen 9 9950X3D2 with DDR5: 24.2 tok/s measured)
LAYER_OVERHEAD_S = 2e-5       # per layer per token: kernel launches and synchronisation

# GPU memory bandwidth in GB/s, by model name
_GPU_BW = [
    (r"RTX\s*5090", 1792), (r"RTX\s*5080", 960), (r"RTX\s*5070\s*Ti", 896), (r"RTX\s*5070", 672),
    (r"RTX\s*5060\s*Ti", 448), (r"RTX\s*5060", 448), (r"RTX\s*5050", 320),
    (r"RTX\s*4090", 1008), (r"RTX\s*4080\s*Super", 736), (r"RTX\s*4080", 717), (r"RTX\s*4070\s*Ti\s*Super", 672),
    (r"RTX\s*4070\s*Ti", 504), (r"RTX\s*4070", 504), (r"RTX\s*4060\s*Ti", 288), (r"RTX\s*4060", 272),
    (r"RTX\s*4050", 192), (r"RTX\s*3090\s*Ti", 1008), (r"RTX\s*3090", 936), (r"RTX\s*3080\s*Ti", 912),
    (r"RTX\s*3080", 760), (r"RTX\s*3070\s*Ti", 608), (r"RTX\s*3070", 448), (r"RTX\s*3060\s*Ti", 448),
    (r"RTX\s*3060", 360), (r"RTX\s*3050", 224), (r"RTX\s*2080\s*Ti", 616), (r"RTX\s*2080", 448),
    (r"RTX\s*2070", 448), (r"RTX\s*2060", 336), (r"GTX\s*1660\s*(Ti|Super)", 336), (r"GTX\s*1660", 192),
    (r"GTX\s*1650", 128), (r"GTX\s*1080\s*Ti", 484), (r"GTX\s*1080", 320), (r"GTX\s*1070", 256),
    (r"GTX\s*1060", 192), (r"RTX\s*A6000|RTX\s*6000\s*Ada", 768), (r"RTX\s*A5000", 768), (r"RTX\s*A4000", 448),
    (r"\bA100\b", 1555), (r"\bH100\b", 2039), (r"\bL40S?\b", 864), (r"\bT4\b", 320),
    (r"RX\s*9070\s*XT|RX\s*9070", 640), (r"RX\s*7900\s*XTX", 960), (r"RX\s*7900\s*XT", 800),
    (r"RX\s*7900\s*GRE", 576), (r"RX\s*7800\s*XT", 624), (r"RX\s*7700\s*XT", 432), (r"RX\s*7600", 288),
    (r"RX\s*6950\s*XT|RX\s*6900\s*XT|RX\s*6800", 512), (r"RX\s*6750|RX\s*6700", 384), (r"RX\s*6650|RX\s*6600", 224),
    (r"Arc\s*A770", 560), (r"Arc\s*A750", 512), (r"Arc\s*B580", 456), (r"Arc\s*B570", 380),
    (r"M4\s*Max", 546), (r"M4\s*Pro", 273), (r"\bM4\b", 120), (r"M3\s*Max", 400), (r"M3\s*Pro", 150),
    (r"\bM3\b", 100), (r"M2\s*Ultra|M1\s*Ultra", 800), (r"M2\s*Max|M1\s*Max", 400), (r"M2\s*Pro|M1\s*Pro", 200),
    (r"\bM2\b", 100), (r"\bM1\b", 68),
]


def gpu_gbps(h):
    """Memory bandwidth of the main GPU: known cards by name (laptop parts at 70%), else by VRAM size."""
    g = (h.get("gpus") or [{}])[0]
    name = g.get("name") or h.get("gpu_name") or ""
    for pat, bw in _GPU_BW:
        if re.search(pat, name, re.I):
            return bw * (0.7 if re.search(r"laptop|mobile|max-q", name, re.I) else 1.0)
    mb = g.get("total_mb") or h.get("vram_total_mb") or 0
    for cap, bw in ((4200, 128), (6300, 192), (8400, 288), (12500, 360), (16500, 448), (24600, 900)):
        if mb <= cap:
            return bw
    return 1000


_RAM = {"gbps": None}


def ram_gbps():
    """Effective RAM bandwidth in GB/s: on Windows from the installed modules (speed x channels), else an average
    dual-channel desktop. Read once per run."""
    if _RAM["gbps"]:
        return _RAM["gbps"]
    gbps = 45.0
    if IS_WIN:
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "Get-CimInstance Win32_PhysicalMemory | ForEach-Object { $_.ConfiguredClockSpeed }"],
                                 capture_output=True, text=True, timeout=15, creationflags=0x08000000).stdout
            speeds = [int(x) for x in out.split() if x.strip().isdigit() and int(x) > 0]
            if speeds:
                channels = 2 if len(speeds) >= 2 else 1               # consumer boards: 2 channels at most
                gbps = min(speeds) * 8 * channels / 1000
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    elif IS_MAC:
        gbps = 100.0                                                  # unified memory: the GPU table covers it
    _RAM["gbps"] = gbps
    return gbps


DISK_GBPS = 1.5               # NVMe sequential reads, roughly; only matters for plans that don't fit in RAM


def bandwidth(h):
    return {"gpu": gpu_gbps(h) if h.get("gpus") else 0, "ram": ram_gbps(), "disk": DISK_GBPS}


# ---- layout ---------------------------------------------------------------------------------------------------

def analytic_layout(meta, total_bytes):
    """A tensor layout when no header is readable: embeddings and output ~4% each, layers share the rest, the
    feed-forward ~80% of a dense layer, the experts ~92% of a mixture-of-experts layer."""
    n = max(1, meta.get("n_layer") or 32)
    embd = out = total_bytes * 0.04
    per = (total_bytes - embd - out) / n
    if meta.get("experts"):
        L = {"attn": per * 0.06, "ffn": 0, "exps": per * 0.92, "other": per * 0.02}
    else:
        L = {"attn": per * 0.19, "ffn": per * 0.80, "exps": 0, "other": per * 0.01}
    return {"path": "", "meta": meta, "layers": [dict(L) for _ in range(n)],
            "other": {"token_embd": embd, "output": out, "rest": 0}, "total": total_bytes,
            "moe": bool(meta.get("experts"))}


def scaled(info, total_bytes):
    """The same layout for another quant of the model: every tensor scaled to the new file size."""
    k = total_bytes / max(1, info["total"])
    return {**info, "layers": [{a: b * k for a, b in L.items()} for L in info["layers"]],
            "other": {a: (b * k if isinstance(b, (int, float)) and not isinstance(b, bool) else b)
                      for a, b in info["other"].items()},
            "total": total_bytes}


# ---- speed ----------------------------------------------------------------------------------------------------

def speed(p, info, bw, ram_free_mb, ctx, kv):
    """Estimated tokens/s for one placement: the bytes each new token reads, from where they live."""
    meta = info["meta"]
    mb = 1 / 2**20
    embd = info["other"]["token_embd"] * mb
    fixed = bigmodel.compute_mb(info, ctx) + bigmodel.GPU_OVERHEAD_MB
    gpu_w = max(0.0, p["gpu_mb"] - p["kv_gpu_mb"] - (fixed if p["strategy"] != "stream" else p["gpu_mb"]))
    cpu_w = max(0.0, p["cpu_mb"] - embd)
    layers_b = sum(sum(L.values()) for L in info["layers"]) or 1
    exps = sum(L["exps"] for L in info["layers"])
    fe = exps / layers_b
    act = meta["experts_used"] / meta["experts"] if meta.get("experts") and meta.get("experts_used") else 1.0
    if p["strategy"] == "moe":
        cpu_read = cpu_w * act                                      # the experts in RAM: only the active ones
        gpu_exps = max(0.0, exps * mb - cpu_w)
        gpu_read = max(0.0, gpu_w - gpu_exps) + gpu_exps * act
    else:
        gpu_read = gpu_w * (1 - fe) + gpu_w * fe * act
        cpu_read = cpu_w * (1 - fe) + cpu_w * fe * act
    fit_ram = max(0.0, ram_free_mb * 0.85)
    disk_read = 0.0
    if cpu_w > fit_ram:                                              # the rest is read from disk (mmap)
        over = (cpu_w - fit_ram) / cpu_w
        disk_read, cpu_read = cpu_read * over, cpu_read * (1 - over)
    kv_read = p["kv_gpu_mb"] * 0.5                                   # half the context filled on average
    t = 0.0
    if bw["gpu"]:
        t += (gpu_read + kv_read) / 1024 / (bw["gpu"] * EFF["gpu"])
    else:
        cpu_read += gpu_read + kv_read
    t += cpu_read / 1024 / (bw["ram"] * EFF["ram"]) + disk_read / 1024 / (bw["disk"] * EFF["disk"])
    t += len(info["layers"]) * LAYER_OVERHEAD_S
    return round(1 / t, 1) if t > 0 else 0.0, round(disk_read)


# ---- the plan -------------------------------------------------------------------------------------------------

def _bpw(f, params):
    if f.get("bpw"):
        return f["bpw"]
    return round(f["size"] * 8 / params, 2) if params else 4.5


def candidates(files, template, h, budget_mb, ram_free_mb, proj_mb=0.0):
    """Every (quant, placement, context, KV type) that loads, with its estimated speed."""
    meta = template["meta"]
    bw = bandwidth(h)
    ctx_train = meta.get("ctx_train") or 4096
    ctxs = sorted({min(c, ctx_train) for c in CTX_STEPS})
    out = []
    gpu_budget = budget_mb - proj_mb if h.get("gpus") else 0
    for f in files:
        info = scaled(template, f["size"])
        bpw = _bpw(f, meta.get("params"))
        mtp = bool(meta.get("mtp_layers")) or bool(re.search(r"(^|[-_.])MTP([-_.]|$)", f["name"], re.I))
        for ctx in ctxs:
            for kv in KV_TYPES:
                for p in bigmodel.plan(info, gpu_budget, ram_free_mb, ctx, kv):
                    tg, disk = speed(p, info, bw, ram_free_mb, ctx, kv)
                    out.append({"file": f["name"], "quant": f.get("quant") or "", "bpw": bpw, "size": f["size"],
                                "parts": f.get("parts") or [f["name"]], "strategy": p["strategy"], "cfg": p["cfg"],
                                "ctx": ctx, "kv": kv, "gpu_mb": p["gpu_mb"] + round(proj_mb), "cpu_mb": p["cpu_mb"],
                                "disk_mb": disk, "fits_ram": p["fits_ram"] and not disk, "est_tg": tg,
                                "quality": round(quality(bpw, f.get("quant", "")), 3),
                                "note": p["note"], "mtp": mtp})
    return out


# answer quality by bits per weight, from published perplexity / KL-divergence comparisons of llama.cpp quants:
# little lost down to ~4.5 bits, more below 4, a lot below 3 (piecewise linear between these points)
_QUALITY = ((2.0, 0.35), (2.5, 0.52), (3.0, 0.68), (3.5, 0.76), (4.0, 0.84), (4.5, 0.89), (5.0, 0.93), (5.5, 0.95),
            (6.5, 0.98), (8.5, 1.0))
FAST_TPS = 40.0               # beyond this, more speed adds little in chat or agent work


def quality(bpw, quant=""):
    legacy = (quant or "").upper() in ("Q4_0", "Q4_1", "Q5_0", "Q5_1")
    b = bpw - (0.45 if legacy else 0)
    if b <= _QUALITY[0][0]:
        return _QUALITY[0][1]
    for (x0, y0), (x1, y1) in zip(_QUALITY, _QUALITY[1:]):
        if b <= x1:
            return y0 + (y1 - y0) * (b - x0) / (x1 - x0)
    return 1.0


AERO_PROMPT_TOKENS = 7400     # Aero's system prompt and tool list (measured: 7,346 tokens cached on the first turn)


def score(c, min_tps, agent_ctx):
    """How good a plan is to use: answer quality and speed (log scale from the minimum speed to FAST_TPS), plus a
    bonus for agent-size context and a q8_0 KV cache, and a penalty for a context that Aero's own prompt nearly fills."""
    q = quality(c["bpw"], c["quant"])
    lo = max(0.5, min_tps)
    hi = max(FAST_TPS, lo * 2)
    s = min(1.0, max(0.0, math.log(max(c["est_tg"], 1e-3) / lo) / math.log(hi / lo)))
    ctx = 0.06 if c["ctx"] >= agent_ctx else (-0.12 if c["ctx"] < 2 * AERO_PROMPT_TOKENS else 0)
    return 0.6 * q + 0.4 * s + ctx + (0.02 if c["kv"] == "q8_0" else 0)


def choose(cands, min_tps, ctx_train):
    """(pick, level): the best score among plans that fit in RAM and reach min_tps; else, among those that fit in
    RAM, the best score within 80% of the fastest; else the fastest at all (it streams from disk)."""
    if not cands:
        return None, "none"
    usable = [c for c in cands if c["bpw"] >= MIN_BPW] or cands
    agent = min(32768, ctx_train)
    for c in usable:
        c["score"] = round(score(c, min_tps, agent), 4)
    fits = [c for c in usable if c["fits_ram"]]
    fast = [c for c in fits if c["est_tg"] >= min_tps]
    if fast:
        return max(fast, key=lambda c: (c["score"], c["est_tg"])), "ok"
    if fits:
        top = max(c["est_tg"] for c in fits)
        return max([c for c in fits if c["est_tg"] >= top * 0.8], key=lambda c: (c["score"], c["est_tg"])), "slow"
    return max(usable, key=lambda c: (c["est_tg"], c["score"])), "very_slow"


def summary(c):
    where = {"gpu": "all on the GPU", "moe": "experts partly in RAM, the rest on the GPU",
             "ffn": "attention on the GPU, part of the feed-forward in RAM", "layers": "split across GPU and CPU",
             "stream": "on the CPU"}[c["strategy"]]
    s = (f"{c['quant'] or c['file']} ({c['bpw']:g} bits/weight), {where}, {c['ctx'] // 1024}k context, "
         f"{c['kv']} KV cache: about {c['est_tg']:g} tokens/s")
    if c["mtp"]:
        s += " before speculative decoding (MTP usually adds 20-40%)"
    return s


def warning_for(pick, level):
    if level == "slow":
        return (f"This model is big for this PC: about {pick['est_tg']:g} tokens/s. It works, but replies will be "
                "slow; a smaller model of the same family would be much faster.")
    if level == "very_slow":
        return (f"This model doesn't fit in your RAM, so parts of it are read from disk while it runs: about "
                f"{pick['est_tg']:g} tokens/s. Consider a smaller model.")
    return None


def plan(files, template, h, budget_mb, ram_free_mb, min_tps=5.0, proj_mb=0.0):
    """The Select decision for a list of quant files. With a projector (vision), the plan keeps it only when that
    doesn't make the pick slower class."""
    ctx_train = template["meta"].get("ctx_train") or 4096
    base = candidates(files, template, h, budget_mb, ram_free_mb, 0.0)
    pick, level = choose(base, min_tps, ctx_train)
    with_proj = False
    if proj_mb and pick:
        wp = candidates(files, template, h, budget_mb, ram_free_mb, proj_mb)
        p2, l2 = choose(wp, min_tps, ctx_train)
        rank = {"ok": 0, "slow": 1, "very_slow": 2, "none": 3}
        if p2 and rank[l2] <= rank[level] and p2["quality"] >= pick["quality"] - 0.06:
            pick, level, base, with_proj = p2, l2, wp, True
    best = {}
    for c in sorted(base, key=lambda c: (c["fits_ram"], c["est_tg"] >= min_tps, c["quality"], c["est_tg"]),
                    reverse=True):
        best.setdefault(c["file"], c)
    others = sorted(best.values(), key=lambda c: (-c["quality"], -c["est_tg"]))[:8]
    return {"pick": pick, "level": level, "with_proj": with_proj, "warning": warning_for(pick, level) if pick else
            "No quant of this model could be planned.", "summary": summary(pick) if pick else "",
            "options": others, "budget_mb": round(budget_mb), "ram_free_mb": round(ram_free_mb),
            "bandwidth": bandwidth(h)}


def template_for(meta, tensors=None, total_bytes=0):
    """The layout every quant is scaled from: the real tensor table when the header was readable."""
    if tensors:
        info = bigmodel.layout(tensors, meta)
        if info["layers"] and info["total"]:
            return info
    return analytic_layout(meta, total_bytes or meta.get("size_bytes") or 1)


def size_b(name):
    """Parameter count in billions from a repo or file name ("Qwen3-30B-A3B" -> 30), or 0."""
    m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])", name or "")
    return float(m.group(1)) if m else 0.0


def family_query(repo):
    """Search words for smaller models of the same family: the repo name without its size and GGUF tags."""
    name = repo.split("/")[-1]
    name = re.sub(r"(?i)[-_.]?(gguf|i1|imatrix|instruct|it|chat|mtp)\b", "", name)
    name = re.sub(r"(?<![\d.])\d+(?:\.\d+)?\s*[bB](?:[-_]?A\d+(?:\.\d+)?[bB])?(?![a-zA-Z])", "", name)
    return re.sub(r"[-_.]+", " ", name).strip() or name


def ram_for_weights(h):
    """RAM the model's offloaded weights may take: what's free now, less 2 GB for Aero, the browser and the OS."""
    free = h.get("ram_avail_mb") or 0
    return max(0, min(free, (h.get("ram_total_mb") or 0) * 0.85) - 2048)


def fmt_gb(mb):
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{math.ceil(mb)} MB"
