"""The user's own browser, used on screen without its remote-debugging switch (1.2.3, Windows).

Aero opens a new window of the user's default browser with the address (their everyday profile, so it is signed in
wherever they are) and works in it the way it works in any app (apps.py): a picture of the window with numbered
controls read through UI Automation, links and buttons pressed through UI Automation, fields set and read back.
Chromium exposes the whole page to UI Automation once something asks for it. Measured on Chrome 141 / Windows 11:
link Invoke, the Back button, a field's value plus a posted Enter, and reading the address bar all work while the
window is in the background.

Aero only looks at the windows it opened, never the user's other windows or tabs, and closes them when the request is
done (left open if the user added tabs to one). Nothing is copied or decrypted from the profile.
"""
import json
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from . import apps
from ..config import IS_WIN

_WIN = {}        # owner -> {"hwnd", "name", "exe", "ctx"}: the window Aero opened for that agent

WM_CLOSE, WM_KEYDOWN, WM_CHAR, WM_KEYUP = 0x0010, 0x0100, 0x0102, 0x0101


def supported():
    """The user's default browser when this mode can work with it (Windows, Chromium-based), else None."""
    if not IS_WIN:
        return None
    from .. import osinfo
    return osinfo.user_browser()


def active(owner):
    w = _WIN.get(owner)
    if w and not apps.user32.IsWindow(w["hwnd"]):
        _WIN.pop(owner, None)
        w = None
    return w


