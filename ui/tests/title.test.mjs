// The window title — `lib/title.js`.
//
// The one channel this console has to an operator who is not looking at it,
// which on a measurement built out of half-hour waits is most of the time. The
// recordings are the same ones the monitor is held down against, because the
// title is a second rendering of the monitor's model and must never be able to
// disagree with it: `stream_tree_sim.jsonl` is the only recording with a
// `NeedsOperator` in it, and `stream_stopped_sim.jsonl` ends in a run that was
// stopped at 39 of 60 shots.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { createStore } from '../lib/store.js';
import { titleModel, liveRunId, renderTitle, BASE } from '../lib/title.js';

const here = dirname(fileURLToPath(import.meta.url));
const frames = (name) => readFileSync(join(here, '../fixtures', name), 'utf8')
  .split('\n').filter(Boolean).map((line) => JSON.parse(line));

function foldUntil(name, until) {
  const store = createStore({ schedule: () => {} });
  for (const frame of frames(name)) {
    store.applyFrame(frame);
    if (until && until(frame)) break;
  }
  return store;
}

const TREE = 'stream_tree_sim.jsonl';
const STOPPED = 'stream_stopped_sim.jsonl';
const BACE = 'stream_bace_sim.jsonl';

test('an idle console says only its own name', () => {
  const store = createStore({ schedule: () => {} });
  assert.deepEqual(titleModel(store.getState()), { level: 'idle', text: BASE });
});

test('a pause reaches the taskbar, with what it is asking for', () => {
  // The whole reason this file exists: the run has stopped and is waiting for
  // a person who, by construction, is somewhere else — a temperature step is
  // 14 minutes to 2 hours (`ui-rules` §5).
  const store = foldUntil(TREE, (f) => f.type === 'NeedsOperator');
  const model = titleModel(store.getState());
  assert.equal(model.level, 'paused');
  assert.equal(model.text, `⏸ needs you · set 250.0 K — ${BASE}`);
});

test('a running sweep carries the outermost loop and no faster counter', () => {
  // `ui-rules` §5's three scales are the monitor's, on the screen. A taskbar
  // entry that rewrote itself once a shot would be noise, so the title takes
  // the one counter whose iteration is measured in hours.
  const store = foldUntil(TREE, (f) => f.type === 'StepDone' && f.node_path === 'T=250K/led=1.020V/bace');
  const model = titleModel(store.getState());
  assert.equal(model.level, 'running');
  assert.match(model.text, /^⏵ .* · T 250 K · 1 of 2 — /);
  assert.doesNotMatch(model.text, /shot/, 'the shot counter belongs on the monitor, not in the taskbar');
});

test('a manual run with no loops is its own label', () => {
  const store = foldUntil(BACE, (f) => f.type === 'StepDone');
  const model = titleModel(store.getState());
  assert.equal(model.level, 'running');
  assert.equal(model.text, `⏵ bace — ${BASE}`);
});

test('an ending is announced only while nobody has seen it', () => {
  const store = foldUntil(STOPPED);
  const state = store.getState();
  const runId = state.order[state.order.length - 1];

  // Nothing holds the worker any more, so with nothing to announce the title
  // is back to the console's own name — the operator is looking at the screen,
  // which already says what happened.
  assert.deepEqual(titleModel(state), { level: 'idle', text: BASE });

  // And while they were away, it waits for them — with `kept of requested`,
  // because a truncated run is normal and that is how it is said (§9).
  const model = titleModel(state, { announce: runId });
  assert.equal(model.level, 'ended');
  assert.equal(model.text, `⏹ stopped · bace · 39/60 — ${BASE}`);
});

test('a run that finished cleanly needs no counts', () => {
  const store = foldUntil(BACE);
  const state = store.getState();
  const runId = state.order[state.order.length - 1];
  const model = titleModel(state, { announce: runId });
  assert.equal(model.text, `✓ done · bace — ${BASE}`);
});

test('a live run outranks an ending that was never seen', () => {
  // The latch is the shell's and is cleared on focus, so a stale one can
  // survive into the next run. What is happening now wins.
  const store = foldUntil(BACE, (f) => f.type === 'StepDone');
  const model = titleModel(store.getState(), { announce: 'some-older-run' });
  assert.equal(model.level, 'running');
});

test('an announce naming a run this console never saw says nothing at all', () => {
  const store = foldUntil(STOPPED);
  assert.deepEqual(titleModel(store.getState(), { announce: 'not-a-run' }),
    { level: 'idle', text: BASE });
});

test('the live run is read across draws, not off the frame that ends it', () => {
  // `parked` is a frame like any other and a ring gap can swallow it; the
  // shell notices an ending by "was live, is not", so `liveRunId` has to be
  // exactly the test the monitor draws itself on.
  const running = foldUntil(STOPPED, (f) => f.type === 'StepDone');
  assert.equal(typeof liveRunId(running.getState()), 'string');
  const ended = foldUntil(STOPPED);
  assert.equal(liveRunId(ended.getState()), null);
});

test('the title is written only when it moved', () => {
  // The store notifies once per animation frame for the length of a run.
  const store = foldUntil(BACE, (f) => f.type === 'StepDone');
  let writes = 0;
  const doc = { _t: BASE, get title() { return this._t; }, set title(v) { writes += 1; this._t = v; } };
  for (let i = 0; i < 50; i += 1) renderTitle(doc, store.getState(), {});
  assert.equal(writes, 1, 'fifty notifies, one write');
});
