/* Performance Lab: HAPO profiles picked from measured tuning trials, Quick/Deep Tune, benchmarks and exports.
   Every number on this screen was measured on this PC; anything that was not measured says so.
   Uses the helpers in app.js (h, api, sseFetch, modal, toast, S, Scene, loadModel, runLoad, syncBusy …). */
const Lab = { tab: 'profiles', mid: null, info: null, reports: [], busy: false, progress: null };
const LAB_TABS = [['profiles', 'Profiles'], ['bench', 'Benchmarks'], ['pc', 'This PC']];
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function openLab(tab) {
  if (typeof tab === 'string') Lab.tab = tab;
  try { S.models = await api('/api/models'); } catch { }
  const ok = (S.models || []).filter(m => m.exists);
  if (!ok.some(m => m.id === Lab.mid)) Lab.mid = S.st?.engine?.model?.id || ok[0]?.id || null;
  modal('Performance Lab', h('div', { class: 'lab' }), true);
  await renderLab();
}

async function renderLab() {
  const body = $('#modalBody .lab');
  if (!body) return;
  const mid = Lab.mid;
  const [info, reps] = await Promise.all([
    mid ? api(`/api/hapo/${mid}`).catch(e => ({ error: e.message })) : null,
    api('/api/bench').catch(() => ({ reports: [] }))]);
  Lab.info = info; Lab.reports = reps.reports || [];
  body.innerHTML = '';
  const models = (S.models || []).filter(m => m.exists);
  const sel = h('select', { class: 'lab-model', onchange: e => { Lab.mid = e.target.value; renderLab(); } },
    ...models.map(m => h('option', { value: m.id, selected: m.id === mid }, m.name + (m.tuned ? '' : ' · not tuned'))));
  const tabs = h('div', { class: 'seg lab-tabs' }, ...LAB_TABS.map(([k, lab]) =>
    h('button', { class: k === Lab.tab ? 'on' : '', onclick: () => { Lab.tab = k; renderLab(); } }, lab)));
  body.append(h('div', { class: 'lab-head' }, models.length ? sel : null,
    info && !info.error ? h('span', { class: 'lab-state' + (info.loaded ? ' on' : '') }, info.loaded ? 'Loaded now' : 'Not loaded') : null,
    S.settings?.strict_offline ? h('span', { class: 'lab-state off' }, 'Strict offline') : null,
    h('div', { class: 'grow' }), tabs));
  const pane = h('div', { class: 'lab-pane client' });
  body.append(pane);
  if (!mid) pane.append(h('div', { class: 'lab-empty' }, h('h3', {}, 'No model yet'), h('p', {}, 'Add a model from the launcher first.')));
  else if (info?.error) pane.append(h('div', { class: 'err-msg' }, info.error));
  else ({ profiles: labProfiles, bench: labBench, pc: labPC })[Lab.tab](pane, info);
  if (mid && info && !info.error) body.append(h('div', { class: 'lab-foot' },
    h('small', { class: 'muted' }, 'Exports include the fingerprint, every profile and the latest benchmark of each kind.'),
    h('div', { class: 'grow' }),
    h('a', { class: 'btn ghost sm', href: `/api/lab/report?mid=${encodeURIComponent(mid)}&fmt=md`, download: '' }, 'Export Markdown'),
    h('a', { class: 'btn ghost sm', href: `/api/lab/report?mid=${encodeURIComponent(mid)}&fmt=json`, download: '' }, 'Export JSON')));
  if (Lab.progress) body.querySelector('.lab-progress-slot')?.append(Lab.progress.el);
}

// ---------------------------------------------------------------- profiles
function labStat(v, unit, label) {
  return h('div', { class: 'lstat' }, h('b', {}, v ?? '–', unit ? h('small', {}, ' ' + unit) : null), h('span', {}, label));
}

