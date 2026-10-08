# Validation report

Aero 1.0.0, 2026-10-08. What was actually tested, where, and what still needs a Windows PC or a person.

**Where the tests ran.** A Linux cloud container: Intel Xeon at 2.8 GHz, 4 cores, 16 GB RAM, **no GPU**, Python 3.11,
Node 22, Chromium (software rendering), PowerShell 7.4.6, llama.cpp 0.5.0-dev (a portable CPU build). Aero is a
Windows app; nothing in this report ran on Windows. Every "Passed" below means passed in that container.

The live model in these runs is `tinytest-f16`, a tiny model with random weights. It loads, tunes and streams tokens
like a real model, so it proves the plumbing works, but its answers are noise: its speed numbers say nothing about a
real model on a real PC, and quality scores (recall, JSON and tool-call accuracy) are meaningless for it.

## 1. Automated tests

`python -m unittest discover -s tests -v` from `source/`: **57 tests, all passed.** None reaches the network: the
OpenAI API is answered by an in-process mock transport, the Codex CLI is a small fake script, nvidia-smi and the
Windows registry are faked, and every test uses a throwaway data folder.

| File | Tests | Covers |
|---|---|---|
| `tests/test_chatgpt.py` | 21 | GPT-6 Astra review with read-only tools; reviewer can't write; GPT-6.1 Sol repair writes; plain-text and unverified-org retries; daily budget cap; prices; Codex CLI status reads only the sign-in method (a masked key in its output is never kept); strict offline never runs the CLI; plan review read-only and no `OPENAI_API_KEY` reaches the CLI; plan repair asks before writing; unsafe model names refused; pass order ChatGPT then Claude, Claude's packet carries Astra's verdicts and Sol's answer, lessons from both passes, either pass alone, minor findings go back to the local model, a ChatGPT failure still runs Claude, strict offline skips both |
| `tests/test_hapo_bench_offline.py` | 14 | HAPO profile rules, Pareto set, goals with no measurement, apply and restore after a failed load, old tuning caches; benchmark JSON and tool-call scoring, router suite scoring, reports mark missing results; strict offline (remote blocked, loopback allowed, async client blocked, network tools hidden); loopback detection |
| `tests/test_hardware.py` | 6 | Hardware scan on PCs other than the reference one: NVIDIA live through nvidia-smi and not listed twice; AMD from the registry's 64-bit VRAM size; Intel Arc found, integrated GPUs and the basic display adapter left out; iGPU-only means CPU-only; two GPUs pooled; a recommendation that fits each sample PC (table below) |
| `tests/test_installer.py` | 10 | llama.cpp build choice: newest CUDA build the driver supports, never newer; RTX 50-series needs 12.8+, else Vulkan; AMD and Intel get Vulkan; no GPU gets the CPU build; registry scan for AMD and Intel cards with iGPUs left out |
| `tests/test_migrate.py` | 6 | Move from `C:\Halcyon`: saved paths rewritten (case-insensitive), model ids follow their new paths, MCP config updated, safe to run twice; old built-in "About you" text carried over only when the user never saved one; an unreadable legacy file is kept; settings upgraded once |

What the catalog recommends for sample PCs (from `test_recommendations_fit_each_pc`; estimates, which the tuner then
replaces with measurements on first load):

| PC (faked scan) | Main model | Router |
|---|---|---|
| RTX 5080 16 GB, 32 GB RAM | Qwen3.8-27B IQ3_S, 12.0 GB, fully on the GPU | MiniCPM5-2B |
| RX 9070 XT 16 GB, 32 GB RAM | Qwen3.8-27B IQ3_S, 12.0 GB, fully on the GPU | MiniCPM5-2B |
| Intel Arc B580 12 GB, 32 GB RAM | Qwen3.6-35B-A3B Q4_K_XL, 20.9 GB, MoE experts in RAM | MiniCPM5-2B |
| RTX 3060 Laptop 6 GB, 16 GB RAM | Gemma 4 26B-A4B IQ3_XXS, 9.9 GB, MoE experts in RAM | MiniCPM5-2B |
| No GPU, 16 GB RAM, 4 cores | Qwen3.5-4B Q6_K, 3.7 GB, CPU | LFM2.5-1.2B-Instruct |
| No GPU, 8 GB RAM, 4 cores | Qwen3.5-4B Q6_K, 3.7 GB, CPU | LFM2.5-1.2B-Instruct |

