// The shell: the store, the socket, the four tabs and the hash router.
//
// Hash routing, not paths: `--ui DIR` is a `StaticFiles(html=True)` mount with
// no SPA fallback, so `/ui/bench` is a 404 and `#/bench` is not. The views are
// stubs in M0 — `docs/ui-plan.md` cuts the work vertically, and what M0 owes
// is the event layer underneath them, proven against real frames.

import { createApi } from './lib/api.js';
import { createStore, currentRun } from './lib/store.js';
import { createStream } from './lib/stream.js';
import { h, fill } from './lib/dom.js';
import * as fmt from './lib/format.js';

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
  onFrame: (frame) => { store.applyFrame(frame); afterFrame(frame); },
  onStatus: (status) => store.applyConnection(status),
  onSessionChange: ({ from, to }) => {
    // The service restarted. Everything the old session numbered is gone;
    // the new `Hello` rebuilds the bench and the ring supplies the rest.
    console.info(`service session ${from} -> ${to}; rebuilding`);
    store.reset();
  },
});

const railEl = h('header.rail');
const tabsEl = h('nav.tabs');
const viewEl = h('main.view');
const barEl = h('footer.stream-bar');

document.getElementById('app').replaceChildren(railEl, tabsEl, viewEl, barEl);

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
  mounted = BY_ROUTE[name].mount(viewEl, { store, api, stream, fmt });
  renderTabs();
}

function renderTabs() {
  fill(tabsEl, VIEWS.map((view) => h('a', {
    href: `#/${view.route}`,
    class: view.route === mountedRoute ? 'on' : '',
    text: view.title,
  })));
}

function renderRail(state) {
  // M1 replaces this with the real rail — the eight live values, `how:
  // "inferred"` distinct from a read-back, and the relay's own treatment.
  // Until then it says what is known without pretending to be that.
  const session = state.session || {};
  const bench = state.bench || {};
  const instruments = bench.instruments || {};
  const voc = instruments.voc || {};
  fill(railEl,
    h('span.wordmark', 'bace'),
    slot('session', session.id || fmt.ABSENT),
    slot('bench', session.mode ? session.mode + (session.fast ? ' · fast' : '') : fmt.ABSENT),
    slot('state', state.benchState || fmt.ABSENT),
    slot('V_oc / V', voc.value === undefined || voc.value === null ? fmt.ABSENT : fmt.volts(voc.value, { unit: false })),
    slot('T / K', fmt.kelvin(instruments.temperature ? instruments.temperature.kelvin : null, { unit: false })),
    slot('power', instruments.power ? fmt.intensity(instruments.power.watts) : fmt.ABSENT),
    h('span.spacer'),
    slot('queue', String((state.queue || []).length)),
    slot('run', runLabel(state)));
}

/**
 * The bench snapshot is a read-back, and the service re-takes it around a run:
 * the V_oc a `jv_bace` just measured, the module's `last`, the chain read at
 * Start. Nothing on the stream carries the snapshot, so it is re-fetched when
 * a run parks — the one moment it is known to have changed.
 */
function afterFrame(frame) {
  if (frame.type !== 'RunStateChanged' || !frame.data || frame.data.state !== 'parked') return;
  // `readBack`: take the instruments, the chain and the verdicts, and leave
  // the run, the queue and the bench state to the stream — this response and
  // the next run's `preflight` frame race, and the loser must not be the one
  // that cannot arrive out of order.
  api.bench().then((bench) => store.applyBench(bench, { readBack: true })).catch(() => {});
  api.modules().then(store.applyModules).catch(() => {});
}

function slot(key, value) {
  return h('span.slot', h('span.key', { text: key }), h('span.value', { text: value }));
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
  fill(barEl,
    h('span.dot'),
    h('span', { text: c.state || 'idle' }),
    h('span', { text: `session ${c.session || fmt.ABSENT}` }),
    h('span', { text: `seq ${c.lastSeq ?? c.seq ?? fmt.ABSENT}` }),
    h('span', { text: `${stats.frames || 0} frames` }),
    stats.duplicates ? h('span', { text: `${stats.duplicates} deduped` }) : null,
    stats.gaps ? h('span', { text: `${stats.gaps} gaps` }) : null,
    stats.drops ? h('span', { text: `${stats.drops} drops · ${stats.reconnects} reconnects` }) : null,
    stats.tracesGone ? h('span', { text: `${stats.tracesGone} traces replayed without their arrays` }) : null);
}

store.subscribe((state) => { renderRail(state); renderBar(state); });
window.addEventListener('hashchange', show);

// The bench and the catalogue come over HTTP once; everything after that
// arrives on the socket. A service that is not there is not an error worth a
// dialogue — the stream bar says `offline` and keeps retrying.
async function boot() {
  show();
  try {
    store.applyBench(await api.bench());
    store.applyModules(await api.modules());
  } catch (error) {
    console.warn('the service did not answer:', error.message);
  }
  stream.start(null);
}

boot();
