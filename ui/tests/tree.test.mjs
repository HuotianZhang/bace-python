// The pipeline tree and the schedule it becomes — `docs/ui-plan.md` M5 as
// assertions, against two recorded `POST /pipelines/validate` answers.
//
// The fixtures are the point. `validate_txill_sim.json` is the canonical tree
// the milestone has to prove — 9 temperatures × 5 levels, 90 module runs,
// 4500 shots — and `validate_bound_sim.json` is the two shapes it has none
// of: a `temperature` **module**, whose setpoint binds the rest of the run
// rather than the rest of one iteration, and duplicate sibling modules, which
// the service spells `bace` and `bace#2` and which this file must match to
// their tree nodes **without** spelling either.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  LOOPS, canSwitchForm, checkSummary, costModel, gridModel, insertAt, loopValues,
  moveAt, newLoop, newModule, nodeAt, remapPath, removeAt, sameTree, scheduleLeaves,
  scheduleTree, setField, setParam, setValueForm, structureSummary, timeline,
  treeRows, valueForm,
} from '../lib/tree.js';
import { UNKNOWN_PX, scheduleModel } from '../lib/charts/schedule.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const load = (name) => JSON.parse(fs.readFileSync(path.join(here, '..', 'fixtures', name), 'utf8'));
const TXILL = load('validate_txill_sim.json');
const BOUND = load('validate_bound_sim.json');
const NESTED = load('validate_nested_sim.json');

/** The canonical tree, as `docs/service-contract.md` §7 spells it. */
const canonical = () => ({
  kind: 'loop', loop: 'temperature', label: 'T',
  values_k: [295, 290, 280, 270, 260, 250, 240, 230, 220],
  tolerance_k: 0.2, hold_s: 60, timeout_s: 1800,
  children: [{
    kind: 'loop', loop: 'illumination',
    led_start_v: 1.010, led_stop_v: 1.030, led_step_v: 0.005,
    led_low_v: 0.4, led_settle_s: 2.0,
    children: [
      { kind: 'module', module: 'jv_bace', params: {} },
      { kind: 'module', module: 'bace', params: { n_loops: 100 } },
    ],
  }],
});

// -- the schedule, nested ---------------------------------------------------

test('the flat schedule nests back into the tree it was a walk of', () => {
  const nodes = scheduleTree(TXILL.schedule);
  assert.equal(nodes.length, 9, 'nine temperature iterations at the top');
  assert.equal(nodes[0].loop, 'temperature');
  assert.equal(nodes[0].value, 295);
  assert.equal(nodes[0].children.length, 5, 'five levels under each');
  assert.deepEqual(nodes[0].children[0].children.map((c) => c.module), ['jv_bace', 'bace']);
  assert.equal(scheduleLeaves(nodes).length, 90, 'ninety module runs');
  assert.equal(TXILL.counters.modules, 90, 'and the service agrees');
});

test('a loop carries what is measured inside it, at its own scale', () => {
  const [first] = scheduleTree(TXILL.schedule);
  assert.equal(first.modules, 10, '5 levels × 2 modules');
  assert.equal(first.shots, 500, '5 × 100 loops of one point');
  // 5 × (18.2 jv_bace + 80 bace + 2 led settle), which is exactly what the
  // cost model reports for this temperature — the numbers on the schedule
  // and the numbers under it are the same numbers.
  assert.ok(Math.abs(first.measure_s - TXILL.cost.per_temperature[0].measure_s) < 1e-6);
});

test('the counters are three scales, and the middle one is not 45', () => {
  // §5: "step 412 of 8400 is useless here". The schedule says which
  // temperature, which level, how far into the scan — never one number.
  assert.deepEqual(TXILL.counters,
    { temperatures: 9, levels: 5, modules: 90, shots: 4500 });
  const nodes = scheduleTree(TXILL.schedule);
  assert.equal(nodes.length, TXILL.counters.temperatures);
  assert.equal(nodes[0].children.length, TXILL.counters.levels,
    'levels is the loop\'s length, not the times it is entered');
  assert.equal(nodes.flatMap((n) => n.children).length, 45, 'which it is entered 45 times');
});

// -- the temperature in force ----------------------------------------------

