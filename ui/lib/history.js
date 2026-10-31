// Results models consume `GET /runs` and `GET /runs/{id}` without opening HDF5 or
// parsing folder names. Records carry identity, temperature provenance, LED level,
// statistics, curve metrics and shot counts (`docs/service-contract.md` §6). Partial
// cells remain outlined and missing cells remain hatched in the grid product, with
// exclusions stated in counts. Zero Q standard deviation denotes unrecorded
// uncertainty (`docs/ui-rules.md` §2). Flags state reasons derived from verdicts,
// outcomes, counts and provenance. Saturation counts follow service-flagged shots; a
// shared extreme alone is not a flag.

import * as fmt from './format.js';
import { moduleTitle } from './fields.js';

/** Modules whose node is one cell of the grid: one Q per node. */
export const GRID_MODULES = new Set(['bace']);

/** How far apart two LED levels may be and still be the same illumination (V). */
export const LED_MATCH_V = 0.0005;

const finite = (v) => typeof v === 'number' && Number.isFinite(v);

// -- identity ---------------------------------------------------------------

/** The helper formats sample identity or returns `null` when unnamed. */
export function identityOf(sample) {
  if (!sample) return null;
  const parts = [sample.sample, sample.material, sample.pixel ? `pixel ${sample.pixel}` : ''].filter(Boolean);
  return parts.length ? parts.join(' · ') : null;
}

/**
 * The run row uses minute precision; the session log's `fmt.time` is unnecessarily fine
 * here.
 */
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

/**
 * What a row is called: the module for a manual run, the tree's shape for a pipeline.
 */
export function runLabel(row) {
  if (!row) return fmt.ABSENT;
  if (row.kind === 'pipeline') return 'Sequence';
  return moduleTitle(row.module || row.kind || 'run');
}

/** Row severity reserves alert for failure (`docs/ui-rules.md` §3). */
export function stateLevel(state) {
  if (state === 'failed' || state === 'blocked') return 'crit';
  if (state === 'aborted' || state === 'stopped' || state === 'cancelled') return 'warn';
  if (state === 'done') return 'ok';
  return 'live';
}

/**
 * The helper groups index rows by session in service order, most recent first.
 * Identity belongs to each run, so a session can show multiple devices. `live`
 * supplies active run state from the stream rather than a stale index response.
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
      outcome: row.outcome_text || '',
      at: stamp(row.started_at || row.queued_at),
      identity,
      voc: vocRange(row),
      folder: row.folder || (row.folders && row.folders[0]) || null,
      // `node_count_done` counts module executions; `node_count` counts tree leaves.
      // Two modules on a 2 × 2 grid give eight executions and two leaves, so these
      // values cannot form a completed/requested ratio.
      nodes: row.node_count_done ?? null,
    });
  }
  return { groups, total: (rows || []).length };
}

function vocRange(row) {
  if (!finite(row.voc_min)) return null;
  if (finite(row.voc_max) && row.voc_max !== row.voc_min) {
    return `Voc ${fmt.volts(row.voc_min, { unit: false })} … ${fmt.volts(row.voc_max)}`;
  }
  return `Voc ${fmt.volts(row.voc_min)}`;
}

// -- the record's nodes -----------------------------------------------------

/**
 * The helper orders module nodes by execution. Insertion order reflects arrival from
 * both record sources; timestamps break ties for records assembled differently.
 */
export function moduleNodes(record) {
  const nodes = Object.values((record && record.nodes) || {}).filter((n) => n && n.module);
  return nodes.sort((a, b) => (a.started_at || a.finished_at || 0) - (b.started_at || b.finished_at || 0));
}

/** `partial` compares requested and retained counts rather than run state. */
export function isPartial(node) {
  return finite(node.kept) && finite(node.requested) && node.kept < node.requested;
}

/** The predicate identifies recorded Q uncertainty; zero denotes absence. */
export function sigmaRecorded(node) {
  return (node.q_std || []).some((s) => finite(s) && s !== 0);
}

