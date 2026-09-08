# bace — the BACE measurement rig in Python

A port of `BACE_Mehrdad.vi` (LabVIEW, 907 VIs) to Python + PyVISA. Redesigned
from the recovered physics rather than transliterated, modular so it can be
extended, and focused on **measurement** — analysis stays downstream.

## Start here

| file | for |
|---|---|
| **`HANDOFF.md`** | the current state, what is built, what is not, and every trap. **Read this first.** |
| `BENCH.md` | how to exercise it against the real rig, stage by stage |
| `bace/service/README.md` | how to run the service and drive it by hand; `docs/service-contract.md` has every shape |
| `docs/history/bace-status.html` | the full narrative with evidence and figures, through 31 August 2026 |
| `rig.toml` / `run.toml` | the bench and the recipe, kept separate |
| `scripts/` | the double-click `.bat` entry points — `Setup.bat` first on Windows, then one per bench stage; `setup.sh` for a Linux checkout |
| `docs/ui-kickoff.md` | where the console work starts, with `docs/ui-rules.md` and `docs/design/` |
| `ui/README.md` | the console itself — how to run it, the fixtures it develops against, and what each milestone owes. `docs/ui-plan.md` is the plan it follows |
| `bace/consoles/keithley/README.md` | the standalone Keithley 2400 panel: one instrument, one process, one port, and the smallest complete example of that shape |

## Installing on Windows

Double-click `scripts\Setup.bat` on the lab PC, or `scripts\Setup (sim).bat` on
a machine with no instruments. Either installs into `py -3`, which is the
interpreter every other `.bat` in that folder calls. By hand it is
`py -3 -m pip install -e .[lab,dev]` (or `.[service,dev]` without the rig),
quoted in PowerShell as `'.[lab,dev]'`. `BENCH.md` has the detail.

## Developing away from the bench (Linux, a container, a cloud session)

The console can be built with no hardware at all: `--sim` assembles the rig from
`bace.drivers.simulated` and never imports pyvisa, so a Linux checkout needs
only the `service` extra.

```bash
bash scripts/setup.sh          # installs .[service,dev], then runs the suite
python -m bace.service --sim --fast --port 8900 --ui ui/
```

`--fast` makes every settle a no-op, so a 20-loop scan takes a second; it is
refused on a real rig, where it would measure before the device had settled with
no symptom in the data. `--seed` varies the simulated device.

Two things that catch people out on a sandbox:

- **`--host` is refused unless it is a loopback address** (`127.0.0.0/8`, `::1`,
  `localhost`). The service has no auth and owns every instrument, so this is not
  a configuration choice. Where a sandbox exposes ports by proxying localhost
  there is nothing to do; where it wants the process on `0.0.0.0`, put the proxy
  in front rather than editing the check.
- **Run from the repo root.** `rig.toml` and `run.toml` are found by name in the
  working directory; from anywhere else the service falls back to the built-in
  defaults, which is a recipe nobody chose. `scripts/setup.sh` and the `.bat`
  files both `cd` there first.

One test fails on the machine this port was written on — a stray 64-bit
`delib64.dll` in System32 makes the DIO backend findable where the test needs it
absent. There is no DELIB on Linux, so expect a clean run there.

`.github/workflows/tests.yml` runs the suite on every push and pull request, on
**ubuntu-latest and windows-latest** — Windows because the lab PC runs
WinPython and this project has already been bitten by the difference (a colon
in `material` built a path that failed there and passed on Linux; the `.dat`
files are byte-exact with CRLF). No runner touches an instrument: the suite is
the simulated rig and recorded transcripts throughout, so the `rig` extra is
not installed.

## Layout

