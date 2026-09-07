// The hash as the console reads it — `#/bench?module=bace` is the route
// `bench` with the query `{module: 'bace'}`. One place, so a link written on
// the pipeline tab and the router that answers it cannot disagree about the
// shape.

/** `{route, query}` from a location hash. Unknown or empty hash → route ''. */
export function parseHash(hash) {
  const raw = String(hash || '').replace(/^#\/?/, '');
  const [route, search = ''] = raw.split('?');
  const query = {};
  for (const pair of search.split('&')) {
    if (!pair) continue;
    const [k, v = ''] = pair.split('=');
    try { query[decodeURIComponent(k)] = decodeURIComponent(v); } catch { query[k] = v; }
  }
  return { route, query };
}

/** The hash for a route and a query: `benchHash('bace')` → `#/bench?module=bace`. */
export function benchHash(module) {
  return module ? `#/bench?module=${encodeURIComponent(module)}` : '#/bench';
}

/**
 * Take the mounted view down and put the next one up.
 *
 * The teardown is wrapped because a `dispose` that throws must not stop the
 * swap, and one did: `views/bench.js` cleared a timer under the name
 * `views/pipeline.js` gives its own, which in a module is a `ReferenceError`.
 * It threw out of the router *after* the hash had changed, so every click from
 * the bench to another tab moved the address bar and left the bench on the
 * screen — a console that looked frozen until the page was reloaded, and
 * looked fine from every other tab.
 *
 * The view coming down is gone as far as the operator is concerned; whatever
 * it failed to release is a leak, and a leak is a smaller thing than a tab
 * that does not open.
 */
export function swapView(mounted, mount, { onError = viewFailed } = {}) {
  try {
    if (mounted && mounted.dispose) mounted.dispose();
  } catch (error) {
    onError(error);
  }
  return mount();
}

function viewFailed(error) {
  console.error('a view did not come down cleanly:', error);
}
