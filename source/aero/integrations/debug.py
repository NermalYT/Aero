"""Debug & reverse workspace: EpicGames/raddebugger (MIT) and morluto/rea (MIT) built into Aero.

RAD Debugger (Windows x64, alpha):
  debuginfo_dump / debuginfo_convert / symbolize   its headless converter (`raddbg --bin`): dump PE/PDB/ELF/RDI,
                                                    convert debug info to .rdi, map a module offset to a source line
  debug_start / debug_command / debug_state / debug_eval / debug_stop
                                                    a debugging session Aero drives over raddbg's IPC socket (launch,
                                                    breakpoints, run, step, state, evaluate). The debugger's own window
                                                    opens on the desktop: RAD Debugger has no headless engine, and
                                                    Aero shows the state it reads back in the Debug workspace.
  IPC safety: raddbg's IPC listener has no authentication and binds every network interface (INADDR_ANY in
  src/win32/socket/win32_socket.c; --ipc_addr only applies to the sending side). Aero runs a session only while
  Windows Firewall keeps the network away from raddbg.exe (firewall on in every profile, incoming connections blocked
  by default, no rule letting raddbg.exe in), checks that again before every command, and talks to it over
  127.0.0.1, which the firewall doesn't filter. Aero never changes firewall rules itself.

rea (npm `rea-agents`): a reverse-engineering MCP server (JavaScript/Electron apps, .NET, APKs, HAR files, WASM,
native binaries through Ghidra/Hopper/IDA when the user has them). Installing it registers it as an MCP server in
data/mcp.json, so its tools appear as mcp_rea_* with Aero's MCP permission; the reverse-engineer-anything skill
comes with it.
"""
import os
import random
import re
import socket
import struct
import subprocess
import time
from pathlib import Path

from . import Pack, Requirement, register, toolchain as tc
from .. import action_results, capabilities
from ..config import DATA, IS_WIN
from ..tools import tool

STATE = DATA / "integrations" / "debug"
RAD = tc.ROOT / "raddbg" / "raddbg.exe"
MAGIC = 0x72726300
_session = {"proc": None, "port": None, "target": None, "log": [], "fw_at": 0.0}
EXPOSED = ("RAD Debugger's control port has no authentication and listens on every network interface, so Aero only "
           "runs a session while Windows Firewall keeps the network away from it: ")
FIREWALL_PS = (
    "$ErrorActionPreference='Stop';"
    "$open=@(Get-NetFirewallProfile | Where-Object { $_.Enabled -ne 'True' -or $_.DefaultInboundAction -eq 'Allow' })"
    ".Count;"
    "$allow=@(Get-NetFirewallApplicationFilter -All | Where-Object { "
    "[Environment]::ExpandEnvironmentVariables($_.Program) -eq $env:AERO_RADDBG } | Get-NetFirewallRule | "
    "Where-Object { $_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' }).Count;"
    "Write-Output \"$open $allow\"")


def install_rad(emit=None, cancel=None):
    if not IS_WIN:
        raise RuntimeError("RAD Debugger releases are Windows-only (Linux support is preliminary, build from source).")
    rel = tc.github_release("EpicGames/raddebugger")
    a = next((a for a in rel["assets"] if a["name"] == "raddbg.exe"), None)
    if not a:
        raise RuntimeError(f"raddebugger {rel['tag']} has no raddbg.exe asset.")
    tc.download(a["url"], RAD, emit, cancel, sha256=a.get("digest"), label="raddbg.exe")
    tc.mark("raddbg", version=rel["tag"])
    return f"RAD Debugger {rel['tag']}"


def _rad_ok():
    if not IS_WIN:
        return False, "Windows only"
    return RAD.exists(), (tc.installed().get("raddbg", {}).get("version") or "installed") if RAD.exists() else "not installed"


def _bin(args, timeout=300, cancel=None):
    if not RAD.exists():
        raise RuntimeError("RAD Debugger isn't installed (integration_install raddebugger).")
    return tc.run([str(RAD), "--bin", *args], timeout=timeout, cancel=cancel)


