# `ui/` — the console

The front end `docs/ui-plan.md` describes, built against the finished service.
Plain ES modules and hand-written DOM, no bundler and no build step: the lab PC
has none, and the service serves these files itself.

```
python -m bace.service --sim --fast --port 8900 --ui ui      # the console, live
python3 tools/serve_ui.py                                    # no service at all
```

The first mounts this directory at `/ui` after every API route, same origin —
so `fetch('/bench')` works with no CORS and no proxy. The second serves the
repo root for the offline bench at `/ui/replay.html`, which needs
`acceptance/` in reach as well as `ui/`.

## What is here

| | |
|---|---|
| `index.html`, `app.js`, `style.css` | the shell: the bar and its chips, the rail, four tabs behind a hash router, the chain strip, the stream bar |
| `lib/api.js` | every route in `docs/service-contract.md`, wrapped once |
| `lib/stream.js` | the WebSocket, and the whole reconnect discipline — no view knows about any of it |
| `lib/store.js` | the fold: the `/bench` snapshot and the `/events` frames become the state every view subscribes to |
| `lib/format.js` | the number rules of `docs/ui-rules.md` §2, including the two zeros that are absences |
| `lib/rail.js` | the pinned rail and the chain strip: `railModel(state)` is a pure function of the store, the DOM is beside it |
| `lib/fields.js` | which fields sit above the fold on each card and in what order, as data, plus `cardModel` — the pure function one `GET /modules` entry becomes a card's rows through |
| `lib/card.js` | the generated card and the one field component every parameter goes through: provenance rendered, `doc` under the field and `doc_full` on hover |
| `lib/watch.js` | when to ask `GET /bench` again — which frames move the bench, and the throttle that collapses a scan's worth of them into one request per 700 ms |
| `lib/dom.js` | `h()`, and `keyed()`: rebuild an element only when its model differs from the one already on screen |
| `lib/svg.js` | `h()` in the SVG namespace, because `createElement('svg')` is an `HTMLUnknownElement` — a tag with the right name and no geometry |
| `lib/scale.js` | the scale and axis foundation, and the only thing in `ui/` written from nothing: linear and log scales, nice ticks, and the min/max thinning that keeps a transient's peak when 4000 samples go into 450 pixels |
| `lib/charts/frame.js` | stacked panels over one x axis (or one each), their gridlines, captions, shading and crosshair. Every chart returns a *model* and this draws any of them |
| `lib/charts/jv.js`, `transient.js`, `timing.js` | the three M3 components, as pure functions of the data — the same split the rail has |
| `lib/charts/loops.js` | the M4 component: Q per loop, or Q(axis), with the switch between them in the caption — `ui-rules` §4's zero-width axis is a repeat, and the chart says so rather than drawing a curve through it |
| `lib/results.js` | what goes in a card's result slot, and the key it is rebuilt on: the chart moves with every shot, the form does not. Since M4 the newest shot's own line comes first — Q, mean and σ at its point, the peaks, the digitiser's verdict and which `trigger_sweep` was in force |
| `lib/monitor.js` | the run monitor (M4): `monitorModel(state)` is a pure function of the store — the loops at their three time scales, the shot, its segment from `StepPhase`, the ETA counting down, which of stop / abort / cancel apply, and the prompt a `NeedsOperator` opens — with the DOM beside it, under the rail on every tab |
| `lib/replay.js`, `replay.html` | the offline bench: fixtures fed into the same store the socket feeds |
| `views/` | bench is M2's six generated cards with M3's charts in three of them; pipeline · results · rig are stubs, and each says which milestone fills it |
| `fonts/` | IBM Plex Sans and Mono, Archivo — 24 woff2, 387 KB, lifted out of the Round 3 mockup by `tools/extract_ui_fonts.py`. Nothing is fetched from a network at runtime |
| `fixtures/` | see below |
| `tests/` | `node --test ui/tests/…` — and `tests/test_ui.py` runs them from the Python suite, skipping where there is no Node |

Hash routing, because a `StaticFiles(html=True)` mount has no SPA fallback:
`/ui/bench` would be a 404, `#/bench` is not.

## The four rules the socket is written around

Each is a bug in a client that does the obvious thing; `lib/stream.js` carries
them and `ui/tests/stream.test.mjs` holds them down.

* **`seq` belongs to a service session, not to a socket.** A restarted service
  starts its counter at zero, so a cursor carried across the restart replays
  nothing and then discards every numbered frame the new process sends.
* **Reconnect, not just reset.** `since` is fixed when the socket opens and the
  replay is computed after `Hello`, so a session change closes the socket and
  opens a new one — with `since=0`, which is *not* the same request as no
  `since` (the replay is guarded by `if since is not None`).
