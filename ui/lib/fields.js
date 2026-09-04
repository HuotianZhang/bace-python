// Which fields sit above the fold on each card, and in what order — the table
// `docs/ui-plan.md` M2 said would be produced during this phase, and which
// `docs/ui-fields.md` argues out in prose. This file is that table as data,
// plus `cardModel`, the pure function that turns one `GET /modules` entry into
// the rows a card draws.
//
// Three things it is careful about, each of them a rule from somewhere:
//
//   * **Nothing is re-derived** (`ui-rules` §6). `{value, source, detail,
//     editable}` are the service's answer and are rendered as they came. The
//     point count in a range row is *read out of* `estimate_text`, which the
//     service recomputes and returns on every `PUT` — the span is rounded,
//     never truncated, and a second implementation of that rounding would
//     eventually disagree in the one case that mattered.
//   * **Every field carries its explanation** (`ui-rules` §1). `doc` is the
//     line under the field; `doc_full` is the whole of it, revealed on hover.
//     A name like `smu_nplc`, `trigger_sweep` or `duty_percent` means nothing
//     on its own, and `inverted_output` means something other than it looks.
//   * **Counts are computed, never typed.** The Round 3 artboard's "17 more"
//     was stale the day the SMU fields landed. Every fold count here is the
//     length of what is actually in the fold.
//
// The model is separate from the DOM (`card.js`) for the reason `rail.js` is:
// `ui/tests/fields.test.mjs` holds these rules down against a recorded
// `/modules` with no browser in the way.

/** Fold groups, in the order an operator reaches for them on a rig day. */
export const FOLD_ORDER = [
  'illumination', 'led', 'acquisition', 'timing', 'trigger', 'processing',
  'output', 'sourcemeter', 'axis', 'pinned',
];

/**
 * The layout, per module. `above` is an ordered list of row specs; anything a
 * module has and `above` does not name falls into the fold, grouped.
 *
 * A row spec is a parameter name, or one of the composite rows `card.js`
 * knows how to draw. `when` is given the resolved values and decides whether
 * the row is drawn; `otherwise` says where it goes when it is not, and the
 * distinction is not cosmetic:
 *
 *   * `'hide'` — **the run will not read this value in this configuration.**
 *     The pinned field that *is* the axis; the V_oc flag the other axis owns
 *     (`vpre_on_voc` is invalid with the vpre axis, and `centre_on_voc` is the
 *     flag there); `v_sat`, which only the `measure_dc` branch reads; the
 *     pulse levels when the LED is not being pulsed. Showing these would offer
 *     a knob that does nothing, which is the trap the polarity pair was.
 *   * `'fold'` — it applies, it just is not worth a line right now. It goes
 *     into its group in the fold and is one click away.
 *
 * Default is `'hide'`, because a conditional row is usually conditional for
 * the first reason.
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
      // The inherited level wins the row: inside an illumination loop the loop
      // owns the LED and the range is not what runs (`ui-rules` §6).
      // Not inherited is not *ignored*: a typed `led_v` still wins for the
      // run ("inherited from an illumination loop, or typed"), so it folds
      // rather than disappearing.
      { kind: 'field', name: 'led_v', when: (v, w) => isBound(w.led_v), otherwise: 'fold' },
      {
        kind: 'range', label: 'LED levels', names: ['led_start_v', 'led_stop_v', 'led_step_v'],
        when: (v, w) => !isBound(w.led_v),
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
      // §3: the accent is the swept quantity, and which quantity is swept *is*
      // the choice of experiment — `vpre` is BACE, `delay_ns` is TDCF. It was
      // spent on the polarity pair, which is a knob, not the experiment.
      { kind: 'segmented', name: 'axis_name', accent: true },
      { kind: 'range', label: 'axis', names: ['axis_start', 'axis_stop', 'axis_step'], accent: true },
      // Exactly one of these two is ever valid, and `axis_name` decides which:
      // with the vpre axis the whole sweep is centred on V_oc; with any other
      // the *pinned* vpre is an offset from it. The other is not folded, it is
      // hidden — it does not apply.
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
      // A parameter whose cost is measured in gigabytes is not invisible.
      { when: (v) => Boolean(v.store_shots), text: 'store_shots · every shot kept' },
    ],
  },

  light: {
    // Every field, unconditionally — unlike every other card, and for a
    // reason the browser found: the buttons below *act on these values*.
    // `DC` sends `led_v`, `Pulse` sends all four, and a button that drives the
    // lamp from a number the operator cannot see is worse than a field they
    // do not need. (The pipeline node form, M5, can hide by `led_mode`: there
    // the node is what runs, and what it does not read it does not read.)
    above: [
      { kind: 'segmented', name: 'shutter' },
      { kind: 'segmented', name: 'led_mode' },
      'led_v', 'led_low_v', 'pulse_frequency_hz', 'duty_percent', 'settle_s',
    ],
    readback: 'illumination',
    // Not a Run. A run whose only module is `light` is refused
    // (`light.undone-by-park`): every run ends parked, so it would set a light
    // and hand it straight back. The manual form is the bench action, which
    // does not go through the worker. One action per click, as M1's strip does.
    actions: [
      { label: 'Open', action: 'shutter-open' },
      { label: 'Shut', action: 'shutter-shut' },
      { label: 'DC', action: 'set-led-dc', args: (v) => ({ level: v.led_v }) },
      {
        label: 'Pulse',
        action: 'set-led-pulse',
        args: (v) => ({
          level: v.led_v, low: v.led_low_v,
          frequency_hz: v.pulse_frequency_hz, duty_percent: v.duty_percent,
        }),
      },
      { label: 'LED off', action: 'led-off' },
    ],
  },

  power: { above: ['wavelength_nm', 'samples'], run: [{ label: 'Read', kind: 'run' }] },
  temperature: { above: ['setpoint_k', 'tolerance_k', 'hold_s', 'timeout_s'], run: { label: 'Hold', kind: 'run' } },
};

/** Which modules get a bench card, in the order the artboard lays them out. */
export const BENCH_CARDS = ['jv', 'jv_bace', 'bace', 'light', 'power', 'temperature'];

