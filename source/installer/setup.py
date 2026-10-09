"""Aero installer helper. It runs inside Aero's own Python environment; the install scripts call it
(Install-Aero.bat and Update-Aero.bat on Windows, install.sh on Linux and macOS).

1. Detects the GPU and installs the matching llama.cpp build from the newest llama.cpp release into <dest>/llama:
     Windows  NVIDIA: the CUDA build matching the driver · AMD and Intel cards: Vulkan · no dedicated GPU: CPU
     Linux    NVIDIA: the CUDA build matching the driver · AMD and Intel cards: Vulkan · no dedicated GPU: CPU
              When the prebuilt binaries can't run here (musl distros such as Alpine, a glibc older than the
              build's, NixOS, a CPU architecture without a build) it compiles llama.cpp from source instead.
     macOS    Apple Silicon: the Metal build · Intel Macs: the x64 build
2. Puts the app icon next to the app folder.
3. Creates shortcuts:
     Windows  Desktop and Start Menu shortcuts that always launch as administrator
     Linux    an app-menu entry (~/.local/share/applications/aero.desktop) and the `aero` command (~/.local/bin)
     macOS    ~/Applications/Aero.app and the `aero` command (~/.local/bin)

Exit codes: 0 done · 2 failed · 3 needs system packages first: install.sh reads <dest>/data/setup-needs.txt
(one of "vulkan", "build-tools"), installs them with the distro's package manager and runs this again.
"""
import argparse
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
NO_WIN = 0x08000000 if IS_WIN else 0
LLAMA_REPO = "ggml-org/llama.cpp"
GH = f"https://api.github.com/repos/{LLAMA_REPO}/releases?per_page=30"
ICON_NAME = "aero-bubble.ico"
EXE = "llama-server.exe" if IS_WIN else "llama-server"


class NeedPackages(Exception):
    """The machine is missing system packages that only the install script (with the package manager) can add."""

    def __init__(self, need, why):
        super().__init__(why)
        self.need = need


def say(msg):
    print(f"  {msg}", flush=True)


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:2]) if v else (0, 0)


