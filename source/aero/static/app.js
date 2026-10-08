/* Aero UI: launcher (model picker + HF search), tuning overlay, lane-based chat (router, local model, Claude Fable
   reviewer, Claude Opus executor), live dashboard, settings. Plain JS, no build step. */
'use strict';

// ---------------------------------------------------------------- helpers
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(kid));
  return el;
};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtBytes = n => { if (!n) return '0 B'; const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return (i ? n.toFixed(n < 10 ? 2 : 1) : n) + ' ' + u[i]; };
const fmtNum = n => (n ?? 0).toLocaleString();
const fmtCtx = n => n >= 1024 && n % 1024 === 0 ? n / 1024 + 'k' : fmtK(n);
const fmtK = n => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + 'k' : String(n || 0);
const uid = () => Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);

const I = {
  gear: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
  plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  clip: '<svg viewBox="0 0 24 24"><path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>',
  send: '<svg viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
  stop: '<svg viewBox="0 0 24 24" style="fill:currentColor;stroke:none"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  panel: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/></svg>',
  x: '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>',
  search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>',
  file: '<svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>',
  text: '<svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10"/></svg>',
  tools: '<svg viewBox="0 0 24 24"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
  brain: '<svg viewBox="0 0 24 24"><path d="M9.5 2A2.5 2.5 0 0 0 7 4.5v.5a3 3 0 0 0-3 3v1a3 3 0 0 0 0 6v1a3 3 0 0 0 3 3h.5A2.5 2.5 0 0 0 12 19.5V4.5A2.5 2.5 0 0 0 9.5 2zM14.5 2A2.5 2.5 0 0 1 17 4.5v.5a3 3 0 0 1 3 3v1a3 3 0 0 1 0 6v1a3 3 0 0 1-3 3h-.5a2.5 2.5 0 0 1-4.5-1.5"/></svg>',
  copy: '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  retry: '<svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>',
  dots: '<svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></svg>',
  app: '<svg viewBox="0 0 24 24"><path d="M10 20H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v5"/><path d="M3 9h18"/><path d="m14 14 7 2.5-3 1.2-1.3 3z"/></svg>',
  mem: '<svg viewBox="0 0 24 24"><path d="M12 3a9 9 0 1 0 9 9"/><path d="M12 7v5l3 2"/><path d="M17 3h4v4"/><path d="m21 3-5 5"/></svg>',
  pin: '<svg viewBox="0 0 24 24"><path d="M12 17v5"/><path d="M9 10.76V6h6v4.76l2 3.24H7z"/><path d="M8 6h8"/></svg>',
  edit: '<svg viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
  sun: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  moon: '<svg viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>',
  auto: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" style="fill:currentColor;stroke:none"/></svg>',
  dash: '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>',
  loop: '<svg viewBox="0 0 24 24"><path d="M18.2 8.5a4.5 4.5 0 1 1 0 7c-2-1.6-4.4-5.4-6.2-7s-3.8-2.3-6.2-.4a4.5 4.5 0 1 0 0 7c2-1.6 4.4-5.4 6.2-7"/></svg>',
  orbit: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="2.6"/><ellipse cx="12" cy="12" rx="10" ry="4.2"/><ellipse cx="12" cy="12" rx="10" ry="4.2" transform="rotate(60 12 12)"/><ellipse cx="12" cy="12" rx="10" ry="4.2" transform="rotate(120 12 12)"/></svg>',
  review: '<svg viewBox="0 0 24 24"><path d="M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5z"/><path d="m8.5 12 2.5 2.5 5-5"/></svg>',
  bulb: '<svg viewBox="0 0 24 24"><path d="M9 18h6M10 22h4"/><path d="M12 2a7 7 0 0 0-4 12.7V17h8v-2.3A7 7 0 0 0 12 2z"/></svg>',
  github: '<svg viewBox="0 0 24 24"><path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.9a3.4 3.4 0 0 0-.9-2.6c3.1-.4 6.4-1.5 6.4-6.9A5.4 5.4 0 0 0 20 4.8 5 5 0 0 0 19.9 1S18.7.6 16 2.4a13.4 13.4 0 0 0-7 0C6.3.6 5.1 1 5.1 1A5 5 0 0 0 5 4.8a5.4 5.4 0 0 0-1.5 3.8c0 5.4 3.3 6.5 6.4 6.9a3.4 3.4 0 0 0-.9 2.6V22"/></svg>',
  plug: '<svg viewBox="0 0 24 24"><path d="M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0zM12 18v4"/></svg>',
  route: '<svg viewBox="0 0 24 24"><circle cx="6" cy="19" r="3"/><circle cx="18" cy="5" r="3"/><path d="M12 19h4.5a3.5 3.5 0 0 0 0-7h-8a3.5 3.5 0 0 1 0-7H12"/></svg>',
  spark: '<svg viewBox="0 0 24 24"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6.3 6.3l2.5 2.5M15.2 15.2l2.5 2.5M6.3 17.7l2.5-2.5M15.2 8.8l2.5-2.5"/></svg>',
  ext: '<svg viewBox="0 0 24 24"><path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>',
  lab: '<svg viewBox="0 0 24 24"><path d="M4 18a8 8 0 1 1 16 0"/><path d="m12 18 4-6"/><circle cx="12" cy="18" r="1.4"/><path d="M6.5 13.5l1 .6M12 9v1.2M17.5 13.5l-1 .6"/></svg>',
};
const ico = (name) => { const s = h('span', { class: 'ico', html: I[name] }); return s; };

async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers = { 'Content-Type': 'application/json', ...(o.headers || {}) }; delete o.json; }
  const r = await fetch(path, o);
  if (!r.ok) { let msg = r.statusText; try { const j = await r.json(); msg = j.detail || JSON.stringify(j); } catch { } throw new Error(msg); }
  const ct = r.headers.get('content-type') || '';
  return ct.includes('json') ? r.json() : r.text();
}

/** POST and consume a text/event-stream response. onEvent(obj) per event. Returns when the stream ends. */
async function sseFetch(path, body, onEvent, signal) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal });
  if (!r.ok) { let msg = r.statusText; try { msg = (await r.json()).detail; } catch { } throw new Error(msg); }
  const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = '';
  for (; ;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
      for (const line of chunk.split('\n')) if (line.startsWith('data: ')) { try { onEvent(JSON.parse(line.slice(6))); } catch (e) { console.error(e); } }
    }
  }
}

let toastT;
function toast(msg, err = false, ms = 3500) {
  const t = $('#toast'); t.textContent = msg; t.className = 'toast' + (err ? ' err' : ''); clearTimeout(toastT); toastT = setTimeout(() => t.classList.add('hidden'), ms);
}

function modal(title, body, wide = false) {
  $('#modalTitle').textContent = title;
  const b = $('#modalBody'); b.innerHTML = ''; b.append(body);
  $('#modal .modal').classList.toggle('wide', wide);
  $('#modal').classList.remove('hidden');
}
function closeModal() { $('#modal').classList.add('hidden'); }

function popMenu(anchor, items) {
  $$('.menu').forEach(m => m.remove());
  const m = h('div', { class: 'menu' }, items.map(([label, fn, cls]) => h('button', { class: cls || '', onclick: () => { m.remove(); fn(); } }, label)));
  document.body.append(m);
  const r = anchor.getBoundingClientRect();
  m.style.top = Math.min(r.bottom + 4, innerHeight - m.offsetHeight - 8) + 'px';
  m.style.left = Math.max(8, Math.min(r.right - m.offsetWidth, innerWidth - m.offsetWidth - 8)) + 'px';
  setTimeout(() => document.addEventListener('click', () => m.remove(), { once: true }), 0);
}

// ---------------------------------------------------------------- markdown
marked.setOptions({ gfm: true, breaks: false });
function renderMd(el, text, streaming = false) {
  let src = text || '';
  // close an unterminated code fence while streaming so the rest doesn't render as code-less text
  if (streaming && (src.match(/```/g) || []).length % 2 === 1) src += '\n```';
  el.innerHTML = DOMPurify.sanitize(marked.parse(src), { ADD_ATTR: ['target'] });
  el.querySelectorAll('pre code').forEach(code => {
    if (!streaming) { try { hljs.highlightElement(code); } catch { } }
    const pre = code.parentElement;
    const lang = (code.className.match(/language-(\S+)/) || [])[1];
    if (lang) pre.append(h('span', { class: 'lang' }, lang));
    pre.append(h('button', { class: 'copy', onclick: () => { navigator.clipboard.writeText(code.innerText); toast('Copied'); } }, 'Copy'));
  });
  el.querySelectorAll('a').forEach(a => { a.target = '_blank'; a.rel = 'noreferrer'; });
}

// ---------------------------------------------------------------- state
const S = {
  st: null, settings: null, hw: null,
  chats: [], chat: null, streaming: false, abort: null,
  pending: { pastes: [], atts: [], app: null },
  models: [], loadReq: null,
};
const PASTE_CHARS = 1200, PASTE_LINES = 18;

// ---------------------------------------------------------------- boot
async function boot() {
  $('#lSettings').innerHTML = I.gear; $('#settingsBtn').innerHTML = I.gear;
  $('#sbCollapse').innerHTML = I.panel; $('#sbExpand').innerHTML = I.panel;
  $('#attachBtn').innerHTML = I.clip; $('#modalClose').innerHTML = I.x;
  $('.search-ico').innerHTML = I.search; $('.ico-plus').innerHTML = I.plus; $('.ico-mem').innerHTML = I.mem; $('.ico-lab').innerHTML = I.lab;
  $('#dashBtn').innerHTML = I.dash;
  Scene.init(); bindTips();
  bindChatUI(); bindLauncher();
  await refreshState();
  applyTheme(); renderDashVisibility();
  loadToolDescriptions(); loadCloud();
  pollDash();
  await loadChats();
  if (S.st.engine.status === 'ready') showChat();
  else {
    await showLauncher();
    if (S.settings.auto_load_last && S.models[0] && S.models[0].exists && !S.st.busy) loadModel(S.models[0].id);
  }
  setInterval(refreshState, 5000);
  refreshMemCount();
  // tell the backend the window is closing so it can unload the model and exit
  addEventListener('pagehide', () => { try { navigator.sendBeacon('/api/bye'); } catch { } });
}

async function refreshState() {
  try {
    S.st = await api('/api/state?ui=1');
    const first = !S.settings;
    S.settings = S.st.settings; S.hw = S.st.hw;
    if (first) applyTheme();
    renderEngine(); syncBusy();
  } catch (e) { /* backend restarting */ }
}

function renderEngine() {
  const e = S.st.engine;
  const dot = $('#engineDot');
  dot.className = 'dot ' + ({ ready: 'ready', tuning: 'busy', loading: 'busy', error: 'error' }[e.status] || '');
  $('#modelName').textContent = e.model ? (e.model.name || 'Model') : 'No model loaded';
  $('#modelMeta').textContent = e.status === 'ready' ? `${fmtCtx(e.ctx)} ctx · ${e.tune?.tg ?? '?'} tok/s${e.vision ? ' · vision' : ''}${e.tune?.profile_name ? ' · ' + e.tune.profile_name : ''}` :
    e.status === 'idle' ? 'Click to choose a model' : e.status;
  const g = S.hw?.gpus?.[0];
  const gs = S.hw?.gpus || [];
  $('#hwChip').textContent = [gs.length ? gs.map(x => `${x.name} · ${fmtNum(x.total_mb)} MB VRAM`).join(' + ') : 'No dedicated GPU (CPU mode)', `${S.hw?.cpu || ''}`, `${Math.round((S.hw?.ram_total_mb || 0) / 1024)} GB RAM`].filter(Boolean).join('  ·  ');
  const banner = $('#banner');
  if (e.status === 'error' && e.error) { banner.className = 'banner err'; banner.textContent = 'Model server error:\n' + e.error; }
  else if (e.warning) { banner.className = 'banner'; banner.textContent = e.warning; }
  else banner.className = 'banner hidden';
  if (!S.st.llama.found) { banner.className = 'banner err'; banner.textContent = 'llama-server was not found in the install folder. Re-run Update-Aero.bat.'; }
  $('#emptySub').textContent = e.status === 'ready' ? `${e.model?.name} · ${fmtCtx(e.ctx)} context · ${e.desc}` : 'Load a model to start chatting.';
  renderToggles();
}

// ---------------------------------------------------------------- launcher
function bindLauncher() {
  let t;
  $('#hfQuery').addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => hfSearch($('#hfQuery').value), 350); });
  $('#browseBtn').onclick = async () => {
    try { const m = await api('/api/models/browse', { method: 'POST' }); if (!m.cancelled) { await renderMyModels(); toast('Added ' + m.name); } }
    catch (e) {
      const p = prompt('Paste the full path to a .gguf file:'); if (!p) return;
      try { await api('/api/models/add', { method: 'POST', json: { path: p } }); renderMyModels(); } catch (e2) { toast(e2.message, true); }
    }
  };
  $('#lSettings').onclick = openSettings;
  $('#lBack').onclick = () => showChat();
}

async function showLauncher() {
  $('#chat').classList.add('hidden'); $('#launcher').classList.remove('hidden');
  $('#lBack').classList.toggle('hidden', S.st?.engine.status !== 'ready');
  await renderMyModels();
  if (!$('#hfResults').childElementCount) hfSearch('');
  setTimeout(() => $('#hfQuery').focus(), 50);
}

async function renderMyModels() {
  const box = $('#myModels');
  try { S.models = await api('/api/models'); } catch (e) { S.models = []; }
  box.innerHTML = '';
  if (!S.models.length) {
    box.append(h('div', { class: 'empty-note' }, 'No models yet. Search Hugging Face on the right and download one; ', h('br'), 'it gets auto-tuned for this PC on first load.'));
    return;
  }
  const cur = S.st?.engine.model?.id;
  for (const m of S.models) {
    const tuned = m.tuned ? h('small', { class: 'tuned' }, `Tuned · ${fmtCtx(m.tuned.ctx)} ctx · ${m.tuned.tg} tok/s · ${m.tuned.limit_gb} GB limit · ${m.tuned.trials} trials${m.tuned.stale ? ' · llama.cpp updated since: re-tune for best results' : ''}`)
      : h('small', { class: 'untuned' }, m.exists ? 'Not tuned yet: first load asks for a VRAM limit, then runs the auto-tuner' : 'File missing');
    const more = h('button', { class: 'icon-btn', html: I.dots, title: 'More' });
    more.onclick = (ev) => {
      ev.stopPropagation();
      popMenu(more, [
        ['Load', () => loadModel(m.id)],
        ['Re-tune and load', () => loadModel(m.id, true)],
        ['Forget tuning', async () => { await api(`/api/models/${m.id}/forget_tune`, { method: 'POST' }); renderMyModels(); }],
        ['Remove from list', async () => { await api(`/api/models/${m.id}`, { method: 'DELETE' }); renderMyModels(); }],
        ['Delete model files…', async () => { if (confirm(`Delete ${m.name} from disk?`)) { await api(`/api/models/${m.id}?delete_files=true`, { method: 'DELETE' }); renderMyModels(); } }, 'danger'],
      ]);
    };
    const card = h('div', { class: 'mcard' + (m.id === cur ? ' current' : '') },
      h('div', { class: 'mc-main' },
        h('b', {}, m.name, m.id === cur ? h('span', { class: 'badge gpu' }, 'loaded') : null),
        h('div', { class: 'caps mini' }, ...Object.keys(CAP_LABEL).filter(k => k !== 'text' && ((m.caps || {})[k] ?? (k === 'vision' && !!m.mmproj)))
          .map(k => h('span', { class: 'cap cap-' + k, title: CAP_TIP[k] }, CAP_LABEL[k])),
          m.caps?.ctx_train ? h('span', { class: 'cap', title: 'Context the model was trained for' }, fmtCtx(m.caps.ctx_train) + ' trained ctx') : null),
        h('small', {}, [m.repo, m.size ? fmtBytes(m.size) : null].filter(Boolean).join(' · ')), tuned),
      m.exists ? h('button', { class: 'btn sm', onclick: () => m.id === cur ? showChat() : loadModel(m.id) }, m.id === cur ? 'Open' : 'Load') : null, more);
    box.append(card);
  }
}

let hfSeq = 0;
async function hfSearch(q) {
  const box = $('#hfResults'); const seq = ++hfSeq;
  box.innerHTML = '<div class="muted" style="padding:10px">Searching…</div>';
  try {
    const rows = await api('/api/hf/search?q=' + encodeURIComponent(q));
    if (seq !== hfSeq) return;
    box.innerHTML = '';
    if (!q) box.append(h('div', { class: 'muted', style: 'padding:2px 10px 8px;font-size:12.5px' }, 'Trending GGUF models'));
    if (!rows.length) box.append(h('div', { class: 'muted', style: 'padding:10px' }, 'No GGUF models found.'));
    for (const r of rows) {
      const [org, ...rest] = r.id.split('/');
      box.append(h('div', { class: 'hf-row', onclick: () => openRepo(r.id) },
        h('div', { class: 'hf-id' }, h('span', {}, org + '/'), rest.join('/')),
        r.gated ? h('span', { class: 'badge split' }, 'gated') : null,
        h('small', {}, `↓ ${fmtK(r.downloads)}  ♥ ${fmtK(r.likes)}  ${r.updated || ''}`)));
    }
  } catch (e) { if (seq === hfSeq) box.innerHTML = `<div class="err-msg">${esc(e.message)}</div>`; }
}

const CAP_LABEL = { text: 'Text', vision: 'Vision', audio: 'Audio', tools: 'Tool calling', thinking: 'Thinking', moe: 'MoE', mtp: 'MTP (faster decoding)' };
const CAP_TIP = {
  text: 'Reads and writes text.',
  vision: 'Understands images and screenshots (through the mmproj projector).',
  audio: 'Understands audio clips (through the mmproj projector).',
  tools: 'Its chat template supports tool calls, so agent mode can use files, the browser and the desktop.',
  thinking: 'Can reason step by step before answering (the Think toggle).',
  moe: 'Mixture of experts: only a few experts run per token, so it stays fast even when experts sit in system RAM.',
  mtp: 'Has multi-token-prediction layers for speculative decoding.',
};
const FIT_TXT = { gpu: 'Full GPU', gpu_short: 'Full GPU, short context', moe: 'Experts in RAM', split: 'CPU + GPU (slow)', no: 'Too big' };
const FIT_CLS = { gpu: 'gpu', gpu_short: 'split', moe: 'gpu', split: 'split', no: 'no' };

