"""The tuning advisor: a small LLM that decides which llama.cpp experiment to run next.

It lives in the VRAM the user left over (or on the CPU when that's too small, so it never steals
VRAM from the measurements), answers through a JSON grammar so every reply is a legal one-step
move, and must pass a decision test before it is trusted. If no advisor can be found, loaded or
passes the test, the built-in step policy (same rules, written in code) takes over.
"""
import json
import re
from pathlib import Path

import httpx

from . import gguf, hf
from .config import ADVISOR_PORT, models_dir
from .engine import LlamaServer, build_args, classify_failure, supports

# Bonsai first (the original pick), then small models with reliable instruction following.
FALLBACK_REPOS = ["unsloth/Qwen3-1.7B-GGUF", "Qwen/Qwen3-1.7B-GGUF",
                  "unsloth/Qwen3-4B-Instruct-2507-GGUF", "Qwen/Qwen3-4B-GGUF"]
ADVISOR_CTX = 8192
MAX_CANDIDATES = 6

GUIDE = """You are the tuning advisor inside Aero. You tune llama.cpp settings for ONE model on this PC by choosing ONE experiment at a time. Each experiment is a real test run, so choose carefully.

How experiments work
- Trials marked "seed" come from the estimator: the fastest usable config and its predicted optimum.
- Every option changes exactly ONE setting by ONE step from an earlier trial (#n). You may only pick from the options listed.
- A trial PASSES when the model loads, stays inside the user's VRAM limit and runs at normal speed. Otherwise it FAILS.
- ctx = context length in tokens (longer chats and files; uses VRAM).
- gpu = layers running on the GPU (more = much faster; uses VRAM). gpu+ moves one layer onto the GPU, gpu- moves one layer to the CPU.
- kv = KV-cache precision: f16 is exact, q8_0 is near-exact at half the memory.
- ub = prompt chunk size (bigger = faster prompt reading, slightly more VRAM).
- threads = CPU threads; only matters when some layers run on the CPU.
- "est" is a calibrated VRAM estimate. Options marked OVER LIMIT will almost certainly fail.

Strategy (follow in order)
1. If no trial has passed yet, pick an option that lowers VRAM from the last failed trial: ctx- when its context is above 32,768, otherwise gpu- (else ctx-).
2. Grow the context from the best trial with ctx+ while it keeps passing.
3. If ctx+ from the best is not offered or is OVER LIMIT, walk toward more context: gpu- (one layer to the CPU), then ctx+ from that new trial, again and again, as long as generation stays at or above the speed floor. If a ctx+ trial failed on memory, gpu- on that failed trial is the same walk.
4. If a passing trial has MORE context than the best but is below the speed floor, walk back from it: gpu+ (speed), or ctx- when gpu+ doesn't fit, until it reaches the floor.
5. When the context can't grow (or the goal says speed), try gpu+ for speed, ub+ for prompt speed, then kv+ (f16) if it still fits.
6. If layers run on the CPU, try threads+ and threads- once each.
7. Never pick an OVER LIMIT option while an option that fits is available.
8. Only pick FINISH (when offered) if no option can still improve the goal.

Reply with JSON only: a short reason, then the option id."""

