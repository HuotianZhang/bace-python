// A saved recipe against the bench it is reopened on — `lib/recipe.js`.
//
// A recipe's tree carries only what its nodes override; every other value a
// node runs with is the module's bench value at start time. So a recipe is
// an instance, and the bench is its main component: change the bench and
// the recipe runs differently with no diff in its file. Since the save
// records the bench (`POST /pipelines/save` writes `bench: {module: {param:
// {value, source}}}` for every module in the tree), reopening one can say
// exactly where the bench has moved since — and offer to put it back.
//
// `drift` is pure: the recipe's `bench` against the catalogue's entries.

/**
 * Every parameter whose bench value differs from what the recipe was saved
 * with: `[{module, name, saved, now, unit}]`, in catalogue order. Empty when
 * nothing moved, or when the recipe predates the record (`bench` absent —
 * `recorded(recipe)` says which).
 */
export function drift(recipe, byName, { tree = null, rows = null } = {}) {
  const bench = (recipe && recipe.bench) || null;
  if (!bench) return [];
  const covered = coveredBy(tree || (recipe && recipe.tree) || null, rows);
  const out = [];
  for (const [module, saved] of Object.entries(bench)) {
    const entry = byName && byName[module];
    if (!entry) continue;
    for (const p of entry.params || []) {
      const was = saved[p.name];
      if (!was || !('value' in was)) continue;
      if (same(was.value, p.value)) continue;
      // A bench value no node of this module reads is not drift: every
      // node overrides it in the tree, or a loop above binds it.
      if (covered[module] && covered[module].has(p.name)) continue;
      out.push({ module, name: p.name, saved: was.value, now: p.value, unit: p.unit || '' });
    }
  }
  return out;
}

/** Whether the recipe records the bench at all (saved after that landed). */
export function recorded(recipe) {
  return Boolean(recipe && recipe.bench && typeof recipe.bench === 'object');
}

/**
 * The `PUT` bodies that restore the recipe's bench: `{module: {param:
 * value}}`, one per module that drifted. A node's own override is not in
 * here — it is in the tree, and it never left.
 */
export function restore(items) {
  const out = {};
  for (const it of items) {
    if (!out[it.module]) out[it.module] = {};
    out[it.module][it.name] = it.saved;
  }
  return out;
}

/**
 * Per module, the parameters *every* node of it in the tree gets from
 * somewhere other than the bench: its own `params` override, or — when the
 * resolved schedule rows are given — a loop's binding (`inherited`) or a
 * run's derivation (`derived`). A parameter one node overrides and another
 * does not is still the bench's for the other, so it stays.
 */
export function coveredBy(tree, rows) {
  const per = new Map();
  const bound = new Map();
  for (const row of rows || []) {
    if (row.kind !== 'module' || !row.params) continue;
    const set = new Set(Object.entries(row.params)
      .filter(([, pv]) => pv && (pv.source === 'inherited' || pv.source === 'derived'))
      .map(([name]) => name));
    bound.set(row.path.join(','), set);
  }
  const visit = (node, path) => {
    if (!node) return;
    if (node.kind === 'loop') { (node.children || []).forEach((c, i) => visit(c, path.concat(i))); return; }
    if (node.kind !== 'module') return;
    const own = new Set(Object.keys(node.params || {}));
    for (const name of bound.get(path.join(',')) || []) own.add(name);
    if (!per.has(node.module)) per.set(node.module, own);
    else per.set(node.module, new Set([...per.get(node.module)].filter((n) => own.has(n))));
  };
  visit(tree, []);
  return Object.fromEntries(per);
}

function same(a, b) {
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => same(x, b[i]));
  if (typeof a === 'number' && typeof b === 'number') return Math.abs(a - b) <= 1e-12 * Math.max(1, Math.abs(a), Math.abs(b));
  return a === b || (a === null && b === undefined) || (a === undefined && b === null);
}
