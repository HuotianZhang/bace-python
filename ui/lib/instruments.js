// The Instruments panel groups immediate shutter, LED and relay actions. Selection
// reflects read-back, never the last click; inferred state has a dashed outline. Each
// click sends one bench action without a run or result file. LED controls edit the
// `light` module's level, low level, frequency and duty: DC sends the first and Pulse
// sends all four. Unused fields stay editable but dim. Shared module values also
// supply pipeline nodes. `panelModel` is pure; `renderInstruments` renders it.

import { h } from './dom.js';
import { driveLevel } from './fields.js';

/** The four `light` parameters the LED switch sends, in the order drawn. */
export const LED_LEVELS = ['led_v', 'led_low_v', 'pulse_frequency_hz', 'duty_percent'];

/** Which of the levels each LED position sends. `off` sends none. */
const SENDS = { dc: ['led_v'], pulse: LED_LEVELS, off: [] };

/**
 * The model combines bench state and the `light` entry. Rows carry positions,
 * actions, inference state and optional levels. Read-back selects one position when
 * available and none when unreadable.
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
        { id: 'open', label: 'Open', on: shutter.open === true, action: 'shutter-open', args: {} },
        { id: 'shut', label: 'Shut', on: shutter.open === false, action: 'shutter-shut', args: {} },
      ],
    },
    {
      key: 'led', label: 'LED', model: 'Agilent 33220A', inferred: is('led'),
      positions: [
        { id: 'off', label: 'Off', on: ledPosition === 'off', action: 'led-off', args: {} },
        { id: 'dc', label: 'DC', on: ledPosition === 'dc', action: 'set-led-dc',
          args: { level: values.led_v } },
        { id: 'pulse', label: 'Pulse', on: ledPosition === 'pulse', action: 'set-led-pulse',
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
        { id: 'amplifier', label: 'Amplifier', on: relay.position === 'amplifier', action: 'relay-to-amplifier', args: {} },
        { id: 'sourcemeter', label: 'Source meter', on: relay.position === 'sourcemeter', action: 'relay-to-sourcemeter', args: {} },
      ],
    },
  ];
  return { rows };
}

/**
 * The panel uses `ctx` actions and edits. Park stays in the pinned strip with one
 * visible abort-and-cancel confirmation. Side-by-side switches label themselves
 * without a repeated heading. The row measures about 1226 px; a narrower view wraps
 * whole instruments to a second row (`ui/style.css`, `.inst`).
 * Four-pixel padding retains instrument-switch borders and fill (`docs/ui-rules.md`
 * §14).
 */
export function renderInstruments(model, ctx) {
  const busy = Boolean(ctx.busy);
  return h('div.inst',
    model.rows.map((row) => h('div.irow', { class: row.inferred ? 'inferred' : '' },
      h('span.n', { text: row.label, title: row.model || null }),
      h('span.sw', { role: 'group', 'aria-label': row.label, title: row.inferred ? 'Inferred from the running step, not read back.' : '' },
        row.positions.map((p) => h('button.p', {
          type: 'button',
          class: p.on ? 'on' : '',
          'aria-pressed': p.on ? 'true' : 'false',
          disabled: busy || null,
          title: busy ? 'A run holds the bench. Only Park acts until it ends.' : `${row.label}: ${p.label}`,
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
