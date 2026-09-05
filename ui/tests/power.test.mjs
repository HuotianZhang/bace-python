// The power panel and its trace — the 1918-C monitor under the rail.
//
// The store's `powerLog`, the window and its statistics, the chart model and
// the panel model are pure functions of frames, so every rule is held down
// here without a browser: the fold keeps readings in `ts` order and once
// each, the history the service holds merges without a duplicate, and what
// the panel says about a reading (averaged, stale, saturated) comes off the
// frames and nowhere else.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createStore, insertReading, POWER_LOG_MAX } from '../lib/store.js';
import {
  powerModel, powerStats, windowPoints, timeUnit, wattsUnit, lowerBound, statsLine, WINDOWS,
} from '../lib/charts/power.js';
import { powerPanelModel, DEFAULT_UI, INTERVALS } from '../lib/power.js';

const T0 = 1_788_000_000;

function reading(ts, watts, { trustworthy = true, averaged = true, seq = null } = {}) {
  return { seq, ts, run_id: null, node_path: '', type: 'PowerReading',
    data: { watts, trustworthy, wavelength_nm: 530, source: 'simulated', averaged }, decimated: {} };
}

const store = () => createStore({ schedule: () => {} });

// -- the fold ---------------------------------------------------------------

test('every PowerReading goes into the trace, in ts order and once', () => {
  const s = store();
  s.applyFrame(reading(T0 + 1, 1e-4, { seq: 1 }));
  s.applyFrame(reading(T0 + 2, 2e-4, { seq: 2 }));
  // A replay after a reconnect can hand back one older than the newest held.
  s.applyFrame(reading(T0 + 1.5, 1.5e-4, { seq: 3 }));
  s.applyFrame(reading(T0 + 2, 2e-4, { seq: 4 }));
  const log = s.getState().powerLog;
  assert.deepEqual(log.map((p) => p.ts), [T0 + 1, T0 + 1.5, T0 + 2]);
  assert.equal(s.getState().power.averaged, true, 'the newest reading says it was averaged');
});

test('the history the service holds merges by ts without a duplicate', () => {
  const s = store();
  s.applyFrame(reading(T0 + 2, 2e-4, { seq: 1 }));
  s.applyFrame(reading(T0 + 3, 3e-4, { seq: 2 }));
  s.applyPowerHistory({ points: [[T0 + 1, 1e-4, true], [T0 + 2, 2e-4, true], [T0 + 2.5, 2.5e-4, false]] });
  const log = s.getState().powerLog;
  assert.deepEqual(log.map((p) => p.ts), [T0 + 1, T0 + 2, T0 + 2.5, T0 + 3]);
  assert.equal(log[2].trustworthy, false);
  assert.equal(insertReading(log, { ts: T0 + 3, watts: 0 }), false, 'the same ts is refused');
});

test('the trace is bounded, dropping the oldest', () => {
  const s = store();
  const points = [];
  for (let i = 0; i < POWER_LOG_MAX + 10; i += 1) points.push([T0 + i, 1e-4, true]);
  s.applyPowerHistory({ points });
  const log = s.getState().powerLog;
  assert.equal(log.length, POWER_LOG_MAX);
  assert.equal(log[0].ts, T0 + 10);
  assert.equal(log[log.length - 1].ts, T0 + POWER_LOG_MAX + 9);
});

test('a reset drops the trace, and the monitors ride on every bench snapshot', () => {
  const s = store();
  s.applyFrame(reading(T0, 1e-4, { seq: 1 }));
  s.applyBench({ monitors: [{ name: 'power', running: true, interval_s: 0.5 }] }, { readBack: true });
  assert.equal(s.getState().monitors[0].interval_s, 0.5);
  s.reset();
  assert.deepEqual(s.getState().powerLog, []);
  assert.deepEqual(s.getState().monitors, []);
});

// -- the window and its statistics ----------------------------------------------

const LOG = Array.from({ length: 600 }, (_, i) => ({ ts: T0 + i, watts: 1e-4 * (1 + 0.01 * Math.sin(i / 20)), trustworthy: i !== 599 }));

test('the window is the readings since now minus its length', () => {
  const now = T0 + 599;
  assert.equal(windowPoints(LOG, { seconds: 60, now }).length, 61);
  assert.equal(windowPoints(LOG, { seconds: null, now }).length, 600);
  assert.equal(lowerBound(LOG, T0 + 10.5), 11);
  assert.deepEqual(WINDOWS.map((w) => w.key), ['1m', '5m', '30m', '2h', 'all']);
});

