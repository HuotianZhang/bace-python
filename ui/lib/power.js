// The power panel keeps monitoring accessible on every tab; the rail supplies spot
// checks (`docs/ui-rules.md` §8). Browser preferences are reapplied on connect, but
// the service monitor runs independently. Readings show wavelength, saturation and
// averaging. The 5 Hz filter yields mean power, half the DC level at 50 % duty
// (`docs/service-contract.md` §8). History merges by timestamp. A separate fold hides
// 210 px without stopping readings. CSV exports service history; SVG exports the
// visible chart. Separate keys keep 200 ms readings from replacing the switch.

import { h, keyed } from './dom.js';
import * as fmt from './format.js';
import { chart } from './charts/frame.js';
import { powerModel, powerStats, windowPoints, WINDOWS } from './charts/power.js';

/** The intervals the switch offers, in seconds. */
export const INTERVALS = [0.2, 0.5, 1, 2, 5];

/** What the console remembers about the panel between loads. */
export const DEFAULT_UI = { on: false, interval_s: 1, window: 'auto', chart: true, fromZero: false };

/** `auto` shows everything until the log is longer than this, then the last of it. */
export const AUTO_CAP_S = 7200;

/**
 * Auto uses all readings up to `AUTO_CAP_S`, then the last two hours. Spot checks
 * need no window choice.
 */
export function autoWindow(log, now) {
  const first = log.length ? log[0].ts : now;
  const span = Math.max(0, now - first);
  return { key: 'auto', label: 'auto', seconds: span > AUTO_CAP_S ? AUTO_CAP_S : null, auto: true };
}

/** After this many intervals with no reading, the number is drawn as stale. */
export const STALE_INTERVALS = 3;

/**
 * The model uses the most recent stream or bench reading. Both supply watts at the
 * meter and averaging state; the panel invents neither.
 */
export function powerPanelModel(state, ui = DEFAULT_UI, { now = Date.now() / 1000 } = {}) {
  const bench = state.bench || {};
  const inst = (bench.instruments || {}).power || null;
  const monitor = (state.monitors || []).find((m) => m.name === 'power') || null;
  const running = Boolean(monitor && monitor.running);
  const interval = running ? monitor.interval_s : ui.interval_s;
  const log = state.powerLog || [];
  const auto = !ui.window || ui.window === 'auto';
  const window = auto ? autoWindow(log, now) : WINDOWS.find((w) => w.key === ui.window) || autoWindow(log, now);
  const points = windowPoints(log, { seconds: window.seconds, now });
  const stats = powerStats(points);

  const stream = state.power && Number.isFinite(state.power.watts) ? state.power : null;
  const readBack = inst && inst.available && Number.isFinite(inst.watts)
    ? { watts: inst.watts, trustworthy: inst.trustworthy, wavelength_nm: inst.wavelength_nm,
        averaged: inst.averaged, source: 'read-back', ts: bench.read_at || 0 }
    : null;
  const newest = stream && (!readBack || (stream.ts || 0) >= (readBack.ts || 0)) ? stream : readBack;
  const age = newest ? now - (newest.ts || now) : null;
  const value = newest ? {
    watts: newest.watts,
    text: fmt.intensity(newest.watts),
    trustworthy: newest.trustworthy !== false,
    averaged: newest.averaged === true ? true : newest.averaged === false ? false : null,
    wavelength_nm: newest.wavelength_nm ?? null,
    source: newest.source || null,
    ts: newest.ts || null,
    stale: running && age !== null && age > STALE_INTERVALS * (interval || 1),
  } : null;

  const parts = [];
  if (value) {
    parts.push(value.averaged === true ? 'averaged' : value.averaged === false ? 'not averaged' : null);
    if (value.wavelength_nm) parts.push(`${value.wavelength_nm} nm`);
    if (!value.trustworthy) parts.push('saturated or overrange');
    if (value.source && value.source !== 'read-back') parts.push(value.source);
    if (value.stale) parts.push(`no reading for ${fmt.duration(age)}`);
    else if (value.ts) parts.push(fmt.time(value.ts));
  }

  return {
    running,
    wanted: Boolean(ui.on),
    // The panel opens when monitoring is active or requested so a click responds
    // without a round trip. The model keeps `panelShape` consistent with rendering.
    live: running || Boolean(ui.on),
    interval_s: interval,
    available: inst ? inst.available !== false : null,
    reason: inst && inst.available === false ? inst.reason || 'meter not answering' : null,
    value,
    sub: parts.filter(Boolean).join(' · '),
    level: !value ? 'off' : !value.trustworthy ? 'warn' : value.stale ? 'stale' : value.averaged === false ? 'warn' : null,
    window,
    points,
    stats,
    count: log.length,
    monitor: monitor ? { readings: monitor.readings, failures: monitor.failures, last_error: monitor.last_error } : null,
    chart: ui.chart !== false,
    fromZero: Boolean(ui.fromZero),
  };
}

