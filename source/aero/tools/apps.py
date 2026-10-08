"""App control: pick any window on the desktop, see inside it, and drive it with Aero's own cursor and keys.

How it stays out of the user's way:
  - app_view captures just that window with PrintWindow, so it works even when the window is behind others,
    and reads its UI Automation tree (buttons, fields, text, menus) with numbered element ids.
  - Actions on element ids use UI Automation patterns (Invoke, Toggle, Select, Expand, SetValue), which press
    buttons and fill fields without touching the real mouse or keyboard.
  - Clicks at coordinates and plain typing are posted as window messages to the target window (background
    input). Apps that ignore those (some Chromium/Electron/UWP/game windows) can be driven with input="real",
    which briefly borrows the real mouse/keyboard, then puts the cursor and the previously active window back.
  - Every action shows Aero's own red cursor on screen (overlay.py), so you can see what it is doing.
Windows only.
"""
import ctypes
import io
import threading
import time

from . import tool
from .. import attachments
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
            _sel["elements"] = {}
            _lock = threading.RLock()   # the stuck thread may still hold the old lock
            raise RuntimeError("The app didn't respond within 60 s (a dialog may have opened). Call app_view again.")
    return run


_sel = {"hwnd": None, "title": "", "left": 0, "top": 0, "scale": 1.0, "w": 0, "h": 0, "elements": {}}
_lock = threading.RLock()

if IS_WIN:
    from ctypes import wintypes as wt
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32")
    dwmapi = ctypes.WinDLL("dwmapi")
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
    _sig(user32.ScreenToClient, wt.BOOL, H, ctypes.POINTER(wt.POINT))
    _sig(user32.ChildWindowFromPointEx, H, H, wt.POINT, wt.UINT)
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

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", H), ("hwndFocus", H),
                    ("hwndCapture", H), ("hwndMenuOwner", H), ("hwndMoveSize", H), ("hwndCaret", H),
                    ("rcCaret", wt.RECT)]
    _sig(user32.GetGUIThreadInfo, wt.BOOL, wt.DWORD, ctypes.POINTER(GUITHREADINFO))

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                    ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x200, 0x201, 0x202, 0x203
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MOUSEWHEEL = 0x204, 0x205, 0x207, 0x208, 0x20A
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x100, 0x101, 0x102
VK = {"enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "delete": 0x2E,
      "del": 0x2E, "space": 0x20, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
      "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22, "insert": 0x2D,
      **{f"f{i}": 0x6F + i for i in range(1, 13)}}
MODS = {"ctrl", "control", "alt", "shift", "win", "windows", "cmd"}


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


def windows():
    """Visible top-level app windows, front to back."""
    _win_only()
    import os
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


def _find(window):
    """A window by id, or by part of its title or app name; the last selected one when empty."""
    _win_only()
    if window in (None, "", 0) and _sel["hwnd"] and user32.IsWindow(_sel["hwnd"]):
        return _sel["hwnd"]
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


def _restore_quietly(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 4)          # SW_SHOWNOACTIVATE: un-minimize without stealing focus
        time.sleep(0.35)


# ---- capture -----------------------------------------------------------------------------------

def _capture(hwnd):
    from PIL import Image
    l, t, r, b = _rect(hwnd)
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
    if not ok or im.getextrema() in (((0, 0), (0, 0), (0, 0)),):
        import mss                                           # GPU-drawn apps can print black: grab the screen
        with mss.mss() as s:
            raw = s.grab({"left": l, "top": t, "width": w, "height": h})
        im = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    return im, (l, t)


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


def _tree(hwnd, max_items=200, max_depth=25, budget_s=6.0, find=""):
    """Numbered list of the window's visible controls. Element ids stay valid until the next view.
    Returns (lines, {id: control}, {id: (type, box in picture pixels)}, ids that were listed)."""
    auto = _uia()
    root = auto.ControlFromHandle(hwnd)
    if not root:
        return [], {}, {}, set()
    q = _clean(find).lower()
    if q:                                    # a search walks further than a normal view and lists only matches
        max_items, max_depth, budget_s = 3000, 40, 10.0
    lines, elems, boxes, listed, t0, cut = [], {}, {}, set(), time.time(), False
    sx, sy, sc = _sel["left"], _sel["top"], _sel["scale"] or 1
    anc = {}                                 # depth -> (name, cx, cy) of the control last seen at that depth
    for c, depth in auto.WalkControl(root, includeTop=False, maxDepth=max_depth):
        if time.time() - t0 > budget_s or len(elems) >= max_items:
            cut = True
            break
        anc[depth] = None
        try:
            if c.IsOffscreen:
                continue
            r = c.BoundingRectangle
            if r.width() <= 1 or r.height() <= 1:
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
    return lines, elems, boxes, listed


