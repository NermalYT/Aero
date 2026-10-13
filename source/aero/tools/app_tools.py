"""app_find and app_launch: find installed apps by the names people use and start them with proof (app_registry.py).
Both work on Windows, Linux and macOS."""
import re
import time

from . import tool
from .. import action_results, app_catalog, app_registry


def _alive(windows):
    """Is any of these windows still open? (Without a way to check, assume so for the short window that matters.)"""
    if not windows:
        return False
    try:
        import ctypes
        return any(ctypes.windll.user32.IsWindow(int(w["id"])) for w in windows)
    except Exception:  # noqa: BLE001
        return True


def _fmt(c):
    r = c["record"]
    bits = [f'{c["name"]} (id {c["id"]}, confidence {c["confidence"]:.2f}: {c["reason"]})']
    if r.get("exe"):
        bits.append(f'program: {r["exe"]}')
    if r.get("handler"):
        bits.append(f'links handled by: {r["handler"]}')
    ms = [m["method"] for m in r.get("launch") or []]
    if ms:
        bits.append("launch: " + ", ".join(dict.fromkeys(ms)))
    if r.get("trust") == "untrusted":
        bits.append("in a folder other programs can write to: ask the user before starting it")
    e = app_catalog.entry(r.get("catalog")) if r.get("catalog") else None
    if e and e.get("notes"):
        bits.append("note: " + e["notes"])
    if e and e.get("uris"):
        bits.append("deep links: " + "; ".join(f"{k} {v}" for k, v in e["uris"].items()))
    if c.get("related"):
        bits.append("related: " + ", ".join(app_catalog.CATALOG[x]["name"] for x in c["related"]
                                            if x in app_catalog.CATALOG))
    return "- " + "\n  ".join(bits)


@tool("app_find", "Find an installed app (or a known web service) by the name people use for it, e.g. 'bloxstrap', "
      "'vs code', 'my spotify'. Lists the matches with confidence, what was matched, how each can be launched and "
      "notes on how best to work with it. refresh=true re-scans the computer (after installing something).",
      "screen", {"name": {"type": "string"}, "refresh": {"type": "boolean"}}, ["name"],
      summary=lambda a: a.get("name", ""))
def app_find(ctx, name, refresh=False):
    if refresh:
        app_registry.scan(force=True)
    cands = app_registry.resolve(name)
    if not cands:
        return {"text": f'No installed app or known service matches "{name}". Try another name, or refresh=true '
                        "after installing it.", "error": True}
    head = ""
    if cands[0].get("ambiguous"):
        head = ("Several apps match about equally well; ask the user which one they mean before launching "
                "(ask_user):\n")
    return head + "\n".join(_fmt(c) for c in cands)


WEB_BROWSERS = re.compile(r"\b(google ?chrome|chrome|chromium|microsoft ?edge|msedge|edge|brave|vivaldi|firefox|opera|"
                          r"arc|librewolf|waterfox|yandex browser|web ?browser|browser)\b", re.I)
_LAUNCHED = {}           # (owner, app) -> {"at", "windows"}: what this agent launched, to start each app once


def _is_web_browser(app):
    return bool(WEB_BROWSERS.search(str(app or "").replace(".exe", "")))


@tool("app_launch", "Start an installed app and check that it really started (its process or window appeared). "
      "Give the app id from app_find or a name. uri: a deep link the app handles (e.g. "
      "roblox://experiences/start?placeId=123), opened by whatever program is registered for it. args: command-line "
      "arguments. show=true brings it to the front; otherwise it starts without taking your focus where the app "
      "allows it.", "desktop",
      {"app": {"type": "string"}, "uri": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}},
       "show": {"type": "boolean"}, "method": {"type": "string", "enum": ["shortcut", "aumid", "exe", "desktop",
                                                                         "bundle", "command", "protocol", "url"]}},
      ["app"], summary=lambda a: (a.get("app") or "") + (f" · {a['uri']}" if a.get("uri") else ""))
def app_launch(ctx, app, uri="", args=None, show=False, method=None):
    if _is_web_browser(app) and not uri:
        # 1.3.1: "open my chrome and pull up my Modrinth page" made the model launch the user's own Chrome again and
        # again. Aero never uses the user's browser: its own one keeps the user's sign-ins (Settings > Tools)
        return {"text": f"Aero doesn't open the user's own web browser ({app}). It has its own browser, which keeps "
                        "the user's sign-ins: use browser_open with the page's address (it opens on screen) and the "
                        "other browser_* tools. If you don't know the address, web_search for it first.",
                "error": True}
    key = (ctx.owner, str(app).lower())
    was = _LAUNCHED.get(key)
    if was and time.time() - was["at"] < 600 and not uri and _alive(was.get("windows")):
        return {"text": f"{app} was already started for this request (windows: " + "; ".join(
            f'id {w["id"]} "{w["title"]}"' for w in was["windows"][:4]) + "). Don't start it again: use app_view "
            "with a window id.", "error": True}
    if uri and not str(uri).split(":", 1)[0].replace("-", "").replace(".", "").isalnum():
        return {"text": f"'{uri}' is not a link Aero can open.", "error": True}
    if uri and str(uri).split(":", 1)[0].lower() in ("http", "https", "file", "javascript", "data", "vbscript"):
        return {"text": "Use open_app or the browser tools for web pages and files; uri is for app links.", "error": True}
    from .apps import on_screen
    show = bool(show) or on_screen(ctx)                  # apps the model uses are on display unless asked otherwise
    r = app_registry.launch(app, method=method, args=[str(a) for a in (args or [])][:20], uri=uri or None,
                            background=not show, cancel=ctx.cancel)
    if not r.get("ok") and not r.get("verified"):
        env = action_results.make("process", success=False, verified=False, error=r.get("error"),
                                  method="process list checked")
        if r.get("error"):
            return action_results.attach({"text": r["error"]}, env)
    text = f'{r.get("app")}: {r.get("note") or ""}'.strip()
    if r.get("windows"):
        text += "\nWindows: " + "; ".join(f'id {w["id"]} "{w["title"]}"' for w in r["windows"][:4])
        text += "\nUse app_view with a window id to look inside."
    if uri:
        text += ("\nA started process means the link was received; it does not prove the requested page, game or "
                 "experience finished loading. Check the window before saying it did.")
    verified = True if r.get("verified") else None if r.get("already_running") else False
    if r.get("ok") and r.get("windows"):
        _LAUNCHED[key] = {"at": time.time(), "windows": r["windows"][:4]}
    env = action_results.make("process", success=bool(r.get("ok")), verified=verified,
                              method="new process of the app seen" if r.get("verified") else
                              "app was already running" if r.get("already_running") else "process list checked",
                              target={"app_id": r.get("id"), "pids": r.get("pids")})
    return action_results.attach({"text": text}, env)
