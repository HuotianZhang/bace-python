// The component selects Q per loop for a zero-width axis and Q(axis) for a sweep. The
// caption makes that switch explicit; repeated points avoid curvature bias
// (`docs/ui-rules.md` §4). Zero Q standard deviation denotes unrecorded uncertainty,
// including a single loop, and uses a hollow marker (§2). Truncated runs show
// kept/requested counts and hatch the unacquired region. Scalar `StepDone`,
// `LoopDone` and `AxisResolved` data preserve this chart when replay omits traces.
// Endpoint hydration supplies service statistics when the ring is incomplete. `step`
// is the 1-based axis position; `index` counts shots across all loops. Using `index`
// would place later loops beyond the axis.

import * as scale from '../scale.js';
import * as fmt from '../format.js';
import { layout, axisTicks, heightFor, ASPECT, MAX_UNIT, PLOT_MARGIN } from './frame.js';
import { ranged } from './view.js';

/** Swept quantities use slash-unit labels (`docs/ui-rules.md` §2). */
export const AXIS_UNITS = { vpre: 'V', vcoll: 'V', delay_ns: 'ns' };

/** The plain names of the swept quantities, for an axis title. */
const AXIS_NAMES = { vpre: 'Vpre', vcoll: 'Vcoll', delay_ns: 'Delay' };

/**
 * The helper classifies an axis as repeat (`true`), sweep (`false`) or unresolved
 * (`null`). Ring-tail hydration and unstarted runs can lack an axis.
 */
export function isRepeat(node) {
  const values = (node && node.values) || [];
  if (values.length === 1) return true;
  if (values.length > 1) return false;
  const axis = node && node.axis;
  if (axis && axis.start !== undefined && axis.start !== null && axis.start === axis.stop) return true;
  return null;
}

const finite = (v) => typeof v === 'number' && Number.isFinite(v);

/**
 * The helper builds one row per axis point with its shots and service statistics. The
 * last shot carries the running mean including itself. `LoopDone` recomputes it;
 * hydrated `node.q_mean` takes precedence when its record is more recent than
 * retained shots.
 */
export function pointSummary(node) {
  const values = node.values || [];
  const loopsAt = node.loops && node.loops.length ? node.loops[node.loops.length - 1].ts : -Infinity;
  const points = values.map((x, i) => ({ i, x, shots: [] }));
  for (const shot of node.shots || []) {
    const i = (shot.step || 1) - 1;
    if (points[i]) points[i].shots.push(shot);
  }
  return points.map((p) => {
    const last = p.shots[p.shots.length - 1] || null;
    const fromService = node.q_mean && finite(node.q_mean[p.i]);
    const useShot = last && (!fromService || (last.ts || 0) >= loopsAt);
    const mean = useShot ? last.q_mean : fromService ? node.q_mean[p.i] : null;
    const std = useShot ? last.q_std : node.q_std && finite(node.q_std[p.i]) ? node.q_std[p.i] : null;
    return {
      i: p.i, x: p.x,
      n: p.shots.length,
      q: p.shots.map((s) => s.q),
      mean: finite(mean) ? mean : null,
      // Zero Q standard deviation means unrecorded uncertainty (`docs/ui-rules.md`
      // §2).
      sigma: finite(std) && std !== 0 ? std : null,
    };
  });
}

/**
 * The repeat series uses each shot's Q and service-provided running mean and sample
 * deviation. A replay can contain only a tail, so recomputing from retained shots
 * would describe the replay rather than the run. `q_std` is zero before two loops and
 * renders as absent uncertainty (`docs/ui-rules.md` §2).
 */
export function loopSeries(node) {
  const shots = (node.shots || []).slice().sort((a, b) => (a.loop - b.loop) || (a.index - b.index));
  const out = [];
  for (const shot of shots) {
    if (!finite(shot.q)) continue;
    out.push({
      loop: shot.loop, q: shot.q,
      mean: finite(shot.q_mean) ? shot.q_mean : shot.q,
      sigma: finite(shot.q_std) && shot.q_std !== 0 ? shot.q_std : null,
    });
  }
  return out;
}

/**
 * `Q (nC)`: one SI prefix for the axis, chosen from the data, never per value.
 */
