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

### Five structural decisions

Everything else follows from these, and each one turns a rule in
`docs/ui-rules.md` from a discipline into a property of the code. (The fifth
was added 2026-09-03, from measuring the first four running.)

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

*Amended 2026-09-03, from building it (M0).* Three things this decision did not
say, and the third changes what it claims.

**The identity is the incarnation, not the id.** `session_id` is
`YYYYMMDD_HHMMSS`, stamped to the whole second, so two processes started inside
the same second share it — and a cursor kept across that restart is the failure
this whole section exists to prevent, arriving through the check meant to catch
it. `data.session.started_at` is the float the session was created at; the pair
settles it.

**A socket you have replaced is still a socket.** The service has already
queued frames on it, and they arrive after the swap. Folded, they push the
cursor above zero and the new socket's `since=0` replay is then discarded as
duplicates — losing exactly what the reconnect was for. The identity check that
`onclose` needs, `onmessage` needs too.

**The ring is not the whole run, so the screen is not only a projection of the
stream.** `RING_SIZE` is 5000 envelopes; one 100 x 51 scan is more than three
times that in `StepStarted`/`StepDone`/`Progress` alone. So a console opened —
or reloaded — late in a long run replays a *tail*: no `RunQueued`, no
`RunStarted`, no `AxisResolved`, and `/bench` carries that run's summary, not
its shape. `since=0` bounds the cost; it does not promise completeness.

The rule that follows: **the screen is a projection of the event stream, and of
the run's own record where the ring falls short.** Ask `GET /runs/{id}` and
`GET /runs/{id}/data` for the current run at boot, and again whenever the
stream counts a gap — the same truncation arriving a different way. Nothing so
fetched may move anything backwards: the stream is the newer source for
whatever it has already said, and a count is a **maximum**, never an
assignment, or the shots the ring happened to carry would drag `kept` below
what actually ran.

*Amended again 2026-09-03, from building the rail (M1).* There is a third
thing the stream does not carry, and it is not a shortfall of the ring: **the
bench snapshot**. `GET /bench` is the last read-back, and a read-back is a job
on the worker — which, while a run is on it, is running the run. So for the
length of a scan the service answers that stale snapshot with the running
step's implications overlaid, every field of it marked `how: "inferred"`
(`service/live.py`), and that overlay reaches a client only if the client
asks. Nothing on the socket carries it. A console that fetched `/bench` at
boot and at `parked` — which is what M0 did — shows a cold rail through hours
of a run driving the device: relay on the SourceMeter, bias off, shutter shut.

So the third source is the bench itself, and the rule is **the stream says
when to ask**: on the frames `live.py` folds (`NodeStarted`, `RunStarted`,
`StepPhase`, `InstrumentState`, `JVStarted`/`JVFinished`, `NodeDone`), throttled,
because the shutter moves twice a shot. Not a poll on a timer, and *not* a
second implementation of the overlay in JavaScript: the service stays the only
thing that infers anything, which is `ui-rules` §6 — render provenance, never
re-derive it — applied to the one place it would have been tempting. `/bench`
touches no instrument and takes no lock (`app.py:307`), so the cost is a
loopback request per shot phase.

*Amended 2026-09-03, from measuring it in a browser.* That last sentence was
wrong, and wrong in a way that made the rail worse than not asking at all.

**The request is not free while the stream is busy.** `GET /bench` answers in
5 ms on an idle bench and 6 ms during a run *with nobody listening* — and in a
**median of 3.0 s** during the same run with one subscriber. The run is not
what costs it; the fan-out is. `app.py`'s pump did `await queue.get()` and
`await ws.send_text(...)` in a loop, and neither suspends when it has no reason
to — `Queue.get` takes its fast path while the queue is non-empty, and
`send_text` returns as soon as the transport accepts the bytes — so a pump with
a backlog ran `_dumps` on twenty-kilobyte frames without ever giving the loop
back, and every HTTP handler waited behind it. One `await asyncio.sleep(0)` per
frame fixes it: 3.0 s → **13 ms**, and the same scan finished *faster* (a median
of 14.1 s → 9.8 s over three repeats), because the producer's own callbacks
were queued behind the pump too.

**A replayed frame has to ask like any other.** The console gated the refetch on
the stream's `replay` flag, reasoning that one read-back per historical frame
would be hundreds of requests. The throttle already made that impossible — the
whole boot replay coalesces into one — and the guard cost the rail exactly the
frames it exists for: after a 1008 drop the client reconnects, `head` becomes
the service's newest seq, and *every* frame to the end of a fast scan is at or
below it. Measured over a 21 x 60 scan, the rail changed **twice** and stood
still for 10.1 s of a 10.6 s run. The guard is gone; the policy is
`ui/lib/watch.js`, with its own tests.

