// The results grid renders history models as a temperature-by-LED table. Each `bace`
// cell shows Q, V_oc and uncertainty. Partial cells have an accent outline, missing
// cells a hatch and selection an ink border, preserving distinctions without colour.
// `dom.keyed` prevents unrelated stream frames from rebuilding the grid during
// reading.

import { h, num } from './dom.js';
import * as fmt from './format.js';
import { moduleTitle } from './fields.js';
import { cellName, flagsFor, identityOf, isPartial, sigmaRecorded, spanOf,
  temperatureText, vocText, cellQ, moduleNodes, GRID_MODULES } from './history.js';

// -- the run list -----------------------------------------------------------

/** One session's group of runs, newest first, with its identities on the heading. */
export function renderGroups(groups, { selected = null, onSelect } = {}) {
  if (!groups.groups.length) {
    return h('p.absent', { title: 'A run appears here once it ends.' }, 'No runs yet');
  }
  return groups.groups.map((group) => h('div.hgroup',
    h('div.hgh',
      h('span.num', { text: `session ${group.session}` }),
      h('span.cs', { text: group.identities.length ? group.identities.join(' / ') : 'No sample named' }),
      h('span.cs.right', { text: fmt.plural(group.runs.length, 'run') })),
    group.runs.map((run) => h('button.hrow', {
      class: (run.run_id === selected ? 'on ' : '') + `lv-${run.level}`,
      dataset: { run: run.run_id },
      onclick: () => onSelect && onSelect(run.run_id),
    },
      h('span.hr1',
        h('span.hname', { text: run.label }),
        h('span', { class: `tag st ${run.level}`, text: run.state || fmt.ABSENT }),
        run.name ? h('span.cs', { text: run.name }) : null),
      h('span.hr2',
        h('span.num', { text: run.at })),
      run.outcome ? h('span.hr3.cs', { text: run.outcome }) : null))));
}

// -- the run header ---------------------------------------------------------

/** The header identifies the run, its time, counts and state. */
export function renderRunHead(record, grid, summary) {
  const identity = identityOf(record.sample);
  const sample = record.sample || {};
  const level = record.state === 'failed' || record.state === 'blocked' ? 'crit'
    : ['stopped', 'aborted', 'cancelled'].includes(record.state) ? 'warn' : record.state === 'done' ? 'ok' : 'live';
  const counts = grid.shape === 'grid'
    ? [summary.partial ? `${summary.partial} partial` : null,
      summary.missing ? `${summary.missing} not run` : null].filter(Boolean).join(', ')
    : '';
  return h('div.rhead',
    h('div.rh1',
      h('span.rname', { text: record.kind === 'pipeline' ? (record.name || 'Sequence') : moduleTitle(record.module || record.kind || 'run') }),
      h('span', { class: `tag st ${level}`, text: record.state || fmt.ABSENT }),
      h('span.num.cs', { text: record.run_id })),
    h('div.rh2',
      h('span', { class: identity ? '' : 'absent', text: identity || 'No sample named' }),
      sample.operator ? h('span.cs', { text: `Operator ${sample.operator}` }) : null,
      h('span.num', { text: spanOf(record.started_at || record.queued_at, record.finished_at) }),
      h('span.num', { text: counts })),
    sample.comment ? h('p.rcomment', { text: sample.comment }) : null,
    record.error ? h('p.warn1.crit', { text: `Failed: ${record.error}` }) : null);
}

// -- the grid ---------------------------------------------------------------

/**
 * The T × LED table. Every row and column that occurs is drawn, every cell of
 * the product is a cell: filled, partial (outlined), or missing (hatched).
 */
