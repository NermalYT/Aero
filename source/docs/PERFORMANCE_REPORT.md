# Performance report

Aero 1.0.0, 2026-10-08. "2.0" below is Halcyon 2.0, the version Aero replaced (same code base).

**Read this first.** Every number in this file was measured in the development container: 4 vCPU Intel Xeon at
2.8 GHz, 16 GB RAM, no GPU, Linux, Chromium with software rendering, llama.cpp 0.5.0-dev (a portable CPU build),
and `tinytest-f16.gguf`, a tiny model with random weights. These runs prove that the tuner, profiles, benchmarks and
reports measure real things end to end. They say nothing about speed on a real GPU, and the accuracy numbers
(JSON, tools, recall, code edit) are near zero because a random-weight model cannot answer anything.

**Nothing in 1.0 was measured on a real Windows PC.** Run `validation\Validate-Aero.ps1`, then the Performance
Lab's Quick Tune and Quick benchmark, to get the real numbers for your PC. The Lab's Export Markdown button produces a report in this same
format.

## 1. Versions

| Item | Value |
|---|---|
| Aero | 1.0.0 (previous: Halcyon 2.0.0) |
| llama.cpp | 0.5.0-dev, `build-portable` (AVX2/FMA/F16C, no native tuning), CPU backend |
| Model | `tinytest-f16.gguf`, random weights, 32k training context |
| Hardware | 4 vCPU Xeon @ 2.80 GHz, 16 GB RAM, no NVIDIA GPU |
| Browser | Chromium (Playwright build), software compositing, 1440x900 |

## 2. What changed in the inference path, before vs after

| Area | Halcyon 2.0 | Aero 1.0 | Speed effect |
|---|---|---|---|
| Main model arguments | as tuned | as tuned, plus `--offline` | None expected (the flag only stops downloads). Not A/B measured |
| `no_mmap` knob | `--no-mmap` | `--no-mmap`, or `--load-mode none` on v0.5.0+ | Same behaviour; 2.0 would fail to start on v0.5.0+ |
| Router | `--mlock` | `--load-mode mlock` on v0.5.0+ | 2.0's router fails to start on v0.5.0+ (`error: invalid argument: --mlock`, reproduced here). 1.0 starts |
| Profiles | tuner pick only | tuner pick or a HAPO profile chosen from the same trials | Whatever the measured trial does; nothing new is guessed |
| Scenery while generating | animating | paused by default | See section 6 |

Inference speed itself was not changed in 1.0, so there is no before/after tokens-per-second table for the model.
The updater installs the newest llama.cpp release; with the 2.0 router that would have broken routing.

Measured on the reference PC (RTX 5080 16 GB) in an earlier session (recorded in `engine.py`, not re-measured for
1.0): Qwen3.8-27B IQ2_M,
32k q8_0 KV, temperature 0.6. No speculation 59.5 tok/s; MTP 108 (code) / 66 (free text); n-gram 123 / 59; MTP +
n-gram 168 / 69.

## 3. Tuning (Quick Tune, 10 trials)

VRAM limit 2 GB (CPU only here, so the limit measures host RAM). 194 s, converged, verdict by the verifier.

| # | Config | Gen tok/s | Prompt tok/s | Memory MB | How it was chosen |
|---|---|---|---|---|---|
| 1 | ctx 32,768, kv q8_0, ub 512, 4 threads | 89.9 | 215.3 | 241 | estimator seed |
| 2 | kv q8_0, ub 1024 | 76.6 | 212.7 | 259 | ub up |
| 3 | kv f16, ub 512 | 127.6 | 891.5 | 362 | KV up |
| 4 | kv f16, ub 1024 | **128.6** | 874.2 | 381 | ub up (the pick) |
| 5 | kv f16, ub 512, 2 threads | 87.2 | 484.3 | 362 | threads down |
| 6 to 10 | re-tests of #3 and #4 | 122.1 to 132.1 | 842.9 to 948.2 | 361 to 381 | verifier |

The pick held 18.2 tok/s with 18,700 tokens of context filled (the deep-context check). Re-tests vary by about 4%
in generation and 6% in prompt speed, which is why the tuner re-tests before choosing.

