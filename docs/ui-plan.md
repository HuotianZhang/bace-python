# Building `ui/` — direction and phases

2026-09-02. How the console gets built and in what order. The brief is
`docs/ui-kickoff.md`, the design is Round 3 (`docs/bace-console-round3.html`,
`docs/design/`), the API is `docs/service-contract.md`, the rendering rules are
`docs/ui-rules.md`. This file adds only what those do not: the shape of the
front end, and the order the work happens in.

The goal, plainly: **the designed screens, driven by the measurement modules
that already exist.** The service is finished and rig-proven; nothing below
asks it for a new measurement.

Two sessions planned this independently and arrived at the same shape — no
build step, generated parameter rows, self-drawn charts, the event layer
before the bench, results last behind a design pass. Where they differed this
file takes the better of the two, and says so.

---

## Direction

### Four structural decisions

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
code path.

The whole reconnect discipline lives in one module and no view knows about it:
`since=` replay, `seq` dedupe, `StepPhase` arriving with `seq: null` and never
entering the ring, the 1008 drop that `--sim --fast` *will* cause on a long
scan — and `decimated[name].replay === true`, which means *redraw the loop
curve, the trace is gone*.

**The dedupe cursor belongs to a service session, not to the socket.** `seq` is
per-session and a new process starts it at zero, so a service restarted under
an open browser breaks a naive cursor twice over: `?since=<the old high seq>`
replays nothing, and then `seq > last seen` discards every numbered frame the
new process sends — a bench that goes quietly stale until someone reloads the
page. `Hello` carries what settles it (`data.session.id`, beside `data.seq` and
`data.bench`), and its envelope `seq` is `null`, so it is exempt from the
dedupe it configures.

But resetting the cursor on that `Hello` is too late by itself, because the
server has already acted on the stale one:

```python
await ws.send_text(_dumps(_hello(session)))      # app.py:415
if since is not None:
    for frame in session.events_since(since):    # the query value, fixed at open
```

`since` is a query parameter, decided when the socket opened and unchangeable
after. So a socket opened with the old session's high `since` gets `Hello`,
then a replay computed from a cursor that matches nothing in the new session —
and every frame the new process emitted before the client noticed is gone.
`data.bench` gives the bench as it is now; it does not give back a completed
shot or its loop curve.

So the rule is **reconnect, not just reset**: handle `Hello` first, and when
`data.session.id` differs from the one in hand, drop the cursor and the session
state, close the socket and reopen it with **`since=0`**.

Not "without `since`" — the two are not the same request. The replay is guarded
by `if since is not None`, so an absent query parameter gets `Hello` and the
frames from that moment on, and loses everything the new process emitted before
the client noticed: exactly the completed shots and partial loop curves the
reconnect exists to recover. `since=0` replays the ring from its start.

Rebuild from the new `Hello`'s `data.bench` and let the ring supply the rest. That last one is the subtle branch: a replayed
`StepDone` carries every scalar and its `verdict` with the four traces `null`,
and a client that blanks the chart instead of redrawing from scalars has a bug
that only shows up after a reconnect.

**3 · The charts need one foundation, then six components.**

`docs/design/bace-charts*.js` is not a chart library. Its sixteen elements are
`def(tag, w, h, html)` — a fixed SVG string per tag, every path a pre-computed
pixel coordinate, no attributes and no data input. (The self-contained
`bace-console-round3.html` embeds the same two files byte for byte, so it is
not a second implementation to reach for.) They are an excellent visual
specification — colours, the sequential J–V ramp, the shaded integration
window, the ±σ band, the −4 V layout, the axis wording — and they are not an
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

No charting library can do this job: the domain rules are the point.
`ui-rules` §4 — a zero-width axis plots Q *per loop*, not a curve, **and the
switch must be visible rather than silent**; light, dark and photocurrent
belong in one frame, because photocurrent alone hides the failure where both
parents sit on the digitiser's rail; §2 — `σ_Q = 0` means *not recorded* and
needs its own rendering, since a zero-length error bar drawn as a bare dot is
a lie. **Load the `dataviz` skill before writing the first chart** (`ui-rules`
§4 asks for it).

