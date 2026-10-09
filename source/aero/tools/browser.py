"""Aero's own browser (Playwright on the installed Edge, Chrome or Chromium, with a separate profile).

How it works:
  - Background by default (Settings > Tools > Browser): no window, no focus taken, nothing on screen. It is Aero's own
    profile, not the user's browser: it is signed in only where the user signed in to it. browser_open(show=true)
    switches to a visible window (same profile) so the user can sign in or watch.
  - Each agent gets its own tab, so two agents browsing at once don't click on each other's pages.
  - Pages are described as numbered interactive elements ([12] button "Sign in"); the numbers belong to the latest
    snapshot of that tab and a number from an older snapshot is refused instead of clicking the wrong thing.
  - browser_read_sections reads the whole page (below the fold too) as numbered sections with headings, links,
    tables, provenance and explicit flags for anything cut or unreadable (extraction.py). browser_extract returns
    tables, links, forms and page facts as data.
  - Strict offline blocks every request the browser makes to anything but this computer, inside the browser itself
    (on top of the tools being hidden).
"""
import os
import queue
import re
import threading
import time
from urllib.parse import urlparse

from . import tool
from .. import action_results, attachments, extraction
from ..config import DATA

_SNAPSHOT_JS = r"""
(maxText) => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none' && r.bottom > 0 && r.right > 0 &&
           r.top < innerHeight * 3; };
  const gen = String(Date.now() % 100000);
  document.querySelectorAll('[data-rp-ref]').forEach(e => { e.removeAttribute('data-rp-ref'); e.removeAttribute('data-rp-gen'); });
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],' +
              '[role=checkbox],[role=radio],[role=option],[role=combobox],[role=searchbox],[contenteditable=""],[contenteditable=true],[onclick]';
  const items = []; let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (!vis(el) || n >= 250) continue;
    n++; el.setAttribute('data-rp-ref', n); el.setAttribute('data-rp-gen', gen);
    const tag = el.tagName.toLowerCase(); const role = el.getAttribute('role') || '';
    const isField = tag === 'input' || tag === 'textarea' || tag === 'select';
    let label = (el.getAttribute('aria-label') || (isField ? (el.placeholder || el.name || el.title) : (el.innerText || el.value || el.title || el.alt)) || '').trim().replace(/\s+/g,' ');
    if (!label && el.querySelector('img')) label = el.querySelector('img').alt || 'image';
    let kind = role || tag; if (tag === 'input') kind = 'input:' + (el.type || 'text');
    let extra = '';
    if (tag === 'a') extra = ' -> ' + (el.getAttribute('href') || '').slice(0, 80);
    if (tag === 'input' && (el.type === 'checkbox' || el.type === 'radio')) extra = el.checked ? ' [checked]' : ' [ ]';
    if ((tag === 'input' || tag === 'textarea') && el.value && el.type !== 'password') extra += ' value="' + el.value.slice(0, 60) + '"';
    if (tag === 'select') extra = ' options: ' + Array.from(el.options).slice(0, 15).map(o => o.text.trim()).join(' | ');
    items.push('[' + n + '] ' + kind + ' "' + label.slice(0, 90) + '"' + extra);
  }
  const full = (document.body ? document.body.innerText : '').replace(/\n\s*\n+/g, '\n').trim();
  let text = full;
  if (text.length > maxText) text = text.slice(0, maxText);
  return { title: document.title, url: location.href, items: items.join('\n'), text, total: full.length, gen,
           password: !!document.querySelector('input[type=password]') };
}
"""

_SIGNIN = re.compile(r"(^|[./])(accounts\.google\.com|login\.|signin\.|auth\.|sso\.)|/(log-?in|sign-?in|oauth|"
                     r"authorize|sso)\b", re.I)


class StaleRef(RuntimeError):
    pass


