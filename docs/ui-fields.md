# Which parameters sit above the fold

`docs/ui-plan.md` leaves this open on purpose — *"a table produced during M2,
from the Round 3 artboard plus what `GET /modules` actually returns, reviewed
then rather than guessed now."* This is that table.

Produced 2026-09-03 against a live `--sim` service, read off `GET /modules`
rather than off the contract. The artboard is R3·1 and R3·2 in
`docs/design/BACE Console - Round 3.dc.html`. Where the two disagree, the
disagreement is named below and not quietly resolved.

**Revised the same day**, after `jv_dark` was split into `jv` (sweep only,
touching neither shutter nor LED) and `light` (the bench's two light switches,
as a node). Nine modules, 102 parameters. The split makes the J–V card *smaller*
and moves what it lost onto a card of its own, which is what the R3 artboard
could not have anticipated — it drew `jv_dark` with no illumination row at all
and left the operator no way to make the bench dark except by running the
module that did it as a side effect.

Nothing here is a component design. It is the data the generated card reads:
per card, an ordered list of names above the fold, and the group order below
it. `ui-plan` decision 1 wants exactly this — *"a table of group names and
orders, edited without touching code"*.

---

## The line

**Above the fold: what changes the measurement you are about to take. Below:
what changes how the instrument takes it.**

Sweep geometry, illumination levels, loop counts, the V_oc the axis is centred
on — above. SMU settling and NPLC, scope record length and timebase, trigger
arming, the integration reference, shutter waits — below.

One exception, stated because it is one: **the two polarity knobs are "how" and
sit above anyway.** `invert_polarity` and `output_polarity` are the sign
convention that cost this port a day — the port read all-positive where
LabVIEW read all-negative, and the fix was `current_sign = -1` in `rig.toml`
(`HANDOVER-2026-09-02.md`). A wrong polarity does not fail; it produces a
plausible curve with the sign flipped. The artboard already gives both the
accent treatment. They stay above the fold and keep it.

`ui-rules` §1 is the other constraint: *"density is a requirement, not a
preference … if a screen feels comfortable, it is probably hiding something."*
So the fold is not a wastebasket. Fourteen fields above the fold on the `bace`
card is the intent, not an overrun.

---

## jv · 15 parameters → 6 above, 9 folded

| # | field | render | note |
|---|---|---|---|
| 1 | `start_v` `stop_v` `step_v` | **one range row** | `−0.20 → 1.20 · step 0.02 · 71 pts` |
| 2 | `settle_s` | float, s | dwell per point — hysteresis lives here |
| 3 | `both_directions` | bool | |
| 4 | `pixel_area_cm2` | float, cm² | `0` means the answer is amps, not A/cm² (`ui-rules` §6) |
| — | illumination | **bench read-back**, not a parameter | `shut`, `1.020 V`, or `unknown ⚠`. This is the whole of `jv`'s relationship with the light: it reads it, labels the curve from the read, and changes nothing. From `/bench`, and it is what the curve's `label` will say |

Folded: `sourcemeter · 9`.

The read-back row is above the fold and near the top, not tucked at the
bottom, because on this card it is the one thing the operator cannot set and
must know: the same six parameters produce a dark curve or a light one, and
only that row says which. When it reads `unknown ⚠` the run will still go —
`jv` is refusable by nothing but a missing SourceMeter — and the file will say
`unknown` too.

## light · 7 parameters → all 7 above, nothing folded

Not on the R3 artboard: it did not exist. The card is the bench's two light
switches, and it is the answer to "how do I make it dark" that the artboard
left to a side effect of `jv_dark`.

| # | field | render | note |
|---|---|---|---|
| 1 | `shutter` | **segmented**, 3 choices | `open` · `shut` · `leave`. The shutter is the light switch: it decides whether light reaches the sample, whatever the generator is doing |
| 2 | `led_mode` | **segmented**, 4 choices | `dc` · `pulse` · `off` · `leave`. Prefer the shutter over `off` — a cycled generator loses its thermal steady state and the next module waits for it again (operator instruction, 2026-09-02) |
| 3 | `led_v` | float, V | the DC level, or the pulse high level — the same number `bace` must pulse at |
| 4 | `led_low_v` | float, V | `pulse` only; shown when `led_mode == pulse` |
| 5 | `pulse_frequency_hz` `duty_percent` | float | `pulse` only; shown when `led_mode == pulse` |
| 6 | `settle_s` | float, s | |
| — | illumination | **bench read-back** | the same row `jv` carries, and the same function behind it. What the node reports having achieved, not what it asked for |

