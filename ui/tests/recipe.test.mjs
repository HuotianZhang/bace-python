// A saved recipe against the bench it is reopened on — `lib/recipe.js`.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { drift, recorded, restore, coveredBy } from '../lib/recipe.js';

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
