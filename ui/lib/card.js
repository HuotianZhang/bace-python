// The generated module card, and the one field component every parameter goes
// through — `docs/ui-plan.md` M2, decision 1. There is one field component and
// one card component, and all six bench cards come from them; nothing about
// `bace`'s fifty parameters is typed out by hand.
//
// The payoff is `ui-rules` §6: with one component owning
// `{value, source, detail, editable}`, there is no second place that could
// re-derive it. Inherited-reads-as-inherited, derived-is-not-editable and
// `PUT null` resets are one implementation, not six.
//
// **Every field says what it is.** `doc` is the line under the name;
// `doc_full` is the whole explanation, hidden until the pointer is on the
// field (and reachable from the keyboard, since a tooltip nobody can focus is
// a tooltip half the operators cannot read). `ui-rules` §1: the name is the
// label, not the explanation — `smu_nplc`, `trigger_sweep` and `duty_percent`
// mean nothing on their own, and `inverted_output` means something other than
// it looks.
//
// The card never keeps its own copy of a value. An edit `PUT`s and re-renders
// from the entry the service answers with, so the estimate, the point count
// and the provenance all move together and none of them is this file's guess.

import { h, fill } from './dom.js';
import * as fmt from './format.js';
import { cardModel, foldCount } from './fields.js';

/**
 * One card. `ctx` carries `{api, store, onError}`; `open` is the per-card
 * disclosure state, kept by the caller so a re-render does not close a fold
 * the operator just opened.
 */
export function moduleCard(entry, ctx, open) {
  const model = cardModel(entry, { bench: ctx.bench });
  const busy = Boolean(ctx.busy);
  const card = h('div.card.mod', { dataset: { module: model.name } });

  const blocked = startBlockers(ctx.verdicts, model);
  fill(card,
    header(model, ctx, busy, blocked),
    h('div.cb',
      h('div.pr', model.above.map((row) => renderRow(row, model, ctx))),
      model.readback ? readback(model.readback) : null,
      needsList(model),
      fold(model, ctx, open),
      blocked.length ? blockedNote(blocked) : null));
  return card;
}

// -- the header -----------------------------------------------------------
function header(model, ctx, busy, blocked) {
  const buttons = [];
  for (const action of model.actions) {
    buttons.push(h('button.btns', {
      disabled: busy || null,
      title: busy ? 'a run holds the worker; every action but park answers 409' : `POST /bench/actions/${action.action}`,
      onclick: () => ctx.act(action),
    }, action.label));
  }
  for (const [i, run] of model.run.entries()) {
    const last = i === model.run.length - 1;
    buttons.push(h(last ? 'button.btnp' : 'button.btns', {
      disabled: busy || blocked.length > 0 || null,
      title: blocked.length ? blocked.map((c) => `${c.level}: ${c.text}`).join('\n') : model.estimate,
      onclick: () => ctx.run(model, run),
    }, run.label));
  }
  return h('div.ch',
    h('span.cn', { text: model.name }),
    model.status !== 'built' ? h('span.tag.nb', { text: model.status }) : null,
    h('span.cs', { text: model.estimate }),
    model.chips.map((text) => h('span.tag.cost', { text })),
    lastTag(model.last),
    h('span', { style: { flex: '1' } }),
    buttons);
}

function lastTag(last) {
  if (!last) return null;
  const level = last.state === 'done' ? 'ok' : last.state === 'failed' ? 'bad' : '';
  return h('span.tag' + (level ? '.' + level : ''),
    { title: last.summary || '', text: `${last.state} ${fmt.clock(last.ts)}` });
}

// -- the rows -------------------------------------------------------------
function renderRow(row, model, ctx) {
  switch (row.kind) {
    case 'range': return rangeRow(row, ctx, model);
    case 'voc': return vocRow(row, ctx, model);
    case 'polarity': return polarityRow(row, ctx, model);
    case 'segmented': return field(row.spec, ctx, model, { segmented: true, accent: row.accent });
    default: return field(row.spec, ctx, model, { accent: row.accent });
  }
}

