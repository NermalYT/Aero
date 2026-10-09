"""Installed and running apps on this computer: what they are called, where they live, how to start them, and proof
that a start worked.

Discovery is read-only and uses what each OS publishes for this purpose:
  Windows   Start menu shortcuts (.lnk, parsed here), the Installed apps (Uninstall) registry keys, App Paths, link
            scheme handlers for the schemes in app_catalog.py, Get-StartApps for Store/MSIX apps (their AUMIDs), and
            running processes
  Linux     .desktop entries in the XDG application folders (system, user, Flatpak, Snap)
  macOS     .app bundles in /Applications, /System/Applications and ~/Applications (Info.plist)
Nothing scans whole disks. Results are cached in data/apps.json and re-scanned when the Uninstall keys or Start menu
folders change, when the cache is older than Settings' app_scan_hours, or on request.

resolve() ranks candidates for a phrase ("my bloxstrap", "vs code") with a confidence and a reason: catalog aliases,
names, program names, link schemes, what is running, and launches that worked before on this computer. Two
different apps scoring the same are reported as ambiguous instead of guessed.

launch() starts an app by its best known method (a launch that was verified before comes first), then checks that
a process or window of that app really appeared. A process started by Windows is not proof that a game or page
loaded; the result says exactly what was seen. Programs in folders anyone can write to (Downloads, temp, network
shares) are refused unless trusted=True, which only the user's own approval should give.
"""
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import app_catalog
from .config import DATA, load_settings

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
STORE = DATA / "apps.json"
SCHEMA = 1
_lock = threading.RLock()
_mem = {"data": None}
NO_WINDOW = 0x08000000 if IS_WIN else 0


# ------------------------------------------------------------------------------------------------ names

_FILLER = re.compile(r"\b(my|the|app|application|program|launcher app|please)\b", re.I)


def norm(s):
    """'My VS Code.exe' -> 'vs code'."""
    s = re.sub(r"\.(exe|lnk|app|desktop)$", "", str(s or "").strip(), flags=re.I)
    s = _FILLER.sub(" ", s)
    s = re.sub(r"[^\w+#.]+", " ", s.lower()).strip()
    return re.sub(r"\s+", " ", s)


def fname(path):
    """'C:\\x\\Bloxstrap.exe' -> 'Bloxstrap.exe' on any OS (records can hold Windows paths)."""
    return re.split(r"[\\/]", str(path or ""))[-1]


def stem(path):
    n = fname(path)
    return n[:n.rindex(".")] if "." in n else n


def _rid(name, exe=""):
    base = norm(stem(exe) if exe else name) or norm(name)
    return re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:48] or "app"


# ------------------------------------------------------------------------------------------------ .lnk files

