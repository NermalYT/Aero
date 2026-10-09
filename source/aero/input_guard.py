"""The user's real mouse and keyboard are one shared resource. This module decides when an agent may use them.

Background tools (UI Automation, window messages, the automated browser, files) never come here. Tools that move the
real cursor or type on the real keyboard (pyautogui: mouse_*, scroll, type_text, press_keys, focus_window, and the
app_* tools with input="real") do, and only through this sequence:

  1. Strict Background Only (Settings > Tools) refuses them outright: no "just one click" fallback.
  2. Otherwise the user grants foreground control first: once, or for the rest of this task (the "Foreground control
     required" card). A desktop permission of "auto" in Settings counts as a standing grant.
  3. Aero waits until the user has stopped typing and moving the mouse (Windows: GetLastInputInfo; elsewhere this is
     unknown, so the grant itself is the handoff).
  4. One exclusive lock: two agents never own the real input at the same time.
  5. Afterwards the window that was in front before is put back when the agent changed it, and the result says
     whether that worked.

Only input timing is read (milliseconds since the last input event). Keys are never logged or hooked.
"""
import ctypes
import sys
import threading
import time

from . import resources

IS_WIN = sys.platform == "win32"
IDLE_MS = 1200              # the user counts as idle after this long without input
IDLE_WAIT_S = 20            # how long Aero waits for the user to stop before giving up on this action
LOCK_WAIT_S = 30

_grants = {}                # chat id -> {"scope": "once" | "task", "at": time}
_lock = threading.Lock()


def strict(settings):
    return bool((settings or {}).get("strict_background"))


# ---- grants ---------------------------------------------------------------------------------------------

def grant(chat_id, scope="once"):
    with _lock:
        _grants[chat_id] = {"scope": "task" if scope == "task" else "once", "at": time.time()}


def has_grant(chat_id):
    with _lock:
        return chat_id in _grants


def consume(chat_id):
    """Use the grant for one action: a one-time grant is gone afterwards."""
    with _lock:
        g = _grants.get(chat_id)
        if g and g["scope"] == "once":
            _grants.pop(chat_id, None)


def clear(chat_id):
    with _lock:
        _grants.pop(chat_id, None)


def grants():
    with _lock:
        return {k: dict(v) for k, v in _grants.items()}


# ---- user activity ----------------------------------------------------------------------------------------

def idle_ms():
    """Milliseconds since the user last touched the mouse or keyboard, or None where the OS doesn't say."""
    if not IS_WIN:
        return None
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        li = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
            return None
        return (ctypes.windll.kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF
    except Exception:
        return None


def foreground():
    if not IS_WIN:
        return None
    try:
        return int(ctypes.windll.user32.GetForegroundWindow() or 0) or None
    except Exception:
        return None


def cursor():
    if not IS_WIN:
        return None
    try:
        from ctypes import wintypes
        p = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
        return p.x, p.y
    except Exception:
        return None


def restore_foreground(hwnd):
    """Bring the user's previous window back. True when it is in front afterwards."""
    if not IS_WIN or not hwnd:
        return False
    try:
        u = ctypes.windll.user32
        if not u.IsWindow(hwnd):
            return False
        if foreground() == hwnd:
            return True
        try:
            import pyautogui
            pyautogui.press("alt")            # lets SetForegroundWindow work from a background process
        except Exception:
            pass
        u.SetForegroundWindow(hwnd)
        time.sleep(0.12)
        return foreground() == hwnd
    except Exception:
        return False


# ---- the guarded run ----------------------------------------------------------------------------------------

class Session:
    """One guarded use of the real input: idle wait and lock are done by the caller (agent.exec_tool, async);
    this records what was in front and puts it back."""

    def __init__(self, owner):
        self.owner = owner
        self.fg_before = None
        self.cursor_before = None
        self.restored = None

    def __enter__(self):
        self.fg_before = foreground()
        self.cursor_before = cursor()
        return self

    def __exit__(self, *a):
        if IS_WIN and self.fg_before and foreground() != self.fg_before:
            self.restored = restore_foreground(self.fg_before)
        else:
            self.restored = True
        return False

    def note(self):
        if self.restored is False:
            return " The window you had in front could not be put back automatically."
        return ""


def lock_key():
    return resources.EXCLUSIVE_INPUT


def owner_of_input():
    h = resources.holder(resources.EXCLUSIVE_INPUT)
    return h["owner"] if h else None
