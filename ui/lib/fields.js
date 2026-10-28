// The field layout maps catalogue entries to card rows through pure `cardModel`;
// `ui/lib/card.js` renders them. Values, provenance, editability and estimates come
// from the service (`docs/ui-rules.md` §6). Range counts come from `estimate_text` to
// avoid duplicating rounding. Each field retains `doc` and `doc_full` for accessible
// explanations (§1). Fold counts derive from their actual fields. Separating models
// from DOM permits fixture tests without a browser.

/** Fold groups follow expected bench-setup access order. */
export const FOLD_ORDER = [
  'illumination', 'led', 'acquisition', 'timing', 'trigger', 'processing',
  'output', 'sourcemeter', 'axis', 'pinned',
];

/**
 * The layout defines ordered visible rows and grouped folds. Row specifications name
 * parameters or composites supported by `ui/lib/card.js`. `when` tests resolved
 * values. `otherwise: 'hide'` removes controls that do not apply, including pinned
 * fields owned by the axis, inapplicable V_oc flags, unused `v_sat` and non-pulsed
 * LED levels. `otherwise: 'fold'` retains applicable controls behind disclosure.
 * Conditional rows default to hidden because an ineffective control must not invite
 * edits.
 */
export const LAYOUT = {
  jv: {
    above: [
      { kind: 'range', label: 'sweep', names: ['start_v', 'stop_v', 'step_v'] },
      'settle_s', 'both_directions', 'pixel_area_cm2',
    ],
    readback: 'illumination',
    run: { label: 'Run', kind: 'run' },
  },

  jv_bace: {
    above: [
      // An illumination loop owns the inherited LED level, so that level replaces the
      // range (`docs/ui-rules.md` §6). A typed `led_v` still applies outside that
      // binding and folds rather than disappearing.
      { kind: 'field', name: 'led_v', when: (v, w) => isBound(w.led_v), otherwise: 'fold' },
      {
        kind: 'range', label: 'LED levels', names: ['led_start_v', 'led_stop_v', 'led_step_v'],
        when: (v, w) => !isBound(w.led_v), stepUnusedAtZeroWidth: true,
      },
      'led_settle_s', 'dark',
      { kind: 'range', label: 'sweep', names: ['start_v', 'stop_v', 'step_v'] },
      'settle_s', 'both_directions', 'pixel_area_cm2',
    ],
    readback: 'shutter',
    run: { label: 'Run', kind: 'run' },
  },

  bace: {
    above: [
      // The accent marks the swept quantity (`docs/ui-rules.md` §3). The axis chooses
      // the experiment: `vpre` is BACE and `delay_ns` is TDCF. Operator labels
      // identify the scan parameter and range; wire identifiers retain `axis_*`
      // names.
      { kind: 'segmented', name: 'axis_name', label: 'scan parameter', accent: true },
      { kind: 'range', label: 'scan range', names: ['axis_start', 'axis_stop', 'axis_step'], accent: true, stepUnusedAtZeroWidth: true },
      // `axis_name` selects the applicable V_oc flag. The `vpre` axis centres its
      // sweep on V_oc; other axes offset pinned V_pre from it. The inapplicable flag
      // is hidden rather than folded.
      { kind: 'field', name: 'centre_on_voc', when: (v) => v.axis_name === 'vpre' },
      { kind: 'voc' },
      'measure_dc',
      { kind: 'field', name: 'v_sat', when: (v) => Boolean(v.measure_dc) },
      { kind: 'field', name: 'vpre', when: (v) => v.axis_name !== 'vpre' },
      { kind: 'field', name: 'vpre_on_voc', when: (v) => v.axis_name !== 'vpre' },
      { kind: 'field', name: 'vcoll', when: (v) => v.axis_name !== 'vcoll' },
      { kind: 'field', name: 'delay_ns', when: (v) => v.axis_name !== 'delay_ns' },
      'n_loops', 'n_averages',
      { kind: 'field', name: 'invert_polarity' },
      { kind: 'polarity' },
    ],
    run: [{ label: 'Shot', kind: 'shot' }, { label: 'Scan', kind: 'run' }],
    chips: [
      // High storage cost must stay visible before a run can allocate gigabytes.
      { when: (v) => Boolean(v.store_shots), text: 'store_shots · every shot kept' },
    ],
  },

  light: {
    // The `light` node form reads all seven parameters to control shutter, LED,
    // levels and settle time. Bench controls instead live in the Instruments panel.
    // `node: true` marks the three node-defining parameters with no bench switch and
    // keeps them visible in the node form.
    above: [
      { kind: 'segmented', name: 'shutter', node: true },
      { kind: 'segmented', name: 'led_mode', node: true },
      'led_v', 'led_low_v', 'pulse_frequency_hz', 'duty_percent',
      { kind: 'field', name: 'settle_s', node: true },
    ],
  },

  power: { above: ['wavelength_nm', 'samples'], run: [{ label: 'Read', kind: 'run' }] },
  // Settle names the complete temperature operation: setpoint, band wait and `hold_s`
  // dwell. Calling it Hold would imply only the dwell and duplicate the field label.
  temperature: {
    above: ['setpoint_k', 'tolerance_k', 'hold_s', 'timeout_s'],
    run: { label: 'Settle', kind: 'run' },
  },
};