def parse_lnk(path):
    """Target, arguments and working folder of a Windows shortcut (MS-SHLLINK), or None. Pure Python, read-only."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if len(data) < 76 or struct.unpack_from("<I", data, 0)[0] != 0x4C:
        return None
    flags = struct.unpack_from("<I", data, 20)[0]
    unicode = bool(flags & 0x80)
    pos = 76
    try:
        if flags & 0x1:                                   # HasLinkTargetIDList
            pos += 2 + struct.unpack_from("<H", data, pos)[0]
        target = ""
        if flags & 0x2:                                   # HasLinkInfo
            li = pos
            size, hsize, liflags, _vol, lbp, _cnr, cps = struct.unpack_from("<IIIIIII", data, li)
            if liflags & 0x1:
                if hsize >= 0x24:
                    lbpu, cpsu = struct.unpack_from("<II", data, li + 28)
                    target = _wstr(data, li + lbpu) + _wstr(data, li + cpsu)
                else:
                    target = _astr(data, li + lbp) + _astr(data, li + cps)
            pos += size
        out = {"target": target, "name": "", "relative": "", "workdir": "", "args": "", "icon": ""}
        for key, bit in (("name", 0x4), ("relative", 0x8), ("workdir", 0x10), ("args", 0x20), ("icon", 0x40)):
            if flags & bit:
                n = struct.unpack_from("<H", data, pos)[0]
                pos += 2
                if unicode:
                    out[key] = data[pos:pos + 2 * n].decode("utf-16-le", "replace")
                    pos += 2 * n
                else:
                    out[key] = data[pos:pos + n].decode("mbcs" if IS_WIN else "latin-1", "replace")
                    pos += n
        # EnvironmentVariableDataBlock: targets written as %windir%\..., %LOCALAPPDATA%\...
        while pos + 8 <= len(data):
            bsize, sig = struct.unpack_from("<II", data, pos)
            if bsize < 4:
                break
            if sig == 0xA0000001 and bsize >= 788:
                env_t = data[pos + 268:pos + 788].decode("utf-16-le", "replace").split("\x00", 1)[0]
                if env_t and (not out["target"] or "%" in env_t):
                    out["target"] = os.path.expandvars(env_t)
            pos += bsize
        if not out["target"] and out["relative"]:
            out["target"] = str((Path(path).parent / out["relative"]).resolve())
        return out if out["target"] else None
    except (struct.error, ValueError, IndexError):
        return None


def _astr(b, at):
    end = b.index(b"\x00", at)
    return b[at:end].decode("mbcs" if IS_WIN else "latin-1", "replace")


def _wstr(b, at):
    end = at
    while end + 1 < len(b) and b[end:end + 2] != b"\x00\x00":
        end += 2
    return b[at:end].decode("utf-16-le", "replace")


# ------------------------------------------------------------------------------------------------ scanners

def _rec(name, kind, source, exe="", launch=None, **kw):
    r = {"id": _rid(name, exe), "name": str(name).strip(), "kind": kind, "sources": [source], "exe": exe or "",
         "launch": list(launch or []), "aliases": [], "publisher": "", "version": "", "protocols": []}
    r.update({k: v for k, v in kw.items() if v})
    return r


def _start_menu_dirs():
    out = []
    for env, tail in (("ProgramData", r"Microsoft\Windows\Start Menu\Programs"),
                      ("APPDATA", r"Microsoft\Windows\Start Menu\Programs")):
        base = os.environ.get(env)
        if base:
            out.append(Path(base) / tail)
    return [d for d in out if d.is_dir()]


_SKIP_SHORTCUT = re.compile(r"\b(uninstall|readme|help|documentation|release notes|license|website|manual|"
                            r"what'?s new|changelog|support|faq)\b", re.I)


def _scan_shortcuts():
    recs = []
    for root in _start_menu_dirs():
        for p in root.rglob("*"):
            if p.suffix.lower() == ".url":
                try:
                    m = re.search(r"^URL=(.+)$", p.read_text(encoding="utf-8", errors="replace"), re.M)
                except OSError:
                    continue
                if m and not _SKIP_SHORTCUT.search(p.stem):
                    url = m.group(1).strip()
                    scheme = url.split(":", 1)[0].lower()
                    if scheme not in ("http", "https", "file"):
                        recs.append(_rec(p.stem, "protocol", "start_menu", launch=[{"method": "protocol",
                                                                                     "target": url}]))
                continue
            if p.suffix.lower() != ".lnk" or _SKIP_SHORTCUT.search(p.stem):
                continue
            info = parse_lnk(p)
            if not info:
                continue
            tgt = info["target"]
            if not tgt.lower().endswith(".exe"):
                continue
            launch = [{"method": "shortcut", "target": str(p)}, {"method": "exe", "target": tgt, "args": info["args"]}]
            recs.append(_rec(p.stem, "win32", "start_menu", exe=tgt, launch=launch, shortcut=str(p),
                             workdir=info["workdir"]))
    return recs


def _reg_values(root, path, view=0):
    import winreg
    try:
        k = winreg.OpenKey(root, path, 0, winreg.KEY_READ | view)
    except OSError:
        return None
    out = {}
    i = 0
    while True:
        try:
            n, v, _t = winreg.EnumValue(k, i)
        except OSError:
            break
        out[n] = v
        i += 1
    winreg.CloseKey(k)
    return out


def _reg_subkeys(root, path, view=0):
    import winreg
    try:
        k = winreg.OpenKey(root, path, 0, winreg.KEY_READ | view)
    except OSError:
        return []
    out, i = [], 0
    while True:
        try:
            out.append(winreg.EnumKey(k, i))
        except OSError:
            break
        i += 1
    winreg.CloseKey(k)
    return out


_UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"


def _uninstall_roots():
    import winreg
    return [(winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
            (winreg.HKEY_CURRENT_USER, 0)]


def _exe_from_icon(icon):
    s = str(icon or "").strip().strip('"')
    s = re.sub(r",\s*-?\d+$", "", s).strip('"')
    return s if s.lower().endswith(".exe") else ""


def _scan_uninstall():
    recs = []
    for root, view in _uninstall_roots():
        for sub in _reg_subkeys(root, _UNINSTALL, view):
            v = _reg_values(root, _UNINSTALL + "\\" + sub, view) or {}
            name = str(v.get("DisplayName") or "").strip()
            if not name or v.get("SystemComponent") == 1 or v.get("ParentKeyName"):
                continue
            exe = _exe_from_icon(v.get("DisplayIcon"))
            if exe and re.search(r"(unins\w*|uninstall\w*|setup)\.exe$", exe, re.I):
                exe = ""
            loc = str(v.get("InstallLocation") or "").strip().strip('"')
            launch = [{"method": "exe", "target": exe}] if exe else []
            recs.append(_rec(name, "win32", "uninstall", exe=exe, launch=launch, publisher=str(v.get("Publisher") or ""),
                             version=str(v.get("DisplayVersion") or ""), install_dir=loc))
    return recs


def _scan_app_paths():
    import winreg
    recs = []
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in _reg_subkeys(root, base):
            v = _reg_values(root, base + "\\" + sub) or {}
            exe = str(v.get("") or "").strip().strip('"')
            if exe.lower().endswith(".exe"):
                recs.append(_rec(Path(sub).stem, "win32", "app_paths", exe=os.path.expandvars(exe),
                                 launch=[{"method": "exe", "target": os.path.expandvars(exe)}]))
    return recs


def protocol_handler(scheme):
    """The program registered for a link scheme ('roblox' -> 'C:\\...\\Bloxstrap.exe'), or ''. Per-user
    registration (HKCU) wins over the machine-wide one, as Windows does."""
    if not IS_WIN:
        return ""
    import winreg
    for root, path in ((winreg.HKEY_CURRENT_USER, rf"Software\Classes\{scheme}\shell\open\command"),
                       (winreg.HKEY_CLASSES_ROOT, rf"{scheme}\shell\open\command")):
        v = _reg_values(root, path)
        if v and v.get(""):
            cmd = str(v[""])
            m = re.match(r'\s*"([^"]+)"', cmd) or re.match(r"\s*(\S+\.exe)", cmd, re.I)
            return os.path.expandvars(m.group(1)) if m else cmd
    return ""


def _scan_protocols():
    recs = []
    schemes = sorted({p for e in app_catalog.CATALOG.values() for p in e.get("protocols", [])})
    for sch in schemes:
        exe = protocol_handler(sch)
        if exe:
            recs.append(_rec(stem(exe), "win32", "protocol_handler", exe=exe, protocols=[sch],
                             launch=[{"method": "exe", "target": exe}]))
    return recs


def _scan_start_apps():
    """Get-StartApps: every app in the Start menu with its Application User Model ID (Store/MSIX apps too)."""
    try:
        p = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                            "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
                           capture_output=True, text=True, timeout=25, creationflags=NO_WINDOW, encoding="utf-8",
                           errors="replace")
        rows = json.loads(p.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    if isinstance(rows, dict):
        rows = [rows]
    recs = []
    for r in rows:
        name, aumid = str(r.get("Name") or "").strip(), str(r.get("AppID") or "").strip()
        if not name or not aumid or _SKIP_SHORTCUT.search(name):
            continue
        if "!" in aumid:                                  # packaged (Store/MSIX) app
            recs.append(_rec(name, "uwp", "start_apps", launch=[{"method": "aumid", "target": aumid}], aumid=aumid))
        elif aumid.lower().endswith(".exe") and os.path.isabs(aumid):
            recs.append(_rec(name, "win32", "start_apps", exe=aumid, launch=[{"method": "exe", "target": aumid}]))
    return recs


def _desktop_dirs():
    home = Path.home()
    dirs = [Path(d) / "applications" for d in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")]
    dirs += [home / ".local/share/applications", Path("/var/lib/flatpak/exports/share/applications"),
             home / ".local/share/flatpak/exports/share/applications", Path("/var/lib/snapd/desktop/applications")]
    seen, out = set(), []
    for d in dirs:
        if d.is_dir() and str(d) not in seen:
            seen.add(str(d))
            out.append(d)
    return out


def parse_desktop_entry(path):
    """Name, Exec (field codes removed) and flags of a freedesktop .desktop file, or None."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    sec, d = None, {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            sec = line
            continue
        if sec != "[Desktop Entry]" or "=" not in line or line.startswith("#"):
            continue
        k, v = line.split("=", 1)
        d.setdefault(k.strip(), v.strip())
    if d.get("Type", "Application") != "Application" or d.get("NoDisplay") == "true" or d.get("Hidden") == "true":
        return None
    exe = re.sub(r"\s%[fFuUdDnNickvm]", "", d.get("Exec", "")).strip()
    if not d.get("Name") or not exe:
        return None
    return {"name": d["Name"], "exec": exe, "try": d.get("TryExec", ""), "generic": d.get("GenericName", ""),
            "keywords": d.get("Keywords", ""), "id": Path(path).stem}


