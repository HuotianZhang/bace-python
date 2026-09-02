"""Simulated instruments — the whole rig, in software.

Every protocol in `drivers.protocols` has an implementation here, and they all
share one `Bench`. That sharing is the point: the simulated scope does not
return a canned waveform, it returns the transient implied by whatever the
simulated generator and shutter were *actually* told to do. So a sequencing
mistake — dark levels applied for the light trace, a shutter left open, an
autorange that never happened — shows up as a wrong number rather than passing
silently, which is the only way a simulator earns its place.

What this is **not**: a device model anyone should trust for physics. The
charge-vs-bias law is a plausible exponential, not a fit to a real sample, and
the record/trigger alignment is a bench parameter rather than a prediction of
where the real scope puts the trigger. Use it to debug sequencing, aborts,
storage and the UI. Never to decide anything about a device.

Deliberate infidelities that catch real bugs:

* `:ACQ:POIN 5000` returns 4000 points, as the real instrument does. Code that
  assumes the record length it asked for will break here first.
* Acquiring without a vertical range raises, and a range that is too small
  clips — so a missing or mis-scaled autorange is visible in the data.
* Noise falls as 1/sqrt(averages), so an averaging count that never reaches the
  instrument shows up as an unexpectedly noisy trace.
"""
from __future__ import annotations

import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field

import numpy as np

from .lakeshore331 import TemperatureError
from .protocols import DCPoint, TemperatureReading, Trace

KT_Q_290K = 8.617333e-5 * 290.0     # V


# -- the sample -----------------------------------------------------------
@dataclass
class SimulatedDevice:
    """A crude solar cell.

    Numbers are anchored on the 2026-08-07 archive so a simulated run lands in
    the same order of magnitude as a real one: ~3.65e-10 C extracted at
    V_oc = 0.906 V under a 1.020 V LED drive.
    """

    q_ref: float = 3.65e-10        # C of photocharge extracted at V_pre = V_oc
    voc_ref: float = 0.906         # V, at led_ref drive
    led_ref: float = 1.020         # V at the 33220A output
    led_threshold: float = 1.000   # V, LED turn-on
    led_ideality_v: float = 0.030  # V per e-fold of LED current
    v_ideal: float = 0.045         # V per e-fold of stored charge vs prebias
    charge_floor: float = 0.15     # fraction of q_ref still extracted at 0 V
    capacitance: float = 1.0e-9    # F, geometric — the dark run's charge
    tau_ext: float = 8.0e-8        # s, extraction time constant
    diode_n: float = 1.5           # ideality for V_oc vs intensity
    jsc_ref: float = 2.0e-4        # A at led_ref
    jsat_ref: float = 6.0e-4       # A at led_ref
    shunt_ohm: float = 2.0e5       # shunt resistance, for the J-V curve

    def led_current(self, drive_v: float) -> float:
        """Arbitrary units, 1.0 at `led_ref`. Zero below threshold."""
        if drive_v <= self.led_threshold:
            return 0.0
        ref = math.exp((self.led_ref - self.led_threshold) / self.led_ideality_v)
        return math.exp((drive_v - self.led_threshold) / self.led_ideality_v) / ref

    def voc(self, drive_v: float) -> float:
        i = self.led_current(drive_v)
        if i <= 0.0:
            return 0.0
        return self.voc_ref + self.diode_n * KT_Q_290K * math.log(i)

    @property
    def i0(self) -> float:
        """Saturation current, chosen so the diode equation reproduces `voc()`.

        V_oc = n kT ln(I_ph/I_0), and `voc()` says V_oc = voc_ref + n kT ln(i)
        with I_ph = jsc_ref * i, so I_0 = jsc_ref * exp(-voc_ref / n kT). The
        J-V curve and the V_oc used by the transient axis therefore cannot
        disagree, which is the whole point of deriving it rather than picking
        a number.
        """
        return self.jsc_ref * math.exp(-self.voc_ref / (self.diode_n * KT_Q_290K))

    def current(self, v: float, drive_v: float) -> float:
        """Single-diode I(V). Positive current is injection.

        No series resistance: adding it makes I(V) implicit, and the simulator
        exists to exercise sequencing and file formats, not to stand in for a
        device. A curve from here should never be fitted.
        """
        i_ph = self.jsc_ref * self.led_current(drive_v)
        arg = np.clip(v / (self.diode_n * KT_Q_290K), -50.0, 50.0)
        return float(self.i0 * (math.exp(arg) - 1.0) + v / self.shunt_ohm - i_ph)

    def photocharge(self, vpre: float, drive_v: float) -> float:
        """Charge stored at prebias `vpre` under illumination `drive_v`.

        **No delay dependence.** Nothing here decays between the LED falling
        edge and the collection pulse, so a simulated TDCF delay scan comes out
        flat. That is a missing model, not a result — the delay axis is
        exercised here for its sequencing, and any physics read off it would be
        read off an assumption.
        """
        i = self.led_current(drive_v)
        if i <= 0.0:
            return 0.0
        rel = math.exp(np.clip((vpre - self.voc(drive_v)) / self.v_ideal, -50.0, 20.0))
        return self.q_ref * (i ** 0.8) * (rel + self.charge_floor)


