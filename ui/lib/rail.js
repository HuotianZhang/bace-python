// The rail and strip appear in every view. Inferred state differs visibly from
// read-back because an active run overlays event implications rather than querying
// instruments (`docs/service-contract.md` §4). The relay identifies the device
// circuit and gets interlock treatment (`docs/ui-rules.md` §3). Pure `railModel`
// supports fixture tests. Service provenance, expected state, severity, remedies and
// V_oc source stay authoritative; missing readings stay absent.

import { h, fill, keyed } from './dom.js';
import { icon } from './icons.js';
import * as fmt from './format.js';

/** The relay diagram orders its three circuit nodes from left to right. */
export const RELAY_NODES = ['2400', 'device', 'amp'];

/**
 * Temperature becomes stale after three monitor intervals, tolerating one missed
 * tick. The 331 monitor skips reads while the worker holds the bus. Long pipelines
 * therefore need `skipped` to explain reading age rather than imply current
 * temperature (`docs/service-contract.md` §8).
 */
const TEMPERATURE_STALE_TICKS = 3;

/** Which chain checks speak about which rail cell. The chain owns `expected`. */
const CHAIN_CELL = {
  led_polarity: 'led',
  bias_arm: 'bias',
  bias_arm_slope: 'bias',
  bias_polarity: 'bias',
};

/**
 * The model supplies eight live values (`docs/ui-rules.md` §1). Alert is reserved for
 * live bias, warning for incorrect chain state or unavailable monitoring, off for
 * disabled output and ordinary ink otherwise (§3).
 */
export function railModel(state) {
  const bench = state.bench || {};
  const instruments = bench.instruments || {};
  const session = state.session || {};
  const chain = chainByKey(bench);
  const inferred = new Set(bench.inferred || []);
  const is = (name) => inferred.has(name) || (instruments[name] || {}).how === 'inferred';

  return [
    relayCell(instruments.relay, is('relay')),
    biasCell(instruments.bias, chain, is('bias')),
    smuCell(instruments.smu, is('smu')),
    shutterCell(instruments.shutter, is('shutter')),
    ledCell(instruments.led, chain, is('led')),
    vocCell(instruments.voc),
    powerCell(instruments.power),
    temperatureCell(temperatureNow(state), session),
  ];
}

/**
 * Temperature readings overlay the snapshot so the rail follows monitoring between
 * HTTP fetches. `wired` never changes through this overlay: operator values also
 * arrive as `TemperatureRead`, and readings cannot prove a controller exists. Unwired
 * benches retain typed provenance. Age compares service frame and reading timestamps
 * to avoid browser-clock skew. Without frames, the clock does not advance, so an idle
 * stream alone cannot imply staleness.
 */
export function temperatureNow(state) {
  const bench = state.bench || {};
  const block = (bench.instruments || {}).temperature || null;
  const live = state.temperature || null;
  const monitor = (state.monitors || []).find((m) => m.name === 'temperature') || null;
  let t = block;
  const readAt = bench.read_at;
  if (t && t.wired && live && (!(readAt > 0) || live.ts >= readAt)) {
    t = { ...t, kelvin: live.kelvin, in_band: live.in_band, source: live.source, read_at: live.ts };
    if (live.setpoint_k !== null && live.setpoint_k !== undefined) t.setpoint_k = live.setpoint_k;
  }
  // Either active `monitors` state or the snapshot's `monitor` flag establishes
  // monitoring.
  const running = Boolean((monitor && monitor.running) || (t && t.monitor));
  const interval = monitor ? monitor.interval_s : null;
  const at = t ? t.read_at : null;
  const now = state.lastFrame ? state.lastFrame.ts : null;
  const stale = Boolean(running && interval > 0 && at > 0 && now > 0
    && now - at > TEMPERATURE_STALE_TICKS * interval);
  return { ...(t || {}), running, stale, interval,
           skipped: monitor ? monitor.skipped : null };
}

function chainByKey(bench) {
  const items = (bench.chain && bench.chain.items) || [];
  return Object.fromEntries(items.map((item) => [item.key, item]));
}

