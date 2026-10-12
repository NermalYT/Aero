"""Code workspace: the language essentials built into Aero.

Toolchains (installed on request into data/toolchains, never onto the system PATH):
  Python   uv (astral-sh/uv, MIT/Apache-2.0), ruff (astral-sh/ruff, MIT), pyright (microsoft/pyright, MIT, via npm)
  JS/TS    Node.js LTS (nodejs/node, MIT), Biome (biomejs/biome, MIT/Apache-2.0), TypeScript (Apache-2.0, via npm)
  Rust     rustup + cargo + clippy + rustfmt (rust-lang, MIT/Apache-2.0)
  Go       the official Go distribution (golang/go, BSD-3-Clause)
  CSS/web  MDN browser-compat-data (mdn/browser-compat-data, CC0) for "does Safari support X?"

project_check runs the checks a project already has, each with its machine-readable output (ruff JSON, pyright
JSON, pytest JUnit XML, Biome JSON, tsc, cargo's JSON message stream, go vet/test JSON) and turns them into one
list of diagnostics {tool, file, line, col, severity, code, message} that the model and the Code workspace use.
"""
import json
import platform
import re
import shutil
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from . import Pack, Requirement, register, toolchain as tc
from .. import action_results, capabilities
from ..config import DATA, IS_WIN
from ..tools import tool

STATE = DATA / "integrations" / "code"
BCD = tc.ROOT / "bcd" / "data.json"
MACHINE = platform.machine().lower()
ARCH = "aarch64" if MACHINE in ("arm64", "aarch64") else "x86_64"
EXE = ".exe" if IS_WIN else ""

# ------------------------------------------------------------------------------------------------ toolchains


def _target():
    if IS_WIN:
        return f"{ARCH}-pc-windows-msvc"
    if sys.platform == "darwin":
        return f"{ARCH}-apple-darwin"
    return f"{ARCH}-unknown-linux-gnu"


def _gh_binary(repo, pattern, dest_name, bin_rel="", emit=None, cancel=None, tag_prefix=None):
    """Install the release asset matching pattern into toolchains/<dest_name>."""
    if tag_prefix:
        import httpx
        tc.require_online(f"look up {repo}'s releases")
        rels = httpx.get(f"https://api.github.com/repos/{repo}/releases?per_page=30", timeout=30,
                         headers={"Accept": "application/vnd.github+json"}).json()
        rel = next(r for r in rels if str(r.get("tag_name", "")).startswith(tag_prefix) and not r.get("prerelease"))
        rel = {"tag": rel["tag_name"], "assets": [{"name": a["name"], "url": a["browser_download_url"],
                                                   "size": a.get("size") or 0,
                                                   "digest": (a.get("digest") or "").replace("sha256:", "") or None}
                                                  for a in rel["assets"]]}
    else:
        rel = tc.github_release(repo)
    rx = re.compile(pattern)
    a = next((a for a in rel["assets"] if rx.search(a["name"])), None)
    if not a:
        raise RuntimeError(f"{repo} {rel['tag']} has no release file for this computer ({pattern}).")
    dest = tc.ROOT / dest_name
    arc = tc.ROOT / "_dl" / a["name"]
    tc.download(a["url"], arc, emit, cancel, sha256=a.get("digest"), label=a["name"])
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    if a["name"].endswith((".zip", ".tar.gz", ".tgz", ".tar.xz")):
        tc.extract(arc, dest)
        arc.unlink(missing_ok=True)
    else:                                                     # a bare binary
        dest.mkdir(parents=True, exist_ok=True)
        b = dest / (dest_name + EXE)
        shutil.move(str(arc), b)
        if not IS_WIN:
            b.chmod(0o755)
    tc.add_bin(f"{dest_name}/{bin_rel}" if bin_rel else dest_name)
    tc.mark(dest_name, version=rel["tag"], source=a["url"])
    return f"{dest_name} {rel['tag']}"


def _install_uv(emit, cancel):
    r = _gh_binary("astral-sh/uv", rf"^uv-{re.escape(_target())}\.(zip|tar\.gz)$", "uv", emit=emit, cancel=cancel)
    _flatten("uv")
    return r


def _install_ruff(emit, cancel):
    r = _gh_binary("astral-sh/ruff", rf"^ruff-{re.escape(_target())}\.(zip|tar\.gz)$", "ruff", emit=emit, cancel=cancel)
    _flatten("ruff")
    return r