Together: the rail now changes 9 times in that run and its worst stale stretch
is 2.1 s, which is the throttle.

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
| a J–V HDF5 — **recorded in M0** as `ui/fixtures/jv_sim.h5`, with `jv_sim.json` beside it in the shape `GET /runs/{id}/data` answers | the J–V chart | the per-curve voltage, current and density arrays. *Re-recorded 2026-09-03 with `pixel_area_cm2 = 0.04`*: as first recorded both curves carried `density: null`, because `run.toml` has no `[jv]` table and a J–V left to the recipe reports amps — which left the mA/cm² axis M3 draws with nothing behind it |
| a captured `Hello` frame — **to be recorded**, it is not in the acceptance set | the bench snapshot: the rail and the chain | `data.bench`, which is `GET /bench` whole: instruments, `inferred`, `chain`, `rig`, `verdicts`, `queue`, `state` |
| *(added 2026-09-03)* a recorded **pipeline** stream | node identity, and the counters | two `bace` nodes under one `run_id`, each numbering its shots from one, and the loop's `Progress(node_path="rep=1")` beside the leaf's `node_path: ""` |
| *(added 2026-09-03, M1)* `GET /bench` **taken mid-scan** | the rail's inferred overlay | `inferred: ["relay", "bias", "led", "shutter"]`, the bias LIVE at its two levels and the shutter open — none of which exists in a snapshot at rest |

**Two of the four have to be recorded; the journal cannot stand in for either.**
(A fifth was added while building M0, for the reason a fixture is ever added:
a defect needed frames nothing in the set carried. Node identity is invisible
in every single-node recording, and a frame written by hand would have been an
assumption about the wire rather than the wire.)

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

*Recorded 2026-09-02 (M0).* Both exist now, off `--sim`, and they landed in
**`ui/fixtures/`** rather than beside the journals: the browser can only read
what is served under `/ui`, and the acceptance set is a rig-day record that
simulator output does not belong in. `tools/record_ui_fixtures.py` writes them
— `--tag rig` records the same set on the bench, which is still to do — and
every file says `sim` in its name, because a simulated J-V is a
plausible-looking curve. The journals stay where they are; the offline page
reaches them through the repo root (`tools/serve_ui.py`). The one thing a
browser cannot read at all is the HDF5, so `tools/make_ui_fixtures.py` renders
the rig day's `bace-run/2` in the shape `GET /runs/{id}/data` answers.

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
`docs/ui-kickoff.md` implies they do. (`tests/test_ui.py` now asserts the
correction: all three journals are 0…N with no gap.) Its "real `seq` gaps" is not so: `seq` in
all three files runs 0…N with no gap at all, which is what the journal is —
one monotonic counter, and `StepPhase`, the only unjournalled frame, consumes
no number. Gaps and `decimated.replay` belong to the *socket*, where a client
falls behind and is dropped at 1008. Test that against a live `--sim --fast`
scan, which the service README says will cause it; the journals cannot.

**5 · The model is the render key, and the store keeps what the ring keeps.**

*Added 2026-09-03, from measuring M0 and M1 in a browser.*

The store notifies on every batch of frames, and a `--sim --fast` scan batches
one per animation frame for the length of the run. With four shell renderers
and a view all rebuilding themselves on every notify, one 21 x 60 scan built
**110 078 DOM elements** — for a rail that changed twice, a module table that
changed twice and a strip that changed once.

That is not only waste, and the waste is not the argument. A rebuilt element is
a *different* element: it drops the operator's text selection mid-copy (proved
in a browser — the selection on the SMU cell is gone within 2.5 s of a running
scan), and from M2 it would take the focus and the caret out of a parameter
field the moment a frame arrived. Every renderer here is already a pure model
plus a DOM function, so the rule is the one that shape makes available: **build
the model, and touch the DOM only where it differs** (`dom.keyed`). Nothing has
to remember to invalidate anything, which is the one thing a hand-maintained
dirty flag always gets wrong. 110 078 elements became 21 829, and the selection
survives.

The same principle bounds the store. A decimated shot is ~14.6 kB of heap, so
a 3780-shot scan held **61 MB** and the canonical 9 T x 5 level tree of M5 —
forty-five leaves of the same size — would be around 830 MB, which is the tab,
not a chart. The store now keeps the traces of the last `RING_TRACES_KEPT`
shots, which is *the service's own number and the service's own scope*:
`session._traces` is one `deque(maxlen=200)` for the whole session, appended on
every `StepDone` whatever run or node it belongs to. So the ring replays the
traces of the last 200 shots anyone took and strips the rest, and a console
that had ever been dropped already held exactly those while one that had not
held everything. Mirroring it makes the two the same console. Older shots keep
every scalar and their verdict and read `tracesGone`, which is the word the
store already had for a replayed shot — so no chart needs a second case.
61 MB became 13.7 MB.

