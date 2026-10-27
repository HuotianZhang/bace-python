# Scope window and baseline

Sample "test" at 294 K, LED 1.000 V (light not focused on the pixel, so Q is about 0.2 nC), Vcoll −4 V, Vpre = Voc ± 1 mV, delay 0. All runs with `offset_correct = false`, so the baseline is chosen here, offline.

## Runs

| Run folder (`Q:\Huotian\bace-python\runs\`) | What changed | Loops |
|---|---|---|
| `test_293.99K_1000mVLED_484mVVOC_20261026_125108` | 500 ns/div, 5 µs pulse, 4.3 µs window | 1 |
| `test_294K_1000mVLED_483mVVOC_20261026_133541` | 2 µs/div, 20 µs pulse, 17.5 µs window | 1 |
| `test_294.01K_1000mVLED_483mVVOC_20261026_141941` | as 133541, sample disconnected | 1 |
| `test_294.01K_1000mVLED_483mVVOC_20261026_174743` | A, null: shutter shut for both traces, `dark_reference = "same"` | 3, shots stored |
| `test_294.01K_1000mVLED_501mVVOC_20261026_175026` | C, BACE as 133541 | 5, shots stored |
| `bench/scope_offset_probe_20261026_141133.npz` | scope only, bias and LED off | — |
| `test_293.98K_20261026_163157` | dark J–V, −4.5 to +0.6 V | — |

## Findings

| Question | Answer |
|---|---|
| Was the old 2 µs record long enough? | No. At 1.5 µs Q is about 80 % of its value at 15 µs. |
| What is the 0.18 mA before the trigger? | The scope's zero: 0.16 mA (−0.84 mV) with nothing driven, at the run's range. It moves with the vertical range (0.21 mA at 0.8 V, −0.05 mA at 1.6 V). The dark trace inherits the light trace's range, so it cancels in light − dark. |
| What is the 0.14 mA sag after the step? | Instrumental. It is there with the sample disconnected and common to both traces. |
| Is the late light − dark offset (1–8 µA) device current? | No. The null run gives the same late-minus-pre-trigger offset as BACE: −1.2 ± 2.9 µA per shot in both. |
| Is it reverse leakage at −4 against −4.48 V? | No. Leakage is −0.57 µA at −4.00 V and −0.76 µA at −4.48 V. The 0.19 µA difference has the wrong sign and is 1 % of Q over 6 µs. |
| Pre-trigger or last-10 % baseline? | Pre-trigger. The tail reads 10–20 % low and makes Q flat at the tail by construction. Neither removes the drift, which happens after the step. |
| Which window? | 6 µs. With the null subtracted, Q is 96 % collected at 6 µs and 98 % at 8 µs. The per-loop scatter grows with the window: 0.004 nC at 1.5 µs, 0.012 nC at 6 µs, 0.034 nC at 15 µs. |

## Settle time and averages

Eight runs at Voc, 6 loops each, four conditions interleaved twice (`settle-*` runs 20261026_201428-002 to -009, `numbers-settle.txt`).

| Settle: levels / dark levels / shutter | Averages | Time per shot | −Q (nC) | SD per loop (nC) | Time for SEM = 1 % at one point |
|---|---|---|---|---|---|
| 2 / 2 / 5 s | 200 | 17.3 s | 0.198 ± 0.006 | 0.021 | 33 min |
| 0.5 / 0.5 / 1 s | 200 | 6.3 s | 0.210 ± 0.003 | 0.009 | 1.9 min |
| 0.1 / 0.1 / 0.2 s | 200 | 3.9 s | 0.206 ± 0.004 | 0.012 | 2.3 min |
| 0.5 / 0.5 / 1 s | 50 | 5.0 s | 0.208 ± 0.008 | 0.029 | 16 min |

The means agree within the error. The longer the gap between the light and dark traces, the larger the scatter, which fits the amplifier drift found in the null run. Fifty averages triple the scatter. Voc rose from 0.493 to 0.512 V over the 30 minutes, so the first slow run sits low for that reason. Check the settle once at the coldest temperature, where the device is slowest.

## Settings adopted in `run.toml`

| Setting | Value |
|---|---|
| `timebase_ns_per_div` | 1000 (1 µs before the trigger, 9 µs after) |
| `record_length` | 10000 |
| `t_int_width_s` | 6e-6 |
| `pulse_width_ns` | 10000 |
| `offset_region` | `"pre-trigger"` |
| `alternate_order` | `true` (dark trace first on even loops) |
| `settle_s`, `dark_settle_s`, `shutter_settle_s` | 0.5, 0.5, 1.0 s |
| `n_averages` | 200 |

Use 10 or more loops and report the mean with its standard error.

## Figures

| File | Shows |
|---|---|
| `traces-raw.png`, `traces-loglog.png` | 500 ns/div: light and dark traces, and their decay |
| `photocurrent.png`, `cumulative-q-head.png`, `cumulative-q-baselines.png`, `q-vs-vpre-baselines.png`, `head-offsets.png` | 500 ns/div: light − dark, Q(t) and Q under each baseline |
| `long-*.png` | 20 µs pulse: Q(t) to 17.5 µs, light − dark decay, fraction collected |
| `open-*.png` | sample disconnected against connected |
| `stats-*.png` | null run A against BACE run C: drift per shot, Q against window, scatter against window |
| `settle-*.png` | Q per loop and the cost of precision against settle time and averages |

Scripts: `make_figures.py`, `make_figures_long.py`, `make_figures_open.py`, `make_figures_stats.py RUN_A RUN_C`, `make_figures_settle.py JOURNAL RUN_ID...`. Numbers: `numbers*.txt`.
