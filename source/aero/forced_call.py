"""A tool call the model can't refuse: grammar-constrained JSON instead of tool_choice.

llama-server's tool_choice "required" (and a named tool_choice) doesn't constrain the output for every chat template:
measured with Qwen3-4B on llama.cpp b11501, "required" let the model keep writing text (it only blocked the end
token), and a named tool_choice is ignored. response_format with a JSON schema IS enforced by a grammar for every
model, so a forced call asks for {"tool": <one of the offered names>, "arguments": <that tool's parameters>} as an
anyOf over the tools and turns the answer into an ordinary tool call.

Used when a request plainly needs an action (intents.py) but the model answered "I can't do that", by the loopback
adapter for clients that force a named tool (Hindsight), and by the tool-calling exam.
"""
import json

import httpx


def schema(tools):
    """anyOf over the tools: {"tool": const name, "arguments": that tool's parameters}."""
    alts = []
    for t in tools:
        f = t.get("function") or t
        params = f.get("parameters") or {"type": "object", "properties": {}}
        params = {"type": "object", "properties": params.get("properties") or {},
                  "required": [r for r in params.get("required") or [] if r in (params.get("properties") or {})]}
        alts.append({"type": "object", "properties": {"tool": {"const": f["name"]}, "arguments": params},
                     "required": ["tool", "arguments"], "additionalProperties": False})
    return {"anyOf": alts} if len(alts) > 1 else alts[0]


def catalog_text(tools):
    lines = []
    for t in tools:
        f = t.get("function") or t
        props = (f.get("parameters") or {}).get("properties") or {}
        args = ", ".join(f"{k}: {(v.get('description') or v.get('type') or '')[:60]}" for k, v in list(props.items())[:6])
        lines.append(f"- {f['name']}: {(f.get('description') or '')[:200]}" + (f" ({args})" if args else ""))
    return "\n".join(lines)


def ask(url, messages, tools, temperature=0.2, hint="", timeout=600, extra=None):
    """One forced call. Returns (name, args) or None when the server failed."""
    if not tools:
        return None
    note = ("Choose the tool to call next and its arguments. You must call one of these tools now; don't explain."
            + (f"\n{hint}" if hint else "") + "\n\nTools:\n" + catalog_text(tools))
    msgs = [m for m in messages if m.get("role") != "tool" or m.get("content")]
    msgs = msgs + [{"role": "user", "content": note}] if msgs and msgs[-1].get("role") != "user" else \
        msgs[:-1] + [{"role": "user", "content": (msgs[-1].get("content") or "") + "\n\n" + note}] if msgs else \
        [{"role": "user", "content": note}]
    body = {"messages": _plain(msgs), "temperature": temperature, "max_tokens": 600, "stream": False,
            "response_format": {"type": "json_schema", "json_schema": {"name": "tool_call", "schema": schema(tools)}},
            "chat_template_kwargs": {"enable_thinking": False}, **(extra or {})}
    try:
        r = httpx.post(url.rstrip("/") + "/v1/chat/completions", json=body, timeout=timeout)
        r.raise_for_status()
        j = json.loads((r.json()["choices"][0]["message"].get("content") or "").strip())
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return None
    if isinstance(j, dict) and isinstance(j.get("tool"), str) and isinstance(j.get("arguments"), dict):
        return j["tool"], j["arguments"]
    return None


def _plain(msgs):
    """Messages a request without tools accepts: tool calls and results as plain text."""
    out = []
    for m in msgs:
        role = m.get("role")
        if role == "tool":
            out.append({"role": "user", "content": f"[result of {m.get('name') or 'a tool'}]\n{m.get('content') or ''}"})
        elif role == "assistant" and m.get("tool_calls"):
            calls = "; ".join(f"{c['function']['name']}({c['function'].get('arguments') or '{}'})" for c in m["tool_calls"])
            out.append({"role": "assistant", "content": ((m.get("content") or "") + f"\n[called {calls}]").strip()})
        elif role in ("system", "user", "assistant"):
            c = m.get("content")
            if isinstance(c, list):
                c = "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
            out.append({"role": role, "content": c or ""})
    # llama.cpp templates want alternating turns after the system message: merge neighbours with the same role
    merged = []
    for m in out:
        if merged and merged[-1]["role"] == m["role"] and m["role"] != "system":
            merged[-1]["content"] += "\n\n" + m["content"]
        else:
            merged.append(dict(m))
    return merged
