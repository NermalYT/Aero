"""File system tools."""
import fnmatch
import os
import re
import shutil
import time

from . import tool
from .. import attachments

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "$Recycle.Bin", "System Volume Information"}


def _fmt_size(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


@tool("list_dir", "List a folder's contents (files with sizes, subfolders). Relative paths resolve against the "
      "working directory.", "files_read",
      {"path": {"type": "string", "description": "Folder path. Default: working directory."},
       "depth": {"type": "integer", "description": "Recursion depth 1-4 (default 1)."}},
      summary=lambda a: a.get("path", "."))
def list_dir(ctx, path=".", depth=1):
    root = ctx.path(path)
    if not root.exists():
        return {"text": f"Not found: {root}", "error": True}
    depth = max(1, min(int(depth or 1), 4))
    lines, count = [f"{root}"], 0

    def walk(d, lvl):
        nonlocal count
        try:
            items = sorted(os.scandir(d), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except (PermissionError, OSError) as e:
            lines.append("  " * lvl + f"[{e.__class__.__name__}]")
            return
        for e in items:
            count += 1
            if count > 1500:
                return
            try:
                if e.is_dir(follow_symlinks=False):
                    lines.append("  " * lvl + f"{e.name}/")
                    if lvl < depth and e.name not in SKIP_DIRS:
                        walk(e.path, lvl + 1)
                else:
                    st = e.stat()
                    lines.append("  " * lvl + f"{e.name}  ({_fmt_size(st.st_size)}, "
                                 f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))})")
            except OSError:
                lines.append("  " * lvl + e.name)

    walk(root, 1)
    if count > 1500:
        lines.append("... (truncated at 1500 entries)")
    return "\n".join(lines)


@tool("read_file", "Read a file. Text files return numbered lines; PDFs, Office docs, spreadsheets, archives and "
      "binaries are converted to text automatically. Images are returned as an image when the model has vision.",
      "files_read",
      {"path": {"type": "string"},
       "start_line": {"type": "integer", "description": "1-based first line (default 1)."},
       "max_lines": {"type": "integer", "description": "Lines to return (default 600)."}},
      ["path"], summary=lambda a: a.get("path", ""))
def read_file(ctx, path, start_line=1, max_lines=600):
    p = ctx.path(path)
    if not p.is_file() and not os.path.isabs(str(path)):
        # 1.3.1: models read a skill's "references/x.md" against the working folder even when told its folder
        p = next((d / path for d in getattr(ctx, "skill_dirs", None) or [] if (d / path).is_file()), p)
    if not p.is_file():
        return {"text": f"Not a file: {p}", "error": True}
    size = p.stat().st_size
    if size > 200 * 2**20:
        return {"text": f"{p} is {_fmt_size(size)}; too large to read whole. Use run_command to inspect parts.",
                "error": True}
    data = p.read_bytes()
    kind, text, note = attachments.extract(p.name, data)
    if kind == "image":
        meta = attachments.save_upload(p.name, data)
        seen = "" if ctx.vision or not ctx.settings.get("ocr_for_text_models", True) else attachments.ocr_text(meta["id"])
        if seen:                         # 1.3: a model that can't see gets the image's text (OCR)
            return {"text": f"Image {p} ({_fmt_size(size)}). The model can't see images; its text, read with OCR:\n"
                            f"{seen}", "image": meta["id"]}
        return {"text": f"Image {p} ({_fmt_size(size)}).", "image": meta["id"]}
    lines = text.splitlines()
    s = max(1, int(start_line or 1))
    n = max(1, int(max_lines or 600))
    chunk = lines[s - 1:s - 1 + n]
    body = "\n".join(f"{i:>5}  {line}" for i, line in enumerate(chunk, s))
    more = len(lines) - (s - 1 + len(chunk))
    tail = f"\n... {more} more lines (use start_line={s + len(chunk)})" if more > 0 else ""
    return f"{p} ({len(lines)} lines){(' ' + note) if note else ''}\n{body}{tail}"


@tool("write_file", "Create or overwrite a text file (parent folders are created). Set append=true to append.",
      "files_write",
      {"path": {"type": "string"}, "content": {"type": "string"},
       "append": {"type": "boolean", "description": "Append instead of overwrite."}},
      ["path", "content"], summary=lambda a: a.get("path", ""))
