// The scale and axis foundation — `docs/ui-plan.md` decision 3: *"All six sit
// on one scale/axis module. That module is the only thing in `ui/` being
// written from nothing."*
//
// It is written from nothing because no charting library can do the job the
// plan describes: the domain rules are the point, and a library that draws a
// nice axis for you still draws a lie when a 4000-point transient is thinned
// to 450 pixels by taking every ninth sample. Everything here is a pure
// function of numbers, so it is tested in `node` with no browser and no DOM.
//
// The one convention: **`−` is U+2212**, not a hyphen. The design's own charts
// do `String(v).replace('-','−')` and the mono face aligns the real minus with
// its digits, which is the difference between a column that reads down and one
// that does not (`docs/ui-rules.md` §2).

export const MINUS = '−';

/** A linear scale. `f(v)` maps the domain onto the range; `f.invert` returns. */
export function linear([d0, d1], [r0, r1]) {
  // A zero-width domain is normal here, not a guard against nonsense: a
  // zero-width axis is a *repeat* (`ui-rules` §4), and a flat trace — a dark
  // reference that never moved — has one too. Both should draw down the middle
  // of the panel rather than divide by zero and vanish.
  const span = d1 - d0;
  const f = span === 0
    ? () => (r0 + r1) / 2
    : (v) => r0 + ((v - d0) / span) * (r1 - r0);
  f.invert = span === 0 ? () => d0 : (px) => d0 + ((px - r0) / (r1 - r0)) * span;
  f.domain = [d0, d1];
  f.range = [r0, r1];
  f.kind = 'linear';
  return f;
}

/**
 * A base-10 log scale, for `|J|` on the dark J–V — the one plot in this
 * project whose interesting range is five decades of leakage.
 *
 * `floor` is where a zero or a negative goes. A log axis has no zero, and a
 * dark sweep crosses it: dropping those points would silently shorten the
 * curve, so they are clamped to the floor and the model says how many were.
 */
export function log10([d0, d1], [r0, r1], { floor = 1e-12 } = {}) {
  const lo = Math.log10(Math.max(Math.abs(d0), floor));
  const hi = Math.log10(Math.max(Math.abs(d1), floor));
  const inner = linear([lo, hi], [r0, r1]);
  const f = (v) => inner(Math.log10(Math.max(Math.abs(v), floor)));
  f.invert = (px) => 10 ** inner.invert(px);
  f.domain = [d0, d1];
  f.range = [r0, r1];
  f.floor = floor;
  f.kind = 'log10';
  return f;
}

/**
 * A nice step: 1, 2 or 5 times a power of ten. The step is chosen for the
 * *span*, so the same axis keeps its step as a live trace grows into it
 * instead of re-labelling itself every frame.
 */
export function step(span, count = 5) {
  if (!(span > 0)) return 1;
  const rough = span / Math.max(1, count);
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const scaled = rough / magnitude;
  // The thresholds are the geometric means of the 1 / 2 / 5 / 10 steps
  // (sqrt(2), sqrt(10), sqrt(50)), so each span gets the step whose tick count
  // is closest to the one asked for. Rounding at the integers instead — the
  // obvious version — turns a request for six ticks on the J–V's −4 … 2 V into
  // four, and the sweep's own volts stop being labelled.
  const chosen = scaled >= 7.0710678 ? 10 : scaled >= 3.1622777 ? 5 : scaled >= 1.4142136 ? 2 : 1;
  return chosen * magnitude;
}

/** The tick values inside `[lo, hi]`, on a nice step. */
export function ticks(lo, hi, count = 5) {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return [];
  if (lo === hi) return [lo];
  const [a, b] = lo <= hi ? [lo, hi] : [hi, lo];
  const dv = step(b - a, count);
  const out = [];
  // The epsilon is 1e-9 of a step, not of a value: `0.1 * 3` is 0.30000000000000004
  // and an axis that drops its last tick because of it looks like a bug in the
  // data. Rounding the value onto the step also keeps the label short.
  for (let i = Math.ceil(a / dv - 1e-9); i * dv <= b + dv * 1e-9; i += 1) {
    const v = i * dv;
    if (v >= a - dv * 1e-9 && v <= b + dv * 1e-9) out.push(round(v, dv));
  }
  return out;
}

/** Whole decades inside `[lo, hi]`, for a log axis. */
export function decades(lo, hi) {
  const a = Math.ceil(Math.log10(Math.max(Math.abs(lo), Number.MIN_VALUE)));
  const b = Math.floor(Math.log10(Math.max(Math.abs(hi), Number.MIN_VALUE)));
  const out = [];
  // `10 ** -5` is 0.000009999999999999999 and `1e-5` is not — they are
  // different doubles, and only the second one labels itself as a decade.
  for (let e = a; e <= b; e += 1) out.push(Number(`1e${e}`));
  return out;
}

/** Snap a value onto its step, so `0.30000000000000004` labels as `0.3`. */
export function round(value, dv) {
  const decimals = Math.max(0, -Math.floor(Math.log10(dv) + 1e-9));
  return Number(value.toFixed(Math.min(20, decimals)));
}