# -- the shared state -----------------------------------------------------
@dataclass
class Bench:
    """Everything the simulated instruments can see about each other."""

    device: SimulatedDevice = field(default_factory=SimulatedDevice)
    rng: np.random.Generator = field(default_factory=np.random.default_rng)

    # rig constants
    pulse_amp: float = 4.0             # the x4 amplifier in the bias path
    sense_resistor_ohm: float = 5.192
    baseline_a: float = -2.5e-5        # the DC offset the last-10 % correction removes
    noise_a: float = 3.0e-4            # single-shot rms current noise
    points_fraction: float = 0.8       # :ACQ:POIN 5000 -> 4000 points returned

    # live instrument state, written by the simulated drivers
    shutter_open: bool = False
    led_drive_v: float = 0.0
    led_mode: str = "OFF"              # OFF | DC | PULSE
    led_duty: float = 1.0              # on-fraction of the cycle; 1 under DC
    bias_high_v: float = 0.0           # at the generator output
    bias_low_v: float = 0.0
    bias_delay_s: float = 0.0
    bias_width_s: float = 5e-6
    bias_output: bool = False
    smu_output: bool = False
    relay: str = "amplifier"
    temperature_k: float = 294.8       # the cryostat, as the 331 stand-in moves it

    # bookkeeping, handy in tests
    shots: int = 0

    # -- derived device conditions ---------------------------------------
    @property
    def vpre_device(self) -> float:
        return self.bias_high_v * self.pulse_amp

    @property
    def vcoll_device(self) -> float:
        return self.bias_low_v * self.pulse_amp

    @property
    def swing(self) -> float:
        return abs(self.vpre_device - self.vcoll_device)

    def extracted_charge(self) -> float:
        """Total charge the next shot would extract, in coulombs.

        Capacitive charge from the bias swing, always; photocharge only if the
        shutter is open. The dark reference reproduces the same swing about
        0 V, so its capacitive term matches and cancels — which is exactly the
        property the real dark run relies on.
        """
        q = self.device.capacitance * self.swing
        if self.shutter_open:
            q += self.device.photocharge(self.vpre_device, self.led_drive_v)
        return q


# -- bias source ----------------------------------------------------------
class SimulatedBiasSource:
    """Stand-in for the Agilent 81150A."""

    def __init__(self, bench: Bench):
        self.bench = bench
        self.frequency_hz = 1000.0
        self.duty_percent = 50.0
        self.edge_time_s = 2.5e-9
        self.inverted = False
        self.output_polarity = "?"
        # What `*RST` leaves on the real instrument: free-running. It is also
        # exactly the state a run that forgot to arm would inherit, so a test
        # can tell "armed by this run" from "inherited" by looking here.
        self.arm_source = "IMM"
        self.arm_slope = "POS"

    def configure_trigger(self, *, external: bool = True,
                          positive_slope: bool = True) -> None:
        self.arm_source = "EXT" if external else "IMM"
        if external:
            self.arm_slope = "POS" if positive_slope else "NEG"

    def trigger_state(self) -> dict[str, str]:
        return {"arm_source": self.arm_source, "arm_slope": self.arm_slope}

    def configure_shape(self, frequency_hz: float, *, duty_percent: float = 50.0,
                        edge_time_s: float = 2.5e-9,
                        inverted_output: bool | None = False) -> None:
        self.frequency_hz = frequency_hz
        self.duty_percent = duty_percent
        self.edge_time_s = edge_time_s
        if inverted_output is None:                 # leave it, report it
            self.output_polarity = "INV" if self.inverted else "NORM"
        else:
            self.inverted = inverted_output
            self.output_polarity = "INV" if inverted_output else "NORM"

    def set_levels(self, high_v: float, low_v: float, *,
                   delay_s: float, width_s: float) -> None:
        b = self.bench
        b.bias_high_v, b.bias_low_v = float(high_v), float(low_v)
        b.bias_delay_s, b.bias_width_s = float(delay_s), float(width_s)

    def enable_output(self, on: bool = True) -> None:
        self.bench.bias_output = bool(on)

    def disable_output(self) -> None:
        self.enable_output(False)

    @property
    def output_enabled(self) -> bool:
        return self.bench.bias_output

    @property
    def last_levels(self) -> tuple[float, float]:
        """`(high, low)` as last written by `set_levels`, for the service's
        bench read-back. Read-only: the bench holds the levels, this only
        reports them, so a read-back can never move an output."""
        return (self.bench.bias_high_v, self.bench.bias_low_v)


