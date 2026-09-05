// The way back from a tree edit — `lib/undo.js`.
//
// `✕` on a tree row removes the node *and everything under it*, in one click,
// and a nine-temperature tree with five levels and two modules under it is one
// mis-click from gone. `↑`, `↓`, adding a node and typing an override are as
// irreversible, and get no ceremony at all.
//
// The way back is an undo stack rather than a confirm dialog on `✕`. A dialog
// taxes every deliberate removal — and there are many, composing a tree is
// mostly removals — to catch the rare accidental one, and it answers nothing
// for the four edits that have no dialog. With a stack behind it the click is
// cheap, which is the point: the operator composes by trying things.
//
// It is cheap to keep because `lib/tree.js` is immutable — every edit returns a
// new tree and shares every branch it did not touch — so a history is a list of
// references, not of copies. **Nothing in here knows what a tree is.** An entry
// is whatever the view wants restored together (here: the tree and which node's
// form is open), and `push` is told how to compare two of them, so the one
// module that does know — `lib/tree.js` — stays the only one that does.
//
// The value is immutable too, in the same idiom as the trees it carries: every
// function answers a new history and none edits the one it was given.

/**
 * How many edits back it goes.
 *
 * Not a memory bound — structural sharing makes fifty trees cost about what
 * one costs — but a bound on how far a held `Cmd+Z` can rewind a four-hour
 * session's worth of composing in one press-and-hold. Fifty is about a
 * morning's edits to one tree, and past that the recipe on disk is the way
 * back rather than the stack.
 */
export const LIMIT = 50;

/**
 * A history holding one entry and nothing to undo — what reopening a recipe,
 * or booting onto `GET /pipelines/last`, starts.
 *
 * A *new* history rather than a push, because those two do not edit the tree
 * on screen, they replace it: an undo across that boundary would put back a
 * tree from a recipe the operator has closed, under the name of the one they
 * opened. The tree they came from is still in its own file.
 */
export function start(entry) {
  return { past: [], present: entry, future: [] };
}

/**
 * Commit `entry`, dropping anything that was redoable — the ordinary rule:
 * editing after an undo makes a new branch, and the abandoned one is gone.
 *
 * `same(a, b)` decides whether this is an edit at all. It has to be asked,
 * because not every commit changes the tree: re-typing a value it already
 * has, or switching a loop's value form to the form it is already in, both
 * reach here with a new object holding the same pipeline. Pushed, each one
 * costs a press of `Cmd+Z` that visibly does nothing — the failure that makes
 * an undo stack feel broken. Identity is the default and catches the cheap
 * case; `views/pipeline.js` passes `tree.sameTree` for the rest.
 */
export function push(hist, entry, same = (a, b) => a === b) {
  if (hist.present !== undefined && same(entry, hist.present)) return hist;
  const past = [...hist.past, hist.present].slice(-LIMIT);
  return { past, present: entry, future: [] };
}

/** One edit back, or the history unchanged when it is already at the start. */
export function undo(hist) {
  if (!canUndo(hist)) return hist;
  return {
    past: hist.past.slice(0, -1),
    present: hist.past[hist.past.length - 1],
    future: [hist.present, ...hist.future],
  };
}

/** One edit forward, or the history unchanged when nothing was undone. */
export function redo(hist) {
  if (!canRedo(hist)) return hist;
  return {
    past: [...hist.past, hist.present],
    present: hist.future[0],
    future: hist.future.slice(1),
  };
}

export function canUndo(hist) {
  return Boolean(hist) && hist.past.length > 0;
}

export function canRedo(hist) {
  return Boolean(hist) && hist.future.length > 0;
}

/** What the two buttons say when they are hovered: how far either way. */
export function depth(hist) {
  return { back: hist ? hist.past.length : 0, forward: hist ? hist.future.length : 0 };
}

/**
 * Whether a keystroke is an undo, a redo, or neither.
 *
 * `Cmd+Z` / `Cmd+Shift+Z` on a Mac, `Ctrl+Z` / `Ctrl+Shift+Z` and `Ctrl+Y`
 * on the lab PC — which is a Windows machine (`scripts/*.bat` is how it is
 * started), where `Ctrl+Y` is redo and leaving it out would read as a bug.
 *
 * **A field being edited keeps its own undo.** With the caret in a loop's
 * value list, `Cmd+Z` is the browser taking back the characters just typed,
 * and stealing it to rewind the tree instead would be worse than having no
 * undo at all: the operator loses an edit they can see for one they cannot.
 * The tree's fields commit on `change` — blur or Enter — so a keystroke that
 * reaches here with the focus elsewhere is unambiguous.
 */
export function keyAction(e, active = null) {
  if (!(e.metaKey || e.ctrlKey) || e.altKey) return null;
  if (editing(active)) return null;
  const key = String(e.key || '').toLowerCase();
  if (key === 'y' && !e.metaKey) return 'redo';
  if (key !== 'z') return null;
  return e.shiftKey ? 'redo' : 'undo';
}

function editing(el) {
  if (!el) return false;
  if (el.isContentEditable) return true;
  return ['input', 'textarea', 'select'].includes(String(el.tagName || '').toLowerCase());
}