*Corrected 2026-09-03, from a review of the change.* The first version of this
capped **per node**, which is neither what the service does nor a bound: 200
per leaf across those forty-five leaves is 9000 shots of arrays, ~130 MB. One
ring of 200 is ~3 MB whatever the tree. The lesson is the one this whole
section is about — the invariant was stated as "what the ring keeps" and then
implemented as something else, and only reading the service's own declaration
settled it.

Neither is a micro-optimisation to do later: both are properties of the layer
M2–M6 are written *on*, and both get more expensive to retrofit with every
card, chart and field added above them.

*And M2 proved that, 2026-09-03.* The cards landed before this decision did,
rendering on every store notify: all six were rebuilt **737 times each in four
seconds** of a scan, and the operator's caret went with them — focus a
parameter field and it is gone within 2.5 s, on an *idle* bench too, because a
power monitor at 1 Hz is enough to do it. The fix is this decision applied
where it was written for: `cardModel` is already the pure function an entry
becomes rows through, so its output is the key. Two details the shell did not
need: a card is replaced **in place**, because detaching an element blurs
whatever inside it had the focus and a rebuild of the `bace` card must not take
the caret out of the `jv` card beside it; and the model must carry no clock, or
the 700 ms `/bench` refetch above would rebuild every card twice a second and
the fix would arrive through the fix (`ui/tests/fields.test.mjs` pins that).
Idle with a monitor ticking: zero rebuilds. Through a scan: six, in the three
cards whose read-back row actually moved. 435 506 elements became 19 762, and
the rail’s worst stale stretch went back to the 2.8 s the shell alone gets.

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

**Done 2026-09-03.** `ui/` is the shell, `lib/{api,stream,store,format,dom}.js`
and `replay.html`; `ui/README.md` says how to run it. All three proofs hold and
are `ui/tests/live.test.mjs`, against a service the runner starts: the drop
happened at 1008 with 3860 numbered frames, none missing and none twice, and
replaying that run from `since=0` returned 1060 of its 1260 shots with the
arrays gone and the scalars intact.

Three places where the phase differed from the paragraph above, none of them
consequential and all of them worth saying:

- **the tabs are not empty.** Each renders read-only what the store holds — the
  chain, the module list, the run, the session log, `rig.toml` — because a fold
  you can read off the screen is worth more than one you can only read off a
  test. M1 and M2 replace all of it; none of it decides anything they own.
- **`ui/vendor/` does not exist.** Nothing third-party is used yet, so there is
  nothing to vendor. The rule ("vendored, never fetched") stands for when there
  is.
- **only the `--sim` half of the fixtures is recorded.** `--tag rig` records the
  same set on the bench and is still to do; every file says `sim` in its name
  until then, because a simulated J-V is a plausible-looking curve.

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

**Done 2026-09-03.** `ui/lib/rail.js` is the rail and the strip: a pure
`railModel(state)` — the eight cells of §1, each `{value, sub, level,
inferred}` — with the DOM beside it, so `ui/tests/rail.test.mjs` holds every
rule below down against two recorded snapshots and no browser. Both are
**shell furniture in `app.js`, not the bench tab's**: they are in every view,
which is the reason they came first, and the bench tab lost its duplicate chain
card to the strip. The design is `docs/design/BenchRail.dc.html`, whose five
states are not a mode switch — they are five snapshots, and the model draws
whichever one the service last answered.

The two acceptances hold:

- **`how: "inferred"` is not a read-back.** The mark rides on the cell's label
  with a dashed rule under the value — the artboard's own language for a value
  that is true because something else made it true. Mid-scan four cells carry
  it and four do not, and the four that do not are the ones a run implies
  nothing about (the SMU outside a J-V, the V_oc, the power meter, the
  temperature).
- **The relay is a circuit, not a level.** Three nodes and the closed side,
  from the artboard; and *"neutral"* — the device in neither circuit — is said
  only when the service answered, never as the empty state.

Four things the phase turned up that the paragraph above did not say:

- **A refusal was unreadable, and it is the whole point of the strip.** The
  service's own handlers answer `{"error": …}` (`app.py:123`) and only
  FastAPI's answer `{"detail": …}`; M0's `ApiError` read `detail` alone, so
  *"the 33220A output is ON; the chain fix is made with the LED off"* reached
  the operator as `POST /bench/actions/set-33220a-pol-inv -> 409`. `error.text`
  now carries the sentence, and `level`/`refused` come with it.
- **A refusal that names a remedy needs a button for it.** The chain fixes are
  made with the output off, so the one warning a cold bench actually shows —
  `33220A POL NORM` with the LED on — refuses. Without `led-off` the operator
  is told what to do and given no way to do it. The strip offers the
  prerequisite the contract names, as its own click: one action per button,
  never chained.
