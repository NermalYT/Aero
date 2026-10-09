# Changelog

## Aero 1.1.0 (2026-10-09)

Real app work: Aero knows which apps you have, acts in other apps without taking your input or focus (and says so
when it can't), reads whole pages, asks small questions while the rest of the task keeps going, and frees GPU memory
while someone views your PC over Remote Desktop. Design, measurements and limits: `docs/V1.1_ARCHITECTURE.md`,
`docs/V1.1_PERFORMANCE_REPORT.md`, `docs/V1.1_TEST_MATRIX.md`, `docs/V1.1_LIMITATIONS.md`.

### Apps by name

- **App registry** (`app_registry.py`, `app_catalog.py`). Windows: Start-menu shortcuts (read without COM), the
  Installed apps list, App Paths, link-scheme handlers and `Get-StartApps` (Store apps); Linux: `.desktop` files;
  macOS: `/Applications`. About 400 ms for 222 apps on the test PC, cached in `data/apps.json` and re-scanned when
  something is installed or removed.
- **`app_find`** ranks matches with a confidence and a reason, flags two equally good matches as ambiguous, and
  keeps apart apps that belong together but aren't the same (Bloxstrap is not Roblox, but Roblox links open through
  it).
- **`app_launch`** starts an app without taking your focus where the app allows it, with an app link when given
  (`roblox://experiences/start?placeId=…`), and reports what really happened: a new process or window of the app,
  the app already running, or nothing. Launch methods that worked are remembered per computer (method only) and
  forgotten when the program changes. Programs in Downloads, temp folders or network shares need your OK.
- Names in a message ("open my Bloxstrap", "in Word", "on Spotify") are found without a model call and passed to
  the model with how to open them; common words only count when written like a name or after "open", "my", "in"….
- Settings → **Apps**: search, your own names for apps, launches that worked.

### Background control that tells the truth

- `app_view` shows each window's **control mode**: background through UI Automation, background through window
  messages, needs foreground control, or read-only (programs running as administrator).
- Typing sets values directly and **reads them back**. Classic Win32 text fields get `WM_SETTEXT` (UI Automation's
  SetValue on them brought the window to the front, measured on Windows 11). Push buttons get `BN_CLICKED`
  (`BM_CLICK` did the same). Other clicks and keys are compared before and after: input an app ignored is reported
  as **failed verification**, not done.
- A foreground guard around every action notices when an app took focus anyway, puts your window back where Windows
  allows it, says when it couldn't, and avoids that app's control and route afterwards.
- Element ids are tied to one view of one window of one process; stale ids are refused.
- Minimized or covered GPU windows get no picture (instead of a screen grab of whatever is on top) and are not
  un-minimized unless asked (`restore=true`).
- Each agent has its own selected window, and **one agent per window**: another agent acting on it is told who
  has it.

### Your mouse and keyboard

- Anything that uses them (`mouse_*`, `type_text`, `press_keys`, `focus_window`, `input="real"`, ctrl/alt/win
  shortcuts) shows **Foreground control required** first: Allow once, Allow for this task, Deny. A desktop
  permission of *auto* still counts as standing permission.
- Aero waits until you stop typing (Windows), takes an exclusive lock so no two agents use the input at once, and
  puts back the window you had in front.
- **Strict Background Only** (Settings → Tools) refuses all of it, with no "just one click" exception.
- Typing goes in short pieces so Stop works mid-text; on Windows through SendInput's Unicode mode, so the clipboard
  is never touched; elsewhere the clipboard's previous text is restored after pasting.
- Nothing falls back to real input on its own any more: the tool descriptions and the prompt no longer suggest
  retrying with `input="real"`.

### Questions while it works

- **`ask_user`** posts a question card (choices, free text, yes/no, numbers) and returns at once; the model keeps
  doing what doesn't depend on the answer, and **`get_answer`** waits only where it needs it (Stop still works).
  Repeated questions are merged; questions survive a restart; an answer given after the reply finished is sent into
  the same chat so the task continues.

### Tasks, Stop and evidence

- Every tool call is a node in a **task graph** (`task_graph.py`): a live step list in the chat, saved per turn,
  and a Stop summary of what finished and what didn't. The graph's scheduler runs independent work side by side and
  resumes a node when its question is answered.
