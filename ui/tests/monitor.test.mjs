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
  scopedTo, queuedRuns,
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

// -- the shot in flight ----------------------------------------------------

test('the counter is the shot being acquired, not the last one that finished', () => {
  // Between `StepStarted` and `StepDone` the instrument is inside the next
  // shot: counted from `kept` alone the monitor opens every node with
  // "shot 0 of 6" and then trails the loop and point beside it by one.
  const started = foldUntil(TREE, (f) => f.type === 'StepStarted');
  const first = monitorModel(started.getState());
  assert.equal(first.shots.kept, 0, 'nothing has completed yet');
  assert.equal(first.shots.nth, 1, 'and the instrument is inside the first');
  assert.match(first.shots.text, /^shot 1 of 6 · loop 1 · point 1 of 3$/);

  // Its `StepDone` does not move the number on again.
  const done = foldUntil(TREE, (f) => f.type === 'StepDone');
  const after = monitorModel(done.getState());
  assert.equal(after.shots.kept, 1);
  assert.equal(after.shots.nth, 1);
});

test('a StepStarted replayed from behind lends the counter nothing', () => {
  // After a drop the ring's numbered frames arrive behind the live ones: a
  // start for shot 300 while the instrument is inside 560 describes a shot
  // that finished long ago, and its loop and point are the tail of the run.
  const store = createStore({ schedule: () => {} });
  const run = 'r1';
  store.applyFrame({ seq: 1, ts: 1, run_id: run, node_path: 'bace', type: 'RunQueued', data: { kind: 'manual', module: 'bace' } });
  store.applyFrame({ seq: 2, ts: 2, run_id: run, node_path: 'bace', type: 'NodeStarted', data: { node_path: 'bace', kind: 'bace', label: 'bace' } });
  store.applyFrame({ seq: 3, ts: 3, run_id: run, node_path: 'bace', type: 'RunStarted', data: { n_shots: 600 } });
  const record = store.getState().runs[run];
  const node = record.nodes.bace;
  node.kept = 560;
  node.values = [0, 10, 20];

  store.applyFrame({ seq: 4, ts: 4, run_id: run, node_path: 'bace', type: 'StepStarted', data: { index: 300, loop: 100, step: 1 } });
  const stale = shotCounter(record, node);
  assert.equal(stale.nth, 560, 'never behind what has completed');
  assert.equal(stale.text, 'shot 560 of 600', 'and it lends no loop or point');

  store.applyFrame({ seq: 5, ts: 5, run_id: run, node_path: 'bace', type: 'StepStarted', data: { index: 560, loop: 187, step: 2 } });
  assert.equal(shotCounter(record, node).text, 'shot 561 of 600 · loop 187 · point 2 of 3');
});

test('the shot in flight is this node\'s, not the one before it', () => {
  // The index restarts at zero on every module node, so the previous node's
  // last start is not this node's first.
  const store = createStore({ schedule: () => {} });
  const run = 'r1';
  store.applyFrame({ seq: 1, ts: 1, run_id: run, node_path: 'rep=1/bace', type: 'RunQueued', data: { kind: 'pipeline' } });
  store.applyFrame({ seq: 2, ts: 2, run_id: run, node_path: 'rep=1/bace', type: 'StepStarted', data: { index: 40, loop: 20, step: 1 } });
  store.applyFrame({ seq: 3, ts: 3, run_id: run, node_path: 'rep=2/bace', type: 'NodeStarted', data: { node_path: 'rep=2/bace', kind: 'bace', label: 'bace' } });
  store.applyFrame({ seq: 4, ts: 4, run_id: run, node_path: 'rep=2/bace', type: 'RunStarted', data: { n_shots: 60 } });
  const record = store.getState().runs[run];
  assert.equal(shotCounter(record, record.nodes['rep=2/bace']).text, 'shot 0 of 60',
    'the previous node\'s shot is not this node\'s');
});

// -- what the shell holds about a run --------------------------------------

