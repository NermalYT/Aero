"""Prompt cache per chat (1.2.70): a long chat picks up where it left off, even after a restart or a model reload.

llama-server keeps the KV cache of what it last processed in a slot, and (with --cache-ram) recent prompts in RAM, so
switching between chats within one server run is already cheap. Two things lose it: a side job (memory, compaction,
reflection, titles) running in the chat's slot, and a server restart. So:
  - the user's chat runs in slot 0 ("id_slot": 0); when the running server has 2 or more slots (Settings → Model &
    tuning; only on builds with --kv-unified and --cache-idle-slots, so the slots share one KV pool), side jobs run in
    slot 1 (side_slot), which costs no extra VRAM;
  - a long chat's slot is saved to disk (--slot-save-path, data/kv-cache/<chat>.bin) right after the answer, before
    any side job is queued: that is the only moment slot 0 surely holds the chat (with --cache-idle-slots, the next
    task moves an idle slot to the RAM cache and clears it; measured: a save made later held 3,162 of 9,400 tokens).
    llama-server writes the file on its main loop, so it is saved again only when the chat grew by SAVE_GROWTH tokens
    since the last save. Unattended (scheduled) chats aren't saved;
  - the first time a chat runs after the server started, its saved cache (same model file, context and KV type) is
    restored into slot 0 before the request; within one server run the RAM prompt cache does that job instead;
  - the folder stays under a size limit (least recently used first; never the file just written), a chat's cache goes
    when the chat is deleted, and compaction moves it to the new chat id.
"""
import json
import re
import threading
import time
from pathlib import Path

import httpx

from .config import DATA

DIR = DATA / "kv-cache"
MIN_TOKENS = 1500          # below this, reading the prompt again is faster than restoring a file
SAVE_GROWTH = 2000         # save again only after the chat grew by this many tokens
_state = {"slot0": None, "boot": None, "seen": set(), "forgotten": set(), "last_turn": 0.0}
_lock = threading.RLock()  # bookkeeping
_io = threading.Lock()     # one save or restore at a time, in order (llama-server runs them FIFO)


def _name(cid):
    return re.sub(r"[^\w-]", "", str(cid or ""))[:80] or "chat"


def fingerprint(engine):
    """What a saved cache belongs to: the model file itself (path, size, modified time), its projector, context and
    KV type. A file saved for anything else is never restored."""
    m, cfg = engine.get("model") or {}, engine.get("cfg") or {}
    size = mtime = None
    try:
        st = Path(m.get("path") or "").stat()
        size, mtime = st.st_size, int(st.st_mtime)
    except OSError:
        pass
    return {"model": m.get("path"), "size": size, "mtime": mtime, "mmproj": bool(m.get("mmproj")),
            "ctx": cfg.get("ctx"), "kv": cfg.get("kv", "q8_0")}


def enabled(settings):
    return bool((settings or {}).get("save_chat_cache", True))


def side_slot(url):
    """Extra request fields for side jobs: slot 1 when the server at url was started with more than one slot (its
    real slot count, not the setting, which may have changed without a reload)."""
    from . import engine
    return {"id_slot": 1} if engine.slots_of(url) >= 2 else {}


def _boot(url):
    from . import engine
    return engine.boot_of(url)


def _note_boot(boot):
    if _state["boot"] != boot:
        _state.update(boot=boot, slot0=None, seen=set())


def before_turn(cid, eng, settings):
    """Before the first request of a chat turn: restore the chat's saved cache into slot 0 when this server run hasn't
    had the chat yet. Returns a short note for the log, or None."""
    if not enabled(settings) or not eng.get("url"):
        return None
    boot = _boot(eng["url"])
    with _lock:
        _note_boot(boot)
        first = cid not in _state["seen"]
        _state["seen"].add(cid)
        _state["slot0"] = cid
        _state["forgotten"].discard(cid)
    ram = int((settings or {}).get("prompt_cache_ram_mb", 8192) or 0)
    if not first and ram > 0:
        return None                     # llama-server's RAM prompt cache has it
    f, meta = DIR / f"{_name(cid)}.bin", DIR / f"{_name(cid)}.json"
    if not (f.exists() and meta.exists()):
        return None
    try:
        m = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if m.get("fp") != fingerprint(eng):
        return None
    t0 = time.time()
    with _io:
        with _lock:
            if _state["slot0"] != cid:
                return None
        try:
            r = httpx.post(eng["url"] + "/slots/0?action=restore", json={"filename": f.name}, timeout=180)
        except httpx.HTTPError:
            return None
    if r.status_code != 200:
        return None
    n = (r.json() or {}).get("n_restored") or m.get("n") or 0
    m["used_at"] = time.time()
    try:
        meta.write_text(json.dumps(m), encoding="utf-8")
    except OSError:
        pass
    return f"Restored this chat's saved prompt cache ({n:,} tokens) in {time.time() - t0:.1f} s."