test('a temperature loop stamps its own setpoint, and agrees with the resolver', () => {
  const leaves = scheduleLeaves(scheduleTree(TXILL.schedule));
  for (const leaf of leaves) {
    assert.equal(leaf.temperature.how, 'loop');
    assert.equal(leaf.temperature.k, leaf.detail.temperature_k,
      `${leaf.node_path} must agree with what the resolver put in its detail`);
  }
});

test('a temperature module binds the rest of the run, not the rest of one iteration', () => {
  // This is the whole reason the walk exists. `Step.detail.temperature_k` is
  // null on every one of these steps — the resolver only knows about loops —
  // so a console that drew the schedule from the schedule alone would show a
  // run with no temperature anywhere on it.
  for (const step of BOUND.schedule) {
    assert.equal(step.detail.temperature_k ?? null, null,
      'the resolver says nothing about a temperature module');
  }
  const leaves = scheduleLeaves(scheduleTree(BOUND.schedule));
  const at = Object.fromEntries(leaves.map((l) => [l.node_path, l.temperature && l.temperature.k]));
  assert.equal(at['rep=1/temperature'], null, 'nothing is bound before it settles');
  assert.equal(at['rep=1/bace'], 250, 'and everything after it is');
  assert.equal(at['rep=2/temperature'], 250,
    'including the next iteration of the loop enclosing it — the cryostat did not move');
  assert.equal(at['rep=2/bace#2'], 250);
  for (const leaf of leaves.slice(1)) assert.equal(leaf.temperature.how, 'module');
});

// -- the rows, and matching a tree node to its schedule entries -------------

test('a tree node finds its schedule entries by counting iterations, not by spelling paths', () => {
  const nodes = scheduleTree(TXILL.schedule);
  const rows = treeRows(canonical(), TXILL.tree, nodes);
  assert.deepEqual(rows.map((r) => r.path.join('/')), ['', '0', '0/0', '0/1']);
  assert.deepEqual(rows.map((r) => r.iterations.length), [9, 45, 45, 45]);
  assert.equal(rows[3].module, 'bace');
  assert.equal(rows[3].shots, 4500, 'the bace node is 45 runs of 100 shots');
  assert.equal(rows[2].shots, 0, 'a J-V takes curves, not shots');
});

test('duplicate siblings land on the right rows, and this file never spells `bace#2`', () => {
  const nodes = scheduleTree(BOUND.schedule);
  const rows = treeRows(BOUND.tree, BOUND.tree, nodes);
  const modules = rows.filter((r) => r.kind === 'module');
  assert.deepEqual(modules.map((r) => r.module), ['temperature', 'bace', 'bace', 'wait']);
  // The two `bace` nodes are different nodes with different overrides, and
  // the schedule numbers them apart. Matching them up wrongly would put the
  // second node's shots on the first's row, which is M0's own warning for
  // this milestone: `loop:index` is not an identity across nodes.
  assert.deepEqual(modules.map((r) => r.shots), [0, 12, 6, 0]);
  assert.deepEqual(modules[1].iterations.map((i) => i.node_path), ['rep=1/bace', 'rep=2/bace']);
  assert.deepEqual(modules[2].iterations.map((i) => i.node_path), ['rep=1/bace#2', 'rep=2/bace#2']);
  // And the match survives paths this file could not have spelled: the same
  // schedule with every node path rewritten still lands on the same rows,
  // because the walk counts iterations and reads `detail.count`.
  const renamed = BOUND.schedule.map((step) => ({ ...step, node_path: step.node_path.replace(/[a-z]/g, 'x') }));
  const shuffled = treeRows(BOUND.tree, BOUND.tree, scheduleTree(renamed))
    .filter((r) => r.kind === 'module');
  assert.deepEqual(shuffled.map((r) => r.shots), [0, 12, 6, 0]);
});

test('a module row carries the ParamSet the schedule resolved for it, provenance included', () => {
  const rows = treeRows(canonical(), TXILL.tree, scheduleTree(TXILL.schedule));
  const bace = rows[3];
  assert.equal(bace.params.led_v.source, 'inherited');
  assert.equal(bace.params.led_v.detail, 'illumination loop');
  assert.equal(bace.params.led_v.value, 1.010, 'the first iteration\'s level');
  assert.equal(bace.params.n_loops.value, 100);
  assert.equal(bace.params.n_loops.detail, 'pipeline node');
  assert.equal(bace.voc.how, 'jv_bace', 'and which measurement will supply its V_oc');
  assert.equal(bace.voc.led_v, 1.010, 'at the level it is measured at — §6\'s coupling');
});

