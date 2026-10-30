// The plan panel: the plans an agent proposed, and the person's decision on each.
//
// An agent may run only inside a plan a person approved here
// (docs/service-contract.md section 8b). The panel shows everything the
// approval covers, the envelope, the caps and the fingerprint, because the
// approval names that fingerprint: the service refuses it for any other plan.
// Values an agent pinned to keep its runs reproducible fold under one line, so
// what the agent may still choose stands out; they stay one click away.
// Approving takes two clicks, as every action that commits the bench does.

import { h, fill } from './dom.js';

/** A bound as the operator reads it: `−4 to −0.5`, `≤ 4`, `one of a, b`. */
export function describeBound(bound) {
  if (!bound) return '';
  if (Array.isArray(bound.choices)) return `one of ${bound.choices.map(String).join(', ')}`;
  const lo = bound.min, hi = bound.max;
  if (lo !== undefined && hi !== undefined) return `${lo} to ${hi}`;
  if (lo !== undefined) return `≥ ${lo}`;
  if (hi !== undefined) return `≤ ${hi}`;
  return '';
}

/** Every bound of the envelope, in order: `[where, name, bound]`. */
function bounds(envelope) {
  const out = [];
  for (const [module, params] of Object.entries((envelope && envelope.modules) || {})) {
    const names = Object.keys(params || {});
    if (!names.length) out.push([module, '—', null]);
    for (const name of names) out.push([module, name, params[name]]);
  }
  for (const [loop, params] of Object.entries((envelope && envelope.loops) || {})) {
    for (const [name, bound] of Object.entries(params || {})) out.push([`${loop} loop`, name, bound]);
  }
  return out;
}

const row = ([where, name, bound]) => [where, name, bound ? describeBound(bound) : 'as on the bench'];

/** One row per thing the envelope lets an agent set: `[where, name, allowed]`. */
export function envelopeRows(envelope) {
  return bounds(envelope).map(row);
}

/**
 * Voltages, currents, temperatures, delays and the scan axis stay in view even when
 * pinned: they are what a person rejects a plan for (manual 1.6). The rule follows the
 * unit suffix of the parameter names, so a new voltage is caught without a list.
 */
const ALWAYS_SHOWN = /^(vcoll|vpre|voc|v_sat|delay_ns)$|_(v|k|a)$|_on_voc$|^axis_/;

/** A bound that leaves the agent one value. */
function pinned(bound) {
  if (!bound) return false;
  if (Array.isArray(bound.choices)) return bound.choices.length === 1;
  return bound.min !== undefined && bound.min === bound.max;
}

/** The rows to read, and the pinned rows that fold: `{ shown, fixed }`. */
export function splitRows(envelope) {
  const shown = [], fixed = [];
  for (const entry of bounds(envelope)) {
    (pinned(entry[2]) && !ALWAYS_SHOWN.test(entry[1]) ? fixed : shown).push(row(entry));
  }
  return { shown, fixed };
}

/** Plans waiting for a decision. */
export function pendingCount(plans) {
  return (plans || []).filter((p) => p.state === 'proposed').length;
}

function minutes(s) {
  return s === null || s === undefined ? '—' : `${Math.round(Number(s) / 60)} min`;
}

