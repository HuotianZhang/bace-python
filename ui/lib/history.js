// The results tab's models — `docs/ui-plan.md` M6, from R2·3.
//
// Everything here is a pure function of what `GET /runs` and `GET /runs/{id}`
// answer, and nothing else: the tab opens no HDF5 and parses no folder name.
// That is what `docs/naming-plan.md` rule 1 bought — the identity travels
// with every run (`sample`), the temperature triple and the LED level sit on
// every module node, and what a node measured (its axis, `q_mean`/`q_std`
// per point, its curves' metrics, its shot counts) is on the node record
// whichever process answers. The DOM is in `lib/grid.js`, beside these, the
// same split the rail and the monitor have.
//
// Three rules from R2·3 that the models make true by construction:
//
//   * **The partial cell is outlined, never averaged in silently, never
//     dropped.** A cell whose node ended with fewer shots than it asked for is
//     a cell with `partial: true`; a cell in the T × LED product with no node
//     at all is `missing: true` and stays in the grid as the space it would
//     have filled. Neither is left out of a count without the count saying so.
//   * **σ_Q = 0 is *not recorded*** (`ui-rules` §2): `fmt.sigmaQ` answers
//     `null` for it and the legend says `□ σ_Q not recorded · ● σ_Q measured`.
//   * **Every flag states its reason.** A flag is a sentence in the voice of
//     `ui-rules` §10 — what happened and what it means for the number on
//     screen — never a code with a triangle beside it.
//
// And two decisions the design pass settled, recorded here because the
// artboard predates them (`docs/design/README.md`): R2·3 reads a `flags.json`
// the service never writes, so the flags are built from the record — the run's
// own verdicts, the node's outcome and counts, the temperature and V_oc
// provenance; and its `saturation` line is the corrected wording — the
// digitiser's verdict on each shot is the service's judgment, so the line
// counts the shots the service *flagged*, and says nothing about a shared
// extreme it judged as nothing.

import * as fmt from './format.js';

/** Modules whose node is one cell of the grid: one Q per node. */
export const GRID_MODULES = new Set(['bace']);

/** How far apart two LED levels may be and still be the same illumination (V). */
export const LED_MATCH_V = 0.0005;

const finite = (v) => typeof v === 'number' && Number.isFinite(v);

// -- identity ---------------------------------------------------------------

/** `s4 · PTQ10:IT-4F · pixel a`, or `null` when nothing was named. */
export function identityOf(sample) {
  if (!sample) return null;
  const parts = [sample.sample, sample.material, sample.pixel ? `pixel ${sample.pixel}` : ''].filter(Boolean);
  return parts.length ? parts.join(' · ') : null;
}

