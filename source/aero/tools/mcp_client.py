"""MCP (Model Context Protocol) client: local stdio servers and remote Streamable HTTP servers (with OAuth).

data/mcp.json uses the format Claude Desktop, Claude Code and Cursor use:
    {"mcpServers": {
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\\\"]},
        "github": {"type": "http", "url": "https://api.githubcopilot.com/mcp/",
                   "headers": {"Authorization": "Bearer ${secret:github_token}"}},
        "linear": {"type": "http", "url": "https://mcp.linear.app/mcp"}          <- signs in with OAuth
    }}
"${secret:NAME}" in env/headers is replaced with a value from Aero's encrypted vault, so tokens never sit in
the JSON file. Every server tool is offered to the models as mcp_<server>_<tool>.
"""
import base64
import hashlib
import json
import os
import re
import secrets as _rnd
import shutil
import subprocess
import threading
import time
import urllib.parse

import httpx

from . import REGISTRY, Tool
from .. import attachments, vault
from ..config import DATA, IS_WIN, LOGS, UI_PORT

CONFIG = DATA / "mcp.json"
EXAMPLE = {"mcpServers": {}}
PROTOCOL = "2025-06-18"
_servers = {}
_status = {}
_pending_auth = {}      # state -> {server, verifier, client, token_endpoint, resource, redirect}
_SECRET_RE = re.compile(r"\$\{secret:([A-Za-z0-9_.-]+)\}")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def read_config():
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(EXAMPLE, indent=2), encoding="utf-8")
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"mcpServers": {}, "_error": str(e)}


def write_config(cfg):
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def _expand(v):
    if not isinstance(v, str):
        return v
    v = _SECRET_RE.sub(lambda m: vault.get(m.group(1)), v)
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), v)


def redirect_uri():
    return f"http://127.0.0.1:{UI_PORT}/oauth/callback"


class NeedsAuth(Exception):
    def __init__(self, url, www_auth=""):
        super().__init__("sign-in required")
        self.url, self.www_auth = url, www_auth


# ------------------------------------------------------------------------------------------------ stdio

