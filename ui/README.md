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
| `lib/watch.js` | when to ask `GET /bench` again — which frames move the bench, and the throttle that collapses a scan's worth of them into one request per 700 ms |
| `lib/dom.js` | `h()`, and `keyed()`: rebuild an element only when its model differs from the one already on screen |
| `lib/replay.js`, `replay.html` | the offline bench: fixtures fed into the same store the socket feeds |
| `views/` | bench · pipeline · results · rig. Stubs, and each says which milestone fills it |
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
| `jv_sim.json`, `jv_sim.h5` | the J-V chart. The journal reduces `JVCurveDone` to metrics with no arrays, so this had to be recorded |
| `hello_sim.json` | the bench snapshot — the rail and the chain. No journal contains one |
| `bench_running_sim.json` | `GET /bench` taken **mid-scan**: the four instruments the run implies, every one `how: "inferred"`. Nothing at rest carries a single one |
| `stream_bace_sim.jsonl`, `stream_jv_sim.jsonl` | the wire: traces decimated with `stride`/`n_full`, and the `StepPhase` frames that are live-only |
| `stream_pipeline_sim.jsonl` | node identity: two `bace` nodes under one `run_id`, each numbering its own shots from one, and the loop's `Progress` beside the leaf's |

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
* **The store keeps the traces the ring would replay**, `RING_TRACES_KEPT` of
  them per node — the service's own number, so a console that has been dropped
  and one that has not hold the same thing. A decimated shot is ~14.6 kB, so
  3780 of them was 61 MB and M5's canonical tree would have been ~830 MB.

Reproduce any of it with a browser and `playwright-core`; the shapes are in
`ui/tests/render.test.mjs`, which holds both client-side rules down without one.

**M2 is next**: the generated field and card components, the five module cards,
the `edited` layer through `PUT`, and Start disabled by `invalid` *and* by
`crit`. Its bar is `ui-rules` §8 — a dark J-V in fifteen seconds by someone who
has not seen the UI before.

The phases, and what each one has to prove, are in `docs/ui-plan.md`.
