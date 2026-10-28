# The integration window is pinned to the pulse

`RunConfig.t0_int_s` is measured from field arrival at the device.
[The transient](manual/2.2-transient.md) explains the timing and the choice of
window. This document defines the coordinate conversion for code and files.

## Window coordinates

    field arrives at   trigger + :PULS:DEL1 + trigger_offset_s
    window (record)    t0_int_s + delay_ns + trigger_offset_s − trace.t0
                       … to … + t_int_width_s

Here `delay_ns` is converted to seconds. `:PULS:DEL1` receives `delay_ns`
unchanged. `trigger_offset_s` positions the window and never reaches the
generator command. Its value is 47.1 ns, from a 49-point fit with a 1.5 ns
residual (`notes.md`).

`trace.t0` is the record origin relative to the trigger (`:WAV:XOR?`).
The nominal origin is −200 ns at 200 ns/div and −500 ns at 500 ns/div.
`:TIM:POS` places the screen centre four of the ten divisions after the
trigger. Processing uses the returned origin.

`t_int_width_s` defaults to 1.5 µs in `RunConfig`; `run.toml` selects 6 µs
and `t0_int_s = -2e-9`. Both ends follow field arrival. The run warns once
if the record ends before the window. `core.process.charge` sums samples in
the interval open at the start and closed at the end, multiplied by `dt`.

Every `StepDone` carries `t0_int_record_s` and `t1_int_record_s`. HDF5 stores
the per-point window in `/traces/window` and origin in `/traces/trace_t0`,
schema `bace-run/3` (`bace/storage/hdf5.py`).

## The two zeros

| Zero | Test | Meaning |
|---|---|---|
| Integration zero | `tools/reintegrate.py run.h5 --sweep=-40:40:2` | Q stops changing as the start moves earlier; the whole transient fits. |
| Light zero | Delay scan with `recipes/run-delay.toml` | The knee in Q(delay) locates light-off at the device. The field-relative window keeps integration comparable at every point. |

## Reading older files

Files can express `t0_int_s` relative to the record, trigger or pulse.
Configurations that name `t0_int_reference` are refused with conversion
guidance. The table converts these values to the field-relative definition.

| old reference | old `t0_int_s` means                | field-relative `t0_int_s` =                                          |
|---------------|--------------------------------------|-----------------------------------------------------------|
| `pulse`       | after `:PULS:DEL1` (+ old command offset) | old − trigger_offset_s (− the old command offset, if `rig.toml` had one) |
| `trigger`     | after the trigger                    | old − delay_ns − trigger_offset_s                         |
| `record`      | after the first sample               | old + trace.t0 − delay_ns − trigger_offset_s (trace.t0 = −timebase_ns_per_div, e.g. −200 ns at 200 ns/div) |

All terms must use seconds. At delay 90 ns and latency 47.1 ns:

| File value | Field-relative start | Width |
|---|---|---|
| LabVIEW `record` 320 ns at 200 ns/div | 320 − 200 − 90 − 47.1 = −17.1 ns | 2000 − 320 = 1680 ns, to record end (`recipes/run-labview.toml`, `recipes/run-labview-same.toml`) |
| `trigger` 120.5 ns | 120.5 − 90 − 47.1 = −16.6 ns | Preserve the file's end or width. |
| `pulse` 45 ns without an added command offset | 45 − 47.1 = −2.1 ns (−2 ns with 47 ns latency) | Preserve the file's end or width. |

For an older delay axis, inspect the file's command-offset convention.
An axis that excludes a 47 ns command offset reads as `:PULS:DEL1 − 47 ns`;
an axis without that offset reads as `:PULS:DEL1`. In those files the
`config/rig` attribute `trigger_offset_s` can denote a command offset rather
than field latency. The supported command requires `delay_ns >= 0`.
The numerical archive regression calls `charge()` with record-time bounds;
that function accepts record time independently of the configuration format.
