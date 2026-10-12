"""Mobile workspace: Flutter (flutter/flutter, BSD-3-Clause) and xtool (xtool-org/xtool, MIT) built into Aero.

Flutter: Aero installs the official SDK archive (checked against the SHA-256 Flutter publishes) into
data/toolchains/flutter, with analytics off, and drives it through its machine interfaces:
  flutter_create / flutter_pub / flutter_analyze (machine output) / flutter_test (JSON reporter) / flutter_build /
  flutter_devices (--machine) / flutter_doctor
  flutter_preview   `flutter run -d web-server --machine` kept running by Aero: the app is served on a local port and
                    shown live inside the Mobile workspace; hot reload and hot restart go over its JSON-RPC stdin
Android and iOS emulators stay their own windows (Flutter starts them); the in-app preview is the web build.

xtool: iOS apps (SwiftPM) built, signed and installed from Linux or WSL without a Mac. On Windows Aero runs it inside
WSL. The user does the Apple parts themselves: downloading Xcode.xip after accepting Apple's licence, `xtool setup`
and `xtool auth` (Aero never types Apple credentials).
"""
import json
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import Pack, Requirement, register, toolchain as tc
from .. import action_results, capabilities
from ..config import DATA, IS_WIN
from ..tools import tool

STATE = DATA / "integrations" / "mobile"
SDK = tc.ROOT / "flutter"
tc.add_bin("flutter/bin")
_preview = {"proc": None, "url": None, "app_id": None, "log": [], "path": None, "id": 0}


def flutter():
    p = SDK / "bin" / ("flutter.bat" if IS_WIN else "flutter")
    return str(p) if p.exists() else tc.which("flutter")


def dart():
    p = SDK / "bin" / ("dart.bat" if IS_WIN else "dart")
    return str(p) if p.exists() else tc.which("dart")


def install(emit=None, cancel=None):
    import httpx
    tc.require_online("download the Flutter SDK")
    osn = "windows" if IS_WIN else "macos" if sys.platform == "darwin" else "linux"
    j = httpx.get(f"https://storage.googleapis.com/flutter_infra_release/releases/releases_{osn}.json", timeout=30).json()
    h = j["current_release"]["stable"]
    cands = [r for r in j["releases"] if r["hash"] == h]
    if osn == "macos":
        arm = platform.machine().lower() in ("arm64", "aarch64")
        cands = [r for r in cands if ("arm64" in r["archive"]) == arm] or cands
    rel = cands[0]
    url = j["base_url"] + "/" + rel["archive"]
    arc = tc.ROOT / "_dl" / Path(rel["archive"]).name
    tc.download(url, arc, emit, cancel, sha256=rel.get("sha256"), label=f"Flutter {rel['version']}")
    if SDK.exists():
        shutil.rmtree(SDK, ignore_errors=True)
    if emit:
        emit({"type": "log", "text": "Unpacking the Flutter SDK…"})
    tc.extract(arc, SDK, strip=1)
    arc.unlink(missing_ok=True)
    f = flutter()
    tc.run([f, "--disable-analytics"], timeout=600, cancel=cancel)
    tc.run([f, "config", "--no-analytics", "--no-cli-animations"], timeout=600, cancel=cancel)
    r = tc.run([f, "--version", "--machine"], timeout=900, cancel=cancel, network=True)
    tc.mark("flutter", version=rel["version"])
    return f"Flutter {rel['version']} (Dart {json.loads(r['out'][r['out'].index('{'):]).get('dartSdkVersion') if '{' in r['out'] else '?'})"


def _ok():
    f = flutter()
    if not f:
        return False, "not installed"
    v = tc.installed().get("flutter", {}).get("version")
    return True, f"Flutter {v}" if v else f


def _need():
    f = flutter()
    if not f:
        raise RuntimeError("The Flutter SDK isn't installed (integration_install flutter: about 1.9 GB, asks first).")
    return f


