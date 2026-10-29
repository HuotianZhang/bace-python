// The plan panel: the plans an agent proposed, and the person's decision on each.
//
// An agent may run only inside a plan a person approved here
// (docs/service-contract.md section 8b). The panel shows everything the
// approval covers, the envelope, the caps and the fingerprint, because the
// approval names that fingerprint: the service refuses it for any other plan.
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

/** One row per thing the envelope lets an agent set: `[where, name, allowed]`. */
export function envelopeRows(envelope) {
  const rows = [];
  for (const [module, params] of Object.entries((envelope && envelope.modules) || {})) {
    const names = Object.keys(params || {});
    if (!names.length) rows.push([module, '—', 'as on the bench']);
    for (const name of names) rows.push([module, name, describeBound(params[name])]);
  }
  for (const [loop, params] of Object.entries((envelope && envelope.loops) || {})) {
    for (const [name, bound] of Object.entries(params || {})) {
      rows.push([`${loop} loop`, name, describeBound(bound)]);
    }
  }
  return rows;
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

  function card(plan) {
    const caps = plan.envelope || {};
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
      h('table.pl-env',
        h('thead', h('tr', h('th', 'Scope'), h('th', 'Parameter'), h('th', 'Allowed'))),
        h('tbody', ...envelopeRows(caps).map(([where, name, allowed]) =>
          h('tr', h('td', where), h('td', name), h('td', allowed))))),
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