* **A `Hello` on a reconnect must not advance the cursor.** `data.seq` is the
  newest seq there is; the replay that follows carries lower ones.
* **Falling behind is normal.** `--sim --fast` outruns any socket: a `Notice`
  carrying `data.since`, then close 1008, then reconnect. Older replayed
  `StepDone` frames come back with their traces gone and
  `decimated[…].replay` set — *redraw the loop curve, the trace is gone*.

## The fixtures

`docs/ui-plan.md` decision 4 names four. Two were already in the repo and two
did not exist; `tools/record_ui_fixtures.py` records those two off a running
service, and `tools/make_ui_fixtures.py` renders the rig day's HDF5 in the
shape `GET /runs/{id}/data` answers, because a browser cannot open HDF5.

| fixture | what it is for |
|---|---|
| `../acceptance/20260902_service-vs-labview/journals/*.jsonl` | the event and state layer: full lifecycles, the chain verdicts, **two real `RunFailed`**. Not copied in here — the offline page reaches them through the repo root |
| `transient_20260902_153722.json` | the transient charts, from that day's `bace-run/2` at full precision |
| `jv_sim.json`, `jv_sim.h5` | the J-V chart. The journal reduces `JVCurveDone` to metrics with no arrays, so this had to be recorded. Both curves carry a **density in mA/cm²** — re-recorded 2026-09-03 with `pixel_area_cm2 = 0.04`, because `run.toml` has no `[jv]` table and a J-V left to the recipe reports amps and `density: null`, which left the unit with nothing behind it |
| `hello_sim.json` | the bench snapshot — the rail and the chain. No journal contains one |
| `bench_running_sim.json` | `GET /bench` taken **mid-scan**: the four instruments the run implies, every one `how: "inferred"`. Nothing at rest carries a single one |
| `stream_bace_sim.jsonl`, `stream_jv_sim.jsonl` | the wire: traces decimated with `stride`/`n_full`, and the `StepPhase` frames that are live-only |
| `stream_pipeline_sim.jsonl` | node identity: two `bace` nodes under one `run_id`, each numbering its own shots from one, and the loop's `Progress` beside the leaf's |
| `stream_tree_sim.jsonl` | the M4 tree, 2 T x 2 levels x a three-point scan of two loops: both `NeedsOperator`s and the recorder's answers to them, the executor's `Progress` at three scales, and a `TemperatureRead` typed by a person |
| `stream_stopped_sim.jsonl` | a 20-loop scan stopped `after_shot` at loop 13 — 60 requested, 39 kept: the one `RunAborted`, `stopping`, `stopped`, and a node that ended `stopped` in the set |

Everything with `_sim` in its name came off `--sim`, and says so in its name on
purpose: a simulated J-V is a plausible-looking curve, and must never be
mistaken for a measured one. `--tag rig` records the same set on the bench.

**The journals do not exercise the reconnect path.** Their `seq` runs 0…N with
no gap — that is what a journal is, and `tests/test_ui.py` asserts it rather
than leaving the claim in a document. Gaps and `decimated.replay` belong to the
socket, and are proved against a live service:

```
python -m bace.service --sim --fast --port 8900
BACE_SERVICE=http://127.0.0.1:8900 node --test ui/tests/live.test.mjs
```

## What is built, and what is next

**M0** is the event layer and the fixtures. **M1** is the pinned rail and the
chain strip — shell furniture, in every view, because that is what they are:

* the eight live values of `docs/ui-rules.md` §1, from `docs/design/BenchRail.dc.html`;
* **`how: "inferred"` drawn as what it is.** While a run holds the worker the
  snapshot is the one Start took with the running step's implications overlaid
  (`service/live.py`), so during a `bace` the bias being LIVE and the relay
  being on the amplifier are *inferred*, not read — a dashed rule and a mark on
  the label, never the same as a read-back;
* **the relay's own treatment**: it is the interlock, and the two positions are
  physically different circuits, so it is a three-node diagram rather than a
  level colour;
* **the strip's fixes**, one `POST /bench/actions/{name}` per check that reads
  wrong, never automatic, disabled while a run holds the worker — and when the
  bench refuses one, its sentence, with a button for the remedy that sentence
  names.

The rail is kept alive by asking `/bench` again on the frames that move the
overlay: the snapshot reaches a client only if it asks, and a console that
asked at boot and at `parked` would draw a cold bench through hours of a scan.
The service stays the only thing that infers anything.

### What a run costs the console