**The card's buttons are bench actions, not a Run.** This is the one card
whose primary control does not post to `/runs`: a run whose only module is
`light` is `invalid` (`light.undone-by-park`), because every run ends parked —
outputs off, shutter shut — so it would set a light and hand it straight back.
The manual form is `POST /bench/actions/{shutter-open, shutter-shut,
set-led-dc, set-led-pulse, led-off}`, which do not go through the worker. So
the card offers **Open · Shut** and **DC · Pulse · Off** as action buttons,
each one action per click, the way M1's chain strip already does it.

The module still exists as a *node*: in a pipeline it goes before the step
that needs the light, and there park at the end of the run is exactly where
the bench should end up. So the same card feeds the pipeline tab's node
editor (M5) with the same fields and a different verb.

## jv_bace · 22 parameters → 11 above, 11 folded

Unchanged by the split, and deliberately: `jv_bace` sweeps illumination *as*
the measurement and is the V_oc source, so the light is its business. Taking
it away would break the coupling invariant the whole experiment hangs on.

| # | field | render | note |
|---|---|---|---|
| 1 | `led_start_v` `led_stop_v` `led_step_v` | **one range row** | the artboard's `led_levels_v`: `1.010 → 1.030 · step 0.005 · 5` |
| 1′ | `led_v` | inherited row | **only when inherited or derived.** Inside an illumination loop this is the level, and the range row is not shown (`ui-rules` §6: an inherited value reads as inherited, showing the value it will get) |
| 2 | `led_settle_s` | float, s | |
| 3 | `dark` | bool | includes the dark curve — it doubles the run, and the estimate says so |
| 4 | `start_v` `stop_v` `step_v` | **one range row** | |
| 5 | `settle_s` | float, s | |
| 6 | `both_directions` | bool | |
| 7 | `pixel_area_cm2` | float, cm² | |
| — | shutter | **bench read-back**, not a parameter | the artboard's `shut` / `was open ✓`. From the `/bench` snapshot |

Folded: `led · led_low_v` (carried for the rail; `run_jv` drives the LED DC),
`led_v` when it is neither inherited nor derived, `sourcemeter · 9`.

## bace · 50 parameters → 14 above, 34 folded, 2 not applicable

The count is the same for every axis choice, because two fields are hidden by
whichever axis is selected rather than folded.

| # | field | render | shown when |
|---|---|---|---|
| 1 | `axis_name` | **segmented**, 3 choices | always |
| 2 | `axis_start` `axis_stop` `axis_step` | **one range row** + derived point count | always |
| 3 | `centre_on_voc` | bool | `axis_name == vpre` |
| 4 | `voc` + `led_v` | **needs row** — see below | always |
| 5 | `measure_dc` | bool | always — it is the *other* way to get a V_oc, and belongs beside the row that says there is none |
| 6 | `vpre` | float, V | `axis_name != vpre` |
| 6′ | `vpre_on_voc` | bool | `axis_name != vpre` |
| 7 | `vcoll` | float, V | `axis_name != vcoll` |
| 8 | `delay_ns` | float, ns | `axis_name != delay_ns` |
| 9 | `n_loops` | int | |
| 10 | `n_averages` | int | |
| 11 | `invert_polarity` | bool, **accent** | the exception above |
| 12 | `output_polarity` | enum, **accent** | the exception above |

**`centre_on_voc` and `vpre_on_voc` are one decision, not two.** Exactly one is
meaningful, and `axis_name` decides which: with the `vpre` axis the whole sweep
is centred on V_oc and `vpre_on_voc` is *invalid* (`service-contract.md` §5
says so); with any other axis the pinned `vpre` is an offset from the V_oc in
scope. The card shows whichever applies and hides the other. The axis field
itself is hidden from the pinned row for the same reason — it is the axis.

Folded, by group: `illumination · 5` (`led_low_v` `v_sat` `led_settle_s`
`led_settle_max_s` `led_settle_tolerance`), `acquisition · 5`
(`timebase_ns_per_div` `record_length` `read_intensity`
`acquisition_timeout_s` `store_shots`), `timing · 6`, `trigger · 4`,
`processing · 4` (`t0_int_s` `t0_int_reference` `offset_correct`
`dark_reference`), `output · 1` (`inverted_output`), `sourcemeter · 9`.

## power · 2 above, nothing folded

`wavelength_nm`, `samples`. The card's body is the reading, not the form:
`ui-rules` §8 wants both uses on it — **Read** (one number, now) and
**Monitor** (stays visible while a scan runs), which the artboard has as two
buttons.

## temperature · 4 above, nothing folded

