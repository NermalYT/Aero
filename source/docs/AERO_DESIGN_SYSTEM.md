# Aero design system

Aero 1.0. The look is Frutiger Aero as it shipped on desktops from about 2004 to 2012: Windows Vista and 7 glass,
Mac OS X Aqua gel, Wii and Zune era nature imagery. One physical world under two lights. The app sits in tinted,
blurred glass frames over a landscape with a frog pond; text lives on near-opaque "client" surfaces inside the
frames, like a Windows 7 app, so it always reads cleanly over the scenery.

Files: `aero/static/app.css` (tokens, materials, every component), `aero/static/scene.css` and
`aero/static/scene.js` (pond, frogs, bubbles, fireflies, the scene governor), `brand/` (icon masters).

## 1. Principles

1. **Physical materials.** Glass refracts (backdrop blur and saturation), gel is lit from above (a bright cap on the
   top half), selection is a soft blue Windows 7 highlight, meters are glossy capsules.
2. **Text never sits on scenery.** Frames are glass; content panes are client surfaces at 84 to 96% opacity.
3. **Day and night are the same place.** Night is the same hills and pond by moonlight, not a different theme.
4. **The scenery serves the app.** It never covers a control, it pauses while the model works, and it can be
   turned down to Still or Off, or have its glass blur turned off.
5. **Frutiger Aero is the look and the speed, not the wording.** No slogans, mood lines or filler copy anywhere in the
   UI: every string says what something is, what it is doing, or what a button does.
6. **Control is always visible.** While a model drives input, the "is controlling" header (a blue gel bar across the
   top, 34 px, blinking red dot, red gel Stop pill) sits above everything, including modals, and the same banner
   sits over the controlled app.

## 2. Tokens

Shared (both lights): `--font` Segoe UI first (Frutiger, Myriad, Calibri as fallbacks), `--mono` Cascadia Code;
radii `--r` 12px, `--r-sm` 7px, `--r-xs` 4px, `--r-frame` 14px, `--r-pill`; spacing `--sp-1..5` (4, 8, 12, 16,
24px); control heights `--ctl-h` 30px and `--ctl-h-sm` 26px; motion `--t-fast` .12s, `--t-med` .22s, `--ease`;
blur `--blur-frame` 14px, `--blur-pop` 22px; `--streak` (the diagonal Aero reflections on every frame),
`--gel-cap` and `--gel-glow` (gel buttons).

Per light (`:root[data-theme="day"|"night"]`):

| Group | Tokens | Day | Night |
|---|---|---|---|
| Sky | `--sky-a..d` | #0a6fd6 to #d8f3ff azure | #020816 to #17688a midnight |
| Hills | `--h1a..h3c` | spring greens #a6e27e to #1f8a2b | moonlit #12513f to #052616 |
| Glass frame | `--frame`, `--frame-hi`, `--frame-out`, `--frame-in` | rgba(190,228,255,.40) | rgba(16,44,86,.52) |
| Client surface | `--client`, `--client-2`, `--client-line` | white .84 / .95 | navy rgba(7,22,46,.86) |
| Text | `--text`, `--muted`, `--faint`, `--heading` | #08223f, #3a5a7c, #56738f, #1a4d8f | #eaf6ff, #a9cbe6, #87acc9, #a9dcff |
| Accent | `--accent`, `--accent2` | #1d7ff0, #12b3ff | #2389f2, #2ccdf5 |
| Selection | `--sel-a/b/line`, `--hov-a/b/line` | Win7 #eaf5ff to #c6e2fc, line #7aa8dc | #1f548e to #153d6c |
| Buttons | `--btn-a..d`, `--btn-line`, `--btn-ink` | white to #d0d9e3 | #2d5281 to #132b4b |
| State | `--ok`, `--warn`, `--bad` | #0e9f60, #c87a10, #d93b52 | #2fe0a8, #ffbe55, #ff7a8f |
| Lanes | `--router`, `--local`, `--fable`, `--opus` | aqua, azure, violet, gold | brighter versions |
| Focus | `--focus` | white ring + 3.5px azure | dark ring + azure |

Pond tokens live in `scene.css`: `--frog`, `--frog-ink`, `--frog-mouth`, `--frog-tongue`, `--frog-lid` (0 by day,
.58 at night: sleepy lids), `--pad*`, `--lotus*`, `--stone*`, `--reed*`, water `--w0..w3`, `--moonpath` (0 by day).

## 3. Materials

- **Glass frame** (`.glass`): `--frame` tint, `backdrop-filter: blur(var(--blur-frame)) saturate(1.6)`, a 1px
  outer line (`--frame-out`), a 1px inner highlight (`--frame-in`), and `--streak` reflections.
- **Client surface**: `--client` with a hairline `--client-line`; this is where messages, settings and tables sit.
- **Gel button** (`.btn`, the New chat button, the send button): accent gradient, `--gel-cap` highlight on the top
  half, `--gel-glow` underneath, darker rim. Pressed state drops the cap.
- **Standard button** (`.btn.ghost`): the Windows 7 grey-white push button (`--btn-a..d`), blue hover.
- **Selection and hover**: Windows 7 list highlight, two-stop gradient with a 1px line.
- **Meters**: glossy capsules (`--meter-ok/ok2/warn/bad`), the RAM and VRAM bars.
- **Scrollbars**: Aqua capsule thumb in the accent colour.
- **Tooltips, menus, dialogs**: `--tip-*` and `--glass-*` surfaces with `--blur-pop`.