/**
 * One parameter. The whole of decision 1 is here: the provenance comes off the
 * wire and is rendered, the editability is the wire's, and `doc`/`doc_full`
 * are the engine's docstring rather than anything this file invents.
 */
function field(spec, ctx, model, { segmented = false, accent = false } = {}) {
  const editable = spec.editable !== false;
  const cls = ['pf'];
  if (!editable) cls.push('ro');
  if (spec.source === 'inherited') cls.push('inh');
  if (spec.source === 'derived') cls.push('der');
  if (accent) cls.push('acc');

  const commit = (value) => ctx.edit(model.name, { [spec.name]: value });
  return h('div.' + cls.join('.'), help(spec),
    h('span.l', { text: spec.name }, spec.unit ? h('i', { text: spec.unit }) : null),
    editable ? input(spec, commit, segmented) : h('span.v', { text: display(spec) }),
    provenance(spec, ctx, model, editable));
}

/** The value as text, for a field the operator may not type into. */
function display(spec) {
  if (spec.value === null || spec.value === undefined) return fmt.ABSENT;
  if (spec.type === 'bool') return spec.value ? 'true' : 'false';
  return String(spec.value);
}

function input(spec, commit, segmented) {
  if (spec.type === 'bool') {
    return h('span.bool',
      ...[true, false].map((v) => h('span', {
        class: spec.value === v ? 'on' : '',
        onclick: () => commit(v),
      }, v ? 'true' : 'false')));
  }
  if (spec.type === 'enum' && segmented) {
    return h('span.seg', ...spec.choices.map((choice) => h('span.sego', {
      class: spec.value === choice ? 'on' : '',
      onclick: () => commit(choice),
    }, choice)));
  }
  if (spec.type === 'enum') {
    return h('select.v', { onchange: (e) => commit(e.target.value) },
      ...spec.choices.map((choice) => h('option', { value: choice, selected: spec.value === choice || null }, choice)));
  }
  const el = h('input.v', {
    type: 'text',
    value: spec.value === null || spec.value === undefined ? '' : String(spec.value),
    onchange: (e) => {
      const raw = e.target.value.trim();
      // An emptied nullable field is `PUT null`, which is the reset: the
      // service drops the edited layer for that one parameter and the value
      // falls back to whatever the layer below says. Not the same as 0.
      commit(raw === '' ? null : raw);
    },
  });
  return el;
}

/**
 * Where the value came from, and — when it came from an edit — the way back.
 * `ui-rules` §6: this is rendered, never re-derived, and the reset is a `PUT`
 * of `null` rather than a locally remembered previous value.
 */
function provenance(spec, ctx, model, editable) {
  const label = spec.source === 'default' ? '' : spec.source;
  return h('span.src',
    label ? h('span.tag.src-' + spec.source, { title: spec.detail || '', text: label }) : null,
    spec.source === 'edited' && editable
      ? h('button.link', {
        title: 'PUT null — drop the edit and fall back to the layer below',
        onclick: () => ctx.edit(model.name, { [spec.name]: null }),
      }, 'reset')
      : null);
}

/**
 * The one sentence, and the whole of it on hover. `title` is deliberate rather
 * than a custom tooltip: it works before any CSS loads, it is what a keyboard
 * user's browser will read out, and it cannot be clipped by an overflowing
 * card. The visible line is `doc`; `doc_full` is the rest.
 */
function help(spec) {
  const full = spec.doc_full && spec.doc_full !== spec.doc ? spec.doc_full : spec.doc;
  if (!spec.doc) return {};
  return { class: 'has-help', title: full, 'data-doc': spec.doc };
}

/**
 * Three parameters and a derived count in one row. The count comes from the
 * service's `estimate_text`, which a `PUT` returns afresh — the span is
 * rounded and never truncated, and re-implementing that here would eventually
 * round differently in the one case that mattered.
 */