function profCard(p, info) {
  const c = h('div', { class: 'prof' + (p.active ? ' active' : '') + (p.measured ? '' : ' nm') });
  c.append(h('div', { class: 'prof-h' }, h('b', {}, p.name),
    p.active ? h('span', { class: 'badge rec' }, 'In use') : null,
    p.pareto ? h('span', { class: 'badge', title: 'On the Pareto front: no other trial is faster, longer and leaner at once' }, 'Pareto') : null));
  if (p.measured) {
    c.append(h('div', { class: 'prof-nums' },
      labStat(p.tg, 'tok/s', 'generate'), labStat(fmtNum(Math.round(p.pp || 0)), 'tok/s', 'prompt'),
      labStat(p.ctx ? fmtCtx(p.ctx) : '–', '', 'context'),
      labStat(p.mem_mb ? (p.mem_mb / 1024).toFixed(1) : 'not measured', p.mem_mb ? 'GB' : '', info.fingerprint?.hardware?.gpu ? 'VRAM' : 'RAM')),
      h('div', { class: 'prof-cfg' }, p.desc || ''),
      p.trial ? h('small', { class: 'muted' }, `Trial #${p.trial}` + (p.runs > 1 ? `, averaged over ${p.runs} runs` : '')) : null);
  } else {
    c.append(h('div', { class: 'prof-nm' }, 'Not measured'), h('small', { class: 'muted' }, p.why || ''));
  }
  c.append(h('small', { class: 'prof-rule' }, p.rule));
  if (p.measured && !p.active) c.append(h('button', { class: 'btn sm', disabled: Lab.busy, onclick: () => labApply(p.id) }, 'Use this profile'));
  return c;
}

async function labReload(note) {
  const info = Lab.info;
  if (!info?.loaded) { toast(note + ' It will be used the next time you load this model.'); return renderLab(); }
  closeModal();
  const r = await runLoad({ id: Lab.mid }, { model: info.model.name });
  if (r) setTimeout(() => openLab('profiles'), 300);
}

async function labApply(goal) {
  try { await api('/api/hapo/apply', { method: 'POST', json: { id: Lab.mid, profile: goal } }); }
  catch (e) { return toast(e.message, true); }
  const name = goal === 'tuner' ? "the tuner's pick" : (Lab.info.profiles.find(p => p.id === goal)?.name || goal);
  labReload(`Switched to ${name}.`);
}

async function labRestore() {
  try { await api('/api/hapo/restore', { method: 'POST', json: { id: Lab.mid } }); }
  catch (e) { return toast(e.message, true); }
  labReload('Restored the previous profile.');
}

async function labTune(depth) {
  if (Lab.busy) return;
  closeModal();
  await loadModel(Lab.mid, true, { depth });
  openLab('profiles');
}

function tuneButtons() {
  return h('div', { class: 'lab-actions' },
    h('button', { class: 'btn', disabled: Lab.busy, onclick: () => labTune('short') }, 'Quick Tune', h('small', {}, '10 trials')),
    h('button', { class: 'btn ghost', disabled: Lab.busy, onclick: () => labTune('long') }, 'Deep Tune', h('small', {}, '50 trials with repeats')));
}

function labProfiles(pane, info) {
  if (!info.tuned) {
    pane.append(h('div', { class: 'lab-empty' }, h('h3', {}, 'Not measured yet'),
      h('p', {}, 'Profiles are picked from real tuning trials on this PC. Run a Quick Tune or a Deep Tune first; your VRAM limit is asked before it starts.'),
      tuneButtons()));
    return;
  }
  const tp = info.tuner_pick || {};
  const goalName = (GOALS.find(g => g[0] === tp.mode) || [, tp.mode || 'Balanced'])[1];
  const tuner = { id: 'tuner', name: "Tuner's pick", measured: true, active: !info.active, desc: tp.desc, tg: tp.tg, pp: tp.pp, ctx: tp.ctx,
    trial: tp.trial, rule: `The tuner's winner for the ${goalName} goal, proven with a long-prompt run` + (tp.tg_deep ? ` (${tp.tg_deep} tok/s with the context half full).` : '.') };
  const tm = info.candidates.find(c => c.n === tp.trial);
  if (tm) { tuner.mem_mb = tm.mem_mb; tuner.pareto = tm.pareto; tuner.runs = tm.runs; }
  const grid = h('div', { class: 'prof-grid' }, profCard(tuner, info), ...info.profiles.map(p => profCard(p, info)));
  const hist = info.history || [];
  pane.append(h('div', { class: 'lab-sub' }, h('h3', {}, 'Profiles'),
    h('small', { class: 'muted' }, `Picked from ${info.candidates.length} passing trials, tuned ${info.tuned_at}.`),
    h('div', { class: 'grow' }),
    h('button', { class: 'btn ghost sm', disabled: !hist.length || Lab.busy, onclick: labRestore, title: hist.length ? `Back to ${hist[0].name || "the tuner's pick"}` : 'Nothing to restore yet' }, 'Restore previous')));
  if (info.llama_changed) pane.append(h('div', { class: 'lab-warn' }, 'These trials ran on an older llama.cpp build. They still load, but a new tune may measure differently.'));
  if (info.stale) pane.append(h('div', { class: 'lab-warn' }, 'Your GPU driver, hardware or llama.cpp changed since the active profile was applied. Re-tune to measure again.'));
  pane.append(grid);
  pane.append(h('div', { class: 'lab-sub' }, h('h3', {}, 'Every passing trial'),
    h('small', { class: 'muted' }, 'Generation speed against context. Ringed dots are on the Pareto front; the blue one is in use.')));
  pane.append(trialChart(info));
  pane.append(h('div', { class: 'lab-sub' }, h('h3', {}, 'Tune again'),
    h('small', { class: 'muted' }, 'New trials replace these and every profile is picked again.')), tuneButtons());
  if (hist.length) pane.append(h('div', { class: 'lab-sub' }, h('h3', {}, 'History')),
    h('div', { class: 'lab-hist' }, ...hist.slice(0, 6).map(x => h('div', {}, h('b', {}, x.name || "Tuner's pick"),
      h('span', { class: 'muted' }, x.applied_at ? ` applied ${x.applied_at}` : ' (default)'), x.desc ? h('small', { class: 'muted' }, ' · ' + x.desc) : null))));
}

