"""Review & test workspace, part 1: alibaba/open-code-review (Apache-2.0) built into Aero.

`ocr` (the project's Go binary, downloaded from its GitHub releases with the published SHA-256) reviews git changes or
whole files: a deterministic pipeline picks files and per-language rules, an LLM agent writes line-level comments.
Aero points it at its own local model through OCR's documented OCR_LLM_* variables, with HOME set to Aero's folder so
nothing is written to the user's ~/.opencodereview. Results land in the Review workspace grouped by severity.

Without the binary (or with Local Only and no model), code_review_rules gives the local agent the same per-language
rule documents (vendored in skills_builtin/packs/open-code-review) for OCR's delegate mode: the agent reviews itself.
"""
import json
import platform
import re
import sys
import time
from pathlib import Path

import httpx

from . import Pack, Requirement, register, toolchain as tc
from .. import action_results, capabilities
from ..config import DATA, IS_WIN, LLAMA_PORT
from ..tools import tool

STATE = DATA / "integrations" / "review"
HOME = tc.ROOT / "ocr" / "home"
RULES = Path(__file__).resolve().parent.parent / "skills_builtin" / "packs" / "open-code-review" / \
    "open-code-review-delegate" / "rule_docs"
EXT_RULE = {".py": "python", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
            ".js": "ts_js_tsx_jsx", ".jsx": "ts_js_tsx_jsx", ".ts": "ts_js_tsx_jsx", ".tsx": "ts_js_tsx_jsx",
            ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".cs": "default", ".php": "php",
            ".swift": "swift", ".rb": "default", ".sol": "solidity", ".tf": "terraform", ".yaml": "yaml", ".yml": "yaml",
            ".json": "json", ".proto": "protobuf", ".graphql": "graphql", ".zig": "zig", ".nim": "nim",
            ".dart": "default", ".m": "objc", ".scala": "default", ".r": "r", ".jl": "julia", ".hs": "haskell"}


