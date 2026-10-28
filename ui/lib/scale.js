// Pure scale functions retain measurement structure without DOM dependencies. Taking
// every ninth transient sample can miss its spike, requiring explicit reduction
// rules. U+2212 minus aligns with mono digits (`docs/ui-rules.md` §2).

export const MINUS = '−';

/** A linear scale. `f(v)` maps the domain onto the range; `f.invert` returns. */
export function linear([d0, d1], [r0, r1]) {
  // Repeats and flat traces have zero-width domains. They map to panel centre rather
  // than dividing by zero and disappearing.
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
 * The base-10 scale displays dark leakage over five decades. Zero and negative inputs
 * clamp to `floor`; the model counts them so a zero crossing cannot silently shorten
 * the curve.
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
 * Tick steps use 1, 2 or 5 times a power of ten. Span determines the step so a
 * growing curve does not relabel a stable domain.
 */
export function step(span, count = 5) {
  if (!(span > 0)) return 1;
  const rough = span / Math.max(1, count);
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const scaled = rough / magnitude;
  // Geometric-mean thresholds select the nearest requested tick count. Integer
  // thresholds would give four ticks instead of six over −4 V to 2 V, omitting sweep
  // voltages.
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
  // The epsilon is 1e-9 of a step, retaining endpoint ticks despite products such as
  // `0.1 * 3`. Step rounding also keeps labels short.
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
  // `10 ** -5` and `1e-5` can be different doubles. Only the exact decade spelling
  // gives the intended label.
  for (let e = a; e <= b; e += 1) out.push(Number(`1e${e}`));
  return out;
}

/** Step rounding formats `0.30000000000000004` as `0.3`. */
export function round(value, dv) {
  const decimals = Math.max(0, -Math.floor(Math.log10(dv) + 1e-9));
  return Number(value.toFixed(Math.min(20, decimals)));
}

/**
 * Tick labels use only precision resolved by their step. Extra decimal digits would
 * imply unsupported axis precision.
 */
export function tickText(value, dv) {
  if (value === 0) return '0';
  const magnitude = Math.abs(value);
  if (magnitude >= 1e6 || magnitude < 1e-4) {
    // Scientific ticks retain one decimal only when needed. Rounding `4.5e−10` to
    // `5e−10` would disagree with its gridline.
    return value.toExponential(1).replace('.0e', 'e').replace('+', '').replaceAll('-', MINUS);
  }
  const decimals = Math.max(0, -Math.floor(Math.log10(dv) + 1e-9));
  return value.toFixed(Math.min(20, decimals)).replaceAll('-', MINUS);
}

/**
 * The helper computes padded extent with optional zero inclusion. Photocurrent needs
 * zero to distinguish a small signal from shifted baseline; J–V quadrants also depend
 * on it.
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
    // A flat trace gets one step of space on each side so it remains visible rather
    // than hiding in a zero-height frame.
    const air = Math.abs(lo) > 0 ? Math.abs(lo) * 0.1 : 1;
    return [lo - air, hi + air];
  }
  const air = (hi - lo) * pad;
  return [lo - air, hi + air];
}

/**
 * The polyline breaks across missing samples instead of connecting unsupported gaps.
 */
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
 * Pixel thinning keeps extrema in acquisition order. Taking every ninth sample of a
 * 4000-point trace in 450 px can miss the peak, contradicting the autorange verdict
 * beside it. Each column retains minimum and maximum to preserve envelope and edge
 * direction with one vertical segment per column. Below about two samples per column,
 * the exact polyline is cheaper.
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
    // `X` can look up sample times, so its index must be integral. A fractional array
    // index returns `undefined` and can collapse a column to x = 0.
    const x = fixed(X(Math.round((from + to - 1) / 2)));
    const first = loAt <= hiAt ? lo : hi;
    const second = loAt <= hiAt ? hi : lo;
    d += (pen ? 'L' : 'M') + x + ' ' + fixed(Y(first));
    d += 'L' + x + ' ' + fixed(Y(second));
    pen = true;
  }
  return d;
}

/** The helper selects the nearest sample for crosshair readout. */
export function nearestIndex(value, { t0 = 0, dt = 1, n }) {
  if (!(n > 0)) return -1;
  const i = Math.round((value - t0) / dt);
  return Math.min(n - 1, Math.max(0, i));
}

/**
 * Two decimal places keep path coordinates within a tenth of a pixel; additional
 * precision adds unnecessary text.
 */
export function fixed(px) {
  return Number.isFinite(px) ? String(Math.round(px * 100) / 100) : '0';
}
