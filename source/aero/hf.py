"""Hugging Face search, file listing and resumable downloads for GGUF models.

Any model can be searched for, not only GGUF repos: a repo with the original weights (safetensors) resolves to the
GGUF versions people have published of it (the Hub's base_model:quantized links), and when there is none Aero can
make its own (quantize.py) or run it with AirLLM. Downloads resume, use several connections for big files and are
checked against the SHA-256 the Hub publishes for every LFS file."""
import hashlib
import io
import json
import os
import re
import threading
import time
from pathlib import Path

import httpx

from .config import load_settings, models_dir
from . import gguf
from .gguf import SPLIT_RE

API = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
QUANT_RE = re.compile(r"(IQ\d_[A-Z0-9_]+|Q\d_K(?:_[SMLX]{1,2})?|Q\d_\d|Q\d_K|TQ\d_\d|MXFP4(?:_MOE)?|BF16|F16|F32|UD-[A-Z0-9_]+)",
                      re.I)
# bits per weight, used only to rank quants
BPW = {"Q2_K": 2.6, "Q3_K_S": 3.4, "Q3_K_M": 3.9, "Q3_K_L": 4.3, "Q4_0": 4.5, "Q4_K_S": 4.6, "Q4_K_M": 4.85,
       "Q4_1": 5.0, "Q5_0": 5.5, "Q5_K_S": 5.5, "Q5_K_M": 5.7, "Q6_K": 6.6, "Q8_0": 8.5, "F16": 16, "BF16": 16,
       "F32": 32, "MXFP4": 4.25, "MXFP4_MOE": 4.25, "IQ4_XS": 4.25, "IQ4_NL": 4.5, "IQ3_M": 3.7, "IQ3_XXS": 3.1,
       "IQ2_M": 2.7, "IQ2_XXS": 2.1, "IQ1_M": 1.75, "IQ1_S": 1.56, "IQ2_XS": 2.3, "IQ2_S": 2.5, "IQ3_XS": 3.3,
       "IQ3_S": 3.5, "Q2_K_L": 2.9, "Q2_K_XL": 3.0, "Q3_K_XL": 4.0, "Q4_K_L": 4.9, "Q4_K_XL": 4.9, "Q5_K_L": 5.8,
       "Q5_K_XL": 5.8, "Q6_K_L": 6.8, "Q6_K_XL": 6.8, "Q8_K_XL": 9.0, "TQ1_0": 1.7, "TQ2_0": 2.1, "Q1_0": 1.1,
       "Q2_0": 2.1}


def _headers():
    tok = load_settings().get("hf_token", "").strip()
    h = {"User-Agent": "Aero/1.0"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


TEXT_TASKS = {None, "text-generation", "image-text-to-text", "any-to-any", "audio-text-to-text",
              "text2text-generation", "conversational"}
SIZE_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])")
QUANTERS = ("bartowski", "unsloth", "lmstudio-community", "ggml-org", "Qwen", "google", "MaziyarPanahi",
            "mradermacher", "QuantFactory", "TheBloke", "second-state")


def _row(m):
    tags = m.get("tags") or []
    mid = m.get("id") or m.get("modelId")
    sz = SIZE_RE.search(mid.split("/")[-1])
    return {"id": mid, "downloads": m.get("downloads", 0), "likes": m.get("likes", 0),
            "updated": (m.get("lastModified") or m.get("createdAt") or "")[:10], "pipeline": m.get("pipeline_tag"),
            "gated": bool(m.get("gated")), "gguf": "gguf" in tags or mid.lower().endswith("-gguf"),
            "size_b": float(sz.group(1)) if sz else None}


def search(query: str, limit=40, scope="all"):
    """scope 'all': every model whose name matches (text models; the GGUF ones are marked), 'gguf': GGUF repos only.
    No query: what's trending in GGUF."""
    q = query.strip()
    params = {"limit": limit, "full": "false"}
    if scope == "gguf" or not q:
        params["filter"] = "gguf"
    if q:
        params.update(search=q, sort="downloads", direction=-1)
    else:
        params.update(sort="trendingScore", direction=-1)
    r = httpx.get(f"{API}/api/models", params=params, headers=_headers(), timeout=20, follow_redirects=True)
    r.raise_for_status()
    out = [_row(m) for m in r.json()]
    return [x for x in out if x["gguf"] or x["pipeline"] in TEXT_TASKS]


