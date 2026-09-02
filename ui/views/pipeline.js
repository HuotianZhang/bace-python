// The pipeline tab — M5. The tree editor, Dry run against
// `POST /pipelines/validate`, and the schedule as "what it will do, in order",
// including a `temperature` module binding the **rest of the run** rather than
// the rest of one iteration. The cost carries `lower_bound: true`, which
// renders as "at least" and never as a promise.

import { h, fill } from '../lib/dom.js';

export default {
  route: 'pipeline',
  title: 'pipeline',

  mount(container) {
    fill(container,
      h('h1', 'pipeline'),
      h('p.lede', 'M5. The tree editor, the Dry run, and the schedule — with the three '
        + 'counters at three scales, because "step 412 of 8400" is useless here.'),
      h('div.card', h('p.absent', 'not built yet — the event layer it will run on is (M0).')));
    return { dispose() {} };
  },
};
