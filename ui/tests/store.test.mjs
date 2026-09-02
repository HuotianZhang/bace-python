// The fold, against the frames the service actually wrote. Nothing here is a
// hand-made fixture: three rig journals of 2026-09-02 and two recordings off a
// `--sim` service, which is `docs/ui-plan.md` decision 4 in test form.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { createStore, currentRun } from '../lib/store.js';
import { parseJsonl, replayInto } from '../lib/replay.js';
import { sigmaQ, density } from '../lib/format.js';

const here = dirname(fileURLToPath(import.meta.url));
const journal = (name) => parseJsonl(readFileSync(
  join(here, '../../acceptance/20260902_service-vs-labview/journals', name), 'utf8'));
const fixture = (name) => readFileSync(join(here, '../fixtures', name), 'utf8');

/** The store notifies on a microtask; the tests read `getState()` directly. */
const store = () => createStore({ schedule: () => {} });

test('a rig journal replays into the store, run for run', () => {
  const s = store();
  replayInto(s, journal('20260902_153357.jsonl'));
  const state = s.getState();

  assert.equal(state.order.length, 3);
  const [dark, bace] = [state.runs['20260902_153357-001'], state.runs['20260902_153357-003']];

  assert.equal(dark.module, 'jv_dark');
  assert.equal(dark.curves.length, 1);
  assert.equal(dark.kept, 1);
  assert.equal(dark.requested, 1);

  assert.equal(bace.module, 'bace');
  assert.equal(bace.shots.length, 2, 'two shots, from the journalled StepDone scalars');
  assert.equal(bace.kept, 2);
  assert.ok(bace.axis && bace.axis.name === 'vpre');
  assert.equal(bace.values.length, 1, 'a zero-width axis: one value, repeated');
});

test('parked is a transition, not the outcome', () => {
  // Every terminal state is reached through `parked`, so the journal's last
  // line for a finished run is `parked` — and a store that took it at face
  // value would report every run, failed ones included, as parked.
  const s = store();
  replayInto(s, journal('20260902_153357.jsonl'));
  const state = s.getState();
  for (const id of state.order) {
    assert.equal(state.runs[id].state, 'done', `${id} should read done, not parked`);
    assert.ok(state.runs[id].parked_at, `${id} should still record that it parked`);
  }
  assert.equal(state.activeRunId, null, 'nothing is running once the last run parked');
});

test('the two real RunFailed survive the fold', () => {
  const s = store();
  replayInto(s, journal('20260902_125751.jsonl'));
  const state = s.getState();
  const failed = state.order.map((id) => state.runs[id]).filter((r) => r.error);
  assert.equal(failed.length, 2);
  for (const run of failed) {
    assert.equal(run.state, 'failed');
    assert.match(run.error.text, /^SyncError: no sync on CHAN3/);
    assert.equal(run.error.where, 'bace');
  }
  assert.ok(state.log.some((line) => line.level === 'crit'), 'a failure is in the session log');
});

test('the bench verdicts of a session land outside any run', () => {
  const s = store();
  replayInto(s, journal('20260902_144844.jsonl'));
  const state = s.getState();
  const codes = new Set(state.verdicts.map((v) => v.code));
  assert.ok(codes.size > 0, 'the chain verdicts are session-level, not run-level');
  for (const verdict of state.verdicts) assert.equal(verdict.node_path, '');
});

test('a verdict is replaced per (code, node_path), not appended', () => {
  const s = store();
  const frame = (level) => ({ seq: 1, ts: 1, run_id: null, node_path: '', type: 'Verdict',
                              data: { level, code: 'chain.led-polarity', text: level, node_path: '' } });
  s.applyFrame(frame('warn'));
  s.applyFrame(frame('ok'));
  const { verdicts } = s.getState();
  assert.equal(verdicts.length, 1, 'the Start re-read replaces the submit-time copy');
  assert.equal(verdicts[0].level, 'ok');
});