export function chargeUnit(values) {
  let peak = 0;
  for (const v of values) if (finite(v) && Math.abs(v) > peak) peak = Math.abs(v);
  const [value, prefix] = fmt.prefixed(peak > 0 ? peak : 1e-12);
  const factor = 10 ** Math.round(Math.log10((peak > 0 ? peak : 1e-12) / value));
  return { factor, label: `Q (${prefix}C)`, unit: `${prefix}C` };
}

/** The notes that stay under the plot: a run that fell short of what was asked. */
const shortfalls = (notes) => notes.filter((n) => /completed · |without traces/.test(n));

/** The caption uses the run outcome to identify truncation. */
function ending(record, node) {
  const state = record && record.state;
  if (node && node.outcome && node.outcome !== 'ok') return node.outcome;
  if (state === 'stopped' || state === 'aborted' || state === 'failed') return state;
  if (record && record.aborted) return record.aborted.reason === 'requested' ? 'stopped' : 'aborted';
  return null;
}

/**
 * The model draws one panel and selects repeat or sweep presentation. `found` has the
 * `{record, node}` shape returned by `results.runFor`.
 */
export function loopsModel(found, options = {}) {
  const { width = 560, height = null } = options;
  // Q against the swept axis: a quantity against a quantity, so `curve`.
  const qPanel = [{ key: 'q', weight: 1 }];
  const qHeight = height ?? heightFor(width, qPanel, { margin: PLOT_MARGIN, aspect: ASPECT.curve, cap: MAX_UNIT.curve });
  const node = found && found.node;
  const record = found && found.record;
  if (!node) {
    return {
      key: 'loops',
      // The empty panel is named Q because the resolved axis determines whether it
      // becomes Q per loop or Q(axis). The measured chart must make that switch
      // visible (`docs/ui-rules.md` §4).
      absent: {
        text: 'No shots yet',
        detail: null,
        frame: { width, height: qHeight, panels: [{ key: 'q', weight: 1, label: 'Q' }], margin: PLOT_MARGIN },
      },
      panels: [],
      notes: [],
    };
  }
  const repeat = isRepeat(node);
  const shots = node.shots || [];
  if (repeat === null) {
    return {
      key: 'loops',
      absent: {
        text: shots.length ? 'Axis not known yet' : 'No shots yet',
        detail: shots.length
          ? 'The run began before this page opened; the axis arrives with its record.'
          : null,
      },
      panels: [], notes: [],
    };
  }
  const values = node.values || [];
  const points = values.length || 1;
  const requestedShots = finite(node.requested) ? node.requested : null;
  const loopsRequested = requestedShots !== null ? Math.max(1, Math.round(requestedShots / points))
    : finite(record && record.n_loops) ? record.n_loops : null;
  const kept = finite(node.kept) ? Math.max(node.kept, shots.length) : shots.length;
  const loopsDone = Math.max((node.loops || []).length, Math.floor(kept / points));
  const ended = ending(record, node);
  const frame = layout({ width, height: qHeight, panels: qPanel, margin: PLOT_MARGIN });
  const panel = frame.panels[0];
  const common = { key: 'loops', width: frame.width, height: frame.height, margin: frame.margin };
  const counts = { points, kept, requestedShots, loopsDone, loopsRequested, ended };
  return repeat
    ? repeatModel(common, panel, node, counts, options)
    : sweepModel(common, panel, node, counts, options);
}

// -- Q per loop -----------------------------------------------------------