class _Browser:
    """All Playwright calls run on one worker thread (the sync API is thread-bound). Each owner (an agent's chat)
    has its own tab."""

    def __init__(self):
        self.q = queue.Queue()
        self.thread = None
        self.pw = self.ctx = None
        self.pages = {}              # owner -> page
        self.gens = {}               # owner -> generation of its latest snapshot
        self.headed = None           # True / False: how the running context was started

    def _ensure_thread(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, daemon=True, name="browser")
        self.thread.start()

    def _loop(self):
        while True:
            fn, box, ev = self.q.get()
            try:
                box["result"] = fn()
            except Exception as e:  # noqa: BLE001
                box["error"] = e
            ev.set()

    def call(self, fn, timeout=120, cancel=None):
        """Run fn on the browser thread. With cancel (Stop), returns early; the browser finishes that one step but
        nothing after it runs for this agent."""
        self._ensure_thread()
        box, ev = {}, threading.Event()
        self.q.put((fn, box, ev))
        t0 = time.monotonic()
        while not ev.wait(0.25):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("Stopped.")
            if time.monotonic() - t0 > timeout:
                raise TimeoutError("browser action timed out")
        if "error" in box:
            raise box["error"]
        return box["result"]

    # --- run on the worker thread ---
    def _want_headed(self, show=False):
        from .. import osinfo
        from ..config import load_settings
        if not osinfo.has_display() or os.environ.get("AERO_HEADLESS") == "1":
            return False
        return bool(show) or load_settings().get("browser_mode") == "visible"

    def _context(self, show=False):
        want = self._want_headed(show)
        if self.ctx is not None and (self.headed == want or (self.headed and not show)):
            return self.ctx
        if self.ctx is not None:                     # switch background <-> visible: same profile, new process
            try:
                self.ctx.close()
            except Exception:
                pass
            self._reset()
        from playwright.sync_api import sync_playwright
        if self.pw is None:
            self.pw = sync_playwright().start()
        prof = str(DATA / "browser-profile")
        from .. import osinfo
        exe = os.environ.get("AERO_BROWSER") or osinfo.browser_executable()
        last = None
        args = ["--start-maximized"] if want else ["--mute-audio"]
        for kw in ([{"executable_path": exe}] if exe else []) + [{"channel": "msedge"}, {"channel": "chrome"}, {}]:
            try:
                self.ctx = self.pw.chromium.launch_persistent_context(prof, headless=not want, viewport=None if want
                                                                      else {"width": 1366, "height": 900},
                                                                      args=args, **kw)
                break
            except Exception as e:  # noqa: BLE001
                last = e
        if self.ctx is None:
            raise RuntimeError("Could not start a browser. Install Chrome, Edge, Chromium or Brave, or run "
                               f"`python -m playwright install chromium` in Aero's environment. ({last})")
        self.headed = want
        self.ctx.on("close", lambda *_: self._reset())
        self.ctx.route("**/*", _offline_route)
        return self.ctx

    def _reset(self):
        self.ctx = None
        self.pages = {}
        self.gens = {}

    def page(self, owner, show=False, new_tab=False):
        ctx = self._context(show)
        p = self.pages.get(owner)
        if p is None or p.is_closed() or new_tab:
            blank = [x for x in ctx.pages if not x.is_closed() and x.url in ("about:blank", "") and
                     x not in self.pages.values()]
            p = blank[0] if blank and not new_tab else ctx.new_page()
            self.pages[owner] = p
        if show:
            try:
                p.bring_to_front()
            except Exception:
                pass
        return p

    def tabs(self, owner):
        ctx = self._context()
        mine = self.pages.get(owner)
        return [(i, x, x is mine) for i, x in enumerate(ctx.pages) if not x.is_closed()]

    def snapshot(self, owner, max_text=6000):
        p = self.page(owner)
        try:
            p.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        d = p.evaluate(_SNAPSHOT_JS, max_text)
        self.gens[owner] = d["gen"]
        tabs = len([x for x in self.ctx.pages if not x.is_closed()])
        cut = d["total"] > len(d["text"])
        txt = (f"URL: {d['url']}\nTitle: {d['title']}\nTabs open: {tabs}\n\nInteractive elements:\n{d['items'] or '(none)'}"
               f"\n\nPage text:\n{d['text']}")
        if cut:
            txt += (f"\n[page text cut at {len(d['text']):,} of {d['total']:,} chars: use browser_read_sections "
                    "to read the rest]")
        if d.get("password") or _SIGNIN.search(d["url"] or ""):
            txt += ("\n[This page asks for a sign-in. Never type the user's password: ask them to sign in "
                    "themselves (browser_open with show=true opens a window they can use).]")
        return txt

    def locate(self, owner, ref):
        p = self.page(owner)
        loc = p.locator(f'[data-rp-ref="{int(ref)}"]').first
        gen = self.gens.get(owner)
        try:
            n = p.locator(f'[data-rp-ref="{int(ref)}"]').count()
            g = loc.get_attribute("data-rp-gen", timeout=1500) if n else None
        except Exception:
            n, g = 0, None
        if not n or (gen and g != gen):
            raise StaleRef(f"Element [{ref}] is not on the page from your latest snapshot any more (the page changed "
                           "or it was from an older snapshot). Call browser_snapshot for fresh numbers.")
        return loc

    def close_owner(self, owner):
        p = self.pages.pop(owner, None)
        self.gens.pop(owner, None)
        if p is not None and not p.is_closed():
            try:
                p.close()
            except Exception:
                pass


