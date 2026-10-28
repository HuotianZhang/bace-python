# bace.service; the bench over HTTP and WebSocket

The service owns the bench in one process on the lab PC. HTTP and WebSocket
expose the engine's event generators. One worker serialises instrument jobs;
the service broadcasts events, writes an append-only journal and runs pipelines.
The browser console is the operator interface. This document covers direct
API use.

[Service contract](../../docs/service-contract.md) defines request and response
shapes. [The manual](../../docs/manual/README.md) explains operation. Paths
with a repository directory prefix are relative to the repository root.

## Running it

Needs the `service` extra (`fastapi`, `uvicorn`, `websockets`):
`pip install -e .[service]`. The lab PC runs on the real rig and needs the VISA stack as well:
`pip install -e .[lab]` (`rig` + `service`). A Python without the extra is
told so at once, before anything touches the bench; a bench with no VISA
runtime at all comes up with its four VISA roles marked unavailable and the
fix named, rather than not at all.

```
:: at the desk, no instruments: the simulated rig, every settle a no-op
"C:/WPy64-31370/python/python.exe" -m bace.service --sim --fast

:: the lab PC, the real rig
py -3 -m bace.service --rig rig.toml --run run.toml
```

Or double-click `scripts\Run Service.bat` / `scripts\Run Service (sim).bat`;
each `cd`s to the repo root first so `rig.toml` and `run.toml` are found by name.
The operator's entry point is `BACE.pyw` at the root (`bace/launcher.py`): it
starts the service without a window, logging to `runs/logs/`, unless one
already answers on the port, and opens the console.

| flag | default | what |
|---|---|---|
| `--sim` | off | build the rig from `drivers.simulated`; `pyvisa` is never imported |
| `--fast` | off | with `--sim` only: every sleep in the service's generators is a no-op, so waits take no wall time. Refused on the real rig, because there it would measure before the device has settled, with no symptom in the data |
| `--port` | `8900` | |
| `--host` | `127.0.0.1` | there is no auth, so only a loopback address (`127.0.0.0/8`, `::1`, `localhost`) is accepted; anything else is refused |
| `--out` | `runs` | run folders and the journal |
| `--rig`, `--run` | found by name | `rig.toml` / `run.toml` in the working directory or beside the package, as `bace.bench` finds them. Absent: the built-in defaults, with a printed warning. Named and missing: an error, because a typo in `--run` that silently ran the defaults would be a recipe nobody chose |
| `--ui DIR` | none | serve a directory of static files at `/ui`; `/` then redirects there |
| `--seed` | `0` | the simulator's seed |

Startup loads the configs, builds the rig, opens the journal, runs one
read-back job and prints where everything is:

```
bace service  sim fast  session 20260902_080637
  rig.toml   D:\BACE\bace-python\rig.toml
  run.toml   D:\BACE\bace-python\run.toml
  out        D:\BACE\bace-python\runs
  journal    D:\BACE\bace-python\runs\journal\20260902_080637.jsonl
  url        http://127.0.0.1:8900/
```

A missing instrument on the real rig is not fatal: it is printed as
`unavailable`, `/bench` says so, the modules that need it report `needs`, and
a run on it is refused at validate with the check `bench.instrument`. Assembly sets scope defaults and meter units, wavelength, averaging and
filtering. The printed `startup write` lines and `SessionStarted` enumerate
scope setup and meter units/wavelength, not each instrument command.

