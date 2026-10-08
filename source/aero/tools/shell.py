"""Shell tool: PowerShell on Windows, bash elsewhere."""
import subprocess

from . import tool
from ..config import IS_WIN

_NO_WINDOW = 0x08000000 if IS_WIN else 0
MAX_OUT = 30000


@tool("run_command", "Run a shell command and return its output. On Windows this is PowerShell "
      "(use shell='cmd' for cmd.exe). Runs with the app's admin rights. Use for anything the file tools can't do: "
      "git, python, package managers, system info, process control.", "shell",
      {"command": {"type": "string"},
       "cwd": {"type": "string", "description": "Working folder (default: working directory)."},
       "timeout": {"type": "integer", "description": "Seconds (default 120, max 1800)."},
       "shell": {"type": "string", "enum": ["powershell", "cmd", "bash"]}},
      ["command"], summary=lambda a: a.get("command", "")[:120])
def run_command(ctx, command, cwd=None, timeout=120, shell=None):
    cwd = str(ctx.path(cwd or "."))
    timeout = max(1, min(int(timeout or 120), 1800))
    if IS_WIN:
        if shell == "cmd":
            args = ["cmd.exe", "/d", "/s", "/c", command]
        else:
            args = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command]
    else:
        args = ["bash", "-lc", command]
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace")
        return {"text": f"Timed out after {timeout}s.\n{out[-MAX_OUT:]}", "error": True}
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    text = out
    if err.strip():
        text += ("\n[stderr]\n" if out.strip() else "[stderr]\n") + err
    if len(text) > MAX_OUT:
        text = text[:MAX_OUT // 3] + f"\n... [{len(text) - MAX_OUT:,} chars cut] ...\n" + text[-2 * MAX_OUT // 3:]
    return {"text": f"exit code {p.returncode}\n{text.strip() or '(no output)'}", "error": p.returncode != 0}