def _offline_route(route):
    """Strict offline, enforced inside the browser: anything that isn't this computer is aborted and audited."""
    try:
        from .. import localonly
        host = urlparse(route.request.url).hostname or ""
        if route.request.url.startswith(("data:", "blob:", "about:", "chrome:", "edge:")) or localonly.is_loopback(host):
            return route.continue_()
        if localonly.strict():
            localonly.audit(route.request.method, host, False, "browser")
            return route.abort("blockedbyclient")
    except Exception:
        pass
    return route.continue_()


B = _Browser()


def _go(ctx, fn, timeout=120):
    return B.call(fn, timeout, getattr(ctx, "cancel", None))


def _owner(ctx):
    return getattr(ctx, "owner", None) or "aero"


def stop_owner(owner):
    """Stop pressed or the turn ended: close this agent's tab when the browser is idle (never blocks)."""
    if B.thread and B.thread.is_alive() and owner in B.pages:
        B.q.put((lambda: B.close_owner(owner), {}, threading.Event()))


@tool("browser_open", "Open a URL in Aero's own browser (in the background unless show=true, which opens a visible "
      "window the user can watch or sign in with). Returns the page's interactive elements as [number] entries "
      "plus the start of its text.", "browser",
      {"url": {"type": "string"}, "new_tab": {"type": "boolean"},
       "show": {"type": "boolean", "description": "Open a visible window (for signing in or when the user wants to "
                                                  "watch)"}}, ["url"], summary=lambda a: a.get("url", ""))
def browser_open(ctx, url, new_tab=False, show=False):
    url = str(url or "").strip()
    if "://" not in url and not url.startswith("about:"):
        url = "https://" + url
    scheme = url.split(":", 1)[0].lower()
    if scheme not in ("http", "https", "about", "file"):
        return {"text": f"The browser only opens web pages (not {scheme}: links).", "error": True}
    owner = _owner(ctx)

    def go():
        p = B.page(owner, show=show, new_tab=new_tab)
        p.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            p.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        return B.snapshot(owner)
    return _go(ctx, go)


@tool("browser_snapshot", "Re-read the current page: interactive elements and visible text.", "browser",
      {"max_text": {"type": "integer"}})
def browser_snapshot(ctx, max_text=6000):
    owner = _owner(ctx)
    return _go(ctx, lambda: B.snapshot(owner, int(max_text or 6000)))


@tool("browser_click", "Click element [ref] from the latest snapshot.", "browser",
      {"ref": {"type": "integer"}}, ["ref"], summary=lambda a: f"[{a.get('ref')}]")