// -- what the panel draws, and what it rebuilds it on ------------------------

/**
 * `panelShape` determines collapse, fold and menu rows. Hiding a chart leaves
 * interval, CSV and Clear available so recorded data stays reachable. Window, y-axis
 * and SVG controls require a chart. Interval changes service polling; fold changes
 * only `ui.chart` (`docs/ui-rules.md` §14). Only live monitoring needs a fold;
 * inactive panels already occupy one line. The pure model makes these distinctions
 * testable.
 */
export function panelShape(model) {
  const chart = model.live && model.chart;
  return {
    collapsed: !model.live,
    fold: model.live,
    chart,
    menu: {
      interval: true,
      window: chart,
      toggles: chart,
      exportCsv: true,
      exportSvg: chart,
      clear: true,
    },
  };
}

/**
 * Keys include only state affecting each part. A count changing five times per second
 * must not replace an open menu; only data availability affects export controls. The
 * status key contains only the message so monitor responses cannot erase an
 * acknowledgement or error from switching off.
 */
export function panelKeys(model, status) {
  return {
    switch: JSON.stringify([model.running, model.wanted, model.available]),
    reading: JSON.stringify([model.value, model.sub, model.level, model.reason, model.live]),
    status: JSON.stringify([status]),
    // The fold key contains its own state, never readings, so frequent redraws cannot
    // replace the button.
    fold: JSON.stringify([model.live, model.chart]),
    menu: JSON.stringify([model.live, model.interval_s, model.window.key, model.chart, model.fromZero,
      model.count > 0, model.points.length > 0]),
    chart: JSON.stringify([model.points.length ? model.points[model.points.length - 1].ts : null,
      model.points.length, model.window.key, model.fromZero]),
  };
}

// -- the DOM ----------------------------------------------------------------

/**
 * The renderer keys switch, reading, reply, fold, menu and chart independently in
 * `el`.
 */
export function renderPowerPanel(el, state, ui, handlers = {}) {
  const model = powerPanelModel(state, ui);
  const { status = null } = handlers;
  if (!el.__key) {
    el.__key = 'power';
    el.replaceChildren(
      h('div.pw-row',
        h('span.pw-switch-box'), h('span.pw-reading'),
        h('span.spacer'), h('span.pw-status-box'), h('span.pw-fold-box'), h('span.pw-menu-box')),
      h('div.pw-chart#pw-chart'));
  }
  // Off: the switch, the last spot check muted, and the `⋯` — one line.
  const shape = panelShape(model);
  const key = panelKeys(model, status);
  el.classList.toggle('off', shape.collapsed);
  keyed(el.querySelector('.pw-switch-box'), key.switch, () => switchRow(model, handlers));
  keyed(el.querySelector('.pw-reading'), key.reading, () => readingEl(model, model.live));
  keyed(el.querySelector('.pw-status-box'), key.status, () => (status
    ? h('span', { class: 'pw-status ' + (status.level || ''), text: status.text }) : []));
  keyed(el.querySelector('.pw-fold-box'), key.fold, () => foldButton(shape, handlers));
  keyed(el.querySelector('.pw-menu-box'), key.menu, () => menu(model, shape.menu, handlers));
  const chartEl = el.querySelector('.pw-chart');
  chartEl.hidden = !shape.chart;
  if (shape.chart) {
    keyed(chartEl, key.chart,
      () => chart((w) => powerModel(state.powerLog || [], {
        width: w, seconds: model.window.seconds, fromZero: model.fromZero,
      })));
  } else if (chartEl.__key !== undefined) {
    // The hidden chart is emptied because SVG export serializes `.pw-chart`. A
    // retained SVG would export a stale trace after monitoring stops.
    chartEl.__key = undefined;
    chartEl.textContent = '';
  }
  return model;
}

function switchRow(model, { onToggle }) {
  const box = h('input', {
    type: 'checkbox', role: 'switch', 'aria-label': 'monitor the power meter',
    checked: model.running || null,
    disabled: model.available === false || null,
    onchange: (e) => onToggle && onToggle(e.target.checked),
  });
  const title = model.available === false
    ? model.reason
    : model.running ? 'stop reading the meter — the trace stays'
      : `the service reads the meter every ${model.interval_s} s, whether or not this page is open`;
  return [
    h('label.pw-switch', { title }, box, h('span', 'power monitor')),
    model.wanted && !model.running && model.available !== false
      ? h('span.pw-note', { text: 'starting' }) : null,
  ];
}

