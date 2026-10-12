// Aero v1.2: workspaces. Each workspace is a panel beside the chat for one kind of work (Code, Mobile apps, Create,
// Diagrams, Review & test, Debug & reverse, Finance, Library, Memory+, Big models), built from the integration packs
// (aero/integrations). A request that needs a pack opens its workspace by itself (the "workspace" event); the panel
// shows what is installed (everything is set up with Aero; Install buttons only for what failed), the pack's live
// state and its results.
'use strict';

Object.assign(I, {
  code: '<svg viewBox="0 0 24 24"><path d="m8 7-5 5 5 5M16 7l5 5-5 5M14 4l-4 16"/></svg>',
  mobile: '<svg viewBox="0 0 24 24"><rect x="7" y="2.5" width="10" height="19" rx="2"/><path d="M11 18.5h2"/></svg>',
  create: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/></svg>',
  diagram: '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="5" rx="1"/><rect x="14" y="16" width="7" height="5" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><path d="M6.5 8v5.5h11V16M10 5.5h4"/></svg>',
  review: '<svg viewBox="0 0 24 24"><path d="M9 11l2 2 4-4"/><path d="M20 12a8 8 0 1 1-3-6.2"/></svg>',
  debug: '<svg viewBox="0 0 24 24"><rect x="8" y="6" width="8" height="13" rx="4"/><path d="M12 6V3M4 9h4M16 9h4M4 15h4M16 15h4M9 3.5 10 5M15 3.5 14 5"/></svg>',
  finance: '<svg viewBox="0 0 24 24"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>',
  library: '<svg viewBox="0 0 24 24"><path d="M4 4h4v16H4zM10 4h4v16h-4z"/><path d="m16 5 3.8-1 3 15-3.9 1z"/></svg>',
  memory: '<svg viewBox="0 0 24 24"><path d="M9 3a4 4 0 0 0-4 4 4 4 0 0 0-1 7.5A4 4 0 0 0 9 21h1V3zM15 3a4 4 0 0 1 4 4 4 4 0 0 1 1 7.5A4 4 0 0 1 15 21h-1V3z"/></svg>',
  models: '<svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="8.5" y="8.5" width="7" height="7" rx="1"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2"/></svg>',
});

const WS = { list: [], open: null, timer: null, bigRepo: '' };

async function wsInit(tries = 0) {
  try { WS.list = (await api('/api/integrations')).workspaces || []; } catch { WS.list = []; }
  renderWsList();
  if (!WS.list.length && tries < 6) setTimeout(() => wsInit(tries + 1), 1500 * (tries + 1));   // server still starting
}

function renderWsList() {
  const box = $('#wsList'); if (!box) return;
  box.innerHTML = '';
  for (const w of WS.list) {
    const ready = (w.packs || []).some(p => p.ready);
    box.append(h('button', { class: 'sb-mem ws-btn' + (WS.open === w.id ? ' on' : ''), title: w.blurb, onclick: () => openWorkspace(w.id) },
      h('span', { class: 'ico', html: I[w.icon] || I.tools }), h('span', {}, w.name),
      h('small', { class: 'ws-dot ' + (ready ? 'ok' : 'off'), title: ready ? 'Ready' : 'Needs setup' })));
  }
}

// every integration ships with Aero: while the background setup runs (integrations/bundle.py), one line under the
// workspace list says how far it is
async function bundleStatus() {
  let st; try { st = await api('/api/integrations/bundle'); } catch { return setTimeout(bundleStatus, 15000); }
  const box = $('#wsList'); if (!box) return;
  $('.ws-bundle', box.parentElement)?.remove();
  const items = st.items || [], done = items.filter(x => x.state === 'done').length;
  const failed = items.filter(x => x.state === 'failed');
  const cur = items.find(x => x.state === 'running');
  let el = null;
  if (st.running && done < items.length) {
    el = h('div', { class: 'ws-bundle', title: 'Every integration comes with Aero; it finishes setting them up in the background.' },
      h('small', {}, `Setting up integrations · ${done} of ${items.length}` + (cur ? ` · ${cur.label}` : '')),
      h('div', { class: 'progress' }, h('i', { style: `width:${(st.done_mb / Math.max(1, st.total_mb) * 100).toFixed(1)}%` })));
  } else if (failed.length) {
    el = h('div', { class: 'ws-bundle bad', title: failed.map(x => `${x.label}: ${x.error || 'failed'}`).join('\n') },
      h('small', {}, `${failed.length} integration${failed.length === 1 ? '' : 's'} didn't install `),
      h('button', { class: 'btn ghost sm', onclick: async () => { await api('/api/integrations/bundle/retry', { method: 'POST' }); setTimeout(bundleStatus, 1500); } }, 'Retry'));
  }
  if (el) box.after(el);
  if (st.running) setTimeout(bundleStatus, 8000);
  else if (done && WS.list.length) wsInit();
}

