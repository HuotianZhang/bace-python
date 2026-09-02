// The results tab — M6, and a stub until then on purpose.
//
// It is preceded by a design pass: R2·3 is a Round 2 artboard and Round 3 left
// this tab empty, and the standing instruction is to iterate in Claude Design
// with the user before coding a screen that differs from Round 3. Two things
// have to be settled first — R2·3 names a `flags.json` the service does not
// write, and its saturation line predates the 2026-09-02 correction — and by
// M6 the requirements come from real result shapes rather than from guesses.

import { h, fill } from '../lib/dom.js';

export default {
  route: 'results',
  title: 'results',

  mount(container) {
    fill(container,
      h('h1', 'results'),
      h('p.lede', 'M6, after a design pass. Two open questions before R2·3 can be treated '
        + 'as a specification: the flags file it reads does not exist, and its saturation '
        + 'line predates the correction that made a shared extreme evidence of nothing.'),
      h('div.card', h('p.absent', 'not built yet.')));
    return { dispose() {} };
  },
};
