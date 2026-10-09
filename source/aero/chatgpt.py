"""ChatGPT in the cloud, reached one of two legitimate ways (Settings → ChatGPT → Connection):
- "api": the official OpenAI API with the user's own API key (pay as you go), through the Responses API.
- "plan": the user's ChatGPT plan (Plus / Pro / Business), through the official, unmodified Codex CLI signed in with
  OpenAI's own login (`codex login` opens chatgpt.com in the browser). Aero never reads, copies or stores the Codex
  login (~/.codex/auth.json). It only runs `codex login status`, and keeps nothing from it but "signed in with
  ChatGPT", "signed in with an API key" or "not signed in". "auto" uses the API key when one is saved, otherwise
  the plan sign-in.

- GPT-6 Astra makes the finalization pass: it reviews the local model's finished work with read-only tools and
  returns the same verdict Claude Fable does (ok / minor / major, issues, plan).
- GPT-6.1 Sol does the repairs when Astra finds major problems: it gets Astra's plan and works on the PC. Through
  the API it uses Aero's own tools under the same approval rules as every other model. Through the plan it is
  Codex working in your work folder with Codex's workspace-write sandbox, which you approve once per repair.

When Claude review is on too, this whole pass runs first and Claude then reviews the result with Astra's verdicts
and Sol's work in its packet (pipeline.py). Spending is tracked with Claude's in data/cloud_usage.json under the
model names, and capped by Settings → ChatGPT → daily budget.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import agent, cloud, osinfo, vault
from .config import IS_WIN

KEY_NAME = "openai_api_key"
_NO_WINDOW = 0x08000000 if IS_WIN else 0

# USD per million tokens: input, output, cached input. Prompts over 272K input tokens cost 2x input and cache
# and 1.5x output for the whole request (developers.openai.com/api/docs/models, 2026-10-08).
PRICES = {
    "gpt-6-astra": (10.0, 50.0, 1.00),
    "gpt-6.1-sol": (2.0, 10.0, 0.10),
}
LONG_PROMPT = 272_000
NAMES = {"gpt-6-astra": "GPT-6 Astra", "gpt-6.1-sol": "GPT-6.1 Sol"}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

_clients = {}
_no_summary = set()         # models whose requests rejected reasoning summaries (unverified organisation)


def name_of(model):
    return NAMES.get(model, model)


# ------------------------------------------------------------------------------------------------ key & client

def has_key():
    return bool(vault.get(KEY_NAME))


def key_status():
    k = vault.get(KEY_NAME)
    return {"set": bool(k), "masked": vault.mask(k) if k else ""}


def set_key(value):
    vault.put(KEY_NAME, (value or "").strip())
    _clients.clear()


def client():
    import openai
    k = vault.get(KEY_NAME)
    if not k:
        raise RuntimeError("No OpenAI API key. Add one in Settings → ChatGPT.")
    if k not in _clients:
        _clients.clear()
        _clients[k] = openai.AsyncOpenAI(api_key=k, max_retries=3, timeout=900)
    return _clients[k]


async def test_key():
    """Cheapest authenticated call: list models. Returns the model ids this key can use."""
    page = await client().models.list()
    return sorted(m.id for m in page.data)


# ------------------------------------------------------------------------------------------------ spend

def cost_of(model, inp, out, cached=0):
    p = PRICES.get(model) or PRICES["gpt-6-astra"]
    long_mult = (2.0, 1.5) if inp > LONG_PROMPT else (1.0, 1.0)
    return ((inp - cached) * p[0] * long_mult[0] + cached * p[2] * long_mult[0] + out * p[1] * long_mult[1]) / 1e6


def spent_today():
    return cloud.spent_today(prefix="gpt")


def budget_ok(settings):
    if backend(settings) == "plan":
        return True                       # the plan has its own limits; nothing is billed per call
    cap = float(settings.get("openai_daily_budget_usd") or 0)
    return cap <= 0 or spent_today() < cap


def backend(settings):
    """Which connection to use for this request: 'api', 'plan' or None (nothing set up)."""
    want = settings.get("chatgpt_backend") or "auto"
    if want == "api":
        return "api" if has_key() else None
    if want == "plan":
        return "plan" if cli_path(settings) else None
    if has_key():
        return "api"
    if cli_path(settings) and settings.get("chatgpt_plan_signed_in"):
        return "plan"
    return None


def not_ready_text(settings):
    want = settings.get("chatgpt_backend") or "auto"
    if want == "plan":
        return ("ChatGPT review is on, but the Codex CLI is not installed. Install it (Settings → ChatGPT shows how), "
                "then sign in with your ChatGPT account.")
    if want == "api":
        return "ChatGPT review is on, but no OpenAI API key is saved. Add one in Settings → ChatGPT."
    return ("ChatGPT review is on, but ChatGPT is not connected. Sign in with your ChatGPT plan or add an OpenAI API "
            "key in Settings → ChatGPT.")


# ------------------------------------------------------------------------------------------------ prompts

REVIEW_SYSTEM = (cloud.REVIEW_SYSTEM
                 .replace("You are Claude Fable 5.1, the reviewer in Aero", "You are GPT-6 Astra, the reviewer in Aero")
                 .replace("a stronger model (Claude Opus 5.5) will execute", "a stronger model (GPT-6.1 Sol) will execute"))
EXECUTE_SYSTEM = (cloud.EXECUTE_SYSTEM
                  .replace("You are Claude Opus 5.5, working inside Aero", "You are GPT-6.1 Sol, working inside Aero")
                  .replace("a reviewer (Claude Fable 5.1) found major problems", "a reviewer (GPT-6 Astra) found major problems"))


def _review_packet(turn, round_no, previous):
    blocks = cloud.review_packet(turn, round_no, previous)
    return [dict(b) for b in blocks]


def _execute_packet(turn, rv):
    blocks = cloud.execute_packet(turn, rv)
    return [{**b, "text": b["text"].replace("# Claude Fable 5.1's review", "# GPT-6 Astra's review")}
            if b.get("type") == "text" else b for b in blocks]


def _to_input(blocks):
    """Anthropic-style content blocks (text, base64 image) → Responses API input content."""
    out = []
    for b in blocks:
        if b.get("type") == "text":
            out.append({"type": "input_text", "text": b["text"]})
        elif b.get("type") == "image":
            src = b.get("source") or {}
            out.append({"type": "input_image", "image_url": f"data:{src.get('media_type')};base64,{src.get('data')}"})
    return out


# ------------------------------------------------------------------------------------------------ API: tools

def _tool_defs(settings, names):
    from . import tools
    out = []
    for s in tools.schemas(settings):
        f = s["function"]
        if f["name"] not in names:
            continue
        out.append({"type": "function", "name": f["name"], "description": (f["description"] or f["name"])[:1024],
                    "parameters": f["parameters"] or {"type": "object", "properties": {}}, "strict": False})
    return out


SUBMIT_REVIEW = {"type": "function", "name": "submit_review", "description": cloud.SUBMIT_REVIEW["description"],
                 "parameters": cloud.SUBMIT_REVIEW["input_schema"], "strict": True}


def _item_input(item):
    """A response output item → the item to send back next step (store=False: everything travels in the request,
    reasoning as encrypted content)."""
    t = getattr(item, "type", None)
    if t == "reasoning":
        d = item.model_dump(exclude_none=True)
        d.pop("status", None)
        return d
    if t == "function_call":
        return {"type": "function_call", "call_id": item.call_id, "name": item.name, "arguments": item.arguments or "{}"}
    if t == "message":
        text = "".join(getattr(c, "text", "") or "" for c in item.content or [])
        return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    return None


# ------------------------------------------------------------------------------------------------ API: loop

async def _stream(turn, lane, model, instructions, inp, tool_defs, effort, max_tokens, on_tool, step_budget):
    """A manual agent loop over the Responses API with streaming, mirroring cloud._stream. on_tool(call, box) is
    an async generator that yields UI events and sets box['output'] (text), box['image'] or box['stop']."""
    import openai
    c = client()
    s = turn.settings
    for _step in range(step_budget):
        if turn.cancel.is_set():
            return
        if not budget_ok(s):
            yield {"t": "notice", "lane": lane, "text": f"Daily OpenAI budget (${s.get('openai_daily_budget_usd')}) "
                                                        f"reached; {name_of(model)} stopped. Raise it in Settings → ChatGPT."}
            return
        msg = {"role": "assistant", "content": "", "reasoning": "", "id": uuid.uuid4().hex[:10], "lane": lane,
               "model": name_of(model)}
        yield {"t": "assistant_start", "id": msg["id"], "lane": lane, "model": msg["model"]}
        reasoning = {"effort": effort if effort in EFFORTS else "high"}
        if model not in _no_summary:
            reasoning["summary"] = "auto"
        kw = dict(model=model, instructions=instructions, input=inp, store=False, reasoning=reasoning,
                  include=["reasoning.encrypted_content"], max_output_tokens=max_tokens,
                  prompt_cache_key=f"aero-{turn.chat_id}-{lane}"[:64])
        if tool_defs:
            kw["tools"] = tool_defs
            kw["parallel_tool_calls"] = False
        t0 = time.time()
        first = None
        final = None
        try:
            async with c.responses.stream(**kw) as stream:
                async for ev in stream:
                    if turn.cancel.is_set():
                        break
                    et = ev.type
                    if et == "response.output_text.delta" and ev.delta:
                        first = first or time.time()
                        msg["content"] += ev.delta
                        yield {"t": "content", "d": ev.delta, "lane": lane}
                    elif et == "response.reasoning_summary_text.delta" and ev.delta:
                        first = first or time.time()
                        msg["reasoning"] += ev.delta
                        yield {"t": "reasoning", "d": ev.delta, "lane": lane}
                    elif et == "response.reasoning_summary_part.done":
                        msg["reasoning"] += "\n\n"
                        yield {"t": "reasoning", "d": "\n\n", "lane": lane}
                    elif et == "response.output_item.added" and getattr(ev.item, "type", "") == "function_call":
                        yield {"t": "tool_pending", "name": ev.item.name, "lane": lane}
                if not turn.cancel.is_set():
                    final = await stream.get_final_response()
        except openai.BadRequestError as e:
            low = str(e).lower()
            if "summary" in low and model not in _no_summary:
                _no_summary.add(model)            # organisation not verified for summaries: retry without them
                yield {"t": "assistant_done", "message": {**msg, "content": "", "discard": True}}
                continue
            raise
        if final is None:
            msg["stats"] = {"finish": "stopped"}
            turn.history.append(msg)
            yield {"t": "assistant_done", "message": msg}
            return

        served = getattr(final, "model", None) or model
        base = next((k for k in PRICES if served.startswith(k)), model)
        uu = final.usage
        inp_t = getattr(uu, "input_tokens", 0) or 0
        cached = getattr(getattr(uu, "input_tokens_details", None), "cached_tokens", 0) or 0
        out_t = getattr(uu, "output_tokens", 0) or 0
        u = {"input": inp_t - cached, "output": out_t, "cache_read": cached, "cache_write": 0,
             "usd": cost_of(base, inp_t, out_t, cached)}
        cloud.note_usage(base, u)
        calls = [it for it in final.output if getattr(it, "type", "") == "function_call"]
        if calls:
            msg["tool_calls"] = [{"id": it.call_id, "type": "function",
                                  "function": {"name": it.name, "arguments": it.arguments or "{}"}} for it in calls]
        dt = time.time() - t0
        status = getattr(final, "status", "completed")
        reason = getattr(getattr(final, "incomplete_details", None), "reason", None)
        msg["stats"] = {"prompt_tokens": inp_t, "completion_tokens": out_t, "cached_tokens": cached,
                        "usd": round(u["usd"], 4), "time": round(dt, 1), "ttft": round(first - t0, 2) if first else None,
                        "finish": reason or status, "tg": round(out_t / max(0.1, dt - ((first or t0) - t0)), 1)}
        msg["reasoning"] = msg["reasoning"].strip()
        if not msg["reasoning"]:
            msg.pop("reasoning")
        turn.history.append(msg)
        yield {"t": "assistant_done", "message": msg}
        yield {"t": "cloud_usage", "model": base, "usage": u, "today_usd": cloud.spent_today()}

        if status == "incomplete" and reason == "content_filter":
            yield {"t": "notice", "lane": lane, "text": f"{name_of(model)} declined this request."}
            return
        if not calls:
            if status == "incomplete":
                yield {"t": "notice", "lane": lane, "text": f"{name_of(model)} stopped early ({reason})."}
            return
        inp = inp + [x for x in (_item_input(it) for it in final.output) if x]
        images = []
        stop = None
        for call in calls:
            box = {}
            async for ev in on_tool(call, box):
                yield ev
            if "stop" in box:
                stop = box["stop"]
            inp.append({"type": "function_call_output", "call_id": call.call_id, "output": box.get("output") or "ok"})
            if box.get("image"):
                images.append((call.name, box["image"]))
        if stop is not None:
            turn.cloud_result = stop
            return
        if images:
            from . import attachments
            content = []
            for name, img in images[-2:]:
                try:
                    content += [{"type": "input_text", "text": f"[Image returned by {name}]"},
                                {"type": "input_image", "image_url": attachments.image_data_url(img)}]
                except Exception:
                    pass
            if content:
                inp.append({"role": "user", "content": content})
        if turn.cancel.is_set():
            return
    yield {"t": "notice", "lane": lane, "text": f"{name_of(model)} stopped after {step_budget} steps."}


def _parse_args(raw):
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def _local_tool_runner(turn, lane, defs, allowed_categories=None):
    schemas = {d["name"]: d["parameters"] for d in defs}

    async def run(call, box):
        args = _parse_args(call.arguments)
        why = "arguments are not a JSON object" if args is None else cloud.validate(schemas.get(call.name), args)
        if why:
            tmsg = {"role": "tool", "tool_call_id": call.call_id, "name": call.name, "error": True, "label": "",
                    "content": json.dumps({"INVALID_JSON": why}), "lane": lane}
            yield {"t": "tool_start", "call_id": call.call_id, "name": call.name, "args": args, "label": "",
                   "category": None, "needs_approval": False, "lane": lane}
            turn.history.append(tmsg)
            yield {"t": "tool_result", "message": tmsg}
            box["output"] = tmsg["content"]
            return
        out = {}
        async for ev in agent.exec_tool(turn, call.call_id, call.name, args, lane,
                                        allowed_categories=allowed_categories, out=out):
            yield ev
        turn.history.append(out["msg"])
        res = out["res"]
        box["output"] = ("[ERROR] " if res.get("error") else "") + (res.get("text") or "(no output)")
        if res.get("image"):
            box["image"] = res["image"]
    return run


async def _api_review(turn, round_no, previous):
    s = turn.settings
    model = s.get("gpt_review_model") or "gpt-6-astra"
    names = cloud.read_only_names(s) if s.get("gpt_review_tools", True) else []
    defs = _tool_defs(s, set(names))
    allowed = cloud.READ_ONLY_CATEGORIES | {"memory"}
    local_run = _local_tool_runner(turn, "astra", defs, allowed)

    async def on_tool(call, box):
        if call.name == "submit_review":
            v = _parse_args(call.arguments) or {}
            why = "verdict must be ok, minor or major" if v.get("verdict") not in ("ok", "minor", "major") else \
                cloud.validate(cloud.SUBMIT_REVIEW["input_schema"], v)
            if why:
                box["output"] = "[ERROR] " + why
                return
            box["stop"] = v
            box["output"] = "Review recorded."
            return
        if call.name not in names:
            box["output"] = "[ERROR] Reviewers have read-only tools only."
            return
        async for ev in local_run(call, box):
            yield ev

    turn.cloud_result = None
    inp = [{"role": "user", "content": _to_input(_review_packet(turn, round_no, previous))}]
    system = cloud.with_profile(turn, REVIEW_SYSTEM, "review")
    async for ev in _stream(turn, "astra", model, system, inp, defs + [SUBMIT_REVIEW],
                            s.get("gpt_review_effort") or "high", 32000, on_tool, 16):
        yield ev
    if turn.cloud_result is None and not turn.cancel.is_set():
        last = next((m for m in reversed(turn.history) if m.get("lane") == "astra" and m.get("role") == "assistant"), None)
        if last and (last.get("content") or "").strip():
            inp = inp + [{"role": "assistant", "content": last["content"]},
                         {"role": "user", "content": "Now call submit_review with your verdict."}]
            async for ev in _stream(turn, "astra", model, system, inp, [SUBMIT_REVIEW], "low", 8000, on_tool, 2):
                yield ev


async def _api_execute(turn, rv):
    s = turn.settings
    model = s.get("gpt_fix_model") or "gpt-6.1-sol"
    names = cloud.all_names(s)
    defs = _tool_defs(s, set(names))
    run = _local_tool_runner(turn, "sol", defs)
    inp = [{"role": "user", "content": _to_input(_execute_packet(turn, rv))}]
    async for ev in _stream(turn, "sol", model, cloud.with_profile(turn, EXECUTE_SYSTEM, "execute"), inp, defs,
                            s.get("gpt_fix_effort") or "high", 64000, run, int(s.get("agent_max_steps") or 40)):
        yield ev


# ------------------------------------------------------------------------------------------------ plan: Codex CLI

def cli_path(settings=None):
    """The official Codex CLI: an explicit path from settings, codex.exe on PATH, or the native codex.exe inside
    npm's global @openai/codex package (preferred over npm's codex.cmd shim), else the shim itself."""
    p = ((settings or {}).get("codex_cli_path") or "").strip().strip('"')
    if p and Path(p).is_file():
        return p
    if IS_WIN:
        exe = shutil.which("codex.exe")
        if exe:
            return exe
        npm = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "npm"
        for cand in sorted((npm / "node_modules" / "@openai" / "codex").glob("**/codex.exe")):
            return str(cand)
        shim = shutil.which("codex.cmd") or (str(npm / "codex.cmd") if (npm / "codex.cmd").is_file() else None)
        return shim
    return shutil.which("codex")


def _env():
    """Child environment for plan mode: an API key would replace the ChatGPT sign-in, so blank it."""
    e = dict(os.environ)
    for k in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        e.pop(k, None)
    return e


def _run_cli(args, timeout=30, settings=None):
    exe = cli_path(settings or _settings())
    if not exe:
        raise RuntimeError("The Codex CLI is not installed.")
    return subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=_env(), creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL)


def _settings():
    from .config import load_settings
    return load_settings()


def version():
    try:
        out = (_run_cli(["--version"], 20).stdout or "").strip()
        m = re.search(r"\d+\.\d+\.\d+\S*", out)
        return m.group(0) if m else (out.split()[-1] if out else None)
    except Exception:
        return None


def status():
    """Whether Codex is installed and signed in, and how. Only the method is kept from `codex login status`
    (its output can include a masked API key, which is never passed on)."""
    exe = cli_path(_settings())
    st = {"cli": exe, "installed": bool(exe), "loggedIn": False, "method": None}
    if not exe:
        return st
    if _settings().get("strict_offline"):
        st["offline"] = True
        return st
    st["version"] = version()
    try:
        p = _run_cli(["login", "status"], 30)
        text = ((p.stdout or "") + "\n" + (p.stderr or "")).lower()
        if p.returncode == 0 and "not logged in" not in text:
            st["loggedIn"] = True
            st["method"] = "chatgpt" if "chatgpt" in text else "api_key" if "api key" in text else "unknown"
    except Exception as e:  # noqa: BLE001
        st["error"] = f"{type(e).__name__}: {e}"[:300]
    return st


def login(device=False):
    """Open OpenAI's own sign-in (`codex login` → chatgpt.com in the browser) in a console window."""
    from . import localonly
    localonly.guard("signing in to ChatGPT")
    exe = cli_path(_settings())
    if not exe:
        raise RuntimeError("The Codex CLI is not installed. Install it first (Settings → ChatGPT shows how).")
    args = [exe, "login"] + (["--device-auth"] if device else [])
    if IS_WIN:
        subprocess.Popen(args, env=_env(), creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        subprocess.Popen(args, env=_env())
    return {"started": True}


def logout():
    p = _run_cli(["logout"], 30)
    return {"ok": p.returncode == 0, "output": "Signed out." if p.returncode == 0 else "Codex could not sign out."}


def _safe_model(m, default):
    return m if m and _MODEL_RE.match(m) else default


def _exec_args(model, effort, sandbox, schema_path=None, images=()):
    a = ["exec", "--json", "--ephemeral", "--skip-git-repo-check", "--sandbox", sandbox, "-m", model,
         "-c", f'model_reasoning_effort="{effort if effort in EFFORTS else "high"}"']
    if schema_path:
        a += ["--output-schema", str(schema_path)]
    for img in images:
        a += ["-i", str(img)]
    return a + ["-"]


def _cmd_label(cmd):
    if isinstance(cmd, list):
        cmd = " ".join(str(x) for x in cmd)
    cmd = str(cmd or "").replace("\n", " ")
    return cmd if len(cmd) <= 90 else cmd[:87] + "…"


async def _codex(turn, lane, model, prompt, sandbox, effort, cwd, schema=None, images=()):
    """Run one `codex exec --json` and turn its JSONL events into Aero UI events. The final agent message is left
    in turn.codex_final. The process runs in a thread (works under any asyncio event loop) and is killed on cancel."""
    exe = cli_path(turn.settings)
    if not exe:
        raise RuntimeError("The Codex CLI is not installed.")
    tmp = Path(tempfile.mkdtemp(prefix="aero-codex-"))
    schema_path = None
    if schema:
        schema_path = tmp / "schema.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
    img_paths = []
    for i, (media, data) in enumerate(images):
        import base64
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(media, ".png")
        pth = tmp / f"img{i}{ext}"
        pth.write_bytes(base64.b64decode(data))
        img_paths.append(pth)
    args = [exe, *_exec_args(model, effort, sandbox, schema_path, img_paths)]
    loop = asyncio.get_running_loop()
    q = asyncio.Queue()
    END = object()
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
                            env=_env(), creationflags=_NO_WINDOW, text=True, encoding="utf-8", errors="replace")
    err_tail = []

    def feed():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except Exception:
            pass

    def read_out():
        for line in proc.stdout:
            line = line.strip()
            if line:
                loop.call_soon_threadsafe(q.put_nowait, line)
        loop.call_soon_threadsafe(q.put_nowait, END)

    def read_err():
        for line in proc.stderr:
            err_tail.append(line.rstrip())
            del err_tail[:-15]

    threads = [threading.Thread(target=fn, daemon=True) for fn in (feed, read_out, read_err)]
    for th in threads:
        th.start()

    turn.codex_final = ""
    state = {"msg": None, "t0": time.time(), "first": None, "started": {}}

    def start_msg():
        m = {"role": "assistant", "content": "", "reasoning": "", "id": uuid.uuid4().hex[:10], "lane": lane,
             "model": name_of(model)}
        state.update(msg=m, t0=time.time(), first=None)
        return {"t": "assistant_start", "id": m["id"], "lane": lane, "model": m["model"]}

    def finish_msg(usage=None, finish=None):
        m = state["msg"]
        if not m:
            return None
        state["msg"] = None
        st = {"time": round(time.time() - state["t0"], 1), "finish": finish, "via": "plan"}
        if state["first"]:
            st["ttft"] = round(state["first"] - state["t0"], 2)
        if usage:
            st["prompt_tokens"] = usage.get("input_tokens") or 0
            st["cached_tokens"] = usage.get("cached_input_tokens") or 0
            st["completion_tokens"] = usage.get("output_tokens") or 0
        m["stats"] = st
        m["reasoning"] = m["reasoning"].strip()
        if not m["reasoning"]:
            m.pop("reasoning")
        if not m["content"] and not m.get("reasoning") and not usage:
            return None
        turn.history.append(m)
        return {"t": "assistant_done", "message": m}

    yield start_msg()
    try:
        while True:
            get = asyncio.ensure_future(q.get())
            while not get.done():
                await asyncio.wait({get}, timeout=0.25)
                if turn.cancel.is_set() and proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            line = get.result()
            if line is END:
                break
            try:
                ev = json.loads(line)
            except Exception:
                continue
            et = ev.get("type", "")
            item = ev.get("item") or {}
            it = item.get("type", "")
            if et in ("item.started", "item.updated", "item.completed"):
                if not state["msg"]:
                    yield start_msg()
                iid = item.get("id") or uuid.uuid4().hex[:8]
                if it == "reasoning" and et == "item.completed" and item.get("text"):
                    state["first"] = state["first"] or time.time()
                    d = item["text"].strip() + "\n\n"
                    state["msg"]["reasoning"] += d
                    yield {"t": "reasoning", "d": d, "lane": lane}
                elif it == "agent_message" and et == "item.completed":
                    state["first"] = state["first"] or time.time()
                    text = item.get("text") or ""
                    turn.codex_final = text
                    if not schema:                          # the review's JSON verdict is shown as a card instead
                        state["msg"]["content"] += text
                        yield {"t": "content", "d": text, "lane": lane}
                elif it in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
                    call_id = "cx" + iid
                    if it == "command_execution":
                        name, cat, label, args = "Codex: command", "shell", _cmd_label(item.get("command")), \
                            {"command": item.get("command")}
                    elif it == "file_change":
                        paths = [f"{c.get('kind', 'update')} {c.get('path')}" for c in item.get("changes") or []]
                        name, cat, label, args = "Codex: file change", "files_write", _cmd_label(", ".join(paths)), \
                            {"changes": item.get("changes")}
                    elif it == "mcp_tool_call":
                        name, cat, label, args = f"Codex: {item.get('server')}.{item.get('tool')}", "mcp", "", \
                            {"server": item.get("server"), "tool": item.get("tool")}
                    else:
                        name, cat, label, args = "Codex: web search", "web", _cmd_label(item.get("query")), \
                            {"query": item.get("query")}
                    if call_id not in state["started"]:
                        state["started"][call_id] = True
                        done = finish_msg()
                        if done:
                            yield done
                        yield {"t": "tool_start", "call_id": call_id, "name": name, "args": args, "label": label,
                               "category": cat, "needs_approval": False, "lane": lane}
                    if et == "item.completed":
                        out = item.get("aggregated_output") if it == "command_execution" else \
                            json.dumps(item.get("changes") or item.get("result") or item.get("query") or "", ensure_ascii=False)
                        failed = item.get("status") == "failed" or (it == "command_execution" and item.get("exit_code") not in (0, None))
                        tmsg = {"role": "tool", "tool_call_id": call_id, "name": name, "label": label, "lane": lane,
                                "content": (str(out or "") + (f"\n[exit code {item.get('exit_code')}]" if it == "command_execution" else ""))[:20000],
                                "error": bool(failed)}
                        turn.history.append(tmsg)
                        yield {"t": "tool_result", "message": tmsg}
                        yield start_msg()
                elif it == "error" and item.get("message"):
                    yield {"t": "notice", "lane": lane, "level": "warn", "text": f"Codex: {item['message']}"[:500]}
            elif et == "turn.completed":
                u = ev.get("usage") or {}
                done = finish_msg(u, "completed")
                if done:
                    yield done
                row = {"input": max(0, (u.get("input_tokens") or 0) - (u.get("cached_input_tokens") or 0)),
                       "output": u.get("output_tokens") or 0, "cache_read": u.get("cached_input_tokens") or 0,
                       "cache_write": 0, "usd": 0.0}
                cloud.note_usage(model + " (plan)", row)
                yield {"t": "cloud_usage", "model": model, "via": "plan", "usage": row, "today_usd": cloud.spent_today()}
            elif et in ("turn.failed", "error"):
                m = (ev.get("error") or {}).get("message") if isinstance(ev.get("error"), dict) else ev.get("message")
                yield {"t": "notice", "lane": lane, "level": "error", "text": (f"Codex stopped: {m}"[:500]) + _hint(m)}
        rc = await asyncio.to_thread(proc.wait)
        done = finish_msg(None, "stopped" if turn.cancel.is_set() else None)
        if done:
            yield done
        if rc not in (0, None) and not turn.cancel.is_set() and not turn.codex_final:
            tail = " | ".join(x for x in err_tail[-3:] if x.strip())
            yield {"t": "notice", "lane": lane, "level": "error",
                   "text": f"Codex exited with code {rc}" + (f": {tail}"[:400] if tail else "") + _hint(tail)}
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        for th in threads:
            th.join(timeout=2)
        for f in (proc.stdout, proc.stderr):
            try:
                f.close()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def _hint(text):
    t = (text or "").lower()
    if any(k in t for k in ("not logged in", "login", "unauthorized", "401", "auth")):
        return " Sign in again under Settings → ChatGPT."
    if "model" in t and any(k in t for k in ("not found", "not supported", "does not exist", "access")):
        return " Your ChatGPT plan may not include this model yet; pick another in Settings → ChatGPT."
    if "usage limit" in t or "rate limit" in t or "429" in t:
        return " Your ChatGPT plan's usage limit is reached for now."
    return ""


