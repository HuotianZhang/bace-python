// Replay feeds service fixtures through the WebSocket store. Journals preserve run
// semantics but omit arrays (`docs/service-contract.md` §3). Recorded streams retain
// decimation and live phases. Hello supplies snapshots; J–V endpoints supply full
// curves; module fixtures supply provenance and explanations. Catalogue recording
// needs a clean journal to avoid last-used contamination. Running snapshots expose
// inferred state. Validation supplies checks, schedule, counters and cost without
// touching instruments. Run-index and record fixtures supply results and temperature
// provenance; simulated results recording answers pauses. Journal sequences have no
// ring gaps, so gap recovery and omitted replay traces require live socket tests
// against a fast simulated scan.

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
  { key: 'runs-index', kind: 'index',
    label: 'sim — GET /runs?session=all: every row with the device it ran on',
    url: 'fixtures/runs_sim.json' },
  { key: 'run-grid', kind: 'record',
    label: 'sim — GET /runs/{id} of a 2 T x 2 level pipeline stopped inside its last cell: the results grid',
    url: 'fixtures/run_grid_sim.json' },
  { key: 'run-bace', kind: 'record',
    label: 'sim — GET /runs/{id} of a manual bace at the Voc a jv_bace measured: one cell',
    url: 'fixtures/run_bace_sim.json' },
];

/** The parser reads JSONL frames and skips blank lines. */
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
 * The replay uses the live store path, including `applyHello` for bench snapshots, so
 * fixtures and socket data initialize identical state.
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
 * The paced replay multiplies recorded intervals by `speed`; zero feeds immediately.
 * Its returned handle supports `stop()`.
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
