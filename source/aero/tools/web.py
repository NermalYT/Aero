"""Lightweight web search (DuckDuckGo HTML endpoint, no API key) and page fetch."""
import html
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from . import tool
from ..attachments import _html_text

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


@tool("fetch_url", "Download a web page or text file and return its readable text.", "web",
      {"url": {"type": "string"}, "max_chars": {"type": "integer"}}, ["url"], summary=lambda a: a.get("url", ""))
def fetch_url(ctx, url, max_chars=30000):
    if "://" not in url:
        url = "https://" + url
    r = httpx.get(url, headers=UA, timeout=30, follow_redirects=True)
    ct = r.headers.get("content-type", "")
    body = r.text
    if "html" in ct:
        title = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        body = (f"Title: {_clean(title.group(1))}\n\n" if title else "") + _html_text(body)
    n = int(max_chars or 30000)
    return f"HTTP {r.status_code} {r.url}\n\n{body[:n]}" + ("\n... (truncated)" if len(body) > n else "")
