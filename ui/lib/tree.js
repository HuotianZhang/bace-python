// The tree model follows the pipeline schema (`docs/service-contract.md` §7). It
// retains typed ranges and takes resolved counts, levels and cost from validation to
// avoid conflicting rounding. `treeRows` aligns typed and resolved nodes.
// `scheduleTree` propagates temperature-module bindings through the remaining run,
// including later loop iterations, because schedule detail alone reports only
// enclosing-loop setpoints. `costModel` labels unknown settling time as a lower bound
// rather than promising a finish. Pure models support fixture tests;
// `ui/views/pipeline.js` renders them.

/**
 * Loop specifications supply labels, fields and summaries. `/modules` describes
 * modules rather than loops, so these follow the pipeline schema
 * (`docs/service-contract.md` §7). Fields include explanations because names alone
 * are insufficient (`docs/ui-rules.md` §1).
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
 * Park is excluded from node choices because every run parks on exit
 * (`docs/service-contract.md` §7). Manual park belongs in the strip. Other utilities
 * remain available; a standalone light-only tree is the service validator's
 * responsibility.
 */
export const NOT_A_NODE = new Set(['park']);

// -- constructing -----------------------------------------------------------

/**
 * The constructor returns a loop with service-schema defaults
 * (`docs/service-contract.md` §7).
 */
export function newLoop(kind) {
  const base = { kind: 'loop', loop: kind, children: [] };
  if (kind === 'temperature') return { ...base, values_k: [290], tolerance_k: 0.2, hold_s: 60, timeout_s: 1800 };
  if (kind === 'illumination') return { ...base, levels_v: [1.02], led_low_v: 0.4, led_settle_s: 2 };
  return { ...base, count: 2 };
}

/**
 * Module nodes start with empty `params` so they inherit bench values. Inserting an
 * override at creation would pin a transient bench setting into the tree
 * (`docs/service-contract.md` §7).
 */
export function newModule(name) {
  return { kind: 'module', module: name, params: {} };
}

/**
 * The row marks node overrides with the same reset glyph as the form and lists them
 * on hover. No overrides gives `null`; an absent mark avoids a growing sentence in
 * the compact row.
 */
export function overrideMark(row) {
  const overrides = (row && row.overrides) || [];
  if (!overrides.length) return null;
  return {
    text: '↺',
    title: 'differs from the bench: ' + overrides.map(([k, v]) => `${k} ${v}`).join(' · '),
    count: overrides.length,
  };
}

// Paths contain child indices: `[]` identifies root and `[0, 1]` its first child's
// second child. Edits return independent trees so a refused edit can leave the
// accepted tree intact.

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

/** Insertion adds `node` below `path` at `index`, defaulting to the end. */
export function insertAt(tree, path, node, index = null) {
  return updateAt(tree, path, (parent) => {
    const children = (parent.children || []).slice();
    children.splice(index === null ? children.length : index, 0, node);
    return { ...parent, children };
  });
}

/**
 * Removing root returns `null`, the empty pipeline state that must render without an
 * exception (`docs/ui-rules.md` §9).
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

/** The helper moves a node by `delta` sibling positions. */
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
 * The helper remaps selection after a sibling move. Child-index paths change for the
 * moved node and siblings it passes. Keeping the old index would silently select
 * another node, especially with two `bace` siblings, and send the next override to
 * the wrong one.
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
 * The helper changes one node override. `null` removes the key so it falls back to
 * resolved bench parameters rather than storing null.
 */
export function setParam(tree, path, name, value) {
  return updateAt(tree, path, (node) => {
    const params = { ...(node.params || {}) };
    if (value === null) delete params[name];
    else params[name] = value;
    return { ...node, params };
  });
}

/** Node reset removes all overrides and restores bench inheritance. */
export function clearParams(tree, path) {
  return updateAt(tree, path, (node) => ({ ...node, params: {} }));
}

/** Loop-field reset removes its key so the service default applies. */
export function setField(tree, path, name, value) {
  return updateAt(tree, path, (node) => {
    const next = { ...node };
    if (value === null || value === '') delete next[name];
    else next[name] = value;
    return next;
  });
}

/**
 * The helper converts list and range forms without inventing setpoints. A range
 * requires service-expanded values before conversion; otherwise the tree stays
 * unchanged and the caller explains why. Replacing an unresolved range with an empty
 * list or zero range would silently discard the requested experiment.
 */
export function setValueForm(tree, path, form, resolved) {
  const node = nodeAt(tree, path);
  const spec = node && LOOPS[node.loop];
  if (!spec || !spec.list) return tree;
  if (!canSwitchForm(node, resolved, form).ok) return tree;
  const values = loopValues(node, resolved);
  return updateAt(tree, path, (n) => {
    const next = { ...n };
    for (const key of [spec.list, ...spec.range]) delete next[key];
    if (form === 'list') {
      next[spec.list] = values.slice();
      return next;
    }
    next[spec.range[0]] = values[0];
    next[spec.range[1]] = values[values.length - 1];
    // Two values determine spacing. A single value always means one iteration, so its
    // step is a placeholder rather than a measured interval.
    next[spec.range[2]] = values.length > 1
      ? Math.abs(Number((values[1] - values[0]).toFixed(9)))
      : (spec.decimals === 3 ? 0.005 : 5);
    return next;
  });
}

