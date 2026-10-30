// The store folds bench snapshots and events for all views. Manual runs and pipeline
// nodes share one event model and monitor (`docs/ui-rules.md` §8). Folding records
// service facts without interpreting quantities or re-deriving provenance; views
// retain supplied `source`, `detail` and `editable` (§6).

import { hasReplayedTraces } from './stream.js';

const LOG_LIMIT = 800;

/**
 * Trace retention is global across runs and nodes, matching the service's last 200
 * shots (`docs/service-contract.md` §3). A decimated shot uses about 14.6 kB of heap
 * on this bench. Per-node retention across 45 leaves would keep 9000 traces, about
 * 130 MB; one global ring uses about 3 MB regardless of tree size. Monitors need the
 * current shot, Q charts use scalars and results fetch full precision, so older
 * arrays are unnecessary.
 */
const TRACES_KEPT = 200;

/**
 * Stream state keeps the rail busy between snapshot fetches. Terminal outcomes map to
 * stopping while the worker parks; `parked` transitions the bench to idle rather than
 * becoming a lasting bench state (`docs/service-contract.md` §2).
 */
export function benchStateOf(runState) {
  if (BENCH_STATES.has(runState)) return runState;
  if (runState === 'queued') return 'preflight';
  return 'stopping';
}

const BENCH_STATES = new Set(['idle', 'preflight', 'running', 'paused', 'stopping']);

/**
 * The set contains terminal outcomes in the service state model
 * (`docs/service-contract.md` §2).
 */
export const TERMINAL = new Set(['done', 'stopped', 'aborted', 'failed', 'blocked', 'cancelled']);

/** Frames that describe a run, and are meaningless without one. */
const RUN_SCOPED = new Set([
  'RunQueued', 'RunStateChanged', 'NodeStarted', 'NodeDone', 'RunStarted', 'AxisResolved',
  'DCMeasured', 'InstrumentState', 'StepStarted', 'StepPhase', 'StepDone', 'LoopDone',
  'Progress', 'RunFinished', 'RunAborted', 'RunFailed', 'JVStarted', 'JVPoint', 'JVCurveDone',
  'JVFinished', 'SeriesPointDone', 'NeedsOperator', 'OperatorResumed',
]);

/**
 * The console cap covers about 5.5 h at 1 Hz or 66 min at 0.2 s. Older readings
 * remain in service history, capped at 200 000, and the journal
 * (`docs/service-contract.md` §8).
 */
export const POWER_LOG_MAX = 20000;

/**
 * Insertion orders readings by `ts` and rejects duplicates. Stream and history share
 * the service timestamp, so merging must retain each reading once
 * (`docs/service-contract.md` §8).
 */
export function insertReading(log, entry) {
  const last = log.length ? log[log.length - 1] : null;
  if (!last || entry.ts > last.ts) { log.push(entry); return true; }
  let lo = 0;
  let hi = log.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (log[mid].ts < entry.ts) lo = mid + 1; else hi = mid;
  }
  if (lo < log.length && log[lo].ts === entry.ts) return false;
  log.splice(lo, 0, entry);
  return true;
}

