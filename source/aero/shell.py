"""Aero's own window on Windows (1.3.2): Microsoft Edge WebView2 in a WinForms form, without the Windows title bar.
The page draws its own header (title, project, views, minimize / maximize / close); the header is a drag region
(CSS app-region: drag, which WebView2 turns into a real title bar: move, Snap, double-click to maximize, the system
menu on right-click). The window keeps its normal frame otherwise, so the edges resize, Snap layouts and the minimize
and maximize animations work, and a maximized window stops at the taskbar.

    pythonw -m aero.shell http://127.0.0.1:8180

Runs as its own process (WinForms wants its own STA thread and message loop). Aero runs as administrator; the window
is started un-elevated with the desktop's token (launch()), like a browser would, so files dragged from Explorer can
be dropped on it. Without .NET, pythonnet, the WebView2 runtime or the SDK assemblies (from the pywebview package),
available() is False and Aero opens its window in Edge or Chrome as before.

The page talks to the window with window.chrome.webview.postMessage("min" | "max" | "close" | "state") and hears
{"window": "maximized" | "normal"} back."""
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

IS_WIN = os.name == "nt"
if IS_WIN:                       # importable everywhere: __main__ asks available() on macOS and Linux too
    import ctypes.wintypes as wt
WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"     # the WebView2 runtime's EdgeUpdate client id


def _lib():
    import importlib.util
    spec = importlib.util.find_spec("webview")
    if not spec or not spec.origin:
        return None
    lib = Path(spec.origin).parent / "lib"
    return lib if (lib / "Microsoft.Web.WebView2.Core.dll").is_file() and \
        (lib / "Microsoft.Web.WebView2.WinForms.dll").is_file() else None


def runtime_version():
    """The installed WebView2 runtime's version, or None (Windows 11 has it; Windows 10 gets it with Edge)."""
    if not IS_WIN:
        return None
    import winreg
    for hive, key in ((winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}"),
                      (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}"),
                      (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}")):
        try:
            with winreg.OpenKey(hive, key) as k:
                v = winreg.QueryValueEx(k, "pv")[0]
                if v and v != "0.0.0.0":
                    return v
        except OSError:
            continue
    return None


def available():
    if not IS_WIN or os.environ.get("AERO_NATIVE_WINDOW") == "0":
        return False
    import importlib.util
    return bool(importlib.util.find_spec("clr") and _lib() and runtime_version())


# ------------------------------------------------------------------------------------------------ starting it

def command(url):
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return [str(pyw if pyw.is_file() else exe), "-m", "aero.shell", url]


def launch(url, cwd):
    """Start the window process: un-elevated with the desktop's token when Aero is elevated (falls back to a plain
    child process). Returns the pid."""
    argv = command(url)
    if _elevated():
        pid = _spawn_as_desktop_user(argv, cwd)
        if pid:
            return pid
    return subprocess.Popen(argv, cwd=cwd).pid


def _elevated():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


if IS_WIN:
    class _SI(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                    ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                    ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                    ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
                    ("hStdError", wt.HANDLE)]

    class _PI(ctypes.Structure):
        _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE), ("dwProcessId", wt.DWORD),
                    ("dwThreadId", wt.DWORD)]


def _spawn_as_desktop_user(argv, cwd):
    """CreateProcessWithTokenW with a copy of Explorer's token (the shell window's process): the same user, not
    elevated. Administrators hold the impersonation privilege this needs. None when anything is missing."""
    k32, adv, u32 = ctypes.windll.kernel32, ctypes.windll.advapi32, ctypes.windll.user32
    k32.OpenProcess.restype = wt.HANDLE
    hwnd = u32.GetShellWindow()
    if not hwnd:
        return None
    pid = wt.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    proc = k32.OpenProcess(0x1000, False, pid.value)                 # PROCESS_QUERY_LIMITED_INFORMATION
    if not proc:
        return None
    tok, dup = wt.HANDLE(), wt.HANDLE()
    try:
        if not adv.OpenProcessToken(wt.HANDLE(proc), 0x000B, ctypes.byref(tok)):   # DUPLICATE|ASSIGN_PRIMARY|QUERY
            return None
        # TOKEN_QUERY|DUPLICATE|ASSIGN_PRIMARY|ADJUST_DEFAULT|ADJUST_SESSIONID, SecurityImpersonation, TokenPrimary
        if not adv.DuplicateTokenEx(tok, 0x018B, None, 2, 1, ctypes.byref(dup)):
            return None
        si, pi = _SI(), _PI()
        si.cb = ctypes.sizeof(si)
        cmd = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        env = os.environ.get("AERO_HOME")
        envblock = None
        if env:                                    # a test copy: the window must reach the same data folder
            block = "".join(f"{k}={v}\0" for k, v in os.environ.items()) + "\0"
            envblock = ctypes.create_unicode_buffer(block)
        ok = adv.CreateProcessWithTokenW(dup, 0, None, cmd, 0x00000400 if envblock else 0,   # CREATE_UNICODE_ENVIRONMENT
                                         envblock, str(cwd), ctypes.byref(si), ctypes.byref(pi))
        if not ok:
            return None
        k32.CloseHandle(pi.hThread)
        k32.CloseHandle(pi.hProcess)
        return pi.dwProcessId
    finally:
        for h in (tok, dup):
            if h:
                k32.CloseHandle(h)
        k32.CloseHandle(wt.HANDLE(proc))


