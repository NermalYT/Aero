"""The "is using" signal: while a model drives the mouse, keyboard, an app or a browser, Aero says so at the top of its
own window, with a Stop button: "<model> is using <app>", plus "with your mouse and keyboard" when the real input is
in use (only after the user allowed foreground control). On screen, a banner with Stop sits on the window being used
and a blue outline goes around it; both follow that window (overlay.py). Background browsing has no window, so it gets
no on-screen banner.

Which actions count: anything that sends input or navigates (clicks, typing, keys, scrolling, switching or opening
windows, browser actions). Looking (screenshots, reading an app or a page) does not.
"""
import re

from .config import IS_WIN

CONTROL_TOOLS = {
    "mouse_click", "mouse_move", "mouse_drag", "scroll", "type_text", "press_keys", "focus_window", "open_app",
    "app_click", "app_type", "app_keys", "app_scroll", "app_launch",
    "browser_open", "browser_click", "browser_type", "browser_select", "browser_press", "browser_scroll", "browser_back",
    "browser_tabs",
}


def short_app(title):
    """ "notes.txt - Notepad" -> "Notepad"; "Inbox (3) - user@x.com - Outlook" -> "Outlook"."""
    t = re.sub(r"\s+", " ", title or "").strip()
    parts = [p.strip() for p in re.split(r" [-–—|] ", t) if p.strip()]
    name = parts[-1] if len(parts) > 1 and len(parts[-1]) <= 40 else t
    return name[:48] or ""


def _foreground():
    """Title and rectangle (left, top, right, bottom) of the window in front, on Windows."""
    if not IS_WIN:
        return "", None
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        r = wintypes.RECT()
        u.GetWindowRect(h, ctypes.byref(r))
        return buf.value, (r.left, r.top, r.right, r.bottom)
    except Exception:
        return "", None


def target(name, args, owner=None):
    """(what is being controlled, its screen rectangle or None)."""
    args = args or {}
    if name.startswith("browser_"):
        try:
            from .tools import browser
            return browser.display_name(owner), None
        except Exception:  # noqa: BLE001
            return "Aero's browser", None
    if name == "app_launch":
        return short_app(str(args.get("app") or "")) or "an app", None
    if name.startswith("app_"):
        try:
            from .tools import apps
            title, rect = apps.view_info(owner)
            if title or rect:
                return short_app(title) or "an app", rect
        except Exception:
            pass
        return "an app", None
    if name == "open_app":
        what = str(args.get("target") or args.get("path") or args.get("name") or "").strip()
        what = re.sub(r"\.(exe|lnk|bat|cmd)$", "", what.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1], flags=re.I)
        if what.islower() and "." not in what and ":" not in what:
            what = what[:1].upper() + what[1:]     # "notepad" -> "Notepad"
        return short_app(what) or "your PC", None
    if name == "focus_window":
        return short_app(str(args.get("title") or "")) or "your PC", None
    title, rect = _foreground()
    if title and not re.search(r"\bAero\b", title):
        return short_app(title), rect
    return "your PC", None


def window(name, owner=None):
    """The window being used, for the outline and to keep the banner on it (overlay.py), or None."""
    if not IS_WIN:
        return None
    try:
        if name.startswith("browser_"):
            from .tools import browser
            return browser.window_of(owner)
        if name.startswith("app_"):
            from .tools import apps
            return apps.selected_hwnd(owner)
        if name in ("open_app", "focus_window"):
            return None
        import ctypes
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        h = u.GetForegroundWindow()
        title, _ = _foreground()
        return h if h and title and not re.search(r"\bAero\b", title) else None
    except Exception:  # noqa: BLE001
        return None


def mode_of(name, args, physical=False):
    """How a control tool acts: a capabilities.CONTROL_MODES key, or LAUNCH for starting an app."""
    if physical:
        return "FOREGROUND_CONSENT_REQUIRED"
    if name.startswith("browser_"):
        return "BROWSER_ISOLATED"
    if name in ("open_app", "app_launch"):
        return "LAUNCH"
    if name.startswith("app_"):
        return "ACCESSIBILITY_BACKGROUND" if (args or {}).get("element") else "WINDOW_MESSAGE_BACKGROUND"
    return "FOREGROUND_CONSENT_REQUIRED"           # mouse_*, type_text, press_keys, focus_window: the real input


def phrase(by, what, mode):
    """'Qwen3.8 is using Notepad' and the like (1.2.3: the owner asked for "MODEL is using APP"); real input always
    says so, and a hidden browser says it is in the background."""
    by = by or "Aero"
    if mode == "FOREGROUND_CONSENT_REQUIRED":
        return f"{by} is using {what} with your mouse and keyboard"
    if mode == "BROWSER_ISOLATED":                # its own window, never your mouse or keyboard
        return f"{by} is using {what}" + ("" if _browser_shown() else " in the background")
    if mode == "LAUNCH":
        return f"{by} is opening {what}"
    return f"{by} is using {what}"


def _browser_shown():
    """Aero's browser window is (or is about to be) on screen: the running one's state, else the setting."""
    try:
        from .tools.browser import B
        if B.headed is not None:
            return bool(B.headed)
        from .config import load_settings
        return load_settings().get("browser_mode", "visible") != "background"
    except Exception:  # noqa: BLE001
        return False


def on_screen(name, mode):
    """Whether the on-screen banner makes sense: over an app window being driven, or across the screen while the
    real input is in use. Aero's background browser has no window to put it on."""
    if mode == "BROWSER_ISOLATED":
        return _browser_shown()
    return mode != "LAUNCH"


def actor(turn, lane):
    """The model doing the controlling, by name; "Aero" when it isn't known."""
    s = getattr(turn, "settings", {}) or {}
    if lane == "opus":
        from .cloud import NAMES
        m = s.get("fix_model") or "claude-opus-5-5"
        return NAMES.get(m, m)
    if lane == "sol":
        from .chatgpt import name_of
        return name_of(s.get("gpt_fix_model") or "gpt-6.1-sol")
    if lane in ("fable", "astra"):
        return {"fable": "Claude Fable 5.1", "astra": "GPT-6 Astra"}[lane]
    return getattr(turn, "model_name", "") or "Aero"


def banner_on(text, rect=None, hwnd=None):
    try:
        from . import overlay
        overlay.banner(text, rect, hwnd)
    except Exception:
        pass


def banner_off():
    try:
        from . import overlay
        overlay.banner(None)
    except Exception:
        pass
