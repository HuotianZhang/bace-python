/**
 * What a card shows beside its form — `docs/ui-plan.md` M3.
 *
 * Round 3 puts the result inside the card that produced it: `ch-jvdark` in the
 * `jv` card, `ch-transient` in the `bace` card. The timing diagram joins them
 * there rather than sitting on the rig tab, because it is a function of the
 * *form*: it redraws as the operator types, and what it makes visible — a
 * wrong V_coll sign, an absurd delay, a duty that has shortened the
 * illumination — is visible **before** the run rather than in the file
 * afterwards (`ui-rules` §7).
 *
 * Keyed on the data behind it, never rebuilt with the card: a shot arrives
 * every fraction of a second in `--sim --fast`, and a card rebuilt that often
 * is a card whose fields cannot be typed in.
 */

import { h } from './dom.js';
import { chart } from './charts/frame.js';
import { transientModel } from './charts/transient.js';
import { jvModel } from './charts/jv.js';
import { timingModel } from './charts/timing.js';
import { loopsModel, AXIS_UNITS } from './charts/loops.js';
import * as fmt from './format.js';

/** Which cards have a result panel at all. */
export const RESULT_CARDS = new Set(['bace', 'jv', 'jv_bace']);

/**
 * The newest **node** of this module that has anything to draw, and the run it
 * belongs to.
 *
 * By the run's `module` it would find only manual runs: a pipeline's
 * `RunQueued.module` is `null` and the module names live on the nodes, as
 * `NodeStarted.data.kind` (`bace`, `jv_bace`; a loop's is `repeat`). So the
 * canonical 2 x bace tree drew nothing at all on the card that produced it,
 * which `ui/fixtures/stream_pipeline_sim.jsonl` shows in four shots.
 *
 * Per node, not per run, for the reason M0 left on record for M5: a pipeline
 * is one `run_id` over many nodes, each numbering its shots from one, so the
 * run-level `lastShot` is whichever node moved last. The card wants *this*
 * module's newest one.
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

/** The values a module's form currently holds, as the timing model wants them. */
export function valuesOf(entry) {
  return Object.fromEntries((entry.params || []).map((p) => [p.name, p.value]));
}

/**
 * The signature the result panel is rebuilt on, in **two halves**.
 *
 * `form` is what the operator typed and what the bench read back — the timing
 * diagram is a function of it, so it redraws the moment either moves.
 * `data` is the run: the newest shot, the curves so far. A shot's identity is
 * its node, its numbering and its arrival — not its arrays, which are 800
 * numbers whose JSON would cost more than the redraw it is guarding.
 *
 * They are separate because they deserve different cadences. Typing has to
 * feel immediate; shots arrive at 130 a second under `--sim --fast`, which is
 * a hundred redraws nobody can read. See `createChartThrottle`.
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
    // M4: the loop chart moves with every shot *and* with what the run knows
    // about itself — the axis arriving late from `GET /runs/{id}/data`, a
    // `LoopDone` recomputing every point's σ, the node ending `stopped` with
    // loops never acquired.
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

/** Both halves as one string — what `dom.keyed` is given. */
export function resultKey(name, entry, found, bench) {
  const keys = resultKeys(name, entry, found, bench);
  return `${keys.form}|${keys.data}`;
}

/**
 * At most one data-driven redraw per this many milliseconds, per card.
 *
 * **On the rig this changes nothing.** A shot takes about 0.8 s there
 * (`journal.shot_time_s`), so every one of them draws. It exists for
 * `--sim --fast`, where 1260 shots arrive in nine seconds: measured, the
 * charts were rebuilt 4043 times and the shell built 77 282 SVG elements for a
 * plot no eye could follow, the heap went 9.8 MB → 42.7 MB, and `GET /bench`
 * — which shares the main thread — went from 13 ms to a 204 ms worst case.
 * The rail met the same problem in M1 and answered it the same way; this is
 * `REFETCH_MS` for pixels.
 */
export const REDRAW_MS = 500;

