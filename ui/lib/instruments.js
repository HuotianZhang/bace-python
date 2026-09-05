// The Instruments panel at the top of the bench tab: one row per instrument
// an operator switches by hand, a switch whose lit position *is* the
// instrument's state, and — for the LED — the levels the switch drives it at.
//
// This replaced the `light` card. That card had five buttons, seven fields
// and a red verdict, and the operator could not tell which of the fields a
// button read, whether `DC` opened the shutter, or whether `Off` used the
// form. The answer is structural rather than written: a switch is what a
// bench has for something that acts now, and the values beside the LED's
// switch are the only values any position of it sends.
//
//   * **The lit position is the read-back**, never the last click. The
//     snapshot after every action is what lights it, so a switch that did
//     not take (`BenchActionRefused`) does not move. `how: "inferred"` draws
//     the whole switch dashed, the same distinction the rail makes.
//   * **Clicking a position is the bench action for it**, one per click, the
//     same `POST /bench/actions/*` the chain strip posts. Nothing here goes
//     through the worker, nothing is a run, and nothing writes a file.
//   * **The LED's levels are the `light` module's** — `led_v`, `led_low_v`,
//     `pulse_frequency_hz`, `duty_percent` — edited through the same `PUT`
//     as any card, so a `light` node in a tree starts from what the switch
//     was last set to. `DC` sends the first, `pulse` all four; the ones the
//     lit position does not use are drawn dim, and still editable.
//
// `panelModel` is pure and tested; `renderInstruments` draws it.

import { h } from './dom.js';
import { driveLevel } from './fields.js';

/** The four `light` parameters the LED switch sends, in the order drawn. */
export const LED_LEVELS = ['led_v', 'led_low_v', 'pulse_frequency_hz', 'duty_percent'];

/** Which of the levels each LED position sends. `off` sends none. */
const SENDS = { dc: ['led_v'], pulse: LED_LEVELS, off: [] };

/**
 * `{rows, park}` from the `/bench` snapshot and the `light` module's entry.
 *
 * Each row is `{key, label, inferred, positions: [{id, label, on, action,
 * args}], levels?: [{spec, used}]}`. `on` is the read-back: exactly one
 * position is lit when the instrument answers, none when it does not.
 */
export function panelModel(bench, light) {
  const instruments = (bench && bench.instruments) || {};
  const inferred = new Set((bench && bench.inferred) || []);
  const is = (name) => inferred.has(name) || (instruments[name] || {}).how === 'inferred';
  const values = Object.fromEntries(((light && light.params) || []).map((p) => [p.name, p.value]));
  const specs = Object.fromEntries(((light && light.params) || []).map((p) => [p.name, p]));

  const shutter = instruments.shutter || {};
  const led = instruments.led || {};
  const relay = instruments.relay || {};

  const ledOn = led.output === true && String(led.mode || '').toUpperCase() !== 'OFF';
  const mode = String(led.mode || '').toUpperCase();
  const ledPosition = led.output === null || led.output === undefined || !led.mode || led.mode === '?'
    ? null
    : !ledOn ? 'off' : mode === 'DC' ? 'dc' : mode === 'PULSE' ? 'pulse' : null;

  const rows = [
    {
      key: 'shutter', label: 'Shutter', inferred: is('shutter'),
      positions: [
        { id: 'open', label: 'open', on: shutter.open === true, action: 'shutter-open', args: {} },
        { id: 'shut', label: 'shut', on: shutter.open === false, action: 'shutter-shut', args: {} },
      ],
    },
    {
      key: 'led', label: 'LED · 33220A', inferred: is('led'),
      positions: [
        { id: 'off', label: 'off', on: ledPosition === 'off', action: 'led-off', args: {} },
        { id: 'dc', label: 'DC', on: ledPosition === 'dc', action: 'set-led-dc',
          args: { level: values.led_v } },
        { id: 'pulse', label: 'pulse', on: ledPosition === 'pulse', action: 'set-led-pulse',
          args: { level: values.led_v, low: values.led_low_v,
            frequency_hz: values.pulse_frequency_hz, duty_percent: values.duty_percent } },
      ],
      // What the generator is actually at, when it is on: the switch says
      // `DC`, this says `1.000 V`, and the two are one reading.
      level: ledOn ? driveLevel(led) : null,
      levels: LED_LEVELS.filter((n) => specs[n]).map((n) => ({
        spec: specs[n],
        used: (SENDS[ledPosition || 'off'] || []).includes(n),
      })),
    },
    {
      key: 'relay', label: 'Relay', inferred: is('relay'),
      positions: [
        { id: 'amplifier', label: 'amplifier', on: relay.position === 'amplifier', action: 'relay-to-amplifier', args: {} },
        { id: 'sourcemeter', label: 'sourcemeter', on: relay.position === 'sourcemeter', action: 'relay-to-sourcemeter', args: {} },
      ],
    },
  ];
  return { rows, park: { action: 'park', args: {} } };
}

/**
 * The panel. `ctx` is `{busy, act(action, args), edit(module, params),
 * park()}`; `park` is the strip's own, so the arm-then-confirm it does while
 * a run holds the worker is one implementation.
 */
export function renderInstruments(model, ctx) {
  const busy = Boolean(ctx.busy);
  return h('div.inst',
    h('div.zh',
      h('span.zt', 'Instruments'),
      h('span', { style: { flex: '1' } }),
      h('button.btnd', {
        title: busy ? 'abort the run and park — the chain strip asks first' : 'outputs off, shutter shut, relay parked',
        onclick: () => ctx.park(),
      }, 'Park')),
    model.rows.map((row) => h('div.irow', { class: row.inferred ? 'inferred' : '' },
      h('span.n', { text: row.label }),
      h('span.sw', { role: 'group', 'aria-label': row.label, title: row.inferred ? 'inferred from the running step, not read back' : '' },
        row.positions.map((p) => h('button.p', {
          type: 'button',
          class: p.on ? 'on' : '',
          'aria-pressed': p.on ? 'true' : 'false',
          disabled: busy || null,
          title: busy ? 'a run holds the bench; only Park is allowed until it ends' : `${row.label} → ${p.label}`,
          onclick: () => { if (!p.on) ctx.act(p.action, p.args); },
        }, p.label))),
      row.levels ? levels(row, ctx) : h('span'))));
}

function levels(row, ctx) {
  return h('span.vals', row.levels.map(({ spec, used }) => h('label', { class: used ? '' : 'dim' },
    h('input.v', {
      type: 'text', 'aria-label': spec.name, title: spec.doc || spec.name,
      value: spec.value === null || spec.value === undefined ? '' : (spec.unit === 'V' ? Number(spec.value).toFixed(3) : String(spec.value)),
      onchange: (e) => {
        const raw = e.target.value.trim();
        ctx.edit('light', { [spec.name]: raw === '' ? null : raw });
      },
    }),
    h('i', { text: unitLabel(spec) }))));
}

function unitLabel(spec) {
  if (spec.name === 'led_low_v') return 'V low';
  return spec.unit || '';
}