/**
 * The cell shows one axis point's mean Q. A zero-width axis has one point. A sweep
 * uses the point nearest its V_oc, or the middle point without V_oc, and identifies
 * that choice. Averaging across Q(V_pre) would erase the curve and produce no defined
 * point measurement.
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
      at = `nearest Voc, ${axisName(node)} = ${fmt.sig(values[index], 4)}`;
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
 * The grid retains missing cells in the product of temperatures and LED levels. No
 * grid module gives `shape: 'none'`; one temperature still gives one row because
 * manual results remain results (`docs/ui-rules.md` §8). Repeated nodes at the same
 * coordinates remain in the cell, which shows the most recent and their count.
 */
export function gridModel(record) {
  const nodes = moduleNodes(record).filter((n) => GRID_MODULES.has(n.module));
  if (!nodes.length) return { shape: 'none', rows: [], cols: [], cells: [], byKey: {}, nodes: [] };
  const rowsBy = new Map();
  const colsBy = new Map();
  const byKey = {};
  // Requested axes retain temperatures with no completed `bace` node. Otherwise
  // stopping early would remove rows and hide missing cells.
  const asked = requestedAxes(record);
  for (const t of asked.temperatures) {
    // No temperature is reached for a missing node; `asked` retains its setpoint.
    rowsBy.set(tKey(t), { key: tKey(t), t: null, asked: t, how: 'setpoint', source: '',
      label: fmt.kelvin(t), ran: false });
  }
  for (const v of asked.levels) {
    colsBy.set(ledKey(v), { key: ledKey(v), led_v: v, asked: v,
      label: `LED ${fmt.volts(v, { decimals: 3 })}`, ran: false });
  }
  for (const node of nodes) {
    // Nodes group under the requested setpoint, even when confirmed nearby. The
    // schedule supplies it; without a schedule, the nearest requested temperature
    // within loop tolerance determines the row. Cells retain confirmed values and
    // provenance.
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
        label: finite(node.led_v) ? `LED ${fmt.volts(node.led_v, { decimals: 3 })}` : 'LED not recorded', ran: false });
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
 * The helper reads requested axes from the tree and resolved schedule so they survive
 * missing measurements. Ranges use service-resolved steps rather than local counting.
 * Journal records lack schedules, so their ranges contribute no inferred levels;
 * explicit lists and recorded nodes supply the axes. Temperature-module setpoints
 * come from the tree because step detail can carry only loop or session bindings.
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

/** The cell uses its most recent node's values and retains the total node count. */
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
 * The summary reports Q span, uncertainty, power availability and incomplete cells.
 * Partial cells are excluded from the span and named as excluded so it cannot imply a
 * complete run.
 */