HAPO profiles from these trials: Maximum Speed, Balanced, Maximum Context and Maximum Quality all chose trial #4
(the model's maximum 32k context, f16 KV, ub 1024), the same config as the tuner's pick. Agent Optimized and
Efficiency chose trial #3 (ub 512, 362 MB instead of 381 MB, prompt speed 891 vs 874 tok/s). Efficiency did not
choose the 241 MB q8_0 trial #1 because it keeps 69.9% of the best speed and the rule needs 70%. Applying Agent
Optimized and reloading used that profile; Restore previous returned to the tuner's pick both times it was pressed.

## 4. Quick benchmark (Agent Optimized profile)

ctx 32,768, CPU only, kv f16, ub 512, 4 threads. 104.6 s.

| Workload | Metric | Median | p95 | CV | n |
|---|---|---|---|---|---|
| Chat | TTFT | 151 ms | 154 ms | 3.2% | 3 |
| Chat | Decode | 218.3 tok/s | 222.7 | 1.2% | 3 |
| Prefill 2,048 | Prompt speed | 1,709 tok/s | 1,711 | 0.1% | 2 |
| Prefill 2,048 | TTFT | 1,435 ms | 1,443 | 0.7% | 2 |
| Prefill 8,192 | Prompt speed | 727 tok/s | 732 | 0.9% | 2 |
| Prefill 8,192 | TTFT | 12,541 ms | 12,574 | 0.4% | 2 |
| Code edit | Decode | 112.0 tok/s | 112.1 | 0.1% | 2 |
| Code edit | Draft acceptance (n-gram) | 0.12 | | | 1 |
| Prompt cache | Cold TTFT, 4,096-token prefix | 4,015 ms | | | 1 |
| Prompt cache | Warm TTFT, same prefix | 321 ms | | | 1 |
| Prompt cache | Tokens reused | 99% | | | 1 |
| Memory | `llama-server` host RAM peak | 810 MB | | | |
| Memory | VRAM | not measured (no GPU) | | | |

Accuracy (meaningless for a random-weight model, shown so the report format is complete): JSON valid 0% of 8, tool
calls 17% of 6, long-context recall 0 of 3 at 8k, code edit correct 0 of 2.

What this shows: the prompt cache works through Aero's chat path (12.5x faster first token on a repeated 4k
prefix), and prompt speed drops with document length (1,709 tok/s at 2k vs 727 at 8k on this CPU), which is why the
bench measures prefill at several sizes instead of one.

## 5. Router suite

The router run on the tiny model returned an error for every request: llama.cpp could not initialise the JSON
grammar sampler for a random-weight vocabulary (HTTP 400). That is a property of the test model, not of Aero, so
router accuracy and latency are **not measured**. The report now separates "could not answer" (errors, with the first
error text) from "answered wrongly", so this case shows as errors rather than 0% accuracy.

## 6. UI overhead: scenery and glass

### 6.1 Frame time while idle, Halcyon 2.0 vs Aero 1.0

Same container, same 1440x900 window, no model generating, median of 5 s of `requestAnimationFrame` deltas (best
of two runs). Lower is better; 16.7 ms is a full 60 fps.

| UI | Scenery | Median ms | p95 ms | fps |
|---|---|---|---|---|
| 2.0 | Full, day | 150.0 | 333.3 | 6.0 |
| 2.0 | Full, night | 116.7 | 233.3 | 7.8 |
| 2.0 | Still, day | 116.6 | 216.7 | 8.2 |
| 2.0 | Off, day | 100.0 | 133.3 | 9.6 |
| 1.0 | Full, day | 100.0 | 216.8 | 8.6 |
| 1.0 | Full, night | 99.9 | 183.3 | 9.8 |
| 1.0 | Still, day | **16.7** | 16.8 | 49.4 |
| 1.0 | Off, day | **16.7** | 16.8 | 50.8 |
| 1.0 | Full, day, transparency off | 33.4 | 66.7 | 24.4 |

2.0 never really stopped drawing: its Still and Off modes still cost 100 to 117 ms a frame here. In 1.0, Still and
Off are idle (one frame per vsync, nothing to paint), and Full is a third cheaper than 2.0's Full.

### 6.2 What costs the most in Full

Removing pieces one at a time from the 1.0 Full scene (container, day): the full scene was about 117 ms per frame;
without the glass backdrop blur it was 33 ms; with the scenery Off it was 16.7 ms. Bubbles, clouds and the pond
were minor; the light ribbons, sun rays and stars together accounted for most of the rest. The blur is expensive
because every animated pixel behind a glass frame has to be re-blurred every frame. That finding is why 1.0 adds
**Enable transparency** (Settings > Appearance), which keeps the look but drops the blur.

### 6.3 Generation speed with the scenery on, Full vs Off (Performance Lab "Scenery cost")

| Glass | Scenery | Decode tok/s (median, n=4) | Frame time median / p95 ms |
|---|---|---|---|
| on | Full | 1.35 | 233 / 433 |
| on | Off | 81.8 | 16.7 / 217 |
| off | Full | 2.86 | 50 / 83 |
| off | Off | 186.2 | 16.7 / 16.7 |

Decode was 98% slower with Full scenery, with glass on or off (the two Off rows differ because the container's
shared CPU was busier during the first run). Turning transparency off cut the Full frame time from 233 to 50 ms but
did not give the model its cores back. **This is not representative of a PC with a GPU.** Here the browser renders in
software on the same 4 CPU cores the CPU-only model uses, so every animated frame takes cores away from the model.
On a PC with a GPU the model runs on the GPU and Edge composites there too; the contention is smaller but not zero
(llama.cpp issue #28196 reports decode falling from 76.7 to 47.5 tok/s on an RTX 5090 when a desktop app competed for
the GPU). That is why 1.0 pauses the scenery while a model is generating, tuning or benchmarking by default, and why
the Lab's Scenery cost test exists: it measures the real number on the real PC.

## 7. Regressions and open items

- No inference regression is known. None was measured either, because the inference path did not change beyond the
  two flags in section 2.
- Full scenery with glass is the heaviest UI mode. Mitigations: paused while busy (default), Still, Off, and Enable
  transparency off.
- Not measured anywhere yet: VRAM and GPU numbers, MTP/EAGLE acceptance on real models, router accuracy on the real
  router model, 16k/32k prefill and recall on a real model, thermals and clock drift.
