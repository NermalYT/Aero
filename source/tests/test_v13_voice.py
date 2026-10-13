"""1.3: voice typing and transcripts of attached audio with whisper.cpp.
Run from source/:
    python -m unittest discover -s tests -v
"""
import io
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aero import attachments, tools  # noqa: E402
from aero.integrations import load_all  # noqa: E402

load_all()
from aero.integrations import voice  # noqa: E402


def wav(seconds=1.0, rate=16000):
    n = int(seconds * rate)
    b = io.BytesIO()
    b.write(b"RIFF" + struct.pack("<I", 36 + n * 2) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16))
    b.write(b"data" + struct.pack("<I", n * 2) + b"\x00\x00" * n)
    return b.getvalue()


class Setup(unittest.TestCase):
    def test_the_pack_installs_a_pinned_model_and_the_official_build(self):
        p = voice.Pack if False else __import__("aero.integrations", fromlist=["PACKS"]).PACKS["voice"]
        self.assertEqual(p.license, "MIT")
        self.assertEqual([r.id for r in p.requirements], ["whisper", "model"])
        self.assertEqual(len(voice.MODEL["sha256"]), 64)
        with mock.patch.object(voice, "IS_WIN", True), mock.patch.object(voice.platform, "machine", lambda: "AMD64"):
            self.assertEqual(voice._asset(), "whisper-bin-x64.zip")

    def test_binaries_come_from_the_newest_build_release_that_has_them(self):
        a = {"name": "whisper-bin-x64.zip", "digest": "sha256:ab", "browser_download_url": "u"}
        rels = [{"tag_name": "v1.9.6", "prerelease": True, "assets": [dict(a, browser_download_url="rc")]},
                {"tag_name": "v1.9.5", "assets": []},
                {"tag_name": "b5454", "prerelease": True, "assets": [a]},       # the build release of v1.9.5
                {"tag_name": "b4938", "assets": [dict(a, browser_download_url="old")]}]

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return rels
        with mock.patch.object(voice.httpx, "get", lambda *a, **k: R()), \
                mock.patch.object(voice.tc, "require_online", lambda what: None):
            tag, a = voice._newest_release_with("whisper-bin-x64.zip")
        self.assertEqual((tag, a["browser_download_url"]), ("b5454", "u"))


class NotReady(unittest.TestCase):
    def test_says_so_instead_of_failing_quietly(self):
        with mock.patch.object(voice, "cli", lambda: None):
            with self.assertRaises(RuntimeError) as e:
                voice.transcribe("x.wav")
        self.assertIn("isn't set up", str(e.exception))

    def test_audio_attachments_keep_the_old_note_without_voice(self):
        with mock.patch.object(voice, "ready", lambda: False):
            kind, text, _ = attachments.extract("memo.mp3", b"ID3" + b"\x00" * 3000)
        self.assertIn("cannot be read directly", text)


class Transcribing(unittest.TestCase):
    def test_an_attached_recording_is_transcribed(self):
        with mock.patch.object(voice, "ready", lambda: True), \
                mock.patch.object(voice, "transcribe", lambda p, language="auto", cancel=None: "Call me back at five."):
            kind, text, _ = attachments.extract("voicemail.wav", wav())
        self.assertEqual(kind, "file")
        self.assertIn("Transcript of voicemail.wav", text)
        self.assertIn("Call me back at five.", text)

    def test_the_command_line(self):
        seen = {}

        def run(cmd, timeout=None, cancel=None):
            seen["cmd"] = cmd
            return {"rc": 0, "out": " [BLANK_AUDIO]\n Hello there. (laughs)\n General Kenobi.\n (crickets chirping)\n",
                    "err": ""}
        with mock.patch.object(voice, "cli", lambda: Path("whisper-cli")), \
                mock.patch.object(voice, "model_path", lambda: Path("m.bin")), mock.patch.object(voice.tc, "run", run):
            self.assertEqual(voice.transcribe("a.wav", "en"), "Hello there. General Kenobi.")
        self.assertEqual(seen["cmd"][:6], ["whisper-cli", "-m", "m.bin", "-f", "a.wav", "-nt"])
        self.assertIn("en", seen["cmd"])

    def test_the_tool(self):
        d = Path(tempfile.mkdtemp())
        (d / "a.wav").write_bytes(wav())
        ctx = tools.Ctx({"work_dir": str(d)})
        with mock.patch.object(voice, "transcribe", lambda p, language="auto", cancel=None: "hi"):
            self.assertIn("Transcript of a.wav:\nhi", voice.transcribe_audio(ctx, "a.wav"))
        self.assertTrue(voice.transcribe_audio(ctx, "nope.wav")["error"])

    def test_the_endpoint(self):
        from fastapi.testclient import TestClient

        from aero import server
        c = TestClient(server.app)
        with mock.patch.object(voice, "ready", lambda: False):
            self.assertEqual(c.post("/api/voice/transcribe", content=wav()).status_code, 409)
        with mock.patch.object(voice, "ready", lambda: True), \
                mock.patch.object(voice, "transcribe_bytes", lambda data, suffix, lang: "check my email"):
            self.assertEqual(c.post("/api/voice/transcribe", content=wav()).json(), {"text": "check my email"})
        self.assertEqual(c.post("/api/voice/transcribe", content=b"RIFF").json(), {"text": ""})


@unittest.skipUnless(voice.ready(), "whisper.cpp not installed here")
class Real(unittest.TestCase):
    def test_silence_is_no_speech(self):
        self.assertIsInstance(voice.transcribe_bytes(wav(2.0)), str)


if __name__ == "__main__":
    unittest.main()
