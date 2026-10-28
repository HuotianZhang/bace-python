// The identity panel edits `[sample]` and previews filing. Anonymous runs lack device
// identity in folder names, and renaming folders can break references. The panel
// shows the stem and service-reduced spelling, including character removal and
// truncation. Distinct long names can collapse to one filename, so reductions include
// reasons and an action to adopt them. Metadata retains typed values. `temperature_k`
// is omitted because stale typed values can mislabel runs; the panel identifies
// controller readings instead. Pure `identityModel` supports tests without a browser.

import { h, keyed } from './dom.js';

/**
 * The five fields follow folder-name order. `doc` explains the affected file or
 * instrument and consequences (`docs/ui-rules.md` §1). `in_name` marks values
 * affecting persistent paths.
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
 * The helper explains filename reductions. The service supplies `sample_in_name`,
 * including truncation, so the browser cannot produce a conflicting preview through
 * separate reduction logic.
 */
const REASONS = [
  [/[<>:"/|?*\\]/, 'a folder name cannot hold that character'],
  [/[\x00-\x1f]/, 'a folder name cannot hold a control character'],
  [/\s/, 'a space would be a space in a directory name'],
  [/_/, 'an underscore forges a field boundary in the name'],
];

/**
 * The helper shows filename reductions during typing. Colons cannot appear in Windows
 * path segments. Metadata retains full values; filenames use the service's reduced
 * spelling. Truncation after character 24 can merge batch names, so the result must
 * be visible before filing.
 */
export function nameHazard(value, inName) {
  const text = String(value == null ? '' : value);
  const filed = String(inName == null ? '' : inName);
  if (!text || filed === text) return null;
  const reason = REASONS.find(([re]) => re.test(text));
  return {
    filed,
    // A value reduced to empty disappears from the name. An empty suggestion would
    // hide that consequence.
    text: filed
      ? (text.length > filed.length && !reason
        ? `too long for a name — filed as` : `filed as`)
      : 'reduces to nothing, so it is left out of the name',
    why: reason ? reason[1] : null,
  };
}

/** The helper formats bar identity or returns `null` when absent. */
export function identityOf(sample) {
  if (!sample) return null;
  const parts = [sample.sample, sample.material, sample.pixel].filter(Boolean);
  return parts.length ? parts.join(' · ') : null;
}

/**
 * The model supplies panel and bar content. `stem` joins present device fields with
 * underscores; its ellipsis marks temperature, LED level, V_oc and timestamp
 * components determined outside this panel.
 */
export function identityModel(state) {
  const session = (state && state.session) || {};
  const sample = session.sample || {};
  const file = session.sample_file || {};
  // The service supplies filename spelling. Without `sample_in_name`, the panel shows
  // the typed fields used by the responding service.
  const inName = session.sample_in_name || sample;
  const rows = IDENTITY_FIELDS.map((spec) => {
    const value = sample[spec.name] == null ? '' : String(sample[spec.name]);
    const was = file[spec.name] == null ? '' : String(file[spec.name]);
    return {
      ...spec,
      value,
      file: was,
      // A `null` reset restores the file value. Only edits differing from it need a
      // reset (`docs/service-contract.md` §3a).
      typed: value !== was,
      hazard: spec.in_name === true ? nameHazard(value, inName[spec.name]) : null,
    };
  });
  // The bar shows recorded identity; the stem shows reduced filename spelling. They
  // can differ.
  const named = identityOf(sample);
  const stem = [inName.sample, inName.material, inName.pixel].filter(Boolean).join('_');
  return {
    rows,
    named,
    chip: named || 'no sample named',
    /** The predicate identifies a session without a named device. */
    unnamed: !named,
    stem,
    // Without identity fields, filenames begin with temperature and carry no device
    // name.
    preview: stem ? `${stem}_…` : '290K_… — no device in the name',
    /**
     * The helper lists fields whose filename spelling differs from their typed value.
     */
    hazards: rows.filter((r) => r.hazard),
  };
}

// -- the DOM ----------------------------------------------------------------

/**
 * The renderer mounts the panel in `el`. `onSet(name, value)` edits one key; `null`
 * resets it and `onClose` closes the panel. Model and response keys preserve the
 * active input. Five fields and a localhost response of about 1 ms, measured on this
 * bench, permit one panel key. Run frames do not update it.
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
      h('span.cs', 'the session’s [sample] block. '
        + 'It names the runs queued after it; anything already queued keeps the name it has.'),
      h('span', { style: { flex: '1' } }),
      status ? h('span', { class: 'id-status ' + (status.level || ''), text: status.text }) : null,
      h('button.btng', { onclick: () => onClose && onClose() }, 'close')),
    h('div.id-rows', model.rows.map((row) => identityRow(row, onSet))),
    h('div.id-foot',
      // The preview is a stem: temperature, LED level, V_oc, baseline flag and
      // comment slug follow. Its ellipsis must not imply a complete filename.
      h('span.l', 'next folder starts'),
      h('span', { class: 'id-stem' + (model.stem ? '' : ' absent'), text: model.preview }),
      h('span', { style: { flex: '1' } }),
      // The panel explains why it omits `temperature_k`, avoiding a stale temperature
      // claim.
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
          // Adopting reduced spelling makes metadata match the folder. It is
          // optional, but truncation after character 24 can merge otherwise distinct
          // batches.
          hazard.filed
            ? h('button.link', {
              title: `type ${hazard.filed} instead, so the record and the folder agree`,
              onclick: () => onSet && onSet(row.name, hazard.filed),
            }, 'use it')
            : null)
        : null));
}
