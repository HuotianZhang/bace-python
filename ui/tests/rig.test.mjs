// The rig tab's two drawings are the design pack's, ported as they stand —
// so the tests are about the port, not the picture: that the markup is the
// design's, that the swept axis is the one in the accent, and that a bad axis
// draws the reference rather than throwing.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { chainMarkup, shotMarkup, AXES, CHAIN_SIZE, SHOT_SIZE } from '../lib/charts/rig.js';

const ACCENT = '#ec3013';

test('the chain names every instrument on the rig, and the two that are not wired', () => {
  const svg = chainMarkup();
  for (const name of ['33220A', '81150A', 'Keithley 2400', 'Infiniium', 'Deditec DIO', 'R_sense 5.192 Ω', 'device in cryostat']) {
    assert.ok(svg.includes(name), name);
  }
  assert.ok(svg.includes('not wired'), 'the 331 is drawn, and drawn as not wired');
  assert.ok(svg.includes('SYNC → CHAN3') && svg.includes('SYNC ↑ arms'), 'both trigger edges are labelled');
  assert.deepEqual(CHAIN_SIZE, { width: 960, height: 470 });
});

test('the swept axis is the one in the accent, and the others are ghosted', () => {
  const vpre = shotMarkup('vpre');
  const delay = shotMarkup('delay_ns');
  const vcoll = shotMarkup('vcoll');
  // The V_pre level label is in the accent only when vpre is swept.
  const level = (svg) => svg.match(/fill:(#[0-9a-f]{6})">V_pre 1\.0423 V</)[1];
  assert.equal(level(vpre), ACCENT);
  assert.notEqual(level(delay), ACCENT);
  assert.notEqual(level(vcoll), ACCENT);
  // The delay label is in the accent for delay_ns and vcoll (the pulse is theirs), never for vpre.
  const del = (svg) => svg.match(/fill:(#[0-9a-f]{6})">\+60 · :PULS:DEL1/)[1];
  assert.equal(del(delay), ACCENT);
  assert.equal(del(vcoll), ACCENT);
  assert.notEqual(del(vpre), ACCENT);
  assert.deepEqual(SHOT_SIZE, { width: 1000, height: 760 });
});

test('the measured edges are in the drawing, in the order they were measured', () => {
  const svg = shotMarkup('vpre');
  const at = (label) => svg.indexOf(label);
  assert.ok(at('−380 · drive off') < at('0 · Sync = trigger'));
  assert.ok(at('0 · Sync = trigger') < at('+60 · :PULS:DEL1'));
  assert.ok(at('+60 · :PULS:DEL1') < at('light off at the sample · +122'));
  assert.ok(svg.includes('t0_int +118.5'));
});

test('an axis the console does not know draws the reference with nothing highlighted', () => {
  assert.deepEqual(AXES, ['vpre', 'delay_ns', 'vcoll']);
  const odd = shotMarkup('led_v');
  assert.ok(!/fill:#ec3013">V_pre 1\.0423 V</.test(odd));
  assert.ok(!/fill:#ec3013">\+60 · :PULS:DEL1/.test(odd));
});
