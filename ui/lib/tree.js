// The pipeline tree — the model half of M5, and the only file in `ui/` that
// knows the shape `docs/service-contract.md` §7 posts.
//
// Three things it is careful about, and each is a rule from somewhere:
//
//   * **Nothing is re-derived.** A range typed as `1.010 → 1.030 step 0.005`
//     is five levels or four depending on how the ratio is rounded, and the
//     service settled that once (`pipeline.range_values`: rounded, never
//     truncated, because the LabVIEW truncation wrote a four-level loop as
//     five). So the editor holds the tree *as typed* and reads every count,
//     every level and every cost off `POST /pipelines/validate`'s answer —
//     which returns the resolved tree beside the schedule. `treeRows` walks
//     the two in lockstep; before the first answer a row says what it can and
//     no more.
//   * **A `temperature` module binds the rest of the run**, not the rest of
//     one iteration (`docs/ui-plan.md` M5, `executor._Open`). The schedule
//     the service sends does not say so — `Step.detail.temperature_k` is the
//     enclosing *loop's* setpoint and is `null` under a module — so
//     `scheduleTree` re-walks the steps with the executor's own rule and
//     stamps what the cryostat will be at on every module below it.
//   * **`lower_bound` is "at least", never a promise** (`cost.lower_bound`
//     is set whenever any temperature has no measured settle behind it).
//     `costModel` carries the word, so no caller has to remember it.
//
// The DOM is `views/pipeline.js`; this is pure, and `ui/tests/tree.test.mjs`
// holds it down against a recorded validate with no browser in the way.

/**
 * The three loops, as data — label, fields, and how a row summarises one.
 *
 * Written out here rather than fetched because there is nowhere to fetch it
 * from: `GET /modules` describes module parameters, and a loop is not a
 * module. The tree schema lives in `docs/service-contract.md` §7 and in
 * `pipeline._LOOP_KEYS`, and this is that table. Every field carries its one
 * sentence, for the same reason a module parameter does (`ui-rules` §1): the
 * name is the label, not the explanation.
 */
export const LOOPS = {
  temperature: {
    label: 'temperature',
    /** The values field, and the range that can stand in for it. */
    list: 'values_k',
    range: ['start_k', 'stop_k', 'step_k'],
    unit: 'K',
    decimals: 1,
    fields: [
      { name: 'tolerance_k', unit: 'K', type: 'float', doc: 'how close to the setpoint counts as arrived — the reading must stay inside ± this for hold_s.' },
      { name: 'hold_s', unit: 's', type: 'float', doc: 'the dwell after the band is entered, before anything is measured.' },
      { name: 'timeout_s', unit: 's', type: 'float', doc: 'give up settling after this and ask the operator; the run never measures at a temperature it did not reach.' },
    ],
    owns: 'temperature_k of everything under it',
  },
  illumination: {
    label: 'illumination',
    list: 'levels_v',
    range: ['led_start_v', 'led_stop_v', 'led_step_v'],
    unit: 'V',
    decimals: 3,
    fields: [
      { name: 'led_low_v', unit: 'V', type: 'float', doc: 'the dark half of the LED cycle. It must sit below turn-on, or the dark half is not dark.' },
      { name: 'led_settle_s', unit: 's', type: 'float', doc: 'the wait after a level is set, before the first measurement at it.' },
    ],
    owns: 'led_v of every module inside it',
  },
  repeat: {
    label: 'repeat',
    count: 'count',
    fields: [],
    owns: null,
  },
};

/** Loop kinds in the order the "add" menu offers them: outermost first. */
export const LOOP_ORDER = ['temperature', 'illumination', 'repeat'];

/**
 * Modules that are never a useful tree node on their own.
 *
 * `park` is what every run ends with (`executor` runs it in `finally`), so a
 * `park` node asks for it twice; the bench strip is where a park by hand
 * belongs. Everything else in the catalogue is offered, including the
 * utilities — `wait`, `note` and `light` are exactly the nodes a pipeline is
 * composed of, and `light` is refused only when it is the run's *only*
 * module (`light.undone-by-park`), which is the service's check to make.
 */
export const NOT_A_NODE = new Set(['park']);

// -- constructing -----------------------------------------------------------

