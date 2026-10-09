"""Builds Aero's release files into dist/ from this checkout.

    python tools/build_release.py            # every file below
    python tools/build_release.py --no-rpm   # skip the .rpm (needs rpmbuild)

dist/
  Aero-windows.zip        Aero/Update-Aero.bat + Aero/source/   (Windows 10/11: extract, run Update-Aero.bat)
  Aero-macos.zip          Aero/Install-Aero.command + install.sh + source/   (double-click Install-Aero.command)
  Aero-linux.tar.gz       Aero/install.sh + source/   (any distro: sh install.sh)
  install.sh              the one-line installer: curl -fsSL .../releases/latest/download/install.sh | sh
  aero_<v>_all.deb        Debian, Ubuntu, Mint, Pop!_OS...: puts the `aero` command and an app-menu entry on the
  aero-<v>-1.noarch.rpm   Fedora, RHEL, Rocky, Alma, openSUSE...: system; each user's first start installs Aero
  PKGBUILD                Arch, Manjaro, EndeavourOS: makepkg -si          into their home folder (models are big)
  SHA256SUMS.txt          checksums of everything above (Aero's updater refuses a download that doesn't match)

Every archive holds the complete app, so any older Aero can update straight to this one.
"""
import argparse
import gzip
import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
SRC = REPO_DIR / "source"
DIST = REPO_DIR / "dist"
SKIP_DIRS = {"__pycache__", ".pytest_cache", "data", "models", "llama", "venv", "node_modules"}
SKIP_FILES = {".DS_Store", "mods-applied.json"}
CRLF_EXT = {".bat", ".cmd", ".ps1"}
EXEC_EXT = {".sh", ".command"}
MTIME = None                     # set from the last commit, so rebuilding the same commit gives the same bytes


def version():
    m = re.search(r'^VERSION = "([^"]+)"', (SRC / "aero" / "config.py").read_text(encoding="utf-8"), re.M)
    return m.group(1)


def source_files():
    """(path on disk, path inside source/) for every file that ships."""
    out = []
    for dp, dns, fns in os.walk(SRC):
        dns[:] = sorted(d for d in dns if d not in SKIP_DIRS)
        for f in sorted(fns):
            if f in SKIP_FILES or f.endswith((".pyc", ".pyo")):
                continue
            p = Path(dp, f)
            out.append((p, p.relative_to(SRC).as_posix()))
    return out


def body(path: Path):
    data = path.read_bytes()
    if path.suffix.lower() in CRLF_EXT:
        if b"\r\n" not in data and b"\n" in data:
            raise SystemExit(f"{path} must be stored with CRLF line endings (see .gitattributes)")
    return data


def mode_for(name):
    return 0o755 if Path(name).suffix in EXEC_EXT else 0o644


def write_zip(dest: Path, entries):
    """entries: [(name in the zip, bytes)]. Unix permissions are kept (macOS Archive Utility honours them)."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=time.localtime(MTIME)[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100000 | mode_for(name)) << 16
            z.writestr(info, data)


def write_targz(dest: Path, entries):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as t:
        dirs = set()
        for name, _ in entries:
            parts = name.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                dirs.add("/".join(parts[:i]))
        for d in sorted(dirs):
            ti = tarfile.TarInfo(d)
            ti.type, ti.mode, ti.mtime, ti.uname, ti.gname = tarfile.DIRTYPE, 0o755, MTIME, "root", "root"
            t.addfile(ti)
        for name, data in entries:
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode, ti.mtime, ti.uname, ti.gname = len(data), mode_for(name), MTIME, "root", "root"
            t.addfile(ti, io.BytesIO(data))
    with open(dest, "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=MTIME, compresslevel=9) as gz:
        gz.write(raw.getvalue())


def src_entries(prefix):
    return [(f"{prefix}source/{rel}", body(p)) for p, rel in source_files()]


# ------------------------------------------------------------------------------------------------ Linux packages

LAUNCHER = """#!/bin/sh
# aero: starts Aero. This package holds Aero's installer; Aero itself (its Python environment, llama.cpp build,
# models and data) lives in each user's home folder, so the first start sets it up there, and a newer package
# updates it on the next start. Aero also updates itself from GitHub releases.
SHARE=/usr/share/aero
DEST="${AERO_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/aero}"
pkg_ver=$(sed -n 's/^VERSION = "\\(.*\\)"/\\1/p' "$SHARE/source/aero/config.py" 2>/dev/null)
have_ver=$(sed -n 's/^VERSION = "\\(.*\\)"/\\1/p' "$DEST/app/aero/config.py" 2>/dev/null)
need=""
if [ ! -x "$DEST/bin/aero" ]; then need=install
elif [ -n "$pkg_ver" ] && [ "$pkg_ver" != "$have_ver" ] && \\
     [ "$(printf '%s\\n%s\\n' "$have_ver" "$pkg_ver" | sort -V | tail -n 1)" = "$pkg_ver" ]; then need=update
