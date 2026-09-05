// The hash, both ways — `lib/route.js`.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseHash, benchHash } from '../lib/route.js';

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
