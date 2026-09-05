// The Instruments panel's model — `lib/instruments.js`. Pure, so the rule
// that the lit position is the read-back and nothing else is held down here
// without a browser.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { panelModel, LED_LEVELS } from '../lib/instruments.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const CATALOGUE = JSON.parse(fs.readFileSync(path.join(here, '..', 'fixtures', 'modules_sim.json'), 'utf8'));
const light = CATALOGUE.modules.find((m) => m.name === 'light');

const bench = (over = {}) => ({
  instruments: {
    shutter: { open: false, how: 'readback' },
    led: { output: true, mode: 'PULSE', high_v: 1.0, low_v: 0.4, frequency_hz: 500 },
    relay: { position: 'sourcemeter', how: 'readback' },
    ...over,
  },
  inferred: [],
});

const lit = (row) => row.positions.filter((p) => p.on).map((p) => p.id);
const rowOf = (model, key) => model.rows.find((r) => r.key === key);

test('the lit position is the read-back, one per instrument', () => {
  const m = panelModel(bench(), light);
  assert.deepEqual(m.rows.map((r) => r.key), ['shutter', 'led', 'relay']);
  assert.deepEqual(lit(rowOf(m, 'shutter')), ['shut']);
  assert.deepEqual(lit(rowOf(m, 'led')), ['pulse']);
  assert.deepEqual(lit(rowOf(m, 'relay')), ['sourcemeter']);
});

test('an instrument that does not answer lights nothing', () => {
  const m = panelModel(bench({ shutter: { open: null }, led: { output: null, mode: '?' } }), light);
  assert.deepEqual(lit(rowOf(m, 'shutter')), []);
  assert.deepEqual(lit(rowOf(m, 'led')), []);
  assert.equal(panelModel(null, light).rows.length, 3, 'no snapshot yet is still three rows');
});

test('output off is off whatever the mode says, and DC is DC', () => {
  assert.deepEqual(lit(rowOf(panelModel(bench({ led: { output: false, mode: 'PULSE' } }), light), 'led')), ['off']);
  assert.deepEqual(lit(rowOf(panelModel(bench({ led: { output: true, mode: 'OFF' } }), light), 'led')), ['off']);
  const dc = panelModel(bench({ led: { output: true, mode: 'DC', offset_v: 1.02, high_v: 0.9 } }), light);
  assert.deepEqual(lit(rowOf(dc, 'led')), ['dc']);
  assert.equal(rowOf(dc, 'led').level, 1.02, 'in DC the level is the offset, as the jv row reads it');
});

test('a position is the bench action for it, with the light module’s levels', () => {
  const m = panelModel(bench(), light);
  const by = Object.fromEntries(rowOf(m, 'led').positions.map((p) => [p.id, p]));
  const value = (n) => light.params.find((p) => p.name === n).value;
  assert.equal(by.off.action, 'led-off');
  assert.deepEqual(by.off.args, {});
  assert.equal(by.dc.action, 'set-led-dc');
  assert.deepEqual(by.dc.args, { level: value('led_v') });
  assert.equal(by.pulse.action, 'set-led-pulse');
  assert.deepEqual(by.pulse.args, {
    level: value('led_v'), low: value('led_low_v'),
    frequency_hz: value('pulse_frequency_hz'), duty_percent: value('duty_percent'),
  });
  const sh = Object.fromEntries(rowOf(m, 'shutter').positions.map((p) => [p.id, p.action]));
  assert.deepEqual(sh, { open: 'shutter-open', shut: 'shutter-shut' });
  const rl = Object.fromEntries(rowOf(m, 'relay').positions.map((p) => [p.id, p.action]));
  assert.deepEqual(rl, { amplifier: 'relay-to-amplifier', sourcemeter: 'relay-to-sourcemeter' });
  assert.equal(m.park.action, 'park');
});

test('the levels beside the switch say which the lit position sends', () => {
  const used = (m) => rowOf(m, 'led').levels.filter((l) => l.used).map((l) => l.spec.name);
  assert.deepEqual(used(panelModel(bench(), light)), LED_LEVELS, 'pulse sends all four');
  assert.deepEqual(used(panelModel(bench({ led: { output: true, mode: 'DC' } }), light)), ['led_v']);
  assert.deepEqual(used(panelModel(bench({ led: { output: false, mode: 'DC' } }), light)), [], 'off sends none');
  assert.deepEqual(rowOf(panelModel(bench(), light), 'led').levels.map((l) => l.spec.name), LED_LEVELS);
});

test('inferred is not read', () => {
  const m = panelModel({ ...bench(), inferred: ['shutter'] }, light);
  assert.equal(rowOf(m, 'shutter').inferred, true);
  assert.equal(rowOf(m, 'relay').inferred, false);
  const how = panelModel(bench({ led: { output: true, mode: 'DC', how: 'inferred' } }), light);
  assert.equal(rowOf(how, 'led').inferred, true);
});
