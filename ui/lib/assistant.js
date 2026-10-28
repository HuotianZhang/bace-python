// The assistant panel: a conversation with a model that reads the bench.
//
// What it is for is the operator who was never trained on this console — why
// a parameter has its value, what the chain does, where to click. The service
// side is `bace/service/assistant.py`; this is the panel, opened from the
// bar's `ask` chip, beside every tab. In this version the model reads and
// never acts, so nothing here confirms anything.
//
// The key is the asker's own. It goes to the service on each question in a
// header and is kept in this browser only when they tick "remember"; the
// service never stores it. A console with no key, or a service without the
// SDK, works exactly as before — the panel says what is missing and nothing
// else changes.
//
// Pure parts first (`splitLines`, `applyFrame`, `blocks`, `inline`,
// `toolLabel`), tested in `tests/assistant.test.mjs`; the DOM after them.

import { h, fill } from './dom.js';

export const KEY_STORE = 'bace.assistant.key';

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

/** NDJSON: the whole lines in `buffer` parsed, and what is left of the last one. */
export function splitLines(buffer) {
  const lines = buffer.split('\n');
  const rest = lines.pop();
  const frames = [];
  for (const line of lines) {
    if (!line.trim()) continue;
    try { frames.push(JSON.parse(line)); } catch { /* a line the service did not write */ }
  }
  return { frames, rest };
}

/** The transcript after one frame. `state.items`' last entry is the answer being written. */
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
      if (frame.stop_reason === 'refusal') last.error = 'the model declined to answer this';
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
 * The markdown a model writes, as blocks: headings, paragraphs, lists, code
 * and tables. Enough for an answer, and built as DOM rather than HTML so a
 * model's text can never become markup.
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

/** One line's spans: `code` and **bold**, everything else as it stands. */
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
  return h('div.as-a',
    tools.length ? h('div.as-tools', tools) : null,
    blocks(item.text).map(blockEl),
    !item.done && !item.error && !item.text ? h('div.as-wait', { text: 'thinking …' }) : null,
    item.error ? h('div.as-err', { text: item.error }) : null,
    item.usage ? h('div.as-usage', {
      text: `${item.usage.input} in · ${item.usage.output} out${item.usage.cache_read ? ` · ${item.usage.cache_read} cached` : ''} tokens`,
    }) : null);
}

function readKey() {
  try { return localStorage.getItem(KEY_STORE) || ''; } catch { return ''; }
}

function writeKey(key) {
  try {
    if (key) localStorage.setItem(KEY_STORE, key);
    else localStorage.removeItem(KEY_STORE);
  } catch { /* storage blocked: the key lives for this page only */ }
}

/**
 * The panel. `onChange` is called when it opens or closes, so the bar's chip
 * can say so.
 */
export function createAssistantPanel({ api, onChange = () => {} }) {
  let state = { open: false, status: null, key: readKey(), conversation: null,
                items: [], busy: false, editingKey: false };
  let controller = null;

  const el = h('aside.assistant', { hidden: true, 'aria-label': 'assistant' });
  const headEl = h('div.as-head');
  const noteEl = h('div.as-note');
  const logEl = h('div.as-log');
  const input = h('textarea', { rows: 3, placeholder: 'Ask about the bench, the physics or the console. Enter sends, Shift+Enter is a new line.' });
  const sendBtn = h('button.as-send', { text: 'ask' });
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

  function needsKey() {
    return !state.key || state.editingKey;
  }

  function draw() {
    el.hidden = !state.open;
    const status = state.status;
    fill(headEl,
      h('span.as-title', 'assistant'),
      h('span.as-model', { text: status && status.model ? status.model : '' }),
      h('span.spacer'),
      state.key && !state.editingKey ? h('button.as-link', { text: 'key', title: 'change or forget the API key', onclick: () => set({ editingKey: true }) }) : null,
      state.items.length ? h('button.as-link', { text: 'new', title: 'start a new conversation', onclick: reset }) : null,
      h('button.as-link', { text: '✕', title: 'close', onclick: () => toggle(false) }));

    if (status && !status.available) {
      fill(noteEl, h('p', { text: `Not available on this service: ${status.reason}` }));
    } else if (needsKey()) {
      const keyInput = h('input', { type: 'password', placeholder: 'sk-ant-…', autocomplete: 'off', value: state.key });
      const remember = h('input', { type: 'checkbox', checked: Boolean(readKey()) });
      const use = () => {
        const key = keyInput.value.trim();
        writeKey(remember.checked ? key : '');
        set({ key, editingKey: false });
        input.focus();
      };
      keyInput.addEventListener('keydown', (event) => { if (event.key === 'Enter') use(); });
      fill(noteEl,
        h('p', 'The assistant runs on your own Anthropic API key. It goes to this service with each question and on to the Anthropic API; the service does not keep it, and this browser keeps it only if you tick the box.'),
        h('div.as-key', keyInput, h('button', { text: 'use', onclick: use })),
        h('label.as-remember', remember, ' remember on this computer'),
        state.key ? h('button.as-link', { text: 'forget the key', onclick: () => { writeKey(''); set({ key: '', editingKey: true }); } }) : null);
    } else {
      fill(noteEl);
    }

    const near = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
    fill(logEl, state.items.length
      ? state.items.map(itemEl)
      : h('div.as-empty',
        h('p', 'It reads the bench, the modules, the runs and the project documents, and it changes nothing: when something has to be done, it says where to click.'),
        EXAMPLES.map((q) => h('button.as-example', { text: q, onclick: () => send(q) }))));
    if (near) logEl.scrollTop = logEl.scrollHeight;

    const ready = status && status.available && !needsKey();
    input.disabled = !ready;
    sendBtn.disabled = !ready;
    sendBtn.textContent = state.busy ? 'stop' : 'ask';
  }

  async function send(text) {
    text = String(text || '').trim();
    if (!text || state.busy || needsKey()) return;
    input.value = '';
    controller = new AbortController();
    set({ busy: true, items: [...state.items, { role: 'user', text }, { role: 'assistant', text: '', tools: [] }] });
    try {
      await api.ask({
        text, conversation: state.conversation, key: state.key, signal: controller.signal,
        onFrame: (frame) => {
          state = applyFrame(state, frame);
          if (frame.type === 'error' && frame.status === 401) state = { ...state, editingKey: true };
          draw();
        },
      });
    } catch (error) {
      const message = error.name === 'AbortError' ? 'stopped' : (error.text || error.message);
      state = applyFrame(state, { type: 'error', message });
      if (error.status === 401) state = { ...state, editingKey: true };
    }
    // A stream that ended without saying so (the service went away) still ends the turn.
    if (state.busy) state = applyFrame(state, { type: 'error', message: 'the answer was cut off' });
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
        set({ status: { available: false, reason: error.text || error.message } });
      }
    }
    if (open) input.focus();
  }

  draw();
  return { el, toggle, isOpen: () => state.open };
}
