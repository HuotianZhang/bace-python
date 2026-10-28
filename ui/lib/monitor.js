// The monitor remains below the rail on every tab while a run holds the worker,
// including park. Stop and prompts must be reachable from every view. Outer loops,
// shots and segments use separate counters (`docs/ui-rules.md` §5). `StepPhase` is
// live-only (`docs/service-contract.md` §3). Settling reads as waiting. ETA prefers
// outer-loop progress, then module progress, then a labelled cost prediction. Stop
// completes the shot; Abort arms first. Resume records the reached temperature. Pure
// `monitorModel` supports fixture tests.

import { h, keyed } from './dom.js';
import * as fmt from './format.js';
import { TERMINAL } from './store.js';

/**
 * Only known measurement modules get acquisition counters. Long waits and settling
 * must not imply acquisition (`docs/ui-rules.md` §5). Unfamiliar kinds receive no
 * counter before their semantics are known.
 */
const SHOT_KINDS = new Set(['bace']);
const CURVE_KINDS = new Set(['jv', 'jv_bace']);

/** The seven segments of a shot, in the order `run_transient_scan` yields them. */
export const PHASES = ['levels', 'light settle', 'acquire light', 'dark levels', 'dark settle', 'acquire dark', 'process'];

/**
 * A node path's segments as the operator reads them: `T=250K` → `T 250 K`,
 * `led=1.020V` → `LED 1.020 V`, `rep=2` → `repeat 2`, a leaf → its name.
 */
export function describeSegment(segment) {
  const m = /^([A-Za-z]+)=(.+)$/.exec(segment);
  if (!m) return { kind: 'module', label: segment };
  const [, key, raw] = m;
  if (key === 'T') return { kind: 'temperature', label: `T ${raw.replace(/K$/, ' K')}` };
  if (key === 'led') return { kind: 'illumination', label: `LED ${raw.replace(/V$/, ' V')}` };
  if (key === 'rep') return { kind: 'repeat', label: `repeat ${raw}` };
  return { kind: key, label: `${key} ${raw}` };
}

/**
 * The loop counters for the node the run is at. Each is the executor's
 * `Progress` for that loop, `done` counting the children it has finished —
 * so while the loop is open its current child is `done + 1`.
 */
export function loopCounters(record, path) {
  const segments = (path || '').split('/').filter(Boolean);
  const out = [];
  let prefix = '';
  for (const segment of segments) {
    prefix = prefix ? `${prefix}/${segment}` : segment;
    const described = describeSegment(segment);
    if (described.kind === 'module') continue;
    const progress = record.progressByNode[prefix] || null;
    const node = record.nodes[prefix] || null;
    const open = !node || !node.outcome;
    const total = progress ? progress.total : null;
    const done = progress ? progress.done : null;
    const current = done === null ? null : open ? Math.min(done + 1, total || done + 1) : done;
    out.push({
      key: prefix, kind: described.kind, label: described.label,
      done, total, current, open,
      text: current === null ? described.label : `${described.label} · ${current} of ${total ?? fmt.ABSENT}`,
    });
  }
  return out;
}

/**
 * The counter describes the shot in progress. Counting only `StepDone` would show
 * zero during the first acquisition and disagree with loop, point and segment.
 * `StepStarted` must belong to this node because indices restart per node. Replayed
 * starts can precede completed shots, so monotonic `kept` supplies the lower bound.
 */
export function shotCounter(record, node) {
  if (!node) return null;
  const curves = (node.curves || []).length;
  const shots = (node.shots || []).length;
  const isJv = CURVE_KINDS.has(node.kind) || (!node.kind && curves > 0);
  const acquires = isJv || SHOT_KINDS.has(node.kind) || (!node.kind && shots > 0);
  // Unknown kinds without acquisitions have no counter. Recorded acquisitions
  // identify nodes whose start has left the ring.
  if (!acquires) return null;
  const kept = typeof node.kept === 'number' ? node.kept : (isJv ? curves : shots);
  const requested = typeof node.requested === 'number' ? node.requested : null;
  const started = record.step;
  const step = !isJv && !node.outcome && started
    && (started.node_path || '') === (node.node_path || '')
    && typeof started.index === 'number' && started.index + 1 >= kept
    ? started : null;
  // J–V has no per-curve start event. An open node is inside the curve following its
  // last completion, capped at the requested total to avoid implying an extra sweep.
  const inFlight = isJv && !node.outcome
    ? Math.min(kept + 1, requested === null ? kept + 1 : requested)
    : null;
  const nth = step ? Math.max(kept, step.index + 1) : inFlight ?? kept;
  const parts = [`${isJv ? 'curve' : 'shot'} ${nth}${requested !== null ? ' of ' + requested : ''}`];
  // Live `JVPoint` supplies within-sweep progress, analogous to `StepPhase` within a
  // shot.
  if (isJv && !node.outcome && node.partial && node.partial.of) {
    parts.push(`point ${node.partial.k} of ${node.partial.of}`);
  }
  if (step) {
    parts.push(`loop ${step.loop}`);
    if (node.values && node.values.length > 1) parts.push(`point ${step.step} of ${node.values.length}`);
  }
  return { kind: 'shots', kept, nth, requested, text: parts.join(' · ') };
}

