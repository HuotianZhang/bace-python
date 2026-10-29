# Field placement

`ui/lib/fields.js` defines visible rows and folded groups. `cardModel` turns
a catalogue entry into that layout. `ui/lib/card.js` renders it. This document
states the placement and its reasons. Paths beginning with `ui/`, `bace/` or
`docs/` are relative to the repository root.

## Placement

Visible rows define the next measurement. Folded groups control acquisition,
processing and instrument setup.

Sweep geometry, illumination levels, loop counts, the V_oc the axis is centred
on; above. SMU settling and NPLC, scope record length and timebase, trigger
arming, the integration window, shutter waits; below.

`invert_polarity` and the combined `output_polarity` control remain visible.
They change physical bias levels and which level the device holds between
pulses. `current_sign` is a separate bench convention for recorded current.
Changing generator polarity to change the displayed sign changes the
measurement (`docs/notes.md`). The scan axis receives the accent treatment.

The fold retains grouped fields; density does not mean omitting constraints.

## jv · 15 parameters → 6 above, 9 folded

| # | field | render | note |
|---|---|---|---|
| 1 | `start_v` `stop_v` `step_v` | one range row | `−0.20 → 1.20 · step 0.02 · 71 pts` |
| 2 | `settle_s` | float, s | dwell per point; hysteresis lives here |
| 3 | `both_directions` | bool | |
| 4 | `pixel_area_cm2` | float, cm² | `0` means the answer is amps, not mA/cm² (`ui-rules` §6) |
| — | illumination | bench read-back, not a parameter | `shut`, `1.020 V`, or `unknown ⚠`. This is the whole of `jv`'s relationship with the light: it reads it, labels the curve from the read, and changes nothing. From `/bench`, and it is what the curve's `label` will say |

Folded: `sourcemeter · 9`.

The read-back stays near the top because identical sweep parameters can
produce dark or light curves. Unknown illumination does not by itself block
`jv`; the file records unknown. Parameter errors, missing SourceMeter,
compliance and live-output checks can still block the run.

## Instruments

The bench tab is two zones, and the shape of each says what it does:

- Instruments; one row per instrument an operator switches by hand:
  `Shutter` `open | shut`, `LED · 33220A` `off | DC | pulse`, `Relay`
  `amplifier | sourcemeter`, and `Park`. A switch's lit position is the
  instrument's read-back, never the last click; clicking another position
  posts that one bench action (`POST /bench/actions/*`, the same the chain
  strip posts), and the snapshot in the answer is what moves the switch. A
  refused action leaves it where the bench is. `how: "inferred"` draws the
  whole switch dashed, as the rail does. Nothing here is a run.
- Modules: five cards for J–V, light J–V, BACE, power and temperature actions.

The shutter and LED are independent bench actions. The LED mode determines
which fields a switch action sends:

| the LED switch | sends | leaves alone |
|---|---|---|
| `off` | nothing; output off | the shutter |
| `DC` | `led_v`, output on | the shutter |
| `pulse` | `led_v` `led_low_v` `pulse_frequency_hz` `duty_percent`, output on | the shutter |

The four LED fields sit beside the switch, editable, and are the `light` module's
own parameters through the ordinary `PUT`; so a `light` node in a tree
starts from the module's resolved bench values. The ones the lit position does
not send are drawn dim. `ui/lib/instruments.js` is the model and the renderer;
`ui/tests/instruments.test.mjs` holds the read-back rule down.

`shutter`, `led_mode` and `settle_s`; what a *node* reads to know what to
do and how long to hold; have no place on the bench: clicking `open` already
is `shutter = open`. They are edited where the node runs, on the pipeline tab.

### Node overrides

A module's bench values supply each pipeline node unless that node overrides
them. The node form (`cardModel(entry, {form: 'node'})`) keeps visible:

- loop-bound and derived values (`↳`, accent),
- this node's overrides, with `↺` beside the value,
- node-only rows: `light.shutter`, `led_mode` and `settle_s`,

Other values fold as `N more · same as bench` in service groups. `↺ bench`
drops all node overrides. The tree and form retain one convention for the
same dependency.

The tree shows `↺` after a node with overrides, with their names on hover.
A node without overrides carries no such marker.

`→ bench` in the node form opens `#/bench?module=bace`, scrolls to the
module card and highlights it for less than two seconds. The view handles
the query on mount and hash change without resetting fold state. The
highlight is view state, so an arriving check can rebuild the card without
losing it.

### Recipe bench values

`POST /pipelines/save` stores the tree and resolved bench values and sources
for every module it names. On open, the console compares these with the
bench. It lists changed parameters that at least one node still takes from
the bench. Parameters overridden by every node or bound by loops are excluded.
The comparison offers restore or keep-bench actions.

