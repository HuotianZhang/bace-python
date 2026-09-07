// The Keithley 2400 by itself — `docs/ui-plan.md` M7, the SMU panel.
//
// This is the instrument's own front panel, drawn in a browser: source a
// voltage or a current, hold a compliance, switch the output on, and watch
// what comes back. It touches the SourceMeter and nothing else — no module,
// no run, no folder, and no other instrument. The rest of the console is
// about a measurement; this one is about a device on a probe station and a
// number an operator wants now.
//
// Four things it owes that the instrument's own panel does not have to:
//
//   * **the display is dashes unless there is something to display.** The
//     2400 shows dashes with its output off, and so does this — with the
//     output off a `:READ?` still answers, with the source disconnected
//     inside the instrument, and the near-zero it returns looks exactly like
//     a measurement of a dead device (`drivers/keithley2400.read_panel`). The
//     same for a panel that is not applied.
//   * **a run takes the instrument away.** Every measurement routine opens
//     with `*RST`, which drops the panel and the compliance with it, so while
//     a run holds the bench this panel is not the operator's and afterwards
//     it is unapplied. The service says so (`service/live.py` clears `panel`
//     the moment a run drives the SMU) and this renders what it says rather
//     than deciding for itself.
//   * **the relay is not part of this panel and still decides what it reads.**
//     The device terminals are shared with the amplifier, and off the
//     SourceMeter's side the panel reads an open circuit. It is not moved
//     from here — a second surface for the one action on this rig that can
//     damage hardware is not reach, it is a second place to hit by accident
//     (`ui-rules` §14, the same argument that keeps Park on the strip alone).
//     So the panel states the position and names where it moves.
//   * **`rig.toml`'s two ceilings apply to a level, not only to a
//     compliance.** Until this panel nothing could type a source level at
//     all, and the service refuses one past the ceiling; the fields show the
//     ceiling beside the value so the refusal is not the first the operator
//     hears of it.
//
// `smuPanelModel` is a pure function of the store and is what the tests hold
// down; `renderSmuPanel` draws it.

import { h, keyed } from './dom.js';
import * as fmt from './format.js';

/** Bench states in which a run holds the worker, so nothing here may act. */
export const BUSY_STATES = new Set(['preflight', 'running', 'paused', 'stopping']);

/**
 * The display refresh, in seconds — the same switch the power monitor has,
 * because it is the same kind of control: it starts and stops a thread in the
 * *service*, not a timer in this tab, so the panel goes on reading with the
 * browser closed and two consoles cannot disagree about the interval.
 */
export const REFRESH_STEPS = [0.2, 0.5, 1, 2, 5];

/** How many intervals a monitored reading may be old before it reads stale. */
export const STALE_INTERVALS = 3;

/** The panel fields the operator types into, in the order drawn. */
export const FIELDS = [
  { name: 'level', label: 'level', figures: 6,
    doc: 'What the source is held at. Volts sourcing voltage, amps sourcing current.' },
  { name: 'current_compliance_a', label: 'I limit', unit: 'A', ceiling: 'current_a', figures: 3,
    doc: 'The most current the 2400 will pass while it sources voltage; it clamps there.' },
  { name: 'voltage_compliance_v', label: 'V limit', unit: 'V', ceiling: 'voltage_v', figures: 4,
    doc: 'The most voltage the 2400 will put across the device while it sources current.' },
  { name: 'nplc', label: 'NPLC', figures: 3,
    doc: 'Integration time in power-line cycles. 1 NPLC is 20 ms on 50 Hz mains and rejects its hum; below 1 is faster and noisier.' },
  { name: 'averaging', label: 'filter', unit: 'readings', figures: 0,
    doc: 'How many readings the 2400 averages into each one it reports. 1 is the filter off.' },
  { name: 'source_range', label: 'range', placeholder: 'auto', figures: 3,
    doc: 'The source range to hold, or empty for the instrument\'s autorange — which is what a panel wants when the level is about to move by decades.' },
];

/**
 * The whole panel as a value: `{available, setup, reading, switches, fields,
 * notes, …}`.
 *
 * Everything a screen needs and nothing it has to re-derive — in particular
 * `applied`, which is the difference between a panel the instrument is in and
 * a form somebody has filled in, and is the thing this surface is easiest to
 * lie about.
 */
