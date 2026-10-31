/**
 * Result panels stay beside their forms. Timing responds to edits so signs, delays
 * and illumination errors are visible before a run (`docs/ui-rules.md` §7). Separate
 * result keys prevent frequent shots from rebuilding editable fields.
 */

import { h } from './dom.js';
import { chart } from './charts/frame.js';
import { transientModel } from './charts/transient.js';
import { jvModel } from './charts/jv.js';
import { timingModel } from './charts/timing.js';
import { loopsModel, AXIS_UNITS } from './charts/loops.js';
import * as fmt from './format.js';

/** The predicate identifies modules with result panels. */
export const RESULT_CARDS = new Set(['bace', 'jv', 'jv_bace']);

/**
 * The helper finds the most recent drawable module node and its run. Pipeline
 * `RunQueued.module` is `null`, so run-level lookup finds only manual runs. Node shot
 * numbering restarts, and run-level `lastShot` can belong to another module.
 * Selection must use matching nodes.
 */
export function runFor(state, name) {
  for (let i = state.order.length - 1; i >= 0; i -= 1) {
    const record = state.runs[state.order[i]];
    if (!record) continue;
    const node = newestNode(record, name);
    if (node) return { record, node };
  }
  return null;
}

function newestNode(record, name) {
  // Insertion order is arrival order, so the last match is the newest node.
  const nodes = Object.values(record.nodes || {});
  for (let i = nodes.length - 1; i >= 0; i -= 1) {
    const node = nodes[i];
    if (node.kind !== name) continue;
    if ((node.curves && node.curves.length) || (node.shots && node.shots.length)
      || (node.partial && node.partial.k)) return node;
  }
  return null;
}

/** The helper extracts current form values for the timing model. */
export function valuesOf(entry) {
  return Object.fromEntries((entry.params || []).map((p) => [p.name, p.value]));
}

/**
 * Signatures separate form from data. Timing responds immediately to parameters and
 * read-back; measured charts use a throttle. Shot identity uses node, numbering and
 * arrival rather than serializing hundreds of array values. Fast simulation reaches
 * about 130 shots/s on this bench, beyond readable redraw rates.
 * `createChartThrottle` coalesces these updates.
 */
export function resultKeys(name, entry, found, bench) {
  const form = [name];
  if (name === 'bace') {
    form.push(JSON.stringify(valuesOf(entry)));
    form.push(JSON.stringify(((bench && bench.chain && bench.chain.items) || [])
      .map((item) => `${item.key}=${item.value}`)));
    form.push(JSON.stringify((bench && bench.rig && bench.rig.values) || null));
  }
  const data = [];
  if (found) {
    const { record, node } = found;
    data.push(record.run_id, record.state || '', node.node_path);
    const shot = node.lastShot;
    if (shot) data.push(`${shot.node_path}:${shot.loop}:${shot.index}:${shot.ts}:${shot.tracesGone ? 'gone' : 'traces'}`);
    // Loop charts respond to shots and metadata: hydrated axes, recomputed
    // uncertainty and stopped nodes with unacquired loops all change the plot.
    data.push(`shots:${node.shots ? node.shots.length : 0}`, `loops:${node.loops ? node.loops.length : 0}`,
      `axis:${node.values ? node.values.length : 0}`, `kept:${node.kept ?? ''}/${node.requested ?? ''}`,
      `outcome:${node.outcome || ''}`, `aborted:${record.aborted ? record.aborted.reason : ''}`);
    if (node.curves && node.curves.length) {
      const last = node.curves[node.curves.length - 1];
      data.push(`curves:${node.curves.length}:${last.label}:${last.ts}`);
    }
    // The sweep in flight: the chart moves with every point read.
    if (node.partial) data.push(`partial:${node.partial.index}:${node.partial.direction}:${node.partial.k}`);
  }
  return { form: form.join('|'), data: data.join('|') };
}

/** The combined signature supplies the key for `dom.keyed`. */
export function resultKey(name, entry, found, bench) {
  const keys = resultKeys(name, entry, found, bench);
  return `${keys.form}|${keys.data}`;
}