def _draw_marks(im, boxes, listed):
    """Numbered boxes on clickable controls, matching the [n] ids in the element list."""
    from PIL import ImageDraw, ImageFont
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.load_default(size=13)
    except TypeError:
        font = ImageFont.load_default()
    W, H = im.size
    for eid, (ct, x0, y0, x1, y1) in boxes.items():
        if eid not in listed or ct not in ACTIONABLE or ct == "ScrollBarControl":
            continue
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W - 1, x1), min(H - 1, y1)
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        d.rectangle([x0, y0, x1, y1], outline=(255, 40, 90), width=1)
        t = str(eid)
        tw = d.textlength(t, font=font)
        ly = y0 if (y1 - y0 >= 20 or y0 < 15) else y0 - 15     # inside the corner; above only small boxes
        d.rectangle([x0, ly, x0 + tw + 4, ly + 14], fill=(255, 40, 90))
        d.text((x0 + 2, ly), t, fill=(255, 255, 255), font=font)


def _observe(ctx, hwnd, elements=True, find="", zoom=None, marks=True):
    """Capture the window, list its controls and return {'text', 'image'}; selects the window."""
    from PIL import Image
    with _lock:
        _restore_quietly(hwnd)
        full, (l, t) = _capture(hwnd)
        w, h = full.size
        max_side = int(ctx.settings.get("screenshot_max_side") or 1568)
        scale = max(w, h) / max_side if max(w, h) > max_side else 1.0
        im = full.resize((round(w / scale), round(h / scale)), Image.LANCZOS) if scale > 1 else full.copy()
        keep = {} if (elements or hwnd != _sel["hwnd"]) else _sel["elements"]    # a picture-only look keeps ids
        _sel.update(hwnd=hwnd, title=_title(hwnd), left=l, top=t, scale=scale, w=im.size[0], h=im.size[1],
                    elements=keep)
        lines = []
        if elements:
            try:
                lines, elems, boxes, listed = _tree(hwnd, find=find)
                _sel["elements"] = elems
                if marks and ctx.vision:
                    _draw_marks(im, boxes, listed)
            except Exception as e:  # noqa: BLE001
                lines = [f"(UI Automation unavailable for this window: {e})"]
        pic, note = im, ""
        if zoom:
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
    buf = io.BytesIO()
    pic.save(buf, "PNG", optimize=True)
    meta = attachments.save_image_bytes(buf.getvalue(), f"app-{time.strftime('%H%M%S')}.png")
    pid, _ = _pid(hwnd)
    head = (f"Window id {hwnd}: \"{_sel['title']}\" ({_proc_name(pid)}), {w}x{h}, picture {im.size[0]}x{im.size[1]}; "
            f"coordinates are in that picture's pixels.{note}")
    if not ctx.vision:
        head += " The loaded model has no vision, so rely on the element list."
    elif elements and marks:
        head += " Numbered boxes in the picture match the [n] ids below."
    body = "\n".join(lines) if lines else ("(no element list requested)" if not elements else
                                            "(no accessible controls found; use the picture and x,y)")
    return {"text": head + "\n" + body, "image": meta["id"]}


def _snapshot_windows():
    try:
        return {w["id"] for w in windows()}
    except Exception:
        return set()