export function createStore({ logLimit = LOG_LIMIT, schedule = queueMicrotask } = {}) {
  let state = emptyState();
  const listeners = new Set();
  let pending = false;
  /**
   * The global trace queue holds shots oldest first. Views do not read it, so it
   * stays outside `state` and shares objects with run shot records.
   */
  let traced = [];

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
    // A separate log counter changes after the capped length stops growing. Keying
    // only on length would freeze rendering at 800 entries.
    state.logged += 1;
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

    /** Reset clears session state for a reload or service-session change. */
    reset() {
      state = emptyState();
      traced = [];
      notify();
    },

    /**
     * The snapshot supplies instruments, chain and V_oc. With `readBack`, run, queue
     * and bench state stay stream-owned: a response can race the next queued run's
     * start or an ending. Applying stale HTTP state would move the console backward.
     * Boot and Hello snapshots instead establish initial state.
     */
    applyBench(bench, { readBack = false } = {}) {
      if (!bench) return;
      state.bench = bench;
      state.session = bench.session || state.session;
      state.readAt = bench.read_at || null;
      // Snapshot monitor state determines the power switch; the requested preference
      // cannot establish that polling is active.
      if (Array.isArray(bench.monitors)) state.monitors = bench.monitors;
      // Snapshots replace the base verdict list because resolved warnings disappear
      // by omission. Merging would preserve them indefinitely. Only stream verdicts
      // following the snapshot survive above that base; relying on stream alone would
      // miss warnings emitted before connection.
      const readAt = bench.read_at || 0;
      const merged = (bench.verdicts || []).map((v) => ({ ...v, node_path: v.node_path || '', ts: readAt }));
      for (const verdict of state.verdicts) {
        if ((verdict.ts || 0) > readAt) replaceVerdict(merged, verdict);
      }
      state.verdicts = merged;
      // Submit-time cost prediction is constant for the run and can be read from a
      // snapshot without reverting stream state. Active J–V progress lacks measured
      // ETA, and ordinary starts need this refetch path to display any prediction.
      if (bench.run && bench.run.run_id && bench.run.finish_at) {
        run(bench.run.run_id).finish_at = bench.run.finish_at;
      }
      if (!readBack) {
        state.queue = bench.queue || [];
        state.benchState = bench.state || 'idle';
        if (bench.run && bench.run.run_id) {
          state.activeRunId = bench.run.run_id;
          const record = run(bench.run.run_id);
          record.state = bench.run.state || record.state;
          record.node_path = bench.run.node_path || record.node_path;
          record.module = bench.run.module || record.module;
          record.progress = bench.run.progress || record.progress;
          record.eta = bench.run.eta || record.eta;
          // Boot and Hello snapshots authoritatively describe open pauses; replay
          // follows with retained events. A pause absent from the snapshot is not
          // open.
          adoptPending(record, bench.run.pending, { authoritative: true });
        } else {
          state.activeRunId = null;
        }
      }
      notify();
    },

    /** `GET /monitors`, or the one entry `POST /monitors/{kind}` answers with. */
    applyMonitors(list) {
      if (!Array.isArray(list)) return;
      state.monitors = list;
      notify();
    },

    /**
     * History merges by `ts` so reloads recover retained readings without duplicating
     * stream data (`docs/service-contract.md` §8).
     */
    applyPowerHistory(body) {
      const points = (body && body.points) || [];
      for (const row of points) {
        if (!Array.isArray(row) || !Number.isFinite(row[0]) || !Number.isFinite(row[1])) continue;
        insertReading(state.powerLog, { ts: row[0], watts: row[1], trustworthy: row[2] !== false });
      }
      trimPowerLog(state);
      notify();
    },

    /** Clearing service power history also clears the console trace. */
    clearPowerLog() {
      state.powerLog = [];
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

    /**
     * The sample-update response applies immediately, without waiting for
     * `SampleNamed` on the socket. Both fold to the same session block; the
     * committing action should update the bar promptly.
     */
    applySession(session) {
      if (!session) return;
      state.session = { ...(state.session || {}), ...session };
      notify();
    },

    /** One module entry, as `PUT /modules/{m}/params` answers it. */
    applyModule(entry) {
      if (!entry || !entry.name) return;
      state.modules.byName = { ...state.modules.byName, [entry.name]: entry };
      if (!state.modules.order.includes(entry.name)) state.modules.order.push(entry.name);
      // Catalogue `at` changes provide a view render key.
      state.modules.at = Date.now() / 1000;
      notify();
    },

    /**
     * The record hydrates a run whose opening events have left the 5000-envelope ring
     * (`docs/service-contract.md` §3). A 100 × 51 scan exceeds that limit threefold
     * with shot and progress events alone. Bench summaries lack the axis and data,
     * requiring record and endpoint fetches. Hydration never reduces counts or
     * overwrites more recent stream facts.
     */
    applyRunRecord(payload) {
      if (!payload || !payload.run_id) return;
      const record = run(payload.run_id);
      for (const key of ['kind', 'module', 'name', 'tree', 'folder', 'queued_at', 'started_at']) {
        if (record[key] === null || record[key] === undefined) record[key] = payload[key] ?? record[key];
      }
      if (!record.state) record.state = payload.state || null;
      if (payload.params_as_executed && !Object.keys(record.resolved).length) {
        record.resolved = payload.params_as_executed;
      }
      for (const folder of payload.folders || []) {
        if (!record.folders.includes(folder)) record.folders.push(folder);
      }
      if (payload.progress && !record.progress) record.progress = payload.progress;
      if (payload.eta && !record.eta) record.eta = payload.eta;
      if (payload.cost && payload.cost.finish_at && !record.finish_at) {
        record.finish_at = payload.cost.finish_at;
      }
      adoptPending(record, payload.pending);
      if (payload.error && !record.error) record.error = { text: payload.error, where: '', ts: null };
      record.hydrated = true;
      notify();
    },

    /** `GET /runs/{id}/data` for the same run: the axis, and what it has kept. */
    applyRunData(runId, nodePath, data) {
      if (!data || !runId) return;
      const record = run(runId);
      const node = nodeOf(record, nodePath || record.node_path || '');
      if (data.axis) node.axis = record.axis = record.axis || data.axis;
      if (data.values) node.values = record.values = (record.values.length ? record.values : data.values);
      if (data.q_mean) node.q_mean = record.q_mean = record.q_mean || data.q_mean;
      if (data.q_std) node.q_std = record.q_std = record.q_std || data.q_std;
      if (data.voc !== undefined && data.voc !== null && record.voc === null) record.voc = data.voc;
      if (typeof data.kept === 'number') node.kept = Math.max(node.kept || 0, data.kept);
      if (typeof data.requested === 'number') node.requested = data.requested;
      rollUp(record);
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
      // Service events can have no `run_id`. Run-specific events without one belong
      // in the session log as malformed input rather than crashing folding.
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
          // `parked` records safe-state transition, not outcome
          // (`docs/service-contract.md` §2). It must not overwrite failed, stopped or
          // completed status.
          record.parked_at = frame.ts;
          record.phase = null;
          record.needsOperator = null;
          if (state.activeRunId === frame.run_id) {
            state.activeRunId = null;
            state.benchState = 'idle';
          }
          if (!TERMINAL.has(record.state)) record.state = 'parked';
          break;
        }
        if (record.state === 'stopping' && (data.state === 'preflight' || data.state === 'running')) {
          // An accepted preflight stop remains stopping despite subsequent worker
          // start reports. Otherwise the rail would imply the stop no longer applies.
          // Transitions stay recorded even when they cannot replace the displayed
          // state.
          log(frame, 'info', `${data.state} · after the stop was accepted`);
          break;
        }
        record.state = data.state;
        record.reason = data.reason || '';
        if (data.state === 'running' && !record.started_at) record.started_at = frame.ts;
        if (data.state === 'queued') {
          // Queue membership follows stream events as runs wait for or acquire the
          // worker. Waiting for park-time refetch alone would leave the count stale
          // during execution.
          if (!state.queue.includes(frame.run_id)) state.queue = [...state.queue, frame.run_id];
        } else {
          if (state.queue.includes(frame.run_id)) {
            state.queue = state.queue.filter((id) => id !== frame.run_id);
          }
        }
        if (TERMINAL.has(data.state)) {
          record.finished_at = frame.ts;
          record.phase = null;
          // Terminal states clear pending prompts even without `OperatorResumed`. A
          // stopped, aborted or failed pause has no waiting consumer and must not
          // retain a Resume form.
          record.needsOperator = null;
          // The active run retains the worker while park performs potentially
          // blocking instrument I/O. Clearing `activeRunId` before `parked` could
          // select a queued run as active.
          if (state.activeRunId === frame.run_id && data.state !== 'cancelled') {
            state.benchState = benchStateOf(data.state);
          }
        } else if (data.state !== 'queued') {
          state.activeRunId = frame.run_id;
          state.benchState = benchStateOf(data.state);
        }
        log(frame, TERMINAL.has(data.state) && data.state !== 'done' ? 'warn' : 'info',
            `${data.state}${data.reason ? ' · ' + data.reason : ''}`);
        break;
      }

      case 'NodeStarted':
        Object.assign(nodeOf(record, data.node_path), {
          kind: data.kind, label: data.label, started_at: frame.ts, outcome: null,
        });
        record.node_path = data.node_path;
        // A node change clears the preceding node's segment.
        record.phase = null;
        break;

      case 'NodeDone': {
        const detail = data.detail || {};
        Object.assign(nodeOf(record, data.node_path), {
          outcome: data.outcome, detail, finished_at: frame.ts,
          kept: detail.kept, requested: detail.requested, folder: detail.folder, summary: detail.summary,
        });
        rollUp(record);
        for (const folder of detail.folders || (detail.folder ? [detail.folder] : [])) {
          if (!record.folders.includes(folder)) record.folders.push(folder);
        }
        log(frame, data.outcome === 'ok' ? 'info' : 'warn',
            `${data.node_path} ${data.outcome}${detail.summary ? ' · ' + detail.summary : ''}`);
        break;
      }

      case 'RunStarted': {
        Object.assign(record, {
          description: data.description, n_shots: data.n_shots, n_steps: data.n_steps,
          n_loops: data.n_loops, config: data.config || null,
        });
        const node = nodeOf(record, frame.node_path);
        if (typeof data.n_shots === 'number') node.requested = data.n_shots;
        // Trigger mode is node-scoped because each module start replaces the
        // run-level copy. Earlier node shots must not acquire another node's trigger
        // label.
        node.config = data.config || null;
        rollUp(record);
        break;
      }

      case 'AxisResolved': {
        const node = nodeOf(record, frame.node_path);
        node.axis = record.axis = data.axis || null;
        node.values = record.values = data.values || [];
        if (data.voc !== undefined) node.voc = record.voc = data.voc;
        break;
      }

      case 'DCMeasured':
        record.dc = data;
        break;

      case 'InstrumentState':
        record.instruments = { ...record.instruments, ...(data.values || {}) };
        break;

      case 'StepStarted':
        // Shot identity includes node path because indices restart at zero in each
        // module node.
        record.step = { ...data, node_path: frame.node_path || '' };
        // Live phase clears only for an equal or later shot on the same node, or a
        // different node. During catch-up, replay can describe shot 300 while live
        // acquisition is at 560; clearing for every replayed start would suppress the
        // indicator. A lower index on another node can instead be that node's first
        // shot.
        if (!record.phase || typeof record.phase.index !== 'number'
            || (frame.node_path || '') !== (record.phase.node_path || '')
            || data.index >= record.phase.index) {
          record.phase = null;
        }
        break;

      case 'StepPhase':
        // Live-only `StepPhase` has null sequence and stays outside replay
        // (`docs/service-contract.md` §3). Keeping it separate lets reconnect loss
        // leave shot data intact.
        record.phase = { ...data, node_path: frame.node_path || '', ts: frame.ts };
        break;

      case 'StepDone':
        foldStepDone(record, nodeOf(record, frame.node_path), frame, data, traced);
        break;

      case 'LoopDone': {
        const loop = { node_path: frame.node_path || '', loop: data.loop,
                       q_mean: data.q_mean, q_std: data.q_std, ts: frame.ts };
        const node = nodeOf(record, frame.node_path);
        record.loops.push(loop);
        node.loops.push(loop);
        node.q_mean = record.q_mean = data.q_mean;
        node.q_std = record.q_std = data.q_std;
        break;
      }

      case 'Progress': {
        // Loop and leaf progress require separate node keys. Combining them would
        // replace temperature progress with shot counts, losing the three distinct
        // scales (`docs/ui-rules.md` §5).
        const scope = data.node_path || '';
        record.progressByNode[scope] = { ...data, ts: frame.ts };
        if (scope === '') record.progress = record.progressByNode[scope];
        break;
      }

      case 'RunFinished': {
        const node = nodeOf(record, frame.node_path);
        node.finished = record.finished = data;
        node.axis = record.axis = data.axis || record.axis;
        node.values = record.values = data.values || record.values;
        node.q_mean = record.q_mean = data.q_mean || record.q_mean;
        node.q_std = record.q_std = data.q_std || record.q_std;
        break;
      }

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

      case 'JVStarted': {
        record.jv = data;
        const node = nodeOf(record, frame.node_path);
        if (typeof data.n_curves === 'number') node.requested = data.n_curves;
        rollUp(record);
        break;
      }

      case 'JVPoint': {
        // Live `JVPoint` accumulates a node's incomplete curve. `JVCurveDone`
        // replaces it with the full curve. Reconnect loss affects only the growth
        // display (`docs/service-contract.md` §3).
        const node = nodeOf(record, frame.node_path);
        let p = node.partial;
        if (!p || p.index !== data.index || p.direction !== data.direction) {
          p = node.partial = {
            index: data.index, label: data.label, dark: data.dark, led_level_v: data.led_level_v,
            direction: data.direction, of: data.of, k: 0, partial: true,
            voltage: [], current: [], density: (data.density === null || data.density === undefined) ? null : [],
            node_path: frame.node_path || '', ts: frame.ts,
          };
        }
        p.k = data.k;
        p.ts = frame.ts;
        p.voltage.push(data.voltage);
        p.current.push(data.current);
        if (p.density) p.density.push(data.density);
        break;
      }

      case 'JVCurveDone': {
        const curve = { ...data, node_path: frame.node_path || '', ts: frame.ts };
        const node = nodeOf(record, frame.node_path);
        record.curves.push(curve);
        node.curves.push(curve);
        node.kept = node.curves.length;
        node.partial = null;
        rollUp(record);
        break;
      }

      case 'JVFinished':
        // Collected curves retain socket arrays; reduced journal summary curves
        // cannot replace that richer data.
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
        // Drop notices can have null `run_id` and null sequence before close 1008.
        // They belong to the service rather than a run.
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
                        wavelength_nm: data.wavelength_nm, source: data.source,
                        averaged: data.averaged ?? null, ts: frame.ts };
        // Replay can deliver readings out of order relative to retained data; history
        // may contain them too. Timestamp insertion handles both cases.
        if (Number.isFinite(frame.ts) && Number.isFinite(data.watts)) {
          insertReading(state.powerLog, { ts: frame.ts, watts: data.watts, trustworthy: data.trustworthy !== false });
          trimPowerLog(state);
        }
        break;

      case 'SampleNamed':
        // `SampleNamed.after` replaces the full identity block for all consoles. The
        // service owns merging and lower layers. Waiting for another bench fetch
        // would leave other consoles displaying stale filing identity.
        state.session = { ...(state.session || {}), sample: { ...data.after } };
        log(frame, 'info', `sample · ${(data.changed || []).join(', ') || 'named'}`);
        break;

      case 'ParamsEdited':
        // The views revalidate on this stamp; `lib/watch.js` refetches the values.
        state.paramsEdited = frame.seq;
        log(frame, 'info', `params · ${data.module} · ${(data.changed || []).join(', ')}`);
        break;

      case 'TemperatureRead':
        state.temperature = { kelvin: data.kelvin, setpoint_k: data.setpoint_k, in_band: data.in_band,
                              source: data.source, ts: frame.ts };
        break;

      default:
        // Unrecognized event types remain in the log. Service vocabulary can expand,
        // and silent drops would conceal those events.
        log(frame, 'info', frame.type);
    }
    state.lastFrame = { type: frame.type, seq: frame.seq, ts: frame.ts, run_id: frame.run_id };
  }

  return api;
}