/**
 * The interval bounds data redraws per card. Bench shots take about 0.8 s, allowing
 * each to draw. With 1260 simulated shots in nine seconds, measurements on this bench
 * give 4043 rebuilds and 77 282 SVG elements without throttling. Heap rises from 9.8
 * MB to 42.7 MB; main-thread contention raises bench-request latency from 13 ms to
 * 204 ms worst case. Coalescing preserves responsiveness.
 */
export const REDRAW_MS = 500;

/**
 * The throttle paints immediately or schedules the most recent request, retaining the
 * final draw. `paint` rereads the store so deferred work displays current data.
 */
export function createChartThrottle({
  redrawMs = REDRAW_MS,
  setTimeoutImpl = globalThis.setTimeout,
  clearTimeoutImpl = globalThis.clearTimeout,
  now = () => Date.now(),
} = {}) {
  const cards = new Map();
  const stats = { drawn: 0, deferred: 0 };

  function fire(name) {
    const card = cards.get(name);
    if (!card || !card.paint) return;
    if (card.timer) { clearTimeoutImpl(card.timer); card.timer = null; }
    card.at = now();
    stats.drawn += 1;
    const paint = card.paint;
    card.paint = null;
    paint();
  }

  return {
    /**
     * Operator-driven changes use `immediate` so scan throttling cannot delay typing
     * feedback.
     */
    request(name, { immediate = false, paint }) {
      // `-Infinity` allows the first draw at any clock origin. Zero would defer it
      // for a clock starting near zero, including test clocks.
      const card = cards.get(name) || { at: -Infinity, timer: null, paint: null };
      cards.set(name, card);
      card.paint = paint;
      if (immediate) { fire(name); return true; }
      const wait = redrawMs - (now() - card.at);
      if (wait <= 0) { fire(name); return true; }
      stats.deferred += 1;
      if (!card.timer) card.timer = setTimeoutImpl(() => { card.timer = null; fire(name); }, wait);
      return false;
    },
    stats: () => ({ ...stats }),
    dispose() {
      for (const card of cards.values()) if (card.timer) clearTimeoutImpl(card.timer);
      cards.clear();
    },
  };
}


/**
 * The renderer rebuilds on key changes. `bace` includes form timing and measured
 * traces; J–V modules include curves and derived metrics.
 */
export function resultPanel(name, { entry, found, bench }) {
  if (name === 'bace') return baceResult(entry, found, bench);
  if (name === 'jv' || name === 'jv_bace') return jvResult(name, found);
  return [];
}

/**
 * Before a run, timing leads the `bace` panel. During acquisition, shot values,
 * verdict, transient and Q lead because edited forms describe the next run. The
 * running card serves as monitor (`docs/ui-rules.md` §11).
 */
function baceResult(entry, found, bench) {
  const values = valuesOf(entry);
  const rig = (bench && bench.rig && bench.rig.values) || {};
  const chain = (bench && bench.chain) || {};
  const timing = timingModel(values, { rig, chain });
  // Closed folds need alert counts without SVG. Drawing waits for an open fold and
  // observed width.
  const diagram = [timingFold(entry.name, timing, (w) => timingModel(values, { rig, chain, width: w }))];

  const shot = found && found.node.lastShot;
  // Absent models reserve the eventual named panels. An early return would replace
  // chart geometry with an empty sentence before acquisition.
  if (!shot) {
    return [
      chart((w) => transientModel(null, { width: w }), { id: `${entry.name}/transient` }),
      chart((w) => loopsModel(null, { width: w }), { id: `${entry.name}/q` }),
      ...diagram,
    ];
  }

  const config = ((found.node.config || found.record.config || {}).run) || {};
  return [
    shotBlock(shot, found, config),
    chart((w, view) => transientModel(shot, {
      width: w,
      view,
      t0_int_s: values.t0_int_s,
      t_int_width_s: values.t_int_width_s,
      pulse_delay_s: pulseDelayS(shot, values, rig),
      offset_corrected: values.offset_correct,
      offset_region: values.offset_region,
      dark_reference: values.dark_reference,
    }), { id: `${entry.name}/transient` }),
    chart((w, view) => loopsModel(found, { width: w, view }), { id: `${entry.name}/q` }),
    ...diagram,
  ];
}

