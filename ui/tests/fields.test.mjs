// The layout table and the card model — `docs/ui-fields.md` as assertions.
//
// `cardModel` is pure, so every rule this phase argued out is held down here
// against a recorded `GET /modules` with no browser and no service. What the
// artboard says, what the service answers and what the card draws are three
// different things, and this is where the third is pinned to the first two.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { cardModel, foldCount, points, effectivePolarity, driveLevel, BENCH_CARDS } from '../lib/fields.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const CATALOGUE = JSON.parse(fs.readFileSync(path.join(here, '..', 'fixtures', 'modules_sim.json'), 'utf8'));
const byName = Object.fromEntries(CATALOGUE.modules.map((m) => [m.name, m]));

/** A copy of one module's entry with some parameter values changed. */
function entry(name, values = {}) {
  const src = byName[name];
  return {
    ...src,
    params: src.params.map((p) => (p.name in values
      ? { ...p, ...(typeof values[p.name] === 'object' && values[p.name] !== null
        ? values[p.name] : { value: values[p.name] }) }
      : p)),
  };
}

const above = (model) => model.above.flatMap((row) => (row.kind === 'range'
  ? row.names || [row.start.name, row.stop.name, row.step.name]
  : row.kind === 'voc' ? ['voc', 'led_v']
    : row.kind === 'polarity' ? ['output_polarity', 'inverted_output']
      : [row.spec.name]));

test('every bench card is in the catalogue, and park/wait/note are not cards', () => {
  for (const name of BENCH_CARDS) assert.ok(byName[name], `${name} is in GET /modules`);
  for (const name of ['park', 'wait', 'note']) {
    assert.ok(byName[name], `${name} exists`);
    assert.ok(!BENCH_CARDS.includes(name), `${name} is a pipeline node, not a bench card`);
  }
});

test('jv is six fields and a read-back, and no illumination parameter at all', () => {
  const model = cardModel(entry('jv'));
  assert.deepEqual(above(model),
    ['start_v', 'stop_v', 'step_v', 'settle_s', 'both_directions', 'pixel_area_cm2']);
  assert.equal(foldCount(model), 9);
  assert.deepEqual(model.fold.map((g) => g.group), ['sourcemeter']);
  const names = byName.jv.params.map((p) => p.name);
  assert.ok(!names.some((n) => n.startsWith('led') || n === 'dark'),
    'the absence of the illumination parameters is the module');
});

test('the jv card reads the light off the bench, and unknown is not dark', () => {
  // DC reports its level as the *offset* — `set_dc` writes `:VOLT:OFFS` —
  // and this fixture said `high_v`, which is what the row used to read.
  const lit = cardModel(entry('jv'), {
    bench: bench({ open: true, output: true, mode: 'DC', offset_v: 1.02 }),
  });
  assert.equal(lit.readback.lit, true);
  assert.equal(lit.readback.text, '1.02 V');

  const dark = cardModel(entry('jv'), { bench: bench({ open: false, output: true, mode: 'DC' }) });
  assert.equal(dark.readback.lit, false);
  assert.equal(dark.readback.text, 'dark');

  // All three have to be readable. A bench that cannot say is `unknown`, and
  // `unknown` is not `false` — the same rule `illumination_state` applies,
  // because this row predicts the label the run will write.
  for (const led of [{ open: true, output: null, mode: 'DC' },
    { open: null, output: true, mode: 'DC' },
    { open: true, output: true, mode: '?' }]) {
    const model = cardModel(entry('jv'), { bench: bench(led) });
    assert.equal(model.readback.lit, null, JSON.stringify(led));
    assert.equal(model.readback.text, 'unknown');
  }
});

