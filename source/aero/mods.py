"""Mods: the user describes a change to Aero in plain words and a model makes it, the way Claude Code edits a project.

Life of a mod
1. new(prompt) copies Aero's app folder into data/mods/<id>/work. The mod chat's model edits only that copy (the
   agent's working directory is the copy, and writes outside it are refused).
2. check(id) proves the copy still works: every changed Python file compiles, changed JavaScript parses (when Node
   is installed), the server module imports, the unit tests pass, and a throwaway Aero boots from the copy (with an
   empty data folder and spare ports) and serves its page.
3. apply(id) writes the changed files into the real app folder, keeping the originals in data/mods/<id>/before, and
   Aero restarts (or just reloads the page when only static files changed).
4. undo(id) puts the originals back. turn_off/turn_on keep the mod but take it out or put it back in.

Safety nets
- A mod that keeps Aero from starting is undone automatically on the next launch (boot_guard/boot_failed), and
  `python -m aero --safe` starts with every mod switched off.
- Updates replace the app folder; at the next start every mod that was on is applied again to the new version.
  A mod whose lines changed in the update is marked "needs redo": its chat can make it again on the new code.
"""
import difflib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .config import DATA, PKG_DIR, VERSION

APP_DIR = PKG_DIR.parent                  # <install>/app (source/ in a checkout): what a mod may change
MODS = DATA / "mods"
ORDER = MODS / "order.json"               # ids of mods that are on, in the order they were applied
BOOT = MODS / "boot.json"                 # {"pending": id, "tries": n}: a mod applied but not yet seen to boot
MARK = APP_DIR / "aero" / "mods-applied.json"   # written into the app folder; an update's fresh copy lacks it
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", "node_modules", "data", "models", "llama", "venv",
             "brand"}                     # brand/: logo source images, not part of the running app
TEXT_EXT = {".py", ".js", ".css", ".html", ".json", ".md", ".txt", ".svg", ".bat", ".ps1", ".sh", ".toml", ".cfg"}
MAX_FILE = 2_000_000


# ------------------------------------------------------------------------------------------------ records

def _now():
    return time.time()


def _dir(mod_id):
    if not re.fullmatch(r"[0-9a-f]{10}", str(mod_id or "")):
        raise ValueError("bad mod id")
    return MODS / mod_id


