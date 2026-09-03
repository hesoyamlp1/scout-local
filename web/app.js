(() => {
'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $all = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g,
  char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const fmtN = n => n == null ? '' : n >= 10000 ? `${(n / 10000).toFixed(1)}万` : String(n);
const fmtMs = ms => ms == null ? '' : ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
const modelName = model => String(model || '').replace(/^gpt-5\.6-/, '').replace(/^gpt-/, '') || 'Codex';
const runtimeLabel = (model, reasoning) => `${modelName(model)}${reasoning ? ` · ${reasoning}` : ''}`;
const host = url => { try { return new URL(url).host.replace(/^www\./, ''); } catch { return url || ''; } };

const S = {
  session: localStorage.getItem('scout.session') || '',
  turns: [], sessions: [], shelf: [], memory: [], sources: [], candidates: [],
  live: null, sse: null, sseReconnectTimer: 0, seq: 0, busy: false,
  workspace: 'chat', libraryFilter: 'all', reading: null, readerReturnWorkspace: 'library', readerView: 'target', readerChapter: 0, readerSegment: 0,
  readerSession: '', readerTurns: [], readerLive: null, readerSeq: 0, readerSse: null, readerSseTimer: 0, readerBusy: false,
  readerQuote: null, readerSelection: null, readerChatOpen: true,
  noteSeg: null, noteQuote: '', quote: null, config: null, toastTimer: null,
};

const WORKSPACES = {
  chat: { title: '新的对话', status: '可以问任何事，也可以说想找什么来看' },
  library: { title: '你的书架', status: '系列、文章与中文译文' },
  sources: { title: '礼貌关注的来源', status: '来源地图与尚未进入书架的新发现' },
  memory: { title: 'Scout 对你的了解', status: '可以查看和删除的长期记忆' },
  settings: { title: '设置', status: 'Codex Runtime、模型与工作边界' },
};

const SEND_DEBOUNCE_MS = 500;
const SETTINGS_DEBOUNCE_MS = 300;
const SCROLL_BOTTOM_TOLERANCE = 24;
let lastSendAt = 0;
let lastReaderSendAt = 0;
let settingsDirty = false;
let settingsSaveTimer = 0;
let settingsSaveInFlight = null;

function md(text) {
  const raw = window.marked ? marked.parse(String(text || ''), { breaks: true, gfm: true })
    : esc(text).replace(/\n/g, '<br>');
  const clean = window.DOMPurify ? DOMPurify.sanitize(raw) : raw;
  return clean.replace(/\[(\d{1,3})\](?!\()/g,
    (_match, num) => `<a class="cite" data-cite="${num}">[${num}]</a>`);
}

function codexMessageText(data = {}) {
  const text = String(data.text || data.status || '').trim();
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed.answer === 'string' && Array.isArray(parsed.series_ids)) return parsed.answer.trim();
  } catch { /* 普通 commentary 本来就不是 JSON */ }
  return text;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (response.status === 401) {
    const next = `${location.pathname}${location.search}`;
    location.replace(`/login?next=${encodeURIComponent(next)}`);
    throw new Error('登录已失效');
  }
  if (!response.ok) {
    const detail = await response.text().catch(() => '');
    throw new Error(`${response.status} ${detail}`.slice(0, 220));
  }
  return response.json();
}

function toast(message) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(S.toastTimer);
  S.toastTimer = setTimeout(() => el.classList.remove('show'), 2400);
}

function workspaceFromUrl() {
  const view = new URLSearchParams(location.search).get('view') || 'chat';
  return WORKSPACES[view] ? view : 'chat';
}

function setWorkspaceTopbar(view) {
  if (view === 'chat') {
    const session = S.sessions.find(item => item.id === S.session);
    $('#conversation-title').textContent = session?.title || '新的对话';
    $('#conversation-status').textContent = session?.running ? '正在处理' : S.turns.length ? `${S.turns.length} 轮对话` : WORKSPACES.chat.status;
    return;
  }
  $('#conversation-title').textContent = WORKSPACES[view].title;
  $('#conversation-status').textContent = WORKSPACES[view].status;
}

function showWorkspace(view, { push = true, load = true } = {}) {
  if (!WORKSPACES[view]) view = 'chat';
  S.workspace = view;
  $all('[data-workspace]').forEach(page => {
    const active = page.dataset.workspace === view;
    page.classList.toggle('active', active);
    page.setAttribute('aria-hidden', String(!active));
  });
  const actionFor = { chat: 'chat', library: 'open-library', sources: 'open-sources', memory: 'open-memory' };
  $all('.nav-item').forEach(item => item.classList.toggle('active', item.dataset.action === actionFor[view]));
  $('.account-row').classList.toggle('active', view === 'settings');
  setWorkspaceTopbar(view);
  if (push && workspaceFromUrl() !== view) {
    history.pushState({ view }, '', view === 'chat' ? '/' : `/?view=${view}`);
  }
  closeRail();
  if (!load) return;
  if (view === 'library') loadShelf($('#library-search').value.trim());
  if (view === 'sources') loadSources();
  if (view === 'memory') loadMemory();
  if (view === 'settings') loadSettings();
}

function normalizeTranslations(turn) {
  if (Array.isArray(turn?.translations) && turn.translations.length) return turn.translations;
  return turn?.translation?.series ? [turn.translation] : [];
}

function renderStoryCard(item, translation = null, liveJob = null) {
  const series = item?.series_id || item?.series || translation?.series || liveJob?.series || '';
  const title = item?.title || translation?.title || liveJob?.title || '正在准备的内容';
  const url = item?.url || translation?.url || liveJob?.url || '';
  const source = item?.source || host(url);
  const complete = item?.complete || (liveJob?.complete ? '完整正文' : '');
  const chars = translation?.text?.length || item?.chars || liveJob?.chars || 0;
  const summaryRaw = item?.summary || (translation ? '完整译文已经保存，可以随时回来继续读。' : '正文已经读入内容库。');
  const summary = summaryRaw.length > 220 ? `${summaryRaw.slice(0, 220)}…` : summaryRaw;
  const segments = translation?.segments?.length || liveJob?.total || 0;
  const semanticSeries = item?.kind === 'series';
  const done = Boolean(translation || liveJob?.done);
  const progress = liveJob && !done ? Math.max(5, Math.round((liveJob.current || 0) / Math.max(1, liveJob.total || 1) * 100)) : 100;
  const stateClass = done ? 'ready' : liveJob ? 'translating' : 'queued';
  let state;
  if (done) {
    state = `<div class="story-state"><span class="state-icon">✓</span><span><b>中文已就绪</b><small>${fmtN(chars)} 字${segments ? ` · ${segments} 段` : ''}</small></span></div>`;
  } else if (liveJob) {
    state = `<div class="progress-row"><div class="progress-copy"><span>正在翻译</span><b>${liveJob.current || 0} / ${liveJob.total || '?'} 段</b></div><div class="progress-track"><span style="width:${progress}%"></span></div></div>`;
  } else if (semanticSeries) {
    state = `<div class="story-state muted"><span class="state-icon">目</span><span><b>系列已整理</b><small>${item.fetched_chapters || 0}/${item.chapters || 0} 章已抓${item.translated_chapters ? ` · ${item.translated_chapters} 章中文` : ''}</small></span></div>`;
  } else {
    state = `<div class="story-state muted"><span class="state-icon">↳</span><span><b>已读入内容库</b><small>${fmtN(chars)} 字${complete ? ` · ${esc(complete)}` : ''}</small></span></div>`;
  }
  const primary = series
    ? done || !liveJob
      ? `<button class="primary-button" data-action="read" data-series="${esc(series)}">${translation ? '开始阅读' : '打开内容'}</button>`
      : `<button class="primary-button disabled" disabled>翻好后阅读</button>`
    : '';
  const translate = series && !translation && !liveJob
    ? `<button class="secondary-button" data-action="translate-story" data-series="${esc(series)}" data-title="${esc(title)}">${semanticSeries ? '完整翻译系列' : '翻成中文'}</button>` : '';
  const origin = url ? `<a class="text-button story-origin" href="${esc(url)}" target="_blank" rel="noreferrer">原网页 ↗</a>` : '';
  return `<article class="story-card ${stateClass}" data-series="${esc(series)}">
    <div class="story-number">${item?.num ? String(item.num).padStart(2, '0') : '文'}</div>
    <div class="story-main">
      <div class="story-meta"><span>${esc(source)}</span>${complete ? `<i></i><span class="complete">${esc(complete)}</span>` : ''}</div>
      <h2>${esc(title)}</h2><p>${esc(summary)}</p>${state}
      <div class="story-actions">${primary}${translate}${origin}<span class="shelf-mark">已进入书架</span></div>
    </div>
  </article>`;
}