def _flatten(name):
    """Release archives sometimes hold one folder: move its files up so the binary sits in toolchains/<name>."""
    d = tc.ROOT / name
    subs = [p for p in d.iterdir()] if d.exists() else []
    if len(subs) == 1 and subs[0].is_dir():
        for p in subs[0].iterdir():
            shutil.move(str(p), d / p.name)
        subs[0].rmdir()


def _install_biome(emit, cancel):
    osn = "win32" if IS_WIN else "darwin" if sys.platform == "darwin" else "linux"
    arch = "arm64" if ARCH == "aarch64" else "x64"
    return _gh_binary("biomejs/biome", rf"^biome-{osn}-{arch}(\.exe)?$", "biome", emit=emit, cancel=cancel,
                      tag_prefix="@biomejs/biome@")


def _install_node(emit, cancel):
    import httpx
    tc.require_online("download Node.js")
    idx = httpx.get("https://nodejs.org/dist/index.json", timeout=30).json()
    rel = next(r for r in idx if r.get("lts"))
    v = rel["version"]
    arch = "arm64" if ARCH == "aarch64" else "x64"
    name = f"node-{v}-win-{arch}.zip" if IS_WIN else f"node-{v}-{'darwin' if sys.platform == 'darwin' else 'linux'}-{arch}.tar.{'gz' if sys.platform == 'darwin' else 'xz'}"
    sums = httpx.get(f"https://nodejs.org/dist/{v}/SHASUMS256.txt", timeout=30).text
    sha = next((ln.split()[0] for ln in sums.splitlines() if ln.endswith(name)), None)
    arc = tc.ROOT / "_dl" / name
    tc.download(f"https://nodejs.org/dist/{v}/{name}", arc, emit, cancel, sha256=sha, label=name)
    dest = tc.ROOT / "node"
    shutil.rmtree(dest, ignore_errors=True)
    tc.extract(arc, dest, strip=1)
    arc.unlink(missing_ok=True)
    tc.add_bin("node" if IS_WIN else "node/bin")
    tc.mark("node", version=v)
    return f"Node.js {v} (LTS)"


def _install_go(emit, cancel):
    import httpx
    tc.require_online("download Go")
    rels = httpx.get("https://go.dev/dl/?mode=json", timeout=30).json()
    rel = next(r for r in rels if r.get("stable"))
    goos = "windows" if IS_WIN else "darwin" if sys.platform == "darwin" else "linux"
    goarch = "arm64" if ARCH == "aarch64" else "amd64"
    f = next(f for f in rel["files"] if f["os"] == goos and f["arch"] == goarch and f["kind"] == "archive")
    arc = tc.ROOT / "_dl" / f["filename"]
    tc.download("https://go.dev/dl/" + f["filename"], arc, emit, cancel, sha256=f.get("sha256"), label=f["filename"])
    dest = tc.ROOT / "go"
    shutil.rmtree(dest, ignore_errors=True)
    tc.extract(arc, dest, strip=1)
    arc.unlink(missing_ok=True)
    tc.add_bin("go/bin")
    tc.mark("go", version=rel["version"])
    return rel["version"]


def _install_rust(emit, cancel):
    url = (f"https://static.rust-lang.org/rustup/dist/{_target()}/rustup-init{EXE}")
    exe = tc.ROOT / "_dl" / f"rustup-init{EXE}"
    tc.download(url, exe, emit, cancel, label="rustup-init")
    if not IS_WIN:
        exe.chmod(0o755)
    home = tc.ROOT / "rust"
    r = tc.run([str(exe), "-y", "--no-modify-path", "--profile", "minimal", "-c", "clippy", "-c", "rustfmt"],
               timeout=3600, cancel=cancel, network=True,
               env_extra={"RUSTUP_HOME": str(home / "rustup"), "CARGO_HOME": str(home / "cargo")})
    if r["rc"] != 0:
        raise RuntimeError("rustup-init failed: " + (r["err"] or r["out"])[-600:])
    tc.add_bin("rust/cargo/bin")
    tc.mark("rust", version="stable")
    note = " Building on Windows also needs the Visual Studio C++ build tools (rustup links with them)." if IS_WIN else ""
    return "Rust stable (rustup, cargo, clippy, rustfmt)." + note