/** The worst level among the chain checks that speak about one cell. */
function chainLevel(chain, cell) {
  let worst = null;
  for (const [key, item] of Object.entries(chain)) {
    if (CHAIN_CELL[key] !== cell) continue;
    if (item.level === 'crit') return 'crit';
    if (item.level === 'warn') worst = 'warn';
  }
  return worst;
}

/**
 * The relay model identifies the physical circuit containing the device. The diagram
 * distinguishes source-meter, amplifier and neutral rather than numeric levels.
 */
function relayCell(relay, inferred) {
  const position = (relay && relay.position) || 'unknown';
  const how = relay && relay.how;
  // Neutral requires an explicit service answer. Missing snapshots and unreadable
  // routers remain absent states.
  const answered = Boolean(relay) && how !== 'unavailable';
  const value = { amplifier: 'amplifier', sourcemeter: 'Keithley 2400' }[position]
    || (answered ? 'neutral' : fmt.ABSENT);
  return {
    key: 'relay', label: 'Relay', value,
    sub: how === 'cached' ? 'cached' : how === 'unavailable' ? 'router not answering' : null,
    level: how === 'unavailable' ? 'warn' : null,
    inferred, how, position,
    // Which links are made: the device is always the middle node; the closed
    // side is the instrument it is wired to.
    links: [position === 'sourcemeter', position === 'amplifier'],
  };
}

/**
 * Live bias is the rail alert: `output: true` means the 81150A drives the device.
 * Other cells must not compete with it (`docs/ui-rules.md` §3).
 */
function biasCell(bias, chain, inferred) {
  const b = bias || {};
  const live = b.output === true;
  // A bias that is off but armed wrong is still a warning: the arming is what
  // decides whether the collection pulse lands where the recipe says.
  const level = live ? 'alert' : chainLevel(chain, 'bias') || (b.output === false ? 'off' : null);
  return {
    key: 'bias', label: 'Bias',
    value: live ? 'LIVE' : b.output === false ? 'off' : fmt.ABSENT,
    sub: live ? pulseLevels(b) : armText(b, chain),
    level, inferred, how: b.how,
  };
}

function pulseLevels(b) {
  if (b.high_v === null || b.high_v === undefined) return null;
  const low = b.low_v === null || b.low_v === undefined ? fmt.ABSENT : fmt.volts(b.low_v, { decimals: 2, unit: false });
  return `${fmt.volts(b.high_v, { decimals: 4, unit: false })} → ${low} V`;
}

/**
 * Arming source and slope jointly determine whether Sync arms on the light-off edge.
 * The rail combines their mark; the strip lists them separately.
 */
function armText(b, chain) {
  const checks = [chain.bias_arm, chain.bias_arm_slope].filter(Boolean);
  if (!checks.length) return b.arm_source ? `armed ${b.arm_source}` : null;
  const mark = checks.every((item) => item.level === 'ok') ? '✓' : '⚠';
  return `armed ${b.arm_source || fmt.ABSENT} ${mark}`;
}

/**
 * Compliance sits beside the ceiling (`docs/ui-rules.md` §6). `compliance` is the run
 * setting; `ceiling` is the `rig.toml` limit. Without a run setting, ≤ marks the
 * limit rather than implying selection. A 2400 can source 1 A into a small cell.
 */
function smuCell(smu, inferred) {
  const s = smu || {};
  const compliance = s.compliance || {};
  const ceiling = s.ceiling || {};
  // Current and voltage compliance resolve independently. Showing only a selected
  // half would hide the ceiling enforced for the other.
  const current = limit(compliance.current_a, ceiling.current_a, fmt.amps);
  const voltage = limit(compliance.voltage_v, ceiling.voltage_v,
    (v) => fmt.volts(v, { decimals: 1 }));
  const both = current && voltage;
  const text = both && current.ceiling && voltage.ceiling
    ? `≤ ${current.text} / ${voltage.text}`            // one ≤ covers both
    : [current, voltage].filter(Boolean)
      .map((part) => (part.ceiling ? '≤ ' : '') + part.text).join(' / ');
  return {
    key: 'smu', label: 'Keithley',
    value: s.output === true ? 'ON' : s.output === false ? 'off' : fmt.ABSENT,
    sub: text ? `cc ${text}` : null,
    level: s.output === true ? null : 'off',
    inferred, how: s.how,
  };
}