fi
SETUP_ONLY=""
if [ "${1:-}" = --setup-only ]; then SETUP_ONLY=1; shift; fi
if [ -n "$need" ]; then
    upd=""; [ "$need" = update ] && upd="--update"
    if [ -t 0 ]; then
        # shellcheck disable=SC2086
        sh "$SHARE/install.sh" --dir "$DEST" --no-launch $upd
        ok=$?
        if [ -n "$SETUP_ONLY" ]; then          # the terminal window opened from the app menu
            if [ $ok = 0 ]; then
                (setsid "$DEST/bin/aero" </dev/null >/dev/null 2>&1 &)
                printf '\\n  Aero is starting. You can close this window.\\n'
            fi
            printf '\\n  Press Return to close. '
            read -r _
            exit $ok
        fi
        [ $ok = 0 ] || exit 1
    else
        # started from the app menu: run the installer in a terminal window, where it can ask questions
        for t in x-terminal-emulator gnome-terminal konsole xfce4-terminal mate-terminal tilix kitty alacritty xterm; do
            command -v "$t" >/dev/null 2>&1 || continue
            case "$t" in
                gnome-terminal|tilix) "$t" -- /usr/bin/aero --setup-only ;;
                xfce4-terminal|mate-terminal) "$t" -x /usr/bin/aero --setup-only ;;
                kitty) kitty /usr/bin/aero --setup-only ;;
                *) "$t" -e /usr/bin/aero --setup-only ;;
            esac && exit 0
        done
        mkdir -p "$DEST/data/logs"                  # no terminal at all: install quietly, then start
        # shellcheck disable=SC2086
        sh "$SHARE/install.sh" --dir "$DEST" --no-launch --yes --skip-models $upd </dev/null \\
            >>"$DEST/data/logs/install.log" 2>&1 || exit 1
    fi
fi
exec "$DEST/bin/aero" "$@"
"""

DESKTOP = """[Desktop Entry]
Type=Application
Name=Aero
GenericName=Local AI assistant
Comment=Run local AI models on your own computer
Exec=aero
Icon=aero
Terminal=false
Categories=Utility;Development;
StartupWMClass=Aero
"""


def staged_tree(root: Path, v):
    """The files a .deb/.rpm installs (under root)."""
    share = root / "usr" / "share" / "aero"
    for p, rel in source_files():
        q = share / "source" / rel
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_bytes(body(p))
        q.chmod(mode_for(rel))
    shutil.copy2(REPO_DIR / "install.sh", share / "install.sh")
    (share / "install.sh").chmod(0o755)
    bindir = root / "usr" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "aero").write_text(LAUNCHER, encoding="utf-8")
    (bindir / "aero").chmod(0o755)
    apps = root / "usr" / "share" / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / "aero.desktop").write_text(DESKTOP, encoding="utf-8")
    icon = root / "usr" / "share" / "icons" / "hicolor" / "512x512" / "apps"
    icon.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SRC / "aero" / "static" / "icons" / "aero-512.png", icon / "aero.png")
    doc = root / "usr" / "share" / "doc" / "aero"
    doc.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_DIR / "LICENSE", doc / "copyright")
    for dp, dns, fns in os.walk(root):
        for d in dns:
            os.chmod(Path(dp, d), 0o755)
        for f in fns:
            os.utime(Path(dp, f), (MTIME, MTIME))


def build_deb(v, out: Path):
    if not shutil.which("dpkg-deb"):
        print("  dpkg-deb not found: skipping the .deb")
        return None
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "pkg"
        staged_tree(root, v)
        size_kb = sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) // 1024
        (root / "DEBIAN").mkdir()
        (root / "DEBIAN" / "control").write_text(
            f"Package: aero\nVersion: {v}\nSection: utils\nPriority: optional\nArchitecture: all\n"
            "Depends: python3 (>= 3.10), python3-venv, curl | wget, ca-certificates\n"
            "Recommends: python3-pip, libgomp1, libvulkan1, chromium | chromium-browser | google-chrome-stable | microsoft-edge-stable, xdg-utils\n"
            f"Installed-Size: {size_kb}\nMaintainer: Aero <https://github.com/NermalYT/Aero>\n"
            "Homepage: https://github.com/NermalYT/Aero\n"
            "Description: local AI models on your own computer\n"
            " Aero picks a model that fits your hardware, installs the right llama.cpp build for your GPU,\n"
            " tunes it and gives you a local agent. The first start installs Aero into your home folder.\n",
            encoding="utf-8")
        (root / "DEBIAN").chmod(0o755)
        dest = out / f"aero_{v}_all.deb"
        env = {**os.environ, "SOURCE_DATE_EPOCH": str(MTIME)}
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(root), str(dest)], check=True,
                       stdout=subprocess.DEVNULL, env=env)
        return dest


def build_rpm(v, out: Path):
    if not shutil.which("rpmbuild"):
        print("  rpmbuild not found: skipping the .rpm (apt install rpm, or dnf install rpm-build)")
        return None
    with tempfile.TemporaryDirectory() as td:
        top = Path(td)
        root = top / "stage"
        staged_tree(root, v)
        files = sorted("/" + p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
        spec = top / "aero.spec"
        spec.write_text(
            f"Name: aero\nVersion: {v}\nRelease: 1\nSummary: Local AI models on your own computer\nLicense: MIT\n"
            "URL: https://github.com/NermalYT/Aero\nBuildArch: noarch\n"
            "Requires: (python3 >= 3.10 or python3.12 or python3.11 or python312 or python311)\n"   # RHEL 9 / Leap 15: python3 is older
            "Requires: (curl or wget)\nRequires: ca-certificates\nRecommends: (libgomp or libgomp1)\n"
            "%global __os_install_post %{nil}\n%global __brp_mangle_shebangs %{nil}\n%define _build_id_links none\n"
            "%description\nAero picks a model that fits your hardware, installs the right llama.cpp build for your\n"
            "GPU, tunes it and gives you a local agent. The first start installs Aero into your home folder.\n"
            f"%install\nmkdir -p %{{buildroot}}\ncp -a {root}/. %{{buildroot}}/\n"
            "%files\n" + "\n".join(f'"{f}"' for f in files) + "\n", encoding="utf-8")
        subprocess.run(["rpmbuild", "-bb", "--define", f"_topdir {top}/rpm", "--define", "_binary_payload w9.xzdio",
                        "--define", f"source_date_epoch {MTIME}", str(spec)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        built = next((top / "rpm" / "RPMS" / "noarch").glob("*.rpm"))
        dest = out / f"aero-{v}-1.noarch.rpm"
        shutil.copy2(built, dest)
        return dest


def pkgbuild(v, linux_sha):
    return f"""# Maintainer: Aero <https://github.com/NermalYT/Aero>
