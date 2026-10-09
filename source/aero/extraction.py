"""Whole-page reading with provenance: a web page becomes metadata, numbered sections under their headings, tables
and links, so the model can read all of it (not just the first screen) in pieces that fit its context.

Pipeline (the same for Aero's browser and for fetch_url):
  A. metadata     URL, title, canonical URL, language, description, structured-data types, headings, counts of
                  links, forms and tables, when it was read and how
  B. blocks       headings, paragraphs, list items, code, quotes and tables from the DOM (browser: BLOCKS_JS runs in
                  the page and walks all of it, below the fold included; fetch_url: Python's html.parser), each
                  tagged with its landmark (main, nav, header, footer, aside)
  C. dedupe       navigation, headers, footers and sidebars are dropped when the page has main content; a block
                  repeated on the page (or already seen on an earlier page of the same crawl) is kept once
  D. sections     blocks grouped under their heading path ("Pricing > Team plan"), long sections cut at block
                  boundaries, each with an id (s4, s4.2) the model can ask for again to get the exact text
  E. budget       select() returns the sections that fit a character budget (by relevance to a query, or in page
                  order) and says exactly what was left out; nothing is silently cut

Frames: content inside frames the browser lets Aero read is included with the frame's URL; frames it cannot read are
listed as unavailable, never guessed.
"""
import hashlib
import json
import re
import time
from html.parser import HTMLParser
from math import log

SECTION_MAX = 2600          # characters per section piece
DEFAULT_BUDGET = 12000      # characters of section text returned per call by default
_WS = re.compile(r"\s+")
BOILER = {"nav", "header", "footer", "aside"}


def clean(s):
    return _WS.sub(" ", str(s or "")).strip()


# ------------------------------------------------------------------------------------------------ browser side

# Runs in the page: returns {meta, blocks, truncated}. Visible-in-layout content anywhere on the page counts (scrolled
# out of view is fine); content hidden with display:none / visibility:hidden is left out.
BLOCKS_JS = r"""
(limit) => {
  const LM = 'nav,header,footer,aside,[role=navigation],[role=banner],[role=contentinfo],[role=complementary]';
  const landmark = el => { const m = el.closest(LM); if (!m) return el.closest('main,[role=main],article') ? 'main' : '';
    const r = (m.getAttribute('role') || '').toLowerCase();
    return {navigation: 'nav', banner: 'header', contentinfo: 'footer', complementary: 'aside'}[r] || m.tagName.toLowerCase(); };
  const shown = el => { try { return el.checkVisibility ? el.checkVisibility({checkVisibilityCSS: true}) : !!(el.offsetParent || el.getClientRects().length); } catch (e) { return true; } };
  const txt = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const links = el => Array.from(el.querySelectorAll('a[href]')).slice(0, 20).map(a => [txt(a).slice(0, 80), a.href]).filter(x => x[1] && !x[1].startsWith('javascript:'));
  const out = []; let chars = 0, truncated = false;
  const push = b => { if (!b.text && !(b.rows && b.rows.length)) return; if (out.length >= limit.blocks || chars > limit.chars) { truncated = true; return; } chars += (b.text || '').length; out.push(b); };
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','CANVAS','IFRAME','OBJECT','BUTTON','SELECT','OPTION','INPUT','TEXTAREA']);
  const BLOCK = /^(H[1-6]|P|LI|PRE|BLOCKQUOTE|TABLE|DT|DD|FIGCAPTION|SUMMARY|CAPTION)$/;
  const walk = el => {
    for (const c of el.children) {
      if (SKIP.has(c.tagName) || !shown(c)) continue;
      const t = c.tagName;
      if (/^H[1-6]$/.test(t)) { push({type: 'heading', level: +t[1], text: txt(c).slice(0, 300), lm: landmark(c), id: c.id || ''}); continue; }
      if (t === 'TABLE') {
        const rows = Array.from(c.rows || []).slice(0, 400).map(r => Array.from(r.cells).slice(0, 30).map(td => txt(td).slice(0, 300)));
        const cap = c.caption ? txt(c.caption) : '';
        push({type: 'table', text: cap, rows, head: !!c.tHead || (c.rows[0] && c.rows[0].querySelector('th') ? true : false), links: links(c), lm: landmark(c)}); continue;
      }
      if (t === 'PRE') { push({type: 'pre', text: (c.innerText || '').slice(0, 20000), lm: landmark(c)}); continue; }
      if (t === 'UL' || t === 'OL' || t === 'DL') { walk(c); continue; }
      if (BLOCK.test(t)) {
        const nested = c.querySelector('p,li,h1,h2,h3,h4,h5,h6,table,pre,ul,ol');
        if (nested && t !== 'P') { walk(c); continue; }
        push({type: t === 'LI' ? 'li' : t === 'BLOCKQUOTE' ? 'quote' : 'p', text: txt(c).slice(0, 8000), links: links(c), lm: landmark(c)}); continue;
      }
      // generic containers: text that sits directly in them becomes a paragraph, then go deeper
      const own = Array.from(c.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').replace(/\s+/g, ' ').trim();
      if (own.length > 30) push({type: 'p', text: own.slice(0, 8000), links: [], lm: landmark(c)});
      walk(c);
    }
  };
  if (document.body) walk(document.body);
  const q = s => document.querySelector(s);
  const ld = Array.from(document.querySelectorAll('script[type="application/ld+json"]')).map(s => { try { const j = JSON.parse(s.textContent); return [].concat(j).map(x => x && x['@type']).flat(); } catch (e) { return []; } }).flat().filter(Boolean).slice(0, 10);
  const meta = { url: location.href, title: document.title, lang: document.documentElement.lang || '',
    canonical: q('link[rel=canonical]') ? q('link[rel=canonical]').href : '',
    description: q('meta[name=description]') ? q('meta[name=description]').content : '',
    links: document.links.length, forms: Array.from(document.forms).slice(0, 10).map(f => ({ action: f.action, fields: Array.from(f.elements).filter(e => e.name && e.type !== 'hidden' && e.type !== 'password').slice(0, 20).map(e => e.name) })),
    tables: document.querySelectorAll('table').length, ld_types: ld,
    frames: Array.from(document.querySelectorAll('iframe,frame')).slice(0, 20).map(f => f.src || '(no src)'),
    height: document.documentElement.scrollHeight, viewport: innerHeight };
  return { meta, blocks: out, truncated };
}
"""