def browser_click(ctx, ref):
    owner = _owner(ctx)

    def go():
        el = B.locate(owner, ref)
        p = B.page(owner)
        before_url, before_n = p.url, len(B.ctx.pages)
        el.click(timeout=10000)
        p.wait_for_timeout(700)
        if len(B.ctx.pages) > before_n:                  # opened a tab: that tab becomes this agent's
            B.pages[owner] = B.ctx.pages[-1]
        snap = B.snapshot(owner)
        moved = B.pages[owner].url != before_url
        env = action_results.make("browser_isolated", verified=True if moved else None,
                                  method="the page navigated" if moved else "", mode="BROWSER_ISOLATED")
        return action_results.attach({"text": snap}, env)
    try:
        return _go(ctx, go)
    except StaleRef as e:
        return {"text": str(e), "error": True}


@tool("browser_type", "Type into element [ref] (replaces its content). submit=true presses Enter.", "browser",
      {"ref": {"type": "integer"}, "text": {"type": "string"}, "submit": {"type": "boolean"}},
      ["ref", "text"], summary=lambda a: f"[{a.get('ref')}] {a.get('text', '')[:60]}")
def browser_type(ctx, ref, text, submit=False):
    owner = _owner(ctx)

    def go():
        el = B.locate(owner, ref)
        try:
            if (el.get_attribute("type", timeout=1500) or "").lower() == "password":
                return {"text": "That is a password field. Aero never types passwords: ask the user to sign in "
                                "themselves (browser_open with show=true).", "error": True}
        except Exception:
            pass
        el.fill(text, timeout=10000)
        got = None
        try:
            got = el.input_value(timeout=1500)
        except Exception:
            pass
        if submit:
            el.press("Enter")
            B.page(owner).wait_for_timeout(1200)
        snap = B.snapshot(owner)
        ok = None if got is None else (got == text)
        env = action_results.make("browser_isolated", verified=ok, method="the field's value read back"
                                  if got is not None else "", mode="BROWSER_ISOLATED")
        return action_results.attach({"text": snap}, env)
    try:
        return _go(ctx, go)
    except StaleRef as e:
        return {"text": str(e), "error": True}


@tool("browser_select", "Choose an option in a <select> element [ref] by its visible text.", "browser",
      {"ref": {"type": "integer"}, "option": {"type": "string"}}, ["ref", "option"])
def browser_select(ctx, ref, option):
    owner = _owner(ctx)
    try:
        return _go(ctx, lambda: (B.locate(owner, ref).select_option(label=option), B.snapshot(owner))[1])
    except StaleRef as e:
        return {"text": str(e), "error": True}


@tool("browser_press", "Press a key in the browser, e.g. 'Enter', 'Escape', 'PageDown', 'Control+L'.", "browser",
      {"key": {"type": "string"}}, ["key"])
def browser_press(ctx, key):
    owner = _owner(ctx)

    def go():
        p = B.page(owner)
        p.keyboard.press(key)
        p.wait_for_timeout(500)
        return B.snapshot(owner)
    return _go(ctx, go)


@tool("browser_scroll", "Scroll the page. direction 'down' or 'up'. To read a long page, browser_read_sections is "
      "better: it reads all of it at once.", "browser",
      {"direction": {"type": "string", "enum": ["down", "up"]}})
def browser_scroll(ctx, direction="down"):
    owner = _owner(ctx)

    def go():
        p = B.page(owner)
        p.mouse.wheel(0, 900 if direction != "up" else -900)
        p.wait_for_timeout(500)
        return B.snapshot(owner)
    return _go(ctx, go)


@tool("browser_back", "Go back in browser history.", "browser", {})
def browser_back(ctx):
    owner = _owner(ctx)

    def go():
        B.page(owner).go_back(wait_until="domcontentloaded")
        return B.snapshot(owner)
    return _go(ctx, go)


