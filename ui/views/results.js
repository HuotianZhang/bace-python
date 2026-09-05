// The results tab — `docs/ui-plan.md` M6, from R2·3.
//
// Two columns: the runs the journal knows, and the one that is open. The
// left is `GET /runs?session=all`, grouped by session; the right is that
// run's `GET /runs/{id}` — its identity, its T × LED grid with the partial
// cell outlined and the never-run cell hatched, R2·3's summary strip, and
// the selected node's numbers, flags, folder and provenance. Nothing on this
// tab is computed from a folder name, and nothing opens an HDF5: the record
// carries what a grid needs (`docs/naming-plan.md` rule 1, built for this).
//
// Three decisions, each a rule from somewhere:
//
//   * **The record is always asked for, this session's runs included.** The
//     store holds this session's runs as the stream delivered them, but the
//     shape the grid reads — `nodes` with the temperature triple and the
//     per-point statistics — is the record's, the same from this process and
//     from a journal file, and a view that drew this session's runs from one
//     shape and last week's from another would be two views. One fetch per
//     selection, a few kilobytes; the store says *when* to ask again.
//   * **The list moves when a run parks, and not before.** A run in flight is
//     the monitor's business, under the rail on every tab; here it is a row
//     whose state reads off the stream (`live`), and the index is re-read at
//     `parked` — which is when the journal's summary of it is complete.
//   * **A manual run is a result** (`ui-rules` §8): one cell, no grid, the
//     same panel. The grid appears when there is a grid; a bench day of single
//     runs is listed, not apologised for.

import { h, fill, keyed } from '../lib/dom.js';
import * as fmt from '../lib/format.js';
import { chart } from '../lib/charts/frame.js';
import { gridChartModel } from '../lib/charts/grid.js';
import { runGroups, gridModel, summaryModel, moduleNodes, csvOf, rerunParams, GRID_MODULES } from '../lib/history.js';
import { renderGroups, renderRunHead, renderGrid, renderSummary, renderNodes, renderNodePanel } from '../lib/grid.js';

/** Which run and node are open, kept across a tab switch. */
let selectedRun = null;
let selectedNode = null;
let chartBy = 'led';

