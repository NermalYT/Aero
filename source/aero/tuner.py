"""Per-model auto-tuner, driven one step at a time by the advisor model.

Flow (runs once per model, result cached):
  1. The user types a VRAM limit for the model (no default, no timer). What's left over hosts the advisor.
  2. The user picks a depth: Short 10 / Medium 25 / Long 50 trials, or Full (until converged).
  3. Trial #1 is the estimator's predicted optimum for this model + hardware.
  4. Each next trial changes exactly ONE knob by ONE step (ctx, GPU layers, KV precision, ubatch, threads)
     from an earlier trial. The advisor picks which step; illegal, repeated and provably-failing steps are
     never offered. Every trial is a real llama-server run: load, VRAM measured against the limit,
     prompt + generation speed measured after a warm-up.
  5. The last part of the budget re-tests the top configs and averages them (measurement noise).
  6. The advisor names its final pick; the verifier checks it against the measurements and the goal.
  7. The winner gets a deep-context run (half the context filled) to prove it holds up under real load.
  Dense models bigger than the GPU get a second way to split: instead of whole layers on the CPU, only the
  feed-forward weights of some layers sit in RAM while attention and the KV cache of every layer stay on the GPU
  (bigmodel.py, llama-server -ot). Both splits are seeded and measured; the "split" move swaps one for the other at
  about the same VRAM, and the faster one wins.
  8. Tool calling is baked in: the winner (and, on Long / Full, a runner-up of similar speed with a different KV
     precision or context) takes Aero's tool-calling exam (tool_eval.py: BFCL cases + Aero requests), at two
     temperatures; the result decides the temperature Aero uses for tool steps and whether, for requests that
     plainly need an action, Aero forces the first call straight away (forced_call.py) instead of waiting for the
     model to say "I can't".

Full depth really is full: KV down to q4_0, finer context steps, threads even with every layer on the GPU, and when
the improving moves run out, a sweep over the untested combinations next to the best ones until 8 trials in a row
change nothing (or the 120-trial cap).
"""
import hashlib
import json
import statistics
import time
from pathlib import Path

import httpx

from . import bigmodel, gguf, hardware
from .config import TRIAL_PORT, read_store, write_store
from .engine import supports, LlamaServer, build_args, classify_failure, server_version, spec_args

LADDER = [4096, 8192, 12288, 16384, 24576, 32768, 40960, 49152, 57344, 65536, 81920, 98304, 131072,
          196608, 262144, 393216, 524288, 1048576]
DEPTHS = {"short": 10, "medium": 25, "long": 50, "full": None}
FULL_CAP = 120
PATIENCE = 8                 # Full: stop sweeping after this many trials in a row that don't change the best
MODE_TEXT = {
    "balanced": "Balanced: the largest context that keeps at least 80% of the best generation speed; if that is "
                "under 32k, a 32k+ config that keeps at least 50% wins instead (agent work needs 32k).",
    "max_context": "Max context: the largest context that keeps at least 50% of the best generation speed.",
    "max_speed": "Max speed: the fastest generation; context up to 32k is enough.",
}

_WORDS = ("the system routes each request through a small model that picks a specialist, "
          "runs tools, reviews the output and returns a final answer; latency, memory bandwidth, "
          "cache locality and quantization all matter. ").split()