test('an armed Abort belongs to the run it was armed for', () => {
  // `parked` is a frame like any other and a ring gap can swallow it, so a
  // flag that outlived its run would hand the next one an Abort that fires
  // on the first click and discards a shot with no confirmation.
  assert.equal(scopedTo('r1', 'r1'), 'r1');
  assert.equal(scopedTo('r2', 'r1'), null, 'armed for the run that has gone');
  assert.equal(scopedTo('r1', null), null);
  assert.equal(scopedTo(null, 'r1'), null);
  // The same for the sentence a stop came back with: "after_shot accepted"
  // over a fresh run would tell the operator their stop is in force.
  const status = { run_id: 'r1', level: 'ok', text: 'after_shot accepted' };
  assert.equal(scopedTo('r1', status), status);
  assert.equal(scopedTo('r2', status), null);
});

test('a run waiting for the worker is reachable, so it can be cancelled', () => {
  // The store keeps `activeRunId` for the run that holds the bench, so the
  // monitor's own record can never be a queued one: without a row of their
  // own, the only way to be rid of a queued run is to let it start and then
  // stop it, which on a rig is an hour of the sample's life.
  const s = createStore({ schedule: () => {} });
  s.applyFrame({ seq: 1, ts: 1, run_id: 'r1', node_path: '', type: 'RunQueued', data: { kind: 'manual', module: 'bace' } });
  s.applyFrame({ seq: 2, ts: 2, run_id: 'r1', node_path: '', type: 'RunStateChanged', data: { state: 'running' } });
  s.applyFrame({ seq: 3, ts: 3, run_id: 'r2', node_path: '', type: 'RunQueued', data: { kind: 'manual', module: 'jv' } });
  s.applyFrame({ seq: 4, ts: 4, run_id: 'r2', node_path: '', type: 'RunStateChanged', data: { state: 'queued' } });
  const state = s.getState();
  assert.equal(state.activeRunId, 'r1');
  assert.equal(monitorModel(state).run_id, 'r1', 'the monitor draws the run on the worker');
  assert.deepEqual(queuedRuns(state), [{ run_id: 'r2', position: 1, label: 'jv' }]);

  // A queue folded from a `/bench` snapshot can name a run this console never
  // saw queued; the id is the label rather than a blank row.
  assert.deepEqual(queuedRuns({ queue: ['20260903-009'], runs: {} }),
    [{ run_id: '20260903-009', position: 1, label: '20260903-009' }]);
  assert.deepEqual(queuedRuns({ queue: [], runs: {} }), []);
});

// -- which nodes have a counter, and what it counts ------------------------

test('a J-V counts the curve it is sweeping, not the ones that finished', () => {
  // There is no per-curve start: `JVStarted`, then a blocking sweep that on
  // the rig is minutes, then `JVCurveDone`. Counted from what has completed
  // the monitor reads `curve 0 of 2` for the whole of the first one.
  const store = createStore({ schedule: () => {} });
  const run = 'r1';
  store.applyFrame({ seq: 1, ts: 1, run_id: run, node_path: 'jv_bace', type: 'RunQueued', data: { kind: 'manual', module: 'jv_bace' } });
  store.applyFrame({ seq: 2, ts: 2, run_id: run, node_path: 'jv_bace', type: 'NodeStarted', data: { node_path: 'jv_bace', kind: 'jv_bace', label: 'jv_bace' } });
  store.applyFrame({ seq: 3, ts: 3, run_id: run, node_path: 'jv_bace', type: 'JVStarted', data: { n_curves: 2 } });
  const record = store.getState().runs[run];
  const node = record.nodes.jv_bace;
  assert.equal(shotCounter(record, node).text, 'curve 1 of 2', 'inside the first sweep');

  store.applyFrame({ seq: 4, ts: 4, run_id: run, node_path: 'jv_bace', type: 'JVCurveDone',
    data: { label: 'dark', dark: true, metrics: {}, n_points: 71 } });
  assert.equal(shotCounter(record, node).text, 'curve 2 of 2', 'and inside the second');

  store.applyFrame({ seq: 5, ts: 5, run_id: run, node_path: 'jv_bace', type: 'JVCurveDone',
    data: { label: 'as found 1.02 V', dark: false, metrics: {}, n_points: 71 } });
  assert.equal(shotCounter(record, node).text, 'curve 2 of 2', 'never past what was asked for');

  store.applyFrame({ seq: 6, ts: 6, run_id: run, node_path: 'jv_bace', type: 'NodeDone',
    data: { node_path: 'jv_bace', outcome: 'ok', detail: { kept: 2, requested: 2 } } });
  assert.equal(shotCounter(record, node).text, 'curve 2 of 2', 'and the finished node counts what it kept');
});

