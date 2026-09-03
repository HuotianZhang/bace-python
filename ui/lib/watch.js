// When to ask `GET /bench` again — the rail's other half.
//
// `docs/ui-plan.md` decision 2, third amendment: the bench snapshot is the one
// thing the stream does not carry. While a run holds the worker the snapshot
// is the one Start took with the running step's implications overlaid, every
// field of it marked `how: "inferred"` (`service/live.py`) — and that overlay
// reaches a client only if the client asks. A console that asked at boot and
// at `parked` shows a cold rail — relay on the SourceMeter, bias off, shutter
// shut — through hours of a scan driving the device.
//
// So the *stream* says when to ask and the *service* stays the only thing that
// infers anything (`ui-rules` §6: render provenance, never re-derive it). The
// alternative — folding `live.py` a second time in JavaScript — would put the
// two out of step the first time either changed.
//
// This lives in its own module rather than in `app.js` because it is the one
// piece of the shell with state of its own — a want, a flight, a throttle —
// and that is exactly the kind of thing that is worth a test.

/**
 * The frames after which `GET /bench` says something new about the rail.
 *
 * They are the boundaries `service/live.py` folds: the relay at `NodeStarted`,
 * the bias and the LED at `RunStarted`, the arming and the polarity a run read
 * back, the shutter twice per shot at `StepPhase`, the SMU across a J-V, and
 * the module's own unwind at `NodeDone`.
 */
export const BENCH_MOVERS = new Set([
  'NodeStarted', 'NodeDone', 'RunStarted', 'RunFinished', 'RunAborted', 'RunFailed',
  'StepStarted', 'StepPhase', 'InstrumentState', 'JVStarted', 'JVFinished', 'DCMeasured',
]);

/** At most one request per this many milliseconds, however many frames ask. */
export const REFETCH_MS = 700;

/**
 * The watch: `frame(f)` on every envelope, and it asks when it should.
 *
 * One request at a time, at most one per `throttleMs`, and never a dropped
 * ask: a want raised while a request is in flight or the throttle is closed is
 * served when it opens. Dropping them would be fine for the shutter, which
 * moves again in a moment, and wrong for the read-back after `parked` — the
 * one that has no frame behind it to ask again.
 *
 * **Replayed frames ask too.** They did not, once: `app.js` handed the stream's
 * `replay` flag through and skipped them, on the reasoning that one read-back
 * per historical frame would be hundreds of requests. The throttle above
 * already makes that impossible — the whole boot replay coalesces into one
 * request — and the guard cost the rail its updates at exactly the moment the
 * bench is busiest. Measured, on a 21 x 60 `--sim --fast` scan: the client
 * falls behind, is dropped at 1008, reconnects, and every frame from there to
 * the end of the run arrives at or below the new `Hello`'s seq — so it counts
 * as replay, so nothing asks. The rail changed twice in a ten-second run and
 * sat still for 9.0 s of it, and the `parked` refetch — the one with no frame
 * behind it — was left to whichever frame happened to arrive after the client
 * caught up.
 */
export function createBenchWatch({
  fetchBench,
  fetchModules = null,
  onBench = () => {},
  onModules = () => {},
  throttleMs = REFETCH_MS,
  setTimeoutImpl = globalThis.setTimeout,
  now = () => Date.now(),
} = {}) {
  let wanted = false;
  let wantsModules = false;
  let fetching = false;
  let fetchedAt = 0;
  let timer = null;
  const stats = { asked: 0, coalesced: 0 };

  function want({ modules = false } = {}) {
    if (wanted) stats.coalesced += 1;
    wanted = true;
    wantsModules = wantsModules || modules;
    pump();
  }

  function pump() {
    if (!wanted || fetching) return;
    const wait = throttleMs - (now() - fetchedAt);
    if (wait > 0) {
      if (!timer) timer = setTimeoutImpl(() => { timer = null; pump(); }, wait);
      return;
    }
    wanted = false;
    const modules = wantsModules;
    wantsModules = false;
    fetching = true;
    fetchedAt = now();
    stats.asked += 1;
    // `readBack`: take the instruments, the chain and the verdicts, and leave
    // the run, the queue and the bench state to the stream — this response and
    // the next run's `preflight` frame race, and the loser must not be the one
    // that cannot arrive out of order.
    Promise.resolve(fetchBench())
      .then((bench) => onBench(bench))
      .catch(() => {})
      .finally(() => { fetching = false; pump(); });
    if (modules && fetchModules) Promise.resolve(fetchModules()).then(onModules).catch(() => {});
  }

  return {
    want,
    stats: () => ({ ...stats }),

    /** One envelope. Returns whether it asked for anything. */
    frame(frame) {
      if (!frame || !frame.type) return false;
      if (frame.type === 'RunStateChanged' && frame.data && frame.data.state === 'parked') {
        // The one moment the snapshot is known to have changed for good: the
        // run is off the worker, the overlay is gone, and what is on the rail
        // now is the bench the next run will start from. The catalogue moved
        // too — this run is the modules' `last`.
        want({ modules: true });
        return true;
      }
      if (BENCH_MOVERS.has(frame.type)) { want(); return true; }
      return false;
    },
  };
}
