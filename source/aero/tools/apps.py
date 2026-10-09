"""App control on Windows: pick a window, see inside it, and act on it without taking the user's mouse or keyboard.

What is background and what is not (the result of every action says which one was used):
  - Reading: UI Automation (the control tree, values and text) and PrintWindow (a picture of just that window). Both
    work while the window is covered by others. A window that is minimized, or GPU-drawn and covered, can't be
    pictured without bringing it forward: the view says so and gives the control list only (never a picture of
    whatever else is on screen at that spot).
  - ACCESSIBILITY_BACKGROUND: element ids act through UI Automation patterns (Invoke, Toggle, SelectionItem,
    ExpandCollapse, Value, Scroll). Values are read back after they are set.
  - WINDOW_MESSAGE_BACKGROUND: x,y clicks, typing without an element id and single keys are posted to the window.
    Many apps (Chromium, Electron, UWP, games) ignore posted input, so the window is compared before and after and
    the result says whether anything changed. A field that didn't change is reported as a failure, never success.
    Classic Win32 edit fields get their text through WM_SETTEXT and are read back with WM_GETTEXT.
  - FOREGROUND_CONSENT_REQUIRED: input="real" and shortcuts with ctrl/alt/shift/win need the real keyboard and
    mouse. They only run when agent.exec_tool has the user's grant and the exclusive input lock (input_guard.py);
    otherwise they are refused. Nothing here falls back to real input on its own.
  - OBSERVE_ONLY: windows of programs running with higher rights (administrator) than Aero. Windows blocks input to
    them (UIPI); Aero reports that instead of trying to get around it.
Each agent (chat) has its own selected window and element ids. Ids belong to one view of one window: an id from an
older view, a closed window or another process is refused. SetFocus and foreground changes are never used in the
background paths. Every action shows Aero's own cursor (overlay.py) at the spot it acts on.
"""
import ctypes
import hashlib
import io
import os
import threading
import time

from . import REGISTRY, tool
from .. import action_results, attachments
from ..config import IS_WIN

_ui_pool = None


def _on_ui_thread(fn):
    """Run on one long-lived thread: UI Automation COM objects (the element ids) only work from the thread
    that created them."""
    import functools

    @functools.wraps(fn)
    def run(*a, **k):
        global _ui_pool, _lock
        if _ui_pool is None:
            from concurrent.futures import ThreadPoolExecutor

            def init():
                try:
                    import uiautomation as auto
                    _ui_pool.uia_init = auto.UIAutomationInitializerInThread()
                except Exception:
                    pass
            _ui_pool = ThreadPoolExecutor(1, "aero-ui", initializer=init)
        from concurrent.futures import TimeoutError as FutTimeout
        try:
            return _ui_pool.submit(fn, *a, **k).result(timeout=60)
        except FutTimeout:
            _ui_pool = None             # that thread is stuck (e.g. a button opened a modal dialog): start fresh
            for s in _sessions.values():
                s["elements"] = {}
            _lock = threading.RLock()   # the stuck thread may still hold the old lock
            raise RuntimeError("The app didn't respond within 60 s (a dialog may have opened). Call app_view again.")
    return run


def _new_session():
    return {"hwnd": None, "title": "", "left": 0, "top": 0, "scale": 1.0, "w": 0, "h": 0, "elements": {}, "gen": 0,
            "pid": None, "mode": None, "lines": [], "sig": None, "picture": False}


_sessions = {}            # owner (chat id) -> session
_last_owner = {"v": None}
_sel = _new_session()     # kept for code that still reads apps._sel: the most recently used session
_lock = threading.RLock()


def session(owner):
    s = _sessions.get(owner)
    if s is None:
        s = _sessions[owner] = _new_session()
    _last_owner["v"] = owner
    return s


def selected_hwnd(owner):
    s = _sessions.get(owner)
    h = s and s.get("hwnd")
    return h if h and IS_WIN and user32.IsWindow(h) else None


def view_info(owner=None):
    """(title, (left, top, right, bottom)) of an agent's selected window, for the 'is controlling' banner."""
    s = _sessions.get(owner if owner is not None else _last_owner["v"]) or {}
    if not s.get("hwnd"):
        return "", None
    w, h = (s.get("w") or 0) * (s.get("scale") or 1), (s.get("h") or 0) * (s.get("scale") or 1)
    return s.get("title") or "", (s["left"], s["top"], s["left"] + w, s["top"] + h)


def forget(owner):
    _sessions.pop(owner, None)


def _owner(ctx):
    return getattr(ctx, "owner", None) or "aero"


if IS_WIN:
    from ctypes import wintypes as wt
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32")
    dwmapi = ctypes.WinDLL("dwmapi")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    H = ctypes.c_void_p

    def _sig(fn, res, *args):
        fn.restype, fn.argtypes = res, list(args)

    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, H, wt.LPARAM)
    _sig(user32.EnumWindows, wt.BOOL, WNDENUMPROC, wt.LPARAM)
    _sig(user32.EnumChildWindows, wt.BOOL, H, WNDENUMPROC, wt.LPARAM)
    _sig(user32.IsWindowVisible, wt.BOOL, H)
    _sig(user32.IsWindow, wt.BOOL, H)
    _sig(user32.IsIconic, wt.BOOL, H)
    _sig(user32.GetWindowTextLengthW, ctypes.c_int, H)
    _sig(user32.GetWindowTextW, ctypes.c_int, H, wt.LPWSTR, ctypes.c_int)
    _sig(user32.GetClassNameW, ctypes.c_int, H, wt.LPWSTR, ctypes.c_int)
    _sig(user32.GetWindowRect, wt.BOOL, H, ctypes.POINTER(wt.RECT))
    _sig(user32.GetWindowThreadProcessId, wt.DWORD, H, ctypes.POINTER(wt.DWORD))
    _sig(user32.GetWindow, H, H, wt.UINT)
    _sig(user32.GetWindowLongW, ctypes.c_long, H, ctypes.c_int)
    _sig(user32.GetWindowDC, H, H)
    _sig(user32.ReleaseDC, ctypes.c_int, H, H)
    _sig(user32.PrintWindow, wt.BOOL, H, H, wt.UINT)
    _sig(user32.ShowWindow, wt.BOOL, H, ctypes.c_int)
    _sig(user32.SetForegroundWindow, wt.BOOL, H)
    _sig(user32.GetForegroundWindow, H)
    _sig(user32.PostMessageW, wt.BOOL, H, wt.UINT, wt.WPARAM, wt.LPARAM)
    _sig(user32.SendMessageTimeoutW, wt.LPARAM, H, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT,
         ctypes.POINTER(ctypes.c_size_t))
    _sig(user32.ScreenToClient, wt.BOOL, H, ctypes.POINTER(wt.POINT))
    _sig(user32.ChildWindowFromPointEx, H, H, wt.POINT, wt.UINT)
    _sig(user32.WindowFromPoint, H, wt.POINT)
    _sig(user32.GetAncestor, H, H, wt.UINT)
    _sig(user32.GetCursorPos, wt.BOOL, ctypes.POINTER(wt.POINT))
    _sig(user32.SetCursorPos, wt.BOOL, ctypes.c_int, ctypes.c_int)
    _sig(user32.MapVirtualKeyW, wt.UINT, wt.UINT, wt.UINT)
    _sig(user32.VkKeyScanW, ctypes.c_short, wt.WCHAR)
    _sig(gdi32.CreateCompatibleDC, H, H)
    _sig(gdi32.CreateCompatibleBitmap, H, H, ctypes.c_int, ctypes.c_int)
    _sig(gdi32.SelectObject, H, H, H)
    _sig(gdi32.DeleteObject, wt.BOOL, H)
    _sig(gdi32.DeleteDC, wt.BOOL, H)
    _sig(gdi32.GetDIBits, ctypes.c_int, H, H, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT)
    _sig(dwmapi.DwmGetWindowAttribute, ctypes.c_long, H, wt.DWORD, ctypes.c_void_p, wt.DWORD)
    _sig(kernel32.OpenProcess, H, wt.DWORD, wt.BOOL, wt.DWORD)
    _sig(kernel32.CloseHandle, wt.BOOL, H)
    _sig(advapi32.OpenProcessToken, wt.BOOL, H, wt.DWORD, ctypes.POINTER(H))
    _sig(advapi32.GetTokenInformation, wt.BOOL, H, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD))
    _sig(advapi32.GetSidSubAuthorityCount, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_void_p)
    _sig(advapi32.GetSidSubAuthority, ctypes.POINTER(wt.DWORD), ctypes.c_void_p, wt.DWORD)

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", H), ("hwndFocus", H),
                    ("hwndCapture", H), ("hwndMenuOwner", H), ("hwndMoveSize", H), ("hwndCaret", H),
                    ("rcCaret", wt.RECT)]
    _sig(user32.GetGUIThreadInfo, wt.BOOL, wt.DWORD, ctypes.POINTER(GUITHREADINFO))
    _sig(user32.GetParent, H, H)
    _sig(user32.GetDlgCtrlID, ctypes.c_int, H)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                    ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x200, 0x201, 0x202, 0x203
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MOUSEWHEEL = 0x204, 0x205, 0x207, 0x208, 0x20A
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x100, 0x101, 0x102
WM_SETTEXT, WM_GETTEXT, WM_GETTEXTLENGTH = 0x000C, 0x000D, 0x000E
SMTO_ABORTIFHUNG = 0x0002
VK = {"enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "delete": 0x2E,
      "del": 0x2E, "space": 0x20, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
      "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22, "insert": 0x2D,
      **{f"f{i}": 0x6F + i for i in range(1, 13)}}
