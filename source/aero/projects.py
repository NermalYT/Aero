"""Projects (1.3.2): a named folder with its own instructions. A chat started in a project works in its folder (file
and shell tools resolve relative paths there), the model reads the project's instructions with its system prompt, and
the Files view browses that folder. Stored in data/projects.json; a chat records its project in its "project" field.

The file view reads only inside a project's folder (or the working directory when there is no project): paths are
resolved and checked against that root, so "..", links and absolute paths can't leave it."""
import json
import mimetypes
import os
import threading
import time
import uuid
from pathlib import Path

from .config import CHATS, DATA

FILE = DATA / "projects.json"
_lock = threading.Lock()
SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        "$Recycle.Bin", "System Volume Information"}
MAX_ENTRIES = 2000           # one folder listing
MAX_TEXT = 2 * 2**20         # the viewer shows text files up to 2 MB
MAX_RAW = 40 * 2**20         # images, PDFs and the like up to 40 MB


def all():  # noqa: A001 - module-level API: projects.all()
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        return [p for p in data if isinstance(p, dict) and p.get("id")] if isinstance(data, list) else []
    except Exception:  # noqa: BLE001 - missing or damaged file: no projects
        return []


def _save(items):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, FILE)


def get(pid):
    return next((p for p in all() if p["id"] == pid), None) if pid else None


def _folder(path):
    p = Path(os.path.expandvars(os.path.expanduser(str(path or "").strip().strip('"'))))
    if not str(path or "").strip() or not p.is_absolute():
        raise ValueError("Give the project's folder as a full path, e.g. C:\\Users\\you\\code\\my-app.")
    if not p.is_dir():
        raise ValueError(f"No such folder: {p}")
    return str(p.resolve())


def create(name, path, instructions=""):
    name = str(name or "").strip()[:80]
    folder = _folder(path)
    with _lock:
        items = all()
        p = {"id": uuid.uuid4().hex[:10], "name": name or Path(folder).name or folder, "path": folder,
             "instructions": str(instructions or "")[:20000], "created": time.time()}
        items.append(p)
        _save(items)
    return p


def update(pid, **fields):
    with _lock:
        items = all()
        p = next((x for x in items if x["id"] == pid), None)
        if p is None:
            raise KeyError(pid)
        if "name" in fields and str(fields["name"] or "").strip():
            p["name"] = str(fields["name"]).strip()[:80]
        if "path" in fields:
            p["path"] = _folder(fields["path"])
        if "instructions" in fields:
            p["instructions"] = str(fields["instructions"] or "")[:20000]
        _save(items)
    return p


def delete(pid):
    """Forget the project. Its folder and chats stay; the chats show under Recent again."""
    with _lock:
        items = all()
        left = [x for x in items if x["id"] != pid]
        if len(left) != len(items):
            _save(left)
        return len(left) != len(items)


def of_chat(chat_id):
    try:
        return json.loads((CHATS / f"{chat_id}.json").read_text(encoding="utf-8")).get("project")
    except Exception:  # noqa: BLE001 - a new chat has no file yet
        return None


def block(p):
    """The project's part of the system prompt: fixed for the project, so the prompt cache keeps working."""
    text = f"\n\n# Project: {p['name']}\nThe user is working in this project. Its folder is {p['path']} (your working " \
           "directory): relative paths in file and shell tools start there."
    if (p.get("instructions") or "").strip():
        text += "\n\n## Project instructions (from the user)\n" + p["instructions"].strip()
    return text


def chat_settings(settings, chat_id, pid=None):
    """settings for a turn in this chat: the project's folder as the working directory and its instructions with the
    system prompt. Unchanged when the chat has no project (or it was deleted)."""
    p = get(pid or of_chat(chat_id))
    if not p or not Path(p["path"]).is_dir():
        return settings
    return {**settings, "work_dir": p["path"], "project_id": p["id"],
            "system_prompt": (settings.get("system_prompt") or "") + block(p)}


# ------------------------------------------------------------------------------------------------ file view

def root_of(pid, settings):
    """The folder the file view may show: the project's, or the working directory without one."""
    p = get(pid)
    if p:
        return Path(p["path"]).resolve()
    return Path(os.path.expandvars(os.path.expanduser(settings.get("work_dir") or "~"))).resolve()


def inside(root, rel):
    """root/rel resolved, or ValueError when it would leave root (.., an absolute path, a link pointing out)."""
    rel = str(rel or "")
    target = (Path(rel) if os.path.isabs(rel) else root / rel.replace("\\", "/").lstrip("/")).resolve()
    if target != root and root not in target.parents:
        raise ValueError("That path is outside the project folder.")
    return target


def listing(root, rel=""):
    d = inside(root, rel)
    if not d.is_dir():
        raise FileNotFoundError(f"Not a folder: {d}")
    out, n = [], 0
    try:
        items = sorted(os.scandir(d), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
    except OSError as e:
        raise PermissionError(str(e)) from e
    for e in items:
        n += 1
        if n > MAX_ENTRIES:
            break
        try:
            is_dir = e.is_dir(follow_symlinks=False)
            st = e.stat(follow_symlinks=False)
            out.append({"name": e.name, "dir": is_dir, "size": None if is_dir else st.st_size, "mtime": st.st_mtime,
                        "skip": is_dir and e.name in SKIP})
        except OSError:
            continue
    return {"root": str(root), "path": d.relative_to(root).as_posix() if d != root else "", "entries": out,
            "truncated": n > MAX_ENTRIES}


TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".css", ".html", ".htm", ".xml", ".yml",
            ".yaml", ".toml", ".ini", ".cfg", ".bat", ".cmd", ".ps1", ".sh", ".c", ".h", ".cpp", ".hpp", ".cs", ".java",
            ".kt", ".go", ".rs", ".rb", ".php", ".lua", ".sql", ".csv", ".log", ".svg", ".gradle", ".swift", ".dart",
            ".vue", ".svelte", ".env", ".gitignore", ".mcmeta", ".properties", ".lang"}


def read(root, rel):
    f = inside(root, rel)
    if not f.is_file():
        raise FileNotFoundError(f"Not a file: {f}")
    size = f.stat().st_size
    mime = mimetypes.guess_type(f.name)[0] or ""
    info = {"path": f.relative_to(root).as_posix(), "full": str(f), "size": size, "mtime": f.stat().st_mtime,
            "mime": mime}
    if mime.startswith(("image/", "video/", "audio/")) and not f.suffix.lower() == ".svg" or mime == "application/pdf":
        return {**info, "kind": "media"}
    if size > MAX_TEXT:
        return {**info, "kind": "large"}
    data = f.read_bytes()
    if f.suffix.lower() not in TEXT_EXT and b"\0" in data[:8000]:
        return {**info, "kind": "binary"}
    return {**info, "kind": "text", "text": data.decode("utf-8", "replace"), "ext": f.suffix.lower().lstrip(".")}


def raw(root, rel):
    f = inside(root, rel)
    if not f.is_file():
        raise FileNotFoundError(f"Not a file: {f}")
    if f.stat().st_size > MAX_RAW:
        raise ValueError("Too large to show here.")
    return f, mimetypes.guess_type(f.name)[0] or "application/octet-stream"
