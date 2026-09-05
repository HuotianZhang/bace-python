// The pinned rail and the chain strip — `docs/ui-plan.md` M1.
//
// They come before the module cards because they are in *every* view, and
// because they carry the two hardest things `GET /bench` says:
//
//   * **`how: "inferred"` is not a read-back.** While a run holds the worker
//     the snapshot is the one Start took, with the running step's implications
//     overlaid (`service/live.py`): during a `bace` the bias being LIVE, the
//     relay being on the amplifier and the shutter being open are *inferred
//     from the run's own events*, not read off an instrument. A rail that
//     draws them identically is telling the operator something it does not
//     know.
//   * **The relay is the interlock**, not "info" (`docs/ui-rules.md` §3): the
//     two positions are physically different circuits, and it gets its own
//     treatment rather than a level colour.
//
// The model is separated from the DOM on purpose: `railModel` is a pure
// function of the store's state, so `ui/tests/rail.test.mjs` can hold every
// one of these rules down against a recorded `/bench` without a browser.
//
// Nothing here re-derives provenance (`ui-rules` §6). `how`, `inferred`, the
// chain's `expected`/`level`/`fix` and the V_oc's `from` are the service's
// answers; this file decides where they sit and what they look like, and
// invents no value of its own — an absent reading renders as an absence.

import { h, fill, keyed } from './dom.js';
import * as fmt from './format.js';

/** The relay's three nodes, left to right, as `design/BenchRail.dc.html` draws them. */
export const RELAY_NODES = ['2400', 'device', 'amp'];

/**
 * How many monitor intervals may pass with no reading before the temperature
 * on the rail stops being presented as current. The 331's monitor reads only
 * while it can hold the worker's bus lock (`service/monitors.py`), so a
 * pipeline run holding the bus for an hour leaves the card's reading exactly
 * that old — the service counts those ticks as `skipped`, and the contract
 * (§8) says to render *why* rather than an old number as if it were now.
 * Three, so one missed tick is not an announcement.
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
 * The eight live values of `docs/ui-rules.md` §1, as a plain object per cell:
 * `{key, label, value, sub, level, inferred, how}`.
 *
 * `level` is the loudness §3 gives: `alert` for a live bias output and nothing
 * else on the rail, `warn` for a chain state that reads wrong or a monitor
 * that will not answer, `off` for an output that is off, `null` for ink.
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
 * The temperature the rail draws: the read-back's block with the newest
 * `TemperatureRead` the stream carried laid over it, plus what the monitor
 * is doing.
 *
 * The overlay is the one the service already makes on its own snapshot
 * (`session._temperature_block`), made again here because `/bench` is fetched
 * at boot and after an action while readings arrive every few seconds in
 * between. Without it the rail would show the boot read-back all day — the
 * monitor's readings would reach the journal and the chart and not the one
 * number the operator looks at.
 *
 * `wired` is never overlaid, and an unwired bench is never overlaid at all: a
 * reading does not make the 331 wired, and the value an operator types at a
 * pause arrives as a `TemperatureRead` too (`source: "operator"`, service
 * `temperature.py`) — on a bench with no controller that number is a typed
 * one and the cell has to keep saying so.
 *
 * Age is measured on the service's clock and never on the browser's: the
 * newest frame's `ts` against the newest reading's, both off the same stream.
 * So a console on another machine, or one whose clock is minutes out, reads
 * what the lab PC reads, and an idle bench — no frames at all, so no clock
 * moving — never drifts into looking stale for want of a run.
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
  // `monitors` is the service's list of what is *running*; the block's
  // `monitor` is the same answer taken at the read-back. Either says yes.
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
 * The interlock. Not a level: the value is *which circuit the device is in*,
 * and the three-node diagram is the whole point — "amplifier" and
 * "sourcemeter" are not two settings of one thing.
 */
