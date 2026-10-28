# Keithley console

The 2400's own front panel, in a browser. One instrument, one process, one
port.

```
py -3 keithley_console.py --sim        try it with no hardware at all
py -3 keithley_console.py              GPIB0::24, from rig.toml
py -3 keithley_console.py --browser    the page opened for you
```

Then <http://127.0.0.1:8924/>. On Windows, `Run Keithley Console.bat` is the
double-click form. Source a voltage or a current, hold a compliance, switch the
output on, watch what comes back. No module, no run, no folder, and no other
instrument; no relay, no shutter, no LED, no scope. The console writes no measurement files.

## Standalone operation

The folder runs independently of `bace/`. It needs Python 3.11 or later;
simulation uses only the standard library. `smu.py` and `simulated.py` supply
the instrument and device model. The package drivers cover DC measurement
and J–V experiments; the standalone console provides a source panel.
Repository-prefixed paths in this document are relative to the repository root.

`tests/test_keithley_console.py` holds the standalone property down in a
subprocess, because it is the one most easily lost by accident.

## Instrument ownership

One process owns an instrument. This console and the BACE service cannot
both hold `GPIB0::24`, and the one that starts second gets a VISA error that
reads like a cable fault. So this is what you run *instead of* the service,
when the Keithley is what you want by hand: a V_oc on a probe station, a
compliance check on a pixel, a look at a diode before it is worth queueing
a run.

The meter and temperature controller also support separate console owners
on `:8918` and `:8331`. For the 2400,
`8924` continues the convention: the last two digits are the instrument's GPIB
address, so the port says which instrument answers.

## Requirements