/**
 * A replayed shot can carry scalars and verdict but null traces with
 * `decimated[name].replay` (`docs/service-contract.md` §3). Folding retains scalar Q
 * points and existing curves rather than blanking the chart during reconnect.
 */
function foldStepDone(record, node, frame, data, traced) {
  // Replay-dropped and journal-omitted arrays both mean no curve, with loop points
  // still available from scalars. `tracesGone` expresses this explicitly; a trace
  // object containing only nulls would invite dereferences
  // (`docs/service-contract.md` §3).
  const replay = hasReplayedTraces(frame);
  const arrays = data.light || data.dark || data.photo || data.photo_averaged;
  const gone = replay || !arrays;
  // Shot keys include node path because every pipeline module restarts numbering.
  // `loop:index` alone would overwrite another node's shots and could leave an
  // unrelated tail.
  const key = `${node.node_path}|${data.loop}:${data.index}`;
  const previous = record.shotsByKey[key];
  const shot = {
    node_path: node.node_path,
    index: data.index, loop: data.loop, step: data.step,
    setpoint: data.setpoint, axis_value: data.axis_value,
    q: data.q, q_mean: data.q_mean, q_std: data.q_std,
    intensity_w: data.intensity_w, clipped: data.clipped,
    verdict: data.verdict || null, ts: frame.ts,
    traces: gone ? (previous ? previous.traces : null) : {
      light: data.light || null, dark: data.dark || null,
      photo: data.photo || null, photo_averaged: data.photo_averaged || null,
      decimated: frame.decimated || {},
    },
    tracesGone: gone && !(previous && previous.traces),
  };
  record.shotsByKey[key] = shot;
  if (previous) {
    record.shots[record.shots.indexOf(previous)] = shot;
    node.shots[node.shots.indexOf(previous)] = shot;
  } else {
    record.shots.push(shot);
    node.shots.push(shot);
  }
  record.lastShot = shot;
  node.lastShot = shot;
  // Hydrated counts cannot decrease when replay contains fewer shots than the run
  // acquired.
  node.kept = Math.max(node.kept || 0, node.shots.length);
  if (shot.traces) keepTraces(traced, shot, previous);
  rollUp(record);
  if (shot.verdict && shot.verdict.level === 'warn') {
    record.shotWarnings.push({ index: data.index, loop: data.loop, text: shot.verdict.text, ts: frame.ts });
  }
}