/** An enum small enough to be a segmented control rather than a select. */
export const SEGMENTED_MAX = 4;

/** A value the pipeline supplies: shown as what it is, never as an empty input. */
function isBound(wire) {
  return Boolean(wire && (wire.source === 'inherited' || wire.source === 'derived'));
}

/**
 * One card, as rows. `entry` is a `GET /modules` entry (or the one a `PUT`
 * answers with — same shape, which is why an edit re-renders from the
 * response). `bench` is the `/bench` snapshot, for the read-back rows.
 *
 * Returns `{name, title, status, kind, estimate, needs, chips, above, fold,
 * run, actions, hidden}`. `hidden` is the parameters the run will not read in
 * this configuration, kept apart from `fold` so a card can say "32 folded,
 * 3 not applicable" and mean both halves. `above + fold + hidden` is always
 * every parameter the module has.
 */
export function cardModel(entry, { bench = null } = {}) {
  const layout = LAYOUT[entry.name] || { above: (entry.params || []).map((p) => p.name) };
  const wire = Object.fromEntries((entry.params || []).map((p) => [p.name, p]));
  const values = Object.fromEntries((entry.params || []).map((p) => [p.name, p.value]));

  const used = new Set();
  const hidden = new Set();
  const above = [];

  for (const spec of layout.above || []) {
    const row = normalise(spec);
    const names = rowNames(row);
    if (!names.every((n) => n in wire)) continue;
    if (row.when && !row.when(values, wire)) {
      if (row.otherwise !== 'fold') names.forEach((n) => hidden.add(n));
      continue;
    }
    names.forEach((n) => used.add(n));
    const built = buildRow(row, wire, values, entry);
    if (built) above.push(built);
  }

  // The axis's own pinned field is not folded — it *is* the axis.
  if (entry.name === 'bace' && values.axis_name in wire) hidden.add(values.axis_name);

  const fold = foldGroups(entry, used, hidden);
  return {
    name: entry.name,
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
    const [start, stop, step] = row.names.map((n) => wire[n]);
    return { kind: 'range', label: row.label, start, stop, step, points: points(entry.estimate_text), accent: Boolean(row.accent) };
  }
  if (row.kind === 'voc') {
    // The needs row. Both states the artboard draws are the two the service
    // answers: `⚠ none · run jv_bace first` from `needs[]`, and the derived
    // entry `1.0423 V @ 1.020 · jv_bace 20:58`. `led_v` lives *in* this row
    // because the coupling invariant is that the level V_oc was measured at
    // and the level the transient pulses at are one number.
    const need = (entry.needs || []).find((n) => n.code === 'voc') || null;
    return { kind: 'voc', voc: wire.voc, led: wire.led_v, need };
  }
  if (row.kind === 'polarity') {
    // One control, not two. `RunConfig.polarity_instruction()` reads
    // `inverted_output` only when `output_polarity` is `auto` — at NORM, INV
    // or leave the boolean is inert, and two fields where one is silently
    // dead at three of four settings is a trap however they are laid out.
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
  return { kind: segmented ? 'segmented' : 'field', spec, accent: Boolean(row.accent) };
}

/** What the 81150A will actually be told, given the pair. */
export function effectivePolarity(values) {
  const mode = String(values.output_polarity || 'auto').toLowerCase();
  if (mode === 'leave') return { write: false, text: 'left as found, and read back' };
  if (mode === 'norm') return { write: true, text: 'NORM' };
  if (mode === 'inv') return { write: true, text: 'INV' };
  return { write: true, text: values.inverted_output ? 'INV' : 'NORM', from: 'inverted_output' };
}

/**
 * Everything not above the fold, by group, in `FOLD_ORDER`. A group that ends
 * up empty does not render — which is how the `output` group disappears once
 * `inverted_output` is drawn inside the polarity control above.
 */
function foldGroups(entry, used, hidden) {
  const by = new Map();
  for (const p of entry.params || []) {
    if (used.has(p.name) || hidden.has(p.name)) continue;
    const group = p.group || 'other';
    if (!by.has(group)) by.set(group, []);
    by.get(group).push(p);
  }
  const order = [...FOLD_ORDER, ...[...by.keys()].filter((g) => !FOLD_ORDER.includes(g))];
  return order.filter((g) => by.has(g)).map((g) => ({ group: g, params: by.get(g), count: by.get(g).length }));
}

/** How many folded, across every group — the number the disclosure counts. */
export function foldCount(model) {
  return model.fold.reduce((n, g) => n + g.count, 0);
}

/**
 * The point count, read out of the service's own `estimate_text`
 * (`1 curves × 141 pts ≈ 16 s`). Never recomputed: `PUT` returns a fresh
 * entry with a fresh estimate, so the card gets the count and the cost
 * together and there is no second rounding rule to get wrong.
 */
export function points(estimateText) {
  const m = /×\s*(\d+)\s*pts/.exec(String(estimateText || ''));
  return m ? Number(m[1]) : null;
}

/**
 * The bench read-back row: not a parameter, and from `/bench` rather than
 * `/modules`. On `jv` it is the whole of the module's relationship with the
 * light — the same six parameters give a dark curve or a light one, and only
 * this says which.
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
  // `lit` needs all three, and any of them being unreadable makes it unknown
  // rather than dark — the same rule `experiment.jv.illumination_state`
  // applies, because this row has to predict the label that run will write.
  const open = shutter.open;
  const on = led.output;
  const mode = led.mode;
  const lit = (open === null || open === undefined || on === null || on === undefined
    || !mode || mode === '?')
    ? null
    : Boolean(open && on && String(mode).toUpperCase() !== 'OFF');
  // In DC the level is the *offset*: `set_dc` writes `:VOLT:OFFS`, and
  // `/bench` reports it separately from `high_v`, which keeps the previous
  // pulse amplitude. Rendering `high_v` there would have this row predict one
  // level while the `jv` that follows records another — and the two are the
  // same reading, so disagreeing is worse than either being wrong alone.
  const level = driveLevel(led);
  return {
    kind: 'illumination', lit, open, mode, level, inferred: isInferred,
    text: lit === null ? 'unknown' : lit ? levelText(level) : 'dark',
  };
}

/** The level the LED is actually driven at, by mode. Undefined where unread. */
export function driveLevel(led) {
  const dc = String((led || {}).mode || '').toUpperCase() === 'DC';
  const level = dc ? (led || {}).offset_v : (led || {}).high_v;
  return level === undefined ? null : level;
}

function levelText(level) {
  return level === null || level === undefined ? 'lit' : `${Number(level)} V`;
}