/**
 * Queued runs need individual Cancel controls. Stop cancels before execution
 * (`docs/service-contract.md` §6); starting first would unnecessarily expose the
 * sample. The ID labels entries whose `RunQueued` is absent from this console.
 */
export function queuedRuns(state) {
  return (state.queue || []).map((runId, i) => {
    const record = state.runs[runId] || null;
    const named = record && (record.module || record.name
      || (record.kind === 'pipeline' ? 'pipeline' : record.kind));
    return { run_id: runId, position: i + 1, label: named || runId };
  });
}

/**
 * Armed Abort and action replies belong to one run ID. A ring gap can omit `parked`,
 * so clearing only on that frame can arm the next run. Ownership travels with the
 * value and is checked during rendering.
 */
export function scopedTo(runId, held) {
  if (!held || !runId) return null;
  return (typeof held === 'string' ? held : held.run_id) === runId ? held : null;
}

/**
 * `5 · acquire light`, or nothing: `StepPhase` is live-only and belongs to the shot in
 * flight.
 */
export function phaseIndicator(record) {
  const phase = record.phase;
  if (!phase || !phase.phase) return null;
  return {
    k: phase.k, of: phase.of, phase: phase.phase,
    text: `${phase.k} · ${phase.phase}`,
    segments: PHASES.filter((p) => phase.of === 7 || p !== 'dark levels'),
  };
}

/**
 * The prompt renders the pause reason, target and any timeout reading. Readings
 * during the pause stay beside it because the controller polls while the operator
 * decides (`docs/service-contract.md` §7).
 */
export function pausePrompt(record, state) {
  const pending = record.needsOperator;
  if (!pending) return null;
  const d = pending.detail || {};
  const reading = state.temperature && (state.temperature.ts || 0) >= (pending.ts || 0) ? state.temperature : null;
  const temperature = /temperature/.test(pending.what || '');
  const lines = [];
  if (temperature) {
    lines.push(`set the cryostat to ${fmt.kelvin(d.setpoint_k)}`
      + (d.tolerance_k !== undefined ? ` ± ${fmt.sig(d.tolerance_k, 2)} K` : '')
      + (d.hold_s !== undefined ? `, then it holds ${fmt.duration(d.hold_s)}` : ''));
    if (d.index !== undefined && d.count !== undefined) lines.push(`temperature ${d.index + 1} of ${d.count}`);
    if (pending.what !== 'temperature') {
      lines.push(d.reason ? `${pending.what} · ${d.reason}` : pending.what);
      if (d.kelvin !== undefined && d.kelvin !== null) lines.push(`it got to ${fmt.kelvin(d.kelvin)}`);
      if (d.error) lines.push(String(d.error));
    }
  } else {
    lines.push(pending.what);
  }
  return {
    what: pending.what, node_path: pending.node_path, since: pending.ts,
    temperature,
    setpoint_k: d.setpoint_k ?? null,
    lines,
    reading: reading ? { kelvin: reading.kelvin, source: reading.source, in_band: reading.in_band, ts: reading.ts } : null,
    // Blank resume uses the last pause reading. Without one, the setpoint stays
    // unconfirmed (`docs/service-contract.md` §7).
    fallback: reading ? `resume with nothing typed takes the last reading, ${fmt.kelvin(reading.kelvin)}`
      : 'resume with nothing typed leaves the temperature unconfirmed — the folder names carry the setpoint',
  };
}