- **`fix` on an `ok` check is the action that made it ok.** Offering it again
  is noise on a strip that is on every screen, so only a check that reads wrong
  gets a button. Everything else is disabled while a run holds the worker,
  because every action but `park` answers 409 then.
- **Park arms first while a run is active.** It aborts the run *and cancels the
  queue*; that is the right behaviour and the wrong thing to do on a stray
  click, so the button becomes "abort the run and park?" and does it on the
  second.

**Proved** on `--sim`: the fix path end to end in a browser — refused, `led-off`,
`set INV`, chain 3/4 → 4/4, the rail's LED warning gone and the bar's chain
chip with it; park during a run aborting it and cancelling a queued `jv_dark`;
and `ui/tests/live.test.mjs` now carries M1's own — the rail reads LIVE,
inferred, relay on the amplifier while a run holds the worker, and nothing
inferred outlives it.

**Then measured, 2026-09-03**, which is the part M1 had not done: a headless
browser on a live `--sim --fast` scan, counting what the shell does per frame
rather than reading what it draws. It found that the rail M1 exists to keep
honest was standing still for 91 % of a run, and that the shell was building a
hundred thousand DOM elements to keep it that way. Decision 2's amendment and
decision 5 are what came of it; the numbers, before → after on one 21 x 60 scan
(1260 shots):

| | before | after |
|---|---|---|
| rail changes during the run | 2 | 9 |
| longest stretch with the rail unchanged | 10.1 s | 2.1 s |
| worst `GET /bench` | 8.1 s | 49 ms |
| DOM elements built | 110 078 | 21 829 |
| JS heap at the end | 36.2 MB | 9.8 MB |
| a selection held on the rail | lost | kept |
| shots folded | 1260 / 1260 | 1260 / 1260 |

At 3780 shots the same changes are 232 272 → 70 460 elements and
61.1 → 13.7 MB, and the longest stale stretch 11.0 s → 2.8 s.

One of them is in the service, not the console: `app.py`'s WebSocket pump never
yielded to the event loop, so every HTTP handler queued behind it. It is
written up in decision 2's amendment, because that is where the wrong claim
was.

### M2 · The bench cards do work

The generated field and card components from decision 1, the **six** module
cards — `jv`, `jv_bace`, `bace`, `light`, `power`, `temperature`. Which fields
sit above the fold on each, and in what order, is `docs/ui-fields.md`, produced
at the start of this phase against a live `--sim` service as this plan said it
would be. Editing writes the `edited` layer through `PUT`; `null` resets one;
`POST …/params/reset` drops the layer.

**Five cards became six during this phase**, and the catalogue changed under
it: `jv_dark` was doing two jobs and admitting to one, so it split into `jv`
(sweep only, touching neither shutter nor LED, labelling the curve from a
read-back) and `light` (the shutter and the LED, as a node). Everything below
this line that still says `jv_dark` is a record of a phase that was finished
before the split, and is left as it happened.

`light` is the one card whose primary control is **not** a Run: a run whose
only module is `light` is `invalid`, because the park that ends every run would
undo it (`light.undone-by-park`). Its buttons are bench actions — the same ones
M1's chain strip posts, one action per click. The module stays a node for M5's
trees, where it goes before the step that needs the light.

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

The 422 a refused Start answers reaches the card the way M1's refusals reach
the strip: `error.checks` for the list, `error.text` for the sentence. Both are
on `ApiError` since M1, and neither is `detail`.

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

**Built 2026-09-03.** `lib/scale.js` and `lib/charts/frame.js` are the
foundation — a model per chart, one renderer for any of them, the same split
`railModel` has had since M1 — and `lib/charts/{jv,transient,timing}.js` are the
three components. They land in the card that owns them, through a **slot**
`views/bench.js` fills and keys separately (`lib/results.js`): the chart moves
with every shot and the form does not, and one key for both would rebuild six
cards' worth of fields between two shots of a scan, which is decision 5's
failure with a chart in front of it.

Three decisions worth recording, because each departs from something:

* **No two y scales in one panel.** `ch-transient` overlays the running
  integral on the photocurrent with a right-hand axis. §4 asks for the space,
  not the overlay, so the integral is a third panel on the same x: the charge
  still flattens where the photocurrent decays, and nothing crosses a curve it
  shares no scale with.
* **The illumination ramp is single-hue.** The design's five values are a grey
  pair spliced to a red trio, and in OKLab the first and third are the same
  lightness (0.78), as are the second and fourth (0.68). A sequential encoding
  that cannot be ordered is not one, so the ramp is the red half continued.
* **The timing diagram is on the `bace` card**, as this file's own component
  table says, rather than on the rig tab where R3·4 draws it. Confirmed with
  the user, 2026-09-03.