**Enable transparency** (Settings > Appearance, on by default; the Windows 7 name). Off: `body.no-transparency`
removes every backdrop blur and gives the frames a solid tint (#b4d5f0 by day, #14304f by night). The look stays
Aero; only the see-through blur goes. Measured in the development container: median frame time with the scenery
on Full dropped from about 100 ms to 33 ms with transparency off (software rendering; see the performance report).

## 4. The world

**Day.** Azure sky gradient, a sun with soft rays, glossy white clouds, light ribbons, three layers of rounded green
hills with white crests, white daisies, rising soap bubbles with iridescent rims, and the pond along the bottom.

**Night.** The same layout: midnight sky with twinkling stars and a moon, dark teal hills, the light ribbons turned green like a faint aurora,
fireflies blinking over the water, a moon path on the pond, dimmer bubbles. Frogs turn darker and get half-closed
sleepy lids.

## 5. The pond and frogs

Original art drawn in `scene.js` as layered SVG, inspired by the character qualities of a reference
image (an open-mouth green frog): round moss-green body, thick dark-olive outline, two eye bumps on top, stubby
front legs, a simple happy closed mouth. The source photo itself is never shown.

- **Outline-then-fill**: each body part is drawn once as a fat dark stroke, then again as a flat fill on top, so the
  body, eye bumps and legs merge into one silhouette with one clean outline.
- **Three poses**: `frogSit` (sitting on a lily pad, eyes open), `frogPeek` (head and eyes above the water with
  ripple rings and a reflection), and `frogSit({eyes: 'happy'})` (resting on a stone with happy closed eyes).
- **Closed mouth by default.** Every so often (14 to 44 s by day, 30 to 80 s at night) one visible frog **meeps**:
  the mouth opens wide (dark mouth, pink tongue) for about a second, with a small squash. Clicking a frog makes it
  meep. Meeps only happen while the scenery is animating.
- **Pond composition** (`PLACES` in `scene.js`): reeds and cattails on both banks, three lily pads, a lotus, a
  stone, three frogs, glints and a sky reflection on the water, ripple rings.
- **Never over controls.** The pond is a strip at the bottom (`--pond-h`). The app's frames end above it
  (`--pond-pad`), so no frog, pad or reed can sit on a button or text. The pond is `aria-hidden` and does not take
  focus.
- **Adapts to the window**: below 1100px wide the small pad and lotus go; below 860px the stone, the resting frog and
  the right reeds go; below 680px tall the pond is removed so the app keeps its height. Settings > Appearance has a
  "Frog pond" switch.

## 6. The bubble icon

The app icon is an iridescent soap-glass bubble, never a frog. Masters (editable SVG, original art):

- `brand/aero-bubble.svg` (512 viewBox): soft shadow, clear blue-cyan body, aqua caustic, a wave inside,
  iridescent rim (pink, violet, sky, mint, butter), top gloss cap, upper-left specular crescent, a second reflection.
- `brand/aero-bubble-small.svg`: the same bubble simplified for 16 to 32px (fewer layers, a thicker rim, a
  bigger highlight) so it stays a bubble at taskbar size.

`brand/render_icons.py <brand dir>` renders both with Chromium to `brand/png/aero-{16..1024}.png` (16, 20, 24,
32, 40, 48, 64, 96, 128, 180, 192, 256, 512, 1024), builds `brand/aero.ico` with the 16 to 256 sizes, and
writes `brand/png/preview-sizes.png`.

Shipped copies are in `aero/static/icons/`: the two SVGs (favicon and in-app brand mark), PNGs 16/32/48/180/192/
256/512 (favicons, apple-touch, the web manifest), and `aero-bubble.ico`. The installer copies that ICO to
`C:\Aero\aero-bubble.ico` and points the Desktop and Start Menu shortcuts at it. The file name changed from
`aero.ico` on purpose: Windows caches shortcut icons by path, so a new path shows the new icon without clearing
the icon cache. The Edge app window takes its title-bar and taskbar icon from the page's favicon and manifest.

## 7. Motion budget

| Mode | What moves | Cost |
|---|---|---|
| Full | Bubbles rise, clouds drift, light ribbons and sun rays shimmer, fireflies blink at night, frogs breathe and meep, ripples | CSS animations on transforms and opacity, plus the backdrop blur re-sampling moving scenery |
| Still | Nothing. The full scene is drawn once | One paint; no frames while idle |
| Off | Only the sky gradient. Pond, bubbles, hills and fireflies are removed | Lowest |

The **scene governor** (`Scene` in `scene.js`) pauses every animation (`animation-play-state: paused`, so nothing
jumps when it resumes) when any of these hold:

- the window is hidden or minimized (`visibilitychange`);
- a model is generating, tuning, benchmarking or loading, if "Pause the scenery while busy" is on (default on);
- the OS asks for reduced motion (`prefers-reduced-motion`), which also turns off UI transitions.

Scenery defaults to Full; Settings > Appearance offers Full / Still / Off, the frog pond switch, the pause-while-busy
switch and Enable transparency.

## 8. Accessibility and scaling

- Body text 14.5px Segoe UI on client surfaces; contrast is set by the client tokens, not by the scenery behind.
  Headings on glass get `--text-glow` for legibility.
- Every interactive element has a visible `--focus` ring (`:focus-visible`), and the composer's own focus style is
  kept so the ring is not doubled.
- The scenery is `aria-hidden`; frogs are clickable for fun but are not in the tab order.
- Layout: the dashboard column floats over the chat below 1180px and stacks below 860px; the pond trims itself as in
  section 5. Screenshots at 1080p, 1440p and 4K, and at 125%, 150% and 200% display scaling, are in
  `docs/VALIDATION_REPORT.md`.
