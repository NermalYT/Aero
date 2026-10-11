"""Measure Remote Mode on real hardware: load a model fully on the GPU, then with the Remote Mode split, and report
what llama.cpp and the GPU driver say. Nothing in Aero's data folder is changed (it runs a throwaway llama-server on
its own port and writes logs under AERO_HOME).

    set AERO_HOME=C:\\Users\\you\\AeroTest\\home        (a throwaway folder)
    set AERO_LLAMA_DIR=C:\\Aero\\llama                  (an existing llama.cpp install, used read-only)
    python validation\\measure_remote_mode.py <model.gguf> [--ctx 8192] [--target 0.8] [--kv q8_0] [--json out.json]

Prints, for the normal and the Remote Mode configuration: the planned share of weight bytes on the GPU (from the GGUF
tensor sizes), the measured share (llama.cpp's "model buffer size" lines), the model's GPU and CPU buffers, the KV
cache and compute buffers on the GPU, VRAM in use (nvidia-smi or the Linux AMD sysfs counter), load time, and a short
generation speed check. Real hardware results only: nothing is estimated where a measurement is missing.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from aero import gguf, hardware, vram_policy  # noqa: E402
from aero.config import LLAMA_PORT  # noqa: E402
from aero.engine import LlamaServer, build_args, server_version  # noqa: E402


def speed(url, n=64):
    r = httpx.post(url + "/completion", json={"prompt": "Write a short paragraph about lighthouses.", "n_predict": n,
                                              "temperature": 0, "cache_prompt": False}, timeout=300).json()
    t = r.get("timings") or {}
    return round(t.get("predicted_per_second") or 0, 1)


def run(srv, model, cfg, label):
    before = hardware.settle_vram(timeout=8)
    t0 = time.time()
    srv.start(build_args(model, cfg, srv.port, final=False))
    ok, why = srv.wait_ready(900)
    load_s = round(time.time() - t0, 1)
    if not ok:
        tail = srv.log_tail(20)
        srv.stop()
        return {"label": label, "ok": False, "why": why, "log_tail": tail}
    time.sleep(1.0)
    used = hardware.gpu_used_mb()
    log = srv.log_path.read_text(encoding="utf-8", errors="replace")
    b = vram_policy.buffers(log)
    tg = speed(srv.url)
    srv.stop()
    after_stop = hardware.settle_vram(timeout=8)
    return {"label": label, "ok": True, "cfg": cfg, "load_s": load_s, "vram_before_mb": before, "vram_loaded_mb": used,
            "vram_model_total_mb": (used - before) if used is not None and before is not None else None,
            "vram_after_stop_mb": after_stop, "measured_weight_share": b["weights_gpu_share"],
            "gpu_buffers_mb": b["gpu"], "cpu_buffers_mb": b["cpu"], "devices": b["devices"],
            "offloaded": b["offloaded"], "decode_tok_s": tg}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--ctx", type=int, default=8192)
    ap.add_argument("--kv", default="q8_0")
    ap.add_argument("--target", type=float, default=0.8)
    ap.add_argument("--json")
    a = ap.parse_args()
    lb = gguf.layer_bytes(a.model, mtp_used=False)          # the probe runs without speculative decoding
    normal = {"ctx": a.ctx, "ngl": 999, "kv": a.kv, "fa": True, "ub": 512}
    pl = vram_policy.plan(lb, normal, a.target)
    srv = LlamaServer(LLAMA_PORT + 20, "remote-probe")
    hw = hardware.snapshot()
    rep = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "model": Path(a.model).name, "llama_server": server_version(),
           "gpu": hw.get("gpu_name"), "vram_total_mb": hw.get("vram_total_mb"), "cpu": hw.get("cpu"),
           "ram_total_mb": hw.get("ram_total_mb"), "target": a.target,
           "layers": {"n_layer": lb["n_layer"], "total_mb": round(lb["total"] / 2**20), "embd_mb": round(lb["embd"] / 2**20),
                      "output_mb": round(lb["output"] / 2**20), "layer_mb_min": round(min(lb["layers"]) / 2**20),
                      "layer_mb_max": round(max(lb["layers"]) / 2**20), "experts_mb": round(sum(lb["experts"]) / 2**20)},
           "plan": {k: v for k, v in pl.items() if k != "cfg"}, "remote_cfg": pl.get("cfg"),
           "naive_ngl": int(lb["n_layer"] * a.target),
           "naive_share": round(vram_policy.fraction(lb, int(lb["n_layer"] * a.target)), 4)}
    rep["normal"] = run(srv, a.model, normal, "normal")
    if pl.get("cfg"):
        rep["remote"] = run(srv, a.model, pl["cfg"], "remote")
        if rep["normal"].get("ok") and rep["remote"].get("ok") and rep["normal"]["vram_model_total_mb"] is not None:
            rep["vram_freed_mb"] = rep["normal"]["vram_model_total_mb"] - rep["remote"]["vram_model_total_mb"]
    print(json.dumps(rep, indent=1))
    if a.json:
        Path(a.json).write_text(json.dumps(rep, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
