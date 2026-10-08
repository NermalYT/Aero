"""Benchmark suite for the loaded model, the router and the UI.

Everything here is measured against the real llama-server Aero is running, through the same OpenAI-compatible
endpoint and chat template the agent uses. A metric the server does not report (for example draft acceptance on a
build without speculative decoding) is recorded as None and shown as "not measured"; nothing is estimated.

Workloads (quick / deep repeats):
  chat         short question, 160 tokens out: time to first token (TTFT), decode tok/s
  prefill      synthetic documents of 2k / 8k / 16k tokens: TTFT and prompt tok/s
  code         rewrite a 60-line file with one rename: decode tok/s and draft acceptance (speculative decoding
               shines on text the model can copy)
  cache        the same long prefix twice with prompt caching on: share of tokens reused, cold vs warm TTFT
  json         extract fields into JSON with given keys: valid JSON rate and exact-field rate
  tools        OpenAI-style tool calls: right tool (or none) with the right key argument
  recall       a fact hidden at several depths of a long filler document: found or not
  vram         nvidia-smi sampled every 0.5 s during the run: idle, mean and peak; llama-server host RAM

Reports go to data/bench/<time>_<model>.json and export as Markdown or JSON.
"""
import json
import math
import re
import statistics
import threading
import time

import httpx

from . import hardware
from .config import DATA

BENCH_DIR = DATA / "bench"
QUICK = {"chat": 3, "prefill": [2048, 8192], "prefill_reps": 2, "code": 2, "cache": 1, "json": 8, "tools": 6,
         "recall_depths": [0.1, 0.5, 0.9], "recall_tokens": 8192}
DEEP = {"chat": 7, "prefill": [2048, 8192, 16384], "prefill_reps": 3, "code": 4, "cache": 3, "json": 12, "tools": 10,
        "recall_depths": [0.05, 0.25, 0.5, 0.75, 0.95], "recall_tokens": 32768}
DECODE_ONLY = {"chat": 4}


class Cancelled(Exception):
    pass


# ---- statistics -----------------------------------------------------------------------------------------

