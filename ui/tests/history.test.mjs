// The results tab's models, against the records the service actually
// answered — `ui/fixtures/run_grid_sim.json`, `run_bace_sim.json` and
// `runs_sim.json`, recorded off `--sim --fast` by `tools/record_ui_fixtures.py
// --only results`. Nothing here is a hand-made record.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  cellName, cellQ, csvOf, flagsFor, gridModel, identityOf, isPartial, moduleNodes, provenanceSummary,
  rerunParams, runGroups, summaryModel, temperatureText, vocText, spanOf,
} from '../lib/history.js';
import { gridChartModel } from '../lib/charts/grid.js';

const here = dirname(fileURLToPath(import.meta.url));
const fixture = (name) => JSON.parse(readFileSync(join(here, '../fixtures', name), 'utf8'));

const grid = fixture('run_grid_sim.json');
const manual = fixture('run_bace_sim.json');
const rows = fixture('runs_sim.json');

// -- the list -----------------------------------------------------------------

test('the index groups by session and carries the identity on every row', () => {
  const groups = runGroups(rows);
  assert.equal(groups.total, 3);
  assert.equal(groups.groups.length, 1, 'one session recorded them all');
  const [group] = groups.groups;
  assert.deepEqual(group.identities, ['s4 · SIM · pixel a'], 'from `sample`, not from a folder name');
  assert.deepEqual(group.runs.map((r) => r.label), ['pipeline · T×2/led×2/jv_bace+bace', 'bace', 'jv_bace']);
  assert.deepEqual(group.runs.map((r) => r.level), ['warn', 'ok', 'ok'], 'stopped is a warning, done is ok');
  assert.equal(group.runs[0].counts, '614/808');
  assert.match(group.runs[2].voc, /^V_oc /, 'a jv_bace row says what it measured');
});

test('a live state from the store overrides the index, and an unnamed sample says so', () => {
  const groups = runGroups(rows, { live: { [rows[0].run_id]: { state: 'running' } } });
  assert.equal(groups.groups[0].runs[0].state, 'running');
  assert.equal(groups.groups[0].runs[0].level, 'live');
  assert.equal(identityOf({ sample: '', material: '', pixel: '' }), null);
  assert.equal(identityOf(null), null);
  assert.equal(identityOf({ sample: 's4', material: 'PTQ10:IT-4F', pixel: 'a' }), 's4 · PTQ10:IT-4F · pixel a');
});

test('a span is two stamps and a duration, or what is known of it', () => {
  assert.equal(spanOf(null, null), '—');
  assert.match(spanOf(1788507000, null), / →$/);
  assert.match(spanOf(1788507000, 1788507000 + 25 * 60), /→ \d\d:\d\d · 25 min$/);
});

// -- the grid -----------------------------------------------------------------

test('the 2 x 2 grid: three complete cells, one partial, none missing', () => {
  const g = gridModel(grid);
  assert.equal(g.shape, 'grid');
  assert.deepEqual(g.rows.map((r) => r.label), ['280.1 K', '250.1 K'], 'temperatures descend, as the canonical tree runs them');
  assert.deepEqual(g.cols.map((c) => c.label), ['led_v 1.010 V', 'led_v 1.020 V']);
  assert.equal(g.cells.length, 4);
  assert.deepEqual(g.cells.map((c) => c.missing), [false, false, false, false]);
  const partial = g.cells.filter((c) => c.partial);
  assert.equal(partial.length, 1);
  assert.equal(partial[0].path, 'T=280K/led=1.020V/bace');
  assert.equal(partial[0].kept, 6);
  assert.equal(partial[0].requested, 200);
  assert.equal(partial[0].outcome, 'stopped');
  // Kept as it is: the running mean over the six shots that ran, from the
  // journal's LoopDone -- not dropped, and not a number from the other cells.
  assert.ok(Number.isFinite(partial[0].q), 'a partial cell still has its Q');
  assert.ok(partial[0].sigma !== null, 'six loops leave a σ');
  assert.equal(cellName(g, partial[0]), '280.1 K · 1.020 V');
  for (const cell of g.cells) {
    assert.ok(cell.voc !== null && cell.vocHow === 'jv_bace', `${cell.path} is centred on the jv_bace before it`);
    assert.equal(cell.points, 1, 'a bace at V_oc has a zero-width axis');
  }
});

