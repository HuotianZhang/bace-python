// The timing diagram: one shot, one LED cycle, one record — drawn from the
// form, before anything runs.
//
// `docs/ui-rules.md` §7: *"Draw the shot, do not describe it… where a wrong
// V_coll sign or an absurd delay becomes visible before the run."* That is the
// whole point of this component, and it is why it belongs beside M2's card
// rather than on the rig tab: it redraws as the form is edited, and the thing
// it makes visible is the thing the operator is typing.
//
// Everything below is the code's own arithmetic, not a picture of it:
//
//   * the seven `StepPhase` segments, in the order `run_transient_scan` yields
//     them — six when `dark_reference = "same"` — with the shutter moves and
//     the power read placed where the code actually does them, since those are
//     not separately reported (§7);
//   * `dark_settle_s` sleeps inside **`dark levels`**, so it does not happen at
//     all under `dark_reference = "same"`;
//   * the LED runs at `:OUTP:POL INV` on this rig, which inverts the waveform
//     and *not* the Sync — so the Sync's rising edge means light **off**, which
//     is the edge the 81150A arms on, and the lit fraction is
//     `100 - duty_percent`;
//   * the 81150A at `NORM` rests the device at V_pre and pulses to V_coll.
//     `INV` rests it at V_coll and extraction never stops: measured, and it
//     collapsed the photo peak from ~3 mA to ~0.5 mA (`docs/README.md`, the
//     overturned table ④).
//
// The levels drawn are the levels **at the device** — what the operator typed.
// `core.pulses` divides them by the amplifier gain on the way to the
// generator, and that pair is shown as a caption rather than as the waveform,
// because the device is what the experiment is about.

import * as scale from '../scale.js';
import * as fmt from '../format.js';
import { layout, axisTicks } from './frame.js';

/**
 * The shot, segment by segment, in `run_transient_scan`'s own order.
 *
 * `phase` is the `StepPhase` name where there is one and `null` for the two
 * that are not reported. `k`/`of` match the frames, so M4's live indicator can
 * light a segment from the wire without a second table of names.
 */
export function shotSegments(values = {}) {
  const same = values.dark_reference === 'same';
  const averages = Number(values.n_averages) || 0;
  const frequency = Number(values.pulse_frequency_hz) || 0;
  const acquire = frequency > 0 ? averages / frequency : 0;
  const settle = Number(values.settle_s) || 0;
  const shutter = Number(values.shutter_settle_s) || 0;
  const darkSettle = Number(values.dark_settle_s) || 0;

  const list = [
    { key: 'levels', phase: 'levels', label: 'levels', seconds: 0,
      detail: 'set the light levels, open the shutter', mark: 'shutter opens' },
    { key: 'light-settle', phase: 'light settle', label: 'light settle', seconds: settle + shutter,
      detail: `settle_s ${seconds(settle)} + shutter_settle_s ${seconds(shutter)}`,
      mark: values.read_intensity ? 'power read' : null },
    { key: 'acquire-light', phase: 'acquire light', label: 'acquire light', seconds: acquire,
      detail: `${fmt.plural(averages, 'average')} at ${fmt.sig(frequency, 3)} Hz · autoranging`, accent: true },
    { key: 'dark-levels', phase: 'dark levels', label: 'dark levels', seconds: same ? 0 : darkSettle,
      detail: same
        ? 'skipped · dark_reference = same leaves the levels alone, and dark_settle_s never sleeps'
        : `set the dark levels, then dark_settle_s ${seconds(darkSettle)}`,
      skipped: same },
    { key: 'dark-settle', phase: 'dark settle', label: 'dark settle', seconds: shutter,
      detail: `close the shutter, then shutter_settle_s ${seconds(shutter)}`, mark: 'shutter shuts' },
    { key: 'acquire-dark', phase: 'acquire dark', label: 'acquire dark', seconds: acquire,
      detail: `${fmt.plural(averages, 'average')} · the range is inherited, not re-found`, accent: true },
    { key: 'process', phase: 'process', label: 'process', seconds: 0,
      detail: 'subtract, baseline-correct, integrate — one Q' },
  ];
  const live = list.filter((seg) => !seg.skipped);
  return list.map((seg) => ({
    ...seg,
    k: seg.skipped ? null : live.indexOf(seg) + 1,
    of: live.length,
  }));
}

