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
 * The signature the result panel is rebuilt on.
 *
 * Everything that can change what is drawn, and nothing that cannot. A shot's
 * identity is its node, its numbering and its arrival — not its arrays, which
 * are 800 numbers whose JSON would cost more than the redraw it is guarding.
 */
export function resultKey(name, entry, record, bench) {
  const parts = [name];
  if (name === 'bace') {
    parts.push(JSON.stringify(valuesOf(entry)));
    parts.push(JSON.stringify(((bench && bench.chain && bench.chain.items) || [])
      .map((item) => `${item.key}=${item.value}`)));
    parts.push(JSON.stringify((bench && bench.rig && bench.rig.values) || null));
  }
  if (!record) return parts.join('|');
  parts.push(record.run_id, record.state || '');
  const shot = record.lastShot;
  if (shot) parts.push(`${shot.node_path}:${shot.loop}:${shot.index}:${shot.ts}:${shot.tracesGone ? 'gone' : 'traces'}`);
  if (record.curves.length) {
    const last = record.curves[record.curves.length - 1];
    parts.push(`curves:${record.curves.length}:${last.label}:${last.ts}`);
  }
  return parts.join('|');
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