/**
 * The helper checks whether a form conversion preserves execution. An unresolved
 * range has no values to list. An uneven list cannot become a range: a
 * nine-temperature list starting 295, 290, 280 K would expand to sixteen temperatures
 * with a 5 K step. Changing representation must not change the experiment.
 */
export function canSwitchForm(node, resolved, form = 'list') {
  const spec = node && LOOPS[node.loop];
  if (!spec || !spec.list) return { ok: false, why: 'this loop has no value list' };
  const values = loopValues(node, resolved);
  if (!values || !values.length) {
    return {
      ok: false,
      why: 'the range has not been checked yet, so there are no values to list. '
        + 'The service is the only thing that expands a range — try again in a moment.',
    };
  }
  if (form === 'range' && !evenlySpaced(values)) {
    const rule = Math.abs(values[1] - values[0]);
    const would = Math.round(Math.abs(values[values.length - 1] - values[0]) / rule) + 1;
    return {
      ok: false,
      why: `these ${values.length} values are not evenly spaced, and a range can only say `
        + `one spacing: ${fmtNum(values[0], spec.decimals)} → `
        + `${fmtNum(values[values.length - 1], spec.decimals)} step `
        + `${fmtNum(rule, spec.decimals)} would run ${would}. Keep the list.`,
    };
  }
  return { ok: true, why: '' };
}

/** Spacing comparison uses the service's nine-decimal rounding precision. */
function evenlySpaced(values) {
  if (values.length < 3) return true;
  const first = values[1] - values[0];
  return values.every((v, i) => i === 0 || Math.abs((v - values[i - 1]) - first) < 1e-9);
}

/** Any populated range field selects range representation. */
export function valueForm(node) {
  const spec = LOOPS[node.loop];
  if (!spec || !spec.list) return null;
  return spec.range.some((k) => node[k] !== undefined) ? 'range' : 'list';
}

// -- the rows the structure card draws --------------------------------------

/**
 * The helper aligns typed nodes, resolved nodes and produced schedule entries. Only
 * the typed tree is editable; resolution supplies levels and counts, while schedule
 * entries supply defaults, provenance, pauses, relay boundaries and V_oc sources.
 * Ordered children and step-declared iteration counts align them without
 * reconstructing service path spelling or duplicate-sibling tokens. `iterations`
 * retains every execution: a node inside nine temperatures and five levels has 45
 * entries. The form identifies the first iteration it shows.
 */
export function treeRows(tree, resolved = null, scheduled = null) {
  const out = [];
  walkRows(tree, resolved, scheduled || [], [], 0, out);
  return out;
}

/**
 * Schedule entries map to ordered children. Modules consume one entry; loops consume
 * their declared `Step.detail.count` iterations. Service path spelling needs no local
 * reconstruction.
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
    /** The helper reads defaults filled by the service for this node. */
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
    // Execution count differs from loop length: five illumination levels under nine
    // temperatures produce 45 iterations.
    row.runs = iterations.length;
    out.push(row);
    const children = node.children || [];
    const rc = (resolved && resolved.children) || [];
    // The first iteration supplies child rows; each child's `iterations` gathers
    // executions from all iterations so nested nodes retain their full count.
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
 * Values come from the resolved tree. Ranges remain `null` before service resolution
 * because local rounding could disagree.
 */
export function loopValues(node, resolved) {
  const spec = LOOPS[node.loop];
  if (!spec) return null;
  // Repeats need only `loopCount`, not allocated value arrays. A typed count such as
  // 4294967296 can throw an array-length error before validation; smaller huge arrays
  // can freeze the tab.
  if (spec.count) return null;
  // Typed lists are authoritative without range expansion, so stale resolution cannot
  // override an edited list. Ranges require the service answer before displaying a
  // count.
  const list = valueForm(node) === 'list'
    ? node[spec.list]
    : (resolved ? resolved[spec.list] : null);
  return Array.isArray(list) ? list : null;
}

/** How many times a `repeat` runs, from the tree or from the answer. */
export function loopCount(node, resolved) {
  const count = node.count ?? (resolved && resolved.count);
  return count === undefined || count === null ? null : count;
}

