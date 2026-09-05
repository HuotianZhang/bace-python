// The transient: light and dark, their difference, and the running integral.
//
// `docs/ui-rules.md` §4 is most of the specification, and three of its rules
// are the reason this is not a line chart with a library:
//
//   * **light, dark and photocurrent belong together.** Showing the
//     photocurrent alone hides exactly the failure where both parents sit on
//     the digitiser's rail and the difference is identically zero — which is
//     what `verdict.rail_light` / `rail_dark` count, and what the shared
//     vertical window makes visible at a glance.
//   * **the running integral is worth the space**: `cumulative_charge_c`
//     saturates at Q, so the charge is where the curve flattens. It is the one
//     plot the LabVIEW panel had that the operator will look for.
//   * **the integration window is a region, not a number.** `t0_int` decides
//     which part of the transient is charge and which part is discarded, and a
//     window that starts after the transient is over is a run that measures
//     nothing while producing a plausible number.
//
// Two sources, one model. A live `StepDone` carries traces decimated for the
// wire; `GET /runs/{id}/data` carries them whole with the service's own
// `cumulative_q` beside them. `traceSet` folds both into record time, which is
// the axis the design's own chart uses and the axis `t0_int` is resolved into
// (`experiment/transient.py`: `record` → as given, `trigger` → minus the
// trace's `t0`, `pulse` → and plus `:PULS:DEL1`).

import * as scale from '../scale.js';
import * as fmt from '../format.js';
import { layout, axisTicks, heightFor, ASPECT } from './frame.js';

/** Where the integration window starts, **in record time** — the service's own arithmetic. */
export function windowRecordS(t0IntS, reference, { traceT0 = 0, pulseDelayS = 0 } = {}) {
  if (t0IntS === null || t0IntS === undefined) return null;
  if (reference === 'pulse') return t0IntS + pulseDelayS - traceT0;
  if (reference === 'trigger') return t0IntS - traceT0;
  return t0IntS;                                   // 'record', and the default
}

/**
 * The time of kept sample `i`, in record time.
 *
 * The last sample is **not** one stride after the one before it. The service
 * keeps the first sample, every k-th after that, and always the last one, so
 * with `n = 4000` and `stride = 5` the kept indices are 0, 5, … 3995, 3999 —
 * and a client that placed the tail one stride past 3995 would draw the whole
 * end of the record four samples early (`experiment/wire.py`).
 */
export function sampleTime(i, { dt, stride, n, kept }) {
  if (kept && i === kept - 1 && n) return (n - 1) * dt;
  return i * stride * dt;
}

/**
 * A store shot or a `GET /runs/{id}/data` body, as one shape in record time.
 *
 * `absent` is the two ways a shot arrives with no arrays — the ring dropped
 * them after a reconnect (`decimated[…].replay`), or the journal never stored
 * them (`decimated[…].omitted`). The store already folds both into
 * `tracesGone`; they differ only in the sentence, and both mean *no curve, the
 * loop point comes from the scalars* rather than *nothing happened*.
 */
