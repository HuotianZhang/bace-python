// The power chart shows readings against the clock over a selected window.
// `PowerReading` and `GET /monitors/power/history` share `ts`, allowing
// duplicate-free merging (`docs/service-contract.md` §8). Values are watts at the
// meter, not irradiance (`docs/ui-rules.md` §2). Saturated and overrange readings
// remain as alert-colour dots so clipping cannot disappear from a long measurement.

import * as scale from '../scale.js';
import * as fmt from '../format.js';
import { layout, axisTicks, heightFor, ASPECT, PLOT_MARGIN } from './frame.js';
import { ranged } from './view.js';

/** The windows the panel offers. `seconds: null` is the whole log. */
export const WINDOWS = [
  { key: '1m', label: '1 min', seconds: 60 },
  { key: '5m', label: '5 min', seconds: 300 },
  { key: '30m', label: '30 min', seconds: 1800 },
  { key: '2h', label: '2 h', seconds: 7200 },
  { key: 'all', label: 'all', seconds: null },
];

/** The first index whose `ts` is at least `ts`, in a log sorted by `ts`. */
export function lowerBound(points, ts) {
  let lo = 0;
  let hi = points.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid].ts < ts) lo = mid + 1; else hi = mid;
  }
  return lo;
}

/**
 * The helper selects readings in a window ending at the current time; `seconds: null`
 * selects the complete log.
 */
export function windowPoints(points, { seconds, now }) {
  if (!points || !points.length) return [];
  if (seconds === null || seconds === undefined) return points;
  return points.slice(lowerBound(points, now - seconds));
}

/**
 * The statistics describe the selected window and count readings the meter marks
 * untrustworthy. `std` is `null` for fewer than two readings because sample deviation
 * requires two.
 */
export function powerStats(points) {
  const n = points.length;
  if (!n) return { n: 0, mean: null, min: null, max: null, std: null, untrustworthy: 0, span_s: 0 };
  let sum = 0;
  let min = Infinity;
  let max = -Infinity;
  let untrustworthy = 0;
  for (const p of points) {
    sum += p.watts;
    if (p.watts < min) min = p.watts;
    if (p.watts > max) max = p.watts;
    if (p.trustworthy === false) untrustworthy += 1;
  }
  const mean = sum / n;
  let ss = 0;
  for (const p of points) ss += (p.watts - mean) ** 2;
  return {
    n, mean, min, max,
    std: n > 1 ? Math.sqrt(ss / (n - 1)) : null,
    untrustworthy,
    span_s: points[n - 1].ts - points[0].ts,
  };
}

/** Seconds, minutes or hours for the axis, by the span it has to label. */
export function timeUnit(spanS) {
  if (spanS <= 180) return { div: 1, unit: 's' };
  if (spanS <= 3 * 3600) return { div: 60, unit: 'min' };
  return { div: 3600, unit: 'h' };
}

/** The SI prefix the whole window is drawn in, from its largest reading. */
export function wattsUnit(points) {
  let top = 0;
  for (const p of points) if (Number.isFinite(p.watts) && Math.abs(p.watts) > top) top = Math.abs(p.watts);
  if (top === 0) return { factor: 1, prefix: '' };
  const [value, prefix] = fmt.prefixed(top);
  return { factor: top / value, prefix };
}

/**
 * The model plots the selected window against elapsed time before the current
 * reading. `fromZero` makes an unlit lamp visibly zero; a fitted y window instead
 * exposes small drift, such as 1 %, in a settled LED.
 */
