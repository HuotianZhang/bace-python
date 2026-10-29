// Module cards share one parameter field component. The service supplies `value`,
// `source`, `detail`, `editable` and estimates; rendering these together preserves
// provenance and editability (`docs/ui-rules.md` §6). `doc` explains each field and
// `doc_full` remains accessible by pointer and keyboard (§1). Edits render the
// service's answer so values, counts and estimates stay consistent.

import { h, fill } from './dom.js';
import * as fmt from './format.js';
import { cardModel, foldCount, groupLabel } from './fields.js';
import { icon } from './icons.js';

/**
 * The card renders one module. `ctx` supplies `api`, `store` and `onError`; the
 * caller keeps `open` so redraws preserve disclosure state.
 */
export function moduleCard(entry, ctx, open) {
  const model = cardModel(entry, { bench: ctx.bench });
  const busy = Boolean(ctx.busy);
  const wide = Boolean(ctx.hasResult && ctx.hasResult(model.name));
  const card = h('div.card.mod' + (wide ? '.wide' : ''), { dataset: { module: model.name } });

  // Blockers gate Start. The `light` card has only bench actions, so showing run
  // blockers under those actions would imply they apply to them.
  const blocked = model.run.length ? startBlockers(ctx.checksFor(model.name), model) : [];
  const form = [
    h('div.pr', model.above.map((row) => renderRow(row, model, ctx))),
    model.readback ? readback(model.readback) : null,
    provSummary(model),
    needsList(model),
    fold(model, ctx, open),
    blocked.length ? blockedNote(blocked) : null,
  ];
  // `ui/views/bench.js` fills and keys the result slot independently. A shot must not
  // rebuild the surrounding form and discard the operator's caret.
  fill(card, header(model, ctx, busy, blocked),
    wide ? h('div.cb.split', h('div.col', form), h('div.res')) : h('div.cb', form));
  return card;
}

// -- the header -----------------------------------------------------------
function header(model, ctx, busy, blocked) {
  const buttons = [];
  for (const action of model.actions) {
    // `led-off` makes the bench safe, so its visual priority must distinguish it from
    // Pulse, which drives the lamp (`docs/ui-rules.md` §3).
    const danger = action.action === 'led-off';
    buttons.push(h('button' + (danger ? '.btnd' : '.btns'), {
      disabled: busy || null,
      title: busy ? BUSY_TITLE : `${action.label}. Acts on the bench now.`,
      onclick: () => ctx.act(model, action),
    }, action.label));
  }
  for (const [i, run] of model.run.entries()) {
    const last = i === model.run.length - 1;
    // Temperature settling takes 14 min to 2 h on this bench (`docs/ui-rules.md` §5).
    // It needs a different treatment from the primary Scan action so settling does
    // not appear to be an ordinary measurement step.
    const slow = SLOW_RUN.has(model.name);
    const plain = slow || PLAIN_RUN.has(model.name);
    buttons.push(h(last && !plain ? 'button.btnp' : slow ? 'button.btnw' : 'button.btns', {
      disabled: busy || blocked.length > 0 || null,
      title: blocked.length ? blocked.map((c) => `${c.level}: ${c.text}`).join('\n')
        : busy ? BUSY_TITLE : model.estimate,
      onclick: () => ctx.run(model, run),
    }, run.label));
  }
  return h('div.ch',
    h('span.cn', { text: model.name }),
    model.status !== 'built' ? h('span.tag.nb', { text: model.status }) : null,
    subtitle(model),
    model.chips.map((text) => h('span.tag.cost', { text })),
    lastTag(model.last),
    h('span', { style: { flex: '1' } }),
    buttons);
}

/**
 * The predicate identifies runs whose iterations take minutes to hours
 * (`docs/ui-rules.md` §5).
 */
const SLOW_RUN = new Set(['temperature']);

/** A read-only run does not need the primary action treatment. */
const PLAIN_RUN = new Set(['power']);

const BUSY_TITLE = 'A run holds the bench. Only Park acts until it ends.';

/**
 * The estimate describes run cost: the time in ink, its count in grey, the shot
 * time and its source in the title. Action-only cards omit it because a no-op run
 * estimate would contradict the editable values sent by their actions. Without the
 * structured estimate the service's sentence stands.
 */