/**
 * Shot values include Q, mean, uncertainty, peaks, verdict and trigger mode. AUTO
 * with disconnected sync can yield plausible near-zero Q from untriggered noise
 * (`docs/ui-rules.md` §9).
 */
/**
 * The pure row model supplies Q, mean and uncertainty with their shot and point.
 * Repeating them in the crosshair row would lose that context. Zero Q uncertainty
 * remains absent rather than measured zero (`docs/ui-rules.md` §2).
 */
export function shotRows(shot, found) {
  const v = shot.verdict || null;
  const sigma = fmt.sigmaQ(shot.q_std);
  // `StepDone.index` counts from zero; the operator counts shots from one,
  // as the monitor's `shot 4 of 24` and the record's `kept` do.
  const nth = shot.index + 1;
  const rows = [
    ['Q', fmt.charge(shot.q)],
    ['Mean Q', fmt.charge(shot.q_mean)],
    ['σQ', sigma ? `${sigma} C` : fmt.ABSENT],
  ];
  if (v) {
    rows.push(['Peak light', fmt.amps(v.peak_light_a)]);
    rows.push(['Peak dark', fmt.amps(v.peak_dark_a)]);
  }
  return rows;
}

export function shotBlock(shot, found, config) {
  const v = shot.verdict || null;
  const axis = (found && found.node && found.node.axis) || null;
  const level = v && v.level === 'warn' ? 'alert' : v ? 'ok' : '';
  const nearZero = v && Number.isFinite(shot.q) && Number.isFinite(v.peak_light_a)
    && Math.abs(shot.q) < 1e-13;
  const nth = shot.index + 1;
  const rows = shotRows(shot, found);
  const trigger = config.trigger_sweep ? String(config.trigger_sweep).toUpperCase() : null;
  return h('div.shot', { class: level },
    h('div.shot-head',
      h('span.shot-title', { text: `Shot ${nth}, loop ${shot.loop}` }),
      shot.clipped ? h('span.tag.bad', { text: 'clipped' }) : null,
      shot.tracesGone ? h('span.tag', { text: 'no traces' }) : null),
    // A definition list pairs labels and values in the narrow chart-side column,
    // stacking each label above its number.
    h('dl.shot-rows', rows.map(([k, val]) => [h('dt', { text: k }), h('dd.num', { text: val })])),
    v && level === 'alert' ? h('div', { class: 'shot-verdict ' + level, text: v.text }) : null,
    trigger ? h('div', {
      class: 'shot-trigger' + (trigger === 'AUTO' ? ' auto' : ''),
      title: trigger === 'AUTO'
        ? 'AUTO sweeps anyway when no trigger arrives: a loose sync cable gives untriggered noise whose dark subtraction cancels to almost nothing'
        : 'TRIG waits for the edge, and a missing trigger becomes a timeout with a message',
      text: `Trigger ${trigger}` + (trigger === 'AUTO' && nearZero ? ': Q near zero, check the sync cable' : ''),
    }) : null);
}

/**
 * The helper resolves this shot's field arrival from delay plus `trigger_offset_s`
 * (manual 2.2). Along a delay scan the window follows the shot, not the form's single
 * delay. Form delay would leave shading and integral stationary while the measured
 * window moves. The latency is not sent to the generator.
 */
export function pulseDelayS(shot, values, rig) {
  const setpoint = (shot && shot.setpoint) || {};
  const delayNs = setpoint.delay_ns === undefined || setpoint.delay_ns === null
    ? values.delay_ns
    : setpoint.delay_ns;
  return (Number(delayNs) || 0) * 1e-9 + (Number(rig.trigger_offset_s) || 0);
}

