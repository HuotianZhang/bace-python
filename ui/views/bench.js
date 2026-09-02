// The bench tab. M1 puts the pinned rail and the chain strip here, M2 the five
// generated module cards, M4 the live monitor. M0 owes none of that — what it
// shows is what the store already holds, so that the fold can be read off the
// screen rather than off a test.

import { h, fill } from '../lib/dom.js';
import * as fmt from '../lib/format.js';
import { currentRun } from '../lib/store.js';

export default {
  route: 'bench',
  title: 'bench',

  mount(container, { store }) {
    const body = h('div');
    fill(container,
      h('h1', 'bench'),
      h('p.lede', 'M1 puts the pinned rail and the chain strip here — they come before the '
        + 'cards because they are in every view. M2 generates the five module cards from '
        + 'GET /modules. What follows is the store, as the event layer has folded it.'),
      body);

    const off = store.subscribe((state) => fill(body,
      chainCard(state),
      verdictCard(state),
      modulesCard(state),
      runCard(state),
      logCard(state)));
    return { dispose: off };
  },
};

function chainCard(state) {
  const chain = state.bench && state.bench.chain;
  if (!chain) return h('div.card', h('h2', 'chain'), h('p.absent', 'no read-back yet'));
  return h('div.card',
    h('h2', `chain · ${chain.ok}/${chain.total} ok`),
    h('table.rows', chain.items.map((item) => h('tr',
      h('th', { text: item.label }),
      h('td', h('span.num', { text: item.value })),
      h('td', h('span', { class: 'level-' + item.level, text: item.level })),
      h('td', { text: item.expected ? `expected ${item.expected}` : '' }),
      h('td', { text: item.text || '' })))));
}

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
