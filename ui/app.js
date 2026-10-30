// The console shell connects the store, socket, tabs and hash router. The `--ui DIR`
// static mount has no SPA fallback, so `/ui/bench` returns 404. Hash routes keep
// navigation within the served page.

import { createApi } from './lib/api.js';
import { createStore, currentRun } from './lib/store.js';
import { createStream } from './lib/stream.js';
import { h, fill, keyed } from './lib/dom.js';
import * as fmt from './lib/format.js';
import { parseHash, swapView } from './lib/route.js';
import { renderRail, renderChainStrip, PREREQUISITE } from './lib/rail.js';
import { renderMonitor } from './lib/monitor.js';
import { renderPowerPanel, svgFileOf, DEFAULT_UI as POWER_DEFAULTS } from './lib/power.js';
import { renderTitle, liveRunId } from './lib/title.js';
import { renderIdentity, identityModel } from './lib/identity.js';
import { createBenchWatch } from './lib/watch.js';
import { createAssistantPanel } from './lib/assistant.js';
import { createPlansPanel } from './lib/plans.js';

import bench from './views/bench.js';
import pipeline from './views/pipeline.js';
import results from './views/results.js';
import rig from './views/rig.js';

const VIEWS = [bench, pipeline, results, rig];
const BY_ROUTE = Object.fromEntries(VIEWS.map((v) => [v.route, v]));

const api = createApi({});
const store = createStore({ schedule: (fn) => requestAnimationFrame(fn) });

const stream = createStream({
  // Each connection calls `syncPower` to restore the requested monitor state and
  // merge the service's trace.
  onHello: (frame) => { store.applyHello(frame); syncPower(); plansPanel.refresh(); },
  onFrame: (frame, meta) => { store.applyFrame(frame); afterFrame(frame, meta || {}); },
  onStatus: (status) => store.applyConnection(status),
  onSessionChange: ({ from, to }) => {
    // A session change invalidates the numbered stream. `Hello` restores the bench
    // and the ring restores events. Neither includes the catalogue, which
    // `store.reset()` clears, so the cards need a separate fetch.
    console.info(`service session ${from} -> ${to}; rebuilding`);
    store.reset();
    watch.want({ modules: true });
  },
});

const tabsEl = h('nav.tabs');
const chipsEl = h('span.chips');
const identEl = h('section.ident', { hidden: true });
const railEl = h('header.rail');
const powerEl = h('section.power');
const monitorEl = h('section.monitor', { hidden: true });
const viewEl = h('main.view');
const stripEl = h('div.strip');
const barEl = h('footer.stream-bar');

// The bar's `ask` control opens the assistant beside every tab. The assistant and
// the plans share the side of the view, so opening one closes the other.
let askOpen = false;
const assistant = createAssistantPanel({
  api, onChange: (open) => {
    askOpen = open;
    if (open && plansOpen) plansPanel.toggle(false);
    renderChips(store.getState());
  },
});

// The plans an agent proposed wait here for a person's decision.
let plansOpen = false;
let plansPending = 0;
const plansPanel = createPlansPanel({
  api, onChange: ({ open, pending }) => {
    plansOpen = open;
    plansPending = pending;
    if (open && askOpen) assistant.toggle(false);
    renderChips(store.getState());
  },
});

document.getElementById('app').replaceChildren(
  h('div.bar', h('span.wordmark', 'bace'), tabsEl, h('span.spacer'), chipsEl),
  identEl, railEl, powerEl, monitorEl, h('div.body', viewEl, assistant.el, plansPanel.el),
  stripEl, barEl);

let mounted = null;
let mountedRoute = null;

function route() {
  const { route: hash } = parseHash(location.hash);
  return BY_ROUTE[hash] ? hash : 'bench';
}

function show() {
  const name = route();
  const { query } = parseHash(location.hash);
  if (name === mountedRoute) {
    // A query change on the active tab reaches the view without remounting it.
    if (mounted && mounted.focus) mounted.focus(query);
    return;
  }
  // Routing state and tab selection must match the address bar even if `swapView`
  // encounters a view error.
  const previous = mounted;
  mounted = null;
  mountedRoute = name;
  viewEl.scrollTop = 0;
  renderTabs();
  // Park belongs to the strip pinned below every view. A single confirmation control
  // avoids conflicting armed states.
  mounted = swapView(previous,
    () => BY_ROUTE[name].mount(viewEl, { store, api, stream, fmt, notify, query }));
}

