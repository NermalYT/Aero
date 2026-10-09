# Aero

**The Frutiger Aero, lightweight and efficient local LLM bootstrapper for Windows, Linux and macOS.**

Aero scans your computer, picks a model and quant that fit it, installs the right llama.cpp build for your GPU
(NVIDIA, AMD, Intel, Apple Silicon or none), tunes the model to the VRAM you allow, and gives you a local agent that
can use your files, shell, screen, apps and browser. Everything local runs on 127.0.0.1, and with **strict offline**
on, nothing leaves the computer at all. When you want a second opinion, ChatGPT and/or Claude review the finished
work, and your local model learns from what they found.

![Aero at work: an agent hands part of the job to a subagent, then opens Notepad while the header says so](screenshots/agents-control.jpg)

## Download and install

Get the file for your system from the [latest release](https://github.com/NermalYT/Aero/releases/latest).

| System | Download | Then |
| --- | --- | --- |
| **Windows** 10 / 11 (64-bit) | `Aero-windows.zip` | Extract it, double-click **`Update-Aero.bat`**, accept the admin prompt. Installs into `C:\Aero` with Start menu and desktop shortcuts, and lists Aero under **Settings → Apps → Installed apps**. |
| **macOS** (Apple Silicon or Intel) | `Aero-macos.zip` | Unzip it, right-click **`Install-Aero.command`** → Open. Adds `~/Applications/Aero.app`. |
| **Linux**, any distro | one line in a terminal (below) | Installs for your user into `~/.local/share/aero`, with an app-menu entry and the `aero` command. |
| Debian, Ubuntu, Mint, Pop!_OS | `aero_<version>_all.deb` | `sudo apt install ./aero_*_all.deb`, then start Aero from the app menu or run `aero`. |
| Fedora, RHEL, Rocky, Alma, openSUSE | `aero-<version>-1.noarch.rpm` | `sudo dnf install ./aero-*.rpm` (openSUSE: `sudo zypper install ./aero-*.rpm`), then run `aero`. |
| Arch, Manjaro, EndeavourOS | `PKGBUILD` | `makepkg -si` in a folder holding the `PKGBUILD`, then run `aero`. |

Linux and macOS, one line (downloads the newest release and installs it):

```sh
curl -fsSL https://github.com/NermalYT/Aero/releases/latest/download/install.sh | sh
```

The installer finds your package manager (apt, dnf, zypper, pacman, apk, xbps, eopkg, emerge, swupd or Homebrew),
shows the command before it installs anything missing (Python 3.10+, the Vulkan loader, the OpenMP runtime) and
asks first. It never needs to run as root. Then the model chooser recommends a model for your computer, and Aero
opens. `sh install.sh --help` lists its options.

**Uninstall:** on Windows, **Settings → Apps → Installed apps → Aero → ⋯ → Uninstall** (or Control Panel →
Programs and Features), like any other program. It shows what it will remove, with two boxes, both ticked: delete
downloaded models, and delete chats, memory and settings. Untick one to keep that folder. It stops Aero, then removes
`C:\Aero`, the Start menu and desktop shortcuts, Aero's temporary files and its Installed apps entry. Python (if the
installer added it) is a separate program in the same list. On Linux and macOS, run `sh ~/.local/share/aero/uninstall.sh`
(macOS: `sh ~/Library/Application\ Support/Aero/uninstall.sh`; add `--all` to delete models and chats too). The
`.deb`, `.rpm` and Arch package remove their `aero` command with the package manager; run `uninstall.sh` first to
remove your own Aero folder.

**Updates:** Aero checks GitHub for a newer release once when it starts. **Update now** downloads it, verifies its
checksum and installs it; models, chats, memory, settings, mods and tunings are kept.

**Needs:** 8 GB RAM or more (16 GB is comfortable) and an internet connection for the install and model downloads.
A GPU is optional. An install under Aero's earlier names (`C:\Halcyon` or `C:\VRAMpire`) is moved to `C:\Aero` with
everything in it.

## What it does