test('the statistics are over the window, and count what the meter flagged', () => {
  const s = powerStats(windowPoints(LOG, { seconds: 60, now: T0 + 599 }));
  assert.equal(s.n, 61);
  assert.ok(Math.abs(s.mean - 1e-4) < 1.5e-6);
  assert.ok(s.min < s.mean && s.max > s.mean);
  assert.ok(s.std > 0 && s.std < 1e-6);
  assert.equal(s.untrustworthy, 1);
  assert.equal(s.span_s, 60);
  assert.deepEqual(powerStats([]), { n: 0, mean: null, min: null, max: null, std: null, untrustworthy: 0, span_s: 0 });
  assert.equal(powerStats([LOG[0]]).std, null, 'one reading has no σ');
  assert.match(statsLine(s), /^mean \d+\.?\d* µW  ·  min .*  ·  max .*  ·  σ .* µW \(.* %\)  ·  61 readings$/);
});

test('the axis unit follows the span', () => {
  assert.deepEqual(timeUnit(60), { div: 1, unit: 's' });
  assert.deepEqual(timeUnit(300), { div: 60, unit: 'min' });
  assert.deepEqual(timeUnit(7200), { div: 60, unit: 'min' });
  assert.deepEqual(timeUnit(86400), { div: 3600, unit: 'h' });
  assert.deepEqual(wattsUnit([{ watts: 1.85e-4 }, { watts: 9e-5 }]), { factor: 1e-6, prefix: 'µ' });
  assert.deepEqual(wattsUnit([{ watts: 0 }]), { factor: 1, prefix: '' });
});

// -- the chart model --------------------------------------------------------

test('the chart is one panel of watts against time before now, in the window unit', () => {
  const model = powerModel(LOG, { seconds: 300, now: T0 + 599 });
  assert.equal(model.panels.length, 1);
  const [panel] = model.panels;
  assert.match(panel.label, /P \/ µW$/);
  assert.deepEqual(model.x.scale.domain, [-5, 0]);
  assert.equal(model.x.label, 'min before now');
  assert.equal(model.x.format(-2), '2 min ago');
  assert.equal(panel.series.length, 1);
  assert.ok(panel.series[0].d.startsWith('M'));
  assert.equal(panel.series[0].format(1.85e-4), '185 µW');
  const at = panel.series[0].at(-1);
  assert.equal(at.ts, T0 + 539, 'the reading nearest one minute ago');
  assert.ok(Math.abs(at.y - LOG[539].watts) < 1e-12);
  assert.equal(panel.dots.length, 1, 'the one the meter flagged is a dot');
  assert.equal(panel.dots[0].colour, 'alert');
  assert.match(panel.note, /^301 readings · 5 min · 1 flagged by the meter$/);
  assert.equal(model.legend.length, 1);
  assert.match(model.readout, /^mean /);
});

test('the y axis is a window on the data unless pinned to zero', () => {
  const free = powerModel(LOG, { seconds: 60, now: T0 + 599 });
  assert.ok(free.panels[0].y.scale.domain[0] > 0);
  const pinned = powerModel(LOG, { seconds: 60, now: T0 + 599, fromZero: true });
  assert.ok(pinned.panels[0].y.scale.domain[0] <= 0);
});

test('an empty window is said, not drawn', () => {
  assert.equal(powerModel([], { seconds: 60, now: T0 }).absent.text, 'no readings yet');
  const stale = powerModel(LOG, { seconds: 60, now: T0 + 5000 });
  assert.equal(stale.absent.text, 'nothing in the last 60 s');
  const all = powerModel(LOG, { seconds: null, now: T0 + 5000 });
  assert.equal(all.panels.length, 1, '`all` spans back to the first reading');
  assert.equal(all.x.label, 'min before now');
  assert.deepEqual(all.x.scale.domain, [-5000 / 60, 0]);
  assert.equal(powerModel(LOG, { seconds: null, now: T0 + 4 * 3600 }).x.label, 'h before now');
});

// -- the panel ----------------------------------------------------------------

