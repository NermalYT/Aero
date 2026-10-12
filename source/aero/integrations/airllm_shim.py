"""AirLLM behind an OpenAI-compatible server, so Aero's agent loop talks to it exactly like llama-server.

Runs inside AirLLM's own virtual environment (data/toolchains/airllm/venv: torch, transformers, airllm), never in
Aero's process. Standard library + what that environment has; it imports nothing from Aero.

    python airllm_shim.py --model <hf repo or folder> --port 8181 --shards <dir> [--compression none|4bit|8bit]
                          [--max-seq 4096] [--hf-home <dir>]

Endpoints: GET /health, /v1/models, /props, /slots, /metrics; POST /v1/chat/completions (stream or not) and
/completion (llama.cpp style, for Aero's quick checks). Tool calls: the model's chat template renders the tools;
a reply in <tool_call>{...}</tool_call> form is returned as content and Aero's agent loop parses it.
"""
import argparse
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"model": None, "tok": None, "ready": False, "error": None, "name": "", "max_seq": 4096, "lock": threading.Lock()}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def load(args):
    try:
        if args.hf_home:
            os.environ["HF_HOME"] = args.hf_home
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        from airllm import AutoModel
        log(f"loading {args.model} (layer shards in {args.shards}, compression {args.compression})")
        kw = {"layer_shards_saving_path": args.shards, "max_seq_len": args.max_seq}
        if args.compression in ("4bit", "8bit"):
            kw["compression"] = args.compression
        else:
            kw["prefetching"] = True
        m = AutoModel.from_pretrained(args.model, **kw)
        STATE.update(model=m, tok=m.tokenizer, ready=True, name=os.path.basename(args.model.rstrip("/\\")),
                     max_seq=args.max_seq)
        log("ready")
    except Exception as e:  # noqa: BLE001
        STATE["error"] = f"{type(e).__name__}: {e}"
        log("load failed:", STATE["error"])
        traceback.print_exc()


def render(messages, tools=None):
    tok = STATE["tok"]
    msgs = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            c = "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
        mm = {"role": m.get("role", "user"), "content": c or ""}
        if m.get("tool_calls"):
            mm["tool_calls"] = m["tool_calls"]
        if m.get("role") == "tool":
            mm["name"] = m.get("name")
        msgs.append(mm)
    try:
        return tok.apply_chat_template(msgs, tools=tools or None, add_generation_prompt=True, tokenize=False)
    except Exception:  # noqa: BLE001  (templates without tool support)
        return tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)


def generate(prompt, max_new, temperature, on_text=None):
    """Greedy (or sampled) generation; on_text(piece) streams pieces. Returns (text, prompt_n, gen_n, seconds)."""
    import torch
    tok, m = STATE["tok"], STATE["model"]
    ids = tok([prompt], return_tensors="pt", return_attention_mask=False, truncation=True,
              max_length=STATE["max_seq"], padding=False)["input_ids"]
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    ids = ids.to(dev)
    t0 = time.time()
    kw = {"max_new_tokens": max_new, "use_cache": True, "return_dict_in_generate": True}
    if temperature and temperature > 0:
        kw.update(do_sample=True, temperature=float(temperature))
    streamer = None
    if on_text is not None:
        from transformers import TextIteratorStreamer
        streamer = TextIteratorStreamer(tok, skip_prompt=True, skip_special_tokens=True)
        kw["streamer"] = streamer
    box = {}

    def run():
        try:
            box["out"] = m.generate(ids, **kw)
        except Exception as e:  # noqa: BLE001
            box["err"] = e
    th = threading.Thread(target=run, daemon=True)
    th.start()
    text = ""
    if streamer is not None:
        for piece in streamer:
            text += piece
            on_text(piece)
    th.join()
    if "err" in box:
        raise box["err"]
    seq = box["out"].sequences[0][ids.shape[1]:]
    if streamer is None:
        text = tok.decode(seq, skip_special_tokens=True)
    return text, int(ids.shape[1]), int(seq.shape[0]), time.time() - t0


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/health":
            if STATE["ready"]:
                return self._json(200, {"status": "ok"})
            return self._json(503, {"status": "error" if STATE["error"] else "loading", "error": STATE["error"]})
        if p == "/v1/models":
            return self._json(200, {"data": [{"id": STATE["name"] or "airllm", "object": "model"}]})
        if p == "/props":
            return self._json(200, {"default_generation_settings": {"n_ctx": STATE["max_seq"]}, "engine": "airllm"})
        if p == "/slots":
            return self._json(200, [])
        if p == "/metrics":
            b = b""
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return self.wfile.write(b)
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json(400, {"error": {"message": "bad JSON"}})
        if not STATE["ready"]:
            return self._json(503, {"error": {"message": STATE["error"] or "AirLLM is still loading"}})
        p = self.path.split("?")[0]
        max_new = int(body.get("max_tokens") or body.get("n_predict") or 512)
        temp = body.get("temperature") or 0
        if p == "/completion":
            with STATE["lock"]:
                text, pn, gn, sec = generate(body.get("prompt", ""), max_new, temp)
            return self._json(200, {"content": text, "timings": {"prompt_n": pn, "predicted_n": gn,
                                                                 "predicted_per_second": gn / max(sec, 1e-6),
                                                                 "prompt_per_second": 0}})
        if p != "/v1/chat/completions":
            return self._json(404, {"error": {"message": "not found"}})
        prompt = render(body.get("messages") or [], body.get("tools"))
        if not body.get("stream"):
            with STATE["lock"]:
                text, pn, gn, sec = generate(prompt, max_new, temp)
            return self._json(200, {"choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                                 "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": pn, "completion_tokens": gn},
                                    "timings": {"prompt_n": pn, "predicted_n": gn,
                                                "predicted_per_second": gn / max(sec, 1e-6)}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def send(obj):
            self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
            self.wfile.flush()
        try:
            with STATE["lock"]:
                text, pn, gn, sec = generate(prompt, max_new, temp,
                                             on_text=lambda s: send({"choices": [{"index": 0, "delta": {"content": s},
                                                                                  "finish_reason": None}]}))
            send({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": pn, "completion_tokens": gn},
                  "timings": {"prompt_n": pn, "predicted_n": gn, "predicted_per_second": gn / max(sec, 1e-6),
                              "prompt_per_second": 0, "cache_n": 0}})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--shards", required=True)
    ap.add_argument("--compression", default="none")
    ap.add_argument("--max-seq", type=int, default=4096)
    ap.add_argument("--hf-home", default="")
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    threading.Thread(target=load, args=(a,), daemon=True).start()
    log(f"listening on 127.0.0.1:{a.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