def write_file(ctx, path, content, append=False):
    p = ctx.path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a" if append else "w", encoding="utf-8", newline="") as f:
        f.write(content)
    return f"{'Appended' if append else 'Wrote'} {len(content):,} chars to {p}"


NUMBERED = re.compile(r"^ *\d+  ")              # read_file's "  123  " line prefix, copied by mistake


def _unnumber(text):
    lines = text.split("\n")
    full = [ln for ln in lines if ln.strip()]
    if full and all(NUMBERED.match(ln) for ln in full):
        return "\n".join(NUMBERED.sub("", ln, count=1) for ln in lines)
    return text


def _indent(line):
    return line[:len(line) - len(line.lstrip())]


def _loose(s, old):
    """Where old matches s line by line when indentation and trailing spaces are ignored: [line index, ...]."""
    a, b = s.split("\n"), old.strip("\n").split("\n")
    want = [x.strip() for x in b]
    if not any(want):
        return []
    return [i for i in range(len(a) - len(b) + 1)
            if a[i].strip() == want[0] and [x.strip() for x in a[i:i + len(b)]] == want]


def _nearest(s, old):
    """The file's line most like old_text's first line, for the error message (bounded: long files and lines)."""
    import difflib
    first = next((x.strip() for x in old.split("\n") if x.strip()), "")[:300]
    lines = s.split("\n")
    if not first or len(lines) > 20000:
        return ""
    best = difflib.get_close_matches(first, [x.strip()[:300] for x in lines], n=1, cutoff=0.5)
    if not best:
        return ""
    i = next(k for k, x in enumerate(lines) if x.strip()[:300] == best[0])
    return f" The closest line is {i + 1}: {lines[i].strip()[:200]!r}."


@tool("edit_file", "Replace an exact snippet of text in a file. old_text must match exactly once unless "
      "replace_all is true. Read the file first.", "files_write",
      {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
       "replace_all": {"type": "boolean"}},
      ["path", "old_text", "new_text"], summary=lambda a: a.get("path", ""))
def edit_file(ctx, path, old_text, new_text, replace_all=False):
    """1.3 (found in a live mod: ten "not found" in a row on a CRLF app.js):
    - line endings stay as they are: a CRLF file stays CRLF; a file with mixed endings is only edited by exact
      matches, so its other lines are untouched;
    - read_file's line numbers, when copied into old_text, are ignored (only after the text as given didn't match);
    - a snippet that differs only in indentation or trailing spaces is applied when it fits exactly one place."""
    p = ctx.path(path)
    raw = p.read_bytes().decode("utf-8")
    given, new = str(old_text), str(new_text)
    n = raw.count(given) if given else 0
    if n and (n == 1 or replace_all):                          # exactly as given: no normalising at all
        raw = raw.replace(given, new) if replace_all else raw.replace(given, new, 1)
        p.write_bytes(raw.encode("utf-8"))
        return f"Edited {p} ({n if replace_all else 1} replacement{'s' if replace_all and n > 1 else ''})"
    if n > 1:
        return {"text": f"old_text matches {n} times; add surrounding context or set replace_all.", "error": True}
    crlf = "\r\n" in raw and raw.count("\n") == raw.count("\r\n")
    if "\r\n" in raw and not crlf:                            # mixed endings: try the snippet in CRLF, nothing more
        alt = given.replace("\r\n", "\n").replace("\n", "\r\n")
        k = raw.count(alt) if alt != given else 0
        if k == 1 or (k and replace_all):
            alt_new = new.replace("\r\n", "\n").replace("\n", "\r\n")
            p.write_bytes((raw.replace(alt, alt_new) if replace_all else raw.replace(alt, alt_new, 1)).encode("utf-8"))
            return f"Edited {p} ({k if replace_all else 1} replacement{'s' if replace_all and k > 1 else ''})"
        return {"text": "old_text not found (this file mixes line endings, so only an exact copy matches). Re-read the "
                        "file and copy the snippet exactly." + _nearest(raw.replace("\r\n", "\n"), given), "error": True}
    s = raw.replace("\r\n", "\n")
    old, new = given.replace("\r\n", "\n"), new.replace("\r\n", "\n")
    n = s.count(old) if old else 0
    if n == 0 and _unnumber(old) != old and s.count(_unnumber(old)):
        old, new = _unnumber(old), _unnumber(new)
        n = s.count(old)
    how = ""
    if n == 0:
        old = _unnumber(old)
        hits = _loose(s, old)
        if len(hits) == 1:
            lines, k = s.split("\n"), len(old.strip("\n").split("\n"))
            i = hits[0]
            fi, oi = _indent(lines[i]), _indent(old.strip("\n").split("\n")[0])
            body = new[:-1] if new.endswith("\n") and old.endswith("\n") else new
            body = body[1:] if body.startswith("\n") and old.startswith("\n") else body
            new_lines = [] if body == "" else [fi + ln[len(oi):] if oi != fi and ln.startswith(oi) else ln
                                               for ln in body.split("\n")]
            s = "\n".join(lines[:i] + new_lines + lines[i + k:])
            n, how = 1, " (matched ignoring indentation and trailing spaces)"
        elif len(hits) > 1:
            return {"text": f"old_text matches {len(hits)} places when indentation is ignored; copy it exactly "
                            "with more surrounding lines.", "error": True}
        else:
            return {"text": "old_text not found. Re-read the file and copy the snippet exactly, without read_file's "
                            "line numbers." + _nearest(s, old), "error": True}
    elif n > 1 and not replace_all:
        return {"text": f"old_text matches {n} times; add surrounding context or set replace_all.", "error": True}
    else:
        s = s.replace(old, new) if replace_all else s.replace(old, new, 1)
    p.write_bytes((s.replace("\n", "\r\n") if crlf else s).encode("utf-8"))
    return f"Edited {p} ({n if replace_all else 1} replacement{'s' if replace_all and n > 1 else ''}){how}"


