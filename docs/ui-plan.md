# Building `ui/` — direction and phases

2026-09-02. How the console gets built and in what order. The brief is
`docs/ui-kickoff.md`, the design is Round 3 (`docs/bace-console-round3.html`,
`docs/design/`), the API is `docs/service-contract.md`, the rendering rules are
`docs/ui-rules.md`. This file adds only what those do not: the shape of the
front end, and the order the work happens in.

The goal, plainly: **the designed screens, driven by the measurement modules
that already exist.** The service is finished and rig-proven; nothing below
asks it for a new measurement.

---

## Direction

### Three structural decisions

Everything else follows from these, and each one turns a rule in
`docs/ui-rules.md` from a discipline into a property of the code.

**1 · The forms are generated, not written.**

`GET /modules` describes every parameter completely — `type`, `unit`,
`choices`, `minimum`, `maximum`, `nullable`, `group`, `doc`, `editable`,
`source`, `detail`. So there is one field component and one card component, and
all eight modules get a card from them. Nothing about `bace`'s fifty parameters
is typed out by hand.

The payoff is `ui-rules` §6: *"render provenance, never re-derive it."* With one
component owning `{value, source, detail, editable}`, there is no second place
that could re-derive it. Inherited-reads-as-inherited, derived-is-not-editable
and `PUT null` resets are one implementation, not eight.

It also settles the density question the same way: which fields sit above the
fold and which fold into "N more · run.toml" is a table of group names and
orders, edited without touching code. The Round 3 artboard already names the
ones that matter; the rest are defaults or read-back values, and the table is
where that judgement gets recorded.

**2 · The screen is a projection of the event stream.**

One store, folding the `/bench` snapshot and the `/events` frames into the
state every view subscribes to. Not per-view fetching.

This is what makes `ui-rules` §8 — *"a manual run and a pipeline step share the
same live monitor"* — true by construction: they are the same `run_id` on the
same stream, and a manual run **is** a one-node pipeline in the service, same
code path. The reconnect discipline (`since=`, `seq` dedupe, `StepPhase`
arriving with `seq: null`, the 1008 drop under `--fast`) lives in one module and
no view knows about it.

**3 · The charts need one foundation, then six components.**

`docs/design/bace-charts*.js` is not a chart library. Its sixteen elements are
`def(tag, w, h, html)` — a fixed SVG string per tag, every path a pre-computed
pixel coordinate, no attributes and no data input. They are an excellent visual
specification (colours, the sequential J–V ramp, the shaded integration window,
the ±σ band, the −4 V layout, the axis wording) and they are not an
implementation.

Grouped by what they actually are, the work is smaller than sixteen:

| component | replaces | where |
|---|---|---|
| J–V | `ch-jv`, `ch-jvdark` | bench, results |
| transient | `ch-transient`, `ch-shot` | bench, results |
| Q per loop / Q(axis) | `ch-qloop`, `-full`, `-trunc`, `-20`, `ch-qt` | bench, results |
| timing diagram | `ch-timing` | bench |
| schedule / ETA / settle | `ch-schedule`, `ch-eta`, `ch-settle` | pipeline |
| sparkline | `ch-spark` | rail |
| *(static, reusable as it stands)* | `ch-rig` | rig |

All six sit on one scale/axis module. That module is the only thing in `ui/`
being written from nothing.

### What the front end is made of

Plain ES modules and plain CSS, served by `--ui DIR` — `StaticFiles(html=True)`
mounted at `/ui`, after every API route, same origin, so `fetch('/bench')`
works with no CORS and no proxy. No bundler, because the lab PC has no build
step. Hash routing, because a static mount has no SPA fallback.

The React in `docs/bace-console-round3.html` is the Claude Design canvas
runtime, not a decision about the console. The artboard CSS is flex and grid
with zero absolute positioning, so it ports as layout rather than as pictures.

IBM Plex Sans and Mono come out of `bace-console-round3.html` itself — 24 woff2
files, 387 KB, already in the repo — and go into `ui/fonts/`. Nothing loads
from a network.

### What it is not

Not the JV console and not the 331 controller (the standing instruction). Not
responsive: one lab PC, 1920×1080 or wider, one window, the rail fixed and
everything else scrolling. Not comfortable: `ui-rules` §1 — *"if a screen feels
comfortable, it is probably hiding something the operator needs."*

---

## Phases

Cut vertically, not horizontally: each phase drives a real path end to end
against `--sim --fast` rather than finishing one layer across all four tabs.
Every phase names the thing that proves it.

### Phase 0 · One pipe

