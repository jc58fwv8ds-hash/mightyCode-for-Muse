let sessionId = null;
let es = null;
let bootId = null;          // backend process id we are talking to
let recovering = false;
let connDown = false;
let ccSessionId = null;     // Claude Code conversation id (for --resume after a restart)
const log = document.getElementById('log');
const input = document.getElementById('input');
const sendBtn = document.getElementById('sendBtn');
const stopBtn = document.getElementById('stopBtn');
const banner = document.getElementById('banner');
const turnStatus = document.getElementById('turnStatus');

// ---- small storage helpers (Safari private mode can throw) ------------
const store = {
  get(k, s = localStorage) { try { return s.getItem(k); } catch (e) { return null; } },
  set(k, v, s = localStorage) { try { v == null ? s.removeItem(k) : s.setItem(k, v); } catch (e) {} },
};

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function scrollDown() { log.scrollTop = log.scrollHeight; }
function note(text, cls = 'msg note') { log.appendChild(el('div', cls, text)); scrollDown(); }

function showBanner(text, kind = 'warn', actions = []) {
  banner.innerHTML = '';
  banner.className = 'banner ' + kind;
  banner.appendChild(el('span', null, text));
  actions.forEach(([label, fn]) => { const b = el('button', null, label); b.onclick = fn; banner.appendChild(b); });
  banner.hidden = false;
}
function hideBanner() { banner.hidden = true; }

function setInputEnabled(on) {
  input.disabled = !on; sendBtn.disabled = !on; stopBtn.disabled = on || !sessionId;
  input.placeholder = sessionId ? (on ? 'やりたいこと' : '実行中です') : 'セッションを開始すると入力できます';
}

async function api(path, body, timeoutMs = 8000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const opt = body === undefined ? { cache: 'no-store', signal: ctl.signal }
      : { method: 'POST', cache: 'no-store', signal: ctl.signal,
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    const r = await fetch(path, opt);
    let j = {};
    try { j = await r.json(); } catch (e) {}
    return { ok: r.ok, status: r.status, data: j };
  } finally { clearTimeout(t); }
}

function diffHtml(diff) {
  const pre = el('pre');
  diff.split('\n').forEach(line => {
    const span = el('span', null, line + '\n');
    if (line.startsWith('+') && !line.startsWith('+++')) span.className = 'diff-add';
    else if (line.startsWith('-') && !line.startsWith('---')) span.className = 'diff-del';
    else if (line.startsWith('@@') || line.startsWith('+++') || line.startsWith('---')) span.className = 'diff-hdr';
    pre.appendChild(span);
  });
  return pre;
}

function argsSummary(tool, args) {
  if (!args) return '';
  if (tool === 'Bash') return args.command || '';
  if (args.file_path) return args.file_path;
  if (args.path) return args.path;
  if (args.pattern) return args.pattern;
  return JSON.stringify(args);
}

// ---- session health panel ---------------------------------------------
const HEALTH_LABEL = { cli: 'Claude Code CLI 起動', model: 'モデル生成テスト', cli_model: 'CLI → モデル到達' };
const HEALTH_STATE = { pending: '未確認', checking: '確認中', waiting: '待機中', ok: 'OK', fail: '失敗' };
let healthBox = null;
function renderHealth(ev) {
  if (!healthBox) {
    healthBox = el('div', 'card health');
    healthBox.appendChild(el('div', 'head', 'セッションの接続確認'));
    healthBox.rows = {};
    for (const k of ['cli', 'model', 'cli_model']) {
      const row = el('div', 'hrow');
      const lamp = el('span', 'hicon pending');
      lamp.setAttribute('role', 'img'); lamp.setAttribute('aria-label', HEALTH_STATE.pending);
      row.appendChild(lamp);
      row.appendChild(el('span', 'hlabel', HEALTH_LABEL[k]));
      row.appendChild(el('span', 'hdetail', ''));
      healthBox.rows[k] = row;
      healthBox.appendChild(row);
    }
    const retry = el('button', 'retry', '再チェック');
    retry.onclick = () => api('/api/check', { session: sessionId });
    retry.hidden = true;
    healthBox.retry = retry;
    healthBox.appendChild(retry);
    log.appendChild(healthBox);
  }
  const row = healthBox.rows[ev.stage];
  if (!row) return;
  row.children[0].className = 'hicon ' + ev.state;
  row.children[0].setAttribute('aria-label', HEALTH_STATE[ev.state] || ev.state);
  row.children[0].title = HEALTH_STATE[ev.state] || ev.state;
  row.children[2].textContent = ev.detail || '';
  row.className = 'hrow ' + ev.state;
  const h = ev.health || {};
  const failed = Object.entries(h).filter(([, v]) => v === 'fail').map(([k]) => HEALTH_LABEL[k]);
  healthBox.className = 'card health' + (failed.length ? ' bad' : '');
  healthBox.retry.hidden = !failed.length;
  if (failed.length) {
    showBanner(`接続確認に失敗: ${failed.join('、')}。このままではタスクがモデルに届きません。`, 'error',
      [['再チェック', () => api('/api/check', { session: sessionId })]]);
  } else if (!connDown) hideBanner();
}

