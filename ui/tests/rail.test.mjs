// The rail and the chain strip, against two recorded `/bench` snapshots: one
// at rest and one taken while a `bace` was inside its acquisition. The second
// exists because the first cannot test the thing M1 is for — at rest nothing
// is inferred, and the overlay is the half of the rail the operator looks at
// for hours.
//
// The model is a pure function of the store's state, so every rule below is
// held down without a browser.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { createStore } from '../lib/store.js';
import { railModel, chainModel, parkLabel, parkTitle } from '../lib/rail.js';

const here = dirname(fileURLToPath(import.meta.url));
const fixture = (name) => JSON.parse(readFileSync(join(here, '../fixtures', name), 'utf8'));

const HELLO = fixture('hello_sim.json');          // a bench at rest, with a chain warning
const RUNNING = fixture('bench_running_sim.json'); // mid-scan: the four inferred instruments

function stateFrom(bench, { hello = null } = {}) {
  const store = createStore({ schedule: () => {} });
  if (hello) store.applyHello(hello);
  if (bench) store.applyBench(bench, { readBack: Boolean(hello) });
  return store.getState();
}

const cell = (model, key) => model.find((c) => c.key === key);

test('the rail is the eight live values, in the design order', () => {
  const model = railModel(stateFrom(HELLO.data.bench));
  assert.deepEqual(model.map((c) => c.key),
    ['relay', 'bias', 'smu', 'shutter', 'led', 'voc', 'power', 'temperature']);
});

test('at rest nothing is inferred, and every cell says how it knows', () => {
  const model = railModel(stateFrom(HELLO.data.bench));
  assert.equal(model.filter((c) => c.inferred).length, 0);
  assert.equal(cell(model, 'relay').how, 'readback');
  assert.equal(cell(model, 'shutter').how, 'readback');
});

test('mid-run the four instruments the run implies are marked inferred, and the others are not', () => {
  const model = railModel(stateFrom(RUNNING));
  const inferred = model.filter((c) => c.inferred).map((c) => c.key);
  // `service/live.py` overlays exactly what the running step implies: the
  // relay at NodeStarted, the bias and the LED at RunStarted, the shutter at
  // each StepPhase. The SMU, the V_oc, the power meter and the temperature are
  // still the read-back's, and drawing them the same way would be a claim the
  // console cannot make.
  assert.deepEqual(inferred.sort(), ['bias', 'led', 'relay', 'shutter']);
  assert.ok(!cell(model, 'smu').inferred, 'the SMU is the read-back\'s, not the run\'s');
  assert.ok(!cell(model, 'power').inferred);
  assert.ok(!cell(model, 'temperature').inferred);
});

test('a live bias output is the one alert on the rail, with its two levels', () => {
  const model = railModel(stateFrom(RUNNING));
  const bias = cell(model, 'bias');
  assert.equal(bias.value, 'LIVE');
  assert.equal(bias.level, 'alert');
  assert.equal(bias.sub, '0.5000 → -0.25 V');
  // §3: "should be the only thing on screen at that intensity".
  assert.equal(model.filter((c) => c.level === 'alert').length, 1);
});

test('the relay is a circuit, not a level: three nodes and the closed side', () => {
  const running = cell(railModel(stateFrom(RUNNING)), 'relay');
  assert.equal(running.value, 'amplifier');
  assert.equal(running.level, null, 'the interlock gets its own treatment, not a level colour');
  assert.deepEqual(running.links, [false, true]);

  const dc = cell(railModel(stateFrom({ instruments: { relay: { position: 'sourcemeter', how: 'readback' } } })), 'relay');
  assert.equal(dc.value, 'Keithley 2400');
  assert.deepEqual(dc.links, [true, false]);

  const unknown = cell(railModel(stateFrom({ instruments: { relay: { position: 'unknown', how: 'cached' } } })), 'relay');
  assert.equal(unknown.value, 'neutral');
  assert.deepEqual(unknown.links, [false, false], 'neither circuit is claimed');
  assert.equal(unknown.sub, 'cached, not re-read');
});

