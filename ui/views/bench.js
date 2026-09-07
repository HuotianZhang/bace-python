// The bench tab — `docs/ui-plan.md` M2.
//
// The rail and the chain strip landed in M1 and are *shell* furniture, not
// this tab's: they are in every view, which is why they came first. What is
// here is the six generated module cards, and M4's live monitor will go inside
// the running one.
//
// The one thing here that is neither a card nor a switch is the `MODULES`
// header: the values the cards make live in the catalogue's edited layer,
// which is a session, so an afternoon of tuning six of them went when the
// service did. `POST /bench/save` writes the whole bench to a file,
// `GET /bench/saved` lists them, `DELETE /bench/saved/{name}` removes one;
// putting one back is the ordinary `PUT` a field makes, one module at a time,
// so a value the spec now refuses is refused with the sentence the field
// would have given and the rest of the file still lands. The row carries the
// identity, the `⋯` beside it carries the rest.
//
// This file owns almost nothing. `fields.js` decides which rows a card has,
// `card.js` draws them, and the service owns every value and its provenance.
// What is left is the loop: an edit `PUT`s and re-renders from the entry that
// comes back, a Run posts and lets the stream say what happened, and an action
// posts and re-reads the bench. No value is cached here, because a cached
// value is a second opinion about a number the service is the authority on.

import { h, fill, keyed } from './../lib/dom.js';
import * as fmt from './../lib/format.js';
import { moduleCard } from './../lib/card.js';
import { drift, recorded, restore, savedBenchModel, showValue, fileStem } from './../lib/recipe.js';
import { BENCH_CARDS, cardModel, cardRuns } from './../lib/fields.js';
import { panelModel, renderInstruments } from './../lib/instruments.js';
import { RESULT_CARDS, createChartThrottle, resultKeys, resultPanel, runFor } from './../lib/results.js';