def profile_args(user_data):
    """Open in the profile the user last used. With one profile there's nothing to pick, and no profile chooser is
    shown either way (that only appears at startup when the user asked for it)."""
    try:
        st = json.loads((Path(user_data) / "Local State").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    prof = st.get("profile") or {}
    cache, last = prof.get("info_cache") or {}, prof.get("last_used")
    return [f"--profile-directory={last}"] if len(cache) > 1 and last in cache else []


def _browser_windows(proc):
    return {w["id"]: w for w in apps.windows() if (w.get("app") or "").lower() == proc}


def _launch(ub, url, timeout=20.0):
    """Start a new window of the user's browser on url. Returns its window handle, or None."""
    exe = ub["exe"]
    proc = Path(exe).name.lower()
    before = set(_browser_windows(proc))
    flags = 0x00000008 | 0x00000200            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: outlives Aero
    subprocess.Popen([exe, *profile_args(ub["user_data"]), "--new-window", url], creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    host = (urlparse(url).hostname or "").removeprefix("www.")
    t0 = time.time()
    while time.time() - t0 < timeout:
        new = [h for h in _browser_windows(proc) if h not in before]
        if new:
            if len(new) == 1:
                return new[0]
            # the browser was closed and restored its last session too: the window showing our address
            for h in new:
                if host and host in (apps._on_ui_thread(_address)(h) or ""):
                    return h
            return new[0]
        time.sleep(0.25)
    return None


# ---- UI Automation (on apps' UI thread) -----------------------------------------------------------------------

def _walk(root, depth):
    auto = apps._uia()
    try:
        yield from auto.WalkControl(root, maxDepth=depth)
    except Exception:  # noqa: BLE001 (pages change while they load)
        return


def _doc(hwnd):
    """The web page's document (Chromium: AutomationId RootWebArea; the other documents are the browser's own)."""
    auto = apps._uia()
    root = auto.ControlFromHandle(hwnd)
    named = None
    for c, _ in _walk(root, 16):
        try:
            if c.ControlTypeName != "DocumentControl":
                continue
            if c.AutomationId == "RootWebArea":
                return c
            if named is None and c.Name and not c.ClassName:
                named = c
        except Exception:  # noqa: BLE001
            continue
    return named


@apps._on_ui_thread
def _wait_page(hwnd, timeout=15.0):
    """Ask for the page through UI Automation (Chromium turns its accessibility on at the first request) and wait
    until it has content."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            d = _doc(hwnd)
            if d is not None and d.GetFirstChildControl() is not None:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


def _omnibox(hwnd):
    auto = apps._uia()
    root = auto.ControlFromHandle(hwnd)
    for c, _ in _walk(root, 12):
        try:
            if c.ControlTypeName == "EditControl" and "address" in (c.Name or "").lower():
                return c
        except Exception:  # noqa: BLE001
            continue
    return None


def _address(hwnd):
    try:
        e = _omnibox(hwnd)
        v = e.GetValuePattern().Value if e is not None else ""
    except Exception:  # noqa: BLE001
        return ""
    if v and "://" not in v and not v.startswith(("about:", "chrome:", "edge:")):
        v = "https://" + v
    return v


@apps._on_ui_thread
def _page_facts(hwnd):
    """(url, title, has a password field)."""
    d = _doc(hwnd)
    pw = False
    if d is not None:
        for c, _ in _walk(d, 40):
            try:
                if c.ControlTypeName == "EditControl" and c.IsPassword:
                    pw = True
                    break
            except Exception:  # noqa: BLE001
                continue
    return _address(hwnd), (d.Name if d is not None else apps._title(hwnd)), pw


@apps._on_ui_thread
def _navigate(hwnd, url):
    """Type the address in the window's address bar and press Enter (both in the background)."""
    e = _omnibox(hwnd)
    if e is None:
        return False
    with apps.FocusGuard():
        e.GetValuePattern().SetValue(url)
        _enter(hwnd)
    return True


def _enter(hwnd):
    apps.user32.PostMessageW(hwnd, WM_KEYDOWN, 0x0D, 0x001C0001)
    apps.user32.PostMessageW(hwnd, WM_CHAR, 0x0D, 0x001C0001)
    apps.user32.PostMessageW(hwnd, WM_KEYUP, 0x0D, 0xC01C0001)


def _post_key(hwnd, vk):
    apps.user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 1)
    apps.user32.PostMessageW(hwnd, WM_KEYUP, vk, 0xC0000001)


@apps._on_ui_thread
def _press_toolbar(hwnd, name):
    auto = apps._uia()
    for c, _ in _walk(auto.ControlFromHandle(hwnd), 12):
        try:
            if c.ControlTypeName == "ButtonControl" and c.Name == name:
                try:
                    c.GetInvokePattern().Invoke()
                except Exception:  # noqa: BLE001
                    c.GetLegacyIAccessiblePattern().DoDefaultAction()
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


@apps._on_ui_thread
def _scroll_page(hwnd, down):
    """Scroll by about a window: the page's ScrollPattern, else bring the first thing below (or above) the window
    into view. Returns a description of what moved, or None."""
    d = _doc(hwnd)
    if d is None:
        return None
    try:
        sp = d.GetScrollPattern()
        if sp and sp.VerticallyScrollable:
            before = sp.VerticalScrollPercent
            sp.Scroll(2, 3 if down else 0)                          # NoAmount, LargeIncrement / LargeDecrement
            time.sleep(0.2)
            after = d.GetScrollPattern().VerticalScrollPercent
            if after != before:
                return f"{before:.0f}% -> {after:.0f}% of the page"
    except Exception:  # noqa: BLE001
        pass
    try:
        r = d.BoundingRectangle
        top, bottom = r.top, r.bottom
    except Exception:  # noqa: BLE001
        return None
    pick = None
    for c, _ in _walk(d, 60):
        try:
            if c.ControlTypeName not in ("TextControl", "HyperlinkControl", "ButtonControl", "EditControl",
                                         "ImageControl"):
                continue
            b = c.BoundingRectangle
            if b.height() <= 0:
                continue
            if down and b.top >= bottom:
                pick = c
                break
            if not down and b.bottom <= top:
                pick = c                                            # the last one above the window
            elif not down and pick is not None:
                break
        except Exception:  # noqa: BLE001
            continue
    if pick is None:
        return None
    try:
        y0 = pick.BoundingRectangle.top
        pick.GetScrollItemPattern().ScrollIntoView()
        time.sleep(0.2)
        moved = abs(pick.BoundingRectangle.top - y0)
        return f"moved {moved} px" if moved else None
    except Exception:  # noqa: BLE001
        return None


@apps._on_ui_thread
def _page_text(hwnd, max_chars=40000):
    """The page's text in reading order, the parts below the window too; headings marked with #."""
    d = _doc(hwnd)
    if d is None:
        return ""
    out, seen, n, t0 = [], set(), 0, time.time()
    for c, _ in _walk(d, 60):
        if n > max_chars or time.time() - t0 > 10:
            break
        try:
            kind = c.ControlTypeName
            if kind not in ("TextControl", "HyperlinkControl", "ListItemControl", "DataItemControl", "ButtonControl",
                            "HeaderItemControl"):
                continue
            name = (c.Name or "").strip()
            if not name or (kind != "TextControl" and name in seen):
                continue
            if kind == "TextControl" and "heading" in (c.LocalizedControlType or "").lower():
                name = "# " + name
            seen.add(name)
            out.append(name)
            n += len(name) + 1
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(out)[:max_chars]


@apps._on_ui_thread
def _select(owner, ref, option):
    sess = apps.session(owner)
    c = apps._element(sess, ref)
    try:
        c.GetExpandCollapsePattern().Expand()
        time.sleep(0.2)
    except Exception:  # noqa: BLE001
        pass
    want = str(option).strip().lower()
    for o, _ in _walk(c, 6):
        try:
            if o.ControlTypeName == "ListItemControl" and (o.Name or "").strip().lower() == want:
                o.GetSelectionItemPattern().Select()
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


@apps._on_ui_thread
def _tab_count(hwnd):
    auto = apps._uia()
    return sum(1 for c, _ in _walk(auto.ControlFromHandle(hwnd), 12) if c.ControlTypeName == "TabItemControl")


# ---- what the browser tools call ------------------------------------------------------------------------------

def _head(owner, w):
    url, title, pw = _page_facts(w["hwnd"])
    w["url"] = url
    from .browser import SIGNIN_NOTE, _SIGNIN
    sign = ("\n" + SIGNIN_NOTE) if pw or _SIGNIN.search(url or "") else ""
    return (f"[In your own {w['name']} window (signed in as you). Element numbers are from the picture's control list; "
            f"browser_click / browser_type use them.]\nURL: {url}\nTitle: {title}"), sign


def view(ctx, owner, elements=True):
    w = active(owner)
    res = apps.app_view(ctx, window=str(w["hwnd"]), elements=elements)
    head, sign = _head(owner, w)
    res["text"] = head + "\n" + res["text"] + sign
    return res


def open_url(ctx, owner, url, new_tab=False):
    """browser_open: a new window of the user's browser, or the agent's window going to a new address."""
    ub = supported()
    w = active(owner)
    if w and not new_tab:
        old = w.get("url", "")
        if _navigate(w["hwnd"], url):
            t0 = time.time()
            while time.time() - t0 < 10:
                time.sleep(0.4)
                cur = _page_facts(w["hwnd"])[0]
                if cur and cur != old:
                    break
            _wait_page(w["hwnd"])
            return view(ctx, owner)
    hwnd = _launch(ub, url)
    if not hwnd:
        raise RuntimeError(f"{ub['name']} didn't open a window.")
    prev = _WIN.get(owner)
    _WIN[owner] = {"hwnd": hwnd, "name": ub["name"], "exe": ub["exe"], "ctx": ctx, "url": url,
                   "extra": (prev or {}).get("extra", []) + ([prev["hwnd"]] if prev and new_tab else [])}
    _wait_page(hwnd)
    return view(ctx, owner)


@apps._on_ui_thread
def _in_page(owner, hwnd, ref):
    """True when element [ref] is part of the web page, not the browser's own address bar, menus or profile button
    (the user's browser: Aero leaves those alone). Else a reason."""
    try:
        c = apps._element(apps.session(owner), ref)
    except apps.StaleElement as e:
        return str(e)
    d = _doc(hwnd)
    if d is None:
        return "The page isn't readable right now: call browser_snapshot again."
    auto = apps._uia()
    for _ in range(80):
        try:
            if auto.ControlsAreSame(c, d):
                return True
            c = c.GetParentControl()
        except Exception:  # noqa: BLE001
            c = None
        if c is None:
            break
    return (f"[{ref}] is part of the browser itself (address bar, toolbar or menus), not the page. Use browser_open "
            "for an address and browser_back to go back; Aero leaves the rest of your browser alone.")


def _check(owner, ref=None):
    """The agent's element ids belong to its view of this window (a view of another app in between replaced them),
    and only the page's elements are used."""
    w = active(owner)
    if apps.selected_hwnd(owner) != w["hwnd"]:
        return {"text": "Your latest view was of another window: call browser_snapshot for fresh element numbers.",
                "error": True}
    if ref is not None:
        ok = _in_page(owner, w["hwnd"], ref)
        if ok is not True:
            return {"text": ok, "error": True}
    return None


def _after(ctx, owner, res):
    if isinstance(res, dict):
        w = active(owner)
        if w:
            head, sign = _head(owner, w)
            res["text"] = f"URL: {w['url']}\n" + (res.get("text") or "") + sign
    return res


def click(ctx, owner, ref):
    return _check(owner, ref) or _after(ctx, owner, apps.app_click(ctx, element=int(ref)))


@apps._on_ui_thread
def _set_field(owner, hwnd, ref, text, submit):
    """Set a page field's value through UI Automation and read it back; never characters posted to the window
    (they would land in whatever has focus there). Enter goes in while the browser still has the field focused:
    once Aero has put the user's window back in front, the browser ignores a posted Enter (measured)."""
    c = apps._element(apps.session(owner), ref)
    try:
        if c.IsPassword:
            return "password", None
    except Exception:  # noqa: BLE001
        pass
    try:
        vp = c.GetValuePattern()
        if not vp or vp.IsReadOnly:
            raise ValueError
    except Exception:  # noqa: BLE001
        return "nofield", None
    with apps.FocusGuard():
        vp.SetValue(text)
        got = None
        try:
            got = c.GetValuePattern().Value
        except Exception:  # noqa: BLE001
            pass
        if submit:
            _enter(hwnd)
            time.sleep(0.4)
    _WIN[owner]["field"] = c
    return "ok", got


def type_text(ctx, owner, ref, text, submit=False):
    if (bad := _check(owner, ref)):
        return bad
    w = active(owner)
    try:
        how, got = _set_field(owner, w["hwnd"], ref, str(text), bool(submit))
    except apps.StaleElement as e:
        return {"text": str(e), "error": True}
    if how == "password":
        return {"text": "That is a password field. Aero never types passwords: ask the user to sign in themselves.",
                "error": True}
    if how == "nofield":
        return {"text": f"[{ref}] isn't a text field (no value to set). Pick the field itself from the latest "
                        "browser_snapshot.", "error": True}
    if submit:
        time.sleep(0.8)
        _wait_page(w["hwnd"], 8)
    v = view(ctx, owner)
    ok = got is not None and got.strip() == str(text).strip()
    v["text"] = (f"Set [{ref}] to {str(text)[:80]!r}" + (" (read back)" if ok else " (couldn't read it back)") +
                 (" + Enter" if submit else "") + "\n" + v["text"])
    return v


def select(ctx, owner, ref, option):
    if (bad := _check(owner, ref)):
        return bad
    try:
        ok = _select(owner, ref, option)
    except apps.StaleElement as e:
        return {"text": str(e), "error": True}
    if not ok:
        return {"text": f"No option '{option}' in [{ref}]. Click it with browser_click to see its options.",
                "error": True}
    return view(ctx, owner)


def press(ctx, owner, key):
    k = str(key or "").strip().lower().replace("arrow", "")
    if "+" in k and len(k) > 1:
        return {"text": f"Shortcuts like {key} can't be pressed in your own browser window in the background: click "
                        "the control instead.", "error": True}
    vk = apps.VK.get(k)
    if vk is None and len(k) == 1:
        vk = apps.user32.VkKeyScanW(k) & 0xFF
    if vk is None:
        return {"text": f"Unknown key '{key}'.", "error": True}
    w = active(owner)
    _key(owner, w["hwnd"], vk)
    time.sleep(0.6)
    return view(ctx, owner)


@apps._on_ui_thread
def _key(owner, hwnd, vk):
    """A key for the field Aero typed in last (Enter, Tab, Escape), else for the page (PageDown, Home...). The browser
    takes posted keys only while it is the active window, so this happens inside a FocusGuard (the user's window
    comes back right after)."""
    target = _WIN.get(owner, {}).get("field") if vk in (0x0D, 0x09, 0x1B) else None
    target = target or _doc(hwnd)
    with apps.FocusGuard():
        try:
            target.SetFocus()
        except Exception:  # noqa: BLE001
            pass
        if vk == 0x0D:
            _enter(hwnd)
        else:
            _post_key(hwnd, vk)
        time.sleep(0.4)


def scroll(ctx, owner, down=True):
    w = active(owner)
    got = _scroll_page(w["hwnd"], down)
    v = view(ctx, owner)
    v["text"] = (f"Scrolled {'down' if down else 'up'} ({got}).\n" if got else
                 f"Nothing to scroll {'down' if down else 'up'} to: this is the {'end' if down else 'top'} of the "
                 "page. browser_read reads the whole page at once.\n") + v["text"]
    return v


def back(ctx, owner):
    w = active(owner)
    if not _press_toolbar(w["hwnd"], "Back"):
        return {"text": "There is no page to go back to.", "error": True}
    time.sleep(1.0)
    _wait_page(w["hwnd"], 8)
    return view(ctx, owner)


def read(owner, max_chars=40000):
    w = active(owner)
    url, title, _ = _page_facts(w["hwnd"])
    txt = _page_text(w["hwnd"], int(max_chars or 40000))
    return f"URL: {url}\nTitle: {title}\n\n{txt or '(no text found on this page)'}"


def wait_for(owner, text="", timeout=15):
    w = active(owner)
    t0 = time.time()
    while time.time() - t0 < float(timeout or 15):
        if not text:
            if _wait_page(w["hwnd"], 2):
                return "The page has loaded."
        elif text.lower() in _page_text(w["hwnd"], 200000).lower():
            return f"The page shows '{text}'."
        time.sleep(0.5)
    return {"text": f"Not seen within {timeout} s" + (f": '{text}'" if text else ""), "error": True}


def tabs(owner):
    w = active(owner)
    url, title, _ = _page_facts(w["hwnd"])
    return f"Your own {w['name']} window that Aero opened: 0: {title} ({url}). Aero doesn't see your other tabs."


def page_url(owner):
    w = active(owner)
    return (w or {}).get("url", "")


def show(owner):
    w = active(owner)
    apps.display(w["hwnd"])
    return _page_facts(w["hwnd"])[0]


def snapshot_again(owner):
    w = active(owner)
    return view(w["ctx"], owner)["text"]


def close(owner):
    """The request is done: close the windows Aero opened, unless the user added tabs to one (theirs now)."""
    w = _WIN.pop(owner, None)
    if not w:
        return
    for h in [w["hwnd"], *w.get("extra", [])]:
        try:
            if apps.user32.IsWindow(h) and _tab_count(h) <= 1:
                apps.user32.PostMessageW(h, WM_CLOSE, 0, 0)
        except Exception:  # noqa: BLE001
            pass
    apps.forget(owner)