/**
 * The helper retains arrays up to global `TRACES_KEPT`, marking evicted shots with
 * `tracesGone`. Shot objects are shared by run lists, node lists and keyed lookup, so
 * mutation updates all references. A duplicate replaces its predecessor's queue
 * position; two entries could let eviction clear the retained shot's arrays.
 */
function keepTraces(traced, shot, previous) {
  const at = previous ? traced.indexOf(previous) : -1;
  if (at === -1) traced.push(shot);
  else traced[at] = shot;
  while (traced.length > TRACES_KEPT) {
    const old = traced.shift();
    if (old.traces) { old.traces = null; old.tracesGone = true; }
  }
}

/**
 * Hydration restores open pauses missing from ring replay. A temperature pause can
 * last hours while the ring holds 5000 envelopes; without `pending`, a paused run
 * would have no Resume form. Bench and record responses provide it. Hydration cannot
 * revive terminal pauses, replace more recent stream prompts or undo a recorded
 * resume.
 */
function adoptPending(record, pending, { authoritative = false } = {}) {
  if (!record) return;
  if (record.parked_at || TERMINAL.has(record.state)) return;
  if (!pending || !pending.what) {
    // An authoritative snapshot with no pause clears a prompt whose resume event has
    // left the ring. Otherwise submissions to a measuring run return 409. A
    // read-back-only response cannot clear it because run state remains stream-owned.
    if (authoritative) record.needsOperator = null;
    return;
  }
  const since = pending.since || 0;
  // A later prompt replaces an earlier pause from another node. Resume answers the
  // currently open pause, so stale labels could assign an operator-entered
  // temperature to the wrong setpoint.
  if (record.needsOperator && (record.needsOperator.ts || 0) >= since) return;
  if (record.resumes.some((resume) => (resume.ts || 0) >= since)) return;
  record.needsOperator = {
    what: pending.what, node_path: pending.node_path || '',
    detail: pending.detail || {}, ts: since,
  };
}