export function smuPanelModel(state) {
  const bench = state.bench || {};
  const instruments = bench.instruments || {};
  const smu = instruments.smu || {};
  const relay = instruments.relay || {};
  const unavailable = (bench.unavailable || {}).smu || null;
  const setup = smu.panel || smu.panel_defaults || null;
  const available = !unavailable && Boolean(setup);
  const applied = Boolean(smu.panel);
  const output = smu.output === undefined ? null : smu.output;
  const inferred = new Set(bench.inferred || []).has('smu') || smu.how === 'inferred';
  const busy = BUSY_STATES.has(state.benchState);
  const monitor = (state.monitors || []).find((m) => m.name === 'smu') || null;
  const ceiling = smu.ceiling || { current_a: null, voltage_v: null };

  return {
    available,
    /** Why there is no panel, when there is none. */
    unavailable: available ? null : (unavailable
      || 'no SourceMeter on this bench: nothing answered at [sourcemeter] address'),
    applied,
    setup,
    output,
    inferred,
    busy,
    ceiling,
    relay: relay.position || 'unknown',
    monitor: monitor ? { running: true, interval_s: monitor.interval_s,
                         readings: monitor.readings, skipped: monitor.skipped }
                     : { running: false, interval_s: null, readings: 0, skipped: 0 },
    reading: readingModel(state, { applied, output, monitor }),
    switches: available ? switchesModel(setup, output, busy) : [],
    fields: available ? fieldsModel(setup, ceiling) : [],
    notes: available ? notesModel({ applied, busy, relay: relay.position || null }) : [],
  };
}

/**
 * What the display shows. `null` is dashes, and dashes is what the panel
 * shows whenever there is nothing being measured: the output off, the panel
 * not applied, or nothing read yet.
 *
 * A reading is never carried past the state it was taken in — switching the
 * output off leaves the last numbers on screen on some instruments and that
 * is the one thing a bench display must not do, because the numbers are then
 * a measurement of a moment that has passed with nothing saying so.
 */
export function readingModel(state, { applied, output, monitor }) {
  const last = state.smu || null;
  if (!applied || output !== true || !last) return null;
  const interval = monitor && monitor.interval_s ? Number(monitor.interval_s) : null;
  const age = Number.isFinite(last.ts) ? (Date.now() / 1000) - last.ts : null;
  return {
    volts: last.volts, amps: last.amps,
    ohms: last.ohms === null || last.ohms === undefined ? ohmsOf(last) : last.ohms,
    compliance: last.compliance === undefined ? null : last.compliance,
    at: last.ts, age,
    // Stale is a claim about a *monitored* reading: the monitor is running and
    // has not delivered for three of its intervals, which on this bench means
    // the worker has the GPIB and the panel is watching a run go by. A spot
    // reading taken by hand is not stale, it is simply as of when it was
    // taken, and the clock beside it says when.
    stale: Boolean(monitor && interval && age !== null && age > STALE_INTERVALS * interval),
    monitored: Boolean(monitor),
  };
}

function ohmsOf(reading) {
  const { volts, amps } = reading;
  if (!Number.isFinite(volts) || !Number.isFinite(amps) || amps === 0) return null;
  return volts / amps;
}

/**
 * The four switches, and which of them the output locks.
 *
 * `function`, `terminals` and sensing are `keithley2400.PANEL_STATIC`: the
 * first swings the source between volts and amps, and the other two change
 * which wires the instrument is measuring through. None is changed under a
 * live output — on the instrument or here — so the switch is drawn locked
 * with the sentence that says how to get at it.
 */
export function switchesModel(setup, output, busy) {
  const live = output === true;
  const locked = live
    ? 'the output is ON: switch it off to change this, then switch it back on'
    : null;
  return [
    { key: 'function', label: 'source', locked, busy,
      positions: [
        { id: 'voltage', label: 'V', on: setup.function === 'voltage' },
        { id: 'current', label: 'A', on: setup.function === 'current' },
      ] },
    { key: 'terminals', label: 'terminals', locked, busy,
      positions: [
        { id: 'FRON', label: 'front', on: setup.terminals === 'FRON' },
        { id: 'REAR', label: 'rear', on: setup.terminals === 'REAR' },
      ] },
    // A boolean whose two positions have names on the instrument, so it is a
    // pair and not a tick (`ui-rules` §14): the 2400 says `4W` on its display,
    // and nobody outside this repository reads an empty box as "two-wire".
    { key: 'four_wire', label: 'sensing', locked, busy,
      positions: [
        { id: false, label: '2-wire', on: setup.four_wire === false },
        { id: true, label: '4-wire', on: setup.four_wire === true },
      ] },
    // The loudest thing on this screen, and `ui-rules` §3 says so: a live
    // source is the alert register, not an accent and not a grey.
    { key: 'output', label: 'output', locked: null, busy, alert: live,
      positions: [
        { id: false, label: 'off', on: output === false },
        { id: true, label: 'on', on: live },
      ] },
  ];
}

