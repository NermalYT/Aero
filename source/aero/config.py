"""Paths and persistent settings for Aero.

Everything lives under one root folder: C:\\Aero on Windows, ~/.local/share/aero on Linux and
~/Library/Application Support/Aero on macOS (AERO_HOME overrides it):
    app/        this package
    venv/       private Python environment
    llama/      llama.cpp binaries (llama-server + its GPU libraries)
    models/     downloaded GGUF files (changeable in Settings); models/_router holds the CPU router
    data/       settings, chats, model registry, tuning cache, uploads, logs, secrets, mods, updates
"""
import hashlib
import json
import os
import threading
from pathlib import Path

from .osinfo import IS_LINUX, IS_MAC, IS_WIN  # noqa: F401  (re-exported for the other modules)

APP_NAME = "Aero"
VERSION = "1.0.0"
REPO = "NermalYT/Aero"            # GitHub repository the updater reads releases from

PKG_DIR = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("AERO_HOME") or PKG_DIR.parent.parent).resolve()
DATA = ROOT / "data"
LLAMA_DIR = Path(os.environ.get("AERO_LLAMA_DIR") or ROOT / "llama").resolve()   # validation points it at a real install
CHATS = DATA / "chats"
UPLOADS = DATA / "uploads"
LOGS = DATA / "logs"
for _d in (DATA, CHATS, UPLOADS, LOGS):
    _d.mkdir(parents=True, exist_ok=True)

# A system-wide HTTP_PROXY would otherwise route Aero's calls to its own llama-server and UI through the proxy, which
# can't reach this computer's 127.0.0.1. Child processes inherit this too.
for _k in ("NO_PROXY", "no_proxy"):
    _have = [h.strip() for h in os.environ.get(_k, "").split(",") if h.strip()]
    os.environ[_k] = ",".join(_have + [h for h in ("127.0.0.1", "localhost", "::1") if h not in _have])

UI_PORT = int(os.environ.get("AERO_PORT", "8180"))
LLAMA_PORT = int(os.environ.get("AERO_LLAMA_PORT", "8181"))
TRIAL_PORT = LLAMA_PORT + 1
ADVISOR_PORT = LLAMA_PORT + 2
ROUTER_PORT = LLAMA_PORT + 3

_PROMPT = """You are Aero, an autonomous AI agent running locally on the user's own computer ({os}). You are not a chatbot that describes what could be done: you have real tools, and when a task needs them you use them and finish the job. You can read, write and search files, run {shell} commands, {screen_line} run a real web browser, search the web, use connected MCP servers (GitHub and others), load skills, and remember things across chats.

# How Aero works around you
- A small router model on the CPU reads each request first. It picks the tools you are given and may add a short plan. Both arrive in the <turn_context> block at the end of the user's message, together with the time it was sent and relevant memories. Aero writes that block, not the user. Follow the plan when it fits, and ignore any step that turns out to be wrong.
- You only see the tools picked for this task. If you need one you do not have, call load_tools with tool names or a category (files_read, files_write, shell, screen, desktop, browser, web, memory, mcp, skills). Never pretend to have a tool you were not given.
- When the user turns on review, your finished work is checked in the cloud: by GPT-6 Astra (ChatGPT), by Claude Fable 5.1, or by both, ChatGPT first. A message that starts with [Message from the cloud reviewer] lists problems in your work and a plan to fix them: fix every point, verify, then give a new final answer. If a review finds major problems, GPT-6.1 Sol or Claude Opus 5.5 may finish the task; its answer then appears in the chat so you can learn from it.
- Memories of kind "lesson" are corrections from earlier reviews. Apply them every time they are relevant.

# How to work
1. Understand the goal. If the request is clear, start immediately. Ask a question only when a wrong guess would waste a lot of work or be hard to undo; otherwise pick the sensible default, say which one, and keep going.
2. Look before you act. Never guess file contents, folder layouts, command output, window contents or what is on screen: read or look first.
3. Act in small, checked steps. After every action, check its result (the tool output or a fresh view) before the next one. If something did not work, read the error, change your approach and try again; do not repeat the exact same failing call.
4. Keep going until the goal is actually done, then verify it (re-read the file, re-run the command, look at the window). Only then give the final answer.
5. Final answer: lead with the result, then the important details (paths, values, what changed). Say plainly what you did not finish or could not verify. Never claim something worked unless a tool showed it.

# Tool calling rules
- Call tools through the tool-calling interface only. Never write a tool call as text, JSON or a code block in your reply, and never invent tool results.
- Use exact argument names and types from each tool's schema. Paths can be absolute or relative to the working directory.
- One clear purpose per call. When several lookups do not depend on each other, call them together in one step; when one depends on another's result, wait for it.
- Read the whole tool result before deciding the next step. Errors are information: fix the cause (wrong path, wrong id, missing permission) rather than retrying blindly.
- Some tools may need the user's approval. If a call is denied, do not retry it; explain what you wanted to do and offer another way.
- Keep the user in the loop on long tasks with a short sentence between steps, but do not narrate every click.

# Files and commands
- Explore with list_dir, find_files and search_files before reading; read_file shows numbered lines.
- Change existing files with edit_file (exact snippet replace) instead of rewriting whole files. Re-read the changed part afterwards.
- run_command uses {shell}. Prefer non-interactive commands, quote paths with spaces, and check the exit code and output. Do not start programs that wait for input, and never run destructive commands (deleting data, formatting, registry or system changes, killing unknown processes) unless the user clearly asked for exactly that.

{apps_section}# Web, GitHub and MCP
- web_search to find sources, fetch_url to read a page's text quickly, browser_* tools when you need to click, log in, fill forms or the page needs JavaScript. In the browser, use the numbered refs from browser_open/browser_snapshot.
- Tools named mcp_<server>_<tool> come from connected MCP servers (for example mcp_github_... for the user's GitHub). Read their descriptions; anything that writes to an outside service (pushing, commenting, opening issues or pull requests, sending messages) needs the user to have asked for it.
- Prefer primary sources, include links for facts you looked up, and never invent URLs or citations.

# Skills
- A skill is a folder of instructions for one kind of task. When <turn_context> lists relevant skills, or a task matches a skill you know about, call use_skill(name) and follow what it says.

# Memory
- The "About the user" section at the end of this prompt says who the user is, how they like answers written, and what you worked on together recently. Follow it in every reply without mentioning it.
- Relevant memories from earlier chats appear in <turn_context>. Treat them as true background context unless the user corrects them.
- Use remember for lasting facts the user would not want to repeat (their setup, projects, decisions). When the user states or shows how they want you to work or write ("shorter", "stop using tables", "always comment the code"), save it with kind "preference" so every future chat and model follows it. Do not store secrets, passwords or API keys, or one-off details.
- Use recall when the user refers to something from an earlier chat or you need past context. Use forget when a memory is wrong.
- Long chats are compacted automatically: when a carried-over summary appears, continue the work from it without asking the user to repeat themselves.

# Style
- Direct and concise; no filler or flattery. Use Markdown, short headers and lists for structure, and fenced code blocks with a language tag for code and commands.
- When a step failed or you are unsure, say so plainly.
- Where the "About the user" section asks for something different, follow it."""