def parse_analyze(out, root):
    """`flutter analyze --machine` / `dart analyze --format=machine`: SEVERITY|TYPE|CODE|FILE|LINE|COL|LEN|MESSAGE."""
    res = []
    for ln in (out or "").splitlines():
        parts = ln.split("|")
        if len(parts) >= 8 and parts[0] in ("INFO", "WARNING", "ERROR"):
            f = parts[3]
            try:
                f = str(Path(f).resolve().relative_to(Path(root).resolve()))
            except (ValueError, OSError):
                pass
            res.append({"tool": "flutter analyze", "file": f, "line": int(parts[4] or 0), "col": int(parts[5] or 0),
                        "severity": parts[0].lower(), "code": parts[2].lower(), "message": "|".join(parts[7:])})
    return res


def parse_test(out):
    """The dart test JSON reporter stream: (passed, failed, [failures])."""
    tests, failures, passed = {}, [], 0
    errors = {}
    for ln in (out or "").splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        t = e.get("type")
        if t == "testStart":
            tests[e["test"]["id"]] = e["test"].get("name")
        elif t == "error":
            errors.setdefault(e.get("testID"), []).append((e.get("error") or "")[:500])
        elif t == "testDone" and not e.get("hidden"):
            if e.get("result") == "success":
                passed += 1
            else:
                failures.append({"name": tests.get(e.get("testID")), "error": " | ".join(errors.get(e.get("testID"), []))})
    return passed, len(failures), failures


@tool("flutter_create", "Create a Flutter app (Android, iOS, web, Windows, macOS, Linux from one Dart codebase).",
      "dev", {"path": {"type": "string", "description": "Folder for the new app (its name becomes the package name)"},
              "platforms": {"type": "array", "items": {"type": "string", "enum": ["android", "ios", "web", "windows",
                                                                                   "macos", "linux"]}},
              "org": {"type": "string", "description": "Reverse domain, e.g. com.example"},
              "description": {"type": "string"}}, ["path"], summary=lambda a: a.get("path", ""))
def flutter_create(ctx, path, platforms=None, org="com.example", description=""):
    try:
        f = _need()
    except RuntimeError as e:
        return {"text": str(e), "error": True}
    d = ctx.path(path)
    name = re.sub(r"[^a-z0-9_]", "_", d.name.lower()).strip("_") or "app"
    cmd = [f, "create", "--project-name", name, "--org", org or "com.example",
           "--platforms", ",".join(platforms or ["android", "ios", "web", "windows"])]
    if description:
        cmd += ["--description", description[:200]]
    r = tc.run(cmd + [str(d)], timeout=900, cancel=ctx.cancel, network=True)
    ok = (d / "pubspec.yaml").exists()
    env = action_results.make("file", verified=ok, method="pubspec.yaml exists" if ok else "no pubspec.yaml",
                              changed={"path": str(d)})
    return action_results.attach({"text": f"{'Created' if ok else 'Failed to create'} {d}\n{(r['out'] + r['err'])[-1500:]}",
                                  "error": not ok}, env)


@tool("flutter_pub", "Manage a Flutter app's packages: get, add <packages>, upgrade, outdated.", "dev",
      {"path": {"type": "string"}, "action": {"type": "string", "enum": ["get", "add", "upgrade", "outdated"]},
       "packages": {"type": "array", "items": {"type": "string"}}}, ["path", "action"],
      summary=lambda a: f"{a.get('action')} {' '.join(a.get('packages') or [])}")
def flutter_pub(ctx, path, action, packages=None):
    try:
        f = _need()
    except RuntimeError as e:
        return {"text": str(e), "error": True}
    r = tc.run([f, "pub", action, *(packages or [])], cwd=ctx.path(path), timeout=900, cancel=ctx.cancel, network=True)
    return {"text": (r["out"] + r["err"])[-4000:], "error": r["rc"] != 0}


@tool("flutter_analyze", "Analyze a Flutter/Dart app: errors, warnings and lints with file and line.", "integrations",
      {"path": {"type": "string"}}, ["path"], summary=lambda a: a.get("path", ""))
