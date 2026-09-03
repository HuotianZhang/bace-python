// The run monitor's model, against the recorded tree — `docs/ui-plan.md` M4.
//
// `stream_tree_sim.jsonl` is two temperatures by two levels by a three-point
// scan of two loops, recorded off `--sim --fast` with the operator's pauses
// answered by the recorder. It is the only recording with a `NeedsOperator`,
// an `OperatorResumed`, a `Progress` at three scales and a `TemperatureRead`
// typed by a person, and everything the monitor says is asserted against it
// here rather than read off a screen — the same split the rail has.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { createStore } from '../lib/store.js';
import {
  monitorModel, loopCounters, shotCounter, phaseIndicator, pausePrompt, stopModel, describeSegment,
} from '../lib/monitor.js';

const here = dirname(fileURLToPath(import.meta.url));
const frames = (name) => readFileSync(join(here, '../fixtures', name), 'utf8')
  .split('\n').filter(Boolean).map((line) => JSON.parse(line));

/** Fold a recording up to (and including) the first frame `until` accepts. */
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
const runOf = (store) => store.getState().runs[store.getState().order[0]];

// -- the three counters at three scales -----------------------------------

test('a node path reads as the operator reads it', () => {
  assert.deepEqual(describeSegment('T=250K'), { kind: 'temperature', label: 'T 250 K' });
  assert.deepEqual(describeSegment('led=1.020V'), { kind: 'illumination', label: 'LED 1.020 V' });
  assert.deepEqual(describeSegment('rep=2'), { kind: 'repeat', label: 'repeat 2' });
  assert.deepEqual(describeSegment('bace'), { kind: 'module', label: 'bace' });
});

test('the counters are three events at three scales, never one number', () => {
  // `ui-rules` §5: "step 412 of 8400" is useless; the operator wants which
  // temperature, which intensity, how far into the scan. Each comes from its
  // own `Progress`, keyed by the loop's `node_path`.
  const store = foldUntil(TREE, (f) => f.type === 'StepDone' && f.node_path === 'T=250K/led=1.020V/bace');
  const model = monitorModel(store.getState());
  assert.deepEqual(model.loops.map((l) => l.text), ['T 250 K · 1 of 2', 'LED 1.020 V · 2 of 2']);
  assert.equal(model.shots.kept, 1);
  assert.equal(model.shots.requested, 6, 'this node\'s own six shots, not the tree\'s twenty-four');
  assert.match(model.shots.text, /^shot 1 of 6 · loop 1 · point 1 of 3$/);
});

test('an open loop counts its current child; a closed one counts what it finished', () => {
  const store = foldUntil(TREE, (f) => f.type === 'NodeDone' && f.node_path === 'T=250K');
  const run = runOf(store);
  // The executor's exit `Progress` for T=250K says done = 1 of 2.
  const closed = loopCounters(run, 'T=250K');
  assert.equal(closed[0].open, false);
  assert.equal(closed[0].current, 1);
});

test('the shot counter is the leaf\'s, and a loop node has none', () => {
  const store = foldUntil(TREE, (f) => f.type === 'NeedsOperator');
  const run = runOf(store);
  assert.equal(shotCounter(run, run.nodes['T=250K']), null, 'a settling temperature is not a step that runs');
});

// -- the segment indicator ------------------------------------------------

test('StepPhase says where inside the shot the run is, and is gone when the shot ends', () => {
  const during = foldUntil(TREE, (f) => f.type === 'StepPhase' && f.data.phase === 'acquire light');
  const phase = phaseIndicator(runOf(during));
  assert.equal(phase.text, '3 · acquire light');
  assert.equal(phase.k, 3);
  assert.equal(phase.of, 7);
  assert.equal(phase.segments.length, 7);

  // A shot in flight is between StepStarted and StepDone; the next shot's
  // StepStarted clears the last phase of the previous one.
  const next = foldUntil(TREE, (f) => f.type === 'StepStarted' && f.data.index === 1);
  assert.equal(phaseIndicator(runOf(next)), null);
});