def _read(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _write(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def get(mod_id):
    rec = _read(_dir(mod_id) / "mod.json", None)
    if not rec:
        raise KeyError(mod_id)
    return rec


def save(rec):
    rec["updated"] = _now()
    _write(_dir(rec["id"]) / "mod.json", rec)
    return rec


def listing():
    out = []
    if MODS.exists():
        for d in MODS.iterdir():
            rec = _read(d / "mod.json", None) if d.is_dir() else None
            if rec:
                out.append(public(rec))
    return sorted(out, key=lambda r: -r.get("created", 0))


def public(rec):
    return {k: rec.get(k) for k in ("id", "name", "prompt", "status", "created", "updated", "chat_id", "files",
                                    "checks", "version", "error", "restart", "parent")}


def _order():
    return [i for i in _read(ORDER, []) if (MODS / i / "mod.json").exists()]


def _set_order(ids):
    _write(ORDER, ids)


def name_for(prompt):
    words = re.findall(r"[A-Za-z0-9][\w'-]*", prompt or "")[:6]
    name = " ".join(words) or "Mod"
    return (name[:48]).strip()


# ------------------------------------------------------------------------------------------------ the copy

def _ignore(_dir_path, names):
    return [n for n in names if n in SKIP_DIRS or n.endswith((".pyc", ".pyo")) or n == "mods-applied.json"]


def _files(root: Path):
    """Relative paths of every file under root that a mod is compared on."""
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for f in fns:
            if f.endswith((".pyc", ".pyo")) or f == "mods-applied.json":
                continue
            out.append(Path(dp, f).relative_to(root).as_posix())
    return set(out)


def _copy_live(dest: Path, overlay: Path = None):
    """A copy of the live app at dest, with overlay's files (a mod's saved before/after files) put on top."""
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(APP_DIR, dest, ignore=_ignore)
    if overlay is not None and overlay.exists():
        for f in _files(overlay):
            (dest / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(overlay / f, dest / f)


def new(prompt, chat_id=None):
    """Start a mod: two copies of the live app, base (as it was) and work (what the model edits). The mod is the
    difference between them, so a later update or another mod doesn't show up as part of this one."""
    mod_id = uuid.uuid4().hex[:10]
    d = _dir(mod_id)
    d.mkdir(parents=True)
    _copy_live(d / "base")
    _copy_live(d / "work")
    rec = {"id": mod_id, "name": name_for(prompt), "prompt": (prompt or "").strip(), "status": "draft",
           "created": _now(), "updated": _now(), "chat_id": chat_id, "files": [], "checks": None, "version": VERSION}
    return save(rec)


def work_dir(mod_id):
    """The mod's copy of Aero. A mod reopened after it was applied (or turned off) gets fresh copies of the live
    app: base with the files as they were before the mod, work with the mod's files on top."""
    d = _dir(mod_id)
    w = d / "work"
    if not w.exists() or not (d / "base").exists():
        rec = _read(d / "mod.json", {}) or {}
        on = rec.get("status") == "applied"
        _copy_live(d / "base", d / "before" if on else None)
        _copy_live(w, None if on else d / "after")
        for c in rec.get("files") or []:              # files the mod added (not in before/) or deleted (not in after/)
            f = c["path"]
            if c["status"] == "added":
                (d / "base" / f).unlink(missing_ok=True)
            elif c["status"] == "deleted":
                (w / f).unlink(missing_ok=True)
    return w


def base_dir(mod_id):
    work_dir(mod_id)
    return _dir(mod_id) / "base"


def inside(mod_id, path):
    """True when path is inside the mod's copy (writes anywhere else are refused)."""
    try:
        Path(path).resolve().relative_to(work_dir(mod_id).resolve())
        return True
    except (ValueError, OSError):
        return False


def _read_text(p: Path):
    try:
        if p.stat().st_size > MAX_FILE:
            return None
        b = p.read_bytes()
    except OSError:
        return None
    if b"\x00" in b[:8192]:
        return None
    return b.decode("utf-8", "replace")


def changes(mod_id):
    """[{path, status: added|modified|deleted, plus, minus, binary}]: what the mod changed (base -> work)."""
    w, base = work_dir(mod_id), base_dir(mod_id)
    live, mine = _files(base), _files(w)
    out = []
    for f in sorted(live | mine):
        a, b = base / f, w / f
        if f in live and f not in mine:
            out.append({"path": f, "status": "deleted", "plus": 0, "minus": len((_read_text(a) or "").splitlines())})
            continue
        if f in mine and f not in live:
            t = _read_text(b)
            out.append({"path": f, "status": "added", "plus": len((t or "").splitlines()), "minus": 0, "binary": t is None})
            continue
        try:
            if a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes():
                continue
        except OSError:
            continue
        ta, tb = _read_text(a), _read_text(b)
        if ta is None or tb is None:
            out.append({"path": f, "status": "modified", "plus": 0, "minus": 0, "binary": True})
            continue
        plus = minus = 0
        for line in difflib.unified_diff(ta.splitlines(), tb.splitlines(), lineterm="", n=0):
            if line.startswith("+") and not line.startswith("+++"):
                plus += 1
            elif line.startswith("-") and not line.startswith("---"):
                minus += 1
        out.append({"path": f, "status": "modified", "plus": plus, "minus": minus})
    return out


def diff_text(mod_id, path=None, limit=400_000):
    """Unified diff of the mod (one file, or all of them)."""
    w, base = work_dir(mod_id), base_dir(mod_id)
    parts = []
    for c in changes(mod_id):
        f = c["path"]
        if path and f != path:
            continue
        if c.get("binary"):
            parts.append(f"Binary file {f} {c['status']}\n")
            continue
        a = (_read_text(base / f) or "") if c["status"] != "added" else ""
        b = (_read_text(w / f) or "") if c["status"] != "deleted" else ""
        parts.append("".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), f"a/{f}", f"b/{f}", n=3)))
    text = "\n".join(p if p.endswith("\n") else p + "\n" for p in parts)
    return text[:limit] + ("\n... [diff cut]\n" if len(text) > limit else "")


