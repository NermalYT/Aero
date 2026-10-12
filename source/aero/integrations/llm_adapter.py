"""A loopback OpenAI-compatible adapter in front of Aero's llama-server, for integrations whose clients force one
specific tool: tool_choice {"type": "function", "function": {"name": X}}. llama-server reads that as "auto" (measured
with Hindsight's reflect: the model answered in prose and the call failed), so the adapter forwards the request with
tool_choice "required" and only tool X offered, which llama-server enforces with a grammar. Everything else passes
through unchanged, streaming included.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

_srv = {"httpd": None, "target": None}
_lock = threading.Lock()


def rewrite(body):
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name") or choice.get("name")
        tools = [t for t in body.get("tools") or [] if (t.get("function") or {}).get("name") == name]
        if name and tools:
            return {**body, "tools": tools, "tool_choice": "required"}
    return body


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _forward(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if method == "POST" and self.path.split("?")[0].rstrip("/").endswith("/chat/completions"):
            try:
                raw = json.dumps(rewrite(json.loads(raw or b"{}"))).encode()
            except ValueError:
                pass
        try:
            with httpx.Client(timeout=None) as c, c.stream(method, _srv["target"] + self.path, content=raw or None,
                                                          headers={"Content-Type": "application/json"}) as r:
                self.send_response(r.status_code)
                for k in ("content-type", "cache-control"):
                    if k in r.headers:
                        self.send_header(k, r.headers[k])
                self.send_header("Connection", "close")
                self.end_headers()
                for chunk in r.iter_raw():
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except httpx.HTTPError as e:
            b = json.dumps({"error": {"message": f"Aero's model server is unreachable: {e}"}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def do_GET(self):
        self._forward("GET")

    def do_POST(self):
        self._forward("POST")


def url(base):
    """The adapter's base URL (…/v1) in front of base (Aero's model server, http://127.0.0.1:PORT/v1). Started on
    first use, bound to 127.0.0.1 on a free port, lives as long as Aero."""
    with _lock:
        if _srv["httpd"] is None:
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            httpd.daemon_threads = True
            threading.Thread(target=httpd.serve_forever, daemon=True, name="aero-llm-adapter").start()
            _srv["httpd"] = httpd
        _srv["target"] = base.rstrip("/").removesuffix("/v1")
        return f"http://127.0.0.1:{_srv['httpd'].server_address[1]}/v1"
