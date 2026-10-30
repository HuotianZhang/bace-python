// The axis dialog floats over the page. A chart in a running card is rebuilt with every
// shot, so a form inside it would lose the field being typed in. The dialog stays put
// and reaches the chart by id, whichever build of it is on screen.

import { h, fill } from '../dom.js';
import * as fmt from '../format.js';
import { clearRange, parseRange, ranged, resetView, setRange } from './view.js';

/** The chart build on screen for each id, kept up to date by `frame.js`. */
const live = new Map();

/** The open dialog, or `null`. There is one at a time. */
let open = null;

const show = (v) => fmt.sig(v, 4).replace('-', '−');

/** Records a chart build, and refreshes the dialog if it belongs to this chart. */
export function publish(ctx) {
  live.set(ctx.id, ctx);
  if (open && open.id === ctx.id) open.sync();
}

function rowFor(id, axis) {
  const min = h('input.ax-in', { type: 'text', inputmode: 'decimal', 'aria-label': `${axis.title} minimum` });
  const max = h('input.ax-in', { type: 'text', inputmode: 'decimal', 'aria-label': `${axis.title} maximum` });
  const log = axis.canLog
    ? h('input', { type: 'checkbox', 'aria-label': `${axis.title} log scale` }) : null;
  const row = { key: axis.key, min, max, log };

  const current = () => live.get(id).axes().find((a) => a.key === axis.key);
  const commit = () => {
    const ctx = live.get(id);
    const now = current();
    if (!now) return;
    const parsed = parseRange(min.value, max.value, now.scale.domain, { log: now.log });
    min.classList.toggle('bad', !parsed);
    max.classList.toggle('bad', !parsed);
    if (!parsed) return;
    if (parsed.auto) clearRange(ctx.view, axis.key);
    else setRange(ctx.view, axis.key, now.unit, parsed.lo, parsed.hi);
    ctx.redraw();
  };
  min.onchange = commit;
  max.onchange = commit;
  if (log) {
    log.onchange = () => {
      const ctx = live.get(id);
      // A linear range means nothing on a log axis, and the reverse.
      clearRange(ctx.view, axis.key);
      ctx.view.log = log.checked;
      ctx.redraw();
    };
  }
  row.el = [
    h('span.ax-name', { text: axis.title }), min, max,
    log ? h('label.ax-log', log, h('span', 'Log')) : h('span'),
  ];
  return row;
}

/** Shows what the chart is using. A field being typed in is left alone. */
function syncRow(row, axis, view) {
  const [lo, hi] = axis.scale.domain;
  const set = ranged(view, axis.key, axis.unit, null);
  for (const [input, auto, value] of [[row.min, lo, set && set[0]], [row.max, hi, set && set[1]]]) {
    input.placeholder = show(auto);
    if (document.activeElement !== input) input.value = value === null ? '' : show(value);
  }
  if (row.log) row.log.checked = axis.log === true;
}

export function closeAxes() {
  if (!open) return;
  open.close();
  open = null;
}

/** Opens the dialog beside `anchor`, or closes it if this chart's is already open. */
export function openAxes(anchor, id) {
  if (open && open.id === id) { closeAxes(); return; }
  closeAxes();
  const rows = live.get(id).axes().map((axis) => rowFor(id, axis));
  const grid = h('div.ax-grid',
    h('span'), h('span.ax-head', 'Min'), h('span.ax-head', 'Max'), h('span'),
    rows.map((row) => row.el));
  const el = h('div.axes-pop', { role: 'dialog', 'aria-label': 'Axes' },
    grid,
    h('div.ax-foot',
      h('button.btns', {
        type: 'button',
        onclick: () => { const ctx = live.get(id); resetView(ctx.view); ctx.redraw(); },
      }, 'Auto'),
      h('button.btns', { type: 'button', onclick: closeAxes }, 'Close')));
  document.body.append(el);

  const box = anchor.getBoundingClientRect();
  const size = el.getBoundingClientRect();
  el.style.left = `${Math.max(8, Math.min(box.right - size.width, window.innerWidth - size.width - 8))}px`;
  el.style.top = `${box.bottom + size.height + 8 > window.innerHeight
    ? Math.max(8, box.top - size.height - 4) : box.bottom + 4}px`;

  const outside = (event) => { if (!el.contains(event.target) && event.target !== anchor) closeAxes(); };
  const escape = (event) => { if (event.key === 'Escape') closeAxes(); };
  document.addEventListener('pointerdown', outside);
  document.addEventListener('keydown', escape);
  open = {
    id,
    sync: () => {
      const ctx = live.get(id);
      const axes = ctx.axes();
      for (const row of rows) {
        const axis = axes.find((a) => a.key === row.key);
        if (axis) syncRow(row, axis, ctx.view);
      }
    },
    close: () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', escape);
      fill(el);
      el.remove();
    },
  };
  open.sync();
}