/** A fresh loop node, with the defaults the contract names. */
export function newLoop(kind) {
  const base = { kind: 'loop', loop: kind, children: [] };
  if (kind === 'temperature') return { ...base, values_k: [290], tolerance_k: 0.2, hold_s: 60, timeout_s: 1800 };
  if (kind === 'illumination') return { ...base, levels_v: [1.02], led_low_v: 0.4, led_settle_s: 2 };
  return { ...base, count: 2 };
}

/**
 * A fresh module node. `params` is empty on purpose and stays that way until
 * something is typed: "as on the bench · only what differs is typed here" is
 * the contract's own sentence for a module node, and an override written at
 * creation would pin the bench's value of the moment into the tree.
 */
export function newModule(name) {
  return { kind: 'module', module: name, params: {} };
}

// -- walking and editing ----------------------------------------------------
//
// A path is a list of child indices from the root: `[]` is the root itself,
// `[0, 1]` the second child of the first. Every operation returns a new tree
// and leaves the old one alone, so an edit that the service refuses can be
// dropped by keeping the tree that was there.

/** The node at `path`, or null. */
export function nodeAt(tree, path) {
  let node = tree;
  for (const index of path) {
    if (!node || !Array.isArray(node.children)) return null;
    node = node.children[index];
  }
  return node || null;
}

/** A copy of `tree` with `fn(node)` in place of the node at `path`. */
export function updateAt(tree, path, fn) {
  if (!path.length) return fn(tree);
  const [index, ...rest] = path;
  const children = (tree.children || []).slice();
  children[index] = updateAt(children[index], rest, fn);
  return { ...tree, children };
}

/** Insert `node` as a child of the node at `path`, at `index` (default: last). */
export function insertAt(tree, path, node, index = null) {
  return updateAt(tree, path, (parent) => {
    const children = (parent.children || []).slice();
    children.splice(index === null ? children.length : index, 0, node);
    return { ...parent, children };
  });
}

/**
 * Remove the node at `path`. Removing the root gives `null`, which is a
 * pipeline with no nodes — one of the empty states `ui-rules` §9 asks to be
 * drawn rather than crashed on.
 */
export function removeAt(tree, path) {
  if (!path.length) return null;
  const parent = path.slice(0, -1);
  const index = path[path.length - 1];
  return updateAt(tree, parent, (node) => {
    const children = (node.children || []).slice();
    children.splice(index, 1);
    return { ...node, children };
  });
}

/** Move the node at `path` `delta` places among its siblings. */
export function moveAt(tree, path, delta) {
  if (!path.length) return tree;
  const parent = path.slice(0, -1);
  const index = path[path.length - 1];
  const siblings = (nodeAt(tree, parent) || {}).children || [];
  const to = index + delta;
  if (to < 0 || to >= siblings.length) return tree;
  return updateAt(tree, parent, (node) => {
    const children = (node.children || []).slice();
    const [moved] = children.splice(index, 1);
    children.splice(to, 0, moved);
    return { ...node, children };
  });
}

/**
 * Where a path ends up when the node at `path` moves `delta` places among its
 * siblings — the companion `moveAt` needs, and the reason it needs one:
 * a path is a list of child indices, so a move renumbers the moved node *and*
 * every sibling it passed. A selection left at the old index is then pointing
 * at whichever node took that place, and with two `bace` siblings — which the
 * schema supports and the service numbers `bace` and `bace#2` — the next
 * override lands on the wrong node with nothing on screen saying so.
 */
export function remapPath(sel, path, delta) {
  if (!sel || !path.length) return sel;
  const parent = path.slice(0, -1);
  // Only a path among the moved node's own siblings is renumbered; anything
  // elsewhere in the tree is untouched by the move.
  if (sel.length <= parent.length) return sel;
  if (sel.slice(0, parent.length).join(',') !== parent.join(',')) return sel;
  const from = path[path.length - 1];
  const to = from + delta;
  const at = sel[parent.length];
  let moved = at;
  if (at === from) moved = to;
  else if (from < to && at > from && at <= to) moved = at - 1;
  else if (to < from && at >= to && at < from) moved = at + 1;
  return [...parent, moved, ...sel.slice(parent.length + 1)];
}