_APPS_WINDOWS = """# Seeing and controlling apps (fastest and most reliable path first)
1. Pick the window: app_list shows open windows; app_view("<part of title or app name>") selects one. If the app is not open, start it with open_app, then app_view it.
2. Look: app_view returns a picture of only that window (even when it is covered) with numbered boxes drawn on clickable controls, plus the same numbered list of buttons, fields, menus and text. The [n] number in the list is the box number in the picture.
3. Navigate by element id, not by pixels: app_click(element=n) presses buttons, menu items, tabs, checkboxes and list items directly; app_type(text, element=n) fills a field (mode "replace" or "append"). These run in the background and do not touch the user's mouse or keyboard.
4. Big or busy app? Use app_view(find="save") to list only matching controls (it searches deeper than the normal list), and app_view(zoom=[x, y, w, h]) to see a small region at full resolution when text is tiny.
5. Every app_click/app_type/app_keys/app_scroll returns a fresh view (new picture and new element list) so you can see the result immediately. Element ids change with every view, so always use ids from the latest result.
6. Use x,y clicks only when the target has no element id (canvas, game, custom-drawn UI); coordinates are pixels of the latest app_view picture.
7. Keys: app_keys("enter"), app_keys("ctrl+s"), app_keys("tab tab enter"). Long text: app_read reads all text in the window or one element; better than a picture for documents, logs and chats.
8. If an action had no visible effect, retry once with input="real" (briefly borrows the real mouse and keyboard, then puts them back). Some games, Chromium and Electron apps need this.
9. Prefer app_* tools over full-screen screenshot plus mouse_click; use screenshot only to see the whole desktop or multiple monitors. With screenshot, coordinates are pixels of the latest screenshot.
10. Never type passwords or payment details, never confirm purchases, deletions or messages to other people unless the user asked for exactly that.
"""

_APPS_OTHER = """# Seeing and controlling the screen
1. screenshot shows the whole screen; mouse_click, type_text, press_keys and scroll act on it in the screenshot's pixel coordinates. Take a fresh screenshot after every action to check the result.
2. Open apps, files, folders and URLs with open_app (an app name, a command, a path or a URL).
3. Prefer files, the shell and the browser tools when they can do the job: they are faster and more reliable than clicking.
4. Never type passwords or payment details, never confirm purchases, deletions or messages to other people unless the user asked for exactly that.
"""