/** One LED cycle: which half is lit, where the Sync is, and what the bias does. */
export function cyclePlan(values = {}, rig = {}, chain = {}) {
  const frequency = Number(values.pulse_frequency_hz) || 0;
  const period = frequency > 0 ? 1 / frequency : 0;
  const duty = Number(values.duty_percent);
  // `?` is what a driver answers for a query that failed, and an absent chain
  // is a bench nobody has read. Neither is INV. Defaulting them to the
  // expected polarity drew a confident diagram of the *other* half of the
  // cycle and stated that the Sync edge means light off — a claim about the
  // instrument from a read-back that never came back.
  const read = chainValue(chain, 'led_polarity');
  const ledPolarity = read === 'NORM' || read === 'INV' ? read : null;
  const inverted = ledPolarity === 'INV';
  // `set_polarity`: inverting flips which half the LED is lit for and leaves
  // the Sync alone, so under INV the lit fraction is the *complement* of the
  // duty written to the instrument, and raising `duty_percent` shortens the
  // illumination. With the polarity unread there is no answer to which half.
  const litFraction = ledPolarity === null ? null : (inverted ? 100 - duty : duty) / 100;
  const bias = biasLevels(values, chain);
  const delayS = (Number(values.delay_ns) || 0) * 1e-9 + (Number(rig.trigger_offset_s) || 0);
  return {
    period,
    duty: duty / 100,
    litFraction,
    ledPolarity,
    inverted,
    // The Sync rises at the start of the written waveform's high phase, and
    // the 81150A arms on that rising edge. Under INV that instant is the LED
    // going *off* — which is what BACE extracts after.
    syncAt: 0,
    syncMeans: ledPolarity === null ? 'unknown — the 33220A has not been read' : inverted ? 'light off' : 'light on',
    lightDelayS: (Number(rig.light_path_delay_ns) || 0) * 1e-9,
    ledHigh: numberOr(values.led_v, null),
    ledLow: numberOr(values.led_low_v, null),
    ledThreshold: numberOr(rig.led_threshold_v, null),
    delayS,
    widthS: (Number(values.pulse_width_ns) || 0) * 1e-9,
    ...bias,
  };
}

/** What the device rests at, and what it is pulsed to — through the amplifier. */
export function biasLevels(values = {}, chain = {}) {
  const vpre = numberOr(values.vpre, null);
  const vcoll = numberOr(values.vcoll, null);
  const invert = values.invert_polarity === true;
  // `core.pulses.pulse_levels`: `invert` swaps *and negates* both levels, for
  // a device of the opposite architecture. It is a sign convention, not a
  // switch — getting it wrong flips the charge rather than disabling anything.
  const high = invert ? (vcoll === null ? null : -vcoll) : vpre;
  const low = invert ? (vpre === null ? null : -vpre) : vcoll;
  const declared = String(values.output_polarity || 'auto').toLowerCase();
  const readBack = chainValue(chain, 'bias_polarity');
  const polarity = declared === 'leave'
    ? (readBack || null)
    : declared === 'auto' ? (values.inverted_output ? 'INV' : 'NORM') : declared.toUpperCase();
  return {
    vpre, vcoll, invert, polarity,
    polaritySource: declared === 'leave' ? 'left as found — this is the read-back' : `output_polarity = ${values.output_polarity}`,
    // Measured, and the reason the recipe is NORM: through the inverting x4
    // amplifier INV rests the device at V_coll, so extraction never stops.
    restsAt: polarity === 'INV' ? low : high,
    pulsesTo: polarity === 'INV' ? high : low,
    restsLabel: polarity === 'INV' ? (invert ? '−V_pre' : 'V_coll') : (invert ? '−V_coll' : 'V_pre'),
    pulsesLabel: polarity === 'INV' ? (invert ? '−V_coll' : 'V_pre') : (invert ? '−V_pre' : 'V_coll'),
  };
}