MODS = {"ctrl", "control", "alt", "shift", "win", "windows", "cmd"}
CLASSIC_EDIT = ("edit", "richedit", "richedit20w", "richedit50w", "richeditd2dpt", "scintilla")
EDITISH = CLASSIC_EDIT + ("_wwg", "chrome_renderwidgethosthwnd", "consolewindowclass")
# window classes that ignore posted mouse/keyboard input in practice (input comes through other channels)
POSTED_INPUT_IGNORED = ("chrome_widgetwin", "chrome_renderwidgethosthwnd", "applicationframewindow",
                        "windows.ui.core.corewindow", "unitywndclass", "unrealwindow", "sdl_app", "glfw30")


class StaleElement(ValueError):
    pass


_STEALS = set()          # (process name, control type, pattern) seen bringing its window to the front


def _steal_key(c, how):
    try:
        return (_proc_name(c.ProcessId).lower(), c.ControlTypeName, how)
    except Exception:
        return None


class FocusGuard:
    """Some UI Automation providers focus or activate the window they act on (measured on Windows 11: the Win32 Edit
    proxy's ValuePattern.SetValue brings its window to the front). A background action must not change the window
    in front, so every pattern call runs inside this guard: when the window in front changed during the call and the
    user didn't touch the mouse or keyboard meanwhile, the previous window is put back with SetForegroundWindow (no
    synthetic input), and the result says what happened."""

    def __init__(self, settle=False):
        self.settle = settle          # whole actions: posted messages act a little later, so look again after a pause

    def __enter__(self):
        self.before = user32.GetForegroundWindow()
        self.t0 = time.monotonic()
        self.changed = False
        self.restored = None
        return self

    def __exit__(self, *a):
        if self.settle is False:
            return self._check()
        if self.settle:
            time.sleep(0.15)
            return self._check()
        return False

    def report(self, res):
        """Add what happened to the window in front to a tool result (whole-action guard)."""
        if not self.changed or not isinstance(res, dict):
            return res
        t = self.note().strip(" ()")
        res["text"] = (res.get("text") or "") + "\n" + t[:1].upper() + t[1:] + "."
        env = res.get("envelope")
        if isinstance(env, dict):
            env["focus"] = {"changed": True, "restored": self.restored}
        return res

    def _check(self):
        now = user32.GetForegroundWindow()
        if self.before and now != self.before:
            self.changed = True
            from .. import input_guard
            idle = input_guard.idle_ms()
            took = (time.monotonic() - self.t0) * 1000 + 50
            if idle is None or idle >= took:              # nobody touched the input during the call: it was us
                user32.SetForegroundWindow(self.before)
                time.sleep(0.05)
                self.restored = user32.GetForegroundWindow() == self.before
                # When Windows' foreground lock refuses this, Aero does not go further: UI Automation's SetFocus would
                # work, but it injects a keystroke (it resets the user's last-input time; measured on Windows 11).
        return False

    def note(self):
        if not self.changed:
            return ""
        if self.restored:
            return " (the app came to the front when UI Automation acted on it; Aero put your window back)"
        if self.restored is False:
            return " (the app came to the front when UI Automation acted on it and Aero could not put your window back)"
        return ""

    def apply(self, env):
        if self.changed and env is not None:
            env["focus"] = {"changed": True, "restored": self.restored}
        return env


def _win_only():
    if not IS_WIN:
        raise RuntimeError("App control is only available on Windows.")


# ---- windows -----------------------------------------------------------------------------------

def _title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    b = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, b, n + 1)
    return b.value


def _class(hwnd):
    b = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, b, 256)
    return b.value


def _rect(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _cloaked(hwnd):
    v = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(v), ctypes.sizeof(v))    # DWMWA_CLOAKED
    return v.value != 0


def _pid(hwnd):
    p = wt.DWORD()
    tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
    return p.value, tid


def _proc_name(pid):
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:
        return "?"


def integrity(pid):
    """The process's integrity level RID (0x1000 low, 0x2000 medium, 0x3000 high = administrator, 0x4000 system),
    or None when Windows won't say (protected processes)."""
    if not IS_WIN:
        return None
    h = kernel32.OpenProcess(0x1000, False, int(pid))          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    tok = H()
    try:
        if not advapi32.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):   # TOKEN_QUERY
            return None
        need = wt.DWORD()
        advapi32.GetTokenInformation(tok, 25, None, 0, ctypes.byref(need))  # TokenIntegrityLevel
        buf = ctypes.create_string_buffer(need.value or 64)
        if not advapi32.GetTokenInformation(tok, 25, buf, len(buf), ctypes.byref(need)):
            return None
        sid = ctypes.c_void_p.from_buffer(buf).value                 # TOKEN_MANDATORY_LABEL.Label.Sid
        n = advapi32.GetSidSubAuthorityCount(sid).contents.value
        return int(advapi32.GetSidSubAuthority(sid, n - 1).contents.value)
    except Exception:
        return None
    finally:
        if tok:
            kernel32.CloseHandle(tok)
        kernel32.CloseHandle(h)


def windows():
    """Visible top-level app windows, front to back."""
    _win_only()
    me = os.getpid()
    out = []

    def cb(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd) or _cloaked(hwnd):
                return True
            t = _title(hwnd)
            if not t.strip():
                return True
            ex = user32.GetWindowLongW(hwnd, -20)
            if ex & 0x80 and not ex & 0x40000:                      # tool windows without APPWINDOW
                return True
            l, tp, r, b = _rect(hwnd)
            pid, _ = _pid(hwnd)
            if r - l < 60 or b - tp < 40 or _class(hwnd) in ("Progman", "WorkerW", "Shell_TrayWnd"):
                return True
            out.append({"id": int(hwnd), "title": t, "app": _proc_name(pid), "pid": pid, "mine": pid == me,
                        "rect": [l, tp, r - l, b - tp], "minimized": bool(user32.IsIconic(hwnd))})
        except Exception:
            pass
        return True
    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def _find(window, sess):
    """A window by id, or by part of its title or app name; the agent's selected one when empty."""
    _win_only()
    if window in (None, "", 0) and sess["hwnd"] and user32.IsWindow(sess["hwnd"]):
        return sess["hwnd"]
    ws = windows()
    s = str(window or "").strip()
    if s.isdigit():
        h = int(s)
        if user32.IsWindow(h):
            return h
    low = s.lower().removesuffix(".exe")
    for key in ("title", "app"):
        for w in ws:
            if not w["mine"] and low and low in w[key].lower().removesuffix(".exe"):
                return w["id"]
    raise ValueError(f"No window matches '{window}'. Use app_list to see open windows.")


