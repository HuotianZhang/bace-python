# BACE service layer — contract v1

2026-09-02. This is the implementation contract for `bace/service/`, derived from
`docs/service-plan.md` (the plan), the Round 3 console design
(`docs/bace-console-round3.html`, artifact *UI mockups: Pipeline bench timeline*)
and the design pack, historical since 2026-09-02 — what survives of it is
`docs/ui-rules.md`, and the canvas source is `docs/design/`. Where this file
and the plan
disagree, the plan wins; where this file and the code disagree, fix one of them
and say so here.

The service is **a thin shell around the engine**: one process on the lab PC,
bound to 127.0.0.1, owning every VISA instrument, wrapping the existing event
generators (`run_transient_scan`, `run_jv`, and the DC characterisation the
intensity series does) in HTTP + WebSocket. It rewrites no measurement logic.
It does four things: queue, broadcast events, keep a journal, execute the
pipeline tree.

---

## 1. Package layout

```
bace/params.py            provenance machinery (Source, ParamSpec, ParamSet …)   [round 1]
bace/experiment/events.py Envelope, Progress.node_path, the service vocabulary  [round 1]
                          (+ StepPhase and RunQueued, 2026-09-02 review round)
bace/experiment/wire.py   to_wire / envelope_to_wire                            [round 1]
bace/drivers/readback.py  ask / on_off / number / word: the read-only queries   [review round]
                          the real drivers' read_state()/read_output() spell

bace/service/
  __init__.py
  __main__.py     python -m bace.service [--sim] [--fast] [--host] [--port 8900]
                  [--out runs] [--rig rig.toml] [--run run.toml] [--ui DIR]
  session.py      Session: one service process lifetime. Owns the Bench, the
                  Catalogue, the Journal, the RunWorker, the run registry.
  rigs.py         Bench: builds a Rig (simulated or real), reads instrument
                  state back, performs the explicit bench actions.
  modules.py      Catalogue: the runnable modules, their ParamSets with
                  provenance, and build(module, params, ctx) -> generator.
  journal.py      Journal: append-only JSONL per session; history queries
                  (last-used params, settle times, run index).
  worker.py       BenchLock + RunWorker thread + RunHandle (stop / abort /
                  resume) + the asyncio bridge that fans events out.
  wire.py         thin: envelope creation and the WS/journal payload policy
                  (which arrays are decimated, which are omitted).
  pipeline.py     Tree schema, node paths, validation (the check catalogue),
                  schedule resolution, cost model, dry run. Pure logic.
  executor.py     run_pipeline(rig, schedule, ctx) -> Iterator[Event]: the three
                  bindings, relay transitions, NeedsOperator, recorders, park.
  monitors.py     the observers: the power monitor and the temperature monitor
                  (HTTP, no VISA, run beside a run).
  live.py         what the running step implies about the instruments, folded
                  from the run's events, overlaid on /bench as `inferred`.
  app.py          create_app(session) -> FastAPI. Routes, WS, static mount.
```

Dependency rule, enforced by `tests/test_architecture.py`: `core/` imports
nothing outward; `drivers/` nothing from `experiment/storage/service`;
`experiment/` and `storage/` nothing from `service`. `bace.service` imports
`pyvisa` and the real drivers **lazily, inside `rigs.py`**, so `--sim` works on a
machine with no VISA backend. FastAPI/uvicorn are optional dependencies
(`pyproject.toml` extra `service = ["fastapi>=0.110", "uvicorn>=0.29", "websockets>=12"]`;
the lab PC needs the rig's VISA stack too, which is the `lab = ["bace[rig,service]"]`
extra); `bace.service.pipeline`, `modules`, `journal`, `worker`, `executor` must
import without FastAPI installed — only `app.py` and `__main__.py` need it.

**Engine changes made in round 1 beyond the plan's five items** (recorded here
so "only the round-1 items changed" is true, the plan's 不重写测量逻辑
notwithstanding; each is a sequencing gap the design pack had already
recorded, the design pack 01-modules §4, `docs/ui-rules.md`): `run_jv` opens the shutter for a light
curve and shuts it for a dark one (`_set_shutter`) and yields
`InstrumentState({"shutter": …})` per curve; `_set_illumination` no longer
swallows a failing `led.off()` and skips the settle on a bare-SMU rig;
`run_intensity_series` unblocks the shutter around `measure_dc`; and
`storage/jv.py` is therefore schema **`bace-jv/3`** (`/config/resolved`, a
per-curve `shutter` attribute, and — since the `jv`/`light` split of
2026-09-03 — a per-curve `illumination` of `dark`/`light`/`unknown`, with
`dark` *absent* rather than False on an unknown curve). The transient recorder's `bace-run/2` is
unchanged. The review round of 2026-09-02 added one more engine change:
`run_transient_scan` yields `StepPhase` between the same instrument calls in
the same order (§3), and nothing about a measurement moved.

Python on sternwarte: `C:/WPy64-31370/python/python.exe` (3.13). Lab PC: `py -3`.

---

## 2. Process and concurrency model

- **One `Session`** per process. `session_id = "YYYYMMDD_HHMMSS"` of start.
- **One `RunWorker` thread** owns every instrument. Everything that touches
  VISA is a *job* on that thread: a run, a bench read-back, a bench action.
  Jobs are executed one at a time; a run is a long job. This is the **bench lock**.
- The worker pulls events from the job's synchronous generator and hands each
  to the asyncio side with `loop.call_soon_threadsafe(queue.put_nowait, …)`. The
  asyncio side (one task) enriches the envelope, writes the journal, and fans out
  to WebSocket subscribers. **Recorders run on the worker thread**, inside the
  generator chain (`storage.recorder.record`), exactly as `tools/scan.py` does.
- **Queue**: `POST /runs` while a run is active does not fail; it queues. FIFO,
  single operator. A queued run may be cancelled (`POST /runs/{id}/stop` while
  queued → state `cancelled`). The design shows one card live at a time; the
  queue exists so a second click is not lost.
- **Observers** that do not touch the VISA bus run concurrently with a job:
  the power monitor (HTTP to `:8918`) and the temperature read-back (HTTP to
  `:8331` when configured). Nothing else.
- **Stop semantics** (two honest verbs, from the plan):
  - `after_shot`: set the job's abort flag; every generator polls it at its
    natural boundary (transient: before each step; jv: before each curve;
    executor: before each node). The shot in flight completes and is kept; the
    generator yields `RunAborted(reason="requested", done, total)` and unwinds
    through its `finally` (outputs off, shutter shut).
  - `abort`: the worker calls `gen.close()` at the next event it receives (a
    blocking VISA call cannot be interrupted; the next yield is the earliest
    honest moment). `GeneratorExit` unwinds every `finally` in the chain; the
    shot being acquired when the close lands is abandoned mid-flight and
    nothing after it starts (a shot that had already completed and yielded its
    `StepDone` before the close is kept, as it came); the worker itself then
    emits `RunAborted(reason="aborted", done, total)`, counting from the last
    Progress and the `StepDone`s it saw (the module's own Progress describes
    the shot *before* the one it just reported, so the `StepDone` count is
    used when it is ahead). An abort-aware `RunContext.sleep` (§7) returns a
    settle or hold early, so the abort is not dead for the length of a hold.
  - Both end with `Rig.park()` and `RunStateChanged("parked")`.
- **Pause** exists only as `NeedsOperator`: the executor blocks on a
  `threading.Event` inside the generator until `POST /runs/{id}/resume`. Stop
  and abort still work while paused (the wait polls the flags every 0.2 s).
  A resume answers the pause that is open and no other: `Job.resume` keeps the
  operator's detail only while the job is paused (armed the instant the worker
  sees `NeedsOperator`, cleared when `wait_for_operator` returns), so a resume
  that lands while the run is measuring — a double-clicked button, a retried
  request — is refused with 409 **and dropped**. Kept, it would answer the next
  temperature pause the instant it opened, measuring the subtree with the
  cryostat wherever it was and tagging it a temperature nobody confirmed.

### Run states

```
queued → preflight → running ⇄ paused → stopping → parked
                         │                          ├─ done      (finished)
                         ├─ blocked (crit at start) ├─ stopped   (after_shot honoured)
                         │                          ├─ aborted   (shot discarded)
                         │                          ├─ failed    (exception; error recorded)
                         │                          └─ cancelled (removed from the queue)
                         └──────────────────────────┘
```

Every terminal state is reached through `parked` (except `cancelled` — a job
removed from the queue never touched the bench). `RunStateChanged(state,
reason)` is emitted on every transition and journaled. `blocked` is its own
terminal state (`worker.Blocked` raised by `make()`), reached when a `crit`
check fails on the read-back taken at Start — or an `invalid` one, an
instrument that went away since the Dry run: the run never touched the bench,
and a client keying on the state can tell "the bench was unsafe" from "the code
crashed" without parsing the error. The Start re-check validates against the
V_oc the tree was submitted with, not the session's current one, so a jv_bace
queued behind a run cannot change what that run was validated against. `stopping` is reported by the session the
moment a stop is accepted, not at the job's next event (which for a long shot is
seconds away), and the worker's own `preflight`/`running` reports that follow a
stop accepted during preflight do not displace it on the record (they are still
journaled as the transitions they were).