/**
 * The value uses the run setting when present, otherwise an explicitly marked
 * `rig.toml` ceiling.
 */
function limit(chosen, ceiling, render) {
  if (chosen !== null && chosen !== undefined) return { text: render(chosen), ceiling: false };
  if (ceiling !== null && ceiling !== undefined) return { text: render(ceiling), ceiling: true };
  return null;
}

function shutterCell(shutter, inferred) {
  const open = shutter && shutter.open;
  return {
    key: 'shutter', label: 'Shutter',
    value: open === true ? 'open' : open === false ? 'shut' : fmt.ABSENT,
    sub: (shutter && shutter.how) === 'cached' ? 'cached' : null,
    level: null, inferred, how: shutter && shutter.how,
  };
}

/**
 * LED NORM makes rising Sync coincide with light on, putting extraction during
 * illumination. The rail marks the warning; the strip offers its remedy.
 */
function ledCell(led, chain, inferred) {
  const l = led || {};
  const polarity = chain.led_polarity;
  const on = l.output === true;
  const level = chainLevel(chain, 'led') || (on ? null : l.output === false ? 'off' : null);
  const parts = [];
  if (on && l.mode) parts.push(String(l.mode).toLowerCase());
  if (l.polarity) parts.push(`POL ${l.polarity}${polarity ? (polarity.level === 'ok' ? ' ✓' : ' ⚠') : ''}`);
  // The low level must stay below LED turn-on so the dark phase is dark
  // (`docs/ui-rules.md` §10). Frequency belongs in the timing diagram; this cell has
  // one line.
  if (l.low_v !== null && l.low_v !== undefined) parts.push(`low ${fmt.volts(l.low_v, { decimals: 3, unit: false })} V`);
  return {
    key: 'led', label: 'LED',
    value: on && l.high_v !== null && l.high_v !== undefined
      ? fmt.volts(l.high_v, { decimals: 3, unit: false }) + ' V'
      : on ? 'on' : l.output === false ? 'off' : fmt.ABSENT,
    sub: parts.join(' · ') || null,
    level, inferred, how: l.how,
  };
}

/**
 * V_oc includes its source measurement and LED level. A value at another illumination
 * cannot replace it (`docs/ui-rules.md` §6).
 */
function vocCell(voc) {
  const v = voc || {};
  if (!voc) return { key: 'voc', label: 'Voc', value: fmt.ABSENT, sub: 'no read-back yet', level: null, inferred: false };
  if (v.value === null || v.value === undefined) {
    return { key: 'voc', label: 'Voc', value: fmt.ABSENT, sub: 'none this session', level: 'off', inferred: false };
  }
  const from = v.from || {};
  const parts = [];
  if (v.led_v !== null && v.led_v !== undefined) parts.push(`@ ${fmt.volts(v.led_v, { decimals: 3, unit: false })} V`);
  if (from.how) parts.push(from.how);
  if (from.ts) parts.push(fmt.clock(from.ts));
  return { key: 'voc', label: 'Voc', value: fmt.volts(v.value), sub: parts.join(' · ') || null, level: null, inferred: false };
}

/**
 * Power uses watts at the meter because beam-splitter and area factors are
 * unavailable (`docs/ui-rules.md` §2). An unresponsive meter is a warning with an
 * explicit empty state (§9).
 */