# ------------------------------------------------------------------------------------------------ IPC

def frame(text):
    b = text.encode("utf-8")
    return struct.pack("<IQ", MAGIC, len(b)) + b


def unframe(data):
    """[(text)] from a reply buffer; outputs are NUL-separated inside each packet."""
    out, i = [], 0
    while i + 12 <= len(data):
        magic, n = struct.unpack_from("<IQ", data, i)
        if magic != MAGIC:
            break
        out += [s for s in data[i + 12:i + 12 + n].decode("utf-8", "replace").split("\0") if s]
        i += 12 + n
    return out


def firewall_closed():
    """(True, "") when Windows Firewall keeps other machines away from raddbg.exe's listener; else (False, why)."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", FIREWALL_PS],
                           capture_output=True, text=True, timeout=30, creationflags=tc.NO_WINDOW,
                           env={**os.environ, "AERO_RADDBG": str(RAD)})
        open_profiles, allow = (int(x) for x in r.stdout.split()[-2:])
    except Exception:  # noqa: BLE001
        return False, "Aero couldn't read the Windows Firewall settings"
    if open_profiles:
        return False, "Windows Firewall is off, or lets incoming connections in by default, in at least one profile"
    if allow:
        return False, ("Windows Firewall has a rule letting raddbg.exe accept incoming connections (usually from "
                       "choosing Allow when Windows asked). Remove it in Windows Security > Firewall & network "
                       "protection > Allow an app through firewall, or in an administrator PowerShell: "
                       f"Get-NetFirewallApplicationFilter -Program '{RAD}' | Get-NetFirewallRule | "
                       "Remove-NetFirewallRule")
    return True, ""


def _guard():
    """Re-check the firewall (at most every 2 s) and end the session when the debugger became reachable."""
    if time.time() - _session.get("fw_at", 0) < 2:
        return
    ok, why = firewall_closed()
    if not ok:
        stop()
        raise RuntimeError(EXPOSED + why + ". Aero ended the session.")
    _session["fw_at"] = time.time()


def ipc(cmd, timeout=10, check=True):
    port = _session.get("port")
    if not port:
        raise RuntimeError("No debugging session (debug_start first).")
    if check:
        _guard()
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(frame(cmd))
        s.settimeout(timeout)
        buf = b""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if len(buf) >= 12:
                _m, n = struct.unpack_from("<IQ", buf, 0)
                if len(buf) >= 12 + n:
                    break
    return "\n".join(unframe(buf))


def _free_port():
    for _ in range(20):
        p = random.randint(42000, 48999)
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise RuntimeError("no free port")


def _listen_addr(port):
    """The address raddbg listens on for port (netstat), to refuse a session exposed to the network."""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=10,
                             creationflags=tc.NO_WINDOW).stdout
    except Exception:  # noqa: BLE001
        return None
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) >= 4 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
            return parts[1].rsplit(":", 1)[0]
    return None


def parse_state(text):
    """The useful parts of raddbg's `state` reply (Metadesk-like text) as a dict; raw text kept."""
    d = {"raw": text[:8000]}
    for key in ("running", "run_gen", "stop_count", "ip", "module", "symbol", "voff"):
        m = re.search(rf"\b{key}\s*[:=]\s*([^\s,}}]+)", text)
        if m:
            d[key] = m.group(1).strip("'\"")
    m = re.search(r"stop_event\s*[:=]\s*\{([^}]*)\}", text, re.S)
    if m:
        d["stop_event"] = " ".join(m.group(1).split())[:400]
    return d