const approvalCards = {};  // call_id -> card element

function handleEvent(ev) {
  switch (ev.type) {
    case 'user_message': {
      const fromMuse = ev.by === 'muse';
      const m = el('div', 'msg user' + (fromMuse ? ' muse' : ''));
      m.appendChild(el('span', 'who' + (fromMuse ? ' muse' : ''), fromMuse ? 'Muse' : 'あなた'));
      m.appendChild(document.createTextNode(ev.content));
      log.appendChild(m);
      // A turn is now running, whoever started it (this screen or Muse).
      setInputEnabled(false); stopBtn.disabled = false;
      break; }
    case 'assistant_text':
      log.appendChild(el('div', 'msg assistant', ev.content)); break;
    case 'error':
      log.appendChild(el('div', 'msg error', 'エラー: ' + ev.message)); break;
    case 'notice':
      log.appendChild(el('div', 'msg note warn', ev.message)); break;
    case 'health':
      renderHealth(ev); break;
    case 'turn_status':
      turnStatus.textContent = ev.message || '';
      turnStatus.className = 'turnstatus ' + (ev.stage || '');
      break;
    case 'cc_session':
      ccSessionId = ev.cc_session_id;
      store.set('lc_cc_session', ccSessionId);
      break;
    case 'compacted':
      log.appendChild(el('div', 'msg note', `— コンテキストを圧縮しました（約${ev.tokens}トークン） —`)); break;
    case 'usage':
      document.getElementById('tokenLabel').textContent =
        `コンテキスト 約${ev.est_tokens.toLocaleString()}トークン`; break;
    case 'tool_call': {
      const card = el('div', 'card');
      const head = el('div', 'head');
      head.appendChild(el('span', 'tool', ev.tool));
      head.appendChild(document.createTextNode(' ' + argsSummary(ev.tool, ev.args)));
      card.appendChild(head);
      if (ev.diff) card.appendChild(diffHtml(ev.diff));
      if (ev.tool === 'Write' && ev.args && ev.args.content && !ev.diff) {
        card.appendChild(el('pre', null, ev.args.content.slice(0, 4000)));
      }
      log.appendChild(card);
      break; }
    case 'approval_required': {
      const card = el('div', 'card approval');
      const head = el('div', 'head', `承認が必要です: ${ev.tool} ${argsSummary(ev.tool, ev.args)}`);
      card.appendChild(head);
      if (ev.diff) card.appendChild(diffHtml(ev.diff));
      if (ev.tool === 'Bash') card.appendChild(el('pre', null, ev.args.command || ''));
      if (ev.tool === 'Write' && ev.args && ev.args.content && !ev.diff)
        card.appendChild(el('pre', null, ev.args.content.slice(0, 4000)));
      const btns = el('div', 'buttons');
      const ok = el('button', 'primary', '承認');
      const ng = el('button', 'deny', '却下');
      ok.onclick = () => decide(ev.call_id, true, card);
      ng.onclick = () => decide(ev.call_id, false, card);
      btns.appendChild(ok); btns.appendChild(ng);
      card.appendChild(btns);
      if (ev.bridge_file) card.appendChild(el('div', 'bridge', 'Muse からも承認できます（承認ブリッジに書き出し済み）'));
      card.appendChild(el('div', 'outcome', ''));
      approvalCards[ev.call_id] = card;
      log.appendChild(card);
      break; }
    case 'approval_resolved': {
      const card = approvalCards[ev.call_id];
      if (card) {
        card.querySelectorAll('button').forEach(b => b.disabled = true);
        card.style.opacity = '0.75';
        const isMuse = String(ev.by).toLowerCase() === 'muse';
        const who = isMuse ? 'Muse' : ({ gui: 'あなた', stop: '停止操作', restart: '再起動' }[ev.by] || ev.by);
        const out = card.querySelector('.outcome');
        out.textContent = '';
        out.className = 'outcome ' + (ev.decision ? 'ok' : 'ng');
        out.appendChild(el('span', null, ev.decision ? '承認' : '却下'));
        out.appendChild(el('span', 'who' + (isMuse ? ' muse' : ''), who));
        if (ev.note) out.appendChild(el('span', 'note', ev.note));
      }
      break; }
    case 'guard_blocked': {
      const card = el('div', 'card guard');
      card.appendChild(el('div', 'head', `Guardがブロックしました: ${ev.tool}`));
      card.appendChild(el('div', null, argsSummary(ev.tool, ev.args)));
      card.appendChild(el('div', null, '理由: ' + ev.reason));
      log.appendChild(card);
      break; }
    case 'cc_denied': {
      const card = el('div', 'card guard');
      card.appendChild(el('div', 'head', `Claude Codeの安全チェックでブロック: ${ev.tool}`));
      card.appendChild(el('div', null, '理由: ' + ev.reason));
      log.appendChild(card);
      break; }
    case 'tool_result': {
      const d = el('details', 'result');
      const s = el('summary', null, `${ev.tool} の結果`);
      const pre = el('pre', null, ev.result);
      d.appendChild(s); d.appendChild(pre);
      log.appendChild(d);
      break; }
    case 'done':
      setInputEnabled(true); turnStatus.textContent = '';
      input.focus(); break;
  }
  scrollDown();
}