/**
 * Set one of a module node's `params` overrides. `null` **removes** the key
 * rather than storing a null, which is the same meaning `PUT null` has on a
 * bench card: drop what was typed here and fall back to the layer below —
 * here, the module's resolved ParamSet as it stands on the bench.
 */
export function setParam(tree, path, name, value) {
  return updateAt(tree, path, (node) => {
    const params = { ...(node.params || {}) };
    if (value === null) delete params[name];
    else params[name] = value;
    return { ...node, params };
  });
}

/** Set a field on a loop node; `null` removes it, so the service's default returns. */
export function setField(tree, path, name, value) {
  return updateAt(tree, path, (node) => {
    const next = { ...node };
    if (value === null || value === '') delete next[name];
    else next[name] = value;
    return next;
  });
}

/**
 * Switch a temperature or illumination loop between its list form and its
 * range form, on the values it has — so a range becomes the levels it
 * actually made, not the three numbers that made them.
 *
 * **Returns the tree unchanged when the conversion would have to invent
 * something**, and the caller says so rather than performing it. That is one
 * case, and it is a common one: a range typed a moment ago and not yet
 * validated has no levels anywhere — only the service expands a range — so
 * the first cut turned it into an empty list, and the inverse turned an
 * unvalidated list into `0 → 0`. Either way the operator's setpoints were
 * gone, silently, on a click meant to change how they are written.
 */
export function setValueForm(tree, path, form, resolved) {
  const node = nodeAt(tree, path);
  const spec = node && LOOPS[node.loop];
  if (!spec || !spec.list) return tree;
  const values = loopValues(node, resolved);
  if (!values || !values.length) return tree;
  return updateAt(tree, path, (n) => {
    const next = { ...n };
    for (const key of [spec.list, ...spec.range]) delete next[key];
    if (form === 'list') {
      next[spec.list] = values.slice();
      return next;
    }
    next[spec.range[0]] = values[0];
    next[spec.range[1]] = values[values.length - 1];
    // The step the values already have, where there are two to read it off.
    // A single value is a one-iteration loop whatever the step says
    // (`range_values` returns `(start,)` when start == stop), so the spacing
    // there is a placeholder and not a claim.
    next[spec.range[2]] = values.length > 1
      ? Math.abs(Number((values[1] - values[0]).toFixed(9)))
      : (spec.decimals === 3 ? 0.005 : 5);
    return next;
  });
}

/** Whether `setValueForm` can convert this loop without inventing values. */
export function canSwitchForm(node, resolved) {
  const values = node && LOOPS[node.loop] ? loopValues(node, resolved) : null;
  return Boolean(values && values.length);
}

/** Which form a loop's values are typed in — the range wins if any of it is set. */
export function valueForm(node) {
  const spec = LOOPS[node.loop];
  if (!spec || !spec.list) return null;
  return spec.range.some((k) => node[k] !== undefined) ? 'range' : 'list';
}

// -- the rows the structure card draws --------------------------------------

/**
 * The tree flattened to rows, each with the node **as typed**, the node **as
 * the service resolved it**, and the schedule entries it produced.
 *
 * Three sources rather than one because they answer different questions and
 * only the first is editable: the typed tree is what an input writes into and
 * what gets posted; the resolved tree is where the level list and the counts
 * come from; the schedule is where everything else does — the defaults the
 * service filled in (`tolerance_k`, `led_settle_s`), the resolved ParamSet of
 * a module node with its provenance, whether a temperature step will pause,
 * which relay boundary a node crosses, and which measurement supplies its
 * V_oc.
 *
 * They line up because `Loop.as_wire` emits its children in order and the
 * schedule is a walk of the same tree, so index paths address the first two —
 * and `zip` below matches the third **using only what a step says about
 * itself**: a module child takes one entry, a loop child takes the `count`
 * consecutive iterations its own `detail` declares. Nothing here reconstructs
 * a node path or a `#2` sibling token; those are the service's spelling and
 * stay there.
 *
 * `iterations` is every entry a node produced, not one: a `bace` inside 9
 * temperatures × 5 levels is 45 of them, all with different values bound into
 * their params. The form shows the first and says which it is.
 */
export function treeRows(tree, resolved = null, scheduled = null) {
  const out = [];
  walkRows(tree, resolved, scheduled || [], [], 0, out);
  return out;
}