function powerCell(power) {
  const p = power || {};
  // Before the first bench response, meter state is unknown. Calling it unresponsive
  // would assert a query failure without evidence.
  if (!power) return { key: 'power', label: 'Power', value: fmt.ABSENT, sub: 'no read-back yet', level: null, inferred: false };
  if (!p.available) {
    return { key: 'power', label: 'Power', value: fmt.ABSENT,
      sub: p.reason || 'meter not answering', level: 'warn', inferred: false };
  }
  const parts = [];
  if (p.wavelength_nm) parts.push(`${p.wavelength_nm} nm`);
  // Averaging reports mean pulsed power, half the DC level at 50 % duty. Without it
  // the meter samples one instant of the waveform (`docs/service-contract.md` §8).
  if (p.averaged === true) parts.push('avg');
  if (p.averaged === false) parts.push('not averaged');
  if (p.monitor) parts.push('monitored');
  return {
    key: 'power', label: 'Power', value: fmt.intensity(p.watts),
    sub: parts.join(' · ') || null,
    level: p.trustworthy === false ? 'warn' : null,
    inferred: false,
  };
}

/**
 * Wired temperature is controller read-back; unwired session temperature is typed
 * filename provenance, not measurement. Monitoring supplies cadence and age. Stale
 * readings cannot retain in-band verdicts because the present state is unknown during
 * skipped reads.
 */
function temperatureCell(temperature, session) {
  const t = temperature || {};
  if (t.wired) {
    const parts = [];
    if (t.setpoint_k !== null && t.setpoint_k !== undefined) parts.push(`set ${fmt.kelvin(t.setpoint_k, { unit: false })}`);
    if (t.source) parts.push(t.source);
    if (t.ramping) parts.push('ramping');
    if (t.stale) parts.push(t.skipped ? `stale · ${fmt.plural(t.skipped, 'tick')} skipped` : 'stale');
    // The sub-line shows cadence rather than a snapshot count. A frozen count beside
    // changing readings would misrepresent monitoring.
    else if (t.running) parts.push(t.interval > 0 ? `every ${+t.interval} s` : 'monitored');
    // Stopped or disabled monitoring leaves only the last read-back, so the operator
    // needs that limitation beside the value.
    else parts.push('read-back only');
    return {
      key: 'temperature', label: 'T', value: fmt.kelvin(t.kelvin),
      sub: parts.join(' · ') || null,
      level: t.stale ? null : t.in_band === false ? 'warn' : t.in_band === true ? 'ok' : null,
      inferred: false,
    };
  }
  const typed = (session.sample || {}).temperature_k;
  return {
    key: 'temperature', label: 'T',
    value: typed === null || typed === undefined ? fmt.ABSENT : fmt.kelvin(typed),
    sub: typed === null || typed === undefined ? 'not wired' : 'typed · not wired',
    level: 'typed',
    inferred: false,
  };
}

/**
 * The strip shows chain checks and service-supplied `fix` actions. Remedies require
 * operator clicks and never run automatically (`docs/ui-rules.md` §3).
 */
export function chainModel(state) {
  const bench = state.bench || {};
  const chain = bench.chain || null;
  const items = (chain && chain.items) || [];
  const busy = (state.benchState || 'idle') !== 'idle';
  return {
    read_at: chain && chain.read_at,
    ok: chain ? chain.ok : null,
    total: chain ? chain.total : null,
    // Bench actions use the worker. During a run, all except park return 409
    // (`docs/service-contract.md` §4), so controls explain their unavailability
    // before a click.
    busy,
    // Park cancels queued runs, so their count belongs in its visible confirmation
    // and render key. Queue changes must update an already armed label.
    queued: (state.queue || []).length,
    items: items.map((item) => ({
      ...item,
      // An `ok` check's `fix` names its satisfying action. Only failing checks need a
      // remedy button.
      action: item.level !== 'ok' && item.fix ? item.fix : null,
      actionLabel: item.fix ? fixLabel(item) : null,
    })),
  };
}

/**
 * LED polarity changes require LED output off; external arming requires bias output
 * off (`docs/service-contract.md` §4). A refusal naming a prerequisite gets its own
 * button. Each click performs one action; the strip never chains them.
 */
