# Shutter console

A shutter switch in a browser: one page, two buttons, and the Deditec digital
output that moves the optical shutter.

```
py -3 shutter_console.py --sim        try it with no hardware at all
py -3 shutter_console.py --browser    the real line, page opened for you
py -3.11-32 shutter_console.py        where the DELIB build is 32-bit
```

Then <http://127.0.0.1:8910/>. On Windows, `Run Shutter Console.bat` is the
double-click form.

## Standalone on purpose

This folder imports **nothing** from `bace/` — not the drivers, not the config,
and not the service. Copy the folder to a machine with the Deditec module and a
Python 3.11, and it runs; there is nothing else to install. The `ctypes` block
in `shutter_console.py` is therefore a deliberate second copy of what
`bace/drivers/shutter.py` and `bace/drivers/delib.py` do properly — read those
for the full story (the DLL search, the readback, the 32-bit bridge for a
64-bit host). **No measurement depends on this file**, which is what makes the
copy acceptable: it is an example of driving one line, not a driver.

It is also not part of the BACE service, and the service knows nothing about
it. What they share is the hardware.

## Three things to know before using it on the rig

**One process owns the module.** While this console runs, the BACE service,
`tools/shutter.py` and the LabVIEW VI cannot open the Deditec module — and
while any of them holds it, this cannot. `DapiOpenModule` answers 0, which
reads like missing hardware. Stop one before starting the other.

**The line is left where you put it.** Closing the window releases the module
handle without driving anything, so a shutter you opened stays open. Shut it on
the page first if that is what you want.

**Module 1 is the relay, not a shutter.** The same module type on module number
1 moves the device between the amplifier and the SourceMeter, and throwing it
under a live source is the one software mistake on this bench that costs
hardware rather than a dataset. `--module-nr 1` is refused without `--force`,
and `tools/relay.py` is the program for that line — it proves both outputs off
before it moves.

## What the page says

The state is a word — `OPEN`, `SHUT` or `UNKNOWN` — with an amber dot when
light reaches the sample, and a line underneath saying **how it was got**:

| `how` | meaning |
|---|---|
| `readback` | the module was asked and answered |
| `assumed` | this console is repeating what it last sent; this DELIB build has no `DapiDOReadback32` |
| `unknown` | no readback, and nothing has been set yet |

A shutter believed shut and actually open is a dark measurement taken in the
light, so the three are not flattened into a boolean.

## The API behind it

Loopback only, and that is not overridable: there is no authentication and the
thing on the other end moves hardware.

| | |
|---|---|
| `GET /` | the page |
| `GET /api/state` | the state, as above |
| `POST /api/open` | light reaches the sample |
| `POST /api/shut` | the dark state |

Every route answers JSON; a driver error comes back as `503` with its text, so
the page can show it instead of leaving a switch that silently does nothing.

## Files

| | |
|---|---|
| `shutter_console.py` | the DELIB line, the HTTP server, the refusals |
| `page.html` | the switch. Plain HTML, no build step; edit it and reload |
| `Run Shutter Console.bat` | double-click entry point |

`tests/test_shutter_console.py` in the repo root exercises this folder by
path — the API, the two refusals, and the import weight — against a fake line.

## The bench's numbers

Measured on the rig 2026-09-01 by walking all four states of the two lines and
reading the Keithley in each (`python -m bace.bench --dio`), and the defaults at
the top of `shutter_console.py`:

| | |
|---|---|
| module id | 9 |
| shutter | module 0, channel 0 — **1 opens**, 0 shuts |
| relay | module 1 — 1 is the SourceMeter, 0 the amplifier |

They are also `[dio]` in the repo's `rig.toml`. If someone rewires the bench,
both places have to change; this folder deliberately does not read that file,
because then it would need the repo.