*(An earlier plan cited these rules to `06-visual.md` of the design pack. That
pack is at `D:\BACE\ui-brief\` and is historical — it is not in the repo, and
nothing in the repo needs it. All three rules survive in `docs/ui-rules.md`,
§4 and §2, which is where to cite them from.)*

**4 · The UI is testable without the bench, from four fixtures.**

`acceptance/20260902_service-vs-labview/` holds the rig day of 2026-09-02, and
it is better test material than anything that could be written by hand. Feed it
into the same store the WebSocket feeds.

| fixture | drives | what is in it |
|---|---|---|
| `journals/*.jsonl` — 3 files, 58 / 63 / 91 lines | the event and state layer | full `RunQueued → RunStateChanged → NodeStarted → … → RunFinished` lifecycles, the chain `Verdict`s in `20260902_144844.jsonl`, and **two real `RunFailed`** in `20260902_125751.jsonl` |
| `service_153722/run20260902_153722.h5` (`schema = bace-run/2`), and the LabVIEW `.dat` beside it in `labview_150640/` | the transient charts | the traces, at full precision |
| a `bace-jv/2` HDF5 — **to be recorded**, there is none in the repo | the J–V chart | the per-curve voltage, current and density arrays |
| a captured `Hello` frame — **to be recorded**, it is not in the acceptance set | the bench snapshot: the rail and the chain | `data.bench`, which is `GET /bench` whole: instruments, `inferred`, `chain`, `rig`, `verdicts`, `queue`, `state` |

**Two of the four have to be recorded; the journal cannot stand in for either.**

*The bench snapshot.* A journal contains none — grepping the acceptance files
for `bench`, `chain`, `instruments` and `queue` returns nothing, `Hello` is
never journalled, and `InstrumentState` frames are single values
(`{"values": {"shutter": "shut"}}`), which say what one step changed rather
than what the bench is. Since M1 is the rail and the chain, a set without this
would leave the milestone that needs it most with nothing to develop against.

*The J–V curves.* The one HDF5 in the repo is `schema = bace-run/2` — a
transient run, with `axis`, `charge`, `traces`, no curves — and the journal
payload policy reduces `JVCurveDone` to `metrics + label + n_points` with no
arrays. So the chart fixture as it stands covers the transient plots and gives
the J–V component nothing at all.

Capture both off a `--sim` service and again off the rig, and check them in
beside the journals.

**The split is not optional.** The journal payload policy (contract §3) stores
*"enough to render the session log and the history queries, never the traces"*:
a journalled `StepDone` is `index, loop, step, setpoint, axis_value, q, q_mean,
q_std, intensity_w, clipped, verdict` with `decimated: {light: {omitted:
true}, …}`, and a journalled `JVCurveDone` has `metrics` and no voltage or
current arrays. So the jsonl replay exercises every run semantic and cannot
draw a single curve. Charts take the HDF5.

The third shape — a live WebSocket frame, traces decimated with `stride` and
`n_full` — comes only from a running `--sim` service, or from a fixture built
by hand from the HDF5.

**The fixtures do not exercise the reconnect path**, and one line of
`docs/ui-kickoff.md` implies they do. Its "real `seq` gaps" is not so: `seq` in
all three files runs 0…N with no gap at all, which is what the journal is —
one monotonic counter, and `StepPhase`, the only unjournalled frame, consumes
no number. Gaps and `decimated.replay` belong to the *socket*, where a client
falls behind and is dropped at 1008. Test that against a live `--sim --fast`
scan, which the service README says will cause it; the journals cannot.

### What the front end is made of

Plain ES modules and hand-written DOM, served by `--ui DIR` —
`StaticFiles(html=True)` mounted at `/ui`, after every API route, same origin,
so `fetch('/bench')` works with no CORS and no proxy. No bundler, because the
lab PC has no build step. Hash routing, because a static mount has no SPA
fallback. Anything third-party is vendored into `ui/vendor/`; nothing is
fetched from a network at runtime.

The React in `docs/bace-console-round3.html` is the Claude Design canvas
runtime, not a decision about the console. The artboard CSS is flex and grid
with zero absolute positioning, so it ports as layout rather than as pictures.

IBM Plex Sans and Mono come out of `bace-console-round3.html` itself — 24 woff2
files, 387 KB, already in the repo — and go into `ui/fonts/`.

### What it is not

Not the JV console and not the 331 controller (the standing instruction). Not
responsive: one lab PC, 1920×1080 or wider, one window, the rail fixed and
everything else scrolling. Not comfortable: `ui-rules` §1 — *"if a screen feels
comfortable, it is probably hiding something the operator needs."*

---

## Phases

Cut vertically, not horizontally: each phase drives a real path end to end
rather than finishing one layer across all four tabs. Every phase names the
thing that proves it.

### M0 · The event layer, and the fixtures

`ui/` exists, fonts and vendor land, four empty tabs behind a hash router.
`api.js` wraps the routes; `stream.js` owns the WebSocket and everything in
decision 2; `store.js` folds the bench snapshot and the frames into state.

Then the offline replay: all four fixtures from decision 4, feeding that same
store — including recording the two that do not exist yet, the `Hello` that M1
depends on and the `bace-jv/2` file that M3 does. This is a deliverable, not a test written later — from here on every
phase has a deterministic real-data bench to develop against, and the UI can be
worked on with no service running.

**Proves:** a `jv_dark` posted to a live `--sim` reaches `done` with
`kept`/`requested` back; the three acceptance journals replay into the same
store, `RunFailed` included; and — against a live `--fast` scan, not the
fixtures — a client dropped at 1008 reconnects with `since=`, misses no
numbered frame, and redraws from `decimated.replay`.

### M1 · The pinned rail and the chain strip

They come before the cards because they are in *every* view — a cross-cutting
requirement, not part of one tab — and because finishing them exercises the
hardest semantics in `/bench` straight away.

Two of those are worth stating as the acceptance:

- **`how: "inferred"` must be visually distinct from a read-back.** While a run
  holds the worker, the snapshot is the one Start took, with the running step's
  implications overlaid (`service/live.py`). During a `bace`, the bias being
  LIVE and the relay being on the amplifier are *inferred*, not read. A screen
  that shows them identically is telling the operator something it does not
  know.
- **The relay gets its own visual treatment.** `ui-rules` §3: it is the
  interlock, not "info", and the two positions are physically different
  circuits.

The chain strip carries a button per `fix`, calling
`POST /bench/actions/{name}`.

### M2 · The bench cards do work

The generated field and card components from decision 1, the five module cards.
Editing writes the `edited` layer through `PUT`; `null` resets one;
`POST …/params/reset` drops the layer.

Verdicts render three-tier and behave accordingly. **Start is disabled by
`invalid` and by `crit`** — `pipeline.validate` is `valid = not any(c.level in
("invalid", "crit"))`, and both `session.submit` and the Start re-check refuse
on the same pair, so a UI that only blocks on `crit` offers a button that
answers 422. The two are not the same failure and should not look the same:
*"a `crit` is the safety block; an `invalid` here is an instrument that went
away since the Dry run, and a run the builder would refuse must not be started
to fail"* (`service/session.py`). So `crit` keeps the loudest treatment
`ui-rules` §3 reserves for hardware safety, and `invalid` — a malformed tree,
an axis geometry the dataclass refuses, a missing instrument, a `centre_on_voc`
with no V_oc in scope — disables Start while reading as "this tree is not
runnable yet", which is what it is.

`warn` states evidence and never blocks. The fix is the bench action `fix`
names, which the operator clicks.

**Proves:** `ui-rules` §8's bar — a dark J–V in **fifteen seconds** by someone
who has not seen the UI before. This is the first phase whose output an
operator could use.

### M3 · Results become visible, and so does the shot

The scale/axis foundation, then J–V and transient — the transient carrying the
shaded integration window and the running integral, the one plot the LabVIEW
panel had that the operator will look for.

Then the timing diagram, which belongs here because it pairs with M2's form: it
redraws as the form is edited, and it is where a wrong `V_coll` sign or an
absurd delay becomes visible **before** the run.

**Proves:** the run from M2 draws; the HDF5 fixture draws; an operator can
reject a bad shot definition without running it.

### M4 · What a run looks like while it runs

The live monitor: `StepPhase` as the shot-segment indicator, decimated traces
arriving, the per-shot `verdict`, the inferred overlay from M1 now moving as
the run moves. Stop after-shot, abort, and the `NeedsOperator` resume with a
typed `temperature_k`. Then Q per loop and Q(axis), with the zero-width-axis
switch visible rather than silent, and error bars reflecting the loops that
actually ran.

**Proves:** a 2 T × 2 level tree on the simulator stays readable start to
finish, and a client dropped at 1008 during it comes back — via
`decimated.replay` — without losing the loop curve.

### M5 · The pipeline tab

The tree editor, Dry run against `POST /pipelines/validate`, the schedule as
"what it will do, in order" — including a `temperature` module binding the
**rest of the run** rather than the rest of one iteration — the check list
collapsed to `16 checks · 15 ok · 1 warn · show`, and the cost with
`lower_bound: true` rendered as "at least", never as a promise.

**Proves:** the canonical 9 T × 5 level tree can be built, dry-run and
submitted, and the three counters at three scales read correctly against a
4.5-hour sweep's ETA.

### M6 · The results tab

A stub until here. Preceded by a design pass, because R2·3 is a Round 2
artboard and Round 3 left the tab empty — the standing instruction is to
iterate in Claude Design before coding a screen that differs from Round 3. By
this point bench and pipeline are running and the requirements put to that pass
are drawn from real result shapes rather than guessed.

What the view needs decides what gets written, not the other way round: the run
folders on disk today are artefacts of back-end debugging, and R2·3's file list
predates all of it.

### Alongside · The rig tab

Read-only reference, and `ch-rig` is a static schematic that ports as it
stands — the one place "reusable as they stand" is true. Drop it in whenever a
phase runs short.

---

## What blocks what

| before | must be done |
|---|---|
| anything on the rig | the folder-name defect in `docs/naming-plan.md` §2 — `material = "PTQ10:IT-4F"` builds a path segment with a colon, which fails on Windows and passes on Linux |
| M6 | `docs/naming-plan.md` rule 1 — the journal carries the sample block and the temperature triple on every node, **and `GET /runs` exposes them on each row**; writing them into `SessionStarted` alone leaves `run_index()` emitting summaries with no identity |
| M6 | a Round 3 design pass on results, with the user, in Claude Design |
| M3 | nothing — the chart foundation is new code with no service dependency |

Nothing else in the service is on the critical path. The API is complete for
M0–M5 as it stands.

## What this plan deliberately does not decide

- **Which parameters sit above the fold on each card.** A table produced during
  M2, from the Round 3 artboard plus what `GET /modules` actually returns,
  reviewed then rather than guessed now.
- **The results grid's data source.** M6, from the view's needs.
- **Whether to keep `[sample] temperature_k = 290.0` in the recipes.**
  `docs/naming-plan.md` §4 states the problem; it is a bench-habit decision.
