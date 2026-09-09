# Documentation

Three kinds, in three places.

- **`docs/` itself** — current documents. Wrong here is a defect; fix it in
  place.
- **`figures/`** — evidence pages, one measurement each. Where a later
  measurement overturned one, the page carries a dated correction and the
  claim is listed in `notes.md`.
- **`history/`** — accurate for their date, not for today. Kept for the
  reasoning.

The figure pages are single HTML files with no external dependencies beyond a
web font. Open them in a browser.

## Current

| file | what it is |
|---|---|
| `notes.md` | The rig as measured, defects only the hardware found, deliberate deviations from the LabVIEW original, open and closed questions, and the claims that measurement overturned. |
| `bench-checkout.md` | Bringing the real bench up in stages with `python -m bace.bench`. |
| `service-contract.md` | What `bace/service/` implements: package layout, concurrency and stop semantics, every endpoint, the wire and journal payload policy, the pipeline tree schema, the check catalogue, the cost model. Where it and the code disagree, fix one and say so. |
| `integration-window.md` | The integration window is measured from the field's arrival at the device. The arithmetic, and how to read files written before it. |
| `ui-rules.md` | What a screen must say: significant figures, the two zeros that mean *not recorded* and *not calibrated*, colour with a job, the three time scales, provenance on screen, the ten segments of one shot. |
| `ui-plan.md` | How `ui/` is built: the four structural decisions and the milestones M0–M6. `../ui/README.md` says what each file is. |
| `ui-fields.md` | Which parameters sit above the fold on each bench card, and why. |
| `bace-console-round3.html` | The console design the service contract was derived from. Self-contained. |
| `design/` | The design project the mockups were made in. `design/README.md`. |

## Figures

| file | what it is |
|---|---|
| `figures/bace-wrapup.html` | The port closing out: the four comparison runs charted. |
| `figures/bace-drift.html` | The reference itself was drifting: the port reproduces the LabVIEW engine, and the earlier 4× gap was the sample. |
| `figures/bace-timing.html` | The trigger chain in time: five edges, the 502 ns optical path, where the delay axis' zero is. |
| `figures/bace-polarity.html` | The four `output_polarity` × `invert_polarity` combinations. |
| `figures/bace-polarity-raw.html` | The same four as raw light and dark traces. |
| `figures/bace-delay-scan-1.html` | The first delay scan. |
| `figures/bace-delay-scan-2.html` | The second, and the baseline-correction trap it exposed. |
| `figures/bace-chain.html` | The instrument chain: what is wired to what. |
| `figures/bace-anatomy.html` | What one BACE run does, step by step. |
| `figures/bace-architecture.html` | The module layout and why the dependencies point the way they do. |

## History

| file | what it is |
|---|---|
| `history/HANDOVER-2026-09-02.md` | The day the port matched LabVIEW: what was proved, what was fixed, the raw numbers. |
| `history/bace-status.html` | The running record through 31 August 2026. |
| `history/port-plan.html` | The original plan, before any of it was built. |
| `history/service-plan.md` | The plan the service was built from. The contract supersedes it. |
| `history/ui-kickoff.md` | The brief the console was built from. |
| `history/ux-screening.md` | A review of the measurement flow from the operator's side, 2026-09-05, and what it changed. |
| `history/naming-plan.md` | How run folders and records carry sample identity. Rule 1 is built; the rest is record. |
