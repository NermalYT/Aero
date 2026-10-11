"""Screen capture and mouse/keyboard control (Claude Code / Codex "computer use" style).

Coordinates the model sends are in the pixel space of the most recent screenshot of that
monitor; they are mapped back to real desktop pixels here, so downscaling is transparent.
"""
import io
import os
import subprocess
import threading
import time

from . import REGISTRY, tool
from .. import attachments, osinfo
from ..config import IS_MAC, IS_WIN

_state = {"monitor": 1, "scale": 1.0, "left": 0, "top": 0, "w": 0, "h": 0}
_lock = threading.Lock()


def _pg():
    import pyautogui
    pyautogui.FAILSAFE = True       # slam the mouse into a screen corner to abort
    pyautogui.PAUSE = 0.05
    return pyautogui


def _map(x, y):
    s = _state
    return int(s["left"] + float(x) * s["scale"]), int(s["top"] + float(y) * s["scale"])


@tool("screenshot", "Capture the screen. Returns an image (vision models) and its size. Use the screenshot's "
      "pixel coordinates for mouse tools. monitor: 1 = primary, 2 = second..., 0 = all monitors stitched.",
      "screen", {"monitor": {"type": "integer"}}, summary=lambda a: f"monitor {a.get('monitor', 1)}")
def screenshot(ctx, monitor=1):
    import mss
    from PIL import Image
    with _lock, mss.mss() as sct:
        mons = sct.monitors
        monitor = int(monitor if monitor is not None else 1)
        if monitor < 0 or monitor >= len(mons):
            monitor = 1
        m = mons[monitor]
        raw = sct.grab(m)
        im = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    max_side = int(ctx.settings.get("screenshot_max_side") or 1568)
    w, h = im.size
    scale = max(w, h) / max_side if max(w, h) > max_side else 1.0
    if scale > 1:
        im = im.resize((round(w / scale), round(h / scale)), Image.LANCZOS)
    _state.update(monitor=monitor, scale=scale, left=m["left"], top=m["top"], w=im.size[0], h=im.size[1])
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    meta = attachments.save_image_bytes(buf.getvalue(), f"screenshot-{time.strftime('%H%M%S')}.png")
    txt = (f"Screenshot of monitor {monitor}: real {w}x{h}, shown at {im.size[0]}x{im.size[1]}. "
           f"Use coordinates in the shown {im.size[0]}x{im.size[1]} space.")
    if not ctx.vision:
        txt += " NOTE: the loaded model has no vision projector, so you cannot see this image."
    return {"text": txt, "image": meta["id"]}


@tool("mouse_click", "Click at (x, y) in the last screenshot's coordinates.", "desktop",
      {"x": {"type": "number"}, "y": {"type": "number"},
       "button": {"type": "string", "enum": ["left", "right", "middle"]},
       "clicks": {"type": "integer", "description": "1 = single, 2 = double click."}},
      ["x", "y"], summary=lambda a: f"{a.get('button', 'left')} ×{a.get('clicks', 1)} at {a.get('x')},{a.get('y')}")
def mouse_click(ctx, x, y, button="left", clicks=1):
    pg = _pg()
    rx, ry = _map(x, y)
    pg.click(rx, ry, clicks=int(clicks or 1), interval=0.08, button=button or "left")
    return f"Clicked {button} x{clicks} at screenshot ({x},{y}) = screen ({rx},{ry})"


@tool("mouse_move", "Move the mouse to (x, y) in screenshot coordinates (for hover menus).", "desktop",
      {"x": {"type": "number"}, "y": {"type": "number"}}, ["x", "y"])
def mouse_move(ctx, x, y):
    rx, ry = _map(x, y)
    _pg().moveTo(rx, ry, duration=0.1)
    return f"Moved to ({rx},{ry})"


@tool("mouse_drag", "Drag with the left button from (x1,y1) to (x2,y2) in screenshot coordinates.", "desktop",
      {"x1": {"type": "number"}, "y1": {"type": "number"}, "x2": {"type": "number"}, "y2": {"type": "number"}},
      ["x1", "y1", "x2", "y2"])