export function powerModel(points, {
  seconds = null, now, width = 1400, height = null, fromZero = false, columns = null, view = null,
} = {}) {
  const at = now !== undefined ? now : Date.now() / 1000;
  const inWindow = windowPoints(points, { seconds, now: at });
  if (!inWindow.length) {
    return {
      key: 'power', panels: [], notes: [],
      absent: {
        text: seconds === null || !points.length ? 'no readings yet' : `nothing in the last ${fmt.duration(seconds)}`,
        detail: points.length ? null : 'switch the monitor on, and the meter is read at the interval beside it',
      },
    };
  }
  const span = seconds !== null && seconds !== undefined
    ? seconds
    : Math.max(1, at - inWindow[0].ts);
  const { div, unit } = timeUnit(span);
  const { factor, prefix } = wattsUnit(inWindow);
  const stats = powerStats(inWindow);

  // The meter's whole window at a glance: a `strip`, where the excursions
  // and the ends carry the meaning and the middle is a baseline.
  const pPanel = [{ key: 'power', weight: 1 }];
  const frame = layout({
    width, panels: pPanel, margin: PLOT_MARGIN,
    height: height ?? heightFor(width, pPanel, { margin: PLOT_MARGIN, aspect: ASPECT.strip }),
  });
  const panel = frame.panels[0];
  const rect = panel.rect;
  const X = scale.linear([-span / div, 0], [rect.x, rect.x + rect.w]);
  const xs = inWindow.map((p) => (p.ts - at) / div);
  const ys = inWindow.map((p) => p.watts / factor);
  const domain = scale.extent([ys], { includeZero: fromZero, pad: 0.08 }) || [0, 1];
  const Y = scale.linear(ranged(view, 'power', `${prefix}W`, domain), [rect.y + rect.h, rect.y]);
  const cols = columns || Math.round(rect.w);
  const nearest = (xValue) => {
    const ts = at + xValue * div;
    const i = lowerBound(inWindow, ts);
    if (i <= 0) return 0;
    if (i >= inWindow.length) return inWindow.length - 1;
    return ts - inWindow[i - 1].ts <= inWindow[i].ts - ts ? i - 1 : i;
  };
  const dots = [];
  for (let i = 0; i < inWindow.length; i += 1) {
    if (inWindow[i].trustworthy === false) {
      dots.push({ x: scale.fixed(X(xs[i])), y: scale.fixed(Y(ys[i])), colour: 'alert', r: 2.2 });
    }
  }
  const built = {
    ...panel,
    label: `optical power at the meter  ·  P / ${prefix}W`,
    note: `${stats.n} reading${stats.n === 1 ? '' : 's'} · ${fmt.duration(stats.span_s)}`
      + (stats.untrustworthy ? ` · ${stats.untrustworthy} flagged by the meter` : ''),
    noteColour: stats.untrustworthy ? 'alert' : 'grey',
    y: { scale: Y, ticks: axisTicks(Y, { count: 4 }), title: `Power (${prefix}W)`, unit: `${prefix}W` },
    series: [{
      key: 'power', label: 'P', colour: 'ink', width: 1.3,
      d: inWindow.length > cols * 2
        ? scale.envelopePath(ys, (i) => X(xs[i]), Y, { columns: cols })
        : scale.linePath(xs, ys, X, Y),
      at: (xValue) => {
        const i = nearest(xValue);
        return i < 0 ? null : { y: ys[i] * factor, ts: inWindow[i].ts };
      },
      format: (v) => fmt.intensity(v),
    }],
    dots,
  };
  return {
    key: 'power',
    width: frame.width,
    height: frame.height,
    margin: frame.margin,
    panels: [built],
    x: {
      scale: X,
      ticks: axisTicks(X, { count: 6 }),
      label: `${unit} before now`,
      title: `Time before now (${unit})`,
      unit,
      // The panel's window menu sets this axis.
      fixed: true,
      format: (v) => `${ago(-v)} ${unit} ago`,
    },
    legend: dots.length ? [{ label: 'saturated or overrange, as the meter flagged it', colour: 'alert', marker: 'dot' }] : [],
    readout: statsLine(stats),
    notes: [],
    stats,
    unit: { factor, prefix, div, timeUnit: unit },
  };
}

/** `2 min ago`, `0.5 min ago`: the crosshair's time, without trailing zeros. */
function ago(v) {
  const abs = Math.abs(v);
  return String(Number(abs.toFixed(abs >= 10 ? 0 : abs >= 1 ? 1 : 2)));
}

/**
 * The caption reports mean, minimum, maximum, sample deviation and reading count for
 * the selected window.
 */
export function statsLine(stats) {
  if (!stats || !stats.n) return '';
  const [, prefix] = fmt.prefixed(Math.abs(stats.mean) || 1);
  const factor = stats.mean === 0 ? 1 : Math.abs(stats.mean) / fmt.prefixed(Math.abs(stats.mean))[0];
  const inUnit = (w) => fmt.sig(w / factor, 3);
  const parts = [`mean ${inUnit(stats.mean)} ${prefix}W`, `min ${inUnit(stats.min)}`, `max ${inUnit(stats.max)}`];
  if (stats.std !== null) {
    const rel = stats.mean ? Math.abs(stats.std / stats.mean) * 100 : null;
    parts.push(`σ ${inUnit(stats.std)} ${prefix}W${rel !== null ? ` (${fmt.sig(rel, 2)} %)` : ''}`);
  }
  parts.push(`${stats.n} reading${stats.n === 1 ? '' : 's'}`);
  return parts.join('  ·  ');
}
