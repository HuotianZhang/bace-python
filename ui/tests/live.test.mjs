// The proofs no fixture can give, run against a live service:
//
//     python3 -m bace.service --sim --fast --port 8900
//     BACE_SERVICE=http://127.0.0.1:8900 node --test ui/tests/live.test.mjs
//
// Without `BACE_SERVICE` they skip. They drive the console's own `stream.js`
// and `store.js` — Node has a global WebSocket, so this is the same code the
// browser runs, not a second implementation of it.
//
// The second one is the reason the journals are not enough: their `seq` runs
// 0…N with no gap, because that is what a journal is. Falling behind and being
// dropped at 1008 belongs to the socket, and `--sim --fast` produces frames
// faster than any socket takes them, which is exactly the condition.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createApi } from '../lib/api.js';
import { createStore } from '../lib/store.js';
import { createStream } from '../lib/stream.js';
import { railModel } from '../lib/rail.js';

const base = process.env.BACE_SERVICE || '';
const options = { skip: base ? false : 'set BACE_SERVICE to a running --sim service' };

function connect() {
  const store = createStore({ schedule: (fn) => setTimeout(fn, 0) });
  const seqs = [];
  const stream = createStream({
    url: base.replace(/^http/, 'ws') + '/events',
    onHello: (frame) => store.applyHello(frame),
    onFrame: (frame) => { if (frame.seq !== null) seqs.push(frame.seq); store.applyFrame(frame); },
    onStatus: (status) => store.applyConnection(status),
    onSessionChange: () => store.reset(),
  });
  stream.start(null);
  return { store, stream, seqs };
}

/** Wait until the store says this run reached a terminal state. */
async function settle(store, runId, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const run = store.getState().runs[runId];
    if (run && run.parked_at) return run;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`${runId} did not park within ${timeoutMs} ms`);
}

test('a jv reaches done, with kept and requested back', options, async () => {
  const api = createApi({ base });
  const { store, stream } = connect();
  try {
    const posted = await api.startRun('jv', { start_v: -0.2, stop_v: 1.2, step_v: 0.05 });
    const run = await settle(store, posted.run_id, 60000);
    assert.equal(run.state, 'done');
    assert.equal(run.kept, 1);
    assert.equal(run.requested, 1);
    assert.equal(run.curves.length, 1, 'one curve: jv sets no light, so there is no plan');
    assert.ok(run.folders.length >= 1, 'the run says where it wrote');
    // `jv` labels the curve from what it read, never from what it assumed.
    // On a cold `--sim` bench that read is a shut shutter.
    assert.match(run.curves[0].label, /^as found /);
    assert.equal(run.curves[0].dark, true);
  } finally {
    stream.close();
  }
});

test('light sets the bench, and the jv after it reads what light did', options, async () => {
  const api = createApi({ base });
  const { store, stream } = connect();
  try {
    // The manual form is the bench action, not a run: a light-only run is
    // refused because the park that ends every run would undo it.
    await assert.rejects(() => api.startRun('light', { shutter: 'open' }),
                         (err) => /only sets the light/.test(err.text || String(err)));
    await api.action('set-led-dc', { level: 1.02 });
    await api.action('shutter-open');

    const posted = await api.startRun('jv', { step_v: 0.05 });
    const run = await settle(store, posted.run_id, 60000);
    assert.equal(run.state, 'done');
    assert.equal(run.curves[0].dark, false, 'read back as lit');
    assert.equal(run.curves[0].label, 'as found 1.02 V');
    assert.ok(run.curves[0].metrics.voc > 0, 'a lit curve has a V_oc');
  } finally {
    await api.action('shutter-shut');
    stream.close();
  }
});

