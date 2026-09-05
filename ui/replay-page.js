// The offline bench, as a page. `docs/ui-plan.md` M0: "this is a deliverable,
// not a test written later — from here on every phase has a deterministic
// real-data bench to develop against, and the UI can be worked on with no
// service running."
//
// The journals need the repo root on the server, because they live in
// `acceptance/` and are not copied into `ui/`:
//
//     python3 tools/serve_ui.py          # repo root, then /ui/replay.html
//
// Under the service's own `--ui` mount only the `fixtures/` entries load; the
// journals 404 there, which is the honest outcome — with a service running you
// have the real stream.

import { createStore, currentRun } from './lib/store.js';
import { FIXTURES, loadFixture, replayInto, replayPaced } from './lib/replay.js';
import { h, fill, keyed } from './lib/dom.js';
import * as fmt from './lib/format.js';
import { renderRail, renderChainStrip } from './lib/rail.js';
import { renderMonitor } from './lib/monitor.js';
import { chart } from './lib/charts/frame.js';
import { transientModel } from './lib/charts/transient.js';
import { jvModel } from './lib/charts/jv.js';
import { timingModel } from './lib/charts/timing.js';
import { loopsModel } from './lib/charts/loops.js';
import { scheduleModel } from './lib/charts/schedule.js';
import { costModel, scheduleTree, timeline } from './lib/tree.js';
import { pulseDelayS, runFor, shotBlock, valuesOf } from './lib/results.js';
import { gridModel, summaryModel, runGroups, moduleNodes, GRID_MODULES } from './lib/history.js';
import { gridChartModel } from './lib/charts/grid.js';
import { renderGroups, renderRunHead, renderGrid, renderSummary, renderNodes, renderNodePanel } from './lib/grid.js';

const store = createStore({ schedule: (fn) => requestAnimationFrame(fn) });
const loaded = [];
let paced = null;

const controls = h('div.card');
const summary = h('div');
const charts = h('div.card');
const resultsEl = h('div.card');
/** The results tab's selection, offline: which record, which node. */
let openRecord = null;
let openNode = null;
/** The two endpoint fixtures, which are payloads rather than frames. */
const payloads = {};
// The real rail and the real strip, off the same store: M1 is developed and
// looked at here, with no service and no bench. `bench_running_sim.json` is
// the mid-scan snapshot, and it is the only fixture that carries an inferred
// anything.
const railEl = h('header.rail');
// The real monitor too (M4): a paced replay of the tree fixture is the one
// place the pauses, the counters at three scales and the segment indicator
// can be watched with no service — the prompt's Resume says what it would
// have posted, since nothing here can answer it.
const monitorEl = h('section.monitor', { hidden: true });
const stripEl = h('div.strip');
const app = document.getElementById('app');
app.replaceChildren(
  h('div.bar', h('span.wordmark', 'bace'), h('span.spacer'),
    h('span.chips', h('span.chip', { text: 'offline replay' }))),
  railEl, monitorEl,
  h('main.view', h('h1', 'offline replay'),
    h('p.lede', 'Fixtures fed into the same store the WebSocket feeds. The journals replay '
      + 'every run semantic and cannot draw a curve — the payload policy keeps the scalars '
      + 'and drops the traces. The recorded streams carry the traces, decimated as the wire '
      + 'sends them, and the StepPhase frames that are live-only and exist nowhere else.'),
    controls, charts, resultsEl, summary),
  stripEl);

function renderControls(status) {
  fill(controls,
    h('h2', 'fixtures'),
    h('table.rows', FIXTURES.map((entry) => h('tr',
      h('th', h('button', { onclick: () => load(entry, { pace: false }) }, 'fold')),
      h('td', entry.kind === 'journal' || entry.kind === 'stream'
        ? h('button', { onclick: () => load(entry, { pace: true }) }, 'replay paced') : null),
      h('td', { text: entry.kind }),
      h('td', { text: entry.label }),
      h('td.num', { text: loaded.includes(entry.key) ? 'loaded' : '' })))),
    h('p', h('button', { onclick: () => { store.reset(); loaded.length = 0; renderControls('');
renderCharts(); } }, 'reset store'),
      ' ', h('span.absent', { text: status || '' })));
}

