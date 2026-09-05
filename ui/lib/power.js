// The power panel — the 1918-C, always on the screen, under the rail.
//
// `docs/ui-rules.md` §8 names the two uses of the meter: a *spot check* (one
// number, now, to confirm the lamp is on) and a *live monitor* that stays
// visible while a scan runs. The rail's cell is the first. This is the
// second, and it is shell furniture like the run monitor: whichever tab is
// open, the switch, the number and the trace are here.
//
// What it owes, taken from the meter's own console (`D:\1918cPowerMeter`,
// whose driver is the one vendored under `bace/drivers/newport1918c/`):
//
//   * **a switch that stays on.** The monitor is a thread in the service
//     (`POST /monitors/power`), so it runs whether or not a browser is open;
//     the switch here starts and stops it, and the console remembers the
//     wish (`localStorage`) and re-asserts it on every connect — a reload, a
//     service restart — so "on" means on until somebody switches it off.
//     `--power-monitor SECONDS` on the service does the same from boot.
//   * **the reading**, big, with what the meter said about it: averaged or
//     not (the 5 Hz filter that makes a pulsed LED read as its mean — half
//     the DC level at 50 % duty), the wavelength, saturation.
//   * **the trace below it**, over a window the operator picks, with the
//     statistics over that window, from the store's `powerLog` (every
//     `PowerReading` frame, merged with `GET /monitors/power/history` on
//     connect so a reload does not start the chart blank).
//   * **export**: the whole history the service holds as a CSV
//     (`/monitors/power/history.csv`), and the chart on screen as an SVG.
//
// `powerPanelModel` is pure; the DOM is beside it, keyed in parts so a
// reading arriving every 200 ms redraws the number and the trace and never
// the switch under the operator's pointer (`dom.keyed`, and the run
// monitor's argument for the same split).

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
 * `auto`: every reading there is, so the trace fills the axis from the
 * first one — until the monitor has run longer than `AUTO_CAP_S`, when it
 * becomes the last two hours. Nobody chooses a window for a spot check.
 */
export function autoWindow(log, now) {
  const first = log.length ? log[0].ts : now;
  const span = Math.max(0, now - first);
  return { key: 'auto', label: 'auto', seconds: span > AUTO_CAP_S ? AUTO_CAP_S : null, auto: true };
}

/** After this many intervals with no reading, the number is drawn as stale. */
export const STALE_INTERVALS = 3;

/**
 * The panel as one plain object.
 *
 * The number shown is the newest reading the console has: the stream's
 * `PowerReading` when the monitor is running, else the read-back's (`GET
 * /bench`), whichever is later. Both are watts at the meter and both carry
 * `averaged`; neither is invented here.
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

// -- the DOM ----------------------------------------------------------------

/**
 * Into `el`, in four keyed parts: the switch and its interval, the reading,
 * the window and the buttons, and the chart. `status` is the sentence the
 * last click came back with.
 */
export function renderPowerPanel(el, state, ui, handlers = {}) {
  const model = powerPanelModel(state, ui);
  const { status = null } = handlers;
  if (!el.__key) {
    el.__key = 'power';
    el.replaceChildren(
      h('div.pw-row',
        h('span.pw-switch-box'), h('span.pw-reading'),
        h('span.spacer'), h('span.pw-status-box'), h('span.pw-menu-box')),
      h('div.pw-chart'));
  }
  // Off: the switch and, muted, the last spot check. Nothing to configure
  // until there is something running to configure.
  const live = model.running || model.wanted;
  el.classList.toggle('off', !live);
  keyed(el.querySelector('.pw-switch-box'), JSON.stringify([model.running, model.wanted, model.available]),
    () => switchRow(model, handlers));
  keyed(el.querySelector('.pw-reading'), JSON.stringify([model.value, model.sub, model.level, model.reason, live]),
    () => readingEl(model, live));
  keyed(el.querySelector('.pw-status-box'), JSON.stringify([status, live]), () => (status && live
    ? h('span', { class: 'pw-status ' + (status.level || ''), text: status.text }) : []));
  keyed(el.querySelector('.pw-menu-box'), JSON.stringify([live, model.interval_s, model.window.key, model.chart, model.fromZero, model.count]),
    () => (live ? menu(model, handlers) : []));
  const chartEl = el.querySelector('.pw-chart');
  chartEl.hidden = !(live && model.chart);
  if (live && model.chart) {
    const last = model.points.length ? model.points[model.points.length - 1].ts : null;
    keyed(chartEl, JSON.stringify([last, model.points.length, model.window.key, model.fromZero]),
      () => chart(powerModel(state.powerLog || [], { seconds: model.window.seconds, fromZero: model.fromZero })));
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
 * Everything the operator touches less than once a session, behind one
 * `⋯`: the interval and the window (with the defaults that suit a scan),
 * the two chart toggles, and the three actions. `<details>` so it needs no
 * script to open, and closes itself when the pointer leaves it.
 */
function menu(model, { onInterval, onWindow, onChart, onZero, onClear, onExportCsv, onExportSvg }) {
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
    h('summary', { title: 'interval, window, export …', 'aria-label': 'power monitor options' }, '⋯'),
    h('div.pw-menu-body',
      pick('every', INTERVALS.map((s) => [s, `${s} s`]), model.interval_s, (s) => onInterval && onInterval(s),
        'how often the service reads the meter'),
      pick('window', [['auto', 'auto'], ...WINDOWS.map((w) => [w.key, w.label])], model.window.auto ? 'auto' : model.window.key,
        (k) => onWindow && onWindow(k),
        'how much of the trace is drawn; the statistics under it are over the same span'),
      h('div.pw-opt.togs',
        toggle('trace', model.chart, (v) => onChart && onChart(v), 'show or hide the trace'),
        toggle('y from 0', model.fromZero, (v) => onZero && onZero(v), 'pin the y axis to zero, so an LED that is off reads as off')),
      h('div.pw-opt.acts',
        action('Export CSV', 'every reading the service holds, not only what this page saw',
          !model.count, () => onExportCsv && onExportCsv()),
        action('Export SVG', 'the trace on screen, as an SVG file',
          !model.points.length || !model.chart, () => onExportSvg && onExportSvg()),
        action('Clear', 'forget the readings held; the journal keeps them',
          !model.count, () => onClear && onClear()))));
  details.addEventListener('mouseleave', () => { details.open = false; });
  return details;
}

/**
 * The chart on screen as a file that stands on its own: the SVG uses the
 * page's colour tokens (`var(--ink)`), which mean nothing outside it, so
 * their current values are written into the file.
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
