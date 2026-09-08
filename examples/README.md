# Examples

What to run to see it work. Nothing here is imported by the package, and
nothing here touches an instrument unless you ask it to.

## The four demos — the engine on the simulator

No hardware, no VISA, no service. Each builds its bench from
`bace.drivers.simulated` and prints what came out. **Run them from the repo
root, with `-m`**: like everything else they find `rig.toml` and `run.toml` by
name in the working directory, and `-m` is what puts the repo root on the
import path, so they work in a checkout where `bace` has not been installed.

```
python -m examples.demo_pipeline    the measurement core end to end: axis -> pulse
                                    levels -> synthetic traces -> dark subtraction,
                                    baseline, running average, charge
python -m examples.demo_scan        a transient scan over the prebias axis, driven by
                                    the same event stream the service and the console
                                    consume. Also: voc, delay, field
python -m examples.demo_jv          a J-V scan — dark, light, and hysteresis
python -m examples.demo_series      an intensity series: V_oc / J_sc / J_sat per LED
                                    level, each with a transient centred on its V_oc
```

`python examples/demo_scan.py` is not the same command: running a script by path
puts `examples/` on `sys.path` instead of the repo root, so `import bace` then
depends on the package having been installed.

They are the fastest way to see the shape of the event stream without starting
anything. For the API and the console instead, `python -m bace.service --sim
--fast --port 8900 --ui ui/`.

## Standalone folders — the hardware, and nothing else

Self-contained: they share nothing with `bace/` but the instrument, so they
keep working when the package does not, and each opens exactly one thing — one
DIO line, one GPIB session. **One process owns an instrument**, so each is what
you run *instead of* the service, never beside it.

| | |
|---|---|
| `shutter_console/` | the shutter as one switch in a browser. Imports nothing from `bace/`, needs nothing installed, and the service knows nothing about it. `tests/test_shutter_console.py` holds that down |
| `keithley_console/` | the 2400's own front panel in a browser — source a level, hold a compliance, switch the output on, watch what comes back. Same rule: nothing from `bace/`, and under `--sim` not even a VISA backend. `tests/test_keithley_console.py` holds that down |

Both are run from inside their own folder (`py -3 keithley_console.py --sim`),
or double-clicked on Windows through the `.bat` beside the script — not with
`-m` from the repo root, which is the opposite of what the demos above need.
That is the difference the two halves of this file are about: the demos are the
package demonstrating itself, and these are programs that happen to live here.

`tools/` is the other half of this: standalone rig scripts for the bench —
`scan`, `bare`, `lightpower`, `shutter`, `relay`, `identify_dio` — driven from a
terminal rather than a page.
