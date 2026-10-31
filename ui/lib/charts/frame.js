// The chart frame renders pure models as SVG with shared axes, captions and a
// crosshair. `ui/lib/scale.js` supplies the non-DOM geometry. Keeping model functions
// pure permits tests without a browser. The running integral has a separate panel on
// the transient's x axis: two y scales would create arbitrary crossings and obscure
// which scale a curve uses (`docs/ui-rules.md` §4).
//
// A panel whose `y` carries a `title` is a plot: a closed frame with inward ticks on
// all four sides, no gridlines, and its unit in the axis title. Its label and note go
// to the chart's info tip. Panels without a title are diagrams (timing, schedule) and
// keep their header lines.

import { s, root } from '../svg.js';
import { h, fill } from '../dom.js';
import * as scale from '../scale.js';
import { closeAxes, openAxes, publish } from './axes.js';
import { isManual, resetView, setRange, viewOf } from './view.js';

/** A model colour is a `style.css` token name, or a literal hex from a ramp. */
export function paint(colour) {
  return !colour ? 'none' : colour[0] === '#' ? colour : `var(--${colour})`;
}

export const FONT_S = "var(--font-body)";

/** The surround uses model units and one shared default. */
export const MARGIN = { left: 56, right: 16, top: 16, bottom: 26, gap: 22 };

/** The surround of a plot: room for tick labels and titles outside its frame. */
export const PLOT_MARGIN = { left: 60, right: 16, top: 10, bottom: 42, gap: 7 };

/**
 * The aspect ratios describe plot width divided by height for a panel of weight 1.
 * Time traces need length to resolve thousands of samples; quantity-against-quantity
 * curves need a stable shape to preserve apparent slopes. Strips show a time interval
 * through its ends and excursions. Panel weights retain deliberate height
 * differences, including a half-height running integral. Layout measurements on this
 * bench span 2.1:1 to 12.3:1 without a shared rule; the reference panels span 5.2:1
 * to 6.8:1, with `trace` near their middle.
 */
export const ASPECT = {
  trace: 6,     // a lane of time — the timing diagram
  stack: 3.6,   // a quantity against time, one of a stack — the transient's panels
  curve: 1.6,   // a quantity against another quantity — J–V, Q(axis), the grid
  strip: 11,    // a length of time seen whole — the power monitor, the schedule
};

/**
 * The tallest a panel of weight 1 grows, by family. A plot in a full-width view would
 * otherwise push everything below it off the screen.
 */
export const MAX_UNIT = { stack: 130, curve: 320 };

/**
 * The helper derives chart height from panel aspects and width. Resizing preserves
 * the plot proportions in every container.
 */
export function heightFor(width, panels, { margin = {}, aspect = ASPECT.trace, cap = Infinity } = {}) {
  const m = { ...MARGIN, ...margin };
  const total = panels.reduce((sum, p) => sum + (p.weight || 1), 0);
  const plot = Math.max(1, width - m.left - m.right);
  return Math.round(Math.min(plot / aspect, cap) * total + m.top + m.bottom + m.gap * (panels.length - 1));
}

/**
 * The layout stacks weighted panels over one x axis. Relative weights allow a
 * half-height integral without caller-side pixel arithmetic; the shared axis appears
 * below the last panel.
 */
export function layout({ width, height, panels, margin = {} }) {
  const m = { ...MARGIN, ...margin };
  const total = panels.reduce((sum, p) => sum + (p.weight || 1), 0);
  const free = height - m.top - m.bottom - m.gap * (panels.length - 1);
  const w = width - m.left - m.right;
  const out = [];
  let y = m.top;
  for (const panel of panels) {
    const ph = (free * (panel.weight || 1)) / total;
    out.push({ ...panel, rect: { x: m.left, y, w, h: ph } });
    y += ph + m.gap;
  }
  return { width, height, margin: m, panels: out };
}

/**
 * The renderer builds the SVG, legend, notes and crosshair readout. A stationary
 * readout lets the operator compare values while moving the pointer and cannot cover
 * the trace. Its empty row retains height to prevent layout jumps.
 */
export function chart(model, { id = null } = {}) {
  if (typeof model !== 'function') {
    const figure = h('figure.chart', { dataset: { chart: model.key || '' } });
    return drawInto(figure, model);
  }
  return responsive(model, id);
}

/**
 * Container widths round before model construction to avoid a rebuild for every pixel
 * during resizing. Four units are below half a stroke at the measurement scale used
 * here.
 */
