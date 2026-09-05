// The J–V sweep: `ch-jv` and `ch-jvdark`, as one component.
//
// Almost everything here is a unit decision, because that is where this plot
// goes wrong:
//
//   * **`density` is mA/cm² on the wire.** The factor of a thousand belongs to
//     the quantity and is applied once, where the pixel area is
//     (`experiment.jv.current_density`), so this chart converts nothing and
//     picks no prefix. A client that applies it again is out by a thousand in
//     the other direction (`docs/ui-plan.md`, what M0 taught M3).
//   * **`pixel_area_cm2 = 0` means there is no density.** The service sends
//     `null` and the axis becomes amps, rather than defaulting to 1 cm² and
//     mislabelling A as mA/cm² (`ui-rules` §6).
//   * **`metrics.jsc` is in amps**, not a density — it is interpolated from
//     `current`, whatever area the run was given. It is a number beside the
//     chart, never a point on the mA/cm² axis.
//   * **the metrics are interpolated, not measured**, so they say so.
//
// And one layout decision: **the −4 V start is a layout problem, not a
// cropping problem** (`ui-rules` §4). The real sweep runs from −4 V, so the
// reverse arm is four times the power quadrant, and an axis that starts at
// −0.2 V because that is where the interesting part is has thrown away the
// part that shows the shunt.

import * as scale from '../scale.js';
import * as fmt from '../format.js';
import { layout, axisTicks } from './frame.js';

/**
 * The illumination ramp: dim → bright, light → dark, one hue.
 *
 * The design's five values are `#bab6b6 · #9b9797 · #ff9783 · #ff563c ·
 * #ae1800`, a grey pair spliced to a red trio. Measured in OKLab their
 * lightnesses are 0.78, 0.68, 0.78, 0.68, 0.48 — the first and third are the
 * *same* step, and so are the second and fourth, so five levels cannot be
 * ordered by eye and collapse into three in greyscale or in print. A
 * sequential encoding has one job, which is to be ordered, so this is the red
 * half of the same ramp continued: strictly darkening, 0.78 → 0.48.
 */
export const RAMP = ['#ff9783', '#ff563c', '#ec3013', '#cc2408', '#ae1800'];

export function ramp(i, n) {
  if (n <= 1) return RAMP[RAMP.length - 2];
  const at = (i / (n - 1)) * (RAMP.length - 1);
  const lo = Math.floor(at);
  const hi = Math.min(RAMP.length - 1, lo + 1);
  return mix(RAMP[lo], RAMP[hi], at - lo);
}

function mix(a, b, t) {
  if (t <= 0) return a;
  if (t >= 1) return b;
  const pa = parseInt(a.slice(1), 16);
  const pb = parseInt(b.slice(1), 16);
  const ch = (shift) => {
    const va = (pa >> shift) & 255;
    const vb = (pb >> shift) & 255;
    return Math.round(va + (vb - va) * t);
  };
  return '#' + [ch(16), ch(8), ch(0)].map((v) => v.toString(16).padStart(2, '0')).join('');
}

/** Which array a curve is plotted from, and what the axis is then called. */
export function currentOf(curves) {
  // Mixed is not a chart. One curve in mA/cm² and one in A on the same axis
  // would be two quantities sharing a scale, so if any curve has no density —
  // which is what a run with no pixel area sends — every curve is drawn in
  // amps and the axis says so.
  const all = curves.length > 0 && curves.every((c) => Array.isArray(c.density) && c.density.length);
  return all
    ? { key: 'density', label: 'J / mA cm⁻²', magnitude: '|J| / mA cm⁻²', format: (v) => fmt.density(v), unit: 'mA/cm²' }
    : { key: 'current', label: 'I / A', magnitude: '|I| / A', format: (v) => fmt.amps(v), unit: 'A' };
}