def arch():
    m = platform.machine().lower()
    return {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(m, m)


# ---------------------------------------------------------------------------------------------------- GPU detection

def gpu_info():
    """The first NVIDIA GPU (name, memory, compute capability, driver, the CUDA version the driver supports)."""
    exe = shutil.which("nvidia-smi") or (r"C:\Windows\System32\nvidia-smi.exe" if IS_WIN else "")
    if not exe or not os.path.exists(exe):
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


_IGPU = re.compile(r"^(AMD )?Radeon(\(TM\))? (\d+M )?Graphics$|Vega \d+ Graphics|UHD Graphics|Iris|"
                   r"^Intel\(R\) (HD )?Graphics|^Intel\(R\) Arc\(TM\) Graphics$", re.I)


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
        if _IGPU.search(name):
            continue
        found.append(f"{name} ({round(mem / 2**30)} GB)")
    return found


# Intel Arc / Battlemage discrete cards by PCI device id (integrated Intel graphics use other ids)
_INTEL_DGPU = re.compile(r"^0x(56[0-9a-f]{2}|e20[0-9a-f]|e21[0-9a-f])$", re.I)


def linux_gpus(sysfs="/sys/class/drm"):
    """AMD and Intel GPUs worth the Vulkan build, from sysfs: AMD cards with at least 2 GB of VRAM, Intel Arc cards."""
    found = []
    try:
        cards = sorted(p for p in os.listdir(sysfs) if re.fullmatch(r"card\d+", p))
    except OSError:
        return []
    for c in cards:
        d = os.path.join(sysfs, c, "device")
        try:
            vendor = open(os.path.join(d, "vendor")).read().strip().lower()
            device = open(os.path.join(d, "device")).read().strip().lower()
        except OSError:
            continue
        if vendor == "0x1002":
            try:
                vram = int(open(os.path.join(d, "mem_info_vram_total")).read()) // 2**20
            except (OSError, ValueError):
                vram = 0
            if vram >= 2048:
                found.append(f"AMD GPU ({round(vram / 1024)} GB)")
        elif vendor == "0x8086" and _INTEL_DGPU.match(device):
            found.append(f"Intel Arc GPU ({device})")
    return found


# ---------------------------------------------------------------------------------------------------- releases

def http_get(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": "Aero-installer", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read() if binary else json.loads(r.read().decode())


def _exists(url):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Aero-installer"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status < 400
    except urllib.error.HTTPError as e:
        return e.code < 400
    except Exception:
        return False


def _latest_tag():
    """The newest llama.cpp release tag from the /releases/latest redirect (no API call)."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(f"https://github.com/{LLAMA_REPO}/releases/latest", method="HEAD",
                                 headers={"User-Agent": "Aero-installer"})
    try:
        opener.open(req, timeout=30)
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location") or ""
        m = re.search(r"/tag/([^/?#]+)", loc)
        if m:
            return m.group(1)
    return None


def candidate_names(tag):
    """Asset names a release normally has, for building a release listing without the GitHub API."""
    names = []
    for a in ("x64", "arm64"):
        names += [f"llama-{tag}-bin-win-{k}-{a}.zip" for k in ("cpu", "vulkan", "cuda-12.4", "cuda-13.4")]
        names += [f"cudart-llama-bin-win-cuda-{v}-{a}.zip" for v in ("12.4", "13.4")]
        names += [f"llama-{tag}-bin-ubuntu-{k}{a}.tar.gz" for k in ("", "vulkan-", "cuda-12.8-", "cuda-13.4-")]
        names += [f"cudart-llama-{tag}-bin-ubuntu-cuda-{v}-{a}.tar.gz" for v in ("12.8", "13.4")]
        names.append(f"llama-{tag}-bin-macos-{a}.tar.gz")
    return names


def releases():
    """Recent llama.cpp releases with their assets. Uses the GitHub API; when that is unreachable or rate-limited it
    finds the latest tag through the release page's redirect and checks which of the usual files exist.
    AERO_LLAMA_TAG pins a tag (the asset list is then probed the same way)."""
    tag = os.environ.get("AERO_LLAMA_TAG")
    if not tag:
        try:
            return http_get(GH)
        except Exception as e:  # noqa: BLE001
            say(f"GitHub's API didn't answer ({e}); looking up the latest release directly.")
            tag = _latest_tag()
    if not tag:
        raise RuntimeError("Could not reach GitHub to find a llama.cpp release.")
    base = f"https://github.com/{LLAMA_REPO}/releases/download/{tag}/"
    wanted = [n for n in candidate_names(tag) if _platform_match(n, tag)]
    assets = [{"name": n, "browser_download_url": base + n} for n in wanted if _exists(base + n)]
    return [{"tag_name": tag, "assets": assets}]


def _platform_match(name, tag):
    if IS_WIN:
        return "-bin-win-" in name
    if IS_MAC:
        return "-bin-macos-" in name
    return "-bin-ubuntu-" in name


def _has_build(rel):
    tag = re.escape(rel["tag_name"])
    pat = (rf"llama-{tag}-bin-win-.+-{arch()}\.zip" if IS_WIN else rf"llama-{tag}-bin-macos-{arch()}\.tar\.gz" if IS_MAC
           else rf"llama-{tag}-bin-ubuntu-(.+-)?{arch()}\.tar\.gz")
    return any(re.fullmatch(pat, a["name"]) for a in rel.get("assets", []))


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


# ---------------------------------------------------------------------------------------------------- build choice

def _cuda_pick(versions, gpu):
    versions = sorted(versions, key=vtuple, reverse=True)
    drv = vtuple(gpu["cuda"])
    ok = [v for v in versions if vtuple(v) <= drv]
    blackwell = vtuple(gpu["cc"]) >= (12, 0)
    if blackwell:
        ok = [v for v in ok if vtuple(v) >= (12, 8)]
    if ok:
        return ok[0]
    say(f"No CUDA build matches driver CUDA {gpu['cuda']}" + (" (RTX 50-series needs a CUDA 12.8+ build)" if blackwell else "")
        + "; using Vulkan.")
    return None


def pick_assets(rel, gpu, others=()):
    """Windows: (kind, [urls]) for this PC's GPU."""
    assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
    tag, a = rel["tag_name"], "x64" if not IS_WIN else (arch() if arch() in ("x64", "arm64") else "x64")
    cuda_builds = [m.group(1) for n in assets
                   for m in [re.fullmatch(rf"llama-{re.escape(tag)}-bin-win-cuda-([\d.]+)-{a}\.zip", n)] if m]
    if gpu and cuda_builds:
        v = _cuda_pick(cuda_builds, gpu)
        if v:
            picks = [assets[f"llama-{tag}-bin-win-cuda-{v}-{a}.zip"]]
            rt = f"cudart-llama-bin-win-cuda-{v}-{a}.zip"
            if rt in assets:
                picks.append(assets[rt])
            return f"CUDA {v}", picks
    # Vulkan runs on AMD, Intel and NVIDIA cards alike; a PC without a dedicated GPU gets the plain CPU build, which
    # doesn't depend on a Vulkan driver being present.
    for kind in (("vulkan", "cpu") if gpu or others else ("cpu", "vulkan")):
        n = f"llama-{tag}-bin-win-{kind}-{a}.zip"
        if n in assets:
            return kind.upper(), [assets[n]]
    raise RuntimeError(f"No suitable Windows {a} build found in llama.cpp {tag}.")


def pick_assets_linux(rel, gpu, others=(), cpu_arch=None, avoid=()):
    """Linux: (kind, [urls]). avoid: kinds that already failed here (e.g. VULKAN without a Vulkan loader)."""
    assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
    tag, a = rel["tag_name"], cpu_arch or arch()
    if gpu and "CUDA" not in avoid:
        cuda_builds = [m.group(1) for n in assets
                       for m in [re.fullmatch(rf"llama-{re.escape(tag)}-bin-ubuntu-cuda-([\d.]+)-{a}\.tar\.gz", n)] if m]
        v = _cuda_pick(cuda_builds, gpu) if cuda_builds else None
        if v:
            picks = [assets[f"llama-{tag}-bin-ubuntu-cuda-{v}-{a}.tar.gz"]]
            rt = f"cudart-llama-{tag}-bin-ubuntu-cuda-{v}-{a}.tar.gz"
            if rt in assets:
                picks.append(assets[rt])
            return f"CUDA {v}", picks
    order = ("VULKAN", "CPU") if (gpu or others) else ("CPU", "VULKAN")
    for kind in order:
        if kind in avoid:
            continue
        n = f"llama-{tag}-bin-ubuntu-{'vulkan-' if kind == 'VULKAN' else ''}{a}.tar.gz"
        if n in assets:
            return kind, [assets[n]]
    raise RuntimeError(f"No prebuilt Linux {a} build in llama.cpp {tag}.")


def pick_assets_mac(rel, cpu_arch=None):
    assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
    tag, a = rel["tag_name"], cpu_arch or arch()
    n = f"llama-{tag}-bin-macos-{a}.tar.gz"
    if n not in assets:
        raise RuntimeError(f"No macOS {a} build in llama.cpp {tag}.")
    return ("METAL" if a == "arm64" else "CPU"), [assets[n]]


# ---------------------------------------------------------------------------------------------------- install

def _extract(archive: Path, into: Path):
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for m in zf.namelist():
                if m.startswith("/") or ".." in Path(m).parts:
                    raise RuntimeError(f"unsafe path in {archive.name}: {m}")
            zf.extractall(into)
        return
    with tarfile.open(archive) as tf:
        try:
            tf.extractall(into, filter="data")              # refuses absolute paths, links out of the folder, devices
        except TypeError:                                   # Python without extraction filters
            for m in tf.getmembers():
                if m.name.startswith("/") or ".." in Path(m.name).parts or m.isdev():
                    raise RuntimeError(f"unsafe path in {archive.name}: {m.name}")
            tf.extractall(into)


def _flatten_libs(exe: Path, staging: Path):
    """CUDA runtime libraries ship in a separate archive; they must sit next to llama-server."""
    pats = ("*.dll",) if IS_WIN else ("*.so", "*.so.*", "*.dylib")
    for pat in pats:
        for lib in list(staging.rglob(pat)):
            if lib.parent != exe.parent and not (exe.parent / lib.name).exists():
                shutil.move(str(lib), exe.parent / lib.name)


def _env_for(exe: Path):
    env = dict(os.environ)
    if not IS_WIN:
        key = "DYLD_LIBRARY_PATH" if IS_MAC else "LD_LIBRARY_PATH"
        env[key] = str(exe.parent) + (os.pathsep + env[key] if env.get(key) else "")
    return env


def _run_exe(exe: Path, args, timeout=60):
    return subprocess.run([str(exe)] + args, capture_output=True, text=True, timeout=timeout, cwd=str(exe.parent),
                          creationflags=NO_WIN, env=_env_for(exe))


def _runs(exe: Path):
    """(ok, output) of `llama-server --version`: proves the binary and its libraries load on this machine."""
    try:
        r = _run_exe(exe, ["--version"])
    except OSError as e:                     # exec format error, missing ELF interpreter (musl, NixOS)
        return False, str(e)
    out = (r.stdout + r.stderr).strip()
    return r.returncode == 0 and "version" in out.lower(), out


def _stop_our_servers(target: Path):
    """Stop Aero's own llama.cpp processes (never other apps' llama.cpp) so the folder can be replaced."""
    try:
        import psutil
        for p in psutil.process_iter(["name", "exe"]):
            exe_path = (p.info.get("exe") or "").lower()
            if exe_path.startswith(str(target).lower() + os.sep):
                p.kill()
    except Exception:
        pass
    time.sleep(1)


def _swap_in(staging: Path, dest: Path):
    target = dest / "llama"
    _stop_our_servers(target)
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


def _show_devices(exe: Path, kind, gpu):
    try:
        out = _run_exe(exe, ["--list-devices"])
        devs = (out.stdout + out.stderr).strip()
    except Exception as e:  # noqa: BLE001
        devs = f"(could not list devices: {e})"
    say("llama.cpp devices:\n      " + "\n      ".join(l for l in devs.splitlines() if l.strip())[:800])
    if gpu and "CUDA" in kind and "CUDA" not in devs:
        say("WARNING: the CUDA build did not list your GPU. Update the NVIDIA driver, then run the installer again.")
    if kind == "VULKAN" and "Vulkan" not in devs:
        say("WARNING: the Vulkan build did not list your GPU. Update the graphics driver (on Linux: Mesa's Vulkan "
            "driver), then run the installer again. Models run on the CPU until then.")
    return devs


def _installed(dest: Path):
    f = dest / "llama" / "VERSION.txt"
    return f.read_text().strip() if f.exists() else ""


def install_llama(dest: Path, force=False, build=False, avoid=()):
    if IS_WIN:
        gpu = gpu_info()
        others = [] if gpu else other_gpus()
    elif IS_LINUX:
        gpu = gpu_info()
        others = [] if gpu else linux_gpus()
    else:
        gpu, others = None, []
    if gpu:
        say(f"GPU: {gpu['name']} ({gpu['mem']}), compute {gpu['cc']}, driver {gpu['driver']} (CUDA {gpu['cuda']})")
    elif others:
        say("GPU: " + ", ".join(others) + " (llama.cpp's Vulkan build runs on it)")
    elif IS_MAC:
        say("Apple Silicon: llama.cpp runs on the GPU through Metal." if arch() == "arm64" else
            "Intel Mac: models run on the CPU.")
    else:
        say("No dedicated GPU found: models will run on the CPU.")
    if build:
        return build_from_source(dest, gpu, others, force)
    rels = releases()
    # "latest" can be a source-only release; use the newest one that ships builds for this OS and CPU
    rel = next((x for x in rels if _has_build(x)), None)
    if not rel:
        if IS_LINUX:
            say(f"llama.cpp publishes no prebuilt Linux {arch()} build; compiling it from source instead.")
            return build_from_source(dest, gpu, others, force, tag=rels[0]["tag_name"] if rels else None)
        raise RuntimeError(f"No recent llama.cpp release has {platform.system()} {arch()} builds.")
    tag = rel["tag_name"]
    if IS_WIN:
        kind, urls = pick_assets(rel, gpu, others)
    elif IS_MAC:
        kind, urls = pick_assets_mac(rel)
    else:
        kind, urls = pick_assets_linux(rel, gpu, others, avoid=avoid)
    have = _installed(dest)
    if have and not force and have in (f"{tag} {kind}", f"{tag} {kind} (built here)"):
        say(f"llama.cpp {tag} ({kind}) already installed.")
        return
    say(f"Installing llama.cpp {tag} ({kind} build)")
    tmp = Path(tempfile.mkdtemp(prefix="aero-"))
    staging = dest / "llama.new"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        for u in urls:
            z = tmp / u.rsplit("/", 1)[-1]
            download(u, z)
            _extract(z, staging)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    exe = next(staging.rglob(EXE), None)
    if not exe:
        raise RuntimeError(f"{EXE} missing from the downloaded archive")
    _flatten_libs(exe, staging)
    if not IS_WIN:
        for f in exe.parent.iterdir():
            if f.is_file() and (f.name.startswith("llama-") or f.suffix in (".so", ".dylib") or ".so." in f.name):
                f.chmod(f.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if not IS_WIN:
        ok, out = _runs(exe)
        if not ok:
            shutil.rmtree(staging, ignore_errors=True)
            if IS_LINUX and "libvulkan" in out and kind == "VULKAN":
                if "vulkan" not in avoid:
                    raise NeedPackages("vulkan", "The Vulkan build needs the Vulkan loader (libvulkan.so.1).")
                say("The Vulkan loader is still missing; using the CPU build.")
                return install_llama(dest, force, avoid=tuple(avoid) + ("VULKAN",))
            if IS_LINUX and "libgomp" in out and "openmp" not in avoid:
                raise NeedPackages("openmp", "The prebuilt llama.cpp needs the OpenMP runtime (libgomp.so.1).")
            if IS_LINUX:
                say(f"The prebuilt llama.cpp can't run on this system ({out.strip()[-300:] or 'no output'}); "
                    "compiling it from source instead.")
                return build_from_source(dest, gpu, others, force, tag=tag)
            raise RuntimeError(f"llama-server doesn't start on this Mac: {out.strip()[-400:]}")
    (staging / "VERSION.txt").write_text(f"{tag} {kind}")
    _show_devices(exe, kind, gpu)
    _swap_in(staging, dest)


def _build_tools():
    cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    return {"cmake": shutil.which("cmake"), "cxx": cxx,
            "gen": shutil.which("ninja") or shutil.which("make") or shutil.which("gmake")}


def build_from_source(dest: Path, gpu, others, force=False, tag=None):
    """Compile llama-server and llama-bench for this machine (static binaries, tuned for this CPU)."""
    tools = _build_tools()
    missing = [k for k, v in tools.items() if not v]
    if missing:
        raise NeedPackages("build-tools", "Compiling llama.cpp needs " + ", ".join(
            {"cmake": "CMake", "cxx": "a C++ compiler", "gen": "make or ninja"}[k] for k in missing) + ".")
    if not tag:
        tag = os.environ.get("AERO_LLAMA_TAG") or _latest_tag()
        if not tag:
            tag = http_get(GH)[0]["tag_name"]
    flags = ["-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=OFF", "-DGGML_NATIVE=ON", "-DLLAMA_OPENSSL=OFF",
             "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF", "-DLLAMA_USE_PREBUILT_UI=OFF",
             "-DLLAMA_BUILD_UI=OFF"]
    kind = "CPU"
    if gpu and shutil.which("nvcc"):
        flags.append("-DGGML_CUDA=ON")
        kind = "CUDA"
    elif (gpu or others) and shutil.which("glslc") and _has_vulkan_headers():
        flags.append("-DGGML_VULKAN=ON")
        kind = "VULKAN"
    elif gpu or others:
        say("No GPU compiler toolkit found (CUDA's nvcc, or glslc + Vulkan headers): building the CPU version. "
            "Install those and run the installer again with --build-llama for GPU speed.")
    if IS_MAC:
        kind = "METAL" if arch() == "arm64" else "CPU"
    have = _installed(dest)
    if have == f"{tag} {kind} (built here)" and not force:
        say(f"llama.cpp {tag} ({kind}, built here) already installed.")
        return
    say(f"Compiling llama.cpp {tag} ({kind}) from source. This takes a few minutes.")
    work = Path(tempfile.mkdtemp(prefix="aero-build-"))
    try:
        src = _fetch_source(tag, work)
        gen = ["-G", "Ninja"] if tools["gen"] and Path(tools["gen"]).name == "ninja" else []
        jobs = str(max(1, (os.cpu_count() or 2)))
        for cmd in (["cmake", "-S", str(src), "-B", str(work / "build")] + gen + flags,
                    ["cmake", "--build", str(work / "build"), "--config", "Release", "-j", jobs,
                     "--target", "llama-server", "llama-bench"]):
            say("$ " + " ".join(cmd))
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError("the llama.cpp build failed:\n" + (r.stdout + r.stderr)[-2500:])
        bin_dir = work / "build" / "bin"
        staging = dest / "llama.new"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        for name in ("llama-server", "llama-bench"):
            shutil.copy2(bin_dir / name, staging / name)
        (staging / "VERSION.txt").write_text(f"{tag} {kind} (built here)")
        ok, out = _runs(staging / EXE)
        if not ok:
            raise RuntimeError(f"the freshly built llama-server doesn't run: {out[-400:]}")
        _show_devices(staging / EXE, kind, gpu)
        _swap_in(staging, dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _fetch_source(tag, work: Path):
    """llama.cpp's source for <tag>: AERO_LLAMA_SRC (a local checkout), the GitHub source archive, or a shallow
    git clone when the archive can't be downloaded."""
    if os.environ.get("AERO_LLAMA_SRC"):
        return Path(os.environ["AERO_LLAMA_SRC"])
    try:
        src_tgz = work / "src.tar.gz"
        download(f"https://github.com/{LLAMA_REPO}/archive/refs/tags/{tag}.tar.gz", src_tgz)
        _extract(src_tgz, work)
        return next(p for p in work.iterdir() if p.is_dir() and (p / "CMakeLists.txt").exists())
    except Exception as e:  # noqa: BLE001
        if not shutil.which("git"):
            raise
        say(f"Source archive download failed ({e}); cloning with git instead.")
        dst = work / "llama.cpp"
        r = subprocess.run(["git", "clone", "--depth", "1", "--branch", tag, f"https://github.com/{LLAMA_REPO}", str(dst)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("git clone failed: " + r.stderr[-600:])
        return dst


def _has_vulkan_headers():
    return any(Path(p).exists() for p in ("/usr/include/vulkan/vulkan.h", "/usr/local/include/vulkan/vulkan.h"))


# ---------------------------------------------------------------------------------------------------- shortcuts

def make_icon(dest: Path):
    """Copy Aero's icon (the iridescent bubble, drawn in source/brand) next to the app folder. It lives in
    <dest> rather than <dest>\\app so the updater's mirror copy never touches the file the shortcuts point at.
    The file name carries the design so Windows' icon cache, keyed by path, cannot keep showing an older icon."""
    icons = dest / "app" / "aero" / "static" / "icons"
    src, ico = (icons / ICON_NAME, dest / ICON_NAME) if IS_WIN else (icons / "aero-256.png", dest / "aero.png")
    try:
        shutil.copyfile(src, ico)
    except OSError as e:
        say(f"Could not copy the icon ({e}); shortcuts will use a generic icon.")
        return None
    if IS_WIN:
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


def _sh_quote(s):
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


def launcher_script(dest: Path):
    """<dest>/bin/aero: runs Aero from its own environment. Every shortcut and the `aero` command point here."""
    return ("#!/bin/sh\n"
            "# Starts Aero. Written by Aero's installer; the next install or update rewrites it.\n"
            f"cd {_sh_quote(dest / 'app')} || exit 1\n"
            f"exec {_sh_quote(dest / 'venv' / 'bin' / 'python')} -m aero \"$@\"\n")


def _write_exec(path: Path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _link_command(dest: Path):
    """~/.local/bin/aero -> <dest>/bin/aero (a symlink, so it never goes stale)."""
    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cmd = bin_dir / "aero"
    try:
        if cmd.is_symlink() or cmd.exists():
            cmd.unlink()
        cmd.symlink_to(dest / "bin" / "aero")
    except OSError as e:
        say(f"Could not create {cmd} ({e}).")
        return None
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        say(f"Note: {bin_dir} isn't on your PATH yet, so open a new terminal (or log out and in) before typing `aero`.")
    return cmd


def desktop_entry(dest: Path, ico):
    exe = str(dest / "bin" / "aero")
    q = f'"{exe}"' if " " in exe else exe
    return ("[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Aero\n"
            "GenericName=Local AI\n"
            "Comment=Run local language models with tools, agents and per-PC tuning\n"
            f"Exec={q}\n"
            f"TryExec={exe}\n"
            + (f"Icon={ico}\n" if ico else "Icon=applications-science\n") +
            "Terminal=false\n"
            "Categories=Development;Utility;\n"
            "Keywords=AI;LLM;llama;chat;agent;\n"
            "StartupNotify=true\n")


def make_linux_shortcuts(dest: Path, ico):
    _write_exec(dest / "bin" / "aero", launcher_script(dest))
    apps = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    entry = apps / "aero.desktop"
    entry.write_text(desktop_entry(dest, ico), encoding="utf-8")
    entry.chmod(0o755)
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(apps)], capture_output=True)
    cmd = _link_command(dest)
    say(f"App menu entry: {entry}")
    if cmd:
        say(f"Command: {cmd}")


INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Aero</string>
  <key>CFBundleDisplayName</key><string>Aero</string>
  <key>CFBundleIdentifier</key><string>io.github.nermalyt.aero</string>
  <key>CFBundleExecutable</key><string>Aero</string>
  <key>CFBundleIconFile</key><string>aero</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>{version}</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
"""


def _app_version(dest: Path):
    try:
        return re.search(r'VERSION = "([^"]+)"', (dest / "app" / "aero" / "config.py").read_text()).group(1)
    except Exception:
        return "1.0"


def _icns(png: Path, out: Path):
    """aero.icns from the 1024 px brand PNG with macOS's own sips and iconutil."""
    if not (shutil.which("sips") and shutil.which("iconutil")):
        return False
    src = png.parent.parent.parent.parent / "brand" / "png" / "aero-1024.png"
    src = src if src.exists() else png
    work = Path(tempfile.mkdtemp(prefix="aero-icon-")) / "aero.iconset"
    work.mkdir()
    try:
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = size * scale
                name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                subprocess.run(["sips", "-z", str(px), str(px), str(src), "--out", str(work / name)], capture_output=True)
        return subprocess.run(["iconutil", "-c", "icns", str(work), "-o", str(out)], capture_output=True).returncode == 0
    finally:
        shutil.rmtree(work.parent, ignore_errors=True)


def make_mac_app(dest: Path):
    _write_exec(dest / "bin" / "aero", launcher_script(dest))
    app = Path.home() / "Applications" / "Aero.app"
    contents = app / "Contents"
    (contents / "Resources").mkdir(parents=True, exist_ok=True)
    (contents / "Info.plist").write_text(INFO_PLIST.replace("{version}", _app_version(dest)), encoding="utf-8")
    _write_exec(contents / "MacOS" / "Aero", "#!/bin/sh\n" f"exec {_sh_quote(dest / 'bin' / 'aero')} \"$@\"\n")
    png = dest / "app" / "aero" / "static" / "icons" / "aero-512.png"
    if not _icns(png, contents / "Resources" / "aero.icns"):
        say("Could not build the app icon (sips/iconutil missing); Aero.app uses the generic icon.")
    subprocess.run(["touch", str(app)], capture_output=True)            # makes Finder pick up the new icon
    cmd = _link_command(dest)
    say(f"App: {app} (open it from Finder, Launchpad or Spotlight)")
    if cmd:
        say(f"Command: {cmd}")


# ---------------------------------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True)
    ap.add_argument("--force-llama", action="store_true")
    ap.add_argument("--skip-llama", action="store_true")
    ap.add_argument("--build-llama", action="store_true", help="compile llama.cpp here instead of downloading a build")
    ap.add_argument("--no-vulkan", action="store_true", help="the Vulkan loader couldn't be installed: skip that build")
    ap.add_argument("--no-openmp", action="store_true", help="the OpenMP runtime couldn't be installed: compile instead")
    ap.add_argument("--no-shortcuts", action="store_true", help="leave the shortcuts and the aero command alone")
    a = ap.parse_args()
    dest = Path(a.dest).resolve()
    (dest / "data").mkdir(parents=True, exist_ok=True)
    (dest / "models").mkdir(parents=True, exist_ok=True)
    needs = dest / "data" / "setup-needs.txt"
    needs.unlink(missing_ok=True)
    if not a.skip_llama:
        try:
            avoid = (("vulkan", "VULKAN") if a.no_vulkan else ()) + (("openmp",) if a.no_openmp else ())
            install_llama(dest, a.force_llama, build=a.build_llama, avoid=avoid)
        except NeedPackages as e:
            say(str(e))
            needs.write_text(e.need + "\n")
            return 3
        except Exception as e:  # noqa: BLE001
            say(f"ERROR installing llama.cpp: {e}")
            if not (dest / "llama").exists():
                say("Download it yourself from https://github.com/ggml-org/llama.cpp/releases and unpack it into "
                    f"{dest / 'llama'} (Windows NVIDIA: win-cuda x64 + cudart; AMD or Intel: win-vulkan x64; no GPU: "
                    "win-cpu x64. Linux: ubuntu-vulkan or ubuntu-x64. macOS: macos-arm64).")
                return 2
    ico = make_icon(dest)
    if not a.no_shortcuts:
        if IS_WIN:
            make_shortcuts(dest, ico)
        elif IS_MAC:
            make_mac_app(dest)
        else:
            make_linux_shortcuts(dest, ico)
    return 0


if __name__ == "__main__":
    sys.exit(main())