def _scan_desktop_entries():
    recs = []
    for d in _desktop_dirs():
        for p in d.glob("*.desktop"):
            e = parse_desktop_entry(p)
            if not e:
                continue
            first = e["exec"].split()[0].strip('"') if e["exec"] else ""
            r = _rec(e["name"], "desktop_entry", "xdg", exe=first,
                     launch=[{"method": "desktop", "target": e["id"]}, {"method": "command", "target": e["exec"]}],
                     desktop_id=e["id"])
            if e["generic"]:
                r["aliases"].append(norm(e["generic"]))
            recs.append(r)
    return recs


def _scan_mac_apps():
    import plistlib
    recs = []
    for root in (Path("/Applications"), Path("/System/Applications"), Path.home() / "Applications",
                 Path("/Applications/Utilities")):
        if not root.is_dir():
            continue
        for app in root.glob("*.app"):
            try:
                with open(app / "Contents" / "Info.plist", "rb") as f:
                    info = plistlib.load(f)
            except Exception:
                continue
            name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or app.stem
            bid = info.get("CFBundleIdentifier") or ""
            exe = str(app / "Contents" / "MacOS" / (info.get("CFBundleExecutable") or app.stem))
            schemes = [s for t in info.get("CFBundleURLTypes", []) or [] for s in t.get("CFBundleURLSchemes", []) or []]
            recs.append(_rec(name, "mac_bundle", "applications", exe=exe, bundle_id=bid, bundle=str(app),
                             protocols=[s.lower() for s in schemes][:8],
                             launch=[{"method": "bundle", "target": bid or str(app)}]))
    return recs


