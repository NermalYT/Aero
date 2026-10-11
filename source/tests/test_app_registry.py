"""Tests for app discovery and launching (app_registry.py, app_catalog.py, tools/app_tools.py): the .lnk and .desktop
parsers, name resolution with confidence and ambiguity, mentions in a message, trust of program folders, and verified
launches (with the process list and the launcher mocked). Run from source/:
    python -m unittest discover -s tests -v
"""
import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aero import app_catalog, app_registry as ar  # noqa: E402


def make_lnk(target, args="", unicode=True):
    """A minimal Windows shortcut: header, LinkInfo with a local base path, and an arguments string."""
    flags = 0x2 | (0x20 if args else 0) | (0x80 if unicode else 0)          # HasLinkInfo, HasArguments, IsUnicode
    header = struct.pack("<I16sI", 0x4C, b"\x01\x14\x02\x00\x00\x00\x00\x00\xc0\x00\x00\x00\x00\x00\x00\x46", flags)
    header += b"\x00" * (76 - len(header))
    base = target.encode("mbcs" if sys.platform == "win32" else "latin-1") + b"\x00"
    hsize = 0x1C
    body_off = hsize
    li = struct.pack("<IIIIIII", 0, hsize, 0x1, 0, body_off, 0, body_off + len(base)) + base + b"\x00"
    li = struct.pack("<I", len(li)) + li[4:]
    out = header + li
    if args:
        out += struct.pack("<H", len(args)) + (args.encode("utf-16-le") if unicode else args.encode("latin-1"))
    return out + b"\x00\x00\x00\x00"


def rec(name, exe="", kind="win32", **kw):
    r = ar._rec(name, kind, "test", exe=exe, launch=[{"method": "exe", "target": exe}] if exe else [], **kw)
    return r


