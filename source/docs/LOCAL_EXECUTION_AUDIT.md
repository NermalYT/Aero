# Local execution audit

Aero 1.0.0, 2026-10-08. This file lists every way Aero's code can reach the network, what strict offline mode does
to each one, and what was actually tested. "Tested" means a test ran and passed; anything else says so.

## The contract

Every token a local model generates comes from a `llama-server` process Aero started itself, from a GGUF file on
disk, listening on `127.0.0.1`. There is no hosted engine in the code path, and no setting that adds one. The only
cloud model use is the optional review passes, which the user turns on with the **ChatGPT** and **Claude** buttons under
the message box: GPT-6 Astra reviews and GPT-6.1 Sol repairs, then Claude Fable 5.1 reviews and Claude Opus 5.5
repairs. Neither ever counts as a local result: every cloud message is labelled with the model that wrote it.

## Inference processes

| Process | Started by | Port | Bind address | Notes |
|---|---|---|---|---|
| Main model `llama-server` | `engine.LlamaServer` | 8181 (`AERO_LLAMA_PORT`) | `--host 127.0.0.1` | `--offline` added when the build supports it |
| Tuner trial `llama-server` | `tuner.Tuner` | 8182 | `--host 127.0.0.1` | One trial at a time, stopped after each |
| Tuner advisor `llama-server` | `advisor.py` | 8183 | `--host 127.0.0.1` | Uses a local advisor GGUF; in strict offline it never downloads one |
| Router `llama-server` (CPU) | `router.py` | 8184 (8194 for its speed check) | `--host 127.0.0.1` | `--load-mode mlock` on v0.5.0+ builds |
| Aero backend (FastAPI) | `aero.__main__` | 8180 (`AERO_PORT`) | `127.0.0.1` | Serves the UI and API |
| Click overlay (Windows) | `overlay.send` | UDP 8185 | `127.0.0.1` | Shows where the computer-use tool clicks |

The trial, advisor and router ports follow the main port (+1, +2, +3). All `llama-server` arguments are built in one function (`engine.build_args`) that always writes
`--host 127.0.0.1`; the router and advisor build theirs the same way. Each child is placed in a Windows Job Object
with kill-on-close, so a crashed Aero cannot leave a server listening.

`GET /api/local_status` (Settings > Privacy & offline) lists every listening socket owned by Aero's own process
and its `llama-server` children, read live with `psutil`, and says whether all of them are loopback.

## Outbound network uses (all of Aero's code)

Found by searching the source for `httpx`, `urllib`, `socket`, `anthropic`, `openai` and `subprocess` (2026-10-08).

| Feature | Where | Destination | Strict offline |
|---|---|---|---|
| Web search, page fetch | `tools/web.py` | duckduckgo.com, any URL | Tools hidden from the models and refused if called |
| Automated browser | `tools/browser.py` | Any site | Hidden and refused |
| Remote MCP servers (HTTP) | `tools/mcp_client.py` | Server URL, OAuth endpoints | Not started; blocked at the transport if reached |
| Local MCP servers (stdio programs) | `tools/mcp_client.py` | Whatever the program does | Not started unless the entry has `"offline_ok": true` |
| GitHub sign-in check | `github.py` | api.github.com | Blocked at the transport |
| Hugging Face search and downloads | `hf.py` | huggingface.co | Blocked at the transport; the Models page shows the error |
| Claude review / Opus takeover (API key) | `cloud.py` (Anthropic SDK, which uses httpx) | api.anthropic.com | Skipped before it starts, with a notice in the chat |
| Claude review / Opus takeover (plan sign-in) | `claude_code.py` (Claude Code CLI subprocess) | Anthropic | Skipped before the subprocess starts; `status()` does not run the CLI |
| Claude Code sign-in | `claude_code.login` | Opens the official `claude auth login --claudeai` | Refused with a message |
| ChatGPT review / Sol repair (API key) | `chatgpt.py` (OpenAI SDK, which uses httpx) | api.openai.com | Skipped before it starts, with a notice in the chat; blocked at the transport if reached |
| ChatGPT review / Sol repair (plan sign-in) | `chatgpt.py` (official Codex CLI subprocess) | OpenAI | Skipped before the subprocess starts; `status()` does not run the CLI |
| ChatGPT sign-in | `chatgpt.login` | Opens the official `codex login` in its own console | Refused with a message |
| OpenAI key check | `chatgpt.test_key` (lists models) | api.openai.com | Blocked at the transport |
| llama.cpp and Python packages | `installer/setup.py`, `Update-Aero.bat` | github.com, pypi.org | Not part of the app; runs only when the user runs the updater |

Everything else that uses `httpx` talks to the local `llama-server` processes (`agent.py`, `pipeline.py`,
`memory.py`, `router.py`, `tuner.py`, `advisor.py`, `bench.py`, `server.py`).

The UI loads no external resources. Its only external URLs are links the user clicks (a model card on
huggingface.co, an example in the MCP help text), and those open in the user's normal browser.

## How strict offline works

