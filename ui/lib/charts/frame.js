// The frame every chart is drawn in: stacked panels over one shared x axis,
// their gridlines, their captions, and the crosshair.
//
// `docs/ui-plan.md` decision 3 groups sixteen design elements into six
// components and says they *"all sit on one scale/axis module"*. This is the
// half of that foundation which touches the DOM; `lib/scale.js` is the half
// that does not. A chart component's job is to return a **model** — panels,
// series, shades, notes — and this file turns any such model into SVG. So the
// components stay pure functions of the data, testable in `node` the way
// `railModel` is, and there is one place that knows what a gridline looks like.
//
// **Two panels, never two y axes.** The design's `ch-transient` puts the
// running integral on a second y scale inside the photocurrent panel. Drawn
// that way the two curves cross wherever the two scales happen to put them,
// and the crossing means nothing — the reader cannot tell which axis a line
// belongs to without checking the colour against a caption. The rule
// `ui-rules` §4 actually asks for is that *"the running integral is worth the
// space… the charge is where the curve flattens"*, and a panel of its own,
// under the photocurrent and on the same x, shows the flattening against the
// decay exactly as well — with no second scale to misread. That is the one
// deliberate departure from the artboards in this milestone.

import { s, root } from '../svg.js';
import { h, fill } from '../dom.js';
import * as scale from '../scale.js';
import * as fmt from '../format.js';

/** A model colour is a `style.css` token name, or a literal hex from a ramp. */
export function paint(colour) {
  return !colour ? 'none' : colour[0] === '#' ? colour : `var(--${colour})`;
}

export const FONT_N = "var(--font-num)";
export const FONT_S = "var(--font-body)";

/** The plot's surround, in the model's own units. One default, in one place. */
export const MARGIN = { left: 56, right: 16, top: 16, bottom: 26, gap: 22 };

/**
 * The three shapes a panel comes in here, as **plot width ÷ plot height**.
 *
 * Before this there was no such rule and every chart picked its own numbers:
 * measured per panel the console ran from 2.1:1 (the J–V) to 12.3:1 (the power
 * monitor), a sixfold spread with nothing to appeal to. The design pack's own
 * drawings sit in a band — `docs/design/bace-charts-r3.js` draws its four
 * panels at 5.2, 5.7, 6.6 and 6.8 : 1 — and `trace` is that band's middle.
 *
 * Three, not one, because the spread was not all error. A quantity against
 * *time* wants length: a 4000-sample transient in a square is a smudge. A
 * quantity against another *quantity* — a J–V's power quadrant, Q against the
 * swept axis — is read for its shape, and stretching it lies about the slope.
 * A `strip` is a length of time seen whole, where only the ends and the
 * excursions matter.
 *
 * The ratio names the panel of **weight 1**; the others follow their weight,
 * which stays a deliberate choice — a running-integral panel is half the
 * height of the traces it belongs to, on purpose.
 */
export const ASPECT = {
  trace: 6,     // a quantity against time — the transient's panels
  curve: 2.6,   // a quantity against another quantity — J–V, Q(axis), the grid
  strip: 11,    // a length of time seen whole — the power monitor, the schedule
};

/**
 * The height that gives `panels` their aspect at this width. A chart derives
 * its `height` from this instead of carrying a hand-picked one, so widening a
 * chart makes it *longer* rather than squarer, and the ratio it was drawn to
 * survives every container it is put in.
 */
export function heightFor(width, panels, { margin = {}, aspect = ASPECT.trace } = {}) {
  const m = { ...MARGIN, ...margin };
  const total = panels.reduce((sum, p) => sum + (p.weight || 1), 0);
  const plot = Math.max(1, width - m.left - m.right);
  return Math.round((plot / aspect) * total + m.top + m.bottom + m.gap * (panels.length - 1));
}