function renderTabs() {
  fill(tabsEl, VIEWS.map((view) => h('a', {
    href: `#/${view.route}`,
    class: view.route === mountedRoute ? 'on' : '',
    text: view.title,
  })));
}

/**
 * The bar renders the session controls. The strip carries the trigger-chain count and
 * remedy; the rail marks the affected value. Repeating the count in the bar adds
 * another place to reconcile the same state.
 */
function renderChips(state) {
  const session = state.session || {};
  const ident = identityModel(state);
  const model = [ident.chip, ident.unnamed, ident.hazards.length, identOpen,
                 session.mode, session.fast, currentRun(state) ? runLabel(state) : null,
                 state.queue.length, state.benchState, askOpen, quitArmed, plansOpen, plansPending];
  keyed(chipsEl, JSON.stringify(model), () => [
    // The identity text opens its editor so an unnamed sample has a visible remedy.
    h('button', {
      class: 'chip ident' + (ident.unnamed || ident.hazards.length ? ' bad' : '') + (identOpen ? ' on' : ''),
      title: ident.unnamed
        ? 'No sample named. Folders open on the temperature; click to name it.'
        : `${ident.preview}\nClick to edit the sample.`,
      'aria-expanded': identOpen ? 'true' : 'false',
      onclick: () => toggleIdent(),
    }, ident.chip),
    h('span.chip', { text: `${session.mode || fmt.ABSENT}${session.fast ? ' · fast' : ''}` }),
    currentRun(state) ? h('span.chip.m', { text: runLabel(state) }) : null,
    state.queue.length ? h('span.chip', { text: `Queue ${state.queue.length}` }) : null,
    h('span', { class: 'chip state ' + (state.benchState || 'idle'), text: state.benchState || 'idle' }),
    h('button', {
      class: 'chip ask' + (askOpen ? ' on' : ''), text: 'Ask',
      title: 'Assistant. Reads the bench and the manual; changes nothing.',
      'aria-expanded': askOpen ? 'true' : 'false', onclick: () => assistant.toggle(),
    }),
    h('button', {
      class: 'chip plans' + (plansPending ? ' bad' : '') + (plansOpen ? ' on' : ''),
      text: plansPending ? `Plans ${plansPending}` : 'Plans',
      title: 'Plans proposed by an agent. Each runs once approved here, or at once under [plans] auto_approve in rig.toml.',
      'aria-expanded': plansOpen ? 'true' : 'false', onclick: () => plansPanel.toggle(),
    }),
    h('button', {
      class: 'chip quit' + (restartArmed ? ' armed' : ''),
      text: restartArmed ? 'Restart?' : 'Restart',
      title: 'Aborts the run, parks the bench, then starts the service again from the files on disk. Plans are forgotten.',
      onclick: () => restartService(),
    }),
    h('button', {
      class: 'chip quit' + (quitArmed ? ' armed' : ''),
      text: quitArmed ? 'Shut down?' : 'Shut down',
      title: 'Aborts the run, parks the bench, closes the journal.',
      onclick: () => shutDown(),
    }),
  ]);
}

/**
 * The console can end a service launched without a terminal by `BACE.pyw`. Two clicks
 * within six seconds confirm an abort and park. Closing the browser leaves the
 * service and its run active; `BACE.pyw` reconnects to them.
 */
let quitArmed = false;
let quitArmedTimer = null;

function armQuit(on) {
  quitArmed = on;
  clearTimeout(quitArmedTimer);
  quitArmedTimer = on ? setTimeout(() => { quitArmed = false; renderChips(store.getState()); }, 6000) : null;
  renderChips(store.getState());
}

let restartArmed = false;
let restartArmedTimer = null;

function armRestart(on) {
  restartArmed = on;
  clearTimeout(restartArmedTimer);
  restartArmedTimer = on ? setTimeout(() => { restartArmed = false; renderChips(store.getState()); }, 6000) : null;
  renderChips(store.getState());
}

async function restartService() {
  if (!restartArmed) return armRestart(true);
  armRestart(false);
  try {
    await api.restart(true);
  } catch (error) {
    notify(error.text || error.message, 'bad');
    return;
  }
  stream.close();
  document.getElementById('app').replaceChildren(h('div.ended', h('p.wordmark', 'bace'),
    h('h1', 'Restarting'), h('p', 'Parking the bench and reading the instruments. Reload this page when the service answers.')));
}