/** The helper identifies applicable run actions and reasons for unavailable ones. */
export function stopModel(record) {
  const state = record.state;
  const terminal = TERMINAL.has(state) || Boolean(record.parked_at);
  const queued = state === 'queued';
  const stopping = state === 'stopping';
  return {
    queued,
    stopping,
    canCancel: queued,
    canStop: !terminal && !queued && !stopping,
    canAbort: !terminal && !queued && !stopping,
    why: stopping ? (record.reason || 'stop requested') : terminal ? state : null,
  };
}

/**
 * The model is `null` when no run holds the worker. Parking retains ownership so
 * stopping remains visible through park.
 */
export function monitorModel(state) {
  const record = state.activeRunId ? state.runs[state.activeRunId] : null;
  if (!record || record.parked_at) return null;
  const path = record.needsOperator ? record.needsOperator.node_path : record.node_path;
  const node = record.nodes[record.node_path] || null;
  const loops = loopCounters(record, path || '');
  const prompt = pausePrompt(record, state);
  const paused = record.state === 'paused';
  const outer = loops.length ? record.progressByNode[loops[0].key] : null;
  const eta = etaOf(outer, record, state.lastFrame ? state.lastFrame.ts : null);
  const label = record.module || record.name || (record.tree && record.tree.loop ? `${record.tree.loop} tree` : record.kind) || 'run';
  const description = record.description || null;
  return {
    run_id: record.run_id,
    label,
    kind: record.kind,
    state: record.state,
    reason: record.reason || '',
    description,
    loops,
    shots: shotCounter(record, node),
    phase: paused ? null : phaseIndicator(record),
    // Temperature settling dominates by three orders of magnitude and must appear as
    // waiting rather than acquisition (`docs/ui-rules.md` §5).
    waiting: paused ? (prompt ? prompt.node_path : 'operator') : null,
    eta: paused ? null : eta,
    prompt,
    stop: stopModel(record),
    kept: record.kept,
    requested: record.requested,
    lastShot: lastShotLine(node),
  };
}

/**
 * ETA counts down from absolute finish time. The executor recalculates at loop
 * boundaries (`docs/service-contract.md` §7), about half an hour apart on this bench.
 * Between them the most recent frame clock updates remaining seconds.
 */
function etaOf(outer, record, nowTs) {
  const source = outer && outer.eta_s !== null && outer.eta_s !== undefined ? outer
    : record.progress && record.progress.eta_s !== null && record.progress.eta_s !== undefined ? record.progress
      : null;
  if (!source) {
    if (record.eta && (record.eta.finish_at || record.eta.eta_s !== undefined)) {
      // Hydrated ETA retains `finish_at` and seconds at measurement. Repeating those
      // seconds would freeze the countdown beyond the finish.
      const at = nowTs !== null && nowTs !== undefined ? nowTs : Date.now() / 1000;
      const finishAt = record.eta.finish_at ?? ((record.eta.at || at) + record.eta.eta_s);
      return { seconds: Math.max(0, finishAt - at), finish_at: finishAt, from: 'record' };
    }
    // J–V lacks measured ETA before a curve completes. Manual runs have no executor
    // ETA, so submit-time cost supplies a prediction that must be labelled predicted.
    if (record.finish_at) {
      const at = nowTs !== null && nowTs !== undefined ? nowTs : Date.now() / 1000;
      return { seconds: Math.max(0, record.finish_at - at), finish_at: record.finish_at, from: 'predicted' };
    }
    return null;
  }
  const finishAt = (source.ts || 0) + source.eta_s;
  const now = nowTs !== null && nowTs !== undefined ? nowTs : source.ts || 0;
  return { seconds: Math.max(0, finishAt - now), finish_at: finishAt, from: source.node_path ? 'executor' : 'module' };
}

/** The newest shot's verdict, for the one line the monitor gives it. */
function lastShotLine(node) {
  const shot = node && node.lastShot;
  if (!shot) return null;
  const v = shot.verdict || null;
  return {
    // Counted from one on screen, as the shot counter is; `index` is zero-based on the wire.
    index: shot.index + 1, loop: shot.loop, q: shot.q,
    level: v ? v.level : null,
    text: v ? v.text : null,
  };
}

// -- the DOM ----------------------------------------------------------------

/**
 * Counters and controls have separate keys from the pause prompt. Temperature updates
 * must not replace the input while the operator types; the live reading has its own
 * replaceable line.
 */