def _render_prompt():
    from . import osinfo
    if IS_WIN:
        screen = "see the screen, look inside any app window and drive it with your own cursor and keyboard,"
        apps = _APPS_WINDOWS
    elif osinfo.can_drive_desktop():
        screen = "see the screen and use the mouse and keyboard,"
        apps = _APPS_OTHER
    else:
        screen = ""
        apps = ""
    text = _PROMPT.replace("{os}", osinfo.name()).replace("{shell}", osinfo.shell_name())
    text = text.replace("{screen_line} ", screen + " " if screen else "").replace("{apps_section}", apps + "\n" if apps else "")
    return text


DEFAULT_SYSTEM_PROMPT = _render_prompt()

# The starting "About you" text (Settings > Memory). Empty for a new install: each user writes their own (the
# Memory page offers a template). Profiles from earlier installs carry over (see migrate.py).
STARTER_PROFILE = ""

DEFAULTS = {
    "models_dir": str(ROOT / "models"),
    "hf_token": "",
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "temperature": 0.6,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "repeat_penalty": 1.0,
    "max_tokens": 0,               # 0 = until the context is full
    "thinking": "auto",            # True | False | "auto" (the router decides per request)
    "tune_mode": "balanced",       # balanced | max_context | max_speed
    "allow_q4_kv": False,          # let the tuner try a q4_0 KV cache (more context, small quality loss)
    "context_cap": 0,              # 0 = no cap beyond the model's trained context
    "advisor_model": "auto",       # auto (Bonsai first) | off | <hf org/repo> | <path to a .gguf>
    "last_vram_limit_gb": None,    # shown as a hint only; the limit must be typed before every tune
    "extra_server_args": "",
    "auto_load_last": False,
    "agent_max_steps": 40,
    "subagent_max_steps": 20,     # tool steps one subagent may take before it has to report back
    "tools_enabled": True,
    "tool_policy": {               # ask | auto | off
        "files_read": "auto",
        "files_write": "ask",
        "shell": "ask",
        "screen": "auto",
        "desktop": "ask",
        "browser": "auto",
        "web": "auto",
        "memory": "auto",
        "mcp": "ask",
        "skills": "auto",
        "agents": "auto",
        "mods": "off",             # mod_check: only switched on inside a "Mod Aero" chat (see mods.py)
    },
    "work_dir": str(Path.home()),
    "screenshot_max_side": 1568,
    "keep_screenshots": 2,         # how many recent screenshots stay as images in context
    "user_profile": STARTER_PROFILE,  # "About you": who the user is and how they like answers; every model gets it
    "profile_enabled": True,       # put the profile, learned preferences and recent chats in every system prompt
    "profile_budget_tokens": 1200, # max tokens for that block (also capped at 1/8 of the context)
    "learn_preferences": True,     # the router notices preferences the user states and saves them to memory
    "memory_enabled": True,        # long-term memory block in every chat's system prompt
    "memory_budget_tokens": 1500,  # max tokens of memory per chat (also capped at 10% of the context)
    "auto_compact_at": 0.75,       # compact into a fresh chat when the context is this full (0 = off)
    "auto_memorize": True,         # summarize chats you leave so later chats remember them
    # --- router (small CPU model that picks tools, thinking and a plan before the main model runs)
    "router_enabled": True,
    "router_model": "",            # path to the router .gguf ("" = the one chosen by the updater)
    "router_threads": 0,           # 0 = the benchmarked best
    "router_plan": True,           # let the router add a short plan for complex requests
    "router_tools_min": 4,         # never give the main model fewer tools than this (it can load_tools for more)
    # --- cloud review (Claude Fable 5.1 reviews; Claude Opus 5.5 takes over on major problems)
    "cloud_backend": "auto",       # auto | plan (your Claude subscription via Claude Code) | api (Console API key)
    "claude_cli_path": "",         # "" = the claude.exe bundled with claude-agent-sdk, else the installed one
    "claude_plan_signed_in": False,  # last known result of `claude auth status` (refreshed by Settings → Claude)
    "review_mode": "off",          # off | on | auto (router decides) - the composer button overrides per message
    "review_model": "claude-fable-5-1",
    "fix_model": "claude-opus-5-5",
    "review_effort": "high",       # low | medium | high | xhigh | max
    "fix_effort": "high",
    "review_max_rounds": 2,        # local fix attempts on "minor" before Opus takes over
    "cloud_daily_budget_usd": 10.0,  # stop calling Claude for the day after this much (0 = no cap)
    "cloud_fable_tools": True,     # let Fable read files / search the web while reviewing
    "review_in_loop": False,       # review every iteration of a forever-loop (costly)
    # --- ChatGPT review (GPT-6 Astra reviews; GPT-6.1 Sol repairs major problems). Runs before Claude when both are on
    "chatgpt_review_mode": "off",  # off | on | auto (router decides) - the composer's ChatGPT button overrides per message
    "chatgpt_backend": "auto",     # auto | plan (your ChatGPT plan via the official Codex CLI) | api (OpenAI API key)
    "codex_cli_path": "",          # "" = codex on PATH or npm's global folder
    "chatgpt_plan_signed_in": False,  # last known result of `codex login status` (refreshed by Settings → ChatGPT)
    "gpt_review_model": "gpt-6-astra",
    "gpt_fix_model": "gpt-6.1-sol",
    "gpt_review_effort": "high",   # low | medium | high | xhigh | max
    "gpt_fix_effort": "high",
    "gpt_review_tools": True,      # let Astra read files / search the web while reviewing
    "openai_daily_budget_usd": 10.0,  # stop calling the OpenAI API for the day after this much (0 = no cap)
    "lessons_enabled": True,       # save what the local model learned from reviews as lessons
    "share_lessons_as_training": True,  # also append them to data/training/corrections.jsonl
    # --- forever-loop
    "loop_delay_s": 5,
    "loop_max": 0,                 # 0 = until you press stop
    "shared_learning": True,       # every run's tool results and notes, shared with every model (experience.py)
    "loop_journal": True,          # after each round the model notes what worked and what didn't (looplog.py)
    # --- integrations
    "github_toolsets": "context,repos,issues,pull_requests,actions",
    "github_read_only": False,
    "skills_disabled": [],
    # --- appearance
    "theme": "auto",               # auto | day | night
    "motion": True,                # floating bubbles and soft animations
    "scenery": "",                 # full | still | off ("" = follow motion: off -> still)
    "dashboard": True,             # live side panel
    "spec_mode": "auto",           # auto (model's MTP layer or draft model + n-gram) | ngram (no extra VRAM) | off
    "draft_model": "",             # optional speculative-decoding draft .gguf (same tokenizer family)
    "pond": True,                  # the frog pond along the bottom of the window
    "scene_pause_busy": True,      # freeze the scenery while a model is generating, tuning or benchmarking
    "transparency": True,          # Aero glass blur behind the window frames (Windows 7's "Enable transparency")
    "local_only": False,           # composer's Local Only: no web, browser or MCP tools and no cloud reviews
    "strict_offline": False,       # block every outbound connection from Aero's own code (Settings > Privacy)
    "update_check": True,          # look for a newer Aero release on GitHub once, when Aero starts (see updater.py)
    "update_skip": "",             # a version the user chose "Skip this version" for
    "settings_version": 2,
}

