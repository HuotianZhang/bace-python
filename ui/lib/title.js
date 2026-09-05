// The window title — the console's only channel to an operator who is not
// looking at it.
//
// The measurement this console drives is built out of waiting: a temperature
// step is 14 minutes to 2 hours (`ui-rules` §5), a nine-temperature sweep
// measured 4.5 hours, and where the 331 is not on the bus **every one of those
// steps stops and waits for a person** (`NeedsOperator`, contract §7). The
// design tells that person, correctly and in detail — inside a window they are
// by construction not watching, because the thing they are waiting for takes
// half an hour. Nothing reached the taskbar, the tab strip or the window
// switcher, so the gap between the rig stopping and somebody noticing was
// however long it took them to look, on a sample that is cold and drifting.
//
// So the title says what the run needs, and `document.title` alone: it is the
// one surface that needs no permission, works behind another window, survives
// a minimised console and costs nothing. (The Notification API would ask for a
// grant, and a lab PC that has denied it once denies it silently for good.)
//
// Three states earn it, and nothing else does:
//
//   * **paused** — `⏸ needs you · set 250.0 K`. Ungated: the pause stands until
//     it is answered, so the title is true whether or not anybody is looking.
//   * **running** — `⏵ bace · T 250 K · 1 of 9`. The outermost loop counter
//     and no more: the shot counter moves at shot rate, and a taskbar entry
//     that rewrites itself twice a second is not a glance. The counters at
//     three scales are the monitor's job, on the screen.
//   * **ended, unseen** — `✓ done`, `✕ failed`, `⏹ stopped · 20/100`. Only
//     while the operator has not looked since (`announce`), because an ending
//     is a fact about the past: it should be waiting for them when they come
//     back, and gone once they have.
//
// The model is the monitor's, re-rendered — never a second opinion about what
// the run is doing. `monitorModel` is null exactly when nothing holds the
// worker, which is the same test the monitor draws itself on.

import { currentRun, TERMINAL } from './store.js';
import { monitorModel } from './monitor.js';
import * as fmt from './format.js';

/** What the title reads when nothing is happening — `index.html`'s own. */
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
 * What the pause is asking for, in the few words a taskbar has room for.
 * The whole of it — the tolerance, the hold, why the controller gave up — is
 * on the monitor; this is the part that gets somebody to walk back.
 */
function asking(prompt) {
  if (prompt.temperature && prompt.setpoint_k !== null && prompt.setpoint_k !== undefined) {
    return `set ${fmt.kelvin(prompt.setpoint_k)}`;
  }
  return prompt.what || 'the operator';
}

/**
 * The title, as a model.
 *
 * @param state the store's state.
 * @param announce the run whose ending has not been seen yet, or `null`. The
 *   shell owns that judgement — it is the one thing here that depends on
 *   whether the window has focus, which is not in the store.
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
    // `ui-rules` §9: a truncated run is normal, and *kept of requested* is how
    // it is said — including here, where it is the difference between a sweep
    // that finished and one that gave up at shot 20.
    const counts = how.word === 'done' || ended.kept === null || ended.kept === undefined
      ? '' : ` · ${fmt.keptOf(ended.kept, ended.requested)}`;
    return { level: 'ended', text: `${how.mark} ${how.word} · ${label}${counts} — ${BASE}` };
  }
  return { level: 'idle', text: BASE };
}

/**
 * The run holding the worker right now, by id, or `null`.
 *
 * The shell watches this across draws to notice an ending: `activeRunId` is
 * cleared at `parked` and the record keeps `parked_at`, so "was live, is not"
 * is the whole test and it needs no frame of its own — a `parked` lost to a
 * ring gap must not cost the operator the only sign their run is over.
 */
export function liveRunId(state) {
  const run = currentRun(state);
  if (!run || run.parked_at) return null;
  return state.activeRunId === run.run_id ? run.run_id : null;
}

/**
 * Write it, if it moved. Compared rather than assigned every notify: the store
 * notifies once per animation frame for the length of a run, and a title
 * rewritten sixty times a second is a taskbar entry that flickers in some
 * window managers and a diff in none.
 */
export function renderTitle(doc, state, options) {
  const model = titleModel(state, options);
  if (doc.title !== model.text) doc.title = model.text;
  return model;
}