def after_turn(cid, eng, settings, tokens, unattended=False):
    """Right after a chat's answer (before side jobs run): save slot 0 for this chat when it is long enough and grew
    enough since its last save. Returns the tokens saved, or 0."""
    with _lock:
        _state["last_turn"] = time.time()
        if not enabled(settings) or not eng.get("url") or tokens < MIN_TOKENS or unattended:
            return 0
        if _state["slot0"] != cid or cid in _state["forgotten"]:
            return 0
    name = _name(cid)
    meta = DIR / f"{name}.json"
    old = {}
    try:
        old = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if old.get("fp") == fingerprint(eng) and tokens - int(old.get("n") or 0) < SAVE_GROWTH:
        return 0                         # not enough new since the last save
    limit = _limit(settings)
    per_tok = (old.get("bytes") or 0) / max(1, old.get("n") or 1)
    if per_tok and per_tok * tokens > limit:
        return 0                         # would not fit in the folder's limit on its own
    DIR.mkdir(parents=True, exist_ok=True)
    with _io:
        with _lock:
            if _state["slot0"] != cid or cid in _state["forgotten"]:
                return 0
        try:
            r = httpx.post(eng["url"] + "/slots/0?action=save", json={"filename": f"{name}.bin"}, timeout=300)
        except httpx.HTTPError:
            return 0
        if r.status_code != 200:
            return 0
        n = (r.json() or {}).get("n_saved") or tokens
        size = _size(DIR / f"{name}.bin")
        try:
            meta.write_text(json.dumps({"fp": fingerprint(eng), "n": n, "bytes": size, "saved_at": time.time(),
                                        "used_at": time.time()}), encoding="utf-8")
        except OSError:
            return 0
        with _lock:
            gone = cid in _state["forgotten"]
    if gone:                             # deleted while it was being written
        forget_file(DIR / f"{name}.bin")
        return 0
    prune(limit, keep=name)
    return n


def _limit(settings):
    try:
        gb = float((settings or {}).get("chat_cache_max_gb") or 4)
    except (TypeError, ValueError):
        gb = 4.0
    return max(0.5, gb) * 2**30


def _size(p):
    try:
        return p.stat().st_size
    except OSError:
        return 0


def prune(limit_bytes, keep=None):
    """Keep the saved caches under the limit: the least recently used go first, never `keep` (the file just saved).
    They are Aero's own cache files."""
    files = []
    for b in list(DIR.glob("*.bin")) if DIR.exists() else []:
        used = 0.0
        try:
            used = json.loads(b.with_suffix(".json").read_text(encoding="utf-8")).get("used_at", 0)
        except (OSError, ValueError):
            pass
        files.append((used, _size(b), b))
    total = sum(s for _, s, _ in files)
    for _, size, b in sorted(files):
        if total <= limit_bytes:
            break
        if b.stem == keep:
            continue
        forget_file(b)
        total -= size


def forget_file(b):
    for p in (b, b.with_suffix(".json")):
        try:
            p.unlink()
        except OSError:
            pass


def forget(cid):
    """A deleted chat's cache goes too (also one being written right now)."""
    with _lock:
        _state["forgotten"].add(cid)
        if _state["slot0"] == cid:
            _state["slot0"] = None
    with _io:
        forget_file(DIR / f"{_name(cid)}.bin")


def rebind(old, new):
    """Compaction moved the conversation to a new chat id: the slot now holds the new one; the old file is stale."""
    with _lock:
        if _state["slot0"] == old:
            _state["slot0"] = new
        if old in _state["seen"]:
            _state["seen"].add(new)
    with _io:
        forget_file(DIR / f"{_name(old)}.bin")


def info():
    files = list(DIR.glob("*.bin")) if DIR.exists() else []
    return {"chats": len(files), "bytes": sum(_size(f) for f in files), "dir": str(DIR)}
