"""Lightweight web search (DuckDuckGo HTML endpoint, no API key) and page fetch."""
import html
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from . import tool
from .. import extraction

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36 Edg/130.0"}


def _clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


@tool("web_search", "Search the web. Returns titles, URLs and snippets. Follow up with fetch_url or browser_open.",
      "web", {"query": {"type": "string"}, "max_results": {"type": "integer"}}, ["query"],
      summary=lambda a: a.get("query", ""))
def web_search(ctx, query, max_results=8):
    r = httpx.post("https://html.duckduckgo.com/html/", data={"q": query}, headers=UA, timeout=20,
                   follow_redirects=True)
    r.raise_for_status()
    out = []
    blocks = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|$)',
                        r.text, re.S)
    for href, title, rest in blocks:
        if "duckduckgo.com/y.js" in href:
            continue                                   # ads
        if href.startswith("//duckduckgo.com/l/") or "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', rest, re.S)
        out.append(f"{len(out)+1}. {_clean(title)}\n   {href}\n   {_clean(snip.group(1)) if snip else ''}")
        if len(out) >= int(max_results or 8):
            break
    return "\n".join(out) or "No results (the search endpoint may be rate limiting; try browser_open on a search URL)."


@tool("fetch_url", "Download a web page or text file. A web page comes back as numbered sections under their "
      "headings, with links, tables, the source URL and what was left out; pass query to get only the matching "
      "sections, or sections=['s4'] for exact ones from an earlier fetch. No JavaScript runs (use the browser tools "
      "for pages built by scripts).", "web",
      {"url": {"type": "string"}, "query": {"type": "string"},
       "sections": {"type": "array", "items": {"type": "string"}},
       "max_chars": {"type": "integer", "description": "Text budget (default 12000 for pages, 30000 for text files)"}},
      ["url"], summary=lambda a: a.get("url", "") + (f" · {a['query']}" if a.get("query") else ""))
def fetch_url(ctx, url, max_chars=0, query="", sections=None):
    if "://" not in url:
        url = "https://" + url
    scheme = url.split(":", 1)[0].lower()
    if scheme not in ("http", "https"):
        return {"text": "fetch_url only downloads http and https addresses.", "error": True}
    owner = getattr(ctx, "owner", None) or "aero"
    hit = extraction.recall(owner, url) if sections else None
    if hit:
        page = hit[0]
        status = "cached from the earlier fetch"
    else:
        r = httpx.get(url, headers=UA, timeout=30, follow_redirects=True)
        ct = r.headers.get("content-type", "")
        if "html" not in ct:
            body = r.text
            n = int(max_chars or 30000)
            out = f"HTTP {r.status_code} {r.url}\n\n{body[:n]}"
            if len(body) > n:
                out += f"\n[TRUNCATED: showed {n:,} of {len(body):,} chars; pass a larger max_chars for more]"
            return out
        raw = extraction.from_html(r.text, str(r.url))
        page = extraction.build(raw, "fetch_url (the page's HTML; no JavaScript ran)")
        extraction.remember(owner, page, raw)
        if str(r.url) != url:
            extraction.remember(owner, {**page, "url": url}, raw)
        status = f"HTTP {r.status_code}"
    chosen, info = extraction.select(page, query=query or "", ids=sections or None,
                                     budget=max(1000, min(60000, int(max_chars or 12000))))
    return f"{status}\n" + extraction.render(page, chosen, info, show_outline=not sections)
