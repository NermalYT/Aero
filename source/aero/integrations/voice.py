"""Voice (1.3): speech to text on this PC with ggml-org/whisper.cpp (MIT), the same family as llama.cpp.

- The microphone button in the chat box records you (in the app window, 16 kHz mono WAV) and puts what you said into
  the message box, to read and send. Nothing leaves the PC.
- Audio files you attach (WAV, MP3, FLAC) are transcribed, so the model reads what is said in them.
- The transcribe_audio tool does the same for an audio file on disk.

whisper-cli and the multilingual Whisper base model (8-bit, 82 MB; OpenAI's weights, MIT) are downloaded from the
projects' own releases with their published SHA-256. Windows x64 and Linux x64 get the official build; on macOS
`brew install whisper-cpp` provides whisper-cli (Aero then downloads only the model).
"""
import os
import platform
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import httpx

from . import Pack, Requirement, register, toolchain as tc
from ..config import IS_WIN
from ..tools import tool

DIR = tc.ROOT / "whisper"
MODEL = {"name": "ggml-base-q8_0.bin", "size": 81768585,
         "sha256": "c577b9a86e7e048a0b7eada054f4dd79a56bbfa911fbdacf900ac5b567cbb7d9",
         "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base-q8_0.bin"}
AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg", ".oga"}
MAX_S = 600                      # one transcription at most this long (a 2-hour recording would take a while)
_lock = threading.Lock()
NOT_SPEECH = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|[♪♫]+")


def _asset():
    if IS_WIN and platform.machine().lower() in ("amd64", "x86_64"):
        return "whisper-bin-x64.zip"
    if sys.platform.startswith("linux") and platform.machine().lower() in ("x86_64", "amd64"):
        return "whisper-bin-ubuntu-x64.tar.gz"
    return None


def cli():
    for p in (DIR / "bin" / "Release" / "whisper-cli.exe", DIR / "bin" / "whisper-cli.exe", DIR / "bin" / "whisper-cli",
              DIR / "bin" / "build" / "bin" / "whisper-cli"):
        if p.exists():
            return p
    w = shutil.which("whisper-cli")
    return Path(w) if w else None


def model_path():
    p = DIR / MODEL["name"]
    return p if p.exists() and p.stat().st_size == MODEL["size"] else None


def ready():
    return bool(cli() and model_path())


def _cli_ok():
    c = cli()
    return (True, str(c)) if c else (False, "not installed")


def _model_ok():
    return (True, MODEL["name"]) if model_path() else (False, "not downloaded")


