# BACE bench manual

This manual covers the bench, the physics behind its measurements and the software that controls it, in four parts. Each step of a measurement is traced from the principle to the instrument to the code (3.2). Terms follow the glossary (4.4) and the text follows `../style.md`.

| Part | For | Chapters |
|---|---|---|
| I Operation | Operators and new students | [1.1 Measurement overview](1.1-overview.md) · [1.2 The bench](1.2-bench.md) · [1.3 A first measurement](1.3-first-measurement.md) · [1.4 Results](1.4-results.md) · [1.5 Troubleshooting](1.5-troubleshooting.md) |
| II Principle | Everyone | [2.1 Charge extraction](2.1-extraction.md) · [2.2 The transient](2.2-transient.md) · [2.3 Measurement chain](2.3-signal.md) · [2.4 Validity](2.4-validity.md) |
| III Maintenance | Maintainers | [3.1 Architecture](3.1-architecture.md) · [3.2 Principle, hardware and code](3.2-correspondence.md) · [3.3 Data](3.3-data.md) · [3.4 Extending](3.4-extending.md) · [3.5 Testing](3.5-testing.md) |
| IV Reference | Everyone, and the assistant | [4.1 Parameters](4.1-parameters.md) · [4.2 Checks and warnings](4.2-checks.md) · [4.3 Bench actions](4.3-actions.md) · [4.4 Glossary](4.4-glossary.md) |

## Sources of truth

| Content | Source | How it reaches the manual |
|---|---|---|
| Parameters, checks | The code: parameter specs, the check catalogue | Generated (4.1, 4.2) by `tools/build_manual.py` |
| Hardware constants | `rig.toml` | Quoted with the file as the source |
| Measured values | `analysis/` | Quoted as data, with the folder as the source |
| Physics | The literature in 2.1 and `bace-principle.md` | Derived in Part II |

## Book

The chapters can be read directly as Markdown. `tools/build_manual.py` will also combine them into one HTML file with a contents page and a print stylesheet, for printing or PDF export from a browser.

## The assistant

The console's assistant reads the manual by chapter. Headings are stable anchors; when a heading is renamed, every reference to it is updated.
