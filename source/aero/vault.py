"""Local secret store (API keys, GitHub token, MCP OAuth tokens).

- Windows: values are encrypted with DPAPI (CryptProtectData, bound to the current Windows user), so the file
  data/secrets.json is useless if copied to another account or PC.
- macOS: values live in the login Keychain (service "Aero"); secrets.json only notes that they are there.
- Linux: values live in the desktop keyring through the Secret Service (secret-tool, from libsecret) when one is
  running; otherwise they stay in secrets.json, which only your user account can read (mode 600).
Secrets never leave this machine except to the service they belong to, and the UI only ever sees a masked form.
"""
import base64
import json
import os
import shutil
import subprocess
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


KEYCHAIN = "@keychain"          # secrets.json value: the secret itself is in the OS keychain


def _keychain_put(name, value):
    """Store in the OS keychain. True on success; False sends the value to the file instead."""
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["security", "add-generic-password", "-U", "-s", "Aero", "-a", name, "-w", value],
                               capture_output=True, timeout=20)
            return r.returncode == 0
        if sys.platform.startswith("linux") and shutil.which("secret-tool") and os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            r = subprocess.run(["secret-tool", "store", "--label", f"Aero: {name}", "application", "aero", "name", name],
                               input=value.encode("utf-8"), capture_output=True, timeout=20)
            return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False


def _keychain_get(name):
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["security", "find-generic-password", "-s", "Aero", "-a", name, "-w"],
                               capture_output=True, timeout=20)
        elif shutil.which("secret-tool"):
            r = subprocess.run(["secret-tool", "lookup", "application", "aero", "name", name],
                               capture_output=True, timeout=20)
        else:
            return None
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.decode("utf-8").rstrip("\n") if r.returncode == 0 else None


def _keychain_del(name):
    try:
        if sys.platform == "darwin":
            subprocess.run(["security", "delete-generic-password", "-s", "Aero", "-a", name], capture_output=True,
                           timeout=20)
        elif shutil.which("secret-tool"):
            subprocess.run(["secret-tool", "clear", "application", "aero", "name", name], capture_output=True,
                           timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _load():
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d):
    tmp = PATH.with_suffix(".tmp")
    if sys.platform != "win32":
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # readable by this user only
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(d, indent=1))
    else:
        tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.replace(tmp, PATH)


def get(name, default=""):
    with _lock:
        v = _load().get(name)
    if not v:
        return default
    if v == KEYCHAIN:
        return _keychain_get(name) or default
    try:
        return _unprotect(base64.b64decode(v)).decode("utf-8")
    except Exception:
        return default


def put(name, value):
    with _lock:
        d = _load()
        if d.get(name) == KEYCHAIN:
            _keychain_del(name)
        if value and sys.platform != "win32" and os.environ.get("AERO_VAULT") != "file" and _keychain_put(name, str(value)):
            d[name] = KEYCHAIN
        elif value:
            d[name] = base64.b64encode(_protect(str(value).encode("utf-8"))).decode("ascii")
        else:
            d.pop(name, None)
        _save(d)


def where():
    """Where secrets are kept on this computer, for Settings → Privacy."""
    if sys.platform == "win32":
        return "encrypted with Windows DPAPI for your Windows account"
    if sys.platform == "darwin":
        return "in your macOS login Keychain (service \"Aero\")"
    d = _load()
    if any(v == KEYCHAIN for v in d.values()):
        return "in your desktop keyring (Secret Service)"
    return f"in {PATH} (readable by your user account only)"


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