/** `2026-09-02 15:37`, for a run's row; the session log's `fmt.time` is too fine here. */
export function stamp(ts) {
  if (!ts) return fmt.ABSENT;
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** `15:37 → 16:02 · 25 min`, or what is known of it. */
export function spanOf(started, finished) {
  if (!started) return fmt.ABSENT;
  const from = stamp(started);
  if (!finished) return `${from} →`;
  const to = new Date(finished * 1000);
  const p = (n) => String(n).padStart(2, '0');
  const sameDay = stamp(finished).slice(0, 10) === from.slice(0, 10);
  const end = sameDay ? `${p(to.getHours())}:${p(to.getMinutes())}` : stamp(finished);
  return `${from} → ${end} · ${fmt.duration(finished - started)}`;
}

// -- the run list -----------------------------------------------------------

/** What a row is called: the module for a manual run, the tree's shape for a pipeline. */
export function runLabel(row) {
  if (!row) return fmt.ABSENT;
  if (row.kind === 'pipeline') return row.tree_summary ? `pipeline · ${row.tree_summary}` : 'pipeline';
  return row.module || row.kind || 'run';
}

/** The loudness of a row's state (`ui-rules` §3): a failure is the one alert. */
export function stateLevel(state) {
  if (state === 'failed' || state === 'blocked') return 'crit';
  if (state === 'aborted' || state === 'stopped' || state === 'cancelled') return 'warn';
  if (state === 'done') return 'ok';
  return 'live';
}

/**
 * The rows of `GET /runs?session=all`, grouped by session, newest first —
 * which is the order the service answers in, kept rather than re-sorted. A
 * session is a bench day, and the identity is per run: a session whose runs
 * were queued under two devices shows two identities in one group, which is
 * exactly the case rule 1 was written for.
 *
 * `live` is the store's view of this session's runs — a run still holding the
 * worker reads its state off the stream rather than off an index that was
 * fetched a minute ago.
 */
export function runGroups(rows, { live = {} } = {}) {
  const groups = [];
  const byId = new Map();
  for (const row of rows || []) {
    const sid = row.session_id || (row.run_id || '').split('-')[0] || '?';
    let group = byId.get(sid);
    if (!group) {
      group = { session: sid, identities: [], runs: [] };
      byId.set(sid, group);
      groups.push(group);
    }
    const identity = identityOf(row.sample);
    if (identity && !group.identities.includes(identity)) group.identities.push(identity);
    const state = (live[row.run_id] && live[row.run_id].state) || row.state;
    group.runs.push({
      run_id: row.run_id,
      label: runLabel(row),
      name: row.name || '',
      state,
      level: stateLevel(state),
      counts: fmt.keptOf(row.kept, row.requested),
      // Composed by `service/journal.py`, not here, and it is the console's
      // longest run of numbers: `Q -5.651e-13 ± 0.0e+00 C · 1/1` (#68). The
      // wire and the on-disk journal keep their ASCII — a journal is a record
      // and records get parsed — so the glyph is decided at the boundary, and
      // by `minusIn`, because the same field also carries `failed: <path>`.
      outcome: fmt.minusIn(row.outcome_text || ''),
      at: stamp(row.started_at || row.queued_at),
      identity,
      voc: vocRange(row),
      folder: row.folder || (row.folders && row.folders[0]) || null,
      // `node_count_done` is module *runs* -- eight on a 2 x 2 grid of two
      // modules -- and `node_count` is the tree's leaves, two; the row says
      // the first, and never "8/2".
      nodes: row.node_count_done ?? null,
    });
  }
  return { groups, total: (rows || []).length };
}

function vocRange(row) {
  if (!finite(row.voc_min)) return null;
  if (finite(row.voc_max) && row.voc_max !== row.voc_min) {
    return `V_oc ${fmt.volts(row.voc_min, { unit: false })} … ${fmt.volts(row.voc_max)}`;
  }
  return `V_oc ${fmt.volts(row.voc_min)}`;
}

// -- the record's nodes -----------------------------------------------------

/**
 * The module nodes of a record, in the order they ran. `nodes` is keyed by
 * path in insertion order, which is arrival order from both sources; the
 * timestamps are the tie-break for a record assembled some other way.
 */
export function moduleNodes(record) {
  const nodes = Object.values((record && record.nodes) || {}).filter((n) => n && n.module);
  return nodes.sort((a, b) => (a.started_at || a.finished_at || 0) - (b.started_at || b.finished_at || 0));
}

/** `partial`: it asked for more than it kept. Never a state, a count. */
export function isPartial(node) {
  return finite(node.kept) && finite(node.requested) && node.kept < node.requested;
}

/** Whether the node's statistics have a σ behind them anywhere: zero is *not recorded*. */
export function sigmaRecorded(node) {
  return (node.q_std || []).some((s) => finite(s) && s !== 0);
}

/**
 * The one number a cell shows, and where on the axis it comes from.
 *
 * A `bace` at V_oc has a zero-width axis — one point, and the number is its
 * mean. A swept axis has many, and the cell shows the point nearest the V_oc
 * it was centred on, saying so; without a V_oc it shows the middle point and
 * says that instead. Never an average over the axis: Q(V_pre) is a curve, and
 * the mean of a curve is nobody's charge.
 */
export function cellQ(node) {
  const means = node.q_mean || [];
  const stds = node.q_std || [];
  const values = node.values || [];
  if (!means.length) return { q: null, sigma: null, at: null, index: null, points: values.length };
  let index = 0;
  let at = 'the one point';
  if (means.length > 1) {
    if (finite(node.voc) && values.length === means.length) {
      index = nearest(values, node.voc);
      at = `nearest V_oc, ${axisName(node)} = ${fmt.sig(values[index], 4)}`;
    } else {
      index = Math.floor(means.length / 2);
      at = `the middle point, ${axisName(node)} = ${values.length === means.length ? fmt.sig(values[index], 4) : index + 1}`;
    }
  }
  return {
    q: finite(means[index]) ? means[index] : null,
    sigma: fmt.sigmaQ(finite(stds[index]) ? stds[index] : null) === null ? null : stds[index],
    at, index, points: means.length,
  };
}

function nearest(values, target) {
  let best = 0;
  for (let i = 1; i < values.length; i += 1) {
    if (Math.abs(values[i] - target) < Math.abs(values[best] - target)) best = i;
  }
  return best;
}

function axisName(node) {
  return (node.axis && node.axis.name) || 'axis';
}

// -- the grid ---------------------------------------------------------------

/** Temperatures to a tenth and levels to a millivolt: the keys a cell is found by. */
export const tKey = (k) => (finite(k) ? (Math.round(k * 10) / 10).toFixed(1) : 'none');
export const ledKey = (v) => (finite(v) ? (Math.round(v * 1000) / 1000).toFixed(3) : 'none');

/**
 * The T × LED grid of a record: one cell per `bace` node, every row and
 * column that occurs, and the cells of the product that never ran kept as
 * `missing`. A record with no grid module at all has `shape: 'none'`; one
 * whose cells all share a temperature (a bench day of manual runs) is still
 * a grid — one row — because a manual result is a result (`ui-rules` §8).
 *
 * Two nodes on one cell — a `bace` run twice at the same T and level — keep
 * both: the cell holds every node, shows the newest, and says how many.
 */
export function gridModel(record) {
  const nodes = moduleNodes(record).filter((n) => GRID_MODULES.has(n.module));
  if (!nodes.length) return { shape: 'none', rows: [], cols: [], cells: [], byKey: {}, nodes: [] };
  const rowsBy = new Map();
  const colsBy = new Map();
  const byKey = {};
  // The axes are what was *asked for*, not only what ran: a pipeline stopped
  // before any bace at its last temperature has no node at that temperature,
  // and a row that vanished would make the summary count no missing cell.
  const asked = requestedAxes(record);
  for (const t of asked.temperatures) {
    // `t` is what was reached, and nothing has been: the setpoint is `asked`.
    rowsBy.set(tKey(t), { key: tKey(t), t: null, asked: t, how: 'setpoint', source: '',
      label: fmt.kelvin(t), ran: false });
  }
  for (const v of asked.levels) {
    colsBy.set(ledKey(v), { key: ledKey(v), led_v: v, asked: v,
      label: `led_v ${fmt.volts(v, { decimals: 3 })}`, ran: false });
  }
  for (const node of nodes) {
    // Which row a node belongs to is the setpoint it ran *under* -- a node
    // the operator resumed at 280.1 K is the 280 K row's, and the row then
    // says what was reached and how. The step the service scheduled for
    // this path says the setpoint; without a schedule (a journal record)
    // the nearest asked temperature inside the loop's tolerance does.
    const setpoint = asked.byPath[node.node_path];
    const t = finite(node.temperature_k) ? node.temperature_k : null;
    const askedT = finite(setpoint && setpoint.temperature_k) ? setpoint.temperature_k
      : nearestWithin(asked.temperatures, t, asked.tolerance_k);
    const tk = tKey(askedT !== null ? askedT : t);
    if (!rowsBy.has(tk)) {
      rowsBy.set(tk, { key: tk, t, asked: askedT, how: '', source: '', label: '', ran: false });
    }
    const row = rowsBy.get(tk);
    if (!row.ran) {
      row.ran = true;
      row.t = t;
      row.how = node.temperature_how || '';
      row.source = node.temperature_source || '';
      row.label = t !== null ? fmt.kelvin(t) : 'T not recorded';
    }
    const askedV = finite(setpoint && setpoint.led_v) ? setpoint.led_v
      : nearestWithin(asked.levels, node.led_v, LED_MATCH_V);
    const lk = ledKey(askedV !== null ? askedV : node.led_v);
    if (!colsBy.has(lk)) {
      colsBy.set(lk, { key: lk, led_v: finite(node.led_v) ? node.led_v : null, asked: askedV,
        label: finite(node.led_v) ? `led_v ${fmt.volts(node.led_v, { decimals: 3 })}` : 'led_v not recorded', ran: false });
    }
    colsBy.get(lk).ran = true;
    const key = `${tk}|${lk}`;
    const cell = byKey[key] || (byKey[key] = { key, row: tk, col: lk, nodes: [] });
    cell.nodes.push(node);
  }
  const rows = [...rowsBy.values()].sort((a, b) => (b.t ?? b.asked ?? -Infinity) - (a.t ?? a.asked ?? -Infinity));
  const cols = [...colsBy.values()].sort((a, b) => (a.led_v ?? a.asked ?? Infinity) - (b.led_v ?? b.asked ?? Infinity));
  const cells = [];
  for (const row of rows) {
    for (const col of cols) {
      const key = `${row.key}|${col.key}`;
      const found = byKey[key];
      if (!found) {
        cells.push((byKey[key] = { key, row: row.key, col: col.key, nodes: [], missing: true }));
        continue;
      }
      Object.assign(found, cellOf(found.nodes));
      cells.push(found);
    }
  }
  return { shape: 'grid', rows, cols, cells, byKey, nodes };
}

/** The asked value nearest `value`, when one is within `tol`; else null. */
function nearestWithin(values, value, tol) {
  if (!finite(value)) return null;
  let best = null;
  for (const v of values) {
    if (Math.abs(v - value) <= tol && (best === null || Math.abs(v - value) < Math.abs(best - value))) best = v;
  }
  return best;
}

/**
 * The temperatures and LED levels the run asked for, from the tree it
 * posted and the schedule the service resolved for it — so the grid's
 * axes exist before, and survive without, a `bace` at every one of them.
 *
 * Nothing is counted here (`docs/ui-plan.md` M5): a range on the tree
 * (`led_start_v … led_step_v`) is read off the schedule's steps, which
 * carry the levels the service made of it; a journal record has no
 * schedule, so for last week's run a range contributes nothing and the
 * axes are the tree's explicit lists plus whatever ran. A temperature
 * *module* is a setpoint too, but only its loop's or the session's typed
 * value is bound on the steps' `detail`, so it is read off the tree.
 */
export function requestedAxes(record) {
  const temperatures = new Set();
  const levels = new Set();
  const byPath = {};
  let tolerance_k = 0.5;
  const walk = (node) => {
    if (!node || typeof node !== 'object') return;
    if (node.kind === 'loop') {
      for (const k of node.values_k || []) if (finite(k)) temperatures.add(k);
      for (const v of node.levels_v || []) if (finite(v)) levels.add(v);
      if (finite(node.tolerance_k)) tolerance_k = Math.max(tolerance_k, node.tolerance_k);
    } else if (node.kind === 'module' && node.module === 'temperature') {
      const k = node.params && node.params.setpoint_k;
      if (finite(k)) temperatures.add(k);
    }
    for (const child of node.children || []) walk(child);
  };
  walk(record && record.tree);
  const schedule = record && record.schedule;
  const steps = Array.isArray(schedule) ? schedule : (schedule && schedule.steps) || [];
  for (const step of steps) {
    if (!step || step.kind !== 'module' || !GRID_MODULES.has(_moduleOf(step))) continue;
    const detail = step.detail || {};
    if (finite(detail.temperature_k)) temperatures.add(detail.temperature_k);
    if (finite(detail.led_v)) levels.add(detail.led_v);
    byPath[step.node_path] = { temperature_k: detail.temperature_k, led_v: detail.led_v };
  }
  return { temperatures: [...temperatures], levels: [...levels], byPath, tolerance_k };
}

function _moduleOf(step) {
  if (step.module) return step.module;
  return String(step.node_path || '').split('/').pop().split('#')[0];
}

/** One cell from its nodes: the newest node's numbers, and how many there were. */
function cellOf(nodes) {
  const node = nodes[nodes.length - 1];
  const q = cellQ(node);
  return {
    missing: false,
    node,
    path: node.node_path,
    repeated: nodes.length,
    q: q.q, sigma: q.sigma, at: q.at, points: q.points,
    voc: finite(node.voc) ? node.voc : null,
    vocHow: node.voc_how || null,
    kept: node.kept, requested: node.requested,
    partial: isPartial(node),
    outcome: node.outcome || null,
    sigmaRecorded: q.sigma !== null,
  };
}

// -- the summary strip ------------------------------------------------------

/**
 * R2·3's four lines, each a sentence about the whole run: the span of Q, where
 * σ was recorded, whether the meter answered, and how many cells are not what
 * they were asked to be. A partial cell is excluded from the span *and the
 * line says so* — that is what "never averaged in silently" means here.
 */
export function summaryModel(grid, record) {
  const present = grid.cells.filter((c) => !c.missing);
  const complete = present.filter((c) => !c.partial && finite(c.q));
  const partial = present.filter((c) => c.partial);
  const missing = grid.cells.filter((c) => c.missing);
  const lines = [];

  if (complete.length) {
    const qs = complete.map((c) => c.q);
    const lo = Math.min(...qs);
    const hi = Math.max(...qs);
    const decades = lo !== 0 && hi !== 0 && Math.sign(lo) === Math.sign(hi)
      ? Math.abs(Math.log10(Math.abs(hi) / Math.abs(lo))) : null;
    const sign = qs.every((q) => q < 0) ? 'all negative' : qs.every((q) => q > 0) ? 'all positive' : 'both signs';
    let text = `${fmt.charge(lo)} → ${fmt.charge(hi)}`;
    if (decades !== null) text += ` · ${decades < 0.05 ? 'flat' : decades < 0.5 ? 'within a factor of ' + fmt.sig(10 ** decades, 2) : fmt.sig(decades, 2) + ' decades'}`;
    text += ` · ${sign}`;
    text += ` · over ${complete.length} complete cell${complete.length === 1 ? '' : 's'}`;
    if (partial.length) text += `, ${partial.length} partial excluded`;
    lines.push({ key: 'span', label: 'span', text });
  } else {
    lines.push({ key: 'span', label: 'span', text: present.length ? 'no complete cell' : 'no bace node in this run', absent: true });
  }

  const withSigma = present.filter((c) => c.sigmaRecorded);
  if (present.length) {
    const rowsWith = [...new Set(withSigma.map((c) => grid.rows.find((r) => r.key === c.row)).filter(Boolean).map((r) => r.label))];
    lines.push({
      key: 'sigma', label: 'σ_Q',
      text: withSigma.length === present.length
        ? `recorded on every cell (${present.length} of ${present.length})`
        : withSigma.length === 0
          ? 'recorded on no cell · a zero is not a σ, it means the loops were not recorded'
          : `recorded at ${rowsWith.join(', ')} only · zero elsewhere means not recorded`,
      level: withSigma.length ? '' : 'warn',
    });
  }

  const shots = present.reduce((n, c) => n + (c.node.shots || 0), 0);
  const withIntensity = present.reduce((n, c) => n + (c.node.intensity_recorded || 0), 0);
  if (shots) {
    lines.push({
      key: 'intensity', label: 'intensity',
      text: withIntensity === shots
        ? `read on every shot (${shots} of ${shots}) · watts at the meter, never mW/cm²`
        : withIntensity === 0
          ? `null on ${shots} of ${shots} shots · the meter did not answer; watts not recorded, mW/cm² never inferred`
          : `null on ${shots - withIntensity} of ${shots} shots · watts where read, mW/cm² never inferred`,
      level: withIntensity === shots ? '' : 'warn',
    });
  }

  const flagged = present.reduce((n, c) => n + (c.node.shots_flagged || 0), 0);
  const parts = [];
  if (partial.length) parts.push(`${partial.length} partial (${partial.map((c) => cellName(grid, c)).join(', ')})`);
  if (missing.length) parts.push(`${missing.length} never run (${missing.map((c) => cellName(grid, c)).join(', ')})`);
  if (flagged) parts.push(`${flagged} of ${shots} shots flagged by the digitiser check`);
  const verdicts = (record && record.verdicts) || [];
  const warn = verdicts.filter((v) => v.level === 'warn').length;
  const crit = verdicts.filter((v) => v.level === 'crit').length;
  if (crit) parts.push(`${crit} crit at Start`);
  if (warn) parts.push(`${warn} warn at Start`);
  lines.push({
    key: 'flags', label: 'flags',
    text: parts.length ? parts.join(' · ') : 'none · every cell complete, no shot flagged, nothing to say at Start',
    level: partial.length || missing.length || flagged || crit ? 'warn' : '',
  });
  return { lines, complete: complete.length, partial: partial.length, missing: missing.length, present: present.length };
}

/** `250.1 K · 1.020 V`, a cell's name in a sentence. */
export function cellName(grid, cell) {
  const row = grid.rows.find((r) => r.key === cell.row);
  const col = grid.cols.find((c) => c.key === cell.col);
  return `${row ? row.label : cell.row} · ${col && finite(col.led_v) ? fmt.volts(col.led_v, { decimals: 3 }) : cell.col}`;
}

// -- the temperature and V_oc, in words --------------------------------------

/**
 * The temperature triple as a sentence — `docs/ui-kickoff.md`: *how* alone
 * does not separate the pause the operator typed a number into from the one
 * that ended without one, so both are rendered, and a `simulated` source
 * never reads as a measurement.
 */
export function temperatureText(node) {
  const k = node.temperature_k;
  if (!finite(k)) return { text: 'not recorded', level: 'warn', measured: false };
  const how = node.temperature_how || '';
  const source = node.temperature_source || '';
  const value = fmt.kelvin(k);
  switch (how) {
    case 'typed':
      return { text: `${value} · typed into [sample], nobody read an instrument`, level: 'warn', measured: false };
    case 'setpoint':
      return { text: `${value} · requested, not reached: nothing read back`, level: 'warn', measured: false };
    case 'read':
      // Nobody asked for a setpoint: the controller was read once as the
      // node started -- the bench as found, a measurement of it.
      if (source === 'simulated') return { text: `${value} · read on the simulated 331 as the node started, not a measurement`, level: 'warn', measured: false };
      return { text: `${value} · read on the ${source || 'controller'} as the node started; no setpoint was asked for`, level: 'ok', measured: true };
    case 'settled':
      if (source === 'simulated') return { text: `${value} · settled on the simulated 331, not a measurement`, level: 'warn', measured: false };
      return { text: `${value} · settled, read on the ${source || 'controller'}`, level: 'ok', measured: true };
    case 'operator':
      if (source === 'operator') return { text: `${value} · typed by the operator at the pause`, level: '', measured: false };
      if (source === 'simulated') return { text: `${value} · the pause ended without a number; the last simulated reading`, level: 'warn', measured: false };
      return { text: `${value} · the pause ended without a number; the last reading polled on the ${source || 'controller'}`, level: '', measured: true };
    default:
      return { text: `${value} · ${how || 'provenance not recorded'}${source ? ' · ' + source : ''}`, level: 'warn', measured: false };
  }
}

/** Which measurement supplied the V_oc, and at what LED level (`ui-rules` §6). */
export function vocText(node) {
  if (!finite(node.voc)) return { text: 'none', level: node.module === 'bace' ? 'warn' : '' };
  const how = node.voc_how || '';
  const at = finite(node.voc_led_v) ? ` at ${fmt.volts(node.voc_led_v, { decimals: 3 })}` : '';
  const mismatch = finite(node.voc_led_v) && finite(node.led_v) && Math.abs(node.voc_led_v - node.led_v) > LED_MATCH_V;
  const from = how === 'typed' ? 'typed' : how === 'measure_dc' ? 'measured by this node under DC' : how ? `from ${how}` : 'provenance not recorded';
  return {
    text: `${fmt.volts(node.voc)} · ${from}${at}${mismatch ? ` — this cell pulsed at ${fmt.volts(node.led_v, { decimals: 3 })}` : ''}`,
    level: mismatch || how === 'typed' || !how ? 'warn' : '',
    mismatch,
  };
}

// -- the flags --------------------------------------------------------------

/**
 * Every flag on a node, each with its reason — R2·3's list, built from the
 * record because there is no `flags.json`. Order is loudness: what stops the
 * number meaning anything first, then what qualifies it, then what is merely
 * on record.
 */
export function flagsFor(record, node) {
  const flags = [];
  if (!node) return flags;
  const shots = node.shots || 0;
  if (node.outcome === 'failed') {
    flags.push({ level: 'crit', code: 'failed', text: `the node failed: ${node.error || node.reason || record.error || 'no reason recorded'}` });
  }
  if (isPartial(node)) {
    const why = node.outcome === 'stopped' ? 'stopped after a shot' : node.outcome === 'aborted' ? 'aborted, the shot in flight discarded'
      : node.outcome === 'failed' ? 'failed' : node.outcome || 'ended early';
    const unit = node.module === 'bace' ? 'shots' : 'curves';
    flags.push({ level: 'warn', code: 'partial',
      text: `${node.kept} of ${node.requested} ${unit} kept · ${why}${node.reason && node.outcome !== 'failed' ? ' · ' + node.reason : ''} · the statistics are over the ${unit} that ran, and this cell is never averaged in silently` });
  }
  if (node.module === 'bace' && (node.q_mean || []).length && !sigmaRecorded(node)) {
    flags.push({ level: 'warn', code: 'sigma', text: 'σ_Q not recorded · a zero is not a σ: one loop leaves nothing to deviate from' });
  }
  if (shots && node.shots_flagged) {
    flags.push({ level: 'warn', code: 'digitiser',
      text: `${node.shots_flagged} of ${shots} shots flagged by the digitiser check — a rail or a shared extreme on a trace; the shot's own line in its card says which. Do not interpret those charges` });
  } else if (shots && node.module === 'bace') {
    flags.push({ level: 'info', code: 'digitiser', text: `digitiser check: no shot flagged, ${shots} of ${shots} judged` });
  }
  if (shots) {
    const with_ = node.intensity_recorded || 0;
    if (with_ === 0) flags.push({ level: 'warn', code: 'intensity', text: `intensity null on every shot (${shots}) · the meter did not answer; watts not recorded, mW/cm² never inferred` });
    else if (with_ < shots) flags.push({ level: 'warn', code: 'intensity', text: `intensity null on ${shots - with_} of ${shots} shots · watts where read, mW/cm² never inferred` });
  }
  const t = temperatureText(node);
  if (t.level === 'warn') flags.push({ level: 'warn', code: 'temperature', text: `temperature ${t.text}` });
  const v = vocText(node);
  if (v.mismatch) flags.push({ level: 'warn', code: 'voc', text: `V_oc ${v.text} · a V_oc from a different illumination is worse than none` });
  else if (node.module === 'bace' && v.level === 'warn') flags.push({ level: 'warn', code: 'voc', text: `V_oc ${v.text}` });
  if (node.offset_corrected === false) {
    flags.push({ level: 'info', code: 'baseline', text: 'baseline not subtracted (offset_correct = false) · the dark trace is taken as it came' });
  }
  // The run's own verdicts, where they apply: the whole run (no path), this
  // node, or a loop above it -- `temperature.not-wired` is said on the
  // temperature node and is true of every cell under it. `info` stays: the
  // `trigger.auto` one is `ui-rules` §9's cable-out case, worth saying beside
  // a charge near zero.
  for (const verdict of (record && record.verdicts) || []) {
    if (!['warn', 'crit', 'info'].includes(verdict.level)) continue;
    if (!verdictApplies(verdict.node_path || '', node.node_path || '')) continue;
    flags.push({ level: verdict.level, code: verdict.code || 'verdict',
      text: `${verdict.text || verdict.code}${verdict.node_path ? ' · at Start, on ' + verdict.node_path : ' · at Start, for the whole run'}` });
  }
  const order = { crit: 0, warn: 1, info: 2 };
  return flags.sort((a, b) => (order[a.level] ?? 3) - (order[b.level] ?? 3));
}

/** A verdict with no path is the run's; one on a loop is every node's beneath it. */
export function verdictApplies(verdictPath, nodePath) {
  if (!verdictPath) return true;
  if (verdictPath === nodePath) return true;
  return nodePath.startsWith(verdictPath + '/');
}

// -- provenance -------------------------------------------------------------

/**
 * `resolved from: run.toml 17 · last-used 2 · inherited 2 (led_v, voc) ·
 * edited 1 (n_loops)` — the layers of `params_as_executed[path]`, which a run
 * of this process carries and a journal record does not; `null` then, and the
 * screen says the layers were not kept rather than counting nothing.
 */
export function provenanceSummary(params) {
  if (!params || typeof params !== 'object') return null;
  const counts = new Map();
  const names = new Map();
  for (const [name, entry] of Object.entries(params)) {
    const source = (entry && entry.source) || 'unknown';
    counts.set(source, (counts.get(source) || 0) + 1);
    if (!names.has(source)) names.set(source, []);
    names.get(source).push(name);
  }
  const ORDER = ['run.toml', 'default', 'last-used', 'inherited', 'derived', 'edited'];
  const keys = [...counts.keys()].sort((a, b) => (ORDER.indexOf(a) + 1 || 99) - (ORDER.indexOf(b) + 1 || 99));
  const parts = keys.map((source) => {
    const n = counts.get(source);
    const listed = ['inherited', 'derived', 'edited'].includes(source) && n <= 4 ? ` (${names.get(source).join(', ')})` : '';
    return `${source} ${n}${listed}`;
  });
  return { text: parts.join(' · '), counts: Object.fromEntries(counts), total: Object.keys(params).length };
}

/**
 * The overrides a cell can be run again with, as a manual run of its module:
 * what the tree typed on the node (`edited`) plus what the loop gave it
 * (`inherited`: the level, the low level, the settle) — a manual bace outside
 * any loop owns all three itself. Never the V_oc: the bench's own is the
 * source for a manual run, and a number measured at another hour under
 * another lamp level is not a parameter.
 */
export function rerunParams(params) {
  const out = {};
  for (const [name, entry] of Object.entries(params || {})) {
    if (!entry || name === 'voc') continue;
    if (entry.source === 'edited' || entry.source === 'inherited') {
      if (entry.value !== null && entry.value !== undefined) out[name] = entry.value;
    }
  }
  return out;
}

// -- export -----------------------------------------------------------------

/** The grid as CSV: one row per cell, the numbers at full precision, the absences empty. */
export function csvOf(grid, record) {
  const head = ['run_id', 'node_path', 'temperature_k', 'temperature_how', 'temperature_source',
    'led_v', 'voc_v', 'voc_how', 'q_c', 'sigma_q_c', 'kept', 'requested', 'outcome', 'folder'];
  const lines = [head.join(',')];
  for (const cell of grid.cells) {
    if (cell.missing) {
      const row = grid.rows.find((r) => r.key === cell.row);
      const col = grid.cols.find((c) => c.key === cell.col);
      lines.push([record.run_id, '', row ? (row.t ?? row.asked ?? '') : '', row && row.t === null ? 'setpoint' : '', '', col ? (col.led_v ?? col.asked ?? '') : '',
        '', '', '', '', '', '', 'never run', ''].map(csvField).join(','));
      continue;
    }
    const n = cell.node;
    lines.push([record.run_id, n.node_path, n.temperature_k ?? '', n.temperature_how || '', n.temperature_source || '',
      n.led_v ?? '', n.voc ?? '', n.voc_how || '', cell.q ?? '', cell.sigma ?? '', n.kept ?? '', n.requested ?? '',
      n.outcome || '', n.folder || ''].map(csvField).join(','));
  }
  return lines.join('\n') + '\n';
}

function csvField(v) {
  const s = v === null || v === undefined ? '' : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}
