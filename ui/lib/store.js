// One store. Every view subscribes to it and nothing fetches for itself.
//
// `docs/ui-plan.md` decision 2: the screen is a projection of the event
// stream. The `/bench` snapshot and the `/events` frames fold into the state
// here, which is what makes `docs/ui-rules.md` §8 — "a manual run and a
// pipeline step share the same live monitor" — true by construction rather
// than by discipline: they are the same `run_id` on the same stream, and a
// manual run *is* a one-node pipeline in the service.
//
// The fold is deliberately dumb about meaning. It records what each frame
// says; it does not decide what a number means, and it never re-derives
// provenance (§6). Views read `source`/`detail`/`editable` as they arrived.

import { hasReplayedTraces } from './stream.js';

const LOG_LIMIT = 800;

/** Terminal run states, from the contract's state diagram. */
export const TERMINAL = new Set(['done', 'stopped', 'aborted', 'failed', 'blocked', 'cancelled']);

/** Frames that describe a run, and are meaningless without one. */
const RUN_SCOPED = new Set([
  'RunQueued', 'RunStateChanged', 'NodeStarted', 'NodeDone', 'RunStarted', 'AxisResolved',
  'DCMeasured', 'InstrumentState', 'StepStarted', 'StepPhase', 'StepDone', 'LoopDone',
  'Progress', 'RunFinished', 'RunAborted', 'RunFailed', 'JVStarted', 'JVCurveDone',
  'JVFinished', 'SeriesPointDone', 'NeedsOperator', 'OperatorResumed',
]);

