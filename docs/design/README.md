# The console design canvas

The Claude Design project the console mockups were made in, exported whole on
2026-09-02 so the design is readable — and openable — with no account and no
network beyond a web font.

Open any `.dc.html` from a local web server (`python -m http.server` in this
folder, then the file). Opening one straight from disk works in some browsers
and not others, because `support.js` and the design system load as scripts.
For a single self-contained file that needs no server, read
`../bace-console-round3.html` instead — it is the same Round 3 design with all
33 assets inlined.

| file | what |
|---|---|
| `BACE Console - Round 3.dc.html` | **the chosen design.** Four artboards: R3·1 cold bench, R3·2 the same bench mid-check (the hero), R3·3 the pipeline as 9 temperatures × 5 levels, R3·4 the rig tab. Four tabs, of which `results` is empty here — see Round 2 |
| `BACE Console - Round 2.dc.html` | set aside as a whole, but **R2·3 is the only results design that exists** (see below). R2·1 is the bace form in full, R2·2 the run as a journal with an ETA that shows its drift |
| `BACE Console - Round 1.dc.html` | three directions × three artboards. Verdict: **B, with C's journal folded in**. Archived — see the correction note below |
| `BenchRail.dc.html` | the pinned rail as a component |
| `bace-charts.js` | the chart decisions as working code — see below |
| `bace-charts-r2.js`, `bace-charts-r3.js` | the per-round additions (`ch-eta`, `ch-qloop-trunc`, `ch-timing`, `ch-schedule`, `ch-settle`, …) |
| `icons.js` | the Lucide-style icon set the artboards use |
| `support.js` | the canvas runtime (`dc-runtime`); needed only to open a `.dc.html` |
| `_ds/modernist-…/` | the design system: the tonal ramps and **Archivo**, the heading face |

## The charts are the specification, not the pictures

`bace-charts.js` opens with *"Generated from `ui-brief/data/*.json` — every
value measured unless labelled."* It defines framework-free custom elements
(Shadow DOM, inline SVG, no dependencies) that implement most of what
`../ui-rules.md` asks for, with the real numbers baked in. Read them before
writing a chart; several can be reused as they stand.

`ch-jv` colours five curves `#bab6b6 · #9b9797 · #ff9783 · #ff563c · #ae1800`
— a sequential ramp, not five categorical hues — and labels the axis *"five
levels, sequential 1.010 → 1.030 V"* and *"sweep starts at −4 V"*.
`ch-transient` shades the integration window and prints `Q = ∫ (light − dark)
dt`. `ch-qloop` draws the ±σ band with a running mean over the per-loop dots.
`ch-qloop-trunc` (Round 2) is the truncated run.

The palette the design settled on, which no written brief records: accent
`#ec3013`, accent-dark `#ae1800`, ink `#201e1d`, grey `#7d7979`, rules
`#d7d3d3`, fill `#eae7e7`. Primary button `#ec3013` on white, secondary white
with a `#201e1d` border, both 800-weight Archivo.

## R2·3 — the results design

`docs/ui-kickoff.md` used to say the results tab was undesigned on both sides.
It is not: Round 2's third artboard is a finished results view, set aside with
Round 2 and never given a Round 3 replacement.

> **Results · the finished 9 × 5 grid · 45 runs, 44 complete, 1 partial kept as
> it is** — *"The partial cell is outlined, never averaged in silently, never
> dropped."*

It carries the grid of Q at V_pre = V_oc with V_oc beneath each cell; the
legend **`□ σ_Q not recorded · ● σ_Q measured`**, which is the answer to the
σ = 0 rule in `../ui-rules.md`; a summary strip (span, σ provenance, `intensity
null in 45 of 45 · :8918 down for the whole run`); a selected-cell panel; a
flag list where every flag states its reason; `where it is` with the full path
and file list; the provenance summary `resolved from: run.toml 17 · last-used 2
· inherited 2 (led_v, V_oc) · edited 1`; and `Re-queue this cell`.

**Two things to decide before treating it as a specification — decided in
M6, 2026-09-04.** It names a `flags.json` in the run folder, and the service
writes HDF5 + the legacy `.dat` + the journal: the view reads the record
(`GET /runs/{id}`, whose `nodes` now carry the temperature triple, the V_oc
with its level, the counts and the per-point statistics), and the flags are
built from it — the run's verdicts where they apply, the node's outcome and
counts, the temperature and V_oc provenance, the meter's silence — each a
sentence with its reason. And its `saturation` line used the pre-correction
wording: the service reports a shared extreme but **judges it as nothing**,
so the built line is the digitiser's own judgment — *N of M shots flagged*, or
*no shot flagged, M of M judged* — and says nothing about an extreme it
judged as nothing. `ui/lib/history.js` carries both decisions in its header.

## What has been corrected here, and what has not

Rounds 2 and 3 were corrected on 2026-09-02, because both are live inputs to
`ui/`:

- the 81150A `:OUTP1:POL` read `INV` where the validated recipe is **NORM**
  (`docs/README.md`, the overturned table; the 33220A's own `POL INV` is
  correct and untouched — there the rising edge must mean *LED off*);
- the bace form's second polarity row was `inverted_output` → INV. It is
  `output_polarity` → NORM: `inverted_output` is a boolean that an explicit
  `output_polarity` architects out;
- Round 2's `+ 47 ns` became the measured light path. Round 3 already had it.

**Round 1 is archived as it was.** It still carries `delay + 47 ns` in three
places, `:OUTP1:POL INV ✓` in its chain card, and the claim that INV holds the
device at V_pre between pulses — which the rig overturned on 2026-09-02:
through the inverting ×4 amplifier, INV rests the device at V_coll and
extraction never stops. It is kept for the reasoning that produced direction B,
not as a statement about the bench.
