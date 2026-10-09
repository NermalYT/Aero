"""Aero installer helper (runs inside the new venv, called by Install-Aero.bat).

1. Detects the GPU and picks the right llama.cpp Windows build from the latest GitHub release and installs it to
   <dest>\\llama: NVIDIA gets the CUDA build matching its driver, AMD and Intel cards the Vulkan build, and a PC
   without a dedicated GPU the CPU build.
2. Puts the app icon next to the app folder.
3. Creates Desktop and Start Menu shortcuts that always launch as administrator.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

NO_WIN = 0x08000000
GH = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30"
ICON_NAME = "aero-bubble.ico"


def say(msg):
    print(f"  {msg}", flush=True)


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:2]) if v else (0, 0)


def gpu_info():
    exe = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    if not os.path.exists(exe):
        return None
    try:
        q = subprocess.run([exe, "--query-gpu=name,memory.total,compute_cap,driver_version", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=20, creationflags=NO_WIN).stdout.strip().splitlines()
        full = subprocess.run([exe], capture_output=True, text=True, timeout=20, creationflags=NO_WIN).stdout
    except Exception:
        return None
    if not q:
        return None
    name, mem, cc, drv = [x.strip() for x in q[0].split(",")[:4]]
    # older drivers print "CUDA Version: 12.8", newer ones "CUDA UMD Version: 13.4"
    m = re.search(r"CUDA(?:\s+UMD)?\s+Version:\s*([\d.]+)", full)
    cuda = m.group(1) if m else ""
    if not cuda:   # fall back to the driver branch: R580+ ships CUDA 13, R570+ CUDA 12.8
        major = vtuple(drv)[0]
        cuda = "13.0" if major >= 580 else "12.8" if major >= 570 else "12.4" if major >= 550 else "0"
    return {"name": name, "mem": mem, "cc": cc, "driver": drv, "cuda": cuda}


def other_gpus():
    """AMD and Intel graphics cards with their own memory, from the display driver registry (the same source Aero's
    hardware scan uses). Integrated graphics are left out: they run models no faster than the CPU build."""
    try:
        import winreg
    except ImportError:
        return []
    found = []
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return []
    for i in range(64):
        try:
            sub = winreg.EnumKey(root, i)
        except OSError:
            break
        if not sub.isdigit():
            continue
        try:
            k = winreg.OpenKey(root, sub)
            name = str(winreg.QueryValueEx(k, "DriverDesc")[0])
            raw = winreg.QueryValueEx(k, "HardwareInformation.qwMemorySize")[0]
            mem = int.from_bytes(raw, "little") if isinstance(raw, (bytes, bytearray)) else int(raw)
        except (OSError, ValueError):
            continue
        low = name.lower()
        if not any(v in low for v in ("amd", "radeon", "intel", "arc")) or mem < 2 * 2**30:
            continue
        if re.search(r"^(AMD )?Radeon(\(TM\))? (\d+M )?Graphics$|Vega \d+ Graphics|UHD Graphics|Iris|"
                     r"^Intel\(R\) (HD )?Graphics|^Intel\(R\) Arc\(TM\) Graphics$", name, re.I):
            continue
        found.append(f"{name} ({round(mem / 2**30)} GB)")
    return found


def http_get(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": "Aero-installer", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read() if binary else json.loads(r.read().decode())


def download(url, dest: Path):
    req = urllib.request.Request(url, headers={"User-Agent": "Aero-installer"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got, t0, last = 0, time.time(), 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if time.time() - last > 0.5:
                last = time.time()
                pct = f"{got / total * 100:5.1f}%" if total else ""
                print(f"\r    {dest.name}: {got / 2**20:7.1f} MB {pct}  {got / 2**20 / max(time.time() - t0, 0.01):6.1f} MB/s",
                      end="", flush=True)
    print()


def pick_assets(rel, gpu, others=()):
    assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
    tag = rel["tag_name"]
    cuda_builds = []
    for n in assets:
        m = re.fullmatch(rf"llama-{re.escape(tag)}-bin-win-cuda-([\d.]+)-x64\.zip", n)
        if m:
            cuda_builds.append(m.group(1))
    cuda_builds.sort(key=vtuple, reverse=True)
    if gpu:
        drv = vtuple(gpu["cuda"])
        ok = [v for v in cuda_builds if vtuple(v) <= drv]
        blackwell = vtuple(gpu["cc"]) >= (12, 0)
        if blackwell:
            ok = [v for v in ok if vtuple(v) >= (12, 8)]
        if ok:
            v = ok[0]
            picks = [assets[f"llama-{tag}-bin-win-cuda-{v}-x64.zip"]]
            rt = f"cudart-llama-bin-win-cuda-{v}-x64.zip"
            if rt in assets:
                picks.append(assets[rt])
            return f"CUDA {v}", picks
        say(f"No CUDA build in {tag} matches driver CUDA {gpu['cuda']}"
            + (" (RTX 50-series needs a CUDA 12.8+ build)" if blackwell else "") + "; using Vulkan.")
    # Vulkan runs on AMD, Intel and NVIDIA cards alike; a PC without a dedicated GPU gets the plain CPU build, which
    # doesn't depend on a Vulkan driver being present.
    for kind in (("vulkan", "cpu") if gpu or others else ("cpu", "vulkan")):
        n = f"llama-{tag}-bin-win-{kind}-x64.zip"
        if n in assets:
            return kind.upper(), [assets[n]]
    raise RuntimeError("No suitable Windows x64 build found in the latest llama.cpp release.")


def install_llama(dest: Path, force=False):
    gpu = gpu_info()
    others = [] if gpu else other_gpus()
    if gpu:
        say(f"GPU: {gpu['name']} ({gpu['mem']}), compute {gpu['cc']}, driver {gpu['driver']} (CUDA {gpu['cuda']})")
    elif others:
        say("GPU: " + ", ".join(others) + " (llama.cpp's Vulkan build runs on it)")
    else:
        say("No dedicated GPU found: models will run on the CPU.")
    # "latest" can be a source-only release; use the newest one that ships Windows x64 builds
    rel = next((x for x in http_get(GH) if any(re.fullmatch(rf"llama-{re.escape(x['tag_name'])}-bin-win-.+-x64\.zip", a["name"])
                                              for a in x.get("assets", []))), None)
    if not rel:
        raise RuntimeError("No recent llama.cpp release has Windows x64 builds.")
    tag = rel["tag_name"]
    target = dest / "llama"
    ver_file = target / "VERSION.txt"
    kind, urls = pick_assets(rel, gpu, others)
    if ver_file.exists() and not force and ver_file.read_text().strip() == f"{tag} {kind}":
        say(f"llama.cpp {tag} ({kind}) already installed.")
        return
    say(f"Installing llama.cpp {tag} ({kind} build)")
    tmp = Path(tempfile.mkdtemp(prefix="aero-"))
    staging = dest / "llama.new"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for u in urls:
        z = tmp / u.rsplit("/", 1)[-1]
        download(u, z)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(staging)
    exe = next(staging.rglob("llama-server.exe"), None)
    if not exe:
        raise RuntimeError("llama-server.exe missing from the downloaded archive")
    # cudart DLLs must sit next to the exe
    for dll in staging.glob("*.dll"):
        if dll.parent != exe.parent:
            shutil.move(str(dll), exe.parent / dll.name)
    (staging / "VERSION.txt").write_text(f"{tag} {kind}")
    out = subprocess.run([str(exe), "--list-devices"], capture_output=True, text=True, timeout=60,
                         cwd=str(exe.parent), creationflags=NO_WIN)
    devs = (out.stdout + out.stderr).strip()
    say("llama.cpp devices:\n      " + "\n      ".join(l for l in devs.splitlines() if l.strip())[:800])
    if gpu and "CUDA" in kind and "CUDA" not in devs:
        say("WARNING: the CUDA build did not list your GPU. Update the NVIDIA driver, then re-run the installer.")
    if kind == "VULKAN" and "Vulkan" not in devs:
        say("WARNING: the Vulkan build did not list your GPU. Update the graphics driver, then re-run the installer. "
            "Models still run on the CPU until then.")
    # stop Aero's own llama-server processes (never other apps' llama.cpp), then swap folders
    try:
        import psutil
        for p in psutil.process_iter(["name", "exe"]):
            exe_path = (p.info.get("exe") or "").lower()
            if exe_path.startswith(str(target).lower() + os.sep):
                p.kill()
    except Exception:
        pass
    time.sleep(1)
    old = dest / "llama.old"
    shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        try:
            target.rename(old)
        except OSError as e:
            shutil.rmtree(staging, ignore_errors=True)
            raise RuntimeError(f"the current llama.cpp folder is in use ({e}); close Aero and run the update again")
    staging.rename(target)
    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)


def make_icon(dest: Path):
    """Copy Aero's icon (the iridescent bubble, drawn in source/brand) next to the app folder. It lives in
    <dest> rather than <dest>\\app so the updater's mirror copy never touches the file the shortcuts point at.
    The file name carries the design so Windows' icon cache, keyed by path, cannot keep showing an older icon."""
    src = dest / "app" / "aero" / "static" / "icons" / ICON_NAME
    ico = dest / ICON_NAME
    try:
        shutil.copyfile(src, ico)
    except OSError as e:
        say(f"Could not copy the icon ({e}); shortcuts will use the Python icon.")
        return None
    for name in ("halcyon.ico", "halcyon-bubble.ico", "vrampire.ico"):   # icons from the app's earlier names
        try:
            (dest / name).unlink(missing_ok=True)
        except OSError:
            pass
    return ico


