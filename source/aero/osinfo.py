"""What this computer is and where things live on it: Windows, macOS or Linux (any distro).

Everything OS-specific that more than one module needs sits here: the shell the agent's run_command uses, how to
open a file or URL with the system's default app, which Chromium-family browser can show Aero as an app window,
and which desktop-control tools can work in this session.
"""
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
NO_WINDOW = 0x08000000 if IS_WIN else 0


def _os_release():
    out = {}
    for p in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            for line in open(p, encoding="utf-8"):
                k, _, v = line.strip().partition("=")
                if k:
                    out[k] = v.strip().strip('"')
            return out
        except OSError:
            continue
    return out


def name():
    """'Windows 11', 'macOS 15.3 (Apple Silicon)', 'Fedora Linux 43 (Workstation Edition)' and so on."""
    if IS_WIN:
        rel, build = platform.release(), sys.getwindowsversion().build if hasattr(sys, "getwindowsversion") else 0
        return f"Windows {'11' if rel == '10' and build >= 22000 else rel}"
    if IS_MAC:
        ver = platform.mac_ver()[0] or platform.release()
        return f"macOS {ver} ({'Apple Silicon' if platform.machine() == 'arm64' else 'Intel'})"
    r = _os_release()
    return r.get("PRETTY_NAME") or f"Linux {platform.release()}"


def kind():
    return "windows" if IS_WIN else "macos" if IS_MAC else "linux"


def shell_name():
    if IS_WIN:
        return "PowerShell"
    return "bash" if shutil.which("bash") else "sh"


def shell_argv(command, shell=None):
    """The argv that runs one command line in the agent's shell."""
    if IS_WIN:
        if shell == "cmd":
            return ["cmd.exe", "/d", "/s", "/c", command]
        return ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command]
    if shell != "sh" and shutil.which("bash"):
        return ["bash", "-lc", command]
    return ["sh", "-lc", command]


def wayland_only():
    """True on a Wayland desktop without an X server to fall back on: screenshots and synthetic input from
    ordinary programs are blocked there."""
    return IS_LINUX and os.environ.get("XDG_SESSION_TYPE") == "wayland" and not os.environ.get("DISPLAY")


def has_display():
    if IS_WIN or IS_MAC:
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def can_drive_desktop():
    """Screenshots, mouse and keyboard through mss and pyautogui. On Linux that needs an X11 (or XWayland)
    display; macOS needs the Screen Recording and Accessibility permissions, which macOS asks for on first use."""
    if IS_WIN or IS_MAC:
        return True
    return bool(os.environ.get("DISPLAY"))


