"""The models Aero offers to download: CPU decision routers and GPU main models, with sourced scores.

Scores are copied from the cited pages as of 2026-10-08. "Vendor" means the model's maker reported it.
Router speeds were measured with llama-bench (llama.cpp b11476, CPU only) on one reference PC (Ryzen 9 9950X3D2,
DDR5) on 2026-10-08; the updater and Settings → Router re-measure the chosen router on whatever PC runs it.
Main-model speeds name the GPU they were measured on. plan() fits each model to the PC it runs on (which quant,
GPU / MoE offload / CPU), and the tuner measures the real speed on first load.
AA = Artificial Analysis Intelligence Index v4.3.2 (higher is smarter; the scale tops out near 70 for frontier
cloud models). Router models are too small for most AA rankings, so their tool-use scores are shown instead.
"""
import re

# ------------------------------------------------------------------------------------------------ routers (CPU)

ROUTERS = [
    {
        "id": "minicpm5-2b-q4", "name": "MiniCPM5-2B", "org": "OpenBMB", "released": "2026-09",
        "params": "2B dense", "license": "Apache-2.0",
        "repo": "openbmb/MiniCPM5-2B-GGUF", "file": "MiniCPM5-2B-Q4_K_M.gguf", "quant": "Q4_K_M", "size_gb": 1.56,
        "recommended": True, "tag": "Best tool picking (newest)",
        "scores": [
            {"name": "BFCL v4 (tool calling)", "value": "66.6", "src": "https://huggingface.co/OpenBMB/MiniCPM5-2B",
             "note": "vendor"},
            {"name": "IFEval / IFBench (instructions)", "value": "86.7 / 66.3", "src": "https://huggingface.co/OpenBMB/MiniCPM5-2B",
             "note": "vendor"},
            {"name": "τ²-Bench Telecom", "value": "97.1", "src": "https://huggingface.co/OpenBMB/MiniCPM5-2B", "note": "vendor"},
            {"name": "AA index v4.2", "value": "15", "src": "https://artificialanalysis.ai/models/open-source/small"},
        ],
        "speed_est": "46.2 tok/s out (12 threads) · 1,341 tok/s prompt (32 threads) · ≈2.8 s per decision, measured on a Ryzen 9 9950X3D2",
        "measured": {"tg": 46.2, "gen_threads": 12, "pp": 1340.9, "pp_threads": 32, "decision_ms": 2821, "cpu": "Ryzen 9 9950X3D2", "llama_cpp": "b11476", "date": "2026-10-08"},
        "notes": "Newest and best published tool-selection score under 3B. Plain transformer, so the cached tool "
                 "catalog is reused perfectly between requests. Thinks by default; Aero's JSON grammar and "
                 "reasoning budget 0 keep it to a straight answer. Scores are the vendor's own, not yet independently checked.",
    },
    {
        "id": "minicpm5-2b-q8", "name": "MiniCPM5-2B (Q8_0, sharper)", "org": "OpenBMB", "released": "2026-09",
        "params": "2B dense", "license": "Apache-2.0",
        "repo": "openbmb/MiniCPM5-2B-GGUF", "file": "MiniCPM5-2B-Q8_0.gguf", "quant": "Q8_0", "size_gb": 2.68,
        "tag": "Same model, near-lossless",
        "scores": [{"name": "BFCL v4 (tool calling)", "value": "66.6", "src": "https://huggingface.co/OpenBMB/MiniCPM5-2B",
                    "note": "vendor, full precision"}],
        "speed_est": "26.9 tok/s out (10 threads) · 604 tok/s prompt (32 threads) · ≈5.0 s per decision, measured on a Ryzen 9 9950X3D2",
        "measured": {"tg": 26.9, "gen_threads": 10, "pp": 604.2, "pp_threads": 32, "decision_ms": 4957, "cpu": "Ryzen 9 9950X3D2", "llama_cpp": "b11476", "date": "2026-10-08"},
        "notes": "8-bit weights lose almost nothing, but it generates about 40% slower than Q4_K_M (26.9 vs 46.2 tok/s measured) because each token reads 2.7 GB.",
    },
    {
        "id": "granite-4.2-3b", "name": "Granite 4.2 3B", "org": "IBM", "released": "2026-08",
        "params": "3B dense", "license": "Apache-2.0",
        "repo": "ibm-granite/granite-4.2-3b-GGUF", "file": "granite-4.2-3b-Q4_K_M.gguf", "quant": "Q4_K_M", "size_gb": 2.24,
        "tag": "Safest instruction follower",
        "scores": [
            {"name": "IFBench (instructions)", "value": "74.33", "src": "https://huggingface.co/ibm-granite/granite-4.2-3b",
             "note": "vendor"},
            {"name": "IFEval", "value": "93.7", "src": "https://huggingface.co/OpenBMB/MiniCPM5-2B", "note": "measured by OpenBMB"},
            {"name": "BFCL v4 (tool calling)", "value": "52.41", "src": "https://huggingface.co/ibm-granite/granite-4.2-3b",
             "note": "vendor"},
            {"name": "AA index v4.2", "value": "11", "src": "https://artificialanalysis.ai/models/open-source/small"},
        ],
        "speed_est": "30.4 tok/s out (12 threads) · 826 tok/s prompt (32 threads) · ≈4.3 s per decision, measured on a Ryzen 9 9950X3D2",
        "measured": {"tg": 30.4, "gen_threads": 12, "pp": 826.3, "pp_threads": 32, "decision_ms": 4310, "cpu": "Ryzen 9 9950X3D2", "llama_cpp": "b11476", "date": "2026-10-08"},
        "notes": "Best instruction-following score in its class and a documented thinking off-switch. Plain "
                 "transformer (good cache reuse). Slightly weaker at picking tools than MiniCPM5.",
    },
    {
        "id": "lfm2.5-1.2b", "name": "LFM2.5-1.2B-Instruct", "org": "Liquid AI", "released": "2026-01",
        "params": "1.2B hybrid", "license": "LFM Open License v1.0 (free under $10M revenue)",
        "repo": "LiquidAI/LFM2.5-1.2B-Instruct-GGUF", "file": "LFM2.5-1.2B-Instruct-Q8_0.gguf", "quant": "Q8_0",
        "size_gb": 1.25, "tag": "Fastest",
        "scores": [
            {"name": "IFEval / IFBench", "value": "86.23 / 47.33", "src": "https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct",
             "note": "vendor"},
            {"name": "BFCL v3 (tool calling)", "value": "49.12", "src": "https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct",
             "note": "vendor"},
        ],
        "speed_est": "55.9 tok/s out (10 threads) · 1,177 tok/s prompt (32 threads) · ≈2.4 s per decision, measured on a Ryzen 9 9950X3D2",
        "measured": {"tg": 55.9, "gen_threads": 10, "pp": 1177.0, "pp_threads": 32, "decision_ms": 2402, "cpu": "Ryzen 9 9950X3D2", "llama_cpp": "b11476", "date": "2026-10-08"},
        "notes": "The fastest decisions in this list. Weaker plans and tool picks than the 2-3B models. Hybrid "
                 "conv/attention layers can reuse the cached prompt less reliably.",
    },
    {
        "id": "lfm2.5-2.6b", "name": "LFM2.5-2.6B", "org": "Liquid AI", "released": "2026",
        "params": "2.6B hybrid", "license": "LFM Open License v1.0 (free under $10M revenue)",
        "repo": "LiquidAI/LFM2.5-2.6B-GGUF", "file": "LFM2.5-2.6B-Q4_K_M.gguf", "quant": "Q4_K_M", "size_gb": 1.67,
        "tag": "Fast, structured output",
        "scores": [
            {"name": "BFCL v4 (tool calling)", "value": "56.88", "src": "https://huggingface.co/LiquidAI/LFM2.5-2.6B",
             "note": "vendor"},
            {"name": "IFStruct / IFEval", "value": "85.49 / 93.4", "src": "https://huggingface.co/LiquidAI/LFM2.5-2.6B",
             "note": "IFEval measured by OpenBMB"},
        ],
        "speed_est": "39.1 tok/s out (10 threads) · 1,083 tok/s prompt (32 threads) · ≈3.3 s per decision, measured on a Ryzen 9 9950X3D2",
        "measured": {"tg": 39.1, "gen_threads": 10, "pp": 1082.9, "pp_threads": 32, "decision_ms": 3346, "cpu": "Ryzen 9 9950X3D2", "llama_cpp": "b11476", "date": "2026-10-08"},
        "notes": "Strong at structured output. Always reasons first, which the JSON grammar suppresses. Hybrid "
                 "architecture: prompt-cache reuse is less reliable than in plain transformers.",
    },
    {
        "id": "qwen3.5-4b", "name": "Qwen3.5-4B", "org": "Alibaba Qwen", "released": "2026",
        "params": "4B hybrid", "license": "Apache-2.0",
        "repo": "unsloth/Qwen3.5-4B-GGUF", "file": "Qwen3.5-4B-Q4_K_M.gguf", "quant": "Q4_K_M", "size_gb": 2.74,
        "tag": "Best independent tool test",
        "scores": [
            {"name": "Independent 40-case tool-calling test", "value": "97.5% (1st of 13)",
             "src": "https://www.jdhodges.com/blog/local-llms-on-tool-calling-2026-pt1-local-lm/", "note": "independent"},
            {"name": "IFEval / τ²-Bench", "value": "89.8 / 79.9", "src": "https://huggingface.co/Qwen/Qwen3.5-4B", "note": "vendor"},
            {"name": "BFCL v4", "value": "50.3", "src": "https://huggingface.co/Qwen/Qwen3.5-4B", "note": "vendor"},
        ],
        "speed_est": "20.1 tok/s out (10 threads) · 555 tok/s prompt (32 threads) · ≈6.5 s per decision, measured on a Ryzen 9 9950X3D2",
        "measured": {"tg": 20.1, "gen_threads": 10, "pp": 555.1, "pp_threads": 32, "decision_ms": 6511, "cpu": "Ryzen 9 9950X3D2", "llama_cpp": "b11476", "date": "2026-10-08"},
        "notes": "The most proven small tool caller, but about half the speed of the 2B models. Gated-DeltaNet "
                 "hybrid: llama.cpp issue #24587 reports it re-reading the whole prompt, which hurts on CPU.",
    },
    {
        "id": "qwen3-4b-2507", "name": "Qwen3-4B-Instruct-2507", "org": "Alibaba Qwen", "released": "2025-08",
        "params": "4B dense", "license": "Apache-2.0",
        "repo": "unsloth/Qwen3-4B-Instruct-2507-GGUF", "file": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf", "quant": "Q4_K_M",
        "size_gb": 2.50, "tag": "Verified on the BFCL board",
        "scores": [
            {"name": "BFCL v4 live / non-live accuracy", "value": "76.39% / 87.88%",
             "src": "https://gorilla.cs.berkeley.edu/leaderboard.html", "note": "official board"},
            {"name": "BFCL v4 irrelevance detection", "value": "84.93%", "src": "https://gorilla.cs.berkeley.edu/leaderboard.html"},
        ],
        "speed_est": "25.6 tok/s out (10 threads) · 728 tok/s prompt (32 threads) · ≈5.1 s per decision, measured on a Ryzen 9 9950X3D2",
        "measured": {"tg": 25.6, "gen_threads": 10, "pp": 727.9, "pp_threads": 32, "decision_ms": 5100, "cpu": "Ryzen 9 9950X3D2", "llama_cpp": "b11476", "date": "2026-10-08"},
        "notes": "Older but independently verified, never thinks, plain transformer. A dependable fallback.",
    },
    {
        "id": "nemotron3-nano-4b", "name": "Nemotron 3 Nano 4B", "org": "NVIDIA", "released": "2026",
        "params": "4B Mamba hybrid", "license": "NVIDIA Open Model License",
        "repo": "nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF", "file": "NVIDIA-Nemotron3-Nano-4B-Q4_K_M.gguf", "quant": "Q4_K_M",
        "size_gb": 2.84, "tag": "Cheap long prompts",
        "scores": [
            {"name": "BFCL v3 (reasoning off)", "value": "61.1", "src": "https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-4B-BF16",
             "note": "vendor"},
            {"name": "Independent 40-case tool-calling test", "value": "95.0%", "src": "https://www.jdhodges.com/blog/local-llms-on-tool-calling-2026-pt1-local-lm/", "note": "independent"},
        ],
        "speed_est": "23.4 tok/s out (12 threads) · 361 tok/s prompt (32 threads) · ≈6.0 s per decision, measured on a Ryzen 9 9950X3D2",
        "measured": {"tg": 23.4, "gen_threads": 12, "pp": 361.4, "pp_threads": 32, "decision_ms": 5958, "cpu": "Ryzen 9 9950X3D2", "llama_cpp": "b11476", "date": "2026-10-08"},
        "notes": "Mostly Mamba-2 layers with 4 attention layers, so long prompts are cheap, but the cache is reused less reliably.",
    },
]

