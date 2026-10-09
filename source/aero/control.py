"""The "is controlling" signal: while a model drives the mouse, keyboard, an app or the automated browser, Aero says
so at the top of its own window and in a banner at the top of the app being controlled, both with a Stop button.

Which actions count: anything that sends input or navigates (clicks, typing, keys, scrolling, switching or opening
windows, browser actions). Looking (screenshots, reading an app or a page) does not.
"""
import re

from .config import IS_WIN

CONTROL_TOOLS = {
    "mouse_click", "mouse_move", "mouse_drag", "scroll", "type_text", "press_keys", "focus_window", "open_app",
    "app_click", "app_type", "app_keys", "app_scroll",
    "browser_open", "browser_click", "browser_type", "browser_select", "browser_press", "browser_scroll", "browser_back",
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


def target(name, args):
    """(what is being controlled, its screen rectangle or None)."""
    args = args or {}
    if name.startswith("browser_"):
        return "the browser", None
    if name.startswith("app_"):
        try:
            from .tools import apps
            s = apps._sel
            if s.get("hwnd"):
                w, h = (s.get("w") or 0) * (s.get("scale") or 1), (s.get("h") or 0) * (s.get("scale") or 1)
                return short_app(s.get("title")) or "an app", (s["left"], s["top"], s["left"] + w, s["top"] + h)
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


def banner_on(text, rect=None):
    try:
        from . import overlay
        overlay.banner(text, rect)
    except Exception:
        pass


def banner_off():
    try:
        from . import overlay
        overlay.banner(None)
    except Exception:
        pass
