// The transient chart shows light trace, dark trace, their difference and the running
// integral. A shared view exposes traces at the digitiser's rail even if their
// difference is zero. The running integral approaches Q, and the integration-window
// region shows which current contributes (`docs/ui-rules.md` §4). Live data is
// decimated; the data endpoint supplies full traces and `cumulative_q`. `traceSet`
// normalizes both to record time. The window starts at field arrival plus `t0_int_s`
// minus trace origin, with width `t_int_width_s` (manual 2.2). Reported
// `t0_int_record_s` and `t1_int_record_s` take precedence over form arithmetic.

import * as scale from '../scale.js';
import * as fmt from '../format.js';
import { layout, axisTicks, heightFor, ASPECT, MAX_UNIT, PLOT_MARGIN } from './frame.js';
import { chargeUnit } from './loops.js';
import { ranged } from './view.js';

/**
 * The helper resolves integration-window start in record time. `pulseDelayS` includes
 * programmed delay and `trigger_offset_s`, as supplied by `pulseDelayS` in
 * `ui/lib/results.js` (manual 2.2).
 */
export function windowRecordS(t0IntS, { traceT0 = 0, pulseDelayS = 0 } = {}) {
  if (t0IntS === null || t0IntS === undefined) return null;
  return t0IntS + pulseDelayS - traceT0;
}

/**
 * The window ends `t_int_width_s` after its start, or is `null` if either input is
 * unknown.
 */
export function windowEndRecordS(startS, widthS) {
  if (startS === null || startS === undefined) return null;
  if (widthS === null || widthS === undefined || !(widthS > 0)) return null;
  return startS + widthS;
}

/**
 * The helper places retained sample `i` in record time. Decimation always retains the
 * last sample (`docs/service-contract.md` §3). For 4000 samples at stride 5, the
 * final indices are 3995 and 3999; assuming a full final stride misplaces the tail.
 */
export function sampleTime(i, { dt, stride, n, kept }) {
  if (kept && i === kept - 1 && n) return (n - 1) * dt;
  return i * stride * dt;
}

/**
 * The helper normalizes a store shot or data-endpoint payload to record time. Ring
 * replay can drop arrays and journals omit them; both set `tracesGone` but need
 * distinct explanations. Missing curves still leave scalar loop points available
 * (`docs/service-contract.md` §3).
 */
