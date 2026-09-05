// The number rules of `docs/ui-rules.md` §2, which are the ones a screen gets
// wrong quietly: significant figures are what the measurement resolves, and
// two zeros in the real archive are not zeros at all.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { charge, sigmaQ, volts, kelvin, intensity, duration, keptOf, plural, sig, ABSENT } from '../lib/format.js';

test('a charge keeps five to six significant figures, in the archive\'s own spelling', () => {
  assert.equal(charge(3.65257e-10), '3.65257e-10 C');
  assert.equal(charge(-1.7183827170358963e-10), '-1.71838e-10 C');
  assert.equal(charge(null), ABSENT);
});

test('V_oc to four decimals, T to one', () => {
  assert.equal(volts(1.0277123), '1.0277 V');
  assert.equal(kelvin(294.83), '294.8 K');
});

test('intensity is watts at the meter, never an irradiance', () => {
  // The beam-splitter/area factor is not recoverable, so mW/cm² here would be
  // a number that looks calibrated and is not.
  assert.equal(intensity(1.407e-3), '1.41 mW');
  assert.equal(intensity(2.0e-5), '20.0 µW');
  assert.equal(intensity(null), ABSENT);
});

test('an absence is never a value', () => {
  assert.equal(sigmaQ(0), null, 'sigma_Q = 0 means not recorded');
  assert.equal(volts(null), ABSENT);
  assert.equal(keptOf(20, 100), '20/100', '100 requested, 20 completed');
  assert.equal(keptOf(null, 100), ABSENT);
});

test('the three time scales read as themselves', () => {
  assert.equal(duration(0.8), '0.80 s', 'a shot is sub-second and keeps two figures');
  assert.equal(duration(1620), '27 min');
  assert.equal(duration(16200), '4 h 30 min');
});

test('significant figures, not decimal places', () => {
  assert.equal(sig(0.55051, 3), '0.551', 'FF, to three');
  assert.equal(sig(126.127, 3), '126', 'J_sc in A/m², to three');
  assert.equal(sig(0, 3), '0');
});

test('a count of one is singular — the schedule footer said "writes 1 folders"', () => {
  assert.equal(plural(1, 'folder'), '1 folder');
  assert.equal(plural(4, 'folder'), '4 folders');
  assert.equal(plural(0, 'folder'), '0 folders', 'none of them is plural, as English has it');
  assert.equal(plural(1, 'module run'), '1 module run', 'the noun is the whole phrase');
  assert.equal(plural(2, 'module run'), '2 module runs');
});