function renderArtifacts(turn) {
  const items = turn?.items || [];
  const translations = normalizeTranslations(turn);
  const bySeries = new Map(translations.map(tr => [tr.series, tr]));
  const docs = items.filter(item => item.kind === 'doc' || item.kind === 'series');
  const seen = new Set();
  const cards = [];
  for (const item of docs) {
    const series = item.series_id || item.id;
    if (seen.has(series)) continue;
    seen.add(series);
    cards.push(renderStoryCard(item, bySeries.get(series) || null));
  }
  for (const tr of translations) {
    if (seen.has(tr.series)) continue;
    seen.add(tr.series);
    cards.push(renderStoryCard(null, tr));
  }
  const other = items.filter(item => item.kind !== 'doc' && item.kind !== 'series');
  const chips = other.length ? `<div class="source-chips">${other.map(item =>
    `<button class="source-chip" data-item-num="${item.num || ''}"><span>[${item.num || ''}]</span>${esc(item.title || item.kind)}</button>`).join('')}</div>` : '';
  return `${cards.length ? `<div class="content-deck">${cards.join('')}</div>` : ''}${chips}`;
}

function activityStep(live, key, label, done = false) {
  live.activity ||= [];
  const current = live.activity[live.activity.length - 1];
  if (current?.key === key) { current.label = label; current.state = done ? 'done' : 'active'; return; }
  live.activity.forEach(step => { if (step.state === 'active') step.state = 'done'; });
  live.activity.push({ key, label, state: done ? 'done' : 'active' });
  if (live.activity.length > 12) live.activity = live.activity.slice(-12);
}

function updateActivity(live, type, data = {}) {
  const label = STATUS[type]?.(data) || live.status || '正在处理';
  const keys = {
    job_queued: 'queued', worker_claimed: 'claimed', codex_start: 'understand',
    codex_search: `search:${data.id || ''}`, codex_tool: `tool:${data.id || data.tool || ''}`,
    codex_message: `message:${data.id || label}`,
    codex_progress: `progress:${label}`, search_start: 'search', search_done: 'search_done',
    fetch_try: `fetch:${host(data.url)}`, fetch_ok: `read:${host(data.url)}`,
    translate_start: `translate:${data.series || ''}`, translate_segment: `translate:${data.series || ''}`,
    translate_done: `translated:${data.series || ''}`, answer_start: 'answer', answer_delta: 'answer',
  };
  const key = keys[type]; if (!key) return;
  activityStep(live, key, type === 'answer_start' || type === 'answer_delta' ? '开始组织回答' : label, Boolean(data.done));
  if (type === 'answer_start' || type === 'answer_delta') {
    live.activity.forEach(step => { step.state = 'done'; }); live.activityOpen = false;
  }
}

function renderActivity(live, scope = 'main') {
  const facts = [];
  if (live.searchResults != null) facts.push(`<span>搜索候选内容</span><b>${live.searchResults} 条</b>`);
  if (live.fetched) facts.push(`<span>真正打开阅读</span><b>${live.fetched} 篇</b>`);
  const jobs = Object.values(live.jobs || {});
  if (jobs.length) facts.push(`<span>逐篇翻译</span><b>${jobs.filter(job => job.done).length}/${jobs.length} 篇完成</b>`);
  if (live.worker) facts.push(`<span>执行引擎</span><b>${esc(live.worker)}</b>`);
  if (live.model) facts.push(`<span>本轮模型</span><b>${esc(runtimeLabel(live.model, live.reasoning))}</b>`);
  const steps = live.activity?.length ? live.activity : [{ key: 'current', label: live.status || '正在处理', state: 'active' }];
  const answered = Boolean(live.answer);
  const summary = answered ? '已经读完，正在回答' : (live.status || '正在处理');
  const action = scope === 'reader' ? 'toggle-reader-activity' : 'toggle-log';
  const retry = live.retryJob ? `<button class="retry-button" data-action="retry-job" data-job="${esc(live.retryJob)}">重新执行</button>` : '';
  return `<div class="agent-activity ${live.activityOpen ? 'expanded' : ''} ${answered ? 'answered' : ''} ${live.retryJob ? 'failed' : ''}">
    <button class="activity-summary" data-action="${action}"><span class="activity-orbit"><i></i></span><strong>${esc(summary)}</strong><small>${answered ? '过程已收起' : '点击查看过程'}</small><span class="activity-chevron">⌄</span></button>
    <div class="activity-detail"><div class="activity-detail-inner"><ol>${steps.map(step => `<li class="${step.state}"><i>${step.state === 'done' ? '✓' : ''}</i><span>${esc(step.label)}</span></li>`).join('')}</ol>${facts.length ? `<div class="activity-facts">${facts.join('')}</div>` : ''}</div></div>${retry}
  </div>`;
}

function renderWorklog(live) { return renderActivity(live, 'main'); }

function renderTurn(turn) {
  const metrics = turn.metrics || {};
  const meta = [metrics.model ? runtimeLabel(metrics.model, metrics.reasoning) : '',
    metrics.ms ? fmtMs(metrics.ms) : '', metrics.calls ? `${metrics.calls} 次模型调用` : ''].filter(Boolean).join(' · ');
  const retry = metrics.stopped === 'error' && metrics.retry_job ? `<button class="retry-button" data-action="retry-job" data-job="${esc(metrics.retry_job)}">重新执行</button>` : '';
  return `<article class="turn user-turn"><div class="bubble user-bubble">${esc(turn.question)}</div></article>
    <article class="turn assistant-turn" data-turn="${esc(turn.id || '')}"><div class="assistant-mark">S</div><div class="assistant-body">
      <div class="answer">${md(turn.answer)}</div>${renderArtifacts(turn)}
      ${retry}${meta ? `<div class="answer-meta"><i>✓</i><span>${esc(meta)}</span></div>` : ''}
    </div></article>`;
}

function renderLive() {
  const live = S.live;
  if (!live) return '';
  const jobs = Object.values(live.jobs || {});
  const jobCards = jobs.length ? `<div class="content-deck">${jobs.map(job => renderStoryCard(null, null, job)).join('')}</div>` : '';
  return `<article class="turn user-turn"><div class="bubble user-bubble">${esc(live.question)}</div></article>
    <article class="turn assistant-turn live-turn"><div class="assistant-mark">S</div><div class="assistant-body">
      ${renderWorklog(live)}${live.answer ? `<div class="answer answer-enter">${md(live.answer)}</div>` : ''}${jobCards}
    </div></article>`;
}

function renderConversation(forceBottom = false) {
  const conversation = $('#conversation');
  const previousTop = conversation.scrollTop;
  const nearBottom = conversation.scrollHeight - previousTop - conversation.clientHeight <= SCROLL_BOTTOM_TOLERANCE;
  const completedTurns = S.turns.filter(turn => turn.done && (!S.live?.turn || turn.id !== S.live.turn));
  $('#welcome').classList.toggle('hidden', Boolean(completedTurns.length || S.live));
  $('#turn-list').innerHTML = completedTurns.map(renderTurn).join('') + renderLive();
  // 不排队一个稍后才执行的滚动：用户可能已经开始往上看，它会把位置抢回去。
  if (forceBottom || nearBottom) conversation.scrollTop = conversation.scrollHeight;
  else conversation.scrollTop = Math.min(previousTop, conversation.scrollHeight - conversation.clientHeight);
}

