# Third-party notices

Aero is MIT-licensed (see `LICENSE`). It ships no third-party source code except the browser libraries listed under
"Bundled", which keep their own licence headers. Everything else is installed from PyPI at install time and is
listed here with its licence.

## Bundled in `source/aero/static/vendor/`

| File | Project | Licence |
|---|---|---|
| `marked.js` | marked (MarkedJS, Christopher Jeffrey) | MIT |
| `purify.min.js` | DOMPurify 3.4.16 (Cure53 and contributors) | Apache-2.0 or MPL-2.0 |
| `highlight.min.js`, `hljs.css` | highlight.js 11.12.0 (Josh Goebel and contributors) | BSD-3-Clause |

## Installed from PyPI (`source/requirements.txt`, `source/requirements-extra.txt`)

| Package | Licence (from its package metadata, 2026-10-09) | Notes |
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
| tzdata | Apache-2.0 | Windows only; new in 1.1 (IANA time zones for meeting invitations) |
| nvidia-ml-py | BSD | |
| anthropic, openai | MIT / Apache-2.0 | |
| playwright | Apache-2.0 | optional |
| claude-agent-sdk | MIT | optional |

## Downloaded at install time

llama.cpp (MIT) binaries from its GitHub releases, or built from source where no build fits. Model files are
downloaded only when you choose them, under each model's own licence.

## Projects studied for 1.1, no code taken

Microsoft UFO, browser-use, Microsoft Playwright MCP, Docling, pywinauto, the MCP specification and reference
servers. Rejected for licence or weight reasons: crawl4ai, OmniParser, RustDesk (AGPL-3.0). Details:
`source/docs/V1.1_OPEN_SOURCE_RESEARCH.md`.