# ------------------------------------------------------------------------------------------------ html side

class _HtmlBlocks(HTMLParser):
    """Same blocks as BLOCKS_JS, from raw HTML (no JavaScript runs, so script-built content is missing)."""
    SKIP = {"script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "button", "select",
            "option", "textarea"}
    BLOCKS = {"p", "li", "pre", "blockquote", "dt", "dd", "figcaption", "summary", "caption"}
    LANDMARK_ROLE = {"navigation": "nav", "banner": "header", "contentinfo": "footer", "complementary": "aside",
                     "main": "main"}

    def __init__(self, base_url=""):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.blocks, self.meta = [], {"title": "", "canonical": "", "description": "", "lang": "", "ld_types": [],
                                      "links": 0, "forms": [], "tables": 0, "frames": []}
        self.skip = 0
        self.stack = []             # open tags (name, landmark)
        self.cur = None             # block being collected
        self.table = None           # {"rows": [...], "row": [...], "cell": str | None}
        self.links = []
        self.a_href = None
        self.a_text = []
        self.in_title = False
        self.ld = False
        self.ld_text = []

    def _landmark(self):
        for name, lm in reversed(self.stack):
            if lm:
                return lm
        return ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("meta",):
            n = (a.get("name") or a.get("property") or "").lower()
            if n in ("description", "og:description") and not self.meta["description"]:
                self.meta["description"] = clean(a.get("content"))[:400]
            return
        if tag == "link" and "canonical" in (a.get("rel") or "").lower():
            self.meta["canonical"] = self._abs(a.get("href"))
            return
        if tag == "html":
            self.meta["lang"] = a.get("lang") or ""
        if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self.ld, self.ld_text = True, []
        if tag in ("iframe", "frame"):
            self.meta["frames"].append(self._abs(a.get("src")) or "(no src)")
        if tag in self.SKIP:
            self.skip += 1
            return
        if tag in ("br", "img", "hr", "input", "meta", "link", "source", "wbr"):
            if tag == "img" and self.cur is not None and a.get("alt"):
                self.cur["text"] += " " + a["alt"]
            return
        role = (a.get("role") or "").lower()
        lm = tag if tag in BOILER or tag == "main" else self.LANDMARK_ROLE.get(role, "")
        if tag == "article" and not lm:
            lm = "main"
        self.stack.append((tag, lm))
        if self.skip:
            return
        if tag == "title":
            self.in_title = True
        elif tag == "form":
            self.meta["forms"].append({"action": self._abs(a.get("action")), "fields": []})
        elif tag in ("input", "select", "textarea") and self.meta["forms"] and a.get("name") and \
                a.get("type") not in ("hidden", "password"):
            self.meta["forms"][-1]["fields"].append(a["name"])
        elif tag == "a" and a.get("href"):
            self.meta["links"] += 1
            self.a_href, self.a_text = self._abs(a["href"]), []
        elif re.fullmatch(r"h[1-6]", tag):
            self._close_block()
            self.cur = {"type": "heading", "level": int(tag[1]), "text": "", "lm": self._landmark(), "id": a.get("id") or ""}
        elif tag == "table":
            self._close_block()
            self.meta["tables"] += 1
            self.table = {"rows": [], "row": None, "cell": None, "lm": self._landmark(), "caption": "", "head": False}
        elif self.table is not None and tag == "tr":
            self.table["row"] = []
        elif self.table is not None and tag in ("td", "th"):
            self.table["cell"] = ""
            if tag == "th":
                self.table["head"] = True
        elif tag in self.BLOCKS and self.table is None:
            if self.cur is not None and self.cur["type"] in ("li", "p") and tag in ("p",):
                return                                        # <li><p>..</p></li>: one block
            self._close_block()
            t = {"li": "li", "pre": "pre", "blockquote": "quote"}.get(tag, "p")
            self.cur = {"type": t, "text": "", "lm": self._landmark(), "links": []}
        elif tag in ("div", "section", "span", "td") and self.cur is None and self.table is None:
            pass

    def handle_endtag(self, tag):
        if tag == "script" and self.ld:
            self.ld = False
            try:
                j = json.loads("".join(self.ld_text))
                for x in (j if isinstance(j, list) else [j]):
                    t = x.get("@type") if isinstance(x, dict) else None
                    if t:
                        self.meta["ld_types"] += t if isinstance(t, list) else [t]
            except ValueError:
                pass
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        while self.stack and self.stack[-1][0] != tag and tag in {n for n, _ in self.stack}:
            self.stack.pop()
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
        if self.skip:
            return
        if tag == "title":
            self.in_title = False
        elif tag == "a" and self.a_href is not None:
            text = clean("".join(self.a_text))
            if self.cur is not None and "links" in self.cur and len(self.cur["links"]) < 20:
                self.cur["links"].append([text[:80], self.a_href])
            self.a_href = None
        elif re.fullmatch(r"h[1-6]", tag) or (tag in self.BLOCKS and self.table is None):
            self._close_block()
        elif self.table is not None:
            if tag in ("td", "th") and self.table["cell"] is not None and self.table["row"] is not None:
                self.table["row"].append(clean(self.table["cell"])[:300])
                self.table["cell"] = None
            elif tag == "tr" and self.table["row"] is not None:
                if self.table["row"]:
                    self.table["rows"].append(self.table["row"][:30])
                self.table["row"] = None
            elif tag == "table":
                t = self.table
                self.table = None
                if t["rows"]:
                    self.blocks.append({"type": "table", "text": t["caption"], "rows": t["rows"][:400],
                                        "head": t["head"], "lm": t["lm"], "links": []})

    def handle_data(self, data):
        if self.ld:
            self.ld_text.append(data)
            return
        if self.skip:
            return
        if self.in_title:
            self.meta["title"] += data
            return
        if self.a_href is not None:
            self.a_text.append(data)
        if self.table is not None:
            if self.table["cell"] is not None:
                self.table["cell"] += data
            elif self.stack and self.stack[-1][0] == "caption":
                self.table["caption"] += data
            return
        if self.cur is not None:
            self.cur["text"] += data
        elif clean(data) and len(clean(data)) > 30:
            self.blocks.append({"type": "p", "text": clean(data), "lm": self._landmark(), "links": []})

    def _close_block(self):
        if self.cur is not None:
            c = self.cur
            self.cur = None
            c["text"] = c["text"].strip("\n") if c["type"] == "pre" else clean(c["text"])
            if c["text"]:
                self.blocks.append(c)

    def _abs(self, href):
        if not href:
            return ""
        from urllib.parse import urljoin
        return urljoin(self.base, href)

    def result(self):
        self._close_block()
        self.meta["title"] = clean(self.meta["title"])
        return {"meta": self.meta, "blocks": self.blocks, "truncated": False}


