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
import { h, fill } from './lib/dom.js';
import * as fmt from './lib/format.js';
import { renderRail, renderChainStrip } from './lib/rail.js';

const store = createStore({ schedule: (fn) => requestAnimationFrame(fn) });
const loaded = [];
let paced = null;

const controls = h('div.card');
const summary = h('div');
// The real rail and the real strip, off the same store: M1 is developed and
// looked at here, with no service and no bench. `bench_running_sim.json` is
// the mid-scan snapshot, and it is the only fixture that carries an inferred
// anything.
const railEl = h('header.rail');
const stripEl = h('div.strip');
const app = document.getElementById('app');
app.replaceChildren(
  h('div.bar', h('span.wordmark', 'bace'), h('span.spacer'),
    h('span.chips', h('span.chip', { text: 'offline replay' }))),
  railEl,
  h('main.view', h('h1', 'offline replay'),
    h('p.lede', 'Fixtures fed into the same store the WebSocket feeds. The journals replay '
      + 'every run semantic and cannot draw a curve — the payload policy keeps the scalars '
      + 'and drops the traces. The recorded streams carry the traces, decimated as the wire '
      + 'sends them, and the StepPhase frames that are live-only and exist nowhere else.'),
    controls, summary),
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
    h('p', h('button', { onclick: () => { store.reset(); loaded.length = 0; renderControls(''); } }, 'reset store'),
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
    else if (entry.kind === 'bench') {
      // `readBack`, exactly as a mid-run refetch lands: the instruments and
      // the chain, and not the run, the queue or the bench state.
      store.applyBench(frames, { readBack: loaded.length > 0 });
      renderControls(`bench applied — ${(frames.inferred || []).length} inferred: ${(frames.inferred || []).join(', ') || 'none'}`);
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

store.subscribe((state) => {
  renderRail(railEl, state);
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
