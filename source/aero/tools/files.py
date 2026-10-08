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
        return {"text": f"Image {p} ({_fmt_size(size)}).", "image": meta["id"]}
    lines = text.splitlines()
    s = max(1, int(start_line or 1))
    n = max(1, int(max_lines or 600))
    chunk = lines[s - 1:s - 1 + n]
    body = "\n".join(f"{i:>5}  {l}" for i, l in enumerate(chunk, s))
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


@tool("edit_file", "Replace an exact snippet of text in a file. old_text must match exactly once unless "
      "replace_all is true. Read the file first.", "files_write",
      {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
       "replace_all": {"type": "boolean"}},
      ["path", "old_text", "new_text"], summary=lambda a: a.get("path", ""))
def edit_file(ctx, path, old_text, new_text, replace_all=False):
    p = ctx.path(path)
    s = p.read_text(encoding="utf-8")
    n = s.count(old_text)
    if n == 0:
        return {"text": "old_text not found. Re-read the file and copy the snippet exactly.", "error": True}
    if n > 1 and not replace_all:
        return {"text": f"old_text matches {n} times; add surrounding context or set replace_all.", "error": True}
    s = s.replace(old_text, new_text) if replace_all else s.replace(old_text, new_text, 1)
    p.write_text(s, encoding="utf-8", newline="")
    return f"Edited {p} ({n if replace_all else 1} replacement{'s' if replace_all and n > 1 else ''})"


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
