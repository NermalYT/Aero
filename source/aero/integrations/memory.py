"""Memory+ workspace: vectorize-io/hindsight (MIT) built into Aero.

Hindsight is an agent memory server: retain extracts facts, entities, times and relations from text; recall runs
semantic, keyword, graph and time retrieval together; reflect reasons over a bank's memories; consolidation turns
facts into observations and mental models. Aero keeps its own memory (memory.py) as the everyday store; Hindsight
is the deeper, per-project memory the model can use when a task needs it.

Aero installs `hindsight-api-slim[embedded-db,local-onnx]` into its own virtual environment (data/toolchains/
hindsight/venv, so its pinned dependencies never touch Aero's), starts `hindsight-api` bound to 127.0.0.1 on its
own port, ONNX embeddings on the CPU, the RRF reranker (no torch), and Aero's own llama.cpp server as its LLM
(OpenAI-compatible, strict JSON schema for small models). Aero starts Hindsight's embedded PostgreSQL (pg0, pgvector
included) itself, on 127.0.0.1 only, with its data in data/toolchains/hindsight/pg0 (Hindsight's pg0:// URLs can't
move the data folder; pg0 keeps the PostgreSQL program in ~/.pg0/installation). First start downloads the small
embedding model (intfloat/multilingual-e5-small) from Hugging Face.
"""
import json
import subprocess
import sys
import time

import httpx

from . import Pack, Requirement, llm_adapter, register, toolchain as tc
from .. import action_results, capabilities
from ..config import IS_WIN, LLAMA_PORT
from ..tools import tool

HOME = tc.ROOT / "hindsight"
VENV = HOME / "venv"
PORT = LLAMA_PORT + 707
URL = f"http://127.0.0.1:{PORT}"
PG_NAME = "aero-hindsight"
PG_START = """import sys
from pg0 import Pg0
pg = Pg0(name=sys.argv[1], username="hindsight", password="hindsight", database="hindsight", data_dir=sys.argv[2],
         config={"listen_addresses": "127.0.0.1"})
info = pg.info()
print((info if info.running else pg.start()).uri)
"""
PG_STOP = "import sys\nfrom pg0 import Pg0\nPg0(name=sys.argv[1]).stop()\n"
_srv = {"proc": None, "db": False}


def _py():
    return VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")


def _exe():
    return VENV / ("Scripts/hindsight-api.exe" if IS_WIN else "bin/hindsight-api")


def install(emit=None, cancel=None):
    tc.require_online("install Hindsight from PyPI")
    if not _py().exists():
        if emit:
            emit({"type": "log", "text": "Creating Hindsight's own Python environment…"})
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True, creationflags=tc.NO_WINDOW)
    if emit:
        emit({"type": "log", "text": "pip install hindsight-api-slim[embedded-db,local-onnx] (a few hundred MB)…"})
    r = tc.run([str(_py()), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                "hindsight-api-slim[embedded-db,local-onnx]"], timeout=3600, cancel=cancel, network=True)
    if r["rc"] != 0:
        raise RuntimeError("pip install failed: " + (r["err"] or r["out"])[-800:])
    tc.mark("hindsight", version=_version())
    return f"Hindsight {_version()}"


def _version():
    r = tc.run([str(_py()), "-c", "import importlib.metadata as m; print(m.version('hindsight-api-slim'))"], timeout=60)
    return r["out"].strip() or "?"


def _ok():
    return _exe().exists(), (tc.installed().get("hindsight", {}).get("version") or "installed") if _exe().exists() \
        else "not installed"


