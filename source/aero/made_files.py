"""Files Aero made for the user (a meetings document, a spreadsheet, an exported diagram, an image), downloadable
from the chat. Only files a tool reported creating get an id; the server serves those ids and nothing else."""
import hashlib
import threading
import time
from pathlib import Path

from .config import read_store, write_store

KINDS = {".docx", ".xlsx", ".pptx", ".pdf", ".md", ".txt", ".csv", ".ics", ".html", ".json", ".png", ".jpg", ".jpeg",
         ".webp", ".svg", ".zip", ".odt", ".ods"}
STORE = "made_files.json"
MAX = 500
_lock = threading.Lock()


def register(path):
    """{id, name, size} for a file a tool made, or None (not a file, not a document kind)."""
    try:
        p = Path(path).resolve()
        if p.suffix.lower() not in KINDS or not p.is_file():
            return None
        size = p.stat().st_size
    except OSError:
        return None
    fid = hashlib.sha1(f"{p}|{p.stat().st_mtime_ns}".encode()).hexdigest()[:16]
    with _lock:
        d = read_store(STORE, {})
        d[fid] = {"path": str(p), "at": time.time()}
        if len(d) > MAX:
            for k in sorted(d, key=lambda k: d[k]["at"])[:len(d) - MAX]:
                d.pop(k)
        write_store(STORE, d)
    return {"id": fid, "name": p.name, "size": size}


def path_of(fid):
    rec = read_store(STORE, {}).get(fid)
    if not rec:
        return None
    p = Path(rec["path"])
    return p if p.is_file() else None


def from_envelope(env):
    ch = (env or {}).get("changed_state")
    return register(ch["path"]) if isinstance(ch, dict) and ch.get("path") else None
