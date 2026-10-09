"""Entry point: start the backend and open the UI in a chromeless browser app window (Edge, Chrome, Chromium or
Brave; the default browser when none of them is installed).

    python -m aero                  start Aero and open its window
    python -m aero --no-window      backend only (open http://127.0.0.1:8180 yourself)
    python -m aero --reopen         after an update or a mod restart: reuse the window that is still open
    python -m aero --safe           start with every mod switched off (see mods.py)
    python -m aero --version
"""
import os
import subprocess
import sys
import threading
import time
import webbrowser

import httpx

from . import osinfo
from .config import APP_NAME, DATA, IS_WIN, UI_PORT, VERSION

URL = f"http://127.0.0.1:{UI_PORT}"


def _dpi_aware():
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor: real pixels for screenshots/clicks
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def _already_running():
    try:
        return httpx.get(URL + "/api/state", timeout=1.5).status_code == 200
    except Exception:
        return False


def open_window():
    argv, sandboxed = osinfo.find_browser()
    if not argv:
        webbrowser.open(URL)
        return None
    args = argv + [f"--app={URL}", "--window-size=1280,860", "--no-first-run", "--no-default-browser-check",
                   "--disable-features=Translate"]
    if not sandboxed:                                 # snap/flatpak browsers can't use a profile under ~/.local
        args.append(f"--user-data-dir={DATA / 'ui-profile'}")
    kw = {} if IS_WIN else {"start_new_session": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        return subprocess.Popen(args, **kw)
    except OSError as e:
        log(f"Could not start {argv[0]} ({e}); opening the default browser instead.")
        webbrowser.open(URL)
        return None


def _quiet_stdio():
    """pythonw on Windows has no console; a launcher icon on macOS/Linux may have none either: log to a file."""
    if sys.stdout is None or sys.stderr is None or (not IS_WIN and not sys.stdout.isatty() and "--log-stdout" not in sys.argv):
        from .config import LOGS
        f = open(LOGS / "app.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f


def main():
    if "--version" in sys.argv:
        print(f"{APP_NAME} {VERSION}")
        return
    _quiet_stdio()
    _dpi_aware()
    log(f"{APP_NAME} {VERSION} starting on {osinfo.name()} (pid {os.getpid()})")
    reopen = "--reopen" in sys.argv
    if reopen:
        _wait_port_free(60)
    if _already_running():
        log("Already running; opening another window.")
        open_window()
        return
    from . import mods
    mods.boot_guard(safe="--safe" in sys.argv or os.environ.get("AERO_SAFE") == "1", log=log)
    import uvicorn
    try:
        from .server import app, shutdown
    except Exception:                                 # a mod (or a broken update) that breaks the import
        import traceback
        log(traceback.format_exc())
        mods.boot_failed(log=log)
        raise
    cfg = uvicorn.Config(app, host="127.0.0.1", port=UI_PORT, log_level="warning", access_log=False)
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(600):                              # up to 60 s on a cold first start
        if _already_running() or not t.is_alive():
            break
        time.sleep(0.1)
    if not _already_running():
        log("Backend did not come up; see the traceback above.")
        mods.boot_failed(log=log)
        return
    mods.boot_ok()
    from . import server, updater
    server.RUN["main"] = True
    if os.environ.get("AERO_UPDATE_CHECK") != "0":
        updater.check_at_startup()                    # once per start; Aero never polls for updates while it runs
    if "--no-window" in sys.argv:
        _live_headless(t)
        return
    if not (reopen and _window_reconnects(12)):
        open_window()
    _live_while_window_open()
    then = _exit_action()
    if then:
        then()                                        # starts detached and waits for this process to exit
    shutdown()
    time.sleep(1)


def _wait_port_free(timeout):
    """--reopen: the previous Aero is still shutting down; wait until its port is free."""
    t0 = time.time()
    while time.time() - t0 < timeout and _already_running():
        time.sleep(0.5)


def _window_reconnects(timeout):
    """True when the window from before the restart pings the new backend (it keeps polling while Aero is away)."""
    from .server import _last_ping
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _last_ping["ui"] > t0 - 1:
            log("The open window reconnected.")
            return True
        time.sleep(0.3)
    return False


def _live_headless(t):
    from .server import _last_ping
    while t.is_alive():
        time.sleep(1)
        if _last_ping.get("exit"):
            log(f"Stopping for {_last_ping['exit']}.")
            time.sleep(1)
            break
    then = _exit_action()
    if then:
        then()
    from .server import shutdown
    shutdown()
    time.sleep(1)


def _exit_action():
    """What to run once Aero has stopped: the updater, or a fresh Aero after a mod (see server /api/restart)."""
    from .server import _last_ping
    return _last_ping.get("then")


def _window_procs():
    """Browser processes running our app-window profile. Aero runs as admin on Windows and Edge relaunches
    itself un-elevated, so the msedge.exe we start exits at once; its replacement keeps our
    --user-data-dir on the command line, so that is how the window is found."""
    import psutil
    prof = str(DATA / "ui-profile").lower()
    names = osinfo.browser_process_names()
    n = 0
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (p.info["name"] or "").lower()
            if name.startswith(names) and prof in " ".join(p.info["cmdline"] or []).lower():
                n += 1
        except Exception:
            pass
    return n


def _live_while_window_open():
    """Run until the app window goes away: its browser processes are gone, or the page said goodbye
    (/api/bye on close) and didn't ping again (a reload pings again within a second).
    Pings alone can't be trusted: Edge freezes hidden or minimized windows for minutes."""
    from .server import _last_ping
    t0, fallback = time.time(), False
    while not _last_ping["ui"]:                       # wait for the window to connect
        time.sleep(0.5)
        if _last_ping.get("exit"):
            return
        if not fallback and time.time() - t0 > 45:
            log("The app window never connected; opening the default browser instead.")
            webbrowser.open(URL)
            fallback = True
        if time.time() - t0 > 600:
            log("No window connected for 10 minutes; exiting.")
            return
    log("App window connected.")
    seen, gone_since, n = False, None, 0
    while True:
        time.sleep(1)
        n += 1
        if _last_ping.get("exit"):
            log(f"Stopping for {_last_ping['exit']}.")
            time.sleep(1)                             # let the reply that asked for it reach the window
            return
        now, ui, bye = time.time(), _last_ping["ui"], _last_ping["bye"]
        if bye > ui and now - bye > 5:
            log("App window closed; unloading the model and exiting.")
            return
        if n % 3 == 0:
            if _window_procs():
                seen, gone_since = True, None
            elif seen:
                gone_since = gone_since or now
                if now - gone_since > 8:
                    log("App window is gone; unloading the model and exiting.")
                    return
        if not seen and now - ui > 1800:              # plain browser tab we can't see: 30 min of silence
            log("No word from the app window for 30 minutes; exiting.")
            return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        raise