# Kept out of the list on purpose (see the report): Jev (hosted API only, not local); Mapika/decider-4b
# (Jev-class, but only scores fixed options and cannot write a plan); FunctionGemma 270M (needs fine-tuning);
# xLAM-2-3b (non-commercial licence); Gemma 4 E2B (weak tool scores).

# ------------------------------------------------------------------------------------------------ main models (GPU)

MAIN_MODELS = [
    {
        "id": "qwen3.8-27b", "name": "Qwen3.8-27B", "org": "Alibaba Qwen", "released": "2026-08", "params": "27B dense",
        "total_b": 27, "active_b": 27, "license": "Apache-2.0", "vision": True, "repo": "unsloth/Qwen3.8-27B-GGUF",
        "quant": "UD-IQ3_S", "size_gb": 12.04,
        "aa": 34, "aa_note": "xhigh, AA estimate; 28 at medium", "tag": "Smartest for 16-24 GB GPUs",
        "aa_src": "https://artificialanalysis.ai/models/qwen3-8-27b-medium",
        "speed": "68-74 tok/s measured on an RTX 5070 Ti with the MTP drafter",
        "notes": "Highest AA score in this list that runs fully on a 16 GB card (UD-IQ3_S); 24 GB cards get a "
                 "5-bit quant. Vision, strong agentic tool use, thinking with effort levels. Needs llama.cpp b10450+.",
    },
    {
        "id": "qwen3.6-35b-a3b", "name": "Qwen3.6-35B-A3B", "org": "Alibaba Qwen", "released": "2026-04", "params": "35B MoE, 3B active",
        "total_b": 35, "active_b": 3, "moe": True, "license": "Apache-2.0", "vision": True,
        "repo": "unsloth/Qwen3.6-35B-A3B-GGUF", "quant": "UD-IQ3_XXS", "size_gb": 13.21,
        "aa": 18, "aa_src": "https://artificialanalysis.ai/models/qwen3-6-35b-a3b", "tag": "Fastest smart model",
        "speed": "150.6 tok/s measured on an RTX 5080 (fully on the GPU)",
        "notes": "Only 3B parameters work per token, so it stays quick even when some experts sit in system RAM: "
                 "the best pick for 8-12 GB cards with 32 GB of RAM. SWE-bench Verified 73.4 (vendor).",
    },
    {
        "id": "gemma-4-26b-a4b", "name": "Gemma 4 26B-A4B", "org": "Google", "released": "2026-04", "params": "25B MoE, 3.8B active",
        "total_b": 25, "active_b": 3.8, "moe": True, "license": "Apache-2.0", "vision": True,
        "repo": "unsloth/gemma-4-26B-A4B-it-GGUF", "quant": "UD-IQ4_XS", "size_gb": 13.60,
        "aa": 17, "aa_note": "AA estimate", "aa_src": "https://artificialanalysis.ai/models/gemma-4-26b-a4b",
        "tag": "Fast, good vision",
        "speed": "84 tok/s measured on an RTX 5060 Ti",
        "notes": "Solid vision and 256K context. Its chat template needed fixes; Aero downloads the current GGUF.",
    },
    {
        "id": "muse-glimmer-30b", "name": "Muse Glimmer 30B", "org": "Meta", "released": "2026-08", "params": "30B dense",
        "total_b": 30, "active_b": 30, "license": "Apache-2.0", "vision": True, "repo": "unsloth/Muse-Glimmer-30B-GGUF",
        "quant": "UD-IQ3_XXS", "size_gb": 13.1,
        "aa": 17, "aa_src": "https://artificialanalysis.ai/models/muse-glimmer", "tag": "Best tool recovery",
        "speed": "not measured yet",
        "notes": "MCP Atlas 75.5 and SWE-bench Verified 76.0 (vendor). Dense, so slower than the MoE options.",
    },
    {
        "id": "ling-3.0-flash-vl", "name": "Ling-3.0-flash-VL", "org": "inclusionAI", "released": "2026-09", "params": "124B MoE, 5.5B active",
        "total_b": 124, "active_b": 5.5, "moe": True, "min_bpw": 2.6, "license": "MIT", "vision": True,
        "repo": "bartowski/Ling-3.0-flash-VL-GGUF", "quant": "IQ2_M", "size_gb": 45.32,
        "aa": 25, "aa_src": "https://artificialanalysis.ai/models/ling-3-0-flash-vl", "tag": "Biggest (GPU + lots of RAM)",
        "speed": "not measured yet (experts run from system RAM)",
        "notes": "Needs about 45 GB of system RAM next to the GPU, so only PCs with 64 GB+ get it. Smarter than the "
                 "MoE models above, slower than the 27B. Needs llama.cpp b11159 or newer.",
    },
    {
        "id": "gemma-4-12b", "name": "Gemma 4 12B", "org": "Google", "released": "2026-06", "params": "12B dense",
        "total_b": 12, "active_b": 12, "license": "Apache-2.0", "vision": True, "repo": "unsloth/gemma-4-12b-it-GGUF",
        "quant": "Q8_0", "size_gb": 12.67,
        "aa": 14, "aa_note": "AA estimate", "aa_src": "https://artificialanalysis.ai/models/gemma-4-12b",
        "tag": "Images and audio, 10-16 GB GPUs",
        "speed": "not measured yet",
        "notes": "Images and audio, 256K context. 8-bit on 16 GB cards, 4-6 bit on 8-12 GB cards.",
    },
    {
        "id": "glm-4.7-flash", "name": "GLM-4.7-Flash", "org": "Z.ai", "released": "2026-01", "params": "31B MoE, 3B active",
        "total_b": 31, "active_b": 3, "moe": True, "license": "MIT", "vision": False, "repo": "unsloth/GLM-4.7-Flash-GGUF",
        "quant": "UD-Q3_K_XL", "size_gb": 13.78,
        "aa": 15, "aa_note": "AA estimate", "aa_src": "https://artificialanalysis.ai/models/glm-4-7-flash", "tag": "Text agent",
        "speed": "not measured yet",
        "notes": "τ²-Bench 79.5 and BrowseComp 42.8 (vendor). Text only.",
    },
    {
        "id": "gpt-oss-20b", "name": "gpt-oss-20b", "org": "OpenAI", "released": "2025-08", "params": "21B MoE, 3.6B active",
        "total_b": 21, "active_b": 3.6, "moe": True, "fixed_quant": True, "license": "Apache-2.0", "vision": False,
        "repo": "ggml-org/gpt-oss-20b-GGUF", "quant": "MXFP4", "size_gb": 12.11,
        "aa": 9, "aa_src": "https://artificialanalysis.ai/models/gpt-oss-20b", "tag": "Very fast, reliable tools",
        "speed": "140 tok/s measured on an RTX 4080 (Ollama)",
        "notes": "Dependable tool calling, low AA score. Text only. Ships in one native 4-bit format. Runs well on "
                 "a CPU with 16 GB+ RAM because only 3.6B parameters work per token.",
    },
    {
        "id": "qwen3.5-9b", "name": "Qwen3.5-9B", "org": "Alibaba Qwen", "released": "2026-03", "params": "9B hybrid",
        "total_b": 9, "active_b": 9, "license": "Apache-2.0", "vision": True, "repo": "unsloth/Qwen3.5-9B-GGUF",
        "quant": "Q4_K_M", "size_gb": 5.7, "size_est": True,
        "aa": None, "rank": 11, "tag": "For 6-8 GB GPUs and laptops",
        "speed": "not measured yet",
        "notes": "Small enough for 8 GB cards with room for context, and usable on a fast CPU. No Artificial Analysis "
                 "score recorded here; size is estimated until Aero lists the repo.",
    },
    {
        "id": "qwen3.5-4b", "name": "Qwen3.5-4B", "org": "Alibaba Qwen", "released": "2026", "params": "4B hybrid",
        "total_b": 4, "active_b": 4, "license": "Apache-2.0", "vision": False, "repo": "unsloth/Qwen3.5-4B-GGUF",
        "quant": "Q4_K_M", "size_gb": 2.74,
        "aa": None, "rank": 6, "tag": "Smallest: older GPUs and CPU-only PCs",
        "speed": "20.1 tok/s measured on a Ryzen 9 9950X3D2 CPU (10 threads, no GPU)",
        "notes": "The same model as the router option: 97.5% on an independent 40-case tool-calling test. Runs on "
                 "nearly anything; expect simpler answers than the larger models.",
    },
]

