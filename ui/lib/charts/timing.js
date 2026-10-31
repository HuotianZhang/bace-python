// The timing diagram renders one shot, one LED cycle and one record from the form so
// wrong signs and delays are visible before a run (`docs/ui-rules.md` §7). It follows
// the reported phases and places unreported shutter moves and the power read between
// them. `dark_settle_s` belongs to dark levels, which `dark_reference = "same"`
// skips. LED INV reverses the waveform but leaves Sync unchanged: the rising Sync
// edge marks light off and the lit fraction is `100 - duty_percent`. The 81150A at
// NORM rests at V_pre and pulses to V_coll. INV rests at V_coll, leaving extraction
// active; the measured photo peak falls from about 3 mA to 0.5 mA (`docs/notes.md`,
// Closed questions). The waveform uses device levels; generator levels account for
// amplifier gain in the caption.

import * as scale from '../scale.js';
import * as fmt from '../format.js';
import { layout, axisTicks, heightFor, ASPECT } from './frame.js';

/**
 * The helper describes shot segments in acquisition order. `phase` is the reported
 * `StepPhase` name or `null` for an unreported segment. Matching `k` and `of` lets
 * the live indicator use stream frames without a second phase table.
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
  // A failed polarity query returns `?`; an absent chain has no read-back. Neither
  // establishes INV. Assuming the expected polarity would draw an unsupported light
  // phase and Sync relationship.
  const read = chainValue(chain, 'led_polarity');
  const ledPolarity = read === 'NORM' || read === 'INV' ? read : null;
  const inverted = ledPolarity === 'INV';
  // LED inversion reverses the lit phase but not Sync. Under INV, increasing
  // `duty_percent` shortens illumination because the lit fraction is its complement.
  // Without polarity read-back the lit phase is unknown.
  const litFraction = ledPolarity === null ? null : (inverted ? 100 - duty : duty) / 100;
  const bias = biasLevels(values, chain);
  const delayS = (Number(values.delay_ns) || 0) * 1e-9 + (Number(rig.trigger_offset_s) || 0);
  return {
    period,
    duty: duty / 100,
    litFraction,
    ledPolarity,
    inverted,
    // Sync rises at the written waveform's high phase and arms the 81150A. Under INV
    // this edge switches the LED off before extraction.
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

/** The helper resolves device resting and pulse levels through the amplifier. */
export function biasLevels(values = {}, chain = {}) {
  const vpre = numberOr(values.vpre, null);
  const vcoll = numberOr(values.vcoll, null);
  const invert = values.invert_polarity === true;
  // `core.pulses.pulse_levels` swaps and negates both levels under `invert`. This
  // sign convention accommodates the opposite device architecture; an incorrect
  // setting reverses Q rather than disabling the output.
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
    // Through the inverting ×4 amplifier, INV rests the device at V_coll and leaves
    // extraction active. NORM supplies the measured operating polarity
    // (`docs/notes.md`, Closed questions).
    restsAt: polarity === 'INV' ? low : high,
    pulsesTo: polarity === 'INV' ? high : low,
    restsLabel: polarity === 'INV' ? (invert ? '−Vpre' : 'Vcoll') : (invert ? '−Vcoll' : 'Vpre'),
    pulsesLabel: polarity === 'INV' ? (invert ? '−Vcoll' : 'Vpre') : (invert ? '−Vpre' : 'Vcoll'),
  };
}

/** The model describes record duration, sampling and integration-window position. */
export function recordPlan(values = {}, rig = {}) {
  const perDiv = (Number(values.timebase_ns_per_div) || 0) * 1e-9;
  const span = perDiv * 10;                       // the Infiniium's ten divisions
  const points = Number(values.record_length) || 0;
  const dt = points > 0 ? span / points : 0;
  // Field arrival includes `trigger_offset_s` after the programmed delay. This bench
  // latency is not sent to the generator; the integration window is relative to field
  // arrival (manual 2.2).
  const delayS = (Number(values.delay_ns) || 0) * 1e-9 + (Number(rig.trigger_offset_s) || 0);
  const widthS = (Number(values.pulse_width_ns) || 0) * 1e-9;
  const t0 = numberOr(values.t0_int_s, null);
  const intWidth = numberOr(values.t_int_width_s, null);
  // `Infiniium.configure_timebase` sets ten divisions with the horizontal reference
  // four divisions after the trigger. The record therefore spans one division before
  // and nine after it. Starting the drawing at the trigger would misplace every edge
  // and show a window beyond the record as valid. At 200 ns/div, recorded origins on
  // this bench are −200 ns in simulation and −199.5 ns on the instrument, differing
  // by the sample grid.
  const start = -perDiv;
  const end = perDiv * 9;
  const fromTrigger = t0 === null ? null : t0 + delayS;
  const toTrigger = fromTrigger === null || intWidth === null ? null : fromTrigger + intWidth;
  return {
    span, points, dt, delayS, widthS, t0, intWidth, fromTrigger, toTrigger, start, end,
  };
}