test('a module that acquires nothing has no counter at all', () => {
  // Of the nine modules only bace, jv and jv_bace produce shots or curves.
  // Counted as a shot producer, a long `wait` read `shot 0` for its whole
  // execution — the step that "runs" §5 says a settle must never look like.
  const record = { step: null, nodes: {}, progressByNode: {} };
  for (const kind of ['light', 'power', 'temperature', 'park', 'wait', 'note', 'repeat', 'illumination']) {
    assert.equal(shotCounter(record, { node_path: kind, kind, shots: [], curves: [] }), null, kind);
  }
  assert.ok(shotCounter(record, { node_path: 'bace', kind: 'bace', shots: [], curves: [], kept: 0, requested: 6 }));
  // A node whose `NodeStarted` left the ring is read from what it produced.
  assert.equal(shotCounter(record, { node_path: 'x', kind: null, shots: [{ q: 1 }], curves: [], kept: 1, requested: 6 }).text,
    'shot 1 of 6');
  assert.equal(shotCounter(record, { node_path: 'x', kind: null, shots: [], curves: [], kept: 0 }), null,
    'and one that produced nothing says nothing');
});

test('a run with no measured ETA still shows the one the cost model predicted', () => {
  // A J-V's `Progress` carries `eta_s: null` on purpose, and the executor's
  // ETA belongs to a loop a manual run does not have. The prediction made at
  // submit is the only one a slow sweep ever has, and it says it is one.
  const state = {
    activeRunId: 'r1', lastFrame: { ts: 1000 },
    runs: { r1: { run_id: 'r1', kind: 'manual', module: 'jv_bace', state: 'running', node_path: 'jv_bace',
      nodes: { jv_bace: { node_path: 'jv_bace', kind: 'jv_bace', shots: [], curves: [], loops: [], kept: 0, requested: 2 } },
      progressByNode: {}, progress: { done: 0, total: 2, eta_s: null }, eta: null, finish_at: 1180,
      step: null, phase: null, needsOperator: null, resumes: [], parked_at: null, reason: '' } },
    queue: [], temperature: null,
  };
  const model = monitorModel(state);
  assert.equal(model.eta.from, 'predicted');
  assert.equal(model.eta.seconds, 180, 'counting down from the newest frame\'s clock');
  assert.equal(model.eta.finish_at, 1180);

  // A measured one wins the moment there is one.
  state.runs.r1.progress = { done: 1, total: 2, eta_s: 60, ts: 1000 };
  assert.equal(monitorModel(state).eta.from, 'module');
});

test('a hydrated ETA counts down too, rather than repeating the second it was measured at', () => {
  // `record.eta` arrives from `/bench` or `GET /runs/{id}` when the ring lost
  // the loop `Progress` that would have carried it. It has the absolute
  // `finish_at` beside the seconds it had when it was measured; repeating
  // those freezes the countdown and keeps it positive past the finish.
  const base = (lastTs) => ({
    activeRunId: 'r1', lastFrame: { ts: lastTs }, queue: [], temperature: null,
    runs: { r1: { run_id: 'r1', kind: 'pipeline', name: 'tree', state: 'running', node_path: 'T=250K/bace',
      nodes: { 'T=250K/bace': { node_path: 'T=250K/bace', kind: 'bace', shots: [], curves: [], loops: [], kept: 0, requested: 6 } },
      progressByNode: {}, progress: null, eta: { eta_s: 600, finish_at: 2000, at: 1400, node_path: 'T=250K' },
      finish_at: null, step: null, phase: null, needsOperator: null, resumes: [], parked_at: null, reason: '' } },
  });
  assert.equal(monitorModel(base(1400)).eta.seconds, 600, 'as measured');
  assert.equal(monitorModel(base(1700)).eta.seconds, 300, 'five minutes later');
  assert.equal(monitorModel(base(2100)).eta.seconds, 0, 'and never negative past the finish');
  assert.equal(monitorModel(base(1700)).eta.finish_at, 2000);
});
