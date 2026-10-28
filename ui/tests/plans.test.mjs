// The plan panel: `lib/plans.js`.
//
// What is asserted is what the person approving a plan reads: every bound of
// the envelope as one row, in words, and the count of plans that wait.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { describeBound, envelopeRows, pendingCount } from '../lib/plans.js';

test('a bound reads as a range, a one-sided limit or a list of choices', () => {
  assert.equal(describeBound({ min: -4, max: -0.5 }), '-4 to -0.5');
  assert.equal(describeBound({ max: 4 }), '≤ 4');
  assert.equal(describeBound({ min: 0 }), '≥ 0');
  assert.equal(describeBound({ choices: ['delay_ns', 'vcoll'] }), 'one of delay_ns, vcoll');
  assert.equal(describeBound(null), '');
});

test('every module parameter and loop value of the envelope is a row', () => {
  const rows = envelopeRows({
    modules: { bace: { vcoll: { min: -4, max: -0.5 }, n_loops: { max: 4 } }, jv_bace: {} },
    loops: { illumination: { levels_v: { min: 1.0, max: 1.03 } } },
  });
  assert.deepEqual(rows, [
    ['bace', 'vcoll', '-4 to -0.5'],
    ['bace', 'n_loops', '≤ 4'],
    ['jv_bace', '—', 'as on the bench'],
    ['illumination loop', 'levels_v', '1 to 1.03'],
  ]);
  assert.deepEqual(envelopeRows(undefined), []);
});

test('only proposed plans wait for a decision', () => {
  assert.equal(pendingCount([{ state: 'proposed' }, { state: 'approved' }, { state: 'proposed' }]), 2);
  assert.equal(pendingCount(undefined), 0);
});