/**
 * Stacked panel rectangles over one x axis.
 *
 * `weight` is relative height, so a running-integral panel can be half the
 * height of the traces it belongs to without any caller doing arithmetic in
 * pixels. The x axis is drawn once, under the last panel: they share it, which
 * is the whole reason they are stacked rather than side by side.
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
 * The model as an element: the SVG, a legend, the notes, and the readout the
 * crosshair writes into.
 *
 * The readout is a fixed row rather than a floating tooltip on purpose. It
 * never moves, so a value can be read while the pointer travels — which is
 * what comparing two points on a transient *is* — and it cannot cover the
 * trace it describes. Empty, it holds its height, so nothing on the card jumps
 * when the pointer enters.
 */
export function chart(model) {
  if (typeof model !== 'function') {
    const figure = h('figure.chart', { dataset: { chart: model.key || '' } });
    return drawInto(figure, model);
  }
  return responsive(model);
}

/**
 * Rounding applied to a measured container before it is handed to a model, so
 * a drag across the window is a handful of rebuilds and not one per pixel.
 * Four units is under half a stroke: nothing on screen moves by it.
 */
const WIDTH_STEP = 4;

/** Below this the axis labels collide whatever the aspect; do not go smaller. */
const MIN_WIDTH = 240;

/**
 * A chart that is a function of the width it is given, rather than of a number
 * picked in its own file.
 *
 * `svg.root()` sets a `viewBox` and `width: 100%`, so the container does not
 * lay a chart out — it **zooms** it, type and all. With each chart carrying a
 * hand-picked viewBox width and each container a different real one, the same
 * `9.5px` axis label was landing anywhere from 8.6 px (the rig tab) to 12.9 px
 * (the power monitor): a 1.5× spread invisible in code, where every file says
 * the same number. Measuring the container and building the model at that
 * width pins the scale at 1, so 9.5 px is 9.5 px on every tab.
 *
 * The observer fires once with the first real width, before paint. Until then
 * the figure stays empty rather than being drawn at a guessed width and
 * redrawn a frame later.
 */
function responsive(make) {
  const figure = h('figure.chart');
  let drawn = null;
  const draw = (raw) => {
    const width = Math.max(MIN_WIDTH, Math.round(raw / WIDTH_STEP) * WIDTH_STEP);
    if (width === drawn) return;
    drawn = width;
    const model = make(width);
    figure.dataset.chart = model.key || '';
    drawInto(figure, model);
  };
  if (typeof ResizeObserver === 'function') {
    // The figure's width comes from its container and the SVG inside is
    // `width: 100%`, so painting can never change what is being observed —
    // there is no feedback loop to guard against.
    new ResizeObserver((entries) => {
      const w = entries[entries.length - 1].contentRect.width;
      if (w > 0) draw(w);
    }).observe(figure);
  } else {
    draw(MIN_WIDTH);
  }
  return figure;
}

function drawInto(figure, model) {
  if (model.absent) {
    fill(figure, absent(model.absent));
    return figure;
  }
  const readout = h('div.chart-readout', { text: model.readout || '' });
  const svg = renderSvg(model, readout);
  fill(figure, svg,
    model.legend && model.legend.length ? legend(model.legend) : null,
    readout,
    model.notes && model.notes.length
      ? h('div.chart-notes', model.notes.map((n) => h('span.chart-note', { text: n })))
      : null);
  return figure;
}