function subtitle(model) {
  if (!model.run.length && model.actions.length) return h('span.cs', { text: 'Acts now' });
  const c = model.cost;
  const count = c && Number.isInteger(c.points)
    ? [Number.isInteger(c.loops) ? fmt.plural(c.loops, 'loop') : null,
      Number.isInteger(c.curves) ? fmt.plural(c.curves, 'curve') : null,
      fmt.plural(c.points, 'pt')].filter(Boolean).join(' × ')
    : null;
  if (!count || !(c.seconds > 0)) return h('span.cs', { text: model.estimate });
  const shot = typeof c.shot_s === 'number'
    ? `One shot ${fmt.duration(c.shot_s)}, ${c.shot_source === 'journal' ? 'measured on this bench' : 'default'}.`
    : '';
  return h('span.cs', { title: [model.estimate, shot].filter(Boolean).join('\n') },
    h('span.cost', { text: `≈ ${fmt.duration(c.seconds)}` }), ' ', count);
}

/** The helper lists parameters above the fold, including composite rows. */
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
 * Common provenance appears once per card. Individual tags remain for values whose
 * source needs attention, avoiding repeated default-layer labels.
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
  // Counts summarize common sources; edited, inherited and measured values keep
  // individual tags.
  const order = ['run.toml', 'last-used', 'default'];
  const text = order.filter((k) => counted.has(k))
    .map((k) => (k === 'default' ? fmt.plural(counted.get(k), 'default') : `${counted.get(k)} from ${k}`))
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
 * The row renders a shared card composite. Pipeline node forms use the bench
 * parameters with node overrides (`docs/service-contract.md` §7). Sharing `cardModel`
 * and this renderer keeps field selection and provenance consistent.
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
 * The field renders one parameter from the service entry. Provenance, editability and
 * `doc`/`doc_full` come from the service rather than local inference.
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
  // `input` handles read-only and inert values because composite rows call it
  // directly. All paths therefore use the same boolean control.
  return h('div.pw', h('div.' + cls.join('.'), help(spec),
    label(spec, ctx, model, text),
    input(spec, commit, segmented),
    spec.inert
      // The blank glyph accompanies words explaining an absent value
      // (`docs/ui-rules.md` §2).
      ? h('span.src', h('span.tag.off', { title: spec.inert }, icon('blank'), h('span', { text: 'not read' })))
      : provenance(spec, ctx, model, editable)), docLine(spec, ctx, model));
}

/** The renderer displays read-only values as text. */
function display(spec) {
  if (spec.value === null || spec.value === undefined) return fmt.ABSENT;
  return typed(spec);
}

/**
 * The displayed value uses measurement precision (`docs/ui-rules.md` §2): voltages
 * have three decimals and V_oc has four. Other quantities retain their supplied
 * precision. A field omits the unit already shown beside it.
 */
function typed(spec) {
  const v = spec.value;
  if (typeof v !== 'number' || !Number.isFinite(v)) return String(v);
  if (spec.name === 'voc') return v.toFixed(4);
  if (spec.unit === 'V') return v.toFixed(3);
  return String(v);
}

/**
 * Choosers use buttons for keyboard focus and screen-reader roles. Clickable spans
 * would make polarity, shutter and LED mode mouse-only. `aria-pressed` conveys the
 * visual selection.
 */
function option(label, on, commit) {
  return h('button.opt', {
    type: 'button',
    class: on ? 'on' : '',
    'aria-pressed': on ? 'true' : 'false',
    onclick: () => commit(),
  }, label);
}

/**
 * A boolean uses a checkbox (`docs/ui-rules.md` §14). The row supplies its visible
 * name and `aria-label` supplies the same name to screen readers. `null` means the
 * service has no value, so an empty box must not imply `false`. The `indeterminate`
 * property is set after element creation because it is not an HTML attribute.
 */
function checkbox(spec, commit, { disabled = false, title = null } = {}) {
  const el = h('input.cbx', {
    type: 'checkbox',
    'aria-label': spec.name,
    checked: spec.value === true,
    disabled: disabled || null,
    title,
    onchange: commit ? (e) => commit(e.target.checked) : null,
  });
  el.indeterminate = spec.value === null || spec.value === undefined;
  return el;
}