test('the chain says which cell reads wrong; the rail never re-derives it', () => {
  const model = railModel(stateFrom(HELLO.data.bench));
  const led = cell(model, 'led');
  // `led_polarity` reads NORM against an expected INV in this recording: the
  // Sync's rising edge would mean light ON, so extraction would happen during
  // illumination. The warning belongs where the operator is already looking.
  assert.equal(led.level, 'warn');
  assert.match(led.sub, /POL NORM ⚠/);

  const ok = railModel(stateFrom({
    instruments: { led: { output: false, polarity: 'INV', mode: 'OFF' } },
    chain: { ok: 1, total: 1, items: [{ key: 'led_polarity', label: '33220A POL', value: 'INV', expected: 'INV', level: 'ok', fix: 'set-33220a-pol-inv' }] },
  }));
  assert.equal(cell(ok, 'led').level, 'off');
  assert.match(cell(ok, 'led').sub, /POL INV ✓/);
});

test('the V_oc carries which measurement supplied it, and at what LED level', () => {
  const model = railModel(stateFrom(HELLO.data.bench));
  const voc = cell(model, 'voc');
  // A V_oc from a different illumination is worse than no V_oc (`ui-rules` §6).
  assert.match(voc.value, /^0\.904\d V$/);
  assert.match(voc.sub, /@ 1\.020 V/);
  assert.match(voc.sub, /jv_bace/);

  const none = cell(railModel(stateFrom(RUNNING)), 'voc');
  assert.equal(none.value, '—', 'no V_oc invents no number');
  assert.equal(none.sub, 'none this session');
});

test('a temperature nobody measured reads as typed, not as a reading', () => {
  // Not wired: the number on the rail is the one typed into the session, and
  // it goes into every folder name. `290.0 K` presented as an instrument
  // reading would be a measurement that never happened.
  const model = railModel(stateFrom(HELLO.data.bench, { hello: HELLO }));
  const t = cell(model, 'temperature');
  assert.equal(t.value, '290.0 K');
  assert.equal(t.sub, 'typed · not wired');
  assert.equal(t.level, 'typed');

  const wired = cell(railModel(stateFrom({
    instruments: { temperature: { wired: true, kelvin: 250.1, setpoint_k: 250.0, in_band: true, source: 'console' } },
  })), 'temperature');
  assert.equal(wired.value, '250.1 K');
  assert.equal(wired.level, 'ok');
  assert.match(wired.sub, /set 250\.0/);

  const drifting = cell(railModel(stateFrom({
    instruments: { temperature: { wired: true, kelvin: 261.4, setpoint_k: 250.0, in_band: false } },
  })), 'temperature');
  assert.equal(drifting.level, 'warn');
});

test('the monitor\'s readings move the rail, and a monitor that cannot read says so', () => {
  // The 331 is watched from start-up, so the number on the rail is a reading
  // and not the boot read-back: `/bench` is fetched twice a session and
  // `TemperatureRead` arrives every few seconds in between.
  const store = createStore({ schedule: () => {} });
  store.applyBench({
    read_at: 1000,
    instruments: { temperature: { wired: true, kelvin: 261.4, setpoint_k: 250.0, in_band: false, source: 'instrument', monitor: true, reads: 4 } },
    monitors: [{ name: 'temperature', running: true, interval_s: 5, readings: 4, skipped: 0 }],
  });
  const read = (ts, kelvin, in_band) => store.applyFrame({
    seq: ts, ts, run_id: null, node_path: '', type: 'TemperatureRead',
    data: { kelvin, setpoint_k: 250.0, in_band, source: 'instrument' } });

  read(1005, 250.2, true);
  const live = cell(railModel(store.getState()), 'temperature');
  assert.equal(live.value, '250.2 K', 'the reading, not the read-back');
  assert.equal(live.level, 'ok');
  assert.match(live.sub, /every 5 s/, 'the cadence, not a count that is only as new as the snapshot');

  // The monitor reads only while it can hold the worker's bus lock, and a
  // pipeline run holds it for the whole subtree. Frames keep coming; readings
  // do not. An old number drawn as `ok` would be the console telling the
  // operator the cryostat is in band when nothing has looked for a minute.
  store.applyFrame({ seq: 2000, ts: 1200, run_id: 'r1', node_path: 'bace', type: 'StepPhase',
                     data: { phase: 'acquire light', k: 3 } });
  store.applyMonitors([{ name: 'temperature', running: true, interval_s: 5, readings: 5, skipped: 38 }]);
  const stale = cell(railModel(store.getState()), 'temperature');
  assert.equal(stale.value, '250.2 K', 'the last reading is still the last reading');
  assert.equal(stale.level, null, 'nothing has read the 331 for 190 s: `in band` is not a claim about now');
  assert.match(stale.sub, /stale · 38 ticks skipped/);

  // A newer reading — the settle's own poll, or the bus coming free — ends it.
  read(1210, 249.9, true);
  assert.equal(cell(railModel(store.getState()), 'temperature').level, 'ok');
});

