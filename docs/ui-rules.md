# Rendering rules for the console

These rules define field labels, numbers, charts, state and controls.
`service-contract.md` defines payloads and routes. `ui-fields.md` defines
card placement. The [manual](manual/README.md) explains the measurement.
Paths beginning with `ui/`, `bace/` or `docs/` are relative to the repository
root; `design/` is relative to `docs/`.

## 1 · The register

The console is light, clean and dense. Type carries hierarchy; colour carries
state. The bench rail exposes eight live values. Module cards group the
remaining parameters rather than hiding constraints.

The layout targets a desktop lab PC. The rail stays fixed. The pipeline
action row stays at the foot of its scrolling panel so Start remains
reachable. Its name and recipe controls stay beside the action; the drift
note stays in the flow. Button labels name actions and costs without
repeating adjacent values.

Long run names must stay distinguishable and selectable:

```
s4_PTQ10IT4F_pxa_290K_1020mVLED_906mVVOC_offsetcorr_20260807_111521
```

A field shows its label: a sentence-case noun phrase such as "Hardware
averages" or "Centre on V_oc", from the parameter spec's `label`. The code
name (`n_averages`, `centre_on_voc`) is the identifier: the first line of the
tooltip and of the help line, and the spelling in exports, provenance, drift
lists and check codes. `doc`, a sentence identifying the instrument or
calculation and its effect, is the tooltip's second line and the help line's
body; it is not drawn under the field. `doc_full` extends the help line.
Dataclass docstrings and explicit service parameter specs are the sources.

The screen carries label, value, unit and state. `style.md` ("The console")
gives the text rules; the visual ones are:

| Rule | |
|---|---|
| Type scale | 13 px values, labels and controls; 12 px secondary text; 11 px chart ticks and marks; 15, 17 and 20 px headings and large readouts |
| Secondary text | `--grey`, 7:1 on paper. Text is never lighter |
| Controls | Buttons, switches and inputs are 24 px high with a 1 px box |
| Field row | Label, value, state. Nothing sits between rows except a warning about the row above |
| Side panels | The assistant and the plans share one side panel. It narrows the view on a wide screen and overlays it below 1280 px |

## 2 · Numbers

| Quantity | Rendering |
|---|---|
| Text and numbers | IBM Plex Sans throughout, tabular figures |
| Q | Six significant figures in scientific notation, e.g. `3.65257e-10 C`, in the shared formatter; summary views can show nC |
| V_oc and potentials | Four decimals |
| Current density | Three significant figures, fixed unit mA/cm² |
| Temperature | One decimal in K |
| Traces | Three significant figures |
| Axis labels | Quantity (unit): `Vpre (V)`, `Q (pC)`, `Time (ns)`, `T (K)` |
| Other currents, times and powers | SI prefixes where readable: mA, ns, mW |

`ui/lib/format.js` owns these rules. Names and symbols follow the glossary:
V_pre, V_coll, V_oc, Q, J_sc and J_sat. A current-density column never
changes its prefix per value. `JVCurveDone.density` arrives in mA/cm².

| Data | Screen meaning |
|---|---|
| `σ_Q = 0`, null or absent | Hollow marker, `σ_Q not recorded`; no zero-length error bar presented as measured precision |
| Uncalibrated `LED Intensity [mW/cm²] = 0.000000` in archive data | `not calibrated`; show W at the meter when available, never inferred irradiance |

A computed standard deviation can also be zero for one acquired shot.
The marker reports unavailable spread, not proof of zero noise
(`bace/core/process.py`, `ui/lib/format.js`).

## Names

A parameter keeps its name from the LabVIEW panel (`BACE_Mehrdad.vi`, pictured in the BACE Quick Guide). Where the panel has no name, use the plainest common word. Code names stay on the wire and in tooltips.

| Console | Code or old name |
|---|---|
| Measure, Sequence, Setup | Bench, Pipeline and Rig tabs |
| JV, Intensity JV, BACE, Power Meter | `jv`, `jv_bace`, `bace`, `power` |
| Measurement, step | module, node |
| All off | park |
| Check | dry run |
| Keithley | source meter, SMU |
| Vpre, Vcoll, Voc, Jsc, Jsat | `V_pre`, `V_coll`, `V_oc`, `J_sc`, `J_sat` |
| Start Voltage, End Voltage, dV, Stab. time | `start_v`, `stop_v`, `step_v`, `settle_s` on JV |
| Start Voltage LED, End Voltage LED, dV LED, LED stab. time | `led_start_v`, `led_stop_v`, `led_step_v`, `led_settle_s` |
| LED Voltage, Voltage Low, Freq, Duty Cycle | `led_v`, `led_low_v`, `pulse_frequency_hz`, `duty_percent` |
| Settle Time Jsc, Settle Time Voc, Settle Time Jsat | `smu_settle_*_ms` |
| Voc/Jsc/Jsat, Vcoll for Jsat | `measure_dc`, `v_sat` |
| # Averages, # of Loops | `n_averages`, `n_loops` |
| t0 Integration, Integration time | `t0_int_s`, `t_int_width_s` |
| Offset corr., Ext. trig, Recording Intensity | `offset_correct`, `external_trigger`, `read_intensity` |
| Wait before measure | `settle_s` on BACE |
| Resistor, Pulse Amp | `sense_resistor_ohm`, `pulse_amp` |

