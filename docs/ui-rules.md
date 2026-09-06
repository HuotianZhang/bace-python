# Rendering rules for the console

What a mockup cannot show and an API contract has no place for: how a number
should be set, what a chart must switch on, and which states are so
consequential that a small amber triangle is a failure of the design.

Distilled 2026-09-02 from the design pack at `D:\BACE\ui-brief\` — which was
written before the console was designed and is now historical — keeping only
what (a) later measurement has not overturned, (b) is not already in
`service-contract.md` or the code, and (c) is not obvious. The pack itself
stays where it is; nothing in the repo needs it any more.

Two companions carry what this file deliberately does not:
`design/` holds the canvas and, in `design/bace-charts*.js`, most of these
rules already written as working code; `service-contract.md` is the authority
on every shape and route.

---

## 1 · The register

Light, clean, dense. A working instrument, not a dashboard and not a control
room in dark chrome — closer to a well-set scientific paper: generous with
information, sparing with ornament, and quiet enough that a red element means
something.

**Density is a requirement, not a preference.** A `bace` module has ~20
parameters, a pipeline has three nested levels, the bench rail carries eight
live values, and the operator wants them all visible. The design's job is to
make that legible, not to hide it behind progressive disclosure. *If a screen
feels comfortable, it is probably hiding something the operator needs — ask
what was left out.*

Desktop only: a lab PC at 1920×1080 or wider, one window. No mobile, no tablet.
The bench rail is fixed; everything else may scroll — with one exception, and
it is the shape any other should take: the pipeline's action row is pinned to
the foot of its own panel (`.pipe-actions-bar`), because the canonical
structure is taller than the port and Start would otherwise sit below the fold.
What it pins is a control, not a reading, and that is the whole of the rule:
the name field and the recipe picker ride with the buttons because they are
acted on there; the drift note stays in the flow because it is something to
read. The panel under it still scrolls and nothing the run recorded is behind
it. A button label carries what cannot be got otherwise — undo names what it
takes back, Start names its cost — and never a value that is on screen in a
field beside it.

Long names are real and must not truncate into ambiguity:

```
s4_PTQ10IT4F_pxa_290K_1020mVLED_906mVVOC_offsetcorr_20260807_111521
```

**Use the real parameter names as labels.** The operator already knows
`v_pre`, `n_averages`, `centre_on_voc`; a prettier synonym is a second
vocabulary to learn.

**And never the name alone.** The name is the label, not the explanation.
Some of these fifty names carry their meaning (`n_loops`, `setpoint_k`);
plenty carry none (`smu_nplc`, `trigger_sweep`, `duty_percent`, `v_sat`); and
a few carry the wrong one — `inverted_output` is the 81150A's output polarity,
`invert_polarity` is arithmetic on the computed levels, and the two sit four
fields apart. So **every field shows one sentence saying which instrument it
touches and what it does to it**, and every field can be expanded to the whole
explanation, including what a wrong value produces. `GET /modules` carries
both: `doc` is that sentence, `doc_full` the rest. Neither is optional and
neither is the UI's to write — the engine's dataclass docstrings are the one
source, so the screen and the code cannot drift apart.

The same rule holds off-screen: a review, a hand-off note or a commit message
that names a parameter says what it is for. A reader who has to go and look up
`store_shots` has been handed a decision they cannot make.

---

## 2 · Numbers

- **IBM Plex Sans** for text, **IBM Plex Mono** for every number. (The canvas
  also uses **Archivo** for the wordmark, card titles and buttons — see
  `design/README.md`.)
- **Tabular figures everywhere a number can change.** A charge that jitters
  horizontally while it converges is a bug, not a style.
- Significant figures carry meaning, because they are what the measurement
  resolves: **charge to 5–6** (`3.65257e-10 C`), **V_oc to 4 decimals**,
  **current density to 3**, **temperature to 1 decimal**, traces to 3.
- The slash-unit convention the rest of the project uses: `V_pre / V`,
  `Q / C`, `time / s`, `T / K`. Subscripts matter — `V_oc`, `V_pre`, `V_coll`,
  `J_sc`, `J_sat`, `t_0,int`.
- SI prefixes where they are natural (mA, ns, mW), scientific notation where
  they are not (charge). Do not force one convention on both.
- **Current density is mA/cm², always** — never a prefix chosen per value. It
  is the unit a J–V curve is read in, it is what the service sends
  (`JVCurveDone.density`, `experiment.jv.current_density`), and it is the one
  quantity here where a per-value prefix actively destroys the comparison: one
  sweep printed `20.0 mA/cm²` at one end and `1.90 nA/cm²` at the other is a
  column nobody can read down and an axis nobody can label.

**Two zeros that are not zero.** Both appear throughout the real archive and
both must be rendered as absences, never as values:

| in the data | means | must read |
|---|---|---|
| `σ_Q = 0.000000` — most of the 9 × 5 grid; only the 290 K and 295 K runs carry real σ (~4e−11 C) | **not recorded** | its own rendering. A zero-length error bar drawn as a bare dot is a lie. R2·3 answers it: `□ σ_Q not recorded · ● σ_Q measured` |
| `LED Intensity [mW/cm²] = 0.000000` — **every row**, because the beam-splitter/area factor is not recoverable | **not calibrated** | watts, or "not calibrated". Never a number that looks calibrated |

---

## 3 · Colour, with a job

Reserve saturation for meaning. In order of loudness:

1. **Alert** — a live bias output, a `crit` preflight entry, a saturation
   verdict. Should be the only thing on screen at that intensity.
2. **Warning** — a trigger-chain state that reads wrong, autorange giving up,
   the power-meter console not answering.
3. **Accent** — the swept quantity. In the timing diagram the swept axis is in
   the accent colour and the rest is ghosted; the same accent identifies it in
   the form and in the chart.
4. **Everything else** — greys, with the type doing the hierarchy.

Relay position gets its own visual treatment. It is not "info", it is the
interlock, and the two positions are physically different circuits.

The hex values the design settled on are in `design/README.md`.

---

## 4 · Charts

**Read `design/bace-charts.js` and `design/bace-charts-r3.js` first.** They are
the chart decisions in executable form — sequential J–V ramp, the shaded
integration window, the ±σ band with a running mean — with measured values
baked in, and several are reusable as they stand. Load the `dataviz` skill
before writing a new one.

What the scripts do **not** carry, because the case was never drawn:

- **A zero-width axis plots Q per loop, not a curve.** `start == stop` is a
  *repeat* — "Q at V_oc twenty times" is 20 loops of one point, which has no
  curvature bias, where the legacy three-point straddle buys one for nothing.
  A swept axis plots Q(axis) with error bars that tighten as loops complete.
  **The chart switches on this, and the switch should be visible, not silent.**
  (`ch-qloop` vs `ch-qt`.)
- **The −4 V start is a layout problem to solve, not to crop.** The real sweep
  runs from −4 V, so the reverse arm is four times the power quadrant.
- **FF falls as intensity rises** — series resistance, the opposite of what a
  naive model gives. Do not "correct" it, and do not treat it as an outlier.
- **A flat I(delay) is the result, not a null result.** A constant photocurrent
  across the delay scan is one of that scan's acceptance criteria; if it moved,
  Q(delay) could not be read on its own.
- **The running integral is worth the space.** `cumulative_charge_c` saturates
  at Q, which is the clearest way to show what the integration window buys:
  the charge is where the curve flattens. It is also the one plot the LabVIEW
  panel had that the operator will look for.
- Light, dark and photocurrent belong together — showing photocurrent alone
  hides exactly the failure where both parents sit on the digitiser's rail and
  the difference is identically zero.

---

## 5 · Three time scales, and the progress counters

The loops differ by three orders of magnitude, and the UI must not pretend
otherwise. This is the strongest reason temperature is a loop and not an axis.

| loop | per iteration | between iterations |
|---|---|---|
| `temperature` | **14 min – 2 h** (measured; the 2 h is the first step, cooling from room temperature) | ramp, wait for the band, dwell |
| `illumination` | **seconds** | set the level, `led_settle_s ≈ 2 s`, re-measure V_oc |
| `repeat` (`n_loops`) | **milliseconds** | nothing — it is the averaging loop |
| the axis inside one shot | **nanoseconds** | it is the physics, not scheduling |

**"Step 412 of 8400" is useless here.** What the operator wants is *which
temperature, which intensity, how far into the scan* — three counters at three
scales — plus an ETA dominated entirely by the outer one. A nine-temperature
sweep measured 4.5 hours, of which the measuring was a small fraction.

Temperature settling is the slowest thing in the system by three orders of
magnitude, and **must not look like a step that "runs"**.

---

## 6 · Provenance on screen

The service hands every parameter `{value, source, detail, editable}`. Render
it; never re-derive it. Beyond that:

- **An inherited value must read as inherited, showing the value it will get** —
  not as an empty input. `led_v` inside an illumination loop, and a `V_oc`
  arriving from a `jv_bace` two nodes earlier, are the two that matter.
- **Make the coupling invariant visibly automatic.** The DC level that measured
  V_oc and the pulse *high* level of the transient must be the same number. The
  code enforces it; the screen's job is to make nobody want to type a different
  one.
- **Show the compliance ceiling next to the field.** `rig.toml` carries
  `max_current_compliance_a` and `max_voltage_compliance_v`; a run may choose
  anything up to them. A 2400 will happily push 1 A into a small cell.
- **J–V metrics are interpolated, not measured** — `J_sc`, `V_oc`, `V_mpp`,
  `P_mpp`, `FF` all come from the curve. Label them derived.
- `pixel_area_cm2 = 0` means report amps and leave density out, rather than
  defaulting to 1 cm² and silently mislabelling A as mA/cm².
- **Which measurement supplied a V_oc, and at what LED level.** A V_oc from a
  different illumination is worse than no V_oc.
- Bench properties, not per-run choices: `R_sense 5.192 Ω`, amplifier gain ×4,
  probe attenuation 1. They are multiplicative and leave no trace in the data,
  so a results view restates them beside the numbers.

---

## 7 · The shot

One shot is ten physical segments, and two of the asymmetries are load-bearing:

```
1 set light levels → 2 open shutter → 3 settle → 4 read power →
5 acquire light (autoranging) → 6 set dark levels → 7 settle →
8 close shutter → 9 acquire dark (range inherited) →
10 subtract, baseline-correct, integrate → one Q
```

- **Autorange runs on the light trace only**; the dark trace inherits the
  range, or the subtraction compares two different scales.
- **The shutter closes before the dark acquisition**, not after the light one.

The service reports seven `StepPhase` frames, not ten — `levels`, `light
settle`, `acquire light`, `dark levels`, `dark settle`, `acquire dark`,
`process`, and six when `dark_reference = "same"`. The shutter moves and the
power read are not separately reported, so a ten-segment strip has to place
them itself.

`dark_reference` decides what "photocurrent" means and belongs on screen:
`translated` (the default, and what the VI does) repeats the same voltage
*swing* referenced to zero; `same` leaves the dark trace at the light levels.
They are not interchangeable.

**Draw the shot, do not describe it.** The timing diagram — the LED square
wave, the bias stepping from V_pre to V_coll a delay after the edge, the
resulting transient, with the swept quantity in accent and the rest ghosted,
updating as the form is edited — is where a wrong V_coll sign or an absurd
delay becomes visible *before* the run. (`ch-timing`.)

**The swept axis is the primary control**, not a dropdown buried among thirty
parameters: which quantity is swept *is* the choice of experiment — `vpre` is
BACE, `delay_ns` is TDCF, `vcoll` is the field dependence of extraction. An
earlier round named five presets worth reconsidering: `bace_at_voc`,
`bace_legacy_three_point`, `bace_sweep`, `tdcf_delay`, `field_dependence`.

---

## 8 · Manual runs are first-class

The operator's stated workflow is to fire one module before committing to
anything long. So:

- **Firing one module takes seconds of interaction, not a wizard.** The bar is
  a dark J–V in **fifteen seconds** by someone who has not seen the UI before.
- A manual run and a pipeline step **share the same live monitor**. What the
  operator learns to read while checking the bench is what they read at hour
  three.
- Manual results are results: same history, tagged manual. A verification J–V
  an hour before a long run is exactly what you want to find later when the
  long run looks odd.
- A `temperature` loop nested inside an `illumination` loop is legal and
  pathological — it re-settles the cryostat for every level. **Warn with the
  cost; do not forbid.**

`power` has two uses that may want different treatment: a **spot check** (one
number, now, to confirm the lamp is on) and a **live monitor** that stays
visible while a scan runs. The meter sits on a beam splitter precisely so it
can be read during a run.

---

## 9 · Failures that look like results

The dangerous class. `service-contract.md` §3 carries the saturation rule as
`shot_verdict` and §2 the run states; what it does not carry:

- **`trigger_sweep = AUTO` plus a charge near zero is worth saying out loud.**
  AUTO sweeps anyway when no trigger arrives, so a loose sync cable produces
  untriggered noise whose dark subtraction cancels to almost nothing — a
  plausible bad result from a disconnected cable. `TRIG` waits and times out
  instead. Show which mode is in force.
- **Both sign conventions, always, wherever a charge is shown.**
  `invert_polarity` and `output_polarity` each flip the result and they are
  different knobs.
- **A truncated run is normal, not exceptional.** The real archive declares 100
  loops and contains 20. Show **kept of requested**, and let the error bars
  reflect the loops that actually ran.
- Empty states that need drawing: no device mounted · no run yet · a pipeline
  with zero nodes · a module never run this session · the power-meter console
  down · temperature not wired at all.
- **An empty state says what is there, or what to do — never why the missing
  thing is missing.** "no bace node in this run — the grid is one cell per
  bace; the nodes are listed below" told the operator how this console is
  built in order to explain an absence they had not asked about. "no bace node
  in this run — its nodes are listed below" points at what they can read
  instead, and "no nodes yet — add a loop or a module above" at what they can
  do. The internals are the author's problem.

---

## 10 · Voice

Plain, specific, unhedged. Say what happened and what it means for the number
on screen:

> *"the LED low level must sit below turn-on, so the dark half of the cycle
> really is dark"*
> *"seven samples in a row at the same value — this is the digitiser's rail.
> Do not interpret the charge."*
> *"100 loops requested, 20 completed."*

Not: "Warning: potential data quality issue detected."

---

## 11 · Questions the design has already answered

Do not re-open these; the artboard or the service settled them.

| question | answer |
|---|---|
| Should `ok` preflight entries collapse to a count? | Yes — R3·3 shows `16 checks · 15 ok · 1 warn · show` |
| Is the ten-segment strip noise once the rig is trusted? | Collapsed — R3·2 shows one line, `5 · acquire light` |
| Should a module form remember the last settings? | Yes — the service's `last_used_params` |
| Editor beside the monitor, or does it give way? | Beside: the running card *is* the monitor |
| Which of Round 1's three directions? | B, with C's journal folded in |
| What does a results view look like? | R2·3 — see `design/README.md` |

---

## 12 · The one the console now does itself

Printed on the LabVIEW panel the operator is leaving behind:

> *"Check if Cursor 1 matches the start of the transient."*

A manual verification performed on **every single run**, by eye, for years.
When this file was written nothing in `bace-python` did it, and it was named
here as the strongest candidate in the whole design pack for something the new
console should check itself.

**Done, 2026-09-05** — `core.diagnostics`, and see `service-contract.md` §3.
Six of twenty-three delay-scan shots on the first hardware day had a
photocurrent ten times too large and passed every rail check, because the
trigger had jittered and the light and dark displacement spikes sat 0.3–1.2 ns
apart where a good shot aligns to 0.01 ns. `spike_lag_ns` is that lag, by
cross-correlation, sub-sample; beyond `SPIKE_LAG_NS = 0.25` the shot is `warn`,
*"Q of this shot is not a charge"*, and the console draws the verdict beside
the trace it belongs to (`monitor.js: lastShotLine`). The eye did it for years;
it is a number now.

## 13 · What the screen owes the person, not the sample

Everything above is about what a screen must say. `docs/ux-screening.md`
(2026-09-05) is the other axis — what the flow *asks of* the operator — and it
turned up one rule this file had not stated:

> **Everything that costs the sample something arms and confirms. Nothing that
> cost the operator something did.**

Park on a busy bench arms, Abort arms, Run and Start are guarded against the
double click that would start two experiments — and the pipeline tree, the one
thing on screen composed by hand, could be destroyed in one click three ways
with nothing offering it back. The asymmetry is backwards: the sample is not
recoverable, which is why it is worth arming; the operator's work *is*
recoverable, which is why it is worth giving back rather than making harder.
So the tree has an undo (`lib/undo.js`) and the recipe picker still acts on one
click.

And the corollary, which is the same rule pointed outward: **a run that has
stopped and is waiting for somebody has to be able to reach them.** A
temperature step is 14 minutes to 2 hours and the operator is by construction
elsewhere, so the window title carries the pause, the run, and an ending they
were not there to see (`lib/title.js`). `document.title` and nothing else: it
needs no permission and works behind another window, where a denied
notification grant is denied silently for good.

---

## 14 · Three control families, one look each

A dark "selected" chip used to carry three meanings. It now carries one.
Before drawing a new control, ask what a click *does*, and pick the family
that answers:

| a click… | family | in `style.css` | looks like |
|---|---|---|---|
| **changes a value a later Run reads** — a setting | `.seg` (enums), `.cbx` (booleans) | grey border, the chosen option white on ink; the swept axis's chooser is white on the accent; a boolean is a checkbox in the same ink | `vpre \| vcoll \| delay_ns`, `auto \| NORM \| INV \| leave`, and a tick for `both_directions` |
| **acts on the bench the moment it lands** — an instrument switch | `.sw` | ink border, a larger hit target, the lit position filled grey with the accent under it; dashed and grey-underlined when the position is inferred, not read back | Instruments: `open \| shut`, `off \| DC \| pulse`, `amplifier \| sourcemeter`; the power monitor's `every 0.2 s … 5 s`, which stops and restarts the service's polling of the meter |
| **changes only what is drawn** — a view filter | `.filt` (a group of `.opt`s), or a lone `button.filt` toggle | no border, no fill; the chosen option is bold with a rule under it | the power monitor's `window auto … all`, inside its `⋯` menu; the results grid's `Q(led_v) per T \| Q(T) per led_v` |

The rule is about consequence, not shape. A pair of options that flips a
relay is a switch even though it has two positions like a boolean; a chip
that only re-plots is a filter even though it looks like an enum. A control
that does not fit is a sign the action itself is unclear — settle that
first.

**Within the setting family, a boolean is a checkbox** (#42). It was a
`true | false` pair in the family's own look, which is the wire's vocabulary
wearing a control's clothes: everyone already reads a tick as on, and nobody
outside this repository reads `false` as "the LED does not invert". A box
that is neither — the service has not said — is drawn indeterminate rather
than empty, because an empty box claims `false`. The exception is a boolean
whose two positions have *names on the instrument*: `inverted_output` is a
`bool` on the wire and `NORM | INV` on the screen, because that is what the
81150A's own front panel says. A tick beside `inverted_output` would be a
box whose meaning is the word next to it.

**And a fourth question before the family: how often is it touched?** A
control the operator reaches for less than once a session does not sit on
the screen at all — it sits behind one `⋯`. The power monitor is the
example: the switch, the reading and the trace are always there; the
interval, the window, the chart toggles and the exports are one click
further away, with defaults (`1 s`, `auto`) that a spot check never needs to
change. Off, the panel is one quiet line.

**And the limit on that answer: folding a panel away must not fold away what
it recorded.** Behind one `⋯` is one click further; behind a switch the
operator has just turned off is unreachable, and a trace that cost an hour of
the sample's life is not something to make them restart the instrument to
export. So the collapsed power panel keeps its `⋯`, carrying Export CSV,
Clear and the interval — the last because it acts on the bench and is the one
thing worth setting *before* the switch is flipped. What collapsing drops is
only what describes a chart that is not drawn: the window, the chart toggles,
and Export SVG, which serialises the SVG on screen. The same limit applies to
the sentence an action answers with: it belongs to the click, not to the
state the click leaves behind, so switching a thing *off* — or failing to —
still says so.

**And the other half of it: what folds the panel must not be the switch that
runs the instrument.** The power panel's trace is 210 px of shell above
whichever view the operator is working in, and until 2026-09-06 the only way
to be rid of it was to switch the monitor off — which stops a thread in the
service reading the meter, and leaves the run with no power record for as
long as the operator wanted the room. Two questions were answered by one
control, and the wrong one won. So the trace has a fold of its own beside the
`⋯` (`▾ trace` / `▸ trace`, remembered like every other panel wish): the
switch is about the bench, the fold is about this screen, and folding takes
away the chart and nothing else — the monitor goes on reading, the readings
go on arriving, and the number stays big at the top of the panel. It is a
button on the row rather than a toggle inside the `⋯`, because the fourth
question — how often is it touched? — answers differently for it than for the
window and the interval: this one is reached for whenever the view underneath
needs the room. The limit above still holds under the fold, and by the same
rule: with no chart on screen the menu drops the window, `y from 0` and
Export SVG, and keeps the interval, Export CSV and Clear.

**And a fifth, once the family and the depth are settled: where does it
sit?** Beside the thing it acts on. This is not a nicety — a control far
enough from its subject is a control nobody finds, and the console has now
proved it three times in one afternoon (2026-09-06, the saved-bench row):
the `⋯` pushed to the end of a 1440 px row, 1300 px from the `MODULES` label
it belonged to; the `open` beside each saved file pushed the same way by a
`1fr` third column; and a file list that only existed once something had
been saved, so the way *back* from a file was invisible until you had made
one. Each was found by an operator asking where the button was. So: pack a
row's controls toward the thing they name and let the slack fall at the end,
never between; and give a list that can be empty a first row, an example or
a sentence naming what would be in it — an empty state that shows nothing
hides the path through it.
