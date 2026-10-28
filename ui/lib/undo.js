// The bounded undo stack protects the client-owned pipeline tree. Removing a subtree,
// opening a recipe and clearing node overrides can discard substantial composition
// work. Unlike sample exposure, this work is recoverable, so those operations use
// undo rather than extra confirmation. The stack holds opaque snapshots; the caller
// identifies changes and action labels through its single mutation path.

/**
 * The history retains up to `limit` undo snapshots. Fifty trees occupy a few tens
 * of kilobytes on this bench.
 */
export function createHistory({ limit = 50 } = {}) {
  /** States left behind, oldest first; the last is the one `undo` returns. */
  let past = [];
  /** States undone out of, newest first; the first is what `redo` returns. */
  let future = [];

  return {
    /**
     * The recorder saves the state being left with the replacing action's label. Undo
     * can then name the action being reversed rather than require the operator to
     * remember several clicks.
     */
    push(state, label = '') {
      past.push({ state, label });
      if (past.length > limit) past.splice(0, past.length - limit);
      // An edit after undo discards the unused redo branch.
      future = [];
    },

    /**
     * The state before the last recorded action, or `null`. `current` becomes the redo.
     */
    undo(current) {
      const entry = past.pop();
      if (!entry) return null;
      future.unshift({ state: current, label: entry.label });
      return entry;
    },

    /** Redo returns the state removed by undo, or `null`. */
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