function fmtParams(n) { return !n ? '' : n >= 1e9 ? (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + 'B' : (n / 1e6).toFixed(0) + 'M'; }

async function openRepo(repo) {
  const p = $('#repoPanel'); p.classList.remove('hidden'); p.innerHTML = '';
  const close = h('button', { class: 'icon-btn', html: I.x, onclick: () => p.classList.add('hidden') });
  const body = h('div', { class: 'rp-body' }, h('div', { class: 'muted' }, 'Reading the model\'s files and header…'));
  p.append(h('div', { class: 'rp-head' }, h('h3', {}, repo), h('a', { href: `https://huggingface.co/${repo}`, target: '_blank', class: 'btn ghost sm' }, 'Model card'), close), body);
  try {
    const d = await api('/api/hf/files?repo=' + encodeURIComponent(repo));
    body.innerHTML = '';
    if (!d.files.length) { body.append(h('div', { class: 'empty-note' }, 'This repo has no GGUF model files.')); return; }
    const M = d.model || { caps: { text: true }, notes: [] }, meta = M.meta;
    // capabilities
    body.append(h('div', { class: 'caps' }, ...Object.keys(CAP_LABEL).filter(k => M.caps[k]).map(k =>
      h('span', { class: 'cap cap-' + k, title: CAP_TIP[k] }, CAP_LABEL[k]))));
    // general facts
    if (meta) {
      const rows = [
        ['Architecture', meta.arch + (meta.size_label ? ` · ${meta.size_label}` : '')],
        meta.params || meta.experts ? ['Parameters', [fmtParams(meta.params), meta.experts ? `${meta.experts} experts, ${meta.experts_used} active per token` : ''].filter(Boolean).join(' · ')] : null,
        ['Layers', `${meta.n_layer}` + (meta.kv_layers !== meta.n_layer ? ` (${meta.kv_layers} keep a KV cache; the rest are linear/recurrent)` : '')],
        ['Trained context', `${fmtNum(meta.ctx_train)} tokens` + (meta.sliding_window ? ` · sliding window ${fmtNum(meta.sliding_window)} on most layers` : '')],
        ['Context cost', `${fmtNum(M.kv_mb_32k)} MB of KV cache per 32k tokens (q8_0) · ${fmtNum(M.kv_mb_32k_f16)} MB at f16`],
        M.projector ? ['Projector', `${fmtBytes(M.projector.size)}${M.projector.type ? ' · ' + M.projector.type : ''} · ${[M.caps.vision && 'images', M.caps.audio && 'audio'].filter(Boolean).join(' + ')}`] : null,
        M.license ? ['License', M.license] : null,
      ].filter(Boolean);
      body.append(h('div', { class: 'facts' }, ...rows.map(([k, v]) => h('div', {}, h('span', {}, k), h('b', {}, v)))));
    }
    for (const n of M.notes || []) body.append(h('p', { class: 'warn-note' }, n));
    // vision toggle + budget
    let withProj = !!d.mmproj.length;
    const projBox = d.mmproj.length ? h('label', { class: 'proj-toggle' },
      h('input', { type: 'checkbox', checked: true, onchange: e => { withProj = e.target.checked; render(); } }),
      ` Download the ${M.caps.audio && !M.caps.vision ? 'audio' : M.caps.audio ? 'vision + audio' : 'vision'} projector (+${fmtBytes(d.mmproj[0].size)} of VRAM)`) : null;
    const why = h('p', { class: 'muted rec-why' });
    body.append(h('p', { class: 'muted', style: 'margin:10px 0 6px;font-size:13px' },
      `Planning for ${fmtNum(d.budget_mb)} MB of VRAM (${d.budget_note}) and ${Math.round(d.ram_mb / 1024)} GB RAM. ` +
      `Each quant is checked with its weights${d.mmproj.length ? ', the projector' : ''}, the KV cache and llama.cpp's buffers.`), projBox, why);
    const prog = h('div', { class: 'dl-progress hidden' }, h('div', { class: 'progress' }, h('i')), h('small', { class: 'muted' }, ''));
    body.append(prog);
    const tb = h('tbody');
    body.append(h('table', { class: 'qtable' }, h('thead', {}, h('tr', {}, h('th', {}, 'Quant'), h('th', {}, 'Size'), h('th', {}, 'Context on GPU'), h('th', {}, 'Fit'), h('th', {}))), tb));
    function render() {
      const key = withProj ? 'with_proj' : 'text_only';
      const rec = withProj ? d.recommended : d.recommended_text;
      const recWhy = withProj ? d.recommended_reason : d.recommended_text_reason;
      const rf = d.files.find(f => f.name === rec);
      const tf = d.files.find(f => f.name === d.recommended_text);
      const better = withProj && tf && rf && tf.name !== rf.name && (tf.bpw || 0) > (rf.bpw || 0);
      why.textContent = rf ? `Recommended: ${rf.quant}, ${recWhy}.` + (better ? ` Without the projector, ${tf.quant} fits instead.` : '') : '';
      tb.innerHTML = '';
      for (const f of d.files) {
        const P = f[key], isRec = f.name === rec;
        const fitKey = P ? P.fit : f.fit;
        const btn = h('button', { class: 'btn sm' + (isRec ? '' : ' ghost') }, f.local ? 'Downloaded' : 'Download');
        btn.onclick = () => downloadModel(repo, f, prog, btn, withProj);
        const tip = P ? `At ${fmtCtx(P.target_ctx)} context this needs about ${fmtNum(P.need_mb)} MB of VRAM` : '';
        tb.append(h('tr', { class: isRec ? 'rec' : '', title: tip },
          h('td', {}, h('b', {}, f.quant), f.bpw ? h('small', { class: 'bpw' }, ` ${f.bpw} bpw`) : null, isRec ? h('span', { class: 'badge rec' }, 'recommended') : null,
            h('div', { style: 'font-size:11.5px;color:var(--faint);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:240px' }, f.name + (f.parts.length > 1 ? ` (${f.parts.length} parts)` : ''))),
          h('td', { class: 'num' }, fmtBytes(f.size)),
          h('td', { class: 'num' }, P ? (P.max_ctx ? 'up to ' + fmtCtx(P.max_ctx) : '—') : '?'),
          h('td', {}, h('span', { class: 'badge ' + (FIT_CLS[fitKey] || 'split') }, FIT_TXT[fitKey] || fitKey)),
          h('td', { style: 'text-align:right' }, btn)));
      }
    }
    render();
  } catch (e) { body.innerHTML = `<div class="err-msg">${esc(e.message)}</div>`; }
}

async function downloadModel(repo, f, prog, btn, withProj = true) {
  if (S.st.busy) return toast('Another download or load is running.', true);
  prog.classList.remove('hidden'); btn.disabled = true;
  const bar = $('i', prog), lbl = $('small', prog);
  let model = null, err = null;
  try {
    await sseFetch('/api/hf/download', { repo, name: f.name, mmproj: withProj }, ev => {
      if (ev.type === 'progress') {
        const pct = ev.total ? ev.done / ev.total * 100 : 0;
        bar.style.width = pct.toFixed(1) + '%';
        lbl.textContent = `${fmtBytes(ev.done)} / ${fmtBytes(ev.total)} · ${fmtBytes(ev.speed)}/s · ${ev.file.split('/').pop()}`;
      } else if (ev.type === 'result') model = ev.result;
      else if (ev.type === 'error') err = ev.error;
    });
  } catch (e) { err = e.message; }
  btn.disabled = false;
  if (err) { lbl.textContent = err; toast(err, true); return; }
  lbl.textContent = 'Downloaded.'; btn.textContent = 'Downloaded';
  $('#repoPanel').classList.add('hidden');
  await renderMyModels();
  if (model) loadModel(model.id);
}

// ---------------------------------------------------------------- load + tune overlay
const DEPTH_INFO = {
  short: ['Short', '10 trials', 'The estimator\'s best guess plus the 9 most promising one-step moves.'],
  medium: ['Medium', '25 trials', 'Explores context, GPU offload, batch and KV precision. Recommended.'],
  long: ['Long', '50 trials', 'Wider search plus repeat runs of the top configs to average out noise.'],
  full: ['Full', 'until done', 'Keeps stepping until no one-step move can improve the goal (max 120).'],
};
const GOALS = [['balanced', 'Balanced'], ['max_context', 'Max context'], ['max_speed', 'Max speed']];
const fmtDur = s => s < 90 ? `${Math.max(1, Math.round(s))} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
const fmtClock = s => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;

/** A blocking prompt: no close button, Esc and backdrop clicks do nothing. */
function gate(title, ...body) {
  const card = h('div', { class: 'gate-card' }, h('h3', {}, title), ...body);
  const ov = h('div', { class: 'overlay gate' }, card);
  document.body.append(ov);
  return { ov, card, close: () => ov.remove() };
}

/** Parse "14.75", "14.75gb", "14.75 GB", "14,75". Returns GB or null. */
function parseGB(txt) {
  const m = String(txt || '').trim().replace(',', '.').match(/^(\d+(?:\.\d+)?|\.\d+)\s*(gb|gib|g)?$/i);
  return m ? parseFloat(m[1]) : null;
}

/** Step 1: the VRAM (or RAM) limit. Unskippable, no timer: the user types a number or takes the suggestion for
 *  this PC (which only fills the box; Continue still confirms it). */
function askVramLimit(info) {
  return new Promise(resolve => {
    const cpu = info.cpu_only;
    const totalGB = (cpu ? info.ram_total_mb : info.vram_total_mb) / 1024;
    const othersGB = info.others_mb / 1024;
    const input = h('input', { class: 'gb-input', placeholder: cpu ? 'e.g. 24' : `e.g. ${(Math.floor(totalGB * 4) / 4 - 1.25).toFixed(2)}`, autocomplete: 'off', spellcheck: 'false', inputmode: 'decimal' });
    const live = h('div', { class: 'gb-live' });
    const msg = h('div', { class: 'gb-msg' });
    const go = h('button', { class: 'btn', disabled: true }, 'Continue');
    const cancel = h('button', { class: 'btn ghost' }, 'Cancel load');
    const intro = cpu
      ? h('p', {}, `No dedicated GPU found, so the model runs in system RAM (${fmtNum(info.ram_total_mb)} MB). Type the most RAM in GB that `, h('b', {}, info.model), ' may use.')
      : h('p', {}, `Type the most VRAM in GB that `, h('b', {}, info.model), ` may use on your ${info.gpu} (${totalGB.toFixed(2)} GB). `,
        'Whatever is left over hosts the tuning advisor, so a tight limit means a smaller advisor (or the CPU).');
    const g = gate(cpu ? 'How much RAM can this model use?' : 'How much VRAM can this model use?', intro,
      h('div', { class: 'gb-row' }, input, h('span', { class: 'gb-unit' }, 'GB'),
        info.suggest ? h('button', { class: 'btn ghost sm', title: info.suggest.why, onclick: () => { input.value = String(info.suggest.gb); check(); input.focus(); } },
          `Suggested for this PC: ${info.suggest.gb} GB`) : null),
      info.suggest ? h('small', { class: 'muted' }, `Suggestion: ${info.suggest.why}.`) : null,
      info.last_limit_gb ? h('small', { class: 'muted' }, `Last time you typed ${info.last_limit_gb} GB.`) : null,
      live, msg, h('div', { class: 'gate-foot' }, cancel, go));
    const done = v => { g.close(); resolve(v); };
    const check = () => {
      const v = parseGB(input.value);
      live.innerHTML = ''; msg.textContent = ''; msg.className = 'gb-msg';
      let ok = v !== null && v >= 0.5 && v <= totalGB + 1e-9;
      const rows = cpu ? [['System RAM', `${fmtNum(info.ram_total_mb)} MB`], ['Free right now', `${fmtNum(info.ram_avail_mb)} MB`], ['Model file', `${fmtNum(info.size_mb)} MB`]]
        : [['Card', `${fmtNum(info.vram_total_mb)} MB`], [info.vram_measured === false ? 'Other apps (estimated)' : 'Other apps right now', `${fmtNum(info.others_mb)} MB`], ['Model file', `${fmtNum(info.size_mb)} MB`]];
      if (v !== null) {
        const lim = v * 1024;
        rows.push([cpu ? 'Model may use' : 'Model may use', `${fmtNum(Math.round(lim))} MB`]);
        if (!cpu) {
          const left = info.vram_total_mb - info.others_mb - lim - info.advisor_margin_mb;
          rows.push(['Left for the advisor', left > 0 ? `${fmtNum(Math.round(left))} MB` : 'none (advisor runs on the CPU)']);
        }
      }
      live.append(h('div', { class: 'kv' }, rows.flatMap(([a, b]) => [h('span', {}, a), h('span', {}, b)])));
      if (input.value.trim() && v === null) { msg.textContent = 'Type a number like 14.75 (GB).'; msg.classList.add('err'); }
      else if (v !== null && v < 0.5) { msg.textContent = 'Too small: at least 0.5 GB.'; msg.classList.add('err'); }
      else if (v !== null && v > totalGB + 1e-9) { msg.textContent = `That is more than the ${totalGB.toFixed(2)} GB ${cpu ? 'of RAM' : 'this card has'}.`; msg.classList.add('err'); }
      else if (v !== null) {
        const lim = v * 1024;
        const notes = [];
        if (!cpu && lim + info.others_mb > info.vram_total_mb - 64) notes.push(`Other apps are using ${fmtNum(info.others_mb)} MB right now, so free VRAM (not your number) will be the real limit.`);
        if (lim < info.size_mb * 1.03) notes.push(cpu ? 'The model barely fits or does not fit in this much RAM.' : 'The model is bigger than this limit, so some layers will run on the CPU (slower).');
        msg.textContent = notes.join(' ');
      }
      go.disabled = !ok;
      return ok ? v : null;
    };
    input.addEventListener('input', check);
    input.addEventListener('keydown', e => { if (e.key === 'Enter') { const v = check(); if (v !== null) done(v); } });
    go.onclick = () => { const v = check(); if (v !== null) done(v); };
    cancel.onclick = () => done(null);
    check();
    setTimeout(() => input.focus(), 30);
  });
}

/** Step 2: how many trials. 90-second visible countdown, Medium when it runs out. */
function askDepth(info, limitGB) {
  return new Promise(resolve => {
    const SECONDS = 90;
    let left = SECONDS, sel = 'medium', mode = info.mode || 'balanced', timer = null;
    const per = info.per_trial_s;
    const fixed = 45 + 60;  // advisor start-up + long-prompt check
    const est = k => k === 'full' ? `up to ${fmtDur(fixed + info.full_cap * (per + 7))}` : `≈ ${fmtDur(fixed + info.depths[k] * (per + 7))}`;
    const clock = h('div', { class: 'cd-clock' }, fmtClock(SECONDS));
    const bar = h('div', { class: 'cd-bar' }, h('i'));
    const label = h('div', { class: 'cd-label' });
    const cards = h('div', { class: 'depth-grid' });
    const goal = h('div', { class: 'seg goal-seg' });
    const cancel = h('button', { class: 'btn ghost' }, 'Cancel load');
    const g = gate('How long should tuning run?',
      h('p', {}, `Every trial is a real llama-server run of `, h('b', {}, info.model), ` inside your ${limitGB} GB limit. Pick a depth, or Aero starts `,
        h('b', {}, 'Medium'), ' when the timer runs out.'),
      h('div', { class: 'cd' }, clock, h('div', { class: 'cd-right' }, label, bar)), cards,
      h('div', { class: 'goal-row' }, h('span', { class: 'muted' }, 'Goal'), goal),
      h('div', { class: 'gate-foot' }, h('small', { class: 'muted', style: 'margin-right:auto' }, 'Keep games and other GPU apps closed while it runs.'), cancel));
    const finish = v => { clearInterval(timer); g.close(); resolve(v); };
    const render = () => {
      cards.innerHTML = '';
      for (const [k, [name, n, desc]] of Object.entries(DEPTH_INFO)) {
        cards.append(h('button', { class: 'depth' + (k === sel ? ' on' : ''), onclick: () => finish({ depth: k, mode }) },
          h('b', {}, name), h('span', { class: 'dn' }, n), h('small', {}, desc), h('span', { class: 'de' }, est(k))));
      }
      goal.innerHTML = '';
      for (const [k, lab] of GOALS) goal.append(h('button', { class: k === mode ? 'on' : '', onclick: () => { mode = k; render(); } }, lab));
      label.textContent = `Starting ${DEPTH_INFO[sel][0]} in ${fmtClock(left)}`;
    };
    cancel.onclick = () => finish(null);
    render();
    timer = setInterval(() => {
      left--;
      clock.textContent = fmtClock(Math.max(0, left));
      $('i', bar).style.width = (left / SECONDS * 100) + '%';
      label.textContent = `Starting ${DEPTH_INFO[sel][0]} in ${fmtClock(Math.max(0, left))}`;
      clock.classList.toggle('low', left <= 10);
      if (left <= 0) finish({ depth: sel, mode, auto: true });
    }, 1000);
  });
}

async function loadModel(id, retune = false, preset = null) {
  if (S.streaming) return toast('Wait for the current reply to finish.', true);
  if (S.st?.busy) return toast('Another download or load is running.', true);
  let info;
  try { info = await api(`/api/models/${id}/tune_info`); } catch (e) { return toast(e.message, true); }
  let req = { id };
  if (retune || !info.tuned) {
    const limit = await askVramLimit(info);
    if (limit === null) return;
    const pick = preset?.depth ? { depth: preset.depth, mode: info.mode || 'balanced' } : await askDepth(info, limit);
    if (!pick) return;
    req = { id, retune: true, vram_limit_gb: limit, depth: pick.depth, mode: pick.mode };
  }
  const res = await runLoad(req, info);
  if (res?.needs_tune) {
    toast(res.reason, true, 6000);
    return loadModel(id, true);
  }
}

async function runLoad(req, info) {
  S.loadReq = req;
  const ov = $('#loadOverlay'); ov.classList.remove('hidden');
  $('#loadTitle').textContent = `Loading ${info?.model || 'model'}`;
  $('#loadSub').textContent = req.vram_limit_gb ? 'Preparing the tuning advisor…' : 'Using saved tuning…';
  $('#loadSpin').className = 'spinner';
  $('#loadLog').innerHTML = ''; $('#trialTable').classList.add('hidden'); $('#trialTable tbody').innerHTML = '';
  $('#loadFoot').classList.add('hidden'); $('#loadCancel').classList.remove('hidden');
  $('#loadProgress').classList.add('hidden'); $('#advChip').classList.add('hidden'); $('#trialCount').textContent = '';
  const log = (t, cls = '') => { const d = h('div', { class: cls }, t); $('#loadLog').append(d); $('#loadLog').scrollTop = 1e9; };
  const bar = p => { $('#loadProgress').classList.remove('hidden'); $('#loadProgress i').style.width = Math.min(100, p).toFixed(1) + '%'; };
  let result = null, err = null, budget = 0, done = 0;
  try {
    await sseFetch('/api/load', req, ev => {
      switch (ev.type) {
        case 'log': log(ev.text); break;
        case 'advisor': log('advisor · ' + ev.text, 'adv'); break;
        case 'advisor_progress': {
          bar(ev.total ? ev.done / ev.total * 100 : 0);
          $('#loadSub').textContent = `Pulling the advisor model: ${fmtBytes(ev.done)} / ${fmtBytes(ev.total)} · ${fmtBytes(ev.speed)}/s`;
          break;
        }
        case 'advisor_ready': {
          const c = $('#advChip'); c.classList.remove('hidden'); c.textContent = `Advisor: ${ev.name} · ${ev.where}`;
          $('#loadProgress').classList.add('hidden'); break;
        }
        case 'tune_begin':
          budget = ev.budget;
          $('#loadTitle').textContent = `Auto-tuning ${ev.meta.name}`;
          $('#loadSub').textContent = `${DEPTH_INFO[ev.depth][0]} · ${GOALS.find(g => g[0] === ev.mode)?.[1]} · model limit ${fmtNum(Math.round(ev.limit_mb))} MB. ` +
            (ev.advisor ? `${ev.advisor} picks each one-step move; every result is measured.` : 'The built-in step policy picks each one-step move; every result is measured.') + ' Saved afterwards, so this only happens once.';
          if (!ev.advisor) { const c = $('#advChip'); c.classList.remove('hidden'); c.textContent = 'Advisor: built-in step policy'; }
          $('#trialTable').classList.remove('hidden'); bar(0); break;
        case 'trial_start': {
          const by = { advisor: 'advisor', policy: 'policy', estimator: 'estimator', verifier: 'verifier' }[ev.by] || ev.by;
          const mv = ev.base ? `${ev.move} ← #${ev.base}` : ev.move;
          const tr = h('tr', { id: 'trial-' + ev.n, title: ev.reason ? `${by}: ${ev.reason}` : '' },
            h('td', {}, ev.n), h('td', { class: 'mv' }, mv, h('small', { class: 'by ' + ev.by }, by)), h('td', { class: 'cfg' }, ev.desc),
            h('td', { class: 'run' }, 'testing…'), h('td'), h('td'), h('td'));
          $('#trialTable tbody').append(tr); $('#trialTable').scrollTop = 1e9;
          $('#trialCount').textContent = `trial ${ev.n} of ${ev.budget}`;
          break;
        }
        case 'trial_done': {
          done++; bar(done / Math.max(1, budget) * 100);
          const tr = $('#trial-' + ev.n); if (!tr) break;
          const td = tr.children;
          td[3].className = ev.ok ? 'ok' : 'bad'; td[3].title = ev.error || '';
          td[3].textContent = ev.ok ? (ev.retest ? `ok · avg ${ev.avg_tg} t/s` : `ok (${ev.load_s}s load)`) : ev.error;
          td[4].textContent = ev.ok ? `${fmtNum(Math.round(ev.pp))} t/s` : ''; td[5].textContent = ev.ok ? `${ev.tg} t/s` : '';
          td[6].textContent = ev.mem_mb ? fmtNum(ev.mem_mb) + ' MB' : ''; break;
        }
        case 'verify_start':
          $('#loadSub').textContent = `Long-prompt check of #${ev.n}: filling ${fmtNum(ev.tokens)} tokens of context…`;
          log(`Long-prompt check of #${ev.n} (${ev.desc}) with ${fmtNum(ev.tokens)} tokens…`); break;
        case 'verify_done':
          log(ev.ok ? `Long-prompt check passed: ${fmtNum(ev.filled)} tokens read at ${fmtNum(Math.round(ev.pp_deep))} t/s, then ${ev.tg_deep} t/s generation.` : `Long-prompt check failed: ${ev.error}`, ev.ok ? 'ok' : 'err'); break;
        case 'tune_done':
          log(`Best: #${ev.n} ${ev.desc} · ${ev.tg} tok/s generate · ${fmtNum(Math.round(ev.pp))} tok/s prompt · ${ev.trials} trials in ${fmtDur(ev.elapsed_s)} · ${ev.verdict}`, 'ok');
          $('#trial-' + ev.n)?.classList.add('best'); bar(100);
          break;
        case 'loading': $('#loadTitle').textContent = 'Loading model'; $('#loadSub').textContent = ev.profile ? `${ev.profile} profile · ${ev.desc}` : ev.desc; break;
        case 'result': result = ev.result; break;
        case 'error': err = ev.error; break;
      }
    });
  } catch (e) { err = e.message; }
  await refreshState();
  if (result?.needs_tune) { ov.classList.add('hidden'); return result; }
  if (err) {
    $('#loadSpin').className = 'spinner fail'; $('#loadTitle').textContent = err === 'Cancelled' ? 'Cancelled' : 'Could not load the model';
    log(err, 'err'); $('#loadFoot').classList.remove('hidden'); $('#loadCancel').classList.add('hidden');
    return null;
  }
  $('#loadSpin').className = 'spinner done';
  const hadTrials = !!$('#trialTable tbody').childElementCount;
  setTimeout(() => {
    ov.classList.add('hidden');
    if (result?.warning) toast(result.warning, true, 8000);
    else toast(`Ready · ${fmtCtx(result.ctx)} context · ${result.tg} tok/s`);
    showChat();
  }, hadTrials ? 1500 : 0);
  return result;
}
$('#loadCancel').onclick = () => api('/api/cancel', { method: 'POST' });
$('#loadRetry').onclick = () => S.loadReq && (S.loadReq.vram_limit_gb ? loadModel(S.loadReq.id, true) : runLoad(S.loadReq, { model: S.models.find(x => x.id === S.loadReq.id)?.name }));
$('#loadClose').onclick = () => { $('#loadOverlay').classList.add('hidden'); showLauncher(); };

// ---------------------------------------------------------------- chats
async function loadChats() { try { S.chats = await api('/api/chats'); } catch { S.chats = []; } renderChatList(); }

function renderChatList() {
  const box = $('#chatList'); box.innerHTML = '';
  for (const c of S.chats) {
    const del = h('button', { class: 'del', html: I.trash, title: 'Delete chat' });
    del.onclick = async (ev) => {
      ev.stopPropagation();
      await api('/api/chats/' + c.id, { method: 'DELETE' });
      if (S.chat?.id === c.id) newChat();
      loadChats();
    };
    const looping = S.loop?.active && S.loop.chatId === c.id;
    box.append(h('div', { class: 'chat-item' + (S.chat?.id === c.id ? ' active' : ''), onclick: () => openChat(c.id) }, h('span', {}, c.title, looping ? h('span', { class: 'loopmark', 'data-tip': 'Forever-loop running' }, '∞') : null), del));
  }
}

function leaveChat() {
  const c = S.chat;
  if (c && S.settings?.auto_memorize && S.settings?.memory_enabled && c.messages.filter(m => m.role === 'user').length >= 2)
    api(`/api/chats/${c.id}/memorize`, { method: 'POST' }).catch(() => { });
}

const PHONE = matchMedia('(max-width: 860px)');
function setSidebar(open) {
  $('#sidebar').classList.toggle('collapsed', !open);
  $('#sbExpand').classList.toggle('hidden', open);
}

function newChat() {
  if (PHONE.matches) setSidebar(false);
  if (S.streaming) stopGen();
  if (S.loop?.active) stopLoop('Forever-loop stopped.');
  leaveChat();
  S.chat = { id: uid(), title: '', messages: [], created: Date.now() / 1000 };
  renderMessages(); renderChatList(); $('#chatTitle').textContent = ''; $('#input').focus();
}

async function openChat(id) {
  if (S.streaming) return toast('Stop the current reply first.', true);
  if (S.compacting) return toast('Still compacting this chat…');
  if (S.chat?.id !== id) { leaveChat(); if (S.loop?.active) stopLoop('Forever-loop stopped because you switched chats.'); }
  try { S.chat = await api('/api/chats/' + id); } catch (e) { return toast(e.message, true); }
  if (PHONE.matches) setSidebar(false);
  renderMessages(); renderChatList(); $('#chatTitle').textContent = S.chat.title || '';
}

async function saveChat() {
  if (!S.chat || !S.chat.messages.length) return;
  await api('/api/chats/' + S.chat.id, { method: 'PUT', json: S.chat });
  const ex = S.chats.find(c => c.id === S.chat.id);
  if (!ex) { S.chats.unshift({ id: S.chat.id, title: S.chat.title || 'New chat', updated: Date.now() / 1000 }); }
  else { ex.title = S.chat.title || ex.title; S.chats = [ex, ...S.chats.filter(c => c !== ex)]; }
  renderChatList();
}