The standard library, and `pyvisa` for the real instrument (`pip install
pyvisa`, plus NI-VISA or Keysight's runtime). `--sim` needs neither; no VISA
backend and no instrument; so the page comes up on a laptop or in a
container.

| | |
|---|---|
| `rig.toml` `[sourcemeter]` | the VISA address, and the two bench ceilings |
| `run.toml` `[sourcemeter]` | the compliance, NPLC, filter and terminals the panel opens on |

Both are looked for by name in the folder you start from, then beside
`keithley_console.py`, then in every directory above it; so double-clicking
the `.bat` inside a checkout finds the repository's files two levels up, and on
a bare machine a copy of either can simply sit in this folder or the one
holding it. Whichever file is used is printed at start-up, so "which
ceilings am I on" is answered on the screen.

An unrecognised key in `[sourcemeter]` is an error, not a silent default:
`current_complaince_a = 0.001` would otherwise be dropped and the panel would
open on the built-in 0.05 A, fifty times the current written down. The three
settle times a measurement needs and a panel does not (`settle_jsc_ms` and
friends) are accepted and ignored, because a real `run.toml` carries them. A missing file is a printed warning and the built-in
defaults; a file that is *named* (`--rig`, `--run`) and missing is an error,
because a typo that silently ran the defaults would be a bench nobody chose.

## Instrument rules

* `rig.toml`'s ceilings hold a typed level, not only a compliance. Nowhere
  else in this project can a level be put straight onto the device; a sweep's
  ends come from a module's parameters, and V_oc and J_sc source zero; so this
  is the one surface where the one number an operator types has to be held
  under something. `max_voltage_compliance_v` bounds a sourced voltage and
  `max_current_compliance_a` a sourced current: they are the bench's statement
  of what the device may see, whichever end of the instrument it arrives from,
  so the same limits bound both source levels and compliance.
* The output goes off when the console stops; for the ways of stopping it
  that software can catch. Ctrl-C (`SIGINT`), `kill`/`SIGTERM`, Ctrl-Break on
  Windows (`SIGBREAK`), a closed terminal on Linux and macOS (`SIGHUP`), an
  exception out of the server; and the two ways it can fail to start at all: a port that turns out
  to be taken, which is how starting the console twice ends, and a `rig.toml`
  ceiling or `run.toml` default the panel refuses. Both are found *after* the
  instrument is open and possibly already driving, left that way by whoever
  had it before. Each is caught and queues output-off on the way out,
  the GPIB session with it; a taken port also gets a sentence rather than a
  traceback about a socket.
  What cannot be caught, and where the difference bites: closing the console
  window on Windows. That is `CTRL_CLOSE_EVENT`, which Python does not
  deliver as a signal at all; the process is killed without running its
  shutdown, and the source is left driving. `SIGHUP` covers the same gesture on
  Linux and macOS, which is why the two platforms are named separately above
  rather than the terminal being called closed on both. `SIGKILL`, End Task's
  `TerminateProcess`, a logoff and a power cut are the same story. Nothing in
  software fixes any of them; the instrument's own OUTPUT key does, and
  `Run Keithley Console.bat` says so where a Windows operator will read it. A
  browser tab closing is not something to rely on for that. Output-off is
  queued, not waited for in the shutdown path, and the worker drains its
  queue on every way out: Ctrl-C can arrive while a read is in flight, and a
  read here can legally take 80 s. So the console can take a moment to
  stop, and pressing Ctrl-C again will not hurry it: the second one is
  absorbed rather than killing the process, because the shutdown it would
  interrupt is the part that switches the source off. When even the budget
  runs out; a VISA call that never returns, which cannot be interrupted
  without writing to the bus from a second thread; the console says so on
  stderr instead of exiting quietly.
* "The instrument did not say" is not "off". A `:OUTP?` that times out or
  answers something unrecognised leaves the output *unknown*: the page draws
  neither position, and a function, terminals or sensing change is refused
  while output read-back remains unknown; those are the changes the interlock
  exists to stop under a live source, and deciding them on the flag this
  process last wrote is deciding them on a guess. A level or a compliance
  still applies, because the one thing that must never be blocked is bringing
  a source down. The simulated SourceMeter has no such query and no front
  panel for anybody to touch, so there its cached flag *is* the truth.
* The page marks an unreachable console, locks controls and names the
  instrument's OUTPUT key as the available remedy. A stale screen must not
  claim that the output is off.

* A tightened compliance is written before the level it limits. One click
  can do both; 0 V on a 50 mA limit to 5 V on 1 mA is an ordinary thing to
  type; and under a live output those are two separate writes on the bus.
  Level first, the device sees the requested level under the preceding looser limit
  between commands. So whichever change narrows what the device may see
  goes first: a tightened compliance before the level, a loosened one after it.
* The output-off is never swallowed, on either side. The console keeps it
  queued rather than cancelling it; the page sends it even while another
  request is in flight. A source change can legally take 175 s, and a click on
  `off` that is quietly discarded because the page is busy leaves exactly the
  same live source as one the console drops.
* Every wait uses the driver's calculated budget. NPLC 10 and a
  100-deep filter are both legal on a 2400 and both accepted here, and that
  pair is `100 x 4 x 10 / 50 Hz` = 80 s of integration (four apertures per
  averaged reading, because `:FUNC:CONC ON` measures both and auto-zeroes
  each). So a read job waits `smu.panel_budget_for` and `read_panel`
  raises the VISA timeout for its own query; a flat 30 s would answer a
  healthy read with "the instrument did not answer", while the read went on
  running with queued work still pending.
* The output state is asked of the instrument, never remembered. The
  operator has a hand on the 2400's own OUTPUT key, and a cached flag would
  report a source off while it drives; worse, `apply_panel`'s interlock reads
  the same flag, so it would permit a function or wiring change under a live
  output. Every job that touches the bus re-reads `:OUTP?` first, and the
  display's tick is what keeps it fresh between clicks.
* A bench ceiling that is not a finite positive number is refused at
  start-up. TOML accepts `nan`, and every comparison against a NaN is false,
  so such a ceiling would wave through any level on the one surface where a
  number goes straight onto the device. A limit that permits everything is
  worse than no limit, because it looks like one.
* A request the caller gave up on does not reach the instrument later. A
  job still waiting its turn is dropped when its `do()` times out; one already
  started cannot be recalled, and nothing here pretends otherwise. Switching
  the output off is the exception; dropping that one because the caller
  stopped waiting is a source left driving, so it is never cancelled: a slow
  one answers 202 `pending` and still runs.
* Shutdown refuses further instrument work. `ThreadingHTTPServer` runs each
  request on a daemon thread and `server_close()` does not wait for the ones
  already accepted, so a straggler could otherwise queue an output-*on* behind
  the shutdown's off. Closing seals the bus first, drops what had not started,
  and makes the off the last thing that runs.
* Instrument responses carry the panel, including failures. A 500 that left
  the caller unable to see whether the source is live is worse than the error
  it is reporting An output-on whose first reading fails is a 200 with
  `output: true` and the read error recorded: the instrument is driving even
  though no measured value is available.
* The display is dashes unless there is something to display, and it always
  says which of the three reasons for the absence. With the output off a `:READ?` still
  answers, from a source disconnected inside the instrument, and the near-zero
  it returns looks exactly like a measurement of a dead device.
* One thread touches the instrument. VISA blocks and its sessions are not
  thread-safe, and `ThreadingHTTPServer` gives every request a thread of its
  own; so each one submits a job to the panel's single worker and waits. The
  free-running display is what that worker does *between* jobs, so queued jobs take priority before the next display poll. An in-flight
  reading still finishes before the job; I/O never interleaves.

Under `--sim` the cell is lit, so sourcing 0 A shows a plausible V_oc the
moment the output goes on; `--sim-dark` gives the unlit case. `simulated.py` is
a single-diode model in pure Python; no relay and no shutter, because there is
no bench here to model, and no numpy, so the folder runs under whatever Python
is on the machine. Every simulated number is a model and the page says so
in the bar: a simulated V_oc must never be mistaken for a measured one.

## The API

Six API routes and the page. Instrument API responses carry the panel, because a
panel is a state and a client that had to merge four answers into one screen
would be four chances to draw a bench that never existed.

| | |
|---|---|
| `GET /` | the page: one file, no build step, nothing fetched from a network |
| `GET /api/state` | the panel, the last reading, the display's counters |
| `POST /api/source` | any of `function level current_compliance_a voltage_compliance_v nplc averaging terminals four_wire source_range`; all optional, merged onto the panel the instrument already holds, so moving a level is a body of one key |
| `POST /api/output` | `{"on": true}` / `{"on": false}` |
| `POST /api/read` | one reading |
| `POST /api/poll` | `{"interval_s": 1.0}`, or `null` to stop the display |
| `POST /api/errors` | drain the 2400's own `:SYST:ERR?` queue |

An instrument-state refusal is a 409 with a sentence: `{"error": …, "level": "warn"|"crit"}`
— what happened and what to do, never a bare status code. A value the console
cannot read is a 422 naming the field; no instrument at all (or a console
shutting down) is a 503 carrying why; an output-off queued behind a read that
has not finished is a 202 saying so, because it is coming. Instrument errors carry the panel state. A security refusal is 403 without
state; malformed request handling is separate.

Every POST must declare `Content-Type: application/json`, and that is a safety
check rather than a parser's convenience; see below.

```bash
J="content-type: application/json"
curl -s localhost:8924/api/state | python -m json.tool
curl -s -H "$J" localhost:8924/api/source -d '{"function":"I","level":0}'
curl -s -H "$J" localhost:8924/api/output -d '{"on":true}'
curl -s -H "$J" -X POST localhost:8924/api/read \
  | python -c 'import json,sys; print(json.load(sys.stdin)["reading"])'
```

### Request origin

Binding to `127.0.0.1` keeps a *network* out. It does not keep a browser
out: any page the operator has open can `fetch("http://127.0.0.1:8924/api/output",
{method: "POST", mode: "no-cors", body: '{"on": true}'})`, and the browser
sends it; to a fixed port, on a machine whose console is a fixed program. The
page cannot read the answer and does not need to; the side effect is a source
driving a device. That is cross-site request forgery against an instrument, and
the console therefore checks request type and origin.

Two independent checks, because neither is enough alone:

* `Content-Type: application/json` is required on every POST. A `no-cors`
  request may only set one of the simple types (`text/plain`,
  `application/x-www-form-urlencoded`, `multipart/form-data`), so it is refused;
  a cross-origin fetch that sets the header properly triggers a CORS preflight
  this console does not answer.
* `Origin`, when the request carries one, must be loopback. A browser sends
  it on every cross-site POST; a non-browser client sends none.

Either failing is a 403 with no state: a page that may not ask is not told
what the bench is doing either.

## Copying it for another instrument

This is the smallest complete example of the shape. Six files, and two
of them are about the 2400:

| | |
|---|---|
| `keithley_console.py` | the command line: the two config files, opening the session, and the one warning worth printing when it will not start |
| `panel.py` | the instrument and the one thread allowed to touch it. No HTTP. The policy a panel needs on top of a driver: the ceilings, what is refused and why, and what the display does between jobs |
| `server.py` | `http.server`, six routes, and the three kinds of "no". Knows nothing about the 2400 |
| `page.html` | one file; inline CSS and JS, system fonts, nothing fetched. Draws `GET /api/state` and posts back |
| `smu.py`, `simulated.py` | the instrument and a model of what it is pointed at. instrument-specific driver and model |

For another instrument, swap those last two and edit `panel.py`: decide what
the ceilings are, what the display reads, and which fields cannot be changed
under a live output. Preserve the worker queue, cancellation and shutdown rules in `panel.py`. `server.py` and the
page follow the panel's state object rather than the instrument, so most of
both survives the copy untouched.

The one rule not to lose in the copying: the driver keeps the instrument's
state, and the console keeps none of it. `panel` in the state object is
`Keithley2400.panel`; the driver's own record of what it configured, which
`*RST` clears there; and not a copy this program is keeping in step. A console
that cached it would go on drawing a source the instrument had forgotten.
