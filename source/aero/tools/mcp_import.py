"""Find MCP servers configured in other apps on this PC so they can be imported into Aero with one click.

Sources: Claude Desktop, Claude Code (user + project scopes, and installed Claude Code plugins), Codex, Cursor,
VS Code. ChatGPT's apps/connectors run in OpenAI's cloud; the ones that are public MCP servers can be added by URL.
"""
import json
import os
import re
from pathlib import Path

from .mcp_client import read_config, write_config


def _load_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return None


def _norm(cfg, plugin_root=None):
    """Bring a server entry into Aero's shape (command/args/env or type/url/headers)."""
    if not isinstance(cfg, dict):
        return None
    c = {}
    if cfg.get("url") or cfg.get("serverUrl") or cfg.get("httpUrl"):
        c["type"] = "sse" if (cfg.get("type") or cfg.get("transport")) == "sse" else "http"
        c["url"] = cfg.get("url") or cfg.get("serverUrl") or cfg.get("httpUrl")
        if cfg.get("headers") or cfg.get("http_headers"):
            c["headers"] = cfg.get("headers") or cfg.get("http_headers")
    elif cfg.get("command"):
        c["command"] = cfg["command"]
        if cfg.get("args"):
            c["args"] = list(cfg["args"])
        if cfg.get("env"):
            c["env"] = dict(cfg["env"])
        if cfg.get("cwd"):
            c["cwd"] = cfg["cwd"]
    else:
        return None
    if plugin_root:
        txt = json.dumps(c).replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root).replace("\\", "\\\\"))
        c = json.loads(txt)
    return c


def _servers_in(obj):
    if not isinstance(obj, dict):
        return {}
    if isinstance(obj.get("mcpServers"), dict):
        return obj["mcpServers"]
    if isinstance(obj.get("servers"), dict):          # VS Code mcp.json
        return obj["servers"]
    if obj and all(isinstance(v, dict) and ("command" in v or "url" in v) for v in obj.values()):
        return obj                                     # plugin .mcp.json may be a bare {name: cfg} map
    return {}


def discover():
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    found = []

    def add(source, name, cfg, plugin_root=None):
        n = _norm(cfg, plugin_root)
        if n:
            found.append({"name": re.sub(r"[^\w.-]", "_", name), "source": source, "config": n})

    for p in (appdata / "Claude" / "claude_desktop_config.json", home / "Library/Application Support/Claude/claude_desktop_config.json"):
        for k, v in _servers_in(_load_json(p) or {}).items():
            add("Claude Desktop", k, v)
    cj = _load_json(home / ".claude.json") or {}
    for k, v in _servers_in(cj).items():
        add("Claude Code", k, v)
    for proj, pv in (cj.get("projects") or {}).items():
        for k, v in _servers_in(pv or {}).items():
            add(f"Claude Code ({Path(proj).name})", k, v)
    plugins = home / ".claude" / "plugins"
    if plugins.is_dir():
        for p in list(plugins.rglob(".mcp.json"))[:200]:
            if "node_modules" in p.parts:
                continue
            for k, v in _servers_in(_load_json(p) or {}).items():
                add(f"Claude Code plugin {p.parent.name}", k, v, p.parent)
        for p in list(plugins.rglob("plugin.json"))[:400]:
            j = _load_json(p) or {}
            root = p.parent.parent if p.parent.name == ".claude-plugin" else p.parent
            ms = j.get("mcpServers")
            if isinstance(ms, dict):
                for k, v in ms.items():
                    add(f"Claude Code plugin {j.get('name') or root.name}", k, v, root)
    codex = home / ".codex" / "config.toml"
    if codex.exists():
        try:
            import tomllib
            t = tomllib.loads(codex.read_text(encoding="utf-8"))
            for k, v in (t.get("mcp_servers") or {}).items():
                add("Codex", k, v)
        except Exception:
            pass
    for p in (home / ".cursor" / "mcp.json", appdata / "Code" / "User" / "mcp.json"):
        for k, v in _servers_in(_load_json(p) or {}).items():
            add("Cursor" if ".cursor" in str(p) else "VS Code", k, v)
    have = read_config().get("mcpServers", {})
    seen = set()
    out = []
    for f in found:
        key = (f["name"], json.dumps(f["config"], sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        f["exists"] = f["name"] in have
        out.append(f)
    return out


def import_servers(items):
    """items: [{"name", "config"}] (from discover). Existing names get a numeric suffix."""
    cfg = read_config()
    ms = cfg.setdefault("mcpServers", {})
    added = []
    for it in items:
        name = it["name"]
        base, i = name, 2
        while name in ms and ms[name] != it["config"]:
            name = f"{base}_{i}"
            i += 1
        if name not in ms:
            ms[name] = it["config"]
            added.append(name)
    write_config(cfg)
    return added
