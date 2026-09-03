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

/** Which cards have a result panel at all. */
export const RESULT_CARDS = new Set(['bace', 'jv', 'jv_bace']);

/** The newest run of this module that has anything to draw. */
export function runFor(state, name) {
  for (let i = state.order.length - 1; i >= 0; i -= 1) {
    const record = state.runs[state.order[i]];
    if (!record || record.module !== name) continue;
    if (record.curves.length || record.shots.length) return record;
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
export function resultKeys(name, entry, record, bench) {
  const form = [name];
  if (name === 'bace') {
    form.push(JSON.stringify(valuesOf(entry)));
    form.push(JSON.stringify(((bench && bench.chain && bench.chain.items) || [])
      .map((item) => `${item.key}=${item.value}`)));
    form.push(JSON.stringify((bench && bench.rig && bench.rig.values) || null));
  }
  const data = [];
  if (record) {
    data.push(record.run_id, record.state || '');
    const shot = record.lastShot;
    if (shot) data.push(`${shot.node_path}:${shot.loop}:${shot.index}:${shot.ts}:${shot.tracesGone ? 'gone' : 'traces'}`);
    if (record.curves.length) {
      const last = record.curves[record.curves.length - 1];
      data.push(`curves:${record.curves.length}:${last.label}:${last.ts}`);
    }
  }
  return { form: form.join('|'), data: data.join('|') };
}

/** Both halves as one string — what `dom.keyed` is given. */
export function resultKey(name, entry, record, bench) {
  const keys = resultKeys(name, entry, record, bench);
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
export function resultPanel(name, { entry, record, bench }) {
  if (name === 'bace') return baceResult(entry, record, bench);
  if (name === 'jv' || name === 'jv_bace') return jvResult(record);
  return [];
}

function baceResult(entry, record, bench) {
  const values = valuesOf(entry);
  const rig = (bench && bench.rig && bench.rig.values) || {};
  const chain = (bench && bench.chain) || {};
  const timing = timingModel(values, { rig, chain });
  const out = [chart(timing)];
  if (timing.alerts.length) out.push(alertList(timing.alerts));

  const shot = record && record.lastShot;
  if (shot) {
    out.push(chart(transientModel(shot, {
      t0_int_s: values.t0_int_s,
      t0_int_reference: values.t0_int_reference,
      pulse_delay_s: (Number(values.delay_ns) || 0) * 1e-9,
      offset_corrected: values.offset_correct,
      dark_reference: values.dark_reference,
    })));
  } else {
    out.push(h('p.absent', 'no shot yet — the transient appears with the first one'));
  }
  return out;
}

function jvResult(record) {
  const curves = (record && record.curves) || [];
  if (!curves.length) return [h('p.absent', 'no curves yet — a sweep draws here as each one finishes')];
  const model = jvModel(curves);
  const out = [chart(model)];
  if (model.metrics.entries.length) out.push(metricsTable(model.metrics));
  return out;
}

function alertList(alerts) {
  return h('div.alerts', alerts.map((a) => h('div.warn1.' + a.level,
    h('span.code', { text: a.level }), h('span', { text: a.text }))));
}

/**
 * The metrics, labelled derived — `ui-rules` §6: *"J–V metrics are
 * interpolated, not measured."* And `J_sc` says `in amps` because it is: it is
 * interpolated from `current`, whatever area the run was given, so it is the
 * one number on this card the mA/cm² rule does not reach.
 */
function metricsTable(metrics) {
  return h('div.metrics',
    h('table.rows', metrics.entries.map((entry) => [
      h('tr', h('th', { colspan: 2, text: entry.label })),
      ...entry.rows.map((row) => h('tr',
        h('td.l', { text: row.key }),
        h('td.num', { text: row.value + (row.note ? ` · ${row.note}` : '') }))),
    ])),
    h('p.chart-note', { text: metrics.note }));
}
