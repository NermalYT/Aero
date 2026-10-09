"""Shell tool: PowerShell on Windows, bash (or sh where bash is missing) on macOS and Linux."""
import subprocess

from . import tool
from .. import osinfo
from ..config import IS_WIN

_NO_WINDOW = osinfo.NO_WINDOW
MAX_OUT = 30000

if IS_WIN:
    _DESC = ("Run a shell command and return its output. This is PowerShell (use shell='cmd' for cmd.exe). Runs with "
             "the app's admin rights. ")
    _SHELLS = ["powershell", "cmd"]
else:
    _DESC = (f"Run a shell command in {osinfo.shell_name()} on {osinfo.name()} and return its output. Runs as the "
             "current user (use sudo only if the user asked and it works without a password prompt). ")
    _SHELLS = ["bash", "sh"]


@tool("run_command", _DESC + "Use for anything the file tools can't do: git, python, package managers, system info, "
      "process control.", "shell",
      {"command": {"type": "string"},
       "cwd": {"type": "string", "description": "Working folder (default: working directory)."},
       "timeout": {"type": "integer", "description": "Seconds (default 120, max 1800)."},
       "shell": {"type": "string", "enum": _SHELLS}},
      ["command"], summary=lambda a: a.get("command", "")[:120])
def run_command(ctx, command, cwd=None, timeout=120, shell=None):
    cwd = str(ctx.path(cwd or "."))
    timeout = max(1, min(int(timeout or 120), 1800))
    args = osinfo.shell_argv(command, shell)
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
