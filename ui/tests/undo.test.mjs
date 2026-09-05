// The way back from a tree edit — `lib/undo.js`.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { start, push, undo, redo, canUndo, canRedo, depth, keyAction, LIMIT } from '../lib/undo.js';
import { newLoop, newModule, insertAt, removeAt, moveAt, sameTree } from '../lib/tree.js';

/** The tree the issue is about: a temperature loop with modules under it. */
function canonical() {
  let t = newLoop('temperature');
  t = insertAt(t, [], newModule('jv_bace'));
  t = insertAt(t, [], newModule('bace'));
  return t;
}

const entry = (tree, selected = null) => ({ tree, selected });
const same = (a, b) => sameTree(a.tree, b.tree);

test('a new history has one entry and nothing either way', () => {
  const h = start(entry(canonical()));
  assert.equal(canUndo(h), false);
  assert.equal(canRedo(h), false);
  assert.deepEqual(depth(h), { back: 0, forward: 0 });
});

test('✕ on a loop is one press away from back', () => {
  const tree = canonical();
  let h = start(entry(tree, [0]));
  // The click the issue is about: the node and its whole subtree, gone.
  h = push(h, entry(removeAt(tree, []), null), same);
  assert.equal(h.present.tree, null, 'the pipeline is empty');
  h = undo(h);
  assert.deepEqual(h.present.tree, tree, 'the loop and both modules are back');
  assert.deepEqual(h.present.selected, [0], 'and the form that was open is open again');
  assert.equal(canRedo(h), true);
});

test('undo and redo walk the same edits in both directions', () => {
  const t0 = canonical();
  const t1 = insertAt(t0, [], newLoop('repeat'));
  const t2 = moveAt(t1, [2], -1);
  let h = start(entry(t0));
  h = push(h, entry(t1), same);
  h = push(h, entry(t2), same);
  assert.deepEqual(depth(h), { back: 2, forward: 0 });

  h = undo(h); assert.deepEqual(h.present.tree, t1);
  h = undo(h); assert.deepEqual(h.present.tree, t0);
  assert.equal(canUndo(h), false, 'the start of the history is the end of the road');
  assert.equal(undo(h), h, 'and pressing again is not an error, it is nothing');

  h = redo(h); assert.deepEqual(h.present.tree, t1);
  h = redo(h); assert.deepEqual(h.present.tree, t2);
  assert.equal(canRedo(h), false);
  assert.equal(redo(h), h);
});

test('an edit after an undo drops the branch that was redoable', () => {
  const t0 = canonical();
  let h = start(entry(t0));
  h = push(h, entry(insertAt(t0, [], newModule('bace'))), same);
  h = undo(h);
  assert.equal(canRedo(h), true);
  const other = insertAt(t0, [], newLoop('repeat'));
  h = push(h, entry(other), same);
  assert.equal(canRedo(h), false, 'the abandoned branch is gone');
  assert.deepEqual(h.present.tree, other);
});

test('a commit that changes nothing is not an edit', () => {
  // Re-typing a value the node already has reaches `change` with a new object
  // holding the same pipeline. Pushed, it would cost a press of Cmd+Z that
  // visibly does nothing.
  const tree = canonical();
  let h = start(entry(tree));
  h = push(h, entry(structuredClone(tree)), same);
  assert.deepEqual(depth(h), { back: 0, forward: 0 });
  assert.equal(canUndo(h), false);
  // The selection moving is not an edit either, when it comes through `change`.
  h = push(h, entry(structuredClone(tree), [0]), same);
  assert.equal(canUndo(h), false);
});

test('the stack is bounded, and it is the oldest edits that fall off', () => {
  let tree = canonical();
  let h = start(entry(tree));
  for (let i = 0; i < LIMIT + 10; i += 1) {
    tree = insertAt(tree, [], newModule('bace'));
    h = push(h, entry(tree), same);
  }
  assert.equal(depth(h).back, LIMIT);
  assert.deepEqual(h.present.tree, tree, 'the newest edit is still the one on screen');
});

test('reopening a recipe starts a new history rather than pushing onto it', () => {
  const mine = canonical();
  let h = start(entry(mine));
  h = push(h, entry(removeAt(mine, [0])), same);
  // Not `push`: an undo across this boundary would put back a tree from a
  // recipe the operator has closed, under the name of the one they opened.
  h = start(entry(newLoop('illumination')));
  assert.equal(canUndo(h), false);
  assert.equal(canRedo(h), false);
});

test('the history never edits the value it was handed', () => {
  const h0 = start(entry(canonical()));
  const before = JSON.stringify(h0);
  const h1 = push(h0, entry(null), same);
  undo(h1);
  redo(undo(h1));
  assert.equal(JSON.stringify(h0), before);
  assert.notEqual(h1, h0);
});

// -- the keystroke -------------------------------------------------------

const key = (k, mods = {}) => ({ key: k, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, ...mods });

test('Cmd+Z and Ctrl+Z undo; the shifted pair, and Ctrl+Y, redo', () => {
  assert.equal(keyAction(key('z', { metaKey: true })), 'undo');
  assert.equal(keyAction(key('z', { ctrlKey: true })), 'undo');
  assert.equal(keyAction(key('Z', { metaKey: true, shiftKey: true })), 'redo');
  assert.equal(keyAction(key('z', { ctrlKey: true, shiftKey: true })), 'redo');
  // The lab PC is a Windows machine — `scripts/*.bat` is how it is started.
  assert.equal(keyAction(key('y', { ctrlKey: true })), 'redo');
});

test('a bare z, and a modifier that means something else, are not the undo', () => {
  assert.equal(keyAction(key('z')), null);
  assert.equal(keyAction(key('z', { altKey: true, metaKey: true })), null);
  assert.equal(keyAction(key('s', { metaKey: true })), null);
  assert.equal(keyAction(key('y', { metaKey: true })), null, 'Cmd+Y is not redo on a Mac');
});

test('a field being edited keeps its own undo', () => {
  // With the caret in a loop's value list, Cmd+Z is the browser taking back
  // the characters just typed. Stealing it to rewind the tree instead loses
  // an edit the operator can see for one they cannot.
  for (const tag of ['INPUT', 'TEXTAREA', 'SELECT']) {
    assert.equal(keyAction(key('z', { metaKey: true }), { tagName: tag }), null, tag);
  }
  assert.equal(keyAction(key('z', { metaKey: true }), { tagName: 'DIV', isContentEditable: true }), null);
  assert.equal(keyAction(key('z', { metaKey: true }), { tagName: 'BODY' }), 'undo');
  assert.equal(keyAction(key('z', { metaKey: true }), { tagName: 'BUTTON' }), 'undo',
    'the ✕ that was just clicked still has the focus, and that is the press that matters most');
});

test('↑ between two identical siblings changes nothing, and is not recorded', () => {
  // The schema allows two `bace` nodes under one loop — the service numbers
  // them `bace` and `bace#2` — and swapping them leaves the same pipeline.
  // `moveNode`'s own guard is identity, so the commit does reach here.
  let tree = canonical();
  tree = insertAt(tree, [], newModule('bace'));
  let h = start(entry(tree));
  h = push(h, entry(moveAt(tree, [2], -1)), same);
  assert.equal(canUndo(h), false);
});
