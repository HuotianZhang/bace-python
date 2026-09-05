// The run as a length of time — `ch-schedule` from R3·3, and the fifth chart
// component.
//
// The design draws it as alternating blocks: the cryostat settling in grey,
// the measuring in accent, one pair per temperature, over a wall clock that
// ends at `finish 01:36 · 4 h 32`. Two things it draws that this cannot, and
// both are the same fact:
//
//   * **a settle nobody has measured has no width.** The artboard's own table
//     shows `—` for the first temperature, and `pipeline.estimate` refuses to
//     invent one ("Never invent a settle time"). A block drawn at the median
//     of the others would be this file guessing at the slowest thing in the
//     system by three orders of magnitude (`ui-rules` §5: 14 min to 2 h). So
//     an unmeasured settle is **hatched, and takes no time on the axis** — the
//     mark `lib/charts/loops.js` already uses for *not acquired* — and the
//     note under the bar says the whole thing is a floor. The `hold_s` beside
//     it is drawn at its width, because that one is a number the tree typed.
//   * **there is no clock on a lower bound.** `cost.finish_at` is `null`
//     exactly when `lower_bound` is set, so the axis is elapsed time from
//     Start, and the wall clock appears only when the cost stopped being a
//     floor. A `finish 01:36` under a number that is "at least" is a promise
//     the run cannot keep.
//
// The model is a pure function of `tree.timeline()` and the cost; the DOM is
// `charts/frame.js`, as it is for the other four.

import * as scale from '../scale.js';
import * as fmt from '../format.js';
import { layout } from './frame.js';

/** Under this many pixels a block has no room for its own caption. */
const CAPTION_PX = 34;

/**
 * The width a settle nobody has measured is drawn at.
 *
 * Not zero, because a mark with no width is not a mark; not the median of the
 * others, because that would be this file guessing at the slowest thing in
 * the system. Six pixels of hatch, at the place the time will go, and the
 * note under the bar says how many of them there are.
 */
export const UNKNOWN_PX = 6;

/**
 * `{blocks, cost}` → a chart model. `blocks` is `tree.timeline(scheduleTree)`
 * and `cost` is `tree.costModel(validation.cost)`.
 *
 * The x domain is the cost's own `total_s` where the two agree, and the sum
 * of the blocks otherwise — they disagree only while a validate is in flight,
 * and a bar drawn to a total from the previous tree would be the one thing on
 * this screen that silently described something else.
 */
export function scheduleModel({ blocks, cost, width = 900, height = 118, now = null } = {}) {
  const list = blocks || [];
  const spans = list.map((block) => ({
    block,
    settle: block.settle_s || 0,
    hold: block.hold_s || 0,
    measuring: block.measure_s || 0,
    // Only a temperature loop settles. A stretch of measuring outside one has
    // no settle to be unknown about, and hatching it would say the console
    // could not tell how long something takes that takes no time at all.
    unknownSettle: block.settles !== false
      && (block.settle_s === null || block.settle_s === undefined),
  }));
  const total = spans.reduce((sum, s) => sum + s.settle + s.hold + s.measuring, 0);

  if (!list.length || !(total > 0)) {
    return {
      key: 'schedule', width, height, cursor: false,
      absent: {
        text: list.length ? 'nothing to run' : 'no nodes yet',
        detail: list.length
          ? 'every module in this structure costs nothing the catalogue can estimate'
          : 'add a loop or a module, and the schedule appears with the first Dry run',
      },
      panels: [], notes: [],
    };
  }

  // A band, not a panel filling the card. The blocks are the accent colour,
  // and `ui-rules` §3 reserves that intensity for one thing on screen at a
  // time — nine full-height bars of it read as nine alerts. The artboard
  // gives the bar a quarter of its height for the same reason, and the
  // temperature labels need the room above it.
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
    // The settle and the hold are two different kinds of waiting and only one
    // of them is unknown: `hold_s` is a number the tree typed, and drawing it
    // hatched alongside the settle would say the console cannot tell how long
    // a sixty-second dwell takes.
    if (span.unknownSettle) {
      shades.push({ x: X(at), w: UNKNOWN_PX, hatch: true, colour: 'rule', opacity: 1 });
    } else if (span.settle > 0) {
      const x0 = X(at);
      const x1 = X(at + span.settle);
      shades.push({ x: x0, w: x1 - x0, colour: 'grey', opacity: 0.3 });
      if (x1 - x0 >= CAPTION_PX) {
        marks.push({ x: (x0 + x1) / 2, y: rect.y + rect.h / 2 + 3, anchor: 'middle',
          mono: true, size: 9.5, colour: 'grey', text: fmt.duration(span.settle) });
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
        x: (mx0 + mx1) / 2, y: rect.y - 6, anchor: 'middle', mono: true,
        size: 10, weight: 600, colour: 'ink',
        text: `${Number(span.block.setpoint_k).toFixed(0)} K`,
      });
    }
    at += span.measuring;
  }

  // The x axis: elapsed time from Start, and the wall clock only where the
  // cost stopped being a floor (`finish_at` is null exactly when it has not).
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
  notes.push(`${spans.length} ${spans.length === 1 ? 'block' : 'temperatures'} · `
    + `${fmt.plural(list.reduce((n, b) => n + (b.modules || 0), 0), 'module run')} · `
    + `${fmt.plural(list.reduce((n, b) => n + (b.shots || 0), 0), 'shot')}`);
  notes.push(startAt
    ? `start ${fmt.clock(startAt)} · finish ${fmt.clock(finishAt)} · ${totalText}`
    : `${totalText} from Start · the axis is elapsed time, because no finish time is knowable yet`);
  if (spans.some((s) => s.unknownSettle)) {
    const n = spans.filter((s) => s.unknownSettle).length;
    notes.push(`${n} of ${spans.length} temperatures have no measured settle behind them — `
      + 'hatched, and taking no time on this axis rather than the median of the others. '
      + 'The journal fills them in as this bench measures them.');
  }

  return {
    key: 'schedule',
    width: frame.width, height: frame.height, margin: frame.margin,
    cursor: false,
    // No panel note: the setpoint labels sit along the top of the bar, and a
    // caption in the corner lands on the last one of them. The counts belong
    // under the bar with the rest of the prose.
    panels: [{ ...panel, shades, marks, rules }],
    x: { ticks, label: startAt ? 'clock' : 'elapsed / from Start' },
    legend: [
      { label: 'settle · cryostat', colour: 'grey', marker: 'band' },
      { label: 'measure', colour: 'accent' },
      ...(spans.some((s) => s.unknownSettle) ? [{ label: 'settle not measured', colour: 'rule', marker: 'band' }] : []),
    ],
    notes,
  };
}

/** Round hours or half-hours where the run is long, minutes where it is not. */
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