/**
 * Which schedule entries belong to each child, in order.
 *
 * A module child is one entry. A loop child is `count` of them, and `count`
 * is on the iteration itself (`Step.detail.count`), so this needs no second
 * copy of how the service names or numbers a node.
 */
function zip(children, entries) {
  const list = entries || [];
  let at = 0;
  return children.map((child) => {
    if (child.kind !== 'loop') {
      const one = list[at];
      at += 1;
      return one ? [one] : [];
    }
    const first = list[at];
    const count = first ? (first.count || 1) : 0;
    const slice = list.slice(at, at + count);
    at += count;
    return slice;
  });
}

function walkRows(node, resolved, entries, path, depth, out) {
  if (!node) return;
  const iterations = entries || [];
  const first = iterations[0] || null;
  const row = {
    path, depth, node, resolved: resolved || null, kind: node.kind,
    iterations, first,
    /** The service's own defaults for this node, where it filled any in. */
    detail: (first && first.detail) || null,
    needs_operator: iterations.some((it) => it && it.needs_operator),
  };
  if (node.kind === 'loop') {
    const spec = LOOPS[node.loop] || null;
    row.loop = node.loop;
    row.spec = spec;
    row.values = loopValues(node, resolved);
    row.summary = loopSummary(node, resolved);
    row.owns = spec ? spec.owns : null;
    row.measure_s = iterations.reduce((sum, it) => sum + (it.measure_s || 0), 0);
    row.shots = iterations.reduce((sum, it) => sum + (it.shots || 0), 0);
    // How many times this loop is *entered*, which for a loop inside another
    // is not its own length: the canonical tree's illumination loop has five
    // levels and runs forty-five times.
    row.runs = iterations.length;
    out.push(row);
    const children = node.children || [];
    const rc = (resolved && resolved.children) || [];
    // Every iteration has the same children, so the first is the one whose
    // entries the child rows are drawn from — and a child's own `iterations`
    // gathers them from all of them, which is how a `bace` under two loops
    // reports 45 runs rather than one.
    const perIteration = iterations.map((it) => zip(children, it.children));
    children.forEach((child, i) => walkRows(
      child, rc[i] || null,
      perIteration.flatMap((z) => z[i] || []),
      [...path, i], depth + 1, out));
    return;
  }
  row.module = node.module;
  row.overrides = Object.entries(node.params || {});
  row.estimate_s = first ? first.estimate_s : null;
  row.shots = iterations.reduce((sum, it) => sum + (it.shots || 0), 0);
  row.runs = iterations.length;
  row.relay = first ? first.relay : null;
  row.relay_transition = iterations.some((it) => it.relay_transition);
  row.voc = first ? first.voc : null;
  row.centre_on_voc = Boolean(first && first.centre_on_voc);
  row.params = first ? first.params : null;
  out.push(row);
}

/**
 * The values a loop will run, from the **resolved** tree. `null` until the
 * service has answered: a range is not a count until something has rounded
 * it, and rounding it here is the one thing this file must not do.
 */
export function loopValues(node, resolved) {
  const spec = LOOPS[node.loop];
  if (!spec) return null;
  if (spec.count) {
    const count = node.count ?? (resolved && resolved.count);
    return count === undefined || count === null ? null : Array.from({ length: count }, (_, i) => i + 1);
  }
  // A typed list is its own answer — `pipeline._loop_values` returns it
  // unchanged — so the node is authoritative for it, and an answer describing
  // the list before the last edit cannot stand in front of what is on screen.
  // A range is the other way round: only the service expands one, and until
  // it has there is no count to show.
  const list = valueForm(node) === 'list'
    ? node[spec.list]
    : (resolved ? resolved[spec.list] : null);
  return Array.isArray(list) ? list : null;
}