def _after(ctx, msg, before, look):
    """Result of an action plus a fresh view, so the model sees the effect without another call. If the action
    opened a new window of the same app (a dialog, a menu window), that window is selected and shown."""
    if isinstance(msg, dict) or look is False:
        return msg
    time.sleep(0.35)
    h = _sel["hwnd"]
    try:
        ws = [w for w in windows() if not w["mine"]]
        pid = _pid(h)[0] if h and user32.IsWindow(h) else None
        new = [w for w in ws if w["id"] not in before and (pid is None or w["pid"] == pid)]
        if new:
            h = new[0]["id"]
            msg += f'\nA new window opened and is now selected: id {h} "{new[0]["title"]}".'
        if not h or not user32.IsWindow(h):
            return msg + "\nThe window closed. Use app_list to pick another."
        v = _observe(ctx, h)
        v["text"] = msg + "\n\nNow:\n" + v["text"]
        return v
    except Exception as e:  # noqa: BLE001
        return msg + f"\n(Couldn't refresh the view: {e}. Call app_view.)"


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
    user32.PostMessageW(tgt, WM_MOUSEMOVE, 0, lp)
    for i in range(int(clicks or 1)):
        if i and button == "left":
            user32.PostMessageW(tgt, WM_LBUTTONDBLCLK, mk, lp)
        else:
            user32.PostMessageW(tgt, down, mk, lp)
        user32.PostMessageW(tgt, up, 0, lp)
        time.sleep(0.05)


class _Borrow:
    """Real input: bring the window forward, act, then give the cursor and the old active window back."""
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


EDITISH = ("edit", "richedit", "scintilla", "_wwg", "chrome_renderwidgethosthwnd", "consolewindowclass")
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
    user32.PostMessageW(target, WM_KEYDOWN, vk, 1 | sc << 16)
    user32.PostMessageW(target, WM_KEYUP, vk, 1 | sc << 16 | 0xC0000000)


def _post_text(target, text):
    for ch in text:
        if ch == "\n":
            _post_key(target, 0x0D)
        else:
            user32.PostMessageW(target, WM_CHAR, ord(ch), 1)
        time.sleep(0.002)


def _to_screen(x, y):
    return int(_sel["left"] + float(x) * _sel["scale"]), int(_sel["top"] + float(y) * _sel["scale"])


def _element(eid):
    c = _sel["elements"].get(int(eid))
    if c is None:
        raise ValueError(f"No element [{eid}] in the last app_view. Call app_view again; ids change every view.")
    return c


def _center(c):
    r = c.BoundingRectangle
    return int(r.left + r.width() / 2), int(r.top + r.height() / 2)


def _hwnd():
    h = _sel["hwnd"]
    if not h or not user32.IsWindow(h):
        raise ValueError("No app selected. Call app_view with a window title or id first.")
    return h


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
      "cover it) with numbered boxes on its controls, plus the matching [n] list of buttons, fields, menus and "
      "text. Use the ids with app_click/app_type. find='word' lists only matching controls (searches deeper, "
      "fast way to locate something in a big app). zoom=[x,y,w,h] shows that region at full detail.",
      "screen", {"window": {"type": "string", "description": "Window id, or part of its title or app name "
                                                            "(e.g. 'Notepad', 'chrome'). Empty = the selected app."},
                 "find": {"type": "string", "description": "Only list controls whose name/value/type contains this"},
                 "zoom": {"type": "array", "items": {"type": "number"},
                          "description": "[x, y, width, height] region of the picture to show enlarged"},
                 "elements": {"type": "boolean", "description": "Include the element list (default true)."}},
      summary=lambda a: (a.get("window") or "selected app") + (f" · find '{a['find']}'" if a.get("find") else ""))
@_on_ui_thread
def app_view(ctx, window="", find="", zoom=None, elements=True):
    return _observe(ctx, _find(window), elements=elements, find=find or "", zoom=zoom)


@tool("app_click", "Click in the selected app with Aero's own cursor. Give an element id from app_view "
      "(pressed through UI Automation, the user's mouse is not touched) or x,y in the app_view picture "
      "(sent to the window in the background). If nothing changes, retry with input='real' (briefly uses the "
      "real mouse, then puts it back).", "desktop",
      {"element": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"},
       "button": {"type": "string", "enum": ["left", "right", "middle"]},
       "clicks": {"type": "integer", "description": "2 = double click"},
       "look": {"type": "boolean", "description": "Return a fresh view after acting (default true)"},
       "input": {"type": "string", "enum": ["auto", "real"]}},
      summary=lambda a: f"element [{a['element']}]" if a.get("element") else f"{a.get('x')},{a.get('y')}")
@_on_ui_thread
def app_click(ctx, element=None, x=None, y=None, button="left", clicks=1, input="auto", look=True):
    before = _snapshot_windows()
    return _after(ctx, _click(ctx, element, x, y, button, clicks, input), before, look)