export function jvModel(curves, options = {}) {
  // `planned` is the sweep's [start, stop]: with it the x axis is the range
  // asked for, so a curve drawn point by point grows into a fixed frame
  // instead of the frame growing with it.
  const { width = 530, height = 260, log = null, planned = null } = options;
  const list = (curves || []).filter((c) => c && Array.isArray(c.voltage) && c.voltage.length);
  if (!list.length) {
    return {
      key: 'jv',
      // The slot holds the sweep's own shape before the sweep: one panel over
      // the voltage axis, so the card does not change geometry when the first
      // point lands. The y label is generic because whether it is A or
      // mA cm⁻² depends on a pixel area this run has not declared yet.
      absent: {
        text: 'no curves yet — a sweep draws here point by point',
        detail: null,
        frame: {
          width,
          height,
          panels: [{ key: 'jv', weight: 1, label: 'I  ·  V' }],
          margin: { left: 58, bottom: 28, top: 18 },
          xLabel: 'V / V',
        },
      },
      panels: [],
      notes: [],
    };
  }
  const quantity = currentOf(list);
  const values = list.map((c) => c[quantity.key]);

  // A sweep that is dark all through is a leakage measurement, and leakage is
  // read over decades: that is what `ch-jvdark` draws. `dark` is `true`,
  // `false` or **`null`** — a `jv` node sweeps under whatever light it finds
  // and labels the curve from a read-back, so `null` is "the bench could not
  // say", which is not the same as "not dark" and must not silently pick the
  // log axis for a curve that may have a photocurrent in it.
  const allDark = list.every((c) => c.dark === true);
  const useLog = log === null ? allDark : log;

  // The ramp encodes the **illumination level**, so it is ranked by level and
  // not by curve. With `both_directions` a level produces two curves, and one
  // slot each made a single level's forward and reverse arms the brightest and
  // the darkest of the ramp — two illuminations that never existed. The
  // reverse arm is already told apart by its dash.
  const light = list.filter((c) => c.dark !== true);
  const levels = [...new Set(light.map((c) => c.led_level_v ?? null))]
    .sort((a, b) => (a ?? 0) - (b ?? 0));

  const frame = layout({
    width, height, panels: [{ key: 'jv', weight: 1 }], margin: { left: 58, bottom: 28, top: 18 },
  });
  const panel = frame.panels[0];
  const rect = panel.rect;

  const xDomain = scale.extent([list.map((c) => c.voltage).flat().concat(
    Array.isArray(planned) && planned.length === 2 && planned.every(Number.isFinite) ? planned : [])],
  { pad: 0.02 }) || [-1, 1];
  const X = scale.linear(xDomain, [rect.x, rect.x + rect.w]);

  let Y;
  let clamped = 0;
  if (useLog) {
    const magnitudes = values.flat().filter((v) => Number.isFinite(v)).map(Math.abs);
    const floor = 10 ** Math.floor(Math.log10(Math.max(1e-12, quantile(magnitudes, 0.02))));
    clamped = magnitudes.filter((v) => v <= floor).length;
    const top = 10 ** Math.ceil(Math.log10(Math.max(floor * 10, ...magnitudes)));
    Y = scale.log10([floor, top], [rect.y + rect.h, rect.y], { floor });
  } else {
    const full = scale.extent(values, { includeZero: true, pad: 0.08 }) || [-1, 1];
    const framed = powerQuadrant(list, quantity.key);
    // **Crop in y, never in x.** Forward injection past V_oc runs to hundreds
    // of mA/cm² and, given the whole axis, flattens the power quadrant — the
    // part of a J–V anyone reads — into a line on the zero. The design crops
    // the same way and says so with a clip path; what must not be cropped is
    // the −4 V start, which is a layout problem (`ui-rules` §4) and is on x.
    const cropped = framed && (full[1] - full[0]) > (framed[1] - framed[0]) * 2;
    Y = scale.linear(cropped ? framed : full, [rect.y + rect.h, rect.y]);
    Y.cropped = cropped ? countOutside(values, framed) : 0;
  }

  const series = [];
  const legend = [];
  const marks = [];
  const dots = [];
  for (const curve of list) {
    const y = curve[quantity.key];
    if (!Array.isArray(y) || !y.length) continue;
    const colour = curve.dark === true
      ? 'ink'
      : ramp(levels.indexOf(curve.led_level_v ?? null), Math.max(1, levels.length));
    const label = curveLabel(curve);
    // The sweep in flight (`JVPoint`s folded by the store): the same colour
    // as the curve it will become, dotted so it reads as unfinished, and a
    // dot on the last point read, which is where the instrument is now.
    const partial = curve.partial === true;
    // The **key** is the identity, and forward and reverse at one level share
    // a label: keyed on that, the renderer built two crosshair dots with the
    // same `data-series`, `attachCursor`'s `find` updated only the first, and
    // it carried the reverse arm's value under the forward arm's colour.
    series.push({
      key: `${label}·${curve.direction || 'forward'}${partial ? '·partial' : ''}`,
      label: partial ? `${label} · sweeping` : label,
      colour,
      width: 1.5,
      // The return leg is a **second sweep**, not the continuation of the
      // first: it is measured after the forward one, on a device the forward
      // one has just been through. Drawn as one unbroken line the hysteresis
      // reads as noise.
      dash: partial ? '2 3' : curve.direction === 'reverse' ? '4 2' : null,
      // `Math.abs(null)` is **0**, and 0 is finite: mapped straight, a sample
      // the instrument never returned became a point sitting on the log
      // floor with the curve drawn through it — a leakage measurement out of
      // an absence. `linePath` lifts the pen for anything non-finite, so the
      // absolute value is taken only where there is a value to take it of.
      d: scale.linePath((i) => X(curve.voltage[i]), useLog ? y.map(magnitude) : y, (v) => v, Y),
      at: (v) => {
        const i = nearestVoltage(curve.voltage, v);
        if (i < 0 || !Number.isFinite(y[i])) return null;
        return { y: useLog ? Math.abs(y[i]) : y[i] };
      },
      format: quantity.format,
    });
    legend.push({
      label: partial ? `${label} · sweeping ${curve.k || y.length} of ${curve.of || '?'}`
        : curve.direction === 'reverse' ? `${label} · reverse` : label,
      colour, width: 1.6, dash: partial ? '2 3' : curve.direction === 'reverse' ? '4 2' : null,
    });
    if (partial) {
      const last = y.length - 1;
      const yv = useLog ? Math.abs(y[last]) : y[last];
      if (Number.isFinite(curve.voltage[last]) && Number.isFinite(yv)) {
        dots.push({ x: X(curve.voltage[last]), y: Y(yv), colour, r: 3 });
      }
    }
    // Direct labels, on four curves or fewer: past that they collide with
    // each other and the legend is the honest place for identity. At the
    // sweep's **start**, where the curves are a whole J_sc apart — at the
    // finish they are all in forward injection, on top of one another, and
    // half of them are off the top of a cropped axis.
    if (list.length <= 4) {
      marks.push({
        x: X(curve.voltage[0]) + 5,
        y: Math.min(rect.y + rect.h - 4,
          Math.max(rect.y + 9, Y(useLog ? Math.abs(y[0]) : y[0]) - 5)),
        text: label, colour, anchor: 'start', weight: 600, size: 9,
      });
    }
  }

  const rules = [];
  if (!useLog && Y.domain[0] <= 0 && Y.domain[1] >= 0) {
    rules.push({ x1: rect.x, y1: Y(0), x2: rect.x + rect.w, y2: Y(0), colour: 'rule', width: 1 });
  }
  if (X.domain[0] <= 0 && X.domain[1] >= 0) {
    rules.push({ x1: X(0), y1: rect.y, x2: X(0), y2: rect.y + rect.h, colour: 'rule', width: 1 });
  }

  return {
    key: 'jv',
    width: frame.width,
    height: frame.height,
    margin: frame.margin,
    panels: [{
      ...panel,
      label: `${useLog ? quantity.magnitude : quantity.label}  ·  V / V →`,
      note: sweepNote(list, xDomain),
      y: { scale: Y, ticks: axisTicks(Y, { count: 4 }) },
      series, rules, marks, dots,
    }],
    x: { scale: X, ticks: axisTicks(X, { count: 6 }), label: 'V / V', format: (v) => fmt.volts(v, { decimals: 3 }) },
    legend: legend.length > 1 ? legend : [],
    readout: '',
    // No metrics for the sweep in flight: a V_oc interpolated on half a
    // curve is a number nobody measured.
    metrics: metricsOf(list.filter((c) => c.partial !== true), quantity),
    notes: notes(list, quantity, { useLog, clamped, allDark, cropped: Y.cropped || 0 }),
  };
}

