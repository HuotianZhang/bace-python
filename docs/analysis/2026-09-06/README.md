# 2026-09-06 — the oscilloscope averager defect, and the 220–295 K sweep

Sample 260826-6 / 6, PTQ10:ITIC, pxa. All raw data is under
`Q:\Huotian\bace-python\runs\`.

## 1. "the third point has far too little charge" — the averager was never emptied

In the four 3-point vpre scans of 01:24–01:31 (`260826-6_*_20260906_01xxxx`), the third
point's Q was a third of the first two. The diagnosis (`averager-bug-*.png`):

- `averages_dark = averages_light + ~27`: `:WAV:COUN?` runs on, because the DSO9054H's
  averager is not emptied between the light and the dark acquisition. LabVIEW's "double
  configure" (COUN 64 → RUN → COUN 200) does not empty it, nor does `:STOP`/`:RUN`; only
  an auto-range that **changes the range** does.
- A step whose range landed inside the 2 % deadband — usually the third — folded the
  previous step's dark buffer into its light trace
  (`averager-bug-light-dark-differences.png`: light2 − light0 is a positive-going
  transient).
- So about 40 % of every dark trace was light. A "normal" point read about 60 % of the
  true value, and a point that lost its restart read 20–35 %. `Q ≈ 25 / count`.

`probe_averager_20260906_030246.txt` (`tools/probe_averager.py`, measured on the lab PC):

| operation | count | conclusion |
|---|---|---|
| `:STOP` / `:RUN` | 52 → 125 | runs on |
| COUN 64 → RUN → COUN 200 | 52 → 125 | runs on |
| rewrite the same range | 125 → 191 | runs on |
| change the range | 191 → 47 | emptied |
| `:CDIS` | 200 → 0 | emptied |
| AVER OFF → one shot → AVER ON | 1 → 0 | emptied |
| `:DIG` + `*OPC?` (32) | 32 after 0.42 s | waits for the full count |
| `:WAV:COUN?` read while running | always answers 200 | it answers the configured count |

The fix (PRs #59 and #60, `bace/drivers/infiniium.py`): before every acquisition, STOP →
AVER OFF → `:CDIS` → AVER ON, and the count read back while stopped must be zero; each
auto-range pass is a single `:DIG`; the acquisition proper is `:DIG;` + `*OPC?`, with the
count checked after the fetch; `configure_edge_trigger` keeps the trigger source channel
displayed (`:DIG CHAN2` turns the other channels' display off). Verified on runs
032529-001 / -003: three points at Q = −5.58 / −5.81 / −5.69 e−10 C, 200/200 acquisitions,
where the same conditions before the fix read −3.5 / −3.5 / −1.2 e−10.

**Every BACE measurement taken before this fix reads 35–40 % low in Q. The 2026-09-02
agreement with LabVIEW does not validate absolute charge, because LabVIEW waits on
`:ADER?` the same way.**

## 2. The warming pipeline, 03:50–07:29 (`pipeline_20260906_035040`)

220 → 295 K in 5 K steps × five LED levels 1.010–1.030 V × (jv_bace + bace), where the
bace is three vpre points at Voc ± 1 mV, vcoll −4 V, **delay 0 ns** (edited on the bench
from 90 at 03:41). The per-cell numbers are in `pipeline-220-295K-cells.txt` and the
figure is `pipeline-220-295K-overview.png`.

- Each LED level held its intensity to within 2.4 % over the whole run and 0.3 % within a
  cell; 86.9–90.0 µW during the J–V. Intensity is not the variable.
- −Q rises monotonically from 0.66 nC at 220 K to 1.31 nC at 295 K; Voc falls linearly
  from 1.113 to 1.017 V; the peak current goes from 4.5 to 11.4 mA.
- Every temperature reading but the first ends in .1 K: approaching from below, the 331
  enters the 0.2 K tolerance band 0.1–0.15 K above the setpoint.

### The Q saturation at 285–295 K (`pipeline-220-295K-saturation.png`)

Ruled out on the instrument side, one item at a time: the transient completes within
1.0 µs (98 % inside the window, < 0.001 nC after it, ≤ 1 % lost before it); nothing
clips, and the resolution is 1.5–1.6 µA; the baselines differ by ≤ 0.02 mA; the light and
dark spikes differ by 0.23 → 0.66 mA in height and 0.14 → 0.46 ns in time, both growing
monotonically with temperature (the 89 "trigger jittered" warnings are the rule
misfiring), and integrating that difference gives about 0.005 nC; the averaging was
200/200. The field actually arrives at about 235 ns, some 10 ns earlier than the 246.6 ns
`trigger_offset_s` implies (the 47.1 ns was calibrated at delay = 90), which is worth
≤ 1 %.

**This section's explanation was corrected on 2026-09-07**: see
`spike-lag-and-dark-charge.md`. The saturation is not simply "some light-independent
component takes over" — the shutter-shut control shows that **the photogenerated charge
itself peaks at 250 K and is halved again by 290 K**, while the injected charge triples
over the same range, and the saturation is where the two cross. The α and dQ/dVpre below
still hold; the "circuit RC" explanation does not.

The numbers that point at physics:

| T / K | α = dlnQ/dlnI | dQ/dVpre |
|---|---|---|
| 220 | 0.18 | 0.3 %/mV |
| 250 | 0.13 | 0.05 %/mV |
| 280 | 0.09 | 1.7 %/mV |
| 295 | 0.01 | 2.4 %/mV |

Q(T) doubles while Jsc at the same level grows only about 30 %; at room temperature Q is
independent of intensity yet varies with Vpre at the exp(qV/kT) slope; and on 2026-09-05,
with vpre = 0 (the light and dark voltage spans coinciding), Q was only 0.02 nC. The
conclusion: at room temperature the charge at Voc is dominated by a component that did
not come from the light — the device's dark charge (doping or shallow traps, thermally
activated), or the capacitive mismatch of the translated dark reference (∫C(V)dV over
[−4, Voc] is not equal to that over [−4−Voc, 0]). The experiments that separate them: a
bace with `light_shutter = shut` (recipe
`T220-295K_led1010-1030mV_jvbace_bace-shutter-shut`), `dark_reference = same`, and a wide
vpre scan (Voc ± 20 mV) at 295 K and at 220 K.

## 3. Continued

`spike-lag-and-dark-charge.md` (2026-09-07): the investigation of the console's repeated
"spikes are 0.46 ns apart" warning, three tests showing it is a false alarm, and the
shutter-shut control that splits Q into a photogenerated and an injected part. Figures
`spike-lag-diagnosis.png` and `spike-lag-explained.png` (whose panels D/E/F have been
superseded by `spike-lag-revised.png`).
