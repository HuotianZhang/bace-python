// The plan panel: `lib/plans.js`.
//
// What is asserted is what the person approving a plan reads: every bound of
// the envelope as one row, in words, and the count of plans that wait.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { describeBound, envelopeRows, pendingCount, splitRows } from '../lib/plans.js';

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

test('a pinned value folds, and what the agent may still choose stays in view', () => {
  const { shown, fixed } = splitRows({
    modules: { bace: {
      timebase_ns_per_div: { choices: [1000] },
      settle_s: { min: 0.5, max: 0.5 },
      vcoll: { choices: [-0.5, -1, -2] },
      n_averages: { choices: [50, 200] },
    }, jv_bace: {} },
    loops: { repeat: { count: { min: 1, max: 60 } } },
  });
  assert.deepEqual(fixed.map((r) => r[1]), ['timebase_ns_per_div', 'settle_s']);
  assert.deepEqual(shown.map((r) => r[1]), ['vcoll', 'n_averages', '—', 'count']);
});

test('a pinned voltage, current, temperature, delay or axis is never folded', () => {
  const names = ['vcoll', 'vpre', 'voc', 'v_sat', 'delay_ns', 'led_v', 'stop_v', 'setpoint_k',
    'smu_current_compliance_a', 'vpre_on_voc', 'centre_on_voc', 'axis_name', 'axis_stop'];
  const pinned = Object.fromEntries(names.map((n) => [n, { choices: [1] }]));
  const { shown, fixed } = splitRows({
    modules: { bace: pinned },
    loops: { temperature: { values_k: { choices: [250] } } },
  });
  assert.deepEqual(fixed, []);
  assert.equal(shown.length, names.length + 1);
});

test('nothing is lost between the two tables', () => {
  const envelope = { modules: { bace: { a: { choices: [1] }, vcoll: { choices: [-1] }, b: { max: 2 } } } };
  const { shown, fixed } = splitRows(envelope);
  assert.equal(shown.length + fixed.length, envelopeRows(envelope).length);
});

test('only proposed plans wait for a decision', () => {
  assert.equal(pendingCount([{ state: 'proposed' }, { state: 'approved' }, { state: 'proposed' }]), 2);
  assert.equal(pendingCount(undefined), 0);
});