function showChat() {
  $('#launcher').classList.add('hidden'); $('#chat').classList.remove('hidden');
  if (!S.chat) newChat(); else renderMessages();
  $('#input').focus();
}

// ---------------------------------------------------------------- lanes: who is talking
Object.assign(S, {
  toolDesc: {}, toolCats: {}, cloud: null, activeLane: null, userStopped: false, lastRun: null,
  loop: { armed: false, active: false, iteration: 0, started: 0, base: null, chatId: null, finishing: false, waitUntil: 0, cancel: null },
});
const LANE = {
  router: { label: 'Router', role: 'picks tools and thinking · CPU' },
  local: { label: 'Local model', role: 'on this PC' },
  astra: { label: 'GPT-6 Astra', role: 'ChatGPT · reviewer', plan: 'ChatGPT' },
  sol: { label: 'GPT-6.1 Sol', role: 'ChatGPT · repairs', plan: 'ChatGPT' },
  fable: { label: 'Claude Fable 5.1', role: 'reviewer', plan: 'Claude' },
  opus: { label: 'Claude Opus 5.5', role: 'takes over', plan: 'Claude' },
};
const REVIEW_LANES = new Set(['fable', 'astra']), TAKEOVER_LANES = new Set(['opus', 'sol']);
const isCloudLane = n => REVIEW_LANES.has(n) || TAKEOVER_LANES.has(n);
const isReviewerMsg = m => REVIEW_LANES.has(m.from);
const PHASE_NOW = { work: 'working', fix: 'fixing', lesson: 'learning', review: 'reviewing', execute: 'taking over' };
const PHASE_DONE = { work: 'done', fix: 'fixed', lesson: 'learned', review: 'reviewed', execute: 'done' };
const CAT_ICON = { files_read: 'file', files_write: 'edit', shell: 'text', screen: 'app', desktop: 'app', browser: 'ext', web: 'search',
  memory: 'mem', mcp: 'plug', skills: 'bulb', meta: 'tools', cloud: 'review', claude_code: 'spark' };

function laneModelName(lane, model) {
  if (model) return model;
  if (lane === 'local') return S.st?.engine.model?.name || LANE.local.label;
  return LANE[lane]?.label || lane;
}
function phaseText(phase, round, done) {
  const t = (done ? PHASE_DONE : PHASE_NOW)[phase] || phase || '';
  return round && (phase === 'review' || phase === 'fix') ? `${t} · round ${round}` : t;
}

/** Each turn element carries a context: which lane block is open, the router card, the live cursor. */
function newTurn(box) {
  const el = h('div', { class: 'msg turn' });
  box.append(el);
  el._ctx = { el, lane: null, lanes: [], router: null, afterFix: false };
  return el._ctx;
}
function turnCtx(el) { if (!el._ctx) el._ctx = { el, lane: null, lanes: [], router: null, afterFix: false }; return el._ctx; }

function ensureLane(ctx, lane, model, phase, round, via) {
  lane = lane || 'local';
  if (ctx.lane && ctx.lane.name === lane) {
    if (phase) { ctx.lane.phase = phase; ctx.lane.round = round; ctx.lane.ph.textContent = phaseText(phase, round, !S.streaming); }
    return ctx.lane;
  }
  if (ctx.lane) finishLane(ctx.lane);
  phase = phase || (REVIEW_LANES.has(lane) ? 'review' : TAKEOVER_LANES.has(lane) ? 'execute' : ctx.afterFix ? 'fix' : 'work');
  const orb = h('span', { class: 'orb' });
  const name = h('b', {}, laneModelName(lane, model));
  const role = h('span', {}, via === 'plan' ? `${LANE[lane]?.role || ''} · on your ${LANE[lane]?.plan || 'Claude'} plan` : (LANE[lane]?.role || ''));
  const ph = h('span', { class: 'phase' }, phaseText(phase, round, !S.streaming));
  const el = h('div', { class: 'lane lane-' + lane }, h('div', { class: 'lane-head' }, orb, name, role, ph));
  ctx.el.append(el);
  const L = { name: lane, el, orb, nameEl: name, ph, phase, round, msgs: [], foot: null, via };
  ctx.lane = L; ctx.lanes.push(L);
  return L;
}
function finishLane(L, stopped) {
  if (!L) return;
  L.orb.classList.remove('live');
  L.ph.textContent = stopped ? 'stopped' : phaseText(L.phase, L.round, true);
  if (L.el.childElementCount <= 1 && !L.msgs.length) L.el.remove();     // nothing came out of it
}
function closeLane(ctx) { if (ctx.lane) finishLane(ctx.lane); ctx.lane = null; }

// ---------------------------------------------------------------- rendering messages
function renderMessages() {
  const box = $('#messages'); box.innerHTML = '';
  const msgs = S.chat?.messages || [];
  $('#empty').classList.toggle('hidden', msgs.length > 0 || !!S.chat?.carry);
  if (S.chat?.carry) box.append(carryCard(S.chat.carry));
  let ctx = null;
  const need = () => ctx || (ctx = newTurn(box));
  for (const m of msgs) {
    if (m.role === 'user' && !isReviewerMsg(m)) { if (ctx) closeLane(ctx); ctx = null; box.append(renderUser(m)); continue; }
    renderItem(need(), m);
  }
  if (ctx) { closeLane(ctx); for (const L of ctx.lanes) finishLane(L); }
  if (!S.streaming) $$('.tstat.run, .tstat.ask', box).forEach(s => { s.className = 'tstat err'; s.textContent = 'not run'; });
  updateCtxMeter();
  scrollBottom(true);
}

function renderItem(ctx, m) {
  switch (m.role) {
    case 'router': closeLane(ctx); ctx.router = routerCard(m); ctx.el.append(ctx.router); break;
    case 'assistant': appendAssistant(ensureLane(ctx, m.lane, m.model, null, null, m.stats?.via), m); break;
    case 'tool': attachToolResult(ctx, m); break;
    case 'review': closeLane(ctx); ctx.el.append(reviewCard(m)); ctx.afterFix = false; break;
    case 'user': closeLane(ctx); ctx.el.append(fixCard(m)); ctx.afterFix = true; break;      // from a reviewer
    case 'lesson': closeLane(ctx); ctx.el.append(lessonCard(m)); break;
    case 'notice': appendNotice(ctx, m); break;
  }
}

function appendNotice(ctx, m) {
  const el = h('div', { class: m.error || m.level === 'error' ? 'err-msg' : 'notice' }, m.content);
  if (m.lane && ctx.lane && ctx.lane.name === m.lane) ctx.lane.el.append(el); else ctx.el.append(el);
  return el;
}

function carryCard(c) {
  const open = h('a', { href: '#', onclick: e => { e.preventDefault(); openChat(c.from); } }, c.from_title || 'the earlier chat');
  const body = h('div', { class: 'carry-body' });
  renderMd(body, c.summary + (c.open_tasks?.length ? '\n\n**Open tasks**\n' + c.open_tasks.map(t => '- ' + t).join('\n') : ''));
  return h('details', { class: 'carry' }, h('summary', {}, h('span', { class: 'ico', html: I.mem }),
    h('span', {}, 'Continued from ', open, '. The model starts with this summary plus your long-term memory.')), body);
}

function renderUser(m) {
  const wrap = h('div', { class: 'msg user' });
  const atts = h('div', { class: 'u-atts' });
  for (const a of m.attachments || []) {
    if (a.kind === 'image') atts.append(h('img', { src: '/api/uploads/' + a.id, alt: a.name, onclick: () => viewImage('/api/uploads/' + a.id, a.name) }));
    else atts.append(fileChip(a, () => viewFile(a)));
  }
  for (const p of m.pastes || []) atts.append(pasteChip(p, () => viewPaste(p)));
  if (m.target_app) atts.append(h('div', { class: 'chip app-chip', title: m.target_app.title }, h('div', { class: 'ci', html: I.app }),
    h('div', { class: 'ct' }, h('b', {}, m.target_app.title), h('small', {}, m.target_app.app))));
  if (atts.childElementCount) wrap.append(atts);
  if (m.loop) wrap.append(h('div', { class: 'loop-tag' }, `∞ Forever-loop · round ${m.loop.iteration || 1}`));
  if (m.content) wrap.append(h('div', { class: 'bubble' }, m.content));
  return wrap;
}

function fileChip(a, onclick, onremove) {
  const c = h('div', { class: 'chip u-chip', onclick }, h('div', { class: 'ci', html: I.file }),
    h('div', { class: 'ct' }, h('b', {}, a.name), h('small', {}, `${fmtBytes(a.size)}${a.chars ? ' · ' + fmtK(Math.round(a.chars / 3.5)) + ' tokens' : ''}`)));
  if (onremove) c.append(h('button', { class: 'x', onclick: (e) => { e.stopPropagation(); onremove(); } }, '×'));
  return c;
}
function pasteChip(p, onclick, onremove) {
  const lines = p.text.split('\n').length;
  const c = h('div', { class: 'chip paste', onclick }, h('div', { class: 'ci', html: I.text }),
    h('div', { class: 'ct' }, h('b', {}, p.name || 'Pasted text'), h('small', {}, `${lines} lines · ${fmtK(p.text.length)} chars`)));
  if (onremove) c.append(h('button', { class: 'x', onclick: (e) => { e.stopPropagation(); onremove(); } }, '×'));
  return c;
}
function viewPaste(p, editable) {
  const pre = h('pre', { class: 'paste-view' }, p.text);
  const body = h('div', {}, pre);
  if (editable) body.append(h('div', { style: 'display:flex;gap:8px;justify-content:flex-end' },
    h('button', { class: 'btn ghost', onclick: () => { const i = S.pending.pastes.indexOf(p); if (i >= 0) S.pending.pastes.splice(i, 1); insertAtCursor(p.text); renderChips(); closeModal(); } }, 'Insert as plain text'),
    h('button', { class: 'btn', onclick: closeModal }, 'Done')));
  modal(p.name || 'Pasted text', body, true);
}
async function viewFile(a) {
  if (a.kind === 'image') return viewImage('/api/uploads/' + a.id, a.name);
  const r = await api(`/api/uploads/${a.id}/text`);
  modal(a.name, h('pre', { class: 'paste-view' }, r.text || '(no text could be extracted)'), true);
}
function viewImage(src, name) { modal(name || 'Image', h('img', { class: 'full', src }), true); }

function thinkBlock(text, cloud, open = false) {
  return h('details', { class: 'think', open }, h('summary', {}, cloud ? 'Thinking (summary)' : 'Thought process'), h('div', { class: 'think-body' }, text));
}

function appendAssistant(L, m) {
  const cloud = isCloudLane(L.name);
  if (m.reasoning) L.el.append(thinkBlock(m.reasoning, cloud));
  if (m.content) { const md = h('div', { class: 'md' }); renderMd(md, m.content); L.el.append(md); }
  for (const tc of m.tool_calls || []) if (!isHiddenCall(tc.function.name)) L.el.append(toolCard(tc.id, tc.function.name, parseArgs(tc.function.arguments)));
  L.msgs.push(m);
  if (cloud) laneFoot(L);
  else if (m.stats && !(m.tool_calls || []).length) L.el.append(statsRow(m));
}

/** Calls to Aero's own tools made by Claude through Claude Code get their card from the tool run itself. */
const isHiddenCall = name => name.startsWith('mcp__aero__');
function parseArgs(s) { try { const a = JSON.parse(s || '{}'); return a && typeof a === 'object' ? a : { value: a }; } catch { return {}; } }
function argLabel(args) {
  if (!args) return '';
  const v = args.command || args.path || args.file_path || args.url || args.query || args.pattern || args.text || args.keys || args.title ||
    args.target || args.window || args.description || (args.x != null ? `${args.x}, ${args.y}` : '') || Object.values(args).find(x => typeof x === 'string') || '';
  return String(v).replace(/\s+/g, ' ').slice(0, 200);
}
function findCard(scope, id) { return $(`.tool[data-cid="${CSS.escape(id)}"]`, scope); }

function toolCard(id, name, args, category, label) {
  const info = toolInfo(name);
  const cat = category || info.catKey;
  const card = h('div', { class: 'tool', 'data-cid': id },
    h('div', { class: 'tool-head', onclick: () => card.classList.toggle('open') },
      h('span', { class: 'tico', html: I[CAT_ICON[cat]] || I.tools }),
      h('span', { class: 'tname', 'data-tool': name }, info.name),
      h('span', { class: 'targ' }, label || argLabel(args)),
      h('span', { class: 'tstat run' }, 'running…')),
    h('div', { class: 'tool-body' }, h('div', { class: 'lbl' }, 'Input'), h('pre', {}, JSON.stringify(args || {}, null, 2))));
  return card;
}

function attachToolResult(ctx, m) {
  let card = findCard(ctx.el, m.tool_call_id);
  if (!card) {
    card = toolCard(m.tool_call_id, m.name, null, null, m.label);
    $('pre', card)?.previousElementSibling?.remove(); $('pre', card)?.remove();
    const L = ctx.lane && ctx.lane.name === (m.lane || 'local') ? ctx.lane : ensureLane(ctx, m.lane, null);
    L.el.append(card);
  }
  $('.approve', card)?.remove();
  const st = $('.tstat', card);
  st.className = 'tstat ' + (m.denied || m.error ? 'err' : 'ok');
  st.textContent = m.denied ? 'denied' : m.error ? 'error' : 'done';
  const body = $('.tool-body', card);
  $$('.res', body).forEach(x => x.remove());
  body.append(h('div', { class: 'lbl res' }, 'Output'), h('pre', { class: 'res' }, m.content || ''));
  if (m.image && !$('img.shot', card)) card.append(h('img', { class: 'shot', src: '/api/uploads/' + m.image, onclick: () => viewImage('/api/uploads/' + m.image) }));
  if (m.error && !m.denied) card.classList.add('open');
}

function statsRow(m) {
  const s = m.stats || {};
  const copy = h('button', { html: I.copy, 'data-tip': 'Copy reply', onclick: () => { navigator.clipboard.writeText(m.content || ''); toast('Copied'); } });
  const parts = [];
  if (s.tg) parts.push(`${s.tg} tok/s`);
  if (s.completion_tokens) parts.push(`${fmtNum(s.completion_tokens)} tokens`);
  if (s.cached_tokens && s.prompt_tokens) parts.push(`${Math.round(s.cached_tokens / s.prompt_tokens * 100)}% prompt cached`);
  if (s.ttft != null) parts.push(`${s.ttft}s to first token`);
  if (s.finish === 'length') parts.push('cut off (max tokens / context)');
  if (s.finish === 'stopped') parts.push('stopped');
  return h('div', { class: 'stats' }, copy, ...parts.map(p => h('span', {}, p)));
}

/** Footer of a Claude lane: steps, tokens and what it cost (API) or would have cost (plan). */
function laneFoot(L) {
  let pin = 0, out = 0, cache = 0, usd = 0, est = 0, t = 0, steps = 0, via = L.via || '';
  for (const m of L.msgs) {
    const s = m.stats || {}; steps++;
    pin += s.prompt_tokens || 0; out += s.completion_tokens || 0; cache += s.cached_tokens || 0;
    usd += s.usd || 0; est += s.est_usd || 0; t += s.time || 0; if (s.via) via = s.via;
  }
  const last = [...L.msgs].reverse().find(m => m.content);
  const copy = h('button', { html: I.copy, 'data-tip': 'Copy the final answer', onclick: () => { navigator.clipboard.writeText(last?.content || ''); toast('Copied'); } });
  const gpt = L.name === 'astra' || L.name === 'sol';
  const cost = via === 'plan'
    ? h('span', { class: 'cost', 'data-tip': gpt ? 'Runs on your ChatGPT subscription through the Codex CLI, so nothing is billed per call.' : 'Runs on your Claude subscription through Claude Code, so nothing is billed per call. The estimate is what the same tokens would cost on the API.' }, (gpt ? 'ChatGPT plan' : 'Claude plan') + (est ? ` · ≈$${est.toFixed(3)} API-equivalent` : ''))
    : h('span', { class: 'cost', 'data-tip': gpt ? 'Billed to your OpenAI API key' : 'Billed to your Anthropic API key' }, `$${usd.toFixed(4)}`);
  const parts = [`${steps} step${steps === 1 ? '' : 's'}`, `${fmtK(pin)} in`, `${fmtK(out)} out`];
  if (cache) parts.push(`${fmtK(cache)} cached`);
  if (t) parts.push(fmtDur(t));
  const foot = h('div', { class: 'stats' }, copy, cost, ...parts.map(p => h('span', {}, p)));
  L.foot?.remove(); L.el.append(foot); L.foot = foot;
}

// ---------------------------------------------------------------- router, review, fix and lesson cards
function routerCard(m) {
  const d = m.decision || {}, info = m.info || {};
  const picked = new Set(d.tools || []);
  const exposed = m.exposed || [];
  const chips = [
    h('span', { class: 'rchip' }, d.complexity || '?'),
    h('span', { class: 'rchip' + (m.think ? ' on' : ''), 'data-tip': 'Whether the local model thinks step by step before answering' }, m.think ? 'thinking on' : 'thinking off'),
    h('span', { class: 'rchip' + (m.gpt_review ? ' on' : ''), 'data-tip': 'Whether ChatGPT (GPT-6 Astra) reviews the result first' }, m.gpt_review ? 'ChatGPT review' : 'no ChatGPT'),
    h('span', { class: 'rchip' + (m.review ? ' on' : ''), 'data-tip': 'Whether Claude Fable 5.1 reviews the result' }, m.review ? 'Claude review' : 'no Claude'),
    d.vision ? h('span', { class: 'rchip on' }, 'vision') : null,
    h('span', { class: 'rchip', 'data-tip': 'Tools sent to the local model out of all enabled tools. It can load more itself with load_tools.' }, `${exposed.length} of ${m.all_tools ?? '?'} tools`),
    m.saved_tokens ? h('span', { class: 'rchip save', 'data-tip': 'Tool-schema tokens the local model did not have to read this turn' }, `−${fmtK(m.saved_tokens)} tokens`) : null,
    m.reused ? h('span', { class: 'rchip', 'data-tip': 'Forever-loop rounds reuse the first decision' }, 'reused') : null,
    m.learned ? h('span', { class: 'rchip learn', 'data-tip': 'Saved to your preferences. Every model gets it from now on. Edit or remove it in Settings → Memory.' }, 'learned a preference') : null,
  ];
  const tools = exposed.length ? h('div', { class: 'chiprow' }, ...exposed.map(n => h('span', { class: 'rchip tool' + (picked.has(n) ? ' on' : ''), 'data-tool': n }, n))) : null;
  const plan = (d.plan || []).length ? h('ol', {}, ...d.plan.map(p => h('li', {}, p))) : null;
  const skills = (d.skills || []).length ? h('div', { class: 'chiprow' }, h('span', { class: 'rchip' }, 'skills'), ...d.skills.map(s => h('span', { class: 'rchip on' }, s))) : null;
  const facts = [info.model, info.threads ? `${info.threads} threads` : null, info.prompt_n ? `read ${fmtNum(info.prompt_n)} tokens (${fmtNum(info.cached_n || 0)} cached)` : null,
    info.pp ? `${Math.round(info.pp)} tok/s read` : null, info.tg ? `${Math.round(info.tg)} tok/s write` : null].filter(Boolean).join(' · ');
  return h('details', { class: 'router-card' },
    h('summary', {}, h('span', { class: 'orb' }), h('b', {}, 'Router'), h('span', { class: 'intent' }, d.intent || '(no intent)'),
      h('span', { class: 'rchip' }, m.reused ? 'reused' : info.ms ? `${Math.round(info.ms)} ms` : '')),
    h('div', { class: 'rbody' }, h('div', { class: 'chiprow' }, ...chips), tools, plan, skills,
      m.learned ? h('div', { class: 'learned' }, h('b', {}, 'Remembered: '), m.learned) : null,
      facts ? h('small', { class: 'muted' }, facts) : null));
}
function routerPending() {
  return h('details', { class: 'router-card thinking' },
    h('summary', {}, h('span', { class: 'orb live' }), h('b', {}, 'Router'), h('span', { class: 'intent' }, 'Choosing tools and thinking mode…')));
}

const VERDICT_TXT = { ok: 'Looks good', minor: 'Minor fixes', major: 'Major problems' };
function reviewCard(m) {
  const v = String(m.verdict || '').split(' ')[0];
  const issues = (m.issues || []).length ? h('ul', { class: 'issues' }, ...m.issues.map(i => h('li', {},
    h('span', { class: 'sev ' + (i.severity || 'minor') }, i.severity || 'issue'),
    h('div', {}, i.where ? h('span', { class: 'where' }, i.where + '  ') : null, i.problem || ''),
    i.fix ? h('div', { class: 'fix' }, 'Fix: ' + i.fix) : null))) : null;
  let plan = null;
  const gpt = m.pass === 'chatgpt';
  const who = m.model || (gpt ? 'GPT-6 Astra' : 'Claude Fable 5.1');
  const fixer = m.fixer || (gpt ? 'GPT-6.1 Sol' : 'Claude Opus 5.5');
  if (m.plan) { const md = h('div', { class: 'md' }); renderMd(md, m.plan); plan = h('details', { open: v === 'major' }, h('summary', {}, v === 'major' ? `${who}'s full re-plan` : `${who}'s plan`), md); }
  const better = m.improved_prompt ? h('details', {}, h('summary', {}, 'Improved prompt'), h('div', { class: 'think-body' }, m.improved_prompt)) : null;
  const esc = v === 'major' ? `${fixer} takes over with this plan.` : m.escalated ? `Still not fixed after the local retries: ${fixer} takes over.`
    : v === 'minor' ? 'Sent back to your local model to fix.' : null;
  return h('div', { class: 'review-card' + (gpt ? ' pass-chatgpt' : '') },
    h('div', { class: 'rv-head' }, h('span', { class: 'orb' }), h('b', {}, who), h('span', { class: 'muted' }, `${gpt ? 'ChatGPT' : 'Claude'} review · round ${m.round || 1}`),
      h('span', { class: 'grow' }), h('span', { class: 'verdict ' + v }, VERDICT_TXT[v] || m.verdict)),
    m.summary ? h('div', {}, m.summary) : null, issues, plan, better,
    (m.files || []).length ? h('small', { class: 'muted' }, 'Files it checked: ' + m.files.join(', ')) : null,
    esc ? h('div', { class: 'esc' }, esc) : null);
}
function fixCard(m) {
  const round = (String(m.content || '').match(/round (\d+)/) || [])[1];
  return h('div', { class: 'fix-req' + (m.from === 'astra' ? ' pass-chatgpt' : '') }, h('b', {}, `${LANE[m.from]?.label || 'Reviewer'} → local model`), ` · fix request${round ? ' (round ' + round + ')' : ''}`,
    h('details', {}, h('summary', { class: 'muted', style: 'cursor:pointer;font-size:12px' }, 'What it asked for'), h('div', { class: 'think-body', style: 'padding:6px 0 0' }, m.content)));
}
function lessonCard(m) {
  return h('div', { class: 'lesson-card' }, h('div', { class: 'lh' }, h('span', { class: 'ico', html: I.bulb }), 'Your local model learned'),
    h('ul', {}, ...(m.lessons || []).map(l => h('li', {}, l.text || l))),
    h('small', { class: 'muted' }, (m.from?.length ? `From ${m.from.join(' and ')}'s review. ` : '') + 'Saved to memory as lessons, so every later chat and model sees them' + (S.settings?.share_lessons_as_training ? ', and to data\\training\\corrections.jsonl' : '') + '.'));
}