def gguf_versions(repo: str, limit=12):
    """GGUF versions of a repo with the original weights, best first: the same model (not fine-tunes of it) from
    well-known quantizers, then by downloads."""
    params = [("filter", f"base_model:quantized:{repo}"), ("filter", "gguf"), ("sort", "downloads"),
              ("direction", "-1"), ("limit", "40"), ("full", "false")]
    r = httpx.get(f"{API}/api/models", params=params, headers=_headers(), timeout=20, follow_redirects=True)
    r.raise_for_status()
    base = re.sub(r"[^a-z0-9]", "", repo.split("/")[-1].lower())
    rows = []
    for m in r.json():
        x = _row(m)
        name = re.sub(r"[-_.](i1|imat(rix)?|gguf)(?![a-z0-9])", "", x["id"].split("/")[-1].lower())
        name = re.sub(r"[^a-z0-9]", "", name)
        x["same"] = name == base or name.endswith(base)      # bartowski prefixes the org: Qwen_Qwen3-8B-GGUF
        x["known"] = x["id"].split("/")[0] in QUANTERS
        rows.append(x)
    rows.sort(key=lambda x: (not x["same"], not x["known"], -x["downloads"]))
    return rows[:limit]


def source_info(repo: str):
    """A repo with the original weights: parameter count, architecture and its weight files (for quantize.py and
    AirLLM)."""
    r = httpx.get(f"{API}/api/models/{repo}", params={"blobs": "true"}, headers=_headers(), timeout=30,
                  follow_redirects=True)
    if r.status_code in (401, 403):
        raise RuntimeError(GATED)
    r.raise_for_status()
    j = r.json()
    sib = j.get("siblings") or []
    weights = [s for s in sib if s["rfilename"].endswith(".safetensors")]
    if not weights:
        weights = [s for s in sib if re.search(r"pytorch_model.*\.bin$", s["rfilename"])]
    need = [s for s in sib if re.match(r"^(config\.json|generation_config\.json|tokenizer.*|special_tokens_map\.json|"
                                       r".*\.tiktoken|vocab\.json|merges\.txt|tokenizer\.model|chat_template.*|"
                                       r"preprocessor_config\.json|.*\.safetensors\.index\.json)$", s["rfilename"])]
    cfg = j.get("config") or {}
    st = j.get("safetensors") or {}
    return {"repo": repo, "library": j.get("library_name"), "pipeline": j.get("pipeline_tag"),
            "arch": (cfg.get("architectures") or [None])[0], "model_type": cfg.get("model_type"),
            "params": int(st.get("total") or 0), "dtypes": st.get("parameters") or {},
            "gated": bool(j.get("gated")), "license": (j.get("cardData") or {}).get("license") or "",
            "weights": [{"path": s["rfilename"], "size": s.get("size") or 0} for s in weights],
            "files": [s["rfilename"] for s in weights + need],
            "size": sum(s.get("size") or 0 for s in weights)}


def quant_of(name: str):
    base = Path(name).name
    if base.lower().endswith(".gguf"):
        base = base[:-5]            # not Path.stem: "Qwen3-1.7B-Q8_0" would lose ".7B-Q8_0"
    m = QUANT_RE.findall(base.upper())
    return m[-1].upper() if m else "?"


