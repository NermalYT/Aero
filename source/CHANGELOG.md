# Changelog

## Aero 1.2.5 (2026-10-12)

Aero has its own browser and never uses yours.

- **One browser: Aero's own.** 1.2.3's "your own browser by sight" mode is gone, and so is 1.2.1's connection over the
  remote-debugging switch. In a live test on the owner's PC, the screen mode's window of the owner's own Chrome led to
  the whole Chrome closing at the end of the request, taking an open page with it. Aero now always browses in its own
  profile of your default browser (`data/browser-profile`), on screen unless a request says "in the background".
- **Sign in once, as yourself.** Settings → Tools → **Sign in to your accounts** opens Aero's profile as a normal
  browser window, without automation, because Google refuses sign-ins in automated browsers. Sign in to your Chrome
  profile (profile icon → Turn on sync) or to single sites, then press **Done**. Aero closes the window the way you
  would, so the browser saves the sign-ins, and uses them from then on. When a page asks for a sign-in mid-task, the
  same window opens on that page; Aero waits, then continues signed in on it. While the window is open, Aero's
  browser waits instead of fighting over the profile.
- **The banner fits.** It shows the model's short name ("Qwen3.8-27B is using Aero's browser"); 1.2.3 showed 90
  characters of a long file name and cut off the app. Live check on the owner's PC: the outline sat on Aero's browser
  window, and the real Stop button stopped the task at once and closed the browser, outline and banner.
- **No more random banners.** Only the Aero app shows the cursor, banner and outline. Tests and scripts that ran
  Aero's tool code used to pop them up and take over the app's overlay.
- `AERO_BROWSER_ARGS` adds switches for Aero's browser (e.g. `--window-position=...` to keep test windows on one
  monitor).

## Aero 1.2.3 (2026-10-12)

Fixes from a day of hands-on testing of 1.2.1 on a real PC, and Aero now works in your own signed-in browser without
any setup. (There was no 1.2.2 release.) Test report: `docs/V1.2.1_HANDS_ON_TESTS.md`.