def mouse_drag(ctx, x1, y1, x2, y2):
    pg = _pg()
    a, b = _map(x1, y1), _map(x2, y2)
    pg.moveTo(*a)
    pg.dragTo(*b, duration=0.4, button="left")
    return f"Dragged {a} → {b}"


@tool("scroll", "Scroll the mouse wheel. Positive = up, negative = down (clicks of ~3 lines). Optional x,y to "
      "scroll over a specific spot.", "desktop",
      {"amount": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"}}, ["amount"])
def scroll(ctx, amount, x=None, y=None):
    pg = _pg()
    if x is not None and y is not None:
        pg.moveTo(*_map(x, y))
    pg.scroll(int(amount) * 120 if IS_WIN else int(amount))
    return f"Scrolled {amount}"


def _clipboard_cmd():
    import shutil
    if IS_WIN:
        return ["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"]
    if IS_MAC:
        return ["pbcopy"]
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"]
    return None


def _clipboard_read():
    import shutil
    cmd = (["pbpaste"] if IS_MAC else ["wl-paste", "--no-newline"] if os.environ.get("WAYLAND_DISPLAY") and
           shutil.which("wl-paste") else ["xclip", "-selection", "clipboard", "-o"] if shutil.which("xclip") else
           ["xsel", "--clipboard", "--output"] if shutil.which("xsel") else None)
    if not cmd:
        return None
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=5)
        return p.stdout if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _clipboard_write(text):
    cmd = _clipboard_cmd()
    if not cmd:
        return False
    try:
        p = subprocess.run(cmd, input=text, text=True, encoding="utf-8", creationflags=osinfo.NO_WINDOW, timeout=10)
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _paste_text(text):
    """Non-ASCII text on macOS/Linux goes through the clipboard (pyautogui can only type plain keys); the clipboard's
    previous text is put back afterwards."""
    old = _clipboard_read()
    if not _clipboard_write(text):
        return False
    _pg().hotkey("command" if IS_MAC else "ctrl", "v")
    time.sleep(0.2)
    if old is not None:
        _clipboard_write(old)
    return True


@tool("type_text", "Type text at the current keyboard focus. Click the target field first.", "desktop",
      {"text": {"type": "string"}, "enter": {"type": "boolean", "description": "Press Enter afterwards."}},
      ["text"], summary=lambda a: repr(a.get("text", ""))[:80])
def type_text(ctx, text, enter=False):
    """Typed in short chunks so Stop takes effect mid-text. On Windows every character goes through SendInput's
    Unicode mode, so the clipboard is never touched; elsewhere non-ASCII text is pasted and the previous clipboard
    text is put back."""
    pg = _pg()
    stop = getattr(ctx, "cancelled", lambda: False)
    done = 0
    if IS_WIN:
        for i in range(0, len(text), 40):
            if stop():
                return {"text": f"Stopped after typing {done} of {len(text)} chars.", "error": True}
            chunk = text[i:i + 40]
            for line_i, part in enumerate(chunk.split("\n")):
                if line_i:
                    pg.press("enter")
                _send_unicode(part)
            done += len(chunk)
    elif text.isascii():
        for i in range(0, len(text), 40):
            if stop():
                return {"text": f"Stopped after typing {done} of {len(text)} chars.", "error": True}
            pg.write(text[i:i + 40], interval=0.01)
            done += len(text[i:i + 40])
    elif not _paste_text(text):
        pg.write(text, interval=0.01)
    if enter:
        pg.press("enter")
    return f"Typed {len(text)} chars" + (" + Enter" if enter else "")


def _send_unicode(text):
    """Windows SendInput with KEYEVENTF_UNICODE: any character, no clipboard, no keyboard-layout guessing."""
    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    units = text.encode("utf-16-le")
    seq = []
    for i in range(0, len(units), 2):
        code = int.from_bytes(units[i:i + 2], "little")
        for flags in (0x0004, 0x0004 | 0x0002):          # KEYEVENTF_UNICODE, then with KEYEVENTF_KEYUP
            inp = INPUT(type=1)
            inp.ki = KEYBDINPUT(0, code, flags, 0, 0)
            seq.append(inp)
    if seq:
        arr = (INPUT * len(seq))(*seq)
        ctypes.windll.user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))
        time.sleep(0.004 * len(text))