export function traceSet(input, { t0_int_s = null, t0_int_reference = 'record', pulse_delay_s = 0 } = {}) {
  if (!input) return { absent: { text: 'no shot yet', detail: null } };

  // -- `GET /runs/{id}/data`: full precision, and the service's own integral.
  if (input.time_s && input.light) {
    const time = input.time_s;
    const last = input.last_shot || {};
    const row = (a) => (Array.isArray(a) && Array.isArray(a[0]) ? a[a.length - 1] : a);
    return {
      source: 'data',
      x: (i) => time[i],
      n: time.length,
      dt: input.dt || (time.length > 1 ? time[1] - time[0] : 0),
      light: last.light || row(input.light),
      dark: last.dark || row(input.dark),
      photo: last.photo || row(input.photo),
      cumulative: last.cumulative_q || null,
      cumulativeFrom: last.t0_int_record_s ?? null,
      window: last.t0_int_record_s ?? windowRecordS(t0_int_s, t0_int_reference, { pulseDelayS: pulse_delay_s }),
      windowSource: last.t0_int_record_source || null,
      stride: 1,
      nFull: time.length,
      q: last.q ?? null,
      q_mean: Array.isArray(input.q_mean) ? input.q_mean[input.q_mean.length - 1] : input.q_mean ?? null,
      q_std: Array.isArray(input.q_std) ? input.q_std[input.q_std.length - 1] : input.q_std ?? null,
      loopAveraged: true,
      verdict: null,
      title: last.index !== undefined ? `shot ${last.index}` : null,
    };
  }

  // -- a live `StepDone`, off the wire.
  if (input.tracesGone || !input.traces) {
    return {
      absent: {
        text: 'no traces for this shot',
        detail: input.tracesGone
          ? 'the ring replayed it without them, or the journal never stored them — '
            + 'the scalars are the shot, and the loop point comes from those'
          : null,
      },
      q: input.q ?? null, q_mean: input.q_mean ?? null, q_std: input.q_std ?? null,
    };
  }
  const t = input.traces;
  const env = t.light || t.dark || {};
  const info = t.decimated || {};
  const stride = (info['light.y'] && info['light.y'].stride) || 1;
  const nFull = (info['light.y'] && info['light.y'].n_full) || (env.n || 0);
  const light = (t.light && t.light.y) || null;
  const kept = light ? light.length : (t.photo || []).length;
  const dt = env.dt || 0;
  return {
    source: 'stream',
    x: (i) => sampleTime(i, { dt, stride, n: env.n || nFull, kept }),
    n: kept,
    dt,
    light,
    dark: (t.dark && t.dark.y) || null,
    photo: t.photo_averaged || t.photo || null,
    photoLabel: t.photo_averaged
      ? 'photocurrent = light − dark  ·  I / mA  ·  averaged over the loops so far'
      : null,
    cumulative: null,
    window: windowRecordS(t0_int_s, t0_int_reference, { traceT0: env.t0 ?? 0, pulseDelayS: pulse_delay_s }),
    windowSource: t0_int_reference ? `t0_int_reference = ${t0_int_reference}` : null,
    trigger: env.t0 === undefined || env.t0 === null ? null : -env.t0,
    stride,
    nFull,
    q: input.q ?? null,
    q_mean: input.q_mean ?? null,
    q_std: input.q_std ?? null,
    verdict: input.verdict || null,
    title: input.index === undefined ? null : `shot ${input.index} · loop ${input.loop}`,
  };
}

/**
 * The running integral of the photocurrent on screen, from the window.
 *
 * The service computes this properly and serves it on `GET /runs/{id}/data`;
 * live, there is no such field and the choice is to draw nothing or to draw
 * the *shape* from the trace that is already on the chart. It draws it, and
 * says so — the panel's caption names which of the two it is. What it never
 * does is call the endpoint a charge: Q is a number the service owns and the
 * event carries, and it is shown from the event beside this, not read off the
 * end of a curve integrated from a decimated copy of the trace.
 *
 * Trapezoidal, on the samples' own spacing, because the last kept sample is
 * not one stride from the one before it.
 */
export function runningIntegral(photo, x, from) {
  const out = new Array(photo.length).fill(null);
  let sum = 0;
  let started = -1;
  for (let i = 0; i < photo.length; i += 1) {
    const xi = x(i);
    if (from !== null && from !== undefined && xi < from) continue;
    if (started === -1) { started = i; out[i] = 0; continue; }
    const dx = xi - x(i - 1);
    const a = photo[i - 1];
    const b = photo[i];
    if (Number.isFinite(a) && Number.isFinite(b)) sum += ((a + b) / 2) * dx;
    out[i] = sum;
  }
  return { values: out, from: started };
}

const NS = 1e9;
const MA = 1e3;

/**
 * The chart model. Three panels on one x axis — never two y scales in one
 * panel, see `frame.js` — and every absence stated rather than drawn as a
 * value.
 */
/**
 * The panels an unrun slot stands for: the two every shot has. The running
 * integral is not among them — whether there is one depends on a window this
 * shot has not resolved yet.
 */
const ABSENT_PANELS = [
  { key: 'traces', weight: 1.25, label: 'light and dark  ·  I / mA' },
  { key: 'photo', weight: 1, label: 'photocurrent = light − dark  ·  I / mA' },
];