/** The typed fields, with the ceiling each is held under. */
export function fieldsModel(setup, ceiling) {
  return FIELDS.map((spec) => {
    const value = setup[spec.name];
    const unit = spec.name === 'level' || spec.name === 'source_range'
      ? setup.unit : (spec.unit || '');
    return {
      ...spec,
      unit,
      value: value === null || value === undefined ? null : value,
      text: fieldText(value, spec),
      limit: spec.ceiling ? ceiling[spec.ceiling] : null,
    };
  });
}

/** A field's value as it is typed back into the box — never rounded away. */
export function fieldText(value, spec) {
  if (value === null || value === undefined) return '';
  if (spec.figures === 0) return String(Math.round(value));
  // `sig` and not `toFixed`: a 20 nA compliance and a 50 mA one go in the
  // same box, and a box that shows `0.0000` for the first is a box that
  // cannot be corrected without retyping what is already right.
  return trimZeros(fmt.sig(value, spec.figures + 3));
}

/**
 * `0.0500000` -> `0.05`, `2.00000e-8` -> `2e-8`.
 *
 * The digits that are not there are not information, and this is a box the
 * operator corrects rather than a column they read down — a value that has to
 * be cleared before it can be edited is one that gets cleared and mistyped.
 * (`ui-rules` §2's significant figures are about what a measurement resolves;
 * this is a setting being handed back to whoever typed it.)
 */
export function trimZeros(text) {
  const [mantissa, exponent] = String(text).split('e');
  const cut = mantissa.includes('.')
    ? mantissa.replace(/0+$/, '').replace(/\.$/, '')
    : mantissa;
  return exponent === undefined ? cut : `${cut}e${exponent}`;
}

/**
 * The conditions worth a sentence. Each names its own remedy, and none of
 * them offers a button for an instrument this panel does not own.
 */
export function notesModel({ applied, busy, relay }) {
  const notes = [];
  if (busy) {
    notes.push({ level: 'warn', key: 'busy',
      text: 'a run holds the bench: the SourceMeter is the run\'s until it ends, '
          + 'and the run resets it — the panel comes back unapplied.' });
  }
  if (!applied) {
    notes.push({ level: 'info', key: 'unapplied',
      text: 'the SourceMeter is not on the panel: nothing has told it what to source. '
          + 'Set the source to put it there — the output will not switch on until it is.' });
  }
  if (relay !== 'sourcemeter') {
    notes.push({ level: 'warn', key: 'relay',
      text: relay === 'amplifier'
        ? 'the relay is on the amplifier, so the device is not on the SourceMeter\'s '
          + 'side of it and this panel is reading an open circuit. The bench tab\'s '
          + 'Instruments row moves it — and it will not move while this output is on.'
        : 'the relay has not been read back, so which instrument the device is '
          + 'connected to is unknown. The bench tab\'s Instruments row reads and moves it.' });
  }
  return notes;
}

// -- the display's own numbers -----------------------------------------------
// Six significant figures, which is what the 2400's display carries, and SI
// prefixes where they are natural (`ui-rules` §2). Not `format.amps`, which is
// three figures for a compliance on the rail: a panel reading a device settle
// needs the digits that are moving.

const OHM_PREFIXES = [[1, ''], [1e3, 'k'], [1e6, 'M'], [1e9, 'G']];

export function displayVolts(v, figures = 6) {
  if (v === null || v === undefined || Number.isNaN(v)) return fmt.ABSENT;
  return fmt.sig(v, figures) + ' V';
}

export function displayAmps(a, figures = 6) {
  if (a === null || a === undefined || Number.isNaN(a)) return fmt.ABSENT;
  if (a === 0) return '0 A';
  const [value, prefix] = fmt.prefixed(a);
  return fmt.sig(value, figures) + ' ' + prefix + 'A';
}

export function displayOhms(r, figures = 4) {
  if (r === null || r === undefined || Number.isNaN(r) || !Number.isFinite(r)) return fmt.ABSENT;
  if (r === 0) return '0 Ω';
  const magnitude = Math.abs(r);
  let chosen = OHM_PREFIXES[0];
  for (const step of OHM_PREFIXES) if (magnitude >= step[0]) chosen = step;
  return fmt.sig(r / chosen[0], figures) + ' ' + chosen[1] + 'Ω';
}