export const PREREQUISITE = {
  'set-33220a-pol-inv': 'led-off',
  'set-33220a-pol-norm': 'led-off',
  'arm-81150a-ext': 'bias-off',
};

function fixLabel(item) {
  if (item.expected && item.expected !== 'leave') return `set ${item.expected}`;
  return item.fix;
}

// -- the DOM ----------------------------------------------------------------

/**
 * The armed label states active-run abort and queue cancellation
 * (`docs/service-contract.md` §4). Queue loss must be visible before confirmation
 * rather than only on hover. The count appears only when nonzero.
 */
export function parkLabel(queued = 0) {
  return queued > 0
    ? `Abort the run, cancel ${fmt.plural(queued, 'queued run')}, and turn all off?`
    : 'Abort the run and turn all off?';
}

/**
 * Hover text expands the Park consequence. Queue precedence is mentioned only when a
 * queue exists.
 */
export function parkTitle(queued = 0) {
  return queued > 0
    ? `All off aborts the run and cancels ${fmt.plural(queued, 'queued run')}. `
      + 'The bench is safe now, not after the queue.'
    : 'All off aborts the run. The bench is safe now.';
}

/** The instrument behind each rail cell, for the name's tooltip. */
const CELL_MODEL = {
  relay: 'Relay: router between the Keithley 2400 and the amplifier, with interlock',
  bias: 'Bias: Agilent 81150A pulse generator',
  smu: 'Keithley 2400',
  shutter: 'Shutter: on the Deditec DIO',
  led: 'LED: Agilent 33220A function generator driving the light source',
  voc: 'Voc: open-circuit voltage in scope this session',
  power: 'Power: Newport 1918-C optical power meter',
  temperature: 'T: Lake Shore 331 temperature controller',
};

/**
 * The renderer keys the rail on its pure model. Rebuilding unchanged DOM on every
 * frame wastes work and drops text selection during copying.
 */
export function renderRail(el, state) {
  const model = railModel(state);
  keyed(el, JSON.stringify(model), () => model.map(cellEl));
}

/**
 * Each cell maps to its instrument glyph. Unknown keys use a dashed ring rather than
 * another instrument's mark.
 */
const CELL_ICON = {
  relay: 'relay',
  bias: 'bias',
  smu: 'smu',
  shutter: 'shutter',
  led: 'led',
  voc: 'voc',
  power: 'power',
  temperature: 'temp',
};

function cellEl(cell) {
  const classes = ['brc'];
  if (cell.level) classes.push('lv-' + cell.level);
  if (cell.inferred) classes.push('inferred');
  // The diagram identifies a connected circuit. Unlit can mean neutral or absent
  // read-back, so those states retain words.
  if (cell.key === 'relay' && cell.links && (cell.links[0] || cell.links[1])) classes.push('drawn');
  // A reading cell puts its label above its value, so a narrow cell keeps the
  // instrument's name whole.
  if (cell.key !== 'relay') classes.push('stacked');
  // A one-line rail opens details on hover and focus. `tabindex` provides keyboard
  // access; `title` also exposes text when the panel extends off-screen.
  return h('div', {
    class: classes.join(' '),
    tabindex: cell.sub ? '0' : null,
    title: cell.sub || null,
  },
    h('span.brl',
      icon(CELL_ICON[cell.key] || 'blank'),
      h('span', { text: cell.label, title: CELL_MODEL[cell.key] || null }),
      // Inference attaches to the label while the value remains numeric. A dashed
      // rule conveys it on one line; the hidden badge retains title text.
      cell.inferred ? h('span.tag-inferred', { title: 'Implied by the running step, not read back.', text: 'inferred' }) : null),
    h('span.brv', {
      text: cell.value,
      title: cell.inferred ? 'implied by the running step, not read back' : null,
    }),
    cell.key === 'relay' ? relayEl(cell) : null,
    cell.sub ? h('span.brs', { text: cell.sub }) : null);
}