def scan_running():
    """[{pid, name, exe}] of this user's processes (cheap: names and paths only)."""
    try:
        import psutil
    except ImportError:
        return []
    out = []
    for p in psutil.process_iter(["pid", "name", "exe"]):
        try:
            if p.info.get("name"):
                out.append({"pid": p.info["pid"], "name": p.info["name"], "exe": p.info.get("exe") or ""})
        except Exception:
            continue
    return out


# ------------------------------------------------------------------------------------------------ store

def _load():
    if _mem["data"] is not None:
        return _mem["data"]
    try:
        d = json.loads(STORE.read_text(encoding="utf-8"))
        if d.get("schema") != SCHEMA:
            d = {}
    except (OSError, ValueError):
        d = {}
    d.setdefault("schema", SCHEMA)
    d.setdefault("records", [])
    d.setdefault("learned", {})      # app id -> {"method", "target", "ok", "fail", "last_ok", "exe_sig"}
    d.setdefault("aliases", {})      # user alias -> app id
    d.setdefault("disabled", [])     # app ids the user switched off
    d.setdefault("scanned_at", 0)
    d.setdefault("stamp", "")
    _mem["data"] = d
    return d


def _save(d):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STORE)


def _stamp():
    """Cheap fingerprint of 'something was installed or removed': Uninstall key write times and Start menu
    folder times (Windows), application folder times elsewhere."""
    parts = []
    try:
        if IS_WIN:
            import winreg
            for root, view in _uninstall_roots():
                try:
                    k = winreg.OpenKey(root, _UNINSTALL, 0, winreg.KEY_READ | view)
                    n, _vals, mtime = winreg.QueryInfoKey(k)
                    winreg.CloseKey(k)
                    parts.append(f"{n}:{mtime}")
                except OSError:
                    parts.append("-")
            dirs = _start_menu_dirs()
        elif IS_MAC:
            dirs = [Path("/Applications"), Path.home() / "Applications"]
        else:
            dirs = _desktop_dirs()
        for d in dirs:
            try:
                parts.append(str(int(d.stat().st_mtime)))
            except OSError:
                parts.append("-")
    except Exception:
        return ""
    return "|".join(parts)


def _merge(recs):
    """One record per app: shortcuts, uninstall entries and handlers for the same program are folded together."""
    by_exe, by_name, out = {}, {}, []
    for r in recs:
        key_exe = os.path.normcase(r["exe"]) if r.get("exe") else ""
        key_name = norm(r["name"])
        hit = by_exe.get(key_exe) if key_exe else None
        if hit is None and key_name and r["kind"] != "protocol":
            hit = by_name.get(key_name)
            if hit is not None and hit.get("exe") and key_exe and os.path.normcase(hit["exe"]) != key_exe:
                hit = None                                # same name, different program: keep both
        if hit is None:
            r = dict(r)
            out.append(r)
            if key_exe:
                by_exe[key_exe] = r
            if key_name:
                by_name.setdefault(key_name, r)
            continue
        for s in r["sources"]:
            if s not in hit["sources"]:
                hit["sources"].append(s)
        for m in r["launch"]:
            if m not in hit["launch"]:
                hit["launch"].append(m)
        for k in ("publisher", "version", "install_dir", "aumid", "shortcut", "bundle_id", "desktop_id"):
            if r.get(k) and not hit.get(k):
                hit[k] = r[k]
        if r.get("exe") and not hit.get("exe"):
            hit["exe"] = r["exe"]
            by_exe[os.path.normcase(r["exe"])] = hit
        hit["protocols"] = sorted(set(hit.get("protocols") or []) | set(r.get("protocols") or []))
        if r["kind"] == "uwp":
            hit["kind"] = "uwp"
        if len(r["name"]) < len(hit["name"]) and r["sources"][0] == "start_menu":
            hit["name"] = r["name"]                       # the Start menu name is what people call it
    ids = {}
    for r in out:
        cid = app_catalog.by_exe(fname(r["exe"])) if r.get("exe") else None
        if not cid:
            n = norm(r["name"])
            cid = next((k for k, e in app_catalog.CATALOG.items() if n == norm(e["name"]) or n in e["aliases"]), None)
        if cid:
            r["catalog"] = cid
            if r.get("exe") and norm(r["name"]) == norm(stem(r["exe"])):
                r["name"] = app_catalog.CATALOG[cid]["name"]      # "notepad" (a program file name) -> "Notepad"
        base = r["id"]
        i = ids.get(base, 0)
        ids[base] = i + 1
        if i:
            r["id"] = f"{base}-{i + 1}"
        r["trust"] = trust_of(r.get("exe"))
    return out


