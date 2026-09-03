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
| `index.html`, `app.js`, `style.css` | the shell: the rail, four tabs behind a hash router, the stream bar |
| `lib/api.js` | every route in `docs/service-contract.md`, wrapped once |
| `lib/stream.js` | the WebSocket, and the whole reconnect discipline — no view knows about any of it |
| `lib/store.js` | the fold: the `/bench` snapshot and the `/events` frames become the state every view subscribes to |
| `lib/format.js` | the number rules of `docs/ui-rules.md` §2, including the two zeros that are absences |
| `lib/dom.js` | `h()`, and nothing else |
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

M0 is done: the event layer, the fixtures, and four tabs that are honest about
being stubs. What building it taught — the rules the review turned up, and
which later phase each one belongs to — is recorded in `docs/ui-plan.md`,
under "What M0 taught the phases after it". M1 is the pinned rail and the chain strip — they come before the
cards because they are in every view, and because they exercise the hardest
semantics in `/bench` straight away: `how: "inferred"` must be visually
distinct from a read-back, and the relay gets its own treatment because it is
the interlock, not "info".

The phases, and what each one has to prove, are in `docs/ui-plan.md`.
