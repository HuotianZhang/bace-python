// Recipes and saved benches share a value-and-source `bench` block
// (`docs/service-contract.md` §§4, 7). One pure `drift` comparison serves both. Trees
// store overrides; other parameters come from the bench at Start, so bench edits can
// change execution without a tree diff. Reopening compares saved values with the
// catalogue and offers restoration. Records without a tree cover every recorded
// module.

/**
 * The helper lists differing bench values in catalogue order. Empty can mean
 * agreement or absent saved data; `recorded(recipe)` distinguishes them. Rows retain
 * `saved`, `now` and `unit` for rendering.
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

/** The predicate reports whether the recipe includes saved bench values. */
export function recorded(recipe) {
  return Boolean(recipe && recipe.bench && typeof recipe.bench === 'object');
}

/**
 * Comparison requires at least one recorded module in the catalogue. Since `drift`
 * skips absent modules, an empty catalogue would otherwise claim agreement before
 * loading or after reset. Unknown bench state cannot imply a match.
 */
export function comparable(recipe, byName) {
  if (!recorded(recipe) || !byName) return false;
  return Object.keys(recipe.bench).some((module) => Boolean(byName[module]));
}

/**
 * The helper builds per-module `PUT` bodies restoring changed bench values. Node
 * overrides remain in the tree and need no restoration.
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
 * The helper finds parameters every module node obtains outside the bench: node
 * overrides, loop bindings or derivations. If one node still reads the bench value,
 * comparison retains that parameter.
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

/**
 * The saved-bench row identifies the open file and agreement with the bench.
 * Infrequent name, list and drift controls sit behind its menu (`docs/ui-rules.md`
 * §14). States distinguish no files, files with none open, an open match and an open
 * mismatch. The pure model permits tests without a browser.
 */
export function savedBenchModel(benches, loadedName, byName) {
  const files = benches || [];
  const file = loadedName ? files.find((f) => f.name === loadedName) || null : null;
  // No catalogue means no comparison. The row retains `loaded` and shows the count so
  // it can compare when modules arrive.
  if (!file || !comparable(file, byName)) {
    return { state: files.length ? 'idle' : 'none', name: null, count: files.length, moved: [] };
  }
  const moved = drift(file, byName);
  return {
    state: moved.length ? 'drift' : 'match',
    name: file.name,
    count: files.length,
    moved,
  };
}

/**
 * The helper matches the service's `_RECIPE_STEM` reduction. Overwrite confirmation
 * compares normalized stems; a raw name containing spaces cannot match its filename.
 * Both tabs share the rule. `tests/test_service_api.py` and
 * `ui/tests/recipe.test.mjs` check matching examples.
 */
export function fileStem(name) {
  return String(name === null || name === undefined ? '' : name)
    .trim().replace(/[^A-Za-z0-9_.-]+/g, '_').replace(/^_+|_+$/g, '');
}

/**
 * Drift text formats voltage to three decimals like the cards and lists array
 * elements.
 */
export function showValue(v, unit) {
  if (typeof v === 'number') return unit === 'V' ? v.toFixed(3) : Number.isInteger(v) ? String(v) : v.toFixed(3);
  if (Array.isArray(v)) return v.map((x) => showValue(x, unit)).join(', ');
  return v === null || v === undefined ? '—' : String(v);
}

function same(a, b) {
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => same(x, b[i]));
  if (typeof a === 'number' && typeof b === 'number') return Math.abs(a - b) <= 1e-12 * Math.max(1, Math.abs(a), Math.abs(b));
  return a === b || (a === null && b === undefined) || (a === undefined && b === null);
}