function trialChart(info) {
  const C = info.candidates;
  const W = 900, H = 230, L = 52, R = 16, T = 14, B = 34;
  const svg = (tag, a = {}) => { const e = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const [k, v] of Object.entries(a)) e.setAttribute(k, v); return e; };
  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, class: 'lab-chart', role: 'img', 'aria-label': 'Trials: generation speed against context' });
  if (!C.length) return root;
  const ctxs = [...new Set(C.map(c => c.knobs.ctx))].sort((a, b) => a - b);
  const lx = v => Math.log2(v);
  const x0 = lx(ctxs[0]), x1 = lx(ctxs[ctxs.length - 1]);
  const X = v => L + (x1 > x0 ? (lx(v) - x0) / (x1 - x0) : 0.5) * (W - L - R);
  const ymax = Math.max(...C.map(c => c.tg)) * 1.12 || 1;
  const Y = v => T + (1 - v / ymax) * (H - T - B);
  const mems = C.map(c => c.mem_mb || 0).filter(Boolean);
  const mlo = Math.min(...mems), mhi = Math.max(...mems);
  for (let i = 0; i <= 4; i++) {
    const v = ymax * i / 4, y = Y(v);
    root.append(svg('line', { x1: L, x2: W - R, y1: y, y2: y, class: 'grid' }));
    const t = svg('text', { x: L - 8, y: y + 4, 'text-anchor': 'end' }); t.textContent = v.toFixed(v < 10 ? 1 : 0); root.append(t);
  }
  for (const c of ctxs) { const t = svg('text', { x: X(c), y: H - 12, 'text-anchor': 'middle' }); t.textContent = fmtCtx(c); root.append(t); }
  const yl = svg('text', { x: 12, y: T + (H - T - B) / 2, transform: `rotate(-90 12 ${T + (H - T - B) / 2})`, 'text-anchor': 'middle' }); yl.textContent = 'tok/s'; root.append(yl);
  const activeTrial = info.active?.trial ?? info.tuner_pick?.trial;
  for (const c of C) {
    const f = mems.length && c.mem_mb ? (mhi > mlo ? (c.mem_mb - mlo) / (mhi - mlo) : 0) : 0.5;
    const g = svg('g', { class: 'pt' + (c.pareto ? ' pareto' : '') + (c.n === activeTrial ? ' act' : '') });
    const dot = svg('circle', { cx: X(c.knobs.ctx), cy: Y(c.tg), r: c.n === activeTrial ? 7.5 : 5.5, style: `--m:${(f * 100).toFixed(0)}%` });
    const tip = svg('title'); tip.textContent = `#${c.n} · ${c.desc}\n${c.tg} tok/s generate · ${Math.round(c.pp)} tok/s prompt` + (c.mem_mb ? ` · ${fmtNum(c.mem_mb)} MB` : '');
    g.append(dot, tip);
    root.append(g);
  }
  return h('div', { class: 'lab-chart-wrap' }, root, h('div', { class: 'lab-legend' },
    h('span', { class: 'lg lo' }, 'less VRAM'), h('span', { class: 'lg hi' }, 'more VRAM'), h('span', { class: 'lg ring' }, 'Pareto'), h('span', { class: 'lg act' }, 'in use')));
}