def start(target, args=(), breakpoints=(), cancel=None):
    if not RAD.exists():
        raise RuntimeError("RAD Debugger isn't installed (integration_install raddebugger).")
    stop()
    ok, why = firewall_closed()
    if not ok:
        raise RuntimeError(EXPOSED + why + ".")
    port = _free_port()
    STATE.mkdir(parents=True, exist_ok=True)
    proj = STATE / "aero.raddbg_project"
    cmd = [str(RAD), f"--ipc_port={port}", f"--project:{proj}",
           f"--user:{STATE / 'aero.raddbg_user'}", str(target), *map(str, args)]
    p = subprocess.Popen(cmd, cwd=str(Path(target).parent), creationflags=0)
    _session.update(proc=p, port=port, target=str(target), log=[], fw_at=time.time())
    for _ in range(50):
        time.sleep(0.2)
        if _listen_addr(port):
            break
    else:
        stop()
        raise RuntimeError("RAD Debugger didn't open its IPC port.")
    for bp in breakpoints or ():
        _session["log"].append(("add_breakpoint", ipc(f"add_breakpoint {bp}")))
    return port


def stop():
    p = _session.get("proc")
    if p and p.poll() is None:
        try:
            ipc("exit", timeout=3, check=False)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
        if p.poll() is None:
            tc.kill(p)
    _session.update(proc=None, port=None)


@tool("debuginfo_dump", "Dump a binary's or debug-info file's structure with RAD Debugger's converter (PE/COFF, PDB, "
      "ELF, RDI): sections, modules, symbols. only: comma-separated subsets to keep the output small.", "integrations",
      {"path": {"type": "string"}, "only": {"type": "string"}}, ["path"], summary=lambda a: a.get("path", ""),
      available=lambda: IS_WIN)
def debuginfo_dump(ctx, path, only=""):
    p = ctx.path(path)
    try:
        r = _bin(["--dump"] + ([f"--only:{only}"] if only else []) + [str(p)], cancel=ctx.cancel)
    except RuntimeError as e:
        return {"text": str(e), "error": True}
    return {"text": (r["out"] or r["err"])[:40000], "error": r["rc"] != 0}


@tool("debuginfo_convert", "Convert debug info (PDB, PE, ELF/DWARF) to RAD's .rdi format, or emit Breakpad symbols "
      "(format='breakpad').", "dev", {"path": {"type": "string"}, "out": {"type": "string"},
                                       "format": {"type": "string", "enum": ["rdi", "breakpad"]}}, ["path"],
      summary=lambda a: a.get("path", ""), available=lambda: IS_WIN)
def debuginfo_convert(ctx, path, out="", format="rdi"):
    p = ctx.path(path)
    o = ctx.path(out) if out else p.with_suffix(".rdi" if format == "rdi" else ".sym")
    try:
        r = _bin([f"--{format}", f"--out:{o}", str(p)], timeout=1800, cancel=ctx.cancel)
    except RuntimeError as e:
        return {"text": str(e), "error": True}
    ok = o.exists() and o.stat().st_size > 0
    env = action_results.make("file", verified=ok, method=f"{o.name} {'exists' if ok else 'missing'}",
                              changed={"path": str(o)})
    return action_results.attach({"text": f"{'Wrote' if ok else 'Failed to write'} {o}\n{(r['out'] + r['err'])[-1500:]}",
                                  "error": not ok}, env)


@tool("symbolize", "Map an offset inside a module (voff, hex like 0x1a2b) to its source file and line, using the "
      "module's debug info (PDB/RDI).", "integrations",
      {"path": {"type": "string", "description": "The .exe/.dll/.pdb/.rdi"}, "voff": {"type": "string"}},
      ["path", "voff"], summary=lambda a: f"{a.get('path', '')} @ {a.get('voff', '')}", available=lambda: IS_WIN)
def symbolize(ctx, path, voff):
    try:
        r = _bin(["--voff2line", f"--voff:{voff}", str(ctx.path(path))], cancel=ctx.cancel)
    except RuntimeError as e:
        return {"text": str(e), "error": True}
    return {"text": (r["out"] or r["err"]).strip()[:4000] or "No line found.", "error": r["rc"] != 0}


