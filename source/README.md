# Aero

*A lightweight bootstrapper for local AI on Windows, Linux and macOS. Your hardware does the work, a tiny CPU model
makes the decisions, and ChatGPT or Claude check it only when you ask.*

Aero scans your computer, recommends a model and quant that fit it, installs the right llama.cpp build for your GPU
(NVIDIA, AMD, Intel, Apple Silicon or none), tunes the model to the exact VRAM you allow, and gives you an agent that can use your
files, shell, screen, apps and browser. A small decision router on the CPU reads every request first and hands the
main model only the tools it needs. Everything local runs on 127.0.0.1; with **strict offline** on, nothing leaves
the machine at all.

When you want a second opinion, **ChatGPT** (GPT-6 Astra reviews, GPT-6.1 Sol repairs) and/or **Claude** (Claude
Fable 5.1 reviews, Claude Opus 5.5 repairs) check the finished work, and your local model learns from what they
found. Every model's thinking and tool calls stream into the chat on its own coloured lane.

Engine: **llama.cpp** (`llama-server`). Models: **GGUF**. Look: **Frutiger Aero**, day and night.

---

## Requirements

- **Windows** 10 or 11, 64-bit. Administrator rights for the install (Aero also runs as administrator so its desktop
  and app-control tools can reach every window).
- **Linux**, any distro with Python 3.10 or newer available (Debian 12+, Ubuntu 22.04+, Fedora, RHEL / Rocky / Alma
  9+, openSUSE Leap 15.6 and Tumbleweed, Arch, Alpine, Void and others). Installs for your user; sudo only for
  missing system packages.
- **macOS** on Apple Silicon (Metal) or Intel (CPU). Homebrew is used for Python if no Python 3.10+ is installed.
- 16 GB RAM or more is comfortable; 8 GB works with small models.
- Any of: an NVIDIA GPU (CUDA build), an AMD or Intel GPU (Vulkan build), Apple Silicon (Metal), or no GPU at all
  (CPU build; small and MoE models are the quick ones there).
- Disk: a few GB for the app and llama.cpp, plus your models (2 to 30 GB each).
- Internet for the install and model downloads. After that Aero runs fully offline if you want.
- Optional: an OpenAI API key or a ChatGPT plan (Plus, Pro, Business) for ChatGPT reviews; an Anthropic API key or a
  Claude plan (Pro, Max, Team) for Claude reviews.

## Install or update

Every release on [GitHub](https://github.com/NermalYT/Aero/releases/latest) has a file for each system. Models,
chats, memory, settings, mods and tunings are never deleted by an install or update.

### Windows

`Aero-windows.zip` holds **`Update-Aero.bat`** and the **`source`** folder (the app, this README, the installer and
the uninstaller in `source\installer`).

1. Extract the whole zip anywhere.
2. Double-click **`Update-Aero.bat`** and accept the admin prompt.
3. It works out what to do:

| Your PC | What happens |
| --- | --- |
| Nothing installed | Full install into `C:\Aero`: Python 3.12 if needed, a private Python environment, the llama.cpp build for your GPU, the bubble icon and shortcuts |
| Halcyon in `C:\Halcyon`, or VRAMpire in `C:\VRAMpire` | Moves the whole folder to `C:\Aero` (models, chats, memory, settings, tunings and your "About you" text come along), rebuilds the Python environment, fixes the saved paths, swaps the shortcuts and icon |
| Aero installed | Updates the app code, Python packages and llama.cpp |

4. Then the **model chooser** runs in the same window (below), and Aero opens.

Run `C:\Aero\Update-Aero.bat` later for packages, llama.cpp and the chooser.

**Uninstall** like any other program: **Settings → Apps → Installed apps → Aero → ⋯ → Uninstall**, or Control Panel →
Programs and Features. The installer registers Aero there with its icon, version, publisher and size (every update
refreshes the version). Uninstalling:

1. asks for administrator rights (Aero was installed for every user of the PC);
2. shows what it removes, with two boxes, both ticked: **Delete downloaded models** and **Delete chats, memory,
   settings and saved keys**. Untick one to keep that folder; installing Aero again picks it up;
3. stops Aero, its llama.cpp servers and its app window;
4. deletes the Start menu and desktop shortcuts, `C:\Aero` (or all of it except the folders you kept), Aero's
   temporary files and its Installed apps entry. A file Windows still has open is deleted at your next sign-in.

`C:\Aero\Uninstall-Aero.bat` does the same. For scripts and winget, the entry's quiet uninstall
(`Uninstall-Aero.ps1 -Quiet`) keeps models and chats; add `-All` to delete them too. Python, the Codex CLI and
Claude Code are separate programs and stay installed.

### macOS

Unzip `Aero-macos.zip`, then right-click **`Install-Aero.command`** → **Open** (the first time, macOS asks because
the file comes from the internet). A Terminal window runs the installer: Python 3.12 from Homebrew if you have no
Python 3.10+, Aero into `~/Library/Application Support/Aero`, llama.cpp's Metal build on Apple Silicon (CPU build on
Intel), **Aero.app** in `~/Applications` and the `aero` command in `~/.local/bin`. Then the model chooser runs and
Aero opens. Desktop control asks for the Screen Recording and Accessibility permissions the first time it is used.

