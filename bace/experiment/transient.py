"""The transient measurement scans one axis over repeated loops.

Each shot records a light trace and a dark trace, subtracts them, integrates
Q and updates the loop averages. This module controls the bias generator,
shutter and digitiser. `bace.experiment.intensity_series` controls the LED,
relay and DC characterisation.

VISA calls block and sessions are not thread-safe. The synchronous generator
runs on the thread that owns the instruments. The service forwards events to
an asynchronous queue so instrument waits do not block its socket event loop.

Closing the generator raises `GeneratorExit` at a yield. The `finally` block
attempts to disable bias and shut the shutter on completion, abort, exception
or close. An `abort` callable is polled before each shot and emits `RunAborted`
before the same cleanup.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Callable, Iterator

import numpy as np

from ..core.axis import ScanPlan, ScanSpec
from ..core.process import (PRE_TRIGGER_GUARD_S, ChargeAccumulator, RunningAverage,
                            baseline_is_flat, charge, photocurrent)
from ..core.pulses import pulse_levels
from .events import (AxisResolved, Event, InstrumentState, LoopDone, Notice,
                     Progress, RunAborted, RunFinished, RunStarted, StepDone,
                     StepPhase, StepStarted)
from .rig import Rig


@dataclass(frozen=True)
class RunConfig:
    """Run parameters define acquisition, timing and processing.

    The output stores these parameters verbatim beside the bench configuration.
    """

    n_averages: int = 200
    """How many acquisitions the digitiser averages in hardware before it returns one trace.

    Uncorrelated noise falls as `1 / sqrt(n_averages)`; acquisition time grows
    with `n_averages`. Too few acquisitions leave a noisy trace and uncertain Q.
    """

    timebase_ns_per_div: float = 200.0
    """The digitiser's horizontal scale, in ns per division, with ten divisions across the record, so the record duration is ten times this value.

    The record spans ten divisions, with one before the trigger. At 200 ns/div,
    the 2 µs record covers the leading edge of a 5 µs pulse; record duration and
    `pulse_width_ns` are independent. A longer record costs more samples at the
    same sample interval. A short record cuts off the integration window and
    underestimates Q, especially at later delay points.

    The trigger lies 200 ns into the record at 200 ns/div and 500 ns into it at
    500 ns/div. `:TIM:POS` places the screen centre four divisions after the
    trigger. The integration window follows field arrival, so changing the record
    origin does not change the part of the transient it integrates.
    """

    record_length: int = 5000
    """How many samples the digitiser is asked for per trace.

    More samples cost transfer time and storage. The digitiser may return fewer:
    a request for 5000 returns 4000 on this bench (`docs/notes.md`). Consumers use
    the returned length; assuming the request is fulfilled gives a wrong time axis.
    """

    t0_int_s: float = 0.0
    """Where charge integration starts, in s from the field's arrival at the device: the 81150A trigger plus `:PULS:DEL1` and `trigger_offset_s`.

    Field arrival is the trigger plus `:PULS:DEL1` and
    `RigConfig.trigger_offset_s`. A negative start includes samples before the
    step. A few ns of lead include the whole transient; light minus dark removes
    the shared displacement current. Extra lead adds noise.

    The integration window follows the pulse along a delay axis. The 81150A Sync
    is not delayed by `:PULS:DEL1`, so a window tied to the trigger or first sample
    integrates different parts of the transient at different points. That makes
    Q depend on window placement as well as carrier recombination. A delay scan
    therefore needs the same window relative to field arrival at every point.
    Locating light-off from Q against delay requires valid integration at every
    point. The transient start can be checked on stored traces with
    `tools/reintegrate.py`.

    Configurations with `t0_int_reference` are refused with conversion guidance;
    `t0_int_s` always uses field arrival as its reference.
    """

    t_int_width_s: float = 1.5e-6
    """How long the charge integration window lasts, in s, starting at `t0_int_s` relative to field arrival.

    The interval is open at the start and closed at the end. Both ends follow
    the delay; integrating to the record end instead shortens the window at
    later delay points. A wider window adds noise from drift between traces.
    A short window loses extraction current, and a short pulse truncates it.
    The run warns once if the record ends before the integration window.

    A 1.5 µs window spans about six 265 ns extraction time constants measured on
    this bench. Slow tails can extend further: manual 2.2 records 80 % of the
    15 µs charge at 1.5 µs. The width needs checking on each device.
    """

    output_polarity: str = "auto"
    """What to do with the 81150A output polarity (`:OUTP1:POL`): `auto` uses `inverted_output`, `NORM` or `INV` forces polarity, and `leave` preserves it.

    `auto` uses `inverted_output`. `leave` sends no polarity command and records
    the instrument read-back. The read-back costs an instrument query and keeps
    the file's polarity convention explicit.

    The resting device voltage depends on wiring, including reversed terminals.
    A wrong polarity moves extraction to the other pulse edge, 5 µs away for a
    5 µs pulse and outside the default record. The step into V_pre can still
    produce a plausible Q. `leave` allows the bench polarity to remain in force
    when the wiring does not justify selecting one.
    """

    pulse_width_ns: float = 5000.0
    """How long the 81150A holds the collection level V_coll before returning to V_pre, in ns: the extraction pulse width.

    A longer pulse reduces the time available at V_pre in each cycle. It must
    outlast extraction and the integration window; a shorter pulse cuts the
    transient off and underestimates Q. The default 5 µs exceeds the few hundred
    ns extraction time measured on this bench, but slow tails need checking.
    """

    pulse_frequency_hz: float = 500.0
    """The rate both generators run at, in Hz: the 33220A chopping the LED and the 81150A firing the collection pulse.

    The 33220A drives the LED and its Sync arms the 81150A. Their frequencies
    must match. At the bench's 500 Hz, the period is 2 ms and each phase lasts
    1 ms at 50 % duty (manual 1.2). Raising the frequency shortens equilibration
    time; a device that does not reach steady state gives a transient V_oc.
    """
    duty_percent: float = 50.0
    """The fraction of each period at the 33220A waveform's high level, in %; with inverted output, this is the LED's dark fraction.

    At 500 Hz and 50 %, the device has 1 ms of light and 1 ms of dark per cycle.
    The bench uses `:OUTP:POL INV` so rising Sync means light off
    (`bace.service.rigs.LED_EXPECTED_POLARITY`). Under inversion the lit fraction
    is `100 - duty_percent`: increasing the duty shortens illumination. At 50 %
    the fractions coincide. A short on-phase prevents steady-state V_oc.

    `LedDrive.__post_init__` checks the LED threshold, duty range and positive
    frequency. `LedDrive.check_equilibration` tests five time constants but has
    no caller or device time-constant parameter. `LedDrive.on_time_s` computes
    period times duty, which under inversion is the dark time. The operator must
    check equilibration for the device.

    The low level is below the LED threshold. Duty changes on-time, not the
    on-phase intensity, which equals the DC intensity. The shutter separates the
    light trace from the dark trace; both use the same trigger timing.

    `Agilent81150.configure_shape` also writes this duty, but
    `Agilent81150.set_levels` overwrites its width with `pulse_width_ns` on every
    shot. Duty therefore controls LED illumination; `pulse_width_ns` controls
    the collection pulse.
    """

    offset_correct: bool = True
    """Subtract a constant baseline, in A, from light minus dark before charge integration: the mean of the region `offset_region` names.

    It adds no acquisition time. Disabling it leaves a constant offset in Q;
    using a region with extraction current removes signal as well as baseline.
    """

    offset_region: str = "tail"
    """Where baseline correction takes the mean of light minus dark: `tail` uses the last 10 % of the record; `pre-trigger` uses samples before the trigger.

    `tail` uses the last 10 % of the record. `pre-trigger` uses samples before
    the trigger, excluding `PRE_TRIGGER_GUARD_S` for Sync pickup. Both reuse
    stored samples and add no acquisition time.

    The tail is a baseline only after extraction ends. On this bench a 2 µs
    record leaves extraction current in the tail and Q reads 10–20 % low. Longer
    records let amplifier drift enter the tail mean. Before the trigger neither
    trace contains the step. The scope zero is about 1 mV and depends on range,
    so both traces need the same range. This region does not depend on record
    length. Neither region removes drift after the step: a null run and a BACE
    run both give −1.2 ± 2.9 µA per shot for the late-minus-pre-trigger offset
    (`docs/analysis/2026-10-26/scope-window/README.md`).
    """

    alternate_order: bool = False
    """Have the digitiser take the dark trace first on every even loop, so light-then-dark and dark-then-light acquisition alternate.

    Drift between acquisitions then enters Q with opposite signs on successive
    loops and cancels in the mean over an even number of loops. It adds no
    acquisition, but a dark trace taken first requires the range of this point's
    light trace from a preceding loop. Both traces share that range and scope
    zero. A range fitted to the dark trace alone can clip the light trace.

    With `alternate_order` disabled, every shot takes the light trace first.
    Amplifier drift measured on this bench averages −1.2 µA per shot
    (`docs/analysis/2026-10-26/scope-window/README.md`), so a constant order can
    bias the mean Q.
    """

    invert_polarity: bool = False
    """Swap and negate both 81150A bias levels in software before writing them, for a device of the opposite architecture; independent of `inverted_output`.

    It supports the opposite device architecture without another acquisition.
    It is independent of the instrument polarity, `inverted_output`. A wrong
    value reverses the voltage steps and Q; it does not merely disable a feature.
    """

    inverted_output: bool = False
    """Apply inverted rather than normal hardware output polarity on the 81150A when `output_polarity` is `auto`; separate from the software level transformation `invert_polarity`.

    It writes `:OUTP:POL INV` or normal polarity without another acquisition.
    It is independent of the software swap and negation in `invert_polarity`.
    The LabVIEW waveform sequence includes an explicit polarity selection.

    The generator low level holds the device between pulses. With normal
    polarity and a low level corresponding to V_coll, the device rests at V_coll
    and pulses to V_pre; inversion exchanges those phases. Amplifier inversion
    and terminal wiring also determine the device voltages. A wrong selection
    extracts on the wrong edge and can give plausible Q outside the intended
    light-off timing.
    """

    settle_s: float = 0.2
    """How long the run waits with the shutter open and light levels on the 81150A before the digitiser acquires, in s.

    It adds `settle_s` to each shot, plus `shutter_settle_s`. Too short a wait
    records an unsettled device; an excessive wait increases the gap between
    traces and can increase drift noise.
    """

    dark_settle_s: float = 0.2
    """How long the run waits after writing the 81150A dark levels before closing the shutter, in s; skipped with `same` after a light trace.

    It adds time when `dark_reference` is `translated`, or when a dark trace
    comes first and needs its levels written. With `same` after a light trace,
    no levels change and this wait is skipped. Too short a wait records an
    unsettled bias step; an excessive wait increases drift between traces.
    """

    dark_reference: str = "translated"
    """Which voltage levels the 81150A holds for the dark trace: `translated` repeats the swing starting at 0 V; `same` repeats the light levels.

    `translated` repeats the light step's voltage change starting at 0 V.
    For example, a light step from −1.014 to +1.000 V becomes a dark step from
    0.000 to +2.014 V at the generator. Avoiding forward bias limits dark
    injection. It requires a level write and `dark_settle_s`. The absolute voltage
    ranges differ, so displacement current cancels only where capacitance is
    independent of voltage.

    `same` uses the light levels, so only the shutter changes between traces.
    It avoids the level write and dark-level wait after a light trace. The
    identical voltage step cancels displacement current without a capacitance
    mismatch, but holds V_pre in the dark and can sustain injected carriers.
    A wrong reference changes what Q subtracts and can mask device charge.
    """

    light_shutter: str = "open"
    """Where the shutter sits for the light trace: `open` illuminates the sample; `shut` takes a control without light.

    `open` measures under illumination at V_pre. `shut` records both traces
    without light as a control, without adding acquisitions. With a translated
    dark reference, Q then includes charge sustained by the bias at V_pre and
    the capacitive mismatch of the two voltage ranges. With `same`, identical
    steps without light form a null run.

    On this bench Q nearly doubles from 220 to 295 K and is nearly independent
    of intensity at room temperature (`docs/analysis/2026-09-06/README.md`). The
    control tests charge sustained without light; it does not define an additive
    photogenerated share (manual 2.1).

    V_pre still uses V_oc measured under illumination: the DC measurement opens
    the shutter. The LED still pulses and the intensity read-back measures shutter
    leakage. Selecting `shut` for an illuminated measurement records a control
    instead of the intended light trace.
    """

    shutter_settle_s: float = 0.0
    """How long the run waits after the shutter finishes moving before the digitiser acquires, in s, for both traces.

    It adds a wait to both traces, in addition to their level settle times.
    A difference between traces is unreliable if one still contains shutter or
    device settling. Too short a wait records that transient; excessive waits
    increase the time between traces and can increase drift noise.

    Zero preserves the no-wait acquisition contract used with the LabVIEW
    regression in `tests/test_regression_20260807.py`. Bench measurements need
    a settle time checked on the device. The LabVIEW panel's corresponding
    control is `Wait before measure (0.2)s`.
    """

    read_intensity: bool = True
    """Read the 1918-C power meter once per shot and store its optical power, in W, beside the charge.

    The meter samples a beam splitter, so it adds only an instrument round trip
    and does not interrupt the shot. The read-back exposes LED droop during long
    runs. Disabling it leaves `StepDone.intensity_w` empty and loses that record.
    """

    acquisition_timeout_s: float = 30.0
    """How long to wait for the digitiser to return a trace before giving up on the shot, in s.

    A longer limit permits more averaging but delays failure reporting. A short
    limit aborts valid acquisitions; an excessive limit leaves a run waiting for
    a missing trigger, such as an unarmed 81150A.
    """

    external_trigger: bool = True
    """Arm the 81150A from its external trigger input, the 33220A Sync, rather than free-running.

    The run writes the arming mode and records `bias_arm_source` and
    `bias_arm_slope` in `InstrumentState`. It costs setup commands and queries.
    The collection pulse must follow LED light-off; only Sync carries that phase.

    With external triggering disabled, the free-running 81150A still triggers
    the digitiser from its own Sync. Acquisitions can therefore look valid and
    give plausible Q while extraction occurs at an arbitrary LED phase.
    """

    trigger_slope_positive: bool = True
    """Arm the 81150A on the rising rather than falling edge of the 33220A Sync when external triggering is enabled.

    It adds no acquisition time and applies only with `external_trigger` enabled.
    With `:OUTP:POL INV` on the 33220A, rising Sync means light off. Selecting the
    falling edge extracts during generation and changes the meaning of Q.
    """

    trigger_sweep: str = "AUTO"
    """The digitiser's trigger sweep mode: `AUTO` acquires even without a trigger; `TRIG` waits for one; independent of 81150A arming through `external_trigger`.

    The default `AUTO` preserves the recovered LabVIEW SCPI sequence. It acquires
    even without a trigger. `TRIG` waits for an edge, up to
    `acquisition_timeout_s`. This setting does not control 81150A arming.
    A loose Sync cable under `AUTO` produces untriggered noise whose subtraction
    can give plausible near-zero Q; `TRIG` makes the missing edge a timeout.
    """

    calibrate_trigger: bool = False
    """Have the digitiser acquire its trigger channel before the scan and set its edge threshold to `max(CHAN3) * 0.5 * attenuation`, in V.

    It adds a 16-acquisition probe before the scan and sets half the signed Sync
    peak in V, scaled by probe attenuation, as the edge threshold. LabVIEW
    compatibility requires this half-amplitude calibration. The digitiser must
    acquire the trigger channel before a range is set; calibration is disabled
    by default and enabled for the bench. A missing or mis-scaled Sync gives an
    invalid threshold, and `AUTO` can then average untriggered noise.
    """

    def __post_init__(self) -> None:
        if not self.t_int_width_s > 0:
            raise ValueError(
                f"t_int_width_s must be positive, not {self.t_int_width_s!r}: an "
                "empty integration window still produces a number, so this "
                "refuses before an instrument is touched."
            )
        if self.dark_reference not in ("translated", "same"):
            raise ValueError(
                f"dark_reference must be 'translated' or 'same', not "
                f"{self.dark_reference!r}. It decides what the dark trace is a "
                "reference *for*, and a typo would silently pick the default."
            )
        if self.light_shutter not in ("open", "shut"):
            raise ValueError(
                f"light_shutter must be 'open' or 'shut', not {self.light_shutter!r}"
            )
        if self.offset_region not in ("tail", "pre-trigger"):
            raise ValueError(
                f"offset_region must be 'tail' or 'pre-trigger', not "
                f"{self.offset_region!r}")
        if self.output_polarity.lower() not in ("auto", "norm", "inv", "leave"):
            raise ValueError(
                f"output_polarity must be 'auto', 'NORM', 'INV' or 'leave', not "
                f"{self.output_polarity!r}"
            )

    def polarity_instruction(self) -> bool | None:
        """The polarity instruction is the boolean for `Agilent81150.configure_shape`.

        `None` leaves the instrument polarity unchanged.
        """
        p = self.output_polarity.lower()
        if p == "leave":
            return None
        if p == "norm":
            return False
        if p == "inv":
            return True
        return self.inverted_output

    def as_dict(self) -> dict:
        return asdict(self)


def resolve_window(config: RunConfig, trace_t0: float, pulse_delay_s: float,
                   trigger_offset_s: float = 0.0) -> tuple[float, float]:
    """The resolved integration window gives start and end in record time, in s.

    Field arrival is `pulse_delay_s + trigger_offset_s` after the trigger.
    `trace_t0` is the record origin relative to the trigger, from `:WAV:XOR?`;
    it is negative when the trigger lies inside the record. Subtracting it
    converts trigger-relative time to time from the first sample. For example,
    `trace_t0 = -500 ns`, latency 47 ns and `t0_int_s = -2 ns` place the start at
    delay plus 545 ns of record time; the end is `t_int_width_s` later.

    This conversion belongs with the bench timing. `bace.core.process.charge`
    integrates on `arange(n) * dt` and does not depend on cabling or record origin.
    The LabVIEW arithmetic contract is checked by
    `tests/test_regression_20260807.py`.
    """
    start = config.t0_int_s + pulse_delay_s + trigger_offset_s - trace_t0
    return start, start + config.t_int_width_s


PROVISIONAL_TRIGGER_V = 0.5
"""The provisional Sync threshold is 0.5 V during calibration.

