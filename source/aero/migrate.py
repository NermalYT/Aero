"""One-time move from the app's earlier names (C:\\Halcyon, or C:\\VRAMpire before that) to Aero (C:\\Aero).

The updater moves the folder itself; this fixes what refers to the old location inside data\\:
model paths (and the model ids derived from them), the models folder and working directory in
settings, and MCP server configs. Chats, memory, tunings (keyed by file name + size) carry over as is.
The old app's "About you" text is kept too: Aero ships with an empty one, so a profile the old
version only had as its built-in default is copied into settings (from data\\legacy-config.py.txt,
which the updater saves before replacing the old code).
Safe to run more than once.
"""
import argparse
import ast
import json
import re
import sys
from pathlib import Path

from .config import DATA, ROOT


def _swap(s, old, new):
    if not isinstance(s, str):
        return s
    return re.sub(re.escape(old), lambda m: new, s, flags=re.I) if old.lower() in s.lower() else s


def _walk(obj, old, new):
    if isinstance(obj, dict):
        return {k: _walk(v, old, new) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, old, new) for v in obj]
    return _swap(obj, old, new)


def run(old_root, new_root=None, log=print):
    old = str(Path(old_root))
    new = str(Path(new_root or ROOT))
    if old.lower() == new.lower():
        return 0
    changed = 0
    for name in ("settings.json", "models.json", "mcp.json", "router.json"):
        p = DATA / name
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log(f"  skip {name}: {e}")
            continue
        d2 = _walk(d, old, new)
        if name == "models.json" and isinstance(d2, dict):
            from .models import _id
            d2 = {(_id(v["path"]) if isinstance(v, dict) and v.get("path") else k):
                  ({**v, "id": _id(v["path"])} if isinstance(v, dict) and v.get("path") else v)
                  for k, v in d2.items()}
        if d2 != d:
            p.write_text(json.dumps(d2, indent=2, ensure_ascii=False), encoding="utf-8")
            changed += 1
            log(f"  updated {name}")
    return changed


def upgrade_settings(log=print):
    """Settings written by VRAMpire 1.x store every key, so new defaults would never reach them. Adjust the few
    that changed meaning: thinking was on/off; Aero's router can decide per request ("auto")."""
    p = DATA / "settings.json"
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    if int(s.get("settings_version") or 1) >= 2:
        return False
    if s.get("thinking") is True:
        s["thinking"] = "auto"
    s["settings_version"] = 2
    p.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    log("  settings upgraded (thinking: auto)")
    return True


def carry_profile(log=print):
    """Copy the old app's built-in "About you" text into settings when the user never saved their own."""
    legacy = DATA / "legacy-config.py.txt"
    if not legacy.exists():
        return False
    p = DATA / "settings.json"
    done = False
    try:
        s = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        if "user_profile" not in s:
            text = None
            for node in ast.parse(legacy.read_text(encoding="utf-8", errors="replace")).body:
                if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "STARTER_PROFILE" for t in node.targets) \
                        and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    text = node.value.value
            if text and text.strip():
                s["user_profile"] = text
                p.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
                log("  kept your \"About you\" text from the old install")
                done = True
    except Exception as e:  # noqa: BLE001
        log(f"  could not read the old \"About you\" text ({e}); it is still in {legacy.name}")
        return False
    legacy.unlink(missing_ok=True)
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="old", help="the old install folder, e.g. C:\\Halcyon")
    ap.add_argument("--settings-only", action="store_true", help="only bring settings up to date (every update runs this)")
    a = ap.parse_args()
    if a.old and not a.settings_only:
        n = run(a.old)
        print(f"  Migrated settings from {a.old}: {n} file(s) updated.")
    carry_profile()
    upgrade_settings()
    return 0


if __name__ == "__main__":
    sys.exit(main())