M1 was proved by reading the screen. It was then **measured** — a headless
browser on a live `--sim --fast` scan, counting what the shell does per frame —
and the measurement found the opposite of what the screen showed: the rail M1
exists to keep honest changed twice in a 10.6 s run and stood still for 10.1 s
of it, while the shell built 110 078 DOM elements saying nothing new.

Four things came out of it, all of them in the layer M2–M6 will be written on,
and all of them cheaper now than after the cards and the charts exist:

* **`GET /bench` is not free while the stream is busy.** 5 ms idle, 6 ms during
  a run with nobody listening, a **median of 3.0 s** during the same run with
  one subscriber. `app.py`'s pump never yielded — neither `Queue.get` nor
  `send_text` suspends when it has no reason to — so every HTTP handler waited
  behind a tight loop of `_dumps` on twenty-kilobyte frames. One
  `await asyncio.sleep(0)` per frame: 3.0 s → 13 ms, and the scan itself got
  *faster*.
* **A replayed frame asks for the bench like any other** (`lib/watch.js`). The
  old `replay` guard suppressed the refetch for the whole tail of a fast scan,
  which is exactly when the rail is showing an overlay it can only get by
  asking. The throttle is what bounds the cost, and always was.
* **The model is the render key** (`dom.keyed`). A rebuilt element is a
  different element: it drops the operator's selection mid-copy, and from M2 it
  would take the caret out of a parameter field on every frame. 110 078
  elements → 21 829, and the selection survives.
