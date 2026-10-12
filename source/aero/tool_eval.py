"""Tool-calling exam, run by the tuner on a loaded model (and from the Performance Lab).

Two parts:
  bfcl     40 cases from BFCL v4 (Berkeley Function Calling Leaderboard, Apache-2.0; tool_eval/bfcl_sample.json):
           real-world requests with one function, several to choose from, several calls at once, and requests where
           no function fits. Graded the BFCL way: the right function, every required argument, values among the
           accepted answers; for "irrelevance", no call at all.
  aero     12 requests a user types into Aero, with Aero's own tool definitions: does the model infer the right tool
           and target ("look at my GitHub notifications" -> open the browser on github.com, "what's in my Downloads"
           -> list that folder, "hello" -> no tool)?

Each case goes to llama-server's /v1/chat/completions with tools, exactly like Aero's agent does. A call the server
returned as a structured tool call counts as "structured"; one the model printed as text (toolcall_text) still counts
as correct but is reported, because it means Aero has to repair it.
"""
import json
import re
import time
from pathlib import Path

import httpx

from . import toolcall_text

DATA = Path(__file__).with_name("tool_eval") / "bfcl_sample.json"

# (request, accepted tool names, check on the arguments or None, the tools Aero offers for it)
AERO_CASES = [
    ("Look at my GitHub notifications and open pull requests and tell me what needs my attention.",
     {"browser_open"}, lambda a: "github.com" in str(a.get("url", "")).lower()),
    ("Check my emails and look for any meetings I have this week.",
     {"browser_open", "use_skill"}, lambda a: any(w in json.dumps(a).lower() for w in ("mail", "outlook", "meeting"))),
    ("What files are in my Downloads folder?", {"list_dir", "find_files"},
     lambda a: "download" in json.dumps(a).lower()),
    ("Open Spotify.", {"app_launch", "app_find", "open_app"}, lambda a: "spotify" in json.dumps(a).lower()),
    ("What's the weather in Paris tomorrow?", {"web_search", "fetch_url", "browser_open"},
     lambda a: "paris" in json.dumps(a).lower()),
    (r"Read C:\notes\todo.txt and summarize it.", {"read_file"}, lambda a: "todo.txt" in json.dumps(a).lower()),
    ("Run ipconfig and tell me my IP address.", {"run_command"}, lambda a: "ipconfig" in json.dumps(a).lower()),
    ("Remember that my dog is called Biscuit.", {"remember"}, lambda a: "biscuit" in json.dumps(a).lower()),
    ("Make an image of a red fox in the snow.", {"image_generate"}, lambda a: "fox" in json.dumps(a).lower()),
    ("Find every PDF on my desktop.", {"find_files", "list_dir"}, lambda a: "pdf" in json.dumps(a).lower()),
    ("Hi! How are you today?", set(), None),
    ("What is 17 times 23?", set(), None),
]
AERO_TOOLS = ["browser_open", "browser_read_sections", "web_search", "fetch_url", "list_dir", "read_file", "find_files",
              "write_file", "run_command", "app_find", "app_launch", "remember", "recall", "image_generate", "use_skill",
              "ask_user"]


def load_bfcl():
    try:
        return json.loads(DATA.read_text(encoding="utf-8"))["cases"]
    except (OSError, ValueError, KeyError):
        return []


# ---- grading ----------------------------------------------------------------------------------------------------

def _norm(v):
    if isinstance(v, str):
        return re.sub(r"[\s.,;:!?'\"]+$", "", v.strip()).casefold()
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    return v


def value_ok(val, allowed):
    """BFCL possible answers: a list of accepted values; "" means the argument may be left out (checked by caller);
    a dict answer has its own lists of accepted values per key."""
    for opt in allowed:
        if isinstance(opt, dict) and isinstance(val, dict):
            if all((k in val and value_ok(val[k], o)) or ("" in o and k not in val) for k, o in opt.items()):
                return True
        elif isinstance(opt, list) and isinstance(val, list):
            if len(opt) == len(val) and all(_norm(a) == _norm(b) for a, b in zip(opt, val)):
                return True
        elif opt != "" and _norm(opt) == _norm(val):
            return True
        elif isinstance(opt, str) and isinstance(val, str) and opt and _norm(opt) in _norm(val) and len(opt) > 6:
            return True                                     # "Berkeley, CA" vs "Berkeley, CA, USA"
    return False


def call_ok(call, expected):
    """call: (name, args); expected: {name: {param: [accepted values]}}."""
    (name, params), = expected.items()
    got_name, args = call
    if got_name != name:
        return False
    for k, allowed in params.items():
        if k not in args:
            if "" in allowed:
                continue
            return False
        if not value_ok(args[k], allowed):
            return False
    return True