async function decide(callId, decision, card) {
  card.querySelectorAll('button').forEach(b => b.disabled = true);
  card.style.opacity = '0.65';
  try {
    const r = await api('/api/approve', { session: sessionId, call_id: callId, decision, by: 'gui' });
    if (r.status === 410) return recover('session_lost');
    if (r.ok && r.data.ok === false) card.querySelector('.outcome').textContent = '（既に別経路で解決済み）';
  } catch (e) {
    card.querySelectorAll('button').forEach(b => b.disabled = false);
    card.style.opacity = '1';
    showBanner('承認を送信できませんでした（バックエンドに接続できません）', 'error');
  }
}

// ---- connection monitor -----------------------------------------------
function applyStatus(r) {
  const dot = document.getElementById('modelDot');
  dot.className = 'dot ' + (r.model_reachable ? 'dot-on'
    : (r.model_state === 'restarting' || r.model_state === 'unknown' ? 'dot-wait' : 'dot-off'));
  const ver = String(r.claude_version || '').replace(/\s*\(Claude Code\)\s*/, '');
  const cc = r.claude_found ? ('Claude Code' + (ver ? ' ' + ver : '')) : 'Claude Code 未検出';
  const st = { ok: '', unknown: '（生成確認中）', restarting: '（モデル再起動中）', fail: '（モデル生成不可）' }[r.model_state] || '';
  const label = document.getElementById('modelLabel');
  label.textContent = `${cc} · ${String(r.model).split('/').pop()}${st}`;
  label.title = r.model_detail || '';
  document.getElementById('approvalLabel').textContent =
    r.approvals_pending ? `承認待ち ${r.approvals_pending}件` : '';
}

async function checkConnection() {
  if (recovering) return;
  let r;
  try {
    r = await api('/api/status' + (sessionId ? '?session=' + sessionId : ''), undefined, 6000);
  } catch (e) {
    if (!connDown) {
      connDown = true;
      showBanner('バックエンドに接続できません。再接続を試みています…', 'error');
      document.getElementById('modelDot').className = 'dot dot-off';
    }
    return;
  }
  if (!r.ok) return;
  const s = r.data;
  const wasDown = connDown;
  connDown = false;
  if (bootId && s.boot_id !== bootId) return recover('restarted');
  bootId = s.boot_id;
  applyStatus(s);
  if (sessionId && s.session_alive === false) return recover('session_lost');
  refreshSessions();
  if (wasDown) {
    hideBanner(); note('バックエンドに再接続しました');
    if (sessionId && (!es || es.readyState === EventSource.CLOSED)) openEvents();
  }
}

