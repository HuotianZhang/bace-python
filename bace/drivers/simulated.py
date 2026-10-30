"""Simulate the bench instruments with shared state.

Each driver reads one `Bench`. The digitiser synthesises a transient from
the commanded levels and shutter state, exposing sequence errors such as
dark levels for a light trace, an open shutter for dark or missing autorange.

The device is a sequence-testing model. Its charge law is a plausible
exponential, not a fit. Record alignment is configurable, not predicted.
The simulator exercises sequencing, aborts, storage and the console; its
curves must not be used to infer device physics.

The simulated scope returns 4000 samples for a 5000-sample request, as
measured on this bench (`docs/notes.md`). Acquisition without a range raises,
a small range clips, and current noise scales as 1/sqrt(N_avg). These
constraints expose mistaken record-length, range and averaging assumptions."""
from __future__ import annotations

import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field

import numpy as np

from .keithley2400 import (PANEL_STATIC, PanelReading, PanelSetup,
                          SourceMeterError, panel_budget_for)
from .lakeshore331 import TemperatureError
from .protocols import DCPoint, TemperatureReading, Trace

KT_Q_290K = 8.617333e-5 * 290.0     # V


# -- the sample -----------------------------------------------------------
@dataclass
class SimulatedDevice:
    """Model a solar cell for sequence and storage checks.

    Bench reference scale: about 3.65e-10 C at V_pre = V_oc = 0.906 V under
    1.020 V LED drive. These values set the order of magnitude, not a
    predictive device model."""

    q_ref: float = 3.65e-10        # C of photocharge extracted at V_pre = V_oc
    voc_ref: float = 0.906         # V at `led_ref` drive
    led_ref: float = 1.020         # V at the 33220A output
    led_threshold: float = 1.000   # V, LED turn-on
    led_ideality_v: float = 0.030  # V per e-fold of LED current
    v_ideal: float = 0.045         # V per e-fold of stored Q vs V_pre
    charge_floor: float = 0.15     # fraction of `q_ref` extracted at 0 V
    capacitance: float = 1.0e-9    # F, geometric — the dark run's charge
    tau_ext: float = 8.0e-8        # s, extraction time constant
    diode_n: float = 1.5           # ideality for V_oc vs intensity
    saturation_efolds: float = 3.0 # stored-Q ceiling in e-folds above V_oc
    jsc_ref: float = 2.0e-4        # A at `led_ref`
    jsat_ref: float = 6.0e-4       # A at `led_ref`
    shunt_ohm: float = 2.0e5       # shunt resistance, for the J-V curve

    def led_current(self, drive_v: float) -> float:
        """Return relative LED current at `drive_v` in V.

        The scale is 1 at `led_ref` and zero below turn-on threshold."""
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
        """Return diode saturation current in A consistent with `voc`.

        V_oc = n kT ln(I_ph/I_0), with thermal energy expressed in V. Using
        `jsc_ref` and `voc_ref` derives I_0 so the J-V model and transient axis
        share the same V_oc."""
        return self.jsc_ref * math.exp(-self.voc_ref / (self.diode_n * KT_Q_290K))

    def current(self, v: float, drive_v: float) -> float:
        """Return single-diode current in A at voltage `v` and LED drive `drive_v` in V.

        Positive current is injection. Series resistance is omitted to keep
        the equation explicit. The model tests sequencing and file formats;
        its J-V curves must not be fitted as device measurements."""
        i_ph = self.jsc_ref * self.led_current(drive_v)
        arg = np.clip(v / (self.diode_n * KT_Q_290K), -50.0, 50.0)
        return float(self.i0 * (math.exp(arg) - 1.0) + v / self.shunt_ohm - i_ph)

    def photocharge(self, vpre: float, drive_v: float) -> float:
        """Return stored Q in C at V_pre `vpre` and LED drive `drive_v` in V.

        There is no decay between light off and the bias step. Simulated delay
        scans are flat by assumption and test only delay-axis sequencing."""
        i = self.led_current(drive_v)
        if i <= 0.0:
            return 0.0
        # Stored Q grows exponentially with V_pre to a ceiling at
        # `saturation_efolds * v_ideal`, 0.135 V above V_oc by default, about
        # 20 times `q_ref`. The ceiling keeps wiring errors within a plausible
        # plot scale: an uncapped model at 1.1 V above V_oc can yield 0.18 C.
        rel = math.exp(np.clip((vpre - self.voc(drive_v)) / self.v_ideal,
                               -50.0, self.saturation_efolds))
        return self.q_ref * (i ** 0.8) * (rel + self.charge_floor)


