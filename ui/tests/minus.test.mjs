// One glyph for the minus sign, everywhere a reader sees one — and the ASCII
// hyphen kept everywhere a machine reads it back (#68).
//
// The console rendered both at once. `scale.tickText` had always written
// U+2212, because the convention was declared in `scale.js` and `scale.js`
// draws axes; `format.js` had never written one, because the rule lived in the
// module next door. So a `jv` card carried `−0.2` on the axis at its left and
// `-109 nA` in the metric table below it, which is the column `ui-rules` §2
// says is the difference between one that reads down and one that does not.
//
// Two halves, and the second is why this file is not one assertion:
//
//   * a **reader's** number is U+2212 — the axis, the metric table, the rail,
//     the shot rows, the schedule, the drift note, the chart captions;
//   * a **machine's** number is U+002D — an `<input>` the operator types back
//     into (`Number('−0.2')` is `NaN`), the keys the results grid finds a cell
//     by, the CSV, and a file stem.
//
// A blanket replacement at `format.js`'s exit satisfies the first and breaks
// the second the moment a value crosses between them, so the boundary is the
// thing under test, not the glyph.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import * as fmt from '../lib/format.js';
import * as scale from '../lib/scale.js';
import { tKey, ledKey, csvOf, gridModel } from '../lib/history.js';
import { showValue, fileStem } from '../lib/recipe.js';
import { chargeUnit } from '../lib/charts/loops.js';
import { runGroups } from '../lib/history.js';

const here = dirname(fileURLToPath(import.meta.url));
const fixture = (name) => JSON.parse(readFileSync(join(here, '../fixtures', name), 'utf8'));

const HYPHEN = '-';

// -- the reader's half ------------------------------------------------------

test('every formatter that renders a number renders U+2212, exponent included', () => {
  // The values are the archive's own: a charge off the 2026-08-07 grid, the
  // J_sc of a dark sweep, the V_pre a `bace` scan starts at.
  const cases = [
    ['charge', fmt.charge(-1.7183827170358963e-10), '−1.71838e−10 C'],
    ['scientific', fmt.scientific(-3.65257e-10, 6), '−3.65257e−10'],
    ['sigmaQ', fmt.sigmaQ(4.2e-11), '4.20e−11'],
    ['volts', fmt.volts(-0.1312), '−0.1312 V'],
    ['density', fmt.density(-4.49), '−4.49 mA/cm²'],
    ['amps', fmt.amps(-1.09e-7), '−109 nA'],
    ['kelvin', fmt.kelvin(-40.2), '−40.2 K'],
    ['intensity', fmt.intensity(1.407e-3), '1.41 mW'],
    ['sig', fmt.sig(-0.00190, 3), '−0.00190'],
    ['tickText', scale.tickText(-0.2, 0.1), '−0.2'],
  ];
  for (const [name, got, want] of cases) {
    assert.equal(got, want, name);
    assert.ok(!got.includes(HYPHEN), `${name} still carries an ASCII hyphen: ${got}`);
  }
});

test('the exponent is a sign too — the half a leading-minus fix leaves behind', () => {
  // A positive charge has no leading sign at all, and this is the reading the
  // console shows most: `1.59750e-12 C` in the shot rows under a chart whose
  // own decade labels already read `1e−12`.
  const q = fmt.charge(1.5975e-12);
  assert.equal(q, '1.59750e−12 C');
  assert.equal(chargeUnit([1.5975e-12]).label, 'Q / 1e−12 C');
});