/** The scope's record: how long, how finely, and where the window falls in it. */
export function recordPlan(values = {}, rig = {}) {
  const perDiv = (Number(values.timebase_ns_per_div) || 0) * 1e-9;
  const span = perDiv * 10;                       // the Infiniium's ten divisions
  const points = Number(values.record_length) || 0;
  const dt = points > 0 ? span / points : 0;
  const delayS = (Number(values.delay_ns) || 0) * 1e-9 + (Number(rig.trigger_offset_s) || 0);
  const widthS = (Number(values.pulse_width_ns) || 0) * 1e-9;
  const reference = values.t0_int_reference || 'record';
  const t0 = numberOr(values.t0_int_s, null);
  // **The record does not begin at the trigger.** `Infiniium.configure_timebase`
  // writes `:TIM:RANG` of ten divisions and `:TIM:POS` of *four*, which puts
  // the horizontal reference four divisions after the trigger — so the record
  // runs from one division before it to nine after. Drawn from zero, every
  // edge and the whole shaded window sat one division early, and a window
  // between nine and ten divisions after the trigger was shown as inside a
  // record that had already ended.
  //
  // Both recordings agree: at 200 ns/div the sim trace carries `t0 = −200 ns`
  // and the rig day's `−199.5 ns`, the instrument's own sample grid apart.
  const start = -perDiv;
  const end = perDiv * 9;
  const fromTrigger = t0 === null ? null
    : reference === 'pulse' ? t0 + delayS
      : reference === 'trigger' ? t0
        // `record` is measured from the first sample, so it is that sample's
        // own offset away — nominally one division, and exactly whatever
        // `Trace.t0` (`:WAV:XOR?`) turns out to be once a shot has run.
        : t0 + start;
  return {
    span, points, dt, delayS, widthS, reference, t0, fromTrigger, start, end,
    nominal: reference === 'record',
  };
}

/**
 * What is wrong with this shot, before it is run.
 *
 * Every entry is a fact about the values in the form or about the bench's own
 * read-back — never a guess. `level` follows `ui-rules` §3: `alert` is the
 * loudest and is reserved for a run that would measure the wrong thing.
 */
