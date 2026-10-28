# `ui/`: the console

The console uses ES modules and DOM elements without a bundler or build step.
The service serves these files. [Rendering rules](../docs/ui-rules.md) define
the screen; [field placement](../docs/ui-fields.md) defines module cards.
Paths with a repository prefix are relative to the repository root. Local
`lib/`, `views/`, `fixtures/`, `studies/` and `tests/` paths are relative to `ui/`.

```
python -m bace.service --sim --fast --port 8900 --ui ui      # the console, live
python3 tools/serve_ui.py                                    # no service at all
```

The service mounts `/ui` after its API routes. Requests share one origin,
with no CORS or proxy. The offline server exposes the repository root at
`/ui/replay.html`; it also needs `acceptance/` in reach.

## Files

| File | Purpose |
|---|---|
| `index.html`, `app.js`, `style.css` | Shell, bar, rail, chain strip, stream state and hash router for four tabs |
| `lib/api.js` | HTTP route wrappers |
| `lib/stream.js` | WebSocket cursor, replay, reconnect and session changes |
| `lib/store.js` | Snapshot and event fold shared by every view; one trace ring across all nodes |
| `lib/format.js` | Numbers, units and absence markers from `docs/ui-rules.md` §2 |
| `lib/rail.js` | Pure `railModel`, DOM and `temperatureNow`; eight readings in one row, with details on hover/focus and a relay diagram |
| `lib/fields.js`, `lib/card.js` | Field layout, pure `cardModel`, generated forms, layer, `doc` and expanded `doc_full` |
| `lib/instruments.js` | Shutter, LED and relay switches, LED fields and pure `panelModel`; switches follow read-back |
| `lib/route.js` | Hash parsing and `benchHash`, including `#/bench?module=bace` |
| `lib/recipe.js` | `drift`, `restore`, `fileStem`, `savedBenchModel`; comparison and restore of recipe or bench values |
| `lib/watch.js` | Snapshot refresh triggers and 700 ms `REFETCH_MS` throttle |
| `lib/dom.js` | `h()` and `keyed()`; replace an element only when its model changes |
| `lib/svg.js` | Elements in the SVG namespace; HTML `createElement('svg')` has no SVG geometry |
| `lib/scale.js` | Linear/log scales, ticks and min/max thinning that retains transient peaks |
| `lib/charts/frame.js` | Stacked panels, axes, grid, captions, shading and crosshair from chart models |
| `lib/charts/jv.js`, `lib/charts/transient.js`, `lib/charts/timing.js` | J–V, transient and editable timing models |
| `lib/charts/loops.js` | Q per loop or Q(axis), with the mode stated in the caption |
| `lib/charts/schedule.js` | Settle and measurement time; unknown settles are hatched without invented duration |
| `lib/tree.js` | Tree edits, schedule nesting, temperature binding and lower-bound costs |
| `lib/history.js`, `lib/grid.js` | Run list, T × LED grid, summary, flags, temperature provenance, CSV and DOM |
| `lib/charts/grid.js` | Q(led_v) per temperature or Q(T) per LED level; partial cells are hollow |
| `lib/charts/rig.js` | Chain and shot drawings at three time scales; measured timing from `docs/manual/1.2-bench.md` |
| `lib/results.js` | Per-node result slots, last-shot line, form/data keys and `REDRAW_MS` throttle |
| `lib/monitor.js` | Pure `monitorModel` and DOM: loop, point and phase counters, ETA, stop, abort, cancel and operator prompt |
| `lib/power.js`, `lib/charts/power.js` | Monitor switch, reading, trace, exports, menu and independent trace fold; pure `powerPanelModel`, `panelShape`, `panelKeys` |
| `lib/identity.js` | Session sample editor through `PUT /session/sample`, folder preview and pure `identityModel`; no temperature field |
| `lib/title.js` | Pause, run, outer loop and terminal state in `document.title`, from `monitorModel`; no notification permission |
| `lib/undo.js` | Bounded whole-tree snapshot stack for delete, recipe open and reset |
| `lib/assistant.js` | `ask` panel, NDJSON transcript, text-safe Markdown-to-DOM renderer, provider settings and optional browser key storage per provider address |
| `lib/replay.js`, `replay.html` | Fixtures through the live store |
| `preview.html` | Real shell and bench view with stub API; bench saves last only for the tab lifetime; `#/rig` opens the rig view |
| `studies/rail-density.html`, `studies/bench-head.html` | Layout comparison records |
| `views/` | Bench: five module cards and Instruments. Pipeline: tree and schedule. Results: run records and grids. Rig: chain, shot timing and read-back. |
| `fonts/` | IBM Plex Sans/Mono and Archivo, 24 WOFF2 files, about 387 kB; no font request at runtime |
| `fixtures/` | Recorded API and event data listed below |
| `tests/` | Node tests, also run by `tests/test_ui.py` when Node exists |
| `eslint.config.mjs` | `no-undef` over library, views, entry points and tests, with explicit browser globals |

