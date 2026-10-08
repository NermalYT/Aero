"""Model chooser that Update-Aero.bat runs after updating the app.

Scans this PC first (GPUs of any vendor, VRAM, RAM, CPU) and fits every model to it.
1. Lists the CPU decision-router models with their published scores and speed, you pick one (Enter = keep the
   current one, or the one recommended for this PC on a first install). It is downloaded and benchmarked on this
   PC's CPU with llama-bench, and the fastest thread count is saved.
2. Lists the main models with their Artificial Analysis intelligence score and, for this PC, the quant that fits
   and where it runs (fully on the GPU, MoE experts in RAM, or the CPU). Enter = the recommended one when you have
   no model yet, otherwise skip. Main models are auto-tuned for your memory the first time Aero loads them.

    python -m aero.setup_models                     interactive
    python -m aero.setup_models --router minicpm5-2b-q4 --main skip
"""
import argparse
import os
import sys
import threading
import time
from pathlib import Path

from . import catalog, hardware, hf, models, router
from .config import IS_WIN, load_settings, save_settings

if IS_WIN:
    os.system("")                 # turn on ANSI colours in the Windows console
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

C = {"aqua": "\033[96m", "blue": "\033[94m", "dim": "\033[2m", "b": "\033[1m", "gold": "\033[93m", "ok": "\033[92m",
     "bad": "\033[91m", "x": "\033[0m"}


def c(text, *styles):
    return "".join(C[s] for s in styles) + str(text) + C["x"]


def rule(title):
    print()
    print(c("  " + "─" * 74, "blue"))
    print(c(f"   {title}", "b", "aqua"))
    print(c("  " + "─" * 74, "blue"))


def ask(prompt, valid, default, interactive):
    if not interactive:
        print(f"  {prompt} {c(default or 'skip', 'dim')}  (automatic)")
        return default
    while True:
        try:
            a = input(f"  {prompt} ").strip().lower()
        except EOFError:
            return default
        if not a:
            return default
        if a in valid:
            return a
        print(c(f"  Type one of: {', '.join(sorted(valid, key=lambda v: (len(v), v)))} (or press Enter).", "gold"))


class Progress:
    def __init__(self):
        self.last = 0
        self.finished = False

    def __call__(self, ev):
        if ev.get("type") != "progress" or self.finished or time.time() - self.last < 0.5 and ev["done"] < ev["total"]:
            return
        self.finished = ev["done"] >= ev["total"]
        self.last = time.time()
        pct = ev["done"] / ev["total"] * 100 if ev.get("total") else 0
        bar = "█" * int(pct / 4) + "░" * (25 - int(pct / 4))
        sys.stdout.write(f"\r  {c(bar, 'aqua')} {pct:5.1f}%  {ev['done'] / 2**30:5.2f} / {ev['total'] / 2**30:.2f} GB"
                         f"  {ev.get('speed', 0) / 2**20:6.1f} MB/s ")
        sys.stdout.flush()
        if ev["done"] >= ev["total"]:
            sys.stdout.write("\n")


def download(entry, root=None, quant=None, budget_mb=None):
    parts, mm = catalog.resolve(entry, quant, budget_mb)
    files = parts + ([mm] if mm else [])
    print(f"  Downloading {c(entry['name'], 'b')} from huggingface.co/{entry['repo']} ({', '.join(p.split('/')[-1] for p in files)})")
    paths = hf.download(entry["repo"], files, Progress(), threading.Event(), root=root)
    return paths[:len(parts)], (paths[-1] if mm else None)


def show_hardware(hw):
    rule("This PC")
    print(f"  {hardware.summary(hw)}")
    if hw["gpus"] and not hw.get("vram_measured", True):
        print(c("   This GPU doesn't report live VRAM use to Windows, so Aero measures its own models from llama.cpp's", "dim"))
        print(c("   memory report and assumes about 1 GB for the desktop.", "dim"))
    if not hw["gpus"]:
        print(c("   No dedicated GPU: models run on the CPU from system RAM. Small and MoE models are the quick ones.", "dim"))


# ------------------------------------------------------------------------------------------------ router