export function summaryModel(grid, record) {
  const present = grid.cells.filter((c) => !c.missing);
  const complete = present.filter((c) => !c.partial && finite(c.q));
  const partial = present.filter((c) => c.partial);
  const missing = grid.cells.filter((c) => c.missing);
  const lines = [];

  const withSigma = present.filter((c) => c.sigmaRecorded);
  if (present.length) {
    const rowsWith = [...new Set(withSigma.map((c) => grid.rows.find((r) => r.key === c.row)).filter(Boolean).map((r) => r.label))];
    if (withSigma.length !== present.length) {
      lines.push({
        key: 'sigma', label: 'σQ',
        text: withSigma.length ? `only at ${rowsWith.join(', ')}` : 'not recorded',
        level: withSigma.length ? '' : 'warn',
      });
    }
  }

  const shots = present.reduce((n, c) => n + (c.node.shots || 0), 0);
  const withIntensity = present.reduce((n, c) => n + (c.node.intensity_recorded || 0), 0);
  if (shots && withIntensity !== shots) {
    lines.push({
      key: 'intensity', label: 'Intensity',
      text: `missing on ${shots - withIntensity} of ${shots} shots`,
      level: 'warn',
    });
  }

  const flagged = present.reduce((n, c) => n + (c.node.shots_flagged || 0), 0);
  const parts = [];
  if (partial.length) parts.push(`${partial.length} partial (${partial.map((c) => cellName(grid, c)).join(', ')})`);
  if (missing.length) parts.push(`${missing.length} never run (${missing.map((c) => cellName(grid, c)).join(', ')})`);
  if (flagged) parts.push(`${flagged} of ${shots} shots flagged by the scope check`);
  const verdicts = (record && record.verdicts) || [];
  const warn = verdicts.filter((v) => v.level === 'warn').length;
  const crit = verdicts.filter((v) => v.level === 'crit').length;
  if (crit) parts.push(`${crit} crit at Start`);
  if (warn) parts.push(`${warn} warn at Start`);
  if (parts.length) {
    lines.push({
      key: 'flags', label: 'Warnings',
      text: parts.join(', '),
      level: partial.length || missing.length || flagged || crit ? 'warn' : '',
    });
  }
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
 * Temperature text includes method and value. An unanswered pause must differ from a
 * confirmed temperature; a simulated source must never read as a measurement.
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
      // Without a requested setpoint, the value is controller read-back at node
      // start.
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

/**
 * V_oc provenance includes its source measurement and LED level (`docs/ui-rules.md`
 * §6).
 */
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
 * Node flags include their reasons. Ordering puts invalidating conditions first,
 * qualifications next and informational records last.
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
    flags.push({ level: 'warn', code: 'sigma', text: 'σQ not recorded · a zero is not a σ: one loop leaves nothing to deviate from' });
  }
  if (shots && node.shots_flagged) {
    flags.push({ level: 'warn', code: 'scope',
      text: `${node.shots_flagged} of ${shots} shots flagged by the scope check — a rail or a shared extreme on a trace; the shot's own line in its card says which. Do not interpret those charges` });
  } else if (shots && node.module === 'bace') {
    flags.push({ level: 'info', code: 'scope', text: `scope check: no shot flagged, ${shots} of ${shots} judged` });
  }
  if (shots) {
    const with_ = node.intensity_recorded || 0;
    if (with_ === 0) flags.push({ level: 'warn', code: 'intensity', text: `intensity null on every shot (${shots}) · the meter did not answer; watts not recorded, mW/cm² never inferred` });
    else if (with_ < shots) flags.push({ level: 'warn', code: 'intensity', text: `intensity null on ${shots - with_} of ${shots} shots · watts where read, mW/cm² never inferred` });
  }
  const t = temperatureText(node);
  if (t.level === 'warn') flags.push({ level: 'warn', code: 'temperature', text: `temperature ${t.text}` });
  const v = vocText(node);
  if (v.mismatch) flags.push({ level: 'warn', code: 'voc', text: `Voc ${v.text} · a Voc from a different illumination is worse than none` });
  else if (node.module === 'bace' && v.level === 'warn') flags.push({ level: 'warn', code: 'voc', text: `Voc ${v.text}` });
  if (node.offset_corrected === false) {
    flags.push({ level: 'info', code: 'baseline', text: 'baseline not subtracted (offset_correct = false) · the dark trace is taken as it came' });
  }
  // Verdicts apply to the run, node or containing loop. Informational `trigger.auto`
  // remains visible because disconnected sync can produce plausible near-zero Q
  // (`docs/ui-rules.md` §9).
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
 * The helper counts layers in `params_as_executed[path]`. Journal records omit them,
 * so `null` means unavailable provenance rather than zero contributing layers.
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
 * Re-queue carries node overrides and loop-supplied LED level, low level and settle
 * time into a manual run, which owns them outside a loop. It excludes V_oc: manual
 * runs use the bench source at the applicable illumination rather than a copied
 * measurement.
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

/**
 * The grid as CSV: one row per cell, the numbers at full precision, the absences empty.
 */
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