test('bace reads the same length whichever axis is swept, and loses no parameter', () => {
  // Fifteen, not the fourteen `ui-fields.md` first counted: merging
  // `output_polarity` with `inverted_output` into one control brought the
  // boolean above the fold with it, and emptied the `output` fold group.
  // 15 above + 32 folded + 3 not applicable = the module's 50, for every axis.
  for (const axis of ['vpre', 'vcoll', 'delay_ns']) {
    const model = cardModel(entry('bace', { axis_name: axis }));
    const shown = above(model);
    assert.equal(shown.length, 15, `${axis}: ${shown.join(' ')}`);
    assert.equal(foldCount(model), 32, axis);
    assert.deepEqual(model.hidden.sort(),
      ['v_sat', axis, axis === 'vpre' ? 'vpre_on_voc' : 'centre_on_voc'].sort(), axis);
    assert.equal(shown.length + foldCount(model) + model.hidden.length,
      byName.bace.params.length, `${axis}: every parameter is somewhere`);
    // The swept quantity is never also a pinned field.
    assert.ok(!shown.includes(axis), `${axis} is the axis, not a pinned value`);
  }
});

test('hidden means the run will not read it; folded means it will', () => {
  // `v_sat` is only read inside the `measure_dc` branch, so with measure_dc
  // off it is hidden rather than folded — offering it would be a knob that
  // does nothing, which is the trap the polarity pair was.
  const off = cardModel(entry('bace'));
  assert.ok(off.hidden.includes('v_sat'));
  const on = cardModel(entry('bace', { measure_dc: true }));
  assert.ok(!on.hidden.includes('v_sat'));
  assert.equal(above(on).length, 16);
  assert.equal(foldCount(on) + on.hidden.length + 16, byName.bace.params.length);

  // `led_v` on jv_bace is the other case: not inherited is not ignored — a
  // typed level still wins for the run — so it folds and stays reachable.
  const manual = cardModel(entry('jv_bace'));
  assert.ok(!manual.hidden.includes('led_v'));
  assert.ok(manual.fold.some((g) => g.params.some((p) => p.name === 'led_v')));
});

test('centre_on_voc and vpre_on_voc are one decision, and axis_name makes it', () => {
  // `service-contract.md` §5: `vpre_on_voc` is invalid with the vpre axis,
  // where `centre_on_voc` is the flag. Exactly one is ever meaningful, so the
  // card shows that one and hides the other rather than folding it.
  const vpre = cardModel(entry('bace', { axis_name: 'vpre' }));
  assert.ok(above(vpre).includes('centre_on_voc'));
  assert.ok(vpre.hidden.includes('vpre_on_voc'));

  const delay = cardModel(entry('bace', { axis_name: 'delay_ns' }));
  assert.ok(above(delay).includes('vpre_on_voc'));
  assert.ok(delay.hidden.includes('centre_on_voc'));
});

test('the V_oc row carries led_v, and says which of the two states it is in', () => {
  const none = cardModel(entry('bace'));
  const row = none.above.find((r) => r.kind === 'voc');
  assert.ok(row, 'the V_oc row exists');
  assert.equal(row.voc.value, null);
  assert.ok(row.need && /run jv_bace first/.test(row.need.text));
  assert.equal(row.led.name, 'led_v', 'the level lives in this row: one number, one place');

  const derived = cardModel(entry('bace', {
    voc: { value: 1.0423, source: 'derived', detail: 'jv_bace (this session)', editable: false },
  }));
  const got = derived.above.find((r) => r.kind === 'voc');
  assert.equal(got.voc.value, 1.0423);
  assert.equal(got.voc.source, 'derived');
  assert.equal(got.voc.editable, false, 'derived is not typed over');
});

test('v_sat appears only when measure_dc is on', () => {
  assert.ok(!above(cardModel(entry('bace'))).includes('v_sat'));
  assert.ok(above(cardModel(entry('bace', { measure_dc: true }))).includes('v_sat'));
});

