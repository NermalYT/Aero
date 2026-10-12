"""Update a real Windows install of Aero in place, in a throwaway folder. Windows only.

1. Installs the previous release (its Aero-windows.zip) into <work>\\install: with the release's own installer when it
   can install outside C:\\Aero, otherwise with the same steps as its Install-Aero.bat (app, Python environment,
   packages, setup.py, settings) minus llama.cpp, shortcuts, the Installed apps entry and models.
2. Starts that Aero and gives it settings, a chat, a memory fact and a model file, then runs the new build's
   Update-Aero.bat over the install while it is running, and checks that the data was kept, the app files are exactly
   the new build's, and the Aero it starts again answers /api/state with the new version.
3. Aero's own updater (Settings > Updates > Update now) against a fake release served from this machine: first a
   download whose SHA-256 doesn't match SHA256SUMS.txt, which it must refuse without touching the install, then the
   real thing, after which Aero comes back as the fake release's version with the data kept.
4. A fresh install of the new build into <work>\\fresh through Update-Aero.bat --dest.

    python validation\\check_windows_update.py --previous Aero-1.1.0.zip --candidate ..\\dist\\Aero-windows.zip ^
        --work C:\\Users\\you\\AeroTest\\update

Nothing outside --work is touched: every install runs with --skip-llama --no-shortcuts --no-register --skip-models,
pip keeps its cache in --work, the servers use ports --port to --port+21, and only processes started from --work are
stopped. --work is deleted
and recreated, so it must be a new folder or one this script made before. Exit code 0 when every check passed.
"""
import argparse
import functools
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import httpx
import psutil

MARKER = ".aero-update-check"
PROTECTED = [Path(p) for p in (r"C:\Aero", r"C:\Halcyon", r"C:\VRAMpire")]
TEST_FLAGS = ["--skip-llama", "--no-shortcuts", "--no-register", "--skip-models"]
FAKE_VERSION = "99.0.0"
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail != "" else ""), flush=True)
    return bool(ok)


def say(msg):
    print(f"---- {msg}", flush=True)


# ------------------------------------------------------------------------------------------------ files

def unzip(archive: Path, into: Path):
    into.mkdir(parents=True)
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            p = (into / name).resolve()
            if name.startswith(("/", "\\")) or ":" in name or into.resolve() not in p.parents:
                raise SystemExit(f"{archive} has an unsafe path: {name}")
        z.extractall(into)
    for cand in [into] + sorted(p for p in into.iterdir() if p.is_dir()):
        if (cand / "source" / "aero" / "__main__.py").exists():
            return cand
    raise SystemExit(f"{archive} doesn't contain Aero")


def tree(folder: Path):
    """{relative path: sha256} of every file, without bytecode caches."""
    out = {}
    for dp, dns, fns in os.walk(folder):
        dns[:] = [d for d in dns if d != "__pycache__"]
        for f in fns:
            p = Path(dp, f)
            out[p.relative_to(folder).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def same_tree(got, want):
    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    changed = sorted(k for k in set(got) & set(want) if got[k] != want[k])
    ok = not (missing or extra or changed)
    return ok, "" if ok else f"missing {missing[:5]}, extra {extra[:5]}, different {changed[:5]}"


def version_in(source: Path):
    m = re.search(r'^VERSION = "([^"]+)"', (source / "aero" / "config.py").read_text(encoding="utf-8"), re.M)
    return m.group(1)


def sha256(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ------------------------------------------------------------------------------------------------ the PC around it

def outside_state():
    """Aero's shortcuts and Installed apps entries, to show a test install left them alone."""
    import winreg
    out = {}
    desktop = subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                             capture_output=True, text=True).stdout.strip()
    start = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs"
    for lnk in (Path(desktop) / "Aero.lnk", start / "Aero.lnk"):
        out[str(lnk)] = lnk.stat().st_mtime if lnk.exists() else None
    for hive, name in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"), (winreg.HKEY_CURRENT_USER, "HKCU")):
        try:
            with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Aero", 0,
                                winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)) as k:
                out[name] = [winreg.QueryValueEx(k, v)[0] for v in ("InstallLocation", "DisplayVersion")]
        except OSError:
            out[name] = None
    return out