const WIDTH_STEP = 4;

/** The minimum width prevents axis-label collisions at any supported aspect. */
const MIN_WIDTH = 240;

/**
 * The responsive chart builds its model at the measured container width. `svg.root()`
 * scales its `viewBox` with `width: 100%`, so an arbitrary model width also scales
 * text. Measurements on this bench put a nominal 9.5 px label between 8.6 px and 12.9
 * px across containers. Matching model and container widths keeps that text at 9.5
 * px. The figure stays empty before the observer's first width rather than drawing a
 * guessed layout. With an `id` the chart has a view (`view.js`) that `make` receives
 * and that survives the rebuild of a live card.
 */
function responsive(make, id) {
  const figure = h('figure.chart');
  const view = id ? viewOf(id) : null;
  let drawn = null;
  const redraw = () => {
    const model = make(drawn, view);
    figure.dataset.chart = model.key || '';
    const ctx = id ? { id, view, redraw, axes: () => axesOf(model) } : null;
    drawInto(figure, model, ctx);
    if (ctx) publish(ctx);
  };
  const draw = (raw) => {
    const width = Math.max(MIN_WIDTH, Math.round(raw / WIDTH_STEP) * WIDTH_STEP);
    if (width === drawn) return;
    drawn = width;
    redraw();
  };
  if (typeof ResizeObserver === 'function') {
    // The container sets figure width and the SVG fills it. Painting cannot change
    // the observed width, so no resize feedback guard is needed.
    new ResizeObserver((entries) => {
      const w = entries[entries.length - 1].contentRect.width;
      if (w > 0) draw(w);
    }).observe(figure);
  } else {
    draw(MIN_WIDTH);
  }
  return figure;
}

function drawInto(figure, model, ctx = null) {
  if (model.absent) {
    fill(figure, absent(model.absent));
    return figure;
  }
  const readout = h('div.chart-readout', { text: model.readout || '' });
  const svg = renderSvg(model, readout, ctx);
  const plot = isPlot(model);
  const shown = plot ? alertsOf(model) : model.notes || [];
  fill(figure, plot ? tools(model, ctx) : null, svg,
    model.legend && model.legend.length ? legend(model.legend) : null,
    readout,
    shown.length
      ? h('div.chart-notes', shown.map((n) => h('span.chart-note', { text: n })))
      : null);
  return figure;
}

const isPlot = (model) => model.panels.some((p) => p.y && p.y.title);

/** What the info tip says: every caption that a plot no longer prints. */
export function detailsOf(model) {
  const out = [];
  for (const panel of model.panels) {
    if (panel.label) out.push(panel.label);
    if (panel.note && panel.noteColour !== 'alert') out.push(panel.note);
  }
  const alerts = model.alerts || [];
  return [...out, ...(model.notes || []).filter((n) => !alerts.includes(n))];
}

/** What stays on screen under a plot: warnings, not explanations. */
export function alertsOf(model) {
  return [
    ...model.panels.filter((p) => p.note && p.noteColour === 'alert').map((p) => p.note),
    ...(model.alerts || []),
  ];
}

/** The axes an operator can set: the shared x, then each panel's y. */
export function axesOf(model) {
  const out = [];
  if (model.x && model.x.title && !model.x.fixed) {
    out.push({ key: 'x', title: model.x.title, unit: model.x.unit, scale: model.x.scale });
  }
  for (const panel of model.panels) {
    if (!panel.y || !panel.y.title) continue;
    out.push({
      key: panel.key, title: panel.y.title, unit: panel.y.unit, scale: panel.y.scale,
      canLog: panel.y.canLog === true, log: panel.y.scale.kind === 'log10',
    });
  }
  return out;
}

function tools(model, ctx) {
  const info = detailsOf(model);
  const manual = ctx && isManual(ctx.view);
  return h('div.chart-tools',
    info.length ? h('span.chart-info', { tabindex: 0, title: info.join('\n'), text: 'i' }) : null,
    manual ? h('button.chart-tool', {
      type: 'button',
      title: 'Back to automatic ranges',
      onclick: () => { resetView(ctx.view); closeAxes(); ctx.redraw(); },
    }, 'Auto') : null,
    ctx ? h('button.chart-tool', {
      type: 'button', class: manual ? 'on' : '', 'aria-haspopup': 'dialog',
      title: 'Set the axis ranges. Drag on the plot to zoom, double-click to go back.',
      onclick: (e) => openAxes(e.currentTarget, ctx.id),
    }, 'Axes') : null);
}