function rangeRow(row, ctx, model) {
  const commit = (name) => (value) => ctx.edit(model.name, { [name]: value });
  const specs = [row.start, row.stop, row.step];
  return h('div.pf.range', { class: 'has-help', title: rangeHelp(row) },
    h('span.l', { text: row.label }),
    h('span.v.rng',
      input(row.start, commit(row.start.name), false),
      h('i', '→'),
      input(row.stop, commit(row.stop.name), false),
      h('i', 'step'),
      input(row.step, commit(row.step.name), false),
      row.points !== null ? h('i.pts', { text: `${row.points} pts` }) : null),
    rangeProvenance(specs, ctx, model));
}

/**
 * One row, three parameters, three provenances. The row shows the highest one
 * of the three — a range whose `step_v` was edited must not read `default`
 * because its `start_v` still is — and names which of them it belongs to, so
 * "edited" is never ambiguous about *what*. Reset drops every edit in the row
 * in one `PUT`, because the operator edited a range, not a field.
 */
function rangeProvenance(specs, ctx, model) {
  const rank = ['default', 'run.toml', 'last-used', 'edited', 'inherited', 'derived'];
  const top = specs.reduce((a, b) => (rank.indexOf(b.source) > rank.indexOf(a.source) ? b : a));
  if (top.source === 'default') return h('span.src');
  const edited = specs.filter((s) => s.source === 'edited');
  const which = specs.filter((s) => s.source === top.source).map((s) => s.name).join(', ');
  return h('span.src',
    h('span.tag.src-' + top.source, { title: `${which} — ${top.detail || top.source}`, text: top.source }),
    edited.length
      ? h('button.link', {
        title: `PUT null for ${edited.map((s) => s.name).join(', ')}`,
        onclick: () => ctx.edit(model.name, Object.fromEntries(edited.map((s) => [s.name, null]))),
      }, 'reset')
      : null);
}

function rangeHelp(row) {
  return [row.start, row.stop, row.step]
    .map((s) => `${s.name}${s.unit ? ' / ' + s.unit : ''} — ${s.doc_full || s.doc}`)
    .join('\n\n');
}

/**
 * The V_oc row. `led_v` is *in* it, not elsewhere on the card, because the
 * coupling invariant is that the DC level V_oc was measured at and the pulse
 * high level are one number — `ui-rules` §6 wants the screen to make nobody
 * want to type a different one.
 */
function vocRow(row, ctx, model) {
  const voc = row.voc;
  const bound = voc.source === 'derived' || voc.source === 'inherited';
  const missing = voc.value === null || voc.value === undefined;
  const commit = (value) => ctx.edit(model.name, { voc: value });
  return h('div.pf.voc', {
    class: missing && row.need ? 'miss' : bound ? 'inh' : '',
    title: voc.doc_full || voc.doc,
  },
    h('span.l', 'V_oc'),
    h('span.v',
      missing
        ? h('span.need', { text: row.need ? row.need.text : 'none' })
        : bound
          ? h('span', { text: fmt.volts(voc.value) }, h('i', { text: voc.detail || 'derived' }))
          : input(voc, commit, false),
      h('i.at', '@'),
      input(row.led, (v) => ctx.edit(model.name, { led_v: v }), false),
      h('i', 'V LED')),
    provenance(voc, ctx, model, !bound));
}

/**
 * The polarity control: one four-way choice, with the boolean it falls back to
 * revealed only where it is read. `auto` *is* `inverted_output`; at NORM, INV
 * or leave that boolean is never consulted, and showing it as an independent
 * field would offer a knob that does nothing.
 */
function polarityRow(row, ctx, model) {
  const mode = row.mode;
  const auto = String(mode.value).toLowerCase() === 'auto';
  const commit = (value) => ctx.edit(model.name, { output_polarity: value });
  return h('div.pf.acc.pol', {
    class: 'has-help',
    title: `${mode.name} / ${row.fallback.name} — ${mode.doc_full || mode.doc}\n\n`
      + `${row.fallback.name}: ${row.fallback.doc_full || row.fallback.doc}`,
  },
    h('span.l', '81150A output polarity'),
    h('span.v',
      h('span.seg', ...mode.choices.map((choice) => h('span.sego', {
        class: mode.value === choice ? 'on' : '',
        onclick: () => commit(choice),
      }, choice))),
      auto
        ? h('span.bool.acc', ...[true, false].map((v) => h('span', {
          class: row.fallback.value === v ? 'on' : '',
          onclick: () => ctx.edit(model.name, { inverted_output: v }),
        }, v ? 'INV' : 'NORM')))
        : null,
      h('i.eff', { text: `→ ${row.effective.text}` })),
    provenance(mode, ctx, model, true));
}