export function transientModel(input, options = {}) {
  const {
    width = 560, height = null, columns = null,
    t0_int_s = null, t0_int_reference = 'record', pulse_delay_s = 0,
    offset_corrected = null, dark_reference = null,
  } = options;
  const set = traceSet(input, { t0_int_s, t0_int_reference, pulse_delay_s });
  if (set.absent) {
    return {
      key: 'transient',
      // The slot keeps the shape of the chart that will land in it: the two
      // panels every shot has, named, over the axis they share. The running
      // integral is not drawn, because whether there is one depends on a
      // window this shot has not resolved yet.
      absent: {
        ...set.absent,
        frame: {
          width,
          height: height ?? heightFor(width, ABSENT_PANELS, { aspect: ASPECT.trace }),
          panels: ABSENT_PANELS,
          xLabel: 't / ns  →  record time',
        },
      },
      notes: [],
      panels: [],
    };
  }

  const photo = set.photo;
  const hasCumulative = Boolean(set.cumulative && set.cumulative.length);
  const derived = !hasCumulative && photo && set.window !== null && set.window !== undefined;
  const integral = derived ? runningIntegral(photo, set.x, set.window) : null;

  // Two of the notes under this chart were settings restated as prose, about a
  // curve drawn a few pixels above them. They belong **on** the panel they
  // define: `dark_reference` is what the photocurrent *is* (`ui-rules` §7 —
  // `translated` and `same` are not interchangeable), and the offset
  // correction is what the light and dark baselines have already had done to
  // them. A reader who has to carry a line from below the axis back up to the
  // trace has been given a footnote where a label was wanted.
  const panels = [
    { key: 'traces', weight: 1.25, label: 'light and dark  ·  I / mA' + offsetClause(offset_corrected) },
    // `suffix`, not a baked-in label: `photoPanel` swaps the whole label for
    // `set.photoLabel` when the run stored loop averages, and a clause written
    // into the base label would be dropped in exactly that case.
    { key: 'photo', weight: 1, label: 'photocurrent = light − dark  ·  I / mA', suffix: darkClause(dark_reference) },
  ];
  if (hasCumulative || derived) {
    panels.push({ key: 'charge', weight: 0.8, label: 'running integral  ·  Q / C' });
  }
  // The height is the aspect's, not a number chosen here: three panels of a
  // quantity against time, each at `ASPECT.trace` for its weight.
  const frame = layout({ width, height: height ?? heightFor(width, panels, { aspect: ASPECT.trace }), panels });
  const plot = frame.panels[0].rect;
  const cols = columns || Math.round(plot.w);

  const xEnd = set.n ? set.x(set.n - 1) : 0;
  const X = scale.linear([0, xEnd * NS], [plot.x, plot.x + plot.w]);
  const xTicks = axisTicks(X, { count: 5 });
  const at = (values, factor) => (xValue) => {
    if (!values) return null;
    const i = nearest(set, xValue / NS);
    return i < 0 ? null : { y: values[i] * factor };
  };

  const built = [];
  for (const panel of frame.panels) {
    if (panel.key === 'traces') built.push(tracesPanel(panel, set, X, cols, at));
    if (panel.key === 'photo') built.push(photoPanel(panel, set, X, cols, at));
    if (panel.key === 'charge') {
      built.push(chargePanel(panel, set, X, cols, at, hasCumulative ? set.cumulative : integral.values,
        hasCumulative));
    }
  }

  // The window spans every panel: it is one region of time, and shading it in
  // the photocurrent alone would suggest the light and dark traces are
  // integrated over something else.
  if (set.window !== null && set.window !== undefined) {
    const x0 = X(set.window * NS);
    for (const panel of built) {
      panel.shades = [{ x: x0, w: plot.x + plot.w - x0, colour: 'accent', opacity: 0.055 }];
      panel.rules = [...(panel.rules || []), { x1: x0, colour: 'accent', width: 1.5 }];
    }
    built[0].marks = [...(built[0].marks || []), {
      x: x0 + 5, y: built[0].rect.y + 12, text: 't_0,int', colour: 'accent', weight: 600, mono: true, halo: true,
    }, {
      x: x0 + 5, y: built[0].rect.y + 22, halo: true,
      text: `${fmt.sig(set.window * NS, 4)} ns of record time`, colour: 'accent', size: 8.5,
    },
    // Where the window came from, beside the window — it was a note under the
    // chart, which is the one place it cannot be read against the line it dates.
    ...(set.windowSource ? [{
      x: x0 + 5, y: built[0].rect.y + 32, halo: true,
      text: set.windowSource, colour: 'grey', size: 8.5,
    }] : [])];
  }
  if (set.trigger !== null && set.trigger !== undefined && set.trigger > 0) {
    const x0 = X(set.trigger * NS);
    for (const panel of built) {
      panel.rules = [...(panel.rules || []), { x1: x0, colour: 'grey', width: 1, dash: '3 3' }];
    }
    built[0].marks = [...(built[0].marks || []), {
      x: x0 - 5, y: built[0].rect.y + 12, text: 'trigger', colour: 'grey', anchor: 'end', size: 8.5, halo: true,
    }];
  }

  return {
    key: 'transient',
    width: frame.width,
    height: frame.height,
    margin: frame.margin,
    panels: built,
    x: {
      scale: X,
      ticks: xTicks,
      label: 't / ns  →  record time',
      format: (v) => `${fmt.sig(v, 4)} ns`,
    },
    legend: [
      { label: 'light', colour: 'ink', width: 1.4 },
      { label: 'dark', colour: 'grey', width: 1.4 },
      { label: 'photocurrent', colour: 'ink', width: 1.4 },
      ...(hasCumulative || derived ? [{ label: 'running integral', colour: 'accent', width: 1.6, dash: '3 2' }] : []),
    ],
    // The readout row belongs to the crosshair, and at rest it used to restate
    // Q, the running mean and σ — the same three numbers the shot block prints
    // above the chart, there with the shot and the point they belong to. One
    // statement of a number, in the place that can say which number it is.
    readout: '',
    notes: notes(set, { hasCumulative, derived }),
  };
}

