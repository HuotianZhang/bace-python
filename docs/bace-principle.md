# What BACE measures

This document maps the literature to the implementation. The manual explains
[charge extraction](manual/2.1-extraction.md), the
[transient](manual/2.2-transient.md), the
[measurement chain](manual/2.3-signal.md) and
[validity](manual/2.4-validity.md).

## Sources

| reference | what it covers |
|---|---|
| J. Kniepert, *Correlation between Dynamic Parameters and Device Performance of Organic Solar Cells*, PhD thesis, Universität Potsdam (2015), ch. 5 | Method, extracted charge and simulated carrier profiles |
| J. Kniepert, I. Lange, N. J. van der Kaap, L. J. A. Koster, D. Neher, *Adv. Energy Mater.* 4, 1301401 (2014) | BACE in P3HT:PCBM |
| J. Kniepert et al., *J. Phys. Chem. C* 119, 8310 (2015) | Methods section: dark reference, LEDs, extraction bias |
| J. Kniepert et al., *J. Appl. Phys.* (2019), doi:10.1063/1.5129037 | BACE and TDCF tested by drift-diffusion simulation: inhomogeneous profiles, surface recombination |
| S. Kiermasch, A. Baumann, M. Fischer, V. Dyakonov, K. Tvingstedt, *Energy Environ. Sci.* (2018), doi:10.1039/C7EE03155F | Capacitive charge in thin-film devices |
| O. J. Sandberg, K. Tvingstedt, P. Meredith, A. Armin, *J. Phys. Chem. C* (2019), arXiv:1905.07303 | When charge extraction gives the carrier density |

## The measurement

| Step | Code | Manual |
|---|---|---|
| Hold V_pre under steady illumination, normally V_oc | The 2400 measures V_oc at 0 A; `centre_on_voc` centres the V_pre axis. `vpre_on_voc` binds the pinned prebias on the other axes. | 2.1; 3.2 |
| Step to V_coll as the light switches off | `core.pulses.pulse_levels`, 33220A LED drive, 81150A bias and `delay_ns` | 2.2; 3.2 |
| Repeat the voltage change in the dark from 0 V | `dark_reference = "translated"`, the default | 2.1 |
| Subtract the baseline and integrate light − dark | `core.process`; the window follows field arrival (`integration-window.md`) | 2.3 |
| Interpret carrier density n = \|Q\|/(eAd) | Files retain signed Q; the code does not calculate n. | 2.4 |

The bench converts the device current to a voltage on CHAN2 with the current
chain's factor R_s = 5.192 V/A, and amplifies the bias by 4 (`rig.toml`;
[The bench](manual/1.2-bench.md)).

## What the extracted charge is

[Charge extraction](manual/2.1-extraction.md) gives the four-state expression
and its large-reverse-bias approximation. The translated reference subtracts
the dark density at 0 V. A constant geometric capacitance cancels because both
steps have the same swing. Voltage-dependent capacitance leaves a residual.
Light and applied bias at V_oc do not form additive parts of the density.
The dark charge at forward bias, compared with simulation, probes injection
barriers, including the thesis example of PTB7:PCBM at 0.7 V.

## Completeness

[Validity](manual/2.4-validity.md) defines the collection-voltage, delay,
window and null tests. The bulk-density interpretation needs thick layers
(about 200 nm or more), high intensity and injection barriers. Thin layers,
ohmic contacts and low intensity can give an apparent recombination order
above 2 through contact charge. These are conditions to test, not software
acceptance criteria.

## What the literature does not cover

Digitiser averaging, baseline, window placement, drift between traces and
uncertainty belong to the measurement chain. [Measurement chain](manual/2.3-signal.md)
states the bench results and links their sources. `notes.md` and `analysis/`
retain the evidence.
