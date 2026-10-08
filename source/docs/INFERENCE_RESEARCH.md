# Inference research for Aero

**Scope.** This note covers local LLM inference optimizations for Aero, a Windows 11 desktop app that runs GGUF models through llama.cpp's `llama-server` (the CUDA build on NVIDIA, Vulkan on AMD and Intel, CPU otherwise). Aero scans each PC and tunes for it; this research was done against one reference PC, which has an RTX 5080 16 GB (Blackwell, sm_120), a Ryzen 9 9950X3D2 (Zen 5, 16C/32T, two CCDs with 3D V-Cache), and 64 GB DDR5. It checks the current state of llama.cpp's CUDA and server features, the alternative engines, the speculative decoding options, KV cache quantization quality, and the public Blackwell benchmarks. It sorts each one into what Aero has already built, what its auto-tuner could add, what to put off, and what to reject. Only techniques that run entirely on the user's machine count. Anything hosted is listed separately as rejected.

**Researched 2026-10-08.**

**Measurement note.** Nothing in this file was measured on the reference PC. Every "Measured in Aero" cell reads "not yet measured". Aero's Performance Lab is where these get measured. All throughput numbers below come from the cited third parties, on their hardware, models and builds.

Notes on dates. GitHub release pages show day and month but not the year. The years below were inferred and cross-checked in three ways: release asset timestamps (ExLlamaV3, FlashInfer), a dated news item for llama.cpp v0.1.0 (2026-08-17), and the order of the releases. Where a date could not be pinned down, the entry says so.


## What this update did with the research

The table below keeps the researcher's original "Status" column. This section records what Aero 1.0 changed because
of it, what it measured, and what is still open.

**Implemented in 1.0**

- `--load-mode` (row "`--load-mode`"): llama.cpp v0.5.0 removed `--mlock` and `--no-mmap`. The updater installs the
  newest llama.cpp release, so the router would have failed to start on an update. `router.py` now passes
  `--load-mode mlock` when the build lists that value, and `engine.py` maps the tuner's `no_mmap` knob to
  `--no-mmap` or `--load-mode none`, whichever the build supports. Verified against a v0.5.0-dev build in the
  development container (the old flags are rejected there; the new ones start).
- `--offline` (recommended knob 13): every llama-server Aero starts now gets `--offline` when the build supports
  it, so the engine never downloads or phones home. Verified in the container's `ARGS:` log line.
- CUDA 13.x build on Blackwell (row "Use the CUDA 13.4 Windows build"): already the installer's behaviour before this
  update. `installer/setup.py:pick_assets` takes the newest `win-cuda-X-x64` build the driver supports and refuses
  builds older than 12.8 on compute capability 12.x, so an RTX 5080 with a CUDA 13 driver gets the 13.4 build.
- Measurement plumbing the knobs below need: the Performance Lab bench reads `prompt_per_second`,
  `predicted_per_second`, `cache_n`, `draft_n` and `draft_n_accepted` from llama-server's own timings, measures TTFT on
  the client, and samples `nvidia-smi` for VRAM. It reports draft acceptance and prompt-cache reuse separately
  (see `docs/HAPO_ARCHITECTURE.md`).

**Measured for 1.0 (development container only, not a real PC)**

- Prompt cache reuse works through Aero's chat path: a 4,096-token shared prefix re-used 99% of its tokens, and
  TTFT fell from 4,015 ms cold to 321 ms warm on the container's CPU build. See `docs/PERFORMANCE_REPORT.md`.
  This proves the measurement, not a speed on the RTX 5080.

**Still open (candidates the tuner does not try yet)**

- `--spec-draft-n-max` sweep for MTP, `ngram-mod` vs `ngram-simple`, and acceptance per position.
- `--cache-reuse N` and `--cache-ram` sizes; slot save/restore of the system prompt.
- `--moe-cache-mib` on builds that have it.
- Router CCD pinning (`-Cr`, `--cpu-strict`) on the 9950X3D2. Needs the real CPU-to-CCD map from that PC.
- Asymmetric KV needs a custom CUDA build; deferred.

None of these were added blind: each changes speed, VRAM or output and has to win a measured A/B on real hardware first.

---

## Summary table

