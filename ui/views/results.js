// Results use the run index and selected record to show identities, grid, summary,
// node values, flags, files and provenance (`docs/service-contract.md` §6). They
// never parse filenames or open HDF5. Every selection fetches the same record shape
// for session and journal runs. The stream updates active row state; park triggers
// index refresh and completes the open record. Manual runs retain the same result
// treatment (`docs/ui-rules.md` §8).

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
  title: 'Results',

  mount(container, { store, api, notify }) {
    let rows = [];
    let rowsStatus = 'Loading …';
    let record = null;
    let recordStatus = '';
    let rerunState = null;
    let disposed = false;
    /**
     * Only the latest selection request applies; an earlier response cannot replace a
     * later choice.
     */
    let recordRequest = 0;

    const body = h('div.results');
    fill(container,
      h('h1', 'Results'),
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
      h('div.rleft', gridEl, sumEl, chartEl, h('h3', { title: 'In the order they ran.' }, 'Nodes'), nodesEl),
      h('div.rright', panelEl)));

    // -- fetching --------------------------------------------------------

    async function loadRows() {
      try {
        rows = await api.runs('all');
        rowsStatus = '';
      } catch (error) {
        rowsStatus = `Runs not loaded: ${error.text || error.message}`;
      }
      if (disposed) return;
      if (!selectedRun && rows.length) selectedRun = rows[0].run_id;
      render(store.getState());
      if (selectedRun && (!record || record.run_id !== selectedRun)) loadRecord(selectedRun);
    }

    async function loadRecord(runId) {
      const mine = (recordRequest += 1);
      recordStatus = `Loading ${runId} …`;
      render(store.getState());
      let got = null;
      try {
        got = await api.run(runId);
        recordStatus = '';
      } catch (error) {
        recordStatus = `${runId} not loaded: ${error.text || error.message}`;
      }
      if (disposed || mine !== recordRequest) return;
      if (got) {
        record = got;
        // Refetch preserves node selection in the same run. Selecting another run
        // opens its first grid cell or node.
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
      // Changing run selection clears the prior grid and export button while
      // fetching. Otherwise a failed request could leave another run's results
      // beneath the selected row.
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
     * Re-queue starts one manual module run using node overrides. It does not
     * recreate containing loops, so the caption states the missing temperature and
     * illumination bindings.
     */
    let submitting = false;
    async function rerun(node, params) {
      if (submitting) return;
      submitting = true;
      const overrides = rerunParams(params);
      rerunState = { text: `Starting ${node.module} …` };
      render(store.getState());
      try {
        const out = await api.startRun(node.module, overrides, `again: ${node.node_path || node.module}`);
        rerunState = { text: `Queued as ${out.run_id}, ${out.state}${out.position ? ', position ' + out.position : ''}` };
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
        h('span.cn', 'Runs'),
        h('span.cs', { text: rowsStatus || `${fmt.plural(groups.total, 'run')}, ${fmt.plural(groups.groups.length, 'session')}` }),
        h('span.spacer'),
        h('button.btng', { onclick: () => { rowsStatus = 'Loading …'; render(store.getState()); loadRows(); } }, 'Reload'),
      ]);
      keyed(listBody, JSON.stringify([groups, selectedRun]), () => renderGroups(groups, { selected: selectedRun, onSelect: select }));

      if (!record) {
        keyed(headEl, JSON.stringify(['none', recordStatus, selectedRun]), () => h('div.ch',
          h('span.cn', { text: selectedRun || 'No run' }),
          h('span.cs', { text: recordStatus || '' })));
        for (const el of [gridEl, sumEl, chartEl, nodesEl, panelEl]) keyed(el, 'none', () => []);
        return;
      }
      const grid = gridModel(record);
      const summary = summaryModel(grid, record);
      const node = moduleNodes(record).find((n) => n.node_path === selectedNode) || null;
      const recordKey = JSON.stringify([record.run_id, record.state, record.finished_at, record.kept, Object.keys(record.nodes || {}).length, recordStatus]);

      keyed(headEl, recordKey, () => [
        h('div.ch',
          h('span.cn', 'Open'),
          recordStatus ? h('span.cs', { text: recordStatus }) : null,
          h('span.tag', { text: record.from === 'journal' ? 'journal' : 'this session' }),
          record.data_in_memory === false
            ? h('span.tag', { title: 'Scalars only; the arrays are in the HDF5 file.', text: 'arrays not in memory' }) : null,
          h('span.spacer'),
          grid.shape === 'grid' ? h('button.btng', { onclick: () => exportCsv(grid) }, 'Export CSV') : null,
          h('button.btng', { onclick: () => loadRecord(record.run_id) }, 'Reload')),
        renderRunHead(record, grid, summary),
      ]);
      keyed(gridEl, `${recordKey}|${selectedNode}`, () => renderGrid(grid, { selected: selectedNode, onSelect: selectNode }));
      keyed(sumEl, recordKey, () => (grid.shape === 'grid' ? renderSummary(summary) : []));
      keyed(chartEl, `${recordKey}|${chartBy}`, () => {
        if (grid.shape !== 'grid' || summary.present < 2) return [];
        return [
          h('div.schedbar',
            h('span.cs', 'Plot'),
            h('span.filt', { role: 'group', 'aria-label': 'plot' },
              h('button.opt', { type: 'button', 'aria-pressed': chartBy === 'led' ? 'true' : 'false', class: chartBy === 'led' ? 'on' : '', onclick: () => { chartBy = 'led'; render(store.getState()); } }, 'Q(led_v) per T'),
              h('button.opt', { type: 'button', 'aria-pressed': chartBy === 'T' ? 'true' : 'false', class: chartBy === 'T' ? 'on' : '', onclick: () => { chartBy = 'T'; render(store.getState()); } }, 'Q(T) per led_v'))),
          chart((w, view) => gridChartModel(grid, { by: chartBy, width: w, view }), { id: `grid/${chartBy}` }),
        ];
      });
      keyed(nodesEl, `${recordKey}|${selectedNode}`, () => renderNodes(record, { selected: selectedNode, onSelect: selectNode }));
      keyed(panelEl, `${recordKey}|${selectedNode}|${JSON.stringify(rerunState)}`, () =>
        renderNodePanel(record, node, grid, { onRerun: rerun, rerunState }));
    }

    // -- the store -------------------------------------------------------

    /** The set records park refreshes so each run triggers one reread. */
    const parkedSeen = new Set();
    for (const id of store.getState().order) {
      if (store.getState().runs[id].parked_at) parkedSeen.add(id);
    }
    const off = store.subscribe((state) => {
      // Park refreshes the index and completes the selected record. Other events
      // already update rows through `live`. Detection reads records rather than
      // `lastFrame` because animation-frame batches can place park before their final
      // event.
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
