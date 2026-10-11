"""Router benchmark on 50 representative requests: decision time and whether the tools it picks can do the job.

    set AERO_HOME=C:\\Users\\you\\AeroTest\\home      (throwaway)
    set AERO_LLAMA_DIR=C:\\Aero\\llama                (an existing llama.cpp install, used read-only)
    python validation\\bench_router_intents.py <router.gguf> --tree <path to an Aero source/ folder> --out run.json
    python validation\\bench_router_intents.py --score run_a.json run_b.json

The first form runs one Aero version's router (its own rules, schema and tool catalog, from --tree) on the CPU,
exactly as Aero starts it, and records every decision with its time. The second form scores saved runs with one
fixed rule set: a request counts as routed well when the exposed tools (the router's pick plus the companions and
the minimum Aero adds) cover every capability the request needs. Capabilities come from this version's
capabilities.py, applied the same way to every run, so versions with different tool names are compared fairly.
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

# (request, required capability groups: each group is satisfied by any one of its capabilities)
PROMPTS = [
    ("Open up my Bloxstrap, find me a fun Roblox game, and launch it.", [["app.launch"], ["browser.search", "browser.read"]]),
    ("Check my Gmail for meetings this week and make me a document with everything I need.",
     [["email.read", "browser.navigate", "mcp.call"], ["document.create"]]),
    ("Open Discord, find my conversation with Alex, and summarize the important points.", [["app.launch", "app.inspect"], ["app.read"]]),
    ("Open my most recent Python project in VS Code and fix the failing tests.", [["filesystem.search"], ["terminal.execute"], ["filesystem.modify"]]),
    ("Open my budget spreadsheet and update the totals.", [["filesystem.search", "filesystem.read"], ["filesystem.modify", "spreadsheet.edit"]]),
    ("Play my workout playlist on Spotify.", [["app.launch", "app.interact"]]),
    ("Read all the pages on this pricing site and compare the plans: https://example.com/pricing", [["browser.read", "browser.navigate"]]),
    ("Use the browser to finish the signup form on example.com.", [["browser.interact"]]),
    ("What's 17 times 23?", []),
    ("hi", []),
    ("Rename every .jpeg in my Downloads folder to .jpg.", [["filesystem.search"], ["filesystem.modify"]]),
    ("Find the TODO comments in my repo and list them.", [["filesystem.search"]]),
    ("Install the requests package with pip.", [["terminal.execute"]]),
    ("Take a screenshot and tell me what's on my screen.", [["screen.observe"]]),
    ("Click the Save button in Notepad.", [["app.interact"]]),
    ("Type 'meeting notes' into the open Notepad window.", [["app.interact"]]),
    ("What windows do I have open right now?", [["app.inspect"]]),
    ("Search the web for the latest llama.cpp release notes.", [["browser.search"]]),
    ("Summarize this article: https://en.wikipedia.org/wiki/Frog", [["browser.read"]]),
    ("Log in to my bank's website and check my balance.", [["browser.interact", "browser.navigate"]]),
    ("Remember that my favorite color is green.", [["memory.learn"]]),
    ("What did we talk about yesterday about the router?", [["memory.recall"]]),
    ("Write a Python script that prints the first 20 primes and save it to primes.py.", [["filesystem.modify"]]),
    ("Edit config.yaml and change the port to 8080.", [["filesystem.read"], ["filesystem.modify"]]),
    ("Delete the temp folder on my desktop.", [["filesystem.modify"]]),
    ("Launch Steam and start Hades.", [["app.launch"]]),
    ("Open Calculator and add 1234 and 5678.", [["app.launch"], ["app.interact"]]),
    ("Open File Explorer to my Documents folder.", [["app.launch"]]),
    ("Check what's using port 8180.", [["terminal.execute"]]),
    ("Read the PDF on my desktop called report.pdf and summarize it.", [["filesystem.read", "document.read"]]),
    ("Make a Word document listing my meetings for next week from my email.", [["email.read", "browser.navigate", "mcp.call"], ["document.create"]]),
    ("Open Outlook and read my newest email.", [["app.launch", "app.inspect", "email.read"], ["app.read", "email.read"]]),
    ("Send a message to Sam on Slack saying I'm running late.", [["app.interact", "browser.interact", "mcp.call"]]),
    ("Open the GitHub issues for NermalYT/Aero and list the open ones.", [["mcp.call", "browser.read", "browser.navigate"]]),
    ("Scroll down in the browser and read the rest of the page.", [["browser.read"]]),
    ("Go back to the previous page in the browser.", [["browser.navigate"]]),
    ("Fill in the 'Name' field on the form in my browser with Pat Lee.", [["browser.interact"]]),
    ("Download the latest Aero release notes and save them to notes.md.", [["browser.read"], ["filesystem.modify"]]),
    ("Compare the specs of the RTX 5080 and RX 9070 XT from their official pages.", [["browser.search", "browser.read"]]),
    ("Open VS Code.", [["app.launch"]]),
    ("Start Minecraft.", [["app.launch"]]),
    ("Open settings and turn on dark mode.", [["app.launch", "app.interact"]]),
    ("Which account should you use to check my email? Just check the work one.", [["email.read", "browser.navigate", "mcp.call"]]),
    ("Run the tests in this project and tell me which fail.", [["terminal.execute"]]),
    ("Look at the app window I selected and click Next.", [["app.interact"]]),
    ("Read the text in the open Word document.", [["app.read", "document.read"]]),
    ("Find all photos larger than 10 MB on my D drive.", [["filesystem.search", "terminal.execute"]]),
    ("Translate 'good morning' into Japanese.", []),
    ("Explain how a CPU cache works.", []),
    ("Organize my desktop into folders by file type.", [["filesystem.search"], ["filesystem.modify"]]),
]

# Written after the first run and never used to tune anything: an out-of-sample check of the capability hints.
HELDOUT = [
    ("Open Notepad and write a shopping list: eggs, milk, bread.", [["app.launch"], ["app.interact", "filesystem.modify"]]),
    ("Look up the weather in State College tomorrow.", [["browser.search"]]),
    ("Save a note that my dentist appointment is on Friday.", [["memory.learn", "filesystem.modify"]]),
    ("Show me the files in my Pictures folder.", [["filesystem.search"]]),
    ("Go to github.com and open my notifications.", [["browser.navigate"]]),
    ("Launch OBS.", [["app.launch"]]),
    ("Close the dialog in the app I selected.", [["app.interact"]]),
    ("What's the capital of Australia?", []),
    ("Copy all .txt files from Downloads into a new folder called Text.", [["filesystem.modify", "terminal.execute"]]),
    ("Find a good pizza place near me.", [["browser.search"]]),
    ("Read my latest emails and tell me what's urgent.", [["email.read", "browser.navigate", "mcp.call"]]),
    ("In the open browser tab, click the Accept button.", [["browser.interact"]]),
    ("Run git status in my project folder.", [["terminal.execute"]]),
    ("Open Word and start a new document.", [["app.launch"]]),
    ("Write a haiku about frogs.", []),
    ("Check the CPU temperature.", [["terminal.execute", "screen.observe"]]),
    ("Search my documents for the word invoice.", [["filesystem.search"]]),
    ("Turn the volume down.", [["app.interact", "terminal.execute", "screen.physical_input"]]),
    ("What did I tell you about my GPU last week?", [["memory.recall"]]),
    ("Open YouTube and play lo-fi music.", [["browser.navigate", "app.launch"]]),
    ("Put my meetings from my inbox into a document.", [["email.read", "browser.navigate", "mcp.call"], ["document.create"]]),
    ("Make a folder on my desktop called Projects.", [["filesystem.modify", "terminal.execute"]]),
    ("Open Telegram and read the latest message from Mom.", [["app.launch", "app.inspect"], ["app.read"]]),
    ("Fill out the contact form on example.org with my name.", [["browser.interact"]]),
    ("Start Epic Games and launch Fortnite.", [["app.launch"]]),
]


def run(model, tree, out, threads, prompts=None):
    sys.path.insert(0, str(Path(tree).resolve()))
    from aero import config, pipeline, router, tools
    from aero.engine import LlamaServer
    tools.load_all()
    s = config.load_settings()
    s["local_only"] = False
    catalog = pipeline.tool_catalog(s)
    srv = LlamaServer(config.ROUTER_PORT + 30, "router-bench")
    args = router.server_args(model, threads, router.physical_cores())
    args[args.index("--port") + 1] = str(srv.port)          # its own port, so a running Aero is not disturbed
    srv.start(args, {"CUDA_VISIBLE_DEVICES": "-1", "GGML_VK_VISIBLE_DEVICES": ""})
    ok, why = srv.wait_ready(180)
    if not ok:
        raise SystemExit("router server: " + why + srv.log_tail(10))
    router._state.update(server=srv, ready=True, model=Path(model).stem, threads=threads)
    router.decide([{"role": "user", "content": "hi"}], catalog, [])            # warm the prompt cache
    rows = []
    for text, need in prompts or PROMPTS:
        t0 = time.time()
        try:
            d, info = router.decide([{"role": "user", "content": text}], catalog, [])
            err = None
        except Exception as e:  # noqa: BLE001
            d, info, err = {}, {}, f"{type(e).__name__}: {e}"
        ms = round((time.time() - t0) * 1000)
        import inspect
        kw = {"text": text} if "text" in inspect.signature(pipeline.choose_exposure).parameters else {}
        exposed = pipeline.choose_exposure(d, s, None, [c[0] for c in catalog], **kw) if d else []
        apps_line = ""
        if hasattr(pipeline, "app_context") and d:          # v1.1: deterministic app hints added at turn time
            turn = type("T", (), {"exposed": list(exposed), "settings": s})()
            apps_line = pipeline.app_context(turn, text)
            exposed = turn.exposed
        rows.append({"text": text, "need": need, "ms": ms, "router_ms": info.get("ms"), "prompt_n": info.get("prompt_n"),
                     "cached_n": info.get("cached_n"), "tools": d.get("tools"), "exposed": exposed,
                     "complexity": d.get("complexity"), "error": err, "apps": apps_line})
        print(f"{ms:>6} ms  {len(exposed):>2} tools  {text[:70]}", flush=True)
    srv.stop()
    sizes = {"catalog_tools": len(catalog), "rules_chars": len(router.RULES),
             "catalog_chars": len(router.catalog(catalog, [])),
             "schema_chars": len(json.dumps(router.schema([c[0] for c in catalog])))}
    res = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "tree": str(tree), "model": Path(model).name, "threads": threads,
           "sizes": sizes, "rows": rows}
    Path(out).write_text(json.dumps(res, indent=1), encoding="utf-8")


def score(files):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from aero import capabilities
    for f in files:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        ok, ms, n_tools = 0, [], []
        misses = []
        for r in d["rows"]:
            caps = {c for t in r["exposed"] or [] for c in capabilities.caps_of(t).capabilities}
            good = all(any(c in caps for c in group) for group in r["need"]) and not r["error"]
            ok += good
            if not good:
                misses.append(r["text"][:60])
            ms.append(r["ms"])
            n_tools.append(len(r["exposed"] or []))
        ms.sort()
        print(f"\n{f}: {d['model']} · {d['threads']} threads · catalog {d['sizes']['catalog_tools']} tools, "
              f"{d['sizes']['catalog_chars'] + d['sizes']['rules_chars']:,} prompt chars, schema "
              f"{d['sizes']['schema_chars']:,} chars")
        print(f"  routed well: {ok}/{len(d['rows'])}  ·  decision p50 {statistics.median(ms):.0f} ms, "
              f"p95 {ms[int(len(ms) * 0.95) - 1]:.0f} ms, max {ms[-1]} ms  ·  tools exposed median "
              f"{statistics.median(n_tools):.0f}")
        for m in misses:
            print("  missed:", m)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("model", nargs="?")
    ap.add_argument("--tree")
    ap.add_argument("--out")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--score", nargs="*")
    ap.add_argument("--heldout", action="store_true", help="the 25 held-out requests instead of the 50")
    a = ap.parse_args()
    if a.score:
        score(a.score)
    else:
        run(a.model, a.tree, a.out, a.threads, HELDOUT if a.heldout else PROMPTS)
