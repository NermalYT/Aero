"""Prompt inference: what a plain request means Aero should DO, worked out without a model call.

Small local models often answer "I can't access your GitHub" while Aero has a browser the user can sign in to, or
"I can't run ipconfig" next to a shell tool (measured with Qwen3-4B: 6 of 10 everyday requests acted on with normal
tool calling). For requests whose meaning is clear, an intent gives the model:
  - a concrete plan (which sites, in which order, what to read, what to hand back),
  - the tools for it (added to what the router picked),
  - the first action, forced through forced_call.py (grammar-constrained, so the model can't refuse) when the model
    answers without acting,
  - whether apps and the browser are on screen (default: yes, so the user sees what the model does; "in the
    background" in the request keeps them hidden),
  - whether the browser closes when the task is done.

match(text, settings) returns the best Intent or None. Patterns are deliberately concrete: a request has to name the
thing ("github", "email", "ip address") and an action on it.
"""
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

BACKGROUND = re.compile(r"\b(in the background|without (showing|opening a window)|don'?t show|hidden|headless|"
                        r"quietly)\b", re.I)
VISIBLE = re.compile(r"\b(show me|let me (see|watch)|on screen|visibly|i want to watch)\b", re.I)

MAIL_SITES = [("Gmail", "https://mail.google.com/mail/u/0/"), ("Outlook", "https://outlook.live.com/mail/0/"),
              ("Outlook (work or school)", "https://outlook.office.com/mail/")]
MAIL_HOSTS = ("mail.google.com", "outlook.live.com", "outlook.office.com", "outlook.office365.com", "mail.yahoo.com",
              "www.icloud.com/mail", "mail.proton.me", "mail.zoho.com", "mail.aol.com", "fastmail.com", "gmx.",
              "web.de", "mail.tutanota.com", "app.tuta.com")
CAL_SITES = [("Google Calendar", "https://calendar.google.com/calendar/u/0/r/week"),
             ("Outlook calendar", "https://outlook.live.com/calendar/0/view/workweek")]


@dataclass
class Intent:
    id: str
    label: str
    plan: str
    tools: list
    first_tools: list = field(default_factory=list)
    first_hint: str = ""
    must_act: bool = True
    visible: bool = True
    close_browser: bool = True
    skill: str = ""

    def public(self):
        return {"id": self.id, "label": self.label, "visible": self.visible, "close_browser": self.close_browser,
                "tools": self.tools}


# ---- where the user's mail lives ----------------------------------------------------------------------------------

def _bookmark_files():
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", ""))
        roots = [base / "Microsoft" / "Edge" / "User Data", base / "Google" / "Chrome" / "User Data",
                 base / "BraveSoftware" / "Brave-Browser" / "User Data"]
    else:
        home = Path.home()
        roots = [home / ".config" / "google-chrome", home / ".config" / "microsoft-edge", home / ".config" / "chromium",
                 home / "Library" / "Application Support" / "Google" / "Chrome",
                 home / "Library" / "Application Support" / "Microsoft Edge"]
    out = []
    for r in roots:
        if r.is_dir():
            out += [p for p in r.glob("*/Bookmarks") if p.is_file()][:6]
    return out


def mail_sites(settings=None):
    """[(name, url)]: the accounts the user listed (Settings > Accounts), mail sites saved as bookmarks in their
    browsers (read-only), then Gmail and Outlook. Each site once."""
    seen, out = set(), []

    def add(name, url):
        key = re.sub(r"^https?://|/+$", "", url.lower())
        key = re.sub(r"(/mail)?/u/\d+.*$|/mail/\d*.*$", "", key)
        if key not in seen:
            seen.add(key)
            out.append((name, url))
    for u in (settings or {}).get("mail_accounts") or []:
        add(u.split("/")[2] if "://" in u else u, u)
    if (settings or {}).get("use_bookmarks_for_accounts", True):
        for f in _bookmark_files():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            stack = list((data.get("roots") or {}).values())
            while stack:
                n = stack.pop()
                if not isinstance(n, dict):
                    continue
                stack += n.get("children") or []
                url = n.get("url") or ""
                if url.startswith("http") and any(h in url.lower() for h in MAIL_HOSTS):
                    add(n.get("name") or url, url)
    for name, url in MAIL_SITES:
        add(name, url)
    return out[:8]


