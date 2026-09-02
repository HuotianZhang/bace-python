# bace — the BACE measurement rig in Python

A port of `BACE_Mehrdad.vi` (LabVIEW, 907 VIs) to Python + PyVISA. Redesigned
from the recovered physics rather than transliterated, modular so it can be
extended, and focused on **measurement** — analysis stays downstream.

## Start here

| file | for |
|---|---|
| **`HANDOFF.md`** | the current state, what is built, what is not, and every trap. **Read this first.** |
| `BENCH.md` | how to exercise it against the real rig, stage by stage |
| `docs/bace-status.html` | the full narrative with evidence and figures |
| `rig.toml` / `run.toml` | the bench and the recipe, kept separate |
| `scripts/` | the double-click `.bat` entry points, one per bench stage |

## Layout

```
bace/           the package
  core/         the physics. Imports nothing outward
  drivers/      six Protocol contracts; real and simulated instruments satisfy both
  experiment/   the run itself — a synchronous generator of typed events
  storage/      byte-exact legacy .dat, plus HDF5
  bench/        the staged hardware harness
tools/          standalone rig scripts — scan, bare, lightpower, relay, identify_dio
scripts/        the double-click .bat entry points; each cd's to the repo root first
recipes/        the named recipe variants, passed with --run
tests/          the suite
docs/           the figure pages; docs/README.md indexes them
bench-archive/  the 2026-08-07 reference run the regression test reproduces
runs/           measurement output. On disk, not in git
```

`rig.toml` and `run.toml` stay at the repo root on purpose: `bace.bench`
discovers them **by name** in the working directory or beside the package
(`checks._find`), and only warns before falling back to built-in defaults if
they are missing. The `.bat` files in `scripts/` therefore `cd` to the root
before doing anything, so every relative path below still resolves.

Everything runs end to end on the simulated rig, with no instruments present:

```
python -m pytest -q          # 168 tests
python demo_scan.py          # a simulated transient scan
python -m bace.bench         # the offline stages of the bench harness
```

## Status

The measurement half is written, tested, and proven on the rig: scope
acquisition, auto-range, DIO identity, shutter, and first light on a real device
all confirmed. `service/` (FastAPI + WebSocket) and `ui/` are not started — see
`HANDOFF.md` §3, which also carries the user's explicit instruction about the
UI's shape.