_lock = threading.Lock()


def _read_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: Path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# sha256 (first 16 hex) of whitespace-normalized built-in prompts from older versions (VRAMpire 1.x, Halcyon 1.x-2.1, Aero 1.0).
# A saved prompt matching one of them was never edited by the user, so the current built-in prompt replaces it.
_OLD_PROMPT_HASHES = {"da007051eb78a68a", "1fd67a9e6f73d5db", "ef381fe6be5c58eb", "a820eff84853d0f2",
                      "81cdc02aa91d3a9d", "36a783e6649e46ef", "56ebba6b1529cdf4"}


def _prompt_hash(text):
    return hashlib.sha256(" ".join(str(text or "").split()).encode()).hexdigest()[:16]


def load_settings() -> dict:
    with _lock:
        s = _read_json(DATA / "settings.json", {})
    if s.get("system_prompt") and _prompt_hash(s["system_prompt"]) in _OLD_PROMPT_HASHES:
        s.pop("system_prompt", None)            # an untouched old built-in prompt: use the current one
    merged = json.loads(json.dumps(DEFAULTS))
    for k, v in s.items():
        if k == "tool_policy" and isinstance(v, dict):
            merged["tool_policy"].update(v)
        else:
            merged[k] = v
    return merged


def save_settings(patch: dict) -> dict:
    cur = load_settings()
    for k, v in patch.items():
        if k not in DEFAULTS:
            continue
        if k == "tool_policy" and isinstance(v, dict):
            cur["tool_policy"].update(v)
        else:
            cur[k] = v
    out = dict(cur)
    if out.get("system_prompt") == DEFAULT_SYSTEM_PROMPT:
        out.pop("system_prompt")                # keep following the built-in prompt as it improves
    with _lock:
        _write_json(DATA / "settings.json", out)
    return cur


def models_dir() -> Path:
    p = Path(load_settings()["models_dir"])
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---- small JSON stores ---------------------------------------------------

def read_store(name: str, default):
    with _lock:
        return _read_json(DATA / name, default)


def write_store(name: str, obj):
    with _lock:
        _write_json(DATA / name, obj)
