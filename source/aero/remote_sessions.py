"""Is someone viewing or controlling this computer remotely right now? Only verified answers count.

Each provider reports what it can actually know, separately:
    installed   the remote-desktop program is on this computer
    running     its background service is running
    viewer      True: a remote viewer is connected right now / False: none / None: this provider can't tell
    confidence  verified (a documented OS or app interface said so) | probable (an undocumented sign) | unknown

A program or service merely running is never a viewer. Providers:

  rdp       Windows Remote Desktop, through the documented WTS API: the session Aero runs in (and every other
            session) with its connection state and client protocol (WTSClientProtocolType 2 = RDP). Verified.
  logind    Linux: systemd-logind's Remote property of the session Aero runs in (set for xrdp, SSH-forwarded and
            other remote logins), or xrdp's XRDP_SESSION variable. Verified when logind answers.
  rustdesk  RustDesk has no documented way for another program to learn that a viewer is connected (checked
            2026-10-09, see docs/V1.1_OPEN_SOURCE_RESEARCH.md). Aero reports installed / service running, and the
            connection-manager window process (rustdesk --cm) that RustDesk shows during a session as a *probable*
            viewer, which only counts when Settings allow probable signals. A verified event can be pushed in by an
            integration through push_event().
  others    AnyDesk, Parsec, Chrome Remote Desktop, Sunshine, TeamViewer and VNC servers: installed / running only;
            viewer state unknown (never treated as active or as disconnected).

Overall state: remote_active (a counted viewer is connected), remote_connecting, remote_disconnected (a counted
viewer just left), local (providers that can tell say nobody is connected) or unknown (nothing can tell).
"""
import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time

IS_WIN = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
STATES = ("local", "remote_connecting", "remote_active", "remote_reconnecting", "remote_disconnected", "unknown")

# Windows WTS constants (wtsapi32.h)
WTS_CURRENT_SERVER_HANDLE = 0
WTSActive, WTSConnected, WTSConnectQuery, WTSShadow, WTSDisconnected, WTSIdle, WTSListen, WTSReset, WTSDown, \
    WTSInit = range(10)
WTSUserName, WTSClientName, WTSConnectState, WTSClientProtocolType = 5, 10, 8, 16
_CONNECT_STATE = {0: "active", 1: "connected", 2: "connect_query", 3: "shadow", 4: "disconnected", 5: "idle",
                  6: "listen", 7: "reset", 8: "down", 9: "init"}

OTHER_PROGRAMS = {
    "anydesk": ("AnyDesk", ("anydesk.exe", "anydesk")),
    "parsec": ("Parsec", ("parsecd.exe", "parsecd")),
    "chrome_remote_desktop": ("Chrome Remote Desktop", ("remoting_host.exe", "chrome-remote-desktop-host")),
    "sunshine": ("Sunshine", ("sunshine.exe", "sunshine")),
    "teamviewer": ("TeamViewer", ("teamviewer.exe", "teamviewerd", "teamviewer_service.exe")),
    "vnc": ("VNC server", ("winvnc.exe", "tvnserver.exe", "vncserver-x11-core", "x11vnc", "Xvnc")),
}

_pushed = {}          # provider -> {"viewer": bool, "at": t, "session": str, "verified": True}
_lock = threading.Lock()


def push_event(provider, connected, session="", verified=True):
    """For integrations that receive a real connect/disconnect event from a remote-desktop app (and for tests)."""
    with _lock:
        _pushed[provider] = {"viewer": bool(connected), "at": time.time(), "session": str(session)[:80],
                             "verified": bool(verified)}


def clear_pushed():
    with _lock:
        _pushed.clear()


# ---- Windows RDP ------------------------------------------------------------------------------------------

def _wts():
    w = ctypes.WinDLL("wtsapi32")
    from ctypes import wintypes as wt

    class WTS_SESSION_INFOW(ctypes.Structure):
        _fields_ = [("SessionId", wt.DWORD), ("pWinStationName", wt.LPWSTR), ("State", ctypes.c_int)]
    w.WTSEnumerateSessionsW.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.POINTER(WTS_SESSION_INFOW)),
                                        ctypes.POINTER(wt.DWORD)]
    w.WTSEnumerateSessionsW.restype = wt.BOOL
    w.WTSQuerySessionInformationW.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p),
                                              ctypes.POINTER(wt.DWORD)]
    w.WTSQuerySessionInformationW.restype = wt.BOOL
    w.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    return w, WTS_SESSION_INFOW


