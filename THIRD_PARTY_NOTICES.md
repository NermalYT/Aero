# Third-party notices

Aero is MIT-licensed (see `LICENSE`). It ships third-party material only where listed under "Bundled"; each keeps
its own licence file. Everything else is installed from PyPI or npm, or downloaded from its project, when you choose
to, and is listed here with its licence.

## Bundled in `source/aero/static/vendor/`

| File | Project | Licence |
|---|---|---|
| `marked.js` | marked (MarkedJS, Christopher Jeffrey) | MIT |
| `purify.min.js` | DOMPurify 3.4.16 (Cure53 and contributors) | Apache-2.0 or MPL-2.0 |
| `highlight.min.js`, `hljs.css` | highlight.js 11.12.0 (Josh Goebel and contributors) | BSD-3-Clause |

## Bundled in `source/aero/skills_builtin/packs/` (new in 1.2)

Each folder holds the project's licence file and an `UPSTREAM.md` with the repository and commit. Files are
unchanged.

| Folder | Project | Commit | Licence |
|---|---|---|---|
| `diagram-design/` | cathrynlavery/diagram-design (skill, references, templates, scripts; icons per its THIRD_PARTY_LICENSES.md) | `7a2e221` | MIT (icons MIT / CC0) |
| `open-code-review/` | alibaba/open-code-review (skills, per-language rule documents) | `357c3f0` | Apache-2.0 |
| `e2e/` | tester-army/e2e (skill and references; NOTICE kept) | `449fa93` | Apache-2.0 |
| `rea/` | morluto/rea (reverse-engineer-anything skill) | `f28f338` | MIT |
| `finance/` | anthropics/financial-services (48 vertical skills) | `574ed36` | Apache-2.0 |

## Bundled in `source/aero/integrations/data/` (new in 1.2)

| File | Project | Commit | Licence |
|---|---|---|---|
| `awesome-python.json` | vinta/awesome-python, converted to JSON (entries unchanged) | `ab40553` | CC BY 4.0 |
| `secret-knowledge.json` | trimstray/the-book-of-secret-knowledge, converted to JSON (entries unchanged) | `7d37069` | MIT |

## Bundled in `source/aero/tool_eval/`

| File | Project | Commit | Licence |
|---|---|---|---|
| `bfcl_sample.json` | ShishirPatil/gorilla, Berkeley Function Calling Leaderboard v4: a fixed 40-case sample of its live and simple categories (function names with dots use `__`) | `6ea5797` | Apache-2.0 |

awesome-selfhosted (awesome-selfhosted/awesome-selfhosted-data, **CC BY-SA 3.0**) is not bundled: Aero downloads it
into your data folder when you ask, and it stays under its own licence there.

## Installed from PyPI (`source/requirements.txt`, `source/requirements-extra.txt`)