### Linux

One line in a terminal downloads the newest release and installs it:

```sh
curl -fsSL https://github.com/NermalYT/Aero/releases/latest/download/install.sh | sh
```

Or download `Aero-linux.tar.gz`, unpack it and run `sh Aero/install.sh`. Either way, the installer:

1. finds your package manager (apt, dnf / yum, zypper, pacman, apk, xbps, eopkg, emerge or swupd), shows the
   command for anything missing (Python 3.10+ with venv, curl, the Vulkan loader for AMD / Intel GPUs, the OpenMP
   runtime) and asks before running it with sudo;
2. installs Aero for your user into `~/.local/share/aero` (`--dir` picks another folder) with a private Python
   environment;
3. downloads llama.cpp's CUDA, Vulkan or CPU build for your GPU, or compiles llama.cpp where no prebuilt build runs
   (Alpine and other musl systems, ARM boards);
4. adds an app-menu entry and the `aero` command (`~/.local/bin/aero`), runs the model chooser and starts Aero.

`sh install.sh --help` lists the options (`--yes` for no questions, `--skip-models`, `--build-llama`, `--no-launch`
and more).

**Packages.** The release also has a `.deb` (Debian 12+, Ubuntu 22.04+, Mint, Pop!_OS: `sudo apt install
./aero_*_all.deb`), an `.rpm` (Fedora, RHEL / Rocky / Alma 9+, openSUSE: `sudo dnf install ./aero-*.rpm` or `sudo
zypper install ./aero-*.rpm`) and a `PKGBUILD` (Arch and its family: `makepkg -si`). They install the `aero` command
and an app-menu entry for everyone; the first time each user starts Aero, it sets itself up in that user's
`~/.local/share/aero` (in a terminal window, where the installer can ask questions).

Run `sh ~/.local/share/aero/install.sh` later for packages, llama.cpp and the chooser. Uninstall: `sh
~/.local/share/aero/uninstall.sh` (asks whether to keep models and chats).

### Updates from GitHub

When Aero starts, it asks GitHub once whether a newer release exists; it never checks while it runs, and strict
offline skips the check. When there is one, a bar at the bottom offers **What's new**, **Update now** and **Skip this
version**; Settings → Updates has the same plus **Check now** and the switch for the startup check.

**Update now** downloads the release file for your system, checks its SHA-256 against the release's
`SHA256SUMS.txt`, hands over to the installer inside it (`Update-Aero.bat` on Windows, `install.sh` on Linux and
macOS) and closes Aero. The installer replaces the app, keeps everything else, applies your mods again and starts
Aero; the window reconnects by itself. An Aero several versions behind updates straight to the newest release.