function input(spec, commit, segmented, { blank = false } = {}) {
  // Read-only values must not offer inputs, including in composite rows. An
  // illumination loop owns `led_v`; a node override is non-editable and fails
  // validation. Inherited values show their resolved value (`docs/ui-rules.md` §6). A
  // read-only boolean remains a disabled checkbox so its on/off meaning stays
  // visible.
  if (spec.editable === false) {
    return spec.type === 'bool'
      ? checkbox(spec, null, { disabled: true })
      : h('span.v', { text: display(spec) });
  }
  // An inert field retains its value for configurations that read it. The row and
  // hover text explain why it cannot be edited in this configuration.
  if (spec.inert) {
    return spec.type === 'bool'
      ? checkbox(spec, null, { disabled: true, title: spec.inert })
      : h('span.v.off', { text: display(spec), title: spec.inert });
  }
  if (spec.type === 'bool') return checkbox(spec, commit);
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
      // An empty nullable field sends `PUT null`, removing this parameter's edited
      // layer. It restores the lower layer rather than setting zero.
      commit(raw === '' ? null : raw);
    },
  });
  return el;
}

/**
 * The provenance tag shows the service's source and an available reset. Reset sends
 * `PUT null` rather than restoring a locally remembered value (`docs/ui-rules.md`
 * §6).
 */
/**
 * `provSummary` counts common sources once per card. Individual tags are reserved for
 * sources that need attention, avoiding repetitive labels in every row.
 */
const QUIET_SOURCES = new Set(['default', 'run.toml', 'last-used']);

function provenance(spec, ctx, model, editable) {
  // A node override uses its value and reset control as its indication; an edited tag
  // would repeat that information.
  const label = QUIET_SOURCES.has(spec.source) || spec.node ? '' : spec.source;
  // Reset ownership differs between bench cards and node forms. Both bench edits and
  // node overrides resolve as `edited`, so that source alone cannot identify a
  // node-owned key. `spec.node` identifies keys in the client's tree without parsing
  // `detail`. Reset remains available even when the value is non-editable: an illegal
  // `led_v` override inside an illumination loop blocks Start, and removing it must
  // not require deleting the node. Read-only values still cannot be edited.
  const resettable = spec.node === undefined ? (spec.source === 'edited' && editable) : spec.node;
  return h('span.src',
    label ? h('span.tag.src-' + spec.source, { title: spec.detail || '', text: label }) : null,
    resettable
      ? h('button.link.undo', {
        title: spec.node
          ? 'Drop this override; the bench value applies.'
          : 'Drop the edit; the layer below applies.',
        'aria-label': spec.node ? 'back to the bench' : 'reset',
        onclick: () => ctx.edit(model.name, { [spec.name]: null }),
      }, '↺')
      : null);
}

/**
 * The tooltip names the parameter by its code name, then says what it does
 * (`docs/ui-rules.md` §1). The label button opens the same text as the help line,
 * so keyboard users reach it too.
 */
function tooltip(spec) {
  const full = spec.doc_full && spec.doc_full !== spec.doc ? spec.doc_full : spec.doc;
  return full ? `${spec.name}\n${full}` : spec.name;
}

function help(spec) {
  if (!spec.doc) return {};
  return { class: 'has-help', title: tooltip(spec), 'data-doc': spec.doc };
}

/** The unit after a name, unless the name already ends in it (`settle_s`, `pixel_area_cm2`). */
function unitTag(spec, name) {
  if (!spec.unit) return null;
  const suffix = '_' + spec.unit.toLowerCase().replace('²', '2').replace('µ', 'u');
  return String(name).toLowerCase().endsWith(suffix) ? null : h('i', { text: spec.unit });
}

/** The label, or the code name where the service sends none, as text or a help button. */
function label(spec, ctx, model, text) {
  const name = text || spec.label || spec.name;
  if (!spec.doc) {
    return h('span.l', { text: name, title: spec.name }, unitTag(spec, name));
  }
  const key = `${model.name}:${spec.name}`;
  const open = ctx.docOpen ? ctx.docOpen(key) : false;
  return h('button.l.lh', {
    type: 'button',
    'aria-expanded': open ? 'true' : 'false',
    title: tooltip(spec),
    onclick: () => ctx.toggleDoc && ctx.toggleDoc(key),
  }, name, unitTag(spec, name));
}