# ------------------------------------------------------------------------------------------------ fitting to a PC

# quality ladder tried from the top; the first that fits wins (UD- variants are matched too when the repo uses them)
LADDER = ["Q8_0", "Q6_K", "Q5_K_M", "Q4_K_XL", "Q4_K_M", "IQ4_XS", "Q3_K_XL", "IQ3_S", "IQ3_XXS", "IQ2_M"]
GB = 1e9 / 2**20                 # catalog sizes are decimal GB; memory is counted in MiB
OTHERS_EST_MB = 1000             # what the desktop and open apps usually keep on the GPU
CPU_BW_GBS = 60                  # a typical dual-channel desktop; only used to rule out painfully slow CPU picks


def _bpw(q):
    from .hf import BPW
    return BPW.get((q or "").upper().replace("UD-", ""), 0)


def size_at(entry, quant):
    """Estimated download size (decimal GB) of an entry at another quant, scaled from the listed one."""
    if entry.get("fixed_quant") or quant == entry["quant"]:
        return entry["size_gb"]
    return entry["size_gb"] * _bpw(quant) / max(_bpw(entry["quant"]), 0.1)


def _min_bpw(entry):
    if entry.get("min_bpw"):
        return entry["min_bpw"]
    b = entry.get("total_b") or 10
    return 3.0 if b >= 20 else 3.5 if b >= 10 else 4.5