test('the temperature row says how it knows: typed at the pause is not settled', () => {
  const g = gridModel(grid);
  assert.deepEqual(g.rows.map((r) => [r.how, r.source]), [['operator', 'operator'], ['operator', 'operator']]);
  assert.match(temperatureText(g.nodes[0]).text, /typed by the operator at the pause/);
  assert.equal(temperatureText({ temperature_k: 290, temperature_how: 'typed', temperature_source: '' }).level, 'warn');
  assert.match(temperatureText({ temperature_k: 250, temperature_how: 'setpoint', temperature_source: '' }).text, /requested, not reached/);
  assert.match(temperatureText({ temperature_k: 250, temperature_how: 'settled', temperature_source: 'simulated' }).text, /not a measurement/);
  assert.equal(temperatureText({ temperature_k: 250, temperature_how: 'settled', temperature_source: 'instrument' }).measured, true);
  assert.match(temperatureText({ temperature_k: 250, temperature_how: 'operator', temperature_source: 'console' }).text, /last reading polled/);
  assert.equal(temperatureText({ temperature_k: null }).text, 'not recorded');
});

test('a manual bace is one cell and a grid of one, never an apology', () => {
  const g = gridModel(manual);
  assert.equal(g.shape, 'grid');
  assert.equal(g.rows.length, 1);
  assert.equal(g.cols.length, 1);
  assert.equal(g.cells.length, 1);
  assert.equal(g.rows[0].how, 'typed', 'no temperature node above it: the session\'s typed 290 K');
  const [cell] = g.cells;
  assert.equal(cell.partial, false);
  assert.equal(cell.kept, 5);
  assert.ok(cell.sigma !== null, 'five loops leave a σ');
  assert.equal(vocText(cell.node).mismatch, false, 'the V_oc came from a jv_bace at the same level');
});

test('a record with no bace has no grid, and its nodes are still listed', () => {
  const noGrid = { ...manual, nodes: Object.fromEntries(Object.entries(manual.nodes).filter(([k]) => k !== 'bace')) };
  const g = gridModel(noGrid);
  assert.equal(g.shape, 'none');
  assert.equal(moduleNodes(manual).length, 1);
});

test('a swept axis shows the point nearest V_oc, never the mean of the curve', () => {
  const node = { module: 'bace', node_path: 'x', values: [0.8, 0.9, 1.0], q_mean: [-1e-10, -2e-10, -3e-10], q_std: [0, 1e-12, 0], voc: 0.92 };
  const q = cellQ(node);
  assert.equal(q.index, 1);
  assert.equal(q.q, -2e-10);
  assert.equal(q.sigma, 1e-12);
  assert.match(q.at, /nearest V_oc/);
  const noVoc = cellQ({ ...node, voc: null });
  assert.equal(noVoc.index, 1);
  assert.match(noVoc.at, /middle point/);
  // σ_Q = 0 is not recorded (ui-rules §2).
  assert.equal(cellQ({ ...node, voc: 0.8 }).sigma, null);
});

test('the same T and level twice keeps both nodes and shows the newest', () => {
  const twice = { ...manual, nodes: { ...manual.nodes, 'bace#2': { ...manual.nodes.bace, node_path: 'bace#2', finished_at: manual.nodes.bace.finished_at + 1, q_mean: [-5e-12] } } };
  const g = gridModel(twice);
  assert.equal(g.cells.length, 1);
  assert.equal(g.cells[0].repeated, 2);
  assert.equal(g.cells[0].path, 'bace#2');
});

test('a cell the tree asked for and never ran stays in the grid as missing', () => {
  const nodes = Object.fromEntries(Object.entries(grid.nodes).filter(([k]) => !k.startsWith('T=280K/led=1.020V/bace')));
  const g = gridModel({ ...grid, nodes });
  assert.equal(g.cells.length, 4, 'the product of 2 T x 2 levels');
  const missing = g.cells.filter((c) => c.missing);
  assert.equal(missing.length, 1);
  assert.equal(missing[0].row, '280.1');
  assert.equal(missing[0].col, '1.020');
  const s = summaryModel(g, { ...grid, nodes });
  assert.equal(s.missing, 1);
  assert.match(s.lines.find((l) => l.key === 'flags').text, /1 never run \(280.1 K · 1.020 V\)/);
});

// -- the summary strip ---------------------------------------------------------

