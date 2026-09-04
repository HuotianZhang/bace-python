// Q per loop, or Q(axis) — `docs/ui-plan.md` M4, the fourth component.
//
// `docs/ui-rules.md` §4 is the whole reason this is one component with a
// switch inside it rather than two charts a caller picks between:
//
//   * **a zero-width axis plots Q per loop, not a curve.** `start == stop` is
//     a *repeat* — "Q at V_oc twenty times" is twenty loops of one point,
//     which has no curvature bias — and a swept axis plots Q(axis) with error
//     bars that tighten as loops complete. *The chart switches on this, and the
//     switch is visible, not silent*: the panel says which of the two it is
//     drawing and why, in the caption, on every render.
//   * **σ_Q = 0 is not recorded** (§2). One loop leaves `q_std = 0` — the
//     service's sample deviation needs two — and a zero-length error bar
//     drawn as a bare dot is a lie. R2·3's answer is the legend here:
//     `□ σ_Q not recorded · ● σ_Q measured`.
//   * **a truncated run is normal** (§9): the archive declares 100 loops and
//     holds 20. The loops that were never acquired are drawn as the region
//     they would have filled, hatched and captioned, and the count reads
//     *kept of requested* — never a curve that stops early as if that were
//     the whole run.
//
// The data is the store's per-node record: `shots` (every scalar of every
// `StepDone`, arrays or not — this chart never needs a trace, which is what
// lets it survive a reconnect that replayed the shots without them), `loops`
// (`LoopDone`), `values`/`axis` (`AxisResolved`), and `q_mean`/`q_std`, which
// the service recomputes at every `LoopDone` and `RunFinished` and the client
// fills in from `GET /runs/{id}/data` when the ring fell short.
//
// **`step`, not `index`.** A `StepDone`'s `index` is the shot's number in the
// whole scan (0 … n_shots − 1); `step` is its 1-based place on the axis. The
// second loop's first point is `index 3, step 1` on a three-point axis, and a
// chart that used `index` as the point would draw every loop after the first
// off the end of the axis.

import * as scale from '../scale.js';
import * as fmt from '../format.js';
import { layout, axisTicks } from './frame.js';

/** The unit each swept quantity is read in — `ui-rules` §2's slash convention. */
export const AXIS_UNITS = { vpre: 'V', vcoll: 'V', delay_ns: 'ns' };

/**
 * Whether this node's axis is zero-width. `true` is a repeat, `false` a sweep,
 * `null` is a node whose axis has not been resolved yet (the ring's tail, or a
 * run that has not started).
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
 * One row per axis point: its shots so far, and the mean and σ the service
 * holds for it.
 *
 * The mean comes from the newest source. A shot carries the running mean *at
 * that point, including itself*, so the last shot at a point is exactly the
 * service's number until the next `LoopDone` — which recomputes the same
 * thing. After a reconnect that lost the tail of the ring, or a boot that
 * hydrated from `GET /runs/{id}/data`, `node.q_mean` may be newer than any
 * shot the client holds, and then it wins.
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
      // A zero is *not recorded*, not a σ of zero (`ui-rules` §2).
      sigma: finite(std) && std !== 0 ? std : null,
    };
  });
}

/**
 * The loops of a repeat, in order: each shot's Q, and the mean and sample
 * deviation so far — **the service's own**, carried on every `StepDone` as
 * `q_mean`/`q_std` over every loop that ran, not recomputed here from the
 * shots this client happens to hold. A long scan outgrows the ring, so a
 * console that opened late or was dropped holds a *tail* of the shots, and a
 * mean summed from that tail would be a statistic of the replay rather than
 * of the run (`ui-rules` §6: render, never re-derive). `q_std` is `0` until
 * there are two loops, which is *not recorded*, so it reads as `null`.
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

/** `Q / 1e-12 C`: one power of ten for the axis, chosen from the data, never per value. */
export function chargeUnit(values) {
  let peak = 0;
  for (const v of values) if (finite(v) && Math.abs(v) > peak) peak = Math.abs(v);
  const e = peak > 0 ? Math.floor(Math.log10(peak)) : -12;
  return { factor: 10 ** e, label: `Q / 1e${e} C` };
}

/** How the run this node belongs to ended, for the caption on a truncated one. */
function ending(record, node) {
  const state = record && record.state;
  if (node && node.outcome && node.outcome !== 'ok') return node.outcome;
  if (state === 'stopped' || state === 'aborted' || state === 'failed') return state;
  if (record && record.aborted) return record.aborted.reason === 'requested' ? 'stopped' : 'aborted';
  return null;
}

/**
 * The chart model: one panel, and the switch. `found` is `{record, node}` as
 * `results.runFor` answers it.
 */