const STATUS = {
  job_queued: data => data.worker?.online ? '任务已排队，等待 Codex 领取' : '任务已保存；Codex Worker 暂时离线',
  worker_claimed: () => 'Codex 已领取任务',
  codex_start: data => `Codex ${data.model || ''} 正在理解任务`,
  codex_search: data => data.status || '正在使用 OpenAI 托管搜索发现来源',
  codex_tool: data => data.status || `${data.tool || 'Codex'} 正在操作 Scout 内容库`,
  codex_progress: data => data.status || 'Codex 正在处理',
  codex_message: data => codexMessageText(data) || 'Codex 正在处理',
  agent_start: data => data.agent === 'main' ? '正在理解你的意思' : `${data.agent} 正在工作`,
  search_start: data => `正在搜索：${(data.queries || []).join(' / ')}`,
  search_done: data => `找到 ${data.results || 0} 条候选，正在挑真正值得读的`,
  fetch_try: data => `正在打开 ${host(data.url)}`,
  fetch_ok: data => `读到了《${data.title || host(data.url)}》`,
  fetch_fail: data => `${host(data.url)} 没有读下来，正在换一个`,
  find_query: data => `正在记忆里找：${(data.queries || []).join(' / ')}`,
  series_filled: data => `已经补读 ${data.read || 0} 页，正在确认全文`,
  translate_start: data => `开始逐篇翻译《${data.title || '这篇内容'}》`,
  translate_segment: data => `正在翻译第 ${(data.idx || 0) + 1}/${data.total || '?'} 段`,
  translate_done: data => `《${data.title || '这篇内容'}》中文已经就绪`,
  translation_reused: data => `《${data.title || '这篇内容'}》以前翻过，已经直接取回`,
};

function connect(sessionId) {
  clearTimeout(S.sseReconnectTimer);
  if (S.sse) S.sse.close();
  if (!sessionId) return;
  const stream = new EventSource(`/api/stream/${sessionId}?since=${S.seq}`);
  S.sse = stream;
  stream.onmessage = event => {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    const seq = Number(message.seq || 0);
    if (seq <= S.seq) return;
    S.seq = seq;
    handleEvent(message.type, message.data || {});
  };
  stream.onerror = () => {
    stream.close();
    if (S.sse !== stream || S.session !== sessionId) return;
    S.sse = null;
    clearTimeout(S.sseReconnectTimer);
    S.sseReconnectTimer = setTimeout(() => connect(sessionId), 300);
  };
}

function handleEvent(type, data) {
  if (type === 'turn_start') {
    S.live = { turn: data.turn || '', question: data.question, answer: '', status: '正在排队', jobs: {}, fetched: 0, searchResults: null, job: data.job || '', activity: [{ key: 'queued', label: '准备任务', state: 'active' }] };
    setBusy(true); renderConversation(); return;
  }
  if (type === 'job_queued' && !S.live && data.question) {
    S.live = { question: data.question, answer: '', status: '任务已重新排队', jobs: {}, fetched: 0, searchResults: null, job: data.job || '', activity: [{ key: 'queued', label: '任务已重新排队', state: 'active' }] };
    setBusy(true); renderConversation();
  }
  const live = S.live;
  if (!live) return;
  if (STATUS[type]) live.status = STATUS[type](data);
  updateActivity(live, type, data);
  if (type === 'job_queued') { live.job = data.job || live.job; live.worker = data.worker?.online ? 'Codex 在线' : 'Codex 离线'; }
  if (type === 'worker_claimed') live.worker = 'Codex 在线';
  if (type === 'codex_start') { live.model = data.model || ''; live.reasoning = data.reasoning || ''; }
  if (type === 'search_done') live.searchResults = data.results || 0;
  if (type === 'fetch_ok') live.fetched += 1;
  if (type === 'answer_delta') live.answer += data.text || '';
  if (type === 'translate_start') {
    live.jobs[data.series] = { series: data.series, title: data.title, chars: data.chars, complete: data.complete, current: 0, total: 0, done: false };
  }
  if (type === 'translate_segment') {
    const job = live.jobs[data.series] ||= { series: data.series, title: '正在翻译的内容' };
    job.current = (data.idx || 0) + 1; job.total = data.total || job.total; job.done = false;
  }
  if (type === 'translate_done' || type === 'translation_reused') {
    const job = live.jobs[data.series] ||= { series: data.series };
    Object.assign(job, data, { current: data.segments || job.total || 1, total: data.segments || job.total || 1, done: true });
  }
  if (type === 'answer_final') {
    live.answer = data.text || live.answer; live.items = data.items || [];
    live.translation = data.translation || null; live.translations = data.translations || [];
  }
  if (type === 'turn_done') {
    if (data.metrics?.stopped === 'error' || live.retryJob) {
      live.retryJob ||= data.retry_job || ''; setBusy(false); renderConversation(); loadSessions(); return;
    }
    S.live = null; setBusy(false); reloadSession(false); loadShelf(); loadSessions(); return;
  }
  if (type === 'error') {
    live.status = `这次没有完成：${data.error || '未知错误'}`; live.retryJob = data.job || ''; setBusy(false);
  }
  if (S.workspace === 'chat') $('#conversation-status').textContent = live.status;
  renderConversation();
}

function setBusy(busy) {
  S.busy = busy;
  $('#send-button').disabled = busy;
  $('#send-button').textContent = busy ? '·' : '↑';
}

async function send(text = '') {
  if (S.busy) return toast('这一轮还在处理，完成后就能继续说');
  const now = performance.now();
  if (now - lastSendAt < SEND_DEBOUNCE_MS) return;
  const input = $('#prompt');
  let question = (text || input.value).trim();
  if (!question) return;
  lastSendAt = now;
  if (S.quote) {
    question = `【引用《${S.quote.title}》第 ${S.quote.seg + 1} 段】\n原文：${S.quote.source}\n译文：${S.quote.target}\n\n${question}`;
    S.quote = null; renderQuote();
  }
  input.value = ''; input.style.height = '';
  setBusy(true);
  S.live = { turn: '', question, answer: '', status: '正在发送', jobs: {}, fetched: 0, searchResults: null, activity: [{ key: 'send', label: '发送请求', state: 'active' }] };
  renderConversation(true);
  try {
    if (settingsDirty || settingsSaveInFlight) {
      S.live.status = '正在保存模型设置'; renderConversation();
    }
    // 先让 debounce 中或正在提交的模型设置落地，再创建任务的 runtime 快照。
    await flushSettingsSave();
    const result = await api('/api/ask', { method: 'POST', body: JSON.stringify({ question, session: S.session || null }) });
    if (S.live) { S.live.turn = result.turn || S.live.turn; S.live.job = result.job || S.live.job; }
    if (result.session !== S.session) {
      S.session = result.session; S.seq = Number(result.seq || 0); S.turns = [];
      localStorage.setItem('scout.session', S.session); connect(S.session);
    }
  } catch (error) {
    S.live.status = `发不出去：${error.message}`; setBusy(false); renderConversation();
  }
}

async function reloadSession(forceBottom = false) {
  if (!S.session) return;
  try {
    const session = await api(`/api/sessions/${S.session}`);
    S.seq = Number(session.seq || 0);
    S.turns = (session.turns || []).filter(turn => turn.done);
    if (S.workspace === 'chat') {
      $('#conversation-title').textContent = session.title || '新的对话';
      $('#conversation-status').textContent = session.running ? '正在处理' : `${S.turns.length} 轮对话`;
    }
    setBusy(Boolean(session.running)); renderConversation(forceBottom);
  } catch { /* 会话可能已被回收 */ }
}