# Decision test: (state, options, expected id). All three must be answered correctly.
EXAM = [
    ("""Goal: Balanced: the largest context that keeps at least 80% of the best generation speed.
VRAM limit for the model: 14,750 MB.
Model: dense, 64 layers, 19,800 MB file, trained context 131,072.
Trials so far:
#1 ctx 32,768 | gpu 64/64 | kv q8_0 | ub 512 -> FAIL: over the VRAM limit""",
     [("A", "from #1 ctx+ -> ctx 49,152 | gpu 64/64 | kv q8_0 | ub 512 (est 23,800 MB, OVER LIMIT)"),
      ("B", "from #1 gpu- -> ctx 32,768 | gpu 63/64 | kv q8_0 | ub 512 (est 22,550 MB, OVER LIMIT)"),
      ("C", "from #1 ub+ -> ctx 32,768 | gpu 64/64 | kv q8_0 | ub 1024 (est 23,100 MB, OVER LIMIT)")], "B"),
    ("""Goal: Balanced: the largest context that keeps at least 80% of the best generation speed.
VRAM limit for the model: 14,750 MB.
Model: dense, 36 layers, 8,700 MB file, trained context 131,072.
Trials so far:
#1 ctx 16,384 | gpu 36/36 | kv q8_0 | ub 512 -> PASS 9,800 MB, prompt 5,100 t/s, gen 121 t/s
#2 ctx 24,576 | gpu 36/36 | kv q8_0 | ub 512 -> PASS 10,400 MB, prompt 5,050 t/s, gen 120 t/s  (best)""",
     [("A", "from #2 threads+ -> ctx 24,576 | gpu 36/36 | kv q8_0 | ub 512 | 32 threads (est 10,400 MB)"),
      ("B", "from #2 ctx+ -> ctx 32,768 | gpu 36/36 | kv q8_0 | ub 512 (est 11,000 MB)"),
      ("C", "from #2 gpu- -> ctx 24,576 | gpu 35/36 | kv q8_0 | ub 512 (est 10,150 MB)")], "B"),
    ("""Goal: Balanced: the largest context that keeps at least 80% of the best generation speed.
Question: which PASSING trial best matches the goal?
Trials:
#1 ctx 16,384 -> PASS gen 120 t/s
#2 ctx 32,768 -> PASS gen 118 t/s
#3 ctx 65,536 -> PASS gen 60 t/s
#4 ctx 49,152 -> FAIL: over the VRAM limit""",
     [("1", "trial #1"), ("2", "trial #2"), ("3", "trial #3")], "2"),
]


def _schema(ids):
    return {"type": "object", "additionalProperties": False, "required": ["reason", "choice"],
            "properties": {"reason": {"type": "string", "maxLength": 160},
                           "choice": {"type": "string", "enum": list(ids)}}}