// -- counts are the service's ----------------------------------------------

test('a range has no count until the service has answered', () => {
  const typed = canonical();
  const before = treeRows(typed);
  assert.equal(before[1].values, null, 'nothing rounds 1.010 → 1.030 step 0.005 here');
  assert.match(before[1].summary, /1\.010 → 1\.030 V step 0\.005/);
  const after = treeRows(typed, TXILL.tree, scheduleTree(TXILL.schedule));
  assert.deepEqual(after[1].values, [1.01, 1.015, 1.02, 1.025, 1.03], 'five, because it said so');
  assert.match(after[1].summary, /^1\.010 → 1\.030 V · 5/);
});

test('the temperature list summarises as the artboard writes it', () => {
  const rows = treeRows(canonical(), TXILL.tree, scheduleTree(TXILL.schedule));
  assert.equal(rows[0].summary, '295.0 → 220.0 K · 9 · tol 0.2');
  assert.equal(rows[0].owns, LOOPS.temperature.owns);
});

test('structureSummary counts the tree, not the runs', () => {
  assert.deepEqual(structureSummary(canonical()), { loops: 2, modules: 2 });
});

// -- editing ----------------------------------------------------------------

test('every edit returns a new tree and leaves the old one alone', () => {
  const before = canonical();
  const frozen = JSON.stringify(before);
  const added = insertAt(before, [0], newModule('wait'));
  assert.equal(JSON.stringify(before), frozen, 'the tree it was given is untouched');
  assert.equal(added.children[0].children.length, 3);
  assert.equal(nodeAt(added, [0, 2]).module, 'wait');

  const moved = moveAt(added, [0, 2], -1);
  assert.deepEqual(moved.children[0].children.map((c) => c.module), ['jv_bace', 'wait', 'bace']);
  assert.equal(moveAt(moved, [0, 0], -1), moved, 'a move off the end is not a move');

  const removed = removeAt(moved, [0, 1]);
  assert.deepEqual(removed.children[0].children.map((c) => c.module), ['jv_bace', 'bace']);
  assert.equal(removeAt(removed, []), null, 'removing the root is a pipeline with no nodes');
});

test('a null param override removes the key, which is what a reset means', () => {
  const typed = setParam(canonical(), [0, 1], 'n_averages', 4);
  assert.deepEqual(nodeAt(typed, [0, 1]).params, { n_loops: 100, n_averages: 4 });
  const back = setParam(typed, [0, 1], 'n_averages', null);
  assert.deepEqual(nodeAt(back, [0, 1]).params, { n_loops: 100 });
  assert.ok(!('n_averages' in nodeAt(back, [0, 1]).params),
    'gone, not stored as null — a null in a tree is a value the service would coerce');
});

test('a loop field cleared falls back to the service default rather than to zero', () => {
  const typed = setField(canonical(), [], 'hold_s', null);
  assert.ok(!('hold_s' in typed), 'the key is gone');
  assert.equal(setField(typed, [], 'hold_s', 120).hold_s, 120);
});

test('the list/range switch keeps the values the service made, not the rule that made them', () => {
  const typed = canonical();
  assert.equal(valueForm(typed.children[0]), 'range');
  assert.equal(valueForm(typed), 'list');
  const asList = setValueForm(typed, [0], 'list', TXILL.tree.children[0]);
  const loop = nodeAt(asList, [0]);
  assert.deepEqual(loop.levels_v, [1.01, 1.015, 1.02, 1.025, 1.03]);
  assert.ok(!('led_start_v' in loop), 'and the range that made them is gone');
  assert.equal(valueForm(loop), 'list');
  assert.deepEqual(loopValues(loop, null), [1.01, 1.015, 1.02, 1.025, 1.03],
    'a typed list needs no service to be read');

  const back = setValueForm(asList, [0], 'range', loop);
  assert.equal(nodeAt(back, [0]).led_start_v, 1.01);
  assert.equal(nodeAt(back, [0]).led_stop_v, 1.03);
  assert.ok(Math.abs(nodeAt(back, [0]).led_step_v - 0.005) < 1e-9,
    'the step is read off the values, never invented');
});

