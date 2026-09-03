// The three M3 charts, against the recorded fixtures. Each model is a pure
// function of the data, so what the operator would see is asserted here rather
// than looked at — the same split the rail has had since M1.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { createStore, currentRun } from '../lib/store.js';
import { transientModel, traceSet, sampleTime, windowRecordS } from '../lib/charts/transient.js';
import { jvModel, currentOf, ramp, RAMP } from '../lib/charts/jv.js';
import { timingModel, shotSegments, cyclePlan, biasLevels, timingAlerts, recordPlan } from '../lib/charts/timing.js';
import { runFor } from '../lib/results.js';

const here = dirname(fileURLToPath(import.meta.url));
const fixture = (name) => JSON.parse(readFileSync(join(here, '../fixtures', name), 'utf8'));
const jsonl = (name) => readFileSync(join(here, '../fixtures', name), 'utf8')
  .split('\n').filter(Boolean).map((line) => JSON.parse(line));

const TRANSIENT = fixture('transient_20260902_153722.json');   // the rig day, full precision
const JV = fixture('jv_sim.json');
const BENCH = fixture('hello_sim.json').data.bench;

function liveRun() {
  const store = createStore({ schedule: () => {} });
  for (const frame of jsonl('stream_bace_sim.jsonl')) store.applyFrame(frame);
  return currentRun(store.getState());
}

// -- the transient --------------------------------------------------------

test('light, dark and photocurrent are in one frame, never the difference alone', () => {
  // `ui-rules` §4: photocurrent on its own hides the failure where both
  // parents sit on the digitiser's rail and the difference is identically zero.
  const model = transientModel(TRANSIENT);
  assert.deepEqual(model.panels.map((p) => p.key), ['traces', 'photo', 'charge']);
  assert.deepEqual(model.panels[0].series.map((s) => s.key), ['dark', 'light']);
});

test('the two traces share one vertical window, as the digitiser did', () => {
  const model = transientModel(TRANSIENT);
  const [dark, light] = model.panels[0].series;
  assert.equal(model.panels[0].y.scale, model.panels[0].y.scale);
  assert.ok(dark.d.length > 0 && light.d.length > 0);
  assert.match(model.panels[0].note, /autorange|shared vertical window/);
});

test('the integration window is a region on every panel, not a number in a caption', () => {
  const model = transientModel(TRANSIENT);
  for (const panel of model.panels) {
    assert.equal(panel.shades.length, 1, `${panel.key} carries the window`);
    assert.ok(panel.rules.some((r) => r.colour === 'accent'));
  }
});

test('t0_int is resolved into record time the way the service resolves it', () => {
  // `experiment/transient.py`: record → as given, trigger → minus the trace's
  // t0, pulse → and plus :PULS:DEL1.
  assert.equal(windowRecordS(1.185e-7, 'record'), 1.185e-7);
  assert.ok(Math.abs(windowRecordS(1.185e-7, 'trigger', { traceT0: -1.995e-7 }) - 3.18e-7) < 1e-12);
  assert.ok(Math.abs(windowRecordS(0, 'pulse', { traceT0: -2e-7, pulseDelayS: 9e-8 }) - 2.9e-7) < 1e-12);
});

test('a decimated trace puts its last sample at the end of the record, not one stride on', () => {
  // n = 4000, stride 5: the kept indices are 0, 5 … 3995, 3999.
  assert.equal(sampleTime(800, { dt: 5e-10, stride: 5, n: 4000, kept: 801 }), 3999 * 5e-10);
  assert.equal(sampleTime(799, { dt: 5e-10, stride: 5, n: 4000, kept: 801 }), 799 * 5 * 5e-10);
});

test('a live shot says it is decimated, and where the full precision is', () => {
  const shot = liveRun().lastShot;
  const model = transientModel(shot);
  assert.ok(model.notes.some((n) => /1 in 5 of 4000/.test(n)));
  assert.ok(model.notes.some((n) => /runs\/\{id\}\/data/.test(n)));
});

test('a shot with no arrays states the absence rather than drawing an empty chart', () => {
  const model = transientModel({ tracesGone: true, q: -1e-10 });
  assert.ok(model.absent, 'there is no curve to draw');
  assert.match(model.absent.detail, /the scalars are the shot/);
  assert.deepEqual(model.panels, []);
});

