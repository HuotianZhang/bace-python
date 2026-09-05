// The undo stack — `lib/undo.js`.
//
// The pipeline tree is the one thing in the console that is composed by hand
// and the one thing the service does not hold a layer underneath, so it is the
// only thing that can be lost. What is asserted here is the behaviour that
// makes it *not* lost: the label travels with the step, a branch drops the
// future, and the bound is a bound.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createHistory } from '../lib/undo.js';

test('a step back returns the state that was left, under the name of what left it', () => {
  const h = createHistory();
  assert.equal(h.canUndo, false, 'nothing has happened yet');
  assert.equal(h.undo('anything'), null);

  h.push({ tree: 'A' }, 'add the temperature loop');
  h.push({ tree: 'B' }, 'remove bace');

  // The button says what it will take back, not that it can take something.
  assert.equal(h.undoLabel, 'remove bace');
  const back = h.undo({ tree: 'C' });
  assert.deepEqual(back.state, { tree: 'B' });
  assert.equal(back.label, 'remove bace');
  assert.equal(h.undoLabel, 'add the temperature loop');
});

test('what was undone can be put back, and the labels stay with their steps', () => {
  const h = createHistory();
  h.push({ tree: 'A' }, 'add jv');
  const back = h.undo({ tree: 'B' });
  assert.deepEqual(back.state, { tree: 'A' });
  assert.equal(h.canUndo, false);
  assert.equal(h.canRedo, true);
  assert.equal(h.redoLabel, 'add jv');

  const forward = h.redo({ tree: 'A' });
  assert.deepEqual(forward.state, { tree: 'B' }, 'the state undone out of');
  assert.equal(h.canRedo, false);
  assert.equal(h.canUndo, true, 'and it is a step back again');
});

test('a new action drops the branch that was not taken', () => {
  // The ordinary rule, and the one that keeps redo honest: after undoing a
  // remove and then typing something else, "redo" cannot mean the remove.
  const h = createHistory();
  h.push({ tree: 'A' }, 'add bace');
  h.undo({ tree: 'B' });
  assert.equal(h.canRedo, true);
  h.push({ tree: 'A' }, 'set n_loops');
  assert.equal(h.canRedo, false);
  assert.equal(h.undoLabel, 'set n_loops');
});

test('the stack is bounded, and it is the oldest step that goes', () => {
  const h = createHistory({ limit: 3 });
  for (const n of ['one', 'two', 'three', 'four']) h.push({ tree: n }, n);
  assert.equal(h.depth, 3);
  assert.equal(h.undo(null).state.tree, 'four');
  assert.equal(h.undo(null).state.tree, 'three');
  assert.equal(h.undo(null).state.tree, 'two');
  assert.equal(h.undo(null), null, 'and `one` fell off the bottom, not the top');
});

test('clear leaves nothing in either direction', () => {
  const h = createHistory();
  h.push({ tree: 'A' }, 'a');
  h.undo({ tree: 'B' });
  h.clear();
  assert.equal(h.canUndo, false);
  assert.equal(h.canRedo, false);
  assert.equal(h.undoLabel, '');
});