/** `sourcing 0.000000 V` — what was asked for, beside what came back. */
export function sourceLine(setup) {
  if (!setup) return fmt.ABSENT;
  const level = setup.unit === 'V' ? displayVolts(setup.level) : displayAmps(setup.level);
  return `sourcing ${level}`;
}

// -- the DOM -----------------------------------------------------------------
//
// Three regions, each rebuilt on a key of its own (`dom.keyed`), because the
// display moves every interval and the controls hold a caret. One key over the
// whole panel would take the field out from under whoever is typing in it once
// a second — the trap `lib/power.js` documents, where a key carrying the
// reading count tore the `⋯` menu down five times a second.
//
// So: **nothing that moves with a reading may be in the controls' key.** Not
// the reading, not the monitor's counters. What the operator wants to know
// about those (is it delivering? is the source clamping?) is in the display,
// where it belongs and where a rebuild costs nothing.

/** The keys the three regions are rebuilt on. Pure, and tested. */
export function panelKeys(model) {
  const r = model.reading;
  return {
    display: JSON.stringify([
      model.available, model.applied, model.output, displayWhy(model),
      model.setup && [model.setup.unit, model.setup.level, model.setup.four_wire,
                      model.setup.terminals],
      r && [r.volts, r.amps, r.ohms, r.compliance, r.stale]]),
    // `monitor.running` and its interval, never its counters: `readings` moves
    // every tick and `skipped` moves every tick the output is off, and either
    // in this key is a field that cannot be typed into.
    controls: JSON.stringify([
      model.available, model.setup, model.output, model.busy, model.inferred,
      model.monitor.running, model.monitor.interval_s]),
    notes: JSON.stringify(model.notes.map((n) => [n.key, n.level, n.text])),
  };
}

/**
 * The panel as a component: `{el, update(model)}`.
 *
 * `ctx` is `{source(patch), output(on), read(), refresh(seconds | null)}` —
 * every one an action on the bench the moment it lands, which is why every
 * control here is in the switch family (`ui-rules` §14) and none is in the
 * settings family the bench cards use. The callbacks take what was clicked
 * and not a value derived when the panel was drawn, so a stale model can at
 * worst re-send a patch the service already holds.
 */
export function createSmuPanel(ctx) {
  const displayEl = h('div.smu-display-wrap');
  const controlsEl = h('div.smu-controls');
  const notesEl = h('div.smu-notes');
  const bodyEl = h('div.cb', displayEl, controlsEl, notesEl);
  const el = h('div.card.mod.smu',
    h('div.ch',
      h('span.cn', 'Keithley 2400'),
      h('span.cs', 'SourceMeter · this panel touches no other instrument')),
    bodyEl);

  function update(model) {
    el.classList.toggle('inferred', Boolean(model.inferred));
    if (!model.available) {
      keyed(displayEl, 'none:' + model.unavailable,
        () => h('p.absent', { text: model.unavailable }));
      keyed(controlsEl, 'none', () => []);
      keyed(notesEl, 'none', () => []);
      return;
    }
    const keys = panelKeys(model);
    keyed(displayEl, keys.display, () => display(model));
    keyed(controlsEl, keys.controls, () => [
      h('div.smu-switches', model.switches.map((row) => switchRow(row, ctx))),
      h('div.smu-boxes', model.fields.map((field) => fieldBox(field, model, ctx))),
      refreshRow(model, ctx),
    ]);
    keyed(notesEl, keys.notes, () => model.notes.map((note) =>
      h('p', { class: 'note ' + note.level, text: note.text })));
  }

  return { el, update };
}