def from_html(html, url=""):
    p = _HtmlBlocks(url)
    try:
        p.feed(html or "")
        p.close()
    except Exception:            # malformed markup: keep what was parsed
        pass
    out = p.result()
    out["meta"]["url"] = url
    out["frames_unavailable"] = list(out["meta"]["frames"])      # fetch_url never loads frames
    return out


# ------------------------------------------------------------------------------------------------ build a page

def _fp(text):
    return hashlib.sha1(clean(text).lower().encode()).hexdigest()[:16]


def table_text(rows, head=True, max_rows=60):
    if not rows:
        return ""
    w = max(len(r) for r in rows)
    norm = [r + [""] * (w - len(r)) for r in rows[:max_rows]]
    lines = ["| " + " | ".join(c.replace("|", "/") for c in norm[0]) + " |"]
    if head:
        lines.append("|" + "---|" * w)
    lines += ["| " + " | ".join(c.replace("|", "/") for c in r) + " |" for r in norm[1:]]
    if len(rows) > max_rows:
        lines.append(f"... {len(rows) - max_rows} more rows (browser_extract kind=tables for all of them)")
    return "\n".join(lines)


def build(raw, method, seen=None, accessed=None):
    """raw: {meta, blocks, truncated} from BLOCKS_JS or from_html. seen: a set of block fingerprints from earlier
    pages of the same crawl (updated in place). Returns the page dict used by format()/select()."""
    meta = dict(raw.get("meta") or {})
    blocks = list(raw.get("blocks") or [])
    has_main = any(b.get("lm") == "main" for b in blocks) or \
        sum(len(b.get("text") or "") for b in blocks if b.get("lm") not in BOILER) > 400
    dropped_boiler = dropped_dupe = 0
    kept, local = [], set()
    for b in blocks:
        if has_main and b.get("lm") in BOILER:
            dropped_boiler += 1
            continue
        body = b.get("text") or ""
        if b.get("type") == "table":
            body += json.dumps(b.get("rows") or [])[:2000]
        fp = _fp(body) if body else None
        if fp and b.get("type") != "heading":
            if fp in local or (seen is not None and fp in seen and len(body) < 600):
                dropped_dupe += 1
                continue
            local.add(fp)
        kept.append(b)
    if seen is not None:
        seen.update(local)
    sections, path, cur = [], [], None

    def new_section(title, level):
        nonlocal cur
        cur = {"heading": title, "level": level, "path": " > ".join([h for _, h in path]), "parts": [], "links": [],
               "tables": 0}
        sections.append(cur)
    for b in kept:
        if b["type"] == "heading":
            lvl = int(b.get("level") or 2)
            while path and path[-1][0] >= lvl:
                path.pop()
            path.append((lvl, b["text"]))
            new_section(b["text"], lvl)
            continue
        if cur is None:
            new_section(meta.get("title") or "Top of page", 0)
        if b["type"] == "table":
            cur["parts"].append(("table", ((b.get("text") + "\n") if b.get("text") else "") +
                                 table_text(b.get("rows") or [], b.get("head", True)), b))
            cur["tables"] += 1
        elif b["type"] == "li":
            cur["parts"].append(("li", "- " + b["text"], b))
        elif b["type"] == "pre":
            cur["parts"].append(("pre", "```\n" + b["text"] + "\n```", b))
        elif b["type"] == "quote":
            cur["parts"].append(("quote", "> " + b["text"], b))
        else:
            cur["parts"].append(("p", b["text"], b))
        cur["links"] += [l for l in b.get("links") or [] if l and l[1]][:20]
    out, n = [], 0
    for s in sections:
        if not s["parts"] and s["level"] > 0:
            # a heading with nothing under it before the next heading: keep it as an empty marker in the outline
            pass
        n += 1
        pieces, buf = [], []
        size = 0
        for kind, text, _b in s["parts"]:
            if buf and size + len(text) > SECTION_MAX:
                pieces.append("\n".join(buf))
                buf, size = [], 0
            if len(text) > SECTION_MAX * 2 and kind == "p":       # one giant paragraph: cut at sentence ends
                for part in re.findall(r".{1,%d}(?:[.!?](?=\s)|$)" % SECTION_MAX, text, re.S):
                    pieces.append(part.strip())
                continue
            buf.append(text)
            size += len(text) + 1
        if buf:
            pieces.append("\n".join(buf))
        if not pieces:
            pieces = [""]
        for j, body in enumerate(pieces, 1):
            sid = f"s{n}" if len(pieces) == 1 else f"s{n}.{j}"
            out.append({"id": sid, "heading": s["heading"], "level": s["level"], "path": s["path"], "text": body,
                        "chars": len(body), "links": s["links"][:30] if j == 1 else [], "tables": s["tables"]})
    page = {"url": meta.get("url", ""), "title": meta.get("title", ""), "canonical": meta.get("canonical", ""),
            "lang": meta.get("lang", ""), "description": meta.get("description", ""),
            "ld_types": meta.get("ld_types", []), "forms": meta.get("forms", []), "links": meta.get("links", 0),
            "tables": meta.get("tables", 0), "frames": meta.get("frames", []),
            "frames_unavailable": raw.get("frames_unavailable", []), "frames_read": raw.get("frames_read", []),
            "accessed": accessed or time.strftime("%Y-%m-%d %H:%M:%S"), "method": method,
            "sections": out, "blocks_truncated": bool(raw.get("truncated")),
            "dropped": {"boilerplate": dropped_boiler, "duplicates": dropped_dupe},
            "total_chars": sum(s["chars"] for s in out)}
    return page