test('a client dropped at 1008 comes back without missing a numbered frame',
     { ...options, timeout: 600000 }, async () => {
  const api = createApi({ base });
  const { store, stream, seqs } = connect();
  let posted;
  try {
    posted = await api.startRun('bace', {
      // `record_length` is pinned, not inherited: the last-used layer carries
      // whatever the previous run set, and a longer record makes each shot
      // slow enough for the socket to keep up — which is the one thing this
      // test needs not to happen.
      axis_name: 'delay_ns', axis_start: 0, axis_stop: 200, axis_step: 10,
      centre_on_voc: false, vpre: 1.0, vcoll: -2.0, n_loops: 60,
      store_shots: false, record_length: 500, n_averages: 8,
    });
    const run = await settle(store, posted.run_id, 540000);
    const stats = stream.state.stats;

    assert.equal(run.state, 'done');
    assert.ok(stats.drops >= 1,
      `the client was never dropped (${stats.frames} frames): the scan was too small to outrun the socket`);

    // No numbered frame missed: every seq from the first to the last, once.
    const numbered = seqs.slice().sort((a, b) => a - b);
    const seen = new Set(numbered);
    const missing = [];
    for (let seq = numbered[0]; seq <= numbered[numbered.length - 1]; seq += 1) {
      if (!seen.has(seq)) missing.push(seq);
    }
    assert.deepEqual(missing, [], 'the since= replay filled every gap the drop made');
    assert.equal(seen.size, numbered.length, 'and delivered none of them twice');
    assert.equal(run.shots.length, run.kept);
    assert.equal(run.kept, run.requested, 'nothing was lost while the socket was being replaced');
  } finally {
    stream.close();
  }

  // Then the other half of the same guarantee, on purpose rather than by luck:
  // the ring keeps the traces of the last 200 shots only, so a client that
  // replays a long scan from the start gets the older shots back with their
  // arrays gone and `decimated[...].replay` set. Whether the *drop* above
  // reached back that far depends on how far behind the socket got; this does
  // not, and it is the same code path.
  const replayed = connect();
  try {
    replayed.stream.close();
    replayed.stream.start(0);
    const run = await settle(replayed.store, posted.run_id, 120000);
    const gone = run.shots.filter((shot) => shot.tracesGone);
    assert.ok(gone.length >= 1,
      'nothing came back stripped: the ring is larger than this scan, so raise n_loops');
    for (const shot of gone) {
      assert.equal(typeof shot.q, 'number', 'a replayed shot still carries its charge');
      assert.ok(shot.verdict, 'and its verdict — the loop curve is redrawable without the trace');
    }
    assert.ok(run.shots.some((shot) => shot.traces), 'and the newest shots still have their traces');
    // This is also what a page opened mid-run does, which is why the console
    // boots with `since=0`: the run comes back whole — its axis, its counts
    // and its outcome — none of which `/bench` carries.
    assert.ok(run.axis && run.values.length, 'the axis is rebuilt from the replay');
    assert.equal(run.kept, run.requested);
    assert.equal(run.state, 'done', 'including the terminal frames');
    // The stream counts frames over the whole session, this run's and every
    // earlier one's; the store counts the shots of this run.
    assert.ok(replayed.stream.state.stats.tracesGone >= gone.length);
  } finally {
    replayed.stream.close();
  }
});

test('a run hydrates from its own endpoints, in the shapes they answer',
     options, async () => {
  // The store's hydrate path exists for a run whose opening frames have left
  // the ring. What it needs is that `GET /runs/{id}` and `GET /runs/{id}/data`
  // say what it reads — which only a live service can show.
  const api = createApi({ base });
  const { store, stream } = connect();
  try {
    const posted = await api.startRun('bace', {
      axis_name: 'delay_ns', axis_start: 0, axis_stop: 200, axis_step: 100,
      centre_on_voc: false, vpre: 1.0, vcoll: -2.0, n_loops: 2, store_shots: false,
    });
    await settle(store, posted.run_id, 120000);

    const fresh = createStore({ schedule: () => {} });
    fresh.applyRunRecord(await api.run(posted.run_id));
    fresh.applyRunData(posted.run_id, 'bace', await api.runData(posted.run_id));
    const run = fresh.getState().runs[posted.run_id];

    assert.equal(run.module, 'bace');
    assert.equal(run.axis.name, 'delay_ns');
    assert.equal(run.values.length, 3);
    assert.equal(run.kept, 6);
    assert.equal(run.requested, 6);
    assert.ok(run.hydrated);
  } finally {
    stream.close();
  }
});

