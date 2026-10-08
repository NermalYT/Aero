# Aero

**The Frutiger Aero, lightweight and efficient local LLM bootstrapper for Windows.**

Aero scans your PC, picks a model and quant that fit it, installs the right llama.cpp build for your GPU (NVIDIA,
AMD, Intel or none), tunes the model to the VRAM you allow, and gives you a local agent that can use your files,
shell, screen, apps and browser. Everything local runs on 127.0.0.1, and with **strict offline** on, nothing leaves
the PC at all. When you want a second opinion, ChatGPT and/or Claude review the finished work, and your local model
learns from what they found.

![Aero by day](screenshots/day.jpg)

## Download and install

1. Download the latest **`Aero.zip`** from [Releases](https://github.com/NermalYT/Aero/releases), or **Code → Download
   ZIP** on this page.
2. Extract the whole zip anywhere.
3. Double-click **`Update-Aero.bat`** and accept the admin prompt.

It installs into `C:\Aero`, then opens the model chooser with a recommendation for your PC. To update later,
download the new zip and run its `Update-Aero.bat`; your models, chats, memory, settings and tunings are kept. An install under Aero's earlier names
(`C:\Halcyon` or `C:\VRAMpire`) is moved to `C:\Aero` with everything in it.

**Needs:** Windows 10 or 11 (64-bit), 8 GB RAM or more (16 GB is comfortable), and an internet connection for the
install and model downloads. A GPU is optional.

## What it does

- **Fits any PC.** Reads the GPUs (live VRAM on NVIDIA; the driver's VRAM size on AMD and Intel), RAM and CPU, then
  recommends the largest-quality model that fits: fully on the GPU, MoE experts in system RAM, or CPU only.
- **Tunes for real.** Measures llama.cpp settings on your PC within a VRAM limit you choose. HAPO turns the measured
  trials into profiles (Maximum Speed, Balanced, Maximum Context, Maximum Quality, Agent Optimized, Efficiency).
- **A tiny router on the CPU** reads each request first and gives the main model only the tools it needs.
- **Optional cloud reviews, two buttons.** **ChatGPT**: GPT-6 Astra reviews, GPT-6.1 Sol repairs. **Claude**: Claude
  Fable 5.1 reviews, Claude Opus 5.5 repairs. With both on, ChatGPT goes first and Claude reviews the final state with
  ChatGPT's findings. Every problem found becomes a lesson for your local model. Connect with an API key or your
  ChatGPT / Claude plan through the official Codex CLI / Claude Code sign-in.
- **Strict offline.** Blocks every non-loopback request inside Aero, skips cloud reviews, hides network tools and
  keeps an audit log. Local chat keeps working with no network at all.
- **Performance Lab.** Benchmarks (time to first token, prefill, decode, long-context recall, JSON and tool-call
  accuracy), a router suite, and a test of what the scenery costs your generation speed. Real measurements only.
- **Frutiger Aero.** Glass over a day or night landscape with a frog pond. Scenery Full, Still or Off; it pauses
  while a model is working.

| Night | Both review passes (demo chat) |
|---|---|
| ![Night](screenshots/night.jpg) | ![Review passes](screenshots/review-passes.jpg) |

| Performance Lab | Narrow window |
|---|---|
| ![Performance Lab](screenshots/performance-lab.jpg) | ![Narrow window](screenshots/narrow.jpg) |

![A frog meeps](screenshots/frog-meep.jpg)

## Documentation

- [Full manual](source/README.md): install, the model chooser, connecting ChatGPT and Claude, offline and privacy,
  tools, troubleshooting.
- [Changelog](source/CHANGELOG.md)
- [Validation report](source/docs/VALIDATION_REPORT.md): what has been tested, and what still needs a Windows PC.
- [Local execution audit](source/docs/LOCAL_EXECUTION_AUDIT.md), [HAPO](source/docs/HAPO_ARCHITECTURE.md),
  [performance report](source/docs/PERFORMANCE_REPORT.md), [inference research](source/docs/INFERENCE_RESEARCH.md),
  [design system](source/docs/AERO_DESIGN_SYSTEM.md).

## Status

1.0.0 is the first public release. Its automated tests and a live run of the backend passed on Linux (no GPU); the
Windows install, GPU speeds and real ChatGPT and Claude calls have not been tested yet. To check an install on your
PC without changing it:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Aero\app\validation\Validate-Aero.ps1 -OpenUI
```

## Development

The app is Python (FastAPI) in `source/aero`, with the UI in `source/aero/static`. Tests run anywhere:

```
cd source
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

## Licence

MIT, see [LICENSE](LICENSE). Bundled highlight.js, marked and DOMPurify keep their own licences. Models you download
have their own licences, shown in the chooser.