def list_files(repo: str):
    r = httpx.get(f"{API}/api/models/{repo}/tree/main", params={"recursive": "true"}, headers=_headers(),
                  timeout=30, follow_redirects=True)
    r.raise_for_status()
    files = [f for f in r.json() if f.get("type") == "file" and f["path"].lower().endswith(".gguf")]
    mmproj, groups = [], {}
    for f in files:
        size = (f.get("lfs") or {}).get("size") or f.get("size") or 0
        path = f["path"]
        if (f.get("lfs") or {}).get("oid"):
            _SHA[(repo, path)] = f["lfs"]["oid"]
        name = Path(path).name
        if "mmproj" in name.lower():
            mmproj.append({"path": path, "size": size})
            continue
        m = SPLIT_RE.match(name)
        key = (str(Path(path).parent / m.group(1)) if m else path)
        g = groups.setdefault(key, {"name": Path(key).name.replace(".gguf", ""), "parts": [], "size": 0})
        g["parts"].append(path)
        g["size"] += size
    out = []
    for g in groups.values():
        g["parts"].sort()
        g["quant"] = quant_of(g["name"])
        g["bpw"] = BPW.get(g["quant"].replace("UD-", ""), 0)
        g["path"] = g["parts"][0]
        out.append(g)
    out.sort(key=lambda g: g["size"])
    # prefer an f16/bf16 projector when several exist
    mmproj.sort(key=lambda f: (0 if re.search(r"f16|bf16", f["path"], re.I) else 1, f["size"]))
    return {"files": out, "mmproj": mmproj}


_INFO_CACHE = {}
_SHA = {}                    # (repo, path) -> the SHA-256 the Hub lists for that LFS file


def model_info(repo: str):
    """Repo facts from the Hub: task, tags, license, gating and the Hub's own GGUF summary."""
    if repo in _INFO_CACHE:
        return _INFO_CACHE[repo]
    r = httpx.get(f"{API}/api/models/{repo}", headers=_headers(), timeout=20, follow_redirects=True)
    r.raise_for_status()
    j = r.json()
    card = j.get("cardData") or {}
    out = {"pipeline": j.get("pipeline_tag") or card.get("pipeline_tag"), "tags": j.get("tags") or [],
           "license": card.get("license") or "", "gated": bool(j.get("gated")), "gguf": j.get("gguf") or {},
           "base_model": card.get("base_model")}
    _INFO_CACHE[repo] = out
    return out


_META_CACHE = {}


def remote_metadata(repo: str, path: str):
    """Read a remote GGUF's header by fetching only its first megabytes (more if the vocabulary is huge)."""
    key = (repo, path)
    if key in _META_CACHE:
        return _META_CACHE[key]
    kv = gguf.read_metadata(io.BytesIO(remote_header(repo, path)))
    _META_CACHE[key] = kv
    return kv


_HEAD_CACHE = {}


def remote_tensors(repo: str, path: str, size: int):
    """[(name, bytes)] of a remote GGUF file from its header alone (planner.py prices every quant from one of these)."""
    return gguf.tensors_from(io.BytesIO(remote_header(repo, path)), size)


def remote_header(repo: str, path: str):
    """The first megabytes of a remote GGUF: enough for its whole header (metadata and tensor table)."""
    key = (repo, path)
    if key in _HEAD_CACHE:
        return _HEAD_CACHE[key]
    url = f"{API}/{repo}/resolve/main/{path}"
    with httpx.Client(headers=_headers(), follow_redirects=True, timeout=60) as c:
        for mb in (6, 32, 96):
            want = mb * 2**20
            buf = bytearray()
            with c.stream("GET", url, headers={"Range": f"bytes=0-{want - 1}"}) as r:
                if r.status_code in (401, 403):
                    raise RuntimeError("gated or private repo")
                r.raise_for_status()
                for chunk in r.iter_bytes(1 << 20):
                    buf += chunk
                    if len(buf) >= want:     # server ignored Range: stop instead of pulling the whole file
                        break
            try:
                bio = io.BytesIO(bytes(buf[:want]))
                gguf.tensors_from(bio, 1 << 62)                    # the whole header, tensor table included
                data = bio.getvalue()[:bio.tell()]                 # keep the header only, not the whole window
                while len(_HEAD_CACHE) >= 16:
                    _HEAD_CACHE.pop(next(iter(_HEAD_CACHE)))
                _HEAD_CACHE[key] = data
                return data
            except EOFError:
                if len(buf) < want:          # that was the whole file
                    raise
    raise RuntimeError("GGUF header is unusually large")


