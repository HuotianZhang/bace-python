# The BACE service layer — a plan, v1

> **Status (evening of 2026-09-02)**: P0, P1 and P2 are all done and proven on
> the real rig (the evening section of `history/HANDOVER-2026-09-02.md`); the
> 331 console wiring for temperature was done ahead of P3 (contract §7), and
> the temperature loop settles by itself when `rig.toml` points at a console
> and otherwise pauses on the `NeedsOperator` of this document. P3 still owes
> the results tab (undesigned) and failure recovery. `service-contract.md` is
> the implementation contract; `ui-kickoff.md` is where the console work
> starts.

2026-09-02. Inputs: the Round 3 UI (`design/BACE Console - Round 3.dc.html`),
and the project documents `bace-ui-flow-model.md`, `bace-ui-round2-brief.md`
(the three-tier verdict, source marking) and `bace-open-defects.md` (batch four
= settle before writing `service/`).

## In one sentence

The service layer is **a thin shell around the engine**: one process on
sternwarte, owning the rig, wrapping the **event generators** —
`run_transient_scan`, `run_jv` and the rest — in HTTP + WebSocket, with the
console in a browser as its only client. It rewrites no measurement logic: the
logic stays in `bace/experiment/`, and the service does four things only —
queue, broadcast events, write the journal, execute the pipeline tree.

## Process and concurrency

- **One service process** (the lab PC, `py -3`, bound to 127.0.0.1), FastAPI +
  uvicorn, serving the front-end static files alongside. The VISA instruments
  (2400 / 33220A / 81150A / scope / relay / shutter) are touched by this
  process and nothing else.
- **The existing standalone consoles stay standalone**: the 1918-C on :8918
  (a single-process constraint), the 331 on :8331 (not yet wired). The service
  reaches them over HTTP, exactly as the current code does.
- **At most one RunWorker at a time** (the bench lock). The generator runs on a
  worker thread (VISA I/O blocks); RunRecorder / JVRecorder wrap the generator
  chain the way `tools/scan.py` does it (`storage.recorder.record`); events go
  into an asyncio queue, then fan out to the WebSocket subscribers and the
  journal. (Corrected 2026-09-02: the recorder is not inside the asyncio
  fan-out — see contract §2.)
- **A manual run of one card = a single-node pipeline**, down the same code
  path. This is how the flow model's "manual run and pipeline step share the
  monitor and the history" is actually built.
- **The only concurrency allowed is an observer that never touches the VISA
  bus**: the power monitor goes over HTTP to :8918 and can run alongside a bace
  scan (this is what R3·2 draws); anything on GPIB must go through the one
  worker. That rule also answers "is power a module or a monitor" — it is both:
  `power read` is a module (it takes the worker), `power monitor` is an
  observer (it does not).

## The API, sketched (resources follow the UI's four tabs)

```
GET  /bench                     instrument state + trigger-chain read-back + the
                                rig.toml values in force
POST /bench/read                re-read the chain and the instrument state (the
                                strip's re-read)
POST /bench/actions/{name}      explicit one-click actions: set-33220a-pol-inv ·
                                park · relay …
                                → every action lands in the journal (a "by hand"
                                entry; R3·2 already draws it)
GET  /modules                   the module catalogue + every parameter as
                                {value, source, editable}
PUT  /modules/{m}/params        edit (source becomes edited; reset returns to
                                default)
POST /runs                      start one module run {module, params} → run_id
POST /runs/{id}/stop            {mode: after_shot | abort} — two honest verbs
GET  /runs · /runs/{id}         the journal (the session log and the this-session
                                card)
GET  /runs/{id}/data            light and dark traces / Q(loop) / the J–V curve,
                                feeding the charts inside the cards
POST /pipelines/validate        a tree in → the resolved execution sequence + 16
                                checks + a cost estimate
POST /pipelines                 Start (validates internally first; a crit is
                                refused)
WS   /events                    the whole event stream
```

Dry run = `validate` plus the full schedule returned, touching no output. That
is exactly what the UI's Dry run button is.

## Events and the journal

