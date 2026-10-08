"""Agent-driven web browser (Playwright on the installed Microsoft Edge, separate profile).

The browser window is visible so you can watch and step in. Pages are described to the model as
numbered interactive elements ([12] button "Sign in"), and the model acts on those numbers, which
is far more reliable for small local models than pixel clicking.
"""
import os
import queue
import threading

from . import tool
from .. import attachments
from ..config import DATA

_SNAPSHOT_JS = r"""
(maxText) => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none' && r.bottom > 0 && r.right > 0 &&
           r.top < innerHeight * 3; };
  document.querySelectorAll('[data-rp-ref]').forEach(e => e.removeAttribute('data-rp-ref'));
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],' +
              '[role=checkbox],[role=radio],[role=option],[role=combobox],[role=searchbox],[contenteditable=""],[contenteditable=true],[onclick]';
  const items = []; let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (!vis(el) || n >= 250) continue;
    n++; el.setAttribute('data-rp-ref', n);
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
  let text = (document.body ? document.body.innerText : '').replace(/\n\s*\n+/g, '\n').trim();
  if (text.length > maxText) text = text.slice(0, maxText) + '\n... (page text truncated, scroll or use browser_read for more)';
  return { title: document.title, url: location.href, items: items.join('\n'), text };
}
"""


class _Browser:
    """All Playwright calls run on one worker thread (the sync API is thread-bound)."""

    def __init__(self):
        self.q = queue.Queue()
        self.thread = None
        self.pw = self.ctx = self.page = None

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

    def call(self, fn, timeout=120):
        self._ensure_thread()
        box, ev = {}, threading.Event()
        self.q.put((fn, box, ev))
        if not ev.wait(timeout):
            raise TimeoutError("browser action timed out")
        if "error" in box:
            raise box["error"]
        return box["result"]

    # --- run on the worker thread ---
    def _page(self):
        if self.page is not None and not self.page.is_closed():
            return self.page
        if self.ctx is None:
            from playwright.sync_api import sync_playwright
            self.pw = sync_playwright().start()
            prof = str(DATA / "browser-profile")
            exe = os.environ.get("AERO_BROWSER")
            last = None
            for kw in ([{"executable_path": exe}] if exe else []) + [{"channel": "msedge"}, {"channel": "chrome"}, {}]:
                try:
                    self.ctx = self.pw.chromium.launch_persistent_context(
                        prof, headless=os.environ.get("AERO_HEADLESS") == "1", viewport=None,
                        args=["--start-maximized"], **kw)
                    break
                except Exception as e:  # noqa: BLE001
                    last = e
            if self.ctx is None:
                raise RuntimeError(f"Could not start a browser (Edge/Chrome): {last}")
            self.ctx.on("close", lambda *_: self._reset())
        pages = [p for p in self.ctx.pages if not p.is_closed()]
        self.page = pages[-1] if pages else self.ctx.new_page()
        return self.page

    def _reset(self):
        self.ctx = None
        self.page = None

    def snapshot(self, max_text=6000):
        p = self._page()
        try:
            p.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        d = p.evaluate(_SNAPSHOT_JS, max_text)
        tabs = len(self.ctx.pages)
        return (f"URL: {d['url']}\nTitle: {d['title']}\nTabs open: {tabs}\n\nInteractive elements:\n{d['items'] or '(none)'}"
                f"\n\nPage text:\n{d['text']}")

    def locate(self, ref):
        return self._page().locator(f'[data-rp-ref="{int(ref)}"]').first


B = _Browser()


@tool("browser_open", "Open a URL in the automated browser (a visible Edge window). Returns the page's "
      "interactive elements as [number] entries plus page text.", "browser",
      {"url": {"type": "string"}, "new_tab": {"type": "boolean"}}, ["url"], summary=lambda a: a.get("url", ""))
def browser_open(ctx, url, new_tab=False):
    if "://" not in url and not url.startswith("about:"):
        url = "https://" + url

    def go():
        p = B._page()
        if new_tab:
            p = B.ctx.new_page()
            B.page = p
        p.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            p.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        return B.snapshot()
    return B.call(go)


@tool("browser_snapshot", "Re-read the current page: interactive elements and visible text.", "browser",
      {"max_text": {"type": "integer"}})
def browser_snapshot(ctx, max_text=6000):
    return B.call(lambda: B.snapshot(int(max_text or 6000)))


@tool("browser_click", "Click element [ref] from the last snapshot.", "browser",
      {"ref": {"type": "integer"}}, ["ref"], summary=lambda a: f"[{a.get('ref')}]")
def browser_click(ctx, ref):
    def go():
        el = B.locate(ref)
        before = len(B.ctx.pages)
        el.click(timeout=10000)
        B.page.wait_for_timeout(700)
        if len(B.ctx.pages) > before:
            B.page = B.ctx.pages[-1]
        return B.snapshot()
    return B.call(go)


@tool("browser_type", "Type into element [ref] (replaces its content). submit=true presses Enter.", "browser",
      {"ref": {"type": "integer"}, "text": {"type": "string"}, "submit": {"type": "boolean"}},
      ["ref", "text"], summary=lambda a: f"[{a.get('ref')}] {a.get('text', '')[:60]}")
def browser_type(ctx, ref, text, submit=False):
    def go():
        el = B.locate(ref)
        el.fill(text, timeout=10000)
        if submit:
            el.press("Enter")
            B.page.wait_for_timeout(1200)
        return B.snapshot()
    return B.call(go)


@tool("browser_select", "Choose an option in a <select> element [ref] by its visible text.", "browser",
      {"ref": {"type": "integer"}, "option": {"type": "string"}}, ["ref", "option"])
def browser_select(ctx, ref, option):
    return B.call(lambda: (B.locate(ref).select_option(label=option), B.snapshot())[1])


@tool("browser_press", "Press a key in the browser, e.g. 'Enter', 'Escape', 'PageDown', 'Control+L'.", "browser",
      {"key": {"type": "string"}}, ["key"])
def browser_press(ctx, key):
    def go():
        B._page().keyboard.press(key)
        B.page.wait_for_timeout(500)
        return B.snapshot()
    return B.call(go)


@tool("browser_scroll", "Scroll the page. direction 'down' or 'up'.", "browser",
      {"direction": {"type": "string", "enum": ["down", "up"]}})
def browser_scroll(ctx, direction="down"):
    def go():
        B._page().mouse.wheel(0, 900 if direction != "up" else -900)
        B.page.wait_for_timeout(500)
        return B.snapshot()
    return B.call(go)


@tool("browser_back", "Go back in browser history.", "browser", {})
def browser_back(ctx):
    def go():
        B._page().go_back(wait_until="domcontentloaded")
        return B.snapshot()
    return B.call(go)


@tool("browser_screenshot", "Screenshot the browser viewport (for vision models).", "browser", {})
def browser_screenshot(ctx):
    png = B.call(lambda: B._page().screenshot(type="png"))
    meta = attachments.save_image_bytes(png, "browser.png")
    return {"text": "Browser screenshot captured." + ("" if ctx.vision else " (model has no vision)"),
            "image": meta["id"]}


@tool("browser_read", "Get the full visible text of the current page (long pages, articles, docs).", "browser",
      {"max_chars": {"type": "integer"}})
def browser_read(ctx, max_chars=40000):
    def go():
        p = B._page()
        t = p.evaluate("() => document.body ? document.body.innerText : ''")
        return f"URL: {p.url}\n\n{t[:int(max_chars or 40000)]}"
    return B.call(go)
