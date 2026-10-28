// The assistant panel — `lib/assistant.js`.
//
// What is asserted is what turns the service's frames into a transcript and a
// model's markdown into blocks: the NDJSON split across chunk boundaries, each
// frame's effect on the answer being written, and the block parser, which has
// to build tables (the model is told to use them) and must never let a line of
// text become markup.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { splitLines, applyFrame, blocks, inline, toolLabel, missing, keySlot, isLocal, PRESETS } from '../lib/assistant.js';

test('a frame split across two chunks is parsed once, whole', () => {
  const a = splitLines('{"type":"text","text":"hel');
  assert.deepEqual(a.frames, []);
  const b = splitLines(a.rest + 'lo"}\n{"type":"done"}\n');
  assert.deepEqual(b.frames, [{ type: 'text', text: 'hello' }, { type: 'done' }]);
  assert.equal(b.rest, '');
});

test('the frames write the answer, and the tools say what was read', () => {
  let state = { busy: true, items: [{ role: 'user', text: 'q' }, { role: 'assistant', text: '', tools: [] }] };
  for (const frame of [
    { type: 'conversation', id: 'c1' },
    { type: 'tool', name: 'get_module', input: { name: 'bace' } },
    { type: 'tool_result', name: 'get_module', ok: true },
    { type: 'text', text: 'n_averages is ' },
    { type: 'text', text: '200.' },
    { type: 'done', stop_reason: 'end_turn', usage: { input: 1, output: 2, cache_read: 0 } },
  ]) state = applyFrame(state, frame);
  const answer = state.items[1];
  assert.equal(state.conversation, 'c1');
  assert.equal(state.busy, false);
  assert.equal(answer.text, 'n_averages is 200.');
  assert.deepEqual(answer.tools, [{ label: 'read module bace', ok: true, note: null }]);
  assert.equal(answer.done, true);
});

test('an error ends the turn and is said', () => {
  const state = applyFrame({ busy: true, items: [{ role: 'assistant', text: '' }] },
    { type: 'error', message: 'the API key was refused' });
  assert.equal(state.busy, false);
  assert.equal(state.items[0].error, 'the API key was refused');
});

test('a tool the panel does not know is named as it is', () => {
  assert.equal(toolLabel('read_doc', { name: 'principle' }), 'read principle');
  assert.equal(toolLabel('something_new', {}), 'something_new');
});

test('markdown becomes blocks: heading, list, table, code, paragraph', () => {
  const out = blocks([
    '## Why 200',
    '',
    'Two reasons:',
    '- scatter **triples** at 50',
    '- the panel used 200',
    '',
    '| averages | sd (nC) |',
    '|---|---|',
    '| 200 | 0.009 |',
    '| 50 | 0.029 |',
    '',
    '```',
    'n_averages = 200',
    '```',
  ].join('\n'));
  assert.deepEqual(out.map((b) => b.type), ['h', 'p', 'ul', 'table', 'code']);
  assert.deepEqual(out[2].items, ['scatter **triples** at 50', 'the panel used 200']);
  assert.deepEqual(out[3].header, ['averages', 'sd (nC)']);
  assert.deepEqual(out[3].rows, [['200', '0.009'], ['50', '0.029']]);
  assert.equal(out[4].text, 'n_averages = 200');
});

test('a line that looks like a table row but has no separator is a paragraph, and the parser ends', () => {
  assert.deepEqual(blocks('| just pipes |').map((b) => b.type), ['p']);
});

test('inline spans: code and bold, the rest as text', () => {
  assert.deepEqual(inline('set `n_averages` to **200** <b>'), [
    { t: 'text', s: 'set ' }, { t: 'code', s: 'n_averages' }, { t: 'text', s: ' to ' },
    { t: 'b', s: '200' }, { t: 'text', s: ' <b>' },
  ]);
});

test('what is still needed: a key for Claude; address, model and key for the others', () => {
  assert.equal(missing({ provider: 'anthropic' }, ''), 'an Anthropic API key');
  assert.equal(missing({ provider: 'anthropic' }, 'k'), null);
  assert.equal(missing({ provider: 'openai', base_url: '', model: 'm' }, 'k'), 'the API address');
  assert.equal(missing({ provider: 'openai', base_url: 'https://api.deepseek.com', model: '' }, 'k'), 'the model name');
  assert.equal(missing({ provider: 'openai', base_url: 'https://api.deepseek.com', model: 'm' }, ''), 'an API key');
  // A model on this PC takes no key.
  assert.equal(missing({ provider: 'openai', base_url: 'http://localhost:11434/v1', model: 'qwen' }, ''), null);
});

test('one stored key per provider address, and the presets are addresses', () => {
  assert.equal(keySlot({ provider: 'anthropic', base_url: 'x' }), 'anthropic');
  assert.equal(keySlot({ provider: 'openai', base_url: 'https://api.deepseek.com' }), 'https://api.deepseek.com');
  assert.ok(isLocal('http://127.0.0.1:11434/v1') && !isLocal('https://api.openai.com/v1') && !isLocal('nonsense'));
  for (const p of PRESETS) assert.match(p.base_url, /^https:\/\/|^http:\/\/localhost/);
});
