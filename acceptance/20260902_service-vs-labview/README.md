# The service against LabVIEW, on the rig, 2026-09-02

The acceptance evidence for `bace/service/`: the LabVIEW engine and the Python
service measuring the same device thirty-one minutes apart, on the same bench,
the same afternoon. `tests/test_acceptance_20260902.py` reads this folder.

Kept in the repository, unlike everything else under `runs/`, for the reason
`bench-archive/` is: a claim with no evidence beside it gets rediscovered. The
port spent two nights on a "3× low" that turned out to be the reference
drifting, and a whole rig day on a polarity that the recipes had labelled
backwards. Both are settled here, in files.

| | LabVIEW 15:06 | service 15:37 |
|---|---|---|
| V_pre | 1.08284 / 1.09284 / 1.10284 V | 1.09133 V (centred on a V_oc the Keithley read at 0 A) |
| Q | −2.14 / −2.24 / −2.02 e−10 C | −1.72e−10 C (n = 2, σ 5.2e−11) |
| photocurrent peak | −1.25 mA @ 372 ns | −0.76 mA @ 377 ns |
| tail of light − dark | zero | zero |
| loops | 3 | 2 |

Same sign, same order, the spike in the same place, both tails at zero — which
is *the* criterion (`docs/README.md`, "The criterion, since it was got wrong
repeatedly"). Q differs by 23 %, on two runs half an hour apart with n = 2:
this device moved by a factor 2.8 in three hours on 2026-09-01
(`docs/history/HANDOVER-2026-09-02.md`), so the comparison is a sign-and-shape one, not a
tight numerical one. The tight numerical regression is `bench-archive/`, which
asserts that the same traces in give the same numbers out to 1.6e−06.

## What is here

```
labview_150640/     what the LabVIEW VI wrote: the light and dark trace
                    matrices (3 x 4000), the Q table, the intensity table.
                    The PNGs, the per-loop photocurrent and the derived
                    photocurrent matrix are left on the lab PC -- light,
                    dark and Q are what a check needs.
service_153722/     the service's own HDF5 (bace-run/2). It carries the
                    averaged light, dark and photocurrent, Q per loop,
                    the axis, the full run and rig config, and
                    /config/resolved -- what the instruments answered,
                    which is how this file can say which polarity ran.
journals/           three sessions of the rig day, in order:
                    125751  the run refusing to measure: "no sync on CHAN3,
                            4.4 mV peak to peak". The trigger calibration
                            was circular (`docs/notes.md`, hardware defect 11).
                    144844  everything working, wrong polarity: :OUTP1:POL
                            INV, photo peak 0.23 mA, Q +5e-11, and the
                            "tail is not a baseline" warning firing.
                    153357  the accepted run above.
```

The full set — every run folder and journal of the day, the PNGs, the failed
attempts — stays on the lab PC under
`Q:\Huotian\bace-python\service-layer-dev-04d292\runs\`.

## The configuration this was measured in

Pinned by the test, because it is the thing that was wrong all day:

| | |
|---|---|
| `invert_polarity` | `true` — the software half, levels swapped and negated |
| `:OUTP1:POL` | **`NORM`** — the instrument half |
| `current_sign` | `-1` |
| averages / shutter settle | 200 / 5 s on both traces |
| LED | 1.000 V through the 33220A at `:OUTP:POL INV`, never switched off |
| t0 integration | 120.5 ns after the trigger = 320 ns of record |

`invert_polarity = true` **with NORM** is the validated pair. The recipes
called `true` + `INV` "combination ④" and called it the answer; that label is
overturned (`docs/README.md`, the overturned table). Through the inverting ×4
amplifier, INV rests the device at V_coll, so extraction never stops and no
charge accumulates: `journals/20260902_144844.jsonl` is what that looks like
when everything else is right.

## Re-checking it

```
python -m pytest tests/test_acceptance_20260902.py -v
```

The test recomputes the photocurrent and the charge from each side's own raw
traces with `bace.core`, and compares the two runs on sign, peak position and
tail. It skips if this folder has been removed.
