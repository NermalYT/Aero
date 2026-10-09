"""app_find and app_launch: find installed apps by the names people use and start them with proof (app_registry.py).
Both work on Windows, Linux and macOS."""
from . import tool
from .. import action_results, app_catalog, app_registry


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
    if uri and not str(uri).split(":", 1)[0].replace("-", "").replace(".", "").isalnum():
        return {"text": f"'{uri}' is not a link Aero can open.", "error": True}
    if uri and str(uri).split(":", 1)[0].lower() in ("http", "https", "file", "javascript", "data", "vbscript"):
        return {"text": "Use open_app or the browser tools for web pages and files; uri is for app links.", "error": True}
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
    env = action_results.make("process", success=bool(r.get("ok")), verified=verified,
                              method="new process of the app seen" if r.get("verified") else
                              "app was already running" if r.get("already_running") else "process list checked",
                              target={"app_id": r.get("id"), "pids": r.get("pids")})
    return action_results.attach({"text": text}, env)