export function timingAlerts(values = {}, rig = {}, chain = {}) {
  const out = [];
  const cycle = cyclePlan(values, rig, chain);
  const record = recordPlan(values, rig);

  if (cycle.polarity === 'INV') {
    out.push({ level: 'alert', key: 'bias-polarity',
      text: 'the 81150A at INV rests the device at V_coll through the inverting ×4 amplifier, '
        + 'so extraction never stops — measured: the photo peak collapsed from ~3 mA to ~0.5 mA '
        + 'and the displacement spike changed sign' });
  }
  if (cycle.polarity === null) {
    out.push({ level: 'warn', key: 'bias-polarity-unknown',
      text: 'output_polarity = leave writes nothing, and the bench has not read one back: '
        + 'which level the device rests at between pulses is unknown until the run reports it' });
  }
  if (cycle.ledPolarity === null) {
    out.push({ level: 'warn', key: 'led-polarity-unknown',
      text: 'the 33220A\'s polarity has not been read back, so which half of the cycle the device '
        + 'is lit for is unknown — and with it whether the Sync edge the 81150A arms on is light '
        + 'off (INV, what BACE needs) or light on' });
  }
  if (cycle.ledPolarity === 'NORM') {
    out.push({ level: 'alert', key: 'led-polarity',
      text: 'the 33220A reads NORM, so the Sync\'s rising edge means light ON and the extraction '
        + 'happens in the middle of carrier generation — still producing a plausible charge' });
  }
  if (Number.isFinite(cycle.ledThreshold) && Number.isFinite(cycle.ledLow)
      && cycle.ledLow >= cycle.ledThreshold) {
    out.push({ level: 'warn', key: 'led-low',
      text: `led_low_v ${fmt.volts(cycle.ledLow, { decimals: 3 })} is at or above the LED threshold `
        + `${fmt.volts(cycle.ledThreshold, { decimals: 3 })}: the dark half of the cycle is not dark` });
  }
  if (Number.isFinite(cycle.duty) && Math.abs(cycle.duty - 0.5) > 1e-9 && cycle.inverted
      && Number.isFinite(cycle.litFraction)) {
    out.push({ level: 'info', key: 'duty',
      text: `duty_percent ${fmt.sig(values.duty_percent, 3)} % under INV lights the device for `
        + `${fmt.sig(cycle.litFraction * 100, 3)} % of the period — raising it shortens the illumination` });
  }
  if (cycle.delayS < 0) {
    out.push({ level: 'invalid', key: 'delay',
      text: `delay_ns ${fmt.sig(values.delay_ns, 4)} with a trigger offset of `
        + `${fmt.sig((rig.trigger_offset_s || 0) * 1e9, 3)} ns asks the generator to fire before its own trigger` });
  }
  if (record.span > 0 && cycle.widthS > 0 && cycle.delayS + cycle.widthS < record.end) {
    out.push({ level: 'warn', key: 'width',
      text: `the collection pulse ends ${fmt.sig((cycle.delayS + cycle.widthS) * 1e9, 4)} ns after the `
        + `trigger and the record runs to ${fmt.sig(record.end * 1e9, 4)} ns: the device is back at `
        + 'its resting level while the integration is still running' });
  }
  if (record.fromTrigger !== null && record.span > 0
      && (record.fromTrigger >= record.end || record.fromTrigger < record.start)) {
    out.push({ level: 'invalid', key: 'window',
      text: `t0_int is ${fmt.sig(record.fromTrigger * 1e9, 4)} ns from the trigger, and the record `
        + `runs ${fmt.sig(record.start * 1e9, 4)} … ${fmt.sig(record.end * 1e9, 4)} ns `
        + '(`:TIM:POS` is four of its ten divisions): the integration window is empty' });
  }
  if (record.reference === 'record') {
    out.push({ level: 'info', key: 'window-reference',
      text: 't0_int_reference = record measures from the first sample, which sits one division '
        + 'before the trigger — so this window slides against the signal whenever '
        + 'timebase_ns_per_div changes. Drawn here at its nominal place; the trace reports its own' });
  }
  if (values.dark_reference === 'same') {
    out.push({ level: 'info', key: 'dark-reference',
      text: 'dark_reference = same: the levels are not rewritten, the shutter is the only thing that '
        + 'moves, and dark_settle_s does not sleep at all' });
  }
  return out;
}

const AXIS_OF = { vpre: 'vpre', delay_ns: 'delay_ns', vcoll: 'vcoll' };

export function timingModel(values = {}, options = {}) {
  const { rig = {}, chain = {}, width = 640, height = 470 } = options;
  const swept = AXIS_OF[values.axis_name] || null;
  const cycle = cyclePlan(values, rig, chain);
  const record = recordPlan(values, rig);
  const segments = shotSegments(values);
  const alerts = timingAlerts(values, rig, chain);

  const frame = layout({
    width,
    height,
    panels: [{ key: 'shot', weight: 0.7 }, { key: 'cycle', weight: 1.15 }, { key: 'record', weight: 1 }],
    margin: { left: 92, right: 16, top: 20, bottom: 34, gap: 46 },
  });
  const [shotPanel, cyclePanel, recordPanel] = frame.panels;

  return {
    key: 'timing',
    cursor: false,
    width: frame.width,
    height: frame.height,
    margin: frame.margin,
    swept,
    alerts,
    segments,
    panels: [
      shotStrip(shotPanel, segments),
      cycleRows(cyclePanel, cycle, swept),
      recordRows(recordPanel, cycle, record, swept),
    ],
    legend: [
      { label: 'the swept quantity', colour: 'accent', width: 2.4 },
      { label: 'everything else', colour: 'ink', width: 1.4 },
      { label: 'light at the sample', colour: 'ink', width: 1.4, dash: '5 3' },
    ],
    readout: '',
    // `ui-rules` §4: draw the shot, do not describe it. The segment-by-segment
    // sentence was the diagram above restated in prose — the strip is already
    // to scale and already labelled, and the durations are on it. What is left
    // here is only what the picture cannot say.
    notes: [
      `levels are at the device · the generator sees them divided by the ×${fmt.sig(rig.pulse_amp || 1, 2)} amplifier`,
      cycle.polaritySource,
      `33220A POL ${cycle.ledPolarity || '?'} · the Sync's rising edge means ${cycle.syncMeans}`,
      ...(cycle.invert
        ? ['invert_polarity swaps and negates both levels before they are written — '
           + 'a sign convention for a device of the opposite architecture, not a switch: '
           + 'wrong, it flips the charge rather than disabling anything']
        : []),
    ],
  };
}