### IDs

- `run_id = f"{session_id}-{n:03d}"`, `n` counting every run in the session
  (manual and pipeline alike). A manual run IS a pipeline run with one module
  node, same code path (plan: 手动跑一张卡 = 单节点 pipeline).
- A module execution inside a run is addressed by `(run_id, node_path)`.
- `seq` is a per-session monotonic counter over every envelope (events from
  runs and from the service itself). `GET /events?since=<seq>` replays.

---

## 3. Events on the wire and in the journal

Wire format = `experiment.wire.envelope_to_wire(Envelope)`:

```json
{"seq": 1412, "ts": 1788390180.512, "run_id": "20260902_205200-003",
 "node_path": "T=250K/led=1.020V/bace", "type": "StepDone",
 "data": {…the dataclass as JSON…},
 "decimated": {"light.y": {"n_full": 4000, "stride": 4}, …}}
```

- `run_id` is `null` and `node_path` is `""` for service-level events that
  belong to no run (`BenchAction`, `PowerReading`, `TemperatureRead`, the bench
  `Verdict`s).
- `node_path` for a manual run is the module name (`"bace"`); for a pipeline it
  is the tree position (`"T=250K/led=1.020V/bace"`, see §7). Events emitted by
  the executor about a loop node carry that loop's path.

**Payload policy** (`service/wire.py`), the one place that decides sizes:

| event | WebSocket | journal |
|---|---|---|
| `StepDone` | `light`, `dark`, `photo`, `photo_averaged` decimated to ≤ 1000 points (stride, first and last kept — the last kept sample is the record's final one, at `t0 + (n-1)*dt`, not one stride after its predecessor) | scalars only: index, loop, step, setpoint, axis_value, q, q_mean, q_std, intensity_w, clipped + `verdict` |
| `StepPhase` | in full, **live only**: `seq` null (like the drop `Notice`), not in the `since=` ring | not written |
| `RunFinished` | `photo_averaged` omitted; `values`, `q_mean`, `q_std`, `q_all` in full | same minus `q_all` if > 10 000 cells |
| `JVCurveDone` | in full (≤ a few hundred points) | metrics + label + n_points + the other scalars (index, dark, led_level_v, direction, intensity_w); no arrays |
| `JVFinished` | in full | its curves reduced exactly as `JVCurveDone` — otherwise the arrays the table keeps out come back through the summary |
| `LoopDone`, `AxisResolved`, `SeriesPointDone` | in full | in full |
| everything else | in full | in full |

*Journal `JVCurveDone` keeps the extra scalars beyond `metrics + label +
n_points` so the run index can name which light curve gave V_oc; `JVFinished`
is not in the plan's table and is reduced here for the reason stated.*

**`StepPhase(index, phase, k, of)`** is yielded by `run_transient_scan` as each
segment of a shot starts — `levels`, `light settle`, `acquire light`,
`dark levels` (absent with `dark_reference = "same"`, so `of` is 6 then and 7
otherwise), `dark settle`, `acquire dark`, `process` — for the live card's
"5 · acquire light" indicator. It is *ephemeral*: six or seven a shot say
where inside the shot the run is, which a log and a replay do not want, so it
is sent to the subscribers that are there with `seq: null` and never
journaled, ringed or numbered. A client that dedupes on `seq` passes it
through; a client that reconnects redraws from `StepStarted`/`StepDone`.

The journal stores enough to render the session log and the history queries,
never the traces — the recorders (HDF5 `bace-run/2` + legacy `.dat`, unchanged)
keep the data. `GET /runs/{id}/data` serves full arrays from an in-memory store
of the last 20 runs (older → 404 with the folder path, read the HDF5).

**The `since=` ring keeps the last 5000 *envelopes*, but only the traces of the
last `RING_TRACES_KEPT` (200) `StepDone` shots.** A ring full of thousand-point
lists is ~600 MB for one 100×51 scan; older `StepDone` frames in a replay carry
every scalar and the `verdict` (what a reconnecting client redraws Q(loop) from)
with the four traces `null` and `decimated[name] = {"omitted": true, "replay":
true}`, exactly as the journal line drops them. A WS client treats
`decimated[name].replay` as "redraw the loop curve, the trace is gone".

**`RunQueued` is a dataclass in `experiment.events`**
(`RunQueued(kind, module, tree, params, resolved, name, folder)`), sent down
the session's one event path like every other frame — one `seq` counter, one
journal line, one fan-out; the service assembles no frame by hand. It carries
both `params` and `resolved`: `params` is only what the operator chose — the
`edited` and carried-forward `last-used` layers, and never the V_oc parameter
— because it is what `last_used_params` hands back next session, and a journal
that fed every resolved default back as last-used would, after one run, show
the whole card as last-used and hide a recipe edit for twenty sessions.
`resolved` is every value the module ran with, for the record.

**Service-added per-shot verdict.** For every `StepDone` the asyncio side
computes `service.wire.shot_verdict(light.y, dark.y)` and attaches
`"verdict": {"rail_light": n, "rail_dark": n, "rail_run_light": n, "rail_run_dark": n, "shared_extreme": bool, "peak_light_a": A, "peak_dark_a": A, "level": "ok"|"warn", "text": …}`
to the wire `data` (not to the dataclass). The rule is the design's, corrected
2026-09-02 (the design pack 03-states §C, `docs/ui-rules.md`): **only a run of identical samples inside
one trace is evidence of the digitiser's rail** — `rail_run_*` ≥ 7 consecutive
samples on the trace's own extreme is `warn`, "the charge of this shot is
meaningless"; an extreme repeated more than 8 times anywhere (`rail_*`) is
`warn`, "the window is probably too small"; light and dark sharing an extreme
is **reported and judged as nothing** (the displacement spike dominates both,
so good data shares one). `peak_*_a` is the signed extreme of each *full*
trace, computed before decimation, because a 5:1 thinned trace can straddle
the true peak. The `ok` text is the "autorange pass 2 · no shared extreme · 0
rail samples" line on the live card. The autorange pass count comes from
`scope.last_autorange_passes` when the driver has it, read by
`rigs.Bench.shot_diagnostics()` right after the `StepDone` and merged into the
same `verdict` object. (`bench.checks.saturation_verdict`, which the bench
harness still uses, keeps the older shared-extreme rule; it is the harness's
to change.)

### Journal (`service/journal.py`)

- Path: `<out>/journal/<session_id>.jsonl`. One JSON object per line, the wire
  envelope (journal payload policy). Append-only; `flush()` after every line.
  Never rewritten. (Future failure recovery replays it; this round only writes.)
- First line: `{"type": "SessionStarted", "data": {"session_id", "mode": "sim"|"rig", "rig_toml", "run_toml", "out", "fingerprint": bench.checks.fingerprint(bace root), "python", "version", "fast", "startup_writes": [...]}}`.
  `startup_writes` lists the instrument writes the assembly made before the
  worker started (the scope's `default_setup`, the 1918-C's units and
  wavelength): not a run and not a by-hand action, so this is where they are
  on record.
- History queries (read this session's file and, newest first, every earlier
  `<out>/journal/*.jsonl`; stop at 20 files). Each file is parsed once and kept
  (this session's is folded line by line as it is written), so a query — and the
  Dry run, which asks `last_used_params` and `estimate_s` for each of a tree's
  module nodes — is a walk over objects, not a re-read of twenty files. Without
  this the canonical tree's Dry run took ~9 s on the event-loop thread after one
  long run and grew with every session.
  - `last_used_params(module) -> dict | None`: the `params` of the most recent
    `RunQueued` for that module whose run reached `done` or `stopped`. Reads
    every bench's files: what the operator typed on the sim console is still
    what they typed.
  - `run_index(session_id | "all") -> list[RunSummary]`. A pipeline's
    `kept`/`requested`/`outcome_text` are the sum over its module nodes' counts,
    the same sum the session's run record makes, so `GET /runs` and
    `GET /runs/{id}` agree; a pipeline's `outcome_text` counts module runs
    (`"3/18 module runs · aborted"`), not the last module's line. A jv_bace's
    line gives the V_oc *range* over its light curves when there were several
    (`"6 curves · V_oc 1.0269 … 1.0479 V"`), and each summary carries
    `voc_min`, `voc_max`, `light_curves`.
  - `run_record(run_id) -> dict | None`: one run's `summary()` plus its `tree`
    and `nodes` — every module node's `NodeDone` reduced to `{module, outcome,
    kept, requested, voc, voc_how, led_v, temperature_k, temperature_how,
    temperature_source, summary, folder,
    finished_at}` — from whichever session file holds it. What `GET /runs/{id}`
    answers for a run this process never held (§6): the pipeline tab's grey
    V_oc grid is the *previous* run's, and that record died with its process.
  - `settle_history() -> dict[float, list[float]]`: seconds between
    `NeedsOperator(what="temperature")` and the matching `OperatorResumed`,
    keyed by setpoint (rounded to 0.1 K); when the 331 is wired this becomes
    the automatic settle time and the query does not change. **Reads only files
    whose `SessionStarted` header has the same `mode`/`fast` as this session**
    (a file with no header is read): a `--sim --fast` session settles a
    "cryostat" in the time a click takes, and the lab PC's Dry run must not
    learn that.
  - `shot_time_s(module="bace") -> float | None`: median interval between
    consecutive `StepDone` of the last completed bace run — same bench filter as
    `settle_history`, for the same reason (a sim shot is a millisecond).
- `RunQueued` (`experiment.events.RunQueued`; `service/journal.py run_queued()`
  builds the same line for a test or a script), `RunStateChanged`,
  `NodeStarted/NodeDone`, `Verdict`, `NeedsOperator`, `OperatorResumed`,
  `BenchAction`, `TemperatureRead`, `PowerReading` are all journaled; so is
  every run event under the payload policy, `StepPhase` excepted.

---

## 4. `/bench` — read-back, chain, actions

`GET /bench` returns the cached snapshot (never touches VISA):

```json
{
 "session": {"id": "20260902_205200", "mode": "sim", "started": "…", "sample": {"sample": "s4", "material": "PTQ10:IT-4F", "pixel": "a"}, "errors": 0, "last_error": null},
 "state": "idle" | "preflight" | "running" | "paused" | "stopping",
 "run": {"run_id": "…", "state": "running", "node_path": "…", "module": "bace", "progress": {…},
         "eta": {"eta_s": 12400.0, "finish_at": …, "at": …, "node_path": "T=250K", "done": 4, "total": 9} | null,
         "finish_at": …, "pending": null} | null,
 "queue": ["20260902_205200-004"],
 "read_at": 1788390180.5 | null,
 "instruments": {
   "relay":   {"position": "amplifier"|"sourcemeter"|"unknown", "how": "readback"|"cached"|"unavailable"|"inferred"},
   "bias":    {"output": true|false|null, "polarity": "NORM"|"INV"|"?", "polarity_read": true|false, "arm_source": "EXT"|"IMM"|"?", "arm_slope": "POS"|"NEG"|"?", "high_v": 1.0423, "low_v": -4.0, "frequency_hz": 500.0},
   "smu":     {"output": …, "compliance": {"current_a": 0.01, "voltage_v": 2.0}, "ceiling": {"current_a": 0.05, "voltage_v": 5.0}},
   "shutter": {"open": true|false|null, "how": "readback"|"cached"|"inferred"},
   "led":     {"output": …, "polarity": "NORM"|"INV"|"?", "mode": "DC"|"PULSE"|"OFF"|"?", "high_v": 1.020, "low_v": 0.400, "frequency_hz": 500.0, "offset_v": 0.71 (real driver only)},
   "voc":     {"value": 1.0423, "led_v": 1.020, "from": {"run_id": "…", "node_path": "jv_bace", "ts": …, "how": "jv_bace"|"measure_dc"|"typed"}} | {"value": null},
   "power":   {"available": true, "watts": 1.407e-3, "trustworthy": true, "wavelength_nm": 530.0, "monitor": false},
   "temperature": {"wired": false|true, "kelvin": 294.8|null, "setpoint_k": null, "in_band": null, "source": "instrument"|"console"|"simulated"|"operator"|null,
                   "read_at": … (of the newest reading), "monitor": false, "reads": 0,
                   … when wired (a controller attached: this process on the bus, the 331 console answering, or the --sim stand-in):
                   "connected": true|false (whether the instrument answered the last poll), "ramping": …, "heater_range": 3, "status_text": "ok", "max_setpoint_k": 350.0,
                   … when a console is named: "console": "http://127.0.0.1:8331", and "reason" when a 331 was expected and not attached}
 },
 "inferred": [] | ["relay", "bias", "led", "shutter"],
 "chain": {"read_at": …, "ok": 3, "total": 4, "items": [
    {"key": "led_polarity",   "label": "33220A POL",      "value": "NORM", "expected": "INV", "level": "warn", "fix": "set-33220a-pol-inv",
     "text": "NORM makes the Sync's rising edge mean light ON, so extraction would happen during illumination"},
    {"key": "bias_arm",       "label": "81150A ARM",      "value": "EXT",  "expected": "EXT", "level": "ok",   "fix": "arm-81150a-ext"},
    {"key": "bias_arm_slope", "label": "SLOP",            "value": "POS",  "expected": "POS", "level": "ok",   "fix": "arm-81150a-ext"},
    {"key": "bias_polarity",  "label": "81150A POL",      "value": "INV",  "expected": "INV"|"NORM"|"leave", "level": "ok"|"warn"|"info", "fix": null}
 ]},
 "rig": {"path": "…/rig.toml", "loaded_at": …, "fingerprint": "d7daadac0cc1",
         "values": {"sense_resistor_ohm": 5.192, "pulse_amp": 4.0, "current_sign": -1.0, "trigger_offset_s": 0.0, "light_path_delay_ns": 502.0, "probe_attenuation": 1.0, "led_threshold_v": 1.0, "max_current_compliance_a": 0.05, "max_voltage_compliance_v": 5.0}},
 "verdicts": [ …the bench-level Verdict list from the last read-back… ]
}
```

**Read, not remembered.** On the real rig every `output`, the 81150A's
`polarity` and both generators' levels and frequency come from the instruments
(`read_state()`/`read_output()` on the drivers, the queries `tools/scan.py`
sent by hand: `:OUTP1?`, `:OUTP1:POL?`, `:VOLT1:HIGH?/LOW?`, `:FREQ1?`,
`:PULS:DEL1?`, `:FUNC1:PULS:WIDT?`; `:OUTP?`, `:OUTP:POL?`, `FUNC:SHAP?`,
`:VOLT:HIGH?/LOW?/OFFS?`, `:FREQ?`; the Keithley's `:OUTP?`), and each read
refreshes the driver's cached flag, so the relay interlock — which reads that
flag — sees a generator the LabVIEW VI left ON. The assembly reads each of
them once, before the first job. A query that fails is `null`/`?`, never a
default. `bias.polarity_read` says whether `polarity` was queried (the real
driver) or is the driver's memory (the simulator, which learns it when a run
configures the shape): a `?` from the former is a `warn` on the chain card
like any failed query, from the latter `info`.

**While a run holds the worker** the read-back is the one Start took, before
the module enabled anything, so the instruments the running step implies are
overlaid on it from the run's own events (`service/live.py`): a `bace` step's
`NodeStarted` puts the relay on the amplifier, its `RunStarted` turns the bias
output and the LED pulse on at the step's levels, `InstrumentState` carries the
arming and polarity the run read back, `StepStarted` the pulse levels of the
shot, each `StepPhase` the shutter, a jv's `InstrumentState({"shutter"})` the
shutter and the LED's DC state, `JVStarted`/`JVFinished` the SourceMeter, and
the module's `NodeDone` puts everything back off. Every overlaid instrument
carries `how: "inferred"` and is listed in `inferred`; `read_at` stays the
read-back's. Nothing inferred outlives the step that implied it. `state` is one
of `idle | preflight | running | paused | stopping`: a run still `queued` when
its preflight frame has not been applied reads as `preflight`, and a run in a
terminal state that the worker is still parking reads as `stopping`; `parked`
is a `RunStateChanged` transition every run passes through, not a resting bench
state, so it does not appear here.

`POST /bench/read` enqueues a read-back job (short); 409 while a run is active
(the chain is re-read automatically at every Start and the result attached to
the run). It **waits for the job by default** and answers 202 `{"job": "…",
…the /bench snapshot}` so a UI click sees the outcome; `?wait=false` returns the
bare 202 `{"job": "…"}` and the caller watches the stream. In `--sim` mode the
read-back reads the simulated instruments the same way. The temperature block
follows the newest `TemperatureRead` on the event path (the temperature
monitor, the pause's console poll, the operator's typed value) when it is newer
than the read-back, with `read_at` of that reading, `source` and, when the
monitor runs, `monitor: true` and its `reads` count. `session.errors`/
`session.last_error` surface what went wrong on the event path (a journal line
that could not be written) — each is also logged to `bace.service` as it
happens.

**Expected chain** comes from the recipe in force: `led_polarity` expected `INV`
always (03-states §B); `bias_arm` expected `EXT` when `external_trigger`;
`bias_polarity` expected `RunConfig.polarity_instruction()` (`leave` → level
`info`, value shown, nothing expected).

`POST /bench/actions/{name}` with an optional JSON body. Every action is a
worker job, journaled as `BenchAction(name, args, result, by="hand")`, followed
by a read-back. `result.before` carries the read-back's values of what the
action changes (`{"led.polarity": "NORM"}`; for `park`, the four outputs), so
the session log can say "33220A :OUTP:POL NORM → INV · by hand" from the
journal alone. Waits by default and answers 202 `{"job", "name", "result",
"read_at", "bench": …snapshot}`; `?wait=false` returns `{"job", "name"}`. 409
while a run is active except `park`, which is allowed always: while a run is
active it aborts the current run **and cancels every queued run** before parking
(one `stop_runs` step on the worker, so a run popped between "cancel the queue"
and "abort the current" cannot slip through), because a person who clicks park
wants the bench safe now, not after the runs behind the current one. A park
that has not started within the wait (the aborted run is inside a VISA call
that outlasts it) is **not cancelled**: the answer is 202 `{"job", "name",
"pending": true, "result": null, …}` and the park runs when the worker frees.
Any other action that timed out unstarted is cancelled and answered 504.
Actions:

| name | does | refuses when |
|---|---|---|
| `park` | `Rig.park()` — outputs off, shutter shut, router park | never |
| `set-33220a-pol-inv` / `set-33220a-pol-norm` | `led.set_polarity(True/False)` | LED output ON **as the instrument reports it** (the chain fix is made with the LED off; say so), or the output query unanswered |
| `arm-81150a-ext` | `bias.configure_trigger(external=True, positive_slope=True)` | bias output ON as the instrument reports it, or unanswered |
| `set-led-pulse` | `led.set_pulse(level, low, frequency, duty)` from the bace params in force + `enable_output(True)` | — |
| `set-led-dc` | `led.set_dc(level)` + enable | — |
| `led-off` / `bias-off` / `smu-off` | `disable_output()` | — |
| `shutter-open` / `shutter-shut` | `shutter.unblock()/shut()` | — |
| `relay-to-sourcemeter` / `relay-to-amplifier` | reads both sources' outputs back first (`read_output`, refreshing the flags the interlock reads), then `router._move` via the public context managers (enter `router.dc()`/`transient()` and exit immediately, so the interlock runs) | a source is live, or a source did not answer its output query → `crit` Verdict, nothing moved |
| `read-power` | one `PowerReading` from the meter | meter silent → `warn` |

Unknown action → 404. The service never performs any of these on its own.

---

## 5. `/modules` — the catalogue with provenance

`GET /modules` →

```json
{"modules": [
  {"name": "bace", "title": "bace", "status": "built"|"partial"|"not wired",
   "kind": "measurement"|"observer"|"utility",
   "estimate_s": 16.0, "estimate_text": "one shot ≈ 0.8 s · 20 loops",
   "needs": [{"code": "voc", "text": "none · run jv_bace first"}],
   "last": {"run_id": "…", "state": "done", "ts": …, "summary": "Q 8.520e-10 ± 1.8e-11 C · 20/20"} | null,
   "params": [ …ParamSet.as_wire()… ]},
  …]}
```

`PUT /modules/{m}/params` body `{"n_loops": 100, "axis_name": "delay_ns"}` →
sets the `edited` layer (coerced; 422 with the parameter name on error) and
returns the module entry. Body value `null` resets that parameter (drops the
edited value). `POST /modules/{m}/params/reset` drops the whole edited layer.

Layers, lowest first: dataclass defaults → `run.toml` (the file passed with
`--run`, mapped per module below) → last-used (journal) → edited (this session)
→ inherited/derived (pipeline context, never settable through the API). The
bench card applies the resolver's manual-run rule to `bace.voc` (§7, `_voc`):
when the session has a V_oc at the card's `led_v` (|Δ| ≤ 1e-9 V), nothing was
typed and `measure_dc` is off, the entry reads `{"value": 1.0423, "source":
"derived", "detail": "jv_bace jv_bace (this session)", "editable": false}` —
what the run will centre on — rather than `default · null` with an empty
`needs`. A typed value still wins, as it does for the run.

### The catalogue

| module | maps to | params (name → where it goes) |
|---|---|---|
| `jv` | `run_jv(rig, JVConfig(light_control="leave", …))` | `start_v stop_v step_v settle_s both_directions pixel_area_cm2` → JVConfig; `smu_current_compliance_a smu_voltage_compliance_v smu_nplc` → SourceMeterConfig (real rig: `Keithley2400(res, config=…)`; sim: ignored). **No illumination parameters at all** — it sweeps under whatever light it finds, touching neither shutter nor LED on the way in or out, unwinds only the SourceMeter, and labels the curve from a read-back (`experiment.jv.illumination_state`): `as found dark`, `as found 1.020 V`, or `as found unknown` with a warning when the bench cannot say. `JVCurveDone.dark` is `True`/`False`/**`None`**, and None is not False |
| `jv_bace` | `run_jv(rig, JVConfig(light_control="manage", dark=<dark>, led_levels_v=<levels>))` | all of `jv` + `led_start_v led_stop_v led_step_v` (or inherited `led_v` → one level), `led_settle_s`, `dark: bool = True` (include the dark curve), `led_low_v` (unused by run_jv, carried for the rail). This is the module that *owns* the light: it sweeps illumination as the measurement, and it is the V_oc source |
| `light` | `rigs.apply_led` / `rigs.apply_shutter` — the same functions the `set-led-*`, `led-off` and `shutter-*` actions call | `shutter` (`open`/`shut`/`leave`), `led_mode` (`dc`/`pulse`/`off`/`leave`), `led_v led_low_v pulse_frequency_hz duty_percent settle_s`. Exists because a bench action is not a pipeline step: without it a tree of `jv` steps could never be dark. `leave` on either half touches nothing, so the shutter can move without cycling a generator that is at its thermal steady state. **A run whose only module is `light` is `invalid`** (`light.undone-by-park`): every run ends parked, so it would hand the bench back unchanged — the manual form is the bench action |
| `bace` | `run_transient_scan(rig, ScanSpec, RunConfig, voc=…)` inside `router.transient()` with the LED pulsed at `led_v` | `axis_name axis_start axis_stop axis_step centre_on_voc` → Axis; `vpre vcoll delay_ns n_loops` → ScanSpec; `vpre_on_voc: bool = False` (group `pinned`: the pinned `vpre` is an *offset from the V_oc in scope* — the design's inherited "vpre = V_oc + 0.000 V" when `delay_ns` or `vcoll` is the axis, TDCF at V_oc; resolved in `build()` from the same V_oc source `centre_on_voc` uses, under the same coupling check, so the engine still gets an absolute prebias; invalid with the `vpre` axis, where `centre_on_voc` is the flag); every `RunConfig` field verbatim; `store_shots`; `led_v led_low_v` (33220A pulse levels; inherited inside an illumination loop); `voc` (derived from the V_oc source, or edited = typed by hand → warn); `measure_dc: bool = False` (measure V_oc/J_sc/J_sat on the Keithley under the LED first, like the intensity series; `v_sat`); `led_settle_s` (the least wait after DC → pulse), `led_settle_max_s: float = 60.0` s and `led_settle_tolerance: float = 0.02` (group `illumination`: the power meter behind the open shutter is polled every 0.5 s until three readings agree within the tolerance, giving up with a warning at the maximum; see the README's "Light"); `smu_*` as above |
| `power` | `PowerReading` from the meter; `Read` is a worker job, `Monitor` is an observer | `wavelength_nm samples` |
| `temperature` | `service.temperature.settle`: through the 331 when `Rig.temperature` holds a controller (setpoint written, band held for `hold_s`), else `NeedsOperator` + wait; status `partial` | `setpoint_k tolerance_k hold_s timeout_s` |
| `park` | `Rig.park()` | — |
| `wait` | sleep | `seconds` |
| `note` | journal entry only | `text` |

`run.toml` mapping (`params.toml_layer`): `[axis]` → `axis_*`/`centre_on_voc`;
`[pinned]` → `vpre vcoll delay_ns`; `[acquisition]` → RunConfig fields,
`n_loops`, `store_shots`; `[illumination] level_v/low_level_v/v_sat` →
`led_v/led_low_v/v_sat`; `[sourcemeter]` → `smu_*`; `[sample]` → the session's
metadata (not a module param). jv modules take their defaults from `JVConfig`
(run.toml has no jv table; add an optional `[jv]` table to `config.load_run`
only if trivial — otherwise defaults + last-used is enough).

`ParamSpec.doc` comes from `params.field_docs` on the dataclass — one sentence,
naming the instrument and what the parameter does to it — and `doc_full` is the
whole docstring for the field's expandable help (`ui-rules` §1). Both are on the
wire; `doc_full` is empty when `doc` is the whole of it. Every one of the 69
distinct parameters carries a `doc` as of 2026-09-03. Groups:
`axis`, `pinned`, `acquisition`, `processing`, `timing`, `trigger`, `output`,
`illumination`, `sourcemeter`, `led` — the UI shows the first six fields of the
bace card and folds "17 more · run.toml"; the service just labels groups.

`Catalogue.build(module, params: dict, ctx: RunContext, rig) -> Iterator[Event]`
returns the generator (already wrapped in the recorder for that module and in
the router context) — this is the single place a module's parameters become
dataclasses. `RunContext` carries: `run_id`, `node_path`, `out_folder`,
`metadata: RunMetadata` (sample/material/pixel/operator from `[sample]`,
`temperature_k` from context or typed — with `temperature_how` and
`temperature_source` saying which, see §7 — `led_drive_v`, `voc_v`), `led_v`
(inherited or None), `voc: VocSource | None` (`value, led_v, run_id, node_path, how`),
`sleep` (a no-op under `--fast`), `abort: Callable[[], bool]`, `resolved`
(seed for `RunRecorder.resolved`: `led_output_polarity`, `bias_arm_source`,
`bias_arm_slope`, `led_levels_v`, `led_frequency_hz` from the last read-back).

Manual `bace` semantics (the bench card). When `measure_dc` is set the builder
measures V_oc first, the way the intensity series does: 33220A to **DC** at the
level (`LedDrive.dc_settings`), the **shutter open** (`Rig.park()` shut it before
the job — an LED that is on is not light at the sample), `led_settle_s` for the
device to reach its light steady state, `smu.measure_dc` inside `router.dc()`,
then the shutter shut. Only then does it switch to pulse mode at
`led_v/led_low_v` at `pulse_frequency_hz/duty_percent`, enable, read the 33220A
back (refusing OFF, DC, another frequency, or `OUTP:SYNC?` = 0), open the
shutter and wait for the power meter to read stable (`led_settle_s`,
`led_settle_max_s`, `led_settle_tolerance`; the fixed `led_settle_s` alone
without a meter), and run the transient inside `router.transient()`. A V_oc
read under the pulse (a slow Keithley integrates over the on and off phases)
or with the shutter shut (the dark V_oc) would centre the axis on nobody's
V_oc, and the coupling check — which compares drive *levels* — cannot tell;
the simulator's `measure_dc` now returns the dark 0 V when the shutter is shut,
so the test catches the class of mistake. After the scan the shutter is shut
and the LED is left pulsing: no module switches the generator off (operator
instruction 2026-09-02; the shutter is the light switch).

V_oc source resolution for a **manual** run (`build()`): `voc` param typed →
`how="typed"`, else the source in scope (`ctx.voc` — the session's most recent
light J-V curve at `led_v`, |Δ| ≤ 1e-9 V), else `measure_dc`, else invalid when
`centre_on_voc`. In a **pipeline** the resolver (`pipeline._voc`, §7) chooses one
first — a jv_bace in scope at this level, then `measure_dc`, then a typed value,
then the session's — and hands `build()` a schedule where a chosen measurement
has cleared `voc` and a chosen typed value left `ctx.voc` unset, so the two
never both arrive; the `build()` order governs only a direct call with both set,
where the number just typed wins. When `measure_dc` and a typed `voc` are set on
the *same* node the run measures and the typed number never runs, so `voc.typed`
warns that it was ignored rather than letting it vanish from the schedule.

---

## 6. `/runs`

- `POST /runs` body `{"module": "bace", "params": {…overrides on top of the module's resolved params…}, "name": "…"}` → 202 `{"run_id", "state": "queued"|"preflight", "position": 0}`.
  Internally: build a one-node tree, validate (§7), refuse with 422
  `{"checks": […]}` on `invalid`/`crit`, else queue.
- `POST /runs/{id}/stop` body `{"mode": "after_shot"|"abort"}` → 202 `{"run_id",
  "state", "mode"}`; the `state` is the new one — `stopping` for a running or
  paused run (reported at once, so the record, `/bench` and the stream all say
  `stopping` from the moment the stop was accepted), `cancelled` for a queued
  run. 404 unknown; 409 if already terminal.
- `POST /runs/{id}/resume` body `{"note": "set to 250.0 K by hand", "temperature_k": 250.1}`
  → 202; 409 if not paused. `temperature_k` becomes the context temperature
  for the subtree (folder names, metadata) and is journaled in `OperatorResumed`.
- `GET /runs?session=<id>|all` → `[RunSummary]`: `run_id, kind ("manual"|"pipeline"), name, module|tree_summary, state, queued_at, started_at, finished_at, kept, requested, outcome_text, folder, node_count, voc_min, voc_max, light_curves`.
- `GET /runs/{id}` → the full record: tree, resolved schedule, params with
  provenance as executed per node, per-node outcomes (`NodeDone` details),
  verdicts (one entry per `(code, node_path)` — the Start re-read replaces the
  submit-time copy, so a "N warn" count off the record is honest), chain
  read-back at start, folders written, error if failed, `progress` (the
  module's own), `eta` (the latest loop `Progress` with the executor's
  re-derived ETA: `{eta_s, finish_at, at, node_path, done, total}`; null for a
  manual run), and `cost` whose `finish_at` follows that ETA once one has been
  measured (`finish_source: "measured"`, `finish_at_submit` kept beside it).
  `from: "session"`. For a run this process never held — last week's — the
  answer is the journal's record (§3 `run_record`): the summary fields plus
  `tree` and `nodes`, `from: "journal"`, `data_in_memory: false`; 404 when no
  journal file knows the id.
- `GET /runs/{id}/data?node=<node_path>` → for `bace`:
  `{"axis": {…}, "values": […], "q_mean": […], "q_std": […], "q_all": [[…]], "time_s": […], "light": [[…]], "dark": [[…]], "photo": [[…]], "last_shot": {"light": […], "dark": […], "photo": […], "cumulative_q": […], "t0_int_record_s": …}, "kept": 12, "requested": 20}`;
  for `jv_*`: `{"curves": [{label, dark, led_level_v, direction, voltage, current, density, metrics}]}`;
  for a pipeline run without `node` → 400 listing the nodes.
  Full precision, no decimation. The `cumulative_q` array is the running
  integral of the last photocurrent from `t0_int` (the LabVIEW "Integrated
  PhotoCurrent" plot; computed with `numpy.cumsum(photo[i0:]) * dt`).
- `GET /events` WebSocket. Query `since=<seq>` replays from the in-memory ring
  (last 5000 envelopes, traces of the last 200 shots — see §3) then streams live.
  First frame: `{"seq": null, "type": "Hello", "data": {"session", "seq":
  last_seq, "bench": <GET /bench>}}`. The **envelope** `seq` is `null`, not
  `last_seq`: the replay that follows carries lower seqs, and a Hello stamped
  with the newest seq would make a client that dedupes on `seq > last seen` drop
  the whole replay; `data.seq` is the last seq the client has. A client that
  falls behind (send queue > 1000) is dropped with a `Notice` frame (`seq null`,
  carrying `data.since` to reconnect from) followed by socket close 1008. (An
  HTTP `GET /events?since=<seq>` beside the WebSocket returns the same ring
  frames as `{"seq": last_seq, "events": […]}` for a client without a socket.)

---

## 7. Pipelines

### Tree schema (what the UI posts)

```json
{"kind": "loop", "loop": "temperature", "label": "T",
 "values_k": [295, 290, 280, 270, 260, 250, 240, 230, 220], "tolerance_k": 0.2, "hold_s": 60, "timeout_s": 1800,
 "children": [
   {"kind": "loop", "loop": "illumination", "led_start_v": 1.010, "led_stop_v": 1.030, "led_step_v": 0.005,
    "led_low_v": 0.4, "led_settle_s": 2.0,
    "children": [
      {"kind": "module", "module": "jv_bace", "params": {}},
      {"kind": "module", "module": "bace", "params": {"n_loops": 100}}
    ]}
 ]}
```

- Loops: `temperature` (`values_k` list, or `start_k/stop_k/step_k`),
  `illumination` (`levels_v` list, or `led_start_v/led_stop_v/led_step_v`;
  `led_low_v`; `led_settle_s`), `repeat` (`count`). A loop needs ≥ 1 child.
- Module node `params` are overrides on the module's resolved ParamSet
  ("as on the bench · only what differs is typed here"). A module inside an
  illumination loop **may not** set `led_v`/`led_start_v` etc. — the loop owns it.
- The root may be a module node (a manual run) or a loop.
- Optional `"name"` at the root → the pipeline's folder stem.

### Node paths

`temperature` iteration → `T=250K` (`:g` of the setpoint + `K`);
`illumination` → `led=1.020V` (3 decimals); `repeat` → `rep=2` (1-based);
module leaf → its module name; duplicate sibling modules → `bace#2`. Joined
with `/`. A manual run's single node has path `"bace"`.

### Resolution → schedule

`pipeline.resolve(tree, catalogue, bench_snapshot, history) -> Schedule`:
a flat, ordered list of `Step`s, each `{node_path, kind: "loop-enter"|"module"|"loop-exit", loop, value, module, params (resolved ParamValues incl. inherited), needs_operator: bool, relay: "dc"|"transient"|None, estimate_s}` plus the counters
`{"temperatures": 9, "levels": 5, "modules": 90, "shots": 9*5*100}`.
The schedule is what `Dry run` shows ("what it will do, in order") and what the
executor runs; the executor must never re-derive structure.

### The three bindings (plan, "唯一的新逻辑")

1. **illumination loop → `led_v` of every module inside it.** Set as
   `Source.INHERITED` (detail `"illumination loop"`) on `jv_bace.led_levels_v = [led_v]`
   and `bace.led_v`. The loop also owns `led_low_v`.
2. **`jv_bace` → V_oc → `bace` at the same `led_v`.** Within one illumination
   iteration, the latest light curve's `metrics.voc` at that level becomes
   `bace.voc` (`Source.DERIVED`, detail `"jv_bace <node_path>"`). `measure_dc`
   on the `bace` node is the alternative source. A `bace` with
   `centre_on_voc` and no source in scope is **invalid** at validation.
   Outside any loop (manual), the session's last `jv_bace` at the same level
   qualifies (§5).
3. **Relay transitions at every `jv_* ↔ bace` boundary**: `run_jv` already
   runs inside `router.dc()`; the executor runs `bace` inside `router.transient()`.
   The interlock (`Router` refuses while a source is live) is the guard; the
   schedule marks each transition so the UI can draw "relay 2400 → amplifier".

### Temperature (the 331, or the pause)

One helper, `service.temperature.settle`, serves the temperature loop and the
`temperature` module; which path it takes is decided by `Rig.temperature`.

**Who owns the instrument** (changed 2026-09-03). The 331 answers only the
last query it received, so exactly one owner may exist — but that owner is
now normally *this process*: `Bench.build_real` opens `[temperature] address`
itself through `drivers.lakeshore331.controller`, and the one-owner rule is
kept by a lock (`controller._LOCK`) rather than by a process boundary. Naming
`[temperature] console` is the escape hatch for a bench where the 331 console
is running and holds the bus; clearing *both* says there is no cryostat here,
and then nothing is opened and nothing is reported unavailable. `--sim` is
unchanged and deliberately does not follow `address`: any non-empty `console`
attaches the simulator's stand-in, and the default (none) is the
operator-pause path, because that is the one a UI has to handle well. Wiring
the 331 changed no route, no tree field and no event type (plan, P3: "API
不变").

**With a controller attached** the node settles on its own. It is read first
— the first poll, at zero on the poll clock, is the cryostat as found and
goes out as a `TemperatureRead` like every other; a setpoint is written to an
instrument that answers, never into a controller with nothing behind it —
then the setpoint is written (once), the controller is polled every 5 s
through `RunContext.sleep` (so `--fast` costs polls, not wall time), each
usable reading is yielded as
`TemperatureRead(source="instrument"|"console"|"simulated")`, and the node is
done once
the reading has stayed inside `tolerance_k` of the setpoint, with no ramp
still walking it (RAMPST?), for `hold_s` — the dwell starts when the band is
entered. Then `Verdict(ok, "temperature.settled", "250 K reached in 27 min,
held 60 s", node_path, data={setpoint_k, kelvin, settle_s, hold_s, held_s,
polls, source})`, and the **last measured kelvin** (not the setpoint) becomes
the subtree's `temperature_k` — folder names and metadata. A `temperature`
*module* binds the same way for the nodes after it, and for the **rest of the
run**, not the rest of one loop iteration: the cryostat stays where it was put
until another temperature node moves it, so a `bace` beside it — or at the
next level of an enclosing illumination loop — is labelled with what the
cryostat is at, not with the session's number. A temperature loop still wins
for its own subtree; each iteration writes its setpoint again on the way in.

**Where the number came from travels with it.** The folder name is the
2026-08-07 archive convention and does not change: `290K` in a name is the
same string however the 290 was arrived at. So every `RunMetadata`, every
`/metadata` group in HDF5 and every journalled node carries two more fields —

| `temperature_how` | `temperature_source` | means |
|---|---|---|
| `typed` | `""` | `[sample]` in the recipe, or the console's metadata field. Nobody read an instrument. |
| `setpoint` | `""` | A temperature loop asked for it and the settle never got one reading. **What was requested, not what was reached.** |
| `settled` | `instrument` \| `console` \| `simulated` | The controller held it inside the band. `instrument` is this process on the bus, `console` the 331 console asked over HTTP; `simulated` means the stand-in, so a `--sim` file cannot be mistaken for a measured one. |
| `operator` | `operator` | A person typed the number at the pause. |
| `operator` | `instrument` \| `console` \| `simulated` | A person ended the pause without typing one; the number is the last reading polled while they decided. |

`how` alone is not enough — the last two rows share it — so both are stored
and both are rendered. A field absent altogether is a file written before
these existed; the HDF5 schema stays `bace-run/2`, because two attributes on
a group every reader already opens is an addition, not a change.

The same split — the name is a convenience, the file is the record — applies
to `[sample] comment`. The folder name carries a slug of it
(`bace.storage.naming.slug`: whitespace and `_` become `-`, the characters a
Windows path segment may not hold are dropped, 64 characters, anything merely
non-ASCII is kept), while the metadata keeps the sentence verbatim. `runs/`
holds a directory named `290K_1000mVLED_offsetcorr_LabVIEW panel replica -
combination 4 - shutter only dark_20260902_012223` from 2026-09-01 to say
why: spaces in a path, and a claim that was overturned the next day and can
no longer be corrected without renaming the folder.
Each node is marked in the controller's audit trail at the setpoint write
and at the settle, best effort — the console's own log through `POST
/api/note` when a console owns the instrument, the session journal when this
process does (the console kept a separate file because it was the only
program on the instrument; here the journal is where an operator looks).

The 350 K ceiling and the front-panel settings hold whoever owns the bus, and
each failure is named for what it is:

- **the heater watchdog** cuts the heater after
  `Limits.max_consecutive_faults` consecutive faults, and the count resets on
  the first clean one. A fault is a bad control-sensor status (`RDGST?`) **or
  a heater fault** (`HTRST?`): an open or shorted load while the sensor reads
  well would otherwise reset the count forever. Cutting is `RANGE 0`, plus --
  on a loop-2 cryostat, where `RANGE` does not reach the analog output --
  `MOUT 0` then `CMODE open loop`, in that order. It is fed from every
  `read()` (the monitor, the settle) **and from the worker's own sleeps
  inside a run** (`rigs.Bench._feed_watchdog`, at most every
  `WATCHDOG_POLL_S`), because the monitor cannot read while a run holds the
  bus and a fault beginning after a temperature settles must not wait hours
  to be seen. `DirectTemperatureController.heater_cut` holds the reason once
  it has fired;
- a setpoint above the 350 K ceiling (`[temperature] max_setpoint_k`, or the
  console's own limit when a console owns the bus) is **refused**, never
  clamped — `Verdict(crit, "temperature.refused", <the controller's own
  sentence>, data={…, "error": <the driver's full text>, "status": 403})`
  then `NeedsOperator(what="temperature refused", detail={…, "error"})`. The
  direct driver raises the same `TemperatureError(refused=True, status=403)`
  the console's HTTP 403 produced, so this path is one path. A refused
  setpoint never names the subtree: a resume with no `temperature_k` typed
  takes the last reading;
- the heater range, ramp rate and PID are whatever the front panel holds. A
  heater range of 0 while the setpoint is above the reading is said at once
  — `Verdict(warn, "temperature.heater-off", …)`, once per node — and the
  settle goes on toward its timeout, so the operator raises the range on the
  instrument instead of finding out half an hour later; nothing is changed
  from here, and a cool-down with the heater off is not a warning;
- `timeout_s` on the poll clock without reaching the band, three consecutive
  readings with `connected` false (`reason = "silent"`, before or after the
  write), or the bus itself not answering three polls or the setpoint write
  (`reason = "unreachable"`: fix the instrument or the cable — or start the
  console, if rig.toml names one — do not reconsider the setpoint) gives
  `Verdict(warn, "temperature.timeout", …,
  data={…, "reason", "written", "error"})` then
  `NeedsOperator(what="temperature timeout", detail={…, "kelvin",
  "elapsed_s", "reason", "written", "error"})` — the run does not proceed to
  measure at a temperature it did not reach; the operator resumes (accepting
  the reading, optionally typing `temperature_k`) or stops. A write the
  console did not answer in time is read back once before that verdict: when
  the state already shows the setpoint in force (a slow bus, not a dead
  console) a `Notice(warning)` says so and the settle goes on.

A stop or abort is honoured at every poll.

**Without a controller** the node pauses as before:
`NeedsOperator(what="temperature", node_path="T=250K", detail={"setpoint_k": 250.0, "tolerance_k": 0.2, "hold_s": 60, "timeout_s": 1800, "index": 4, "count": 9})`,
run state `paused`, and waits. `POST /runs/{id}/resume` wakes it; the resume
detail (`temperature_k`, `note`) is journaled as `OperatorResumed` and
`temperature_k` flows into the subtree's metadata. While any pause lasts
(this one, or the timeout/refusal ones above, on the loop path and on the
`temperature` module's alike) an attached controller is polled every 5 s and
each `TemperatureRead` goes out **live** — through an out-of-band `emit` hook
the session binds to its event path, so the reading reaches the journal and
the socket while the run is still paused, which is the point (a reading
delivered after the operator decided the cryostat had settled is history).
Without a hook (a script, a test) the readings are yielded after the resume
instead, the last `MAX_POLLED_READS` of them. `hold_s` is honoured after
resume (a `wait`). The settle time goes to the journal for the cost model:
resume − pause on this path, the same plus the console's `elapsed_s` for a
`temperature timeout` pause, `data.settle_s` of the `temperature.settled`
verdict on the automatic one; a refused setpoint's pause teaches nothing.

The schedule's `needs_operator` on a temperature step, and the
`temperature.not-wired` check, come from the bench snapshot the tree was
validated against: `ok` only when the read-back showed the controller
attached and `connected` true; `warn` (a pause) when no controller is
attached, when the console named was silent at Start, when it answered at
Start and not at the read-back (`error` in the block: start the console),
when it is up with the instrument silent behind it, and when a controller is
attached but not read back yet (`connected` null in the bench stub before
the first read-back). The run decides again at the node from a fresh read.

### The check catalogue (`pipeline.validate`)

Stable ids; each result is a `Verdict(level, code, text, node_path)`.
`invalid` and `crit` block Start; `warn`/`info`/`ok` do not. The service never
fixes anything: a `fix` field names a bench action the operator may click.

| id | level | condition |
|---|---|---|
| `tree.shape` | invalid | unknown kind/loop/module, loop without children, malformed values |
| `tree.owned-param` | invalid | a module inside an illumination loop sets `led_v`/`led_*_v` |
| `light.undone-by-park` | invalid | the run's only module is `light`: every run ends parked, so it would set a light and hand it straight back. Names the bench actions, which do not go through the worker |
| `voc.source` | invalid | `bace.centre_on_voc` or `bace.vpre_on_voc` with no V_oc source in scope (`data.needed_by` names the flag) |
| `voc.coupling` | invalid | V_oc source's `led_v` ≠ the bace's `led_v` (`assert_axis_centre`) |
| `led.levels` | invalid | `LedDrive` refuses (low ≥ threshold, level < threshold, level ≤ low) |
| `axis.geometry` | invalid | `Axis`/`ScanSpec` refuse (step, centre_on_voc on a non-vpre axis, n_loops < 1), `vpre_on_voc` with the `vpre` axis, or `delay_ns + trigger_offset < 0` |
| `bench.instrument` | invalid | a module step's instrument is in the read-back's `unavailable` (`Catalogue.needs` minus the V_oc and power advisories; the `power` module needs its meter). `info` before the first read-back. Without it a run on an unplugged Keithley was accepted, queued and failed at preflight |
| `smu.ceiling` | crit | compliance above the bench ceiling (`config.check_smu_limits`) |
| `relay.interlock` | crit | a tree with both a `jv_*` and a `bace` and no `Router`; or at Start a source reports its output live |
| `bench.live-at-start` | crit | bias or SMU output ON at Start (read-back) |
| `voc.typed` | warn | `bace.voc` typed by hand, not measured this session; or a typed `voc` ignored because `measure_dc` on the same node measures it |
| `chain.led-polarity` | warn | 33220A reads NORM, or `?` (an unreadable polarity on a transient is as unproven as NORM; fix `set-33220a-pol-inv`) — `info` when no transient in the tree uses the Sync edge (a J-V-only tree); the `/bench` card always warns, because it describes the bench, not a tree |
| `chain.bias-arm` | warn | 81150A not EXT / not POS when the recipe wants external arming (fix `arm-81150a-ext`) |
| `chain.bias-polarity` | warn | 81150A `:OUTP1:POL` ≠ the recipe's instruction (info when `leave`, or when unread `?` — the run writes it at Start, as the `/bench` card reports) |
| `temperature.not-wired` | warn / ok | a temperature loop: `ok` when the read-back shows the 331 console attached and its instrument answering ("settles automatically, ±tol, hold, timeout"), `warn` when no controller is attached (pauses at each T for a manual set; names the console when one is named and silent) or when the console is up with the instrument silent behind it (pauses too). The schedule's `needs_operator` on temperature steps follows the same rule; `True` before the first read-back |
| `temperature.inside-illumination` | warn | temperature loop nested in an illumination loop; text states the cost |
| `trigger.auto` | info | `trigger_sweep = AUTO` (a missing trigger looks like data) |
| `power.console` | warn | `:8918` not answering → intensity will be NaN |
| `intensity.factor` | info | no calibration factor (the original's "Factor" is not recoverable) → intensity recorded in watts at the meter, not irradiance at the sample |
| `cost.long` | warn | estimated total > 8 h, or `store_shots` > 2 GB |
| `chain.stale` | info | chain read-back older than 10 min (re-read at Start anyway) |

`Verdict.data` carries the numbers the text quotes.

### Cost model (`pipeline.estimate`)

```
t_module(bace) = n_loops × n_steps × t_shot           t_shot = journal.shot_time_s() or 0.8 s
t_module(jv_*) = n_curves × (n_points × settle_s + n_points × 0.05 + led_settle_s)
t_illumination(level) = led_settle_s + Σ children
t_temperature(T) = settle(T) + hold_s + Σ children    settle(T) = median(journal.settle_history()[T]) or None
                                                       (settle_history: operator pauses *and* the console's temperature.settled verdicts)
total = Σ …
```

Output: `{"total_s": …, "measuring_s": …, "waiting_s": … | null, "per_temperature": [{"setpoint_k": 250, "settle_s": 2220 | null, "measure_s": 438}], "lower_bound": true when any settle is None, "finish_at": ts | null, "t_shot_s": 0.8, "t_shot_source": "journal"|"default"}`.
Never invent a settle time: `null` renders as "—" (the design's table shows
"—" for the first temperature).

### Endpoints

- `POST /pipelines/validate` body `{"tree": …}` → 200 `{"valid": bool, "checks": [Verdict…], "schedule": [Step…], "counters": {…}, "cost": {…}, "node_paths": […], "folder": "<out>/<stem>", "folder_pattern": "<out>/<stem>_YYYYMMDD_HHMMSS"}`. This is the **Dry run** button: touches nothing. `folder` is the stem; the run's folder is `folder_pattern` with the stamp `submit` takes, which the Dry run cannot know — "writes 90 folders under `20260901_Txill_YYYYMMDD_HHMMSS/`".
- `POST /pipelines` body `{"tree": …, "name": "…"}` → validates (including a fresh chain read-back job when idle), 422 with the checks when `invalid`/`crit`, else 202 `{"run_id", "state", "checks", "cost", "folder"}` (the stamped folder).
- `GET /pipelines/last` → the last validated tree in this session (so the UI can reopen it). Saving recipes to disk is `POST /pipelines/save {"tree", "name"}` → `<out>/recipes/<name>.json`; `GET /pipelines/saved` lists them. Small, optional.

### Executor (`executor.run_pipeline`)

A synchronous generator run as one worker job. For each schedule step:
`loop-enter` → `NodeStarted`, apply the binding (illumination: `led.set_pulse`
happens inside the module builder, not here; the loop only sets the context),
temperature: the settle through the console, or `NeedsOperator` + wait + `hold_s` (see "Temperature" above); `Progress(node_path=<loop path>, done=i, total=n, eta_s=…)`.
**`eta_s` is re-derived from what this run has measured** (the plan's "ETA
随实测重算"): the sum of what is left in the schedule, where a module costs the
median duration of the same module in this run once one has completed (its
`estimate_s` until then) and a temperature costs the median settle the
operator has taken *in this run* once one has been measured (the journal's
median from the schedule until then, or nothing) plus `hold_s`, and an
illumination level its settle. Every loop enter and exit carries it, so after
the first temperature settled in two hours instead of the journal's twenty
minutes the next `Progress` says so; the session copies the latest into the
record's `eta` and `cost.finish_at` (§6). A number that no settle has informed
yet is a floor; the cost's `lower_bound` says so.
`module` → `NodeStarted`, `catalogue.build(...)`, forward every event unchanged
(the worker stamps the envelope with this step's `node_path`), capture what the
next binding needs (V_oc from `JVCurveDone` light curves; `RunFinished` for
the summary), `NodeDone(outcome, detail={"module", "kept", "requested", "folder",
"folders", "summary"})`. A module that reported its own end (`RunFinished`/
`JVFinished`) is `ok` even if an `after_shot` was requested during its last shot
— a complete node is not `stopped`, and the stop takes effect at the next node.
On `RunAborted` from a module: stop the tree (`NodeDone(outcome="stopped")`
for the open nodes), then unwind. On an exception: `RunFailed(error, where=node_path)`,
unwind, re-raise nothing (the worker marks `failed`). `finally`: `Rig.park()`.

An **abort** closes the generator before it can emit `NodeDone`, so the *session*
synthesizes one per node the worker saw open, with the folder its recorder had
opened (`RunContext.folders`) and the shots it took, on the same event path as
the frames — so `GET /runs/{id}` names the folder and the counts of a run whose
files are on disk, rather than reporting nothing. The record's terminal state
and `parked` are likewise applied on the event path (as the `RunStateChanged`
frame is ingested), after those synthesized `NodeDone`s, so a client polling on
`parked` never reads a run as finished-with-no-data a beat before its counts
land.

`RunContext.sleep` is abort-aware on the real bench (`Bench.sleeper`): a settle,
a temperature `hold_s` (default 60 s) or a `wait` module sleeps in 0.2 s slices
and returns early once **abort** is requested, so the abort button is not dead
for the length of a hold. An `after_shot` does *not* cut a sleep short — the shot
in flight is kept, settle and all. `--fast` makes every sleep a no-op.

Folders: a pipeline run creates `<out>/<stem>_<stamp>/` where `stem` is the
run name or `pipeline`; each module run records into
`<that folder>/<RunMetadata.folder_name()>` (bace: `RunRecorder`; jv: `JVRecorder`;
the folder name already carries temperature, LED level, V_oc). A manual run
records directly under `<out>/` as `tools/scan.py` does. `GET /runs/{id}`
lists every folder written.

---

## 8. The observers (`monitors.py`)

`POST /monitors/power {"interval_s": 1.0}` starts a thread that reads the
meter every interval and emits `PowerReading` (run_id null, `node_path=""`);
`DELETE /monitors/power` stops it; `GET /monitors` lists. **The meter is
never on the bus** — USB when this process owns it, HTTP when the meter's
console does — so it runs right through a bace scan (this is R3·2),
serialised against the worker's own reads by `rigs._METER_LOCK`. One monitor
of each kind at most. If the meter stops answering, emit a `Verdict(warn,
"power.console")` once and keep trying.

`POST /monitors/temperature {"interval_s": 5.0}` is the same shape for the
331; 422 only when the bench has no 331 at all (neither `[temperature]
address` nor `console`). Every reading is a
`TemperatureRead(source="instrument"|"console"|"simulated")` on the stream
and in the journal, the `/bench` temperature block follows it (`kelvin`,
`read_at`, `monitor: true`, `reads`), and a silent instrument is one
`Verdict(warn, "temperature.console")`. This is the temperature card's
"294.8 K · 331 reads" while a scan runs. `DELETE /monitors/temperature` stops it.

**The one exception to "observers never touch the bus."** When this process
owns the 331's GPIB session its monitor *is* on GPIB0, so it takes
`RunWorker.bus` -- the lock the worker holds for the whole of a job --
acquires it **without blocking**, and reads only while holding it. A tick
that cannot take it is skipped and counted in `skipped` on `GET /monitors`;
a skip is not a failure. Holding the lock rather than testing `worker.idle`
is the point: the boolean was true one instant and stale the next, so a
multi-query read could straddle the start of a job. With a console named the
331 is HTTP again and no lock is passed.

**What that costs.** A pipeline run holds the bus for its whole length, so
during a long subtree the temperature monitor emits nothing and the card's
reading goes stale. `skipped` is how a UI says *why* rather than showing an
old number as if it were current — **render it**. Readings still arrive from
the worker where it is safe to take them: while a temperature node settles,
and while a run is paused for the operator.

The **safety** half of that gap is closed, and separately: the heater
watchdog rides on reads, so suppressing reads for hours would have suppressed
it too. The worker feeds it from its own sleeps (§7), which is a fault check
and not a reading — no `TemperatureRead` is emitted from there, because a
number taken mid-run is not the cryostat the shot around it was measured at.

---

## 9. CLI (`__main__.py`)

```
python -m bace.service --sim --fast --port 8900          # develop off the bench
py -3 -m bace.service --rig rig.toml --run run.toml      # the lab PC
```

- `--sim` builds the rig from `drivers.simulated.make_bench()` (a `SimulatedRig`:
  bias, scope, shutter, led, smu, power, router); `--fast` makes every `sleep`
  a no-op inside the service's generators (`RunContext.sleep`) so a 20-loop scan
  takes seconds. Without `--sim` the real rig is assembled from `rig.toml` the
  way `tools/scan.py` and `bench.checks.stage_measure` do it (pyvisa
  ResourceManager; `Infiniium(..., current_sign=rig.current_sign)`;
  `Agilent81150`; `Agilent33220A`; `Keithley2400(config=SourceMeterConfig from run.toml)`;
  shutter and relay through `bench.checks._dio_backend` (direct DELIB or the
  win32bridge helper), the relay wrapped in an adapter exposing `set(channel, value)`
  for `BiasRouter`; the 1918-C opened here, or `ConsolePowerMeter` when
  `[power_meter] console` names one; the 331 likewise). A missing
  instrument is not fatal: the bench snapshot says `unavailable` for it, the
  modules that need it report `needs`, and `bench.instrument` refuses a run on
  it at validate. A missing VISA runtime — `pyvisa` not installed, or installed
  with neither NI-VISA nor pyvisa-py behind it — is the same case four times
  over: scope, bias, led and smu are `unavailable` with the reason and the fix
  (`pip install -e .[rig]`), the DIO lines and the consoles are still tried,
  and the service comes up. A `relay_module_nr` of 0 is refused by
  `config.load_rig` (module 0 is the shutter), and a `RigConfig` built by hand
  that still says so lands as an unavailable relay, not a traceback.
- Extras: the desk needs `service`; the lab PC needs `lab` (`rig` + `service`),
  because `Bench.build_real` imports `pyvisa`. `main()` imports `uvicorn` and
  `app` **before** it builds the session, so a Python without the extra is told
  which extra and nothing has touched the bench or written a journal header.
- `--host 127.0.0.1` fixed unless overridden **with another loopback address**
  (`127.0.0.0/8`, `::1`, `localhost`); anything else is refused with exit 2. No
  auth (plan: 不做鉴权), which is acceptable only where nobody but this machine
  can connect.
- `scripts\Run Service.bat` / `Run Service (sim).bat` `cd` to the repo root, as
  every other script does, so `--out runs`, `rig.toml` and `run.toml` resolve
  there; the banner prints every path absolute.
- `--out runs` (folders and the journal); `--ui DIR` mounts static files at
  `/ui` (the front end lives elsewhere; `/` redirects to `/ui/` when mounted,
  else returns a JSON index of the routes).
- Startup: load configs (`config.load_rig/load_run`), build the rig (three
  writes happen here and nowhere else outside a run or an action — the
  scope's `default_setup`, the 1918-C console's units and wavelength — and the
  `SessionStarted` header lists them as `startup_writes`), open the journal,
  run one read-back job, print the URL. Shutdown: abort the worker and
  **wait for the thread to end before releasing the bench** — a job inside a
  blocking VISA call ends when the call returns, and parking and closing the
  resources from the shutdown thread meanwhile would interleave with the
  generator's own I/O on the same sessions. `close()` waits `WORKER_JOIN_S` (60 s)
  beyond the worker's own `shutdown` timeout; if the thread still has not ended
  the bench is left as it is and the fact recorded, and the daemon thread ends
  with the process. The DIO **relay line is released, not closed**, at shutdown:
  the shutter driver's `close()` drives its line low, which on the relay module
  is a throw to the amplifier with no interlock — the relay stays where the last
  interlocked move left it.

---

## 10. Tests (all on the simulator, `--fast` semantics, no real sleeps)

- `tests/test_service_wire.py` — payload policy: StepDone decimated on the wire, scalars-only in the journal, verdict attached (the single-trace rail rule, the peaks from the full trace), `StepPhase` live-only, JSON round-trip.
- `tests/test_service_live.py` — the `inferred` overlay event by event on a simulated run, and the session's `/bench` read while a scan is held inside its acquisition: relay amplifier, bias LIVE, LED pulsing, shutter open, `StepPhase` frames with `seq` null and absent from the ring and the journal.
- `tests/test_drivers.py` — the real drivers' `read_state()`/`read_output()` transcripts on a scripted fake IO, and `?`/None when the instrument will not answer; `tests/test_service_rigs.py` — `build_real` reading outputs, levels and the 81150A's polarity off the fakes, a bench with no VISA coming up with the four roles unavailable, the relay move reading both sources first.
- `tests/test_service_journal.py` — append-only, first line, `last_used_params`, `run_index`, `settle_history`, `shot_time_s`, reading older sessions.
- `tests/test_service_worker.py` — bench lock (two jobs serialise), events reach subscribers in order with monotonic seq, `after_shot` keeps the shot and yields `RunAborted(requested)`, `abort` closes the generator and the `finally` parks, `resume` wakes a paused generator, a raising generator → `failed` then `parked`, queue FIFO + cancel.
- `tests/test_service_modules.py` — catalogue params with provenance (defaults → run.toml → last-used → edited → inherited), `build()` produces the right dataclasses (transcript-style assertions on the simulated instruments: LED pulsed at `led_v`, router in `transient()` for bace, the light untouched by `jv`), manual bace with `centre_on_voc` refuses without a V_oc source and accepts the session's jv_bace at the same level, coupling invariant enforced.
- `tests/test_service_pipeline.py` — node paths; every check in the catalogue triggered by a minimal tree; the canonical tree (9 T × 5 levels × jv_bace+bace) resolves to 90 module steps with the right inherited/derived params; cost with and without settle history; `temperature inside illumination` warns.
- `tests/test_service_executor.py` — the canonical tree with 2 T × 2 levels on the simulator: V_oc flows from jv_bace into bace at the same level (assert the bace ran centred on that V_oc: `AxisResolved.voc == jv metrics.voc`), relay positions at each boundary, `NeedsOperator` pauses and `resume` continues with `temperature_k` in the folder name, stop after_shot in the middle leaves `kept < requested` and `NodeDone(outcome="stopped")`, abort discards, folders written under one parent.
- `tests/test_service_api.py` — FastAPI `TestClient` (httpx is installed): `/bench`, actions journaled as by-hand, `/modules` + `PUT` params + reset, `POST /runs` → WS `/events` receives `RunStateChanged` … `RunFinished` … `parked`, `/runs/{id}/data`, `/pipelines/validate` on the canonical tree, `/pipelines` start + stop, 409/422/404 paths, the power monitor.
- `tests/test_architecture.py` gains: `bace.service.pipeline`, `modules`, `journal`, `worker`, `executor` import without `fastapi` (monkeypatch `sys.modules['fastapi'] = None` in a subprocess or check `sys.modules` after import); `experiment/`, `storage/` do not import `service`.

---

## 11. Not in this round

Results tab, failure recovery (the journal is append-only so it can be replayed
later; nothing replays it yet), 331 automation, multi-client writes, auth,
analysis. `checks.py` split. The front end itself.