export function createStore({ logLimit = LOG_LIMIT, schedule = queueMicrotask } = {}) {
  let state = emptyState();
  const listeners = new Set();
  let pending = false;

  function notify() {
    if (pending) return;
    pending = true;
    schedule(() => {
      pending = false;
      for (const listener of listeners) listener(state);
    });
  }

  function run(runId) {
    if (!runId) return null;
    let record = state.runs[runId];
    if (!record) {
      record = emptyRun(runId);
      state.runs[runId] = record;
      state.order.push(runId);
    }
    return record;
  }

  function log(frame, level, text) {
    state.log.push({
      seq: frame.seq, ts: frame.ts, type: frame.type, level,
      run_id: frame.run_id || null, node_path: frame.node_path || '', text,
    });
    if (state.log.length > logLimit) state.log.splice(0, state.log.length - logLimit);
  }

  const api = {
    getState: () => state,

    subscribe(listener) {
      listeners.add(listener);
      listener(state);
      return () => listeners.delete(listener);
    },

    /** Wholesale: a new session, or the page reloading. */
    reset() {
      state = emptyState();
      notify();
    },

    /** `GET /bench`, and the `data.bench` every `Hello` carries. */
    applyBench(bench) {
      if (!bench) return;
      state.bench = bench;
      state.session = bench.session || state.session;
      state.queue = bench.queue || [];
      state.benchState = bench.state || 'idle';
      state.readAt = bench.read_at || null;
      if (bench.run && bench.run.run_id) {
        state.activeRunId = bench.run.run_id;
        const record = run(bench.run.run_id);
        record.state = bench.run.state || record.state;
        record.node_path = bench.run.node_path || record.node_path;
        record.module = bench.run.module || record.module;
        record.progress = bench.run.progress || record.progress;
        record.eta = bench.run.eta || record.eta;
      } else {
        state.activeRunId = null;
      }
      notify();
    },

    /** `GET /modules`. Kept as it came: the provenance is the service's. */
    applyModules(payload) {
      const modules = (payload && payload.modules) || [];
      state.modules = {
        order: modules.map((m) => m.name),
        byName: Object.fromEntries(modules.map((m) => [m.name, m])),
        at: Date.now() / 1000,
      };
      notify();
    },

    /** One module entry, as `PUT /modules/{m}/params` answers it. */
    applyModule(entry) {
      if (!entry || !entry.name) return;
      state.modules.byName = { ...state.modules.byName, [entry.name]: entry };
      if (!state.modules.order.includes(entry.name)) state.modules.order.push(entry.name);
      notify();
    },

    /** The stream's `Hello`: the bench whole, and where the cursor stands. */
    applyHello(frame) {
      const data = frame.data || {};
      state.connection = { ...state.connection, session: data.session ? data.session.id : null, seq: data.seq };
      api.applyBench(data.bench);
    },

    applyConnection(status) {
      state.connection = { ...state.connection, ...status };
      notify();
    },

    /** One envelope. The whole fold. */
    applyFrame(frame) {
      if (!frame || !frame.type) return;
      fold(frame);
      notify();
    },

    applyFrames(frames) {
      for (const frame of frames) if (frame && frame.type) fold(frame);
      notify();
    },
  };

  function fold(frame) {
    const data = frame.data || {};
    const record = run(frame.run_id);
    if (!record && RUN_SCOPED.has(frame.type)) {
      // `run_id` is null for everything the service says about itself
      // (`BenchAction`, `PowerReading`, the bench `Verdict`s). A run's own
      // event without one would be a service bug, and the session log is
      // where it should show, rather than a crash in the fold.
      log(frame, 'warn', `${frame.type} with no run_id`);
      return;
    }
    switch (frame.type) {
      case 'SessionStarted':
        state.session = { ...(state.session || {}), ...data };
        log(frame, 'info', `session ${data.session_id || ''} · ${data.mode}${data.fast ? ' fast' : ''}`);
        break;

      case 'RunQueued':
        Object.assign(record, {
          kind: data.kind, module: data.module, tree: data.tree, name: data.name || null,
          params: data.params || {}, resolved: data.resolved || {},
          folder: data.folder || null, queued_at: frame.ts,
        });
        log(frame, 'info', `queued ${data.module || 'pipeline'}${data.name ? ' · ' + data.name : ''}`);
        break;

      case 'RunStateChanged': {
        record.states.push({ state: data.state, reason: data.reason, ts: frame.ts });
        if (data.state === 'parked') {
          // Every terminal state is reached *through* `parked`, and the
          // journal's last line for a finished run says so: done, then parked.
          // It is a transition, not a resting state (contract §2), so it must
          // not overwrite the outcome the run actually had — a run that reads
          // `parked` where it should read `failed` is the bench lying quietly.
          record.parked_at = frame.ts;
          record.phase = null;
          if (state.activeRunId === frame.run_id) state.activeRunId = null;
          if (!TERMINAL.has(record.state)) record.state = 'parked';
          break;
        }
        record.state = data.state;
        record.reason = data.reason || '';
        if (data.state === 'running' && !record.started_at) record.started_at = frame.ts;
        if (TERMINAL.has(data.state)) {
          record.finished_at = frame.ts;
          if (state.activeRunId === frame.run_id) state.activeRunId = null;
          record.phase = null;
        } else if (data.state !== 'queued') {
          state.activeRunId = frame.run_id;
        }
        log(frame, TERMINAL.has(data.state) && data.state !== 'done' ? 'warn' : 'info',
            `${data.state}${data.reason ? ' · ' + data.reason : ''}`);
        break;
      }

      case 'NodeStarted':
        record.nodes[data.node_path] = {
          ...(record.nodes[data.node_path] || {}),
          node_path: data.node_path, kind: data.kind, label: data.label, started_at: frame.ts, outcome: null,
        };
        record.node_path = data.node_path;
        break;

      case 'NodeDone': {
        const node = record.nodes[data.node_path] || { node_path: data.node_path };
        const detail = data.detail || {};
        record.nodes[data.node_path] = {
          ...node, outcome: data.outcome, detail, finished_at: frame.ts,
          kept: detail.kept, requested: detail.requested, folder: detail.folder, summary: detail.summary,
        };
        // A pipeline's counts are the sum over its module nodes, the same sum
        // the session's record makes; a manual run has one node and the sum is
        // that node. Either way the rail says "kept of requested" (§9).
        record.kept = sumNodes(record, 'kept');
        record.requested = sumNodes(record, 'requested');
        for (const folder of detail.folders || (detail.folder ? [detail.folder] : [])) {
          if (!record.folders.includes(folder)) record.folders.push(folder);
        }
        log(frame, data.outcome === 'ok' ? 'info' : 'warn',
            `${data.node_path} ${data.outcome}${detail.summary ? ' · ' + detail.summary : ''}`);
        break;
      }

      case 'RunStarted':
        Object.assign(record, {
          description: data.description, n_shots: data.n_shots, n_steps: data.n_steps,
          n_loops: data.n_loops, config: data.config || null,
        });
        record.requested = record.requested || data.n_shots || null;
        break;

      case 'AxisResolved':
        record.axis = data.axis || null;
        record.values = data.values || [];
        record.voc = data.voc === undefined ? record.voc : data.voc;
        break;

      case 'DCMeasured':
        record.dc = data;
        break;

      case 'InstrumentState':
        record.instruments = { ...record.instruments, ...(data.values || {}) };
        break;

      case 'StepStarted':
        record.step = data;
        record.phase = null;
        break;

      case 'StepPhase':
        // Live-only, `seq` null, never journalled or replayed: where inside
        // the shot the run is *now* (`docs/ui-rules.md` §7). Held on its own,
        // so a reconnect that loses it leaves the shot itself untouched.
        record.phase = { ...data, ts: frame.ts };
        break;

      case 'StepDone':
        foldStepDone(record, frame, data);
        break;

      case 'LoopDone':
        record.loops.push({ loop: data.loop, q_mean: data.q_mean, q_std: data.q_std, ts: frame.ts });
        record.q_mean = data.q_mean;
        record.q_std = data.q_std;
        break;

      case 'Progress':
        record.progress = { ...data, ts: frame.ts };
        break;

      case 'RunFinished':
        record.finished = data;
        record.axis = data.axis || record.axis;
        record.values = data.values || record.values;
        record.q_mean = data.q_mean || record.q_mean;
        record.q_std = data.q_std || record.q_std;
        break;

      case 'RunAborted':
        record.aborted = data;
        record.kept = data.done !== undefined ? data.done : record.kept;
        record.requested = data.total !== undefined ? data.total : record.requested;
        log(frame, 'warn', `aborted · ${data.reason || ''} ${data.done ?? ''}/${data.total ?? ''}`);
        break;

      case 'RunFailed':
        record.error = { text: data.error, where: data.where, ts: frame.ts };
        log(frame, 'crit', data.error || 'failed');
        break;

      case 'JVStarted':
        record.jv = data;
        break;

      case 'JVCurveDone':
        record.curves.push({ ...data, ts: frame.ts });
        break;

      case 'JVFinished':
        // The summary's curves are the same objects, reduced the same way in
        // the journal; the ones already collected carry the arrays when the
        // frame came off the socket, so they win.
        record.jvFinished = data;
        if (record.curves.length === 0) record.curves = (data.curves || []).slice();
        break;

      case 'SeriesPointDone':
        record.seriesPoints.push({ ...data, ts: frame.ts });
        break;

      case 'Verdict': {
        const entry = { level: data.level, code: data.code, text: data.text,
                        node_path: data.node_path || frame.node_path || '', data: data.data || {}, ts: frame.ts };
        if (frame.run_id) replaceVerdict(record.verdicts, entry);
        else replaceVerdict(state.verdicts, entry);
        if (data.level === 'crit' || data.level === 'warn') log(frame, data.level, data.text);
        break;
      }

      case 'Notice': {
        // Not every Notice belongs to a run: the one that says this client is
        // being dropped for falling behind carries no `run_id` at all, and
        // arrives (with `seq` null) on the way to a 1008 close.
        const notice = { level: data.level, text: data.text, since: data.since, ts: frame.ts };
        (record ? record.notices : state.notices).push(notice);
        log(frame, data.level === 'warning' ? 'warn' : (data.level || 'info'), data.text);
        break;
      }

      case 'NeedsOperator':
        record.needsOperator = { what: data.what, node_path: data.node_path, detail: data.detail || {}, ts: frame.ts };
        log(frame, 'warn', `needs operator · ${data.what}`);
        break;

      case 'OperatorResumed':
        record.needsOperator = null;
        record.resumes.push({ node_path: data.node_path, note: data.note, detail: data.detail || {}, ts: frame.ts });
        log(frame, 'info', `resumed${data.note ? ' · ' + data.note : ''}`);
        break;

      case 'BenchAction':
        state.actions.push({ name: data.name, args: data.args, result: data.result, by: data.by, ts: frame.ts });
        log(frame, 'info', `${data.name} · by ${data.by || 'hand'}`);
        break;

      case 'PowerReading':
        state.power = { watts: data.watts, trustworthy: data.trustworthy,
                        wavelength_nm: data.wavelength_nm, source: data.source, ts: frame.ts };
        break;

      case 'TemperatureRead':
        state.temperature = { kelvin: data.kelvin, setpoint_k: data.setpoint_k, in_band: data.in_band,
                              source: data.source, ts: frame.ts };
        break;

      default:
        // An event type this console does not know yet still belongs in the
        // session log: the service's vocabulary may grow, and a silent drop
        // would be the one thing nobody could see.
        log(frame, 'info', frame.type);
    }
    state.lastFrame = { type: frame.type, seq: frame.seq, ts: frame.ts, run_id: frame.run_id };
  }

  return api;
}