test('a form switch never invents values, and never loses the ones typed', () => {
  // Only the service expands a range, so a range typed a moment ago and not
  // yet validated has no levels anywhere. Converting it would mean making
  // some up; the first cut made an empty list, which lost the operator's
  // sweep on a click meant to change how it is written.
  const fresh = canonical();
  assert.equal(canSwitchForm(nodeAt(fresh, [0]), null, 'list').ok, false, 'a range with no answer');
  assert.match(canSwitchForm(nodeAt(fresh, [0]), null, 'list').why, /has not been checked yet/);
  assert.equal(setValueForm(fresh, [0], 'list', null), fresh, 'so the tree is left alone');

  // With the answer in hand it converts, and to the levels the service made.
  const resolved = TXILL.tree.children[0];
  assert.equal(canSwitchForm(nodeAt(fresh, [0]), resolved, 'list').ok, true);
  const asList = setValueForm(fresh, [0], 'list', resolved);
  assert.deepEqual(nodeAt(asList, [0]).levels_v, [1.01, 1.015, 1.02, 1.025, 1.03]);
  assert.ok(!('led_start_v' in nodeAt(asList, [0])), 'and the range that made them is gone');

  // And back, because those five *are* evenly spaced.
  const back = setValueForm(asList, [0], 'range', null);
  assert.equal(nodeAt(back, [0]).led_start_v, 1.01);
  assert.equal(nodeAt(back, [0]).led_stop_v, 1.03);
  assert.ok(Math.abs(nodeAt(back, [0]).led_step_v - 0.005) < 1e-9,
    'the step is read off the values, never invented');
});

test('a list that is not evenly spaced cannot become a range, because that is a different sweep', () => {
  // The canonical temperature list steps 5 K once and 10 K after. Read as
  // `295 → 220 step 5` it is sixteen temperatures where the list is nine —
  // a different experiment, one click away, on a control that only claims to
  // change how the values are written.
  const node = canonical();
  const can = canSwitchForm(node, null, 'range');
  assert.equal(can.ok, false);
  assert.match(can.why, /not evenly spaced/);
  assert.match(can.why, /would run 16/);
  assert.match(can.why, /Keep the list/);
  assert.equal(setValueForm(node, [], 'range', null), node, 'and the list is untouched');

  // An evenly spaced list converts, and a list of one or two always can.
  const even = setField(canonical(), [], 'values_k', [295, 290, 285]);
  assert.equal(canSwitchForm(even, null, 'range').ok, true);
  assert.equal(canSwitchForm(setField(even, [], 'values_k', [295]), null, 'range').ok, true);
  assert.equal(canSwitchForm(setField(even, [], 'values_k', [295, 220]), null, 'range').ok, true);
});

test('a typed list is authoritative over an answer describing the list before it', () => {
  // The validate in flight still carries the old levels. A summary drawn from
  // it would describe a loop the operator has already replaced.
  const typed = setField(canonical(), [], 'values_k', [300, 290]);
  const rows = treeRows(typed, TXILL.tree, scheduleTree(TXILL.schedule));
  assert.deepEqual(rows[0].values, [300, 290], 'what is on screen is what will run');
  assert.match(rows[0].summary, /^300\.0 → 290\.0 K · 2/);
});

test('a move takes the selection with it', () => {
  // Two `bace` siblings is the case that makes this more than tidiness: the
  // form would switch to the other one and the next override would land on
  // the wrong node.
  const path = [0, 1];
  assert.deepEqual(remapPath([0, 1], path, -1), [0, 0], 'the moved node');
  assert.deepEqual(remapPath([0, 0], path, -1), [0, 1], 'and the sibling it passed');
  assert.deepEqual(remapPath([0, 1, 2], path, -1), [0, 0, 2], 'a selection inside the moved subtree');
  assert.deepEqual(remapPath([1, 0], path, -1), [1, 0], 'and nothing elsewhere in the tree');
  assert.deepEqual(remapPath([], path, -1), [], 'nor the root');
  assert.deepEqual(remapPath([0, 3], [0, 0], 3), [0, 2], 'a move past several renumbers each of them');
  assert.deepEqual(remapPath([0, 0], [0, 0], 3), [0, 3]);
  assert.equal(remapPath(null, path, 1), null);
});