# -- digitizer ------------------------------------------------------------
class SimulatedDigitizerError(RuntimeError):
    pass


class SimulatedDigitizer:
    """Stand-in for the Infiniium DSO9054H."""

    def __init__(self, bench: Bench, *, current_sign: float = -1.0,
                 trigger_position_divisions: float = 4.0):
        self.bench = bench
        self.current_sign = float(current_sign)
        """`RigConfig.current_sign`, applied in `acquire` at the same place
        `Infiniium._fetch` applies it -- beside the sense-resistor division --
        so the simulator reports in the rig's convention. The device model
        underneath keeps returning the physical photocharge: a test that
        compares a recovered Q against `SimulatedDevice.photocharge` has to
        multiply by this, exactly as a reader of a real file has to."""
        self.trigger_position_divisions = trigger_position_divisions
        """Where `:TIM:POS` puts the screen centre, in divisions. The trigger
        then lands at `range/2 - TIM:POS` into the record, which for the rig's
        200 ns/div and four divisions is 200 ns -- measured on the instrument
        2026-09-01, and not the 800 ns this simulator first assumed."""
        self._ns_per_div = 200.0
        self._record_length = 5000
        self._range_v: float | None = None
        self._offset_v = 0.0
        self._clipped = False
        self.last_autorange_passes = 0
        self.last_autorange_clipped = False
        self.last_autorange_windows: list[tuple[float, float, bool]] = []

    # -- configuration ---------------------------------------------------
    def configure_timebase(self, timebase_ns_per_div: float, record_length: int) -> None:
        self._ns_per_div = float(timebase_ns_per_div)
        self._record_length = int(record_length)

    def configure_edge_trigger(self, source: str = "CHAN3", *, positive: bool = True,
                               high_threshold: float | None = None,
                               level: float | None = None,
                               sweep: str = "AUTO") -> None:
        self._trigger_source = source
        self._trigger_sweep = sweep

    def configure_channel(self, channel: int, *, vertical_range: float,
                          offset: float = 0.0, display: bool = True) -> None:
        self._range_v = float(vertical_range)
        self._offset_v = float(offset)

    # -- geometry --------------------------------------------------------
    @property
    def full_screen_s(self) -> float:
        return self._ns_per_div / 1e8

    @property
    def n_points(self) -> int:
        return max(2, int(self._record_length * self.bench.points_fraction))

    @property
    def dt(self) -> float:
        return self.full_screen_s / self.n_points

    @property
    def trigger_position_s(self) -> float:
        """Where the trigger falls *within the record*.

        `:TIM:POS` is the time at the centre of the screen, so the window is
        `TIM:POS +/- range/2` and the trigger sits `range/2 - TIM:POS` in. The
        scope confirmed this: 2 us range with `:TIM:POS 8e-7` gave
        `:WAV:XOR? = -1.995e-7`.
        """
        tim_pos = self._ns_per_div * 1e-9 * self.trigger_position_divisions
        return self.full_screen_s / 2.0 - tim_pos

    # -- synthesis -------------------------------------------------------
    def _ideal_volts(self, n_averages: int) -> np.ndarray:
        b = self.bench
        n, dt = self.n_points, self.dt
        t = np.arange(n) * dt
        t_arrive = self.trigger_position_s + b.bias_delay_s
        tau = b.device.tau_ext
        q = b.extracted_charge()

        i = np.zeros(n)
        after = t >= t_arrive
        i[after] = (q / tau) * np.exp(-(t[after] - t_arrive) / tau)
        i += b.baseline_a
        if n_averages > 0:
            i += b.rng.normal(0.0, b.noise_a / math.sqrt(n_averages), n)
        return i * b.sense_resistor_ohm        # amps -> volts at the scope

    SYNC_SOURCE = "CHAN3"
    SYNC_HIGH_V = 1.2
    """What the 81150A sync looks like at the scope: a positive step of about
    1.2 V (the bench sessions of 2026-09-01 calibrated 0.115 V thresholds from
    0.23 V of it seen through the 1/R division) arriving at the trigger
    position and staying high for the rest of the record. The real CHAN3
    carries this, not a current, and `run_transient_scan` calibrates its
    threshold from it -- a simulator that answered the transient for every
    source let the 2026-09-02 sign flip pass every test while the rig set a
    0.25 mV threshold."""

    def _sync_volts(self, n_averages: int) -> np.ndarray:
        b = self.bench
        n, dt = self.n_points, self.dt
        t = np.arange(n) * dt
        v = np.where(t >= self.trigger_position_s, self.SYNC_HIGH_V, 0.0)
        if n_averages > 0:
            v = v + b.rng.normal(0.0, 2e-3 / math.sqrt(n_averages), n)
        return v

    def _volts_for(self, source: str, n_averages: int) -> np.ndarray:
        """Volts at the scope input for `source`: the sync on its channel,
        the device current through the sense resistor on any other."""
        if str(source).upper() == self.SYNC_SOURCE:
            return self._sync_volts(n_averages)
        return self._ideal_volts(n_averages)

    def autorange(self, source: str = "CHAN2", *, scale_factor: float = 1.5,
                  offset_divisor: float = 2.0, channel: int = 2,
                  passes: int = 6, growth: float = 1.8,
                  max_range_v: float = 8.0) -> tuple[float, float]:
        """One pass, because this one can see the signal without clipping it.

        The real driver has to iterate: it can only learn the excursion from a
        trace already limited by the window in force. `passes`, `growth` and
        `max_range_v` are accepted and ignored so the two are interchangeable.
        """
        v = self._volts_for(source, 16)
        hi, lo = float(v.max()), float(v.min())
        pk = hi - lo
        if abs(pk) < 1e-3:
            pk = 1e-2
        self._range_v = abs(scale_factor * pk)
        self._offset_v = (hi + lo) / offset_divisor
        self.last_autorange_passes = 1
        self.last_autorange_clipped = False
        self.last_autorange_windows = [(self._range_v, self._offset_v, False)]
        return self._range_v, self._offset_v

    def acquire(self, n_averages: int, *, source: str = "CHAN2",
                autorange_first: bool = False, timeout_s: float = 30.0) -> Trace:
        if autorange_first:
            self.autorange(source)
        if self._range_v is None:
            raise SimulatedDigitizerError(
                "no vertical range set — the light trace must autorange before the "
                "dark trace acquires, or the channel must be configured explicitly"
            )
        v = self._volts_for(source, max(1, int(n_averages)))
        top = self._offset_v + self._range_v / 2.0
        bottom = self._offset_v - self._range_v / 2.0
        self._clipped = bool((v > top).any() or (v < bottom).any())
        v = np.clip(v, bottom, top)
        self.bench.shots += 1
        # volts -> amps, with the sign convention, in one place. Clipping was
        # done in volts above, as the real window is, so the sign cannot move
        # the rail.
        return Trace(y=self.current_sign * v / self.bench.sense_resistor_ohm,
                     dt=self.dt, t0=-self.trigger_position_s)

    @property
    def clipped(self) -> bool:
        """True if the last acquisition hit the vertical limits."""
        return self._clipped

    def close(self) -> None:
        pass