/** `295 → 220 K · 9 · tol 0.2` — the row's one line, R3·3's own wording. */
export function loopSummary(node, resolved) {
  const spec = LOOPS[node.loop];
  if (!spec) return String(node.loop || '');
  const values = loopValues(node, resolved);
  if (spec.count) return values ? `${values.length} times` : 'count not set';
  const form = valueForm(node);
  const dp = spec.decimals;
  const parts = [];
  if (values && values.length) {
    const first = values[0].toFixed(dp);
    const last = values[values.length - 1].toFixed(dp);
    parts.push(values.length === 1 ? `${first} ${spec.unit}` : `${first} → ${last} ${spec.unit}`);
    parts.push(String(values.length));
  } else if (form === 'range') {
    const [a, b, s] = spec.range.map((k) => node[k]);
    parts.push(`${fmtNum(a, dp)} → ${fmtNum(b, dp)} ${spec.unit} step ${fmtNum(s, dp)}`);
  } else {
    parts.push('no values');
  }
  if (node.loop === 'temperature' && node.tolerance_k !== undefined) parts.push(`tol ${node.tolerance_k}`);
  if (node.loop === 'illumination' && node.led_low_v !== undefined) parts.push(`low ${fmtNum(node.led_low_v, 3)} V`);
  return parts.join(' · ');
}

function fmtNum(v, dp) {
  return v === undefined || v === null || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(dp);
}

/** `2 loops · 2 modules` — a count of what is in the tree, not of what it runs. */
export function structureSummary(tree) {
  let loops = 0;
  let modules = 0;
  for (const row of treeRows(tree)) {
    if (row.kind === 'loop') loops += 1; else modules += 1;
  }
  return { loops, modules };
}

// -- what it will do, in order ----------------------------------------------

/**
 * The flat schedule as the nesting it came from, with what the cryostat will
 * be at stamped on every module.
 *
 * Nested rather than flat because §5 is the whole argument of this screen:
 * the canonical tree is 198 steps and `step 3 of 198` says nothing an
 * operator can act on. Three scales — which temperature, which level, how far
 * into the scan — is what the monitor draws during the run, and it is what
 * the schedule has to say before it.
 *
 * `temperature` is the executor's rule, not the resolver's: a temperature
 * *loop* writes its setpoint into its own subtree and each iteration writes
 * it again, and a temperature *module* writes into the root and every open
 * scope — so it binds the rest of the run, including the next iteration of a
 * loop enclosing it (`executor.ExecCtx`, `docs/service-contract.md` §7). The
 * resolver cannot say this: `Step.detail.temperature_k` is the enclosing
 * loop's setpoint and `null` under a module, so a schedule drawn from it
 * alone shows a nine-hour sweep with no temperature on any node.
 */
export function scheduleTree(steps) {
  const root = { node_path: '', kind: 'root', children: [], depth: -1 };
  const stack = [root];
  /** Root, then every open loop: exactly the holders the executor writes to. */
  const scopes = [{ t: null }];

  for (const step of steps || []) {
    if (step.kind === 'loop-enter') {
      const detail = step.detail || {};
      const scope = { t: scopes[scopes.length - 1].t };
      if (step.loop === 'temperature') {
        scope.t = { k: detail.setpoint_k, how: 'loop', node_path: step.node_path };
      }
      scopes.push(scope);
      const group = {
        kind: 'loop', loop: step.loop, node_path: step.node_path, value: step.value,
        index: detail.index, count: detail.count, label: detail.label || '',
        detail, estimate_s: step.estimate_s, needs_operator: Boolean(step.needs_operator),
        depth: stack.length - 1, children: [],
        settle_s: step.loop === 'temperature' ? detail.settle_s ?? null : null,
        measure_s: 0, shots: 0, modules: 0, temperature: scope.t,
      };
      stack[stack.length - 1].children.push(group);
      stack.push(group);
      // An illumination level's own settle is **measuring** to the cost model
      // (`pipeline.estimate` folds it in through `measured`), so it is
      // measuring here too — 2 s a level is 90 s over the canonical tree, and
      // a bar drawn 90 s shorter than the total printed beside it is the one
      // discrepancy on this screen nobody could explain.
      if (step.loop === 'illumination') {
        for (const open of stack) {
          if (open.kind === 'loop') open.measure_s += step.estimate_s || 0;
        }
      }
      continue;
    }
    if (step.kind === 'loop-exit') {
      if (stack.length > 1) stack.pop();
      if (scopes.length > 1) scopes.pop();
      continue;
    }
    const detail = step.detail || {};
    const leaf = {
      kind: 'module', module: step.module, node_path: step.node_path,
      detail, params: step.params || {}, estimate_s: step.estimate_s,
      needs_operator: Boolean(step.needs_operator), relay: step.relay,
      relay_transition: Boolean(detail.relay_transition), relay_from: detail.relay_from || null,
      led_v: detail.led_v ?? null, shots: detail.shots || 0,
      voc: detail.voc || null, centre_on_voc: Boolean(detail.centre_on_voc),
      depth: stack.length - 1, temperature: scopes[scopes.length - 1].t,
    };
    stack[stack.length - 1].children.push(leaf);
    // Every loop this module is inside pays for it, which is what makes a
    // temperature's line read `5 levels · 8 min` without anything adding up
    // module estimates twice.
    for (const open of stack) {
      if (open.kind !== 'loop') continue;
      open.measure_s += step.estimate_s || 0;
      open.shots += leaf.shots;
      open.modules += 1;
    }
    if (step.module === 'temperature') {
      // The cryostat is now here and stays here until another temperature
      // node moves it: root and every open scope, exactly as the executor
      // writes it, so the next iteration of an enclosing loop keeps it too.
      const setpoint = (step.params && step.params.setpoint_k && step.params.setpoint_k.value);
      const bound = { k: setpoint ?? null, how: 'module', node_path: step.node_path };
      for (const scope of scopes) scope.t = bound;
    }
  }
  return root.children;
}

