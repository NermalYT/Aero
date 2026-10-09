"""Aero's built-in knowledge of well-known apps and services: names people use for them, the processes and programs
that belong to them, the link schemes they register, and which kind of tools fit them.

This is a starting point for matching, not a promise: app_registry.py checks what is really installed on this
computer before anything is launched, and users can add, fix or switch off entries (data/apps.json). Nothing here
holds a path, an account or a private endpoint.

Fields: name, aliases (lower case; "Case" ones only match with that exact capitalisation, for names that are also
ordinary words), exe (program file names), procs (process names when they differ), protocols (link schemes the app
handles), web (the web address of a service with no local app needed), related (other catalog ids), caps
(capabilities from capabilities.py), backends (best ways to work with it, best first), notes.
"""

CATALOG = {
    # ---- browsers
    "chrome": {"name": "Google Chrome", "aliases": ["chrome", "google chrome"], "exe": ["chrome.exe", "google-chrome",
               "google-chrome-stable"], "caps": ["browser.navigate"], "backends": ["browser"]},
    "msedge": {"name": "Microsoft Edge", "aliases": ["edge", "microsoft edge", "msedge"],
               "exe": ["msedge.exe", "microsoft-edge", "microsoft-edge-stable"], "caps": ["browser.navigate"],
               "backends": ["browser"]},
    "firefox": {"name": "Mozilla Firefox", "aliases": ["firefox", "mozilla firefox"], "exe": ["firefox.exe", "firefox"],
                "caps": ["browser.navigate"], "backends": ["browser"]},
    "brave": {"name": "Brave", "aliases": ["brave", "brave browser"], "exe": ["brave.exe", "brave-browser"],
              "caps": ["browser.navigate"], "backends": ["browser"]},
    "opera": {"name": "Opera", "aliases": ["opera", "opera gx"], "exe": ["opera.exe", "launcher.exe"],
              "caps": ["browser.navigate"], "backends": ["browser"]},
    # ---- web services (no local app needed)
    "gmail": {"name": "Gmail", "aliases": ["gmail", "google mail"], "web": "https://mail.google.com/",
              "caps": ["email.search", "email.read", "email.draft"], "backends": ["mcp", "browser"],
              "notes": "Use a connected Gmail MCP server when there is one; otherwise Aero's browser after the user "
                       "signs in there. Ask which account when several are possible."},
    "google_calendar": {"name": "Google Calendar", "aliases": ["google calendar", "gcal"],
                        "web": "https://calendar.google.com/", "caps": ["calendar.read", "calendar.write"],
                        "backends": ["mcp", "browser"]},
    "google_docs": {"name": "Google Docs", "aliases": ["google docs", "gdocs"], "web": "https://docs.google.com/",
                    "caps": ["document.create", "document.edit"], "backends": ["mcp", "browser"]},
    "google_sheets": {"name": "Google Sheets", "aliases": ["google sheets"], "web": "https://sheets.google.com/",
                      "caps": ["spreadsheet.read", "spreadsheet.edit"], "backends": ["mcp", "browser"]},
    "github": {"name": "GitHub", "aliases": ["github"], "web": "https://github.com/", "caps": ["mcp.call"],
               "backends": ["mcp", "browser"]},
    "youtube": {"name": "YouTube", "aliases": ["youtube"], "web": "https://www.youtube.com/",
                "caps": ["browser.read"], "backends": ["browser"]},
    # ---- communication
    "discord": {"name": "Discord", "aliases": ["discord", "discord ptb", "discord canary"],
                "exe": ["Discord.exe", "DiscordPTB.exe", "DiscordCanary.exe", "discord"], "protocols": ["discord"],
                "caps": ["app.read", "app.interact"], "backends": ["accessibility", "browser"],
                "notes": "Electron app: window messages are often ignored; reading works through UI Automation. "
                         "Sending a message needs the user's clear request."},
    "slack": {"name": "Slack", "aliases": ["slack"], "exe": ["slack.exe", "slack"], "protocols": ["slack"],
              "caps": ["app.read", "app.interact"], "backends": ["mcp", "accessibility", "browser"]},
    "teams": {"name": "Microsoft Teams", "aliases": ["microsoft teams", "ms teams", "Teams"],
              "exe": ["ms-teams.exe", "Teams.exe"], "protocols": ["msteams"], "caps": ["app.read", "app.interact"],
              "backends": ["accessibility"]},
    "zoom": {"name": "Zoom", "aliases": ["zoom", "zoom workplace"], "exe": ["Zoom.exe", "zoom"],
             "protocols": ["zoommtg"], "caps": ["app.launch"], "backends": ["accessibility"]},
    "outlook": {"name": "Outlook", "aliases": ["outlook", "microsoft outlook"], "exe": ["OUTLOOK.EXE", "olk.exe"],
                "caps": ["email.read", "calendar.read"], "backends": ["accessibility", "browser"]},
    "telegram": {"name": "Telegram", "aliases": ["telegram"], "exe": ["Telegram.exe", "telegram-desktop"],
                 "protocols": ["tg"], "caps": ["app.read", "app.interact"], "backends": ["accessibility"]},
    "whatsapp": {"name": "WhatsApp", "aliases": ["whatsapp"], "exe": ["WhatsApp.exe"], "protocols": ["whatsapp"],
                 "caps": ["app.read", "app.interact"], "backends": ["accessibility"]},
    # ---- work and code
    "vscode": {"name": "Visual Studio Code", "aliases": ["vs code", "vscode", "visual studio code", "code editor"],
               "exe": ["Code.exe", "code"], "protocols": ["vscode"],
               "caps": ["document.edit", "filesystem.modify", "terminal.execute"], "backends": ["file", "terminal",
                                                                                                "accessibility"],
               "notes": "Edit files and run tests directly; open a folder with `code <folder>`. Recent folders are in "
                        "the user's VS Code storage (storage.json / state.vscdb)."},
    "notepad": {"name": "Notepad", "aliases": ["notepad"], "exe": ["notepad.exe"],
                "caps": ["document.edit", "app.interact"], "backends": ["file", "accessibility"]},
    "notepadpp": {"name": "Notepad++", "aliases": ["notepad++", "notepad plus plus"], "exe": ["notepad++.exe"],
                  "caps": ["document.edit"], "backends": ["file", "accessibility"]},
    "word": {"name": "Microsoft Word", "aliases": ["microsoft word", "ms word", "Word"], "exe": ["WINWORD.EXE"],
             "caps": ["document.create", "document.edit"], "backends": ["file", "accessibility"],
             "notes": "Edit .docx files directly (python-docx) and keep a copy of the original."},
    "excel": {"name": "Microsoft Excel", "aliases": ["microsoft excel", "ms excel", "Excel"], "exe": ["EXCEL.EXE"],
              "caps": ["spreadsheet.read", "spreadsheet.edit"], "backends": ["file", "accessibility"],
              "notes": "Edit .xlsx directly (openpyxl keeps formulas but does not recalculate them)."},
    "powerpoint": {"name": "Microsoft PowerPoint", "aliases": ["powerpoint", "microsoft powerpoint"],
                   "exe": ["POWERPNT.EXE"], "caps": ["document.create", "document.edit"],
                   "backends": ["file", "accessibility"]},
    "libreoffice": {"name": "LibreOffice", "aliases": ["libreoffice", "libre office", "writer", "calc"],
                    "exe": ["soffice.exe", "soffice", "libreoffice"], "caps": ["document.edit", "spreadsheet.edit"],
                    "backends": ["file", "accessibility"]},
    "obsidian": {"name": "Obsidian", "aliases": ["obsidian"], "exe": ["Obsidian.exe", "obsidian"],
                 "protocols": ["obsidian"], "caps": ["document.edit"], "backends": ["file"]},
    "terminal": {"name": "Windows Terminal", "aliases": ["windows terminal", "terminal"], "exe": ["wt.exe"],
                 "caps": ["terminal.execute"], "backends": ["terminal"]},
    "explorer": {"name": "File Explorer", "aliases": ["file explorer", "explorer", "files", "finder"],
                 "exe": ["explorer.exe"], "caps": ["filesystem.search"], "backends": ["file"]},
    "calculator": {"name": "Calculator", "aliases": ["calculator", "calc app"], "exe": ["CalculatorApp.exe",
                   "calc.exe", "gnome-calculator"], "caps": ["app.interact"], "backends": ["accessibility"]},
    "settings": {"name": "Windows Settings", "aliases": ["windows settings", "settings app"],
                 "protocols": ["ms-settings"], "caps": ["app.interact"], "backends": ["accessibility"]},
    # ---- media and games
    "spotify": {"name": "Spotify", "aliases": ["spotify"], "exe": ["Spotify.exe", "spotify"], "protocols": ["spotify"],
                "caps": ["app.interact"], "backends": ["media_session", "accessibility"],
                "notes": "Play/pause/next go through the system's media keys session; searching needs the app UI."},
    "vlc": {"name": "VLC media player", "aliases": ["vlc"], "exe": ["vlc.exe", "vlc"], "caps": ["app.interact"],
            "backends": ["accessibility"]},
    "obs": {"name": "OBS Studio", "aliases": ["obs", "obs studio"], "exe": ["obs64.exe", "obs"],
            "caps": ["app.interact"], "backends": ["accessibility"]},
    "steam": {"name": "Steam", "aliases": ["steam"], "exe": ["steam.exe", "steam"], "protocols": ["steam"],
              "caps": ["app.launch"], "backends": ["protocol"],
              "notes": "Start games with steam://rungameid/<appid>; anti-cheat protected games are not controlled."},
    "epic": {"name": "Epic Games Launcher", "aliases": ["epic games", "epic games launcher", "epic launcher"],
             "exe": ["EpicGamesLauncher.exe"], "protocols": ["com.epicgames.launcher"], "caps": ["app.launch"],
             "backends": ["protocol"]},
    "battlenet": {"name": "Battle.net", "aliases": ["battle.net", "battlenet"], "exe": ["Battle.net.exe"],
                  "protocols": ["battlenet"], "caps": ["app.launch"], "backends": ["protocol"]},
    "riot": {"name": "Riot Client", "aliases": ["riot client", "riot games", "league of legends", "valorant"],
             "exe": ["RiotClientServices.exe"], "caps": ["app.launch"], "backends": ["protocol"],
             "notes": "Vanguard anti-cheat: Aero only launches; it never controls the game."},
    "roblox": {"name": "Roblox", "aliases": ["roblox", "roblox player"],
               "exe": ["RobloxPlayerBeta.exe", "RobloxPlayerLauncher.exe"], "procs": ["RobloxPlayerBeta.exe"],
               "protocols": ["roblox", "roblox-player"], "related": ["bloxstrap"], "caps": ["app.launch"],
               "backends": ["protocol"],
               "uris": {"experience": "roblox://experiences/start?placeId={place_id}"},
               "notes": "Open an experience with its documented deep link (roblox://experiences/start?placeId=...). "
                        "Whatever program is registered for roblox:// links handles it (Bloxstrap when installed). "
                        "A started player process is not proof the experience loaded."},
    "bloxstrap": {"name": "Bloxstrap", "aliases": ["bloxstrap", "blox strap"], "exe": ["Bloxstrap.exe"],
                  "related": ["roblox"], "caps": ["app.launch"], "backends": ["protocol"],
                  "notes": "A Roblox bootstrapper, not Roblox itself: when installed it registers itself for roblox:// "
                           "and roblox-player: links, so Roblox deep links start through it."},
    "minecraft": {"name": "Minecraft Launcher", "aliases": ["minecraft", "minecraft launcher"],
                  "exe": ["MinecraftLauncher.exe", "Minecraft.exe"], "caps": ["app.launch"], "backends": ["protocol"]},
}

# words that are also app names but too common to treat as a mention unless capitalised
COMMON_WORDS = {"word", "excel", "teams", "steam", "files", "code", "terminal", "calc", "writer", "settings", "edge",
                "opera", "signal", "mail", "photos", "notes", "calendar", "zoom", "brave"}


def entry(app_id):
    e = CATALOG.get(app_id)
    return dict(e, id=app_id) if e else None


def by_exe(exe_name):
    """Catalog id for a program file name (case-insensitive), or None."""
    n = (exe_name or "").lower()
    for k, e in CATALOG.items():
        if n in (x.lower() for x in e.get("exe", []) + e.get("procs", [])):
            return k
    return None


def by_protocol(scheme):
    s = (scheme or "").lower().rstrip(":")
    return [k for k, e in CATALOG.items() if s in e.get("protocols", [])]