# ---- capture -----------------------------------------------------------------------------------

def _uniform(im):
    ext = im.getextrema()
    return all(lo == hi for lo, hi in ext)


def unoccluded(hwnd, grid=5):
    """True when every sampled point of the window belongs to it: nothing else covers it on screen."""
    left, top, right, bottom = _rect(hwnd)
    if right - left < 4 or bottom - top < 4:
        return False
    for i in range(grid):
        for j in range(grid):
            x = left + (right - left) * (i + 0.5) / grid
            y = top + (bottom - top) * (j + 0.5) / grid
            h = user32.WindowFromPoint(wt.POINT(int(x), int(y)))
            if not h or user32.GetAncestor(h, 2) != hwnd:      # GA_ROOT
                return False
    return True


def _capture(hwnd):
    """(image or None, (left, top), info). The picture is only of this window; when no honest picture can be taken
    (minimized, or GPU-drawn and covered) image is None and info says why."""
    from PIL import Image
    l, t, r, b = _rect(hwnd)
    if user32.IsIconic(hwnd):
        return None, (l, t), {"valid": False, "why": "the window is minimized, and Windows doesn't draw minimized "
                                                     "windows (app_view(restore=true) shows it without taking focus)"}
    w, h = max(1, r - l), max(1, b - t)
    wdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(wdc)
    bmp = gdi32.CreateCompatibleBitmap(wdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        ok = user32.PrintWindow(hwnd, mdc, 2)                # PW_RENDERFULLCONTENT: works for covered windows
        bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h, biPlanes=1,
                               biBitCount=32, biCompression=0)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        im = Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1).copy()
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, wdc)
    if ok and not _uniform(im):
        return im, (l, t), {"valid": True, "method": "PrintWindow"}
    if unoccluded(hwnd):                                     # GPU-drawn but in plain view: the screen shows it
        import mss
        with mss.mss() as s:
            raw = s.grab({"left": l, "top": t, "width": w, "height": h})
        im = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        if not _uniform(im):
            return im, (l, t), {"valid": True, "method": "screen (the window is in front and not covered)"}
    return None, (l, t), {"valid": False, "why": "the app draws with the GPU and other windows cover it, so no picture "
                                                 "of it can be taken without bringing it forward"}


def _thumb(im):
    try:
        small = im.convert("L").resize((24, 24))
        return list(small.get_flattened_data() if hasattr(small, "get_flattened_data") else small.getdata())
    except Exception:
        return None


def _changed(a, b, tol=6):
    """True when two 24x24 thumbnails differ visibly; None when either is missing."""
    if not a or not b or len(a) != len(b):
        return None
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a) > tol / 10


# ---- UI Automation tree ------------------------------------------------------------------------

ACTIONABLE = {"ButtonControl", "EditControl", "DocumentControl", "CheckBoxControl", "RadioButtonControl",
              "ComboBoxControl", "ListItemControl", "MenuItemControl", "TabItemControl", "HyperlinkControl",
              "TreeItemControl", "SplitButtonControl", "SliderControl", "SpinnerControl", "DataItemControl",
              "HeaderItemControl", "ScrollBarControl"}


def _uia():
    import uiautomation as auto
    auto.SetGlobalSearchTimeout(2)
    return auto


def _val(c):
    try:
        p = c.GetValuePattern()
        return p.Value if p else ""
    except Exception:
        return ""


def _clean(text):
    """Control names without icon-font glyphs (private-use characters) and line breaks."""
    text = "".join(ch for ch in (text or "") if not 0xE000 <= ord(ch) <= 0xF8FF)
    return " ".join(text.split())


def _tree(hwnd, sess, max_items=200, max_depth=25, budget_s=6.0, find="", offscreen_ok=False):
    """Numbered list of the window's controls. Element ids stay valid until the next view of this window.
    Returns (lines, {id: control}, {id: (type, box in picture pixels)}, ids that were listed, control types seen)."""
    auto = _uia()
    root = auto.ControlFromHandle(hwnd)
    if not root:
        return [], {}, {}, set(), set()
    q = _clean(find).lower()
    if q:                                    # a search walks further than a normal view and lists only matches
        max_items, max_depth, budget_s = 3000, 40, 10.0
    lines, elems, boxes, listed, kinds, t0, cut = [], {}, {}, set(), set(), time.time(), False
    sx, sy, sc = sess["left"], sess["top"], sess["scale"] or 1
    anc = {}                                 # depth -> (name, cx, cy) of the control last seen at that depth
    for c, depth in auto.WalkControl(root, includeTop=False, maxDepth=max_depth):
        if time.time() - t0 > budget_s or len(elems) >= max_items:
            cut = True
            break
        anc[depth] = None
        try:
            if c.IsOffscreen and not offscreen_ok:
                continue
            r = c.BoundingRectangle
            if (r.width() <= 1 or r.height() <= 1) and not offscreen_ok:
                continue
            ct, name = c.ControlTypeName, _clean(c.Name)
            if ct not in ACTIONABLE and not name:
                continue
            cx, cy = round((r.left + r.width() / 2 - sx) / sc), round((r.top + r.height() / 2 - sy) / sc)
            par = anc.get(depth - 1)
            if par and name and par[0] == name and abs(par[1] - cx) <= 4 and abs(par[2] - cy) <= 4:
                anc[depth] = par             # the same control drawn twice (MenuItem > Button > Text "File")
                continue
            if par and ct == "TextControl" and par[3] is not None and par[4] in ACTIONABLE:
                if name != par[0] and len(lines[par[3]]) < 220:      # fold labels like "Ctrl+S" into the parent
                    lines[par[3]] += f" ({name[:40]})"
                anc[depth] = par
                continue
            anc[depth] = (name, cx, cy, None, ct)
            val = _clean(_val(c)) if ct in ("EditControl", "ComboBoxControl", "DocumentControl") else ""
            eid = len(elems) + 1
            elems[eid] = c
            kinds.add(ct)
            boxes[eid] = (ct, (r.left - sx) / sc, (r.top - sy) / sc, (r.right - sx) / sc, (r.bottom - sy) / sc)
            label = ct.replace("Control", "")
            if q and q not in f"{name} {val} {label}".lower():
                continue                     # id stays usable, it just isn't listed
            extra = []
            if val:
                extra.append(f'value="{val[:120]}{"..." if len(val) > 120 else ""}"')
            if not c.IsEnabled:
                extra.append("disabled")
            try:
                if ct == "CheckBoxControl" and c.GetTogglePattern():
                    extra.append("checked" if c.GetTogglePattern().ToggleState == 1 else "unchecked")
            except Exception:
                pass
            ind = "" if q else "  " * min(depth - 1, 8)
            lines.append(f'{ind}[{eid}] {label} "{name[:80]}" at ({cx},{cy}){" " + " ".join(extra) if extra else ""}')
            listed.add(eid)
            anc[depth] = (name, cx, cy, len(lines) - 1, ct)
        except Exception:
            continue
    if cut:
        lines.append("... (more controls not listed; use app_view(find=...) to search, or app_read for text)")
    if q and not listed:
        lines.insert(0, f'(no controls matching "{find}"; try another word, or look at the picture)')
    return lines, elems, boxes, listed, kinds