## 2. Static checks

| Check | Result |
|---|---|
| `node --check` on `app.js`, `lab.js`, `scene.js` | Passed |
| `pyflakes` on `aero/`, `installer/`, `tests/` | Passed except intentional ones: side-effect tool imports in `tools/__init__.py`, the `claude_agent_sdk` availability probe, and one unused `server_version()` result in `server.py` |
| `validation/Validate-Aero.ps1` with PowerShell's own parser (7.4.6) | 0 errors; the file is ASCII only, written for Windows PowerShell 5.1 (no 7.x-only syntax) |
| PSScriptAnalyzer | Not run: it couldn't be installed in the container |

## 3. The running app (live, container)

`Validate-Aero.ps1`'s API and streaming checks were run from PowerShell 7.4.6 against a Linux copy of the backend
(port 8195, `llama-server` on 8201, throwaway data folder), using the script's own `Api` and `Sse` helpers:

| Check | Result |
|---|---|
| Backend answers | Passed: Aero 1.0.0 |
| Hardware scan | Passed: Xeon at 2.8 GHz, 0 GPUs, 16,094 MB RAM |
| Recommendation for this PC | Passed: Qwen3.5-4B Q6_K, CPU, 3.73 GB |
| ChatGPT and Claude status without a sign-in | Passed: both report not connected, nothing called |
| Strict offline blocks a real request and logs it | Passed: Hugging Face search refused; audit line `huggingface.co blocked`, no path or query kept |
| Model loads with its saved tuning | Passed: ctx 32,768, CPU only, kv f16, ub 1024, 4 threads, 128.6 tok/s from the tuner |
| Every listening socket is loopback | Passed: backend :8195 and `llama-server` :8201 on 127.0.0.1 |
| Local chat streams with strict offline on | Passed: first token after 26.5 s (CPU prefill of the system prompt on 4 shared cores), stopped by the script's cap because this model never ends a reply |
| HAPO | Passed: tuned; Maximum Speed, Balanced, Maximum Context, Maximum Quality, Agent Optimized, Efficiency |
| Performance Lab report export | Passed: Markdown report, 4,495 characters |
| Router status | Passed: configured, not started (the router run itself is covered in `PERFORMANCE_REPORT.md` section 5) |

The tuning, benchmark, router suite and Scenery cost numbers are in `docs/PERFORMANCE_REPORT.md`.

## 4. Screens

29 screenshots of the running app in Chromium, all with no page errors, in `screens/aero-1.0/` next to the release
zip: day and night; scenery Full, Still and Off; the frogs with mouths closed and meeping (day and night); a chat that
went through both review passes (ChatGPT first, Claude second, then "Your local model learned"); the composer with
both review buttons on; Settings > ChatGPT, Appearance and Privacy & offline (strict); Performance Lab profiles;
820 px wide (closed, dashboard open, sidebar open, in a chat); 1180 px; 1080p, 1440p and 4K; 125%, 150% and 200%
scaling; transparency off. All 14 Settings sections were also checked by script for elements running past the
window edge: none.

## 5. Defects found while validating, and fixed

