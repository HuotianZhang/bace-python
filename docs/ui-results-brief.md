# The results tab — brief for the Round 3 design pass

2026-09-04. `docs/ui-plan.md` M6 is preceded by a design pass, because R2·3 is
a Round 2 artboard and Round 3 left the tab empty. This file is what to bring
into it: the design that exists, the data that exists, and the list of things
the pass has to decide.

**What this pass must produce is not a screen. It is the data source.** The
plan deliberately leaves "the results grid's data source" to M6, *"from the
view's needs"* — so the output of this pass is a field list, and the service
change (`docs/naming-plan.md` rule 1, and whatever else) is written from it.
Anything decided here that the service does not already serve becomes work
before M6 starts.

---

## 1 · The design that exists

R2·3, in `docs/design/BACE Console - Round 2.dc.html`, third artboard. The only
results design there is; set aside with Round 2 and never given a Round 3
replacement. `docs/design/README.md` has the full reading. It carries:

* the 9 × 5 grid — **45 runs, 44 complete, 1 partial kept as it is**, Q at
  V_pre = V_oc per cell with V_oc beneath it;
* *"The partial cell is outlined, never averaged in silently, never dropped"*;
* the legend `□ σ_Q not recorded · ● σ_Q measured`;
* a summary strip — span, σ provenance, `intensity null in 45 of 45 · :8918
  down for the whole run`;
* a selected-cell panel, a flag list where **every flag states its reason**,
  `where it is` with the full path and file list;
* `resolved from: run.toml 17 · last-used 2 · inherited 2 (led_v, V_oc) · edited 1`;
* `Re-queue this cell`.

The artboards' numbers are one operator's session. Take no parameter name or
value from them (`docs/ui-kickoff.md`).

---

## 2 · The data that exists, measured

Measured 2026-09-04 against the three **rig** journals in
`acceptance/20260902_service-vs-labview/journals/` — the real history a results
tab would open on, not a simulation. Reproduce:

```python
from bace.service.journal import Journal          # copy the three .jsonl into <out>/journal/ first
j = Journal("<out>", "probe", header={"mode": "sim", "fast": False})
j.run_index("all")            # what GET /runs?session=all answers
j.run_record("20260902_153357-003")   # what GET /runs/{id} answers, from: "journal"
```

### `GET /runs?session=all` — 12 runs, 3 sessions

Every key on every row, as it actually comes back:

```
run_id · session_id · kind · name · module · tree_summary · node_count ·
node_count_done · state · parked · queued_at · started_at · finished_at ·
kept · requested · outcome_text · folder · folders · error ·
voc_min · voc_max · light_curves
```

| run_id | module | state | kept/req | outcome |
|---|---|---|---|---|
| `20260902_153357-003` | bace | done | 2/2 | `Q -1.718e-10 ± 5.2e-11 C · 2/2` |
| `20260902_153357-002` | jv_bace | done | 2/2 | `2 curves · V_oc 1.090 V` |
| `20260902_153357-001` | **jv_dark** | done | 1/1 | `1 curve` |
| `20260902_144844-00{1,2,3}` | jv_dark · jv_bace · bace | done | | `Q 4.751e-11 ± 2.4e-11 C · 2/2` |
| `20260902_125751-006` | bace | **failed** | None/None | `SyncError: no sync on CHAN3 …` |
| `20260902_125751-005…001` | jv_bace · jv_dark · bace(failed) · jv_bace · jv_dark | | | |

Four things to read off that table, all of them design input:

1. **There is no identity on a row.** No `sample`, no `material`, no `pixel`,
   no temperature triple. Counting the words in the three journal files:
   `sample: 0 · material: 0 · pixel: 0 · operator: 0 · comment: 0 ·
   temperature_k: 0 · temperature_how: 0 · offset_corrected: 0 ·
   led_drive_v: 2` (in 12 runs). This is `docs/naming-plan.md` rule 1,
   confirmed on the rig day rather than on a `--sim` session.
