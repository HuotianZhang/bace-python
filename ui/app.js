// The shell: the store, the socket, the four tabs and the hash router.
//
// Hash routing, not paths: `--ui DIR` is a `StaticFiles(html=True)` mount with
// no SPA fallback, so `/ui/bench` is a 404 and `#/bench` is not. The views are
// stubs in M0 — `docs/ui-plan.md` cuts the work vertically, and what M0 owes
// is the event layer underneath them, proven against real frames.

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

import bench from './views/bench.js';
import pipeline from './views/pipeline.js';
import results from './views/results.js';
import smu from './views/smu.js';
import rig from './views/rig.js';

// The SMU tab sits between the work surfaces and the reference one, because
// that is what it is: a bench surface that starts nothing. `ui/keithley.html`
// is the same panel in a window of its own.
const VIEWS = [bench, pipeline, results, smu, rig];
const BY_ROUTE = Object.fromEntries(VIEWS.map((v) => [v.route, v]));

const api = createApi({});
const store = createStore({ schedule: (fn) => requestAnimationFrame(fn) });

const stream = createStream({
  // Every connect, not only the first: the power panel's trace and its
  // switch are re-asserted against the service each time (`syncPower`).
  onHello: (frame) => { store.applyHello(frame); syncPower(); },
  onFrame: (frame, meta) => { store.applyFrame(frame); afterFrame(frame, meta || {}); },
  onStatus: (status) => store.applyConnection(status),
  onSessionChange: ({ from, to }) => {
    // The service restarted. Everything the old session numbered is gone;
    // the new `Hello` rebuilds the bench and the ring supplies the rest.
    //
    // The catalogue is not on either, and `store.reset()` has just dropped it
    // — so without this the bench tab reads "GET /modules has not answered"
    // until some later run parks and the watch asks for it. Which is correct
    // and useless: a console with no cards in it, on a bench that is fine.
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

document.getElementById('app').replaceChildren(
  h('div.bar', h('span.wordmark', 'bace'), tabsEl, h('span.spacer'), chipsEl),
  identEl, railEl, powerEl, monitorEl, viewEl, stripEl, barEl);

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
    // Same tab, new query — `#/bench?module=bace` from a node form while
    // the bench is already up. The view answers it without remounting.
    if (mounted && mounted.focus) mounted.focus(query);
    return;
  }
  // The bookkeeping first, and the tabs with it: the hash has already moved,
  // so whatever a view does on the way down or up, the shell has to agree
  // with the address bar afterwards. `swapView` is why (`lib/route.js`).
  const previous = mounted;
  mounted = null;
  mountedRoute = name;
  viewEl.scrollTop = 0;
  renderTabs();
  // Park is the strip's alone: the strip is pinned to the foot of the window
  // and the strip is where it arms, so no view is handed it and none offers a
  // second one.
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
 * The bar's chips. **The trigger chain is not among them** (#42): it was in
 * the bar, on the rail cells as a ⚠, and on the strip with the button that
 * fixes it — one state on three surfaces, which is three places to reconcile
 * and two that can only say something is wrong. The strip is the one that
 * carries the count *and* the fix, and it is at the foot of every view, so
 * the bar's copy went and the rail's ⚠ stays as what it always was: evidence
 * on the value it is about.
 */
function renderChips(state) {
  const session = state.session || {};
  const ident = identityModel(state);
  const model = [ident.chip, ident.unnamed, ident.hazards.length, identOpen,
                 session.mode, session.fast, currentRun(state) ? runLabel(state) : null,
                 state.queue.length, state.benchState];
  keyed(chipsEl, JSON.stringify(model), () => [
    // The identity is a control, not a label. It said `no sample named` for
    // as long as the console has existed and gave the operator nowhere to go
    // with that; now the sentence is the button that answers it.
    h('button', {
      class: 'chip ident' + (ident.unnamed || ident.hazards.length ? ' bad' : '') + (identOpen ? ' on' : ''),
      title: ident.unnamed
        ? 'nothing names the device: every folder this session writes opens on the temperature. Click to name it.'
        : `${ident.preview} — click to change what is mounted`,
      'aria-expanded': identOpen ? 'true' : 'false',
      onclick: () => toggleIdent(),
    }, ident.chip),
    h('span.chip', { text: `${session.mode || fmt.ABSENT}${session.fast ? ' · fast' : ''}` }),
    currentRun(state) ? h('span.chip.m', { text: runLabel(state) }) : null,
    state.queue.length ? h('span.chip', { text: `queue ${state.queue.length}` }) : null,
    h('span', { class: 'chip state ' + (state.benchState || 'idle'), text: state.benchState || 'idle' }),
  ]);
}

/**
 * The identity panel — `lib/identity.js` for why it exists.
 *
 * Open only when asked: the block is set once per device and read constantly,
 * so the bar shows it and the panel edits it. `ui-rules` §1's rule against
 * progressive disclosure is about what the operator needs *while measuring*;
 * five fields nobody touches after the first minute are not that, and the
 * value itself never leaves the bar.
 *
 * It opens itself once when nothing names the device — a console that knows
 * the runs are about to be filed under no name and waits to be asked is the
 * finding this fixes, not a smaller version of it. Once, and never again in
 * this session: a panel that reopened on every reload would be a dialogue,
 * and `--sim` benches genuinely have nothing mounted.
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
 * One key, merged in the service. The answer carries the whole session block,
 * so the store takes it from there rather than waiting for the `SampleNamed`
 * to come back round the socket — and the run holding the bench, which keeps
 * the identity it was queued under and which the panel has to say out loud or
 * the operator will believe they have just fixed it.
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
            ? `${out.changed.join(', ')} · ${out.run_active} keeps the name it was queued under`
            : `${out.changed.join(', ')} · every run queued from now carries it` }
      : { level: '', text: 'unchanged' };
  } catch (error) {
    identStatus = { level: 'bad', text: error.text || error.message };
  }
  drawIdent(store.getState());
  renderChips(store.getState());
}

/**
 * The chain strip's two actions. Neither is ever taken by the console itself:
 * `ui-rules` §3 — a warn states evidence and never blocks, and the fix is the
 * bench action the check names, which the operator clicks.
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
 * What a view says when a call is refused. It goes to the strip, which is at
 * the foot of every view, because that is already where a refusal appears and
 * a second place for the same kind of message is a second place to look. The
 * sentence is the service's own (`error.text`); `checks` is the 422's list,
 * which a refused Start carries and which the operator needs in full.
 */
function notify(text, level = 'warn', checks = null) {
  stripStatus = { level: level === 'crit' || level === 'bad' ? 'bad' : level, text, checks };
  drawStrip(store.getState());
}

/**
 * Clear the strip's sentence.
 *
 * It is the console's one place for a message and it is replaced only by the
 * next one, so a three-hour-old `queued …` and a refusal the operator has
 * already dealt with both keep reading as news. Dismissing is the operator's,
 * never a timer's: a refusal that faded on its own is what this exists beside,
 * not a milder version of it.
 */
function dismiss() {
  stripStatus = null;
  drawStrip(store.getState());
}

/**
 * An armed Park disarms itself: it is a confirmation, not a mode.
 *
 * The flag is the shell's and it is drawn in one place, the strip — which is
 * why arming only has to redraw that. It was two, and the second was a copy
 * of the button in a panel that scrolls away.
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
    // The action answers with the read-back that followed it, so the chain and
    // the rail move without waiting for anything else. `readBack`: the run,
    // the queue and the bench state stay the stream's to say.
    if (out && out.bench) store.applyBench(out.bench, { readBack: true });
    stripStatus = { level: 'ok', text: `${item.label}: ${describe(out)}` };
  } catch (error) {
    // 409 is the bench refusing — the LED is on, or a run holds the worker —
    // and the service says why in a sentence written for this screen, which is
    // shown as it stands. When the sentence names a remedy, the strip offers
    // it as its own click rather than performing it.
    stripStatus = {
      level: 'bad',
      text: error.text || error.message,
      offer: error.refused ? PREREQUISITE[name] || null : null,
    };
  }
  drawStrip(store.getState());
}

/**
 * `result.before` is the read-back's values of what the action changed, which
 * is there so a log can say "33220A :OUTP:POL NORM → INV · by hand" from the
 * journal alone (contract §4). The rail already shows the value now; what the
 * strip adds is what it was.
 */
function describe(out) {
  const before = (out && out.result && out.result.before) || null;
  if (!before) return 'done';
  return Object.entries(before).map(([key, was]) => `${key} was ${was}`).join(' · ') + ' · done';
}

/**
 * Park is allowed at any time, and while a run is active it **aborts that run
 * and cancels the queue** — a person who clicks park wants the bench safe now,
 * not after the runs behind this one. That is not a click to take on a stray
 * mouse, so a busy bench arms the button first and performs it on the second.
 */
async function park() {
  const busy = (store.getState().benchState || 'idle') !== 'idle';
  if (busy && !parkArmed) return armPark(true);
  armPark(false);
  stripStatus = { level: '', text: 'park …' };
  drawStrip(store.getState());
  try {
    const out = await api.action('park');
    if (out && out.bench) store.applyBench(out.bench, { readBack: true });
    stripStatus = out && out.pending
      // The aborted run is inside a VISA call that outlasted the wait. The
      // park is not cancelled; it runs when the worker frees, and saying so is
      // the difference between "nothing happened" and "it is coming".
      ? { level: '', text: 'park queued behind a run still in a VISA call' }
      : { level: 'ok', text: 'parked' };
  } catch (error) {
    stripStatus = { level: 'bad', text: error.text || error.message };
  }
  drawStrip(store.getState());
}

/**
 * The run monitor's three verbs — `docs/ui-plan.md` M4. None is taken by the
 * console on its own; a refusal (409: already ended, or not paused) reaches
 * the operator as the service's sentence, on the monitor itself.
 */
let armedRun = null;
let abortArmedTimer = null;
let monitorStatus = null;

function drawMonitor(state) {
  renderMonitor(monitorEl, state, { onStop: stopRun, onAbort: abortRun, onResume: resumeRun, armedRun, status: monitorStatus });
}

/**
 * Arm the Abort for one run, by id. Not a boolean: `parked` is a frame like
 * any other and a ring gap can swallow it, and a flag that outlived its run
 * would hand the next one an Abort that fires on the first click
 * (`monitor.scopedTo` is what refuses it).
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
    // The stream says `stopping` the moment the stop was accepted; the
    // status here is only the answer, and it clears with the run.
    monitorStatus = { run_id: runId, level: 'ok', text: out.state === 'cancelled' ? 'cancelled' : `${out.mode} accepted` };
  } catch (error) {
    monitorStatus = { run_id: runId, level: 'bad', text: error.text || error.message };
  }
  drawMonitor(store.getState());
}

/**
 * Abort discards the shot being acquired and starts nothing after it, which
 * on a rig is a shot of the sample's life thrown away — so, like Park on a
 * busy bench, it arms first and performs on the second click.
 */
async function abortRun(runId) {
  if (armedRun !== runId) return armAbort(runId);
  armAbort(null);
  await stopRun(runId, 'abort');
}

/**
 * One Resume per pause. A double-clicked button would post twice: the first
 * answers the pause and the second is refused with 409 and dropped (contract
 * §2), leaving an error on screen for a run that resumed perfectly well. A
 * plain variable checked synchronously, not a disabled button — disabling
 * re-renders, and a re-render between the mousedown and the mouseup eats the
 * click, which `views/bench.js` found in M2 and guards its Run the same way.
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
    // 409: not paused — the pause was answered already, or ended with the run.
    monitorStatus = { run_id: runId, level: 'bad', text: error.text || error.message };
  } finally {
    // Released whatever happened: a resume the service refused for a reason
    // the operator can fix is one they have to be able to send again.
    resuming = false;
  }
  drawMonitor(store.getState());
}

/**
 * The power panel — `lib/power.js`. The switch is the operator's wish and the
 * console keeps it: a reload, or the service coming back, re-asserts it, so
 * "on" stays on until somebody switches it off. What the panel *shows* as
 * running is the service's answer (`bench.monitors`), never the wish.
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
  try { localStorage.setItem(POWER_KEY, JSON.stringify(powerUi)); } catch { /* a private window */ }
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
  try { store.applyMonitors((await api.monitors()).monitors); } catch { /* the bench snapshot will say */ }
}

