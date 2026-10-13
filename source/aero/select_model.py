"""Select (1.2.6): from a search result to a running, tuned model in one click.

plan_for(repo) works out what to fetch and how to run it on this PC, before anything is downloaded:
  - a GGUF repo: planner.plan over all its quants
  - a repo with the original weights: the best published GGUF version of it (hf.gguf_versions, also of the model it
    is based on); when nobody published one, a quant Aero makes itself (quantize.py); when llama.cpp can't convert
    the architecture either, AirLLM (layer-by-layer streaming at full precision, slow)
The server's /api/select job then downloads (or makes) the file, tunes it with the automatic VRAM budget and loads it.
"""
from . import gguf, hf, planner, quantize


def resolve(repo):
    """(repo with GGUF files, its listing, note). The listing has no files when nothing GGUF exists."""
    d = hf.list_files(repo)
    if d["files"]:
        return repo, d, ""
    versions, of = [], repo
    try:
        versions = hf.gguf_versions(repo)
        if not versions:
            base = hf.model_info(repo).get("base_model")
            base = base[0] if isinstance(base, list) and base else base
            if isinstance(base, str) and base and base != repo:
                versions, of = hf.gguf_versions(base), base
    except Exception:  # noqa: BLE001
        versions = []
    org = of.split("/")[0].lower()
    versions = sorted(versions, key=lambda v: v["id"].split("/")[0].lower() != org)    # the model's own GGUF first
    for v in versions[:3]:
        try:
            d2 = hf.list_files(v["id"])
        except Exception:  # noqa: BLE001
            continue
        if d2["files"]:
            note = f"{repo} has only the original weights, so Aero uses {v['id']}, the GGUF version"
            if of != repo:
                note += f" of {of}, the model it is based on"
            return v["id"], d2, note
    return repo, d, ""


def _meta(rid, d):
    """Header facts and the tensor layout template, read from the smallest quant (a few MB of its header)."""
    small = d["files"][0]
    kv = hf.remote_metadata(rid, small["parts"][0])
    meta = gguf.summarize_kv(kv)
    if not meta.get("params"):
        try:
            meta["params"] = int((hf.model_info(rid).get("gguf") or {}).get("total") or 0)
        except Exception:  # noqa: BLE001
            pass
    tensors = None
    if len(small["parts"]) == 1:
        try:
            tensors = hf.remote_tensors(rid, small["parts"][0], small["size"])
        except Exception:  # noqa: BLE001
            tensors = None
    return meta, planner.template_for(meta, tensors, small["size"])


def plan_for(repo, h, budget_mb, min_tps=5.0):
    """Everything Select needs to show and do, as a JSON-able dict with route "gguf", "make" or "airllm"."""
    rid, d, note = resolve(repo)
    ram = planner.ram_for_weights(h)
    if not d["files"]:
        return _no_gguf(repo, h, budget_mb, ram)
    meta, template = _meta(rid, d)
    mm = d["mmproj"][0] if d["mmproj"] else None
    r = planner.plan(d["files"], template, h, budget_mb, ram, min_tps, (mm["size"] / 2**20) if mm else 0.0)
    pick = r["pick"]
    local = False
    if pick:
        local = all(hf.local_path(rid, p).exists() for p in pick["parts"])
    r.update(route="gguf", repo=rid, asked=repo, note=note, local=local,
             mmproj=(mm["path"] if mm and r["with_proj"] else None),
             model={"name": meta.get("name") or rid.split("/")[-1], "arch": meta.get("arch"),
                    "params": meta.get("params"), "moe": bool(meta.get("experts")), "ctx_train": meta.get("ctx_train"),
                    "layers": meta.get("n_layer")},
             download_mb=round(sum([pick["size"] if pick and not local else 0, (mm or {}).get("size", 0)
                                    if r["with_proj"] else 0]) / 2**20))
    if r["level"] != "ok":
        r["alternatives"] = alternatives(repo, meta, h, budget_mb)
    return r


def _no_gguf(repo, h, budget_mb, ram):
    """Nobody published a GGUF: make one sized for this PC, or run it with AirLLM when llama.cpp can't."""
    try:
        src = hf.source_info(repo)
    except Exception as e:  # noqa: BLE001
        return {"route": "none", "repo": repo, "asked": repo, "warning": f"Could not read this repo: {e}"}
    params = src.get("params") or int((src.get("size") or 0) / 2)
    if params and quantize.arch_supported(src.get("arch")) is not False:
        q, imx, why = quantize.pick(params, budget_mb, ram, bool(h.get("gpus")))
        bpw = dict((x[0], x[1]) for x in quantize.LADDER)[q]
        return {"route": "make", "repo": repo, "asked": repo, "quant": q, "imatrix": imx,
                "summary": f"Nobody has published a GGUF of this model, so Aero makes one: {q}. {why[0].upper() + why[1:]}.",
                "level": "ok", "warning": None, "download_mb": round((src.get("size") or 0) / 2**20),
                "make_mb": round(params * bpw / 8 / 2**20), "model": {"name": repo.split("/")[-1],
                                                                      "arch": src.get("arch"), "params": params}}
    return {"route": "airllm", "repo": repo, "asked": repo, "level": "slow",
            "summary": "llama.cpp can't run this architecture, so Aero runs it with AirLLM: it streams one layer at a "
                       "time from disk at full precision, so any size fits, at seconds per token.",
            "warning": "AirLLM is very slow (seconds per token). A GGUF model of a similar size will be far faster.",
            "model": {"name": repo.split("/")[-1], "arch": src.get("arch"), "params": params}}


def alternatives(repo, meta, h, budget_mb, limit=3):
    """Smaller GGUF models of the same family that would run fully on this GPU (rough size check)."""
    q = planner.family_query(repo)
    if not q:
        return []
    params_b = (meta.get("params") or 0) / 1e9 or planner.size_b(repo)
    room_mb = budget_mb if h.get("gpus") else planner.ram_for_weights(h)
    try:
        rows = hf.search(q, limit=40, scope="gguf")
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        b = r.get("size_b") or planner.size_b(r["id"])
        if not b or r["id"] == repo or (params_b and b >= params_b * 0.8):
            continue
        if b * 1e9 * 4.8 / 8 / 2**20 + 1500 > room_mb:          # Q4_K_M + context would not fit fully
            continue
        out.append({"id": r["id"], "size_b": b, "downloads": r.get("downloads", 0)})
    out.sort(key=lambda r: (-r["size_b"], -r["downloads"]))
    seen, res = set(), []
    for r in out:
        if r["size_b"] in seen:
            continue
        seen.add(r["size_b"])
        res.append(r)
        if len(res) >= limit:
            break
    return res