`↺ bench to recipe` is the ordinary `PUT`, one per module, so every card and
node form moves with it and the note goes away because there is nothing
left to say. `keep bench` dismisses it. A parameter every node of the
module overrides, or a loop binds, is not listed: no node reads the bench
for it. A recipe without a bench block says that the comparison is unavailable.
`ui/lib/recipe.js`, held down by `ui/tests/recipe.test.mjs`.

## jv_bace · 22 parameters → 11 above, 11 folded

`jv_bace` manages the illumination range and supplies V_oc. Its LED level
must match the BACE pulse high level for the V_oc binding.

| # | field | render | note |
|---|---|---|---|
| 1 | `led_start_v` `led_stop_v` `led_step_v` | one range row | LED levels: `1.010 → 1.030 · step 0.005 · 5` |
| 1′ | `led_v` | inherited row | only when inherited or derived. Inside an illumination loop this is the level, and the range row is not shown (`ui-rules` §6: an inherited value reads as inherited, showing the value it will get) |
| 2 | `led_settle_s` | float, s | |
| 3 | `dark` | bool | adds a dark curve per direction; the estimate counts it |
| 4 | `start_v` `stop_v` `step_v` | one range row | |
| 5 | `settle_s` | float, s | |
| 6 | `both_directions` | bool | |
| 7 | `pixel_area_cm2` | float, cm² | |
| — | shutter | bench read-back, not a parameter | `open`, `shut` or unknown. From the `/bench` snapshot |

Folded: `led · led_low_v` (carried for the rail; `run_jv` drives the LED DC),
`led_v` when it is neither inherited nor derived, `sourcemeter · 9`.

## bace · 53 parameters

With `measure_dc = false`, the bench form has 15 visible parameters,
35 folded and three hidden. With `measure_dc = true`, `v_sat` becomes
visible: 16 visible, 35 folded and two hidden. Counts come from `cardModel`.

| State | Meaning |
|---|---|
| Hidden | The pinned scan field, the V_oc flag belonging to the other axis, or `v_sat` without `measure_dc` |
| Folded | Applicable parameter in its service group |
| Not read | Visible but disabled with a reason from `fields.inertReason` |

Not-read rules cover `trigger_slope_positive` without external arming, the
SourceMeter group without `measure_dc`, step size for a zero-width range,
meter settle bounds without a meter, temperature `timeout_s` without a
controller, and DC settle fields on J–V modules. The console marks
`dark_settle_s` inert whenever `dark_reference = same`; the engine skips it
only for light-first shots. Dark-first shots rewrite levels and use that
wait. This is a console limitation under `alternate_order`.

`core.axis.voc_flags` makes the hidden V_oc flag inert.
`_smu_config` and `smu.ceiling` apply only when BACE requests `measure_dc`.
`ui/tests/fields.test.mjs` checks that visible, folded and hidden fields
account for every parameter.

| # | field | render | shown when |
|---|---|---|---|
| 1 | `axis_name` | segmented, 3 choices, labelled *Scan parameter* | always |
| 2 | `axis_start` `axis_stop` `axis_step` | one range row labelled *Scan range* + derived point count | always |
| 3 | `centre_on_voc` | bool | `axis_name == vpre` |
| 4 | `voc` + `led_v` | needs row; see below | always |
| 5 | `measure_dc` | bool | always; it is the *other* way to get a V_oc, and belongs beside the row that says there is none |
| 5′ | `v_sat` | float, V | `measure_dc == true` |
| 6 | `vpre` | float, V | `axis_name != vpre` |
| 6′ | `vpre_on_voc` | bool | `axis_name != vpre` |
| 7 | `vcoll` | float, V | `axis_name != vcoll` |
| 8 | `delay_ns` | float, ns | `axis_name != delay_ns` |
| 9 | `n_loops` | int | |
| 10 | `n_averages` | int | |
| 11 | `invert_polarity` | bool | arithmetic on the computed levels, not an instrument setting; see rule 5 |
| 12 | `output_polarity` + `inverted_output` | one control | four-way `auto · NORM · INV · leave`, revealing the boolean only under `auto`, and stating the effective answer |

`centre_on_voc` applies only to the V_pre axis. `vpre_on_voc` applies only
to the other axes, where pinned `vpre` becomes an offset from V_oc.
The inactive flag is hidden and ignored by the engine.

| Fold group | Fields |
|---|---|
| `illumination` · 4 | `led_low_v`, `led_settle_s`, `led_settle_max_s`, `led_settle_tolerance` |
| `acquisition` · 7 | `timebase_ns_per_div`, `record_length`, `alternate_order`, `light_shutter`, `read_intensity`, `acquisition_timeout_s`, `store_shots` |
| `timing` · 6 | `pulse_width_ns`, `pulse_frequency_hz`, `duty_percent`, `settle_s`, `dark_settle_s`, `shutter_settle_s` |
| `trigger` · 4 | `trigger_sweep`, `calibrate_trigger`, `external_trigger`, `trigger_slope_positive` |
| `processing` · 5 | `t0_int_s`, `t_int_width_s`, `offset_correct`, `offset_region`, `dark_reference` |
| `sourcemeter` · 9 | Compliance, DC settle, NPLC, averaging, terminals and sensing fields |