/**
 * One shot. The branch that only shows itself after a reconnect: a **replayed**
 * `StepDone` carries every scalar and its verdict with the four traces `null`
 * and `decimated[name].replay === true`. It means *redraw the loop curve, the
 * trace is gone* — so the scalars are folded as usual and whatever traces are
 * held are left alone. A client that blanked the chart here would have a bug
 * that only appears after being dropped at 1008.
 */
function foldStepDone(record, frame, data) {
  const replay = hasReplayedTraces(frame);
  const key = `${data.loop}:${data.index}`;
  const previous = record.shotsByKey[key];
  const shot = {
    index: data.index, loop: data.loop, step: data.step,
    setpoint: data.setpoint, axis_value: data.axis_value,
    q: data.q, q_mean: data.q_mean, q_std: data.q_std,
    intensity_w: data.intensity_w, clipped: data.clipped,
    verdict: data.verdict || null, ts: frame.ts,
    traces: replay ? (previous ? previous.traces : null) : {
      light: data.light || null, dark: data.dark || null,
      photo: data.photo || null, photo_averaged: data.photo_averaged || null,
      decimated: frame.decimated || {},
    },
    tracesGone: replay && !(previous && previous.traces),
  };
  record.shotsByKey[key] = shot;
  if (previous) record.shots[record.shots.indexOf(previous)] = shot;
  else record.shots.push(shot);
  record.lastShot = shot;
  record.kept = record.shots.length;
  if (shot.verdict && shot.verdict.level === 'warn') {
    record.shotWarnings.push({ index: data.index, loop: data.loop, text: shot.verdict.text, ts: frame.ts });
  }
}