def scan(force=False):
    """Scan (or reuse the cache). Returns the records."""
    with _lock:
        d = _load()
        hours = float(load_settings().get("app_scan_hours") or 24)
        stamp = _stamp()
        fresh = d["records"] and time.time() - d["scanned_at"] < hours * 3600 and stamp == d.get("stamp")
        if fresh and not force:
            return d["records"]
        recs = []
        if IS_WIN:
            for fn in (_scan_shortcuts, _scan_uninstall, _scan_app_paths, _scan_protocols, _scan_start_apps):
                try:
                    recs += fn()
                except Exception:
                    continue
        elif IS_MAC:
            recs = _scan_mac_apps()
        else:
            recs = _scan_desktop_entries()
        d["records"] = _merge(recs)
        d["scanned_at"] = time.time()
        d["stamp"] = stamp
        _save(d)
        return d["records"]


def scan_async():
    threading.Thread(target=lambda: _safe(scan), daemon=True, name="app-scan").start()


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


def records():
    d = _load()
    return d["records"] or scan()


def get(app_id):
    for r in records():
        if r["id"] == app_id:
            return r
    e = app_catalog.entry(app_id)
    return _catalog_record(e) if e else None


_handlers = {"t": 0.0, "v": {}}


def handlers():
    """{scheme: program} for the catalog's link schemes that have a program registered (cached for a minute)."""
    if time.time() - _handlers["t"] > 60:
        v = {}
        if IS_WIN:
            for sch in sorted({p for e in app_catalog.CATALOG.values() for p in e.get("protocols", [])}):
                h = _safe(protocol_handler, sch)
                if h:
                    v[sch] = h
        _handlers.update(t=time.time(), v=v)
    return _handlers["v"]


def _catalog_record(e):
    """A catalog app with no installed program found: usable when it is a web service, or when a program is
    registered for its link scheme (Roblox through Bloxstrap, for example)."""
    launch, via = [], ""
    hs = handlers()
    for sch in e.get("protocols", []):
        if sch in hs:
            launch.append({"method": "protocol", "target": f"{sch}:", "handler": hs[sch]})
            via = hs[sch]
            break
    if e.get("web"):
        launch.append({"method": "url", "target": e["web"]})
    kind = "protocol" if via else "service" if e.get("web") else "catalog"
    rec = {"id": e["id"], "name": e["name"], "kind": kind, "sources": ["catalog"], "exe": "", "launch": launch,
           "aliases": list(e.get("aliases", [])), "protocols": e.get("protocols", []), "catalog": e["id"],
           "installed": bool(via), "trust": "system"}
    if via:
        rec["handler"] = via
    return rec


# ------------------------------------------------------------------------------------------------ trust

def trust_of(exe):
    """'system' (Windows, Program Files), 'user' (the user's own app folders), 'untrusted' (Downloads, temp, network
    shares, removable roots) or '' when there is no program file."""
    if not exe:
        return ""
    raw = os.path.expandvars(str(exe))
    if raw.startswith(("\\\\", "//")):              # a network share
        return "untrusted"
    p = os.path.normcase(os.path.abspath(raw))
    low = p.lower()                       # "Downloads" on case-sensitive file systems too
    home = str(Path.home()).lower()
    bad = [os.path.join(home, "downloads"), (os.environ.get("TEMP", "") or "/tmp").lower(),
           (os.environ.get("TMP", "") or "/tmp").lower(), "/tmp", "/var/tmp"]
    if any(b and low.startswith(os.path.normcase(b).lower()) for b in bad):
        return "untrusted"
    sysroots = [os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)", "SystemRoot", "ProgramW6432")]
    sysroots += ["/usr", "/opt", "/Applications", "/System", "/snap", "/var/lib/flatpak", "/bin", "/sbin"]
    if any(r and p.startswith(os.path.normcase(r)) for r in sysroots):
        return "system"
    return "user"


# ------------------------------------------------------------------------------------------------ resolve

def _aliases(r):
    out = {norm(r["name"])}
    out.update(norm(a) for a in r.get("aliases") or [])
    if r.get("exe"):
        out.add(norm(stem(r["exe"])))
    e = app_catalog.entry(r.get("catalog")) if r.get("catalog") else None
    if e:
        out.update(norm(a) for a in e["aliases"])
        out.add(norm(e["name"]))
    return {a for a in out if a}