def flutter_analyze(ctx, path):
    try:
        f = _need()
    except RuntimeError as e:
        return {"text": str(e), "error": True}
    root = ctx.path(path)
    r = tc.run([f, "analyze", "--no-pub", "--machine"], cwd=root, timeout=900, cancel=ctx.cancel)
    diags = parse_analyze(r["out"] + "\n" + r["err"], root)
    _save("analyze", {"path": str(root), "diagnostics": diags})
    if not diags:
        return "No issues found." if r["rc"] == 0 else (r["err"] or r["out"])[-2000:]
    return "\n".join(f"- {d['file']}:{d['line']}:{d['col']} [{d['severity']} {d['code']}] {d['message']}" for d in diags[:100])


@tool("flutter_test", "Run a Flutter app's tests (JSON reporter) and list the failures.", "dev",
      {"path": {"type": "string"}, "name": {"type": "string", "description": "Only tests whose name matches"}},
      ["path"], summary=lambda a: a.get("path", ""))
def flutter_test(ctx, path, name=""):
    try:
        f = _need()
    except RuntimeError as e:
        return {"text": str(e), "error": True}
    root = ctx.path(path)
    r = tc.run([f, "test", "--reporter", "json", *(["--name", name] if name else [])], cwd=root, timeout=1800,
               cancel=ctx.cancel)
    passed, failed, fails = parse_test(r["out"])
    _save("test", {"path": str(root), "passed": passed, "failed": failed, "failures": fails})
    lines = [f"{passed} passed, {failed} failed"] + [f"- {x['name']}: {x['error']}" for x in fails[:30]]
    if not passed and not failed:
        lines.append((r["err"] or r["out"])[-2000:])
    env = action_results.make("terminal", verified=True, method=f"flutter test exit {r['rc']}, JSON reporter parsed",
                              changed={})
    return action_results.attach({"text": "\n".join(lines), "error": r["rc"] != 0}, env)


BUILD_OUT = {"web": "build/web", "apk": "build/app/outputs/flutter-apk", "appbundle": "build/app/outputs/bundle",
             "windows": "build/windows", "linux": "build/linux", "macos": "build/macos", "ios": "build/ios"}


@tool("flutter_build", "Build a Flutter app: web, apk, appbundle, windows, linux, macos or ios (each needs its "
      "platform's toolchain: Android SDK, Visual Studio C++, Xcode).", "dev",
      {"path": {"type": "string"}, "target": {"type": "string", "enum": list(BUILD_OUT)},
       "release": {"type": "boolean"}}, ["path", "target"], summary=lambda a: f"{a.get('target')} {a.get('path', '')}")
def flutter_build(ctx, path, target, release=True):
    try:
        f = _need()
    except RuntimeError as e:
        return {"text": str(e), "error": True}
    root = ctx.path(path)
    r = tc.run([f, "build", target, "--release" if release else "--debug"], cwd=root, timeout=3600,
               cancel=ctx.cancel, network=True)
    out = root / BUILD_OUT.get(target, "build")
    files = [str(p.relative_to(root)) for p in out.rglob("*") if p.is_file()][:20] if out.exists() else []
    env = action_results.make("file", verified=bool(files) and r["rc"] == 0, method=f"{len(files)} files in {out}",
                              changed={"path": str(out)})
    return action_results.attach({"text": f"flutter build {target}: exit {r['rc']}\n" + "\n".join(files) +
                                  ("\n" + (r["err"] or r["out"])[-2500:] if r["rc"] else ""), "error": r["rc"] != 0}, env)


@tool("flutter_devices", "Devices and emulators Flutter can run on (JSON from Flutter).", "integrations", {},
      summary=lambda a: "devices")
