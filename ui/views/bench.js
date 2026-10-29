// The bench tab renders service-owned module values through `ui/lib/fields.js` and
// `ui/lib/card.js`. Edits apply response entries; Run relies on stream events and
// actions use returned read-back. The MODULES row saves the catalogue's
// session-scoped edited layer. Saved benches restore through ordinary per-module
// edits, preserving validation and partial successes (`docs/service-contract.md` §4).
// The row shows file identity and hides infrequent controls behind its menu. No local
// value cache can substitute for the service answer.

import { h, fill, keyed } from './../lib/dom.js';
import * as fmt from './../lib/format.js';
import { moduleCard } from './../lib/card.js';
import { drift, recorded, comparable, restore, savedBenchModel, showValue, fileStem } from './../lib/recipe.js';
import { BENCH_CARDS, cardModel, cardRuns } from './../lib/fields.js';
import { panelModel, renderInstruments } from './../lib/instruments.js';
import { RESULT_CARDS, createChartThrottle, resultKeys, resultPanel, runFor } from './../lib/results.js';

export default {
  route: 'bench',
  title: 'Bench',

  mount(container, { store, api, notify, query = {} }) {
    // Instruments act immediately; cards edit values used by later runs and pipeline
    // nodes. Separate zones make their different consequences visible.
    const panel = h('div.instruments');
    // The MODULES header identifies the whole saved catalogue and keeps saved-bench
    // controls beside their scope.
    const savedEl = h('div.saved');
    const body = h('div.cards');
    fill(container, panel, savedEl, body);

    /**
     * A module query retains its target before catalogue arrival, then scrolls to and
     * highlights its card. Pipeline nodes link here because unoverridden values come
     * from this bench module.
     */
    let wanted = query.module || null;
    /**
     * Highlight state is keyed by module because validation can replace a card
     * element. Rendering reapplies the highlight rather than relying on one element's
     * class.
     */
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

    /**
     * Per-card disclosure state survives re-renders so edits cannot close an open
     * group.
     */
    const open = new Map();
    /**
     * Explanation state uses `module:param` keys and survives field rebuilding like
     * disclosure state.
     */
    const docs = new Set();
    /**
     * Rendering waits through a button click. Field `change` fires during blur caused
     * by mouse press; a localhost edit can answer in about 1 ms on this bench and
     * rebuild the button before mouse release. The browser then cannot dispatch its
     * click. Holding DOM steady through dispatch preserves Run and instrument
     * actions; pending renders follow afterward.
     */
    let pressing = false;
    let missed = false;
    /**
     * `dispose` prevents late saved-bench responses from rendering detached cards or
     * rearming cleared chart timers.
     */
    let disposed = false;
    // The click guard covers cards, saved-bench names and LED levels beside switches.
    // A level commit can otherwise replace DC between mouse press and release,
    // leaving the edit applied but the requested instrument action unperformed.
    for (const zone of [panel, savedEl, body]) {
      zone.addEventListener('pointerdown', (e) => {
        if (e.target.closest('button')) pressing = true;
      });
      // `click` follows `pointerup`, so rendering resumes after dispatch.
      // `pointercancel` also releases the guard to prevent a cancelled drag from
      // freezing the card.
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
     * Actions await the view-wide edit chain because field commits can still be in
     * flight when a button click arrives. Otherwise the display shows the requested
     * value while DC or Pulse drives the previous level captured at render. Actions
     * can read any card, so the chain is per view. Awaiting supplies correctness
     * without disabling and redrawing buttons during a click, which can consume the
     * click.
     */
    let edits = Promise.resolve();

    /**
     * The refusal flag distinguishes a settled edit chain from successful edits.
     * Catches must resolve to allow later edits, but that resolution alone must not
     * allow Run or DC with a refused value. Actions stop after a refusal; a
     * successful subsequent edit clears the flag.
     */
    let refused = false;
    const settled = () => edits;

    /**
     * A synchronous submission flag closes the gap before `RunQueued` reaches store
     * `busy`. Two clicks in that gap would start two experiments because accepted
     * submissions queue. The flag avoids redrawing and replacing a button during its
     * click.
     */
    let submitting = false;

    /**
     * Each runnable card validates an empty-override one-node tree. Bench verdicts
     * cover chain state and cannot catch card errors such as zero J–V step or missing
     * V_oc. Both invalid and critical checks gate Start. Empty node parameters use
     * the catalogue values Run will receive. Action-only `light` cards skip this
     * validation because a standalone light tree is refused when park undoes it; that
     * refusal must not appear under unrelated bench actions.
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

    /** The helper identifies cards whose checks can change with read-back. */
    let validatedAt = null;

    /**
     * The handler reports a refused preceding edit instead of performing Run or an
     * action with an unintended value.
     */
    const stale = () => notify(
      'the edit before this was refused, so nothing was started: the value on '
      + 'the bench is still the old one. Fix the field, or reload the card.',
      'warn');

    const fail = (err) => {
      // Refusals show `ApiError.text` and `ApiError.checks` rather than a bare status
      // or incorrectly reading `detail`.
      notify(err.text || String(err), err.level || 'warn', err.checks || null);
    };

    // Saved benches preserve the whole catalogue's edited layer across sessions.
    // Save, list and delete use the bench routes (`docs/service-contract.md` §4);
    // restoration uses ordinary per-module edits so invalid values get field-specific
    // refusals while successful modules remain applied. MODULES identifies the scope;
    // its menu holds infrequent controls (`docs/ui-rules.md` §14). Opening only
    // compares and lists changes before an explicit apply because replacing bench
    // values is outside undo. `drift` in `ui/lib/recipe.js` also serves recipe
    // comparisons.

    /**
     * Saved benches load in descending save-time order and refresh after save or
     * delete.
     */
    let benches = [];
    /**
     * The name field supplies the next save destination and follows the opened file.
     */
    let benchName = '';
    /**
     * The named file stays selected for comparison through application or dismissal.
     * Opening lists differences without applying them.
     */
    let loaded = null;
    /**
     * The drawer remains open for typing and comparison. A menu closing on pointer
     * exit suits one-click options, but not a name field and table. Save or delete
     * completion can close it.
     */
    let drawerOpen = false;

    /**
     * Arming carries both kind and filename for six seconds. The same row can
     * overwrite or delete, so changing either must require another confirmation; a
     * boolean could authorize the wrong file or operation.
     */
    let armed = null;
    let armedTimer = null;
    const isArmed = (kind, name) => Boolean(armed && armed.kind === kind && armed.name === name);

    function arm(kind, name) {
      armed = kind ? { kind, name } : null;
      clearTimeout(armedTimer);
      armedTimer = kind ? setTimeout(() => {
        armed = null;
        // Arming expiry must not rebuild a name being typed. The field's commit
        // renders on exit, and its action reads the expired state.
        if (!savedEl.contains(document.activeElement)) render();
      }, 6000) : null;
    }

    async function loadBenches() {
      let answer;
      try {
        answer = (await api.savedBench()).benches || [];
      } catch (err) {
        if (disposed) return;
        // A failed list refresh retains known files and reports failure. Replacing it
        // with empty would falsely say none saved and allow an unconfirmed overwrite.
        notify(`the saved benches could not be listed · ${err.text || String(err)}`, 'warn');
        render();
        return;
      }
      if (disposed) return;
      benches = answer;
      if (loaded && !benches.some((b) => b.name === loaded)) loaded = null;
      render();
    }

    /** Opening a saved bench compares values without changing them. */
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
        const { byName } = store.getState().modules;
        // A matching file needs an acknowledgement because no drift note appears. An
        // absent catalogue cannot count as a match even if comparison returns no
        // differences.
        if (!comparable(found, byName)) {
          notify(`${found.name} is open — the comparison follows once the module catalogue has arrived.`, 'ok');
        } else if (!drift(found, byName).length) {
          notify(`the bench already matches ${found.name}.`, 'ok');
        }
      }
      render();
    }

    /**
     * The synchronous save flag prevents duplicate writes during one in-flight request.
     */
    let saving = false;

    async function save() {
      if (saving) return;                    // the second click of a double-click
      const name = fileStem(benchName);
      if (!name) {
        notify('a saved bench needs a name — type one in the field first.', 'warn');
        return render();
      }
      // Existing filenames need visible overwrite scope and a second click. The
      // refreshed service list supplies disk state rather than a guess.
      if (!isArmed('overwrite', name) && benches.some((b) => b.name === name)) {
        const was = benches.find((b) => b.name === name);
        arm('overwrite', name);
        notify(`${name} already exists${was && was.saved_at ? ` — saved ${fmt.clock(was.saved_at)}` : ''}. `
          + 'Click again to write over it, or type another name.', 'warn');
        // `notify` updates only the strip; the button also needs redraw so its
        // confirmation consequence is visible at the click target.
        return render();
      }
      arm(null);
      saving = true;
      try {
        const out = await api.saveBench(name);
        notify(`saved ${out.name} → ${out.path}`, 'ok');
        // The service-returned filename determines subsequent overwrite confirmation.
        benchName = out.name;
        await loadBenches();
        // A successful save records the current bench, so comparison stays empty
        // while values agree.
        loaded = benches.some((b) => b.name === out.name) ? out.name : null;
        drawerOpen = false;
      } catch (err) { fail(err); } finally { saving = false; }
      render();
    }

    /**
     * Delete requires a second click because it leaves no saved file. This is the
     * console's only disk deletion and needs its own confirmation scope.
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
     * Restoration joins the edit chain so Run or DC cannot act on a half-replaced
     * bench. A refused module sets `refused` and blocks that action. Successful edits
     * remain; recomputed comparison lists only remaining differences. `restoreBench`
     * in `ui/views/pipeline.js` uses the same per-module pattern.
     */
    function restoreBench(moved) {
      const bodies = restore(moved);
      const names = Object.keys(bodies);
      const label = loaded || 'the saved bench';
      edits = edits.then(async () => {
        const bad = [];
        for (const [module, params] of Object.entries(bodies)) {
          try {
            // The response is the authoritative module entry used by cards and node
            // forms.
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
     * The MODULES menu stays beside saved identity rather than at the row end. At
     * 1440 px the latter can be 1300 px from its subject on this bench, making the
     * control hard to find (`docs/ui-rules.md` §14).
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

    /** Separate save and open sections make each operation's scope explicit. */
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
            // `benchName` follows typing rather than only commit. Store-driven drawer
            // rebuilds must not replace uncommitted text with the last committed
            // name.
            oninput: (e) => { benchName = e.target.value; },
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
          // The sentence uses caption styling; `.zt` uppercases 9 px zone labels and
          // would give prose inappropriate emphasis.
          h('p.bcap', { text: benches.length
            ? 'open compares a file with the bench; nothing moves until the button under the list'
            : 'nothing saved yet — name the bench above and Save it' }),
          benches.map((b) => benchRow(b))),
        driftNote(m));
    }

    /**
     * Each file row places name, save time and actions together rather than pushing
     * controls to the far end.
     */
    function benchRow(b) {
      const armedDelete = isArmed('delete', b.name);
      return h('div.brow', { class: b.name === loaded ? 'on' : '' },
        // Comparing labels the state distinctly from the Open action.
        h('span.fn', b.name, b.name === loaded ? h('i', ' · comparing') : null),
        h('span.ft', { text: b.error ? 'will not parse' : fmt.clock(b.saved_at) }),
        h('span.fa',
          // Open gets the bordered button because repeated restoration is more
          // frequent than saving; other file actions stay quiet.
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
     * The comparison previews every bench change before explicit apply. Unlike recipe
     * drift beside a future run, this note is the load action itself. Applying is
     * outside undo, so opening a file must never replace values merely to reveal its
     * contents.
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
        // Card checks belong to its own tree. Edit-time validation joins the chain
        // Run awaits so its gate cannot describe an older value.
        checksFor: (name) => checks.get(name) || [],
        // Actions except park return 409 while the worker is held; Start would queue.
        // Cards state these conditions instead of presenting misleading available
        // controls.
        busy: Boolean(run && !run.parked_at),
        // Result-slot presence belongs to the card key; its contents have a separate
        // data key. A shot redraws the chart without replacing the form.
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
              // The response supplies card values, provenance, needs and a refreshed
              // `estimate_text` with point count.
              store.applyModule(await api.setParams(name, params));
              // Validation stays in the edit chain so `settled()` waits for checks
              // and values together.
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
         * The switch action reads `light` levels at click time. Only returned
         * read-back changes selection; refusal leaves the actual bench state visible.
         */
        async actInstrument(action, args) {
          await settled();
          if (refused) return stale();
          // Arguments are rebuilt after edits settle. Render-time arguments can
          // contain the preceding LED level when clicking DC also commits the field.
          const state = store.getState();
          const fresh = panelModel(state.bench, state.modules.byName.light || null).rows
            .flatMap((row) => row.positions).find((p) => p.action === action);
          try {
            const out = await api.action(action, fresh ? fresh.args : args);
            if (out && out.bench) store.applyBench(out.bench, { readBack: true });
          } catch (err) { fail(err); }
        },
        async act(model, action) {
          await settled();
          if (refused) return stale();
          try {
            // Action arguments come from settled store values, not the model captured
            // before an in-flight edit.
            const entry = store.getState().modules.byName[model.name];
            const fresh = entry
              ? (cardModel(entry, { bench: store.getState().bench }).actions
                .find((a) => a.action === action.action) || action)
              : action;
            // Returned read-back updates card illumination and rail together without
            // a separate request.
            const out = await api.action(fresh.action, fresh.args);
            if (out && out.bench) store.applyBench(out.bench, { readBack: true });
          } catch (err) { fail(err); }
        },
      };
    };

    /**
     * Cards retain individual model keys and replace in place. Rebuilding six cards
     * per fast-scan frame creates 435 506 elements over a 21 × 180 scan on this bench
     * and loses focus within 2.5 s; even 1 Hz power updates can blur fields.
     * Rebuilding only changed cards preserves the caret. Detaching the shared
     * container would also blur unchanged neighbours, so replacements stay local.
     */
    const held = new Map();
    let order = '';

    /** The result panels' redraw throttle, and the form key each last drew. */
    const charts = createChartThrottle();
    const drawn = new Map();

    /**
     * The card key includes pure `cardModel` output, worker state, checks and
     * disclosure. Read-back values omit changing timestamps so identical snapshots do
     * not rebuild. Callbacks are excluded because they read the store and API; action
     * arguments are resolved at click time.
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
      if (disposed) return;
      if (pressing) { missed = true; return; }
      const state = store.getState();
      const { byName } = state.modules;
      const c0 = ctx();
      renderPanel(state, c0);
      // Saved benches remain accessible even with an empty catalogue, when restoring
      // one is especially useful.
      renderSaved(state);
      const names = BENCH_CARDS.filter((n) => byName[n]);
      if (!names.length) {
        held.clear();
        drawn.clear();
        order = '';
        // `body` children are managed directly, so its empty state uses `fill`, not
        // `dom.keyed`. A cached key would survive populated rendering and fail to
        // clear cards after reset. The empty element has no focus state to preserve.
        fill(body, h('div.card', h('p.absent', 'the module catalogue has not arrived yet')));
        return;
      }
      const c = ctx();
      if (names.join(',') !== order) {
        // A changed module set requires rebuilding the card row, including initial
        // catalogue arrival.
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
          // An unchanged card can have changed results. Separate keys let shots
          // redraw charts without taking the caret from the form.
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

      // Read-back can change checks without edits through instrument availability,
      // chain state or V_oc. `read_at` triggers validation rather than every stream
      // frame.
      const at = (state.bench && state.bench.read_at) || null;
      if (at !== validatedAt) {
        validatedAt = at;
        revalidate(names);
      }
    }

    /**
     * The result slot is independently keyed and refilled after its card rebuilds.
     * Form edits and read-back draw immediately because timing must follow typing.
     * Shot updates use the chart throttle. `paint` rereads both data and current card
     * element so deferred work cannot draw stale data into a replaced slot.
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
      // An unkeyed slot needs immediate filling after card construction; leaving it
      // empty for the throttle interval would imply a broken result panel.
      charts.request(name, { immediate: slot.__key === undefined || drawn.get(name) !== keys.form, paint });
    }

    const off = store.subscribe(render);
    render();
    // The file list loads on entry and refreshes after save or delete rather than
    // polling.
    loadBenches();
    return {
      // Disposal clears this view's `armedTimer`. Referencing another view's timer
      // name would throw and obstruct tab teardown.
      dispose() { disposed = true; off(); charts.dispose(); clearTimeout(armedTimer); },
      focus,
    };
  },
};
