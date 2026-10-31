// The schedule shows settling and measurement as time blocks. Unknown settling time
// has no duration on the axis; a visible hatch marks its location without inventing a
// median. Known `hold_s` retains its duration. The estimate is then a lower bound and
// has no wall-clock finish (`docs/service-contract.md` §7). Temperature settling
// dominates the cost, taking 14 min to 2 h on this bench (`docs/ui-rules.md` §5). The
// pure model consumes `tree.timeline()` and cost data; the shared frame renders it.

import * as scale from '../scale.js';
import * as fmt from '../format.js';
import { layout } from './frame.js';

/** Under this many pixels a block has no room for its own caption. */
const CAPTION_PX = 34;

/**
 * The constant gives an unknown settle a visible six-pixel hatch without assigning it
 * a duration. The note counts these marks; using another settle's median would invent
 * time.
 */
export const UNKNOWN_PX = 6;

/**
 * The model renders `tree.timeline(scheduleTree)` blocks with
 * `tree.costModel(validation.cost)`. Its x domain uses `total_s` when it agrees with
 * the blocks, otherwise their sum. During validation, the cost can describe a
 * different tree and must not silently determine the current bar's length.
 */
// The schedule is a fixed-height band rather than an `ASPECT` panel. Labels and the
// clock set its height; only its length follows the container. Full-height accent
// blocks would dominate the card (`docs/ui-rules.md` §3).
export function scheduleModel({ blocks, cost, width = 900, height = 118, now = null } = {}) {
  const list = blocks || [];
  const spans = list.map((block) => ({
    block,
    settle: block.settle_s || 0,
    hold: block.hold_s || 0,
    measuring: block.measure_s || 0,
    // Only temperature loops have settling time. Measurement outside one must not
    // receive an unknown-settle hatch.
    unknownSettle: block.settles !== false
      && (block.settle_s === null || block.settle_s === undefined),
  }));
  const total = spans.reduce((sum, s) => sum + s.settle + s.hold + s.measuring, 0);

  if (!list.length || !(total > 0)) {
    return {
      key: 'schedule', width, height, cursor: false,
      absent: {
        text: list.length ? 'nothing to run' : 'no steps yet',
        detail: list.length
          ? 'every module in this structure costs nothing the catalogue can estimate'
          : 'Add a step, then Check',
      },
      panels: [], notes: [],
    };
  }

  // The accent band leaves space for temperature labels and avoids making every block
  // look like an alert (`docs/ui-rules.md` §3).
  const frame = layout({
    width, height,
    panels: [{ key: 'run', weight: 1 }],
    margin: { left: 20, right: 20, top: 34, bottom: 46 },
  });
  const panel = frame.panels[0];
  const { rect } = panel;
  const X = scale.linear([0, total], [rect.x, rect.x + rect.w]);

  const shades = [];
  const marks = [];
  const rules = [];
  let at = 0;
  for (const span of spans) {
    // `hold_s` is a known dwell duration. It must not share the hatch for unknown
    // settling time.
    if (span.unknownSettle) {
      shades.push({ x: X(at), w: UNKNOWN_PX, hatch: true, colour: 'rule', opacity: 1 });
    } else if (span.settle > 0) {
      const x0 = X(at);
      const x1 = X(at + span.settle);
      shades.push({ x: x0, w: x1 - x0, colour: 'grey', opacity: 0.3 });
      if (x1 - x0 >= CAPTION_PX) {
        marks.push({ x: (x0 + x1) / 2, y: rect.y + rect.h / 2 + 3, anchor: 'middle',
          size: 11, colour: 'grey', text: fmt.duration(span.settle) });
      }
      at += span.settle;
    }
    if (span.hold > 0) {
      const x0 = X(at);
      const x1 = X(at + span.hold);
      shades.push({ x: x0, w: x1 - x0, colour: 'grey', opacity: 0.18 });
      at += span.hold;
    }
    const mx0 = X(at);
    const mx1 = X(at + span.measuring);
    if (span.measuring > 0) {
      shades.push({ x: mx0, w: Math.max(mx1 - mx0, 1), colour: 'accent', opacity: 1 });
    }
    if (span.block.setpoint_k !== null && span.block.setpoint_k !== undefined) {
      marks.push({
        x: (mx0 + mx1) / 2, y: rect.y - 6, anchor: 'middle',
        size: 11, weight: 600, colour: 'ink',
        text: `${Number(span.block.setpoint_k).toFixed(0)} K`,
      });
    }
    at += span.measuring;
  }

  // The axis uses elapsed time from Start. A wall-clock finish appears only for an
  // estimate that is not a lower bound (`docs/service-contract.md` §7).
  const lower = Boolean(cost && cost.lower_bound);
  const finishAt = cost ? cost.finish_at : null;
  const startAt = finishAt ? finishAt - total : null;
  const ticks = tickValues(total).map((seconds) => ({
    x: scale.fixed(X(seconds)),
    text: startAt ? fmt.clock(startAt + seconds) : elapsed(seconds),
  }));
  rules.push({ x1: rect.x + rect.w, colour: 'ink', width: 1 });

  const totalText = (lower ? 'at least ' : '') + fmt.duration(total);
  const notes = [];
  if (!startAt) notes.push(`${totalText} from Start`);
  if (spans.some((s) => s.unknownSettle)) {
    const n = spans.filter((s) => s.unknownSettle).length;
    notes.push(`${n} of ${spans.length} temperatures: settle time not known yet`);
  }

  return {
    key: 'schedule',
    width: frame.width, height: frame.height, margin: frame.margin,
    cursor: false,
    // A panel note would collide with the last setpoint label above the bar. Counts
    // belong below it.
    panels: [{ ...panel, shades, marks, rules }],
    x: { ticks, label: startAt ? 'Time' : 'Time from Start' },
    legend: [
      { label: 'Cryostat settle', colour: 'grey', marker: 'band' },
      { label: 'Measure', colour: 'accent' },
      ...(spans.some((s) => s.unknownSettle) ? [{ label: 'Settle not known', colour: 'rule', marker: 'band' }] : []),
    ],
    notes,
  };
}

/**
 * The tick step uses hours or half-hours for long runs and minutes for shorter ones.
 */
function tickValues(total) {
  const step = total > 4 * 3600 ? 3600
    : total > 3600 ? 1800
      : total > 600 ? 300
        : total > 120 ? 60 : 30;
  const out = [];
  for (let t = 0; t <= total + 1e-9; t += step) out.push(t);
  return out;
}

function elapsed(seconds) {
  if (seconds === 0) return '0';
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  const hours = seconds / 3600;
  return Number.isInteger(hours) ? `${hours} h` : `${hours.toFixed(1)} h`;
}