function closeWorkspace() {
  WS.open = null; clearInterval(WS.timer);
  $('#ws')?.classList.add('hidden'); document.body.classList.remove('ws-open'); renderWsList();
}

async function openWorkspace(id, why) {
  WS.open = id; renderWsList();
  const pane = $('#ws'); pane.classList.remove('hidden'); document.body.classList.add('ws-open');
  clearInterval(WS.timer);
  await renderWorkspace(id, why);
  if (['mobile', 'debug', 'memory'].includes(id)) WS.timer = setInterval(() => { if (WS.open === id && !document.hidden) renderWorkspace(id, null, true); }, 5000);
}

async function renderWorkspace(id, why, quiet) {
  const pane = $('#ws');
  let d;
  try { d = await api('/api/integrations/ws/' + id); } catch (e) { if (WS.open === id) pane.innerHTML = `<div class="err-msg">${esc(e.message)}</div>`; return; }
  // another workspace opened while this answer was on its way (Memory+'s 5-second refresh landed over Create in 1.2.1)
  if (WS.open !== id) return;
  if (quiet && pane.dataset.ws === id) { const live = $('.ws-live', pane); if (live) { live.replaceWith(liveBody(id, d)); } return; }
  pane.dataset.ws = id; pane.innerHTML = '';
  const w = d.workspace || {};
  pane.append(h('div', { class: 'ws-head' }, h('span', { class: 'ico', html: I[w.icon] || I.tools }),
    h('div', { class: 'grow' }, h('b', {}, w.name), h('small', { class: 'muted' }, w.blurb)),
    h('button', { class: 'icon-btn', title: 'Close', onclick: closeWorkspace, html: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>' })));
  if (why) pane.append(h('div', { class: 'callout ws-why' }, why));
  pane.append(liveBody(id, d));
  for (const p of d.packs) pane.append(packCard(p));
}

function packCard(p) {
  const reqs = h('div', { class: 'ws-reqs' });
  const log = h('div', { class: 'logbox hidden' }); const bar = h('div', { class: 'progress hidden' }, h('i'));
  for (const r of p.requirements || []) {
    const btn = !r.ok && r.installable ? h('button', { class: 'btn sm', onclick: async e => {
      if (!confirm(`Install ${r.label}${r.size_mb ? ` (about ${r.size_mb >= 1024 ? (r.size_mb / 1024).toFixed(1) + ' GB' : r.size_mb + ' MB'})` : ''} into Aero's folder?`)) return;
      e.target.disabled = true; e.target.textContent = 'Installing…';
      const { err } = await runJob('/api/integrations/install', { pack: p.id, requirement: r.id }, log, bar);
      if (err) toast(err, true); else toast(`${r.label}: installed`);
      await wsInit(); renderWorkspace(WS.open);
    } }, 'Install') : null;
    reqs.append(h('div', { class: 'ws-req ' + (r.ok ? 'ok' : r.optional ? 'opt' : 'miss') },
      h('span', { class: 'ws-dot ' + (r.ok ? 'ok' : 'off') }), h('span', { class: 'grow' }, r.label, h('small', { class: 'muted' }, ' · ' + (r.detail || (r.how ? r.how : '')))), btn));
  }
  // the card opens with an arrow: what it does, things to ask (one click puts them in the message box), its tools,
  // what it needs, and where it comes from
  const ex = (p.examples || []).length ? h('div', { class: 'ws-ex' }, ...p.examples.map(t => h('button', { title: 'Put this in the message box', onclick: () => { const ta = $('#input'); ta.value = t; ta.focus(); ta.dispatchEvent(new Event('input')); } }, t))) : null;
  const tl = (p.tools || []).filter(t => t !== 'use_skill');
  const tools = tl.length ? h('div', { class: 'ws-tools' }, ...tl.flatMap(t => [h('code', {}, t), h('span', { class: 'muted' }, typeof toolInfo === 'function' ? toolInfo(t).desc : '')])) : null;
  return h('details', { class: 'ws-pack', open: !p.ready },
    h('summary', {}, h('b', {}, p.name), h('span', { class: 'chip ' + (p.ready ? 'ok' : '') }, p.ready ? 'ready' : 'needs setup')),
    h('p', { class: 'muted' }, p.summary),
    ex ? h('div', { class: 'ws-label' }, 'Try asking') : null, ex,
    h('div', { class: 'ws-label' }, 'What it needs'), reqs, bar, log,
    tools ? h('div', { class: 'ws-label' }, 'Its tools') : null, tools,
    (p.skills || []).length ? h('small', { class: 'muted' }, 'Skills: ' + p.skills.join(', ')) : null,
    p.notes ? h('p', { class: 'muted ws-note' }, p.notes) : null,
    h('small', { class: 'muted' }, 'Built in from ', h('a', { href: (p.source || '').split(' ')[0], target: '_blank', rel: 'noopener' }, (p.source || '').replace('https://github.com/', '').split(' ')[0]), ` · ${p.license}`));
}

// a section of a workspace panel that opens and closes with an arrow
function wsSec(title, body, { open = true, note = '' } = {}) {
  return h('details', { class: 'ws-sec', open }, h('summary', {}, h('span', {}, title), note ? h('span', { class: 'muted' }, note) : null), ...[].concat(body).filter(Boolean));
}

async function wsRun(tool, args, btn, label) {
  if (btn) { btn.disabled = true; btn.dataset.t = btn.textContent; btn.textContent = label || 'Working…'; }
  try { const r = await api('/api/integrations/run', { method: 'POST', json: { tool, args } }); if (r.error) toast(r.text?.slice(0, 300) || 'Failed', true); return r; }
  catch (e) { toast(e.message, true); return null; }
  finally { if (btn) { btn.disabled = false; btn.textContent = btn.dataset.t; } }
}

const fileUrl = (kind, name) => `/api/integrations/file/${kind}/${encodeURIComponent(name)}`;

function liveBody(id, d) {
  const box = h('div', { class: 'ws-live' });
  const pk = Object.fromEntries((d.packs || []).map(p => [p.id, p]));
  const P = x => pk[x]?.panel || {};
  if (id === 'library') box.append(libraryBox(P('awesome-selfhosted')));
  if (id === 'create') {
    const ta = h('textarea', { rows: 3, placeholder: 'Describe the image: subject, style, lighting, composition…' });
    const size = h('select', {}, ...['square', 'portrait', 'landscape', 'small'].map(s => h('option', { value: s }, s)));
    const out = h('div', { class: 'ws-gallery' });
    const go = h('button', { class: 'btn sm', onclick: async e => {
      if (!ta.value.trim()) return;
      const r = await wsRun('image_generate', { prompt: ta.value.trim(), size: size.value }, e.target, 'Generating…');
      if (r && !r.error) renderWorkspace('create');
    } }, 'Generate');
    box.append(h('div', { class: 'ws-form' }, ta, h('div', { class: 'row' }, size, go)));
    for (const g of P('image').gallery || []) out.append(h('figure', {}, h('img', { src: fileUrl('images', g.file), loading: 'lazy', onclick: () => viewImage(fileUrl('images', g.file)) }), h('figcaption', {}, (g.prompt || '').slice(0, 90), h('small', { class: 'muted' }, g.seconds ? ` · ${g.seconds}s` : ''))));
    box.append(out);
  }
  if (id === 'diagrams') {
    const g = P('diagram-design').gallery || [];
    const frame = h('iframe', { class: 'ws-frame', sandbox: 'allow-scripts', title: 'Diagram preview' });
    const list = h('div', { class: 'ws-list2' });
    const show = x => { frame.src = fileUrl('diagrams', x.file); $$('.on', list).forEach(e => e.classList.remove('on')); };
    for (const x of g) list.append(h('div', { class: 'ws-row' }, h('a', { href: '#', onclick: e => { e.preventDefault(); show(x); e.target.classList.add('on'); } }, x.title),
      h('small', { class: 'muted' }, ` ${x.check ? '✓ checked' : '⚠ check'} · ${x.updated}`),
      h('button', { class: 'btn ghost sm', onclick: e => wsRun('diagram_export', { name: x.name, format: 'svg' }, e.target, '…').then(r => r && !r.error && toast('SVG saved')) }, 'SVG'),
      h('button', { class: 'btn ghost sm', onclick: e => wsRun('diagram_export', { name: x.name, format: 'png' }, e.target, '…').then(r => r && !r.error && toast('PNG saved')) }, 'PNG')));
    if (g.length) { box.append(frame, list); show(g[0]); } else box.append(h('p', { class: 'muted' }, 'No diagrams yet. Ask for one ("draw the architecture of …"): the model writes it with the diagram-design skill and it appears here.'));
  }
  if (id === 'mobile') {
    const pv = P('flutter').preview || {};
    if (pv.url) box.append(h('div', { class: 'ws-device' }, h('iframe', { class: 'ws-frame phone', src: pv.url, title: 'Flutter preview' })),
      h('div', { class: 'row' }, ...[['Hot reload', 'reload'], ['Hot restart', 'restart'], ['Stop', 'stop']].map(([t, a]) => h('button', { class: 'btn ghost sm', onclick: e => wsRun('flutter_preview', { action: a }, e.target, '…').then(() => renderWorkspace('mobile', null, true)) }, t))));
    else box.append(h('p', { class: 'muted' }, 'No app running. Ask for an app ("build me a mobile app that …") or run one: the live preview shows here with hot reload.'));
    const an = P('flutter').analyze, te = P('flutter').test;
    if (te) box.append(h('div', { class: 'callout' }, `Tests: ${te.passed} passed, ${te.failed} failed · ${te.at}`, ...(te.failures || []).slice(0, 5).map(f => h('div', { class: 'err-msg' }, `${f.name}: ${f.error}`))));
    if (an?.diagnostics?.length) box.append(diagList(an.diagnostics));
    if (pv.log?.length) box.append(h('details', {}, h('summary', {}, 'App log'), h('pre', { class: 'logbox' }, pv.log.join('\n'))));
  }
  if (id === 'code') {
    const c = P('essentials');
    const inp = h('input', { type: 'text', placeholder: 'Project folder (blank = working folder)' });
    box.append(h('div', { class: 'ws-form row' }, inp, h('button', { class: 'btn sm', onclick: e => wsRun('project_check', { path: inp.value || '.' }, e.target, 'Checking…').then(() => renderWorkspace('code')) }, 'Check project')));
    if (c.last_check) box.append(h('div', { class: 'callout' }, `${c.last_check.path} · ${c.last_check.kinds.join(', ')} · ${c.last_check.at}`, ...c.last_check.ran.map(r => h('div', {}, `${r.step}: ${r.tool} exit ${r.rc}, ${r.found} findings`))), diagList(c.last_check.diagnostics || []));
  }
  if (id === 'review') {
    const l = P('open-code-review').last;
    if (l) {
      const by = {}; for (const c of l.comments || []) (by[c.severity || 'low'] = by[c.severity || 'low'] || []).push(c);
      box.append(h('div', { class: 'callout' }, `${l.path} · ${l.status} · ${(l.comments || []).length} comments · ${l.at}`));
      for (const s of ['critical', 'high', 'medium', 'low']) for (const c of by[s] || []) box.append(h('div', { class: 'ws-diag ' + s }, h('b', {}, s), ` ${c.path}:${c.start_line || '?'} `, h('span', {}, c.content || ''), c.suggestion_code ? h('pre', {}, c.suggestion_code) : null));
    } else box.append(h('p', { class: 'muted' }, 'No review yet. Ask "review my changes" in a git project.'));
  }
  if (id === 'debug') {
    const r = P('raddebugger');
    if (r.session?.running) box.append(h('div', { class: 'callout' }, `Debugging ${r.session.target}`), r.state ? h('pre', { class: 'logbox' }, Object.entries(r.state).filter(([k]) => k !== 'raw').map(([k, v]) => `${k}: ${v}`).join('\n')) : null);
    else box.append(h('p', { class: 'muted' }, 'No debugging session. Ask the model to debug a program: it sets breakpoints and steps through it, and the state shows here.'));
  }
  if (id === 'finance') {
    const sk = P('financial-services').skills || [];
    box.append(h('p', { class: 'muted' }, 'Drafts for professional review. Aero never gives investment advice or executes trades.'));
    const list = h('div', { class: 'ws-list2' });
    for (const s of sk) list.append(h('div', { class: 'ws-row' }, h('a', { href: '#', title: s.description, onclick: e => { e.preventDefault(); const ta = $('#input'); ta.value = `Use the ${s.name} skill to `; ta.focus(); } }, s.name), h('small', { class: 'muted' }, ' ' + (s.description || '').slice(0, 110))));
    box.append(list);
  }
  if (id === 'memory') {
    const m = P('hindsight');
    box.append(h('p', { class: 'muted' }, m.running ? `Hindsight is running (${m.url}).` : 'Hindsight starts on its first use.'));
    if (m.banks) box.append(h('pre', { class: 'logbox' }, JSON.stringify(m.banks, null, 1).slice(0, 3000)));
  }
  if (id === 'models') box.append(bigModelsBox(P('bigmodels')));
  return box;
}

function diagList(ds) {
  const box = h('div', { class: 'ws-diags' });
  for (const d of ds.slice(0, 200)) box.append(h('div', { class: 'ws-diag ' + (d.severity || '') }, h('code', {}, `${d.file}:${d.line ?? '?'}`), ` [${d.tool}${d.code ? ' ' + d.code : ''}] `, d.message));
  if (!ds.length) box.append(h('p', { class: 'muted' }, 'No findings.'));
  return box;
}

function libraryBox(sh) {
  const q = h('input', { type: 'search', placeholder: 'Search: "async http client", "irc server", "find large files"…' });
  const src = h('select', {}, h('option', { value: '' }, 'All lists'), h('option', { value: 'awesome-python' }, 'awesome-python'), h('option', { value: 'awesome-selfhosted' }, 'awesome-selfhosted'), h('option', { value: 'secret-knowledge' }, 'the-book-of-secret-knowledge'));
  const tag = h('select', {}, h('option', { value: '' }, 'Any tag'), ...(sh.tags || []).map(([t, n]) => h('option', { value: t }, `${t} (${n})`)));
  const out = h('div', { class: 'ws-list2' });
  const go = async () => {
    const r = await api(`/api/library/search?q=${encodeURIComponent(q.value)}&source=${src.value}&tag=${encodeURIComponent(tag.value)}`).catch(e => ({ results: [], error: e.message }));
    out.innerHTML = '';
    for (const x of r.results || []) {
      if (x.code !== undefined) out.append(h('div', { class: 'ws-row liner' }, h('b', {}, x.title), h('small', { class: 'muted' }, ` · ${x.tool}`), ...(x.risks || []).map(k => h('span', { class: 'chip bad' }, k)), h('pre', {}, x.code)));
      else out.append(h('div', { class: 'ws-row' }, h('a', { href: x.url, target: '_blank', rel: 'noopener' }, x.name), h('small', { class: 'muted' }, ` ${x.desc || ''} · ${x.category || (x.tags || []).join(', ') || x.section || ''}${x.licenses?.length ? ' · ' + x.licenses.join(', ') : ''}`)));
    }
    if (!(r.results || []).length) out.append(h('p', { class: 'muted' }, 'Nothing found.'));
  };
  q.onkeydown = e => { if (e.key === 'Enter') go(); };
  return h('div', {}, h('div', { class: 'ws-form row' }, q, src, tag, h('button', { class: 'btn sm', onclick: go }, 'Search')), out,
    h('small', { class: 'muted' }, 'awesome-python (CC BY 4.0) · awesome-selfhosted (CC BY-SA 3.0, downloaded on request) · the-book-of-secret-knowledge (MIT). Risky one-liners are tagged and never run by Aero.'));
}

function bigModelsBox(p) {
  const box = h('div');
  // the normal way: search any model, Aero picks the quant and the tuner places it
  const q = h('input', { type: 'search', placeholder: 'Any model, any size: "llama 3.3 70b", "qwen3 235b", "gemma 3 27b"…' });
  const find = () => { showLauncher(); const i = $('#hfQuery'); i.value = q.value; i.dispatchEvent(new Event('input')); };
  q.onkeydown = e => { if (e.key === 'Enter') find(); };
  box.append(wsSec('Find and run any model', h('div', {},
    h('p', {}, 'Big models are part of normal loading now. Search Hugging Face for any model: Aero picks the best quant for your GPU and RAM, ' +
      'makes one itself when nobody has published a GGUF (with an importance matrix for low-bit quants), and the tuner measures where every ' +
      'part goes: whole layers, only the feed-forward weights, or MoE experts in RAM.'),
    h('div', { class: 'ws-form row' }, q, h('button', { class: 'btn sm', onclick: find }, 'Search'))), { note: 'recommended' }));

  // manual offload plans for a GGUF already on disk
  const sel = h('select', {}, ...(p.ggufs || []).map(g => h('option', { value: g.path }, `${g.name} (${g.gb} GB)`)));
  const budget = h('input', { type: 'number', step: '0.5', min: '0.5', placeholder: 'VRAM GB (blank = free VRAM)', style: 'width:170px' });
  const out = h('div');
  const planBtn = h('button', { class: 'btn sm', onclick: async e => {
    e.target.disabled = true; out.innerHTML = '';
    try {
      const r = await api('/api/bigmodel/plan', { method: 'POST', json: { path: sel.value, budget_gb: +budget.value || 0 } });
      out.append(h('p', { class: 'muted' }, `${r.model}: ${r.size_gb} GB, ${r.layers} layers${r.moe ? ', mixture of experts' : ''}; budget ${r.budget_mb.toLocaleString()} MB VRAM`));
      if (r.draft && !r.draft.path) out.append(h('div', { class: 'callout' }, `A draft model makes this much faster (measured: 70B from 1.0 to 4.85 tok/s). `,
        h('button', { class: 'btn sm', onclick: async () => { const lb = h('div', { class: 'logbox' }); out.append(lb); const { err } = await runJob('/api/bigmodel/draft', { path: sel.value }, lb); if (err) toast(err, true); else { toast('Draft model ready'); e.target.click(); } } }, `Download ${r.draft.repo.split('/')[1]} ${r.draft.quant}`)));
      else if (r.draft?.path) out.append(h('p', { class: 'muted' }, `With the draft model ${r.draft.path.split(/[\\/]/).pop()} (speculative decoding).`));
      for (const x of r.plans) out.append(h('div', { class: 'ws-row' }, h('b', {}, x.strategy), ` GPU ${x.gpu_mb.toLocaleString()} MB · RAM ${x.cpu_mb.toLocaleString()} MB · ≈${x.est_tg} tok/s (estimate) `, h('small', { class: 'muted' }, x.note),
        h('button', { class: 'btn ghost sm', onclick: async () => { const lb = h('div', { class: 'logbox' }); out.append(lb); const { err } = await runJob('/api/bigmodel/load', { path: sel.value, cfg: x.cfg, spec: x.spec || null, strategy: x.strategy }, lb); if (err) toast(err, true); else toast('Loaded'); } }, 'Load')));
    } catch (err) { toast(err.message, true); }
    e.target.disabled = false;
  } }, 'Plan');
  box.append(wsSec('Manual offload plan for a GGUF', h('div', {},
    h('p', { class: 'muted' }, 'Skip the tuner and pick a placement yourself: every plan that fits the VRAM you give, with an estimated speed.'),
    (p.ggufs || []).length ? h('div', { class: 'ws-form row' }, sel, budget, planBtn) : h('p', { class: 'muted' }, 'No GGUF models on disk yet.'), out), { open: false, note: 'advanced' }));

  // AirLLM
  const repo = h('input', { type: 'text', placeholder: 'Hugging Face repo, e.g. Qwen/Qwen2.5-7B-Instruct, or a folder', value: WS.bigRepo || '' });
  WS.bigRepo = '';
  const comp = h('select', {}, h('option', { value: 'none' }, 'full precision'), h('option', { value: '8bit' }, '8-bit'), h('option', { value: '4bit' }, '4-bit'));
  const lb = h('div', { class: 'logbox hidden' });
  box.append(wsSec('AirLLM: full precision, layer by layer', h('div', {},
    h('p', {}, 'Runs the original weights of any size on a small GPU by streaming one layer at a time from disk. Expect seconds per token: use it when a quantized GGUF won\'t do.'),
    h('p', { class: 'muted' }, p.airllm?.installed ? `Ready (${p.airllm.version}).` : 'Its environment is being set up in the background (PyTorch, a few GB); this unlocks when it\'s done.'),
    h('div', { class: 'ws-form row' }, repo, comp, h('button', { class: 'btn sm', disabled: !p.airllm?.installed, onclick: async () => { if (!repo.value.trim()) return; const { err } = await runJob('/api/bigmodel/airllm/load', { model: repo.value.trim(), compression: comp.value }, lb); if (err) toast(err, true); else toast('AirLLM model ready'); } }, 'Run with AirLLM')), lb),
    { open: !!repo.value, note: p.airllm?.installed ? 'ready' : 'setting up' }));
  if ((p.measured || []).length)
    box.append(wsSec('Measured on this PC', p.measured.map(m => h('div', { class: 'callout' }, `${m.model} · budget ${m.budget_mb} MB`, ...(m.results || []).map(r => h('div', {}, r.ok ? `${r.strategy}: ${r.tg} tok/s generate, ${r.pp} tok/s prompt, ${r.vram_mb} MB VRAM, load ${r.load_s}s` : `${r.strategy}: failed (${r.error})`)))), { open: false }));
  return box;
}

// a request that needs a pack opened its workspace (pipeline.integration_context)
function wsOnEvent(ev) {
  if (!ev.id) return;
  const w = WS.list.find(x => x.id === ev.id);
  if (WS.open !== ev.id) openWorkspace(ev.id, `Loaded for this request: ${(ev.packs || []).join(', ')}`);
  if (w) toast(`${w.name} workspace: ${(ev.packs || []).join(', ')}`);
}

// a tool result with something to look at: an image, a diagram, a live app preview
function wsPreview(card, pv) {
  if (!pv || $('.ws-inline', card)) return;
  let el = null;
  if (pv.kind === 'image') el = h('img', { class: 'shot ws-inline', src: fileUrl('images', pv.file), onclick: () => viewImage(fileUrl('images', pv.file)) });
  else if (pv.kind === 'diagram') el = h('iframe', { class: 'ws-inline ws-frame', sandbox: 'allow-scripts', src: fileUrl('diagrams', pv.file), title: 'Diagram' });
  else if (pv.kind === 'web' && pv.url) el = h('a', { class: 'ws-inline btn ghost sm', href: '#', onclick: e => { e.preventDefault(); openWorkspace('mobile'); } }, 'Show the live preview');
  if (el) card.append(el);
  if (pv.kind === 'diagram' && WS.open === 'diagrams') renderWorkspace('diagrams');
  if (pv.kind === 'image' && WS.open === 'create') renderWorkspace('create');
}

// the Workspaces group folds away with its arrow (remembered in this browser); the open one stays listed
function wsFold(folded) {
  const b = $('#wsToggle'); if (!b) return;
  b.setAttribute('aria-expanded', String(!folded)); $('#wsList')?.classList.toggle('folded', folded);
  try { localStorage.setItem('aero.wsFolded', folded ? '1' : ''); } catch { }
}
$('#wsToggle')?.addEventListener('click', () => wsFold($('#wsToggle').getAttribute('aria-expanded') === 'true'));
try { if (localStorage.getItem('aero.wsFolded')) wsFold(true); } catch { }

setTimeout(wsInit, 300);
setTimeout(bundleStatus, 2500);
