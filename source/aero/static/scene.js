/* Aero scenery: the frog pond, frogs, soap bubbles, fireflies, and the scene governor.

   Art: original frog characters drawn here as layered SVG. Round moss-green body, thick dark-olive outline,
   two eye bumps, stubby legs. Mouths are closed most of the time; now and then a frog "meeps" and opens its
   mouth wide (dark mouth, pink tongue). At night the frogs get sleepy lids and blink slowly.

   Cost: everything is SVG/CSS. Motion only runs in Scenery "Full", only while the window is visible, and is
   paused while a model is generating, tuning or benchmarking (body.busy) so inference keeps the GPU. "Still"
   keeps the composed scene with zero animation; "Off" removes it. */
'use strict';

const Scene = (() => {
  // ------------------------------------------------------------------ frog art
  // Outline-then-fill: every part is drawn once as a fat dark stroke, then again as a flat fill on top,
  // so overlapping parts (body, eye bumps, legs) merge into one clean silhouette with a single outline.
  const BODY = 'M28 104 C10 102 6 80 10 62 C14 40 30 22 52 17 C68 13 86 14 100 21 C118 30 128 48 129 68 C130 90 121 104 102 106 C82 108 48 107 28 104 Z';
  const BUMPS = [[56, 21, 11.5], [24, 53, 10.5]];
  const LEGS = 'M80 94 C80 103 81 110 86 111 C91 112 93 106 92 94 Z M103 92 C103 101 104 108 109 109 C114 110 116 104 115 92 Z';
  const HAUNCH = [31, 97, 15, 10];

  function frogSit({ eyes = 'open', id = '' } = {}) {
    const [hx, hy, hrx, hry] = HAUNCH;
    const parts = (cls) => `
      <path class="${cls}" d="${BODY}"/>
      ${BUMPS.map(([x, y, r]) => `<circle class="${cls}" cx="${x}" cy="${y}" r="${r}"/>`).join('')}
      <ellipse class="${cls}" cx="${hx}" cy="${hy}" rx="${hrx}" ry="${hry}"/>
      <path class="${cls}" d="${LEGS}"/>`;
    const eye = (x, y) => eyes === 'happy'
      ? `<path class="fr-lid-line" d="M${x - 4.2} ${y + 1.2} Q${x} ${y - 4} ${x + 4.2} ${y + 1.2}"/>`
      : `<g class="fr-eye"><ellipse class="fr-pupil" cx="${x}" cy="${y}" rx="4.4" ry="5.3"/>
           <circle class="fr-glint" cx="${x - 1.4}" cy="${y - 2.1}" r="1.3"/>
           <path class="fr-lid" d="M${x - 7} ${y - 7.5} H${x + 7} V${y + 0.5} Q${x} ${y + 3.5} ${x - 7} ${y + 0.5} Z"/></g>`;
    return `<svg class="frog-art sit" viewBox="0 0 140 120" aria-hidden="true"${id ? ` id="${id}"` : ''}>
      <ellipse class="fr-shadow" cx="70" cy="111" rx="56" ry="7"/>
      <g class="fr-body">
        <g class="fr-ink">${parts('o')}</g>
        <g class="fr-fill">${parts('f')}</g>
        <path class="fr-sheen" d="M50 32 C66 24 88 24 104 32"/>
        <path class="fr-toe" d="M86 111 V106 M109 109 V104"/>
        ${eye(57, 21)}${eye(24, 53)}
        <g class="fr-mouth-closed">
          <path class="fr-line" d="M46 66 C66 78 104 77 125 58"/>
          <path class="fr-line thin" d="M44 61 Q42 68 48 69"/>
        </g>
        <g class="fr-mouth-open">
          <clipPath id="${id || 'f'}-m"><ellipse cx="96" cy="56" rx="27" ry="33" transform="rotate(-14 96 56)"/></clipPath>
          <ellipse class="fr-mouth" cx="96" cy="56" rx="27" ry="33" transform="rotate(-14 96 56)"/>
          <g clip-path="url(#${id || 'f'}-m)">
            <ellipse class="fr-tongue-back" cx="96" cy="73" rx="27" ry="14"/>
            <ellipse class="fr-tongue" cx="99" cy="80" rx="27" ry="13"/>
          </g>
          <ellipse class="fr-mouth-rim" cx="96" cy="56" rx="27" ry="33" transform="rotate(-14 96 56)"/>
        </g>
      </g>
    </svg>`;
  }

  // a front-facing head peeking out of the water: two eye bumps and the top of the head
  function frogPeek({ id = '' } = {}) {
    const parts = (cls) => `
      <path class="${cls}" d="M14 52 C14 30 30 20 50 20 C70 20 86 30 86 52 Z"/>
      <circle class="${cls}" cx="31" cy="22" r="10"/><circle class="${cls}" cx="69" cy="22" r="10"/>`;
    const eye = (x, y) => `<g class="fr-eye"><ellipse class="fr-pupil" cx="${x}" cy="${y}" rx="4" ry="4.9"/>
      <circle class="fr-glint" cx="${x - 1.3}" cy="${y - 1.9}" r="1.2"/>
      <path class="fr-lid" d="M${x - 7} ${y - 7.5} H${x + 7} V${y + 0.5} Q${x} ${y + 3.5} ${x - 7} ${y + 0.5} Z"/></g>`;
    const cid = (id || 'p') + '-w';
    return `<svg class="frog-art peek" viewBox="0 0 100 66" aria-hidden="true"${id ? ` id="${id}"` : ''}>
      <clipPath id="${cid}"><rect x="0" y="0" width="100" height="53"/></clipPath>
      <g clip-path="url(#${cid})"><g class="fr-body">
        <g class="fr-ink">${parts('o')}</g>
        <g class="fr-fill">${parts('f')}</g>
        <path class="fr-sheen" d="M34 34 C44 30 58 30 68 34"/>
        ${eye(31, 23)}${eye(69, 23)}
        <path class="fr-line thin fr-mouth-closed" d="M38 44 Q50 50 62 44"/>
        <ellipse class="fr-mouth fr-mouth-open" cx="50" cy="44" rx="8" ry="6"/>
      </g></g>
      <ellipse class="fr-reflect" cx="50" cy="57" rx="30" ry="4"/>
      <ellipse class="fr-ring" cx="50" cy="53" rx="44" ry="5.5"/>
      <ellipse class="fr-ring r2" cx="50" cy="54" rx="33" ry="3.6"/>
    </svg>`;
  }

  function lilyPad(cls = '') {
    return `<svg class="pad-art ${cls}" viewBox="0 0 160 44" aria-hidden="true">
      <ellipse class="pad-shadow" cx="80" cy="30" rx="76" ry="12"/>
      <path class="pad-edge" d="M80 26 L144 12 C156 18 158 30 140 36 C118 43 42 43 20 36 C2 30 4 16 22 10 C40 4 64 4 80 26 Z"/>
      <path class="pad-top" d="M80 23 L140 10 C152 15 152 26 136 31 C116 37 44 37 24 31 C8 26 8 15 24 9 C42 3 64 3 80 23 Z"/>
      <path class="pad-vein" d="M80 23 L30 12 M80 23 L20 24 M80 23 L46 33 M80 23 L92 34 M80 23 L126 32 M80 23 L146 20"/>
      <path class="pad-gloss" d="M30 12 C48 6 66 8 74 16"/>
    </svg>`;
  }

  function lotus() {
    return `<svg class="lotus-art" viewBox="0 0 40 30" aria-hidden="true">
      <path class="lo-p back" d="M20 24 C10 22 4 14 6 8 C12 10 18 16 20 24 Z M20 24 C30 22 36 14 34 8 C28 10 22 16 20 24 Z"/>
      <path class="lo-p" d="M20 25 C13 19 12 9 20 2 C28 9 27 19 20 25 Z"/>
      <path class="lo-p side" d="M20 25 C12 24 8 18 9 12 C15 14 19 19 20 25 Z M20 25 C28 24 32 18 31 12 C25 14 21 19 20 25 Z"/>
      <circle class="lo-c" cx="20" cy="17" r="2.6"/>
    </svg>`;
  }

  function stone() {
    return `<svg class="stone-art" viewBox="0 0 120 46" aria-hidden="true">
      <ellipse class="st-shadow" cx="60" cy="40" rx="58" ry="6"/>
      <path class="st-body" d="M8 38 C4 24 22 8 52 6 C84 4 114 16 114 32 C114 42 92 44 60 44 C30 44 10 44 8 38 Z"/>
      <path class="st-gloss" d="M26 16 C40 10 62 9 78 13"/>
    </svg>`;
  }

  function reeds(cls = '') {
    // reeds, cattails and grass tufts for the banks
    return `<svg class="reeds-art ${cls}" viewBox="0 0 120 140" aria-hidden="true">
      <path class="rd" d="M20 140 C22 100 18 60 10 20"/><path class="rd" d="M34 140 C34 96 38 58 46 24"/>
      <path class="rd" d="M52 140 C50 110 52 80 60 52"/><path class="rd" d="M70 140 C72 110 80 80 96 58"/>
      <rect class="ct" x="5" y="22" width="9" height="30" rx="4.5" transform="rotate(-10 9 37)"/>
      <rect class="ct" x="41" y="26" width="9" height="28" rx="4.5" transform="rotate(12 45 40)"/>
      <path class="lf" d="M28 140 C26 110 6 84 0 74 C14 86 32 104 36 140 Z"/>
      <path class="lf" d="M60 140 C64 116 84 100 104 92 C92 104 74 118 70 140 Z"/>
      <path class="lf l2" d="M44 140 C44 120 36 104 26 96 C40 104 52 118 52 140 Z"/>
      <path class="gr" d="M80 140 Q84 124 82 112 M90 140 Q94 126 100 116 M100 140 Q102 130 110 124 M8 140 Q8 130 2 122"/>
    </svg>`;
  }

  // ------------------------------------------------------------------ pond
  const PLACES = [
    // [classes, left %, art]; sizes and heights live in scene.css (fractions of the pond strip's height)
    ['reeds left', -1.5, () => reeds('l')],
    ['pad p1', 9, () => lilyPad()],
    ['frog f-sit', 11.6, () => frogSit({ id: 'frogA' })],
    ['pad p2 sm', 31, () => lilyPad('sm')],
    ['lotus l1', 33.6, () => lotus()],
    ['frog f-peek', 52, () => frogPeek({ id: 'frogB' })],
    ['pad p3 xs', 66, () => lilyPad('xs')],
    ['stone s1', 80, () => stone()],
    ['frog f-rest', 81.6, () => frogSit({ eyes: 'happy', id: 'frogC' })],
    ['reeds right', 93, () => reeds('r')],
  ];

  function buildPond(host) {
    const pond = document.createElement('div');
    pond.className = 'pond';
    pond.setAttribute('aria-hidden', 'true');
    pond.innerHTML = `
      <svg class="water" viewBox="0 0 1600 200" preserveAspectRatio="none" focusable="false">
        <defs>
          <linearGradient id="gWater" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" class="w0"/><stop offset=".22" class="w1"/><stop offset=".62" class="w2"/><stop offset="1" class="w3"/>
          </linearGradient>
          <linearGradient id="gBank" x1="0" y1="0" x2="0" y2="1"><stop offset="0" class="bk0"/><stop offset="1" class="bk1"/></linearGradient>
          <radialGradient id="gGlint" cx=".5" cy=".5" r=".5"><stop offset="0" class="gl0"/><stop offset="1" class="gl1"/></radialGradient>
        </defs>
        <path class="bank" d="M0 34 C120 14 260 20 420 28 C640 40 900 22 1140 26 C1320 30 1480 16 1600 26 L1600 200 L0 200 Z" fill="url(#gBank)"/>
        <path class="water-body" d="M0 52 C140 40 300 46 470 50 C700 56 940 42 1160 46 C1340 50 1480 40 1600 46 L1600 200 L0 200 Z" fill="url(#gWater)"/>
        <path class="shore-gloss" d="M0 52 C140 40 300 46 470 50 C700 56 940 42 1160 46 C1340 50 1480 40 1600 46"/>
        <path class="sky-mirror" d="M0 70 C300 62 600 74 900 66 C1200 58 1400 70 1600 64 L1600 92 C1300 98 1000 86 700 96 C420 104 200 92 0 98 Z"/>
        <g class="glints">
          <ellipse cx="240" cy="84" rx="26" ry="2.4"/><ellipse cx="610" cy="118" rx="18" ry="2"/><ellipse cx="980" cy="92" rx="30" ry="2.6"/>
          <ellipse cx="1290" cy="128" rx="20" ry="2"/><ellipse cx="420" cy="150" rx="14" ry="1.6"/><ellipse cx="1470" cy="100" rx="16" ry="1.8"/>
        </g>
        <g class="moonpath"><ellipse cx="1390" cy="70" rx="40" ry="3"/><ellipse cx="1394" cy="88" rx="30" ry="2.6"/><ellipse cx="1388" cy="108" rx="44" ry="3"/>
          <ellipse cx="1392" cy="132" rx="26" ry="2.4"/><ellipse cx="1390" cy="160" rx="36" ry="2.6"/></g>
      </svg>
      <div class="ripples"><i class="rp r1"></i><i class="rp r2"></i><i class="rp r3"></i></div>`;
    for (const [cls, left, art] of PLACES) {
      const el = document.createElement('div');
      el.className = 'pond-item ' + cls;
      el.style.left = left + '%';
      el.innerHTML = art();
      pond.append(el);
    }
    host.append(pond);
    $$('.frog', pond).forEach(f => f.addEventListener('click', () => meep(f, true)));
    return pond;
  }

  // ------------------------------------------------------------------ bubbles + fireflies
  function makeBubbles(sky) {
    $$('.b, .ff', sky).forEach(b => b.remove());
    const rnd = (a, b) => a + Math.random() * (b - a);
    const n = Math.round(Math.min(26, Math.max(12, innerWidth / 76)));
    for (let i = 0; i < n; i++) {
      const size = 14 + Math.random() ** 2 * 86, rise = rnd(18, 42), sway = rnd(4, 9);
      const el = document.createElement('div');
      el.className = 'b';
      el.style.cssText = `width:${size.toFixed(0)}px;height:${size.toFixed(0)}px;left:${rnd(-2, 98).toFixed(1)}%;` +
        `--sx:${rnd(14, 46).toFixed(0)}px;animation-duration:${rise.toFixed(1)}s,${sway.toFixed(1)}s;animation-delay:${(-Math.random() * rise).toFixed(1)}s,${(-Math.random() * sway).toFixed(1)}s`;
      sky.append(el);
    }
    for (let i = 0; i < 16; i++) {
      const blink = rnd(1.6, 4.2), wander = rnd(6, 14);
      const el = document.createElement('div');
      el.className = 'ff';
      el.style.cssText = `left:${rnd(2, 98).toFixed(1)}%;bottom:${rnd(3, 30).toFixed(1)}%;--fx:${rnd(-60, 60).toFixed(0)}px;--fy:${rnd(-40, 20).toFixed(0)}px;` +
        `animation-duration:${blink.toFixed(1)}s,${wander.toFixed(1)}s;animation-delay:${(-Math.random() * blink).toFixed(1)}s,${(-Math.random() * wander).toFixed(1)}s`;
      sky.append(el);
    }
  }

  // ------------------------------------------------------------------ meeping + the governor
  const st = { mode: 'full', busy: false, hidden: false, timer: 0, pond: null };
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const animating = () => st.mode === 'full' && !st.busy && !st.hidden && !matchMedia('(prefers-reduced-motion: reduce)').matches;

  function meep(frog, byUser = false) {
    if (!frog || frog.classList.contains('meep') || st.mode === 'off') return;
    if (!byUser && !animating()) return;
    frog.classList.add('meep');
    setTimeout(() => frog.classList.remove('meep'), byUser ? 1100 : 950);
  }

  function schedule() {
    clearTimeout(st.timer);
    if (!animating()) return;
    const night = document.documentElement.dataset.theme === 'night';
    const wait = (night ? 30 : 14) + Math.random() * (night ? 50 : 30);
    st.timer = setTimeout(() => {
      const frogs = $$('.pond .frog').filter(f => f.offsetParent !== null);
      meep(frogs[Math.floor(Math.random() * frogs.length)]);
      schedule();
    }, wait * 1000);
  }

  function apply() {
    const b = document.body;
    b.classList.toggle('scene-still', st.mode === 'still');
    b.classList.toggle('scene-off', st.mode === 'off');
    b.classList.toggle('scene-paused', st.mode === 'full' && (st.busy || st.hidden));
    schedule();
  }

  return {
    init() {
      const sky = document.getElementById('sky');
      makeBubbles(sky);
      st.pond = buildPond(sky);
      document.addEventListener('visibilitychange', () => { st.hidden = document.hidden; apply(); });
      apply();
    },
    setMode(mode) { st.mode = mode || 'full'; apply(); },
    setBusy(on) { on = !!on; if (on !== st.busy) { st.busy = on; apply(); } },
    meep: (which) => meep(typeof which === 'string' ? document.querySelector(`.pond .${which}`) : which, true),
    state: () => ({ ...st, pond: !!st.pond, animating: animating() }),
    art: { frogSit, frogPeek, lilyPad, lotus, stone, reeds },
  };
})();