class Parsers(unittest.TestCase):
    def test_lnk_target_and_args(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Bloxstrap.lnk"
            p.write_bytes(make_lnk(r"C:\Users\x\AppData\Local\Bloxstrap\Bloxstrap.exe", "-player"))
            info = ar.parse_lnk(p)
        self.assertEqual(info["target"], r"C:\Users\x\AppData\Local\Bloxstrap\Bloxstrap.exe")
        self.assertEqual(info["args"], "-player")

    def test_lnk_garbage_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.lnk"
            p.write_bytes(b"not a shortcut at all")
            self.assertIsNone(ar.parse_lnk(p))

    def test_desktop_entry(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "org.gnome.Calculator.desktop"
            p.write_text("[Desktop Entry]\nType=Application\nName=Calculator\nGenericName=Calculator\n"
                         "Exec=gnome-calculator %U\n\n[Desktop Action new]\nName=New window\nExec=foo\n")
            e = ar.parse_desktop_entry(p)
            self.assertEqual((e["name"], e["exec"], e["id"]), ("Calculator", "gnome-calculator", "org.gnome.Calculator"))
            p.write_text("[Desktop Entry]\nType=Application\nName=Hidden\nExec=x\nNoDisplay=true\n")
            self.assertIsNone(ar.parse_desktop_entry(p))


class Resolve(unittest.TestCase):
    def setUp(self):
        ar._reset_cache()
        d = ar._load()
        d.update(records=ar._merge([
            rec("Bloxstrap", r"C:\Users\x\AppData\Local\Bloxstrap\Bloxstrap.exe"),
            rec("Discord PTB", r"C:\Users\x\AppData\Local\DiscordPTB\app-1\DiscordPTB.exe"),
            rec("Notepad", r"C:\Windows\System32\notepad.exe"),
            rec("Notepad Next", r"C:\Program Files\NotepadNext\NotepadNext.exe"),
            rec("Steam", r"C:\Program Files (x86)\Steam\steam.exe"),
            rec("Microsoft Word", r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE"),
        ]), learned={}, aliases={}, disabled=[], scanned_at=time.time(), stamp=ar._stamp())
        self.p = mock.patch.object(ar, "handlers", lambda: {})
        self.p.start()

    def tearDown(self):
        self.p.stop()
        ar._reset_cache()

    def test_exact_name_and_catalog(self):
        c = ar.resolve("my Bloxstrap", running=[])
        self.assertEqual(c[0]["id"], "bloxstrap")
        self.assertGreaterEqual(c[0]["confidence"], 0.9)
        self.assertEqual(c[0]["record"]["catalog"], "bloxstrap")
        self.assertIn("roblox", c[0]["related"])

    def test_roblox_is_not_bloxstrap(self):
        c = ar.resolve("roblox", running=[])
        self.assertEqual(c[0]["id"], "roblox")
        self.assertFalse(c[0]["installed"])              # no program and no registered link handler here

    def test_roblox_through_registered_handler(self):
        with mock.patch.object(ar, "handlers", lambda: {"roblox": r"C:\x\Bloxstrap.exe"}):
            c = ar.resolve("roblox", running=[])
        self.assertTrue(c[0]["installed"])
        self.assertIn("Bloxstrap.exe", c[0]["reason"])

    def test_running_and_alias(self):
        c = ar.resolve("discord", running=[{"pid": 1, "name": "DiscordPTB.exe", "exe": ""}])
        self.assertEqual(c[0]["id"], "discordptb")
        self.assertTrue(c[0]["running"])
        ar.set_alias("chat app", "discordptb")
        self.assertEqual(ar.resolve("chat app", running=[])[0]["confidence"], 1.0)

    def test_ambiguous_names_are_flagged(self):
        d = ar._load()
        d["records"] += ar._merge([rec("Acme Writer", r"C:\Program Files\Acme\writer.exe"),
                                   rec("Acme Reader", r"C:\Program Files\Acme\reader.exe")])
        c = ar.resolve("acme", running=[])
        self.assertEqual({c[0]["name"], c[1]["name"]}, {"Acme Writer", "Acme Reader"})
        self.assertTrue(c[0].get("ambiguous"))           # two different programs, same score: ask, don't guess
        self.assertFalse(ar.resolve("acme writer", running=[])[0].get("ambiguous"))

    def test_mentions_common_words(self):
        self.assertEqual(ar.mentions("Give me a word that means calm"), [])
        names = [m["id"] for m in ar.mentions("Fix the totals in my Word document and open Steam")]
        self.assertIn("winword", names)
        self.assertIn("steam", names)
        self.assertEqual(ar.mentions("the steam rose from the kettle"), [])
        line = ar.context_line("Open up my Bloxstrap and find a fun Roblox game")
        self.assertTrue(line.startswith("Apps: "))
        self.assertIn("Bloxstrap (installed", line)

    def test_disabled_app_is_ignored(self):
        ar.set_disabled("steam", True)
        self.assertNotEqual((ar.resolve("steam", running=[]) or [{}])[0].get("id"), "steam")
        ar.set_disabled("steam", False)


class Trust(unittest.TestCase):
    def test_folders(self):
        self.assertEqual(ar.trust_of(""), "")
        self.assertEqual(ar.trust_of(r"\\server\share\tool.exe"), "untrusted")
        self.assertEqual(ar.trust_of(str(Path.home() / "Downloads" / "setup.exe")), "untrusted")
        if sys.platform == "win32":
            self.assertEqual(ar.trust_of(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "x", "x.exe")),
                             "system")
        else:
            self.assertEqual(ar.trust_of("/usr/bin/gedit"), "system")


class Launch(unittest.TestCase):
    def setUp(self):
        ar._reset_cache()
        d = ar._load()
        d.update(records=ar._merge([rec("Bloxstrap", r"C:\Users\x\AppData\Local\Bloxstrap\Bloxstrap.exe"),
                                    rec("Dodgy", str(Path.home() / "Downloads" / "dodgy.exe"))]),
                 learned={}, aliases={}, disabled=[], scanned_at=time.time(), stamp=ar._stamp())
        self.started = []
        self.procs = [{"pid": 10, "name": "explorer.exe", "exe": ""}]
        self.ps = [mock.patch.object(ar, "_start", lambda m, args, bg: self.started.append((m, args, bg))),
                   mock.patch.object(ar, "scan_running", lambda: list(self.procs)),
                   mock.patch.object(ar, "windows_of", lambda pids: [{"id": 99, "title": "Bloxstrap", "pid": pids[0]}]),
                   mock.patch.object(ar, "handlers", lambda: {})]
        for p in self.ps:
            p.start()

    def tearDown(self):
        for p in self.ps:
            p.stop()
        ar._reset_cache()

    def test_verified_start_and_learning(self):
        orig = ar._start

        def start(m, args, bg):
            orig(m, args, bg)
            self.procs.append({"pid": 4242, "name": "Bloxstrap.exe", "exe": ""})
        with mock.patch.object(ar, "_start", start):
            r = ar.launch("bloxstrap", timeout=3)
        self.assertTrue(r["verified"])
        self.assertEqual(r["pids"], [4242])
        self.assertTrue(self.started[0][2])               # background launch by default
        self.assertEqual(ar._load()["learned"]["bloxstrap"]["ok"], 1)

    def test_no_new_process_is_not_success(self):
        r = ar.launch("bloxstrap", timeout=1)
        self.assertFalse(r["verified"])
        self.assertFalse(r["ok"])
        self.assertIn("no new process", r["note"])

    def test_already_running_is_not_claimed_as_started(self):
        self.procs.append({"pid": 5, "name": "Bloxstrap.exe", "exe": ""})
        r = ar.launch("bloxstrap", timeout=4)
        self.assertFalse(r["verified"])
        self.assertTrue(r["ok"])
        self.assertIn("already running", r["note"])

    def test_untrusted_folder_needs_the_user(self):
        r = ar.launch("dodgy", timeout=1)
        self.assertTrue(r.get("needs_trust"))
        self.assertEqual(self.started, [])

    def test_learned_method_is_forgotten_when_the_program_changes(self):
        r = ar.get("bloxstrap")
        ar._learn(r, {"method": "exe", "target": r["exe"]}, ok=True)
        with mock.patch.object(ar, "_exe_sig", lambda exe: "changed"):
            ar._learn(r, {"method": "exe", "target": r["exe"]}, ok=False)
        self.assertEqual(ar._load()["learned"]["bloxstrap"]["ok"], 0)

    def test_uri_goes_through_the_registered_handler(self):
        with mock.patch.object(ar, "protocol_handler", lambda s: r"C:\x\Bloxstrap.exe"):
            r = ar.launch("bloxstrap", uri="roblox://experiences/start?placeId=1818", timeout=1)
        self.assertEqual(self.started[0][0]["method"], "protocol")
        self.assertIn("handled by Bloxstrap.exe", r["note"])


class Catalog(unittest.TestCase):
    def test_catalog_entries_are_well_formed(self):
        for k, e in app_catalog.CATALOG.items():
            self.assertTrue(e["name"] and e["aliases"], k)
            for a in e["aliases"]:
                self.assertTrue(a == a.lower() or a[0].isupper(), (k, a))
            for field in ("exe", "procs", "protocols"):
                for x in e.get(field, []):
                    self.assertNotIn("\\", x, (k, x))        # names only, never paths
        self.assertEqual(app_catalog.by_exe("bloxstrap.exe"), "bloxstrap")
        self.assertIn("roblox", app_catalog.by_protocol("roblox"))


if __name__ == "__main__":
    unittest.main()