test('the summary excludes the partial cell from the span and says so', () => {
  const g = gridModel(grid);
  const s = summaryModel(g, grid);
  assert.equal(s.complete, 3);
  assert.equal(s.partial, 1);
  const span = s.lines.find((l) => l.key === 'span');
  assert.match(span.text, /over 3 complete cells, 1 partial excluded/);
  assert.match(span.text, /all negative/);
  assert.match(span.text, /^-\d\.\d+e-10 C → -\d\.\d+e-10 C/);
  const sigma = s.lines.find((l) => l.key === 'sigma');
  assert.equal(sigma.text, 'recorded on every cell (4 of 4)');
  const intensity = s.lines.find((l) => l.key === 'intensity');
  assert.match(intensity.text, /read on every shot \(606 of 606\)/);
  assert.match(intensity.text, /never mW\/cm²/, 'watts at the meter, never an irradiance');
  const flags = s.lines.find((l) => l.key === 'flags');
  assert.match(flags.text, /1 partial \(280.1 K · 1.020 V\)/);
  assert.match(flags.text, /warn at Start/);
});

test('σ recorded on some rows only names the rows, and none says a zero is not a σ', () => {
  const nodes = {};
  for (const [k, n] of Object.entries(grid.nodes)) {
    nodes[k] = n.module === 'bace' && k.startsWith('T=250K') ? { ...n, q_std: [0] } : n;
  }
  const some = summaryModel(gridModel({ ...grid, nodes }), grid);
  assert.match(some.lines.find((l) => l.key === 'sigma').text, /recorded at 280.1 K only · zero elsewhere means not recorded/);
  for (const [k, n] of Object.entries(nodes)) if (n.module === 'bace') nodes[k] = { ...n, q_std: [0] };
  const none = summaryModel(gridModel({ ...grid, nodes }), grid);
  assert.match(none.lines.find((l) => l.key === 'sigma').text, /a zero is not a σ/);
  assert.equal(none.lines.find((l) => l.key === 'sigma').level, 'warn');
});

test('intensity null on every shot is said as the meter not answering', () => {
  const nodes = {};
  for (const [k, n] of Object.entries(grid.nodes)) nodes[k] = n.module === 'bace' ? { ...n, intensity_recorded: 0 } : n;
  const s = summaryModel(gridModel({ ...grid, nodes }), grid);
  const line = s.lines.find((l) => l.key === 'intensity');
  assert.match(line.text, /null on 606 of 606 shots · the meter did not answer; watts not recorded, mW\/cm² never inferred/);
  assert.equal(line.level, 'warn');
});

// -- the flags ---------------------------------------------------------------

test('every flag on the partial cell states its reason', () => {
  const node = grid.nodes['T=280K/led=1.020V/bace'];
  assert.equal(isPartial(node), true);
  const flags = flagsFor(grid, node);
  const codes = flags.map((f) => f.code);
  assert.ok(codes.includes('partial'));
  const partial = flags.find((f) => f.code === 'partial');
  assert.equal(partial.level, 'warn');
  assert.match(partial.text, /^6 of 200 shots kept · stopped after a shot/);
  assert.match(partial.text, /never averaged in silently/);
  // The digitiser line is the corrected wording: what the service flagged,
  // not a shared extreme it judged as nothing.
  const digitiser = flags.find((f) => f.code === 'digitiser');
  assert.equal(digitiser.level, 'info');
  assert.equal(digitiser.text, 'digitiser check: no shot flagged, 6 of 6 judged');
  // The run's own verdicts at Start, each with its sentence, where they
  // apply: the whole run's, and a loop's for every node beneath it -- this
  // cell is under T=280K, so T=250K's `temperature.not-wired` is not its.
  const atStart = flags.filter((f) => f.text.includes('· at Start'));
  const wholeRun = grid.verdicts.filter((v) => !v.node_path);
  assert.equal(atStart.length, wholeRun.length);
  assert.ok(atStart.every((f) => f.text.length > 40), 'the service\'s sentence, not a code');
  assert.ok(atStart.some((f) => f.level === 'info' && f.code === 'intensity.factor'), 'info verdicts stay: they qualify the number');
  const under250 = flagsFor(grid, grid.nodes['T=250K/led=1.010V/bace']);
  assert.ok(under250.some((f) => f.code === 'temperature.not-wired' && f.text.endsWith('on T=250K')));
  assert.ok(under250.some((f) => f.code === 'trigger.auto'), 'ui-rules §9: AUTO is said beside the charge');
  // Loudness first.
  const order = flags.map((f) => ({ crit: 0, warn: 1, info: 2 }[f.level]));
  assert.deepEqual(order, [...order].sort());
});