test('a recorded stream folds its traces, and StepPhase stays out of the shots', () => {
  const s = store();
  const frames = parseJsonl(fixture('stream_bace_sim.jsonl'));
  replayInto(s, frames);
  const run = currentRun(s.getState());

  const phases = frames.filter((f) => f.type === 'StepPhase');
  assert.ok(phases.length > 0);
  for (const frame of phases) assert.equal(frame.seq, null, 'StepPhase is live-only, seq null');
  assert.equal(run.shots.length, frames.filter((f) => f.type === 'StepDone').length);

  const shot = run.shots[0];
  assert.ok(shot.traces.light.y.length > 0);
  assert.ok(shot.traces.decimated['light.y'].stride >= 1);
  // `t = t0 + i*stride*dt`, and the **last** kept sample is the record\'s own
  // final one at `t0 + (n-1)*dt` rather than one stride after its predecessor.
  // So a 4000-point record at stride 5 arrives as 801 points, not 800: a chart
  // that plots the last one a stride early is wrong by 2 ns at the tail.
  const { n_full, stride } = shot.traces.decimated['light.y'];
  const strided = Math.floor((n_full - 1) / stride) + 1;
  assert.equal(shot.traces.light.y.length, strided + ((n_full - 1) % stride ? 1 : 0),
               'the wire decimates by stride, and keeps the record\'s last sample');
  assert.ok(shot.verdict, 'the service attaches a per-shot verdict to the wire data');
  assert.equal(run.state, 'done');
  assert.equal(run.phase, null, 'a finished run is not inside a shot segment');
});

test('a replayed StepDone keeps its scalars and does not blank the chart', () => {
  // The branch that only shows itself after a reconnect: the ring keeps the
  // traces of the last 200 shots only, so an older frame comes back with the
  // four traces null and `decimated[name].replay` true. It means *redraw the
  // loop curve, the trace is gone* — the scalars and the verdict are all there.
  const s = store();
  const live = parseJsonl(fixture('stream_bace_sim.jsonl')).find((f) => f.type === 'StepDone');
  const replayed = {
    ...live,
    seq: live.seq + 10000,
    data: { ...live.data, light: null, dark: null, photo: null, photo_averaged: null },
    decimated: Object.fromEntries(Object.keys(live.decimated).map((k) => [k, { omitted: true, replay: true }])),
  };
  s.applyFrame(replayed);
  const shot = currentRun(s.getState()).shots[0];
  assert.equal(shot.q, live.data.q);
  assert.deepEqual(shot.verdict, live.data.verdict);
  assert.equal(shot.traces, null);
  assert.equal(shot.tracesGone, true, 'the view must redraw from scalars, not blank the chart');
});

test('a pipeline keeps each node\'s shots and each scope\'s progress apart', () => {
  // One `run_id` over two `bace` nodes, each numbering its own shots from one:
  // `loop:index` alone is not an identity, and the second node would land on
  // the first. And the counters are three *events* — the executor emits
  // `Progress(node_path: "rep=1")` for the loop while the leaf emits its own
  // with `node_path: ""` — so one `progress` field would leave the loop
  // counter holding the shot counter's numbers.
  const s = store();
  const frames = parseJsonl(fixture('stream_pipeline_sim.jsonl'));
  replayInto(s, frames);
  const run = currentRun(s.getState());

  const steps = frames.filter((f) => f.type === 'StepDone');
  assert.equal(steps.length, 4);
  assert.equal(new Set(steps.map((f) => `${f.data.loop}:${f.data.index}`)).size, 2,
               'the fixture is only a test of identity if the two nodes reuse their numbers');
  assert.equal(run.shots.length, 4, 'four shots ran, and four are folded');

  for (const path of ['rep=1/bace', 'rep=2/bace']) {
    assert.equal(run.nodes[path].shots.length, 2, `${path} kept its own two`);
    assert.ok(run.nodes[path].axis, 'and its own axis');
    assert.ok(run.nodes[path].finished, 'and its own RunFinished');
  }
  assert.deepEqual(run.shots.map((shot) => shot.node_path),
                   ['rep=1/bace', 'rep=1/bace', 'rep=2/bace', 'rep=2/bace']);

  assert.deepEqual(Object.keys(run.progressByNode).sort(), ['', 'rep=1', 'rep=2']);
  assert.deepEqual(run.progressByNode['rep=2'].done, 2, 'the loop counter is the loop\'s');
  assert.equal(run.progress.node_path, '', 'and `progress` stays the run\'s own');

  assert.equal(run.kept, 4, 'a pipeline\'s counts are the sum over its module nodes');
  assert.equal(run.requested, 4);
  assert.equal(run.state, 'done');
});

