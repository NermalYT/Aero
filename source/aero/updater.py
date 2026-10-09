"""Updates from GitHub releases.

Aero asks GitHub for its latest release once, a few seconds after it starts (never again while it runs; Settings >
Updates has a "Check now" button and an off switch, and strict offline mode skips the check). When the release is
newer than this copy, the UI offers it. Installing it:
  1. downloads this OS's archive (Aero-windows.zip, Aero-macos.zip or Aero-linux.tar.gz) and SHA256SUMS.txt
     from the release, and refuses the archive unless its SHA-256 matches;
  2. unpacks it into data/updates/ (refusing absolute paths, "..", and links that point outside) and checks that it
     really is Aero at the version the release names;
  3. hands over to the release's own installer and lets Aero exit: Update-Aero.bat --auto on Windows,
     install.sh --update on Linux and macOS. The installer keeps models, chats, settings and mods, then starts the new
     Aero with --reopen, and the open window reconnects to it.
Every version can update straight to the newest one, since each release carries the complete app.
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path

from . import osinfo
from .config import DATA, LOGS, PKG_DIR, REPO, ROOT, VERSION, load_settings

API = os.environ.get("AERO_UPDATE_API") or f"https://api.github.com/repos/{REPO}/releases/latest"
UPDATES = DATA / "updates"
APP_DIR = PKG_DIR.parent
STARTUP_DELAY = 8                 # seconds after start, so the check never slows Aero's first screen
STATE = {"checked": 0.0, "checking": False, "current": VERSION, "latest": None, "available": False, "notes": "",
         "url": "", "published": "", "asset": None, "size": 0, "error": None, "skipped": False,
         "phase": "idle", "done": 0, "total": 0, "message": "", "installable": True}
_lock = threading.Lock()


def asset_name():
    if osinfo.IS_WIN:
        return "Aero-windows.zip"
    if osinfo.IS_MAC:
        return "Aero-macos.zip"
    return "Aero-linux.tar.gz"


def parse_version(v):
    """"v1.2.10" -> (1, 2, 10, 1); a pre-release ("1.3.0-beta.2") sorts below its release."""
    m = re.match(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)", str(v or "").strip())
    if not m:
        return (0, 0, 0, 0)
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0), 0 if m.group(4).strip() else 1)


def newer(a, b):
    return parse_version(a) > parse_version(b)


def _install_kind_default():
    try:
        STATE["installable"] = install_kind() != "source"
    except OSError:
        pass


def install_kind():
    """How this copy of Aero was installed: "windows" / "unix" (an installer made it, so it can update itself) or
    "source" (run from a git checkout or an unpacked folder: update it with git pull or the installer)."""
    if APP_DIR.resolve() != (ROOT / "app").resolve():
        return "source"
    if osinfo.IS_WIN:
        return "windows" if (ROOT / "venv" / "Scripts" / "python.exe").exists() else "source"
    return "unix" if (ROOT / "venv" / "bin").is_dir() else "source"


_install_kind_default()


# ------------------------------------------------------------------------------------------------ check

def _client(timeout=20):
    import httpx
    return httpx.Client(timeout=httpx.Timeout(timeout, connect=10), follow_redirects=True,
                        headers={"User-Agent": f"Aero/{VERSION} (+https://github.com/{REPO})"})


def check(force=False):
    """Ask GitHub for the latest release. Returns STATE. force: the user pressed "Check now"."""
    s = load_settings()
    if not force and not s.get("update_check", True):
        return STATE
    if s.get("strict_offline"):
        STATE.update(error="Strict offline mode is on, so Aero didn't look for updates.", checked=time.time())
        return STATE
    with _lock:
        if STATE["checking"]:
            return STATE
        STATE.update(checking=True, error=None)
    try:
        with _client() as c:
            r = c.get(API, headers={"Accept": "application/vnd.github+json"})
            if r.status_code == 404:
                raise RuntimeError("the repository has no published release yet")
            r.raise_for_status()
            rel = r.json()
        tag = rel.get("tag_name") or ""
        ver = tag.lstrip("vV")
        assets = {a.get("name"): a for a in rel.get("assets") or []}
        mine = assets.get(asset_name())
        kind = install_kind()
        STATE.update(latest=ver, notes=(rel.get("body") or "")[:20000], url=rel.get("html_url") or "",
                     published=rel.get("published_at") or "", tag=tag,
                     asset=(mine or {}).get("browser_download_url"), size=(mine or {}).get("size") or 0,
                     sums=(assets.get("SHA256SUMS.txt") or {}).get("browser_download_url"),
                     available=newer(ver, VERSION) and bool(mine) and not rel.get("draft") and not rel.get("prerelease"),
                     skipped=s.get("update_skip") == ver, installable=kind != "source", kind=kind)
        if newer(ver, VERSION) and not mine:
            STATE["error"] = f"Aero {ver} is out, but its release has no {asset_name()} for this computer yet."
    except Exception as e:  # noqa: BLE001
        STATE["error"] = f"Couldn't check for updates: {type(e).__name__}: {e}"[:400]
    finally:
        STATE.update(checking=False, checked=time.time())
    return STATE


def check_at_startup():
    """One check, a few seconds after Aero starts. Aero never polls while it runs."""
    def run():
        time.sleep(STARTUP_DELAY)
        _cleanup()
        check()
    threading.Thread(target=run, daemon=True, name="update-check").start()


def public():
    return {k: v for k, v in STATE.items() if k not in ("sums",)}


# ------------------------------------------------------------------------------------------------ install

class UpdateError(RuntimeError):
    pass


def _download(c, url, dest, emit=None):
    tmp = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    with c.stream("GET", url) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done, last = 0, 0.0
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if emit and time.time() - last > 0.25:
                    last = time.time()
                    emit({"phase": "download", "done": done, "total": total})
    os.replace(tmp, dest)
    return h.hexdigest()


def _expected_hash(sums_text, name):
    for line in sums_text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return None


def _safe_extract(archive: Path, dest: Path):
    """Unpack, refusing members that would land outside dest."""
    root = dest.resolve()

    def inside(name):
        p = (root / name).resolve()
        return p == root or root in p.parents

    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for m in z.infolist():
                if m.filename.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", m.filename) or not inside(m.filename):
                    raise UpdateError(f"the archive has an unsafe path: {m.filename}")
            for m in z.infolist():
                z.extract(m, root)
                mode = (m.external_attr >> 16) & 0o777
                if mode and not osinfo.IS_WIN:
                    os.chmod(root / m.filename, mode | 0o600)
        return
    with tarfile.open(archive) as t:
        members = t.getmembers()
        for m in members:
            if m.name.startswith("/") or not inside(m.name):
                raise UpdateError(f"the archive has an unsafe path: {m.name}")
            if (m.issym() or m.islnk()) and not inside(os.path.join(os.path.dirname(m.name), m.linkname)):
                raise UpdateError(f"the archive has a link that points outside it: {m.name}")
            if m.isdev():
                raise UpdateError(f"the archive has a device file: {m.name}")
        try:
            t.extractall(root, filter="data")
        except TypeError:                              # Python before 3.10.12/3.11.4: checked above instead
            t.extractall(root)


def _find_root(x: Path):
    for cand in [x] + sorted(p for p in x.iterdir() if p.is_dir()):
        if (cand / "source" / "aero" / "__main__.py").exists():
            return cand
    return None


def _version_in(root: Path):
    m = re.search(r'^VERSION = "([^"]+)"', (root / "source" / "aero" / "config.py").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def prepare(emit=None):
    """Download, verify and unpack the latest release. Returns the unpacked folder (holding source/ and the
    installer). emit: progress callback {"phase", "done", "total", "message"}."""
    emit = emit or (lambda ev: None)
    if not STATE.get("latest"):
        check(force=True)
    if not STATE.get("available") and not os.environ.get("AERO_UPDATE_ANY"):
        raise UpdateError(STATE.get("error") or f"Aero {VERSION} is already the newest version.")
    if not STATE.get("installable", True):
        raise UpdateError("This copy of Aero runs from a source folder, not an install, so it can't replace itself. "
                          "Update the folder with git pull, or install Aero with its installer.")
    if load_settings().get("strict_offline"):
        raise UpdateError("Strict offline mode is on. Turn it off in Settings > Privacy & offline to download the update.")
    if not STATE.get("sums"):
        raise UpdateError("The release has no SHA256SUMS.txt, so the download can't be verified. Not installing it.")
    ver, name = STATE["latest"], asset_name()
    work = UPDATES / f"aero-{ver}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    with _client(timeout=120) as c:
        emit({"phase": "download", "done": 0, "total": STATE.get("size") or 0, "message": f"Downloading {name}"})
        r = c.get(STATE["sums"])
        r.raise_for_status()
        want = _expected_hash(r.text, name)
        if not want:
            raise UpdateError(f"SHA256SUMS.txt doesn't list {name}. Not installing it.")
        got = _download(c, STATE["asset"], work / name, emit)
    if got != want:
        raise UpdateError(f"The download's SHA-256 ({got[:12]}...) doesn't match the release's ({want[:12]}...). "
                          "Not installing it.")
    emit({"phase": "verify", "message": "Checksum matches. Unpacking."})
    x = work / "x"
    x.mkdir()
    _safe_extract(work / name, x)
    root = _find_root(x)
    if not root:
        raise UpdateError(f"{name} doesn't contain Aero (no source/aero folder).")
    found = _version_in(root)
    if found != ver and not os.environ.get("AERO_UPDATE_ANY"):
        raise UpdateError(f"The release says {ver}, but the code inside says {found}. Not installing it.")
    need = "Update-Aero.bat" if osinfo.IS_WIN else "install.sh"
    if not (root / need).exists():
        raise UpdateError(f"{name} has no {need}.")
    (work / name).unlink(missing_ok=True)
    emit({"phase": "ready", "message": f"Aero {found} is downloaded and checked."})
    return root


def handoff(root: Path, headless=False):
    """Start the release's installer, detached, so it can replace this app once Aero has exited. Returns a function
    for __main__ to call after Aero has stopped serving (see server /api/update/install)."""
    log = LOGS / "update.log"
    if osinfo.IS_WIN:
        bat = root / "Update-Aero.bat"
        args = ["cmd", "/c", str(bat), "--auto"]
        flags = subprocess.CREATE_NEW_CONSOLE | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        def go():
            subprocess.Popen(args, cwd=str(root), creationflags=flags, close_fds=True)
        return go
    args = ["sh", str(root / "install.sh"), "--update", "--yes", "--dir", str(ROOT)]
    if headless:
        args.append("--headless")

    def go():
        lf = open(log, "a", encoding="utf-8")
        lf.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} updating Aero {VERSION} -> {STATE.get('latest')}\n")
        lf.flush()
        env = {k: v for k, v in os.environ.items() if k not in ("AERO_SRC_DIR", "AERO_SELF_COPY", "AERO_DL_DIR")}
        env["AERO_HOME"] = str(ROOT)
        subprocess.Popen(args, cwd=str(root), stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                         start_new_session=True, close_fds=True, env=env)
    return go


def _cleanup():
    """Remove unpacked updates from earlier runs (the installer has copied what it needed)."""
    try:
        if UPDATES.exists():
            for p in UPDATES.iterdir():
                if p.is_dir() and time.time() - p.stat().st_mtime > 3600:
                    shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def python_exe():
    return sys.executable