/**
 * Panel A: the shot as its seven reported segments, to scale.
 *
 * To scale is the point and the problem: a shot with `shutter_settle_s = 5`
 * spends ten of its eleven seconds waiting for a shutter, and the two
 * acquisitions that are the measurement are slivers. Drawn any other way the
 * strip would be a diagram of the phase *names* rather than of the shot, so
 * the bar stays honest and the names that do not fit move to the list beneath
 * it — where they carry their durations anyway.
 */
function shotStrip(panel, segments) {
  const rect = panel.rect;
  const total = segments.reduce((sum, seg) => sum + seg.seconds, 0);
  const X = scale.linear([0, total || 1], [rect.x, rect.x + rect.w]);
  const marks = [];
  const rules = [];
  const bars = [];
  const top = rect.y + rect.h * 0.42;
  const barHeight = rect.h * 0.34;
  let at = 0;
  let markSide = 0;
  for (const seg of segments) {
    const x0 = X(at);
    const x1 = X(at + seg.seconds);
    if (!seg.skipped && x1 - x0 > 0.5) {
      bars.push({
        d: `M${scale.fixed(x0)} ${scale.fixed(top)}H${scale.fixed(x1)}V${scale.fixed(top + barHeight)}H${scale.fixed(x0)}Z`,
        colour: seg.accent ? 'ink' : 'fill', opacity: seg.accent ? 0.85 : 1,
      });
    }
    if (!seg.skipped) {
      rules.push({ x1: x0, y1: top, x2: x0, y2: top + barHeight, colour: 'rule', width: 1 });
    }
    if (x1 - x0 > 46) {
      marks.push({ x: (x0 + x1) / 2, y: top + barHeight / 2 + 3, text: seg.label, anchor: 'middle',
        colour: seg.accent ? 'paper' : 'ink', size: 8.5, weight: 600 });
      marks.push({ x: (x0 + x1) / 2, y: top + barHeight + 11, text: seconds(seg.seconds),
        anchor: 'middle', colour: 'grey', size: 8.5, mono: true });
    }
    if (seg.mark) {
      // The three events the service does not report as phases, staggered so
      // two of them landing in the same second do not print over each other.
      const y = top - 6 - (markSide % 2) * 10;
      markSide += 1;
      marks.push({ x: x0 + 3, y, text: seg.mark, colour: 'grey', size: 8.5 });
      rules.push({ x1: x0, y1: y + 2, x2: x0, y2: top, colour: 'rule', width: 1, dash: '2 2' });
    }
    at += seg.seconds;
  }
  const skipped = segments.filter((s) => s.skipped);
  return {
    ...panel,
    label: `A · one shot = one Q · ${seconds(total)}`,
    note: `${segments.filter((s) => !s.skipped).length} StepPhase segments`
      + (skipped.length ? ` · ${skipped.map((s) => s.label).join(', ')} skipped` : ''),
    y: { ticks: [] },
    bands: bars,
    rules,
    marks,
    series: [],
    x: { ticks: [], label: 'to scale · every segment is in the list below' },
  };
}