function display(model) {
  const r = model.reading;
  const sourcesVolts = Boolean(model.setup && model.setup.unit === 'V');
  // **Both numbers are measurements**, whichever is being sourced: the panel
  // senses V and I together and what it shows for either is what the
  // instrument read, never the setpoint. So neither cell is labelled
  // `sourced` — the accent on the quantity's letter says which one the source
  // is holding (`ui-rules` §3: the accent identifies the driven quantity) and
  // `sourcing 0 A` under it says what it is holding it at. A cell labelled
  // `sourced` would read as the setpoint, and the difference between the two
  // is the whole of a compliance.
  return h('div.smu-display', { class: r && r.stale ? 'stale' : '' },
    h('div.smu-cells',
      cell('V', displayVolts(r ? r.volts : null), 'measured', sourcesVolts),
      cell('I', displayAmps(r ? r.amps : null), 'measured', !sourcesVolts),
      cell('R', displayOhms(r ? r.ohms : null), 'V / I', false)),
    h('div.smu-annunciators',
      h('span.smu-source-line', { text: sourceLine(model.setup) }),
      r && r.compliance
        ? h('span.ann.crit', { title: 'the source is at its limit, so the reading is '
            + 'the limit and not the device\'s answer', text: 'Cmpl' })
        : null,
      model.setup && model.setup.four_wire ? h('span.ann', { text: '4W' }) : null,
      model.setup && model.setup.terminals === 'REAR' ? h('span.ann', { text: 'REAR' }) : null,
      r && r.stale
        ? h('span.ann.warn', { title: 'the monitor has not delivered for three of its '
            + 'intervals — a run has the GPIB', text: 'stale' })
        : null,
      r ? null : h('span.muted', { text: displayWhy(model) })));
}

/**
 * Why the display is dashes. Always one of three, and never silence: a bench
 * display showing nothing with no reason beside it is the one an operator
 * reads as a broken instrument.
 */
export function displayWhy(model) {
  if (!model.applied) return 'not on the panel';
  if (model.output !== true) return 'output off';
  return 'nothing read yet';
}

function cell(name, text, role, driven) {
  return h('div.smu-cell',
    h('span.smu-q', { class: driven ? 'driven' : '',
                      title: driven ? 'the quantity the source is holding' : '',
                      text: name }),
    h('span.num.smu-v', { text }),
    h('span.smu-role', { text: role }));
}

function switchRow(row, ctx) {
  const disabled = row.busy || Boolean(row.locked);
  return h('div.irow', { class: row.alert ? 'live' : '' },
    h('span.n', { text: row.label }),
    h('span.sw', { role: 'group', 'aria-label': row.label },
      row.positions.map((p) => h('button.p', {
        type: 'button',
        class: (p.on ? 'on' : '') + (row.alert && p.on ? ' alert' : ''),
        'aria-pressed': p.on ? 'true' : 'false',
        disabled: disabled || null,
        title: row.busy ? 'a run holds the bench; nothing here acts until it ends'
                        : (row.locked || `${row.label} → ${p.label}`),
        onclick: () => {
          if (p.on) return;
          if (row.key === 'output') ctx.output(p.id);
          else ctx.source({ [row.key]: p.id });
        },
      }, p.label))));
}

function fieldBox(field, model, ctx) {
  const ceiling = field.limit === null || field.limit === undefined
    ? '' : `\n\nThis bench's ceiling is ${field.limit} ${field.unit} (rig.toml).`;
  return h('label.smu-field', { class: model.busy ? 'dim' : '' },
    h('span.n', { text: field.label }),
    h('input.v', {
      type: 'text',
      inputmode: 'decimal',
      'aria-label': field.label,
      title: field.doc + ceiling,
      value: field.text,
      placeholder: field.placeholder || '',
      disabled: model.busy || null,
      // On commit, not on every keystroke: every one of these is a GPIB write.
      onchange: (e) => ctx.source({ [field.name]: e.target.value.trim() }),
    }),
    h('i', { text: field.unit }));
}

function refreshRow(model, ctx) {
  const { running, interval_s: interval } = model.monitor;
  const options = [{ id: null, label: 'off' },
                   ...REFRESH_STEPS.map((s) => ({ id: s, label: `${s} s` }))];
  return h('div.irow.smu-refresh',
    h('span.n', { text: 'display' }),
    h('span.sw', { role: 'group', 'aria-label': 'display refresh' },
      options.map((option) => {
        const on = option.id === null ? !running : (running && interval === option.id);
        return h('button.p', {
          type: 'button',
          class: on ? 'on' : '',
          'aria-pressed': on ? 'true' : 'false',
          disabled: model.busy || null,
          title: option.id === null
            ? 'stop the service reading the SourceMeter. The output is left where it is '
              + '— a display is not a source.'
            : `the service reads the SourceMeter every ${option.id} s, whether or not `
              + 'this tab is open',
          onclick: () => { if (!on) ctx.refresh(option.id); },
        }, option.label);
      })),
    h('button.p.smu-once', {
      type: 'button',
      disabled: model.busy || model.output !== true || null,
      title: model.output === true
        ? 'one reading now, journaled as a by-hand action'
        : 'the output is off: there is nothing to read',
      onclick: () => ctx.read(),
    }, 'read once'));
}