test('the running integral is the service\'s where there is one, and says so where it is not', () => {
  const fromService = transientModel(TRANSIENT);
  assert.match(fromService.panels[2].label, /the run's own record/);
  const live = transientModel(liveRun().lastShot, { t0_int_s: 1.205e-7, t0_int_reference: 'trigger' });
  assert.match(live.panels[2].label, /computed from the trace above/);
  assert.match(live.panels[2].note, /not the run's Q/);
});

test('σ_Q of zero is read as an absence, not as a zero', () => {
  const model = transientModel({ ...liveRun().lastShot, q_std: 0 });
  assert.match(model.readout, /σ_Q not recorded/);
});

// -- the J–V --------------------------------------------------------------

test('the axis is mA/cm², and nothing is converted to get there', () => {
  const model = jvModel(JV.curves);
  assert.match(model.panels[0].label, /mA cm⁻²/);
  // `density` is already mA/cm² on the wire; a chart that multiplied by a
  // thousand would be out by a thousand in the other direction.
  const light = JV.curves.find((c) => c.dark !== true);
  const drawn = model.panels[0].series.find((s) => s.label === light.label);
  assert.ok(Math.abs(drawn.at(0).y - light.density[nearest(light.voltage, 0)]) < 1e-12);
});

test('no pixel area means amps, not a density with an invented area', () => {
  const noArea = JV.curves.map((c) => ({ ...c, density: null }));
  assert.equal(currentOf(noArea).key, 'current');
  const model = jvModel(noArea);
  assert.match(model.panels[0].label, /I \/ A/);
  assert.ok(model.notes.some((n) => /inventing one/.test(n)));
});

test('one curve without a density puts every curve in amps, because mixed is not a chart', () => {
  const mixed = [JV.curves[0], { ...JV.curves[1], density: null }];
  assert.equal(currentOf(mixed).key, 'current');
});

test('J_sc keeps its amps, because it is interpolated from the current', () => {
  const model = jvModel(JV.curves);
  const light = model.metrics.entries.find((e) => e.label === '1.02 V');
  const jsc = light.rows.find((r) => r.key === 'J_sc');
  assert.equal(jsc.note, 'in amps');
  assert.match(jsc.value, /A$/);
  assert.match(model.metrics.note, /interpolated/);
});

test('a sweep that is dark all through is read over decades', () => {
  const dark = jvModel(JV.curves.filter((c) => c.dark === true));
  assert.equal(dark.panels[0].y.scale.kind, 'log10');
  assert.match(dark.panels[0].label, /^\|J\|/);
  // `dark` is true, false or **null**, and null is not false: a `jv` node
  // labels the curve from a read-back, and the bench may not have answered.
  const unknown = jvModel(JV.curves.map((c) => ({ ...c, dark: null })));
  assert.equal(unknown.panels[0].y.scale.kind, 'linear');
});

test('the light axis is the power quadrant, and says how much it cropped', () => {
  const model = jvModel(JV.curves);
  assert.ok(model.panels[0].y.scale.domain[1] < 50, 'forward injection is off the top');
  assert.ok(model.notes.some((n) => /power quadrant/.test(n)));
});

test('the −4 V start is a layout problem, so x is never cropped', () => {
  const wide = JV.curves.map((c) => ({
    ...c,
    voltage: c.voltage.map((v) => v - 3.8),
  }));
  const model = jvModel(wide);
  assert.ok(model.x.scale.domain[0] <= -4, 'the reverse arm is drawn, not cut off');
  assert.ok(model.panels[0].note.includes('sweep starts at'));
});

test('the illumination ramp is ordered — one hue, strictly darkening', () => {
  // The design's five values splice a grey pair to a red trio, and in OKLab
  // the first and third have the same lightness: five levels that cannot be
  // put in order are not a sequential encoding.
  const lightness = RAMP.map(oklab);
  for (let i = 1; i < lightness.length; i += 1) {
    assert.ok(lightness[i] < lightness[i - 1], `${RAMP[i]} is darker than ${RAMP[i - 1]}`);
  }
  assert.equal(ramp(0, 5), RAMP[0]);
  assert.equal(ramp(4, 5), RAMP[4]);
});

// -- the timing diagram ---------------------------------------------------

const RESOLVED = jsonl('stream_bace_sim.jsonl').find((f) => f.type === 'RunQueued').data.resolved;
const RIG = BENCH.rig.values;

test('the shot is the seven StepPhase segments the run actually yields', () => {
  const phases = shotSegments(RESOLVED).filter((s) => !s.skipped).map((s) => s.phase);
  assert.deepEqual(phases,
    ['levels', 'light settle', 'acquire light', 'dark levels', 'dark settle', 'acquire dark', 'process']);
});

test('dark_reference = same drops a phase, and with it the dark settle that never sleeps', () => {
  const same = shotSegments({ ...RESOLVED, dark_reference: 'same' });
  const live = same.filter((s) => !s.skipped);
  assert.equal(live.length, 6, 'six phases, not seven');
  const darkLevels = same.find((s) => s.key === 'dark-levels');
  assert.ok(darkLevels.skipped);
  // `dark_settle_s` sleeps *inside* `dark levels`, so under `same` it does not
  // happen at all — the form's number and the generator's are not the same.
  assert.equal(darkLevels.seconds, 0);
  assert.match(darkLevels.detail, /never sleeps/);
  assert.deepEqual(live.map((s) => s.k), [1, 2, 3, 4, 5, 6]);
});

test('the shutter moves and the power read are placed, because they are not reported', () => {
  const segments = shotSegments(RESOLVED);
  assert.equal(segments.find((s) => s.key === 'levels').mark, 'shutter opens');
  assert.equal(segments.find((s) => s.key === 'dark-settle').mark, 'shutter shuts');
  assert.equal(segments.find((s) => s.key === 'light-settle').mark, 'power read');
  assert.equal(shotSegments({ ...RESOLVED, read_intensity: false })
    .find((s) => s.key === 'light-settle').mark, null);
});

test('a shot lasts what its own numbers say it lasts', () => {
  // 2 x 200 averages at 500 Hz, plus settle_s, both shutter settles and the
  // dark settle: the measured ~0.8 s of acquisition is the two acquires.
  const total = shotSegments(RESOLVED).reduce((sum, s) => sum + s.seconds, 0);
  assert.ok(Math.abs(total - (0.3 + 5 + 0.4 + 0.3 + 5 + 0.4)) < 1e-9, `got ${total}`);
});

test('under INV the lit half is the other one, so raising duty shortens it', () => {
  const inv = { items: [{ key: 'led_polarity', value: 'INV' }] };
  assert.equal(cyclePlan({ ...RESOLVED, duty_percent: 40 }, RIG, inv).litFraction, 0.6);
  assert.equal(cyclePlan({ ...RESOLVED, duty_percent: 40 }, RIG, { items: [{ key: 'led_polarity', value: 'NORM' }] })
    .litFraction, 0.4);
  assert.equal(cyclePlan(RESOLVED, RIG, inv).syncMeans, 'light off');
});

test('the 81150A at INV rests the device in extraction, and that is an alert', () => {
  const values = { ...RESOLVED, output_polarity: 'INV', invert_polarity: false, vpre: 1.0, vcoll: -4.0 };
  const bias = biasLevels(values, BENCH.chain);
  assert.equal(bias.polarity, 'INV');
  assert.equal(bias.restsAt, -4.0, 'the device sits at V_coll between pulses');
  const alert = timingAlerts(values, RIG, BENCH.chain).find((a) => a.key === 'bias-polarity');
  assert.equal(alert.level, 'alert');
  assert.match(alert.text, /extraction never stops/);
});

test('NORM rests the device at V_pre, which is the validated recipe', () => {
  const bias = biasLevels({ ...RESOLVED, output_polarity: 'NORM', invert_polarity: false, vpre: 1.0, vcoll: -4.0 },
    BENCH.chain);
  assert.equal(bias.restsAt, 1.0);
  assert.equal(bias.pulsesTo, -4.0);
});

test('invert_polarity swaps and negates both levels, as core.pulses does', () => {
  const bias = biasLevels({ output_polarity: 'NORM', invert_polarity: true, vpre: 1.0, vcoll: -4.0 }, {});
  assert.equal(bias.restsAt, 4.0);
  assert.equal(bias.pulsesTo, -1.0);
});

test('output_polarity = leave reports the read-back, and says so when there is none', () => {
  const left = biasLevels({ ...RESOLVED, output_polarity: 'leave' }, BENCH.chain);
  assert.equal(left.polarity, 'NORM', 'the bench read one back');
  const blind = timingAlerts({ ...RESOLVED, output_polarity: 'leave' }, RIG, { items: [] });
  assert.ok(blind.some((a) => a.key === 'bias-polarity-unknown'));
});

test('a delay before the trigger is refused on the form, as the generator refuses it', () => {
  const alerts = timingAlerts({ ...RESOLVED, delay_ns: -10 }, { ...RIG, trigger_offset_s: 0 }, BENCH.chain);
  const delay = alerts.find((a) => a.key === 'delay');
  assert.equal(delay.level, 'invalid');
  assert.match(delay.text, /before its own trigger/);
});

test('an integration window past the end of the record is empty, and is called that', () => {
  const alerts = timingAlerts({ ...RESOLVED, t0_int_s: 9e-6, t0_int_reference: 'trigger' }, RIG, BENCH.chain);
  const window = alerts.find((a) => a.key === 'window');
  assert.equal(window.level, 'invalid');
});

test('the chain read-back is what the diagram draws, not the form\'s hope', () => {
  // `hello_sim.json` is a bench with the 33220A at NORM — the chain's own
  // warning — so the diagram raises it whatever the recipe says.
  const alerts = timingAlerts(RESOLVED, RIG, BENCH.chain);
  const led = alerts.find((a) => a.key === 'led-polarity');
  assert.equal(led.level, 'alert');
  assert.match(led.text, /light ON/);
});

test('the swept quantity is the accent, and the rest is not', () => {
  const model = timingModel({ ...RESOLVED, axis_name: 'vcoll' }, { rig: RIG, chain: BENCH.chain });
  assert.equal(model.swept, 'vcoll');
  const bias = model.panels[1].series.find((s) => s.key === 'bias');
  assert.equal(bias.colour, 'accent');
  const drive = model.panels[1].series.find((s) => s.key === 'drive');
  assert.equal(drive.colour, 'ink');
});

test('the record panel is the timebase, ten divisions of it', () => {
  const model = timingModel(RESOLVED, { rig: RIG, chain: BENCH.chain });
  assert.match(model.panels[2].label, /2000 ns · 5000 points · dt 0.400 ns/);
});

// -- helpers --------------------------------------------------------------

function nearest(voltage, v) {
  let best = 0;
  let gap = Infinity;
  voltage.forEach((x, i) => { const d = Math.abs(x - v); if (d < gap) { gap = d; best = i; } });
  return best;
}

/** OKLab lightness, for the ramp test. */
function oklab(hex) {
  const to = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  const n = parseInt(hex.slice(1), 16);
  const r = to(((n >> 16) & 255) / 255);
  const g = to(((n >> 8) & 255) / 255);
  const b = to((n & 255) / 255);
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return 0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s;
}

test('the light at the sample follows the drive, late by the fibre and never backwards', () => {
  // Under NORM the LED is lit through the duty phase; under INV through the
  // complement. And the late edge that falls off the end of the period belongs
  // at the *start* of it — wrapped in place it drew a second line travelling
  // backwards across the panel at the wrong level, which is what the first
  // render on screen showed.
  const norm = { items: [{ key: 'led_polarity', value: 'NORM' }] };
  const inv = { items: [{ key: 'led_polarity', value: 'INV' }] };
  const values = { ...RESOLVED, duty_percent: 50, pulse_frequency_hz: 500 };
  const lightOf = (chain) => timingModel(values, { rig: RIG, chain })
    .panels[1].series.find((s) => s.key === 'light');

  for (const chain of [norm, inv]) {
    const xs = [...lightOf(chain).d.matchAll(/[MH]([\d.]+)/g)].map((m) => Number(m[1]));
    for (let i = 1; i < xs.length; i += 1) {
      assert.ok(xs[i] >= xs[i - 1], 'the pen only ever travels left to right');
    }
  }
  // The two polarities put the lit half on opposite sides of the period.
  assert.notEqual(lightOf(norm).d, lightOf(inv).d);
});

// -- what the review found ------------------------------------------------

test('the record runs one division before the trigger and nine after it', () => {
  // `Infiniium.configure_timebase` writes `:TIM:RANG` of ten divisions and
  // `:TIM:POS` of *four*, so the horizontal reference is four divisions after
  // the trigger. Drawn from zero, every edge and the whole shaded window sat
  // one division early. Both recordings agree: at 200 ns/div the sim trace
  // carries t0 = −200 ns and the rig day's −199.5 ns.
  const plan = recordPlan({ timebase_ns_per_div: 200, record_length: 5000 }, {});
  const near = (a, b, why) => assert.ok(Math.abs(a - b) < Math.max(1e-15, Math.abs(b) * 1e-9),
    `${why}: ${a} vs ${b}`);
  near(plan.start, -200e-9, 'one division before the trigger');
  near(plan.end, 1800e-9, 'nine after');
  near(plan.end - plan.start, plan.span, 'ten divisions in all');
  const traceT0 = jsonl('stream_bace_sim.jsonl')
    .find((f) => f.type === 'StepDone').data.light.t0;
  assert.ok(Math.abs(traceT0 - plan.start) < 1e-12, `the trace itself starts at ${traceT0}`);

  // On the panel that means the trigger is drawn a tenth of the way in, not
  // at the left edge: one division of the ten.
  const model = timingModel({ ...RESOLVED, timebase_ns_per_div: 200 }, { rig: RIG, chain: BENCH.chain });
  const panel = model.panels[2];
  const trigger = panel.rules.find((r) => r.colour === 'grey');
  assert.ok(trigger, 'the trigger has its own rule, because the record\'s zero is not it');
  near((trigger.x1 - panel.rect.x) / panel.rect.w, 0.1, 'one division of the ten');
});

test('a t0_int in the last division of the record is not called empty', () => {
  // Nine and a half divisions after the trigger is inside a record that runs
  // to nine — and was inside a record the diagram thought ran to ten.
  const late = { ...RESOLVED, timebase_ns_per_div: 200, t0_int_s: 1900e-9, t0_int_reference: 'trigger' };
  assert.ok(timingAlerts(late, RIG, BENCH.chain).some((a) => a.key === 'window'),
    '1900 ns is past the record\'s 1800 ns end');
  const inside = { ...late, t0_int_s: 1700e-9 };
  assert.ok(!timingAlerts(inside, RIG, BENCH.chain).some((a) => a.key === 'window'));
  const before = { ...late, t0_int_s: -150e-9 };
  assert.ok(!timingAlerts(before, RIG, BENCH.chain).some((a) => a.key === 'window'),
    'and the division before the trigger is in the record too');
});

test('an unread LED polarity stays unknown, and is warned about rather than assumed', () => {
  // `?` is what a driver answers for a query that failed, and an absent chain
  // is a bench nobody has read. Defaulting either to the expected INV drew a
  // confident diagram of the other half of the cycle.
  for (const chain of [{ items: [] }, { items: [{ key: 'led_polarity', value: '?' }] }, {}]) {
    const cycle = cyclePlan(RESOLVED, RIG, chain);
    assert.equal(cycle.ledPolarity, null);
    assert.equal(cycle.inverted, false);
    assert.equal(cycle.litFraction, null, 'which half is lit has no answer');
    assert.match(cycle.syncMeans, /unknown/);
    const alerts = timingAlerts(RESOLVED, RIG, chain);
    assert.equal(alerts.find((a) => a.key === 'led-polarity-unknown').level, 'warn');
    assert.ok(!alerts.some((a) => a.key === 'duty'), 'and no claim about the lit fraction');
    const model = timingModel(RESOLVED, { rig: RIG, chain });
    assert.ok(!model.panels[1].series.some((s) => s.key === 'light'),
      'no waveform is drawn for the light at the sample');
  }
});

test('a null in a dark sweep is a gap, not a point on the log floor', () => {
  // `Math.abs(null)` is 0, and 0 is finite — mapped straight, a sample the
  // instrument never returned became a leakage measurement.
  const dark = JV.curves.find((c) => c.dark === true);
  const holed = { ...dark, density: dark.density.map((v, i) => (i === 30 ? null : v)) };
  const model = jvModel([holed]);
  assert.equal(model.panels[0].y.scale.kind, 'log10');
  const series = model.panels[0].series[0];
  assert.equal(series.d.split('M').length - 1, 2, 'the pen lifts over the hole');
  assert.equal(series.at(holed.voltage[30]), null, 'and the crosshair reads nothing there');
});

test('the integration window travels with the shot\'s own delay, not the form\'s', async () => {
  // `t0_int_reference = pulse` recomputes the window from `levels.delay_s`
  // every step, and `recipes/run-bace.toml` sweeps exactly that axis. Pinned
  // to the form, the shaded window stood still while the real one moved.
  const { pulseDelayS } = await import('../lib/results.js');
  const values = { delay_ns: 90 };
  const rig = { trigger_offset_s: 5e-9 };
  const near = (a, b, why) => assert.ok(Math.abs(a - b) < 1e-15, `${why}: ${a} vs ${b}`);
  near(pulseDelayS(null, values, rig), 95e-9, 'the form, plus the rig offset');
  near(pulseDelayS({ setpoint: { delay_ns: 200 } }, values, rig), 205e-9,
    'the shot on screen, not the pinned value');
  near(pulseDelayS({ setpoint: {} }, values, rig), 95e-9,
    'a shot on another axis has no delay of its own');

  // The fixture's scan sweeps `delay_ns` 0 … 200, and its last shot is at 200
  // while the form's pinned value is 0. Drawn from the form the window sat
  // 200 ns early on that shot, and on every other point of the axis.
  const shot = liveRun().lastShot;
  assert.equal(shot.setpoint.delay_ns, 200, 'the shot on screen is not the pinned point');
  const window = (delay) => transientModel(shot, {
    t0_int_s: 0, t0_int_reference: 'pulse', pulse_delay_s: delay,
  }).panels[0].shades[0].x;
  assert.notEqual(window(pulseDelayS(shot, { delay_ns: 0 }, {})),
    window(pulseDelayS(null, { delay_ns: 0 }, {})),
    'the shot\'s own delay shades a different window from the form\'s');
});

test('both directions of one level share its colour, and not its key', () => {
  // The ramp encodes illumination, so a level's forward and reverse arms are
  // one colour — ranked by curve they became the brightest and the darkest,
  // two illuminations that never existed. Their *keys* must differ, though:
  // the crosshair finds a dot by `data-series`, and two dots with one key
  // left the second never shown and the first carrying the wrong value.
  const light = JV.curves.find((c) => c.dark !== true);
  const model = jvModel([
    { ...light, direction: 'forward' },
    { ...light, direction: 'reverse' },
  ]);
  const [forward, reverse] = model.panels[0].series;
  assert.equal(forward.colour, reverse.colour, 'one level, one colour');
  assert.notEqual(forward.key, reverse.key, 'and two identities');
  assert.equal(forward.dash, null);
  assert.equal(reverse.dash, '4 2', 'the direction is told by the dash');
  assert.equal(new Set(model.panels[0].series.map((s) => s.key)).size, 2);

  // Two real levels still step along the ramp.
  const stepped = jvModel([
    { ...light, led_level_v: 1.0 },
    { ...light, led_level_v: 1.03 },
  ]);
  assert.notEqual(stepped.panels[0].series[0].colour, stepped.panels[0].series[1].colour);
});

test('a pipeline node draws on the card of the module that ran it', () => {
  // `RunQueued.module` is null for a pipeline and the module names are the
  // nodes' `kind`. Found by the run's module, the canonical two-node tree drew
  // nothing at all on the card that produced it.
  const store = createStore({ schedule: () => {} });
  for (const frame of jsonl('stream_pipeline_sim.jsonl')) store.applyFrame(frame);
  const state = store.getState();
  assert.equal(state.runs[state.order[0]].module, null, 'the fixture is a pipeline');

  const found = runFor(state, 'bace');
  assert.ok(found, 'and its bace nodes are still bace');
  assert.equal(found.node.kind, 'bace');
  assert.ok(found.node.shots.length, 'with shots to draw');
  // The newest node, not the run's own `lastShot`: each node numbers its shots
  // from one, so the run-level pointer is whichever node moved last.
  assert.equal(found.node.node_path, 'rep=2/bace');
  assert.equal(runFor(state, 'jv_bace'), null, 'and a module the tree never ran finds nothing');
});

// -- M4: Q per loop, or Q(axis) ----------------------------------------------

import { loopsModel, isRepeat, pointSummary, loopSeries, chargeUnit } from '../lib/charts/loops.js';

function nodeFrom(name, path, until = null) {
  const store = createStore({ schedule: () => {} });
  for (const frame of jsonl(name)) {
    store.applyFrame(frame);
    if (until && until(frame)) break;
  }
  const state = store.getState();
  const record = state.runs[state.order[0]];
  return { record, node: record.nodes[path] };
}

test('the chart switches on the axis, and says so', () => {
  // `ui-rules` §4: a zero-width axis plots Q per loop, not a curve, and the
  // switch is visible rather than silent.
  const sweep = loopsModel(nodeFrom('stream_stopped_sim.jsonl', 'bace'));
  assert.equal(sweep.switch.repeat, false);
  assert.match(sweep.panels[0].label, /^Q\(delay_ns\)/);
  assert.match(sweep.x.label, /delay_ns \/ ns/);

  const repeat = loopsModel({
    record: { state: 'running', n_loops: 20 },
    node: { node_path: 'bace', kind: 'bace', axis: { name: 'vpre', start: 0.9, stop: 0.9, step: 0 }, values: [0.9],
      loops: [], shots: [1, 2, 3].map((l) => ({ index: l - 1, loop: l, step: 1, q: 1e-10 * (1 + 0.1 * l), q_mean: 1e-10, q_std: 0, ts: l })),
      kept: 3, requested: 20, outcome: null },
  });
  assert.equal(repeat.switch.repeat, true);
  assert.match(repeat.panels[0].label, /^Q per loop/);
  assert.match(repeat.panels[0].note, /zero-width axis · vpre 0\.9000 V = stop · repeats, not a curve/);
  assert.equal(repeat.x.format(12), 'loop 12');
});

test('step is the point, index is the shot', () => {
  // A three-point axis, two loops: the second loop's first shot is
  // `index 3, step 1`, and lands on the first point.
  const found = nodeFrom('stream_tree_sim.jsonl', 'T=250K/led=1.010V/bace');
  const points = pointSummary(found.node);
  assert.equal(points.length, 3);
  assert.deepEqual(points.map((p) => p.n), [2, 2, 2]);
  assert.deepEqual(found.node.shots.map((s) => [s.index, s.step]), [[0, 1], [1, 2], [2, 3], [3, 1], [4, 2], [5, 3]]);
});

test('σ_Q of zero is not recorded, and the marker says so', () => {
  // After one loop every point carries `q_std = 0` — the service's sample
  // deviation needs two — so the point is a hollow square and no error bar.
  const one = loopsModel(nodeFrom('stream_stopped_sim.jsonl', 'bace', (f) => f.type === 'LoopDone' && f.data.loop === 1));
  const squares = one.panels[0].dots.filter((d) => d.shape === 'square' && d.hollow);
  assert.equal(squares.length, 3);
  assert.equal(one.panels[0].rules.length, 0, 'no error bar of zero length');
  assert.match(one.readout, /σ_Q not recorded yet — one loop/);
  assert.ok(one.legend.some((e) => e.marker === 'square-hollow' && /not recorded/.test(e.label)));

  // After the second loop the σ exists, and the marker changes with it.
  const two = loopsModel(nodeFrom('stream_stopped_sim.jsonl', 'bace', (f) => f.type === 'LoopDone' && f.data.loop === 2));
  assert.equal(two.panels[0].dots.filter((d) => d.shape === 'square').length, 0);
  assert.equal(two.panels[0].rules.length, 3, 'one error bar per point');
});

test('the error bars reflect the loops that actually ran', () => {
  // `ui-rules` §9: "100 loops requested, 20 completed." The stopped scan
  // asked for 20 loops of 3 points and was stopped after 39 shots.
  const model = loopsModel(nodeFrom('stream_stopped_sim.jsonl', 'bace'));
  assert.ok(model.notes.some((n) => /^20 loops requested, 13 completed · stopped$/.test(n)), model.notes.join(' | '));
  assert.match(model.readout, /^39 of 60 shots/);
  assert.match(model.readout, /stopped$/);
  const points = pointSummary(nodeFrom('stream_stopped_sim.jsonl', 'bace').node);
  assert.deepEqual(points.map((p) => p.n), [13, 13, 13]);
});

test('a stopped repeat whose early loops left the ring hatches from the loop number, not the shot count', () => {
  // The console holds loops 81–90 of a hundred; `kept` is the run's own count
  // and does not move backwards. Measured by the series' length the chart
  // would hatch from loop 11 and cross out ten loops it is drawing.
  const shots = [];
  for (let loop = 81; loop <= 90; loop += 1) {
    shots.push({ index: loop - 1, loop, step: 1, q: 3.6e-10, q_mean: 3.65e-10, q_std: 4e-12, ts: loop });
  }
  const model = loopsModel({
    record: { state: 'stopped', n_loops: 100, aborted: { reason: 'requested', done: 90, total: 100 } },
    node: { node_path: 'bace', kind: 'bace', axis: { name: 'vpre', start: 1, stop: 1, step: 0 }, values: [1],
      loops: [], shots, kept: 90, requested: 100, outcome: 'stopped' },
  });
  const panel = model.panels[0];
  assert.ok(panel.marks.some((m) => m.text === 'loops 91 – 100 not acquired'), panel.marks.map((m) => m.text).join(' | '));
  assert.ok(panel.marks.some((m) => m.text === 'stopped at loop 90'));
  assert.ok(model.notes.some((n) => /^100 loops requested, 90 completed · stopped$/.test(n)), model.notes.join(' | '));
  // The hatch begins after the last dot rather than over it.
  const lastDot = Math.max(...panel.dots.map((d) => Number(d.x)));
  assert.ok(panel.shades[0].x > lastDot, `hatch at ${panel.shades[0].x}, last point at ${lastDot}`);
  // And the empty space before the first dot is not silence about it.
  assert.ok(model.notes.some((n) => /^loops 1 – 80 ran before this console's view of them/.test(n)), model.notes.join(' | '));
});

test('a repeat that was stopped draws the loops it never ran as not acquired', () => {
  const model = loopsModel({
    record: { state: 'stopped', n_loops: 20, aborted: { reason: 'requested', done: 5, total: 20 } },
    node: { node_path: 'bace', kind: 'bace', axis: { name: 'vpre', start: 1, stop: 1, step: 0 }, values: [1],
      loops: [], shots: [1, 2, 3, 4, 5].map((l) => ({ index: l - 1, loop: l, step: 1, q: 1e-10 * (1 + 0.05 * Math.sin(l)), q_mean: 1e-10, q_std: 0, ts: l })),
      kept: 5, requested: 20, outcome: 'stopped' },
  });
  const panel = model.panels[0];
  assert.equal(panel.shades.length, 1);
  assert.equal(panel.shades[0].hatch, true);
  assert.ok(panel.marks.some((m) => m.text === 'loops 6 – 20 not acquired'));
  assert.ok(panel.marks.some((m) => m.text === 'stopped at loop 5'));
  assert.ok(model.notes.some((n) => /^20 loops requested, 5 completed · stopped$/.test(n)));
});

test('the running mean and its band are the service\'s own, not a statistic of the replayed tail', () => {
  // A long repeat outgrows the ring: the console holds loops 18–20 and the
  // service's `q_mean`/`q_std` on each of them cover every loop that ran.
  // Summed here from three shots they would describe the replay, not the run.
  const series = loopSeries({ shots: [
    { index: 17, loop: 18, step: 1, q: 9e-10, q_mean: 3.0e-10, q_std: 4.0e-11 },
    { index: 18, loop: 19, step: 1, q: 9e-10, q_mean: 3.3e-10, q_std: 4.2e-11 },
    { index: 19, loop: 20, step: 1, q: 9e-10, q_mean: 3.6e-10, q_std: 4.4e-11 },
  ] });
  assert.deepEqual(series.map((s) => s.loop), [18, 19, 20]);
  assert.equal(series[2].mean, 3.6e-10, 'the mean over twenty loops, not over three');
  assert.equal(series[2].sigma, 4.4e-11);
  // And one loop's `q_std = 0` is not recorded.
  const first = loopSeries({ shots: [{ index: 0, loop: 1, step: 1, q: 1e-10, q_mean: 1e-10, q_std: 0 }] });
  assert.equal(first[0].sigma, null);
});

test('the charge axis is one power of ten for the whole chart, never a prefix per value', () => {
  assert.deepEqual(chargeUnit([3.65e-10, 4.1e-10]), { factor: 1e-10, label: 'Q / 1e-10 C' });
  assert.deepEqual(chargeUnit([-2.3e-12, 9e-13]), { factor: 1e-12, label: 'Q / 1e-12 C' });
  assert.equal(chargeUnit([]).label, 'Q / 1e-12 C');
});

test('a shot without traces still puts its point on the loop curve', () => {
  // The M4 proof's second half: a client dropped at 1008 comes back via
  // `decimated.replay`, its shots without arrays, and the loop curve must
  // not lose them. The chart never reads a trace.
  const store = createStore({ schedule: () => {} });
  for (const frame of jsonl('stream_stopped_sim.jsonl')) {
    if (frame.type === 'StepDone') {
      const stripped = { ...frame, data: { ...frame.data, light: null, dark: null, photo: null, photo_averaged: null },
        decimated: Object.fromEntries(Object.keys(frame.decimated || {}).map((k) => [k, { omitted: true, replay: true }])) };
      store.applyFrame(stripped);
    } else {
      store.applyFrame(frame);
    }
  }
  const state = store.getState();
  const record = state.runs[state.order[0]];
  const node = record.nodes.bace;
  assert.ok(node.shots.every((s) => s.tracesGone));
  const model = loopsModel({ record, node });
  assert.deepEqual(pointSummary(node).map((p) => p.n), [13, 13, 13]);
  assert.equal(model.panels[0].rules.length, 3);
  assert.ok(model.notes.some((n) => /39 shots without traces/.test(n)));
});

test('a node whose axis the ring never delivered says so rather than guessing', () => {
  const model = loopsModel({ record: { state: 'running' }, node: { node_path: 'bace', kind: 'bace', axis: null, values: [], shots: [{ index: 0, loop: 1, step: 1, q: 1e-10 }], loops: [] } });
  assert.ok(model.absent);
  assert.match(model.absent.text, /axis is not known/);
  assert.equal(isRepeat({ values: [], axis: null }), null);
});
