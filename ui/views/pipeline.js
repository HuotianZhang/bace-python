// The pipeline tab — `docs/ui-plan.md` M5, and R3·3.
//
// Two cards: the tree that gets posted, and what the service says it will do
// with it. Everything on the right-hand side, and every number on the left
// below the tree itself, comes from one `POST /pipelines/validate` — the
// checks, the schedule in order, the counters, the cost and the folder. The
// editor holds the tree; the service holds every conclusion about it.
//
// Four decisions, each of them a rule from somewhere:
//
//   * **The console validates on every commit, not only on Dry run.** The
//     bench tab already does this per card (M2: a `jv` with `step_v = 0` left
//     Start enabled and `/runs` answered 422), and a pipeline has more ways to
//     be wrong than a card: a V_oc with no source in scope, an LED level under
//     turn-on, a module inside an illumination loop typing `led_v`. So the
//     check list, the cost and the counters are never older than the tree.
//     Measured on the canonical 9 × 5 tree: 110 ms and 340 kB an answer,
//     nearly all of it the schedule's 90 resolved ParamSets — so the calls are
//     coalesced (`VALIDATE_MS`) and a commit that lands during one supersedes
//     it rather than queueing behind it. **Dry run** is the same request made
//     deliberately: it re-asks and opens the schedule, which is what a Dry run
//     after a bench read-back is for.
//   * **Nothing is counted here.** How many levels `1.010 → 1.030 step 0.005`
//     makes is a rounding question the service settled once and this file must
//     not answer a second time. Until a validate has answered, a range row
//     says the range and no count.
//   * **The schedule is drawn at three scales** (`ui-rules` §5), because the
//     canonical tree is 198 steps and *step 3 of 198* is the number that
//     paragraph exists to refuse. Which temperature, which level, how far into
//     the scan — the same three the monitor draws during the run.
//   * **The tree is the one thing here that can be lost, so it can be taken
//     back.** Every value on a bench card is the service's, and the way back
//     from an edit is the `↺` beside the row; the tree is the client's own
//     until Start, and three of its controls destroyed work in one click with
//     nothing offering it back — `✕` takes a node *and everything under it*,
//     the recipe picker replaces the whole tree, `↺ bench` drops every
//     override a node types. `change` is already the one funnel every mutation
//     goes through, so the history hangs off it and covers all of them,
//     including the two that are not buttons on this screen.
//   * **A module node's form is the bench's form.** `cardModel` and the field
//     component from `lib/card.js`, over the ParamSet the *schedule* resolved
//     for that node — so `led_v` reads as inherited from the illumination
//     loop, `voc` as derived from the `jv_bace` two nodes earlier, and which
//     rows sit above the fold is the one table in `lib/fields.js` rather than
//     a second opinion about what matters.

import { h, fill, keyed } from '../lib/dom.js';
import * as fmt from '../lib/format.js';
import { icon } from '../lib/icons.js';
import { cardModel } from '../lib/fields.js';
import { renderRow, field, fold } from '../lib/card.js';
import { chart } from '../lib/charts/frame.js';
import { scheduleModel } from '../lib/charts/schedule.js';
import * as tree from '../lib/tree.js';
import { drift, recorded, restore, showValue, fileStem } from '../lib/recipe.js';
import { createHistory } from '../lib/undo.js';
import { benchHash } from '../lib/route.js';

/**
 * How long a burst of edits is collapsed into one validate.
 *
 * A commit is a `change` event or a click, so this is not a keystroke
 * throttle — it is there for the clicks that come in threes (move a node up
 * three places) and for an edit that lands while an answer is in flight. The
 * same shape as `lib/watch.js`'s bench throttle and `lib/results.js`'s redraw
 * one, and for the same reason: 110 ms of service time and a third of a
 * megabyte, per answer.
 */
const VALIDATE_MS = 250;