function relayCell(relay, inferred) {
  const position = (relay && relay.position) || 'unknown';
  const how = relay && relay.how;
  // "neutral" is a statement about the interlock — the device is in neither
  // circuit — so it is said only when the service answered. No snapshot, or a
  // router that would not answer, is an absence.
  const answered = Boolean(relay) && how !== 'unavailable';
  const value = { amplifier: 'amplifier', sourcemeter: 'Keithley 2400' }[position]
    || (answered ? 'neutral' : fmt.ABSENT);
  return {
    key: 'relay', label: 'relay · interlock', value,
    sub: how === 'cached' ? 'cached, not re-read' : how === 'unavailable' ? 'no router answering' : null,
    level: how === 'unavailable' ? 'warn' : null,
    inferred, how, position,
    // Which links are made: the device is always the middle node; the closed
    // side is the instrument it is wired to.
    links: [position === 'sourcemeter', position === 'amplifier'],
  };
}

/**
 * The one alert on the rail. `output: true` means the 81150A is driving the
 * device — §3 keeps that intensity for exactly this, a `crit` and a saturation
 * verdict, and nothing else on this row may compete with it.
 */
function biasCell(bias, chain, inferred) {
  const b = bias || {};
  const live = b.output === true;
  // A bias that is off but armed wrong is still a warning: the arming is what
  // decides whether the collection pulse lands where the recipe says.
  const level = live ? 'alert' : chainLevel(chain, 'bias') || (b.output === false ? 'off' : null);
  return {
    key: 'bias', label: 'bias 81150A',
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
 * What the chain says about the arming. The source and the slope are two
 * checks and one fact — armed from the 33220A Sync, on the edge that is light
 * off — so they carry one mark between them; the strip below has them apart.
 */
function armText(b, chain) {
  const checks = [chain.bias_arm, chain.bias_arm_slope].filter(Boolean);
  if (!checks.length) return b.arm_source ? `armed ${b.arm_source}` : null;
  const mark = checks.every((item) => item.level === 'ok') ? '✓' : '⚠';
  return `armed ${b.arm_source || fmt.ABSENT} ${mark}`;
}

/**
 * `ui-rules` §6: the compliance ceiling belongs beside the value. `compliance`
 * is what the run in force chose, `ceiling` what `rig.toml` will not go above,
 * and a 2400 will happily push 1 A into a small cell — so when no run has
 * chosen one, the ceiling is shown as a ceiling (`≤`) rather than as a setting.
 */
function smuCell(smu, inferred) {
  const s = smu || {};
  const compliance = s.compliance || {};
  const ceiling = s.ceiling || {};
  // Each half is answered separately: a run may choose a current compliance
  // and leave the voltage to the ceiling, and showing only the half it chose
  // would drop the limit `rig.toml` is actually enforcing.
  const current = limit(compliance.current_a, ceiling.current_a, fmt.amps);
  const voltage = limit(compliance.voltage_v, ceiling.voltage_v,
    (v) => fmt.volts(v, { decimals: 1 }));
  const both = current && voltage;
  const text = both && current.ceiling && voltage.ceiling
    ? `≤ ${current.text} / ${voltage.text}`            // one ≤ covers both
    : [current, voltage].filter(Boolean)
      .map((part) => (part.ceiling ? '≤ ' : '') + part.text).join(' / ');
  return {
    key: 'smu', label: 'SMU 2400',
    value: s.output === true ? 'ON' : s.output === false ? 'off' : fmt.ABSENT,
    sub: text ? `cc ${text}` : null,
    level: s.output === true ? null : 'off',
    inferred, how: s.how,
  };
}

/** The run's choice if it made one, else `rig.toml`'s ceiling, marked as one. */
function limit(chosen, ceiling, render) {
  if (chosen !== null && chosen !== undefined) return { text: render(chosen), ceiling: false };
  if (ceiling !== null && ceiling !== undefined) return { text: render(ceiling), ceiling: true };
  return null;
}

function shutterCell(shutter, inferred) {
  const open = shutter && shutter.open;
  return {
    key: 'shutter', label: 'shutter',
    value: open === true ? 'open' : open === false ? 'shut' : fmt.ABSENT,
    sub: (shutter && shutter.how) === 'cached' ? 'cached, not re-read' : null,
    level: null, inferred, how: shutter && shutter.how,
  };
}

/**
 * The LED carries the chain's loudest routine warning: `POL NORM` makes the
 * Sync's rising edge mean light ON, so extraction would happen *during*
 * illumination. The rail says it where the operator is already looking; the
 * strip below carries the one-click fix.
 */
function ledCell(led, chain, inferred) {
  const l = led || {};
  const polarity = chain.led_polarity;
  const on = l.output === true;
  const level = chainLevel(chain, 'led') || (on ? null : l.output === false ? 'off' : null);
  const parts = [];
  if (on && l.mode) parts.push(String(l.mode).toLowerCase());
  if (l.polarity) parts.push(`POL ${l.polarity}${polarity ? (polarity.level === 'ok' ? ' ✓' : ' ⚠') : ''}`);
  // The low level, not the frequency: "the LED low level must sit below
  // turn-on, so the dark half of the cycle really is dark" (`ui-rules` §10).
  // The frequency has a home in M3's timing diagram; this cell has one line.
  if (l.low_v !== null && l.low_v !== undefined) parts.push(`low ${fmt.volts(l.low_v, { decimals: 3, unit: false })} V`);
  return {
    key: 'led', label: 'LED 33220A',
    value: on && l.high_v !== null && l.high_v !== undefined
      ? fmt.volts(l.high_v, { decimals: 3, unit: false }) + ' V'
      : on ? 'on' : l.output === false ? 'off' : fmt.ABSENT,
    sub: parts.join(' · ') || null,
    level, inferred, how: l.how,
  };
}

/**
 * `ui-rules` §6: *which* measurement supplied the V_oc, and at what LED level.
 * A V_oc from a different illumination is worse than no V_oc, so the source
 * and the level are not a detail — they are half the value.
 */
function vocCell(voc) {
  const v = voc || {};
  if (!voc) return { key: 'voc', label: 'V_oc', value: fmt.ABSENT, sub: 'no read-back yet', level: null, inferred: false };
  if (v.value === null || v.value === undefined) {
    return { key: 'voc', label: 'V_oc', value: fmt.ABSENT, sub: 'none this session', level: 'off', inferred: false };
  }
  const from = v.from || {};
  const parts = [];
  if (v.led_v !== null && v.led_v !== undefined) parts.push(`@ ${fmt.volts(v.led_v, { decimals: 3, unit: false })} V`);
  if (from.how) parts.push(from.how);
  if (from.ts) parts.push(fmt.clock(from.ts));
  return { key: 'voc', label: 'V_oc', value: fmt.volts(v.value), sub: parts.join(' · ') || null, level: null, inferred: false };
}

/**
 * Watts at the meter, never mW/cm²: the beam-splitter and area factors are not
 * recoverable here, and an irradiance would be a number that looks calibrated
 * and is not (`ui-rules` §2). A console that will not answer is a warning, and
 * one of the empty states §9 names.
 */
function powerCell(power) {
  const p = power || {};
  // No snapshot at all is not a meter that will not answer: before the first
  // `GET /bench` the console knows nothing, and saying "not answering" would
  // be a claim about a console it has not spoken to.
  if (!power) return { key: 'power', label: 'optical power', value: fmt.ABSENT, sub: 'no read-back yet', level: null, inferred: false };
  if (!p.available) {
    return { key: 'power', label: 'optical power', value: fmt.ABSENT,
      sub: p.reason || 'console not answering', level: 'warn', inferred: false };
  }
  const parts = [];
  if (p.wavelength_nm) parts.push(`${p.wavelength_nm} nm`);
  // The meter's averaging is what makes a pulsed LED read as a power (its
  // mean, half the DC level at 50 % duty) rather than one instant of the
  // square wave; a meter that is *not* averaging is worth a word.
  if (p.averaged === true) parts.push('avg');
  if (p.averaged === false) parts.push('not averaged');
  if (p.monitor) parts.push('monitored');
  return {
    key: 'power', label: 'optical power', value: fmt.intensity(p.watts),
    sub: parts.join(' · ') || null,
    level: p.trustworthy === false ? 'warn' : null,
    inferred: false,
  };
}

/**
 * Wired, the controller's reading and whether it is in band. Not wired, the
 * number is the one *typed* into the session — it goes into every folder name,
 * and nobody measured it. That is provenance, not decoration, so it reads as
 * a claim rather than as a reading.
 *
 * The 331 is watched from the moment the service is up (the session's
 * `temperature_monitor_s`), so on a wired bench this cell is a live reading
 * and the sub-line says at what cadence. Two things it must not do with that:
 * call a reading current when the monitor has been unable to read for several
 * intervals — a run holding the bus is the ordinary way that happens — and
 * keep the band verdict of one that is. `in_band` is a statement about *now*,
 * so a stale reading has no level at all.
 */
function temperatureCell(temperature, session) {
  const t = temperature || {};
  if (t.wired) {
    const parts = [];
    if (t.setpoint_k !== null && t.setpoint_k !== undefined) parts.push(`set ${fmt.kelvin(t.setpoint_k, { unit: false })}`);
    if (t.source) parts.push(t.source);
    if (t.ramping) parts.push('ramping');
    if (t.stale) parts.push(t.skipped ? `stale · ${fmt.plural(t.skipped, 'tick')} skipped` : 'stale');
    // The cadence and not the count of readings: both are the service's, but
    // `readings` is only as new as the last snapshot, and a counter standing
    // still beside a number that moves says the wrong thing about which of
    // them is live. `every 5 s` is what the operator is actually asking.
    else if (t.running) parts.push(t.interval > 0 ? `every ${+t.interval} s` : 'monitored');
    // Not the default: somebody stopped the monitor, or the service was
    // started with --no-temperature-monitor. The number is then as old as
    // the last read-back, which the operator has to be told.
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
 * The chain strip: the four trigger-chain checks, each with the bench action
 * that fixes it. `fix` is the service's own name for that action — the strip
 * offers it, and never performs one by itself (`ui-rules` §3: warn states
 * evidence and never blocks; the operator clicks).
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
    // A bench action is a worker job, and the worker is running the run: every
    // action except `park` answers 409 while one is active. A button that
    // offered itself and then failed would be worse than one that says why.
    busy,
    // Runs waiting for the worker. Park takes them with it (#53), so the
    // count is part of what its confirmation has to say — and, because the
    // model is the strip's render key, part of what redraws the label when a
    // run is queued while Park is already armed.
    queued: (state.queue || []).length,
    items: items.map((item) => ({
      ...item,
      // `fix` on an `ok` item is the action that *made* it ok; offering it
      // again is noise. Only a check that reads wrong gets a button.
      action: item.level !== 'ok' && item.fix ? item.fix : null,
      actionLabel: item.fix ? fixLabel(item) : null,
    })),
  };
}

/**
 * What a refused fix needs first.
 *
 * The chain fixes are made with the output off — `set-33220a-pol-*` refuses
 * while the LED output is ON *as the instrument reports it*, and
 * `arm-81150a-ext` while the bias output is (contract §4). The service says so
 * in a sentence that names the remedy; without a button for it the operator
 * would be told what to do and given no way to do it.
 *
 * Each entry is one action doing one thing: the strip never chains them.
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
 * What an armed Park is about to do, in full.
 *
 * `park` on a busy bench is `worker.stop_runs`: it aborts the run *and
 * cancels every queued one*. The confirmation said "abort the run and park?"
 * — one run, singular — and named the queue only in a `title` nobody hovers
 * before the second click (#53). An operator who reached for Park to stop one
 * scan lost the rest of the night without being told which.
 *
 * The count only appears when there is one, so the ordinary case — stop this,
 * nothing behind it — keeps the shorter sentence.
 */
export function parkLabel(queued = 0) {
  return queued > 0
    ? `abort the run, cancel ${fmt.plural(queued, 'queued run')}, and park?`
    : 'abort the run and park?';
}

/**
 * The same thing at length, on hover. "not after the queue" is the point of
 * the sentence and it only has one when there is a queue to be ahead of.
 */
export function parkTitle(queued = 0) {
  return queued > 0
    ? `park aborts the run and cancels ${fmt.plural(queued, 'queued run')}`
      + ' — the bench is safe now, not after the queue'
    : 'park aborts the run — the bench is safe now';
}

/**
 * The rail, into a container. One cell per value, in the design's order.
 *
 * The model is the render key (`dom.keyed`): eight small objects, built on
 * every notify because that is cheap and pure, and put on the screen only when
 * one of them says something different from what is already there. A rail
 * rebuilt sixty times a second to draw the same eight values is not just
 * waste — it is the operator's selection dropped mid-copy.
 */
export function renderRail(el, state) {
  const model = railModel(state);
  keyed(el, JSON.stringify(model), () => model.map(cellEl));
}

function cellEl(cell) {
  const classes = ['brc'];
  if (cell.level) classes.push('lv-' + cell.level);
  if (cell.inferred) classes.push('inferred');
  return h('div', { class: classes.join(' ') },
    h('span.brl', { text: cell.label },
      // The inferred mark rides on the label, so the value keeps the register
      // of a number: this cell is what the run implies, not what was read.
      cell.inferred ? h('span.tag-inferred', { title: 'implied by the running step, not read back', text: 'inferred' }) : null),
    h('span.brv', { text: cell.value }),
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
 * The strip, into a container. `onFix(name)` and `onPark()` are the only two
 * things it can do to the bench, and both are explicit: nothing here touches
 * it on its own. `onDismiss()` touches nothing at all — it clears the sentence
 * the last action left.
 */
export function renderChainStrip(el, state, { onFix, onPark, onDismiss, status, parkArmed } = {}) {
  const model = chainModel(state);
  const rig = (state.bench && state.bench.rig && state.bench.rig.values) || {};
  // The model plus the two things the strip holds that the store does not: the
  // sentence the last action came back with, and whether Park is armed. Both
  // are part of what is drawn, so both are part of the key.
  const key = JSON.stringify([model, rig, status, parkArmed]);
  keyed(el, key, () => [
    h('span.sk', { text: model.total === null ? 'trigger chain' : `trigger chain ${model.ok} / ${model.total}` }),
    model.items.map((item) => h('span', { class: 'st ' + (item.level === 'ok' ? '' : 'bad') },
      h('span', { text: `${item.label} ${item.value}` }),
      h('span.mark', { text: item.level === 'ok' ? '✓' : '⚠' }),
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
      // A refused Start answers 422 with the checks that refused it, and the
      // list is the answer -- "invalid" alone tells the operator nothing they
      // can act on. `error.checks`, not `detail` (M1's finding).
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
      // A way to be rid of it. The strip is the console's one place for a
      // sentence, and a sentence has no clock on it: an `ok` from a queue
      // three hours ago and a refusal that was fixed twenty minutes ago both
      // sat here reading as news, and the operator could only replace them by
      // provoking another. Nothing dismisses itself — a refusal that vanished
      // on a timer is the failure mode this replaces, not a version of it.
      h('button.stx', {
        title: 'clear this message',
        'aria-label': 'clear this message',
        onclick: () => onDismiss && onDismiss(),
      }, '✕')) : null,
    // Bench properties, not per-run choices, and multiplicative: they leave no
    // trace in the data, so `ui-rules` §6 restates them beside it.
    h('span.st.quiet', { text: rigText(rig) }),
    h('button', {
      class: parkArmed ? 'btns armed' : 'btns',
      title: model.busy ? parkTitle(model.queued) : 'outputs off, shutter shut, router park',
      onclick: () => onPark && onPark(),
    }, parkArmed ? parkLabel(model.queued) : 'Park'),
  ]);
}

function rigText(rig) {
  const parts = [];
  if (rig.sense_resistor_ohm) parts.push(`R_sense ${rig.sense_resistor_ohm} Ω`);
  if (rig.pulse_amp) parts.push(`gain ×${rig.pulse_amp}`);
  if (rig.current_sign) parts.push(`sign ${rig.current_sign > 0 ? '+1' : '−1'}`);
  return parts.join(' · ');
}