/**
 * The fold hides the 210 px chart without stopping readings or interrupting the power
 * record. `ui.chart` is remembered in browser storage and the number stays visible.
 * Its separate button keeps a frequent action beside the trace. Inactive panels have
 * no chart to fold (`docs/ui-rules.md` §14).
 */
function foldButton(shape, { onChart }) {
  if (!shape.fold) return [];
  const open = shape.chart;
  return h('button.pw-fold', {
    type: 'button',
    class: open ? 'open' : null,
    'aria-expanded': open ? 'true' : 'false',
    'aria-controls': 'pw-chart',
    title: open
      ? 'hide the trace — the monitor keeps reading and every reading is kept'
      : 'show the trace — the readings held, over the window',
    onclick: () => onChart && onChart(!open),
  }, h('span.pw-caret', { text: open ? '▾' : '▸' }), h('span', { text: 'trace' }));
}

function readingEl(model, live) {
  if (!model.value) {
    return live ? [
      h('span', { class: 'pw-value off', text: fmt.ABSENT }),
      h('span.pw-sub', { text: model.reason || 'no reading yet' }),
    ] : [h('span.pw-sub', { text: model.reason || 'off' })];
  }
  const value = h('span', { class: 'pw-value' + (model.level ? ' ' + model.level : ''), text: model.value.text,
    title: 'watts at the meter, on the beam splitter — never an irradiance' });
  return live ? [value, h('span.pw-sub', { text: model.sub })] : [value];
}

/**
 * The menu holds infrequent interval, window, scaling and export controls
 * (`docs/ui-rules.md` §14). Native `details` opens without script and closes on
 * pointer exit. `panelShape(model).menu` retains interval, CSV and Clear without a
 * chart. The frequent fold stays beside it; `y from 0` controls scaling rather than
 * visibility.
 */
function menu(model, rows, { onInterval, onWindow, onZero, onClear, onExportCsv, onExportSvg }) {
  const pick = (label, opts, current, onPick, title) => h('div.pw-opt',
    h('span.pw-optl', { text: label, title }),
    h('span.filt', { role: 'group', 'aria-label': label }, opts.map(([key, text]) => h('button.opt', {
      type: 'button', class: key === current ? 'on' : '', 'aria-pressed': key === current ? 'true' : 'false',
      onclick: () => onPick(key),
    }, text))));
  const toggle = (label, on, onPick, title) => h('label.pw-tog', { title },
    h('input', { type: 'checkbox', checked: on || null, onchange: (e) => onPick(e.target.checked) }),
    h('span', { text: label }));
  const action = (label, title, disabled, onPick) => h('button.btns', { title, disabled: disabled || null, onclick: onPick }, label);
  const details = h('details.pw-menu',
    h('summary', { title: rows.window ? 'interval, window, export …' : 'interval, export …',
      'aria-label': 'power monitor options' }, '⋯'),
    h('div.pw-menu-body',
      rows.interval ? pick('every', INTERVALS.map((s) => [s, `${s} s`]), model.interval_s, (s) => onInterval && onInterval(s),
        'how often the service reads the meter — it stops and restarts the monitor') : null,
      rows.window ? pick('window', [['auto', 'auto'], ...WINDOWS.map((w) => [w.key, w.label])], model.window.auto ? 'auto' : model.window.key,
        (k) => onWindow && onWindow(k),
        'how much of the trace is drawn; the statistics under it are over the same span') : null,
      rows.toggles ? h('div.pw-opt.togs',
        toggle('y from 0', model.fromZero, (v) => onZero && onZero(v), 'pin the y axis to zero, so an LED that is off reads as off')) : null,
      h('div.pw-opt.acts',
        rows.exportCsv ? action('Export CSV', 'every reading the service holds, not only what this page saw',
          !model.count, () => onExportCsv && onExportCsv()) : null,
        rows.exportSvg ? action('Export SVG', 'the trace on screen, as an SVG file',
          !model.points.length || !model.chart, () => onExportSvg && onExportSvg()) : null,
        rows.clear ? action('Clear', 'forget the readings held; the journal keeps them',
          !model.count, () => onClear && onClear()) : null)));
  details.addEventListener('mouseleave', () => { details.open = false; });
  return details;
}

/**
 * SVG export resolves page colour tokens to literal values so the file renders
 * independently of the console.
 */
export function svgFileOf(svg, tokens) {
  const clone = svg.cloneNode(true);
  clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
  const style = document.createElementNS('http://www.w3.org/2000/svg', 'style');
  const rules = Object.entries(tokens).map(([name, value]) => `${name}: ${value};`).join(' ');
  style.textContent = `svg { ${rules} background: ${tokens['--paper'] || '#fff'}; }`;
  clone.insertBefore(style, clone.firstChild);
  return '<?xml version="1.0" encoding="UTF-8"?>\n' + new XMLSerializer().serializeToString(clone);
}
