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

  // `jv_dark` and not `jv`: this is the recorded journal of the rig day of
  // 2026-09-02, and that is the module the run was. The split into `jv` +
  // `light` came the day after; a recording says what happened.
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

test('a J-V counts its curves as they complete, not at the end', () => {
  // On the rig a sweep takes minutes. `JVStarted` says how many curves are
  // coming and every `JVCurveDone` is one of them, so the rail can say
  // "1/2" while it happens instead of nothing at all until `NodeDone`.
  const s = store();
  const counts = [];
  for (const frame of parseJsonl(fixture('stream_jv_sim.jsonl'))) {
    s.applyFrame(frame);
    const run = currentRun(s.getState());
    const now = `${run.kept}/${run.requested}`;
    if (counts[counts.length - 1] !== now) counts.push(now);
    if (frame.type === 'JVFinished') break;          // before NodeDone lands
  }
  assert.deepEqual(counts, ['null/null', 'null/2', '1/2', '2/2'],
                   'requested at JVStarted, kept as each curve arrives');
});

test('a journalled shot has no traces to draw, and says so', () => {
  // The journal keeps "enough to render the session log and the history
  // queries, never the traces": a journalled StepDone carries the scalars and
  // `decimated[…].omitted`. A `traces` object of four nulls would count as a
  // shot with traces on the offline page and be a null dereference in a chart.
  const s = store();
  replayInto(s, journal('20260902_153357.jsonl'));
  const bace = s.getState().runs['20260902_153357-003'];
  assert.equal(bace.shots.length, 2);
  for (const shot of bace.shots) {
    assert.equal(shot.traces, null);
    assert.equal(shot.tracesGone, true);
    assert.equal(typeof shot.q, 'number', 'the scalars are all there');
  }
  assert.equal(bace.shots.filter((shot) => shot.traces).length, 0);
});

test('a resolved chain warning disappears with the read-back that resolved it', () => {
  // `chain_verdicts()` omits a check that now reads ok, so a warning the
  // operator has just fixed goes away by being absent from the next snapshot.
  const s = store();
  const warned = { session: { id: 'S1' }, state: 'idle', queue: [], run: null, instruments: {},
                   read_at: 100, verdicts: [{ level: 'warn', code: 'chain.led-polarity',
                                             text: 'reads NORM', node_path: '' }] };
  s.applyBench(warned);
  assert.equal(s.getState().verdicts.length, 1);

  // The operator clicks the fix; the read-back afterwards no longer lists it.
  s.applyBench({ ...warned, read_at: 200, verdicts: [] });
  assert.deepEqual(s.getState().verdicts, [], 'the warning goes with the check');

  // But something the stream said after that read-back was taken survives it.
  s.applyFrame({ seq: 5, ts: 250, run_id: null, node_path: '', type: 'Verdict',
                 data: { level: 'warn', code: 'power.console', text: ':8918 is silent', node_path: '' } });
  s.applyBench({ ...warned, read_at: 200, verdicts: [] });
  assert.deepEqual(s.getState().verdicts.map((v) => v.code), ['power.console']);
});

test('a stop accepted during preflight is not undone by the worker catching up', () => {
  // The session says `stopping` the moment a stop is accepted — for a long
  // shot the job's next event is seconds away — and the worker's own
  // start-of-run reports follow it. The service refuses to let them displace
  // it (`Session._apply_state`); a rail that went back to `running` would be
  // telling the operator their stop had lapsed.
  const s = store();
  const at = (state) => ({ seq: 1, ts: 1, run_id: 'r', node_path: '', type: 'RunStateChanged',
                           data: { state }, decimated: {} });
  s.applyFrame(at('queued'));
  s.applyFrame(at('preflight'));
  s.applyFrame(at('stopping'));
  s.applyFrame(at('running'));            // the worker, already on its way
  assert.equal(s.getState().runs.r.state, 'stopping');
  assert.equal(s.getState().benchState, 'stopping');
  assert.deepEqual(s.getState().runs.r.states.map((entry) => entry.state),
                   ['queued', 'preflight', 'stopping', 'running'],
                   'the transitions are still recorded, as the journal keeps them');

  s.applyFrame(at('stopped'));
  assert.equal(s.getState().runs.r.state, 'stopped');
});

