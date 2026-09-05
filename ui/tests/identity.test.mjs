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

test('an ugly folder name is warned about and an impossible one is refused', () => {
  // `naming-plan.md` §"Fields collide" lists three, and they are two kinds of
  // thing. Ugly: `a_b` forges a field boundary, `s4 pixel a` puts a space in a
  // directory name (the 2026-09-01 bug). Nothing refuses either — `run.toml`
  // may hold them — so the warning is the whole of the protection.
  assert.equal(nameHazard('a_b').level, 'warn');
  assert.match(nameHazard('a_b').text, /underscore/);
  assert.equal(nameHazard('s4 pixel a').level, 'warn');
  assert.equal(nameHazard('s4'), null);
  assert.equal(nameHazard(''), null, 'an empty field is not named, not badly named');

  // Impossible, and the service refuses all three: two directories, a colon
  // that fails on the lab PC and passes here, and a climb out of `runs/`.
  assert.equal(nameHazard('a/b').level, 'refused');
  assert.equal(nameHazard('../../etc').level, 'refused');
  assert.equal(nameHazard('..').level, 'refused');
  const colon = nameHazard('PTQ10:IT-4F');
  assert.equal(colon.level, 'refused');
  // The remedy, not only the complaint — and the same string `slug()` gives,
  // which is what the service's own refusal names.
  assert.equal(colon.suggest, 'PTQ10IT-4F');
  assert.equal(nameHazard('a/b').suggest, 'ab');

  const model = identityModel(session({ sample: 's4 pixel a', material: 'a/b', comment: 'a/b: fine' }));
  assert.deepEqual(model.hazards.map((r) => r.name), ['sample', 'material'],
    'the comment is slugged on the way in, so it is never one');
  assert.deepEqual(model.refused.map((r) => r.name), ['material']);
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

test('a refused value stays in its field, with the remedy beside it', () => {
  // It never reached the block — the service would not take it — so a model
  // drawn from the block alone would clear the operator's typing on the very
  // re-render the refusal causes, and never draw the `use …` that exists for
  // this case. The chip and the folder stem stay the service's: nothing will
  // ever be filed under a name it refused.
  const state = session({ sample: 's4', material: '' }, { sample: '', material: '' });
  const model = identityModel(state, { rejected: { name: 'material', value: 'PTQ10:IT-4F' } });
  const material = model.rows.find((r) => r.name === 'material');
  assert.equal(material.value, 'PTQ10:IT-4F', 'still in the field');
  assert.equal(material.refused, true);
  assert.equal(material.hazard.level, 'refused');
  assert.equal(material.hazard.suggest, 'PTQ10IT-4F');
  assert.equal(model.stem, 's4', 'the stem is what the service holds, not what it refused');
  assert.equal(model.chip, 's4');
});

test('identityOf is the chip the bar has always drawn', () => {
  assert.equal(identityOf({ sample: 's4', material: 'M', pixel: 'p' }), 's4 · M · p');
  assert.equal(identityOf({}), null);
  assert.equal(identityOf(null), null);
});