- Read-only tool calls from one model step run side by side (up to four), results in order.
- Results carry an **evidence note** for the model: verified, sent but not verified, or failed verification;
  badges on the tool cards show the same.
- **Verify before repeating:** a send, post or other consequential call that timed out can't simply run again until
  the agent has looked at something in between (also across turns, for three days).
- **Stop** in one chat now stops only that chat (1.0 also denied other chats' pending approvals), releases windows,
  input and grants, and closes the agent's browser tab.
- The header says how a model is working: "… in the background", "… with your mouse and keyboard", "… is opening …".
  Background browsing puts no banner over your screen.

### Browser and pages

- Aero's browser runs **in the background** by default (Settings → Tools → Aero's browser); `browser_open(show=true)`
  opens a visible window, for example for you to sign in. It is Aero's own profile; password fields are refused.
- **One tab per agent**, `browser_tabs` to list, switch or close; refs from an older snapshot are refused.
- **`browser_read_sections`** reads the whole page (below the fold too) as numbered sections under their headings,
  with links, tables, the source URL and the time; `query` for matching sections, `sections` for exact ones.
  Navigation, headers, footers and repeated blocks are left out and counted; unreadable frames and cut content are
  flagged. **`browser_extract`** returns tables (all rows), links, form fields or page metadata as data;
  **`browser_wait_for`** waits for text or a load state.
- `fetch_url` returns the same sections (no JavaScript) and serves later section requests from its cache.
- Strict offline is also enforced inside the browser: every request to anything but this computer is aborted and
  logged.

### Documents from email

- **`meeting_doc`**: meetings for a week in your time zone from emails the model read: invitations (with their own
  time zones), updates, cancellations, conflicts, links and prep; mentions without an invitation are listed without
  a time. Writes a `.docx`, never over an existing file, reopens it and counts the meetings.
- Built-in skills (`aero/skills_builtin`, can be switched off in Settings → Skills): *meetings-from-email*,
  *roblox-launch*, *code-project-tests*.

### Remote Mode

- **Detects remote viewers** (`remote_sessions.py`): Windows Remote Desktop through the WTS API and Linux remote
  logins through logind are verified; RustDesk's connection window is a *probable* sign, used only if you allow it;
  other remote tools are only seen running. Installed or running is never treated as a viewer.
- **Runs part of the model on the CPU** while a viewer is connected (default: 80 % of the weight bytes on the GPU),
  planned from the GGUF's real tensor sizes and llama.cpp's offload order, then measured from llama.cpp's own buffer
  report. Waits for replies in progress, holds new messages during the restart, restores the exact previous profile
  after the last viewer has been gone 90 s, ignores reconnects inside that time, rolls back a profile that fails to
  load, and stops trying when RAM can't hold the moved weights. Measured on an RTX 5080 with a 27B model: 1,748 MB
  freed, 58.9 → 21.2 tok/s.
- Settings → Model & tuning → Remote Mode (auto / on / off, share, minimum free VRAM, timings, detectors) and a
  dashboard card while it's active.

### Router

- **Capability hints**: plain patterns ("remember that…", "in the browser", "click … button", "open X") add the
  tools a request obviously needs when the CPU router picked none. On 25 requests written after tuning was done:
  22 routed well (1.0: 14). The bigger catalog costs about 0.3 s per decision.

### Other

- llama-server starts with `-lv 4` where supported: newer llama.cpp only logs its buffer sizes at that level, which
  Aero needs for VRAM accounting on AMD/Intel and for Remote Mode.
- `tzdata` is installed on Windows (time zones for invitations).
- Prompt: tool output is data, not instructions; evidence notes; questions; apps; the browser's background mode.
  An unedited 1.0 prompt is recognised on every OS and replaced.
- CI runs the tests on Windows, Ubuntu and macOS for every push (`.github/workflows/ci.yml`).
- `validation/check_upgrade.py`: a real older Aero server writes a data folder, this version opens it and checks
  settings, chats and memory (18/18 from 1.0.0). The release builder leaves ruff's cache out of the archives.
- Fixes found by CI: Windows paths in app records parsed the same on every OS; hardware tests fake the whole
  platform (they failed on macOS runners).
- 100 new tests (218 in total); live Windows desktop checks and real-browser checks are opt-in
  (`AERO_LIVE_UI=1`, `AERO_LIVE_BROWSER=1`).