# -- the shared state -----------------------------------------------------
@dataclass
class Bench:
    """Hold the shared device and instrument state for the simulated bench."""

    device: SimulatedDevice = field(default_factory=SimulatedDevice)
    rng: np.random.Generator = field(default_factory=np.random.default_rng)

    # rig constants
    pulse_amp: float = 4.0             # the x4 amplifier in the bias path
    amplifier_inverting: bool = True
    """Apply inversion in the simulated bias amplifier.

    With `invert_polarity = true` and `:OUTP1:POL NORM`, the generator
    rests at -V_pre/4 and the device at V_pre. The gain sign belongs to
    `amplifier_gain`, independently of the configured magnitude."""
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
    field_latency_s: float = 0.0
    """Add Sync-to-field latency in s after the simulated pulse delay.

    `bace.service.rigs.Bench.build_simulated` copies `RigConfig.trigger_offset_s`
    so field-relative integration windows follow the modelled transient."""
    bias_width_s: float = 5e-6
    bias_inverted: bool = False        # :OUTP1:POL INV on the 81150A
    bias_output: bool = False
    smu_output: bool = False
    relay: str = "amplifier"
    temperature_k: float = 294.8       # the cryostat, as the 331 stand-in moves it

    # bookkeeping, handy in tests
    shots: int = 0

    # -- derived device conditions ---------------------------------------
    @property
    def amplifier_gain(self) -> float:
        return -self.pulse_amp if self.amplifier_inverting else self.pulse_amp

    @property
    def rest_level_v(self) -> float:
        """Return the generator rest level in V between pulses.

        `:OUTP1:POL NORM` holds the low level; `INV` holds the high level.
        This choice determines V_pre through the inverting amplifier. Ignoring
        polarity for the bench's `NORM` and `invert_polarity` combination
        charges the simulated device at -V_coll instead of V_pre."""
        return self.bias_high_v if self.bias_inverted else self.bias_low_v

    @property
    def pulse_level_v(self) -> float:
        """Return the generator pulse level in V, opposite its rest level."""
        return self.bias_low_v if self.bias_inverted else self.bias_high_v

    @property
    def vpre_device(self) -> float:
        """Return V_pre in V from the generator rest level through the amplifier."""
        return self.rest_level_v * self.amplifier_gain

    @property
    def vcoll_device(self) -> float:
        return self.pulse_level_v * self.amplifier_gain

    @property
    def swing(self) -> float:
        return abs(self.vpre_device - self.vcoll_device)

    def extracted_charge(self) -> float:
        """Return total Q in C for the next acquisition.

        The bias swing always contributes displacement charge. Light adds
        photocharge only with the shutter open. A translated dark reference
        has the same swing about 0 V, so its capacitive term cancels in the
        difference."""
        q = self.device.capacitance * self.swing
        if self.shutter_open:
            q += self.device.photocharge(self.vpre_device, self.led_drive_v)
        return q


