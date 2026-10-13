// Aero 1.3.2: the window's own title bar, projects, and the Files and Changes views.
// - Title bar: Aero's own header instead of the Windows one (aero/shell.py hosts the page in WebView2 without a
//   caption; the header is the drag region, and its buttons minimize, maximize and close the window). In a browser
//   window (macOS, Linux, or no WebView2) the same header shows without the window buttons.
// - Projects: a named folder with its own instructions (aero/projects.py). Chats started in a project work in its
//   folder; the sidebar groups them under it.
// - Files: browse the project's folder (or the working directory) and read files. Changes: every file this chat's
//   tools wrote, with what each edit changed.
// Loaded before app.js; only defines functions until boot() calls initShell().
'use strict';

const NATIVE = !!(window.chrome && window.chrome.webview);
const P = { list: [], cur: null, view: 'chat', open: {}, tree: {}, file: null, sel: null, mdRaw: false };
const PROJ_KEY = 'aero.project';

function initShell() {
  Object.assign(I, {
    folder: '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
    chev: '<svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>',
    caret: '<svg viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></svg>',
    wmin: '<svg viewBox="0 0 12 12"><path d="M1.5 6.5h9"/></svg>',
    wmax: '<svg viewBox="0 0 12 12"><rect x="1.5" y="1.5" width="9" height="9" rx="1"/></svg>',
    wrestore: '<svg viewBox="0 0 12 12"><rect x="1.5" y="3.5" width="7" height="7" rx="1"/><path d="M3.5 3.5V2.5a1 1 0 0 1 1-1h5a1 1 0 0 1 1 1v5a1 1 0 0 1-1 1h-1"/></svg>',
    wclose: '<svg viewBox="0 0 12 12"><path d="m2 2 8 8M10 2l-8 8"/></svg>',
    reload: '<svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/></svg>',
    reveal: '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 11v5M9.5 13.5 12 11l2.5 2.5"/></svg>',
  });
  $('#tbSidebar').innerHTML = I.panel;
  $('#tbSidebar').onclick = () => setSidebar($('#sidebar').classList.contains('collapsed'));
  $('.ico-folder').innerHTML = I.folder; $('#projBtn .chev').innerHTML = I.chev;
  $('#projBtn').onclick = e => projMenu(e.currentTarget);
  $('#newProject').innerHTML = I.plus; $('#newProject').onclick = () => projectDialog();
  $$('#tbViews button').forEach(b => b.onclick = () => setView(b.dataset.view));
  $('#fvReload').innerHTML = I.reload; $('#fvReload').onclick = () => refreshView(true);
  try { P.cur = localStorage.getItem(PROJ_KEY) || null; } catch { }
  if (NATIVE) {
    document.body.classList.add('native-frame');
    const send = m => { try { window.chrome.webview.postMessage(m); } catch { } };
    $('#wMin').innerHTML = I.wmin; $('#wMax').innerHTML = I.wmax; $('#wClose').innerHTML = I.wclose;
    $('#wMin').onclick = () => send('min');
    $('#wMax').onclick = () => send('max');
    $('#wClose').onclick = () => send('close');
    window.chrome.webview.addEventListener('message', e => {
      const max = e.data && e.data.window === 'maximized';
      document.body.classList.toggle('win-max', max);
      $('#wMax').innerHTML = max ? I.wrestore : I.wmax;
      $('#wMax').title = max ? 'Restore' : 'Maximize';
    });
    send('state');
  }
  new MutationObserver(() => { clearTimeout(P.chgT); P.chgT = setTimeout(updateChangeCount, 400); })
    .observe($('#messages'), { childList: true, subtree: true });
  loadProjects();
}

// ---------------------------------------------------------------- projects

async function loadProjects() {
  try { P.list = await api('/api/projects'); } catch { P.list = []; }
  if (P.cur && !P.list.some(p => p.id === P.cur)) P.cur = null;
  renderProjButton(); renderChatList();
}

const curProject = () => P.list.find(p => p.id === P.cur) || null;
const projectOf = c => (c && c.project && P.list.find(p => p.id === c.project)) || null;

function renderProjButton() {
  const p = curProject();
  $('#projName').textContent = p ? p.name : 'No project';
  $('#projBtn').classList.toggle('none', !p);
  $('#projBtn').dataset.tip = p ? `Project ${p.name}: ${p.path}. New chats work in this folder with its instructions.`
    : 'No project: chats use the working directory from Settings → Tools. Pick or create a project to give chats their own folder and instructions.';
}