export function traceSet(input, { t0_int_s = null, t_int_width_s = null, pulse_delay_s = 0 } = {}) {
  if (!input) return { absent: { text: 'no shot yet', detail: null } };
  const source = (start) => (start === null || start === undefined ? null
    : `pinned to the pulse · ${fmt.sig(pulse_delay_s * 1e9, 4)} ns after the trigger`);

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
      window: last.t0_int_record_s ?? windowRecordS(t0_int_s, { pulseDelayS: pulse_delay_s }),
      windowEnd: last.t1_int_record_s
        ?? windowEndRecordS(last.t0_int_record_s ?? windowRecordS(t0_int_s, { pulseDelayS: pulse_delay_s }), t_int_width_s),
      windowSource: last.t0_int_record_source || (last.t0_int_record_s !== undefined && last.t0_int_record_s !== null
        ? 'the window the run integrated' : source(t0_int_s)),
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
    // The reported integration window takes precedence; form arithmetic supplies a
    // fallback only without it.
    window: input.t0_int_record_s
      ?? windowRecordS(t0_int_s, { traceT0: env.t0 ?? 0, pulseDelayS: pulse_delay_s }),
    windowEnd: input.t1_int_record_s
      ?? windowEndRecordS(windowRecordS(t0_int_s, { traceT0: env.t0 ?? 0, pulseDelayS: pulse_delay_s }), t_int_width_s),
    windowSource: input.t0_int_record_s !== undefined && input.t0_int_record_s !== null
      ? 'the window the run integrated' : source(t0_int_s),
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
 * The live running integral shows the shape computed from the displayed, decimated
 * trace. Its caption distinguishes it from the full-precision service integral. Its
 * endpoint must not replace Q, which comes from the service event. Trapezoidal
 * integration uses each sample's spacing because the last retained interval can be
 * shorter than the stride.
 */
export function runningIntegral(photo, x, from, to = null) {
  const out = new Array(photo.length).fill(null);
  let sum = 0;
  let started = -1;
  for (let i = 0; i < photo.length; i += 1) {
    const xi = x(i);
    if (from !== null && from !== undefined && xi < from) continue;
    if (to !== null && to !== undefined && xi > to) break;
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

/** The stack's height at a given width. */
const stackHeight = (width, panels) => heightFor(width, panels,
  { margin: PLOT_MARGIN, aspect: ASPECT.stack, cap: MAX_UNIT.stack });

/**
 * The model uses three panels on one x axis to avoid ambiguous dual y scales. Missing
 * data remains an explicit absence.
 */
/**
 * The empty frame reserves the two panels every shot has. An integral panel requires
 * a resolved integration window and is omitted before that exists.
 */
const ABSENT_PANELS = [
  { key: 'traces', weight: 1.25, label: 'Current (mA)' },
  { key: 'photo', weight: 1, label: 'Light − dark (mA)' },
];

export function transientModel(input, options = {}) {
  const {
    width = 560, height = null, columns = null,
    t0_int_s = null, t_int_width_s = null, pulse_delay_s = 0,
    offset_corrected = null, offset_region = null, dark_reference = null, view = null,
  } = options;
  const set = traceSet(input, { t0_int_s, t_int_width_s, pulse_delay_s });
  if (set.absent) {
    return {
      key: 'transient',
      // The empty slot retains the trace panels and shared axis. The integral cannot
      // appear before a shot resolves its integration window.
      absent: {
        ...set.absent,
        frame: {
          width,
          height: height ?? stackHeight(width, ABSENT_PANELS),
          panels: ABSENT_PANELS,
          margin: PLOT_MARGIN,
          xLabel: 'Time (ns)',
        },
      },
      notes: [],
      panels: [],
    };
  }

  const photo = set.photo;
  const hasCumulative = Boolean(set.cumulative && set.cumulative.length);
  const derived = !hasCumulative && photo && set.window !== null && set.window !== undefined;
  const integral = derived ? runningIntegral(photo, set.x, set.window, set.windowEnd) : null;

  // `dark_reference` and baseline correction define the plotted current, so they
  // label its panel rather than sit below the chart. `translated` and `same` are not
  // interchangeable (`docs/ui-rules.md` §7).
  const panels = [
    { key: 'traces', weight: 1.25, label: 'light and dark  ·  I / mA' + offsetClause(offset_corrected, offset_region) },
    // The clause uses `suffix` because `photoPanel` can replace the base label with
    // `set.photoLabel` for loop averages. A clause stored only in the base label
    // would disappear.
    { key: 'photo', weight: 1, label: 'photocurrent = light − dark  ·  I / mA', suffix: darkClause(dark_reference) },
  ];
  if (hasCumulative || derived) {
    panels.push({ key: 'charge', weight: 0.8, label: 'running integral  ·  Q / C' });
  }
  // `ASPECT.stack` and panel weights set the height so time traces keep consistent
  // proportions.
  const frame = layout({
    width, panels, margin: PLOT_MARGIN,
    height: height ?? stackHeight(width, panels),
  });
  const plot = frame.panels[0].rect;
  const cols = columns || Math.round(plot.w);

  const xEnd = set.n ? set.x(set.n - 1) : 0;
  const X = scale.linear(ranged(view, 'x', 'ns', [0, xEnd * NS]), [plot.x, plot.x + plot.w]);
  const xTicks = axisTicks(X, { count: width > 520 ? 10 : 5 });
  // Only the samples the x axis shows go into the paths: thinning the whole record
  // would leave a zoom with fewer columns than it has pixels.
  const span = {
    from: Math.max(0, nearest(set, X.domain[0] / NS) - 1),
    to: Math.min(set.n, nearest(set, X.domain[1] / NS) + 2),
  };
  const at = (values, factor) => (xValue) => {
    if (!values) return null;
    const i = nearest(set, xValue / NS);
    return i < 0 ? null : { y: values[i] * factor };
  };

  const built = [];
  for (const panel of frame.panels) {
    if (panel.key === 'traces') built.push(tracesPanel(panel, set, X, cols, at, span, view));
    if (panel.key === 'photo') built.push(photoPanel(panel, set, X, cols, at, span, view));
    if (panel.key === 'charge') {
      built.push(chargePanel(panel, set, X, cols, at, hasCumulative ? set.cumulative : integral.values,
        hasCumulative, span, view));
    }
  }
  built.forEach((panel, i) => { panel.letter = 'abc'[i]; });
  built[0].keys = [
    ...(set.light ? [{ label: 'Light', colour: 'ink' }] : []),
    ...(set.dark ? [{ label: 'Dark', colour: 'grey', width: 1.6 }] : []),
  ];

  // The integration window is the same time region for all traces, so its edges cross
  // every panel. Thin lines leave the traces in front.
  const windowNote = [];
  if (set.window !== null && set.window !== undefined) {
    const x0 = X(set.window * NS);
    // Both integration-window edges move with the pulse. A window extending beyond
    // the record has no end line.
    const endS = set.windowEnd;
    const plotRight = plot.x + plot.w;
    const ended = endS !== null && endS !== undefined && endS * NS <= xEnd * NS;
    const x1 = ended ? X(endS * NS) : plotRight;
    for (const panel of built) {
      panel.rules = [...(panel.rules || []), { x1: x0, colour: 'grey', width: 0.9, dash: '3 3' },
        ...(ended ? [{ x1, colour: 'grey', width: 0.9, dash: '3 3' }] : [])];
    }
    built[0].marks = [...(built[0].marks || []), {
      x: (x0 + x1) / 2, y: built[0].rect.y + built[0].rect.h - 7, anchor: 'middle',
      text: 'integration window', colour: 'grey', weight: 400, size: 10, halo: true,
    }];
    windowNote.push(`integration window ${fmt.sig(set.window * NS, 4)} ns of record time`
      + (endS === null || endS === undefined ? '' : ` → ${fmt.sig(endS * NS, 4)} ns`)
      + (set.windowSource ? `, ${set.windowSource}` : ''));
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
      title: 'Time (ns)',
      unit: 'ns',
      format: (v) => `${fmt.sig(v, 4)} ns`,
    },
    legend: [],
    // The readout row serves the crosshair. At rest it leaves Q, running mean and
    // uncertainty in the shot block, where their shot and point are identified.
    readout: '',
    notes: [...windowNote, ...notes(set, { hasCumulative, derived })],
  };
}

function nearest(set, xValue) {
  if (!set.n) return -1;
  const span = set.x(set.n - 1);
  if (!(span > 0)) return 0;
  const i = Math.round((xValue / span) * (set.n - 1));
  return Math.min(set.n - 1, Math.max(0, i));
}

function tracesPanel(panel, set, X, cols, at, span, view) {
  // Light and dark share a vertical scale because autorange runs on the light trace
  // and the dark trace inherits that range (`docs/ui-rules.md` §7). Separate scaling
  // would conceal a mismatch.
  const domain = scale.extent([set.light, set.dark], { includeZero: true }) || [-1, 1];
  const Y = scale.linear(ranged(view, panel.key, 'mA', [domain[0] * MA, domain[1] * MA]),
    [panel.rect.y + panel.rect.h, panel.rect.y]);
  const series = [];
  if (set.dark) {
    series.push({
      key: 'dark', label: 'dark', colour: 'grey', width: 1.4,
      d: scale.envelopePath(set.dark.map((v) => v * MA), (i) => X(set.x(i) * NS), Y, { columns: cols, ...span }),
      at: at(set.dark, MA), format: (v) => `${fmt.sig(v, 3)} mA`,
    });
  }
  if (set.light) {
    series.push({
      key: 'light', label: 'light', colour: 'ink', width: 1.15,
      d: scale.envelopePath(set.light.map((v) => v * MA), (i) => X(set.x(i) * NS), Y, { columns: cols, ...span }),
      at: at(set.light, MA), format: (v) => `${fmt.sig(v, 3)} mA`,
    });
  }
  return {
    ...panel,
    note: railNote(set),
    noteColour: set.verdict && set.verdict.shared_extreme ? 'alert' : 'grey',
    y: { scale: Y, ticks: axisTicks(Y, { count: 4 }), title: 'Current (mA)', unit: 'mA' },
    series,
  };
}

function photoPanel(panel, set, X, cols, at, span, view) {
  const domain = scale.extent([set.photo], { includeZero: true }) || [-1, 1];
  const Y = scale.linear(ranged(view, panel.key, 'mA', [domain[0] * MA, domain[1] * MA]),
    [panel.rect.y + panel.rect.h, panel.rect.y]);
  return {
    ...panel,
    label: (set.photoLabel || panel.label) + (panel.suffix || ''),
    note: 'Q = ∫ (light − dark) dt',
    y: { scale: Y, ticks: axisTicks(Y, { count: 3 }), title: 'Light − dark (mA)', unit: 'mA' },
    series: set.photo ? [{
      key: 'photo', label: 'photocurrent', colour: 'photo', width: 1.3,
      d: scale.envelopePath(set.photo.map((v) => v * MA), (i) => X(set.x(i) * NS), Y, { columns: cols, ...span }),
      at: at(set.photo, MA), format: (v) => `${fmt.sig(v, 3)} mA`,
    }] : [],
  };
}

function chargePanel(panel, set, X, cols, at, values, fromService, span, view) {
  // Service `cumulative_q` begins at `t0_int_record_s`, not the record origin.
  // Starting at the record origin would shift the entire integral.
  const offset = fromService ? indexOf(set, set.cumulativeFrom ?? set.window) : 0;
  const finite = values.filter((v) => Number.isFinite(v));
  const unitQ = chargeUnit(finite);
  const f = unitQ.factor;
  const domain = scale.extent([finite.map((v) => v / f)], { includeZero: true }) || [-1, 1];
  const Y = scale.linear(ranged(view, panel.key, unitQ.unit, domain), [panel.rect.y + panel.rect.h, panel.rect.y]);
  const xOf = (i) => X(set.x(i + offset) * NS);
  return {
    ...panel,
    label: fromService
      ? 'running integral  ·  Q / C  ·  the run\'s own record'
      : 'running integral  ·  Q / C  ·  computed from the trace above',
    note: fromService ? null : 'From the decimated trace; not the run\'s Q, which is beside the chart',
    y: { scale: Y, ticks: axisTicks(Y, { count: 3 }), title: `Charge (${unitQ.unit})`, unit: unitQ.unit },
    series: [{
      key: 'cumulative', label: 'running integral', colour: 'integral', width: 1.3,
      d: scale.envelopePath(values.map((v) => (Number.isFinite(v) ? v / f : v)), xOf, Y, {
        columns: cols,
        from: Math.max(0, span.from - offset),
        to: Math.min(values.length, Math.max(0, span.to - offset)),
      }),
      at: (xValue) => {
        const i = nearest(set, xValue / NS) - offset;
        return i < 0 || i >= values.length || !Number.isFinite(values[i]) ? null : { y: values[i] / f };
      },
      format: (v) => fmt.charge(v * f),
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

/** The label identifies baseline correction applied to the traces. */
function offsetClause(offset_corrected, offset_region = null) {
  if (offset_corrected === true && offset_region === 'pre-trigger') return '  ·  offset corrected, before the trigger';
  if (offset_corrected === true) return '  ·  offset corrected, last 10 % of the record';
  if (offset_corrected === false) return '  ·  no offset correction';
  return '';
}

/**
 * The notes describe data-wide constraints. Panel-specific facts belong in labels:
 * dark reference and baseline correction on the current panel, window provenance
 * beside the boundary, and integral source on the Q panel.
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