def running():
    try:
        return httpx.get(URL + "/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def database():
    """Start Hindsight's PostgreSQL (pg0) on 127.0.0.1 with its data in Aero's folder; returns its postgresql:// URL."""
    r = tc.run([str(_py()), "-I", "-c", PG_START, PG_NAME, str(HOME / "pg0")], timeout=300)
    uri = (r["out"].strip().splitlines() or [""])[-1].strip()
    if r["rc"] != 0 or not uri.startswith("postgresql://"):
        raise RuntimeError("Hindsight's database didn't start: " + (r["err"] or r["out"]).strip()[-800:])
    _srv["db"] = True
    return uri


def start(timeout=240):
    if running():
        return URL
    if not _exe().exists():
        raise RuntimeError("Hindsight isn't installed (integration_install hindsight).")
    base, ok = tc.local_model()
    if ok:
        base = llm_adapter.url(base)              # reflect forces named tools, which llama-server alone ignores
    HOME.mkdir(parents=True, exist_ok=True)
    env = {"HINDSIGHT_API_HOST": "127.0.0.1", "HINDSIGHT_API_PORT": str(PORT),
           "HINDSIGHT_API_DATABASE_URL": database(),
           "HINDSIGHT_API_LLM_PROVIDER": "openai" if ok else "none", "HINDSIGHT_API_LLM_BASE_URL": base,
           "HINDSIGHT_API_LLM_API_KEY": "aero-local", "HINDSIGHT_API_LLM_MODEL": "local",
           "HINDSIGHT_API_LLM_STRICT_SCHEMA": "true", "HINDSIGHT_API_LLM_TIMEOUT": "600",
           "HINDSIGHT_API_LLM_MAX_CONCURRENT": "2", "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "onnx",
           "HINDSIGHT_API_RERANKER_PROVIDER": "rrf", "HINDSIGHT_API_OTEL_TRACES_ENABLED": "false",
           "HF_HOME": str(HOME / "hf")}
    _srv["proc"] = tc.start([str(_exe())], cwd=HOME, env_extra=env, log=HOME / "server.log")
    t0 = time.time()
    while time.time() - t0 < timeout:
        if running():
            return URL
        if _srv["proc"].poll() is not None:
            break
        time.sleep(1)
    tail = (HOME / "server.log").read_text(encoding="utf-8", errors="replace")[-1200:] if (HOME / "server.log").exists() else ""
    raise RuntimeError("Hindsight didn't start:\n" + tail)


def stop():
    p = _srv.get("proc")
    if p and p.poll() is None:
        tc.kill(p)
    _srv["proc"] = None
    if _srv.get("db"):
        tc.run([str(_py()), "-I", "-c", PG_STOP, PG_NAME], timeout=60)
        _srv["db"] = False


def _bank(b):
    return "".join(ch for ch in (b or "aero") if ch.isalnum() or ch in "-_")[:60] or "aero"


def api(method, path, body=None, timeout=600):
    start()
    r = httpx.request(method, URL + path, json=body, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"Hindsight {r.status_code}: {r.text[:400]}")
    return r.json()


@tool("hindsight_retain", "Store something in a Hindsight memory bank (facts, entities and times are extracted from "
      "it by the local model). bank: a project or person name; context: where it came from.", "integrations",
      {"content": {"type": "string"}, "bank": {"type": "string"}, "context": {"type": "string"}}, ["content"],
      summary=lambda a: f"{a.get('bank') or 'aero'}: {a.get('content', '')[:60]}")
def hindsight_retain(ctx, content, bank="aero", context=""):
    try:
        r = api("POST", f"/v1/default/banks/{_bank(bank)}/memories",
                {"items": [{"content": content, **({"context": context} if context else {})}], "async": False})
    except (RuntimeError, httpx.HTTPError) as e:
        return {"text": str(e), "error": True}
    env = action_results.make("api", verified=True, method="Hindsight accepted the retain", changed={"bank": bank})
    return action_results.attach({"text": f"Retained in bank '{_bank(bank)}': {json.dumps(r)[:600]}"}, env)


@tool("hindsight_recall", "Find what a Hindsight bank knows about something (semantic, keyword, graph and time "
      "search together).", "integrations",
      {"query": {"type": "string"}, "bank": {"type": "string"}, "max_tokens": {"type": "integer"}}, ["query"],
      summary=lambda a: f"{a.get('bank') or 'aero'}: {a.get('query', '')[:60]}")
def hindsight_recall(ctx, query, bank="aero", max_tokens=2048):
    try:
        r = api("POST", f"/v1/default/banks/{_bank(bank)}/memories/recall", {"query": query,
                                                                              "max_tokens": int(max_tokens or 2048)})
    except (RuntimeError, httpx.HTTPError) as e:
        return {"text": str(e), "error": True}
    res = r.get("results") or []
    if not res:
        return f"Bank '{_bank(bank)}' has nothing about that."
    return "\n".join(f"- {x.get('text') or x.get('content') or json.dumps(x)[:300]}"
                     + (f" ({x.get('type')})" if x.get("type") else "") for x in res[:30])


@tool("hindsight_reflect", "Ask a Hindsight bank a question it answers by reasoning over everything it remembers.",
      "integrations", {"query": {"type": "string"}, "bank": {"type": "string"}}, ["query"],
      summary=lambda a: f"{a.get('bank') or 'aero'}: {a.get('query', '')[:60]}")
def hindsight_reflect(ctx, query, bank="aero"):
    try:
        r = api("POST", f"/v1/default/banks/{_bank(bank)}/reflect", {"query": query})
    except (RuntimeError, httpx.HTTPError) as e:
        return {"text": str(e), "error": True}
    return r.get("text") or json.dumps(r)[:2000]


capabilities.register("hindsight_retain", ["memory.learn"], "local_write")
capabilities.register("hindsight_recall", ["memory.recall"], "local_read")
capabilities.register("hindsight_reflect", ["memory.recall"], "local_read", cost="medium")


def _panel():
    if not running():
        return {"running": False}
    try:
        banks = httpx.get(URL + "/v1/default/banks", timeout=5).json()
    except (httpx.HTTPError, ValueError):
        banks = None
    return {"running": True, "url": URL, "banks": banks}


register(Pack("hindsight", "Hindsight", "memory",
              "Memory banks per project: facts, entities and times extracted, recalled and reasoned over",
              "https://github.com/vectorize-io/hindsight", "MIT",
              triggers=[r"\b(memory bank|hindsight|what do you know about (my|the|this) (project|team|client)|"
                        r"remember everything about|knowledge base about)\b"],
              tools=["hindsight_retain", "hindsight_recall", "hindsight_reflect"],
              requirements=[Requirement("hindsight", "Hindsight server (own Python environment, a few hundred MB)",
                                        _ok, install=install, size_mb=600)],
              panel=_panel, stop=stop,
              notes="Retain and reflect make calls to the local model; quality and speed with small models are not "
                    "benchmarked by the project (its results use hosted models)."))