async function load(entry, { pace }) {
  if (paced) { paced.stop(); paced = null; }
  renderControls(`loading ${entry.url} …`);
  let frames;
  try {
    frames = await loadFixture(entry);
  } catch (error) {
    return renderControls(`${error.message} — serve the repo root for the journals: python3 tools/serve_ui.py`);
  }
  if (!Array.isArray(frames)) {
    // `hello_*.json` is one frame; `bench_*.json` is a `GET /bench` answer,
    // which the store takes as a read-back; `jv_*.json` is an endpoint's
    // answer and not a frame at all — it is shown as what it is.
    if (frames.type === 'Hello') { store.applyHello(frames); renderControls('Hello folded'); }
    else if (entry.kind === 'modules') {
      // The catalogue, so the bench tab's cards can be generated with no
      // service at all — which is the whole point of this page.
      store.applyModules(frames);
      renderControls(`${(frames.modules || []).length} modules, `
        + `${(frames.modules || []).reduce((n, m) => n + m.params.length, 0)} parameters`);
    } else if (entry.kind === 'bench') {
      // `readBack`, exactly as a mid-run refetch lands: the instruments and
      // the chain, and not the run, the queue or the bench state.
      store.applyBench(frames, { readBack: loaded.length > 0 });
      renderControls(`bench applied — ${(frames.inferred || []).length} inferred: ${(frames.inferred || []).join(', ') || 'none'}`);
    } else if (entry.kind === 'validate') {
      // `POST /pipelines/validate`. Not frames and not a run: a tree nobody
      // ran, with the schedule and the cost the pipeline tab draws from. It
      // is the one endpoint whose fixture is the whole screen — M5's time
      // bar, its three scales and its "at least" all come out of this.
      payloads[entry.key] = frames;
      renderCharts();
      renderControls(`${entry.url} — ${frames.counters.modules} module runs, `
        + `${(frames.schedule || []).length} steps, ${frames.checks.length} checks, `
        + `cost ${frames.cost.lower_bound ? 'a floor' : 'known'}`);
    } else if (entry.kind === 'index' || entry.kind === 'record') {
      // `GET /runs` and `GET /runs/{id}`: what the results tab (M6) reads,
      // and nothing a store folds -- the grid is drawn from the record's
      // `nodes`, the same shape from this process and from a journal file.
      payloads[entry.key] = frames;
      if (entry.kind === 'record') { openRecord = entry.key; openNode = null; }
      renderResults();
      renderControls(entry.kind === 'index'
        ? `${entry.url} — ${frames.length} rows, ${new Set(frames.map((r) => r.session_id)).size} session(s)`
        : `${entry.url} — ${Object.keys(frames.nodes || {}).length} nodes, ${frames.state}`);
    } else if (entry.kind === 'data') {
      // `GET /runs/{id}/data`: full precision, and the only place the running
      // integral and the J–V arrays exist offline. The journals cannot draw a
      // curve at all — the payload policy keeps the scalars and drops the
      // traces — so M3 develops against these two.
      payloads[entry.key] = frames;
      renderCharts();
      renderControls(`${entry.url} folded as an endpoint payload — ${Object.keys(frames).join(', ')}`);
    } else renderControls(`${entry.url} is an endpoint payload, not frames — ${Object.keys(frames).join(', ')}`);
    loaded.push(entry.key);
    return;
  }
  if (pace) {
    paced = replayPaced(store, frames, { speed: 20, onDone: () => renderControls('paced replay done') });
    renderControls('replaying …');
  } else {
    const counts = replayInto(store, frames);
    renderControls(`${counts.folded} frames folded, ${counts.hellos} Hello`);
  }
  loaded.push(entry.key);
}

/**
 * The M3 charts, off whatever this page has been given.
 *
 * The timing diagram needs only the catalogue and the bench — it is a function
 * of the form, so it draws before anything has run. The transient and the J–V
 * need arrays, which means an endpoint fixture or a recorded stream; a journal
 * replay leaves them saying so, which is the honest outcome and the reason the
 * fixture set has both kinds.
 */