@tool("debug_start", "Start debugging a Windows program in RAD Debugger, driven by Aero: breakpoints as "
      "'file.c:123' or a function name. The debugger window opens on the desktop; Aero reads its state back. Then "
      "use debug_command (run, step_over, step_into, step_out, continue, halt, add_breakpoint <loc>, run_to_line "
      "<file:line>), debug_state and debug_eval.", "dev",
      {"target": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}},
       "breakpoints": {"type": "array", "items": {"type": "string"}}}, ["target"],
      summary=lambda a: a.get("target", ""), available=lambda: IS_WIN)
def debug_start(ctx, target, args=None, breakpoints=None):
    t = ctx.path(target)
    if not t.exists():
        return {"text": f"No program at {t}.", "error": True}
    try:
        port = start(t, args or (), breakpoints or ())
    except RuntimeError as e:
        return {"text": str(e), "error": True}
    return (f"RAD Debugger is debugging {t} (IPC on 127.0.0.1:{port}); breakpoints: "
            f"{', '.join(breakpoints or []) or 'none'}. Use debug_command 'run' to start it.")


SAFE_CMDS = re.compile(r"^(run|restart|halt|kill|continue|step_(into|over|out)(_line|_inst)?|run_to_(line|name)|"
                       r"add_breakpoint|toggle_breakpoint|enable_breakpoint|disable_breakpoint|clear_breakpoints|"
                       r"set_next_statement|select_thread|up_one_frame|down_one_frame|toggle_watch_expr|"
                       r"break_when_value_changes|find_code_location|goto_name|load_debug_info)\b")


@tool("debug_command", "Send one command to the running RAD Debugger session (see debug_start for the list).", "dev",
      {"command": {"type": "string"}}, ["command"], summary=lambda a: a.get("command", ""), available=lambda: IS_WIN)
def debug_command(ctx, command):
    c = (command or "").strip()
    if not SAFE_CMDS.match(c):
        return {"text": f"'{c.split()[0] if c else ''}' isn't one of the debugging commands Aero sends.", "error": True}
    try:
        out = ipc(c)
        time.sleep(0.3)
        st = parse_state(ipc("state"))
    except (RuntimeError, OSError) as e:
        return {"text": f"The debugging session isn't reachable: {e}", "error": True}
    _session["log"].append((c, out))
    (STATE / "last_state.txt").write_text(st["raw"], encoding="utf-8")
    return f"{c}: {out or 'ok'}\nNow: " + ", ".join(f"{k}={v}" for k, v in st.items() if k != "raw")


@tool("debug_state", "The debugged program's state: running or stopped, where (module, symbol, source line), the stop "
      "reason, locals, threads and modules.", "integrations", {}, summary=lambda a: "state", available=lambda: IS_WIN)
def debug_state(ctx):
    try:
        raw = ipc("state")
    except (RuntimeError, OSError) as e:
        return {"text": str(e), "error": True}
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "last_state.txt").write_text(raw, encoding="utf-8")
    return raw[:12000]


@tool("debug_eval", "Evaluate an expression in the stopped program (variables, fields, pointers, casts).",
      "integrations", {"expr": {"type": "string"}}, ["expr"], summary=lambda a: a.get("expr", ""),
      available=lambda: IS_WIN)
def debug_eval(ctx, expr):
    try:
        return ipc(f"eval {expr}")[:8000] or "(no value)"
    except (RuntimeError, OSError) as e:
        return {"text": str(e), "error": True}


@tool("debug_stop", "End the debugging session and close RAD Debugger.", "dev", {}, summary=lambda a: "stop",
      available=lambda: IS_WIN)
def debug_stop(ctx):
    stop()
    return "Debugging session ended."


# ------------------------------------------------------------------------------------------------ rea

