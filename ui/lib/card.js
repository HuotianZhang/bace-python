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
  const wide = Boolean(ctx.hasResult && ctx.hasResult(model.name));
  const card = h('div.card.mod' + (wide ? '.wide' : ''), { dataset: { module: model.name } });

  // Blockers gate Start. A card with no Start (`light`: bench actions only)
  // has nothing for them to gate, and a verdict drawn under buttons it does
  // not concern reads as a warning about the buttons.
  const blocked = model.run.length ? startBlockers(ctx.checksFor(model.name), model) : [];
  const form = [
    h('div.pr', model.above.map((row) => renderRow(row, model, ctx))),
    model.readback ? readback(model.readback) : null,
    provSummary(model),
    needsList(model),
    fold(model, ctx, open),
    blocked.length ? blockedNote(blocked) : null,
  ];
  // The result panel is a **slot**, not content: `views/bench.js` fills it and
  // keys it on the data behind it, so a shot arriving does not rebuild the
  // card around it. That is the M2 rule with a chart in front of it — a
  // rebuilt element is a different element, and the operator's caret goes with
  // the old one (`docs/ui-plan.md` decision 5). Nothing here knows what a
  // chart is.
  fill(card, header(model, ctx, busy, blocked),
    wide ? h('div.cb.split', h('div.col', form), h('div.res')) : h('div.cb', form));
  return card;
}

// -- the header -----------------------------------------------------------
function header(model, ctx, busy, blocked) {
  const buttons = [];
  for (const action of model.actions) {
    // `led-off` is the one action on this card that makes the bench safe, and
    // it looked exactly like `Pulse`, which drives the lamp. §3's loudness is
    // for meaning: this one is not another verb in the row.
    const danger = action.action === 'led-off';
    buttons.push(h('button' + (danger ? '.btnd' : '.btns'), {
      disabled: busy || null,
      title: busy ? 'a run holds the worker; every action but park answers 409'
        : `POST /bench/actions/${action.action}`,
      onclick: () => ctx.act(model, action),
    }, action.label));
  }
  for (const [i, run] of model.run.entries()) {
    const last = i === model.run.length - 1;
    // Temperature settles in 14 min – 2 h (§5) and "must not look like a step
    // that runs". A red primary, the same button `Scan` uses, is exactly that.
    const slow = SLOW_RUN.has(model.name);
    const plain = slow || PLAIN_RUN.has(model.name);
    buttons.push(h(last && !plain ? 'button.btnp' : slow ? 'button.btnw' : 'button.btns', {
      disabled: busy || blocked.length > 0 || null,
      title: blocked.length ? blocked.map((c) => `${c.level}: ${c.text}`).join('\n') : model.estimate,
      onclick: () => ctx.run(model, run),
    }, run.label));
  }
  return h('div.ch',
    h('span.cn', { text: model.name }),
    model.status !== 'built' ? h('span.tag.nb', { text: model.status }) : null,
    h('span.cs', { text: subtitle(model) }),
    model.chips.map((text) => h('span.tag.cost', { text })),
    lastTag(model.last),
    h('span', { style: { flex: '1' } }),
    buttons);
}

/** A run whose iteration is minutes to hours, not seconds (`ui-rules` §5). */
const SLOW_RUN = new Set(['temperature']);

/** A run that reads and touches nothing: it does not want the primary. */
const PLAIN_RUN = new Set(['power']);

/**
 * What the card costs. A card with actions and no run does not cost a run at
 * all — and the service's `nothing to set` read as a contradiction over six
 * editable fields the buttons beside it send.
 */
function subtitle(model) {
  if (!model.run.length && model.actions.length) return 'acts now · no run, no files';
  return model.estimate;
}

/** Every parameter drawn above the fold, whatever row shape carried it. */
function aboveSpecs(model) {
  const out = [];
  for (const row of model.above) {
    if (row.kind === 'range') out.push(row.start, row.stop, row.step);
    else if (row.kind === 'voc') out.push(row.voc, row.led);
    else if (row.kind === 'polarity') out.push(row.mode, row.fallback);
    else if (row.spec) out.push(row.spec);
  }
  return out.filter(Boolean);
}