```
bace/           the package
  params.py     where each parameter value came from — default, run.toml,
                last-used, edited, inherited, derived — so the console can say so
  core/         the physics — axis, pulses, process, illumination, simulate.
                Imports nothing outward
  drivers/      seven Protocol contracts; real and simulated instruments satisfy both
  experiment/   the run itself — a synchronous generator of typed events;
                wire.py puts them on the wire
  storage/      byte-exact legacy .dat, plus HDF5
  bench/        the staged hardware harness
  service/      FastAPI + WebSocket around the engine — the bench, the modules,
                the runs, the pipeline tree. bace/service/README.md
  consoles/     standalone per-instrument panels: one instrument, one process,
                one port, no service and no ui/. keithley/ is the first
ui/             the console — plain ES modules, no build step, served by the
                service at /ui. ui/README.md; docs/ui-plan.md is the plan.
                studies/ holds the layout comparisons, kept as record
tools/          standalone rig scripts — scan, bare, lightpower, shutter, relay,
                identify_dio; and the console's fixture recorders
scripts/        the double-click .bat entry points; each cd's to the repo root first
examples/       what to run to see it work — the four demo_*.py against the
                simulator, and standalone folders that share nothing with the
                package but the hardware (shutter_console/). examples/README.md
recipes/        the named recipe variants, passed with --run
tests/          the suite
docs/           the living documents — the service contract, the ui plan and
                rules, the naming. figures/ is the evidence, one measurement a
                page; history/ is accurate for its date and not for today.
                docs/README.md indexes all three
bench-archive/  the 2026-08-07 reference run the regression test reproduces
acceptance/     the 2026-09-02 pair — LabVIEW and the service on the same
                device half an hour apart — and three journals of that rig day
runs/           measurement output. On disk, not in git
```

`rig.toml` and `run.toml` stay at the repo root on purpose: `bace.bench`
discovers them **by name** in the working directory or beside the package
(`checks._find`), and only warns before falling back to built-in defaults if
they are missing. The `.bat` files in `scripts/` therefore `cd` to the root
before doing anything, so every relative path below still resolves.

Everything runs end to end on the simulated rig, with no instruments present:

```
python -m pytest -q                    # 880 passed, and nothing skipped
python -m examples.demo_scan           # a simulated transient scan
python -m bace.bench                   # the offline stages of the bench harness
python -m bace.service --sim --fast    # the service on the simulated rig, http://127.0.0.1:8900/
python -m bace.consoles.keithley --sim # just the Keithley's front panel, http://127.0.0.1:8924/
```

## Status

The measurement half is written, tested, and exercised on the rig: scope
acquisition, auto-range, DIO identity, shutter, and first light on a real device
have all been run. A LabVIEW run and a port run eight minutes apart are set side
by side in `docs/history/HANDOVER-2026-09-02.md`, and a second pair in
`acceptance/20260902_service-vs-labview/`; both carry the numbers, and what to
make of them is the reader's.

`service/` is built (2026-09-02): one process owning the instruments, wrapping
the engine's event generators in HTTP + WebSocket — the bench read-back and its
explicit by-hand actions, the module catalogue with every parameter's
provenance, manual runs and the pipeline tree (validate, dry run, execute), the
two stop verbs, an append-only journal per session, and a temperature node
that settles through the 331 when the bench has one and pauses for the
operator (`NeedsOperator`) when it does not. **It needs no other program
running**: the 1918-C and the 331 drivers live in `bace/drivers/`, and this
process opens both (2026-09-03). Proven on the rig 2026-09-02:
jv_dark → jv_bace → bace end to end on the lab PC, the bace matching a LabVIEW
run fifteen minutes apart within the single-shot scatter. `bace/service/README.md`
says how to run and drive it; `docs/service-contract.md` is what it was built to.

`bace/consoles/` is the other half of that: one instrument, one process, one
port, importing neither the service nor `ui/`. The service owns the *bench* —
every instrument on it, one worker, runs and files — and there are times when
what you want is one instrument by hand and nothing else. `consoles/keithley/`
(2026-09-08) is the first: the 2400's own front panel served from the standard
library, on `:8924` the way the 1918-C's console is on `:8918` and the 331's on
`:8331`. One process owns an instrument, so it is what you run *instead of* the
service, not beside it. Its README is also the worked example for writing the
next one.

`ui/` is under way, milestone by milestone (`docs/ui-plan.md`, and `ui/README.md`
for what each one owes). **M0–M3 are built**: the event layer and the fixtures it
develops against, the pinned rail and the chain strip, the six generated bench
cards — a dark J–V can be run from the screen — and the charts, each in the card
that owns it: the J–V, the transient with its shaded integration window and
running integral, and the timing diagram that draws the shot the form describes
before it is run. **M4 is next**: the live monitor. `HANDOFF.md` §3 carries the user's explicit instruction about the UI's
shape, and `docs/bace-console-round3.html` is the design the service contract
was derived from.