def _draw_marks(im, boxes, listed):
    """Numbered boxes on clickable controls, matching the [n] ids in the element list."""
    from PIL import ImageDraw, ImageFont
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.load_default(size=13)
    except TypeError:
        font = ImageFont.load_default()
    W, H_ = im.size
    for eid, (ct, x0, y0, x1, y1) in boxes.items():
        if eid not in listed or ct not in ACTIONABLE or ct == "ScrollBarControl":
            continue
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W - 1, x1), min(H_ - 1, y1)
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        d.rectangle([x0, y0, x1, y1], outline=(255, 40, 90), width=1)
        t = str(eid)
        tw = d.textlength(t, font=font)
        ly = y0 if (y1 - y0 >= 20 or y0 < 15) else y0 - 15     # inside the corner; above only small boxes
        d.rectangle([x0, ly, x0 + tw + 4, ly + 14], fill=(255, 40, 90))
        d.text((x0 + 2, ly), t, fill=(255, 255, 255), font=font)


# ---- control mode -------------------------------------------------------------------------------

def probe(hwnd, kinds=()):
    """(mode, reason): how Aero can act on this window without the user's input (capabilities.CONTROL_MODES)."""
    pid, _ = _pid(hwnd)
    mine, theirs = integrity(os.getpid()), integrity(pid)
    if theirs is None and pid:
        return "OBSERVE_ONLY", "Windows won't tell Aero this program's rights level (a protected or system process)"
    if mine and theirs and theirs > mine:
        return "OBSERVE_ONLY", ("it runs as administrator and Aero doesn't, so Windows blocks Aero's input to it "
                                "(Aero does not get around that)")
    cls = _class(hwnd).lower()
    acc = set(kinds) & {"ButtonControl", "EditControl", "CheckBoxControl", "MenuItemControl", "ListItemControl",
                        "TabItemControl", "ComboBoxControl", "TreeItemControl", "HyperlinkControl", "DocumentControl"}
    if acc:
        return "ACCESSIBILITY_BACKGROUND", "its controls answer UI Automation: element ids act without your input"
    if any(k in cls for k in POSTED_INPUT_IGNORED):
        return "FOREGROUND_CONSENT_REQUIRED", ("it exposes no usable controls and ignores background input "
                                               "(GPU, Chromium/Electron or UWP window); only real input would work")
    return "WINDOW_MESSAGE_BACKGROUND", ("no UI Automation controls; clicks and keys are posted to the window and "
                                         "each result is checked by comparing the window before and after")


# ---- observe ------------------------------------------------------------------------------------

def _restore_quietly(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 4)          # SW_SHOWNOACTIVATE: un-minimize without taking focus
        time.sleep(0.35)


def _observe(ctx, hwnd, elements=True, find="", zoom=None, marks=True, restore=False):
    """Capture the window, list its controls and return {'text', 'image'}; selects the window for this agent."""
    from PIL import Image
    sess = session(_owner(ctx))
    with _lock:
        if restore:
            _restore_quietly(hwnd)
        full, (l, t), cap = _capture(hwnd)
        minimized = bool(user32.IsIconic(hwnd))
        max_side = int(ctx.settings.get("screenshot_max_side") or 1568)
        if full is not None:
            w, h = full.size
            scale = max(w, h) / max_side if max(w, h) > max_side else 1.0
            im = full.resize((round(w / scale), round(h / scale)), Image.LANCZOS) if scale > 1 else full.copy()
        else:
            rl, rt, rr, rb = _rect(hwnd)
            w, h = max(1, rr - rl), max(1, rb - rt)
            scale = max(w, h) / max_side if max(w, h) > max_side else 1.0
            im = None
        same = hwnd == sess["hwnd"]
        keep = sess["elements"] if (not elements and same) else {}
        pid, _ = _pid(hwnd)
        sess.update(hwnd=hwnd, title=_title(hwnd), left=l, top=t, scale=scale, w=round(w / scale),
                    h=round(h / scale), elements=keep, pid=pid, picture=im is not None)
        if elements or not same:
            sess["gen"] += 1
        lines, kinds = [], set()
        if elements:
            try:
                lines, elems, boxes, listed, kinds = _tree(hwnd, sess, find=find, offscreen_ok=minimized)
                sess["elements"] = elems
                if marks and ctx.vision and im is not None:
                    _draw_marks(im, boxes, listed)
            except Exception as e:  # noqa: BLE001
                lines = [f"(UI Automation unavailable for this window: {e})"]
            sess["mode"] = probe(hwnd, kinds)
            sess["lines"] = lines
        sess["sig"] = _thumb(full) if full is not None else None
        pic, note = im, ""
        if zoom and full is not None:
            try:
                zx, zy, zw, zh = [float(v) for v in list(zoom)[:4]]
                box = (max(0, int(zx * scale)), max(0, int(zy * scale)),
                       min(w, int((zx + zw) * scale)), min(h, int((zy + zh) * scale)))
                if box[2] - box[0] < 8 or box[3] - box[1] < 8:
                    raise ValueError("region too small")
                pic = full.crop(box)
                f = min(3.0, max_side / max(pic.size))
                if f > 1.05:
                    pic = pic.resize((round(pic.width * f), round(pic.height * f)), Image.LANCZOS)
                note = (f" Showing only the zoomed region {[int(v) for v in (zx, zy, zw, zh)]} at full detail; "
                        "click coordinates still refer to the whole-window picture.")
            except Exception as e:  # noqa: BLE001
                note = f" (zoom ignored: {e}; give zoom as [x, y, width, height] in picture pixels)"
    meta = None
    if pic is not None:
        buf = io.BytesIO()
        pic.save(buf, "PNG", optimize=True)
        meta = attachments.save_image_bytes(buf.getvalue(), f"app-{time.strftime('%H%M%S')}.png")
    if pic is not None:
        head = (f"Window id {hwnd}: \"{sess['title']}\" ({_proc_name(pid)}), {w}x{h}, picture {sess['w']}x{sess['h']}; "
                f"coordinates are in that picture's pixels.{note}")
    else:
        head = (f"Window id {hwnd}: \"{sess['title']}\" ({_proc_name(pid)}), {w}x{h}. No picture: {cap['why']}. "
                "The control list below still works.")
    mode, why = sess.get("mode") or ("", "")
    if mode:
        head += f"\nControl mode: {mode} ({why})."
    if pic is not None and not ctx.vision:
        head += " The loaded model has no vision, so rely on the element list."
    elif pic is not None and elements and marks:
        head += " Numbered boxes in the picture match the [n] ids below."
    body = "\n".join(lines) if lines else ("(no element list requested)" if not elements else
                                            "(no accessible controls found; use the picture and x,y)")
    return {"text": head + "\n" + body, "image": meta["id"] if meta else None}


def _snapshot_windows():
    try:
        return {w["id"] for w in windows()}
    except Exception:
        return set()