/**
 * Draw now, or once the throttle opens — and never drop a draw.
 *
 * `paint` is a closure that re-reads the store, so a deferred redraw shows the
 * newest shot rather than the one that was pending when it was deferred. The
 * last request wins, which for a chart is exactly right: the intermediate
 * states are frames of an animation nobody asked for.
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
    /** `immediate` for anything the operator did: typing must not wait on a scan. */
    request(name, { immediate = false, paint }) {
      // `-Infinity`, not 0: a card that has never drawn must draw now, and
      // with a zero the very first request is deferred whenever the clock is
      // near its own origin — true of every fake clock, and of nothing else,
      // which is exactly the kind of thing that ships.
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
 * The panel's contents, built fresh each time its key changes.
 *
 * `bace` gets the shot it will take and the shot it last took, in that order:
 * the diagram is about the form and is there before any run, the transient is
 * about the run and appears with the first shot. `jv` and `jv_bace` get their
 * curves and the interpolated metrics beside them.
 */
export function resultPanel(name, { entry, found, bench }) {
  if (name === 'bace') return baceResult(entry, found, bench);
  if (name === 'jv' || name === 'jv_bace') return jvResult(found);
  return [];
}

/**
 * The `bace` card's panel. Before a run it is the shot the form describes;
 * once there is a run it is the run first — the newest shot and its verdict,
 * the transient, Q per loop or Q(axis) — and the diagram after, because a
 * form that is being edited during a scan is the *next* run, and the one
 * going is what the operator is watching (`ui-rules` §11: the running card
 * is the monitor).
 */
function baceResult(entry, found, bench) {
  const values = valuesOf(entry);
  const rig = (bench && bench.rig && bench.rig.values) || {};
  const chain = (bench && bench.chain) || {};
  const timing = timingModel(values, { rig, chain });
  // The fold gets the model for its alert count and a maker for the drawing:
  // the count is needed shut, the drawing only when open and only at a width
  // nobody knows until the observer measures one.
  const diagram = [timingFold(entry.name, timing, (w) => timingModel(values, { rig, chain, width: w }))];

  const shot = found && found.node.lastShot;
  // No early return for the unrun card. `transientModel(null)` and
  // `loopsModel(null)` both answer with an `absent` model, and `chart()` draws
  // that as the panels the run will fill — so the card has one geometry either
  // side of a run rather than a sentence before and three charts after.
  if (!shot) {
    return [
      chart((w) => transientModel(null, { width: w })),
      chart((w) => loopsModel(null, { width: w })),
      ...diagram,
    ];
  }

  const config = ((found.node.config || found.record.config || {}).run) || {};
  return [
    shotBlock(shot, found, config),
    chart((w) => transientModel(shot, {
      width: w,
      t0_int_s: values.t0_int_s,
      t_int_width_s: values.t_int_width_s,
      pulse_delay_s: pulseDelayS(shot, values, rig),
      offset_corrected: values.offset_correct,
      dark_reference: values.dark_reference,
    })),
    chart((w) => loopsModel(found, { width: w })),
    ...diagram,
  ];
}

/**
 * The newest shot, as R3·2 lists it beside the trace: Q, the running mean
 * and σ at its point, the peak of each trace, and the digitiser's verdict —
 * with `trigger_sweep` beside it, because *"AUTO plus a charge near zero is
 * worth saying out loud"* (`ui-rules` §9): AUTO sweeps anyway when no
 * trigger arrives, and a loose sync cable then produces a plausible
 * near-zero Q from untriggered noise.
 */
/**
 * The rows themselves, as `[label, value]` pairs — the model beside the DOM,
 * the same split the rail and the charts have.
 *
 * It is these rows, and no longer the chart's readout, that state Q, the
 * running mean and σ: the readout row belongs to the crosshair, and printing
 * the three numbers there as well said them twice on one card, the second time
 * without the shot or the point they belong to. So `ui-rules` §2's rule about
 * the σ that is not a zero is this function's to keep, and
 * `ui/tests/charts.test.mjs` holds it here.
 */
export function shotRows(shot, found) {
  const v = shot.verdict || null;
  const sigma = fmt.sigmaQ(shot.q_std);
  // `StepDone.index` counts from zero; the operator counts shots from one,
  // as the monitor's `shot 4 of 24` and the record's `kept` do.
  const nth = shot.index + 1;
  const rows = [
    ['Q · shot ' + nth, fmt.charge(shot.q)],
    [`running mean · point ${shot.step}`, fmt.charge(shot.q_mean)],
    [`σ · point ${shot.step}`, sigma ? `${sigma} C` : 'σ_Q not recorded'],
  ];
  if (v) {
    rows.push(['peak · light', fmt.amps(v.peak_light_a)]);
    rows.push(['peak · dark', fmt.amps(v.peak_dark_a)]);
  }
  return rows;
}

export function shotBlock(shot, found, config) {
  const v = shot.verdict || null;
  const axis = (found && found.node && found.node.axis) || null;
  const unit = axis ? AXIS_UNITS[axis.name] || '' : '';
  const where = axis && shot.axis_value !== undefined && shot.axis_value !== null
    ? ` · ${axis.name} ${fmt.sig(shot.axis_value, 4)}${unit ? ' ' + unit : ''}`
    : '';
  const level = v && v.level === 'warn' ? 'alert' : v ? 'ok' : '';
  const nearZero = v && Number.isFinite(shot.q) && Number.isFinite(v.peak_light_a)
    && Math.abs(shot.q) < 1e-13;
  const nth = shot.index + 1;
  const rows = shotRows(shot, found);
  const trigger = config.trigger_sweep ? String(config.trigger_sweep).toUpperCase() : null;
  return h('div.shot', { class: level },
    h('div.shot-head',
      h('span.shot-title', { text: `shot ${nth} · loop ${shot.loop}${where}` }),
      shot.clipped ? h('span.tag.bad', { text: 'clipped' }) : null,
      shot.tracesGone ? h('span.tag', { text: 'no traces' }) : null),
    // A definition list, not a table: in the narrow column beside the charts
    // each pair stacks — the label over the number, as R3·2 sets them — and
    // `dt`/`dd` is what a label and its value are.
    h('dl.shot-rows', rows.map(([k, val]) => [h('dt', { text: k }), h('dd.num', { text: val })])),
    v ? h('div', { class: 'shot-verdict ' + level, text: `digitiser · ${v.text}` }) : null,
    trigger ? h('div', {
      class: 'shot-trigger' + (trigger === 'AUTO' ? ' auto' : ''),
      title: trigger === 'AUTO'
        ? 'AUTO sweeps anyway when no trigger arrives: a loose sync cable gives untriggered noise whose dark subtraction cancels to almost nothing'
        : 'TRIG waits for the edge, and a missing trigger becomes a timeout with a message',
      text: `trigger ${trigger}` + (trigger === 'AUTO' && nearZero ? ' · Q near zero under AUTO — check the sync before believing it' : ''),
    }) : null);
}

/**
 * When the field reaches the device on **this shot**, after the trigger: its
 * `:PULS:DEL1` plus the rig's sync-to-field latency. The integration window
 * is measured from there (`experiment/transient.py` `resolve_window`).
 *
 * The service resolves the window every step from the shot's own delay, and
 * along a delay axis — `recipes/run-bace.toml` sweeps exactly that — the
 * form's `delay_ns` is only the value of one point. Taken from the form, the
 * shaded window and the running integral stood still while the real one moved
 * with every point. `trigger_offset_s` is the latency after `:PULS:DEL1`; the
 * generator is never told it.
 */
export function pulseDelayS(shot, values, rig) {
  const setpoint = (shot && shot.setpoint) || {};
  const delayNs = setpoint.delay_ns === undefined || setpoint.delay_ns === null
    ? values.delay_ns
    : setpoint.delay_ns;
  return (Number(delayNs) || 0) * 1e-9 + (Number(rig.trigger_offset_s) || 0);
}

function jvResult(found) {
  const node = found && found.node;
  const curves = ((node && node.curves) || []).slice();
  // The curve being swept, drawn with the finished ones — point by point,
  // as the service streams them (`JVPoint`, live-only).
  if (node && node.partial && node.partial.voltage.length) curves.push(node.partial);
  // As in `baceResult`: `jvModel([])` is an `absent` model, and the frame it
  // carries is the sweep's own, so the slot holds its shape before the run.
  if (!curves.length) return [chart((w) => jvModel([], { width: w }))];
  // The planned sweep range, so the axis does not grow with the curve.
  const jv = found.record && found.record.jv;
  const cfg = jv && jv.config && jv.config.jv;
  const planned = cfg && Number.isFinite(cfg.start_v) && Number.isFinite(cfg.stop_v) ? [cfg.start_v, cfg.stop_v] : null;
  // Two builds on purpose: the chart's is a function of the width the
  // observer measures, and the metrics table beneath it cannot wait for a
  // measurement — it is text, and its numbers do not depend on the width.
  const model = jvModel(curves, { planned });
  const out = [chart((w) => jvModel(curves, { planned, width: w }))];
  if (model.metrics.entries.length) out.push(metricsTable(model.metrics));
  return out;
}

/**
 * Which timing diagrams are open, by module. Per session, not per rebuild:
 * the panel is rebuilt on every edit and every shot, and a fold that closed
 * on each of them would be a fold nobody could keep open.
 */
const openTiming = new Set();

/** `2 alerts · 1 warn` — the counts a shut fold shows, worst level first. */
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
 * The timing diagram behind one line. It is the form drawn (`ui-rules` §7),
 * and the alerts under it are the reason the drawing is on the card at all —
 * but before a run the card is the form, and the drawing is most of its
 * height. Shut, the line still carries the count and the worst level, in
 * that level's colour, so nothing the diagram would have said is silent.
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
 * The metrics, one row per curve — the LED drive voltage (or `dark`) in the
 * first column, the parameters across — labelled derived (`ui-rules` §6:
 * *"J–V metrics are interpolated, not measured."*). `J_sc` carries `in amps`
 * in its header because it is: it is interpolated from `current`, whatever
 * area the run was given, so it is the one number on this card the mA/cm²
 * rule does not reach.
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
    h('p.chart-note', { text: metrics.note }));
}