function repeatModel(common, panel, node, counts, options) {
  const series = loopSeries(node);
  const axis = node.axis || {};
  const unit = AXIS_UNITS[axis.name] || '';
  const where = axis.name
    ? `${axis.name} ${fmt.sig(axis.start, 4)}${unit ? ' ' + unit : ''} = stop`
    : 'start = stop';
  // The acquired boundary uses loop numbers rather than retained-series length. A
  // replay holding loops 81–90 must not hatch from loop 11. `loopsDone` is hydrated
  // from the run record and cannot decrease; the last retained shot also supplies a
  // loop number. The larger value sets the boundary.
  const firstLoop = series.length ? series[0].loop : 1;
  const lastLoop = series.length ? series[series.length - 1].loop : 0;
  const done = Math.max(lastLoop, counts.loopsDone);
  const total = counts.loopsRequested || Math.max(done, 1);
  const { rect } = panel;
  const view = options.view || null;
  const X = scale.linear(ranged(view, 'x', 'loop', [0.5, total + 0.5]), [rect.x, rect.x + rect.w]);
  const unitQ = chargeUnit(series.map((s) => s.q));
  const scaled = series.map((s) => s.q / unitQ.factor);
  const band = series.filter((s) => s.sigma !== null)
    .flatMap((s) => [(s.mean + s.sigma) / unitQ.factor, (s.mean - s.sigma) / unitQ.factor]);
  const domain = scale.extent([scaled, band], { includeZero: false }) || [-1, 1];
  const Y = scale.linear(ranged(view, 'q', unitQ.unit, domain), [rect.y + rect.h, rect.y]);

  const dots = series.map((s) => ({ x: scale.fixed(X(s.loop)), y: scale.fixed(Y(s.q / unitQ.factor)), r: 2.2, colour: 'grey', opacity: 0.8 }));
  const meanPath = scale.linePath(series.map((s) => X(s.loop)), series.map((s) => s.mean / unitQ.factor), (v) => v, Y);
  const bands = bandPath(series.filter((s) => s.sigma !== null), X, Y, unitQ.factor);

  const shades = [];
  const marks = [];
  const rules = [];
  if (counts.ended && counts.loopsRequested && done < counts.loopsRequested) {
    // Hatching retains the space requested for unacquired loops.
    const x0 = X(done + 0.5);
    shades.push({ x: x0, w: rect.x + rect.w - x0, hatch: true, colour: 'rule', opacity: 1 });
    rules.push({ x1: x0, colour: 'accent', width: 1.5 });
    marks.push({ x: x0 + 6, y: rect.y + 12, colour: 'accent-dark', weight: 500, size: 11,
      text: `loops ${done + 1} – ${counts.loopsRequested} not acquired` });
    marks.push({ x: x0 + 6, y: rect.y + 24, colour: 'accent-dark', size: 11,
      text: `${counts.ended} at loop ${done}` });
  }
  const last = series[series.length - 1];
  const loopTicks = axisTicks(X, {
    values: view && view.ranges.x ? null : loopTickValues(total), count: 5,
  });
  const said = notesFor(counts, done, node, { repeat: true, firstLoop });

  return {
    ...common,
    panels: [{
      ...panel,
      label: `Q per loop  ·  ${unitQ.label}  ·  loop →`,
      note: `Repeats at ${where}`,
      noteColour: 'accent-dark',
      y: { scale: Y, ticks: axisTicks(Y, { count: 4 }), title: unitQ.label, unit: unitQ.unit },
      series: [{
        key: 'mean', label: 'running mean', colour: 'photo', width: 1.5, d: meanPath,
        at: (loop) => {
          const s = nearestLoop(series, loop);
          return s ? { y: s.mean / unitQ.factor } : null;
        },
        format: (v) => fmt.charge(v * unitQ.factor),
      }, {
        key: 'q', label: 'Q', colour: 'ink', width: 0, d: '',
        at: (loop) => {
          const s = nearestLoop(series, loop);
          return s ? { y: s.q / unitQ.factor } : null;
        },
        format: (v) => fmt.charge(v * unitQ.factor),
      }],
      bands: bands ? [{ d: bands, colour: 'photo', opacity: 0.12 }] : [],
      dots, shades, rules, marks,
    }],
    x: {
      scale: X,
      ticks: loopTicks,
      label: `loop  →  ${counts.loopsRequested ? counts.loopsRequested + ' requested' : ''}`.trim(),
      title: 'Loop',
      unit: 'loop',
      format: (v) => `loop ${Math.round(v)}`,
    },
    legend: [
      { label: 'Q, each loop', marker: 'dot', colour: 'grey' },
      { label: 'running mean', colour: 'photo', width: 1.5 },
      { label: '± σ so far', marker: 'band', colour: 'photo' },
    ],
    readout: readoutRepeat(series, last, counts),
    notes: said,
    alerts: shortfalls(said),
    switch: { repeat: true, text: `Repeats at ${where}` },
  };
}

function loopTickValues(total) {
  if (total <= 10) return Array.from({ length: total }, (_, i) => i + 1);
  const dv = scale.step(total, 5);
  const out = [];
  for (let v = dv; v <= total; v += dv) out.push(v);
  if (!out.includes(1)) out.unshift(1);
  return out;
}