// ---------------------------------------------------------------- context meter, compaction, memory count
function lastLocalStats(msgs) {
  return [...msgs].reverse().find(m => m.role === 'assistant' && (m.lane || 'local') === 'local' && m.stats?.prompt_tokens)?.stats;
}
function updateCtxMeter() {
  const msgs = S.chat?.messages || [];
  const st = lastLocalStats(msgs);
  const ctx = S.st?.engine.ctx || 0;
  const used = st ? st.prompt_tokens + (st.completion_tokens || 0) : 0;
  const pct = ctx ? Math.min(100, used / ctx * 100) : 0;
  $('#ctxMeter i').style.width = pct + '%';
  $('#ctxMeter span').textContent = ctx ? `${fmtK(used)} / ${fmtCtx(ctx)}` : '';
  $('#ctxMeter').classList.toggle('hidden', !ctx);
  $('#ctxMeter').title = 'Local model context used by this chat';
  const cb = $('#compactBtn');
  cb.classList.toggle('hidden', !msgs.some(m => m.role === 'user'));
  cb.disabled = S.streaming || S.compacting;
  cb.classList.toggle('hot', pct >= 60);
  return pct;
}
function chatPct() {
  const st = lastLocalStats(S.chat?.messages || []);
  const ctx = S.st?.engine.ctx || 0;
  return st && ctx ? (st.prompt_tokens + (st.completion_tokens || 0)) / ctx : 0;
}

function adoptChat(c, list = true) {
  S.chat = c;
  if (list && !S.chats.find(x => x.id === c.id)) S.chats.unshift({ id: c.id, title: c.title, updated: Date.now() / 1000, continued: true });
  if (S.loop.active) S.loop.chatId = c.id;
  $('#chatTitle').textContent = c.title || '';
  renderMessages(); renderChatList();
}

async function compactChat(auto = false) {
  const chat = S.chat;
  if (!chat || S.streaming || S.compacting || !chat.messages.length) return;
  if (S.st?.engine.status !== 'ready') return toast('Load a model first: compacting uses it to write the summary.', true);
  S.compacting = true; updateCtxMeter();
  await saveChat();
  const note = h('div', { class: 'notice compacting' }, h('span', { class: 'spinner sm' }),
    auto ? 'Context is getting full, so this chat is being saved to memory. A fresh chat will continue from it…'
      : 'Saving this chat to memory and starting a fresh chat that continues from it…');
  $('#messages').append(note); scrollBottom(true);
  try {
    const r = await api(`/api/chats/${chat.id}/compact`, { method: 'POST' });
    chat.compacted_into = r.new_chat.id;
    if (S.chat === chat) adoptChat(r.new_chat);
    toast(r.facts_added ? `Compacted. ${r.facts_added} new thing${r.facts_added > 1 ? 's' : ''} saved to memory.` : 'Compacted. Continuing in a fresh chat.');
    refreshMemCount();
  } catch (e) { note.remove(); toast('Compacting failed: ' + e.message, true); }
  S.compacting = false; updateCtxMeter();
}

async function refreshMemCount() {
  try { const m = await api('/api/memory'); const n = m.facts.length; $('#memCount').textContent = n ? String(n) : ''; } catch { }
}

let stickBottom = true;
$('#scroller').addEventListener('scroll', () => { const s = $('#scroller'); stickBottom = s.scrollHeight - s.scrollTop - s.clientHeight < 80; });
function scrollBottom(force) { if (force || stickBottom) { const s = $('#scroller'); s.scrollTop = s.scrollHeight; } }

// ---------------------------------------------------------------- composer
function bindChatUI() {
  const ta = $('#input');
  ta.addEventListener('input', autosize);
  ta.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!S.streaming) send(); }
    if (e.key === 'Escape' && S.streaming) stopGen();
  });
  ta.addEventListener('paste', onPaste);
  $('#sendBtn').onclick = () => S.streaming ? stopGen() : send();
  $('#attachBtn').onclick = () => $('#fileInput').click();
  $('#appBtn').onclick = pickApp;
  $('#fileInput').onchange = e => { addFiles([...e.target.files]); e.target.value = ''; };
  $('#newChat').onclick = newChat;
  $('#compactBtn').onclick = () => compactChat(false);
  $('#memoryBtn').onclick = () => openSettings('Memory');
  $('#labBtn').onclick = () => openLab();
  $('#settingsBtn').onclick = () => openSettings();
  $('#modelBtn').onclick = () => showLauncher();
  $('#sbCollapse').onclick = () => setSidebar(false);
  $('#sbExpand').onclick = () => setSidebar(true);
  // below 860px the sidebar floats over the chat: start with it closed and close it again after picking a chat
  if (PHONE.matches) setSidebar(false);
  PHONE.addEventListener?.('change', e => setSidebar(!e.matches));
  $('#modalClose').onclick = closeModal;
  $('#modal').addEventListener('mousedown', e => { if (e.target.id === 'modal') closeModal(); });
  $('#toolsToggle').onclick = () => saveSetting({ tools_enabled: !S.settings.tools_enabled });
  $('#thinkToggle').onclick = () => {
    const next = { auto: true, on: false, off: 'auto' }[thinkMode()];
    saveSetting({ thinking: next }).then(() => toast({ auto: 'Thinking: Auto (the router decides per request)', on: 'Thinking: always on', off: 'Thinking: off' }[thinkMode()]));
  };
  $('#reviewPill').onclick = () => {
    const next = { off: 'on', on: 'auto', auto: 'off' }[S.settings.review_mode || 'off'];
    saveSetting({ review_mode: next }).then(() => {
      if (next !== 'off' && !S.cloud?.backend) toast('Claude review is on, but Claude is not connected yet: Settings → Claude.', true, 6000);
      else toast({ on: S.settings.chatgpt_review_mode !== 'off' ? 'Claude Fable 5.1 reviews every reply, after ChatGPT' : 'Claude Fable 5.1 reviews every reply', auto: 'The router decides when Claude reviews', off: 'Claude review off' }[next]);
    });
  };
  $('#gptPill').onclick = () => {
    const next = { off: 'on', on: 'auto', auto: 'off' }[S.settings.chatgpt_review_mode || 'off'];
    saveSetting({ chatgpt_review_mode: next }).then(() => {
      if (next !== 'off' && !S.gpt?.backend) toast('ChatGPT review is on, but ChatGPT is not connected yet: Settings → ChatGPT.', true, 6000);
      else toast({ on: S.settings.review_mode !== 'off' ? 'GPT-6 Astra reviews every reply first, then Claude' : 'GPT-6 Astra reviews every reply', auto: 'The router decides when ChatGPT reviews', off: 'ChatGPT review off' }[next]);
    });
  };
  $('#loopPill').onclick = () => {
    if (S.loop.active) return stopLoop('Forever-loop stopped.');
    S.loop.armed = !S.loop.armed; renderToggles();
    if (S.loop.armed) { toast('Forever-loop armed: your next message repeats until you press Stop.'); askNotify(); }
  };
  $('#themeBtn').onclick = () => saveSetting({ theme: { auto: 'day', day: 'night', night: 'auto' }[S.settings.theme || 'auto'] }).then(applyTheme);
  $('#dashBtn').onclick = () => {
    if (NARROW.matches) { S.dashNarrow = !dashOn(); renderDashVisibility(); return; }   // a floating panel: this window only
    saveSetting({ dashboard: !S.settings.dashboard }).then(renderDashVisibility);
  };
  NARROW.addEventListener?.('change', () => { S.dashNarrow = undefined; renderDashVisibility(); });
  $$('.suggest button').forEach(b => b.onclick = () => { ta.value = b.dataset.s; autosize(); send(); });
  // drag & drop anywhere
  let dragN = 0;
  addEventListener('dragenter', e => { if ([...e.dataTransfer.types].includes('Files')) { dragN++; $('#dropZone').classList.remove('hidden'); } });
  addEventListener('dragleave', () => { if (--dragN <= 0) { dragN = 0; $('#dropZone').classList.add('hidden'); } });
  addEventListener('dragover', e => e.preventDefault());
  addEventListener('drop', e => { e.preventDefault(); dragN = 0; $('#dropZone').classList.add('hidden'); if (e.dataTransfer.files.length) { if ($('#chat').classList.contains('hidden')) return; addFiles([...e.dataTransfer.files]); } });
  addEventListener('keydown', e => { if (e.key === 'Escape') { closeModal(); $('#repoPanel').classList.add('hidden'); } });
  renderSend();
}

async function saveSetting(patch) {
  try { S.settings = await api('/api/settings', { method: 'PUT', json: patch }); } catch (e) { toast(e.message, true); }
  renderToggles();
  return S.settings;
}
const thinkMode = () => { const v = S.settings?.thinking; return v === true ? 'on' : v === false ? 'off' : 'auto'; };

function renderToggles() {
  if (!S.settings) return;
  const s = S.settings;
  const set = (el, icon, label, state, word) => {
    el.innerHTML = I[icon] + `<span>${label}</span>` + (word ? `<b>${word}</b>` : '');
    el.classList.toggle('on', state === 'on'); el.classList.toggle('auto', state === 'auto');
  };
  set($('#toolsToggle'), 'tools', 'Tools', s.tools_enabled ? 'on' : 'off');
  const tm = thinkMode();
  set($('#thinkToggle'), 'brain', 'Think', tm, { auto: 'Auto', on: 'On', off: 'Off' }[tm]);
  const rm = s.review_mode || 'off';
  const gm = s.chatgpt_review_mode || 'off';
  set($('#gptPill'), 'orbit', 'ChatGPT', gm, { auto: 'Auto', on: 'Astra', off: '' }[gm]);
  set($('#reviewPill'), 'review', 'Claude', rm, { auto: 'Auto', on: 'Fable', off: '' }[rm]);
  set($('#loopPill'), 'loop', 'Loop', S.loop.active || S.loop.armed ? 'on' : 'off', S.loop.active ? `#${S.loop.iteration}` : S.loop.armed ? 'armed' : '');
  $('#appBtn').innerHTML = I.app + '<span>App</span>';
  $('#hint').textContent = S.loop.armed && !S.loop.active ? 'Next message repeats until you stop it' : s.tools_enabled ? 'Agent mode' : '';
}

function autosize() { const ta = $('#input'); ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, innerHeight * 0.4) + 'px'; renderSend(); }
function renderSend() {
  const b = $('#sendBtn');
  b.innerHTML = S.streaming ? I.stop : I.send; b.classList.toggle('stop', S.streaming);
  const has = $('#input').value.trim() || S.pending.pastes.length || S.pending.atts.length;
  b.disabled = !S.streaming && (!has || S.pending.atts.some(a => a.loading));
  b.title = S.streaming ? 'Stop (Esc)' : 'Send (Enter)';
}
function insertAtCursor(text) {
  const ta = $('#input'); const s = ta.selectionStart, e = ta.selectionEnd;
  ta.value = ta.value.slice(0, s) + text + ta.value.slice(e); ta.selectionStart = ta.selectionEnd = s + text.length; autosize();
}

function onPaste(e) {
  const cd = e.clipboardData; if (!cd) return;
  const files = [...cd.files];
  if (files.length) { e.preventDefault(); addFiles(files.map((f, i) => f.name && f.name !== 'image.png' ? f : new File([f], `pasted-${Date.now()}-${i}.${(f.type.split('/')[1] || 'png')}`, { type: f.type }))); return; }
  const text = cd.getData('text/plain');
  if (text && (text.length > PASTE_CHARS || text.split('\n').length > PASTE_LINES)) {
    e.preventDefault();
    S.pending.pastes.push({ name: `Pasted text ${S.pending.pastes.length + 1}`, text });
    renderChips();
  }
}

function addFiles(files) {
  for (const f of files) {
    const a = { localId: uid(), name: f.name, size: f.size, loading: true, kind: f.type.startsWith('image/') ? 'image' : 'file', preview: f.type.startsWith('image/') ? URL.createObjectURL(f) : null };
    S.pending.atts.push(a); renderChips();
    fetch('/api/upload?name=' + encodeURIComponent(f.name), { method: 'POST', body: f })
      .then(async r => { if (!r.ok) throw new Error((await r.json()).detail || r.statusText); return r.json(); })
      .then(meta => { Object.assign(a, meta, { loading: false }); renderChips(); })
      .catch(err => { S.pending.atts = S.pending.atts.filter(x => x !== a); renderChips(); toast(`${f.name}: ${err.message}`, true); });
  }
}

async function pickApp() {
  let ws = [];
  try { ws = await api('/api/windows'); } catch (e) { return toast(e.message, true); }
  if (!ws.length) return toast('No app windows found (app control works on Windows).', true);
  popMenu($('#appBtn'), ws.slice(0, 30).map(w => [`${w.title.slice(0, 60)}  ·  ${w.app}${w.minimized ? ' (minimized)' : ''}`,
    () => { S.pending.app = { id: w.id, title: w.title, app: w.app }; renderChips(); if (!S.settings.tools_enabled) toast('Turn on Tools so it can use the app.', true); $('#input').focus(); }]));
}

function renderChips() {
  const box = $('#chips'); box.innerHTML = '';
  if (S.pending.app) {
    const a = S.pending.app;
    box.append(h('div', { class: 'chip app-chip', title: a.title }, h('div', { class: 'ci', html: I.app }),
      h('div', { class: 'ct' }, h('b', {}, a.title), h('small', {}, `${a.app} · Aero will look inside and control it`)),
      h('button', { class: 'x', onclick: e => { e.stopPropagation(); S.pending.app = null; renderChips(); } }, '×')));
  }
  for (const p of S.pending.pastes) box.append(pasteChip(p, () => viewPaste(p, true), () => { S.pending.pastes.splice(S.pending.pastes.indexOf(p), 1); renderChips(); }));
  for (const a of S.pending.atts) {
    const rm = () => { S.pending.atts = S.pending.atts.filter(x => x !== a); renderChips(); };
    if (a.kind === 'image') {
      const c = h('div', { class: 'chip' + (a.loading ? ' loading' : ''), onclick: () => a.preview && viewImage(a.preview, a.name) },
        h('div', { class: 'ci' }, a.preview ? h('img', { src: a.preview }) : ''),
        h('div', { class: 'ct' }, h('b', {}, a.name), h('small', {}, a.loading ? 'uploading…' : (S.st?.engine.vision ? fmtBytes(a.size) : 'model has no vision'))),
        h('button', { class: 'x', onclick: e => { e.stopPropagation(); rm(); } }, '×'));
      box.append(c);
    } else {
      const c = fileChip(a, () => !a.loading && viewFile(a), rm);
      if (a.loading) { c.classList.add('loading'); $('small', c).textContent = 'reading…'; }
      else if (a.note) $('small', c).textContent += ' · ' + a.note;
      box.append(c);
    }
  }
  renderSend();
}

// ---------------------------------------------------------------- sending + streaming
async function send() {
  if (S.st?.engine.status !== 'ready') { toast('Load a model first.', true); return showLauncher(); }
  if (S.compacting) return toast('Still compacting; one moment.');
  const ta = $('#input'); const text = ta.value.trim();
  if (!text && !S.pending.pastes.length && !S.pending.atts.length) return;
  if (!text && S.pending.app) return toast('Say what to do in that app.');
  if (S.pending.atts.some(a => a.loading)) return toast('Still reading attachments…');
  if (S.loop.active) stopLoop('Forever-loop stopped because you sent a new message.');
  const msg = { role: 'user', content: text, ts: Date.now() / 1000 };
  if (S.pending.pastes.length) msg.pastes = S.pending.pastes.map(p => ({ name: p.name, text: p.text }));
  if (S.pending.atts.length) msg.attachments = S.pending.atts.map(a => ({ id: a.id, name: a.name, kind: a.kind, size: a.size, chars: a.chars }));
  if (S.pending.app) msg.target_app = S.pending.app;
  if (S.loop.armed) {
    if (!text) return toast('A forever-loop needs a written prompt to repeat.', true);
    Object.assign(S.loop, { armed: false, active: true, iteration: 1, started: Date.now() / 1000, base: { content: text, target_app: msg.target_app || null }, chatId: S.chat.id, finishing: false });
    msg.loop = { iteration: 1, started: S.loop.started };
    renderLoop();
  }
  S.pending = { pastes: [], atts: [], app: null }; renderChips();
  ta.value = ''; autosize(); renderToggles();
  S.chat.messages.push(msg);
  renderMessages();
  await saveChat();
  const firstTurn = S.chat.messages.filter(m => m.role === 'user').length === 1;
  const run = runAgent(msg.loop);
  if (firstTurn && !S.chat.title) makeTitle(text || msg.pastes?.[0]?.text || msg.attachments?.[0]?.name || 'Chat');
  S.lastRun = await run;
  if (S.loop.active) await loopContinue();
}

async function makeTitle(text) {
  const chat = S.chat;
  try {
    const r = await api('/api/title', { method: 'POST', json: { text } });
    chat.title = r.title || text.slice(0, 48);
  } catch { chat.title = text.slice(0, 48); }
  if (S.chat === chat) $('#chatTitle').textContent = chat.title;
  if (S.streaming && S.chat === chat) { const ex = S.chats.find(c => c.id === chat.id); if (ex) ex.title = chat.title; renderChatList(); return; }
  await api('/api/chats/' + chat.id, { method: 'PUT', json: chat }).catch(() => { });
  const ex = S.chats.find(c => c.id === chat.id); if (ex) ex.title = chat.title; renderChatList();
}