@tool("browser_screenshot", "Screenshot the browser viewport (for vision models).", "browser", {})
def browser_screenshot(ctx):
    owner = _owner(ctx)
    png = _go(ctx, lambda: B.page(owner).screenshot(type="png"))
    meta = attachments.save_image_bytes(png, "browser.png")
    return {"text": "Browser screenshot captured." + ("" if ctx.vision else " (model has no vision)"),
            "image": meta["id"]}


@tool("browser_read", "Get the plain visible text of the current page. For long pages, browser_read_sections is "
      "better (sections, headings, links, tables, and what was left out).", "browser",
      {"max_chars": {"type": "integer"}})
def browser_read(ctx, max_chars=40000):
    owner = _owner(ctx)
    n = int(max_chars or 40000)

    def go():
        p = B.page(owner)
        t = p.evaluate("() => document.body ? document.body.innerText : ''")
        out = f"URL: {p.url}\n\n{t[:n]}"
        if len(t) > n:
            out += f"\n[TRUNCATED: showed {n:,} of {len(t):,} chars; use browser_read_sections for the rest]"
        return out
    return _go(ctx, go)


# ---- whole-page reading ---------------------------------------------------------------------------------------

LIMITS = {"blocks": 6000, "chars": 1_500_000}


def _extract(owner):
    """Run the block extractor in the page and in every frame it can read (on the browser thread)."""
    p = B.page(owner)
    try:
        p.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    raw = p.evaluate(extraction.BLOCKS_JS, LIMITS)
    raw["frames_read"], raw["frames_unavailable"] = [], []
    for fr in p.frames[1:12]:
        if fr.is_detached():
            continue
        try:
            sub = fr.evaluate(extraction.BLOCKS_JS, {"blocks": 800, "chars": 200_000})
            for b in sub.get("blocks") or []:
                b["frame"] = fr.url
                raw["blocks"].append(b)
            raw["frames_read"].append(fr.url)
        except Exception:
            raw["frames_unavailable"].append(fr.url or "(frame)")
    page = extraction.build(raw, "Aero's browser (rendered page, all of it, not just the visible part)")
    extraction.remember(owner, page, raw)
    return page, raw


@tool("browser_read_sections", "Read the whole current page as numbered sections under their headings (everything "
      "on the page, not just what is on screen), with links, tables, the source URL and when it was read. With "
      "query, only the matching sections; with sections=['s3','s7'], exactly those. Says what was left out.",
      "browser", {"query": {"type": "string"}, "sections": {"type": "array", "items": {"type": "string"}},
                  "max_chars": {"type": "integer", "description": "Text budget for this call (default 12000)"}},
      summary=lambda a: a.get("query") or ", ".join(a.get("sections") or []) or "whole page")
def browser_read_sections(ctx, query="", sections=None, max_chars=12000):
    owner = _owner(ctx)

    def go():
        cur = B.page(owner).url
        hit = extraction.recall(owner, cur) if sections else None
        page = hit[0] if hit else _extract(owner)[0]
        chosen, info = extraction.select(page, query=query or "", ids=sections or None,
                                         budget=max(1000, min(60000, int(max_chars or 12000))))
        return extraction.render(page, chosen, info, show_outline=not sections)
    return _go(ctx, go, timeout=180)


@tool("browser_extract", "Get data from the current page: kind='tables' (every table, all rows, as JSON; table=n "
      "for one), 'links' (text and URL), 'forms' (their fields, never values), or 'metadata' (title, canonical URL, "
      "language, description, structured-data types, headings).", "browser",
      {"kind": {"type": "string", "enum": ["tables", "links", "forms", "metadata"]},
       "table": {"type": "integer"}}, ["kind"], summary=lambda a: a.get("kind", ""))
