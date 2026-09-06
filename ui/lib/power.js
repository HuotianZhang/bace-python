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
//     connect so a reload does not start the chart blank) — and a fold beside
//     the `⋯` that puts those 210 px away without stopping the monitor, since
//     wanting the shell out of the way is not wanting the meter unread.
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
    // Live is running *or* wanted: the wish counts, so the panel opens the
    // moment the switch is flipped rather than a round trip later. The
    // definition lives here rather than in the renderer, so `panelShape` and
    // the tests see the same one.
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
 * What the panel draws: the collapsed line or the open one, whether the trace
 * is folded away, which rows the `⋯` carries, and whether the chart is on
 * screen. Pure and exported, so the decisions are held down by
 * `ui/tests/power.test.mjs` — the renderer only obeys it.
 *
 * The rule that shapes it: **switching the monitor off may take away the
 * always-on chart; it must not take away what the monitor recorded.** So the
 * `⋯` is there off as well as on, carrying Export CSV and Clear — and the
 * interval, which is not a view filter at all but a control that acts on the
 * bench the moment it lands (`app.js` stops and restarts the service's
 * polling), and the one thing worth setting *before* the switch is flipped.
 * What a panel with no chart drops is only what describes a chart that is not
 * drawn: the window, the y-axis toggle, and Export SVG, which serialises the
 * SVG on screen and has none to serialise.
 *
 * **Two things can take the chart away, and they are not the same thing.**
 * The switch is about the bench — it starts and stops a thread in the service
 * that reads the meter whether or not a browser is open. The fold is about
 * this screen and nothing else: an operator watching a card wants the meter's
 * number on the rail and the 210 px of trace out of the way, and folding it
 * must not cost them a reading. So `fold` is drawn whenever the monitor is
 * live, it is the operator's own `ui.chart`, and switching it does not touch
 * the service. Off, there is no fold: the panel is already one quiet line.
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
 * What each keyed part is rebuilt on. Pure and exported for the same reason:
 * a key that carries too much is invisible in the DOM and ruinous in the hand.
 *
 * The reading count is the trap. It moves with every `PowerReading` — five
 * times a second at the 0.2 s interval — and the menu is a `<details>` the
 * operator holds open while they pick. Keyed on `count` it was torn down and
 * rebuilt under the pointer at that rate, in the one state where it is drawn
 * at all: `dom.keyed`'s argument and the run monitor's M2 finding, on this
 * panel. Only *whether* there is anything to export or clear changes what the
 * menu draws, so only that is in its key.
 *
 * The status sentence is keyed on itself and on nothing else. Keyed on `live`
 * as well, it was drawn for the one round trip before `refreshMonitors` came
 * back and then silently removed — taking the answer to *switching the
 * monitor off*, and any error that answer carried, with it.
 */
export function panelKeys(model, status) {
  return {
    switch: JSON.stringify([model.running, model.wanted, model.available]),
    reading: JSON.stringify([model.value, model.sub, model.level, model.reason, model.live]),
    status: JSON.stringify([status]),
    // The fold carries its own state and no reading: it is a button the
    // operator clicks, and the trace under it is redrawn five times a second.
    fold: JSON.stringify([model.live, model.chart]),
    menu: JSON.stringify([model.live, model.interval_s, model.window.key, model.chart, model.fromZero,
      model.count > 0, model.points.length > 0]),
    chart: JSON.stringify([model.points.length ? model.points[model.points.length - 1].ts : null,
      model.points.length, model.window.key, model.fromZero]),
  };
}

// -- the DOM ----------------------------------------------------------------

/**
 * Into `el`, in six keyed parts: the switch, the reading, the sentence the
 * last click came back with, the fold, the `⋯` menu and the chart.
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
    // Emptied, not merely hidden: `app.js`'s Export SVG serialises whatever
    // `svg` sits under `.pw-chart`, and one left there is the trace from
    // before the monitor was switched off.
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
 * The fold — `▾ trace` / `▸ trace`, beside the `⋯` and independent of the
 * switch.
 *
 * The trace is 210 px of the shell on every tab, and the shell sits above the
 * view the operator is actually working in: a card being filled in, a tree
 * being edited. Before this there was one way to be rid of it — switch the
 * monitor off — and that answer is wrong twice over: it stops the service
 * reading the meter, which is the thing the operator wanted to keep running,
 * and it leaves the run with no power record for the minutes the panel was
 * out of the way. So the fold is a button of its own, it is remembered like
 * every other panel wish (`ui.chart` in `localStorage`), and it touches
 * nothing but this screen — the monitor goes on reading, the readings go on
 * arriving, and the number stays big at the top of the panel.
 *
 * It is drawn only where there is something to fold: off, the panel is
 * already one quiet line and the chart is not on it.
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
 * Everything the operator touches less than once a session, behind one
 * `⋯`: the interval and the window (with the defaults that suit a scan),
 * `y from 0`, and the three actions. `<details>` so it needs no script to
 * open, and closes itself when the pointer leaves it.
 *
 * `rows` is `panelShape(model).menu` — which of them this state carries. With
 * no chart on screen — the monitor off, or the trace folded away — that is the
 * interval, Export CSV and Clear: nothing the panel recorded is out of reach.
 *
 * The trace itself is not in here. It was a toggle in this menu, two clicks
 * behind a `⋯` that closes itself on `mouseleave`, which is the wrong depth
 * for something the operator reaches for whenever the view underneath needs
 * the room — it is the fold beside the `⋯` now, and this menu keeps only
 * `y from 0`, which describes how the trace is drawn rather than whether.
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