/** The help line under its field: the code name and run.toml key, then the explanation. */
function docLine(spec, ctx, model) {
  const key = `${model.name}:${spec.name}`;
  if (!spec.doc || !ctx.docOpen || !ctx.docOpen(key)) return null;
  const full = spec.doc_full && spec.doc_full !== spec.doc ? spec.doc_full : null;
  const recipe = (spec.recipe || []).filter((k) => k !== spec.name);
  return h('div.doc',
    h('span.dcode', { text: [spec.name, ...recipe].join('  ') }),
    h('span', { text: spec.doc }), full ? h('span.dfull', { text: full }) : null);
}

/**
 * The range row combines three parameters and a service-derived count.
 * `estimate_text` follows each edit; local span arithmetic could round differently
 * from the service.
 */
function rangeRow(row, ctx, model) {
  const commit = (name) => (value) => ctx.edit(model.name, { [name]: value });
  const specs = [row.start, row.stop, row.step];
  // On the swept axis, `start == stop` denotes a repeat and switches the chart from
  // Q(axis) to Q per loop (`docs/ui-rules.md` §4). This indication requires
  // `row.accent`: equal LED limits in `jv_bace` mean one level, not repeated shots.
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
        repeat ? h('span.tag.rep', { title: 'start = stop: one point, repeated n_loops times. The chart shows Q per loop.', tabindex: '0' }, 'repeat')
          : row.points !== null ? h('i.pts', { text: fmt.plural(row.points, 'pt') }) : null),
      rangeProvenance(specs, ctx, model)));
}

/**
 * A composite range shows the highest parameter source and identifies the parameter.
 * An edited `step_v` must remain visible even if `start_v` is default. Reset clears
 * every edit in one `PUT` because the row represents one range.
 */
function rangeProvenance(specs, ctx, model) {
  const rank = ['default', 'run.toml', 'last-used', 'edited', 'inherited', 'derived'];
  const top = specs.reduce((a, b) => (rank.indexOf(b.source) > rank.indexOf(a.source) ? b : a));
  if (QUIET_SOURCES.has(top.source)) return h('span.src');
  const edited = specs.filter((s) => s.source === 'edited');
  const which = specs.filter((s) => s.source === top.source).map((s) => s.name).join(', ');
  return h('span.src',
    h('span.tag.src-' + top.source, { title: `${top.detail || top.source}: ${which}`, text: top.source }),
    edited.length
      ? h('button.link', {
        title: `Drop the edits to ${edited.map((s) => s.name).join(', ')}; the layer below applies.`,
        onclick: () => ctx.edit(model.name, Object.fromEntries(edited.map((s) => [s.name, null]))),
      }, 'Reset')
      : null);
}

function rangeHelp(row) {
  return [row.start, row.stop, row.step]
    .map((s) => `${s.name}${s.unit ? ' / ' + s.unit : ''}: ${s.doc_full || s.doc}`)
    .join('\n\n');
}

/**
 * The V_oc row includes `led_v` because the DC level used to measure V_oc must equal
 * the transient pulse high level (`docs/ui-rules.md` §6).
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
          // A typed V_oc is the fallback and the only route without a source-meter.
          // An absent value must therefore retain an input. Its explanation occupies
          // a separate line so `led_v` stays with V_oc.
          : input(voc, commit, false, { blank: missing }),
        h('i.at', '@'),
        input(row.led, (v) => ctx.edit(model.name, { led_v: v }), false),
        h('i', 'V LED'),
        // V_oc provenance occupies a separate line after its LED level to avoid
        // colliding with the source tag.
        bound ? h('i.det', { text: voc.detail || 'derived' }) : null),
      provenance(voc, ctx, model, !bound)),
    missing && row.need ? h('div.need1', icon('warn', { cls: 'ico' }), h('span', { text: row.need.text })) : null);
}

/**
 * The polarity control has four choices. Only `auto` consults `inverted_output`;
 * showing that fallback for NORM, INV or leave would offer an ineffective control.
 */