export function loopsModel(found, options = {}) {
  const { width = 560, height = 190 } = options;
  const node = found && found.node;
  const record = found && found.record;
  if (!node) return { key: 'loops', absent: { text: 'no run yet', detail: null }, panels: [], notes: [] };
  const repeat = isRepeat(node);
  const shots = node.shots || [];
  if (repeat === null) {
    return {
      key: 'loops',
      absent: {
        text: shots.length ? 'the axis is not known' : 'no shots yet',
        detail: shots.length
          ? 'the run began before this console could see it, and its AxisResolved is not in the ring — '
            + 'GET /runs/{id}/data fills it in'
          : 'Q per loop, or Q(axis), appears with the first shot',
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
  const frame = layout({ width, height, panels: [{ key: 'q', weight: 1 }], margin: { left: 60 } });
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
  // Where the acquired loops end, by **loop number**. A long repeat outgrows
  // the ring, so `series` can be loops 81–90 of a hundred: measured by its
  // length the chart would hatch from loop 11 and cross out ten loops it is
  // drawing. `loopsDone` is the run's own count, which never moves backwards
  // and is hydrated from `GET /runs/{id}/data`; the last shot held names the
  // loop it belongs to. The larger of the two is the boundary.
  const firstLoop = series.length ? series[0].loop : 1;
  const lastLoop = series.length ? series[series.length - 1].loop : 0;
  const done = Math.max(lastLoop, counts.loopsDone);
  const total = counts.loopsRequested || Math.max(done, 1);
  const { rect } = panel;
  const X = scale.linear([0.5, total + 0.5], [rect.x, rect.x + rect.w]);
  const unitQ = chargeUnit(series.map((s) => s.q));
  const scaled = series.map((s) => s.q / unitQ.factor);
  const band = series.filter((s) => s.sigma !== null)
    .flatMap((s) => [(s.mean + s.sigma) / unitQ.factor, (s.mean - s.sigma) / unitQ.factor]);
  const domain = scale.extent([scaled, band], { includeZero: false }) || [-1, 1];
  const Y = scale.linear(domain, [rect.y + rect.h, rect.y]);

  const dots = series.map((s) => ({ x: scale.fixed(X(s.loop)), y: scale.fixed(Y(s.q / unitQ.factor)), r: 2.2, colour: 'ink', opacity: 0.6 }));
  const meanPath = scale.linePath(series.map((s) => X(s.loop)), series.map((s) => s.mean / unitQ.factor), (v) => v, Y);
  const bands = bandPath(series.filter((s) => s.sigma !== null), X, Y, unitQ.factor);

  const shades = [];
  const marks = [];
  const rules = [];
  if (counts.ended && counts.loopsRequested && done < counts.loopsRequested) {
    // The loops that never ran, as the space they would have filled.
    const x0 = X(done + 0.5);
    shades.push({ x: x0, w: rect.x + rect.w - x0, hatch: true, colour: 'rule', opacity: 1 });
    rules.push({ x1: x0, colour: 'accent', width: 1.5 });
    marks.push({ x: x0 + 6, y: rect.y + 12, colour: 'accent-dark', weight: 500, size: 9.5,
      text: `loops ${done + 1} – ${counts.loopsRequested} not acquired` });
    marks.push({ x: x0 + 6, y: rect.y + 24, colour: 'accent-dark', size: 8.5,
      text: `${counts.ended} at loop ${done}` });
  }
  const last = series[series.length - 1];
  const loopTicks = axisTicks(X, { values: loopTickValues(total) });

  return {
    ...common,
    panels: [{
      ...panel,
      label: `Q per loop  ·  ${unitQ.label}  ·  loop →`,
      note: `zero-width axis · ${where} · repeats, not a curve`,
      noteColour: 'accent-dark',
      y: { scale: Y, ticks: axisTicks(Y, { count: 4 }) },
      series: [{
        key: 'mean', label: 'running mean', colour: 'accent', width: 1.6, d: meanPath,
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
      bands: bands ? [{ d: bands, colour: 'accent', opacity: 0.12 }] : [],
      dots, shades, rules, marks,
    }],
    x: {
      scale: X,
      ticks: loopTicks,
      label: `loop  →  ${counts.loopsRequested ? counts.loopsRequested + ' requested' : ''}`.trim(),
      format: (v) => `loop ${Math.round(v)}`,
    },
    legend: [
      { label: 'Q, each loop', marker: 'dot', colour: 'ink' },
      { label: 'running mean', colour: 'accent', width: 1.6 },
      { label: '± σ so far', marker: 'band', colour: 'accent' },
    ],
    readout: readoutRepeat(series, last, counts),
    notes: notesFor(counts, done, node, { repeat: true, firstLoop }),
    switch: { repeat: true, text: `zero-width axis · ${where}` },
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
  parts.push(last.sigma !== null ? `σ ${fmt.scientific(last.sigma, 3)} C` : 'σ_Q not recorded');
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
  const X = scale.linear(xd, [rect.x, rect.x + rect.w]);
  const all = points.flatMap((p) => p.q);
  const unitQ = chargeUnit([...all, ...points.map((p) => p.mean)]);
  const f = unitQ.factor;
  const bars = points.filter((p) => p.sigma !== null && p.mean !== null)
    .flatMap((p) => [(p.mean + p.sigma) / f, (p.mean - p.sigma) / f]);
  const domain = scale.extent([all.map((v) => v / f), points.map((p) => (p.mean === null ? NaN : p.mean / f)), bars],
    { includeZero: false }) || [-1, 1];
  const Y = scale.linear(domain, [rect.y + rect.h, rect.y]);

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
      rules.push({ x1: x, y1: scale.fixed(Y((p.mean + p.sigma) / f)), y2: scale.fixed(Y((p.mean - p.sigma) / f)), colour: 'ink', width: 1 });
    } else {
      dots.push({ x, y, r: 2.6, colour: 'ink', shape: 'square', hollow: true });
    }
  }
  const measured = points.filter((p) => p.mean !== null);
  const meanPath = scale.linePath(measured.map((p) => X(p.x)), measured.map((p) => p.mean / f), (v) => v, Y);
  const withSigma = points.filter((p) => p.sigma !== null).length;
  const current = currentPoint(node, points);

  return {
    ...common,
    panels: [{
      ...panel,
      label: `Q(${axis.name || 'axis'})  ·  ${unitQ.label}  ·  ${points.length} pts`,
      note: counts.loopsRequested
        ? `loop ${Math.min(counts.loopsDone + (current ? 1 : 0), counts.loopsRequested)} of ${counts.loopsRequested}`
        : `${counts.loopsDone} loops`,
      y: { scale: Y, ticks: axisTicks(Y, { count: 4 }) },
      series: [{
        key: 'mean', label: 'mean', colour: 'ink', width: 1.2, d: meanPath,
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
        x: scale.fixed(X(current.x)) + 4, y: rect.y + 11, colour: 'accent', size: 8.5, mono: true, halo: true,
        text: `now · point ${current.i + 1}`,
      }] : [],
      shades: current ? [{ x: scale.fixed(X(current.x)) - 1, w: 2, colour: 'accent', opacity: 0.5 }] : [],
    }],
    x: {
      scale: X,
      ticks: axisTicks(X, { count: 5 }),
      label: `${axis.name || 'axis'}${unit ? ' / ' + unit : ''}  →`,
      format: (v) => `${fmt.sig(v, 4)}${unit ? ' ' + unit : ''}`,
    },
    legend: [
      { label: 'mean over the loops so far', colour: 'ink', width: 1.2 },
      { label: 'σ_Q measured', marker: 'dot', colour: 'ink' },
      { label: 'σ_Q not recorded', marker: 'square-hollow', colour: 'ink' },
      { label: 'each shot', marker: 'dot-faint', colour: 'grey' },
    ],
    readout: readoutSweep(points, counts, withSigma),
    notes: notesFor(counts, counts.loopsDone, node, { repeat: false, withSigma, points: points.length }),
    switch: { repeat: false, text: `swept ${axis.name || 'axis'} · ${points.length} points` },
  };
}

/** The point the newest shot landed on, while the node is still running. */
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
  parts.push(withSigma ? `σ on ${withSigma}` : 'σ_Q not recorded yet — one loop');
  if (counts.ended) parts.push(counts.ended);
  return parts.join('  ·  ');
}

function notesFor(counts, loopsDone, node, { repeat, withSigma = 0, points = 0, firstLoop = 1 }) {
  const out = [];
  if (counts.loopsRequested) {
    // `ui-rules` §9: "100 loops requested, 20 completed."
    out.push(`${counts.loopsRequested} loop${counts.loopsRequested === 1 ? '' : 's'} requested, ${loopsDone} completed`
      + (counts.ended ? ` · ${counts.ended}` : ''));
  }
  if (!repeat && points && withSigma < points && loopsDone >= 1) {
    out.push(`σ_Q needs two loops at a point: ${points - withSigma} of ${points} still carry one`);
  }
  if (repeat && firstLoop > 1) {
    // The chart is a suffix, and says so rather than letting the empty space
    // before the first dot read as loops that never ran.
    out.push(`loops 1 – ${firstLoop - 1} ran before this console's view of them: the ring keeps the last shots, `
      + 'so their Q is not drawn — the mean and σ still cover every loop');
  }
  if (repeat) out.push('the mean and the ± σ band are the service\'s own, over every loop that ran — not recomputed from the shots this console holds');
  const gone = (node.shots || []).filter((s) => s.tracesGone).length;
  if (gone) out.push(`${gone} shot${gone === 1 ? '' : 's'} without traces — replayed or journalled; the point comes from the scalars`);
  return out;
}