/**
 * The quiet provenance, once. Twenty rows saying `run.toml` is twenty
 * repetitions of "normal"; the count is the whole of what it was telling you,
 * and the rows that are a statement still carry their own tag.
 */
function provSummary(model) {
  const specs = aboveSpecs(model);
  if (!specs.length) return null;
  const counted = new Map();
  for (const spec of specs) {
    if (!QUIET_SOURCES.has(spec.source)) continue;
    counted.set(spec.source, (counted.get(spec.source) || 0) + 1);
  }
  if (!counted.size) return null;
  // Each quiet source with its count; the rows that are a statement (typed,
  // a loop's, measured) keep their own tag and are not counted as "the rest".
  const order = ['run.toml', 'last-used', 'default'];
  const text = order.filter((k) => counted.has(k))
    .map((k) => (k === 'default' ? `${counted.get(k)} default${counted.get(k) === 1 ? '' : 's'}` : `${counted.get(k)} from ${k}`))
    .join(' · ');
  return h('div.provsum', { text });
}

function lastTag(last) {
  if (!last) return null;
  const level = last.state === 'done' ? 'ok' : last.state === 'failed' ? 'bad' : '';
  return h('span.tag' + (level ? '.' + level : ''),
    { title: last.summary || '', text: `${last.state} ${fmt.clock(last.ts)}` });
}

// -- the rows -------------------------------------------------------------
/**
 * One row of a card, whichever composite it is. Exported because the pipeline
 * tab's node form is the same form: a module node is the bench's ParamSet
 * with the node's overrides on top (`docs/service-contract.md` §7 — "as on
 * the bench · only what differs is typed here"), so it goes through
 * `cardModel` and this, and the two screens cannot drift about which fields
 * matter or what a provenance looks like.
 */
export function renderRow(row, model, ctx) {
  switch (row.kind) {
    case 'range': return rangeRow(row, ctx, model);
    case 'voc': return vocRow(row, ctx, model);
    case 'polarity': return polarityRow(row, ctx, model);
    case 'segmented': return field(row.spec, ctx, model, { segmented: true, accent: row.accent, label: row.label });
    default: return field(row.spec, ctx, model, { accent: row.accent, label: row.label });
  }
}

/**
 * One parameter. The whole of decision 1 is here: the provenance comes off the
 * wire and is rendered, the editability is the wire's, and `doc`/`doc_full`
 * are the engine's docstring rather than anything this file invents.
 */
export function field(spec, ctx, model, { segmented = false, accent = false, label: text = null } = {}) {
  const editable = spec.editable !== false;
  const cls = ['pf'];
  if (!editable) cls.push('ro');
  if (spec.node) cls.push('node');
  if (spec.source === 'inherited') cls.push('inh');
  if (spec.source === 'derived') cls.push('der');
  if (accent) cls.push('acc');
  if (spec.inert) cls.push('inert');

  const commit = (value) => ctx.edit(model.name, { [spec.name]: value });
  return h('div.pw', h('div.' + cls.join('.'), help(spec),
    label(spec, ctx, model, text),
    editable ? input(spec, commit, segmented) : h('span.v', { text: display(spec) }),
    spec.inert
      ? h('span.src', h('span.tag.off', { title: spec.inert, text: 'not read' }))
      : provenance(spec, ctx, model, editable)), docLine(spec, ctx, model));
}

/** The value as text, for a field the operator may not type into. */
function display(spec) {
  if (spec.value === null || spec.value === undefined) return fmt.ABSENT;
  if (spec.type === 'bool') return spec.value ? 'true' : 'false';
  return typed(spec);
}

/**
 * The value as the operator should read it, which is not always as JSON sent
 * it. `ui-rules` §2: significant figures carry meaning, because they are what
 * the measurement resolves — so a level reads `1.000` where the rail beside it
 * reads `1.000 V`, and `0.4` and `1` do not sit in one column as `0.4` and
 * `1`. Volts to 3, V_oc to 4 (§2), everything else as it came: only the
 * quantities whose resolution is known are given a width.
 */
