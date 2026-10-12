"""Measure big-model plans (aero/bigmodel.py) on real hardware: for a GGUF and a VRAM budget, load each plan in
llama-server, then measure load time, VRAM the server really took (nvidia-smi), prompt speed (about 1,000 tokens)
and generation speed (128 tokens, twice). Nothing in Aero's data folder is changed.

    set AERO_HOME=C:\\Users\\you\\AeroTest\\home        (throwaway)
    set AERO_LLAMA_DIR=C:\\Users\\you\\AeroTest\\llama   (a llama.cpp install, used read-only)
    python validation\\bench_bigmodel.py model.gguf --budget-mb 13000 [--ctx 8192] [--strategies layers,ffn] --out run.json
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROMPT = ("Summarise the following notes in five bullet points.\n" +
          " ".join(f"Note {i}: the quarterly figures for region {i % 7} rose by {i % 13} percent while costs moved "
                   f"by {i % 5} percent, and the team asked for a review of item {i}." for i in range(60)))


def vram_used():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        return sum(int(x) for x in out.split())
    except Exception:  # noqa: BLE001
        return None


def measure(path, p, ctx, threads, port, spec=None):
    from aero.engine import LlamaServer, build_args
    cfg = {"ctx": ctx, "kv": "q8_0", "fa": True, "threads": threads, "ub": 512, **p["cfg"]}
    srv = LlamaServer(port, "bigmodel-bench")
    before = vram_used()
    t0 = time.time()
    srv.start(build_args(path, cfg, port, spec=spec))
    ok, why = srv.wait_ready(1800)
    load_s = round(time.time() - t0, 1)
    if not ok:
        tail = srv.log_tail(12)
        srv.stop()
        return {"strategy": p["strategy"], "ok": False, "error": why, "log": tail[-1500:]}
    used = vram_used()
    rows = []
    try:
        for i in range(2):
            r = httpx.post(srv.url + "/completion", json={"prompt": PROMPT + f" (run {i})", "n_predict": 128,
                                                         "temperature": 0, "cache_prompt": False}, timeout=3600).json()
            tm = r.get("timings") or {}
            rows.append({"pp": round(tm.get("prompt_per_second") or 0, 2), "tg": round(tm.get("predicted_per_second") or 0, 2),
                         "prompt_n": tm.get("prompt_n"), "gen_n": tm.get("predicted_n")})
    finally:
        srv.stop()
        time.sleep(3)
    return {"strategy": p["strategy"] + ("+" + spec[1] if spec else ""), "ok": True, "cfg": p["cfg"], "spec": spec,
            "planned_gpu_mb": p["gpu_mb"],
            "planned_cpu_mb": p["cpu_mb"], "vram_mb": (used - before) if used is not None and before is not None else None,
            "load_s": load_s, "runs": rows, "pp": rows[-1]["pp"], "tg": rows[-1]["tg"], "note": p["note"]}


def main():
    import psutil
    from aero import bigmodel
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--budget-mb", type=int, required=True)
    ap.add_argument("--ctx", type=int, default=8192)
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--strategies", default="")
    ap.add_argument("--draft", default="", help="a small same-family draft model for speculative decoding")
    ap.add_argument("--ngram", action="store_true", help="n-gram speculation (no extra VRAM)")
    ap.add_argument("--n-max", type=int, default=8)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    info = bigmodel.inspect(a.model)
    ram = psutil.virtual_memory().available // 2**20
    plans = bigmodel.plan(info, a.budget_mb, ram, a.ctx)
    if a.strategies:
        want = a.strategies.split(",")
        plans = [p for p in plans if p["strategy"] in want]
    print(f"{Path(a.model).name}: {info['total'] / 2**30:.1f} GB, {len(info['layers'])} layers, MoE {info['moe']}; "
          f"budget {a.budget_mb} MB, RAM free {ram} MB", flush=True)
    res = []
    for p in plans:
        print(f"  {p['strategy']}: planned GPU {p['gpu_mb']} MB, RAM {p['cpu_mb']} MB …", flush=True)
        spec = None
        if a.draft:
            spec = ["--spec-type", "draft-simple", "-md", a.draft, "-ngld", "99", "--spec-draft-n-max", str(a.n_max)]
        elif a.ngram:
            spec = ["--spec-type", "ngram-mod"]
        r = measure(a.model, p, a.ctx, a.threads, 8290, spec)
        res.append(r)
        print("   ", {k: r.get(k) for k in ("ok", "vram_mb", "load_s", "pp", "tg", "error")}, flush=True)
    Path(a.out).write_text(json.dumps({"model": Path(a.model).name, "size_gb": round(info["total"] / 2**30, 2),
                                       "budget_mb": a.budget_mb, "ctx": a.ctx, "ram_free_mb": ram,
                                       "at": time.strftime("%Y-%m-%d %H:%M:%S"), "results": res}, indent=1),
                           encoding="utf-8")


if __name__ == "__main__":
    main()
