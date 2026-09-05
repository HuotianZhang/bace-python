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
| 4 | `pixel_area_cm2` | float, cm² | `0` means the answer is amps, not mA/cm² (`ui-rules` §6) |
| — | illumination | **bench read-back**, not a parameter | `shut`, `1.020 V`, or `unknown ⚠`. This is the whole of `jv`'s relationship with the light: it reads it, labels the curve from the read, and changes nothing. From `/bench`, and it is what the curve's `label` will say |

Folded: `sourcemeter · 9`.

The read-back row is above the fold and near the top, not tucked at the
bottom, because on this card it is the one thing the operator cannot set and
must know: the same six parameters produce a dark curve or a light one, and
only that row says which. When it reads `unknown ⚠` the run will still go —
`jv` is refusable by nothing but a missing SourceMeter — and the file will say
`unknown` too.

## Instruments · the panel above the cards, and no `light` card

The bench tab is two zones, and the shape of each says what it does:

- **Instruments** — one row per instrument an operator switches by hand:
  `Shutter` `open | shut`, `LED · 33220A` `off | DC | pulse`, `Relay`
  `amplifier | sourcemeter`, and `Park`. A switch's lit position is the
  instrument's read-back, never the last click; clicking another position
  posts that one bench action (`POST /bench/actions/*`, the same the chain
  strip posts), and the snapshot in the answer is what moves the switch. A
  refused action leaves it where the bench is. `how: "inferred"` draws the
  whole switch dashed, as the rail does. Nothing here is a run.
- **Modules** — the five cards with a Run button.

The `light` card is gone. It had five buttons, seven fields and a permanent
red verdict, and the operator could not tell which fields a button read,
whether `DC` opened the shutter, or whether `Off` used the form. Now:

| the LED switch | sends | leaves alone |
|---|---|---|
| `off` | nothing — output off | the shutter |
| `DC` | `led_v`, output on | the shutter |
| `pulse` | `led_v` `led_low_v` `pulse_frequency_hz` `duty_percent`, output on | the shutter |

The four levels sit beside the switch, editable, and are the `light` module's
own parameters through the ordinary `PUT` — so a `light` node in a tree
starts from what the switch was last set to. The ones the lit position does
not send are drawn dim. `lib/instruments.js` is the model and the renderer;
`tests/instruments.test.mjs` holds the read-back rule down.

`shutter`, `led_mode` and `settle_s` — what a *node* reads to know what to
do and how long to hold — have no place on the bench: clicking `open` already
is `shutter = open`. They are edited where the node runs, on the pipeline tab.

### A node form shows only what differs from the bench

The other half of the same confusion: a card's values are also where every
pipeline node of that module starts, and a node form that drew all fifty of
them again read as a second, independent copy. So the pipeline tab's node
form (`cardModel(entry, {form: 'node'})`) keeps above the fold only

- what a loop binds or a run derives (`↳`, accent — as before),
- what **this node** overrides (the value in ink, `↺` beside it, no tag), and
- the rows a node has and a card does not (`light`'s `shutter`, `led_mode`,
  `settle_s`),

and folds everything else as `N more · same as bench`, in the service's
groups. A node with nothing typed on it is one or two rows and a fold. The
header carries `↺ bench` while there is anything to take back: every
override dropped, the node is the bench's module again. This is the rule a
Figma instance follows — show the overrides, the rest is the main component —
and it needs no word explaining it.

### A recipe records the bench it was saved on

The same dependency, on disk. A recipe file used to hold the tree alone —
the overrides — so the same recipe ran differently after a bench edit, with
no diff in the file. `POST /pipelines/save` now writes the bench values of
every module in the tree beside it, value and source. When a recipe is
reopened, the pipeline tab compares them with the bench and, where the
bench has moved on a parameter some node still takes from it, puts a note
under the name row:

    the bench has moved since demo was saved — its nodes will run with the bench, not the file:
    [↺ bench to recipe]  [keep bench]
    bace.vpre   saved 0.000   bench 0.800  V

`↺ bench to recipe` is the ordinary `PUT`, one per module, so every card and
node form moves with it and the note goes away because there is nothing
left to say. `keep bench` dismisses it. A parameter every node of the
module overrides, or a loop binds, is not listed: no node reads the bench
for it. A recipe saved before the bench was recorded says so once instead.
`lib/recipe.js`, held down by `tests/recipe.test.mjs`.

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

## bace · 50 parameters → 15 above, 32 folded, 3 not applicable

The count is the same for every axis choice, because the fields the axis makes
meaningless are hidden rather than folded. **Hidden and folded are not the same
statement**, and the split is the rule the code applies:

* **hidden** — the run will not read this value in this configuration. The
  pinned field that *is* the axis; the V_oc flag the other axis owns; `v_sat`,
  which only the `measure_dc` branch reads. Showing it offers a knob that does
  nothing, which is exactly the trap the polarity pair was.
* **folded** — it applies, it is just not worth a line right now.

