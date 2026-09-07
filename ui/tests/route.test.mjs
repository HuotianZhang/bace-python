// The hash, both ways — `lib/route.js`.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseHash, benchHash, swapView } from '../lib/route.js';

test('a route with a query parses to both halves, and a bare one to a route', () => {
  assert.deepEqual(parseHash('#/bench?module=bace'), { route: 'bench', query: { module: 'bace' } });
  assert.deepEqual(parseHash('#/pipeline'), { route: 'pipeline', query: {} });
  assert.deepEqual(parseHash('#bench'), { route: 'bench', query: {} });
  assert.deepEqual(parseHash(''), { route: '', query: {} });
  assert.deepEqual(parseHash('#/bench?a=1&b=two%20words'), { route: 'bench', query: { a: '1', b: 'two words' } });
});

test('the link a node form writes is the hash the bench answers', () => {
  assert.equal(benchHash('bace'), '#/bench?module=bace');
  assert.deepEqual(parseHash(benchHash('jv_bace')).query, { module: 'jv_bace' });
  assert.equal(benchHash(''), '#/bench');
});

test('a view whose dispose throws still comes down, and the next one goes up', () => {
  // The bench cleared a timer it had never declared, so `dispose` threw a
  // `ReferenceError` out of the router — after the hash had already changed.
  // Every click from the bench to another tab moved the address bar and left
  // the bench on screen until the page was reloaded.
  const seen = [];
  const broken = { dispose() { throw new ReferenceError('armedTimer is not defined'); } };
  const next = { name: 'pipeline' };

  const mounted = swapView(broken, () => { seen.push('mounted'); return next; },
    { onError: (error) => seen.push(`caught ${error.name}`) });

  assert.deepEqual(seen, ['caught ReferenceError', 'mounted']);
  assert.equal(mounted, next);
});

test('a view that comes down cleanly comes down before the next goes up', () => {
  const seen = [];
  const mounted = swapView({ dispose: () => seen.push('disposed') },
    () => { seen.push('mounted'); return 'view'; },
    { onError: () => assert.fail('nothing threw') });

  assert.deepEqual(seen, ['disposed', 'mounted']);
  assert.equal(mounted, 'view');
});

test('nothing mounted, and a view with no dispose, are both just a mount', () => {
  assert.equal(swapView(null, () => 'a'), 'a');
  assert.equal(swapView({}, () => 'b'), 'b');
});