// Backend restarted or our session vanished: reload the page and resume
// automatically (keeps the folder, the unsent draft and the conversation id).
function recover(reason) {
  if (recovering) return;
  recovering = true;
  const cwd = document.getElementById('cwd').value.trim();
  store.set('lc_resume', JSON.stringify({ cwd, cc: ccSessionId, reason, at: Date.now(),
                                          had: !!sessionId }), sessionStorage);
  store.set('lc_draft', input.value, sessionStorage);
  showBanner(reason === 'restarted'
    ? 'バックエンドの再起動を検知しました。画面を再読み込みしてセッションを張り直します…'
    : 'セッションが失われていることを検知しました。画面を再読み込みしてセッションを張り直します…', 'warn');
  if (es) { es.close(); es = null; }
  setTimeout(() => location.reload(), 800);
}

function openEvents() {
  if (es) es.close();
  es = new EventSource('/api/events?session=' + sessionId);
  es.addEventListener('hello', m => {
    const h = JSON.parse(m.data);
    if (bootId && h.boot_id !== bootId) return recover('restarted');
    bootId = h.boot_id;
    setInputEnabled(!h.running);
  });
  es.onmessage = m => handleEvent(JSON.parse(m.data));
  es.onerror = () => {
    // EventSource retries by itself while the server is down, but gives up
    // for good on a non-200 (e.g. 410 session_lost) — check right away.
    setTimeout(checkConnection, es && es.readyState === EventSource.CLOSED ? 0 : 1500);
  };
}

async function startSession(cwd, resume) {
  let r;
  try {
    r = await api('/api/session', { cwd, resume: resume || null, by: 'gui' }, 15000);
  } catch (e) {
    showBanner('バックエンドに接続できないためセッションを開始できません', 'error');
    return false;
  }
  if (r.data.error) { showBanner('セッション開始に失敗: ' + r.data.error, 'error'); return false; }
  sessionId = r.data.session_id;
  bootId = r.data.boot_id;
  healthBox = null;
  store.set('lc_cwd', r.data.cwd);
  document.getElementById('cwd').value = r.data.cwd;
  openEvents();
  setInputEnabled(true);
  note(`セッション開始: ${r.data.cwd}` + (resume ? '（前回の会話を再開）' : ''));
  input.focus();
  refreshSessions();
  // Re-apply the auto-approve checkbox to the new session.
  if (document.getElementById('autoApprove').checked)
    api('/api/auto', { session: sessionId, auto_approve: true });
  return true;
}

// ---- sessions shared between this screen and Muse -----------------------
// Muse drives the same backend over HTTP. Every live session is listed so the
// person can join one Muse started (and see what Muse sent), and vice versa.
function folderName(p) { return String(p || '').replace(/\/+$/, '').split('/').pop() || '/'; }

async function refreshSessions() {
  let r;
  try { r = await api('/api/sessions', undefined, 5000); } catch (e) { return; }
  if (!r.ok) return;
  const items = r.data.sessions || [];
  const bar = document.getElementById('sessionsBar');
  const list = document.getElementById('sessionList');
  // Nothing to choose from when the only session is the one on screen.
  bar.hidden = !items.some(s => s.id !== sessionId);
  // Rebuild only when something changed, so chips stay put under the pointer.
  const sig = JSON.stringify([sessionId, items.map(s => [s.id, s.running, s.pending_approvals])]);
  if (sig === list.dataset.sig) return;
  list.dataset.sig = sig;
  list.innerHTML = '';
  items.forEach(s => {
    const b = el('button', 'schip' + (s.id === sessionId ? ' current' : ''));
    b.appendChild(el('span', 'who' + (s.started_by === 'muse' ? ' muse' : ''), s.started_by === 'muse' ? 'Muse' : 'あなた'));
    b.appendChild(el('span', 'folder', folderName(s.cwd)));
    const state = s.pending_approvals ? '承認待ち' : (s.running ? '実行中' : '待機中');
    b.appendChild(el('span', 'state' + (s.pending_approvals ? ' wait' : ''), state));
    b.title = s.cwd + (s.id === sessionId ? '（表示中）' : '（クリックで参加）');
    b.disabled = s.id === sessionId;
    b.onclick = () => joinSession(s);
    list.appendChild(b);
  });
}

