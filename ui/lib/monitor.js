// The run monitor — `docs/ui-plan.md` M4: what a run looks like while it runs.
//
// Shell furniture, like the rail: it sits under the rail on every tab for as
// long as a run holds the worker, and is gone when the bench is parked. A
// pause at hour three of a temperature sweep has to be answerable from
// whichever tab happens to be open, and a stop has to be one click from
// anywhere — both are about the bench, not about a view, which is the same
// reason Park lives on the strip.
//
// What it says, and where each line comes from (`docs/ui-rules.md` §5 — three
// counters at three time scales, and *"step 412 of 8400 is useless here"*):
//
//   * **the loops**, outermost first, from the executor's own `Progress`
//     frames — one per loop node, keyed by its `node_path`, which the store
//     keeps apart from the leaf's (`progressByNode`). `T=250K · 1 of 2`,
//     `LED 1.020 V · 2 of 2`. A temperature that is settling is said to be
//     *waiting*, never drawn as a step that runs.
//   * **the shots**, from the leaf's `Progress` and the node's own count —
//     `shot 4 of 24 · loop 2 · point 1`.
//   * **the segment**, from `StepPhase` — `5 · acquire light` — which is what
//     tells a slow settle from a hung acquisition (contract §3). Live-only,
//     never replayed, so it is shown only while the stream carries it.
//   * **the ETA**, the executor's re-derived one from the outermost loop's
//     `Progress` when there is one, else the module's own.
//
// And the two things the operator can *do*: stop — `after_shot`, the honest
// verb, or `abort`, armed first like Park — and answer a `NeedsOperator`, with
// the `temperature_k` the cryostat actually reached. `monitorModel` is pure;
// the DOM is beside it, and `ui/tests/monitor.test.mjs` holds the model down
// against the recorded tree.

import { h, keyed } from './dom.js';
import * as fmt from './format.js';
import { TERMINAL } from './store.js';

const LOOP_KINDS = new Set(['temperature', 'illumination', 'repeat']);

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
 * `shot 4 of 24 · loop 2 · point 1`, for the leaf the run is at — **the shot
 * being acquired**, not the last one that finished.
 *
 * `kept` counts `StepDone`s; between a `StepStarted` and its `StepDone` the
 * instrument is inside the next one, which is a second on the rig and the
 * whole of the first acquisition. Counted from `kept` alone the monitor
 * opened every node with `shot 0 of 630` and then trailed the loop, point and
 * segment beside it by one for the rest of the run.
 *
 * The in-flight shot is trusted only when it is this node's — the index
 * restarts at zero on every module node — and only when it is not *behind*
 * what has completed: a `StepStarted` replayed out of the ring after a drop
 * describes a shot that finished long ago, and its loop and point would be
 * the tail of the run rather than its head. `kept` never moves backwards, so
 * it is the floor.
 */
export function shotCounter(record, node) {
  // A loop node has no shots of its own: while a temperature settles the
  // run is *at* `T=250K`, and a counter reading `shot 0` there would be the
  // step that "runs" §5 says a settle must never look like.
  if (!node || LOOP_KINDS.has(node.kind)) return null;
  const isJv = node.kind === 'jv' || node.kind === 'jv_bace';
  const kept = typeof node.kept === 'number' ? node.kept : (isJv ? node.curves.length : node.shots.length);
  const requested = typeof node.requested === 'number' ? node.requested : null;
  const started = record.step;
  const step = !isJv && !node.outcome && started
    && (started.node_path || '') === (node.node_path || '')
    && typeof started.index === 'number' && started.index + 1 >= kept
    ? started : null;
  const nth = step ? Math.max(kept, step.index + 1) : kept;
  const parts = [`${isJv ? 'curve' : 'shot'} ${nth}${requested !== null ? ' of ' + requested : ''}`];
  if (step) {
    parts.push(`loop ${step.loop}`);
    if (node.values && node.values.length > 1) parts.push(`point ${step.step} of ${node.values.length}`);
  }
  return { kind: 'shots', kept, nth, requested, text: parts.join(' · ') };
}

/**
 * The shell's own state *about one run* — an armed Abort, the answer a stop
 * came back with — belongs to that run and to no other.
 *
 * Both outlive the run they describe unless something ends them, and the
 * frame that would (`parked`) is a frame like any other: a reconnect whose
 * ring gap swallowed it leaves the next run rendered with an Abort already
 * armed, so its first click discards a shot with no confirmation at all. The
 * owner is carried with the value and compared here rather than cleared on a
 * frame that may never arrive.
 */
export function scopedTo(runId, held) {
  if (!held || !runId) return null;
  return (typeof held === 'string' ? held : held.run_id) === runId ? held : null;
}