def _query(w, sid, cls):
    from ctypes import wintypes as wt
    buf, n = ctypes.c_void_p(), wt.DWORD()
    if not w.WTSQuerySessionInformationW(WTS_CURRENT_SERVER_HANDLE, sid, cls, ctypes.byref(buf), ctypes.byref(n)):
        return None
    try:
        if cls == WTSClientProtocolType:
            return ctypes.cast(buf, ctypes.POINTER(ctypes.c_ushort)).contents.value
        if cls == WTSConnectState:
            return ctypes.cast(buf, ctypes.POINTER(ctypes.c_int)).contents.value
        return ctypes.wstring_at(buf.value) if buf.value else ""
    finally:
        w.WTSFreeMemory(buf)


def rdp_sessions():
    """[{id, station, state, protocol (0 console, 1 ICA, 2 RDP), mine}] from the WTS API."""
    if not IS_WIN:
        return []
    w, INFO = _wts()
    from ctypes import wintypes as wt
    p, n = ctypes.POINTER(INFO)(), wt.DWORD()
    if not w.WTSEnumerateSessionsW(WTS_CURRENT_SERVER_HANDLE, 0, 1, ctypes.byref(p), ctypes.byref(n)):
        return []
    mine = wt.DWORD()
    ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(mine))
    out = []
    try:
        for i in range(n.value):
            s = p[i]
            proto = _query(w, s.SessionId, WTSClientProtocolType)
            out.append({"id": int(s.SessionId), "station": s.pWinStationName or "", "state": _CONNECT_STATE.get(s.State,
                        str(s.State)), "protocol": proto, "mine": s.SessionId == mine.value})
    finally:
        w.WTSFreeMemory(p)
    return out


def _rdp(sessions=None):
    if not IS_WIN and sessions is None:
        return {"name": "rdp", "label": "Windows Remote Desktop", "available": False}
    ss = rdp_sessions() if sessions is None else sessions
    remote = [s for s in ss if s.get("protocol") == 2 and s["state"] in ("active", "shadow")]
    connecting = [s for s in ss if s.get("protocol") == 2 and s["state"] in ("connected", "connect_query", "init")]
    dropped = [s for s in ss if s.get("protocol") == 2 and s["state"] == "disconnected"]
    viewer = bool(remote)
    detail = ("RDP session " + ", ".join(f"{s['id']} ({s['station'] or 'session'}{', Aero runs here' if s['mine'] else ''})"
                                         for s in remote)) if remote else \
        ("connecting" if connecting else "no RDP client connected")
    return {"name": "rdp", "label": "Windows Remote Desktop", "available": True, "installed": True, "running": None,
            "viewer": viewer, "connecting": bool(connecting) and not viewer, "disconnected_sessions": len(dropped),
            "confidence": "verified", "detail": detail, "sessions": [s["id"] for s in remote]}


# ---- Linux logind ------------------------------------------------------------------------------------------

def _logind(run=None):
    if not IS_LINUX and run is None:
        return {"name": "logind", "label": "Linux remote login (logind / xrdp)", "available": False}
    run = run or (lambda args: subprocess.run(args, capture_output=True, text=True, timeout=4))
    sid = os.environ.get("XDG_SESSION_ID", "")
    if os.environ.get("XRDP_SESSION"):
        return {"name": "logind", "label": "Linux remote login (logind / xrdp)", "available": True, "installed": True,
                "running": True, "viewer": True, "confidence": "verified", "detail": "xrdp session (XRDP_SESSION)"}
    if not sid or not shutil.which("loginctl") and run is None:
        return {"name": "logind", "label": "Linux remote login (logind / xrdp)", "available": False,
                "viewer": None, "confidence": "unknown", "detail": "no logind session id"}
    try:
        r = run(["loginctl", "show-session", sid, "-p", "Remote", "-p", "State", "-p", "Type"])
        props = dict(line.split("=", 1) for line in (r.stdout or "").splitlines() if "=" in line)
    except Exception:
        props = {}
    if "Remote" not in props:
        return {"name": "logind", "label": "Linux remote login (logind / xrdp)", "available": True, "viewer": None,
                "confidence": "unknown", "detail": "logind did not answer"}
    viewer = props.get("Remote") == "yes" and props.get("State", "active") == "active"
    return {"name": "logind", "label": "Linux remote login (logind / xrdp)", "available": True, "installed": True,
            "running": True, "viewer": viewer, "confidence": "verified",
            "detail": f"session {sid}: Remote={props.get('Remote')}, State={props.get('State')}"}


# ---- RustDesk and other programs -------------------------------------------------------------------------------

def _procs():
    try:
        import psutil
    except ImportError:
        return []
    out = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            out.append(((p.info.get("name") or "").lower(), [str(x) for x in (p.info.get("cmdline") or [])]))
        except Exception:
            continue
    return out


def _rustdesk_installed():
    if IS_WIN:
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
            if base and os.path.exists(os.path.join(base, "RustDesk", "rustdesk.exe")):
                return True
        return False
    return bool(shutil.which("rustdesk")) or os.path.exists("/Applications/RustDesk.app")