export function createPlansPanel({ api, onChange = () => {} }) {
  let plans = [];
  let open = false;
  let armed = null;
  let armedTimer = null;
  let status = null;
  /** Plans with their pinned values unfolded; a redraw must not fold them again. */
  const unfolded = new Set();

  const el = h('aside.plans', { hidden: true, 'aria-label': 'plans' });
  const headEl = h('div.as-head');
  const noteEl = h('div.as-note');
  const listEl = h('div.pl-list');
  el.append(headEl, noteEl, listEl);

  function arm(id) {
    armed = id;
    clearTimeout(armedTimer);
    armedTimer = id ? setTimeout(() => { armed = null; draw(); }, 6000) : null;
    draw();
  }

  async function decide(plan, decision) {
    if (decision === 'approve' && armed !== plan.plan_id) return arm(plan.plan_id);
    arm(null);
    try {
      if (decision === 'approve') await api.approvePlan(plan.plan_id, plan.fingerprint);
      else if (decision === 'reject') await api.rejectPlan(plan.plan_id, '');
      else await api.closePlan(plan.plan_id, '');
      status = null;
    } catch (error) {
      status = error.text || error.message;
    }
    await refresh();
  }

  const envelopeTable = (rows) => h('table.pl-env',
    h('thead', h('tr', h('th', 'Scope'), h('th', 'Parameter'), h('th', 'Allowed'))),
    h('tbody', ...rows.map(([where, name, allowed]) =>
      h('tr', h('td', where), h('td', name), h('td', allowed)))));

  function card(plan) {
    const caps = plan.envelope || {};
    const { shown, fixed } = splitRows(caps);
    const actions = [];
    if (plan.state === 'proposed') {
      actions.push(h('button.pl-approve', {
        class: armed === plan.plan_id ? 'armed' : '',
        text: armed === plan.plan_id ? `Approve ${plan.fingerprint}?` : 'Approve',
        onclick: () => decide(plan, 'approve'),
      }));
      actions.push(h('button.pl-reject', { text: 'Reject', onclick: () => decide(plan, 'reject') }));
    } else if (plan.state === 'approved') {
      actions.push(h('button.pl-close', { text: 'Close', onclick: () => decide(plan, 'close') }));
    }
    // The limits the approval covers, one cell each.
    const cells = [
      ['Runs', `${(plan.runs || []).length} / ${caps.max_runs}`],
      ['Time', `${minutes(plan.spent_s)} / ${minutes(caps.max_total_s)}`],
      ['Approval', minutes(caps.expires_s)],
      plan.expires_at ? ['Ends', new Date(plan.expires_at * 1000).toLocaleString()] : null,
      ['Plan', plan.plan_id],
      ['Fingerprint', plan.fingerprint],
    ].filter(Boolean);
    return h('section.pl-card', { class: `pl-${plan.state}` },
      h('div.pl-title', h('span', plan.title),
        h('span.pl-state', plan.state === 'approved' && !plan.open ? 'spent' : plan.state)),
      h('p.pl-goal', plan.goal),
      plan.state === 'approved' && plan.reason ? h('p.pl-goal', `Approved: ${plan.reason}`) : null,
      envelopeTable(shown),
      fixed.length ? h('details.pl-fixed', {
        open: unfolded.has(plan.plan_id),
        ontoggle: (e) => {
          if (e.target.open) unfolded.add(plan.plan_id); else unfolded.delete(plan.plan_id);
        },
      }, h('summary', `${fixed.length} more fixed ${fixed.length === 1 ? 'value' : 'values'}`),
      envelopeTable(fixed)) : null,
      h('dl.pl-caps', cells.map(([k, v]) => h('div', h('dt', k), h('dd', v)))),
      actions.length ? h('div.pl-actions', ...actions) : null);
  }

  function draw() {
    fill(headEl, [h('span.as-title', 'Plans'), h('span.spacer'),
      h('button.as-link', { text: 'Close', onclick: () => toggle(false) })]);
    fill(noteEl, status ? [h('p', status)]
      : plans.length ? [] : [h('p', { title: 'An agent proposes a plan; it runs only after approval here.' }, 'No plans proposed')]);
    fill(listEl, [...plans].reverse().map(card));
  }

  async function refresh() {
    try {
      plans = (await api.plans()).plans || [];
    } catch (error) {
      status = error.text || error.message;
    }
    draw();
    onChange({ open, pending: pendingCount(plans) });
  }

  function toggle(want = !open) {
    open = Boolean(want);
    el.hidden = !open;
    // The open state is reported before the list is fetched, so the shell can close
    // the assistant at once rather than after the service answers.
    onChange({ open, pending: pendingCount(plans) });
    if (open) refresh();
  }

  return { el, toggle, refresh, get pending() { return pendingCount(plans); } };
}