/** `2400 —— device —— amp`: which circuit the device is actually in. */
function relayEl(cell) {
  const [left, right] = cell.links;
  return h('div.relay',
    h('span', { class: 'rn' + (left ? ' on' : ''), text: RELAY_NODES[0] }),
    h('span', { class: 'rw' + (left ? ' on' : '') }),
    h('span', { class: 'rn' + (left || right ? ' on' : ''), text: RELAY_NODES[1] }),
    h('span', { class: 'rw' + (right ? ' on' : '') }),
    h('span', { class: 'rn' + (right ? ' on' : ''), text: RELAY_NODES[2] }));
}

/**
 * The strip renders explicit `onFix(name)` and `onPark()` actions. It never acts
 * automatically; `onDismiss()` only clears the last action message.
 */
export function renderChainStrip(el, state, { onFix, onPark, onDismiss, status, parkArmed } = {}) {
  const model = chainModel(state);
  const rig = (state.bench && state.bench.rig && state.bench.rig.values) || {};
  // The model plus the two things the strip holds that the store does not: the
  // sentence the last action came back with, and whether Park is armed. Both
  // are part of what is drawn, so both are part of the key.
  const key = JSON.stringify([model, rig, status, parkArmed]);
  keyed(el, key, () => [
    h('span.sk', { text: model.total === null ? 'Trigger' : `Trigger ${model.ok} / ${model.total}` }),
    model.items.map((item) => h('span', { class: 'st ' + (item.level === 'ok' ? '' : 'bad') },
      h('span', { text: `${item.label} ${item.value}` }),
      // Verdict icons match adjacent numbers in optical weight and inherit severity
      // through `currentColor` so interpretation constraints stay visible.
      icon(item.level === 'ok' ? 'ok' : 'warn', {
        cls: 'mark', size: 13, title: item.level === 'ok' ? 'reads as configured' : 'reads wrong',
      }),
      item.action
        ? h('button.btns', {
          disabled: model.busy,
          title: model.busy ? 'a run has the bench; the fix is a worker job' : item.text || '',
          onclick: () => onFix && onFix(item.action, item),
        }, item.actionLabel)
        : null)),
    h('span.spacer'),
    status ? h('span', { class: 'st status ' + (status.level || '') },
      h('span', { text: status.text }),
      // Refused Start returns 422 with actionable `error.checks`. A bare invalid
      // label cannot explain the remedy.
      (status.checks || []).length
        ? h('span.checks', (status.checks || [])
          .filter((c) => c.level === 'crit' || c.level === 'invalid')
          .map((c) => h('span', { class: 'ck ' + c.level, title: c.text, text: c.code })))
        : null,
      // A refusal that names a remedy gets a button for it, or the operator is
      // told what to do and given no way to do it.
      status.offer
        ? h('button.btns', { onclick: () => onFix && onFix(status.offer, { label: status.offer }) }, status.offer)
        : null,
      // The operator can dismiss a persistent message without another bench action.
      // No timer clears it because a refusal must remain available to read.
      h('button.stx', {
        title: 'clear this message',
        'aria-label': 'clear this message',
        onclick: () => onDismiss && onDismiss(),
      }, '✕')) : null,
    // Gain, R_s and attenuation multiply results without leaving evidence in their
    // values. Bench properties stay beside results (`docs/ui-rules.md` §6).
    h('span.st.quiet', { text: rigText(rig) }),
    h('button', {
      class: parkArmed ? 'btns armed' : 'btns',
      title: model.busy ? parkTitle(model.queued) : 'Outputs off, shutter shut, relay to the Keithley side.',
      onclick: () => onPark && onPark(),
    }, parkArmed ? parkLabel(model.queued) : 'All off'),
  ]);
}

function rigText(rig) {
  const parts = [];
  if (rig.sense_resistor_ohm) parts.push(`Resistor ${rig.sense_resistor_ohm} Ω`);
  if (rig.pulse_amp) parts.push(`Pulse Amp ×${rig.pulse_amp}`);
  if (rig.current_sign) parts.push(`sign ${rig.current_sign > 0 ? '+1' : '−1'}`);
  return parts.join(' · ');
}