**Then measured**, on the same 21 x 60 `--sim --fast` scan M1 and M2 were held
to. The first cut redrew every chart on every shot — 130 a second under
`--fast`, 4043 redraws and 77 282 SVG elements for a plot no eye can follow,
and it took `GET /bench` with it (median 31 ms, worst 204 ms) because they
share a main thread. `REDRAW_MS` in `lib/results.js` is the answer, and it is
`REFETCH_MS` for pixels: 4043 → 184 redraws, 77 282 → 3 543 elements, 42.7 →
11.0 MB of heap, the worst `GET /bench` 204 → 56 ms, and the longest stale
stretch back to the rail's own 2.1 s throttle. A shot on the rig takes ~0.8 s,
so on the bench nothing is throttled; this exists for the simulator. The
result key splits into a form half and a data half so that the two cadences
can differ — typing draws at once, shots draw through the throttle — and the
deferred draw paints the newest shot rather than the one that was pending.

**Seven more came out of review.** Four are claims about the instrument: the
record begins one division *before* the trigger (`:TIM:POS` is four of ten
divisions — and both recordings' `t0` agree, −200 ns at 200 ns/div); an unread
LED polarity was drawn as the expected INV rather than left unknown;
`Math.abs(null)` is a finite 0, so a missing sample became a point on the dark
sweep's log floor; and a `pulse`-referenced integration window was pinned to
the form's `delay_ns` instead of the shot's own setpoint, which stands still on
exactly the axis that moves it. Two are the J–V's colour and identity under
`both_directions`: the ramp ranked curves rather than illumination levels, so
one level's two arms became the brightest and the darkest, and they shared a
series key, so the crosshair drew the reverse arm's value in the forward arm's
colour. The seventh is M5's, arriving early — the result panel found its run by
`RunQueued.module`, which is `null` for a pipeline, so a tree's nodes drew
nothing on the cards that produced them. It is per node now, which is what this
file's own M5 note asks for.

And three faults that the models could not show, found by rendering the thing
in a browser: a thinned column placed at a *fractional* index collapsed to
x = 0 wherever the x mapping was a lookup into the sample times; the
light-at-the-sample waveform, whose late edge wraps past the end of the period,
drew a line travelling backwards across the panel; and half the timing
captions printed over the waveform above them. All three are held down now —
the first two by `tests/scale.test.mjs` and `tests/charts.test.mjs`, the third
by the row geometry that gives every signal its own caption line.

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

**Built 2026-09-03.** `lib/monitor.js` is the run monitor — a pure
`monitorModel(state)` with the DOM beside it, the split the rail set — and
`lib/charts/loops.js` is the fourth chart component, Q per loop or Q(axis)
with the switch in its caption. The newest shot's own line — Q, the running
mean and σ at its point, the peaks, the digitiser's verdict and which
`trigger_sweep` was in force — is `shotBlock` in `lib/results.js`, at the top
of the `bace` card's panel with the transient and the loop chart under it, and
the timing diagram moved below the run: a form being edited during a scan is
the *next* run, and the one going is what the operator is watching. Nothing in
the service changed, as the table below predicted.

Two recordings were added for it, because nothing in the set carried a pause,
a loop's `Progress` at three scales, or a run that ended any way but `done`
or `failed`: `stream_tree_sim.jsonl` is the 2 T × 2 level tree itself with
both `NeedsOperator`s answered by the recorder, and `stream_stopped_sim.jsonl`
a 20-loop scan stopped `after_shot` at loop 13 — 60 requested, 39 kept.
`tools/record_ui_fixtures.py --only tree,stopped` records them — and the
tree is **simulator-only, and never in the default set**, which a review
caught: it answers its own temperature pause with the setpoint plus a tenth
of a kelvin, so against a real service it would command the cryostat, give up
sixty seconds later where a real settle is 14 minutes to 2 hours, measure at
whatever the sample was actually at, and write that invented number into every
folder name with `temperature_how = "operator"`. The step refuses on any mode
but `sim` rather than trusting `--only` to be typed carefully.

Two decisions depart from the artboards, and both are recorded here:

* **The monitor is shell furniture, under the rail, not a header on the
  running card.** R3·2 draws the counters, the segment and the two stop
  buttons on the card, and §11 says *"the running card is the monitor"*. But
  a pipeline's pause belongs to a loop node — `T=250K` — which has no card,
  and a stop is about the bench, not about a view: at hour three of a
  temperature sweep the prompt has to be answerable from whichever tab is
  open, which is the same argument that put Park on the strip. So the
  counters, the segment, the ETA, stop, abort and the operator's answer sit
  in one strip on every tab; the *evidence* — the shot, its verdict, the
  trace, the loop curve — stays in the card that produced it, which is the
  half of §11 that was about reading rather than acting.
* **The monitor's buttons are keyed apart from its counters.** Measured
  first as one row: 4850 rebuilds in an eight-second `--fast` scan, and a
  Stop pressed between two shots would have landed on a button that no
  longer existed — the M2 finding, on the one control that must not miss.
  Three keyed parts now: the counters move with every shot, the buttons only
  with the run's state, and the prompt only with *which* pause it is, so a
  `TemperatureRead` arriving mid-keystroke does not take the caret.

**Measured**, on the 2 T × 2 level tree with 30 loops of 21 points per leaf —
2520 shots in 8.3 s under `--sim --fast`, driven from a headless browser
through the console's own prompt: both pauses answered from the monitor, the
client dropped at 1008 twice and reconnected twice, 2707 shots replayed
without their arrays, and the run `done` with 2520 of 2520. A caret placed in
a `bace` field after the run started stayed there for the whole of it and
left only with the one card rebuild the run's end causes. The heap ended at
13.4 MB against M3's 11.0; the monitor's counters were rebuilt 3997 times,
its buttons 16.

**Three things the measurement found**, none of which the fixtures could:

* **The segment indicator goes dark under `--fast`, by the service's own
  policy.** A subscriber more than `EPHEMERAL_BACKLOG` (fifty) frames behind
  is not queued another `StepPhase` — the frame says where the shot *is*,
  and a client that far behind would read it after the shot — so on a scan
  that outruns every socket the indicator shows the first few shots and then
  nothing. It is not a console fault and it is not fixable in one: proven
  instead on `--sim` without `--fast`, where a shot takes about a second as
  it does on the rig, and the indicator walks `2 · light settle → 5 · dark
  settle` shot after shot.
* **A replayed `StepStarted` must not clear the phase of the shot in
  flight.** After a drop the ring's numbered frames arrive behind the live
  ephemeral ones: the store folded `StepStarted` for shot 300 while the
  instrument was inside shot 560, and cleared the phase on every one. The
  phase carries its own `index`; only a start at or after it clears it now.
* **The ETA has to count down between boundaries.** The executor re-derives
  it at every loop boundary, which for the outermost loop of a temperature
  sweep is once every half hour; repeated as the number the frame carried,
  it read `9.5 s` for four seconds. The finish time is what stands; the
  seconds left are measured from the newest frame's clock.

And one thing it found in the service, filed rather than fixed: a simulated
`bace` with the LED driven above its threshold reports a photocurrent peak of
about two million amps and a charge of `−0.04 C`, ten orders above the
`3.65e-10 C` the simulated device claims to store. The manual fixtures never
showed it because they drive the LED at exactly the threshold, where the
photocharge is zero; the tree fixture does. The console draws it faithfully,
which is the point of the console.

### M5 · The pipeline tab

The tree editor, Dry run against `POST /pipelines/validate`, the schedule as
"what it will do, in order" — including a `temperature` module binding the
**rest of the run** rather than the rest of one iteration — the check list
collapsed to `16 checks · 15 ok · 1 warn · show`, and the cost with
`lower_bound: true` rendered as "at least", never as a promise.

**Proves:** the canonical 9 T × 5 level tree can be built, dry-run and
submitted, and the three counters at three scales read correctly against a
4.5-hour sweep's ETA.

**Built 2026-09-04.** `lib/tree.js` is the model — the tree as typed, the edit
operations, the flat schedule nested back into the shape it was a walk of, and
the cost — with `views/pipeline.js` beside it, the split the rail set in M1.
`lib/charts/schedule.js` is the fifth chart component, R3·3's `ch-schedule`:
the run as a length of time, settle in grey and measuring in accent, one pair
per temperature. Nothing in the service changed, as the table below predicted.

Two fixtures were recorded for it, and both are answers rather than runs —
`POST /pipelines/validate` touches nothing, so these are the only two in the
set that can be recorded on a live bench at any time:
`validate_txill_sim.json` is the canonical tree itself (9 × 5, 90 module runs,
198 steps, 4500 shots) and `validate_bound_sim.json` the two shapes it has
none of — a `temperature` **module**, and duplicate sibling modules, which the
service numbers `bace` and `bace#2`.

Four decisions, and the first is the shape of the whole screen:

* **The console validates on every commit, not only on Dry run.** The bench
  tab already does this per card, and a pipeline has more ways to be wrong
  than a card has. So the check list, the cost, the counters and the schedule
  are never older than the tree — and the corollary is that **nothing on this
  screen is counted by the console**. How many levels `1.010 → 1.030 step
  0.005` makes is a rounding question `pipeline.range_values` settled once
  (rounded, never truncated: the LabVIEW truncation wrote a four-level loop as
  five), and the editor reads the answer off the resolved tree that comes back
  beside the schedule. Before the first answer a range row says the range and
  no count. Measured at **110 ms and 340 kB** an answer on the canonical tree,
  nearly all of it the schedule's 90 resolved ParamSets, so the calls are
  coalesced and a commit landing during one supersedes it.
  **Dry run** is the same request made deliberately — which is what it is for
  after a bench read-back.
* **The schedule is drawn at three scales, and flat behind a switch.** 198
  steps is exactly the number `ui-rules` §5 exists to refuse; the nesting is
  which temperature, which level, how far into the scan, the same three the
  monitor draws during the run. The flat list is still there, because "in
  order" literally means the list and a screen that only ever summarised would
  be hiding it.
* **A module node's form is the bench's form.** `cardModel` and the field
  component from `lib/card.js`, over the ParamSet the *schedule* resolved for
  that node — so `led_v` reads as inherited from the illumination loop and
  `voc` as derived from the `jv_bace` two nodes earlier, and which rows sit
  above the fold is the one table in `lib/fields.js` rather than a second
  opinion about what matters. The one thing the node form does not show is the
  point count in a range row: `estimate_text` is the *bench's*, computed from
  the bench's axis, and no count is better than one describing another form.
* **The tree is matched to its schedule entries by counting iterations.** A
  module child takes one entry, a loop child takes the `count` consecutive
  ones its own `detail` declares — so nothing in `ui/` reconstructs a node
  path or a `#2` sibling token, which are the service's spelling and stay
  there. `tree.test.mjs` proves it by rewriting every node path in the
  recorded schedule and checking the rows land the same.

**Six things the building found**, four of them only visible with the thing
running:

* **The temperature in force is not in the schedule.** `Step.detail.
  temperature_k` is the enclosing *loop's* setpoint and is `null` under a
  `temperature` module — the resolver knows about loops. So a schedule drawn
  from the schedule alone shows a run with no temperature anywhere on it. The
  console re-walks the steps with the executor's own rule instead (root **and**
  every open scope, which is what makes it bind the next iteration of an
  enclosing loop too), and the walk is asserted against the resolver's own
  `detail.temperature_k` wherever a loop did set one.