/**
 * The list orders bench cards. Shutter and LED controls live in Instruments; the
 * `light` node form lives on the pipeline tab.
 */
export const BENCH_CARDS = ['jv', 'jv_bace', 'bace', 'power', 'temperature'];

/**
 * Only cards with a Run need Start validation. A standalone `light` tree is refused
 * because park undoes its action, so action-only cards must not submit it to
 * validation.
 */
export function cardRuns(name) {
  const layout = LAYOUT[name];
  return Boolean(layout && [].concat(layout.run || []).length);
}

/** An enum small enough to be a segmented control rather than a select. */
export const SEGMENTED_MAX = 4;

/**
 * Pipeline-supplied values appear resolved rather than as empty inputs
 * (`docs/ui-rules.md` §6).
 */
function isBound(wire) {
  return Boolean(wire && (wire.source === 'inherited' || wire.source === 'derived'));
}

/**
 * The table identifies visible parameters that the current configuration does not
 * read. Each value is its explanation or `null` when applicable. Inert parameters
 * must agree with service behaviour so a hidden or disabled control cannot reject the
 * run. Bench-dependent rules remain silent in node forms without a bench snapshot.
 * Reasons identify the consuming code rather than infer operator intent.
 */
const SMU_FIELDS = ['smu_current_compliance_a', 'smu_voltage_compliance_v', 'smu_settle_jsc_ms',
  'smu_settle_voc_ms', 'smu_settle_jsat_ms', 'smu_nplc', 'smu_averaging', 'smu_terminals', 'smu_four_wire'];
const SMU_DC_ONLY = ['smu_settle_jsc_ms', 'smu_settle_voc_ms', 'smu_settle_jsat_ms'];

const noPowerMeter = (bench) => Boolean(bench && bench.instruments && bench.instruments.power
  && bench.instruments.power.available === false);
const unwired331 = (bench) => Boolean(bench && bench.instruments && bench.instruments.temperature
  && bench.instruments.temperature.wired === false);
const settleOnClock = (v, bench) => (noPowerMeter(bench)
  ? 'no power meter · the LED settles on the clock, led_settle_s alone' : null);
const smuNotSourced = (v) => (v.measure_dc ? null : 'measure_dc = false · the Keithley is not sourced');
const dcOnly = () => 'only measure_dc reads it · a J–V sweep settles settle_s per point';

const INERT = {
  bace: {
    // `dark_settle_s` belongs to dark levels, which `dark_reference = "same"` skips.
    dark_settle_s: (v) => (v.dark_reference === 'same'
      ? 'dark_reference = same · the dark levels are not rewritten, so nothing settles after them' : null),
    // `trigger_slope_positive` has no effect without `external_trigger`.
    trigger_slope_positive: (v) => (v.external_trigger === false
      ? 'external_trigger = false · the 81150A is not armed by the Sync, so its slope is not read' : null),
    // LED settling uses the power meter when present and elapsed time otherwise.
    led_settle_max_s: settleOnClock,
    led_settle_tolerance: settleOnClock,
    // A `bace` run accesses the source-meter only with `measure_dc` enabled.
    ...Object.fromEntries(SMU_FIELDS.map((n) => [n, smuNotSourced])),
  },
  // J–V sweeps use `settle_s`. DC metric toggles and their settle times belong to
  // `measure_dc`, which J–V modules do not call.
  jv: Object.fromEntries(SMU_DC_ONLY.map((n) => [n, dcOnly])),
  jv_bace: Object.fromEntries(SMU_DC_ONLY.map((n) => [n, dcOnly])),
  temperature: {
    // Without a 331, temperature settling pauses for operator input. `hold_s` follows
    // resume and `tolerance_k` checks the entered value; no automatic wait consumes
    // `timeout_s`.
    timeout_s: (v, bench) => (unwired331(bench)
      ? '331 not wired · the pause waits for the operator and has no timeout' : null),
  },
};