/** Start or stop the monitor in the service, and remember the wish. */
async function togglePower(on) {
  if (powerBusy) return;
  powerBusy = true;
  savePowerUi({ on });
  try {
    if (on) {
      await api.startMonitor('power', powerUi.interval_s);
      powerSay('ok', `monitoring every ${powerUi.interval_s} s`);
    } else {
      try { await api.stopMonitor('power'); } catch (error) { if (error.status !== 404) throw error; }
      powerSay('', 'monitor off · the trace is kept');
    }
  } catch (error) {
    powerSay('bad', error.text || error.message);
  } finally {
    powerBusy = false;
    await refreshMonitors();
  }
}

/** A new interval restarts a running monitor; the trace carries on. */
async function setPowerInterval(seconds) {
  savePowerUi({ interval_s: seconds });
  const running = (store.getState().monitors || []).some((m) => m.name === 'power' && m.running);
  if (!running || powerBusy) return;
  powerBusy = true;
  try {
    try { await api.stopMonitor('power'); } catch (error) { if (error.status !== 404) throw error; }
    await api.startMonitor('power', seconds);
    powerSay('ok', `monitoring every ${seconds} s`);
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
    powerSay('', `cleared ${fmt.plural(out.cleared, 'reading')} · the journal keeps them`);
  } catch (error) {
    powerSay('bad', error.text || error.message);
  }
}

