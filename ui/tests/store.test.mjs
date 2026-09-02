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
import { sigmaQ } from '../lib/format.js';

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

test('sigma_Q of zero is an absence, not a value', () => {
  // Most of the 9 x 5 archive carries 0.000000 because the loops were never
  // recorded; a zero-length error bar drawn as a bare dot is a lie.
  assert.equal(sigmaQ(0), null);
  assert.equal(sigmaQ(null), null);
  assert.ok(sigmaQ(4e-11).startsWith('4.00e-11'));
});
