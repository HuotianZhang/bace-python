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

/** A model colour is a `style.css` token name, or a literal hex from a ramp. */
export function paint(colour) {
  return !colour ? 'none' : colour[0] === '#' ? colour : `var(--${colour})`;
}

export const FONT_N = "var(--font-num)";
export const FONT_S = "var(--font-body)";

/**
 * Stacked panel rectangles over one x axis.
 *
 * `weight` is relative height, so a running-integral panel can be half the
 * height of the traces it belongs to without any caller doing arithmetic in
 * pixels. The x axis is drawn once, under the last panel: they share it, which
 * is the whole reason they are stacked rather than side by side.
 */
export function layout({ width, height, panels, margin = {} }) {
  const m = { left: 56, right: 16, top: 16, bottom: 26, gap: 22, ...margin };
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
  const figure = h('figure.chart', { dataset: { chart: model.key || '' } });
  if (model.absent) {
    fill(figure, h('div.chart-absent', h('p.absent', { text: model.absent.text }),
      model.absent.detail ? h('p.chart-note', { text: model.absent.detail }) : null));
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

function legend(entries) {
  return h('div.chart-legend', entries.map((e) => h('span.legend-item',
    s('svg', { width: 18, height: 8, style: { display: 'inline-block', verticalAlign: 'middle' } },
      s('line', {
        x1: 1, y1: 4, x2: 17, y2: 4,
        stroke: paint(e.colour), 'stroke-width': e.width || 1.6,
        'stroke-dasharray': e.dash || null,
      })),
    h('span', { text: e.label }))));
}

/** Clip-path ids have to be unique in a document, and a card holds several charts. */
let uid = 0;

function renderSvg(model, readout) {
  const kids = [];
  const clip = `chart-clip-${(uid += 1)}`;
  kids.push(s('defs', model.panels.map((panel, i) => s('clipPath', { id: `${clip}-${i}` },
    s('rect', { x: panel.rect.x, y: panel.rect.y, width: panel.rect.w, height: panel.rect.h })))));
  model.panels.forEach((panel, i) => { panel.clip = `${clip}-${i}`; });
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
      x: shade.x, y: rect.y, width: shade.w, height: rect.h,
      fill: paint(shade.colour || 'accent'), 'fill-opacity': shade.opacity ?? 0.055,
    }));
  }
  for (const tick of (panel.y && panel.y.ticks) || []) {
    kids.push(s('line', {
      x1: rect.x, y1: tick.y, x2: rect.x + rect.w, y2: tick.y,
      stroke: paint(tick.zero ? 'rule' : 'grid'), 'stroke-width': 1,
    }));
    kids.push(s('text', {
      x: rect.x - 4, y: tick.y + 3, 'text-anchor': 'end',
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
    kids.push(s('circle', { cx: dot.x, cy: dot.y, r: dot.r || 2.4,
      fill: paint(dot.colour || 'ink'), 'fill-opacity': dot.opacity ?? 1 }));
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
  if (panel.label) {
    kids.push(s('text', { x: rect.x, y: rect.y - 5,
      style: { font: `600 9px ${FONT_S}`, fill: paint('ink') }, text: panel.label }));
  }
  if (panel.note) {
    kids.push(s('text', { x: rect.x + rect.w, y: rect.y - 5, 'text-anchor': 'end',
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
