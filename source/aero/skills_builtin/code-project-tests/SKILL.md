---
name: code-project-tests
description: Open the user's most recent code project (VS Code), find the failing tests and fix them by editing files and running the tests, verified by a real test run.
version: 1.1.0
apps: vscode, visual studio code, terminal
capabilities: filesystem.search, filesystem.modify, terminal.execute, app.launch
backends: file, terminal
os: windows, linux, macos
---

# Fix the failing tests in a project

Use for "open my most recent Python project in VS Code and fix the failing tests".

## 1. Which project
- VS Code keeps its recent folders in its own storage: Windows `%APPDATA%\Code\User\globalStorage\storage.json`
  (and `state.vscdb`), Linux `~/.config/Code/User/globalStorage/storage.json`, macOS
  `~/Library/Application Support/Code/User/globalStorage/storage.json`. Read it with `read_file`; look for the
  most recent folder entries.
- Several equally recent projects of the asked kind: `ask_user` which one, with the folder names as choices.
  Don't edit anything before the answer.

## 2. Work on the files, not the screen
- Edits go through `read_file` / `edit_file`; tests run with `run_command` in the project folder (pytest,
  `python -m unittest`, `npm test`, as the project uses). This is faster and more reliable than clicking in VS Code.
- Open VS Code on the folder only when the user wants to watch: `app_launch("vscode", args=["<folder>"])`.

## 3. Loop
1. Run the tests and read the failures.
2. Read the code involved; fix the cause, not the test, unless the test itself is wrong (say so).
3. Re-run the tests.

## 4. Done means a passing run
Report the command, the before and after counts, the files changed and anything still failing. Never say tests
pass without a run that shows it. Keep the user's other uncommitted changes as they are; don't commit unless asked.