export function renderGrid(grid, { selected = null, onSelect } = {}) {
  if (grid.shape !== 'grid') {
    // The empty state points to available results or actions rather than explaining
    // grid internals (`docs/ui-rules.md` §9).
    return h('div.chart-absent', h('p.absent', 'No BACE step'));
  }
  const head = h('tr', h('th.corner', h('span.cs', 'T / K ↓   LED / V →')),
    grid.cols.map((col) => h('th', { text: col.label })));
  const rows = grid.rows.map((row) => {
    const t = temperatureText({ temperature_k: row.t ?? row.asked, temperature_how: row.how, temperature_source: row.source });
    return h('tr',
      h('th', { title: t.text },
        h('span.num', { text: row.label }),
        h('span', { class: `tlv ${t.level}`, text: row.how ? `· ${row.how}${row.source && row.source !== row.how ? '/' + row.source : ''}` : '' }),
        // The cell identifies the requested temperature when the confirmed value
        // differs; a 280 K row can contain an operator-confirmed 280.1 K.
        row.t !== null && row.asked !== null && row.asked !== undefined && Math.abs(row.t - row.asked) > 0.05
          ? h('span.tlv', { text: `· asked ${fmt.kelvin(row.asked)}` }) : null),
      grid.cols.map((col) => {
        const cell = grid.byKey[`${row.key}|${col.key}`];
        return h('td', renderCell(grid, cell, { selected, onSelect }));
      }));
  });
  return h('div.qgrid-wrap',
    h('table.qgrid', h('thead', head), h('tbody', rows)),
    h('div.chart-legend',
      h('span.cs', 'Q / C, Voc / V')));
}

function renderCell(grid, cell, { selected, onSelect }) {
  if (!cell || cell.missing) {
    return h('div.cell.missing', { title: `${cell ? cellName(grid, cell) + ': ' : ''}not run` }, h('span.cs', 'not run'));
  }
  const classes = ['cell'];
  if (cell.partial) classes.push('partial');
  if (cell.outcome === 'failed') classes.push('failed');
  if (cell.path === selected) classes.push('on');
  const sigma = cell.sigma === null ? 'σ —' : `σ ${fmt.scientific(cell.sigma, 2)}`;
  return h('button', {
    class: classes.join(' '), dataset: { cell: cell.path },
    title: `${cellName(grid, cell)} · ${cell.path}`,
    onclick: () => onSelect && onSelect(cell.path),
  },
    h('span.cq.num', { text: fmt.charge(cell.q, { unit: false }) }),
    h('span.cv.num', { text: `${cell.voc === null ? 'Voc —' : fmt.volts(cell.voc, { unit: false })} · ${sigma}` }),
    cell.partial ? h('span.ck.num', { text: `${fmt.keptOf(cell.kept, cell.requested)} kept` }) : null,
    cell.points > 1 ? h('span.ck.cs', { text: `${cell.points} points · ${cell.at}` }) : null,
    cell.repeated > 1 ? h('span.ck.cs', { title: 'Run more than once; the newest is shown.', text: `×${cell.repeated} newest` }) : null);
}

// -- the summary strip ------------------------------------------------------

export function renderSummary(summary) {
  return h('div.rsum', summary.lines.map((line) => h('div.rsl',
    h('span.rsk', { text: line.label }),
    h('span', { class: `rsv ${line.level || ''} ${line.absent ? 'absent' : ''}`, text: line.text }))));
}

// -- the node list ----------------------------------------------------------

/** Every module node of the run in order — the J-Vs too, which have no cell. */
export function renderNodes(record, { selected = null, onSelect } = {}) {
  const nodes = moduleNodes(record);
  if (!nodes.length) return h('p.absent', 'No step has ended');
  return h('div.nlist', nodes.map((node) => {
    const t = temperatureText(node);
    const v = vocText(node);
    const level = node.outcome === 'failed' ? 'crit' : node.outcome === 'ok' ? 'ok' : 'warn';
    const q = GRID_MODULES.has(node.module) ? cellQ(node) : null;
    const curves = node.curves ? fmt.plural(node.curves.length, 'curve') : null;
    return h('button.nrow', {
      class: (node.node_path === selected ? 'on ' : '') + (isPartial(node) ? 'partial' : ''),
      dataset: { node: node.node_path }, onclick: () => onSelect && onSelect(node.node_path),
    },
      h('span.nr1',
        h('span.num.np', { text: moduleTitle(node.module) }),
        h('span', { class: `tag st ${level}`, text: node.outcome || fmt.ABSENT }),
        h('span.num', { text: `${fmt.keptOf(node.kept, node.requested)} ${node.module === 'bace' ? 'shots' : curves ? 'curves' : ''}`.trim() })),
      h('span.nr2.cs',
        h('span', { class: t.level, text: `T ${t.text}` }),
        node.led_v !== null && node.led_v !== undefined ? h('span', { text: `LED ${fmt.volts(node.led_v, { decimals: 3 })}` }) : null,
        node.summary ? h('span', { text: node.summary }) : null));
  }));
}