def show_routers(cur_id, rec_id):
    rule("1 / 2   Decision router  ·  runs on your CPU and RAM, never touches the GPU")
    print(c("   It reads every request first and picks the tools, thinking mode, cloud review and a short plan", "dim"))
    print(c("   for your main model. Everything runs on this PC. (Jev is not listed: it only runs on TypeSafe's servers.)", "dim"))
    for i, r in enumerate(catalog.ROUTERS, 1):
        tags = []
        if r["id"] == rec_id:
            tags.append(c("★ recommended for this PC", "gold"))
        if r["id"] == cur_id:
            tags.append(c("● current", "ok"))
        print()
        print(f"  {c(f'[{i}]', 'aqua', 'b')} {c(r['name'], 'b')} · {r['org']} · {r['params']} · {r['quant']} {r['size_gb']} GB · "
              f"{r['license']} · {r['released']}  {'  '.join(tags)}")
        print(f"      {c(r['tag'], 'blue')}")
        print("      Scores: " + " · ".join(f"{s['name']} {c(s['value'], 'b')}" + (f" ({s['note']})" if s.get("note") else "")
                                         for s in r["scores"]))
        print(f"      Speed:  {r['speed_est']} (re-measured on your CPU after download)")


def choose_router(arg, interactive, hw):
    cfg = router.config()
    cur = cfg.get("id") if router.model_path() else None
    rec = catalog.recommend_router(hw)["id"]
    show_routers(cur, rec)
    ids = {str(i): r["id"] for i, r in enumerate(catalog.ROUTERS, 1)}
    default = cur or rec
    if arg:
        pick = arg if arg in ids.values() else ids.get(arg)
        if not pick:
            raise SystemExit(f"Unknown router '{arg}'. Choose from: {', '.join(ids.values())}")
    else:
        dn = next(k for k, v in ids.items() if v == default)
        a = ask(f"Router [1-{len(ids)}], Enter = {dn} ({catalog.router_by_id(default)['name']}{', keep' if cur else ''}):",
                set(ids), dn, interactive)
        pick = ids[a]
    entry = catalog.router_by_id(pick)
    if pick == cur and cfg.get("bench"):
        print(f"\n  Keeping {c(entry['name'], 'b')}.")
        show_bench(cfg["bench"])
        a = ask("Re-run the CPU benchmark? [y/N]:", {"y", "n", "yes", "no"}, "n", interactive)
        if a not in ("y", "yes"):
            return entry, cfg["bench"]
        path = router.model_path()
    else:
        root = Path(load_settings()["models_dir"]) / router.ROUTER_DIR
        paths, _ = download(entry, root)
        path = paths[0]
    print(f"\n  Benchmarking {c(entry['name'], 'b')} on your CPU (llama-bench, several thread counts)…")
    router.stop()
    res = router.benchmark(path, emit=lambda t: print(c("  " + t.strip(), "dim")))
    router.save_config({"id": entry["id"], "name": entry["name"], "path": str(path), "threads": res["threads"],
                        "batch_threads": res["batch_threads"], "bench": res})
    if load_settings().get("router_model"):
        save_settings({"router_model": ""})
    show_bench(res)
    return entry, res


def show_bench(b):
    if not b or not b.get("rows"):
        return
    print()
    print(c("   threads   prompt tok/s   output tok/s   one decision", "dim"))
    for r in b["rows"]:
        used = [w for w, k in (("generate", "threads"), ("prompt", "batch_threads")) if r["threads"] == b.get(k)]
        mark = c("  ← " + " + ".join(used), "ok") if used else ""
        dm = f"≈{r['decision_ms']} ms" if r.get("decision_ms") else "–"
        print(f"   {r['threads']:>7}   {r['pp']:>12.1f}   {r['tg']:>12.1f}   {dm:>12}{mark}")
    print(f"  Measured {b.get('at', '')}: generates with {c(b['threads'], 'b')} threads ({c(b.get('tg'), 'b')} tok/s), "
          f"reads prompts with {c(b.get('batch_threads'), 'b')} ({c(b.get('pp'), 'b')} tok/s).")
    print(f"  One routing decision ≈ {c(str(b.get('decision_ms')) + ' ms', 'b')} (300 new prompt tokens + 120 generated).")


# ------------------------------------------------------------------------------------------------ main model

FIT_LABEL = {"gpu": ("fully on the GPU", "ok"), "moe": ("GPU + experts in RAM", "aqua"), "cpu": ("on the CPU", "ok"),
             "split": ("GPU + CPU, slow", "gold"), "slow": ("CPU, very slow", "gold"), "no": ("too big for this PC", "bad")}