A 2 µs record can sample a 5 µs pulse at 500 Hz only when it starts on that
pulse. Calibration therefore needs a valid trigger before measuring the
threshold. An inherited threshold near baseline can free-run and measure
noise despite both generators being enabled. A 0.5 V threshold selects the
TTL Sync; half its measured peak replaces this provisional value.
"""

MIN_SYNC_SWING_V = 0.1
"""The minimum Sync swing is 0.1 V peak to peak at calibration.

The 81150A Sync measures about 1.2 V on this bench; open-channel noise is
below 1 mV. A threshold about one tenth of the Sync swing separates it from
noise. Calibration must undo the current conversion by R_s before comparing
voltages; otherwise the apparent swing is too small.
"""


class BiasOutputError(RuntimeError):
    """The bias output error reports an off read-back after `:OUTP1 ON`."""


class SyncError(RuntimeError):
    """The Sync error stops acquisition when calibration detects no Sync.

    This prevents `AUTO` from recording untriggered noise as data.
    """


AbortCheck = Callable[[], bool]
Sleep = Callable[[float], None]


def _output_state(device) -> str:
    """The output state is `ON`, `OFF` or `?` from the instrument read-back.

    It uses `read_output` when available, otherwise the cached output flag.
    A failed or unknown read-back returns `?`.
    """
    read = getattr(device, "read_output", None)
    try:
        on = read() if callable(read) else bool(device.output_enabled)
    except Exception:                                   # noqa: BLE001
        return "?"
    return "?" if on is None else ("ON" if on else "OFF")


def _resolve_plan(spec: ScanSpec, voc: float | None) -> ScanPlan:
    return spec.plan(voc)


def run_transient_scan(rig: Rig, spec: ScanSpec, config: RunConfig = RunConfig(), *,
                       voc: float | None = None,
                       abort: AbortCheck | None = None,
                       sleep: Sleep = time.sleep) -> Iterator[Event]:
    """The transient scan yields measurement and progress events for one axis.

    `voc`, in V, is required for an axis centred on V_oc. It must match the
    illumination of the shots. The caller that controls the LED applies
    `bace.core.illumination.assert_axis_centre` to enforce that constraint.
    """
    plan = _resolve_plan(spec, voc)
    cfg = rig.config
    t_start = time.monotonic()

    yield RunStarted(description=plan.describe(), n_shots=plan.n_shots,
                     n_steps=plan.n_steps, n_loops=plan.n_loops,
                     config={"run": config.as_dict(), "rig": cfg.as_dict()})
    yield AxisResolved(axis=plan.axis, values=plan.values, voc=voc)

    charges = ChargeAccumulator(n_loops=plan.n_loops, n_steps=plan.n_steps)
    averaged: RunningAverage | None = None
    dt = float("nan")
    window_told = False
    """The window report and the truncation warning each appear at most once."""
    done = 0
    aborted = False

    try:
        # -- setup ---------------------------------------------------------
        rig.scope.configure_timebase(config.timebase_ns_per_div, config.record_length)
        # LabVIEW compatibility requires trigger configuration before waveform
        # configuration. Every run writes arming explicitly: a free-running
        # generator still triggers the digitiser and can give plausible Q at
        # an arbitrary LED phase. See `RunConfig.external_trigger`.
        rig.bias.configure_trigger(external=config.external_trigger,
                                   positive_slope=config.trigger_slope_positive)
        rig.bias.configure_shape(config.pulse_frequency_hz,
                                 duty_percent=config.duty_percent,
                                 inverted_output=config.polarity_instruction())
        pol = rig.bias.output_polarity
        arm = rig.bias.trigger_state()
        left_alone = config.polarity_instruction() is None
        # With `output_polarity = "leave"`, only read-back identifies the
        # polarity. `InstrumentState` persists it beside the configuration;
        # a transient `Notice` cannot provide that file contract. Arming is
        # also read back because a request alone does not prove instrument state.
        yield InstrumentState({
            "bias_output_polarity": pol,
            "bias_polarity_source": "left as found" if left_alone else "set by this run",
            "bias_arm_source": arm.get("arm_source", "?"),
            "bias_arm_slope": arm.get("arm_slope", "?"),
            "dark_reference": config.dark_reference,
            "light_shutter": config.light_shutter,
        })
        yield Notice(
            "info",
            f":OUTP:POL in force = {pol}" + (
                " — left as found, not set by this run. Which level the device "
                "rests at between pulses depends on how the sample is wired, so "
                "this run does not assume it"
                if left_alone else ""))
        # LabVIEW compatibility requires enabling the first point's light
        # levels before trigger calibration. A parked generator supplies no
        # Sync, so calibration without enabling it measures only noise.
        # The shutter remains under the caller's control; the first setpoint
        # under illumination is a measurement condition.
        first = pulse_levels(plan.setpoints[0].vpre, plan.setpoints[0].vcoll,
                             cfg.pulse_amp, plan.setpoints[0].delay_ns,
                             config.pulse_width_ns, invert=config.invert_polarity)
        rig.bias.set_levels(first.high_light, first.low_light,
                            delay_s=first.delay_s, width_s=first.width_s)
        rig.bias.enable_output(True)
        # `read_output`, when available, queries the instrument; otherwise
        # the cached flag is the only state. A refused `:OUTP1 ON` supplies
        # neither pulse nor Sync, so the file needs the output read-back.
        bias_on = _output_state(rig.bias)
        yield InstrumentState({"bias_output": bias_on})
        if bias_on == "OFF":
            # An off read-back proves the generator supplies neither a pulse nor
            # Sync. Continuing cannot produce the intended measurement.
            raise BiasOutputError(
                "the 81150A answers :OUTP1? = 0 after :OUTP1 ON: its output did "
                "not come on, so no pulse reaches the device and no sync reaches "
                "the scope. Check the front panel (Output 1), the error queue, and "
                "whether another program holds the instrument.")

        threshold = None
        if config.calibrate_trigger:
            rig.scope.configure_edge_trigger(cfg.trigger_source,
                                             positive=cfg.trigger_positive,
                                             high_threshold=PROVISIONAL_TRIGGER_V,
                                             sweep=config.trigger_sweep)
            probe = rig.scope.acquire(16, source=cfg.trigger_source,
                                      autorange_first=True,
                                      timeout_s=config.acquisition_timeout_s)
            # `acquire` returns A after division by R_s and multiplication by
            # `current_sign`. Sync and its threshold are in V, so both conversions
            # must be reversed. A missing factor makes the threshold too small;
            # a missing sign reversal takes the noise baseline as the peak of an
            # inverted pulse. `AUTO` can then average untriggered records into a
            # flat trace with plausible near-zero Q.
            volts = np.asarray(probe.y) * cfg.sense_resistor_ohm * cfg.current_sign
            peak = float(np.max(volts) if cfg.trigger_positive else -np.min(volts))
            threshold = peak * 0.5 * cfg.probe_attenuation
            swing = float(np.max(volts) - np.min(volts))
            if swing < MIN_SYNC_SWING_V:
                text = (f"no sync on {cfg.trigger_source}: the trigger channel swings "
                        f"{swing * 1e3:.2g} mV peak to peak during calibration, against "
                        f"the ~1 V the 81150A sync gives. The generator is not being "
                        f"armed, its output is not reaching the device, or the sync "
                        f"cable is off. ")
                yield Notice("warning", text)
                if config.trigger_sweep.upper() == "AUTO":
                    # `AUTO` averages untriggered records into plausible but meaningless
                    # Q. `TRIG` waits for an edge and can time out. Missing Sync must
                    # stop automatic sweeping rather than measure noise.
                    raise SyncError(text + "With trigger_sweep = AUTO every trace "
                                    "would be untriggered noise, so this run stops "
                                    "before the first shot. Set calibrate_trigger = "
                                    "false to acquire regardless.")
            yield Notice("info", f"trigger threshold set to {threshold:.3g} V "
                                 f"from {cfg.trigger_source} (sync peak {peak:.3g} V)")
        rig.scope.configure_edge_trigger(cfg.trigger_source,
                                         positive=cfg.trigger_positive,
                                         high_threshold=threshold,
                                         sweep=config.trigger_sweep)

        # -- the loop ------------------------------------------------------
        sync_failed = False
        # Each point retains its light trace's vertical range and clipping
        # flag. A dark trace acquired first reuses that range: auto-ranging
        # on the dark trace alone can choose a range that clips the light trace.
        light_windows: dict[int, tuple[tuple[float, float], bool]] = {}
        for s in plan:
            if abort is not None and abort():
                aborted = True
                yield RunAborted(reason="requested", done=done, total=plan.n_shots)
                return

            sp = s.setpoint
            axis_value = plan.value_of(s.step)
            yield StepStarted(index=s.index, loop=s.loop, step=s.step,
                              setpoint=sp, axis_value=axis_value)

            levels = pulse_levels(sp.vpre, sp.vcoll, cfg.pulse_amp,
                                  sp.delay_ns, config.pulse_width_ns,
                                  invert=config.invert_polarity)

            # `StepPhase` marks shot segments so the console can distinguish
            # settling from a stalled acquisition.
            same_dark = config.dark_reference == "same"
            # A dark trace can come first only after a light trace sets this
            # point's range, as on even loops when a run starts at loop one.
            dark_first = (config.alternate_order and s.loop % 2 == 0
                          and s.step in light_windows)
            light_phases = ["levels", "light settle", "acquire light"]
            # With `dark_reference = "same"` after a light trace, the levels
            # already match. A dark trace taken first or using `translated`
            # requires its own level write.
            dark_phases = ([] if same_dark and not dark_first else ["dark levels"]) \
                + ["dark settle", "acquire dark"]
            if dark_first:
                light_phases[0] = "light levels"
            phases = (dark_phases + light_phases if dark_first
                      else light_phases + dark_phases) + ["process"]
            of = len(phases)

            def phase(name: str) -> StepPhase:
                return StepPhase(index=s.index, phase=name,
                                 k=phases.index(name) + 1, of=of)

            sync_failed_box = [sync_failed]

            def take(which: str, autorange: bool, window=None):
                trace = rig.scope.acquire(config.n_averages, source=cfg.current_source,
                                          autorange_first=autorange,
                                          timeout_s=config.acquisition_timeout_s,
                                          window=window)
                if autorange:
                    light_windows[s.step] = (rig.scope.window, bool(rig.scope.clipped))
                sync, note = _sync_trace(rig, cfg, sync_failed_box[0])
                notes = []
                if note:
                    sync_failed_box[0] = True
                    notes.append(note)
                note = _averages_notice(trace, config.n_averages, s.index, which)
                if note:
                    notes.append(note)
                return trace, sync, notes

            def light_part(autorange: bool):
                # Levels and shutter changes must precede settling and acquisition.
                yield phase(light_phases[0])
                rig.bias.set_levels(levels.high_light, levels.low_light,
                                    delay_s=levels.delay_s, width_s=levels.width_s)
                if config.light_shutter == "shut":
                    rig.shutter.shut()          # No-light control; see `RunConfig`.
                else:
                    rig.shutter.unblock()
                yield phase("light settle")
                sleep(config.settle_s + config.shutter_settle_s)
                intensity = None
                if config.read_intensity and rig.power is not None:
                    intensity = rig.power.read_power()
                yield phase("acquire light")
                trace, sync, notes = take("light", autorange)
                yield from notes
                return trace, sync, intensity

            def dark_part(autorange: bool, window=None):
                # With `same` after a light trace, only the shutter changes.
                # Rewriting identical levels and waiting for them would add delay
                # between traces without a level change to settle.
                if "dark levels" in dark_phases:
                    yield phase("dark levels")
                    dark_levels = ((levels.high_light, levels.low_light) if same_dark
                                   else (levels.high_dark, levels.low_dark))
                    rig.bias.set_levels(*dark_levels, delay_s=levels.delay_s,
                                        width_s=levels.width_s)
                    sleep(config.dark_settle_s)
                yield phase("dark settle")
                rig.shutter.shut()
                sleep(config.shutter_settle_s)
                yield phase("acquire dark")
                trace, sync, notes = take("dark", autorange, window)
                yield from notes
                return trace, sync

            # Both traces share a vertical range and scope zero. A light trace
            # sets the range; a dark trace taken first uses the range of this
            # point's light trace from the preceding loop.
            if dark_first:
                window, clipped = light_windows[s.step]
                dark, sync_dark = yield from dark_part(autorange=False, window=window)
                light, sync_light, intensity = yield from light_part(autorange=False)
            else:
                light, sync_light, intensity = yield from light_part(autorange=True)
                clipped = light_windows[s.step][1]
                dark, sync_dark = yield from dark_part(autorange=False)
            sync_failed = sync_failed_box[0]
            yield phase("process")

            # -- process ---------------------------------------------------
            dt = light.dt
            first = averaged is None
            # Field arrival moves with `:PULS:DEL1`, so the integration bounds
            # must follow it at every delay point.
            t0_rec, t1_rec = resolve_window(config, light.t0, levels.delay_s,
                                            cfg.trigger_offset_s)
            record_end = (light.n - 1) * dt
            if first:
                yield Notice(
                    "info",
                    f"integration window: {config.t0_int_s * 1e9:+.1f} ns from the "
                    f"field's arrival ({levels.delay_s * 1e9:.1f} ns delay + "
                    f"{cfg.trigger_offset_s * 1e9:.1f} ns latency after the trigger) "
                    f"for {config.t_int_width_s * 1e9:.0f} ns; this record puts the "
                    f"trigger {-light.t0 * 1e9:.1f} ns in, so that is "
                    f"{t0_rec * 1e9:.1f} .. {t1_rec * 1e9:.1f} ns of record time")
                if not (0.0 <= t0_rec < record_end):
                    yield Notice(
                        "warning",
                        f"the integration window starts at "
                        f"{t0_rec * 1e9:.1f} ns, outside the "
                        f"{record_end * 1e9:.0f} ns record — every charge "
                        "will be the whole trace or none of it")
            if t1_rec > record_end and not window_told:
                window_told = True
                yield Notice(
                    "warning",
                    f"the integration window ends at {t1_rec * 1e9:.0f} ns but the "
                    f"record ends at {record_end * 1e9:.0f} ns (delay "
                    f"{sp.delay_ns:g} ns): the window is cut short there, and "
                    "along a delay axis by a different amount at each point. A "
                    "longer timebase or a shorter t_int_width_s makes it whole")
            baseline_end = None
            if config.offset_correct and config.offset_region == "pre-trigger":
                # `-light.t0` locates the trigger in record time. `:WAV:XOR?` gives
                # the first sample's time relative to the trigger.
                baseline_end = -light.t0 - PRE_TRIGGER_GUARD_S
                if first:
                    yield Notice(
                        "info",
                        f"baseline: light − dark before {baseline_end * 1e9:.0f} ns of "
                        f"record time ({int(baseline_end / dt) + 1} samples before the "
                        "trigger)")
            if config.offset_correct and config.offset_region == "tail" and first:
                flat, head, tail, peak = baseline_is_flat(light.y, dark.y)
                if not flat:
                    yield Notice(
                        "warning",
                        f"the tail of light − dark is not a baseline: it sits at "
                        f"{tail * 1e3:.3g} mA against a head of {head * 1e3:.3g} mA "
                        f"and a peak of {peak * 1e3:.3g} mA. `offset_correct` "
                        f"subtracts that tail, so it is removing signal, not "
                        f"offset — the transient has not decayed inside the "
                        f"record. Q will look plausible and be wrong. Both raw "
                        f"traces are stored, so this is recoverable offline")
            photo = photocurrent(light.y, dark.y, dt,
                                 offset_correct=config.offset_correct,
                                 baseline_end_s=baseline_end)
            if averaged is None:
                averaged = RunningAverage(n_steps=plan.n_steps, n_samples=photo.size)
            photo_avg = averaged.update(s.step, s.loop, photo)

            q = charge(photo, dt, t0_rec, t1_rec)
            charges.add(s.loop, s.step, q)
            q_mean, q_std = charges.summary()

            done += 1
            yield StepDone(index=s.index, loop=s.loop, step=s.step, setpoint=sp,
                           axis_value=axis_value, light=light, dark=dark,
                           photo=photo, photo_averaged=photo_avg, q=q,
                           q_mean=float(q_mean[s.step - 1]),
                           q_std=float(q_std[s.step - 1]),
                           intensity_w=intensity,
                           # `Digitizer.clipped` is required. A `False` default hides
                           # missing attributes and marks clipped shots valid.
                           # `tests/test_architecture.py` checks the protocol.
                           # The flag describes the range shared by both traces.
                           clipped=clipped,
                           sync_light=sync_light, sync_dark=sync_dark,
                           t0_int_record_s=t0_rec, t1_int_record_s=t1_rec,
                           dark_first=dark_first)

            elapsed = time.monotonic() - t_start
            per = elapsed / done if done else 0.0
            yield Progress(done=done, total=plan.n_shots, elapsed_s=elapsed,
                           eta_s=per * (plan.n_shots - done) if done else None)

            if s.step == plan.n_steps:
                m, sd = charges.summary()
                yield LoopDone(loop=s.loop, q_mean=m.copy(), q_std=sd.copy())

        q_mean, q_std = charges.summary()
        yield RunFinished(axis=plan.axis, values=plan.values, q_mean=q_mean,
                          q_std=q_std, q_all=charges.all_charges,
                          photo_averaged=(averaged.traces if averaged is not None
                                          else np.empty((plan.n_steps, 0))),
                          dt=dt, elapsed_s=time.monotonic() - t_start)

    finally:
        # Cleanup attempts to remove bias and light on completion, abort,
        # exception and generator close, including a consumer ending iteration.
        try:
            rig.bias.disable_output()
        except Exception:
            pass
        try:
            rig.shutter.shut()
        except Exception:
            pass
        _ = aborted


def _sync_trace(rig: Rig, cfg: RigConfig, already_failed: bool):
    """The Sync fetch returns the trigger trace in V from the acquired record.

    It runs after `Digitizer.acquire` stops the digitiser, so the Sync belongs to
    the same record as the current trace and supports the shot's timing verdict.
    A digitiser without `fetch_volts` returns `None` without a warning. A fetch
    failure returns `None` and a `Notice` only on the first failure, so an
    unavailable channel does not stop the run or flood the event stream.
    """
    fetch = getattr(rig.scope, "fetch_volts", None)
    if fetch is None:
        return None, None
    try:
        return fetch(cfg.trigger_source), None
    except Exception as exc:                                # noqa: BLE001
        if already_failed:
            return None, None
        return None, Notice("warning",
                            f"the sync trace on {cfg.trigger_source} could not be fetched "
                            f"beside the current trace ({type(exc).__name__}: {exc}); "
                            "the shots of this run carry no sync edge and the "
                            "verdict cannot say which side of the trigger chain "
                            "jitters. Check that the channel is displayed")


def _averages_notice(trace: Trace, asked: int, index: int, which: str):
    """The averages check returns a warning for a trace below the requested N_avg.

    It returns `None` when the count meets the request or is unavailable.
    """
    count = getattr(trace, "count", None)
    if count is None or count >= int(asked):
        return None
    return Notice("warning",
                  f"shot {index}: the {which} trace averages {count} acquisitions, not "
                  f"the {int(asked)} asked for -- the digitiser could not wait on its "
                  "averager's count and fetched on the per-acquisition flag instead. "
                  "Its noise is higher than the recipe's and its Q is a single "
                  "shot's, not an average's")
