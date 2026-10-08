"""Local-only inference contract and strict offline mode.

The contract (always on): every token Aero generates comes from a llama-server process it started itself from a
local GGUF file, bound to 127.0.0.1. There is no hosted inference engine anywhere in the code path; the only cloud
model use is the optional ChatGPT and Claude reviews, which the user turns on per chat or in Settings.

Strict offline (Settings > Privacy & offline, off by default) goes further and stops Aero's own code from reaching
the network at all:
  - every outbound HTTP request made inside this process is checked at the httpx transport, which is the one choke
    point all of Aero's network code goes through (web tools, Hugging Face, GitHub, remote MCP servers, the
    Anthropic and OpenAI SDKs). Loopback stays allowed; anything else raises OfflineBlocked.
  - cloud reviews and the Claude Code and Codex CLI bridges are refused before they start a subprocess.
  - web, browser and MCP tools are hidden from the models and refused if called anyway.
  - MCP servers that run as separate programs are not started unless their entry says "offline_ok": true.
  - llama-server always gets --offline (when the build has it), so it never tries to fetch anything itself.

Every non-loopback request (allowed or blocked) is written to data/audit/network.jsonl with the time, method, host
and outcome. Paths and query strings are never written: a search query or a file name is the user's business.
"""
import ipaddress
import json
import threading
import time

import httpx

from .config import DATA, load_settings

AUDIT_DIR = DATA / "audit"
AUDIT = AUDIT_DIR / "network.jsonl"
AUDIT_MAX_BYTES = 1_000_000
NETWORK_TOOL_CATEGORIES = ("web", "browser", "mcp")
_lock = threading.Lock()
_installed = {"done": False}


class OfflineBlocked(RuntimeError):
    pass


def strict():
    try:
        return bool(load_settings().get("strict_offline"))
    except Exception:
        return False


def is_loopback(host):
    h = (host or "").strip("[]").lower()
    if h in ("localhost", "ip6-localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def audit(method, host, allowed, why=""):
    rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "method": method, "host": host, "allowed": allowed}
    if why:
        rec["why"] = why
    try:
        with _lock:
            AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            if AUDIT.exists() and AUDIT.stat().st_size > AUDIT_MAX_BYTES:
                AUDIT.replace(AUDIT.with_suffix(".1.jsonl"))
            with open(AUDIT, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def recent(n=60):
    try:
        lines = AUDIT.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except (OSError, ValueError):
        return []


def check(method, host, what="network"):
    """Raise OfflineBlocked for a non-loopback destination while strict offline is on; audit the attempt."""
    if is_loopback(host):
        return
    if strict():
        audit(method, host, False, what)
        raise OfflineBlocked(f"Strict offline mode is on, so Aero did not contact {host}. "
                             "Turn it off in Settings > Privacy & offline to use this feature.")
    audit(method, host, True, what)


def guard(feature):
    """For features that leave the machine without going through httpx in this process (Claude Code CLI, the
    automated browser, MCP server programs)."""
    if strict():
        audit("-", feature, False, "feature")
        raise OfflineBlocked(f"Strict offline mode is on, so {feature} is turned off. "
                             "Turn it off in Settings > Privacy & offline to use it.")


def install():
    """Wrap httpx's transports once. Every httpx.Client / AsyncClient (and SDKs built on them) ends up here."""
    if _installed["done"]:
        return
    _installed["done"] = True
    sync_send = httpx.HTTPTransport.handle_request
    async_send = httpx.AsyncHTTPTransport.handle_async_request

    def handle_request(self, request):
        check(request.method, request.url.host)
        return sync_send(self, request)

    async def handle_async_request(self, request):
        check(request.method, request.url.host)
        return await async_send(self, request)

    httpx.HTTPTransport.handle_request = handle_request
    httpx.AsyncHTTPTransport.handle_async_request = handle_async_request


def tool_allowed(category, settings=None):
    s = settings if settings is not None else load_settings()
    return not (s.get("strict_offline") and category in NETWORK_TOOL_CATEGORIES)


# ---- status ---------------------------------------------------------------------------------------------

def _listeners(pids):
    """Listening TCP sockets owned by these processes: [(pid, ip, port)]."""
    out = []
    try:
        import psutil
        for c in psutil.net_connections(kind="tcp"):
            if c.pid in pids and c.status == psutil.CONN_LISTEN and c.laddr:
                out.append((c.pid, c.laddr.ip, c.laddr.port))
    except Exception:                       # needs admin on some systems; reported as "not checked"
        return None
    return out


def status(engine_procs):
    """engine_procs: {label: pid or None} for the llama-server children (main, router, trial, advisor)."""
    import os
    s = load_settings()
    pids = {os.getpid(): "Aero UI server"}
    for label, pid in engine_procs.items():
        if pid:
            pids[pid] = label
    socks = _listeners(set(pids))
    listeners = None
    if socks is not None:
        listeners = [{"process": pids.get(pid, str(pid)), "address": ip, "port": port, "loopback": is_loopback(ip)}
                     for pid, ip, port in socks]
    on = bool(s.get("strict_offline"))
    blocked = recent(400)
    return {
        "strict_offline": on,
        "engine": "llama.cpp llama-server started by Aero from local GGUF files, bound to 127.0.0.1",
        "hosted_engines": [],
        "listeners": listeners,
        "listeners_note": None if listeners is not None else
        "Could not read the socket table (Windows hides other processes' sockets unless Aero runs as admin).",
        "all_loopback": None if listeners is None else all(x["loopback"] for x in listeners),
        "disabled": ([
            "ChatGPT and Claude reviews (GPT-6 Astra, GPT-6.1 Sol, Claude Fable, Claude Opus)",
            "Web search, page fetch and the automated browser",
            "GitHub and other MCP servers (except ones marked offline_ok in mcp.json)",
            "Hugging Face search and downloads (the tuner's advisor uses an already-downloaded model or none)",
        ] if on else []),
        "not_covered": [
            "The shell tool runs PowerShell, which can reach the network if a command asks it to. Set the shell "
            "tool to 'ask' to approve each command, or use the firewall test in validation/Validate-Aero.ps1.",
            "Programs you start through Aero (apps, scripts) are outside its control.",
        ],
        "audit_recent": blocked[-60:],
        "audit_counts": {"allowed": sum(1 for r in blocked if r.get("allowed")),
                         "blocked": sum(1 for r in blocked if not r.get("allowed"))},
    }
