// The watch refetches the bench when stream boundaries change the rail. The service
// owns inferred state; duplicating that logic in JavaScript would let provenance
// diverge. Fetching only at boot and park would leave an idle-looking rail during an
// active scan. This module owns pending requests, in-flight state and throttling so
// the policy can be tested independently.

/**
 * These boundaries change the rail overlay: relay at node start, bias and LED at run
 * start, arming and polarity read-back, shutter phases, source-meter activity and
 * node unwind. The watch asks the service rather than inferring instrument values
 * locally.
 */
export const BENCH_MOVERS = new Set([
  'NodeStarted', 'NodeDone', 'RunStarted', 'RunFinished', 'RunAborted', 'RunFailed',
  'StepStarted', 'StepPhase', 'InstrumentState', 'JVStarted', 'JVFinished', 'DCMeasured',
]);

/** At most one request per this many milliseconds, however many frames ask. */
export const REFETCH_MS = 700;

/**
 * The watch allows one request at a time and one per `throttleMs`, retaining requests
 * made during either wait. The park refetch needs this guarantee because no later
 * frame may retry it. Replayed frames also request updates; throttling coalesces
 * them. A 21 × 60 fast simulated scan measured on this bench can remain in replay
 * after close 1008 through the run ending. Suppressing replay requests leaves the
 * rail unchanged for 9.0 s of ten seconds and can lose its final read-back.
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
    // The async body catches synchronous `fetchBench` exceptions and always releases
    // `fetching`; chaining from a thrown call would leave the watch permanently
    // blocked. Injected fetchers need this even though API calls are async.
    // `readBack` applies instruments, chain and verdicts while leaving run, queue and
    // bench state to the ordered stream because HTTP can race a queued run's
    // preflight.
    // Both requests share `fetching`, so an older catalogue cannot land after a newer
    // one when edits arrive in quick succession.
    const bench = async () => {
      try { onBench(await fetchBench()); } catch { /* the next frame asks again */ }
    };
    const catalogue = async () => {
      if (!modules || !fetchModules) return;
      try { onModules(await fetchModules()); } catch { /* the catalogue keeps what it had */ }
    };
    Promise.all([bench(), catalogue()]).finally(() => { fetching = false; pump(); });
  }

  return {
    want,
    stats: () => ({ ...stats }),

    /** The frame handler returns whether an envelope requests a refetch. */
    frame(frame) {
      if (!frame || !frame.type) return false;
      if (frame.type === 'RunStateChanged' && frame.data && frame.data.state === 'parked') {
        // Park releases the worker and removes the inference overlay. Refetch
        // supplies the bench for the next run and catalogue last-result values.
        want({ modules: true });
        return true;
      }
      if (frame.type === 'ParamsEdited') {
        // Every console shares the edited layer, and a run uses it, so a console that
        // keeps the old value would show one value and run another.
        want({ modules: true });
        return true;
      }
      if (BENCH_MOVERS.has(frame.type)) { want(); return true; }
      return false;
    },
  };
}