# -- bias source ----------------------------------------------------------
class SimulatedBiasSource:
    """Model an Agilent 81150A connected to the shared bench."""

    def __init__(self, bench: Bench):
        self.bench = bench
        self.frequency_hz = 1000.0
        self.duty_percent = 50.0
        self.edge_time_s = 2.5e-9
        self.inverted = False
        self.output_polarity = "?"
        # Reset leaves the real generator free-running. Matching that state
        # lets tests detect a run that fails to configure external arming.
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
    def inverted(self) -> bool:
        """Return simulated `:OUTP1:POL` as a boolean.

        The bench stores polarity because it selects the generator rest level
        and therefore V_pre used by the digitiser model."""
        return self.bench.bias_inverted

    @inverted.setter
    def inverted(self, on: bool) -> None:
        self.bench.bias_inverted = bool(on)

    @property
    def last_levels(self) -> tuple[float, float]:
        """Return cached `(high, low)` generator levels in V.

        The service uses this read-only state for bench read-back. Querying it
        must not move an output."""
        return (self.bench.bias_high_v, self.bench.bias_low_v)


# -- digitiser ------------------------------------------------------------
class SimulatedDigitizerError(RuntimeError):
    pass


class SimulatedDigitizer:
    """Model Infiniium acquisition, scaling and clipping on the shared bench."""

    def __init__(self, bench: Bench, *, current_sign: float = -1.0,
                 trigger_position_divisions: float = 4.0):
        self.bench = bench
        self.current_sign = float(current_sign)
        """Apply the rig's `current_sign` once in `acquire`.

        This matches `Infiniium._fetch`. The device model returns physical
        photocharge; comparisons with `SimulatedDevice.photocharge` must
        include this sign, as for measured data."""
        self.trigger_position_divisions = trigger_position_divisions
        """Place `:TIM:POS` at this many divisions from the trigger.

        The trigger sits at half the full range minus `:TIM:POS`: 200 ns
        into a 200 ns/div record with four divisions, as measured on this
        bench (`docs/notes.md`)."""
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
        """Return the trigger's position within the record in s.

        `:TIM:POS` denotes the centre. A 2 us range at position 8e-7 s gives
        `:WAV:XOR? = -1.995e-7 s` on this bench, approximately 200 ns before
        the trigger (`docs/notes.md`)."""
        tim_pos = self._ns_per_div * 1e-9 * self.trigger_position_divisions
        return self.full_screen_s / 2.0 - tim_pos

    # -- synthesis -------------------------------------------------------
    def _ideal_volts(self, n_averages: int) -> np.ndarray:
        b = self.bench
        n, dt = self.n_points, self.dt
        t = np.arange(n) * dt
        t_arrive = self.trigger_position_s + b.bias_delay_s + b.field_latency_s
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
    """Set the simulated 81150A Sync high level in V.

    Sync is a positive step of about 1.2 V at the trigger, high for the
    rest of the record, as measured on this bench. Dividing it by R_s can
    make 1.2 V appear as 0.23 V and yield a 0.115 V threshold. Applying the
    current sign can instead make its maximum a baseline and set a
    0.25 mV threshold. Sync must remain in V so trigger calibration tests
    expose incorrect resistor division or sign conversion."""

    def _sync_volts(self, n_averages: int) -> np.ndarray:
        b = self.bench
        n, dt = self.n_points, self.dt
        t = np.arange(n) * dt
        v = np.where(t >= self.trigger_position_s, self.SYNC_HIGH_V, 0.0)
        if n_averages > 0:
            v = v + b.rng.normal(0.0, 2e-3 / math.sqrt(n_averages), n)
        return v

    def _volts_for(self, source: str, n_averages: int) -> np.ndarray:
        """Return simulated input V for `source`.

        Sync uses its own channel; other sources model device current times R_s."""
        if str(source).upper() == self.SYNC_SOURCE:
            return self._sync_volts(n_averages)
        return self._ideal_volts(n_averages)

    def autorange(self, source: str = "CHAN2", *, scale_factor: float = 1.5,
                  offset_divisor: float = 2.0, channel: int = 2,
                  passes: int = 6, growth: float = 1.8,
                  max_range_v: float = 8.0) -> tuple[float, float]:
        """Set range and offset in one pass and return `(range, offset)` in V.

        The model can inspect the unclipped signal. The real driver instead
        iterates from clipped observations. `passes`, `growth` and `max_range_v`
        are accepted but ignored to preserve the shared call interface."""
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
                autorange_first: bool = False, timeout_s: float = 30.0,
                window: tuple[float, float] | None = None) -> Trace:
        if autorange_first:
            self.autorange(source)
        elif window is not None:
            self._range_v, self._offset_v = float(window[0]), float(window[1])
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
        # Convert V to A and apply the sign once. Clipping occurs in input V
        # before this conversion so `current_sign` cannot move a rail.
        self._last_averages = max(1, int(n_averages))
        return Trace(y=self.current_sign * v / self.bench.sense_resistor_ohm,
                     dt=self.dt, t0=-self.trigger_position_s, count=self._last_averages)

    def fetch_volts(self, source: str) -> Trace:
        """Return a channel from the simulated record in V with timing in s.

        Sync is a voltage step on its channel. Other channels model device
        current through R_s, as required for voltage fetches on the real driver."""
        n = getattr(self, "_last_averages", 1)
        return Trace(y=self._volts_for(source, n), dt=self.dt, t0=-self.trigger_position_s,
                     count=n)

    @property
    def clipped(self) -> bool:
        """Return whether the last acquisition reaches either vertical limit."""
        return self._clipped

    @property
    def window(self) -> tuple[float, float] | None:
        return None if self._range_v is None else (self._range_v, self._offset_v)

    def close(self) -> None:
        pass