test('the bench state follows the run, between one snapshot and the next', () => {
  // `GET /bench` is fetched at boot and again when a run parks; between those
  // two the only thing that knows the bench is busy is the stream. A rail that
  // read `idle` for the length of a scan would be describing the last fetch.
  const s = store();
  s.applyBench(JSON.parse(fixture('hello_sim.json')).data.bench);
  assert.equal(s.getState().benchState, 'idle');

  const seen = [];
  for (const frame of parseJsonl(fixture('stream_bace_sim.jsonl'))) {
    s.applyFrame(frame);
    const now = s.getState().benchState;
    if (seen[seen.length - 1] !== now) seen.push(now);
  }
  assert.deepEqual(seen, ['idle', 'preflight', 'running', 'stopping', 'idle'],
                   'preflight, running, then stopping while the worker parks');
});

test('a queued run is in the queue, and leaves it when the worker takes it', () => {
  // No recording has two overlapping submissions — under `--fast` the first
  // run is over before a second could be posted — so these two frames are the
  // recorded shapes with a second run_id. The rule is the store's, not the
  // wire's: `POST /runs` while a run is active queues (contract §2).
  const s = store();
  const queued = (runId, state) => ({
    seq: 1, ts: 1, run_id: runId, node_path: '', type: 'RunStateChanged',
    data: { state, reason: state === 'queued' ? 'submitted' : 'started' }, decimated: {},
  });
  s.applyFrame(queued('run-1', 'running'));
  s.applyFrame(queued('run-2', 'queued'));
  assert.deepEqual(s.getState().queue, ['run-2']);
  assert.equal(s.getState().activeRunId, 'run-1');

  s.applyFrame(queued('run-1', 'done'));
  assert.equal(s.getState().activeRunId, 'run-1',
               'the run that is being parked still owns the worker');
  assert.equal(s.getState().benchState, 'stopping');

  s.applyFrame({ ...queued('run-1', 'parked'), data: { state: 'parked', reason: 'done' } });
  assert.equal(s.getState().activeRunId, null);
  assert.equal(s.getState().benchState, 'idle');

  s.applyFrame(queued('run-2', 'preflight'));
  assert.deepEqual(s.getState().queue, [], 'and it leaves the queue when picked up');
  assert.equal(s.getState().benchState, 'preflight');
});

test('the Hello frame carries the whole bench', () => {
  const s = store();
  const hello = JSON.parse(fixture('hello_sim.json'));
  assert.equal(hello.seq, null, 'Hello is exempt from the dedupe it configures');
  s.applyHello(hello);
  const state = s.getState();
  assert.ok(state.bench.instruments.relay.position);
  assert.ok(state.bench.chain.items.length >= 4);
  assert.equal(state.connection.session, hello.data.session.id);
  assert.equal(state.session.mode, 'sim');
  assert.ok(state.bench.instruments.voc.value > 0, 'the recording was taken after a jv_bace');
});

test('a current density is A/cm² on the wire, and mA/cm² only after converting', () => {
  // `JVCurveDone.density` is `current / pixel_area_cm2`, so 0.02 there is
  // 20.0 mA/cm². Stamping the label on the number as it came would understate
  // every measured density by a factor of a thousand.
  assert.equal(density(0.02), '20.0 mA/cm²');
  assert.equal(density(-1.9e-9), '-1.90 nA/cm²');
  assert.equal(density(null), '—', 'pixel_area_cm2 = 0 has no density at all');
});

test('sigma_Q of zero is an absence, not a value', () => {
  // Most of the 9 x 5 archive carries 0.000000 because the loops were never
  // recorded; a zero-length error bar drawn as a bare dot is a lie.
  assert.equal(sigmaQ(0), null);
  assert.equal(sigmaQ(null), null);
  assert.ok(sigmaQ(4e-11).startsWith('4.00e-11'));
});