test('six segments when dark_reference = same, seven otherwise', () => {
  const run = { phase: { phase: 'levels', k: 1, of: 6 } };
  assert.equal(phaseIndicator(run).segments.length, 6);
  assert.ok(!phaseIndicator(run).segments.includes('dark levels'));
});

// -- the pause ------------------------------------------------------------

test('a NeedsOperator opens the prompt with the target, and the run reads paused', () => {
  const store = foldUntil(TREE, (f) => f.type === 'RunStateChanged' && f.data.state === 'paused');
  const model = monitorModel(store.getState());
  assert.equal(model.state, 'paused');
  assert.equal(model.waiting, 'T=250K');
  assert.equal(model.eta, null, 'a settle has no ETA that runs');
  assert.equal(model.phase, null);
  assert.equal(model.prompt.setpoint_k, 250);
  assert.equal(model.prompt.node_path, 'T=250K');
  assert.match(model.prompt.lines[0], /^set the cryostat to 250\.0 K ± 0\.50 K/);
  assert.equal(model.prompt.lines[1], 'temperature 1 of 2');
  assert.equal(model.prompt.reading, null, 'no controller on --sim: nothing is polled while the person decides');
  assert.match(model.prompt.fallback, /unconfirmed/);
});

test('the operator\'s answer closes the prompt, and the typed reading follows it', () => {
  const store = foldUntil(TREE, (f) => f.type === 'TemperatureRead');
  const state = store.getState();
  const model = monitorModel(state);
  assert.equal(model.prompt, null);
  assert.equal(model.state, 'running');
  assert.equal(model.waiting, null);
  assert.equal(state.temperature.kelvin, 250.1);
  assert.equal(state.temperature.source, 'operator');
  assert.equal(runOf(store).resumes.length, 1);
  assert.match(runOf(store).resumes[0].note, /by hand/);
});

test('a reading polled during the pause is shown beside the prompt', () => {
  // With a controller attached the cryostat is polled while the person
  // decides, and that reading is for them (contract §7). Simulated here with
  // a `TemperatureRead` after the pause opened.
  const store = foldUntil(TREE, (f) => f.type === 'RunStateChanged' && f.data.state === 'paused');
  const run = runOf(store);
  const before = pausePrompt(run, store.getState());
  assert.equal(before.reading, null);
  store.applyFrame({ seq: null, ts: run.needsOperator.ts + 5, run_id: run.run_id, node_path: 'T=250K',
    type: 'TemperatureRead', data: { kelvin: 251.3, setpoint_k: 250, in_band: false, source: 'simulated' } });
  const after = pausePrompt(run, store.getState());
  assert.equal(after.reading.kelvin, 251.3);
  assert.equal(after.reading.in_band, false);
  assert.match(after.fallback, /takes the last reading, 251\.3 K/);
});

test('a timeout or a refusal says why it is asking', () => {
  const run = {
    needsOperator: { what: 'temperature timeout', node_path: 'T=220K', ts: 10,
      detail: { setpoint_k: 220, tolerance_k: 0.2, hold_s: 60, timeout_s: 1800, index: 8, count: 9,
        kelvin: 223.4, reason: 'silent', elapsed_s: 1800 } },
  };
  const prompt = pausePrompt(run, { temperature: null });
  assert.ok(prompt.lines.some((l) => /temperature timeout · silent/.test(l)));
  assert.ok(prompt.lines.some((l) => /it got to 223\.4 K/.test(l)));
});

// -- stop, abort, cancel ----------------------------------------------------

test('the verbs follow the state: cancel while queued, stop and abort while it runs, nothing once it ends', () => {
  const queued = foldUntil(TREE, (f) => f.type === 'RunStateChanged' && f.data.state === 'queued');
  assert.deepEqual(pick(stopModel(runOf(queued))), { canCancel: true, canStop: false, canAbort: false });

  const running = foldUntil(TREE, (f) => f.type === 'StepDone');
  assert.deepEqual(pick(stopModel(runOf(running))), { canCancel: false, canStop: true, canAbort: true });

  const paused = foldUntil(TREE, (f) => f.type === 'RunStateChanged' && f.data.state === 'paused');
  assert.deepEqual(pick(stopModel(runOf(paused))), { canCancel: false, canStop: true, canAbort: true },
    'stop and abort still work while paused (contract §2)');

  const stopping = foldUntil(STOPPED, (f) => f.type === 'RunStateChanged' && f.data.state === 'stopping');
  const s = stopModel(runOf(stopping));
  assert.deepEqual(pick(s), { canCancel: false, canStop: false, canAbort: false });
  assert.match(s.why, /after_shot requested/);

  const done = foldUntil(TREE, (f) => f.type === 'RunStateChanged' && f.data.state === 'done');
  assert.deepEqual(pick(stopModel(runOf(done))), { canCancel: false, canStop: false, canAbort: false });
});