/** Every module leaf of a `scheduleTree`, in order. */
export function scheduleLeaves(nodes, out = []) {
  for (const node of nodes) {
    if (node.kind === 'module') out.push(node);
    else scheduleLeaves(node.children, out);
  }
  return out;
}

/**
 * The temperature blocks the time bar draws: one per temperature iteration —
 * the settle it is expected to take, then the measuring under it.
 *
 * A settle the journal has never measured is `null`, and stays `null`: the
 * cost model refuses to invent one (`docs/service-contract.md` §7, "Never
 * invent a settle time") and so does this. A run with no temperature loop in
 * it is one block with no settle, which is what such a run is.
 */
export function timeline(nodes) {
  const out = [];
  /** Measuring that is not under any temperature loop, until one appears. */
  let carry = null;
  /**
   * A stretch of measuring that is not inside a temperature loop. `settles`
   * is false and that is the whole point: its `settle_s` is null because
   * there is no cryostat settle here at all, not because nobody has measured
   * one, and the bar must not hatch it as an unknown.
   */
  const blank = () => ({
    node_path: '', setpoint_k: null, settles: false, settle_s: null, hold_s: null,
    measure_s: 0, modules: 0, shots: 0, needs_operator: false,
  });
  const flush = () => {
    if (carry && (carry.measure_s > 0 || carry.modules)) out.push(carry);
    carry = null;
  };

  const visit = (list) => {
    for (const node of list) {
      if (node.kind === 'module') {
        carry = carry || blank();
        carry.measure_s += node.estimate_s || 0;
        carry.modules += 1;
        carry.shots += node.shots || 0;
        carry.needs_operator = carry.needs_operator || node.needs_operator;
        // Whatever the cryostat is at while this runs — a `temperature`
        // module's binding, where there is one.
        if (node.temperature && node.temperature.k !== null && node.temperature.k !== undefined) {
          carry.setpoint_k = node.temperature.k;
        }
        continue;
      }
      if (node.loop === 'temperature') {
        // A temperature iteration is a block of its own: settle, hold, then
        // everything measured inside it. Whatever was accumulating before it
        // is a block too — it happened, and at a different temperature.
        flush();
        out.push({
          node_path: node.node_path, setpoint_k: node.value, settles: true,
          settle_s: node.settle_s, hold_s: node.detail.hold_s ?? null,
          measure_s: node.measure_s, modules: node.modules, shots: node.shots,
          needs_operator: node.needs_operator,
        });
        continue;
      }
      // Any other loop outside a temperature: its own settle is measuring
      // (the cost model counts an illumination level's), then its children.
      if (node.loop === 'illumination') {
        carry = carry || blank();
        carry.measure_s += node.estimate_s || 0;
      }
      visit(node.children);
    }
  };
  visit(nodes);
  flush();
  return out;
}

