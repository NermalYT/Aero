# HAPO: Aero Adaptive Performance Optimization

HAPO is how Aero decides which `llama-server` configuration a model runs with on this PC, proves it with
measurements, and lets the user switch goals and undo the switch. It is built on top of the per-model tuner that
Aero already had; nothing in the tuner's behaviour was removed.

Code: `aero/tuner.py` (measuring), `aero/hapo.py` (fingerprint, profiles, persistence, rollback),
`aero/bench.py` (benchmarks), `aero/server.py` (`/api/hapo/*`, `/api/bench/*`, `/api/lab/report`),
`aero/static/lab.js` (the Performance Lab).

## 1. Measuring: the tuner (unchanged, now feeding HAPO)

1. The user types a VRAM limit for the model. What is left over hosts the tuner's advisor model.
2. The user picks a depth. The Performance Lab offers **Quick Tune** (the tuner's Short depth, 10 trials) and
   **Deep Tune** (Long, 50 trials). The model loader still offers Short / Medium / Long / Full.
3. Trial 1 is the estimator's predicted optimum. Every later trial changes exactly one knob by one step from an earlier
   trial: context (a fixed ladder from 4k to 1M), GPU layers or CPU MoE experts, KV precision (f16 / q8_0 / q4_0),
   micro-batch, threads. The advisor picks the step; illegal, repeated and provably failing steps are never offered.
4. Every trial is a real `llama-server` run on port 8182: load, VRAM measured against the user's limit (the process's
   real usage, so CUDA buffers, the KV cache and speculative-decoding drafts all count), prompt speed and generation
   speed measured after a warm-up. Speculative decoding is part of every trial's arguments, so its VRAM is inside the
   limit like everything else.
5. The last part of the budget re-tests the top configurations and averages them, to beat measurement noise.
6. The winner gets a deep-context run with half its context filled, recorded as `tg_deep`.

The result is cached in `data/tune_cache.json` under `tuner.cache_key(model, mmproj, hw, spec)`: the model file, the
vision projector, the GPU, the RAM and the speculative-decoding setup. In 1.0 each stored trial also carries its exact
knobs (`"knobs": {...}`), so HAPO can rebuild the server config of any trial, not just the winner. Older caches only
stored a text description; `hapo.knobs_of` parses it (`ctx 32,768 | gpu 40/48 | kv q8_0 | ub 512 | 8 threads`).

## 2. Fingerprint

`hapo.fingerprint(hw, model_path)` records what the measurements depend on:

- **hardware**: GPU name, VRAM, driver, CUDA version, CPU, cores and threads, RAM, OS;
- **engine**: the `llama-server` path, its `--version`, the speculative types it offers, and a SHA-1 of its `--help`
  text (a new build with new flags gets a new hash even if the version string did not change);
- **id**: a short hash of hardware + engine;
- **model** (when given): file name, total bytes across split parts, SHA-256 of the first 4 MB (GGUF header and
  metadata, so a re-quantized file with the same name is told apart), architecture, layers, training context.

Every applied profile stores the fingerprint id it was applied under. The Lab marks a profile **stale** when the
current id differs (new driver, new GPU, new llama.cpp) and says so above the profile cards; Quick Tune and Deep
Tune sit right there.
It also warns when the tune was measured with a different llama.cpp version than the one installed now.

## 3. Profiles

Six goals. Each is a fixed rule applied to the tuner's passing, measured trials (`hapo.candidates`: trials that
loaded, stayed under the VRAM limit, produced a generation speed, and are not re-test duplicates). Nothing is
estimated or extrapolated. If no measured trial satisfies a goal, the card says **not measured** and why
(`hapo._why_missing`), and offers Deep Tune.

| Goal | Rule (verbatim from `hapo.PROFILES`) |
|---|---|
| Maximum Speed | The fastest measured generation (within 5% of the best), then the most context up to 32k. |
| Balanced | The largest context that keeps 80% of the best generation speed; if that is under 32k, a 32k+ config that keeps 50% wins instead. |
| Maximum Context | The largest context that keeps at least 50% of the best generation speed. |
| Maximum Quality | The most precise KV cache (f16, then q8_0, then q4_0) with every layer on the GPU, then context up to 32k, then speed. Needs at least 40% of the best speed. |
| Agent Optimized | At least 32k context for long tool chains, ranked by generation speed plus half the weight on prompt speed, because tool results are re-read every step. Needs at least 50% of the best speed. |
| Efficiency | The least VRAM that still keeps 70% of the best speed and 16k context (or the largest measured), leaving room for games and other apps. |

Maximum Speed, Balanced and Maximum Context mirror the tuner's own three goals, so on the same trials they pick the
same configuration the tuner would. The Lab also shows the **tuner's pick** (the config the model loads with when
no profile is applied).

**Pareto front.** `hapo.pareto` marks trials no other trial beats on generation speed, context and VRAM at once. The
Lab's trial chart (context on a log scale, speed up the side, colour by VRAM) rings them, and fills the active one.

## 4. Applying, persistence, rollback

