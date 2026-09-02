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
**pipeline** (the tree editor, Dry run, the schedule), **results** (not yet
designed — design it in Claude Design first or leave it a stub), **rig** (the
diagram and the read-back, reference only). The user's standing instruction:
*not* the shape of the JV console or the 331 controller; iterate the design in
Claude Design with the user before coding screens that differ from Round 3.

## The design inputs

| where | what |
|---|---|
| `docs/bace-console-round3.html` | the chosen design, self-contained, open in a browser |
| artifact `UI mockups: Pipeline bench timeline` (claude.ai/code/artifact/b51c4ebd-…) | the editable canvas of the same design |
| `D:\BACE\ui-brief\` | the design pack: `02-orchestration.md` (the flow model), `03-states.md` (the rail, the chain, failures-that-look-like-results), `06-visual.md` (type, colour, density), `data/` (real numbers to develop against) |

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

## Known gaps the UI should not paper over

- The **results tab is undesigned** on both sides.
- `estimate`/cost is a lower bound until settle history exists; `lower_bound:
  true` renders as "at least", never as a promise.
- σ_Q of 0 means *not recorded*; intensity without the calibration factor is
  watts, never mW/cm² (design pack, `04-data.md`).
- Temperature settles automatically only when `[temperature] console` is set
  in rig.toml; otherwise every temperature node pauses (`NeedsOperator`) and
  the UI must surface resume with a typed `temperature_k`.

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
will consume — real `seq` gaps, real verdicts, a `RunFailed`, a
`NeedsOperator`-free rig session — worth replaying against a draft console
before the first live run.
