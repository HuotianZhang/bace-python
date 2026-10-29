// The assistant explains parameters, the chain and console controls through read-only
// service tools (`docs/service-contract.md` §8a). It opens beside every tab and
// cannot act on the bench. The operator chooses the provider, endpoint, model and
// key. Each question sends the key in a header; browser storage requires the remember
// option and the service does not retain it. Missing credentials or SDKs affect only
// this panel. Pure parsing and state helpers precede the DOM code for testing.

import { h, fill, keyed } from './dom.js';

export const SETTINGS_STORE = 'bace.assistant.settings';
export const KEYS_STORE = 'bace.assistant.keys';

/**
 * Provider presets supply editable starting addresses. The model remains a text field
 * because provider model names vary independently of the console.
 */
export const PRESETS = [
  { name: 'Uni Potsdam · LLM Proxy', base_url: 'https://llm.ki.k8s.rz.uni-potsdam.de' },
  { name: 'OpenAI', base_url: 'https://api.openai.com/v1' },
  { name: 'DeepSeek', base_url: 'https://api.deepseek.com' },
  { name: 'Qwen · DashScope', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
  { name: 'Kimi · Moonshot', base_url: 'https://api.moonshot.cn/v1' },
  { name: 'GLM · Zhipu', base_url: 'https://open.bigmodel.cn/api/paas/v4' },
  { name: 'Ollama on this PC', base_url: 'http://localhost:11434/v1' },
];

export const DEFAULT_SETTINGS = { provider: 'anthropic', base_url: '', model: '' };

/** The predicate identifies a locally served model that requires no key. */
export function isLocal(url) {
  try {
    const host = new URL(url).hostname;
    return host === 'localhost' || host.endsWith('.localhost') || host === '127.0.0.1' || host === '[::1]';
  } catch {
    return false;
  }
}

/** The helper returns missing question requirements or `null` when ready. */
export function missing(settings, key) {
  if (settings.provider === 'openai') {
    if (!settings.base_url) return 'the API address';
    if (!settings.model) return 'the model name';
    if (!key && !isLocal(settings.base_url)) return 'an API key';
    return null;
  }
  return key ? null : 'an Anthropic API key';
}

/** Which stored key goes with these settings: one per provider address. */
export function keySlot(settings) {
  return settings.provider === 'openai' ? settings.base_url : 'anthropic';
}

export const EXAMPLES = [
  'What does BACE measure, and why is the dark reference taken from 0 V?',
  'Why does n_averages on the bace card have the value it has?',
  'Walk me through one BACE measurement at V_oc, click by click.',
];

const TOOL_LABELS = {
  get_session: () => 'read the session',
  get_bench: () => 'read the bench',
  list_modules: () => 'read the modules',
  get_module: (a) => `read module ${a.name}`,
  list_runs: () => 'read the run list',
  get_run: (a) => `read run ${a.run_id}`,
  get_events: () => 'read recent events',
  get_monitors: () => 'read the monitors',
  dry_run: () => 'dry run of a tree',
  read_doc: (a) => `read ${a.name}${a.offset ? ` from ${a.offset}` : ''}`,
};

/** A tool call as the panel says it: `read module bace`. */
export function toolLabel(name, input) {
  const label = TOOL_LABELS[name];
  try {
    return label ? label(input || {}) : name;
  } catch {
    return name;
  }
}

/**
 * The parser returns complete NDJSON frames from `buffer` and the remaining partial
 * line.
 */
export function splitLines(buffer) {
  const lines = buffer.split('\n');
  const rest = lines.pop();
  const frames = [];
  for (const line of lines) {
    if (!line.trim()) continue;
    try { frames.push(JSON.parse(line)); } catch { /* Malformed lines cannot supply service frames. */ }
  }
  return { frames, rest };
}

/**
 * The reducer applies one frame to the transcript. The last `state.items` entry is the
 * answer in progress.
 */
export function applyFrame(state, frame) {
  const items = state.items.slice();
  const last = { ...items[items.length - 1] };
  items[items.length - 1] = last;
  const next = { ...state, items };
  switch (frame.type) {
    case 'conversation':
      next.conversation = frame.id;
      break;
    case 'text':
      last.text = (last.text || '') + frame.text;
      break;
    case 'tool':
      last.tools = [...(last.tools || []), { label: toolLabel(frame.name, frame.input), ok: null }];
      break;
    case 'tool_result': {
      const tools = (last.tools || []).slice();
      const i = tools.map((t) => t.ok).lastIndexOf(null);
      if (i >= 0) tools[i] = { ...tools[i], ok: frame.ok, note: frame.text || null };
      last.tools = tools;
      break;
    }
    case 'done':
      last.done = true;
      last.usage = frame.usage || null;
      if (frame.stop_reason === 'refusal') last.error = 'Declined by the model.';
      next.busy = false;
      break;
    case 'error':
      last.error = frame.message;
      next.busy = false;
      break;
    default:
      break;
  }
  return next;
}

/**
 * The renderer accepts headings, paragraphs, lists, code and tables from model
 * output. DOM construction prevents that output from becoming executable HTML.
 */
export function blocks(text) {
  const lines = String(text || '').replace(/\r/g, '').split('\n');
  const out = [];
  let i = 0;
  const isRow = (s) => /^\s*\|.*\|\s*$/.test(s);
  const cells = (s) => s.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }
    const fence = line.match(/^\s*```/);
    if (fence) {
      const body = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(lines[i])) body.push(lines[i++]);
      i += 1;
      out.push({ type: 'code', text: body.join('\n') });
      continue;
    }
    const head = line.match(/^(#{1,4})\s+(.*)$/);
    if (head) { out.push({ type: 'h', level: head[1].length, text: head[2] }); i += 1; continue; }
    if (isRow(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const header = cells(line);
      const rows = [];
      i += 2;
      while (i < lines.length && isRow(lines[i])) rows.push(cells(lines[i++]));
      out.push({ type: 'table', header, rows });
      continue;
    }
    const bullet = /^\s*[-*]\s+(.*)$/;
    const number = /^\s*\d+[.)]\s+(.*)$/;
    const listType = bullet.test(line) ? 'ul' : number.test(line) ? 'ol' : null;
    if (listType) {
      const re = listType === 'ul' ? bullet : number;
      const items = [];
      while (i < lines.length && re.test(lines[i])) {
        items.push(lines[i].match(re)[1]);
        i += 1;
        // A wrapped item continues on an indented line.
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !bullet.test(lines[i]) && !number.test(lines[i])) {
          items[items.length - 1] += ' ' + lines[i].trim();
          i += 1;
        }
      }
      out.push({ type: listType, items });
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^\s*```/.test(lines[i])
           && !/^#{1,4}\s/.test(lines[i]) && !bullet.test(lines[i]) && !number.test(lines[i])
           && !isRow(lines[i])) {
      para.push(lines[i].trim());
      i += 1;
    }
    if (para.length) out.push({ type: 'p', text: para.join(' ') });
    else { out.push({ type: 'p', text: line.trim() }); i += 1; }
  }
  return out;
}