Hash routing works with `StaticFiles(html=True)`, which has no SPA fallback:
`/ui/bench` is a 404; `#/bench` stays on the served page.

## Socket rules

`lib/stream.js` and `ui/tests/stream.test.mjs` enforce these rules:

| Rule | Reason |
|---|---|
| `seq` belongs to a service session | A restart begins its counter at zero; a cursor from another session rejects valid frames. |
| A changed session reconnects with `since=0` | Replay is computed after Hello. No `since` asks for no replay. |
| Hello never advances the cursor | `data.seq` is the latest server sequence; replay frames can have lower numbers. |
| A dropped client reconnects from `Notice.data.since` after close 1008 | Fast simulation can outrun the send queue. Older replayed shots have scalar data and `decimated[…].replay`, without arrays. |

## Fixtures

`tools/record_ui_fixtures.py` records service responses and events.
`tools/make_ui_fixtures.py` converts stored HDF5 arrays to the API data shape.
Browsers do not open HDF5.

Fixture names below are relative to `ui/fixtures/`, except the explicit
`../acceptance/` path.

| fixture | what it is for |
|---|---|
| `../acceptance/20260902_service-vs-labview/journals/*.jsonl` | the event and state layer: full lifecycles, the chain verdicts, two real `RunFailed`. Not copied in here; the offline page reaches them through the repo root |
| `transient_20260902_153722.json` | the transient charts, from stored `bace-run/2` data at full precision |
| `jv_sim.json`, `jv_sim.h5` | J–V arrays with density in mA/cm² and `pixel_area_cm2 = 0.04`; the journal retains metrics without arrays |
| `hello_sim.json` | the bench snapshot; the rail and the chain. No journal contains one |
| `bench_running_sim.json` | `GET /bench` taken during a scan: the four instruments the run implies, every one `how: "inferred"`. Nothing at rest carries a single one |
| `stream_bace_sim.jsonl`, `stream_jv_sim.jsonl` | decimated wire traces with `stride` and `n_full`, plus live-only `StepPhase`; the J–V fixture has no `JVPoint` frames, which store tests construct directly |
| `stream_pipeline_sim.jsonl` | node identity: two `bace` nodes under one `run_id`, each numbering its own shots from one, and the loop's `Progress` beside the leaf's |
| `stream_tree_sim.jsonl` | 2 T × 2 LED levels × three points × two loops, with operator pauses, resume details, loop progress and typed `TemperatureRead`. `--only tree` requires `--sim`: the recorder answers pauses with synthetic temperatures |
| `stream_stopped_sim.jsonl` | a 20-loop scan stopped `after_shot` at loop 13; 60 requested, 39 kept: the one `RunAborted`, `stopping`, `stopped`, and a node that ended `stopped` in the set |
| `validate_txill_sim.json` | `POST /pipelines/validate` on the canonical 9 T × 5 level tree; 90 module runs, 198 steps, 4500 shots, and a cost that is a floor because no settle has been measured on this bench. The whole pipeline tab is drawn from one of these |
| `validate_bound_sim.json` | the same endpoint on the two shapes the canonical tree has none of: a `temperature` module, whose setpoint binds the rest of the run, and duplicate sibling modules (`bace`, `bace#2`). Its cost is the counter-case; nothing settles, so nothing is a floor |
| `runs_sim.json` | `GET /runs?session=all`: three runs, each with its own `[sample]` block |
| `run_grid_sim.json` | `GET /runs/{id}` of `T [250, 280] × led [1.010, 1.020] × (jv_bace + bace)`, stopped six shots into its last cell: three complete cells and one partial one, the temperature triple `operator/operator` on every node, and the partial node still carrying the running statistics of the loops that ran. Recorded over HTTP alone, polling the record for the moment to ask for the stop; under `--fast` a twenty-shot cell is over before a stop can land |
| `run_bace_sim.json` | `GET /runs/{id}` of a manual `bace` at the V_oc a `jv_bace` measured: one cell, `290.0 K · typed`, and `params_as_executed` for the provenance line |
| `validate_nested_sim.json` | nested temperature loops and a module on either side of the inner loop; tests require a 102 s cost rather than an outer-loop-only 62 s bar |

Names containing `_sim` identify model data. `--tag rig` records the same
set on the bench. Validation touches no instrument or folder, so
`python3 tools/record_ui_fixtures.py --only validate` can run against an active
service. Journal sequences have no gaps. Live socket tests cover drop, replay
and reconnect:

```
python -m bace.service --sim --fast --port 8900
BACE_SERVICE=http://127.0.0.1:8900 node --test ui/tests/live.test.mjs
```

## Rendering

Models are render keys. The rail refreshes `/bench` on events that change the
inferred overlay, including replay, through a throttle. Cards, result panels,
charts, monitor counters and monitor buttons have separate keys. A shot
updates results without replacing parameter fields or losing the caret.
The store retains traces for 200 shots across all nodes, matching the service
ring. Scalar statistics survive trace eviction and replay.