/**
 * A tick's label, to the precision its own step resolves — never more. A grid
 * labelled `0.30000000000000004` and a grid labelled `0.3000` are the same
 * mistake at different sizes: digits the axis does not resolve.
 */
export function tickText(value, dv) {
  if (value === 0) return '0';
  const magnitude = Math.abs(value);
  if (magnitude >= 1e6 || magnitude < 1e-4) {
    // One decimal, dropped when it is a zero: the charge axis wants
    // `4.5e−10` between `0` and `9e−10`, and `5e−10` there is the label
    // disagreeing with the gridline it sits on.
    return value.toExponential(1).replace('.0e', 'e').replace('+', '').replaceAll('-', MINUS);
  }
  const decimals = Math.max(0, -Math.floor(Math.log10(dv) + 1e-9));
  return value.toFixed(Math.min(20, decimals)).replaceAll('-', MINUS);
}

/**
 * The extent of one or more arrays, padded, optionally forced to include zero.
 *
 * Zero is not decoration on these plots: a photocurrent that never crosses it
 * and an axis that does not show it are the same picture, and the operator
 * cannot tell a small signal from a shifted baseline. The J–V's quadrant rules
 * need it for the same reason.
 */
export function extent(arrays, { pad = 0.06, includeZero = false, symmetric = false } = {}) {
  let lo = Infinity;
  let hi = -Infinity;
  for (const values of arrays) {
    if (!values) continue;
    for (let i = 0; i < values.length; i += 1) {
      const v = values[i];
      if (!Number.isFinite(v)) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  }
  if (lo === Infinity) return null;
  if (includeZero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  if (symmetric) { const m = Math.max(Math.abs(lo), Math.abs(hi)); lo = -m; hi = m; }
  if (lo === hi) {
    // A flat trace still deserves a panel: give it one step of air either side
    // rather than a zero-height plot with the line hidden in the frame.
    const air = Math.abs(lo) > 0 ? Math.abs(lo) * 0.1 : 1;
    return [lo - air, hi + air];
  }
  const air = (hi - lo) * pad;
  return [lo - air, hi + air];
}

/** A polyline through `(xs[i], ys[i])`, skipping the gaps rather than closing them. */
export function linePath(xs, ys, X, Y) {
  let d = '';
  let pen = false;
  for (let i = 0; i < ys.length; i += 1) {
    const y = ys[i];
    const x = typeof xs === 'function' ? xs(i) : xs[i];
    if (!Number.isFinite(y) || !Number.isFinite(x)) { pen = false; continue; }
    d += (pen ? 'L' : 'M') + fixed(X(x)) + ' ' + fixed(Y(y));
    pen = true;
  }
  return d;
}

/**
 * A trace, thinned to the pixels it has — **keeping the extremes**.
 *
 * This is the one place a plotting shortcut would change the measurement. A
 * 4000-point record drawn into 450 px by taking every ninth sample loses the
 * peak, and the peak is what the autorange verdict is about: `peak_light_a`
 * beside a curve whose peak is not on it is the chart contradicting the
 * number next to it. So each pixel column draws its own min and max, in the
 * order they occurred, which keeps the envelope *and* the direction of the
 * edge — and costs one vertical segment per column.
 *
 * Under about two samples per column there is nothing to thin, and the plain
 * polyline is both cheaper and exact.
 */
export function envelopePath(ys, X, Y, { columns } = {}) {
  const n = ys.length;
  const cols = Math.max(1, Math.floor(columns || 0));
  if (!n) return '';
  if (n <= cols * 2) return linePath((i) => i, ys, X, Y);
  let d = '';
  let pen = false;
  for (let c = 0; c < cols; c += 1) {
    const from = Math.floor((c * n) / cols);
    const to = Math.min(n, Math.floor(((c + 1) * n) / cols));
    let lo = Infinity;
    let hi = -Infinity;
    let loAt = -1;
    let hiAt = -1;
    for (let i = from; i < to; i += 1) {
      const v = ys[i];
      if (!Number.isFinite(v)) continue;
      if (v < lo) { lo = v; loAt = i; }
      if (v > hi) { hi = v; hiAt = i; }
    }
    if (loAt === -1) { pen = false; continue; }
    // An **integer** index, because `X` is routinely a lookup into the sample
    // times rather than arithmetic on the index — `time_s[1234.5]` is
    // `undefined`, and every column whose midpoint was not a whole number
    // collapsed to x = 0 before this line said `Math.round`.
    const x = fixed(X(Math.round((from + to - 1) / 2)));
    const first = loAt <= hiAt ? lo : hi;
    const second = loAt <= hiAt ? hi : lo;
    d += (pen ? 'L' : 'M') + x + ' ' + fixed(Y(first));
    d += 'L' + x + ' ' + fixed(Y(second));
    pen = true;
  }
  return d;
}

/** The sample nearest a domain value, for the crosshair. */
export function nearestIndex(value, { t0 = 0, dt = 1, n }) {
  if (!(n > 0)) return -1;
  const i = Math.round((value - t0) / dt);
  return Math.min(n - 1, Math.max(0, i));
}

/** Two decimal places is a tenth of a pixel: past that a path string is noise. */
export function fixed(px) {
  return Number.isFinite(px) ? String(Math.round(px * 100) / 100) : '0';
}