test('the rail follows a run that is holding the worker', options, async () => {
  // M1's own proof, and the one the fixtures cannot make on their own: while a
  // run has the bench, `GET /bench` is the snapshot Start took with the
  // running step's implications overlaid, and a console that fetched it at
  // boot and at `parked` would draw a cold rail — relay on the SourceMeter,
  // bias off, shutter shut — through the whole of a scan driving the device.
  //
  // So the shell re-asks on the frames that move the overlay. This is that
  // loop, without the browser: post a run, ask while it runs, and read the
  // rail off the answer.
  const api = createApi({ base });
  const store = createStore({ schedule: () => {} });
  store.applyBench(await api.bench());
  // Sized and polled so the window cannot be missed rather than probably
  // will not be: under `--fast` every settle is a no-op, so the original
  // 2-shot run finished inside the loop's own 100 ms sleep and this test
  // failed about one run in three. 84 shots of a long record is a few hundred
  // milliseconds of real simulation, and the loop below does not sleep at
  // all — a GET on localhost is about a millisecond, so there are hundreds of
  // looks inside the window.
  //
  // Every parameter that decides the size is named, none left to the
  // last-used layer: what a test runs must not depend on what ran before it.
  // The first version of this left `record_length` out, picked up the value
  // an earlier test had used, and made *the drop test* stop dropping.
  const posted = await api.startRun('bace', {
    axis_name: 'delay_ns', axis_start: 0, axis_stop: 100, axis_step: 5,
    centre_on_voc: false, vpre: 1.0, vcoll: -2.0, n_loops: 4, store_shots: false,
    record_length: 2000,
  });

  const deadline = Date.now() + 120000;
  let live = null;
  let parked = false;
  while (Date.now() < deadline && !live && !parked) {
    const bench = await api.bench();
    store.applyBench(bench, { readBack: true });
    const model = railModel(store.getState());
    const bias = model.find((c) => c.key === 'bias');
    if (bias.value === 'LIVE') live = model;
    // Stop looking once the run has let go: a hundred more polls would only
    // turn a missed window into a two-minute timeout. Off the snapshot, not
    // off the store — `applyBench(…, {readBack: true})` deliberately leaves
    // the run, the queue and the bench state to the stream, so the store's
    // copy would never say.
    parked = !live && bench.state === 'idle' && Boolean(bench.run);
  }
  assert.ok(live, 'the bias never read LIVE while the run was on the worker');

  const cell = (key) => live.find((c) => c.key === key);
  assert.equal(cell('bias').level, 'alert');
  assert.ok(cell('bias').inferred, 'a live bias during a run is inferred, never a read-back');
  assert.equal(cell('relay').value, 'amplifier', 'a bace drives the device through the amplifier');
  assert.ok(cell('relay').inferred);
  assert.ok(!cell('power').inferred, 'the monitors are still read, not implied');

  // And nothing inferred outlives the step that implied it: after the run
  // parks the same request answers a read-back again.
  while (Date.now() < deadline) {
    const bench = await api.bench();
    if (bench.state === 'idle') {
      store.applyBench(bench, { readBack: true });
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  const rested = railModel(store.getState());
  assert.equal(rested.filter((c) => c.inferred).length, 0, 'the overlay is gone with the run');
  assert.notEqual(rested.find((c) => c.key === 'bias').value, 'LIVE');
  assert.equal(posted.state, 'queued');
});

// -- M4: the tree, the pauses, and the loop curve after a drop ---------------

/**
 * Stand in for the operator: answer every `NeedsOperator` this run raises
 * through the same route the monitor's Resume posts to, with a typed
 * temperature a tenth of a kelvin off the setpoint. Returns the answers.
 */
function answerPauses(store, api, runId) {
  const answered = [];
  let busy = false;
  const off = store.subscribe(async (state) => {
    const run = state.runs[runId];
    if (!run || !run.needsOperator || busy) return;
    const pending = run.needsOperator;
    if (answered.some((a) => a.ts === pending.ts)) return;
    busy = true;
    try {
      const setpoint = Number((pending.detail || {}).setpoint_k);
      await api.resumeRun(runId, { temperature_k: Math.round((setpoint + 0.1) * 10) / 10, note: 'answered by the test' });
      answered.push({ ts: pending.ts, node_path: pending.node_path, setpoint });
    } catch (error) {
      // A resume that lands after the pause closed is refused with 409 and
      // dropped (contract §2) — which is right, and not this test's failure.
      if (error.status !== 409) throw error;
    } finally {
      busy = false;
    }
  });
  return { answered, stop: off };
}

test('a 2 T x 2 level tree stays readable start to finish, and a dropped client keeps its loop curve',
     { ...options, timeout: 600000 }, async () => {
  const { monitorModel } = await import('../lib/monitor.js');
  const { loopsModel, pointSummary } = await import('../lib/charts/loops.js');
  const api = createApi({ base });
  const { store, stream } = connect();
  const tree = {
    kind: 'loop', loop: 'temperature', label: 'T', values_k: [250, 280], tolerance_k: 0.5, hold_s: 1, timeout_s: 60,
    children: [{
      kind: 'loop', loop: 'illumination', levels_v: [1.010, 1.020], led_low_v: 0.4, led_settle_s: 0.1,
      children: [{
        kind: 'module', module: 'bace',
        // Enough shots per leaf to outrun the socket (the drop is the point),
        // and short records so the run is seconds rather than minutes.
        params: { axis_name: 'delay_ns', axis_start: 0, axis_stop: 200, axis_step: 10, centre_on_voc: false,
          vpre: 1.0, vcoll: -2.0, n_loops: 30, store_shots: false, record_length: 500, n_averages: 8 },
      }],
    }],
  };
  let posted;
  let pauses;
  const seen = { loops: new Set(), paused: 0, states: new Set() };
  try {
    posted = await api.startPipeline(tree, 'ui-live-tree');
    pauses = answerPauses(store, api, posted.run_id);
    const watch = store.subscribe((state) => {
      const model = monitorModel(state);
      if (!model || model.run_id !== posted.run_id) return;
      seen.states.add(model.state);
      seen.loops.add(model.loops.map((l) => l.text).join(' / '));
      if (model.state === 'paused') seen.paused += 1;
      // Readable at every frame: no counter runs past its total.
      if (model.shots) assert.ok(model.shots.kept <= model.shots.requested, model.shots.text);
      for (const loop of model.loops) assert.ok(loop.current === null || loop.current <= loop.total, loop.text);
    });
    const run = await settle(store, posted.run_id, 540000);
    watch();
    const stats = stream.state.stats;

    assert.equal(run.state, 'done');
    assert.equal(pauses.answered.length, 2, 'both temperature nodes paused, and both were answered');
    assert.deepEqual(pauses.answered.map((a) => a.node_path), ['T=250K', 'T=280K']);
    assert.equal(run.resumes.length, 2);
    assert.ok(seen.paused > 0);
    // The subscriber samples once per batch and `--fast` folds a whole leaf
    // into one, so what it *saw* is a sample; what the store *holds* is not.
    assert.ok([...seen.loops].some((t) => /^T 250 K · 1 of 2 \/ LED 1\.0\d0 V · \d of 2$/.test(t)), [...seen.loops].join(' | '));
    assert.ok([...seen.loops].some((t) => /^T 280 K · 2 of 2/.test(t)), [...seen.loops].join(' | '));
    // The executor's exit `Progress` for a loop node says how many of its
    // parent's children are done once it closed: the first temperature's
    // exit reads 1 of 2, the second's 2 of 2 — which is what the counter
    // shows, `T 250 K · 1 of 2`, and what the operator means by it.
    const expected = { 'T=250K': 1, 'T=250K/led=1.010V': 1, 'T=250K/led=1.020V': 2,
      'T=280K': 2, 'T=280K/led=1.010V': 1, 'T=280K/led=1.020V': 2 };
    for (const [path, done] of Object.entries(expected)) {
      const p = run.progressByNode[path];
      assert.ok(p, `${path}: the executor's own Progress reached the store`);
      assert.equal(p.total, 2, `${path}: of two`);
      assert.equal(p.done, done, `${path}: closed at ${done}`);
    }
    assert.ok(stats.drops >= 1,
      `the client was never dropped (${stats.frames} frames): the tree was too small to outrun the socket`);

    // The loop curve, per leaf, after the drop: every point measured, with a
    // σ behind it, whether or not its shots still carry their arrays — the
    // ring replays older shots without them (`decimated[…].replay`) and the
    // chart reads only the scalars.
    const leaves = Object.values(run.nodes).filter((n) => n.kind === 'bace');
    assert.equal(leaves.length, 4);
    let stripped = 0;
    for (const node of leaves) {
      assert.equal(node.values.length, 21, `${node.node_path}: the axis`);
      assert.equal(node.kept, 630, `${node.node_path}: kept`);
      const points = pointSummary(node);
      assert.ok(points.every((p) => p.mean !== null), `${node.node_path}: every point has a mean`);
      assert.ok(points.every((p) => p.sigma !== null), `${node.node_path}: every point has a σ after 30 loops`);
      const model = loopsModel({ record: run, node });
      assert.equal(model.switch.repeat, false);
      assert.equal(model.panels[0].rules.length, 21, `${node.node_path}: one error bar per point`);
      stripped += node.shots.filter((s) => s.tracesGone).length;
    }
    assert.ok(stripped >= 1, 'no shot came back without its arrays: the drop reached nothing the ring had stripped');
  } finally {
    if (pauses) pauses.stop();
    stream.close();
  }
});