def recommend(files, vram_mb, ram_mb):
    """Pick the highest-quality quant that fits entirely in VRAM with room for context;
    otherwise the best one that fits in VRAM + RAM."""
    budget = vram_mb - 3000
    ok = [f for f in files if f["size"] / 2**20 <= budget and 3.5 <= (f["bpw"] or 5) <= 8.5]
    if ok:
        return max(ok, key=lambda f: (f["bpw"], f["size"]))["name"]
    part = [f for f in files if f["size"] / 2**20 <= vram_mb + ram_mb * 0.6 and (f["bpw"] or 5) >= 3.0]
    if part:
        return max(part, key=lambda f: (min(f["bpw"], 5.0), -f["size"]))["name"]
    return files[0]["name"] if files else None


ADVISOR_DIR = "_advisor"   # tuning-advisor models live here, hidden from "Your models"


def sha256_file(path, cancel=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel.is_set():
                raise RuntimeError("Cancelled")
            b = f.read(8 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def local_path(repo: str, path: str, root=None) -> Path:
    return Path(root or models_dir()) / repo.replace("/", "__") / path


SEG_MIN = 64 * 2**20     # smaller files download over one connection
CONNECTIONS = 8          # Hugging Face's CDN caps each connection (about 10 MB/s measured); 8 ran 3x faster


class _Gated(RuntimeError):
    pass


GATED = "This repo is gated or private. Accept its license on huggingface.co and add a token in Settings."


def _write_all(f, data):
    mv = memoryview(data)
    while mv:
        mv = mv[f.write(mv):]


def _get_single(url, part, on_bytes, cancel):
    """One connection, resuming from whatever part already holds."""
    have = part.stat().st_size if part.exists() else 0
    hdr = {"Range": f"bytes={have}-"} if have else {}
    with httpx.Client(headers=_headers(), follow_redirects=True, timeout=60) as c, \
            c.stream("GET", url, headers=hdr, timeout=httpx.Timeout(60, read=300)) as r:
        if r.status_code in (401, 403):
            raise _Gated(GATED)
        r.raise_for_status()
        if have and r.status_code != 206:
            have = 0
            on_bytes(-part.stat().st_size)
        with open(part, "ab" if have else "wb", buffering=0) as f:
            for chunk in r.iter_bytes(4 * 2**20):
                if cancel.is_set():
                    raise RuntimeError("Download cancelled")
                _write_all(f, chunk)
                on_bytes(len(chunk))


def _get_parallel(url, part, size, on_bytes, cancel):
    """Several ranged connections into one preallocated file. part.json records how far each range got, so an
    interrupted download resumes where every range stopped. Returns False if the server ignores ranges."""
    state_f = part.with_name(part.name + ".json")
    segs = None
    if part.exists() and state_f.exists():
        try:
            st = json.loads(state_f.read_text())
            if st.get("size") == size and part.stat().st_size == size:
                segs = st["segs"]
        except Exception:
            segs = None
    if segs is None:
        with open(part, "wb") as f:
            f.truncate(size)
        step = -(-size // CONNECTIONS)
        segs = [[a, min(a + step, size) - 1] for a in range(0, size, step)]
    on_bytes(size - sum(max(0, e - p + 1) for p, e in segs))
    lock = threading.Lock()
    errors = []

    def worker(i):
        tries = 0
        while segs[i][0] <= segs[i][1] and not cancel.is_set() and not errors:
            try:
                with httpx.Client(headers=_headers(), follow_redirects=True, timeout=60) as c, \
                        open(part, "r+b", buffering=0) as f, \
                        c.stream("GET", url, headers={"Range": f"bytes={segs[i][0]}-{segs[i][1]}"},
                                 timeout=httpx.Timeout(60, read=120)) as r:
                    if r.status_code in (401, 403):
                        raise _Gated(GATED)
                    if r.status_code != 206:
                        raise LookupError("ranges not supported")
                    f.seek(segs[i][0])
                    for chunk in r.iter_bytes(2**20):
                        if cancel.is_set():
                            return
                        chunk = chunk[:segs[i][1] - segs[i][0] + 1]
                        _write_all(f, chunk)       # unbuffered: on disk (OS cache) before the position moves
                        with lock:
                            segs[i][0] += len(chunk)
                        on_bytes(len(chunk))
                        if segs[i][0] > segs[i][1]:
                            break
                tries = 0
            except (_Gated, LookupError) as e:
                errors.append(e)
                return
            except Exception as e:  # noqa: BLE001  (dropped connection, timeout: retry this range)
                tries += 1
                if tries > 6:
                    errors.append(e)
                    return
                time.sleep(min(2 ** tries, 30))

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(len(segs))]
    for t in threads:
        t.start()
    last = 0.0
    while any(t.is_alive() for t in threads):
        time.sleep(0.25)
        if time.time() - last > 2:
            last = time.time()
            with lock:
                state_f.write_text(json.dumps({"size": size, "segs": segs}))
    with lock:
        state_f.write_text(json.dumps({"size": size, "segs": segs}))
    if cancel.is_set():
        raise RuntimeError("Download cancelled")
    if errors:
        if isinstance(errors[0], LookupError):
            return False
        raise errors[0]
    if any(pos <= end for pos, end in segs):
        raise RuntimeError("download stopped early; run it again to resume")
    state_f.unlink(missing_ok=True)
    return True


def download(repo: str, paths, emit, cancel, root=None):
    """Download files with resume, big files over several connections. emit({'type':'progress',...})."""
    total = 0
    sizes = {}
    with httpx.Client(headers=_headers(), follow_redirects=True, timeout=60) as c:
        for p in paths:
            url = f"{API}/{repo}/resolve/main/{p}"
            r = c.head(url, follow_redirects=False)       # Hugging Face's own answer carries the real size
            if r.status_code in (401, 403):
                raise RuntimeError(GATED)
            size = r.headers.get("x-linked-size")
            if not size:
                if r.is_redirect:
                    r = c.head(url)
                size = r.headers.get("content-length") if r.status_code < 400 else 0
            sizes[p] = int(size or 0)
            total += sizes[p]
    prog = {"done": 0, "sess": 0, "last": 0.0, "file": ""}
    lock = threading.Lock()
    t0 = time.time()

    def on_bytes(n):
        with lock:
            prog["done"] += n
            prog["sess"] += max(n, 0)
            now = time.time()
            if now - prog["last"] > 0.4:
                prog["last"] = now
                emit({"type": "progress", "file": prog["file"], "done": prog["done"], "total": total,
                      "speed": prog["sess"] / max(now - t0, 1e-3)})

    for p in paths:
        dest = local_path(repo, p, root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and (not sizes[p] or dest.stat().st_size == sizes[p]):
            prog["done"] += dest.stat().st_size
            continue
        prog["file"] = p
        url = f"{API}/{repo}/resolve/main/{p}"
        part = dest.with_name(dest.name + ".part")
        base = prog["done"]
        try:
            ok = False
            if sizes[p] >= SEG_MIN and (not part.exists() or part.with_name(part.name + ".json").exists()):
                ok = _get_parallel(url, part, sizes[p], on_bytes, cancel)
                if not ok:                       # no range support: start over on one connection
                    part.unlink(missing_ok=True)
                    part.with_name(part.name + ".json").unlink(missing_ok=True)
                    prog["done"] = base
            if not ok:
                if part.exists():
                    on_bytes(part.stat().st_size)
                _get_single(url, part, on_bytes, cancel)
        except _Gated as e:
            raise RuntimeError(str(e))
        if sizes[p] and part.stat().st_size != sizes[p]:
            raise RuntimeError(f"{p}: downloaded {part.stat().st_size} of {sizes[p]} bytes; run the download again to resume")
        want = _SHA.get((repo, p))
        if want and load_settings().get("verify_downloads", True):
            emit({"type": "verify", "file": p})
            got = sha256_file(part, cancel)
            if got != want:
                part.unlink(missing_ok=True)
                raise RuntimeError(f"{p} is corrupt (SHA-256 doesn't match what Hugging Face lists); it was deleted, "
                                   "download it again")
        part.replace(dest)
        prog["done"] = base + dest.stat().st_size
    emit({"type": "progress", "file": "", "done": total, "total": total, "speed": 0})
    return [local_path(repo, p, root) for p in paths]