/** The reason `name` is not read on this card as it stands, or `null`. */
export function inertReason(module, name, values, bench = null) {
  const rule = INERT[module] && INERT[module][name];
  return rule ? rule(values, bench) : null;
}

/**
 * `cardModel` turns a catalogue entry into bench or node rows. `bench` supplies
 * read-back rows. A node form keeps differences visible: loop-bound or derived
 * values, node overrides and `node: true` rows. Unchanged bench values fold under the
 * same-as-bench group; bench cards omit node-only rows. The result separates `above`,
 * `fold` and `hidden`, whose union covers every parameter. Hidden values do not
 * apply; folded values remain applicable, so their counts must stay distinct.
 */
export function cardModel(entry, { bench = null, form = 'bench' } = {}) {
  const layout = LAYOUT[entry.name] || { above: (entry.params || []).map((p) => p.name) };
  const values = Object.fromEntries((entry.params || []).map((p) => [p.name, p.value]));
  // All specifications carry the same inert reason above and below the fold to
  // prevent conflicting controls.
  const specs = (entry.params || []).map((p) => {
    const inert = inertReason(entry.name, p.name, values, bench);
    return inert ? { ...p, inert } : p;
  });
  const wire = Object.fromEntries(specs.map((p) => [p.name, p]));

  const used = new Set();
  const hidden = new Set();
  const above = [];

  // Node forms keep differences from the bench above the fold.
  const differs = (n) => Boolean(wire[n] && (wire[n].node || isBound(wire[n])));

  for (const spec of layout.above || []) {
    const row = normalise(spec);
    const names = rowNames(row);
    if (!names.every((n) => n in wire)) continue;
    if (row.node && form !== 'node') {
      names.forEach((n) => hidden.add(n));
      continue;
    }
    if (form === 'node' && !row.node && !names.some(differs)) continue;
    if (row.when && !row.when(values, wire)) {
      if (row.otherwise !== 'fold') names.forEach((n) => hidden.add(n));
      continue;
    }
    names.forEach((n) => used.add(n));
    const built = buildRow(row, wire, values, entry);
    if (built) above.push(built);
  }

  // The swept axis replaces its pinned field, so that duplicate field is hidden
  // rather than folded.
  if (entry.name === 'bace' && values.axis_name in wire) hidden.add(values.axis_name);

  const fold = foldGroups(specs, used, hidden);
  return {
    name: entry.name,
    form,
    title: entry.title || entry.name,
    status: entry.status,
    kind: entry.kind,
    estimate: entry.estimate_text || '',
    estimate_s: entry.estimate_s,
    last: entry.last || null,
    needs: entry.needs || [],
    chips: (layout.chips || []).filter((c) => c.when(values)).map((c) => c.text),
    readback: readbackRow(layout.readback, bench),
    above,
    fold,
    hidden: [...hidden],
    run: layout.run ? [].concat(layout.run) : [],
    actions: (layout.actions || []).map((a) => ({ ...a, args: a.args ? a.args(values) : {} })),
    points: points(entry.estimate_text),
  };
}

function normalise(spec) {
  return typeof spec === 'string' ? { kind: 'field', name: spec } : spec;
}

function rowNames(row) {
  if (row.kind === 'range') return row.names;
  if (row.kind === 'voc') return ['voc', 'led_v'];
  if (row.kind === 'polarity') return ['output_polarity', 'inverted_output'];
  return [row.name];
}