def choose_main(arg, interactive, hw):
    gpus = hw.get("gpus") or []
    where = (" + ".join(f"{g['name']} {g['total_mb'] / 1024:.0f} GB" for g in gpus)) if gpus else "CPU only"
    rule(f"2 / 2   Main model  ·  fitted to this PC ({where}, {hw['ram_total_mb'] / 1024:.0f} GB RAM)")
    print(c("   Intelligence = Artificial Analysis Intelligence Index (higher is smarter; frontier cloud models score about 70).", "dim"))
    print(c("   The quant and size shown are what fits this PC. Aero tunes the model the first time you load it and", "dim"))
    print(c("   measures its real speed here.", "dim"))
    rec_entry, rec_plan = catalog.recommend_main(hw)
    plans = {m["id"]: catalog.plan(m, hw) for m in catalog.MAIN_MODELS}
    for i, m in enumerate(catalog.MAIN_MODELS, 1):
        p = plans[m["id"]]
        label, style = FIT_LABEL[p["fit"]]
        tags = [c("★ recommended for this PC", "gold")] if m["id"] == rec_entry["id"] else []
        print()
        print(f"  {c(f'[{i}]', 'aqua', 'b')} {c(m['name'], 'b')} · {m['org']} · {m['params']} · "
              f"{p['quant']} ≈{p['size_gb']:.1f} GB · {'vision' if m.get('vision') else 'text only'} · "
              f"{c(label, style)}  {'  '.join(tags)}")
        aa = m.get("aa")
        print(f"      Intelligence: {c(aa if aa is not None else 'not rated', 'b', 'aqua')}" +
              (f" ({m['aa_note']})" if m.get("aa_note") else "") + f"   ·   Speed: {m['speed']}")
        print(f"      {c(m['tag'], 'blue')}: {m['notes']}")
    ids = {str(i): m["id"] for i, m in enumerate(catalog.MAIN_MODELS, 1)}
    have = any(m.get("exists") for m in models.all_models())
    rec_n = next(k for k, v in ids.items() if v == rec_entry["id"])
    if arg:
        if arg in ("skip", "none", "0"):
            return None
        if arg in ("rec", "recommended", "auto"):
            pick = rec_entry["id"]
        else:
            pick = arg if arg in ids.values() else ids.get(arg)
        if not pick:
            raise SystemExit(f"Unknown model '{arg}'. Choose from: {', '.join(ids.values())}, rec or skip")
    else:
        default = "skip" if have else rec_n
        prompt = (f"Download a main model? [1-{len(ids)}], Enter = skip (keep the ones you have):" if have else
                  f"Download a main model? [1-{len(ids)}], Enter = {rec_n} ({rec_entry['name']}), s = skip:")
        a = ask(prompt, set(ids) | {"s", "skip"}, default, interactive)
        if a in ("s", "skip"):
            return None
        pick = ids[a]
    entry = catalog.main_by_id(pick)
    p = plans[pick]
    if p["fit"] == "no":
        print(c(f"\n  {entry['name']} is too big for this PC even at its smallest quant; downloading the listed "
                f"{entry['quant']} anyway because you picked it.", "gold"))
    print()
    budget = p["size_gb"] * catalog.GB * 1.05 if p["fit"] != "no" else None
    parts, mm = download(entry, quant=p["quant"], budget_mb=budget)
    from .hf import quant_of
    q = quant_of(parts[0])
    m = models.add(str(parts[0]), repo=entry["repo"], mmproj=str(mm) if mm else None, name=f"{entry['name']} · {q}")
    print(f"  {c('Added', 'ok')} {m['name']} to Your models. Pick it in Aero; the first load asks for a memory limit "
          "(with a suggestion for this PC) and tunes it.")
    return entry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--router", help="router id or list number (default: ask)")
    ap.add_argument("--main", help="main model id, list number, rec (recommended for this PC) or skip (default: ask)")
    ap.add_argument("--yes", action="store_true", help="no questions: keep/recommended router, no main model")
    a = ap.parse_args()
    interactive = sys.stdin.isatty() and not a.yes
    print()
    print(c("   ≋ Aero model setup ≋", "b", "aqua"))
    hw = hardware.snapshot()
    show_hardware(hw)
    try:
        r, bench = choose_router(a.router, interactive, hw)
    except KeyboardInterrupt:
        print("\n  Skipped the router setup.")
        return 0
    except Exception as e:  # noqa: BLE001
        print(c(f"\n  Router setup failed: {e}", "bad"))
        print("  Aero still works without it (every tool is sent to the main model). Try again in Settings → Router.")
        r = None
    try:
        main_entry = choose_main(a.main if a.main else ("skip" if a.yes else None), interactive, hw)
    except KeyboardInterrupt:
        main_entry = None
    except Exception as e:  # noqa: BLE001
        print(c(f"\n  Model download failed: {e}", "bad"))
        print("  You can download it later from Aero's model picker.")
        main_entry = None
    rule("Done")
    if r:
        print(f"  Router: {c(r['name'], 'b')}" + (f" · {bench['threads']}/{bench.get('batch_threads')} threads · "
                                                f"≈{bench.get('decision_ms')} ms per decision" if bench else ""))
    if main_entry:
        print(f"  Main model: {c(main_entry['name'], 'b')}, tuned for this PC on first load")
    return 0


if __name__ == "__main__":
    sys.exit(main())