def _click(ctx, element, x, y, button, clicks, input):
    hwnd = _hwnd()
    button, clicks = button or "left", int(clicks or 1)
    with _lock:
        if element:
            c = _element(element)
            sx, sy = _center(c)
            _show_cursor(sx, sy, "click", f"[{element}] {c.Name[:30]}")
            if input != "real" and button == "left":
                how = _uia_press(c, clicks)
                if how:
                    return f"Pressed [{element}] \"{c.Name}\" via {how} (no real mouse)."
        elif x is not None and y is not None:
            sx, sy = _to_screen(x, y)
            _show_cursor(sx, sy, "click")
        else:
            return {"text": "Give an element id or x and y.", "error": True}
        if input == "real":
            with _Borrow(hwnd) as pg:
                pg.click(sx, sy, clicks=clicks, interval=0.08, button=button)
            return f"Clicked {button} x{clicks} at screen ({sx},{sy}) with the real mouse; cursor put back."
        _bg_click(hwnd, sx, sy, button, clicks)
        return (f"Sent a background {button} click x{clicks} at screen ({sx},{sy}). If nothing changed, retry with "
                "input='real'.")


def _uia_press(c, clicks):
    try:
        nh = c.NativeWindowHandle
        if clicks == 1 and nh and c.ControlTypeName == "ButtonControl" and _class(nh).lower() == "button":
            user32.PostMessageW(nh, 0x00F5, 0, 0)      # BM_CLICK, posted so a dialog it opens can't block us
            return "button click message"
    except Exception:
        pass
    for name, act in (("Invoke", lambda: c.GetInvokePattern().Invoke()),
                      ("Toggle", lambda: c.GetTogglePattern().Toggle()),
                      ("Select", lambda: c.GetSelectionItemPattern().Select()),
                      ("Expand/Collapse", lambda: _expand_toggle(c)),
                      ("default action", lambda: c.GetLegacyIAccessiblePattern().DoDefaultAction())):
        if clicks > 1 and name != "Invoke":
            continue
        try:
            act()
            return name
        except Exception:
            continue
    return None


def _expand_toggle(c):
    p = c.GetExpandCollapsePattern()
    if p.ExpandCollapseState == 0:
        p.Expand()
    else:
        p.Collapse()