`t0 Integration` is measured from the field's arrival, not from the first sample as on the panel.

## 3 · Colour

| Use | Meaning |
|---|---|
| Alert | Live bias, critical preflight result or saturation |
| Warning | Wrong trigger chain, exhausted auto-range or unavailable meter |
| Accent | Swept quantity, shared by form and chart |
| Greys | Other content, with hierarchy in type |

The relay uses its own circuit diagram because its positions connect
different sources. `design/README.md` records the palette.

## 4 · Charts

J–V, timing, transient, loop and grid charts share axes and panel rules in
`ui/lib/charts/`. `design/bace-charts.js` and `design/bace-charts-r3.js`
are design references for the diagrams.

Plots follow journal figures: a closed frame, inward ticks on all four sides,
minor ticks, no gridlines, and the unit in the axis title, `Time (ns)`. Ticks
use SI prefixes, nC and not `2e-10`. The console's timing and schedule
drawings are diagrams and keep their own headers.

Text inside a plot names what is drawn. Captions and provenance go to the info
tip beside `Axes`; a warning stays on screen under the plot.

`Axes` sets each axis's range, and the J–V's log scale. Drag on a plot to zoom
and double-click to go back. A flat drag sets x only and a tall one y only. Set
ranges last for the session, through new shots, and `Auto` clears them. An
axis whose unit changes returns to automatic.

| Case | Rule and reason |
|---|---|
| `start == stop` | Show Q per loop, with mean and ±σ band. This is repetition at one point; fitting a curve would imply an axis span. |
| Swept axis | Show Q(axis) with errors that use acquired loops. State the mode in the caption. |
| Reverse J–V arm | Show the whole requested range, including −4 V when requested. Do not crop it to the power quadrant. |
| FF versus intensity | Preserve the curve's derived value; do not force a trend from a model. Series resistance can reduce FF as intensity rises. |
| Photocurrent across a delay scan | Keep DC illumination evidence beside Q(delay), so changing illumination is visible. |
| Running integral | Use a separate stacked panel. It reaches Q at the window end; flattening helps assess the extraction tail. |
| Integration window | Thin dashed lines across every panel; no fill behind the traces. |
| Series | Sequential ramp (viridis without its pale end), never a rainbow: LED levels and temperatures keep their order in greyscale. |
| Light, dark and difference | Show together; their difference alone hides two clipped parents. |
| Absent arrays | State whether data are unavailable or evicted from replay; retain scalar statistics. |

## 5 · Time scales and progress counters

The screen separates scheduling from waveform time.

| Scale | Screen |
|---|---|
| Temperature | Setpoint, reading, settle band, hold and operator pause or automatic settle |
| Illumination | LED level, settle and V_oc source |
| Loop and point | Acquired shots, current loop and scan point |
| Waveform | Delay in ns and integration window in s; these are not scheduling durations |

Loops contain complete shots with acquisition and settle waits. They do not
run in milliseconds. The measured 0.5/0.5/1 s settle recipe takes 6.3 s per
shot at 200 hardware averages ([Measurement chain](manual/2.3-signal.md)).
The cost uses measured journal durations or an explicitly labelled default.
Unknown temperature settle time leaves a lower bound, not an invented ETA.

## 6 · Provenance on screen

Render the service's `{value, source, detail, editable}`. An inherited or
derived value shows its resolved value and source. A V_oc source includes
the supplying measurement and LED level. The DC LED level for V_oc and the
pulse high level must match; the screen exposes that binding.

Show bench compliance ceilings beside the fields. The recipe may choose
values up to `max_current_compliance_a` and `max_voltage_compliance_v`.
A 2400 can source 1 A, beyond many devices' working limits.

J–V metrics `J_sc`, `V_oc`, `V_mpp`, `P_mpp` and FF are derived from the curve.
`pixel_area_cm2 = 0` leaves current in A and density absent. `metrics.jsc`
remains in A even when the curve also carries density. Results retain
R_s = 5.192 Ω, gain ×4 and probe attenuation 1 from `rig.toml`; these
multiplicative factors are needed to interpret Q.

## 7 · The shot

The light-first sequence has ten physical segments:

```
1 set light levels → 2 open shutter → 3 settle → 4 read power →
5 acquire light (autoranging) → 6 set dark levels → 7 settle →
8 close shutter → 9 acquire dark (range inherited) →
10 subtract, baseline-correct, integrate → one Q
```

The light trace sets the range; the dark trace shares it so range-dependent
zero offsets cancel. With `alternate_order`, an even loop takes dark first
when a preceding light trace supplies that point's range. Neither trace
auto-ranges on that shot. `light_shutter = "shut"` keeps the shutter shut
also for the light trace as a control.