def _after(ctx, res, before, look):
    """The action's result plus a fresh view, so the model sees the effect without another call. Actions that could
    not check their effect directly are verified here: the window's controls and picture are compared with the view
    before the action. If the action opened a new window of the same app (a dialog), that window is selected."""
    if isinstance(res, tuple):
        msg, env = res
    else:
        msg, env = res, None
    if isinstance(msg, dict):
        return msg if env is None else action_results.attach(msg, env)
    sess = session(_owner(ctx))
    if look is False:
        out = {"text": msg}
        return action_results.attach(out, env) if env else out
    time.sleep(0.35)
    h = sess["hwnd"]
    old_lines, old_sig, old_title = list(sess.get("lines") or []), sess.get("sig"), sess.get("title")
    try:
        ws = [w for w in windows() if not w["mine"]]
        pid = _pid(h)[0] if h and user32.IsWindow(h) else None
        new = [w for w in ws if w["id"] not in before and (pid is None or w["pid"] == pid)]
        if new:
            h = new[0]["id"]
            msg += f'\nA new window opened and is now selected: id {h} "{new[0]["title"]}".'
        if not h or not user32.IsWindow(h):
            out = {"text": msg + "\nThe window closed. Use app_list to pick another."}
            if env and env.get("verified") is None:
                env.update(verified=True, verification_method="the window closed after the action")
            return action_results.attach(out, env) if env else out
        v = _observe(ctx, h)
        v["text"] = msg + "\n\nNow:\n" + v["text"]
        if env and env.get("verified") is None:
            moved = bool(new) or sess.get("title") != old_title or \
                [x.split(" at (")[0] for x in sess.get("lines") or []] != [x.split(" at (")[0] for x in old_lines]
            pix = _changed(old_sig, sess.get("sig"))
            if moved or pix:
                env.update(verified=True, verification_method="the window's controls or picture changed after it",
                           changed_state={"controls": moved, "picture": bool(pix)})
            elif env.get("backend") == "window_message_background":
                env.update(verified=False, verification_method="nothing in the window changed: the app likely "
                                                                "ignores background input")
            else:
                env.update(verification_method="no visible change (some actions change nothing on screen)")
        return action_results.attach(v, env) if env else v
    except Exception as e:  # noqa: BLE001
        out = {"text": msg + f"\n(Couldn't refresh the view: {e}. Call app_view.)"}
        return action_results.attach(out, env) if env else out


# ---- overlay cursor ----------------------------------------------------------------------------

def _show_cursor(sx, sy, action="move", label=""):
    try:
        from .. import overlay
        overlay.send(action, sx, sy, label)
    except Exception:
        pass


# ---- input -------------------------------------------------------------------------------------

def _lp(x, y):
    return (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)


def _deepest_child(hwnd, sx, sy):
    """The innermost child window under a screen point, and the point in its client coordinates."""
    cur = hwnd
    for _ in range(30):
        pt = wt.POINT(sx, sy)
        user32.ScreenToClient(cur, ctypes.byref(pt))
        ch = user32.ChildWindowFromPointEx(cur, pt, 0x1 | 0x2 | 0x4)    # skip invisible/disabled/transparent
        if not ch or ch == cur:
            return cur, pt.x, pt.y
        cur = ch
    pt = wt.POINT(sx, sy)
    user32.ScreenToClient(cur, ctypes.byref(pt))
    return cur, pt.x, pt.y


def _bg_click(hwnd, sx, sy, button="left", clicks=1):
    tgt, cx, cy = _deepest_child(hwnd, sx, sy)
    down, up, mk = {"left": (WM_LBUTTONDOWN, WM_LBUTTONUP, 1), "right": (WM_RBUTTONDOWN, WM_RBUTTONUP, 2),
                    "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, 0x10)}[button]
    lp = _lp(cx, cy)
    ok = bool(user32.PostMessageW(tgt, WM_MOUSEMOVE, 0, lp))
    for i in range(int(clicks or 1)):
        if i and button == "left":
            ok &= bool(user32.PostMessageW(tgt, WM_LBUTTONDBLCLK, mk, lp))
        else:
            ok &= bool(user32.PostMessageW(tgt, down, mk, lp))
        ok &= bool(user32.PostMessageW(tgt, up, 0, lp))
        time.sleep(0.05)
    return ok


class _Borrow:
    """Real input, only inside a granted foreground session (input_guard): bring the window forward, act, then put
    the cursor and the previously active window back."""
    def __init__(self, hwnd):
        self.hwnd = hwnd

    def __enter__(self):
        import pyautogui
        pyautogui.FAILSAFE = True
        self.pg = pyautogui
        self.prev = user32.GetForegroundWindow()
        self.pos = wt.POINT()
        user32.GetCursorPos(ctypes.byref(self.pos))
        _restore_quietly(self.hwnd)
        if self.prev != self.hwnd:
            pyautogui.press("alt")                     # lets SetForegroundWindow work from a background process
            user32.SetForegroundWindow(self.hwnd)
            time.sleep(0.15)
        return pyautogui

    def __exit__(self, *a):
        time.sleep(0.1)
        user32.SetCursorPos(self.pos.x, self.pos.y)
        if self.prev and self.prev != self.hwnd and user32.IsWindow(self.prev):
            user32.SetForegroundWindow(self.prev)
        return False


_last_focus = {}


def _focus_hwnd(hwnd):
    """The control that should get background keys: the one with keyboard focus in the window's thread, the
    last one seen focused, or (for a window that is in the background and so has no focus) its largest
    visible text-input child."""
    _, tid = _pid(hwnd)
    gi = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if user32.GetGUIThreadInfo(tid, ctypes.byref(gi)) and gi.hwndFocus:
        _last_focus[hwnd] = gi.hwndFocus
        return gi.hwndFocus
    f = _last_focus.get(hwnd)
    if f and user32.IsWindow(f) and user32.IsWindowVisible(f):
        return f
    best = {"h": None, "a": 0}

    def cb(ch, _):
        try:
            if user32.IsWindowVisible(ch) and any(k in _class(ch).lower() for k in EDITISH):
                l, t, r, b = _rect(ch)
                if (r - l) * (b - t) > best["a"]:
                    best.update(h=ch, a=(r - l) * (b - t))
        except Exception:
            pass
        return True
    user32.EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)
    return best["h"] or hwnd


def _post_key(target, vk):
    sc = user32.MapVirtualKeyW(vk, 0)
    a = user32.PostMessageW(target, WM_KEYDOWN, vk, 1 | sc << 16)
    b = user32.PostMessageW(target, WM_KEYUP, vk, 1 | sc << 16 | 0xC0000000)
    return bool(a and b)


def _post_text(target, text, cancelled=None):
    for ch in text:
        if cancelled and cancelled():
            return False
        if ch == "\n":
            _post_key(target, 0x0D)
        else:
            user32.PostMessageW(target, WM_CHAR, ord(ch), 1)
        time.sleep(0.002)
    return True


def _send(hwnd, msg, wp=0, lp=0, timeout_ms=800):
    res = ctypes.c_size_t(0)
    ok = user32.SendMessageTimeoutW(hwnd, msg, wp, lp, SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(res))
    return res.value if ok else None


def get_text(hwnd, limit=200000):
    """A window's text through WM_GETTEXT (classic controls answer it across processes), or None."""
    n = _send(hwnd, WM_GETTEXTLENGTH)
    if n is None:
        return None
    n = min(int(n), limit)
    buf = ctypes.create_unicode_buffer(n + 1)
    got = _send(hwnd, WM_GETTEXT, n + 1, ctypes.cast(buf, ctypes.c_void_p).value)
    return None if got is None else buf.value


def set_text(hwnd, text):
    buf = ctypes.create_unicode_buffer(text)
    return _send(hwnd, WM_SETTEXT, 0, ctypes.cast(buf, ctypes.c_void_p).value, 3000) is not None


def _is_classic_edit(hwnd):
    return bool(hwnd) and _class(hwnd).lower().startswith(CLASSIC_EDIT)


def _norm_nl(s):
    return (s or "").replace("\r\n", "\n").replace("\r", "\n")


def _read_field(c, hwnd):
    """The current text of a field: ValuePattern, then TextPattern, then WM_GETTEXT. None when unreadable."""
    if c is not None:
        v = None
        try:
            p = c.GetValuePattern()
            v = p.Value if p else None
        except Exception:
            v = None
        if v is not None:
            return v
        try:
            tp = c.GetTextPattern()
            if tp:
                return tp.DocumentRange.GetText(200000)
        except Exception:
            pass
    if hwnd:
        if _is_classic_edit(hwnd):
            return get_text(hwnd)
        try:
            auto = _uia()
            cc = auto.ControlFromHandle(hwnd)
            if cc:
                return _read_field(cc, None)
        except Exception:
            pass
    return None