@tool("app_type", "Type text into the selected app. With an element id the field's value is set directly "
      "(mode 'replace' or 'append'), without the keyboard. Without one, the text goes to the app's focused "
      "control in the background. input='real' types with the real keyboard (briefly focuses the app).",
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
    return _after(ctx, _type(ctx, text, element, mode, enter, input), before, look)


def _type(ctx, text, element, mode, enter, input):
    hwnd = _hwnd()
    with _lock:
        c = _element(element) if element else None
        if c is not None:
            _show_cursor(*_center(c), "type", f"[{element}]")
        if c is not None and input != "real":
            try:
                vp = c.GetValuePattern()
                if vp and not vp.IsReadOnly:
                    vp.SetValue(text if mode == "replace" else (vp.Value or "") + text)
                    if enter:
                        _post_key(c.NativeWindowHandle or _focus_hwnd(hwnd), 0x0D)
                    return f"Set [{element}] to {'' if mode == 'replace' else '…'}{text[:80]!r}" + (" + Enter" if enter else "")
            except Exception:
                pass
            try:
                c.SetFocus()
            except Exception:
                pass
        if input == "real":
            with _Borrow(hwnd) as pg:
                if c is not None:
                    pg.click(*_center(c))
                from .desktop import type_text
                type_text(ctx, text, enter)
            return f"Typed {len(text)} chars with the real keyboard" + (" + Enter" if enter else "")
        tgt = (c.NativeWindowHandle if c is not None and c.NativeWindowHandle else None) or _focus_hwnd(hwnd)
        _post_text(tgt, text)
        if enter:
            _post_key(tgt, 0x0D)
        return (f"Sent {len(text)} chars to the app in the background" + (" + Enter" if enter else "") +
                ". If nothing appeared, retry with input='real'.")


@tool("app_keys", "Press keys in the selected app, e.g. 'enter', 'tab tab', 'ctrl+s', 'alt+f4', 'f5'. Single keys "
      "go to the app in the background; chords with ctrl/alt/shift/win use the real keyboard for a moment "
      "(the previous window is re-activated after).", "desktop",
      {"keys": {"type": "string"}, "input": {"type": "string", "enum": ["auto", "real"]},
       "look": {"type": "boolean", "description": "Return a fresh view after acting (default true)"}}, ["keys"],
      summary=lambda a: a.get("keys", ""))
@_on_ui_thread
def app_keys(ctx, keys, input="auto", look=True):
    before = _snapshot_windows()
    return _after(ctx, _keys(ctx, keys, input), before, look)


def _keys(ctx, keys, input):
    hwnd = _hwnd()
    chords = keys.split()
    needs_real = input == "real" or any(set(k.lower() for k in ch.split("+")) & MODS for ch in chords)
    with _lock:
        l, t, r, b = _rect(hwnd)
        _show_cursor((l + r) // 2, (t + b) // 2, "type", keys[:30])
        if needs_real:
            from .desktop import press_keys
            with _Borrow(hwnd):
                press_keys(ctx, keys)
            return f"Pressed {keys} in \"{_title(hwnd)}\" (real keyboard, previous window restored)."
        tgt = _focus_hwnd(hwnd)
        for k in chords:
            k = k.lower()
            vk = VK.get(k)
            if vk is None and len(k) == 1:
                vk = user32.VkKeyScanW(k) & 0xFF
            if vk is None:
                return {"text": f"Unknown key '{k}'.", "error": True}
            _post_key(tgt, vk)
            time.sleep(0.03)
        return f"Pressed {keys} in the background."


@tool("app_scroll", "Scroll inside the selected app (positive = up, negative = down) at an element or x,y of the "
      "app_view picture, without moving the real mouse.", "desktop",
      {"amount": {"type": "integer"}, "element": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"},
       "look": {"type": "boolean", "description": "Return a fresh view after acting (default true)"}},
      ["amount"], summary=lambda a: str(a.get("amount")))
@_on_ui_thread
def app_scroll(ctx, amount, element=None, x=None, y=None, look=True):
    before = _snapshot_windows()
    return _after(ctx, _scroll(ctx, amount, element, x, y), before, look)


def _scroll(ctx, amount, element, x, y):
    hwnd = _hwnd()
    with _lock:
        if element:
            c = _element(element)
            sx, sy = _center(c)
            try:
                sp = c.GetScrollPattern()
                if sp and sp.VerticallyScrollable:
                    for _ in range(abs(int(amount))):
                        sp.Scroll(3, 4 if amount > 0 else 1)      # NoAmount / SmallIncrement / SmallDecrement
                    _show_cursor(sx, sy, "move")
                    return f"Scrolled [{element}] {amount}"
            except Exception:
                pass
        elif x is not None and y is not None:
            sx, sy = _to_screen(x, y)
        else:
            l, t, r, b = _rect(hwnd)
            sx, sy = (l + r) // 2, (t + b) // 2
        _show_cursor(sx, sy, "move")
        tgt, _, _ = _deepest_child(hwnd, sx, sy)
        for _ in range(abs(int(amount))):
            user32.PostMessageW(tgt, WM_MOUSEWHEEL, (120 if amount > 0 else -120 & 0xFFFF) << 16, _lp(sx, sy))
            time.sleep(0.03)
        return f"Scrolled {amount} at ({sx},{sy}) in the background."


@tool("app_read", "Read the text inside the selected app (or one element): documents, editors, chat logs, web "
      "page text, tables. Better than a picture for long text.", "screen",
      {"element": {"type": "integer"}, "max_chars": {"type": "integer"}},
      summary=lambda a: f"element [{a['element']}]" if a.get("element") else "whole window")
@_on_ui_thread
def app_read(ctx, element=None, max_chars=20000):
    hwnd = _hwnd()
    auto = _uia()
    root = _element(element) if element else auto.ControlFromHandle(hwnd)
    n = int(max_chars or 20000)
    parts = []
    try:
        tp = root.GetTextPattern()
        if tp:
            parts.append(tp.DocumentRange.GetText(n))
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
                if c.IsOffscreen:
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
    return text[:n] + ("\n... (truncated)" if len(text) > n else "") if text else "(no readable text; try app_view)"
