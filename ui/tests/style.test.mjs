// The stylesheet: rules that place an element over the page.
//
// `style.css` is one global sheet with bare class names. A fixed panel styled
// by a bare class also styles any other element that carries the class: the
// bar's `plans` chip once took the plans panel's `position: fixed` and sat,
// invisible, over the assistant's header. A fixed rule therefore names its
// element as well as its class.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(here, '..', 'style.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');

/** Every `selector { … }` rule whose declarations include `position: fixed`. */
function fixedSelectors(sheet) {
  const out = [];
  for (const m of sheet.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (/position\s*:\s*fixed/.test(m[2])) out.push(...m[1].split(',').map((s) => s.trim()));
  }
  return out;
}

test('a fixed rule names its element, not a bare class', () => {
  const selectors = fixedSelectors(css);
  assert.ok(selectors.length > 0, 'the sheet has fixed panels to check');
  for (const s of selectors) {
    assert.match(s, /^[a-z][a-z0-9-]*[.#[]/, `${s}: qualify it with its element, e.g. aside.name`);
  }
});

test('the check catches the collision it is for', () => {
  assert.deepEqual(fixedSelectors('.plans { position: fixed; } aside.ok { position:fixed }'),
    ['.plans', 'aside.ok']);
});
