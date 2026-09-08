# The Keithley 2400 console

The instrument's own front panel, in a browser. One instrument, one process,
one port — and **nothing from `bace.service` and nothing from `ui/`**.

```bash
python -m bace.consoles.keithley --sim     # no hardware, no VISA, no extras
python -m bace.consoles.keithley           # GPIB0::24, from rig.toml
```

Then open <http://127.0.0.1:8924/>. Source a voltage or a current, hold a
compliance, switch the output on, watch what comes back. No module, no run, no
folder, and no other instrument — no relay, no shutter, no LED, no scope.

## Why it is a separate program

**One process owns an instrument.** The console and `bace.service` cannot both
hold `GPIB0::24`, and the one that starts second gets a VISA error that reads
like a cable fault. So this is what you run *instead of* the service, when the
Keithley is what you want by hand: a V_oc on a probe station, a compliance
check on a new pixel, a look at a diode before it is worth queueing a run.

That constraint is why the 1918-C has a console on `:8918` and the 331 one on
`:8331` (`docs/service-plan.md`, 既有的独立 console 保持独立). This is the same
shape for the 2400, and `8924` continues the convention: the last two digits
are the instrument's GPIB address, so the port says which instrument answers.

## What it needs

Nothing but the base install. `http.server` rather than FastAPI, so there is
no `service` extra; `--sim` builds `drivers.simulated.SimulatedSourceMeter`, so
there is no VISA backend either and the console comes up on a laptop or in a
container. On a real bench, `pip install -e .[rig]` for pyvisa.

| | |
|---|---|
| `rig.toml` `[sourcemeter]` | the VISA address, and the two **bench ceilings** |
| `run.toml` `[sourcemeter]` | the compliance, NPLC, filter and terminals the panel opens on |

Both are found by name in the working directory, as everything else in this
project finds them. A missing file is a printed warning and the built-in
defaults; a file that is *named* (`--rig`, `--run`) and missing is an error,
because a typo that silently ran the defaults would be a bench nobody chose.

## The four things it is careful about

* **`rig.toml`'s ceilings hold a typed level, not only a compliance.** Nowhere
  else in this project can a level be put straight onto the device — a sweep's
  ends come from a module's parameters, and V_oc and J_sc source zero — so this
  is the one surface where the one number an operator types has to be held
  under something. `max_voltage_compliance_v` bounds a sourced voltage and
  `max_current_compliance_a` a sourced current: they are the bench's statement
  of what the device may see, whichever end of the instrument it arrives from,
  so no third key was invented to hold the same number twice.
* **The output goes off when the console stops.** Ctrl-C, `kill`/`SIGTERM`, a
  closed terminal (`SIGHUP`), Ctrl-Break on Windows, an exception out of the
  server — and the two ways it can fail to start at all: a port that turns out
  to be taken, which is how starting the console twice ends, and a `rig.toml`
  ceiling or `run.toml` default the panel refuses. Both are found *after* the
  instrument is open and possibly already driving, left that way by whoever
  had it before. Each is caught and the source is switched off on the way out,
  the GPIB session with it; a taken port also gets a sentence rather than a
  traceback about a socket.
  `SIGKILL` and Windows' `TerminateProcess` cannot be caught by anything, so
  the output survives those; nothing in software fixes that, and the
  instrument's own OUTPUT key does. A
  browser tab closing is not something to rely on for that. The off is
  *queued*, not waited for in the shutdown path, and the worker drains its
  queue on every way out: Ctrl-C can arrive while a read is in flight, and a
  read here can legally take 80 s. **So the console can take a moment to
  stop, and pressing Ctrl-C again will not hurry it**: the second one is
  absorbed rather than killing the process, because the shutdown it would
  interrupt is the part that switches the source off. When even the budget
  runs out — a VISA call that never returns, which cannot be interrupted
  without writing to the bus from a second thread — the console says so on
  stderr instead of exiting quietly.