test('the two polarity fields are one control, and auto is the only place the bool is read', () => {
  const model = cardModel(entry('bace'));
  const row = model.above.find((r) => r.kind === 'polarity');
  assert.ok(row, 'one row, not two fields');
  assert.ok(!model.fold.some((g) => g.group === 'output'),
    'the output group empties itself once inverted_output is drawn above');

  // `RunConfig.polarity_instruction()`: auto -> inverted_output; everything
  // else ignores it.
  assert.deepEqual(effectivePolarity({ output_polarity: 'auto', inverted_output: true }),
    { write: true, text: 'INV', from: 'inverted_output' });
  assert.deepEqual(effectivePolarity({ output_polarity: 'auto', inverted_output: false }),
    { write: true, text: 'NORM', from: 'inverted_output' });
  assert.equal(effectivePolarity({ output_polarity: 'NORM', inverted_output: true }).text, 'NORM');
  assert.equal(effectivePolarity({ output_polarity: 'leave', inverted_output: true }).write, false);
});

test('the fold is groups with computed counts, in reach order', () => {
  const model = cardModel(entry('bace'));
  assert.deepEqual(model.fold.map((g) => `${g.group} ${g.count}`), [
    'illumination 4', 'acquisition 5', 'timing 6', 'trigger 4', 'processing 4',
    'sourcemeter 9',
  ]);
  // Every count is the length of what is in it. The artboard's typed "17" was
  // stale the day the SMU fields landed; this cannot be.
  for (const group of model.fold) assert.equal(group.count, group.params.length);
  assert.equal(foldCount(model), 32);
});

test('store_shots is folded but not silent', () => {
  assert.deepEqual(cardModel(entry('bace')).chips, []);
  assert.deepEqual(cardModel(entry('bace', { store_shots: true })).chips,
    ['store_shots · every shot kept']);
});

test('jv_bace shows the LED range, or the inherited level instead of it', () => {
  const manual = cardModel(entry('jv_bace'));
  assert.ok(above(manual).includes('led_start_v'));
  assert.ok(!above(manual).includes('led_v'), 'the range is what runs');

  // Inside an illumination loop the loop owns the level: it reads as
  // inherited, showing the value it will get, not as an empty input (§6).
  const bound = cardModel(entry('jv_bace', {
    led_v: { value: 1.02, source: 'inherited', detail: 'illumination loop', editable: false },
  }));
  assert.ok(above(bound).includes('led_v'));
  assert.ok(!above(bound).includes('led_start_v'));
});

test('light shows every field, because its buttons act on them', () => {
  // The one card that hides nothing, and the browser is what said so: `DC`
  // sends `led_v` and `Pulse` sends all four, so a button driving the lamp
  // from a number the operator cannot see is worse than a field they do not
  // need right now.
  const model = cardModel(entry('light'));
  assert.deepEqual(above(model), ['shutter', 'led_mode', 'led_v', 'led_low_v',
    'pulse_frequency_hz', 'duty_percent', 'settle_s']);
  assert.equal(foldCount(model), 0);
  assert.deepEqual(model.hidden, []);

  // Not a Run: a light-only run is `invalid` (`light.undone-by-park`) because
  // the park that ends every run would undo it. One action per click.
  assert.deepEqual(model.run, []);
  assert.deepEqual(model.actions.map((a) => a.action),
    ['shutter-open', 'shutter-shut', 'set-led-dc', 'set-led-pulse', 'led-off']);
  const dc = cardModel(entry('light', { led_v: 1.02 }));
  assert.deepEqual(dc.actions.find((a) => a.action === 'set-led-dc').args, { level: 1.02 });
  assert.deepEqual(dc.actions.find((a) => a.action === 'set-led-pulse').args,
    { level: 1.02, low: 0.4, frequency_hz: 500, duty_percent: 50 });
});

test('the point count is read out of the service estimate, never recomputed', () => {
  assert.equal(points('1 curves × 141 pts ≈ 16 s'), 141);
  assert.equal(points('one shot ≈ 0.8 s (default) · 20 loops × 17 pts ≈ 4.5 min'), 17);
  assert.equal(points('one reading'), null);
  const model = cardModel(entry('jv'));
  assert.equal(model.above.find((r) => r.kind === 'range').points, points(byName.jv.estimate_text));
});

