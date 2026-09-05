// The design pack's icon set, ported from `docs/design/icons.js`.
//
// The set was drawn for the console and shipped into the repo on 2026-09-02;
// all four `.dc.html` artboards import it and, until this file, `ui/` imported
// it nowhere. Every place the canvas puts an icon — the rail's eight cells,
// the chain strip's verdicts, the run buttons, the pipeline's node kinds, the
// inherited-value mark — the console had a Unicode character or nothing.
//
// The geometry is the design's, kept verbatim: 24×24 Lucide-style strokes with
// squared caps to match the flat system. What changed is the wrapper. The
// source defines a custom element with a Shadow DOM; here it is a pure markup
// function plus a thin element around it, which is the shape `charts/rig.js`
// already uses for the pack's other drawings — testable without a browser, and
// no second component model to learn.
//
// **`stroke="currentColor"` is the point, not a detail.** An icon inherits the
// colour of the thing it labels, so a chain verdict that turns from `--ok` to
// `--accent-dark` takes its glyph with it, and `ui-rules` §3's loudness order
// keeps working with no icon-specific palette to maintain.

import { s } from './svg.js';

/** The 24×24 path geometry, verbatim from `docs/design/icons.js`. */
export const ICONS = {
  relay: '<path d="M3 12h5"/><path d="M16 12h5"/><path d="M8 12l6-5"/><circle cx="8" cy="12" r="1.6"/><circle cx="16" cy="12" r="1.6"/>',
  bias: '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z"/>',
  smu: '<rect x="3" y="4" width="18" height="16"/><path d="M7 15l3-6 3 4 2-3"/>',
  shutter: '<circle cx="12" cy="12" r="9"/><path d="M12 3v9M20.5 8.5 12 12M17 20 12 12M7 20l5-8M3.5 8.5 12 12"/>',
  led: '<circle cx="12" cy="12" r="4"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/>',
  voc: '<path d="M3 18h18"/><path d="M3 18C7 18 8 5 13 5s5 7 8 7"/>',
  power: '<path d="M12 21a9 9 0 1 1 9-9"/><path d="M12 12l5-4"/>',
  temp: '<path d="M10 13.5V4a2 2 0 1 1 4 0v9.5a4 4 0 1 1-4 0z"/>',
  scope: '<rect x="2" y="4" width="20" height="16"/><path d="M5 14l3-5 2 7 3-9 2 5 3-3"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  play: '<path d="M6 4l14 8-14 8z"/>',
  stop: '<rect x="5" y="5" width="14" height="14"/>',
  pause: '<path d="M8 4v16M16 4v16"/>',
  warn: '<path d="M12 3 1.5 21h21z"/><path d="M12 9v5M12 17.5v.5"/>',
  crit: '<circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16.5v.5"/>',
  ok: '<path d="M4 12.5 9.5 18 20 6"/>',
  // A dashed ring: the pack's glyph for *there is no value here*. `ui-rules`
  // §2's two zeros that are not zeros — σ_Q = 0 is **not recorded**, LED
  // intensity = 0 is **not calibrated** — are absences, and an absence drawn
  // as a value is the lie that rule exists to stop.
  blank: '<circle cx="12" cy="12" r="8" stroke-dasharray="3 3"/>',
  inherit: '<path d="M4 4v8a4 4 0 0 0 4 4h12"/><path d="M16 12l4 4-4 4"/>',
  loop: '<path d="M4 9h13a4 4 0 0 1 0 8H8"/><path d="M8 5 4 9l4 4"/>',
  module: '<rect x="4" y="4" width="16" height="16"/>',
  note: '<path d="M5 3h14v18l-7-4-7 4z"/>',
  down: '<path d="M12 4v16M6 14l6 6 6-6"/>',
  right: '<path d="M4 12h16M14 6l6 6-6 6"/>',
  drag: '<circle cx="9" cy="6" r="1.4"/><circle cx="15" cy="6" r="1.4"/><circle cx="9" cy="12" r="1.4"/><circle cx="15" cy="12" r="1.4"/><circle cx="9" cy="18" r="1.4"/><circle cx="15" cy="18" r="1.4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  cursor: '<path d="M3 12h18"/><path d="M12 6l6 6-6 6"/>',
  reset: '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5.5h5.5"/>',
  edit: '<path d="M4 20h4l11-11-4-4L4 16z"/><path d="M13 7l4 4"/>',
  flag: '<path d="M5 21V4h13l-2 5 2 5H5"/>',
  eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
};

/**
 * The markup for one icon, or `blank`'s dashed ring for a name that does not
 * exist — the same fallback the pack's custom element takes, and the right one:
 * a missing glyph reads as "nothing here", never as some other instrument.
 */
export function iconMarkup(name) {
  return ICONS[name] || ICONS.blank;
}

/**
 * One icon as an element. `size` is the box in CSS pixels; `title` becomes a
 * `<title>` so the glyph is not silent to a screen reader — several of these
 * carry the only statement in their cell (`blank` on a value nobody recorded).
 */
export function icon(name, { size = 12, sw = 1.9, cls = '', title = null } = {}) {
  const el = s('svg', {
    class: ('icon ' + cls).trim(),
    viewBox: '0 0 24 24',
    width: size,
    height: size,
    fill: 'none',
    stroke: 'currentColor',
    'stroke-width': sw,
    'stroke-linecap': 'square',
    'stroke-linejoin': 'miter',
    'aria-hidden': title ? null : 'true',
    role: title ? 'img' : null,
  });
  el.innerHTML = (title ? `<title>${String(title).replace(/[<&]/g, '')}</title>` : '') + iconMarkup(name);
  return el;
}
