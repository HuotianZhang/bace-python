// A saved bench against the bench it is reopened on — `lib/recipe.js`.
//
// Two things are saved to a file and compared with the bench when they come
// back: a **recipe** (`POST /pipelines/save`, the pipeline tab) and the
// **bench settings** themselves (`POST /bench/save`, the bench tab). Both
// records carry the same `bench: {module: {param: {value, source}}}` block,
// which is why one `drift` reads both — a bench preset is a recipe record
// with no tree, and a record with no tree covers every module in it.
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
 * Whether there is a bench to compare the record with: at least one module
 * it records is in the catalogue. `drift` skips a module the catalogue does
 * not hold, so against no catalogue at all — before `GET /modules` answers,
 * or just after `store.reset()` on a service restart — every record reads as
 * moved-nothing, which is "✓ matches the bench" said about a bench nobody
 * has seen. That is the one moment the row most needs to be right.
 */
export function comparable(recipe, byName) {
  if (!recorded(recipe) || !byName) return false;
  return Object.keys(recipe.bench).some((module) => Boolean(byName[module]));
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

/**
 * The `MODULES` row of the bench tab, as one object — `views/bench.js`.
 *
 * That row is the only thing about saved benches that is permanently on
 * screen, and it answers exactly one question: **which saved bench this is,
 * and whether the bench has moved since**. Everything else — the name field,
 * the file list, the drift table — is behind the `⋯` beside it, because a
 * control reached for less than once a session does not sit on the screen
 * (`ui-rules` §14's fourth question).
 *
 * Four states, and the row says which:
 *
 *   `none`   nothing saved yet — the row says so, or the `⋯` beside it is a
 *            glyph with nothing to explain it
 *   `idle`   files exist, none open against the bench: the count
 *   `match`  one open, and the bench is what it holds
 *   `drift`  one open, and `moved` lists every value that differs
 *
 * Pure, so `recipe.test.mjs` holds the four down without a browser.
 */
export function savedBenchModel(benches, loadedName, byName) {
  const files = benches || [];
  const file = loadedName ? files.find((f) => f.name === loadedName) || null : null;
  // No catalogue is no comparison, not a match: the row falls back to the
  // count until the modules arrive, and `loaded` is kept so it then answers.
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
 * The file stem a name is saved under: `app.py: _RECIPE_STEM`, in JS.
 *
 * Here because the console has to compare a typed name with the names the
 * service already has, and a raw name never matches a stemmed one — so the
 * "this will write over ⟨name⟩" arming silently did not fire for any name
 * with a space in it, which is most of them. Both tabs' Save use this.
 *
 * The two halves are pinned to the same table:
 * `tests/test_service_api.py: test_a_name_becomes_the_same_file_stem_on_both_sides`
 * and `recipe.test.mjs: a name becomes the file stem the service writes`.
 */
export function fileStem(name) {
  return String(name === null || name === undefined ? '' : name)
    .trim().replace(/[^A-Za-z0-9_.-]+/g, '_').replace(/^_+|_+$/g, '');
}

/**
 * One drifted value, for the note that lists them. Volts to three places
 * because that is what the cards show; an array as its elements.
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