# ---- the intents ------------------------------------------------------------------------------------------------

def _github(text, settings):
    gh_api = bool((settings or {}).get("github_linked"))
    plan = ("1. Open https://github.com/notifications with browser_open (show=true, so the user can watch).\n"
            "2. If it shows a sign-in page, Aero asks the user to sign in in that window and waits; then continue.\n"
            "3. Read the notifications (browser_read_sections). Then open https://github.com/pulls (your pull "
            "requests) and https://github.com/pulls/review-requested (reviews asked of you) and read them.\n"
            "4. Answer with what needs the user's attention first: review requests, failing checks, mentions, "
            "assigned issues; one line each with its link.")
    if gh_api:
        plan = "GitHub is linked in Aero: prefer the mcp_github_* tools (notifications, pull requests). If they " \
               "fail, use the browser:\n" + plan
    return Intent("github", "GitHub: notifications and pull requests", plan,
                  ["browser_open", "browser_read_sections", "browser_snapshot", "browser_click", "browser_tabs"],
                  ["browser_open"], "Start with browser_open url=https://github.com/notifications show=true.")


def _mail(text, settings):
    sites = mail_sites(settings)
    meetings = re.search(r"\b(meeting|meetings|invite|invites|invitation|schedule|calendar|appointment)\b", text, re.I)
    listed = "\n".join(f"   - {n}: {u}" for n, u in sites)
    plan = ("Check EVERY mail account below, one after another, in Aero's browser (show=true, the user watches):\n"
            f"{listed}\n"
            "1. browser_open each site. If a sign-in page appears, Aero asks the user to sign in in that window and "
            "waits; if they skip it, go on with the next account. Never type a password.\n"
            "2. Gmail can hold several accounts: after /mail/u/0/ also try /mail/u/1/ and /mail/u/2/ until one shows "
            "an account you already checked or a sign-in page.\n")
    if meetings:
        plan += ("3. For each account, search this week's mail for invitations and meetings (Gmail: open "
                 "#search/invite%20OR%20meeting%20newer_than%3A10d; Outlook: the search box) and read them with "
                 "browser_read_sections. Also read the account's calendar week if it has one "
                 "(https://calendar.google.com/calendar/u/0/r/week, https://outlook.live.com/calendar/0/view/week).\n"
                 "4. Put every meeting into meeting_doc(meetings=[...], week='this'): it makes the downloadable Word "
                 "schedule (titles, times, links, organizer, conflicts). Never invent a time.\n"
                 "5. Answer with the schedule as a table (day, time, title, where/link, account) and the document.")
        skill = "meetings-from-email"
    else:
        plan += ("3. For each account, read the newest unread mail (browser_read_sections).\n"
                 "4. Answer with what needs the user's attention, grouped by account.")
        skill = ""
    return Intent("mail", "Email: every account" + (", meetings this week" if meetings else ""), plan,
                  ["browser_open", "browser_read_sections", "browser_snapshot", "browser_click", "browser_type",
                   "browser_tabs", "meeting_doc", "use_skill"],
                  ["browser_open"], f"Start with browser_open url={sites[0][1]} show=true.", skill=skill)


def _calendar(text, settings):
    listed = "\n".join(f"   - {n}: {u}" for n, u in CAL_SITES)
    plan = (f"Open the user's calendars in Aero's browser (show=true):\n{listed}\n"
            "If a sign-in page appears, Aero asks the user to sign in and waits. Read the week with "
            "browser_read_sections, then answer with the schedule as a table and, when the user wants a document, "
            "meeting_doc(meetings=[...]).")
    return Intent("calendar", "Calendar: this week", plan,
                  ["browser_open", "browser_read_sections", "browser_snapshot", "browser_click", "meeting_doc"],
                  ["browser_open"], f"Start with browser_open url={CAL_SITES[0][1]} show=true.")