def install_rea(emit=None, cancel=None):
    ok, det = tc.node_ok(22)
    if not ok:
        raise RuntimeError(det + ". rea needs Node.js 22.19+ (Code workspace: toolchain node).")
    tc.npm_install("rea", ["rea-agents"], emit, cancel)
    from ..tools import mcp_client
    cfg = mcp_client.read_config()
    node = tc.which("node")
    entry = tc.npm_prefix("rea") / "node_modules" / "rea-agents"
    pj = entry / "package.json"
    import json as _json
    binrel = _json.loads(pj.read_text(encoding="utf-8")).get("bin", {})
    script = binrel.get("rea-agents") if isinstance(binrel, dict) else binrel
    cfg.setdefault("mcpServers", {})["rea"] = {"command": node, "args": [str(entry / script), "mcp"],
                                               "env": {"DO_NOT_TRACK": "1"}, "aero_pack": "rea"}
    mcp_client.write_config(cfg)
    try:
        mcp_client.start_one("rea")
    except Exception:  # noqa: BLE001
        pass
    tc.mark("rea", version="rea-agents (npm)")
    return "rea-agents, registered as the MCP server 'rea'"


def _rea_ok():
    from ..tools import mcp_client
    on = "rea" in (mcp_client.read_config().get("mcpServers") or {})
    return on, "MCP server 'rea' registered" if on else "not installed"


capabilities.register("debuginfo_dump", ["debug.inspect"], "local_read")
capabilities.register("debuginfo_convert", ["debug.convert"], "local_write")
capabilities.register("symbolize", ["debug.symbolize"], "local_read")
for _n in ("debug_start", "debug_command", "debug_stop"):
    capabilities.register(_n, ["debug.control"], "local_app_write", cost="medium")
for _n in ("debug_state", "debug_eval"):
    capabilities.register(_n, ["debug.inspect"], "local_read")


def _panel():
    p = STATE / "last_state.txt"
    return {"session": {"target": _session.get("target"), "port": _session.get("port"),
                        "running": bool(_session.get("proc") and _session["proc"].poll() is None)},
            "state": parse_state(p.read_text(encoding="utf-8")) if p.exists() else None}


register(Pack("raddebugger", "RAD Debugger", "debug",
              "Windows debugging driven by the agent (breakpoints, stepping, state, eval) and debug-info tools",
              "https://github.com/EpicGames/raddebugger", "MIT",
              triggers=[r"\b(debug|debugger|breakpoint|segfault|access violation|crash(es|ed)?|stack trace|minidump|"
                        r"\.dmp|pdb|symboli[sz]e)\b"],
              tools=["debuginfo_dump", "debuginfo_convert", "symbolize", "debug_start", "debug_command", "debug_state",
                     "debug_eval", "debug_stop"],
              requirements=[Requirement("raddbg", "RAD Debugger (Windows x64, about 5 MB)", _rad_ok,
                                        install=install_rad, size_mb=5)],
              panel=_panel, stop=stop,
              notes="RAD Debugger is alpha and Windows-only; its window opens on the desktop during a session (there is "
                    "no headless engine), Aero reads the state back. Its control port listens on every network "
                    "interface without authentication: sessions run only while Windows Firewall blocks raddbg.exe "
                    "from the network (if Windows asks when it starts, choose Cancel)."))
register(Pack("rea", "rea", "debug", "Reverse engineering over MCP: JS/Electron apps, .NET, APKs, HAR, WASM, binaries",
              "https://github.com/morluto/rea", "MIT",
              triggers=[r"\b(reverse[- ]engineer\w*|decompil\w*|disassembl\w*|\.apk|\.asar|electron app|har file|wasm|"
                        r"\.net assembly|how does .{1,40} (work|implement))\b"],
              tools=["use_skill"], skills=["reverse-engineer-anything"], mcp_server="rea",
              requirements=[Requirement("node", "Node.js 22.19+", lambda: tc.node_ok(22),
                                        how="install Node.js (Code workspace: toolchain node)"),
                            Requirement("rea", "rea-agents MCP server (npm)", _rea_ok, install=install_rea, size_mb=150)],
              notes="Native analysis needs Ghidra, Hopper or IDA, which the user installs; several features are "
                    "Linux-only."))