/** Panel B: one LED cycle — drive, light at the sample, Sync, bias. */
function cycleRows(panel, cycle, swept) {
  const rect = panel.rect;
  const span = cycle.period || 1;
  const X = scale.linear([0, span], [rect.x, rect.x + rect.w]);
  const names = ['LED drive', 'light at sample', 'SYNC · CH3', 'bias'];
  const rows = rowsOf(rect, names);
  const series = [];
  const marks = [];

  // The written waveform: high for `duty`, low for the rest.
  const dutyAt = span * cycle.duty;
  series.push({ key: 'drive', label: 'LED drive', colour: 'ink', width: 1.4,
    d: square(X, rows[0], [[0, true], [dutyAt, false], [span, false]]) });
  annotate(marks, rows[0], rect,
    `high ${fmt.volts(cycle.ledHigh, { decimals: 3 })} · low ${fmt.volts(cycle.ledLow, { decimals: 3 })} · `
    + `duty ${fmt.sig(cycle.duty * 100, 3)} % at the instrument`);

  // The light: inverted under INV, and late by the fibre.
  // Each entry is *at this time the light becomes this*. Under NORM the LED
  // follows the written waveform, so it is lit through the duty phase; under
  // INV it is lit through the complement. Both are shifted by the fibre.
  //
  // With the polarity unread there is no waveform to draw: either half would
  // be a claim, and a diagram is a worse place than most to make one.
  if (cycle.ledPolarity === null) {
    marks.push({ x: rect.x + 4, y: (rows[1].high + rows[1].low) / 2 + 3, size: 8.5, colour: 'warn',
      text: 'which half is lit is unknown until the 33220A\'s polarity is read back' });
    annotate(marks, rows[1], rect,
      `the same square ${fmt.sig(cycle.lightDelayS * 1e9, 3)} ns later — the LED amp and the fibre`,
      { colour: 'warn' });
  } else {
    const lit = cycle.inverted
      ? [[cycle.lightDelayS, false], [dutyAt + cycle.lightDelayS, true]]
      : [[cycle.lightDelayS, true], [dutyAt + cycle.lightDelayS, false]];
    series.push({ key: 'light', label: 'light at sample', colour: 'ink', width: 1.4, dash: '5 3',
      d: squareWrapped(X, rows[1], span, lit) });
    annotate(marks, rows[1], rect,
      `the same square ${fmt.sig(cycle.lightDelayS * 1e9, 3)} ns later · lit for `
      + `${fmt.sig(cycle.litFraction * 100, 3)} % of the period under POL ${cycle.ledPolarity}`);
  }

  // The Sync is not inverted with the waveform: it rises with the written
  // high phase, and that is the edge the 81150A arms on.
  series.push({ key: 'sync', label: 'SYNC', colour: 'accent', width: 1.6,
    d: square(X, rows[2], [[0, true], [span * 0.02, false], [span, false]]) });
  annotate(marks, rows[2], rect,
    `Sync ↑ arms the 81150A · this edge is ${cycle.syncMeans}`,
    { colour: 'accent-dark', weight: 500 });

  const biasRow = rows[3];
  const x0 = X(Math.max(0, cycle.delayS));
  const x1 = X(Math.min(span, cycle.delayS + cycle.widthS));
  series.push({
    key: 'bias', label: 'bias', colour: swept === 'vpre' || swept === 'vcoll' ? 'accent' : 'ink',
    width: swept ? 2 : 1.5,
    d: `M${scale.fixed(rect.x)} ${scale.fixed(biasRow.high)}H${scale.fixed(x0)}`
      + `V${scale.fixed(biasRow.low)}H${scale.fixed(Math.max(x1, x0 + 1))}`
      + `V${scale.fixed(biasRow.high)}H${scale.fixed(rect.x + rect.w)}`,
  });
  annotate(marks, biasRow, rect,
    `rests at ${cycle.restsLabel} ${fmt.volts(cycle.restsAt, { decimals: 3 })}`,
    { colour: cycle.polarity === 'INV' ? 'alert' : 'ink', weight: 600, mono: true });
  annotate(marks, biasRow, rect,
    `${cycle.pulsesLabel} ${fmt.volts(cycle.pulsesTo, { decimals: 3 })} for `
    + `${fmt.sig(cycle.widthS * 1e9, 4)} ns, ${fmt.sig(cycle.delayS * 1e9, 4)} ns after the Sync — panel C`,
    { anchor: 'end', mono: true, colour: swept === 'delay_ns' || swept === 'vcoll' ? 'accent-dark' : 'grey' });

  return {
    ...panel,
    label: `B · one LED cycle · ${seconds(cycle.period)} at ${fmt.sig(1 / (cycle.period || 1), 3)} Hz`,
    note: `81150A POL ${cycle.polarity || '?'}`,
    noteColour: cycle.polarity === 'INV' ? 'alert' : 'grey',
    y: { ticks: [] },
    series,
    marks: [...marks, ...rowLabels(rect, rows, names)],
    x: {
      ticks: axisTicks(scale.linear([0, span * 1e3], [rect.x, rect.x + rect.w]), { count: 4 }),
      label: 't / ms → one period',
    },
  };
}