function renderCharts() {
  const state = store.getState();
  const run = currentRun(state);
  const entry = state.modules.byName.bace;
  const bace = runFor(state, 'bace');
  const jvNode = runFor(state, 'jv_bace') || runFor(state, 'jv');
  const shot = (bace && bace.node.lastShot) || (run && run.lastShot) || null;
  const curves = (jvNode && jvNode.node.curves.length ? jvNode.node.curves
    : (payloads['jv-curves'] || {}).curves) || [];
  const key = [
    entry ? JSON.stringify(valuesOf(entry)) : 'no-modules',
    state.bench ? state.bench.read_at : 'no-bench',
    shot ? `${shot.node_path}:${shot.loop}:${shot.index}:${shot.ts}` : 'no-shot',
    payloads.transient ? 'rig-transient' : '-',
    ['validate-txill', 'validate-bound', 'validate-nested'].filter((k) => payloads[k]).join(','),
    `curves:${curves.length}`,
    bace ? `${bace.node.shots.length}:${bace.node.loops.length}:${bace.node.outcome || ''}:${bace.record.state || ''}` : 'no-node',
  ].join('|');
  keyed(charts, key, () => {
    const rig = (state.bench && state.bench.rig && state.bench.rig.values) || {};
    const chain = (state.bench && state.bench.chain) || {};
    const values = entry ? valuesOf(entry) : null;
    const timing = values ? timingModel(values, { rig, chain }) : null;
    const source = payloads.transient || shot;
    return [
      h('h2', 'charts · M3'),
      h('p.chart-note', 'the timing diagram is a function of the form and draws with no run at all; '
        + 'the transient and the J–V need arrays, which a journal does not carry'),
      timing ? h('h3', 'the shot this form describes') : h('p.absent', 'load the modules fixture for the timing diagram'),
      timing ? chart(timing) : null,
      timing && timing.alerts.length
        ? h('div.alerts', timing.alerts.map((a) => h('div.warn1.' + a.level,
          h('span.code', { text: a.level }), h('span', { text: a.text }))))
        : null,
      bace && bace.node.lastShot ? h('h3', 'the newest shot · M4') : null,
      bace && bace.node.lastShot ? shotBlock(bace.node.lastShot, bace, (bace.record.config && bace.record.config.run) || {}) : null,
      h('h3', 'the transient'),
      source
        ? chart(transientModel(source, values ? {
          t0_int_s: values.t0_int_s, t_int_width_s: values.t_int_width_s,
          pulse_delay_s: pulseDelayS(shot, values, rig),
          offset_corrected: values.offset_correct, dark_reference: values.dark_reference,
        } : {}))
        : h('p.absent', 'load the rig transient fixture, or replay a recorded stream'),
      h('h3', 'Q per loop, or Q(axis) · M4'),
      bace ? chart(loopsModel(bace)) : h('p.absent', 'replay a recorded bace stream — the stopped one shows kept of requested'),
      h('h3', 'the J–V'),
      curves.length ? chart(jvModel(curves)) : h('p.absent', 'load the J-V fixture, or replay the jv stream'),
      h('h3', 'the run as a length of time · M5'),
      h('p.chart-note', 'a settle nobody has measured is hatched and takes no time on the axis, '
        + 'and there is no clock under a total that is a floor — `cost.finish_at` is null exactly '
        + 'when `lower_bound` is set'),
      ...['validate-txill', 'validate-bound', 'validate-nested'].filter((k) => payloads[k]).map((k) => chart(scheduleModel({
        blocks: timeline(scheduleTree(payloads[k].schedule)),
        cost: costModel(payloads[k].cost),
      }))),
      payloads['validate-txill'] || payloads['validate-bound'] || payloads['validate-nested']
        ? null : h('p.absent', 'load a validate fixture for the schedule bar'),
    ];
  });
}

/**
 * The results tab, off the two endpoint fixtures — M6's grid, summary,
 * node list and panel, drawn by the same functions the tab uses. Re-queue
 * says what it would have posted, since nothing here can answer it.
 */
function renderResults() {
  const rows = payloads['runs-index'];
  const record = openRecord ? payloads[openRecord] : null;
  const key = [rows ? rows.length : 0, openRecord, openNode].join('|');
  keyed(resultsEl, key, () => {
    const kids = [h('h2', 'results · M6'),
      h('p.chart-note', 'GET /runs, grouped by session, and one GET /runs/{id}: the T x LED grid with '
        + 'the partial cell outlined and the never-run cell hatched, the summary strip, and every '
        + 'flag with its reason — no folder name parsed, no HDF5 opened')];
    if (rows) {
      kids.push(h('h3', 'runs'), renderGroups(runGroups(rows), {
        selected: record ? record.run_id : null,
        onSelect: (id) => renderControls(`offline: the tab would GET /runs/${id}`),
      }));
    }
    if (!record) {
      kids.push(h('p.absent', 'load a GET /runs/{id} record for the grid'));
      return kids;
    }
    const grid = gridModel(record);
    const summary = summaryModel(grid, record);
    const nodes = moduleNodes(record);
    if (!nodes.some((n) => n.node_path === openNode)) {
      const cell = nodes.find((n) => GRID_MODULES.has(n.module));
      openNode = (cell || nodes[0] || {}).node_path ?? null;
    }
    const node = nodes.find((n) => n.node_path === openNode) || null;
    const select = (path) => { openNode = path; renderResults(); };
    kids.push(renderRunHead(record, grid, summary),
      h('div.rbody',
        h('div.rleft',
          renderGrid(grid, { selected: openNode, onSelect: select }),
          grid.shape === 'grid' ? renderSummary(summary) : null,
          grid.shape === 'grid' && summary.present > 1 ? chart(gridChartModel(grid, { by: 'led' })) : null,
          h('h3', 'nodes, in the order they ran'),
          renderNodes(record, { selected: openNode, onSelect: select })),
        h('div.rright', renderNodePanel(record, node, grid, {
          onRerun: (n) => renderControls(`offline: the tab would POST /runs {module: ${n.module}, params: this node's overrides}`),
        }))));
    return kids;
  });
}

