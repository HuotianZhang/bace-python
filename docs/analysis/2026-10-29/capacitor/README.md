# A 56 pF capacitor in the device's place

A 56 pF capacitor mounted in the sample holder in place of a device, at 294 K, both traces with the shutter shut, 200 averages. It tests the current conversion factor R_s = 5.192 V/A (manual 2.4, "Absolute calibration") and locates the voltage step in time without any device physics.

## Runs

| Run folder (`Q:\Huotian\bace-python\runs\`) | What varies | Loops |
|---|---|---|
| `293.92K_1000mVLED_-114mVVOC_offsetcorr_20261029_163151` | V_coll −0.5, −1.0, −1.5 V at `delay_ns` 0 | 4 |
| `293.94K_1000mVLED_-102mVVOC_offsetcorr_20261029_163940` | `delay_ns` 0 to 150 in 30 ns steps, V_coll −1.5 V | 2 |

V_pre is 0.386 V and 0.398 V: `vpre_on_voc` read the pinned 0.5 V as an offset from the session's V_oc (−0.114 V and −0.102 V, left from an earlier run). The swings ΔV = V_pre − V_coll below are taken from the files.

## Conversion factor

The charge of one trace, from 30 ns before the step's onset for 500 ns, against its pre-trigger baseline:

| ΔV (V) | Q, light trace (pC) | Q, dark trace (pC) |
|---|---|---|
| 0.886 | −51.1 | −51.0 |
| 1.386 | −80.1 | −80.1 |
| 1.886 | −110.0 | −109.6 |

| Fit Q = C·ΔV + b | C (pF) | b (pC) | C / 56 pF |
|---|---|---|---|
| Light trace | 58.9 | +1.3 | 1.052 |
| Dark trace | 58.6 | +1.0 | 1.046 |

The charge is proportional to the swing with an intercept near zero. The capacitance found exceeds the nominal 56 pF by 2.6–2.9 pF, which is of the size of the holder's and cable's stray capacitance and inside a typical ±5 % capacitor tolerance. The conversion factor 5.192 V/A therefore holds to within about 5 %; separating stray capacitance from the factor needs a second capacitor or a run with the holder empty.

Light − dark over the run's window is −0.4, +4.4 and +1.0 pC, at most 5 % of one trace's charge: the dark subtraction removes the electrode charge.

## The step in time

| `delay_ns` | Onset | Half-rise | Peak | Half-rise − delay | Window start |
|---|---|---|---|---|---|
| 0 | 27.2 | 33.5 | 36.8 | 33.50 | 45.1 |
| 30 | 57.6 | 63.5 | 67.2 | 33.52 | 75.1 |
| 60 | 86.4 | 93.5 | 97.6 | 33.51 | 105.1 |
| 90 | 116.8 | 123.5 | 126.4 | 33.51 | 135.1 |
| 120 | 147.2 | 153.5 | 156.8 | 33.54 | 165.1 |
| 150 | 177.6 | 183.5 | 187.2 | 33.47 | 195.1 |

Times in ns after the trigger. Half-rise = 0.9999 × `delay_ns` + 33.52 ns, residual 0.02 ns rms.

| Finding | Value |
|---|---|
| Latency from trigger + `:PULS:DEL1` to the step's half-rise | 33.5 ns, independent of the delay |
| `trigger_offset_s` in `rig.toml` | 47.1 ns |
| Window start (`t0_int_s` = −2 ns from the field arrival `rig.toml` implies) | 11.6 ns after the half-rise, on the overshoot after the step |
| The step at `delay_ns` 90 | 123.5 ns after the trigger; the light goes out at the sample at +122 ns (manual 1.2), so the field arrives 1.5 ns after it, not 15 ns |
| Ringing after the step | Period about 10 ns, decayed within about 60 ns |

The 47.1 ns was fitted to the displacement spike of a device, whose RC time delays the spike; the capacitor's step shows the field itself. With 47.1 ns the integration window starts about 13 ns after the field arrives, so the first part of the extraction current is not integrated. Light − dark removes the displacement current, so what is lost is the extraction current in those 13 ns.

## Figures

| File | Shows |
|---|---|
| `capacitor.png` | Left: the charge of one trace against the swing, with 56 pF × ΔV. Right: the light trace at each delay, with the window start dotted |

Script: `make_figures.py RUN_1 RUN_2` (run folder names as above). Numbers: `numbers.txt`.
