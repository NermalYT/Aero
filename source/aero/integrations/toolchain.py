"""Toolchains Aero manages for its integrations: downloads into data/toolchains (never onto the system PATH),
running their programs with that folder on the PATH, and npm/pip packages in Aero-owned prefixes.

Everything here obeys strict offline (localonly.strict): downloads go through httpx, which strict offline already
blocks, and programs that would reach the network themselves (npm, pip, flutter pub) are refused before they start.
Telemetry switches of the integrated tools are set off for every program Aero runs (TELEMETRY_OFF).
"""
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import threading
import time
import zipfile
from pathlib import Path

import httpx

from ..config import DATA, IS_WIN

ROOT = DATA / "toolchains"
STATE = ROOT / "installed.json"
NO_WINDOW = 0x08000000 if IS_WIN else 0
# Opt-outs for the tools Aero runs (each documented by its project).
TELEMETRY_OFF = {"E2E_TELEMETRY_DISABLED": "1", "DO_NOT_TRACK": "1", "FLUTTER_SUPPRESS_ANALYTICS": "true",
                 "DART_DISABLE_ANALYTICS": "1", "NEXT_TELEMETRY_DISABLED": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                 "OCR_TELEMETRY_ENABLED": "false", "NPM_CONFIG_UPDATE_NOTIFIER": "false", "NPM_CONFIG_FUND": "false"}
_bins = []                     # managed bin folders, registered by packs (relative to ROOT)
_lock = threading.Lock()


class Offline(RuntimeError):
    pass


def add_bin(rel):
    """A pack's bin folder (relative to data/toolchains), searched before the system PATH."""
    if rel not in _bins:
        _bins.append(rel)


def bin_dirs():
    return [ROOT / r for r in _bins if (ROOT / r).exists()]


def env(extra=None):
    e = dict(os.environ)
    e.update(TELEMETRY_OFF)
    e["PATH"] = os.pathsep.join([str(p) for p in bin_dirs()] + [e.get("PATH", "")])
    e.update(extra or {})
    return e


def which(name):
    """A program in Aero's toolchains first, then on the PATH."""
    return shutil.which(name, path=env()["PATH"])


def require_online(what):
    from .. import localonly
    if localonly.strict():
        raise Offline(f"Strict offline is on (Settings > Privacy & offline), so Aero won't {what}.")


# ------------------------------------------------------------------------------------------------ state

def installed():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def mark(key, **info):
    with _lock:
        d = installed()
        d[key] = {**info, "at": time.strftime("%Y-%m-%d %H:%M")}
        ROOT.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(d, indent=1), encoding="utf-8")


# ------------------------------------------------------------------------------------------------ downloads

def download(url, dest, emit=None, cancel=None, sha256=None, label=None):
    """Stream url to dest (a file path), with progress events; checks sha256 when given. Returns dest."""
    require_online(f"download {label or url}")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    t0 = time.time()
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60, read=120)) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0
        last = 0.0
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("Cancelled")
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                now = time.time()
                if emit and now - last > 0.5:
                    last = now
                    emit({"type": "progress", "file": label or dest.name, "done": done, "total": total,
                          "speed": done / max(0.001, now - t0)})
    if sha256 and h.hexdigest().lower() != sha256.lower():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Checksum mismatch for {label or url}: the download is not what the publisher released.")
    tmp.replace(dest)
    return dest


def extract(archive, dest, strip=0):
    """Unpack a .zip / .tar.gz / .tar.xz into dest (paths are checked to stay inside dest)."""
    archive, dest = Path(archive), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()

    def target(name):
        parts = Path(name).parts[strip:]
        if not parts:
            return None
        p = (dest / Path(*parts)).resolve()
        if root not in p.parents and p != root:
            raise RuntimeError(f"Unsafe path in archive: {name}")
        return p
    if archive.name.lower().endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                p = target(info.filename)
                if p is None:
                    continue
                if info.is_dir():
                    p.mkdir(parents=True, exist_ok=True)
                    continue
                p.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(p, "wb") as out:
                    shutil.copyfileobj(src, out)
                mode = (info.external_attr >> 16) & 0o777
                if mode and not IS_WIN:
                    p.chmod(mode)
    else:
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                p = target(m.name)
                if p is None or not (m.isfile() or m.isdir()):
                    continue
                if m.isdir():
                    p.mkdir(parents=True, exist_ok=True)
                    continue
                p.parent.mkdir(parents=True, exist_ok=True)
                with t.extractfile(m) as src, open(p, "wb") as out:
                    shutil.copyfileobj(src, out)
                if not IS_WIN:
                    p.chmod(m.mode & 0o777)
    return dest


