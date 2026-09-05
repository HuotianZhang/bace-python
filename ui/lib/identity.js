// What is mounted — the session's `[sample]` block, and the way to type it.
//
// The one finding in `docs/ux-screening.md` whose cost was permanent. Until
// `PUT /session/sample` existed the block came only from `run.toml`, which is
// empty on a fresh checkout, and the only way to fill it was to edit the file
// and restart the process that owns every instrument. So an operator who
// mounted a device and started measuring filed every run of that session under
// a name with no device in it — and the folder name *is* the record
// (`docs/naming-plan.md`), which cannot be repaired afterwards without
// renaming folders that other files point at. The top bar knew: it said
// `no sample named`, and offered nothing.
//
// Three things this has to get right, and none of them is the input box:
//
//   * **Show the consequence, not the field.** Three of these five go into
//     every folder name this session writes, in this order, and a fourth goes
//     in as a slug. So the panel draws the stem the next run will be filed
//     under — `s4_PTQ10IT4F_pxa_…` — and the operator is typing a filename,
//     which is what they are actually doing.
//   * **Warn where the archive has already been bitten.** `naming-plan.md`
//     §"Fields collide" carries both: `sample = "a_b"` forges a field
//     boundary, and `sample = "s4 pixel a"` puts a space in a directory name
//     — the 2026-09-01 bug `slug()` was written to stop and which this path
//     still does not stop. The console is the new front door for it, so it
//     says so at the field, before the run.
//   * **`temperature_k` is not offered.** It is a legal `[sample]` key and the
//     route takes it, and `run.toml` says in a comment dated 2026-09-04 why
//     nobody should type it: every recipe said 290, and a run at 220 K was
//     filed as "290 K, typed". A field here would rebuild that defect with a
//     nicer surface. The panel says the number is read from the 331 instead.
//
// `identityModel` is pure and `ui/tests/identity.test.mjs` holds it down; the
// DOM is beside it, the same split the rail has.

import { h, keyed } from './dom.js';

/**
 * The five the console offers, in the order the folder name uses them.
 *
 * `doc` is the one sentence `ui-rules` §1 requires of every field — which
 * instrument or file it touches, and what a wrong value produces. `in_name`
 * is whether it reaches the folder name, because that is the difference
 * between a typo you can fix later and one you cannot.
 */
export const IDENTITY_FIELDS = [
  { name: 'sample', in_name: true, placeholder: 's4',
    doc: 'the device. First field of every folder name this session writes.' },
  { name: 'material', in_name: true, placeholder: 'PTQ10IT4F',
    doc: 'what it is made of, as the archive writes it — the second field of the name.' },
  { name: 'pixel', in_name: true, placeholder: 'pxa',
    doc: 'which pixel is under the mask — the third field of the name.' },
  { name: 'operator', in_name: false, placeholder: '',
    doc: 'who is at the bench. Metadata only: it is in the HDF5 and the journal, not in any folder name.' },
  { name: 'comment', in_name: 'slug', placeholder: '',
    doc: 'one sentence about this session. A slug of it goes into the folder name; the file keeps it verbatim.' },
];

/**
 * Why a value is not the string it will be filed as.
 *
 * The *what* comes from the service (`sample_in_name` — `slug()` at
 * `NAME_MAX`, which is what `folder_name()` will actually do); this is only
 * the reason, in the words `naming-plan.md` §"Fields collide" uses. Splitting
 * them that way is deliberate: a second `slug()` in the browser would be a
 * second answer to "what will this be called", and the two would drift on the
 * case nobody tested.
 */
