"""Tool calls a model printed as text instead of llama-server returning them as structured calls.

Happens when a model's chat template or its call format isn't one llama-server's parser knows, when the model calls a
tool it wasn't offered (llama-server then drops the structured call), or when tools are off. The formats below are the
ones local models actually print (from their chat templates and from llama.cpp's own chat parsers):

  hermes      <tool_call>{"name": ..., "arguments": {...}}</tool_call>             Qwen 2.5/3, Hermes, MiniCPM, many
  qwen-xml    <tool_call><function=NAME><parameter=K>V</parameter></function></tool_call>   Qwen3-Coder
  glm         <tool_call>NAME<arg_key>K</arg_key><arg_value>V</arg_value></tool_call>        GLM 4.5+
  functionary <function=NAME>{...}</function>                                       functionary, Llama 3.1 (some)
  mistral     [TOOL_CALLS][{"name": ..., "arguments": {...}}]   or   [TOOL_CALLS]NAME[ARGS]{...}
  python_tag  <|python_tag|>{"name": ..., "parameters": {...}}                       Llama 3.x
  granite     <|tool_call|>[{"name": ..., "arguments": {...}}]                       Granite
  deepseek    <｜tool▁call▁begin｜>function<｜tool▁sep｜>NAME ```json {...}``` <｜tool▁call▁end｜>
  tool_code   ```tool_code\nNAME(k=v, ...)\n```                                     Gemma
  bare        {"name": ..., "parameters"|"arguments": {...}} ending the reply         Llama 3.x without a tag

parse(text, enabled) returns ([(name, args)], text with every recognised call removed). Tagged formats are explicit, so
their calls are returned whatever the name (an unknown tool gets an error result the model can learn from); a bare
JSON object only counts when it ends the reply and names an enabled tool, so a JSON example in an explanation never
runs.
"""
import ast
import json
import re

_DEC = json.JSONDecoder()


def _json_at(text, i):
    """(object, end) of the JSON value starting at or after text[i] (skipping spaces and a ```json fence)."""
    m = re.compile(r"\s*(?:```(?:json)?\s*)?").match(text, i)
    j = m.end() if m else i
    try:
        return _DEC.raw_decode(text, j)
    except ValueError:
        return None, i


def _args(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return None
    return v if isinstance(v, dict) else None


def _call(obj):
    """(name, args) from {"name", "arguments"|"parameters"} or {"function": {...}}, else None."""
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):
        obj = obj["function"]
    name = obj.get("name")
    args = _args(obj.get("arguments", obj.get("parameters", {})))
    if isinstance(name, str) and name and args is not None:
        return name, args
    return None


def _value(s):
    s = s.strip()
    try:
        return json.loads(s)
    except ValueError:
        return s


_TAGGED = [
    # (name, start regex) — each handler returns (calls, end) or None
    ("hermes", re.compile(r"<tool_call>")),
    ("functionary", re.compile(r"<function=([\w.\-:]+)>")),
    ("mistral", re.compile(r"\[TOOL_CALLS\]")),
    ("python_tag", re.compile(r"<\|python_tag\|>")),
    ("granite", re.compile(r"<\|tool_call\|>")),
    ("deepseek", re.compile(r"<｜tool▁calls?▁begin｜>")),
    ("tool_code", re.compile(r"```tool_code\s*\n")),
]