@tool("press_keys", "Press a key or chord, e.g. 'enter', 'ctrl+c', 'alt+tab', 'win+r', 'ctrl+shift+esc'. "
      "Separate several presses with spaces: 'ctrl+a delete'.", "desktop",
      {"keys": {"type": "string"}}, ["keys"], summary=lambda a: a.get("keys", ""))
def press_keys(ctx, keys):
    pg = _pg()
    alias = {"win": "winleft", "windows": "winleft", "cmd": "winleft", "return": "enter", "esc": "escape",
             "del": "delete", "pgup": "pageup", "pgdn": "pagedown", "control": "ctrl"}
    for chord in keys.split():
        parts = [alias.get(k.lower(), k.lower()) for k in chord.split("+") if k]
        if len(parts) == 1:
            pg.press(parts[0])
        else:
            pg.hotkey(*parts)
        time.sleep(0.05)
    return f"Pressed {keys}"


@tool("list_windows", "List visible top-level windows (titles) so you can focus one.", "screen", {})
def list_windows(ctx):
    if not IS_WIN:
        return "list_windows is only available on Windows."
    import pygetwindow as gw
    out = []
    for w in gw.getAllWindows():
        if w.title.strip() and w.visible and w.width > 50:
            out.append(f"- {w.title}  [{w.left},{w.top} {w.width}x{w.height}]{' (minimized)' if w.isMinimized else ''}")
    return "\n".join(out) or "No windows."


@tool("focus_window", "Bring a window to the front by (partial) title.", "desktop",
      {"title": {"type": "string"}}, ["title"], summary=lambda a: a.get("title", ""))
def focus_window(ctx, title):
    if not IS_WIN:
        return {"text": "Windows only.", "error": True}
    import pygetwindow as gw
    wins = [w for w in gw.getAllWindows() if title.lower() in w.title.lower() and w.title.strip()]
    if not wins:
        return {"text": f"No window matching '{title}'.", "error": True}
    w = wins[0]
    try:
        if w.isMinimized:
            w.restore()
        _pg().press("alt")      # lets SetForegroundWindow succeed from a background process
        w.activate()
    except Exception:
        pass
    return f"Focused '{w.title}'"


_OPEN_EXAMPLES = ("'notepad', 'C:\\\\Users', 'https://example.com', 'ms-settings:'" if IS_WIN else
                  "'Safari', 'TextEdit', '~/Documents', 'https://example.com'" if IS_MAC else
                  "'gnome-calculator', 'firefox', '~/Documents', 'https://example.com'")


@tool("open_app", f"Open an application, file, folder or URL with the system's default handler (e.g. {_OPEN_EXAMPLES}).",
      "desktop", {"target": {"type": "string"}}, ["target"], summary=lambda a: a.get("target", ""))
def open_app(ctx, target):
    osinfo.launch_app(target)
    time.sleep(1.0)
    return f"Opened {target}"


@tool("wait", "Wait a number of seconds (max 30) for something on screen to finish.", "screen",
      {"seconds": {"type": "number"}}, ["seconds"])
def wait(ctx, seconds):
    s = max(0.1, min(float(seconds), 30))
    time.sleep(s)
    return f"Waited {s:.1f}s"


# Screenshots and synthetic input need a desktop session this process can reach (X11 on Linux; on macOS the
# Screen Recording and Accessibility permissions). Window listing and focusing use Windows' window manager.
for _n in ("screenshot", "mouse_click", "mouse_move", "mouse_drag", "scroll", "type_text", "press_keys"):
    REGISTRY[_n].available = osinfo.can_drive_desktop
for _n in ("list_windows", "focus_window"):
    REGISTRY[_n].available = lambda: IS_WIN