async function loadSessions() {
  try {
    const data = await api('/api/sessions');
    S.sessions = data.sessions || [];
    $('#chat-count').textContent = S.sessions.length || '';
    $('#history-list').innerHTML = S.sessions.length ? S.sessions.slice(0, 10).map(session =>
      `<button class="history-item ${session.id === S.session ? 'active' : ''}" data-action="open-session" data-session="${esc(session.id)}"><span>${session.running ? '● ' : ''}${esc(session.title)}</span><small>${session.running ? '处理中' : `${session.turns} 轮`}</small></button>`).join('')
      : '<div class="rail-loading">还没有对话</div>';
  } catch { $('#history-list').innerHTML = '<div class="rail-loading">后端还没有启动</div>'; }
}

function filteredShelf() {
  return S.shelf.filter(item => S.libraryFilter === 'translated' ? item.translation : S.libraryFilter === 'notes' ? item.notes : true);
}

function renderLibrary() {
  const items = filteredShelf();
  $('#library-count').textContent = S.shelf.length || '';
  $('#library-list').innerHTML = items.length ? items.map((item, index) =>
    `<button class="library-book" data-action="read" data-series="${esc(item.series)}"><span class="book-index">${String(index + 1).padStart(2, '0')}</span><div><b>${esc(item.title)}</b><small>${item.kind === 'series' ? `${item.fetched_chapters || 0}/${item.chapters || 0} 章${item.translated_chapters ? ` · ${item.translated_chapters} 章中文` : ''}` : `${item.translation ? '已有中文' : item.status} · ${esc(item.host)}${item.notes ? ` · ${item.notes} 条批注` : ''}`}</small><i><span style="width:${item.kind === 'series' ? Math.round((item.fetched_chapters || 0) / Math.max(1, item.chapters || 1) * 100) : item.translation ? 100 : item.complete ? 65 : 25}%"></span></i></div></button>`).join('')
    : '<div class="empty-memory">这里还没有内容。让 Scout 真正读进一篇文章后，它会出现在这里。</div>';
}

async function loadShelf(query = '') {
  try {
    const data = await api(`/api/shelf?q=${encodeURIComponent(query)}`);
    S.shelf = data.items || []; renderLibrary();
  } catch { /* 后端还没有启动 */ }
}

async function loadMemory() {
  $('#memory-list').innerHTML = '<div class="empty-memory">正在读取长期记忆…</div>';
  try {
    const data = await api('/api/memory');
    S.memory = data.items || [];
    $('#memory-count').textContent = `${S.memory.length} 条`;
    $('#memory-list').innerHTML = S.memory.length ? S.memory.map(item =>
      `<div class="memory-item"><span class="memory-symbol">${item.confirmed ? '你' : '✦'}</span><div><b>${item.confirmed ? '你明确说过' : 'Scout 归纳的'}</b><p>${esc(item.text)}</p><small>${item.confirmed ? '明确记忆' : '可随时删除的推断'}</small></div><button data-action="forget-memory" data-memory="${esc(item.id)}">×</button></div>`).join('')
      : '<div class="empty-memory">还没有长期记忆。正常聊天不需要为了“建立档案”而额外做什么。</div>';
  } catch (error) { $('#memory-list').innerHTML = `<div class="empty-memory">记忆暂时打不开：${esc(error.message)}</div>`; }
}