function pick(s) {
  return { canCancel: s.canCancel, canStop: s.canStop, canAbort: s.canAbort };
}

test('the monitor stays through stopping and leaves at parked', () => {
  // The run being parked still owns the worker (`store.js`): a monitor that
  // vanished at `stopped` would leave the rail saying busy with nothing to
  // explain why.
  const stopped = foldUntil(STOPPED, (f) => f.type === 'RunStateChanged' && f.data.state === 'stopped');
  const model = monitorModel(stopped.getState());
  assert.ok(model, 'still shown');
  assert.equal(model.state, 'stopped');
  assert.equal(model.kept, 39);
  assert.equal(model.requested, 60);

  const parked = foldUntil(STOPPED, (f) => f.type === 'RunStateChanged' && f.data.state === 'parked');
  assert.equal(monitorModel(parked.getState()), null);
});

test('stopping is said the moment it is accepted, and the shot in flight still counts', () => {
  const store = foldUntil(STOPPED, (f) => f.type === 'RunAborted');
  const model = monitorModel(store.getState());
  assert.equal(model.state, 'stopping');
  assert.match(model.reason, /after_shot/);
  assert.equal(model.shots.kept, 39, 'the shot in flight completed and was kept');
});

// -- the ETA ---------------------------------------------------------------

test('the ETA is the executor\'s re-derived one from the outermost loop', () => {
  const store = foldUntil(TREE, (f) => f.type === 'Progress' && f.data.node_path === 'T=280K');
  const model = monitorModel(store.getState());
  assert.equal(model.eta.from, 'executor');
  assert.ok(model.eta.seconds > 0 && model.eta.seconds < 10);
  assert.ok(model.eta.finish_at > 1.7e9);
});

test('a manual run\'s ETA is the module\'s own', () => {
  const store = foldUntil(STOPPED, (f) => f.type === 'Progress' && f.data.done === 10);
  const model = monitorModel(store.getState());
  assert.equal(model.loops.length, 0);
  assert.equal(model.eta.from, 'module');
});

// -- the whole tree, start to finish ----------------------------------------

test('a 2 T x 2 level tree stays readable start to finish', () => {
  // The M4 proof, in its model half: at every frame of the recording the
  // monitor says something consistent — a state, the loops it is in, and a
  // shot counter that never exceeds the node's own request.
  const store = createStore({ schedule: () => {} });
  const seen = new Set();
  let paused = 0;
  for (const frame of frames(TREE)) {
    store.applyFrame(frame);
    const model = monitorModel(store.getState());
    if (!model) continue;
    seen.add(model.loops.map((l) => l.text).join(' / '));
    if (model.shots) assert.ok(model.shots.kept <= model.shots.requested, `${model.shots.text}`);
    for (const loop of model.loops) assert.ok(loop.current === null || loop.current <= loop.total, loop.text);
    if (model.state === 'paused') { paused += 1; assert.ok(model.prompt || model.waiting, 'a pause says what it waits for'); }
  }
  assert.equal(paused > 0, true);
  assert.ok(seen.has('T 250 K · 1 of 2 / LED 1.010 V · 1 of 2'));
  assert.ok(seen.has('T 250 K · 1 of 2 / LED 1.020 V · 2 of 2'));
  assert.ok(seen.has('T 280 K · 2 of 2 / LED 1.010 V · 1 of 2'));
  assert.ok(seen.has('T 280 K · 2 of 2 / LED 1.020 V · 2 of 2'));
  assert.equal(monitorModel(store.getState()), null, 'parked: gone');
});