`above + folded + hidden` is every parameter the module has, and
`ui/tests/fields.test.mjs` asserts that sum rather than the three numbers
alone.

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
| 11 | `invert_polarity` | bool, **accent** | arithmetic on the computed levels, not an instrument setting — see rule 5 |
| 12 | `output_polarity` + `inverted_output` | **one control**, accent | four-way `auto · NORM · INV · leave`, revealing the boolean only under `auto`, and stating the effective answer |

**`centre_on_voc` and `vpre_on_voc` are one decision, not two.** Exactly one is
meaningful, and `axis_name` decides which: with the `vpre` axis the whole sweep
is centred on V_oc and `vpre_on_voc` is *invalid* (`service-contract.md` §5
says so); with any other axis the pinned `vpre` is an offset from the V_oc in
scope. The card shows whichever applies and hides the other. The axis field
itself is hidden from the pinned row for the same reason — it is the axis.

Folded, by group: `illumination · 4` (`led_low_v` `led_settle_s`
`led_settle_max_s` `led_settle_tolerance`), `acquisition · 5`
(`timebase_ns_per_div` `record_length` `read_intensity`
`acquisition_timeout_s` `store_shots`), `timing · 6`, `trigger · 4`,
`processing · 4` (`t0_int_s` `t0_int_reference` `offset_correct`
`dark_reference`), `sourcemeter · 9`. The `output` group is not in that list
and has no entry of its own: `inverted_output` is drawn inside the polarity
control above, so the group empties itself. That is what a computed count
buys — a typed one would have said `output · 1` for ever.

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
`17 more · run.toml`. The `bace` card actually folds **32**, and 32 fields
behind one disclosure is the "comfortable screen" `ui-rules` §1 warns about.

So the fold is the service's own groups, each its own disclosure with its own
count, in the order an operator reaches for them on a rig day:

```
illumination · 4    acquisition · 5    timing · 6    trigger · 4
processing · 4      sourcemeter · 9
```

Every count is computed from the answer, never typed — the `17` in the mockup
was already stale when the SMU fields landed, and the `output · 1` this page
itself predicted was stale by the time the polarity control was built. Both are
the argument.

The alternative order is `run.toml`'s own (`axis`, `pinned`, `acquisition`,
`illumination`, `sourcemeter`), which has the merit that the fold and the file
read the same way. Frequency won because the fold is opened by a person mid-run
and the file is edited between them.

---

## Eight calls this table makes that the artboard did not

Each is a deliberate departure, listed so it can be reversed in one place.

1. **`pixel_area_cm2` is above the fold** on both J–V cards. It decides whether
   the result is A or mA/cm² — R3's own result panel prints `mA cm⁻²`, and
   since 2026-09-03 that is the unit the service sends, not one the console
   converts into (`ui-rules` §2). It is
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
5. **`output_polarity` and `inverted_output` are rendered as one control.**
   *Corrected 2026-09-03 from an earlier reading of this table, which called
   them "genuinely different knobs".* They are not two knobs: they are one
   knob and its fallback. `RunConfig.polarity_instruction()` is
   `leave → None`, `NORM → False`, `INV → True`, **`auto` → `inverted_output`**
   — so `inverted_output` is read only when `output_polarity` is `auto`, which
   is the default, and is dead at any other setting. Two fields where one is
   silently inert at three of four settings is a trap however they are laid
   out, so the card shows a single four-way control (`auto · NORM · INV ·
   leave`) that reveals the boolean underneath only on `auto`, and says what
   the effective answer is.

   Both are about the **81150A's output polarity** (`:OUTP:POL`), which decides
   which of the two levels the device *rests* at between pulses: NORM holds it
   in extraction and pulses to V_pre; INV holds it at V_pre and pulses to
   V_coll, which is BACE as the physics describes it. Neither has anything to
   do with `invert_polarity`, four fields away in `processing`, which is
   arithmetic — it swaps and negates the two computed levels before they are
   sent. That pair of names is the single worst ambiguity in the catalogue,
   and it is why rule 8 below exists.
6. **`store_shots` is folded**, at 163 MB for 51 points × 100 loops. Folded but
   not silent: when it is on, the card header carries a chip, because a
   parameter whose cost is measured in gigabytes should not be invisible.
7. **The fold is groups**, per the section above.
8. **No field is shown as a bare name.** Every field carries one sentence
   saying **which instrument it touches and what it does to it**, and expands
   to the whole explanation — what it costs, and what a wrong value produces.
   `GET /modules` now carries both (`doc`, `doc_full`), the engine's dataclass
   docstrings are the single source, and as of 2026-09-03 all 69 distinct
   parameters have a `doc` where 14 had none. The rule is `ui-rules` §1, and
   the reason is on this page: `smu_nplc`, `trigger_sweep`, `duty_percent` and
   `v_sat` mean nothing to a reader who has not been told, and
   `inverted_output` means something other than it looks.
