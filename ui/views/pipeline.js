// The pipeline editor owns the typed tree; validation supplies checks, resolved
// schedule, counters, cost and folder (`docs/service-contract.md` §7). Every commit
// validates so Start reflects missing V_oc, unsafe LED levels and illegal inherited
// overrides. Calls coalesce: the 9 × 5 tree measures 110 ms and 340 kB per response
// on this bench, mainly 90 resolved parameter sets. Dry run deliberately rechecks and
// opens the schedule. Counts use service rounding rather than local expansion.
// Scheduling separates temperature, LED and scan scales (`docs/ui-rules.md` §5). All
// mutations use `change` so undo covers subtree removal, recipe replacement and
// override reset. Node forms share bench field components but use schedule-resolved
// values and provenance, including inherited LED and derived V_oc.

import { h, fill, keyed } from '../lib/dom.js';
import * as fmt from '../lib/format.js';
import { icon } from '../lib/icons.js';
import { cardModel, moduleTitle } from '../lib/fields.js';
import { renderRow, field, fold } from '../lib/card.js';
import { chart } from '../lib/charts/frame.js';
import { scheduleModel } from '../lib/charts/schedule.js';
import * as tree from '../lib/tree.js';
import { drift, recorded, restore, showValue, fileStem } from '../lib/recipe.js';
import { createHistory } from '../lib/undo.js';
import { benchHash } from '../lib/route.js';

/**
 * The interval coalesces committed changes, not keystrokes. Reordering several
 * positions and edits during validation can share work. Responses measure about 110
 * ms and a third of a megabyte on this bench, so repeated requests need the same
 * pending-request discipline as bench refetches and chart redraws.
 */
const VALIDATE_MS = 250;