function renderSources() {
  $('#source-count').textContent = S.sources.filter(item => item.enabled).length || '';
  $('#source-summary').textContent = `${S.sources.length} 个来源`;
  $('#source-list').innerHTML = S.sources.length ? S.sources.map(source => {
    const state = source.enabled ? source.status === 'error' ? '检查失败' : '正在关注' : '已停用';
    const last = source.last_checked ? new Date(source.last_checked * 1000).toLocaleString('zh-CN', { month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit' }) : '尚未检查';
    return `<article class="source-card ${source.enabled ? '' : 'disabled'}"><div><b>${esc(source.name || host(source.url))}</b><a href="${esc(source.url)}" target="_blank" rel="noreferrer">${esc(host(source.url))} ↗</a><p>${esc(source.topic || '未设置主题')}</p><small>${state} · ${last} · ${source.candidate_count || 0} 条新候选${source.error ? ` · ${esc(source.error)}` : ''}</small></div><div class="source-actions"><button data-action="refresh-source" data-source="${esc(source.id)}">检查</button><button data-action="toggle-source" data-source="${esc(source.id)}" data-enabled="${source.enabled ? '0' : '1'}">${source.enabled ? '暂停' : '启用'}</button><button data-action="delete-source" data-source="${esc(source.id)}">删除</button></div></article>`;
  }).join('') : '<div class="empty-memory">还没有长期来源。可以先加入一个新着页、榜单页或喜欢的网站。</div>';
  const fresh = S.candidates.filter(item => item.status === 'new').slice(0, 40);
  $('#candidate-list').innerHTML = fresh.length ? fresh.map(item => `<button class="candidate-card" data-action="read-candidate" data-url="${esc(item.url)}" data-title="${esc(item.title)}"><b>${esc(item.title || item.url)}</b><small>${esc(host(item.url))}</small></button>`).join('') : '<div class="empty-memory">检查来源后，新文章会出现在这里；只有你让 Scout 读取后才进入书架。</div>';
}

async function loadSources() {
  try {
    const data = await api('/api/sources');
    S.sources = data.sources || []; S.candidates = data.candidates || []; renderSources();
  } catch (error) { $('#source-list').innerHTML = `<div class="empty-memory">来源暂时打不开：${esc(error.message)}</div>`; }
}

async function addSource() {
  const url = $('#source-url').value.trim(); if (!url) return;
  const body = { url, name: $('#source-name').value.trim(), topic: $('#source-topic').value.trim(), interval_seconds: 86400 };
  try {
    await api('/api/sources', { method:'POST', body:JSON.stringify(body) });
    $('#source-url').value = ''; $('#source-name').value = ''; $('#source-topic').value = '';
    await loadSources(); toast('来源已经加入，Scout 会礼貌检查');
  } catch (error) { toast(`加入失败：${error.message}`); }
}

function closeRail() { $('#rail').classList.remove('open'); $('.rail-scrim').classList.remove('open'); }

async function openBook(series, chapterIndex = 0) {
  try {
    S.readerReturnWorkspace = S.workspace;
    const previousSeries = S.reading?.series || '';
    S.reading = await api(`/api/read/${series}`); S.readerChapter = chapterIndex; S.readerSegment = 0; S.noteSeg = null; S.noteQuote = ''; S.readerQuote = null; S.readerSelection = null;
    if (previousSeries !== S.reading.series) {
      clearTimeout(S.readerSseTimer); if (S.readerSse) S.readerSse.close();
      S.readerSse = null; S.readerSession = ''; S.readerTurns = []; S.readerLive = null; S.readerSeq = 0; S.readerBusy = false;
    }
    if (S.reading.kind === 'series' && !S.reading.chapters?.[S.readerChapter]?.document) {
      const firstReady = S.reading.chapters.findIndex(chapter => chapter.document);
      S.readerChapter = firstReady >= 0 ? firstReady : 0;
    }
    if (!readerContext().doc?.translation) S.readerView = 'source';
    $('#reader-back-label').textContent = `返回${WORKSPACES[S.readerReturnWorkspace]?.title || '书架'}`;
    S.readerChatOpen = innerWidth > 900;
    renderReader(); renderReaderQuote(); renderReaderChat();
    $('#reader').classList.remove('chat-collapsed'); $('#reader').classList.add('open'); $('#reader').classList.toggle('chat-open', S.readerChatOpen); $('#reader').setAttribute('aria-hidden', 'false');
    loadReaderChat(true);
  } catch (error) { toast(`打不开：${error.message}`); }
}

function readerContext() {
  if (!S.reading) return { work: null, chapter: null, doc: null };
  if (S.reading.kind !== 'series') return { work: null, chapter: null, doc: S.reading };
  const chapter = S.reading.chapters?.[S.readerChapter] || null;
  return { work: S.reading, chapter, doc: chapter?.document || null };
}

function renderReader() {
  const { work, chapter, doc } = readerContext();
  if (!doc) return;
  const segments = doc.translation?.segments || [];
  const sourceOnly = !segments.length;
  const bodySegments = sourceOnly
    ? ((doc.source_segments?.length ? doc.source_segments : (doc.pages || []).map((page, index) => ({ idx: index, total: doc.pages.length, source: page.text, target: '' }))))
    : segments;
  if (sourceOnly) S.readerView = 'source';
  const chapterTitle = chapter?.title || chapter?.label || doc.title;
  $('#reader-work-title').textContent = work?.title || doc.title;
  $('#reader-source').href = doc.url;
  const chapterSelect = $('#reader-chapter-select');
  chapterSelect.innerHTML = work ? work.chapters.map((item, index) =>
    `<option value="${index}" ${index === S.readerChapter ? 'selected' : ''} ${item.document ? '' : 'disabled'}>${esc(`${item.label || `第 ${index + 1} 章`}${item.title && item.title !== item.label ? ` · ${item.title}` : ''}`)}</option>`
  ).join('') : `<option value="0">${esc(doc.title)}</option>`;
  chapterSelect.disabled = !work;
  $('[data-action="previous-chapter"]').disabled = !work || S.readerChapter <= 0;
  $('[data-action="next-chapter"]').disabled = !work || S.readerChapter >= work.chapter_count - 1;
  const translating = Boolean(S.readerBusy && S.readerLive?.translate);
  $('#article-kicker').textContent = `${chapter?.label ? `${chapter.label} · ` : ''}${doc.status} · ${doc.host}`;
  $('#article-title').textContent = chapterTitle;
  $('#article-deck').textContent = sourceOnly ? '原文已经完整保存。需要中文时，可以直接在这里生成逐段对照译文。' : '本章原文和中文已经逐行对应，可以随时切换阅读方式。';
  $('#article-byline').innerHTML = `<span>${esc(doc.host)}</span><i></i><span>${fmtN(doc.chars)} 字</span><i></i><span>${esc(doc.status)}</span>`;
  $('#reader-translation-cta').classList.toggle('hidden', !sourceOnly);
  $('#reader-translation-cta').classList.toggle('translating', translating);
  $('#reader-translation-title').textContent = translating ? '正在生成本章中文译文' : '生成本章中文译文';
  $('#reader-translation-copy').textContent = translating ? (S.readerLive.status || 'Codex 正在完整读取并逐段翻译…') : 'Codex 会完整读取本章、逐段翻译并保存。';
  $('#reader-translate').disabled = translating; $('#reader-translate').textContent = translating ? '翻译中…' : '翻译本章';
  $('#reader-companion-status').textContent = translating ? '正在翻译当前章' : '已经读过当前章全文';
  const notesBySegment = {};
  (doc.notes || []).forEach(note => (notesBySegment[note.seg_idx] ||= []).push(note));
  $('#article-body').innerHTML = bodySegments.map(segment => {
    const notes = notesBySegment[segment.idx] || [];
    const sourceLines = textLines(segment.source);
    const targetLines = textLines(segment.target);
    const lineCount = Math.max(sourceLines.length, targetLines.length);
    const aligned = Array.from({ length: lineCount }, (_, index) =>
      `<div class="aligned-row"><p class="target-text">${esc(targetLines[index] || '')}</p><p class="source-text">${esc(sourceLines[index] || '')}</p></div>`
    ).join('');
    return `<section class="reader-segment" data-segment="${segment.idx}"><div class="aligned-body">${aligned}</div>${notes.map(note => `<div class="segment-note">${esc(note.text)}</div>`).join('')}</section>`;
  }).join('');
  $('#article-body').className = `article-body ${S.readerView}-view`;
  $all('[data-reader-view]').forEach(button => { button.classList.toggle('active', button.dataset.readerView === S.readerView); button.disabled = sourceOnly && button.dataset.readerView !== 'source'; });
}

function textLines(value) {
  const lines = String(value || '').split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  return lines.length ? lines : [''];
}

function chooseSegment(index) {
  const { chapter, doc } = readerContext(); if (!doc) return null;
  const segments = doc.translation?.segments?.length ? doc.translation.segments : (doc.source_segments || []);
  const segment = segments[index] || { source: '', target: '' };
  return { series: doc.series, seg: index, title: chapter?.title || chapter?.label || doc.title, source: segment.source || '', target: segment.target || '' };
}

function setQuote(index) { S.quote = chooseSegment(index); renderQuote(); }
function renderQuote() {
  $('#quote-strip').classList.toggle('hidden', !S.quote);
  if (S.quote) { $('#quote-title').textContent = `《${S.quote.title}》第 ${S.quote.seg + 1} 段`; $('#quote-text').textContent = S.quote.target || S.quote.source; }
}

function renderReaderQuote() {
  $('#reader-quote').classList.toggle('hidden', !S.readerQuote);
  if (S.readerQuote) $('#reader-quote-text').textContent = S.readerQuote.text;
}

function setReaderQuote(value) {
  if (!value) return;
  const text = String(value.text || value.target || value.source || '').trim();
  if (!text) return;
  S.readerQuote = { ...value, text };
  S.readerChatOpen = true; $('#reader').classList.add('chat-open');
  renderReaderQuote(); $('#reader-prompt').focus();
}

function readerNearBottom() {
  const el = $('#reader-chat-scroll');
  return !el || el.scrollHeight - el.scrollTop - el.clientHeight <= 36;
}

function renderReaderChat(forceBottom = false) {
  const scroll = $('#reader-chat-scroll');
  const stick = forceBottom || readerNearBottom();
  const turns = S.readerTurns.filter(turn => turn.done && turn.id !== S.readerLive?.turn);
  let html = turns.map(turn => {
    const metrics = turn.metrics || {};
    const meta = [turn.question.startsWith('翻译本章') ? '已完成本章翻译' : '已读完当前章', metrics.ms ? fmtMs(metrics.ms) : '', metrics.model ? runtimeLabel(metrics.model, metrics.reasoning) : ''].filter(Boolean).join(' · ');
    return `<section class="reader-chat-turn"><div class="reader-chat-question ${turn.question.startsWith('翻译本章') ? 'translation-action' : ''}">${esc(turn.question)}</div><div class="reader-chat-answer">${md(turn.answer)}</div><div class="reader-answer-meta"><i>✓</i><span>${esc(meta)}</span></div></section>`;
  }).join('');
  if (S.readerLive) {
    html += `<section class="reader-chat-turn"><div class="reader-chat-question ${S.readerLive.translate ? 'translation-action' : ''}">${esc(S.readerLive.question)}</div>${renderActivity(S.readerLive, 'reader')}${S.readerLive.answer ? `<div class="reader-chat-answer answer-enter">${md(S.readerLive.answer)}</div>` : ''}</section>`;
  }
  if (!html) html = '<div class="reader-chat-empty">我已经拿到当前章全文。你可以直接问内容、语气、人物，也可以划选一句再问。</div>';
  $('#reader-chat-list').innerHTML = html;
  $('#reader-quick-asks').classList.toggle('hidden', Boolean(turns.length || S.readerLive));
  const sendButton = $('[data-action="send-reader-prompt"]');
  sendButton.disabled = S.readerBusy; sendButton.textContent = S.readerBusy ? '·' : '↑';
  if (stick) requestAnimationFrame(() => { scroll.scrollTop = scroll.scrollHeight; });
}

function connectReader(sessionId) {
  clearTimeout(S.readerSseTimer); if (S.readerSse) S.readerSse.close();
  if (!sessionId) return;
  const stream = new EventSource(`/api/stream/${sessionId}?since=${S.readerSeq}`);
  S.readerSse = stream;
  stream.onmessage = event => {
    let message; try { message = JSON.parse(event.data); } catch { return; }
    const seq = Number(message.seq || 0); if (seq <= S.readerSeq) return;
    S.readerSeq = seq; handleReaderEvent(message.type, message.data || {});
  };
  stream.onerror = () => {
    stream.close();
    if (S.readerSse !== stream || S.readerSession !== sessionId) return;
    S.readerSse = null; clearTimeout(S.readerSseTimer);
    S.readerSseTimer = setTimeout(() => connectReader(sessionId), 300);
  };
}

function handleReaderEvent(type, data) {
  if (type === 'turn_start') {
    S.readerLive = { turn: data.turn || '', question: data.question || '', answer: '', status: data.reader_action === 'translate' ? '准备完整翻译本章…' : 'Codex 正在读这一章…', translate: data.reader_action === 'translate', activity: [{ key: 'queued', label: '准备任务', state: 'active' }] };
    S.readerBusy = true; renderReaderChat(true); renderReader(); return;
  }
  const live = S.readerLive; if (!live) return;
  if (STATUS[type]) live.status = STATUS[type](data);
  if (type === 'codex_start') live.status = live.translate ? 'Codex 正在完整读取本章' : 'Codex 正在结合全文回答';
  updateActivity(live, type, data);
  if (type === 'answer_delta') live.answer += data.text || '';
  if (type === 'answer_final') live.answer = data.text || live.answer;
  if (type === 'error') { live.status = `这次没有完成：${data.error || '未知错误'}`; S.readerBusy = false; }
  if (type === 'turn_done') {
    const translated = Boolean(live.translate);
    S.readerLive = null; S.readerBusy = false;
    if (translated) refreshReaderAfterTranslation(); else loadReaderChat(false);
    return;
  }
  renderReaderChat(); renderReader();
}

async function loadReaderChat(forceBottom = false) {
  if (!S.reading) return;
  try {
    const data = await api(`/api/reader-chat/${S.reading.series}?chapter=${S.readerChapter}`);
    const changed = data.session !== S.readerSession;
    S.readerSession = data.session; S.readerSeq = Number(data.seq || 0);
    S.readerTurns = (data.turns || []).filter(turn => turn.done);
    S.readerBusy = Boolean(data.running);
    if (!data.running) S.readerLive = null;
    renderReaderChat(forceBottom); connectReader(S.readerSession);
    if (changed && innerWidth <= 900) $('#reader').classList.toggle('chat-open', S.readerChatOpen);
  } catch (error) {
    $('#reader-chat-list').innerHTML = `<div class="reader-chat-empty">陪你聊暂时打不开：${esc(error.message)}</div>`;
  }
}

async function sendReader(text = '') {
  if (!S.reading) return;
  if (S.readerBusy) return toast('上一条还在回答');
  const now = performance.now(); if (now - lastReaderSendAt < SEND_DEBOUNCE_MS) return;
  const input = $('#reader-prompt'); const question = String(text || input.value).trim(); if (!question) return;
  lastReaderSendAt = now; input.value = '';
  const quote = S.readerQuote?.text || '';
  S.readerQuote = null; renderReaderQuote();
  S.readerBusy = true; S.readerLive = { turn: '', question, answer: '', status: '正在发送…', activity: [{ key: 'send', label: '发送问题', state: 'active' }] }; renderReaderChat(true);
  try {
    await flushSettingsSave();
    const result = await api('/api/reader-chat/ask', { method: 'POST', body: JSON.stringify({ series: S.reading.series, chapter: S.readerChapter, question, quote }) });
    if (S.readerLive) S.readerLive.turn = result.turn || '';
    if (result.session !== S.readerSession) { S.readerSession = result.session; S.readerSeq = Number(result.seq || 0); connectReader(S.readerSession); }
  } catch (error) {
    S.readerBusy = false; S.readerLive.status = `发不出去：${error.message}`; renderReaderChat();
  }
}

async function refreshReaderAfterTranslation() {
  if (!S.reading) return;
  try {
    const fresh = await api(`/api/read/${S.reading.series}`);
    S.reading = fresh;
    if (readerContext().doc?.translation) S.readerView = 'target';
    renderReader(); await loadReaderChat(false); loadShelf();
    toast('本章中文译文已经就绪');
  } catch (error) { toast(`译文刷新失败：${error.message}`); }
}

async function translateReaderChapter() {
  const { chapter, doc } = readerContext();
  if (!S.reading || !doc || doc.translation) return;
  if (S.readerBusy) return toast('陪你聊正在处理上一条');
  const now = performance.now(); if (now - lastReaderSendAt < SEND_DEBOUNCE_MS) return;
  lastReaderSendAt = now;
  const label = chapter?.label || chapter?.title || doc.title;
  S.readerBusy = true;
  S.readerLive = { turn: '', question: `翻译本章 · ${label}`, answer: '', status: '正在提交完整翻译任务…', translate: true, activity: [{ key: 'send', label: '提交完整翻译任务', state: 'active' }] };
  S.readerChatOpen = true; $('#reader').classList.add('chat-open');
  renderReaderChat(true); renderReader();
  try {
    await flushSettingsSave();
    const result = await api('/api/reader-chat/translate', { method: 'POST', body: JSON.stringify({ series: S.reading.series, chapter: S.readerChapter }) });
    if (result.already) return refreshReaderAfterTranslation();
    if (S.readerLive) S.readerLive.turn = result.turn || '';
    if (result.session !== S.readerSession) { S.readerSession = result.session; S.readerSeq = Number(result.seq || 0); connectReader(S.readerSession); }
  } catch (error) {
    S.readerBusy = false; S.readerLive.status = `翻译没有启动：${error.message}`; renderReaderChat(); renderReader();
  }
}

function hideSelectionTools() { $('#reader-selection-tools').classList.add('hidden'); }

function captureReaderSelection() {
  if (!$('#reader').classList.contains('open')) return;
  const selection = getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return hideSelectionTools();
  const range = selection.getRangeAt(0); let node = range.commonAncestorContainer;
  if (node.nodeType === Node.TEXT_NODE) node = node.parentElement;
  if (!node || !$('#article-body').contains(node)) return hideSelectionTools();
  const text = selection.toString().trim(); if (!text) return hideSelectionTools();
  const segment = node.closest?.('.reader-segment');
  const seg = Number(segment?.dataset.segment ?? -1);
  const context = seg >= 0 ? chooseSegment(seg) : null;
  S.readerSelection = { ...(context || {}), seg, text: text.slice(0, 3000) };
  const rect = range.getBoundingClientRect(); const tools = $('#reader-selection-tools');
  tools.style.left = `${Math.max(120, Math.min(innerWidth - 120, rect.left + rect.width / 2))}px`;
  tools.style.top = `${Math.max(48, rect.top - 8)}px`; tools.classList.remove('hidden');
}

function closeReader() {
  $('#reader').classList.remove('open', 'chat-open'); $('#reader').setAttribute('aria-hidden', 'true'); hideSelectionTools();
  clearTimeout(S.readerSseTimer); if (S.readerSse) S.readerSse.close(); S.readerSse = null;
}

async function goReaderChapter(index) {
  const work = S.reading?.kind === 'series' ? S.reading : null;
  if (!work || index < 0 || index >= work.chapters.length || !work.chapters[index]?.document) return;
  S.readerChapter = index; S.readerSegment = 0; S.readerQuote = null; S.readerSelection = null;
  if (!readerContext().doc?.translation) S.readerView = 'source';
  renderReader(); renderReaderQuote(); hideSelectionTools(); $('#paper').scrollTop = 0;
  await loadReaderChat(false);
}

async function loadSettings() {
  try {
    const [config, stats] = await Promise.all([api('/api/config'), api('/api/stats')]);
    S.config = config;
    const codex = stats.codex || {};
    if (codex.enabled) {
      const worker = codex.worker || {};
      const codexFields = config.groups?.Codex || [];
      const selectedModel = codexFields.find(field => field.key === 'CODEX_MODEL')?.value || 'gpt-5.6-terra';
      const selectedReasoning = codexFields.find(field => field.key === 'CODEX_REASONING')?.value || 'medium';
      $('#settings-summary').textContent = `${runtimeLabel(selectedModel, selectedReasoning)} · ${worker.online ? 'Worker 在线' : 'Worker 离线'} · ${codex.jobs?.queued || 0} 个排队任务`;
      $('#settings-content').innerHTML = `<section class="settings-group"><h3>模型与推理</h3>${codexFields.map(renderSettingField).join('')}</section><section class="settings-group"><h3>Codex Worker</h3><div class="runtime-card ${worker.online ? 'online' : 'offline'}"><b>${worker.online ? '运行正常' : '当前离线'}</b><span>${esc(worker.id || '尚未连接')}</span><small>${worker.slots || 0} 个槽位 · ${worker.busy || 0} 个忙碌 · 使用 ChatGPT 登录态与 OpenAI 托管搜索</small></div></section><section class="settings-group"><h3>工作边界</h3><div class="panel-intro">Codex 负责判断、搜索和翻译；Scout 负责礼貌抓取、分页、书架、批注与恢复。网页不会从 VPS 住宅出口抓取。</div></section>`;
      $('#save-settings').classList.remove('hidden');
    } else {
      $('#settings-summary').textContent = `旧引擎 ${stats.models?.pro || '—'} · 搜索源 ${(stats.search || []).join('、') || '未配置'}`;
      $('#settings-content').innerHTML = Object.entries(config.groups || {}).map(([name, fields]) => `<section class="settings-group"><h3>${esc(name)}</h3>${fields.map(renderSettingField).join('')}</section>`).join('');
      $('#save-settings').classList.remove('hidden');
    }
  } catch (error) { $('#settings-summary').textContent = `设置打不开：${error.message}`; }
}

function renderSettingField(field) {
  let control;
  if (field.type === 'bool') control = `<input data-setting="${field.key}" type="checkbox" ${field.value ? 'checked' : ''}>`;
  else if (field.type === 'secret') control = `<input data-setting="${field.key}" type="password" placeholder="${field.value ? '已配置，留空不改' : '还没配置'}">`;
  else if (field.type === 'choice') control = `<select data-setting="${field.key}" data-type="choice">${(field.choices || []).map(value => `<option value="${esc(value)}" ${value === field.value ? 'selected' : ''}>${esc(value)}</option>`).join('')}</select>`;
  else if (field.type === 'providers') control = `<div class="provider-list">${(S.config.all_providers || []).map(name => `<button data-provider="${name}" class="${(field.value || []).includes(name) ? 'active' : ''}">${name}</button>`).join('')}</div>`;
  else control = `<input data-setting="${field.key}" data-type="${field.type}" type="${field.type === 'int' || field.type === 'float' ? 'number' : 'text'}" value="${esc(field.value ?? '')}">`;
  return `<label class="setting-row"><span><b>${esc(field.label)}</b><small>${esc(field.help || '')}</small></span>${control}</label>`;
}

function collectSettings() {
  if (!S.config) return;
  const fields = {};
  $all('[data-setting]').forEach(input => {
    const key = input.dataset.setting;
    if (input.type === 'checkbox') fields[key] = input.checked;
    else if (input.type === 'password') { if (input.value.trim()) fields[key] = input.value.trim(); }
    else if (input.dataset.type === 'int') fields[key] = parseInt(input.value, 10);
    else if (input.dataset.type === 'float') fields[key] = parseFloat(input.value);
    else fields[key] = input.value;
  });
  const providers = $all('[data-provider].active').map(button => button.dataset.provider);
  if (providers.length) fields.SEARCH_PROVIDERS = providers;
  return fields;
}

async function saveSettingsNow() {
  const fields = collectSettings();
  if (!fields) return;
  const button = $('#save-settings');
  button.disabled = true; button.textContent = '保存中…';
  try {
    const result = await api('/api/config', { method: 'POST', body: JSON.stringify({ fields, agent_model: {}, agent_reasoning: {} }) });
    S.config = result.config || S.config;
    $('#settings-note').textContent = `已保存 ${result.changed.length} 项`; toast('设置已经生效');
    await updateWorkerStatus();
    const codexFields = S.config?.groups?.Codex || [];
    const model = codexFields.find(field => field.key === 'CODEX_MODEL')?.value;
    const reasoning = codexFields.find(field => field.key === 'CODEX_REASONING')?.value;
    if (model) $('#settings-summary').textContent = `${runtimeLabel(model, reasoning)} · 已保存`;
  } catch (error) {
    $('#settings-note').textContent = `保存失败：${error.message}`;
    throw error;
  } finally {
    button.disabled = false; button.textContent = '保存设置';
  }
}

function queueSettingsSave() {
  settingsDirty = true;
  clearTimeout(settingsSaveTimer);
  $('#settings-note').textContent = '等待保存…';
  const model = document.querySelector('[data-setting="CODEX_MODEL"]')?.value;
  const reasoning = document.querySelector('[data-setting="CODEX_REASONING"]')?.value;
  if (model) $('#settings-summary').textContent = `${runtimeLabel(model, reasoning)} · 正在保存…`;
  settingsSaveTimer = setTimeout(() => { flushSettingsSave().catch(() => {}); }, SETTINGS_DEBOUNCE_MS);
}

function flushSettingsSave() {
  clearTimeout(settingsSaveTimer); settingsSaveTimer = 0;
  if (settingsSaveInFlight) return settingsSaveInFlight.then(() => flushSettingsSave());
  if (!settingsDirty) return Promise.resolve();
  settingsDirty = false;
  settingsSaveInFlight = saveSettingsNow().finally(() => { settingsSaveInFlight = null; });
  return settingsSaveInFlight.then(() => settingsDirty ? flushSettingsSave() : undefined);
}

function saveSettings() {
  // 双击保存时第二次直接等第一次；选择在提交期间又变化才会追加一轮。
  if (settingsSaveInFlight && !settingsDirty) return settingsSaveInFlight;
  settingsDirty = true;
  return flushSettingsSave();
}

document.addEventListener('click', async event => {
  const prompt = event.target.closest('[data-prompt]');
  if (prompt) { $('#prompt').value = prompt.dataset.prompt; $('#prompt').focus(); return; }
  const readerPrompt = event.target.closest('[data-reader-prompt]');
  if (readerPrompt) { sendReader(readerPrompt.dataset.readerPrompt); return; }
  const actionElement = event.target.closest('[data-action]');
  if (!actionElement) return;
  const action = actionElement.dataset.action;
  if (action === 'send') send();
  if (action === 'new-chat') {
    S.session = ''; S.turns = []; S.live = null; S.seq = 0; S.busy = false;
    localStorage.removeItem('scout.session'); clearTimeout(S.sseReconnectTimer); if (S.sse) S.sse.close(); S.sse = null;
    showWorkspace('chat'); $('#conversation-title').textContent = '新的对话'; $('#conversation-status').textContent = '可以问任何事，也可以说想找什么来看'; renderConversation(true); closeRail();
  }
  if (action === 'open-session') {
    clearTimeout(S.sseReconnectTimer); if (S.sse) S.sse.close(); S.sse = null;
    showWorkspace('chat'); S.session = actionElement.dataset.session; S.seq = 0; S.live = null; localStorage.setItem('scout.session', S.session); await reloadSession(true); connect(S.session); await loadSessions(); closeRail();
  }
  if (action === 'open-rail') { $('#rail').classList.add('open'); $('.rail-scrim').classList.add('open'); }
  if (action === 'close-rail') closeRail();
  if (action === 'home' || action === 'chat') showWorkspace('chat');
  if (action === 'open-memory') showWorkspace('memory');
  if (action === 'open-library' || action === 'focus-library') showWorkspace('library');
  if (action === 'open-sources') showWorkspace('sources');
  if (action === 'open-settings') showWorkspace('settings');
  if (action === 'toggle-reader-chat') { S.readerChatOpen = !$('#reader').classList.contains('chat-open'); $('#reader').classList.toggle('chat-open', S.readerChatOpen); $('#reader').classList.toggle('chat-collapsed', innerWidth > 900 && !S.readerChatOpen); }
  if (action === 'read') openBook(actionElement.dataset.series);
  if (action === 'close-reader') closeReader();
  if (action === 'translate-story') send(`把《${actionElement.dataset.title}》完整翻译成中文`);
  if (action === 'toggle-log' && S.live) { S.live.activityOpen = !S.live.activityOpen; renderConversation(); }
  if (action === 'toggle-reader-activity' && S.readerLive) { S.readerLive.activityOpen = !S.readerLive.activityOpen; renderReaderChat(); }
  if (action === 'clear-quote') { S.quote = null; renderQuote(); }
  if (action === 'ask-segment') { S.readerSegment = Number(actionElement.dataset.segment); setReaderQuote(chooseSegment(S.readerSegment)); }
  if (action === 'toc-chapter') goReaderChapter(Number(actionElement.dataset.chapter));
  if (action === 'previous-chapter') goReaderChapter(S.readerChapter - 1);
  if (action === 'next-chapter') goReaderChapter(S.readerChapter + 1);
  if (action === 'note-segment') { S.noteSeg = Number(actionElement.dataset.segment); S.noteQuote = ''; $('#note-heading').textContent = `第 ${S.noteSeg + 1} 段的批注`; $('#note-compose').classList.remove('hidden'); $('#note-input').focus(); }
  if (action === 'cancel-note') { S.noteSeg = null; S.noteQuote = ''; $('#note-compose').classList.add('hidden'); }
  if (action === 'save-note' && S.reading && S.noteSeg != null) {
    const text = $('#note-input').value.trim(); if (!text) return;
    const segment = chooseSegment(S.noteSeg) || { series: readerContext().doc?.series || '', source: '', target: '' };
    await api('/api/notes', { method: 'POST', body: JSON.stringify({ series: segment.series, text, seg_idx: S.noteSeg, quote: (S.noteQuote || segment.target || segment.source).slice(0, 3000) }) });
    $('#note-input').value = ''; $('#note-compose').classList.add('hidden'); S.noteSeg = null; S.noteQuote = ''; await openBook(S.reading.series, S.readerChapter); toast('批注已经记下');
  }
  if (action === 'send-reader-prompt') sendReader();
  if (action === 'translate-reader') translateReaderChapter();
  if (action === 'clear-reader-quote') { S.readerQuote = null; renderReaderQuote(); }
  if (action === 'quote-selection' && S.readerSelection) { setReaderQuote(S.readerSelection); hideSelectionTools(); }
  if (action === 'note-selection' && S.readerSelection) {
    S.noteSeg = S.readerSelection.seg; S.noteQuote = S.readerSelection.text;
    $('#note-heading').textContent = '所选文字的批注'; $('#note-compose').classList.remove('hidden'); $('#reader').classList.add('chat-open'); S.readerChatOpen = true; $('#note-input').focus(); hideSelectionTools();
  }
  if (action === 'copy-selection' && S.readerSelection) { await navigator.clipboard.writeText(S.readerSelection.text); hideSelectionTools(); toast('已复制'); }
  if (action === 'forget-memory') {
    await api(`/api/memory/${actionElement.dataset.memory}`, { method: 'DELETE' }); await loadMemory(); toast('这条记忆已经删除');
  }
  if (action === 'add-source') addSource();
  if (action === 'refresh-source') {
    actionElement.disabled = true; actionElement.textContent = '检查中…';
    try { await api(`/api/sources/${actionElement.dataset.source}/refresh`, { method:'POST', body:'{}' }); await loadSources(); toast('来源检查完成'); }
    catch (error) { toast(`检查失败：${error.message}`); }
  }
  if (action === 'toggle-source') {
    await api(`/api/sources/${actionElement.dataset.source}/enabled`, { method:'POST', body:JSON.stringify({ enabled: actionElement.dataset.enabled === '1' }) }); await loadSources();
  }
  if (action === 'delete-source') {
    if (confirm('删除这个来源地图？已经进入书架的文章不会删除。')) { await api(`/api/sources/${actionElement.dataset.source}`, { method:'DELETE' }); await loadSources(); }
  }
  if (action === 'read-candidate') {
    showWorkspace('chat'); send(`读取并完整翻译《${actionElement.dataset.title}》：${actionElement.dataset.url}`);
  }
  if (action === 'retry-job') {
    const job = actionElement.dataset.job;
    try {
      await api(`/api/jobs/${job}/retry`, { method: 'POST', body: '{}' });
      if (S.live) { S.live.retryJob = ''; S.live.status = '任务已重新排队'; setBusy(true); renderConversation(); }
    } catch (error) { toast(`重试失败：${error.message}`); }
  }
  if (action === 'logout') {
    try { await api('/api/logout', { method: 'POST', body: '{}' }); }
    finally { location.replace('/login'); }
  }
  if (action === 'save-settings') saveSettings().catch(() => {});
});

$('#conversation').addEventListener('click', event => {
  const cite = event.target.closest('.cite');
  if (!cite) return;
  const turn = event.target.closest('[data-turn]');
  const data = S.turns.find(item => item.id === turn?.dataset.turn);
  const item = data?.items?.find(source => String(source.num) === cite.dataset.cite);
  if (item?.series_id) openBook(item.series_id);
});

$all('[data-reader-view]').forEach(button => button.addEventListener('click', () => {
  if (button.disabled) return; S.readerView = button.dataset.readerView; renderReader();
}));
$('#reader-chapter-select').addEventListener('change', event => goReaderChapter(Number(event.target.value)));
$('#article-body').addEventListener('pointerup', () => setTimeout(captureReaderSelection, 0));
$('#article-body').addEventListener('keyup', captureReaderSelection);
$('#paper').addEventListener('scroll', hideSelectionTools, { passive: true });
$all('[data-library-filter]').forEach(button => button.addEventListener('click', () => {
  $all('[data-library-filter]').forEach(item => item.classList.toggle('active', item === button)); S.libraryFilter = button.dataset.libraryFilter; renderLibrary();
}));
$('#settings-content').addEventListener('click', event => { const button = event.target.closest('[data-provider]'); if (button) button.classList.toggle('active'); });
$('#settings-content').addEventListener('change', event => {
  const input = event.target.closest('[data-setting]');
  if (!input) return;
  if (input.dataset.setting === 'CODEX_MODEL' || input.dataset.setting === 'CODEX_REASONING') queueSettingsSave();
});
$('#library-search').addEventListener('input', event => { clearTimeout(event.target._timer); event.target._timer = setTimeout(() => loadShelf(event.target.value.trim()), 220); });
$('#prompt').addEventListener('input', event => { event.target.style.height = 'auto'; event.target.style.height = `${Math.min(event.target.scrollHeight, 140)}px`; });
$('#reader-prompt').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendReader(); } });
$('#prompt').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); } });

async function boot() {
  await Promise.all([loadSessions(), loadShelf(), loadSources(), updateWorkerStatus()]);
  if (S.session) { await reloadSession(true); connect(S.session); }
  else renderConversation();
  showWorkspace(workspaceFromUrl(), { push: false, load: true });
}

window.addEventListener('popstate', event => {
  showWorkspace(event.state?.view || workspaceFromUrl(), { push: false, load: true });
});

async function updateWorkerStatus() {
  try {
    const data = await api('/api/worker/status');
    const online = Boolean(data.worker?.online);
    $('#worker-status').classList.toggle('online', online);
    $('#worker-status').classList.toggle('offline', !online);
    const runtime = data.runtime || {};
    $('#worker-status b').textContent = runtime.model
      ? runtimeLabel(runtime.model, runtime.reasoning)
      : online ? 'Codex 在线' : 'Codex 离线';
    $('#worker-status').title = `${online ? 'Worker 在线' : 'Worker 离线'}${runtime.model ? ` · 默认 ${runtime.model} · ${runtime.reasoning || ''}` : ''}`;
  } catch { $('#worker-status').classList.add('offline'); }
}
boot();
})();