@tool("move_path", "Move or rename a file or folder.", "files_write",
      {"source": {"type": "string"}, "destination": {"type": "string"}}, ["source", "destination"],
      summary=lambda a: f"{a.get('source')} → {a.get('destination')}")
def move_path(ctx, source, destination):
    src, dst = ctx.path(source), ctx.path(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Moved {src} → {dst}"


@tool("delete_path", "Delete a file or folder (folders recursively). Irreversible.", "files_write",
      {"path": {"type": "string"}}, ["path"], summary=lambda a: a.get("path", ""))
def delete_path(ctx, path):
    p = ctx.path(path)
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    return f"Deleted {p}"


@tool("find_files", "Find files by name pattern (glob, e.g. '*.py' or '*report*') under a folder.", "files_read",
      {"pattern": {"type": "string"}, "path": {"type": "string"},
       "max_results": {"type": "integer"}}, ["pattern"], summary=lambda a: a.get("pattern", ""))
def find_files(ctx, pattern, path=".", max_results=200):
    root = ctx.path(path)
    out = []
    pat = pattern.lower()
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns + [d + "/" for d in dns]:
            if fnmatch.fnmatch(fn.lower().rstrip("/"), pat):
                out.append(os.path.join(dp, fn))
                if len(out) >= max_results:
                    return "\n".join(out) + "\n... (more results truncated)"
    return "\n".join(out) or "No matches."


@tool("search_files", "Search text inside files (like grep). Returns file:line: match.", "files_read",
      {"query": {"type": "string", "description": "Text or regex."},
       "path": {"type": "string"}, "glob": {"type": "string", "description": "File filter, e.g. '*.py'."},
       "regex": {"type": "boolean"}, "max_results": {"type": "integer"}},
      ["query"], summary=lambda a: a.get("query", ""))
def search_files(ctx, query, path=".", glob="*", regex=False, max_results=150):
    root = ctx.path(path)
    rx = re.compile(query if regex else re.escape(query), re.I)
    out = []
    files = [root] if root.is_file() else None
    walker = ((str(root.parent), [], [root.name]),) if files else os.walk(root)
    for dp, dns, fns in walker:
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            if not fnmatch.fnmatch(fn.lower(), (glob or "*").lower()):
                continue
            fp = os.path.join(dp, fn)
            try:
                if os.path.getsize(fp) > 10 * 2**20:
                    continue
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            out.append(f"{fp}:{i}: {line.strip()[:240]}")
                            if len(out) >= max_results:
                                return "\n".join(out) + "\n... (truncated)"
            except (OSError, UnicodeError):
                continue
    return "\n".join(out) or "No matches."