/** One entry per `(code, node_path)`: a re-read replaces the earlier copy. */
function replaceVerdict(list, entry) {
  const at = list.findIndex((v) => v.code === entry.code && v.node_path === entry.node_path);
  if (at === -1) list.push(entry);
  else list[at] = entry;
}

function sumNodes(record, field) {
  let total = null;
  for (const node of Object.values(record.nodes)) {
    if (node[field] === undefined || node[field] === null) continue;
    total = (total || 0) + node[field];
  }
  return total === null ? record[field] : total;
}

export function emptyRun(runId) {
  return {
    run_id: runId, kind: null, module: null, name: null, node_path: '',
    tree: null, params: {}, resolved: {}, folder: null, folders: [],
    state: null, reason: '', states: [], parked_at: null,
    queued_at: null, started_at: null, finished_at: null,
    description: null, config: null, n_shots: null, n_steps: null, n_loops: null,
    axis: null, values: [], voc: null, dc: null,
    step: null, phase: null,
    shots: [], shotsByKey: {}, lastShot: null, shotWarnings: [],
    loops: [], q_mean: null, q_std: null,
    curves: [], jv: null, jvFinished: null, seriesPoints: [],
    progress: null, eta: null, finished: null, aborted: null, error: null,
    needsOperator: null, resumes: [],
    nodes: {}, verdicts: [], notices: [], instruments: {},
    kept: null, requested: null,
  };
}

export function emptyState() {
  return {
    connection: { state: 'idle', session: null, seq: null, stats: null },
    session: null,
    bench: null,
    benchState: 'idle',
    readAt: null,
    queue: [],
    modules: { order: [], byName: {}, at: null },
    runs: {},
    order: [],
    activeRunId: null,
    verdicts: [],
    actions: [],
    power: null,
    temperature: null,
    notices: [],
    log: [],
    lastFrame: null,
  };
}

/** The run a view should be showing: the live one, else the newest. */
export function currentRun(state) {
  if (state.activeRunId && state.runs[state.activeRunId]) return state.runs[state.activeRunId];
  for (let i = state.order.length - 1; i >= 0; i -= 1) {
    const record = state.runs[state.order[i]];
    if (record) return record;
  }
  return null;
}
