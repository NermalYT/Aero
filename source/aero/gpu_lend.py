"""Lend the GPU to a tool for a moment: when a job (an image) needs more VRAM than is free and the main model fills
the card, unload the model, run the job, and load the model again with the same configuration.

Measured on the owner's RTX 5080 with Qwen3.8-27B loaded (15.1 GB): a 1024 x 1024 image took 8 minutes squeezed into
what was left; with the card free it takes about 14 s, and reloading that model takes about 6 s. The model is idle
while a tool runs anyway, and chat messages sent meanwhile wait (vram_policy.hold) instead of failing.
"""
import contextlib
import threading

from . import hardware, vram_policy

_hooks = {"stop": None, "restore": None}       # set by the server: stop() -> state for restore(state) -> bool
_lock = threading.Lock()


def register(stop, restore):
    _hooks.update(stop=stop, restore=restore)


def free_mb():
    g = [x for x in hardware.gpus() if not x.get("unified")]
    return (g[0].get("free_mb") or 0) if g else None


@contextlib.contextmanager
def lend(need_mb, log=None):
    """Yields True when the main model was unloaded for this job (and is loaded again afterwards)."""
    free = free_mb()
    if free is None or free >= need_mb or not _hooks["stop"]:
        yield False
        return
    with _lock:
        state = None
        try:
            state = _hooks["stop"]()
        except Exception:  # noqa: BLE001
            state = None
        if state is None:                        # nothing loaded, or the server is busy loading/tuning
            yield False
            return
        vram_policy.hold(True)
        if log:
            log(f"Only {free:,} MB of VRAM free: unloading the main model for this job; it comes back afterwards.")
        try:
            yield True
        finally:
            try:
                _hooks["restore"](state)
            finally:
                vram_policy.hold(False)