/** Switch the current project. A chat that already has messages stays in its own project: a new chat starts. */
function setProject(id, { keepChat = false } = {}) {
  P.cur = id || null;
  try { P.cur ? localStorage.setItem(PROJ_KEY, P.cur) : localStorage.removeItem(PROJ_KEY); } catch { }
  if (P.cur) P.open[P.cur] = true;
  renderProjButton();
  if (!keepChat && S.chat) {
    if (S.chat.messages.length && (S.chat.project || null) !== P.cur) newChat();
    else { S.chat.project = P.cur || undefined; renderChatList(); }
  } else renderChatList();
  P.tree = {}; P.file = null;
  if (P.view !== 'chat') refreshView();
}

function projMenu(anchor) {
  const cur = curProject();
  popMenu(anchor, [
    ...P.list.map(p => [(p.id === P.cur ? '✓ ' : '   ') + p.name, () => setProject(p.id)]),
    ['   No project', () => setProject(null)],
    ['New project…', () => projectDialog()],
    ...(cur ? [[`Edit ${cur.name}…`, () => projectDialog(cur)]] : []),
  ]);
}

function projectDialog(p = null) {
  const name = h('input', { type: 'text', value: p?.name || '', placeholder: 'e.g. Mediacraft mod' });
  const path = h('input', { type: 'text', value: p?.path || '', placeholder: 'C:\\Users\\you\\code\\my-project' });
  const ins = h('textarea', { rows: 7, placeholder: 'Optional. How to work in this project: build and test commands, style, what not to touch…' }, p?.instructions || '');
  const err = h('p', { class: 'muted', style: 'color:var(--bad);min-height:1em;margin:4px 0 0' });
  const browse = h('button', { class: 'btn ghost sm', onclick: async () => {
    try { const r = await api('/api/projects/browse', { method: 'POST' }); if (r.path) { path.value = r.path; if (!name.value.trim()) name.value = r.path.split(/[\\/]/).filter(Boolean).pop() || ''; } }
    catch (e) { toast(e.message, true); }
  } }, 'Browse…');
  const save = h('button', { class: 'btn', onclick: async () => {
    err.textContent = '';
    const body = { name: name.value.trim(), path: path.value.trim(), instructions: ins.value };
    try {
      const r = p ? await api('/api/projects/' + p.id, { method: 'PUT', json: body }) : await api('/api/projects', { method: 'POST', json: body });
      closeModal(true); await loadProjects();
      if (!p) setProject(r.id); else { renderProjButton(); if (P.view !== 'chat') refreshView(true); }
      toast(p ? 'Project saved' : `Project ${r.name} created`);
    } catch (e) { err.textContent = e.message; }
  } }, p ? 'Save' : 'Create project');
  const del = p ? h('button', { class: 'btn danger', onclick: async () => {
    if (!confirm(`Remove the project "${p.name}"? Its folder and files stay; its chats move to Recent.`)) return;
    await api('/api/projects/' + p.id, { method: 'DELETE' });
    closeModal(true); if (P.cur === p.id) setProject(null, { keepChat: true }); await loadProjects(); toast('Project removed');
  } }, 'Remove project') : null;
  modal(p ? `Project: ${p.name}` : 'New project', h('div', {},
    field('Name', name),
    field('Folder', h('div', { style: 'display:flex;gap:8px' }, path, browse), 'Chats in this project work here: file and shell tools start in this folder, and the Files view shows it.'),
    field('Instructions', ins, 'Read by the model with its system prompt in every chat of this project.'),
    err,
    h('div', { style: 'display:flex;gap:8px;justify-content:flex-end;margin-top:8px' }, del, h('span', { class: 'grow' }), h('button', { class: 'btn ghost', onclick: () => closeModal(true) }, 'Cancel'), save)));
  setTimeout(() => (p ? ins : name).focus(), 30);
}