function polarityRow(row, ctx, model) {
  const mode = row.mode;
  const auto = String(mode.value).toLowerCase() === 'auto';
  const commit = (value) => ctx.edit(model.name, { output_polarity: value });
  return h('div.pf.pol', {
    class: 'has-help',
    title: `${mode.name}: ${mode.doc_full || mode.doc}\n\n`
      + `${row.fallback.name}: ${row.fallback.doc_full || row.fallback.doc}`,
  },
    h('span.l', mode.label || 'Output polarity'),
    h('span.v',
      h('span.seg', { role: 'group', 'aria-label': mode.name },
        ...mode.choices.map((choice) => option(choice, mode.value === choice, () => commit(choice)))),
      // Although `inverted_output` is boolean on the wire, NORM and INV match the
      // instrument's named positions. Those labels convey polarity more clearly than
      // a checkbox (`docs/ui-rules.md` §14).
      auto
        ? h('span.seg', { role: 'group', 'aria-label': row.fallback.name },
          ...[true, false].map((v) => option(v ? 'INV' : 'NORM', row.fallback.value === v,
            () => ctx.edit(model.name, { inverted_output: v }))))
        : null,
      h('i.eff', { text: `→ ${row.effective.text}` })),
    provenance(mode, ctx, model, true));
}

/**
 * The light read-back reports instrument state independently of `/modules`. The `jv`
 * card uses it to label illumination as dark, light or unknown.
 */
function readback(row) {
  if (row.kind === 'shutter') {
    return h('div.pf.ro.rb', { class: row.inferred ? 'inferred' : '' },
      h('span.l', 'Shutter'),
      h('span.v', { text: row.open === null || row.open === undefined ? fmt.ABSENT : row.open ? 'open' : 'shut' }));
  }
  const level = row.lit === null ? 'warn' : row.lit ? 'lit' : 'dark';
  return h('div.pf.ro.rb.' + level, {
    class: row.inferred ? 'inferred' : '',
    title: 'Read from the bench, not set here. The curve is labelled as found: '
      + 'dark, 1.020 V or unknown. Unknown unless shutter, LED output and LED mode all read back.',
  },
    h('span.l', 'Illumination'),
    h('span.v', { text: row.text },
      h('i', { text: row.lit === null ? 'labelled unknown' : `shutter ${row.open ? 'open' : 'shut'}` })));
}

// -- needs, verdicts and the fold ----------------------------------------
function needsList(model) {
  if (!model.needs.length) return null;
  // The V_oc need is drawn in its own row; anything else is a missing
  // instrument or a warning about one, and belongs under the form.
  const rest = model.needs.filter((n) => n.code !== 'voc');
  if (!rest.length) return null;
  return h('div.needs', rest.map((n) => h('div.warn1',
    icon('warn', { cls: 'ico' }), h('span', { text: n.text }))));
}

/**
 * The fold uses service groups and computed counts. A single disclosure for all
 * `bace` parameters would hide too much (`docs/ui-rules.md` §1); counts must reflect
 * the actual fields.
 */
export function fold(model, ctx, open) {
  const total = foldCount(model);
  if (!total) return null;
  return h('div.fold',
    h('div.foldbar',
      // On a node form, the fold contains unchanged bench values; the fields above it
      // show differences.
      h('span.cs', { text: model.form === 'node' ? `${total} as on the bench` : `${total} more` }),
      model.fold.map((group) => h('button.btng', {
        class: open.has(group.group) ? 'on' : '',
        title: group.inert ? group.params[0].inert : group.group,
        onclick: () => ctx.toggle(model.name, group.group),
      }, `${groupLabel(group.group)} ${group.count}`,
      group.inert ? h('span.tag.off', { text: 'not read' }) : null))),
    model.fold.filter((g) => open.has(g.group)).map((g) => h('div.pr.folded',
      g.params.map((spec) => field(spec, ctx, model, {})))));
}

/**
 * Start is disabled for both `invalid` and `crit` checks, matching service validation
 * (`docs/service-contract.md` §7). Hardware-critical checks retain the strongest
 * visual treatment; invalid parameters indicate an unrunnable tree
 * (`docs/ui-rules.md` §3).
 */
function startBlockers(checks, model) {
  // Both `invalid` and `crit` checks block submission and the Start re-check
  // (`docs/service-contract.md` §7). Every check belongs to this module's one-node
  // tree, so none can be filtered out.
  return (checks || []).filter((v) => v.level === 'crit' || v.level === 'invalid');
}

function blockedNote(blocked) {
  return h('div.blocked', blocked.map((c) => h('div.warn1.' + c.level,
    icon(c.level === 'crit' ? 'crit' : 'warn', { cls: 'ico' }),
    h('span', { text: c.text }),
    h('span.code', { text: c.code }))));
}
