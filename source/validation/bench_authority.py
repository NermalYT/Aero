"""Choose the authority model for full automation: run Aero's authority exam (autonomy.EXAM, 32 decisions with known
answers, a third of them prompt-injection or damage traps) against small GGUF models on the CPU, the way Aero runs
the authority, and report accuracy, wrongly allowed actions and decision time.

    set AERO_HOME=C:\\Users\\you\\AeroTest\\home        (throwaway)
    set AERO_LLAMA_DIR=C:\\Users\\you\\AeroTest\\llama   (a llama.cpp install, used read-only)
    python validation\\bench_authority.py model1.gguf [model2.gguf ...] [--threads 8] --out authority.json

A wrongly allowed action matters more than a wrongly denied one: the table is sorted by false allows, then score,
then time.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def run(models, threads, out, repeat=1):
    from aero import autonomy, router
    from aero.config import AUTHORITY_PORT
    from aero.engine import LlamaServer, build_args, supports
    results = []
    for m in models:
        srv = LlamaServer(AUTHORITY_PORT + 40, "authority-bench")
        cfg = {"ctx": autonomy.CTX, "ngl": 0, "kv": "f16", "fa": True, "threads": threads, "ub": 512}
        args = build_args(m, cfg, srv.port) + ["--jinja"]
        if supports("--device"):
            args += ["--device", "none"]
        if supports("--reasoning-budget"):
            args += ["--reasoning-budget", "0"]
        if supports("--threads-batch"):
            args += ["--threads-batch", str(router.physical_cores())]
        print(f"\n== {Path(m).name}", flush=True)
        srv.start(args, {"CUDA_VISIBLE_DEVICES": "-1", "GGML_VK_VISIBLE_DEVICES": ""})
        ok, why = srv.wait_ready(240)
        if not ok:
            print("  could not start:", why, srv.log_tail(6)[-300:])
            results.append({"model": Path(m).name, "error": why})
            continue
        try:
            try:
                autonomy.judge("hi", autonomy.EXAM[0][3], url=srv.url)      # warm the prompt cache
            except Exception as e:  # noqa: BLE001
                print("  warm-up failed:", e, flush=True)
            runs = []
            for _ in range(repeat):
                runs.append(autonomy.exam(url=srv.url, emit=lambda t: print("  " + t, flush=True)))
            r = runs[-1]
            r.update(model=Path(m).name, threads=threads, size_mb=round(Path(m).stat().st_size / 2**20),
                     stable=all(x["rows"][i]["got"] == r["rows"][i]["got"] for x in runs for i in range(len(r["rows"]))))
            results.append(r)
            print(f"  -> {r['score']}/{r['total']} right, {r['false_allow']} wrongly allowed, "
                  f"{r['false_deny']} wrongly denied, median {r['ms_median']} ms", flush=True)
        finally:
            srv.stop()
    results.sort(key=lambda r: (r.get("false_allow", 99), -r.get("score", 0), r.get("ms_median") or 1e9))
    Path(out).write_text(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "results": results}, indent=1),
                         encoding="utf-8")
    print("\nmodel | right | wrongly allowed | wrongly denied | median ms | size MB")
    for r in results:
        if "error" in r:
            print(f"{r['model']} | error: {r['error']}")
        else:
            print(f"{r['model']} | {r['score']}/{r['total']} | {r['false_allow']} | {r['false_deny']} | "
                  f"{r['ms_median']} | {r['size_mb']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--repeat", type=int, default=1, help="run the exam several times to check the answers are stable")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    run(a.models, a.threads, a.out, a.repeat)
