"""Entry point: start the backend and open the UI in a chromeless Edge app window."""
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

import httpx

from .config import APP_NAME, DATA, IS_WIN, UI_PORT

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


def _edge():
    cands = [shutil.which("msedge")]
    if IS_WIN:
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if base:
                cands.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
        for base in (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if base:
                cands.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    else:
        cands += [shutil.which("chromium"), shutil.which("google-chrome")]
    return next((c for c in cands if c and os.path.exists(c)), None)


def open_window():
    exe = _edge()
    if not exe:
        webbrowser.open(URL)
        return None
    prof = DATA / "ui-profile"
    return subprocess.Popen([exe, f"--app={URL}", f"--user-data-dir={prof}", "--window-size=1280,860",
                             "--no-first-run", "--no-default-browser-check", "--disable-features=Translate"])


def main():
    if sys.stdout is None or sys.stderr is None:      # pythonw (desktop shortcut): log to a file
        from .config import LOGS
        f = open(LOGS / "app.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f
    _dpi_aware()
    log(f"{APP_NAME} starting (pid {os.getpid()})")
    if _already_running():
        log("Already running; opening another window.")
        open_window()
        return
    import uvicorn
    from .server import app, shutdown
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
        return
    if "--no-window" in sys.argv:
        t.join()
        return
    open_window()
    _live_while_window_open()
    shutdown()
    time.sleep(1)


def _window_procs():
    """Browser processes running our app-window profile. Aero runs as admin and Edge relaunches
    itself un-elevated, so the msedge.exe we start exits at once; its replacement keeps our
    --user-data-dir on the command line, so that is how the window is found."""
    import psutil
    prof = str(DATA / "ui-profile").lower()
    n = 0
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (p.info["name"] or "").lower()
            if name.startswith(("msedge", "chrome", "chromium")) and prof in " ".join(p.info["cmdline"] or []).lower():
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