/**
 * The empty state reserves the chart's footprint so the card keeps one geometry
 * before and after measurement. A model with `frame` shows dashed panels, labels, the
 * x axis and a message in the first panel. Models without it use a plain box for
 * smaller absences.
 */
function absent(spec) {
  const box = h('div.chart-absent' + (spec.frame ? '.framed' : ''));
  if (spec.frame) box.append(absentSvg(spec));
  else box.append(h('p.absent', { text: spec.text }));
  if (spec.detail) box.append(h('p.chart-note', { text: spec.detail }));
  return box;
}

function absentSvg(spec) {
  const f = spec.frame;
  const frame = layout({ width: f.width, height: f.height, panels: f.panels, margin: f.margin });
  const kids = [];
  for (const panel of frame.panels) {
    const { rect } = panel;
    kids.push(s('rect', {
      x: rect.x, y: rect.y, width: rect.w, height: rect.h,
      fill: 'none', stroke: paint('rule'), 'stroke-width': 1,
    }));
    if (panel.label) {
      kids.push(s('text', {
        x: rect.x + 6, y: rect.y + 13,
        style: { font: `400 12px ${FONT_S}`, fill: paint('grey') }, text: panel.label,
      }));
    }
  }
  const first = frame.panels[0].rect;
  kids.push(s('text', {
    x: first.x + first.w / 2, y: first.y + first.h / 2 + 4, 'text-anchor': 'middle',
    style: { font: `400 12px ${FONT_S}`, fill: paint('grey') }, text: spec.text,
  }));
  if (f.xLabel) {
    const last = frame.panels[frame.panels.length - 1].rect;
    kids.push(s('text', {
      x: last.x + last.w / 2, y: last.y + last.h + 33, 'text-anchor': 'middle',
      style: { font: `400 12px ${FONT_S}`, fill: paint('grey') }, text: f.xLabel,
    }));
  }
  return root(frame.width, frame.height, { class: 'chart-svg' }, ...kids);
}

function legend(entries) {
  return h('div.chart-legend', entries.map((e) => h('span.legend-item',
    s('svg', { width: 18, height: 10, style: { display: 'inline-block', verticalAlign: 'middle' } },
      legendGlyph(e)),
    h('span', { text: e.label }))));
}

/**
 * Legend entries default to lines. Marker shapes distinguish measured Q uncertainty
 * from unrecorded uncertainty; a line cannot convey that distinction
 * (`docs/ui-rules.md` §2).
 */
function legendGlyph(e) {
  switch (e.marker) {
    case 'dot':
      return s('circle', { cx: 9, cy: 5, r: 2.8, fill: paint(e.colour) });
    case 'dot-faint':
      return s('circle', { cx: 9, cy: 5, r: 1.8, fill: paint(e.colour), 'fill-opacity': 0.55 });
    case 'square-hollow':
      return s('rect', { x: 6.2, y: 2.2, width: 5.6, height: 5.6, fill: paint('paper'),
        stroke: paint(e.colour), 'stroke-width': 1.1 });
    case 'band':
      return s('rect', { x: 1, y: 1.5, width: 16, height: 7, fill: paint(e.colour), 'fill-opacity': 0.12 });
    default:
      return s('line', {
        x1: 1, y1: 5, x2: 17, y2: 5,
        stroke: paint(e.colour), 'stroke-width': e.width || 1.6,
        'stroke-dasharray': e.dash || null,
      });
  }
}

/** The step between header lines, at the 9 px they are set in. */
export const HEAD_LINE = 11;

/**
 * The helper estimates header line count so models reserve space before rendering. It
 * shares the wrapping arithmetic with `wrapLabel`.
 */
export function headLines(label, width, note = false) {
  return (label ? wrapLabel(label, width).length : 0) + (note ? 1 : 0);
}

/**
 * The wrapper splits panel labels at clause separators because SVG text does not
 * wrap. It permits two lines; longer explanations belong in notes. Width uses an
 * estimate of 4.8 units per character at 9 px. Pure models cannot measure text, and a
 * conservative estimate costs an extra wrap rather than overflow.
 */