export function renderMonitor(el, state, { onStop, onAbort, onResume, armedRun = null, status = null } = {}) {
  const model = monitorModel(state);
  const queued = queuedRuns(state);
  if (!model && !queued.length) {
    el.hidden = true;
    // Removing content also clears its keys so an identical model in another run
    // cannot retain a stale prompt.
    if (el.__key !== undefined) { el.__key = undefined; el.textContent = ''; }
    return null;
  }
  el.hidden = false;
  if (!el.__key) {
    el.__key = 'monitor';
    el.replaceChildren(h('div.mon-row', h('div.mon-live'), h('div.mon-actions')),
      h('div.mon-prompt'), h('div.mon-queue'));
  }
  const live = el.querySelector('.mon-live');
  const actions = el.querySelector('.mon-actions');
  const promptEl = el.querySelector('.mon-prompt');
  // The queue outlives the run in front of it: it is drawn whether or not
  // anything holds the worker, and each entry carries its own Cancel.
  keyed(el.querySelector('.mon-queue'), JSON.stringify([queued, queueStatus(queued, status)]),
    () => queueRow(queued, status, onStop));
  el.querySelector('.mon-row').hidden = !model;
  if (!model) {
    keyed(live, '', () => []);
    keyed(actions, '', () => []);
    keyed(promptEl, '', () => []);
    return null;
  }
  // Armed Abort and replies are displayed only for their owning run.
  const abortArmed = Boolean(scopedTo(model.run_id, armedRun));
  const answer = scopedTo(model.run_id, status);
  // Controls are keyed apart from counters so frames cannot replace buttons during a
  // click. A fast scan measured on this bench rebuilds a combined row 4850 times in
  // eight seconds, enough to consume Stop clicks.
  keyed(live, JSON.stringify([model.state, model.label, model.loops, model.shots, model.waiting, model.phase,
    model.lastShot, model.eta && Math.round(model.eta.seconds), model.eta && model.eta.finish_at]),
  () => liveRow(model));
  keyed(actions, JSON.stringify([model.run_id, model.stop, abortArmed, answer]),
    () => actionButtons(model, { onStop, onAbort, abortArmed, status: answer }));
  const p = model.prompt;
  keyed(promptEl, p ? `${model.run_id}|${p.node_path}|${p.since}` : '', () => (p ? promptForm(model, p, onResume) : []));
  // The reading moves while the prompt stands; it lives in its own line so
  // it can be replaced without touching the input beside it.
  const readingEl = promptEl.querySelector('.mon-reading');
  if (readingEl) keyed(readingEl, JSON.stringify(p.reading), () => readingLine(p));
  return model;
}

function liveRow(model) {
  const stateClass = model.state === 'paused' ? 'paused' : model.state === 'stopping' ? 'stopping' : 'running';
  const kids = [
    h('span.mon-state', { class: stateClass, text: model.state || '' }),
    h('span.mon-run', { text: model.label, title: model.description || model.run_id }),
  ];
  for (const loop of model.loops) {
    kids.push(h('span.mon-count', {
      class: loop.kind + (loop.open ? ' open' : ''),
      title: loop.key,
      text: loop.text,
    }));
  }
  if (model.shots) kids.push(h('span.mon-count.shots', { text: model.shots.text }));
  if (model.waiting) {
    kids.push(h('span.mon-wait', { text: `waiting for the operator · ${model.waiting}` }));
  } else if (model.phase) {
    kids.push(phaseEl(model.phase));
  }
  if (model.lastShot && model.lastShot.level) {
    kids.push(h('span', { class: 'mon-verdict ' + (model.lastShot.level === 'warn' ? 'alert' : 'ok'),
      title: model.lastShot.text || '',
      text: model.lastShot.level === 'warn' ? `shot ${model.lastShot.index} · ${model.lastShot.text}` : `shot ${model.lastShot.index} ok` }));
  }
  kids.push(h('span.spacer'));
  if (model.eta) {
    kids.push(h('span.mon-eta', {
      title: model.eta.from === 'executor'
        ? 'the executor\'s ETA, re-derived from what this run has measured'
        : model.eta.from === 'predicted'
          ? 'the cost model\'s prediction, made when the run was submitted — nothing has been measured to re-derive it from yet'
          : 'the module\'s own estimate',
      text: `${model.eta.from === 'predicted' ? 'ETA ~' : 'ETA '}${fmt.duration(model.eta.seconds)} · finish ${fmt.clock(model.eta.finish_at)}`,
    }));
  }
  return kids;
}

/**
 * Action controls have keys separate from counters so clicks survive progress updates.
 */
