// Q across the grid — R2·3's `plot Q(led_v) per T`, and its transpose.
//
// One panel: Q on y, one of the grid's two axes on x, one series per value of
// the other. A cell's marker is the statement R2·3's legend makes — `● σ_Q
// measured` with its ±σ bar, `□ σ_Q not recorded` hollow — and a partial
// cell is drawn in the accent, hollow, with its `kept of requested` beside
// it: it is on the curve because it was measured, and marked because it is
// not what was asked for. A cell that never ran is not on the curve at all,
// and the caption counts it.
//
// The series are a sequential ramp (the J–V's, `charts/jv.js`), because the
// other axis is ordered — nine temperatures, five levels — and five hues that
// cannot be put in order are not an encoding of an ordered quantity.

import * as scale from '../scale.js';
import * as fmt from '../format.js';
import { layout, axisTicks } from './frame.js';
import { ramp } from './jv.js';
import { chargeUnit } from './loops.js';
import { cellName } from '../history.js';

const finite = (v) => typeof v === 'number' && Number.isFinite(v);

/**
 * `by: 'led'` puts led_v on x with a series per temperature; `by: 'T'` the
 * other way round. Either is the same cells read along the other axis.
 */
export function gridChartModel(grid, { by = 'led', width = 620, height = 220 } = {}) {
  const present = (grid && grid.cells ? grid.cells : []).filter((c) => !c.missing && finite(c.q));
  if (!present.length) {
    return { key: 'grid-chart', absent: { text: 'nothing to plot', detail: 'a bace node with a Q gives the grid a point' }, panels: [], notes: [] };
  }
  const xAxis = by === 'T' ? grid.rows : grid.cols;
  const seriesAxis = by === 'T' ? grid.cols : grid.rows;
  const xOf = (cell) => {
    const entry = by === 'T' ? grid.rows.find((r) => r.key === cell.row) : grid.cols.find((c) => c.key === cell.col);
    return entry ? (by === 'T' ? entry.t : entry.led_v) : null;
  };
  const xs = xAxis.map((e) => (by === 'T' ? e.t : e.led_v)).filter(finite);
  if (xs.length === 0) {
    return { key: 'grid-chart', absent: { text: `no ${by === 'T' ? 'temperature' : 'LED level'} recorded on any cell`, detail: null }, panels: [], notes: [] };
  }
  const frame = layout({ width, height, panels: [{ key: 'q', weight: 1 }], margin: { left: 64 } });
  const panel = frame.panels[0];
  const { rect } = panel;
  const xd = xs.length === 1 ? [xs[0] - 1, xs[0] + 1] : scale.extent([xs], { pad: 0.1 });
  const X = scale.linear(xd, [rect.x, rect.x + rect.w]);
  const unitQ = chargeUnit(present.map((c) => c.q));
  const f = unitQ.factor;
  const bars = present.filter((c) => c.sigma !== null).flatMap((c) => [(c.q + c.sigma) / f, (c.q - c.sigma) / f]);
  const domain = scale.extent([present.map((c) => c.q / f), bars], { includeZero: false }) || [-1, 1];
  const Y = scale.linear(domain, [rect.y + rect.h, rect.y]);

  const series = [];
  const dots = [];
  const rules = [];
  const marks = [];
  const legend = [];
  // Series in the order the grid draws them: temperature descending, level ascending.
  seriesAxis.forEach((entry, i) => {
    const cells = present.filter((c) => (by === 'T' ? c.col === entry.key : c.row === entry.key))
      .map((c) => ({ cell: c, x: xOf(c) })).filter((p) => finite(p.x)).sort((a, b) => a.x - b.x);
    if (!cells.length) return;
    const colour = ramp(i, seriesAxis.length);
    const path = cells.length > 1
      ? scale.linePath(cells.map((p) => p.x), cells.map((p) => p.cell.q / f), X, Y) : '';
    series.push({
      key: entry.key, label: entry.label, colour, width: 1.4, d: path,
      at: (x) => {
        let best = cells[0];
        for (const p of cells) if (Math.abs(p.x - x) < Math.abs(best.x - x)) best = p;
        return Math.abs(best.x - x) <= (xd[1] - xd[0]) / Math.max(4, 2 * xs.length) ? { y: best.cell.q / f } : null;
      },
      format: (v) => fmt.charge(v * f),
    });
    legend.push({ label: entry.label, colour, width: 1.4 });
    for (const p of cells) {
      const x = scale.fixed(X(p.x));
      const y = scale.fixed(Y(p.cell.q / f));
      if (p.cell.sigma !== null) {
        rules.push({ x1: x, y1: scale.fixed(Y((p.cell.q + p.cell.sigma) / f)), y2: scale.fixed(Y((p.cell.q - p.cell.sigma) / f)), colour: p.cell.partial ? 'accent' : colour, width: 1 });
        dots.push({ x, y, r: 2.6, colour: p.cell.partial ? 'accent' : colour, hollow: p.cell.partial });
      } else {
        dots.push({ x, y, r: 2.6, shape: 'square', hollow: true, colour: p.cell.partial ? 'accent' : colour });
      }
      if (p.cell.partial) {
        marks.push({ x: x + 5, y: y - 5, colour: 'accent-dark', size: 8.5, mono: true,
          text: `${fmt.keptOf(p.cell.kept, p.cell.requested)} kept` });
      }
    }
  });
  legend.push({ label: 'σ_Q measured', marker: 'dot', colour: 'ink' });
  legend.push({ label: 'σ_Q not recorded', marker: 'square-hollow', colour: 'ink' });
  const partial = present.filter((c) => c.partial);
  const missing = grid.cells.filter((c) => c.missing);
  const notes = [];
  if (partial.length) notes.push(`partial, hollow in the accent: ${partial.map((c) => cellName(grid, c)).join(', ')} — on the curve because it was measured, marked because it is not what was asked`);
  if (missing.length) notes.push(`not on the curve, never run: ${missing.map((c) => cellName(grid, c)).join(', ')}`);
  const xLabel = by === 'T' ? 'T / K' : 'led_v / V';
  return {
    key: 'grid-chart', width: frame.width, height: frame.height, margin: frame.margin,
    panels: [{
      ...panel,
      label: `Q  ·  ${unitQ.label}  ·  ${by === 'T' ? 'one line per LED level' : 'one line per temperature'}`,
      note: `${present.length} cell${present.length === 1 ? '' : 's'} · the cell's Q at V_pre = V_oc`,
      y: { scale: Y, ticks: axisTicks(Y, { count: 4 }) },
      series, dots, rules, marks, shades: [], bands: [],
    }],
    x: {
      scale: X,
      ticks: axisTicks(X, { values: xs.length <= 9 ? xs : null, count: 6 }),
      label: `${xLabel}  →`,
      format: (v) => (by === 'T' ? fmt.kelvin(v) : fmt.volts(v, { decimals: 3 })),
    },
    legend,
    readout: '',
    notes,
    by,
  };
}
