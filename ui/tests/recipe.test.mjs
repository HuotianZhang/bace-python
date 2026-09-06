// A saved recipe against the bench it is reopened on — `lib/recipe.js`.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { drift, recorded, restore, coveredBy, fileStem, showValue } from '../lib/recipe.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const CATALOGUE = JSON.parse(fs.readFileSync(path.join(here, '..', 'fixtures', 'modules_sim.json'), 'utf8'));
const byName = Object.fromEntries(CATALOGUE.modules.map((m) => [m.name, m]));

/** A recipe whose `bench` is the catalogue as it stands, with some values moved. */
function recipe(moved = {}) {
  const bench = {};
  for (const name of ['jv_bace', 'bace']) {
    bench[name] = Object.fromEntries(byName[name].params.map((p) => [p.name,
      { value: name in moved && p.name in moved[name] ? moved[name][p.name] : p.value, source: p.source }]));
  }
  return { name: 'r', tree: {}, bench };
}

test('a recipe saved on this bench has nothing to say', () => {
  assert.deepEqual(drift(recipe(), byName), []);
  assert.equal(recorded(recipe()), true);
});

test('a bench that moved since the save is listed, parameter by parameter', () => {
  const r = recipe({ bace: { vpre: 0.8, n_loops: 7 } });
  const out = drift(r, byName);
  assert.deepEqual(out.map((d) => [d.module, d.name, d.saved, d.now]),
    [['bace', 'vpre', 0.8, byName.bace.params.find((p) => p.name === 'vpre').value],
     ['bace', 'n_loops', 7, byName.bace.params.find((p) => p.name === 'n_loops').value]]);
  assert.deepEqual(restore(out), { bace: { vpre: 0.8, n_loops: 7 } }, 'the PUT that puts the bench back');
});

test('a module the tree does not use, or the bench no longer has, is not compared', () => {
  const r = recipe();
  r.bench.ghost = { x: { value: 1, source: 'edited' } };
  assert.deepEqual(drift(r, byName), []);
  assert.deepEqual(drift(r, {}), [], 'no catalogue yet: nothing to say rather than everything');
});

test('a recipe from before the bench was recorded is not silently equal', () => {
  const old = { name: 'old', tree: {} };
  assert.equal(recorded(old), false);
  assert.deepEqual(drift(old, byName), []);
});

test('floating point round trips are not drift', () => {
  const r = recipe();
  const vpre = byName.bace.params.find((p) => p.name === 'vpre').value;
  r.bench.bace.vpre.value = vpre + 1e-15;
  assert.deepEqual(drift(r, byName), []);
});

test('a value every node overrides, or a loop binds, is not drift', () => {
  const r = recipe({ bace: { vpre: 0.8, n_loops: 7, led_v: 1.5 } });
  const tree = { kind: 'loop', loop: 'illumination', levels_v: [1.01], children: [
    { kind: 'module', module: 'bace', params: { n_loops: 100 } },
  ] };
  const rows = [{ kind: 'module', path: [0], params: { led_v: { value: 1.01, source: 'inherited' } } }];
  assert.deepEqual(coveredBy(tree, rows), { bace: new Set(['n_loops', 'led_v']) });
  assert.deepEqual(drift(r, byName, { tree, rows }).map((d) => d.name), ['vpre'],
    'n_loops is the node’s own, led_v is the loop’s; only vpre is the bench’s');
  // Two nodes, one overriding: the bench still feeds the other.
  const two = { ...tree, children: [tree.children[0], { kind: 'module', module: 'bace', params: {} }] };
  assert.deepEqual(coveredBy(two, []), { bace: new Set() });
  assert.ok(drift(r, byName, { tree: two }).map((d) => d.name).includes('n_loops'));
});

// -- saved bench settings (`POST /bench/save`) ------------------------------
//
// A bench preset is a recipe record with no tree, so `drift` reads it whole:
// nothing is covered, and every module in the file is compared.

/** What `POST /bench/save` writes: every module, no tree. */
function preset(moved = {}) {
  const bench = {};
  for (const m of CATALOGUE.modules) {
    bench[m.name] = Object.fromEntries(m.params.map((p) => [p.name,
      { value: m.name in moved && p.name in moved[m.name] ? moved[m.name][p.name] : p.value,
        source: p.source }]));
  }
  return { name: 'evening', saved_at: 1, bench };
}

test('a saved bench with no tree compares every module in it', () => {
  assert.equal(recorded(preset()), true);
  assert.deepEqual(drift(preset(), byName), [], 'saved on this bench: nothing moved');
  const out = drift(preset({ bace: { vpre: 0.8 }, jv: { step_v: 0.05 } }), byName);
  assert.deepEqual(out.map((d) => [d.module, d.name, d.saved]),
    [['jv', 'step_v', 0.05], ['bace', 'vpre', 0.8]],
    'both modules, in catalogue order (jv before bace) — no tree covers anything');
  assert.deepEqual(restore(out), { jv: { step_v: 0.05 }, bace: { vpre: 0.8 } },
    'the PUT bodies the bench tab sends, one per module');
});

test('a module the running service no longer has is skipped, not restored', () => {
  const p = preset({ bace: { vpre: 0.8 } });
  p.bench.gone = { made_up: { value: 1, source: 'edited' } };
  assert.deepEqual(drift(p, byName).map((d) => d.module), ['bace']);
});

// The stem the service writes, in JS — `app.py: _RECIPE_STEM`. The same table
// is asserted against the service in
// `tests/test_service_api.py: test_a_name_becomes_the_same_file_stem_on_both_sides`.
const STEMS = [
  ['cool down', 'cool_down'],
  ['before/the dark scan', 'before_the_dark_scan'],
  ['  evening  ', 'evening'],
  ['a.b-c_1', 'a.b-c_1'],
  ['!!!', ''],
  ['', ''],
  ['_leading and trailing_', 'leading_and_trailing'],
  ['20260906', '20260906'],
];

test('a name becomes the file stem the service writes', () => {
  for (const [typed, stem] of STEMS) assert.equal(fileStem(typed), stem, typed);
  assert.equal(fileStem(null), '');
  assert.equal(fileStem(undefined), '');
});

test('a drifted value is shown the way the cards show it', () => {
  assert.equal(showValue(1.02, 'V'), '1.020');
  assert.equal(showValue(7, ''), '7');
  assert.equal(showValue(0.5, ''), '0.500');
  assert.equal(showValue([1, 2.5], 'V'), '1.000, 2.500');
  assert.equal(showValue(null, 'V'), '—');
  assert.equal(showValue('shut', ''), 'shut');
});
