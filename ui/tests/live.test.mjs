// The two proofs M0 owes that no fixture can give, run against a live service:
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

test('a jv_dark reaches done, with kept and requested back', options, async () => {
  const api = createApi({ base });
  const { store, stream } = connect();
  try {
    const posted = await api.startRun('jv_dark', { start_v: -0.2, stop_v: 1.2, step_v: 0.05 });
    const run = await settle(store, posted.run_id, 60000);
    assert.equal(run.state, 'done');
    assert.equal(run.kept, 1);
    assert.equal(run.requested, 1);
    assert.equal(run.curves.length, 1, 'one dark curve');
    assert.ok(run.folders.length >= 1, 'the run says where it wrote');
  } finally {
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
      axis_name: 'delay_ns', axis_start: 0, axis_stop: 200, axis_step: 10,
      centre_on_voc: false, vpre: 1.0, vcoll: -2.0, n_loops: 60, store_shots: false,
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
