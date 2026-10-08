"""Registry of models the user has downloaded or opened ("Your models")."""
import hashlib
import time
from pathlib import Path

from .config import models_dir, read_store, write_store
from .gguf import SPLIT_RE


def _id(path):
    return hashlib.sha1(str(Path(path).resolve()).lower().encode()).hexdigest()[:12]


def _is_main_gguf(p: Path):
    n = p.name.lower()
    if "mmproj" in n or not n.endswith(".gguf"):
        return False
    m = SPLIT_RE.match(p.name)
    return not m or m.group(2) == "00001"


def _find_mmproj(model: Path):
    for p in sorted(model.parent.glob("*.gguf")):
        if "mmproj" in p.name.lower():
            return str(p)
    return None


def all_models():
    reg = read_store("models.json", {})
    changed = False
    # auto-register GGUFs dropped into the models folder by hand
    root = models_dir()
    for p in root.rglob("*.gguf"):
        if _is_main_gguf(p) and not {"_advisor", "_router", "_draft"} & set(p.relative_to(root).parts):
            mid = _id(p)
            if mid not in reg:
                reg[mid] = {"id": mid, "name": p.stem, "path": str(p), "repo": None,
                            "mmproj": _find_mmproj(p), "added": time.time(), "last_used": 0}
                changed = True
    for mid, m in list(reg.items()):
        p = Path(m["path"])
        m["exists"] = p.exists()
        if p.exists():
            m["size"] = sum(x.stat().st_size for x in _parts(p) if x.exists())
    if changed:
        write_store("models.json", reg)
    return sorted([m for m in reg.values() if not m.get("hidden")], key=lambda m: (-(m.get("last_used") or 0), -(m.get("added") or 0)))


def _parts(p: Path):
    m = SPLIT_RE.match(p.name)
    if not m:
        return [p]
    return [p.with_name(f"{m.group(1)}-{i:05d}-of-{m.group(3)}.gguf") for i in range(1, int(m.group(3)) + 1)]


def get(mid):
    return read_store("models.json", {}).get(mid)


def add(path, repo=None, mmproj=None, name=None):
    reg = read_store("models.json", {})
    p = Path(path)
    mid = _id(p)
    cur = reg.get(mid, {"added": time.time(), "last_used": 0})
    cur.update({"id": mid, "name": name or cur.get("name") or p.stem, "path": str(p), "repo": repo or cur.get("repo"),
                "mmproj": mmproj or cur.get("mmproj") or _find_mmproj(p), "hidden": False})
    reg[mid] = cur
    write_store("models.json", reg)
    return cur


def update(mid, **kw):
    reg = read_store("models.json", {})
    if mid in reg:
        reg[mid].update(kw)
        write_store("models.json", reg)
        return reg[mid]
    return None


def remove(mid, delete_files=False):
    reg = read_store("models.json", {})
    m = reg.get(mid)
    if m and not delete_files:
        m["hidden"] = True          # keep a tombstone so the folder scan doesn't re-add it
    else:
        reg.pop(mid, None)
    write_store("models.json", reg)
    if m and delete_files:
        p = Path(m["path"])
        for x in _parts(p):
            try:
                x.unlink()
            except Exception:
                pass
        if m.get("mmproj"):
            try:
                Path(m["mmproj"]).unlink()
            except Exception:
                pass
        try:
            p.parent.rmdir()
        except Exception:
            pass
    return m