test('a sign in a sentence is converted; a hyphen in a word, a path or a date is not', () => {
  // `GET /runs` sends `outcome_text` already composed, and the same field
  // carries `failed: <message>`. `minusIn` is what the console reads it with.
  assert.equal(fmt.minusIn('Q -5.651e-13 ± 0.0e+00 C · 1/1'), 'Q −5.651e−13 ± 0.0e+00 C · 1/1');
  assert.equal(fmt.minusIn('failed: no such file /tmp/a-b/c-1'), 'failed: no such file /tmp/a-b/c-1');
  assert.equal(fmt.minusIn('s4_PTQ10IT4F 2026-08-07 jv-dark'), 's4_PTQ10IT4F 2026-08-07 jv-dark');
  assert.equal(fmt.minusIn('stopped 20/100'), 'stopped 20/100');
  assert.equal(fmt.minusIn(fmt.minusIn('-1e-3')), '−1e−3', 'and it is idempotent');
});

test('the schedule and the drift note read as numbers, not as identifiers', () => {
  assert.equal(showValue(-0.2, 'V'), '−0.200');
  assert.equal(showValue([-0.2, 1], 'V'), '−0.200, 1.000');
});

test('one definition of the glyph, so the axis and the table cannot drift apart', () => {
  // The defect was two modules each holding their own answer. `scale.MINUS` is
  // a re-export now, and `===` on a one-character string is the whole proof.
  assert.equal(scale.MINUS, fmt.MINUS);
  assert.equal(fmt.MINUS, '−');
});

// -- the machine's half -----------------------------------------------------

test('a value on its way back into an <input> keeps its ASCII hyphen', () => {
  // `lib/card.js: input()` renders `typed(spec)` raw and `display()` renders
  // it through `minus`. This is the invariant underneath that split: the
  // console commits `Number(raw)` on change, and a real minus does not parse.
  assert.ok(Number.isNaN(Number('−0.200')), 'U+2212 is not a number to JS');
  assert.equal(Number('-0.200'), -0.2);
  // And nothing in `format.js` is on that road: the two formatters that render
  // a *name* or a *count* are left alone, so a hyphenated one survives.
  assert.equal(fmt.label('jv-dark', 'V'), 'jv-dark / V');
  assert.equal(fmt.plural(1, 'module-run'), '1 module-run');
  assert.equal(fmt.keptOf(20, 100), '20/100');
});

test('the keys the results grid finds a cell by stay ASCII', () => {
  // `tKey`/`ledKey` are map keys, not labels: a row built with one spelling
  // and looked up with the other loses every negative cell silently.
  assert.equal(tKey(-40.24), '-40.2');
  assert.equal(ledKey(-1.0005), '-1.000', 'Math.round takes the half toward +∞, which is not this file\'s business');
  const rows = new Map();
  rows.set(tKey(-40.24), 'the row');
  assert.equal(rows.get(tKey(-40.24)), 'the row', 'one spelling on both sides');
});

test('the CSV carries the numbers at full precision, in ASCII', () => {
  // `csvOf` is read by a spreadsheet, and a spreadsheet reading `−1.7e−10`
  // gets text. It is built from the raw values and never from a formatter,
  // and this is what keeps it that way.
  const record = fixture('run_grid_sim.json');
  const csv = csvOf(gridModel(record), record);
  assert.ok(csv.includes('e-'), 'the recorded grid does carry negative exponents');
  assert.ok(!csv.includes(fmt.MINUS), 'and not one U+2212 anywhere in the export');
});

test('the run list reads the service\'s own sentence with the signs converted', () => {
  // `outcome_text` is composed in `service/journal.py` and is the longest run
  // of numbers on the results tab. `runs_sim.json` is what the service sent.
  const groups = runGroups(fixture('runs_sim.json'));
  const outcomes = groups.groups.flatMap((g) => g.runs.map((r) => r.outcome)).filter(Boolean);
  assert.ok(outcomes.length, 'the recording has outcomes to read');
  for (const text of outcomes) {
    assert.ok(!/(^|[^0-9A-Za-z_])-[0-9.]|[0-9][eE]-[0-9]/.test(text),
      `a sign left as a hyphen in the run list: ${text}`);
  }
});

test('a file stem is a filename, not a reading', () => {
  assert.equal(fileStem('bench -20 C'), 'bench_-20_C');
  assert.ok(!fileStem('bench -20 C').includes(fmt.MINUS));
});