def _gpu_fits(mb, vram_mb, others_mb):
    reserve = min(2500, max(1200, mb * 0.2))      # context (KV cache), compute buffers, an MTP drafter
    return mb + reserve + others_mb <= vram_mb


def plan(entry, hw):
    """How this catalog model would run on this PC: where it fits, which quant, and a score for ranking.
    Estimates only; the tuner measures the real thing on first load."""
    vram = hw.get("vram_total_mb") or 0
    others = sum(g.get("used_mb") or 0 for g in hw.get("gpus") or []) if hw.get("vram_measured") else OTHERS_EST_MB
    others = min(others, vram // 3) if vram else 0
    ram = hw.get("ram_total_mb") or 0
    ram_share = max(0.0, (ram - 8192) * 0.7)            # system RAM that may hold offloaded experts or layers
    quants = [entry["quant"]] if entry.get("fixed_quant") else \
        sorted({entry["quant"], *LADDER}, key=lambda q: -_bpw(q))
    quants = [q for q in quants if _bpw(q) >= _min_bpw(entry) and _bpw(q) <= 8.5]
    quality = entry["aa"] if entry.get("aa") is not None else entry.get("rank", 0)
    out = {"fit": "no", "quant": entry["quant"], "size_gb": round(size_at(entry, entry["quant"]), 2), "score": -1,
           "why": "too big for this PC"}
    if vram:
        for q in quants:
            mb = size_at(entry, q) * GB
            if _gpu_fits(mb, vram, others):
                return {"fit": "gpu", "quant": q, "size_gb": round(size_at(entry, q), 2), "score": quality,
                        "why": "runs fully on the GPU"}
        if entry.get("moe"):
            # experts in RAM (llama.cpp --n-cpu-moe), attention + context on the GPU: stays fast
            for q in [q for q in quants if _bpw(q) <= 5.0]:
                mb = size_at(entry, q) * GB
                if mb <= (vram - others - 1500) + ram_share and vram - others >= 3000:
                    return {"fit": "moe", "quant": q, "size_gb": round(size_at(entry, q), 2), "score": quality - 2,
                            "why": "MoE: some experts in system RAM, the rest on the GPU; still quick"}
        for q in [q for q in quants if _bpw(q) <= 4.85]:
            mb = size_at(entry, q) * GB
            if mb <= (vram - others - 1500) + ram_share:
                return {"fit": "split", "quant": q, "size_gb": round(size_at(entry, q), 2), "score": quality - 12,
                        "why": "part GPU, part CPU: works, but slowly"}
        return out
    # CPU only
    budget = max(0.0, (ram - min(6144, ram * 0.4)) * 0.75)      # the OS and apps keep up to 6 GB
    for q in [q for q in quants if _bpw(q) <= 6.6]:
        mb = size_at(entry, q) * GB
        if mb > budget:
            continue
        active_gb = (entry.get("active_b") or entry.get("total_b") or 10) * _bpw(q) / 8
        tps = CPU_BW_GBS * 0.6 / max(active_gb, 0.1)
        if tps < 5:
            return {**out, "fit": "slow", "quant": q, "size_gb": round(size_at(entry, q), 2), "score": quality - 20,
                    "why": f"fits in RAM but would run at roughly {tps:.0f} tok/s on a CPU"}
        return {"fit": "cpu", "quant": q, "size_gb": round(size_at(entry, q), 2), "score": quality,
                "why": f"runs on the CPU, roughly {tps:.0f} tok/s on a typical desktop (Aero measures the real speed)"}
    return out


def recommend_main(hw):
    """The best catalog model for this PC (by Artificial Analysis score, adjusted for how it would run)."""
    best = None
    for m in MAIN_MODELS:
        p = plan(m, hw)
        if p["fit"] in ("no", "slow", "split"):
            continue
        if best is None or p["score"] > best[1]["score"]:
            best = (m, p)
    if best is None:                            # nothing comfortable: the smallest model, whatever its fit
        m = min(MAIN_MODELS, key=lambda e: e["size_gb"])
        best = (m, plan(m, hw))
    return best


def recommend_router(hw):
    """The router for this PC: MiniCPM5-2B normally; the smallest, fastest one on PCs with little RAM or few cores."""
    if (hw.get("ram_total_mb") or 0) < 12 * 1024 or (hw.get("cores") or 8) < 6:
        return router_by_id("lfm2.5-1.2b")
    return next(r for r in ROUTERS if r.get("recommended"))


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def resolve(entry, quant=None, budget_mb=None):
    """The repo files to download for a catalog entry: (model parts, mmproj path or None).
    quant: the quant plan() picked for this PC. When the repo doesn't have exactly that one, the best quant at or
    below its bits per weight is used (and, with budget_mb, no bigger than that), else the entry's listed quant."""
    from . import hf
    listing = hf.list_files(entry["repo"])
    files = listing["files"]
    want = entry.get("file") if not quant or quant == entry.get("quant") else None
    g = next((f for f in files if want and f["path"].split("/")[-1] == want), None)
    for q in ([quant] if quant else []) + [entry.get("quant")]:
        if g:
            break
        nq = _norm(q)
        g = next((f for f in files if _norm(f.get("quant")) == nq), None) or \
            next((f for f in files if nq and nq in _norm(f["name"])), None)
        if not g and q == quant and _bpw(q):
            lower = [f for f in files if 0 < (f.get("bpw") or 0) <= _bpw(q) and (f.get("bpw") or 0) >= 2.5
                     and (budget_mb is None or f["size"] / 2**20 <= budget_mb)]
            g = max(lower, key=lambda f: (f["bpw"], f["size"]), default=None)
    if not g:
        have = ", ".join(sorted({f.get("quant") or f["name"] for f in files})[:20])
        raise RuntimeError(f"{entry['repo']} has no {quant or entry.get('quant')} file (it has: {have}).")
    mm = None
    if entry.get("vision") and listing["mmproj"]:
        mm = listing["mmproj"][0]["path"]
    return g["parts"], mm


def router_by_id(rid):
    return next((c for c in ROUTERS if c["id"] == rid), None)


def main_by_id(mid):
    return next((c for c in MAIN_MODELS if c["id"] == mid), None)