test('a typed temperature stays typed, whatever arrives on the stream', () => {
  // The operator's answer to a pause is a `TemperatureRead` too
  // (`source: "operator"`). On a bench with no 331 it is still a number
  // somebody typed, and the rail must not promote it to a reading.
  const store = createStore({ schedule: () => {} });
  store.applyHello(HELLO);
  store.applyBench({ read_at: 1000, instruments: { temperature: { wired: false } } }, { readBack: true });
  store.applyFrame({ seq: 1, ts: 1100, run_id: 'r1', node_path: 'T=290K', type: 'TemperatureRead',
                     data: { kelvin: 291.7, setpoint_k: null, in_band: null, source: 'operator' } });
  const t = cell(railModel(store.getState()), 'temperature');
  assert.equal(t.value, '290.0 K', 'the session\'s typed number');
  assert.equal(t.sub, 'typed · not wired');
  assert.equal(t.level, 'typed');
});

test('a wired bench nobody is monitoring says the number is a read-back', () => {
  // `--no-temperature-monitor`, or a console that stopped it: the number is
  // as old as the last read-back and there is nothing to make it newer.
  const t = cell(railModel(stateFrom({
    read_at: 1000,
    instruments: { temperature: { wired: true, kelvin: 250.1, setpoint_k: 250.0, in_band: true, source: 'instrument', monitor: false, reads: 0 } },
  })), 'temperature');
  assert.equal(t.value, '250.1 K');
  assert.match(t.sub, /read-back only/);
  assert.equal(t.level, 'ok', 'a read-back is a reading; it is only its age that is not said');
});

test('a power console that will not answer is a warning, not a zero', () => {
  const model = railModel(stateFrom({ instruments: { power: { available: false, reason: ':8918 silent' } } }));
  const power = cell(model, 'power');
  assert.equal(power.value, '—');
  assert.equal(power.level, 'warn');
  assert.equal(power.sub, ':8918 silent');
});

test('the SMU shows a ceiling as a ceiling until a run chooses a compliance', () => {
  const ceiling = cell(railModel(stateFrom({
    instruments: { smu: { output: false, compliance: {}, ceiling: { current_a: 0.05, voltage_v: 5.0 } } },
  })), 'smu');
  assert.equal(ceiling.sub, 'cc ≤ 50.0 mA / 5.0 V');

  const chosen = cell(railModel(stateFrom({
    instruments: { smu: { output: true, compliance: { current_a: 0.01, voltage_v: 2.0 }, ceiling: { current_a: 0.05, voltage_v: 5.0 } } },
  })), 'smu');
  assert.equal(chosen.value, 'ON');
  assert.equal(chosen.sub, 'cc 10.0 mA / 2.0 V');

  // A run that chose a current compliance and left the voltage alone: the
  // ceiling `rig.toml` is enforcing is still what will stop a 2400, so it is
  // still on the rail (`ui-rules` §6).
  const half = cell(railModel(stateFrom(RUNNING)), 'smu');
  assert.equal(half.sub, 'cc 50.0 mA / ≤ 5.0 V');
});

test('the strip offers a fix only for a check that reads wrong', () => {
  const model = chainModel(stateFrom(HELLO.data.bench));
  assert.equal(model.ok, 3);
  assert.equal(model.total, 4);
  const bad = model.items.find((i) => i.level !== 'ok');
  assert.equal(bad.key, 'led_polarity');
  assert.equal(bad.action, 'set-33220a-pol-inv');
  assert.equal(bad.actionLabel, 'set INV');
  // `arm-81150a-ext` is the action that *made* the arming ok; offering it
  // again is noise on a strip that is on every screen.
  for (const item of model.items.filter((i) => i.level === 'ok')) assert.equal(item.action, null);
});