## Aero 1.0.0 (2026-10-09)

The first public release. Aero is the app that was called Halcyon (and VRAMpire before that), rebuilt as a
Frutiger Aero styled, lightweight bootstrapper for local LLMs on Windows, Linux and macOS.

### Windows: a regular installed app

- Aero is listed under **Settings → Apps → Installed apps** (and Control Panel → Programs and Features) with its
  bubble icon, version, publisher, size and links. Installing registers it; every update refreshes the version.
- **Uninstall** from there removes Aero completely: a dialog shows what goes, with *Delete downloaded models* and
  *Delete chats, memory, settings and saved keys* both ticked (untick one to keep that folder). It asks for
  administrator rights, stops Aero, its llama.cpp servers and its app window (including the Python process the venv
  launcher starts), deletes the Start menu and desktop shortcuts, `C:\Aero`, Aero's temporary files and the Installed
  apps entry. A file Windows still holds open is deleted at the next sign-in.
- A quiet uninstall for scripts and winget (`Uninstall-Aero.ps1 -Quiet`, plus `-All` to delete models and chats).
  `C:\Aero\Uninstall-Aero.bat` runs the same uninstaller.
- The uninstaller refuses any folder that isn't an Aero install, and never touches a drive root, Windows, Program
  Files or your profile folders.

### Linux and macOS

- Aero now installs and runs on **Linux** and **macOS** (Apple Silicon and Intel) as well as Windows. One
  `install.sh` handles every Unix system: it finds the package manager (apt, dnf/yum, zypper, pacman, apk, xbps,
  eopkg, emerge, swupd, or Homebrew on macOS), shows the command before it installs anything missing (Python, the
  Vulkan loader, the OpenMP runtime), and installs Aero for your user account into `~/.local/share/aero` (Linux) or
  `~/Library/Application Support/Aero` (macOS). Immutable systems (Silverblue, SteamOS) work when Python 3.10+ is
  already there.
- llama.cpp per system: CUDA on NVIDIA, Vulkan on AMD and Intel GPUs, Metal on Apple Silicon, CPU otherwise. Where
  llama.cpp ships no prebuilt build (musl distros such as Alpine, ARM Linux boards), the installer compiles it.
- Release files for every system: `Aero-windows.zip`, `Aero-macos.zip` (double-click `Install-Aero.command`),
  `Aero-linux.tar.gz`, `install.sh` (one-line install), `.deb` (Debian, Ubuntu, Mint, Pop!_OS), `.rpm` (Fedora, RHEL,
  Rocky, Alma, openSUSE) and a `PKGBUILD` (Arch). The packages put an `aero` command and an app-menu entry on the
  system; the first start sets Aero up for that user.
- Shell, file opening, the app window, the browser tool and desktop control use each system's own tools. Desktop
  control needs an X11 session on Linux, and the Screen Recording and Accessibility permissions on macOS.

### Mod Aero

