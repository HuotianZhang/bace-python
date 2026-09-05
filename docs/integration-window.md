# The integration window is pinned to the pulse

*2026-09-05. Records the decision, the arithmetic, and how to read the files
and recipes written before it.*

## What changed

`RunConfig.t0_int_s` is measured **from the field's arrival at the device**:

    field arrives at   trigger + :PULS:DEL1 + trigger_offset_s
    window (record)    t0_int_s + delay_ns + trigger_offset_s − trace.t0
                       … to … + t_int_width_s

* `:PULS:DEL1` is given `delay_ns` as it is. Nothing is added to the command.
* `trigger_offset_s` (`rig.toml [electrical]`) is the bench's sync-to-field
  latency, 47.1 ns measured on 2026-09-03 (49-point fit, 1.5 ns residual). It
  positions the window; the generator never sees it.
* `trace.t0` is the record's origin relative to the trigger (`:WAV:XOR?`),
  −200 ns at 200 ns/div and −500 ns at 500 ns/div, because `:TIM:POS` is four
  of the ten divisions.
* `t_int_width_s` (new, default 1.5 µs) is how long the window is. Its end
  travels with the pulse too. The run warns once when the record ends before
  the window does; along a delay axis the record used to cut each point by a
  different amount.

The scope triggers on the 81150A's *Sync*, which `:PULS:DEL1` does **not**
delay — only the output is. So along a delay axis the transient slides through
the record while a window fixed to the trigger (or to the first sample) stands
still, and every point gets a different slice of its own transient: Q(delay)
half physics, half window. Pinned to the pulse, every point is treated alike.

Every `StepDone` carries the window it integrated (`t0_int_record_s`,
`t1_int_record_s`); the HDF5 stores it per step (`/traces/window`) beside each
step's `:WAV:XOR?` (`/traces/trace_t0`, schema `bace-run/3`).

## The two zeros

1. **The integration zero** — where the transient starts, so where `t0_int_s`
   should sit. A question for stored data: `tools/reintegrate.py run.h5
   --sweep=-40:40:2` recomputes Q(t0_int) from the file's own traces. The zero
   is where Q stops changing as the window starts earlier.
2. **The light zero** — where the field arrives as the light goes off. A delay
   scan (`recipes/run-delay.toml`) with the window pinned to the pulse, so the
   integration is correct at every point and the knee in Q(delay) is physics.

Until now `run-delay.toml` used a trigger-referenced window covering the whole
record to "find the zero": method 1 applied to problem 2.

## What was removed

`t0_int_reference = record | trigger | pulse` and the two fixed references.
They were kept for the LabVIEW archive and as a timebase-independent
intermediate; both are now one line of arithmetic away. A `run.toml` that
still names the key is refused with the conversion below. The 2026-08-07
regression (`tests/test_regression_20260807.py`) still calls `charge()` with a
record-time start, which is what that engine did; `charge()` itself is
unchanged apart from the optional end.

## Converting an old value

| old reference | old `t0_int_s` means                | new `t0_int_s` =                                          |
|---------------|--------------------------------------|-----------------------------------------------------------|
| `pulse`       | after `:PULS:DEL1` (+ old command offset) | old − trigger_offset_s (− the old command offset, if `rig.toml` had one) |
| `trigger`     | after the trigger                    | old − delay_ns − trigger_offset_s                         |
| `record`      | after the first sample               | old + trace.t0 − delay_ns − trigger_offset_s (trace.t0 = −timebase_ns_per_div, e.g. −200 ns at 200 ns/div) |

Worked numbers, all with delay 90 ns and 47.1 ns latency:

* LabVIEW panel, `record` 320 ns at 200 ns/div → 320 − 200 − 90 − 47.1 =
  **−17.1 ns**; the panel integrated to the record's end, so the width is
  2000 − 320 = **1680 ns** (`recipes/run-labview*.toml`).
* `run.toml` before, `trigger` 120.5 ns → 120.5 − 90 − 47.1 = **−16.6 ns**.
  `run.toml` now carries −2 ns: the window starts 2 ns before the field rather
  than 17 ns, which the dark subtraction makes harmless either way.
* `run-bace.toml` 2026-09-03, `pulse` 45 ns with `trigger_offset_s = 47e-9`
  then a *command* offset → the same instant as **−2 ns** now.

## The delay axis moved by the old command offset

Before 2026-09-02 this port added 47 ns to `delay_ns` before `:PULS:DEL1`;
`rig.toml` in this repo has said 0 since then, but recipes dated 2026-09-03
were written for a bench where it was still 47 ns (`start = -40` → `:PULS:DEL1
= 7 ns`). A negative `delay_ns` is now refused, and those recipes' axes were
shifted by +40 ns so the commanded `:PULS:DEL1` stays 7 … 447 ns. When
comparing an old Q(delay) against a new one, the old axis is `:PULS:DEL1 −
47 ns` if that bench added the offset, and `:PULS:DEL1` if it did not; the run
folder's `config/rig` attribute `trigger_offset_s` says which — but note that
in files older than this note the same attribute meant the *command* offset.