test('sameTree is what decides whether an answer still describes the screen', () => {
  assert.ok(sameTree(canonical(), canonical()));
  assert.ok(!sameTree(canonical(), setField(canonical(), [], 'hold_s', 61)));
});

test('a fresh loop is a loop the service accepts the shape of', () => {
  for (const kind of ['temperature', 'illumination', 'repeat']) {
    const node = newLoop(kind);
    assert.equal(node.kind, 'loop');
    assert.equal(node.loop, kind);
    assert.deepEqual(node.children, [], 'and empty, which the validator refuses until something is in it');
  }
  assert.deepEqual(newModule('bace'), { kind: 'module', module: 'bace', params: {} });
});

// -- the cost ---------------------------------------------------------------

test('an unmeasured settle makes the whole cost a floor, and there is no clock on a floor', () => {
  const cost = costModel(TXILL.cost);
  assert.equal(cost.lower_bound, true);
  assert.equal(cost.prefix, 'at least');
  assert.equal(cost.finish_at, null, 'a clock time under a floor is a promise the run cannot keep');
  assert.equal(cost.waiting_s, null, 'the service will not say, and neither will this');
  assert.equal(cost.unmeasured.length, 9, 'every temperature, on a bench that has measured none');
  for (const t of cost.per_temperature) assert.equal(t.settle_s, null);
});

test('a tree with no temperature in it is not a floor', () => {
  const cost = costModel(BOUND.cost);
  assert.equal(cost.lower_bound, false);
  assert.equal(cost.prefix, '');
  assert.ok(cost.finish_at, 'and then the finish time is knowable and is kept');
  assert.equal(cost.waiting_s, 0);
});

test('the time bar totals exactly what the cost totals', () => {
  // The bar and the number under it are the same run. Measured once at 90 s
  // apart, because the cost counts an illumination level's settle as
  // measuring and the first version of the walk did not.
  for (const fixture of [TXILL, BOUND, NESTED]) {
    const blocks = timeline(scheduleTree(fixture.schedule));
    const total = blocks.reduce((sum, b) => sum + (b.settle_s || 0) + (b.hold_s || 0) + b.measure_s, 0);
    assert.ok(Math.abs(total - fixture.cost.total_s) < 1e-6,
      `${total} vs ${fixture.cost.total_s}`);
  }
});

test('a temperature loop inside another gets its own blocks, and the total still holds', () => {
  // Legal, and the cost model handles it (`open_temperatures` is a list), so
  // the bar has to. Drawn as one block per *outer* iteration it kept the
  // outer holds and dropped the inner ones: 62 s of bar against a cost of
  // 102 s, with the inner modules' time attributed to the outer setpoint.
  const blocks = timeline(scheduleTree(NESTED.schedule));
  assert.deepEqual(blocks.map((b) => b.setpoint_k), [290, 200, 180, 250, 200, 180],
    'two outer iterations, each with its own two inner ones');
  assert.deepEqual(blocks.map((b) => b.hold_s), [30, 10, 10, 30, 10, 10]);
  // Every module is counted once, by the innermost temperature it is inside.
  assert.deepEqual(blocks.map((b) => b.modules), [2, 1, 1, 2, 1, 1]);
  assert.equal(blocks.reduce((n, b) => n + b.modules, 0), NESTED.counters.modules);
  const total = blocks.reduce((sum, b) => sum + (b.settle_s || 0) + (b.hold_s || 0) + b.measure_s, 0);
  assert.ok(Math.abs(total - NESTED.cost.total_s) < 1e-6, `${total} vs ${NESTED.cost.total_s}`);
});

test('the timeline is one block per temperature, or one block for a run without any', () => {
  const blocks = timeline(scheduleTree(TXILL.schedule));
  assert.equal(blocks.length, 9);
  assert.deepEqual(blocks.map((b) => b.setpoint_k), [295, 290, 280, 270, 260, 250, 240, 230, 220]);
  assert.ok(blocks.every((b) => b.settle_s === null && b.needs_operator));

  const bound = timeline(scheduleTree(BOUND.schedule));
  assert.equal(bound.length, 1);
  assert.equal(bound[0].setpoint_k, 250, 'at whatever the temperature module put it at');
  assert.equal(bound[0].modules, 8);
});