def _packet_text(blocks):
    return "\n\n".join(b["text"] for b in blocks if b.get("type") == "text")


def _packet_images(blocks):
    out = []
    for b in blocks:
        if b.get("type") == "image":
            src = b.get("source") or {}
            out.append((src.get("media_type") or "image/png", src.get("data") or ""))
    return out


async def _plan_review(turn, round_no, previous):
    s = turn.settings
    model = _safe_model(s.get("gpt_review_model"), "gpt-6-astra")
    blocks = _review_packet(turn, round_no, previous)
    system = cloud.with_profile(turn, REVIEW_SYSTEM, "review").replace(
        "Finish by calling submit_review exactly once. Do not write the verdict as plain text.",
        "Finish with your verdict as the JSON object the output schema describes, and nothing else.").replace(
        "you have read-only tools on the same PC (read files, list folders, search, look at the screen and windows, "
        "search and fetch the web)", "you can read files and run read-only commands in the work folder")
    prompt = system + "\n\n---\n\n" + _packet_text(blocks)
    turn.cloud_result = None
    async for ev in _codex(turn, "astra", model, prompt, "read-only", s.get("gpt_review_effort") or "high",
                           s.get("work_dir") or str(Path.home()), schema=cloud.SUBMIT_REVIEW["input_schema"],
                           images=_packet_images(blocks)):
        yield ev
    raw = (turn.codex_final or "").strip()
    if raw:
        try:
            v = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
            if v.get("verdict") in ("ok", "minor", "major"):
                turn.cloud_result = {k: v.get(k) or ([] if k == "issues" else "")
                                     for k in cloud.SUBMIT_REVIEW["input_schema"]["properties"]}
        except Exception:
            pass