`POST /api/hapo/apply {id, profile}` stores the chosen trial's server config (`tuner.Space.server_cfg(knobs)`) as the
model's active profile in `data/hapo.json`:

```json
{"active":   {"<tune_key>": {"profile": "agent", "name": "Agent Optimized", "trial": 7, "desc": "ctx 32,768 | ...",
                             "knobs": {...}, "config": {...}, "tg": 41.2, "pp": 1210.5, "mem_mb": 13950,
                             "applied_at": "2026-10-08 19:12", "fingerprint": "fcc903168a55", "tuned_at": ...}},
 "history":  {"<tune_key>": [ previous entries, newest last, max 20; {"profile": "tuner"} means the tuner's pick ]},
 "failures": [ {"at": "...", "profile": "agent", "desc": "...", "error": "last lines of the llama-server log"} ]}
```

- Applying the profile that is already active is a no-op (no history entry).
- Applying "tuner" clears the profile, so the tuner's pick is used.
- `POST /api/hapo/restore {id}` pops the history stack: the previous profile, or the tuner's pick, becomes active.
- The Performance Lab reloads the model right after an apply or restore, so the change is real immediately; the
  chosen profile's name shows in the model card and in the load progress.

**Automatic rollback on failure.** When a model loads (`server.py`, the load job), the active profile's config is
tried first. If `llama-server` fails to start with it (out of memory after a driver update, a flag the new build
rejects), HAPO records the failure, drops the profile, and the load continues with the tuner's pick. The load log
says "The Agent Optimized profile failed to load (out of memory), so Aero went back to the tuner's pick and removed
that profile choice." instead of leaving a broken model.

**Invalidation.** "Forget tuning" for a model also forgets its HAPO entries (`hapo.forget`). A new model file, a
different vision projector, a different GPU or RAM size, or a different speculative setup produce a new tune key,
so old profiles are never applied to the wrong setup.

## 5. Benchmarks

`bench.Bench` runs against the loaded model through the same OpenAI-compatible endpoint and chat template the agent
uses, so the numbers include the real prompt formatting.

| Workload | Quick | Deep | Measures |
|---|---|---|---|
| chat | 3 runs | 7 | TTFT (client-side, first token of any kind), decode tok/s (`predicted_per_second`) |
| prefill | 2k, 8k tokens, 2 runs each | 2k, 8k, 16k, 3 each | prompt tok/s (`prompt_per_second`), TTFT |
| code | 2 | 4 | decode tok/s while rewriting a 60-line file with one rename; draft acceptance (`draft_n_accepted / draft_n`); whether the edit is correct |
| cache | 1 | 3 | the same 4k prefix twice with `cache_prompt`: tokens reused (`cache_n / prompt_n`), cold vs warm TTFT |
| json | 8 tasks | 12 | valid JSON rate, exact-field rate |
| tools | 6 tasks | 10 | right tool (or none) with the right key argument, using OpenAI-style tool definitions |
| recall | 3 depths at 8k | 5 depths at 32k | a fact hidden at a given depth of a filler document: found or not |
| memory | throughout | throughout | `nvidia-smi` every 0.5 s: VRAM idle, mean, peak; `llama-server` host RAM peak |

Document sizes are calibrated in the model's own tokens with `/tokenize`. A context size larger than the loaded
context is skipped and reported as skipped. Statistics per metric: median, nearest-rank p95, mean, min, max and
coefficient of variation, so the Lab can show how noisy a number is.

**Router suite** (`bench.router_suite`): 12 labelled requests (chat, screen, files, shell, web, memory, desktop,
code, maths, browser, disk). Each is sent through the router's real decision path with the real tool catalog; the
case passes when the router picks the expected tool family. It reports accuracy, latency (median and p95), and how
many requests the router could not answer at all (with the first error), so a broken router is not reported as
merely inaccurate. Cases whose tools are disabled are skipped, not failed.

**Scenery cost** (Lab > Benchmarks > Scenery cost): the page sets the scenery to Full, runs a decode benchmark while
recording frame times with `requestAnimationFrame`, then repeats with the scenery Off, and saves both halves as one
report with the percentage difference. During this test the page overrides the "pause the scenery while busy"
setting, otherwise Full would be paused and the test would measure nothing.

Reports are saved in `data/bench/<time>_<kind>_<model>.json`. `GET /api/bench/{id}?fmt=md` exports one as Markdown;
`GET /api/lab/report?mid=...&fmt=md|json` exports the model's fingerprint, profiles, trials and latest benchmarks
together. Every report says which numbers were measured; anything missing reads "not measured".

## 6. What HAPO does not do (yet)

- It does not re-tune by itself. A stale profile is flagged; the user decides when to spend the time.
- It does not adapt at runtime to temperature or clock drift. The bench records VRAM and run-to-run variation; there
  is no thermal sampling yet.
- The knobs from `docs/INFERENCE_RESEARCH.md` "Still open" are not in the tuner's search space. Each has to win a
  measured A/B on the target PC before it is added.