test('every field the cards draw carries an explanation', () => {
  // `ui-rules` §1: the name is the label, not the explanation. A field with no
  // `doc` would reach the operator as a bare `smu_nplc`.
  for (const name of BENCH_CARDS) {
    const model = cardModel(entry(name));
    const specs = [...model.above.flatMap(rowSpecs), ...model.fold.flatMap((g) => g.params)];
    for (const spec of specs) {
      assert.ok(spec.doc && spec.doc.trim(), `${name}.${spec.name} has no doc`);
    }
  }
});

function rowSpecs(row) {
  if (row.kind === 'range') return [row.start, row.stop, row.step];
  if (row.kind === 'voc') return [row.voc, row.led];
  if (row.kind === 'polarity') return [row.mode, row.fallback];
  return [row.spec];
}

function bench(led) {
  return {
    instruments: {
      shutter: { open: led.open, how: 'readback' },
      led: {
        output: led.output, mode: led.mode, high_v: led.high_v ?? null,
        ...('offset_v' in led ? { offset_v: led.offset_v } : {}),
      },
    },
    inferred: [],
  };
}


test('the read-back row shows the level the LED is actually driven at', () => {
  // In DC the level is the *offset*: `set_dc` writes `:VOLT:OFFS`, and
  // `/bench` reports it apart from `high_v`, which keeps the previous pulse
  // amplitude. Showing `high_v` there would make this row predict one level
  // while the `jv` that follows records another — and both come from the same
  // reading, so the disagreement is worse than either being wrong alone.
  const dc = cardModel(entry('jv'), {
    bench: bench({ open: true, output: true, mode: 'DC', high_v: 1.30, offset_v: 1.02 }),
  });
  assert.equal(dc.readback.text, '1.02 V', 'the DC offset, not the stale amplitude');

  const pulse = cardModel(entry('jv'), {
    bench: bench({ open: true, output: true, mode: 'PULSE', high_v: 1.02, offset_v: 0.71 }),
  });
  assert.equal(pulse.readback.text, '1.02 V', 'and the high level when pulsing');

  // Unread is absent, not the other register: `as found lit` is the honest
  // label for a curve taken under light of an unknown level.
  const unread = cardModel(entry('jv'), {
    bench: bench({ open: true, output: true, mode: 'DC', high_v: 1.30 }),
  });
  assert.equal(unread.readback.text, 'lit');
  assert.equal(driveLevel({ mode: 'DC', high_v: 1.3 }), null);
});

test('the card model is what the card is keyed on, so it carries no clock', () => {
  // `views/bench.js` rebuilds a card only when `JSON.stringify(cardModel(...))`
  // moves, which works only because the model is a function of the *values*
  // and not of when they were read. `/bench` is re-read every 700 ms for the
  // length of a run (`lib/watch.js`), so a `read_at` reaching the model would
  // rebuild all six cards twice a second and take the operator's caret with
  // them — which is the defect this keying exists to fix, arriving through the
  // fix. Measured before it: 737 rebuilds per card in four seconds.
  const bench = JSON.parse(fs.readFileSync(
    path.join(here, '..', 'fixtures', 'hello_sim.json'), 'utf8')).data.bench;
  const later = { ...bench, read_at: (bench.read_at || 0) + 42 };

  for (const name of BENCH_CARDS) {
    if (!byName[name]) continue;
    assert.equal(JSON.stringify(cardModel(byName[name], { bench })),
                 JSON.stringify(cardModel(byName[name], { bench: later })),
                 `${name}: a re-read that changed nothing moved the model`);
  }

  // And the converse, or the key would be stable for the wrong reason: a
  // shutter that actually moved has to reach it.
  const moved = { ...bench,
    instruments: { ...bench.instruments,
      shutter: { ...(bench.instruments.shutter || {}), open: !bench.instruments.shutter.open } } };
  const readers = BENCH_CARDS.filter((n) => byName[n] && cardModel(byName[n], { bench }).readback);
  assert.ok(readers.length, 'no card reads the bench, so this proves nothing');
  for (const name of readers) {
    assert.notEqual(JSON.stringify(cardModel(byName[name], { bench })),
                    JSON.stringify(cardModel(byName[name], { bench: moved })),
                    `${name}: the shutter moved and the model did not`);
  }
});