`bace/service/app.py` yields with `await asyncio.sleep(0)` while sending frames so a busy
socket cannot starve HTTP handlers. Result data draws through `REDRAW_MS`;
form edits draw immediately. A deferred draw uses the latest shot.
`ui/tests/render.test.mjs` checks rendering keys without a browser.

## Results

The results tab reads `GET /runs?session=all` and `GET /runs/{id}`. It does
not parse folder names or open HDF5. The record includes sample identity,
temperature source, LED level, per-point Q and standard deviation, J–V
metrics, counts, verdicts and folders. `journal.node_record` supplies the same
shape for session memory and journal queries.

The grid uses requested axes, including cells that never run. A partial cell
has a dashed outline and its running statistics; the span summary excludes
it and says so. Setpoint and reached temperature are distinct. A zero σ_Q
uses the hollow not-recorded marker. Flags state their evidence and remedy.
The list and open record refresh at `parked`; live progress stays in the
monitor. Folders are selectable text. Re-queue runs one module with the node's
overrides and LED level, without copying V_oc into the request.

## Pipeline

Every committed tree edit requests `POST /pipelines/validate`, coalesced
while edits are pending. Dry run requests the same validation deliberately.
Counts, checks, costs, folders and schedule come from the answer. A range
shows no count before resolution. List-to-range conversion requires uniform
spacing; the nine-value temperature list with a 5 K step followed by 10 K
steps cannot become a 16-point 5 K range silently.

The schedule has nested and flat views. `lib/tree.js` carries a temperature
module's binding through the rest of the run, including later enclosing-loop
iterations. An unknown settle means `lower_bound`, rendered as "at least";
the waiting time and finish clock remain absent. Unknown settles are hatched.

During validation the previous answer stays visible and marked stale. Start
is disabled; tree rows wait for the matching schedule. Shot events do not
revalidate an unchanged tree. A node form uses the bench field component over
resolved schedule values. Overrides, inherited/derived values and node-only
rows stay visible; other values fold as "same as bench". A reset remains
available for a forbidden override even when resolution locks the field.
If resolution fails, the catalogue plus node overrides keeps the form editable.

## Monitor

The monitor appears on every tab while a run holds the worker. It shows each
loop's own progress, the leaf's shot/curve count and the live `StepPhase`.
Utility nodes have no acquisition counter. Automatic temperature settling and
operator pauses have distinct states. ETA counts down from the latest event;
a submit-time prediction reads `ETA ~` when no measured ETA exists.

Stop after shot keeps the current shot. Abort requires arming and abandons
the unfinished shot. Cancel applies to queued runs. The operator prompt
survives reconnect through `run.pending`; a stale snapshot cannot restore an
answered pause. Its key identifies the pause, so polled readings preserve
typed input. A replayed `StepStarted` cannot clear a newer live phase.
The service omits ephemeral phases for subscribers more than fifty frames behind.

## Charts

J–V density is mA/cm²; absent area means current in A. `metrics.jsc` remains
in A. Curves at the same LED level share a hue; forward and reverse arms have
distinct identities. The sequential ramp darkens in OKLab from 0.78 to 0.48.
Missing samples remain absent, including on log axes.

Light, dark and their difference share a frame. The running integral has its
own stacked panel and saturates at Q within the integration window. Window
shading uses `StepDone.t0_int_record_s/t1_int_record_s` when present; otherwise
it uses the shot's setpoint, rig latency and returned record origin. It never
uses a later form edit to relocate acquired data.

A zero-width axis shows Q per loop, mean and ±σ band. A swept axis shows
Q(axis) with errors and hollow markers where σ is absent. The caption states
the mode. Unmeasured loops remain hatched. These charts use shot scalars and
therefore survive replay without arrays.

The BACE card's timing diagram follows the form and describes the next run.
The rig tab provides chain and shot context at three scales. The record spans
one division before the trigger and nine after. Unknown LED polarity draws
no asserted light waveform. The lit fraction under INV is `100 - duty`.
`alternate_order` reverses trace order on eligible even loops; dark-first
shots reuse that point's preceding light range. With the bench's
`invert_polarity = true`, NORM holds V_pre and pulses to V_coll. INV holds
V_coll and suppresses accumulation (`docs/notes.md`).

Min/max thinning maps integer sample indices. Wrapped waveform edges draw as
separate segments, and captions stay outside waveform geometry.
`python3 tools/serve_ui.py` with `ui/replay.html` supports browser inspection.

## Saved benches

`POST /bench/save`, `GET /bench/saved` and `DELETE /bench/saved/{name}` manage
whole-catalogue values. The `MODULES` header shows no saved bench, saved files,
a matching file, or the number of differing values. The name, file list and
comparison table sit behind its `⋯`.

Opening a file only shows differences. Restore sends `PUT /modules/{m}/params`
per module; refused values retain their error and successful edits stay.
Overwrite and delete require a second click. Arming compares the service's
file stem: `cool down` and `cool_down` name the same file. The pointer guard
defers rendering between mousedown and mouseup so a click reaches its button.
The comparison is shared with pipeline recipes through `lib/recipe.js`.

The build and performance records are in `docs/ui-plan.md`.