* **Adding a module selects it, so the next module went to the root.**
  Composing `jv_bace` then `bace` inside an illumination loop put the second
  one outside it — and the console described that perfectly: 45 J-Vs, 9 scans,
  and `⚠ V_oc` on every one, because a `bace` outside the loop has no
  `jv_bace` at its own drive level in scope. It was right, and it was not the
  tree anyone was building. A node now lands inside the selected loop, or
  beside the selected module.
* **An illumination level's settle is *measuring* to the cost model**
  (`pipeline.estimate` folds it in through `measured`), so a time bar summing
  only the module estimates came out 90 s short of the total printed under it.
  Both fixtures now assert that the bar and the cost total the same run.
* **A bench edit and a node override both resolve as `edited`.**
  `ParamSet.update_layer(Source.EDITED, node.params, "pipeline node")` merges
  into the same layer, so a reset offered on every `edited` row would — for a
  value the bench typed and this node did not — remove a key the node never
  had and change nothing. Whether *this node* types it is the authority, and
  the tree is the client's own, so nothing has to parse a `detail` string for
  it.
* **A stretch of measuring outside a temperature loop has no settle to be
  unknown about.** The first cut hatched it and reported "1 of 1 temperatures
  have no measured settle" for a tree with no temperature loop in it at all.