* **"The instrument did not say" is not "off".** A `:OUTP?` that times out or
  answers something unrecognised leaves the output *unknown*: the page draws
  neither position, and a function, terminals or sensing change is refused
  until the instrument answers again — those are the changes the interlock
  exists to stop under a live source, and deciding them on the flag this
  process last wrote is deciding them on a guess. A level or a compliance
  still applies, because the one thing that must never be blocked is bringing
  a source down. The simulated SourceMeter has no such query and no front
  panel for anybody to touch, so there its cached flag *is* the truth.
* **The page says when it cannot reach the console.** A stopped console used to
  leave the last answer on screen indefinitely, chip and all, and the state
  that must never be mistaken for is a source the page says is off. The chip
  turns, the controls lock, and the note names the 2400's own OUTPUT key as
  the way out that still works.
* **A tightened compliance is written before the level it limits.** One click
  can do both — 0 V on a 50 mA limit to 5 V on 1 mA is an ordinary thing to
  type — and under a live output those are two separate writes on the bus.
  Level first, the device sees the new level under the old, looser limit until
  the next command lands. So whichever change narrows what the device may see
  goes first: a tightened compliance before the level, a loosened one after it.
* **The output-off is never swallowed, on either side.** The console keeps it
  queued rather than cancelling it; the page sends it even while another
  request is in flight. A source change can legally take 175 s, and a click on
  `off` that is quietly discarded because the page is busy leaves exactly the
  same live source as one the console drops.
* **Every wait is the driver's own budget, not a round number.** NPLC 10 and a
  100-deep filter are both legal on a 2400 and both accepted here, and that
  pair is `100 x 4 x 10 / 50 Hz` = 80 s of integration (four apertures per
  averaged reading, because `:FUNC:CONC ON` measures both and auto-zeroes
  each). So a read job waits `Keithley2400.panel_budget_s` and `read_panel`
  raises the VISA timeout for its own query — a flat 30 s would answer a
  healthy read with "the instrument did not answer", while the read went on
  running and whatever was queued behind it landed afterwards.
* **The output state is asked of the instrument, never remembered.** The
  operator has a hand on the 2400's own OUTPUT key, and a cached flag would
  report a source off while it drives — worse, `apply_panel`'s interlock reads
  the same flag, so it would permit a function or wiring change under a live
  output. Every job that touches the bus re-reads `:OUTP?` first, and the
  display's tick is what keeps it fresh between clicks.
* **A bench ceiling that is not a finite positive number is refused at
  start-up.** TOML accepts `nan`, and every comparison against a NaN is false,
  so such a ceiling would wave through any level on the one surface where a
  number goes straight onto the device. A limit that permits everything is
  worse than no limit, because it looks like one.
* **A request the caller gave up on does not reach the instrument later.** A
  job still waiting its turn is dropped when its `do()` times out; one already
  started cannot be recalled, and nothing here pretends otherwise. **Switching
  the output off is the exception** — dropping that one because the caller
  stopped waiting is a source left driving, so it is never cancelled: a slow
  one answers 202 `pending` and still runs.
* **Shutdown accepts no new instrument work.** `ThreadingHTTPServer` runs each
  request on a daemon thread and `server_close()` does not wait for the ones
  already accepted, so a straggler could otherwise queue an output-*on* behind
  the shutdown's off. Closing seals the bus first, drops what had not started,
  and makes the off the last thing that runs.
* **Every answer carries the panel, including the failures.** A 500 that left
  the caller unable to see whether the source is live is worse than the error
  it is reporting — and an output-on whose first reading fails is a 200 with
  `output: true` and the read error recorded, because the ON has landed and
  the reading was only a nicety.
* **The display is dashes unless there is something to display**, and it always
  says which of the three reasons it is. With the output off a `:READ?` still
  answers, from a source disconnected inside the instrument, and the near-zero
  it returns looks exactly like a measurement of a dead device.
* **One thread touches the instrument.** VISA blocks and its sessions are not
  thread-safe, and `ThreadingHTTPServer` gives every request a thread of its
  own — so each one submits a job to the panel's single worker and waits. The
  free-running display is what that worker does *between* jobs, so a click
  never waits behind a reading and a reading never lands in the middle of a
  click.