# ------------------------------------------------------------------------------------------------ the window

WM_NCCALCSIZE = 0x0083
SM_CYSIZEFRAME, SM_CXPADDEDBORDER = 33, 92
LRESULT = ctypes.c_ssize_t
if IS_WIN:
    SUBCLASSPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, ctypes.c_size_t,
                                      ctypes.c_size_t)

    class _NCCALC(ctypes.Structure):
        _fields_ = [("rgrc", wt.RECT * 3), ("lppos", ctypes.c_void_p)]


def caption_off_top(saved_top, zoomed, frame):
    """The client area's new top edge: where the window starts (the title bar is gone). Maximized windows hang their
    frame off the screen's edge, so their top moves down by the frame's height (as Windows Terminal does)."""
    return saved_top + (frame if zoomed else 0)


def _state_file():
    from .config import DATA
    return DATA / "window.json"


def _load_bounds():
    try:
        b = json.loads(_state_file().read_text(encoding="utf-8"))
        return b if isinstance(b, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _profile_dir(data):
    """WebView2's profile: in Aero's data folder, or the user's local app data when the window (not elevated) can't
    write there (an install in a protected folder)."""
    for d in (Path(data) / "ui-webview", Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Aero" / "ui-webview"):
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return str(d)
        except OSError:
            continue
    return str(Path(data) / "ui-webview")


def run(url):
    import clr  # noqa: F401  (pythonnet)
    lib = _lib()
    clr.AddReference("System.Windows.Forms")
    clr.AddReference("System.Drawing")
    clr.AddReference(str(lib / "Microsoft.Web.WebView2.Core.dll"))
    clr.AddReference(str(lib / "Microsoft.Web.WebView2.WinForms.dll"))
    import System.Windows.Forms as WF
    from Microsoft.Web.WebView2.Core import CoreWebView2PermissionKind, CoreWebView2PermissionState
    from Microsoft.Web.WebView2.WinForms import CoreWebView2CreationProperties, WebView2
    from System import Action
    from System.Drawing import Color, Icon, Point, Rectangle, Size
    from System.Threading import ApartmentState, Thread, ThreadStart

    from .config import DATA, PKG_DIR
    u32, comctl = ctypes.windll.user32, ctypes.windll.comctl32
    comctl.DefSubclassProc.restype = LRESULT
    comctl.DefSubclassProc.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    comctl.SetWindowSubclass.argtypes = [wt.HWND, SUBCLASSPROC, ctypes.c_size_t, ctypes.c_size_t]
    keep = {}

    def main():
        WF.Application.EnableVisualStyles()
        form = WF.Form()
        form.Text = "Aero"
        form.BackColor = Color.FromArgb(255, 18, 38, 62)
        ico = PKG_DIR.parent / "brand" / "aero.ico"
        if not ico.is_file():
            ico = PKG_DIR / "static" / "icons" / "aero.ico"
        if ico.is_file():
            form.Icon = Icon(str(ico))
        form.MinimumSize = Size(720, 480)
        b = _load_bounds()
        rect = Rectangle(int(b.get("x", 0)), int(b.get("y", 0)), int(b.get("w", 1280)), int(b.get("h", 860)))
        if b and any(s.WorkingArea.IntersectsWith(rect) for s in WF.Screen.AllScreens):
            form.StartPosition = WF.FormStartPosition.Manual
            form.Location, form.Size = Point(rect.X, rect.Y), Size(rect.Width, rect.Height)
        else:
            form.StartPosition = WF.FormStartPosition.CenterScreen
            form.Size = Size(1280, 860)
        if b.get("max"):
            form.WindowState = WF.FormWindowState.Maximized
            keep["normal"] = (rect.X, rect.Y, rect.Width, rect.Height)

        web = WebView2()
        props = CoreWebView2CreationProperties()
        props.UserDataFolder = _profile_dir(DATA)
        props.AdditionalBrowserArguments = "--disable-features=Translate,ElasticOverscroll"
        web.CreationProperties = props
        web.Dock = WF.DockStyle.Fill
        web.DefaultBackgroundColor = form.BackColor
        form.Controls.Add(web)

        def post_state():
            try:
                st = "maximized" if form.WindowState == WF.FormWindowState.Maximized else "normal"
                web.CoreWebView2.PostWebMessageAsJson(json.dumps({"window": st}))
            except Exception:  # noqa: BLE001 - not ready yet
                pass

        def on_message(sender, args):
            try:
                msg = args.TryGetWebMessageAsString()
            except Exception:  # noqa: BLE001 - a JSON message: not one of ours
                return
            if msg == "min":
                form.WindowState = WF.FormWindowState.Minimized
            elif msg == "max":
                form.WindowState = WF.FormWindowState.Normal if form.WindowState == WF.FormWindowState.Maximized \
                    else WF.FormWindowState.Maximized
            elif msg == "close":
                form.Close()
            elif msg == "state":
                post_state()

        def on_permission(sender, args):
            # the microphone for voice typing, on Aero's own page only
            if args.PermissionKind == CoreWebView2PermissionKind.Microphone and str(args.Uri).startswith(url):
                args.State = CoreWebView2PermissionState.Allow

        def on_new_window(sender, args):
            args.Handled = True                    # links open in the user's browser, not in Aero's window
            target = str(args.Uri)
            if target.startswith(("http://", "https://", "mailto:")):
                os.startfile(target)  # noqa: S606

        def on_title(sender, args):
            form.Text = web.CoreWebView2.DocumentTitle or "Aero"

        def on_ready(sender, args):
            if not args.IsSuccess:
                sys.stderr.write(f"WebView2 failed: {args.InitializationException}\n")
                keep["failed"] = True
                form.Close()
                return
            s = web.CoreWebView2.Settings
            s.IsNonClientRegionSupportEnabled = True       # app-region: drag in the page = the title bar
            s.IsStatusBarEnabled = False
            s.IsSwipeNavigationEnabled = False
            web.CoreWebView2.WebMessageReceived += on_message
            web.CoreWebView2.PermissionRequested += on_permission
            web.CoreWebView2.NewWindowRequested += on_new_window
            web.CoreWebView2.DocumentTitleChanged += on_title
            web.CoreWebView2.Navigate(url)

        def track(sender, args):
            """Remember the normal (not maximized) bounds. WinForms still counts the removed title bar and makes
            the window one caption taller on every restore from maximized or minimized: put it back."""
            st, was = form.WindowState, keep.get("state")
            keep["state"] = st
            if st != WF.FormWindowState.Normal:
                return
            if was is not None and was != WF.FormWindowState.Normal and keep.get("normal"):
                x, y, w, h = keep["normal"]
                hwnd = wt.HWND(form.Handle.ToInt64())
                form.BeginInvoke(Action(lambda: u32.SetWindowPos(hwnd, None, x, y, w, h, 0x0014)))   # NOZORDER|NOACTIVATE
                return
            b = form.Bounds
            keep["normal"] = (b.X, b.Y, b.Width, b.Height)

        def on_resize(sender, args):
            track(sender, args)
            post_state()

        def save_bounds(sender, args):
            x, y, w, h = keep.get("normal") or (form.Bounds.X, form.Bounds.Y, form.Bounds.Width, form.Bounds.Height)
            try:
                _state_file().write_text(json.dumps({"x": x, "y": y, "w": w, "h": h,
                                                     "max": form.WindowState == WF.FormWindowState.Maximized}),
                                         encoding="utf-8")
            except OSError:
                pass

        def subclass(hwnd, msg, wp, lp, uid, data):
            if msg == WM_NCCALCSIZE and wp:
                p = ctypes.cast(lp, ctypes.POINTER(_NCCALC)).contents
                top = p.rgrc[0].top
                comctl.DefSubclassProc(hwnd, msg, wp, lp)       # left, right and bottom keep their resize borders
                frame = u32.GetSystemMetrics(SM_CYSIZEFRAME) + u32.GetSystemMetrics(SM_CXPADDEDBORDER)
                p.rgrc[0].top = caption_off_top(top, bool(u32.IsZoomed(hwnd)), frame)
                return 0
            return comctl.DefSubclassProc(hwnd, msg, wp, lp)

        def on_handle(sender, args):
            hwnd = wt.HWND(form.Handle.ToInt64())
            keep["proc"] = SUBCLASSPROC(subclass)
            comctl.SetWindowSubclass(hwnd, keep["proc"], 1, 0)
            u32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0027)   # SWP_FRAMECHANGED|NOMOVE|NOSIZE|NOZORDER

        form.HandleCreated += on_handle
        form.Resize += on_resize
        form.Move += track
        form.FormClosing += save_bounds
        web.CoreWebView2InitializationCompleted += on_ready
        web.EnsureCoreWebView2Async(None)
        WF.Application.Run(form)

    t = Thread(ThreadStart(main))
    t.SetApartmentState(ApartmentState.STA)
    t.Start()
    t.Join()
    if keep.get("failed"):
        raise RuntimeError("WebView2 could not start")


def main(argv):
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        pass
    url = argv[1] if len(argv) > 1 else "http://127.0.0.1:8180"
    try:
        run(url)
    except Exception:  # noqa: BLE001 - no .NET, no WebView2: the browser window as before
        import traceback
        traceback.print_exc()
        from .__main__ import open_browser_window
        open_browser_window()


if __name__ == "__main__":
    main(sys.argv)