| Package | Licence (from its package metadata) | Notes |
|---|---|---|
| fastapi, uvicorn, httpx | MIT / BSD-3-Clause / BSD-3-Clause | |
| psutil | BSD-3-Clause | |
| mss | MIT | |
| pillow | MIT-CMU (HPND) | |
| pyautogui, pygetwindow | BSD | |
| pypdf | BSD-3-Clause | |
| python-docx, python-pptx | MIT | |
| openpyxl | MIT | |
| uiautomation | Apache-2.0 | Windows only |
| tzdata | Apache-2.0 | Windows only |
| winrt-runtime, winrt-Windows.Media.Ocr and the other winrt-Windows.* packages it needs (pywinrt) | MIT | Windows only (1.3: OCR with Windows' own engine) |
| nvidia-ml-py | BSD | |
| anthropic, openai | MIT / Apache-2.0 | |
| playwright | Apache-2.0 | optional |
| claude-agent-sdk | MIT | optional |

## Downloaded with Aero (1.2: integrations, each into `data/toolchains` or its own environment; set up by the installer and in the background, `--lean` to skip)

| What | Project | Licence |
|---|---|---|
| uv, ruff | astral-sh/uv, astral-sh/ruff | MIT / Apache-2.0 (uv dual), MIT (ruff) |
| Biome | biomejs/biome | MIT / Apache-2.0 |
| Node.js | nodejs/node | MIT (bundled components under their own licences) |
| pyright, TypeScript | microsoft/pyright, microsoft/TypeScript (npm) | MIT, Apache-2.0 |
| Go | golang/go | BSD-3-Clause |
| Rust (rustup, cargo, clippy, rustfmt) | rust-lang | MIT / Apache-2.0 |
| MDN browser-compat-data | mdn/browser-compat-data | CC0-1.0 |
| Flutter SDK | flutter/flutter | BSD-3-Clause (the SDK bundles Dart and other components under their own licences) |
| xtool | xtool-org/xtool | MIT (Xcode.xip and Apple's SDK: Apple's licence, accepted by you) |
| open-code-review (`ocr`) | alibaba/open-code-review | Apache-2.0 |
| e2e, @e2e-dev/web, ai, @ai-sdk/openai-compatible | tester-army/e2e, vercel/ai | Apache-2.0 |
| RAD Debugger | EpicGames/raddebugger | MIT |
| rea-agents | morluto/rea | MIT |
| Hindsight (hindsight-api-slim) | vectorize-io/hindsight | MIT (in its own Python environment) |
| pg0 (embedded PostgreSQL with pgvector, a Hindsight dependency) | vectorize-io/pg0; PostgreSQL; pgvector | MIT; PostgreSQL Licence; PostgreSQL Licence (in Hindsight's environment) |
| stable-diffusion.cpp | leejet/stable-diffusion.cpp | MIT |
| whisper.cpp (`whisper-cli`, 1.3: voice typing) | ggml-org/whisper.cpp | MIT |
| Whisper base model, 8-bit (`ggml-base-q8_0.bin`) | OpenAI Whisper, converted by ggerganov/whisper.cpp (Hugging Face) | MIT |
| Z-Image-Turbo, its VAE | Tongyi-MAI/Z-Image-Turbo (GGUF by leejet; VAE from Comfy-Org/z_image_turbo) | Apache-2.0 |
| Qwen3-4B-Instruct-2507 (authority model, image text encoder) | Qwen (GGUF by unsloth) | Apache-2.0 |
| AirLLM, PyTorch, transformers, accelerate | lyogavin/airllm; pytorch; huggingface | Apache-2.0; BSD-3-Clause; Apache-2.0 (in AirLLM's own environment) |
| bitsandbytes (AirLLM's 4-bit / 8-bit layers, NVIDIA only) | bitsandbytes-foundation/bitsandbytes | MIT (in AirLLM's own environment) |
| llama.cpp's converter (`convert_hf_to_gguf.py`, `gguf-py`), from the source archive of the installed build | ggml-org/llama.cpp | MIT (run in its own Python environment with numpy, sentencepiece, safetensors, transformers, protobuf, tiktoken, PyTorch for the CPU) |
| Draft models for big models | Llama-3.2-1B (Llama 3.2 Community Licence), Gemma-3-1B (Gemma Terms), Qwen2.5-0.5B / Qwen3-0.6B (Apache-2.0) | as listed; downloaded only when you ask |

llama.cpp (MIT) binaries come from its GitHub releases, or are built from source where no build fits. Model files
are downloaded only when you choose them, under each model's own licence.

## Used as a reference, no code taken

- 1.2: storytold/artcraft (MIT / Apache-2.0) for the Create workspace's layout; its cloud generators are not used.
- 1.1: Microsoft UFO, browser-use, Microsoft Playwright MCP, Docling, pywinauto, the MCP specification and reference
  servers. Rejected for licence or weight reasons: crawl4ai, OmniParser, RustDesk (AGPL-3.0). Details:
  `source/docs/V1.1_OPEN_SOURCE_RESEARCH.md` and `source/docs/V1.2_INTEGRATIONS.md`.