export function wrapLabel(label, width, per = 4.8) {
  const fits = (t) => t.length * per <= width;
  if (fits(label)) return [label];
  const parts = String(label).split('  ·  ');
  if (parts.length < 2) return [label];
  let head = parts[0];
  let i = 1;
  for (; i < parts.length; i += 1) {
    const next = `${head}  ·  ${parts[i]}`;
    if (!fits(next)) break;
    head = next;
  }
  const tail = parts.slice(i).join('  ·  ');
  return tail ? [head, tail] : [head];
}

/** Clip-path ids have to be unique in a document, and a card holds several charts. */
let uid = 0;

function renderSvg(model, readout, ctx) {
  const kids = [];
  const clip = `chart-clip-${(uid += 1)}`;
  // Hatching marks the space requested but not acquired. A stopped curve must not
  // appear to represent the whole requested run.
  const hatch = `chart-hatch-${uid}`;
  kids.push(s('defs',
    model.panels.map((panel, i) => s('clipPath', { id: `${clip}-${i}` },
      s('rect', { x: panel.rect.x, y: panel.rect.y, width: panel.rect.w, height: panel.rect.h }))),
    s('pattern', { id: hatch, width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' },
      s('line', { x1: 0, y1: 0, x2: 0, y2: 6, stroke: paint('rule'), 'stroke-width': 2 }))));
  model.panels.forEach((panel, i) => { panel.clip = `${clip}-${i}`; panel.hatch = hatch; });
  for (const panel of model.panels) {
    kids.push(panel.y && panel.y.title ? renderPlotPanel(panel, model) : renderPanel(panel));
  }
  // Shared panels use one x axis; independent panels need their own. Timing spans can
  // be 11 s, 2 ms and 2 µs, so a shared scale would collapse two panels at the
  // origin.
  if (model.x) {
    const last = model.panels[model.panels.length - 1];
    kids.push(model.x.title ? renderPlotXAxis(model.x, last) : renderXAxis(model, model.x, last));
  }
  for (const panel of model.panels) if (panel.x) kids.push(renderXAxis(model, panel.x, panel));
  const cursor = s('g.cursor', { style: { display: 'none' } },
    s('line', { class: 'cursor-rule', y1: model.margin.top, y2: model.height - model.margin.bottom,
      stroke: paint('ink'), 'stroke-width': 1, 'stroke-dasharray': '2 2' }));
  if (model.cursor !== false) kids.push(cursor);
  const band = s('rect.zoom-band', { fill: paint('ink'), 'fill-opacity': 0.08, stroke: paint('ink'),
    'stroke-width': 0.8, style: { display: 'none' } });
  if (ctx) kids.push(band);
  const svg = root(model.width, model.height, { class: ctx ? 'chart-svg zoomable' : 'chart-svg' }, kids);
  if (model.cursor !== false) attachCursor(svg, model, cursor, readout);
  if (ctx) attachZoom(svg, model, band, ctx);
  return svg;
}

function shadesOf(panel) {
  const { rect } = panel;
  return (panel.shades || []).map((shade) => s('rect', {
    x: shade.x, y: rect.y, width: Math.max(0, shade.w), height: rect.h,
    fill: shade.hatch ? `url(#${panel.hatch})` : paint(shade.colour || 'accent'),
    'fill-opacity': shade.hatch ? 1 : shade.opacity ?? 0.055,
  }));
}

function renderPanel(panel) {
  const { rect } = panel;
  const kids = [s('rect', {
    x: rect.x, y: rect.y, width: rect.w, height: rect.h,
    fill: paint('paper'), stroke: paint('rule'), 'stroke-width': 1,
  }), ...shadesOf(panel)];
  for (const tick of (panel.y && panel.y.ticks) || []) {
    kids.push(s('line', {
      x1: rect.x, y1: tick.y, x2: rect.x + rect.w, y2: tick.y,
      stroke: paint(tick.zero ? 'rule' : 'grid'), 'stroke-width': 1,
    }));
    kids.push(s('text', {
      // `scale.fixed()` returns text. Numeric conversion is required before adding
      // the label offset; string concatenation can place integer ticks outside the
      // panel and fractional ticks on their gridlines.
      x: rect.x - 4, y: Number(tick.y) + 3, 'text-anchor': 'end',
      style: { font: `400 11px ${FONT_S}`, fill: paint('grey') }, text: tick.text,
    }));
  }
  kids.push(...contentOf(panel));
  // Header labels wrap above the note. A shared row can make them overlap in narrow
  // panels; with SVG overflow visible, a long label can also cover adjacent card
  // content.
  const lines = [];
  if (panel.label) lines.push(...wrapLabel(panel.label, rect.w));
  const noteAt = rect.y - 5;
  const labelTop = noteAt - (panel.note ? HEAD_LINE : 0) - (lines.length - 1) * HEAD_LINE;
  lines.forEach((line, i) => {
    kids.push(s('text', { x: rect.x, y: labelTop + i * HEAD_LINE,
      style: { font: `600 11px ${FONT_S}`, fill: paint('ink') }, text: line }));
  });
  if (panel.note) {
    kids.push(s('text', { x: rect.x, y: noteAt,
      style: { font: `400 11px ${FONT_S}`, fill: paint(panel.noteColour || 'grey') }, text: panel.note }));
  }
  kids.push(...cursorDots(panel));
  return s('g.panel', { dataset: { panel: panel.key } }, kids);
}

/** The tick length of a plot's frame, major and minor. */
const TICK = { major: 5, minor: 2.6 };

/** Ticks of one length along one edge, inward by `sign`, as a single path. */
function edgeTicks(values, f, length, { at, sign, vertical }) {
  const d = values.map((v) => {
    const p = scale.fixed(f(v));
    return vertical ? `M${at} ${p}h${sign * length}` : `M${p} ${at}v${sign * length}`;
  }).join('');
  return d;
}

/** Major and minor ticks on the four sides of a plot frame. */
function frameTicks(panel, model) {
  const { rect } = panel;
  const paths = [];
  const side = (axis, f, at, sign, vertical) => {
    const majors = axis.ticks.map((t) => t.v);
    paths.push([edgeTicks(majors, f, TICK.major, { at, sign, vertical }), 0.9]);
    paths.push([edgeTicks(minorValues(f, axis.ticks), f, TICK.minor, { at, sign, vertical }), 0.7]);
  };
  if (panel.y) {
    side(panel.y, panel.y.scale, rect.x, 1, true);
    side(panel.y, panel.y.scale, rect.x + rect.w, -1, true);
  }
  if (model.x) {
    side(model.x, model.x.scale, rect.y + rect.h, -1, false);
    side(model.x, model.x.scale, rect.y, 1, false);
  }
  return paths.filter(([d]) => d).map(([d, width]) => s('path', {
    d, fill: 'none', stroke: paint('ink'), 'stroke-width': width,
  }));
}

/** The minor tick values of an axis, from its scale and its major step. */
export function minorValues(f, ticks) {
  const [a, b] = f.domain;
  if (f.kind === 'log10') return scale.logMinorTicks(Math.min(a, b), Math.max(a, b));
  if (ticks.length < 2) return [];
  return scale.minorTicks(a, b, Math.abs(ticks[ticks.length - 1].v - ticks[ticks.length - 2].v));
}

/** A log tick is set as a power of ten; every other tick is its text. */
function tickLabel(tick, attrs) {
  if (tick.power === undefined) return s('text', { ...attrs, text: tick.text });
  return s('text', attrs, '10', s('tspan', { dy: -4, style: { font: `400 11px ${FONT_S}` } }, tick.power));
}

const LABEL = `400 12px ${FONT_S}`;

function renderPlotPanel(panel, model) {
  const { rect } = panel;
  const bottom = rect.y + rect.h;
  const kids = [s('rect', { x: rect.x, y: rect.y, width: rect.w, height: rect.h, fill: paint('paper') })];
  for (const tick of panel.y.ticks) {
    if (tick.zero) {
      kids.push(s('line', { x1: rect.x, y1: tick.y, x2: rect.x + rect.w, y2: tick.y,
        stroke: paint('rule'), 'stroke-width': 1 }));
    }
    kids.push(tickLabel(tick, { x: rect.x - 6, y: Number(tick.y) + 4, 'text-anchor': 'end',
      style: { font: LABEL, fill: paint('ink') } }));
  }
  // A set axis range leaves marks outside the frame, and the frame must contain them.
  kids.push(s('g', { 'clip-path': `url(#${panel.clip})` }, ...shadesOf(panel), ...contentOf(panel)));
  kids.push(s('rect', { x: rect.x, y: rect.y, width: rect.w, height: rect.h,
    fill: 'none', stroke: paint('ink'), 'stroke-width': 0.9 }));
  kids.push(...frameTicks(panel, model));
  const titleX = rect.x - 45;
  kids.push(s('text', { x: titleX, y: rect.y + rect.h / 2, 'text-anchor': 'middle',
    transform: `rotate(-90 ${titleX} ${rect.y + rect.h / 2})`,
    style: { font: LABEL, fill: paint('ink') }, text: panel.y.title }));
  if (panel.letter) {
    kids.push(s('text', { x: rect.x + 8, y: bottom - 7,
      style: { font: `600 13px ${FONT_S}`, fill: paint('ink') }, text: panel.letter }));
  }
  (panel.keys || []).forEach((key, i) => {
    const x = rect.x + rect.w * 0.62;
    const y = rect.y + rect.h * 0.42 + i * 16;
    kids.push(s('line', { x1: x, y1: y, x2: x + 18, y2: y,
      stroke: paint(key.colour), 'stroke-width': key.width || 1.3 }));
    kids.push(s('text', { x: x + 23, y: y + 4, style: { font: LABEL, fill: paint('ink') }, text: key.label }));
  });
  kids.push(...cursorDots(panel));
  return s('g.panel', { dataset: { panel: panel.key } }, kids);
}

/** Rules, bands, series, dots and marks: what a panel draws from its data. */
function contentOf(panel) {
  const { rect } = panel;
  const kids = [];
  for (const rule of panel.rules || []) {
    kids.push(s('line', {
      x1: rule.x1, y1: rule.y1 ?? rect.y, x2: rule.x2 ?? rule.x1, y2: rule.y2 ?? rect.y + rect.h,
      stroke: paint(rule.colour || 'accent'), 'stroke-width': rule.width || 1.5,
      'stroke-dasharray': rule.dash || null,
    }));
    if (rule.cap) {
      for (const y of [rule.y1, rule.y2]) {
        kids.push(s('line', { x1: Number(rule.x1) - rule.cap, y1: y, x2: Number(rule.x1) + rule.cap, y2: y,
          stroke: paint(rule.colour || 'accent'), 'stroke-width': rule.width || 1.5 }));
      }
    }
  }
  for (const band of panel.bands || []) {
    kids.push(s('path', { d: band.d, fill: paint(band.colour || 'accent'),
      'fill-opacity': band.opacity ?? 0.12, stroke: 'none' }));
  }
  for (const series of panel.series || []) {
    if (!series.d) continue;
    kids.push(s('path', {
      // Clipping contains values outside a panel's y window. Forward injection can
      // exceed a cropped J–V axis by hundreds of times and otherwise draw over
      // adjacent panels and the card.
      class: 'series', 'clip-path': panel.clip ? `url(#${panel.clip})` : null,
      d: series.d, fill: 'none', stroke: paint(series.colour),
      'stroke-width': series.width || 1.3, 'stroke-dasharray': series.dash || null,
      'stroke-opacity': series.opacity ?? null, 'stroke-linejoin': 'round', 'stroke-linecap': 'round',
    }));
  }
  for (const dot of panel.dots || []) {
    // A hollow square marks unrecorded Q uncertainty; a bare dot would imply a
    // measured value (`docs/ui-rules.md` §2).
    if (dot.shape === 'square') {
      const r = dot.r || 2.4;
      kids.push(s('rect', { x: dot.x - r, y: dot.y - r, width: 2 * r, height: 2 * r,
        fill: dot.hollow ? paint('paper') : paint(dot.colour || 'ink'),
        stroke: paint(dot.colour || 'ink'), 'stroke-width': dot.hollow ? 1.1 : 0 }));
      continue;
    }
    kids.push(s('circle', { cx: dot.x, cy: dot.y, r: dot.r || 2.4,
      fill: dot.hollow ? paint('paper') : paint(dot.colour || 'ink'), 'fill-opacity': dot.opacity ?? 1,
      stroke: dot.hollow ? paint(dot.colour || 'ink') : null, 'stroke-width': dot.hollow ? 1.1 : null }));
  }
  for (const mark of panel.marks || []) {
    kids.push(s('text', {
      x: mark.x, y: mark.y, 'text-anchor': mark.anchor || 'start',
      style: {
        font: `${mark.weight || 500} ${mark.size || 11}px ${FONT_S}`,
        fill: paint(mark.colour || 'grey'),
      },
      // An integration-window label needs its position over the data. A
      // surface-colour backing preserves legibility because the trace determines
      // where empty space lies.
      ...(mark.halo ? { stroke: paint('paper'), 'stroke-width': 3, 'paint-order': 'stroke' } : {}),
      text: mark.text,
    }));
  }
  return kids;
}

/**
 * Crosshair dots are created with the chart so pointer movement only updates existing
 * elements.
 */
function cursorDots(panel) {
  return (panel.series || []).filter((series) => series.at).map((series) => s('circle', {
    class: 'cursor-dot', r: 2.6, fill: paint(series.colour),
    stroke: paint('paper'), 'stroke-width': 1.5, style: { display: 'none' },
    dataset: { panel: panel.key, series: series.key },
  }));
}

/** Tick labels and the centred title under the last panel; the ticks are the frame's. */
function renderPlotXAxis(axis, panel) {
  const { rect } = panel;
  const baseline = rect.y + rect.h;
  const kids = axis.ticks.map((tick) => tickLabel(tick, { x: tick.x, y: baseline + 16,
    'text-anchor': 'middle', style: { font: LABEL, fill: paint('ink') } }));
  kids.push(s('text', { x: rect.x + rect.w / 2, y: baseline + 33, 'text-anchor': 'middle',
    style: { font: LABEL, fill: paint('ink') }, text: axis.title }));
  return s('g.xaxis', kids);
}

function renderXAxis(model, axis, panel) {
  const baseline = panel.rect.y + panel.rect.h;
  const kids = [];
  for (const tick of axis.ticks) {
    kids.push(s('line', { x1: tick.x, y1: baseline, x2: tick.x, y2: baseline + 4,
      stroke: paint('rule'), 'stroke-width': 1 }));
    kids.push(s('text', { x: tick.x, y: baseline + 14, 'text-anchor': 'middle',
      style: { font: `400 11px ${FONT_S}`, fill: paint('grey') }, text: tick.text }));
  }
  if (axis.label) {
    // The axis caption sits below tick labels to avoid a collision at the rightmost
    // tick.
    kids.push(s('text', { x: panel.rect.x + panel.rect.w, y: baseline + (axis.ticks.length ? 26 : 14), 'text-anchor': 'end',
      style: { font: `400 11px ${FONT_S}`, fill: paint('grey') }, text: axis.label }));
  }
  return s('g.xaxis', kids);
}

/**
 * The crosshair updates existing rules, dots and readout text through one SVG
 * listener. Rebuilding on pointer movement would create up to sixty DOM redraws per
 * second.
 */
function attachCursor(svg, model, cursor, readout) {
  const plot = model.panels[0].rect;
  const rule = cursor.querySelector('.cursor-rule');
  const dots = [...svg.querySelectorAll('.cursor-dot')];
  const idle = model.readout || '';
  let raf = 0;
  let pending = null;

  const paintAt = (px) => {
    raf = 0;
    const value = model.x.scale.invert(px);
    rule.setAttribute('x1', px);
    rule.setAttribute('x2', px);
    const parts = [];
    for (const panel of model.panels) {
      for (const series of panel.series || []) {
        if (!series.at) continue;
        const point = series.at(value);
        const dot = dots.find((d) => d.dataset.panel === panel.key && d.dataset.series === series.key);
        if (!point || !Number.isFinite(point.y)) { if (dot) dot.style.display = 'none'; continue; }
        const y = panel.y.scale(point.y);
        if (dot) {
          dot.setAttribute('cx', scale.fixed(px));
          dot.setAttribute('cy', scale.fixed(y));
          dot.style.display = y >= panel.rect.y && y <= panel.rect.y + panel.rect.h ? '' : 'none';
        }
        parts.push(`${series.label} ${series.format ? series.format(point.y) : point.y}`);
      }
    }
    readout.textContent = `${model.x.format ? model.x.format(value) : value.toFixed(2)}  ·  ${parts.join('  ·  ')}`;
  };

  svg.addEventListener('pointermove', (event) => {
    const box = svg.getBoundingClientRect();
    if (!box.width) return;
    const px = ((event.clientX - box.left) / box.width) * model.width;
    if (px < plot.x || px > plot.x + plot.w) { leave(); return; }
    cursor.style.display = '';
    pending = px;
    if (!raf) raf = requestAnimationFrame(() => paintAt(pending));
  });
  const leave = () => {
    cursor.style.display = 'none';
    for (const dot of dots) dot.style.display = 'none';
    readout.textContent = idle;
  };
  svg.addEventListener('pointerleave', leave);
}

/** A drag shorter than this, in model units, is a click. */
const MIN_DRAG = 6;

/** The panel under a point of the chart, or `undefined` outside every frame. */
function panelAt(model, x, y) {
  return model.panels.find(({ rect, y: axis }) => axis && axis.title
    && x >= rect.x && x <= rect.x + rect.w && y >= rect.y && y <= rect.y + rect.h);
}

/**
 * The ranges a drag selects. A flat drag sets x alone and a tall one y alone, so a
 * zoom along time does not also fix the current axis by accident.
 */
export function zoomRanges(model, panel, [x0, y0], [x1, y1]) {
  const out = [];
  const pair = (f, a, b) => {
    const [u, v] = [f.invert(a), f.invert(b)];
    return u < v ? [u, v] : [v, u];
  };
  if (Math.abs(x1 - x0) >= MIN_DRAG && model.x && !model.x.fixed) {
    out.push({ key: 'x', unit: model.x.unit, range: pair(model.x.scale, x0, x1) });
  }
  if (Math.abs(y1 - y0) >= MIN_DRAG) {
    const { rect } = panel;
    const clamp = (y) => Math.min(rect.y + rect.h, Math.max(rect.y, y));
    out.push({ key: panel.key, unit: panel.y.unit, range: pair(panel.y.scale, clamp(y0), clamp(y1)) });
  }
  return out;
}

/** Drag on a plot to set its ranges, double-click to return them to automatic. */
function attachZoom(svg, model, band, ctx) {
  let start = null;
  let panel = null;
  const point = (event) => {
    const box = svg.getBoundingClientRect();
    return [((event.clientX - box.left) / box.width) * model.width,
      ((event.clientY - box.top) / box.height) * model.height];
  };
  svg.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    const at = point(event);
    panel = panelAt(model, at[0], at[1]);
    if (!panel) return;
    start = at;
    svg.setPointerCapture(event.pointerId);
  });
  svg.addEventListener('pointermove', (event) => {
    if (!start) return;
    const [x, y] = point(event);
    const { rect } = panel;
    const right = Math.min(rect.x + rect.w, Math.max(rect.x, x));
    const low = Math.min(rect.y + rect.h, Math.max(rect.y, y));
    band.setAttribute('x', Math.min(start[0], right));
    band.setAttribute('y', Math.min(start[1], low));
    band.setAttribute('width', Math.abs(right - start[0]));
    band.setAttribute('height', Math.abs(low - start[1]));
    band.style.display = '';
  });
  svg.addEventListener('pointerup', (event) => {
    if (!start) return;
    const ranges = zoomRanges(model, panel, start, point(event));
    start = null;
    band.style.display = 'none';
    if (!ranges.length) return;
    for (const { key, unit, range } of ranges) setRange(ctx.view, key, unit, range[0], range[1]);
    ctx.redraw();
  });
  svg.addEventListener('dblclick', () => {
    if (!isManual(ctx.view)) return;
    resetView(ctx.view);
    ctx.redraw();
  });
}