* **Nine full-height blocks of accent read as nine alerts.** §3 reserves that
  intensity for one thing on screen; the artboard gives the bar a quarter of
  its height, and the caption in the panel's top corner — added before anyone
  looked at it — printed over the last two setpoint labels.

**Measured**, driving the console's own controls from headless Chromium
against `--sim --fast`: the canonical tree built from an empty pipeline in
nine clicks and four typed values, **13 validates** for the whole of it
(one per commit), no console error; the counters reading `9 temperatures ·
5 levels · 90 module runs · 4500 shots`, the cost `at least 84 min` with
`waiting for T —`; Dry run; Start accepted, `POST /pipelines` queued, and the
run paused at `T=295K` with the monitor's operator prompt open — then aborted
from that same monitor. A `n_loops` override typed on the `bace` node read as
`as on the bench, except n_loops 40`, its reset put it back, and `n_averages`
— edited on the bench, not on the node — offered no reset at all.

**And measured again with a scan running under it**, because this tab is the
one that describes the *next* run while the bench works on this one — the
argument that put the timing diagram below the run in M4, applied to a whole
view. A 21 × 60 scan on the simulator, 1260 shots in 6.5 s, 8888 frames, with
the canonical tree open:

| | M5 |
|---|---|
| DOM mutations inside the pipeline tab, for the whole scan | **29** |
| elements built, whole console (the rail, the chips, the monitor) | 6 262 |
| `POST /pipelines/validate` during the scan | 0 |
| JS heap at the end | 14.2 MB (M4's was 13.4) |
| one commit — nine temperatures to seven — elements built | 515 |

Twenty-nine is the Start button's own state changing as the worker is taken
and given back. Nothing on the stream reaches this view: it reads whether the
worker is held, the catalogue, and `read_at` — and no shot, phase or progress
frame moves any of the three, so the subscription returns without rendering.
`read_at` is stable through a scan by construction, because a read-back is a
job on the worker and the worker is running the run, which is also why no
validate is asked for during one.

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

### What M0 taught the phases after it

Twenty-one findings came out of seven rounds of review on the M0 pull request,
every one of them real, all in the event layer — and several belong to phases
that have not started. They are here so they are not rediscovered there:

- **M1 (the rail).** `state` on `/bench` is fetched twice — at boot and when a
  run parks — so between them only the stream knows the bench is busy: fold
  `RunStateChanged` through the service's own mapping (`session._bench_state`;
  a terminal state is `stopping` while the worker parks, `parked` is the
  transition to `idle`). The run being parked **still owns the worker**, so it
  stays the current run until `parked`. The queue is folded from the frames too,
  or it only changes when the bench is re-read. And the snapshot's `verdicts`
  are the *whole* answer, not an addition to the list held: `chain_verdicts()`
  omits a check that now reads ok, so a warning the operator has just fixed
  disappears **by being absent**, and a merge can never see that.
