# bace

BACE measures transient charge extraction in Python. The service owns the
instruments and serves the browser console. Runs write `.dat` files and HDF5.
The simulator supports every driver contract and runs without instruments.
The numerical regression checks `.dat` round trips byte for byte.

The [bench manual](docs/manual/README.md) covers operation, measurement
principle, maintenance and reference.

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

Simulated bench — the service and the browser console, no hardware:

```bash
python -m bace.service --sim --fast --port 8900 --ui ui/
```

then open `http://127.0.0.1:8900/`. `--fast` makes every settle instantaneous
and is refused without `--sim`. `--seed N` varies the simulated device.
The `ask` chip in the bar opens the assistant, which explains the bench and
the console; it runs on the asker's own key, with Claude or any
OpenAI-compatible model.

Real bench — bring the instruments up in stages before running anything
that drives a sample. `docs/bench-checkout.md` describes each stage. The
harness checks error queues after commands except inside timing-sensitive
acquisition blocks.

```bash
python -m bace.bench                              # offline checks and *IDN?
python -m bace.bench --read --configure           # outputs stay off
python -m bace.bench --read --configure --acquire # one acquisition
python -m bace.service --rig rig.toml --run run.toml --ui ui/
```

One instrument by hand, without the service. The standalone consoles in
`examples/` import nothing from the package. The real Keithley needs
`pyvisa` and a VISA backend. The shutter needs DELIB. Simulation needs
only the standard library:

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
  storage/      .dat reader/writer with byte-exact round trips, and HDF5
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
acceptance/     LabVIEW and service runs on the same device, side by side
```

`rig.toml` describes the bench (addresses, sense resistor, amplifier gain, sign
convention). `run.toml` describes one measurement. HDF5 stores both
configurations. The `.dat` files retain their defined format; see
[Data](docs/manual/3.3-data.md).

Dependencies point inward: `core/` imports nothing from the outer layers, and
`tests/test_architecture.py` enforces it. `pyvisa` is imported only when a real
rig is built.

## Documentation

| | |
|---|---|
| `introduction.html` | start here: how to run it and how the code is arranged, with diagrams. Open in a browser; no network needed |
| `docs/README.md` | index of everything below |
| `docs/notes.md` | records the rig as measured and the claims measurement overturned |
| `docs/bench-checkout.md` | the staged hardware check-out |
| `docs/service-contract.md` | every route, event and check the service implements |
| `bace/service/README.md` | driving the service by hand |
| `ui/README.md` | the console, file by file |
| `examples/README.md` | the demos and the standalone consoles; `examples/keithley_console/README.md` is the worked example for writing another |
| `CHANGELOG.md` | what changed, by date |

## Status

The engine, service and console support the real bench and the simulator.
The comparison data in `acceptance/` check the measurement chain.
The matched-device comparison differs by 6 % in peak, 3 % in decay time
and 4 % in Q (`docs/figures/bace-wrapup.html`). These values do not establish
an absolute charge calibration.
[Testing](docs/manual/3.5-testing.md) explains what the numerical regression
and the bench comparison establish.

## Tests

```bash
python -m pytest -q
```

CI runs the suite on Ubuntu and Windows for pull requests and pushes to `main`. No test touches an
instrument. The console's own suite (`node --test "ui/tests/*.test.mjs"`) and its lint run
from the Python suite when Node and eslint are present, and skip otherwise.