async def _approve_workspace(turn, lane, folder):
    """One approval card before Sol works through Codex: it edits files and runs commands in the work folder
    without asking per step (Codex's workspace-write sandbox), so the user decides up front."""
    pol = turn.settings.get("tool_policy", {})
    if pol.get("files_write", "ask") == "off" or pol.get("shell", "ask") == "off":
        return "readonly"
    allowed = agent._chat_allow.get(turn.chat_id, set())
    if pol.get("files_write") == "auto" and pol.get("shell") == "auto" or {"files_write", "shell"} <= allowed:
        return "allow"
    call_id = "cxa" + uuid.uuid4().hex[:8]
    yield_ev = {"t": "tool_start", "call_id": call_id, "name": "codex_workspace", "lane": lane,
                "args": {"folder": folder, "sandbox": "workspace-write"},
                "label": f"Let GPT-6.1 Sol edit files and run commands in {folder}", "category": "shell",
                "needs_approval": True}
    fut = asyncio.get_running_loop().create_future()
    agent._approvals[call_id] = fut
    return (yield_ev, fut, call_id)


async def _plan_execute(turn, rv):
    s = turn.settings
    model = _safe_model(s.get("gpt_fix_model"), "gpt-6.1-sol")
    folder = s.get("work_dir") or str(Path.home())
    ask = await _approve_workspace(turn, "sol", folder)
    sandbox = "workspace-write"
    if ask == "readonly":
        sandbox = "read-only"
        yield {"t": "notice", "lane": "sol", "text": "File writing or commands are turned off in Settings → Tools, so "
                                                      "GPT-6.1 Sol works read-only and describes the fix instead."}
    elif isinstance(ask, tuple):
        ev, fut, call_id = ask
        yield ev
        decision = await fut
        res = "Allowed for this repair." if decision != "deny" and not turn.cancel.is_set() else "Denied."
        tmsg = {"role": "tool", "tool_call_id": call_id, "name": "codex_workspace", "content": res,
                "error": decision == "deny", "label": ev["label"], "lane": "sol"}
        if decision == "deny":
            tmsg["denied"] = True
        turn.history.append(tmsg)
        yield {"t": "tool_result", "message": tmsg}
        if turn.cancel.is_set():
            return
        if decision == "deny":
            sandbox = "read-only"
            yield {"t": "notice", "lane": "sol", "text": "GPT-6.1 Sol works read-only and describes the fix instead."}
    blocks = _execute_packet(turn, rv)
    system = cloud.with_profile(turn, EXECUTE_SYSTEM, "execute").replace(
        f"Your tools run on the user's own computer (files, {osinfo.shell_name()}, app windows, browser, web, MCP servers). Some need "
        "the user's approval; if one is denied, do not retry it, find another way or explain.",
        "You work through Codex in the user's work folder" + (" and may edit files and run commands there."
                                                              if sandbox != "read-only" else
                                                              ", read-only: explain exactly what to change instead of changing it."))
    prompt = system + "\n\n---\n\n" + _packet_text(blocks)
    async for ev in _codex(turn, "sol", model, prompt, sandbox, s.get("gpt_fix_effort") or "high", folder,
                           images=_packet_images(blocks)):
        yield ev


# ------------------------------------------------------------------------------------------------ review / execute

async def review(turn, round_no=1, previous=None):
    """Astra reviews the turn's work. Yields UI events; the verdict lands in turn.cloud_result (or None)."""
    s = turn.settings
    model = s.get("gpt_review_model") or "gpt-6-astra"
    via = backend(s)
    yield {"t": "lane", "lane": "astra", "phase": "review", "round": round_no, "model": name_of(model), "via": via}
    if via == "plan":
        async for ev in _plan_review(turn, round_no, previous):
            yield ev
    else:
        async for ev in _api_review(turn, round_no, previous):
            yield ev


async def execute(turn, rv):
    """Sol carries out Astra's plan. Yields UI events."""
    s = turn.settings
    model = s.get("gpt_fix_model") or "gpt-6.1-sol"
    via = backend(s)
    yield {"t": "lane", "lane": "sol", "phase": "execute", "model": name_of(model), "via": via}
    if via == "plan":
        async for ev in _plan_execute(turn, rv):
            yield ev
    else:
        async for ev in _api_execute(turn, rv):
            yield ev