function nearestLoop(series, loop) {
  if (!series.length) return null;
  const k = Math.round(loop);
  let best = series[0];
  for (const s of series) if (Math.abs(s.loop - k) < Math.abs(best.loop - k)) best = s;
  return best;
}

/** The ±σ region around the running mean, as one closed path. */
function bandPath(series, X, Y, factor) {
  if (series.length < 2) return null;
  const up = series.map((s) => `${scale.fixed(X(s.loop))} ${scale.fixed(Y((s.mean + s.sigma) / factor))}`);
  const down = series.slice().reverse().map((s) => `${scale.fixed(X(s.loop))} ${scale.fixed(Y((s.mean - s.sigma) / factor))}`);
  return `M${up.join('L')}L${down.join('L')}Z`;
}

function readoutRepeat(series, last, counts) {
  if (!last) return 'no shots yet';
  const parts = [`loop ${last.loop}${counts.loopsRequested ? ' of ' + counts.loopsRequested : ''}`,
    `Q ${fmt.charge(last.q)}`, `mean ${fmt.charge(last.mean)}`];
  parts.push(last.sigma !== null ? `σ ${fmt.scientific(last.sigma, 3)} C` : 'σQ not recorded');
  return parts.join('  ·  ');
}

// -- Q(axis) --------------------------------------------------------------

function sweepModel(common, panel, node, counts, options) {
  const points = pointSummary(node);
  const axis = node.axis || {};
  const unit = AXIS_UNITS[axis.name] || '';
  const { rect } = panel;
  const xs = points.map((p) => p.x);
  const xd = scale.extent([xs], { pad: 0.08 }) || [0, 1];
  const view = options.view || null;
  const X = scale.linear(ranged(view, 'x', unit, xd), [rect.x, rect.x + rect.w]);
  const all = points.flatMap((p) => p.q);
  const unitQ = chargeUnit([...all, ...points.map((p) => p.mean)]);
  const f = unitQ.factor;
  const bars = points.filter((p) => p.sigma !== null && p.mean !== null)
    .flatMap((p) => [(p.mean + p.sigma) / f, (p.mean - p.sigma) / f]);
  const domain = scale.extent([all.map((v) => v / f), points.map((p) => (p.mean === null ? NaN : p.mean / f)), bars],
    { includeZero: false }) || [-1, 1];
  const Y = scale.linear(ranged(view, 'q', unitQ.unit, domain), [rect.y + rect.h, rect.y]);

  const dots = [];
  const rules = [];
  for (const p of points) {
    for (const q of p.q) {
      if (finite(q)) dots.push({ x: scale.fixed(X(p.x)), y: scale.fixed(Y(q / f)), r: 1.6, colour: 'grey', opacity: 0.55 });
    }
  }
  for (const p of points) {
    if (p.mean === null) continue;
    const x = scale.fixed(X(p.x));
    const y = scale.fixed(Y(p.mean / f));
    if (p.sigma !== null) {
      dots.push({ x, y, r: 2.8, colour: 'ink', shape: 'dot' });
      rules.push({ x1: x, y1: scale.fixed(Y((p.mean + p.sigma) / f)), y2: scale.fixed(Y((p.mean - p.sigma) / f)), colour: 'ink', width: 0.9, cap: 2.2 });
    } else {
      dots.push({ x, y, r: 2.6, colour: 'ink', shape: 'square', hollow: true });
    }
  }
  const measured = points.filter((p) => p.mean !== null);
  const meanPath = scale.linePath(measured.map((p) => X(p.x)), measured.map((p) => p.mean / f), (v) => v, Y);
  const withSigma = points.filter((p) => p.sigma !== null).length;
  const current = currentPoint(node, points);
  const said = notesFor(counts, counts.loopsDone, node, { repeat: false, withSigma, points: points.length });
  const name = AXIS_NAMES[axis.name] || axis.name || 'Axis';

  return {
    ...common,
    panels: [{
      ...panel,
      label: `Q(${axis.name || 'axis'})  ·  ${unitQ.label}  ·  ${points.length} pts`,
      note: counts.loopsRequested
        ? `loop ${Math.min(counts.loopsDone + (current ? 1 : 0), counts.loopsRequested)} of ${counts.loopsRequested}`
        : `${counts.loopsDone} loops`,
      y: { scale: Y, ticks: axisTicks(Y, { count: 4 }), title: unitQ.label, unit: unitQ.unit },
      series: [{
        key: 'mean', label: 'mean', colour: 'ink', width: 1.1, d: meanPath,
        at: (xv) => {
          const p = nearestPoint(measured, xv);
          return p ? { y: p.mean / f } : null;
        },
        format: (v) => fmt.charge(v * f),
      }, {
        key: 'sigma', label: 'σ', colour: 'grey', width: 0, d: '',
        at: (xv) => {
          const p = nearestPoint(measured, xv);
          return p && p.sigma !== null ? { y: p.sigma / f } : null;
        },
        format: (v) => `${fmt.scientific(v * f, 3)} C`,
      }],
      dots, rules,
      marks: current ? [{
        x: scale.fixed(X(current.x)) + 4, y: rect.y + 11, colour: 'accent', size: 11, halo: true,
        text: `now · point ${current.i + 1}`,
      }] : [],
      shades: current ? [{ x: scale.fixed(X(current.x)) - 1, w: 2, colour: 'accent', opacity: 0.5 }] : [],
    }],
    x: {
      scale: X,
      ticks: axisTicks(X, { count: 5 }),
      label: `${axis.name || 'axis'}${unit ? ' / ' + unit : ''}  →`,
      title: `${name}${unit ? ` (${unit})` : ''}`,
      unit,
      format: (v) => `${fmt.sig(v, 4)}${unit ? ' ' + unit : ''}`,
    },
    legend: [
      { label: 'mean over the loops so far', colour: 'ink', width: 1.2 },
      { label: 'σQ measured', marker: 'dot', colour: 'ink' },
      { label: 'σQ not recorded', marker: 'square-hollow', colour: 'ink' },
      { label: 'each shot', marker: 'dot-faint', colour: 'grey' },
    ],
    readout: readoutSweep(points, counts, withSigma),
    notes: said,
    alerts: shortfalls(said),
    switch: { repeat: false, text: `swept ${axis.name || 'axis'} · ${points.length} points` },
  };
}