/** Panel C: the record the scope keeps, and the window inside it. */
function recordRows(panel, cycle, record, swept) {
  const rect = panel.rect;
  const start = record.start || 0;
  const end = record.end || (record.span || 1);
  const clamp = (t) => Math.max(start, Math.min(end, t));
  const X = scale.linear([start, end], [rect.x, rect.x + rect.w]);
  const rows = rowsOf(rect, ['bias']);
  const row = rows[0];
  const x0 = X(clamp(cycle.delayS));
  const x1 = X(clamp(cycle.delayS + cycle.widthS));
  const returns = cycle.delayS + cycle.widthS < end;
  const series = [{
    key: 'bias', label: 'bias', colour: swept === 'vcoll' || swept === 'vpre' ? 'accent' : 'ink',
    width: swept ? 2 : 1.5,
    d: `M${scale.fixed(rect.x)} ${scale.fixed(row.high)}H${scale.fixed(x0)}V${scale.fixed(row.low)}`
      + `H${scale.fixed(Math.max(x1, x0 + 1))}`
      + (returns ? `V${scale.fixed(row.high)}H${scale.fixed(rect.x + rect.w)}` : ''),
  }];
  const shades = [];
  const rules = [];
  const marks = [];
  annotate(marks, row, rect,
    `${cycle.restsLabel} ${fmt.volts(cycle.restsAt, { decimals: 3 })} → `
    + `${cycle.pulsesLabel} ${fmt.volts(cycle.pulsesTo, { decimals: 3 })}`
    + (returns ? '' : ' · the return is after the record ends'),
    { mono: true });
  // The trigger is inside the record, one division in, and the record is the
  // only thing on this panel whose zero is not it.
  rules.push({ x1: X(0), colour: 'grey', width: 1, dash: '3 3' });
  // A line below the panel's own caption, which starts at the left edge and
  // reaches past the trigger's tenth of the width.
  marks.push({ x: X(0) + 4, y: rect.y + 24, colour: 'grey', size: 8.5, halo: true, text: 'trigger' });
  if (record.fromTrigger !== null && record.fromTrigger >= start && record.fromTrigger < end) {
    const wx = X(record.fromTrigger);
    shades.push({ x: wx, w: rect.x + rect.w - wx, colour: 'accent', opacity: 0.07 });
    rules.push({ x1: wx, colour: 'accent', width: 1.5 });
    marks.push({ x: wx + 5, y: rect.y + rect.h - 8, mono: true, weight: 600, colour: 'accent', size: 9,
      text: `t0_int ${fmt.sig(record.fromTrigger * 1e9, 4)} ns from the trigger`
        + (record.nominal ? ' · nominal' : '') });
    marks.push({ x: rect.x + rect.w - 5, y: rect.y + rect.h - 8, anchor: 'end', colour: 'accent', size: 8.5,
      text: 'Q = ∫ (light − dark) dt over the shaded part' });
  } else {
    marks.push({ x: rect.x + 5, y: rect.y + rect.h - 8, colour: 'alert', size: 8.5,
      text: record.t0 === null
        ? 'no t0_int on this form'
        : `t0_int ${fmt.sig((record.fromTrigger ?? record.t0) * 1e9, 4)} ns from the trigger is `
          + 'outside this record — the integration window is empty' });
  }
  return {
    ...panel,
    label: `C · the record · ${fmt.sig((end - start) * 1e9, 4)} ns · ${record.points} points · dt ${fmt.sig(record.dt * 1e9, 3)} ns`,
    // `:TIM:POS` is four divisions, so the ten-division window sits one
    // division before the trigger and nine after it.
    note: `timebase ${fmt.sig(((end - start) / 10) * 1e9, 3)} ns/div · one division before the trigger, nine after`,
    y: { ticks: [] },
    series, shades, rules,
    marks: [...marks, ...rowLabels(rect, rows, ['bias'])],
    x: {
      ticks: axisTicks(scale.linear([start * 1e9, end * 1e9], [rect.x, rect.x + rect.w]), { count: 5 }),
      label: 'ns from the trigger',
    },
  };
}

