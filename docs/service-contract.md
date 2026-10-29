# BACE service contract

The service owns the instruments in one process on the lab PC. It exposes the
engine's synchronous event generators through HTTP and WebSocket. It queues
jobs, broadcasts events, writes the journal and executes pipeline trees.

This document describes the implementation in `bace/service/`. Paths with a
package or repository directory prefix are relative to the repository root.
The [manual](manual/README.md) explains operation and measurement physics.
`ui-rules.md` defines console rendering.

## 1. Package layout

```
bace/params.py            provenance machinery (Source, ParamSpec, ParamSet …)
bace/experiment/events.py Envelope, Progress.node_path, the service vocabulary
bace/experiment/wire.py   to_wire / envelope_to_wire
bace/drivers/readback.py  ask / on_off / number / word: the read-only queries
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
  worker.py       RunWorker thread and bus lock + Job (stop / abort /
                  resume) + the asyncio bridge that fans events out.
  wire.py         thin: envelope creation and the WS/journal payload policy
                  (which arrays are decimated, which are omitted).
  pipeline.py     Tree schema, node paths, validation (the check catalogue),
                  schedule resolution, cost model, dry run. Pure logic.
  executor.py     run_pipeline(rig, schedule, ctx) -> Iterator[Event]: the three
                  bindings, relay transitions, NeedsOperator, recorders, park.
  monitors.py     the observers: the power monitor and the temperature monitor
                  (USB/HTTP power; GPIB/HTTP temperature, with a bus lock).
  live.py         what the running step implies about the instruments, folded
                  from the run's events, overlaid on /bench as `inferred`.
  assistant.py    the console's assistant: a conversation over read-only tools
                  on the session (§8a).
  providers.py    the assistant's vendors: Claude, and OpenAI-compatible
                  endpoints. Imports `anthropic` and `openai` lazily.
  plans.py        experiment plans: the envelope, approval by fingerprint, and
                  the check of a submission under a plan (§8b).
  mcp.py          the Model Context Protocol for an external agent (§8c).
  app.py          create_app(session) -> FastAPI. Routes, WS, static mount.
```

Dependency rule, enforced by `tests/test_architecture.py`: `bace/core/` imports
nothing outward; `bace/drivers/` imports nothing from experiment, storage or
service. `bace/experiment/` and `bace/storage/` import nothing from service. `bace.service` imports
`pyvisa` and the concrete VISA driver implementations inside `rigs.py`
when `Bench.build_real` runs, so `--sim` works on a
machine with no VISA backend. Some driver types import eagerly, but opening
real instruments and importing PyVISA happen in `Bench.build_real`.
FastAPI/uvicorn are optional dependencies
(`pyproject.toml` extra `service`: FastAPI ≥ 0.110, uvicorn ≥ 0.29, websockets ≥ 12,
`anthropic` ≥ 2.3 and `openai` ≥ 3.40;
the lab PC needs the rig's VISA stack too, which is the `lab = ["bace[rig,service]"]`
extra); `bace.service.pipeline`, `modules`, `journal`, `worker`, `executor` must
import without FastAPI installed; only `app.py` and `__main__.py` need it.

The J–V recorder writes `bace-jv/4`. It stores `/config/resolved`, per-curve
`shutter` and `illumination` (`dark`, `light` or `unknown`). An unknown curve
omits the `dark` attribute. `density` is in mA/cm². The transient recorder
writes `bace-run/3`. [Data](manual/3.3-data.md) describes both formats.

`run_jv` manages shutter and LED only with `light_control="manage"`.
With `light_control="leave"`, it reads the illumination and leaves it alone.
`run_intensity_series` opens the shutter for `measure_dc`.
`_set_illumination` propagates a failing `led.off()` and skips the LED settle
on a rig with no LED.

## 2. Process and concurrency model

- One `Session` per process. `session_id = "YYYYMMDD_HHMMSS"` of start.
- One `RunWorker` thread executes bench jobs. VISA writes and run I/O
  stay on that thread: a run, a bench read-back, a bench action.
  Jobs are executed one at a time; a run is a long job. This is the bench lock.
- The worker pulls events from the job's synchronous generator and hands each
  to the asyncio side with `loop.call_soon_threadsafe(queue.put_nowait, …)`. The
  asyncio side (one task) enriches the envelope, writes the journal, and fans out
  to WebSocket subscribers. Recorders run on the worker thread, inside the
  generator chain (`storage.recorder.record`), exactly as `tools/scan.py` does.
- Queue: `POST /runs` while a run is active does not fail; it queues. FIFO,
  single operator. A queued run may be cancelled (`POST /runs/{id}/stop` while
  queued → state `cancelled`). The worker executes queued runs in submission order.
- Observers run beside jobs: power over USB or HTTP to `:8918`, and
  temperature over HTTP to `:8331`. A directly owned GPIB temperature monitor
  acquires the worker bus lock without blocking and skips busy ticks (§8).
- Stop semantics:
  - `after_shot`: set the job's abort flag; every generator polls it at its
    natural boundary (transient: before each step; jv: before each curve;
    executor: before each node). The shot in flight completes and is kept; the
    generator yields `RunAborted(reason="requested", done, total)` and unwinds
    through its `finally` (bias and SMU off, shutter shut; LED unchanged).
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
- `NeedsOperator` pauses the executor on a `threading.Event`. Stop and abort
  remain available; the wait polls flags every 0.2 s. `Job.resume` accepts
  detail only during the open pause. A resume outside it returns 409 and
  drops its detail, so a duplicate request cannot answer a later pause.

### Run states

```
queued → preflight → running ⇄ paused → stopping
             │           │                 │
             └───────────┴─────────────────┴─→ done | stopped | aborted | failed | blocked
                                               → parked (cleanup complete)
queued → cancelled (removed without acquisition)
```

The worker reports the terminal outcome before calling `job.park`, then
reports `parked` with that outcome. The run record retains the terminal state.
Cancellation removes a queued job without touching instruments.
`RunStateChanged(state, reason)` records every transition.

`worker.Blocked` at Start means an `invalid` or `crit` re-check fails. The
run does not acquire. The re-check uses the V_oc captured at submission,
so another queued run cannot change its source. An accepted stop updates
the record to `stopping` immediately. Later preflight/running frames do not
replace that state, though they remain journalled.

### IDs

- `run_id = f"{session_id}-{n:03d}"`, `n` counting every run in the session
  (manual and pipeline alike). A manual run is a pipeline run with one module
  node on the same code path.
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

Payload policy (`service/wire.py`), the one place that decides sizes:

| event | WebSocket | journal |
|---|---|---|
| `StepDone` | `light`, `dark`, `photo`, `photo_averaged`, `sync_light`, `sync_dark` decimated to ≤ 1000 points (stride, first and last kept; the last kept sample is the record's final one, at `t0 + (n-1)*dt`, not one stride after its predecessor) | scalars only: index, loop, step, setpoint, axis_value, q, q_mean, q_std, intensity_w, clipped, t0_int_record_s, t1_int_record_s, dark_first + `verdict` |
| `StepPhase` | in full, live only: `seq` null (like the drop `Notice`), not in the `since=` ring | not written |
| `JVPoint` | in full, live only, exactly as `StepPhase`; one per point of a sweep as it is read | not written |
| `RunFinished` | `photo_averaged` omitted; `values`, `q_mean`, `q_std`, `q_all` in full | same minus `q_all` if > 10 000 cells |
| `JVCurveDone` | in full (≤ a few hundred points) | metrics + label + n_points + the other scalars (index, dark, led_level_v, direction, intensity_w); no arrays |
| `JVFinished` | in full | its curves reduced exactly as `JVCurveDone`; otherwise the arrays the table keeps out come back through the summary |
| `LoopDone`, `AxisResolved`, `SeriesPointDone` | in full | in full |
| everything else | in full | in full |

Journal `JVCurveDone` retains curve identity scalars so the run index can
identify the light curve that supplies V_oc. `JVFinished` reduces its curves
by the same policy, so the summary cannot reintroduce trace arrays.

`StepPhase(index, phase, k, of)` is yielded by `run_transient_scan` as each
segment of a shot starts; `levels`, `light settle`, `acquire light`,
`dark levels` (absent with `dark_reference = "same"`, so `of` is 6 then and 7
otherwise), `dark settle`, `acquire dark`, `process`; for the live card's
"5 · acquire light" indicator. It is *ephemeral*: six or seven a shot say
where inside the shot the run is, which a log and a replay do not want, so it
is sent to the subscribers that are there with `seq: null` and never
journaled, ringed or numbered. A client that dedupes on `seq` passes it
through; a client that reconnects redraws from `StepStarted`/`StepDone`.

`JVPoint(index, k, of, label, dark, led_level_v, direction, voltage,
current, density)` reports each point read by the SourceMeter. `k` is the
point count within curve `index`; current is in A and density in mA/cm² when
an area is supplied. `Keithley2400.sweep_points` sources, settles and reads
one point at a time. Abort can land between points.

The frame is live-only. `JVCurveDone` carries the complete curve. The recorder
writes at each completed curve and at most every 2 s during a sweep. A partial
curve carries label `partial k of n`, `partial = true` and `points_planned`
on its HDF5 group, with no metrics. A fault keeps completed curves and the
partial curve. Abort does not report the incomplete curve as done.

The journal stores enough to render the session log and the history queries,
never the traces; the recorders (HDF5 `bace-run/3` + legacy `.dat`)
keep the data. `GET /runs/{id}/data` serves full arrays from an in-memory store
of the last 20 runs (older → 404 with the folder path, read the HDF5).

The `since=` ring keeps the last 5000 *envelopes*, but only the traces of the
last `RING_TRACES_KEPT` (200) `StepDone` shots. A ring full of thousand-point
lists is ~600 MB for one 100×51 scan; older `StepDone` frames in a replay carry
every scalar and the `verdict` (what a reconnecting client redraws Q(loop) from)
with all six traces `null` and `decimated[name] = {"omitted": true, "replay":
true}`, exactly as the journal line drops them. A WS client treats
`decimated[name].replay` as "redraw the loop curve, the trace is gone".

`RunQueued` is a dataclass in `experiment.events`
(`RunQueued(kind, module, tree, params, resolved, name, folder)`), sent down
the session's one event path like every other frame; one `seq` counter, one
journal line, one fan-out; the service assembles no frame by hand. It carries
both `params` and `resolved`: `params` is only what the operator chose; the
`edited` and carried-forward `last-used` layers, and never the V_oc parameter
— because it is what `last_used_params` hands back next session, and a journal
that fed every resolved default back as last-used would, after one run, show
the whole card as last-used and hide a recipe edit for twenty sessions.
`resolved` is every value the module ran with, for the record.

Service-added per-shot verdict. For every `StepDone` the asyncio side
computes `service.wire.shot_verdict(light.y, dark.y)` and attaches
`"verdict": {"rail_light": n, "rail_dark": n, "rail_run_light": n, "rail_run_dark": n, "shared_extreme": bool, "peak_light_a": A, "peak_dark_a": A, "spike_lag_ns": ns|null, "edge_light_ns": ns|null, "edge_dark_ns": ns|null, "averages_light": n|null, "averages_dark": n|null, "sync_edge_light_ns": ns|null, "sync_edge_dark_ns": ns|null, "sync_lag_ns": ns|null, "level": "ok"|"warn", "text": …}`
to the wire `data` (not to the dataclass).

The timing warning is defined in `service.wire.shot_verdict`:

| Evidence | Threshold | Result |
|---|---|---|
| Absolute spike lag and a broad leading edge | `SPIKE_LAG_NS = 0.25` ns and `SPIKE_EDGE_NS = 8` ns, both exceeded | `warn` |
| Absolute Sync lag | `SYNC_LAG_NS = 0.1` ns exceeded, independently of spike lag | `warn` |
| Spike lag alone | Above 0.25 ns with neither condition above | `ok`; lag remains reported |

Spike correlation mixes displacement current and extracted charge. It does
not establish jitter by itself. Aligned Sync edges constrain the trigger
chain; they do not prove equal latency from Sync to field arrival.
[The transient](manual/2.2-transient.md) and
[Checks and warnings](manual/4.2-checks.md) explain the diagnostic limits.

Measured values in `analysis/2026-09-06/spike-lag-and-dark-charge.md`:
six of 23 affected shots show tenfold current, reversed Q, 0.3–1.2 ns lag,
8–13 ns edges instead of about 6 ns, and spike heights 4–14 % apart.
Adding measured extraction current to an unshifted spike reproduces lag to
about 0.01 ns over 220–290 K. Shifting the dark trace removes only 3.5–4.6 %
of the spike difference. This is shape evidence, not a correction to apply
to Q. The same source records a 5.5 ns Sync pulse, 11 of 400 search samples,
with a 0.038 V percentile span against a 0.30 V edge test.

`edge_*_ns` and `sync_edge_*_ns` are 10–90 % edge times. The engine fetches
the trigger channel from each acquisition as `StepDone.sync_light/sync_dark`
in V. The socket decimates these with the current traces. The edge detector
supports steps and narrow pulses. It distinguishes absent Sync traces from
fetched traces whose edges cannot be resolved.

`averages_*` is stopped `:WAV:COUN?`. The driver clears the averager before
each acquisition, uses `:DIG` with `*OPC?` for the full count, and raises
`ScopeError` on a nonempty reset or short acquisition count. A nonnumeric
count is `null`; it is not proof that the requested average completed.
The engine can also yield a warning `Notice` for a short trace count.
HDF5 stores diagnostics by (loop, point), with Sync arrays under
`traces/sync_light` and `traces/sync_dark`.

| Rail evidence | Rule | Meaning |
|---|---|---|
| `rail_run_*` | At least 7 consecutive identical samples at one trace's extreme | `warn`; Q is meaningless |
| `rail_*` | More than 8 repetitions of one extreme anywhere in one trace | `warn`; the vertical range is probably too small |
| `shared_extreme` | Light and dark share an extreme | Reported without a warning; both contain the displacement spike |

`peak_*_a` is the signed extreme of each full trace before decimation; a
5:1 thinning can miss it. `Bench.shot_diagnostics()` adds
`scope.last_autorange_passes` when available. The status line can read
"autorange pass 1 · no shared extreme · 1 rail sample · spikes 0.01 ns apart · edge 6.5 ns · 200 avg · sync edge 2.1 ns".
The bench harness's `bench.checks.saturation_verdict` also warns on shared
extremes. Its rule differs from the service rule.

### Journal (`service/journal.py`)

- Path: `<out>/journal/<session_id>.jsonl`. One JSON object per line, the wire
  envelope (journal payload policy). Append-only; `flush()` after every line.
  Never rewritten. History queries read it; interrupted runs are not resumed.
- First line: `{"type": "SessionStarted", "data": {"session_id", "mode": "sim"|"rig", "rig_toml", "run_toml", "out", "fingerprint": bench.checks.fingerprint(bace root), "python", "version", "fast", "startup_writes": [...], "sample": {…}}}`.
  `sample` is the session's own `[sample]` block; what a file with no runs
  in it can still say. It is *also* on every `RunQueued` (`data.sample`),
  because a resumed file keeps the header of whoever opened it and two
  processes started in the same second share one.
  `startup_writes` lists assembly operations: scope `default_setup`, and
  meter units and wavelength. Direct meter opening also configures averaging
  and digital filtering; these writes are not separately enumerated in that
  list. The list is not a complete SCPI transcript.

- History queries (read this session's file and, newest first, every earlier
  `<out>/journal/*.jsonl`; stop at 20 files). Each file is parsed once and kept
  (this session's is folded line by line as it is written), so history queries and Dry run walk cached objects. Dry run asks
  `last_used_params` and `estimate_s` for each module node.

  - `last_used_params(module) -> dict | None`: the `params` of the most recent
    `RunQueued` for that module whose run reached `done` or `stopped`. Reads
    every bench's files: what the operator typed on the sim console is still
    what they typed.
  - `run_index(session_id | "all") -> list[RunSummary]`. A pipeline's
    `kept`/`requested`/`outcome_text` are the sum over its module nodes' counts,
    the same sum the session's run record makes, so `GET /runs` and
    `GET /runs/{id}` agree; a pipeline's `outcome_text` counts module runs
    (`"3/18 module runs · aborted"`), not the last module's line. A jv_bace's
    line gives the V_oc *range* over its light curves when several light curves exist
    (`"6 curves · V_oc 1.0269 … 1.0479 V"`), and each summary carries
    `voc_min`, `voc_max`, `light_curves`.
  - `run_record(run_id) -> dict | None`: one run's `summary()` plus its `tree`,
    its `verdicts` (every `warn`/`crit` the run's lines carried) and its
    `nodes`; every module node reduced by `node_record()` to the shape §6
    lists under `GET /runs/{id}`: its `NodeDone` detail (module, outcome,
    counts, V_oc and its LED level, the run LED level, temperature triple,
    baseline subtraction flag and folder) and
    what it measured (the axis and `q_mean`/`q_std` per point from
    `RunFinished`, `LoopDone` or the running mean; a J-V's curves as their
    metrics; its shot counts); from whichever session file holds it. What
    `GET /runs/{id}` uses this shape for a run outside process memory (§6).
    The results tab draws its grid from the record without opening HDF5.
  - `settle_history() -> dict[float, list[float]]`: seconds between
    `NeedsOperator(what="temperature")` and the matching `OperatorResumed`,
    keyed by setpoint (rounded to 0.1 K); when the 331 is wired this becomes
    the automatic settle time and the query does not change. Reads only files
    whose `SessionStarted` header has the same `mode`/`fast` as this session
    (a file with no header is read): a `--sim --fast` session settles a
    "cryostat" in the time a click takes, and the lab PC's Dry run must not
    learn that.
  - `shot_time_s(module="bace") -> float | None`: median interval between
    consecutive `StepDone` of the last completed bace run; same bench filter as
    `settle_history`, for the same reason (a sim shot is a millisecond).
- The submit-time checks that are not `ok` are journaled as `Verdict` lines
  right after the `queued` transition, each on its own `node_path`, so a
  record read back from the file carries the same flags this process does
  (`trigger.auto`, `intensity.factor`, `temperature.not-wired`); the Start
  re-read's chain verdicts follow and replace by `(code, node_path)`.
- `RunQueued` (`experiment.events.RunQueued`; `service/journal.py run_queued()`
  builds the same line for a test or a script), `RunStateChanged`,
  `NodeStarted/NodeDone`, `Verdict`, `NeedsOperator`, `OperatorResumed`,
  `BenchAction`, `TemperatureRead`, `PowerReading`, `SampleNamed` are all
  journaled; so is every run event under the payload policy, except
  `StepPhase` and `JVPoint`.

---

## 3a. `/session`; what is mounted

`GET /session` is who this process is: `id, mode, fast, started, started_at,
sample, sample_file, out, rig_toml, run_toml, fingerprint, journal, errors,
last_error`. The same `session` block rides on `GET /bench` and in `Hello`.

`PUT /session/sample` merges the session's `[sample]` block. It changes
the identity for runs submitted after the edit.

```
PUT /session/sample  {"sample": "s4", "material": "PTQ10IT4F", "pixel": null}
  -> 200 {"session": {…}, "changed": ["material", "pixel", "sample"], "run_active": "…-003" | null}
  -> 422 {"error": "[sample]: unknown key(s) …", "param": "sample", "accepted": [...]}
```

Rules:

- It is a merge. A key the body does not carry is left alone, so a console
  offering four fields cannot silently drop a `temperature_k` somebody typed
  into the file.
- `null` is the way back, to the value `run.toml` opened the session with ;
  the same meaning `null` has in `PUT /modules/{m}/params`. `sample_file` is on
  the wire (and only for this) so a client can render that layer and offer the
  reset beside a typed field. A key the file never had is removed rather than
  set to `""`.
- It touches nothing and takes no worker job, so it is *allowed while a run
  is going*; an operator who notices at hour one that the device is unnamed
  should not have to choose between abandoning the sweep and mislabelling
  everything after it. `run_active` is the run holding the bench, for the
  sentence the console owes them.
- Each submitted run captures its own `RunRecord.sample` and `RunQueued.sample`.
  `Catalogue.base_metadata(rec.sample)` uses that copy for folder names and
  metadata throughout the run. A session rename does not rename queued or
  active runs.

`SampleNamed(before, after, changed)` goes out on the event path like every
other frame; `run_id` null, `node_path` `""`; and is journaled, because a
record retains the identity assigned to each run. An unchanged sample
block emits no event and writes no line.

`RunMetadata.folder_name()` slugs `sample`, `material` and `pixel` with
`NAME_MAX = 24`. `as_dict()` retains the typed strings verbatim. The route
accepts names with spaces, colons and path separators; the folder uses their
safe forms. `GET /session` exposes these three forms as `sample_in_name`, so
the console previews the same spelling. Names sharing their first 24 slugged
characters can have the same folder prefix.

The console omits `temperature_k`, though the route accepts it. A controller
reading at node start supplies `temperature_how = "read"` when no temperature
node or typed value binds it. [Data](manual/3.3-data.md) explains the record.

---

## 4. `/bench`; read-back, chain, actions

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
                   … when a console is named: "console": "http://127.0.0.1:8331", and "reason" when an expected 331 is unavailable}
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
         "values": {"sense_resistor_ohm": 5.192, "pulse_amp": 4.0, "current_sign": -1.0, "trigger_offset_s": 4.71e-8, "light_path_delay_ns": 502.0, "probe_attenuation": 1.0, "led_threshold_v": 1.0, "max_current_compliance_a": 0.05, "max_voltage_compliance_v": 5.0}},
 "verdicts": [ …the bench-level Verdict list from the last read-back… ]
}
```

The real drivers query output state, 81150A polarity, generator levels and
frequency through `read_state()` and `read_output()`. Queries include
`:OUTP1?`, `:OUTP1:POL?`, `:VOLT1:HIGH?/LOW?`, `:FREQ1?`, `:PULS:DEL1?`,
`:FUNC1:PULS:WIDT?`; `:OUTP?`, `:OUTP:POL?`, `FUNC:SHAP?`,
`:VOLT:HIGH?/LOW?/OFFS?`, `:FREQ?`; and the Keithley's `:OUTP?`.
Read-back refreshes cached output flags used by the relay interlock.
Assembly reads each output before the first job. Failed queries give
`null` or `?`. `bias.polarity_read` distinguishes an instrument query from
simulator memory: unread real polarity warns; unread simulator polarity is
informational.

While a run holds the worker the read-back is the one Start took, before
the module enabled anything, so the instruments the running step implies are
overlaid on it from the run's own events (`service/live.py`): a `bace` step's
`NodeStarted` puts the relay on the amplifier, its `RunStarted` turns the bias
output and the LED pulse on at the step's levels, `InstrumentState` carries the
arming and polarity the run read back, `StepStarted` the pulse levels of the
shot, each `StepPhase` the shutter, a jv's `InstrumentState({"shutter"})` the
shutter and the LED's DC state, `JVStarted`/`JVFinished` the SourceMeter, and
the module's `NodeDone` marks bias and SMU off and shuts the shutter for
modules that manage it. LED and relay implications persist to the run-end
transition, which clears the overlay. Park leaves the LED running. Every overlaid instrument
carries `how: "inferred"` and is listed in `inferred`; `read_at` stays the
read-back's. LED and relay state can persist between module nodes; terminal run
transitions clear all inferred state. `state` is one
of `idle | preflight | running | paused | stopping`: a run still `queued` when
its preflight frame has not been applied reads as `preflight`, and a run in a
terminal state that the worker is still parking reads as `stopping`; `parked`
is a `RunStateChanged` transition every run passes through, not a resting bench
state, so it does not appear here.

`POST /bench/read` enqueues a read-back job (short); 409 while a run is active
(the chain is re-read automatically at every Start and the result attached to
the run). It waits for the job by default and answers 202 `{"job": "…",
…the /bench snapshot}` so a UI click sees the outcome; `?wait=false` returns the
bare 202 `{"job": "…"}` and the caller watches the stream. In `--sim` mode the
read-back reads the simulated instruments the same way. The temperature block
follows the newest `TemperatureRead` on the event path (the temperature
monitor, the pause's console poll, the operator's typed value) when it is newer
than the read-back, with `read_at` of that reading, `source` and, when the
monitor runs, `monitor: true` and its `reads` count. `session.errors`/
`session.last_error` surface what went wrong on the event path (a journal line
that could not be written); each is also logged to `bace.service` as it
happens.

Expected chain comes from the recipe in force: `led_polarity` expected `INV`
always; `bias_arm` expected `EXT` when `external_trigger`;
`bias_polarity` expected `RunConfig.polarity_instruction()` (`leave` → level
`info`, value shown, nothing expected).

`POST /bench/actions/{name}` with an optional JSON body. Every action is a
worker job, journaled as `BenchAction(name, args, result, by="hand")`, followed
by a read-back. `result.before` carries the read-back's values of what the
action changes (`{"led.polarity": "NORM"}`; for `park`, the bias, SMU, LED and shutter states), so
the session log can say "33220A:OUTP:POL NORM → INV · by hand" from the
journal alone. Waits by default and answers 202 `{"job", "name", "result",
"read_at", "bench": …snapshot}`; `?wait=false` returns `{"job", "name"}`. 409
while a run is active except `park`, which is allowed always: while a run is
active it aborts the current run and cancels every queued run before parking
(one `stop_runs` step on the worker, so a run popped between "cancel the queue"
and "abort the current" cannot slip through), because a person who clicks park
requires the bench parked before another run starts. A park
that has not started within the wait (the aborted run is inside a VISA call
that outlasts it) is not cancelled: the answer is 202 `{"job", "name",
"pending": true, "result": null, …}` and the park runs when the worker frees.
Any other action that timed out unstarted is cancelled and answered 504.
Actions:

| name | does | refuses when |
|---|---|---|
| `park` | `Rig.park()` disables bias and SMU and shuts the shutter; LED unchanged | never |
| `set-33220a-pol-inv` / `set-33220a-pol-norm` | `led.set_polarity(True/False)` | LED output ON as the instrument reports it (the chain fix is made with the LED off; say so), or the output query unanswered |
| `arm-81150a-ext` | `bias.configure_trigger(external=True, positive_slope=True)` | bias output ON as the instrument reports it, or unanswered |
| `set-led-pulse` | `led.set_pulse(level, low, frequency, duty)` from BACE parameters or request overrides + `enable_output(True)` |; |
| `set-led-dc` | `led.set_dc(level)` from BACE parameters or request override + enable |; |
| `led-off` / `bias-off` / `smu-off` | `disable_output()` |; |
| `shutter-open` / `shutter-shut` | `shutter.unblock()/shut()` |; |
| `relay-to-sourcemeter` / `relay-to-amplifier` | reads both sources' outputs back first (`read_output`, refreshing the flags the interlock reads), then `router._move` via the public context managers (enter `router.dc()`/`transient()` and exit immediately, so the interlock runs) | a source is live, or a source did not answer its output query → `crit` Verdict, nothing moved |
| `read-power` | one `PowerReading` from the meter | meter silent → `warn` |

Unknown action → 404. Chain fixes require explicit actions. Run cleanup and shutdown also park the bench.

### Saving the bench

Bench cards hold the catalogue's edited layer in process memory. Saving
records resolved values for every module so another session can restore them.

`POST /bench/save` body `{"name": "…"}` → 200 `{"name": "<stem>", "path":
"<out>/bench/<stem>.json"}`, 422 `{"error", "param": "name"}` when the name
stems to nothing. The record is `{"name", "saved_at", "bench"}`, where `bench`
is `{module: {param: {"value", "source"}}}` for every module in the
catalogue, resolved; the recipe's `bench` block widened from one tree's
modules to the whole bench. The name is stemmed by `_RECIPE_STEM` exactly as a
recipe's is, and an existing file is written over without asking (the console
arms the second click, which is where a confirmation belongs).

`GET /bench/saved` → `{"benches": [{"name", "path", "saved_at", "bench"}…]}`,
newest first; keyed `benches` as `/pipelines/saved` is keyed `recipes`: each
tab's saved things, under the name that tab calls them. A file that will not
parse is listed with an `error` and no values rather than dropped: the operator
saved it, so a picker that silently omitted it would say the save never
happened. `GET /pipelines/saved` answers the same way, from the same reader.

`DELETE /bench/saved/{name}` → 200 `{"name": "<stem>", "deleted": true}`, or 404
`{"error", "name"}` when there is no such file. The name is stemmed the way the
save stems it, so the console deletes under the name it lists. This is the only
route that unlinks anything: a recipe has no delete (nothing on the pipeline tab
offers one) and a run's folder is the record of an experiment. The console arms
the second click for it, as it does for the overwrite beside it; and for more
reason: an overwrite replaces a file with the bench in front of the
operator and this leaves nothing.

There is deliberately no load route. Putting a saved value back on the
bench is `PUT /modules/{m}/params`, one module at a time; the same edit a
card's field makes; so a value the spec refuses is refused with the
sentence the field would have given and the modules that took theirs keep it.
A load route would have had to invent an all-or-nothing rule for the whole
catalogue at once, and one that fails leaves the operator with the bench they
intend to replace and no idea which value stopped it. The console's bench tab
compares a picked file with `GET /modules`, lists every value that would move,
and sends those `PUT`s when the button under the list is clicked.

---

## 5. `/modules`; the catalogue with provenance

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

`GET /modules/{m}` returns that module's entry; an unknown name returns 404.

Layers, lowest first: dataclass defaults → `run.toml` (the file passed with
`--run`, mapped per module below) → last-used (journal) → edited (this session)
→ inherited/derived (pipeline context, never settable through the API). The
bench card applies the resolver's manual-run rule to `bace.voc` (§7, `_voc`):
when the session has a V_oc at the card's `led_v` (|Δ| ≤ 1e-9 V), no value is typed and `measure_dc` is off, the entry reads `{"value": 1.0423, "source":
"derived", "detail": "jv_bace jv_bace (this session)", "editable": false}` ;
what the run will centre on; rather than `default · null` with an empty
`needs`. A typed value still wins, as it does for the run.

### The catalogue

| module | maps to | params (name → where it goes) |
|---|---|---|
| `jv` | `run_jv(rig, JVConfig(light_control="leave", …))` | `start_v stop_v step_v settle_s both_directions pixel_area_cm2` → JVConfig; `smu_current_compliance_a smu_voltage_compliance_v smu_nplc` → SourceMeterConfig (real rig: `Keithley2400(res, config=…)`; sim: ignored). No illumination parameters at all; it sweeps under whatever light it finds, touching neither shutter nor LED on the way in or out, unwinds only the SourceMeter, and labels the curve from a read-back (`experiment.jv.illumination_state`): `as found dark`, `as found 1.020 V`, or `as found unknown` with a warning when the bench cannot say. `JVCurveDone.dark` is `True`/`False`/`None`, and None is not False |
| `jv_bace` | `run_jv(rig, JVConfig(light_control="manage", dark=<dark>, led_levels_v=<levels>))` | all of `jv` + `led_start_v led_stop_v led_step_v` (or inherited `led_v` → one level), `led_settle_s`, `dark: bool = True` (include the dark curve), `led_low_v` (unused by run_jv, carried for the rail). This is the module that *owns* the light: it sweeps illumination as the measurement, and it is the V_oc source |
| `light` | `rigs.apply_led` / `rigs.apply_shutter`; the same functions the `set-led-*`, `led-off` and `shutter-*` actions call | `shutter` (`open`/`shut`/`leave`), `led_mode` (`dc`/`pulse`/`off`/`leave`), `led_v led_low_v pulse_frequency_hz duty_percent settle_s`. Exists because a bench action is not a pipeline step: without it a tree of `jv` steps could never be dark. `leave` on either half touches nothing, so the shutter can move without cycling a generator that is at its thermal steady state. A run whose only module is `light` is `invalid` (`light.undone-by-park`): every run ends parked, so it would hand the bench back unchanged; the manual form is the bench action |
| `bace` | `run_transient_scan(rig, ScanSpec, RunConfig, voc=…)` inside `router.transient()` with the LED pulsed at `led_v` | `axis_name axis_start axis_stop axis_step centre_on_voc` → Axis; `vpre vcoll delay_ns n_loops` → ScanSpec; `vpre_on_voc: bool = False` (group `pinned`: the pinned `vpre` is an *offset from the V_oc in scope*; `vpre = V_oc + 0.000 V` when `delay_ns` or `vcoll` is the axis, TDCF at V_oc; resolved in `build()` from the same V_oc source `centre_on_voc` uses, under the same coupling check, so the engine still gets an absolute prebias; inert with the `vpre` axis, where `centre_on_voc` is the flag; `core.axis.voc_flags` reads each of the pair only on its own side of `axis_name`, so the one the console hides never acts); every `RunConfig` field verbatim; `store_shots`; `led_v led_low_v` (33220A pulse levels; inherited inside an illumination loop); `voc` (derived from the V_oc source, or edited = typed by hand → warn); `measure_dc: bool = False` (measure V_oc/J_sc/J_sat on the Keithley under the LED first, like the intensity series; `v_sat`); `led_settle_s` (the least wait after DC → pulse), `led_settle_max_s: float = 60.0` s and `led_settle_tolerance: float = 0.02` (group `illumination`: the power meter behind the open shutter is polled every 0.5 s to obtain three readings that agree within the tolerance, giving up with a warning at the maximum; see the README's "Light"); `smu_*` as above |
| `power` | `PowerReading` from the meter; `Read` is a worker job, `Monitor` is an observer | `wavelength_nm samples` |
| `temperature` | `service.temperature.settle`: through the 331 when `Rig.temperature` holds a controller (setpoint written, band held for `hold_s`), else `NeedsOperator` + wait; status `partial` | `setpoint_k tolerance_k hold_s timeout_s` |
| `park` | `Rig.park()` |; |
| `wait` | sleep | `seconds` |
| `note` | journal entry only | `text` |

`run.toml` mapping (`modules._TOML`):

| Table | Catalogue values |
|---|---|
| `[axis]` | `axis_*`, `centre_on_voc` |
| `[pinned]` | `vpre`, `vcoll`, `delay_ns` |
| `[acquisition]` | `RunConfig` fields, `n_loops`, `store_shots` |
| `[illumination]` | `level_v/low_level_v/v_sat` → `led_v/led_low_v/v_sat` for BACE; LED levels for `light`; both LED range ends and `led_low_v` for `jv_bace` |
| `[sourcemeter]` | `smu_*` for BACE and both J–V modules |
| `[sample]` | Session metadata, not module parameters |
| Optional `[jv]` | Sweep geometry, settle, directions and pixel area for both J–V modules; LED range, low level, settle and `dark` for `jv_bace` |

`Catalogue` validates `[jv]` keys; `config.load_run` does not read that table.
The shipped `run.toml` has no `[jv]` table. The remaining layers are defaults,
last used and edited values.

`ParamSpec.doc` is the short field explanation; `doc_full` holds an expanded
explanation when available. Dataclass fields use `params.field_docs`; service
parameters also have explicit specifications. Both reach the console.
Groups are `axis`, `pinned`, `acquisition`, `processing`, `timing`, `trigger`,
`output`, `illumination`, `sourcemeter` and `led`. `ui-fields.md` describes
card placement; the service supplies the groups and values.

`Catalogue.build(module, params: dict, ctx: RunContext, rig) -> Iterator[Event]`
returns the generator (already wrapped in the recorder for that module and in
the router context); this is the single place a module's parameters become
dataclasses. `RunContext` carries: `run_id`, `node_path`, `out_folder`,
`metadata: RunMetadata` (sample/material/pixel/operator from `[sample]`,
`temperature_k` from context or typed; with `temperature_how` and
`temperature_source` saying which, see §7; `led_drive_v`, `voc_v`), `led_v`
(inherited or None), `voc: VocSource | None` (`value, led_v, run_id, node_path, how`),
`sleep` (a no-op under `--fast`), `abort: Callable[[], bool]`, `resolved`
(seed for `RunRecorder.resolved`: `led_output_polarity`, `bias_arm_source`,
`bias_arm_slope`, `led_levels_v`, `led_frequency_hz` from the last read-back).

Manual `bace` semantics (the bench card). When `measure_dc` is set the builder
measures V_oc first, the way the intensity series does: 33220A to DC at the
level (`LedDrive.dc_settings`), the shutter open (`Rig.park()` shut it before
the job; an LED that is on is not light at the sample), `led_settle_s` for the
device to reach its light steady state, `smu.measure_dc` inside `router.dc()`,
then the shutter shut. Only then does it switch to pulse mode at
`led_v/led_low_v` at `pulse_frequency_hz/duty_percent`, enable, read the 33220A
back (refusing OFF, DC, another frequency, or `OUTP:SYNC?` = 0), open the
shutter and wait for the power meter to read stable (`led_settle_s`,
`led_settle_max_s`, `led_settle_tolerance`; the fixed `led_settle_s` alone
without a meter), and run the transient inside `router.transient()`. A V_oc
read under the pulse (a slow Keithley integrates over the on and off phases)
or with the shutter shut (the dark V_oc) would centre the axis on nobody's
V_oc, and the coupling check; which compares drive *levels*; cannot tell;
the simulator's `measure_dc` returns the dark 0 V when the shutter is shut,
so the test catches the class of mistake. After the scan the shutter is shut
and the LED is left pulsing: park leaves the generator running behind the shutter. A `light` node may
explicitly request `led_mode="off"`; managed dark J–V without a shutter
also switches the LED off.

V_oc source resolution for a manual run (`build()`): `voc` param typed →
`how="typed"`, else the source in scope (`ctx.voc`; the session's most recent
light J-V curve at `led_v`, |Δ| ≤ 1e-9 V), else `measure_dc`, else invalid when
`centre_on_voc`. An as-found `jv` curve qualifies only if its read-back put
the LED in DC: under a pulsing lamp the Keithley integrates across the
pulse's light and dark phases, so the crossing is a time average and no V_oc ;
the same reason `measure_dc` switches the generator to DC before measuring one.
The curve is still filed as light; it just provides no source, and the run says
so on a warning `Notice`. In a pipeline the resolver (`pipeline._voc`, §7) chooses one
first; a jv_bace in scope at this level, then `measure_dc`, then a typed value,
then the session's; and hands `build()` a schedule where a chosen measurement
has cleared `voc` and a chosen typed value left `ctx.voc` unset, so the two
never both arrive; the `build()` order governs only a direct call with both set,
where the number just typed wins. When `measure_dc` and a typed `voc` are set on
the *same* node the run measures and the typed number never runs, so `voc.typed`
warns that the measured value takes precedence.

---

## 6. `/runs`

- `POST /runs` body `{"module": "bace", "params": {…overrides on top of the module's resolved params…}, "name": "…"}` → 202 `{"run_id", "state": "queued"|"preflight", "position": 0}`.
  Internally: build a one-node tree, validate (§7), refuse with 422
  `{"checks": […]}` on `invalid`/`crit`, else queue.
- `POST /runs/{id}/stop` body `{"mode": "after_shot"|"abort"}` → 202 `{"run_id",
  "state", "mode"}`; the `state` reflects the accepted request; `stopping` for a running or
  paused run (reported at once, so the record, `/bench` and the stream all say
  `stopping` from acceptance of the stop), `cancelled` for a queued
  run. 404 unknown; 409 if already terminal.
- `POST /runs/{id}/resume` body `{"note": "set to 250.0 K by hand", "temperature_k": 250.1}`
  → 202; 409 if not paused. `temperature_k` becomes the context temperature
  for the subtree (folder names, metadata) and is journaled in `OperatorResumed`.
- `GET /runs?session=<id>|all` → `[RunSummary]`: `run_id, session_id, kind ("manual"|"pipeline"), name, module|tree_summary, sample, state, queued_at, started_at, finished_at, kept, requested, outcome_text, folder, node_count, node_count_done, voc_min, voc_max, light_curves`.
  `sample` is the `[sample]` block captured at submission (`sample, material,
  pixel, operator, comment, temperature_k`), on every row; the identity
  travels with the run, not only in the session header , so a grid grouped by device parses no folder name.
- `GET /runs/{id}` → the full record: tree, resolved schedule, params with
  provenance as executed per node, per-node outcomes (`NodeDone` details),
  verdicts (one entry per `(code, node_path)`; the Start re-read replaces the
  submit-time copy, so a "N warn" count off the record is honest), chain
  read-back at start, folders written, error if failed, `progress` (the
  module's own), `eta` (the latest loop `Progress` with the executor's
  re-derived ETA: `{eta_s, finish_at, at, node_path, done, total}`; null for a
  manual run), and `cost` whose `finish_at` follows that ETA once one has been
  measured (`finish_source: "measured"`, `finish_at_submit` kept beside it).
  `sample` as on the index row, and `nodes`; every module node that has
  ended, in the shape §3 `run_record` gives (`journal.node_record` builds it
  for both, so the two sources agree byte for byte): `{node_path, module,
  outcome, kept, requested, voc, voc_how, voc_led_v, led_v, temperature_k,
  temperature_how, temperature_source, offset_corrected, summary, folder,
  error, reason, started_at, finished_at, elapsed_s, axis, values, q_mean,
  q_std, curves, shots, intensity_recorded, shots_flagged}`. `axis`/`values`/
  `q_mean`/`q_std` are a transient's per-point statistics (from `RunFinished`;
  from the last `LoopDone` or the last shot's running mean when the node stops before a `RunFinished`), `curves` a J-V's curves reduced to their metrics, and
  the three counts what the node's `StepDone` lines said; never a trace.
  `from: "session"`. For a run this process never held; last week's; the
  answer is the journal's record (§3 `run_record`): the summary fields plus
  `tree`, `nodes` and `verdicts`, `from: "journal"`, `data_in_memory: false`;
  404 when no journal file knows the id.
- `GET /runs/{id}/data?node=<node_path>` → for `bace`:
  `{"axis": {…}, "values": […], "q_mean": […], "q_std": […], "q_all": [[…]], "time_s": […], "light": [[…]], "dark": [[…]], "photo": [[…]], "last_shot": {"light": […], "dark": […], "photo": […], "cumulative_q": […], "t0_int_record_s": …, "t1_int_record_s": …}, "kept": 12, "requested": 20}`;
  for `jv_*`: `{"curves": [{label, dark, led_level_v, direction, voltage, current, density, metrics}]}`
; `voltage` in V, `current` in A as the instrument reported it, `density` in
  mA/cm² and `null` when `pixel_area_cm2` is 0 (there is no density without
  an area; the client shows amps). `metrics.jsc` is in A, not a density:
  it is interpolated from `current`, independently of pixel area;
  for a pipeline run without `node` → 400 listing the nodes.
  Full precision, no decimation. The `cumulative_q` array is the running
  integral of the last photocurrent over that shot's own integration window
  `(t0_int_record_s, t1_int_record_s]` (the LabVIEW "Integrated PhotoCurrent"
  plot; computed with `numpy.cumsum(photo[inside]) * dt`). The window is
  measured from the pulse, so it differs from shot to shot along a delay axis;
  `StepDone` carries the same two numbers for every shot.
- `GET /events` WebSocket. Query `since=<seq>` replays from the in-memory ring
  (last 5000 envelopes, traces of the last 200 shots; see §3) then streams live.
  First frame: `{"seq": null, "type": "Hello", "data": {"session", "seq":
  last_seq, "bench": <GET /bench>}}`. The envelope `seq` is `null`, not
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
  illumination loop may not set `led_v`/`led_start_v` etc.; the loop owns it.
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

### The three bindings

1. illumination loop → `led_v` of every module inside it. Set as
   `Source.INHERITED` (detail `"illumination loop"`) on `jv_bace.led_levels_v = [led_v]`
   and `bace.led_v`. The loop also owns `led_low_v`.
2. `jv_bace` → V_oc → `bace` at the same `led_v`. Within one illumination
   iteration, the latest light curve's `metrics.voc` at that level becomes
   `bace.voc` (`Source.DERIVED`, detail `"jv_bace <node_path>"`). `measure_dc`
   on the `bace` node is the alternative source. A `bace` with
   `centre_on_voc` and no source in scope is invalid at validation.
   Outside any loop (manual), the session's last `jv_bace` at the same level
   qualifies (§5).
3. Relay transitions at every `jv_* ↔ bace` boundary: `run_jv` already
   runs inside `router.dc()`; the executor runs `bace` inside `router.transient()`.
   The interlock (`Router` refuses while a source is live) is the guard; the
   schedule marks each transition so the UI can draw "relay 2400 → amplifier".

### Temperature (the 331, or the pause)

One helper, `service.temperature.settle`, serves the temperature loop and the
`temperature` module; which path it takes is decided by `Rig.temperature`.

The 331 answers the last query it receives, so it needs one owner.
`Bench.build_real` opens `[temperature] address` through
`drivers.lakeshore331.controller`; `controller._LOCK` serialises access.
Naming `[temperature] console` delegates ownership to that console. Empty
address and console disable the controller without an unavailable warning.
In `--sim`, a nonempty console attaches the stand-in; the address alone does
not. Empty console selects operator pauses.

With a controller attached the node settles on its own. It is read first
— the first poll, at zero on the poll clock, is the cryostat as found and
goes out as a `TemperatureRead` like every other; a setpoint is written to an
instrument that answers, never into a controller with nothing behind it ;
then the setpoint is written (once), the controller is polled every 5 s
through `RunContext.sleep` (so `--fast` costs polls, not wall time), each
usable reading is yielded as
`TemperatureRead(source="instrument"|"console"|"simulated")`, and the node is
done once
the reading has stayed inside `tolerance_k` of the setpoint, with no ramp
still walking it (RAMPST?), for `hold_s`; the dwell starts when the band is
entered. Then `Verdict(ok, "temperature.settled", "250 K reached in 27 min,
held 60 s", node_path, data={setpoint_k, kelvin, settle_s, hold_s, held_s,
polls, source})`, and the last measured kelvin (not the setpoint) becomes
the subtree's `temperature_k`; folder names and metadata. A `temperature`
*module* binds the same way for the nodes after it, and for the rest of the
run, not the rest of one loop iteration: the cryostat retains its setpoint unless another temperature node moves it, so a `bace` beside it; or at the
next level of an enclosing illumination loop; is labelled with what the
cryostat is at, not with the session's number. A temperature loop still wins
for its own subtree; each iteration writes its setpoint again on the way in.

Every `RunMetadata`, HDF5 `/metadata` group and journalled node carries
`temperature_how` and `temperature_source`. The folder's `290K` alone
cannot distinguish them.

| `temperature_how` | `temperature_source` | means |
|---|---|---|
| `typed` | `""` | `[sample]` in the recipe, or the console's metadata field. Nobody read an instrument. |
| `setpoint` | `""` | A temperature loop asked for it and the settle never got one reading. Requested temperature without a measured value. |
| `read` | `instrument` \| `console` \| `simulated` | Nobody typed one and no temperature node bound one, so the module read the controller once as the node started (`modules._bind_temperature`): the bench as found. Without a typed value or controller, temperature stays not recorded. |
| `settled` | `instrument` \| `console` \| `simulated` | The controller held it inside the band. `instrument` is this process on the bus, `console` the 331 console asked over HTTP; `simulated` means the stand-in, so a `--sim` file cannot be mistaken for a measured one. |
| `operator` | `operator` | A person typed the number at the pause. |
| `operator` | `instrument` \| `console` \| `simulated` | A person ended the pause without typing one; the number is the last reading polled while they decided. |

Both fields are stored and rendered. Missing fields mean the file does not
record that provenance. Current transient files use `bace-run/3`.

`[sample] comment` is retained verbatim in metadata. The folder uses
`bace.storage.naming.slug`: whitespace and `_` become `-`, Windows-forbidden
characters are removed, non-ASCII characters remain, and the limit is
64 characters. Folder names are labels; metadata is the record.

Each node is marked in the controller's audit trail at the setpoint write
and at the settle, best effort; the console's own log through `POST
/api/note` when a console owns the instrument, the session journal when this
process does The owner keeps the audit record.

The 350 K ceiling and the front-panel settings hold whoever owns the bus, and
each failure is named for what it is:

- the heater watchdog cuts the heater after
  `Limits.max_consecutive_faults` consecutive faults, and the count resets on
  the first clean one. A fault is a bad control-sensor status (`RDGST?`) or
  a heater fault (`HTRST?`): an open or shorted load while the sensor reads
  well would otherwise reset the count forever. Cutting is `RANGE 0`, plus --
  on a loop-2 cryostat, where `RANGE` does not reach the analog output --
  `MOUT 0` then `CMODE open loop`, in that order. It is fed from every
  `read()` (the monitor, the settle) and from the worker's own sleeps
  inside a run (`rigs.Bench._feed_watchdog`, at most every
  `WATCHDOG_POLL_S`), because the monitor cannot read while a run holds the
  bus and a fault beginning after a temperature settles must not wait hours
  to be seen. `DirectTemperatureController.heater_cut` holds the reason once
  it has fired;
- a setpoint above the 350 K ceiling (`[temperature] max_setpoint_k`, or the
  console's own limit when a console owns the bus) is refused, never
  clamped; `Verdict(crit, "temperature.refused", <the controller's own
  sentence>, data={…, "error": <the driver's full text>, "status": 403})`
  then `NeedsOperator(what="temperature refused", detail={…, "error"})`. The
  direct driver raises the same `TemperatureError(refused=True, status=403)`
  the console's HTTP 403 produced, so this path is one path. A refused
  setpoint never names the subtree: a resume with no `temperature_k` typed
  takes the last reading;
- the heater range, ramp rate and PID are whatever the front panel holds. A
  heater range of 0 while the setpoint is above the reading is said at once
; `Verdict(warn, "temperature.heater-off", …)`, once per node; and the
  settle goes on toward its timeout, so the operator raises the range on the
  instrument instead of finding out half an hour later; nothing is changed
  from here, and a cool-down with the heater off is not a warning;
- `timeout_s` on the poll clock without reaching the band, three consecutive
  readings with `connected` false (`reason = "silent"`, before or after the
  write), or the bus itself not answering three polls or the setpoint write
  (`reason = "unreachable"`: fix the instrument or the cable; or start the
  console, if rig.toml names one; do not reconsider the setpoint) gives
  `Verdict(warn, "temperature.timeout", …,
  data={…, "reason", "written", "error"})` then
  `NeedsOperator(what="temperature timeout", detail={…, "kelvin",
  "elapsed_s", "reason", "written", "error"})`; the run does not proceed to
  measure at a temperature it did not reach; the operator resumes (accepting
  the reading, optionally typing `temperature_k`) or stops. A write the
  console did not answer in time is read back once before that verdict: when
  the state already shows the setpoint in force (a slow bus, not a dead
  console) a `Notice(warning)` says so and the settle goes on.

A stop or abort is honoured at every poll.

Without a controller the node pauses:
`NeedsOperator(what="temperature", node_path="T=250K", detail={"setpoint_k": 250.0, "tolerance_k": 0.2, "hold_s": 60, "timeout_s": 1800, "index": 4, "count": 9})`,
run state `paused`, and waits. `POST /runs/{id}/resume` wakes it; the resume
detail (`temperature_k`, `note`) is journaled as `OperatorResumed` and
`temperature_k` flows into the subtree's metadata. While any pause lasts
(this one, or the timeout/refusal ones above, on the loop path and on the
`temperature` module's alike) an attached controller is polled every 5 s and
each `TemperatureRead` goes out live; through an out-of-band `emit` hook
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
`temperature.not-wired` check, come from the validation snapshot: `ok` only when the read-back showed the controller
attached and `connected` true; `warn` (a pause) when no controller is
attached, when the named console is silent at Start, when a responding console fails the read-back (`error` in the block: start the console),
when it is up with the instrument silent behind it, and when a controller is
attached but not read back yet (`connected` null in the bench stub before
the first read-back). The run decides again at the node from a fresh read.

### The check catalogue (`pipeline.validate`)

Stable ids; each result is a `Verdict(level, code, text, node_path)`.
`invalid` and `crit` block Start; `warn`/`info`/`ok` do not. A `fix` field names an explicit bench action. The BACE builder also sets
LED polarity to match external arming before configuring pulses.

| id | level | condition |
|---|---|---|
| `tree.shape` | invalid | unknown kind/loop/module, loop without children, malformed values |
| `tree.owned-param` | invalid | a module inside an illumination loop sets `led_v`/`led_*_v` |
| `light.undone-by-park` | invalid | the run's only module is `light`: every run ends parked, so it would set a light and hand it straight back. Names the bench actions, which are worker jobs without run-level park |
| `voc.source` | invalid | `bace.centre_on_voc` or `bace.vpre_on_voc` with no V_oc source in scope (`data.needed_by` names the flag) |
| `voc.coupling` | invalid | V_oc source's `led_v` ≠ the bace's `led_v` (`assert_axis_centre`) |
| `led.levels` | invalid | `LedDrive` refuses (low ≥ threshold, level < threshold, level ≤ low) |
| `axis.geometry` | invalid | `Axis`/`ScanSpec` refuse (step, n_loops < 1) or `delay_ns < 0` (`:PULS:DEL1` is given `delay_ns` as it is). `centre_on_voc` on a non-vpre axis and `vpre_on_voc` on the vpre axis are not faults: each flag is read only on its own side of `axis_name` (`core.axis.voc_flags`) |
| `bench.instrument` | invalid | a module step's instrument is in the read-back's `unavailable` (`Catalogue.needs` minus the advisories in `ADVISORY_NEEDS`; the `power` module needs its meter). `warn` for a plain `jv` with no shutter or LED to read: it runs, its illumination recorded as unknown. `info` before the first read-back. This refuses a run before acquisition when a required instrument is unavailable |
| `smu.ceiling` | crit | compliance above the bench ceiling (`config.check_smu_limits`) |
| `relay.interlock` | crit | a tree with both a `jv_*` and a `bace` and no `Router`; or at Start a source reports its output live |
| `bench.live-at-start` | crit | bias or SMU output ON at Start (read-back) |
| `voc.typed` | warn | `bace.voc` typed by hand, not measured this session; or a typed `voc` ignored because `measure_dc` on the same node measures it |
| `chain.led-polarity` | info / warn / ok | INV is `ok`. For externally armed BACE, unread or NORM is `info` because the builder writes polarity at Start (INV for positive slope, NORM for negative). Other transient cases warn; a J–V-only tree reports info. The bench card warns on unread or NORM independently of a tree. |
| `chain.bias-arm` | warn | 81150A arming source or slope differs from `external_trigger` and `trigger_slope_positive` (fix `arm-81150a-ext`) |
| `chain.bias-polarity` | warn | 81150A `:OUTP1:POL` ≠ the recipe's instruction (info when `leave`, or when unread `?`; the run writes it at Start, as the `/bench` card reports) |
| `temperature.not-wired` | warn / ok | a temperature loop or module: `ok` when the read-back shows a controller attached and its instrument answering ("settles automatically, ±tol, hold, timeout"), `warn` when no controller is attached (pauses at each T for a manual set; names the console when one is named and silent) or when the console is up with the instrument silent behind it (pauses too). The schedule's `needs_operator` on temperature steps follows the same rule; `True` before the first read-back |
| `temperature.inside-illumination` | warn | temperature loop nested in an illumination loop; text states the cost |
| `trigger.auto` | info | `trigger_sweep = AUTO` (a missing trigger looks like data) |
| `power.console` | warn | Required power read unavailable over direct USB or the configured console; intensity is not recorded. Before a read-back, availability is informational. |
| `intensity.factor` | info | no calibration factor → intensity recorded in watts at the meter, not irradiance at the sample |
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

Output: `{"total_s": …, "measuring_s": …, "waiting_s": … | null, "per_temperature": [{"setpoint_k": 250, "settle_s": 2220 | null, "measure_s": 438}], "lower_bound": true when any settle is None, "finish_at": ts | null, "t_shot_s": 0.8, "t_shot_source": "journal"|"default", "unestimated_modules": n}`.
For plain `jv`, `led_settle_s = 0` in the estimate because the module leaves
illumination alone. A `temperature` module, like a temperature loop, adds
settle history and hold. Unestimated module counts are explicit.

Never invent a settle time: `null` renders as "—" (the design's table shows
"—" for the first temperature).

### Endpoints

- `POST /pipelines/validate` body `{"tree": …}` → 200 `{"valid": bool, "checks": [Verdict…], "schedule": [Step…], "counters": {…}, "cost": {…}, "node_paths": […], "folder": "<out>/<stem>", "folder_pattern": "<out>/<stem>_YYYYMMDD_HHMMSS"}`. This is the Dry run button: touches nothing. `folder` is the stem; the run's folder is `folder_pattern` with the stamp `submit` takes, which the Dry run cannot know; the console shows the folder pattern and module count.
- `POST /pipelines` body `{"tree": …, "name": "…"}` → validates (including a fresh chain read-back job when idle), 422 with the checks when `invalid`/`crit`, else 202 `{"run_id", "state", "checks", "cost", "folder"}` (the stamped folder).
- `GET /pipelines/last` → the last validated tree in this session (so the UI can reopen it). Saving recipes to disk is `POST /pipelines/save {"tree", "name"}` → `<out>/recipes/<name>.json`; `GET /pipelines/saved` lists them. Small, optional. The bench's own save (section 4) writes the same `bench` block to `<out>/bench/` for the whole catalogue, and both listings come from one reader; a file that will not parse is a row with an `error`, not a row that vanished.
  The file is complete. A tree carries only what its nodes override; everything else a node runs with is the module's bench value resolved at submission, so a file holding the tree alone would run differently after a bench edit with no diff in it. The record is `{"name", "saved_at", "tree", "bench"}`, where `bench` is `{module: {param: {"value", "source"}}}` for every module the tree names, as the bench had them at save time (a tree that does not parse records `{}`). The UI compares `bench` with `GET /modules` when a recipe is reopened, lists every parameter the bench has moved on that some node of that module still takes from the bench (not one every node overrides or a loop binds), and offers to `PUT` the recipe's values back. Start is unchanged: it runs with the bench as it is; the note is what makes that visible.

### Executor (`executor.run_pipeline`)

A synchronous generator run as one worker job. For each schedule step:
`loop-enter` → `NodeStarted`, apply the binding (illumination: `led.set_pulse`
happens inside the module builder, not here; the loop only sets the context),
temperature: the settle through the attached controller, or `NeedsOperator` + wait + `hold_s` (see "Temperature" above); `Progress(node_path=<loop path>, done=i, total=n, eta_s=…)`.
`eta_s` is re-derived from what this run has measured: the sum of what is left in the schedule, where a module costs the
median duration of the same module in this run once one has completed (its
`estimate_s` before that) and a temperature costs the median settle the
operator has taken *in this run* once one has been measured (the journal's
median from the schedule before that, or nothing) plus `hold_s`, and an
illumination level its settle. Every loop enter and exit carries it. Measured durations replace initial
estimates; the session copies the latest into the
record's `eta` and `cost.finish_at` (§6). A number that no settle has informed
yet is a floor; the cost's `lower_bound` says so.
`module` → `NodeStarted`, `catalogue.build(...)`, forward every event unchanged
(the worker stamps the envelope with this step's `node_path`), capture what the
next binding needs (V_oc from `JVCurveDone` light curves; `RunFinished` for
the summary), `NodeDone(outcome, detail={"module", "kept", "requested", "folder",
"folders", "summary"})`. A module that reported its own end (`RunFinished`/
`JVFinished`) is `ok` even if `after_shot` arrives during its last shot
— a complete node is not `stopped`, and the stop takes effect at the next node.
On `RunAborted` from a module: stop the tree (`NodeDone(outcome="stopped")`
for the open nodes), then unwind. On an exception: `RunFailed(error, where=node_path)`,
unwind, re-raise nothing (the worker marks `failed`). `finally`: `Rig.park()`.

An abort closes the generator before it can emit `NodeDone`, so the *session*
synthesizes one per node the worker saw open, with the folder its recorder had
opened (`RunContext.folders`) and the shots it took, on the same event path as
the frames; so `GET /runs/{id}` names the folder and the counts of a run whose
files are on disk, rather than reporting nothing. The record's terminal state
and `parked` are likewise applied on the event path (as the `RunStateChanged`
frame is ingested), after those synthesized `NodeDone`s, so a client polling on
`parked` never reads a run as finished-with-no-data a beat before its counts
land.

`RunContext.sleep` is abort-aware on the real bench (`Bench.sleeper`): a settle,
a temperature `hold_s` (default 60 s) or a `wait` module sleeps in 0.2 s slices
and returns early once abort is requested, so the abort button is not dead
for the length of a hold. An `after_shot` does *not* cut a sleep short; the shot
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
`DELETE /monitors/power` stops it; `GET /monitors` lists. The meter is
never on the bus; USB when this process owns it, HTTP when the meter's
console does; so it runs right through a bace scan ,
serialised against the worker's own reads by `rigs._METER_LOCK`. One monitor
of each kind at most. If the meter stops answering, emit a `Verdict(warn,
"power.console")` once and keep trying. `--power-monitor SECONDS` on the
service starts it as the process comes up, so the meter is watched from boot
and not from the moment a console is opened.

Power history: The session keeps every monitor reading; up to
`POWER_HISTORY_MAX = 200 000`, 55 h at 1 Hz; across monitor stops and
restarts, and stamps each with the same `ts` its frame carries, so a console
that folds the stream can merge it without a duplicate.
`GET /monitors/power/history?since=<ts>&limit=<n>` answers `{points:
[[ts, watts, trustworthy], …], count, total, kept_max, first_ts, last_ts,
wavelength_nm, source, averaged, running, interval_s}` (the last five are the
meter's settings, off the newest reading); `GET /monitors/power/history.csv`
is the same as a file (`time_iso, unix_s, watts, trustworthy, wavelength_nm,
source, averaged`, `Content-Disposition: attachment`);
`DELETE /monitors/power/history` forgets it; the journal on disk keeps every
reading regardless.

`averaged` on `PowerReading`, `instruments.power` and `read-power` reports
meter averaging. Direct opening configures DC-continuous mode, a 5 Hz analog
filter when enabled, and `digital_filter` smoothing (`rig.toml`, default
100 samples; zero disables it). `null` means the driver cannot report the
setting, including the console path.

At the recipe's 500 Hz and 50 % duty, averaged power is the mean over the
cycle, approximately half the on-state power when the low level is dark.
An unfiltered reading can sample either phase. Frequency and duty are
parameters, not fixed values for every transient. Power is in W at the meter;
it is not irradiance at the sample.

`POST /monitors/temperature {"interval_s": 5.0}` is the same shape for the
331; 422 only when the bench has no 331 at all (neither `[temperature]
address` nor `console`). This monitor is on by default: a bench
that has a 331 starts reading it in `Session.start`, at
`TEMPERATURE_MONITOR_S = 5 s`, before any console connects; a cryostat
drifts whether or not a run is going, and the card's number should be a
reading of a known age rather than the read-back of whenever somebody last
pressed something. `--temperature-monitor SECONDS` changes the interval and
`--no-temperature-monitor` turns it off; the route is then how a console
changes the interval (`DELETE` then `POST`) or starts it again, not how it is
first switched on. A bench with no 331 starts nothing and records no
start-up error: that is the ordinary bench, and `unavailable["temperature"]`
already says why the card is empty. Every reading is a
`TemperatureRead(source="instrument"|"console"|"simulated")` on the stream
and in the journal, the `/bench` temperature block follows it (`kelvin`,
`read_at`, `monitor: true`, `reads`), and a silent instrument is one
`Verdict(warn, "temperature.console")`. This is the temperature card's
"294.8 K · 331 reads" while a scan runs. `DELETE /monitors/temperature` stops it.

The one exception to "observers never touch the bus." When this process
owns the 331's GPIB session its monitor *is* on GPIB0, so it takes
`RunWorker.bus`; the lock the worker holds for the whole of a job --
acquires it without blocking, and reads only while holding it. A tick
that cannot take it is skipped and counted in `skipped` on `GET /monitors`;
a skip is not a failure. Holding the lock rather than testing `worker.idle`
is the point: a separate idle test can race with a job start and permit interleaved
queries. With a console named the
331 is HTTP again and no lock is passed.

What that costs. A pipeline run holds the bus for its whole length, so
during a long subtree the temperature monitor emits nothing and the card's
reading goes stale. `skipped` is how a UI says *why* rather than showing an
stale reading as current; render it. Readings still arrive from
the worker where it is safe to take them: while a temperature node settles,
and while a run is paused for the operator.

The heater watchdog needs periodic fault checks even while the worker
holds the bus. The worker feeds it from its own sleeps (§7), which is a fault check
and not a reading; no `TemperatureRead` is emitted from there, because a
watchdog read is not a temperature binding for the surrounding shot.

---

## 8a. `/assistant`; the console's assistant

A conversation with a model that reads the bench and explains it: Claude,
or any OpenAI-compatible `/chat/completions` endpoint (`providers.py`). It
changes nothing: its tools are `get_session`, `get_bench`, `list_modules`,
`get_module`, `list_runs`, `get_run`, `get_events`, `get_monitors`,
`dry_run` (`Session.validate_tree(remember=False)`, so `/pipelines/last` is
untouched) and `read_doc` over a fixed list of the project's documents and the
session's `run.toml` and `rig.toml`.

| route | answers |
|---|---|
| `GET /assistant` | `{available, providers: {anthropic, openai}, model, docs}`; each provider `{available, sdk}` or `{available: false, reason}` without `anthropic` 2.3 / `openai` 3.40 |
| `POST /assistant/chat {"text", "conversation"?, "provider"?, "model"?, "base_url"?}` | NDJSON frames: `conversation {id}`, then `text {text}`, `tool {name, input}` and `tool_result {name, ok, text?}` in order, then `done {stop_reason, usage, model}` or `error {message, status}`. `provider` is `anthropic` (default) or `openai`, which needs `model` and `base_url`. 401 without a key the provider needs, 422 for an address or model it cannot use, 503 without the SDK, 409 while the same conversation is still answering |
| `DELETE /assistant/conversations/{id}` | forgets one history; 404 when there is none |

The API key is the asker's: header `X-API-Key` on each `POST`, used for that
request and not stored, journaled or logged. An OpenAI-compatible address must
be `https://`, or `http://` to this machine, which also needs no key (a local
model). Histories are kept in memory, the newest 32, each under its provider,
address and model, so an assistant turn goes back exactly as it came and a
question with another model starts another conversation; a turn that fails or is
abandoned preserves the history. Claude is `claude-opus-5-5` unless the
request or `BACE_ASSISTANT_MODEL` says otherwise, at effort `medium`, with the
API's server-side fallback on a refusal for the models that take it.

---

## 8b. `/plans`; experiment plans

An agent runs experiments only inside a plan a person approved
(`service/plans.py`). A plan has a title, a goal and an envelope:

```json
{"title": "V_coll saturation", "goal": "Q against V_coll at V_oc",
 "envelope": {
   "modules": {"bace": {"vcoll": {"min": -4, "max": -0.5}, "n_loops": {"max": 10}},
               "jv_bace": {}},
   "loops": {"illumination": {"levels_v": {"min": 1.0, "max": 1.03}}},
   "max_runs": 3, "max_total_s": 3600, "expires_s": 14400}}
```

| Envelope key | Meaning |
|---|---|
| `modules` | The modules a tree may contain, each with the parameters a module node may set and their bounds. A bound is `{"min": a, "max": b}` (either side optional, inclusive; a list value must lie within it element by element) or `{"choices": [...]}`. `{}` allows the module with nothing set. |
| `loops` | The loops a tree may contain: `values_k` for `temperature`, `levels_v` for `illumination`, `count` for `repeat`, and any loop parameter (`hold_s`, `led_low_v`, …) the tree sets. |
| `max_runs`, `max_total_s` | Caps on the submissions under the plan and on the sum of their cost estimates (section 7). |
| `expires_s` | How long the approval lasts, at most 7 days. |

The envelope bounds what the submitted tree states. A parameter the tree does
not set takes its value from its layers (run.toml, last used, the operator's
edits), which the agent cannot change.

| Route | |
|---|---|
| `GET /plans` | Every plan of the session: state (`proposed`, `approved`, `rejected`, `closed`), fingerprint, runs, `spent_s`, `expires_at`, and `open` (whether a submission would be accepted now). |
| `POST /plans` | Proposes a plan; 201 with the plan, 422 when the envelope names an unknown module or parameter or a malformed bound. Journals `PlanProposed`. |
| `GET /plans/{id}` | One plan; 404 when unknown. |
| `POST /plans/{id}/approve` | `{"fingerprint": "…"}`: approves a proposed plan. A fingerprint other than the plan's is a 409, so an approval covers exactly the plan shown. Journals `PlanDecided` with the fingerprint. The console's plan card is the place for it; the MCP endpoint does not offer it. |
| `POST /plans/{id}/reject`, `/close` | `{"reason": "…"}`: rejects a proposed plan, or closes an approved one. Journaled. |

`POST /runs` and `POST /pipelines` take an optional `"plan": "<id>"`. A run
submitted under a plan is refused with the check `plan.envelope` (`invalid`,
422, every reason listed in `data.refusals`) when the plan is not approved,
has expired, has used its runs or its time, or when the tree lies outside the
envelope. Otherwise it is validated and queued like any run: every check of
section 7, the interlocks and park apply. `RunQueued.plan` and the run record's
`plan` name the plan; a run a person starts has `plan: null` and no envelope.

Plans are held in the session's memory: a restart forgets them, so no
approval outlives the process it was given to.

## 8c. `/mcp`; the Model Context Protocol

`POST /mcp` serves an external agent (Claude Code, Codex, any MCP client)
over the protocol's Streamable HTTP transport: one JSON-RPC 2.0 message per
request, one JSON response, with no server-sent stream (`GET /mcp` is 405).
Protocol revisions 2025-06-18, 2025-03-26 and 2024-11-05 are answered; another
requested revision is offered the newest. A notification (no `id`) is 202 with
no body. A request with an `Origin` other than a loopback host is 403, so a web
page cannot drive the bench through the browser at the console.

| Method | Answer |
|---|---|
| `initialize` | Protocol version, `capabilities.tools`, `serverInfo` and `instructions` that tell the agent to read the manual, propose a plan and wait for a person's approval |
| `ping` | `{}` |
| `tools/list` | The tools below, with JSON Schema inputs |
| `tools/call` | `{"content": [{"type": "text", "text": …}], "isError": …}`; arguments checked against the schema first |
| anything else | `-32601` |

| Tool | Effect |
|---|---|
| `get_session`, `get_bench`, `list_modules`, `get_module`, `list_runs`, `get_run`, `get_events`, `get_monitors`, `dry_run`, `read_doc` | The assistant's read-only tools (§8a) |
| `list_plans`, `propose_plan`, `close_plan` | §8b; `propose_plan` journals `PlanProposed` |
| `start_run`, `start_pipeline` | `POST /runs` and `POST /pipelines` with `plan` required; a refusal is `isError` with every invalid and crit check |
| `stop_run` | `POST /runs/{id}/stop` for a run started under a plan; a run a person started is refused |

No tool approves a plan. Approval is `POST /plans/{id}/approve` from the
console (§8b).

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
  it at validate. A missing VISA runtime; `pyvisa` not installed, or installed
  with neither NI-VISA nor pyvisa-py behind it; is the same case four times
  over: scope, bias, led and smu are `unavailable` with the reason and the fix
  (`pip install -e .[rig]`), the DIO lines and the consoles are still tried,
  and the service comes up. A `relay_module_nr` of 0 is refused by
  `config.load_rig` (module 0 is the shutter), and a `RigConfig` built by hand
  that still says so lands as an unavailable relay, not a traceback.
- Extras: the desk needs `service`; the lab PC needs `lab` (`rig` + `service`),
  because `Bench.build_real` imports `pyvisa`. `main()` imports `uvicorn` and
  `app` before it builds the session, so a Python without the extra is told
  which extra and nothing has touched the bench or written a journal header.
- `--host 127.0.0.1` fixed unless overridden with another loopback address
  (`127.0.0.0/8`, `::1`, `localhost`); anything else is refused with exit 2. No
  authentication, which is acceptable only where nobody but this machine
  can connect.
- `scripts\Run Service.bat` / `Run Service (sim).bat` `cd` to the repo root, as
  every other script does, so `--out runs`, `rig.toml` and `run.toml` resolve
  there; the banner prints every path absolute.
- One process per port. `main()` binds and listens on the port before it
  builds the session (`listen_socket`), and a port in use exits 3 with nothing
  opened. On Windows the socket takes `SO_EXCLUSIVEADDRUSE`: uvicorn's own
  socket sets `SO_REUSEADDR`, which there lets a second process bind a port in
  use. uvicorn then serves on the bound socket.
- `POST /service/shutdown` `{"force": bool}` → 202 `{"shutting_down": true,
  "aborts": run_id | null}`. A run holding or waiting for the bench is a 409
  that names it (`run`) unless `force`. The route sets the server's
  `should_exit` after answering; the lifespan's shutdown then aborts, parks and
  closes the journal as Ctrl-C does. An app built without `request_exit` (a
  test client, an embedding) answers 409. Open requests get
  `GRACEFUL_SHUTDOWN_S` (5 s) to end.
- The launcher (`bace/launcher.py`, `BACE.pyw` at the root) is standard
  library only. It probes the port: free (by binding it; on Windows a refused
  local connection takes about 2 s) → start
  `python -m bace.service --rig rig.toml --run run.toml --ui ui` with no
  window, output to `runs/logs/service_<stamp>.log`; held and silent → a
  service still starting, waited for; `GET /session` answers → reuse. The
  console opens in Edge's `--app` window, else the default browser. Failures
  are message boxes quoting the log.
- `--out runs` (folders and the journal); `--ui DIR` mounts static files at
  `/ui` (the front end lives elsewhere; `/` redirects to `/ui/` when mounted,
  else returns a JSON index of the routes).
- Startup loads configs (`config.load_rig/load_run`), builds the bench and
  opens the journal. Assembly sets scope defaults and meter units, wavelength,
  averaging and filtering. `SessionStarted.startup_writes` lists the scope
  setup and meter units/wavelength; it does not enumerate every command.
  A read-back job populates the snapshot before the URL is printed.
- Shutdown aborts the worker and waits for its thread before releasing
  resources. Blocking VISA calls cannot be interrupted. `close()` allows
  `WORKER_JOIN_S` (60 s) beyond the worker shutdown timeout. If it still runs,
  the service records that fact and leaves the bench resources alone; the
  daemon thread ends with the process. The relay line is released without a
  low write, so shutdown cannot move it without the interlock. The shutter
  driver's `close()` does drive its line low.

---

## 10. Tests

- `tests/test_service_wire.py`; payload policy: StepDone decimated on the wire, scalars-only in the journal, verdict attached (the single-trace rail rule, the peaks from the full trace), `StepPhase` live-only, JSON round-trip.
- `tests/test_service_live.py`; the `inferred` overlay event by event on a simulated run, and the session's `/bench` read while a scan is held inside its acquisition: relay amplifier, bias LIVE, LED pulsing, shutter open, `StepPhase` frames with `seq` null and absent from the ring and the journal.
- `tests/test_drivers.py`; the real drivers' `read_state()`/`read_output()` transcripts on a scripted fake IO, and `?`/None when the instrument will not answer; `tests/test_service_rigs.py`; `build_real` reading outputs, levels and the 81150A's polarity off the fakes, a bench with no VISA coming up with the four roles unavailable, the relay move reading both sources first.
- `tests/test_service_journal.py`; append-only, first line, `last_used_params`, `run_index`, `settle_history`, `shot_time_s`, reading older sessions.
- `tests/test_service_worker.py`; bench lock (two jobs serialise), events reach subscribers in order with monotonic seq, `after_shot` keeps the shot and yields `RunAborted(requested)`, `abort` closes the generator and the `finally` parks, `resume` wakes a paused generator, a raising generator → `failed` then `parked`, queue FIFO + cancel.
- `tests/test_service_modules.py`; catalogue params with provenance (defaults → run.toml → last-used → edited → inherited), `build()` produces the right dataclasses (transcript-style assertions on the simulated instruments: LED pulsed at `led_v`, router in `transient()` for bace, the light untouched by `jv`), manual bace with `centre_on_voc` refuses without a V_oc source and accepts the session's jv_bace at the same level, coupling invariant enforced.
- `tests/test_service_pipeline.py`; node paths; every check in the catalogue triggered by a minimal tree; the canonical tree (9 T × 5 levels × jv_bace+bace) resolves to 90 module steps with the right inherited/derived params; cost with and without settle history; `temperature inside illumination` warns.
- `tests/test_service_executor.py`; the canonical tree with 2 T × 2 levels on the simulator: V_oc flows from jv_bace into bace at the same level (assert the bace ran centred on that V_oc: `AxisResolved.voc == jv metrics.voc`), relay positions at each boundary, `NeedsOperator` pauses and `resume` continues with `temperature_k` in the folder name, stop after_shot in the middle leaves `kept < requested` and `NodeDone(outcome="stopped")`, abort discards, folders written under one parent.
- `tests/test_service_api.py` uses FastAPI `TestClient` with the `httpx2`
  transport in the `dev` extra. It covers bench reads and actions, module
  edits/reset, runs and WebSocket events, full data, pipeline validation,
  start/stop, error statuses and the power monitor.

- `tests/test_architecture.py`: `bace.service.pipeline`, `modules`, `journal`, `worker`, `executor` import without `fastapi` (monkeypatch `sys.modules['fastapi'] = None` in a subprocess or check `sys.modules` after import); `experiment/`, `storage/` do not import `service`.

---

## 11. Limitations

Interrupted runs are not resumed after restart. Journal queries recover
records, not instrument execution. The service has no authentication or
operator ownership protocol for concurrent writers. The worker serialises
requests from all clients. Analysis beyond the console's result views is
external. Controller heater range, PID, ramp and loop wiring are read-only
through this service; only the setpoint is written by a temperature node.
