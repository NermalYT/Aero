"""mod_check: in a "Mod Aero" chat, the model checks its modified copy of Aero before the user sees it (mods.py)."""
from . import tool


def _check(ctx, tests=True):
    from .. import mods
    mod_id = getattr(ctx, "mod_id", None)
    if not mod_id:
        return {"text": "mod_check only works in a Mod Aero chat.", "error": True}
    changed = mods.changes(mod_id)
    if not changed:
        return {"text": "Your copy of Aero has no changes yet, so there is nothing to check.", "error": True}
    res = mods.check(mod_id, tests=bool(tests))
    lines = [f"{'PASS' if s['ok'] else 'FAIL'}  {s['name']}" + (f"\n{s['detail']}" if s["detail"] and not s["ok"] else "")
             for s in res["steps"]]
    files = ", ".join(f"{c['path']} ({c['status']}, +{c['plus']} -{c['minus']})" for c in changed)
    head = "All checks passed." if res["ok"] else "Some checks failed. Fix them and call mod_check again."
    return {"text": f"{head}\nChanged files: {files}\n\n" + "\n".join(lines), "error": not res["ok"]}


tool("mod_check", "Check your modified copy of Aero: Python compiles, JavaScript parses, the server imports, the unit "
     "tests pass, and a test copy of Aero starts and serves its page. Call it when your change is done, and again "
     "after fixing anything it reports.", "mods",
     {"tests": {"type": "boolean", "description": "Run the unit tests too (default true)."}},
     summary=lambda a: "checking the modded copy")(_check)