# Arch Linux and derivatives: put this file in an empty folder and run  makepkg -si
pkgname=aero
pkgver={v}
pkgrel=1
pkgdesc="Local AI models on your own computer: picks, installs, tunes and runs them"
arch=('any')
url="https://github.com/NermalYT/Aero"
license=('MIT')
depends=('python>=3.10' 'curl' 'ca-certificates')
optdepends=('vulkan-icd-loader: GPU acceleration on AMD and Intel' 'chromium: the app window and browser tools')
source=("Aero-linux-$pkgver.tar.gz::https://github.com/NermalYT/Aero/releases/download/v$pkgver/Aero-linux.tar.gz")
sha256sums=('{linux_sha}')

package() {{
  install -d "$pkgdir/usr/share/aero"
  cp -a "$srcdir/Aero/source" "$pkgdir/usr/share/aero/source"
  install -Dm755 "$srcdir/Aero/install.sh" "$pkgdir/usr/share/aero/install.sh"
  install -Dm755 /dev/stdin "$pkgdir/usr/bin/aero" <<'LAUNCHER'
{LAUNCHER.rstrip()}
LAUNCHER
  install -Dm644 /dev/stdin "$pkgdir/usr/share/applications/aero.desktop" <<'DESKTOP'
{DESKTOP.rstrip()}
DESKTOP
  install -Dm644 "$srcdir/Aero/source/aero/static/icons/aero-512.png" "$pkgdir/usr/share/icons/hicolor/512x512/apps/aero.png"
  install -Dm644 "$srcdir/Aero/source/LICENSE" "$pkgdir/usr/share/licenses/aero/LICENSE"
}}
"""


def sha256(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    global MTIME
    ap = argparse.ArgumentParser(description="Build Aero's release files into dist/.")
    ap.add_argument("--no-rpm", action="store_true")
    ap.add_argument("--no-deb", action="store_true")
    a = ap.parse_args()
    try:
        MTIME = int(subprocess.run(["git", "-C", str(REPO_DIR), "log", "-1", "--format=%ct"], capture_output=True,
                                   text=True, check=True).stdout.strip())
    except Exception:
        MTIME = int(time.time())
    v = version()
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir()
    print(f"Aero {v} -> {DIST}")
    win = [("Aero/Update-Aero.bat", body(REPO_DIR / "Update-Aero.bat"))] + src_entries("Aero/")
    write_zip(DIST / "Aero-windows.zip", win)
    mac = [("Aero/Install-Aero.command", body(REPO_DIR / "Install-Aero.command")),
           ("Aero/install.sh", body(REPO_DIR / "install.sh"))] + src_entries("Aero/")
    write_zip(DIST / "Aero-macos.zip", mac)
    lin = [("Aero/install.sh", body(REPO_DIR / "install.sh"))] + src_entries("Aero/")
    write_targz(DIST / "Aero-linux.tar.gz", lin)
    shutil.copy2(REPO_DIR / "install.sh", DIST / "install.sh")
    if not a.no_deb:
        build_deb(v, DIST)
    if not a.no_rpm:
        build_rpm(v, DIST)
    (DIST / "PKGBUILD").write_text(pkgbuild(v, sha256(DIST / "Aero-linux.tar.gz")), encoding="utf-8")
    files = sorted(p for p in DIST.iterdir() if p.is_file())
    (DIST / "SHA256SUMS.txt").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in files), encoding="utf-8")
    for p in sorted(DIST.iterdir()):
        print(f"  {p.name:28} {p.stat().st_size / 1e6:7.2f} MB")


if __name__ == "__main__":
    main()