test('a run whose beginning left the ring is filled in, and not overwritten', () => {
  // The ring is 5000 envelopes and a 100 x 51 scan is three times that in
  // `StepStarted`/`StepDone`/`Progress` alone, so a console opened late in one
  // replays a tail: no `RunQueued`, no `RunStarted`, no `AxisResolved`. The
  // record and the data endpoint supply the shape; the stream stays the newer
  // source for everything it has already said.
  const s = store();
  const frames = parseJsonl(fixture('stream_bace_sim.jsonl'));
  // What a truncated replay is: the shot frames still in the ring, and none of
  // the run's opening ones.
  const tail = frames.filter((f) => ['StepStarted', 'StepPhase', 'StepDone', 'Progress'].includes(f.type))
    .filter((f) => f.type === 'StepDone' || f.type === 'Progress').slice(-4);
  s.applyFrames(tail);
  const runId = tail[0].run_id;
  assert.equal(s.getState().runs[runId].axis, null, 'the tail alone has no axis');

  s.applyRunRecord({ run_id: runId, kind: 'manual', module: 'bace', state: 'running',
                     kept: 6, requested: 6, folders: ['runs/290K_20260902'] });
  s.applyRunData(runId, 'bace', { axis: { name: 'delay_ns', start: 0, stop: 200, step: 100 },
                                  values: [0, 100, 200], kept: 6, requested: 6, voc: null });
  const run = s.getState().runs[runId];
  assert.equal(run.module, 'bace');
  assert.equal(run.axis.name, 'delay_ns');
  assert.deepEqual(run.values, [0, 100, 200]);
  assert.equal(run.kept, 6, 'the count is the run\'s, not the tail we happen to hold');
  assert.equal(run.requested, 6);
  assert.equal(run.shots.length, 2, 'and the shots the ring did carry are still there');
  assert.deepEqual(run.shots.map((shot) => shot.index), [4, 5], 'the tail, not the whole run');

  // A shot arriving after hydration does not drop the count back to what we hold.
  const another = { ...frames.find((f) => f.type === 'StepDone'), seq: 99999 };
  s.applyFrame({ ...another, data: { ...another.data, index: 9, loop: 9 } });
  assert.equal(s.getState().runs[runId].kept, 6);
});