test('a run holds the worker, so the strip disables the fixes while one is going', () => {
  const store = createStore({ schedule: () => {} });
  store.applyBench(RUNNING);
  const model = chainModel(store.getState());
  // Every bench action except `park` answers 409 while a run is active. A
  // button that offered itself and then failed would be worse than one that
  // says why.
  assert.equal(model.busy, true);
  assert.equal(chainModel(stateFrom(HELLO.data.bench)).busy, false);
});

test('an armed Park names the queue it is about to cancel', () => {
  // `park` on a busy bench is `worker.stop_runs`: it aborts the run *and*
  // cancels every queued one. The confirmation said "abort the run and park?"
  // and named the queue only in a tooltip, so the operator who reached for it
  // to stop one scan lost the rest of the night silently (#53).
  assert.equal(parkLabel(0), 'abort the run and park?', 'nothing behind it, nothing to say');
  assert.equal(parkLabel(), 'abort the run and park?', 'and no count is the same case');
  assert.equal(parkLabel(1), 'abort the run, cancel 1 queued run, and park?');
  assert.equal(parkLabel(4), 'abort the run, cancel 4 queued runs, and park?');
  // "not after the queue" is the point of the hover, and it only has one when
  // there is a queue to be ahead of.
  assert.equal(parkTitle(0), 'park aborts the run — the bench is safe now');
  assert.equal(parkTitle(2),
    'park aborts the run and cancels 2 queued runs — the bench is safe now, not after the queue');
});

test('the count Park names is the queue the strip already knows about', () => {
  const store = createStore({ schedule: () => {} });
  store.applyBench(RUNNING);
  const model = chainModel(store.getState());
  assert.equal(model.queued, (RUNNING.queue || []).length);
  // The model is the strip's render key, so a run queued while Park is armed
  // moves the label rather than leaving a stale count on screen.
  assert.equal(chainModel(createStore({ schedule: () => {} }).getState()).queued, 0,
    'a console with no snapshot claims no queue');
});

test('the rail follows the stream, not the snapshot, for what the stream owns', () => {
  // A snapshot fetched mid-run is applied `readBack`: its instruments and its
  // chain land, and its `state`/`run`/`queue` do not — an HTTP response and
  // the socket race, and only one of the two cannot arrive out of order.
  const store = createStore({ schedule: () => {} });
  store.applyBench(HELLO.data.bench);
  assert.equal(store.getState().benchState, 'idle');
  store.applyBench(RUNNING, { readBack: true });
  const state = store.getState();
  assert.equal(state.benchState, 'idle', 'the snapshot did not move the bench state');
  assert.equal(cell(railModel(state), 'bias').value, 'LIVE', 'but the instruments are the new ones');
});

test('a console with no snapshot yet says so, and claims nothing', () => {
  // One of the empty states `ui-rules` §9 names: the service not answering.
  // Every cell is an absence, and none of them is a reading of zero or a
  // warning about an instrument nobody has asked.
  const empty = createStore({ schedule: () => {} }).getState();
  const model = railModel(empty);
  assert.deepEqual(model.map((c) => c.value), ['—', '—', '—', '—', '—', '—', '—', '—'],
    'every cell is an absence: nothing has been read');
  const sub = (key) => model.find((c) => c.key === key).sub;
  const level = (key) => model.find((c) => c.key === key).level;
  assert.equal(sub('power'), 'no read-back yet');
  assert.equal(level('power'), null, 'a meter nobody has asked is not a meter that will not answer');
  assert.equal(sub('voc'), 'no read-back yet');
  assert.equal(sub('temperature'), 'not wired');
  // `neutral` says the device is in neither circuit, which is a claim about
  // the interlock. It is not made before the service has answered.
  assert.deepEqual(model.find((c) => c.key === 'relay').links, [false, false]);
  assert.equal(chainModel(empty).total, null);

  const noRouter = cell(railModel(stateFrom({ instruments: { relay: { position: 'unknown', how: 'unavailable' } } })), 'relay');
  assert.equal(noRouter.value, '—');
  assert.equal(noRouter.level, 'warn');
  assert.equal(noRouter.sub, 'no router answering');
});