// ---------------------------------------------------------------- benchmarks
const fmtS = (s, unit = '', d = 1) => !s ? 'not measured' : `${(+s.median).toFixed(d)}${unit}`;
const fmtSp = s => !s ? '' : `p95 ${(+s.p95).toFixed(1)} · cv ${(s.cv * 100).toFixed(1)}% · n=${s.n}`;
const pct = v => v == null ? 'not measured' : `${Math.round(v * 100)}%`;

function benchRows(r) {
  const res = r.results || {};
  const rows = [];
  const add = (w, m, s, unit, d) => rows.push([w, m, typeof s === 'string' ? s : fmtS(s, unit, d), typeof s === 'object' && s ? fmtSp(s) : '']);
  if (r.kind === 'router') {
    const x = res.router || {};
    rows.push(['Router', 'Picked the right tools', x.accuracy == null ? 'not measured' : `${pct(x.accuracy)} of ${x.cases}`, x.errors ? `${x.errors} requests failed` : '']);
    add('Router', 'Decision time', x.latency_ms, ' ms', 0);
    return rows;
  }
  if (r.kind === 'scenery') {
    for (const mode of ['full', 'off']) {
      const x = res[mode] || {};
      add(`Scenery ${mode}`, 'Decode', x.decode_tps, ' tok/s');
      const fr = x.frames;
      rows.push([`Scenery ${mode}`, 'Frame time', fr ? `${fr.median_ms} ms median` : 'not measured', fr ? `p95 ${fr.p95_ms} ms · ${fr.long_frames} long frames · ${fr.frames} frames` : '']);
    }
    rows.push(['Scenery', 'Full vs Off decode', res.delta_pct == null ? 'not measured' : `${res.delta_pct > 0 ? '+' : ''}${res.delta_pct}%`, '']);
    return rows;
  }
  const c = res.chat || {};
  add('Chat', 'Time to first token', c.ttft_ms, ' ms', 0);
  add('Chat', 'Decode', c.decode_tps, ' tok/s');
  for (const [n, p] of Object.entries(res.prefill || {})) {
    if (p.skipped) { rows.push([`Prefill ${fmtCtx(+n)}`, '', 'skipped', p.skipped]); continue; }
    add(`Prefill ${fmtCtx(+n)}`, 'Prompt speed', p.prefill_tps, ' tok/s', 0);
    add(`Prefill ${fmtCtx(+n)}`, 'Time to first token', p.ttft_ms, ' ms', 0);
  }
  const cd = res.code;
  if (cd && !cd.error) {
    add('Code edit', 'Decode', cd.decode_tps, ' tok/s');
    add('Code edit', 'Draft acceptance', cd.draft_acceptance, '', 2);
    rows.push(['Code edit', 'Edit correct', cd.edit_correct || 'not measured', '']);
  } else if (cd?.error) rows.push(['Code edit', '', 'not measured', cd.error]);
  const ca = res.cache;
  if (ca && !ca.error) {
    add('Prompt cache', `Cold TTFT (${fmtCtx(ca.prefix_tokens)})`, ca.cold_ttft_ms, ' ms', 0);
    add('Prompt cache', 'Warm TTFT', ca.warm_ttft_ms, ' ms', 0);
    add('Prompt cache', 'Tokens reused', ca.reused_share, '', 2);
  }
  const j = res.json || {}, t = res.tools || {}, rc = res.recall || {};
  if ('json' in res) rows.push(['JSON', 'Valid / exact fields', j.tasks ? `${pct(j.valid_json)} / ${pct(j.exact_fields)}` : 'not measured', j.tasks ? `${j.tasks} tasks` : (j.error || '')]);
  if ('tools' in res) rows.push(['Tool calls', 'Right tool and argument', t.tasks ? pct(t.accuracy) : 'not measured', t.tasks ? `${t.tasks} tasks` : (t.skipped || t.error || '')]);
  if ('recall' in res) rows.push(['Long context', 'Recall', rc.of ? `${rc.found}/${rc.of}` : 'not measured', rc.of ? `${fmtNum(rc.haystack_tokens)} tokens` : (rc.skipped || rc.error || '')]);
  const m = res.memory || {};
  rows.push(['Memory', 'VRAM idle / mean / peak', m.vram_peak_mb ? `${fmtNum(m.vram_idle_mb)} / ${fmtNum(m.vram_mean_mb)} / ${fmtNum(m.vram_peak_mb)} MB` : 'not measured', m.vram_samples ? `${m.vram_samples} samples` : 'no GPU with live VRAM readings (NVIDIA, or AMD on Linux)']);
  rows.push(['Memory', 'llama-server RAM peak', m.host_ram_peak_mb ? `${fmtNum(m.host_ram_peak_mb)} MB` : 'not measured', '']);
  return rows;
}

