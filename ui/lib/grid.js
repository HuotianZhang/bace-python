// The results tab's DOM — R2·3 drawn from `lib/history.js`'s models.
//
// The grid is a table because it is one: temperatures down, LED levels
// across, one cell per `bace` node with its Q and, beneath it, the V_oc it
// was centred on and its σ. R2·3's three cell states are three classes and
// nothing else — `partial` is outlined in the accent, `missing` is hatched,
// and the selected cell carries the ink border — so a reader can tell them
// apart with the colour turned off.
//
// Everything here is built from a model and keyed on it (`dom.keyed`), for
// the reason every other renderer in the shell is: the store notifies on
// every frame, and a run finishing in the background must not tear down the
// grid the operator is reading.

import { h, num } from './dom.js';
import * as fmt from './format.js';
import { cellName, flagsFor, identityOf, isPartial, provenanceSummary, sigmaRecorded, spanOf,
  temperatureText, vocText, cellQ, moduleNodes, GRID_MODULES } from './history.js';

// -- the run list -----------------------------------------------------------

/** One session's group of runs, newest first, with its identities on the heading. */
export function renderGroups(groups, { selected = null, onSelect } = {}) {
  if (!groups.groups.length) {
    return h('p.absent', 'no run in the journal yet · a run parks and appears here, tagged manual or pipeline');
  }
  return groups.groups.map((group) => h('div.hgroup',
    h('div.hgh',
      h('span.num', { text: `session ${group.session}` }),
      h('span.cs', { text: group.identities.length ? group.identities.join(' / ') : 'no sample named' }),
      h('span.cs.right', { text: `${group.runs.length} run${group.runs.length === 1 ? '' : 's'}` })),
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
        h('span.num', { text: run.at }),
        h('span.num', { text: run.counts }),
        run.voc ? h('span.num', { text: run.voc }) : null,
        run.nodes ? h('span.num', { text: `${run.nodes} module run${run.nodes === 1 ? '' : 's'}` }) : null),
      run.outcome ? h('span.hr3.cs', { text: run.outcome }) : null))));
}

// -- the run header ---------------------------------------------------------

/** R2·3's top line: who, when, how many, and the state. */
export function renderRunHead(record, grid, summary) {
  const identity = identityOf(record.sample);
  const sample = record.sample || {};
  const nodes = moduleNodes(record);
  const level = record.state === 'failed' || record.state === 'blocked' ? 'crit'
    : ['stopped', 'aborted', 'cancelled'].includes(record.state) ? 'warn' : record.state === 'done' ? 'ok' : 'live';
  const counts = grid.shape === 'grid'
    ? `${summary.present} bace cell${summary.present === 1 ? '' : 's'} · ${summary.complete} complete · ${summary.partial} partial · ${summary.missing} never run`
    : `${nodes.length} module node${nodes.length === 1 ? '' : 's'}`;
  return h('div.rhead',
    h('div.rh1',
      h('span.rname', { text: record.kind === 'pipeline' ? (record.name || 'pipeline') : (record.module || record.kind || 'run') }),
      h('span', { class: `tag st ${level}`, text: record.state || fmt.ABSENT }),
      h('span.num.cs', { text: record.run_id })),
    h('div.rh2',
      h('span', { class: identity ? '' : 'absent', text: identity || 'no sample named' }),
      sample.operator ? h('span.cs', { text: `operator ${sample.operator}` }) : null,
      h('span.num', { text: spanOf(record.started_at || record.queued_at, record.finished_at) }),
      h('span.num', { text: counts }),
      h('span.num.cs', { text: `${fmt.keptOf(record.kept, record.requested)} kept` })),
    sample.comment ? h('p.rcomment', { text: sample.comment }) : null,
    record.error ? h('p.warn1.crit', { text: `failed · ${record.error}` }) : null);
}

// -- the grid ---------------------------------------------------------------

/**
 * The T × LED table. Every row and column that occurs is drawn, every cell of
 * the product is a cell: filled, partial (outlined), or missing (hatched).
 */
