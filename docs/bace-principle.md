# What BACE measures

The method, as published by the group that introduced it, and what in this
code corresponds to each part.

## Sources

| reference | what it covers |
|---|---|
| J. Kniepert, *Correlation between Dynamic Parameters and Device Performance of Organic Solar Cells*, PhD thesis, Universität Potsdam (2015), ch. 5 | The method introduced; what the extracted charge represents; simulated carrier profiles |
| J. Kniepert, I. Lange, N. J. van der Kaap, L. J. A. Koster, D. Neher, *Adv. Energy Mater.* **4**, 1301401 (2014) | First journal use, P3HT:PCBM |
| J. Kniepert et al., *J. Phys. Chem. C* **119**, 8310 (2015) | Methods section: dark reference, LEDs, extraction bias |
| J. Kniepert et al., *J. Appl. Phys.* (2019), doi:10.1063/1.5129037 | BACE and TDCF tested by drift-diffusion simulation: inhomogeneous profiles, surface recombination |
| S. Kiermasch, A. Baumann, M. Fischer, V. Dyakonov, K. Tvingstedt, *Energy Environ. Sci.* (2018), doi:10.1039/C7EE03155F | Capacitive charge in thin-film devices |
| O. J. Sandberg, K. Tvingstedt, P. Meredith, A. Armin, *J. Phys. Chem. C* (2019), arXiv:1905.07303 | When charge extraction gives the carrier density |

The setup in the thesis is this bench's: a 33220A drives the LEDs, an 81150A
switches the bias between V_pre and V_extr and triggers everything, the current
is read on a DSO9054H across 50 Ω, the LEDs are Lumileds Rebel (switch-off
under 200 ns).

## The measurement

| step | thesis | here |
|---|---|---|
| hold | V_pre = V_oc(I) under steady illumination; V_oc measured beforehand across 10 MΩ | V_oc from the 2400 at 0 A, then `centre_on_voc` |
| extract | LEDs off and bias to V_extr at once, rise ≈ 20 ns | the 33220A and 81150A edges, `delay_ns` between them |
| reference | the same voltage jump in the dark, starting at 0 V | `dark_reference = "translated"` (default) |
| charge | integral of light − dark | `core.process`, window pinned to the pulse (`integration-window.md`) |
| density | n_extr = Q / (eAd) | not computed; files carry Q |

## What the extracted charge is

With the four carrier profiles involved (thesis eq. 5.2):

    n_exc = [n(I, V_oc) − n(0, V_extr)] − [n(0, 0) − n(0, V_extr − V_oc)]
          ≈ n(I, V_oc) − n(0, 0)

since n(0, V_extr) ≈ n(0, V_extr − V_oc) at large reverse bias.

| consequence | |
|---|---|
| The reference starts at 0 V on purpose | Starting it at V_oc would subtract the carriers the bias sustains at V_oc, and the total density at V_oc is the quantity wanted. `dark_reference = "same"` is that subtraction; it suits a null run, not a measurement. |
| A constant geometric capacitance cancels | Both jumps have the same swing. Only the curvature of C(V) over the two different ranges is left. |
| At V_oc, light and bias set the same density | The density is fixed by the quasi-Fermi level splitting, which V_oc is. A dark run held at the same V_oc probes nearly the same carriers; in an ideal device, exactly the same. Light and dark parts are not additive. |
| Q / (eAd) is a bulk density only sometimes | For thick layers (≥ 200 nm suggested), high intensity and real injection barriers. Thin layers, ohmic contacts and low intensity leave the contact space-charge regions dominant: n_extr no longer follows the bulk, and the apparent recombination order comes out above 2. |
| The dark charge has a use | Q in the dark at forward bias, against simulation, estimates the injection barrier (thesis, PTB7:PCBM at 0.7 V). |

## Completeness

| check | what it shows |
|---|---|
| Q against V_extr | Q saturates once extraction outruns recombination; conventional CE at 0 V got about 60 % in as-cast P3HT:PCBM |
| Q at 0 and 200 ns delay | Recombination between LED switch-off and extraction |
| Q against window length | Whether the record holds the whole transient (`analysis/2026-10-26/scope-window/`) |

## What the literature does not cover

The instrument chain: averaging on the digitiser, the baseline, the window, the
drift between the two traces, and an uncertainty budget. `notes.md` and
`analysis/` record what this bench showed for each.