/** Sidebar: each project with its chats under it (Recent lists the chats without one). */
function renderProjects(item) {
  const box = $('#projList'); if (!box) return;
  box.innerHTML = '';
  $('#projEmpty').classList.toggle('hidden', P.list.length > 0);
  for (const p of P.list) {
    const chats = S.chats.filter(c => c.project === p.id);
    const open = P.open[p.id] ?? (p.id === P.cur);
    const more = h('button', { class: 'del', html: I.dots, title: 'Project options', onclick: e => {
      e.stopPropagation();
      popMenu(e.currentTarget, [['New chat here', () => { setProject(p.id, { keepChat: true }); newChat(); }], ['Edit…', () => projectDialog(p)],
        ['Show folder', () => api('/api/files/reveal', { method: 'POST', json: { project: p.id, path: '' } }).catch(err => toast(err.message, true))]]);
    } });
    const row = h('div', { class: 'proj-row' + (p.id === P.cur ? ' active' : '') + (p.exists === false ? ' missing' : ''), title: p.path,
      onclick: () => { P.open[p.id] = !(open && p.id === P.cur); if (p.id !== P.cur) setProject(p.id); else renderChatList(); } },
      h('span', { class: 'caret' + (open ? ' open' : ''), html: I.caret }), h('span', { class: 'pico', html: I.folder }),
      h('span', { class: 'pname' }, p.name), h('small', {}, chats.length || ''), more);
    const list = h('div', { class: 'proj-chats' + (open ? '' : ' hidden') }, ...chats.map(item));
    if (open && !chats.length) list.append(h('div', { class: 'proj-none' }, 'No chats yet'));
    box.append(h('div', { class: 'proj' }, row, list));
  }
}

// ---------------------------------------------------------------- views: Chat · Files · Changes

function setView(v) {
  P.view = v;
  $$('#tbViews button').forEach(b => b.classList.toggle('on', b.dataset.view === v));
  $('#chatPane').classList.toggle('hidden', v !== 'chat');
  $('#filesView').classList.toggle('hidden', v === 'chat');
  if (v === 'chat') { setTimeout(() => $('#input').focus(), 0); return; }
  refreshView();
}

function refreshView(force = false) {
  if (P.view === 'files') openFiles(force);
  else if (P.view === 'changes') openChanges();
}

const projQ = () => `project=${encodeURIComponent((S.chat && S.chat.project) || P.cur || '')}`;

async function openFiles(force = false) {
  const p = projectOf(S.chat) || curProject();
  $('#fvRoot').textContent = p ? p.name : 'Working directory';
  $('#fvRoot').title = p ? p.path : 'No project: the working directory from Settings → Tools';
  const tree = $('#fvList'); tree.innerHTML = '';
  if (force) P.tree = {};
  tree.append(await treeLevel('', 0));
  if (!P.file) fvBody(h('div', { class: 'fv-empty' }, p ? `Files in ${p.path}` : 'No project. Showing the working directory. Create a project to give chats their own folder.'));
  else if (force) openFile(P.file);
}

async function listDir(path) {
  if (!P.tree[path]) P.tree[path] = await api(`/api/files/list?${projQ()}&path=${encodeURIComponent(path)}`);
  return P.tree[path];
}

async function treeLevel(path, depth) {
  const box = h('div', { class: 'fv-level' });
  let d;
  try { d = await listDir(path); } catch (e) { box.append(h('div', { class: 'fv-err' }, e.message)); return box; }
  if (!path) $('#fvRoot').title = d.root;
  for (const e of d.entries) {
    const rel = (path ? path + '/' : '') + e.name;
    const pad = { style: `padding-left:${8 + depth * 14}px` };
    if (e.dir) {
      const kids = h('div', { class: 'hidden' });
      const row = h('div', { class: 'fv-row dir' + (e.skip ? ' skip' : ''), ...pad, title: rel },
        h('span', { class: 'caret', html: I.caret }), h('span', { class: 'pico', html: I.folder }), h('span', { class: 'fname' }, e.name));
      row.onclick = async () => {
        const open = kids.classList.toggle('hidden') === false;
        row.querySelector('.caret').classList.toggle('open', open);
        if (open && !kids.childElementCount) kids.append(await treeLevel(rel, depth + 1));
      };
      box.append(row, kids);
    } else {
      const row = h('div', { class: 'fv-row file' + (P.file === rel ? ' active' : ''), ...pad, title: rel, 'data-rel': rel },
        h('span', { class: 'caret blank' }), h('span', { class: 'pico', html: I.file }), h('span', { class: 'fname' }, e.name),
        h('small', {}, fmtBytes(e.size)));
      row.onclick = () => openFile(rel);
      box.append(row);
    }
  }
  if (d.truncated) box.append(h('div', { class: 'fv-err' }, 'Only the first 2,000 entries are shown.'));
  if (!d.entries.length && !path) box.append(h('div', { class: 'fv-err' }, 'This folder is empty.'));
  return box;
}

function fvBody(...kids) { const b = $('#fvBody'); b.innerHTML = ''; b.append(...kids); b.scrollTop = 0; }