/**
 * The cells the run fills: temperature × LED level, with what lands in each.
 *
 * R3·3 draws this grid with a V_oc in every cell "from the last run in grey".
 * The V_oc of a run that has not happened is not knowable and is not drawn
 * here — what is drawn is which modules will write into the cell, which comes
 * straight off the schedule. `ui-rules` §9's empty state, `a pipeline with
 * zero nodes`, is the empty return.
 */
export function gridModel(nodes) {
  const leaves = scheduleLeaves(nodes);
  const temperatures = [];
  const levels = [];
  const cells = new Map();
  for (const leaf of leaves) {
    const t = leaf.temperature ? leaf.temperature.k : null;
    const led = leaf.led_v;
    if (t === null && led === null) continue;
    if (t !== null && !temperatures.includes(t)) temperatures.push(t);
    if (led !== null && led !== undefined && !levels.includes(led)) levels.push(led);
    const key = `${t}|${led}`;
    if (!cells.has(key)) cells.set(key, []);
    cells.get(key).push(leaf);
  }
  // One cell is not a grid. A run at one temperature and one level fills a
  // single box, and drawing it as a table says nothing the node list did not
  // — `ui-rules` §9's empty state, which is a state to draw rather than an
  // empty table to draw it in.
  if (temperatures.length * levels.length < 2) return null;
  return {
    temperatures, levels, cells,
    cell: (t, led) => cells.get(`${t}|${led}`) || [],
    count: [...cells.values()].reduce((n, list) => n + list.length, 0),
  };
}

// -- the checks and the cost ------------------------------------------------

/**
 * `16 checks · 15 ok · 1 warn · show` — R3·3's collapsed list, and §11's
 * already-answered question: *"Should `ok` preflight entries collapse to a
 * count? Yes."*
 *
 * `blocking` is `invalid` **and** `crit`, because `pipeline.validate` is
 * `valid = not any(level in ("invalid", "crit"))` and a Start that blocked on
 * only one of them would offer a click the service answers 422.
 */
export const LEVELS = ['crit', 'invalid', 'warn', 'info', 'ok'];

export function checkSummary(checks) {
  const list = checks || [];
  const counts = Object.fromEntries(LEVELS.map((l) => [l, 0]));
  for (const check of list) counts[check.level] = (counts[check.level] || 0) + 1;
  const blocking = list.filter((c) => c.level === 'crit' || c.level === 'invalid');
  const notable = list.filter((c) => c.level !== 'ok' && c.level !== 'info');
  return {
    total: list.length,
    counts,
    blocking,
    notable,
    // Ordered loudest first, which is the order §3 puts them in.
    ordered: LEVELS.flatMap((level) => list.filter((c) => c.level === level)),
    worst: LEVELS.find((level) => counts[level] > 0) || null,
  };
}

/**
 * The cost, with the one word that keeps it honest.
 *
 * `lower_bound` is set whenever any temperature's settle is unmeasured, and
 * on the first run of a device that is every one of them — the artboard's own
 * table shows `—` for the first temperature. So the total is rendered
 * *"at least 1 h 24"*, never *"1 h 24"*, and `finish_at` is dropped with it:
 * a clock time is a promise in a way a duration is not.
 */
export function costModel(cost) {
  if (!cost) return null;
  const lower = Boolean(cost.lower_bound);
  return {
    total_s: cost.total_s ?? null,
    measuring_s: cost.measuring_s ?? null,
    waiting_s: cost.waiting_s ?? null,
    lower_bound: lower,
    /** The word that goes in front of every duration on this screen. */
    prefix: lower ? 'at least' : '',
    finish_at: lower ? null : (cost.finish_at ?? null),
    per_temperature: cost.per_temperature || [],
    t_shot_s: cost.t_shot_s ?? null,
    t_shot_source: cost.t_shot_source || null,
    unestimated: cost.unestimated_modules || 0,
    /** Which temperatures have no measured settle behind them. */
    unmeasured: (cost.per_temperature || []).filter((t) => t.settle_s === null || t.settle_s === undefined),
  };
}

/**
 * Whether two trees are the same pipeline. Used to decide whether the
 * validate in hand still describes what is on screen — a cost and a check
 * list for a tree the operator has since edited is worse than none.
 */
export function sameTree(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}