| Technique / project | Source link(s) | Latest version / date verified | License | Windows native? | Fully local? | Claimed gain (with source) | Cost / difficulty | Measured in Aero | Status | Why |
|---|---|---|---|---|---|---|---|---|---|---|
| llama.cpp `llama-server`, CUDA, GGUF | github.com/ggml-org/llama.cpp | v0.6.0 (2026-10-05, GitHub "Latest"). Nightly b11435 (2026-10-06). Nightly tag b11480 exists (date not verified) | MIT | Yes (prebuilt win-cuda-12.4 and win-cuda-13.4 x64 zips) | Yes | Baseline. Scoreboard RTX 5080, Llama 2 7B Q4_0: pp512 9487.70 t/s, tg128 184.68 t/s with FA (discussion #15013) | Already integrated | not yet measured | Implemented (baseline) | Aero's engine |
| Use the CUDA 13.4 Windows build (native sm_120a cubins), not the CUDA 12.4 build | release.yml, ggml-cuda CMakeLists.txt, common.cuh in llama.cpp | Release workflow at master, 2026-10 | MIT | Yes | Yes | No published number. From source: the 12.4 build has no 120a code, so `blackwell_mma_available()` is false and native FP4 (NVFP4/MXFP4) MMA paths are off. Blackwell then runs through PTX JIT of older archs | Low: pick the other zip. Needs driver >= 580 (CUDA 13.x) | not yet measured | Candidate for tuner | A/B the two binaries per model, especially MXFP4/NVFP4 GGUFs |
| Flash attention `-fa on/off/auto` | server README | master, 2026-10 | MIT | Yes | Yes | RTX 5080: pp512 8297 to 9488 t/s (+14%), tg128 182.0 to 184.7 t/s (+1.5%), FA off to on (#15013) | Trivial | not yet measured | Candidate for tuner | Default is `auto`. Quantized V requires FA. Pin it explicitly in trials |
| Symmetric KV types `-ctk/-ctv` f16, q8_0, q4_0 | server README, llama-context.cpp | master | MIT | Yes | Yes | Quality (PR #7412): q8_0/q8_0 KLD 0.00098, q4_0/q4_0 KLD 0.0365 vs f16 | Already in tuner | not yet measured | Implemented | Aero already tunes this. Add a quality gate (see §13) |
| Asymmetric KV (e.g. K q8_0 / V q4_0) | build.md (`GGML_CUDA_FA_QUANTS`), fattn.cu | master | MIT | Only with a custom build | Yes | PR #7412: f16/q8_0/q4_0 at 6.5 bpv has KLD 0.0051, close to V-only q4_0. That saves VRAM at near-q8 quality | Official builds compile only q4_0-q4_0, q8_0-q8_0, f16-f16, bf16-bf16. Other pairs fall back to "converting K and V to f16 instead (slow)" | not yet measured | Deferred | Needs Aero to ship its own CUDA build with `GGML_CUDA_FA_QUANTS` set |
| KV Hadamard rotation (automatic with quantized KV) | PR #21038, llama-kv-cache.cpp | master (on by default) | MIT | Yes | Yes | gpt-oss-20b AIME25: q8_0 31.7% to 37.1%, q4_0 2.0% to 21.7% (f16 37.9%). Cost: tg about 0.88x for q4_0 on one MoE (PR #21038) | Env toggle only | not yet measured | Candidate for tuner | A/B `LLAMA_ATTN_ROT_DISABLE=1` for speed vs quality when KV is quantized |
| Prompt cache, `--cache-reuse N` | server README, server-context.cpp | master | MIT | Yes | Yes | No speed number published. It avoids re-prefilling shifted chunks | Low. Silently disabled for multimodal, M-RoPE, and SWA without `--swa-full` | not yet measured | Candidate for tuner | Fits Aero's stable-system-prompt design |
| `--cache-ram`, `--cache-idle-slots` (host-RAM prompt cache) | server README | master (default 8192 MiB, on) | MIT | Yes | Yes | None published | Low | not yet measured | Candidate for tuner | 64 GB RAM allows a larger cache. Set `0` during benchmarks |
| `-np` / `--kv-unified` / `--kv-unified-per-slot` | server README, server.cpp | master. `-np` auto means 4 slots plus unified KV | MIT | Yes | Yes | None published | Low | not yet measured | Candidate for tuner | A single-user app may do better with `-np 1` (VRAM, TTFT) |
| Slot save/restore (`--slot-save-path`, `/slots/{id}?action=save/restore/erase`) | server README | master | MIT | Yes | Yes | None published. Restores a saved system-prompt KV without re-prefill | Medium (app plumbing) | not yet measured | Candidate for tuner | Persists the warm system-prompt KV across server restarts |
| `--swa-full`, `--ctx-checkpoints`, `-cms` | server README, llama-kv-cache-iswa.cpp | master | MIT | Yes | Yes | None published | Low. `--swa-full` costs VRAM | not yet measured | Candidate for tuner | Prompt-cache behavior on SWA and hybrid models |
| MoE placement `-ot`, `-cmoe`, `-ncmoe` | server README | master | MIT | Yes | Yes | Model-dependent | Already in tuner (`--n-cpu-moe`) | not yet measured | Implemented | `-ot` regexes are a finer-grained fallback |
| `--moe-cache-mib` (GPU LRU cache for CPU-resident experts) | server README, llama-moe-cache.cpp. Earlier PR #26563 | master and nightly >= b11480 only. Not in v0.6.0 | MIT | Yes | Yes | Earlier, different PR #26563 (closed): 1.72x to 2.07x on Qwen3.6-35B-A3B with 8 GB VRAM, but 0.84x on a 122B model. No numbers for the merged flag | Low (one flag) | not yet measured | Candidate for tuner | Directly relevant to 16 GB + `--n-cpu-moe`. New and experimental |
| `-ncffn/--n-cpu-ffn` (dense FFN on CPU) | server README | >= v0.4.0 | MIT | Yes | Yes | None published | Low | not yet measured | Candidate for tuner | Alternative to lowering `-ngl` for dense models that slightly exceed VRAM |
| MTP speculative decoding (`--spec-type draft-mtp`) | docs/speculative.md, issue #28196 | master. Merge reported around 2026-05-16 (secondary source) | MIT | Yes | Yes | RTX 5090, native Linux, Qwen3.5-27B: 80.8 to 136.2 t/s (1.69x) at depth 2. A second tester got 1.50x at depth 2-3 (#28196) | Already in Aero | not yet measured | Implemented | Tune `--spec-draft-n-max` per model |
| n-gram speculative (`ngram-simple`, `ngram-map-k`, `ngram-map-k4v`, `ngram-mod`, `ngram-cache`) | docs/speculative.md | master | MIT | Yes | Yes | Docs give example acceptance (0.70 for ngram-mod), no speed figure | Already in Aero (n-gram) | not yet measured | Implemented | Add `ngram-mod` and `--spec-default` as tuner variants |
| EAGLE-3 (`--spec-type draft-eagle3`) | PR #18039, docs/speculative.md | Merged 2026-06-12 | MIT | Yes | Yes | RTX A6000: Llama3.1-8B Q4_K_M 121.5 to 274.4 t/s (2.26x). Qwen3-30B-A3B 1.39x. gpt-oss-20b 1.06x, and 0.89-0.95x on two prompts (PR #18039) | Medium: needs a per-target EAGLE-3 GGUF converted with `--target-model-dir` | not yet measured | Candidate for tuner | Only where a matching EAGLE-3 draft exists. Weak on MoE |
| Draft model (`draft-simple`, `-md`) | docs/speculative.md | master | MIT | Yes | Yes | None in docs | Medium: needs VRAM for the draft | not yet measured | Candidate for tuner | Fallback for models with no MTP head |
| DFlash / DSpark drafts | docs/speculative.md | master | MIT | Yes | Yes | None in docs | Medium. DSpark supports only Qwen3-backbone drafts | not yet measured | Deferred | Few drafts available. Revisit later |
| `--load-mode` (replaces `--mlock`, `--no-mmap`) | arg.cpp at v0.4.0, v0.5.0, master | `--mlock` removed in v0.5.0 (2026-09-23) | MIT | Yes (Windows mlock uses VirtualLock + SetProcessWorkingSetSize) | Yes | n/a (stability) | Low, but **required** | not yet measured | Implemented in 2.1 | The router now passes `--load-mode mlock`; `--no-mmap` maps to `--load-mode none` |
| CPU scheduling `-t/-tb`, `-C/-Cr/--cpu-strict`, `--prio`, `--poll` | server README | master | MIT | Yes | Yes | None published for Zen 5 dual-CCD | Low | not yet measured | Candidate for tuner | Separate the router and the main model onto different CCDs |
| `--fit` / `--fit-target` / `--fit-ctx` (auto-fit, default on) | server README | master | MIT | Yes | Yes | n/a | Low | not yet measured | Candidate for tuner | Turn it off or pin it during trials so results stay reproducible |
| CUDA graphs, PDL, graph-opt env toggles | common.cuh, ggml-cuda.cu, root CMakeLists.txt | master | MIT | Yes | Yes | None published | Trivial (env vars) | not yet measured | Candidate for tuner | A/B only. Defaults are normally best |
| Windows environment: Sysmem fallback policy, desktop GPU contention | NVIDIA KB 5490, issue #28196 | Driver 536.40+ / 546.01+ | n/a | Windows-only | Yes | Desktop-app GPU contention cut decode from 76.7 to 47.5 t/s on an RTX 5090 (#28196) | Low | not yet measured | Candidate for tuner | Aero's own UI may compete for the GPU |
| Unsloth Dynamic GGUFs (UD 2.0 and 3.0) | unsloth.ai docs, github.com/unslothai/unsloth | Dynamic 3.0 dated 2026-08-19 (ai-tldr). Unsloth core Apache-2.0, Studio AGPL-3.0 | Quants follow each base model's license (not stated in the docs; check each HF repo) | Yes (plain GGUF) | Yes | Unsloth's own claim: ">10% top-1 better accuracy at the same size". Gemma 3 27B UD-Q4_K_XL MMLU 71.47% vs BF16 71.5% | Low: model choice | not yet measured | Candidate for tuner | Quant-choice candidates per model. The claims are vendor-reported |
| ExLlamaV3 (EXL3) | github.com/turboderp-org/exllamav3 | 1.5.4 (2026-10-03) | MIT | Yes (cu128 win_amd64 wheels, needs `triton-windows`) | Yes | None verified for RTX 5080 | High: second Python/Torch stack, EXL3 files only (no GGUF) | not yet measured | Deferred | Not GGUF-compatible. Would double model storage and runtime complexity |
| TabbyAPI | github.com/theroyallab/tabbyAPI | Rolling release, no tags (README pins exllamav3 1.5.4) | AGPL-3.0 | Yes (`start.bat`, Windows wheels pinned) | Yes | n/a | High, plus AGPL obligations | not yet measured | Deferred | ExLlamaV3-only server. "Not meant to run on production servers" |
| vLLM | docs.vllm.ai, github.com/vllm-project/vllm | v0.31.0 (2026-10-05) | Apache-2.0 | No ("does not support Windows natively". WSL or a community fork) | Yes (if run locally) | n/a | High | not yet measured | Rejected | No native Windows. GGUF is "highly experimental" via an out-of-tree plugin |
| SGLang | docs.sglang.io, github.com/sgl-project/sglang | v0.5.21 (2026-10-02) | Apache-2.0 | No (maintainer answered "no" to Windows support, 2025-12-25) | Yes (if run locally) | n/a | High | not yet measured | Rejected | Linux-only. Requires CUDA 13. No GGUF |
| FlashInfer | github.com/flashinfer-ai/flashinfer | v0.7.0.post1 (2026-09-29). v0.7.1rc2 (2026-10-02) | Apache-2.0 | Not stated | Yes | n/a | n/a | not yet measured | Rejected | A kernel library for vLLM/SGLang/TRT-LLM. llama.cpp can't use it |
| KTransformers / kt-kernel | github.com/kvcache-ai/ktransformers | v0.7.1 (Sep 15; year not shown on the page, likely 2026) | Apache-2.0 | No (v0.7.0.post4 requires "Linux x86_64, glibc 2.35+") | Yes | Server-scale claims only (e.g. 8x L20 + Xeon) | High | not yet measured | Rejected | Linux-only now. Built around SGLang and AMX/AVX-512 server CPUs |
| NVIDIA TensorRT-LLM | nvidia.github.io/TensorRT-LLM, GitHub releases | v1.3.0rc29 (Sep 29, year not shown) | Apache-2.0 | No ("requires Linux x86_64 or Linux aarch64". Windows deprecated as of v0.18.0) | Yes (if run locally) | n/a | Very high | not yet measured | Rejected | No Windows. No GGUF |
| Najafu/llama-autotune | github.com/Najafu/llama-autotune | **Exists.** v0.3.0 (CHANGELOG dated 2026-07-03). 4 stars | MIT | Yes ("developed and battle-tested on Windows") | Yes | None published | n/a (reference) | not yet measured | Deferred | Reference design (Optuna Bayesian stage, constraint engine). Aero already has its own tuner |
| castlen3/llama.cpp-gpu-tuning-guide | github.com/castlen3/llama.cpp-gpu-tuning-guide | **Exists.** 11 commits, 0 stars, commit dates not visible | No LICENSE file found (raw LICENSE returned 404) | n/a (docs, Windows-oriented) | Yes | None (community SOP) | n/a (reference) | not yet measured | Deferred | Useful checklist. Its claims are unverified, and it names a `--no-graph` flag that current `llama-server` doesn't have |

---

## Per-item details

### 1. llama.cpp (ggml-org/llama.cpp)

**Release status (verified 2026-10-08).**
- Release tags in the new `v0.x` series:
  - v0.1.0: 2026-08-17. A dated news item and the HN thread confirm it. In that thread the maintainer said official semantic versioning was "almost ready".
  - v0.4.0: Sep 4.
  - v0.5.0: Sep 23.
  - v0.6.0: Oct 5, marked "Latest" on GitHub.
- Nightly `bNNNNN` builds are still published several times a day: b11435 on Oct 6, and tags up to at least b11480 exist.
- License: MIT.
- Windows release assets for each build include `llama-<tag>-bin-win-cuda-12.4-x64.zip` and a CUDA 13.4 x64 zip, each with a matching cudart DLL zip.
- **Blackwell build detail (from `.github/workflows/release.yml` and `ggml/src/ggml-cuda/CMakeLists.txt`).**
  - The Windows CUDA job sets `-DGGML_NATIVE=OFF` and no `CMAKE_CUDA_ARCHITECTURES`, so it uses the default arch list.
  - `120a-real` is added only when CUDA >= 12.8. So only the **CUDA 13.4** Windows zip contains native Blackwell code.
  - `blackwell_mma_available()` requires the highest compiled arch to be >= 1200. On the 12.4 zip it is false, so the native FP4 (NVFP4/MXFP4) tensor-core paths can't run there.
  - NVIDIA's release notes give a minimum driver of 580 for CUDA 13.x. CUDA 13.4 maps to the R615 branch.
  - A self-built binary must target `120a`, not plain `120`. Issue #19662 (open) shows MXFP4 kernels failing to assemble for non-"a" sm_120 targets.
- **Recent CUDA changes relevant to RTX 50, from the release notes:**
  - v0.6.0:
    - Model-driven W4A4 (NVFP4/MXFP4) mul_mat path.
    - MMVQ shared-expert fusion.
    - NVFP4 accumulation in MMQ.
    - Whole-tile FlashAttention scheduling and fp16 tile-FA tuning for head sizes 40-112.
    - MMVF for thin f16/bf16 mul_mat at small batch.
    - Fix for an MMQ memory fault when n_expert is much larger than n_ubatch.
  - v0.5.0:
    - CUDA graphs enabled for MTP drafting.
    - Sparse FA for DSV4/Qwen4 prefill.
    - CUDA builds upgraded to 13.4.
  - v0.4.0:
    - Fused MoE expert reduction.
    - FA shared-memory XOR swizzle.
- **CUDA runtime knobs, verified in source and build.md:**
  - CUDA graphs are compiled in by default. The root `CMakeLists.txt` sets `GGML_CUDA_GRAPHS_DEFAULT ON`. Disable them at runtime with `GGML_CUDA_DISABLE_GRAPHS`.
  - Programmatic dependent launch (PDL) is used on Hopper and newer, which includes sm_120. Disable it with `GGML_CUDA_PDL=0`.
  - `GGML_CUDA_GRAPH_OPT=1` is an opt-in, undocumented concurrent-stream graph optimization (found in `ggml-cuda.cu`).
  - `GGML_CUDA_MMQ_PREC=auto|q8|q4` overrides the activation precision for NVFP4/MXFP4 on Blackwell (documented in build.md).
  - MMQ kernels are the default on GPUs with int8 tensor cores. `GGML_CUDA_FORCE_MMQ` and `GGML_CUDA_FORCE_CUBLAS` are compile-time options only.
  - `GGML_CUDA_ENABLE_UNIFIED_MEMORY` is Linux-only. On Windows the equivalent is the NVIDIA Control Panel "System Memory Fallback" setting.
- **FlashAttention kernel selection on Ada/Blackwell** (`ggml/src/ggml-cuda/fattn.cu`):
  - With a quantized K or V cache, the vector kernel, which reads the quantized cache directly, is chosen only when the query batch is <= 2 tokens.
  - Larger batches, including prompt processing and multi-token verify batches, use the MMA kernel. It first converts K and V to f16 (`need_f16_K/V = true`).
  - *Inference from the source, not measured.* Speculative decoding with `--spec-draft-n-max` >= 2 sends verify batches of 3 or more tokens. With a quantized KV cache, each verify step therefore goes through the f16-conversion path, and that cost grows with context length. The Performance Lab should measure spec decoding × KV type × context length together.

**Server flags, verified against the auto-generated flag list in `tools/server/README.md` at master (2026-10). Defaults are shown as documented.**

| Area | Exact flag(s) | Default / notes |
|---|---|---|
| Threads | `-t, --threads N`; `-tb, --threads-batch N` | -1 (auto); `-tb` defaults to the same as `-t` |
| Affinity | `-C, --cpu-mask M`; `-Cr, --cpu-range lo-hi`; `--cpu-strict <0\|1>`; batch variants `-Cb`, `-Crb`, `--cpu-strict-batch` | Empty by default |
| Priority / polling | `--prio N` (-1 low, 0 normal, 1 medium, 2 high, 3 realtime); `--prio-batch N`; `--poll <0...100>` (default 50); `--poll-batch <0\|1>` | |
| Context / batch | `-c, --ctx-size N` (0 = from model); `-b, --batch-size N` (2048); `-ub, --ubatch-size N` (512); `--keep N` | |
| SWA | `--swa-full` | Default false |
| Flash attention | `-fa, --flash-attn [on\|off\|auto]` | auto |
| KV types | `-ctk, --cache-type-k TYPE`; `-ctv, --cache-type-v TYPE` | Allowed: f32, f16, bf16, q8_0, q4_0, q4_1, iq4_nl, q5_0, q5_1. Default f16 |
| KV offload | `-kvo/--kv-offload`, `-nkvo/--no-kv-offload` | Enabled |
| Loading | `-lm, --load-mode MODE` (auto, none, mmap, mlock, mmap+mlock, dio); `-lzm, --lazy-mode MODE` (on, auto, off) | `--mlock`/`--mmap`/`--no-mmap` were **removed** in v0.5.0 (see below) |
| Devices / offload | `-dev, --device`; `--list-devices`; `-ngl, --gpu-layers N` (number, 'auto' or 'all'; default auto); `-sm`, `-ts`, `-mg` | |
| Tensor placement | `-ot, --override-tensor <pattern>=<buffer type>,...`; `-cmoe, --cpu-moe`; `-ncmoe, --n-cpu-moe N`; `-ncffn, --n-cpu-ffn N`; `--moe-cache-mib N` (default 0) | `--moe-cache-mib` is in master and nightly >= b11480 only |
| Auto-fit | `-fit, --fit [on\|off]` (default on); `-fitt, --fit-target MiB` (1024); `-fitc, --fit-ctx N` (4096) | Adjusts any args you leave unset so the model fits |
| Prompt cache | `--cache-prompt/--no-cache-prompt` (on); `--cache-reuse N` (0); `-cram, --cache-ram N` MiB (8192; -1 no limit, 0 off); `--cache-idle-slots` (on, needs cache-ram) | |
| Slots / KV pool | `-np, --parallel N` (-1 auto means 4 slots + unified KV, per `server.cpp`); `-kvu/--kv-unified`, `-no-kvu`; `--kv-unified-per-slot N`; `-sps, --slot-prompt-similarity` (0.10) | |
| Checkpoints | `-ctxcp, --ctx-checkpoints N` (32); `-cms, --checkpoint-min-step N` (8192) | For SWA and hybrid models |
| Slot persistence | `--slot-save-path PATH`; `POST /slots/{id}?action=save`, `?action=restore`, `?action=erase` | |
| Speculative | `--spec-type` (none, draft-simple, draft-eagle3, draft-mtp, draft-dflash, draft-dspark, ngram-simple, ngram-map-k, ngram-map-k4v, ngram-mod, ngram-cache); `-md, --spec-draft-model`; `--spec-draft-n-max N` (3); `--spec-draft-n-min N` (0); `--spec-draft-p-min` (0.00); `--spec-draft-p-split` (0.10); `--spec-draft-sampling {greedy,probabilistic}` (greedy); `--spec-draft-backend-sampling` (on); `-ngld`, `-devd`, `-ctkd`, `-ctvd`, `-otd`, `-ncmoed`; `--spec-ngram-mod-n-match` (24), `-n-min` (48), `-n-max` (64); `--spec-ngram-simple-*`, `--spec-ngram-map-k-*`, `--spec-ngram-map-k4v-*`; `--spec-default` (enables ngram-mod 24/48/64); `--spec-synth-len`, `--spec-synth-rates` (benchmarking only) | |
| Sampling offload | `-bs, --backend-sampling` | Experimental, off |
| Metrics | `--metrics` exposes `llamacpp:spec_decode_num_draft_tokens_total`, `..._accepted_tokens_total`, `..._drafts_total`, `..._accepted_tokens_per_pos_total`, plus prompt and predict throughput gauges | |
| Network isolation | `--offline` (forces the local cache, no network) | Recommended for Aero |

**Breaking flag changes Aero must handle (verified by diffing `common/arg.cpp` at each tag).**
- `--mlock`, `--mmap`, `--no-mmap`:
  - v0.1.0 through v0.4.0: still accepted but print "DEPRECATED ... use --load-mode". In those versions `--mlock` alone maps to load mode `mlock`.
  - v0.5.0, v0.6.0, master, and nightlies b11417 to b11480: the flags are gone. The parser rejects unknown flags with "error: invalid argument".
  - **Aero's router (`-dev none --mlock`) must switch to `--load-mode mlock` or `--load-mode mmap+mlock`** (env `LLAMA_ARG_LOAD_MODE`) on current builds.
- `--draft`, `--draft-n`, `--draft-max`, `--draft-min`, `--spec-ngram-size-n`, `--spec-ngram-size-m` and `--spec-ngram-min-hits` throw "the argument has been removed" (already true at v0.1.0). Use the `--spec-draft-n-*` or `--spec-ngram-*-*` names instead.
- `-dt/--defrag-thold` is deprecated (warning only).
- v0.6.0 bumped the session formats (`LLAMA_SESSION_VERSION` 11, `LLAMA_STATE_SEQ_VERSION` 4). **Slot files saved by older builds won't restore on newer ones.** Aero should tag saved slot files with the build that wrote them.

**KV type / flash-attention rules (verified in `src/llama-context.cpp`, `fattn.cu`, build.md).**
- A quantized V cache requires FA. `-fa auto` turns FA on automatically. `-fa off` with a quantized V cache fails with "quantized V cache requires flash_attn to be enabled".
- A quantized K cache with FA requires the K head dim to be divisible by the quant block size. The same applies to V.
- MLA models and DeepSeek4 reject K and V types that differ.
- The official CUDA builds compile FA vector kernels only for `q4_0-q4_0; q8_0-q8_0; f16-f16; bf16-bf16`. That is the `GGML_CUDA_FA_QUANTS` default, and the release workflow doesn't override it. Any other K/V pair logs "no FlashAttention vector kernel compiled for K/V types ..., converting K and V to f16 instead (slow)". The castlen3 guide also reports prefill stalls and hangs with mixed types on CUDA. That is a community claim, not verified here.
- A Hadamard rotation (PR #21038, "llama : rotate activations for better quantization") is applied automatically whenever K or V is quantized and the head dim % 64 == 0. Disable it with `LLAMA_ATTN_ROT_DISABLE=1`. MLA models are not supported.

**Prompt cache and cache reuse (verified in `tools/server/server-context.cpp` and `src/llama-kv-cache*.cpp`).**
- `--cache-reuse N` reuses chunks by KV shifting. The server turns it off with a warning in two cases:
  - Multimodal: "cache_reuse is not supported by multimodal".
  - The memory can't shift: "cache_reuse is not supported by this context".
- Shifting is impossible in these cases:
  - M-RoPE models (`n_pos_per_embd > 1`).
  - Step35.
  - **SWA models unless the SWA cache is full size.** `llama_kv_cache_iswa::get_can_shift()` requires the base and SWA caches to be the same size, which in practice means `--swa-full`.
- Hybrid memory can shift only if its attention part can.
- For SWA and hybrid models, prefix reuse relies on `--ctx-checkpoints` and `-cms`.

**Speculative decoding** is covered in §12.

**Windows memory locking (verified in `src/llama-mmap.cpp`).** On Windows, mlock is implemented with `VirtualLock` after raising the working set through `SetProcessWorkingSetSize`. Failures are logged as warnings, not errors. *Uncertain:* the `dio` load mode says "use DirectIO if available". Only an `O_DIRECT` POSIX path was seen, so whether it works on Windows was not confirmed.

### 2. ExLlamaV3 (turboderp-org/exllamav3)
- **Version:** 1.5.4 (2026-10-03; asset timestamps confirm 2026). Earlier releases:
  - 1.5.3 (Sep 27): experimental n-gram corpus, dynamic draft sizing with TP.
  - 1.5.2 (Sep 26).
  - 1.5.1 (Sep 20): DFlash2.
  - 1.5.0 (Sep 13): better speculative decoding for MoE. Its benchmark table includes the RTX 5090.
- **License:** MIT.
- **Windows:** prebuilt `+cu128.torch2.10.0` `win_amd64` wheels for CPython 3.10-3.14. The README says: "On Windows, you also need the `triton-windows` package ... ExLlamaV3 does not import without it". The PyPI package has no prebuilt extension and needs VS Build Tools.
- **Format:** EXL3, based on QTIP, plus FP16/BF16 HF weights. **No GGUF support**, so GGUF files can't be reused.
- **Other features:** 2-8 bit cache quantization, TP/EP, CPU offload for MoE (AVX2/AVX-512), speculative decoding (draft models, n-gram).
- **Verdict:** Deferred. Viable on Windows, but it would need a second model library in EXL3 format, a PyTorch + Triton runtime (several GB), and a separate tuner. No RTX 5080 numbers were found to justify it.

### 3. TabbyAPI (theroyallab/tabbyAPI)
- **Status:** "The official API server for Exllama". A FastAPI app with an OpenAI-compatible API, tool calling, and speculative decoding via draft models.
- **Backends:** ExLlamaV3 only. ExLlamaV2 now appears only in the acknowledgements. Supported formats are EXL3 and FP16/BF16. GGUF isn't mentioned.
- **License:** AGPL-3.0.
- **Releases:** no tags. It's a rolling release, and the README says it "is not meant to run on production servers".
- **Windows:** `start.bat` ships, and `pyproject.toml` pins Windows wheels: torch 2.9.0+cu128 (and 2.11.0+cu130 in another extra), exllamav3 1.5.4, `triton-windows`, `winloop`.
- **Verdict:** Deferred. Same blockers as ExLlamaV3, plus the AGPL obligations if Aero bundled it.

### 4. vLLM (vllm-project/vllm)
- **Latest:** v0.31.0 (Oct 5). v0.30.0 was Sep 22. License Apache-2.0.
- **Windows:** the docs say verbatim: "vLLM does not support Windows natively". They suggest WSL or a community fork. Only Linux is listed for CUDA.
- **GGUF:** "highly experimental and under-optimized". It has moved to an out-of-tree `vllm-gguf-plugin`.
- **Verdict:** Rejected for native Windows. Under WSL2 it would still be local, but it can't use Aero's GGUF library well.

### 5. SGLang (sgl-project/sglang)
- **Latest:** v0.5.21 (Oct 2). v0.5.20 (Sep 18) added SM120 sparse-MLA changes. License Apache-2.0.
- **Requirements:** the install docs target "Linux with NVIDIA GPUs" and state "SGLang requires CUDA 13". v0.5.19 was the last CUDA 12 release.
- **Windows:** in GitHub discussion #4095 ("Does sglang support windows?"), the maintainer answered "no" (2025-12-25).
- **Verdict:** Rejected (Linux-only, no GGUF).

### 6. FlashInfer (flashinfer-ai/flashinfer)
- **What it is:** a kernel library and kernel generator for attention, GEMM and MoE. It powers SGLang, vLLM, TensorRT-LLM, TGI and others. It is not a server.
- **Version:** v0.7.0.post1 (2026-09-29), v0.7.1rc2 (Oct 2). SM 12.0/12.1 (RTX 50, DGX Spark) is listed. License Apache-2.0.
- **Windows:** not mentioned.
- **Verdict:** Rejected. llama.cpp has its own CUDA kernels and can't use FlashInfer.

### 7. KTransformers (kvcache-ai/ktransformers)
- **What it is now:** the repo is now `kt-kernel`, a CPU kernel library for SGLang (inference), plus SFT. The old integrated framework moved to `archive/`.
- **Version:** v0.7.1 (Sep 15). v0.7.0.post4 lists its requirement as "Linux x86_64, glibc 2.35+, Python 3.11 or 3.12, CUDA 12.8". License Apache-2.0.
- **Windows:** the only Windows mention is a 2024 "Support windows native" note for the archived framework.
- **Claims:** aimed at huge MoE models on server CPUs, e.g. DeepSeek-R1 FP8 on 8x L20 + Xeon at 227.85 tok/s total. Not relevant to a 16 GB consumer GPU.
- **Verdict:** Rejected for native Windows (Linux-only).

### 8. Najafu/llama-autotune
- **Verified: it exists.** MIT license, 4 stars, 2 forks, 16 commits.
- **Version:** 0.3.0, dated 2026-07-03 in CHANGELOG.md.
- **What it does:** a Python 3.12+ tool that drives `llama-bench` and `llama-server`. It has hardware detection, a GGUF header parser, and an auto-scaling speed probe. The search runs in three stages: heuristics, then a local grid, then Optuna Bayesian tuning. A constraint engine estimates VRAM and detects OOM, and results go to SQLite.
- **Windows:** the README says it was "developed and battle-tested on Windows".
- **Limitations:** no published benchmark results. It optimizes `llama-bench` runs, not server-level TTFT, cache reuse or speculative decoding.
- **Verdict:** Deferred, as a reference only. Ideas Aero could borrow:
  - pruning failed or implausible trials instead of scoring them;
  - recording peak RAM;
  - a "max_context" objective.

### 9. castlen3/llama.cpp-gpu-tuning-guide
- **Verified: it exists.** 11 commits, 0 stars, 0 forks.
- **License:** no LICENSE file. `raw.githubusercontent.com/.../main/LICENSE` returned 404, so default copyright applies. Commit dates weren't visible (commit pages blocked by robots).
- **Content:** a Windows-oriented SOP. Its main points:
  - Kill stale processes before each run.
  - Use `--cache-ram 0` and `--fit off` while benchmarking.
  - Keep mmap on, and use `--no-mmap` only as a diagnostic.
  - Avoid asymmetric `-ctk/-ctv` on CUDA ("may cause prefill stall/hang").
  - Measure decode and prefill separately. Spec decoding doesn't help prefill.
  - CUDA graph compile can hang at >= 32K context in background mode.
- **Caveats:** it suggests `--no-graph`. That flag isn't in the current `llama-server` flag list. The real switch is the `GGML_CUDA_DISABLE_GRAPHS` env var. It still refers to `--mmap`/`--no-mmap`, which were removed in v0.5.0.
- **Verdict:** Deferred, as a checklist reference. Its claims are anecdotal.

### 10. NVIDIA TensorRT-LLM
- **Supported OS:** the support matrix says "TensorRT-LLM requires Linux x86_64 or Linux aarch64".
- **Windows history:** the v0.18.0 notes say "Windows platform support is deprecated as of v0.18.0" and "All Windows-related code and functionality will be completely removed in future releases."
- **Latest:** v1.3.0rc29 (Sep 29). Its notes mention SM120/121 work but no GeForce or Windows support. License Apache-2.0.
- **Verdict:** Rejected (no Windows, no GGUF, needs an engine-build workflow).

### 11. Unsloth dynamic GGUF quants
- **What they are:** ordinary GGUF files. Unsloth says they "work with most inference engines including llama.cpp", and no custom quant types were mentioned.
- **Dynamic 2.0 (now labeled "Old"):**
  - Per-layer quant-type selection, with a 1.5M-token calibration set.
  - Claims for Gemma 3 27B: KLD for UD-Q4_K_XL went from 0.024916 to 0.023701, and MMLU 5-shot was 71.47% vs BF16's 71.5%.
- **Dynamic 3.0 (dated 2026-08-19 by ai-tldr; the Unsloth page shows no date):**
  - New imatrix calibration data, pure post-training quantization.
  - Headline claim: ">10% top-1 better accuracy at the same size". This is vendor-reported, and the KLD figures appear only in charts.
  - The MTP module is stripped from UD-Q2_K_XL and smaller. A separate Q4_0 MTP module is offered. This matters for Aero's MTP path.
- **License:** the Unsloth library is Apache-2.0 and Unsloth Studio is AGPL-3.0. The docs don't state a license for the quant files themselves. Each Hugging Face repo carries its own license, which normally follows the base model's. Check per model.
- **Fully local:** yes once downloaded.
- **Verdict:** Candidate for tuner, as a quant-selection candidate validated with Aero's own KLD/speed measurements.

### 12. Speculative decoding status in llama.cpp (as of master, 2026-10)
- **Types:** `draft-simple` (separate draft model), `draft-eagle3`, `draft-mtp`, `draft-dflash`, `draft-dspark`, `ngram-simple`, `ngram-map-k`, `ngram-map-k4v`, `ngram-mod`, `ngram-cache`. Several can be combined with commas. "If a draft model is combined with a draftless decoding the draftless decoding has higher precedence."
- **MTP:**
  - Uses the main model's MTP/NextN heads, either inside the GGUF or as a sidecar file downloaded with `-hf` (`--mtp` for `llama-download`).
  - Secondary sources (byteiota) put the mainline merge at about 2026-05-16, build ~9200. The PR number was not verified.
  - v0.5.0 added CUDA graphs for MTP drafting. v0.6.0 added probabilistic draft sampling and stopped accepting draft tokens past end-of-generation.
  - Measurements on an RTX 5090 (Qwen3.5-27B Q4_K_M, issue #28196):
    - Native Linux: plain 80.8 t/s; depth 1 118.4 (1.47x); depth 2 136.2 (1.69x); depth 3 128.0; depth 4 121.7.
    - Second tester: best at depth 2-3 (about 1.50x).
    - Acceptance depends on the prompt: 0.29-0.34 for prose vs 0.68-0.75 for code.
    - The thread also reports that b10741-b10743 couldn't start `draft-mtp` (fixed by #28173, first good build b10749). It also reports that greedy MTP depth 6 produced output that differed from plain decoding. Both are unresolved third-party reports.
- **EAGLE-3:**
  - PR #18039 was merged 2026-06-12. It needs an EAGLE-3 checkpoint for the specific target model, converted with `convert_hf_to_gguf.py ... --target-model-dir`.
  - Supported drafts include Llama 3.1/3.3, Qwen3 dense/MoE, gpt-oss and Gemma 4 (see the list in docs/speculative.md).
  - Gains are large on dense models (2.26x on Llama3.1-8B Q4_K_M, A6000) and small or negative on MoE (gpt-oss-20b 0.89-1.06x).
- **n-gram:** no extra model needed.
  - `ngram-mod` is a ~16 MB shared hash pool across slots.
  - `--spec-default` enables `ngram-mod` with n_match 24, n_min 48, n_max 64.
  - The docs recommend long drafts for MoE models and suggest lowering `n-min`/`n-max` for dense ones.
- **Draft model:** `-md` plus the `--spec-draft-*` controls, with its own KV types, placement and threads.
- **Measuring:** the `--metrics` counters `llamacpp:spec_decode_*` give acceptance per draft position. The `tools/server/bench/speed-bench` client compares baseline and spec runs.
- **Caveat:** speculative decoding doesn't speed up prefill (castlen3 guide; follows from how it works).

### 13. Quantized KV cache quality
- **PR #7412** (2024-05, CUDA research demo; K = K cache, V = V cache):
  - KLD vs FP16:
    - f16/f16/q8_0: 0.000743
    - f16/q8_0/f16: 0.000944
    - q8_0/q8_0: 0.000980
    - f16/f16/q4_0: 0.004988
    - f16/q8_0/q4_0: 0.005079
    - f16/q4_0/f16: 0.032916
    - q4_0/q4_0: 0.036509
  - For comparison, weights alone: q6_K 0.005460, q4_K_M 0.031280.
  - Author's conclusions: **the K cache is much more sensitive than the V cache**, q8_0 KV showed no significant loss, and weights are the most sensitive part overall.
- **PR #21038** (automatic Hadamard rotation) narrows the q4_0 gap considerably:
  - Qwen3.5 9B q4_0/q4_0 KLD 0.007979 to 0.005059.
  - gpt-oss-20b AIME25 with q4_0 KV: 2.0% to 21.7%. f16 scores 37.9%, and q8_0 with rotation scores 37.1%.
  - Even with rotation, low-bit KV can still hurt reasoning tasks badly.
- **localbench / oobabooga** (2026-04-24, KLD vs f16-cache top-40 logprobs, ~250k tokens, rotation included):
  - Sensitivity is strongly **model-dependent**.
  - q8_0 KLD: Gemma 4 31B 0.108, Gemma 4 26B-A4B 0.377, Qwen 3.6 27B 0.024, Qwen 3.6 35B-A3B 0.039.
  - Gemma 26B-A4B with q4_0: KLD 1.088, 68.0% top-1 agreement.
  - For Qwen, the damage concentrates in long documents and tool calling.
  - So "q8_0 is practically lossless" does not hold for every model.
- **Implication for Aero:**
  - Make the KV type per-model, with a measured quality gate (e.g. `llama-perplexity --kl-divergence` against an f16-KV baseline on Aero's own tool-call and long-context prompts), not just a VRAM-fit decision.
  - Prefer quantizing V over K when only one can be quantized. On official CUDA builds that needs a custom FA build (see §1).

### 14. Public benchmarks on Blackwell consumer cards, and Windows vs Linux
- **llama.cpp CUDA scoreboard, discussion #15013** (Llama 2 7B Q4_0, `llama-bench` pp512/tg128; OS not stated; commit hashes as listed):
  - RTX 5080 (commit 8a4280c): FA off pp512 8297.36, tg128 181.99. FA on pp512 9487.70, tg128 184.68.
  - RTX 5090: FA on 14970.15 / 300.40.
  - RTX 5070 Ti: FA on 8419.56 / 182.43.
  - RTX 4090: FA on 14770.63 / 188.96.
  - On this small model the RTX 5080's tg128 is about the same as the 4090's (bandwidth-bound), and its pp512 is about 64% of the 4090's.
- **Discussion #19890** (2026-02-25): RTX 5090, CUDA 13.0, Qwen3.5-35B-A3B UD-Q4_K_XL, `-fa 1 -ctk q8_0 -ctv q8_0`. pp512 7026 t/s, falling to 6461 at 32K; tg average 194.0 t/s.
- **Phoronix RTX 5090 llama.cpp review** (2025-01-27): Ubuntu 24.10. Numbers are in charts only, and the backend isn't stated in the text. The RTX 5080 wasn't tested there.
- **Windows vs Linux:**
  - No controlled CUDA Windows-vs-Linux comparison on the same machine was found. What exists:
  - **Issue #28196** (RTX 5090; third-party, no maintainer response):
    - The first report blamed Windows. The author then showed the Windows deficit came mostly from **desktop GPU contention under WDDM**: an Electron app plus dwm held about 35% of the GPU.
    - Plain decode measured 76.7 t/s with the app closed, 55.4 with it idle, and 47.5 while it was streaming.
    - The author still quoted a "~1.65x" Windows penalty against native Linux, but the Windows numbers went through Ollama's runner, not plain `llama-server`. **Uncertain.**
  - **OpenBenchmarking 2509076-NE-LLAMA508252** (Sep 2025, Ryzen 9 9950X3D + RX 9070 XT, llama.cpp b6401, Vulkan and CPU backends, not CUDA):
    - Vulkan was mostly tied, with Windows ahead on some MoE/small models (e.g. gpt-oss-20b pp512 4211.61 vs 3389.88).
    - CPU decode was 4-8% faster on Linux 6.17 for 7-8B models.
  - **Phoronix Razer Blade 18** (2026-07-15): Vulkan "similar performance between Windows and Linux". The OpenBLAS CPU backend was faster on Windows 11.
  - **NVIDIA KB 5490:** the "CUDA - Sysmem Fallback Policy" (driver 536.40+, can be disabled from 546.01) silently spills into shared system memory near the VRAM limit, which slows inference. "Prefer No Sysmem Fallback" turns OOM into a hard failure. That suits a tuner that wants clean OOM signals.

---

## Recommended tuner knobs for llama.cpp on RTX 5080

Measure each knob one at a time against a fixed baseline. Kill stale processes between trials. Use `--fit off`, or record what fit chose, plus `--cache-ram 0` for throughput trials, and `--metrics`. Per trial, record:
- prompt tokens/s (`prompt_per_second`) at about 512, 4K and 16K+ tokens;
- decode tokens/s (`predicted_per_second`) for short and long outputs;
- TTFT;
- peak VRAM (startup log "KV buffer size", compute buffer, `nvidia-smi`);
- host RAM;
- the build tag.

Run a KLD check against an f16-KV baseline whenever a knob can change outputs.

1. **Binary choice:** Windows `win-cuda-13.4-x64` vs `win-cuda-12.4-x64` (driver >= 580 for 13.x). Measure pp and tg per model, especially MXFP4/NVFP4 GGUFs. Check the startup feature list for `BLACKWELL_NATIVE_FP4`. If a model uses NVFP4/MXFP4, also A/B `GGML_CUDA_MMQ_PREC=q4` vs `q8` (speed vs KLD).
2. **`-fa on`** (explicit) vs `auto`. Verify FA is actually on in the log. Measure pp at 4K/16K.
3. **`-ctk/-ctv`:** symmetric `f16/f16`, `q8_0/q8_0`, `q4_0/q4_0` only, on official builds. Measure:
   - tg at short and long context, pp at 16K/32K, and the VRAM freed (which can go to more `-ngl` or less `--n-cpu-moe`);
   - KLD vs f16 on Aero's tool-calling and long-document prompts.
   - With quantized KV, also A/B `LLAMA_ATTN_ROT_DISABLE=1` (rotation costs about 5-12% tg in PR #21038 reports, but improves quality).
4. **Spec decoding × KV type × context:**
   - `--spec-type draft-mtp` with `--spec-draft-n-max` 1, 2, 3, 4.
   - `--spec-draft-sampling greedy` vs `probabilistic` at Aero's normal temperature.
   - `--spec-type draft-mtp,ngram-mod`, and `--spec-default` for MTP-less models.
   - Record acceptance per position from `llamacpp:spec_decode_num_accepted_tokens_per_pos_total`.
   - Repeat at 2K and 16K context with f16 vs q8_0 KV, because verify batches above 2 tokens use the f16-conversion FA path (inferred from source).
5. **`-ub` 256, 512, 1024, 2048 and `-b` 2048, 4096.** Measure pp at real prompt sizes and the compute buffer VRAM. Larger ubatch usually helps pp on big GPUs but costs VRAM that could hold KV or layers.
6. **MoE models (16 GB VRAM):**
   - `--n-cpu-moe N` sweep. This is already in the tuner.
   - On builds >= b11480, add `--moe-cache-mib` 0, 1024, 2048, 4096 (tg, VRAM, and output stability; new and experimental).
   - `-t` 8, 12, 16 with `-Cr` pinned to one CCD vs both. Check the logical-CPU-to-CCD mapping on the actual machine (e.g. with Sysinternals Coreinfo) rather than assuming it.
   - `-tb` 16 vs 32. Measure tg (bound by RAM bandwidth) and pp.
7. **Dense models that slightly exceed VRAM:** `-ngl N` vs `-ncffn N` (FFN-only CPU offload). Measure tg and pp.
8. **Slots:** `-np 1` vs the default auto (4 slots + unified KV), and `--kv-unified-per-slot N`. Measure VRAM, TTFT, and tg with the router running at the same time.
9. **Prompt reuse:**
   - `--cache-reuse` 0, 64, 256 (only effective when the log shows no "cache_reuse is not supported" warning).
   - `--cache-ram` 0, 8192, 16384, and `--cache-idle-slots`.
   - For SWA models, `--swa-full` on/off (VRAM vs reuse). For hybrid models, `--ctx-checkpoints` and `-cms`.
   - Slot save/restore of the stable system prompt (`--slot-save-path`) for cold-start TTFT. Tag files by build, since session versions change between builds.
   - Measure TTFT for "same system prompt + new user turn" and after a server restart.
10. **Loading:** `--load-mode mmap` vs `none` vs `mmap+mlock` for the main model. Measure load time, RSS and first-token latency after idle. For the **router**, replace `--mlock` with `--load-mode mlock` or `mmap+mlock`. This is required on v0.5.0+.
11. **Router vs main CPU contention:**
    - Give the router `-dev none` with `-Cr` and `--cpu-strict 1` on the CCD the main model doesn't use.
    - Router `--poll` 0 vs 50, and `--prio` 0, 1, 2.
    - Main model `--poll` 50 vs 100.
    - Measure router latency and main-model tg while both are running.
12. **CUDA env A/B (keep the defaults unless a win is measured):** `GGML_CUDA_DISABLE_GRAPHS=1`, `GGML_CUDA_PDL=0`, `GGML_CUDA_GRAPH_OPT=1` (experimental), and `-bs/--backend-sampling` (experimental).
13. **Windows environment checks (run before trials, not tuned per model):**
    - Set NVIDIA "CUDA - Sysmem Fallback Policy" to "Prefer No Sysmem Fallback" for `llama-server.exe`, so OOM is detected cleanly.
    - Measure tg with Aero's UI focused vs minimized, and consider turning off UI GPU acceleration during generation if contention shows up (issue #28196 pattern).
    - Run `--offline` so inference never touches the network.

---

## Rejected because hosted / not local

None of these run on the owner's machine, so none count under Aero's rules:
- **Hosted inference APIs:** OpenRouter, Hugging Face Inference Endpoints and Inference Providers, Replicate, Together AI, Fireworks AI, Groq. Also any OpenAI/Anthropic-style hosted API used for generation, routing or drafting.
- **GPU clouds and rented GPUs:** RunPod, Lambda, Vast.ai, Modal, CoreWeave and similar, including running vLLM/SGLang/TensorRT-LLM there.
- **Remote speculative-decoding drafts:** any draft model or "speculator" served over the network instead of loaded into the local `llama-server` with `-md`, MTP or n-gram.
- **llama.cpp features that would leave the machine if pointed elsewhere:** these are allowed only in local-only form.
  - `--rpc host:port`: only `127.0.0.1` would be local.
  - `--tools-runtime ssh:<target>`.
  - Remote MCP servers via `--mcp-servers-*` / `--ui-mcp-proxy`.
- **Model acquisition flags:** `-hf/--hf-repo`, `--spec-draft-hf`, `-mu/--model-url`, `-dr/--docker-repo` and the `--*-default` presets download weights. Downloading is acceptable for getting a model, but inference must run with local files. Use `--offline`.
- **Unsloth / KTransformers / TensorRT-LLM cloud notebooks or hosted demos**, if any: not evaluated, and excluded by rule.

---

## Sources (every URL actually opened)

Pages fetched (WebFetch):
- https://github.com/ggml-org/llama.cpp/releases
- https://github.com/ggml-org/llama.cpp/releases/tag/v0.6.0
- https://github.com/ggml-org/llama.cpp/releases/tag/v0.5.0
- https://github.com/ggml-org/llama.cpp/releases/tag/v0.4.0
- https://github.com/ggml-org/llama.cpp/releases/tag/v0.1.0
- https://zeli.app/story/49335017
- https://github.com/ggml-org/llama.cpp/discussions/15013
- https://github.com/ggml-org/llama.cpp/discussions/19890
- https://github.com/ggml-org/llama.cpp/issues/28196
- https://github.com/ggml-org/llama.cpp/issues/19662
- https://github.com/ggml-org/llama.cpp/pull/7412
- https://github.com/ggml-org/llama.cpp/pull/21038
- https://github.com/ggml-org/llama.cpp/pull/18039
- https://github.com/ggml-org/llama.cpp/pull/26563
- https://prismix.dev/news/7ffdd0b8ae0b
- https://byteiota.com/qwen3-6-mtp-in-llama-cpp-27b-model-now-1-7x-faster/
- https://github.com/Najafu/llama-autotune
- https://github.com/castlen3/llama.cpp-gpu-tuning-guide
- https://github.com/turboderp-org/exllamav3
- https://github.com/turboderp-org/exllamav3/releases
- https://github.com/theroyallab/tabbyAPI
- https://github.com/theroyallab/tabbyAPI/releases
- https://docs.vllm.ai/en/latest/getting_started/installation/gpu.html
- https://docs.vllm.ai/en/latest/features/quantization/gguf.html
- https://github.com/vllm-project/vllm/releases
- https://docs.sglang.ai/get_started/install.html (redirected to https://docs.sglang.io/docs/get-started/install.md, also opened)
- https://github.com/sgl-project/sglang/releases
- https://github.com/sgl-project/sglang/discussions/4095
- https://github.com/flashinfer-ai/flashinfer
- https://github.com/flashinfer-ai/flashinfer/releases
- https://github.com/kvcache-ai/ktransformers
- https://github.com/kvcache-ai/ktransformers/releases
- https://nvidia.github.io/TensorRT-LLM/installation/index.html
- https://nvidia.github.io/TensorRT-LLM/reference/support-matrix.html
- https://nvidia.github.io/TensorRT-LLM/0.18.2/release-notes.html
- https://github.com/NVIDIA/TensorRT-LLM/releases
- https://unsloth.ai/docs/basics/unsloth-dynamic-2.0-ggufs
- https://unsloth.ai/docs/basics/dynamic-3.0-ggufs
- https://ai-tldr.dev/releases/unsloth-dynamic-3-0-ggufs/
- https://github.com/unslothai/unsloth
- https://localbench.substack.com/p/kv-cache-quantization-benchmark
- https://www.phoronix.com/review/nvidia-rtx5090-llama-cpp
- https://www.phoronix.com/review/razer-blade18-windows-linux/7
- https://mail.openbenchmarking.org/result/2509076-NE-LLAMA508252
- https://nvidia.custhelp.com/app/answers/detail/a_id/5490
- https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html

Raw source files downloaded and read (raw.githubusercontent.com):
- ggml-org/llama.cpp master:
  - `tools/server/README.md`, `tools/server/server-context.cpp`, `tools/server/server.cpp`
  - `common/arg.cpp`
  - `src/llama-context.cpp`, `src/llama-kv-cache.cpp`, `src/llama-kv-cache-iswa.cpp`, `src/llama-memory-hybrid.cpp`, `src/llama-memory-recurrent.cpp`, `src/llama-mmap.cpp`, `src/llama-moe-cache.cpp`
  - `ggml/src/ggml-cuda/fattn.cu`, `ggml/src/ggml-cuda/ggml-cuda.cu`, `ggml/src/ggml-cuda/common.cuh`, `ggml/src/ggml-cuda/mmq.cuh`, `ggml/src/ggml-cuda/mmq.cu`, `ggml/src/ggml-cuda/CMakeLists.txt`
  - `ggml/CMakeLists.txt`, `CMakeLists.txt`, `ggml/include/ggml.h`
  - `docs/build.md`, `docs/speculative.md`, `docs/development/token_generation_performance_tips.md`
  - `.github/workflows/release.yml`, `LICENSE`
- ggml-org/llama.cpp tags:
  - `common/arg.cpp` at v0.1.0, v0.2.0, v0.3.0, v0.4.0, v0.5.0 and v0.6.0;
  - `common/arg.cpp` at nightlies b11417, b11429, b11435, b11436-b11460 and b11461-b11480;
  - `common/speculative.cpp` at v0.6.0.
  - Older b-number tags (b9000-b10741) were also sampled. Their flag contents were not monotonic with build number, so no conclusion was drawn from them.
- turboderp-org/exllamav3 master: `README.md`, `LICENSE`
- theroyallab/tabbyAPI main: `README.md`, `pyproject.toml`, `LICENSE`
- Najafu/llama-autotune main: `README.md`, `CHANGELOG.md`, `LICENSE`
- castlen3/llama.cpp-gpu-tuning-guide main: `README.md`, `docs/cuda.md`, `LICENSE` (404, not present)
- vllm-project/vllm, sgl-project/sglang, flashinfer-ai/flashinfer, kvcache-ai/ktransformers, NVIDIA/TensorRT-LLM main: `LICENSE`

Attempted but blocked or failed (no content used):
- https://github.com/Najafu/llama-autotune/commits/main (robots)
- https://github.com/castlen3/llama.cpp-gpu-tuning-guide/commits/main (robots)
- https://github.com/ggml-org/llama.cpp/pulls?q=... (robots)
- https://openbenchmarking.org/result/2509076-NE-LLAMA508252 (fetch error; the mail. mirror was used)
- https://dev.to/breachprotocol/quantizing-v4-flashs-kv-cache-in-llamacpp-changes-which-tokens-it-picks-38np (fetch error)
- https://insiderllm.com/guides/fp4-inference-llamacpp-nvfp4-mxfp4/ (robots)

Search-result-only leads (not opened, not relied on): markaicode.com benchmark pages, techplained.com, inventivehq.com, openclawdc.com, runaihome.com, llmconfigurator.com, mer.vin MTP article, hardware-corner.net GPU ranking.