// Show an existing session here; its whole history is replayed from seq 0.
function joinSession(s) {
  sessionId = s.id;
  healthBox = null;
  log.innerHTML = '';
  Object.keys(approvalCards).forEach(k => delete approvalCards[k]);
  turnStatus.textContent = '';
  document.getElementById('cwd').value = s.cwd;
  note(`セッションに参加: ${s.cwd}（${s.started_by === 'muse' ? 'Museが開始' : 'この画面で開始'}）`);
  openEvents();
  setInputEnabled(!s.running);
  refreshSessions();
}

document.getElementById('startBtn').onclick = () =>
  startSession(document.getElementById('cwd').value.trim(), null);

document.getElementById('autoApprove').onchange = async e => {
  if (!sessionId) return;
  const r = await api('/api/auto', { session: sessionId, auto_approve: e.target.checked });
  if (r.status === 410) recover('session_lost');
};

document.getElementById('quitBtn').onclick = async () => {
  if (!confirm('mightyCode for Muse を終了しますか？（モデルサーバーも止まります）')) return;
  clearInterval(monitor);
  recovering = true;
  try { await api('/api/quit', {}); } catch (e) {}
  document.body.innerHTML = '<p style="padding:40px;font-family:sans-serif">mightyCode for Muse を終了しました。このタブは閉じて大丈夫です。</p>';
};

stopBtn.onclick = async () => {
  try {
    const r = await api('/api/stop', { session: sessionId });
    if (r.status === 410) recover('session_lost');
  } catch (e) { showBanner('停止を送信できませんでした（接続なし）', 'error'); }
};

async function send() {
  const text = input.value.trim();
  if (!text || !sessionId) return;
  input.value = '';
  setInputEnabled(false); stopBtn.disabled = false;
  turnStatus.textContent = '送信中…';
  let r;
  try {
    r = await api('/api/chat', { session: sessionId, message: text, by: 'gui' });
  } catch (e) {
    input.value = text; setInputEnabled(true); turnStatus.textContent = '';
    showBanner('送信できませんでした: バックエンドに接続できません。再接続後にもう一度送信してください', 'error');
    checkConnection();
    return;
  }
  if (r.status === 410) { input.value = text; return recover('session_lost'); }
  if (!r.ok) {
    input.value = text; setInputEnabled(true); turnStatus.textContent = '';
    showBanner('送信できませんでした: ' + (r.data.message || r.data.error || r.status), 'error');
  }
}
sendBtn.onclick = send;
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});

// Safari restores pages from the back/forward cache and throttles background
// tabs; re-validate the connection whenever the page becomes visible again.
window.addEventListener('pageshow', e => { if (e.persisted) { checkConnection(); if (sessionId) openEvents(); } });
document.addEventListener('visibilitychange', () => { if (!document.hidden) checkConnection(); });
window.addEventListener('online', checkConnection);
window.addEventListener('focus', checkConnection);

const monitor = setInterval(checkConnection, 3000);

// ---- boot ---------------------------------------------------------------
(async () => {
  const savedCwd = store.get('lc_cwd');
  if (savedCwd) document.getElementById('cwd').value = savedCwd;
  ccSessionId = store.get('lc_cc_session');
  const resumeRaw = store.get('lc_resume', sessionStorage);
  const draft = store.get('lc_draft', sessionStorage);
  store.set('lc_resume', null, sessionStorage);
  store.set('lc_draft', null, sessionStorage);
  await checkConnection();
  if (resumeRaw) {
    try {
      const rs = JSON.parse(resumeRaw);
      if (rs.had && rs.cwd && Date.now() - rs.at < 5 * 60 * 1000) {
        note(rs.reason === 'restarted'
          ? 'バックエンドの再起動を検知したため、セッションを自動で張り直しました'
          : '失われたセッションを自動で張り直しました');
        const ok = await startSession(rs.cwd, rs.cc);
        if (ok && draft) { input.value = draft; note('未送信だった入力を復元しました。内容を確認して送信してください'); }
      }
    } catch (e) {}
  }
})();