test('a pause nobody answered goes with the run that ended', () => {
  // Stopped, aborted or failed at a `NeedsOperator`, there is no
  // `OperatorResumed` — nobody answered it — and a finished run still showing
  // an operator prompt is a screen asking for something nothing waits for.
  const s = store();
  s.applyFrame({ seq: 1, ts: 1, run_id: 'r', node_path: '', type: 'NeedsOperator',
                 data: { what: 'temperature', node_path: 'T=250K', detail: { setpoint_k: 250 } } });
  assert.ok(s.getState().runs.r.needsOperator);
  s.applyFrame({ seq: 2, ts: 2, run_id: 'r', node_path: '', type: 'RunStateChanged',
                 data: { state: 'aborted' } });
  assert.equal(s.getState().runs.r.needsOperator, null);
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

test('a current density is mA/cm² on the wire, and stays mA/cm² on screen', () => {
  // `JVCurveDone.density` is `current * 1000 / pixel_area_cm2`
  // (`experiment.jv.current_density`), so the console converts nothing and
  // never picks a prefix: one J-V curve printed part in mA/cm² and part in
  // nA/cm² is a column that cannot be read down.
  assert.equal(density(20.0), '20.0 mA/cm²');
  assert.equal(density(-1.9e-3), '-0.00190 mA/cm²');
  assert.equal(density(0), '0 mA/cm²', 'a measured zero is a value, not an absence');
  assert.equal(density(null), '—', 'pixel_area_cm2 = 0 has no density at all');
});

test('the read-back\'s own verdicts are visible without a Verdict frame', () => {
  // The startup read-back's verdicts live on the snapshot and nowhere else: a
  // socket opened after them replays nothing, so a console that folded only
  // `Verdict` frames would show a clean bench that is not clean.
  const s = store();
  const bench = JSON.parse(fixture('hello_sim.json')).data.bench;
  assert.ok(bench.verdicts.length, 'the recording caught the chain warning');
  s.applyBench(bench);
  assert.deepEqual(s.getState().verdicts.map((v) => v.code), bench.verdicts.map((v) => v.code));

  // And the frame for the same check replaces that copy rather than doubling it.
  const one = bench.verdicts[0];
  s.applyFrame({ seq: 9, ts: 1, run_id: null, node_path: '', type: 'Verdict',
                 data: { ...one, level: 'ok', text: 're-read at Start' } });
  const verdicts = s.getState().verdicts;
  assert.equal(verdicts.filter((v) => v.code === one.code).length, 1);
  assert.equal(verdicts.find((v) => v.code === one.code).level, 'ok');
});

test('a snapshot fetched after a park does not undo what the stream has said', () => {
  // The HTTP response and the socket race: by the time the post-park `/bench`
  // arrives, the next queued run may already have reported `preflight`.
  const s = store();
  const change = (runId, state) => ({ seq: 1, ts: 1, run_id: runId, node_path: '',
                                      type: 'RunStateChanged', data: { state }, decimated: {} });
  s.applyFrame(change('run-1', 'running'));
  s.applyFrame(change('run-1', 'done'));
  s.applyFrame(change('run-1', 'parked'));
  s.applyFrame(change('run-2', 'preflight'));          // the worker moved on

  const stale = { session: { id: 'S1' }, state: 'idle', queue: [], run: null,
                  instruments: {}, verdicts: [{ level: 'warn', code: 'chain.led-polarity',
                                                text: 'NORM', node_path: '' }] };
  s.applyBench(stale, { readBack: true });
  assert.equal(s.getState().activeRunId, 'run-2', 'the older snapshot does not idle the bench');
  assert.equal(s.getState().benchState, 'preflight');
  assert.equal(s.getState().verdicts.length, 1, 'but its read-back still lands');
});

test('sigma_Q of zero is an absence, not a value', () => {
  // Most of the 9 x 5 archive carries 0.000000 because the loops were never
  // recorded; a zero-length error bar drawn as a bare dot is a lie.
  assert.equal(sigmaQ(0), null);
  assert.equal(sigmaQ(null), null);
  assert.ok(sigmaQ(4e-11).startsWith('4.00e-11'));
});

test('the store remembers the traces the ring would replay, and no more', () => {
  // The service keeps the arrays of the last `RING_TRACES_KEPT` = 200 shots
  // and strips the rest, in **one** deque for the whole session whatever run
  // or node the shot belongs to (`session.py`). So a console that had ever
  // been dropped held exactly those and one that had not held every shot of
  // the run. The store now keeps the service's number, globally, either way —
  // which bounds the tab as well as settling the invariant: measured on
  // `--sim --fast`, a decimated shot is ~14.6 kB of heap, and a cap *per node*
  // would keep 200 for each of the canonical tree's 45 leaves, ~130 MB.
  //
  // No fixture in the repo is 200 shots long, so the shape is a recorded
  // `StepDone` off the wire, renumbered: the arrays and the `decimated` block
  // are the service's own.
  const frames = parseJsonl(fixture('stream_bace_sim.jsonl'));
  const template = frames.find((f) => f.type === 'StepDone');
  assert.ok(template.data.light.y.length, 'the template carries its arrays');

  const s = store();
  s.applyFrames(frames.filter((f) => ['RunQueued', 'NodeStarted', 'RunStarted'].includes(f.type)));
  const runId = template.run_id;
  for (let i = 1; i <= 250; i += 1) {
    s.applyFrame({ ...template, seq: 10000 + i,
      data: { ...template.data, loop: Math.ceil(i / 25), index: i } });
  }

  const run = s.getState().runs[runId];
  assert.equal(run.shots.length, 250, 'every shot is still on the record');
  const withTraces = run.shots.filter((shot) => shot.traces);
  assert.equal(withTraces.length, 200, 'and two hundred of them still have their arrays');
  assert.equal(run.shots[249].traces.light.y.length, template.data.light.y.length,
    'the newest shot is whole');

  const forgotten = run.shots[0];
  assert.equal(forgotten.traces, null);
  assert.equal(forgotten.tracesGone, true, 'which is what a replayed shot says too');
  assert.equal(typeof forgotten.q, 'number', 'the charge survives: the loop curve is still drawable');
  assert.ok(forgotten.verdict, 'and so does its verdict');
});

test('the trace cap is one ring for the store, not one per node', () => {
  // The mistake this pins: `session.py`'s `_traces` is a single
  // `deque(maxlen=200)` appended on every `StepDone` whatever node it came
  // from, so a per-node cap does not mirror it and does not bound anything —
  // 200 per leaf across M5's 45-leaf tree is 9000 shots of arrays.
  const frames = parseJsonl(fixture('stream_bace_sim.jsonl'));
  const template = frames.find((f) => f.type === 'StepDone');
  const s = store();
  s.applyFrames(frames.filter((f) => ['RunQueued', 'RunStarted'].includes(f.type)));

  // Two module nodes under one run_id, 150 shots each: under the cap alone,
  // over it together.
  for (const node of ['rep=1/bace', 'rep=2/bace']) {
    s.applyFrame({ ...template, seq: 0, node_path: node, type: 'NodeStarted',
      data: { node_path: node, kind: 'module', label: node } });
    for (let i = 1; i <= 150; i += 1) {
      s.applyFrame({ ...template, seq: 20000 + i, node_path: node,
        data: { ...template.data, loop: 1, index: i } });
    }
  }

  const run = s.getState().runs[template.run_id];
  const nodes = ['rep=1/bace', 'rep=2/bace'].map((n) => run.nodes[n]);
  assert.deepEqual(nodes.map((n) => n.shots.length), [150, 150], 'every shot is on its node');

  const withTraces = nodes.flatMap((n) => n.shots).filter((shot) => shot.traces);
  assert.equal(withTraces.length, 200, 'two hundred across both nodes, not two hundred each');
  // And it is the newest 200 that survive, so the older node gives up first.
  assert.equal(nodes[0].shots.filter((shot) => shot.traces).length, 50);
  assert.equal(nodes[1].shots.filter((shot) => shot.traces).length, 150);
  for (const shot of nodes[0].shots.slice(0, 100)) {
    assert.equal(shot.traces, null);
    assert.equal(shot.tracesGone, true);
    assert.equal(typeof shot.q, 'number', 'the charge survives the forgetting');
  }
});

test('a replayed StepStarted from behind does not clear the phase of the shot in flight', () => {
  // `StepPhase` is live-only; the numbered frames replay from the ring. A
  // client catching up after a drop folds old starts while the instrument is
  // far ahead, and the indicator must describe the shot the bench is in.
  const s = store();
  const run = 'r1';
  s.applyFrame({ seq: 1, ts: 1, run_id: run, node_path: 'bace', type: 'RunQueued', data: { kind: 'manual', module: 'bace' } });
  s.applyFrame({ seq: null, ts: 2, run_id: run, node_path: 'bace', type: 'StepPhase', data: { index: 560, phase: 'acquire light', k: 3, of: 7 } });
  s.applyFrame({ seq: 300, ts: 3, run_id: run, node_path: 'bace', type: 'StepStarted', data: { index: 300, loop: 15, step: 1 } });
  assert.equal(s.getState().runs[run].phase.phase, 'acquire light', 'an older start leaves it');
  s.applyFrame({ seq: 900, ts: 4, run_id: run, node_path: 'bace', type: 'StepStarted', data: { index: 561, loop: 27, step: 16 } });
  assert.equal(s.getState().runs[run].phase, null, 'the next shot clears it');

  // But the index restarts at zero on every module node: the next node's
  // first shot is not an older shot of this one, and a new node is never in
  // the segment the last one was.
  s.applyFrame({ seq: null, ts: 5, run_id: run, node_path: 'rep=1/bace', type: 'StepPhase', data: { index: 560, phase: 'acquire dark', k: 6, of: 7 } });
  s.applyFrame({ seq: 901, ts: 6, run_id: run, node_path: 'rep=2/bace', type: 'StepStarted', data: { index: 0, loop: 1, step: 1 } });
  assert.equal(s.getState().runs[run].phase, null, 'another node\'s start clears it');
  s.applyFrame({ seq: null, ts: 7, run_id: run, node_path: 'rep=2/bace', type: 'StepPhase', data: { index: 0, phase: 'levels', k: 1, of: 7 } });
  s.applyFrame({ seq: 902, ts: 8, run_id: run, node_path: 'rep=3', type: 'NodeStarted', data: { node_path: 'rep=3', kind: 'repeat', label: 'rep=3' } });
  assert.equal(s.getState().runs[run].phase, null, 'and so does a node starting');
});

test('a pipeline keeps each node\'s acquisition config, not only the last one started', () => {
  const s = store();
  replayInto(s, parseJsonl(fixture('stream_tree_sim.jsonl')));
  const state = s.getState();
  const run = state.runs[state.order[0]];
  const leaves = Object.values(run.nodes).filter((n) => n.kind === 'bace');
  assert.equal(leaves.length, 4);
  for (const node of leaves) assert.equal(node.config.run.trigger_sweep, 'AUTO', node.node_path);
});

test('a pause the ring no longer holds comes back from the snapshot', () => {
  // A temperature pause lasts hours — that is what it is for — and the ring is
  // 5000 envelopes, so a console opened during one replays a tail with no
  // `NeedsOperator` in it. Without the snapshot's `pending` the screen shows a
  // paused run and no way to answer it: the experiment is blocked from the UI.
  const s = store();
  const pending = { what: 'temperature', node_path: 'T=250K', since: 100,
    detail: { setpoint_k: 250, tolerance_k: 0.2, hold_s: 60, index: 4, count: 9 } };
  s.applyBench({ state: 'paused', queue: [], instruments: {}, verdicts: [],
    run: { run_id: 'r1', state: 'paused', node_path: 'T=250K', pending } });
  const run = s.getState().runs.r1;
  assert.equal(run.needsOperator.what, 'temperature');
  assert.equal(run.needsOperator.node_path, 'T=250K');
  assert.equal(run.needsOperator.detail.setpoint_k, 250);
  assert.equal(run.needsOperator.ts, 100, 'when it opened, so a later resume can answer it');

  // The run record answers with the same field, for the boot that asks it.
  const other = store();
  other.applyRunRecord({ run_id: 'r2', state: 'paused', pending });
  assert.equal(other.getState().runs.r2.needsOperator.what, 'temperature');
});

test('a pause the stream has already answered does not come back with the snapshot', () => {
  // Nothing fetched moves anything backwards (decision 2): an HTTP response
  // and the socket race, and the answer is the newer fact.
  const s = store();
  s.applyFrame({ seq: 1, ts: 1, run_id: 'r1', node_path: '', type: 'RunQueued', data: { kind: 'pipeline' } });
  s.applyFrame({ seq: 2, ts: 90, run_id: 'r1', node_path: 'T=250K', type: 'NeedsOperator',
    data: { what: 'temperature', node_path: 'T=250K', detail: {} } });
  s.applyFrame({ seq: 3, ts: 120, run_id: 'r1', node_path: 'T=250K', type: 'OperatorResumed',
    data: { node_path: 'T=250K', note: 'set by hand', detail: {} } });
  s.applyBench({ state: 'running', queue: [], instruments: {}, verdicts: [],
    run: { run_id: 'r1', state: 'running',
      pending: { what: 'temperature', node_path: 'T=250K', since: 100, detail: {} } } });
  assert.equal(s.getState().runs.r1.needsOperator, null, 'the snapshot was taken before the resume');
});

test('a pause does not outlive the run it belonged to', () => {
  const s = store();
  s.applyFrame({ seq: 1, ts: 1, run_id: 'r1', node_path: '', type: 'RunStateChanged',
    data: { state: 'stopped', reason: 'requested' } });
  s.applyRunRecord({ run_id: 'r1', state: 'stopped',
    pending: { what: 'temperature', node_path: 'T=250K', since: 5, detail: {} } });
  assert.equal(s.getState().runs.r1.needsOperator, null, 'nobody is waiting for an answer');
});

test('a pause the run has moved on from is replaced by the one that is open', () => {
  // This console was away while another client answered T=250K and the run
  // opened T=280K, and those frames left the ring. Resume answers *the* open
  // pause, so a prompt still showing the old node would have the operator
  // type a temperature for a node the cryostat has left.
  const s = store();
  s.applyFrame({ seq: 1, ts: 1, run_id: 'r1', node_path: '', type: 'RunQueued', data: { kind: 'pipeline' } });
  s.applyFrame({ seq: 2, ts: 100, run_id: 'r1', node_path: 'T=250K', type: 'NeedsOperator',
    data: { what: 'temperature', node_path: 'T=250K', detail: { setpoint_k: 250 } } });
  assert.equal(s.getState().runs.r1.needsOperator.node_path, 'T=250K');

  s.applyBench({ state: 'paused', queue: [], instruments: {}, verdicts: [],
    run: { run_id: 'r1', state: 'paused', node_path: 'T=280K',
      pending: { what: 'temperature', node_path: 'T=280K', since: 900, detail: { setpoint_k: 280 } } } });
  const held = s.getState().runs.r1.needsOperator;
  assert.equal(held.node_path, 'T=280K', 'the open one');
  assert.equal(held.detail.setpoint_k, 280);

  // And a snapshot older than the prompt in hand changes nothing.
  s.applyBench({ state: 'paused', queue: [], instruments: {}, verdicts: [],
    run: { run_id: 'r1', state: 'paused',
      pending: { what: 'temperature', node_path: 'T=250K', since: 100, detail: { setpoint_k: 250 } } } });
  assert.equal(s.getState().runs.r1.needsOperator.node_path, 'T=280K');
});

test('the cost model\'s finish time is kept, for the runs that have no measured ETA', () => {
  const s = store();
  // On a read-back too: that is the only path it takes for a run started
  // while the console was open, and it is a constant of the run rather than
  // a state that could race the stream.
  s.applyBench({ state: 'running', queue: [], instruments: {}, verdicts: [],
    run: { run_id: 'r1', state: 'running', finish_at: 1788400000 } }, { readBack: true });
  assert.equal(s.getState().runs.r1.finish_at, 1788400000);
  assert.equal(s.getState().activeRunId, null, 'and the run block itself is still the stream\'s');

  const other = store();
  other.applyRunRecord({ run_id: 'r2', state: 'running', cost: { finish_at: 1788400111, lower_bound: false } });
  assert.equal(other.getState().runs.r2.finish_at, 1788400111);
});