# ------------------------------------------------------------------------------------------------ select and format

def _terms(s):
    return [w for w in re.findall(r"[a-z0-9]{2,}", str(s or "").lower())
            if w not in {"the", "and", "for", "with", "that", "this", "are", "was", "you", "your", "from", "what"}]


def select(page, query="", ids=None, budget=DEFAULT_BUDGET):
    """(sections to show, info). ids: exact sections. query: most relevant first (BM25), shown in page order.
    Otherwise page order. Never more than budget characters; info says what was left out."""
    secs = page["sections"]
    if ids:
        want = set(ids)
        pool = [s for s in secs if s["id"] in want or s["id"].split(".")[0] in want]
        missing = sorted(want - {s["id"] for s in pool} - {s["id"].split(".")[0] for s in pool})
    else:
        pool, missing = list(secs), []
    ranked = pool
    if query and not ids:
        q = _terms(query)
        docs = [_terms(s["path"] + " " + s["heading"] + " " + s["text"]) for s in pool]
        avg = sum(len(d) for d in docs) / max(1, len(docs))
        df = {}
        for d in docs:
            for w in set(d):
                df[w] = df.get(w, 0) + 1
        N = len(docs)

        def score(d):
            sc = 0.0
            for w in q:
                tf = d.count(w)
                if not tf:
                    continue
                idf = log(1 + (N - df.get(w, 0) + 0.5) / (df.get(w, 0) + 0.5))
                sc += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(d) / max(1, avg)))
            return sc
        scored = [(score(d), i) for i, d in enumerate(docs)]
        ranked = [pool[i] for sc, i in sorted(scored, key=lambda x: -x[0]) if sc > 0]
    chosen, used = [], 0
    for s in ranked:
        if used + s["chars"] > budget and chosen:
            continue
        chosen.append(s)
        used += s["chars"]
        if used >= budget:
            break
    order = {s["id"]: i for i, s in enumerate(secs)}
    chosen.sort(key=lambda s: order[s["id"]])
    shown = {s["id"] for s in chosen}
    left = [s for s in (ranked if query and not ids else pool) if s["id"] not in shown]
    info = {"shown": len(chosen), "total": len(secs), "chars_shown": used, "chars_total": page["total_chars"],
            "left_out": [s["id"] for s in left], "missing_ids": missing,
            "matched": len(ranked) if query and not ids else None,
            "complete": not left and not page["blocks_truncated"] and not missing and not (query and not ids
                                                                                             and len(ranked) < len(secs))}
    return chosen, info