def stop_everything_in(work: Path):
    """Stop every process started from the work folder (servers, venv launchers, an updater window), but never this
    script or whatever started it (their command lines name the folder too)."""
    w = str(work).lower()
    me = psutil.Process()
    spare = {me.pid} | {p.pid for p in me.parents()}
    for p in psutil.process_iter(["exe", "cmdline"]):
        if p.pid in spare:
            continue
        try:
            exe = (p.info["exe"] or "").lower()
            cmd = " ".join(p.info["cmdline"] or []).lower()
            if exe.startswith(w) or (Path(exe).name in ("cmd.exe", "python.exe", "pythonw.exe") and w in cmd):
                for c in p.children(recursive=True):
                    c.kill()
                p.kill()
        except (psutil.Error, OSError):
            pass


# ------------------------------------------------------------------------------------------------ Aero

class Aero:
    def __init__(self, port):
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.c = httpx.Client(base_url=self.url, timeout=60)

    def state(self):
        try:
            r = httpx.get(self.url + "/api/state", timeout=3)
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    def wait(self, timeout, version=None):
        t0 = time.time()
        while time.time() - t0 < timeout:
            s = self.state()
            if s and (version is None or s.get("version") == version):
                return s
            time.sleep(1)
        return None

    def wait_down(self, timeout=60):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.state() is None:
                return True
            time.sleep(0.5)
        return False

    def shutdown(self):
        try:
            self.c.post("/api/shutdown", timeout=5)
        except Exception:
            pass
        return self.wait_down()


def start_aero(dest: Path, env, log: Path):
    """Start an installed Aero the way its shortcut does, without the window."""
    return subprocess.Popen([str(dest / "venv" / "Scripts" / "python.exe"), "-m", "aero", "--no-window"],
                            cwd=dest / "app", env=env, stdin=subprocess.DEVNULL, stdout=open(log, "ab"),
                            stderr=subprocess.STDOUT, creationflags=0x00000200)   # CREATE_NEW_PROCESS_GROUP


