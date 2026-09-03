// What the shell does per frame, rather than what it draws.
//
// The store notifies on every batch of frames and a `--sim --fast` scan
// batches one per animation frame for the length of the run, so two decisions
// govern whether the console is usable during a run at all: what it rebuilds
// (`dom.keyed`) and when it asks the bench for a snapshot (`lib/watch.js`).
// Both are held down here without a browser.
//
// The numbers in the comments are from `--sim --fast` in headless Chromium,
// one 21 x 60 scan, 1260 shots.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { keyed } from '../lib/dom.js';
import { createBenchWatch, BENCH_MOVERS } from '../lib/watch.js';

/** `keyed` only touches `textContent` and `append`, so this is element enough. */
function fake() {
  return { textContent: 'x', appended: 0, append(...kids) { this.appended += kids.length; } };
}

test('a rebuild happens once per distinct key, not once per notify', () => {
  // 110 078 elements were built for one scan, for a rail that changed twice.
  const el = fake();
  let built = 0;
  const build = () => { built += 1; return []; };

  assert.equal(keyed(el, 'a', build), true, 'the first key always builds');
  for (let i = 0; i < 100; i += 1) keyed(el, 'a', build);
  assert.equal(built, 1, 'a hundred notifies with nothing new built nothing');

  assert.equal(keyed(el, 'b', build), true);
  assert.equal(built, 2, 'and a key that moved built exactly once');
});

test('the key is per element, so two containers do not shadow each other', () => {
  const rail = fake();
  const strip = fake();
  let built = 0;
  const build = () => { built += 1; return []; };
  keyed(rail, 'same', build);
  keyed(strip, 'same', build);
  assert.equal(built, 2, 'the same key on a different element is still a build');
});

// -- the bench watch --------------------------------------------------------

/** A watch with time and timers in hand, and a promise per request. */
function watchHarness({ throttleMs = 700 } = {}) {
  let clock = 10_000;
  const timers = [];
  const asked = [];
  const modules = [];
  let resolveBench = null;
  const watch = createBenchWatch({
    fetchBench: () => new Promise((resolve) => { asked.push(clock); resolveBench = resolve; }),
    fetchModules: () => { modules.push(clock); return Promise.resolve({ modules: [] }); },
    onBench: () => {},
    onModules: () => {},
    throttleMs,
    setTimeoutImpl: (fn, ms) => { timers.push({ fn, at: clock + ms }); return timers.length; },
    now: () => clock,
  });
  return {
    watch, asked, modules,
    /** Advance the clock and fire whatever is due. */
    tick(ms) {
      clock += ms;
      for (const t of timers.splice(0)) { if (t.at <= clock) t.fn(); else timers.push(t); }
    },
    // The response lands, and the `then/catch/finally` chain behind it runs.
    settle: async () => { const r = resolveBench; resolveBench = null; if (r) r({});
                          await new Promise((done) => setTimeout(done, 0)); },
  };
}

test('a shot phase asks for the bench, and a burst of them asks once', async () => {
  const h = watchHarness();
  const phase = { type: 'StepPhase', data: {} };

  assert.equal(h.watch.frame(phase), true, 'StepPhase moves the bench');
  assert.equal(h.asked.length, 1, 'and asks straight away');

  for (let i = 0; i < 50; i += 1) h.watch.frame(phase);
  assert.equal(h.asked.length, 1, 'while one is in flight, nothing else is sent');

  await h.settle();
  assert.equal(h.asked.length, 1, 'and the throttle is still closed when it lands');
  h.tick(700);
  assert.equal(h.asked.length, 2, 'the ask raised meanwhile is served when it opens, not dropped');
});

test('a want raised behind a closed throttle is never lost', async () => {
  // The shutter moves twice a shot and can be dropped safely; the read-back
  // after `parked` cannot — it has no frame behind it to ask again.
  const h = watchHarness();
  h.watch.frame({ type: 'StepPhase', data: {} });
  await h.settle();
  h.tick(100);
  h.watch.frame({ type: 'RunStateChanged', data: { state: 'parked' } });
  assert.equal(h.asked.length, 1, 'the throttle is shut');
  h.tick(600);
  assert.equal(h.asked.length, 2, 'and it went out when the throttle opened');
  assert.equal(h.modules.length, 1, 'parked re-reads the catalogue too: this run is its `last`');
});

test('a frame that says nothing about the bench asks for nothing', () => {
  const h = watchHarness();
  for (const type of ['StepDone', 'Progress', 'LoopDone', 'Verdict', 'PowerReading']) {
    assert.equal(h.watch.frame({ type, data: {} }), false, `${type} does not move the bench`);
  }
  assert.equal(h.asked.length, 0);
  assert.ok(!BENCH_MOVERS.has('StepDone'), 'the shot itself changes nothing the rail shows');
  assert.ok(BENCH_MOVERS.has('StepPhase'), 'the shutter inside it does');
});

test('a replayed frame asks like any other', () => {
  // It did not, once: `app.js` skipped the stream's `replay` frames, and after
  // a 1008 drop *every* frame to the end of a fast scan is at or below the new
  // `Hello`'s seq. Measured, the rail changed twice in a ten-second run and
  // stood still for 10.1 s of a 1260-shot one. The throttle above is what
  // bounds the cost — the whole boot replay coalesces into one request — so
  // the watch is not told which frames are replay and does not want to be.
  const h = watchHarness();
  assert.equal(h.watch.frame({ type: 'NodeStarted', data: {} }), true);
  assert.equal(h.asked.length, 1);
});