2. **`kept/requested` is `None/None` on a failure**, and `state` is the only
   thing that says so. Two of twelve runs are failures — a sixth of the real
   history — and their `error` is a 300-character sentence
   (`"SyncError: no sync on CHAN3: the trigger channel swings 4.4 mV peak to
   peak during calibration, against the ~1 V the 81150A sync gives…"`).
3. **`folder` is a Windows path**: `runs\290K_1000mVLED_1091mVVOC_offsetcorr_20260902_153722`.
   Backslashes, and *not* the eight-field grid `naming-plan` §2 proposes — the
   device was unnamed, so the name starts at the temperature. Seven of the
   eight fields are conditional today: **the name is not a parseable key**,
   before or after that proposal.
4. **`jv_dark` is a module that no longer exists** (split into `jv` + `light`
   on 2026-09-03). Last week's history names it. The results tab is the one
   view that reads across that rename.

`voc_min`, `voc_max`, `light_curves`, `node_count_done` are already on the row —
the grey V_oc grid `ui-kickoff` mentions reads these.

### `GET /runs/{id}`, from the journal

`summary()` plus `tree` and `nodes`. One node, reduced:

```json
"bace": {"module": "bace", "outcome": "ok", "kept": 2, "requested": 2,
         "voc": 1.091329, "voc_how": "measure_dc", "led_v": 1.0,
         "temperature_k": null, "temperature_how": null, "temperature_source": null,
         "summary": "Q -1.718e-10 ± 5.2e-11 C · 2/2",
         "folder": "runs\\290K_1000mVLED_1091mVVOC_offsetcorr_20260902_153722"}
```

The temperature triple is `null` on every one of these — the HDF5 beside it
says `290.0`. That is rule 1's second hole (`executor._node_detail` reads the
raw context field instead of calling `ctx.temperature()`).

**No verdicts, and no params.** The journal record is `summary + tree + nodes`;
a run this process still holds answers with `verdicts` (one per
`(code, node_path)`), a run from last week does not.

### What is in the journal but not in the record

`Verdict` lines are in the file, with exactly what a flag list wants:

```json
{"level": "warn", "code": "chain.bias-polarity",
 "text": "81150A POL reads NORM: the device would rest at the other level between
          pulses, and extraction would happen on the other edge of the pulse …"}
```

`_Run.apply` keeps `Verdict` only when the code is `temperature.settled`, and
only for the cost model's settle history. Everything else is dropped on the
way to `record()`. One of the four in this set carries `run_id: null` — a
bench verdict in force at the time, attached to no run.

`RunQueued` carries `params` (the operator's overrides — **empty** on all
twelve) and `resolved` (15 entries, **values only**: `start_v: -0.2,
step_v: 0.02, pixel_area_cm2: 0.0, …`). No `{value, source, detail}`.

### What is on disk

A run folder holds `run<stamp>.h5` and up to eight `.dat` files, one per stem:
`1_allLoopsQ · 1_averagesQ · 1b_allLoopsIntensity · 1b_averagesIntensity ·
2_averagesPhotoCurrent · 3_allLoopsPhotoCurrent · 4_averagesLightCurrent ·
4_averagesDarkCurrent`. **There is no `flags.json`** — nothing in the tree
writes one. An intensity series writes a parent named `…_series` holding one
run folder per level.

The HDF5 `/metadata` is where the identity actually lives:

```
sample '' · material '' · pixel '' · operator '' · comment '' ·
temperature_k 290.0 · led_drive_v 1.0 · offset_corrected True ·
voc_v 1.091329 · started '2026-09-02T15:37:22'
```

and `config/{run,rig,resolved}` hold the settings as attributes — 22, 24 and 9
of them — again **values only, no provenance**. A grid that groups by device
today has to open 45 HDF5 files, or parse the folder name.

---

## 3 · What the pass has to decide