def github_release(repo, tag=None):
    """The latest (or a tagged) GitHub release of repo: {tag, assets: [{name, url, size, digest}]}."""
    require_online(f"look up {repo}'s releases")
    url = f"https://api.github.com/repos/{repo}/releases/" + (f"tags/{tag}" if tag else "latest")
    r = httpx.get(url, headers={"Accept": "application/vnd.github+json"}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    j = r.json()
    return {"tag": j.get("tag_name"), "assets": [{"name": a["name"], "url": a["browser_download_url"],
                                                   "size": a.get("size") or 0,
                                                   "digest": (a.get("digest") or "").replace("sha256:", "") or None}
                                                  for a in j.get("assets") or []]}


# ------------------------------------------------------------------------------------------------ running

def run(cmd, cwd=None, timeout=600, cancel=None, stdin=None, env_extra=None, network=False, what=None):
    """Run a program to completion. Returns {"rc", "out", "err", "ms"}; Stop (cancel) kills it."""
    if network:
        require_online(what or f"run {Path(str(cmd[0])).name}")
    exe = cmd[0]
    found = which(exe) if not os.path.isabs(str(exe)) else exe
    if not found:
        return {"rc": 127, "out": "", "err": f"{exe} is not installed.", "ms": 0}
    cmd = [found] + [str(c) for c in cmd[1:]]
    if IS_WIN and str(found).lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c"] + cmd
    t0 = time.time()
    p = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, stdin=subprocess.PIPE if stdin is not None else
                         subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env(env_extra),
                         creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace")
    box = {}

    def comm():
        try:
            box["out"], box["err"] = p.communicate(stdin)
        except Exception as e:  # noqa: BLE001
            box["out"], box["err"] = "", str(e)
    th = threading.Thread(target=comm, daemon=True)
    th.start()
    while th.is_alive():
        th.join(0.2)
        if (cancel is not None and cancel.is_set()) or time.time() - t0 > timeout:
            kill(p)
            th.join(5)
            why = "stopped" if cancel is not None and cancel.is_set() else f"timed out after {timeout}s"
            return {"rc": -1, "out": box.get("out", ""), "err": (box.get("err") or "") + f"\n({why})",
                    "ms": round((time.time() - t0) * 1000)}
    return {"rc": p.returncode, "out": box.get("out", ""), "err": box.get("err", ""),
            "ms": round((time.time() - t0) * 1000)}


def kill(p):
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        else:
            p.kill()
    except Exception:  # noqa: BLE001
        pass


def start(cmd, cwd=None, env_extra=None, log=None):
    """Start a long-running program (a dev server, an MCP server). Returns the Popen."""
    exe = which(cmd[0]) or cmd[0]
    cmd = [exe] + [str(c) for c in cmd[1:]]
    if IS_WIN and str(exe).lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c"] + cmd
    out = open(log, "w", encoding="utf-8", errors="replace") if log else subprocess.DEVNULL
    return subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, stdin=subprocess.PIPE, stdout=out,
                            stderr=subprocess.STDOUT, env=env(env_extra), creationflags=NO_WINDOW, text=True)


def node_ok(min_major=22):
    node = which("node")
    if not node:
        return False, "Node.js is not installed (nodejs.org)"
    r = run([node, "--version"], timeout=20)
    v = (r["out"] or "").strip().lstrip("v")
    try:
        ok = int(v.split(".")[0]) >= min_major
    except ValueError:
        ok = False
    return ok, f"Node {v}" + ("" if ok else f" (needs {min_major} or newer)")


def npm_prefix(name):
    return ROOT / "npm" / name


def npm_install(name, packages, emit=None, cancel=None):
    """npm packages into data/toolchains/npm/<name> (an Aero-owned prefix). Returns the prefix."""
    require_online(f"install {', '.join(packages)} from npm")
    prefix = npm_prefix(name)
    prefix.mkdir(parents=True, exist_ok=True)
    if not (prefix / "package.json").exists():
        (prefix / "package.json").write_text(json.dumps({"name": f"aero-{name}", "private": True}), encoding="utf-8")
    if emit:
        emit({"type": "log", "text": f"npm install {' '.join(packages)}"})
    r = run(["npm", "install", "--no-audit", "--no-fund", "--loglevel=error", *packages], cwd=prefix, timeout=1800,
            cancel=cancel, network=True)
    if r["rc"] != 0:
        raise RuntimeError("npm install failed: " + (r["err"] or r["out"])[-800:])
    add_bin(f"npm/{name}/node_modules/.bin")
    return prefix


def npm_bin(name, program):
    d = npm_prefix(name) / "node_modules" / ".bin"
    for cand in ([program + ".cmd", program] if IS_WIN else [program]):
        if (d / cand).exists():
            return d / cand
    return None


def run_python(script, args, timeout=180):
    """A vendored helper script under `python -I` (isolated: no user site, no current folder on sys.path).
    Returns (exit code, stdout, stderr)."""
    import sys
    r = subprocess.run([sys.executable, "-I", str(script), *map(str, args)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW)
    return r.returncode, r.stdout or "", r.stderr or ""


def local_model():
    """Aero's own model server: (OpenAI-compatible base URL, running?). The integrations that need an LLM (code
    review, e2e, Hindsight) are pointed at it."""
    import socket
    from ..config import LLAMA_PORT
    url = f"http://127.0.0.1:{LLAMA_PORT}"
    with socket.socket() as s:                   # Windows takes ~2 s to refuse a closed port: probe briefly first
        s.settimeout(0.3)
        if s.connect_ex(("127.0.0.1", LLAMA_PORT)) != 0:
            return url + "/v1", False
    try:
        ok = httpx.get(url + "/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        ok = False
    return url + "/v1", ok
