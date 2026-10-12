"""Library workspace: three curated lists, searchable by the model and in the UI.

- awesome-python (vinta/awesome-python, CC BY 4.0): 500+ Python libraries by category. Snapshot bundled
  (integrations/data/awesome-python.json, with attribution); "Update" re-reads the live list.
- awesome-selfhosted (awesome-selfhosted/awesome-selfhosted-data, CC BY-SA 3.0): 1,300+ self-hostable programs with
  licences, platforms and tags (e.g. "Communication - IRC"). Not shipped with Aero (share-alike data): downloaded into
  data/library on request and kept under its own licence.
- the-book-of-secret-knowledge (trimstray, MIT): ~900 links to tools and manuals plus shell one-liners. One-liners
  are shown with risk tags (destructive, hides tracks, remote shell, needs root) and are never run by Aero itself: the
  model may only suggest them, and anything it runs still goes through run_command and its permission.
"""
import json
import math
import re
import tarfile
import time
from collections import Counter
from html import unescape
from pathlib import Path

from . import Pack, Requirement, register, toolchain
from .. import capabilities
from ..config import DATA
from ..tools import tool

HERE = Path(__file__).resolve().parent / "data"
LIB = DATA / "library"
SELFHOSTED = LIB / "awesome-selfhosted.json"
PY_URL = "https://raw.githubusercontent.com/vinta/awesome-python/master/README.md"
BOSK_URL = "https://raw.githubusercontent.com/trimstray/the-book-of-secret-knowledge/master/README.md"
SH_TAR = "https://codeload.github.com/awesome-selfhosted/awesome-selfhosted-data/tar.gz/refs/heads/master"
SOURCES = {
    "awesome-python": {"name": "awesome-python", "url": "https://github.com/vinta/awesome-python", "license": "CC BY 4.0"},
    "awesome-selfhosted": {"name": "awesome-selfhosted", "url": "https://github.com/awesome-selfhosted/awesome-selfhosted-data",
                           "license": "CC BY-SA 3.0"},
    "secret-knowledge": {"name": "the-book-of-secret-knowledge", "url": "https://github.com/trimstray/the-book-of-secret-knowledge",
                         "license": "MIT"},
}

# ------------------------------------------------------------------------------------------------ parsers

_LINK = re.compile(r"^\s*[-*]\s+\[([^\]]+)\]\((https?://[^)]+)\)\s*(?:[-–—:]\s*(.*))?$")


def parse_awesome_python(md):
    """[{name, url, desc, category, group}] from the README (### Category, optional '- Group' sub-bullets)."""
    out, cat, group, started = [], None, None, False
    for line in md.splitlines():
        if line.startswith("## ") and "Projects" in line:
            started = True
            continue
        if not started:
            continue
        if line.startswith("## "):
            break
        if line.startswith("### "):
            cat, group = line[4:].strip(), None
            continue
        m = _LINK.match(line)
        if m and cat:
            out.append({"name": m.group(1).strip(), "url": m.group(2).strip(), "desc": (m.group(3) or "").strip(),
                        "category": cat, "group": group or "", "source": "awesome-python"})
            continue
        g = re.match(r"^- ([^\[].*?)\s*$", line)
        if g and cat:
            group = g.group(1).strip()
    return out


_BOSK_LINK = re.compile(r'<a href="(https?://[^"]+)"><b>(.*?)</b></a>\s*-\s*(.*?)<br>', re.S)
RISKS = [
    ("destructive", r"\b(shred|mkfs|wipefs|dd\s+if=.*of=/dev/|rm\s+-[a-z]*r[a-z]*f?\s+/(\s|$)|>\s*/dev/sd|:\(\)\s*\{)"),
    ("hides tracks", r"\b(unset\s+HISTFILE|history\s+-c|HISTSIZE=0|kill\s+-9\s+\$\$|shred\s+.*history)"),
    ("remote shell", r"(\bnc\b.*\s-e\s|/dev/tcp/|\bsocat\b.*exec|bash\s+-i\s+>&)"),
    ("needs root", r"(^|\s)sudo\s|\biptables\b|\bmodprobe\b|\bsysctl\s+-w"),
    ("network scan", r"\b(nmap|masscan|hping3)\b"),
]
_RISKS = [(name, re.compile(p, re.I | re.M)) for name, p in RISKS]


def risks_of(code):
    return [name for name, rx in _RISKS if rx.search(code or "")]


