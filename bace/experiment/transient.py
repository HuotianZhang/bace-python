"""The standalone transient measurement — one axis, N loops, on real or simulated instruments.

This is `TDCF-BACE.vi` rewritten: the engine that sweeps one axis, takes a
light and a dark trace at every point, subtracts, integrates, and averages over
loops. It commands the bias generator, the shutter and the scope, and nothing
else. Illumination and DC characterisation belong to the layer above
(`experiment.intensity_series`), exactly as they did in the original — the
standalone VI had no 33220A, no Keithley and no relay.

**Why a plain generator and not an async one.** The architecture note called for
an async generator. That is wrong here for a concrete reason: every call inside
this loop is blocking VISA I/O, and VISA sessions are not thread-safe, so this
code must run on the one thread that owns the instruments. An `async def`
generator whose body blocks for two seconds would stall the service's event
loop for two seconds. So the run is a synchronous generator executed on the
instrument thread, and the service adapts it to `async` at its own edge by
pumping events into an asyncio queue. `async` belongs where the waiting is on
sockets, not where it is on an oscilloscope.

**Abort** has two paths, and both unwind the same way. A consumer that stops
iterating (or calls `.close()`) raises `GeneratorExit` at the yield, and the
`finally` disables the bias output and shuts the shutter. A consumer that wants
a clean, reported stop passes an `abort` callable, which is polled once per
step and produces a `RunAborted` event before the same unwinding. Nothing is
left driving the sample either way.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Callable, Iterator

import numpy as np

from ..core.axis import ScanPlan, ScanSpec
from ..core.process import (ChargeAccumulator, RunningAverage, baseline_is_flat,
                            charge, photocurrent)
from ..core.pulses import pulse_levels
from .events import (AxisResolved, Event, InstrumentState, LoopDone, Notice,
                     Progress, RunAborted, RunFinished, RunStarted, StepDone,
                     StepPhase, StepStarted)
from .rig import Rig


@dataclass(frozen=True)
class RunConfig:
    """The measurement recipe. Copied verbatim into the output."""

    n_averages: int = 200
    """How many traces the scope averages in hardware before it hands one
    back. Noise falls as 1/sqrt(n); time rises as n."""

    timebase_ns_per_div: float = 200.0
    """The scope's horizontal scale, in nanoseconds per division with ten
    divisions across the record — so the record is ten times this.

    200 is what the LabVIEW panel shows (`Timebase (200 ns) = 200E+0`) and what
    the 2026-08 archive was taken with. It briefly defaulted to 500 here on the
    argument that ten divisions should equal `pulse_width_ns`; a screenshot of
    the panel on 2026-09-01 shows Timebase 200 ns *and* Pulse Width 5e3 ns side
    by side, so they were never meant to be equal. The record is a 2 us window
    onto a 5 us pulse: only the leading edge is in it, which is the edge the
    measurement is about.

    It also fixes `t0_int_s`: at 200 ns/div the trigger sits 200 ns into the
    record, so the panel's record-referenced 320 ns is 120 ns after the trigger
    -- which is what `run.toml` carries as `trigger` + 1.185e-7.
    """

    record_length: int = 5000
    """How many samples the scope is asked for per trace. It may return fewer
    — 5000 gave 4000 in the 2026-08-07 archive — so nothing here assumes it got
    what it asked for."""

    t0_int_s: float = 3.18e-7
    """Where charge integration starts, in the units `t0_int_reference` names."""

    t0_int_reference: str = "record"
    """`record`, `trigger` or `pulse` — what `t0_int_s` is measured from.

    **`record`** measures from the first sample. That is what the original
    engine did and what the 2026-08-07 regression reproduces to 1.6e-06, so it
    stays the default and nothing about the validated path changes.

    Its weakness is that the trigger's position inside the record is a function
    of the timebase (see `timebase_ns_per_div`), so a record-time window
    silently slides relative to the signal whenever the timebase changes —
    318 ns of record time is 118.5 ns *after* the trigger at 200 ns/div and
    182 ns *before* it at 500 ns/div.

    **`trigger`** measures from the trigger instead, using the instrument's own
    `:WAV:XOR?`, which every `Trace` carries as `t0`. The two agree exactly at
    the archive's geometry — 118.5 ns after a trigger sitting 199.5 ns into the
    record is 318.0 ns of record time, i.e. `3.18e-7` — so this is a change of
    spelling, not of value. Prefer it for new work; `run.toml` selects it.

    **`pulse`** measures from the applied pulse edge, i.e. from `:PULS:DEL1`.
    The scope triggers on the 81150A's *Sync*, which is **not** delayed by
    `:PULS:DEL1` — only the output is. So along a delay axis the transient
    slides through the record while a trigger-referenced window stands still,
    and each point gets a different slice of its own transient. Q(delay) is then
    half physics and half window. `pulse` makes the window travel with the
    transient, which is what a delay scan of a *measurement* wants.

    (For a delay scan whose job is to *find* the zero, `trigger` with
    `t0_int_s = 0` is the neutral choice instead: the window covers the whole
    post-trigger record, so every point is treated identically without assuming
    where the transient is. See `run-delay.toml`.)
    """

    output_polarity: str = "auto"
    """What to do with the 81150A's output polarity (`:OUTP1:POL`): `auto`,
    `NORM`, `INV` or `leave`.

    `auto` uses `inverted_output`, which is what every run did before this
    existed. `leave` writes nothing: the polarity found on the instrument is
    read back and reported instead.

    `leave` is not laziness. Which level the device rests at between pulses
    depends on how the sample is wired, and that is not always known — the
    device's own terminals may be reversed. Writing a polarity then is a guess,
    and a wrong guess puts the extraction on the *other* edge of the pulse,
    5 us away and outside the record, while still producing a transient (the
    step into prebias) that integrates to a plausible charge. Declining to set
    it, and recording what was there, is the honest option.
    """

    pulse_width_ns: float = 5000.0
    """How long the 81150A holds the collection level before returning -- the
    width of the extraction pulse itself. It has to outlast the extraction:
    a pulse shorter than the charge takes to come out cuts the transient off
    mid-integration and under-reports Q. 5 us against a transient that is over
    in a few hundred ns."""

    pulse_frequency_hz: float = 500.0
    """The rate **both** generators run at: the 33220A chopping the LED and
    the 81150A firing the collection pulse. Both were found at 500 Hz on the
    rig (2026-08-31), not the 1 kHz assumed from the recovered code. The 81150A
    is armed by the 33220A sync, so the two must match; 500 Hz gives a 2 ms
    period and a 1 ms on-phase."""
    duty_percent: float = 50.0
    """The LED's light/dark split within one period, written to the 33220A
    (`FUNC:PULS:DCYC`). At 500 Hz and 50 % the device gets 1 ms of light and
    1 ms of dark per cycle. What that buys is the **on-phase**: the device has
    to reach its light V_oc before the collection pulse fires, which is why
    `LedDrive.check_equilibration` refuses an on-phase under 5 tau. There is no
    duty-cycle dimming to trade against it -- the low level sits below the LED's
    turn-on threshold, so the diode is fully off rather than dim, and the
    on-phase intensity is exactly the DC intensity.

    It is *not* what separates the two traces. `run_transient_scan` takes the
    light trace with the shutter open and the dark reference with it shut, at
    the same trigger timing, and the photocurrent is that difference -- the
    shutter is the light switch, not the duty boundary.

    The same number is written to the 81150A too, by `configure_shape`, where
    it does not survive: `set_levels` writes `PULS:WIDT` from `pulse_width_ns`
    afterwards and again on every shot, and the width is what the collection
    pulse actually has. So this controls the lamp, not the extraction."""

    offset_correct: bool = True
    """Subtract the mean of the last 10 % of the scope's photocurrent record
    from the whole trace, taking a constant baseline off with it."""

    invert_polarity: bool = False
    """Swap and negate both 81150A bias levels in software, before they are
    written, for a device of the opposite architecture — the original's
    `New Sample?`. A sign convention, not a feature —
    getting it wrong flips the charge, it does not merely disable something.

    This is the *software* half. `inverted_output` below is the instrument half.
    They are independent, and the two names are close enough that this docstring
    spent a while attached to the wrong one."""

    inverted_output: bool = False
    """`:OUTP:POL INV` on the 81150A — a *different* knob from `invert_polarity`.

    `invert_polarity` is the MathScript half of the original's `New Sample?`:
    it swaps and negates the two computed levels. This is the other half.
    `Agilent 81150StandardWaveformTDCF20131120.vi` calls
    `Configure Output Polarity.vi` from a case structure whose two frames hold
    `Polarity = 0 (Normal)` and `Polarity = 1 (Inverted)`, so the original sets
    one or the other per shot -- and which frame goes with which sample type was
    an open question, answered by measurement rather than by the binary.

    It matters because the 81150A's *low* level is what the device sits at
    between pulses. With NORM and `LoLvl = Vcoll` the device is held in
    extraction and pulsed to V_pre; with INV it is held at V_pre and pulsed to
    V_coll, which is BACE as the physics describes it.
    """

    settle_s: float = 0.2
    """How long the run waits once the shutter is open and the light levels
    are on the 81150A, before the scope acquires."""

    dark_settle_s: float = 0.2
    """How long the run waits once the 81150A holds the dark levels, before
    the shutter closes."""

    dark_reference: str = "translated"
    """Which levels the 81150A holds for the dark trace: `"translated"` |
    `"same"`.

    `"translated"` is what this port reconstructed from the VI and what
    `core.pulses.pulse_levels` returns a second pair of levels for: the dark
    trace repeats the same voltage *swing* shifted so it starts at 0 V, e.g.
    light `-1.014 -> +1.000 V` becomes dark `0.000 -> +2.014 V`. The stated
    reason is to keep the diode out of forward bias, where in the dark it would
    inject heavily. The cost is that the two traces are taken over different
    absolute ranges, so the capacitive term only cancels where C(V) is flat.

    `"same"` leaves the levels alone and lets **the shutter be the only thing
    that changes** between the two traces. The voltage step is then identical,
    so the displacement current cancels exactly rather than to within C(V) --
    at the price of holding the device at the prebias level in the dark.

    Which one the LabVIEW engine does has not been read off the block diagram;
    the reconstruction says translated. `"same"` is here to be run against it.
    """

    shutter_settle_s: float = 0.0
    """How long the run waits after the shutter has finished moving, before
    the scope acquires. **Applied to both traces.**

    Until 2026-09-01 the two branches were not symmetric: the light trace waited
    `settle_s` after `unblock()`, while the dark trace slept `dark_settle_s`
    *before* `shut()` and then acquired the instant the shutter closed. So the
    dark trace was taken with neither the mechanical shutter nor the device
    given any time to settle, and the light trace was the only one that got any.
    A difference of two traces cannot be trusted when one of them is measured
    during a transient the other was allowed to finish.

    Zero by default, which preserves the archive behaviour for the regression;
    a real measurement should set it. The LabVIEW panel's own analogue is
    `Wait before measure (0.2)s`.
    """

    read_intensity: bool = True
    """Read the 1918-C power meter once per shot and store the number beside
    the charge. The meter sits behind a beam splitter, so this costs nothing
    but a serial round trip -- and it is the only record of the LED drooping
    during a long scan, which is a real effect this rig showed (see the LED
    settle note in `service/modules.py`). Off leaves `intensity_w` empty."""

    acquisition_timeout_s: float = 30.0
    """How long to wait for the scope to return a trace before giving up on
    the shot. Not a measurement setting: it is the guard against a run that
    hangs for ever because the 81150A never armed and the scope is waiting for
    a trigger that will not come."""

    external_trigger: bool = True
    """Arm the 81150A from its external trigger input -- the 33220A Sync --
    rather than free-running. The panel's "ext. trig?" toggle. True is BACE:
    the collection pulse has to be phased to the LED cycle, and only the Sync
    carries that phase.

    Written by the run, not inherited. Until 2026-09-02 only `tools/scan.py`
    armed the generator, in its pre-flight; `run_transient_scan` itself sent
    nothing, so a run started any other way -- the bench harness, the
    service, a front panel left in IMM -- pulsed at whatever the instrument
    held. That is dangerous precisely because it is silent: the scope
    triggers on the 81150A's *own* Sync, so a free-running generator still
    triggers every acquisition and still produces a transient of plausible
    size, at a random phase of the LED cycle. So the arming is written on
    every run and read back into `InstrumentState` as `bias_arm_source` and
    `bias_arm_slope`, beside the polarity.
    """

    trigger_slope_positive: bool = True
    """Arm the 81150A on the rising edge of the external trigger — the 33220A
    Sync. With the 33220A at
    `:OUTP:POL INV` the rising Sync edge is light-off, the edge extraction
    has to follow; arming on the falling edge would extract in the middle of
    generation. Moot when `external_trigger` is False."""

    trigger_sweep: str = "AUTO"
    """**The scope's** trigger sweep mode, `AUTO` or `TRIG` — not the 81150A's
    arming, which is `external_trigger` beside it. The recovered driver used
    AUTO, so that is the default.
    AUTO sweeps anyway when no trigger arrives, so a loose sync cable produces
    untriggered noise whose dark subtraction cancels to nearly zero charge — a
    plausible result from a disconnected cable. TRIG waits instead, turning that
    into a timeout. Worth switching once the trigger path is known good."""

    calibrate_trigger: bool = False
    """Have the scope acquire its trigger channel once before the scan and set
    its edge threshold to `max(CHAN3) * 0.5 * attenuation`, as the original did.
    Needs a digitizer that can acquire that channel before any range has been
    set, so it is off by default and switched on for the real rig."""

    def __post_init__(self) -> None:
        if self.t0_int_reference not in ("record", "trigger", "pulse"):
            raise ValueError(
                f"t0_int_reference must be 'record', 'trigger' or 'pulse', not "
                f"{self.t0_int_reference!r}. A run whose integration window is "
                "measured from nothing in particular still produces numbers, so "
                "this refuses before an instrument is touched."
            )
        if self.dark_reference not in ("translated", "same"):
            raise ValueError(
                f"dark_reference must be 'translated' or 'same', not "
                f"{self.dark_reference!r}. It decides what the dark trace is a "
                "reference *for*, and a typo would silently pick the default."
            )
        if self.output_polarity.lower() not in ("auto", "norm", "inv", "leave"):
            raise ValueError(
                f"output_polarity must be 'auto', 'NORM', 'INV' or 'leave', not "
                f"{self.output_polarity!r}"
            )

    def polarity_instruction(self) -> bool | None:
        """What to hand `configure_shape`. `None` means leave it alone."""
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


def resolve_t0_int(config: RunConfig, trace_t0: float,
                   pulse_delay_s: float = 0.0) -> float:
    """`config.t0_int_s` expressed in **record** time, which is what `charge` wants.

    A `Trace`'s `t0` is the instrument's `:WAV:XOR?` — the record's own origin
    relative to the trigger — and is negative whenever the trigger sits inside
    the record, which it always does here. So a window `x` after the trigger
    begins at `x - t0` of record time: 118.5 ns after a trigger at t0 =
    -199.5 ns is 318.0 ns, the archive's number.

    Kept out of `core.process` deliberately: `charge()` is validated numerics
    and integrates on `arange(n) * dt`, full stop. Where the window *starts* is
    a question about the instrument's record, and belongs on this side of the
    boundary.
    """
    if config.t0_int_reference == "record":
        return config.t0_int_s
    if config.t0_int_reference == "pulse":
        # The pulse is `:PULS:DEL1` after the arm; the Sync the scope triggers
        # on is not. So a window pinned to the pulse has to travel with it.
        return config.t0_int_s + pulse_delay_s - trace_t0
    return config.t0_int_s - trace_t0


PROVISIONAL_TRIGGER_V = 0.5
"""Where the scope triggers on the sync channel while the sync is being
measured. The calibration is circular otherwise: `max(CHAN3)` over a 2 us
record only sees a 5 us pulse at 500 Hz if the record *starts on it*, and the
scope only starts on it if it is already triggering on it. Every earlier
calibration inherited a trigger level the LabVIEW VI or a previous session
had left on CHAN3; session 125751 on the rig (2026-09-02) inherited the
0.25 mV level of the session before it, free-ran, and measured 4.5 mV of a
1.2 V sync with both generators demonstrably on. Half a volt triggers a TTL
sync and nothing else; the measured half-amplitude replaces it afterwards."""

MIN_SYNC_SWING_V = 0.1
"""The least peak-to-peak swing on the trigger channel that counts as a sync.
The 81150A sync into the scope is about 1.2 V (bench sessions of 2026-09-01
calibrated 0.115 V thresholds from 0.23 V of it seen through the 1/R
division); noise on an open CHAN3 is under a millivolt. A tenth of the
real thing is a generous floor, and anything under it is not a trigger."""


class BiasOutputError(RuntimeError):
    """`:OUTP1 ON` was sent and the 81150A still reports its output off."""


class SyncError(RuntimeError):
    """The trigger channel carried no sync at calibration, and the run refused
    to acquire untriggered noise as data."""


AbortCheck = Callable[[], bool]
Sleep = Callable[[float], None]


def _output_state(device) -> str:
    """`ON` / `OFF` / `?` -- from the instrument when the driver has a
    `read_output`, else from its cached flag."""
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
    """Run one scan, yielding events as it goes.

    `voc` is required if the axis is centred on V_oc, and must have been
    measured under the illumination that is in force now — `core.illumination`
    has the guard for that; the caller that owns the LED is the one that can
    apply it.
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
    t0_int_record: float | None = None
    """`config.t0_int_s` in record time. Not known until the first trace reports
    where its own record begins, so it is resolved once and reused."""
    done = 0
    aborted = False

    try:
        # -- setup ---------------------------------------------------------
        rig.scope.configure_timebase(config.timebase_ns_per_div, config.record_length)
        # Arming before the shape, as `Agilent 81150StandardWaveformTDCF` does
        # (Configure Trigger, then Configure Standard Waveform). Written on
        # every run: a generator left free-running by the front panel still
        # triggers the scope and still yields a plausible charge, at a random
        # phase of the LED cycle -- see `RunConfig.external_trigger`.
        rig.bias.configure_trigger(external=config.external_trigger,
                                   positive_slope=config.trigger_slope_positive)
        rig.bias.configure_shape(config.pulse_frequency_hz,
                                 duty_percent=config.duty_percent,
                                 inverted_output=config.polarity_instruction())
        pol = rig.bias.output_polarity
        arm = rig.bias.trigger_state()
        left_alone = config.polarity_instruction() is None
        # The readback, not the request, is what a later reader needs: with
        # `output_polarity = "leave"` the recipe says nothing about which
        # convention ran, so only this reaches the file. A Notice goes to the
        # terminal and is gone; InstrumentState is written beside the config.
        # The arming is read back for the same reason: what was *sent* is in
        # the run config, what the instrument *holds* is here.
        yield InstrumentState({
            "bias_output_polarity": pol,
            "bias_polarity_source": "left as found" if left_alone else "set by this run",
            "bias_arm_source": arm.get("arm_source", "?"),
            "bias_arm_slope": arm.get("arm_slope", "?"),
            "dark_reference": config.dark_reference,
        })
        yield Notice(
            "info",
            f":OUTP:POL in force = {pol}" + (
                " — left as found, not set by this run. Which level the device "
                "rests at between pulses depends on how the sample is wired, so "
                "this run does not assume it"
                if left_alone else ""))
        # The generator is enabled BEFORE the scope calibrates its trigger, at
        # the first step's light levels, the shutter wherever the caller left
        # it (a lit device at its first setpoint is a shot, not a hazard) -- the
        # order `Agilent 81150StandardWaveformTDCF` and then the scope VIs
        # take. Until 2026-09-02 the calibration came first, and it worked only
        # because every earlier run inherited an 81150A the LabVIEW VI had left
        # ON. The service parks the bench after every run, so the first run
        # from a parked bench (session 115857) calibrated against a silent
        # CHAN3: 5.2 mV peak to peak, no sync, no measurement.
        first = pulse_levels(plan.setpoints[0].vpre, plan.setpoints[0].vcoll,
                             cfg.pulse_amp, plan.setpoints[0].delay_ns,
                             config.pulse_width_ns, invert=config.invert_polarity,
                             trigger_offset_s=cfg.trigger_offset_s)
        rig.bias.set_levels(first.high_light, first.low_light,
                            delay_s=first.delay_s, width_s=first.width_s)
        rig.bias.enable_output(True)
        # Read back where the driver can (`read_output` is not a protocol
        # member; the real driver asks `:OUTP1?`), else the cached flag: a
        # generator that did not take `:OUTP1 ON` gives no sync and no
        # pulse, and the file should say which it was.
        bias_on = _output_state(rig.bias)
        yield InstrumentState({"bias_output": bias_on})
        if bias_on == "OFF":
            # The instrument, not the driver, says the output did not come
            # on. Nothing downstream can work -- no pulse, no sync -- and
            # the operator asked, watching the panel, whether it ever did.
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
            # `acquire` returns amps in the rig's sign convention: the
            # digitizer divides by the sense resistor and multiplies by
            # `current_sign`. The sync on CHAN3 is a voltage, and the scope
            # wants a voltage threshold, so undo both. Until 2026-09-02 this
            # took max() of the amps directly: a factor 1/R nobody noticed
            # (0.115 V on a 1.2 V sync still triggers), and then, the day
            # `current_sign = -1` landed, a sign flip that turned the sync
            # into a negative pulse whose max() is the baseline noise -- the
            # first service run on the rig set a 0.25 mV threshold and, with
            # `trigger_sweep = AUTO`, averaged twenty untriggered records
            # into a flat trace and a charge of 1e-12 C.
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
                    # AUTO would sweep anyway, average untriggered records
                    # and report a plausible, meaningless charge; TRIG would
                    # at least time out. Refuse rather than measure noise.
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
                                  invert=config.invert_polarity,
                                  trigger_offset_s=cfg.trigger_offset_s)

            # The shot's segments, announced as each starts (`StepPhase`),
            # so a live view can tell a settle from a hung acquisition.
            # Nothing about the measurement changes: these are yields
            # between the same instrument calls in the same order.
            same_dark = config.dark_reference == "same"
            phases = ["levels", "light settle", "acquire light"]
            phases += [] if same_dark else ["dark levels"]
            phases += ["dark settle", "acquire dark", "process"]
            of = len(phases)

            def phase(name: str) -> StepPhase:
                return StepPhase(index=s.index, phase=name,
                                 k=phases.index(name) + 1, of=of)

            # light: levels first, then shutter, then acquire with autorange
            yield phase("levels")
            rig.bias.set_levels(levels.high_light, levels.low_light,
                                delay_s=levels.delay_s, width_s=levels.width_s)
            rig.shutter.unblock()
            yield phase("light settle")
            sleep(config.settle_s + config.shutter_settle_s)

            intensity = None
            if config.read_intensity and rig.power is not None:
                intensity = rig.power.read_power()

            yield phase("acquire light")
            light = rig.scope.acquire(config.n_averages, source=cfg.current_source,
                                      autorange_first=True,
                                      timeout_s=config.acquisition_timeout_s)

            # dark: shutter closed, range inherited. With `dark_reference =
            # "same"` nothing but the shutter moves, so the levels are not
            # rewritten at all -- and the settle before the shutter has nothing
            # to settle.
            if same_dark:
                dark_levels = (levels.high_light, levels.low_light)
            else:
                yield phase("dark levels")
                dark_levels = (levels.high_dark, levels.low_dark)
                rig.bias.set_levels(*dark_levels, delay_s=levels.delay_s,
                                    width_s=levels.width_s)
                sleep(config.dark_settle_s)
            yield phase("dark settle")
            rig.shutter.shut()
            sleep(config.shutter_settle_s)
            yield phase("acquire dark")
            dark = rig.scope.acquire(config.n_averages, source=cfg.current_source,
                                     autorange_first=False,
                                     timeout_s=config.acquisition_timeout_s)
            yield phase("process")

            # -- process ---------------------------------------------------
            dt = light.dt
            first = t0_int_record is None
            # Recomputed every step: a `pulse`-referenced window travels with
            # `:PULS:DEL1`, so it is not a constant along a delay axis.
            t0_int_record = resolve_t0_int(config, light.t0,
                                           pulse_delay_s=levels.delay_s)
            if first:
                if config.t0_int_reference in ("trigger", "pulse"):
                    yield Notice(
                        "info",
                        f"integration starts {config.t0_int_s * 1e9:.1f} ns after "
                        f"the {config.t0_int_reference}; this record puts the trigger "
                        f"{-light.t0 * 1e9:.1f} ns in, so that is "
                        f"{t0_int_record * 1e9:.1f} ns of record time")
                if not (0.0 <= t0_int_record < (light.n - 1) * dt):
                    yield Notice(
                        "warning",
                        f"the integration window starts at "
                        f"{t0_int_record * 1e9:.1f} ns, outside the "
                        f"{(light.n - 1) * dt * 1e9:.0f} ns record — every charge "
                        "will be the whole trace or none of it")
            if config.offset_correct and first:
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
                                 offset_correct=config.offset_correct)
            if averaged is None:
                averaged = RunningAverage(n_steps=plan.n_steps, n_samples=photo.size)
            photo_avg = averaged.update(s.step, s.loop, photo)

            q = charge(photo, dt, t0_int_record)
            charges.add(s.loop, s.step, q)
            q_mean, q_std = charges.summary()

            done += 1
            yield StepDone(index=s.index, loop=s.loop, step=s.step, setpoint=sp,
                           axis_value=axis_value, light=light, dark=dark,
                           photo=photo, photo_averaged=photo_avg, q=q,
                           q_mean=float(q_mean[s.step - 1]),
                           q_std=float(q_std[s.step - 1]),
                           intensity_w=intensity,
                           # Not `getattr(..., False)`. That default is what let
                           # the real driver call this `last_autorange_clipped`
                           # and report False for every shot ever taken on the
                           # rig. `clipped` is in the `Digitizer` protocol now,
                           # so a driver that lacks it fails loudly here and in
                           # `test_every_real_driver_satisfies_its_protocol`.
                           clipped=bool(rig.scope.clipped))

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
        # Runs on normal completion, on abort, on exception, and on the
        # consumer walking away mid-iteration (GeneratorExit). The sample is
        # never left with a driven bias or an open shutter.
        try:
            rig.bias.disable_output()
        except Exception:
            pass
        try:
            rig.shutter.shut()
        except Exception:
            pass
        _ = aborted