function actionButtons(model, { onStop, onAbort, abortArmed, status }) {
  const kids = [];
  if (status) kids.push(h('span', { class: 'mon-status ' + (status.level || ''), text: status.text }));
  const stop = model.stop;
  if (stop.canCancel) {
    kids.push(h('button.btns', { onclick: () => onStop && onStop(model.run_id, 'after_shot'), title: 'cancel this run before it starts' }, 'Cancel'));
  } else {
    kids.push(h('button.btns', {
      disabled: !stop.canStop || null,
      title: stop.canStop ? 'after_shot: the shot in flight completes and is kept, then the run parks' : stop.why || '',
      onclick: () => onStop && onStop(model.run_id, 'after_shot'),
    }, 'Stop after this shot'));
    kids.push(h('button', {
      class: abortArmed ? 'btns armed' : 'btns',
      disabled: !stop.canAbort || null,
      title: stop.canAbort ? 'abort: the shot being acquired is discarded, nothing after it starts' : stop.why || '',
      onclick: () => onAbort && onAbort(model.run_id),
    }, abortArmed ? 'abort now?' : 'Abort'));
  }
  return kids;
}

/** Whichever queued run the last answer belongs to, so it is drawn beside it. */
function queueStatus(queued, status) {
  return queued.some((q) => scopedTo(q.run_id, status)) ? status : null;
}

/** The queue, one line, with the Cancel each entry needs. */
function queueRow(queued, status, onStop) {
  if (!queued.length) return [];
  return [
    h('span.mon-qk', { text: `queue ${queued.length}` }),
    queued.map((q) => {
      const answer = scopedTo(q.run_id, status);
      return h('span.mon-q',
        h('span', { title: q.run_id, text: `${q.position} · ${q.label}` }),
        answer ? h('span', { class: 'mon-status ' + (answer.level || ''), text: answer.text }) : null,
        h('button.btns', {
          title: 'cancel this run before it starts',
          onclick: () => onStop && onStop(q.run_id, 'after_shot'),
        }, 'Cancel'));
    }),
  ];
}

/**
 * The strip highlights the current segment among seven, or six with the same dark
 * reference.
 */
function phaseEl(phase) {
  return h('span.mon-phase', { title: 'where inside the shot the run is, from StepPhase' },
    h('span.mon-seg-row', phase.segments.map((name, i) => h('span', {
      class: 'mon-seg' + (i + 1 < phase.k ? ' done' : i + 1 === phase.k ? ' on' : ''),
      title: `${i + 1} · ${name}`,
    }))),
    h('span.mon-phase-text', { text: phase.text }));
}

function promptForm(model, p, onResume) {
  const temperature = h('input.v', { type: 'text', inputmode: 'decimal', placeholder: p.setpoint_k !== null ? fmt.kelvin(p.setpoint_k, { unit: false }) : '', 'aria-label': 'temperature_k' });
  const note = h('input.v.note', { type: 'text', placeholder: 'note — what you did', 'aria-label': 'note' });
  const submit = () => {
    const raw = temperature.value.trim();
    const typed = raw === '' ? null : Number(raw);
    if (raw !== '' && !Number.isFinite(typed)) { temperature.focus(); return; }
    onResume && onResume(model.run_id, { temperature_k: typed, note: note.value.trim() });
  };
  temperature.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
  note.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
  return [
    h('span.mon-ask', { text: `needs the operator · ${p.node_path}` }),
    h('span.mon-lines', p.lines.map((line) => h('span', { text: line }))),
    h('span.mon-reading'),
    h('label.mon-field', h('span', 'temperature_k'), temperature, h('i', 'K')),
    h('label.mon-field', note),
    h('button.btnp', { onclick: submit, title: 'answers the pause that is open and no other' }, 'Resume'),
  ];
}

/**
 * The prompt pairs the reading with what blank resume binds. Once polling supplies a
 * reading, calling it unconfirmed would misstate the service's behaviour
 * (`docs/service-contract.md` §7).
 */
function readingLine(p) {
  const r = p.reading;
  return [
    r ? h('span', {
      class: r.in_band === true ? 'ok' : r.in_band === false ? 'warn' : '',
      text: `reads ${fmt.kelvin(r.kelvin)} · ${r.source}${r.in_band === true ? ' · in band' : r.in_band === false ? ' · out of band' : ''}`,
    }) : null,
    h('span.mon-fallback', { text: p.fallback }),
  ];
}