`setpoint_k`, `tolerance_k`, `hold_s`, `timeout_s`, and the reading. Status is
`partial` — the card carries the `not wired` tag and the artboard's dim
treatment until a rig has a 331.

`park`, `wait` and `note` get no bench card: `park` is on the rail already
(M1), and the other two are pipeline nodes (M5).

---

## Three rows that are not one parameter

The generated field component owns `{value, source, detail, editable}` and one
parameter. These three are the cases it does not cover, and the card component
owns them.

**The range row.** Three parameters and a derived count, in one row: the J–V
sweep, the LED levels, the `bace` axis. **The count is never computed here.**
`PUT /modules/{m}/params` returns the module entry, and its `estimate_text`
already carries it — `1 curves × 141 pts ≈ 16 s` after a `step_v` edit,
`20 loops × 17 pts ≈ 4.5 min` after an axis edit. The service rounds the span
*never truncates it*; a second implementation in the UI would eventually round
differently and be wrong in the one case that mattered. So the card re-renders
from the PUT response, and the point count and the estimate come back together.

**The needs row.** `voc` with `led_v` as its detail — the artboard's two states
are the two the service answers: `⚠ none · run jv_bace first` from `needs[]`,
and `1.0423 V @ 1.020 · jv_bace 20:58` from the derived entry
(`source: "derived"`, `editable: false`) that `service-contract.md` §5 spells
out. `led_v` is *in* that row, not elsewhere on the card, because the coupling
invariant is that the DC level which measured V_oc and the pulse high level are
the same number — `ui-rules` §6 wants the screen to make nobody want to type a
different one. A typed `voc` still wins, and reads as typed, and warns.

**The bench read-back.** `jv_bace`'s shutter, and — `ui-rules` §6 —
`max_current_compliance_a` / `max_voltage_compliance_v` beside the folded SMU
compliance fields. Both come from `/bench`, not `/modules`. Worth knowing
before the code is written: `rig.toml` ships `max_current_compliance_a = 0.05`
and the default `smu_current_compliance_a` is `0.05`, so the field opens at its
ceiling and the ceiling must be visible for that to read as deliberate.

---

## The fold is groups, not a button

This is the one place the table departs from the artboard, and it is a
consequence of real data rather than a preference. R3 draws one button reading
`17 more · run.toml`. The `bace` card actually folds **34**, and 34 fields
behind one disclosure is the "comfortable screen" `ui-rules` §1 warns about.

So the fold is the service's own groups, each its own disclosure with its own
count, in the order an operator reaches for them on a rig day:

```
illumination · 5    acquisition · 5    timing · 6    trigger · 4
processing · 4      output · 1         sourcemeter · 9
```

Every count is computed from the answer, never typed — the `17` in the mockup
was already stale when the SMU fields landed, which is the argument.

The alternative order is `run.toml`'s own (`axis`, `pinned`, `acquisition`,
`illumination`, `sourcemeter`), which has the merit that the fold and the file
read the same way. Frequency won because the fold is opened by a person mid-run
and the file is edited between them.

---

## Seven calls this table makes that the artboard did not

Each is a deliberate departure, listed so it can be reversed in one place.

1. **`pixel_area_cm2` is above the fold** on both J–V cards. It decides whether
   the result is A or A/cm², and R3's own result panel prints `mA cm⁻²`. It is
   also **a parameter of two modules, not of the sample** — the edited layer is
   per module, so it is typed twice and can disagree between the cards.
   Raising it makes that visible; the alternative is to lift it to the session
   metadata, which is a service change.
2. **`settle_s` and `both_directions` are on `jv_bace` too.** R3 shows them on
   the dark card alone; the sweep semantics are identical and two J–V cards
   that read differently teach the operator that they measure differently.
3. **`dark` is above the fold.** It doubles the run.
4. **`measure_dc` is above the fold, next to the V_oc row.** With no V_oc in
   scope, R3·1 says *"none · run jv_bace first"* and offers no second route;
   `measure_dc` is the second route.
5. **`inverted_output` stays folded while `output_polarity` is above.** They
   are genuinely different knobs — `:OUTP1:POL` versus `:OUTP:POL INV` on the
   81150A — and the docstring for one says so about the other. Splitting them
   across the fold is a trap, and the mitigation is that the folded group is
   labelled `output` and holds nothing else. Raising both is the safer call if
   the rig ever needs the second one.
6. **`store_shots` is folded**, at 163 MB for 51 points × 100 loops. Folded but
   not silent: when it is on, the card header carries a chip, because a
   parameter whose cost is measured in gigabytes should not be invisible.
7. **The fold is groups**, per the section above.