def _install_pyright(emit, cancel):
    tc.npm_install("pyright", ["pyright"], emit, cancel)
    return "pyright (npm)"


def _install_typescript(emit, cancel):
    tc.npm_install("typescript", ["typescript"], emit, cancel)
    return "TypeScript (npm)"


def _install_bcd(emit, cancel):
    tc.download("https://unpkg.com/@mdn/browser-compat-data/data.json", BCD, emit, cancel, label="browser-compat-data")
    tc.mark("bcd", version=json.loads(BCD.read_text(encoding="utf-8")).get("__meta", {}).get("version"))
    _bcd_cache.clear()
    return "MDN browser-compat-data"


# id: (label, binary, installer, size MB, needs Node)
TOOLCHAINS = {
    "uv": ("uv (Python packages and environments)", "uv", _install_uv, 20, False),
    "ruff": ("ruff (Python lint and format)", "ruff", _install_ruff, 12, False),
    "pyright": ("pyright (Python types)", "pyright", _install_pyright, 20, True),
    "node": ("Node.js LTS", "node", _install_node, 35, False),
    "biome": ("Biome (JS/TS/CSS lint and format)", "biome", _install_biome, 40, False),
    "typescript": ("TypeScript (tsc)", "tsc", _install_typescript, 25, True),
    "go": ("Go", "go", _install_go, 75, False),
    "rust": ("Rust (rustup, cargo, clippy, rustfmt)", "cargo", _install_rust, 300, False),
    "bcd": ("MDN browser-compat-data (CSS/HTML/JS support tables)", None, _install_bcd, 15, False),
}
for _rel in ("uv", "ruff", "biome", "go/bin", "rust/cargo/bin", "npm/pyright/node_modules/.bin",
             "npm/typescript/node_modules/.bin", "node" if IS_WIN else "node/bin"):
    tc.add_bin(_rel)


def have(tid):
    label, binary, _i, _s, _n = TOOLCHAINS[tid]
    if tid == "bcd":
        return BCD.exists(), "downloaded" if BCD.exists() else "not downloaded"
    p = tc.which(binary)
    return bool(p), (str(p) if p else "not installed")


def install(tid, emit=None, cancel=None):
    label, _b, inst, _s, needs_node = TOOLCHAINS[tid]
    if needs_node and not tc.which("node"):
        _install_node(emit, cancel)
    return inst(emit, cancel)


# ------------------------------------------------------------------------------------------------ projects

