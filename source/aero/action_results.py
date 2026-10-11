"""One result shape for actions: what was attempted, through which backend, and whether it was verified.

Tools keep returning {"text", "image", "error"}; an action can add "envelope" (built with make()). tools.run fills in
the rest for every call (duration, side effect from capabilities.py). The model gets a one-line evidence note only
when an action says something about verification, so "the click was sent" is never mistaken for "it worked":

    [verified: value read back · accessibility background]
    [sent but NOT verified: the app gave no readable state · window messages]
    [FAILED verification: the field still holds the old text · window messages]
"""

BACKENDS = ("api", "mcp", "file", "browser_isolated", "accessibility_background", "window_message_background",
            "physical_input", "process", "none")


def make(backend, *, attempted=True, success=True, verified=None, method="", changed=None, error=None, mode=None,
         target=None):
    """verified: True (checked from a source of truth), False (checked and it did NOT take effect), None (could not
    be checked)."""
    env = {"backend": backend, "attempted": bool(attempted), "success": bool(success), "verified": verified,
           "verification_method": method or None, "error": error, "changed_state": changed}
    if mode:
        env["control_mode"] = mode
    if target:
        env["target"] = target
    return env


def attach(res, env):
    if isinstance(res, str):
        res = {"text": res}
    res["envelope"] = {**(res.get("envelope") or {}), **env}
    if env.get("verified") is False or not env.get("success", True):
        res["error"] = True
    return res


def finish(res, name, duration_ms, side_effect):
    """Fill the envelope tools.run attaches to every result."""
    env = res.get("envelope") or {}
    env.setdefault("backend", "none")
    env.setdefault("attempted", True)
    env.setdefault("success", not res.get("error"))
    env.setdefault("verified", None)
    env["side_effect"] = side_effect
    env["duration_ms"] = int(duration_ms)
    env["tool"] = name
    res["envelope"] = env
    return res


_BACKEND_WORDS = {"accessibility_background": "accessibility background", "window_message_background": "window messages",
                  "browser_isolated": "Aero's browser", "physical_input": "real mouse and keyboard", "file": "file",
                  "process": "process", "api": "API", "mcp": "MCP"}


def evidence(env):
    """The one-line note the model sees, or "" when the tool made no verification claim."""
    if not env or "verification_method" not in env:          # only make() sets it: the tool made a claim
        return ""
    where = _BACKEND_WORDS.get(env.get("backend"), env.get("backend") or "")
    how = env.get("verification_method") or ""
    v = env.get("verified")
    if not env.get("success", True):
        head = "FAILED"
    elif v is True:
        head = "verified"
    elif v is False:
        head = "FAILED verification"
    else:
        head = "sent but NOT verified"
    parts = [p for p in (how, where) if p]
    return f"[{head}{': ' + ' · '.join(parts) if parts else ''}]"