def resolve(phrase, running=None, limit=5):
    """[{id, name, confidence, reason, installed, running, record}] best first. confidence is 0..1; 'ambiguous' is
    set on the first entry when the top two are different apps within 0.05 of each other."""
    q = norm(phrase)
    if not q:
        return []
    d = _load()
    disabled = set(d["disabled"])
    user_alias = {norm(k): v for k, v in d["aliases"].items()}
    procs = running if running is not None else scan_running()
    run_names = {(p.get("name") or "").lower() for p in procs}
    run_exes = {os.path.normcase(p.get("exe") or "") for p in procs if p.get("exe")}
    pool = [r for r in records() if r["id"] not in disabled]
    installed_catalog = {r.get("catalog") for r in pool if r.get("catalog")}
    pool += [_catalog_record(app_catalog.entry(k)) for k in app_catalog.CATALOG if k not in installed_catalog
             and k not in disabled]
    q_words = set(q.split())
    out = []
    for r in pool:
        names = _aliases(r)
        score, why = 0.0, ""
        if user_alias.get(q) == r["id"]:
            score, why = 1.0, f'your alias "{phrase}"'
        elif q in names:
            score, why = 0.95, "exact name"
        else:
            for n in names:
                nw = set(n.split())
                if q_words and q_words <= nw:
                    s = 0.8 * len(q_words) / max(1, len(nw)) + 0.1
                    if s > score:
                        score, why = s, f'"{phrase}" is part of the name "{n}"'
                elif nw and nw <= q_words and len(n) >= 4:
                    s = 0.75
                    if s > score:
                        score, why = s, f'the name "{n}" appears in "{phrase}"'
                elif len(q) >= 4 and n.startswith(q):
                    s = 0.7
                    if s > score:
                        score, why = s, f'"{n}" starts with "{phrase}"'
        if score <= 0:
            continue
        installed = r.get("installed", True)
        is_run = False
        if r.get("exe"):
            is_run = fname(r["exe"]).lower() in run_names or os.path.normcase(r["exe"]) in run_exes
        elif r.get("catalog"):
            e = app_catalog.entry(r["catalog"])
            is_run = any(x.lower() in run_names for x in e.get("exe", []) + e.get("procs", []))
        if not installed:
            score -= 0.15 if r.get("kind") == "service" else 0.3
            why += "; not installed" if r.get("kind") != "service" else "; web service"
        elif r.get("handler"):
            score -= 0.05
            why += f'; opens through {fname(r["handler"])} (registered for its links)'
        if is_run:
            score += 0.03
            why += "; running now"
        learned = d["learned"].get(r["id"])
        if learned and learned.get("ok"):
            score += 0.02
            why += "; started fine before"
        out.append({"id": r["id"], "name": r["name"], "confidence": round(max(0.0, min(1.0, score)), 3),
                    "reason": why, "installed": installed, "running": is_run, "record": r})
    # keep the best record per catalog app (a Start menu shortcut and an uninstall entry of the same program)
    out.sort(key=lambda x: -x["confidence"])
    seen, best = set(), []
    for c in out:
        key = c["record"].get("catalog") or os.path.normcase(c["record"].get("exe") or "") or c["id"]
        if key in seen:
            continue
        seen.add(key)
        best.append(c)
    best = best[:limit]
    if len(best) >= 2 and best[0]["confidence"] - best[1]["confidence"] < 0.05 and best[0]["confidence"] < 0.99:
        best[0]["ambiguous"] = True
    for c in best:
        rel = (app_catalog.entry(c["record"].get("catalog")) or {}).get("related") if c["record"].get("catalog") else None
        if rel:
            c["related"] = rel
    return best


def mentions(text, limit=4):
    """Apps a message names, found deterministically (no model call): catalog aliases, plus installed app names of
    two or more words or long single names. Common words ("word", "steam") only count capitalised."""
    t = str(text or "")
    if not t.strip():
        return []
    low = " " + re.sub(r"[^\w+#.]+", " ", t.lower()) + " "
    low = re.sub(r"\.(?=\s)", " ", low)                      # "Spotify." ends a sentence; "battle.net" keeps its dot
    found = []
    for k, e in app_catalog.CATALOG.items():
        for a in e["aliases"]:
            if a != a.lower() or a in app_catalog.COMMON_WORDS:
                # a name that is also an ordinary word ("Word", "steam"): capitalised, or right after "open", "my"...
                cap = a if a != a.lower() else a[:1].upper() + a[1:]
                if re.search(rf"(?<![\w]){re.escape(cap)}(?![\w])", t) or re.search(
                        rf"\b(open|launch|start|run|close|in|into|use|using|my|from|with)\s+{re.escape(a.lower())}\b",
                        t, re.I):
                    found.append((k, a.lower()))
                    break
                continue
            if f" {a} " in low:
                found.append((k, a))
                break
    have = {k for k, _ in found}
    try:
        recs = _load()["records"]
    except Exception:
        recs = []
    for r in recs:
        if r.get("catalog") in have:
            continue
        n = norm(r["name"])
        if not n or (len(n.split()) < 2 and len(n) < 6) or n in app_catalog.COMMON_WORDS:
            continue
        if f" {n} " in low:
            found.append((r["id"], n))
            have.add(r["id"])
    out = []
    for key, alias in found[:limit]:
        c = resolve(alias, running=[], limit=1)
        if c:
            out.append({"phrase": alias, **{k: v for k, v in c[0].items() if k != "record"},
                        "catalog": c[0]["record"].get("catalog"), "handler": c[0]["record"].get("handler"),
                        "launch": [m["method"] for m in c[0]["record"].get("launch", [])][:3]})
    return out


def context_line(text, ms=None):
    """'Apps: Bloxstrap (installed; Roblox launcher; app_launch "bloxstrap") ...' for <turn_context>, or ''."""
    if ms is None:
        try:
            ms = mentions(text)
        except Exception:
            return ""
    if not ms:
        return ""
    parts = []
    for m in ms:
        e = app_catalog.entry(m.get("catalog")) or app_catalog.entry(m["id"]) or {}
        bits = ["installed" if m["installed"] else ("web service" if e.get("web") else "not found on this computer")]
        if m.get("running"):
            bits.append("running")
        if m.get("handler"):
            bits.append(f"opens through {fname(m['handler'])}")
        if m.get("related"):
            bits.append("related: " + ", ".join(app_catalog.CATALOG[r]["name"] for r in m["related"]
                                                if r in app_catalog.CATALOG))
        if e.get("backends"):
            bits.append("best via " + "/".join(e["backends"][:2]))
        if m["installed"]:
            bits.append(f'app_launch "{m["id"]}"')
        parts.append(f'{m["name"]} ({"; ".join(bits)})')
    return "Apps: " + " · ".join(parts)