| Defect | Fix |
|---|---|
| Settings > ChatGPT ran off the right edge: one long unbroken line forced the pane to about 1,490 px | Settings content column can shrink (`minmax(0, 1fr)`), long rows wrap |
| When the composer row wrapped, the Send button jumped to the left | The hint and Send are one group that always sits on the right |
| Windows narrower than 1,180 px opened with the dashboard covering the chat | Narrow windows start with the dashboard closed; the button opens it for that window without changing the saved setting |
| Below 860 px the sidebar covered the chat | It starts closed there and closes again after picking a chat |
| The Codex CLI's pipes were left open after a run | Reader threads joined and pipes closed |
| `Validate-Aero.ps1` could wait forever on a model that never finishes a reply | Chats are capped (`StopAfterSec`) and stopped through `/api/stop` |
| Halcyon 2.0's router failed to start on llama.cpp 0.5.0 (`--mlock`, `--no-mmap` removed) | Uses `--load-mode` on builds that have it |

## 6. Not tested

Not run because the container has no Windows, no GPU, or no sign-in. Nothing below is claimed to work until
`Validate-Aero.ps1` or a person checks it.

- **Install and upgrade on Windows**: `Update-Aero.bat`, `Install-Aero.bat`, the Python environment build, the
  llama.cpp download for each GPU kind, the move from `C:\Halcyon` or `C:\VRAMpire` to `C:\Aero` on a real disk
  (the path rewriting itself is unit-tested), Desktop and Start Menu shortcuts, the icon in Explorer and the
  taskbar, the Edge app window.
- **The Windows parts of `Validate-Aero.ps1`**: CIM and registry hardware checks, nvidia-smi, the icon file sizes,
  shortcuts, the Windows Firewall test, and the whole script under Windows PowerShell 5.1.
- **GPU inference**: CUDA and Vulkan speeds, VRAM-limited tuning on a real card, llama.cpp's buffer measurement on
  AMD and Intel cards, multi-GPU splitting.
- **Real model quality**: the benchmark's recall, JSON and tool-call scores with a real model.
- **Real ChatGPT and Claude reviews**: the OpenAI API path, the Codex CLI plan path, and the Claude paths were tested
  with mocks and fakes only. No real sign-in or API key was used.
- **Windows-only tools**: PowerShell shell tool, screen and app control, the cursor overlay, DPAPI key storage.
- **Scenery cost on a PC with a GPU** (the container renders in software on the same cores as the model).
- **Accessibility**: a screen reader, Windows high-contrast mode, Windows' "Animation effects" switch (the CSS
  `prefers-reduced-motion` rule is in place; it was not checked on Windows).

## 7. Known limitations

- Strict offline works inside Aero's own process. The shell tool runs PowerShell, which can reach the network if a
  command asks it to, and programs Aero starts are outside its control. `Validate-Aero.ps1 -FirewallTest` proves
  local chat works with Aero's traffic blocked at the firewall.
- On AMD and Intel cards under Windows there is no cheap live VRAM reading. Aero estimates what the desktop holds
  (1 GB) and measures its own model from llama.cpp's buffer report.
- Model recommendations are estimates from file sizes and a typical memory bandwidth; the first load's tuning
  measures the real numbers on the PC.
- The ChatGPT plan path needs the Codex CLI (`npm install -g @openai/codex`) and a `codex login`; the Claude plan
  path needs Claude Code and `claude auth login`. API keys work without either.
- The installer and launcher are Windows only. The backend runs on Linux for development and testing.

## 8. Checking a real install

From an ordinary PowerShell window (it changes nothing in `C:\Aero`):

```powershell
powershell -ExecutionPolicy Bypass -File C:\Aero\app\validation\Validate-Aero.ps1 -OpenUI
```

Add `-Bench deep` for the full benchmark suite, `-Router` for the router suite, `-TuneLimitGB 14` to tune an untuned
model in the throwaway copy, `-CompareTo <earlier results.json>` to compare runs, and, from an administrator window,
`-FirewallTest`. The report lands in `%USERPROFILE%\AeroTest\run-<time>\REPORT.md`, with a "Needs a person"
checklist (icon at your scaling, Full/Still/Off scenery, frogs meeping and never covering controls, Animation
effects off, both review buttons in order).