function benchTableEl(r) {
  return h('table', { class: 'rtable lab-table' },
    h('thead', {}, h('tr', {}, h('th', {}, 'Workload'), h('th', {}, 'Metric'), h('th', {}, 'Median'), h('th', {}, 'Spread'))),
    h('tbody', {}, ...benchRows(r).map(([w, m, v, s]) => h('tr', {}, h('td', {}, w), h('td', {}, m),
      h('td', { class: 'num' + (v === 'not measured' ? ' nm' : '') }, v), h('td', { class: 'muted' }, s)))));
}

function labProgress(title) {
  const steps = h('div', { class: 'lab-steps' });
  const bar = h('div', { class: 'bar blue' }, h('i'));
  const log = h('div', { class: 'logbox' });
  const el = h('div', { class: 'lab-progress' }, h('div', { class: 'lab-sub' }, h('h3', {}, title), h('div', { class: 'grow' }),
    h('button', { class: 'btn ghost sm', onclick: () => api('/api/cancel', { method: 'POST' }) }, 'Stop')), bar, steps, log);
  return { el, step(name, i, of) {
    $$('span', steps).forEach(s => s.classList.remove('now'));
    let s = $(`span[data-s="${name}"]`, steps); if (!s) { s = h('span', { 'data-s': name }, name); steps.append(s); }
    s.classList.add('now'); $('i', bar).style.width = Math.min(100, (i - 1) / of * 100) + '%';
  }, done(name) { $(`span[data-s="${name}"]`, steps)?.classList.replace('now', 'ok'); },
  log(t) { log.textContent += t + '\n'; log.scrollTop = 1e9; }, finish() { $('i', bar).style.width = '100%'; } };
}

async function benchStream(path, body, prog) {
  let result = null, err = null;
  try {
    await sseFetch(path, body, ev => {
      if (ev.type === 'bench_step') prog?.step(ev.name, ev.i, ev.of);
      else if (ev.type === 'bench_done_step') { prog?.done(ev.name); if (ev.result?.error) prog?.log(`${ev.name}: ${ev.result.error}`); }
      else if (ev.type === 'log') prog?.log(ev.text);
      else if (ev.type === 'result') result = ev.result;
      else if (ev.type === 'error') err = ev.error;
    });
  } catch (e) { err = e.message; }
  if (err) throw new Error(err);
  return result;
}

async function labRun(kind) {
  if (Lab.busy) return;
  if (S.streaming) return toast('Wait for the current reply to finish.', true);
  Lab.busy = true; S.labBusy = true; syncBusy();
  const title = { quick: 'Quick benchmark', deep: 'Deep benchmark', router: 'Router suite', scenery: 'Scenery cost' }[kind];
  Lab.progress = labProgress(title + '…');
  await renderLab();
  let err = null;
  try {
    if (kind === 'scenery') await labScenery(Lab.progress);
    else await benchStream(kind === 'router' ? '/api/bench/router' : '/api/bench/run',
      kind === 'router' ? {} : { suite: kind, scenery: sceneMode() + (S.settings?.scene_pause_busy !== false ? ' (paused while busy)' : '') }, Lab.progress);
  } catch (e) { err = e.message; }
  Lab.busy = false; S.labBusy = false; Lab.progress = null; syncBusy();
  if (err) toast(err === 'Cancelled' ? 'Benchmark stopped.' : err, err !== 'Cancelled', 6000);
  else toast(`${title} saved.`);
  Lab.tab = 'bench';
  if ($('#modalBody .lab')) renderLab();
}

