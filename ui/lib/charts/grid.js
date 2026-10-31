// The grid chart plots Q against LED level or temperature, with one series per value
// of the other axis. Measured Q uncertainty uses a filled marker and error bar;
// absent uncertainty uses a hollow square (`docs/ui-rules.md` §2). Partial cells stay
// visible as hollow accent markers with kept/requested counts. Missing cells have no
// point and are counted in the caption. A sequential ramp preserves the order of
// temperatures and LED levels in colour and greyscale.

import * as scale from '../scale.js';
import * as fmt from '../format.js';
import { layout, axisTicks, heightFor, ASPECT, MAX_UNIT, PLOT_MARGIN } from './frame.js';
import { ramp } from './jv.js';
import { chargeUnit } from './loops.js';
import { ranged } from './view.js';
import { cellName } from '../history.js';

const finite = (v) => typeof v === 'number' && Number.isFinite(v);

/**
 * `by: 'led'` plots `led_v` with one series per temperature. `by: 'T'` transposes the
 * same cells.
 */
export function gridChartModel(grid, { by = 'led', width = 620, height = null, view = null } = {}) {
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
  // Q against temperature or LED level — a quantity against a quantity.
  const qPanel = [{ key: 'q', weight: 1 }];
  const frame = layout({
    width, panels: qPanel, margin: PLOT_MARGIN,
    height: height ?? heightFor(width, qPanel, { margin: PLOT_MARGIN, aspect: ASPECT.curve, cap: MAX_UNIT.curve }),
  });
  const panel = frame.panels[0];
  const { rect } = panel;
  const xd = xs.length === 1 ? [xs[0] - 1, xs[0] + 1] : scale.extent([xs], { pad: 0.1 });
  const xUnit = by === 'T' ? 'K' : 'V';
  const X = scale.linear(ranged(view, 'x', xUnit, xd), [rect.x, rect.x + rect.w]);
  const unitQ = chargeUnit(present.map((c) => c.q));
  const f = unitQ.factor;
  const bars = present.filter((c) => c.sigma !== null).flatMap((c) => [(c.q + c.sigma) / f, (c.q - c.sigma) / f]);
  const domain = scale.extent([present.map((c) => c.q / f), bars], { includeZero: false }) || [-1, 1];
  const Y = scale.linear(ranged(view, 'q', unitQ.unit, domain), [rect.y + rect.h, rect.y]);

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
        rules.push({ x1: x, y1: scale.fixed(Y((p.cell.q + p.cell.sigma) / f)), y2: scale.fixed(Y((p.cell.q - p.cell.sigma) / f)), colour: p.cell.partial ? 'accent' : colour, width: 0.9, cap: 2.2 });
        dots.push({ x, y, r: 2.6, colour: p.cell.partial ? 'accent' : colour, hollow: p.cell.partial });
      } else {
        dots.push({ x, y, r: 2.6, shape: 'square', hollow: true, colour: p.cell.partial ? 'accent' : colour });
      }
      if (p.cell.partial) {
        marks.push({ x: x + 5, y: y - 5, colour: 'accent-dark', size: 11,
          text: `${fmt.keptOf(p.cell.kept, p.cell.requested)} kept` });
      }
    }
  });
  legend.push({ label: 'σQ measured', marker: 'dot', colour: 'ink' });
  legend.push({ label: 'σQ not recorded', marker: 'square-hollow', colour: 'ink' });
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
      note: `${present.length} cell${present.length === 1 ? '' : 's'} · the cell's Q at Vpre = Voc`,
      y: { scale: Y, ticks: axisTicks(Y, { count: 4 }), title: unitQ.label, unit: unitQ.unit },
      series, dots, rules, marks, shades: [], bands: [],
    }],
    x: {
      scale: X,
      ticks: axisTicks(X, { values: xs.length <= 9 && !(view && view.ranges.x) ? xs : null, count: 6 }),
      label: `${xLabel}  →`,
      title: by === 'T' ? 'T (K)' : 'LED (V)',
      unit: xUnit,
      format: (v) => (by === 'T' ? fmt.kelvin(v) : fmt.volts(v, { decimals: 3 })),
    },
    legend,
    readout: '',
    notes,
    alerts: notes,
    by,
  };
}