/** Stream one turn: router → local model → (Fable review → local fix | Opus) → lessons. Returns {stopped, error}. */
async function runAgent(loop) {
  let chat = S.chat;
  S.streaming = true; S.userStopped = false; renderSend(); updateCtxMeter(); syncBusy();
  S.abort = new AbortController();
  const box = $('#messages');
  let ctx = box.lastElementChild?.classList.contains('turn') ? turnCtx(box.lastElementChild) : newTurn(box);
  let cur = null;        // the assistant message streaming right now
  let raf = 0, failed = null;
  let routerEl = null, lessonNote = null;
  const flush = () => {
    raf = 0; if (!cur) return;
    if (cur.reasoning && !cur.think) {
      cur.thinkBody = h('div', { class: 'think-body' });
      cur.think = h('details', { class: 'think', open: true }, h('summary', {}, cur.cloud ? 'Thinking…' : 'Thinking…'), cur.thinkBody);
      cur.L.el.insertBefore(cur.think, cur.md);
    }
    if (cur.think) { cur.thinkBody.textContent = cur.reasoning; cur.thinkBody.scrollTop = 1e9; }
    if (cur.content) { renderMd(cur.md, cur.content, true); cur.md.classList.add('cursor'); }
    scrollBottom();
  };
  const sched = () => { if (!raf) raf = requestAnimationFrame(flush); };
  const pendingLabel = h('div', { class: 'notice' });
  const setLive = lane => {
    S.activeLane = lane;
    $$('.orb.live', ctx.el).forEach(o => o.classList.remove('live'));
    if (lane && ctx.lane?.name === lane) ctx.lane.orb.classList.add('live');
  };
  const push = m => chat.messages.push(m);
  const opts = { think: thinkMode(), review: S.settings.review_mode || 'off', chatgpt_review: S.settings.chatgpt_review_mode || 'off', loop: loop || null };

  try {
    await sseFetch('/api/chat', { chat_id: chat.id, messages: chat.messages, carry: chat.carry || null, title: chat.title || '', opts }, ev => {
      switch (ev.t) {
        case 'router_start': routerEl = routerPending(); ctx.el.append(routerEl); S.activeLane = 'router'; scrollBottom(); break;
        case 'router': {
          push(ev.message); closeLane(ctx);
          const card = routerCard(ev.message);
          if (routerEl) routerEl.replaceWith(card); else ctx.el.append(card);
          ctx.router = card; routerEl = null; scrollBottom(); break;
        }
        case 'router_error': {
          const n = { role: 'notice', lane: 'router', content: 'Router skipped: ' + ev.error };
          push(n); const el = appendNotice(ctx, n);
          if (routerEl) { routerEl.replaceWith(el); routerEl = null; }
          break;
        }
        case 'exposure': {
          const chip = ctx.router && $$('.rchip', ctx.router).find(c => / of .* tools$/.test(c.textContent));
          if (chip) chip.textContent = chip.textContent.replace(/^\d+/, ev.exposed.length);
          break;
        }
        case 'lane': {
          if (ev.phase === 'lesson') {
            closeLane(ctx);
            lessonNote = h('div', { class: 'notice' }, h('span', { class: 'spinner sm' }), ' Your local model is turning the review into lessons…');
            ctx.el.append(lessonNote); scrollBottom(); break;
          }
          if (ev.lane === 'local' && ev.phase === 'fix') ctx.afterFix = true;
          const L = ensureLane(ctx, ev.lane, ev.model, ev.phase, ev.round, ev.via);
          if (ev.model) L.nameEl.textContent = ev.model;
          setLive(ev.lane); scrollBottom(); break;
        }
        case 'compacting': pendingLabel.textContent = 'Context is nearly full: saving the older part of this chat to memory…'; ctx.el.append(pendingLabel); scrollBottom(); break;
        case 'compacted': {
          pendingLabel.remove();
          const old = chat;
          old.compacted_into = ev.new_chat_id;
          api('/api/chats/' + old.id, { method: 'PUT', json: old }).catch(() => { });
          chat = { id: ev.new_chat_id, title: (old.title || 'Chat').replace(/ · part (\d+)$/, (m, n) => ` · part ${+n + 1}`) + (/ · part \d+$/.test(old.title || '') ? '' : ' · part 2'),
            messages: ev.keep, carry: ev.carry, created: Date.now() / 1000 };
          if (S.chat === old) adoptChat(chat); else S.chats.unshift({ id: chat.id, title: chat.title, updated: Date.now() / 1000 });
          api('/api/chats/' + chat.id, { method: 'PUT', json: chat }).catch(() => { });
          const last = $('#messages').lastElementChild;
          ctx = last?.classList.contains('turn') ? turnCtx(last) : newTurn($('#messages'));
          ctx.el.append(h('div', { class: 'notice' }, `Context was nearly full, so the older part was saved to memory${ev.facts_added ? ` (${ev.facts_added} new facts)` : ''}. Continuing here.`));
          refreshMemCount(); break;
        }
        case 'assistant_start': {
          const lane = ev.lane || 'local';
          if (cur && !cur.content && !cur.reasoning) { cur.md.remove(); cur.think?.remove(); }
          const L = ensureLane(ctx, lane, ev.model);
          setLive(lane);
          cur = { L, md: h('div', { class: 'md cursor' }), content: '', reasoning: '', t0: Date.now(), cloud: isCloudLane(lane) };
          L.el.append(cur.md); L.foot && L.el.append(L.foot); scrollBottom(); break;
        }
        case 'reasoning': if (cur) { cur.reasoning += ev.d; sched(); } break;
        case 'content':
          if (!cur) break;
          cur.content += ev.d;
          if (cur.think && cur.think.open && !cur.thinkClosed) { cur.think.open = false; cur.thinkClosed = true; $('summary', cur.think).textContent = `Thought for ${((Date.now() - cur.t0) / 1000).toFixed(1)}s`; }
          sched(); break;
        case 'tool_pending': pendingLabel.textContent = `Preparing ${toolInfo(ev.name).name}…`; (cur?.L.el || ctx.el).append(pendingLabel); scrollBottom(); break;
        case 'assistant_done': {
          pendingLabel.remove();
          const m = ev.message;
          if (raf) { cancelAnimationFrame(raf); raf = 0; }
          if (m.discard) { if (cur) { cur.md.remove(); cur.think?.remove(); } cur = null; break; }
          push(m);
          const L = cur?.L || ensureLane(ctx, m.lane, m.model);
          if (cur) {
            if (cur.think) { if (!cur.thinkClosed) $('summary', cur.think).textContent = cur.cloud ? 'Thinking (summary)' : 'Thought process'; cur.think.open = false; cur.thinkBody.textContent = m.reasoning || cur.reasoning; }
            else if (m.reasoning) L.el.insertBefore(thinkBlock(m.reasoning, cur.cloud), cur.md);
            cur.md.classList.remove('cursor');
            if (m.content) renderMd(cur.md, m.content); else cur.md.remove();
          }
          if (m.model && L.name !== 'local') L.nameEl.textContent = m.model;
          for (const tc of m.tool_calls || []) {
            if (isHiddenCall(tc.function.name) || findCard(ctx.el, tc.id)) continue;
            L.el.append(toolCard(tc.id, tc.function.name, parseArgs(tc.function.arguments)));
          }
          L.msgs.push(m);
          if (isCloudLane(L.name)) laneFoot(L);
          else if (!(m.tool_calls || []).length) L.el.append(statsRow(m));
          cur = null; updateCtxMeter(); scrollBottom(); break;
        }
        case 'tool_start': {
          let card = findCard(ctx.el, ev.call_id);
          if (!card) {
            const L = ctx.lane && ctx.lane.name === (ev.lane || 'local') ? ctx.lane : ensureLane(ctx, ev.lane, null);
            card = toolCard(ev.call_id, ev.name, ev.args, ev.category, ev.label); L.el.append(card);
          }
          if (ev.label) $('.targ', card).textContent = ev.label;
          if (ev.needs_approval && !$('.approve', card)) {
            $('.tstat', card).className = 'tstat ask'; $('.tstat', card).textContent = 'needs approval';
            const ap = h('div', { class: 'approve' });
            const decide = d => {
              api('/api/approve', { method: 'POST', json: { call_id: ev.call_id, decision: d, chat_id: chat.id, category: ev.category } }).catch(() => { });
              ap.remove(); $('.tstat', card).className = 'tstat run'; $('.tstat', card).textContent = d === 'deny' ? 'denying…' : 'running…';
            };
            const detail = ev.args?.command || ev.args?.content || ev.args?.text || ev.args?.new_text || ev.args?.new_string || null;
            ap.append(...[
              h('div', { class: 'q' }, `Allow `, h('b', { 'data-tool': ev.name }, toolInfo(ev.name).name), ` ${ev.label ? '→ ' + ev.label : ''}?`,
                ev.lane && ev.lane !== 'local' ? h('small', { class: 'muted' }, ` (asked by ${LANE[ev.lane]?.label || ev.lane})`) : null),
              h('button', { class: 'btn sm', onclick: () => decide('allow') }, 'Allow'),
              ev.category ? h('button', { class: 'btn ghost sm', onclick: () => decide('allow_chat'), 'data-tip': `Auto-allow every "${ev.category}" action for the rest of this chat` }, 'Always in this chat') : null,
              h('button', { class: 'btn danger sm', onclick: () => decide('deny') }, 'Deny'),
              detail && String(detail).length > 60 ? h('pre', {}, String(detail).slice(0, 4000)) : null].filter(Boolean));
            card.append(ap); scrollBottom(true);
            notify('Aero needs your approval', `${toolInfo(ev.name).name} ${ev.label || ''}`);
          }
          break;
        }
        case 'tool_result': push(ev.message); attachToolResult(ctx, ev.message); scrollBottom(); break;
        case 'review': {
          push(ev.message); closeLane(ctx); S.activeLane = null;
          ctx.el.append(reviewCard(ev.message)); ctx.afterFix = false; scrollBottom(); break;
        }
        case 'fix_request': push(ev.message); closeLane(ctx); ctx.el.append(fixCard(ev.message)); ctx.afterFix = true; scrollBottom(); break;
        case 'lesson': {
          push(ev.message); lessonNote?.remove(); lessonNote = null;
          ctx.el.append(lessonCard(ev.message)); refreshMemCount(); scrollBottom(); break;
        }
        case 'cloud_usage': {
          if (ev.via === 'plan' && ev.usage?.est_usd) {
            const L = ctx.lane && isCloudLane(ctx.lane.name) ? ctx.lane : null;
            const m = L && L.msgs[L.msgs.length - 1];
            if (m) { m.stats = m.stats || {}; m.stats.est_usd = (m.stats.est_usd || 0) + ev.usage.est_usd; laneFoot(L); }
          }
          break;
        }
        case 'notice': {
          const n = { role: 'notice', content: ev.text, lane: ev.lane, level: ev.level };
          push(n); appendNotice(ctx, n); scrollBottom(); break;
        }
        case 'error': { failed = ev.error; const n = { role: 'notice', content: ev.error, error: true }; push(n); appendNotice(ctx, n); scrollBottom(); break; }
        case 'done': lessonNote?.remove(); break;
      }
    }, S.abort.signal);
  } catch (e) {
    if (e.name !== 'AbortError') { failed = e.message; const n = { role: 'notice', content: e.message, error: true }; push(n); appendNotice(ctx, n); }
  }
  if (raf) { cancelAnimationFrame(raf); raf = 0; }
  if (cur) {
    cur.md.classList.remove('cursor');
    if (cur.content || cur.reasoning) {
      renderMd(cur.md, cur.content);
      const m = { role: 'assistant', content: cur.content, reasoning: cur.reasoning || undefined, lane: cur.L.name, stats: { finish: 'stopped' } };
      push(m); cur.L.msgs.push(m);
    } else cur.md.remove();
  }
  pendingLabel.remove(); lessonNote?.remove(); routerEl?.remove();
  const stopped = S.userStopped;
  $$('.tstat.run, .tstat.ask', ctx.el).forEach(s => { s.className = 'tstat err'; s.textContent = 'stopped'; });
  $$('.approve', ctx.el).forEach(a => a.remove());
  if (ctx.lane) finishLane(ctx.lane, stopped);
  for (const L of ctx.lanes) if (L !== ctx.lane) finishLane(L);
  ctx.lane = null;
  S.streaming = false; S.abort = null; S.activeLane = null; renderSend(); updateCtxMeter(); syncBusy();
  if (S.chat === chat) await saveChat(); else await api('/api/chats/' + chat.id, { method: 'PUT', json: chat }).catch(() => { });
  const at = S.settings?.auto_compact_at || 0;
  if (at > 0 && S.chat === chat && chatPct() >= at) await compactChat(true);
  if (!stopped) notify(S.loop.active ? `Aero finished loop round ${S.loop.iteration}` : 'Aero finished', S.chat?.title || 'Reply ready');
  return { stopped, error: failed };
}

/** A desktop notification while the window is in the background (only if the user allowed them). */
function notify(title, body) {
  try { if (document.hidden && 'Notification' in window && Notification.permission === 'granted') new Notification(title, { body, icon: 'icons/aero-192.png' }); } catch { }
}
function askNotify() { try { if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission(); } catch { } }

function stopGen() {
  if (!S.chat) return;
  S.userStopped = true;
  api('/api/stop', { method: 'POST', json: { chat_id: S.chat.id } }).catch(() => { });
  setTimeout(() => S.abort?.abort(), 1500);   // hard-abort if the server doesn't close the stream
}

// ---------------------------------------------------------------- forever-loop
async function loopContinue() {
  while (S.loop.active) {
    const max = +S.settings.loop_max || 0;
    if (S.lastRun?.error) return stopLoop('Forever-loop stopped: the last round ended with an error.', true);
    if (S.lastRun?.stopped) return stopLoop('Forever-loop stopped.');
    if (S.loop.finishing) return stopLoop(`Forever-loop finished after round ${S.loop.iteration}.`);
    if (max && S.loop.iteration >= max) return stopLoop(`Forever-loop done: ${max} rounds.`);
    if (S.chat?.id !== S.loop.chatId) return stopLoop('Forever-loop stopped because you switched chats.');
    const go = await loopWait(Math.max(0, +S.settings.loop_delay_s || 0));
    if (!go || !S.loop.active) return;
    if (S.chat?.id !== S.loop.chatId) return stopLoop('Forever-loop stopped because you switched chats.');
    if (S.streaming || S.compacting) return stopLoop('Forever-loop stopped.');
    S.loop.iteration++;
    const b = S.loop.base;
    const msg = { role: 'user', content: b.content, ts: Date.now() / 1000, loop: { iteration: S.loop.iteration, started: S.loop.started } };
    if (b.target_app) msg.target_app = b.target_app;
    S.chat.messages.push(msg);
    renderMessages(); renderToggles(); renderLoop();
    await saveChat();
    S.lastRun = await runAgent(msg.loop);
  }
}
function loopWait(sec) {
  return new Promise(resolve => {
    S.loop.waitUntil = Date.now() + sec * 1000;
    const t = setInterval(() => {
      renderLoop();
      if (Date.now() >= S.loop.waitUntil) { clearInterval(t); S.loop.cancel = null; S.loop.waitUntil = 0; resolve(true); }
    }, 250);
    S.loop.cancel = () => { clearInterval(t); S.loop.waitUntil = 0; resolve(false); };
    renderLoop();
  });
}
function stopLoop(text, err = false) {
  const wasActive = S.loop.active;
  S.loop.active = false; S.loop.armed = false; S.loop.finishing = false;
  if (S.loop.cancel) { S.loop.cancel(); S.loop.cancel = null; }
  if (wasActive) api('/api/loop/stop', { method: 'POST' }).catch(() => { });
  renderLoop(); renderToggles(); renderChatList();
  if (text && wasActive) toast(text, err, 5000);
}
function renderLoop() {
  const bar = $('#loopBar'), L = S.loop;
  if (!L.active) { bar.classList.add('hidden'); bar.innerHTML = ''; return; }
  bar.classList.remove('hidden');
  const max = +S.settings?.loop_max || 0;
  const wait = L.waitUntil ? Math.max(0, Math.ceil((L.waitUntil - Date.now()) / 1000)) : 0;
  const status = L.finishing ? 'stopping after this round' : L.waitUntil ? `next round in ${wait}s` : 'running';
  const mins = Math.round((Date.now() / 1000 - L.started) / 60);
  bar.innerHTML = '';
  bar.append(...[h('span', { class: 'inf' }, '∞'),
    h('span', {}, h('b', {}, 'Forever-loop'), ` · round ${L.iteration}${max ? ' of ' + max : ''} · ${status}${mins >= 1 ? ` · ${mins} min` : ''}`),
    h('span', { class: 'grow' }),
    L.waitUntil ? h('button', { class: 'btn ghost sm', onclick: () => { S.loop.waitUntil = Date.now(); } }, 'Run now') : null,
    !L.finishing && !L.waitUntil ? h('button', { class: 'btn ghost sm', onclick: () => { S.loop.finishing = true; renderLoop(); } }, 'Finish this round') : null,
    h('button', { class: 'btn danger sm', onclick: () => { if (S.streaming) stopGen(); stopLoop('Forever-loop stopped.'); } }, 'Stop')].filter(Boolean));
}

// ---------------------------------------------------------------- theme, sky, tooltips
const darkQuery = matchMedia('(prefers-color-scheme: dark)');
function applyTheme() {
  const mode = S.settings?.theme || 'auto';
  const dark = mode === 'night' || (mode === 'auto' && darkQuery.matches);
  document.documentElement.dataset.theme = dark ? 'night' : 'day';
  const b = $('#themeBtn');
  b.innerHTML = I[{ auto: 'auto', day: 'sun', night: 'moon' }[mode]];
  b.title = { auto: 'Theme: follows Windows (click for day)', day: 'Theme: day (click for night)', night: 'Theme: night (click for auto)' }[mode];
  Scene.setMode(sceneMode());
  document.body.classList.toggle('no-pond', S.settings?.pond === false);
  document.body.classList.toggle('no-transparency', S.settings?.transparency === false);
  syncBusy();
}
/** The scene governor: freeze scenery motion while a model generates, tunes, loads or benchmarks. */
function syncBusy() {
  const busy = !S.sceneTest && !!(S.streaming || S.labBusy || S.st?.busy);    // the Lab's scenery test needs it animating
  Scene.setBusy(busy && S.settings?.scene_pause_busy !== false);
}
// "full" = animated scenery, "still" = same scene without motion, "off" = plain sky (older settings: motion=false means still)
function sceneMode() {
  const s = S.settings || {};
  return s.scenery || (s.motion === false ? 'still' : 'full');
}
darkQuery.addEventListener('change', applyTheme);

function bindTips() {
  const tip = $('#tip');
  let cur = null;
  const hide = () => { cur = null; tip.classList.remove('show'); };
  document.addEventListener('mouseover', e => {
    const el = e.target.closest?.('[data-tool],[data-tip]');
    if (el === cur) return;
    if (!el) return hide();
    cur = el;
    if (el.dataset.tool) {
      const t = toolInfo(el.dataset.tool);
      tip.innerHTML = `<b>${esc(t.name)}</b>${esc(t.desc)}${t.cat ? `<small>${esc(t.cat)}</small>` : ''}`;
    } else tip.textContent = el.dataset.tip;
    const r = el.getBoundingClientRect();
    tip.style.left = '0px'; tip.style.top = '0px'; tip.classList.add('show');
    const w = tip.offsetWidth, ht = tip.offsetHeight;
    const left = Math.max(8, Math.min(r.left, innerWidth - w - 8));
    const top = r.bottom + 8 + ht < innerHeight ? r.bottom + 8 : Math.max(8, r.top - ht - 8);
    tip.style.left = left + 'px'; tip.style.top = top + 'px';
  });
  document.addEventListener('scroll', hide, true);
  document.addEventListener('mousedown', hide);
}

async function loadToolDescriptions() {
  try {
    const t = await api('/api/tools');
    S.toolCats = t.categories || {};
    for (const [cat, list] of Object.entries(t.tools || {}))
      for (const x of list) S.toolDesc[x.name] = { d: x.description || '', cat };
  } catch { }
}

const brief = s => { s = String(s || '').replace(/\s+/g, ' ').trim(); const m = s.match(/^(.{20,240}?[.!?])(\s|$)/); return m ? m[1] : s.length > 240 ? s.slice(0, 237) + '…' : s; };
/** Name, one-line description and category for any tool name the models use. */
function toolInfo(raw) {
  let name = String(raw || ''), via = '';
  if (name.startsWith('mcp__aero__')) { name = name.slice(14); via = 'Aero tool, used by Claude through Claude Code'; }
  const t = S.toolDesc[name];
  if (t) return { name, desc: brief(t.d), cat: via || S.toolCats[t.cat] || t.cat, catKey: t.cat };
  const m = name.match(/^mcp__(.+?)__(.+)$/);
  if (m) {
    const server = m[1].replace(/^plugin_/, '').replace(/_/g, ' ');
    return { name: m[2], desc: `"${m[2]}" from the ${server} MCP server, used by Claude through your Claude Code setup.`, cat: 'Claude Code MCP / plugin tool', catKey: 'mcp' };
  }
  if (name.startsWith('mcp_')) return { name, desc: 'A tool from one of your MCP servers.', cat: S.toolCats.mcp || 'MCP servers', catKey: 'mcp' };
  return { name, desc: 'No description available.', cat: '', catKey: '' };
}

// ---------------------------------------------------------------- live dashboard
// Below 1180px the dashboard floats over the chat, so a narrow window starts with it closed; the button opens it for
// this window without changing the saved setting.
const NARROW = matchMedia('(max-width: 1180px)');
function dashOn() {
  const saved = S.settings ? S.settings.dashboard !== false : true;
  return NARROW.matches ? (S.dashNarrow ?? false) : saved;
}
function renderDashVisibility() {
  const on = dashOn();
  $('#dash').classList.toggle('closed', !on);
  $('#dashBtn').classList.toggle('on', on);
  if (on && !D.built) buildDash();
}

const D = { built: false };
function meterEl(label, cls = '') {
  const i = h('i'), em = h('em'), bar = h('div', { class: 'bar ' + cls }, i);
  return { el: h('div', { class: 'meter' }, h('span', {}, label), bar, em),
    set(p, txt, c) { i.style.width = Math.max(0, Math.min(100, p || 0)).toFixed(1) + '%'; em.textContent = txt; if (c !== undefined) bar.className = 'bar ' + c; } };
}
function kvsEl(rows) {
  const v = {};
  const el = h('div', { class: 'kvs' }, ...rows.flatMap(([k, label, tip]) => [h('span', tip ? { 'data-tip': tip } : {}, label), v[k] = h('b', {}, '–')]));
  return { el, set(k, val) { if (v[k]) v[k].textContent = val ?? '–'; } };
}
function mrowEl(lane, name) {
  const orb = h('span', { class: 'orb' }), b = h('b', {}, name), st = h('span', { class: 'st' }), sm = h('small');
  const el = h('div', { class: 'mrow', style: `--lc:var(--${lane})` }, orb, b, st, sm);
  return { el, set({ title, state, cls, sub, live }) {
    if (title != null) b.textContent = title; st.textContent = state; st.className = 'st ' + (cls || ''); sm.textContent = sub || '';
    orb.classList.toggle('live', !!live); } };
}
function dcard(title, ...kids) { const r = h('span', { class: 'r' }); const c = h('div', { class: 'dcard' }, h('h4', {}, title, r), ...kids); c.r = r; return c; }

function buildDash() {
  const dash = $('#dash'); dash.innerHTML = ''; D.built = true;
  D.rows = { router: mrowEl('router', 'Router'), local: mrowEl('local', 'Local model'), astra: mrowEl('astra', 'GPT-6 Astra'), sol: mrowEl('sol', 'GPT-6.1 Sol'), fable: mrowEl('fable', 'Claude Fable 5.1'), opus: mrowEl('opus', 'Claude Opus 5.5') };
  D.cModels = dcard('Models', ...Object.values(D.rows).map(r => r.el));
  D.vram = meterEl('VRAM'); D.gload = meterEl('Load'); D.temp = meterEl('Temp'); D.power = meterEl('Power');
  D.gkv = kvsEl([['sm', 'Core clock'], ['mem', 'Memory clock'], ['fan', 'Fan']]);
  D.cGpu = dcard('GPU', D.vram.el, D.gload.el, D.temp.el, D.power.el, D.gkv.el);
  D.cores = h('div', { class: 'cores' });
  D.ram = meterEl('RAM');
  D.ckv = kvsEl([['main', 'Main model process'], ['router', 'Router process']]);
  D.cCpu = dcard('CPU · RAM', D.cores, D.ram.el, D.ckv.el);
  D.speed = h('div', { class: 'big' }, '–', h('small', {}, 'tok/s'));
  D.spark = h('div');
  D.skv = kvsEl([['pp', 'Prompt reading', 'Average prompt-processing speed of the local model'], ['ttft', 'First token', 'Average time until the first token'], ['replies', 'Replies']]);
  D.cSpeed = dcard('Local speed', D.speed, D.spark, D.skv.el);
  D.tkv = kvsEl([['in', 'Local tokens read'], ['out', 'Local tokens written'], ['tools', 'Tool calls'], ['turns', 'Turns'],
    ['rcalls', 'Router decisions'], ['rms', 'Avg routing time'], ['saved', 'Tokens saved by routing', 'Tool-schema tokens the local model did not need to read because the router only sent the tools it needed'], ['up', 'Session time']]);
  D.cTokens = dcard('This session', D.tkv.el);
  D.tri = h('div', { class: 'tri' });
  D.cReviews = dcard('Cloud reviews', D.tri);
  D.cloudBig = h('div', { class: 'big' }, '$0', h('small', {}, 'today'));
  D.cloudRows = h('div', { class: 'kvs' });
  D.budget = meterEl('Budget', 'gold');
  D.cCloud = dcard('Cloud usage', D.cloudBig, D.cloudRows, D.budget.el);
  D.mkv = kvsEl([['facts', 'Remembered facts'], ['lessons', 'Lessons learned', 'Corrections the local model took from reviews'], ['chats', 'Chat summaries'], ['tools', 'Tools'], ['skills', 'Skills']]);
  D.mcp = h('div', { class: 'kvs', style: 'margin-top:6px' });
  D.cMem = dcard('Memory · tools', D.mkv.el, D.mcp);
  D.loopTxt = h('div');
  D.cLoop = dcard('Forever-loop', D.loopTxt);
  dash.append(D.cModels, D.cLoop, D.cGpu, D.cCpu, D.cSpeed, D.cTokens, D.cReviews, D.cCloud, D.cMem);
  D.cLoop.classList.add('hidden');
}

function sparkSvg(vals) {
  if (!vals || vals.length < 2) return '';
  const w = 280, ht = 40, max = Math.max(...vals) * 1.1 || 1;
  const pts = vals.map((v, i) => [i / (vals.length - 1) * w, ht - v / max * (ht - 4) - 2]);
  const line = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  return `<svg class="spark" viewBox="0 0 ${w} ${ht}" preserveAspectRatio="none"><path class="a" d="${line} L${w} ${ht} L0 ${ht} Z"/><path class="l" d="${line}"/></svg>`;
}
const avg = a => a && a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0;
const fmtUp = s => s < 3600 ? `${Math.floor(s / 60)} min` : `${Math.floor(s / 3600)} h ${Math.floor(s % 3600 / 60)} min`;
const sumObj = o => Object.values(o || {}).reduce((a, b) => a + (+b || 0), 0);

