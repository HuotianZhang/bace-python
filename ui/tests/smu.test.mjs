// The SMU panel's model — `lib/smu.js`. Pure, so the three claims this panel
// lives or dies by are held down here without a browser:
//
//   * the display shows nothing unless there is something to show, and always
//     says why;
//   * a panel the instrument is *in* is not a form somebody has filled in;
//   * nothing that moves with a reading is in the controls' render key, or
//     the fields cannot be typed into.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  smuPanelModel, panelKeys, displayWhy, fieldText, notesModel,
  displayVolts, displayAmps, displayOhms, sourceLine, REFRESH_STEPS,
} from '../lib/smu.js';

const SETUP = {
  function: 'voltage', level: 0, current_compliance_a: 0.05, voltage_compliance_v: 2,
  nplc: 1, averaging: 1, terminals: 'FRON', four_wire: false, source_range: null,
  unit: 'V', limit: 0.05,
};

const state = (over = {}) => ({
  benchState: 'idle',
  monitors: [],
  smu: null,
  bench: {
    inferred: [],
    unavailable: {},
    instruments: {
      smu: { output: false, ceiling: { current_a: 0.05, voltage_v: 5 },
             panel: null, panel_defaults: SETUP },
      relay: { position: 'sourcemeter', how: 'readback' },
    },
  },
  ...over,
});

/** A state with the panel applied, the output on and one reading in. */
function live(over = {}, setup = SETUP) {
  const s = state();
  s.bench.instruments.smu = { ...s.bench.instruments.smu, output: true, panel: setup };
  s.smu = { volts: 0.905142, amps: -1.982e-4, ohms: -4566.8, compliance: false,
            function: setup.function, level: setup.level, ts: Date.now() / 1000 };
  return { ...s, ...over };
}

const switchOf = (model, key) => model.switches.find((r) => r.key === key);
const lit = (row) => row.positions.filter((p) => p.on).map((p) => p.label);

test('no SourceMeter is one sentence, not an empty panel', () => {
  const s = state();
  s.bench.unavailable = { smu: 'GPIB0::24::INSTR did not answer' };
  const m = smuPanelModel(s);
  assert.equal(m.available, false);
  assert.match(m.unavailable, /did not answer/);
  assert.deepEqual(m.switches, [], 'nothing to switch on a bench without one');
  assert.deepEqual(m.fields, []);
});

test('the defaults are drawn before anything is applied, and are not "applied"', () => {
  const m = smuPanelModel(state());
  assert.equal(m.available, true);
  assert.equal(m.applied, false, 'panel_defaults is a starting point, not a state');
  assert.equal(m.setup.function, 'voltage');
  assert.equal(m.reading, null);
  assert.equal(displayWhy(m), 'not on the panel');
  assert.ok(m.notes.some((n) => n.key === 'unapplied'));
});

test('the display is dashes with the output off, and says so', () => {
  const s = state();
  s.bench.instruments.smu = { ...s.bench.instruments.smu, panel: SETUP, output: false };
  s.smu = { volts: 0.9, amps: 1e-6, ohms: 9e5, compliance: false, ts: Date.now() / 1000 };
  const m = smuPanelModel(s);
  assert.equal(m.applied, true);
  assert.equal(m.reading, null, 'a reading taken before the output went off is not a reading now');
  assert.equal(displayWhy(m), 'output off');
});

test('a reading is shown only with the panel applied and the output on', () => {
  const m = smuPanelModel(live());
  assert.ok(m.reading);
  assert.equal(m.reading.volts, 0.905142);
  assert.equal(m.reading.stale, false);
  assert.equal(m.reading.compliance, false);
});

test('R is computed when the service did not send one, and is nothing at 0 A', () => {
  const s = live();
  s.smu = { ...s.smu, ohms: null };
  assert.ok(Math.abs(smuPanelModel(s).reading.ohms - (0.905142 / -1.982e-4)) < 1e-6);
  s.smu = { ...s.smu, amps: 0, ohms: null };
  assert.equal(smuPanelModel(s).reading.ohms, null, 'V/0 is not a resistance');
});

test('a monitored reading goes stale after three intervals; a spot reading never does', () => {
  const old = Date.now() / 1000 - 10;
  const monitored = live({ monitors: [{ name: 'smu', interval_s: 1, readings: 9, skipped: 0 }] });
  monitored.smu = { ...monitored.smu, ts: old };
  assert.equal(smuPanelModel(monitored).reading.stale, true);

  const byHand = live();
  byHand.smu = { ...byHand.smu, ts: old };
  const m = smuPanelModel(byHand);
  assert.equal(m.reading.stale, false, 'nobody promised another one');
  assert.equal(m.reading.monitored, false);
});