export default {
  route: 'results',
  title: 'results',

  mount(container, { store, api, notify }) {
    let rows = [];
    let rowsStatus = 'loading the runs …';
    let record = null;
    let recordStatus = '';
    let rerunState = null;
    let disposed = false;
    /** The newest request wins: a click during a fetch must not be answered by the earlier one. */
    let recordRequest = 0;

    const body = h('div.results');
    fill(container,
      h('h1', 'results'),
      body);
    const listEl = h('div.card.hist');
    const openEl = h('div.card.open');
    fill(body, listEl, openEl);

    const listHead = h('div.ch');
    const listBody = h('div.cb');
    fill(listEl, listHead, listBody);
    const headEl = h('div.rhd');
    const gridEl = h('div.rgrid');
    const sumEl = h('div');
    const chartEl = h('div.rchart');
    const nodesEl = h('div');
    const panelEl = h('div');
    fill(openEl, headEl, h('div.cb.rbody',
      h('div.rleft', gridEl, sumEl, chartEl, h('h3', 'nodes, in the order they ran'), nodesEl),
      h('div.rright', panelEl)));

    // -- fetching --------------------------------------------------------

    async function loadRows() {
      try {
        rows = await api.runs('all');
        rowsStatus = '';
      } catch (error) {
        rowsStatus = `could not load the runs · ${error.text || error.message}`;
      }
      if (disposed) return;
      if (!selectedRun && rows.length) selectedRun = rows[0].run_id;
      render(store.getState());
      if (selectedRun && (!record || record.run_id !== selectedRun)) loadRecord(selectedRun);
    }

    async function loadRecord(runId) {
      const mine = (recordRequest += 1);
      recordStatus = `loading ${runId} …`;
      render(store.getState());
      let got = null;
      try {
        got = await api.run(runId);
        recordStatus = '';
      } catch (error) {
        recordStatus = `could not load ${runId} · ${error.text || error.message}`;
      }
      if (disposed || mine !== recordRequest) return;
      if (got) {
        record = got;
        // The node stays selected across a re-read of the same run; a new run
        // opens on its first grid cell, or its first node.
        const nodes = moduleNodes(record);
        if (!nodes.some((n) => n.node_path === selectedNode)) {
          const cell = nodes.find((n) => GRID_MODULES.has(n.module));
          selectedNode = (cell || nodes[0] || {}).node_path ?? null;
        }
      }
      render(store.getState());
    }

    function select(runId) {
      if (runId === selectedRun && record && record.run_id === runId) return;
      selectedRun = runId;
      rerunState = null;
      // The previous run's grid and export button must not stay under the
      // newly highlighted row while its record is asked for -- or for ever,
      // if the request fails. Nothing on the right until the answer.
      record = null;
      loadRecord(runId);
    }

    function selectNode(path) {
      selectedNode = path;
      rerunState = null;
      render(store.getState());
    }

    // -- actions ---------------------------------------------------------

    /**
     * R2·3's "Re-queue this cell": one manual run of the node's module with
     * its own overrides. Not a pipeline: the temperature and the level were a
     * loop's to bind, and a manual run has no loop — the caption says so.
     */
    let submitting = false;
    async function rerun(node, params) {
      if (submitting) return;
      submitting = true;
      const overrides = rerunParams(params);
      rerunState = { text: `starting ${node.module} …` };
      render(store.getState());
      try {
        const out = await api.startRun(node.module, overrides, `again: ${node.node_path || node.module}`);
        rerunState = { text: `queued as ${out.run_id} · ${out.state}${out.position ? ' · position ' + out.position : ''}` };
      } catch (error) {
        rerunState = { text: error.text || error.message, checks: error.checks };
        notify(error.text || error.message, error.level === 'crit' ? 'crit' : 'warn', error.checks);
      } finally {
        submitting = false;
      }
      render(store.getState());
    }

    function exportCsv(grid) {
      const text = csvOf(grid, record);
      const blob = new Blob([text], { type: 'text/csv' });
      const a = h('a', { href: URL.createObjectURL(blob), download: `${record.run_id}_grid.csv` });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    }

    // -- rendering -------------------------------------------------------

    function render(state) {
      const live = {};
      for (const id of state.order) {
        const run = state.runs[id];
        if (run && run.state) live[id] = { state: run.state };
      }
      const groups = runGroups(rows, { live });
      keyed(listHead, JSON.stringify([groups.total, rowsStatus]), () => [
        h('span.cn', 'runs'),
        h('span.cs', { text: rowsStatus || `${groups.total} in the journal · ${groups.groups.length} session${groups.groups.length === 1 ? '' : 's'}` }),
        h('span.spacer'),
        h('button.btng', { onclick: () => { rowsStatus = 'asking again …'; render(store.getState()); loadRows(); } }, 'reload'),
      ]);
      keyed(listBody, JSON.stringify([groups, selectedRun]), () => renderGroups(groups, { selected: selectedRun, onSelect: select }));

      if (!record) {
        keyed(headEl, JSON.stringify(['none', recordStatus, selectedRun]), () => h('div.ch',
          h('span.cn', { text: selectedRun || 'no run' }),
          h('span.cs', { text: recordStatus || (rows.length ? '' : 'nothing to open yet') })));
        for (const el of [gridEl, sumEl, chartEl, nodesEl, panelEl]) keyed(el, 'none', () => []);
        return;
      }
      const grid = gridModel(record);
      const summary = summaryModel(grid, record);
      const node = moduleNodes(record).find((n) => n.node_path === selectedNode) || null;
      const recordKey = JSON.stringify([record.run_id, record.state, record.finished_at, record.kept, Object.keys(record.nodes || {}).length, recordStatus]);

      keyed(headEl, recordKey, () => [
        h('div.ch',
          h('span.cn', 'open'),
          h('span.cs', { text: recordStatus || `${record.from === 'journal' ? 'from the journal' : 'from this session'}${record.data_in_memory === false ? ' · arrays not in memory' : ''}` }),
          h('span.spacer'),
          grid.shape === 'grid' ? h('button.btng', { onclick: () => exportCsv(grid) }, 'export grid · csv') : null,
          h('button.btng', { onclick: () => loadRecord(record.run_id) }, 'reload')),
        renderRunHead(record, grid, summary),
      ]);
      keyed(gridEl, `${recordKey}|${selectedNode}`, () => renderGrid(grid, { selected: selectedNode, onSelect: selectNode }));
      keyed(sumEl, recordKey, () => (grid.shape === 'grid' ? renderSummary(summary) : []));
      keyed(chartEl, `${recordKey}|${chartBy}`, () => {
        if (grid.shape !== 'grid' || summary.present < 2) return [];
        return [
          h('div.schedbar',
            h('span.cs', 'plot'),
            h('span.filt', { role: 'group', 'aria-label': 'plot' },
              h('button.opt', { type: 'button', 'aria-pressed': chartBy === 'led' ? 'true' : 'false', class: chartBy === 'led' ? 'on' : '', onclick: () => { chartBy = 'led'; render(store.getState()); } }, 'Q(led_v) per T'),
              h('button.opt', { type: 'button', 'aria-pressed': chartBy === 'T' ? 'true' : 'false', class: chartBy === 'T' ? 'on' : '', onclick: () => { chartBy = 'T'; render(store.getState()); } }, 'Q(T) per led_v'))),
          chart((w) => gridChartModel(grid, { by: chartBy, width: w })),
        ];
      });
      keyed(nodesEl, `${recordKey}|${selectedNode}`, () => renderNodes(record, { selected: selectedNode, onSelect: selectNode }));
      keyed(panelEl, `${recordKey}|${selectedNode}|${JSON.stringify(rerunState)}`, () =>
        renderNodePanel(record, node, grid, { onRerun: rerun, rerunState }));
    }

    // -- the store -------------------------------------------------------

    /** Which runs this view has seen park, so each park re-reads once. */
    const parkedSeen = new Set();
    for (const id of store.getState().order) {
      if (store.getState().runs[id].parked_at) parkedSeen.add(id);
    }
    const off = store.subscribe((state) => {
      // A run parking is the index changing and, when it is the open run,
      // the record completing. Everything else on the stream is a state a
      // row reads off `live`, which `render` already does. Read off the
      // records rather than off `lastFrame`: the store folds a batch per
      // animation frame, and the frame that parked a run is rarely the last
      // one in its batch.
      let refetch = false;
      for (const id of state.order) {
        const run = state.runs[id];
        if (!run || !run.parked_at || parkedSeen.has(id)) continue;
        parkedSeen.add(id);
        refetch = true;
        if (record && record.run_id === id) loadRecord(id);
      }
      if (refetch) loadRows();
      render(state);
    });

    loadRows();
    return {
      dispose() {
        disposed = true;
        off();
      },
    };
  },
};