export default {
  route: 'bench',
  title: 'bench',

  mount(container, { store, api, notify, query = {} }) {
    // The Instruments panel first — the switches — then the module cards.
    // Two zones, because they are two different things: a switch acts on the
    // bench the moment it is clicked, a card's Run posts a job the worker
    // runs, and the values on a card are also where every pipeline node of
    // that module starts. The shape says which is which; nothing is labelled.
    const panel = h('div.instruments');
    // The `MODULES` header: the zone label the cards used to get from a CSS
    // `::before`, now a row that also carries which saved bench this is and
    // the `⋯` that saves and opens them. It labels the thing it acts on,
    // which is the whole reason it is here rather than in a zone of its own.
    const savedEl = h('div.saved');
    const body = h('div.cards');
    fill(container, panel, savedEl, body);

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
    // Both zones this view redraws that have a text field beside a button:
    // the cards, and the saved-bench drawer, whose name field commits on the
    // same blur its own Save button causes.
    for (const zone of [savedEl, body]) {
      zone.addEventListener('pointerdown', (e) => {
        if (e.target.closest('button')) pressing = true;
      });
      // `click` fires after `pointerup`, so release on the later of the two —
      // and on `pointercancel`, or a drag off the button would freeze the card.
      for (const kind of ['click', 'pointercancel']) {
        zone.addEventListener(kind, () => {
          pressing = false;
          if (missed) { missed = false; render(); }
        }, true);
      }
      zone.addEventListener('pointerup', () => {
        // Nothing landed on a button: no `click` is coming, so release here.
        setTimeout(() => {
          if (!pressing) return;
          pressing = false;
          if (missed) { missed = false; render(); }
        }, 0);
      });
    }

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

    // -- saved benches -----------------------------------------------------
    //
    // The cards' values are the catalogue's *edited* layer, and that layer is
    // a session: it lives in the process and goes with it. Until now the only
    // way to keep an afternoon of tuning was to save a pipeline recipe, which
    // records the bench for the modules one tree happens to name, as a
    // by-product of saving a structure the operator may not want.
    //
    // So: `POST /bench/save` writes the whole catalogue's values to
    // `<out>/bench/<name>.json`, `GET /bench/saved` lists them,
    // `DELETE /bench/saved/{name}` removes one, and there is **no load
    // route**. Putting a saved bench back is `PUT /modules/{m}/params`, one
    // module at a time — the same edit a field makes — so a value the spec now
    // refuses is refused with the sentence the field would have given, and the
    // modules that took theirs keep them. An all-or-nothing load that fails
    // leaves the operator holding the bench they were trying to replace with
    // no idea which value stopped it.
    //
    // **Where it lives** (`bench-head.html`, option B, 2026-09-06). This was a
    // zone of its own between the switches and the cards: 65 px carrying a
    // name field and a Save button, permanently, for something touched once a
    // session. Worse, a Save sitting between two zones stated the scope of
    // neither — the switches above act on the bench the moment they are
    // clicked, the fields below `PUT` on change, and this one was an explicit
    // Save of *which* of them? So it moved into the `MODULES` header, which is
    // the row that labels the thing it saves. The row carries the identity and
    // nothing else — which saved bench, and whether the bench has moved — and
    // the machinery is behind the `⋯` beside it (`ui-rules` §14's fourth
    // question). `lib/recipe.js: savedBenchModel` is that row, pure.
    //
    // **And the load says what it will do first.** Opening a file never moves
    // a value: it draws the drift — `bace.vpre  saved 1.020 · bench 0.800`,
    // one line each — and the button under the list is what sends the `PUT`s.
    // Replacing the bench is not undoable and is not a read-back, so it is not
    // something to find out about by clicking. That comparison is
    // `lib/recipe.js: drift`, the same one the pipeline tab makes when a
    // recipe is reopened, because a saved bench *is* a recipe record with no
    // tree.

    /** `GET /bench/saved`, newest first. Re-read after every save and delete. */
    let benches = [];
    /** What the next Save will be called, and what opening a file fills in. */
    let benchName = '';
    /** The file being compared with the bench, by name, until it is applied or
     *  dismissed. Opening one never applies it: opening shows the list. */
    let loaded = null;
    /** Whether the `⋯` drawer is open. Not a `<details>` that shuts on
     *  `mouseleave` the way `lib/power.js`'s menu is: that depth suits an
     *  option picked in one click, and this holds a field to type a name into
     *  and a table to read. It stays open until it is closed, or until a save
     *  or a delete finishes. */
    let drawerOpen = false;

    /**
     * An arm-then-confirm in flight, as `{kind, name}` — the six seconds Park
     * uses, and the pipeline tab's Save.
     *
     * `views/pipeline.js` arms one thing and so keeps a bare stem; this row
     * arms two — an overwrite and a delete — so the kind rides with the name.
     * Both halves matter for the same reason that comment gives: a name that
     * changed between the two clicks would otherwise carry the arming to a
     * different file, and a kind that changed would delete what the operator
     * meant to overwrite.
     */
    let armed = null;
    let armedTimer = null;
    const isArmed = (kind, name) => Boolean(armed && armed.kind === kind && armed.name === name);

    function arm(kind, name) {
      armed = kind ? { kind, name } : null;
      clearTimeout(armedTimer);
      armedTimer = kind ? setTimeout(() => { armed = null; render(); }, 6000) : null;
    }

    async function loadBenches() {
      let answer;
      try {
        answer = (await api.savedBench()).benches || [];
      } catch (err) {
        // A list that will not answer is not an empty list. Emptying it would
        // draw "none saved" over a folder full of files and invite a Save
        // under a name that then writes over one with no arming, so the list
        // stays as it was and the strip says the refresh did not land.
        notify(`the saved benches could not be listed · ${err.text || String(err)}`, 'warn');
        render();
        return;
      }
      benches = answer;
      if (loaded && !benches.some((b) => b.name === loaded)) loaded = null;
      render();
    }

    /** Open one against the bench: the comparison, never an apply. */
    function openBench(name) {
      const found = benches.find((b) => b.name === name) || null;
      arm(null);
      if (!found) { loaded = null; return render(); }
      benchName = found.name;
      if (found.error) {
        notify(`${found.name} will not open · ${found.error}`, 'warn');
        loaded = null;
      } else if (!recorded(found)) {
        notify(`${found.name} holds no bench values — there is nothing to put back.`, 'warn');
        loaded = null;
      } else {
        loaded = found.name;
        // No note is drawn for a file that matches, so this is the whole
        // answer to the click; without it, opening one does nothing visible.
        if (!drift(found, store.getState().modules.byName).length) {
          notify(`the bench already matches ${found.name}.`, 'ok');
        }
      }
      render();
    }

    /** A save on the wire, so a double-click writes one file and not two. */
    let saving = false;

    async function save() {
      if (saving) return;                    // the second click of a double-click
      const name = fileStem(benchName);
      if (!name) {
        notify('a saved bench needs a name — type one in the field first.', 'warn');
        return render();
      }
      // A name that is already a file: say what it will replace and take a
      // second click for it. `benches` is the service's list, re-read after
      // every save, so this is what is on disk rather than a guess.
      if (!isArmed('overwrite', name) && benches.some((b) => b.name === name)) {
        const was = benches.find((b) => b.name === name);
        arm('overwrite', name);
        notify(`${name} already exists${was && was.saved_at ? ` — saved ${fmt.clock(was.saved_at)}` : ''}. `
          + 'Click again to write over it, or type another name.', 'warn');
        // `notify` draws the strip and nothing else; the button has to say
        // what the second click will do, or the arming is invisible where the
        // finger already is.
        return render();
      }
      arm(null);
      saving = true;
      try {
        const out = await api.saveBench(name);
        notify(`saved ${out.name} → ${out.path}`, 'ok');
        // The name as the service wrote it, so the next Save over the same
        // file arms rather than replacing it silently.
        benchName = out.name;
        await loadBenches();
        // Saved from this bench, so the file and the bench agree; the note has
        // nothing to say until the bench moves under it.
        loaded = benches.some((b) => b.name === out.name) ? out.name : null;
        drawerOpen = false;
      } catch (err) { fail(err); } finally { saving = false; }
      render();
    }

    /**
     * Delete one, on the second click.
     *
     * Armed like the overwrite beside it, and for more reason: an overwrite
     * replaces a file with the bench in front of the operator, and this leaves
     * nothing. It is the only thing the console removes from disk.
     */
    async function removeBench(name) {
      if (!isArmed('delete', name)) {
        arm('delete', name);
        notify(`${name} will be deleted — the file is not recoverable. Click again to confirm.`, 'warn');
        return render();
      }
      arm(null);
      try {
        await api.deleteBench(name);
        notify(`deleted ${name}`, 'ok');
        if (loaded === name) loaded = null;
        await loadBenches();
      } catch (err) { fail(err); }
      render();
    }

    /**
     * Put the listed values back, one module at a time.
     *
     * Inside the `edits` chain, exactly as a field's edit is, and for the same
     * reason: a Run or a DC clicked while six `PUT`s are on the wire must not
     * go with the bench half-replaced. A module the service refuses leaves
     * `refused` set, which is what stops that Run — and the ones that landed
     * stay landed, because the note redraws from the bench and says what is
     * still unlike the file.
     *
     * The same shape `views/pipeline.js: restoreBench` has, because it is the
     * same act: the drift note's button, sending one `PUT` per module.
     */
    function restoreBench(moved) {
      const bodies = restore(moved);
      const names = Object.keys(bodies);
      const label = loaded || 'the saved bench';
      edits = edits.then(async () => {
        const bad = [];
        for (const [module, params] of Object.entries(bodies)) {
          try {
            // The response *is* the module as the bench now has it, which is
            // what every card and every node form draws from.
            store.applyModule(await api.setParams(module, params));
          } catch (err) { bad.push(module); fail(err); }
        }
        await revalidate(names);
        refused = bad.length > 0;
        notify(bad.length
          ? `${label}: ${fmt.plural(bad.length, 'module')} of ${names.length} refused — the rest is on the bench`
          : `bench set to ${label} · ${fmt.plural(moved.length, 'value')}`,
        bad.length ? 'warn' : 'ok');
        render();
      });
      return edits;
    }

    function renderSaved(state) {
      const m = savedBenchModel(benches, loaded, state.modules.byName);
      keyed(savedEl, JSON.stringify([m, benchName, drawerOpen, armed,
        benches.map((b) => [b.name, b.saved_at, Boolean(b.error)])]),
      () => [savedBar(m), drawerOpen ? savedDrawer(m) : null]);
    }

    /**
     * The `MODULES` header: the zone label, the identity, and the `⋯`.
     *
     * The `⋯` follows the identity rather than sitting at the end of the row.
     * At 1440 the end of the row is 1300 px from the label it belongs to, and
     * a control that far from the thing it acts on is one nobody finds — which
     * is exactly what happened the first time this was built.
     */
    function savedBar(m) {
      const said = {
        none: () => [h('span.none', 'no saved bench')],
        idle: () => [h('span.none', { text: `${fmt.plural(m.count, 'saved bench', 'saved benches')}` })],
        match: () => [h('span.who', { text: m.name }), h('span.match', '✓ matches the bench')],
        drift: () => [h('span.who', m.name, h('span.dot', ' •')),
          h('span.drift', { text: `${fmt.plural(m.moved.length, 'value')} differ${m.moved.length === 1 ? 's' : ''}` })],
      }[m.state]();
      return h('div.modbar',
        h('span.zt', 'Modules'),
        said,
        h('button.dots', {
          type: 'button',
          'aria-expanded': drawerOpen ? 'true' : 'false',
          'aria-label': 'saved benches — save this one, open one, compare it with the bench',
          title: 'saved benches — save this one, open one, compare it with the bench',
          onclick: () => { drawerOpen = !drawerOpen; arm(null); render(); },
        }, '⋯'),
        h('span.sp'));
    }

    /** Save above, open below — the two halves, each saying which it is. */
    function savedDrawer(m) {
      const stem = fileStem(benchName);
      const overwrites = Boolean(stem) && benches.some((b) => b.name === stem);
      const armedSave = isArmed('overwrite', stem);
      return h('div.bdrawer',
        h('div.namerow',
          h('span.l', 'save as'),
          h('input.v', {
            type: 'text', value: benchName,
            // Short enough for the 20em field: a placeholder cut off mid-word
            // is a label that has to be guessed at.
            placeholder: 'the bench as it is now',
            'aria-label': 'the name to save the bench under',
            title: 'the file: <out>/bench/<name>.json',
            onchange: (e) => { benchName = e.target.value; render(); },
          }),
          h('button', {
            class: armedSave ? 'btns armed' : 'btns',
            title: overwrites
              ? `${stem} already exists — saving writes over it`
              : 'writes every module’s values to a file, so they outlive this session',
            onclick: save,
          }, armedSave ? `overwrite ${stem}?` : 'Save bench')),
        h('div.bfiles',
          // Not `.zt`: that is the zone-label face — 9px and uppercased — and
          // a whole sentence set in it is shouting. This is a caption.
          h('p.bcap', { text: benches.length
            ? 'open compares a file with the bench; nothing moves until the button under the list'
            : 'nothing saved yet — name the bench above and Save it' }),
          benches.map((b) => benchRow(b))),
        driftNote(m));
    }

    /** One file: what it is called, when it was written, and what can be done
     *  to it — all within a hand's width of the name, never at the row's end. */
    function benchRow(b) {
      const armedDelete = isArmed('delete', b.name);
      return h('div.brow', { class: b.name === loaded ? 'on' : '' },
        // `· comparing`, not `· open`: `open` is the button two columns along,
        // and the same word for the state and the act reads as one thing.
        h('span.fn', b.name, b.name === loaded ? h('i', ' · comparing') : null),
        h('span.ft', { text: b.error ? 'will not parse' : fmt.clock(b.saved_at) }),
        h('span.fa',
          // `open` is the path walked most — a bench is saved once and put
          // back many times — so it is the bordered button and the other two
          // are quiet beside it.
          h('button.btns', {
            title: 'compare this file with the bench; nothing moves until the button under the list',
            disabled: b.error ? true : null,
            onclick: () => openBench(b.name),
          }, 'open'),
          h('button.btng', {
            title: `write the bench as it is now over ${b.name}`,
            onclick: () => { benchName = b.name; save(); },
          }, 'overwrite'),
          h('button', {
            class: armedDelete ? 'btnd armed' : 'btnd',
            title: armedDelete ? 'click again and the file is gone' : `delete ${b.name}`,
            onclick: () => removeBench(b.name),
          }, armedDelete ? `delete ${b.name}?` : 'delete')));
    }

    /**
     * What opening a file would change, before it changes it.
     *
     * The same shape the pipeline tab's drift note has, and the same
     * comparison behind it — with one difference that matters: there, the note
     * is a reading about a run that will happen anyway, and here it *is* the
     * load. Nothing on the bench moves until the button in its head is
     * clicked, because replacing the bench is not undoable and the operator
     * should not have to click to find out what a file holds.
     */
    function driftNote(m) {
      if (m.state === 'match') {
        return h('div.recipe-note.matched',
          h('div.rn-head',
            h('span', { text: `the bench matches ${m.name} — nothing to put back.` }),
            h('span', { style: { flex: '1' } }),
            h('button.btng', { title: 'stop comparing', onclick: () => { loaded = null; render(); } }, 'close')));
      }
      if (m.state !== 'drift') return null;
      return h('div.recipe-note',
        h('div.rn-head',
          h('span', { text: `${fmt.plural(m.moved.length, 'value')} on the bench `
            + `${m.moved.length === 1 ? 'differs' : 'differ'} from ${m.name}:` }),
          h('span', { style: { flex: '1' } }),
          h('button.btns', {
            title: 'puts these values onto the bench, one module at a time',
            onclick: () => restoreBench(m.moved),
          }, `↺ bench to ${m.name}`),
          h('button.btng', {
            title: 'keep the bench as it is',
            onclick: () => { loaded = null; render(); },
          }, 'keep bench')),
        h('div.rn-list', m.moved.map((it) => h('div.rn-row',
          h('span.l', { text: `${it.module}.${it.name}` }),
          h('span.v', { text: `saved ${showValue(it.saved, it.unit)}` }),
          h('span.v.now', { text: `bench ${showValue(it.now, it.unit)}` }),
          h('i', { text: it.unit })))));
    }

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
      }));
    }

    function render() {
      if (pressing) { missed = true; return; }
      const state = store.getState();
      const { byName } = state.modules;
      const c0 = ctx();
      renderPanel(state, c0);
      // Before the early return below: an empty catalogue is exactly when the
      // operator most wants to see that a saved bench is there to put back.
      renderSaved(state);
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
    // Files, so nothing changes them behind us: read once here and again
    // after every save or delete, rather than on a timer.
    loadBenches();
    return {
      // `armedTimer` — the overwrite/delete arming above. It was
      // `saveArmedTimer`, which is `views/pipeline.js`'s name for its own and
      // is not declared here: a module is strict, so `dispose` threw a
      // `ReferenceError` and took the router's swap down with it. The hash had
      // already changed, so clicking another tab from the bench moved the
      // address bar and left the bench on screen until the page was reloaded.
      dispose() { off(); charts.dispose(); clearTimeout(armedTimer); },
      focus,
    };
  },
};