# -- shutter --------------------------------------------------------------
class SimulatedShutter:
    """Stand-in for the Deditec DIO shutter, wired into the bench.

    `drivers.shutter.SimulatedShutter` also exists and satisfies the same
    protocol, but it keeps its state to itself. This one tells the bench, so
    the simulated scope actually sees the difference between light and dark.
    """

    def __init__(self, bench: Bench):
        self.bench = bench

    def unblock(self) -> None:
        self.bench.shutter_open = True

    def shut(self) -> None:
        self.bench.shutter_open = False

    @property
    def is_open(self) -> bool:
        return self.bench.shutter_open

    @contextmanager
    def dark(self):
        was_open = self.is_open
        self.shut()
        try:
            yield
        finally:
            if was_open:
                self.unblock()


# -- LED drive ------------------------------------------------------------
class SimulatedLedSource:
    """Stand-in for the Agilent 33220A driving the LED."""

    def __init__(self, bench: Bench):
        self.bench = bench
        self._output = False
        # NORM, because that is what a cold instrument was found at (bench
        # session 2026-08-31) -- and the wrong setting for BACE, so a run that
        # never sets it inherits the wrong one here too, as on the rig.
        self._polarity = "NORM"
        # What the service's read-back reports beside the mode. Kept here
        # rather than on the bench because the bench only needs the on-level;
        # the low level and the frequency are what the *generator* holds.
        self.frequency_hz = 1000.0
        self.duty_percent = 50.0
        self._levels: tuple[float | None, float | None] = (None, None)

    def set_dc(self, level_v: float) -> None:
        self.bench.led_drive_v = float(level_v)
        self.bench.led_mode = "DC"
        self.bench.led_duty = 1.0
        self._levels = (float(level_v), None)

    def set_pulse(self, high_v: float, low_v: float, *, frequency_hz: float = 1000.0,
                  duty_percent: float = 50.0) -> None:
        # The device is measured during the on-phase, so the bench sees the high level.
        self.bench.led_drive_v = float(high_v)
        self.bench.led_mode = "PULSE"
        self.bench.led_duty = float(duty_percent) / 100.0
        self._levels = (float(high_v), float(low_v))
        self.frequency_hz = float(frequency_hz)
        self.duty_percent = float(duty_percent)

    @property
    def last_levels(self) -> tuple[float | None, float | None]:
        """`(high, low)` as last set; `low` is None in DC mode. Read-only, for
        the service's bench read-back."""
        return self._levels

    def off(self) -> None:
        self.bench.led_drive_v = 0.0
        self.bench.led_mode = "OFF"
        self._output = False

    def enable_output(self, on: bool = True) -> None:
        self._output = bool(on)
        if not on:
            self.bench.led_drive_v = 0.0
            self.bench.led_mode = "OFF"

    def disable_output(self) -> None:
        self.enable_output(False)

    @property
    def output_enabled(self) -> bool:
        return self._output

    @property
    def mode(self) -> str:
        return self.bench.led_mode

    def set_polarity(self, inverted: bool) -> None:
        self._polarity = "INV" if inverted else "NORM"

    def polarity(self) -> str:
        return self._polarity