test('flags that only a bad node has: a flagged shot, a missing meter, a V_oc from another lamp level, no baseline', () => {
  const node = { ...grid.nodes['T=250K/led=1.010V/bace'], shots_flagged: 3, intensity_recorded: 100, voc_led_v: 1.02, offset_corrected: false };
  const flags = flagsFor({ verdicts: [] }, node);
  const by = Object.fromEntries(flags.map((f) => [f.code, f]));
  assert.match(by.digitiser.text, /^3 of 200 shots flagged by the digitiser check/);
  assert.match(by.digitiser.text, /Do not interpret those charges/);
  assert.match(by.intensity.text, /null on 100 of 200 shots/);
  assert.match(by.voc.text, /this cell pulsed at 1.010 V/);
  assert.match(by.voc.text, /worse than none/);
  assert.equal(by.baseline.level, 'info');
  assert.match(by.baseline.text, /offset_correct = false/);
  const failed = flagsFor({ error: 'scope fell over' }, { ...node, outcome: 'failed', kept: 3, error: 'RuntimeError: scope fell over' });
  assert.equal(failed[0].level, 'crit');
  assert.match(failed[0].text, /scope fell over/);
});

test('a typed temperature is a flag; a settled one on an instrument is not', () => {
  const typed = flagsFor({ verdicts: [] }, manual.nodes.bace);
  assert.match(typed.find((f) => f.code === 'temperature').text, /typed into \[sample\], nobody read an instrument/);
  const settled = flagsFor({ verdicts: [] }, { ...manual.nodes.bace, temperature_how: 'settled', temperature_source: 'instrument' });
  assert.equal(settled.find((f) => f.code === 'temperature'), undefined);
});

// -- provenance and re-queue ---------------------------------------------------

test('resolved from: the layers of params_as_executed, the few named', () => {
  const p = provenanceSummary(grid.params_as_executed['T=250K/led=1.010V/bace']);
  assert.match(p.text, /run\.toml \d+/);
  assert.match(p.text, /inherited 3 \(led_v, led_low_v, led_settle_s\)/, 'the illumination loop\'s three');
  assert.match(p.text, /derived 1 \(voc\)/);
  assert.match(p.text, /edited \d+ \(/);
  assert.equal(p.total, Object.keys(grid.params_as_executed['T=250K/led=1.010V/bace']).length);
  assert.equal(provenanceSummary(null), null, 'a journal record has values, not layers');
});

test('running a cell again takes its overrides and its level, never its V_oc', () => {
  const again = rerunParams(grid.params_as_executed['T=250K/led=1.010V/bace']);
  assert.equal(again.led_v, 1.01, 'the level the loop gave it is a manual bace\'s own parameter');
  assert.equal(again.led_low_v, 0.4);
  assert.equal(again.n_loops, 200);
  assert.equal('voc' in again, false);
  assert.equal('axis_name' in again, false, 'run.toml stays run.toml');
});

// -- export and the chart --------------------------------------------------------

test('the csv is one row per cell with the absences empty', () => {
  const g = gridModel(grid);
  const csv = csvOf(g, grid);
  const lines = csv.trim().split('\n');
  assert.equal(lines.length, 5);
  assert.match(lines[0], /^run_id,node_path,temperature_k,temperature_how,temperature_source,led_v,voc_v,voc_how,q_c,sigma_q_c,kept,requested,outcome,folder$/);
  assert.ok(lines.some((l) => l.includes(',6,200,stopped,')));
  const nodes = Object.fromEntries(Object.entries(grid.nodes).filter(([k]) => !k.startsWith('T=280K/led=1.020V/bace')));
  const withMissing = csvOf(gridModel({ ...grid, nodes }), { ...grid, nodes });
  assert.ok(withMissing.includes(',never run,'));
});

test('the chart is one series per temperature over led_v, and its transpose', () => {
  const g = gridModel(grid);
  const byLed = gridChartModel(g, { by: 'led' });
  assert.equal(byLed.panels[0].series.length, 2, 'two temperatures');
  assert.equal(byLed.panels[0].dots.length, 4);
  assert.equal(byLed.panels[0].dots.filter((d) => d.hollow).length, 1, 'the partial cell is hollow');
  assert.equal(byLed.panels[0].marks.length, 1);
  assert.match(byLed.panels[0].marks[0].text, /6\/200 kept/);
  assert.match(byLed.notes[0], /partial, hollow in the accent: 280.1 K · 1.020 V/);
  const byT = gridChartModel(g, { by: 'T' });
  assert.equal(byT.panels[0].series.length, 2, 'two levels');
  assert.equal(byT.x.format(250.1), '250.1 K');
  const none = gridChartModel(gridModel({ nodes: {} }), {});
  assert.ok(none.absent);
});