def run_bat(bat: Path, args, env, log: Path, timeout=1800):
    """Run a batch file with nobody at the keyboard. Output goes to a file, not a pipe: the Aero it starts at the end
    inherits the handles and would hold a pipe open."""
    with open(log, "wb") as f:
        try:
            r = subprocess.run(["cmd", "/c", str(bat)] + [str(a) for a in args], cwd=bat.parent, env=env,
                               stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
            code = r.returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
    text = log.read_text(encoding="utf-8", errors="replace")
    print("     " + "\n     ".join(text.strip().splitlines()[-25:]), flush=True)
    return code, text


def base_env(port, work):
    env = {k: v for k, v in os.environ.items()
           if k not in ("AERO_HOME", "AERO_INSTALL_DIR", "AERO_INSTALL_FLAGS", "AERO_LLAMA_DIR", "AERO_UPDATE_API",
                        "AERO_UPDATE_ANY", "PYTHONPATH")}
    env.update(AERO_PORT=str(port), AERO_LLAMA_PORT=str(port + 1), AERO_UPDATE_CHECK="0",
               PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_CACHE_DIR=str(work / "pip-cache"), PYTHONUTF8="1")
    system32 = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32")
    env["PATH"] = system32 + os.pathsep + env.get("PATH", "")      # Windows' timeout.exe, not Git Bash's
    return env


def seed(a: Aero, dest: Path):
    """What check_upgrade.py writes, plus a model file and a stale app file."""
    st = a.c.get("/api/state").json()
    a.c.put("/api/settings", json={"user_profile": "Update test profile", "temperature": 0.55,
                                   "tool_policy": {**st["settings"].get("tool_policy", {}), "desktop": "auto"}})
    a.c.put("/api/chats/updchat1", json={"id": "updchat1", "title": "Update chat", "messages": [
        {"role": "user", "content": "hello from the old version"},
        {"role": "assistant", "content": "reply from the old version"}]})
    a.c.post("/api/memory", json={"text": "The update tester prefers metric units.", "kind": "user"})
    (dest / "models" / "keep-me.gguf").write_bytes(b"GGUF not a real model")
    (dest / "app" / "aero" / "zz_stale_from_old_version.py").write_text("# the update must remove this\n")
    return st["version"]


def data_kept(a: Aero, dest: Path, label):
    s = a.c.get("/api/state").json()["settings"]
    check(f"{label}: settings kept", s.get("user_profile") == "Update test profile" and s.get("temperature") == 0.55
          and s.get("tool_policy", {}).get("desktop") == "auto",
          f"{s.get('user_profile')!r}, {s.get('temperature')}, {s.get('tool_policy', {}).get('desktop')}")
    chat = a.c.get("/api/chats/updchat1")
    check(f"{label}: chat kept", chat.status_code == 200 and [m["content"] for m in chat.json()["messages"]]
          == ["hello from the old version", "reply from the old version"])
    check(f"{label}: memory fact kept", "metric units" in json.dumps(a.c.get("/api/memory").json()))
    m = dest / "models" / "keep-me.gguf"
    check(f"{label}: model file kept", m.exists() and m.read_bytes() == b"GGUF not a real model")


def test_flags_honoured(dest: Path, label):
    check(f"{label}: no llama.cpp download (--skip-llama)", not (dest / "llama").exists())
    check(f"{label}: no Installed apps entry (--no-register)", not (dest / "Uninstall-Aero.ps1").exists())


# ------------------------------------------------------------------------------------------------ phases

def install_previous(prev_root: Path, dest: Path, env, work: Path):
    bat = prev_root / "Update-Aero.bat"
    if "AERO_INSTALL_DIR" in bat.read_text(encoding="utf-8", errors="replace"):
        say(f"installing the previous release with its own installer into {dest}")
        code, _ = run_bat(bat, ["--dest", dest] + TEST_FLAGS + ["--headless"], env, work / "install-previous.log")
        if code != 0:
            raise SystemExit(f"the previous release's installer failed ({code})")
        return None
    say(f"the previous release's installer only installs into C:\\Aero; repeating its steps into {dest}")
    shutil.copytree(prev_root / "source", dest / "app", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(bat, dest / "Update-Aero.bat")
    shutil.copy2(prev_root / "source" / "installer" / "Uninstall-Aero.bat", dest / "Uninstall-Aero.bat")
    if (prev_root / "source" / "README.md").exists():
        shutil.copy2(prev_root / "source" / "README.md", dest / "README.md")
    log = open(work / "install-previous.log", "wb")

    def run(args, ok=True, **kw):
        r = subprocess.run([str(a) for a in args], env=env, stdin=subprocess.DEVNULL, stdout=log,
                           stderr=subprocess.STDOUT, **kw)
        if ok and r.returncode:
            raise SystemExit(f"{args[:4]} failed ({r.returncode}); see install-previous.log")
    run([sys.executable, "-m", "venv", dest / "venv"])
    vpy = dest / "venv" / "Scripts" / "python.exe"
    run([vpy, "-m", "pip", "install", "--upgrade", "pip", "-q"])
    run([vpy, "-m", "pip", "install", "--upgrade", "-r", dest / "app" / "requirements.txt", "-q"])
    if (dest / "app" / "requirements-extra.txt").exists():
        run([vpy, "-m", "pip", "install", "--upgrade", "-r", dest / "app" / "requirements-extra.txt", "-q"], ok=False)
    run([vpy, dest / "app" / "installer" / "setup.py", "--dest", dest, "--skip-llama", "--no-shortcuts",
         "--no-register"])
    run([vpy, "-m", "aero.migrate", "--settings-only"], cwd=dest / "app")
    log.close()
    return start_aero(dest, env, work / "aero-previous.log")


def cli_update(cand_root, cand_version, prev_version, dest, env, work, a: Aero, old_proc):
    say("running the new build's Update-Aero.bat over the install while the previous Aero runs")
    want = tree(cand_root / "source")
    code, out = run_bat(cand_root / "Update-Aero.bat", ["--dest", dest] + TEST_FLAGS + ["--headless"], env,
                        work / "update-cli.log")
    check("Update-Aero.bat finished without errors", code == 0 and "[ERROR]" not in out, f"exit {code}")
    check("it updated the given folder", f"Install folder: {dest}" in out)
    check("it didn't ask for administrator rights", "Requesting administrator rights" not in out)
    if old_proc is not None:
        try:
            old_proc.wait(30)
        except subprocess.TimeoutExpired:
            pass
        check("the running previous Aero was stopped", old_proc.poll() is not None)
    ok, detail = same_tree(tree(dest / "app"), want)
    check("app files are exactly the new build's (the stale file is gone)", ok, detail)
    check("Update-Aero.bat in the install is the new one",
          sha256(dest / "Update-Aero.bat") == sha256(cand_root / "Update-Aero.bat"))
    check("installed config.py has the new version",
          version_in(dest / "app") == cand_version != prev_version, f"{prev_version} -> {version_in(dest / 'app')}")
    s = a.wait(180, cand_version)
    check("the Aero it started answers /api/state with the new version", s is not None,
          (a.state() or {}).get("version"))
    if s:
        data_kept(a, dest, "update")
    test_flags_honoured(dest, "update")
    a.shutdown()


def fake_release(cand_zip: Path, www: Path, base_url):
    """The candidate zip as release FAKE_VERSION, plus a tampered copy with the same name."""
    www.mkdir(parents=True)

    def build(tamper):
        buf = io.BytesIO()
        with zipfile.ZipFile(cand_zip) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = zin.read(info)
                if info.filename.endswith("source/aero/config.py"):
                    data = re.sub(rb'^VERSION = "[^"]+"', f'VERSION = "{FAKE_VERSION}"'.encode(), data, flags=re.M)
                if tamper and info.filename.endswith("source/README.md"):
                    data += b"\nchanged after the checksums were published\n"
                zout.writestr(info, data)
        return buf.getvalue()
    good, bad = build(False), build(True)
    (www / "good.zip").write_bytes(good)
    (www / "tampered.zip").write_bytes(bad)
    (www / "SHA256SUMS.txt").write_text(f"{hashlib.sha256(good).hexdigest()}  Aero-windows.zip\n")
    (www / "api.json").write_text(json.dumps({
        "tag_name": f"v{FAKE_VERSION}", "html_url": base_url, "body": "Fake release for the update check",
        "published_at": "2026-10-11T00:00:00Z", "draft": False, "prerelease": False,
        "assets": [{"name": "Aero-windows.zip", "browser_download_url": f"{base_url}/Aero-windows.zip",
                    "size": len(good)},
                   {"name": "SHA256SUMS.txt", "browser_download_url": f"{base_url}/SHA256SUMS.txt"}]}))
    return good


def serve(www: Path, port):
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Quiet, directory=str(www)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def install_now(a: Aero):
    """POST /api/update/install and read its event stream. Returns (result or None, error or None)."""
    result = error = None
    with a.c.stream("POST", "/api/update/install", timeout=600) as r:
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.read()[:300]!r}"
        for line in r.iter_lines():
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                if ev.get("type") == "result":
                    result = ev.get("result")
                elif ev.get("type") == "error":
                    error = ev.get("error")
    return result, error


def in_app_update(cand_zip, cand_version, dest, env, work, a: Aero, fport):
    say(f"Aero's own updater against a fake release {FAKE_VERSION} on 127.0.0.1:{fport}")
    www = work / "fake-release"
    base = f"http://127.0.0.1:{fport}"
    good = fake_release(cand_zip, www, base)
    srv = serve(www, fport)
    env = dict(env, AERO_UPDATE_API=base + "/api.json", AERO_INSTALL_FLAGS=" ".join(TEST_FLAGS),
               AERO_INSTALL_DIR=str(dest))       # the updater passes this itself; set here too so a regression
    try:                                         # can never send a test update to C:\Aero
        start_aero(dest, env, work / "aero-in-app.log")
        check("the updated install starts", a.wait(120, cand_version) is not None)
        before = tree(dest / "app")

        shutil.copy2(www / "tampered.zip", www / "Aero-windows.zip")
        u = a.c.post("/api/update/check").json()
        check("Aero sees the fake release as an update", u.get("available") and u.get("latest") == FAKE_VERSION,
              f"latest {u.get('latest')}, available {u.get('available')}, error {u.get('error')}")
        res, err = install_now(a)
        check("a download that doesn't match SHA256SUMS.txt is refused", res is None and err
              and "doesn't match" in err, err or res)
        time.sleep(5)
        s = a.state()
        check("after the refusal Aero keeps running the same version", s and s.get("version") == cand_version,
              (s or {}).get("version"))
        ok, detail = same_tree(tree(dest / "app"), before)
        check("after the refusal the app files are untouched", ok, detail)
        check("after the refusal the update reports the error", a.c.get("/api/update").json().get("phase") == "error")

        (www / "Aero-windows.zip").write_bytes(good)
        a.c.post("/api/update/check")
        res, err = install_now(a)
        check("the genuine download is accepted and handed to Update-Aero.bat",
              res and res.get("installing") and res.get("version") == FAKE_VERSION, err or res)
        s = a.wait(900, FAKE_VERSION)
        check(f"Aero comes back by itself as {FAKE_VERSION}", s is not None, (a.state() or {}).get("version"))
        want = tree(unzip(www / "good.zip", work / "fake-unzipped") / "source")
        ok, detail = same_tree(tree(dest / "app"), want)
        check("app files are exactly the fake release's", ok, detail)
        if s:
            data_kept(a, dest, "in-app update")
        test_flags_honoured(dest, "in-app update")
        a.shutdown()
    finally:
        srv.shutdown()
        srv.server_close()


def fresh_install(cand_root, cand_version, work, port):
    dest = work / "fresh"
    env = base_env(port, work)
    say(f"fresh install of the new build into {dest}")
    code, out = run_bat(cand_root / "Update-Aero.bat", ["--dest", dest] + TEST_FLAGS + ["--headless"], env,
                        work / "install-fresh.log")
    check("fresh install: Update-Aero.bat hands over to Install-Aero.bat and finishes",
          code == 0 and "Aero installer" in out and "[ERROR]" not in out, f"exit {code}")
    check("fresh install: Python environment created", (dest / "venv" / "Scripts" / "python.exe").exists())
    ok, detail = same_tree(tree(dest / "app"), tree(cand_root / "source"))
    check("fresh install: app files are the new build's", ok, detail)
    check("fresh install: Update-Aero.bat and Uninstall-Aero.bat next to the app",
          (dest / "Update-Aero.bat").exists() and (dest / "Uninstall-Aero.bat").exists())
    a = Aero(port)
    check("fresh install: the Aero it started answers /api/state", a.wait(180, cand_version) is not None,
          (a.state() or {}).get("version"))
    test_flags_honoured(dest, "fresh install")
    a.shutdown()


# ------------------------------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--previous", required=True, type=Path, help="the previous release's Aero-windows.zip")
    ap.add_argument("--previous-sums", type=Path, help="its SHA256SUMS.txt, to check the download")
    ap.add_argument("--candidate", required=True, type=Path, help="the new build's Aero-windows.zip")
    ap.add_argument("--work", required=True, type=Path, help="throwaway folder (deleted and recreated)")
    ap.add_argument("--port", type=int, default=8390)
    ap.add_argument("--skip-in-app", action="store_true")
    ap.add_argument("--skip-fresh", action="store_true")
    a = ap.parse_args()
    if os.name != "nt":
        raise SystemExit("Windows only")
    work = a.work.resolve()
    if len(work.parts) < 3 or any(work == p or p in work.parents or work in p.parents for p in PROTECTED):
        raise SystemExit(f"refusing to use {work}: pick a throwaway folder")
    if work.exists():
        if any(work.iterdir()) and not (work / MARKER).exists():
            raise SystemExit(f"{work} isn't empty and wasn't made by this script; pick a new folder")
        stop_everything_in(work)
        shutil.rmtree(work)
    work.mkdir(parents=True)
    (work / MARKER).write_text("made by check_windows_update.py; deleted on its next run\n")

    if a.previous_sums:
        line = next((ln for ln in a.previous_sums.read_text().splitlines() if ln.endswith(" Aero-windows.zip")), "")
        check("the previous release's zip matches its SHA256SUMS.txt", line.split()[:1] == [sha256(a.previous)])
    prev_root = unzip(a.previous.resolve(), work / "previous")
    cand_root = unzip(a.candidate.resolve(), work / "candidate")
    prev_version, cand_version = version_in(prev_root / "source"), version_in(cand_root / "source")
    say(f"previous {prev_version}, new build {cand_version}")
    if cand_version == prev_version:
        raise SystemExit("the new build has the previous release's version; give it its own (CI adds -ci.<run>)")

    outside = outside_state()
    dest = work / "install"
    env = base_env(a.port, work)
    aero = Aero(a.port)
    ran = []
    try:
        proc = install_previous(prev_root, dest, env, work)
        s = aero.wait(180)
        if not check(f"the previous release ({prev_version}) installed in {dest.name} and running",
                     s and s.get("version") == prev_version, (s or {}).get("version")):
            raise SystemExit("the previous release didn't start; see aero-previous.log and install-previous.log")
        seed(aero, dest)
        ran.append("update")
        cli_update(cand_root, cand_version, prev_version, dest, env, work, aero, proc)
        if not a.skip_in_app:
            ran.append("in-app")
            in_app_update(a.candidate.resolve(), cand_version, dest, env, work, aero, a.port + 5)
        if not a.skip_fresh:
            ran.append("fresh")
            fresh_install(cand_root, cand_version, work, a.port + 20)
    except SystemExit as e:
        check(f"stopped early: {e}", False)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(f"stopped early: {type(e).__name__}: {e}", False)
    finally:
        stop_everything_in(work)
    after = outside_state()
    check("no Aero shortcut or Installed apps entry was created or changed", after == outside,
          "" if after == outside else f"{outside} -> {after}")
    passed = sum(ok for _, ok in results)
    print(f"{passed}/{len(results)} passed; phases run: {', '.join(ran) or 'none'}")
    (work / "results.json").write_text(json.dumps({"previous": prev_version, "candidate": cand_version,
                                                   "phases": ran, "checks": results}, indent=1))
    sys.exit(0 if results and all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()