function typed(spec) {
  const v = spec.value;
  if (typeof v !== 'number' || !Number.isFinite(v)) return String(v);
  if (spec.name === 'voc') return v.toFixed(4);
  if (spec.unit === 'V') return v.toFixed(3);
  return String(v);
}

/**
 * The choosers are `<button>`s, not clickable spans.
 *
 * A span with an `onclick` cannot be tabbed to, has no role, and is invisible
 * to a screen reader — and these are the polarity, the shutter and the LED
 * mode, which is not a set of controls to leave mouse-only. `aria-pressed`
 * carries the selection, which is what the dark chip says visually.
 */
function option(label, on, commit) {
  return h('button.opt', {
    type: 'button',
    class: on ? 'on' : '',
    'aria-pressed': on ? 'true' : 'false',
    onclick: () => commit(),
  }, label);
}

function input(spec, commit, segmented, { blank = false } = {}) {
  // **Not editable is not an input, wherever the value is drawn.** `field`
  // branches on this too, but the composite rows — the range, the V_oc row's
  // `led_v`, the polarity pair — reach `input` directly, and the pipeline
  // tab's node form is where that matters: `led_v` inside an illumination
  // loop resolves `inherited`, which `params.LOCKED` makes non-editable on
  // the wire whatever the spec says, and `ParamSet.set_edited` refuses. An
  // input there offered an override that could only ever end in
  // `tree.owned-param` refusing the tree. `ui-rules` §6: an inherited value
  // reads as inherited, showing the value it will get.
  if (spec.editable === false) return h('span.v', { text: display(spec) });
  // Not read in this configuration (`fields.inertReason`): shown, not typed
  // into. The value stays so it is there when the setting that reads it is
  // turned back on; the reason is on the row's tag and on hover here.
  if (spec.inert) return h('span.v.off', { text: display(spec), title: spec.inert });
  if (spec.type === 'bool') {
    return h('span.bool', { role: 'group', 'aria-label': spec.name },
      ...[true, false].map((v) => option(v ? 'true' : 'false', spec.value === v, () => commit(v))));
  }
  if (spec.type === 'enum' && segmented) {
    return h('span.seg', { role: 'group', 'aria-label': spec.name },
      ...spec.choices.map((choice) => option(choice, spec.value === choice, () => commit(choice))));
  }
  if (spec.type === 'enum') {
    return h('select.v', { 'aria-label': spec.name, onchange: (e) => commit(e.target.value) },
      ...spec.choices.map((choice) => h('option', { value: choice, selected: spec.value === choice || null }, choice)));
  }
  const el = h('input.v', {
    type: 'text',
    class: blank ? 'blank' : '',
    'aria-label': spec.name,
    value: spec.value === null || spec.value === undefined ? '' : typed(spec),
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
/**
 * The sources that are not a statement. `run.toml` is where most values come
 * from, so a boxed tag on every row was twenty repetitions of "normal" — and
 * the answer of making it nearly the paper's own colour left an illegible tag
 * still holding a column. It is counted once per card instead
 * (`provSummary`), and only the three that *say* something keep a tag.
 */
const QUIET_SOURCES = new Set(['default', 'run.toml', 'last-used']);

function provenance(spec, ctx, model, editable) {
  // A node's own override carries no tag: the value in ink and the ↺ beside
  // it are the statement, and `edited` next to them said it a third time.
  const label = QUIET_SOURCES.has(spec.source) || spec.node ? '' : spec.source;
  // Who can take the value back. On a bench card that is whoever edited it,
  // and `source === 'edited'` says so. On a pipeline node form it is not:
  // the node's overrides and the bench card's edits **both** resolve as
  // `edited` (`ParamSet.update_layer(Source.EDITED, node.params, "pipeline
  // node")` merges into the same layer), so a reset offered on every edited
  // row would, for a value the bench typed and this node did not, be a
  // button that removes a key the node never had and changes nothing. The
  // node form sets `spec.node` — is this override typed *here* — and that is
  // the authority, because the tree is the client's own and needs no string
  // parsed out of `detail` to know what is in it.
  //
  // **And the reset is not gated on `editable`.** Removing a key the node
  // types is not typing into it: a module that types `led_v` inside an
  // illumination loop resolves `inherited` — non-editable — *and* is refused
  // by `tree.owned-param`, so gating the two together left the operator with
  // an override that blocks Start and no way to drop it but deleting the
  // node. The value stays read-only; the way out stays open.
  const resettable = spec.node === undefined ? (spec.source === 'edited' && editable) : spec.node;
  return h('span.src',
    label ? h('span.tag.src-' + spec.source, { title: spec.detail || '', text: label }) : null,
    resettable
      ? h('button.link.undo', {
        title: spec.node
          ? 'back to the bench — drop this node’s override, the value falls back to the module as it stands on the bench'
          : 'PUT null — drop the edit and fall back to the layer below',
        'aria-label': spec.node ? 'back to the bench' : 'reset',
        onclick: () => ctx.edit(model.name, { [spec.name]: null }),
      }, '↺')
      : null);
}

/**
 * The one sentence, and the whole of it — reachable, which it was not.
 *
 * `title` alone was the bug: it lives on a `div` with no `tabindex`, so the
 * explanation of `smu_nplc` was mouse-only, and `ui-rules` §1 asks for a field
 * that *can be expanded*, not one that can be hovered. So the name is a
 * `<button>` — tab to it, press it, and `doc` (then `doc_full`) opens under
 * the row. The `title` stays for the pointer.
 */
function help(spec) {
  const full = spec.doc_full && spec.doc_full !== spec.doc ? spec.doc_full : spec.doc;
  if (!spec.doc) return {};
  return { class: 'has-help', title: full, 'data-doc': spec.doc };
}

/** The name, as a label or as the button that opens the explanation. */
function label(spec, ctx, model, text) {
  const name = text || spec.name;
  if (!spec.doc) {
    return h('span.l', { text: name }, spec.unit ? h('i', { text: spec.unit }) : null);
  }
  const key = `${model.name}:${spec.name}`;
  const open = ctx.docOpen ? ctx.docOpen(key) : false;
  return h('button.l.lh', {
    type: 'button',
    'aria-expanded': open ? 'true' : 'false',
    title: spec.doc_full && spec.doc_full !== spec.doc ? spec.doc_full : spec.doc,
    onclick: () => ctx.toggleDoc && ctx.toggleDoc(key),
  }, name, spec.unit ? h('i', { text: spec.unit }) : null);
}

/** The explanation itself, under the row it belongs to. */
function docLine(spec, ctx, model) {
  const key = `${model.name}:${spec.name}`;
  if (!spec.doc || !ctx.docOpen || !ctx.docOpen(key)) return null;
  const full = spec.doc_full && spec.doc_full !== spec.doc ? spec.doc_full : null;
  return h('div.doc', h('span', { text: spec.doc }), full ? h('span.dfull', { text: full }) : null);
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
  // `start == stop` is a *repeat*, not a sweep — "Q at V_oc twenty times" — and
  // `ui-rules` §4 says the switch must be visible rather than silent: the chart
  // beside this row changes from Q(axis) to Q per loop on exactly this test.
  // Only on the swept axis (`row.accent`): a jv_bace's LED levels from 1.000
  // to 1.000 is one level, not a repeat, and `n_loops` says nothing about it.
  const repeat = Boolean(row.accent) && row.start.value !== null && Number(row.start.value) === Number(row.stop.value);
  return h('div.pw',
    h('div.pf.range' + (row.accent ? '.acc' : ''), { class: 'has-help', title: rangeHelp(row) },
      h('span.l', { text: row.label }),
      h('span.v.rng',
        input(row.start, commit(row.start.name), false),
        h('i', '→'),
        input(row.stop, commit(row.stop.name), false),
        h('i', 'step'),
        input(row.step, commit(row.step.name), false),
        repeat ? h('span.tag.rep', 'repeat')
          : row.points !== null ? h('i.pts', { text: `${row.points} pts` }) : null),
      rangeProvenance(specs, ctx, model)),
    repeat ? h('div.note1', { text: 'start = stop: one point, measured n_loops times — Q per loop, not Q(axis)' }) : null);
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
  if (QUIET_SOURCES.has(top.source)) return h('span.src');
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
  return h('div.pw',
    h('div.pf.voc', {
      class: missing && row.need ? 'miss' : bound ? 'inh' : '',
      title: voc.doc_full || voc.doc,
    },
      h('span.l', 'V_oc'),
      h('span.v',
        bound
          ? h('span', { text: fmt.volts(voc.value) })
          // Missing shows the field, not a sentence where the field should be:
          // the typed V_oc is the documented last resort and the *only* route
          // on a bench with no SourceMeter, so it has to look like something
          // you can type in. The reason goes on its own line below, where it
          // cannot push `@ led_v` onto a second row.
          : input(voc, commit, false, { blank: missing }),
        h('i.at', '@'),
        input(row.led, (v) => ctx.edit(model.name, { led_v: v }), false),
        h('i', 'V LED'),
        // Where the V_oc came from -- "jv_bace (this session, 11:49)" -- is a
        // sentence, and on the value's own line it ran under the tag. Its own
        // line, after the level it was measured at.
        bound ? h('i.det', { text: voc.detail || 'derived' }) : null),
      provenance(voc, ctx, model, !bound)),
    missing && row.need ? h('div.need1', h('span.ico', '⚠'), h('span', { text: row.need.text })) : null);
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
  return h('div.pf.pol', {
    class: 'has-help',
    title: `${mode.name} / ${row.fallback.name} — ${mode.doc_full || mode.doc}\n\n`
      + `${row.fallback.name}: ${row.fallback.doc_full || row.fallback.doc}`,
  },
    h('span.l', '81150A output polarity'),
    h('span.v',
      h('span.seg', { role: 'group', 'aria-label': mode.name },
        ...mode.choices.map((choice) => option(choice, mode.value === choice, () => commit(choice)))),
      auto
        ? h('span.bool', { role: 'group', 'aria-label': row.fallback.name },
          ...[true, false].map((v) => option(v ? 'INV' : 'NORM', row.fallback.value === v,
            () => ctx.edit(model.name, { inverted_output: v }))))
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
export function fold(model, ctx, open) {
  const total = foldCount(model);
  if (!total) return null;
  return h('div.fold',
    h('div.foldbar',
      // On a node form the fold *is* the statement: everything in it is the
      // module as it stands on the bench, and only what is above it differs.
      h('span.cs', { text: `${total} more · ${model.form === 'node' ? 'same as bench' : 'run.toml'}` }),
      model.fold.map((group) => h('button.btng', {
        class: open.has(group.group) ? 'on' : '',
        title: group.inert ? group.params[0].inert : '',
        onclick: () => ctx.toggle(model.name, group.group),
      }, `${group.group} · ${group.count}${group.inert ? ' · not read' : ''}`))),
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
function startBlockers(checks, model) {
  // `pipeline.validate` is `valid = not any(level in ("invalid", "crit"))`,
  // and both `session.submit` and the Start re-check refuse on that same
  // pair — so a button that blocked on only one of them would offer a click
  // that answers 422. Every check of this module's own one-node tree counts;
  // there is no other tree in it to filter out.
  return (checks || []).filter((v) => v.level === 'crit' || v.level === 'invalid');
}

function blockedNote(blocked) {
  return h('div.blocked', blocked.map((c) => h('div.warn1.' + c.level,
    h('span.ico', c.level === 'crit' ? '⛔' : '⚠'),
    h('span.code', { text: c.code }),
    h('span', { text: c.text }))));
}
