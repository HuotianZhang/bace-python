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
import { renderRail, renderChainStrip, PREREQUISITE } from './lib/rail.js';
import { renderMonitor } from './lib/monitor.js';
import { createBenchWatch } from './lib/watch.js';

import bench from './views/bench.js';
import pipeline from './views/pipeline.js';
import results from './views/results.js';
import rig from './views/rig.js';

const VIEWS = [bench, pipeline, results, rig];
const BY_ROUTE = Object.fromEntries(VIEWS.map((v) => [v.route, v]));

const api = createApi({});
const store = createStore({ schedule: (fn) => requestAnimationFrame(fn) });

const stream = createStream({
  onHello: (frame) => store.applyHello(frame),
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
const railEl = h('header.rail');
const monitorEl = h('section.monitor', { hidden: true });
const viewEl = h('main.view');
const stripEl = h('div.strip');
const barEl = h('footer.stream-bar');

document.getElementById('app').replaceChildren(
  h('div.bar', h('span.wordmark', 'bace'), tabsEl, h('span.spacer'), chipsEl),
  railEl, monitorEl, viewEl, stripEl, barEl);

let mounted = null;
let mountedRoute = null;

function route() {
  const hash = (location.hash || '').replace(/^#\/?/, '').split('?')[0];
  return BY_ROUTE[hash] ? hash : 'bench';
}

function show() {
  const name = route();
  if (name === mountedRoute) return;
  if (mounted && mounted.dispose) mounted.dispose();
  viewEl.scrollTop = 0;
  mountedRoute = name;
  mounted = BY_ROUTE[name].mount(viewEl, { store, api, stream, fmt, notify });
  renderTabs();
}

function renderTabs() {
  fill(tabsEl, VIEWS.map((view) => h('a', {
    href: `#/${view.route}`,
    class: view.route === mountedRoute ? 'on' : '',
    text: view.title,
  })));
}

function renderChips(state) {
  const session = state.session || {};
  const sample = session.sample || {};
  const chain = (state.bench && state.bench.chain) || null;
  const bad = chain ? chain.total - chain.ok : 0;
  const named = [sample.sample, sample.material, sample.pixel].filter(Boolean).join(' · ');
  const model = [bad && chain ? [chain.ok, chain.total, worstChain(chain)] : null, named,
                 session.mode, session.fast, currentRun(state) ? runLabel(state) : null,
                 state.queue.length, state.benchState];
  keyed(chipsEl, JSON.stringify(model), () => [
    // The chain's own summary rides in the bar, so a check that reads wrong is
    // visible from the pipeline and the results tabs too — the strip that
    // fixes it is at the foot of whichever view is open.
    bad ? h('span.chip.bad', { text: `chain ${chain.ok} / ${chain.total} · ${worstChain(chain)}` }) : null,
    h('span.chip', { text: named || 'no sample named' }),
    h('span.chip', { text: `${session.mode || fmt.ABSENT}${session.fast ? ' · fast' : ''}` }),
    currentRun(state) ? h('span.chip.m', { text: runLabel(state) }) : null,
    state.queue.length ? h('span.chip', { text: `queue ${state.queue.length}` }) : null,
    h('span', { class: 'chip state ' + (state.benchState || 'idle'), text: state.benchState || 'idle' }),
  ]);
}

function worstChain(chain) {
  const bad = (chain.items || []).find((item) => item.level !== 'ok');
  return bad ? `${bad.label} ${bad.value}` : '';
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
  renderChainStrip(stripEl, state, { onFix: fix, onPark: park, status: stripStatus, parkArmed });
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

/** An armed Park disarms itself: it is a confirmation, not a mode. */
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
let abortArmed = false;
let abortArmedTimer = null;
let monitorStatus = null;

function drawMonitor(state) {
  renderMonitor(monitorEl, state, { onStop: stopRun, onAbort: abortRun, onResume: resumeRun, abortArmed, status: monitorStatus });
}

function armAbort(on) {
  abortArmed = on;
  clearTimeout(abortArmedTimer);
  abortArmedTimer = on ? setTimeout(() => { abortArmed = false; drawMonitor(store.getState()); }, 6000) : null;
  drawMonitor(store.getState());
}

async function stopRun(runId, mode) {
  monitorStatus = { level: '', text: `${mode} …` };
  drawMonitor(store.getState());
  try {
    const out = await api.stopRun(runId, mode);
    // The stream says `stopping` the moment the stop was accepted; the
    // status here is only the answer, and it clears with the run.
    monitorStatus = { level: 'ok', text: out.state === 'cancelled' ? 'cancelled' : `${out.mode} accepted` };
  } catch (error) {
    monitorStatus = { level: 'bad', text: error.text || error.message };
  }
  drawMonitor(store.getState());
}

/**
 * Abort discards the shot being acquired and starts nothing after it, which
 * on a rig is a shot of the sample's life thrown away — so, like Park on a
 * busy bench, it arms first and performs on the second click.
 */
async function abortRun(runId) {
  if (!abortArmed) return armAbort(true);
  armAbort(false);
  await stopRun(runId, 'abort');
}

async function resumeRun(runId, detail) {
  monitorStatus = { level: '', text: 'resume …' };
  drawMonitor(store.getState());
  const body = {};
  if (detail.temperature_k !== null && detail.temperature_k !== undefined) body.temperature_k = detail.temperature_k;
  if (detail.note) body.note = detail.note;
  try {
    await api.resumeRun(runId, body);
    monitorStatus = null;
  } catch (error) {
    // 409: not paused — the pause was answered already, or ended with the run.
    monitorStatus = { level: 'bad', text: error.text || error.message };
  }
  drawMonitor(store.getState());
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
    abortArmed = false;
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
    h('span', { text: `${stats.frames || 0} frames` }),
    stats.duplicates ? h('span', { text: `${stats.duplicates} deduped` }) : null,
    stats.gaps ? h('span', { text: `${stats.gaps} gaps` }) : null,
    stats.drops ? h('span', { text: `${stats.drops} drops · ${stats.reconnects} reconnects` }) : null,
    stats.tracesGone ? h('span', { text: `${stats.tracesGone} traces replayed without their arrays` }) : null,
  ]);
}

store.subscribe((state) => {
  renderRail(railEl, state);
  renderChips(state);
  drawMonitor(state);
  drawStrip(state);
  renderBar(state);
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