- **Mod Aero** (the puzzle-piece button in the sidebar): describe a change to Aero in plain words ("make the send button
  green", "add a word counter under the message box") and your local model makes it in a copy of Aero's code, with
  its own chat. Nothing in the running Aero changes until you press **Apply**.
- Before you can apply it, Aero checks the copy: Python compiles, the server imports, the unit tests pass, and a test
  copy of Aero starts and serves its page. **Show changes** shows the diff.
- Apply merges the change into Aero (a three-way merge, so it fits on top of later updates), then reloads the page
  or restarts Aero. Mods can be turned off, on, changed by chatting more, or deleted.
- Safe by design: the model can only write inside its copy. If a modded Aero fails to start, the mod is undone and
  Aero starts again without it; `aero --safe` starts with every mod off.
- Mods survive updates: after an update Aero applies them again. One that no longer fits the new code is marked
  *needs redo*, and opening its chat lets the model make it again on the new version.

### Updates from GitHub

- When Aero starts, it asks GitHub once whether a newer release exists (never while it runs). A bar offers
  **Update now**, **What's new** and **Skip this version**. Settings → Updates has *Check now* and the switch.
- Update now downloads the release for your system, checks its SHA-256 against the release's checksum list, hands
  over to the release's own installer and restarts Aero. Models, chats, settings, memory, mods and tunings stay. An
  Aero several versions behind updates straight to the newest release.
- Strict offline skips the check.

### Forever-loop journal

- During a forever-loop, after every round your local model writes a journal entry: what worked well (tools,
  commands, settings worth reusing), what didn't work and why, the next step, and where the task stands.
- The next round starts with the journal, so the model keeps what works, stops repeating what failed and picks up
  its own plan. A card in the chat shows each round's entry; the loop bar's **Journal** button shows the whole
  journal and can delete it.
- One journal per task, kept in `data/loops`, so running the same loop again later picks up what it learned.
  Settings → General → *Keep a forever-loop journal* switches it off.

### Shared learning across models

- After every task with tool calls, Aero records which tools worked, failed or were refused, and which model did
  it. When something went wrong or the user gave feedback, that model writes short notes: what worked, what failed
  and why. Forever-loop rounds share their journal entries the same way.
- Before a similar task, whichever model is loaded gets those notes (labelled with the model that learned each one)
  and the tools that kept failing on that kind of task, so models learn from each other's mistakes.
- Preferences the user states go into long-term memory as part of the profile every model reads.
- Settings → Memory shows every note, per-model numbers (tasks, tool calls, failures, refusals, speed) and can
  forget a note or everything. One switch turns it off.

### Agents and subagents

- Each chat's task gets an **agent** named for what it does ("Photo Renamer", "Desktop Organizer", "Bug Fixer"),
  first from the request's wording, then from the model along with the chat title. The name shows next to the chat
  title.
- New `run_subagent` tool: the agent hands a self-contained part of a job to a **subagent**, a fresh copy of the local
  model with its own context and the same tools and approvals, and gets back only its report. Its steps stream
  inside the tool card, then fold into one line. Subagents can't start subagents. Settings → Tools has its
  permission (auto) and a step limit (20). The router suggests it for big jobs with separate parts.
- **Talk to any agent or subagent.** Clicking an agent opens its chat. A subagent's **Chat** button (on its card or in
  the dashboard) opens a chat where it answers as that subagent, from its task, steps and report.

### Dashboard

- The Models card is now **Models · Agents**: one scrolling card with three foldable sections, Models, Agents and
  Subagents (grouped under their agent). Working ones come first. Hover any row for what it is doing, its task and
  its result; click to open its chat.

### Control header and Stop

- While a model clicks, types, presses keys, scrolls, opens or switches apps, or drives the browser, a header across
  the top of Aero says "*model* is controlling *app*" with a **Stop** button. The same banner appears at the top of
  the app being controlled (or the screen) on Windows: it never takes focus, is left out of screenshots, and moves
  down when the model needs the spot. Stop on either one stops every running task.

### Install and upgrade on Windows

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

### ChatGPT review pass

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

- **Local Only** button next to ChatGPT, Claude and Loop. **On**: the model answers by itself with no web search,
  web pages, automated browser or MCP servers, and the ChatGPT and Claude reviews don't run (their buttons grey
  out). The model is told it has no internet and says so when a question needs it. **Off** (the default): the model
  searches and reads the web when a task needs it. Also in Settings → Privacy & offline; strict offline turns it on.
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
- Calls to the local model and the UI on 127.0.0.1 never go through a system-wide HTTP proxy.
- `validation\Validate-Aero.ps1`: checks a real install without changing it (see `docs/VALIDATION_REPORT.md`).
- Documentation: `docs/INFERENCE_RESEARCH.md`, `docs/LOCAL_EXECUTION_AUDIT.md`, `docs/HAPO_ARCHITECTURE.md`,
  `docs/PERFORMANCE_REPORT.md`, `docs/AERO_DESIGN_SYSTEM.md`, `docs/VALIDATION_REPORT.md`.

### Compatibility

- Settings, chats, memory, tunings and models from Halcyon 2.x and VRAMpire 1.x carry over. Tunings stay valid
  because they are keyed by model file, GPU and llama.cpp build, not by the folder.
- The Python package is now `aero` (`python -m aero`); old shortcuts that ran `-m halcyon` or `-m vrampire` are
  replaced by the updater.
- Ports are unchanged: 8180 app, 8181 model, 8182 tuning trials, 8183 advisor, 8184 router, 8185 cursor overlay.
