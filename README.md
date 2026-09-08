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
pip install -e .[lab]             # the above plus pyvisa, for the lab PC
```

`scripts/setup.sh` does the first line on Linux and then runs the suite.

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

**One instrument by hand**, without the service:

```bash
python -m bace.consoles.keithley --sim    # the 2400's front panel, :8924
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
  consoles/     standalone per-instrument panels
  params.py     where each parameter value came from
ui/             the browser console: ES modules, no build step
tools/          standalone rig scripts
scripts/        .bat entry points and setup.sh
examples/       demos against the simulator
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
| `docs/README.md` | index of everything below |
| `docs/notes.md` | the rig as measured, defects the hardware found, deliberate deviations from the original, open and closed questions |
| `docs/bench-checkout.md` | the staged hardware check-out |
| `docs/service-contract.md` | every route, event and check the service implements |
| `bace/service/README.md` | driving the service by hand |
| `ui/README.md` | the console, file by file |
| `bace/consoles/keithley/README.md` | the standalone-console pattern, worked |
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
instrument.