# -- shutter --------------------------------------------------------------
class SimulatedShutter:
    """Model shutter state on the shared bench.

    `bace.drivers.shutter.SimulatedShutter` keeps state locally. This model
    shares it with the digitiser so traces respond to light and dark."""

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
    """Model the Agilent 33220A LED drive on the shared bench."""

    def __init__(self, bench: Bench):
        self.bench = bench
        self._output = False
        # A reset bench generator holds `NORM`. This is wrong for the LED's
        # light-off arming edge, so tests require the run to select `INV`.
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
        """Return cached `(high, low)` drive levels in V for read-back.

        `low` is `None` in DC mode; reading levels cannot change the drive."""
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
    """Model the source meter and its compliance on the shared bench."""

    def __init__(self, bench: Bench, *, compliance_a: float = 0.05,
                 noise_a: float = 5e-7):
        self.bench = bench
        self._output = False
        self._panel: PanelSetup | None = None
        self.compliance_a = compliance_a
        self.noise_a = noise_a
        self.clipped = False

    def _prepare(self) -> None:
        """Clear the panel state to match the real driver's measurement reset.

        A simulated panel must not remain valid across a run that reconfigures
        the source meter."""
        self._panel = None

    def measure_dc(self, *, v_sat: float = -1.0,
                   settle_s: float | None = None) -> DCPoint:
        """Return simulated DC quantities in V and A under light reaching the device.

        A shut shutter produces the dark point. Under pulse drive a slow meter
        averages the on and off phases. At NPLC 1 and 500 Hz this covers ten
        LED cycles; the model uses duty-weighted V_oc. Reading before selecting
        DC therefore shifts the axis centre and must be visible in tests."""
        self._prepare()
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
        """Return model voltage in V and current in A as arrays.

        Current clamps at configured compliance, exposing a limit that flattens
        the J-V curve. The shutter determines illumination independently of LED
        drive: a dark J-V keeps the generator running behind the shut shutter."""
        got = list(self.sweep_points(start_v, stop_v, points, settle_s=settle_s))
        return (np.array([v for v, _ in got], dtype=float),
                np.array([i for _, i in got], dtype=float))

    def sweep_points(self, start_v: float, stop_v: float, points: int, *,
                     settle_s: float = 0.0):
        """Yield model `(V, A)` points under the light reaching the device.

        The real driver also yields points for streaming. Light is read at each
        point so moving the shutter during a sweep changes the curve."""
        if points < 2:
            raise ValueError("a sweep needs at least two points")
        self._prepare()
        b = self.bench
        self.clipped = False
        for x in np.linspace(float(start_v), float(stop_v), int(points)):
            lit = b.shutter_open and b.led_mode in ("DC", "PULSE")
            drive = b.led_drive_v if lit else 0.0
            i = float(b.device.current(float(x), drive) + b.rng.normal(0.0, self.noise_a))
            if abs(i) > self.compliance_a:
                self.clipped = True
            yield float(x), float(np.clip(i, -self.compliance_a, self.compliance_a))

    # -- the front panel ---------------------------------------------------
    # The panel models relay state and both compliance clamps.
    #
    # DC measurements and sweeps run inside `router.dc`, which selects the
    # source meter. The panel relies on the operator's relay choice and reads
    # an open circuit when disconnected. It must expose that wiring error.
    #
    # Unlike a sweep's current clamp, panel compliance affects both the held
    # quantity and the other reading. The status reproduces the instrument's
    # `Cmpl` indication.
    @property
    def panel(self) -> PanelSetup | None:
        return self._panel

    def apply_panel(self, setup: PanelSetup) -> PanelSetup:
        was = self._panel
        if was is None and self._output:
            raise SourceMeterError(
                "the output is ON and this instrument is not on the panel -- "
                "something else is driving it. Switch the output off first; "
                "applying a panel resets the instrument, which would drop it "
                "with nothing said")
        if was is not None and self._output:
            changed = [f for f in PANEL_STATIC if getattr(setup, f) != getattr(was, f)]
            if changed:
                raise SourceMeterError(
                    f"the output is ON: {', '.join(changed)} cannot be changed under "
                    "it -- switch the output off, change it, switch it back on")
        self._panel = setup
        return setup

    def panel_budget_s(self) -> float:
        """Return the real driver's panel-read budget in s.

        The model does not integrate over time. Callers still need the same
        budget when switching between simulated and connected instruments."""
        panel = self._panel
        return panel_budget_for(panel.nplc if panel is not None else 1.0,
                                panel.averaging if panel is not None else 1)

    def read_panel(self) -> PanelReading:
        panel = self._panel
        if panel is None:
            raise SourceMeterError("the panel is not applied to this instrument")
        if not self._output:
            raise SourceMeterError("the output is OFF: there is nothing to read")
        b = self.bench
        lit = b.shutter_open and b.led_mode in ("DC", "PULSE")
        drive = b.led_drive_v if lit else 0.0
        connected = b.relay == "sourcemeter"
        noise = float(b.rng.normal(0.0, self.noise_a))
        if panel.function == "voltage":
            volts, amps, compliance = float(panel.level), noise, False
            if connected:
                amps = float(b.device.current(volts, drive)) + noise
                if abs(amps) > panel.current_compliance_a:
                    # The source cannot hold its level at that current, so it
                    # holds the current instead and the voltage falls where
                    # the device puts it.
                    amps = math.copysign(panel.current_compliance_a, amps)
                    volts, _ = self._solve_v(amps, drive, panel.voltage_compliance_v)
                    compliance = True
        else:
            target = float(panel.level)
            if connected:
                volts, compliance = self._solve_v(target, drive, panel.voltage_compliance_v)
                amps = (float(b.device.current(volts, drive)) + noise if compliance
                        else target + noise)
            elif abs(target) <= 1e-15:
                # Nothing asked for, nothing to force: an open circuit takes
                # 0 A at any voltage and the source sits where it is.
                volts, amps, compliance = 0.0, noise, False
            else:
                # No path for the current, so the source runs to its voltage
                # limit trying to push it.
                volts = math.copysign(panel.voltage_compliance_v, target)
                amps, compliance = noise, True
        return PanelReading(volts=float(volts), amps=float(amps),
                            compliance=compliance, function=panel.function,
                            level=float(panel.level))

    def _solve_v(self, target_a: float, drive_v: float,
                 bound_v: float) -> tuple[float, bool]:
        """Return `(voltage, at_limit)` for `target_a` in A within `bound_v` in V.

        `SimulatedDevice.current` increases strictly with voltage, so bisection
        needs no derivative and resolves to floating-point precision. Reaching
        a bound means the requested current requires voltage beyond compliance."""
        bound = abs(float(bound_v))
        lo, hi = -bound, bound
        current = self.bench.device.current
        if current(lo, drive_v) >= target_a:
            return lo, True
        if current(hi, drive_v) <= target_a:
            return hi, True
        for _ in range(80):
            mid = 0.5 * (lo + hi)
            if current(mid, drive_v) < target_a:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi), False

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
    """Model Newport 1918-C power readings behind the shutter.

    Both device and meter see the light after the shutter and beam splitter.
    Drive changes appear immediately because the model omits LED thermal
    settling. The service's settle check therefore passes after three polls.
    `noise` is 0.2 % rms, measured on this bench with a settled LED. Keeping
    noise at that level lets three readings meet the 2 % criterion without
    random extra waits in seeded tests."""

    def __init__(self, bench: Bench, *, w_per_unit: float = 1.0e-3,
                 noise: float = 0.002):
        self.bench = bench
        self.w_per_unit = w_per_unit
        self.noise = float(noise)
        self.wavelength_nm = 530.0
        self.averaged = True
        """Select the model's 5 Hz analog filtering state.

        `bace.drivers.newport1918c.direct.DirectPowerMeter.set_averaging`
        selects the real filter. With
        pulsed light the model returns duty-weighted mean power when enabled,
        or the sampled phase and resulting flicker otherwise."""
        self.range: int | None = None
        self.auto_range = True
        """Record the held range; the model's readings do not depend on it."""

    def set_wavelength(self, nm: float) -> None:
        self.wavelength_nm = float(nm)

    def set_averaging(self, on: bool = True, **_: object) -> dict:
        self.averaged = bool(on)
        return {"mode": 0, "analog_filter": 4 if on else 0}

    def set_range(self, index: int | None) -> dict:
        self.range = None if index is None else int(index)
        self.auto_range = index is None
        return {"range": self.range, "auto": self.auto_range}

    def read_power(self) -> float:
        b = self.bench
        base = b.device.led_current(b.led_drive_v) * self.w_per_unit
        if not b.shutter_open:
            base *= 1e-4
        if b.led_mode == "PULSE":
            # The detector sees a square wave: `led_duty` of each cycle lit.
            if self.averaged:
                base *= b.led_duty
            else:
                base *= 1.0 if b.rng.random() < b.led_duty else 1e-4
        return float(base * (1.0 + b.rng.normal(0.0, self.noise)))

    def read_statistics(self, n: int) -> tuple[float, float]:
        vals = np.array([self.read_power() for _ in range(max(2, n))])
        return float(vals.mean()), float(vals.std(ddof=1))