function buildRow(row, wire, values, entry) {
  if (row.kind === 'range') {
    const [start, stop, raw] = row.names.map((n) => wire[n]);
    // Equal range limits give one point without consulting the step. J–V sweep
    // validation still requires positive `step_v`, so its field remains active.
    const zeroWidth = start.value !== null && start.value !== undefined
      && Number(start.value) === Number(stop.value);
    const step = row.stepUnusedAtZeroWidth && zeroWidth && !raw.inert
      ? { ...raw, inert: 'start = stop · one point, so the step is not read' } : raw;
    return { kind: 'range', label: row.label, start, stop, step, points: points(entry.estimate_text), accent: Boolean(row.accent) };
  }
  if (row.kind === 'voc') {
    // The V_oc row shows the service's missing-value reason or derived provenance.
    // `led_v` stays beside it because the measurement level must equal the transient
    // pulse high level (`docs/ui-rules.md` §6).
    const need = (entry.needs || []).find((n) => n.code === 'voc') || null;
    return { kind: 'voc', voc: wire.voc, led: wire.led_v, need };
  }
  if (row.kind === 'polarity') {
    // `RunConfig.polarity_instruction()` consults `inverted_output` only for
    // `output_polarity = "auto"`. Showing it independently would offer an ineffective
    // control for three of the four choices.
    return {
      kind: 'polarity',
      mode: wire.output_polarity, fallback: wire.inverted_output,
      effective: effectivePolarity(values),
    };
  }
  const spec = wire[row.name];
  if (!spec) return null;
  const segmented = row.kind === 'segmented'
    || (spec.type === 'enum' && spec.choices.length <= SEGMENTED_MAX);
  return { kind: segmented ? 'segmented' : 'field', spec, accent: Boolean(row.accent), label: row.label || null };
}

/** The helper resolves the polarity instruction sent to the 81150A. */
export function effectivePolarity(values) {
  const mode = String(values.output_polarity || 'auto').toLowerCase();
  if (mode === 'leave') return { write: false, text: 'left as found, and read back' };
  if (mode === 'norm') return { write: true, text: 'NORM' };
  if (mode === 'inv') return { write: true, text: 'INV' };
  return { write: true, text: values.inverted_output ? 'INV' : 'NORM', from: 'inverted_output' };
}

/**
 * The helper groups folded fields in `FOLD_ORDER`. Empty groups do not render,
 * including the output group when its only field appears in the composite polarity
 * control.
 */
function foldGroups(specs, used, hidden) {
  const by = new Map();
  for (const p of specs) {
    if (used.has(p.name) || hidden.has(p.name)) continue;
    const group = p.group || 'other';
    if (!by.has(group)) by.set(group, []);
    by.get(group).push(p);
  }
  const order = [...FOLD_ORDER, ...[...by.keys()].filter((g) => !FOLD_ORDER.includes(g))];
  return order.filter((g) => by.has(g)).map((g) => ({
    group: g, params: by.get(g), count: by.get(g).length,
    // A whole group that is not read says so on its button, before it is opened.
    inert: by.get(g).every((p) => p.inert),
  }));
}

/** The count includes fields across every fold group. */
export function foldCount(model) {
  return model.fold.reduce((n, g) => n + g.count, 0);
}

/**
 * The point count comes from service `estimate_text`. Each edit returns a fresh count
 * and cost together, avoiding a second rounding implementation.
 */
export function points(estimateText) {
  const m = /×\s*(\d+)\s*pts/.exec(String(estimateText || ''));
  return m ? Number(m[1]) : null;
}

/**
 * The light row uses `/bench` read-back rather than module parameters. A `jv` run can
 * produce dark or light curves with the same parameters, so read-back must identify
 * its illumination.
 */
function readbackRow(kind, bench) {
  if (!kind || !bench) return null;
  const instruments = bench.instruments || {};
  const shutter = instruments.shutter || {};
  const led = instruments.led || {};
  const inferred = new Set(bench.inferred || []);
  const isInferred = inferred.has('shutter') || shutter.how === 'inferred';
  if (kind === 'shutter') {
    return { kind: 'shutter', open: shutter.open, inferred: isInferred };
  }
  // Light requires all three state values. An unreadable value means unknown
  // illumination, matching the run's recorded label rather than implying dark.
  const open = shutter.open;
  const on = led.output;
  const mode = led.mode;
  const lit = (open === null || open === undefined || on === null || on === undefined
    || !mode || mode === '?')
    ? null
    : Boolean(open && on && String(mode).toUpperCase() !== 'OFF');
  // In DC mode, `set_dc` writes `:VOLT:OFFS`, reported separately from `high_v`. The
  // latter retains pulse amplitude and would predict a different LED level from the
  // run's recorded read-back.
  const level = driveLevel(led);
  return {
    kind: 'illumination', lit, open, mode, level, inferred: isInferred,
    text: lit === null ? 'unknown' : lit ? levelText(level) : 'dark',
  };
}

/**
 * The helper selects the driven LED level by mode, returning `undefined` without
 * read-back.
 */
export function driveLevel(led) {
  const dc = String((led || {}).mode || '').toUpperCase() === 'DC';
  const level = dc ? (led || {}).offset_v : (led || {}).high_v;
  return level === undefined ? null : level;
}

function levelText(level) {
  return level === null || level === undefined ? 'lit' : `${Number(level)} V`;
}