| Order | `StepPhase` sequence |
|---|---|
| Light first, translated | `levels`, `light settle`, `acquire light`, `dark levels`, `dark settle`, `acquire dark`, `process` |
| Light first, same | The same sequence without `dark levels`: six phases; no `dark_settle_s` wait |
| Dark first, either reference | `dark levels`, `dark settle`, `acquire dark`, `light levels`, `light settle`, `acquire light`, `process`: seven phases, including `dark_settle_s` |

Shutter moves and power reads have no separate phase frames. A ten-segment
strip places them within the reported phases. `dark_reference = "translated"`
keeps the light step's voltage change from 0 V; `same` repeats its levels.
[Charge extraction](manual/2.1-extraction.md) explains their different meaning.

The timing diagram follows the form: LED waveform, V_pre to V_coll, delay,
current and window. The swept quantity uses the accent. The primary control
is the axis: `vpre`, `delay_ns` or `vcoll`. Preset concepts such as
`bace_at_voc`, `bace_legacy_three_point`, `bace_sweep`, `tdcf_delay` and
`field_dependence` describe these choices; they are not catalogue modules.

## 8 · Manual runs

A module Run uses the same monitor, journal and result record as a pipeline
node. The interaction target is a dark J–V within fifteen seconds of
operator input, without a wizard. A temperature loop inside an illumination
loop is legal; its repeated settling cost warrants a warning.

Power supports a spot Read and a persistent Monitor. The beam splitter
permits reads during runs. The switch controls service polling; it does not
only change the drawing.

## 9 · Failures that look like results

| Evidence | Screen rule |
|---|---|
| `trigger_sweep = AUTO` | State the mode: it can acquire without a trigger. Calibration refuses Sync below 0.1 V; with calibration disabled or a later lost trigger, noise can resemble near-zero Q. TRIG waits and can time out. |
| Q shown | Retain `invert_polarity`, effective `output_polarity` and the rig's `current_sign` in provenance; these have distinct effects. |
| Stopped run | Show kept/requested and statistics over acquired shots. A truncated run is a normal outcome. |
| Empty state | Say what is present or give an action: no device, no run, no nodes, no module result, unavailable meter or unwired temperature. |

Empty states avoid implementation explanations. A list with no entries still
has a row or sentence that exposes its purpose and next action.

## 10 · Voice

Warnings state evidence, consequence and remedy in short sentences.

> LED low level reaches the turn-on threshold. Set it below threshold so the low half-cycle is dark.

> Seven consecutive samples hold one extreme. The digitiser clips. Repeat the point with a larger range.

> 100 loops requested, 20 completed.

## 11 · Display states

| Subject | Rule |
|---|---|
| OK preflight checks | Collapse to a count with a disclosure, e.g. `16 checks, 1 warning  [Show]`. |
| Shot phases | Collapse to the current phase, e.g. `5 · acquire light`. |
| Module settings | Keep the service's last-used layer. |
| Running card | Keep the editor beside its result monitor. |
| Results | Show records, grid, selected node and provenance. |

## 12 · Timing checks

The shot verdict checks timing with spike and Sync traces. Spike lag alone
does not prove jitter: the light trace includes extracted charge and can
have another shape. Warn when absolute spike lag exceeds 0.25 ns and an
edge exceeds 8 ns, or independently when absolute Sync lag exceeds 0.1 ns.
The verdict stays beside its trace. Aligned Sync bounds trigger motion but
does not prove equal downstream field latency. Missing or unreadable Sync
stays explicit. [Checks and warnings](manual/4.2-checks.md) gives all checks.

## 13 · Operator work

Park while busy and Abort require arming. Run and Start guard double clicks.
The tree has undo through `ui/lib/undo.js`, covering deletion, recipe open
and reset. Recoverable edits use undo without adding confirmation to each
ordinary edit.

An operator away from the window can see pause, run and ending state in
`document.title`, through `ui/lib/title.js`. The title needs no notification
permission. It is another rendering of the monitor's state.

## 14 · Control families

| Click consequence | Family | Appearance |
|---|---|---|
| Changes a value read by Run | `.seg` enum or `.cbx` boolean | Grey border; selected enum in ink, scan axis in accent; boolean checkbox |
| Acts on the bench immediately | `.sw` | Ink border, larger target, lit position with accent underline; inferred positions dashed with grey underline |
| Changes the view | `.filt` with `.opt`, or `button.filt` | No border/fill; chosen option bold and underlined |

The consequence defines the family. The monitor interval is an instrument
switch because it restarts service polling. Plot windows and grid orientation
are view filters. Unknown booleans are indeterminate, not unchecked.
`inverted_output` uses NORM/INV because those are instrument labels.

Rare controls sit behind one `⋯`. Frequent controls stay beside their subject.
The power panel keeps switch, reading and trace accessible; interval, window,
chart toggles and exports use the menu, with defaults of 1 s and auto window.

Collapsing the panel never hides recorded data from export. Off or folded,
the menu retains interval, CSV and Clear. With no chart it omits window,
`y from 0` and SVG export. The trace fold (`▾ trace` / `▸ trace`) is independent
of the monitor switch and remembered; folding leaves polling and the numeric
reading active. The action response stays visible even when the action turns
the instrument off or fails.

Place controls beside the thing they name. Pack a row toward its subject and
leave unused space at the end, not between a label and its action.