// -- the checks -------------------------------------------------------------

test('the check list collapses to counts, loudest first, and blocking is crit and invalid both', () => {
  const summary = checkSummary(TXILL.checks);
  assert.equal(summary.total, TXILL.checks.length);
  assert.equal(summary.counts.ok + summary.counts.warn + summary.counts.info
    + summary.counts.crit + summary.counts.invalid, summary.total);
  assert.deepEqual(summary.blocking, [], 'the canonical tree is valid');
  assert.equal(TXILL.valid, true);
  assert.ok(summary.notable.length, 'and the 331 is not wired, which is a warn');
  assert.ok(summary.notable.some((c) => c.code === 'temperature.not-wired'));
  assert.ok(!summary.notable.some((c) => c.level === 'ok' || c.level === 'info'),
    'an ok is a count, not a line (§11)');

  const made = checkSummary([
    { level: 'ok', code: 'a', text: '' }, { level: 'warn', code: 'b', text: '' },
    { level: 'crit', code: 'c', text: '' }, { level: 'invalid', code: 'd', text: '' },
  ]);
  assert.deepEqual(made.ordered.map((c) => c.code), ['c', 'd', 'b', 'a']);
  assert.deepEqual(made.blocking.map((c) => c.code), ['c', 'd'],
    'validate is `not any(level in ("invalid", "crit"))`, and Start must block on the same pair');
  assert.equal(made.worst, 'crit');
});

// -- the grid ---------------------------------------------------------------

test('the grid is the cells the run fills, and nothing it cannot know', () => {
  const grid = gridModel(scheduleTree(TXILL.schedule));
  assert.deepEqual(grid.temperatures, [295, 290, 280, 270, 260, 250, 240, 230, 220]);
  assert.deepEqual(grid.levels, [1.01, 1.015, 1.02, 1.025, 1.03]);
  assert.equal(grid.count, 90);
  assert.deepEqual(grid.cell(250, 1.02).map((l) => l.module), ['jv_bace', 'bace']);
  assert.deepEqual(grid.cell(250, 9.99), [], 'a cell nothing runs in is empty, not zero');
});

test('one cell is not a grid', () => {
  // The bound tree runs at one temperature and one LED level: a table of a
  // single box says nothing the node list did not.
  assert.equal(gridModel(scheduleTree(BOUND.schedule)), null);
});

// -- the chart --------------------------------------------------------------

test('an unmeasured settle is hatched and has no width, and the axis is elapsed time', () => {
  const model = scheduleModel({
    blocks: timeline(scheduleTree(TXILL.schedule)),
    cost: costModel(TXILL.cost),
  });
  const shades = model.panels[0].shades;
  assert.equal(shades.filter((s) => s.hatch).length, 9,
    'nine settles nobody has measured, drawn as the mark for "not acquired"');
  assert.ok(shades.filter((s) => s.hatch).every((s) => s.w === UNKNOWN_PX),
    'each a mark, not a duration — never the median of the others');
  // The hold beside it is a number the tree typed, and is drawn at its width.
  assert.ok(shades.some((s) => !s.hatch && s.colour === 'grey'), 'the holds are drawn');
  assert.match(model.x.label, /elapsed/);
  assert.ok(model.notes.some((n) => n.startsWith('at least ')));
  assert.ok(!model.notes.some((n) => /finish \d\d:\d\d/.test(n)),
    'no clock time under a floor — the note says why there is none');
});

test('a cost that is not a floor gets its wall clock back', () => {
  const model = scheduleModel({
    blocks: timeline(scheduleTree(BOUND.schedule)),
    cost: costModel(BOUND.cost),
  });
  assert.equal(model.x.label, 'clock');
  const when = model.notes.find((n) => n.includes('finish'));
  assert.ok(/start \d\d:\d\d · finish \d\d:\d\d/.test(when), when);
  assert.ok(!when.includes('at least'));
});

test('an empty pipeline draws an absence, not a chart of nothing', () => {
  const model = scheduleModel({ blocks: [], cost: null });
  assert.ok(model.absent, 'ui-rules §9: a pipeline with zero nodes is a state to draw');
  assert.deepEqual(model.panels, []);
});