* **The store keeps the traces the ring would replay**: `RING_TRACES_KEPT` of
  them, in one ring for the store, because `session._traces` is one
  `deque(maxlen=200)` for the whole session — so a console that has been
  dropped and one that has not hold the same thing. A decimated shot is
  ~14.6 kB, so 3780 of them was 61 MB and M5's canonical tree would have been
  ~830 MB. (Capped per *node*, as this first was, it would still be ~130 MB
  across that tree's 45 leaves; a review caught it.)

Reproduce any of it with a browser and `playwright-core`; the shapes are in
`ui/tests/render.test.mjs`, which holds both client-side rules down without one.

**M2 is built**: `lib/fields.js` decides which rows a card has, `lib/card.js`
draws them, and `views/bench.js` is the loop between them — six generated
cards, the `edited` layer through `PUT`, and Start disabled by `invalid` *and*
by `crit`.

**M3 is built**: the scale and axis foundation, then the J–V, the transient
and the timing diagram, each in the card that owns it.

**M4 is built**: the run monitor under the rail, the newest shot's verdict
beside its trace, and Q per loop / Q(axis). **M5 is next** — the pipeline
tab: the tree editor, the Dry run against `POST /pipelines/validate`, and the
schedule as "what it will do, in order".

### What a run looks like while it runs

`lib/monitor.js` is shell furniture like the rail: it appears under it on every
tab for as long as a run holds the worker and is gone when the bench is parked.
`docs/ui-rules.md` §5 is the whole shape of it — three counters at three time
scales, never one number:

* the loops, outermost first, each from the executor's own `Progress` for that
  loop node (`T 250 K · 1 of 2 · LED 1.020 V · 2 of 2`), and a temperature
  that is settling reads *waiting for the operator*, never as a step that runs;
* the shots, from the leaf's own count (`shot 4 of 630 · loop 2 · point 1 of 21`);
* the segment, from `StepPhase` (`5 · acquire light`) — live-only, so it is
  shown only while the stream carries it;
* the ETA, the executor's re-derived one, counting down from the newest
  frame's clock rather than repeating the number the frame carried.

And the three verbs: `after_shot` (the honest one: the shot in flight completes
and is kept), `abort` (armed first, like Park — a discarded shot is a shot of
the sample's life), and `cancel` for a run still queued. A `NeedsOperator`
opens the prompt: what to set the cryostat to, which temperature of how many,
the reading polled while the person decides when a controller is attached, a
field for the `temperature_k` it actually reached and a note, and what a resume
with nothing typed will bind. The prompt is keyed on *which* pause it is, so a
reading arriving mid-keystroke does not take the caret; the buttons are keyed
on the run's state, so a Stop pressed between two shots lands on a button that
still exists.

The evidence stays in the card that produced it. The `bace` card's panel is,
during and after a run: the newest shot — Q, the running mean and σ at its
point, the peak of each trace, the digitiser's verdict, and `trigger AUTO` said
out loud when a charge near zero could be a loose sync cable (§9) — then the
transient, then Q per loop or Q(axis), then the timing diagram, which describes
the *next* run.

The loop chart (`lib/charts/loops.js`) is one component with the switch inside
it, and the switch is in the caption on every render: a zero-width axis plots
Q per loop with the running mean and its ±σ band, and says *repeats, not a
curve*; a swept axis plots Q(axis), the mean over the loops so far with an
error bar where σ exists and a hollow square where it does not — one loop
leaves `q_std = 0`, and a zero-length error bar drawn as a dot is a lie. A
truncated run reads *kept of requested* and, on a repeat, draws the loops it
never ran as the hatched space they would have filled. It reads only the
scalars of a shot, which is what lets it survive a reconnect that replayed
the shots without their arrays.

### What the tree cost the console

Measured on the 2 T x 2 level tree with 30 loops of 21 points per leaf — 2520
shots in 8.3 s under `--sim --fast`, driven from headless Chromium through the
console's own prompt (`ui/tests/live.test.mjs` holds the same proof down
against the service without a browser):

| | M4 |
|---|---|
| pauses answered from the monitor's prompt | 2 of 2 |
| client dropped at 1008 · reconnected | 2 · 2 |
| shots replayed without their arrays | 2707 |
| shots folded | 2520 / 2520, every leaf's 21 points with a σ |
| the monitor's counters rebuilt · its buttons rebuilt | 3997 · 16 |
| card rebuilds | 38, at the run's edges and the read-back rows that moved |
| JS heap at the end | 13.4 MB |
| a caret held in a `bace` field | kept for the whole run; lost only to the one rebuild the run's end causes |

Three findings, written up in `docs/ui-plan.md` M4: the segment indicator goes
dark under `--fast` because the service stops queueing `StepPhase` for a
subscriber fifty frames behind (`session.EPHEMERAL_BACKLOG`) — proven instead
on `--sim` at the rig's cadence, where it walks the segments shot after shot; a
replayed `StepStarted` from behind must not clear the phase of the shot in
flight; and the ETA has to count down between the boundaries the executor
re-derives it at.

### What the charts are for, and the three places they part from the artboards

The domain rules are the component, and every one of them is a test in
`tests/charts.test.mjs` rather than a paragraph here: `density` is mA/cm² and
the chart converts nothing; no pixel area means the axis is **amps**, never a
density with an invented area; `metrics.jsc` keeps its amps because it is
interpolated from the current; light, dark and photocurrent share a frame,
because the photocurrent alone hides the failure where both parents sit on the
digitiser's rail; a shot with no arrays states which of the two absences it is;
σ_Q = 0 is *not recorded*.

Three departures, all deliberate:

* **The running integral gets its own panel.** `ch-transient` puts it on a
  second y scale inside the photocurrent panel. Two scales in one frame make
  the crossing point mean nothing, and `ui-rules` §4 asks for the *space*, not
  the overlay — stacked on the same x, the charge still flattens where the
  photocurrent decays, and there is one axis per panel.
* **The illumination ramp is one hue.** The design's five values
  (`#bab6b6 · #9b9797 · #ff9783 · #ff563c · #ae1800`) splice a grey pair to a
  red trio, and measured in OKLab the first and third have the *same*
  lightness, as do the second and fourth. Five levels that cannot be put in
  order are not a sequential encoding, so this is the red half continued,
  strictly darkening 0.78 → 0.48.
* **The timing diagram is on the `bace` card**, where `docs/ui-plan.md` puts
  it, not on the rig tab where R3·4 draws it. It is a function of the *form*:
  it redraws as the operator types, and a wrong `output_polarity`, a delay
  before its own trigger or a duty that has shortened the illumination is
  visible before the run rather than in the file afterwards.

The diagram is the code's own arithmetic, not a picture of it — the seven
`StepPhase` segments in the order `run_transient_scan` yields them (six under
`dark_reference = "same"`, which is also the case where `dark_settle_s` never
sleeps at all), the shutter moves and the power read placed where the code
does them since those are not reported, the lit fraction as `100 - duty` under
the 33220A's INV, and the 81150A at INV resting the device at V_coll so
extraction never stops (`docs/README.md`, the overturned table ④).

### What a scan costs the console, with the charts in it

M1 and M2 were each measured in a headless browser on a live `--sim --fast`
scan rather than read off the screen, and M3 is held to the same bar. Same
shape as before — one 21 x 60 scan, 1260 shots, about nine seconds — with a
parameter field focused *after* the run started, so the one card rebuild that
`busy` causes is not mistaken for the thing being measured:

| | M3, first cut | M3, shipped |
|---|---|---|
| chart redraws | 4 043 | **184** |
| SVG elements built | 77 282 | **3 543** |
| HTML elements built | 23 818 | **10 275** |
| JS heap at the end | 42.7 MB | **11.0 MB** |
| `GET /bench`, median | 31.4 ms | **16.8 ms** |
| `GET /bench`, worst | 204 ms | **56.4 ms** |
| longest stretch with the rail unchanged | 2.9 s | **2.1 s** — the rail's own throttle |
| a caret held in a parameter field | kept | kept |
| shots folded | 1260 / 1260 | 1260 / 1260 |

The first cut redrew every chart on every shot, which under `--sim --fast` is
130 redraws a second of a plot no eye can follow — the M1 finding again, at
chart scale, and it took `GET /bench` with it because they share a main
thread. The answer is the one the rail already uses: **`REDRAW_MS`, a
`REFETCH_MS` for pixels** (`lib/results.js`). A shot on the rig takes about
0.8 s (`journal.shot_time_s`), so on the bench nothing is throttled at all;
it exists for the simulator.

The two cadences are not the same, and the key says so. `resultKeys` splits
into a **form** half and a **data** half: anything the operator did — a
keystroke, a bench read-back — draws at once, because a timing diagram that
lags half a second behind the typing is not a function of the form; a shot
draws through the throttle, and the deferred draw paints the *newest* shot
rather than the one that was pending, because the frames in between are an
animation nobody asked for.

The card and its charts are keyed **apart**. One key for both would rebuild
six cards' worth of fields between two shots, which is decision 5's failure
with a chart in front of it: measured over the scan, 27 card rebuilds against
184 chart redraws, and the caret survives.

### What the review found

Seven defects, all real, and all but one in the claims the charts make about
the instrument rather than in how they draw:

* **The record does not begin at the trigger.** `configure_timebase` writes
  `:TIM:RANG` of ten divisions and `:TIM:POS` of *four*, so the record runs
  from one division before the trigger to nine after it. Drawn from zero,
  every edge and the whole shaded window sat a division early, and a window in
  the tenth division was shown inside a record that had already ended. Both
  recordings agree with the correction: at 200 ns/div the sim trace carries
  `t0 = −200 ns` and the rig day's `−199.5 ns`.
* **An unread LED polarity was drawn as INV.** `?` is what a driver answers
  for a failed query and an absent chain is a bench nobody has read; neither
  is the expected polarity. It now stays unknown — no light waveform, a
  `warn`, and the Sync edge's meaning left unstated.
* **`Math.abs(null)` is 0, and 0 is finite.** A sample the instrument never
  returned became a point on the log floor with the dark curve drawn through
  it: a leakage measurement out of an absence.
* **The integration window was pinned to the form.** With
  `t0_int_reference = "pulse"` the service recomputes it from each shot's own
  `:PULS:DEL1`, and `run-bace.toml` sweeps exactly that axis — so the shading
  stood still while the real window moved with every point. It takes the
  shot's setpoint now, and `trigger_offset_s` with it.
* **A pipeline's results never reached the card that produced them.** A
  pipeline's `RunQueued.module` is `null` and the module names live on the
  nodes, as `NodeStarted.data.kind` — so the result panel is per *node* now,
  which is what M0 left on record for M5 anyway: each node numbers its shots
  from one, and the run-level pointer is whichever node moved last.
* **The J–V ramp encoded the curve, not the illumination.** Under
  `both_directions` one level's forward and reverse arms took the brightest
  and the darkest slot — two illuminations that never existed. And they shared
  a *key*, so the crosshair built two dots with one identity, updated the
  first for both, and drew the reverse arm's value in the forward arm's
  colour.

### What rendering it found

The models are tested in `node`, and three faults were still only visible in a
browser — which is the argument for looking at the thing:

* every thinned column whose midpoint was not a whole number **collapsed to
  x = 0**, because the x mapping is routinely a lookup into the sample times
  and `time_s[1234.5]` is `undefined`;
* the light-at-the-sample waveform, whose late edge wraps past the end of the
  period, drew a second line **travelling backwards** across the panel;
* half the timing diagram's captions printed over the waveform above them.

Reproduce with `python3 tools/serve_ui.py` and `ui/replay.html`, which now
draws all three charts off the fixtures with no service at all.

The cards are keyed on their own model, for the reason the rail is: measured
before it, all six were rebuilt **737 times each in four seconds** of a scan,
and the operator's caret went with them — focus a parameter field and it is
gone, on an idle bench too, because a power monitor is enough. `cardModel` is
the pure function an entry becomes rows through, so its output is the key, and
a card is replaced *in place* so rebuilding one does not blur a field in
another. Idle with a monitor ticking: zero rebuilds. Through a scan: six, in
the three cards whose read-back row actually moved.

The phases, and what each one has to prove, are in `docs/ui-plan.md`.