export function renderGrid(grid, { selected = null, onSelect } = {}) {
  if (grid.shape !== 'grid') {
    return h('div.chart-absent', h('p.absent', 'no bace node in this run — the grid is one cell per bace; the nodes are listed below'));
  }
  const head = h('tr', h('th.corner', h('span.cs', 'T / K ↓  ·  led_v →')),
    grid.cols.map((col) => h('th', { text: col.label })));
  const rows = grid.rows.map((row) => {
    const t = temperatureText({ temperature_k: row.t, temperature_how: row.how, temperature_source: row.source });
    return h('tr',
      h('th', { title: t.text },
        h('span.num', { text: row.label }),
        h('span', { class: `tlv ${t.level}`, text: row.how ? `· ${row.how}${row.source && row.source !== row.how ? '/' + row.source : ''}` : '' })),
      grid.cols.map((col) => {
        const cell = grid.byKey[`${row.key}|${col.key}`];
        return h('td', renderCell(grid, cell, { selected, onSelect }));
      }));
  });
  return h('div.qgrid-wrap',
    h('table.qgrid', h('thead', head), h('tbody', rows)),
    h('div.chart-legend',
      h('span.legend-item', h('span.mk.filled'), h('span', 'σ_Q measured')),
      h('span.legend-item', h('span.mk.hollow'), h('span', 'σ_Q not recorded')),
      h('span.legend-item', h('span.mk.partial'), h('span', 'partial · kept of requested')),
      h('span.legend-item', h('span.mk.missing'), h('span', 'never run')),
      h('span.cs', 'Q / C · V_oc / V beneath')));
}

function renderCell(grid, cell, { selected, onSelect }) {
  if (!cell || cell.missing) {
    return h('div.cell.missing', { title: `${cell ? cellName(grid, cell) : ''} · never run` }, h('span.cs', 'never run'));
  }
  const classes = ['cell'];
  if (cell.partial) classes.push('partial');
  if (cell.outcome === 'failed') classes.push('failed');
  if (cell.path === selected) classes.push('on');
  const sigma = cell.sigma === null ? '□' : `● σ ${fmt.scientific(cell.sigma, 2)}`;
  return h('button', {
    class: classes.join(' '), dataset: { cell: cell.path },
    title: `${cellName(grid, cell)} · ${cell.path}`,
    onclick: () => onSelect && onSelect(cell.path),
  },
    h('span.cq.num', { text: fmt.charge(cell.q, { unit: false }) }),
    h('span.cv.num', { text: `${cell.voc === null ? 'V_oc —' : fmt.volts(cell.voc, { unit: false })} · ${sigma}` }),
    cell.partial ? h('span.ck.num', { text: `${fmt.keptOf(cell.kept, cell.requested)} kept` }) : null,
    cell.points > 1 ? h('span.ck.cs', { text: `${cell.points} points · ${cell.at}` }) : null,
    cell.repeated > 1 ? h('span.ck.cs', { text: `×${cell.repeated}, newest shown` }) : null);
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
  if (!nodes.length) return h('p.absent', 'no module node has ended in this run');
  return h('div.nlist', nodes.map((node) => {
    const t = temperatureText(node);
    const v = vocText(node);
    const level = node.outcome === 'failed' ? 'crit' : node.outcome === 'ok' ? 'ok' : 'warn';
    const q = GRID_MODULES.has(node.module) ? cellQ(node) : null;
    const curves = node.curves ? `${node.curves.length} curve${node.curves.length === 1 ? '' : 's'}` : null;
    return h('button.nrow', {
      class: (node.node_path === selected ? 'on ' : '') + (isPartial(node) ? 'partial' : ''),
      dataset: { node: node.node_path }, onclick: () => onSelect && onSelect(node.node_path),
    },
      h('span.nr1',
        h('span.num.np', { text: node.node_path || node.module }),
        h('span', { class: `tag st ${level}`, text: node.outcome || fmt.ABSENT }),
        h('span.num', { text: `${fmt.keptOf(node.kept, node.requested)} ${node.module === 'bace' ? 'shots' : curves ? 'curves' : ''}`.trim() })),
      h('span.nr2.cs',
        h('span', { class: t.level, text: `T ${t.text}` }),
        node.led_v !== null && node.led_v !== undefined ? h('span', { text: `led_v ${fmt.volts(node.led_v, { decimals: 3 })}` }) : null,
        q && q.q !== null ? h('span.num', { text: `Q ${fmt.charge(q.q)}${q.sigma !== null ? ' ± ' + fmt.scientific(q.sigma, 2) : ''}` }) : null,
        node.summary ? h('span', { text: node.summary }) : null));
  }));
}

// -- the selected node ------------------------------------------------------

/**
 * R2·3's right-hand column: the cell's numbers, its flags with their reasons,
 * where the files are, what its parameters resolved from, and Re-queue.
 */