export default {
  route: 'pipeline',
  title: 'Sequence',

  mount(container, { store, api, notify }) {
    // -- the state this view owns ------------------------------------------
    /**
     * A `null` typed tree represents the empty pipeline state (`docs/ui-rules.md` §9).
     */
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
     * Overwriting a recipe needs confirmation because the service replaces its file
     * and undo cannot recover it. Arming lasts six seconds, matching Park's
     * confirmation window.
     */
    let saveArmed = null;
    let saveArmedTimer = null;
    let recipes = [];
    /**
     * `loaded` retains the opened recipe for bench comparison. Its note lists
     * differing values and offers restoration. Start still uses the current bench, so
     * the comparison must make any changed execution visible
     * (`docs/service-contract.md` §7).
     */
    let loaded = null;
    /**
     * Undo retains whole snapshots because tree states are small JSON and operations
     * such as clearing overrides or opening a recipe are not invertible.
     * Per-operation inverses would duplicate mutation logic.
     */
    const history = createHistory();
    /**
     * Undo snapshots include every mutable view value so restoration covers selection
     * and recipe identity as well as tree structure.
     */
    const snapshot = () => ({ typed, name, selected, loaded });

    const body = h('div.pipe');
    fill(container,
      h('h1', 'Sequence'),
      body);

    const structureEl = h('div.card.pipe-col');
    const scheduleEl = h('div.card.pipe-col');
    fill(body, structureEl, scheduleEl);

    // Separate section keys prevent tree edits from rebuilding schedule charts and
    // validation responses from replacing fields while the operator types.
    const headEl = h('div.ch');
    const treeEl = h('div.tree');
    const nodeEl = h('div.nodeform');
    const valuesEl = h('div.vlists');
    const costEl = h('div.costs');
    const checksEl = h('div.checks');
    // The drift reading remains in flow. Only controls belong in a pinned bar
    // (`docs/ui-rules.md` §1).
    const noteEl = h('div.pipe-note');
    // The action row is a direct card-body child so sticky positioning can span the
    // panel. A nested containing block would restrict its travel.
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
     * `now` bypasses coalescing for deliberate Dry run and read-back requests. A
     * response applies only if `answered` still matches the displayed tree; edits
     * during an in-flight request must not accept obsolete validation. Stale Start
     * remains unavailable.
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
          // Validation failure clears authorization to Start. Unknown checks cannot
          // preserve a stale valid result.
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
     * Every mutation records undo through this function. `label` names the action so
     * Undo does not require memory of prior clicks. Byte-identical trees add no
     * history entry because recommitting a value changes nothing.
     */
    function change(next, { select = undefined, label = '', before = null } = {}) {
      // The recipe picker captures `before` ahead of changing `name` and `loaded`;
      // otherwise undo would retain the replacement recipe's identity.
      if (!tree.sameTree(typed, next)) history.push(before || snapshot(), label);
      typed = next;
      if (select !== undefined) selected = select;
      if (selected && !tree.nodeAt(typed, selected)) selected = null;
      revalidate();
      render();
    }

    /**
     * Undo and redo restore the whole snapshot, including selection and recipe
     * identity, so removing nodes or opening files returns to the same screen state.
     */
    function step(direction) {
      const entry = direction === 'redo' ? history.redo(snapshot()) : history.undo(snapshot());
      if (!entry) return;
      ({ typed, name, selected, loaded } = entry.state);
      if (selected && !tree.nodeAt(typed, selected)) selected = null;
      notify(`${direction === 'redo' ? 'Redone' : 'Undone'}${entry.label ? ': ' + entry.label : ''}`, 'ok');
      revalidate();
      render();
    }

    /**
     * Keyboard undo leaves focused fields to browser text undo; stealing it would
     * reverse a node action instead of a digit. The window listener also works
     * without focus, including after node deletion.
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
     * Derived models recompute only when tree or validation identity changes. A
     * 198-step schedule nests into 9 groups, 45 subgroups and 90 leaves, then gets
     * walked for timeline, grid and rows. Repeating that work per frame is
     * unnecessary. Identity keys are safe because `change` and `revalidate` replace
     * objects rather than mutate them.
     */
    let memo = null;
    /** Answer stamps let fold render keys distinguish revalidation results. */
    let folds = 0;

    function derived() {
      if (memo && memo.typed === typed && memo.answer === answer) return memo;
      folds += 1;
      const fresh = Boolean(answer) && tree.sameTree(typed, answered);
      /**
       * While validation is pending, the schedule retains the prior answer with stale
       * labels and Start disabled. Blanking it for about 350 ms, measured on this
       * bench, would falsely show no nodes despite a populated editor. Tree rows use
       * only matching answers: their iteration-based alignment could otherwise attach
       * another node's counts to the current row.
       */
      const nodes = tree.scheduleTree((answer && answer.schedule) || []);
      memo = {
        id: folds,
        typed,
        answer,
        fresh,
        /** Only validation matching the displayed tree is current. */
        answered: fresh ? answer : null,
        /** The drawn answer can remain stale while validation is pending. */
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
        h('span.cn', 'Steps'),
        inflight ? h('span.tag', { text: 'checking' }) : null,
        h('span', { style: { flex: '1' } }),
        addControls(v),
      ]);
    }

    /**
     * Insertion targets the selected loop's children or the selected module's
     * siblings. Adding a module selects it, so defaulting the next insertion to root
     * would move a paired `bace` outside its illumination loop and lose access to the
     * matching `jv_bace` V_oc.
     */
    function addTarget() {
      if (!typed || !selected) return null;
      const at = tree.nodeAt(typed, selected);
      if (at && at.kind === 'loop') return selected;
      return selected.length ? selected.slice(0, -1) : null;
    }

    /**
     * Adding a loop above a root module wraps that module, matching a single scan
     * placed under a sweep. The service permits either root shape
     * (`docs/service-contract.md` §7).
     */
    function addControls(v) {
      const into = addTarget();
      const at = into ? tree.nodeAt(typed, into) : null;
      const where = !typed || !at ? 'root' : `into ${at.kind === 'loop' ? at.loop : at.module}`;
      const picker = h('select.v', { title: 'The module this node runs.' },
        ...v.moduleNames.map((n) => h('option', { value: n }, moduleTitle(n))));
      const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
      return h('span.addbar',
        h('span.cs', { text: where }),
        ...tree.LOOP_ORDER.map((kind) => h('button.btng', {
          title: `${cap(kind)} loop. ${tree.LOOPS[kind].owns ? `Sets ${tree.LOOPS[kind].owns.split(' ')[0]} for everything inside.` : 'Repeats its children.'}`,
          onclick: () => addNode(tree.newLoop(kind), into, `add the ${kind} loop`),
        }, `+ ${cap(kind)}`)),
        picker,
        h('button.btng', { onclick: () => addNode(tree.newModule(picker.value), into, `add ${picker.value}`) }, '+ Measurement'));
    }

    function addNode(node, into, label = 'add a node') {
      if (!typed) return change(node, { select: [], label });
      if (into) {
        const parent = tree.nodeAt(typed, into);
        const index = (parent.children || []).length;
        return change(tree.insertAt(typed, into, node), { select: [...into, index], label });
      }
      if (typed.kind === 'module' && node.kind === 'loop') {
        // Wrapping preserves the root module as the loop's only child.
        return change({ ...node, children: [typed] }, { select: [], label });
      }
      if (typed.kind !== 'loop') {
        notify('The root is one module. Add a loop first, or remove the module.', 'warn');
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
          return h('p.absent', 'Add a step or open a saved sequence.');
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
        if (row.owns) tags.push(h('span.inh', { title: 'This loop sets it for every node inside.', text: row.owns.split(' ')[0] }));
        // `needs_operator` comes from bench capability, not tree shape. Only an
        // answering attached controller removes the pause. Long operator waits must
        // remain visibly distinct from acquisition.
        if (row.needs_operator) tags.push(h('span.tag.nb', { title: 'No 331 reading. The run pauses here for a manual set.', text: 'pauses' }));
        if (row.measure_s) tags.push(h('span.cs', { text: fmt.duration(row.measure_s) }));
      } else {
        const entry = v.catalogue[row.module];
        if (!entry) tags.push(h('span.tag.bad', { title: 'No module by that name on this service.', text: 'unknown module' }));
        else if (entry.status !== 'built') tags.push(h('span.tag.nb', { text: entry.status }));
        if (row.centre_on_voc) {
          tags.push(h('span.inh', {
            title: row.voc ? `${row.voc.how} ${row.voc.node_path || ''} at ${fmt.volts(row.voc.led_v)}` : 'No Voc source in scope.',
          }, icon(row.voc ? 'inherit' : 'warn'),
          h('span', { text: row.voc ? `Voc ← ${row.voc.how}` : 'Voc' })));
        }
        if (row.relay_transition) {
          tags.push(h('span.cs', { title: 'The relay moves here; refused while a source is live.', text: `relay → ${row.relay}` }));
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
        h('span.n', { text: row.kind === 'loop' ? row.loop : moduleTitle(row.module) }),
        // A module node uses bench values by default; only overrides need a row mark.
        row.kind === 'loop' ? h('span.d', { text: row.summary }) : overrideTag(row),
        h('span', { style: { flex: '1' } }),
        tags,
        h('span.rowact',
          h('button.btng', { title: 'Move up', onclick: () => moveNode(row.path, -1) }, '↑'),
          h('button.btng', { title: 'Move down', onclick: () => moveNode(row.path, 1) }, '↓'),
          h('button.btng', {
            title: 'Removes this node and its children. Ctrl-Z undoes.',
            onclick: () => change(tree.removeAt(typed, row.path),
              { select: null, label: `remove ${row.kind === 'loop' ? 'the ' + row.loop + ' loop' : row.module}` }),
          }, '✕')));
    }

    /**
     * Reordering remaps selection with child indices. Otherwise moving past another
     * `bace` would silently show its form and send subsequent overrides to the wrong
     * node.
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
            h('span.cn', { text: node.kind === 'loop' ? `${node.loop} loop` : moduleTitle(node.module) }),
            // The bench link opens the module card supplying every unoverridden node
            // value.
            node.kind === 'module'
              ? h('a.btng.goto', {
                href: benchHash(node.module),
                title: `Opens the ${node.module} card. Values not overridden here come from it.`,
              }, 'Measure')
              : null,
            h('span', { style: { flex: '1' } }),
            // Node reset appears only with overrides. Dropping them restores bench
            // inheritance and reduces the form to its remaining differences.
            node.kind === 'module' && Object.keys(node.params || {}).length
              ? h('button.btng.undo', {
                title: 'Drops every override; the bench values apply.',
                onclick: () => change(tree.clearParams(typed, selected),
                  { label: `drop ${node.module}’s overrides` }),
              }, 'Reset')
              : null,
            h('button.btng', { onclick: () => { selected = null; render(); } }, 'Close')),
          node.kind === 'loop' ? loopForm(node, row) : moduleForm(node, row, v, open));
      });
    }

    function openedFor(path) {
      if (!opened.has(path)) opened.set(path, new Set());
      return opened.get(path);
    }

    /**
     * The loop form explicitly distinguishes a list from a range. A list names
     * setpoints; a range specifies their construction, and the operator must see
     * which representation applies.
     */
    function loopForm(node, row) {
      const spec = tree.LOOPS[node.loop];
      if (!spec) return h('p.absent', { text: `No ${node.loop} loop on this service.` });
      const ctx = loopCtx();
      const rows = [];
      if (spec.count) {
        rows.push(field(loopSpec(node, row, { name: 'count', label: 'Count', type: 'int', unit: '',
          doc: 'How many times the children run. Nothing changes between iterations: it is the outer averaging loop.' }), ctx, { name: '' }, {}));
      } else {
        const form = tree.valueForm(node);
        // The count beside a range is the service's: `range_values` rounds rather
        // than truncates, so 1.010 → 1.030 step 0.005 is five levels, not four.
        rows.push(h('div.pf.range',
          h('span.l', 'Values'),
          h('span.v.rng',
            h('span.seg', { role: 'group', 'aria-label': 'values as' },
              ...['list', 'range'].map((f) => h('button.opt', {
                type: 'button',
                class: form === f ? 'on' : '',
                'aria-pressed': form === f ? 'true' : 'false',
                onclick: () => switchForm(f, row),
              }, f === 'list' ? 'List' : 'Range'))),
            form === 'list' ? listInput(node, spec) : null,
            form === 'range' ? spec.range.map((n, i) => h('input.v', {
              value: node[n] === undefined ? '' : String(node[n]),
              title: n,
              onchange: (e) => commitField(n, e.target.value),
            })) : null,
            form === 'range' && row && row.values
              ? h('i.pts', { title: 'Counted by the service, rounding the span.', text: fmt.plural(row.values.length, 'value') })
              : null),
          h('span.src')));
      }
      for (const f of spec.fields) rows.push(field(loopSpec(node, row, f), ctx, { name: '' }, {}));
      return h('div.pr', rows);
    }

    /**
     * Form switching preserves execution or refuses. `tree.canSwitchForm` explains
     * unresolved ranges with no list values and uneven lists that cannot become an
     * equivalent range.
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

    /** The list input parses comma-separated values on commit. */
    function listInput(node, spec) {
      const values = node[spec.list] || [];
      return h('input.v.vlist', {
        value: values.map((v) => Number(v)).join(', '),
        title: `${spec.list}: the values, in the order they run`,
        onchange: (e) => {
          const raw = e.target.value.trim();
          if (!raw) return commitField(spec.list, null);
          const parsed = raw.split(/[\s,]+/).filter(Boolean).map(Number);
          if (parsed.some((n) => !Number.isFinite(n))) {
            notify(`${spec.list}: not a list of numbers. Separate them with commas.`, 'warn');
            return render();
          }
          return commitField(spec.list, parsed);
        },
      });
    }

    /**
     * Loop fields use module-field specifications for shared value, provenance and
     * explanation rendering. `node` identifies tree-owned values eligible for reset;
     * service defaults cannot be removed as node overrides.
     */
    function loopSpec(node, row, f) {
      const typedHere = node[f.name] !== undefined;
      const fallback = row && row.detail ? row.detail[f.name] : undefined;
      return {
        ...f,
        value: typedHere ? node[f.name] : (fallback === undefined ? null : fallback),
        source: typedHere ? 'edited' : 'default',
        detail: typedHere ? 'typed on this node' : 'default',
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
     * Loop fields require finite numbers on the wire, unlike coercible module
     * parameters. Parsing here gives a field-specific refusal rather than a service
     * error about JSON (`docs/service-contract.md` §7).
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
     * The module form renders schedule-resolved parameters through the bench
     * components. Inherited `led_v` and derived `voc` retain their actual node
     * provenance (`docs/ui-rules.md` §6). `spec.node` identifies which edited values
     * this node owns because bench edits and node overrides share the edited layer.
     */
    function moduleForm(node, row, v, open) {
      const catalogue = v.catalogue[node.module];
      if (!catalogue) {
        return h('p.absent', { text: `No module ${node.module} on this service.` });
      }
      const overrides = node.params || {};
      /**
       * The form prefers the resolved node parameters. Invalid trees can return
       * `schedule: null`; relying only on schedule would hide a mistyped field and
       * its reset precisely when needed. Catalogue values plus node overrides keep
       * the form available, with a note that loop bindings are unresolved.
       */
      const resolved = (row && row.params) || null;
      const entry = {
        ...catalogue,
        // The catalogue's estimate, sentence and parts alike, describes the bench
        // axis rather than this node. The fallback omits the point count instead
        // of showing another form's.
        estimate_text: '', estimate: null, estimate_s: row ? row.estimate_s : null, last: null, needs: [],
        params: (catalogue.params || []).map((p) => {
          // Schedule values omit `editable`; catalogue editability describes bench
          // sources. Node editability must also exclude inherited and derived
          // sources, matching `params.LOCKED`. Otherwise an illumination-loop `led_v`
          // input would offer an override guaranteed to fail `tree.owned-param`
          // validation.
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
      // `bench: null` excludes present instrument state from a node that can run
      // hours later. `form: 'node'` keeps loop bindings, overrides and node-only
      // shutter, mode and settle fields visible. Unchanged parameters fold under
      // same-as-bench.
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
          ? h('p.chart-note', {
            title: 'The tree does not resolve, so the loops above bind nothing yet.',
          }, 'Unresolved: bench values with this node’s overrides. See Checks.')
          : row && row.runs > 1
            ? h('p.chart-note', {
              title: 'The loops above bind other values into the remaining runs.',
              text: `First of ${row.runs} runs${where.length ? ': ' + where.join(', ') : ''}`,
            })
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
     * The converter retains numeric types in saved trees rather than storing numeric
     * strings. Unconvertible values stay exactly typed so service validation supplies
     * its own reason. Fractional integer input such as 100.9 must not truncate
     * silently to 100 loops.
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
     * The value tables use journal-derived settling for each setpoint. Missing
     * measurements remain an em dash rather than a guessed duration
     * (`docs/service-contract.md` §7).
     */
    function renderValues(v) {
      // Only loops with physical setpoint lists need tables. A repeat count does not
      // require listing consecutive iteration numbers.
      const loops = v.rows.filter((r) => r.kind === 'loop' && r.spec && r.spec.list
        && r.values && r.values.length);
      keyed(valuesEl, JSON.stringify(loops.map((r) => [r.loop, r.values, r.iterations.map((it) => it.settle_s)])), () => {
        if (!loops.length) return null;
        return loops.map((row) => {
          const spec = row.spec;
          const settles = row.loop === 'temperature' ? row.iterations.map((it) => it.settle_s) : null;
          return h('div.vlist-block',
            h('div.cs', {
              title: settles ? 'Second row: the settle time this bench measured at each temperature.'
                : spec && spec.owns ? spec.owns : '',
              text: `${row.loop.charAt(0).toUpperCase() + row.loop.slice(1)}, ${fmt.plural(row.values.length, 'value')}`,
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
     * The cost summary labels lower-bound total duration as at least. Unknown
     * settling leaves temperature-wait duration absent and removes finish time; known
     * holds cannot stand in for the missing waits (`docs/service-contract.md` §7).
     */
    function renderCost(v) {
      keyed(costEl, JSON.stringify([v.cost, v.counters]), () => {
        if (!v.cost) return h('p.absent', 'Not checked');
        const c = v.cost;
        return [
          h('div.nums',
            num('Total', c.total_s, c.prefix),
            num('Measuring', c.measuring_s, ''),
            num('Waiting for T', c.waiting_s, c.waiting_s === null ? '' : c.prefix)),
          h('div.cs', {
            title: c.t_shot_s ? `Shot time from the ${c.t_shot_source === 'journal' ? 'journal' : c.t_shot_source}.` : '',
            text: c.finish_at ? `Finish ${fmt.clock(c.finish_at)}` : '',
          }),
          c.lower_bound
            ? h('p.chart-note', {
              title: c.unmeasured.length
                ? 'A settle takes 14 min to 2 h on this bench; the journal fills them in as they are measured.'
                : 'The catalogue has no estimate for them.',
              text: c.unmeasured.length
                ? `Lower bound: ${c.unmeasured.length} of ${c.per_temperature.length} settles unmeasured.`
                : `Lower bound: ${fmt.plural(c.unestimated, 'measurement')} not estimated.`,
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

    /**
     * The check summary collapses successful entries to a count (`docs/ui-rules.md`
     * §11).
     */
    function renderChecks(v) {
      const c = v.checks;
      keyed(checksEl, JSON.stringify([c, showAllChecks, v.stale, inflight]), () => {
        if (!c.total) {
          return h('p.absent', inflight ? 'Checking …' : 'Not checked');
        }
        const shown = showAllChecks ? c.ordered : c.notable;
        return [
          h('div.cksum',
            h('span.m', { text: fmt.plural(c.total, 'check') }),
            ...tree.LEVELS.filter((l) => c.counts[l]).map((l) => h('span.tag.' + tagClass(l), { text: `${c.counts[l]} ${l}` })),
            v.stale ? h('span.tag', { title: 'The steps changed after the last check.', text: 'stale' }) : null,
            h('span', { style: { flex: '1' } }),
            h('button.btng', { onclick: () => { showAllChecks = !showAllChecks; render(); } },
              showAllChecks ? 'Hide' : 'Show')),
          // The text leads; the node path and the check code follow as identifiers
          // (`docs/ui-rules.md` §1), the code being what manual 4.2 is indexed by.
          shown.length
            ? h('div.cklist', shown.map((check) => h('div.warn1.' + check.level, { title: check.code },
              levelMark(check.level),
              h('span', { text: check.text }),
              check.node_path ? h('span.cs', { text: check.node_path }) : null)))
            : null,
        ];
      });
    }

    const tagClass = (level) => (level === 'ok' ? 'ok' : level === 'warn' ? 'cost' : level === 'info' ? 'nb' : 'bad');
    // Informational checks retain their letter because borrowing a warning or success
    // glyph would imply a different severity.
    const levelMark = (level) => (level === 'info'
      ? h('span.ico', 'i')
      : icon(level === 'crit' ? 'crit' : level === 'ok' ? 'ok' : 'warn', { cls: 'ico' }));

    // -- Start, Save, Dry run ----------------------------------------------

    function renderActions(v) {
      const c = v.cost;
      const blocked = !v.answer || !v.answer.valid || v.busy || v.stale || inflight || !typed;
      const moved = loaded ? drift(loaded, store.getState().modules.byName, { tree: typed, rows: v.rows }) : [];
      // Overwrite confirmation compares the service filename stem. A name such as
      // cool down becomes cool_down, so raw text cannot identify its saved file.
      const stem = fileStem(name || (typed && typed.name) || '');
      const overwrites = recipes.some((r) => r.name === stem);
      const armed = Boolean(stem) && saveArmed === stem;
      const key = JSON.stringify([Boolean(typed), v.answer && v.answer.valid, v.busy, v.stale, inflight, c && c.total_s, name, recipes.map((r) => r.name), loaded && loaded.name, moved, history.depth, history.canRedo, history.undoLabel, history.redoLabel, saveArmed, overwrites]);
      keyed(noteEl, key, () => [recipeNote(v)]);
      keyed(actionsBarEl, key, () => [
        h('div.namerow',
          h('span.l', 'Name'),
          h('input.v', {
            value: name, placeholder: 'pipeline',
            title: 'Folder stem: <name>_YYYYMMDD_HHMMSS',
            // `name` follows every input event so validation or arming redraws cannot
            // replace uncommitted typing with the last committed value.
            oninput: (e) => { name = e.target.value.trim(); },
            onchange: (e) => { name = e.target.value.trim(); revalidate(); render(); },
          }),
          recipes.length
            ? h('select.v', {
              title: 'Opens a saved sequence.',
              onchange: (e) => {
                const found = recipes.find((r) => r.name === e.target.value);
                if (found && found.tree) {
                  // Opening a recipe replaces the tree in one click. Undo restores
                  // the whole prior snapshot, including name and source recipe, so
                  // unsaved composition remains recoverable without another
                  // confirmation.
                  const was = snapshot();
                  name = found.name;
                  loaded = found;
                  change(found.tree, { select: null, label: `open ${found.name}`, before: was });
                }
              },
            }, h('option', { value: '' }, 'Open …'),
            ...recipes.map((r) => h('option', { value: r.name }, r.name)))
            : null),
        h('div.btnrow',
          // Undo sits beside forward actions and names the operation rather than
          // merely counting history steps.
          h('button.btng', {
            disabled: !history.canUndo || null,
            title: history.canUndo
              ? `Ctrl-Z: ${history.undoLabel || 'the last change'}`
              : 'Nothing to undo.',
            onclick: () => step('undo'),
          }, history.canUndo && history.undoLabel ? `↶ Undo ${history.undoLabel}` : '↶ Undo'),
          history.canRedo
            ? h('button.btng', {
              title: `Ctrl-Shift-Z: ${history.redoLabel || 'the last undo'}`,
              onclick: () => step('redo'),
            }, '↷ Redo')
            : null,
          h('button.btnp', {
            disabled: blocked || null,
            title: blocked
              ? (v.busy ? 'A run holds the bench; this would queue behind it.'
                : v.stale || inflight ? 'Checking again.'
                  : !typed ? 'Nothing to run.'
                    : 'Refused; see Checks.')
              : 'Checks once more against the bench, then queues the run.',
            onclick: start,
          }, c ? `Start  ${c.prefix ? c.prefix + ' ' : ''}${fmt.duration(c.total_s)}` : 'Start'),
          h('button', {
            class: armed ? 'btns armed' : 'btns',
            disabled: !typed || null,
            title: overwrites
              ? `${stem} exists. Saving overwrites it; the old file is not in the undo history.`
              : 'Saves the steps and the present values.',
            onclick: save,
          }, armed ? `Overwrite ${stem}?` : 'Save'),
          h('button.btns', {
            disabled: !typed || null,
            title: 'Checks without touching the bench.',
            onclick: () => { showAllChecks = true; showFlat = false; revalidate({ now: true }); },
          }, 'Check')),
      ]);
    }

    /**
     * A synchronous flag permits one submission before `RunQueued` updates store
     * `busy`. Otherwise two clicks can queue two pipelines and expose the sample
     * twice. Disabling via redraw could consume the intended click, so the guard
     * leaves DOM intact.
     */
    async function start() {
      if (submitting || !typed) return;
      submitting = true;
      try {
        const out = await api.startPipeline(typed, name || undefined);
        notify(`Queued ${out.run_id}: ${out.folder}`, 'ok');
      } catch (error) {
        // A refused Start needs the complete 422 check list, not only its first
        // reason.
        notify(error.text || String(error), error.level || 'warn', error.checks || null);
        if (error.checks) { answer = { ...(answer || {}), valid: false, checks: error.checks }; answered = typed; }
      } finally {
        submitting = false;
        render();
      }
    }

    /**
     * The drift note lists saved-versus-bench differences and offers per-module
     * restoration or dismissal. Start uses the current bench, so these values must
     * remain visible. Records without a saved bench block explicitly identify that
     * missing comparison data.
     */
    function recipeNote(v) {
      if (!loaded) return null;
      const byName = store.getState().modules.byName;
      if (!recorded(loaded)) {
        return h('div.recipe-note',
          h('span', { text: `${loaded.name} has no saved bench values; nodes take the bench as it is.` }),
          h('button.btng', { onclick: () => { loaded = null; render(); } }, 'OK'));
      }
      const moved = drift(loaded, byName, { tree: typed, rows: (v && v.rows) || null });
      if (!moved.length) return null;
      return h('div.recipe-note',
        h('div.rn-head',
          h('span', { text: `Bench differs from ${loaded.name}. Nodes run with the bench.` }),
          h('span', { style: { flex: '1' } }),
          h('button.btns', {
            title: 'Puts the saved values back.',
            onclick: () => restoreBench(moved),
          }, 'Restore saved values'),
          h('button.btng', { title: 'Keeps the bench as it is.', onclick: () => { loaded = null; render(); } }, 'Keep bench')),
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
          // The response supplies the authoritative module entry used by cards and
          // node forms.
          store.applyModule(await api.setParams(module, params));
        }
        notify(`Bench set to ${loaded.name}: ${moved.map((m) => `${m.module}.${m.name}`).join(', ')}`, 'ok');
      } catch (error) {
        notify(error.text || String(error), 'warn');
      }
      revalidate({ now: true });
      render();
    }

    /**
     * Save arming expires after six seconds and belongs to one filename. Changing the
     * name must not transfer confirmation to another file, matching run-scoped Abort
     * ownership.
     */
    function armSave(name) {
      saveArmed = name;
      clearTimeout(saveArmedTimer);
      saveArmedTimer = name ? setTimeout(() => {
        saveArmed = null;
        // Arming expiry avoids rebuilding a name under the caret. Its commit renders
        // after focus leaves.
        if (!actionsBarEl.contains(document.activeElement)) render();
      }, 6000) : null;
    }

    async function save() {
      const stem = fileStem(name || (typed && typed.name) || '');
      if (!stem) {
        notify('Type a name first.', 'warn');
        return;
      }
      // An existing filename gets a named overwrite warning and second click. The
      // saved-recipe list refreshes after saving so confirmation uses service state.
      if (saveArmed !== stem && recipes.some((r) => r.name === stem)) {
        armSave(stem);
        const was = recipes.find((r) => r.name === stem);
        notify(`${stem} exists${was && was.saved_at ? ` (saved ${fmt.clock(was.saved_at)})` : ''}. `
          + 'Click again to overwrite.', 'warn');
        // The strip notification alone does not update the button. Its label must
        // also reveal what the confirmation click performs.
        render();
        return;
      }
      armSave(null);
      try {
        const out = await api.savePipeline(typed, stem);
        notify(`Saved ${out.name} to ${out.path}`, 'ok');
        await loadRecipes();
        // A successful save captures the current bench, so comparison stays empty
        // while values agree.
        loaded = recipes.find((r) => r.name === out.name) || null;
      } catch (error) {
        notify(error.text || String(error), 'warn');
      }
      render();
    }

    // -- what it will do, in order -----------------------------------------

    function renderSchedule(v) {
      keyed(schedHeadEl, JSON.stringify([v.counters, v.cost && v.cost.t_shot_source, v.stale]), () => [
        h('span.cn', {
          title: v.cost
            ? `Shot time from the ${v.cost.t_shot_source === 'journal' ? 'journal' : 'default'}; settles as measured on this bench.`
            : 'From the last check of the tree.',
        }, 'Schedule'),
        v.stale ? h('span.tag.nb', { text: 'stale' }) : null,
      ]);

      keyed(chartEl, JSON.stringify([v.blocks, v.cost]),
        () => chart((w) => scheduleModel({ blocks: v.blocks, cost: v.cost, width: w })));

      // The schedule key identifies the answer, not only its paths. Revalidation can
      // preserve tree shape while changing controller availability, settling,
      // estimates and shots. `derived()` stamps answers so those changes update every
      // fold.
      keyed(stepsEl, JSON.stringify([v.id, [...expanded].sort(), showFlat]), () => {
        if (!v.nodes.length) return h('p.absent', 'Nothing scheduled');
        return [
          h('div.schedbar',
            h('span.cs', {
              title: showFlat ? 'Every step in order, loop boundaries included.'
                : 'Expand a temperature for its levels, a level for its modules.',
              text: showFlat ? fmt.plural(((v.shown && v.shown.schedule) || []).length, 'step') : '',
            }),
            h('span', { style: { flex: '1' } }),
            h('button.btng', { onclick: () => { showFlat = !showFlat; render(); } }, showFlat ? 'Nested' : 'Flat')),
          showFlat ? flatSteps(v) : v.nodes.map((node) => schedNode(node)),
        ];
      });

      keyed(bindEl, JSON.stringify(bindings(v)), () => bindings(v).map((text) => h('span.inh', { text })));

      const grid = v.grid;
      keyed(gridEl, JSON.stringify([grid && grid.temperatures, grid && grid.levels, grid && grid.count]), () => {
        if (!grid) return null;
        return [
          h('div.cs', { text: `Grid ${grid.temperatures.length} × ${grid.levels.length}, ${fmt.plural(grid.count, 'measurement')}` }),
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
      // The folder stem appears beneath the output directory; hover exposes its full
      // path.
      // The stamp is taken at Start, which is why the stem shows its pattern;
      // manual 3.3 says so.
      keyed(folderEl, JSON.stringify([folder, v.counters.modules]), () => (folder
        ? [h('span.l', 'Output'), ' ',
          h('span.path', { title: folder, text: folder.replace(/[\\/]+$/, '').split(/[\\/]/).pop() + '/' })]
        : ''));
    }

    /** The renderer draws one nested schedule row at its appropriate progress scale. */
    function schedNode(node) {
      if (node.kind === 'module') {
        return h('div.sr.mod', { style: { marginLeft: `${node.depth * 16}px` } },
          h('span.nk.mod', icon('module')),
          h('span.n', { text: moduleTitle(node.module) }),
          node.led_v !== null && node.led_v !== undefined ? h('span.d', { text: 'LED ' + fmt.volts(node.led_v) }) : null,
          node.temperature && node.temperature.k !== null && node.temperature.k !== undefined
            ? h('span.d', {
              title: node.temperature.how === 'module'
                ? `Set by the temperature node ${node.temperature.node_path}; it holds for the rest of the run.`
                : `The ${node.temperature.node_path} loop.`,
              text: 'T ' + fmt.kelvin(node.temperature.k),
            }, node.temperature.how === 'module' ? h('span.tag', { text: 'bound' }) : null)
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
            ? h('span.mon-wait', { title: 'No 331 reading. The run pauses here until the cryostat is set and the run resumed.', text: 'pause' })
            : null,
          node.loop === 'temperature'
            ? h('span.d', { text: `settle ${node.settle_s === null || node.settle_s === undefined ? fmt.ABSENT : fmt.duration(node.settle_s)}` })
            : null,
          h('span', { style: { flex: '1' } }),
          h('span.cs', { text: fmt.plural(node.modules, 'run') }),
          node.shots ? h('span.cs', { text: fmt.plural(node.shots, 'shot') }) : null,
          h('span.cs', { title: spec && node.loop === 'temperature' ? 'Measuring time, without the settle.' : null, text: fmt.duration(node.measure_s) })),
        open ? node.children.map((child) => schedNode(child)) : null);
    }

    /**
     * The optional flat schedule includes every service step in order, including loop
     * boundaries where settling and LED changes occur. The main view instead uses
     * three progress scales (`docs/ui-rules.md` §5). A 198-step tree has ninety
     * module leaves; showing only those would omit 108 relevant boundaries.
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
     * Bindings appear only where resolved parameters declare them. Inheritance comes
     * from `source: "inherited"`, never an assumption based on tree shape
     * (`docs/ui-rules.md` §6).
     */
    function bindings(v) {
      const leaves = v.leaves;
      const out = [];
      if (leaves.some((l) => l.params.led_v && l.params.led_v.source === 'inherited')) {
        out.push('led_v ← illumination loop');
      }
      const voc = leaves.filter((l) => l.voc && l.voc.how);
      if (voc.length) {
        out.push(`Voc ← ${voc[0].voc.how}, ${fmt.plural(voc.length, 'centred scan')}`);
      }
      const transitions = leaves.filter((l) => l.relay_transition).length;
      if (transitions) out.push(`relay moves ${fmt.plural(transitions, 'time')}`);
      const bound = leaves.filter((l) => l.temperature && l.temperature.how === 'module');
      if (bound.length) {
        out.push(`T set by a node for ${fmt.plural(bound.length, 'later node')}`);
      }
      return out;
    }

    // -- the loop --------------------------------------------------------

    /**
     * Rendering waits through click dispatch. A validation response between mouse
     * press and release could replace the clicked row and consume actions such as
     * deletion.
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
     * The view reopens the session's last validated tree so reloading during a long
     * sweep preserves composition (`docs/service-contract.md` §7).
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
     * Read-back can change instrument, chain and controller checks, so `read_at`
     * triggers validation. Other dependencies are worker availability and catalogue
     * state. Shot, phase and progress frames describe the current run, not this
     * editor's next run, and must not rebuild the view.
     */
    let validatedAt = null;
    let validatedEdit = null;
    let storeKey = null;
    const off = store.subscribe((state) => {
      const at = (state.bench && state.bench.read_at) || null;
      if (at !== validatedAt || state.paramsEdited !== validatedEdit) {
        validatedAt = at;
        validatedEdit = state.paramsEdited;
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