def parse_bosk(md):
    """(links, oneliners). links: [{name, url, desc, chapter, section}]; oneliners: [{tool, title, code, risks}]."""
    links, liners = [], []
    chapter = section = tool_name = title = None
    in_code, code = False, []
    in_liners = False
    for line in md.splitlines():
        if in_code:
            if line.startswith("```"):
                in_code = False
                if tool_name and title:
                    c = "\n".join(code).strip()
                    liners.append({"tool": tool_name, "title": title, "code": c, "risks": risks_of(c),
                                   "source": "secret-knowledge"})
                code = []
            else:
                code.append(line)
            continue
        if line.startswith("```") and in_liners:
            in_code, code = True, []
            continue
        h4 = re.match(r"^####\s+(.*?)\s*&nbsp;", line)
        if h4:
            chapter = re.sub(r":\w+:\s*", "", h4.group(1)).strip()
            in_liners = chapter.lower().startswith("shell one-liners")
            continue
        h5 = re.match(r"^#####\s+(.*)$", line)
        if h5:
            t = re.sub(r":\w+:\s*", "", h5.group(1)).strip()
            m = re.match(r"Tool:\s*\[([^\]]+)\]", t)
            if m:
                tool_name = m.group(1)
            else:
                section = t
            continue
        h6 = re.match(r"^######\s+(.*)$", line)
        if h6:
            title = h6.group(1).strip()
            continue
        if not in_liners:
            for m in _BOSK_LINK.finditer(line):
                links.append({"name": unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip(), "url": m.group(1),
                              "desc": unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip(), "chapter": chapter or "",
                              "section": section or "", "source": "secret-knowledge"})
    return links, liners


def parse_selfhosted_yaml(text):
    """The awesome-selfhosted-data software/*.yml subset: scalars, lists of scalars, nested maps (skipped)."""
    d, key = {}, None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw.startswith(" "):
            m = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", raw)
            if not m:
                continue
            key, val = m.group(1), m.group(2).strip()
            d[key] = _scalar(val) if val else []
        elif key and raw.lstrip().startswith("- ") and isinstance(d.get(key), list):
            d[key].append(_scalar(raw.lstrip()[2:].strip()))
        elif key and isinstance(d.get(key), list) and not d[key]:
            d[key] = {}                                       # a nested map (commit_history, current_release)
    return d


def _scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    if v in ("true", "false"):
        return v == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def selfhosted_from_tar(path):
    out = []
    with tarfile.open(path) as t:
        for m in t.getmembers():
            if not (m.isfile() and "/software/" in m.name and m.name.endswith(".yml")):
                continue
            d = parse_selfhosted_yaml(t.extractfile(m).read().decode("utf-8", "replace"))
            if not d.get("name"):
                continue
            out.append({"name": d.get("name"), "url": d.get("website_url") or d.get("source_code_url") or "",
                        "source_code_url": d.get("source_code_url") or "", "desc": d.get("description") or "",
                        "licenses": d.get("licenses") or [], "platforms": d.get("platforms") or [],
                        "tags": d.get("tags") or [], "stars": d.get("stargazers_count"), "updated": d.get("updated_at"),
                        "archived": bool(d.get("archived")), "demo": d.get("demo_url") or "",
                        "depends_3rdparty": bool(d.get("depends_3rdparty")), "source": "awesome-selfhosted"})
    return out


# ------------------------------------------------------------------------------------------------ data

_cache = {}


def _load(name):
    if name in _cache:
        return _cache[name]
    p = {"awesome-python": LIB / "awesome-python.json", "secret-knowledge": LIB / "secret-knowledge.json",
         "awesome-selfhosted": SELFHOSTED}[name]
    if not p.exists() and name != "awesome-selfhosted":
        p = HERE / p.name                                     # the bundled snapshot
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {"items": [], "oneliners": []}
    _cache[name] = d
    return d


def items(name):
    d = _load(name)
    return d.get("items", []) + (d.get("oneliners", []) if name == "secret-knowledge" else [])


def update(names=None, emit=None, cancel=None):
    """Re-read the live lists (awesome-selfhosted: the first download). Returns {name: count}."""
    import httpx
    toolchain.require_online("download the library lists")
    LIB.mkdir(parents=True, exist_ok=True)
    out = {}
    names = names or list(SOURCES)
    if "awesome-python" in names:
        md = httpx.get(PY_URL, timeout=60, follow_redirects=True).text
        its = parse_awesome_python(md)
        _save("awesome-python", {"items": its})
        out["awesome-python"] = len(its)
    if "secret-knowledge" in names:
        md = httpx.get(BOSK_URL, timeout=60, follow_redirects=True).text
        links, liners = parse_bosk(md)
        _save("secret-knowledge", {"items": links, "oneliners": liners})
        out["secret-knowledge"] = len(links) + len(liners)
    if "awesome-selfhosted" in names:
        tar = LIB / "awesome-selfhosted-data.tar.gz"
        toolchain.download(SH_TAR, tar, emit, cancel, label="awesome-selfhosted-data")
        its = selfhosted_from_tar(tar)
        tar.unlink(missing_ok=True)
        _save("awesome-selfhosted", {"items": its})
        out["awesome-selfhosted"] = len(its)
    return out