/**
 * The check model reports constraints visible in form values or bench read-back.
 * Severity follows `docs/ui-rules.md` §3; `alert` is reserved for settings that
 * measure the wrong quantity.
 */
export function timingAlerts(values = {}, rig = {}, chain = {}) {
  const out = [];
  const cycle = cyclePlan(values, rig, chain);
  const record = recordPlan(values, rig);

  if (cycle.polarity === 'INV') {
    out.push({ level: 'alert', key: 'bias-polarity',
      text: 'the 81150A at INV rests the device at Vcoll through the inverting ×4 amplifier, '
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
  if ((Number(values.delay_ns) || 0) < 0) {
    out.push({ level: 'invalid', key: 'delay',
      text: `delay_ns ${fmt.sig(values.delay_ns, 4)} asks the generator to fire before its own trigger: `
        + '`:PULS:DEL1` is given delay_ns as it is, and cannot go below 0' });
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
  if (record.toTrigger !== null && record.span > 0 && record.fromTrigger < record.end
      && record.toTrigger > record.end) {
    out.push({ level: 'warn', key: 'window-end',
      text: `the integration window ends ${fmt.sig(record.toTrigger * 1e9, 4)} ns after the trigger `
        + `and the record ends at ${fmt.sig(record.end * 1e9, 4)} ns: the window is cut short — and `
        + 'along a delay axis by a different amount at each point. A longer timebase or a shorter '
        + 't_int_width_s makes it whole' });
  }
  if (record.fromTrigger !== null && !(Number(values.t_int_width_s) > 0)) {
    out.push({ level: 'invalid', key: 'window-width',
      text: 't_int_width_s must be positive: an empty integration window still produces a number' });
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
  const { rig = {}, chain = {}, width = 640, height = null } = options;
  const swept = AXIS_OF[values.axis_name] || null;
  const cycle = cyclePlan(values, rig, chain);
  const record = recordPlan(values, rig);
  const segments = shotSegments(values);
  const alerts = timingAlerts(values, rig, chain);

  // Three lanes of a quantity against time, so `trace` like the transient's.
  const lanes = [{ key: 'shot', weight: 0.7 }, { key: 'cycle', weight: 1.15 }, { key: 'record', weight: 1 }];
  const laneMargin = { left: 92, right: 16, top: 20, bottom: 34, gap: 46 };
  const frame = layout({
    width,
    height: height ?? heightFor(width, lanes, { margin: laneMargin, aspect: ASPECT.trace }),
    panels: lanes,
    margin: laneMargin,
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
    // The labelled, scaled diagram already conveys phases and durations. Its notes
    // retain only constraints the picture cannot show.
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
 * Panel A draws the reported shot segments to scale. With `shutter_settle_s = 5`,
 * shutter waits occupy ten of eleven seconds and acquisitions become narrow slivers.
 * Labels that do not fit move to the duration list rather than distorting the timing.
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
        colour: seg.accent ? 'paper' : 'ink', size: 11, weight: 600 });
      marks.push({ x: (x0 + x1) / 2, y: top + barHeight + 11, text: seconds(seg.seconds),
        anchor: 'middle', colour: 'grey', size: 11 });
    }
    if (seg.mark) {
      // Unreported events use staggered labels so coincident times do not overlap.
      const y = top - 6 - (markSide % 2) * 10;
      markSide += 1;
      marks.push({ x: x0 + 3, y, text: seg.mark, colour: 'grey', size: 11 });
      rules.push({ x1: x0, y1: y + 2, x2: x0, y2: top, colour: 'rule', width: 1, dash: '2 2' });
    }
    at += seg.seconds;
  }
  const skipped = segments.filter((s) => s.skipped);
  return {
    ...panel,
    label: `A · one shot = one Q · ${seconds(total)}`,
    note: `${segments.filter((s) => !s.skipped).length} segments`
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

  // Light at the sample follows the drive after the fibre delay. NORM lights the duty
  // phase; INV lights its complement. Unknown polarity supplies no waveform because
  // neither phase is supported by read-back.
  if (cycle.ledPolarity === null) {
    marks.push({ x: rect.x + 4, y: (rows[1].high + rows[1].low) / 2 + 3, size: 11, colour: 'warn',
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

  // Sync rises with the written high phase and arms the 81150A. Inverting the
  // waveform does not invert Sync.
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
    { colour: cycle.polarity === 'INV' ? 'alert' : 'ink', weight: 600 });
  annotate(marks, biasRow, rect,
    `${cycle.pulsesLabel} ${fmt.volts(cycle.pulsesTo, { decimals: 3 })} for `
    + `${fmt.sig(cycle.widthS * 1e9, 4)} ns, ${fmt.sig(cycle.delayS * 1e9, 4)} ns after the Sync — panel C`,
    { anchor: 'end', colour: swept === 'delay_ns' || swept === 'vcoll' ? 'accent-dark' : 'grey' });

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
    + (returns ? '' : ' · the return is after the record ends'));
  // The trigger sits one division into the record. The record origin differs from the
  // trigger origin used by the other signals.
  rules.push({ x1: X(0), colour: 'grey', width: 1, dash: '3 3' });
  // The trigger label needs a separate line because the panel caption extends past
  // its position.
  marks.push({ x: X(0) + 4, y: rect.y + 24, colour: 'grey', size: 11, halo: true, text: 'trigger' });
  if (record.fromTrigger !== null && record.fromTrigger >= start && record.fromTrigger < end) {
    const wx = X(record.fromTrigger);
    const wend = record.toTrigger === null ? end : Math.min(end, record.toTrigger);
    const wx1 = X(wend);
    shades.push({ x: wx, w: Math.max(0, wx1 - wx), colour: 'accent', opacity: 0.07 });
    rules.push({ x1: wx, colour: 'accent', width: 1.5 });
    if (record.toTrigger !== null && record.toTrigger <= end) {
      rules.push({ x1: wx1, colour: 'accent', width: 1, dash: '4 3' });
    }
    marks.push({ x: wx + 5, y: rect.y + rect.h - 8, weight: 600, colour: 'accent', size: 11,
      text: `t0_int ${fmt.sig(record.fromTrigger * 1e9, 4)} ns from the trigger`
        + (record.toTrigger === null ? '' : ` → ${fmt.sig(record.toTrigger * 1e9, 4)} ns`)
        + ' · with the pulse' });
    marks.push({ x: rect.x + rect.w - 5, y: rect.y + rect.h - 8, anchor: 'end', colour: 'accent', size: 11,
      text: 'Q = ∫ (light − dark) dt over the shaded part' });
  } else {
    marks.push({ x: rect.x + 5, y: rect.y + rect.h - 8, colour: 'alert', size: 11,
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
    note: `${fmt.sig(((end - start) / 10) * 1e9, 3)} ns/div; 1 division before the trigger, 9 after`,
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
 * Each signal band reserves a caption line above its waveform. Sharing the vertical
 * space would let captions overlap the preceding signal.
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
    size: options.size || 11,
  });
}

function rowLabels(rect, rows, names) {
  return names.map((name, i) => ({
    x: rect.x - 8, y: (rows[i].high + rows[i].low) / 2 + 3, text: name,
    anchor: 'end', colour: 'ink', weight: 600, size: 11,
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
 * The helper wraps fibre-delayed transitions into one period. Wrapped points must be
 * sorted to avoid drawing backward horizontal segments. The state at zero comes from
 * the period's last transition because a delayed edge can wrap to the start.
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
 * The helper formats short intervals with SI prefixes, such as 2.0 ms or 500 ns.
 * `fmt.duration` serves loop durations in seconds, minutes and hours rather than LED
 * periods.
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