/** One entry per `(code, node_path)`: a re-read replaces the earlier copy. */
function replaceVerdict(list, entry) {
  const at = list.findIndex((v) => v.code === entry.code && v.node_path === entry.node_path);
  if (at === -1) list.push(entry);
  else list[at] = entry;
}

/**
 * Module facts are keyed by node path because each pipeline node has its own axis,
 * loops and numbering. Run-level summaries describe the most recent node; keeping
 * data only there would let one node overwrite another.
 */
function nodeOf(record, path) {
  const key = path || '';
  const existing = record.nodes[key];
  if (existing) return existing;
  const created = { node_path: key, shots: [], curves: [], loops: [], partial: null };
  record.nodes[key] = created;
  return created;
}

/**
 * Run totals sum module-node counts like the service record. Transients count shots
 * and J–V counts curves. Counts update as acquisitions arrive rather than waiting for
 * `NodeDone`, keeping long sweeps visible (`docs/service-contract.md` §6).
 */
function rollUp(record) {
  let kept = null;
  let requested = null;
  for (const node of Object.values(record.nodes)) {
    if (typeof node.kept === 'number') kept = (kept || 0) + node.kept;
    if (typeof node.requested === 'number') requested = (requested || 0) + node.requested;
  }
  if (kept !== null) record.kept = kept;
  if (requested !== null) record.requested = requested;
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
    progress: null, progressByNode: {}, eta: null, finish_at: null,
    finished: null, aborted: null, error: null,
    needsOperator: null, resumes: [],
    nodes: {}, verdicts: [], notices: [], instruments: {}, hydrated: false,
    kept: null, requested: null,
  };
}

export function emptyState() {
  return {
    connection: { state: 'idle', session: null, seq: null, stats: null },
    session: null,
    paramsEdited: null,
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
    powerLog: [],
    monitors: [],
    temperature: null,
    notices: [],
    log: [],
    logged: 0,
    lastFrame: null,
  };
}

function trimPowerLog(state) {
  const log = state.powerLog;
  if (log.length > POWER_LOG_MAX) log.splice(0, log.length - POWER_LOG_MAX);
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