def local_llm():
    """Aero's own model server: (OpenAI-compatible base URL, running?)."""
    url = f"http://127.0.0.1:{LLAMA_PORT}"
    import socket
    with socket.socket() as s:                   # Windows takes ~2 s to refuse a closed port: probe briefly first
        s.settimeout(0.3)
        if s.connect_ex(("127.0.0.1", LLAMA_PORT)) != 0:
            return url + "/v1", False
    try:
        ok = httpx.get(url + "/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        ok = False
    return url + "/v1", ok


def ocr_bin():
    p = tc.ROOT / "ocr" / ("ocr.exe" if IS_WIN else "ocr")
    return p if p.exists() else (Path(tc.which("ocr")) if tc.which("ocr") else None)


def install(emit=None, cancel=None):
    rel = tc.github_release("alibaba/open-code-review")
    osn = "windows" if IS_WIN else "darwin" if sys.platform == "darwin" else "linux"
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "amd64"
    name = f"opencodereview-{osn}-{arch}" + (".exe" if IS_WIN else "")
    a = next((a for a in rel["assets"] if a["name"] == name), None)
    if not a:
        raise RuntimeError(f"open-code-review {rel['tag']} has no {name}.")
    dest = tc.ROOT / "ocr" / ("ocr.exe" if IS_WIN else "ocr")
    tc.download(a["url"], dest, emit, cancel, sha256=a.get("digest"), label=name)
    if not IS_WIN:
        dest.chmod(0o755)
    tc.add_bin("ocr")
    tc.mark("ocr", version=rel["tag"], sha256=a.get("digest"))
    return f"open-code-review {rel['tag']}"


def _env(model_url):
    HOME.mkdir(parents=True, exist_ok=True)
    return {"OCR_LLM_URL": model_url, "OCR_LLM_TOKEN": "aero-local", "OCR_LLM_MODEL": "local",
            "OCR_LLM_PROTOCOL": "openai", "OCR_LLM_TIMEOUT": "900", "HOME": str(HOME), "USERPROFILE": str(HOME)}


def review(path, mode="workspace", commit=None, frm=None, to=None, background="", scan_paths=None, effort="low",
           cancel=None, model_url=None):
    """Run ocr and return its JSON result (also saved for the workspace)."""
    exe = ocr_bin()
    if not exe:
        raise RuntimeError("open-code-review isn't installed (integration_install open-code-review).")
    url, ok = (model_url, True) if model_url else local_llm()
    if not ok:
        raise RuntimeError("No local model is loaded, and open-code-review uses Aero's model. Load a model first.")
    out = Path(STATE) / f"review-{int(time.time())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    if mode == "scan":
        cmd = [str(exe), "scan", "--format", "json", "--audience", "agent", "--output", str(out)]
        if scan_paths:
            cmd += ["--path", ",".join(scan_paths)]
    else:
        cmd = [str(exe), "review", "--format", "json", "--audience", "agent", "--output", str(out),
               "--effort", effort or "low", "--concurrency", "2"]
        if mode == "commit" and commit:
            cmd += ["--commit", commit]
        elif mode == "range" and frm:
            cmd += ["--from", frm] + (["--to", to] if to else [])
    if background:
        cmd += ["--background", background[:1500]]
    r = tc.run(cmd, cwd=path, timeout=3600, cancel=cancel, env_extra=_env(url))
    try:
        res = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        res = {"status": "failed", "error": (r["err"] or r["out"])[-1500:], "comments": []}
    res["path"] = str(path)
    res["exit"] = r["rc"]
    res["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (Path(STATE) / "last.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


SEV = ("critical", "high", "medium", "low")


def summarize(res, limit=60):
    cs = res.get("comments") or []
    by = {s: [c for c in cs if (c.get("severity") or "low") == s] for s in SEV}
    head = [f"open-code-review: {res.get('status')} · {len(cs)} comments"
            + (f" · {res['summary'].get('files_reviewed')} files, {res['summary'].get('elapsed')}"
               if res.get("summary") else "")]
    if res.get("message"):
        head.append(res["message"])
    if res.get("error"):
        head.append("Error: " + res["error"])
    for s in SEV:
        for c in by[s][:limit]:
            loc = f"{c.get('path')}:{c.get('start_line')}" if c.get("start_line") else c.get("path")
            head.append(f"- [{s}] {loc} ({c.get('category') or 'other'}): {(c.get('content') or '').strip()[:400]}")
            if c.get("suggestion_code"):
                head.append("  suggestion: " + c["suggestion_code"].strip()[:300])
    for w in res.get("warnings") or []:
        head.append(f"warning: {str(w)[:200]}")
    return "\n".join(head)


@tool("code_review", "Review code changes with open-code-review (rule-guided, line-level comments with severity and "
      "category), using Aero's local model. mode: workspace (uncommitted changes), commit (one commit), range "
      "(from..to, e.g. a PR branch against main), scan (whole files; scan_paths limits it). background: what the "
      "change is for (improves the review).", "dev",
      {"path": {"type": "string", "description": "The git repository"},
       "mode": {"type": "string", "enum": ["workspace", "commit", "range", "scan"]},
       "commit": {"type": "string"}, "from": {"type": "string"}, "to": {"type": "string"},
       "scan_paths": {"type": "array", "items": {"type": "string"}}, "background": {"type": "string"},
       "effort": {"type": "string", "enum": ["low", "medium", "high"]}},
      summary=lambda a: f"{a.get('path') or '.'} ({a.get('mode') or 'workspace'})")
def code_review(ctx, path=".", mode="workspace", commit=None, background="", scan_paths=None, effort="low", **kw):
    root = ctx.path(path)
    if not (root / ".git").exists() and mode != "scan":
        return {"text": f"{root} is not a git repository; use mode='scan' to review files without git.", "error": True}
    try:
        res = review(root, mode, commit, kw.get("from"), kw.get("to"), background, scan_paths, effort, ctx.cancel)
    except RuntimeError as e:
        return {"text": str(e) + " Or use code_review_rules and review the code yourself.", "error": True}
    env = action_results.make("terminal", verified=res.get("exit") == 0, method=f"ocr exit {res.get('exit')}, "
                              f"{len(res.get('comments') or [])} comments parsed", changed={})
    return action_results.attach({"text": summarize(res), "error": res.get("exit") not in (0, None)}, env)


@tool("code_review_rules", "open-code-review's review rules for the given files' languages (null-pointer, concurrency, "
      "injection, resource and API-misuse checks per language), for reviewing code yourself when the ocr binary "
      "isn't available.", "integrations",
      {"files": {"type": "array", "items": {"type": "string"}}}, ["files"],
      summary=lambda a: f"{len(a.get('files') or [])} files")
def code_review_rules(ctx, files):
    names = []
    for f in files or []:
        p = Path(f)
        key = EXT_RULE.get(p.suffix.lower()) or {"package.json": "package_json", "Cargo.toml": "cargo_toml",
                                                  "pom.xml": "pom_xml", "build.gradle": "build_gradle"}.get(p.name)
        if key and key not in names:
            names.append(key)
    names = names or ["default"]
    parts = []
    for n in names[:4]:
        p = RULES / f"{n}.md"
        if p.exists():
            parts.append(f"## Rules: {n}\n" + p.read_text(encoding="utf-8")[:12000])
    return ("\n\n".join(parts) + "\n\n(open-code-review rule documents, Apache-2.0, alibaba/open-code-review)") if parts \
        else {"text": "No rules for those files.", "error": True}


capabilities.register("code_review", ["code.review"], "local_write", cost="high", verify="ocr exit code and JSON")
capabilities.register("code_review_rules", ["code.review"], "local_read")


def _ocr_ok():
    p = ocr_bin()
    v = tc.installed().get("ocr", {}).get("version")
    return bool(p), (f"{v or 'installed'} ({p})" if p else "not installed")


def _git_ok():
    g = tc.which("git")
    if not g:
        return False, "Git is not installed (git-scm.com)"
    r = tc.run([g, "--version"], timeout=20)
    m = re.search(r"(\d+)\.(\d+)", r["out"])
    ok = bool(m) and (int(m.group(1)), int(m.group(2))) >= (2, 41)
    return ok, r["out"].strip() + ("" if ok else " (open-code-review needs 2.41 or newer)")


def _model_ok():
    url, ok = local_llm()
    return ok, "Aero's model is loaded" if ok else "load a model first (open-code-review uses it)"


def _panel():
    try:
        last = json.loads((Path(STATE) / "last.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = None
    return {"last": last}


register(Pack("open-code-review", "open-code-review", "review",
              "Rule-guided code review of git changes or files with line-level comments, on Aero's local model",
              "https://github.com/alibaba/open-code-review", "Apache-2.0",
              triggers=[r"\b(review|audit)\b.{0,40}\b(code|changes?|diff|pull request|pr|branch|commit|repo|project)\b",
                        r"\bcode review\b", r"\b(find|check for)\b.{0,30}\b(bugs?|vulnerabilit(y|ies)|security issues?|sql "
                        r"injection|race conditions?|null pointer|npe)\b"],
              tools=["code_review", "code_review_rules", "read_file", "edit_file"],
              skills=["open-code-review", "open-code-review-delegate"],
              requirements=[Requirement("ocr", "open-code-review binary (about 55 MB)", _ocr_ok, install=install,
                                        size_mb=55),
                            Requirement("git", "Git 2.41+", _git_ok, how="install Git from git-scm.com"),
                            Requirement("model", "a loaded local model", _model_ok, how="load a model in Aero",
                                        optional=True)],
              panel=_panel,
              notes="Review quality depends on the local model; OCR's published benchmarks use hosted models."))