function renderDash(d) {
  if (!D.built) buildDash();
  const sys = d.sys || {}, g = sys.gpu, ses = d.session || {}, rs = d.router || {}, eng = d.engine || {}, cl = d.cloud || {};
  const back = S.cloud?.backend, gback = S.gpt?.backend;
  const today = cl.today || {};
  const findModel = key => Object.entries(today).filter(([k]) => k.includes(key)).map(([, v]) => v);
  const laneSub = (key, role, b, plan) => {
    const rows = findModel(key); const calls = rows.reduce((a, r) => a + (r.calls || 0), 0);
    const tok = rows.reduce((a, r) => a + (r.input || 0) + (r.cache_read || 0) + (r.output || 0), 0);
    return `${role} · ${b === 'plan' ? plan + ' plan' : b === 'api' ? 'API key' : 'not connected'}${calls ? ` · ${calls} calls · ${fmtK(tok)} tokens today` : ''}`;
  };
  const act = S.activeLane;
  // models
  D.rows.router.set({
    title: rs.model || 'Router', live: act === 'router',
    state: !rs.enabled ? 'off' : !rs.configured ? 'not set up' : rs.ready ? 'ready' : rs.loading ? 'starting' : rs.error ? 'error' : 'stopped',
    cls: !rs.enabled || !rs.configured ? 'off' : rs.ready ? 'on' : rs.loading ? 'busy' : rs.error ? 'err' : 'off',
    sub: rs.ready ? `CPU · ${rs.threads} threads${rs.last ? ` · last ${Math.round(rs.last.ms)} ms` : ''}${sys.procs?.router ? ` · ${fmtNum(sys.procs.router)} MB` : ''}`
      : rs.configured && rs.error ? String(rs.error).slice(0, 90) : 'Settings → Router to pick one' });
  const busy = S.streaming && (act === 'local');
  D.rows.local.set({
    title: eng.model || 'No model loaded', live: busy,
    state: eng.status === 'ready' ? (busy ? 'generating' : 'ready') : eng.status || 'idle',
    cls: eng.status === 'ready' ? (busy ? 'busy' : 'on') : eng.status === 'error' ? 'err' : eng.status === 'idle' ? 'off' : 'busy',
    sub: eng.status === 'ready' ? `${fmtCtx(eng.ctx || 0)} ctx · ${eng.tune?.tg ?? '?'} tok/s tuned${eng.vram_mb ? ` · ${fmtNum(eng.vram_mb)} MB VRAM` : ''}${eng.vision ? ' · vision' : ''}` : 'Pick a model to load' });
  const sname = k => S.settings?.[k] || '';
  for (const [lane, key, role, b, plan] of [['astra', sname('gpt_review_model') || 'astra', 'reviews first', gback, 'ChatGPT'],
    ['sol', sname('gpt_fix_model') || 'sol', 'repairs major problems', gback, 'ChatGPT'],
    ['fable', 'fable', S.settings?.chatgpt_review_mode !== 'off' ? 'reviews last' : 'reviewer', back, 'Claude'], ['opus', 'opus', 'takes over on major problems', back, 'Claude']]) {
    const live = act === lane && S.streaming;
    D.rows[lane].set({ live, state: live ? (REVIEW_LANES.has(lane) ? 'reviewing' : 'working') : b ? 'standby' : 'off', cls: live ? 'busy' : b ? 'on' : 'off', sub: laneSub(key, role, b, plan) });
  }
  D.cModels.r.textContent = `${[rs.ready, eng.status === 'ready'].filter(Boolean).length} local · ${[gback && 'ChatGPT', back && 'Claude'].filter(Boolean).join(' + ') || 'no cloud'}`;
  // loop
  const lp = S.loop;
  D.cLoop.classList.toggle('hidden', !lp.active);
  if (lp.active) D.loopTxt.textContent = `Round ${lp.iteration}${+S.settings.loop_max ? ' of ' + S.settings.loop_max : ''} · running ${fmtUp(Math.round(Date.now() / 1000 - lp.started))} · ${lp.waitUntil ? 'waiting' : 'working'}`;
  // gpu
  D.cGpu.classList.toggle('hidden', !g);
  if (g) {
    D.cGpu.r.textContent = (g.name || '').replace(/^(NVIDIA (GeForce )?|AMD |Intel\(R\) )/, '');
    const vp = g.total_mb ? g.used_mb / g.total_mb * 100 : 0;
    D.vram.set(vp, `${(g.used_mb / 1024).toFixed(1)} / ${(g.total_mb / 1024).toFixed(1)} GB`, vp > 94 ? 'hot' : vp > 85 ? 'warm' : '');
    D.gload.set(g.util, `${g.util ?? 0}%`);
    D.temp.set((g.temp || 0) / 90 * 100, g.temp != null ? `${g.temp} °C` : '–', g.temp >= 83 ? 'hot' : g.temp >= 74 ? 'warm' : 'aqua');
    D.power.set(g.power_limit_w ? g.power_w / g.power_limit_w * 100 : 0, g.power_w != null ? `${Math.round(g.power_w)} / ${Math.round(g.power_limit_w || 0)} W` : '–', 'violet');
    D.gkv.set('sm', g.sm_mhz != null ? `${fmtNum(g.sm_mhz)} MHz` : '–'); D.gkv.set('mem', g.mem_mhz != null ? `${fmtNum(g.mem_mhz)} MHz` : '–');
    D.gkv.set('fan', g.fan != null ? `${g.fan}%` : '–');
  }
  // cpu
  const per = sys.cpu_per_core || [];
  if (D.cores.childElementCount !== per.length) { D.cores.innerHTML = ''; per.forEach(() => D.cores.append(h('i'))); D.cores.classList.toggle('two', per.length > 16); }
  per.forEach((v, i) => { D.cores.children[i].style.height = Math.max(4, v) + '%'; });
  D.cCpu.r.textContent = `${sys.cpu ?? 0}% · ${sys.cpu_mhz ? (sys.cpu_mhz / 1000).toFixed(2) + ' GHz' : ''}`;
  const rp = sys.ram_total_mb ? sys.ram_used_mb / sys.ram_total_mb * 100 : 0;
  D.ram.set(rp, `${(sys.ram_used_mb / 1024 || 0).toFixed(1)} / ${(sys.ram_total_mb / 1024 || 0).toFixed(0)} GB`, rp > 90 ? 'hot' : rp > 80 ? 'warm' : '');
  D.ckv.set('main', sys.procs?.main ? `${fmtNum(sys.procs.main)} MB RAM` : 'not running');
  D.ckv.set('router', sys.procs?.router ? `${fmtNum(sys.procs.router)} MB RAM` : 'not running');
  // speed
  const tg = ses.tg_history || [];
  D.speed.firstChild.textContent = tg.length ? tg[tg.length - 1].toFixed(1) : '–';
  D.spark.innerHTML = sparkSvg(tg);
  D.cSpeed.r.textContent = tg.length ? `avg ${avg(tg).toFixed(1)}` : '';
  D.skv.set('pp', ses.pp_history?.length ? `${fmtNum(Math.round(avg(ses.pp_history)))} tok/s` : '–');
  D.skv.set('ttft', ses.ttft_history?.length ? `${avg(ses.ttft_history).toFixed(2)} s` : '–');
  D.skv.set('replies', fmtNum(tg.length));
  // tokens
  D.tkv.set('in', fmtNum(ses.local_prompt_tokens)); D.tkv.set('out', fmtNum(ses.local_completion_tokens));
  D.tkv.set('tools', fmtNum(sumObj(ses.tool_calls))); D.tkv.set('turns', fmtNum(ses.turns));
  D.tkv.set('rcalls', fmtNum(ses.router_calls)); D.tkv.set('rms', ses.router_calls ? `${Math.round(ses.router_ms_total / ses.router_calls)} ms` : '–');
  D.tkv.set('saved', fmtNum(ses.router_tokens_saved)); D.tkv.set('up', fmtUp(sys.uptime_s || 0));
  // reviews
  const rv = ses.reviews || {};
  D.tri.innerHTML = '';
  for (const [k, lab, col] of [['ok', 'ok', 'var(--ok)'], ['minor', 'minor', 'var(--warn)'], ['major', 'major', 'var(--bad)']])
    D.tri.append(h('div', {}, h('b', { style: `color:${col}` }, String(rv[k] || 0)), h('small', {}, lab)));
  D.cReviews.r.textContent = sumObj(rv) ? `${sumObj(rv)} this session` : '';
  // cloud
  D.cloudBig.firstChild.textContent = '$' + (cl.today_usd || 0).toFixed(2);
  D.cloudBig.lastChild.textContent = back === 'plan' && gback !== 'api' ? 'API billed today' : 'today';
  const conn = (b, n) => b === 'plan' ? `${n} plan` : b === 'api' ? `${n} API` : null;
  D.cCloud.r.textContent = [conn(gback, 'ChatGPT'), conn(back, 'Claude')].filter(Boolean).join(' · ') || 'not connected';
  D.cloudRows.innerHTML = '';
  for (const [m, v] of Object.entries(today)) {
    D.cloudRows.append(h('span', {}, m), h('b', {}, `${v.calls || 0} calls · ${fmtK((v.input || 0) + (v.cache_read || 0))} in · ${fmtK(v.output || 0)} out`));
  }
  if (cl.today_plan_est_usd) D.cloudRows.append(h('span', { 'data-tip': 'What today\'s plan usage would have cost on the API. Not billed.' }, 'Plan, API-equivalent'), h('b', {}, '≈$' + cl.today_plan_est_usd.toFixed(2)));
  D.cloudRows.append(h('span', {}, 'This month (API)'), h('b', {}, '$' + (cl.month_usd || 0).toFixed(2)));
  const cap = +cl.budget || 0;
  D.budget.el.classList.toggle('hidden', !cap || back === 'plan');
  const cs = cl.today_claude_usd ?? cl.today_usd ?? 0;
  if (cap) D.budget.set(cs / cap * 100, `Claude $${cs.toFixed(2)} / $${cap}`, cs >= cap ? 'hot' : 'gold');
  // memory / tools / mcp
  const mem = d.memory || {};
  D.mkv.set('facts', fmtNum(mem.facts)); D.mkv.set('lessons', fmtNum(mem.lessons)); D.mkv.set('chats', fmtNum(mem.chats));
  D.mkv.set('tools', fmtNum(d.tools)); D.mkv.set('skills', fmtNum(d.skills));
  const sig = JSON.stringify(d.mcp || {});
  if (sig !== D.mcpSig) {
    D.mcpSig = sig; D.mcp.innerHTML = '';
    for (const [n, st] of Object.entries(d.mcp || {}))
      D.mcp.append(h('span', {}, n), h('b', { style: `color:${st === 'running' ? 'var(--ok)' : st === 'error' ? 'var(--bad)' : 'var(--warn)'}` }, st === 'needs_auth' ? 'sign in' : st));
  }
  D.cMem.r.textContent = Object.keys(d.mcp || {}).length ? `${Object.values(d.mcp).filter(s => s === 'running').length} MCP running` : '';
}

let dashTimer = 0;
async function pollDash() {
  clearTimeout(dashTimer);
  const on = S.settings ? S.settings.dashboard !== false : true;
  if (on && !document.hidden) {
    try { renderDash(await api('/api/stats')); } catch { }
  }
  dashTimer = setTimeout(pollDash, document.hidden ? 5000 : 1500);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) pollDash(); });

async function loadCloud() {
  try { S.cloud = await api('/api/cloud'); } catch { S.cloud = null; }
  try { S.gpt = await api('/api/chatgpt'); } catch { S.gpt = null; }
}

// ---------------------------------------------------------------- settings
const SECTIONS = ['General', 'Router', 'Claude', 'ChatGPT', 'GitHub', 'Plugins & MCP', 'Skills', 'Model & tuning', 'Tools', 'Privacy & offline', 'Memory', 'Appearance', 'Hugging Face', 'About'];
async function openSettings(start = 'General') {
  if (typeof start !== 'string') start = 'General';
  const s = await api('/api/state').then(r => r.settings);
  const nav = h('div', { class: 'set-nav' });
  const pane = h('div');
  const changes = {};
  const sections = {
    'General': () => [
      field('System prompt', h('textarea', { 'data-k': 'system_prompt' }, s.system_prompt), 'The local model\'s instructions. Leave it unchanged to keep getting Aero\'s improvements.'),
      h('div', { class: 'row3' },
        field('Temperature', num('temperature', s.temperature, 0.05)),
        field('Top P', num('top_p', s.top_p, 0.01)),
        field('Top K', num('top_k', s.top_k, 1))),
      h('div', { class: 'row3' },
        field('Min P', num('min_p', s.min_p, 0.01)),
        field('Repeat penalty', num('repeat_penalty', s.repeat_penalty, 0.01)),
        field('Max reply tokens', num('max_tokens', s.max_tokens, 1), '0 = until the context is full')),
      h('div', { class: 'row3' },
        field('Thinking', jselect('thinking', s.thinking, [['auto', 'Auto: the router decides'], [true, 'Always on'], [false, 'Off']])),
        field('Forever-loop pause (s)', num('loop_delay_s', s.loop_delay_s, 1), 'Wait between rounds.'),
        field('Forever-loop max rounds', num('loop_max', s.loop_max, 1), '0 = until you press Stop.')),
      toggle('review_in_loop', s.review_in_loop, 'Let ChatGPT and Claude review every forever-loop round (costs more; normally only round 1 is reviewed)'),
      toggle('auto_load_last', s.auto_load_last, 'On startup, load the last used model automatically'),
    ],
    'Router': () => routerSection(s),
    'Claude': () => claudeSection(s),
    'ChatGPT': () => chatgptSection(s),
    'GitHub': () => githubSection(s),
    'Plugins & MCP': () => mcpSection(),
    'Skills': () => skillsSection(),
    'Model & tuning': () => {
      const e = S.st.engine;
      return [
        e.model ? h('div', { class: 'kv', style: 'margin-bottom:16px' },
          h('span', {}, 'Loaded'), h('span', {}, e.model.name),
          h('span', {}, 'Configuration'), h('span', {}, e.desc || '-'),
          h('span', {}, 'Speed'), h('span', {}, e.tune ? `${e.tune.tg} tok/s generate · ${e.tune.pp} tok/s prompt` : '-'),
          h('span', {}, 'Tuned'), h('span', {}, e.tune ? `${e.tune.at} · ${e.tune.trials} trials · ${e.tune.depth || ''} · limit ${e.tune.limit_gb} GB` : '-'),
          h('span', {}, 'Advisor'), h('span', {}, e.tune ? (e.tune.advisor || 'built-in step policy') : '-')) : h('p', { class: 'muted' }, 'No model loaded.'),
        field('Tuning goal', select('tune_mode', s.tune_mode, [['balanced', 'Balanced: largest context at 80%+ of top speed, 32k+ when possible'], ['max_context', 'Max context: largest context at 50%+ of top speed'], ['max_speed', 'Max speed: smaller context (≤32k), fastest generation']]),
          'Also asked before every tune. Use Re-tune to apply it to a model that is already tuned.'),
        h('div', { class: 'row2' },
          field('Context cap', num('context_cap', s.context_cap, 1024), '0 = model maximum'),
          field('Tuning advisor', h('input', { type: 'text', 'data-k': 'advisor_model', value: s.advisor_model }),
            'auto = Bonsai first, quant sized to the VRAM you leave free · off = built-in step policy · or an HF repo / .gguf path')),
        toggle('allow_q4_kv', s.allow_q4_kv, 'Let the tuner try a q4_0 KV cache (more context, small quality loss)'),
        field('Speculative decoding', select('spec_mode', s.spec_mode || 'auto', [['auto', 'Auto: the model\'s own MTP layer (or a draft model) + n-gram lookup'], ['ngram', 'N-gram lookup only (no extra VRAM)'], ['off', 'Off']]),
          'Drafts several tokens and lets the model check them in one pass. Measured on an RTX 5080 with Qwen3.8-27B: 59 → 168 tok/s editing code, 59 → 69 tok/s on free text. MTP costs about 1 GB of VRAM, which counts against your limit when tuning. Changing it re-tunes on the next load.'),
        field('Draft model (optional)', h('input', { type: 'text', 'data-k': 'draft_model', value: s.draft_model, placeholder: 'path to a draft .gguf from the same family, e.g. mtp-…, eagle3-…, dflash-…' }),
          'Used instead of the MTP layer when set. The type is read from the file name (mtp-, eagle3-, dflash-, dspark-; anything else is a plain draft model).'),
        field('Extra llama-server arguments', h('input', { type: 'text', 'data-k': 'extra_server_args', value: s.extra_server_args }), 'Advanced. Appended to the final launch command.'),
        field('Models folder', h('input', { type: 'text', 'data-k': 'models_dir', value: s.models_dir })),
        h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' },
          e.model ? h('button', { class: 'btn ghost', onclick: () => { closeModal(); loadModel(e.model.id, true); } }, 'Re-tune current model now') : null,
          h('button', { class: 'btn ghost', onclick: () => { closeModal(); openLab(); } }, 'Open Performance Lab')),
      ];
    },
    'Tools': () => {
      const box = h('div');
      box.append(toggle('tools_enabled', s.tools_enabled, 'Enable tools (agent mode)'),
        field('Working directory', h('input', { type: 'text', 'data-k': 'work_dir', value: s.work_dir }), 'Relative paths in file and shell tools resolve here. Claude Code also starts here.'),
        h('div', { class: 'row2' },
          field('Max tool steps per reply', num('agent_max_steps', s.agent_max_steps, 1)),
          field('Screenshots kept as images', num('keep_screenshots', s.keep_screenshots, 1), 'Older ones are dropped from context to save tokens.')),
        h('label', { style: 'font-weight:600;font-size:13px;display:block;margin:6px 0' }, 'Permissions'));
      const pol = { ...s.tool_policy };
      api('/api/tools').then(t => {
        for (const [cat, desc] of Object.entries(t.categories)) {
          if (['meta', 'cloud', 'claude_code'].includes(cat)) continue;
          const list = t.tools[cat] || [];
          const seg = h('div', { class: 'seg' });
          for (const v of ['ask', 'auto', 'off']) seg.append(h('button', { class: (pol[cat] === v ? 'on ' : '') + v, onclick: () => { pol[cat] = v; $$('button', seg).forEach(b => b.classList.toggle('on', b.textContent === v)); box.dataset.policy = JSON.stringify(pol); } }, v));
          const names = h('small', {}, ...(list.length ? list.flatMap((x, i) => [i ? ', ' : '', h('span', { 'data-tool': x.name, style: 'cursor:help' }, x.name)]) : ['none']));
          box.append(h('div', { class: 'pol-row' }, h('span', {}, desc, names), seg));
        }
        box.dataset.policy = JSON.stringify(pol);
      });
      box.classList.add('policy-box');
      return [box, h('p', { class: 'muted', style: 'font-size:12.5px;margin-top:12px' }, 'ask = you approve each action · auto = runs immediately · off = hidden from the model. These rules also apply when Claude uses Aero\'s screen and desktop tools. Desktop control: slam the mouse into a screen corner to abort.')];
    },
    'Privacy & offline': () => privacySection(s),
    'Memory': () => memorySection(s),
    'Appearance': () => {
      const seg = h('div', { class: 'seg' });
      let theme = s.theme || 'auto';
      const inp = h('input', { type: 'hidden', 'data-k': 'theme', value: theme });
      for (const [v, lab] of [['auto', 'Auto (follow Windows)'], ['day', 'Day: bright sky'], ['night', 'Night: deep ocean']])
        seg.append(h('button', { class: v === theme ? 'on' : '', onclick: e => { theme = v; inp.value = v; $$('button', seg).forEach(b => b.classList.toggle('on', b === e.currentTarget)); document.documentElement.dataset.theme = v === 'auto' ? (darkQuery.matches ? 'night' : 'day') : v; } }, lab));
      const sceneSeg = h('div', { class: 'seg' });
      let scene = sceneMode();
      const sceneInp = h('input', { type: 'hidden', 'data-k': 'scenery', value: scene });
      for (const [v, lab] of [['full', 'Full: clouds, bubbles and frogs move'], ['still', 'Still: same scene, no motion'], ['off', 'Off: plain sky']])
        sceneSeg.append(h('button', { class: v === scene ? 'on' : '', onclick: e => { scene = v; sceneInp.value = v; $$('button', sceneSeg).forEach(b => b.classList.toggle('on', b === e.currentTarget));
          Scene.setMode(v); } }, lab));
      return [field('Theme', h('div', {}, seg, inp)),
        field('Scenery', h('div', {}, sceneSeg, sceneInp), 'Sky, sun, clouds, hills, bubbles and the frog pond behind the glass. The Performance Lab can measure what Full costs on your PC.'),
        toggle('pond', s.pond !== false, 'Show the frog pond along the bottom of the window'),
        toggle('scene_pause_busy', s.scene_pause_busy !== false, 'Freeze the scenery while a model is generating, tuning or benchmarking'),
        toggle('transparency', s.transparency !== false, 'Enable transparency: blur the scenery behind the glass frames (turn off to save GPU work)'),
        toggle('dashboard', s.dashboard !== false, 'Show the live dashboard on the right (models, GPU, CPU, tokens, Claude usage)')];
    },
    'Hugging Face': () => [field('Access token', h('input', { type: 'password', 'data-k': 'hf_token', value: s.hf_token, placeholder: 'hf_…' }), 'Only needed for gated or private models (Llama, some Gemma repos). Create one at huggingface.co/settings/tokens.')],
    'About': () => [h('div', { class: 'kv' },
      h('span', {}, 'App'), h('span', {}, `${S.st.app} ${S.st.version}`),
      h('span', {}, 'llama-server'), h('span', {}, S.st.llama.path || 'not found'),
      h('span', {}, 'GPU'), h('span', {}, (S.hw.gpus || []).map(g => `${g.name} (${fmtNum(g.total_mb)} MB, driver ${g.driver})`).join(', ') || 'none'),
      h('span', {}, 'CUDA (driver)'), h('span', {}, S.hw.cuda || '-'),
      h('span', {}, 'CPU'), h('span', {}, `${S.hw.cpu} · ${S.hw.cores} cores / ${S.hw.threads} threads`),
      h('span', {}, 'RAM'), h('span', {}, `${fmtNum(S.hw.ram_total_mb)} MB`)),
      h('div', { style: 'display:flex;gap:8px;margin-top:16px;flex-wrap:wrap' },
        ...[['data', 'Open data folder'], ['models', 'Open models folder'], ['logs', 'Open logs'], ['training', 'Open training data']].map(([w, lab]) =>
          h('button', { class: 'btn ghost sm', onclick: () => api('/api/open_path', { method: 'POST', json: { what: w } }) }, lab)),
        h('button', { class: 'btn ghost sm', onclick: async () => { await api('/api/unload', { method: 'POST' }); refreshState(); toast('Model unloaded'); } }, 'Unload model'),
        h('button', { class: 'btn danger sm', onclick: () => { api('/api/shutdown', { method: 'POST' }); setTimeout(() => window.close(), 300); } }, 'Quit Aero'))],
  };
  const show = name => {
    collect();
    $$('button', nav).forEach(b => b.classList.toggle('active', b.textContent === name));
    pane.innerHTML = ''; pane.append(...sections[name]().filter(Boolean));
  };
  function collect() {
    $$('[data-k]', pane).forEach(el => {
      const k = el.dataset.k;
      changes[k] = el.type === 'checkbox' ? el.checked : el.dataset.json ? JSON.parse(el.value) : (el.type === 'number' || el.dataset.num) ? Number(el.value) : el.value;
    });
    const pb = $('.policy-box', pane); if (pb?.dataset.policy) changes.tool_policy = JSON.parse(pb.dataset.policy);
  }
  for (const name of SECTIONS) nav.append(h('button', { onclick: () => show(name) }, name));
  const saveBtn = h('button', { class: 'btn', onclick: async () => {
    collect();
    S.settings = await api('/api/settings', { method: 'PUT', json: changes });
    closeModal(); toast('Settings saved');
    renderToggles(); applyTheme(); renderDashVisibility(); loadCloud(); refreshState();
  } }, 'Save');
  const body = h('div', {}, h('div', { class: 'set-layout' }, nav, pane), h('div', { style: 'display:flex;justify-content:flex-end;gap:8px;margin-top:14px;border-top:1px solid var(--line);padding-top:14px' }, h('button', { class: 'btn ghost', onclick: () => { closeModal(); applyTheme(); } }, 'Cancel'), saveBtn));
  modal('Settings', body, true);
  show(SECTIONS.includes(start) ? start : 'General');
}