A copy run from a git checkout updates with `git pull`.

### The model chooser

First it shows what it found, for example:

```
  This PC
  Radeon RX 9070 XT 16 GB · 32 GB RAM · AMD Ryzen 7 9800X3D 8-Core Processor (8 cores)
```

**1 / 2: Decision router (CPU).** A list of small, recent, fully local models with their published scores
(instruction following, tool calling, reasoning), size and licence. The one marked **★ recommended for this PC**
fits your RAM and cores. Pick a number or press Enter. It downloads into `models\_router`, then `llama-bench` runs it
at several thread counts on your CPU and keeps the fastest.

**2 / 2: Main model.** Every model is fitted to your PC: the quant and size that fit your VRAM, and where it will
run (*fully on the GPU*, *GPU + experts in RAM* for mixture-of-experts models, *on the CPU*, or *too big for this
PC*), with its **Artificial Analysis Intelligence Index** score. Press Enter for the recommended one, pick a number,
or skip and keep the models you have. The first time you load a model, Aero tunes it (below) and shows the real
measured speed.

Run the chooser again any time with `C:\Aero\Update-Aero.bat` (Linux and macOS: `sh <install folder>/install.sh`),
or switch routers in Settings → Router.

## How a request flows

```
 you ─► router (CPU) ─► your model ─► [ChatGPT pass] ─► [Claude pass] ─► answer
                                         │                  │
                         GPT-6 Astra reviews        Claude Fable 5.1 reviews
                         ├ ok ─► next pass          ├ ok ─► answer
                         ├ minor ─► your model fixes it, Astra/Fable re-check
                         └ major ─► GPT-6.1 Sol     └ major ─► Claude Opus 5.5
                                    repairs it                 repairs it
                                         └──────── lessons saved for your model ────────┘
```

- **Router** (aqua lane): picks the tools, whether to think, whether vision is needed, a short plan for complex
  work, and whether a cloud review is worth it. Its answer is forced through a JSON grammar, so it is always valid.
  Sending 6 tool schemas instead of 46 saves thousands of tokens per step. The main model can still call
  `load_tools` if it needs one the router left out.
- **Your model** (blue lane): does the work with the tools it was given.
- **ChatGPT** and **Claude** buttons under the message box: each cycles *off → on → auto* (auto = the router decides
  per request). With both on, ChatGPT goes first and Claude second, and Claude sees Astra's verdicts and everything
  Sol changed, so it reviews the final state.
- **Reviewers** (Astra green, Fable violet) open files, look at the screen and search the web to check claims with
  read-only tools, then return *ok*, *minor* (a list of fixes, sent back to your model, up to 2 rounds) or *major*.
- **Repairers** (Sol orange, Opus amber): on *major*, or *minor* problems your model couldn't fix, they get the
  reviewer's full re-plan and finish the task with the same tools and the same approval rules.