/** The parser returns inline code and bold spans, leaving other text unchanged. */
export function inline(text) {
  const spans = [];
  const re = /(`[^`]+`|\*\*[^*]+\*\*)/g;
  let at = 0;
  for (const m of String(text).matchAll(re)) {
    if (m.index > at) spans.push({ t: 'text', s: text.slice(at, m.index) });
    const s = m[0];
    spans.push(s[0] === '`' ? { t: 'code', s: s.slice(1, -1) } : { t: 'b', s: s.slice(2, -2) });
    at = m.index + s.length;
  }
  if (at < text.length) spans.push({ t: 'text', s: text.slice(at) });
  return spans;
}

// -- the DOM ----------------------------------------------------------------------
function inlineEl(text) {
  return inline(text).map((span) => (span.t === 'code' ? h('code', { text: span.s })
    : span.t === 'b' ? h('b', { text: span.s }) : span.s));
}

function blockEl(block) {
  switch (block.type) {
    case 'h': return h(`h${Math.min(block.level + 2, 6)}`, inlineEl(block.text));
    case 'code': return h('pre', { text: block.text });
    case 'ul': return h('ul', block.items.map((item) => h('li', inlineEl(item))));
    case 'ol': return h('ol', block.items.map((item) => h('li', inlineEl(item))));
    case 'table':
      return h('div.as-table', h('table',
        h('thead', h('tr', block.header.map((c) => h('th', inlineEl(c))))),
        h('tbody', block.rows.map((row) => h('tr', row.map((c) => h('td', inlineEl(c))))))));
    default: return h('p', inlineEl(block.text));
  }
}

