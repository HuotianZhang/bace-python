// Shared hash parsing keeps pipeline links and router queries consistent.
// `#/bench?module=bace` resolves to bench with a module query.

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
 * View swaps catch teardown errors so they cannot block the next tab. The hash
 * already names the destination; leaving the old view would make navigation appear
 * frozen. Incomplete cleanup is a leak but must not prevent opening the requested
 * tab.
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