/** The summary shows loop limits, count and tolerance in one row. */
export function loopSummary(node, resolved) {
  const spec = LOOPS[node.loop];
  if (!spec) return String(node.loop || '');
  if (spec.count) {
    const count = loopCount(node, resolved);
    return count === null ? 'count not set' : `${count} times`;
  }
  const values = loopValues(node, resolved);
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
 * The helper nests the flat schedule and propagates temperature to each module.
 * Temperature, LED level and scan progress need separate scales (`docs/ui-rules.md`
 * §5). A temperature loop binds its subtree per iteration; a temperature module binds
 * root and all open scopes, including later iterations (`docs/service-contract.md`
 * §7). Resolver detail alone cannot represent module bindings and would leave those
 * modules without a temperature.
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
      // Illumination settling counts as measuring in service cost. At 2 s per level
      // over 45 levels, omitting it would make the timeline 90 s shorter than its
      // printed total.
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
      // A temperature module updates root and every open scope so its binding
      // persists into the next enclosing-loop iteration, matching execution
      // semantics.
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
 * The timeline has a block per temperature iteration with settling followed by
 * measurement. Unmeasured settling stays `null` rather than invented
 * (`docs/service-contract.md` §7). A run without temperature loops has no settling
 * block.
 */
export function timeline(nodes) {
  const out = [];
  /**
   * `carry` holds measurement outside temperature loops; `current` holds the
   * innermost temperature block. Each module contributes exactly once. Settles and
   * holds contribute once to waiting regardless of nesting, matching service cost.
   */
  let carry = null;
  let current = null;
  const blank = () => ({
    node_path: '', setpoint_k: null, settles: false, settle_s: null, hold_s: null,
    measure_s: 0, modules: 0, shots: 0, needs_operator: false,
  });
  const flush = () => {
    if (carry && (carry.measure_s > 0 || carry.modules)) out.push(carry);
    carry = null;
  };
  const into = () => {
    if (current) return current;
    carry = carry || blank();
    return carry;
  };

  const visit = (list) => {
    for (const node of list) {
      if (node.kind === 'module') {
        // Outside temperature loops, a module's temperature binding starts another
        // block when it changes. Replacing one block's setpoint would mislabel
        // ambient, 250 K and 200 K scans as all at 200 K.
        if (!current) {
          const k = node.temperature && node.temperature.k !== null
            && node.temperature.k !== undefined ? node.temperature.k : null;
          if (carry && carry.setpoint_k !== k) flush();
          into().setpoint_k = k;
        }
        const block = into();
        block.measure_s += node.estimate_s || 0;
        block.modules += 1;
        block.shots += node.shots || 0;
        block.needs_operator = block.needs_operator || node.needs_operator;
        continue;
      }
      if (node.loop === 'temperature') {
        // Each temperature iteration retains its own settle, hold and direct
        // measurement. Measurement outside it retains a separate block. Nested
        // temperature loops are legal: inner blocks retain their holds and outer
        // blocks retain only direct work. Collapsing them can turn a 102 s cost into
        // a 62 s bar by losing 40 s of holds. Total time is exact; nested ordering is
        // approximate because direct outer work following an inner loop remains in
        // the earlier outer block.
        flush();
        const block = {
          node_path: node.node_path, setpoint_k: node.value, settles: true,
          settle_s: node.settle_s, hold_s: node.detail.hold_s ?? null,
          measure_s: 0, modules: 0, shots: 0, needs_operator: node.needs_operator,
        };
        out.push(block);
        const enclosing = current;
        current = block;
        visit(node.children);
        // Back to the enclosing temperature, exactly as the cost model pops
        // `open_temperatures` at a loop exit.
        current = enclosing;
        continue;
      }
      // Any other loop: its own settle is measuring (the cost model counts an
      // illumination level's), then its children.
      if (node.loop === 'illumination') into().measure_s += node.estimate_s || 0;
      visit(node.children);
    }
  };
  visit(nodes);
  flush();
  return out;
}

/**
 * The grid shows scheduled modules at temperature-by-LED coordinates. Future V_oc is
 * unknown and must not be invented. A tree with no nodes returns the empty state
 * (`docs/ui-rules.md` §9).
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
  // One temperature and LED level needs no grid: the node list already describes the
  // single cell. The empty state avoids a redundant table.
  if (temperatures.length * levels.length < 2) return null;
  return {
    temperatures, levels, cells,
    cell: (t, led) => cells.get(`${t}|${led}`) || [],
    count: [...cells.values()].reduce((n, list) => n + list.length, 0),
  };
}

// -- the checks and the cost ------------------------------------------------

/**
 * The model collapses successful checks to a count (`docs/ui-rules.md` §11). Both
 * `invalid` and `crit` block Start, matching service validation; either alone would
 * allow a request rejected with 422 (`docs/service-contract.md` §7).
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
    // Checks follow severity order (`docs/ui-rules.md` §3).
    ordered: LEVELS.flatMap((level) => list.filter((c) => c.level === level)),
    worst: LEVELS.find((level) => counts[level] > 0) || null,
  };
}

/**
 * The cost model labels total duration as at least when any temperature lacks
 * measured settling. It also omits wall-clock finish because a lower bound cannot
 * promise one (`docs/service-contract.md` §7).
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
    /** The helper lists temperatures without measured settling time. */
    unmeasured: (cost.per_temperature || []).filter((t) => t.settle_s === null || t.settle_s === undefined),
  };
}

/**
 * Tree equality determines whether validation still describes the editor. Stale
 * checks and cost cannot authorize Start for a different tree.
 */
export function sameTree(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}
