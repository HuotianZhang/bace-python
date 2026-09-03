// The bench tab — `docs/ui-plan.md` M2.
//
// The rail and the chain strip landed in M1 and are *shell* furniture, not
// this tab's: they are in every view, which is why they came first. What is
// here is the six generated module cards, and M4's live monitor will go inside
// the running one.
//
// This file owns almost nothing. `fields.js` decides which rows a card has,
// `card.js` draws them, and the service owns every value and its provenance.
// What is left is the loop: an edit `PUT`s and re-renders from the entry that
// comes back, a Run posts and lets the stream say what happened, and an action
// posts and re-reads the bench. No value is cached here, because a cached
// value is a second opinion about a number the service is the authority on.

import { h, fill } from './../lib/dom.js';
import { moduleCard } from './../lib/card.js';
import { BENCH_CARDS } from './../lib/fields.js';

export default {
  route: 'bench',
  title: 'bench',

  mount(container, { store, api, notify }) {
    const body = h('div.cards');
    fill(container, body);

    /** Which fold groups are open, per card. Kept here so a re-render — and
     *  every edit is one — does not shut a fold the operator just opened. */
    const open = new Map();
    const opened = (name) => {
      if (!open.has(name)) open.set(name, new Set());
      return open.get(name);
    };

    const fail = (err) => {
      // A refusal reaches the operator as its sentence, never as `-> 409`.
      // `error.text` and `error.checks` are on `ApiError` since M1, and
      // neither of them is `detail`.
      notify(err.text || String(err), err.level || 'warn', err.checks || null);
    };

    const ctx = () => {
      const state = store.getState();
      const run = state.runs[state.activeRunId] || null;
      return {
        bench: state.bench,
        verdicts: state.verdicts || [],
        // Every action but park answers 409 while a run holds the worker, and
        // a Start would queue behind it — so the cards say so rather than
        // offering buttons that will be refused.
        busy: Boolean(run && !run.parked_at),
        toggle(name, group) {
          const set = opened(name);
          if (set.has(group)) set.delete(group); else set.add(group);
          render();
        },
        async edit(name, params) {
          try {
            // The response *is* the new card: value, provenance, `needs` and a
            // freshly computed `estimate_text` with the point count in it.
            store.applyModule(await api.setParams(name, params));
          } catch (err) { fail(err); render(); }
        },
        async run(model, button) {
          try {
            const params = button.kind === 'shot' ? { n_loops: 1 } : {};
            await api.startRun(model.name, params);
          } catch (err) { fail(err); }
        },
        async act(action) {
          try {
            // The action answers with the read-back that followed it, so the
            // card's illumination row and the rail move together and neither
            // waits on a separate GET.
            const out = await api.action(action.action, action.args);
            if (out && out.bench) store.applyBench(out.bench, { readBack: true });
          } catch (err) { fail(err); }
        },
      };
    };

    function render() {
      const state = store.getState();
      const { byName } = state.modules;
      const names = BENCH_CARDS.filter((n) => byName[n]);
      if (!names.length) {
        fill(body, h('div.card', h('p.absent', 'GET /modules has not answered')));
        return;
      }
      const c = ctx();
      fill(body, names.map((name) => moduleCard(byName[name], c, opened(name))));
    }

    const off = store.subscribe(render);
    render();
    return { dispose: off };
  },
};