def detect(root):
    """What a folder holds: languages, frameworks, the commands it already defines."""
    root = Path(root)
    found, cmds = [], {}

    def has(*names):
        return any((root / n).exists() for n in names)
    if has("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg") or list(root.glob("*.py"))[:1]:
        found.append("python")
    if has("package.json"):
        try:
            pj = json.loads((root / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pj = {}
        deps = {**pj.get("dependencies", {}), **pj.get("devDependencies", {})}
        found.append("typescript" if has("tsconfig.json") or "typescript" in deps else "javascript")
        fw = [f for f in ("react", "next", "vue", "svelte", "vite", "@angular/core", "express", "electron") if f in deps]
        if fw:
            found.append("frameworks: " + ", ".join(fw))
        cmds.update({k: f"npm run {k}" for k in (pj.get("scripts") or {}) if k in ("test", "build", "lint", "dev",
                                                                                   "start", "typecheck")})
    if has("Cargo.toml"):
        found.append("rust")
    if has("go.mod"):
        found.append("go")
    if has("CMakeLists.txt"):
        found.append("c/c++ (cmake)")
    if has("pubspec.yaml"):
        found.append("dart/flutter")
    if list(root.glob("*.csproj"))[:1] or list(root.glob("*.sln"))[:1]:
        found.append(".net")
    if not found and (list(root.glob("*.html"))[:1] or list(root.glob("*.css"))[:1]):
        found.append("web (html/css)")
    return {"path": str(root), "kinds": found, "commands": cmds, "git": (root / ".git").exists()}


def _python(root):
    for c in (".venv", "venv", "env"):
        p = Path(root) / c / ("Scripts/python.exe" if IS_WIN else "bin/python")
        if p.exists():
            return str(p)
    return shutil.which("python") or shutil.which("python3") or sys.executable


def _rel(root, f):
    try:
        return str(Path(f).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return str(f)


def parse_ruff(out, root):
    try:
        items = json.loads(out or "[]")
    except ValueError:
        return []
    return [{"tool": "ruff", "file": _rel(root, d.get("filename", "")), "line": (d.get("location") or {}).get("row"),
             "col": (d.get("location") or {}).get("column"), "severity": "warning", "code": d.get("code"),
             "message": d.get("message")} for d in items]


def parse_pyright(out, root):
    try:
        j = json.loads(out)
    except ValueError:
        return []
    return [{"tool": "pyright", "file": _rel(root, d.get("file", "")), "line": d["range"]["start"]["line"] + 1,
             "col": d["range"]["start"]["character"] + 1, "severity": d.get("severity", "error"),
             "code": d.get("rule"), "message": d.get("message")} for d in j.get("generalDiagnostics", [])]


def parse_junit(path, root):
    out = []
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        return out
    for case in tree.iter("testcase"):
        for kind in ("failure", "error"):
            el = case.find(kind)
            if el is not None:
                out.append({"tool": "pytest", "file": _rel(root, case.get("file") or case.get("classname", "")),
                            "line": int(case.get("line")) + 1 if case.get("line") else None, "col": None,
                            "severity": "error", "code": f"test {kind}",
                            "message": f"{case.get('classname')}::{case.get('name')}: {(el.get('message') or '')[:300]}"})
    return out


def parse_biome(out, root):
    try:
        j = json.loads(out[out.index("{"):]) if "{" in out else {}
    except ValueError:
        return []
    res = []
    for d in j.get("diagnostics", []):
        loc = d.get("location") or {}
        path = (loc.get("path") or {}).get("file") if isinstance(loc.get("path"), dict) else loc.get("path")
        span = loc.get("start") or {}
        res.append({"tool": "biome", "file": _rel(root, path or ""), "line": span.get("line"), "col": span.get("column"),
                    "severity": d.get("severity", "warning"), "code": d.get("category"),
                    "message": d.get("description") or d.get("message")})
    return res


_TSC = re.compile(r"^(.*?)\((\d+),(\d+)\): (error|warning) (TS\d+): (.*)$")


def parse_tsc(out, root):
    res = []
    for ln in (out or "").splitlines():
        m = _TSC.match(ln.strip())
        if m:
            res.append({"tool": "tsc", "file": _rel(root, m.group(1)), "line": int(m.group(2)), "col": int(m.group(3)),
                        "severity": m.group(4), "code": m.group(5), "message": m.group(6)})
    return res


def parse_cargo(out, root):
    res = []
    for ln in (out or "").splitlines():
        try:
            j = json.loads(ln)
        except ValueError:
            continue
        if j.get("reason") != "compiler-message":
            continue
        m = j.get("message") or {}
        span = next((s for s in m.get("spans") or [] if s.get("is_primary")), None) or {}
        res.append({"tool": "cargo", "file": _rel(root, span.get("file_name", "")), "line": span.get("line_start"),
                    "col": span.get("column_start"), "severity": m.get("level", "warning"),
                    "code": (m.get("code") or {}).get("code"), "message": m.get("message")})
    return res


def parse_go_test(out, root):
    res, logs = [], {}
    for ln in (out or "").splitlines():
        try:
            j = json.loads(ln)
        except ValueError:
            continue
        key = (j.get("Package"), j.get("Test"))
        if j.get("Action") == "output":
            logs.setdefault(key, []).append(j.get("Output", ""))
        elif j.get("Action") == "fail" and j.get("Test"):
            res.append({"tool": "go test", "file": j.get("Package"), "line": None, "col": None, "severity": "error",
                        "code": "test fail", "message": f"{j['Test']}: " + "".join(logs.get(key, []))[-400:].strip()})
    return res


def check(root, steps=("lint", "types", "test"), cancel=None):
    """Run each available check. Returns {kinds, ran: [{step, tool, rc, ms, note}], diagnostics}."""
    root = Path(root)
    info = detect(root)
    kinds = " ".join(info["kinds"])
    ran, diags = [], []

    def go(step, tool_name, cmd, parse, cwd=root, **kw):
        r = tc.run(cmd, cwd=cwd, timeout=kw.pop("timeout", 900), cancel=cancel, **kw)
        found = parse(r) if r["rc"] != 127 else []
        ran.append({"step": step, "tool": tool_name, "rc": r["rc"], "ms": r["ms"], "found": len(found),
                    "note": (r["err"] or "")[-300:] if r["rc"] not in (0, 1) and not found else ""})
        diags.extend(found)
    if "python" in kinds:
        if "lint" in steps and tc.which("ruff"):
            go("lint", "ruff", ["ruff", "check", "--output-format", "json", "."], lambda r: parse_ruff(r["out"], root))
        if "types" in steps and tc.which("pyright"):
            go("types", "pyright", ["pyright", "--outputjson", "--pythonpath", _python(root)],
               lambda r: parse_pyright(r["out"], root))
        if "test" in steps and (list(root.glob("tests"))[:1] or list(root.glob("test_*.py"))[:1]):
            xml = Path(tempfile.mkdtemp(prefix="aero-pytest-")) / "junit.xml"
            go("test", "pytest", [_python(root), "-m", "pytest", "-q", f"--junitxml={xml}"],
               lambda r: parse_junit(xml, root))
    if "javascript" in kinds or "typescript" in kinds:
        if "lint" in steps and tc.which("biome"):
            go("lint", "biome", ["biome", "lint", "--reporter=json", "."], lambda r: parse_biome(r["out"], root))
        local_tsc = root / "node_modules" / ".bin" / ("tsc.cmd" if IS_WIN else "tsc")
        if "types" in steps and (root / "tsconfig.json").exists() and (local_tsc.exists() or tc.which("tsc")):
            go("types", "tsc", [str(local_tsc) if local_tsc.exists() else "tsc", "--noEmit", "--pretty", "false"],
               lambda r: parse_tsc(r["out"], root))
        if "test" in steps and "test" in info["commands"]:
            go("test", "npm test", ["npm", "test", "--silent"], lambda r: [] if r["rc"] == 0 else [
                {"tool": "npm test", "file": "", "line": None, "col": None, "severity": "error", "code": "tests failed",
                 "message": (r["out"] + r["err"])[-600:]}])
    if "rust" in kinds and tc.which("cargo"):
        if "lint" in steps or "types" in steps:
            go("lint", "cargo clippy", ["cargo", "clippy", "--message-format=json", "--quiet"],
               lambda r: parse_cargo(r["out"], root), timeout=1800)
        if "test" in steps:
            go("test", "cargo test", ["cargo", "test", "--no-fail-fast", "--quiet"], lambda r: [] if r["rc"] == 0 else [
                {"tool": "cargo test", "file": "", "line": None, "col": None, "severity": "error", "code": "tests failed",
                 "message": (r["out"] + r["err"])[-600:]}], timeout=1800)
    if "go" in kinds and tc.which("go"):
        if "lint" in steps:
            go("lint", "go vet", ["go", "vet", "./..."], lambda r: [
                {"tool": "go vet", "file": ln.split(":")[0], "line": None, "col": None, "severity": "warning",
                 "code": "vet", "message": ln} for ln in (r["err"] or "").splitlines() if ln and not ln.startswith("#")])
        if "test" in steps:
            go("test", "go test", ["go", "test", "-json", "./..."], lambda r: parse_go_test(r["out"], root))
    res = {"path": str(root), "kinds": info["kinds"], "ran": ran, "diagnostics": diags[:500],
           "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "last_check.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


# ------------------------------------------------------------------------------------------------ web compat

_bcd_cache = {}


def _bcd():
    if "d" not in _bcd_cache:
        _bcd_cache["d"] = json.loads(BCD.read_text(encoding="utf-8"))
    return _bcd_cache["d"]


def compat(query, browsers=("chrome", "edge", "firefox", "safari", "safari_ios", "chrome_android")):
    """Support data for a feature: a dotted BCD key ('css.properties.container-type') or words ('has selector')."""
    d = _bcd()
    q = query.strip()
    node, key = d, None
    if "." in q and " " not in q:
        node = d
        for part in q.split("."):
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                break
        key = q if node else None
    if not key:
        words = [w for w in re.split(r"[\s:()]+", q.lower()) if w]
        hits = []

        def walk(n, path, depth):
            if depth > 5 or not isinstance(n, dict):
                return
            for k, v in n.items():
                if k == "__compat" or k.startswith("__") or not isinstance(v, dict):
                    continue
                p = path + [k]
                s = ".".join(p).lower()
                if all(w in s for w in words):
                    hits.append(".".join(p))
                walk(v, p, depth + 1)
        for top in ("css", "html", "javascript", "api", "http", "svg"):
            walk(d.get(top, {}), [top], 0)
        hits.sort(key=len)
        if not hits:
            return None, []
        key = hits[0]
        node = d
        for part in key.split("."):
            node = node[part]
        alts = hits[1:8]
    else:
        alts = []
    c = (node or {}).get("__compat") or {}
    sup = c.get("support") or {}
    rows = []
    for b in browsers:
        s = sup.get(b)
        s = s[0] if isinstance(s, list) else s
        if not s:
            rows.append((b, "unknown"))
            continue
        v = s.get("version_added")
        note = " (behind a flag)" if s.get("flags") else " (prefixed)" if s.get("prefix") else ""
        rows.append((b, ("no" if v is False else f"since {v}" if v else "unknown") + note))
    status = c.get("status") or {}
    return {"key": key, "mdn": c.get("mdn_url"), "spec": c.get("spec_url"), "rows": rows,
            "experimental": status.get("experimental"), "deprecated": status.get("deprecated")}, alts


# ------------------------------------------------------------------------------------------------ tools

@tool("project_info", "What a project folder holds (languages, frameworks, the commands it defines) and which "
      "toolchains for it are installed in Aero.", "integrations", {"path": {"type": "string"}},
      summary=lambda a: a.get("path") or ".")
def project_info(ctx, path="."):
    root = ctx.path(path)
    if not root.is_dir():
        return {"text": f"{root} is not a folder.", "error": True}
    info = detect(root)
    tcs = []
    for tid in TOOLCHAINS:
        ok, det = have(tid)
        tcs.append(f"{tid}: {'yes' if ok else 'no'}")
    return (f"{root}\nKinds: {', '.join(info['kinds']) or 'nothing recognised'}\nCommands: "
            f"{', '.join(f'{k} = {v}' for k, v in info['commands'].items()) or 'none'}\nGit: {info['git']}\n"
            f"Toolchains in Aero: {', '.join(tcs)}")


@tool("project_check", "Run the project's checks with their machine-readable outputs (lint: ruff / Biome / clippy / "
      "go vet; types: pyright / tsc; test: pytest / npm test / cargo test / go test) and get one list of diagnostics "
      "with file, line and message. Use it before and after fixing code.", "dev",
      {"path": {"type": "string"}, "steps": {"type": "array", "items": {"type": "string",
                                                                          "enum": ["lint", "types", "test"]}}},
      summary=lambda a: f"{a.get('path') or '.'} ({', '.join(a.get('steps') or ['lint', 'types', 'test'])})")
def project_check(ctx, path=".", steps=None):
    root = ctx.path(path)
    if not root.is_dir():
        return {"text": f"{root} is not a folder.", "error": True}
    r = check(root, tuple(steps or ("lint", "types", "test")), cancel=ctx.cancel)
    if not r["ran"]:
        missing = [t for t in ("ruff", "pyright", "biome", "typescript", "rust", "go") if not have(t)[0]]
        return {"text": f"No checks could run for {', '.join(r['kinds']) or 'this folder'}. Toolchains not installed: "
                        f"{', '.join(missing)} (toolchain_install asks the user, then installs into Aero's folder).",
                "error": True}
    lines = [f"{x['step']}: {x['tool']} exit {x['rc']}, {x['found']} findings, {x['ms'] / 1000:.1f}s"
             + (f" ({x['note'].strip()})" if x["note"] else "") for x in r["ran"]]
    for d in r["diagnostics"][:80]:
        lines.append(f"- {d['file']}:{d['line'] or '?'}:{d['col'] or '?'} [{d['tool']} {d['code'] or ''}] "
                     f"{d['severity']}: {d['message']}")
    if len(r["diagnostics"]) > 80:
        lines.append(f"… {len(r['diagnostics']) - 80} more in the Code workspace")
    env = action_results.make("terminal", verified=True, method="exit codes and parsed tool output",
                              changed={"checks": len(r["ran"])})
    return action_results.attach({"text": "\n".join(lines)}, env)


@tool("toolchain_install", "Install a language toolchain into Aero's own folder (not the system PATH): uv, ruff, "
      "pyright, node, biome, typescript, go, rust, bcd (MDN browser support data). Asks the user first.", "setup",
      {"name": {"type": "string", "enum": list(TOOLCHAINS)}}, ["name"], summary=lambda a: a.get("name", ""))
def toolchain_install(ctx, name):
    if name not in TOOLCHAINS:
        return {"text": f"Unknown toolchain '{name}'. Choose one of {', '.join(TOOLCHAINS)}.", "error": True}
    ok, det = have(name)
    if ok:
        return f"{name} is already installed ({det})."
    try:
        msg = install(name, cancel=ctx.cancel)
    except tc.Offline as e:
        return {"text": str(e), "error": True}
    except Exception as e:  # noqa: BLE001
        return {"text": f"Installing {name} failed: {e}", "error": True}
    ok, det = have(name)
    env = action_results.make("file", verified=ok, method=f"found {det}" if ok else "not found after installing",
                              changed={"toolchain": name})
    return action_results.attach({"text": f"Installed {msg}. {det}", "error": not ok}, env)


@tool("web_compat", "Browser support for an HTML, CSS or JavaScript feature from MDN's browser-compat-data: a dotted "
      "key like 'css.properties.container-type' or words like 'css has selector'.", "integrations",
      {"feature": {"type": "string"}}, ["feature"], summary=lambda a: a.get("feature", ""))
def web_compat(ctx, feature):
    if not BCD.exists():
        return {"text": "MDN browser-compat-data isn't downloaded yet (about 15 MB, CC0). toolchain_install with "
                        "name='bcd' fetches it.", "error": True}
    r, alts = compat(feature)
    if not r:
        return {"text": f"No feature matches '{feature}' in browser-compat-data.", "error": True}
    flags = [x for x, on in (("experimental", r["experimental"]), ("deprecated", r["deprecated"])) if on]
    return (f"{r['key']}" + (f" ({', '.join(flags)})" if flags else "") + "\n" +
            "\n".join(f"- {b}: {s}" for b, s in r["rows"]) + (f"\nMDN: {r['mdn']}" if r["mdn"] else "") +
            (f"\nOther matches: {', '.join(alts)}" if alts else "") + "\nData: MDN browser-compat-data (CC0)")


for _n, _caps, _side in (("project_info", ["code.inspect"], "local_read"), ("project_check", ["code.check", "terminal.execute"], "local_write"),
                         ("toolchain_install", ["integration.install"], "local_write"), ("web_compat", ["code.web_compat"], "local_read")):
    capabilities.register(_n, _caps, _side, "internet" if _n == "toolchain_install" else "local",
                          cost="high" if _n in ("project_check", "toolchain_install") else "low")


def _req(tid):
    label, _b, _i, size, _n = TOOLCHAINS[tid]
    return Requirement(tid, label, lambda: have(tid), install=lambda emit, cancel: install(tid, emit, cancel),
                       size_mb=size, optional=True)


def _panel():
    try:
        last = json.loads((STATE / "last_check.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = None
    return {"toolchains": [{"id": t, "label": TOOLCHAINS[t][0], "ok": have(t)[0], "detail": have(t)[1]}
                           for t in TOOLCHAINS], "last_check": last, "installed": tc.installed()}


register(Pack("essentials", "Language essentials", "code",
              "Python, JS/TS, Rust, Go and CSS toolchains with one diagnostics view",
              "https://github.com/astral-sh/uv · astral-sh/ruff · biomejs/biome · nodejs/node · golang/go · rust-lang/rustup "
              "· microsoft/pyright · microsoft/TypeScript · mdn/browser-compat-data",
              "MIT / Apache-2.0 / BSD-3-Clause / CC0 (each tool keeps its own)",
              triggers=[r"\b(lint|linter|type ?check|typecheck|failing tests?|fix (the )?tests|unit tests?|build (fails|"
                        r"error)|compile error)\b", r"\b(python|typescript|javascript|node|rust|cargo|golang|go module|"
                        r"css|tailwind|react|vite)\b.{0,40}\b(project|app|code|repo|error|bug)\b",
                        r"\b(can i use|browser support|supported in (safari|chrome|firefox))\b"],
              tools=["project_info", "project_check", "toolchain_install", "web_compat", "run_command", "read_file",
                     "edit_file"],
              skills=["code-project-tests"], requirements=[_req(t) for t in TOOLCHAINS], panel=_panel))