/**
 * The empty state, as the chart's **own footprint** rather than a sentence.
 *
 * R3·1 draws every unrun card's result slot as a named rectangle the size of
 * the chart that will land in it — `transient`, `Q · loop`, `not run this
 * session` — so the card has one geometry, not two, and the eye learns where
 * to look before there is anything to look at. A model that carries `frame`
 * gets that: the panels it will have, dashed, with their labels and the x
 * axis's, and the message inside the first one. A model without `frame` keeps
 * the plain box, which is all a note-sized absence needs.
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
      fill: 'none', stroke: paint('rule'), 'stroke-width': 1, 'stroke-dasharray': '3 3',
    }));
    if (panel.label) {
      kids.push(s('text', {
        x: rect.x, y: rect.y - 5,
        style: { font: `600 9px ${FONT_S}`, fill: paint('grey') }, text: panel.label,
      }));
    }
  }
  const first = frame.panels[0].rect;
  kids.push(s('text', {
    x: first.x + first.w / 2, y: first.y + first.h / 2 + 4, 'text-anchor': 'middle',
    style: { font: `400 11px ${FONT_S}`, fill: paint('grey') }, text: spec.text,
  }));
  if (f.xLabel) {
    const last = frame.panels[frame.panels.length - 1].rect;
    kids.push(s('text', {
      x: last.x + last.w, y: last.y + last.h + 14, 'text-anchor': 'end',
      style: { font: `400 9px ${FONT_S}`, fill: paint('grey') }, text: f.xLabel,
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
 * A legend entry is a line unless it says otherwise. The markers exist for
 * the loop chart, where the *shape* of a point is the statement — R2·3's
 * `□ σ_Q not recorded · ● σ_Q measured` — and a line could not say it.
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
 * How many header lines a label of this length needs at this plot width, so a
 * model can reserve the room before anything is drawn. Same arithmetic as
 * `wrapLabel`, which is the only reason it is worth sharing.
 */
export function headLines(label, width, note = false) {
  return (label ? wrapLabel(label, width).length : 0) + (note ? 1 : 0);
}