class StdioServer:
    def __init__(self, name, cfg):
        self.name, self.cfg = name, cfg
        self.proc = None
        self.pending = {}
        self.next_id = 1
        self.lock = threading.Lock()
        self.tools = []

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        cmd = _expand(self.cfg["command"])
        exe = shutil.which(cmd) or cmd
        env = dict(os.environ)
        env.update({k: str(_expand(v)) for k, v in (self.cfg.get("env") or {}).items()})
        safe = re.sub(r"\W", "_", self.name)
        log = open(LOGS / f"mcp-{safe}.log", "w", encoding="utf-8", errors="replace")
        args = [_expand(a) for a in self.cfg.get("args") or []]
        self.proc = subprocess.Popen([exe] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, env=env,
                                     cwd=self.cfg.get("cwd"), creationflags=0x08000000 if IS_WIN else 0)
        threading.Thread(target=self._reader, daemon=True).start()
        self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                    "clientInfo": {"name": "Aero", "version": "2.0"}}, timeout=90)
        self.notify("notifications/initialized")
        self.tools = _list_tools(self)

    def _reader(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if "id" in msg and msg["id"] in self.pending:
                box, ev = self.pending.pop(msg["id"])
                box.update(msg)
                ev.set()
        for box, ev in list(self.pending.values()):
            box["error"] = {"message": "server exited"}
            ev.set()

    def _send(self, obj):
        data = (json.dumps(obj) + "\n").encode()
        with self.lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def request(self, method, params, timeout=120):
        with self.lock:
            rid = self.next_id
            self.next_id += 1
        box, ev = {}, threading.Event()
        self.pending[rid] = (box, ev)
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not ev.wait(timeout):
            self.pending.pop(rid, None)
            raise TimeoutError(f"MCP {self.name}: {method} timed out")
        if "error" in box:
            raise RuntimeError(f"MCP {self.name}: {box['error'].get('message')}")
        return box.get("result", {})

    def stop(self):
        if self.alive():
            self.proc.kill()


# ------------------------------------------------------------------------------------------------ Streamable HTTP

class HttpServer:
    """MCP over Streamable HTTP: JSON-RPC POSTs, answered with JSON or a short SSE stream."""

    def __init__(self, name, cfg):
        self.name, self.cfg = name, cfg
        self.url = _expand(cfg["url"])
        self.session = None
        self.next_id = 1
        self.lock = threading.Lock()
        self.tools = []
        self.client = httpx.Client(timeout=httpx.Timeout(600, connect=20), follow_redirects=True)
        self.ok = False

    def alive(self):
        return self.ok

    def _headers(self):
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
             "User-Agent": "Aero/2.0"}
        for k, v in (self.cfg.get("headers") or {}).items():
            h[k] = _expand(v)
        tok = oauth_token(self.name)
        if tok and not any(k.lower() == "authorization" for k in h):
            h["Authorization"] = f"Bearer {tok}"
        if self.session:
            h["Mcp-Session-Id"] = self.session
            h["MCP-Protocol-Version"] = PROTOCOL
        return h

    def _post(self, obj, want_id=None):
        r = self.client.post(self.url, json=obj, headers=self._headers())
        if r.status_code == 401:
            if oauth_refresh(self.name):
                r = self.client.post(self.url, json=obj, headers=self._headers())
            if r.status_code == 401:
                raise NeedsAuth(self.url, r.headers.get("www-authenticate", ""))
        if r.status_code == 404 and self.session and want_id is not None:
            self.session = None                    # session expired: start a new one
            self._initialize()
            r = self.client.post(self.url, json=obj, headers=self._headers())
        if r.status_code >= 400:
            raise RuntimeError(f"MCP {self.name}: HTTP {r.status_code}: {r.text[:300]}")
        sid = r.headers.get("mcp-session-id")
        if sid:
            self.session = sid
        if want_id is None:
            return None
        ctype = r.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            for msg in _sse_messages(r.text):
                if msg.get("id") == want_id:
                    return msg
            raise RuntimeError(f"MCP {self.name}: no response in event stream")
        return r.json() if r.content else {}

    def request(self, method, params, timeout=120):
        with self.lock:
            rid = self.next_id
            self.next_id += 1
        msg = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, want_id=rid)
        if "error" in msg:
            raise RuntimeError(f"MCP {self.name}: {msg['error'].get('message')}")
        return msg.get("result", {})

    def notify(self, method, params=None):
        try:
            self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})
        except NeedsAuth:
            raise
        except Exception:
            pass

    def _initialize(self):
        with self.lock:
            rid = self.next_id
            self.next_id += 1
        msg = self._post({"jsonrpc": "2.0", "id": rid, "method": "initialize",
                          "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                                     "clientInfo": {"name": "Aero", "version": "2.0"}}}, want_id=rid)
        if "error" in msg:
            raise RuntimeError(f"MCP {self.name}: {msg['error'].get('message')}")
        self.notify("notifications/initialized")

    def start(self):
        self._initialize()
        self.ok = True
        self.tools = _list_tools(self)

    def stop(self):
        self.ok = False
        if self.session:
            try:
                self.client.delete(self.url, headers=self._headers(), timeout=5)
            except Exception:
                pass
        self.client.close()


def _sse_messages(text):
    data = []
    for line in text.splitlines() + [""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line.strip() and data:
            try:
                yield json.loads("\n".join(data))
            except Exception:
                pass
            data = []


def _list_tools(srv):
    cursor, out = None, []
    for _ in range(50):
        res = srv.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=90)
        out += res.get("tools", [])
        cursor = res.get("nextCursor")
        if not cursor:
            break
    return out


# ------------------------------------------------------------------------------------------------ OAuth 2.1 (MCP auth spec)

def _vault_key(server):
    return f"mcp_oauth:{server}"


def oauth_token(server):
    d = vault.get_json(_vault_key(server), {})
    return d.get("access_token") if d and (not d.get("expires_at") or d["expires_at"] > time.time() + 30) else None


def oauth_refresh(server):
    d = vault.get_json(_vault_key(server), {})
    if not d or not d.get("refresh_token") or not d.get("token_endpoint"):
        return False
    form = {"grant_type": "refresh_token", "refresh_token": d["refresh_token"], "client_id": d["client_id"]}
    if d.get("resource"):
        form["resource"] = d["resource"]
    if d.get("client_secret"):
        form["client_secret"] = d["client_secret"]
    try:
        r = httpx.post(d["token_endpoint"], data=form, timeout=30, headers={"Accept": "application/json"})
        r.raise_for_status()
        _store_tokens(server, r.json(), d)
        return True
    except Exception:
        return False


def _store_tokens(server, tok, base):
    d = dict(base)
    d["access_token"] = tok["access_token"]
    if tok.get("refresh_token"):
        d["refresh_token"] = tok["refresh_token"]
    d["expires_at"] = time.time() + float(tok["expires_in"]) if tok.get("expires_in") else None
    vault.put_json(_vault_key(server), d)


def _wellknown(base, kind):
    u = urllib.parse.urlsplit(base)
    path = u.path.rstrip("/")
    cands = [f"{u.scheme}://{u.netloc}/.well-known/{kind}{path}"] if path else []
    cands.append(f"{u.scheme}://{u.netloc}/.well-known/{kind}")
    return cands


def oauth_begin(server):
    """Discover the server's authorization server, register Aero as a client and return the sign-in URL."""
    cfg = read_config().get("mcpServers", {}).get(server) or {}
    url = _expand(cfg.get("url") or "")
    if not url:
        raise RuntimeError("Only remote (url) servers use OAuth sign-in.")
    c = httpx.Client(timeout=30, follow_redirects=True, headers={"Accept": "application/json", "User-Agent": "Aero/2.0"})
    # 1. protected resource metadata (from the 401 challenge, else the well-known location)
    meta_url, scope = None, None
    r = c.post(url, json={"jsonrpc": "2.0", "id": 0, "method": "initialize",
                          "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                                     "clientInfo": {"name": "Aero", "version": "2.0"}}},
               headers={"Accept": "application/json, text/event-stream"})
    wa = r.headers.get("www-authenticate", "")
    m = re.search(r'resource_metadata="([^"]+)"', wa)
    if m:
        meta_url = m.group(1)
    m = re.search(r'scope="([^"]+)"', wa)
    if m:
        scope = m.group(1)
    prm = {}
    for cand in ([meta_url] if meta_url else []) + _wellknown(url, "oauth-protected-resource"):
        try:
            rr = c.get(cand)
            if rr.status_code == 200:
                prm = rr.json()
                break
        except Exception:
            continue
    issuer = (prm.get("authorization_servers") or [None])[0]
    if not issuer:
        u = urllib.parse.urlsplit(url)
        issuer = f"{u.scheme}://{u.netloc}"
    resource = prm.get("resource") or url
    # 2. authorization server metadata
    asm = {}
    for cand in _wellknown(issuer, "oauth-authorization-server") + _wellknown(issuer, "openid-configuration"):
        try:
            rr = c.get(cand)
            if rr.status_code == 200 and rr.json().get("authorization_endpoint"):
                asm = rr.json()
                break
        except Exception:
            continue
    if not asm:
        raise RuntimeError("This server does not publish OAuth metadata; add a token header instead.")
    # 3. dynamic client registration (or a client id from the config)
    client_id, client_secret = cfg.get("oauth_client_id"), cfg.get("oauth_client_secret")
    if not client_id:
        if not asm.get("registration_endpoint"):
            raise RuntimeError("The server needs a pre-registered OAuth client id (set oauth_client_id in mcp.json).")
        reg = c.post(asm["registration_endpoint"], json={
            "client_name": "Aero", "redirect_uris": [redirect_uri()], "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"})
        if reg.status_code >= 400:
            raise RuntimeError(f"Client registration failed: {reg.text[:300]}")
        j = reg.json()
        client_id, client_secret = j["client_id"], j.get("client_secret")
    # 4. PKCE + state, remembered until the browser comes back to /oauth/callback
    verifier = base64.urlsafe_b64encode(_rnd.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = _rnd.token_urlsafe(24)
    scope = scope or cfg.get("oauth_scope") or " ".join(prm.get("scopes_supported") or [])
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri(), "code_challenge": challenge,
         "code_challenge_method": "S256", "state": state, "resource": resource}
    if scope:
        q["scope"] = scope
    _pending_auth[state] = {"server": server, "verifier": verifier, "client_id": client_id,
                            "client_secret": client_secret, "token_endpoint": asm["token_endpoint"],
                            "resource": resource, "at": time.time()}
    _status.setdefault(server, {})["state"] = "signing_in"
    return asm["authorization_endpoint"] + ("&" if "?" in asm["authorization_endpoint"] else "?") + urllib.parse.urlencode(q)


def oauth_finish(state, code):
    p = _pending_auth.pop(state, None)
    if not p or time.time() - p["at"] > 900:
        raise RuntimeError("This sign-in link expired. Start again from Settings → Plugins & MCP.")
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
            "client_id": p["client_id"], "code_verifier": p["verifier"], "resource": p["resource"]}
    if p.get("client_secret"):
        form["client_secret"] = p["client_secret"]
    r = httpx.post(p["token_endpoint"], data=form, timeout=30, headers={"Accept": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError(f"Token exchange failed: {r.text[:300]}")
    _store_tokens(p["server"], r.json(), {k: p[k] for k in ("client_id", "client_secret", "token_endpoint", "resource")})
    start_one(p["server"])
    return p["server"]


def oauth_forget(server):
    vault.put(_vault_key(server), "")


# ------------------------------------------------------------------------------------------------ registry glue

def _tool_name(server, tool):
    n = re.sub(r"[^a-zA-Z0-9_]", "_", f"mcp_{server}_{tool}")
    return n[:64]


def _make_fn(server, tool_name):
    def fn(ctx, **args):
        s = _servers.get(server)
        if not s or not s.alive():
            return {"text": f"MCP server '{server}' is not running.", "error": True}
        try:
            res = s.request("tools/call", {"name": tool_name, "arguments": args}, timeout=600)
        except NeedsAuth:
            _status[server] = {"state": "needs_auth"}
            return {"text": f"MCP server '{server}' needs the user to sign in again (Settings → Plugins & MCP).",
                    "error": True}
        texts, image = [], None
        for c in res.get("content", []):
            if c.get("type") == "text":
                texts.append(c.get("text", ""))
            elif c.get("type") == "image" and c.get("data"):
                ext = (c.get("mimeType") or "image/png").split("/")[-1]
                image = attachments.save_image_bytes(base64.b64decode(c["data"]), f"mcp.{ext}")["id"]
            elif c.get("type") == "resource":
                r = c.get("resource", {})
                texts.append(r.get("text") or f"[resource {r.get('uri')}]")
            elif c.get("type") == "resource_link":
                texts.append(f"[{c.get('name') or 'resource'}: {c.get('uri')}]")
        if not texts and res.get("structuredContent") is not None:
            texts.append(json.dumps(res["structuredContent"], ensure_ascii=False)[:60000])
        return {"text": "\n".join(texts) or "(no content)", "image": image, "error": bool(res.get("isError"))}
    return fn


def _register(name, srv):
    for k in [k for k, t in REGISTRY.items() if t.category == "mcp" and getattr(t, "server", None) == name]:
        del REGISTRY[k]
    for t in srv.tools:
        schema = t.get("inputSchema") or {"type": "object", "properties": {}}
        tn = _tool_name(name, t["name"])
        tool = Tool(tn, f"[MCP {name}] {t.get('description') or t.get('title') or t['name']}"[:1024],
                    schema.get("properties", {}), "mcp", _make_fn(name, t["name"]), schema.get("required", []))
        tool.server = name
        REGISTRY[tn] = tool


def start_one(name):
    cfg = read_config().get("mcpServers", {}).get(name)
    old = _servers.pop(name, None)
    if old:
        old.stop()
    for k in [k for k, t in REGISTRY.items() if t.category == "mcp" and getattr(t, "server", None) == name]:
        del REGISTRY[k]
    if not cfg or cfg.get("disabled"):
        _status.pop(name, None)
        return
    kind = "http" if cfg.get("url") else "stdio"
    from .. import localonly
    if localonly.strict() and not cfg.get("offline_ok"):
        _status[name] = {"state": "offline", "kind": kind,
                         "error": "Not started: strict offline mode is on. Add \"offline_ok\": true to this server's "
                                  "entry in mcp.json if it never uses the network."}
        return
    _status[name] = {"state": "starting", "kind": kind}
    try:
        if kind == "http" and (cfg.get("type") or "http").lower() == "sse":
            raise RuntimeError("Legacy SSE servers are not supported; use the server's Streamable HTTP URL.")
        s = HttpServer(name, cfg) if kind == "http" else StdioServer(name, cfg)
        s.start()
        _servers[name] = s
        _register(name, s)
        _status[name] = {"state": "running", "kind": kind, "tools": [t["name"] for t in s.tools],
                         "oauth": bool(oauth_token(name))}
    except NeedsAuth:
        _status[name] = {"state": "needs_auth", "kind": kind}
    except Exception as e:  # noqa: BLE001     (includes OfflineBlocked for remote servers)
        _status[name] = {"state": "error", "kind": kind, "error": str(e)[:500]}


def start_all():
    stop_all()
    names = [n for n, c in read_config().get("mcpServers", {}).items() if not c.get("disabled")]
    threads = [threading.Thread(target=start_one, args=(n,), daemon=True) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(180)


def start_all_async():
    threading.Thread(target=start_all, daemon=True, name="mcp").start()


def stop_all():
    for s in list(_servers.values()):
        try:
            s.stop()
        except Exception:
            pass
    _servers.clear()
    _status.clear()
    for k in [k for k, t in REGISTRY.items() if t.category == "mcp"]:
        del REGISTRY[k]


def status():
    return _status