def open_with_default(target):
    """Open a file, folder or URL with the system's default handler. Returns True when a handler was started."""
    target = str(target)
    try:
        if IS_WIN:
            os.startfile(target)
            return True
        if IS_MAC:
            subprocess.Popen(["open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        for opener in ("xdg-open", "gio", "kde-open", "exo-open"):
            exe = shutil.which(opener)
            if exe:
                args = [exe, "open", target] if opener == "gio" else [exe, target]
                subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                return True
    except OSError:
        pass
    return False


def launch_app(target):
    """Open an app by name (or a file, folder or URL). Returns a short description of what was started."""
    target = str(target).strip()
    if IS_WIN:
        try:
            os.startfile(target)
        except OSError:
            subprocess.Popen(["cmd", "/c", "start", "", target], creationflags=NO_WINDOW)
        return target
    looks_like_path = "://" in target or target.startswith(("/", "~", ".")) or os.path.exists(os.path.expanduser(target))
    if looks_like_path:
        if not open_with_default(os.path.expanduser(target)):
            raise RuntimeError("No program to open files with was found (xdg-open is missing).")
        return target
    if IS_MAC:
        r = subprocess.run(["open", "-a", target], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"macOS couldn't find an app named '{target}': {r.stderr.strip()[:200]}")
        return target
    exe = shutil.which(target) or shutil.which(target.lower())
    if exe:
        subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return target
    for launcher in ("gtk-launch", "kioclient5", "kioclient"):            # by .desktop id, e.g. "org.gnome.Calculator"
        lx = shutil.which(launcher)
        if lx:
            args = [lx, target] if launcher == "gtk-launch" else [lx, "exec", target]
            r = subprocess.run(args, capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return target
    raise RuntimeError(f"No program called '{target}' was found. Give the command name (e.g. 'gnome-calculator') "
                       "or a file, folder or URL.")


# ---- Chromium-family browsers (Aero's app window and the agent's browser tool) ---------------------------------

_MAC_APPS = ["Google Chrome", "Microsoft Edge", "Chromium", "Brave Browser", "Vivaldi", "Google Chrome Canary"]
_LINUX_BINS = ["google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "microsoft-edge-stable",
               "microsoft-edge", "brave-browser", "brave", "vivaldi-stable", "vivaldi", "thorium-browser"]
_FLATPAK_IDS = ["com.google.Chrome", "org.chromium.Chromium", "com.microsoft.Edge", "com.brave.Browser",
                "io.github.ungoogled_software.ungoogled_chromium"]


_CHROMIUM_NAMES = ("chrome", "msedge", "brave", "vivaldi", "chromium", "thorium", "opera", "google-chrome",
                   "microsoft-edge", "chromium-browser", "brave-browser", "Google Chrome", "Microsoft Edge",
                   "Brave Browser", "Vivaldi", "Chromium")
_MAC_BUNDLES = {"com.google.chrome": "Google Chrome", "com.microsoft.edgemac": "Microsoft Edge",
                "com.brave.browser": "Brave Browser", "com.vivaldi.vivaldi": "Vivaldi", "org.chromium.chromium": "Chromium",
                "com.google.chrome.canary": "Google Chrome Canary"}


def _chromium(exe):
    stem = Path(exe).stem if not IS_MAC else Path(exe).name
    return any(stem.lower() == n.lower() for n in _CHROMIUM_NAMES)


def default_browser():
    """The executable of the user's default web browser when it is Chromium-based (Chrome, Edge, Brave, Vivaldi…),
    else None (Firefox, Safari: Playwright drives Chromium only). Windows: the https UserChoice ProgId and its open
    command; macOS: LaunchServices' https handler; Linux: xdg-settings."""
    try:
        if IS_WIN:
            import winreg
            prog = None
            for scheme in ("https", "http"):
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations"
                                        rf"\UrlAssociations\{scheme}\UserChoice") as k:
                        prog = winreg.QueryValueEx(k, "ProgId")[0]
                        break
                except OSError:
                    continue
            if not prog:
                return None
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog}\shell\open\command") as k:
                cmd = winreg.QueryValueEx(k, "")[0]
            exe = cmd.split('"')[1] if cmd.startswith('"') else cmd.split(" ")[0]
            return exe if os.path.exists(exe) and _chromium(exe) else None
        if IS_MAC:
            out = subprocess.run(["defaults", "read", "com.apple.LaunchServices/com.apple.launchservices.secure",
                                  "LSHandlers"], capture_output=True, text=True, timeout=10).stdout
            import re
            for block in out.split("}"):
                if re.search(r'LSHandlerURLScheme = https;', block):
                    m = re.search(r'LSHandlerRoleAll = "?([\w.-]+)"?;', block)
                    app = _MAC_BUNDLES.get((m.group(1) if m else "").lower())
                    for root in ("/Applications", str(Path.home() / "Applications")):
                        exe = os.path.join(root, f"{app}.app", "Contents", "MacOS", app) if app else ""
                        if exe and os.path.exists(exe):
                            return exe
            return None
        out = subprocess.run(["xdg-settings", "get", "default-web-browser"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
        name = out.removesuffix(".desktop")
        for cand in (name, name.split(".")[-1].lower(), name + "-stable"):
            exe = shutil.which(cand)
            if exe and not os.path.realpath(exe).startswith("/snap/") and _chromium(exe):
                return exe
    except Exception:  # noqa: BLE001
        return None
    return None


def find_browser():
    """(argv prefix, sandboxed) of a Chromium-family browser that supports --app windows, or (None, False): the
    user's default browser first (1.2.1), then the usual ones. sandboxed: a snap or flatpak build, which can't use a
    profile folder inside a hidden directory of $HOME."""
    if os.environ.get("AERO_BROWSER") and os.path.exists(os.environ["AERO_BROWSER"]):
        return [os.environ["AERO_BROWSER"]], False
    d = default_browser()
    if d:
        return [d], False
    if IS_WIN:
        cands = [shutil.which("msedge")]
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if base:
                cands.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
        for base in (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if base:
                cands.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        exe = next((c for c in cands if c and os.path.exists(c)), None)
        return ([exe], False) if exe else (None, False)
    if IS_MAC:
        for root in ("/Applications", str(Path.home() / "Applications")):
            for app in _MAC_APPS:
                exe = os.path.join(root, f"{app}.app", "Contents", "MacOS", app)
                if os.path.exists(exe):
                    return [exe], False
        return None, False
    for b in _LINUX_BINS:
        exe = shutil.which(b)
        if exe:
            real = os.path.realpath(exe)
            return [exe], real.startswith("/snap/") or "/snap/" in exe
    for d in ("/var/lib/flatpak/exports/bin", str(Path.home() / ".local/share/flatpak/exports/bin")):
        for fid in _FLATPAK_IDS:
            exe = os.path.join(d, fid)
            if os.path.exists(exe):
                return [exe], True
    return None, False


def browser_executable():
    """A plain browser executable for Playwright (not snap or flatpak wrappers), or None."""
    argv, sandboxed = find_browser()
    if argv and not sandboxed and len(argv) == 1:
        return argv[0]
    return None


def browser_process_names():
    return ("msedge", "chrome", "chromium", "brave", "vivaldi", "thorium", "google chrome", "microsoft edge")