async function shutDown() {
  if (!quitArmed) return armQuit(true);
  armQuit(false);
  try {
    await api.shutdown(true);
  } catch (error) {
    notify(error.text || error.message, 'bad');
    return;
  }
  stream.close();
  const heading = h('h1', 'Shutting down');
  const body = h('p', 'Aborting any run, parking the bench, closing the journal.');
  document.getElementById('app').replaceChildren(h('div.ended', h('p.wordmark', 'bace'), heading, body));
  // HTTP 202 acknowledges the exit request, not park. An instrument call that does
  // not return can prevent park. The page waits for process exit and reports the
  // limit of what it can observe; runs\logs\ records which happened.
  const gone = await serviceGone(SHUTDOWN_WAIT_MS);
  heading.textContent = gone ? 'Service stopped' : 'Service has not stopped';
  body.textContent = gone
    ? 'Bench parked unless a run held an instrument; runs\\logs\\ says which. '
      + 'Close this window; BACE.pyw starts the service again.'
    : 'A run is still inside an instrument call. Check the instruments; see runs\\logs\\.';
}

/**
 * The worker permits 5 s for abort and 60 s for joining during exit.
 */
const SHUTDOWN_WAIT_MS = 75000;

async function serviceGone(limitMs) {
  const deadline = Date.now() + limitMs;
  while (Date.now() < deadline) {
    try {
      await api.call('GET', '/session');
    } catch (error) {
      if (!error.status) return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return false;
}

/**
 * The identity panel edits the device metadata shown in the bar. These fields change
 * infrequently, so the values stay visible while the editor opens on request. An
 * unnamed device opens the panel once per session to prevent anonymous filing.
 * Reopening on every reload would obstruct simulated benches with no device mounted.
 */
let identOpen = false;
let identStatus = null;
let identOffered = false;

function drawIdent(state) {
  if (!identOffered && identityModel(state).unnamed && state.session) {
    identOffered = true;
    identOpen = true;
  }
  renderIdentity(identEl, state, {
    open: identOpen, status: identStatus, onSet: setSample,
    onClose: () => {
      identOpen = false;
      identStatus = null;
      drawIdent(store.getState());
      renderChips(store.getState());
    },
  });
}

function toggleIdent() {
  identOpen = !identOpen;
  identOffered = true;
  if (!identOpen) identStatus = null;
  drawIdent(store.getState());
  renderChips(store.getState());
}

/**
 * The service merges one identity key and returns the whole session block. Applying
 * that answer updates the panel without a socket delay. A queued run retains its
 * captured identity, so the panel must identify the run that does not receive the
 * edit.
 */
async function setSample(name, value) {
  identStatus = { level: '', text: `${name} …` };
  drawIdent(store.getState());
  try {
    const out = await api.setSample({ [name]: value });
    store.applySession(out.session);
    identStatus = out.changed && out.changed.length
      ? { level: 'ok',
          text: out.run_active
            ? `Saved. ${out.run_active} keeps its name.`
            : 'Saved. Applies to runs queued from now.' }
      : { level: '', text: 'Unchanged' };
  } catch (error) {
    identStatus = { level: 'bad', text: error.text || error.message };
  }
  drawIdent(store.getState());
  renderChips(store.getState());
}

/**
 * The chain strip offers actions for operator confirmation. A warning states evidence
 * without blocking; the console never performs its remedy automatically
 * (`docs/ui-rules.md` §3).
 */
let stripStatus = null;
let parkArmed = false;
let parkArmedTimer = null;

function drawStrip(state) {
  renderChainStrip(stripEl, state, {
    onFix: fix, onPark: park, onDismiss: dismiss, status: stripStatus, parkArmed,
  });
}

/**
 * A refused call appears in the strip below every view, giving refusals one location.
 * The service supplies `error.text`; a 422 also carries `checks`, which the operator
 * needs in full.
 */
function notify(text, level = 'warn', checks = null) {
  stripStatus = { level: level === 'crit' || level === 'bad' ? 'bad' : level, text, checks };
  drawStrip(store.getState());
}

/**
 * The dismiss action clears the strip's message. Messages persist for the operator to
 * read and dismiss; a timer could hide a refusal before it is read.
 */
function dismiss() {
  stripStatus = null;
  drawStrip(store.getState());
}

/**
 * An armed Park expires because arming is a confirmation, not a mode. The shell owns
 * the flag and redraws its single control in the strip.
 */
function armPark(on) {
  parkArmed = on;
  clearTimeout(parkArmedTimer);
  parkArmedTimer = on ? setTimeout(() => { parkArmed = false; drawStrip(store.getState()); }, 6000) : null;
  drawStrip(store.getState());
}

async function fix(name, item) {
  stripStatus = { level: '', text: `${name} …` };
  drawStrip(store.getState());
  try {
    const out = await api.action(name);
    // The action returns a read-back so the chain and rail update immediately.
    // `readBack` leaves run, queue and bench state to the stream.
    if (out && out.bench) store.applyBench(out.bench, { readBack: true });
    stripStatus = { level: 'ok', text: `${item.label}: ${describe(out)}` };
  } catch (error) {
    // A 409 can mean the LED is on or a run holds the worker. The strip displays the
    // service's reason unchanged and offers any remedy as a separate operator action.
    stripStatus = {
      level: 'bad',
      text: error.text || error.message,
      offer: error.refused ? PREREQUISITE[name] || null : null,
    };
  }
  drawStrip(store.getState());
}

/**
 * `result.before` carries the read-back values preceding the action, allowing the
 * journal to describe the change (`docs/service-contract.md` §4). The strip adds
 * those values to the current state shown on the rail.
 */
function describe(out) {
  const before = (out && out.result && out.result.before) || null;
  if (!before) return 'done';
  return 'done; ' + Object.entries(before).map(([key, was]) => `${key} was ${was}`).join(', ');
}

/**
 * Park aborts the active run and cancels the queue to make the bench safe
 * (`docs/service-contract.md` §4). A busy bench requires two clicks to prevent an
 * accidental abort.
 */
async function park() {
  const busy = (store.getState().benchState || 'idle') !== 'idle';
  if (busy && !parkArmed) return armPark(true);
  armPark(false);
  stripStatus = { level: '', text: 'Parking …' };
  drawStrip(store.getState());
  try {
    const out = await api.action('park');
    if (out && out.bench) store.applyBench(out.bench, { readBack: true });
    stripStatus = out && out.pending
      // A blocking VISA call can exceed the wait. Park remains queued for the worker,
      // so the operator needs to see that it is pending.
      ? { level: '', text: 'Park queued; a run holds an instrument.' }
      : { level: 'ok', text: 'Parked' };
  } catch (error) {
    stripStatus = { level: 'bad', text: error.text || error.message };
  }
  drawStrip(store.getState());
}

/**
 * The monitor exposes operator-requested run controls. A 409 for an ended run or
 * absent pause displays the service's reason on the monitor.
 */
let armedRun = null;
let abortArmedTimer = null;
let monitorStatus = null;

function drawMonitor(state) {
  renderMonitor(monitorEl, state, { onStop: stopRun, onAbort: abortRun, onResume: resumeRun, armedRun, status: monitorStatus });
}

/**
 * Abort arming belongs to one run ID. A ring gap can omit `parked`; a boolean could
 * then arm the next run. `monitor.scopedTo` rejects an arm belonging to another run.
 */
function armAbort(runId) {
  armedRun = runId;
  clearTimeout(abortArmedTimer);
  abortArmedTimer = runId ? setTimeout(() => { armedRun = null; drawMonitor(store.getState()); }, 6000) : null;
  drawMonitor(store.getState());
}

async function stopRun(runId, mode) {
  monitorStatus = { run_id: runId, level: '', text: `${mode} …` };
  drawMonitor(store.getState());
  try {
    const out = await api.stopRun(runId, mode);
    // The stream owns `stopping`; this status only acknowledges the request and
    // clears with the run.
    monitorStatus = { run_id: runId, level: 'ok', text: out.state === 'cancelled' ? 'cancelled' : `${out.mode} accepted` };
  } catch (error) {
    monitorStatus = { run_id: runId, level: 'bad', text: error.text || error.message };
  }
  drawMonitor(store.getState());
}

/**
 * Abort discards the shot in progress and starts no further shot
 * (`docs/service-contract.md` §2). Two clicks protect the sample from an accidental
 * abort.
 */
async function abortRun(runId) {
  if (armedRun !== runId) return armAbort(runId);
  armAbort(null);
  await stopRun(runId, 'abort');
}

/**
 * Resume sends one request per pause. A second request receives 409 and is dropped
 * (`docs/service-contract.md` §2), which would leave a misleading error after a
 * successful resume. A synchronous flag guards the request: disabling and redrawing
 * between mouse press and release can consume the click.
 */
let resuming = false;

async function resumeRun(runId, detail) {
  if (resuming) return;
  resuming = true;
  monitorStatus = { run_id: runId, level: '', text: 'resume …' };
  drawMonitor(store.getState());
  const body = {};
  if (detail.temperature_k !== null && detail.temperature_k !== undefined) body.temperature_k = detail.temperature_k;
  if (detail.note) body.note = detail.note;
  try {
    await api.resumeRun(runId, body);
    monitorStatus = null;
  } catch (error) {
    // A 409 means the pause has an answer or the run has ended.
    monitorStatus = { run_id: runId, level: 'bad', text: error.text || error.message };
  } finally {
    // Releasing the guard after any response lets the operator retry a refusal with a
    // correctable cause.
    resuming = false;
  }
  drawMonitor(store.getState());
}

/**
 * The power panel remembers the requested monitor state across reloads and
 * reconnects. The displayed running state comes from `bench.monitors`, not that
 * preference.
 */
const POWER_KEY = 'bace.power';
let powerUi = loadPowerUi();
let powerStatus = null;
let powerBusy = false;

function loadPowerUi() {
  try {
    const raw = localStorage.getItem(POWER_KEY);
    return raw ? { ...POWER_DEFAULTS, ...JSON.parse(raw) } : { ...POWER_DEFAULTS };
  } catch { return { ...POWER_DEFAULTS }; }
}

function savePowerUi(patch) {
  powerUi = { ...powerUi, ...patch };
  try { localStorage.setItem(POWER_KEY, JSON.stringify(powerUi)); } catch { /* Browser storage can be unavailable in private windows. */ }
  drawPower(store.getState());
}

function drawPower(state) {
  renderPowerPanel(powerEl, state, powerUi, {
    status: powerStatus,
    onToggle: togglePower, onInterval: setPowerInterval,
    onWindow: (key) => savePowerUi({ window: key }),
    onChart: (open) => savePowerUi({ chart: open }),
    onZero: (on) => savePowerUi({ fromZero: on }),
    onClear: clearPowerHistory, onExportCsv: exportPowerCsv, onExportSvg: exportPowerSvg,
  });
}

function powerSay(level, text) {
  powerStatus = text ? { level, text } : null;
  drawPower(store.getState());
}

async function refreshMonitors() {
  try { store.applyMonitors((await api.monitors()).monitors); } catch { /* The bench snapshot supplies monitor state if this query fails. */ }
}

/** The action changes the service monitor state and remembers the preference. */
async function togglePower(on) {
  if (powerBusy) return;
  powerBusy = true;
  savePowerUi({ on });
  try {
    if (on) {
      await api.startMonitor('power', powerUi.interval_s);
      powerSay('ok', `Every ${powerUi.interval_s} s`);
    } else {
      try { await api.stopMonitor('power'); } catch (error) { if (error.status !== 404) throw error; }
      powerSay('', 'Off. Trace kept.');
    }
  } catch (error) {
    powerSay('bad', error.text || error.message);
  } finally {
    powerBusy = false;
    await refreshMonitors();
  }
}

/** Changing the interval restarts an active monitor without clearing its trace. */
async function setPowerInterval(seconds) {
  savePowerUi({ interval_s: seconds });
  const running = (store.getState().monitors || []).some((m) => m.name === 'power' && m.running);
  if (!running || powerBusy) return;
  powerBusy = true;
  try {
    try { await api.stopMonitor('power'); } catch (error) { if (error.status !== 404) throw error; }
    await api.startMonitor('power', seconds);
    powerSay('ok', `Every ${seconds} s`);
  } catch (error) {
    powerSay('bad', error.text || error.message);
  } finally {
    powerBusy = false;
    await refreshMonitors();
  }
}

async function clearPowerHistory() {
  try {
    const out = await api.clearPowerHistory();
    store.clearPowerLog();
    powerSay('', `${fmt.plural(out.cleared, 'reading')} cleared. The journal keeps them.`);
  } catch (error) {
    powerSay('bad', error.text || error.message);
  }
}

/** The download includes the complete history retained by the service. */
function exportPowerCsv() {
  const a = h('a', { href: api.powerHistoryCsv(), download: '' });
  document.body.append(a);
  a.click();
  a.remove();
}

function exportPowerSvg() {
  const svg = powerEl.querySelector('.pw-chart svg');
  if (!svg) return;
  const css = getComputedStyle(document.documentElement);
  const tokens = {};
  for (const name of ['--ink', '--grey', '--rule', '--fill', '--grid', '--paper', '--accent', '--alert', '--warn', '--ok', '--font-num', '--font-body']) {
    tokens[name] = css.getPropertyValue(name).trim();
  }
  const blob = new Blob([svgFileOf(svg, tokens)], { type: 'image/svg+xml' });
  const url = URL.createObjectURL(blob);
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const a = h('a', { href: url, download: `power_${stamp}.svg` });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/**
 * Each connection merges service history by `ts` and reapplies the monitor
 * preference, including after a service restart.
 */
let syncingPower = false;

async function syncPower() {
  if (syncingPower) return;
  syncingPower = true;
  try {
    try { store.applyPowerHistory(await api.powerHistory()); } catch (error) {
      console.warn('power history:', error.message);
    }
    const running = (store.getState().monitors || []).some((m) => m.name === 'power' && m.running);
    if (powerUi.on && !running && !powerBusy) {
      powerBusy = true;
      try {
        await api.startMonitor('power', powerUi.interval_s);
        powerSay('ok', `monitoring every ${powerUi.interval_s} s`);
      } catch (error) {
        if (error.status !== 409) powerSay('bad', error.text || error.message);
      } finally {
        powerBusy = false;
        await refreshMonitors();
      }
    }
  } finally {
    syncingPower = false;
  }
}

/**
 * The run record fills gaps in a replay. The ring holds 5000 envelopes
 * (`docs/service-contract.md` §3), so a long scan can lose its opening `AxisResolved`
 * or events during a disconnect. Both cases need the record.
 */
async function hydrate(runId, nodePath) {
  if (!runId || hydrating.has(runId)) return;
  hydrating.add(runId);
  try {
    store.applyRunRecord(await api.run(runId));
    store.applyRunData(runId, nodePath, await api.runData(runId, nodePath || undefined));
  } catch (error) {
    console.warn(`could not hydrate ${runId}:`, error.message);
  } finally {
    hydrating.delete(runId);
  }
}

const hydrating = new Set();
let gapsSeen = 0;

/**
 * `lib/watch.js` keeps the rail current during a run. It owns frame selection,
 * refetch throttling and replay handling.
 */
const watch = createBenchWatch({
  fetchBench: () => api.bench(),
  fetchModules: () => api.modules(),
  onBench: (bench) => store.applyBench(bench, { readBack: true }),
  onModules: (payload) => store.applyModules(payload),
});

function afterFrame(frame, { replay = false } = {}) {
  // A failed run needs a message in the persistent strip. Its monitor disappears at
  // park, so a message there alone can vanish before the operator reads it.
  if (frame.type === 'RunFailed' && !replay) {
    notify(`${frame.run_id} failed · ${(frame.data && frame.data.error) || 'no reason given'}`, 'crit');
  } else if (frame.type === 'RunAborted' && !replay) {
    const d = frame.data || {};
    notify(`${frame.run_id} ${d.reason === 'requested' ? 'stopped' : 'aborted'} · ${d.done ?? '?'} of ${d.total ?? '?'} shots`, 'warn');
  }
  if (frame.type === 'RunStateChanged' && frame.data && frame.data.state === 'parked') {
    monitorStatus = null;
    armedRun = null;
  }
  // A ring gap means the active run can be missing its opening events.
  const { stats } = stream.state;
  if (stats && stats.gaps > gapsSeen) {
    gapsSeen = stats.gaps;
    const state = store.getState();
    const active = state.activeRunId && state.runs[state.activeRunId];
    if (active && !active.axis) hydrate(state.activeRunId, active.node_path);
  }
  if (frame.type === 'PlanProposed' || frame.type === 'PlanDecided'
      || (frame.type === 'RunQueued' && frame.data && frame.data.plan)) {
    plansPanel.refresh();
  }
  watch.frame(frame);
}

function runLabel(state) {
  const run = currentRun(state);
  if (!run) return fmt.ABSENT;
  const counts = run.kept === null && run.requested === null ? '' : ` · ${fmt.keptOf(run.kept, run.requested)}`;
  return `${run.module || run.kind || 'run'} ${run.state || ''}${counts}`;
}

function renderBar(state) {
  // Counters update from every stream frame. A socket-status callback only fires on
  // connection changes and would leave the frame count stale during a scan.
  const c = { ...(state.connection || {}), ...stream.state };
  const stats = c.stats || {};
  barEl.className = 'stream-bar ' + (c.state || 'idle');
  // The connection state is the reading; session, sequence and frame counts are the
  // bar's title. Gaps, drops and replays stay visible because they need attention.
  const STATE = { live: 'Connected', connecting: 'Connecting', offline: 'Offline, retrying',
                  behind: 'Catching up', closed: 'Disconnected', idle: 'Idle' };
  keyed(barEl, JSON.stringify([c.state, c.session, c.lastSeq, c.seq, stats]), () => {
    barEl.title = `session ${c.session || fmt.ABSENT}\nseq ${c.lastSeq ?? c.seq ?? fmt.ABSENT}\n`
      + `${fmt.plural(stats.frames || 0, 'frame')}`
      + (stats.duplicates ? `\n${stats.duplicates} deduped` : '');
    return [
      h('span.dot'),
      h('span', { text: STATE[c.state] || c.state || 'Idle' }),
      stats.gaps ? h('span', { text: fmt.plural(stats.gaps, 'gap') }) : null,
      stats.drops ? h('span', { text: `${fmt.plural(stats.drops, 'drop')}, ${fmt.plural(stats.reconnects, 'reconnect')}` }) : null,
      stats.tracesGone ? h('span', { text: `${fmt.plural(stats.tracesGone, 'trace')} replayed without arrays` }) : null,
    ];
  });
}

/**
 * The shell tracks whether a run ending needs attention in the window title. `away`
 * combines focus and visibility; `unseenEnd` retains an ending while the operator is
 * elsewhere and clears on return. A pause remains visible even in a focused window
 * because it requires an answer.
 */
let away = document.hidden;
let liveRun = null;
let unseenEnd = null;

function drawTitle(state) {
  const holding = liveRunId(state);
  // Comparing draws detects a run ending even if a ring gap omits `parked`. An absent
  // operator needs that ending in the title.
  if (liveRun && liveRun !== holding && away) unseenEnd = liveRun;
  liveRun = holding;
  renderTitle(document, state, { announce: unseenEnd });
}

/** Returning to the window acknowledges the retained run ending. */
function seen() {
  away = false;
  unseenEnd = null;
  drawTitle(store.getState());
}

function gone() { away = true; }

window.addEventListener('focus', seen);
window.addEventListener('blur', gone);
document.addEventListener('visibilitychange', () => (document.hidden ? gone() : seen()));

store.subscribe((state) => {
  renderRail(railEl, state);
  drawIdent(state);
  renderChips(state);
  drawPower(state);
  drawMonitor(state);
  drawStrip(state);
  renderBar(state);
  drawTitle(state);
});
window.addEventListener('hashchange', show);

// HTTP loads the initial bench and catalogue. The socket supplies subsequent events.
// An unavailable service stays visible as offline in the stream bar while reconnect
// attempts continue.
async function boot() {
  show();
  try {
    const bench = await api.bench();
    store.applyBench(bench);
    store.applyModules(await api.modules());
    // The active run can outgrow the ring. Fetching its record before replay restores
    // the axis and counts even when the opening frames are absent.
    if (bench.run && bench.run.run_id) await hydrate(bench.run.run_id, bench.run.node_path);
  } catch (error) {
    console.warn('the service did not answer:', error.message);
  }
  // `since=0` requests retained events so reopening during a run restores more than
  // the `/bench` summary: axes, curves and shots. Replay also captures a run that
  // parks during the initial HTTP requests. The ring bounds the transfer to 5000
  // envelopes; only the last 200 shots retain traces (`docs/service-contract.md` §3).
  // Other shots carry `decimated[…].replay`, so the store redraws from scalars. A
  // full transfer is a few tens of megabytes over loopback.
  stream.start(0);
}

boot();