/**
 * The window a J–V is read in: a couple of J_sc below zero and a little above.
 *
 * Taken from the curve itself — |J| where the sweep crosses 0 V — rather than
 * from `metrics.jsc`, which is in amps and would need the area back to be
 * comparable with a density axis.
 */
function powerQuadrant(list, key) {
  let reference = 0;
  for (const curve of list) {
    const y = curve[key];
    if (!Array.isArray(y)) continue;
    const i = nearestVoltage(curve.voltage, 0);
    if (i >= 0 && Number.isFinite(y[i])) reference = Math.max(reference, Math.abs(y[i]));
  }
  if (!(reference > 0)) return null;
  // A little over one J_sc of headroom below, and enough above for the
  // injection to be seen turning up before it is clipped. Wider than this and
  // a single curve sits in the top third of an empty panel; narrower and the
  // reverse-bias slope — the shunt — has nowhere to show.
  return [-1.6 * reference, 0.6 * reference];
}

function countOutside(values, [lo, hi]) {
  let n = 0;
  for (const list of values) {
    for (const v of list || []) if (Number.isFinite(v) && (v < lo || v > hi)) n += 1;
  }
  return n;
}

/** `|v|`, and `null` for anything that was not a number — see the curve's `d`. */
function magnitude(v) {
  return Number.isFinite(v) ? Math.abs(v) : null;
}

