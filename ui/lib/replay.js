// The offline bench. `docs/ui-plan.md` decision 4: the UI is testable without
// the bench, from fixtures fed into the same store the WebSocket feeds — which
// is the only reason that is true. Nothing here is a test double; the frames
// are the service's own, recorded off a rig day or off `--sim`.
//
// The three shapes are not interchangeable, and the plan says which is which:
//
//   * **the journals** (`acceptance/20260902_service-vs-labview/journals/`)
//     replay every run semantic — the lifecycles, the chain verdicts, two real
//     `RunFailed` — and cannot draw a single curve: the journal payload policy
//     stores "enough to render the session log and the history queries, never
//     the traces";
//   * **a recorded stream** (`fixtures/stream_*.jsonl`) is the wire as a
//     client sees it: traces decimated with `stride` and `n_full`, and the
//     `StepPhase` frames that exist nowhere else;
//   * **`fixtures/hello_*.json`** is the bench snapshot, which no journal
//     contains, and **`fixtures/jv_*.json`** the J-V curves at full precision,
//     which the journal reduces to metrics;
//   * **`fixtures/modules_sim.json`** is `GET /modules` — every parameter of
//     every module with its provenance, its `doc` and its `doc_full`. It is
//     what M2's cards are generated from, so with it the bench tab is
//     workable offline; recorded off a service with a *clean* journal, or the
//     last-used layer of whoever recorded it rides along as if it were the
//     catalogue's own;
//   * **`fixtures/bench_running_*.json`** is `GET /bench` taken *while a run
//     held the worker*, which is the only place the `inferred` overlay exists:
//     at rest every instrument on the rail is a read-back;
//   * **`fixtures/validate_*.json`** is `POST /pipelines/validate` — the
//     checks, the schedule in order, the counters and the cost of a tree
//     nobody ran. It is what M5's schedule and its time bar are drawn from,
//     and the one endpoint in the set that can be recorded on a live bench at
//     any time, because it touches nothing.
//
// The journals also do *not* exercise the reconnect path — their `seq` runs
// 0…N with no gap, because that is what a journal is. Gaps and
// `decimated.replay` belong to the socket; test them against a live
// `--sim --fast` scan, which the service README says will drop a client.

export const FIXTURES = [
  { key: 'journal-153357', kind: 'journal', label: 'rig 15:33:57 — jv_dark, jv_bace, bace',
    url: '../acceptance/20260902_service-vs-labview/journals/20260902_153357.jsonl' },
  { key: 'journal-144844', kind: 'journal', label: 'rig 14:48:44 — the chain verdicts',
    url: '../acceptance/20260902_service-vs-labview/journals/20260902_144844.jsonl' },
  { key: 'journal-125751', kind: 'journal', label: 'rig 12:57:51 — two real RunFailed',
    url: '../acceptance/20260902_service-vs-labview/journals/20260902_125751.jsonl' },
  { key: 'stream-bace', kind: 'stream', label: 'sim — a bace scan off the wire, traces decimated',
    url: 'fixtures/stream_bace_sim.jsonl' },
  { key: 'stream-jv', kind: 'stream', label: 'sim — a jv_bace off the wire',
    url: 'fixtures/stream_jv_sim.jsonl' },
  { key: 'stream-pipeline', kind: 'stream',
    label: 'sim — a two-node pipeline: node identity, and the loop\'s own progress',
    url: 'fixtures/stream_pipeline_sim.jsonl' },
  { key: 'stream-tree', kind: 'stream',
    label: 'sim — 2 T x 2 levels x bace: the pauses, the operator\'s answers, Progress at three scales',
    url: 'fixtures/stream_tree_sim.jsonl' },
  { key: 'stream-stopped', kind: 'stream',
    label: 'sim — a 20-loop scan stopped after_shot at loop 13: kept of requested, and a node that ended stopped',
    url: 'fixtures/stream_stopped_sim.jsonl' },
  { key: 'hello', kind: 'hello', label: 'sim — the Hello frame, and the bench in it',
    url: 'fixtures/hello_sim.json' },
  { key: 'bench-running', kind: 'bench',
    label: 'sim — GET /bench mid-scan: the four instruments the run implies, every one inferred',
    url: 'fixtures/bench_running_sim.json' },
  { key: 'modules', kind: 'modules',
    label: 'sim — GET /modules: the catalogue the six bench cards are generated from',
    url: 'fixtures/modules_sim.json' },
  { key: 'jv-curves', kind: 'data', label: 'sim — GET /runs/{id}/data for the J-V',
    url: 'fixtures/jv_sim.json' },
  { key: 'transient', kind: 'data',
    label: 'rig 15:37:22 — the transient run at full precision, as the data endpoint answers',
    url: 'fixtures/transient_20260902_153722.json' },
  { key: 'validate-txill', kind: 'validate',
    label: 'sim — POST /pipelines/validate on the canonical 9 T x 5 level tree',
    url: 'fixtures/validate_txill_sim.json' },
  { key: 'validate-bound', kind: 'validate',
    label: 'sim — validate with a temperature module: the setpoint binds the rest of the run',
    url: 'fixtures/validate_bound_sim.json' },
  { key: 'validate-nested', kind: 'validate',
    label: 'sim — validate with a temperature loop inside a temperature loop',
    url: 'fixtures/validate_nested_sim.json' },
];

/** Parse a JSONL body into frames, skipping blank lines. */
export function parseJsonl(text) {
  return text.split('\n').map((line) => line.trim()).filter(Boolean).map((line) => JSON.parse(line));
}

export async function loadFixture(entry, { fetch: fetchImpl = globalThis.fetch, base = '' } = {}) {
  const response = await fetchImpl(base + entry.url);
  if (!response.ok) throw new Error(`${entry.url} -> ${response.status}`);
  const text = await response.text();
  return entry.url.endsWith('.jsonl') ? parseJsonl(text) : JSON.parse(text);
}

/**
 * Feed frames into a store as if they had arrived. `Hello` goes through
 * `applyHello` exactly as the stream does it, so the bench a recording carries
 * lands the same way it would live.
 */
export function replayInto(store, frames) {
  const list = Array.isArray(frames) ? frames : [frames];
  let hellos = 0;
  const rest = [];
  for (const frame of list) {
    if (frame && frame.type === 'Hello') { store.applyHello(frame); hellos += 1; }
    else rest.push(frame);
  }
  store.applyFrames(rest);
  return { frames: list.length, hellos, folded: rest.length };
}

/**
 * The same, paced, for watching a run redraw. `speed` multiplies the recorded
 * intervals; `0` is instant. Returns a handle with `stop()`.
 */
export function replayPaced(store, frames, { speed = 1, onDone = () => {} } = {}) {
  let index = 0;
  let timer = null;
  let stopped = false;
  const first = frames.length ? frames[0].ts : 0;

  function step() {
    if (stopped) return;
    const frame = frames[index];
    if (!frame) { onDone(); return; }
    if (frame.type === 'Hello') store.applyHello(frame); else store.applyFrame(frame);
    index += 1;
    const next = frames[index];
    if (!next) { onDone(); return; }
    const wait = speed > 0 ? Math.max(0, (next.ts - frame.ts) * 1000 / speed) : 0;
    timer = setTimeout(step, Math.min(wait, 2000));
  }

  step();
  return {
    stop() { stopped = true; if (timer) clearTimeout(timer); },
    get progress() { return { index, total: frames.length, first }; },
  };
}
