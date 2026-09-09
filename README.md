# bace

The BACE transient charge-extraction measurement, in Python. A port of
`BACE_Mehrdad.vi` (LabVIEW), redesigned around the recovered physics rather than
transliterated. It writes the same `.dat` files the LabVIEW program wrote, byte
for byte, and HDF5 beside them.

The whole program runs with no instruments attached: every driver has a
simulated counterpart, and the test suite runs entirely on the simulator.

## Install

Python 3.11 or later.

```bash
pip install -e .[service,dev]     # engine, HTTP service, tests. No VISA needed
pip install -e .[lab,dev]         # the above plus pyvisa, for the lab PC
```

On Windows, `scripts\Setup.bat` (lab PC) or `scripts\Setup (sim).bat` (no
instruments) does this into `py -3`, the interpreter every other `.bat` there
uses. On Linux, `scripts/setup.sh` builds a `.venv`, installs, runs the suite,
and starts the service once to prove the command below works; `BACE_NO_VENV=1`,
`EXTRAS=rig` and `NO_TEST=1` adjust it.

## Run

**Simulated bench** — the service and the browser console, no hardware:

```bash
python -m bace.service --sim --fast --port 8900 --ui ui/
```

then open `http://127.0.0.1:8900/`. `--fast` makes every settle instantaneous
and is refused without `--sim`. `--seed N` varies the simulated device.

**Real bench** — bring the instruments up in stages before running anything
that drives a sample. `docs/bench-checkout.md` walks each stage; the harness
reads every instrument's error queue after every command.

```bash
python -m bace.bench                              # offline checks and *IDN?
python -m bace.bench --read --configure           # outputs stay off
python -m bace.bench --read --configure --acquire # one acquisition
python -m bace.service --rig rig.toml --run run.toml --ui ui/
```

**One instrument by hand**, without the service. The standalone consoles in
`examples/` import nothing from the package and need only `pyvisa`, and only
for the real instrument:

```bash
python examples/keithley_console/keithley_console.py --sim   # the 2400, :8924
python examples/shutter_console/shutter_console.py           # the shutter
```

The `.bat` files in `scripts/` are double-click versions of the above for the
lab PC. Run everything from the repository root: `rig.toml` and `run.toml` are
found by name in the working directory.

## Layout

```
bace/
  core/         the physics: axis, pulses, processing, illumination
  drivers/      eight Protocol contracts, each with a real and a simulated driver
  experiment/   a run, as a generator of typed events
  storage/      legacy .dat (byte-exact) and HDF5
  bench/        the staged hardware check-out harness
  service/      FastAPI + WebSocket around the engine; owns the instruments
  params.py     where each parameter value came from
ui/             the browser console: ES modules, no build step
tools/          standalone rig scripts
scripts/        .bat entry points, Setup.bat, setup.sh
examples/       demos against the simulator, and the standalone consoles
recipes/        named run.toml variants
tests/          the suite
docs/           documentation; see docs/README.md
bench-archive/  the reference run the numerical regression reproduces
acceptance/     a LabVIEW run and a port run on the same device, side by side
```

`rig.toml` describes the bench (addresses, sense resistor, amplifier gain, sign
convention). `run.toml` describes one measurement. Both are copied into every
output file.

Dependencies point inward: `core/` imports nothing from the outer layers, and
`tests/test_architecture.py` enforces it. `pyvisa` is imported only when a real
rig is built.

## Documentation

| | |
|---|---|
| `introduction.html` | start here: how to run it and how the code is arranged, with diagrams. Open in a browser; no network needed |
| `docs/README.md` | index of everything below |
| `docs/notes.md` | the rig as measured, defects the hardware found, deliberate deviations from the original, open and closed questions |
| `docs/bench-checkout.md` | the staged hardware check-out |
| `docs/service-contract.md` | every route, event and check the service implements |
| `bace/service/README.md` | driving the service by hand |
| `ui/README.md` | the console, file by file |
| `examples/README.md` | the demos and the standalone consoles; `keithley_console/README.md` is the worked example for writing another |
| `CHANGELOG.md` | what changed, by date |

## Status

The measurement engine, the service and the console are built and have been run
on the rig. A run of this port and a run of the LabVIEW program on the same
device eight minutes apart agree on charge to 4 %; the data are in
`docs/history/HANDOVER-2026-09-02.md` and `acceptance/`.

## Tests

```bash
python -m pytest -q
```

CI runs the suite on Ubuntu and Windows on every push. No test touches an
instrument. The console's own suite (`node --test ui/tests/`) and its lint run
from the Python suite when Node and eslint are present, and skip otherwise.