function itemEl(item) {
  if (item.role === 'user') return h('div.as-q', { text: item.text });
  const tools = (item.tools || []).map((t) => h('div', {
    class: 'as-tool' + (t.ok === false ? ' bad' : ''),
    text: `${t.ok === null ? '…' : t.ok ? '✓' : '✕'} ${t.label}${t.note ? ` · ${t.note}` : ''}`,
  }));
  // Token counts are the answer's title: useful for cost, not for reading.
  return h('div.as-a', {
    title: item.usage
      ? `${item.usage.input} in, ${item.usage.output} out${item.usage.cache_read ? `, ${item.usage.cache_read} cached` : ''} tokens`
      : null,
  },
    tools.length ? h('div.as-tools', tools) : null,
    blocks(item.text).map(blockEl),
    !item.done && !item.error && !item.text ? h('div.as-wait', { text: '…' }) : null,
    item.error ? h('div.as-err', { text: item.error }) : null);
}

function readJson(name, fallback) {
  try { return JSON.parse(localStorage.getItem(name)) || fallback; } catch { return fallback; }
}

function writeJson(name, value) {
  try { localStorage.setItem(name, JSON.stringify(value)); } catch { /* storage blocked: this page only */ }
}

function storedKey(settings) {
  return readJson(KEYS_STORE, {})[keySlot(settings)] || '';
}

function storeKey(settings, key) {
  const keys = readJson(KEYS_STORE, {});
  if (key) keys[keySlot(settings)] = key;
  else delete keys[keySlot(settings)];
  writeJson(KEYS_STORE, keys);
}

function hostOf(url) {
  try { return new URL(url).host; } catch { return url; }
}

/**
 * The panel reports open and closed state through `onChange` so the bar control stays
 * consistent.
 */