/** `5 · acquire light`, or nothing: `StepPhase` is live-only and belongs to the shot in flight. */
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
 * The prompt a `NeedsOperator` opens. `what` is `temperature` for a node with
 * no controller, and names the reason otherwise (`temperature timeout`,
 * `temperature refused`); the detail carries the target, and — for a timeout
 * — the reading it got to and why it gave up. The newest `TemperatureRead`
 * since the pause is shown beside it: with a controller attached the
 * cryostat is polled while the person decides, and that reading is *for*
 * them (contract §7).
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
    // What a resume with nothing typed takes: the last reading polled while
    // the operator decided, else nothing is bound and the setpoint's own
    // provenance stands (`how: "setpoint"`, never confirmed).
    fallback: reading ? `resume with nothing typed takes the last reading, ${fmt.kelvin(reading.kelvin)}`
      : 'resume with nothing typed leaves the temperature unconfirmed — the folder names carry the setpoint',
  };
}

/** Which of the run's verbs apply right now, and why the others do not. */
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
 * The whole monitor, as one plain object — `null` when nothing holds the
 * worker. The run being parked still owns it (`store.js`), so the monitor
 * says `stopping` through the park rather than vanishing before the bench is
 * safe.
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
    // Temperature settling is the slowest thing in the system by three orders
    // of magnitude and must not look like a step that "runs" (§5).
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
 * The ETA, counting down. The executor re-derives it at every loop boundary
 * (`executor.py`: "ETA 随实测重算"), which for the outermost loop of a
 * temperature sweep is once every half hour — so between boundaries the
 * finish time is what stands, and the seconds left are measured from the
 * newest frame's clock rather than repeated as the number the frame carried.
 */
function etaOf(outer, record, nowTs) {
  const source = outer && outer.eta_s !== null && outer.eta_s !== undefined ? outer
    : record.progress && record.progress.eta_s !== null && record.progress.eta_s !== undefined ? record.progress
      : null;
  if (!source) {
    return record.eta && record.eta.eta_s !== undefined
      ? { seconds: record.eta.eta_s, finish_at: record.eta.finish_at, from: 'record' } : null;
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
 * Into `el`, keyed in two halves. The counters, the phase and the buttons are
 * keyed on the model; the prompt is keyed on *which pause it is*, so a
 * `TemperatureRead` arriving while the operator is halfway through typing
 * 250.1 does not rebuild the field under their caret — the reading goes in
 * the half that may rebuild.
 */
export function renderMonitor(el, state, { onStop, onAbort, onResume, armedRun = null, status = null } = {}) {
  const model = monitorModel(state);
  if (!model) {
    el.hidden = true;
    // Clear the keys with the content, or a run that ends and starts again
    // with an identical first model would leave the old prompt on screen.
    if (el.__key !== undefined) { el.__key = undefined; el.textContent = ''; }
    return null;
  }
  el.hidden = false;
  if (!el.__key) {
    el.__key = 'monitor';
    el.replaceChildren(h('div.mon-row', h('div.mon-live'), h('div.mon-actions')), h('div.mon-prompt'));
  }
  const live = el.querySelector('.mon-live');
  const actions = el.querySelector('.mon-actions');
  const promptEl = el.querySelector('.mon-prompt');
  // Both belong to a run, and this may not be the run they were held for.
  const abortArmed = Boolean(scopedTo(model.run_id, armedRun));
  const answer = scopedTo(model.run_id, status);
  // The counters move with every shot; the buttons do not. Keyed together,
  // a `--fast` scan rebuilt the row 4850 times in eight seconds (measured),
  // and a Stop pressed between two shots would land on a button that no
  // longer existed — the M2 finding, on the one control that must not miss.
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
      title: model.eta.from === 'executor' ? 'the executor\'s ETA, re-derived from what this run has measured' : 'the module\'s own estimate',
      text: `ETA ${fmt.duration(model.eta.seconds)} · finish ${fmt.clock(model.eta.finish_at)}`,
    }));
  }
  return kids;
}

/** Stop, abort or cancel — keyed apart from the counters, so a click always lands. */
function actionButtons(model, { onStop, onAbort, abortArmed, status }) {
  const kids = [];
  if (status) kids.push(h('span', { class: 'mon-status ' + (status.level || ''), text: status.text }));
  const stop = model.stop;
  if (stop.canCancel) {
    kids.push(h('button.btns', { onclick: () => onStop && onStop(model.run_id, 'after_shot'), title: 'POST /runs/{id}/stop — a queued run is cancelled' }, 'Cancel'));
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

/** The seven (or six) segments, the current one lit — R3·2's `5 · acquire light`. */
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
    h('button.btnp', { onclick: submit, title: 'POST /runs/{id}/resume — answers the pause that is open and no other' }, 'Resume'),
  ];
}

/**
 * The reading, and what a blank resume binds — together, because the second
 * follows from the first: once a reading has been polled, a resume with
 * nothing typed takes it, and a sentence still saying "unconfirmed" would
 * have the operator submit under the wrong description of what the service
 * will do.
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