Under `--sim` the relay is put on the SourceMeter and the cell is lit, because
there is no relay on this console to move and no lamp to switch; `--sim-dark`
gives the unlit case. Every simulated number is a model of a solar cell and the
page says so in the bar — a simulated V_oc must never be mistaken for a
measured one.

## The API

Six routes, and the page. Every one answers the **whole panel**, because a
panel is a state and a client that had to merge four answers into one screen
would be four chances to draw a bench that never existed.

| | |
|---|---|
| `GET /` | the page: one file, no build step, nothing fetched from a network |
| `GET /api/state` | the panel, the last reading, the display's counters |
| `POST /api/source` | any of `function level current_compliance_a voltage_compliance_v nplc averaging terminals four_wire source_range` — all optional, merged onto the panel the instrument already holds, so moving a level is a body of one key |
| `POST /api/output` | `{"on": true}` / `{"on": false}` |
| `POST /api/read` | one reading now |
| `POST /api/poll` | `{"interval_s": 1.0}`, or `null` to stop the display |
| `POST /api/errors` | drain the 2400's own `:SYST:ERR?` queue |

A refusal is a **409 with a sentence**: `{"error": …, "level": "warn"|"crit"}`
— what happened and what to do, never a bare status code. A value the console
cannot read is a 422 naming the field; no instrument at all (or a console
shutting down) is a 503 carrying why; an output-off queued behind a read that
has not finished is a **202** saying so, because it is coming. Every one of
them carries the panel state alongside the error.

Every POST must declare `Content-Type: application/json`, and that is a safety
check rather than a parser's convenience — see below.

```bash
J="content-type: application/json"
curl -s localhost:8924/api/state | python -m json.tool
curl -s -H "$J" localhost:8924/api/source -d '{"function":"I","level":0}'
curl -s -H "$J" localhost:8924/api/output -d '{"on":true}'
curl -s -H "$J" -X POST localhost:8924/api/read \
  | python -c 'import json,sys; print(json.load(sys.stdin)["reading"])'
```

### Loopback is not a wall

Binding to `127.0.0.1` keeps a *network* out. It does not keep a **browser**
out: any page the operator has open can `fetch("http://127.0.0.1:8924/api/output",
{method: "POST", mode: "no-cors", body: '{"on": true}'})`, and the browser
sends it — to a fixed port, on a machine whose console is a fixed program. The
page cannot read the answer and does not need to; the side effect is a source
driving a device. That is cross-site request forgery against an instrument, and
it worked here until it was reviewed.

Two independent checks, because neither is enough alone:

* **`Content-Type: application/json` is required on every POST.** A `no-cors`
  request may only set one of the simple types (`text/plain`,
  `application/x-www-form-urlencoded`, `multipart/form-data`), so it is refused;
  a cross-origin fetch that sets the header properly triggers a CORS preflight
  this console does not answer.
* **`Origin`, when the request carries one, must be loopback.** A browser sends
  it on every cross-site POST; a non-browser client sends none.

Either failing is a **403 with no state**: a page that may not ask is not told
what the bench is doing either.

## Copying it for another instrument

This is the smallest complete example of the shape, and it is three files:

| | |
|---|---|
| `panel.py` | the instrument and the one thread allowed to touch it. No HTTP. The policy a panel needs on top of a driver: the ceilings, what is refused and why, and what the display does between jobs |
| `server.py` | `http.server`, six routes, and the three kinds of "no". Knows nothing about the 2400 |
| `page.html` | one file — inline CSS and JS, system fonts, nothing fetched. Draws `GET /api/state` and posts back |

For another instrument, `panel.py` is what changes: give it a driver from
`bace/drivers/`, decide what the ceilings are and what the display reads, and
keep the worker exactly as it is. `server.py` and the page follow the panel's
state object rather than the instrument, so most of both survives the copy.

The one rule not to lose in the copying: **the driver keeps the instrument's
state, and the console keeps none of it.** `panel` in the state object is
`Keithley2400.panel` — the driver's own record of what it configured, which
`*RST` clears there — and not a copy this program is keeping in step. A console
that cached it would go on drawing a source the instrument had forgotten.