def flutter_devices(ctx):
    try:
        f = _need()
    except RuntimeError as e:
        return {"text": str(e), "error": True}
    r = tc.run([f, "devices", "--machine"], timeout=300, cancel=ctx.cancel)
    try:
        devs = json.loads(r["out"][r["out"].index("["):])
    except ValueError:
        return (r["err"] or r["out"])[-2000:]
    return "\n".join(f"- {d.get('name')} ({d.get('id')}): {d.get('targetPlatform')}"
                     + (" emulator" if d.get("emulator") else "") for d in devs) or "No devices."


@tool("flutter_doctor", "Flutter's own check of what is installed for each platform (Android SDK, Visual Studio, "
      "Chrome, Xcode) and what's missing.", "integrations", {}, summary=lambda a: "doctor")
def flutter_doctor(ctx):
    try:
        f = _need()
    except RuntimeError as e:
        return {"text": str(e), "error": True}
    r = tc.run([f, "doctor", "-v"], timeout=600, cancel=ctx.cancel)
    _save("doctor", {"text": r["out"][-12000:]})
    return (r["out"] or r["err"])[-12000:]


# ------------------------------------------------------------------------------------------------ live preview

def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _reader(p):
    for line in iter(p.stdout.readline, ""):
        line = line.strip()
        _preview["log"] = (_preview["log"] + [line])[-400:]
        if line.startswith("[{"):
            try:
                for ev in json.loads(line):
                    e, prm = ev.get("event"), ev.get("params") or {}
                    if e == "app.start":
                        _preview["app_id"] = prm.get("appId")
                    elif e == "app.webLaunchUrl":
                        _preview["url"] = prm.get("url")
            except ValueError:
                pass


def preview_start(path, cancel=None):
    f = _need()
    preview_stop()
    port = _port()
    p = subprocess.Popen([f, "run", "-d", "web-server", "--web-port", str(port), "--web-hostname", "127.0.0.1",
                          "--machine"], cwd=str(path), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=tc.env(),
                         creationflags=tc.NO_WINDOW, bufsize=1)
    _preview.update(proc=p, url=None, app_id=None, log=[], path=str(path))
    threading.Thread(target=_reader, args=(p,), daemon=True).start()
    t0 = time.time()
    while time.time() - t0 < 600:
        if _preview["url"] or p.poll() is not None or (cancel is not None and cancel.is_set()):
            break
        time.sleep(0.5)
    if not _preview["url"]:
        tail = "\n".join(_preview["log"][-15:])
        preview_stop()
        raise RuntimeError("The preview didn't start:\n" + tail)
    return _preview["url"]


def preview_send(method, full=False):
    p = _preview.get("proc")
    if not p or p.poll() is not None or not _preview.get("app_id"):
        raise RuntimeError("No preview is running (flutter_preview action=start).")
    _preview["id"] += 1
    msg = [{"id": _preview["id"], "method": method, "params": {"appId": _preview["app_id"], "fullRestart": full,
                                                                "reason": "aero"}}]
    p.stdin.write(json.dumps(msg) + "\n")
    p.stdin.flush()


def preview_stop():
    p = _preview.get("proc")
    if p and p.poll() is None:
        try:
            if _preview.get("app_id"):
                p.stdin.write(json.dumps([{"id": 999, "method": "app.stop", "params": {"appId": _preview["app_id"]}}])
                              + "\n")
                p.stdin.flush()
                p.wait(10)
        except Exception:  # noqa: BLE001
            pass
        if p.poll() is None:
            tc.kill(p)
    _preview.update(proc=None, url=None, app_id=None)


@tool("flutter_preview", "Run a Flutter app as a live web preview inside Aero's Mobile workspace, then hot reload "
      "(keeps state), hot restart, or stop it. Use after editing Dart files to see the change.", "dev",
      {"path": {"type": "string"}, "action": {"type": "string", "enum": ["start", "reload", "restart", "stop"]}},
      ["action"], summary=lambda a: f"{a.get('action')} {a.get('path') or ''}")
