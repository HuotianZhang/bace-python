# Examples

What to run to see it work. Nothing here is imported by the package, and
nothing here touches an instrument unless you ask it to.

## The four demos — the engine on the simulator

No hardware, no VISA, no service. Each builds its bench from
`bace.drivers.simulated` and prints what came out. **Run them from the repo
root**: like everything else, they find `rig.toml` and `run.toml` by name in
the working directory.

```
python examples/demo_pipeline.py    the measurement core end to end: axis -> pulse
                                    levels -> synthetic traces -> dark subtraction,
                                    baseline, running average, charge
python examples/demo_scan.py        a transient scan over the prebias axis, driven by
                                    the same event stream the service and the console
                                    consume. Also: voc, delay, field
python examples/demo_jv.py          a J-V scan — dark, light, and hysteresis
python examples/demo_series.py      an intensity series: V_oc / J_sc / J_sat per LED
                                    level, each with a transient centred on its V_oc
```

They are the fastest way to see the shape of the event stream without starting
anything. For the API and the console instead, `python -m bace.service --sim
--fast --port 8900 --ui ui/`.

## Standalone folders — the hardware, and nothing else

Self-contained: they share nothing with `bace/` but the instrument, so they
keep working when the package does not, and they open exactly one line.

| | |
|---|---|
| `shutter_console/` | the shutter as one switch in a browser. Imports nothing from `bace/`, needs nothing installed, and the service knows nothing about it. `tests/test_shutter_console.py` holds that down |

`tools/` is the other half of this: standalone rig scripts for the bench —
`scan`, `bare`, `lightpower`, `shutter`, `relay`, `identify_dio` — driven from a
terminal rather than a page.