function nearest(set, xValue) {
  if (!set.n) return -1;
  const span = set.x(set.n - 1);
  if (!(span > 0)) return 0;
  const i = Math.round((xValue / span) * (set.n - 1));
  return Math.min(set.n - 1, Math.max(0, i));
}

function tracesPanel(panel, set, X, cols, at) {
  // One vertical window for both traces, because that is what the digitiser
  // had: the autorange runs on the light trace only and the dark trace
  // inherits the range (`ui-rules` §7). Scaling them independently would draw
  // two traces that look comparable and are not.
  const domain = scale.extent([set.light, set.dark], { includeZero: true }) || [-1, 1];
  const Y = scale.linear([domain[0] * MA, domain[1] * MA], [panel.rect.y + panel.rect.h, panel.rect.y]);
  const series = [];
  if (set.dark) {
    series.push({
      key: 'dark', label: 'dark', colour: 'grey', width: 1, opacity: 0.75,
      d: scale.envelopePath(set.dark.map((v) => v * MA), (i) => X(set.x(i) * NS), Y, { columns: cols }),
      at: at(set.dark, MA), format: (v) => `${fmt.sig(v, 3)} mA`,
    });
  }
  if (set.light) {
    series.push({
      key: 'light', label: 'light', colour: 'ink', width: 1.1,
      d: scale.envelopePath(set.light.map((v) => v * MA), (i) => X(set.x(i) * NS), Y, { columns: cols }),
      at: at(set.light, MA), format: (v) => `${fmt.sig(v, 3)} mA`,
    });
  }
  return {
    ...panel,
    note: railNote(set),
    noteColour: set.verdict && set.verdict.shared_extreme ? 'alert' : 'grey',
    y: { scale: Y, ticks: axisTicks(Y, { count: 3 }) },
    series,
  };
}

function photoPanel(panel, set, X, cols, at) {
  const domain = scale.extent([set.photo], { includeZero: true }) || [-1, 1];
  const Y = scale.linear([domain[0] * MA, domain[1] * MA], [panel.rect.y + panel.rect.h, panel.rect.y]);
  return {
    ...panel,
    label: (set.photoLabel || panel.label) + (panel.suffix || ''),
    note: 'Q = ∫ (light − dark) dt',
    y: { scale: Y, ticks: axisTicks(Y, { count: 3 }) },
    series: set.photo ? [{
      key: 'photo', label: 'photocurrent', colour: 'ink', width: 1.3,
      d: scale.envelopePath(set.photo.map((v) => v * MA), (i) => X(set.x(i) * NS), Y, { columns: cols }),
      at: at(set.photo, MA), format: (v) => `${fmt.sig(v, 3)} mA`,
    }] : [],
  };
}