/**
 * The tick model for an axis: values from `scale.ticks`, their pixels, and
 * their labels at the step's own precision.
 */
export function axisTicks(f, { count = 5, values = null, zeroAt = null } = {}) {
  const [d0, d1] = f.domain;
  const log = f.kind === 'log10';
  const dv = log ? null : scale.step(Math.abs(d1 - d0), count);
  const decades = log ? scale.decades(d0, d1) : null;
  // A range zoomed inside one decade has no decade to label.
  const fine = log && decades.length < 2 && !values;
  const list = values || (fine ? fineLogTicks(d0, d1) : log ? decades : scale.ticks(d0, d1, count));
  return list.map((v) => ({
    v,
    x: scale.fixed(f(v)),
    y: scale.fixed(f(v)),
    text: fine ? String(Number(v.toPrecision(2))).replace('-', scale.MINUS)
      : log ? logText(v) : scale.tickText(v, dv),
    zero: zeroAt === null ? v === 0 : v === zeroAt,
    ...(log && !fine ? powerOf(v) : {}),
  }));
}

function logText(v) {
  const e = Math.round(Math.log10(v));
  return e === 0 ? '1' : `1e${e}`.replace('-', scale.MINUS);
}

/** The exponent a decade tick is set with; 1 stays plain. */
function powerOf(v) {
  const e = Math.round(Math.log10(v));
  return e === 0 ? {} : { power: String(e).replace('-', scale.MINUS) };
}

function fineLogTicks(lo, hi) {
  const out = [];
  for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e += 1) {
    for (const k of [1, 2, 5]) {
      const v = Number(`${k}e${e}`);
      if (v >= lo && v <= hi) out.push(v);
    }
  }
  return out;
}