function stateWith({ monitors = [], power = null, bench = null, log = [] } = {}) {
  const s = store();
  if (bench) s.applyBench(bench, { readBack: true });
  if (monitors.length) s.applyMonitors(monitors);
  for (const p of log) s.applyPowerHistory({ points: [[p.ts, p.watts, p.trustworthy !== false]] });
  if (power) s.applyFrame(power);
  return s.getState();
}

test('running is the service\'s answer; the wish is the console\'s', () => {
  const idle = powerPanelModel(stateWith(), { ...DEFAULT_UI, on: true });
  assert.equal(idle.running, false);
  assert.equal(idle.wanted, true);
  assert.equal(idle.value, null);
  assert.equal(idle.interval_s, 1, 'the remembered interval until the monitor says its own');
  const on = powerPanelModel(stateWith({ monitors: [{ name: 'power', running: true, interval_s: 0.2, readings: 5, failures: 0, last_error: null }] }), DEFAULT_UI);
  assert.equal(on.running, true);
  assert.equal(on.interval_s, 0.2);
  assert.deepEqual(on.monitor, { readings: 5, failures: 0, last_error: null });
  assert.deepEqual(INTERVALS, [0.2, 0.5, 1, 2, 5]);
});

test('the reading is the newest the console has, and says what the meter said about it', () => {
  const now = T0 + 10;
  const monitors = [{ name: 'power', running: true, interval_s: 1 }];
  const fresh = powerPanelModel(stateWith({ monitors, power: reading(T0 + 9.5, 9.2e-5, { seq: 1 }) }), DEFAULT_UI, { now });
  assert.equal(fresh.value.text, '92.0 µW');
  assert.equal(fresh.value.averaged, true);
  assert.equal(fresh.value.stale, false);
  assert.match(fresh.sub, /^averaged · 530 nm · simulated · /);
  assert.equal(fresh.level, null);

  const stale = powerPanelModel(stateWith({ monitors, power: reading(T0, 9.2e-5, { seq: 1 }) }), DEFAULT_UI, { now });
  assert.equal(stale.value.stale, true);
  assert.equal(stale.level, 'stale');
  assert.match(stale.sub, /no reading for 10 s$/);

  const raw = powerPanelModel(stateWith({ monitors, power: reading(T0 + 9.5, 1.8e-4, { averaged: false, seq: 1 }) }), DEFAULT_UI, { now });
  assert.equal(raw.level, 'warn', 'a meter that is not averaging shows one instant of the pulse');
  assert.match(raw.sub, /^not averaged/);

  const clipped = powerPanelModel(stateWith({ monitors, power: reading(T0 + 9.5, 8e-4, { trustworthy: false, seq: 1 }) }), DEFAULT_UI, { now });
  assert.equal(clipped.level, 'warn');
  assert.match(clipped.sub, /saturated or overrange/);
});

test('with the monitor off the read-back stands in, and a silent meter disables the switch', () => {
  const bench = { read_at: T0 + 5, instruments: { power: { available: true, watts: 1.1e-4, trustworthy: true, wavelength_nm: 530, averaged: true, monitor: false } } };
  const m = powerPanelModel(stateWith({ bench }), DEFAULT_UI, { now: T0 + 100 });
  assert.equal(m.value.text, '110 µW');
  assert.equal(m.value.source, 'read-back');
  assert.equal(m.value.stale, false, 'stale is a monitor that stopped answering, not an old read-back');
  // A later stream reading wins over an older read-back.
  const later = powerPanelModel(stateWith({ bench, power: reading(T0 + 6, 1.2e-4, { seq: 1 }) }), DEFAULT_UI, { now: T0 + 7 });
  assert.equal(later.value.text, '120 µW');
  const silent = powerPanelModel(stateWith({ bench: { instruments: { power: { available: false, reason: ':8918 silent' } } } }), DEFAULT_UI);
  assert.equal(silent.available, false);
  assert.equal(silent.reason, ':8918 silent');
  assert.equal(silent.value, null);
});

test('the window and the statistics on the panel are the chart\'s', () => {
  const ui = { ...DEFAULT_UI, window: '1m' };
  const m = powerPanelModel(stateWith({ log: LOG }), ui, { now: T0 + 599 });
  assert.equal(m.window.key, '1m');
  assert.equal(m.points.length, 61);
  assert.equal(m.stats.n, 61);
  assert.equal(m.count, 600);
  assert.equal(m.chart, true);
  assert.equal(m.fromZero, false);
});