/** Frame times from requestAnimationFrame while a benchmark half runs. */
function frameRecorder() {
  const d = []; let last = performance.now(), on = true;
  const tick = t => { d.push(t - last); last = t; if (on) requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
  return { stop() {
    on = false;
    const s = d.slice(2).sort((a, b) => a - b);
    if (!s.length) return null;
    const q = p => s[Math.min(s.length - 1, Math.max(0, Math.ceil(p * s.length) - 1))];
    return { frames: s.length, median_ms: +q(0.5).toFixed(2), p95_ms: +q(0.95).toFixed(2), long_frames: s.filter(x => x > 25).length,
      fps: +(1000 / (s.reduce((a, b) => a + b, 0) / s.length)).toFixed(1) };
  } };
}

/** Same decode workload with the scenery on Full (animating, not paused) and then Off. Keep the window visible. */
async function labScenery(prog) {
  const before = sceneMode();
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  S.sceneTest = true;
  const half = async mode => {
    Scene.setMode(mode); syncBusy(); await sleep(900);
    prog.log(`Scenery ${mode}: generating…`);
    const rec = frameRecorder();
    const r = await benchStream('/api/bench/run', { suite: 'decode', scenery: mode, save: false }, prog);
    const fr = rec.stop();
    return { decode_tps: r?.results?.chat?.decode_tps || null, ttft_ms: r?.results?.chat?.ttft_ms || null, frames: fr,
      animating: Scene.state().animating, hidden_during_run: document.hidden, reduced_motion: reduced, viewport: `${innerWidth}x${innerHeight}`,
      transparency: !document.body.classList.contains('no-transparency') };
  };
  try {
    const full = await half('full');
    const off = await half('off');
    await api('/api/bench/scenery', { method: 'POST', json: { full, off } });
  } finally {
    S.sceneTest = false; Scene.setMode(before); syncBusy();
  }
}

function labBench(pane, info) {
  const loaded = info.loaded;
  const file = (S.models.find(m => m.id === Lab.mid)?.path || '').split(/[\\/]/).pop();
  pane.append(h('div', { class: 'lab-sub' }, h('h3', {}, 'Run a benchmark'),
    h('small', { class: 'muted' }, loaded ? 'Runs against the model as it is loaded now, through the same server and chat template your chats use.' : 'Load this model to benchmark it.')));
  const b = (kind, label, sub, cls = 'btn ghost', need = true) => h('button', { class: cls, disabled: Lab.busy || (need && !loaded), onclick: () => labRun(kind) }, label, h('small', {}, sub));
  pane.append(h('div', { class: 'lab-actions' },
    b('quick', 'Quick benchmark', 'speed, cache, JSON, tools, recall', 'btn'),
    b('deep', 'Deep benchmark', 'more repeats, 16k prefill, 32k recall'),
    b('router', 'Router suite', '12 labeled requests', 'btn ghost', false),
    b('scenery', 'Scenery cost', 'Full vs Off, same workload')));
  pane.append(h('div', { class: 'lab-progress-slot' }));
  const mine = Lab.reports.filter(r => r.kind === 'router' || !r.model || r.model === file);   // router runs belong to every model
  if (!mine.length) { pane.append(h('div', { class: 'lab-empty sm' }, h('p', {}, 'No benchmark has been run for this model yet. Results appear here and are saved in data\\bench.'))); return; }
  const shown = [];
  for (const kind of ['model', 'scenery', 'router']) { const r = mine.find(x => x.kind === kind); if (r) shown.push(r); }
  for (const row of shown) {
    const box = h('div', {}, h('div', { class: 'lab-sub' }, h('h3', {}, 'Loading…')));
    pane.append(box);
    api(`/api/bench/${row.id}`).then(r => benchBox(box, r)).catch(e => { box.innerHTML = ''; box.append(h('div', { class: 'err-msg' }, e.message)); });
  }
  const rest = mine.filter(r => !shown.includes(r));
  if (rest.length) pane.append(h('div', { class: 'lab-sub' }, h('h3', {}, 'Earlier runs')),
    h('div', { class: 'lab-hist' }, ...rest.slice(0, 8).map(r => h('div', {}, h('b', {}, r.kind === 'model' ? `${r.depth} benchmark` : r.kind),
      h('span', { class: 'muted' }, ` ${r.at} · ${r.profile || "tuner's pick"}` + (r.decode_tps ? ` · ${r.decode_tps} tok/s` : '')), h('div', { class: 'grow' }),
      h('a', { href: `/api/bench/${r.id}?fmt=md&download=1`, download: '' }, 'md'), ' · ', h('a', { href: `/api/bench/${r.id}?download=1`, download: '' }, 'json')))));
}

function benchBox(box, r) {
  const title = { model: r.depth === 'deep' ? 'Deep benchmark' : 'Quick benchmark', router: 'Router suite', scenery: 'Scenery cost' }[r.kind] || 'Benchmark';
  box.innerHTML = '';
  box.append(h('div', { class: 'lab-sub' }, h('h3', {}, title),
    h('small', { class: 'muted' }, `${r.at} · ${r.profile || "tuner's pick"}` + (r.kind === 'router' ? '' : ` · ${r.desc || ''}`)), h('div', { class: 'grow' }),
    h('a', { class: 'btn ghost sm', href: `/api/bench/${r.id}?fmt=md&download=1`, download: '' }, 'Markdown'),
    h('a', { class: 'btn ghost sm', href: `/api/bench/${r.id}?download=1`, download: '' }, 'JSON')), benchTableEl(r));
  if (r.kind === 'scenery' && (r.results.full?.reduced_motion || r.results.full?.hidden_during_run || r.results.full?.animating === false))
    box.append(h('div', { class: 'lab-warn' }, 'The scenery could not animate during the Full half (reduced motion is on, the pond is hidden at this window size, or the window was hidden), so this run does not show its real cost.'));
  if (r.kind === 'router') {
    const rs = r.results.router || {};
    if (rs.errors) box.append(h('div', { class: 'lab-warn' }, `The router could not answer ${rs.errors} of the requests: ${(rs.first_error || '').slice(0, 200)}`));
    box.append(h('details', { class: 'lab-details' }, h('summary', {}, 'Every request'), h('table', { class: 'rtable lab-table' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Request'), h('th', {}, 'Result'), h('th', {}, 'Tools chosen'), h('th', {}, 'ms'))),
      h('tbody', {}, ...(rs.rows || []).map(x => h('tr', {}, h('td', {}, x.request),
        h('td', { class: x.skipped ? 'muted' : x.error ? 'bad' : x.ok ? 'ok' : 'bad' }, x.skipped ? 'skipped' : x.error ? 'error' : x.ok ? 'pass' : 'fail'),
        h('td', { class: 'muted' }, (x.chose || []).join(', ') || (x.skipped || (x.error || '').slice(0, 90) || '–')), h('td', { class: 'num' }, x.ms ?? '–')))))));
  }
}

// ---------------------------------------------------------------- this PC
function labPC(pane, info) {
  const fp = info.fingerprint;
  const kv = (rows) => h('div', { class: 'kv' }, ...rows.flatMap(([k, v]) => [h('span', {}, k), h('span', {}, v == null || v === '' ? 'not detected' : String(v))]));
  const hw = fp.hardware, en = fp.engine, md = fp.model || {};
  pane.append(h('div', { class: 'lab-sub' }, h('h3', {}, 'Hardware'), h('small', { class: 'muted' }, `Fingerprint ${fp.id}`)),
    kv([['GPU', hw.gpu], ['VRAM', hw.vram_mb ? `${fmtNum(hw.vram_mb)} MB` : null], ['Driver', hw.driver], ['CUDA (driver)', hw.cuda],
      ['CPU', hw.cpu], ['Cores / threads', `${hw.cores} / ${hw.threads}`], ['RAM', `${hw.ram_gb} GB`], ['OS', hw.os]]),
    h('div', { class: 'lab-sub' }, h('h3', {}, 'Inference engine')),
    kv([['Engine', 'llama.cpp llama-server (local process, 127.0.0.1 only)'], ['Build', en.version], ['Binary', en.binary],
      ['Speculative decoding', (en.spec_types || []).join(', ') || 'not offered by this build'], ['Hosted engines', 'none']]),
    h('div', { class: 'lab-sub' }, h('h3', {}, 'Model file')),
    kv([['File', md.file], ['Size', md.bytes ? fmtBytes(md.bytes) : null], ['Header hash', md.head_sha256], ['Architecture', md.arch],
      ['Layers', md.layers], ['Trained context', md.ctx_train ? fmtNum(md.ctx_train) : null]]),
    h('p', { class: 'muted', style: 'font-size:12.5px;margin-top:14px' }, 'Profiles remember this fingerprint. When the driver, GPU, CPU, RAM or llama.cpp build changes, the Lab marks them stale so you know to re-tune.'),
    h('button', { class: 'btn ghost sm', onclick: () => { closeModal(); openSettings('Privacy & offline'); } }, 'Privacy & offline settings'));
}
