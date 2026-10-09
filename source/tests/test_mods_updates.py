"""Tests for mods, updates and the loop journal: mods (a prompt edits a copy of Aero, checks, apply, undo, boot safety, carrying mods across
an update), the forever-loop journal, the update check and verified download, and the OS layer. Mods run against a
tiny stand-in app in a temp folder, never against this checkout. The model is the scripted fake (tests/fake_llama.py).
Run from source/:
    python -m unittest discover -s tests -v
"""
import asyncio
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import unittest
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import fake_llama  # noqa: E402
from aero import agent, config, looplog, mods, osinfo, pipeline, tools, updater  # noqa: E402

tools.load_all()

CSS = ".send { background: blue; }\n.other { color: red; }\n"
SERVER = '"""stand-in server module"""\nVALUE = 1\n'


def sse_events(chunks):
    return [json.loads(c[6:]) for c in chunks if c.startswith("data: ")]


class TinyApp:
    """Point mods.py at a small fake app folder and its own data folder."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="aero-mods-"))
        self.app = self.root / "app"
        (self.app / "aero" / "static").mkdir(parents=True)
        (self.app / "aero" / "__init__.py").write_text("")
        (self.app / "aero" / "server.py").write_text(SERVER)
        (self.app / "aero" / "static" / "app.css").write_text(CSS)
        data = self.root / "data" / "mods"
        self.patches = [mock.patch.object(mods, "APP_DIR", self.app), mock.patch.object(mods, "MODS", data),
                        mock.patch.object(mods, "ORDER", data / "order.json"),
                        mock.patch.object(mods, "BOOT", data / "boot.json"),
                        mock.patch.object(mods, "MARK", self.app / "aero" / "mods-applied.json"),
                        mock.patch.object(mods, "_boot_test", lambda w, home: (True, ""))]

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def css(self):
        return (self.app / "aero" / "static" / "app.css").read_text()


class Mods(unittest.TestCase):
    def test_edit_check_apply_undo(self):
        with TinyApp() as t:
            rec = mods.new("Make the send button green")
            self.assertEqual((rec["status"], rec["name"]), ("draft", "Make the send button green"))
            w = mods.work_dir(rec["id"])
            (w / "aero" / "static" / "app.css").write_text(CSS.replace("blue", "green"))
            ch = mods.changes(rec["id"])
            self.assertEqual([(c["path"], c["status"], c["plus"], c["minus"]) for c in ch],
                             [("aero/static/app.css", "modified", 1, 1)])
            self.assertIn("-.send { background: blue; }", mods.diff_text(rec["id"]))
            with self.assertRaises(RuntimeError):
                mods.apply(rec["id"])                      # no passing checks yet
            res = mods.check(rec["id"], tests=False)
            self.assertTrue(res["ok"], res)
            self.assertTrue(mods.checked(mods.get(rec["id"])))
            out = mods.apply(rec["id"])
            self.assertEqual(out, {"restart": False, "files": ["aero/static/app.css"]})   # CSS only: a reload is enough
            self.assertIn("green", t.css())
            self.assertEqual(json.loads(mods.MARK.read_text())["mods"], [rec["id"]])
            mods.undo(rec["id"])
            self.assertEqual(t.css(), CSS)
            self.assertEqual(mods.get(rec["id"])["status"], "off")
            mods.turn_on(rec["id"])
            self.assertIn("green", t.css())
            mods.delete(rec["id"])
            self.assertEqual(t.css(), CSS)
            self.assertEqual(mods.listing(), [])

    def test_check_catches_broken_python(self):
        with TinyApp():
            rec = mods.new("break it")
            (mods.work_dir(rec["id"]) / "aero" / "server.py").write_text("def broken(:\n")
            res = mods.check(rec["id"], tests=False)
            self.assertFalse(res["ok"])
            first = res["steps"][0]
            self.assertEqual((first["name"], first["ok"]), ("Python files compile", False))
            self.assertIn("aero/server.py, line 1", first["detail"])
            self.assertEqual(len(res["steps"]), 2)              # stops at the failed import, no boot test

    def test_python_change_needs_restart_and_boot_guard_undoes_it(self):
        with TinyApp() as t:
            rec = mods.new("change the server")
            (mods.work_dir(rec["id"]) / "aero" / "server.py").write_text(SERVER.replace("1", "2"))
            self.assertTrue(mods.apply(rec["id"], force=True)["restart"])
            self.assertEqual(json.loads(mods.BOOT.read_text())["pending"], rec["id"])
            log = []
            mods.boot_guard(log=log.append)                    # first start with the mod: allowed one try
            self.assertIn("VALUE = 2", (t.app / "aero" / "server.py").read_text())
            mods.boot_guard(log=log.append)                    # it never reached boot_ok: undo it
            self.assertIn("VALUE = 1", (t.app / "aero" / "server.py").read_text())
            self.assertEqual(mods.get(rec["id"])["status"], "broken")
            self.assertFalse(mods.BOOT.exists())

    def test_boot_ok_keeps_the_mod(self):
        with TinyApp() as t:
            rec = mods.new("change the server")
            (mods.work_dir(rec["id"]) / "aero" / "server.py").write_text(SERVER.replace("1", "3"))
            mods.apply(rec["id"], force=True)
            mods.boot_guard(log=lambda m: None)
            mods.boot_ok()
            mods.boot_guard(log=lambda m: None)
            self.assertIn("VALUE = 3", (t.app / "aero" / "server.py").read_text())
            self.assertEqual(mods.get(rec["id"])["status"], "applied")

    def test_safe_mode_turns_every_mod_off(self):
        with TinyApp() as t:
            rec = mods.new("green")
            (mods.work_dir(rec["id"]) / "aero" / "static" / "app.css").write_text(CSS.replace("blue", "green"))
            mods.apply(rec["id"], force=True)
            mods.boot_guard(safe=True, log=lambda m: None)
            self.assertEqual(t.css(), CSS)
            self.assertEqual(mods.get(rec["id"])["status"], "off")

    def test_mods_come_back_after_an_update(self):
        with TinyApp() as t:
            rec = mods.new("green")
            (mods.work_dir(rec["id"]) / "aero" / "static" / "app.css").write_text(CSS.replace("blue", "green"))
            mods.apply(rec["id"], force=True)
            # an update replaces the app folder: new lines in the same file, no marker
            (t.app / "aero" / "static" / "app.css").write_text(CSS + ".new-in-update { margin: 0; }\n")
            mods.MARK.unlink()
            self.assertEqual(mods.reapply_after_update(log=lambda m: None), [])
            css = t.css()
            self.assertIn("green", css)
            self.assertIn(".new-in-update", css)
            self.assertTrue(mods.MARK.exists())

    def test_update_that_rewrote_the_same_line_marks_the_mod_for_redo(self):
        with TinyApp() as t:
            rec = mods.new("green")
            (mods.work_dir(rec["id"]) / "aero" / "static" / "app.css").write_text(CSS.replace("blue", "green"))
            mods.apply(rec["id"], force=True)
            (t.app / "aero" / "static" / "app.css").write_text(".send { background: purple; }\n.other { color: red; }\n")
            mods.MARK.unlink()
            self.assertEqual(mods.reapply_after_update(log=lambda m: None), [rec["id"]])
            self.assertIn("purple", t.css())
            self.assertEqual(mods.get(rec["id"])["status"], "needs_redo")
            again, created = mods.for_turn(rec["id"], None, "again")     # its chat makes it again on the new code
            self.assertFalse(created)
            self.assertEqual(again["status"], "draft")
            self.assertIn("Redo an older mod", mods.persona(rec["id"]))
            self.assertEqual(mods.changes(rec["id"]), [])

    def test_draft_from_before_an_update_does_not_undo_it(self):
        with TinyApp() as t:
            rec = mods.new("green")
            (mods.work_dir(rec["id"]) / "aero" / "static" / "app.css").write_text(CSS.replace("blue", "green"))
            (t.app / "aero" / "static" / "app.css").write_text(CSS + ".added-later {}\n")   # live changed meanwhile
            (t.app / "aero" / "server.py").write_text(SERVER + "LATER = True\n")
            self.assertEqual([c["path"] for c in mods.changes(rec["id"])], ["aero/static/app.css"])
            mods.apply(rec["id"], force=True)
            self.assertIn("green", t.css())
            self.assertIn(".added-later", t.css())
            self.assertIn("LATER = True", (t.app / "aero" / "server.py").read_text())

    def test_applied_mod_gets_a_follow_up_mod(self):
        with TinyApp():
            rec = mods.new("green")
            (mods.work_dir(rec["id"]) / "aero" / "static" / "app.css").write_text(CSS.replace("blue", "green"))
            mods.apply(rec["id"], force=True)
            nxt, created = mods.for_turn(rec["id"], "chat-1", "and make it round")
            self.assertTrue(created)
            self.assertEqual((nxt["parent"], nxt["status"], nxt["chat_id"]), (rec["id"], "draft", "chat-1"))
            self.assertEqual(mods.changes(nxt["id"]), [])

    def test_patch_text(self):
        old = "a\nb\nc\nd\ne\nf\ng\nh\n"
        new = old.replace("b\n", "B\n").replace("g\n", "G\n")
        cur = "zero\n" + old.replace("e\n", "E\n")
        self.assertEqual(mods.patch_text(cur, old, new), "zero\na\nB\nc\nd\nE\nf\nG\nh\n")
        self.assertIsNone(mods.patch_text(old.replace("b\n", "x\n"), old, new))

    def test_mod_turn_writes_only_in_the_copy(self):
        with TinyApp() as t:
            rec = mods.new("Make the send button green")
            s = mods.chat_settings(config.load_settings(), rec["id"])
            turn = agent.Turn("m1", [], s, {"url": "x", "ctx": 8192, "model": {"name": "m"}})
            mods.prepare_turn(turn, rec["id"])
            names = [x["function"]["name"] for x in agent.current_schemas(turn)]
            self.assertEqual(set(names), set(mods.MOD_TOOLS))
            self.assertTrue(agent.sandbox_ok(turn, "write_file", {"path": "aero/static/app.css"}))
            self.assertFalse(agent.sandbox_ok(turn, "write_file", {"path": str(t.app / "aero" / "server.py")}))
            self.assertFalse(agent.sandbox_ok(turn, "edit_file", {"path": "../../../../outside.txt"}))
            self.assertFalse(agent.sandbox_ok(turn, "move_path", {"source": "aero/server.py", "destination": "/tmp/x"}))
            turn.close()

    def test_whole_mod_chat_turn(self):
        with TinyApp() as t:
            rec = mods.new("Make the send button green")
            real_client = httpx.AsyncClient
            handler = fake_llama.mock_handler()
            with mock.patch.object(agent.httpx, "AsyncClient",
                                   lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)):
                s = config.load_settings()
                s.update({"router_enabled": False, "memory_enabled": False, "profile_enabled": False})
                hist = [{"role": "user", "content": "Make the send button green"}]

                async def go():
                    return [c async for c in pipeline.run_turn("mod-chat", hist, s, {"url": "http://fake", "ctx": 32768,
                                                               "model": {"name": "Qwen"}}, opts={"mod": rec["id"]})]
                evs = sse_events(asyncio.run(go()))
            kinds = [e["t"] for e in evs]
            self.assertNotIn("error", kinds, [e for e in evs if e["t"] == "error"])
            self.assertNotIn("router_start", kinds)
            calls = [e["message"]["name"] for e in evs if e["t"] == "tool_result"]
            self.assertEqual(calls, ["search_files", "write_file", "mod_check"])
            check = next(e for e in evs if e["t"] == "tool_result" and e["message"]["name"] == "mod_check")["message"]
            self.assertFalse(check["error"], check["content"])
            self.assertNotIn("mod_checking", kinds)           # the model's own mod_check covered this exact state
            ready = next(e for e in evs if e["t"] == "mod_ready")["mod"]
            self.assertEqual([f["path"] for f in ready["files"]], ["aero/static/app.css"])
            self.assertTrue(ready["checks"]["ok"])
            self.assertEqual(t.css(), CSS)                     # nothing reaches the real app before Apply
            mods.apply(rec["id"])
            self.assertIn("Mod: green send button", t.css())


class LoopJournal(unittest.TestCase):
    def setUp(self):
        self.task = "Keep improving the README until it is great " + os.urandom(4).hex()

    def tearDown(self):
        looplog.clear(self.task)

    def test_summary_keeps_newest_side_and_dedupes(self):
        j = looplog.load(self.task)
        j["entries"] = [{"iteration": 1, "worked": ["Use search_files first"], "failed": ["pip install without venv"],
                         "next": "Add examples", "status": "draft"},
                        {"iteration": 2, "worked": ["use search_files first", "pip install without venv"], "failed": [],
                         "next": "Add a table of contents", "status": "examples added"}]
        looplog.save(j)
        s = looplog.summary(looplog.load(self.task))
        self.assertEqual(s["worked"], ["use search_files first", "pip install without venv"])
        self.assertEqual(s["failed"], [])                 # it worked later, so it no longer counts as failing
        self.assertEqual((s["next"], s["status"], s["rounds"]), ("Add a table of contents", "examples added", 2))
        block = looplog.block(self.task)
        self.assertIn("forever-loop journal (2 earlier rounds", block)
        self.assertIn("Planned next step: Add a table of contents", block)

    def test_loop_round_writes_and_then_uses_the_journal(self):
        seen = []
        handler = fake_llama.mock_handler()

        async def spy(request):
            seen.append(json.loads(request.content or b"{}"))
            return await handler(request)
        real_client = httpx.AsyncClient
        client = lambda **kw: real_client(transport=httpx.MockTransport(spy), **kw)  # noqa: E731
        s = config.load_settings()
        s.update({"router_enabled": False, "memory_enabled": False, "profile_enabled": False,
                  "tool_policy": {**s["tool_policy"], "agents": "off", "desktop": "off"}})

        def round_(i):
            hist = [{"role": "user", "content": self.task, "loop": {"iteration": i, "started": 1}}]

            async def go():
                return [c async for c in pipeline.run_turn(f"loop-{i}", hist, s, {"url": "http://fake", "ctx": 32768,
                                                           "model": {"name": "Qwen"}}, opts={"loop": {"iteration": i}})]
            with mock.patch.object(agent.httpx, "AsyncClient", client), \
                    mock.patch.object(looplog.httpx, "AsyncClient", client):
                return sse_events(asyncio.run(go()))
        evs = round_(1)
        note = next(e for e in evs if e["t"] == "loopnote")["message"]
        self.assertEqual(note["worked"], fake_llama.JOURNAL["worked"])
        self.assertEqual(note["key"], looplog.key(self.task))
        self.assertTrue(any(b.get("response_format", {}).get("json_schema", {}).get("name") == "loop_journal" for b in seen))
        seen.clear()
        round_(2)
        first = next(b for b in seen if b.get("stream"))
        user = [m for m in first["messages"] if m["role"] == "user"][-1]["content"]
        text = user if isinstance(user, str) else json.dumps(user)
        self.assertIn("Your forever-loop journal (1 earlier round", text)
        self.assertIn("Didn't work (don't repeat):", text)
        self.assertEqual(len(looplog.load(self.task)["entries"]), 2)

    def test_journal_off(self):
        s = config.load_settings()
        s.update({"loop_journal": False, "router_enabled": False, "memory_enabled": False, "profile_enabled": False,
                  "tool_policy": {**s["tool_policy"], "agents": "off", "desktop": "off"}})
        real_client = httpx.AsyncClient
        client = lambda **kw: real_client(transport=httpx.MockTransport(fake_llama.mock_handler()), **kw)  # noqa: E731
        hist = [{"role": "user", "content": self.task, "loop": {"iteration": 1, "started": 1}}]

        async def go():
            return [c async for c in pipeline.run_turn("loop-x", hist, s, {"url": "http://fake", "ctx": 32768,
                                                       "model": {"name": "Qwen"}}, opts={"loop": {"iteration": 1}})]
        with mock.patch.object(agent.httpx, "AsyncClient", client):
            evs = sse_events(asyncio.run(go()))
        self.assertFalse(any(e["t"] == "loopnote" for e in evs))


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class Updates(unittest.TestCase):
    """A fake GitHub: the releases API answer and the release files, served from a temp folder on localhost."""

    def setUp(self):
        self.www = Path(tempfile.mkdtemp(prefix="aero-rel-"))
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(self.www)))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        self.data = Path(tempfile.mkdtemp(prefix="aero-upd-"))
        self.patches = [mock.patch.object(updater, "API", self.base + "/api.json"),
                        mock.patch.object(updater, "UPDATES", self.data),
                        mock.patch.object(updater, "install_kind", lambda: "unix")]
        for p in self.patches:
            p.start()
        updater.STATE.update(latest=None, available=False, error=None, asset=None, sums=None, checking=False)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.www, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    def release(self, version, files=None, bad_hash=False, inner_version=None):
        name = updater.asset_name()
        files = files or {"Aero/install.sh": "#!/bin/sh\n", "Aero/Update-Aero.bat": "@echo off\r\n",
                          "Aero/source/aero/__main__.py": "",
                          "Aero/source/aero/config.py": f'VERSION = "{inner_version or version}"\n'}
        buf = io.BytesIO()
        if name.endswith(".zip"):
            with zipfile.ZipFile(buf, "w") as z:
                for k, v in files.items():
                    z.writestr(k, v)
        else:
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                for k, v in files.items():
                    data = v.encode()
                    info = tarfile.TarInfo(k)
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
        blob = buf.getvalue()
        (self.www / name).write_bytes(blob)
        digest = hashlib.sha256(blob).hexdigest()
        if bad_hash:
            digest = "0" * 64
        (self.www / "SHA256SUMS.txt").write_text(f"{digest}  {name}\n")
        (self.www / "api.json").write_text(json.dumps({
            "tag_name": f"v{version}", "html_url": self.base, "body": "What's new", "published_at": "2026-10-09",
            "draft": False, "prerelease": False,
            "assets": [{"name": name, "browser_download_url": f"{self.base}/{name}", "size": len(blob)},
                       {"name": "SHA256SUMS.txt", "browser_download_url": f"{self.base}/SHA256SUMS.txt"}]}))

    def test_versions(self):
        self.assertTrue(updater.newer("1.1.0", "1.0.1"))
        self.assertTrue(updater.newer("v1.10.0", "1.9.9"))
        self.assertFalse(updater.newer("1.1.0", "1.1.0"))
        self.assertTrue(updater.newer("1.2.0", "1.2.0-beta.1"))
        self.assertFalse(updater.newer("garbage", "0.0.1"))

    def test_check_finds_a_newer_release(self):
        self.release("9.0.0")
        st = updater.check(force=True)
        self.assertEqual((st["latest"], st["available"], st["error"]), ("9.0.0", True, None))
        self.release(config.VERSION)
        self.assertFalse(updater.check(force=True)["available"])

    def test_check_respects_the_switch_and_strict_offline(self):
        self.release("9.0.0")
        with mock.patch.object(updater, "load_settings", lambda: {"update_check": False}):
            self.assertIsNone(updater.check()["latest"])
        with mock.patch.object(updater, "load_settings", lambda: {"strict_offline": True}):
            self.assertIn("Strict offline", updater.check(force=True)["error"])

    def test_prepare_verifies_and_unpacks(self):
        self.release("9.0.0")
        updater.check(force=True)
        events = []
        root = updater.prepare(events.append)
        self.assertTrue((root / "source" / "aero" / "__main__.py").exists())
        self.assertEqual(updater._version_in(root), "9.0.0")
        self.assertEqual(events[-1]["phase"], "ready")

    def test_prepare_refuses_a_bad_checksum(self):
        self.release("9.0.0", bad_hash=True)
        updater.check(force=True)
        with self.assertRaisesRegex(updater.UpdateError, "SHA-256"):
            updater.prepare()

    def test_prepare_refuses_mismatched_version(self):
        self.release("9.0.0", inner_version="1.0.0")
        updater.check(force=True)
        with self.assertRaisesRegex(updater.UpdateError, "code inside says 1.0.0"):
            updater.prepare()

    def test_prepare_refuses_paths_outside(self):
        self.release("9.0.0", files={"Aero/source/aero/__main__.py": "", "../evil.txt": "x",
                                     "Aero/source/aero/config.py": 'VERSION = "9.0.0"\n'})
        updater.check(force=True)
        with self.assertRaisesRegex(updater.UpdateError, "unsafe path"):
            updater.prepare()
        self.assertFalse((self.data.parent / "evil.txt").exists())

    def test_source_checkout_cannot_replace_itself(self):
        self.release("9.0.0")
        with mock.patch.object(updater, "install_kind", lambda: "source"):
            updater.check(force=True)
            with self.assertRaisesRegex(updater.UpdateError, "source folder"):
                updater.prepare()

    def test_handoff_runs_the_release_installer(self):
        root = self.data / "rel"
        root.mkdir()
        with mock.patch.object(updater.subprocess, "Popen") as popen:
            updater.handoff(root, headless=True)()
        args = popen.call_args[0][0]
        if osinfo.IS_WIN:
            self.assertEqual(args[-1], "--auto")
        else:
            self.assertEqual(args[:3], ["sh", str(root / "install.sh"), "--update"])
            self.assertIn("--headless", args)
            self.assertEqual(popen.call_args[1]["start_new_session"], True)


class OsInfo(unittest.TestCase):
    def test_shell_argv(self):
        argv = osinfo.shell_argv("echo hi")
        if osinfo.IS_WIN:
            self.assertIn("powershell", argv[0].lower())
        else:
            self.assertEqual(argv[1:], ["-lc", "echo hi"])

    def test_name_and_kind(self):
        self.assertTrue(osinfo.name())
        self.assertIn(osinfo.kind(), ("windows", "macos", "linux"))

    def test_windows_only_tools_hidden_elsewhere(self):
        names = {x["function"]["name"] for x in tools.schemas(config.load_settings())}
        if not osinfo.IS_WIN:
            self.assertNotIn("app_click", names)
            self.assertNotIn("list_windows", names)
        self.assertNotIn("mod_check", names)                  # only inside a Mod Aero chat


if __name__ == "__main__":
    unittest.main()