`inverted_output` belongs to the combined polarity row; it has no separate
output fold.

## power · 2 above, nothing folded

`wavelength_nm` and `samples` are visible. Read takes a worker job.
The persistent power panel offers Monitor, which runs beside measurements.

## temperature · 4 above, nothing folded

`setpoint_k`, `tolerance_k`, `hold_s`, `timeout_s`, and the reading. Catalogue status is `partial`. The bench snapshot distinguishes an attached
331 from the operator-pause path; the card exposes Settle.

`park` is available on the rail. `wait` and `note` are pipeline nodes.
None has a bench card.

---

## Composite rows

The generated field component owns `{value, source, detail, editable}` and one
parameter. These three are the cases it does not cover, and the card component
owns them.

The range row combines three parameters and a point count. The count comes
from the service's `estimate_text` after each `PUT`, not another span-rounding
implementation in the browser. The service rounds the span rather than
truncating it.

The needs row. `voc` with `led_v` as its detail; the service answers: `⚠ none · run jv_bace first` from `needs[]`,
and `1.0423 V @ 1.020 · jv_bace 20:58` from the derived entry
(`source: "derived"`, `editable: false`) that `service-contract.md` §5 spells
out. `led_v` is *in* that row, not elsewhere on the card, because the coupling
invariant is that the DC level which measured V_oc and the pulse high level are
the same number; `ui-rules` §6 wants the screen to make nobody want to type a
different one. A typed `voc` still wins, and reads as typed, and warns.

The bench read-back. `jv_bace`'s shutter and
`max_current_compliance_a` / `max_voltage_compliance_v` beside the folded SMU
compliance fields. Both come from `/bench`, not `/modules`. `rig.toml` sets `max_current_compliance_a = 0.05`; `run.toml` selects
`smu_current_compliance_a = 0.01`. The dataclass default is 0.05 A. The
ceiling stays visible beside the resolved value.

---

## Fold groups

Folded values stay in separate service groups. Each disclosure shows its
computed field count.

So the fold is the service's own groups, each its own disclosure with its own
count, in the order an operator reaches for them on a rig day:

```
illumination · 4    acquisition · 7    timing · 6    trigger · 4
processing · 5      sourcemeter · 9
```

Counts are computed from the catalogue answer. `FOLD_ORDER` puts illumination
and LED first, then acquisition, timing, trigger, processing, output,
SourceMeter, axis and pinned fields. Frequently edited settings stay near
their module's main controls.

## Field rules

1. `pixel_area_cm2` is above the fold on both J–V cards. It decides whether
   the result is A or mA/cm² (`ui-rules` §2). The service supplies mA/cm²; the console does not
   convert the array. It is
   also a parameter of two modules, not of the sample; the edited layer is
   per module, so it is typed twice and can disagree between the cards.
   Raising it makes that visible; the alternative is to lift it to the session
   metadata, which is a service change.
2. `settle_s` and `both_directions` are on `jv_bace` too. The sweep semantics are identical; two J–V cards
   that read differently teach the operator that they measure differently.
3. `dark` is above the fold. It adds a dark curve per direction; it doubles the curve count only
   when there is one light level.
4. `measure_dc` is above the fold, next to the V_oc row. With no V_oc in
   scope, the card offers `measure_dc` as another source beside running `jv_bace`.
5. `output_polarity` and `inverted_output` form one control.
   `RunConfig.polarity_instruction()` maps `leave → None`, `NORM → False`,
   `INV → True` and `auto → inverted_output`. The fallback is visible only
   under `auto`. `:OUTP1:POL` selects the output level between pulses;
   `invert_polarity` swaps and negates programmed levels. With the bench's
   inverting ×4 amplifier and `invert_polarity = true`, NORM holds V_pre and
   pulses to V_coll. INV holds V_coll (`docs/notes.md`).
   [Principle, hardware and code](manual/3.2-correspondence.md) gives the mapping.

6. `store_shots` is folded, about 163 MB before compression for 51 points × 100 loops ×
   4000 float64 difference samples. `store_shots` stores each shot's baseline-corrected
   light − dark, not its two raw traces. The returned sample count determines
   the allocation; the cost warning estimates it from requested `record_length`. Folded but
   not silent: when it is on, the card header carries a chip, because a
   parameter whose cost is measured in gigabytes should not be invisible.
7. The fold is groups, per the section above.
8. No field is a bare name. `doc` states its instrument or calculation and
   effect; `doc_full` expands the explanation where available. The service
   supplies both from dataclass docstrings and explicit specs.