export function createAssistantPanel({ api, onChange = () => {} }) {
  const settings = { ...DEFAULT_SETTINGS, ...readJson(SETTINGS_STORE, {}) };
  let state = { open: false, status: null, settings, key: storedKey(settings), conversation: null,
                items: [], busy: false, editing: false };
  // The settings form edits `draft`. Only provider or preset changes increment
  // `formVersion`; rebuilding during typing would lose the caret.
  let draft = null;
  let formVersion = 0;
  let controller = null;

  const el = h('aside.assistant', { hidden: true, 'aria-label': 'assistant' });
  const headEl = h('div.as-head');
  const noteEl = h('div.as-note');
  const logEl = h('div.as-log');
  const input = h('textarea', { rows: 3, placeholder: 'Ask about the bench, the physics or the console',
                                title: 'Enter sends; Shift+Enter starts a new line.' });
  const sendBtn = h('button.as-send', { text: 'Ask' });
  const composerEl = h('div.as-composer', input, sendBtn);
  el.append(headEl, noteEl, logEl, composerEl);

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send(input.value);
    }
  });
  sendBtn.addEventListener('click', () => (state.busy ? stop() : send(input.value)));

  function set(patch) {
    state = { ...state, ...patch };
    draw();
  }

  function providerStatus() {
    const all = state.status && state.status.providers;
    return all ? all[state.settings.provider] : null;
  }

  function modelLabel() {
    const s = state.settings;
    if (s.provider === 'openai') return s.model ? `${s.model} · ${hostOf(s.base_url)}` : '';
    return s.model || (state.status && state.status.model) || '';
  }

  function editing() {
    return state.editing || Boolean(missing(state.settings, state.key));
  }

  // `keyFrom` binds a key to its provider address so changing providers cannot carry
  // it across.
  function freshDraft() {
    return { ...state.settings, key: state.key, keyFrom: keySlot(state.settings),
             remember: Boolean(storedKey(state.settings)) };
  }

  function openForm() {
    draft = freshDraft();
    formVersion += 1;
    set({ editing: true });
  }

  function draw() {
    el.hidden = !state.open;
    fill(headEl,
      h('span.as-title', 'Assistant'),
      h('span.as-model', { text: modelLabel() }),
      h('span.spacer'),
      !editing() ? h('button.as-link.as-settings', { text: 'Settings', title: 'Model, address and key', onclick: openForm }) : null,
      state.items.length ? h('button.as-link', { text: 'New', title: 'Starts a new conversation.', onclick: reset }) : null,
      h('button.as-link', { text: '✕', title: 'Close', onclick: () => toggle(false) }));

    const ps = providerStatus();
    if (editing() && !draft) draft = freshDraft();
    // With the form closed, an unavailable provider still says why the input is off.
    keyed(noteEl, JSON.stringify([editing(), formVersion, ps, Boolean(state.status)]),
      () => (editing() ? form()
        : ps && !ps.available ? h('p.as-warn', { text: `Not available on this service: ${ps.reason}` }) : []));

    const near = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
    fill(logEl, state.items.length
      ? state.items.map(itemEl)
      : h('div.as-empty',
        h('p', 'Reads the bench, the modules, the runs and the manual. Changes nothing.'),
        EXAMPLES.map((q) => h('button.as-example', { text: q, onclick: () => send(q) }))));
    if (near) logEl.scrollTop = logEl.scrollHeight;

    const ready = Boolean(ps && ps.available) && !editing();
    input.disabled = !ready;
    sendBtn.disabled = !ready && !state.busy;
    sendBtn.textContent = state.busy ? 'Stop' : 'Ask';
  }

  /** The settings form edits `draft` before the Use action applies it. */
  function form() {
    const d = draft;
    const all = state.status && state.status.providers;
    const ps = all ? all[d.provider] : null;
    const rebuild = (patch) => { Object.assign(draft, patch); formVersion += 1; draw(); };
    // Changing the destination selects its stored key or an empty key.
    const moveTo = (patch) => {
      const next = { ...d, ...patch };
      const key = storedKey(next);
      rebuild({ ...patch, key, keyFrom: keySlot(next), remember: Boolean(key) });
    };
    const field = (label, control) => h('label.as-field', h('span', label), control);
    const provider = h('select', { onchange: (e) => moveTo({ provider: e.target.value, model: '' }) },
      h('option', { value: 'anthropic', text: 'Claude · Anthropic', selected: d.provider === 'anthropic' }),
      h('option', { value: 'openai', text: 'OpenAI-compatible', selected: d.provider === 'openai' }));
    const local = d.provider === 'openai' && isLocal(d.base_url);
    const keyInput = h('input', {
      type: 'password', autocomplete: 'off', value: d.key || '',
      placeholder: d.provider === 'anthropic' ? 'sk-ant-…' : local ? 'Not needed for a model on this PC' : 'API key',
      oninput: (e) => { d.key = e.target.value.trim(); d.keyFrom = keySlot(d); },
    });
    const model = h('input', {
      value: d.model || '', autocomplete: 'off',
      placeholder: d.provider === 'anthropic' ? (state.status && state.status.model) || 'claude-opus-5-5'
        : 'As the provider spells it, e.g. deepseek-chat',
      oninput: (e) => { d.model = e.target.value.trim(); },
    });
    const remember = h('input', { type: 'checkbox', checked: d.remember,
                                  onchange: (e) => { d.remember = e.target.checked; } });
    const rows = [
      ps && !ps.available ? h('p.as-warn', { text: `Not available on this service: ${ps.reason}` }) : null,
      h('p', { text: 'Your key goes with each question and is not stored by the service. Tick the box to keep it in this browser.' }),
      field('Provider', provider),
    ];
    if (d.provider === 'openai') {
      const preset = h('select', { onchange: (e) => e.target.value && moveTo({ base_url: e.target.value }) },
        h('option', { value: '', text: 'Choose …' }),
        PRESETS.map((p) => h('option', { value: p.base_url, text: p.name, selected: p.base_url === d.base_url })));
      rows.push(field('Preset', preset),
        field('Address', h('input', { value: d.base_url || '', placeholder: 'https://…/v1', autocomplete: 'off',
                                      oninput: (e) => {
                                        d.base_url = e.target.value.trim();
                                        if (d.key && keySlot(d) !== d.keyFrom) {
                                          d.key = '';
                                          keyInput.value = '';
                                          d.remember = false;
                                          remember.checked = false;
                                        }
                                      } })));
    }
    rows.push(field('Model', model), field('Key', keyInput),
      h('label.as-remember', remember, ' Remember the key on this computer'));
    const problem = h('p.as-warn');
    const use = () => {
      const next = { provider: d.provider, base_url: d.provider === 'openai' ? d.base_url : '', model: d.model };
      // The last guard: a key typed for one address is never sent to another.
      if (d.key && d.keyFrom !== keySlot(next)) {
        problem.textContent = 'The key was entered for another address: enter it again.';
        return;
      }
      const why = missing(next, d.key);
      if (why) { problem.textContent = `Still needed: ${why}.`; return; }
      writeJson(SETTINGS_STORE, next);
      storeKey(next, d.remember ? d.key : '');
      const changed = JSON.stringify(next) !== JSON.stringify(state.settings);
      draft = null;
      set({ settings: next, key: d.key, editing: false, ...(changed ? { conversation: null } : {}) });
      input.focus();
    };
    const canCancel = state.editing && !missing(state.settings, state.key);
    rows.push(h('div.as-actions', h('button', { text: 'Use', onclick: use }),
      canCancel ? h('button.as-link', { text: 'Cancel', onclick: () => { draft = null; set({ editing: false }); } }) : null,
      storedKey(d) ? h('button.as-link', { text: 'Forget key',
                                           onclick: () => { storeKey(d, ''); rebuild({ key: '', remember: false }); } }) : null),
    problem);
    return rows;
  }

  async function send(text) {
    text = String(text || '').trim();
    if (!text || state.busy || editing()) return;
    input.value = '';
    controller = new AbortController();
    set({ busy: true, items: [...state.items, { role: 'user', text }, { role: 'assistant', text: '', tools: [] }] });
    const s = state.settings;
    try {
      await api.ask({
        text, conversation: state.conversation, key: state.key, signal: controller.signal,
        provider: s.provider, model: s.model || null, base_url: s.provider === 'openai' ? s.base_url : null,
        onFrame: (frame) => {
          state = applyFrame(state, frame);
          if (frame.type === 'error' && frame.status === 401) state = { ...state, editing: true };
          draw();
        },
      });
    } catch (error) {
      const message = error.name === 'AbortError' ? 'Stopped.' : (error.text || error.message);
      state = applyFrame(state, { type: 'error', message });
      if (error.status === 401 || error.status === 422) state = { ...state, editing: true };
    }
    // A stream ending without a terminal frame still closes the turn, including
    // service disconnects.
    if (state.busy) state = applyFrame(state, { type: 'error', message: 'Answer cut off.' });
    controller = null;
    draw();
  }

  function stop() {
    if (controller) controller.abort();
  }

  function reset() {
    stop();
    if (state.conversation) api.forgetConversation(state.conversation).catch(() => {});
    set({ conversation: null, items: [], busy: false });
  }

  async function toggle(open = !state.open) {
    set({ open });
    onChange(open);
    if (open && !state.status) {
      try {
        set({ status: await api.assistantStatus() });
      } catch (error) {
        const down = { available: false, reason: error.text || error.message };
        set({ status: { available: false, providers: { anthropic: down, openai: down } } });
      }
    }
    if (open) input.focus();
  }

  draw();
  return { el, toggle, isOpen: () => state.open };
}