store.subscribe((state) => {
  renderRail(railEl, state);
  renderMonitor(monitorEl, state, {
    onStop: (id, mode) => renderControls(`offline: the monitor would POST /runs/${id}/stop {mode: ${mode}}`),
    onAbort: (id) => renderControls(`offline: the monitor would POST /runs/${id}/stop {mode: abort}`),
    onResume: (id, detail) => renderControls(`offline: the monitor would POST /runs/${id}/resume ${JSON.stringify(detail)}`),
  });
  renderCharts();
  renderChainStrip(stripEl, state, {
    // Offline there is no service to answer, so the strip says what it would
    // have posted rather than pretending to have posted it.
    onFix: (name) => renderControls(`offline: the strip would POST /bench/actions/${name}`),
    onPark: () => renderControls('offline: the strip would POST /bench/actions/park'),
  });
  const run = currentRun(state);
  fill(summary,
    h('div.card',
      h('h2', `runs · ${state.order.length}`),
      h('table.rows',
        h('tr', h('th', 'run'), h('th', 'kind'), h('th', 'module'), h('th', 'state'),
          h('th', 'kept/requested'), h('th', 'shots'), h('th', 'curves'), h('th', 'nodes'), h('th', 'error')),
        state.order.map((id) => {
          const record = state.runs[id];
          return h('tr',
            h('th', h('span.num', { text: id })),
            h('td', { text: record.kind || '' }),
            h('td', { text: record.module || '' }),
            h('td', { class: record.state === 'failed' ? 'level-crit' : '', text: record.state || '' }),
            h('td', h('span.num', { text: fmt.keptOf(record.kept, record.requested) })),
            h('td', h('span.num', { text: String(record.shots.length) })),
            h('td', h('span.num', { text: String(record.curves.length) })),
            h('td', h('span.num', { text: String(Object.keys(record.nodes).length) })),
            h('td', { class: 'level-crit', text: record.error ? record.error.text.slice(0, 90) : '' }));
        }))),
    run ? h('div.card',
      h('h2', `newest run · ${run.run_id}`),
      h('table.rows',
        detail('axis', run.axis ? `${run.axis.name} ${run.axis.start} … ${run.axis.stop} step ${run.axis.step}` : fmt.ABSENT),
        detail('V_oc / V', run.voc === null || run.voc === undefined ? fmt.ABSENT : fmt.volts(run.voc, { unit: false })),
        detail('Q / C', run.q_mean ? run.q_mean.map((q) => fmt.charge(q, { unit: false })).join('  ') : fmt.ABSENT),
        // σ_Q = 0 means *not recorded*, and is rendered as an absence, never
        // as a zero-length error bar (`docs/ui-rules.md` §2).
        detail('σ_Q / C', run.q_std ? run.q_std.map((s) => fmt.sigmaQ(s) ?? 'not recorded').join('  ') : fmt.ABSENT),
        detail('shots with traces', String(run.shots.filter((s) => s.traces).length)),
        detail('shots whose traces the ring dropped', String(run.shots.filter((s) => s.tracesGone).length)),
        detail('shot segment', run.phase ? `${run.phase.k}/${run.phase.of} · ${run.phase.phase}` : fmt.ABSENT),
        detail('warnings on shots', String(run.shotWarnings.length)))) : null,
    state.log.length ? h('div.card',
      h('h2', `session log · ${state.log.length}`),
      h('div.log', state.log.slice(-300).reverse().map((line) => h('div',
        h('span.ts', { text: fmt.time(line.ts) }),
        h('span.type', { text: line.type }),
        h('span', { class: 'level-' + line.level, text: line.text || '' }))))) : null);
});

function detail(key, value) {
  return h('tr', h('th', { text: key }), h('td', h('span.num', { text: value })));
}

renderControls('');
renderCharts();
