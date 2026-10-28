// The title reaches operators outside the console window. Temperature steps take 14
// min to 2 h and a nine-temperature sweep measures 4.5 h on this bench
// (`docs/ui-rules.md` §5). Without a controller, steps pause for a person
// (`docs/service-contract.md` §7). `document.title` needs no permission and remains
// visible in tabs and window switching; denied notification permissions would be
// unreliable. Pauses persist through focus. Running titles use only the outermost
// counter to avoid shot-rate changes. Endings remain only while unseen. The title
// renders the monitor model rather than deriving another run state.

import { currentRun, TERMINAL } from './store.js';
import { monitorModel } from './monitor.js';
import * as fmt from './format.js';

/** The idle title matches `index.html`. */
export const BASE = 'BACE console';

/** The ending, as one word and one mark. Anything else is reported verbatim. */
const ENDINGS = {
  done: { mark: '✓', word: 'done' },
  stopped: { mark: '⏹', word: 'stopped' },
  aborted: { mark: '⏹', word: 'aborted' },
  cancelled: { mark: '⏹', word: 'cancelled' },
  failed: { mark: '✕', word: 'failed' },
  blocked: { mark: '✕', word: 'blocked' },
};

/**
 * Pause titles retain the request that brings the operator back. Tolerance, hold and
 * controller failure details stay in the monitor because taskbar space is limited.
 */
function asking(prompt) {
  if (prompt.temperature && prompt.setpoint_k !== null && prompt.setpoint_k !== undefined) {
    return `set ${fmt.kelvin(prompt.setpoint_k)}`;
  }
  return prompt.what || 'the operator';
}

/**
 * The model builds the window title from store state. The shell supplies `announce`,
 * the run ending awaiting acknowledgement, because focus and visibility are outside
 * the store.
 */
export function titleModel(state, { announce = null } = {}) {
  const live = monitorModel(state);
  if (live) {
    if (live.prompt) {
      return { level: 'paused', text: `⏸ needs you · ${asking(live.prompt)} — ${BASE}` };
    }
    if (live.state === 'stopping') {
      return { level: 'stopping', text: `⏹ stopping · ${live.label} — ${BASE}` };
    }
    // The outermost loop is the one whose iteration is measured in hours; on a
    // manual run there is none and the label stands alone.
    const outer = live.loops.length ? ` · ${live.loops[0].text}` : '';
    return { level: 'running', text: `⏵ ${live.label}${outer} — ${BASE}` };
  }
  const ended = announce ? state.runs[announce] : null;
  if (ended && (TERMINAL.has(ended.state) || ended.parked_at)) {
    const how = ENDINGS[ended.state] || { mark: '⏹', word: ended.state || 'ended' };
    const label = ended.module || ended.name || ended.kind || 'run';
    // The title includes kept/requested counts to distinguish truncated runs from
    // complete sweeps (`docs/ui-rules.md` §9).
    const counts = how.word === 'done' || ended.kept === null || ended.kept === undefined
      ? '' : ` · ${fmt.keptOf(ended.kept, ended.requested)}`;
    return { level: 'ended', text: `${how.mark} ${how.word} · ${label}${counts} — ${BASE}` };
  }
  return { level: 'idle', text: BASE };
}

/**
 * The helper returns the worker-owning run ID or `null`. Comparing draws detects
 * endings through `activeRunId` and `parked_at`, even if a ring gap loses `parked`;
 * absent operators still need the ending announcement.
 */
export function liveRunId(state) {
  const run = currentRun(state);
  if (!run || run.parked_at) return null;
  return state.activeRunId === run.run_id ? run.run_id : null;
}

/**
 * The writer changes the title only when its text differs. Per-frame assignment can
 * cause taskbar flicker in some window managers without conveying any change.
 */
export function renderTitle(doc, state, options) {
  const model = titleModel(state, options);
  if (doc.title !== model.text) doc.title = model.text;
  return model;
}
