// The rig tab — read-only reference, and the one place "reusable as it stands"
// is true: `ch-rig` in `docs/design/bace-charts-r3.js` is a static schematic
// that ports unchanged. The plan drops this in whenever a phase runs short.

import { h, fill } from '../lib/dom.js';
import * as fmt from '../lib/format.js';

export default {
  route: 'rig',
  title: 'rig',

  mount(container, { store }) {
    const body = h('div');
    fill(container,
      h('h1', 'rig'),
      h('p.lede', 'Reference: the diagram and the read-back. The schematic ports from the '
        + 'design pack as it stands; these are the values under it, from GET /bench.'),
      body);

    const off = store.subscribe((state) => {
      const rig = state.bench && state.bench.rig;
      if (!rig) return fill(body, h('div.card', h('p.absent', 'no read-back yet')));
      fill(body, h('div.card',
        h('h2', `rig.toml · ${rig.fingerprint || ''}`),
        h('table.rows', Object.entries(rig.values || {}).map(([key, value]) => h('tr',
          h('th', { text: key }),
          h('td', h('span.num', { text: typeof value === 'number' ? fmt.sig(value, 6) : String(value) })))))));
    });
    return { dispose: off };
  },
};
