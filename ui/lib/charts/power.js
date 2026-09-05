// The live power trace — the meter's readings against the clock, the one
// chart in the console whose x axis is *now* rather than a record.
//
// It is the R3·2 power sparkline grown into what the 1918-C's own console
// shows: a strip chart of the last minute, five minutes, or the whole trace,
// the number beside it, and the statistics over the window. Every reading is
// a `PowerReading` frame — the monitor's, at the interval the switch chose —
// and the store keeps them in order by the frame's `ts` (`powerLog`). The
// history the service holds (`GET /monitors/power/history`) carries the same
// `ts`, so the two merge without a duplicate.
//
// Watts at the meter, never an irradiance (`ui-rules` §2), and a reading the
// meter flagged as saturated or overrange is a dot in the alert colour on the
// line rather than a value left out: a clipped log is the classic way to ruin
// a long measurement, and the chart is where it is seen first.

import * as scale from '../scale.js';
import * as fmt from '../format.js';
import { layout, axisTicks, heightFor, ASPECT } from './frame.js';

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

/** The readings inside the window ending now — the whole log when `seconds` is null. */
export function windowPoints(points, { seconds, now }) {
  if (!points || !points.length) return [];
  if (seconds === null || seconds === undefined) return points;
  return points.slice(lowerBound(points, now - seconds));
}

/**
 * Mean, extremes and the sample standard deviation over the window, and how
 * many of the readings the meter itself did not trust. `std` is null under
 * two points: a σ of one reading is not a number.
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
 * The chart model: one panel, the readings in the window against seconds
 * (minutes, hours) before now. `fromZero` pins the y axis to zero — the
 * lamp being off is then visibly off — and otherwise the axis is a window
 * on the data, so a 1 % drift on a settled LED is a slope rather than a
 * flat line.
 */
export function powerModel(points, {
  seconds = null, now, width = 1400, height = null, fromZero = false, columns = null,
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
    width, panels: pPanel, margin: { left: 60 },
    height: height ?? heightFor(width, pPanel, { margin: { left: 60 }, aspect: ASPECT.strip }),
  });
  const panel = frame.panels[0];
  const rect = panel.rect;
  const X = scale.linear([-span / div, 0], [rect.x, rect.x + rect.w]);
  const xs = inWindow.map((p) => (p.ts - at) / div);
  const ys = inWindow.map((p) => p.watts / factor);
  const domain = scale.extent([ys], { includeZero: fromZero, pad: 0.08 }) || [0, 1];
  const Y = scale.linear(domain, [rect.y + rect.h, rect.y]);
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
    y: { scale: Y, ticks: axisTicks(Y, { count: 4 }) },
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

/** `mean 92.1 µW · min 91.4 · max 92.9 · σ 0.31 µW · 300 readings` */
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

// -- the rail's spark --------------------------------------------------------

/**
 * How much of the trace the rail's spark carries, in seconds.
 *
 * Two minutes. The panel's window is the operator's to pick, and a glyph that
 * meant a different span every time it was glanced at would be worse than no
 * glyph; this one is fixed, so *flat* always means flat for the same two
 * minutes. Long enough that a drift or a step is a shape rather than a
 * wobble, short enough that it is a statement about now — which is the whole
 * of what the rail is for (`ui-rules` §8's spot check).
 */
export const SPARK_WINDOW_S = 120;

/**
 * The last two minutes as a path that fits beside a number: the rail's cell,
 * 60 × 18 px.
 *
 * This is `powerModel` with everything a 60 px box cannot carry taken out —
 * no axes, no units, no statistics, no crosshair. What is left is the one
 * question the rail answers: *is the lamp on, and is it holding?* The panel a
 * click away answers the rest, and the two read the same log so they cannot
 * disagree about the shape.
 *
 * Two decisions are worth naming.
 *
 * **The box always spans the window's own readings**, from the first in it to
 * `now`. A domain pinned to `[now - SPARK_WINDOW_S, now]` would draw a stub
 * in the right sixth of the box for the first twenty seconds of a monitor,
 * which is exactly when somebody is watching it.
 *
 * **A constant series is not centred unless it is non-zero.** A flat line has
 * no extent to scale into, and the two constants say different things: a
 * steady reading is holding, and mid-box is where it belongs — but zero is
 * the lamp being *off*, and a line drawn mid-box says it is on and steady.
 * That is `ui-rules` §2's refusal to draw an absence as a value, one box
 * smaller, and it is the reason the panel carries a `y from 0` switch at all.
 */
export function sparkModel(points, {
  w = 60, h = 18, pad = 1.5, seconds = SPARK_WINDOW_S, now, columns = null,
} = {}) {
  const at = now !== undefined ? now : Date.now() / 1000;
  const inWindow = windowPoints(points || [], { seconds, now: at });
  if (inWindow.length < 2) return null;

  let lo = Infinity;
  let hi = -Infinity;
  let flagged = 0;
  for (const p of inWindow) {
    if (p.trustworthy === false) flagged += 1;
    if (!Number.isFinite(p.watts)) continue;
    if (p.watts < lo) lo = p.watts;
    if (p.watts > hi) hi = p.watts;
  }
  if (!Number.isFinite(lo)) return null;

  const flat = hi === lo;
  const X = scale.linear([inWindow[0].ts, at], [pad, w - pad]);
  const Y = flat
    ? () => (lo === 0 ? h - pad : h / 2)
    : scale.linear([lo, hi], [h - pad, pad]);

  const xs = inWindow.map((p) => p.ts);
  const ys = inWindow.map((p) => p.watts);
  const cols = columns || Math.round(w - 2 * pad);
  const last = inWindow[inWindow.length - 1];
  return {
    w,
    h,
    flat,
    flagged,
    n: inWindow.length,
    span_s: at - inWindow[0].ts,
    // The same thinning the panel uses, for the same reason: a column that
    // drops its own extreme is a peak the operator never sees. At 60 px the
    // envelope is one vertical segment per pixel, which is what a spark is.
    d: inWindow.length > cols * 2
      ? scale.envelopePath(ys, (i) => X(xs[i]), Y, { columns: cols })
      : scale.linePath(xs, ys, X, Y),
    last: { x: scale.fixed(X(last.ts)), y: scale.fixed(Y(last.watts)), watts: last.watts },
  };
}