test('the output locks the three things the instrument will not change under it', () => {
  const off = smuPanelModel(state());
  for (const key of ['function', 'terminals', 'four_wire']) {
    assert.equal(switchOf(off, key).locked, null, `${key} is free with the output off`);
  }
  const on = smuPanelModel(live());
  for (const key of ['function', 'terminals', 'four_wire']) {
    assert.match(switchOf(on, key).locked, /output is ON/, `${key} is locked under a live output`);
  }
  assert.equal(switchOf(on, 'output').locked, null, 'the output itself is never locked');
  assert.equal(switchOf(on, 'output').alert, true, 'a live source is the alert register');
});

test('sensing is a named pair, not a tick — the 2400 says 4W', () => {
  const row = switchOf(smuPanelModel(state()), 'four_wire');
  assert.deepEqual(row.positions.map((p) => p.label), ['2-wire', '4-wire']);
  assert.deepEqual(lit(row), ['2-wire']);
  assert.deepEqual(row.positions.map((p) => p.id), [false, true], 'the ids are the wire values');
});

test('the level carries the unit of whatever is being sourced', () => {
  const volts = smuPanelModel(state()).fields.find((f) => f.name === 'level');
  assert.equal(volts.unit, 'V');
  const amps = smuPanelModel(live({}, { ...SETUP, function: 'current', unit: 'A', limit: 2 }))
    .fields.find((f) => f.name === 'level');
  assert.equal(amps.unit, 'A');
});

test('each compliance field carries the bench ceiling it is held under', () => {
  const fields = smuPanelModel(state()).fields;
  assert.equal(fields.find((f) => f.name === 'current_compliance_a').limit, 0.05);
  assert.equal(fields.find((f) => f.name === 'voltage_compliance_v').limit, 5);
  assert.equal(fields.find((f) => f.name === 'nplc').limit, null, 'nplc has no bench ceiling');
});

test('a field is typed back as itself: no rounding a small compliance to zero', () => {
  assert.equal(fieldText(2e-8, { figures: 3 }), '2e-8', 'a 20 nA compliance is not 0');
  assert.equal(fieldText(0.05, { figures: 3 }), '0.05', 'and not 0.0500000 either');
  assert.equal(fieldText(1, { figures: 0 }), '1');
  assert.equal(fieldText(null, { figures: 3 }), '', 'autorange is an empty box');
});

test('a run holds the bench: everything is disabled and the panel says why', () => {
  const m = smuPanelModel(live({ benchState: 'running' }));
  assert.equal(m.busy, true);
  assert.ok(m.switches.every((row) => row.busy));
  assert.ok(m.notes.some((n) => n.key === 'busy' && /resets it/.test(n.text)));
});

test('the relay is stated and never moved from here', () => {
  const away = notesModel({ applied: true, busy: false, relay: 'amplifier' });
  assert.match(away[0].text, /open circuit/);
  assert.match(away[0].text, /bench tab/, 'it names where the relay moves, and offers no button');
  assert.equal(notesModel({ applied: true, busy: false, relay: 'sourcemeter' }).length, 0);
  assert.match(notesModel({ applied: true, busy: false, relay: 'unknown' })[0].text, /unknown/);
});

test('the controls key holds no counter that moves with a reading', () => {
  const a = live({ monitors: [{ name: 'smu', interval_s: 1, readings: 3, skipped: 0 }] });
  const b = live({ monitors: [{ name: 'smu', interval_s: 1, readings: 900, skipped: 41 }] });
  b.smu = { ...b.smu, volts: 0.7, ts: b.smu.ts + 5 };
  const ka = panelKeys(smuPanelModel(a));
  const kb = panelKeys(smuPanelModel(b));
  assert.equal(ka.controls, kb.controls, 'a caret in a field survives every tick');
  assert.notEqual(ka.display, kb.display, 'and the display still moves with the reading');
});

test('the display key moves when the compliance annunciator lights', () => {
  const clear = live();
  const clamped = live();
  clamped.smu = { ...clamped.smu, compliance: true };
  assert.notEqual(panelKeys(smuPanelModel(clear)).display,
                  panelKeys(smuPanelModel(clamped)).display);
});

test('the panel\'s own numbers: six figures, prefixes where they are natural', () => {
  assert.equal(displayVolts(0.90514215), '0.905142 V');
  assert.equal(displayAmps(-1.98234e-4), '-198.234 µA');
  assert.equal(displayAmps(0), '0 A');
  assert.equal(displayOhms(1911435.3), '1.911 MΩ');
  assert.equal(displayOhms(null), '—');
  assert.equal(sourceLine({ unit: 'A', level: 0 }), 'sourcing 0 A');
  assert.equal(sourceLine({ unit: 'V', level: 1.5 }), 'sourcing 1.50000 V');
});

test('the refresh steps are the power monitor\'s, so one switch is learnt once', () => {
  assert.deepEqual(REFRESH_STEPS, [0.2, 0.5, 1, 2, 5]);
});
