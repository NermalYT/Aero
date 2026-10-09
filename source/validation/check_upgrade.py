"""Check that a data folder written by an older Aero opens in this one: a real server of the old version writes
settings, a chat and a memory fact into a throwaway AERO_HOME, then this version's server starts on the same folder
and is checked over its API. Nothing outside that folder is touched (no shortcuts, no install folder, no model).

    git worktree add ..\\aero-1.0 v1.0.0
    python validation\\check_upgrade.py ..\\aero-1.0\\source C:\\Users\\you\\AeroTest\\upgrade-home

The folder given last is deleted and recreated. Exit code 0 when every check passed.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

NEW = Path(__file__).resolve().parent.parent
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail != "" else ""))


def start(src, home, port):
    env = dict(os.environ, AERO_HOME=str(home), AERO_PORT=str(port), AERO_LLAMA_PORT=str(port + 10))
    log = open(home / f"server-{port}.log", "w")
    flags = 0x00000200 if os.name == "nt" else 0          # CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen([sys.executable, "-m", "aero", "--no-window", "--log-stdout"], cwd=src, env=env,
                         stdout=log, stderr=subprocess.STDOUT, creationflags=flags, start_new_session=os.name != "nt")
    url = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            if httpx.get(url + "/api/state", timeout=2).status_code == 200:
                return p, httpx.Client(base_url=url, timeout=30)
        except Exception:
            pass
        if p.poll() is not None:
            break
        time.sleep(0.5)
    stop(p)
    raise SystemExit(f"The server in {src} did not start; see {log.name}")


def stop(p):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
    else:
        p.terminate()
    p.wait(15)


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    old, home = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)

    p, c = start(old, home, 8290)
    try:
        old_settings = c.get("/api/state").json()["settings"]
        old_version = c.get("/api/state").json()["version"]
        c.put("/api/settings", json={"user_profile": "Upgrade test profile", "temperature": 0.55,
                                     "tool_policy": {**old_settings.get("tool_policy", {}), "desktop": "auto"}})
        c.put("/api/chats/upgchat1", json={"id": "upgchat1", "title": "Upgrade chat", "messages": [
            {"role": "user", "content": "hello from the old version"},
            {"role": "assistant", "content": "reply from the old version"}]})
        c.post("/api/memory", json={"text": "The upgrade tester prefers metric units.", "kind": "user"})
        before = c.get("/api/state").json()["settings"]
        check(f"{old_version} server wrote settings, a chat and a memory fact",
              c.get("/api/chats/upgchat1").json()["messages"][0]["content"] == "hello from the old version")
    finally:
        stop(p)

    p, c = start(NEW, home, 8292)
    try:
        state = c.get("/api/state").json()
        s = state["settings"]
        check("server reports the new version", state.get("version") != old_version, state.get("version"))
        check("an unedited old system prompt is replaced", s.get("system_prompt") != before.get("system_prompt"))
        check("user_profile kept", s.get("user_profile") == "Upgrade test profile", s.get("user_profile"))
        check("temperature kept", s.get("temperature") == 0.55, s.get("temperature"))
        check("desktop permission 'auto' kept", s.get("tool_policy", {}).get("desktop") == "auto")
        check("new tool category 'ask' defaults to auto", s.get("tool_policy", {}).get("ask") == "auto")
        check("strict_background defaults off", s.get("strict_background") is False)
        check("browser_mode has a default", bool(s.get("browser_mode")), s.get("browser_mode"))
        chat = c.get("/api/chats/upgchat1").json()
        check("chat kept", [m["content"] for m in chat["messages"]]
              == ["hello from the old version", "reply from the old version"])
        check("chat listed", any(x["id"] == "upgchat1" for x in c.get("/api/chats").json()))
        check("memory fact kept", "metric units" in json.dumps(c.get("/api/memory").json()))
        for path in ("/api/questions", "/api/apps", "/api/remote", "/api/resources", "/api/tasks/upgchat1",
                     "/api/stats"):
            r = c.get(path)
            check(f"GET {path}", r.status_code == 200, r.status_code)
    finally:
        stop(p)
    print(f"{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