def fingerprint(mod_id):
    """Changes to the copy since the last check show up as a different fingerprint."""
    import hashlib
    w, h = work_dir(mod_id), hashlib.sha256()
    for c in changes(mod_id):
        h.update(f"{c['path']}\0{c['status']}\0".encode())
        if c["status"] != "deleted":
            try:
                h.update(hashlib.sha256((w / c["path"]).read_bytes()).digest())
            except OSError:
                pass
    return h.hexdigest()[:16]


def checked(rec):
    """True when the mod's last checks passed and the copy hasn't changed since."""
    ch = rec.get("checks") or {}
    return bool(ch.get("ok")) and ch.get("fp") == fingerprint(rec["id"])


def needs_restart(paths):
    """Python changes need a restart; static files (CSS, JS, HTML, images) only need the page reloaded."""
    return any(not p.startswith("aero/static/") for p in paths)


# ------------------------------------------------------------------------------------------------ checks

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _py():
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):          # checks capture output: use the console interpreter
        cand = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.exists(cand):
            return cand
    return exe


def _env(home):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AERO_")}
    env.update(AERO_HOME=str(home), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", AERO_UPDATE_CHECK="0",
               AERO_VAULT="file")
    llama = os.environ.get("AERO_LLAMA_SERVER")
    if llama:
        env["AERO_LLAMA_SERVER"] = llama
    return env


def _run(args, cwd, env, timeout):
    flags = 0x08000000 if sys.platform == "win32" else 0
    try:
        r = subprocess.run(args, cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=flags, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout + r.stderr)
    except subprocess.TimeoutExpired as e:
        return -1, f"timed out after {timeout}s\n{(e.stdout or '')[-2000:] if isinstance(e.stdout, str) else ''}"