# -- source meter ---------------------------------------------------------
class SimulatedSourceMeter:
    """Stand-in for the Keithley 24xx."""

    def __init__(self, bench: Bench, *, compliance_a: float = 0.05,
                 noise_a: float = 5e-7):
        self.bench = bench
        self._output = False
        self.compliance_a = compliance_a
        self.noise_a = noise_a
        self.clipped = False

    def measure_dc(self, *, v_sat: float = -1.0,
                   settle_s: float | None = None) -> DCPoint:
        """V_oc, J_sc, J_sat under the light that actually reaches the sample.

        Light only reaches it with the shutter open; the meter is on the
        bench like everything else, so a DC characterisation taken with the
        shutter shut is the dark point, not a number that happens to look
        right. Under PULSE a slow meter (NPLC 1 is ten LED cycles) averages
        the on and off phases, modelled here as the duty-weighted mean of
        the two open-circuit voltages -- visibly not the DC V_oc, which is
        the point: a sequence that reads V_oc before switching the 33220A to
        DC would otherwise pass every test and centre a real axis on the
        wrong number.
        """
        d, b = self.bench.device, self.bench
        lit = b.shutter_open and b.led_mode in ("DC", "PULSE")
        i = d.led_current(b.led_drive_v) if lit else 0.0
        if i <= 0.0:
            return DCPoint(voc=0.0, jsc=0.0, jsat=0.0, v_sat=v_sat)
        on = b.led_duty if b.led_mode == "PULSE" else 1.0
        noise = b.rng.normal(0.0, 2e-4)
        return DCPoint(voc=on * d.voc(b.led_drive_v) + noise,
                       jsc=-d.jsc_ref * i * on,
                       jsat=-d.jsat_ref * i * on,
                       v_sat=v_sat)

    def sweep(self, start_v: float, stop_v: float, points: int, *,
              settle_s: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """A linear voltage sweep through the single-diode model.

        The current is **clipped at the configured compliance**, exactly as the
        instrument would clip it. That makes a compliance set too low visible as
        a flat top on the curve rather than as a quietly wrong fill factor.
        """
        if points < 2:
            raise ValueError("a sweep needs at least two points")
        b = self.bench
        v = np.linspace(float(start_v), float(stop_v), int(points))
        i = np.array([b.device.current(float(x), b.led_drive_v) for x in v])
        i += b.rng.normal(0.0, self.noise_a, i.size)
        self.clipped = bool((np.abs(i) > self.compliance_a).any())
        return v, np.clip(i, -self.compliance_a, self.compliance_a)

    def enable_output(self, on: bool = True) -> None:
        self._output = bool(on)
        self.bench.smu_output = self._output

    def disable_output(self) -> None:
        self.enable_output(False)

    @property
    def output_enabled(self) -> bool:
        return self._output


# -- power meter ----------------------------------------------------------
class SimulatedPowerMeter:
    """Stand-in for the Newport 1918-C."""

    def __init__(self, bench: Bench, *, w_per_unit: float = 1.0e-3):
        self.bench = bench
        self.w_per_unit = w_per_unit
        self.wavelength_nm = 530.0

    def set_wavelength(self, nm: float) -> None:
        self.wavelength_nm = float(nm)

    def read_power(self) -> float:
        b = self.bench
        base = b.device.led_current(b.led_drive_v) * self.w_per_unit
        if not b.shutter_open:
            base *= 1e-4
        return float(base * (1.0 + b.rng.normal(0.0, 0.01)))

    def read_statistics(self, n: int) -> tuple[float, float]:
        vals = np.array([self.read_power() for _ in range(max(2, n))])
        return float(vals.mean()), float(vals.std(ddof=1))


# -- temperature ----------------------------------------------------------
class SimulatedTemperatureController:
    """Stand-in for the 331 console: a first-order approach to the setpoint
    that advances one step per `read()`, not per wall-clock second.

    Per read rather than per second because the settle that polls it sleeps
    through `RunContext.sleep`, which `--fast` makes a no-op: a model on the
    wall clock would be polled a thousand times in one millisecond and never
    move, and a test could not count polls. Each read closes `1/tau` of the
    remaining gap (`tau = time_constant_reads`), so from 294.8 K a 250 K
    setpoint is inside a 0.2 K band after about thirty reads at the default
    six -- a handful of polls under `--fast`, a couple of minutes at the
    executor's five-second poll if someone ran it slow.

    Because the model steps per read, *every* reader steps it: the bench
    read-back at Start and the temperature monitor, when one runs beside a
    settle under `--sim`, each move the stand-in one step, so a settle then
    converges in fewer of its own polls than the same tree alone. That is
    an artefact of the stand-in, not of the service -- the real console
    answers `/api/state` from a cached poll and a reader moves nothing --
    and it is accepted rather than hidden behind a second read path the
    protocol does not have. The lock keeps a step whole when two threads
    read at once.

    The ceiling is the console's rule (`ls331/config.py`, 350 K), reproduced
    with the console's own wording so a refusal in a test reads as it would
    on the rig: refused, never clamped. `notes` keeps what the service wrote
    to the audit log, `setpoints` every setpoint it wrote; `connected` may be
    set False by a test to play the console-up, instrument-silent case, in
    which the console keeps reporting its last values with the flag down;
    `ramping` plays RAMPST? (True while the console's ramp still walks the
    setpoint), which the model itself never raises and a test sets to check
    that a reading inside the band does not count while it is up;
    `heater_range` is what the console holds and may be set to 0 to play the
    heater off.
    """

    def __init__(self, bench: Bench, *, start_k: float = 294.8, ceiling_k: float = 350.0,
                 time_constant_reads: int = 6):
        self.bench = bench
        self.kelvin = float(start_k)
        self.setpoint_k = float(start_k)
        self.ceiling_k = float(ceiling_k)
        self.time_constant_reads = max(1, int(time_constant_reads))
        self.heater_range = 3
        self.connected = True
        self.ramping = False
        self.reads = 0
        self.notes: list[str] = []
        self.setpoints: list[float] = []
        self.last: TemperatureReading | None = None
        self._lock = threading.Lock()
        bench.temperature_k = self.kelvin

    def read(self) -> TemperatureReading:
        with self._lock:
            self.reads += 1
            if self.heater_range or self.setpoint_k < self.kelvin:
                # The heater only heats: with the range off the model still
                # cools toward a lower setpoint (the cryogen does that) and
                # sits where it is for a higher one.
                self.kelvin += (self.setpoint_k - self.kelvin) / self.time_constant_reads
            self.bench.temperature_k = self.kelvin
            r = TemperatureReading(
                kelvin=self.kelvin, setpoint_k=self.setpoint_k, ramping=bool(self.ramping),
                heater_range=self.heater_range, connected=self.connected,
                status_text="ok" if self.connected else "the instrument is not answering",
                elapsed_s=float(self.reads), max_setpoint_k=self.ceiling_k)
            self.last = r
            return r

    def set_setpoint(self, kelvin: float) -> float:
        kelvin = float(kelvin)
        if kelvin > self.ceiling_k:
            raise TemperatureError(
                "setpoint %.3f K exceeds the %.1f K limit for this cryostat"
                % (kelvin, self.ceiling_k), refused=True, status=403)
        if kelvin < 0.0:
            raise TemperatureError("setpoint %.3f K is below the 0.0 K limit" % kelvin,
                                   refused=True, status=403)
        self.setpoint_k = kelvin
        self.setpoints.append(kelvin)
        return kelvin

    def note(self, text: str) -> bool:
        self.notes.append(str(text))
        return True


# -- router ---------------------------------------------------------------
class SimulatedRouter:
    """Stand-in for the relay, including the interlock.

    The interlock is the point of simulating this at all: it is the one place a
    software mistake damages a sample, so the sequencing that keeps it happy is
    worth debugging with nothing connected.
    """

    def __init__(self, bench: Bench, *, sourcemeter=None, pulse_path=None):
        self.bench = bench
        self._smu = sourcemeter
        self._bias = pulse_path

    def _assert_quiet(self) -> None:
        live = [n for n, d in (("SourceMeter", self._smu), ("pulse generator", self._bias))
                if d is not None and getattr(d, "output_enabled", False)]
        if live:
            raise RuntimeError(
                f"refusing to move the relay while {' and '.join(live)} "
                f"{'is' if len(live) == 1 else 'are'} still driving"
            )

    def _move(self, position: str) -> None:
        if self.bench.relay == position:
            return
        self._assert_quiet()
        self.bench.relay = position

    @contextmanager
    def dc(self):
        self._move("sourcemeter")
        try:
            yield
        finally:
            if self._smu is not None:
                self._smu.disable_output()

    @contextmanager
    def transient(self):
        self._move("amplifier")
        try:
            yield
        finally:
            if self._bias is not None:
                self._bias.disable_output()

    def park(self) -> None:
        for d in (self._smu, self._bias):
            if d is not None:
                d.disable_output()

    @property
    def position(self) -> str:
        return self.bench.relay


# -- convenience ----------------------------------------------------------
@dataclass
class SimulatedRig:
    """All eight instruments on one bench, ready to hand to an experiment.
    `temperature` is built but only attached to a `Rig` when the bench names
    a console (`service.rigs.Bench.build_simulated`), so `--sim` with the
    default `rig.toml` still pauses for the operator as the real bench does."""

    bench: Bench
    bias: SimulatedBiasSource
    scope: SimulatedDigitizer
    shutter: SimulatedShutter
    led: SimulatedLedSource
    smu: SimulatedSourceMeter
    power: SimulatedPowerMeter
    router: SimulatedRouter
    temperature: SimulatedTemperatureController


def make_bench(seed: int | None = 0, current_sign: float = -1.0,
               **device_kw) -> SimulatedRig:
    """`current_sign` defaults to the bench's own (`RigConfig.current_sign`),
    so a simulated run and a real one write the same sign for the same
    physics. Pass +1 only to test that the sign is applied once."""
    bench = Bench(device=SimulatedDevice(**device_kw),
                  rng=np.random.default_rng(seed))
    bias = SimulatedBiasSource(bench)
    smu = SimulatedSourceMeter(bench)
    return SimulatedRig(
        bench=bench,
        bias=bias,
        scope=SimulatedDigitizer(bench, current_sign=current_sign),
        shutter=SimulatedShutter(bench),
        led=SimulatedLedSource(bench),
        smu=smu,
        power=SimulatedPowerMeter(bench),
        router=SimulatedRouter(bench, sourcemeter=smu, pulse_path=bias),
        temperature=SimulatedTemperatureController(bench),
    )
