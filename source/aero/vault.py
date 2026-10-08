"""Local secret store (API keys, GitHub token, MCP OAuth tokens).

Values are encrypted with Windows DPAPI (CryptProtectData, bound to the current Windows user), so the
file data/secrets.json is useless if copied to another account or PC. Off Windows (tests) they are only
base64-obfuscated. Secrets never leave this machine except to the service they belong to, and the UI
only ever sees a masked form.
"""
import base64
import json
import os
import sys
import threading

from .config import DATA

PATH = DATA / "secrets.json"
_lock = threading.Lock()
_ENTROPY = b"Aero local secrets v1"


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _blob(data: bytes):
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _protect(raw: bytes) -> bytes:
        src, _k1 = _blob(raw)
        ent, _k2 = _blob(_ENTROPY)
        out = _Blob()
        if not _crypt32.CryptProtectData(ctypes.byref(src), "Aero", ctypes.byref(ent), None, None, 0x1,
                                         ctypes.byref(out)):
            raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)

    def _unprotect(enc: bytes) -> bytes:
        src, _k1 = _blob(enc)
        ent, _k2 = _blob(_ENTROPY)
        out = _Blob()
        if not _crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1,
                                           ctypes.byref(out)):
            raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)
else:
    def _protect(raw: bytes) -> bytes:
        return b"plain:" + raw

    def _unprotect(enc: bytes) -> bytes:
        return enc[6:] if enc.startswith(b"plain:") else enc


def _load():
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d):
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.replace(tmp, PATH)


def get(name, default=""):
    with _lock:
        v = _load().get(name)
    if not v:
        return default
    try:
        return _unprotect(base64.b64decode(v)).decode("utf-8")
    except Exception:
        return default


def put(name, value):
    with _lock:
        d = _load()
        if value:
            d[name] = base64.b64encode(_protect(str(value).encode("utf-8"))).decode("ascii")
        else:
            d.pop(name, None)
        _save(d)


def get_json(name, default=None):
    v = get(name)
    try:
        return json.loads(v) if v else default
    except Exception:
        return default


def put_json(name, obj):
    put(name, json.dumps(obj) if obj is not None else "")


def mask(value):
    if not value:
        return ""
    return value[:7] + "…" + value[-4:] if len(value) > 14 else "…" + value[-3:]


def has(name):
    return bool(get(name))