export default {
  route: 'pipeline',
  title: 'pipeline',

  mount(container, { store, api, notify }) {
    // -- the state this view owns ------------------------------------------
    /** The tree as typed. `null` is a pipeline with no nodes (`ui-rules` §9). */
    let typed = null;
    let name = '';
    /** The last answer, and the tree it describes. */
    let answer = null;
    let answered = null;
    let inflight = false;
    let pending = null;
    let timer = null;
    let submitting = false;
    /** Which node's form is open, as an index path; `null` is none. */
    let selected = null;
    /** Fold groups open on the node form, per node path. */
    const opened = new Map();
    /** Which schedule groups are expanded, by node path. */
    let expanded = new Set();
    let showAllChecks = false;
    let showFlat = false;
    /**
     * A Save that would write over an existing recipe, armed.
     *
     * The console arms every action that costs the *sample* something — Park
     * on a busy bench, Abort — and armed nothing that costs the operator their
     * own work. A recipe is the only thing this screen writes to disk, the
     * service overwrites `<out>/recipes/<name>.json` without asking
     * (`app.py: save_pipeline`), and unlike an edit to the tree it is outside
     * the undo history: the file that was there is gone. So it arms, and
     * disarms itself six seconds later, exactly as Park does.
     */
    let saveArmed = null;
    let saveArmedTimer = null;
    let recipes = [];
    /**
     * The recipe the tree was reopened from, until the operator dismisses
     * the note or reopens another. A recipe is an instance of the bench: the
     * file records the bench values it was saved with, and while any of them
     * has moved since, the note under the name row says which and offers to
     * put them back. Nothing else changes — Start still runs with the bench
     * as it is, which is what it always did; now it is visible.
     */
    let loaded = null;
    /**
     * The way back — `lib/undo.js`. It holds whole snapshots rather than
     * inverse operations because the states are small JSON and the operations
     * are not all invertible: `↺ bench` drops an unknown number of overrides
     * and `open recipe` replaces everything, and an inverse for each is a
     * second implementation of the tree that can disagree with the first.
     */
    const history = createHistory();
    /** Everything a mutation can move, so undo restores the screen and not only the tree. */
    const snapshot = () => ({ typed, name, selected, loaded });

    const body = h('div.pipe');
    fill(container,
      h('h1', 'pipeline'),
      body);

    const structureEl = h('div.card.pipe-col');
    const scheduleEl = h('div.card.pipe-col');
    fill(body, structureEl, scheduleEl);

    // Sections, keyed apart: an edit to the tree must not rebuild the schedule
    // chart, and an answer arriving must not take the caret out of a field
    // the operator is still typing in (`dom.keyed`, and M2's finding).
    const headEl = h('div.ch');
    const treeEl = h('div.tree');
    const nodeEl = h('div.nodeform');
    const valuesEl = h('div.vlists');
    const costEl = h('div.costs');
    const checksEl = h('div.checks');
    // The drift note stays in the flow; it is a reading, and `docs/ui-rules.md`
    // lets the bar pin a control and not a reading.
    const noteEl = h('div.pipe-note');
    // The action row: what the run will be called, and the buttons that act on
    // it. It is the card body's own child rather than a nested box because a
    // sticky box may only travel inside its containing block, and this one has
    // to rise the whole panel.
    const actionsBarEl = h('div.pipe-actions-bar');
    fill(structureEl, headEl,
      h('div.cb', treeEl, nodeEl, valuesEl, h('div.hr'), costEl, h('div.hr'), checksEl, noteEl, actionsBarEl));

    const schedHeadEl = h('div.ch');
    const chartEl = h('div.pipe-chart');
    const stepsEl = h('div.sched');
    const bindEl = h('div.binds');
    const gridEl = h('div.gridfill');
    const folderEl = h('p.chart-note');
    fill(scheduleEl, schedHeadEl,
      h('div.cb', chartEl, stepsEl, h('div.hr'), bindEl, h('div.hr'), gridEl, folderEl));

    // -- validate ----------------------------------------------------------

    /**
     * Ask again. `now` skips the coalescing window — the Dry run button and
     * a bench read-back, both of which are deliberate rather than incidental.
     *
     * A request in flight is never awaited by the next one: the tree may have
     * moved on twice while the first answer was on the wire, and an answer is
     * discarded unless it still describes the tree on screen. `answered` is
     * that test, and it is why `Start` can be stale without being wrong.
     */
    function revalidate({ now = false } = {}) {
      clearTimeout(timer);
      if (!typed) { answer = null; answered = null; render(); return; }
      pending = typed;
      const fire = async () => {
        if (inflight) return;                 // the answer will re-enter here
        const asked = pending;
        pending = null;
        inflight = true;
        render();
        try {
          const out = await api.validate(asked, name);
          answer = out;
          answered = asked;
        } catch (error) {
          // A validator that will not answer must not leave a stale `valid`
          // on screen unlocking Start: what it would have said is unknown,
          // not absent. The same rule `views/bench.js` applies per card.
          answer = { valid: false, checks: [{ level: 'invalid', code: 'validate.unreachable',
            text: error.text || String(error), node_path: '' }], schedule: [], counters: {}, cost: null };
          answered = asked;
        } finally {
          inflight = false;
        }
        render();
        if (pending) fire();
      };
      if (now) fire(); else timer = setTimeout(fire, VALIDATE_MS);
    }

    /**
     * Every mutation goes through here, so nothing can change the tree quietly
     * — and, since 2026-09-05, so nothing can change it irrecoverably either.
     *
     * `label` names the action for the Undo button, because "undo" alone asks
     * the operator to remember what they last did; after four clicks and a
     * look away, they do not. A mutation that leaves the tree byte-identical
     * records nothing: a `change` from a field re-committed at the same value
     * is not a step back anybody wants to take.
     */
    function change(next, { select = undefined, label = '', before = null } = {}) {
      // `before` is for the one caller that has to move `name` and `loaded`
      // before it can call this — the recipe picker — and would otherwise
      // record a snapshot already carrying the new recipe's name.
      if (!tree.sameTree(typed, next)) history.push(before || snapshot(), label);
      typed = next;
      if (select !== undefined) selected = select;
      if (selected && !tree.nodeAt(typed, selected)) selected = null;
      revalidate();
      render();
    }

    /**
     * Take one step back, or forward. The whole snapshot goes back — the
     * selection and the recipe the tree was opened from with it — because a
     * `✕` that cleared the selection and an `open recipe` that replaced the
     * name both have to undo to the screen the operator was looking at, not
     * only to the tree they had.
     */
    function step(direction) {
      const entry = direction === 'redo' ? history.redo(snapshot()) : history.undo(snapshot());
      if (!entry) return;
      ({ typed, name, selected, loaded } = entry.state);
      if (selected && !tree.nodeAt(typed, selected)) selected = null;
      notify(`${direction === 'redo' ? 'redone' : 'undone'}${entry.label ? ' · ' + entry.label : ''}`, 'ok');
      revalidate();
      render();
    }

    /**
     * Ctrl/⌘-Z, and Ctrl/⌘-Shift-Z to put it back.
     *
     * Not while the caret is in a field: there the browser's own undo is the
     * one the operator means, and stealing it would take back the whole node
     * instead of the digit they mistyped. On `window` rather than on the view,
     * so it works with nothing focused — which after a `✕` is exactly where
     * the focus is.
     */
    function onKey(e) {
      if (e.key !== 'z' && e.key !== 'Z') return;
      if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
      const el = e.target;
      const tag = el && el.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (el && el.isContentEditable)) return;
      e.preventDefault();
      step(e.shiftKey ? 'redo' : 'undo');
    }
    window.addEventListener('keydown', onKey);

    // -- the model the whole view renders from -----------------------------

    /**
     * Everything derived from the tree and the answer, computed once per
     * change of either.
     *
     * Not per render, because `render` runs on every store notify and the
     * canonical tree is a 198-step schedule that nests into 9 groups, 45
     * groups and 90 leaves — walked three more times for the timeline, the
     * grid and the rows. That is M1's finding at pipeline scale: the work is
     * pure and cheap *once*, and sixty times a second it is neither.
     *
     * The identities are the cache key rather than a stringify, because both
     * are replaced wholesale by the two functions allowed to move them
     * (`change` and `revalidate`) and never edited in place.
     */
    let memo = null;
    /** Stamped on each fold, so a renderer can key on "this answer". */
    let folds = 0;

    function derived() {
      if (memo && memo.typed === typed && memo.answer === answer) return memo;
      folds += 1;
      const fresh = Boolean(answer) && tree.sameTree(typed, answered);
      /**
       * **The schedule keeps describing the previous tree while the next
       * answer is on the wire, and says so.**
       *
       * Blanked instead — which is what the first cut did, for the ~350 ms
       * between a commit and its answer — the right-hand card read *"no nodes
       * yet — add a loop or a module"* on a tree with four nodes in it, on
       * every keystroke that committed. A screen saying something false is
       * worse than one saying something a moment old, and `stale` is already
       * on the header, on the check line and on the Start button, which
       * refuses while it is set.
       *
       * The **tree rows** are the exception and stay fresh-only: they match a
       * node to its schedule entries by counting iterations, so zipped
       * against a schedule for a tree with a different shape they would put
       * one node's shots on another node's row. An absent tag for a third of
       * a second is the right cost for that.
       */
      const nodes = tree.scheduleTree((answer && answer.schedule) || []);
      memo = {
        id: folds,
        typed,
        answer,
        fresh,
        /** The answer, only while it still describes what is on screen. */
        answered: fresh ? answer : null,
        /** The answer as drawn — a moment old while a validate is in flight. */
        shown: answer,
        stale: Boolean(answer) && !fresh,
        nodes,
        rows: typed && fresh ? tree.treeRows(typed, answer.tree, nodes) : (typed ? tree.treeRows(typed) : []),
        checks: tree.checkSummary((answer && answer.checks) || []),
        cost: tree.costModel(answer && answer.cost),
        counters: (answer && answer.counters) || {},
        blocks: tree.timeline(nodes),
        grid: tree.gridModel(nodes),
        leaves: tree.scheduleLeaves(nodes),
      };
      return memo;
    }

    function view() {
      const state = store.getState();
      const run = state.runs[state.activeRunId] || null;
      const d = derived();
      return {
        ...d,
        answer: d.answered,
        state,
        catalogue: state.modules.byName,
        moduleNames: state.modules.order.filter((n) => !tree.NOT_A_NODE.has(n)),
        busy: Boolean(run && !run.parked_at),
      };
    }

    // -- the structure card ------------------------------------------------

    function renderHead(v) {
      const s = typed ? tree.structureSummary(typed) : { loops: 0, modules: 0 };
      const runs = v.counters.modules;
      keyed(headEl, JSON.stringify([s, runs, v.moduleNames, Boolean(typed), selected, recipes.length, inflight]), () => [
        h('span.cn', 'structure'),
        h('span.cs', {
          text: `${fmt.plural(s.loops, 'loop')} · ${fmt.plural(s.modules, 'module')}`
            + (runs === undefined ? '' : ` · ${fmt.plural(runs, 'run')}`),
        }),
        inflight ? h('span.cs', { text: '· checking' }) : null,
        h('span', { style: { flex: '1' } }),
        addControls(v),
      ]);
    }

    /**
     * Where the next node lands: **inside the selected loop**, or beside the
     * selected module.
     *
     * A module for a parent rather than the root, because adding a module
     * selects it — so without this, composing `jv_bace` then `bace` inside an
     * illumination loop put the second one at the root. Which the console
     * then described perfectly: 45 J-Vs, 9 scans, and `⚠ V_oc` on every one
     * of them, because a `bace` outside the loop has no `jv_bace` at its own
     * drive level in scope. It was right, and it was not the tree anyone was
     * building.
     */
    function addTarget() {
      if (!typed || !selected) return null;
      const at = tree.nodeAt(typed, selected);
      if (at && at.kind === 'loop') return selected;
      return selected.length ? selected.slice(0, -1) : null;
    }

    /**
     * Add a loop or a module. Adding a loop to a tree whose root is a module
     * **wraps** it, because "put this under a temperature sweep" is what an
     * operator means by adding a loop above a single scan, and the contract
     * allows either at the root.
     */
    function addControls(v) {
      const into = addTarget();
      const at = into ? tree.nodeAt(typed, into) : null;
      const where = !typed ? 'as the root' : at ? `into ${label(at)}` : 'at the root';
      const picker = h('select.v', { title: 'the module this node runs' },
        ...v.moduleNames.map((n) => h('option', { value: n }, n)));
      return h('span.addbar',
        h('span.cs', { text: where }),
        ...tree.LOOP_ORDER.map((kind) => h('button.btng', {
          title: `a ${kind} loop — ${tree.LOOPS[kind].owns || 'repeats its children'}`,
          onclick: () => addNode(tree.newLoop(kind), into, `add the ${kind} loop`),
        }, `+ ${kind}`)),
        picker,
        h('button.btng', { onclick: () => addNode(tree.newModule(picker.value), into, `add ${picker.value}`) }, '+ module'));
    }

    function addNode(node, into, label = 'add a node') {
      if (!typed) return change(node, { select: [], label });
      if (into) {
        const parent = tree.nodeAt(typed, into);
        const index = (parent.children || []).length;
        return change(tree.insertAt(typed, into, node), { select: [...into, index], label });
      }
      if (typed.kind === 'module' && node.kind === 'loop') {
        // Wrap: the module becomes the loop's only child, which is the tree
        // the operator was describing.
        return change({ ...node, children: [typed] }, { select: [], label });
      }
      if (typed.kind !== 'loop') {
        notify('the root is a single module. Add a loop first — it takes this module inside it — '
          + 'or remove it and start again.', 'warn');
        return undefined;
      }
      const index = (typed.children || []).length;
      return change(tree.insertAt(typed, [], node), { select: [index], label });
    }

    function renderTree(v) {
      const key = JSON.stringify([typed, v.fresh && answer.tree, selected,
        v.rows.map((r) => [r.runs, r.shots, r.needs_operator, r.relay_transition, r.estimate_s, r.measure_s])]);
      keyed(treeEl, key, () => {
        if (!typed) {
          return h('p.absent', 'no nodes yet — add a loop or a module above, '
            + 'or reopen one of the saved recipes below.');
        }
        return v.rows.map((row) => treeRow(row, v));
      });
    }

    function treeRow(row, v) {
      const on = selected && selected.join(',') === row.path.join(',');
      const el = h('div.tr' + (on ? '.on' : ''), {
        style: { marginLeft: `${row.depth * 22}px` },
        onclick: (e) => { if (!e.target.closest('button')) { selected = on ? null : row.path; render(); } },
      });
      const tags = [];
      if (row.kind === 'loop') {
        if (row.owns) tags.push(h('span.inh', { text: `owns ${row.owns.split(' ')[0]}` }));
        // `needs_operator` on a temperature step is the bench's answer, not
        // the tree's: `ok` only when the read-back showed the 331 attached
        // and answering (`temperature.not-wired`). It is drawn as the pause
        // it will be, because a pause is hours and must not look like a step.
        if (row.needs_operator) tags.push(h('span.tag.nb', { title: 'the 331 is not answering, so the run pauses here for a manual set', text: 'pauses' }));
        if (row.measure_s) tags.push(h('span.cs', { text: fmt.duration(row.measure_s) }));
      } else {
        const entry = v.catalogue[row.module];
        if (!entry) tags.push(h('span.tag.bad', { title: 'this service has no module by that name', text: 'unknown module' }));
        else if (entry.status !== 'built') tags.push(h('span.tag.nb', { text: entry.status }));
        if (row.centre_on_voc) {
          tags.push(h('span.inh', {
            title: row.voc ? `${row.voc.how} ${row.voc.node_path || ''} at ${fmt.volts(row.voc.led_v)}` : 'no source in scope',
          }, icon(row.voc ? 'inherit' : 'warn'),
          h('span', { text: row.voc ? `V_oc ← ${row.voc.how}` : 'V_oc' })));
        }
        if (row.relay_transition) {
          tags.push(h('span.cs', { title: 'the router moves at this boundary; the interlock refuses while a source is live', text: `relay → ${row.relay}` }));
        }
        if (row.runs > 1) tags.push(h('span.cs', { text: `× ${row.runs}` }));
        if (row.shots) tags.push(h('span.cs', { text: fmt.plural(row.shots, 'shot') }));
        if (row.estimate_s !== null && row.estimate_s !== undefined) {
          tags.push(h('span.cs', { text: fmt.duration(row.estimate_s) }));
        }
      }
      return fill(el,
        h('span.nk' + (row.kind === 'module' ? '.mod' : ''),
          icon(row.kind === 'loop' ? 'loop' : 'module')),
        h('span.n', { text: row.kind === 'loop' ? row.loop : row.module }),
        // A module row says nothing about the bench: it *is* the bench's
        // module, and only a node that differs carries the mark.
        row.kind === 'loop' ? h('span.d', { text: row.summary }) : overrideTag(row),
        h('span', { style: { flex: '1' } }),
        tags,
        h('span.rowact',
          h('button.btng', { title: 'move up', onclick: () => moveNode(row.path, -1) }, '↑'),
          h('button.btng', { title: 'move down', onclick: () => moveNode(row.path, 1) }, '↓'),
          h('button.btng', {
            title: 'remove this node and everything under it — Undo, or Ctrl-Z, takes it back',
            onclick: () => change(tree.removeAt(typed, row.path),
              { select: null, label: `remove ${row.kind === 'loop' ? 'the ' + row.loop + ' loop' : row.module}` }),
          }, '✕')));
    }

    /**
     * Reorder a node, and take the selection with it.
     *
     * A path is a list of child indices, so a move renumbers the node and
     * every sibling it passed — and a selection left at the old index is now
     * pointing at whichever node took that place. With two `bace` siblings,
     * which the tree schema supports and the service numbers `bace` and
     * `bace#2`, the form would quietly switch to the other one and the next
     * override would land on the wrong node.
     */
    function moveNode(path, delta) {
      const next = tree.moveAt(typed, path, delta);
      if (next === typed) return;                    // a move off either end
      const at = tree.nodeAt(typed, path);
      change(next, { select: tree.remapPath(selected, path, delta),
        label: `move ${at ? label(at) : 'a node'} ${delta < 0 ? 'up' : 'down'}` });
    }

    /** The `↺` a differing node carries in the tree, with the list on hover. */
    function overrideTag(row) {
      const mark = tree.overrideMark(row);
      if (!mark) return null;
      return h('span.ovr', { title: mark.title, 'aria-label': mark.title, text: mark.text });
    }

    function label(node) {
      return node.kind === 'loop' ? `the ${node.loop} loop` : node.module;
    }

    // -- the selected node's form ------------------------------------------

    function renderNode(v) {
      const node = selected ? tree.nodeAt(typed, selected) : null;
      const row = node ? v.rows.find((r) => r.path.join(',') === selected.join(',')) : null;
      const path = selected ? selected.join(',') : '';
      const open = openedFor(path);
      keyed(nodeEl, JSON.stringify([path, node, row && row.params, row && row.detail, [...open].sort()]), () => {
        if (!node) return null;
        return h('div.nf',
          h('div.nfh',
            h('span.cn', { text: node.kind === 'loop' ? `${node.loop} loop` : node.module }),
            h('span.cs', { text: node.kind === 'loop' ? 'the values it runs, and how it settles' : 'as on the bench' }),
            // The way to the main component: this module's card on the bench,
            // where every value the node does not override is set.
            node.kind === 'module'
              ? h('a.btng.goto', {
                href: benchHash(node.module),
                title: `${node.module} on the bench — what every node of it starts from`,
              }, '→ bench')
              : null,
            h('span', { style: { flex: '1' } }),
            // The way back for the whole node, offered only while there is
            // something to take back: every override dropped, the node is the
            // bench's module again, and the form shrinks to say so.
            node.kind === 'module' && Object.keys(node.params || {}).length
              ? h('button.btng.undo', {
                title: 'drop every override this node types — back to the module as it stands on the bench',
                onclick: () => change(tree.clearParams(typed, selected),
                  { label: `drop ${node.module}’s overrides` }),
              }, '↺ bench')
              : null,
            h('button.btng', { onclick: () => { selected = null; render(); } }, 'close')),
          node.kind === 'loop' ? loopForm(node, row) : moduleForm(node, row, v, open));
      });
    }

    function openedFor(path) {
      if (!opened.has(path)) opened.set(path, new Set());
      return opened.get(path);
    }

    /**
     * A loop's own form. The values are a list or a range and the switch is
     * explicit, because the two are not the same statement: a list is nine
     * temperatures, a range is a rule that made them, and the operator has to
     * be able to see which they typed.
     */
    function loopForm(node, row) {
      const spec = tree.LOOPS[node.loop];
      if (!spec) return h('p.absent', { text: `${node.loop} is not a loop this service knows` });
      const ctx = loopCtx();
      const rows = [];
      if (spec.count) {
        rows.push(field(loopSpec(node, row, { name: 'count', type: 'int', unit: '',
          doc: 'how many times the children run. Nothing changes between iterations — it is the outer averaging loop.' }), ctx, { name: '' }, {}));
      } else {
        const form = tree.valueForm(node);
        rows.push(h('div.pf.range',
          h('span.l', 'values'),
          h('span.v.rng',
            h('span.seg', { role: 'group', 'aria-label': 'values as' },
              ...['list', 'range'].map((f) => h('button.opt', {
                type: 'button',
                class: form === f ? 'on' : '',
                'aria-pressed': form === f ? 'true' : 'false',
                onclick: () => switchForm(f, row),
              }, f))),
            form === 'list' ? listInput(node, spec) : null,
            form === 'range' ? spec.range.map((n, i) => h('input.v', {
              value: node[n] === undefined ? '' : String(node[n]),
              title: n,
              onchange: (e) => commitField(n, e.target.value),
            })) : null,
            form === 'range' && row && row.values
              ? h('i.pts', { text: fmt.plural(row.values.length, 'value') })
              : null),
          h('span.src')));
        if (form === 'range') {
          rows.push(h('p.chart-note', 'the count is the service’s: `range_values` rounds it, '
            + 'because truncating drops the last level whenever floating point puts the ratio just '
            + 'under the integer — which for 1.010 → 1.030 step 0.005 wrote a four-level loop as five.'));
        }
      }
      for (const f of spec.fields) rows.push(field(loopSpec(node, row, f), ctx, { name: '' }, {}));
      return h('div.pr', rows);
    }

    /**
     * Switch how a loop's values are written — and refuse rather than change
     * what it runs. `tree.canSwitchForm` owns both refusals and both
     * sentences: a range nothing has expanded yet has no values to list, and
     * a list that is not evenly spaced cannot be a range without becoming a
     * different sweep.
     */
    function switchForm(form, row) {
      const node = tree.nodeAt(typed, selected);
      const resolved = row && row.resolved;
      const can = tree.canSwitchForm(node, resolved, form);
      if (!can.ok) {
        notify(can.why, 'warn');
        return;
      }
      change(tree.setValueForm(typed, selected, form, resolved), { label: `values as a ${form}` });
    }

    /** A text input over the value list — `295, 290, 280` — parsed on commit. */
    function listInput(node, spec) {
      const values = node[spec.list] || [];
      return h('input.v.vlist', {
        value: values.map((v) => Number(v)).join(', '),
        title: `${spec.list} — the values, in the order they run`,
        onchange: (e) => {
          const raw = e.target.value.trim();
          if (!raw) return commitField(spec.list, null);
          const parsed = raw.split(/[\s,]+/).filter(Boolean).map(Number);
          if (parsed.some((n) => !Number.isFinite(n))) {
            notify(`${spec.list}: "${raw}" is not a list of numbers. Separate them with commas or spaces.`, 'warn');
            return render();
          }
          return commitField(spec.list, parsed);
        },
      });
    }

    /**
     * One loop field as the same spec shape a module parameter has, so it goes
     * through the same component: value, provenance and the sentence under it.
     * `node` says whether the *tree* types this one, which is what decides
     * whether a reset is offered — the service's own filled-in default is not
     * this node's to drop.
     */
    function loopSpec(node, row, f) {
      const typedHere = node[f.name] !== undefined;
      const fallback = row && row.detail ? row.detail[f.name] : undefined;
      return {
        ...f,
        value: typedHere ? node[f.name] : (fallback === undefined ? null : fallback),
        source: typedHere ? 'edited' : 'default',
        detail: typedHere ? 'typed on this node' : 'the service’s default for this loop',
        editable: true,
        node: typedHere,
        doc_full: f.doc,
      };
    }

    function loopCtx() {
      return {
        edit: (_name, params) => {
          for (const [key, raw] of Object.entries(params)) commitField(key, raw);
        },
      };
    }

    /**
     * Loop fields are numbers on the wire, and the service refuses a string:
     * `pipeline._number` wants a finite number, where a module parameter goes
     * through `ParamSpec.coerce` and would have taken one. So the parse is
     * here, and a value that will not parse is refused with a sentence rather
     * than posted for the service to reject in a language about JSON.
     */
    function commitField(key, raw) {
      if (raw === null || raw === '') return change(tree.setField(typed, selected, key, null), { label: `clear ${key}` });
      if (Array.isArray(raw)) return change(tree.setField(typed, selected, key, raw), { label: `set ${key}` });
      const value = Number(raw);
      if (!Number.isFinite(value)) {
        notify(`${key}: "${raw}" is not a number.`, 'warn');
        return render();
      }
      return change(tree.setField(typed, selected, key, value), { label: `set ${key}` });
    }

    /**
     * A module node's form: the bench's own card, over the ParamSet the
     * *schedule* resolved for this node.
     *
     * The provenance is the schedule's and is rendered as it came — `led_v`
     * inherited from the illumination loop, `voc` derived from the `jv_bace`
     * two nodes earlier — which is the whole of `ui-rules` §6 and is only
     * true because the value shown is the one that node will run with. The
     * one thing added is `spec.node`: whether *this node* types the override,
     * which decides whether a reset is offered, because a bench edit and a
     * node override both resolve as `edited` and only one of them is the
     * tree's to drop.
     */
    function moduleForm(node, row, v, open) {
      const catalogue = v.catalogue[node.module];
      if (!catalogue) {
        return h('p.absent', { text: `this service has no module named ${node.module} — this structure cannot run here` });
      }
      const overrides = node.params || {};
      /**
       * The node's resolved ParamSet, when the tree resolves at all.
       *
       * A tree the validator refuses answers `schedule: null` — one bad value
       * and there is no schedule for any node in it. Drawn from the schedule
       * alone the form would then vanish at exactly the moment it is needed:
       * type `100.9` into `n_loops`, and the row that holds the typo, and the
       * reset beside it, are gone with it. So the catalogue stands in, with
       * this node's own overrides on top of it — every field still there and
       * still editable, and only what the loops above would have bound is
       * missing, which is what the note says.
       */
      const resolved = (row && row.params) || null;
      const entry = {
        ...catalogue,
        // The point count is read out of `estimate_text` and the catalogue's
        // is the *bench's*, computed from the bench's axis rather than this
        // node's. An absent count is better than one describing another form.
        estimate_text: '', estimate_s: row ? row.estimate_s : null, last: null, needs: [],
        params: (catalogue.params || []).map((p) => {
          // A schedule's `ParamValue` carries `{value, source, detail}` and no
          // `editable`, and the catalogue's is the answer for the *bench's*
          // ParamSet — where `led_v` is a `run.toml` value and editable. So it
          // has to be recomputed against this node's own source, by the
          // service's own rule (`params.LOCKED`: inherited and derived are not
          // editable on the wire, whatever the spec says). Without it the form
          // offered an input on `led_v` inside an illumination loop, took the
          // override, and the loop that owns it had `tree.owned-param` refuse
          // the tree — an edit that could only ever end in an invalid.
          const pv = resolved
            ? (resolved[p.name] || {})
            // No schedule: the value is what this node types, or the bench's.
            : (p.name in overrides
              ? { value: overrides[p.name], source: 'edited', detail: 'typed on this node' }
              : {});
          const source = pv.source || p.source;
          return {
            ...p,
            ...pv,
            editable: p.editable !== false && source !== 'inherited' && source !== 'derived',
            node: p.name in overrides,
          };
        }),
      };
      // `bench: null` on purpose: the read-back row on a bench card says what
      // the light is doing *now*, and a node that runs in four hours inside an
      // illumination loop is not described by it. `form: 'node'` keeps only
      // what differs from the bench above the fold — a loop's binding, this
      // node's override, and the rows a node has and a card does not
      // (`light`'s shutter, mode and settle) — and folds the rest as `same
      // as bench`. A node with nothing typed on it is one or two rows and a
      // fold, which is what it is.
      const model = cardModel(entry, { bench: null, form: 'node' });
      const ctx = moduleCtx(catalogue);
      const first = (row && row.first) || null;
      const where = [];
      if (first && first.temperature && first.temperature.k !== null && first.temperature.k !== undefined) {
        where.push(`T ${fmt.kelvin(first.temperature.k)}`);
      }
      if (first && first.led_v !== null && first.led_v !== undefined) where.push(`LED ${fmt.volts(first.led_v)}`);
      return h('div',
        !resolved
          ? h('p.chart-note', 'the structure does not resolve yet, so this is the module as it stands on '
            + 'the bench with this node’s own overrides on it — not what the loops above would bind. '
            + 'The check list says what is wrong.')
          : row && row.runs > 1
            ? h('p.chart-note', { text: `resolved for the first of ${row.runs} runs${where.length ? ' — ' + where.join(' · ') : ''}. `
              + 'The loops above bind a different value into each of the others.' })
            : null,
        h('div.pr', model.above.map((r) => renderRow(r, model, ctx))),
        fold(model, ctx, open));
    }

    function moduleCtx(catalogue) {
      const specs = Object.fromEntries((catalogue.params || []).map((p) => [p.name, p]));
      return {
        toggle: (_name, group) => {
          const set = openedFor(selected.join(','));
          if (set.has(group)) set.delete(group); else set.add(group);
          render();
        },
        edit: (_name, params) => {
          let next = typed;
          for (const [key, raw] of Object.entries(params)) {
            next = tree.setParam(next, selected, key, coerce(specs[key], raw));
          }
          change(next, { label: `set ${Object.keys(params).join(', ')}` });
        },
      };
    }

    /**
     * A typed value as the tree should carry it. The service would coerce a
     * string — `ParamSpec.coerce` is what `PUT /modules/{m}/params` leans on —
     * but a tree is also what `POST /pipelines/save` writes to disk, and a
     * recipe holding `"n_loops": "100"` is a file that reads wrong.
     *
     * **A value this cannot convert is left exactly as typed**, so the
     * refusal is the service's own sentence about that parameter rather than
     * this file's about JSON — and `100.9` in an `int` field is one of those.
     * `ParamSpec._as_int` refuses a number that is not whole ("100.9 is not a
     * whole number"); truncating it here would run the scan at 100 loops with
     * nothing on screen saying the typo had been read as something else.
     */
    function coerce(spec, raw) {
      if (raw === null || typeof raw === 'boolean' || typeof raw === 'number') return raw;
      if (!spec) return raw;
      if (spec.type === 'int' || spec.type === 'float') {
        const value = Number(raw);
        if (!Number.isFinite(value)) return raw;
        if (spec.type === 'int' && !Number.isInteger(value)) return raw;
        return value;
      }
      return raw;
    }

    // -- the value lists, the cost and the checks --------------------------

    /**
     * `temperature · 9 values · settle from the last run at each` — R3·3's
     * own two tables. The settle under each temperature is the one the cost
     * model found in the journal for *that setpoint*, and an em dash where it
     * found none, which is the artboard's own rendering of the first one.
     */
    function renderValues(v) {
      // Only the loops whose values *are* values: a `repeat` runs 1, 2, 3,
      // and a table of the numbers one to nine is not information.
      const loops = v.rows.filter((r) => r.kind === 'loop' && r.spec && r.spec.list
        && r.values && r.values.length);
      keyed(valuesEl, JSON.stringify(loops.map((r) => [r.loop, r.values, r.iterations.map((it) => it.settle_s)])), () => {
        if (!loops.length) return null;
        return loops.map((row) => {
          const spec = row.spec;
          const settles = row.loop === 'temperature' ? row.iterations.map((it) => it.settle_s) : null;
          return h('div.vlist-block',
            h('div.cs', {
              text: `${row.loop} · ${fmt.plural(row.values.length, 'value')}`
                + (settles ? ' · settle measured on this bench at each' : spec && spec.owns ? ` · ${spec.owns}` : ''),
            }),
            h('div.lst', { style: { gridTemplateColumns: `repeat(${row.values.length}, auto)` } },
              row.values.map((value) => h('span.h', {
                text: spec && spec.count ? String(value) : Number(value).toFixed(spec ? spec.decimals : 2),
              })),
              settles
                ? settles.map((s) => h('span', { text: s === null || s === undefined ? fmt.ABSENT : fmt.duration(s) }))
                : null));
        });
      });
    }

    /**
     * The three numbers of R3·3, with the one word that keeps them honest.
     *
     * `lower_bound` is set the moment any temperature has no measured settle
     * behind it, which on a fresh device is all of them — so the total reads
     * *at least*, `waiting for T` reads the em dash rather than the holds it
     * does know about, and there is no finish time. A clock time under a
     * number that is a floor is a promise the run cannot keep.
     */
    function renderCost(v) {
      keyed(costEl, JSON.stringify([v.cost, v.counters]), () => {
        if (!v.cost) return h('p.absent', 'no cost yet — the structure has not been checked.');
        const c = v.cost;
        const counters = v.counters;
        return [
          h('div.nums',
            num('total', c.total_s, c.prefix),
            num('measuring', c.measuring_s, ''),
            num('waiting for T', c.waiting_s, c.waiting_s === null ? '' : c.prefix)),
          h('div.cs', {
            text: [
              counters.temperatures ? fmt.plural(counters.temperatures, 'temperature') : null,
              counters.levels ? fmt.plural(counters.levels, 'level') : null,
              counters.modules ? fmt.plural(counters.modules, 'module run') : null,
              counters.shots ? fmt.plural(counters.shots, 'shot') : null,
              c.t_shot_s ? `shot ${fmt.duration(c.t_shot_s)} (${c.t_shot_source})` : null,
              c.finish_at ? `finish ${fmt.clock(c.finish_at)}` : null,
            ].filter(Boolean).join(' · '),
          }),
          c.lower_bound
            ? h('p.chart-note', {
              text: c.unmeasured.length
                ? `a floor, not an estimate: ${c.unmeasured.length} of ${c.per_temperature.length} `
                  + 'temperatures have no settle measured on this bench, and a settle is 14 minutes to '
                  + '2 hours. The journal fills them in as this bench measures them.'
                : `a floor, not an estimate: ${fmt.plural(c.unestimated, 'module run')} cost nothing the catalogue can estimate.`,
            })
            : null,
        ];
      });
    }

    function num(label, seconds, prefix) {
      return h('div.nm',
        h('span.l', { text: label }),
        h('span.big', { text: seconds === null || seconds === undefined ? fmt.ABSENT : (prefix ? prefix + ' ' : '') + fmt.duration(seconds) }));
    }

    /** `22 checks · 12 ok · 3 warn · show` — §11's answered question. */
    function renderChecks(v) {
      const c = v.checks;
      keyed(checksEl, JSON.stringify([c, showAllChecks, v.stale, inflight]), () => {
        if (!c.total) {
          return h('p.absent', inflight ? 'checking …' : 'not checked yet — Dry run asks.');
        }
        const shown = showAllChecks ? c.ordered : c.notable;
        return [
          h('div.cksum',
            h('span.m', { text: fmt.plural(c.total, 'check') }),
            ...tree.LEVELS.filter((l) => c.counts[l]).map((l) => h('span.tag.' + tagClass(l), { text: `${c.counts[l]} ${l}` })),
            v.stale ? h('span.cs', { text: '· the structure has changed since' }) : null,
            h('span', { style: { flex: '1' } }),
            h('button.btng', { onclick: () => { showAllChecks = !showAllChecks; render(); } },
              showAllChecks ? 'collapse' : 'show')),
          shown.length
            ? h('div.cklist', shown.map((check) => h('div.warn1.' + check.level,
              levelMark(check.level),
              h('span.code', { text: check.code }),
              h('span', { text: check.text }),
              check.node_path ? h('span.cs', { text: check.node_path }) : null)))
            : h('p.chart-note', 'nothing to report — every check is ok or info.'),
        ];
      });
    }

    const tagClass = (level) => (level === 'ok' ? 'ok' : level === 'warn' ? 'cost' : level === 'info' ? 'nb' : 'bad');
    // `info` keeps its letter: the pack has no glyph for *worth knowing*, and
    // a borrowed one would say something the check does not.
    const levelMark = (level) => (level === 'info'
      ? h('span.ico', 'i')
      : icon(level === 'crit' ? 'crit' : level === 'ok' ? 'ok' : 'warn', { cls: 'ico' }));

    // -- Start, Save, Dry run ----------------------------------------------

    function renderActions(v) {
      const c = v.cost;
      const blocked = !v.answer || !v.answer.valid || v.busy || v.stale || inflight || !typed;
      const moved = loaded ? drift(loaded, store.getState().modules.byName, { tree: typed, rows: v.rows }) : [];
      // The stem the service will write, not the name as typed: `cool down`
      // is the file `cool_down`, and comparing the raw name meant the "this
      // will write over it" arming never fired for a name with a space in it.
      const stem = fileStem(name || (typed && typed.name) || '');
      const overwrites = recipes.some((r) => r.name === stem);
      const armed = Boolean(stem) && saveArmed === stem;
      const key = JSON.stringify([Boolean(typed), v.answer && v.answer.valid, v.busy, v.stale, inflight, c && c.total_s, name, recipes.map((r) => r.name), loaded && loaded.name, moved, history.depth, history.canRedo, history.undoLabel, history.redoLabel, saveArmed, overwrites]);
      keyed(noteEl, key, () => [recipeNote(v)]);
      keyed(actionsBarEl, key, () => [
        h('div.namerow',
          h('span.l', 'name'),
          h('input.v', {
            value: name, placeholder: 'pipeline',
            title: 'the folder stem: <out>/<name>_YYYYMMDD_HHMMSS',
            onchange: (e) => { name = e.target.value.trim(); revalidate(); render(); },
          }),
          recipes.length
            ? h('select.v', {
              title: 'reopen a saved recipe',
              onchange: (e) => {
                const found = recipes.find((r) => r.name === e.target.value);
                if (found && found.tree) {
                  // Opening a recipe replaces the whole tree, and the one it
                  // replaces may be half an hour of composing that was never
                  // saved. Undo takes the snapshot back whole — the name and
                  // the recipe it was opened from with it — which is why the
                  // picker still acts on one click rather than asking first.
                  const was = snapshot();
                  name = found.name;
                  loaded = found;
                  change(found.tree, { select: null, label: `open ${found.name}`, before: was });
                }
              },
            }, h('option', { value: '' }, 'saved recipes …'),
            ...recipes.map((r) => h('option', { value: r.name }, r.name)))
            : null),
        h('div.btnrow',
          // The way back, beside the ways forward. Named rather than counted:
          // `undo · remove the temperature loop` is answerable without
          // remembering, which after four clicks and a look away is the whole
          // point of it.
          h('button.btng', {
            disabled: !history.canUndo || null,
            title: history.canUndo
              ? `Ctrl-Z — takes back: ${history.undoLabel || 'the last change'}`
              : 'nothing to take back on this structure',
            onclick: () => step('undo'),
          }, history.canUndo && history.undoLabel ? `↶ undo · ${history.undoLabel}` : '↶ undo'),
          history.canRedo
            ? h('button.btng', {
              title: `Ctrl-Shift-Z — puts back: ${history.redoLabel || 'the last undo'}`,
              onclick: () => step('redo'),
            }, '↷ redo')
            : null,
          h('button.btnp', {
            disabled: blocked || null,
            title: blocked
              ? (v.busy ? 'a run holds the worker; this would queue behind it'
                : v.stale || inflight ? 'the structure has changed — checking it again'
                  : !typed ? 'nothing to run'
                    : 'the checks refuse this structure; the list above says why')
              : 'checks the structure once more against the bench as it is now, then queues the run',
            onclick: start,
          }, c ? `Start · ${c.prefix ? c.prefix + ' ' : ''}${fmt.duration(c.total_s)}` : 'Start'),
          h('button', {
            class: armed ? 'btns armed' : 'btns',
            disabled: !typed || null,
            title: overwrites
              ? `${stem} already exists — saving writes over it, and the file it replaces is not in the undo history`
              : 'writes the structure to a file, with the bench values it was saved with',
            onclick: save,
          }, armed ? `overwrite ${stem}?` : 'Save recipe'),
          h('button.btns', {
            disabled: !typed || null,
            title: 'checks the structure without touching the bench',
            onclick: () => { showAllChecks = true; showFlat = false; revalidate({ now: true }); },
          }, 'Dry run')),
      ]);
    }

    /**
     * One submission on the wire at a time. `busy` comes from the store, which
     * only learns of a run when its `RunQueued` arrives — so two clicks inside
     * that window both pass the check and the service, which queues every
     * accepted POST, starts two pipelines. On a rig that is a night of the
     * sample's life. A plain variable checked synchronously, as `views/bench.js`
     * guards its Run: disabling the button re-renders, and a re-render between
     * the mousedown and the mouseup eats the click.
     */
    async function start() {
      if (submitting || !typed) return;
      submitting = true;
      try {
        const out = await api.startPipeline(typed, name || undefined);
        notify(`queued ${out.run_id} → ${out.folder}`, 'ok');
      } catch (error) {
        // 422 carries the checks, and a refused Start is exactly when the
        // operator needs all of them rather than the first sentence.
        notify(error.text || String(error), error.level || 'warn', error.checks || null);
        if (error.checks) { answer = { ...(answer || {}), valid: false, checks: error.checks }; answered = typed; }
      } finally {
        submitting = false;
        render();
      }
    }

    /**
     * Where the bench has moved since the recipe was saved. One line per
     * parameter — `bace.vpre  saved 1.020 · bench 0.800` — and two buttons:
     * put the bench back to the recipe's values (the ordinary `PUT`, one per
     * module, so the cards and every node form move with it), or keep the
     * bench and dismiss. A recipe saved before the bench was recorded says
     * so instead, once.
     */
    function recipeNote(v) {
      if (!loaded) return null;
      const byName = store.getState().modules.byName;
      if (!recorded(loaded)) {
        return h('div.recipe-note',
          h('span', { text: `${loaded.name} was saved before the bench was recorded with it — what its nodes do not override is whatever the bench has now.` }),
          h('button.btng', { onclick: () => { loaded = null; render(); } }, 'ok'));
      }
      const moved = drift(loaded, byName, { tree: typed, rows: (v && v.rows) || null });
      if (!moved.length) return null;
      return h('div.recipe-note',
        h('div.rn-head',
          h('span', { text: `the bench has moved since ${loaded.name} was saved — its nodes will run with the bench, not the file:` }),
          h('span', { style: { flex: '1' } }),
          h('button.btns', {
            title: 'put the recipe’s values back onto the bench, one module at a time',
            onclick: () => restoreBench(moved),
          }, '↺ bench to recipe'),
          h('button.btng', { title: 'keep the bench as it is', onclick: () => { loaded = null; render(); } }, 'keep bench')),
        h('div.rn-list', moved.map((it) => h('div.rn-row',
          h('span.l', { text: `${it.module}.${it.name}` }),
          h('span.v', { text: `saved ${showValue(it.saved, it.unit)}` }),
          h('span.v.now', { text: `bench ${showValue(it.now, it.unit)}` }),
          h('i', { text: it.unit })))));
    }

    async function restoreBench(moved) {
      const bodies = restore(moved);
      try {
        for (const [module, params] of Object.entries(bodies)) {
          // The response is the module entry as the bench now has it; the
          // store's copy is what every card and node form draws from.
          store.applyModule(await api.setParams(module, params));
        }
        notify(`bench back to ${loaded.name}: ${moved.map((m) => `${m.module}.${m.name}`).join(', ')}`, 'ok');
      } catch (error) {
        notify(error.text || String(error), 'warn');
      }
      revalidate({ now: true });
      render();
    }

    /**
     * Armed is a confirmation, not a mode — the same six seconds Park uses.
     *
     * Armed **for one name**, not a flag: a name changed between the two
     * clicks would otherwise carry the arming to a different file and
     * overwrite that one with no confirmation at all. The rule
     * `monitor.scopedTo` applies to an armed Abort, for the same reason.
     */
    function armSave(name) {
      saveArmed = name;
      clearTimeout(saveArmedTimer);
      saveArmedTimer = name ? setTimeout(() => { saveArmed = null; render(); }, 6000) : null;
    }

    async function save() {
      const stem = fileStem(name || (typed && typed.name) || '');
      if (!stem) {
        notify('a saved recipe needs a name — type one in the name field first.', 'warn');
        return;
      }
      // A name that is already a file: say what it will replace, and take the
      // second click for it. `recipes` is `GET /pipelines/saved`, re-read after
      // every save, so this is the service's list rather than a guess.
      if (saveArmed !== stem && recipes.some((r) => r.name === stem)) {
        armSave(stem);
        const was = recipes.find((r) => r.name === stem);
        notify(`${stem} already exists${was && was.saved_at ? ` — saved ${fmt.clock(was.saved_at)}` : ''}. `
          + 'Click again to write over it, or type another name.', 'warn');
        // `notify` draws the strip and nothing else; the button has to say
        // what the second click will do, or the arming is invisible where the
        // finger already is.
        render();
        return;
      }
      armSave(null);
      try {
        const out = await api.savePipeline(typed, stem);
        notify(`saved ${out.name} → ${out.path}`, 'ok');
        await loadRecipes();
        // Saved just now, so the file and the bench agree; the note has
        // nothing to say until the bench moves under it.
        loaded = recipes.find((r) => r.name === out.name) || null;
      } catch (error) {
        notify(error.text || String(error), 'warn');
      }
      render();
    }

    // -- what it will do, in order -----------------------------------------

    function renderSchedule(v) {
      keyed(schedHeadEl, JSON.stringify([v.counters, v.cost && v.cost.t_shot_source, v.stale]), () => [
        h('span.cn', 'what it will do, in order'),
        h('span.cs', {
          text: v.cost
            ? `shot time from the ${v.cost.t_shot_source === 'journal' ? 'journal' : 'default'} · `
              + 'settle from what this bench has measured'
            : 'from the last check of the structure',
        }),
        v.stale ? h('span.tag.nb', { text: 'stale' }) : null,
      ]);

      keyed(chartEl, JSON.stringify([v.blocks, v.cost]),
        () => chart((w) => scheduleModel({ blocks: v.blocks, cost: v.cost, width: w })));

      // Keyed on the *answer*, not on the shape of it. Revalidating the same
      // tree — a Dry run, or a bench read-back — leaves the node paths and
      // the count exactly where they were while `needs_operator`, the settle
      // times, the estimates and the shots all move: the 331 coming online
      // turns every `waits for the operator` off, and a key built from paths
      // would have kept them on screen beside a chart that had already
      // dropped them. `derived()` stamps each answer it folds.
      keyed(stepsEl, JSON.stringify([v.id, [...expanded].sort(), showFlat]), () => {
        if (!v.nodes.length) return h('p.absent', 'nothing scheduled yet.');
        return [
          h('div.schedbar',
            h('span.cs', {
              text: showFlat
                ? `every step, in order — ${((v.shown && v.shown.schedule) || []).length}, loop boundaries included`
                : 'three scales — expand a temperature for its levels, a level for its modules',
            }),
            h('span', { style: { flex: '1' } }),
            h('button.btng', { onclick: () => { showFlat = !showFlat; render(); } }, showFlat ? 'nested' : 'flat')),
          showFlat ? flatSteps(v) : v.nodes.map((node) => schedNode(node)),
        ];
      });

      keyed(bindEl, JSON.stringify(bindings(v)), () => bindings(v).map((text) => h('span.inh', { text })));

      const grid = v.grid;
      keyed(gridEl, JSON.stringify([grid && grid.temperatures, grid && grid.levels, grid && grid.count]), () => {
        if (!grid) return null;
        return [
          h('div.cs', { text: `the cells it fills · ${fmt.plural(grid.count, 'module run')} over ${grid.temperatures.length} × ${grid.levels.length}` }),
          h('div.lst', { style: { gridTemplateColumns: `auto repeat(${grid.levels.length}, 1fr)` } },
            h('span.h', 'T / K'),
            grid.levels.map((led) => h('span.h', { text: Number(led).toFixed(3) })),
            grid.temperatures.flatMap((t) => [
              h('span', { style: { fontWeight: '500' }, text: t === null ? fmt.ABSENT : String(t) }),
              ...grid.levels.map((led) => h('span', {
                style: { color: 'var(--grey)' },
                text: grid.cell(t, led).map((leaf) => leaf.module).join(' → ') || fmt.ABSENT,
              })),
            ])),
        ];
      });

      const folder = v.shown && v.shown.folder_pattern;
      // The operator sees the folder's name under the out directory; the full
      // path is one hover away (#38).
      keyed(folderEl, JSON.stringify([folder, v.counters.modules]), () => (folder
        ? [`writes ${fmt.plural(v.counters.modules || 0, 'folder')} under `,
          h('span.path', { title: folder, text: folder.replace(/\/+$/, '').split('/').pop() + '/' }),
          ' — the stamp is taken at Start, so a Dry run cannot name it. '
          + 'A run that stops early says kept of requested.']
        : ''));
    }

    /** One line of the nested schedule, at whichever of the three scales it is. */
    function schedNode(node) {
      if (node.kind === 'module') {
        return h('div.sr.mod', { style: { marginLeft: `${node.depth * 16}px` } },
          h('span.nk.mod', icon('module')),
          h('span.n', { text: node.module }),
          node.led_v !== null && node.led_v !== undefined ? h('span.d', { text: fmt.volts(node.led_v) + ' LED' }) : null,
          node.temperature && node.temperature.k !== null && node.temperature.k !== undefined
            ? h('span.d', {
              title: node.temperature.how === 'module'
                ? `a temperature node set this and it binds the rest of the run (${node.temperature.node_path})`
                : `the ${node.temperature.node_path} loop`,
              text: fmt.kelvin(node.temperature.k) + (node.temperature.how === 'module' ? ' (bound)' : ''),
            })
            : null,
          h('span', { style: { flex: '1' } }),
          node.relay_transition ? h('span.cs', { text: `relay ${node.relay_from} → ${node.relay}` }) : null,
          node.shots ? h('span.cs', { text: fmt.plural(node.shots, 'shot') }) : null,
          h('span.cs', { text: fmt.duration(node.estimate_s) }));
      }
      const open = expanded.has(node.node_path);
      const spec = tree.LOOPS[node.loop];
      return h('div',
        h('div.sr' + (open ? '.on' : ''), {
          style: { marginLeft: `${node.depth * 16}px` },
          onclick: () => { if (open) expanded.delete(node.node_path); else expanded.add(node.node_path); render(); },
        },
          h('span.nk', { text: open ? '▾' : '▸' }),
          h('span.n', { text: node.node_path.split('/').pop() }),
          h('span.d', { text: `${(node.index ?? 0) + 1} of ${node.count}` }),
          node.needs_operator
            ? h('span.mon-wait', { title: 'the 331 is not answering — the run pauses here until someone sets the cryostat and resumes', text: 'waits for the operator' })
            : null,
          node.loop === 'temperature'
            ? h('span.d', { text: `settle ${node.settle_s === null || node.settle_s === undefined ? fmt.ABSENT : fmt.duration(node.settle_s)}` })
            : null,
          h('span', { style: { flex: '1' } }),
          h('span.cs', { text: fmt.plural(node.modules, 'run') }),
          node.shots ? h('span.cs', { text: fmt.plural(node.shots, 'shot') }) : null,
          h('span.cs', { text: fmt.duration(node.measure_s) + (spec && node.loop === 'temperature' ? ' measuring' : '') })),
        open ? node.children.map((child) => schedNode(child)) : null);
    }

    /**
     * Every step, in order — the service's own list, not the module leaves of
     * it.
     *
     * Behind a switch because the canonical tree is 198 of them and
     * `step 3 of 198` is the number `ui-rules` §5 exists to refuse; but the
     * flat list is what "in order" literally means, and the ninety leaves are
     * not it. The hundred and eight it leaves out are the loop boundaries —
     * which is where every settle and every LED level change happens, and so
     * exactly what an operator opens this view to look at.
     */
    function flatSteps(v) {
      const steps = (v.shown && v.shown.schedule) || [];
      return h('div.flatsteps', steps.map((step, i) => h('div.fs' + (step.kind === 'module' ? '.mod' : ''),
        h('span.i', { text: String(i + 1) }),
        h('span.k', { text: step.kind === 'loop-enter' ? 'enter' : step.kind === 'loop-exit' ? 'exit' : 'run' }),
        h('span.p', { text: step.node_path }),
        step.needs_operator ? h('span.mon-wait', { text: 'waits' }) : null,
        h('span.cs', { text: fmt.duration(step.estimate_s) }))));
    }

    /**
     * The three bindings of the contract, said only where the tree has them.
     * Read off the resolved params rather than assumed from the shape: an
     * `led_v` is inherited because the service says `source: "inherited"`.
     */
    function bindings(v) {
      const leaves = v.leaves;
      const out = [];
      if (leaves.some((l) => l.params.led_v && l.params.led_v.source === 'inherited')) {
        out.push('led_v ← the illumination loop, which owns it');
      }
      const voc = leaves.filter((l) => l.voc && l.voc.how);
      if (voc.length) {
        out.push(`V_oc ← ${voc[0].voc.how} at the same led_v · ${fmt.plural(voc.length, 'centred scan')}`);
      }
      const transitions = leaves.filter((l) => l.relay_transition).length;
      if (transitions) out.push(`relay moves at ${fmt.plural(transitions, 'jv ↔ bace boundary', 'jv ↔ bace boundaries')}`);
      const bound = leaves.filter((l) => l.temperature && l.temperature.how === 'module');
      if (bound.length) {
        out.push(`a temperature node binds ${fmt.plural(bound.length, 'later node')} — the rest of the run, not the rest of one iteration`);
      }
      return out;
    }

    // -- the loop --------------------------------------------------------

    /**
     * Renders held back while a click is in flight — the M2 finding, and it
     * bites harder here: an answer arriving between the mousedown and the
     * mouseup would replace the row the operator is clicking ✕ on, and the
     * click would land on nothing.
     */
    let pressing = false;
    let missed = false;
    body.addEventListener('pointerdown', (e) => { if (e.target.closest('button')) pressing = true; });
    for (const kind of ['click', 'pointercancel']) {
      body.addEventListener(kind, () => {
        pressing = false;
        if (missed) { missed = false; render(); }
      }, true);
    }
    body.addEventListener('pointerup', () => setTimeout(() => {
      if (!pressing) return;
      pressing = false;
      if (missed) { missed = false; render(); }
    }, 0));

    function render() {
      if (pressing) { missed = true; return; }
      const v = view();
      renderHead(v);
      renderTree(v);
      renderNode(v);
      renderValues(v);
      renderCost(v);
      renderChecks(v);
      renderActions(v);
      renderSchedule(v);
    }

    async function loadRecipes() {
      try {
        const out = await api.savedPipelines();
        recipes = out.recipes || [];
      } catch { recipes = []; }
    }

    /**
     * Reopen what this session last validated. `GET /pipelines/last` is the
     * route's whole purpose ("so the UI can reopen it") and it is the
     * difference between a reload during a four-hour sweep costing nothing
     * and costing the tree.
     */
    async function boot() {
      await loadRecipes();
      try {
        const last = await api.lastPipeline();
        if (last && last.tree) {
          typed = last.tree;
          name = last.name || '';
          // Open the first temperature so the schedule shows its levels: the
          // one expansion that makes a nine-hour sweep legible at a glance.
          expanded = new Set();
        }
      } catch {
        // 404 is the ordinary case: nothing validated in this session yet.
      }
      if (typed) revalidate({ now: true });
      render();
    }

    /**
     * The bench snapshot moves several of the checks — an instrument that went
     * away, the chain, whether the 331 answers — so a read-back re-asks, the
     * way `views/bench.js` re-validates its cards on `read_at`.
     *
     * And **nothing else on the stream reaches this tab**. The store notifies
     * once per animation frame for the length of a run, and this screen
     * describes the *next* one: it reads whether the worker is held, the
     * catalogue, and the read-back, and no shot, phase or progress frame moves
     * any of the three. So a scan running under an open pipeline tab costs it
     * exactly nothing — which is the same argument that put the timing diagram
     * below the run in M4, applied to a whole view.
     */
    let validatedAt = null;
    let storeKey = null;
    const off = store.subscribe((state) => {
      const at = (state.bench && state.bench.read_at) || null;
      if (at !== validatedAt) {
        validatedAt = at;
        if (typed && answered) revalidate();
      }
      const run = state.runs[state.activeRunId] || null;
      const key = `${Boolean(run && !run.parked_at)}|${state.modules.at}|${at}`;
      if (key === storeKey) return;
      storeKey = key;
      render();
    });

    boot();
    return {
      dispose() {
        off();
        clearTimeout(timer);
        clearTimeout(saveArmedTimer);
        window.removeEventListener('keydown', onKey);
      },
    };
  },
};