- **Lessons**: what went wrong in either pass becomes short lessons in long-term memory, which your model sees in
  later chats, and a line in `data\training\corrections.jsonl` (with the repairers' answers, ready for a fine-tune).

Hover any tool name in the chat for a one-line description of what it does.

### Forever-loop

The **∞ Loop** button repeats your prompt until you stop it: each round continues from the last one's results. A
bar above the message box shows the round, a countdown and **Run now / Finish this round / Stop**. Sending a
message yourself also stops it. Delay and maximum rounds are in Settings → General. Reviews run only on the first
round unless you turn on *Let ChatGPT and Claude review every forever-loop round* (it costs cloud usage on every
round).

**The loop journal.** After every round, the local model looks back at what it just did (its tool calls, which ones
failed, its answer, any review) and writes a journal entry: what worked well and is worth reusing (a tool, a
command, a path, a setting), what didn't work and why, the next step, and where the task stands. The next round
starts with the journal in its context, so the model keeps what works, stops repeating what failed and starts from
its own plan. An item that failed once and worked later is listed only where it ended up.

A card in the chat shows each round's entry, and the loop bar's **Journal** button opens the whole journal, round by
round, with **Delete** to start fresh. There is one journal per task (the loop's prompt), saved in `data/loops`, so
running the same loop again later, even in a new chat, picks up what it learned. Settings → General → *Forever-loop
journal* turns it off.

## Connecting ChatGPT

Settings → ChatGPT → Connection:

- **ChatGPT plan (Plus / Pro / Business)**: Aero drives the official, unmodified **Codex CLI**. Install it once with
  `npm install -g @openai/codex` (needs Node.js), then press **Sign in**: it runs `codex login`, which opens
  chatgpt.com in your browser; you sign in on OpenAI's own page (**Sign in with a code** uses the device-code flow).
  Aero has no login form of its own, never reads Codex's login file, and only runs `codex login status` to show
  whether you're signed in. Usage counts against your plan's limits. Sol's repairs through the plan run in your work
  folder with Codex's workspace-write sandbox, which you approve once per repair (or read-only if file writing or
  commands are off in Settings → Tools).
- **API key**: an OpenAI key (pay as you go), stored encrypted with Windows DPAPI. Reviews run through the Responses
  API with Aero's own tools under your approval rules. A daily budget caps spending.
- **Auto** uses the API key when one is saved, otherwise the plan sign-in.

Also here: which models review and repair, their reasoning effort, and whether Astra may use read-only tools.

## Connecting Claude

Settings → Claude → Connection:

- **Claude plan (Pro / Max / Team)**: Aero drives the official, unmodified Claude Code program (bundled with
  Anthropic's `claude-agent-sdk`). **Sign in** runs `claude auth login`, which opens claude.ai in your browser; you
  sign in on Anthropic's own page. Aero never reads or stores the login token: it only runs `claude auth status`.
  Usage counts against your plan's limits; the dashboard shows an *API-equivalent* estimate.
- **API key**: an Anthropic Console key (pay as you go), stored encrypted with Windows DPAPI.
- **Auto** uses the API key when one is saved, otherwise the plan sign-in.

## Offline and privacy

- Local models always run in `llama-server` processes Aero starts itself, bound to 127.0.0.1, with `--offline`.
- **Local Only** (the button under the message box, or Settings → Privacy & offline): **On** keeps the chat on your
  PC. The model gets no web search, web pages, browser or MCP servers, the ChatGPT and Claude reviews don't run, and
  the model knows it has no internet. **Off** lets the model search and read the web when a task needs it. Aero
  itself can still check for updates and download models; strict offline (below) stops that too.
- **Strict offline** (Settings → Privacy & offline) blocks every outbound request inside Aero, hides the web, browser
  and network MCP tools, and skips both cloud review passes before anything is sent. Turn it on and pull the network
  cable: chat, tools on your PC, memory, tuning and benchmarks all keep working.
- Every outbound request (allowed or blocked) is logged in `data\audit\network.jsonl` (host only, never the path).
  The same page lists every listening socket and confirms they are all loopback.
- Details and what strict offline can't cover (the shell tool, programs you start): `docs\LOCAL_EXECUTION_AUDIT.md`.

## GitHub, plugins, MCP and skills

- **GitHub** (Settings → GitHub): link with the GitHub CLI login (`gh auth login`) or a personal access token. The
  token goes into the encrypted vault and GitHub's official MCP server is added, so every model gets repo, issue,
  pull request and Actions tools. Choose the toolsets, or make it read-only.
- **Plugins & MCP**: *Import* finds the MCP servers you already set up in Claude Desktop, Claude Code (and its
  plugins) and Codex, and adds them in one click. Or paste any server (command or URL) in the same JSON format as
  Claude Desktop. Remote servers that need a sign-in show a **Sign in** button. Tools appear as `mcp_<server>_<tool>`.
- **Skills**: Aero reads the same `SKILL.md` folders as Claude Code and Codex: `data\skills`, `~\.claude\skills`,
  skills inside installed Claude Code plugins, `~\.codex\skills`, and `.claude\skills` in your working folder. The
  router sees their names and descriptions; the model loads one with `use_skill` when it needs it.

## Using it

1. Open **Aero** from the Desktop or Start Menu (UAC prompt, then the app window opens).
2. **Choose a model**: *Your models*, *Find a model on Hugging Face* (type to search GGUF repos; click one to see
   every quant with its size and whether it fits this PC), or *Open .gguf file…*.
3. **First load: tuning.** Already-tuned models skip straight to loading.
   - **VRAM limit**: the most VRAM the model may use. Aero suggests one from your card's size and what other apps
     use. Every trial is measured against that number: a configuration that uses more fails even if the card had
     room. On a CPU-only PC the limit is system RAM.
   - **Depth**: Short (10 trials), **Medium** (25, default), Long (50), Full (until nothing improves).
   - **Goal**: Balanced (largest context that keeps ≥80% of top speed), Max context, or Max speed.
   - A small advisor model picks each trial one step at a time (context, GPU layers or MoE experts, KV precision,
     batch, threads); real `llama-server` runs measure every one. The best configs are re-run and averaged, and the
     winner gets a long-prompt check. The result is saved per model, GPU and llama.cpp build.
   - **Speculative decoding** is on by default (Settings → Model & tuning). Models that ship their own
     multi-token-prediction layer (MTP) draft several tokens per step with it, and n-gram lookup drafts text the chat
     already contains (code you are editing, tool output). Measured on an RTX 5080 with Qwen3.8-27B: 59 → 168 tok/s
     editing code and 59 → 69 tok/s on free text. The tuning trials include its memory, so your VRAM limit holds.
4. Chat. **Think** cycles *auto* (router decides) → *on* → *off*. Paste big text and it becomes a chip; attach
   images, PDFs, Office files, code or zips with the paperclip or drag-and-drop. Esc stops a reply.

### Agents and subagents

- **Agents.** Each chat's task is worked by an agent with a job title for a name, from the request at once ("Tidy my
  Desktop" → Desktop Organizer) and then from the model when it titles the chat. The name shows next to the chat
  title. Clicking an agent in the dashboard opens its chat, where you talk to it with everything it did in context.
- **Subagents.** For a big job with separate parts, the agent can call `run_subagent` with a name and a full brief.
  A fresh copy of the local model (empty context, same tools, same approvals) does that part and reports back; the
  agent sees only the report, which keeps its own context small. The subagent's steps stream inside the
  `run_subagent` card, then fold into one line with its report. Subagents can't start subagents. Settings → Tools
  has the permission (*auto* by default) and the step limit (20).
- **Talk to a subagent.** **Chat** on its card, or its row in the dashboard, opens a chat with it. It answers as
  that subagent, from its task, its steps and its report, and can keep working with its tools.

### "Is controlling" header and Stop

When a model sends input (clicks, typing, keys, scrolling, opening or switching apps, browser actions), Aero shows
"*model* is controlling *app*" in a header across the top of its window with a **Stop** button, and the same banner
at the top of the app being controlled (or of the screen). Looking (screenshots, reading an app or page) doesn't
count. The banner over the other app never takes focus, is left out of screenshots so the model never sees or
clicks it, and moves to the bottom of the window when the model needs the spot it covers. Stop on either one stops
every running task and denies any pending approval. Both go away when the turn ends.

### Performance Lab and HAPO

**Lab** in the top bar. **Profiles** lists what the tuner's measured trials offer: Maximum Speed, Balanced, Maximum
Context, Maximum Quality, Agent Optimized and Efficiency, each with its real numbers and the rule that picked it.
Apply one, or restore the previous one; Aero tells you when a profile was made on different hardware or a different
llama.cpp. **Benchmarks** runs suites against the loaded model (quick, deep, decode only): time to first token,
prompt and generation speed, long-context recall, JSON and tool-call accuracy, prompt-cache reuse and draft
acceptance, plus a router suite and a **Scenery cost** test that measures generation speed with the scenery on and
off. Export any report as Markdown or JSON. Only measured numbers are shown; anything not measured says so.

### Your profile: every model knows you

- **About you** (Settings → Memory) is a text box for who you are and how you like answers: tone, length, format,
  depth, things to avoid. It starts empty.
- Every model gets it in its system prompt, together with every **learned preference**, the facts about you, and a
  short timeline of your recent chats: whichever GGUF you load, and the ChatGPT and Claude models too.
- **Learning**: say something like "from now on no tables" or "too long" and the router saves it as a preference.
  Compacting or leaving a chat also pulls preferences out of how you corrected the model. Edit or delete any of them
  in the list below the profile.
- **Preview what every model sees** shows the exact block.

### Memory and compaction

- **Compact** (top bar) summarizes the chat into long-term memory and continues in a fresh `· part N` chat. It
  happens on its own at 75% context (Settings → Memory).
- The newest message also gets a recall block: pinned facts, lessons and the memories most relevant to it, plus
  related past chats. The model can `remember` and `recall`. **Memory** in the sidebar lists everything; passwords,
  keys and tokens are filtered out.

### Shared learning: models learn from each other

Every model you load works from the same experience, whichever model gathered it.

- **Run records.** After each task with tool calls, Aero writes down every tool call (worked, failed with its error,
  or refused by you), the model that made it, the agent's name (its kind of work) and the model's speed. No model
  call is spent on this.
- **Notes.** When a tool failed, you refused one, or your message reads like feedback ("don't…", "always…", "I
  prefer…"), the loaded model looks back at the run and writes up to three notes on what worked and three on what
  failed and why. Forever-loop rounds share their journal entries the same way. A note that comes up again is
  counted, not repeated; one that failed before and works now moves to the worked side.
- **Before a similar task**, whichever model is loaded gets the matching notes, each labelled with the model that
  learned it, plus the tools that kept failing on that kind of task and why (for example "open_app failed 3 of 4
  times (last error: No program called 'notepad')").
- **Your preferences.** A lasting preference you state goes into long-term memory as part of your profile, which every
  model's system prompt carries.
- Settings → Memory → *Shared learning* lists every note with its model and agent, a per-model table (tasks, tool
  calls, failures, refusals, speed), and buttons to forget one note or all of it. The switch there turns it off.
  Stored in `data/experience.json`.

### Look

Frutiger Aero, day and night: a blue sky with the sun (or moon and stars), drifting clouds, light ribbons, iridescent
soap bubbles rising off grassy hills with flowers (fireflies at night), and a pond where three frogs sit, peek and
rest, and every so often one of them meeps. Everything sits behind frosted glass panels that keep text readable.

**Settings → Appearance**: *Scenery* Full, Still (same scene, no motion) or Off (plain sky); the frog pond on or off;
freeze the scenery while a model is generating, tuning or benchmarking (on by default, so the scene never slows
generation); *Enable transparency* (turn it off on slower GPUs: same look, no glass blur). Motion follows Windows' reduced-motion setting, and the scene stops while the
window is hidden. Theme follows Windows unless you pick Day or Night.

### Mod Aero

**Mod Aero** in the sidebar changes Aero itself. Describe the change in plain words ("make the send button green",
"add a word counter under the message box", "a button that copies the whole chat") and the **Aero Modder** agent, your
local model, makes it in a copy of Aero's code, in its own chat.

- **It can only touch the copy.** File tools are limited to the mod's folder; nothing in the running Aero changes
  until you press **Apply**.
- **Checked before you apply.** When the model finishes, Aero checks the copy: the Python files compile, the UI
  scripts parse (when Node.js is installed), Aero's server imports, the unit tests pass, and a test copy of Aero starts on a spare port and serves
  its page. The mod card lists each check. **Show changes** shows the diff.
- **Apply** merges the change into Aero (three-way, so it fits on top of updates), then reloads the page (UI-only
  changes) or restarts Aero (Python changes). **Turn off** / **Turn on** undo and redo it; **Delete** removes it.
  Chatting more in a mod's chat changes the mod (an applied mod gets a follow-up).
- **A broken mod can't lock you out.** If Aero fails to start after a mod, the mod is undone and Aero starts again
  without it. `aero --safe` (Windows: `C:\Aero\venv\Scripts\python.exe -m aero --safe` from `C:\Aero\app`) starts with
  every mod off.
- **Mods survive updates.** After an update Aero applies your mods again. A mod that no longer fits the new code is
  marked *needs redo*; open its chat and the model makes it again on the new version from the old diff.
- Mods live in `data/mods`. In a mod chat the cloud reviews run only when their composer button is on; the mod checks
  are the default safety net.

## The dashboard

The glass panel on the right (the dashboard button in the top bar shows or hides it) updates live. The first card,
**Models · Agents**, scrolls through three sections you can fold: **Models** (router, local model, ChatGPT and Claude
lanes, and which one is working), **Agents** (each chat's agent, its status and what it is doing; hover for the
task, result and subagents, click to open its chat) and **Subagents** (grouped under the agent that started them;
hover for the task and report, click to talk to one). The other cards show the forever-loop, GPU (VRAM used by
Aero vs other apps, load, temperature, power, clocks; NVIDIA only), CPU and RAM, local speed, this session's tokens
and tool calls, review verdicts, ChatGPT and Claude usage and spending today, and memory, tool and MCP counts.

## Built-in tools

| Category | Tools | Default |
| --- | --- | --- |
| Read files | `list_dir`, `read_file`, `find_files`, `search_files` | auto |
| Write files | `write_file`, `edit_file`, `move_path`, `delete_path` | **ask** |
| Shell | `run_command` (PowerShell or cmd) | **ask** |
| Screen and app view | `screenshot`, `list_windows`, `wait`, `app_list`, `app_view`, `app_read` | auto |
| Desktop and app control | `mouse_click`, `mouse_move`, `mouse_drag`, `scroll`, `type_text`, `press_keys`, `focus_window`, `open_app`, `app_click`, `app_type`, `app_keys`, `app_scroll` | **ask** |
| Browser | `browser_open`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_select`, `browser_press`, `browser_scroll`, `browser_back`, `browser_screenshot`, `browser_read` | auto |
| Web | `web_search`, `fetch_url` | auto |
| Memory | `remember`, `recall`, `forget` | auto |
| Skills | `use_skill` | auto |
| Subagents | `run_subagent` | auto |
| MCP and GitHub | everything from connected servers | **ask** |

- *ask* shows Allow / Always in this chat / Deny on the tool card, for your model and for the cloud models alike.
  Change any category in Settings → Tools. Aero runs as administrator, so approved shell and file actions have admin
  rights; leave them on *ask* unless you trust the model.
- **App control**: pick a window with the **App** button (or name the app). The model sees only that window, even
  when covered, plus a numbered list of its controls; clicks and typing go to it in the background, so your mouse
  and keyboard stay yours. A blue **Aero** cursor shows where it is acting, and the "is controlling" header and
  banner (above) carry a Stop button. Slam the real mouse into a screen corner to abort desktop control.
- The browser is a visible Edge window with its own profile (`C:\Aero\data\browser-profile`).

## Folders

```
C:\Aero\
  app\        Aero code (replaced on update)
  venv\       private Python
  llama\      llama.cpp binaries (llama-server.exe, llama-bench.exe, GPU runtime DLLs, VERSION.txt)
  models\     downloaded GGUFs; models\_router holds the decision router
  data\       settings.json, models.json, router.json, tune_cache.json, hapo.json, mcp.json, memory.json,
              cloud_usage.json, secrets.json (encrypted), chats\, skills\, training\, bench\, audit\, uploads\, logs\
  aero-bubble.ico   Update-Aero.bat   Uninstall-Aero.bat   Uninstall-Aero.ps1   README.md
```

Linux (`~/.local/share/aero`) and macOS (`~/Library/Application Support/Aero`) have the same `app`, `venv`, `llama`,
`models` and `data` folders, plus `bin/aero` (the launcher), `install.sh` and `uninstall.sh`. In `data`, `mods\` holds
your mods, `loops\` the forever-loop journals and `updates\` downloaded releases (removed after an hour).

## Checking an install

`C:\Aero\app\validation\Validate-Aero.ps1` checks a real install without changing it: hardware scan, icon and
shortcuts, loopback-only sockets, strict offline, a model load, a local chat, HAPO and a benchmark, all in a
throwaway copy under `%USERPROFILE%\AeroTest`. See `docs\VALIDATION_REPORT.md`.

```
powershell -ExecutionPolicy Bypass -File C:\Aero\app\validation\Validate-Aero.ps1
```

## Troubleshooting

- **Icon does nothing:** run `C:\Aero\app\Aero-debug.bat` (Linux and macOS: `aero` in a terminal) to see the
  error. Logs: `C:\Aero\data\logs\` (Linux and macOS: `data/logs` in the install folder).
- **Aero won't start after applying a mod:** it undoes the mod by itself and starts again; if it still doesn't, start
  it with `--safe` (see Mod Aero) and turn the mod off.
- **Update now says the update didn't start:** the message says why (no file for your system in the release, a
  checksum mismatch, no network). Download the release yourself and run its installer; nothing was changed.
- **Linux: `aero: command not found`:** `~/.local/bin` isn't on your PATH yet; log out and in, or run
  `~/.local/share/aero/bin/aero`.
- **Linux: the desktop tools are missing:** clicking, typing and screenshots need an X11 session (or XWayland with
  `DISPLAY` set). On a pure Wayland session Aero hides them; files, shell, apps and the browser still work.
- **macOS: "cannot be opened because it is from an unidentified developer":** right-click `Install-Aero.command` →
  **Open**, or allow it in System Settings → Privacy & Security.
- **The move from Halcyon or VRAMpire says Windows wouldn't move the folder:** close any Explorer window, terminal or
  editor open inside the old folder and run the updater again. Nothing is changed when the move fails.
- **Router shows "off" or "error":** Settings → Router → pick a model (it downloads and benchmarks), or Re-benchmark /
  Restart. Aero still works without it; your model then gets every tool.
- **ChatGPT button says ChatGPT isn't connected:** Settings → ChatGPT → install the Codex CLI and Sign in (plan), or
  add an API key. **Claude button says Claude isn't connected:** Settings → Claude → Sign in or add an API key.
- **Every tuning trial fails "over your limit":** the model is bigger than the limit you typed. Re-tune with a higher
  limit or pick a smaller quant.
- **"CUDA build did not list your GPU" or "Vulkan build did not list your GPU" during install:** update the graphics
  driver and run the updater again. Until then models run on the CPU.
- **AMD or Intel GPU and the dashboard shows no GPU load or temperature:** those live readings come from
  `nvidia-smi`, so they exist on NVIDIA cards only. On AMD and Intel cards Aero measures its own model's VRAM from
  llama.cpp's memory report instead, and estimates what the desktop uses.
- **Gated models (Llama, some Gemma):** accept the licence on huggingface.co and paste a token in
  Settings → Hugging Face.
- Ports: 8180 app, 8181 model, 8182 tuning trials, 8183 advisor, 8184 router, 8185 cursor overlay; all bound to
  127.0.0.1.

## Licence

MIT. See `LICENSE`. The bundled highlight.js (BSD-3-Clause), marked (MIT) and DOMPurify (Apache-2.0 or MPL-2.0)
in `aero/static/vendor` keep their own licences, noted at the top of each file. llama.cpp (MIT) is downloaded by the
installer, not bundled. Models you download have their own licences, shown in the chooser.