def outline(page, max_lines=80):
    lines = []
    for s in page["sections"][:max_lines]:
        ind = "  " * max(0, (s["level"] or 1) - 1)
        lines.append(f"{ind}[{s['id']}] {s['heading'][:90] or '(untitled)'} · {s['chars']:,} chars"
                     + (f" · {s['tables']} table" + ("s" if s["tables"] != 1 else "") if s["tables"] else ""))
    if len(page["sections"]) > max_lines:
        lines.append(f"... {len(page['sections']) - max_lines} more sections")
    return "\n".join(lines)


def provenance(page):
    p = [f"Source: {page['url']}", f"Title: {page['title'] or '(none)'}"]
    if page.get("canonical") and page["canonical"] != page["url"]:
        p.append(f"Canonical: {page['canonical']}")
    p.append(f"Read: {page['accessed']} via {page['method']}")
    return "\n".join(p)


def render(page, chosen, info, show_outline=True):
    """Text for the model: provenance, completeness flags, outline, then the chosen sections."""
    flags = []
    if info["complete"]:
        flags.append("complete: every section of the page is below")
    else:
        flags.append(f"partial: {info['shown']} of {info['total']} sections shown "
                     f"({info['chars_shown']:,} of {info['chars_total']:,} chars)")
        if info.get("matched") is not None:
            flags.append(f"{info['matched']} section{'' if info['matched'] == 1 else 's'} matched the query")
        if info["left_out"]:
            ids = ", ".join(info["left_out"][:30]) + (" ..." if len(info["left_out"]) > 30 else "")
            flags.append(f"not shown: {ids} (ask for them by id)")
        if info["missing_ids"]:
            flags.append("unknown ids: " + ", ".join(info["missing_ids"]))
    if page["blocks_truncated"]:
        flags.append("the page was larger than Aero's extraction limit: content after the limit was NOT read")
    if page.get("frames_unavailable"):
        flags.append(f"{len(page['frames_unavailable'])} embedded frame(s) could not be read: "
                     + ", ".join(page["frames_unavailable"][:5]))
    d = page.get("dropped") or {}
    if d.get("boilerplate") or d.get("duplicates"):
        flags.append(f"left out as page furniture: {d.get('boilerplate', 0)} navigation/header/footer blocks, "
                     f"{d.get('duplicates', 0)} repeated blocks")
    head = provenance(page) + "\n" + "\n".join("- " + f for f in flags)
    parts = [head]
    if show_outline:
        parts.append("Outline:\n" + outline(page))
    for s in chosen:
        title = f"## [{s['id']}] {s['path'] or s['heading']}"
        body = s["text"]
        if s.get("links"):
            body += "\nLinks: " + "; ".join(f"{t or '(link)'} <{u}>" for t, u in s["links"][:12])
        parts.append(title + "\n" + body)
    return "\n\n".join(parts)


def tables(raw):
    """[{index, caption, rows, head}] for every table in the raw blocks (nothing deduplicated or cut)."""
    out = []
    for b in raw.get("blocks") or []:
        if b.get("type") == "table":
            out.append({"index": len(out) + 1, "caption": b.get("text") or "", "rows": b.get("rows") or [],
                        "head": b.get("head", True), "landmark": b.get("lm") or ""})
    return out


# ------------------------------------------------------------------------------------------------ page cache

_pages = {}             # (owner, url) -> (page, raw)
_order = []
CACHE = 8


def remember(owner, page, raw):
    key = (owner, page["url"])
    _pages[key] = (page, raw)
    if key in _order:
        _order.remove(key)
    _order.append(key)
    while len(_order) > CACHE:
        _pages.pop(_order.pop(0), None)


def recall(owner, url):
    return _pages.get((owner, url))