- **The wire format is the existing dataclass events as JSON** (RunStarted /
  DCMeasured / AxisResolved / InstrumentState / StepStarted / StepDone /
  LoopDone / Progress / RunFinished / RunAborted / RunFailed / Notice), with
  the service adding the envelope `{seq, ts, run_id, node_path}`. `node_path`
  is the position in the pipeline tree (say `T=250K/led=1.020V/bace`), and the
  three counters at their three time scales render straight off it.
- **The journal is one append-only jsonl per session**, with the existing
  RunRecorder (HDF5 `bace-run/2` plus the legacy `.dat`) untouched.
  Append-only is the foundation laid for failure recovery later — recovery is
  not in this round, but the format is settled correctly now.
- Verdicts obey the three tiers (round-2 brief): crit is hardware safety only;
  warn states the evidence and nothing more; the service fixes nothing by
  itself — a correction is an explicit call to `/bench/actions/*`.

## The pipeline executor — the only new logic

`service/pipeline.py`: the tree is `loop(temperature | illumination | repeat)`
with `module` leaves. The executor is responsible for three bindings and
nothing else (the flow model's own words):

1. the illumination loop holds `led_v` and injects it into every child module —
   a module does not carry it;
2. `jv_bace` → V_oc → the `bace` at that same led_v (`centre_on_voc`); a `bace`
   with no V_oc source is refused at validate;
3. a relay transition (disable → switch → enable) is inserted at every
   `jv_* ↔ bace` boundary.

**What an unwired temperature means** (the warn of R3·3): not a prohibition —
the executor emits a `NeedsOperator` event at each T node and pauses, waiting
for `POST /runs/{id}/resume`. The whole tree runs with the temperature set by
hand; once the 331 is wired that node automates itself, and the API does not
change.

The cost estimate comes from the settle times in the journal's history (the
per-temperature table of R3·3), and the ETA is recomputed as it measures.

## To clear out of core before starting (the order is the priority)

1. **`current_sign = -1` into rig.toml**, multiplied in `Infiniium._fetch` and
   carried into the protocol and the simulator alike — the handover's "first
   thing next time round", and the real answer to the sign problem.
2. **Batch four, item 14**: the event envelope and the level labels (`Progress`
   gains `node_path`) are settled in core, not assembled in the service.
3. **Batch two, item 8**: `configure_trigger` moves into the run path — the
   service cannot depend on a side effect of `tools/scan.py`'s pre-check.
4. **Batch three, items 10–11**: parameter provenance (default / run.toml /
   last-used / edited / inherited) — `/modules` consumes it directly.
5. Batch four, items 15–17 (the `LedSource` protocol, deleting
   `core/sequence.py`, splitting `checks.py`) come along for the ride and block
   nothing.
6. (Added 2026-09-02: done in fact, and not listed in the plan.) `run_jv` opens
   the shutter for the light curve, shuts it for the dark one, and yields
   `InstrumentState({"shutter"})`; `run_intensity_series` opens the shutter
   around `measure_dc`; `storage/jv.py` therefore rose to `bace-jv/2`. All of
   them are sequencing gaps the design pack had already recorded (01-modules
   §4, see `ui-rules.md`), not a rewrite of the measurement logic; `bace-run/2`
   is unchanged. The review round then added a `StepPhase` yielded by
   `run_transient_scan` for each segment (the same instrument calls in the same
   order — only the yield is new).

## Phases

- **P0** the core clean-up above.
- **P1** the bench tab works: one-module runs + the event stream + the journal +
  `/bench` reads and actions + parameter provenance. `--sim` (the existing
  simulated drivers) makes this developable away from the bench.
- **P2** the pipeline tab: validate / dry run / the executor (the three bindings
  and `NeedsOperator`) + the cost model.
- **P3** the results tab (not yet designed), failure recovery, and automating
  temperature once the 331 is wired.

## Explicitly not doing

No rewriting the measurement logic; no automatic correction or interpretation
of anything; no multi-client writes (one operator, the lock is at the bench);
no auth (127.0.0.1); analysis (intensity-dependent results and the like) waits
until the results tab is designed.