def check(mod_id, tests=True, boot=True):
    """Run every check on the mod's copy. Returns {"ok", "steps": [{"name", "ok", "detail"}], "t"}."""
    w = work_dir(mod_id)
    changed = [c for c in changes(mod_id) if c["status"] != "deleted"]
    steps = []

    def add(name, ok, detail=""):
        steps.append({"name": name, "ok": bool(ok), "detail": (detail or "").strip()[-3000:]})
        return ok

    bad = []
    for c in changed:
        if c["path"].endswith(".py"):
            try:
                compile((w / c["path"]).read_bytes(), c["path"], "exec", dont_inherit=True)
            except SyntaxError as e:
                bad.append(f"{c['path']}, line {e.lineno}: {e.msg}\n{(e.text or '').rstrip()}")
            except Exception as e:  # noqa: BLE001
                bad.append(f"{c['path']}: {e}")
    add("Python files compile", not bad, "\n".join(bad))
    js = [c["path"] for c in changed if c["path"].endswith(".js")]
    node = shutil.which("node")
    if js and node:
        errs = []
        for f in js:
            rc, out = _run([node, "--check", str(w / f)], w, dict(os.environ), 60)
            if rc != 0:
                errs.append(out)
        add("JavaScript parses", not errs, "\n".join(errs))
    elif js:
        add("JavaScript parses", True, "Node.js isn't installed, so the JavaScript was only checked by the boot test.")
    for c in changed:
        if c["path"].endswith(".json"):
            try:
                json.loads((w / c["path"]).read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                add(f"{c['path']} is valid JSON", False, str(e))
    home = Path(tempfile.mkdtemp(prefix="aero-modcheck-"))
    try:
        env = _env(home)
        rc, out = _run([_py(), "-c", "import aero.server"], w, env, 120)
        if not add("Aero's server module imports", rc == 0, out if rc else ""):
            return _done(mod_id, steps)
        if tests and (w / "tests").is_dir():
            rc, out = _run([_py(), "-m", "unittest", "discover", "-s", "tests", "-q"], w, env, 600)
            summary = next((l for l in reversed(out.splitlines()) if l.startswith(("Ran ", "OK", "FAILED"))), "")
            ran = re.search(r"Ran (\d+) tests?", out)
            add("Unit tests pass" + (f" ({ran.group(1)})" if ran else ""), rc == 0, out[-3000:] if rc else summary)
        if boot:
            add("A test copy of Aero starts and serves its page", *_boot_test(w, home))
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return _done(mod_id, steps)


def _done(mod_id, steps):
    res = {"ok": all(s["ok"] for s in steps), "steps": steps, "t": _now(), "fp": fingerprint(mod_id)}
    try:
        rec = get(mod_id)
        rec["checks"] = res
        rec["files"] = changes(mod_id)
        save(rec)
    except KeyError:
        pass
    return res


def _boot_test(w: Path, home: Path):
    import httpx
    port, llama = _free_port(), _free_port()
    env = _env(home)
    env.update(AERO_PORT=str(port), AERO_LLAMA_PORT=str(llama))
    log = home / "boot.log"
    flags = 0x08000000 if sys.platform == "win32" else 0
    with open(log, "w", encoding="utf-8") as lf:
        p = subprocess.Popen([_py(), "-m", "aero", "--no-window", "--log-stdout"], cwd=str(w), env=env, stdout=lf,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags)
    try:
        url = f"http://127.0.0.1:{port}"
        t0 = time.time()
        while time.time() - t0 < 90:
            if p.poll() is not None:
                break
            try:
                st = httpx.get(url + "/api/state", timeout=2)
                if st.status_code == 200:
                    page = httpx.get(url + "/", timeout=5)
                    js = httpx.get(url + "/app.js", timeout=5)
                    css = httpx.get(url + "/app.css", timeout=5)
                    ok = page.status_code == 200 and "<html" in page.text.lower() and js.status_code == css.status_code == 200
                    return ok, "" if ok else f"page {page.status_code}, app.js {js.status_code}, app.css {css.status_code}"
            except Exception:
                pass
            time.sleep(0.4)
        return False, log.read_text(encoding="utf-8", errors="replace")[-3000:] or "it never answered"
    finally:
        try:
            httpx.post(f"http://127.0.0.1:{port}/api/shutdown", timeout=2)
        except Exception:
            pass
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()


# ------------------------------------------------------------------------------------------------ apply / undo

def apply(mod_id, force=False):
    """Copy the mod's changes into the live app. Returns {"restart": bool, "files": [...]}."""
    rec = get(mod_id)
    if rec["status"] in ("applied",):
        raise RuntimeError("This mod is already on.")
    ch = changes(mod_id)
    if not ch:
        raise RuntimeError("The mod's copy has no changes yet.")
    if not force and not checked(rec):
        raise RuntimeError("The checks haven't passed for this version of the mod. Run them again first.")
    d, w, base = _dir(mod_id), work_dir(mod_id), base_dir(mod_id)
    plan, conflicts, removed = [], [], []
    for c in ch:                                   # what each live file becomes; nothing is written yet
        f = c["path"]
        live, b, a = APP_DIR / f, base / f, w / f
        cur = live.read_bytes() if live.exists() else None
        old = b.read_bytes() if b.exists() else None
        if c["status"] == "deleted":
            if cur is not None and cur != old:
                conflicts.append(f)
            plan.append((f, cur, None))
            removed.append(f)
            continue
        new_bytes = a.read_bytes()
        if cur == old:                             # the live file is what the mod started from
            plan.append((f, cur, new_bytes))
            continue
        if c["status"] == "added" or cur is None or c.get("binary"):
            conflicts.append(f)
            continue
        merged = patch_text(cur.decode("utf-8", "replace"), old.decode("utf-8", "replace"),
                            new_bytes.decode("utf-8", "replace"))
        if merged is None:
            conflicts.append(f)
        else:
            plan.append((f, cur, merged.encode("utf-8")))
    if conflicts:
        raise RuntimeError("Aero changed these files after this mod was started, in the same places the mod "
                           "changes: " + ", ".join(conflicts) + ". Ask the mod's chat to make the change again "
                           "on the current version.")
    for sub in ("before", "after"):
        shutil.rmtree(d / sub, ignore_errors=True)
    for f, cur, data in plan:
        if cur is not None:
            (d / "before" / f).parent.mkdir(parents=True, exist_ok=True)
            (d / "before" / f).write_bytes(cur)
        if data is not None:
            (d / "after" / f).parent.mkdir(parents=True, exist_ok=True)
            (d / "after" / f).write_bytes(data)
    (d / "patch.diff").write_text(diff_text(mod_id), encoding="utf-8")
    for f, cur, data in plan:
        if data is None:
            (APP_DIR / f).unlink(missing_ok=True)
        else:
            (APP_DIR / f).parent.mkdir(parents=True, exist_ok=True)
            (APP_DIR / f).write_bytes(data)
    files = [c["path"] for c in ch]
    rec.update(status="applied", files=ch, applied=_now(), version=VERSION, removed=removed,
               restart=needs_restart(files), error=None)
    rec.pop("redo", None)
    save(rec)
    _set_order([i for i in _order() if i != mod_id] + [mod_id])
    _mark()
    if rec["restart"]:
        _write(BOOT, {"pending": mod_id, "tries": 0})
    for sub in ("work", "base"):                   # the live app has it now; reopening the mod makes fresh copies
        shutil.rmtree(d / sub, ignore_errors=True)
    return {"restart": rec["restart"], "files": files}


def _undo_files(rec):
    """Put the originals back where the live file is still exactly what the mod wrote. Returns paths that had been
    changed again since (left alone)."""
    d = _dir(rec["id"])
    conflicts = []
    for c in rec.get("files") or []:
        f = c["path"]
        live, after, before = APP_DIR / f, d / "after" / f, d / "before" / f
        if c["status"] == "added":
            if live.exists() and after.exists() and live.read_bytes() == after.read_bytes():
                live.unlink()
            elif live.exists():
                conflicts.append(f)
            continue
        if c["status"] == "deleted":
            if not live.exists() and before.exists():
                live.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(before, live)
            continue
        if live.exists() and after.exists() and live.read_bytes() == after.read_bytes():
            shutil.copy2(before, live)
            continue
        # someone (a later mod, an update) changed the file again: take out just this mod's lines
        a, b, cur = (_read_text(after) or ""), (_read_text(before) or ""), (_read_text(live) if live.exists() else None)
        merged = patch_text(cur, a, b) if cur is not None else None
        if merged is None:
            conflicts.append(f)
        else:
            live.write_text(merged, encoding="utf-8")
    return conflicts


def undo(mod_id, status="off"):
    """Take a mod out of the live app. status: "off" (kept, can be turned on again) or "broken"."""
    rec = get(mod_id)
    if rec["status"] != "applied":
        rec["status"] = status if rec["status"] != "draft" else "draft"
        save(rec)
        return {"restart": False, "conflicts": []}
    conflicts = _undo_files(rec)
    rec.update(status=status, error=("Some files changed after this mod and were left as they are: "
                                      + ", ".join(conflicts)) if conflicts else None)
    save(rec)
    _set_order([i for i in _order() if i != mod_id])
    _mark()
    return {"restart": needs_restart([c["path"] for c in rec.get("files") or []]), "conflicts": conflicts}


def turn_on(mod_id):
    """Apply a mod that was turned off (or that an update took out) to the current app."""
    rec = get(mod_id)
    if rec["status"] == "applied":
        return {"restart": False}
    if rec["status"] == "draft":
        return apply(mod_id)
    ok, failed = _reapply(rec)
    if not ok:
        rec.update(status="needs_redo", error="These files changed since the mod was made: " + ", ".join(failed))
        save(rec)
        raise RuntimeError(rec["error"] + ". Open the mod's chat and ask for it again on this version.")
    rec.update(status="applied", version=VERSION, error=None, applied=_now())
    save(rec)
    _set_order([i for i in _order() if i != mod_id] + [mod_id])
    _mark()
    restart = needs_restart([c["path"] for c in rec.get("files") or []])
    if restart:
        _write(BOOT, {"pending": mod_id, "tries": 0})
    return {"restart": restart}


def delete(mod_id):
    rec = get(mod_id)
    res = {"restart": False}
    if rec["status"] == "applied":
        res = undo(mod_id)
    shutil.rmtree(_dir(mod_id), ignore_errors=True)
    return res


def _reapply(rec):
    """Write a mod's "after" files onto the current app: as is where the file still matches the mod's "before",
    line by line where it has changed. Returns (ok, [paths that didn't fit])."""
    d = _dir(rec["id"])
    plan, failed = [], []
    for c in rec.get("files") or []:
        f = c["path"]
        live, after, before = APP_DIR / f, d / "after" / f, d / "before" / f
        if c["status"] == "added":
            plan.append((live, after.read_bytes() if after.exists() else None))
            continue
        if c["status"] == "deleted":
            plan.append((live, None))
            continue
        cur = live.read_bytes() if live.exists() else None
        if cur is not None and before.exists() and cur == before.read_bytes():
            plan.append((live, after.read_bytes()))
            continue
        merged = patch_text(cur.decode("utf-8", "replace") if cur is not None else None,
                            _read_text(before) or "", _read_text(after) or "")
        if merged is None:
            failed.append(f)
        else:
            plan.append((live, merged.encode("utf-8")))
    if failed:
        return False, failed
    for live, data in plan:
        if data is None:
            live.unlink(missing_ok=True)
        else:
            live.parent.mkdir(parents=True, exist_ok=True)
            live.write_bytes(data)
    return True, []


def patch_text(current, old, new):
    """Apply the change old -> new to current, hunk by hunk, matching each hunk by its surrounding lines (3 lines of
    context, then 1 when the file changed close to the mod's lines). Returns the patched text, or None when a hunk
    can't be placed."""
    if current is None:
        return None
    for context in (3, 1):
        out = _patch(current, old, new, context)
        if out is not None:
            return out
    return None


def _patch(current, old, new, context):
    cur = current.splitlines(True)
    a, b = old.splitlines(True), new.splitlines(True)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    hunks = []
    for group in sm.get_grouped_opcodes(context):
        i1, i2 = group[0][1], group[-1][2]
        j1, j2 = group[0][3], group[-1][4]
        hunks.append((i1, a[i1:i2], b[j1:j2]))
    edits = []
    for i1, src, dst in hunks:
        hits = [k for k in range(len(cur) - len(src) + 1) if cur[k:k + len(src)] == src] if src else []
        if not src:
            hits = [min(i1, len(cur))]
        if len(hits) > 1:
            hits = [min(hits, key=lambda k: abs(k - i1))]
        if len(hits) != 1:
            return None
        edits.append((hits[0], len(src), dst))
    edits.sort(key=lambda e: e[0])
    for (k, n, _), nxt in zip(edits, edits[1:]):
        if k + n > nxt[0]:
            return None                                  # overlapping hunks: give up rather than guess
    for k, n, dst in reversed(edits):
        cur[k:k + n] = dst
    return "".join(cur)


# ------------------------------------------------------------------------------------------------ boot safety

def _mark():
    try:
        _write(MARK, {"mods": _order(), "version": VERSION})
    except OSError:
        pass


def reapply_after_update(log=print):
    """An update replaced the app folder (the marker is gone): apply every mod that was on to the new version."""
    ids = _order()
    if not ids or MARK.exists():
        return []
    redo = []
    kept = []
    for i in ids:
        try:
            rec = get(i)
        except KeyError:
            continue
        ok, failed = _reapply(rec)
        if ok:
            rec.update(version=VERSION, error=None)
            kept.append(i)
            log(f"Mod '{rec['name']}' applied to Aero {VERSION}.")
        else:
            rec.update(status="needs_redo", error=f"Aero {VERSION} changed {', '.join(failed)}, so this mod has to be "
                                                  "made again on the new version.")
            redo.append(i)
            log(f"Mod '{rec['name']}' doesn't fit Aero {VERSION}; marked for redo.")
        save(rec)
    _set_order(kept)
    _mark()
    return redo


def boot_guard(safe=False, log=print):
    """Runs before Aero imports its server. Re-applies mods after an update, undoes a mod that stopped Aero from
    starting last time, and with safe=True turns every mod off."""
    try:
        MODS.mkdir(parents=True, exist_ok=True)
        if safe:
            for i in reversed(_order()):
                try:
                    undo(i)
                    log(f"Safe mode: turned off mod {get(i)['name']}.")
                except Exception as e:  # noqa: BLE001
                    log(f"Safe mode: couldn't turn off mod {i}: {e}")
            BOOT.unlink(missing_ok=True)
            return
        reapply_after_update(log)
        b = _read(BOOT, None)
        if b and b.get("pending"):
            if b.get("tries", 0) >= 1:
                _undo_broken(b["pending"], "Aero didn't finish starting with this mod on, so it was turned off.", log)
            else:
                _write(BOOT, {**b, "tries": b.get("tries", 0) + 1})
    except Exception as e:  # noqa: BLE001  (a mods problem must never stop Aero itself from starting)
        log(f"Mods: {type(e).__name__}: {e}")


def _undo_broken(mod_id, why, log):
    try:
        undo(mod_id, status="broken")
        rec = get(mod_id)
        rec["error"] = why
        save(rec)
        log(f"Mod '{rec['name']}': {why}")
    except Exception as e:  # noqa: BLE001
        log(f"Couldn't undo mod {mod_id}: {e}")
    BOOT.unlink(missing_ok=True)


def boot_failed(log=print):
    """The backend didn't come up. If a just-applied mod is the likely cause, undo it and start Aero again once."""
    b = _read(BOOT, None)
    if not b or not b.get("pending"):
        return False
    _undo_broken(b["pending"], "Aero didn't start with this mod on, so it was turned off.", log)
    if os.environ.get("AERO_MOD_RETRY") != "1":
        os.environ["AERO_MOD_RETRY"] = "1"
        log("Starting Aero again without the mod.")
        os.execv(sys.executable, [sys.executable, "-m", "aero"] + [a for a in sys.argv[1:] if a != "--reopen"])
    return True


def boot_ok():
    BOOT.unlink(missing_ok=True)


# ------------------------------------------------------------------------------------------------ the mod chat

def file_map(root: Path, limit=160):
    """A short map of Aero's code for the modding model: path, size and the first line of each module's docstring."""
    rows = []
    for f in sorted(_files(root)):
        if not f.endswith((".py", ".js", ".css", ".html", ".txt", ".md", ".sh", ".bat")) or f.startswith(("brand/", "docs/")):
            continue
        p = root / f
        text = _read_text(p) or ""
        lines = text.count("\n") + 1
        doc = ""
        m = re.match(r'\s*(?:"""|\'\'\')(.+?)(?:\n|"""|\'\'\')', text) if f.endswith(".py") else None
        if m:
            doc = m.group(1).strip()[:110]
        rows.append(f"- {f} ({lines} lines){': ' + doc if doc else ''}")
        if len(rows) >= limit:
            break
    return "\n".join(rows)


PERSONA = """# Your job: modding Aero
The user wants to change Aero itself, the app you are running in. You are working on a private copy of Aero's code at
{work}. Your working directory is that copy; relative paths resolve inside it. Only files inside it can be changed.
When you finish, Aero checks the copy (Python compiles, the server imports, the unit tests pass, a test copy boots
and serves its page) and shows the user what changed. Nothing reaches the real app until the user presses Apply.

## How Aero is built
- Backend: Python (FastAPI) in aero/. server.py holds the HTTP API, pipeline.py runs a turn (router, local model,
  cloud reviews), agent.py is the tool loop, tools/ has the agent's tools (each is a function with @tool(...)),
  config.py has the settings and their defaults (DEFAULTS), engine.py runs llama.cpp.
- Frontend: plain HTML/CSS/JS, no build step: aero/static/index.html, app.css (design tokens at the top), app.js
  (h() builds elements, api() calls the backend, S is the app state). The look is Frutiger Aero: glossy glass, sky
  blues, soft gradients. Keep that style, and write UI text that says what something is or does: no slogans.
- Tests: tests/ (unittest). Add or update a test when you change behavior.

## Code map
{map}

## Rules
1. Read the files you will change first. Make the smallest change that does what the user asked, in the existing
   style. Use edit_file for changes; write_file only for new files.
2. Keep Aero's safety features working (approval prompts, strict offline mode, the Stop button, how secrets are
   stored) unless the user's request is explicitly about changing them.
3. Don't add network calls, telemetry or new dependencies unless the user asked for them.
4. When you are done, call mod_check. If a check fails, fix it and call mod_check again.
5. Final answer: what you changed (files and behavior) and how the user will see it after Apply.
"""


def persona(mod_id):
    w = work_dir(mod_id)
    text = PERSONA.format(work=w, map=file_map(w))
    rec = get(mod_id)
    if rec.get("redo"):
        old = (_dir(mod_id) / "patch.diff")
        diff = old.read_text(encoding="utf-8", errors="replace")[:16000] if old.exists() else ""
        text += (f"\n\n## Redo an older mod\nThis mod was made for Aero {rec.get('made_for') or 'an older version'} "
                 f"and Aero {VERSION} changed the same files, so it was taken out. The user's original request: "
                 f"{rec.get('prompt')!r}. Make the same change on this version. The old change, for reference:\n"
                 f"```diff\n{diff}\n```")
    return text


def for_turn(mod_id, chat_id, prompt):
    """The mod a mod-chat message works on. A draft stays as it is; a mod that was turned off or undone goes back to
    being a draft (its copy has its changes); one that needs a redo gets a clean copy of the current Aero; an
    applied mod gets a follow-up mod, so each Apply is one change the user can undo. Returns (record, created)."""
    rec = get(mod_id)
    st = rec["status"]
    if st == "draft":
        return rec, False
    if st == "applied":
        nrec = new(prompt, chat_id)
        nrec["parent"] = mod_id
        return save(nrec), True
    d = _dir(mod_id)
    if st == "needs_redo":
        _copy_live(d / "base")
        _copy_live(d / "work")
        rec.update(redo=True, made_for=rec.get("version"))
    rec.update(status="draft", checks=None, error=None)
    return save(rec), False


MOD_TOOLS = ["list_dir", "read_file", "find_files", "search_files", "write_file", "edit_file", "move_path", "delete_path",
             "mod_check"]


def chat_settings(settings, mod_id):
    """Settings for a mod chat's turn: it works in the mod's copy, and reading, writing and checking that copy need
    no approval (nothing reaches the real app before Apply)."""
    pol = {**(settings.get("tool_policy") or {}), "files_read": "auto", "files_write": "auto", "mods": "auto"}
    return {**settings, "work_dir": str(work_dir(mod_id)), "tool_policy": pol}


def prepare_turn(turn, mod_id):
    """Limit a turn to the modding tools, inside the mod's copy."""
    from . import tools
    turn.sandbox = work_dir(mod_id).resolve()
    turn.tctx.mod_id = mod_id
    turn.persona = persona(mod_id)
    names = [n for n in MOD_TOOLS if n in tools.REGISTRY]
    turn.exposed = names
    turn.blocked = set(tools.REGISTRY) - set(names)
