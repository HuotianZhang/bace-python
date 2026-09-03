// The scale and axis foundation. Every rule here is one a chart would get
// wrong on its own, and none of them needs a browser.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import * as scale from '../lib/scale.js';

test('the axes get the steps the design draws', () => {
  // ch-jv: −4 … 2 V, labelled every volt. With the step chosen at the
  // integers instead of the geometric means this was −4, −2, 0, 2 and the
  // sweep's own volts stopped being labelled.
  assert.deepEqual(scale.ticks(-4, 2, 6), [-4, -3, -2, -1, 0, 1, 2]);
  // ch-transient: 0 … 5000 ns, every 1000.
  assert.deepEqual(scale.ticks(0, 5000, 5), [0, 1000, 2000, 3000, 4000, 5000]);
});

test('a tick is labelled to the precision its own step resolves', () => {
  assert.equal(scale.tickText(0.30000000000000004, 0.1), '0.3');
  assert.equal(scale.tickText(-0.2, 0.2), `${scale.MINUS}0.2`);
  // The charge axis of ch-transient, which is 0 · 4.5e−10 · 9e−10.
  assert.equal(scale.tickText(4.5e-10, 4.5e-10), `4.5e${scale.MINUS}10`);
  assert.equal(scale.tickText(9e-10, 4.5e-10), `9e${scale.MINUS}10`);
});

test('a minus sign is U+2212, not a hyphen', () => {
  assert.ok(scale.tickText(-1.5, 0.5).startsWith('−'));
  assert.ok(!scale.tickText(-1.5, 0.5).includes('-'));
});

test('decades are decades, not the doubles 10 ** e happens to give', () => {
  // `10 ** -5` is 0.000009999999999999999 and labels itself as such.
  assert.deepEqual(scale.decades(1e-5, 1e-2), [1e-5, 1e-4, 1e-3, 1e-2]);
});

test('a zero-width domain draws down the middle rather than dividing by zero', () => {
  // A zero-width axis is a *repeat*, not a mistake (`ui-rules` §4), and a dark
  // reference that never moved is a flat trace.
  const f = scale.linear([1.0423, 1.0423], [0, 100]);
  assert.equal(f(1.0423), 50);
  assert.ok(Number.isFinite(f(0)));
});

test('the log scale clamps a zero to its floor instead of dropping the point', () => {
  const f = scale.log10([1e-6, 1e-2], [100, 0], { floor: 1e-6 });
  assert.equal(f(1e-6), 100);
  assert.equal(f(1e-2), 0);
  assert.equal(f(0), 100, 'a dark sweep crosses zero, and the curve must not gain a hole');
  assert.equal(f(-1e-4), f(1e-4), 'the axis is |J|');
});

test('extent can be made to include zero, because a current chart without it lies', () => {
  assert.deepEqual(scale.extent([[3, 5]], { pad: 0, includeZero: true }), [0, 5]);
  const flat = scale.extent([[2, 2]], {});
  assert.ok(flat[0] < 2 && flat[1] > 2, 'a flat trace still gets a panel');
});

test('thinning a trace keeps the extremes, because the peak is the measurement', () => {
  // 4000 samples of noise with one spike, drawn into 40 columns. Taking every
  // hundredth sample loses the spike; the min/max envelope cannot.
  const ys = new Array(4000).fill(0).map((_, i) => Math.sin(i / 7) * 0.01);
  ys[1234] = -9.5;
  const X = scale.linear([0, 3999], [0, 400]);
  const Y = scale.linear([-10, 10], [100, 0]);
  const d = scale.envelopePath(ys, (i) => X(i), Y, { columns: 40 });
  const ys_drawn = [...d.matchAll(/[ML][\d.]+ ([\d.]+)/g)].map((m) => Number(m[1]));
  assert.ok(ys_drawn.some((y) => y > 96), 'the spike is on the path');
  assert.ok(d.split('M').length <= 3, 'and the path is not broken into pieces');
});

test('a thinned column is placed at a whole index, because X is often a lookup', () => {
  // `time_s[1234.5]` is `undefined`. Every column whose midpoint was not a
  // whole number used to collapse to x = 0.
  const time = new Array(1000).fill(0).map((_, i) => i * 5e-10);
  const X = (i) => time[i] * 1e9;
  const Y = scale.linear([0, 1], [10, 0]);
  const d = scale.envelopePath(new Array(1000).fill(0.5), X, Y, { columns: 25 });
  assert.ok(!/[ML]0 /.test(d), 'no column collapsed to the origin');
  assert.ok(!d.includes('NaN'));
});

test('under two samples a column the trace is drawn exactly', () => {
  const ys = [0, 1, 2, 3];
  const Y = scale.linear([0, 3], [30, 0]);
  const d = scale.envelopePath(ys, (i) => i * 10, Y, { columns: 100 });
  assert.equal(d, scale.linePath((i) => i * 10, ys, (x) => x, Y));
});

test('a gap in the data is a gap in the line, not a chord across it', () => {
  const Y = scale.linear([0, 2], [20, 0]);
  const d = scale.linePath((i) => i * 10, [0, 1, null, 2], (x) => x, Y);
  assert.equal(d.split('M').length - 1, 2, 'the pen lifts and starts again');
});