def _to_screen(sess, x, y):
    return int(sess["left"] + float(x) * sess["scale"]), int(sess["top"] + float(y) * sess["scale"])


def _element(sess, eid):
    """The control behind an id from this agent's latest view of its window, checked to still be there and still
    belong to that window's process."""
    try:
        eid = int(eid)
    except (TypeError, ValueError):
        raise StaleElement(f"'{eid}' is not an element id.")
    c = sess["elements"].get(eid)
    if c is None:
        raise StaleElement(f"No element [{eid}] in your latest app_view of this window. Element ids change with "
                           "every view: call app_view again.")
    h = sess["hwnd"]
    if not h or not user32.IsWindow(h) or _pid(h)[0] != sess.get("pid"):
        raise StaleElement("The window of that view is gone. Call app_list and app_view again.")
    try:
        pid = c.ProcessId
        r = c.BoundingRectangle
        if pid != sess.get("pid") or (r.width() <= 0 and r.height() <= 0 and not user32.IsIconic(h)):
            raise StaleElement(f"Element [{eid}] is no longer in that window (the app changed). Call app_view again.")
    except StaleElement:
        raise
    except Exception:
        raise StaleElement(f"Element [{eid}] no longer exists (the app changed). Call app_view again.")
    return c


def _center(c):
    r = c.BoundingRectangle
    return int(r.left + r.width() / 2), int(r.top + r.height() / 2)


def _hwnd(sess):
    h = sess["hwnd"]
    if not h or not user32.IsWindow(h):
        raise ValueError("No app selected. Call app_view with a window title or id first.")
    return h


def _blocked(sess):
    mode = (sess.get("mode") or ("", ""))[0]
    if mode == "OBSERVE_ONLY":
        return {"text": f"Aero can read this window but not act on it: {sess['mode'][1]}.", "error": True}
    return None


def _need_physical(ctx, what):
    if getattr(ctx, "physical_ok", False):
        return None
    return {"text": f"{what} needs the real mouse and keyboard, and this call didn't get foreground control. Use an "
                    "element id, a file, the shell or the browser instead, or ask for input='real' so Aero can ask "
                    "the user first.", "error": True}


# ---- tools -------------------------------------------------------------------------------------

@tool("app_list", "List open app windows with their ids, app (process) names and sizes. Pick one with app_view.",
      "screen", {})
@_on_ui_thread
def app_list(ctx):
    ws = [w for w in windows() if not w["mine"]]
    if not ws:
        return "No app windows are open."
    fg = int(user32.GetForegroundWindow() or 0)
    return "\n".join(f"- id {w['id']}: {w['title']}  ({w['app']}, {w['rect'][2]}x{w['rect'][3]}"
                     f"{', minimized' if w['minimized'] else ''}{', active' if w['id'] == fg else ''})" for w in ws)


@tool("app_view", "Select an app window and look inside it: a picture of just that window (even if other windows "
      "cover it) with numbered boxes on its controls, the matching [n] list of buttons, fields, menus and text, and "
      "the window's control mode. Use the ids with app_click/app_type. find='word' lists only matching controls "
      "(searches deeper). zoom=[x,y,w,h] shows that region at full detail. restore=true un-minimizes the window "
      "without taking focus.",
      "screen", {"window": {"type": "string", "description": "Window id, or part of its title or app name "
                                                            "(e.g. 'Notepad', 'chrome'). Empty = the selected app."},
                 "find": {"type": "string", "description": "Only list controls whose name/value/type contains this"},
                 "zoom": {"type": "array", "items": {"type": "number"},
                          "description": "[x, y, width, height] region of the picture to show enlarged"},
                 "elements": {"type": "boolean", "description": "Include the element list (default true)."},
                 "restore": {"type": "boolean", "description": "Un-minimize a minimized window (no focus change)"}},
      summary=lambda a: (a.get("window") or "selected app") + (f" · find '{a['find']}'" if a.get("find") else ""))
@_on_ui_thread
def app_view(ctx, window="", find="", zoom=None, elements=True, restore=False):
    sess = session(_owner(ctx))
    return _observe(ctx, _find(window, sess), elements=elements, find=find or "", zoom=zoom, restore=bool(restore))


@tool("app_click", "Click in the selected app. With an element id from app_view it is pressed through UI Automation "
      "(no mouse involved) and the result is checked. x,y in the app_view picture are posted to the window in the "
      "background; apps that ignore that are reported. input='real' uses your real mouse and only runs after you "
      "allow foreground control.", "desktop",
      {"element": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"},
       "button": {"type": "string", "enum": ["left", "right", "middle"]},
       "clicks": {"type": "integer", "description": "2 = double click"},
       "look": {"type": "boolean", "description": "Return a fresh view after acting (default true)"},
       "input": {"type": "string", "enum": ["auto", "real"]}},
      summary=lambda a: f"element [{a['element']}]" if a.get("element") else f"{a.get('x')},{a.get('y')}")
@_on_ui_thread
def app_click(ctx, element=None, x=None, y=None, button="left", clicks=1, input="auto", look=True):
    before = _snapshot_windows()
    try:
        with FocusGuard(settle=input != "real" if "app_click" != "app_scroll" else True) as fg:
            res = _after(ctx, _click(ctx, element, x, y, button, clicks, input), before, look)
        return fg.report(res)
    except StaleElement as e:
        return {"text": str(e), "error": True}


def _click(ctx, element, x, y, button, clicks, input):
    sess = session(_owner(ctx))
    hwnd = _hwnd(sess)
    if (b := _blocked(sess)):
        return b
    button, clicks = button or "left", int(clicks or 1)
    with _lock:
        c = None
        if element:
            c = _element(sess, element)
            sx, sy = _center(c)
            _show_cursor(sx, sy, "click", f"[{element}] {c.Name[:30]}")
            if input != "real" and button == "left":
                with FocusGuard() as fg:
                    how, verified, method = _uia_press(c, clicks)
                if fg.changed and how:
                    _STEALS.add(_steal_key(c, how))
                if how:
                    env = fg.apply(action_results.make("accessibility_background", verified=verified, method=method,
                                                       mode="ACCESSIBILITY_BACKGROUND", target={"window_id": hwnd}))
                    return f"Pressed [{element}] \"{c.Name}\" via {how} (no real mouse).{fg.note()}", env
        elif x is not None and y is not None:
            sx, sy = _to_screen(sess, x, y)
            _show_cursor(sx, sy, "click")
        else:
            return {"text": "Give an element id or x and y.", "error": True}
        if input == "real":
            if (n := _need_physical(ctx, "A real click")):
                return n
            with _Borrow(hwnd) as pg:
                pg.click(sx, sy, clicks=clicks, interval=0.08, button=button)
            env = action_results.make("physical_input", mode="FOREGROUND_CONSENT_REQUIRED", target={"window_id": hwnd})
            return f"Clicked {button} x{clicks} at screen ({sx},{sy}) with the real mouse; cursor put back.", env
        if element and c is not None and button == "left":
            return {"text": f"Element [{element}] doesn't support being pressed through UI Automation. Try x,y from the "
                            "picture (background) or ask for input='real'.", "error": True}
        posted = _bg_click(hwnd, sx, sy, button, clicks)
        env = action_results.make("window_message_background", success=posted, mode="WINDOW_MESSAGE_BACKGROUND",
                                  error=None if posted else "Windows refused the message", target={"window_id": hwnd})
        return f"Posted a background {button} click x{clicks} at screen ({sx},{sy}).", env