class Advisor:
    def __init__(self, settings, hw, headroom_mb, emit, cancel):
        self.settings, self.hw, self.headroom = settings, hw, headroom_mb
        self.emit, self.cancel = emit, cancel
        self.server = LlamaServer(ADVISOR_PORT, "advisor")
        self.ready = False
        self.name = None
        self.where = None
        self.failures = 0
        self.vram_mb = 0

    def log(self, text):
        self.emit({"type": "advisor", "text": text})

    # ---- choosing a model -------------------------------------------------------------
    def _candidates(self):
        """Bonsai repos first (Bonsai 2 before older ones, smallest model first), then fallbacks."""
        pick = (self.settings.get("advisor_model") or "auto").strip()
        if pick.lower() == "off":
            return []
        if pick and pick.lower() != "auto":
            if Path(pick).is_file():
                return [("local", pick, None)]
            return [] if self.settings.get("strict_offline") else [("repo", pick, None)]
        if self.settings.get("strict_offline"):
            have = sorted((models_dir() / hf.ADVISOR_DIR).rglob("*.gguf"), key=lambda p: p.stat().st_size)
            have = [p for p in have if "mmproj" not in p.name.lower() and not re.search(r"-0000[2-9]-of-", p.name)]
            if not have:
                self.log("Strict offline: no advisor model has been downloaded yet, so the built-in step policy "
                         "drives the trials.")
            return [("local", str(p), None) for p in have]
        out = []
        try:
            found = [r for r in hf.search("Bonsai", limit=40) if "bonsai" in r["id"].lower()]
            found.sort(key=lambda r: (0 if r["id"].lower().startswith("prism-ml/") else 1,
                                      0 if re.search(r"bonsai[-_ ]?v?2", r["id"], re.I) else 1,
                                      -(r.get("downloads") or 0)))
            listed = []
            for r in found[:8]:
                try:
                    d = hf.list_files(r["id"])
                    files = [f for f in d["files"] if f["size"] > 0]
                    if files:
                        listed.append((r, d, min(f["size"] for f in files)))
                except Exception:
                    continue
            listed.sort(key=lambda x: (0 if x[0]["id"].lower().startswith("prism-ml/") else 1,
                                       0 if re.search(r"bonsai[-_ ]?v?2", x[0]["id"], re.I) else 1, x[2]))
            out += [("repo", r["id"], d) for r, d, _ in listed]
            if not listed:
                self.log("No Bonsai GGUF repos found on Hugging Face; using the small-model fallbacks.")
        except Exception as e:  # noqa: BLE001
            self.log(f"Hugging Face search failed ({e}); trying the fallbacks.")
        out += [("repo", r, None) for r in FALLBACK_REPOS]
        return out

    def _gpu_need_mb(self, size_mb, path=None):
        kv = 0.3 * size_mb  # rough until the file's metadata is readable
        if path:
            try:
                kv = gguf.kv_bytes_per_token(gguf.summarize(path), "q8_0") * ADVISOR_CTX / 2**20
            except Exception:
                pass
        return size_mb + kv + 260

    def _pick_file(self, repo, listing=None):
        """The leftover VRAM decides the quant. Bonsai (trained natively at low bit-width): the smallest file.
        Other models: the most accurate quant (>= 4 bits) that fits. If nothing fits, the advisor runs on the CPU
        so it never takes VRAM away from the trials."""
        d = listing or hf.list_files(repo)
        files = [f for f in d["files"] if f["size"] > 0]
        if not files:
            return None
        native = "bonsai" in repo.lower()     # Bonsai is trained low-bit: its files ARE full accuracy
        ok = [f for f in files if native or (f["bpw"] or 0) >= 4.0] or files
        fits = [f for f in ok if self._gpu_need_mb(f["size"] / 2**20) <= self.headroom]
        if native:   # every Bonsai file is equally accurate, so the smallest one wins (GPU when it fits)
            return min(fits or ok, key=lambda f: f["size"]), ("GPU" if fits else "CPU")
        if fits:
            return max(fits, key=lambda f: ((f["bpw"] or 0), f["size"])), "GPU"
        cpu = [f for f in ok if (f["bpw"] or 0) <= 8.5] or ok
        return max(cpu, key=lambda f: ((f["bpw"] or 0), -f["size"])), "CPU"

    def _download(self, repo, f):
        root = models_dir() / hf.ADVISOR_DIR
        local = hf.local_path(repo, f["parts"][0], root)
        if all(hf.local_path(repo, p, root).exists() for p in f["parts"]):
            return local
        self.log(f"Pulling advisor {repo} · {f['quant']} ({f['size'] / 2**20:,.0f} MB)…")

        def em(ev):
            if ev.get("type") == "progress":
                self.emit({"type": "advisor_progress", **{k: v for k, v in ev.items() if k != "type"}})
        hf.download(repo, f["parts"], em, self.cancel, root)
        return local

    def _start(self, path, where):
        cfg = {"ctx": ADVISOR_CTX, "ngl": 999 if where == "GPU" else 0, "kv": "q8_0", "fa": True,
               "threads": self.hw.get("cores") or 8, "ub": 512}
        args = build_args(path, cfg, ADVISOR_PORT) + ["--jinja"]
        if supports("--reasoning"):
            args += ["--reasoning", "off"]
        if supports("--reasoning-format"):
            args += ["--reasoning-format", "none"]
        if supports("--reasoning-budget"):
            args += ["--reasoning-budget", "0"]     # decisions only: no thinking tokens
        env = None
        if where == "CPU":
            # keep the advisor completely off the GPU so every byte of VRAM goes to the trials
            if supports("--device"):
                args += ["--device", "none"]
            env = {"CUDA_VISIBLE_DEVICES": "-1", "GGML_VK_VISIBLE_DEVICES": ""}
        self.server.start(args, env)
        ok, why = self.server.wait_ready(300, self.cancel)
        if not ok:
            raise RuntimeError(classify_failure(self.server.log_tail(60)) if why == "exited" else why)

    def prepare(self):
        """Find, pull, load and exam an advisor. Returns True when one passed."""
        if (self.settings.get("advisor_model") or "").strip().lower() == "off":
            self.log("Advisor turned off in Settings; the built-in step policy drives the trials.")
            return False
        if self.hw.get("gpus"):
            self.log(f"Leftover VRAM for the advisor: {max(0, self.headroom):,.0f} MB.")
        else:
            self.log("No dedicated GPU, so the advisor runs on the CPU.")
        tried = 0
        for kind, ref, listing in self._candidates():
            if self.cancel.is_set() or tried >= MAX_CANDIDATES:
                break
            tried += 1
            try:
                if kind == "local":
                    path, label = Path(ref), Path(ref).stem
                    where = "GPU" if self._gpu_need_mb(path.stat().st_size / 2**20, path) <= self.headroom else "CPU"
                else:
                    picked = self._pick_file(ref, listing)
                    if not picked:
                        continue
                    f, where = picked
                    label = f"{ref.split('/')[-1]} · {f['quant'] if f['quant'] != '?' else 'native'}"
                    path = self._download(ref, f)
                    if where == "GPU" and self._gpu_need_mb(f["size"] / 2**20, path) > self.headroom:
                        where = "CPU"
                self.log(f"Loading advisor {label} on the {where}…")
                before = self._vram()
                self._start(path, where)
                if where == "GPU":
                    self.vram_mb = max(0, self._vram() - before)
                    if self.vram_mb > self.headroom:
                        self.log(f"Advisor used {self.vram_mb:,} MB, more than the headroom; moving it to the CPU.")
                        self.server.stop()
                        where = "CPU"
                        self.vram_mb = 0
                        self._start(path, where)
                score = self._exam()
                if score == len(EXAM):
                    self.name, self.where, self.ready = label, where, True
                    self.log(f"Advisor ready: {label} on the {where} (decision test {score}/{len(EXAM)}).")
                    self.emit({"type": "advisor_ready", "name": label, "where": where})
                    return True
                self.log(f"{label} scored {score}/{len(EXAM)} on the decision test; trying the next candidate.")
                self.server.stop()
            except Exception as e:  # noqa: BLE001
                if self.cancel.is_set():
                    break
                self.log(f"{ref}: {e}")
                self.server.stop()
        self.log("No advisor passed; the built-in step policy will drive the trials (same rules, same verification).")
        return False

    def _vram(self):
        from . import hardware
        return hardware.gpu_used_mb() or 0 if self.hw.get("gpus") else 0

    def _exam(self):
        score = 0
        for state, opts, expected in EXAM:
            got, _ = self.ask(state, opts, retries=0)
            score += int(got == expected)
        return score

    # ---- asking -------------------------------------------------------------------------
    def ask(self, state, options, retries=1):
        """options: [(id, text)]. Returns (id, reason) or (None, error)."""
        ids = [o[0] for o in options]
        user = state + "\n\nOptions:\n" + "\n".join(f"{i}: {t}" for i, t in options) + \
            "\n\nPick exactly one option id."
        body = {"messages": [{"role": "system", "content": GUIDE}, {"role": "user", "content": user}],
                "temperature": 0, "top_k": 1, "max_tokens": 256, "cache_prompt": True,
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {"type": "json_schema", "json_schema": {"name": "decision", "schema": _schema(ids)}}}
        err = None
        for _ in range(retries + 1):
            try:
                r = httpx.post(self.server.url + "/v1/chat/completions", json=body, timeout=180)
                r.raise_for_status()
                txt = r.json()["choices"][0]["message"].get("content") or ""
                txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()
                j = json.loads(txt)
                if j.get("choice") in ids:
                    self.failures = 0
                    return j["choice"], str(j.get("reason", ""))[:160]
                err = f"invalid choice {j.get('choice')!r}"
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
        self.failures += 1
        return None, err

    def stop(self):
        self.server.stop()