# -- temperature ----------------------------------------------------------
class SimulatedTemperatureController:
    """Model temperature settling with one first-order step per read.

    Each read closes `1/time_constant_reads` of the gap. Clock-based
    settling cannot advance under fast simulation because the context skips
    sleep. At the default six reads, cooling from 294.8 K to 250 K reaches
    a 0.2 K band in about thirty reads, or a few minutes at a 5 s poll.

    Every reader advances the model, including read-back and a concurrent
    monitor. This can shorten a settle's own poll count; real controller
    reads return cached polls without changing temperature. A lock keeps
    each model step intact across threads.

    The 350 K ceiling is refused without clamping, matching the controller.
    `notes` and `setpoints` record journal notes and requested temperatures.
    Tests can set `connected=False` to retain cached values while reporting
    an unresponsive instrument. `ramping` models `RAMPST?`; an in-band
    reading is not settled while it is true. `heater_range=0` disables
    heating but permits cooling. The model itself does not start ramps."""

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
    """Model relay routing and its output interlock.

    Moving a relay under output can damage the device. The simulation
    exercises the same refusal without connected hardware."""

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
    """Group all eight simulated instrument roles on one bench.

    `bace.service.rigs.Bench.build_simulated` attaches `temperature` to the
    experiment only when the configuration names a console. An empty
    console setting exercises the pause for operator-controlled temperature."""

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
    """Build simulated instruments sharing a `Bench`.

    `current_sign=-1` matches the bench current convention. Use +1 only
    to check that sign conversion occurs once."""
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