function privacySection(s) {
  const box = h('div', {}, h('p', { class: 'muted' }, 'Loading local-only status…'));
  api('/api/local_status').then(st => {
    box.innerHTML = '';
    const lis = st.listeners;
    box.append(
      h('div', { class: 'kv', style: 'margin:4px 0 12px' },
        h('span', {}, 'Inference'), h('span', {}, st.engine),
        h('span', {}, 'Hosted engines'), h('span', {}, st.hosted_engines.length ? st.hosted_engines.join(', ') : 'none'),
        h('span', {}, 'Listening sockets'), h('span', {}, lis == null ? st.listeners_note : lis.length ? (st.all_loopback ? 'all on this PC only (loopback)' : 'NOT all loopback, see below') : 'none found'),
        h('span', {}, 'Network audit'), h('span', {}, `${st.audit_counts.allowed} allowed, ${st.audit_counts.blocked} blocked (recent)`)),
      lis?.length ? h('table', { class: 'rtable' }, h('thead', {}, h('tr', {}, h('th', {}, 'Process'), h('th', {}, 'Address'), h('th', {}, 'Port'), h('th', {}, ''))),
        h('tbody', {}, ...lis.map(l => h('tr', {}, h('td', {}, l.process), h('td', {}, l.address), h('td', { class: 'num' }, l.port), h('td', { class: l.loopback ? 'ok' : 'bad' }, l.loopback ? 'local only' : 'reachable from the network'))))) : null,
      st.disabled.length ? h('div', { class: 'field' }, h('label', {}, 'Turned off right now'), h('ul', { class: 'plain' }, ...st.disabled.map(x => h('li', {}, x)))) : null,
      h('div', { class: 'field' }, h('label', {}, 'What strict offline cannot cover'), h('ul', { class: 'plain' }, ...st.not_covered.map(x => h('li', {}, x)))),
      h('div', { class: 'field' }, h('label', {}, 'Recent outbound connections'),
        st.audit_recent.length ? h('table', { class: 'rtable' }, h('tbody', {}, ...st.audit_recent.slice(-12).reverse().map(r =>
          h('tr', {}, h('td', { class: 'muted' }, r.t), h('td', {}, r.host), h('td', { class: r.allowed ? '' : 'bad' }, r.allowed ? 'allowed' : 'blocked'))))) :
          h('small', {}, 'None yet. Only the host name is logged, never the address path or query.')));
  }).catch(e => { box.innerHTML = ''; box.append(h('div', { class: 'err-msg' }, e.message)); });
  return [
    toggle('strict_offline', s.strict_offline, 'Strict offline: Aero itself never connects to anything outside this PC'),
    h('p', { class: 'muted', style: 'font-size:12.5px;margin:-4px 0 14px' }, 'Turns off the ChatGPT and Claude reviews, web search, the automated browser, GitHub and other MCP servers, and Hugging Face downloads. Your local models, files, screen and app tools keep working. Every outbound attempt is logged in data\\audit\\network.jsonl.'),
    box];
}

/** Run a server job that streams {type: log|progress|result|error} events into a log box. */
async function runJob(path, body, logbox, bar) {
  logbox.classList.remove('hidden'); logbox.textContent = '';
  const add = t => { logbox.textContent += t + '\n'; logbox.scrollTop = 1e9; };
  let result = null, err = null;
  try {
    await sseFetch(path, body, ev => {
      if (ev.type === 'log') add(ev.text);
      else if (ev.type === 'progress') {
        if (bar) { bar.classList.remove('hidden'); $('i', bar).style.width = (ev.total ? ev.done / ev.total * 100 : 0).toFixed(1) + '%'; }
        const line = `${ev.file ? ev.file.split('/').pop() + ': ' : ''}${fmtBytes(ev.done)} / ${fmtBytes(ev.total)} · ${fmtBytes(ev.speed || 0)}/s`;
        const lines = logbox.textContent.split('\n'); if (lines.length > 1 && lines[lines.length - 2].includes(' / ')) lines.splice(lines.length - 2, 1);
        logbox.textContent = lines.join('\n') + line + '\n';
      }
      else if (ev.type === 'result') result = ev.result;
      else if (ev.type === 'error') err = ev.error;
    });
  } catch (e) { err = e.message; }
  if (err) add('Error: ' + err);
  bar?.classList.add('hidden');
  return { result, err };
}

function benchTable(b) {
  if (!b?.rows?.length) return null;
  return h('table', { class: 'rtable', style: 'margin-top:10px' },
    h('thead', {}, h('tr', {}, h('th', {}, 'Threads'), h('th', {}, 'Prompt'), h('th', {}, 'Generate'), h('th', {}, 'One decision'))),
    h('tbody', {}, ...b.rows.map(r => h('tr', { class: r.threads === b.threads || r.threads === b.batch_threads ? 'cur' : '' },
      h('td', { class: 'num' }, String(r.threads)),
      h('td', { class: 'num' }, `${r.pp} tok/s` + (r.threads === b.batch_threads ? ' ✓' : '')),
      h('td', { class: 'num' }, `${r.tg} tok/s` + (r.threads === b.threads ? ' ✓' : '')),
      h('td', { class: 'num' }, r.decision_ms ? `≈${r.decision_ms} ms` : '–')))));
}

function routerSection(s) {
  const wrap = h('div', {}, h('p', { class: 'muted' }, 'Loading…'));
  const out = [
    h('p', { class: 'sec-intro' }, 'A small model on your ', h('b', {}, 'CPU and RAM'), ' reads each request first and decides which tools the main model gets, whether it should think, whether ChatGPT or Claude should review it, and a short plan. It never touches your GPU, so your main model keeps all its VRAM. Everything stays on this PC.'),
    h('div', { class: 'row2' },
      toggle('router_enabled', s.router_enabled, 'Use the router'),
      toggle('router_plan', s.router_plan, 'Let it add a short plan for complex requests')),
    h('div', { class: 'row3' },
      field('Minimum tools', num('router_tools_min', s.router_tools_min, 1), 'The main model always gets at least this many (it can load more).'),
      field('Threads', num('router_threads', s.router_threads, 1), '0 = the benchmarked best'),
      field('Custom router .gguf', h('input', { type: 'text', 'data-k': 'router_model', value: s.router_model, placeholder: 'empty = the one chosen below' }))),
    wrap,
  ];
  (async () => {
    let r;
    try { r = await api('/api/router'); } catch (e) { wrap.innerHTML = `<div class="err-msg">${esc(e.message)}</div>`; return; }
    const st = r.status, cfg = r.config || {};
    wrap.innerHTML = '';
    const log = h('div', { class: 'logbox hidden' });
    const bar = h('div', { class: 'progress hidden', style: 'margin-top:8px' }, h('i'));
    const busy = on => $$('button', wrap).forEach(b => b.disabled = on);
    const statusLine = h('div', { class: 'callout' },
      h('b', {}, cfg.name || 'No router chosen yet'), ' · ',
      !st.configured ? 'pick one below (it downloads, then benchmarks your CPU)' : st.ready ? `running on ${st.threads} CPU threads${st.last ? `, last decision ${Math.round(st.last.ms)} ms` : ''}` : st.loading ? 'starting…' : st.error ? 'error: ' + st.error : 'stopped',
      cfg.bench ? h('small', { class: 'muted', style: 'display:block' }, `Benchmarked ${cfg.bench.at}: generates with ${cfg.bench.threads} threads (${cfg.bench.tg} tok/s), reads prompts with ${cfg.bench.batch_threads || cfg.bench.threads} (${cfg.bench.pp} tok/s), ≈${cfg.bench.decision_ms} ms per decision`) : null);
    const actions = h('div', { style: 'display:flex;gap:8px;margin:8px 0 14px;flex-wrap:wrap' },
      h('button', { class: 'btn ghost sm', disabled: !st.configured, onclick: async () => {
        busy(true); const { result } = await runJob('/api/router/benchmark', {}, log); busy(false);
        if (result) { toast(`Router re-benchmarked: ${result.threads} threads`); { const t = benchTable(result); if (t) wrap.after(t); }; }
      } }, 'Re-benchmark on this PC'),
      h('button', { class: 'btn ghost sm', onclick: async () => { await api('/api/router/restart', { method: 'POST' }); toast('Router restarting'); } }, 'Restart router'));
    const rows = r.candidates.map(c => {
      const cur = cfg.id === c.id;
      const scores = h('div', {}, ...c.scores.map(x => h('small', {}, `${x.name}: `, h('b', {}, x.value), x.note ? ` (${x.note})` : '', ' ',
        x.src ? h('a', { href: x.src, target: '_blank', rel: 'noreferrer' }, 'source') : null)));
      const use = h('button', { class: 'btn sm' + (cur ? ' ghost' : c.recommended ? '' : ' ghost') }, cur ? 'Reinstall' : 'Use this');
      use.onclick = async () => {
        busy(true);
        const { result, err } = await runJob('/api/router/choose', { id: c.id }, log, bar);
        busy(false);
        if (!err) { toast(`${c.name} is now your router`); { const t = benchTable(result); if (t) log.after(t); }; }
      };
      return h('tr', { class: cur ? 'cur' : '' },
        h('td', {}, h('b', {}, c.name), c.recommended ? h('span', { class: 'badge rec' }, 'recommended') : null, cur ? h('span', { class: 'badge gpu' }, 'current') : null,
          h('small', {}, `${c.org} · ${c.params} · ${c.license} · released ${c.released}`), h('small', {}, c.tag), h('small', { style: 'margin-top:4px' }, c.notes)),
        h('td', { class: 'num' }, `${c.quant}`, h('small', {}, `${c.size_gb} GB`)),
        h('td', {}, scores),
        h('td', {}, h('small', {}, cur && cfg.bench ? `measured: ${cfg.bench.tg} tok/s · ≈${cfg.bench.decision_ms} ms` : c.speed_est)),
        h('td', {}, use));
    });
    wrap.append(...[statusLine, actions,
      h('table', { class: 'rtable' }, h('thead', {}, h('tr', {}, h('th', {}, 'Model'), h('th', {}, 'Size'), h('th', {}, 'Scores'), h('th', {}, 'Speed on your CPU'), h('th', {}))), h('tbody', {}, ...rows)),
      bar, log, benchTable(cfg.bench),
      h('p', { class: 'muted', style: 'font-size:12px' }, 'Scores are copied from the linked pages; "vendor" means the model\'s maker reported them. Speeds marked est. are estimates until the benchmark runs on your CPU. Jev was left out because it only runs on TypeSafe\'s servers, not on your PC.')].filter(Boolean));
  })();
  return out;
}

function claudeSection(s) {
  const plan = h('div', { class: 'list' }, h('div', { class: 'li' }, h('div', { class: 't' }, h('b', {}, 'Checking Claude Code sign-in…'))));
  const keyBox = h('div');
  const conn = select('cloud_backend', s.cloud_backend || 'auto', [['auto', 'Automatic: API key if you add one, otherwise your Claude plan'], ['plan', 'My Claude plan (Pro / Max) through Claude Code'], ['api', 'Anthropic API key (pay per token)']]);
  const drawPlan = async (fresh) => {
    let st;
    try { st = await api('/api/claude/plan'); } catch (e) { st = { error: e.message }; }
    plan.innerHTML = '';
    const signIn = h('button', { class: 'btn sm violet', onclick: async () => {
      try { await api('/api/claude/plan/login', { method: 'POST' }); toast('Claude Code\'s sign-in opened. Finish it in your browser, then press Refresh.', false, 8000); }
      catch (e) { toast(e.message, true, 8000); }
    } }, st.loggedIn ? 'Switch account' : 'Sign in with Claude');
    const refresh = h('button', { class: 'btn ghost sm', onclick: () => drawPlan(true) }, 'Refresh');
    const out = h('button', { class: 'btn ghost sm', onclick: async () => { await api('/api/claude/plan/logout', { method: 'POST' }); drawPlan(true); loadCloud(); } }, 'Sign out');
    if (st.error && !('loggedIn' in st)) {
      plan.append(h('div', { class: 'li' }, h('div', { class: 't' }, h('b', {}, 'Claude Code is not available'), h('small', {}, st.error)), refresh));
      return;
    }
    plan.append(h('div', { class: 'li' }, h('span', { class: 'orb', style: '--lc:var(--fable)' }),
      h('div', { class: 't' }, h('b', {}, st.loggedIn ? `Signed in${st.email ? ' as ' + st.email : ''}` : 'Not signed in'),
        h('small', {}, st.loggedIn ? [st.subscriptionType || st.plan ? `Plan: ${st.subscriptionType || st.plan}` : null, st.organizationName, st.authMethod ? `via ${st.authMethod}` : null].filter(Boolean).join(' · ')
          : 'Uses your Claude Pro or Max subscription. Opens claude.ai in your browser.'),
        st.version ? h('small', {}, `Claude Code ${st.version}${st.version_ok === false ? ' · update needed (re-run Update-Aero.bat)' : ''}`) : null),
      signIn, refresh, st.loggedIn ? out : null));
    if (fresh) { loadCloud(); toast(st.loggedIn ? 'Claude plan connected' : 'Not signed in yet'); }
  };
  const drawKey = async () => {
    let c; try { c = await api('/api/cloud'); } catch { c = { key: {} }; }
    keyBox.innerHTML = '';
    const inp = h('input', { type: 'password', class: 'inp', placeholder: c.key?.set ? `Saved: ${c.key.masked}` : 'sk-ant-…', style: 'flex:1' });
    const res = h('small', { class: 'muted' });
    keyBox.append(h('div', { style: 'display:flex;gap:8px;align-items:center' }, inp,
      h('button', { class: 'btn sm', onclick: async () => { if (!inp.value.trim()) return; await api('/api/cloud/key', { method: 'PUT', json: { key: inp.value.trim() } }); inp.value = ''; toast('API key saved (encrypted with Windows DPAPI)'); drawKey(); loadCloud(); } }, 'Save'),
      h('button', { class: 'btn ghost sm', onclick: async () => {
        res.textContent = 'Testing…';
        try { const r = await api('/api/cloud/test', { method: 'POST' }); res.textContent = r.missing.length ? `Works, but this key cannot use: ${r.missing.join(', ')}` : 'Works: Fable 5.1 and Opus 5.5 are available.'; }
        catch (e) { res.textContent = e.message; }
      } }, 'Test'),
      c.key?.set ? h('button', { class: 'btn ghost sm', onclick: async () => { await api('/api/cloud/key', { method: 'PUT', json: { key: '' } }); drawKey(); loadCloud(); } }, 'Remove') : null), res);
  };
  drawPlan(); drawKey();
  return [
    h('p', { class: 'sec-intro' }, 'When your local model finishes, ', h('b', {}, 'Claude Fable 5.1'), ' reviews the work. If it is fine you get it as is. Small problems go back to your local model with a fix list. Big problems: Fable re-plans the task and ',
      h('b', {}, 'Claude Opus 5.5'), ' does it, then your local model writes down what it learned. Every model\'s thinking and tool calls show up in the chat.'),
    field('Connection', conn),
    h('label', { class: 'mem-h' }, 'Your Claude plan'), plan,
    h('div', { class: 'callout violet', style: 'margin-top:10px' }, h('b', {}, 'Why sign-in opens claude.ai instead of a page inside Aero. '),
      'Aero uses Anthropic\'s own Claude Code sign-in, so you log in on the real claude.ai page in your browser. A copy of Claude\'s login screen inside another app is what phishing looks like, and Aero never sees your password or your Claude tokens: it only asks Claude Code whether you are signed in. Claude Code also brings its built-in tools, your Claude Code plugins, skills and MCP servers to Opus.'),
    h('div', { class: 'callout gold' }, h('b', {}, 'Plan usage. '), 'Reviews and take-overs count against your plan\'s limits. On Pro, Fable requests are billed to usage credits; Max includes Fable up to 50% of your weekly limits. The dashboard shows what the same tokens would cost on the API.'),
    h('label', { class: 'mem-h' }, 'Or an Anthropic API key'), keyBox,
    h('p', { class: 'muted', style: 'font-size:12.5px' }, 'With an API key, server-side fallbacks are on: if Fable or Opus declines part of a request, another Claude model finishes it instead of the review failing.'),
    h('label', { class: 'mem-h' }, 'Review'),
    h('div', { class: 'row3' },
      field('Default', select('review_mode', s.review_mode || 'off', [['off', 'Off'], ['on', 'Every reply'], ['auto', 'Router decides']]), 'The Claude button in the composer changes this. With ChatGPT on too, Claude reviews last.'),
      field('Rounds before Opus', num('review_max_rounds', s.review_max_rounds, 1), 'Local fix attempts for minor problems.'),
      field('Daily API budget ($)', num('cloud_daily_budget_usd', s.cloud_daily_budget_usd, 1), 'API key only. 0 = no cap.')),
    h('div', { class: 'row2' },
      field('Reviewer model', h('input', { type: 'text', 'data-k': 'review_model', value: s.review_model })),
      field('Take-over model', h('input', { type: 'text', 'data-k': 'fix_model', value: s.fix_model }))),
    h('div', { class: 'row2' },
      field('Reviewer effort', select('review_effort', s.review_effort, ['low', 'medium', 'high', 'xhigh', 'max'].map(x => [x, x]))),
      field('Take-over effort', select('fix_effort', s.fix_effort, ['low', 'medium', 'high', 'xhigh', 'max'].map(x => [x, x])))),
    toggle('cloud_fable_tools', s.cloud_fable_tools, 'Let Fable read files, search the web and look at the screen while reviewing'),
    toggle('lessons_enabled', s.lessons_enabled, 'After a review finds problems, have the local model write lessons into memory'),
    toggle('share_lessons_as_training', s.share_lessons_as_training, 'Also save each correction to data\\training\\corrections.jsonl (for fine-tuning later)'),
  ];
}