const HL = { py: 'python', js: 'javascript', mjs: 'javascript', cjs: 'javascript', ts: 'typescript', tsx: 'typescript', jsx: 'javascript',
  md: 'markdown', json: 'json', css: 'css', html: 'xml', htm: 'xml', xml: 'xml', svg: 'xml', vue: 'xml', yml: 'yaml', yaml: 'yaml',
  toml: 'ini', ini: 'ini', cfg: 'ini', properties: 'ini', bat: 'dos', cmd: 'dos', ps1: 'powershell', sh: 'bash', c: 'c', h: 'c',
  cpp: 'cpp', hpp: 'cpp', cs: 'csharp', java: 'java', kt: 'kotlin', go: 'go', rs: 'rust', rb: 'ruby', php: 'php', lua: 'lua',
  sql: 'sql', gradle: 'groovy', swift: 'swift', dart: 'dart', mcmeta: 'json', lang: 'ini' };

function codeView(text, ext) {
  text = text.replace(/\r\n?/g, '\n');
  const lines = text.split('\n'), n = lines.length;
  const code = h('code', { class: 'hljs' });
  const lang = HL[ext];
  if (window.hljs && text.length < 400000 && lang && hljs.getLanguage(lang)) {
    try { code.innerHTML = hljs.highlight(text, { language: lang, ignoreIllegals: true }).value; } catch { code.textContent = text; }
  } else code.textContent = text;
  return h('div', { class: 'fv-code' }, h('pre', { class: 'fv-gutter' }, Array.from({ length: n }, (_, i) => i + 1).join('\n')), h('pre', { class: 'fv-src' }, code));
}

async function openFile(rel, absolute = false) {
  P.file = absolute ? null : rel;
  $$('#fvList .fv-row.file').forEach(r => r.classList.toggle('active', r.dataset.rel === rel));
  fvBody(h('div', { class: 'fv-empty' }, 'Opening…'));
  let f;
  try { f = await api(`/api/files/read?${projQ()}&path=${encodeURIComponent(rel)}`); }
  catch (e) { return fvBody(h('div', { class: 'fv-err' }, e.message)); }
  const raw = `/api/files/raw?${projQ()}&path=${encodeURIComponent(rel)}`;
  const reveal = h('button', { class: 'icon-btn', html: I.reveal, title: 'Show in folder', onclick: () =>
    api('/api/files/reveal', { method: 'POST', json: { project: (S.chat && S.chat.project) || P.cur || '', path: rel } }).catch(e => toast(e.message, true)) });
  const isMd = f.kind === 'text' && f.ext === 'md';
  const mdBtn = isMd ? h('button', { class: 'btn ghost sm', onclick: () => { P.mdRaw = !P.mdRaw; openFile(rel, absolute); } }, P.mdRaw ? 'Preview' : 'Source') : null;
  const head = h('div', { class: 'fv-head' }, h('span', { class: 'fv-path', title: f.full }, f.path),
    h('small', {}, `${fmtBytes(f.size)} · ${new Date(f.mtime * 1000).toLocaleString()}`), h('span', { class: 'grow' }), mdBtn, reveal);
  let view;
  if (f.kind === 'text') {
    if (isMd && !P.mdRaw) { view = h('div', { class: 'md fv-md' }); renderMd(view, f.text); }
    else view = codeView(f.text, f.ext);
  } else if (f.kind === 'media') {
    view = f.mime.startsWith('image/') ? h('div', { class: 'fv-media' }, h('img', { src: raw, alt: f.path }))
      : f.mime.startsWith('video/') ? h('div', { class: 'fv-media' }, h('video', { src: raw, controls: true }))
      : f.mime.startsWith('audio/') ? h('div', { class: 'fv-media' }, h('audio', { src: raw, controls: true }))
      : h('iframe', { class: 'fv-pdf', src: raw, title: f.path });
  } else view = h('div', { class: 'fv-empty' }, f.kind === 'large' ? `${fmtBytes(f.size)}: too large to show here.` : 'A binary file: nothing to show as text.');
  fvBody(head, view);
}

// ---------------------------------------------------------------- Changes: what this chat's tools wrote

const WRITERS = { write_file: 'Wrote', edit_file: 'Edited', append_file: 'Appended to', move_path: 'Moved', copy_path: 'Copied', delete_path: 'Deleted',
  write_docx: 'Wrote', diagram_save: 'Saved', make_dir: 'Created' };

