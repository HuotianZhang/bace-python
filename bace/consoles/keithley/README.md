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
* **The output goes off when the console stops.** Ctrl-C, a closed terminal, an
  exception out of the server — the source is switched off on the way out. A
  browser tab closing is not something to rely on for that.
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
cannot read is a 422 naming the field; no instrument at all is a 503 carrying
why.

```bash
curl -s localhost:8924/api/state | python -m json.tool
curl -s localhost:8924/api/source -d '{"function":"I","level":0}'
curl -s localhost:8924/api/output -d '{"on":true}'
curl -s localhost:8924/api/read | python -c 'import json,sys; print(json.load(sys.stdin)["reading"])'
```

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