- **M2 (the cards), M4 (the monitor).** `stopping` is said the moment a stop is
  accepted and the worker's own `preflight`/`running` follow it; they must not
  displace it (`Session._apply_state` refuses too), or the rail tells the
  operator their stop lapsed. A `NeedsOperator` that a stop, an abort or a
  failure ended gets no `OperatorResumed` — nobody answered it — so the prompt
  dies with the run.
- **M3 (the charts).** `JVCurveDone.density` is **mA/cm²** on the wire.
  *Corrected 2026-09-03: it was A/cm², and this entry used to say so, with the
  factor of a thousand the chart had to apply itself.* The factor now belongs
  to the quantity — it is applied once, where the pixel area is
  (`experiment.jv.current_density`) — so a chart converts nothing and a
  formatter picks no prefix, and a client that still does either is out by a
  thousand in the other direction. `metrics.jsc` is the exception that proves
  it: still **A**, because it is interpolated from the current array. And a
  shot arrives without arrays two ways — the ring dropped them
  (`decimated[…].replay`) or the journal never stored them
  (`decimated[…].omitted`) — which are the same case on screen: no curve, and
  the loop point comes from the scalars.
- **M5 (the pipeline).** A pipeline is one `run_id` over **many nodes**, and
  each module node numbers its shots from one, so `loop:index` is not an
  identity: the second `bace` lands on the first's. Everything node-scoped —
  the axis, the values, the V_oc, the loop charges, `RunFinished` — is kept per
  node, or whichever node finished last overwrites the rest. And §5's three
  counters are three *events*: the executor's `Progress(node_path="T=250K")`
  beside the leaf's `node_path: ""`, which is what `Progress.node_path`'s own
  docstring says a consumer should read.

---

## What blocks what

| before | must be done |
|---|---|
| anything on the rig | the folder-name defect in `docs/naming-plan.md` §2 — `material = "PTQ10:IT-4F"` builds a path segment with a colon, which fails on Windows and passes on Linux |
| M6 | `docs/naming-plan.md` rule 1 — the journal carries the sample block and the temperature triple on every node, **and `GET /runs` exposes them on each row**; writing them into `SessionStarted` alone leaves `run_index()` emitting summaries with no identity |
| M6 | a Round 3 design pass on results, with the user, in Claude Design |
| M3 | ~~nothing — the chart foundation is new code with no service dependency~~ **built 2026-09-03**; nothing in the service changed |
| M4 | ~~nothing~~ **built 2026-09-03**; nothing in the service changed. The simulator's lit-photocurrent magnitude is a defect in `--sim`, not on the path |
| M5 | ~~nothing~~ **built 2026-09-04**; nothing in the service changed. One thing the schedule does not carry was found and worked around in the client rather than added to the wire: `Step.detail.temperature_k` is the enclosing loop's setpoint and is `null` under a `temperature` module, so the console applies the executor's binding rule itself |

Nothing else in the service is on the critical path. The API was complete for
M0–M5 as it stood, and M5 proved it: nine clicks and four typed values built
the canonical tree against an unchanged service.

## What this plan deliberately does not decide

- **Which parameters sit above the fold on each card.** A table produced during
  M2, from the Round 3 artboard plus what `GET /modules` actually returns,
  reviewed then rather than guessed now.
- **The results grid's data source.** M6, from the view's needs.
- **Whether to keep `[sample] temperature_k = 290.0` in the recipes.**
  `docs/naming-plan.md` §4 states the problem; it is a bench-habit decision.
