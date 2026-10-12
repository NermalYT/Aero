"""A loopback OpenAI-compatible adapter in front of Aero's llama-server, for integrations whose clients force a tool
call: tool_choice {"type": "function", "function": {"name": X}} or "required". llama-server reads a named choice as
"auto" and doesn't enforce "required" either (measured on b11501: it only blocks the end of the turn, so the model
rambles; with Hindsight's reflect the call failed). So the adapter answers those requests itself: one grammar-forced
call (forced_call.py: a json_schema response that can only be one of the allowed tools with valid arguments), sent
back as an ordinary tool_calls response, streamed or not. If that fails, the request goes through with
tool_choice "required" and only the named tool, as before. Everything else passes through unchanged, streaming
included.
"""
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from .. import forced_call

_srv = {"httpd": None, "target": None}
_lock = threading.Lock()


def rewrite(body):
    """Fallback: only the named tool, tool_choice "required"."""
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name") or choice.get("name")
        tools = [t for t in body.get("tools") or [] if (t.get("function") or {}).get("name") == name]
        if name and tools:
            return {**body, "tools": tools, "tool_choice": "required"}
    return body


def forced_tools(body):
    """The tools a request insists on: the named one, or all of them for "required". None = not forced."""
    choice, tools = body.get("tool_choice"), body.get("tools") or []
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name") or choice.get("name")
        pick = [t for t in tools if (t.get("function") or {}).get("name") == name]
        return pick or None
    if choice == "required" and tools:
        return tools
    return None


def answer(body, call):
    """An OpenAI chat.completion (or its SSE chunks when body asks to stream) carrying one tool call."""
    name, args = call
    cid, now = "chatcmpl-" + uuid.uuid4().hex[:12], int(time.time())
    tc = {"id": "call_" + uuid.uuid4().hex[:12], "type": "function",
          "function": {"name": name, "arguments": json.dumps(args)}}
    model = body.get("model") or "aero"
    if not body.get("stream"):
        return "application/json", json.dumps({
            "id": cid, "object": "chat.completion", "created": now, "model": model,
            "choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": None, "tool_calls": [tc]}}]}).encode()

    def chunk(delta, finish=None):
        return "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": now, "model": model,
                                      "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"
    sse = chunk({"role": "assistant", "content": None, "tool_calls": [{**tc, "index": 0}]}) + \
        chunk({}, "tool_calls") + "data: [DONE]\n\n"
    return "text/event-stream", sse.encode()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, ctype, data):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _forward(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if method == "POST" and self.path.split("?")[0].rstrip("/").endswith("/chat/completions"):
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = None
            pick = forced_tools(body) if isinstance(body, dict) else None
            if pick:
                call = forced_call.ask(_srv["target"], body.get("messages") or [], pick,
                                       float(body.get("temperature") if body.get("temperature") is not None else 0.2))
                if call:
                    try:
                        return self._send(200, *answer(body, call))
                    except (BrokenPipeError, ConnectionResetError):
                        return
            if isinstance(body, dict):
                raw = json.dumps(rewrite(body)).encode()
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
            self._send(502, "application/json",
                       json.dumps({"error": {"message": f"Aero's model server is unreachable: {e}"}}).encode())
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
