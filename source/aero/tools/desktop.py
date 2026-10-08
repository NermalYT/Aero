"""Screen capture and mouse/keyboard control (Claude Code / Codex "computer use" style).

Coordinates the model sends are in the pixel space of the most recent screenshot of that
monitor; they are mapped back to real desktop pixels here, so downscaling is transparent.
"""
import io
import os
import subprocess
import threading
import time

from . import tool
from .. import attachments
from ..config import IS_WIN

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


def _paste_text(text):
    if IS_WIN:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"], input=text,
                           text=True, encoding="utf-8", creationflags=0x08000000)
        if p.returncode == 0:
            _pg().hotkey("ctrl", "v")
            return True
    return False


@tool("type_text", "Type text at the current keyboard focus. Click the target field first.", "desktop",
      {"text": {"type": "string"}, "enter": {"type": "boolean", "description": "Press Enter afterwards."}},
      ["text"], summary=lambda a: repr(a.get("text", ""))[:80])
def type_text(ctx, text, enter=False):
    pg = _pg()
    if text.isascii() and len(text) < 400:
        pg.write(text, interval=0.01)
    elif not _paste_text(text):
        pg.write(text, interval=0.01)
    if enter:
        pg.press("enter")
    return f"Typed {len(text)} chars" + (" + Enter" if enter else "")


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


@tool("open_app", "Open an application, file, folder or URL with Windows' default handler "
      "(e.g. 'notepad', 'C:\\\\Users', 'https://example.com', 'ms-settings:').", "desktop",
      {"target": {"type": "string"}}, ["target"], summary=lambda a: a.get("target", ""))
def open_app(ctx, target):
    if IS_WIN:
        try:
            os.startfile(target)
        except OSError:
            subprocess.Popen(["cmd", "/c", "start", "", target], creationflags=0x08000000)
    else:
        subprocess.Popen(["xdg-open", target])
    time.sleep(1.0)
    return f"Opened {target}"


@tool("wait", "Wait a number of seconds (max 30) for something on screen to finish.", "screen",
      {"seconds": {"type": "number"}}, ["seconds"])
def wait(ctx, seconds):
    s = max(0.1, min(float(seconds), 30))
    time.sleep(s)
    return f"Waited {s:.1f}s"