function changesOf(chat) {
  const out = new Map();
  const results = {};
  for (const m of chat?.messages || []) if (m.role === 'tool' && m.tool_call_id) results[m.tool_call_id] = m;
  for (const m of chat?.messages || []) for (const c of m.tool_calls || []) {
    const name = c.function?.name || '';
    const cat = S.toolDesc?.[name]?.cat;
    if (!WRITERS[name] && cat !== 'files_write') continue;
    let a = {}; try { a = JSON.parse(c.function.arguments || '{}'); } catch { }
    const path = a.destination || a.path || a.file || a.dest;
    if (!path) continue;
    const r = results[c.id];
    const failed = !!(r && (r.error || /^(error|not run|denied)/i.test(String(r.content || '').trim())));
    if (!out.has(path)) out.set(path, []);
    out.get(path).push({ name, a, failed, ts: m.ts });
  }
  return out;
}

function updateChangeCount() {
  const n = changesOf(S.chat).size;
  $('#chgCount').textContent = n ? n : '';
  if (P.view === 'changes') openChanges(true);
}

function lineDiff(a, b) {
  const x = String(a || '').split('\n'), y = String(b || '').split('\n');
  if (x.length * y.length > 250000) return [...x.map(t => ['-', t]), ...y.map(t => ['+', t])];
  const n = x.length, m = y.length, L = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = []; let i = 0, j = 0;
  while (i < n && j < m) { if (x[i] === y[j]) { out.push([' ', x[i]]); i++; j++; } else if (L[i + 1][j] >= L[i][j + 1]) out.push(['-', x[i++]]); else out.push(['+', y[j++]]); }
  while (i < n) out.push(['-', x[i++]]); while (j < m) out.push(['+', y[j++]]);
  return out;
}

function diffView(rows) {
  return h('pre', { class: 'fv-diff' }, rows.map(([k, t]) => h('div', { class: k === '+' ? 'add' : k === '-' ? 'del' : 'ctx' }, `${k} ${t}`)));
}

function openChanges(keep = false) {
  const ch = changesOf(S.chat);
  $('#fvRoot').textContent = `Changed in this chat (${ch.size})`;
  const list = $('#fvList'); list.innerHTML = '';
  if (!ch.size) {
    list.append(h('div', { class: 'fv-err' }, 'Nothing written yet.'));
    return fvBody(h('div', { class: 'fv-empty' }, 'Files this chat creates, edits, moves or deletes show here, with what each edit changed.'));
  }
  const paths = [...ch.keys()];
  if (!keep || !ch.has(P.sel)) P.sel = paths[paths.length - 1];
  for (const path of paths) {
    const ev = ch.get(path), last = ev[ev.length - 1];
    const name = path.split(/[\\/]/).pop();
    list.append(h('div', { class: 'fv-row file' + (path === P.sel ? ' active' : ''), title: path, onclick: () => { P.sel = path; openChanges(true); } },
      h('span', { class: 'pico', html: I.file }), h('span', { class: 'fname' }, name),
      h('small', { class: last.name === 'delete_path' ? 'bad' : '' }, `${ev.length}×`)));
  }
  const ev = ch.get(P.sel);
  const blocks = ev.map((e, i) => {
    const what = `${WRITERS[e.name] || e.name}${e.failed ? ' (failed)' : ''}`;
    let body = null;
    if (e.name === 'edit_file' && (e.a.old_text != null || e.a.new_text != null)) body = diffView(lineDiff(e.a.old_text, e.a.new_text));
    else if ((e.name === 'write_file' || e.name === 'append_file') && e.a.content != null) {
      const lines = String(e.a.content).split('\n');
      body = diffView(lines.slice(0, 400).map(t => ['+', t]).concat(lines.length > 400 ? [[' ', `… ${lines.length - 400} more lines`]] : []));
    } else if (e.name === 'move_path' || e.name === 'copy_path') body = h('p', { class: 'muted' }, `${e.a.source || ''} → ${e.a.destination || ''}`);
    return h('div', { class: 'chg' + (e.failed ? ' failed' : '') }, h('div', { class: 'chg-h' }, h('b', {}, `${i + 1}. ${what}`), e.name), body);
  });
  const open = h('button', { class: 'btn ghost sm', onclick: () => { setView('files'); openFile(P.sel); } }, 'Open file');
  fvBody(h('div', { class: 'fv-head' }, h('span', { class: 'fv-path', title: P.sel }, P.sel), h('span', { class: 'grow' }), open), ...blocks);
}