# ------------------------------------------------------------------------------------------------ windows

def windows_of(pids):
    """Top-level windows of these processes: [{id, title, pid}] (Windows only; [] elsewhere)."""
    if not IS_WIN or not pids:
        return []
    try:
        from .tools import apps
        return [{"id": w["id"], "title": w["title"], "pid": w["pid"]} for w in apps.windows() if w["pid"] in set(pids)]
    except Exception:
        return []


# ------------------------------------------------------------------------------------------------ launch

def _expected_procs(r):
    names = set()
    if r.get("exe"):
        names.add(fname(r["exe"]).lower())
    e = app_catalog.entry(r.get("catalog")) if r.get("catalog") else None
    if e:
        names.update(x.lower() for x in e.get("exe", []) + e.get("procs", []) if x.lower().endswith(".exe") or not IS_WIN)
        for rel in e.get("related", []):
            re_ = app_catalog.CATALOG.get(rel) or {}
            names.update(x.lower() for x in re_.get("exe", []) + re_.get("procs", []))
    return names


def _exe_sig(exe):
    try:
        st = os.stat(exe)
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return ""


def _methods(r):
    """Launch methods, best first: the one that worked before (if the program file hasn't changed), then the
    Start menu shortcut, AUMID, program file, desktop entry, bundle, link scheme, web address."""
    d = _load()
    learned = d["learned"].get(r["id"])
    ms = list(r.get("launch") or [])
    order = {"shortcut": 0, "aumid": 1, "exe": 2, "desktop": 3, "bundle": 4, "command": 5, "protocol": 6, "url": 7}
    ms.sort(key=lambda m: order.get(m.get("method"), 9))
    if learned and learned.get("ok") and learned.get("exe_sig", "") == _exe_sig(r.get("exe") or ""):
        lm = {"method": learned["method"], "target": learned["target"]}
        ms = [lm] + [m for m in ms if (m.get("method"), m.get("target")) != (lm["method"], lm["target"])]
    return ms


SW_SHOWNORMAL, SW_SHOWNOACTIVATE, SW_SHOWMINNOACTIVE = 1, 4, 7


def _shell_execute(target, params="", workdir=None, show=SW_SHOWNORMAL):
    import ctypes
    from ctypes import wintypes
    fn = ctypes.windll.shell32.ShellExecuteW
    fn.restype = wintypes.HINSTANCE
    fn.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
    h = fn(None, "open", str(target), params or None, workdir or None, int(show))
    code = int(ctypes.cast(h, ctypes.c_void_p).value or 0)
    if code <= 32:
        raise OSError(f"Windows could not start {target} (ShellExecute error {code})")