def _ui_state(c):
    st = {}
    for name, get in (("toggle", lambda: c.GetTogglePattern().ToggleState),
                      ("selected", lambda: c.GetSelectionItemPattern().IsSelected),
                      ("expand", lambda: c.GetExpandCollapsePattern().ExpandCollapseState)):
        try:
            st[name] = get()
        except Exception:
            pass
    return st


def _uia_press(c, clicks):
    """Press a control without the mouse. Returns (how, verified, method).

    Classic push buttons get the notification their window handles (WM_COMMAND / BN_CLICKED to the parent): BM_CLICK
    and UI Automation's Invoke on them simulate a mouse press, which makes Windows bring an inactive window to the
    front (measured on Windows 11)."""
    try:
        nh = c.NativeWindowHandle
        if clicks == 1 and nh and c.ControlTypeName == "ButtonControl" and _class(nh).lower() == "button":
            style = user32.GetWindowLongW(nh, -16) & 0x0F                  # BS_TYPEMASK
            parent, cid = user32.GetParent(nh), user32.GetDlgCtrlID(nh)
            if style in (0, 1) and parent and cid:                          # BS_PUSHBUTTON, BS_DEFPUSHBUTTON
                if user32.PostMessageW(parent, 0x0111, (0 << 16) | (cid & 0xFFFF), nh):   # WM_COMMAND, BN_CLICKED
                    return "button command (BN_CLICKED)", None, ""
    except Exception:
        pass
    before = _ui_state(c)
    for name, act in (("Invoke", lambda: c.GetInvokePattern().Invoke()),
                      ("Toggle", lambda: c.GetTogglePattern().Toggle()),
                      ("Select", lambda: c.GetSelectionItemPattern().Select()),
                      ("Expand/Collapse", lambda: _expand_toggle(c)),
                      ("default action", lambda: c.GetLegacyIAccessiblePattern().DoDefaultAction())):
        if clicks > 1 and name != "Invoke":
            continue
        if _steal_key(c, name) in _STEALS:       # this app's control took focus with this pattern before
            continue
        try:
            act()
        except Exception:
            continue
        if name in ("Toggle", "Select", "Expand/Collapse"):
            time.sleep(0.05)
            after = _ui_state(c)
            key = {"Toggle": "toggle", "Select": "selected", "Expand/Collapse": "expand"}[name]
            if key in after:
                ok = after.get(key) != before.get(key) if name != "Select" else bool(after.get(key))
                return name, ok, f"{key} state read back"
        return name, None, ""
    return None, None, ""


def _expand_toggle(c):
    p = c.GetExpandCollapsePattern()
    if p.ExpandCollapseState == 0:
        p.Expand()
    else:
        p.Collapse()


@tool("app_type", "Type text into the selected app. With an element id the field's value is set directly (mode "
      "'replace' or 'append') and read back; without one, the text is sent to the app's focused field in the "
      "background and checked. input='real' types with your real keyboard after you allow foreground control.",
      "desktop",
      {"text": {"type": "string"}, "element": {"type": "integer"},
       "mode": {"type": "string", "enum": ["replace", "append"]},
       "enter": {"type": "boolean", "description": "Press Enter afterwards"},
       "look": {"type": "boolean", "description": "Return a fresh view after acting (default true)"},
       "input": {"type": "string", "enum": ["auto", "real"]}},
      ["text"], summary=lambda a: repr(a.get("text", ""))[:60])
@_on_ui_thread
def app_type(ctx, text, element=None, mode="append", enter=False, input="auto", look=True):
    before = _snapshot_windows()
    try:
        with FocusGuard(settle=input != "real" if "app_type" != "app_scroll" else True) as fg:
            res = _after(ctx, _type(ctx, text, element, mode, enter, input), before, look)
        return fg.report(res)
    except StaleElement as e:
        return {"text": str(e), "error": True}


def _type(ctx, text, element, mode, enter, input):
    sess = session(_owner(ctx))
    hwnd = _hwnd(sess)
    if (b := _blocked(sess)):
        return b
    text = str(text)
    mode = "replace" if mode == "replace" else "append"
    with _lock:
        c = _element(sess, element) if element else None
        if c is not None:
            _show_cursor(*_center(c), "type", f"[{element}]")
        if input == "real":
            if (n := _need_physical(ctx, "Typing with the real keyboard")):
                return n
            with _Borrow(hwnd) as pg:
                if c is not None:
                    pg.click(*_center(c))
                from .desktop import type_text
                type_text(ctx, text, enter)
            env = action_results.make("physical_input", mode="FOREGROUND_CONSENT_REQUIRED", target={"window_id": hwnd})
            return f"Typed {len(text)} chars with the real keyboard" + (" + Enter" if enter else ""), env
        tail = ""
        native = None
        try:
            native = c.NativeWindowHandle if c is not None else None
        except Exception:
            native = None
        if c is not None and not _is_classic_edit(native):
            # 1. UI Automation ValuePattern: set and read back (classic Win32 edits skip this: their UI Automation
            #    proxy activates the window, while WM_SETTEXT below never does)
            try:
                vp = c.GetValuePattern() if _steal_key(c, "SetValue") not in _STEALS else None
                if vp and not vp.IsReadOnly:
                    old = vp.Value or ""
                    want = text if mode == "replace" else old + text
                    with FocusGuard() as fg:
                        vp.SetValue(want)
                    if fg.changed:
                        _STEALS.add(_steal_key(c, "SetValue"))
                    time.sleep(0.05)
                    got = _read_field(c, None)
                    ok = None if got is None else _norm_nl(got) == _norm_nl(want)
                    if enter:
                        _post_key(native or _focus_hwnd(hwnd), 0x0D)
                        tail = " + Enter (sent in the background)"
                    env = fg.apply(action_results.make("accessibility_background", verified=ok, method="the field's "
                                                       "value read back", mode="ACCESSIBILITY_BACKGROUND",
                                                       target={"window_id": hwnd}))
                    return f"Set [{element}] to {'' if mode == 'replace' else '…'}{text[:80]!r}{tail}{fg.note()}", env
            except Exception:
                pass
        target = native or _focus_hwnd(hwnd)
        # 2. classic Win32 edit control: WM_SETTEXT, read back with WM_GETTEXT (no focus, no activation)
        if _is_classic_edit(target):
            old = get_text(target)
            if old is not None:
                want = text if mode == "replace" else old + text
                set_text(target, want)
                got = get_text(target)
                ok = None if got is None else _norm_nl(got) == _norm_nl(want)
                if enter:
                    _post_key(target, 0x0D)
                    tail = " + Enter (sent in the background)"
                env = action_results.make("window_message_background", verified=ok, method="the field's text read "
                                          "back (WM_GETTEXT)", mode="WINDOW_MESSAGE_BACKGROUND",
                                          target={"window_id": hwnd})
                return f"Set the text field ({_class(target)}) to {'' if mode == 'replace' else '…'}{text[:80]!r}{tail}", env
        # 3. posted characters, then check that the field changed
        before = _read_field(c, target)
        _post_text(target, text, getattr(ctx, "cancelled", None))
        if enter:
            _post_key(target, 0x0D)
            tail = " + Enter"
        time.sleep(0.2)
        after = _read_field(c, target)
        if before is None or after is None:
            ok, how = None, ""
        else:
            ok = _norm_nl(text).strip() in _norm_nl(after) and _norm_nl(after) != _norm_nl(before)
            how = "the field's text read back"
        env = action_results.make("window_message_background", verified=ok, method=how,
                                  mode="WINDOW_MESSAGE_BACKGROUND", target={"window_id": hwnd})
        note = ""
        if ok is False:
            note = (" The field did not change: this app ignores background typing. Use an element id with a value, "
                    "edit the file directly, or ask for input='real'.")
        return f"Sent {len(text)} chars to {_class(target)} in the background{tail}.{note}", env