def _save(name, d):
    d = {**d, "source": SOURCES[name], "updated": time.strftime("%Y-%m-%d")}
    p = SELFHOSTED if name == "awesome-selfhosted" else LIB / f"{name}.json"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    _cache.pop(name, None)


# ------------------------------------------------------------------------------------------------ search

_TOK = re.compile(r"[a-z0-9][a-z0-9+#.\-]*")
STOP = {"a", "an", "the", "for", "to", "of", "in", "on", "and", "or", "with", "i", "my", "me", "is", "what", "which",
        "best", "good", "library", "libraries", "tool", "tools", "python", "app", "self", "hosted", "selfhosted", "some"}


def _toks(s):
    return [t for t in _TOK.findall((s or "").lower()) if t not in STOP]


def _text(it):
    return " ".join(str(x) for x in (it.get("name"), it.get("desc"), it.get("category"), it.get("group"),
                                     it.get("chapter"), it.get("section"), it.get("tool"), it.get("title"),
                                     " ".join(it.get("tags") or []), " ".join(it.get("platforms") or [])))


def search(query, source=None, tag=None, limit=12):
    """BM25 over name, description, category and tags. source: one of SOURCES or None for all."""
    names = [source] if source in SOURCES else list(SOURCES)
    docs = [it for n in names for it in items(n)]
    if tag:
        tl = tag.lower()
        docs = [d for d in docs if any(tl in t.lower() for t in d.get("tags") or [])
                or tl in (d.get("category") or "").lower() or tl in (d.get("section") or "").lower()]
    q = _toks(query)
    if not q:
        return docs[:limit]
    toks = [_toks(_text(d)) for d in docs]
    n = len(docs)
    avg = sum(len(t) for t in toks) / max(1, n)
    df = Counter(w for t in toks for w in set(t))
    scored = []
    for d, t in zip(docs, toks):
        c = Counter(t)
        s = 0.0
        for w in q:
            tf = c.get(w, 0) + sum(v for k, v in c.items() if len(w) > 3 and k.startswith(w) and k != w) * 0.5
            if not tf:
                continue
            idf = math.log(1 + (n - df.get(w, 0) + 0.5) / (df.get(w, 0) + 0.5))
            s += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(t) / max(1, avg)))
        if q and q[0] in _toks(d.get("name")):
            s *= 1.5
        if d.get("archived"):
            s *= 0.5
        if s > 0:
            scored.append((s, d))
    scored.sort(key=lambda x: -x[0])
    return [d for _, d in scored[:limit]]


def _line(it):
    src = it.get("source")
    if it.get("code") is not None:
        risk = f" [risk: {', '.join(it['risks'])}]" if it.get("risks") else ""
        return f"- one-liner ({it['tool']}): {it['title']}{risk}\n  {it['code'][:400]}"
    extra = []
    if it.get("category"):
        extra.append(it["category"] + (f" / {it['group']}" if it.get("group") else ""))
    if it.get("tags"):
        extra.append(", ".join(it["tags"]))
    if it.get("licenses"):
        extra.append("licence " + ", ".join(it["licenses"]))
    if it.get("platforms"):
        extra.append(", ".join(it["platforms"][:4]))
    if it.get("stars"):
        extra.append(f"{it['stars']:,} stars")
    if it.get("archived"):
        extra.append("archived")
    return f"- {it['name']} — {it.get('desc') or ''} ({'; '.join(extra)}) {it.get('url') or ''} [{src}]"


@tool("library_search", "Search Aero's curated libraries: awesome-python (Python libraries by category), "
      "awesome-selfhosted (self-hostable software with licences, platforms and tags such as 'Communication - IRC') "
      "and the-book-of-secret-knowledge (CLI/GUI tools, manuals and shell one-liners with risk tags). Use it to "
      "recommend libraries or programs from vetted lists instead of guessing. One-liners are for showing the user; "
      "never run a risky one.", "integrations",
      {"query": {"type": "string"}, "source": {"type": "string", "enum": list(SOURCES)},
       "tag": {"type": "string", "description": "A tag, category or section filter, e.g. 'Communication - IRC'"},
       "limit": {"type": "integer"}}, ["query"], summary=lambda a: a.get("query", ""))
