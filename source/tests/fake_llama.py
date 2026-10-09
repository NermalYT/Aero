"""A scripted stand-in for llama-server's OpenAI-compatible chat API, for tests and screenshots. It plays one task:
the agent hands the photo search to a subagent, the subagent lists a folder and reports, then the agent opens
Notepad (an action that counts as controlling the PC) and answers. Title requests get a title and an agent name.

    python tests/fake_llama.py 8291 [folder]     # serve it on a port (the folder the subagent lists)

Nothing here is a model: replies are picked from the conversation's shape, not generated.
"""
import json
import os
import sys
import time

SUB_NAME = "Photo Scout"
TITLE = {"title": "Rename Desktop photos to .jpg", "agent": "Photo Renamer"}


def _last_tool(msgs):
    for m in reversed(msgs):
        if m.get("role") == "tool":
            return m
        if m.get("role") == "user":
            return None
    return None


def _tool_names(msgs):
    """Names of the tool calls made after the last user message, oldest first."""
    names, ids = [], {}
    for m in msgs:
        if m.get("role") == "user":
            names, ids = [], {}
        for tc in m.get("tool_calls") or []:
            ids[tc.get("id")] = tc["function"]["name"]
        if m.get("role") == "tool":
            names.append(ids.get(m.get("tool_call_id"), m.get("name") or ""))
    return names


def script(body, folder=None):
    """The next reply as (reasoning, content, tool_calls [(name, args)], delay_s)."""
    msgs = body.get("messages") or []
    system = msgs[0].get("content") if msgs and msgs[0].get("role") == "system" else ""
    done = _tool_names(msgs)
    folder = folder or os.environ.get("FAKE_LLAMA_FOLDER") or "."
    if isinstance(system, str) and "Your role: subagent" in system:
        if not done:
            return "I need the folder listing first.", "", [("list_dir", {"path": folder})], 0
        listing = (_last_tool(msgs) or {}).get("content") or ""
        photos = [w.strip(" -*|,") for w in listing.split() if w.lower().strip(" -*|,").endswith(".jpeg")]
        found = ", ".join(sorted(set(photos))) or "none"
        return "", (f"Found {len(set(photos))} .jpeg photos: {found}. I only listed them; nothing was renamed."), [], 0
    if isinstance(system, str) and "You are talking to the user as subagent" in system:
        return "", ("I listed them earlier: beach.jpeg, dog.jpeg and sunset.jpeg. I only listed them, so nothing "
                    "was renamed. Want me to check their sizes?"), [], 0
    if not done:
        return ("The search is a separate part, so a subagent can do it.", "I'll have a subagent find the photos first.",
                [("run_subagent", {"name": SUB_NAME, "task": f"List every .jpeg file directly in {folder} (no subfolders) "
                                   "and report their names. Don't rename anything."})], 0)
    if done[-1] == "run_subagent":
        return "", "Opening Notepad for the list.", [("open_app", {"target": "notepad"})], 0
    return "", ("Done. Photo Scout found the .jpeg photos and Notepad is open for the list. Say the word and I'll "
                "rename them to .jpg."), [], float(os.environ.get("FAKE_LLAMA_FINAL_DELAY") or 0)


def chunks(body, folder=None):
    """The streamed reply as SSE lines (bytes), shaped like llama-server's."""
    reasoning, content, calls, delay = script(body, folder)
    out = []

    def emit(delta, finish=None, **extra):
        out.append("data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}) + "\n\n")
    if reasoning:
        emit({"reasoning_content": reasoning})
    for i in range(0, len(content), 12):
        emit({"content": content[i:i + 12]})
    for i, (name, args) in enumerate(calls):
        emit({"tool_calls": [{"index": i, "id": f"call_{name}_{int(time.time() * 1000) % 100000}_{i}", "type": "function",
                              "function": {"name": name, "arguments": ""}}]})
        emit({"tool_calls": [{"index": i, "function": {"arguments": json.dumps(args)}}]})
    emit({}, "tool_calls" if calls else "stop",
         usage={"prompt_tokens": 900, "completion_tokens": 60, "total_tokens": 960},
         timings={"predicted_per_second": 61.5, "prompt_per_second": 1450.0, "prompt_n": 900, "predicted_n": 60})
    out.append("data: [DONE]\n\n")
    return [x.encode() for x in out], delay


def title_reply():
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(TITLE)}, "finish_reason": "stop"}]}


def mock_handler(folder=None):
    """An httpx.MockTransport handler (for AsyncClient) that answers like the scripted server."""
    import httpx

    async def handle(request):
        body = json.loads(request.content or b"{}")
        if not body.get("stream"):
            return httpx.Response(200, json=title_reply())
        parts, _ = chunks(body, folder)
        return httpx.Response(200, content=b"".join(parts), headers={"content-type": "text/event-stream"})
    return handle


def serve(port, folder=None):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/health"):
                return self._json({"status": "ok"})
            if self.path.startswith("/v1/models"):
                return self._json({"data": [{"id": "fake"}]})
            return self._json({"error": "not found"}, 404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            if not body.get("stream"):
                return self._json(title_reply())
            parts, delay = chunks(body, folder)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            if delay:
                time.sleep(delay)
            for p in parts:
                self.wfile.write(p)
                self.wfile.flush()
                time.sleep(0.03)

    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    serve(int(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else None)