def flutter_preview(ctx, action, path="."):
    try:
        if action == "start":
            url = preview_start(ctx.path(path), ctx.cancel)
            return {"text": f"Preview running at {url} (shown in the Mobile workspace).",
                    "preview": {"kind": "web", "url": url}}
        if action in ("reload", "restart"):
            preview_send("app.restart", full=action == "restart")
            time.sleep(1.5)
            return f"Hot {action} sent. Recent output:\n" + "\n".join(_preview["log"][-8:])
        preview_stop()
        return "Preview stopped."
    except RuntimeError as e:
        return {"text": str(e), "error": True}


# ------------------------------------------------------------------------------------------------ xtool (iOS)

def _wsl(cmd, timeout=600, cancel=None):
    return tc.run(["wsl", "-e", "bash", "-lc", cmd], timeout=timeout, cancel=cancel)


def xtool_cmd(args):
    return (["wsl", "-e", "bash", "-lc", "xtool " + " ".join(args)] if IS_WIN else ["xtool", *args])


def _xtool_ok():
    if sys.platform == "darwin" or sys.platform.startswith("linux"):
        p = tc.which("xtool")
        return bool(p), p or "not installed (xtool.sh)"
    if not tc.which("wsl"):
        return False, "needs WSL (Windows Subsystem for Linux)"
    v = tc.installed().get("xtool", {}).get("version")      # checking inside WSL would boot its VM just to show status
    return bool(v), f"xtool {v} in WSL" if v else "not installed inside WSL"


def install_xtool(emit=None, cancel=None):
    if not IS_WIN:
        raise RuntimeError("Install xtool from its releases (AppImage on Linux, xtool.app on macOS).")
    tc.require_online("download xtool")
    rel = tc.github_release("xtool-org/xtool")
    a = next(a for a in rel["assets"] if a["name"] == "xtool-x86_64.AppImage")
    r = _wsl(f"mkdir -p ~/.local/bin && curl -fsSL -o /tmp/xtool.AppImage '{a['url']}' && "
             f"echo '{a.get('digest') or ''}  /tmp/xtool.AppImage' | (test -z '{a.get('digest') or ''}' || sha256sum -c -) && "
             "cd /tmp && chmod +x xtool.AppImage && ./xtool.AppImage --appimage-extract >/dev/null && "
             "rm -rf ~/.local/share/xtool && mv squashfs-root ~/.local/share/xtool && "
             "ln -sf ~/.local/share/xtool/AppRun ~/.local/bin/xtool && rm xtool.AppImage", timeout=1800, cancel=cancel)
    if r["rc"] != 0:
        raise RuntimeError("Installing xtool in WSL failed: " + (r["err"] or r["out"])[-600:])
    tc.mark("xtool", version=rel["tag"], where="WSL")
    return (f"xtool {rel['tag']} in WSL. Next steps are yours: download Xcode.xip from developer.apple.com, run "
            "`xtool setup` and `xtool auth` in a WSL terminal, and attach your iPhone with usbipd.")


@tool("xtool", "iOS apps without a Mac (SwiftPM, via xtool; in WSL on Windows): new <name>, dev build [--ipa], "
      "devices, install <ipa>, launch <bundle id>, ds teams|devices|certificates. Apple sign-in (xtool setup/auth) "
      "is done by the user in a terminal, never by Aero.", "dev",
      {"args": {"type": "array", "items": {"type": "string"}}, "path": {"type": "string"}}, ["args"],
      summary=lambda a: "xtool " + " ".join(a.get("args") or []))
def xtool_tool(ctx, args, path="."):
    ok, det = _xtool_ok()
    if not ok:
        return {"text": f"xtool isn't available: {det}.", "error": True}
    if args and args[0] in ("auth", "setup"):
        return {"text": "xtool auth/setup ask for Apple credentials: the user runs them in a terminal themselves.",
                "error": True}
    a = [re.sub(r"[^\w@./:=+-]", "", x) for x in args]
    cwd = ctx.path(path)
    if IS_WIN:
        r = tc.run(["wsl", "--cd", str(cwd), "-e", "bash", "-lc", "xtool " + " ".join(a)], timeout=3600, cancel=ctx.cancel)
    else:
        r = tc.run(["xtool", *a], cwd=cwd, timeout=3600, cancel=ctx.cancel)
    return {"text": (r["out"] + r["err"])[-6000:], "error": r["rc"] != 0}