def browser_extract(ctx, kind="metadata", table=None):
    import json
    owner = _owner(ctx)

    def go():
        page, raw = _extract(owner)
        prov = extraction.provenance(page)
        if kind == "tables":
            ts = extraction.tables(raw)
            if table:
                ts = [t for t in ts if t["index"] == int(table)]
            if not ts:
                return prov + "\nNo tables" + (f" with index {table}" if table else "") + " on this page."
            body = json.dumps(ts, ensure_ascii=False)
            if len(body) > 60000:
                return prov + f"\n{len(ts)} tables, too large to show at once ({len(body):,} chars): ask for one " \
                              "with table=n. Sizes: " + ", ".join(f"#{t['index']} {len(t['rows'])} rows" for t in ts)
            return prov + f"\n{len(ts)} table(s), all rows:\n{body}"
        if kind == "links":
            seen, out = set(), []
            for b in raw.get("blocks") or []:
                for t, u in b.get("links") or []:
                    if u not in seen:
                        seen.add(u)
                        out.append({"text": t, "url": u})
            body = json.dumps(out[:500], ensure_ascii=False)
            return prov + f"\n{len(out)} links" + (" (first 500)" if len(out) > 500 else "") + f":\n{body}"
        if kind == "forms":
            return prov + "\nForms (field names only):\n" + json.dumps(page.get("forms") or [], ensure_ascii=False)
        heads = [{"id": s["id"], "level": s["level"], "heading": s["heading"]} for s in page["sections"]]
        meta = {k: page.get(k) for k in ("url", "title", "canonical", "lang", "description", "ld_types", "links",
                                         "tables", "frames", "frames_unavailable", "accessed")}
        meta["headings"] = heads[:200]
        return prov + "\n" + json.dumps(meta, ensure_ascii=False)
    return _go(ctx, go, timeout=180)


@tool("browser_tabs", "List the browser's tabs, switch this agent to one (action='select', index=n) or close one "
      "(action='close'). Only tabs Aero's browser opened; never the user's own browser.", "browser",
      {"action": {"type": "string", "enum": ["list", "select", "close"]}, "index": {"type": "integer"}},
      summary=lambda a: a.get("action", "list") + (f" {a['index']}" if a.get("index") is not None else ""))
def browser_tabs(ctx, action="list", index=None):
    owner = _owner(ctx)

    def go():
        tabs = B.tabs(owner)
        if action in ("select", "close"):
            m = next((x for x in tabs if x[0] == int(index or 0)), None)
            if m is None:
                return {"text": f"No tab {index}.", "error": True}
            if action == "select":
                taken = [o for o, pg in B.pages.items() if pg is m[1] and o != owner]
                if taken:
                    return {"text": "Another agent is working in that tab.", "error": True}
                B.pages[owner] = m[1]
                return B.snapshot(owner)
            m[1].close()
            tabs = B.tabs(owner)
        return "\n".join(f"[{i}] {pg.title()[:80] or '(untitled)'} · {pg.url}" + ("  (yours)" if mine else "")
                         for i, pg, mine in tabs) or "No tabs open."
    return _go(ctx, go)


@tool("browser_wait_for", "Wait until the page shows some text (text='Order placed') or finishes loading "
      "(state='load' or 'networkidle'), up to timeout seconds (max 60).", "browser",
      {"text": {"type": "string"}, "state": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle"]},
       "timeout": {"type": "number"}}, summary=lambda a: a.get("text") or a.get("state") or "")
def browser_wait_for(ctx, text="", state="", timeout=15):
    owner = _owner(ctx)
    ms = int(max(1, min(60, float(timeout or 15))) * 1000)

    def go():
        p = B.page(owner)
        t0 = time.time()
        try:
            if text:
                p.get_by_text(text, exact=False).first.wait_for(timeout=ms)
            else:
                p.wait_for_load_state(state or "load", timeout=ms)
        except Exception:
            return {"text": f"Not seen within {ms // 1000} s: {text or state}. Page: {p.url}", "error": True}
        return f"Seen after {time.time() - t0:.1f} s: {text or state}. Page: {p.title()} · {p.url}"
    return _go(ctx, go, timeout=ms / 1000 + 15)