- **Your own browser, no switch needed.** When the browser's remote-debugging switch is off, Aero opens a new window
  of your default browser (your everyday profile, so you're signed in; with several profiles, the one you used last)
  and works in it by sight: a picture of the window with numbered controls, read through UI Automation
  (`tools/browser_screen.py`, Windows). Links and buttons are pressed and fields are set and read back without your
  mouse or keyboard. Typing goes only into fields of the page, never the browser's address bar, menus or a password
  field. Aero sees only the window it opened, and closes it when the request is done unless you added tabs to it.
  Tested on Chrome and Edge: open, search and Enter, links, Back, scrolling, whole-page reading and sign-in detection.
  With the switch on, Aero still connects through it (faster, more precise). Background requests still use Aero's own
  hidden profile.
- **You can see what Aero is using.** The window of the app or browser a model is using gets a blue outline and a
  banner on top, "<model> is using <app>" (plus "with your mouse and keyboard" when it has the real input), with a
  Stop button that stops every running task. Outline and banner follow the window as it moves, onto any monitor,
  and step away while it's minimized (tested on a three-monitor PC). Aero's cursor no longer gets stuck on a
  monitor left of or above the main one: hidden pieces used to be parked at -500,-500, which is on such a monitor.
  The overlay now also closes when the Aero that started it exits, and a new Aero replaces one left running by an
  older version.
- **Images no longer crawl next to a big model.** When an image needs more video memory than is free, the main model
  is unloaded for the job and loaded again with the same settings; chat messages wait meanwhile (`gpu_lend.py`).
  On the test PC with Qwen3.8-27B loaded: 8 minutes before, 16.7 s now.
- **The tuning advisor behaves.** Candidates that failed to load are remembered per llama.cpp build and skipped before
  downloading, and their files are deleted (17 GB of unusable Bonsai files on the test PC). No advisor over 4B
  parameters or 3 GB runs on the CPU, one slower than 20 s per decision is dropped, and Cancel stops it at once.
  The `--reasoning-format none` flag that broke its grammar is gone.
- **No false "it's open".** An app Aero knows but that isn't installed (Calculator removed from Windows) says so;
  1.2.1 started a windowless stub and said Calculator was open. After starting a program with a command, the model
  checks for its window. Shared lessons only count effects that were verified.
- **Hindsight starts on every PC.** Programs Aero starts get UTF-8 output (Hindsight crashed printing its banner in
  cp1252), and its start error leads with the real exception.
- **Smaller fixes.** Skills say where their files are. A sign-in that was skipped isn't asked again in the same
  request, and the card names the site you asked for. One "tools are off" notice per request. The workspace panel
  no longer redraws the wrong workspace. After an update the window loads the new interface instead of a cached one.

## Aero 1.2.1 (2026-10-12)

A quality-of-life release for 1.2: requests that need doing get done (on screen, in your own signed-in browser),
tool calling is tuned for each model, any model on Hugging Face loads at the size that fits, and every integration
comes installed. Details: `docs/V1.2_TOOL_CALLING.md`.

- **Requests that need an action get one.** A request like "look at my GitHub notifications" or "check my emails and
  look for any meetings this week" is recognised without a model call (`intents.py`): Aero shows the plan above the
  work, offers the right tools, and when the model answers "I can't" (measured: Qwen3-4B refused about half the time
  with every tool offered), the first action is made with a grammar-forced call instead (`forced_call.py`; llama-server
  doesn't enforce `tool_choice: "required"`, measured on b11501). Email means every account: the ones listed in
  Settings, mail sites in the browser bookmarks (read only) and Gmail / Outlook, with `/u/1/`, `/u/2/`… for extra
  Gmail accounts; meetings become a Word schedule plus a table in the chat.
- **Work happens on screen.** Aero's browser and the apps it uses are visible unless the request says "in the
  background". When a page asks for a sign-in, Aero shows it and waits with a card ("I've signed in" / "Skip this
  site"); it never types a password. The browser closes when the request is done.
- **Tool calls look like tool calls.** Calls a model writes as text (Hermes `<tool_call>`, Qwen3-Coder XML, GLM,
  functionary, Mistral, Llama, Granite, DeepSeek, Gemma) are parsed and run instead of printed
  (`toolcall_text.py`); steps that only call tools show the tool cards, not their thinking. With tools switched off,
  Aero says so with a one-click "Turn on tools".
- **Tool calling is part of tuning.** Every tune ends with a tool-calling exam: 40 cases from the Berkeley
  Function-Calling Leaderboard (BFCL v4, Apache-2.0: simple, multiple, parallel, irrelevance) and 12 Aero requests
  (`tool_eval.py`). It picks the temperature for tool steps and whether Aero should start actions itself for that
  model. Settings → Model & tuning shows the score.
- **Full tuning is full.** q4_0 KV, finer context steps and thread counts are in the search; when the improving moves
  run out, a sweep tries the untested neighbours of the best configs until 8 trials in a row change nothing
  (Qwen3-4B: 22 trials, where 1.2.0 stopped at 8). A server that dies under load near the limit counts as out of
  memory, so bigger configs aren't tried one by one.
- **Big models are built into loading.** Search finds any model on Hugging Face, any size. A repo with the original
  weights lists the GGUF versions others published (same model from known quantizers first; AWQ / FP8 repos fall back
  to their base model), or Aero makes its own: download, llama.cpp's converter, an importance matrix for low-bit
  quants, `llama-quantize` to the quant that fits this PC (`quantize.py`). The tuner now also tries the
  feed-forward split for dense models bigger than the GPU (attention and KV on the GPU, feed-forward of some layers
  in RAM) against whole-layer offload and keeps the faster: Gemma-3-27B Q8_0 under a 15 GB limit ran out of memory
  at 131k context with whole layers (26 of 62 on the GPU) but ran 131k with the split; the tuner picked 98k at
  2.7 tok/s (Balanced). At 8k both ran at 3.4-3.7 tok/s. Downloads are checked against the SHA-256 the Hub
  publishes and show the time left.
- **Every integration comes with Aero.** The installer sets them all up (Hindsight, AirLLM, image generation, code
  tools, Flutter…; `--lean` to skip), and Aero finishes any that are missing in the background, with one progress
  line under the workspaces (`integrations/bundle.py`). xtool stays opt-in because it changes WSL.
- **Hindsight's forced tool calls** are answered by the loopback adapter with a grammar-forced call, streamed or not.
- **Files Aero makes are downloadable** from the chat (Download / Open), served by id only (`made_files.py`).
- **Tidier UI:** no Compact button (compaction is automatic at 75 %); themed text fields; workspace cards and the Big
  models panel open with arrows (what it does, things to ask, its tools, what it needs); the Workspaces group in the
  sidebar folds away; a plan chip shows what Aero understood; the tuning screen shows the tool-calling exam.
- **Your own browser, signed in already.** Aero's browser tools now work in your default browser (Chrome, Edge,
  Brave or Vivaldi, read from the system's default-browser setting on Windows, macOS and Linux) instead of always
  Edge. With the browser's remote-debugging switch on (`chrome://inspect/#remote-debugging`, Chrome 144 and later;
  Settings → Tools copies the address), Aero connects to the browser you already use, with your sign-ins: the browser
  asks you to Allow the connection, Aero works in tabs it opens itself, never lists or reads your other tabs, closes
  only its own tabs when done and never closes your browser. Nothing is copied or decrypted. Without the switch,
  with strict offline on, or for a request that says "in the background", Aero uses its own profile as before and
  says how to switch.
- **Sign-in pauses:** answering the card no longer crashes the turn (the answer is the choice's text), a skipped site
  isn't asked again for every page of it, a signed-out Outlook.com (which redirects to a Microsoft 365 page) counts
  as a sign-in page, and the card reads "Aero asks" instead of an internal id. The banner says "on screen" when the
  browser is visible.
- **The tuning advisor works again.** Its server ran with `--reasoning-format none`, which llama.cpp b11501 rejects
  together with a JSON grammar ("Unexpected empty grammar stack after accepting piece: <think>"), so every tune had
  fallen back to the built-in policy after three errors. Measured after the fix: the advisor chose 6 of 10 trials.
- **The window never runs an older UI against a newer Aero:** the page names its scripts and styles with the version
  (`app.js?v=1.2.1`) and the UI files are revalidated on every load.
- **Gemma's tool calls** (`print(default_api.tool(...))` in a `tool_code` or `python` block) run instead of
  showing as code; the tool exam caps answers at 256 tokens and shows its progress.
- **Sturdier setup:** pip installs into Aero's own environments retry (a 3 GB PyTorch download dropped once in
  testing), llama.cpp's converter is fetched file by file and checked against its git hashes, and the importance
  matrix learns from real text (Aero's docs and source) instead of word lists.

## Aero 1.2.0 (2026-10-12)

Aero becomes an all-in-one workstation: other projects' functions built into Aero in workspaces, autonomy modes for
chats and forever-loops, models bigger than your GPU, a faster router, and an installer that can be tested anywhere.
Details: `docs/V1.2_INTEGRATIONS.md`, `docs/V1.2_AUTONOMY.md`, `docs/V1.2_BIG_MODELS.md`,
`docs/V1.2_PERFORMANCE_REPORT.md`, `docs/V1.2_TEST_MATRIX.md`.

### Workspaces and integrations

- **Workspaces** in the sidebar: Code, Mobile apps, Create, Diagrams, Review & test, Debug & reverse, Finance, Library,
  Memory+, Big models. Each is a panel beside the chat with what is installed (Install buttons that ask first and show
  the size), the pack's live state and its results. A request that needs one opens it by itself: patterns per pack,
  no model call, a few milliseconds (`integrations.context`); the pack's tools are added for that turn and its
  skills named, while the router's own catalog stays at 53 tools.
- **15 integration packs** (`aero/integrations/`), each with its source and licence in the workspace and in
  `docs/V1.2_INTEGRATIONS.md`, each installed and run on the test PC (`docs/V1.2_TEST_MATRIX.md`):
  - *Code*: uv, ruff, pyright, Node.js, Biome, TypeScript, Go and Rust installed into `data/toolchains` with their
    published checksums; `project_check` runs lint, types and tests with machine-readable output (ruff JSON, pyright
    JSON, pytest JUnit, Biome JSON, tsc, cargo clippy JSON, go vet, go test JSON, including build failures) into one
    diagnostics list; `web_compat` answers browser-support questions from MDN's browser-compat-data.
  - *Mobile apps*: Flutter (SDK with its published SHA-256, analytics off): create, pub, analyze (machine output),
    test (JSON reporter), build, devices, doctor, and `flutter_preview`: the app served as web and shown live inside
    the workspace, with hot reload (measured: the change on screen 3.5 s after the edit) and hot restart over
    Flutter's JSON-RPC. xtool for SwiftPM iOS apps from WSL (install, `xtool new`, `xtool dev` once the user has added
    Apple's SDK and signed in themselves).
  - *Create*: `image_generate` with stable-diffusion.cpp (Vulkan build) and Z-Image-Turbo (Apache-2.0), 8 steps,
    1024 × 1024 in 14 s on the test PC; weights stay in RAM and Aero gives the engine a VRAM budget from free memory (`--max-vram`, 2-6 GiB): 4.5 GB on a 16 GB card, 3.2 GB at the floor; a gallery. ArtCraft is the layout reference; its cloud
    generators are not used.
  - *Diagrams*: diagram-design's skill vendored; `diagram_save` runs its self-check and shows the diagram in a
    sandboxed frame, `diagram_import` reads Mermaid / draw.io / Excalidraw, `diagram_export` makes SVG and PNG.
  - *Review & test*: open-code-review's `ocr` on Aero's own model (its rule documents vendored for delegate mode);
    e2e's explorer and runner on Aero's model with telemetry forced off.
  - *Debug & reverse*: RAD Debugger's converter (`debuginfo_dump`, `debuginfo_convert`, `symbolize`) and debugging
    sessions driven over its IPC socket; rea registered as an MCP server. RAD Debugger's IPC socket has no
    authentication and listens on every network interface, so a session runs only while Windows Firewall keeps the
    network away from `raddbg.exe` (checked before every command; Aero never edits firewall rules).
  - *Finance*: 48 skills from anthropics/financial-services, `xlsx_audit` (errors, hard-coded numbers in formulas,
    broken row patterns, empty references, circular references), `xlsx_recalc` (LibreOffice), `dcf_validate`,
    `deck_numbers` (numbers of the same category and unit that disagree across slides).
  - *Library*: awesome-python and the-book-of-secret-knowledge bundled as data, awesome-selfhosted downloaded on
    request; `library_search` with tags (e.g. "Communication - IRC"); risky one-liners tagged and never run.
  - *Memory+*: Hindsight in its own environment on 127.0.0.1 with Aero's model; Aero starts its PostgreSQL itself,
    loopback only, with the data in Aero's folder; reflect's forced tool calls go through a loopback adapter
    (`integrations/llm_adapter.py`), because llama-server reads a named `tool_choice` as "auto".
  - *Big models*: see below.
- `integration_install` lets the model install what a pack needs (permission *setup*, ask by default).
- Processes a pack started (Hindsight and its database, a Flutter preview, a debugging session) stop when Aero quits.
- Vendored material keeps its licence files and an `UPSTREAM.md` with the commit (`skills_builtin/packs/`); pack
  skills load with `use_skill` but stay out of the router's catalog.

### Autonomy

- Settings → **Autonomy**, for chats and forever-loops separately: *Ask me each time*, *Collect them and keep
  working* (queued actions don't run; the agent continues; one card to approve or deny the list; approved actions run
  in the same turn or at the start of the next), or *Full auto* (an authority model on the CPU allows or denies each
  action with a reason; never allows when it can't answer; logged in `data/autonomy/decisions.jsonl`).
- The authority model was chosen by measurement: Qwen3-4B-Instruct-2507 answered all 32 exam decisions right
  (14 traps: injected instructions, money, passwords, mass deletion, security changes), 2.9 s median on the CPU;
  LFM2.5 models allowed 7-8 traps and are not offered (`validation/bench_authority.py`).
- The model is told when an action was queued or decided by the authority; queued actions are "NOT run" until they
  are approved.

### Big models

- `bigmodel.py` plans any GGUF for the VRAM you give it from its real tensor sizes: whole layers, feed-forward
  weights in RAM with attention and KV on the GPU (`--override-tensor`, now passed through `cfg["ot"]`), MoE experts
  in RAM, or all on the GPU; tied output embeddings counted (Gemma). It adds a same-family draft model for
  speculative decoding and reserves its VRAM. Measured on an RTX 5080 + DDR5: Llama-3.3-70B Q6_K at **4.85 tok/s
  with Llama-3.2-1B as the draft** (1.0 without) on 16 GB; Gemma-3-27B Q8_0 in 2.8 GB of VRAM at 1.9 tok/s, 2.6 with
  Gemma-3-1B as the draft in 3.1 GB.
- **AirLLM** (lyogavin/airllm) built in for full-precision Hugging Face checkpoints: its own environment (PyTorch
  CUDA, bitsandbytes for its 4-bit and 8-bit layer compression), an OpenAI-compatible shim on Aero's model port so
  the whole agent works with it, one folder of layer files per model, loaded from the Big models workspace. Measured:
  Qwen2.5-32B-Instruct in bf16 at 30 s per token in 1.9 GB of VRAM, 18 s in 8-bit, 12 s in 4-bit; Qwen2.5-7B bf16 7.3 s, 8-bit 5.1 s,
  4-bit 4.3 s (the 4-bit 7B answered wrongly: use 8-bit or full precision when the answer matters).
- `validation/bench_bigmodel.py` measures plans (VRAM, prompt and generation speed, load time).

### Router (faster)

- A shorter decision form: a short intent first, a plan only for long multi-step requests, the preference field only
  when the message sounds like one, no router call for greetings and thanks. CPU decisions p50 2.8 → 1.6 s (50
  requests) and 2.4 → 1.8 s (held-out), same accuracy (50/50) and one more held-out request right (23/25).
- Settings → Router → *Runs on*: GPU for decisions in 0.39 s (p50, 50 requests; held-out 0.45 s); it moves back to
  the CPU while Remote Mode is active. The `router_plan` setting now does what it says.

### Installer and updater (tested anywhere)

- `Update-Aero.bat` / `Install-Aero.bat`: `--dest FOLDER` (or `AERO_INSTALL_DIR`), `--skip-llama`, `--no-shortcuts`,
  `--no-register`, `--skip-models`, `--headless`; installs outside `C:\Aero` need no administrator rights; a failed
  or unanswerable elevation stops with an error.
- `validation/check_windows_update.py` and the CI job `windows-update`: a published release installed into a
  throwaway folder, updated in place with the new build, then through Aero's own updater from a fake release (a
  tampered download is refused), plus a fresh install; from the newest release and from 1.0.0.

### Security

- Aero's API refuses state-changing requests that don't come from Aero's own window (Origin / Sec-Fetch-Site) and
  requests with an unknown Host header. Before, a web page open in a browser could send a "simple" cross-site POST
  to 127.0.0.1 and start turns or change settings.
- Integration installs show their size, go through the *setup* permission, verify published checksums and honour
  strict offline; the file endpoint serves only Aero's diagrams and images (sandboxed); the workspace run endpoint
  only runs integration tools.

### Fixed while testing every integration live

- MCP tool schemas that use `$ref` into `$defs` (rea's) were passed on without their definitions, and llama-server
  refused every request; references are now resolved in place.
- With no router decision every tool went to the model: one MCP server with 170 tools made a 230,000-token request.
  Tool definitions now take at most 35 % of the context, Aero's own tools and the request's hinted tools first, the
  rest through `load_tools` (which refuses what can't fit); a pack's MCP server stays out of the router.
- A model that calls a tool it wasn't offered got an empty reply (llama-server drops the call): the step is asked
  once more for an answer, and a JSON call printed as text runs as a real call when the tool exists and is enabled.
- Draft, image and router models are no longer listed as chat models (`_drafts`, `_create` were missing).
- Big models panel: split GGUF sizes summed; a draft model already in the models folder is used.
- The release archives now include the Library's bundled lists: the build skipped every folder named `data`,
  not only the runtime one.

### Other

- The built-in prompt mentions integrations and queued/authority-decided actions; an unedited 1.1 prompt is replaced.
- `ruff.toml` excludes vendored upstream files from Aero's lint; the older modules' remaining lint findings are fixed.
- New tests: autonomy (22), integrations (51); 291 in total.

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