`ui/` exists, fonts land, four empty tabs behind a hash router. `api.js` wraps
the routes; `stream.js` owns the WebSocket, `since=` replay, `seq` dedupe and
the 1008 reconnect; `store.js` folds the bench snapshot and the frames.

No charts, no forms — a button that posts `{"module": "jv_dark"}` and a list of
frames as they arrive.

**Proves:** the run reaches `done`, `kept`/`requested` come back, and a client
killed mid-run reconnects with `since=` and misses no numbered frame.

### Phase 1 · The bench card does work

The generated field and card components. The five module cards, the pinned
rail, the chain strip with a button per `fix` calling
`POST /bench/actions/{name}`. Editing writes the `edited` layer through `PUT`;
`null` resets.

**Proves:** `ui-rules` §8's bar — a dark J–V in **fifteen seconds** by someone
who has not seen the UI before. This is the first phase whose output an
operator could actually use.

### Phase 2 · Results become visible

The scale/axis foundation, then J–V and transient. The transient carries the
shaded integration window and the running integral — the one plot the LabVIEW
panel had that the operator will look for.

**Proves:** the run from phase 1 draws. Replaying the real journals in
`acceptance/20260902_service-vs-labview/` produces readable charts, including
the `RunFailed` and the `seq` gaps.

### Phase 3 · What a run looks like while it runs

The live monitor: `StepPhase` as the shot-segment indicator, decimated traces,
the per-shot `verdict`, the inferred-instrument overlay (`how: "inferred"`)
shown as distinct from the Start read-back. Stop after-shot, abort, and the
`NeedsOperator` resume with a typed `temperature_k`.

**Proves:** a 2 T × 2 level tree on the simulator stays readable start to
finish, and a client dropped at 1008 during it comes back without losing the
loop curve.

### Phase 4 · Seeing it before and during

The timing diagram, updating as the form is edited — where a wrong `V_coll`
sign or an absurd delay becomes visible *before* the run. Then Q per loop and
Q(axis), with the zero-width-axis switch made visible rather than silent, and
error bars that reflect the loops that actually ran.

**Proves:** an operator can reject a bad shot definition without running it,
and watch σ tighten while it runs.

### Phase 5 · The pipeline tab

The tree editor, Dry run against `POST /pipelines/validate`, the schedule as
"what it will do, in order" — including a `temperature` module binding the
**rest of the run** rather than the rest of one iteration — the check list
collapsed to `16 checks · 15 ok · 1 warn · show`, and the cost with
`lower_bound` rendering as "at least".

**Proves:** the canonical 9 T × 5 level tree can be built, dry-run and
submitted, and the three counters at three scales read correctly against a
4.5-hour sweep's ETA.

### Phase 6 · The results tab

Preceded by a design pass, because R2·3 is a Round 2 artboard and Round 3 left
the tab empty — the standing instruction is to iterate in Claude Design before
coding a screen that differs from Round 3.

What the view needs decides what gets written, not the other way round: the run
folders on disk today are artefacts of back-end debugging, and the file list in
R2·3 predates all of it. Settle the grid's data source from the view's
requirements and from what an operator actually needs to find later.

**Blocked by:** the journal changes in `docs/naming-plan.md`. The grid groups
by device and reads `GET /runs?session=all`, and the journal currently records
no `sample`, and no temperature provenance on any node without a temperature
loop above it. A view cannot render provenance the record does not carry.

### Alongside · The rig tab

Read-only reference, and `ch-rig` is a static schematic that ports as it
stands — the one place "reusable as they stand" is true. Drop it in whenever a
phase runs short.

---

## What blocks what

| before | must be done |
|---|---|
| anything on the rig | the folder-name defect in `docs/naming-plan.md` §2 — `material = "PTQ10:IT-4F"` builds a path segment with a colon, which fails on Windows and passes on Linux |
| phase 6 | `docs/naming-plan.md` rule 1 — the journal carries the sample block and the temperature triple on every node |
| phase 6 | a Round 3 design pass on results, with the user, in Claude Design |
| phase 2 | nothing — the chart foundation is new code with no service dependency |

Nothing else in the service is on the critical path. The API is complete for
phases 0–5 as it stands.

## What this plan deliberately does not decide

- **Which parameters sit above the fold on each card.** A table produced during
  phase 1, from the Round 3 artboard plus what `GET /modules` actually returns,
  reviewed then rather than guessed now.
- **The results grid's data source.** Phase 6, from the view's needs.
- **Whether to keep `[sample] temperature_k = 290.0` in the recipes.**
  `docs/naming-plan.md` §4 states the problem; it is a bench-habit decision.