def _start(m, args, background):
    """Start one launch method. Raises on failure."""
    method, target = m.get("method"), m.get("target")
    extra = " ".join(args or [])
    if IS_WIN:
        show = SW_SHOWMINNOACTIVE if background else SW_SHOWNORMAL
        if method in ("exe", "shortcut"):
            _shell_execute(target, (m.get("args") or "") + (" " + extra if extra else ""), m.get("workdir"), show)
        elif method == "aumid":
            _shell_execute("explorer.exe", f"shell:AppsFolder\\{target}", None, SW_SHOWNORMAL)
        elif method in ("protocol", "url"):
            _shell_execute(target, "", None, SW_SHOWNORMAL)
        else:
            raise OSError(f"{method} launches aren't used on Windows")
        return
    if method == "bundle":
        cmd = ["open", "-g"] if background else ["open"]
        cmd += ["-b", target] if "." in target and not target.endswith(".app") else ["-a", target]
        subprocess.Popen(cmd + (["--args"] + list(args) if args else []), stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    elif method == "desktop":
        exe = shutil.which("gtk-launch")
        if not exe:
            raise OSError("gtk-launch is not installed")
        subprocess.Popen([exe, target] + list(args or []), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    elif method == "command":
        import shlex
        subprocess.Popen(shlex.split(target) + list(args or []), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    elif method in ("protocol", "url"):
        from .osinfo import open_with_default
        if not open_with_default(target):
            raise OSError("no program to open links with")
    elif method == "exe":
        subprocess.Popen([target] + list(args or []), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    else:
        raise OSError(f"unknown launch method {method}")


def launch(app_id_or_phrase, method=None, args=None, uri=None, background=True, trusted=False, timeout=20,
           cancel=None):
    """Start an app and verify it. Returns {ok, verified, app, method, pids, windows, already_running, note}.
    uri: a deep link for the app (e.g. a roblox:// experience link); it goes through whatever program Windows has
    registered for that scheme, and the result names that program."""
    r = get(app_id_or_phrase)
    if r is None:
        cands = resolve(app_id_or_phrase, limit=3)
        if not cands:
            return {"ok": False, "error": f'No installed app matches "{app_id_or_phrase}". Try app_find.'}
        if cands[0].get("ambiguous"):
            return {"ok": False, "ambiguous": True, "error": "Several apps match: " + "; ".join(
                f'{c["name"]} (id {c["id"]}, {c["reason"]})' for c in cands[:3]) + ". Ask the user which one."}
        r = cands[0]["record"]
    if r.get("trust") == "untrusted" and not trusted:
        return {"ok": False, "needs_trust": True, "error": f'{r["name"]} runs from {r.get("exe")}, a folder other '
                f"programs can write to. Ask the user before starting it."}
    expect = _expected_procs(r)
    before = scan_running()
    before_pids = {p["pid"] for p in before}
    already = sorted({p["name"] for p in before if p["name"].lower() in expect})
    ms = _methods(r)
    if uri:
        scheme = uri.split(":", 1)[0].lower()
        handler = protocol_handler(scheme)              # "" where the OS has no lookup
        ms = [{"method": "protocol", "target": uri, "handler": handler}]
        if handler:
            expect.add(fname(handler).lower())
    if method:
        ms = [m for m in ms if m.get("method") == method] or ms
    if not ms:
        return {"ok": False, "error": f'{r["name"]} has no launch method Aero can use on this computer.'}
    errors, used = [], None
    for m in ms[:3]:
        try:
            _start(m, args, background)
            used = m
            break
        except OSError as e:
            errors.append(f'{m["method"]}: {e}')
    if not used:
        _learn(r, ms[0], ok=False)
        return {"ok": False, "app": r["name"], "error": "; ".join(errors)}
    # verification: a new process (or window) of this app
    t0, new, wins = time.time(), [], []
    while time.time() - t0 < timeout:
        if cancel is not None and getattr(cancel, "is_set", lambda: False)():
            break
        now = scan_running()
        new = [p for p in now if p["pid"] not in before_pids and (p["name"].lower() in expect or not expect)]
        if new:
            wins = windows_of([p["pid"] for p in new])
            if wins or time.time() - t0 > 4:
                break
        elif already and time.time() - t0 > 3:
            break                                         # single-instance apps hand the request to the running one
        time.sleep(0.4)
    verified = bool(new)
    note = ""
    if verified:
        note = f'{", ".join(sorted({p["name"] for p in new}))} started' + (
            f' with window "{wins[0]["title"]}"' if wins else " (no window yet)")
    elif already:
        note = f'{", ".join(already)} was already running; the request went to it (no new process to check)'
    else:
        note = "no new process of this app appeared within %ds" % timeout
    if used.get("handler"):
        note += f'; the link was handled by {fname(used["handler"])}'
    _learn(r, used, ok=verified)
    return {"ok": verified or bool(already), "verified": verified, "app": r["name"], "id": r["id"],
            "method": used["method"], "target": used.get("target"), "pids": [p["pid"] for p in new],
            "windows": wins, "already_running": already, "note": note}


def _learn(r, m, ok):
    """Remember which launch method works for an app on this computer (no private data: method + target only)."""
    with _lock:
        d = _load()
        cur = d["learned"].get(r["id"]) or {"ok": 0, "fail": 0}
        sig = _exe_sig(r.get("exe") or "")
        if cur.get("exe_sig") not in (None, sig):
            cur = {"ok": 0, "fail": 0}                    # the program changed (update): earlier results are stale
        if ok:
            cur.update(method=m["method"], target=m.get("target"), ok=cur.get("ok", 0) + 1, last_ok=time.time(),
                       exe_sig=sig)
        else:
            cur["fail"] = cur.get("fail", 0) + 1
            cur["last_fail"] = time.time()
            cur.setdefault("exe_sig", sig)
        d["learned"][r["id"]] = cur
        _save(d)


# ------------------------------------------------------------------------------------------------ user edits

def set_alias(alias, app_id):
    with _lock:
        d = _load()
        if app_id:
            d["aliases"][alias.strip()] = app_id
        else:
            d["aliases"].pop(alias.strip(), None)
        _save(d)


def set_disabled(app_id, off=True):
    with _lock:
        d = _load()
        s = set(d["disabled"])
        (s.add if off else s.discard)(app_id)
        d["disabled"] = sorted(s)
        _save(d)


def forget_learned(app_id=None):
    with _lock:
        d = _load()
        if app_id:
            d["learned"].pop(app_id, None)
        else:
            d["learned"] = {}
        _save(d)


def overview():
    d = _load()
    return {"scanned_at": d["scanned_at"], "count": len(d["records"]), "aliases": d["aliases"],
            "disabled": d["disabled"], "learned": d["learned"],
            "apps": [{k: r.get(k) for k in ("id", "name", "kind", "exe", "sources", "catalog", "trust", "version",
                                             "publisher")} for r in d["records"]]}


def _reset_cache():
    """Tests: forget the in-memory copy."""
    _mem["data"] = None