def _rustdesk(procs, installed=None):
    rd = [(n, c) for n, c in procs if n.startswith("rustdesk")]
    service = any("--service" in c or "--server" in c for _n, c in rd)
    cm = any("--cm" in c for _n, c in rd)
    installed = _rustdesk_installed() if installed is None else installed
    with _lock:
        pushed = dict(_pushed.get("rustdesk") or {})
    base = {"name": "rustdesk", "label": "RustDesk", "available": True, "installed": installed or bool(rd),
            "running": service or bool(rd)}
    if pushed and time.time() - pushed["at"] < 3600:
        return {**base, "viewer": pushed["viewer"], "confidence": "verified" if pushed["verified"] else "probable",
                "detail": "event from an integration" + (f" ({pushed['session']})" if pushed["session"] else "")}
    if cm:
        return {**base, "viewer": True, "confidence": "probable",
                "detail": "RustDesk's connection window (rustdesk --cm) is open, which RustDesk shows during an incoming "
                          "session. RustDesk documents no way to confirm a viewer, so this is not verified."}
    return {**base, "viewer": None if base["running"] else False, "confidence": "unknown",
            "detail": "service running; RustDesk doesn't tell other programs whether a viewer is connected"
            if base["running"] else ("installed, not running" if base["installed"] else "not installed")}


def _others(procs):
    names = {n for n, _c in procs}
    out = []
    for key, (label, exes) in OTHER_PROGRAMS.items():
        running = any(e.lower() in names for e in exes)
        if running:
            out.append({"name": key, "label": label, "available": True, "installed": True, "running": True,
                        "viewer": None, "confidence": "unknown",
                        "detail": "running; Aero has no verified way to see its viewers"})
    return out


# ---- the detector ----------------------------------------------------------------------------------------------

def snapshot(settings=None, procs=None, rdp_list=None, logind_run=None, rustdesk_installed=None):
    """One reading of every provider and the overall state (without memory of earlier readings)."""
    s = settings or {}
    enabled = {"rdp": True, "rustdesk": True, "logind": True, **(s.get("remote_providers") or {})}
    allow_probable = bool(s.get("remote_allow_probable"))
    procs = _procs() if procs is None else procs
    provs = []
    if enabled.get("rdp", True):
        try:
            provs.append(_rdp(rdp_list))
        except Exception as e:  # noqa: BLE001
            provs.append({"name": "rdp", "label": "Windows Remote Desktop", "available": False, "viewer": None,
                          "confidence": "unknown", "detail": f"WTS query failed: {e}"})
    if enabled.get("logind", True):
        provs.append(_logind(logind_run))
    if enabled.get("rustdesk", True):
        provs.append(_rustdesk(procs, rustdesk_installed))
    provs += _others(procs)
    provs = [p for p in provs if p.get("available")]
    verified = [p for p in provs if p.get("viewer") is True and p.get("confidence") == "verified"]
    probable = [p for p in provs if p.get("viewer") is True and p.get("confidence") == "probable"]
    counted = verified + (probable if allow_probable else [])
    knowing = [p for p in provs if p.get("viewer") is not None]
    if counted:
        state = "remote_active"
    elif any(p.get("connecting") for p in provs):
        state = "remote_connecting"
    elif knowing:
        state = "local"
    else:
        state = "unknown"
    return {"state": state, "viewers": len(counted), "verified": len(verified), "probable": len(probable),
            "providers": provs, "at": time.time(),
            "counted": [p["label"] + (" (probable)" if p in probable else "") for p in counted]}


class Detector:
    """Readings over time: adds remote_disconnected (a counted viewer just left) and remote_reconnecting (it came
    back within the cooldown)."""

    def __init__(self, settings_fn, reader=None):
        self.settings_fn = settings_fn
        self.reader = reader or (lambda s: snapshot(s))
        self.last = None
        self.left_at = None

    def read(self):
        s = self.settings_fn()
        snap = self.reader(s)
        prev = (self.last or {}).get("state")
        cool = float(s.get("remote_restore_cooldown_s") or 90)
        if snap["state"] == "remote_active":
            if prev in ("remote_disconnected",) and self.left_at and time.time() - self.left_at < cool:
                snap["state"] = "remote_reconnecting" if prev != "remote_active" else "remote_active"
            self.left_at = None
        elif prev in ("remote_active", "remote_reconnecting") or (prev == "remote_disconnected" and self.left_at):
            if self.left_at is None:
                self.left_at = time.time()
            if time.time() - self.left_at < cool:
                snap["state"] = "remote_disconnected"
        snap["since_left_s"] = round(time.time() - self.left_at, 1) if self.left_at else None
        self.last = snap
        return snap