def library_search(ctx, query, source=None, tag=None, limit=12):
    res = search(query, source, tag, int(limit or 12))
    if source == "awesome-selfhosted" and not items("awesome-selfhosted"):
        return {"text": "The awesome-selfhosted catalog isn't downloaded yet (it's CC BY-SA data, fetched on request). "
                        "Call library_update with source='awesome-selfhosted', or open Library to fetch it.",
                "error": True}
    if not res:
        return f"Nothing in the libraries matches '{query}'" + (f" with tag '{tag}'" if tag else "") + "."
    lic = sorted({SOURCES[r['source']]['name'] + ' (' + SOURCES[r['source']]['license'] + ')' for r in res})
    return "\n".join(_line(r) for r in res) + "\nSources: " + "; ".join(lic)


@tool("library_update", "Download the newest versions of the library lists (awesome-selfhosted is only downloaded "
      "this way). source: one list, or omit for all.", "setup",
      {"source": {"type": "string", "enum": list(SOURCES)}}, summary=lambda a: a.get("source") or "all lists")
def library_update(ctx, source=None):
    try:
        got = update([source] if source else None, cancel=ctx.cancel)
    except toolchain.Offline as e:
        return {"text": str(e), "error": True}
    return "Updated: " + ", ".join(f"{k} ({v} entries)" for k, v in got.items())


capabilities.register("library_search", ["library.search"], "local_read")
capabilities.register("library_update", ["integration.install"], "local_write", "internet", cost="medium")


def _counts():
    out = {}
    for n in SOURCES:
        d = _load(n)
        out[n] = {"items": len(d.get("items", [])), "oneliners": len(d.get("oneliners", [])),
                  "updated": d.get("updated"), "license": SOURCES[n]["license"], "url": SOURCES[n]["url"]}
    sh = _load("awesome-selfhosted").get("items", [])
    out["awesome-selfhosted"]["tags"] = Counter(t for it in sh for t in it.get("tags") or []).most_common(120)
    out["awesome-python"]["categories"] = sorted({it["category"] for it in _load("awesome-python").get("items", [])})
    return out


def _ok(name):
    def check():
        d = _load(name)
        n = len(d.get("items", [])) + len(d.get("oneliners", []))
        return n > 0, (f"{n:,} entries" + (f", updated {d['updated']}" if d.get("updated") else "")) if n else "not downloaded yet"
    return check


def _install_selfhosted(emit, cancel):
    got = update(["awesome-selfhosted"], emit, cancel)
    return f"{got['awesome-selfhosted']} programs"


register(Pack("awesome-python", "awesome-python", "library", "Python libraries by category, curated",
              SOURCES["awesome-python"]["url"], "CC BY 4.0",
              triggers=[r"\b(python|pip|pypi)\b.{0,40}\b(librar(y|ies)|package|module|framework)s?\b",
                        r"\b(which|what|best|recommend\w*)\b.{0,40}\b(python)\b.{0,40}\b(library|package|for)\b"],
              tools=["library_search"], requirements=[Requirement("data", "awesome-python list", _ok("awesome-python"))],
              panel=_counts))
register(Pack("awesome-selfhosted", "awesome-selfhosted", "library",
              "Self-hostable software with licences, platforms and tags", SOURCES["awesome-selfhosted"]["url"],
              "CC BY-SA 3.0 (data, downloaded on request)",
              triggers=[r"\bself[- ]?host(ed|ing)?\b", r"\b(irc|matrix|xmpp)\b.{0,30}\b(server|bouncer|client|network)\b",
                        r"\b(open[- ]source|free|foss)\b.{0,30}\balternative to\b", r"\bhome ?lab\b"],
              tools=["library_search", "library_update"],
              requirements=[Requirement("data", "awesome-selfhosted catalog (about 1 MB)", _ok("awesome-selfhosted"),
                                        install=_install_selfhosted, size_mb=1)]))
register(Pack("secret-knowledge", "the-book-of-secret-knowledge", "library",
              "CLI/GUI tools, manuals, cheat sheets and shell one-liners (shown with risk tags, never auto-run)",
              SOURCES["secret-knowledge"]["url"], "MIT",
              triggers=[r"\bone[- ]?liners?\b", r"\bcheat ?sheets?\b", r"\b(cli|command[- ]line|terminal)\b.{0,30}\btools?\b",
                        r"\b(sysadmin|devops|pentest(ing)?|dns|ssl|tls)\b.{0,30}\b(tools?|commands?)\b"],
              tools=["library_search"], requirements=[Requirement("data", "the-book-of-secret-knowledge",
                                                                  _ok("secret-knowledge"))]))