`localonly.install()` runs before the FastAPI app is created. It wraps `httpx.HTTPTransport.handle_request` and
`httpx.AsyncHTTPTransport.handle_async_request`, which every `httpx.Client`, `httpx.AsyncClient`, module-level
`httpx.get/post`, the Anthropic and OpenAI SDKs and the Hugging Face code go through. Each request's host is checked:

- loopback (`127.0.0.0/8`, `::1`, `localhost`): always allowed, never logged;
- anything else with strict offline on: raises `OfflineBlocked` before a connection is opened, and is logged;
- anything else with strict offline off: allowed and logged.

The setting is read from disk on every check, so turning it on takes effect for the next request without a restart.

The audit log is `data/audit/network.jsonl`, one JSON line per non-loopback request:
`{"t", "method", "host", "allowed", "why"}`. Paths and query strings are never written. It rotates at 1 MB
(`network.1.jsonl`). Settings > Privacy & offline shows the last entries and the allowed/blocked counts.

## Local Only

The composer's **Local Only** button (also in Settings > Privacy & offline, setting `local_only`) is the everyday
switch; strict offline is the hard lock. With Local Only on, `localonly.tool_allowed` hides the `web`, `browser` and
`mcp` tool categories from every model (the router's catalog, the schemas sent to the model, and `tools.run` refuses
them if called anyway), `pipeline.route` turns both review passes off before anything is sent, and the model's
environment block says it has no internet. Aero's own update check and model downloads are not affected. Strict
offline implies Local Only.

## Cloud sign-ins and keys

- **Claude plan**: Aero runs the official, unmodified Claude Code program. Sign-in is `claude auth login` on
  Anthropic's own page. Aero never reads Claude Code's login file; it only runs `claude auth status`.
- **ChatGPT plan**: Aero runs the official, unmodified Codex CLI. Sign-in is `codex login` on OpenAI's own page.
  Aero never reads `~/.codex/auth.json`; it runs `codex login status` and keeps only whether you are signed in and
  how (ChatGPT or API key). The status text can contain a masked key, so it is never shown or stored. The Codex child
  process gets a copy of the environment without `OPENAI_API_KEY` and `CODEX_API_KEY`, so a key set elsewhere can't
  silently replace your plan sign-in.
- **API keys** (Anthropic, OpenAI, GitHub, Hugging Face) are stored in `data\vault.json`, encrypted with Windows
  DPAPI for the current user. The UI only ever receives a masked form.
- Reviews through the API send `store: false` to OpenAI, so responses are not kept server-side for later retrieval;
  Codex runs with `--ephemeral`, so no session file is written.

## What strict offline does not cover

These are shown in the app as well, under "Not covered".

- The shell tool runs PowerShell, which can reach the network if a command asks for it. Set the shell tool to "ask"
  to approve each command. For a hard guarantee, `validation/Validate-Aero.ps1 -FirewallTest` adds temporary
  Windows Firewall rules that block outbound traffic for Aero's Python and `llama-server.exe`, then checks a
  local chat still works.
- Programs the user starts through Aero (apps, scripts) are outside its control.
- The Edge (or Chrome) window that shows the UI is a separate browser process with its own profile in
  `data\ui-profile`. Aero's page makes no external requests, but the browser itself may check for updates.

## Tests

| Test | How | Result |
|---|---|---|
| Loopback detection | `tests/test_hapo_bench_offline.py` `OfflineTests.test_loopback` | Passed (container) |
| Strict offline blocks a remote request and allows loopback | `OfflineTests.test_strict_blocks_remote_allows_loopback` | Passed (container) |
| Async client blocked | `OfflineTests.test_async_client_blocked` | Passed (container) |
| Web, browser and MCP tools hidden in strict mode | `OfflineTests.test_strict_hides_network_tools` | Passed (container) |
| Hugging Face search blocked end to end through the running app | Strict offline on, `GET /api/hf/search`, then read the audit log | Passed (container): request refused, audit line `huggingface.co blocked` |
| Every listening socket is loopback | `GET /api/local_status` with a model loaded | Passed (container): backend and `llama-server` both on 127.0.0.1 |
| `--offline` reaches `llama-server` | `ARGS:` line in `logs/llama-server.log` | Passed (container, llama.cpp 0.5.0-dev) |
| Local chat works with the machine offline | Needs Windows and the firewall rules | Not run. `Validate-Aero.ps1 -FirewallTest` does it |
| Local Only: network tools hidden and refused, no review pass, the model told | `tests/test_local_only.py` (6 tests, one a whole turn with both reviews on) | Passed (container) |
| Both review passes skipped in strict mode | `tests/test_chatgpt.py` `PassOrderTests.test_strict_offline_skips_both_passes` | Passed (container): no review call, one notice per pass |
| Codex CLI never run in strict mode | `CodexPlanTests.test_status_does_not_run_codex_in_strict_offline` | Passed (container, fake Codex CLI) |
| An OpenAI API key in the environment never reaches Codex | `CodexPlanTests.test_plan_review_is_read_only_and_parses_verdict` | Passed (container): the child process had no `OPENAI_API_KEY` |
| Real Claude and ChatGPT reviews end to end | Needs a sign-in or API key | Not run |

Run the Windows checks with `validation\Validate-Aero.ps1` (see `docs/VALIDATION_REPORT.md`).