/**
 * A panel label broken at its own separators so it fits the plot.
 *
 * SVG text does not wrap, and these labels are built by joining clauses with
 * ` · ` — `photocurrent = light − dark · I / mA · averaged over the loops so
 * far · dark translated to zero`. So the separator is the break, and a label
 * splits only where it already reads as a break. Two lines at most: a third
 * would be a paragraph, and a panel that needs a paragraph needs a note.
 *
 * The width is estimated, not measured — 4.8 units a character at 9 px, a
 * little generous for this face. A model cannot measure text, and being a few
 * characters pessimistic costs a wrap nobody notices; being optimistic costs
 * the overflow this exists to stop.
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

function renderSvg(model, readout) {
  const kids = [];
  const clip = `chart-clip-${(uid += 1)}`;
  // The hatch is the design's own mark for "not acquired" (`ch-qloop-trunc`):
  // the loops a stopped run never ran, drawn as the space they would have
  // filled rather than left as a curve that happens to end early.
  const hatch = `chart-hatch-${uid}`;
  kids.push(s('defs',
    model.panels.map((panel, i) => s('clipPath', { id: `${clip}-${i}` },
      s('rect', { x: panel.rect.x, y: panel.rect.y, width: panel.rect.w, height: panel.rect.h }))),
    s('pattern', { id: hatch, width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' },
      s('line', { x1: 0, y1: 0, x2: 0, y2: 6, stroke: paint('rule'), 'stroke-width': 2 }))));
  model.panels.forEach((panel, i) => { panel.clip = `${clip}-${i}`; panel.hatch = hatch; });
  for (const panel of model.panels) kids.push(renderPanel(panel, model));
  // One x axis under the last panel when the panels share it, or one under
  // each when they do not. The timing diagram is the second case and could
  // not be otherwise: its three panels are a shot, one LED cycle and one
  // record — 11 s, 2 ms and 2 us — and a shared axis would draw two of them
  // as a line at the origin.
  if (model.x) kids.push(renderXAxis(model, model.x, model.panels[model.panels.length - 1]));
  for (const panel of model.panels) if (panel.x) kids.push(renderXAxis(model, panel.x, panel));
  const cursor = s('g.cursor', { style: { display: 'none' } },
    s('line', { class: 'cursor-rule', y1: model.margin.top, y2: model.height - model.margin.bottom,
      stroke: paint('ink'), 'stroke-width': 1, 'stroke-dasharray': '2 2' }));
  if (model.cursor !== false) kids.push(cursor);
  const svg = root(model.width, model.height, { class: 'chart-svg' }, kids);
  if (model.cursor !== false) attachCursor(svg, model, cursor, readout);
  return svg;
}

function renderPanel(panel, model) {
  const { rect } = panel;
  const kids = [s('rect', {
    x: rect.x, y: rect.y, width: rect.w, height: rect.h,
    fill: paint('paper'), stroke: paint('rule'), 'stroke-width': 1,
  })];
  for (const shade of panel.shades || []) {
    kids.push(s('rect', {
      x: shade.x, y: rect.y, width: Math.max(0, shade.w), height: rect.h,
      fill: shade.hatch ? `url(#${panel.hatch})` : paint(shade.colour || 'accent'),
      'fill-opacity': shade.hatch ? 1 : shade.opacity ?? 0.055,
    }));
  }
  for (const tick of (panel.y && panel.y.ticks) || []) {
    kids.push(s('line', {
      x1: rect.x, y1: tick.y, x2: rect.x + rect.w, y2: tick.y,
      stroke: paint(tick.zero ? 'rule' : 'grid'), 'stroke-width': 1,
    }));
    kids.push(s('text', {
      // `scale.fixed()` returns a **string**, so `tick.y + 3` concatenated
      // rather than added: a label at y "124" was written to y "1243". Where
      // the tick rounds to a fraction the extra digit only lands after the
      // decimal point ("190.8" → "190.83") and every label sat 3 px high, on
      // its own gridline; where it rounds to an integer — as the power
      // chart's do — the labels left the panel entirely.
      x: rect.x - 4, y: Number(tick.y) + 3, 'text-anchor': 'end',
      style: { font: `400 9.5px ${FONT_N}`, fill: paint('grey') }, text: tick.text,
    }));
  }
  for (const rule of panel.rules || []) {
    kids.push(s('line', {
      x1: rule.x1, y1: rule.y1 ?? rect.y, x2: rule.x2 ?? rule.x1, y2: rule.y2 ?? rect.y + rect.h,
      stroke: paint(rule.colour || 'accent'), 'stroke-width': rule.width || 1.5,
      'stroke-dasharray': rule.dash || null,
    }));
  }
  for (const band of panel.bands || []) {
    kids.push(s('path', { d: band.d, fill: paint(band.colour || 'accent'),
      'fill-opacity': band.opacity ?? 0.12, stroke: 'none' }));
  }
  for (const series of panel.series || []) {
    if (!series.d) continue;
    kids.push(s('path', {
      // Clipped to the panel, because a y axis is allowed to be a window on
      // the data: a J–V cropped to its power quadrant has forward injection
      // hundreds of times off the top, and unclipped it would draw over the
      // panel above and out of the card.
      class: 'series', 'clip-path': panel.clip ? `url(#${panel.clip})` : null,
      d: series.d, fill: 'none', stroke: paint(series.colour),
      'stroke-width': series.width || 1.3, 'stroke-dasharray': series.dash || null,
      'stroke-opacity': series.opacity ?? null, 'stroke-linejoin': 'round', 'stroke-linecap': 'round',
    }));
  }
  for (const dot of panel.dots || []) {
    // A hollow square is a point whose σ was never recorded (`ui-rules` §2):
    // the shape says so where a bare dot would claim a measurement.
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
        font: `${mark.weight || 500} ${mark.size || 9}px ${mark.mono ? FONT_N : FONT_S}`,
        fill: paint(mark.colour || 'grey'),
      },
      // A label that has to sit over the data — the integration window's, and
      // only that one so far — is drawn with the surface colour behind its own
      // glyphs rather than moved somewhere emptier, because where the trace is
      // empty depends on the trace.
      ...(mark.halo ? { stroke: paint('paper'), 'stroke-width': 3, 'paint-order': 'stroke' } : {}),
      text: mark.text,
    }));
  }
  // The header, from the bottom up: the note last, the label's lines above it.
  //
  // Both used to sit on one line, the label from the left and the note from the
  // right, which held only while labels were short and panels wide. Once the
  // labels took on what they define — `dark translated to zero`, `offset
  // corrected` — and the plot narrowed to make room for the shot's numbers, the
  // two ran into each other, and a left-anchored label long enough simply left
  // the chart: `svg.root()` sets `overflow: visible`, so it carried on across
  // whatever the card had put beside it.
  const lines = [];
  if (panel.label) lines.push(...wrapLabel(panel.label, rect.w));
  const noteAt = rect.y - 5;
  const labelTop = noteAt - (panel.note ? HEAD_LINE : 0) - (lines.length - 1) * HEAD_LINE;
  lines.forEach((line, i) => {
    kids.push(s('text', { x: rect.x, y: labelTop + i * HEAD_LINE,
      style: { font: `600 9px ${FONT_S}`, fill: paint('ink') }, text: line }));
  });
  if (panel.note) {
    kids.push(s('text', { x: rect.x, y: noteAt,
      style: { font: `400 9px ${FONT_S}`, fill: paint(panel.noteColour || 'grey') }, text: panel.note }));
  }
  // One dot per series at the crosshair, hidden until the pointer is over the
  // plot. Built here so the cursor group has nothing to create on the fly.
  for (const series of panel.series || []) {
    if (!series.at) continue;
    kids.push(s('circle', {
      class: 'cursor-dot', r: 2.6, fill: paint(series.colour),
      stroke: paint('paper'), 'stroke-width': 1.5, style: { display: 'none' },
      dataset: { panel: panel.key, series: series.key },
    }));
  }
  return s('g.panel', { dataset: { panel: panel.key } }, kids);
}

function renderXAxis(model, axis, panel) {
  const baseline = panel.rect.y + panel.rect.h;
  const kids = [];
  for (const tick of axis.ticks) {
    kids.push(s('line', { x1: tick.x, y1: baseline, x2: tick.x, y2: baseline + 4,
      stroke: paint('rule'), 'stroke-width': 1 }));
    kids.push(s('text', { x: tick.x, y: baseline + 14, 'text-anchor': 'middle',
      style: { font: `400 9.5px ${FONT_N}`, fill: paint('grey') }, text: tick.text }));
  }
  if (axis.label) {
    // Under the tick labels, not beside them: at the right-hand end of an axis
    // the last tick and the caption are the two things most likely to be in
    // the same place, and they were.
    kids.push(s('text', { x: panel.rect.x + panel.rect.w, y: baseline + (axis.ticks.length ? 26 : 14), 'text-anchor': 'end',
      style: { font: `400 9px ${FONT_S}`, fill: paint('grey') }, text: axis.label }));
  }
  return s('g.xaxis', kids);
}

/**
 * The crosshair. One listener on the svg, and nothing is rebuilt: the rule
 * moves, the dots move, and the readout's text is replaced. `dom.keyed` is
 * about not rebuilding on a data frame; this is about not rebuilding sixty
 * times a second while a pointer crosses the plot.
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
    readout.textContent = fmt.minusIn(
      `${model.x.format ? model.x.format(value) : value.toFixed(2)}  ·  ${parts.join('  ·  ')}`);
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

/**
 * The tick model for an axis: values from `scale.ticks`, their pixels, and
 * their labels at the step's own precision.
 */
export function axisTicks(f, { count = 5, values = null, zeroAt = null } = {}) {
  const [d0, d1] = f.domain;
  const list = values || (f.kind === 'log10' ? scale.decades(d0, d1) : scale.ticks(d0, d1, count));
  const dv = f.kind === 'log10' ? null : scale.step(Math.abs(d1 - d0), count);
  return list.map((v) => ({
    v,
    x: scale.fixed(f(v)),
    y: scale.fixed(f(v)),
    text: f.kind === 'log10' ? logText(v) : scale.tickText(v, dv),
    zero: zeroAt === null ? v === 0 : v === zeroAt,
  }));
}

function logText(v) {
  const e = Math.round(Math.log10(v));
  return e === 0 ? '1' : `1e${e}`.replace('-', scale.MINUS);
}