- **Fits any PC.** Reads the GPUs (live VRAM on NVIDIA; the driver's VRAM size on AMD and Intel), RAM and CPU, then
  recommends the largest-quality model that fits: fully on the GPU, MoE experts in system RAM, or CPU only.
- **Tunes for real.** Measures llama.cpp settings on your PC within a VRAM limit you choose. HAPO turns the measured
  trials into profiles (Maximum Speed, Balanced, Maximum Context, Maximum Quality, Agent Optimized, Efficiency).
- **A tiny router on the CPU** reads each request first and gives the main model only the tools it needs.
- **Agents and subagents.** Each chat's task gets an agent named for what it does ("Photo Renamer", "Crash
  Investigator"). An agent can hand a self-contained part of the job to a subagent (a fresh copy of the local model
  with its own context) and gets back its report. The dashboard lists models, agents and subagents in one scrolling
  card: hover for what each is doing, click to talk to it.
- **Name an app, Aero finds it.** It reads what's installed (Start menu, Installed apps, Store apps and link
  handlers on Windows, `.desktop` files on Linux, `/Applications` on macOS) and tells the model which program you
  mean and how to start it, then checks that it really started. "Open my Bloxstrap" finds Bloxstrap and knows Roblox
  links open through it.
- **Background means background.** In other apps, Aero works through UI Automation and window messages, reads values
  back after setting them, and reports input an app ignored as failed. Using your real mouse and keyboard needs your
  OK first (**Foreground control required**), waits until you stop typing, and puts your window back; **Strict
  Background Only** turns it off entirely. One agent per window.
- **You always know when it's driving.** A header across the top of Aero (and a banner over the app, when there is
  one on screen) says who is working and how: "*model* is controlling *app* in the background", "… with your mouse
  and keyboard", "… is using Aero's browser in the background". **Stop** stops it and releases everything it held.
- **Questions that don't stop the task.** When one detail is missing ("Which Gmail account should I check?"), the
  model asks with a card in the chat and keeps working on everything else; your answer, even later, continues the
  same task.
- **Whole web pages, with sources.** Aero's own browser runs in the background, gives each agent its own tab, and
  reads entire pages as sections with headings, tables and links, saying exactly what was left out.
- **Remote Mode.** While someone views your PC over Remote Desktop, part of the model moves to the CPU so the remote
  session has GPU memory, and comes back when they leave (measured on an RTX 5080: 1.7 GB freed, at a speed cost).
- **Optional cloud reviews, two buttons.** **ChatGPT**: GPT-6 Astra reviews, GPT-6.1 Sol repairs. **Claude**: Claude
  Fable 5.1 reviews, Claude Opus 5.5 repairs. With both on, ChatGPT goes first and Claude reviews the final state with
  ChatGPT's findings. Every problem found becomes a lesson for your local model. Connect with an API key or your
  ChatGPT / Claude plan through the official Codex CLI / Claude Code sign-in.
- **Local Only: On / Off.** One button next to ChatGPT, Claude and Loop. On, the model answers by itself with no
  web, browser, MCP servers or cloud reviews. Off, it searches and reads the web when a task needs it.
- **Strict offline.** Blocks every non-loopback request inside Aero, skips cloud reviews, hides network tools and
  keeps an audit log. Local chat keeps working with no network at all.
- **Performance Lab.** Benchmarks (time to first token, prefill, decode, long-context recall, JSON and tool-call
  accuracy), a router suite, and a test of what the scenery costs your generation speed. Real measurements only.
- **Mod Aero.** Describe a change to Aero in plain words and your local model makes it in a copy of Aero's code.
  Aero compiles it, runs its tests and starts a test copy before you can apply it, shows the diff, and can turn it
  off or undo it any time. A mod that breaks startup is undone automatically. Mods carry over to new versions.
- **Forever-loop with a journal.** The ∞ Loop repeats a task until you stop it. After every round the model writes
  down what worked, what didn't and what to do next, and the next round starts from that, so it improves its own
  workflow as it goes.
- **Models learn from each other.** Aero records which tools worked or failed on each task and which model did it,
  and the model writes notes when something goes wrong or you give feedback. Whichever model you load later gets
  the notes from similar tasks, and every model knows your stated preferences.
- **Frutiger Aero.** Glass over a day or night landscape with a frog pond. Scenery Full, Still or Off; it pauses
  while a model is working.

| Day | Night |
|---|---|
| ![Day](screenshots/day.jpg) | ![Night](screenshots/night.jpg) |

| Talking to a subagent | Models, agents and subagents |
|---|---|
| ![A chat with the subagent Photo Scout](screenshots/subagent-chat.jpg) | ![Hovering a subagent shows its task and report](screenshots/agents-panel.jpg) |

