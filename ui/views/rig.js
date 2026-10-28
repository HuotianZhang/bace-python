// The rig tab provides chain wiring, a reference shot at four scales and read-back.
// Static drawing values describe measurements on this bench and do not follow form
// edits. `ui/lib/charts/timing.js` on the `bace` card supplies the form-driven
// comparison. Only the swept-axis highlight follows the bench card.

import { h, fill, keyed } from '../lib/dom.js';
import { s } from '../lib/svg.js';
import * as fmt from '../lib/format.js';
import { valuesOf } from '../lib/results.js';
import { chainSvg, shotSvg, AXES } from '../lib/charts/rig.js';

const LEGEND = [
  { label: 'drive · DC · signal', stroke: 'var(--ink)', width: 1.6 },
  { label: 'light', stroke: 'var(--ink)', width: 2, dash: '1 4', cap: 'round' },
  { label: 'trigger', stroke: 'var(--accent)', width: 1.6, dash: '6 3' },
  { label: 'bias', stroke: 'var(--accent)', width: 2 },
  { label: 'not wired', stroke: 'var(--grey)', width: 1, dash: '3 3' },
];

function legend() {
  return h('div.chart-legend', LEGEND.map((e) => h('span.legend-item',
    s('svg', { width: 22, height: 10, style: { display: 'inline-block', verticalAlign: 'middle' } },
      s('line', { x1: 1, y1: 5, x2: 21, y2: 5, stroke: e.stroke, 'stroke-width': e.width,
        'stroke-dasharray': e.dash || null, 'stroke-linecap': e.cap || null })),
    h('span', { text: e.label }))));
}

/**
 * The helper selects the `bace` card's swept axis or the reference default without a
 * card.
 */
export function sweptAxis(state) {
  const entry = state.modules && state.modules.byName && state.modules.byName.bace;
  const axis = entry ? valuesOf(entry).axis_name : null;
  return AXES.includes(axis) ? axis : 'vpre';
}

export default {
  route: 'rig',
  title: 'rig',

  mount(container, { store }) {
    const chain = h('div.card.mod.rig-chain');
    const shot = h('div.card.mod.rig-shot');
    const readback = h('div.rig-readback');
    fill(container,
      h('h1', 'rig', h('span.sub', 'reference — nothing here acts on the bench')),
      h('div.rig', chain, shot),
      readback);

    // The chain never changes while the service runs: drawn once.
    keyed(chain, 'chain', () => [
      h('div.ch', h('span.cn', 'the chain'), h('span.cs', 'who is wired to what')),
      h('div.cb', chainSvg(), legend()),
    ]);

    const off = store.subscribe((state) => {
      // Only swept-axis changes redraw the reference shot. Editing delay does not
      // alter its bench-reference values.
      const axis = sweptAxis(state);
      keyed(shot, axis, () => [
        h('div.ch', h('span.cn', 'one shot · three scales'),
          h('span.cs', 'timing measured 2026-09-01 · swept · ', h('span.accent', { text: axis }))),
        h('div.cb', shotSvg(axis)),
      ]);

      const rig = state.bench && state.bench.rig;
      // `rig.toml` remains constant during the service session, so fingerprint-keyed
      // rendering avoids rebuilding unchanged read-back.
      if (!rig) return keyed(readback, 'none', () => h('div.card', h('p.absent', 'no read-back yet')));
      keyed(readback, rig.fingerprint || JSON.stringify(rig.values), () => h('div.card',
        h('h2', `rig.toml · ${rig.fingerprint || ''}`),
        h('table.rows', Object.entries(rig.values || {}).map(([key, value]) => h('tr',
          h('th', { text: key }),
          h('td', h('span.num', { text: typeof value === 'number' ? fmt.sig(value, 6) : String(value) })))))));
    });
    return { dispose: off };
  },
};