def summary(xs):
    xs = [float(x) for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if not xs:
        return None
    s = sorted(xs)
    mean = statistics.fmean(s)
    p95 = s[min(len(s) - 1, max(0, math.ceil(0.95 * len(s)) - 1))]         # nearest-rank
    cv = (statistics.stdev(s) / mean) if len(s) > 1 and mean else 0.0
    return {"n": len(s), "median": round(statistics.median(s), 2), "p95": round(p95, 2), "mean": round(mean, 2),
            "min": round(s[0], 2), "max": round(s[-1], 2), "cv": round(cv, 3)}


# ---- VRAM / RAM sampler -----------------------------------------------------------------------------------

class Sampler(threading.Thread):
    def __init__(self, pid=None, gpu=True, every=0.5):
        super().__init__(daemon=True)
        self.pid, self.gpu, self.every = pid, gpu, every
        self.vram, self.rss = [], []
        self._halt = threading.Event()

    def run(self):
        proc = None
        try:
            import psutil
            proc = psutil.Process(self.pid) if self.pid else None
        except Exception:
            proc = None
        while not self._halt.is_set():
            if self.gpu:
                v = hardware.gpu_used_mb()
                if v is not None:
                    self.vram.append(v)
            if proc is not None:
                try:
                    self.rss.append(proc.memory_info().rss // 2**20)
                except Exception:
                    proc = None
            self._halt.wait(self.every)

    def stop(self):
        self._halt.set()
        self.join(5)
        out = {}
        if self.vram:
            out.update(vram_peak_mb=max(self.vram), vram_mean_mb=round(statistics.fmean(self.vram)),
                       vram_samples=len(self.vram))
        if self.rss:
            out.update(host_ram_peak_mb=max(self.rss))
        return out


# ---- test data ------------------------------------------------------------------------------------------

_FILLER = ("The pond keeps a steady temperature through the night because the water releases the heat it stored "
           "during the day. Reeds along the bank slow the wind, lily pads shade the shallows, and the frogs move "
           "between the stones as the light changes. Notes on the garden: the pump runs on a timer, the filter is "
           "rinsed every second week, and the fish are fed once in the morning. ").split()


def filler(n_words, salt=0):
    out = []
    for i in range(n_words):
        out.append(_FILLER[(i * 5 + i // 11 + salt) % len(_FILLER)])
        if i % 23 == 22:
            out.append(f"(section {salt}.{i // 23})")
    return " ".join(out)


CODE_FILE = '''import json


def load_inventory(path):
    """Read the inventory file and return a list of items."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = []
    for raw in data.get("items", []):
        item = {
            "sku": raw["sku"],
            "name": raw.get("name", ""),
            "qty": int(raw.get("qty", 0)),
            "price": float(raw.get("price", 0.0)),
        }
        items.append(item)
    return items


def total_value(items):
    """Sum of qty * price over all items."""
    total = 0.0
    for item in items:
        total += item["qty"] * item["price"]
    return round(total, 2)


def low_stock(items, threshold=5):
    """Items whose quantity is below the threshold."""
    return [item for item in items if item["qty"] < threshold]


def restock_plan(items, target=20):
    """How many of each low item to order to reach the target."""
    plan = {}
    for item in low_stock(items):
        plan[item["sku"]] = target - item["qty"]
    return plan


def report(path):
    items = load_inventory(path)
    lines = [f"{len(items)} items, total value {total_value(items)}"]
    for sku, n in restock_plan(items).items():
        lines.append(f"order {n} x {sku}")
    return "\\n".join(lines)


if __name__ == "__main__":
    import sys
    print(report(sys.argv[1]))
'''

JSON_TASKS = [
    ("Order #4821 from Dana Whitfield shipped on 2026-03-14 with 3 items.",
     {"order_id": 4821, "customer": "Dana Whitfield", "items": 3}),
    ("The RTX 5080 has 16 GB of GDDR7 memory and a 360 W board power.",
     {"gpu": "RTX 5080", "vram_gb": 16, "power_w": 360}),
    ("The meeting moved to Thursday at 15:30 in room B214.", {"day": "Thursday", "time": "15:30", "room": "B214"}),
    ("Ticket SEC-118: severity high, assigned to Priya, status open.",
     {"ticket": "SEC-118", "severity": "high", "assignee": "Priya", "status": "open"}),
    ("The router at 192.168.50.1 serves its admin page on port 8443 over HTTPS.",
     {"ip": "192.168.50.1", "port": 8443, "protocol": "HTTPS"}),
    ("Invoice 2209 totals 1250 dollars and is due in 30 days.", {"invoice": 2209, "total": 1250, "due_days": 30}),
    ("Flight UA 917 departs Newark at 08:45 and lands in Denver.",
     {"flight": "UA 917", "from": "Newark", "to": "Denver", "departs": "08:45"}),
    ("Sensor T-7 read 21.5 degrees and 40 percent humidity.", {"sensor": "T-7", "temp_c": 21.5, "humidity": 40}),
    ("Course IST 261 meets on Mondays in room 110 with 42 students.",
     {"course": "IST 261", "day": "Monday", "room": 110, "students": 42}),
    ("The backup job finished at 02:10 and copied 1840 files.", {"job": "backup", "time": "02:10", "files": 1840}),
    ("Package 77-B weighs 2.4 kg and ships to Lisbon.", {"package": "77-B", "weight_kg": 2.4, "city": "Lisbon"}),
    ("Player Nova scored 31 points with 12 assists.", {"player": "Nova", "points": 31, "assists": 12}),
]

TOOL_DEFS = [
    {"type": "function", "function": {"name": "get_weather", "description": "Current weather for a city.",
     "parameters": {"type": "object", "properties": {"city": {"type": "string"},
                                                    "unit": {"type": "string", "enum": ["c", "f"]}},
                    "required": ["city"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a text file from disk.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "set_timer", "description": "Start a countdown timer.",
     "parameters": {"type": "object", "properties": {"minutes": {"type": "integer"}, "label": {"type": "string"}},
                    "required": ["minutes"]}}},
    {"type": "function", "function": {"name": "convert_currency", "description": "Convert an amount between currencies.",
     "parameters": {"type": "object", "properties": {"amount": {"type": "number"}, "from": {"type": "string"},
                                                    "to": {"type": "string"}}, "required": ["amount", "from", "to"]}}},
    {"type": "function", "function": {"name": "search_notes", "description": "Search the user's saved notes.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
]

# (request, expected tool or None, {arg: substring or number that must match})
TOOL_TASKS = [
    ("What's the weather in Pittsburgh right now, in fahrenheit?", "get_weather", {"city": "pittsburgh"}),
    ("Open C:\\Projects\\notes.txt and show me what's in it.", "read_file", {"path": "notes.txt"}),
    ("Set a 25 minute timer called focus.", "set_timer", {"minutes": 25}),
    ("Convert 120 euros to US dollars.", "convert_currency", {"amount": 120}),
    ("Find my notes about DDR5 timings.", "search_notes", {"query": "ddr5"}),
    ("Hi! How's your day going?", None, {}),
    ("Is it raining in Tokyo?", "get_weather", {"city": "tokyo"}),
    ("Remind me in 10 minutes to check the oven.", "set_timer", {"minutes": 10}),
    ("What is 12 times 12?", None, {}),
    ("Show me the file D:\\logs\\build.log", "read_file", {"path": "build.log"}),
]

ROUTER_CASES = [
    ("hi there!", "none", None),
    ("What's on my screen right now?", "any", {"screenshot", "app_view", "app_list"}),
    ("Read C:\\notes\\todo.txt and tell me what's left on it.", "any", {"read_file"}),
    ("Rename every .jpeg file in my Downloads folder to .jpg", "any", {"run_command", "move_path"}),
    ("Search the web for the newest llama.cpp release notes.", "any", {"web_search", "fetch_url"}),
    ("Remember that my router's admin IP is 192.168.50.1", "any", {"remember"}),
    ("What did we decide about the DDR5 timings in an earlier chat?", "any", {"recall"}),
    ("Type 'hello' into the Notepad window.", "any", {"app_type", "app_view", "app_keys"}),
    ("Fix the failing unit test in my repo at C:\\code\\inventory", "any", {"edit_file", "run_command"}),
    ("What's 17 times 23?", "none", None),
    ("Log in to github.com in the browser and open my notifications.", "any", {"browser_open"}),
    ("How much free disk space do I have on C:?", "any", {"run_command"}),
]


def _norm(v):
    return re.sub(r"\s+", " ", str(v)).strip().lower()


def _matches(got, want):
    if isinstance(want, (int, float)) and not isinstance(want, bool):
        try:
            return abs(float(got) - float(want)) < 1e-6
        except (TypeError, ValueError):
            return False
    return _norm(want) == _norm(got) or (_norm(want) in _norm(got) and len(_norm(got)) <= len(_norm(want)) + 6)


def parse_json_reply(text):
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t.strip())
    m = re.search(r"\{.*\}", t, re.S)
    return json.loads(m.group(0) if m else t)


# ---- the runner -----------------------------------------------------------------------------------------

class Bench:
    def __init__(self, url, ctx, settings, emit, cancel, pid=None, gpu=True):
        self.url, self.ctx, self.s, self.emit, self.cancel = url, int(ctx or 4096), settings, emit, cancel
        self.pid, self.gpu = pid, gpu
        self.tokens_per_word = 1.3

    def check(self):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()

    def log(self, text):
        self.emit({"type": "log", "text": text})

    def _body(self, messages, max_tokens, cache=False, think=False, temperature=None, extra=None):
        b = {"messages": messages, "max_tokens": max_tokens, "seed": 7, "cache_prompt": cache,
             "temperature": self.s.get("temperature", 0.6) if temperature is None else temperature,
             "top_p": self.s.get("top_p", 0.95), "top_k": self.s.get("top_k", 20), "min_p": self.s.get("min_p", 0.0),
             "chat_template_kwargs": {"enable_thinking": think}}
        b.update(extra or {})
        return b

    def stream(self, messages, max_tokens, cache=False, think=False, temperature=None):
        """One streamed request. TTFT is the time to the first generated token of any kind (text or reasoning)."""
        self.check()
        body = {**self._body(messages, max_tokens, cache, think, temperature), "stream": True}
        t0 = time.perf_counter()
        first, text, timings = None, [], {}
        with httpx.stream("POST", self.url + "/v1/chat/completions", json=body,
                          timeout=httpx.Timeout(1800, connect=10)) as r:
            if r.status_code != 200:
                raise RuntimeError(f"llama-server answered {r.status_code}: {r.read().decode('utf-8', 'replace')[:300]}")
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    ch = json.loads(data)
                except ValueError:
                    continue
                if ch.get("timings"):
                    timings = ch["timings"]
                for c in ch.get("choices") or []:
                    d = c.get("delta") or {}
                    piece = (d.get("content") or "") + (d.get("reasoning_content") or "")
                    if piece and first is None:
                        first = time.perf_counter()
                    text.append(d.get("content") or "")
                if self.cancel is not None and self.cancel.is_set():
                    raise Cancelled()
        end = time.perf_counter()
        return {"ttft_ms": round((first - t0) * 1000, 1) if first else None, "wall_ms": round((end - t0) * 1000, 1),
                "text": "".join(text), "timings": timings}

    def call(self, messages, max_tokens, tools=None, temperature=0):
        self.check()
        extra = {"tools": tools, "tool_choice": "auto"} if tools else None
        r = httpx.post(self.url + "/v1/chat/completions", timeout=900,
                       json=self._body(messages, max_tokens, False, False, temperature, extra))
        if r.status_code != 200:
            raise RuntimeError(f"llama-server answered {r.status_code}: {r.text[:300]}")
        return r.json()["choices"][0]["message"]

    def calibrate(self):
        """Size test documents in this model's own tokens."""
        sample = filler(600)
        try:
            r = httpx.post(self.url + "/tokenize", json={"content": sample}, timeout=60)
            self.tokens_per_word = len(r.json()["tokens"]) / len(sample.split(" "))
        except Exception:
            pass

    def doc(self, tokens, salt=0):
        return filler(max(50, int(tokens / max(0.2, self.tokens_per_word))), salt)

    @staticmethod
    def _t(res, key):
        v = (res.get("timings") or {}).get(key)
        return float(v) if v is not None else None

    # ---- workloads ----
    def w_chat(self, reps):
        rows = []
        for i in range(reps):
            r = self.stream([{"role": "user", "content": "In three sentences, explain why memory bandwidth limits "
                                                          f"token generation speed on a GPU. (run {i})"}], 160)
            rows.append(r)
        return {"ttft_ms": summary(r["ttft_ms"] for r in rows),
                "decode_tps": summary(self._t(r, "predicted_per_second") for r in rows),
                "tokens_out": summary(self._t(r, "predicted_n") for r in rows)}

    def w_prefill(self, sizes, reps):
        out = {}
        for n in sizes:
            if n > self.ctx - 512:
                out[str(n)] = {"skipped": f"context is {self.ctx:,} tokens"}
                continue
            rows = []
            for i in range(reps):
                prompt = self.doc(n, salt=n + i * 31) + "\n\nSummarize the notes above in one sentence."
                rows.append(self.stream([{"role": "user", "content": prompt}], 24))
            out[str(n)] = {"ttft_ms": summary(r["ttft_ms"] for r in rows),
                           "prefill_tps": summary(self._t(r, "prompt_per_second") for r in rows),
                           "prompt_tokens": summary(self._t(r, "prompt_n") for r in rows)}
        return out

    def w_code(self, reps):
        rows = []
        for i in range(reps):
            msg = ("Rename the function `low_stock` to `items_below` everywhere in this file, including where it is "
                   "called. Reply with the complete updated file in one Python code block and nothing else.\n\n"
                   f"```python\n{CODE_FILE}```")
            rows.append(self.stream([{"role": "user", "content": msg}], 900))
        drafted = [self._t(r, "draft_n") for r in rows]
        accepted = [self._t(r, "draft_n_accepted") for r in rows]
        acc = [a / d for a, d in zip(accepted, drafted) if a is not None and d]
        renamed = sum(1 for r in rows if "def items_below" in r["text"] and "low_stock(" not in r["text"])
        return {"decode_tps": summary(self._t(r, "predicted_per_second") for r in rows),
                "draft_acceptance": summary(acc) if acc else None,
                "draft_tokens": summary(d for d in drafted if d is not None) if any(d is not None for d in drafted) else None,
                "edit_correct": f"{renamed}/{len(rows)}"}

    def w_cache(self, reps):
        size = min(4096, self.ctx - 768)
        rows = []
        for i in range(reps):
            prefix = self.doc(size, salt=900 + i)
            cold = self.stream([{"role": "user", "content": prefix + "\n\nQuestion: how often is the filter rinsed?"}],
                               24, cache=True)
            warm = self.stream([{"role": "user", "content": prefix + "\n\nQuestion: when are the fish fed?"}],
                               24, cache=True)
            reused = self._t(warm, "cache_n")
            fresh = self._t(warm, "prompt_n")
            rows.append({"cold": cold["ttft_ms"], "warm": warm["ttft_ms"],
                         "reuse": (reused / (reused + fresh)) if reused is not None and fresh is not None and reused + fresh else None})
        reuse = [r["reuse"] for r in rows if r["reuse"] is not None]
        return {"prefix_tokens": size, "cold_ttft_ms": summary(r["cold"] for r in rows),
                "warm_ttft_ms": summary(r["warm"] for r in rows),
                "reused_share": summary(reuse) if reuse else None}

    def w_json(self, n):
        valid = exact = 0
        fails = []
        tasks = JSON_TASKS[:n]
        for text, want in tasks:
            prompt = (f"Extract these fields from the text as one JSON object with exactly these keys: "
                      f"{', '.join(want)}. Use numbers for numeric values. Reply with the JSON object only.\n\n"
                      f"Text: {text}")
            try:
                got = parse_json_reply(self.call([{"role": "user", "content": prompt}], 200).get("content"))
                valid += 1
                if all(k in got and _matches(got[k], v) for k, v in want.items()):
                    exact += 1
                else:
                    fails.append(text[:40])
            except Cancelled:
                raise
            except Exception:
                fails.append(text[:40])
        return {"tasks": len(tasks), "valid_json": valid / len(tasks), "exact_fields": exact / len(tasks),
                "missed": fails[:6]}

    def w_tools(self, n):
        right, fails = 0, []
        tasks = TOOL_TASKS[:n]
        for req, tool, args in tasks:
            try:
                m = self.call([{"role": "system", "content": "You are a helpful assistant. Call a tool only when "
                                                              "the request needs one."},
                               {"role": "user", "content": req}], 300, tools=TOOL_DEFS)
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                if not right and not fails and "tool" in str(e).lower():
                    return {"skipped": f"this model's chat template rejected tools: {str(e)[:160]}"}
                fails.append(req[:40])
                continue
            calls = m.get("tool_calls") or []
            if tool is None:
                ok = not calls
            else:
                ok = False
                if calls and calls[0]["function"]["name"] == tool:
                    try:
                        a = json.loads(calls[0]["function"].get("arguments") or "{}")
                    except ValueError:
                        a = {}
                    ok = all(k in a and (_matches(a[k], v) if not isinstance(v, str) else _norm(v) in _norm(a[k]))
                             for k, v in args.items())
            right += ok
            if not ok:
                fails.append(req[:40])
        return {"tasks": len(tasks), "accuracy": right / len(tasks), "missed": fails[:6]}

    def w_recall(self, depths, tokens):
        size = int(min(tokens, (self.ctx - 512) * 0.9))
        if size < 1024:
            return {"skipped": "context too small"}
        found, rows = 0, []
        for i, d in enumerate(depths):
            code = f"{4817 + i * 137}-{'QXRTMV'[i % 6]}{'KLPZ'[i % 4]}"
            needle = f" Important: the maintenance code for the pond pump is {code}. "
            words = self.doc(size, salt=300 + i).split(" ")
            cut = int(len(words) * d)
            text = " ".join(words[:cut]) + needle + " ".join(words[cut:])
            r = self.stream([{"role": "user", "content": text + "\n\nWhat is the maintenance code for the pond pump? "
                                                                  "Reply with the code only."}], 40, temperature=0)
            ok = code.lower() in (r["text"] or "").lower()
            found += ok
            rows.append({"depth": d, "found": ok, "ttft_ms": r["ttft_ms"]})
        return {"haystack_tokens": size, "found": found, "of": len(depths), "rate": found / len(depths), "runs": rows}

    def run(self, depth="quick"):
        plan = {"quick": QUICK, "deep": DEEP, "decode": DECODE_ONLY}[depth]
        sampler = Sampler(self.pid, self.gpu)
        idle = hardware.gpu_used_mb() if self.gpu else None
        self.calibrate()
        res, t0 = {}, time.time()
        steps = [k for k in ("chat", "prefill", "code", "cache", "json", "tools", "recall_depths") if k in plan]
        sampler.start()
        try:
            for i, k in enumerate(steps):
                name = "recall" if k == "recall_depths" else k
                self.emit({"type": "bench_step", "name": name, "i": i + 1, "of": len(steps)})
                try:
                    if k == "chat":
                        res["chat"] = self.w_chat(plan["chat"])
                    elif k == "prefill":
                        res["prefill"] = self.w_prefill(plan["prefill"], plan["prefill_reps"])
                    elif k == "code":
                        res["code"] = self.w_code(plan["code"])
                    elif k == "cache":
                        res["cache"] = self.w_cache(plan["cache"])
                    elif k == "json":
                        res["json"] = self.w_json(plan["json"])
                    elif k == "tools":
                        res["tools"] = self.w_tools(plan["tools"])
                    else:
                        res["recall"] = self.w_recall(plan["recall_depths"], plan["recall_tokens"])
                except Cancelled:
                    raise
                except Exception as e:  # noqa: BLE001
                    res[name] = {"error": f"{type(e).__name__}: {e}"[:300]}
                self.emit({"type": "bench_done_step", "name": name, "result": res.get(name)})
        finally:
            mem = sampler.stop()
        mem["vram_idle_mb"] = idle
        res["memory"] = mem
        res["elapsed_s"] = round(time.time() - t0, 1)
        return res


# ---- router suite -----------------------------------------------------------------------------------------

def router_suite(decide, catalog, emit, cancel=None, reps=1):
    """decide(history, tool_list) -> (decision, info). Cases whose tools are not in the catalog are skipped."""
    names = {c[0] for c in catalog}
    rows, lat = [], []
    for req, kind, want in ROUTER_CASES:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if kind == "any" and not (want & names):
            rows.append({"request": req, "skipped": "tool not available (strict offline or turned off)"})
            continue
        for _ in range(reps):
            try:
                d, info = decide([{"role": "user", "content": req}], catalog)
            except Exception as e:  # noqa: BLE001
                rows.append({"request": req, "ok": False, "error": str(e)[:200]})
                continue
            chosen = set(d.get("tools") or [])
            ok = (not chosen) if kind == "none" else bool(chosen & want)
            lat.append(info.get("ms"))
            rows.append({"request": req, "ok": ok, "chose": sorted(chosen)[:8], "ms": info.get("ms")})
        emit({"type": "bench_step", "name": "router", "i": len(rows), "of": len(ROUTER_CASES) * reps})
    scored = [r for r in rows if "ok" in r and "error" not in r]
    errors = [r for r in rows if "error" in r]
    return {"cases": len(scored), "accuracy": (sum(r["ok"] for r in scored) / len(scored)) if scored else None,
            "errors": len(errors), "first_error": errors[0]["error"] if errors else None,
            "latency_ms": summary(lat), "rows": rows}


# ---- reports -----------------------------------------------------------------------------------------------

def save(report):
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    name = (report.get("router") or {}).get("model") if report.get("kind") == "router" else None
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name or (report.get("model") or {}).get("file") or "model")[:60]
    rid = time.strftime("%Y%m%d-%H%M%S") + "_" + report.get("kind", "bench") + "_" + slug
    report["id"] = rid
    (BENCH_DIR / f"{rid}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return rid


def load(rid):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", rid or ""):
        raise FileNotFoundError(rid)
    return json.loads((BENCH_DIR / f"{rid}.json").read_text(encoding="utf-8"))


def listing(limit=40):
    if not BENCH_DIR.exists():
        return []
    out = []
    for p in sorted(BENCH_DIR.glob("*.json"), reverse=True)[:limit]:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": r.get("id", p.stem), "kind": r.get("kind"), "depth": r.get("depth"), "at": r.get("at"),
                        "model": ((r.get("router") or {}).get("model") if r.get("kind") == "router"
                                  else (r.get("model") or {}).get("file")), "profile": r.get("profile"),
                        "decode_tps": (((r.get("results") or {}).get("chat") or {}).get("decode_tps") or {}).get("median")})
        except (OSError, ValueError):
            continue
    return out


def _fmt(s, unit="", digits=1):
    if not s:
        return "not measured"
    if isinstance(s, dict) and "median" in s:
        return (f"{s['median']:.{digits}f}{unit} (p95 {s['p95']:.{digits}f}, cv {s['cv'] * 100:.1f}%, n={s['n']})")
    return str(s)


def to_markdown(r):
    res = r.get("results") or {}
    rt = r.get("router") or {}
    title = (f"router {rt.get('model') or '?'}" if r.get("kind") == "router"
             else (r.get("model") or {}).get("file") or "?")
    L = [f"# Aero benchmark: {title}", "",
         f"- When: {r.get('at')}", f"- Kind: {r.get('kind')} ({r.get('depth')})",
         f"- Router: {rt.get('model') or '?'} on {rt.get('threads') or '?'} CPU threads" if r.get("kind") == "router"
         else f"- Profile: {r.get('profile') or 'tuner pick'}; config: {r.get('desc') or '?'}",
         f"- llama.cpp: {((r.get('fingerprint') or {}).get('engine') or {}).get('version', '?')}",
         f"- GPU: {((r.get('fingerprint') or {}).get('hardware') or {}).get('gpu') or 'no dedicated GPU detected'}; "
         f"driver {((r.get('fingerprint') or {}).get('hardware') or {}).get('driver') or 'n/a'}",
         f"- Scenery during the run: {r.get('scenery', 'unknown')}", ""]
    if r.get("kind") == "router":
        rs = res.get("router") or {}
        L += ["## Router", "", f"- Accuracy: {rs.get('accuracy', 0) * 100:.0f}% of {rs.get('cases')} cases"
              if rs.get("accuracy") is not None else "- Accuracy: not measured",
              f"- Requests the router could not answer: {rs.get('errors', 0)}"
              + (f" (first error: {rs['first_error'][:160]})" if rs.get("first_error") else ""),
              f"- Latency: {_fmt(rs.get('latency_ms'), ' ms', 0)}", "", "| Request | Result | Tools chosen | ms |",
              "|---|---|---|---|"]
        for row in rs.get("rows", []):
            L.append(f"| {row['request'][:60]} | {'skipped' if 'skipped' in row else ('pass' if row.get('ok') else 'fail')} "
                     f"| {', '.join(row.get('chose', [])) or '-'} | {row.get('ms', '-')} |")
        return "\n".join(L) + "\n"
    if r.get("kind") == "scenery":
        tr = (res.get("full") or {}).get("transparency")
        L += ["## Scenery cost (Full vs Off)", "",
              f"Glass transparency (backdrop blur): {'not recorded' if tr is None else ('on' if tr else 'off')}", "",
              "| Scenery | Decode tok/s | Frame time ms (median / p95) | Long frames |", "|---|---|---|---|"]
        for mode in ("full", "off"):
            x = res.get(mode) or {}
            fr = x.get("frames") or {}
            L.append(f"| {mode} | {_fmt(x.get('decode_tps'))} | {fr.get('median_ms', 'n/a')} / {fr.get('p95_ms', 'n/a')} "
                     f"| {fr.get('long_frames', 'n/a')} |")
        if res.get("delta_pct") is not None:
            L += ["", f"Decode speed with Full scenery is {res['delta_pct']:+.1f}% versus Off."]
        return "\n".join(L) + "\n"
    c = res.get("chat") or {}
    L += ["## Speed", "", "| Workload | Metric | Result |", "|---|---|---|",
          f"| Chat | Time to first token | {_fmt(c.get('ttft_ms'), ' ms', 0)} |",
          f"| Chat | Decode | {_fmt(c.get('decode_tps'), ' tok/s')} |"]
    for n, p in (res.get("prefill") or {}).items():
        if "skipped" in p:
            L.append(f"| Prefill {int(n):,} | - | skipped: {p['skipped']} |")
        else:
            L += [f"| Prefill {int(n):,} | Prompt speed | {_fmt(p.get('prefill_tps'), ' tok/s')} |",
                  f"| Prefill {int(n):,} | Time to first token | {_fmt(p.get('ttft_ms'), ' ms', 0)} |"]
    cd = res.get("code") or {}
    if cd and "error" not in cd:
        L += [f"| Code edit | Decode | {_fmt(cd.get('decode_tps'), ' tok/s')} |",
              f"| Code edit | Draft acceptance | {_fmt(cd.get('draft_acceptance'), '', 2)} |",
              f"| Code edit | Edit correct | {cd.get('edit_correct')} |"]
    ca = res.get("cache") or {}
    if ca and "error" not in ca:
        L += [f"| Prompt cache | Cold TTFT ({ca.get('prefix_tokens')} tok prefix) | {_fmt(ca.get('cold_ttft_ms'), ' ms', 0)} |",
              f"| Prompt cache | Warm TTFT | {_fmt(ca.get('warm_ttft_ms'), ' ms', 0)} |",
              f"| Prompt cache | Tokens reused | {_fmt(ca.get('reused_share'), '', 2)} |"]
    L += ["", "## Accuracy", ""]
    j, t, rc = res.get("json") or {}, res.get("tools") or {}, res.get("recall") or {}
    L.append(f"- JSON: valid {j['valid_json'] * 100:.0f}%, exact fields {j['exact_fields'] * 100:.0f}% of {j['tasks']} tasks"
             if "tasks" in j else f"- JSON: not measured {j.get('error', '')}")
    L.append(f"- Tool calls: {t['accuracy'] * 100:.0f}% of {t['tasks']} tasks" if "tasks" in t
             else f"- Tool calls: not measured ({t.get('skipped') or t.get('error') or 'not run'})")
    L.append(f"- Long-context recall: {rc['found']}/{rc['of']} at {rc['haystack_tokens']:,} tokens" if "found" in rc
             else f"- Long-context recall: not measured ({rc.get('skipped') or rc.get('error') or 'not run'})")
    m = res.get("memory") or {}
    mb = lambda v: f"{v:,} MB" if isinstance(v, (int, float)) else "not measured"     # noqa: E731
    L += ["", "## Memory", "", f"- VRAM idle before the run: {mb(m.get('vram_idle_mb'))}",
          f"- VRAM mean / peak during the run: {mb(m.get('vram_mean_mb'))} / {mb(m.get('vram_peak_mb'))}",
          f"- llama-server host RAM peak: {mb(m.get('host_ram_peak_mb'))}",
          "", f"Run time: {res.get('elapsed_s')} s. Every number above was measured on this PC in this run."]
    return "\n".join(L) + "\n"