// -- the selected node ------------------------------------------------------

/**
 * The detail panel shows node values, flags with reasons, files, parameter provenance
 * and Re-queue.
 */
export function renderNodePanel(record, node, grid, { onRerun, rerunState = null } = {}) {
  if (!node) {
    return h('div.npanel', h('p.absent', 'Select a step'));
  }
  const cell = grid.shape === 'grid' ? grid.cells.find((c) => !c.missing && c.path === node.node_path) : null;
  const q = GRID_MODULES.has(node.module) ? cellQ(node) : null;
  const t = temperatureText(node);
  const v = vocText(node);
  const flags = flagsFor(record, node).filter((flag) => flag.level !== 'info');
  const params = record.params_as_executed && record.params_as_executed[node.node_path];
  const partial = isPartial(node);
  const unit = node.module === 'bace' ? 'shots' : 'curves';

  const numbers = [];
  if (q) {
    numbers.push(['Q', q.q === null ? fmt.ABSENT : fmt.charge(q.q), q.points > 1 ? q.at : '']);
    numbers.push(['σQ', q.sigma === null ? fmt.ABSENT : fmt.scientific(q.sigma, 3) + ' C',
      q.sigma === null ? (sigmaRecorded(node) ? 'not at this point' : 'single loop') : '']);
  }
  numbers.push(['Acquired', `${fmt.keptOf(node.kept, node.requested)} ${unit}`, partial ? (node.outcome || 'ended early') : node.outcome || '']);
  if (node.curves && node.curves.length) {
    for (const curve of node.curves) {
      const m = curve.metrics || {};
      numbers.push([curve.label || (curve.dark ? 'dark' : 'light'),
        curve.dark === true ? 'dark' : `Voc ${fmt.volts(m.voc)}, FF ${m.ff === null || m.ff === undefined ? fmt.ABSENT : fmt.sig(m.ff, 3)}`,
        '']);
    }
  }

  return h('div.npanel',
    h('div.nph',
      h('span.rname', { text: `${moduleTitle(node.module)}${cell ? ' ' + cellName(grid, cell) : ''}` }),
      h('span', { class: `tag st ${node.outcome === 'ok' ? 'ok' : node.outcome === 'failed' ? 'crit' : 'warn'}`, text: partial ? `partial · ${fmt.keptOf(node.kept, node.requested)}` : node.outcome || fmt.ABSENT })),
    h('table.rows.nnum', numbers.map(([k, val, note]) => h('tr',
      h('th', { text: k }), h('td', num(val)), h('td.cs', { text: note || '' })))),
    h('table.rows.nprov',
      h('tr', h('th', 'T'), h('td', h('span', { class: t.level, text: t.text }))),
      h('tr', h('th', 'LED'), h('td', num(node.led_v === null || node.led_v === undefined ? fmt.ABSENT : fmt.volts(node.led_v, { decimals: 3 })))),
      h('tr', h('th', 'Voc'), h('td', h('span', { class: v.level, text: v.text }))),
      node.offset_corrected !== null && node.offset_corrected !== undefined
        ? h('tr', h('th', 'Offset corr.'), h('td', { title: 'offset_correct', text: node.offset_corrected ? 'on' : 'off' })) : null,
      node.started_at ? h('tr', h('th', 'Ran'), h('td', num(spanOf(node.started_at, node.finished_at)))) : null),
    // Flags travel with the record, not the folder; the files are listed in
    // manual 3.3.
    h('h3', 'Warnings'),
    h('div.flags', flags.length
      ? flags.map((flag) => h('div', { class: `warn1 ${flag.level}`, title: flag.code }, h('span', { text: flag.text })))
      : h('p.absent', 'None')),
    h('h3', 'Files'),
    h('p.path.num', { text: node.folder || fmt.ABSENT }),
    record.folder && node.folder && node.folder !== record.folder
      ? h('p.cs', { text: `In ${record.folder}` }) : null,
    onRerun ? h('div.btnrow',
      h('button.btns', {
        onclick: () => onRerun(node, params), disabled: !params,
        title: 'Runs this step once more at the present Voc and temperature.',
      }, 'Run again'),
      rerunState ? h('span.cs', { text: rerunState.text }) : null)
      : null);
}

