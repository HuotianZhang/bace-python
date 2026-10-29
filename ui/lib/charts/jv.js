// The J–V chart uses `density` in mA/cm² without conversion. With no device area,
// `density` is `null` and the axis uses A. `metrics.jsc` remains in A because it is
// interpolated from `current`, so it cannot sit on a density axis
// (`docs/service-contract.md` §6). Metrics are labelled derived. The −4 V start
// remains visible: cropping the reverse arm hides the shunt even though it occupies
// four times the power quadrant (`docs/ui-rules.md` §§4, 6).

import * as scale from '../scale.js';
import * as fmt from '../format.js';
import { layout, axisTicks, heightFor, ASPECT } from './frame.js';

/**
 * The illumination ramp darkens monotonically from dim to bright so levels remain
 * ordered in greyscale and print. The reference palette's OKLab lightnesses, measured
 * on this bench, are 0.78, 0.68, 0.78, 0.68 and 0.48, which collapse five levels into
 * three. This ramp spans 0.78 to 0.48 without repeating lightness.
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
  // All curves share one quantity and unit. If any curve lacks density because its
  // area is absent, every curve uses A; mixing A and mA/cm² on one scale is invalid.
  const all = curves.length > 0 && curves.every((c) => Array.isArray(c.density) && c.density.length);
  return all
    ? { key: 'density', label: 'J / mA cm⁻²', magnitude: '|J| / mA cm⁻²', format: (v) => fmt.density(v), unit: 'mA/cm²' }
    : { key: 'current', label: 'I / A', magnitude: '|I| / A', format: (v) => fmt.amps(v), unit: 'A' };
}

export function jvModel(curves, options = {}) {
  // `planned` supplies the requested voltage limits so incoming points fill a stable
  // frame rather than continually extending the axis.
  const { width = 530, height = null, log = null, planned = null } = options;
  // A J–V uses `curve` aspect because the power quadrant and reverse-bias slope are
  // read by shape.
  const panelSpec = [{ key: 'jv', weight: 1 }];
  const jvMargin = { left: 58, bottom: 28, top: 18 };
  const jvHeight = height ?? heightFor(width, panelSpec, { margin: jvMargin, aspect: ASPECT.curve });
  const list = (curves || []).filter((c) => c && Array.isArray(c.voltage) && c.voltage.length);
  if (!list.length) {
    return {
      key: 'jv',
      // The empty slot reserves the sweep's panel and voltage axis to avoid a layout
      // change at the first point. Its y label stays generic before the run supplies
      // an area and therefore a unit.
      absent: {
        text: 'No curves yet',
        detail: null,
        frame: {
          width,
          height: jvHeight,
          panels: [{ key: 'jv', weight: 1, label: 'I  ·  V' }],
          margin: jvMargin,
          xLabel: 'V / V',
        },
      },
      panels: [],
      notes: [],
    };
  }
  const quantity = currentOf(list);
  const values = list.map((c) => c[quantity.key]);

  // A fully dark sweep uses a logarithmic leakage axis. `dark` can be `true`, `false`
  // or `null`; an unreadable illumination state must not select a dark axis for a
  // curve that can contain photocurrent.
  const allDark = list.every((c) => c.dark === true);
  const useLog = log === null ? allDark : log;

  // Ramp position encodes LED level, so both directions at one level share a colour.
  // Ranking curves separately would imply different illumination. Dashes distinguish
  // the return sweep.
  const light = list.filter((c) => c.dark !== true);
  const levels = [...new Set(light.map((c) => c.led_level_v ?? null))]
    .sort((a, b) => (a ?? 0) - (b ?? 0));

  const frame = layout({ width, height: jvHeight, panels: panelSpec, margin: jvMargin });
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
    // The y window crops forward injection, which can reach hundreds of mA/cm² and
    // flatten the power quadrant on a full scale. The x axis retains the −4 V start
    // to preserve the reverse arm (`docs/ui-rules.md` §4).
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
    // The incomplete sweep shares its completed curve's colour but uses dots to
    // indicate progress. The last-point marker identifies the instrument's current
    // position.
    const partial = curve.partial === true;
    // Forward and reverse sweeps share a label but need distinct keys. Duplicate
    // `data-series` values would make the crosshair's first match show one
    // direction's value under the other's colour.
    series.push({
      key: `${label}·${curve.direction || 'forward'}${partial ? '·partial' : ''}`,
      label: partial ? `${label} · sweeping` : label,
      colour,
      width: 1.5,
      // The return leg is a separate sweep on a device exposed to the forward sweep.
      // A continuous line would make hysteresis look like noise.
      dash: partial ? '2 3' : curve.direction === 'reverse' ? '4 2' : null,
      // `Math.abs(null)` produces finite zero, which would invent a leakage point on
      // the log floor. Only numeric samples take the absolute value; `linePath`
      // breaks at missing samples.
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
    // Direct labels fit at most four curves; larger sets use the legend. Labels sit
    // at the sweep start where curves differ by J_sc. Forward injection crowds their
    // ends and can lie above the y window.
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
    // Incomplete sweeps have no metrics because a partial curve cannot support the
    // completed sweep's V_oc.
    metrics: metricsOf(list.filter((c) => c.partial !== true), quantity),
    notes: notes(list, quantity, { useLog, clamped, allDark, cropped: Y.cropped || 0 }),
  };
}

/**
 * The helper selects the J–V y window around the power quadrant. It uses the curve's
 * magnitude at 0 V because `metrics.jsc` is in A and would require an area to compare
 * with a density axis.
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
  // Headroom exposes injection before clipping and leaves room for the reverse-bias
  // slope. A wider window compresses the curve into the panel's top third; a narrower
  // one hides the shunt.
  return [-1.6 * reference, 0.6 * reference];
}

function countOutside(values, [lo, hi]) {
  let n = 0;
  for (const list of values) {
    for (const v of list || []) if (Number.isFinite(v) && (v < lo || v > hi)) n += 1;
  }
  return n;
}

/** The helper returns absolute values for numbers and `null` for missing samples. */
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
 * The metrics panel labels interpolated values as derived. `jsc` retains A because it
 * comes from `current`, not `density`; reporting density would require an area the
 * run may lack (`docs/service-contract.md` §6; `docs/ui-rules.md` §6).
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
  return { entries: out, note: 'Derived: interpolated from the curve' };
}

/** Maximum-power-point values use readable prefixes, such as µW for a small cell. */
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