for _n, _c, _s in (("flutter_create", ["mobile.create"], "local_write"), ("flutter_pub", ["mobile.packages"], "local_write"),
                   ("flutter_analyze", ["mobile.check"], "local_read"), ("flutter_test", ["mobile.test"], "local_write"),
                   ("flutter_build", ["mobile.build"], "local_write"), ("flutter_devices", ["mobile.devices"], "local_read"),
                   ("flutter_doctor", ["mobile.check"], "local_read"), ("flutter_preview", ["mobile.run"], "local_write"),
                   ("xtool", ["mobile.ios"], "local_write")):
    capabilities.register(_n, _c, _s, "internet" if _n in ("flutter_pub", "flutter_create", "flutter_build") else "local",
                          cost="high" if _n in ("flutter_build", "flutter_test", "flutter_preview") else "medium")


def _save(kind, d):
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / f"{kind}.json").write_text(json.dumps({**d, "at": time.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")


def _load(kind):
    try:
        return json.loads((STATE / f"{kind}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _panel():
    p = _preview.get("proc")
    return {"preview": {"url": _preview.get("url"), "running": bool(p and p.poll() is None), "path": _preview.get("path"),
                        "log": _preview.get("log", [])[-40:]},
            "analyze": _load("analyze"), "test": _load("test")}


register(Pack("flutter", "Flutter", "mobile", "One Dart codebase for Android, iOS, web and desktop, with a live preview "
              "and hot reload in Aero", "https://github.com/flutter/flutter", "BSD-3-Clause",
              triggers=[r"\b(mobile|android|ios|iphone|ipad|cross[- ]platform)\b.{0,30}\b(app|application|game)\b",
                        r"\b(flutter|dart|pubspec)\b", r"\b(build|make|create|develop)\b.{0,30}\b(an? )?(app|application) "
                        r"for (android|ios|my phone)\b"],
              tools=["flutter_create", "flutter_pub", "flutter_analyze", "flutter_test", "flutter_build",
                     "flutter_preview", "flutter_devices", "flutter_doctor", "read_file", "edit_file", "write_file"],
              requirements=[Requirement("flutter", "Flutter SDK (about 1.9 GB download)", _ok, install=install,
                                        size_mb=1950),
                            Requirement("git", "Git", lambda: (bool(tc.which("git")), tc.which("git") or "not installed"),
                                        how="install Git from git-scm.com")],
              panel=_panel,
              notes="Android builds need the Android SDK (flutter doctor says what is missing); iOS builds of Flutter "
                    "apps need a Mac with Xcode. The in-app preview is the web build; emulators open their own windows."))
register(Pack("xtool", "xtool", "mobile", "iOS apps (SwiftPM) built, signed and installed without a Mac",
              "https://github.com/xtool-org/xtool", "MIT",
              triggers=[r"\b(ios|iphone|swiftui|swift)\b.{0,40}\b(without a mac|on windows|on linux|sideload)\b",
                        r"\bxtool\b"],
              tools=["xtool"],
              requirements=[Requirement("xtool", "xtool (in WSL on Windows)", _xtool_ok, install=install_xtool,
                                        size_mb=60),
                            Requirement("apple", "Xcode.xip, `xtool setup` and `xtool auth`",
                                        lambda: (False, "the user's own steps"), optional=True,
                                        how="download Xcode.xip from developer.apple.com, then run xtool setup and "
                                            "xtool auth in a WSL terminal")],
              notes="Uses Apple's SDK from Xcode.xip on non-Apple hardware: check Apple's licence terms. Password "
                    "sign-in uses private Apple APIs; an App Store Connect API key is the supported route."))
