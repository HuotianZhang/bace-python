// A chart's view is what the operator set on its axes: ranges and the log switch. It
// lives here by chart id because a live chart is rebuilt with every shot, and a zoom
// has to outlast that. Ranges are stored in the units the axis shows and tagged with
// them. An axis whose unit changes from nC to pC returns to automatic rather than
// keeping numbers that now mean something else.

const views = new Map();

/** The session's view of one chart, created on first use. */
export function viewOf(id) {
  if (!views.has(id)) views.set(id, { ranges: {}, log: null });
  return views.get(id);
}

/** Forgets every chart's view. Tests start from here. */
export function clearViews() {
  views.clear();
}

/** The set range of axis `key`, or `auto` when none applies. */
export function ranged(view, key, unit, auto) {
  const range = view && view.ranges[key];
  return range && range.unit === unit ? [range.lo, range.hi] : auto;
}

export function setRange(view, key, unit, lo, hi) {
  view.ranges[key] = { lo, hi, unit };
}

export function clearRange(view, key) {
  delete view.ranges[key];
}

/** Returns every axis of the chart to automatic, and the log switch to its default. */
export function resetView(view) {
  view.ranges = {};
  view.log = null;
}

export function isManual(view) {
  return Boolean(view) && (Object.keys(view.ranges).length > 0 || view.log !== null);
}

/**
 * Reads a min and max as typed. A blank side takes the axis's present limit, and both
 * blank means automatic. Returns `{ auto: true }`, `{ lo, hi }`, or `null` for a range
 * the axis cannot draw.
 */
export function parseRange(minText, maxText, [autoLo, autoHi], { log = false } = {}) {
  const min = String(minText).trim();
  const max = String(maxText).trim();
  if (!min && !max) return { auto: true };
  const lo = min ? Number(min.replace('−', '-')) : autoLo;
  const hi = max ? Number(max.replace('−', '-')) : autoHi;
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || lo >= hi) return null;
  if (log && lo <= 0) return null;
  return { lo, hi };
}