/**
 * The bench read-back. Not a parameter and not from `/modules`: what the
 * instruments say the light is doing, which on the `jv` card is what decides
 * whether the curve will be labelled dark, lit or unknown.
 */
function readback(row) {
  if (row.kind === 'shutter') {
    return h('div.pf.ro.rb', { class: row.inferred ? 'inferred' : '' },
      h('span.l', 'shutter'),
      h('span.v', { text: row.open === null || row.open === undefined ? fmt.ABSENT : row.open ? 'open' : 'shut' }));
  }
  const level = row.lit === null ? 'warn' : row.lit ? 'lit' : 'dark';
  return h('div.pf.ro.rb.' + level, {
    class: row.inferred ? 'inferred' : '',
    title: 'Read off the bench, not set by this module. It is what the curve '
      + 'will be labelled by: `as found dark`, `as found 1.020 V`, or '
      + '`as found unknown` when the bench cannot say whether light reaches '
      + 'the sample. All three of shutter, LED output and LED mode have to be '
      + 'readable for the answer to be anything but unknown.',
  },
    h('span.l', 'illumination'),
    h('span.v', { text: row.text },
      h('i', { text: row.lit === null ? 'nothing will be labelled dark' : `shutter ${row.open ? 'open' : 'shut'}` })));
}

// -- needs, verdicts and the fold ----------------------------------------
function needsList(model) {
  if (!model.needs.length) return null;
  // The V_oc need is drawn in its own row; anything else is a missing
  // instrument or a warning about one, and belongs under the form.
  const rest = model.needs.filter((n) => n.code !== 'voc');
  if (!rest.length) return null;
  return h('div.needs', rest.map((n) => h('div.warn1',
    h('span.ico', '⚠'), h('span', { text: n.text }))));
}

/**
 * The fold, as the service's own groups with computed counts. Not one button:
 * the `bace` card folds thirty-four, and thirty-four behind a single
 * disclosure is the comfortable screen `ui-rules` §1 warns about. Every count
 * is the length of what is in it — the artboard's typed "17" was stale the
 * day the SMU fields landed.
 */
function fold(model, ctx, open) {
  const total = foldCount(model);
  if (!total) return null;
  return h('div.fold',
    h('div.foldbar',
      h('span.cs', { text: `${total} more · run.toml` }),
      model.fold.map((group) => h('button.btng', {
        class: open.has(group.group) ? 'on' : '',
        onclick: () => ctx.toggle(model.name, group.group),
      }, `${group.group} · ${group.count}`))),
    model.fold.filter((g) => open.has(g.group)).map((g) => h('div.pr.folded',
      g.params.map((spec) => field(spec, ctx, model, {})))));
}

/**
 * Start is disabled by `invalid` **and** by `crit`, because
 * `pipeline.validate` is `valid = not any(level in ("invalid", "crit"))` and a
 * button that only blocked on `crit` would answer 422. They are not the same
 * failure and do not look the same: `crit` keeps the loudest treatment
 * `ui-rules` §3 reserves for hardware safety; `invalid` reads as "this tree is
 * not runnable yet", which is what it is.
 */
function startBlockers(verdicts, model) {
  return (verdicts || []).filter((v) => (v.level === 'crit' || v.level === 'invalid')
    && (!v.node_path || v.node_path === model.name || v.node_path.endsWith('/' + model.name)));
}

function blockedNote(blocked) {
  return h('div.blocked', blocked.map((c) => h('div.warn1.' + c.level,
    h('span.ico', c.level === 'crit' ? '⛔' : '⚠'),
    h('span.code', { text: c.code }),
    h('span', { text: c.text }))));
}