def ps(cmd):
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                          capture_output=True, text=True, creationflags=NO_WIN)


def make_shortcuts(dest: Path, ico: Path):
    desktop = ps("[Environment]::GetFolderPath('Desktop')").stdout.strip() or str(Path.home() / "Desktop")
    start = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs"
    target = dest / "venv" / "Scripts" / "pythonw.exe"
    made = []
    for folder in (Path(desktop), start):
        for name in ("Halcyon.lnk", "VRAMpire.lnk"):     # the app's earlier names
            old = folder / name
            if old.exists():
                try:
                    old.unlink()
                    say(f"Removed old shortcut {old}")
                except OSError:
                    pass
        lnk = folder / "Aero.lnk"
        cmd = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
               f"$s.TargetPath='{target}';$s.Arguments='-m aero';$s.WorkingDirectory='{dest / 'app'}';"
               + (f"$s.IconLocation='{ico},0';" if ico else "") + "$s.Description='Aero: local LLM bootstrapper';$s.Save()")
        r = ps(cmd)
        if r.returncode != 0 or not lnk.exists():
            say(f"Could not create {lnk}: {r.stderr.strip()[:300]}")
            continue
        b = bytearray(lnk.read_bytes())
        b[0x15] |= 0x20          # SLDF_RUNAS_USER: "Run as administrator" on every launch
        lnk.write_bytes(bytes(b))
        made.append(str(lnk))
    for m in made:
        say(f"Shortcut: {m}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True)
    ap.add_argument("--force-llama", action="store_true")
    ap.add_argument("--skip-llama", action="store_true")
    ap.add_argument("--no-shortcuts", action="store_true", help="leave the Desktop and Start Menu shortcuts alone")
    a = ap.parse_args()
    dest = Path(a.dest)
    (dest / "data").mkdir(parents=True, exist_ok=True)
    (dest / "models").mkdir(parents=True, exist_ok=True)
    if not a.skip_llama:
        try:
            install_llama(dest, a.force_llama)
        except Exception as e:  # noqa: BLE001
            say(f"ERROR installing llama.cpp: {e}")
            if not (dest / "llama").exists():
                say("Download it manually from https://github.com/ggml-org/llama.cpp/releases (NVIDIA: win-cuda x64 "
                    f"+ cudart; AMD or Intel: win-vulkan x64; no GPU: win-cpu x64) and unzip it into {dest / 'llama'}")
                return 2
    ico = make_icon(dest)
    if not a.no_shortcuts:
        make_shortcuts(dest, ico)
    return 0


if __name__ == "__main__":
    sys.exit(main())
