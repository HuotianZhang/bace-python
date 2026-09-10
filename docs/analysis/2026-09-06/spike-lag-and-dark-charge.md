# Where the spike lag really comes from, and Q split by the shutter-shut control

Analysed 2026-09-07 on the data of 2026-09-06. Figures: `spike-lag-diagnosis.png`,
`spike-lag-explained.png`, `spike-lag-revised.png`.

It started with a warning the console kept raising:

> the light and dark displacement spikes are 0.46 ns apart (spike edges 6.0, 6.0 ns):
> the trigger jittered during this shot, so their difference leaves the spike in the
> photocurrent and Q of this shot is not a charge.

## 0. What the rule is

`spike_lag_ns` in `bace/core/diagnostics.py` cross-correlates the light and dark traces over a window
60 ns before and 100 ns after the displacement spike, interpolates the correlation peak
parabolically to sub-sample precision, and returns the dark trace's lag behind the light
one. `SPIKE_LAG_NS = 0.25` in `bace/service/wire.py` is the threshold above which it warns.

The rule was written after a real jitter incident on 2026-09-04/05: six of twenty-three
shots had a photocurrent ten times too large with the sign flipped, and there **all three
symptoms appeared together** — a lag of 0.3–1.2 ns, edges smeared from 6 ns to 8–13 ns,
and spike heights 4–14 % apart.

## 1. These shots show only the lag

The edges are 6.0/6.0 ns, which is what a good shot gives, and the light and dark spike
heights differ by 0.6 %.

Across five independent sweeps the lag grows monotonically with temperature and the
curves lie on top of each other (about 0.14 ns at 220 K, about 0.46 ns at 295 K),
independent of intensity. Real trigger jitter does not repeat like that. The sample
interval is 0.5 ns, so a 0.43 ns lag is less than one point.

## 2. Three tests (proposed by Huotian, and borne out by the data)

**(a) It is not a time shift.** Shifting the dark trace by the measured lag and
subtracting again reduces the difference over the spike by only **3.5–4.6 %**. A pure
shift should very nearly cancel. The residual of a pure shift would have the shape
`lag × dI/dt`, which matches the measured difference only over the first 2 ns
(`spike-lag-revised.png`, panel C). The "lag" is a summary statistic of two curves of
different shape, not a timing error.

**(b) The lag is made by the extracted charge, not by timing.** Take the trace from the
shutter-shut run at the same prebias (Voc), leave the displacement spike **exactly where
it is**, add only the pure photocurrent term, and cross-correlate against the no-light
dark reference:

| T / K | measured lag (with light) | synthesised (no shift, signal added) | prebias span alone (no light throughout) |
|---|---|---|---|
| 220 | −0.155 | −0.162 | −0.059 |
| 240 | −0.185 | −0.192 | −0.073 |
| 260 | −0.240 | −0.244 | −0.119 |
| 280 | −0.336 | −0.331 | −0.214 |
| 290 | −0.428 | −0.423 | −0.275 |

The synthesis reproduces the measurement almost exactly. **Aligning the two peaks means
subtracting real signal.**

**(c) It is not the circuit impedance, it is the charge in the device held at Voc.** The
1/e decay time of the spike itself:

| T / K | prebias Voc, no light | prebias Voc, with light | prebias 0 V (dark reference) |
|---|---|---|---|
| 220 | 20.0 ns | 25.0 ns | 19.0 ns |
| 250 | 21.0 ns | 28.0 ns | 19.5 ns |
| 280 | 26.5 ns | 29.5 ns | 20.0 ns |
| 290 | 29.5 ns | 30.5 ns | 21.0 ns |

Without light, τ still grows from 20 to 29.5 ns, while the dark reference at a prebias of
0 V stays at 19–21 ns throughout. An impedance that changed with temperature would move
both; only the trace held at Voc slows down, so what changes is the amount and
distribution of charge stored in the device. At 290 K the light and no-light τ differ by
only 1 ns: the light has almost stopped mattering.

**The "circuit RC changing with temperature" explanation written earlier in `README.md`
is withdrawn.**

## 3. The main result that follows: the shutter-shut control splits Q

`pipeline_20260906_035040` (shutter open) against
`T220-295K_..._-shutter-shut_20260906_160959` (shutter shut throughout), at LED 1.030 V:

| T / K | total Q (nC) | no-light Q (nC) | photogenerated Q (nC) | no-light share |
|---|---|---|---|---|
| 220 | 0.870 | 0.423 | 0.447 | 49 % |
| 230 | 1.026 | 0.424 | 0.602 | 41 % |
| 240 | 1.139 | 0.461 | 0.678 | 41 % |
| 250 | 1.235 | 0.513 | 0.722 | 42 % |
| 260 | 1.325 | 0.672 | 0.653 | 51 % |
| 270 | 1.432 | 0.916 | 0.516 | 64 % |
| 280 | 1.523 | 1.168 | 0.356 | 77 % |
| 290 | 1.598 | 1.372 | 0.226 | 86 % |

**The photogenerated charge peaks at 250 K and is halved again by 290 K, while the
injected charge grows from 0.42 to 1.37 nC.** The monotonic rise of the total Q hides
this completely, and the "saturation" at 285–295 K in `README.md` is simply where the two
cross.

**A reservation**: the two runs are twelve hours apart, and at the cold end their Voc
differs by 40 mV (1.086 against 1.126), so the subtraction over 220–250 K is only
approximate. At 280–290 K the two differ by 2–6 mV, and that is where it is most
trustworthy. Settling the cold end needs shutter-open and shutter-shut alternating within
one pipeline, which is what the recipe
`T220-290Kby10_led1010-1030mV_open-vs-shut` is for.

## 4. Two real problems found along the way

1. **"No sync trace was fetched" is wrong**: the sync trace was fetched (1.197 V, stored
   in the HDF5 as `traces/sync_light`). It was `sync_edge_ns` that rejected it — this
   rig's sync is a **5.5 ns wide pulse**, and the function assumed a step, judging the
   levels from the 5th and 95th percentiles of a ±100 ns window around the trigger. A
   pulse that brief occupies 11 of the 400 samples, so the percentile span came out at
   0.038 V against the 0.30 V the test demanded. On this machine a sync edge could
   therefore never be reported.
2. **Q drifts within one cell**: in the run of 2026-09-07 11:52 (after the LED went to DC
   and the optical path lost its OD0.5 and gained an f100mm lens, giving 146 µW), the
   nine shots of the single 290 K cell drifted monotonically from −1.004 to −1.304 nC
   (+30 %), with the lag drifting in step from 0.339 to 0.436 ns. Getting a settled value
   needs a longer light soak.

## 5. Suggestions for the code (not yet implemented)

- A warn should need the lag to be over threshold **and** either an edge slower than 8 ns
  or spike heights more than 2 % apart; a lag on its own drops to info, saying that it is
  mostly made by the extracted charge.
- `sync_edge_ns` should handle a narrow-pulse sync, locating the edge by the largest
  gradient rather than by percentiles.
- "no sync was fetched" and "one was fetched but its edge could not be measured" should
  be told apart.
