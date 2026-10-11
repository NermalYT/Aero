---
name: roblox-launch
description: Find a Roblox experience the user would like and start it through their installed launcher (Roblox or Bloxstrap) with Roblox's documented deep link, then check what actually started.
version: 1.1.0
apps: roblox, bloxstrap
capabilities: app.discover, app.launch, browser.search
backends: protocol
os: windows, macos
---

# Roblox through the user's launcher

Use for "open my Bloxstrap, find me a fun Roblox game and launch it" and similar.

## 1. Which program is which
- `app_find("bloxstrap")` and `app_find("roblox")`. Bloxstrap is a bootstrapper, not Roblox: when installed it
  registers itself for `roblox://` and `roblox-player:` links, and app_find says "opens through Bloxstrap.exe".
- If neither is installed, say so and stop. Don't download anything.

## 2. What the user likes
- Use only what memory and this chat actually say (`recall("roblox games I like")`). If nothing is known and the
  choice matters, ask one short question with `ask_user` (for example genres: obby, tycoon, horror, simulator) and
  keep searching popular options meanwhile.
- Never invent preferences.

## 3. Find candidates (needs the internet)
- With Local Only or strict offline on, say you can't search Roblox right now and offer to launch Roblox's home
  screen instead.
- Otherwise `web_search` for current popular experiences in the liked genre, open the experience pages
  (`fetch_url` or the browser) and read: the place id in the URL (`roblox.com/games/<placeId>/...`), the age
  guidance, the genre and the player count. Drop anything that doesn't fit the user or looks age-inappropriate.
- Don't scrape protected Roblox APIs, don't use account tokens, never sign in for the user.

## 4. Launch
- Exactly one choice, or the user asked you to pick: `app_launch("roblox", uri="roblox://experiences/start?placeId=<id>")`.
  The link goes to whatever is registered (Bloxstrap when installed), which respects the user's launcher.
- Several equally good options and a wrong launch would annoy: show two or three with one line each and ask.

## 5. Say what really happened
- The result separates "the launcher started" (a new Bloxstrap or RobloxPlayerBeta process) from "the experience
  loaded" (which needs a look at the Roblox window: `app_list`, then the window title). Report only what you saw.
- Never touch the game itself (anti-cheat): no input, no memory reading, no injection.
- After a verified start, Aero remembers the launch method on this computer; nothing about the user's account.
