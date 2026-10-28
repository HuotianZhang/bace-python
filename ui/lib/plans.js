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
        text: armed === plan.plan_id ? `approve ${plan.fingerprint}?` : 'approve',
        onclick: () => decide(plan, 'approve'),
      }));
      actions.push(h('button.pl-reject', { text: 'reject', onclick: () => decide(plan, 'reject') }));
    } else if (plan.state === 'approved') {
      actions.push(h('button.pl-close', { text: 'close', onclick: () => decide(plan, 'close') }));
    }
    return h('section.pl-card', { class: `pl-${plan.state}` },
      h('div.pl-title', h('span', plan.title), h('span.pl-state', plan.state + (plan.state === 'approved' && !plan.open ? ' · spent or expired' : ''))),
      h('p.pl-goal', plan.goal),
      h('table.pl-env',
        h('thead', h('tr', h('th', 'module or loop'), h('th', 'parameter'), h('th', 'allowed'))),
        h('tbody', ...envelopeRows(caps).map(([where, name, allowed]) =>
          h('tr', h('td', where), h('td', name), h('td', allowed))))),
      h('p.pl-caps', `runs ${(plan.runs || []).length} of ${caps.max_runs} · time ${minutes(plan.spent_s)} of ${minutes(caps.max_total_s)} · approval lasts ${minutes(caps.expires_s)}`
        + (plan.expires_at ? ` · ends ${new Date(plan.expires_at * 1000).toLocaleString()}` : '')),
      h('p.pl-fp', `${plan.plan_id} · fingerprint ${plan.fingerprint}`),
      actions.length ? h('div.pl-actions', ...actions) : null);
  }

  function draw() {
    fill(headEl, [h('span.as-title', 'plans'), h('span.spacer'),
      h('button.as-link', { text: 'close', onclick: () => toggle(false) })]);
    fill(noteEl, status ? [h('p', status)]
      : plans.length ? [] : [h('p', 'No plan has been proposed. An agent proposes one through POST /plans; it runs only after it is approved here.')]);
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
    if (open) refresh(); else onChange({ open, pending: pendingCount(plans) });
  }

  return { el, toggle, refresh, get pending() { return pendingCount(plans); } };
}