def bench_prompt(n_words=760, salt=0):
    out = []
    for i in range(n_words):
        out.append(_WORDS[(i * 7 + i // 13 + salt) % len(_WORDS)])
        if i % 19 == 18:
            out.append(str(1000 + i + salt))
    return "Summarize the following notes.\n\n" + " ".join(out)


class Cancelled(Exception):
    pass


# ---- cache ---------------------------------------------------------------------------------------

def cache_key(model_path, mmproj, hw, spec=None, pins=None):
    """Same model file + vision projector + GPU + RAM (+ speculative decoding, which costs VRAM, + the settings the
    user pinned, 1.2.65) = same tuning.
    The llama.cpp version is stored in the entry instead of the key, so an update doesn't force a re-tune (the UI
    flags it as tuned on an older build)."""
    p = Path(model_path)
    meta = f"{p.name}|{gguf.split_parts(p)[0].stat().st_size}|{bool(mmproj)}|{hw.get('gpu_name')}|" \
           f"{hw.get('vram_total_mb')}|{hw.get('ram_total_mb', 0) // 1024}"
    if spec:
        meta += "|spec=" + " ".join(spec)
    pins = clean_pins(pins)
    if pins:
        meta += "|pins=" + json.dumps(pins, sort_keys=True)
    return hashlib.sha1(meta.encode()).hexdigest()[:20]


# 1.2.65: settings the user can pin per model (Tuning settings). Search knobs are fixed to the pinned value; the
# others are passed to llama-server as they are.
KV_CHOICES = ("f16", "q8_0", "q4_0", "q8_0/q4_0", "f16/q8_0", "q5_1", "q5_0")
PIN_KNOBS = {
    "ctx": "context length (tokens)", "kv": "KV cache type (K/V)", "cpu_layers": "layers (or expert blocks) kept on "
    "the CPU", "split": "how layers are split: layers or ffn", "ub": "micro-batch", "threads": "threads",
    "fa": "flash attention", "batch": "batch", "threads_batch": "threads for prompt processing", "mmap": "map the file",
    "mlock": "lock the model in RAM", "rope_scale": "YaRN context extension factor",
}


def clean_pins(pins):
    """Only known knobs with usable values; anything else is dropped (the settings API accepts any JSON)."""
    out = {}
    for k, v in (pins or {}).items():
        if k not in PIN_KNOBS or v is None or v == "" or v == "auto":
            continue
        try:
            if k in ("ctx", "cpu_layers", "ub", "threads", "batch", "threads_batch"):
                if isinstance(v, bool):
                    continue
                v = int(v)
                lo, hi = {"ctx": (2048, 1 << 22), "cpu_layers": (0, 1024), "ub": (32, 65536),
                          "threads": (1, 512), "batch": (32, 65536), "threads_batch": (1, 512)}[k]
                if not lo <= v <= hi:
                    continue
            elif k == "rope_scale":
                v = float(v)
                if not 1 < v <= 8:
                    continue
            elif k in ("fa", "mmap", "mlock"):
                v = bool(v) if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
            elif k == "kv":
                if v not in KV_CHOICES:
                    continue
            elif k == "split":
                if v not in ("layers", "ffn"):
                    continue
        except (TypeError, ValueError):
            continue
        out[k] = v
    if out.get("fa") is False and "kv" in out and gguf.kv_types(out["kv"])[1] != "f16":
        del out["kv"]          # llama.cpp only quantizes the V cache with flash attention on
    return out


def cached(key):
    return read_store("tune_cache.json", {}).get(key)


def warm_start(model_path, meta, hw):
    """A previous tuning to start from (1.2.80): this model file's own (another llama.cpp build, limit, pins or
    depth), else the newest tuning of a model with the same architecture and layer count on this GPU (another quant
    of it). Returns (entry, why) or (None, "")."""
    cache = read_store("tune_cache.json", {})
    name = Path(model_path).name
    entries = [e for e in cache.values() if isinstance(e, dict) and e.get("knobs")]
    own = [e for e in entries if e.get("model_file") == name]
    if own:
        e = max(own, key=lambda e: e.get("tuned_at") or "")
        return e, f"this model's earlier tuning ({e.get('tuned_at')}: {e.get('desc')}, {e.get('tg')} tok/s)"
    same = [e for e in entries if (e.get("meta") or {}).get("arch") == meta.get("arch")
            and (e.get("meta") or {}).get("n_layer") == meta.get("n_layer")
            and (e.get("hw") or {}).get("gpu_name") == hw.get("gpu_name") and (e.get("meta") or {}).get("size_bytes")]
    if same:
        e = max(same, key=lambda e: e.get("tuned_at") or "")
        return e, f"the tuning of {e.get('model_file')} on this PC ({e.get('desc')}, {e.get('tg')} tok/s)"
    return None, ""


def store(key, entry):
    c = read_store("tune_cache.json", {})
    c[key] = entry
    write_store("tune_cache.json", c)


def forget(model_path=None):
    c = read_store("tune_cache.json", {})
    if model_path is None:
        c = {}
    else:
        name = Path(model_path).name
        c = {k: v for k, v in c.items() if v.get("model_file") != name}
    write_store("tune_cache.json", c)


# ---- search space ----------------------------------------------------------------------------------

class Space:
    """The knobs and their ladders. A config is {ctx, k, kv, ub, t[, f]}; k = layers (dense) or expert
    blocks (MoE) kept on the CPU; with f (dense only) k counts layers whose feed-forward weights alone are in RAM."""

    def __init__(self, meta, hw, settings, gpu, full=False, pins=None):
        self.meta, self.gpu, self.full = meta, gpu, full
        self.pins = pins = clean_pins(pins)
        self.n = meta["n_layer"]
        self.moe = meta["experts"] > 0
        cap = int(settings.get("context_cap") or 0)
        self.rope = float(pins.get("rope_scale") or 1)
        if self.rope > 1 and not supports("--rope-scaling"):
            self.rope = 1.0
        trained = int(meta["ctx_train"] * self.rope)              # YaRN stretches the trained context
        ctx_max = max(2048, min(cap, trained) if cap else trained)
        self.ctx = [c for c in LADDER if c < ctx_max] + [ctx_max]
        low = settings.get("allow_q4_kv") or full
        # the mixed cache (K q8_0, V q4_0) sits between q4_0 and q8_0: keys are the more sensitive half
        self.kv = (["q4_0", "q8_0/q4_0"] if low else []) + ["q8_0", "f16"]
        self.ub = [256, 512, 1024, 2048]
        phys, logical = hw["cores"], hw["threads"]
        self.thr = sorted({max(2, phys // 2), phys, logical})
        self.phys = phys
        if pins.get("ctx"):
            self.ctx = [min(int(pins["ctx"]), ctx_max)]
        if pins.get("kv"):
            self.kv = [pins["kv"]]
        if pins.get("fa") is False:          # without flash attention llama.cpp refuses a quantized V cache
            self.kv = [v for v in self.kv if gguf.kv_types(v)[1] == "f16"] or ["f16"]
        if pins.get("ub"):
            self.ub = [int(pins["ub"])]
        if pins.get("threads"):
            self.thr = [int(pins["threads"])]
            self.phys = self.thr[0]
        self.pin_k = pins.get("cpu_layers")
        self.pin_split = pins.get("split")
        self.ffn = False             # set by the tuner: a dense model whose feed-forward can be split off
        self.equiv = None            # tuner: the config of the other split kind with about the same VRAM

    @staticmethod
    def key(c):
        return (c["ctx"], c["k"], c["kv"], c["ub"], c["t"], bool(c.get("f")))

    def cpu_layers(self, c):
        return (not self.gpu) or c["k"] > 0

    def neighbors(self, c):
        out = []

        def step(lst, val, d):
            if val not in lst:
                return None
            j = lst.index(val) + d
            return lst[j] if 0 <= j < len(lst) else None
        for d, nm in ((1, "ctx+"), (-1, "ctx-")):
            v = step(self.ctx, c["ctx"], d)
            if v:
                out.append((nm, {**c, "ctx": v}))
        if self.gpu and self.pin_k is None:
            if c["k"] > 0:
                out.append(("gpu+", {**c, "k": c["k"] - 1}))
            if c["k"] < self.n:
                out.append(("gpu-", {**c, "k": c["k"] + 1}))
        if self.gpu:
            e = self.equiv(c) if (self.ffn and self.equiv and c["k"] > 0 and not self.pin_split
                                  and self.pin_k is None) else None
            if e:
                out.append(("split", e))
        for d, nm in ((1, "kv+"), (-1, "kv-")):
            v = step(self.kv, c["kv"], d)
            if v:
                out.append((nm, {**c, "kv": v}))
        if c["kv"] == "q8_0" and "q4_0" in self.kv and "q8_0/q4_0" in self.kv:
            out.append(("kv--", {**c, "kv": "q4_0"}))       # past the mixed step (1.2.65), so q4_0 is still one move away
        for d, nm in ((1, "ub+"), (-1, "ub-")):
            v = step(self.ub, c["ub"], d)
            if v:
                out.append((nm, {**c, "ub": v}))
        if self.cpu_layers(c) or self.full:
            for d, nm in ((1, "threads+"), (-1, "threads-")):
                v = step(self.thr, c["t"], d)
                if v:
                    out.append((nm, {**c, "t": v}))
        return out

    def apply_pins(self, c):
        """A config with the pinned placement (layers on the CPU, kind of split)."""
        c = dict(c)
        if self.pin_k is not None and self.gpu:
            c["k"] = min(self.n, int(self.pin_k))
        if self.pin_split == "ffn" and self.ffn and not self.moe:
            c["f"] = True
        elif self.pin_split == "layers":
            c.pop("f", None)
        return c

    def server_cfg(self, c):
        p = self.pins
        s = {"ctx": c["ctx"], "kv": c["kv"], "fa": p.get("fa", True), "ub": c["ub"], "b": p.get("batch", 2048)}
        if p.get("threads_batch"):
            s["tb"] = p["threads_batch"]
        if p.get("mlock"):
            s["mlock"] = True
        if p.get("mmap") is False:
            s["no_mmap"] = True
        if self.rope > 1:
            s["rope_scale"], s["yarn_orig_ctx"] = self.rope, self.meta["ctx_train"]
        if not self.gpu:
            s["ngl"] = 0
        elif self.moe:
            s["ngl"] = 999
            if c["k"]:
                s["ncmoe"] = c["k"]
        elif c.get("f"):
            s["ngl"] = 999
            if c["k"]:
                s["ot"] = [bigmodel.ffn_override(range(c["k"]))]
        else:
            s["ngl"] = 999 if c["k"] == 0 else self.n - c["k"]
        if self.cpu_layers(c) or self.full or "threads" in p:
            s["threads"] = c["t"]
        return s

    def describe(self, c):
        if not self.gpu:
            g = "CPU only"
        elif self.moe:
            g = f"gpu experts {self.n - c['k']}/{self.n}"
        elif c.get("f"):
            g = f"gpu {self.n}/{self.n}, ffn {self.n - c['k']}/{self.n}"
        else:
            g = f"gpu {self.n - c['k']}/{self.n}"
        s = f"ctx {c['ctx']:,} | {g} | kv {c['kv']} | ub {c['ub']}"
        if self.cpu_layers(c):
            s += f" | {c['t']} threads"
        return s


# ---- the tuner ---------------------------------------------------------------------------------------

class Tuner:
    def __init__(self, model, mmproj, settings, limit_mb, depth, advisor, emit, cancel, others_mb=None, pins=None):
        self.model = Path(model)
        self.mmproj = Path(mmproj) if mmproj else None
        self.settings, self.emit, self.cancel = settings, emit, cancel
        self.meta = gguf.summarize(self.model)
        self.hw = hardware.snapshot()
        self.gpu = bool(self.hw["gpus"])
        self.live_vram = self.hw.get("vram_measured", True)    # False: AMD/Intel on Windows, measured from the log
        self.mode = settings.get("tune_mode", "balanced")
        self.limit = float(limit_mb)
        self.depth = depth if depth in DEPTHS else "medium"
        self.budget = DEPTHS[self.depth] or FULL_CAP
        self.advisor = advisor if (advisor and advisor.ready) else None
        self.space = Space(self.meta, self.hw, settings, self.gpu, full=self.depth == "full", pins=pins)
        self.warm, self.warm_why = warm_start(self.model, self.meta, self.hw) \
            if settings.get("tune_warm_start", True) else (None, "")
        self.ffn_frac = 0.0
        if self.gpu and not self.space.moe:
            try:
                info = bigmodel.inspect(self.model)
                self.ffn_frac = sum(x["ffn"] for x in info["layers"]) / max(1, info["total"])
            except Exception:  # noqa: BLE001
                pass
        self.space.ffn = self.ffn_frac > 0.2
        self.space.equiv = self.equiv
        self.best_since = 0          # trials since the best changed (Full's patience)
        self.best_key = None
        self.server = LlamaServer(TRIAL_PORT, "tune")
        self.spec = spec_args(self.model, settings)      # trials carry it too, so its VRAM counts against the limit
        self.trials = []
        self.decisions = []
        self.calib = 1.0
        if self.warm and self.warm.get("calib"):
            self.calib = min(2.0, max(0.5, float(self.warm["calib"])))   # last time's memory-estimate correction
        self.size_mb = self.meta["size_bytes"] / 2**20
        self.mmproj_mb = (self.mmproj.stat().st_size / 2**20) if self.mmproj and self.mmproj.exists() else 0
        self.vram_total = self.hw["vram_total_mb"]
        self.others_mb = others_mb if others_mb is not None else (self.hw["gpus"][0]["used_mb"] if self.gpu else 0)
        # what the model can really use: the user's limit, or less when other apps + the advisor leave less free
        if self.gpu:
            adv_mb = self.advisor.vram_mb if self.advisor else 0
            self.cap = max(256.0, min(self.limit, self.vram_total - 64 - self.others_mb - adv_mb))
        else:
            self.cap = max(256.0, min(self.limit, self.hw["ram_total_mb"] - 1024))
        self.load_timeout = 120 + int(self.size_mb / 1024 * 15)
        self.prompt = bench_prompt()
        self.tok_per_char = 0.27

    # ---- helpers ---------------------------------------------------------------------------------
    @staticmethod
    def check_cancel(cancel):
        if cancel.is_set():
            raise Cancelled()

    def log(self, msg):
        self.emit({"type": "log", "text": msg})

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def est_raw(self, c):
        m = self.meta
        kv_all = gguf.kv_mb(m, c["ctx"], c["kv"])
        if not self.gpu:
            return self.size_mb + kv_all + 300 + self.mmproj_mb
        n, k = self.space.n, c["k"]
        if self.space.moe:
            w, kv_frac = self.size_mb * (1 - 0.9 * k / n), 1.0
        elif c.get("f"):
            w, kv_frac = self.size_mb * (1 - self.ffn_frac * k / n), 1.0
        else:
            w, kv_frac = self.size_mb * (n - k) / n, (n - k) / n
        kv = kv_all * kv_frac
        compute = 300 + c["ub"] / 512 * 180 * (m["n_embd"] / 4096) + c["ctx"] / 1024 * 1.5
        return w + kv + compute + self.mmproj_mb

    def est(self, c):
        return self.est_raw(c) * self.calib

    def equiv(self, c):
        """The same config with the other kind of split, keeping VRAM at or under what c needs; None when it can't
        (the feed-forward split keeps every layer's KV cache on the GPU, so a huge context may not fit that way)."""
        f = not c.get("f")
        cap = self.est_raw(c)
        k = next((k for k in range(0, self.space.n + 1) if self.est_raw({**c, "f": f, "k": k}) <= cap), None)
        if k is None:
            return None
        out = {**c, "k": k}
        if f:
            out["f"] = True
        else:
            out.pop("f", None)
        return out

    def recalibrate(self):
        ratios = [t["mem_mb"] / self.est_raw(t["cfg"]) for t in self.trials
                  if t.get("mem_mb") and t["mem_mb"] > 200 and self.est_raw(t["cfg"]) > 0]
        if ratios:
            self.calib = min(2.0, max(0.5, statistics.median(ratios)))

    def tried(self, c):
        k = Space.key(c)
        return any(Space.key(t["cfg"]) == k for t in self.trials)

    def predicted_fail(self, c):
        """Memory only grows with ctx, GPU layers, ubatch and KV precision: if a config failed on memory,
        anything at least as big fails too."""
        kvr = self.space.kv.index
        for t in self.trials:
            if t["ok"] or t.get("kind") != "mem":
                continue
            f = t["cfg"]
            if bool(c.get("f")) != bool(f.get("f")):
                continue
            if c["ctx"] >= f["ctx"] and c["k"] <= f["k"] and c["ub"] >= f["ub"] and kvr(c["kv"]) >= kvr(f["kv"]):
                return True
        return False

    # ---- ranking (the verifier) ----------------------------------------------------------------------
    def ranked(self):
        P = [t for t in self.trials if t["ok"] and not t.get("retest")]
        if not P:
            return []
        mx = max(t["tg"] for t in P)
        kvr = self.space.kv.index
        if self.mode == "max_speed":
            pool = [t for t in P if t["tg"] >= mx * 0.95]
            pool.sort(key=lambda t: (min(t["cfg"]["ctx"], 32768), kvr(t["cfg"]["kv"]), t["tg"], t["pp"]), reverse=True)
        else:
            floor = 0.5 if self.mode == "max_context" else 0.8
            pool = [t for t in P if t["tg"] >= mx * floor]
            pool.sort(key=lambda t: (t["cfg"]["ctx"], kvr(t["cfg"]["kv"]), t["tg"], t["pp"]), reverse=True)
            if self.mode == "balanced" and pool and pool[0]["cfg"]["ctx"] < 32768:
                agent = [t for t in P if t["cfg"]["ctx"] >= 32768 and t["tg"] >= mx * 0.5]
                if agent:
                    agent.sort(key=lambda t: (t["cfg"]["ctx"], -t["tg"]))  # smallest 32k+ context, fastest
                    pool = agent[:1] + pool
        rest = [t for t in sorted(P, key=lambda t: (t["cfg"]["ctx"], t["tg"]), reverse=True) if t not in pool]
        return pool + rest

    def best(self):
        r = self.ranked()
        return r[0] if r else None

    # ---- measuring ---------------------------------------------------------------------------------
    def _completion(self, prompt, n_predict, timeout=900):
        r = httpx.post(self.server.url + "/completion", timeout=timeout, json={
            "prompt": prompt, "n_predict": n_predict, "temperature": 0, "top_k": 1, "cache_prompt": False,
            "ignore_eos": True})
        r.raise_for_status()
        return r.json().get("timings", {})

    def bench(self):
        self._completion("Hello", 4, 300)                         # warm-up (CUDA graphs, caches)
        t = self._completion(self.prompt, 96)
        return float(t.get("prompt_per_second") or 0), float(t.get("predicted_per_second") or 0)

    def run_config(self, c):
        """Start llama-server with config c and measure it. Returns a result dict (no bookkeeping)."""
        self.check()
        res = {"ok": False}
        t0 = time.time()
        before = hardware.settle_vram() if self.gpu else 0
        try:
            self.server.start(build_args(self.model, self.space.server_cfg(c), TRIAL_PORT, self.mmproj, spec=self.spec))
            ok, why = self.server.wait_ready(self.load_timeout, self.cancel)
            self.check()
            if not ok:
                err = classify_failure(self.server.log_tail(80)) if why == "exited" else why
                res.update(error=err, kind="mem" if "memory" in err else "load")
                return res
            res["load_s"] = round(time.time() - t0, 1)
            used0 = hardware.gpu_used_mb() if self.gpu and self.live_vram else 0
            pp, tg = self.bench()
            if self.gpu:
                if self.live_vram:
                    used = max(used0 or 0, hardware.gpu_used_mb() or 0)
                    res["mem_mb"] = max(0, used - before)
                else:
                    res["mem_mb"] = self.server.device_mb()
                    used = self.others_mb + res["mem_mb"]
                res["vram_used_mb"] = used
                if res["mem_mb"] > self.limit:
                    res.update(error=f"uses {res['mem_mb']:,} MB, over your {self.limit:,.0f} MB limit", kind="mem")
                elif used > self.vram_total - 64:
                    res.update(error=f"VRAM full ({used:,}/{self.vram_total:,} MB): would spill to shared memory",
                               kind="mem")
            else:
                res["mem_mb"] = self.server.rss_mb()
                if res["mem_mb"] > self.limit:
                    res.update(error=f"uses {res['mem_mb']:,} MB RAM, over your {self.limit:,.0f} MB limit", kind="mem")
            res.update(pp=round(pp, 1), tg=round(tg, 1))
            if "error" not in res:
                res["ok"] = True
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            # the server died under the benchmark (connection reset): near the limit that is memory running out,
            # so bigger configs are predicted to fail too instead of being tried one by one
            tail = self.server.log_tail(40)
            mem = "memory" in classify_failure(tail) or "out of memory" in tail.lower() or \
                self.est(c) >= self.cap * 0.9
            res.update(error=f"{type(e).__name__}: {e}" + (" (ran out of memory under load)" if mem else ""),
                       kind="mem" if mem else "load")
        finally:
            self.server.stop()
        return res

    def trial(self, c, move, base=None, reason="", by="estimator"):
        n = len(self.trials) + 1
        desc = self.space.describe(c)
        self.emit({"type": "trial_start", "n": n, "desc": desc, "move": move, "base": base, "reason": reason, "by": by,
                   "budget": self.budget, "est_mb": round(self.est(c))})
        r = self.run_config(c)
        t = {"n": n, "cfg": c, "desc": desc, "move": move, "base": base, "reason": reason, "by": by, **r}
        self.trials.append(t)
        self.recalibrate()
        b = self.best()
        k = Space.key(b["cfg"]) if b else None
        self.best_since = 0 if k != self.best_key else self.best_since + 1
        self.best_key = k
        self.emit({"type": "trial_done", **{k: v for k, v in t.items() if k != "cfg"}})
        return t

    def retest(self, t):
        n = len(self.trials) + 1
        self.emit({"type": "trial_start", "n": n, "desc": t["desc"], "move": f"re-test #{t['n']}", "base": t["n"],
                   "reason": "repeat measurement of a top candidate", "by": "verifier", "budget": self.budget})
        r = self.run_config(t["cfg"])
        rec = {"n": n, "cfg": t["cfg"], "desc": t["desc"], "move": f"re-test #{t['n']}", "base": t["n"],
               "by": "verifier", "retest": True, **r}
        self.trials.append(rec)
        if r["ok"]:
            runs = t.setdefault("runs", [(t["pp"], t["tg"])])
            runs.append((r["pp"], r["tg"]))
            t["pp"] = round(statistics.mean(x[0] for x in runs), 1)
            t["tg"] = round(statistics.mean(x[1] for x in runs), 1)
            t["mem_mb"] = max(t.get("mem_mb") or 0, r.get("mem_mb") or 0)
            if t["mem_mb"] > self.limit:
                t.update(ok=False, error="exceeded the limit on re-test", kind="mem")
        else:
            t.update(ok=False, error=f"failed on re-test: {r.get('error')}", kind=r.get("kind", "load"))
        rec["ok_display"] = r["ok"]
        self.emit({"type": "trial_done", **{k: v for k, v in rec.items() if k != "cfg"},
                   "avg_tg": t.get("tg"), "avg_pp": t.get("pp")})
        return rec

    def deep_check(self, t):
        """Fill half the context (cap 16k tokens, ~60 s of prompt) and make sure it still runs."""
        c = t["cfg"]
        target = int(min(c["ctx"] * 0.5, 16384, max(2048, (t["pp"] or 500) * 60)))
        self.emit({"type": "verify_start", "n": t["n"], "desc": t["desc"], "tokens": target})
        self.check()
        res = {"ok": False}
        try:
            self.server.start(build_args(self.model, self.space.server_cfg(c), TRIAL_PORT, self.mmproj, spec=self.spec))
            ok, why = self.server.wait_ready(self.load_timeout, self.cancel)
            if not ok:
                res["error"] = why
            else:
                try:
                    toks = httpx.post(self.server.url + "/tokenize", json={"content": self.prompt}, timeout=60)
                    self.tok_per_char = len(toks.json()["tokens"]) / max(1, len(self.prompt))
                except Exception:
                    pass
                parts, total, salt = [], 0, 0
                while total < target:
                    p = bench_prompt(760, salt)
                    parts.append(p)
                    total += int(len(p) * self.tok_per_char)
                    salt += 3
                tm = self._completion("\n\n".join(parts), 48, timeout=1800)
                used = (hardware.gpu_used_mb() if self.live_vram else self.others_mb + self.server.device_mb()) \
                    if self.gpu else self.server.rss_mb()
                res.update(ok=True, tg_deep=round(float(tm.get("predicted_per_second") or 0), 1),
                           pp_deep=round(float(tm.get("prompt_per_second") or 0), 1), filled=int(tm.get("prompt_n") or 0))
                if self.gpu and used and used > self.vram_total - 64:
                    res.update(ok=False, error="VRAM filled up under a long prompt")
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            res["error"] = f"{type(e).__name__}: {e}"
        finally:
            self.server.stop()
        self.emit({"type": "verify_done", "n": t["n"], **res})
        return res

    # ---- choosing the next step --------------------------------------------------------------------------
    def options(self, wide=False):
        best = self.best()
        last = self.trials[-1]
        bases = [b for b in ([best] if best else []) + [last] if b is not None]
        P = [t for t in self.trials if t["ok"] and not t.get("retest")]
        if P:   # also step from the biggest-context and the fastest passing trials (the ends of the frontier)
            bases += [max(P, key=lambda t: (t["cfg"]["ctx"], t["tg"])), max(P, key=lambda t: t["tg"])]
        if wide:
            bases += [t for t in self.ranked()[:6]]
        bases = [t for i, t in enumerate(bases) if t not in bases[:i]]
        seen, opts = set(), []
        for b in bases:
            for move, c in self.space.neighbors(b["cfg"]):
                k = Space.key(c)
                if k in seen or self.tried(c) or self.predicted_fail(c):
                    continue
                seen.add(k)
                e = self.est(c)
                opts.append({"move": move, "base": b["n"], "cfg": c, "est": e, "over": e > self.cap})
        letters = [chr(65 + i) for i in range(26)] + [f"A{chr(65 + i)}" for i in range(26)]
        for i, o in enumerate(opts[:40]):
            o["id"] = letters[i]
        return opts[:40]

    def policy(self, opts):
        """The same strategy the advisor is given, written as code."""
        fit = [o for o in opts if not o["over"]]
        pool = fit or opts
        last = self.trials[-1]

        def find(move, base, fitting=False):
            return next((o for o in (fit if fitting else pool) if o["move"] == move and o["base"] == base), None)
        P = [t for t in self.trials if t["ok"] and not t.get("retest")]
        if not P:
            first = ("ctx-", "gpu-") if (last["cfg"]["ctx"] > 32768 or not self.gpu) else ("gpu-", "ctx-")
            for m in first + ("ub-", "kv-"):
                o = find(m, last["n"])
                if o:
                    return o
            return pool[0] if pool else None
        b = self.best()
        floor = self.speed_floor()
        def useful(t):
            return t["ok"] and not t.get("retest") and t["tg"] >= floor
        if self.mode == "max_speed":
            order = [("gpu+", b), ("split", b), ("ub+", b)] + ([("ctx+", b)] if b["cfg"]["ctx"] < 32768 else []) + \
                    [("threads+", b), ("threads-", b), ("kv+", b)]
        else:
            order = [("ctx+", b)]
            # a walk toward more context: give up one GPU layer, then try ctx+ from there, while speed holds
            same = bool(last["cfg"].get("f")) == bool(b["cfg"].get("f"))     # walks stay in the best split kind
            if last is not b and same and useful(last) and last["cfg"]["ctx"] >= b["cfg"]["ctx"]:
                order.append(("ctx+", last))
                if self.walk_feasible(last):
                    order.append(("gpu-", last))
            if not last["ok"] and same and last.get("kind") == "mem" and last["cfg"]["ctx"] > b["cfg"]["ctx"]:
                order.append(("gpu-", last))
            # a walk back from a bigger-context trial that is too slow: gpu+ for speed, else ctx- to make room
            # (the best itself counts when it only wins through the 32k agent rule)
            slow = [t for t in self.trials if t["ok"] and not t.get("retest") and t["tg"] < floor
                    and (t["cfg"]["ctx"] > b["cfg"]["ctx"] or t is b) and bool(t["cfg"].get("f")) == bool(b["cfg"].get("f"))]
            if slow:
                v = last if last in slow else max(slow, key=lambda t: (t["tg"], t["cfg"]["ctx"]))
                order.append(("gpu+", v))
                if not (v is b and v["cfg"]["ctx"] <= 32768):
                    order.append(("ctx-", v))
            order += [("gpu+", b), ("split", b), ("ub+", b)]
            if not find("ctx+", b["n"], fitting=True) and self.walk_feasible(b):
                order.append(("gpu-", b))   # ctx+ from the best is blocked by memory: start a walk
            order += [("kv+", b), ("threads+", b), ("threads-", b)]
        for m, base in order:
            o = find(m, base["n"])
            if o and not o["over"]:
                return o
        # nothing fits by estimate: measure the improving moves that are only just over it (estimates aren't exact)
        improving = {"ctx+", "gpu+", "ub+", "kv+"} if self.mode != "max_speed" else {"gpu+", "ub+", "ctx+"}
        near = [o for o in opts if o["over"] and o["base"] == b["n"] and o["move"] in improving
                and o["est"] <= self.cap * 1.04]
        return min(near, key=lambda o: o["est"]) if near else None

    def sweep(self, opts):
        """Full depth, once the improving moves are used up: the next untested combination next to the best
        configs (by rank), fitting by estimate; None when there is none or the best hasn't moved for PATIENCE trials."""
        if self.depth != "full" or self.best_since >= PATIENCE:
            return None
        rank = {t["n"]: i for i, t in enumerate(self.ranked())}
        order = {"split": 0, "kv-": 0, "kv--": 0, "kv+": 1, "ub+": 2, "ub-": 3, "ctx+": 4, "threads+": 5, "threads-": 6, "gpu+": 7, "ctx-": 8,
                 "gpu-": 9}
        fit = [o for o in opts if not o["over"]]
        if not fit:
            return None
        return min(fit, key=lambda o: (rank.get(o["base"], 99), order.get(o["move"], 50)))

    def alpha(self):
        """How fast generation drops as layers move to the CPU: tg(k) ~ tg(0) / (1 + alpha * k / n).
        Starts from a typical value and is re-fit from measured trials (same ctx/kv/ub, different k)."""
        n = self.space.n
        est = []
        P = [t for t in self.trials if t["ok"] and not t.get("retest") and t["tg"] > 0]
        for a in P:
            for b in P:
                ca, cb = a["cfg"], b["cfg"]
                if cb["k"] <= ca["k"] or (ca["kv"], ca["ub"], ca["t"], bool(ca.get("f"))) != \
                        (cb["kv"], cb["ub"], cb["t"], bool(cb.get("f"))):
                    continue
                r = a["tg"] / b["tg"]
                den = cb["k"] / n - r * ca["k"] / n
                if r > 1 and den > 0:
                    est.append((r - 1) / den)
        if est:
            return min(30.0, max(0.5, statistics.median(est)))
        return 3.0 if self.space.moe else 9.0

    def walk_feasible(self, t):
        """Would a context walk from trial t (gpu- steps until ctx+ fits) keep generation above the floor?"""
        if not self.gpu or not t["ok"]:
            return False
        up = next((c for mv, c in self.space.neighbors(t["cfg"]) if mv == "ctx+"), None)
        if not up:
            return False
        n = self.space.n
        need = next((k for k in range(t["cfg"]["k"] + 1, n + 1) if self.est({**up, "k": k}) <= self.cap), None)
        if need is None:
            return False
        a = self.alpha()
        tg_after = t["tg"] * (1 + a * t["cfg"]["k"] / n) / (1 + a * need / n)
        return tg_after >= self.speed_floor()

    def speed_floor(self):
        P = [t for t in self.trials if t["ok"] and not t.get("retest")]
        if not P:
            return 0
        return max(t["tg"] for t in P) * {"balanced": 0.8, "max_context": 0.5, "max_speed": 0.95}[self.mode]

    def state_text(self):
        m = self.meta
        best = self.best()
        lines = [f"Goal: {MODE_TEXT[self.mode]}"]
        if self.gpu:
            adv = f"; the advisor uses {self.advisor.vram_mb:,} MB" if self.advisor and self.advisor.vram_mb else ""
            lines.append(f"Hardware: {self.hw['gpu_name']}, {self.vram_total:,} MB VRAM; other apps use "
                         f"{self.others_mb:,} MB{adv}. VRAM limit for the model: {self.limit:,.0f} MB "
                         f"(usable right now: {self.cap:,.0f} MB). "
                         f"CPU {self.hw['cores']} cores / {self.hw['threads']} threads.")
        else:
            lines.append(f"Hardware: CPU only, {self.hw['cores']} cores; RAM limit for the model: {self.limit:,.0f} MB.")
        kind = f"MoE with {m['experts']} experts" if m["experts"] else "dense"
        lines.append(f"Model: {m['name']}, {kind}, {m['n_layer']} layers, {self.size_mb:,.0f} MB file, "
                     f"trained context {m['ctx_train']:,}{', plus vision projector' if self.mmproj else ''}.")
        done = len(self.trials)
        lines.append(f"Trial budget: {done} done, {max(0, self.budget - done)} left.")
        if best:
            lines.append(f"Speed floor for this goal: {self.speed_floor():.1f} t/s generation.")
        lines.append("Trials so far:")
        show = self.trials[-24:]
        if best and best not in show:
            show = [best] + show
        for t in show:
            if t.get("retest"):
                continue
            if t["ok"]:
                r = f"PASS {t.get('mem_mb', 0):,} MB, prompt {t['pp']:,.0f} t/s, gen {t['tg']:.1f} t/s"
            else:
                r = f"FAIL: {t.get('error', 'failed')}"
            lines.append(f"#{t['n']} {t['desc']} -> {r}{'  (best)' if t is best else ''}")
        return "\n".join(lines)

    def option_text(self, o):
        over = ", OVER LIMIT" if o["over"] else ""
        return f"from #{o['base']} {o['move']} -> {self.space.describe(o['cfg'])} (est {o['est']:,.0f} MB{over})"

    def seeds(self):
        """The estimator's two anchors: [fastest usable config, predicted optimum for the goal].
        The fastest one sets the reference speed the goal's floor is measured against."""
        sp = self.space
        base = {"kv": "q8_0" if "q8_0" in sp.kv else sp.kv[-1], "ub": 512 if 512 in sp.ub else sp.ub[0],
                "t": sp.phys}
        if sp.pin_k is not None or sp.pin_split:
            return self._pinned_seeds(base)
        if not self.gpu:
            ctxs = [c for c in sp.ctx if self.est_raw({**base, "ctx": c, "k": sp.n}) <= self.cap * 0.9 and c <= 32768]
            return [{**base, "ctx": (ctxs or sp.ctx[:1])[-1], "k": sp.n}]

        def kmin(ctx):
            return next((k for k in range(0, sp.n + 1) if self.est_raw({**base, "ctx": ctx, "k": k}) <= self.cap * 0.97), None)
        small = min([c for c in sp.ctx if c >= 8192] or sp.ctx[-1:])
        full = [c for c in sp.ctx if c <= 32768 and kmin(c) == 0]
        if full and full[-1] >= small:
            anchor = {**base, "ctx": full[-1], "k": 0}
        else:
            k = kmin(small)
            anchor = {**base, "ctx": small, "k": sp.n if k is None else k}
        def split(seeds):
            """Each seed that needs layers on the CPU, again with the feed-forward split at the same VRAM."""
            return seeds + [e for e in (self.equiv(c) for c in seeds if sp.ffn and c["k"] > 0) if e]
        if self.mode == "max_speed":
            return split([anchor])
        a, n = self.alpha(), sp.n
        def rel(k):
            return (1 + a * anchor["k"] / n) / (1 + a * k / n)
        floor = 0.8 if self.mode == "balanced" else 0.5
        opt = anchor
        for c in sp.ctx:
            k = kmin(c)
            if k is None:
                break
            if c > opt["ctx"] and rel(k) >= floor:
                opt = {**base, "ctx": c, "k": k}
        if self.mode == "balanced" and opt["ctx"] < 32768 <= sp.ctx[-1]:
            k = kmin(32768)
            if k is not None and rel(k) >= 0.5:
                opt = {**base, "ctx": 32768, "k": k}
        return split([anchor] if Space.key(opt) == Space.key(anchor) else [anchor, opt])

    def warm_seed(self):
        """The previous best, adapted to this model and search space: the same context, KV, batch and threads, about
        the same VRAM (layers on the CPU shifted by the difference in file size), and within this tune's limit."""
        if not self.warm:
            return None
        sp, k0 = self.space, dict(self.warm.get("knobs") or {})
        if not k0.get("ctx"):
            return None
        c = {"ctx": max([x for x in sp.ctx if x <= k0["ctx"]] or sp.ctx[:1]),
             "kv": k0.get("kv") if k0.get("kv") in sp.kv else ("q8_0" if "q8_0" in sp.kv else sp.kv[-1]),
             "ub": k0.get("ub") if k0.get("ub") in sp.ub else (512 if 512 in sp.ub else sp.ub[0]),
             "t": k0.get("t") if k0.get("t") in sp.thr else sp.phys, "k": sp.n}
        if self.gpu:
            old = ((self.warm.get("meta") or {}).get("size_bytes") or self.meta["size_bytes"]) / 2**20
            per_layer = max(1.0, self.size_mb / max(1, sp.n))
            shift = (self.size_mb - old) / per_layer
            c["k"] = int(min(sp.n, max(0, round(int(k0.get("k") or 0) + shift))))
            if k0.get("f") and sp.ffn:
                c["f"] = True
        c = sp.apply_pins(c)
        # it has to fit this tune's limit too: more layers on the CPU (unless pinned), else a smaller context
        while self.gpu and sp.pin_k is None and c["k"] < sp.n and self.est(c) > self.cap * 0.97:
            c["k"] += 1
        smaller = [x for x in sp.ctx if x < c["ctx"]]
        while smaller and self.est(c) > self.cap * (0.97 if self.gpu else 0.9):
            c["ctx"] = smaller.pop()
        return c

    def _pinned_seeds(self, base):
        """With the placement pinned: the largest context that fits it, and the smallest."""
        sp = self.space
        k = sp.n if (sp.pin_k is None or not sp.gpu) else min(sp.n, int(sp.pin_k))
        fits = [c for c in sp.ctx if self.est_raw(sp.apply_pins({**base, "ctx": c, "k": k})) <= self.cap * 0.97]
        ctxs = sorted({(fits or sp.ctx[:1])[0], (fits or sp.ctx[:1])[-1]})
        return [sp.apply_pins({**base, "ctx": c, "k": k}) for c in ctxs]

    # ---- tool calling ------------------------------------------------------------------------------------
    def tool_phase(self, best):
        """Run the tool-calling exam on the winner (and a runner-up of similar speed on Long / Full), at the user's
        temperature and at 0.2. Returns (chosen trial, summary)."""
        from . import forced_call, tool_eval, tools
        tools.load_all()
        schemas = tools.schemas({**self.settings, "tools_enabled": True})
        temps = sorted({0.2, round(float(self.settings.get("temperature") or 0.7), 2)})
        limit = {"short": 12, "medium": 30}.get(self.depth)
        slow = best["tg"] < 8                 # a big model at a few tokens/s: a short exam at one temperature
        if slow:
            limit, temps = 12, [0.2]
        cands = [best]
        if self.depth in ("long", "full") and not slow:
            other = next((t for t in self.ranked()[1:4] if t["ok"] and t["tg"] >= best["tg"] * 0.9 and
                          (t["cfg"]["kv"], t["cfg"]["ctx"]) != (best["cfg"]["kv"], best["cfg"]["ctx"])), None)
            if other:
                cands.append(other)
        scored = []
        for cand in cands:
            self.check()
            self.emit({"type": "tool_eval_start", "n": cand["n"], "desc": cand["desc"]})
            self.log(f"Tool-calling exam on #{cand['n']} ({cand['desc']}): BFCL cases + Aero requests…")
            self.server.start(build_args(self.model, self.space.server_cfg(cand["cfg"]), TRIAL_PORT, self.mmproj,
                                         spec=self.spec))
            try:
                ok, why = self.server.wait_ready(self.load_timeout, self.cancel)
                if not ok:
                    self.log(f"#{cand['n']} didn't start for the exam ({why}).")
                    continue
                def progress(e, n=cand["n"]):
                    self.emit({"type": "tool_eval_progress", "n": n, "done": e["done"], "of": e["of"]})
                runs = [tool_eval.run(self.server.url, schemas, temperature=t, limit=limit, cancel=self.cancel,
                                      emit=progress) for t in (temps if self.depth != "short" else temps[:1])]
                aero_cases = [c for c in tool_eval.AERO_CASES if c[1]][:6 if slow else None]
                top = max(runs, key=lambda r: (r["score"], -r["ms_per_case"]))
                forced_ok = 0
                offered = [s for s in schemas if s["function"]["name"] in tool_eval.AERO_TOOLS]
                for req, names, check in aero_cases:
                    self.check()
                    got = forced_call.ask(self.server.url, [{"role": "user", "content": req}], offered,
                                          top["temperature"])
                    forced_ok += bool(got) and got[0] in names and (check is None or check(got[1]))
                forced = round(forced_ok / max(1, len(aero_cases)), 3)
                a_ok, a_n = (int(x) for x in top["by_category"].get("aero", "0/0").split("/"))
                auto = a_ok / max(1, a_n)
                summary = {**top, "runs": [{k: r[k] for k in ("temperature", "score", "bfcl", "aero", "ms_per_case")}
                                           for r in runs],
                           "forced_aero": forced, "auto_aero": round(auto, 3), "act_first": forced > auto,
                           "trial": cand["n"]}
                scored.append((cand, summary))
                self.log(f"#{cand['n']}: tool calling {top['score']:.0%} (BFCL {top['bfcl']:.0%}, Aero requests "
                         f"{top['aero']:.0%}) at temperature {top['temperature']}; first actions forced "
                         f"{forced:.0%} vs on its own {auto:.0%}"
                         + (" → Aero will force the first action for requests that need one." if forced > auto else "."))
                self.emit({"type": "tool_eval_done", "n": cand["n"], **{k: summary[k] for k in
                                                                          ("score", "bfcl", "aero", "temperature",
                                                                           "forced_aero", "auto_aero", "act_first")}})
            finally:
                self.server.stop()
        if not scored:
            return best, None
        cand, summary = max(scored, key=lambda x: (x[1]["score"], x[0]["tg"]))
        if cand is not best:
            self.log(f"#{cand['n']} calls tools better ({summary['score']:.0%}) at {cand['tg']} tok/s: it becomes the "
                     "pick.")
        return cand, summary

    # ---- main loop ---------------------------------------------------------------------------------------
    def run(self):
        m = self.meta
        self.emit({"type": "tune_begin", "meta": m, "hw": self.hw, "mode": self.mode, "depth": self.depth,
                   "budget": self.budget, "limit_mb": self.limit,
                   "advisor": self.advisor.name if self.advisor else None})
        self.log(f"{m['name']} · {m['arch']} · {m['n_layer']} layers"
                 + (f" · MoE {m['experts']} experts" if m["experts"] else "")
                 + f" · {self.size_mb / 1024:.2f} GB · trained context {m['ctx_train']:,}")
        if self.gpu:
            self.log(f"{self.hw['gpu_name']} · {self.vram_total:,} MB VRAM · other apps {self.others_mb:,} MB · "
                     f"model limit {self.limit:,.0f} MB")
        reserve = 0 if self.depth == "full" else max(1, round(self.budget * 0.2))
        search_budget = self.budget - reserve
        t0 = time.time()

        why = ["estimator's fastest usable config (sets the reference speed)",
               "estimator's predicted optimum for this goal"]
        warm = self.warm_seed()
        if warm:
            self.log(f"Warm start from {self.warm_why}.")
            self.emit({"type": "warm_start", "from": self.warm_why})
            self.trial(warm, "seed", reason="warm start: " + self.warm_why)
        for i, c in enumerate(s for s in self.seeds() if not (warm and Space.key(s) == Space.key(warm))):
            r = "the other split: feed-forward of some layers in RAM, attention and KV on the GPU" if c.get("f") \
                else (why[i] if len(why) > i else "")
            first = self.trial(c, "seed", reason=r)
            if not first["ok"] and first.get("kind") == "load" and "model failed to load" in first.get("error", ""):
                raise RuntimeError("This model can't be loaded by the installed llama.cpp: " + first["error"])

        converged = False
        while len(self.trials) < search_budget:
            self.check()
            opts = self.options() or self.options(wide=True)
            if not opts:
                converged = True
                self.log("Every useful one-step move has been tested.")
                break
            choice, reason, by = None, "", "policy"
            if self.advisor and self.advisor.failures < 3:
                listed = [(o["id"], self.option_text(o)) for o in opts]
                if self.depth == "full":
                    listed.append(("FINISH", "stop searching; the current best is final"))
                cid, why = self.advisor.ask(self.state_text(), listed)
                if cid == "FINISH":
                    pol = self.policy(opts) or self.sweep(self.options(wide=True))
                    if pol is None:
                        self.decisions.append({"after": len(self.trials), "advisor": "FINISH", "accepted": True})
                        self.log(f"Advisor: finished. {why}")
                        converged = True
                        break
                    choice, by = pol, "verifier"
                    reason = "advisor wanted to stop, but an untested improving step remained"
                elif cid:
                    choice = next(o for o in opts if o["id"] == cid)
                    reason, by = why, "advisor"
                else:
                    self.log(f"Advisor reply unusable ({why}); using the step policy for this move.")
            if choice is None:
                choice = self.policy(opts)
                reason = reason or "built-in step policy"
                if choice is None:
                    choice = self.sweep(self.options(wide=True))
                    reason, by = "full sweep: an untested combination next to the best configs", "sweep"
                if choice is None:
                    converged = True
                    self.log("Search converged: no remaining one-step move can improve the goal. "
                             "The rest of the budget re-measures the top configs.")
                    break
            self.trial(choice["cfg"], choice["move"], choice["base"], reason, by)

        # ---- re-test the top candidates and average (measurement noise) ---------------------------------
        left = (self.budget - len(self.trials)) if self.depth != "full" else 3
        def contenders():
            fl = self.speed_floor()
            return [x for x in self.ranked()[:3] if x["tg"] >= fl]
        top = contenders()
        i = 0
        while left > 0 and top and i < 9:
            t = top[i % len(top)]
            if t["ok"] and len(t.get("runs", [])) < 4:
                self.retest(t)
                left -= 1
            i += 1
            top = contenders()
        best = self.best()
        if not best:
            raise RuntimeError("No configuration passed. Raise the VRAM limit or pick a smaller quant.\n"
                               + self.server.log_tail(12))

        # ---- advisor's final pick, checked against the measurements -----------------------------------------
        verdict = "verifier"
        if self.advisor and self.advisor.failures < 3:
            passing = [t for t in self.trials if t["ok"] and not t.get("retest")][-20:]
            q = self.state_text() + "\n\nQuestion: which PASSING trial best matches the goal?"
            cid, why = self.advisor.ask(q, [(str(t["n"]), f"trial #{t['n']}: {t['desc']}, gen {t['tg']} t/s")
                                            for t in passing])
            if cid and int(cid) == best["n"]:
                verdict = "advisor and measurements agree"
                self.log(f"Advisor picked #{cid}; the measurements agree. {why}")
            elif cid:
                verdict = f"advisor picked #{cid}; measurements favour #{best['n']}"
                self.log(f"Advisor picked #{cid}, but #{best['n']} measured better for this goal; using #{best['n']}.")
            self.decisions.append({"final_advisor": cid, "final": best["n"], "reason": why})

        # ---- deep-context proof run --------------------------------------------------------------------------
        deep = None
        for cand in self.ranked()[:3]:
            deep = self.deep_check(cand)
            if deep["ok"]:
                best = cand
                break
            self.log(f"#{cand['n']} failed the long-prompt check ({deep.get('error')}); trying the next best.")
            cand.update(ok=False, error="failed long-prompt check", kind="mem")
        else:
            raise RuntimeError("No configuration survived the long-prompt check.")

        # ---- tool calling, baked in ------------------------------------------------------------------------------
        tool = None
        if self.settings.get("tune_tool_eval", True):
            try:
                best, tool = self.tool_phase(best)
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                self.log(f"Tool-calling check skipped: {type(e).__name__}: {e}")

        cfg = self.space.server_cfg(best["cfg"])
        elapsed = round(time.time() - t0, 1)
        result = {
            "config": cfg, "knobs": best["cfg"], "desc": best["desc"], "pp": best["pp"], "tg": best["tg"],
            "tg_deep": deep.get("tg_deep"), "deep_tokens": deep.get("filled"), "mem_mb": best.get("mem_mb"),
            "trial_n": best["n"], "limit_mb": self.limit, "depth": self.depth, "mode": self.mode,
            "advisor": {"name": self.advisor.name, "where": self.advisor.where} if self.advisor else None,
            "verdict": verdict, "converged": converged,
            "trials": [{**{k: v for k, v in t.items() if k != "cfg"}, "knobs": t["cfg"]} for t in self.trials],
            "decisions": self.decisions, "tuned_at": time.strftime("%Y-%m-%d %H:%M"), "elapsed_s": elapsed,
            "model_file": self.model.name, "llama_version": server_version(), "meta": m, "tool_eval": tool,
            "hw": {"gpu_name": self.hw.get("gpu_name"), "vram_total_mb": self.vram_total,
                   "ram_total_mb": self.hw.get("ram_total_mb")}, "calib": round(self.calib, 4),
            "warm": self.warm_why or None,
        }
        self.emit({"type": "tune_done", "desc": best["desc"], "pp": best["pp"], "tg": best["tg"],
                   "tg_deep": deep.get("tg_deep"), "n": best["n"], "elapsed_s": elapsed, "trials": len(self.trials),
                   "verdict": verdict, "mem_mb": best.get("mem_mb"), "tool_eval": tool})
        return result
