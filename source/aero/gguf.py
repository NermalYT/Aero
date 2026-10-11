"""Minimal GGUF header reader: pulls the metadata the tuner needs without loading weights."""
import re
import struct
from pathlib import Path

_SCALAR = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
SPLIT_RE = re.compile(r"^(.*)-(\d{5})-of-(\d{5})\.gguf$", re.I)


class _Reader:
    def __init__(self, f):
        self.f = f
        try:                                   # known length: lets skips detect a truncated buffer
            here = f.tell()
            self.end = f.seek(0, 2)
            f.seek(here)
        except Exception:
            self.end = None

    def read(self, n):
        b = self.f.read(n)
        if len(b) != n:
            raise EOFError("truncated GGUF header")
        return b

    def u32(self):
        return struct.unpack("<I", self.read(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.read(8))[0]

    def string(self):
        n = self.u64()
        return self.read(n).decode("utf-8", "replace")

    def skip_string(self):
        self.skip(self.u64())

    def skip(self, n):
        pos = self.f.seek(n, 1)
        if self.end is not None and pos > self.end:
            raise EOFError("truncated GGUF header")

    def value(self, t, keep_arrays=False):
        if t in _SCALAR:
            fmt = _SCALAR[t]
            return struct.unpack(fmt, self.read(struct.calcsize(fmt)))[0]
        if t == 8:
            return self.string()
        if t == 9:
            it = self.u32()
            n = self.u64()
            if not keep_arrays:
                # skip quickly (tokenizer vocabularies hold 150k+ strings)
                if it == 8:
                    for _ in range(n):
                        self.skip_string()
                elif it in _SCALAR:
                    self.skip(struct.calcsize(_SCALAR[it]) * n)
                else:
                    for _ in range(n):
                        self.value(it)
                return {"array_len": n}
            return [self.value(it) for _ in range(n)]
        raise ValueError(f"unknown GGUF value type {t}")


# array keys we actually want the contents of (small per-layer lists)
_KEEP_ARRAYS = ("attention.head_count_kv", "attention.head_count", "attention.sliding_window_pattern")


def read_metadata(src) -> dict:
    """src: a path, or a binary file object (e.g. the first megabytes of a remote file).
    Raises EOFError when the header is longer than what src holds."""
    if hasattr(src, "read"):
        return _read_kv(src)
    with open(src, "rb") as f:
        return _read_kv(f)


def _read_kv(f) -> dict:
    r = _Reader(f)
    if r.read(4) != b"GGUF":
        raise ValueError("not a GGUF file")
    version = r.u32()
    n_tensors = r.u64()
    n_kv = r.u64()
    kv = {}
    for _ in range(n_kv):
        key = r.string()
        t = r.u32()
        keep = t == 9 and key.endswith(_KEEP_ARRAYS)
        val = r.value(t, keep_arrays=keep)
        if key == "tokenizer.chat_template":
            kv[key] = val[:20000] if isinstance(val, str) else val
        else:
            kv[key] = val
    kv["_version"] = version
    kv["_n_tensors"] = n_tensors
    return kv


def split_parts(path: Path):
    """Return all shard paths for a split GGUF (or [path])."""
    m = SPLIT_RE.match(path.name)
    if not m:
        return [path]
    total = int(m.group(3))
    return [path.with_name(f"{m.group(1)}-{i:05d}-of-{total:05d}.gguf") for i in range(1, total + 1)]


def _first(v, default=0):
    if isinstance(v, list):
        nz = [x for x in v if x]
        return max(nz) if nz else default
    return v if v is not None else default


def summarize(path) -> dict:
    """Architecture facts used for VRAM estimates."""
    path = Path(path)
    parts = split_parts(path)
    size = sum(p.stat().st_size for p in parts if p.exists())
    return summarize_kv(read_metadata(path), size, len(parts), path.stem)


def summarize_kv(kv: dict, size=0, n_parts=1, fallback_name="") -> dict:
    arch = kv.get("general.architecture", "llama")
    g = lambda k, d=0: kv.get(f"{arch}.{k}", d)
    n_layer = int(g("block_count", 32))
    n_embd = int(g("embedding_length", 4096))
    n_head = int(_first(g("attention.head_count", 32), 32))
    hkv = g("attention.head_count_kv", n_head)
    n_head_kv = int(_first(hkv, n_head))
    head_k = int(g("attention.key_length", n_embd // max(n_head, 1)))
    head_v = int(g("attention.value_length", head_k))
    experts = int(g("expert_count", 0) or 0)
    swa = int(g("attention.sliding_window", 0) or 0)
    # layers that keep a KV cache: hybrid models (linear attention / SSM layers) only cache some of them
    if isinstance(hkv, list):
        kv_heads = sum(int(x) for x in hkv if x)
        kv_layers = sum(1 for x in hkv if x)
    else:
        interval = int(g("full_attention_interval", 0) or 0)
        kv_layers = n_layer // interval if interval > 1 else n_layer
        if g("ssm.state_size", 0) and not g("attention.head_count_kv", 0):
            kv_layers = 0
        kv_heads = kv_layers * n_head_kv
    # share of attention layers that see the whole context (the rest only see the sliding window)
    swa_global = 1.0
    if swa:
        pat = g("attention.sliding_window_pattern", None)
        if isinstance(pat, list) and pat:
            swa_global = max(1, sum(1 for x in pat if not x)) / len(pat)
        elif isinstance(pat, int) and pat > 1:
            swa_global = 1 / pat
        else:   # pattern not stored: known layouts (gemma3 = 5 local : 1 global), else alternating
            swa_global = {"gemma3": 1 / 6, "gemma3n": 1 / 5, "cohere2": 1 / 4}.get(arch, 0.5)
    template = kv.get("tokenizer.chat_template") or ""
    tl = template.lower()
    return {
        "arch": arch,
        "name": kv.get("general.name") or fallback_name,
        "n_layer": n_layer,
        "n_embd": n_embd,
        "n_head": n_head,
        "n_head_kv": n_head_kv,
        "kv_layers": kv_layers,
        "kv_heads": kv_heads,
        "head_k": head_k,
        "head_v": head_v,
        "ctx_train": int(g("context_length", 4096) or 4096),
        "experts": experts,
        "experts_used": int(g("expert_used_count", 0) or 0),
        "sliding_window": swa,
        "swa_global": swa_global,
        "mtp_layers": int(g("nextn_predict_layers", 0) or 0),
        "params": int(kv.get("general.parameter_count") or 0),
        "size_label": kv.get("general.size_label") or "",
        "license": kv.get("general.license") or "",
        "file_type": kv.get("general.file_type"),
        "size_bytes": size,
        "parts": n_parts,
        "has_tools_template": "tool" in tl,
        "has_thinking_template": ("think" in tl) or ("reasoning" in tl),
    }


def mmproj_caps(kv: dict) -> dict:
    """What a multimodal projector adds: images, audio or both."""
    vis = kv.get("clip.has_vision_encoder")
    aud = kv.get("clip.has_audio_encoder")
    if vis is None and aud is None:          # older projectors only did images
        vis = True
    proj = kv.get("clip.vision.projector_type") or kv.get("clip.projector_type") or kv.get("clip.audio.projector_type") or ""
    return {"vision": bool(vis), "audio": bool(aud), "projector": proj}


def kv_bytes_per_token(meta: dict, kv_type: str = "q8_0") -> float:
    per_elem = {"f16": 2.0, "q8_0": 1.0625, "q4_0": 0.5625}.get(kv_type, 2.0)
    heads = meta.get("kv_heads", meta["n_layer"] * meta["n_head_kv"])
    return heads * (meta["head_k"] + meta["head_v"]) * per_elem


def kv_mb(meta: dict, ctx: int, kv_type: str = "q8_0") -> float:
    """KV cache for ctx tokens; sliding-window layers only keep their window."""
    g = meta.get("swa_global", 1.0)
    eff = g * ctx + (1 - g) * min(ctx, (meta.get("sliding_window") or ctx) + 512)
    return kv_bytes_per_token(meta, kv_type) * eff / 2**20


# ---- tensor layout (for Remote Mode's weight split) ------------------------------------------------------------

def read_tensors(path):
    """[(name, bytes)] for every tensor in a GGUF (all shards of a split file). Sizes come from the distance between
    tensor offsets in the data section, so they are exact for every quantization type, including ones this reader
    has never heard of (the last tensor of each shard runs to the end of the file)."""
    out = []
    for part in split_parts(Path(path)):
        with open(part, "rb") as f:
            r = _Reader(f)
            if r.read(4) != b"GGUF":
                raise ValueError("not a GGUF file")
            r.u32()
            n_tensors, n_kv = r.u64(), r.u64()
            align = 32
            for _ in range(n_kv):
                key = r.string()
                t = r.u32()
                val = r.value(t)
                if key == "general.alignment" and isinstance(val, int) and val > 0:
                    align = val
            infos = []
            for _ in range(n_tensors):
                name = r.string()
                nd = r.u32()
                for _d in range(nd):
                    r.u64()
                r.u32()                                       # ggml type
                infos.append((name, r.u64()))
            here = f.tell()
            data_start = (here + align - 1) // align * align
            end = f.seek(0, 2)
        infos.sort(key=lambda x: x[1])
        for i, (name, off) in enumerate(infos):
            nxt = infos[i + 1][1] if i + 1 < len(infos) else end - data_start
            out.append((name, max(0, nxt - off)))
    return out


_BLK = re.compile(r"^blk\.(\d+)\.")


def layer_bytes(path, mtp_used=False):
    """How a model's weight bytes split up: {n_layer, layers: [bytes of blk.i], experts: [bytes of blk.i's MoE expert
    tensors], output (output.weight + output_norm), embd (token_embd, which llama.cpp keeps on the CPU), other,
    nextn (multi-token-prediction blocks: llama.cpp counts them as layers but only loads them when MTP speculative
    decoding is on, so they weigh 0 unless mtp_used), total}. Current llama.cpp counts the output layer as one of -ngl's layers and places it first: -ngl N puts the
    output layer and the last N-1 repeating layers on the GPU. --n-cpu-moe N keeps the experts of the first N layers
    on the CPU."""
    layers, experts = {}, {}
    out = {"output": 0, "embd": 0, "other": 0, "total": 0, "nextn": 0}
    kv = read_metadata(split_parts(Path(path))[0])
    arch = kv.get("general.architecture", "llama")
    blocks = int(kv.get(f"{arch}.block_count") or 0)
    n_mtp = int(kv.get(f"{arch}.nextn_predict_layers") or 0)
    main = blocks - n_mtp if blocks and n_mtp else 0
    for name, n in read_tensors(path):
        m = _BLK.match(name)
        if m and main and int(m.group(1)) >= main:
            out["nextn"] += n
            if not mtp_used:
                layers.setdefault(int(m.group(1)), 0)
                continue
        out["total"] += n
        if m:
            i = int(m.group(1))
            layers[i] = layers.get(i, 0) + n
            if "_exps" in name:                              # MoE expert tensors (not shared experts)
                experts[i] = experts.get(i, 0) + n
        elif name.startswith("token_embd"):
            out["embd"] += n
        elif name.startswith("output"):
            out["output"] += n
        else:
            out["other"] += n
    nl = (max(layers) + 1) if layers else 0
    out["n_layer"] = nl
    out["layers"] = [layers.get(i, 0) for i in range(nl)]
    out["experts"] = [experts.get(i, 0) for i in range(nl)]
    return out