@tool("app_keys", "Press keys in the selected app in the background: 'enter', 'tab tab', 'f5', 'down down enter'. "
      "Shortcuts with ctrl/alt/shift/win need your real keyboard: they only run after you allow foreground control; "
      "prefer clicking the menu item (app_view find='save').", "desktop",
      {"keys": {"type": "string"}, "input": {"type": "string", "enum": ["auto", "real"]},
       "look": {"type": "boolean", "description": "Return a fresh view after acting (default true)"}}, ["keys"],
      summary=lambda a: a.get("keys", ""))
@_on_ui_thread
def app_keys(ctx, keys, input="auto", look=True):
    before = _snapshot_windows()
    with FocusGuard(settle=not (input == "real" or has_chord(keys))) as fg:
        res = _after(ctx, _keys(ctx, keys, input), before, look)
    return fg.report(res)


def has_chord(keys):
    return any(set(k.lower() for k in ch.split("+")) & MODS for ch in str(keys or "").split())


def _keys(ctx, keys, input):
    sess = session(_owner(ctx))
    hwnd = _hwnd(sess)
    if (b := _blocked(sess)):
        return b
    chords = str(keys).split()
    needs_real = input == "real" or has_chord(keys)
    with _lock:
        l, t, r, b = _rect(hwnd)
        _show_cursor((l + r) // 2, (t + b) // 2, "type", keys[:30])
        if needs_real:
            if (n := _need_physical(ctx, f"The shortcut '{keys}'")):
                return n
            from .desktop import press_keys
            with _Borrow(hwnd):
                press_keys(ctx, keys)
            env = action_results.make("physical_input", mode="FOREGROUND_CONSENT_REQUIRED", target={"window_id": hwnd})
            return f"Pressed {keys} in \"{_title(hwnd)}\" (real keyboard, previous window restored).", env
        tgt = _focus_hwnd(hwnd)
        for k in chords:
            if getattr(ctx, "cancelled", lambda: False)():
                return {"text": "Stopped.", "error": True}
            k = k.lower()
            vk = VK.get(k)
            if vk is None and len(k) == 1:
                vk = user32.VkKeyScanW(k) & 0xFF
            if vk is None:
                return {"text": f"Unknown key '{k}'.", "error": True}
            _post_key(tgt, vk)
            time.sleep(0.03)
        env = action_results.make("window_message_background", mode="WINDOW_MESSAGE_BACKGROUND",
                                  target={"window_id": hwnd})
        return f"Posted {keys} to the app in the background.", env


@tool("app_scroll", "Scroll inside the selected app (positive = up, negative = down) at an element or x,y of the "
      "app_view picture, without moving the real mouse.", "desktop",
      {"amount": {"type": "integer"}, "element": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"},
       "look": {"type": "boolean", "description": "Return a fresh view after acting (default true)"}},
      ["amount"], summary=lambda a: str(a.get("amount")))
@_on_ui_thread
def app_scroll(ctx, amount, element=None, x=None, y=None, look=True):
    before = _snapshot_windows()
    try:
        with FocusGuard(settle=input != "real" if "app_scroll" != "app_scroll" else True) as fg:
            res = _after(ctx, _scroll(ctx, amount, element, x, y), before, look)
        return fg.report(res)
    except StaleElement as e:
        return {"text": str(e), "error": True}


def _scroll(ctx, amount, element, x, y):
    sess = session(_owner(ctx))
    hwnd = _hwnd(sess)
    if (b := _blocked(sess)):
        return b
    amount = int(amount or 0)
    with _lock:
        if element:
            c = _element(sess, element)
            sx, sy = _center(c)
            try:
                sp = c.GetScrollPattern()
                if sp and sp.VerticallyScrollable:
                    before = sp.VerticalScrollPercent
                    with FocusGuard() as fg:
                        for _ in range(abs(amount)):
                            sp.Scroll(3, 4 if amount > 0 else 1)  # NoAmount / SmallIncrement / SmallDecrement
                    _show_cursor(sx, sy, "move")
                    after = c.GetScrollPattern().VerticalScrollPercent
                    env = fg.apply(action_results.make("accessibility_background", verified=after != before,
                                                       method="scroll position read back",
                                                       mode="ACCESSIBILITY_BACKGROUND"))
                    return f"Scrolled [{element}] {amount} ({before:.0f}% -> {after:.0f}%){fg.note()}", env
            except Exception:
                pass
        elif x is not None and y is not None:
            sx, sy = _to_screen(sess, x, y)
        else:
            l, t, r, b = _rect(hwnd)
            sx, sy = (l + r) // 2, (t + b) // 2
        _show_cursor(sx, sy, "move")
        tgt, _, _ = _deepest_child(hwnd, sx, sy)
        for _ in range(abs(amount)):
            user32.PostMessageW(tgt, WM_MOUSEWHEEL, (120 if amount > 0 else -120 & 0xFFFF) << 16, _lp(sx, sy))
            time.sleep(0.03)
        env = action_results.make("window_message_background", mode="WINDOW_MESSAGE_BACKGROUND")
        return f"Posted {amount} wheel steps at ({sx},{sy}) in the background.", env


@tool("app_read", "Read the text inside the selected app (or one element): documents, editors, chat logs, web "
      "page text, tables. Better than a picture for long text.", "screen",
      {"element": {"type": "integer"}, "max_chars": {"type": "integer"}},
      summary=lambda a: f"element [{a['element']}]" if a.get("element") else "whole window")
@_on_ui_thread
def app_read(ctx, element=None, max_chars=20000):
    sess = session(_owner(ctx))
    hwnd = _hwnd(sess)
    auto = _uia()
    try:
        root = _element(sess, element) if element else auto.ControlFromHandle(hwnd)
    except StaleElement as e:
        return {"text": str(e), "error": True}
    n = int(max_chars or 20000)
    parts = []
    try:
        tp = root.GetTextPattern()
        if tp:
            parts.append(tp.DocumentRange.GetText(n + 1))
    except Exception:
        pass
    if not parts:
        v = _val(root)
        if v:
            parts.append(v)
    if not parts or sum(map(len, parts)) < 40:
        seen, t0 = set(p.strip() for p in parts), time.time()
        for c, _ in auto.WalkControl(root, includeTop=True, maxDepth=40):
            if time.time() - t0 > 8 or sum(map(len, parts)) > n:
                break
            try:
                if c.IsOffscreen and not user32.IsIconic(hwnd):
                    continue
                txt = ""
                if c.ControlTypeName in ("EditControl", "DocumentControl"):
                    try:
                        tp = c.GetTextPattern()
                        txt = tp.DocumentRange.GetText(n) if tp else _val(c)
                    except Exception:
                        txt = _val(c)
                elif c.ControlTypeName in ("TextControl", "HyperlinkControl", "ListItemControl", "DataItemControl",
                                           "HeaderItemControl", "TreeItemControl"):
                    txt = c.Name
                txt = (txt or "").strip()
                if txt and txt not in seen:
                    seen.add(txt)
                    parts.append(txt)
            except Exception:
                continue
    text = "\n".join(parts)
    if not text:
        return "(no readable text; try app_view)"
    if len(text) > n:
        return text[:n] + f"\n[TRUNCATED: showed {n:,} of at least {len(text):,} chars; pass a larger max_chars]"
    return text


def state_digest(owner):
    """Short fingerprint of an agent's window view (for tests and verification)."""
    s = _sessions.get(owner) or {}
    return hashlib.sha1("\n".join(s.get("lines") or []).encode()).hexdigest()[:12]


# App control reads windows through Windows' UI Automation, so these tools exist only there.
for _n in ("app_list", "app_view", "app_click", "app_type", "app_keys", "app_scroll", "app_read"):
    REGISTRY[_n].available = lambda: IS_WIN
