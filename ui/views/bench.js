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
import { BENCH_CARDS, cardModel } from './../lib/fields.js';

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

    /**
     * Edits in flight. A text field commits on `change`, which the browser
     * fires during the blur the button click itself causes — so a click on
     * Run, DC or Pulse lands while the `PUT` for the value the operator just
     * typed is still on the wire. Without this, the action goes with the old
     * number while the input visibly shows the new one: for a `light` action,
     * deterministically, since its arguments were computed when the card was
     * last drawn.
     *
     * On a lab bench that is the LED driven at a level nobody chose, so
     * nothing that acts starts until the edits before it have landed. The
     * chain is per view rather than per card because an action can read a
     * value from any of them.
     *
     * Awaiting is the whole fix, and deliberately the only one: disabling the
     * buttons while a `PUT` is in flight would re-render between the
     * mousedown that blurs the field and the mouseup that clicks, and the
     * click would land on a button that no longer exists — the operator taps
     * Run and nothing happens. Correctness comes from the await; the DOM is
     * left alone until the answer arrives.
     */
    let edits = Promise.resolve();
    const settled = () => edits;

    /**
     * A submission already on the wire. `busy` comes from the store, which
     * only learns of a run when its `RunQueued` frame arrives — so two clicks
     * inside that window both pass the check and the service, which queues
     * every accepted POST, starts *two* experiments. On a rig that is an hour
     * of the sample's life and a second set of files nobody asked for.
     *
     * A plain variable, set before the await and checked synchronously at the
     * top: no re-render, so it cannot eat the click the way disabling the
     * button would.
     */
    let submitting = false;

    /**
     * `POST /pipelines/validate` for each card's own one-node tree, by module.
     *
     * The bench snapshot's `verdicts` are the *chain* checks and nothing else,
     * so filtering those told the card nothing about its own values: a `jv`
     * with `step_v = 0`, or a `bace` with no V_oc in scope, left Run enabled
     * and `/runs` answered 422. `ui-plan` M2 says Start is disabled by
     * `invalid` **and** `crit` — this is what makes that true rather than
     * claimed.
     *
     * The node's params are empty on purpose: the edited layer is already in
     * the catalogue, so an empty-params node validates with exactly the values
     * a Run would use, and the answer cannot drift from the button beside it.
     */
    const checks = new Map();

    function revalidate(names) {
      return Promise.all(names.map(async (name) => {
        try {
          const v = await api.validate({ kind: 'module', module: name, params: {} });
          checks.set(name, v.checks || []);
        } catch (err) {
          // A validator that will not answer must not silently unblock Start:
          // the checks it would have returned are unknown, not absent.
          checks.set(name, [{ level: 'invalid', code: 'validate.unreachable',
            text: err.text || String(err), node_path: name }]);
        }
      })).then(render);
    }

    /** Which cards a bench read-back could have changed the answer for. */
    let validatedAt = null;

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
        // The card's own checks, not the chain's. Empty until the first
        // answer lands, which is why an edit re-validates inside the same
        // chain `run` awaits: Run cannot fire on checks older than the value
        // beside it.
        checksFor: (name) => checks.get(name) || [],
        // Every action but park answers 409 while a run holds the worker, and
        // a Start would queue behind it — so the cards say so rather than
        // offering buttons that will be refused.
        busy: Boolean(run && !run.parked_at),
        toggle(name, group) {
          const set = opened(name);
          if (set.has(group)) set.delete(group); else set.add(group);
          render();
        },
        edit(name, params) {
          edits = edits.then(async () => {
            try {
              // The response *is* the new card: value, provenance, `needs` and
              // a freshly computed `estimate_text` with the point count in it.
              store.applyModule(await api.setParams(name, params));
              // Inside the chain, so `run`'s `await settled()` waits for it
              // too: the button and the checks that gate it move together.
              await revalidate([name]);
            } catch (err) { fail(err); render(); }
          });
          return edits;
        },
        async run(model, button) {
          if (submitting) return;         // the second click of a double-click
          submitting = true;
          try {
            await settled();
            const params = button.kind === 'shot' ? { n_loops: 1 } : {};
            await api.startRun(model.name, params);
          } catch (err) { fail(err); } finally { submitting = false; }
        },
        async act(model, action) {
          await settled();
          try {
            // Re-derived after the edits land, never the arguments captured
            // when the card was drawn: `cardModel` computes an action's args
            // from the values it was given, and those are one `PUT` old the
            // moment an edit is in flight.
            const entry = store.getState().modules.byName[model.name];
            const fresh = entry
              ? (cardModel(entry, { bench: store.getState().bench }).actions
                .find((a) => a.action === action.action) || action)
              : action;
            // The action answers with the read-back that followed it, so the
            // card's illumination row and the rail move together and neither
            // waits on a separate GET.
            const out = await api.action(fresh.action, fresh.args);
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

      // Several of the checks are about the bench rather than the values —
      // an instrument that went away, the chain, a V_oc that has just been
      // measured — so a fresh read-back can change the answer without any
      // edit. Keyed on `read_at` rather than on every frame: a read-back is
      // a job on the worker and happens rarely, where frames do not.
      const at = (state.bench && state.bench.read_at) || null;
      if (at !== validatedAt) {
        validatedAt = at;
        revalidate(names);
      }
    }

    const off = store.subscribe(render);
    render();
    return { dispose: off };
  },
};
