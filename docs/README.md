# Documentation

The manual explains the bench. The current documents describe tools and
interfaces. Records retain measurements, analysis and design decisions.
Paths in this index are relative to `docs/`.

## Current

| File | Purpose |
|---|---|
| `manual/` | Bench manual: operation, principle, maintenance and reference. `manual/README.md` lists the chapters. |
| `style.md` | Register, quantities, comments, console text and review. |
| `bace-principle.md` | Literature references and their correspondence with the code and manual. |
| `bench-checkout.md` | Staged bench checks with `python -m bace.bench`. |
| `service-contract.md` | Package layout, concurrency, routes, events, journal, pipeline schema, checks and cost model. |
| `integration-window.md` | Field-relative window arithmetic and reading older files. |
| `ui-rules.md` | Console rendering rules: numbers, colours, provenance, charts, shot phases and controls. |
| `ui-fields.md` | Visible fields, conditional fields and folded groups on module cards and node forms. |

## Records

| File | Purpose |
|---|---|
| `notes.md` | Records the rig as measured and the claims measurement overturned. |
| `ui-plan.md` | The record of how the console was built. It stays in `docs/`. `../ui/README.md` describes the implementation. |
| `bace-console-round3.html` | Self-contained console design. |
| `design/` | Design sources and mockups; `design/README.md` describes them. |
| `history/` | Handover, plans and reviews. These are records, not specifications of the current code. |
| `analysis/` | Measurement write-ups, figures and tables. |

## Figures

The evidence pages open in a browser. They have no external dependency except
a web font. Corrections on the pages qualify their claims.

| File | Content |
|---|---|
| `figures/bace-wrapup.html` | Four comparison runs. |
| `figures/bace-drift.html` | Device drift in the LabVIEW and service comparisons. |
| `figures/bace-timing.html` | Trigger chain, five edges, 502 ns optical path and delay zero. |
| `figures/bace-polarity.html` | Four `output_polarity` × `invert_polarity` combinations. |
| `figures/bace-polarity-raw.html` | Raw light and dark traces for those combinations. |
| `figures/bace-delay-scan-1.html` | Delay-scan data. |
| `figures/bace-delay-scan-2.html` | Delay-scan data and baseline correction. |
| `figures/bace-chain.html` | Instrument wiring. |
| `figures/bace-anatomy.html` | Measurement sequence. |
| `figures/bace-architecture.html` | Modules and dependency direction. |

## History

| File | Content |
|---|---|
| `history/HANDOVER-2026-09-02.md` | Bench comparison, changes and raw values. |
| `history/bace-status.html` | Development status record. |
| `history/port-plan.html` | LabVIEW conversion plan. |
| `history/service-plan.md` | Service plan; `service-contract.md` describes the implementation. |
| `history/ui-kickoff.md` | Console brief. |
| `history/ux-screening.md` | Operator-flow review. |
| `history/naming-plan.md` | Sample identity and run naming plan. |

## Analysis

| File | Content |
|---|---|
| `analysis/2026-09-06/README.md` | Digitiser averaging and the 220–295 K sweep. |
| `analysis/2026-09-06/spike-lag-and-dark-charge.md` | Spike lag and the shutter-shut control. Its correction in §3 rejects the split of Q into photogenerated and injected parts. |
| `analysis/2026-10-26/scope-window/README.md` | Record length, baseline, integration window and settle times from null and interleaved runs. |
| `analysis/` | Also holds working J–V, J–V(T), kinetics, ln Q against V_pre, shutter-shut and V_oc against intensity plots without a write-up reference. |
