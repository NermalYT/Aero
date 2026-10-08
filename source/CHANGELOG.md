# Changelog

## Aero 1.0.0 (2026-10-08)

The first public release. Aero is the app that was called Halcyon (and VRAMpire before that), rebuilt as a
Frutiger Aero styled, lightweight bootstrapper for local LLMs on any Windows PC.

### Install and upgrade

- Installs into `C:\Aero`. Run `Update-Aero.bat` from the extracted zip (or from a GitHub download of the repo).
- **Upgrading from Halcyon or VRAMpire**: the updater moves `C:\Halcyon` (or `C:\VRAMpire`) to `C:\Aero` with every
  model, chat, memory, setting and tuning, rebuilds the Python environment, rewrites saved paths in `settings.json`,
  `models.json`, `mcp.json` and `router.json`, and replaces the old Desktop and Start Menu shortcuts and icon with
  Aero's. If you never edited the old "About you" text, the old built-in one is copied into your settings so it
  carries over (new installs start with an empty profile). Nothing is deleted. If Windows won't move the folder
  (something has a file open inside it), nothing changes and the updater says what to close.
- The installer now picks the llama.cpp build for the GPU it finds: CUDA (matched to the NVIDIA driver, 12.8+ on
  RTX 50-series), Vulkan for AMD and Intel cards, or the CPU build on a PC without a dedicated GPU.

### Any PC: hardware scan and per-PC choices

- Aero scans the PC: NVIDIA cards through `nvidia-smi` (live VRAM), AMD and Intel cards through the Windows display
  driver registry (the 64-bit VRAM size), AMD on Linux through sysfs, CPU and RAM through psutil. Integrated GPUs are
  left out; several GPUs are pooled.
- On cards Windows can't read live (AMD, Intel), Aero measures its own model's VRAM from llama.cpp's buffer report
  instead of guessing, and the VRAM-limit check uses that.
- The model chooser and Settings show a recommended main model and quant for this PC (largest quality that fits
  the VRAM, MoE expert offload where it helps, CPU-only fallbacks), and a recommended router for its CPU. Enter
  takes the recommendation.
- The first-load tuning dialog suggests a VRAM limit from what the card has and what other apps use.

### ChatGPT review pass (new)

- A **ChatGPT** button next to the **Claude** button under the message box. Each cycles off / on / auto.
- GPT-6 Astra reviews the local model's finished work (read-only tools, same verdicts as Claude: ok, minor, major).
  Minor problems go back to the local model to fix; major ones go to GPT-6.1 Sol, which repairs the work.
- With both buttons on, ChatGPT's pass runs first and Claude's second, and Claude's review packet includes Astra's
  verdicts and Sol's changes, so Claude reviews the final state with the full history.
- Every review that found problems, from either pass, becomes lessons for the local model (long-term memory) and a
  training example in `data\training\corrections.jsonl`, including Sol's and Opus's final answers.
- Two ways to connect (Settings → ChatGPT): an OpenAI API key (stored encrypted with Windows DPAPI, daily budget,
  prices from OpenAI's model pages), or your ChatGPT plan through the official Codex CLI (`codex login` on OpenAI's
  own page). Aero never reads the Codex login file; it only runs `codex login status`. Sol's plan-mode repairs ask
  once per repair before Codex may edit files in your work folder.
- Strict offline mode skips both passes before anything is sent.

### Local-only inference

- Every token a local model generates comes from a `llama-server` Aero started itself, bound to 127.0.0.1, with
  `--offline` on builds that support it. No hosted engine is in the code path.
- **Strict offline** (Settings → Privacy & offline): blocks every non-loopback request inside Aero's process at the
  HTTP transport, hides web, browser and network MCP tools, skips cloud reviews, and keeps an audit log of every
  outbound request (allowed or blocked) without paths or query strings.
- A live list of every listening socket of Aero and its llama-server children, with a loopback check.

### Speed and tuning

- **HAPO** (Aero Adaptive Performance Optimization): from the tuner's measured trials, Aero offers profiles
  (Maximum Speed, Balanced, Maximum Context, Maximum Quality, Agent Optimized, Efficiency), each with its measured
  numbers and the rule that picked it. Apply one, roll back,
  and see when a profile was made on different hardware or llama.cpp. Nothing is guessed: a profile is always a
  configuration that was actually run.
- **Performance Lab**: benchmark suites (quick, deep, decode) with time to first token, prefill and decode speed,
  long-context recall, JSON and tool-call accuracy, prompt-cache reuse and speculative-decoding acceptance; a router
  suite; and a Scenery cost test that measures generation speed with the scenery on and off. Reports export as
  Markdown or JSON.
- Works with llama.cpp v0.5.0+ (`--load-mode` instead of the removed `--mlock` and `--no-mmap`). Halcyon 2.0's
  router failed to start on those builds.

### Frutiger Aero redesign

- Glass panels over a day or night landscape: sky, sun or moon and stars, clouds, light ribbons, iridescent bubbles,
  grassy hills with flowers, fireflies at night, and a pond with three original frogs (sitting on a lily pad,
  peeking from the water, resting on a stone). Frogs keep their mouths closed and every so often one meeps.
- Scenery Full, Still or Off; the frog pond can be turned off; motion follows Windows' reduced-motion setting; the
  scene pauses while the window is hidden and, by default, while a model is generating, tuning or benchmarking.
- **Enable transparency** (Settings → Appearance) keeps the look but drops the glass blur, which is the most
  expensive part of the scene to draw.
- New app icon: an iridescent soap-glass bubble, in every Windows size from 16 to 256 px.

### Other

- Every model's system prompt now explains both review passes.
- `validation\Validate-Aero.ps1`: checks a real install without changing it (see `docs/VALIDATION_REPORT.md`).
- Documentation: `docs/INFERENCE_RESEARCH.md`, `docs/LOCAL_EXECUTION_AUDIT.md`, `docs/HAPO_ARCHITECTURE.md`,
  `docs/PERFORMANCE_REPORT.md`, `docs/AERO_DESIGN_SYSTEM.md`, `docs/VALIDATION_REPORT.md`.

### Compatibility

- Settings, chats, memory, tunings and models from Halcyon 2.x and VRAMpire 1.x carry over. Tunings stay valid
  because they are keyed by model file, GPU and llama.cpp build, not by the folder.
- The Python package is now `aero` (`python -m aero`); old shortcuts that ran `-m halcyon` or `-m vrampire` are
  replaced by the updater.
- Ports are unchanged: 8180 app, 8181 model, 8182 tuning trials, 8183 advisor, 8184 router, 8185 cursor overlay.