function chargePanel(panel, set, X, cols, at, values, fromService) {
  // The service's `cumulative_q` starts at the window, not at the record, so
  // its index 0 is `t0_int_record_s`. A curve drawn from the record's start
  // would put the whole integral one window too early.
  const offset = fromService ? indexOf(set, set.cumulativeFrom ?? set.window) : 0;
  const finite = values.filter((v) => Number.isFinite(v));
  const domain = scale.extent([finite], { includeZero: true }) || [-1, 1];
  const Y = scale.linear(domain, [panel.rect.y + panel.rect.h, panel.rect.y]);
  const xOf = (i) => X(set.x(i + offset) * NS);
  return {
    ...panel,
    label: fromService
      ? 'running integral  ·  Q / C  ·  the run\'s own record'
      : 'running integral  ·  Q / C  ·  computed from the trace above',
    note: fromService ? null : 'not the run\'s Q — that is the number beside the chart',
    y: { scale: Y, ticks: axisTicks(Y, { count: 3 }) },
    series: [{
      key: 'cumulative', label: 'running integral', colour: 'accent', width: 1.4, dash: '3 2',
      d: scale.envelopePath(values, xOf, Y, { columns: cols }),
      at: (xValue) => {
        const i = nearest(set, xValue / NS) - offset;
        return i < 0 || i >= values.length || !Number.isFinite(values[i]) ? null : { y: values[i] };
      },
      format: (v) => fmt.charge(v),
    }],
  };
}

function indexOf(set, recordS) {
  if (recordS === null || recordS === undefined || !set.n) return 0;
  const span = set.x(set.n - 1);
  if (!(span > 0)) return 0;
  return Math.max(0, Math.round((recordS / span) * (set.n - 1)));
}

/** The autorange verdict, where the shared vertical window makes it readable. */
function railNote(set) {
  const v = set.verdict;
  if (!v) return 'shared vertical window · autorange from light only';
  if (v.shared_extreme) return 'both traces at an extreme — the difference may be identically zero';
  const rails = (v.rail_light || 0) + (v.rail_dark || 0);
  return `autorange pass ${v.autorange_passes ?? '—'} · ${rails} rail sample${rails === 1 ? '' : 's'}`;
}

function readoutLine(set) {
  const parts = [];
  if (set.q !== null && set.q !== undefined) parts.push(`Q ${fmt.charge(set.q)}`);
  if (set.q_mean !== null && set.q_mean !== undefined) parts.push(`mean ${fmt.charge(set.q_mean)}`);
  const sigma = fmt.sigmaQ(set.q_std);
  parts.push(sigma ? `σ ${sigma} C` : 'σ_Q not recorded');
  return parts.join('  ·  ');
}

/** `dark_reference`, as the clause that says what the photocurrent curve is. */
function darkClause(dark_reference) {
  if (!dark_reference) return '';
  return dark_reference === 'same'
    ? '  ·  dark held at the light levels'
    : '  ·  dark translated to zero';
}

/** What has already been taken off the light and dark baselines, if anything. */
function offsetClause(offset_corrected) {
  if (offset_corrected === true) return '  ·  offset corrected, last 10 % of the record';
  if (offset_corrected === false) return '  ·  no offset correction';
  return '';
}

/**
 * What is left for the notes: the two statements that are about the **data**
 * rather than about any one panel.
 *
 * Everything else moved onto the thing it describes. `dark_reference` and the
 * offset correction became panel labels; `windowSource` became the third line
 * of the `t_0,int` mark, beside the window it dates; and the two "the running
 * integral is …" lines were already the charge panel's own label, printed
 * again four pixels below it.
 */
function notes(set, { hasCumulative, derived }) {
  const out = [];
  if (set.stride > 1) {
    out.push(`decimated for the wire · 1 in ${set.stride} of ${set.nFull} · the file keeps full precision`);
  }
  if (set.loopAveraged) out.push('traces are the loop averages the run stored, not one shot');
  // The one case with no panel to label, because the panel is not drawn at all.
  if (!hasCumulative && !derived) {
    out.push('no integration window: t0_int is unresolved, so there is no running integral to draw');
  }
  return out;
}