function chatgptSection(s) {
  const plan = h('div', { class: 'list' }, h('div', { class: 'li' }, h('div', { class: 't' }, h('b', {}, 'Checking the Codex sign-in…'))));
  const keyBox = h('div');
  const conn = select('chatgpt_backend', s.chatgpt_backend || 'auto', [['auto', 'Automatic: API key if you add one, otherwise your ChatGPT plan'], ['plan', 'My ChatGPT plan (Plus / Pro / Business) through the Codex CLI'], ['api', 'OpenAI API key (pay per token)']]);
  const drawPlan = async (fresh) => {
    let st;
    try { st = await api('/api/chatgpt/plan'); } catch (e) { st = { error: e.message }; }
    plan.innerHTML = '';
    const refresh = h('button', { class: 'btn ghost sm', onclick: () => drawPlan(true) }, 'Refresh');
    if (!st.installed) {
      plan.append(h('div', { class: 'li wrap' }, h('span', { class: 'orb', style: '--lc:var(--astra)' }),
        h('div', { class: 't' }, h('b', {}, 'The Codex CLI is not installed'),
          h('small', {}, 'Codex is OpenAI\'s own command-line agent; Aero uses it to reach ChatGPT with your plan. Install it once in a terminal with: npm install -g @openai/codex (needs Node.js), then press Refresh. Or use an OpenAI API key below.')),
        refresh));
      return;
    }
    if (st.offline) {
      plan.append(h('div', { class: 'li wrap' }, h('div', { class: 't' }, h('b', {}, 'Strict offline mode is on'), h('small', {}, 'Codex is installed; checking the sign-in needs the network.')), refresh));
      return;
    }
    const signIn = h('button', { class: 'btn sm astra', onclick: async () => {
      try { await api('/api/chatgpt/plan/login', { method: 'POST' }); toast('Codex\'s sign-in opened. Finish it in your browser, then press Refresh.', false, 8000); }
      catch (e) { toast(e.message, true, 8000); }
    } }, st.loggedIn ? 'Switch account' : 'Sign in with ChatGPT');
    const device = h('button', { class: 'btn ghost sm', title: 'For PCs where the browser sign-in can\'t reach back to Codex: shows a code to enter at chatgpt.com', onclick: async () => {
      try { await api('/api/chatgpt/plan/login', { method: 'POST', json: { device: true } }); toast('A console window shows a code: enter it on the page it names, then press Refresh.', false, 9000); }
      catch (e) { toast(e.message, true, 8000); }
    } }, 'Sign in with a code');
    const out = h('button', { class: 'btn ghost sm', onclick: async () => { await api('/api/chatgpt/plan/logout', { method: 'POST' }); drawPlan(true); loadCloud(); } }, 'Sign out');
    plan.append(h('div', { class: 'li wrap' }, h('span', { class: 'orb', style: '--lc:var(--astra)' }),
      h('div', { class: 't' }, h('b', {}, st.loggedIn ? (st.method === 'chatgpt' ? 'Signed in with ChatGPT' : st.method === 'api_key' ? 'Codex is signed in with an API key, not a ChatGPT plan' : 'Signed in') : 'Not signed in'),
        h('small', {}, st.loggedIn ? 'Reviews and repairs count against your ChatGPT plan\'s Codex limits.' : 'Uses your ChatGPT subscription. Opens chatgpt.com in your browser.'),
        st.version ? h('small', {}, `Codex CLI ${st.version}`) : null, st.error ? h('small', {}, st.error) : null),
      signIn, st.loggedIn ? null : device, refresh, st.loggedIn ? out : null));
    if (fresh) { loadCloud(); toast(st.loggedIn ? 'ChatGPT plan connected' : 'Not signed in yet'); }
  };
  const drawKey = async () => {
    let c; try { c = await api('/api/chatgpt'); } catch { c = { key: {} }; }
    keyBox.innerHTML = '';
    const inp = h('input', { type: 'password', class: 'inp', placeholder: c.key?.set ? `Saved: ${c.key.masked}` : 'sk-…', style: 'flex:1' });
    const res = h('small', { class: 'muted' });
    keyBox.append(h('div', { style: 'display:flex;gap:8px;align-items:center' }, inp,
      h('button', { class: 'btn sm', onclick: async () => { if (!inp.value.trim()) return; await api('/api/chatgpt/key', { method: 'PUT', json: { key: inp.value.trim() } }); inp.value = ''; toast('API key saved (encrypted with Windows DPAPI)'); drawKey(); loadCloud(); } }, 'Save'),
      h('button', { class: 'btn ghost sm', onclick: async () => {
        res.textContent = 'Testing…';
        try { const r = await api('/api/chatgpt/test', { method: 'POST' }); res.textContent = r.missing.length ? `Works, but this key cannot use: ${r.missing.join(', ')}` : 'Works: GPT-6 Astra and GPT-6.1 Sol are available.'; }
        catch (e) { res.textContent = e.message; }
      } }, 'Test'),
      c.key?.set ? h('button', { class: 'btn ghost sm', onclick: async () => { await api('/api/chatgpt/key', { method: 'PUT', json: { key: '' } }); drawKey(); loadCloud(); } }, 'Remove') : null), res);
  };
  drawPlan(); drawKey();
  const efforts = ['low', 'medium', 'high', 'xhigh', 'max'].map(x => [x, x]);
  return [
    h('p', { class: 'sec-intro' }, 'A second finalization option, with its own ', h('b', {}, 'ChatGPT'), ' button in the composer. ', h('b', {}, 'GPT-6 Astra'),
      ' reviews your local model\'s work. Small problems go back to your local model with a fix list; big ones go to ', h('b', {}, 'GPT-6.1 Sol'),
      ', which repairs them. With the Claude button on too, ChatGPT goes first and Claude reviews last, with Astra\'s verdicts and Sol\'s work in front of it. Your local model learns from both.'),
    field('Connection', conn),
    h('label', { class: 'mem-h' }, 'Your ChatGPT plan'), plan,
    h('div', { class: 'callout violet', style: 'margin-top:10px' }, h('b', {}, 'How sign-in works. '),
      'Aero uses OpenAI\'s own Codex CLI and its own sign-in, which opens the real chatgpt.com page in your browser. Aero never sees your password or your ChatGPT tokens and never opens Codex\'s auth file: it only asks Codex whether you are signed in. Through the plan, Sol works in your work folder with Codex\'s own sandbox, and Aero asks you once before each repair.'),
    h('label', { class: 'mem-h' }, 'Or an OpenAI API key'), keyBox,
    h('p', { class: 'muted', style: 'font-size:12.5px' }, 'With an API key, Astra and Sol use Aero\'s own tools under your approval rules (Settings → Tools), exactly like Claude. Prices (per million tokens): Astra $10 in / $50 out, Sol $2 in / $10 out, with cheaper cached input.'),
    h('label', { class: 'mem-h' }, 'Review'),
    h('div', { class: 'row3' },
      field('Default', select('chatgpt_review_mode', s.chatgpt_review_mode || 'off', [['off', 'Off'], ['on', 'Every reply'], ['auto', 'Router decides']]), 'The ChatGPT button in the composer changes this.'),
      field('Rounds before Sol', num('review_max_rounds', s.review_max_rounds, 1), 'Shared with Claude.'),
      field('Daily API budget ($)', num('openai_daily_budget_usd', s.openai_daily_budget_usd, 1), 'API key only. 0 = no cap.')),
    h('div', { class: 'row2' },
      field('Reviewer model', h('input', { type: 'text', 'data-k': 'gpt_review_model', value: s.gpt_review_model })),
      field('Repair model', h('input', { type: 'text', 'data-k': 'gpt_fix_model', value: s.gpt_fix_model }))),
    h('div', { class: 'row2' },
      field('Reviewer effort', select('gpt_review_effort', s.gpt_review_effort, efforts)),
      field('Repair effort', select('gpt_fix_effort', s.gpt_fix_effort, efforts))),
    toggle('gpt_review_tools', s.gpt_review_tools, 'Let Astra read files, search the web and look at the screen while reviewing (API key)'),
    field('Codex CLI path', h('input', { type: 'text', 'data-k': 'codex_cli_path', value: s.codex_cli_path || '', placeholder: 'empty = find it automatically' }), 'Only if Aero can\'t find codex.exe by itself.'),
  ];
}

function githubSection(s) {
  const box = h('div', {}, h('p', { class: 'muted' }, 'Loading…'));
  const draw = async () => {
    let g; try { g = await api('/api/github'); } catch (e) { box.innerHTML = `<div class="err-msg">${esc(e.message)}</div>`; return; }
    box.innerHTML = '';
    if (g.connected) {
      const u = g.user || {};
      box.append(h('div', { class: 'ghuser' }, u.avatar ? h('img', { src: u.avatar, alt: '' }) : h('span', { class: 'ico', html: I.github }),
        h('div', { class: 't', style: 'flex:1' }, h('b', {}, u.name || u.login || 'Connected'), h('small', { class: 'muted', style: 'display:block' }, `@${u.login || '?'}${u.scopes ? ' · scopes: ' + u.scopes : ''}`),
          h('small', { class: 'muted', style: 'display:block' }, 'GitHub MCP server: ' + (g.mcp ? `${g.mcp.state}${g.mcp.tools ? ` · ${g.mcp.tools.length} tools` : ''}${g.mcp.error ? ' · ' + g.mcp.error : ''}` : 'not started'))),
        h('button', { class: 'btn ghost sm', onclick: async () => { await api('/api/github/disconnect', { method: 'POST' }); draw(); } }, 'Disconnect')));
    } else {
      const tok = h('input', { type: 'password', class: 'inp', placeholder: 'github_pat_… or ghp_…', style: 'flex:1' });
      const go = async body => { try { await api('/api/github/connect', { method: 'POST', json: body }); toast('GitHub connected'); draw(); } catch (e) { toast(e.message, true, 7000); } };
      box.append(h('p', { class: 'sec-intro' }, 'Connect GitHub and both your local model and Claude can read repos, issues, pull requests and Actions through GitHub\'s official MCP server.'),
        g.cli ? h('button', { class: 'btn', onclick: () => go({ cli: true }) }, 'Use my GitHub CLI login (gh)') : h('p', { class: 'muted' }, 'Tip: install the GitHub CLI (cli.github.com) and run "gh auth login" to connect with one click.'),
        h('div', { style: 'display:flex;gap:8px;margin-top:10px' }, tok, h('button', { class: 'btn ghost', onclick: () => go({ token: tok.value }) }, 'Connect with token')),
        h('small', { class: 'muted' }, 'Fine-grained tokens: github.com → Settings → Developer settings → Personal access tokens. Stored encrypted with Windows DPAPI.'));
    }
  };
  draw();
  return [box,
    field('Toolsets', h('input', { type: 'text', 'data-k': 'github_toolsets', value: s.github_toolsets }), 'Which groups of GitHub tools to load: context, repos, issues, pull_requests, actions, code_security, notifications, discussions, gists… ("all" for everything)'),
    toggle('github_read_only', s.github_read_only, 'Read-only (no pushes, comments or merges)')];
}

function mcpSection() {
  const found = h('div', {}, h('p', { class: 'muted' }, 'Looking for MCP servers in Claude Desktop, Claude Code, its plugins, Cursor and VS Code…'));
  const servers = h('div', { class: 'list' });
  const ta = h('textarea', { class: 'code', spellcheck: 'false' }, 'Loading…');
  const drawServers = st => {
    servers.innerHTML = '';
    const names = Object.keys(st || {});
    if (!names.length) servers.append(h('div', { class: 'li' }, h('div', { class: 't' }, h('small', {}, 'No MCP servers yet.'))));
    for (const n of names) {
      const v = st[n];
      const act = (path, label, cls = 'btn ghost sm') => h('button', { class: cls, onclick: async () => {
        try { const r = await api(`/api/mcp/${encodeURIComponent(n)}/${path}`, { method: 'POST' }); if (r.status) drawServers(r.status); if (r.url && !r.opened) window.open(r.url, '_blank'); if (path === 'auth') toast('Finish signing in in your browser.'); }
        catch (e) { toast(e.message, true); } } }, label);
      servers.append(h('div', { class: 'li' }, h('span', { class: 'dot ' + (v.state === 'running' ? 'ready' : v.state === 'error' ? 'error' : 'busy') }),
        h('div', { class: 't' }, h('b', {}, n), h('small', {}, `${v.state === 'needs_auth' ? 'needs sign-in' : v.state}${v.kind ? ' · ' + v.kind : ''}${v.tools ? ` · ${v.tools.length} tools` : ''}${v.error ? ' · ' + v.error : ''}`)),
        v.state === 'needs_auth' ? act('auth', 'Sign in', 'btn sm') : null, act('restart', 'Restart'), v.kind === 'http' || v.state === 'needs_auth' ? act('signout', 'Sign out') : null));
    }
  };
  api('/api/mcp').then(r => { ta.value = r.text; drawServers(r.status); }).catch(e => { ta.value = ''; toast(e.message, true); });
  api('/api/mcp/importable').then(r => {
    found.innerHTML = '';
    if (!r.servers.length) { found.append(h('p', { class: 'muted', style: 'font-size:13px' }, 'Nothing new found on this PC.')); return; }
    const picks = new Set();
    const list = h('div', { class: 'list' }, ...r.servers.map((x, i) => h('label', { class: 'li', style: 'cursor:pointer' },
      h('input', { type: 'checkbox', onchange: e => e.target.checked ? picks.add(i) : picks.delete(i) }),
      h('div', { class: 't' }, h('b', {}, x.name), h('small', {}, `${x.source} · ${x.config.url || [x.config.command, ...(x.config.args || [])].join(' ')}`)))));
    found.append(list, h('div', { style: 'margin-top:8px' }, h('button', { class: 'btn sm', onclick: async () => {
      if (!picks.size) return toast('Tick the servers to import first.');
      const r2 = await api('/api/mcp/import', { method: 'POST', json: { items: [...picks].map(i => r.servers[i]) } });
      toast(r2.added.length ? `Imported ${r2.added.join(', ')}` : 'Already imported'); drawServers(r2.status);
      api('/api/mcp').then(x => { ta.value = x.text; }); loadToolDescriptions();
    } }, 'Import selected')));
  }).catch(e => { found.innerHTML = `<div class="err-msg">${esc(e.message)}</div>`; });
  const save = h('button', { class: 'btn', onclick: async () => { save.disabled = true; try { const r = await api('/api/mcp', { method: 'PUT', json: { text: ta.value } }); drawServers(r.status); toast('MCP servers restarted'); loadToolDescriptions(); } catch (e) { toast(e.message, true); } save.disabled = false; } }, 'Save and restart servers');
  return [
    h('p', { class: 'sec-intro' }, 'Plugins today are ', h('b', {}, 'MCP servers plus skills'), '. Aero runs any MCP server (local commands and remote URLs with sign-in) for your local model, and Claude Opus also gets your Claude Code plugins directly.'),
    h('div', { class: 'callout' }, h('b', {}, 'ChatGPT plugins. '), 'The original ChatGPT plugin store was shut down in April 2024. Today\'s ChatGPT and Codex plugins are MCP servers plus SKILL.md skills, so you can add one here by pasting its MCP server URL (it must allow outside clients) and drop its skills into the Skills folder. They cannot be installed from ChatGPT itself.'),
    h('label', { class: 'mem-h' }, 'Found on this PC'), found,
    h('label', { class: 'mem-h' }, 'Your servers'), servers,
    field('mcp.json', ta, 'Same format as Claude Desktop. Remote servers: { "type": "http", "url": "https://…/mcp" }. Tools show up as mcp_<server>_<tool>.'),
    h('div', { style: 'margin-top:4px' }, save),
  ];
}

function skillsSection() {
  const list = h('div', { class: 'list' }, h('div', { class: 'li' }, h('small', { class: 'muted' }, 'Loading…')));
  const dirNote = h('small', { class: 'muted' });
  const draw = r => {
    list.innerHTML = '';
    dirNote.textContent = `Your own skills go in ${r.dir}\\<name>\\SKILL.md. Claude Code skills and plugin skills are found automatically.`;
    if (!r.skills.length) list.append(h('div', { class: 'li' }, h('div', { class: 't' }, h('small', {}, 'No skills found yet.'))));
    for (const x of r.skills) {
      const cb = h('input', { type: 'checkbox', checked: x.enabled !== false, onchange: async e => { draw(await api('/api/skills/' + encodeURIComponent(x.name), { method: 'PUT', json: { enabled: e.target.checked } })); } });
      list.append(h('label', { class: 'li', style: 'cursor:pointer' }, cb, h('div', { class: 't' }, h('b', {}, x.name), h('small', { title: x.description }, `${x.source} · ${x.description}`))));
    }
  };
  api('/api/skills').then(draw).catch(e => { list.innerHTML = `<div class="err-msg">${esc(e.message)}</div>`; });
  return [h('p', { class: 'sec-intro' }, 'Skills are folders of instructions (SKILL.md) a model loads when a task needs them. The router suggests relevant ones and the model opens them with use_skill.'),
    list, h('div', { style: 'display:flex;gap:8px;align-items:center;margin-top:10px' },
      h('button', { class: 'btn ghost sm', onclick: () => api('/api/open_path', { method: 'POST', json: { what: 'skills' } }) }, 'Open skills folder'), dirNote)];
}

function memorySection(s) {
  const list = h('div', { class: 'mem-list' }, h('p', { class: 'muted' }, 'Loading…'));
  const sums = h('div', { class: 'mem-list' });
  const q = h('input', { type: 'text', placeholder: 'Filter memories…', class: 'mem-filter' });
  const add = h('input', { type: 'text', placeholder: 'Add something to remember, e.g. "I prefer PowerShell over cmd"' });
  const addKind = h('select', { class: 'mem-kind', title: 'What kind of memory' },
    [['preference', 'Preference'], ['user', 'About me'], ['project', 'Project'], ['environment', 'My setup'], ['other', 'Other']].map(([v, l]) => h('option', { value: v }, l)));
  const prof = h('textarea', { 'data-k': 'user_profile', rows: 12, class: 'profile-box', spellcheck: 'true',
    placeholder: 'Who you are and how you like answers: tone, length, format, level of detail, things to avoid, examples of writing you like…' }, s.user_profile || '');
  const prevBox = h('pre', { class: 'profile-preview hidden' });
  const preview = async () => {
    const val = k => $(`[data-k="${k}"]`, prof.closest('.set-layout') || document);
    const body = { user_profile: prof.value };
    for (const k of ['profile_enabled', 'memory_enabled']) { const el = val(k); if (el) body[k] = el.checked; }
    const b = val('profile_budget_tokens'); if (b) body.profile_budget_tokens = Number(b.value);
    const r = await api('/api/memory/profile', { method: 'POST', json: body });
    prevBox.textContent = r.text ? `${r.text}\n\n(about ${fmtNum(r.tokens)} tokens · ${r.facts} learned memories · sized for a ${fmtNum(r.ctx)}-token context)` : 'Nothing goes in: the profile is empty or turned off.';
    prevBox.classList.remove('hidden');
  };
  let data = { facts: [], summaries: [] };
  const fmtDay = t => new Date(t * 1000).toLocaleDateString();
  const draw = () => {
    const f = q.value.trim().toLowerCase();
    list.innerHTML = '';
    const facts = data.facts.filter(x => !f || x.text.toLowerCase().includes(f));
    if (!facts.length) list.append(h('p', { class: 'muted' }, data.facts.length ? 'No match.' : 'Nothing remembered yet. The model saves facts as you chat, and compacting a chat adds more.'));
    for (const x of facts) {
      const txt = h('span', { class: 'mem-text' }, x.text);
      const row = h('div', { class: 'mem-row' + (x.pinned ? ' pinned' : '') + (x.kind === 'lesson' ? ' lesson' : '') },
        h('button', { class: 'icon-btn sm' + (x.pinned ? ' on' : ''), html: I.pin, title: x.pinned ? 'Pinned: always in every chat' : 'Pin: always include in every chat',
          onclick: async () => { Object.assign(x, await api('/api/memory/' + x.id, { method: 'PUT', json: { pinned: !x.pinned } })); draw(); } }),
        txt, h('small', { title: ['preference', 'user'].includes(x.kind) ? 'Part of your profile: every model gets it in its system prompt' : '' },
          `${x.kind === 'lesson' ? 'lesson from review' : x.kind === 'user' ? 'about you' : x.kind}${['preference', 'user'].includes(x.kind) ? ' · every prompt' : ''} · ${fmtDay(x.updated)}`),
        h('button', { class: 'icon-btn sm', html: I.edit, title: 'Edit', onclick: () => {
          const inp = h('input', { type: 'text', value: x.text, class: 'mem-edit' });
          const done = async ok => { if (ok && inp.value.trim() && inp.value !== x.text) Object.assign(x, await api('/api/memory/' + x.id, { method: 'PUT', json: { text: inp.value.trim() } })); draw(); };
          inp.onkeydown = e => { if (e.key === 'Enter') done(true); if (e.key === 'Escape') { e.stopPropagation(); done(false); } };
          inp.onblur = () => done(true);
          txt.replaceWith(inp); inp.focus();
        } }),
        h('button', { class: 'icon-btn sm', html: I.trash, title: 'Forget', onclick: async () => { await api('/api/memory/' + x.id, { method: 'DELETE' }); data.facts = data.facts.filter(y => y !== x); draw(); refreshMemCount(); } }));
      list.append(row);
    }
    sums.innerHTML = '';
    if (!data.summaries.length) sums.append(h('p', { class: 'muted' }, 'No chat summaries yet.'));
    for (const x of data.summaries.slice(0, 200)) {
      sums.append(h('details', { class: 'mem-sum' }, h('summary', {}, h('b', {}, x.title), h('small', {}, fmtDay(x.created)),
        h('button', { class: 'icon-btn sm', html: I.trash, title: 'Delete this summary', onclick: async e => { e.preventDefault(); await api('/api/memory/summary/' + x.chat_id, { method: 'DELETE' }); data.summaries = data.summaries.filter(y => y !== x); draw(); } })),
        h('div', { class: 'mem-sum-body' }, x.summary + (x.open_tasks?.length ? '\n\nOpen tasks:\n' + x.open_tasks.map(t => '- ' + t).join('\n') : ''))));
    }
  };
  const load = async () => { data = await api('/api/memory'); draw(); };
  q.oninput = draw;
  add.onkeydown = async e => {
    if (e.key !== 'Enter' || !add.value.trim()) return;
    const r = await api('/api/memory', { method: 'POST', json: { text: add.value.trim(), kind: addKind.value } });
    toast(r.how === 'added' ? 'Remembered' : 'Updated an existing memory'); add.value = ''; load(); refreshMemCount();
  };
  load();
  const pctSel = select('auto_compact_at', String(s.auto_compact_at ?? 0.75), [['0', 'Off'], ['0.6', 'At 60% full'], ['0.75', 'At 75% full'], ['0.85', 'At 85% full']]);
  pctSel.dataset.num = '1';
  return [
    h('label', { class: 'mem-h' }, 'About you'),
    h('p', { class: 'muted', style: 'font-size:12.5px;margin:0 0 6px' }, 'Every model gets this in its system prompt: whichever local model you load, plus Claude Fable and Claude Opus. Write who you are and how you like answers. Preferences you state in chat are learned and added below it.'),
    prof,
    h('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px' },
      h('button', { class: 'btn ghost sm', onclick: preview }, 'Preview what every model sees'),
      h('button', { class: 'btn ghost sm', title: 'Write in the Aero starter profile again', onclick: async () => { const d = await api('/api/settings/defaults'); prof.value = d.user_profile || ''; } }, 'Restore starter text')),
    prevBox,
    toggle('profile_enabled', s.profile_enabled, 'Put my profile, learned preferences and recent chats in every model\'s system prompt'),
    toggle('learn_preferences', s.learn_preferences, 'Learn my preferences from what I say (the router notices things like "keep it shorter" and saves them)'),
    field('Profile budget (tokens)', num('profile_budget_tokens', s.profile_budget_tokens, 100), 'Your own text first, then learned preferences and facts about you, then recent chats. Also capped at 1/8 of the context.'),
    toggle('memory_enabled', s.memory_enabled, 'Use long-term memory in every chat'),
    toggle('auto_memorize', s.auto_memorize, 'When I leave a chat, summarize it into memory (runs while the model is idle)'),
    h('div', { class: 'row2' },
      field('Auto-compact', pctSel, 'When the context fills up this much, the chat is saved to memory and continues in a fresh one. Also happens mid-task during long agent runs.'),
      field('Memory budget per chat (tokens)', num('memory_budget_tokens', s.memory_budget_tokens, 100), 'Pinned and most relevant memories first. Also capped at 10% of the context.')),
    h('label', { class: 'mem-h' }, 'Remembered facts and lessons'),
    h('div', { class: 'mem-tools' }, addKind, add, q), list,
    h('label', { class: 'mem-h' }, 'Past chat summaries'), sums,
    h('p', { class: 'muted', style: 'font-size:12.5px' }, 'Stored in C:\\Aero\\data\\memory.json. Passwords, keys and tokens are never saved. Deleting a chat removes its summary but keeps the facts.'),
  ];
}

function field(label, input, help) { return h('div', { class: 'field' }, h('label', {}, label), input, help ? h('small', {}, help) : null); }
function num(k, v, step) { return h('input', { type: 'number', 'data-k': k, value: v, step }); }
function select(k, v, opts) { return h('select', { 'data-k': k }, opts.map(([val, lab]) => h('option', { value: val, selected: String(val) === String(v) }, lab))); }
/** A select whose values are JSON (true / false / "auto"). */
function jselect(k, v, opts) { return h('select', { 'data-k': k, 'data-json': '1' }, opts.map(([val, lab]) => h('option', { value: JSON.stringify(val), selected: JSON.stringify(val) === JSON.stringify(v) }, lab))); }
function toggle(k, v, label) { return h('label', { class: 'toggle field' }, h('input', { type: 'checkbox', 'data-k': k, checked: !!v }), label); }

boot();