function jvResult(name, found) {
  const node = found && found.node;
  const curves = ((node && node.curves) || []).slice();
  // The curve being swept, drawn with the finished ones — point by point,
  // as the service streams them (`JVPoint`, live-only).
  if (node && node.partial && node.partial.voltage.length) curves.push(node.partial);
  // As in `baceResult`: `jvModel([])` is an `absent` model, and the frame it
  // carries is the sweep's own, so the slot holds its shape before the run.
  if (!curves.length) return [chart((w) => jvModel([], { width: w }), { id: `${name}/jv` })];
  // The planned sweep range, so the axis does not grow with the curve.
  const jv = found.record && found.record.jv;
  const cfg = jv && jv.config && jv.config.jv;
  const planned = cfg && Number.isFinite(cfg.start_v) && Number.isFinite(cfg.stop_v) ? [cfg.start_v, cfg.stop_v] : null;
  // Charts wait for measured width; text metrics do not depend on width and render
  // immediately. Separate builds meet both constraints.
  const model = jvModel(curves, { planned });
  const out = [chart((w, view) => jvModel(curves, { planned, width: w, view }), { id: `${name}/jv` })];
  if (model.metrics.entries.length) out.push(metricsTable(model.metrics));
  return out;
}

/**
 * Disclosure belongs to the session by module. Rebuilding after edits or shots must
 * not close it.
 */
const openTiming = new Set();

/** Closed folds report severity counts with the worst first. */
export function alertSummary(alerts) {
  const order = ['invalid', 'alert', 'warn', 'info'];
  const counts = order.map((level) => [level, alerts.filter((a) => a.level === level).length])
    .filter(([, n]) => n > 0);
  return {
    worst: counts.length ? counts[0][0] : null,
    text: counts.length ? counts.map(([level, n]) => `${n} ${level}`).join(' · ') : 'nothing to flag',
  };
}

/**
 * The timing fold keeps forms compact. Its closed line retains alert counts, worst
 * severity and colour so hidden geometry cannot hide a constraint (`docs/ui-rules.md`
 * §7).
 */
function timingFold(name, timing, makeTiming) {
  const summary = alertSummary(timing.alerts);
  const open = openTiming.has(name);
  const body = h('div.tbody', { hidden: !open }, chart(makeTiming),
    timing.alerts.length ? alertList(timing.alerts) : null);
  const button = h('button.btng', { type: 'button', class: open ? 'on' : '', 'aria-expanded': String(open) },
    open ? 'hide' : 'show');
  button.onclick = () => {
    if (openTiming.has(name)) openTiming.delete(name); else openTiming.add(name);
    const now = openTiming.has(name);
    body.hidden = !now;
    button.textContent = now ? 'hide' : 'show';
    button.classList.toggle('on', now);
    button.setAttribute('aria-expanded', String(now));
  };
  return h('div.tfold',
    h('div.foldbar',
      h('span.cs', 'timing diagram'),
      h('span', { class: 'cs tsum ' + (summary.worst || 'none'), text: summary.text }),
      button),
    body);
}

function alertList(alerts) {
  return h('div.alerts', alerts.map((a) => h('div.warn1.' + a.level,
    h('span.code', { text: a.level }), h('span', { text: a.text }))));
}

/**
 * Metrics are labelled derived (`docs/ui-rules.md` §6). `J_sc` uses A because it
 * comes from `current` independently of area, so the density unit does not apply
 * (`docs/service-contract.md` §6).
 */
function metricsTable(metrics) {
  const keys = [];
  const notes = {};
  for (const entry of metrics.entries) {
    for (const row of entry.rows) {
      if (!keys.includes(row.key)) keys.push(row.key);
      if (row.note) notes[row.key] = row.note;
    }
  }
  const header = h('tr',
    h('th.l', { text: 'LED' }),
    ...keys.map((k) => h('th', { text: k + (notes[k] ? ` · ${notes[k]}` : '') })));
  const body = metrics.entries.map((entry) => {
    const byKey = Object.fromEntries(entry.rows.map((r) => [r.key, r.value]));
    return h('tr',
      h('td.l', { text: entry.label }),
      ...keys.map((k) => h('td.num', { text: byKey[k] === undefined ? fmt.ABSENT : byKey[k] })));
  });
  return h('div.metrics',
    h('table.rows', h('thead', header), h('tbody', body)),
    metrics.note ? h('p.chart-note', { text: metrics.note }) : null);
}