/** The whole history the service holds, as the file the route names. */
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
 * On every connect: the trace the service holds, merged into the log by
 * `ts`, and the switch re-asserted — a monitor the operator switched on
 * is started again after the service restarted under a console that
 * still says on.
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
 * Fill in a run the stream could not rebuild whole.
 *
 * Two cases, one cause: the ring is 5000 envelopes and a long scan is more
 * than that. Opening the console late in one replays a tail with no
 * `AxisResolved` in it, and a client dropped for longer than the ring comes
 * back the same way — the stream counts a gap and says so. Either way the run
 * is asked for its own record.
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
 * Keep the rail alive for the length of a run: `lib/watch.js` owns the whole
 * policy — which frames move the bench, the throttle that collapses them, and
 * why a replayed frame asks like any other.
 */
const watch = createBenchWatch({
  fetchBench: () => api.bench(),
  fetchModules: () => api.modules(),
  onBench: (bench) => store.applyBench(bench, { readBack: true }),
  onModules: (payload) => store.applyModules(payload),
});

function afterFrame(frame, { replay = false } = {}) {
  // A run that ends badly says so where the operator is looking. The monitor
  // is gone the moment the bench is parked, a second later, so the sentence
  // goes to the strip — at the foot of every view — like every other refusal.
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
  // A gap means the ring could not supply what we asked for: whatever is
  // running has a beginning we will never be sent.
  const { stats } = stream.state;
  if (stats && stats.gaps > gapsSeen) {
    gapsSeen = stats.gaps;
    const state = store.getState();
    const active = state.activeRunId && state.runs[state.activeRunId];
    if (active && !active.axis) hydrate(state.activeRunId, active.node_path);
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
  // The counters come off the stream rather than off the store: they move with
  // every frame, and a status callback fires only when the socket's state
  // changes. A bar that said `0 frames` through a scan would be describing the
  // last connection event, not the connection.
  const c = { ...(state.connection || {}), ...stream.state };
  const stats = c.stats || {};
  barEl.className = 'stream-bar ' + (c.state || 'idle');
  keyed(barEl, JSON.stringify([c.state, c.session, c.lastSeq, c.seq, stats]), () => [
    h('span.dot'),
    h('span', { text: c.state || 'idle' }),
    h('span', { text: `session ${c.session || fmt.ABSENT}` }),
    h('span', { text: `seq ${c.lastSeq ?? c.seq ?? fmt.ABSENT}` }),
    h('span', { text: fmt.plural(stats.frames || 0, 'frame') }),
    stats.duplicates ? h('span', { text: `${stats.duplicates} deduped` }) : null,
    stats.gaps ? h('span', { text: fmt.plural(stats.gaps, 'gap') }) : null,
    stats.drops ? h('span', { text: `${fmt.plural(stats.drops, 'drop')} · ${fmt.plural(stats.reconnects, 'reconnect')}` }) : null,
    stats.tracesGone ? h('span', { text: `${fmt.plural(stats.tracesGone, 'trace')} replayed without their arrays` }) : null,
  ]);
}

/**
 * The window title — `lib/title.js` for why it exists at all.
 *
 * The one thing that model cannot get from the store is whether anybody has
 * looked at this window since a run ended, so the shell keeps it. `away` is
 * the window's focus and visibility together (a console behind another window
 * and a console in a background tab are the same case); `unseenEnd` is the run
 * that ended while it was true, and it is dropped the moment the operator
 * comes back — an ending that is still being announced after they have read it
 * is a badge that means nothing the second time.
 *
 * A *pause* is not latched this way and must not be: it stands until it is
 * answered, so the title keeps saying so with the window wide open.
 */
let away = document.hidden;
let liveRun = null;
let unseenEnd = null;

function drawTitle(state) {
  const holding = liveRunId(state);
  // Was live, is not: the run ended. Read across draws rather than off a
  // `parked` frame, which a ring gap can swallow — and this is the only sign
  // of the ending an operator who is not here will get.
  if (liveRun && liveRun !== holding && away) unseenEnd = liveRun;
  liveRun = holding;
  renderTitle(document, state, { announce: unseenEnd });
}

/** They are back: the ending has been delivered, and nothing else is owed. */
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

// The bench and the catalogue come over HTTP once; everything after that
// arrives on the socket. A service that is not there is not an error worth a
// dialogue — the stream bar says `offline` and keeps retrying.
async function boot() {
  show();
  try {
    const bench = await api.bench();
    store.applyBench(bench);
    store.applyModules(await api.modules());
    // A run in progress is older than the ring may be able to say. Ask it for
    // its own record before the replay starts, so the axis and the counts are
    // there whether or not its opening frames survived.
    if (bench.run && bench.run.run_id) await hydrate(bench.run.run_id, bench.run.node_path);
  } catch (error) {
    console.warn('the service did not answer:', error.message);
  }
  // `since=0`, not "no since". Opening or refreshing the console while a run
  // is going has to rebuild that run, and `/bench` carries its summary, not
  // its axis, its curves or its shots — a chart drawn from the live frames
  // alone would start blank in the middle of a scan. The two HTTP requests
  // above are another reason: a run that parks while they are in flight would
  // otherwise leave a record that never stops running.
  //
  // The cost is bounded by the ring the service keeps deliberately small: 5000
  // envelopes, of which only the last 200 shots still carry their traces, so
  // the worst case is one loopback read of a few tens of megabytes and the
  // rest arrive with `decimated[…].replay` set, which the store already knows
  // means *redraw from the scalars*.
  stream.start(0);
}

boot();