`GET /` lists every route with a
one-line description; `/docs` is FastAPI's interactive page. Ctrl-C, or
`POST /service/shutdown` (the console's `shut down`), aborts the job in
flight, waits for the worker thread to end, parks the bench (bias and SMU off,
shutter shut, LED unchanged) and closes the journal. The port is bound before the bench is
built, so a second service on it exits before it opens an instrument.

On the real rig, read-back queries output states, 81150A polarity, generator
levels and frequency. Relay actions query both sources before moving.
While a run holds the worker, the snapshot overlays states implied by its
events on the Start read-back. These carry `how: "inferred"` and appear in
`inferred`; they are not fresh instrument queries.

### Output paths

| path | what |
|---|---|
| `<out>/journal/<session_id>.jsonl` | one JSON line per event, append-only, flushed per line. First line `SessionStarted`. The history queries (last-used parameters, settle times, shot time, the run index) read this session's file and at most twenty session files including this one |
| `<out>/<folder>/` | a manual run records directly under `<out>/`, exactly as `tools/scan.py` does (`RunRecorder`: HDF5 `bace-run/3` plus the legacy `.dat` set; `JVRecorder` for J-V) |
| `<out>/<name>_<stamp>/<per-module folder>/` | a pipeline run: one parent, one folder per module run, named with temperature, LED level and V_oc |
| `<out>/recipes/<name>.json` | tree and bench values for its modules, saved through `POST /pipelines/save` |
| `<out>/bench/<stem>.json` | whole-catalogue values saved through `POST /bench/save`; `GET /bench/saved` lists and `DELETE /bench/saved/{name}` deletes |

The journal holds scalars and verdicts, never traces; the traces are in the
run folders. `GET /runs/{id}/data` serves full-precision arrays for the last 20
runs from memory; older runs answer 404 with the folder path.

## The endpoints, by tab

The console's four tabs are bench, pipeline, results and rig.
Shapes are in `docs/service-contract.md` (section numbers below).

| tab | endpoint | what |
|---|---|---|
| bench | `GET /bench` | the cached read-back, the run in progress, the queue, the chain, the rig values. Touches no instrument (§4) |
| bench | `POST /bench/read` | a read-back job; 409 while a run is active. Waits by default; `?wait=false` returns the job id (§4) |
| bench | `POST /bench/actions/{name}` | one explicit action, journaled `by="hand"` with `result.before` (what the read-back held before it, so the log can say `NORM → INV`), followed by a read-back. `park`, `set-33220a-pol-inv/-norm`, `arm-81150a-ext`, `set-led-pulse`, `set-led-dc`, `led-off`, `bias-off`, `smu-off`, `shutter-open/-shut`, `relay-to-sourcemeter/-amplifier`, `read-power`. 409 while a run is active, except `park`, which answers `pending: true` rather than a 504 when the aborted run is inside a long instrument call (§4) |
| bench | `GET /modules`, `GET /modules/{m}` | the catalogue; `jv`, `jv_bace`, `bace`, `light`, `power`, `temperature`, `park`, `wait`, `note`; each parameter with its value and where it came from: `default` → `run.toml` → `last-used` → `edited` → `inherited`/`derived` (§5) |
| bench | `PUT /modules/{m}/params`, `POST /modules/{m}/params/reset` | set the `edited` layer (a `null` value resets that parameter); drop the whole layer (§5) |
| bench | `POST /runs` | run one module: `{"module", "params", "name"}`. Validated like a pipeline; 422 with the checks, else 202 queued (§6) |
| bench | `POST /runs/{id}/stop` | `{"mode": "after_shot" \| "abort"}` (§6, and below) |
| bench | `GET /runs/{id}/data` | the arrays for the card's charts: `axis`, `values`, `q_mean`, `q_std`, `q_all`, `time_s`, `light`, `dark`, `photo`, `last_shot`; for J-V, `curves`. `?node=` for a pipeline run (§6) |
| bench | `WS /events`, `GET /events?since=` | every event: `Hello` first, replay from `?since=<seq>`, then live. The HTTP form returns the same ring for a client without a socket. `StepPhase` frames (where inside a shot the run is: `levels`, `light settle`, `acquire light`, …) are live-only, with `seq` null, and never in a replay or the journal (§3, §6) |
| bench | `POST /monitors/power`, `DELETE /monitors/power`, `POST /monitors/temperature`, `DELETE /monitors/temperature`, `GET /monitors` | the observers: the power monitor reads the 1918-C (USB or HTTP; never the bus, so it reads through a scan; `--power-monitor SECONDS` starts it from boot), the temperature monitor the 331 (on by default at 5 s on a bench that has one, `--temperature-monitor SECONDS` / `--no-temperature-monitor`; 422 only when the bench has no 331 at all; on GPIB it reads only while holding the worker's bus lock and skips the tick otherwise, so a long run leaves the card stale; `skipped` says why) (§8) |
| bench | `GET /monitors/power/history`, `GET /monitors/power/history.csv`, `DELETE /monitors/power/history` | every power-monitor reading the session holds (200 000 at most, kept across monitor restarts), as `[ts, watts, trustworthy]` rows under the frames' own `ts`, or as a CSV download; the console's trace and its export (§8) |
| pipeline | `POST /pipelines/validate` | the Dry run: the checks, the schedule in order, the counters, the cost, and `folder_pattern` (the run's folder carries a stamp taken at submit, so the Dry run names the pattern rather than a folder that will not exist). Touches nothing (§7) |
| pipeline | `POST /pipelines` | Start: a fresh read-back when idle, validate, 422 on `invalid`/`crit`, else 202 (§7) |
| pipeline | `POST /runs/{id}/resume` | answer a `NeedsOperator`: `{"temperature_k", "note"}` (§7, and below) |
| pipeline | `GET /pipelines/last`, `POST /pipelines/save`, `GET /pipelines/saved` | the last validated tree; recipes on disk (§7) |
| results | `GET /runs?session=this\|all\|<id>`, `GET /runs/{id}` | the run index from the journal, and one run's full record: tree, schedule, parameters as executed, node outcomes, folders, error, the measured ETA (`eta`, `cost.finish_at`). A run from an earlier session is answered from the journal (`from: "journal"`: the summary, the tree and the per-node V_oc/level/temperature), which is what the pipeline tab's grey V_oc grid of the previous run reads. The results tab reads these records (§6) |
| rig | `GET /bench` (`instruments`, `chain`, `rig`), `GET /session` | the instruments as read back, the trigger chain against the recipe, the `rig.toml` values in force with their fingerprint; who this process is |

A manual run is a pipeline with one module node, on the same code path, so
everything said about pipelines below applies to a card's Run button too.

## Light

Park disables bias and SMU and shuts the shutter. It leaves the LED running
behind the shutter to preserve thermal steady state. A `bace` leaves pulse
mode; a managed light J–V leaves DC. A plain `jv` leaves the illumination
unchanged. A `light` node may explicitly switch the LED off; managed dark
J–V without a shutter also switches it off. `led-off` is a bench action.
The optical path includes the LED, shutter, 85 m fibre and beam splitter
to the meter and device ([The bench](../../docs/manual/1.2-bench.md)).

After the 33220A goes from DC to pulse a `bace` opens the shutter and waits
for the power meter behind it to read stable, not a fixed time: the meter
is polled every 0.5 s. Completion requires the last three readings to agree within
`led_settle_tolerance` (default 0.02, relative) and at least `led_settle_s`
has passed; past `led_settle_max_s` (default 60 s) it goes on with a
`Notice(warning)` that quotes the last readings. The wait is counted on the
poll clock, so `--fast` is instant. It is announced as `Notice(info, "LED
settled in 4.5 s at 7.81e-05 W (3 readings within 2 %)")` and written to the
file as the read-back `led_power_w` / `led_settle_s`. Without a meter (or
with one that raises, said once) the fixed `led_settle_s` is slept instead. A fixed wait cannot establish stable optical power.

## Run states and the two stop verbs

```
queued → preflight → running ⇄ paused → stopping
             │           │                 │
             └───────────┴─────────────────┴─→ done | stopped | aborted | failed | blocked
                                               → parked (cleanup complete)
queued → cancelled (removed without acquisition)
```

The worker reports the terminal outcome, parks the bench, then emits
`RunStateChanged(parked)`. The run record retains `done`, `stopped`,
`aborted`, `failed` or `blocked`. A queued cancellation touches no instrument.
`blocked` means an invalid or critical Start re-check fails before acquisition.
`GET /bench` reports `idle | preflight | running | paused | stopping`;
`parked` is a cleanup event rather than a resting bench state.

`POST /runs/{id}/stop` takes one of two verbs, and there is deliberately no
third:

- `after_shot`; the shot in flight completes and is kept. Every generator
  polls the flag at its natural boundary (transient: before each step; J-V:
  before each curve; the executor: before each node), yields
  `RunAborted(reason="requested")` and unwinds through its `finally`. The
  state is `stopping` from the moment the stop is accepted, `stopped` when it
  lands. A settle in progress is not cut short: the shot is kept, settle and
  all.
- `abort`; the generator is closed at the next event it yields (a
  blocking VISA call cannot be interrupted; the next yield is the earliest
  honest moment). The shot being acquired is abandoned; a shot that had
  already reported its `StepDone` is kept as it came. A settle or a
  temperature hold returns early. State `aborted`.

Both work while paused. A queued run stopped with either verb is `cancelled`.
`POST /bench/actions/park` while a run is active is the panic button: it
aborts the current run and cancels every queued run before parking,
because a person who clicks park requires park before another run starts.

A stopped or aborted run is a normal run: `kept` of `requested` in the record,
the folder on disk, the counts in `GET /runs/{id}` even when the abort closed
the generator before it could report them.

## Temperature: the 331, or `NeedsOperator`

A temperature node (a loop iteration, or the `temperature` module) goes one
of two ways, decided by whether `Rig.temperature` holds a controller
(`bace/service/temperature.py`, one helper for both):

The 331 answered at start-up. Normally that means this process opened
`[temperature] address` itself and owns the GPIB session; naming
`[temperature] console` instead hands ownership to the 331 console and the service asks it over HTTP. Either way the node settles on its own: the controller is
read (the cryostat as found is the first `TemperatureRead`), the setpoint is
written to it, it is polled every 5 s and every reading goes out as
`TemperatureRead`, and once
the reading has held inside `tolerance_k` for `hold_s` (no ramp still walking
the setpoint) the node yields `Verdict(ok, "temperature.settled")` and the
*last measured* kelvin becomes the subtree's temperature; for a loop
iteration the nodes under it, for the `temperature` module the nodes after
it. Every node is marked in the audit trail (the journal here, the console's
own log when a console owns the instrument). The safety rules hold either
way: a setpoint above the 350 K ceiling is refused in the driver's words
(`Verdict(crit, "temperature.refused")`, never clamped), the heater range and
ramp are whatever it holds (a range of 0 with the setpoint above the reading
is said once, `Verdict(warn, "temperature.heater-off")`, never changed). A
`timeout_s` without reaching the band, an instrument that answers nothing
(`reason = "silent"`), or the bus itself failing (`reason = "unreachable"` ;
check the instrument and the GPIB cable, or start the console if rig.toml
names one; a write that did not answer in time is read back first, and a
setpoint that landed anyway settles with a notice) gives `Verdict(warn,
"temperature.timeout")` and the pause below with `what = "temperature
timeout"`; the run never measures at a temperature it did not reach; the
operator accepts the reading or stops.

Under `--sim` there is no instrument to answer, so whether this bench has a
cryostat is a scenario you choose: any non-empty `[temperature] console`
attaches a simulated 331 that converges in a few dozen polls, and the default
(none) is the pause path below. `--sim` deliberately does *not* follow
`address`, because the pause is the case a console has to handle well.

No 331 on the bench, or it did not answer. A temperature loop still runs
the whole tree; at each temperature the executor yields

```
NeedsOperator(what="temperature", node_path="T=250K",
              detail={"setpoint_k": 250.0, "tolerance_k": 0.2, "hold_s": 60.0,
                      "timeout_s": 1800.0, "index": 5, "count": 9})
```

and the run is `paused`. Set the cryostat by hand, wait for it, then

```
POST /runs/{id}/resume   {"temperature_k": 250.1, "note": "set by hand"}
```

`temperature_k` becomes the subtree's temperature; folder names, metadata ;
and both fields are journaled as `OperatorResumed`. The metadata also records
*how* the number is obtained (`temperature_how`/`temperature_source`,
contract §7): typed here, settled by the console, or a setpoint nothing ever
confirmed. The folder name cannot say; `250K` is `250K`; so the file does.
`hold_s` is honoured after
the resume. The seconds between the pause and the resume go to the journal,
keyed by setpoint, and become the settle time the Dry run quotes for that
temperature next time; a temperature never settled before shows `—`, never an
invented number.

While any pause lasts; a loop's or the `temperature` module's; an attached
controller is polled every 5 s and each `TemperatureRead` goes out live, so the
reading reaches the journal and the socket while the run is still paused.
The pause is the fallback and timeout path, and the cost model reads the console's `temperature.settled` verdicts
beside the operator's pauses (a timeout pause counts the console's time before
it too; a refused setpoint's pause counts for nothing). The Dry run's
`needs_operator` and the `temperature.not-wired` check follow the last bench
read-back: `ok` only with the controller attached and `connected` true; before the first read-back, or with a console unavailable at read-back, they say a pause and the run decides again at the node.

A resume outside an open pause returns 409 and drops its detail. Duplicate
requests cannot answer a later pause. Stop and abort work while paused.

## The verdict rule

Every check is a `Verdict(level, code, text, node_path, data)` with a stable
`code` (the catalogue is contract §7); `data` carries the numbers the text
quotes and, where a bench action would fix it, `data.fix` names that action.
Validation levels:

| level | means | blocks Start |
|---|---|---|
| `invalid` | the tree cannot run as written: shape, a `bace` with `centre_on_voc` (on a swept vpre; on any other axis the flag is inert, `core.axis.voc_flags`) and no V_oc source in scope, a V_oc measured at a different LED level than the pulse will use, LED levels `LedDrive` refuses, axis geometry `Axis` refuses | yes |
| `crit` | hardware safety only: compliance above the bench ceiling, a bias or SMU output live at Start, the relay interlock | yes |
| `warn` | states the evidence and nothing more: the 33220A reads `NORM`, the 81150A is not armed `EXT`, a temperature loop will pause, the power console is silent, a run over eight hours. `data.fix` names the action the operator may click | no |
| `info`, `ok` | the record | no |

A `fix` names `POST /bench/actions/{name}`, journaled by hand. The BACE
builder also writes LED polarity to match external trigger slope at Start.
The bench card can warn on NORM while an externally armed tree reports info
because that run sets its polarity. The same discipline
applies to the per-shot `verdict` attached to every `StepDone` on the wire
(`rail_light`, `rail_dark`, `rail_run_light`, `rail_run_dark`,
`shared_extreme`, `peak_light_a`, `peak_dark_a`, `spike_lag_ns`,
`edge_light_ns`, `edge_dark_ns`, `averages_light`, `averages_dark`,
`sync_edge_light_ns`, `sync_edge_dark_ns`, `sync_lag_ns`, `level`, `text`): it says
what the digitiser saw and interprets nothing. The rail rule is: a run of seven or more identical samples on one trace's
extreme is the digitiser's rail (`warn`, the charge is meaningless); an
extreme repeated more than eight times is a vertical range that is probably too small
(`warn`); light and dark sharing an extreme is stated and judged as nothing,
because the displacement spike dominates both traces and good data shares
one. The peaks are taken from the full trace before decimation. Timing warns
when absolute spike lag exceeds 0.25 ns with an edge above 8 ns, or when
absolute Sync lag exceeds 0.1 ns independently. Lag alone does not prove
jitter; missing Sync remains explicit. The contract §3 gives the full rule.

The check `bench.instrument` refuses a tree whose module needs an instrument
the last read-back listed as unavailable; an unplugged Keithley is a 422 at
Start, not a run that fails at preflight. `vpre_on_voc` on a `bace` pins the
prebias as an offset from the V_oc in scope (TDCF at V_oc) and needs a V_oc
source exactly as `centre_on_voc` does.

## Limitations

The results tab displays run records and grids; it does not resume interrupted
runs. A restart creates another session. Journal queries expose earlier runs;
their full data remain in run folders.

Controller heater range, PID, ramp and loop wiring stay front-panel settings.
The service reads them and writes only temperature setpoints. Missing or
unreachable controllers lead to `NeedsOperator`.

The service has no authentication or writer ownership protocol. It accepts
loopback connections and serialises jobs from all clients. `--ui` serves the
console files.

## Driving it by hand

`curl` is at `C:\Windows\System32\curl.exe`. The bodies below are written for
a POSIX shell (Git Bash); in `cmd.exe` escape the quotes,
`-d "{\"module\": \"jv\"}"`, or put the body in a file and pass
`-d @body.json`. In PowerShell call `curl.exe`, because `curl` there is an alias.
The `httpx` forms need `pip install httpx`. The tests use `httpx2`, Starlette's `TestClient` transport, from the `dev` extra.

Watch the stream in a second window while you do any of this. The
`websockets` package ships an interactive client:

```
python -m websockets ws://127.0.0.1:8900/events
```

or, to print one line per frame:

```python
import json
from websockets.sync.client import connect

with connect("ws://127.0.0.1:8900/events?since=0", max_size=None) as ws:
    for raw in ws:
        f = json.loads(raw)
        print(f["seq"], f["type"], f.get("run_id"), f.get("node_path"), str(f["data"])[:80])
```

The first frame is `Hello` with `data.seq` (the last seq the ring holds) and
`data.bench` (the same object `GET /bench` returns); reconnect with
`?since=<seq>` to replay what you missed.

A client more than 1000 frames behind is dropped: it receives a `Notice`
whose `data.since` is the last numbered frame delivered to that client, then the socket
closes with 1008. Reconnect with `?since=<that>` and the ring replays every
numbered frame from there (decimated traces of the last 200 shots, older
shots as scalars). A long `--sim --fast` scan can outrun a subscriber. Clients reconnect on
1008. `StepPhase` and `JVPoint` are live-only and are not queued for a
subscriber more than fifty frames behind. The retained shot traces are
decimated; `/runs/{id}/data` supplies full arrays.

### 1. Fix the chain

The 33220A must read `INV` for the Sync's rising edge to mean light off; the
bench card warns, names the fix, and waits for the click.

```
curl http://127.0.0.1:8900/bench
    ...
    "chain": {"ok": 3, "total": 4, "items": [
      {"key": "led_polarity", "label": "33220A POL", "value": "NORM", "expected": "INV",
       "level": "warn", "fix": "set-33220a-pol-inv", "text": "NORM makes the Sync's rising edge ..."},
      ...

curl -X POST http://127.0.0.1:8900/bench/actions/set-33220a-pol-inv
    202 {"job": "action-2", "name": "set-33220a-pol-inv", "result": {"polarity": "INV"},
         "read_at": ..., "bench": {... "chain": {"ok": 4, "total": 4, ...}}}
```

The action is refused with 409 while the LED output is on (the fix is made
with the LED off; `led-off` first). On the stream: `BenchAction(name,
args, result, by="hand")`, then the read-back's `Verdict`s.

```python
import httpx
c = httpx.Client(base_url="http://127.0.0.1:8900", timeout=30)
chain = c.get("/bench").json()["chain"]
for item in chain["items"]:
    if item["level"] != "ok" and item["fix"]:
        print(item["label"], item["value"], "->", item["expected"], "fix:", item["fix"])
r = c.post("/bench/actions/set-33220a-pol-inv")
print(r.status_code, r.json()["result"])
```

### 2. Run `jv`, read the result

```
curl -X POST http://127.0.0.1:8900/runs -H "Content-Type: application/json" \
     -d '{"module": "jv"}'
    202 {"run_id": "20260902_080637-001", "state": "queued", "position": 0, "checks": [...], "cost": {...}}
```

On the stream, in order: `RunQueued`, `RunStateChanged` (`queued`, then
`preflight`), the chain `Verdict`s from the read-back Start takes,
`RunStateChanged(running)`, then with `node_path: "jv"`: `NodeStarted`,
`JVStarted`, `InstrumentState`, a `JVPoint` per point as it is read (live
only, `seq` null, like `StepPhase`), `JVCurveDone` (the curve with its `metrics`:
`voc`, `jsc`, `fill_factor`, `p_max`, `v_mpp`, `j_mpp`; `voc` is `null` for a
curve read as dark), `Progress`, `JVFinished`, `NodeDone(outcome="ok")`; then
`RunStateChanged(done)` and `RunStateChanged(parked)`. Then

```
curl http://127.0.0.1:8900/runs/20260902_080637-001
    {"run_id": ..., "state": "done", "kind": "manual", "module": "jv", "kept": 1, "requested": 1,
     "folders": ["...\\runs\\290K_20260902_080637"], "node_outcomes": {...}, "params_as_executed": {...},
     "verdicts": [...], "chain_at_start": {...}, "error": null, ...}
curl http://127.0.0.1:8900/runs/20260902_080637-001/data
    {"curves": [{"label": "as found dark", "dark": true, "direction": ..., "led_level_v": ...,
                 "intensity_w": ..., "illumination": {"lit": false, "shutter": "shut", ...},
                 "voltage": [...], "current": [...], "density": [...], "metrics": {...}}]}
```

`voltage` is V, `current` is A as the instrument reported it, and `density` is
mA/cm²; the unit J–V is read in everywhere in this project, applied once
where the pixel area is (`experiment.jv.current_density`) rather than by each
consumer. With `pixel_area_cm2 = 0` there is no area and so no density at all:
the field is `null` and the answer is amps. `metrics.jsc` stays in A; it is
interpolated from the current, not from the density.

`jv` sets no light: it sweeps under whatever it finds and the `label` says
what it read (`as found dark`, `as found 1.020 V`, or `as found unknown` when
the bench cannot say). To make it light or dark first, use the bench actions ;
they are worker jobs without run-level cleanup, so the setting persists:

```
curl -X POST http://127.0.0.1:8900/bench/actions/set-led-dc \
     -H "Content-Type: application/json" -d '{"level": 1.02}'
curl -X POST http://127.0.0.1:8900/bench/actions/shutter-open
```

Inside a pipeline the same thing is a `light` node before the step that needs
it (`{"module": "light", "params": {"shutter": "open", "led_mode": "dc",
"led_v": 1.02}}`); as a run on its own it is refused, because the park that
ends every run would undo it.

To stop a longer run: `curl -X POST .../runs/<id>/stop -d '{"mode": "after_shot"}'`
(with the JSON header) answers `{"run_id", "state": "stopping", "mode"}`.

```python
import time
run_id = c.post("/runs", json={"module": "jv"}).json()["run_id"]
while c.get(f"/runs/{run_id}").json()["state"] not in ("done", "stopped", "aborted", "failed", "blocked"):
    time.sleep(0.5)
rec = c.get(f"/runs/{run_id}").json()
print(rec["state"], rec["kept"], "of", rec["requested"], rec["folders"])
curves = c.get(f"/runs/{run_id}/data").json()["curves"]
print(curves[0]["label"], curves[0]["metrics"])
```

Run `jv_bace` the same way and the bench rail (`GET /bench`,
`instruments.voc`) shows the V_oc it measured, with the LED level and the run
it came from; a `bace` with `centre_on_voc` posted before that is refused with
422 and the check `voc.source` (`"none · run jv_bace first"`).

### 3. Validate a tree (the Dry run), then start it

The canonical tree; nine temperatures, five LED levels, `jv_bace` then `bace`
at each; as the console would post it. Save it as `tree.json`:

```json
{"tree": {
  "kind": "loop", "loop": "temperature", "label": "T",
  "values_k": [295, 290, 280, 270, 260, 250, 240, 230, 220],
  "tolerance_k": 0.2, "hold_s": 60, "timeout_s": 1800,
  "children": [
    {"kind": "loop", "loop": "illumination",
     "led_start_v": 1.010, "led_stop_v": 1.030, "led_step_v": 0.005,
     "led_low_v": 0.4, "led_settle_s": 2.0,
     "children": [
       {"kind": "module", "module": "jv_bace", "params": {}},
       {"kind": "module", "module": "bace", "params": {"n_loops": 100, "centre_on_voc": true}}
     ]}
  ]},
 "name": "canonical"}
```

```
curl -X POST http://127.0.0.1:8900/pipelines/validate -H "Content-Type: application/json" -d @tree.json
    200 {"valid": true,
         "checks": [{"level": "warn", "code": "chain.bias-arm", "node_path": "",
                     "text": "81150A ARM reads IMM: IMM free-runs: the collection pulse sits at a random phase ...",
                     "data": {"value": "IMM", "expected": "EXT", "fix": "arm-81150a-ext"}},
                    {"level": "warn", "code": "temperature.not-wired", "text": "...", "node_path": "", "data": {...}},
                    {"level": "info", "code": "trigger.auto", ...}, {"level": "ok", "code": "tree.shape", ...}, ...],
         "schedule": [{"node_path": "T=295K", "kind": "loop-enter", "needs_operator": true, ...},
                      {"node_path": "T=295K/led=1.010V/jv_bace", "kind": "module", "relay": "dc", ...},
                      {"node_path": "T=295K/led=1.010V/bace", "kind": "module", "relay": "transient",
                       "params": {"voc": {"value": ..., "source": "derived", "detail": "jv_bace T=295K/led=1.010V/jv_bace"}, ...}}, ...],
         "counters": {"temperatures": 9, "levels": 5, "modules": 90, "shots": ...},
         "cost": {"total_s": ..., "lower_bound": true, "per_temperature": [{"setpoint_k": 295, "settle_s": null, ...}, ...],
                  "t_shot_s": 0.8, "t_shot_source": "default"},
         "node_paths": [...], "folder": "...\\runs\\canonical"}
```

`schedule` is exactly what the executor will run, in order; the service
never re-derives structure. `lower_bound: true` and `settle_s: null` say no
settle time has been journaled for those temperatures yet; after one pipeline
they are the medians of what the operator took. The Dry run touches no instrument.

```
curl -X POST http://127.0.0.1:8900/pipelines -H "Content-Type: application/json" -d @tree.json
    202 {"run_id": "20260902_080637-004", "state": "queued", "checks": [...], "cost": {...}, "folder": ...}
```

Without a controller, the run pauses at `T=295K` with `NeedsOperator`.
Set the cryostat, then

```
curl -X POST http://127.0.0.1:8900/runs/20260902_080637-004/resume \
     -H "Content-Type: application/json" -d '{"temperature_k": 295.2, "note": "set by hand"}'
    202 {"run_id": "20260902_080637-004", "state": "paused"}
```

and again at each of the other eight. Every node's events carry its
`node_path` (`T=295K/led=1.010V/bace`); `AxisResolved.voc` on each `bace`
equals the `metrics.voc` of the `jv_bace` just before it, which is binding 2
doing its job.

```python
tree = json.load(open("tree.json"))
v = c.post("/pipelines/validate", json=tree).json()
print(v["valid"], v["counters"], [x["code"] for x in v["checks"] if x["level"] in ("warn", "crit", "invalid")])
for step in v["schedule"]:
    if step["kind"] == "module":
        print(step["node_path"], step["relay"], step["params"]["led_v"]["source"] if "led_v" in step["params"] else "")
if v["valid"]:
    run_id = c.post("/pipelines", json=tree).json()["run_id"]
    # ... on each NeedsOperator frame from the socket:
    c.post(f"/runs/{run_id}/resume", json={"temperature_k": 295.2, "note": "set by hand"})
```

## Tests

```
"C:/WPy64-31370/python/python.exe" -m pytest -q tests/test_service_*.py
```

The tests use simulated instruments and scripted resources. No test opens
hardware; generator waits use `--fast` semantics where appropriate. `tests/test_service_api.py`
also starts `python -m bace.service --sim --fast` in a subprocess and talks to
it over the port, so the CLI is covered end to end.
