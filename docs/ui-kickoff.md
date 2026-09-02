# UI kickoff — building the console against the finished service

2026-09-02. The service layer is built, rig-proven and merged; this file is the
starting brief for the session that builds `ui/`. Read it first, then the two
documents it leans on: `docs/service-contract.md` (every route and shape — the
single source of truth; where UI needs and the contract disagree, fix the
contract deliberately, never guess) and the design inputs below.

## What to build

A static browser front end (plain files — the service serves them, there is no
build server on the lab PC) implementing the Round 3 design: four tabs,
**bench** (the five module cards + the pinned rail + the chain strip),
**pipeline** (the tree editor, Dry run, the schedule), **results** (designed as
Round 2's R2·3, never carried into Round 3 — see below), **rig** (the
diagram and the read-back, reference only). The user's standing instruction:
*not* the shape of the JV console or the 331 controller; iterate the design in
Claude Design with the user before coding screens that differ from Round 3.

## The design inputs

| where | what |
|---|---|
| `docs/bace-console-round3.html` | the chosen design, self-contained, open in a browser |
| artifact `UI mockups: Pipeline bench timeline` (claude.ai/code/artifact/b51c4ebd-…) | the editable canvas of the same design |
| `docs/design/` | the canvas source, openable from a local server, with the chart decisions as working code (`bace-charts*.js`) — and Round 2, whose **R2·3 is the results design** |
| `docs/ui-rules.md` | how to render it: numbers and their absences, colour with a job, the three time scales, provenance on screen, the failures that look like results |

The bundle and the artifact are the same four artboards, and both were
corrected on 2026-09-02: the 81150A `:OUTP1:POL` read `INV` in every chain
strip, and the bace card's second polarity row was `inverted_output` → INV.
It is now `output_polarity` → NORM — `inverted_output` is a *boolean* that an
explicit `output_polarity` architects out, and NORM/INV is `output_polarity`'s
pair. **Take no parameter name or value from the mockup**: the names come from
`GET /modules` and the values from its `{value, source, …}`. The artboards'
numbers are one operator's session — good examples, and a fair starting point
for the cards' defaults, but not the recipe.

**The artifact and the canvas are separate stores.** Publishing to the
artifact URL does not write back to the Claude Design project, and editing
the canvas there does not update this repo. `docs/design/` is the canvas as
exported on 2026-09-02, corrected; if you edit the design, export again.

## Developing

```
"C:/WPy64-31370/python/python.exe" -m bace.service --sim --fast --port 8900
```

gives the full API with a simulated rig and instant runs; `--seed` varies the
device. Serve the work in progress with `--ui <dir>` (mounted at `/ui`, `/`
redirects). `GET /` lists every route; `/docs` is the interactive API page.
The scripted walk-through of the whole operator flow is
`bace/service/README.md`, "Driving it by hand".

## What the service guarantees the UI (highlights; the contract has the rest)

- `WS /events` with `Hello`, replay via `?since=<seq>`, monotonic `seq`; a
  client dropped for falling behind reconnects with `since=` and misses no
  numbered frame. `StepPhase` frames (the shot-segment indicator) are
  live-only, `seq: null`.
- `StepDone` traces arrive decimated (`decimated` says stride and full length;
  time axis `t0 + i·stride·dt`, last kept sample at `t0 + (n−1)·dt`); full
  precision from `GET /runs/{id}/data`.
- Every parameter on `/modules` carries `{value, source, detail, editable}` —
  render provenance, do not re-derive it. Inherited/derived values are not
  editable; `PUT` with `null` resets one.
- Verdicts are three-tier: `crit` blocks Start (and only hardware safety is
  crit), `warn` states evidence and never blocks, fixes are explicit bench
  actions the operator clicks (`fix` names the action).
- The bench card during a run is the Start read-back overlaid with inferred
  state (`how: "inferred"`, listed in `inferred`) — show the distinction.
- Runs from earlier sessions answer from the journal (`GET /runs?session=all`,
  `GET /runs/{id}` with `from: "journal"`) — the grey V_oc grid reads this.
- **Where a temperature came from travels with it** (contract §7, added
  2026-09-02): every run and journalled node carries `temperature_how` ×
  `temperature_source` — `typed`/`""` (nobody read an instrument),
  `setpoint`/`""` (**requested, not reached**), `settled`/`console|simulated`,
  `operator`/`operator` (typed at the pause), `operator`/`console|simulated`
  (the pause ended without a number; this is the last polled reading). `how`
  alone does not separate the last two, so render both — and never let a
  `simulated` source read as a measured one. The folder name says `290K`
  either way; it is not evidence.
- `[sample] comment` is slugged into the folder name and kept verbatim in the
  metadata. Show the sentence, not the slug.

## Known gaps the UI should not paper over

- The **results tab has a design but no home**: Round 2's R2·3 (the 9 × 5 grid,
  the partial cell outlined and never averaged in silently, the flag list where
  every flag states its reason, `resolved from: run.toml 17 · last-used 2 ·
  inherited 2 · edited 1`). It was set aside with Round 2 and Round 3 left the
  tab empty. Two things to settle before building it: it names a `flags.json`
  the service does not write, and its `saturation` line predates the 2026-09-02
  correction. `docs/design/README.md` has the detail.
- `estimate`/cost is a lower bound until settle history exists; `lower_bound:
  true` renders as "at least", never as a promise.
- σ_Q of 0 means *not recorded*; intensity without the calibration factor is
  watts, never mW/cm² (design pack, `04-data.md`).
- Temperature settles automatically only when `[temperature] console` is set
  in rig.toml; otherwise every temperature node pauses (`NeedsOperator`) and
  the UI must surface resume with a typed `temperature_k` — and both ways of
  ending that pause are recorded differently, see `temperature_how` above.
- A `temperature` module binds the **rest of the run**, not the rest of one
  loop iteration (the cryostat does not reset between iterations); a
  temperature *loop* still wins for its own subtree. The pipeline tab's "what
  it will do, in order" must show it that way.

## State of the bench code (so the UI session does not re-litigate it)

The service matched LabVIEW on the rig on 2026-09-02 evening (HANDOVER,
evening section; the two runs and three journals of that day are in
`acceptance/20260902_service-vs-labview/`, with a README, and
`tests/test_acceptance_20260902.py` reads them). The validated recipe is in
`run.toml` — combination ③ (`invert_polarity = true`, `:OUTP1:POL NORM`), 200
averages, 5 s shutter settles, LED 1.000 V; the "④ = INV" claim in older docs
is overturned (`docs/README.md`, the table). The LED is never switched off by
a module; the shutter is the light switch, and a bace waits for the power
meter to read a flat 10 s before scanning.

Those journals are also the most realistic sample of the event stream the UI
will consume — ~~real `seq` gaps~~, real verdicts, a `RunFailed`, a
`NeedsOperator`-free rig session — worth replaying against a draft console
before the first live run. (**Corrected 2026-09-02**: there are no `seq` gaps
in them and there cannot be. A journal is one monotonic counter, and
`StepPhase` — the only unjournalled frame — consumes no number; all three files
run 0…N, which `tests/test_ui.py` asserts. Gaps and `decimated.replay` belong
to the *socket*, where a client falls behind and is dropped at 1008, and are
proved against a live `--sim --fast` scan in `ui/tests/live.test.mjs`.)
