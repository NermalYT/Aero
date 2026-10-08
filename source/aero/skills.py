"""Skills: folders with a SKILL.md (YAML front matter `name` + `description`, then instructions).

Aero reads the same skill format as Claude Code and Codex, from:
    data\\skills\\<name>\\SKILL.md                       Aero's own
    ~\\.claude\\skills\\<name>\\SKILL.md                  Claude Code personal skills
    ~\\.claude\\plugins\\...\\skills\\<name>\\SKILL.md       skills inside installed Claude Code plugins
    ~\\.codex\\skills\\<name>\\SKILL.md                   Codex skills
    <working dir>\\.claude\\skills\\<name>\\SKILL.md      project skills
The router sees only names + descriptions; the model loads a skill's full text with use_skill when it needs it.
"""
import re
import time
from pathlib import Path

from .config import DATA, load_settings

SKILLS_DIR = DATA / "skills"
SKILLS_DIR.mkdir(parents=True, exist_ok=True)
_cache = {"t": 0.0, "v": []}


def _roots():
    home = Path.home()
    s = load_settings()
    roots = [("aero", SKILLS_DIR, 2), ("claude", home / ".claude" / "skills", 2),
             ("claude-plugin", home / ".claude" / "plugins", 9), ("codex", home / ".codex" / "skills", 2)]
    wd = Path(str(s.get("work_dir") or "")).expanduser()
    if str(wd) and (wd / ".claude" / "skills").is_dir():
        roots.append(("project", wd / ".claude" / "skills", 2))
    return roots


def _front_matter(text):
    m = re.match(r"^﻿?---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    meta, key = {}, None
    for line in m.group(1).splitlines():
        kv = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if kv:
            key, val = kv.group(1), kv.group(2).strip()
            meta[key] = "" if val in (">", "|", ">-", "|-") else val.strip("'\"")
        elif key and line.startswith((" ", "\t")):
            meta[key] = (meta[key] + " " + line.strip()).strip()
    return meta, m.group(2)


def _scan(root, depth):
    if not root.is_dir():
        return []
    out = []

    def walk(d, n):
        try:
            entries = list(d.iterdir())
        except Exception:
            return
        for e in entries:
            if e.is_file() and e.name.upper() == "SKILL.MD":
                out.append(e)
            elif e.is_dir() and n > 0 and not e.name.startswith((".git", "node_modules", "__pycache__")):
                walk(e, n - 1)
    walk(root, depth)
    return out


def catalog(force=False):
    """[{name, description, source, path}] of enabled skills (cached for a minute)."""
    if not force and time.time() - _cache["t"] < 60:
        return _cache["v"]
    disabled = set(load_settings().get("skills_disabled") or [])
    seen, out = set(), []
    for source, root, depth in _roots():
        for p in sorted(_scan(root, depth)):
            try:
                meta, _ = _front_matter(p.read_text(encoding="utf-8", errors="replace")[:20000])
            except Exception:
                continue
            name = (meta.get("name") or p.parent.name).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "description": (meta.get("description") or "")[:600], "source": source,
                        "path": str(p), "enabled": name not in disabled})
    _cache.update(t=time.time(), v=[s for s in out])
    return [s for s in out]


def enabled():
    return [s for s in catalog() if s["enabled"]]


def load(name):
    sk = next((s for s in catalog() if s["name"].lower() == (name or "").strip().lower()), None)
    if not sk:
        names = ", ".join(s["name"] for s in enabled()) or "none installed"
        return None, f"No skill named '{name}'. Available: {names}"
    p = Path(sk["path"])
    _, body = _front_matter(p.read_text(encoding="utf-8", errors="replace"))
    files = []
    for f in sorted(p.parent.rglob("*")):
        if f.is_file() and f != p and len(files) < 60:
            files.append(str(f.relative_to(p.parent)))
    return sk, body, files