/** The helper locates the latest shot's point while its node runs. */
function currentPoint(node, points) {
  if (node.outcome) return null;
  const last = node.lastShot;
  if (!last) return null;
  return points[(last.step || 1) - 1] || null;
}

function nearestPoint(points, xv) {
  if (!points.length) return null;
  let best = points[0];
  for (const p of points) if (Math.abs(p.x - xv) < Math.abs(best.x - xv)) best = p;
  return best;
}

function readoutSweep(points, counts, withSigma) {
  const measured = points.filter((p) => p.mean !== null).length;
  const parts = [`${counts.kept}${counts.requestedShots ? ' of ' + counts.requestedShots : ''} shots`];
  parts.push(`${measured} of ${points.length} points measured`);
  parts.push(withSigma ? `σ on ${withSigma}` : 'σQ not recorded yet — one loop');
  if (counts.ended) parts.push(counts.ended);
  return parts.join('  ·  ');
}

function notesFor(counts, loopsDone, node, { repeat, withSigma = 0, points = 0, firstLoop = 1 }) {
  const out = [];
  if (counts.loopsRequested) {
    // Truncated-run captions retain requested and completed loop counts
    // (`docs/ui-rules.md` §9).
    out.push(`${counts.loopsRequested} loop${counts.loopsRequested === 1 ? '' : 's'} requested, ${loopsDone} completed`
      + (counts.ended ? ` · ${counts.ended}` : ''));
  }
  if (!repeat && points && withSigma < points && loopsDone >= 1) {
    out.push(`σQ needs two loops at a point: ${points - withSigma} of ${points} still carry one`);
  }
  if (repeat && firstLoop > 1) {
    // The caption identifies a retained suffix so the space before its first point
    // does not imply unacquired loops.
    out.push(`loops 1 – ${firstLoop - 1} ran before this console's view of them: the ring keeps the last shots, `
      + 'so their Q is not drawn — the mean and σ still cover every loop');
  }
  if (repeat) out.push('the mean and the ± σ band are the service\'s own, over every loop that ran — not recomputed from the shots this console holds');
  const gone = (node.shots || []).filter((s) => s.tracesGone).length;
  if (gone) out.push(`${gone} shot${gone === 1 ? '' : 's'} without traces — replayed or journalled; the point comes from the scalars`);
  return out;
}