// -- schematic helpers ----------------------------------------------------

/**
 * One band per signal: a caption line at the top, the waveform under it.
 *
 * The captions are the reason the rows are laid out at all. Placed by hand at
 * whatever y looked free, they printed over the waveform above — measured on
 * screen, which is the only way that kind of fault is ever found.
 */
function rowsOf(rect, names) {
  const each = rect.h / names.length;
  return names.map((_, i) => {
    const top = rect.y + each * i;
    return { top, caption: top + 10, high: top + each * 0.45, low: top + each * 0.85, bottom: top + each };
  });
}

function annotate(marks, row, rect, text, options = {}) {
  marks.push({
    x: options.anchor === 'end' ? rect.x + rect.w - 4 : rect.x + 4,
    y: row.caption,
    text,
    anchor: options.anchor || 'start',
    colour: options.colour || 'grey',
    weight: options.weight || 400,
    mono: options.mono || false,
    size: options.size || 8.5,
  });
}

function rowLabels(rect, rows, names) {
  return names.map((name, i) => ({
    x: rect.x - 8, y: (rows[i].high + rows[i].low) / 2 + 3, text: name,
    anchor: 'end', colour: 'ink', weight: 600, size: 9,
  }));
}

/** A square wave from `[time, high]` transitions, left to right. */
function square(X, row, points) {
  const y = (high) => (high ? row.high : row.low);
  let d = '';
  for (const [t, high] of points) {
    const x = scale.fixed(X(t));
    if (d === '') d = `M${x} ${scale.fixed(y(high))}`;
    else d += `H${x}V${scale.fixed(y(high))}`;
  }
  return d;
}

/**
 * The same, for a waveform whose edges are **late**: the light at the sample
 * follows the drive by the fibre's own delay, so one of its edges falls off
 * the end of the period and belongs at the start of it.
 *
 * Wrapping each point with `t % span` and drawing them in the order given
 * emits a horizontal segment that travels *backwards* across the panel — a
 * second line at the wrong level, which is what the first render showed. The
 * fix is to wrap first, sort, and start from the state that is in force at
 * zero, which is the state the last transition of the period leaves behind.
 */
function squareWrapped(X, row, span, points) {
  const y = (high) => scale.fixed(high ? row.high : row.low);
  const wrapped = points
    .map(([t, high]) => ({ t: ((t % span) + span) % span, high }))
    .sort((a, b) => a.t - b.t);
  if (!wrapped.length) return '';
  let state = wrapped[wrapped.length - 1].high;
  let d = `M${scale.fixed(X(0))} ${y(state)}`;
  for (const edge of wrapped) {
    if (edge.t === 0) { state = edge.high; d = `M${scale.fixed(X(0))} ${y(state)}`; continue; }
    d += `H${scale.fixed(X(edge.t))}V${y(edge.high)}`;
    state = edge.high;
  }
  return `${d}H${scale.fixed(X(span))}`;
}

function chainValue(chain, key) {
  const items = (chain && chain.items) || [];
  const found = items.find((item) => item.key === key);
  return found ? found.value : null;
}

/**
 * A short time, prefixed where the prefix is natural: `2.0 ms`, `500 ns`.
 * `fmt.duration` is for the three *loop* scales — minutes and hours — and
 * reads an LED period as `0.0020 s`, which is a number nobody says out loud.
 */
export function seconds(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return fmt.ABSENT;
  if (value === 0) return '0 s';
  if (value >= 1) return fmt.duration(value);
  const [scaled, prefix] = fmt.prefixed(value);
  return `${fmt.sig(scaled, 3)} ${prefix}s`;
}

function numberOr(value, fallback) {
  return value === null || value === undefined || Number.isNaN(Number(value)) ? fallback : Number(value);
}