def _newest_release_with(asset):
    """whisper.cpp publishes its binaries on the build releases (bNNNN, marked pre-release, made with each version
    release), not on the version tags."""
    tc.require_online("look up whisper.cpp's releases")
    r = httpx.get("https://api.github.com/repos/ggml-org/whisper.cpp/releases?per_page=15",
                  headers={"Accept": "application/vnd.github+json"}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    for rel in r.json():
        if rel.get("draft") or (rel.get("prerelease") and not re.fullmatch(r"b\d+", rel.get("tag_name") or "")):
            continue
        a = next((a for a in rel.get("assets") or [] if a["name"] == asset), None)
        if a and a.get("digest"):
            return rel["tag_name"], a
    raise RuntimeError(f"No recent whisper.cpp release has {asset}.")


def install_cli(emit=None, cancel=None):
    asset = _asset()
    if not asset:
        raise RuntimeError("No official whisper.cpp build for this system: on macOS run `brew install whisper-cpp`.")
    tag, a = _newest_release_with(asset)
    arc = DIR / asset
    tc.download(a["browser_download_url"], arc, emit, cancel, sha256=a["digest"].replace("sha256:", ""), label=asset)
    out = DIR / "bin"
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    tc.extract(arc, out)
    arc.unlink(missing_ok=True)
    c = cli()
    if not c:
        raise RuntimeError(f"whisper.cpp {tag}: whisper-cli is not in {asset}.")
    if not IS_WIN:
        c.chmod(0o755)
    tc.mark("whisper", version=tag, sha256=a["digest"])
    return f"whisper.cpp {tag}"


def install_model(emit=None, cancel=None):
    tc.download(MODEL["url"], DIR / MODEL["name"], emit, cancel, sha256=MODEL["sha256"], label=MODEL["name"])
    return MODEL["name"]


def transcribe(path, language="auto", threads=None, cancel=None):
    """The words spoken in an audio file (WAV, MP3, FLAC). Raises RuntimeError when voice isn't set up or the
    file can't be read."""
    c, m = cli(), model_path()
    if not (c and m):
        raise RuntimeError("Voice isn't set up yet (whisper.cpp and its model, about 90 MB). Aero installs it in "
                           "the background; Integrations → Create shows its progress.")
    lang = language if language and len(language) <= 5 else "auto"
    t = threads or max(2, min(8, (os.cpu_count() or 4) // 2))
    with _lock:
        r = tc.run([str(c), "-m", str(m), "-f", str(path), "-nt", "-np", "-sns", "-l", lang, "-t", str(t)],
                   timeout=MAX_S, cancel=cancel)
    if r["rc"] != 0:
        tail = (r["err"] or r["out"]).strip().splitlines()[-3:]
        raise RuntimeError("whisper.cpp couldn't read this audio: " + " ".join(tail)[:300])
    # whisper names sounds that aren't speech ("(crickets chirping)", "[BLANK_AUDIO]", "♪"): not words you said
    words = (NOT_SPEECH.sub(" ", x).strip() for x in r["out"].splitlines())
    return " ".join(w for w in words if w)


def transcribe_bytes(data, suffix=".wav", language="auto"):
    with tempfile.TemporaryDirectory(prefix="aero-voice-") as d:
        p = Path(d) / ("audio" + (suffix if suffix.lower() in AUDIO_EXT else ".wav"))
        p.write_bytes(data)
        return transcribe(p, language)


def transcribe_audio(ctx, path, language="auto"):
    p = ctx.path(path)
    if not p.exists():
        return {"text": f"No such file: {p}", "error": True}
    if p.suffix.lower() not in AUDIO_EXT:
        return {"text": f"{p.name} isn't an audio file whisper.cpp reads (WAV, MP3, FLAC).", "error": True}
    try:
        text = transcribe(p, language, cancel=ctx.cancel)
    except RuntimeError as e:
        return {"text": str(e), "error": True}
    return f"Transcript of {p.name}:\n{text or '(no speech found)'}"


tool("transcribe_audio",
     "Transcribe speech in an audio file on this PC (WAV, MP3, FLAC) with whisper.cpp, offline.",
     "files_read",
     {"path": {"type": "string", "description": "the audio file"},
      "language": {"type": "string", "description": "a language code such as en or de; auto detects it"}},
     ["path"], summary=lambda a: str(a.get("path") or ""))(transcribe_audio)


def _panel():
    return {"ready": ready(), "model": MODEL["name"], "cli": str(cli() or "")}


register(Pack("voice", "Voice typing", "create",
              "Speak instead of typing (the microphone button in the chat box), and transcripts of attached audio, "
              "with whisper.cpp on this PC",
              "https://github.com/ggml-org/whisper.cpp", "MIT",
              triggers=[r"\b(transcri(be|pt|ption)|speech to text|dictat(e|ion))\b",
                        r"\b(audio|voice|recording|podcast|voicemail)\b.{0,30}\b(file|says?|said|text)\b"],
              tools=["transcribe_audio"],
              requirements=[Requirement("whisper", "whisper.cpp (whisper-cli, about 9 MB)", _cli_ok, install=install_cli,
                                        how="macOS: brew install whisper-cpp", size_mb=9),
                            Requirement("model", "Whisper base model, multilingual (82 MB)", _model_ok,
                                        install=install_model, size_mb=82)],
              panel=_panel,
              notes="Runs on the CPU. The base model handles clear speech in most languages; long recordings take a "
                    "while."))