def _hermes(text, m):
    i = m.end()
    end_tag = text.find("</tool_call>", i)
    body_end = end_tag if end_tag >= 0 else len(text)
    stop = end_tag + len("</tool_call>") if end_tag >= 0 else len(text)
    body = text[i:body_end]
    fx = re.match(r"\s*<function=([\w.\-:]+)>(.*?)(?:</function>|$)", body, re.S)
    if fx:                                                       # Qwen3-Coder XML
        args = {k: _value(v) for k, v in re.findall(r"<parameter=([\w.\-]+)>(.*?)</parameter>", fx.group(2), re.S)}
        return [(fx.group(1), args)], stop
    if "<arg_key>" in body:                                      # GLM
        name = body.split("<arg_key>", 1)[0].strip()
        keys = re.findall(r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>", body, re.S)
        return ([(name, {k.strip(): _value(v) for k, v in keys})] if name else []), stop
    obj, _ = _json_at(text, i)
    c = _call(obj)
    return ([c] if c else []), stop


def _functionary(text, m):
    obj, end = _json_at(text, m.end())
    args = _args(obj) if obj is not None else None
    close = text.find("</function>", end)
    stop = close + len("</function>") if close >= 0 and close - end < 40 else end
    return ([(m.group(1), args)] if args is not None else []), stop


def _mistral(text, m):
    i = m.end()
    named = re.compile(r"\s*([\w.\-:]+)\[ARGS\]").match(text, i)
    if named:                                                    # [TOOL_CALLS]name[ARGS]{...}
        obj, end = _json_at(text, named.end())
        args = _args(obj) if obj is not None else None
        return ([(named.group(1), args)] if args is not None else []), end
    obj, end = _json_at(text, i)
    items = obj if isinstance(obj, list) else [obj]
    return [c for c in map(_call, items) if c], end


def _single_or_list(text, m):
    obj, end = _json_at(text, m.end())
    items = obj if isinstance(obj, list) else [obj]
    return [c for c in map(_call, items) if c], end


def _deepseek(text, m):
    end_all = text.find("<｜tool▁calls▁end｜>", m.end())
    stop = end_all + len("<｜tool▁calls▁end｜>") if end_all >= 0 else len(text)
    calls = []
    for nm, body in re.findall(r"<｜tool▁call▁begin｜>\s*(?:function)?<｜tool▁sep｜>\s*([\w.\-:]+)\s*(.*?)<｜tool▁call▁end｜>",
                               text[m.start():stop], re.S):
        obj, _ = _json_at(body, 0)
        args = _args(obj) if obj is not None else None
        if args is not None:
            calls.append((nm, args))
    return calls, stop


def _tool_code(text, m):
    close = text.find("```", m.end())
    stop = close + 3 if close >= 0 else len(text)
    src = text[m.end():close if close >= 0 else len(text)].strip()
    calls = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return [], stop
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            f = node.value.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            try:
                args = {kw.arg: ast.literal_eval(kw.value) for kw in node.value.keywords if kw.arg}
            except ValueError:
                continue
            if name:
                calls.append((name, args))
    return calls, stop


_HANDLERS = {"hermes": _hermes, "functionary": _functionary, "mistral": _mistral, "python_tag": _single_or_list,
             "granite": _single_or_list, "deepseek": _deepseek, "tool_code": _tool_code}


def parse(text, enabled=None):
    """([(name, args)], cleaned text). enabled: tool names a bare JSON call may name (None = no bare calls)."""
    text = text or ""
    calls, cut = [], []
    pos = 0
    while True:
        hits = [(m.start(), kind, m) for kind, rx in _TAGGED if (m := rx.search(text, pos))]
        if not hits:
            break
        start, kind, m = min(hits, key=lambda h: h[0])
        found, end = _HANDLERS[kind](text, m)
        if end <= m.start():
            end = m.end()
        calls += found
        cut.append((start, end))
        pos = end
    cleaned = text
    for a, b in reversed(cut):
        cleaned = cleaned[:a] + cleaned[b:]
    if not calls and enabled:
        bare = bare_call(cleaned, enabled)
        if bare:
            name, args, (a, b) = bare
            calls.append((name, args))
            cleaned = cleaned[:a] + cleaned[b:]
    cleaned = re.sub(r"```\w*\s*```", "", cleaned).strip()
    return calls, cleaned


def bare_call(text, enabled):
    """(name, args, (start, end)) of a {"name": <enabled tool>, "parameters"|"arguments": {...}} that ends the text
    (only a code fence may follow)."""
    enabled = set(enabled or ())
    for m in re.finditer(r'\{\s*"name"\s*:', text):
        try:
            obj, end = _DEC.raw_decode(text, m.start())
        except ValueError:
            continue
        c = _call(obj)
        if not c or c[0] not in enabled or text[end:].strip().strip("`").strip():
            continue
        return c[0], c[1], (m.start(), end)
    return None


def has_markup(text):
    """True when text holds a tagged call (whatever the tool), for telling the user when tools are off."""
    return any(rx.search(text or "") for _k, rx in _TAGGED)
