// A bounded undo stack, for the one thing in this console that is composed by
// hand.
//
// Everything else on screen is a value the service owns: an edit is a `PUT`,
// the way back is the `↺` beside the row, and the service holds the layer
// underneath. The **pipeline tree** is the exception — it is the client's own
// until Start, it takes dozens of deliberate clicks to build (the canonical
// tree is 9 temperatures × 5 levels), and three of the controls that act on it
// destroy work in one click with nothing offering it back: `✕` takes a node
// *and everything under it*, the recipe picker replaces the whole tree, and
// `↺ bench` drops every override a node types.
//
// The asymmetry that argues for this: every action that costs the *sample*
// something already arms and confirms — Park on a busy bench, Abort, and (as
// of this change) an overwriting Save. Nothing armed for the operator's own
// work, which is the cheaper of the two to protect and the only one that is
// recoverable at all.
//
// Deliberately dumb: it holds opaque snapshots and compares nothing. The
// caller decides what a state is and whether two of them differ, because
// `views/pipeline.js` already funnels every mutation through one function
// (`change`) and already knows what changed enough to name it.

/**
 * @param {{limit?: number}} options — how many steps back are kept. Fifty
 *   trees is a few tens of kilobytes and more steps than a composition has.
 */
export function createHistory({ limit = 50 } = {}) {
  /** States left behind, oldest first; the last is the one `undo` returns. */
  let past = [];
  /** States undone out of, newest first; the first is what `redo` returns. */
  let future = [];

  return {
    /**
     * Record the state being left, under the name of what is replacing it.
     *
     * The label describes the **action**, not the state — `remove temperature`
     * — so the button can say what it will take back rather than only that it
     * can take something back. A person who has clicked four times and looked
     * away needs the name, not the count.
     */
    push(state, label = '') {
      past.push({ state, label });
      if (past.length > limit) past.splice(0, past.length - limit);
      // The ordinary rule: a new branch drops the one that was not taken.
      future = [];
    },

    /** The state before the last recorded action, or `null`. `current` becomes the redo. */
    undo(current) {
      const entry = past.pop();
      if (!entry) return null;
      future.unshift({ state: current, label: entry.label });
      return entry;
    },

    /** The state that was undone out of, or `null`. */
    redo(current) {
      const entry = future.shift();
      if (!entry) return null;
      past.push({ state: current, label: entry.label });
      return entry;
    },

    get canUndo() { return past.length > 0; },
    get canRedo() { return future.length > 0; },
    /** What `undo` would take back, for the button's own label. */
    get undoLabel() { return past.length ? past[past.length - 1].label : ''; },
    /** What `redo` would put back. */
    get redoLabel() { return future.length ? future[0].label : ''; },
    get depth() { return past.length; },

    clear() { past = []; future = []; },
  };
}
