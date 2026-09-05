// What is mounted — `lib/identity.js`.
//
// The block names every folder the session writes, and until `PUT
// /session/sample` there was no way to type it without editing `run.toml` and
// restarting the process that owns every instrument. What is asserted here is
// the part that is a *judgement* rather than an input box: which fields reach
// the folder name, what the name will be, the two characters that break it,
// and the way back to the file.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createStore } from '../lib/store.js';
import { identityModel, identityOf, nameHazard, IDENTITY_FIELDS } from '../lib/identity.js';

const session = (sample, file) => ({ session: { sample, sample_file: file === undefined ? sample : file } });

test('an unnamed session says what its folders will be called instead', () => {
  const model = identityModel(session({ sample: '', material: '', pixel: '' }));
  assert.equal(model.unnamed, true);
  assert.equal(model.chip, 'no sample named');
  assert.equal(model.stem, '');
  assert.match(model.preview, /no device in the name/,
    'the consequence, not the absence — an unnamed run opens on the temperature');
});

test('the preview is the folder name the next run starts with, in the archive’s order', () => {
  // `storage.naming.folder_name`: sample, material, pixel, then the
  // temperature, the LED level, the V_oc and the stamp — which are not this
  // panel's, and the ellipsis is where they go.
  const model = identityModel(session({ sample: 's4', material: 'PTQ10IT4F', pixel: 'pxa' }));
  assert.equal(model.stem, 's4_PTQ10IT4F_pxa');
  assert.equal(model.preview, 's4_PTQ10IT4F_pxa_…');
  assert.equal(model.chip, 's4 · PTQ10IT4F · pxa');
  assert.equal(model.unnamed, false);
});

test('a partly named device is named', () => {
  // Three fields and any of them will do: `folder_name` drops the empty ones.
  const model = identityModel(session({ sample: 's4', material: '', pixel: '' }));
  assert.equal(model.unnamed, false);
  assert.equal(model.stem, 's4');
});

test('a value the folder name will spell differently says so, and why', () => {
  // Since 2026-09-05 all three identity fields are slugged into the name
  // (`naming-plan.md` §2 — `material = "PTQ10:IT-4F"`, the contract's own
  // example, was a colon in a Windows path segment). Nothing is an error any
  // more: the metadata keeps the value and the name is reduced, the same
  // split the comment has always had. What the operator needs is to see it.
  //
  // The reduction itself is the service's (`sample_in_name`) — a second
  // `slug()` here would be a second answer to "what will this be called".
  assert.equal(nameHazard('s4', 's4'), null, 'the ordinary case says nothing');
  assert.equal(nameHazard('', ''), null);

  const colon = nameHazard('PTQ10:IT-4F', 'PTQ10IT-4F');
  assert.equal(colon.filed, 'PTQ10IT-4F');
  assert.match(colon.why, /cannot hold that character/);

  assert.match(nameHazard('s4 pixel a', 's4-pixel-a').why, /space/);
  assert.match(nameHazard('a_b', 'a-b').why, /underscore/);

  // The one that matters most, and the one the character rules cannot
  // explain: two batches differing after character 24 are one folder name.
  const long = nameHazard('PTQ10IT4F-batch-2026-08-A', 'PTQ10IT4F-batch-2026-08');
  assert.equal(long.why, null);
  assert.match(long.text, /too long/);

  // Reduced to nothing: the field leaves the name entirely.
  const gone = nameHazard('///', '');
  assert.equal(gone.filed, '');
  assert.match(gone.text, /left out of the name/);
});

test('the folder preview is what the folder will say, not what was typed', () => {
  const state = {
    session: {
      sample: { sample: 's4', material: 'PTQ10:IT-4F', pixel: 'px a' },
      sample_file: {},
      sample_in_name: { sample: 's4', material: 'PTQ10IT-4F', pixel: 'px-a' },
    },
  };
  const model = identityModel(state);
  assert.equal(model.stem, 's4_PTQ10IT-4F_px-a');
  assert.equal(model.chip, 's4 · PTQ10:IT-4F · px a', 'the chip is the identity as recorded');
  assert.deepEqual(model.hazards.map((r) => r.name), ['material', 'pixel']);
});

test('a service too old to answer with the reduction is drawn as it would write it', () => {
  const model = identityModel(session({ sample: 's4', material: 'M' }));
  assert.equal(model.stem, 's4_M');
  assert.equal(model.hazards.length, 0);
});

test('temperature_k is not a field this panel offers', () => {
  // It is a legal `[sample]` key and the route takes it. `run.toml` says why
  // nobody should type it: every recipe said 290, and a run at 220 K was filed
  // as "290 K, typed". A field here would rebuild that with a nicer surface.
  assert.equal(IDENTITY_FIELDS.some((f) => f.name === 'temperature_k'), false);
  assert.deepEqual(IDENTITY_FIELDS.map((f) => f.name),
    ['sample', 'material', 'pixel', 'operator', 'comment']);
  assert.equal(IDENTITY_FIELDS.filter((f) => f.in_name === true).length, 3,
    'three reach the folder name; operator does not and comment goes in as a slug');
  assert.ok(IDENTITY_FIELDS.every((f) => f.doc), '`ui-rules` §1: never the name alone');
});

test('a field typed here shows the way back to the file, and one that was not does not', () => {
  const model = identityModel(session(
    { sample: 's7', material: 'SIM' },
    { sample: 's4', material: 'SIM' }));
  const byName = Object.fromEntries(model.rows.map((r) => [r.name, r]));
  assert.equal(byName.sample.typed, true);
  assert.equal(byName.sample.file, 's4', 'and what it would go back to');
  assert.equal(byName.material.typed, false, 'the file said this, so there is nothing to take back');
  assert.equal(byName.operator.typed, false, 'empty on both sides');
});

test('SampleNamed replaces the block, so a second console is not left filing under the old name', () => {
  const store = createStore({ schedule: () => {} });
  store.applyFrame({ seq: 1, ts: 1, run_id: null, node_path: '', type: 'SessionStarted',
    data: { session_id: 'x', mode: 'sim', sample: { sample: 's4', operator: 'hz' } } });
  store.applyFrame({ seq: 2, ts: 2, run_id: null, node_path: '', type: 'SampleNamed',
    data: { before: { sample: 's4', operator: 'hz' }, after: { sample: 's7' }, changed: ['operator', 'sample'] } });
  const model = identityModel(store.getState());
  assert.equal(model.rows.find((r) => r.name === 'sample').value, 's7');
  assert.equal(model.rows.find((r) => r.name === 'operator').value, '',
    'a replacement, not a merge — the merge happened in the service');
});

test('identityOf is the chip the bar has always drawn', () => {
  assert.equal(identityOf({ sample: 's4', material: 'M', pixel: 'p' }), 's4 · M · p');
  assert.equal(identityOf({}), null);
  assert.equal(identityOf(null), null);
});