function quantile(sorted, q) {
  const values = [...sorted].filter((v) => v > 0).sort((a, b) => a - b);
  if (!values.length) return 1e-12;
  return values[Math.min(values.length - 1, Math.floor(values.length * q))];
}

function nearestVoltage(voltage, v) {
  let best = -1;
  let gap = Infinity;
  for (let i = 0; i < voltage.length; i += 1) {
    const d = Math.abs(voltage[i] - v);
    if (d < gap) { gap = d; best = i; }
  }
  return best;
}

export function curveLabel(curve) {
  if (curve.label) return curve.label;
  if (curve.dark === true) return 'dark';
  if (curve.led_level_v !== null && curve.led_level_v !== undefined) return `${fmt.volts(curve.led_level_v, { decimals: 3 })}`;
  return curve.illumination || 'curve';
}

function sweepNote(list, xDomain) {
  const from = Math.min(...list.map((c) => c.voltage[0]));
  const done = list.filter((c) => c.partial !== true);
  const parts = [`${done.length} curve${done.length === 1 ? '' : 's'}`];
  const live = list.find((c) => c.partial === true);
  if (live) parts.push(`sweeping ${curveLabel(live)} · point ${live.k || live.voltage.length} of ${live.of || '?'}`);
  if (xDomain[0] < -1) parts.push(`sweep starts at ${fmt.volts(from, { decimals: 2 })}`);
  if (list.some((c) => c.direction === 'reverse')) parts.push('reverse arm dashed — a second sweep');
  return parts.join(' · ');
}

/**
 * The interpolated metrics, labelled as what they are.
 *
 * `jsc` keeps its amps. It is the one number here the mA/cm² rule does not
 * reach, because it is interpolated from `current` rather than from `density`,
 * and printing it as a density would be inventing an area the run may not have
 * been given (`ui-rules` §6, `docs/service-contract.md` §`/runs/{id}/data`).
 */
export function metricsOf(list, quantity) {
  const out = [];
  for (const curve of list) {
    const m = curve.metrics || {};
    if (m.voc === null && m.jsc === null && m.p_max === null) continue;
    out.push({
      label: curveLabel(curve),
      rows: [
        { key: 'V_oc', value: m.voc === null || m.voc === undefined ? fmt.ABSENT : fmt.volts(m.voc) },
        { key: 'J_sc', value: m.jsc === null || m.jsc === undefined ? fmt.ABSENT : fmt.amps(m.jsc), note: 'in amps' },
        { key: 'FF', value: m.fill_factor === null || m.fill_factor === undefined
          ? fmt.ABSENT : fmt.sig(m.fill_factor * 100, 3) + ' %' },
        { key: 'P_mpp', value: watts(m.p_max) },
        { key: 'V_mpp', value: m.v_mpp === null || m.v_mpp === undefined ? fmt.ABSENT : fmt.volts(m.v_mpp) },
      ],
    });
  }
  return { entries: out, note: 'derived · interpolated from the curve, not measured points' };
}

/** A power at the maximum power point: µW for a small cell, not `0.000148 W`. */
function watts(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return fmt.ABSENT;
  if (value === 0) return '0 W';
  const [scaled, prefix] = fmt.prefixed(value);
  return `${fmt.sig(scaled, 3)} ${prefix}W`;
}

function notes(list, quantity, { useLog, clamped, allDark, cropped = 0 }) {
  const out = [];
  if (quantity.key === 'current') {
    out.push('no pixel area on this run, so the axis is amps — a density would be inventing one');
  }
  if (cropped) {
    out.push(`the axis is the power quadrant · ${cropped} sample${cropped === 1 ? '' : 's'} `
      + 'of forward injection above it, clipped rather than compressed away');
  }
  if (useLog) {
    out.push('|J| on a log axis: a dark sweep is read over decades');
    if (clamped) out.push(`${clamped} sample${clamped === 1 ? '' : 's'} at or below the floor, clamped to it`);
  }
  if (!allDark && list.some((c) => c.dark === null || c.dark === undefined)) {
    out.push('a curve whose illumination the bench could not read back — labelled from the read-back, and it said nothing');
  }
  const levels = list.map((c) => c.led_level_v).filter((v) => v !== null && v !== undefined);
  if (levels.length > 1) {
    out.push(`sequential ${fmt.volts(Math.min(...levels), { decimals: 3 })} → ${fmt.volts(Math.max(...levels), { decimals: 3 })} at the LED`);
  }
  return out;
}