**D1 · Where the flags come from.** R2·3 names `flags.json`; nothing writes
one. The evidence says the file is not needed: the `Verdict` lines are already
in the journal with `level`, `code`, `text` and `node_path` — what is missing
is that `_Run` drops them. Decide: fold verdicts into the journal record (one
per `(code, node_path)`, as the live record already does), or add the file.
And decide what a bench verdict with `run_id: null` does to a cell.

**D2 · The `saturation` line has to be rewritten.** R2·3's wording predates
the 2026-09-02 correction: the service reports a shared extreme but **judges
it as nothing**. Copied as it stands the line reads the same and means
something else.

**D3 · What the grid's two axes are.** R2·3 is 9 T × 5 levels — one pipeline.
The history is not: 12 manual runs of three modules, across three sessions, on
one device. Decide whether results is *a pipeline's view* or *a device's
view* (group by device, across runs and sessions) — the second is what rule 1
exists for, and `?session=all` walks up to twenty journal files to serve it.
Manual runs are first-class either way (`ui-rules.md` §8: *"a verification J–V
an hour before a long run is exactly what you want to find later"*).

**D4 · What each row of `GET /runs` must carry.** This is the one decision
that becomes service work, and the reason the pass comes before M6. The field
list above is what exists. Rule 1 adds the `[sample]` block and the
temperature triple **per run** (not only in the header — three verified paths
defeat a header-only fix). Anything else the grid needs is decided here.

**D5 · Whether the provenance line is possible for a past run.** `resolved
from: run.toml 17 · last-used 2 · inherited 2 · edited 1` needs source labels
that exist only in `GET /modules` at the time of the run. The journal has
values; the HDF5 has values. Three ways out: journal the provenance beside
`resolved`, show the line only for a run this process still holds, or drop it
and show `params` (the overrides) as *what the operator changed*, which is the
half that survives.

**D6 · What a selected cell opens, and what `Re-queue` means.** The panel's
content is mostly answerable (`nodes[…]`, `folder`, `error`, verdicts after
D1). `Re-queue this cell` is the only *write* on this tab: decide what it
posts — the original resolved params as a one-node tree, with or without the
V_oc that was measured then — and say it on the button.

**D7 · The six empty states** `ui-rules.md` §9 names: no device mounted · no
run yet · a pipeline with zero nodes · a module never run this session · the
power-meter console down · temperature not wired at all.

**D8 · How a temperature's provenance is drawn.** `temperature_how ×
temperature_source`: `setpoint` means *requested, not reached*; `simulated`
must never read as measured. The folder name says `290K` either way — it is
not evidence.

**D9 · What a failure looks like in the grid.** Two of twelve, `kept/requested`
`None/None`, a 300-character sentence for a reason. `ui-rules.md` §9 is the
class this belongs to — and the sentence is worth showing, not truncating to
"failed".

**D10 · How `where it is` behaves.** Backslash paths from the lab PC, the
`_series` parent as a folder *class*, and colliding folders made unique
(`naming-plan.md` §2). Related and blocking anything on the rig: a `material`
containing a colon builds a path segment that fails on Windows.

**D11 (optional, and the strongest candidate in the pack)** ·
`ui-rules.md` §12: *"Check if Cursor 1 matches the start of the transient"* —
a manual check the operator did on every run for years, that nothing in
`bace-python` does. Results is one of its two natural homes.

---

## 4 · Do not re-open

`ui-rules.md` §11 lists what the artboards or the service already settled. For
this tab: `□ σ_Q not recorded · ● σ_Q measured`; the partial cell outlined,
never silently averaged, never dropped; truncated runs shown as **kept of
requested**. M4's `ui/lib/charts/loops.js` already implements all three — the
grid must use *the same marks*, not a second vocabulary for the same facts.

## 5 · When the pass ends

* Export the canvas again — **the artifact and the Claude Design project are
  separate stores**, and `docs/design/` is a 2026-09-02 export. Publishing does
  not write back; editing there does not update this repo.
* Record the departures from R2·3 in `docs/ui-plan.md` beside M6, the way M3
  and M4 did.
* Hand D1–D6 to the service as a field list. That, plus rule 1, is what
  unblocks M6.
