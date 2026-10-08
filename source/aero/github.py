"""Link the user's GitHub account: a token (from the GitHub CLI login or a pasted personal access token) is kept in
the encrypted vault, and GitHub's official MCP server is added to data/mcp.json so the models get GitHub tools
(repos, issues, pull requests, Actions, code search...)."""
import shutil
import subprocess
from pathlib import Path

import httpx

from . import vault
from .config import IS_WIN, load_settings
from .tools import mcp_client

TOKEN = "github_token"
REMOTE_URL = "https://api.githubcopilot.com/mcp/"
_NO_WINDOW = 0x08000000 if IS_WIN else 0


def _gh_exe():
    exe = shutil.which("gh")
    if exe:
        return exe
    for p in (r"C:\Program Files\GitHub CLI\gh.exe", r"C:\Program Files (x86)\GitHub CLI\gh.exe"):
        if Path(p).exists():
            return p
    return None


def whoami(token):
    r = httpx.get("https://api.github.com/user", headers={"Authorization": f"Bearer {token}",
                                                          "Accept": "application/vnd.github+json",
                                                          "User-Agent": "Aero/2.0"}, timeout=20)
    if r.status_code == 401:
        raise RuntimeError("GitHub rejected this token (401).")
    r.raise_for_status()
    j = r.json()
    return {"login": j.get("login"), "name": j.get("name"), "avatar": j.get("avatar_url"),
            "scopes": r.headers.get("x-oauth-scopes") or "fine-grained"}


def server_entry():
    s = load_settings()
    headers = {"Authorization": "Bearer ${secret:github_token}"}
    ts = (s.get("github_toolsets") or "").strip()
    if ts:
        headers["X-MCP-Toolsets"] = ts
    if s.get("github_read_only"):
        headers["X-MCP-Readonly"] = "true"
    return {"type": "http", "url": REMOTE_URL, "headers": headers}


def connect(token):
    token = (token or "").strip()
    if not token:
        raise RuntimeError("Paste a token first.")
    info = whoami(token)
    vault.put(TOKEN, token)
    vault.put_json("github_user", info)
    cfg = mcp_client.read_config()
    cfg.setdefault("mcpServers", {})["github"] = server_entry()
    mcp_client.write_config(cfg)
    mcp_client.start_one("github")
    return info


def connect_with_cli():
    exe = _gh_exe()
    if not exe:
        raise RuntimeError("GitHub CLI (gh) is not installed. Install it from https://cli.github.com or paste a token.")
    p = subprocess.run([exe, "auth", "token"], capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW)
    tok = (p.stdout or "").strip()
    if p.returncode != 0 or not tok:
        raise RuntimeError("GitHub CLI is not logged in. Run `gh auth login` in a terminal first. " + (p.stderr or "")[:200])
    return connect(tok)


def refresh_entry():
    """Re-write the server entry after the toolset / read-only settings change."""
    if not vault.has(TOKEN):
        return
    cfg = mcp_client.read_config()
    cfg.setdefault("mcpServers", {})["github"] = server_entry()
    mcp_client.write_config(cfg)
    mcp_client.start_one("github")


def disconnect():
    vault.put(TOKEN, "")
    vault.put("github_user", "")
    cfg = mcp_client.read_config()
    if cfg.get("mcpServers", {}).pop("github", None) is not None:
        mcp_client.write_config(cfg)
    mcp_client.start_one("github")


def status():
    return {"connected": vault.has(TOKEN), "user": vault.get_json("github_user", None),
            "cli": bool(_gh_exe()), "mcp": mcp_client.status().get("github")}