| Both review passes (demo chat) | Performance Lab |
|---|---|
| ![Review passes](screenshots/review-passes.jpg) | ![Performance Lab](screenshots/performance-lab.jpg) |

| Narrow window |
|---|
| ![Narrow window](screenshots/narrow.jpg) |

![A frog meeps](screenshots/frog-meep.jpg)

## Documentation

- [Full manual](source/README.md): install, the model chooser, connecting ChatGPT and Claude, offline and privacy,
  tools, troubleshooting.
- [Changelog](source/CHANGELOG.md)
- [Validation report](source/docs/VALIDATION_REPORT.md): what has been tested, and what still needs a Windows PC.
- 1.1: [architecture](source/docs/V1.1_ARCHITECTURE.md), [test matrix](source/docs/V1.1_TEST_MATRIX.md),
  [performance](source/docs/V1.1_PERFORMANCE_REPORT.md), [limits](source/docs/V1.1_LIMITATIONS.md),
  [baseline](source/docs/V1.1_BASELINE.md), [open-source research](source/docs/V1.1_OPEN_SOURCE_RESEARCH.md),
  [third-party notices](THIRD_PARTY_NOTICES.md).
- [Local execution audit](source/docs/LOCAL_EXECUTION_AUDIT.md), [HAPO](source/docs/HAPO_ARCHITECTURE.md),
  [performance report](source/docs/PERFORMANCE_REPORT.md), [inference research](source/docs/INFERENCE_RESEARCH.md),
  [design system](source/docs/AERO_DESIGN_SYSTEM.md).

## Status

1.1.0 adds app discovery, honest background control, questions during a task, whole-page reading and Remote Mode.
What has passed for 1.1:

- 218 automated tests on Windows 11, and in CI on Windows, Ubuntu and macOS.
- Live on Windows 11: background typing and button presses verified with the window in front, the cursor and the
  clipboard unchanged; ignored input reported as failed; stale controls refused; minimized windows handled.
- Live in Edge (Playwright): a long page read below the fold, table rows, a form filled and checked.
- Remote Mode's GPU/CPU split measured on an RTX 5080 (planned 79.83 %, measured 79.83 %).

Not tested for 1.1: a real Remote Desktop or RustDesk session from another machine, real Gmail accounts, macOS and
Linux desktops, AMD and Intel GPUs ([test matrix](source/docs/V1.1_TEST_MATRIX.md)).

From 1.0.0:

- All 118 automated tests, on Linux (Python 3.11 and 3.12) and on Windows 11 (Python 3.12).
- On Windows 11: Aero's Installed apps entry and its uninstaller, end to end (listed by Windows and winget,
  uninstalled with the same command Settings runs, keeping or deleting models and chats, stopping a running copy,
  deleting the shortcuts and the entry).
- A live run of the UI, a real self-update, and installs from the release files on Ubuntu 24.04 and 22.04, Fedora
  42 and Arch Linux (containers, no GPU).

Not tested yet: a full Windows install with a GPU, macOS, GPU speeds and real ChatGPT and Claude calls. See the
[validation report](source/docs/VALIDATION_REPORT.md). To check a Windows install without changing it:

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

CI (`.github/workflows/ci.yml`) runs the tests on Windows, Ubuntu and macOS for every push. Two opt-in checks need a
real desktop: `AERO_LIVE_UI=1` (Windows background control, `tests/test_app_background.py`) and
`AERO_LIVE_BROWSER=1` (Playwright with Edge or Chrome). `validation/measure_remote_mode.py` and
`validation/bench_router_intents.py` measure Remote Mode and the router on your own hardware.

`python tools/build_release.py` builds every release file into `dist/` (the Windows, macOS and Linux archives, the
`.deb`, the `.rpm`, the `PKGBUILD` and `SHA256SUMS.txt`). The `.deb` needs `dpkg-deb`, the `.rpm` needs `rpmbuild`.

To publish a release, raise `VERSION` in `source/aero/config.py`, add `.github/release-notes/v<version>.md`, then
run **Actions → Release → Run workflow** (or push the tag `v<version>`). The workflow runs the tests, builds every
file above and publishes them as the newest release, which is what Aero's updater installs. Tick **Replace**
to rebuild a release that already exists for that version.

## Licence

MIT, see [LICENSE](LICENSE). Bundled highlight.js, marked and DOMPurify keep their own licences. Models you download
have their own licences, shown in the chooser.