export function renderNodePanel(record, node, grid, { onRerun, rerunState = null } = {}) {
  if (!node) {
    return h('div.npanel', h('p.absent', 'select a cell, or a node in the list, to see its numbers and its flags'));
  }
  const cell = grid.shape === 'grid' ? grid.cells.find((c) => !c.missing && c.path === node.node_path) : null;
  const q = GRID_MODULES.has(node.module) ? cellQ(node) : null;
  const t = temperatureText(node);
  const v = vocText(node);
  const flags = flagsFor(record, node);
  const params = record.params_as_executed && record.params_as_executed[node.node_path];
  const provenance = provenanceSummary(params);
  const partial = isPartial(node);
  const unit = node.module === 'bace' ? 'shots' : 'curves';

  const numbers = [];
  if (q) {
    numbers.push(['Q', q.q === null ? fmt.ABSENT : fmt.charge(q.q), q.q === null ? '' : `mean of ${node.kept ?? '?'} ${q.points > 1 ? '· ' + q.at : ''}`]);
    numbers.push(['σ_Q', q.sigma === null ? 'not recorded' : fmt.scientific(q.sigma, 3) + ' C',
      q.sigma === null ? (sigmaRecorded(node) ? 'not at this point' : 'a zero is not a σ') : `over ${node.kept ?? '?'} loops`]);
  }
  numbers.push(['acquired', `${fmt.keptOf(node.kept, node.requested)} ${unit}`, partial ? (node.outcome || 'ended early') : node.outcome || '']);
  if (node.curves && node.curves.length) {
    for (const curve of node.curves) {
      const m = curve.metrics || {};
      numbers.push([curve.label || (curve.dark ? 'dark' : 'light'),
        curve.dark === true ? 'dark' : `V_oc ${fmt.volts(m.voc)} · FF ${m.ff === null || m.ff === undefined ? fmt.ABSENT : fmt.sig(m.ff, 3)}`,
        'derived — interpolated from the curve']);
    }
  }

  return h('div.npanel',
    h('div.nph',
      h('span.rname', { text: `${node.module} · ${cell ? cellName(grid, cell) : node.node_path || 'this run'}` }),
      h('span', { class: `tag st ${node.outcome === 'ok' ? 'ok' : node.outcome === 'failed' ? 'crit' : 'warn'}`, text: partial ? `partial · ${fmt.keptOf(node.kept, node.requested)}` : node.outcome || fmt.ABSENT })),
    h('table.rows.nnum', numbers.map(([k, val, note]) => h('tr',
      h('th', { text: k }), h('td', num(val)), h('td.cs', { text: note || '' })))),
    h('table.rows.nprov',
      h('tr', h('th', 'T'), h('td', h('span', { class: t.level, text: t.text }))),
      h('tr', h('th', 'led_v'), h('td', num(node.led_v === null || node.led_v === undefined ? fmt.ABSENT : fmt.volts(node.led_v, { decimals: 3 })))),
      h('tr', h('th', 'V_oc'), h('td', h('span', { class: v.level, text: v.text }))),
      node.offset_corrected !== null && node.offset_corrected !== undefined
        ? h('tr', h('th', 'baseline'), h('td', { text: node.offset_corrected ? 'dark baseline subtracted (offset_correct)' : 'not subtracted' })) : null,
      node.started_at ? h('tr', h('th', 'ran'), h('td', num(spanOf(node.started_at, node.finished_at)))) : null),
    h('h3', 'flags on this node'),
    h('p.cs', 'observations · they travel with the record, not with the folder'),
    h('div.flags', flags.length
      ? flags.map((flag) => h('div', { class: `warn1 ${flag.level}` }, h('span.code', { text: flag.code }), h('span', { text: flag.text })))
      : h('p.absent', 'nothing flagged')),
    h('h3', 'where it is'),
    h('p.path.num', { text: node.folder || 'no folder recorded — the node wrote nothing' }),
    record.folder && node.folder && node.folder !== record.folder
      ? h('p.cs', { text: `inside the pipeline's ${record.folder}` }) : null,
    h('p.cs', node.module === 'bace'
      ? 'the recorder writes the HDF5 with /metadata, the legacy .dat, and every trace it kept; the journal line for this node is in the session file'
      : 'the recorder writes the HDF5 with /metadata and the curves; the journal line for this node is in the session file'),
    h('h3', 'resolved from'),
    provenance
      ? h('p.num', { text: provenance.text })
      : h('p.absent', record.from === 'journal'
        ? 'the layers were not kept: a run read back from the journal has its values, not their provenance'
        : 'no parameters recorded for this node'),
    onRerun ? h('div.btnrow',
      h('button.btns', { onclick: () => onRerun(node, params), disabled: !params }, `Run ${node.module} again`),
      h('span.cs', { text: rerunState ? rerunState.text : `one manual ${node.module} on the queue now, with this node's own overrides${params && params.led_v && params.led_v.source === 'inherited' ? ' and its led_v' : ''} — at the bench's V_oc and temperature as they are, because a manual run has no loop to bind them` }))
      : null);
}

