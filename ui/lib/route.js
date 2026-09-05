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
