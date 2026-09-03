// The bench tab. The rail and the chain strip landed in M1 and are *shell*
// furniture, not this tab's — they are in every view, which is why they came
// first. What is left here is M2's: the five generated module cards, and M4's
// live monitor inside the running one.
//
// Until then this shows what the store holds, so the fold can be read off the
// screen rather than off a test. The chain is not repeated here: the strip at
// the foot of the window carries it, with the fix for each check.

import { h, fill, keyed } from '../lib/dom.js';
import * as fmt from '../lib/format.js';
import { currentRun } from '../lib/store.js';

export default {
  route: 'bench',
  title: 'bench',

  mount(container, { store }) {
    // One container per card, each rebuilt only when its own key moves
    // (`dom.keyed`). The store notifies on every batch of frames; the module
    // table changes twice in a run and the log a dozen times, and rebuilding
    // all four sixty times a second is how a screen loses the operator's
    // selection while they are reading a number off it.
    const cards = [h('div'), h('div'), h('div'), h('div')];
    fill(container,
      h('h1', 'bench'),
      h('p.lede', 'The rail above and the strip below are M1, and they are in every view. '
        + 'M2 generates the five module cards from GET /modules and puts Run on each; M4 '
        + 'turns the running one into the monitor. What follows is the store, as the event '
        + 'layer has folded it.'),
      cards);

    const off = store.subscribe((state) => {
      keyed(cards[0], JSON.stringify(state.verdicts), () => verdictCard(state));
      keyed(cards[1], String(state.modules.at), () => modulesCard(state));
      keyed(cards[2], runKey(state), () => runCard(state));
      keyed(cards[3], String(state.logged), () => logCard(state));
    });
    return { dispose: off };
  },
};

function verdictCard(state) {
  const verdicts = state.verdicts || [];
  if (!verdicts.length) return null;
  return h('div.card',
    h('h2', 'verdicts'),
    h('table.rows', verdicts.map((v) => h('tr',
      h('th', h('span', { class: 'level-' + v.level, text: v.level })),
      h('td', { text: v.code }),
      h('td', { text: v.text })))));
}

function modulesCard(state) {
  const { order, byName } = state.modules;
  if (!order.length) return h('div.card', h('h2', 'modules'), h('p.absent', 'GET /modules has not answered'));
  return h('div.card',
    h('h2', `modules · ${order.length}`),
    h('table.rows', order.map((name) => {
      const module = byName[name];
      return h('tr',
        h('th', { text: name }),
        h('td', { text: module.status }),
        h('td', { text: module.kind }),
        h('td', h('span.num', { text: fmt.duration(module.estimate_s) })),
        h('td', { text: `${module.params.length} params` }),
        h('td', { text: module.last ? module.last.summary || module.last.state : '' }));
    })));
}

/** Everything the run card puts on the screen, and nothing else. */
function runKey(state) {
  const run = currentRun(state);
  if (!run) return '';
  return [run.run_id, run.state, run.phase && run.phase.k, run.phase && run.phase.phase,
          run.kept, run.requested, run.shots.length, run.curves.length,
          run.error && run.error.text, run.needsOperator && run.needsOperator.what].join('|');
}

function runCard(state) {
  const run = currentRun(state);
  if (!run) return null;
  const phase = run.phase ? `${run.phase.k} · ${run.phase.phase}` : fmt.ABSENT;
  return h('div.card',
    h('h2', `run ${run.run_id} · ${run.state || ''}`),
    h('table.rows',
      row('module', run.module || run.kind || ''),
      row('node', run.node_path || ''),
      row('shot segment', phase),
      row('kept of requested', fmt.keptOf(run.kept, run.requested)),
      row('shots folded', String(run.shots.length)),
      row('curves', String(run.curves.length)),
      run.error ? row('error', run.error.text) : null,
      run.needsOperator ? row('needs operator', run.needsOperator.what) : null));
}

function row(key, value) {
  return h('tr', h('th', { text: key }), h('td', h('span.num', { text: value })));
}

function logCard(state) {
  if (!state.log.length) return null;
  return h('div.card',
    h('h2', `session log · ${state.log.length}`),
    h('div.log', state.log.slice(-200).reverse().map((line) => h('div',
      h('span.ts', { text: fmt.time(line.ts) }),
      h('span.type', { text: line.type }),
      h('span', { class: 'level-' + line.level, text: line.text || '' })))));
}