const REASONS = [
  [/[<>:"/|?*\\]/, 'a folder name cannot hold that character'],
  [/[\x00-\x1f]/, 'a folder name cannot hold a control character'],
  [/\s/, 'a space would be a space in a directory name'],
  [/_/, 'an underscore forges a field boundary in the name'],
];

/**
 * What a field will be filed as, and why it differs — or `null` when it does
 * not.
 *
 * Until 2026-09-05 nothing reduced these three: `sample`, `material` and
 * `pixel` reached `folder_name()` raw, so `material = "PTQ10:IT-4F"` — the
 * contract's own example — put a colon in a Windows path segment, which fails
 * on the lab PC and passes on Linux (`naming-plan.md` §2). They are slugged
 * now, so nothing here is an error: the value is kept verbatim in the
 * metadata and reduced in the name, exactly as the comment has always been.
 * What the operator needs is to *see* the reduction, and to see it while they
 * are typing rather than in a folder listing afterwards — most of all when
 * the limit bites, because `PTQ10IT4F-batch-2026-08-A` and `…-B` are the
 * same 24 characters and a lab with two batches would otherwise find out from
 * the archive.
 */
export function nameHazard(value, inName) {
  const text = String(value == null ? '' : value);
  const filed = String(inName == null ? '' : inName);
  if (!text || filed === text) return null;
  const reason = REASONS.find(([re]) => re.test(text));
  return {
    filed,
    // Reduced to nothing: the field drops out of the name altogether, which
    // is worth saying plainly rather than showing an empty suggestion.
    text: filed
      ? (text.length > filed.length && !reason
        ? `too long for a name — filed as` : `filed as`)
      : 'reduces to nothing, so it is left out of the name',
    why: reason ? reason[1] : null,
  };
}

/** `s4 · PTQ10IT4F · pxa`, or null — what the bar's chip has always shown. */
export function identityOf(sample) {
  if (!sample) return null;
  const parts = [sample.sample, sample.material, sample.pixel].filter(Boolean);
  return parts.length ? parts.join(' · ') : null;
}

/**
 * Everything the panel and the chip draw from.
 *
 * `stem` is the folder name the next run starts with, built the way
 * `storage.naming.folder_name` builds it — the three fields that are present,
 * joined by `_`. It is a preview and says so with the ellipsis: what follows
 * is the temperature, the LED level, the V_oc and the stamp, none of which is
 * this panel's.
 */
export function identityModel(state) {
  const session = (state && state.session) || {};
  const sample = session.sample || {};
  const file = session.sample_file || {};
  // What the folder will carry, the service's own reduction. Absent from a
  // service older than this, in which case a field is shown as typed and the
  // stem is what that service would have written.
  const inName = session.sample_in_name || sample;
  const rows = IDENTITY_FIELDS.map((spec) => {
    const value = sample[spec.name] == null ? '' : String(sample[spec.name]);
    const was = file[spec.name] == null ? '' : String(file[spec.name]);
    return {
      ...spec,
      value,
      file: was,
      // Typed here rather than opened with. The way back is a `null`, which
      // the service resolves to the file's value — so the reset is offered on
      // exactly the rows where it would change something.
      typed: value !== was,
      hazard: spec.in_name === true ? nameHazard(value, inName[spec.name]) : null,
    };
  });
  // The chip is the identity as recorded; the stem is the identity as the
  // folder will spell it, which after `slug()` is not always the same string.
  const named = identityOf(sample);
  const stem = [inName.sample, inName.material, inName.pixel].filter(Boolean).join('_');
  return {
    rows,
    named,
    chip: named || 'no sample named',
    /** No device named at all: the state the console used to have no answer for. */
    unnamed: !named,
    stem,
    // `folder_name` starts with the temperature when there is no identity, so
    // an unnamed session's folders open on `290K_…` and carry no device.
    preview: stem ? `${stem}_…` : '290K_… — no device in the name',
    /** The fields the folder name will not spell the way they were typed. */
    hazards: rows.filter((r) => r.hazard),
  };
}

// -- the DOM ----------------------------------------------------------------

/**
 * The panel, into `el`. `onSet(name, value)` PUTs one key — `null` for the
 * way back — and `onClose` shuts it.
 *
 * Keyed on the model plus the answer: an edit committing must not rebuild the
 * field the caret is still in, which on this panel means the row the operator
 * just tabbed out of keeps its element while the four beside it redraw. The
 * whole panel is one key because the rows are five inputs and a `PUT` answers
 * in a millisecond on localhost — the measured case `views/bench.js` guards
 * against is a scan's worth of frames, and nothing here moves with a run.
 */
export function renderIdentity(el, state, { open, onSet, onClose, status = null } = {}) {
  const model = identityModel(state);
  el.hidden = !open;
  if (!open) {
    if (el.__key !== undefined) { el.__key = undefined; el.textContent = ''; }
    return model;
  }
  keyed(el, JSON.stringify([model, status]), () => [
    h('div.id-head',
      h('span.cn', 'what is mounted'),
      h('span.cs', 'the session’s [sample] block — PUT /session/sample. '
        + 'It names the runs queued after it; anything already queued keeps the name it has.'),
      h('span', { style: { flex: '1' } }),
      status ? h('span', { class: 'id-status ' + (status.level || ''), text: status.text }) : null,
      h('button.btng', { onclick: () => onClose && onClose() }, 'close')),
    h('div.id-rows', model.rows.map((row) => identityRow(row, onSet))),
    h('div.id-foot',
      // "starts", not "is": the temperature, the LED level, the V_oc, the
      // offset flag and a slug of the comment follow, and the ellipsis is all
      // of them. A label promising the whole name would be wrong by five
      // fields.
      h('span.l', 'next folder starts'),
      h('span', { class: 'id-stem' + (model.stem ? '' : ' absent'), text: model.preview }),
      h('span', { style: { flex: '1' } }),
      // The one legal `[sample]` key deliberately not offered, and why —
      // `run.toml`'s own comment, on the screen that would otherwise reopen it.
      h('span.cs', 'temperature_k is not typed here: a bench with a 331 reads the '
        + 'controller as each node starts, and a typed 290 filed a 220 K run as 290 K once already')),
  ]);
  return model;
}

function identityRow(row, onSet) {
  const input = h('input.v', {
    value: row.value,
    placeholder: row.placeholder,
    'aria-label': row.name,
    title: row.doc,
    onchange: (e) => onSet && onSet(row.name, e.target.value),
  });
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') input.blur(); });
  const hazard = row.hazard;
  return h('div.id-row' + (hazard ? '.warn' : ''),
    h('span.l', { text: row.name },
      row.in_name === true ? h('i', 'in the name') : row.in_name === 'slug' ? h('i', 'slugged into the name') : null),
    input,
    h('span.src',
      row.typed
        ? h('button.link.undo', {
          title: row.file
            ? `back to run.toml — ${row.file}`
            : 'back to run.toml, which left this empty',
          'aria-label': 'reset',
          onclick: () => onSet && onSet(row.name, null),
        }, '↺')
        : null),
    h('div.doc', h('span', { text: row.doc }),
      hazard
        ? h('span.id-warn',
          h('span', { text: `${hazard.text}` }),
          hazard.filed ? h('b', { text: hazard.filed }) : null,
          hazard.why ? h('i', { text: `— ${hazard.why}` }) : null,
          // The name as typed is kept in the metadata, so this is not an
          // error to fix; it is offered because a lab whose two batches
          // differ after character 24 would otherwise find out from the
          // archive. One click and the record says what the folder says.
          hazard.filed
            ? h('button.link', {
              title: `type ${hazard.filed} instead, so the record and the folder agree`,
              onclick: () => onSet && onSet(row.name, hazard.filed),
            }, 'use it')
            : null)
        : null));
}
