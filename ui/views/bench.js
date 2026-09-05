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

import { h, fill, keyed } from './../lib/dom.js';
import { moduleCard } from './../lib/card.js';
import { BENCH_CARDS, cardModel, cardRuns } from './../lib/fields.js';
import { panelModel, renderInstruments } from './../lib/instruments.js';
import { RESULT_CARDS, createChartThrottle, resultKeys, resultPanel, runFor } from './../lib/results.js';

export default {
  route: 'bench',
  title: 'bench',

  mount(container, { store, api, notify, park, query = {} }) {
    // The Instruments panel first — the switches — then the module cards.
    // Two zones, because they are two different things: a switch acts on the
    // bench the moment it is clicked, a card's Run posts a job the worker
    // runs, and the values on a card are also where every pipeline node of
    // that module starts. The shape says which is which; nothing is labelled.
    const panel = h('div.instruments');
    const body = h('div.cards');
    fill(container, panel, body);

    /**
     * A card asked for by name — `#/bench?module=bace`, the pipeline tab's
     * "→ bench" on a node form: the module every node of it starts from.
     * Kept until the card exists (the catalogue may not have answered yet),
     * then scrolled into view and pinged once. The Figma gesture: an
     * instance has "go to main component", and so does a node.
     */
    let wanted = query.module || null;
    /** The card lit right now, by name: a re-render replaces the element
     *  (the checks land a beat after the catalogue), so the ping is state
     *  the render re-applies, not a class on one element. */
    let ping = null;
    function focus(next) {
      wanted = (next && next.module) || null;
      settle();
    }
    function settle() {
      if (!wanted) return;
      const card = body.querySelector(`[data-module="${wanted}"]`);
      if (!card) return;
      const name = wanted;
      wanted = null;
      card.scrollIntoView({ block: 'start', behavior: 'smooth' });
      ping = { name, until: Date.now() + 1600 };
      card.classList.add('pinged');
      setTimeout(() => {
        ping = null;
        const now = held.get(name);
        if (now) now.el.classList.remove('pinged');
      }, 1600);
    }

    /** Which fold groups are open, per card. Kept here so a re-render — and
     *  every edit is one — does not shut a fold the operator just opened. */
    const open = new Map();
    /** Which fields have their explanation open, as `module:param`. Keyed the
     *  same way the folds are, and for the same reason. */
    const docs = new Set();
    /**
     * Renders held back while a click is in flight.
     *
     * A text field commits on `change`, which fires during the blur the button
     * press itself causes. On localhost the `PUT` answers in about a
     * millisecond, so `store.applyModule` — and then `revalidate` — can redraw
     * the card *between the mousedown and the mouseup*. The pressed button is
     * gone by then, the browser has nothing to dispatch `click` on, and the
     * operator's Run or DC silently does not happen.
     *
     * This is the same failure I used as the argument against *disabling* the
     * buttons, and leaving the normal re-render to do it anyway was no better.
     * So the card is held still from the press until the click has been
     * dispatched, and whatever wanted to redraw in between happens after.
     */
    let pressing = false;
    let missed = false;
    body.addEventListener('pointerdown', (e) => {
      if (e.target.closest('button')) pressing = true;
    });
    // `click` fires after `pointerup`, so release on the later of the two —
    // and on `pointercancel`, or a drag off the button would freeze the card.
    for (const kind of ['click', 'pointercancel']) {
      body.addEventListener(kind, () => {
        pressing = false;
        if (missed) { missed = false; render(); }
      }, true);
    }
    body.addEventListener('pointerup', () => {
      // Nothing landed on a button: no `click` is coming, so release here.
      setTimeout(() => {
        if (!pressing) return;
        pressing = false;
        if (missed) { missed = false; render(); }
      }, 0);
    });

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

    /**
     * `true` once an edit in the current burst has been refused. Awaiting the
     * chain is not enough on its own: the chain has to keep going for the
     * *next* edit, so its `catch` resolves — and a resolved promise tells a
     * waiting Run or DC to carry on, with the value the refused edit did not
     * replace. The operator types something the service will not take, clicks
     * DC, and the lamp is driven at the old number with a refusal on screen.
     *
     * So the flag is the answer, not the promise: an action after a refused
     * edit does nothing, and the next successful edit clears it.
     */
    let refused = false;
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
     *
     * Only cards with a Run are asked. `light` has bench actions and no Run,
     * and its one-node tree is precisely the light-only run the service
     * refuses (`light.undone-by-park`) — validating it would draw that
     * refusal, permanently, under buttons that do not go through the worker.
     */
    const checks = new Map();

    function revalidate(names) {
      return Promise.all(names.filter(cardRuns).map(async (name) => {
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

    /** What a Run or an action does when the edit before it was refused. */
    const stale = () => notify(
      'the edit before this was refused, so nothing was started: the value on '
      + 'the bench is still the old one. Fix the field, or reload the card.',
      'warn');

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
        // Whether the card is built with a result slot beside its form. Not
        // *what* goes in it: the slot is filled after the card is built, and
        // keyed on its own data, so a shot arriving redraws a chart and not a
        // card (`lib/results.js`).
        hasResult: (name) => RESULT_CARDS.has(name),
        toggle(name, group) {
          const set = opened(name);
          if (set.has(group)) set.delete(group); else set.add(group);
          render();
        },
        docOpen: (key) => docs.has(key),
        toggleDoc(key) {
          if (docs.has(key)) docs.delete(key); else docs.add(key);
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
              refused = false;
            } catch (err) { refused = true; fail(err); render(); }
          });
          return edits;
        },
        async run(model, button) {
          if (submitting) return;         // the second click of a double-click
          submitting = true;
          try {
            await settled();
            if (refused) return stale();
            const params = button.kind === 'shot' ? { n_loops: 1 } : {};
            await api.startRun(model.name, params);
          } catch (err) { fail(err); } finally { submitting = false; }
        },
        /**
         * A switch position, as a bench action. The arguments are the
         * panel's own (the `light` module's levels, read at click time) and
         * the answer's read-back is what lights the position — a refusal
         * leaves the switch where the bench is.
         */
        async actInstrument(action, args) {
          await settled();
          if (refused) return stale();
          try {
            const out = await api.action(action, args);
            if (out && out.bench) store.applyBench(out.bench, { readBack: true });
          } catch (err) { fail(err); }
        },
        async act(model, action) {
          await settled();
          if (refused) return stale();
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

    /**
     * The cards on screen, by module name, each with the key it was built
     * from — so a card is rebuilt only when its own model moves.
     *
     * `render` is called on every store notify, and a `--sim --fast` scan
     * notifies once per animation frame for the length of the run. Rebuilding
     * six cards each time was measured at 435 506 DOM elements over one
     * 21 x 180 scan, and the cost is not the point: a rebuilt element is a
     * *different* element, so the operator's caret went with it. Focus a
     * parameter field, start a scan, and 2.5 s later `document.activeElement`
     * was `BODY` — and a power monitor ticking at 1 Hz did the same on an idle
     * bench. `docs/ui-plan.md` decision 5 is what this is.
     *
     * Replaced **in place** rather than by refilling `body`: detaching an
     * element blurs whatever inside it had the focus, so a rebuild of the
     * `bace` card must not take the caret out of the `jv` card beside it.
     */
    const held = new Map();
    let order = '';

    /** The result panels' redraw throttle, and the form key each last drew. */
    const charts = createChartThrottle();
    const drawn = new Map();

    /**
     * Everything `moduleCard` draws from, as one string.
     *
     * `cardModel` is the pure function an entry becomes rows through, so its
     * output *is* what ends up on screen — including the read-back row, which
     * is instrument state and carries no timestamp, so a `/bench` refetch that
     * changed nothing does not move the key. `busy`, the card's own checks and
     * which folds are open are the rest of it.
     *
     * `ctx`'s callbacks are deliberately not in the key: they close over the
     * store and the api rather than over any value, and `act` re-derives its
     * arguments from the store at click time on purpose.
     */
    const cardKey = (entry, c, open, docKeys) => JSON.stringify(
      [cardModel(entry, { bench: c.bench }), c.busy, c.checksFor(entry.name), [...open].sort(), docKeys]);

    /** The panel, keyed on what it draws: the snapshot's three instruments,
     *  the light levels and whether the worker is held. */
    function renderPanel(state, c) {
      const light = state.modules.byName.light || null;
      const model = panelModel(state.bench, light);
      keyed(panel, JSON.stringify([model, c.busy]), () => renderInstruments(model, {
        busy: c.busy,
        act: (action, args) => c.actInstrument(action, args),
        edit: (name, params) => c.edit(name, params),
        park,
      }));
    }

    function render() {
      if (pressing) { missed = true; return; }
      const state = store.getState();
      const { byName } = state.modules;
      const c0 = ctx();
      renderPanel(state, c0);
      const names = BENCH_CARDS.filter((n) => byName[n]);
      if (!names.length) {
        held.clear();
        drawn.clear();
        order = '';
        // Plain `fill`, not `dom.keyed`: `keyed` owns an element's children,
        // and the loop below manages `body`'s directly. Keyed here, `body`'s
        // key would stick at this branch's the first time the catalogue was
        // empty and never be cleared by the populated path — so a `store.reset`
        // (the service restarting under us) would leave the cards it just
        // dropped frozen on the screen, saying nothing about it. It is one
        // cheap element with nothing focusable in it; there is nothing to save.
        fill(body, h('div.card', h('p.absent', 'the module catalogue has not arrived yet')));
        return;
      }
      const c = ctx();
      if (names.join(',') !== order) {
        // The set of cards changed — the first answer from `GET /modules`, or
        // a module that was not in it before. Start the row again.
        order = names.join(',');
        held.clear();
        drawn.clear();
        fill(body);
      }
      for (const name of names) {
        const entry = byName[name];
        const open = opened(name);
        const docKeys = [...docs].filter((k) => k.startsWith(name + ':')).sort();
        const key = cardKey(entry, c, open, docKeys);
        const was = held.get(name);
        if (was && was.key === key) {
          // The card is unchanged; its result may not be. The two are keyed
          // apart on purpose — the chart moves with every shot and the form
          // does not, and rebuilding the form to redraw the chart would take
          // the operator's caret out of a field between two shots.
          renderResult(name);
          continue;
        }
        const card = moduleCard(entry, c, open);
        if (ping && ping.name === name && Date.now() < ping.until) card.classList.add('pinged');
        if (was && was.el.parentNode === body) body.replaceChild(card, was.el);
        else body.append(card);
        held.set(name, { key, el: card });
        renderResult(name);
      }
      settle();

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

    /**
     * The result panel beside one card's form: the timing diagram the form
     * describes, and the transient or the J–V the run produced.
     *
     * `keyed` owns the slot's children, and the slot is a fresh empty element
     * whenever the card around it was rebuilt — so a rebuilt card refills
     * here rather than losing its chart, and an unchanged card redraws only
     * when the data behind the chart moved.
     *
     * Two cadences, because the two halves of that key deserve different
     * ones. Anything the operator did — a keystroke, a bench read-back — draws
     * at once: the timing diagram is a function of the form, and a form whose
     * picture lags half a second behind the typing is not one. A shot draws
     * through the throttle, because `--sim --fast` delivers 130 a second and
     * the measurement said what that costs (`lib/results.js`, `REDRAW_MS`).
     *
     * Everything is re-read inside `paint`, never captured here: a deferred
     * redraw must show the newest shot, and the card element it draws into may
     * have been replaced while it waited.
     */
    function renderResult(name) {
      const holder = held.get(name);
      if (!holder || !holder.el.querySelector('.res')) return;
      const state = store.getState();
      const entry = state.modules.byName[name];
      if (!entry) return;
      const keys = resultKeys(name, entry, runFor(state, name), state.bench);
      const slot = holder.el.querySelector('.res');
      const paint = () => {
        const now = store.getState();
        const current = held.get(name);
        const target = current && current.el.querySelector('.res');
        const fresh = current && now.modules.byName[name];
        if (!target || !fresh) return;
        const found = runFor(now, name);
        const k = resultKeys(name, fresh, found, now.bench);
        drawn.set(name, k.form);
        keyed(target, `${k.form}|${k.data}`,
          () => resultPanel(name, { entry: fresh, found, bench: now.bench }));
      };
      // A slot with no key has just been built with the card around it, and an
      // empty result panel for half a second is a card that looks broken.
      charts.request(name, { immediate: slot.__key === undefined || drawn.get(name) !== keys.form, paint });
    }

    const off = store.subscribe(render);
    render();
    return { dispose() { off(); charts.dispose(); }, focus };
  },
};