def grade_bfcl(case, calls):
    if case["category"] == "irrelevance":
        return not calls
    exp = case["answer"]
    if len(calls) != len(exp):
        return False
    left = list(calls)
    for e in exp:
        hit = next((c for c in left if call_ok(c, e)), None)
        if hit is None:
            return False
        left.remove(hit)
    return True


def grade_aero(case, calls):
    _req, names, check = case
    if not names:
        return not calls
    return any(n in names and (check is None or check(a)) for n, a in calls)


# ---- running ----------------------------------------------------------------------------------------------------

def ask(url, messages, tools, temperature=0.2, think=False, timeout=300):
    """One request the way the agent sends it. Returns (calls, structured?, ms)."""
    t0 = time.time()
    body = {"messages": messages, "tools": tools, "tool_choice": "auto", "temperature": temperature,
            "max_tokens": 400, "stream": False, "parallel_tool_calls": True,
            "chat_template_kwargs": {"enable_thinking": bool(think)}}
    r = httpx.post(url.rstrip("/") + "/v1/chat/completions", json=body, timeout=timeout)
    ms = round((time.time() - t0) * 1000)
    if r.status_code != 200:
        return [], False, ms
    msg = (r.json().get("choices") or [{}])[0].get("message") or {}
    calls = []
    for tc in msg.get("tool_calls") or []:
        f = tc.get("function") or {}
        try:
            a = json.loads(f.get("arguments") or "{}")
        except ValueError:
            a = {}
        calls.append((f.get("name", ""), a if isinstance(a, dict) else {}))
    if calls:
        return calls, True, ms
    names = {t["function"]["name"] for t in tools}
    found, _ = toolcall_text.parse(msg.get("content") or "", names)
    return [(n, a) for n, a in found], False, ms


def run(url, aero_tools, temperature=0.2, think=False, limit=None, cancel=None, emit=None):
    """The whole exam against llama-server at url. aero_tools: Aero's tool schemas (for the aero part).
    Returns {score, bfcl, aero, by_category, structured, text_calls, ms_per_case, n, failures}."""
    bfcl = load_bfcl()
    if limit:
        step = max(1, len(bfcl) // max(1, limit - len(AERO_CASES)))
        bfcl = bfcl[::step]
    offered = [t for t in aero_tools if t["function"]["name"] in AERO_TOOLS]
    sysmsg = {"role": "system", "content": "You are Aero, an assistant on the user's Windows PC with tools for files, "
              "the shell, apps, a web browser (the user may be signed in to websites there) and the web. Use a tool "
              "when the request needs one; answer directly when it doesn't."}
    res = {"bfcl_ok": 0, "bfcl_n": 0, "aero_ok": 0, "aero_n": 0, "by_category": {}, "structured": 0, "text_calls": 0,
           "ms": [], "failures": []}

    def tally(cat, ok, structured, calls, ms, ident):
        b = res["by_category"].setdefault(cat, [0, 0])
        b[0] += int(ok)
        b[1] += 1
        res["ms"].append(ms)
        if calls:
            res["structured" if structured else "text_calls"] += 1
        if not ok and len(res["failures"]) < 12:
            res["failures"].append({"case": ident, "got": [n for n, _ in calls][:3]})
        if emit:
            emit({"type": "tool_eval_case", "case": ident, "ok": ok, "ms": ms})

    for c in bfcl:
        if cancel is not None and cancel.is_set():
            break
        calls, structured, ms = ask(url, c["messages"], c["tools"], temperature, think)
        ok = grade_bfcl(c, calls)
        res["bfcl_ok"] += int(ok)
        res["bfcl_n"] += 1
        tally(c["category"], ok, structured, calls, ms, c["id"])
    for case in AERO_CASES:
        if cancel is not None and cancel.is_set():
            break
        calls, structured, ms = ask(url, [sysmsg, {"role": "user", "content": case[0]}], offered, temperature, think)
        ok = grade_aero(case, calls)
        res["aero_ok"] += int(ok)
        res["aero_n"] += 1
        tally("aero" if case[1] else "aero_no_tool", ok, structured, calls, ms, case[0][:48])
    n = res["bfcl_n"] + res["aero_n"]
    called = res["structured"] + res["text_calls"]
    return {"score": round((res["bfcl_ok"] + res["aero_ok"]) / max(1, n), 3), "n": n,
            "bfcl": round(res["bfcl_ok"] / max(1, res["bfcl_n"]), 3), "aero": round(res["aero_ok"] / max(1, res["aero_n"]), 3),
            "by_category": {k: f"{a}/{b}" for k, (a, b) in res["by_category"].items()},
            "structured_rate": round(res["structured"] / max(1, called), 3), "text_calls": res["text_calls"],
            "ms_per_case": round(sum(res["ms"]) / max(1, len(res["ms"]))), "temperature": temperature,
            "think": bool(think), "failures": res["failures"]}