def _lookup(text, settings):
    plan = ("This needs current information from the web: search with web_search, then read the best one or two "
            "results with fetch_url (or browser_read_sections). Answer with the facts and their sources.")
    return Intent("lookup", "Look it up on the web", plan, ["web_search", "fetch_url", "browser_open",
                                                             "browser_read_sections"],
                  ["web_search"], "Start with web_search.", visible=False, close_browser=False)


def _system(text, settings):
    plan = ("Find this out on this PC with run_command (PowerShell): for example ipconfig / Get-NetIPAddress for IP "
            "addresses, Get-PSDrive for disk space, Get-Process for what is running, Get-CimInstance "
            "Win32_OperatingSystem for memory. Then answer with the numbers.")
    return Intent("system", "Check this PC", plan, ["run_command"], ["run_command"], "Run the command now.",
                  visible=False, close_browser=False)


def _folder(text, settings):
    plan = ("Look in the folder with list_dir (or find_files for a pattern) and answer with what is there.")
    return Intent("folder", "Look in a folder", plan, ["list_dir", "find_files", "read_file"], ["list_dir", "find_files"],
                  "List the folder now.", visible=False, close_browser=False)


def _open_app(text, settings):
    plan = ("Start the app the user named with app_launch (it is shown on screen unless the user asked for the "
            "background), then do what they asked in it.")
    return Intent("open_app", "Open an app", plan, ["app_find", "app_launch", "app_view", "app_click", "app_type"],
                  ["app_launch", "app_find"], "Launch the app now.", close_browser=False)


RULES = [
    (re.compile(r"\bgithub\b.*\b(notification|pull request|prs?\b|issues?|reviews?|repos?|stars?|actions|"
                r"inbox|mentions?|what needs)|\b(notification|pull request|prs?|issues?|reviews?)\b.*\bgithub\b",
                re.I | re.S), _github),
    (re.compile(r"\b(check|read|look (at|for|through|in)|go through|summari[sz]e|any|unread|find|search|open|"
                r"what'?s in)\b.*\b(e-?mails?|inbox|gmail|outlook|mailbox|my mail)\b|\b(e-?mails?|inbox|gmail|"
                r"outlook)\b.*\b(meetings?|invites?|unread|today|this week)\b", re.I | re.S), _mail),
    (re.compile(r"\b(my|the)\s+(calendar|schedule|agenda)\b|\bmeetings?\b.*\b(this|next) week\b", re.I), _calendar),
    (re.compile(r"\b(weather|forecast|news|headlines|stock price|share price|exchange rate|score|scores|flight status|"
                r"latest version)\b", re.I), _lookup),
    (re.compile(r"\b(ip ?address|ipconfig|disk space|free space|cpu usage|ram usage|memory usage|running processes|"
                r"uptime|wi-?fi password|mac address)\b", re.I), _system),
    (re.compile(r"\b(what'?s|what is|show me|list|files) (in|on) my (downloads|desktop|documents|pictures|music|"
                r"videos)\b|\bmy (downloads|desktop|documents) folder\b", re.I), _folder),
    (re.compile(r"^\s*(please\s+)?(open|launch|start|run)\s+(?!a |an |the file|ipconfig)([\w .'+-]{2,40})\s*[.!]?\s*$",
                re.I), _open_app),
]


def match(text, settings=None):
    """The intent a request plainly has, or None."""
    t = (text or "").strip()
    if not t or len(t) > 2000:
        return None
    for rx, make in RULES:
        if rx.search(t):
            it = make(t, settings or {})
            if BACKGROUND.search(t):
                it.visible = False
            elif VISIBLE.search(t):
                it.visible = True
            return it
    return None


def context_line(it):
    return (f"Plan for this request ({it.label}). Follow it; it was worked out from the request:\n{it.plan}\n"
            + ("Apps and the browser are on screen for this task, so the user can watch." if it.visible else
               "Work in the background (the user asked, or nothing needs to be seen).")
            + (f"\nLoad the skill {it.skill} with use_skill if you need its details." if it.skill else ""))
