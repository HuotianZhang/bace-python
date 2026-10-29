"""Bench configuration assigns instruments to roles and defines hardware constants.

`RigConfig` describes addresses, channels, R_s and amplifier gain. It changes
with wiring and calibration. A wrong R_s or gain silently rescales every Q.
`bace.experiment.transient.RunConfig` describes acquisition and processing.
Both are stored with the result so its scale and measurement contract remain
explicit. Hardware constants belong outside the run parameters.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from ..drivers.protocols import (BiasSource, Digitizer, LedSource, PowerMeter,
                                 Router, Shutter, SourceMeter, TemperatureController)


@dataclass(frozen=True)
class RigConfig:
    """Hardware constants define this bench's connections, scales and limits."""

    sense_resistor_ohm: float = 5.192
    """Resistance between the device and digitiser input, in Ω, used to convert measured voltage to current.

    R_s is 5.192 Ω on this bench (manual 1.2). Absolute Q scales as `1 / R_s`, so
    an incorrect value rescales every charge.
    """

    current_sign: float = -1.0
    """Sign convention of the digitiser chain: multiply measured current once by this dimensionless factor, +1 or −1.

    `Infiniium._fetch` applies it once beside division by `sense_resistor_ohm`.
    `SimulatedDigitizer.acquire` uses the same convention. Bench comparison with
    LabVIEW gives a dark-trace correlation of 0.9988 after sign reversal, with
    peak, decay time and Q differing by 6.0 %, 2.8 % and 4.0 %. These are measured
    bench comparisons, not an absolute calibration.

    `Infiniium._fetch_volts` applies no current sign: auto-range works in input
    volts, and changing the sign there moves the vertical window.

    The sign must not be corrected with `:OUTP1:POL`. That command changes the
    voltage at the device and the pulse edge that extracts charge; `current_sign`
    changes only the recorded current convention, including which way R_s is
    read. `RigConfig.as_dict` carries it into HDF5 `/config/rig`, so readers can reverse the conversion.
    """

    pulse_amp: float = 4.0
    """Voltage gain of the amplifier between the 81150A and device, in V/V; requested device bias levels are divided by it.

    Bias levels are divided by `pulse_amp`. Measured current does not pass
    through this amplifier.
    """

    probe_attenuation: float = 1.0
    current_source: str = "CHAN2"
    scope_channel: int = 2
    trigger_source: str = "CHAN3"
    """Digitiser channel carrying the 81150A Sync edge used to trigger acquisition and check field-arrival timing.

    It is the in-record timing reference; field arrival also includes the pulse
    delay and `trigger_offset_s`.
    """

    trigger_positive: bool = True
    trigger_offset_s: float = 0.0
    """Latency from the 81150A Sync edge to field arrival at the device after `:PULS:DEL1`, in s, used to position the integration window.

    Field arrival is trigger plus `:PULS:DEL1` plus `trigger_offset_s`. This
    bench measures 35.1 ns, the 50 % point of the voltage step on a 56 pF
    capacitor in the device's place, the same at every delay (`docs/notes.md`).
    A device's displacement spike comes later by its own RC time. `rig.toml`
    carries that value.

    The offset positions the integration window through
    `bace.experiment.transient.resolve_window`; `RunConfig.t0_int_s` is relative
    to field arrival. LabVIEW compatibility requires sending `delay_ns` unchanged
    to `:PULS:DEL1`, without adding the latency. Zero denotes an unmeasured latency
    and places the integration window relative to the set delay alone.
    """

    light_path_delay_ns: float = 0.0
    """Time from the 33220A drive edge to light going off at the sample, in ns; recorded for information without changing the delay or integration window.

    This bench measures 502 ns, including 419 ns in the 85 m fibre (manual 1.2).
    It is informational and does not enter the delay axis or integration window.
    The console shows it beside `trigger_offset_s` to record both timing paths.
    Zero denotes an unmeasured light path delay.
    """

    led_threshold_v: float = 1.0
    dio_module_id: int = 9
    shutter_module_nr: int = 0
    relay_module_nr: int = 1

    dio_dll_path: str = ""
    """Which DELIB library to load for the Deditec shutter and relay modules when automatic selection is wrong; empty uses automatic selection.

    An empty path lets `bace.drivers.delib` choose a build matching the Python
    interpreter. Deditec installs 32-bit `delib.dll` in SysWOW64 and 64-bit
    `delib64.dll` in System32. `ctypes` can load only the matching bitness.
    An explicit path selects a specific build or a copy outside those folders.
    """

    # -- VISA addresses for this bench ------------------------------------
    scope_address: str = "TCPIP0::PwM-DSO9054H.local::inst0::INSTR"
    """VISA address for connecting to the digitiser over LAN through `inst0`.

    It also answers on `::hislip0::INSTR` and `::5025::SOCKET`; this bench selects
    `inst0`.
    """

    bias_address: str = "GPIB0::12::INSTR"
    """VISA address for connecting to the 81150A, which drives the device through the ×4 amplifier."""

    sourcemeter_address: str = "GPIB0::24::INSTR"
    """VISA address for connecting to the Keithley 2400 source meter.

    It answers `*IDN?` at GPIB address 24 on this bench, not address 20.
    """

    led_address: str = "GPIB0::15::INSTR"
    """VISA address for connecting to the 33220A, which drives the LED through a constant-gain amplifier."""

    power_meter_wavelength_nm: float = 520.0
    power_meter_dll: str = ""
    """Path to Newport `usbdll.dll` for accessing the 1918-C power meter over USB; empty searches standard installation folders for matching interpreter bitness.

    An empty path searches standard installation folders for the interpreter's
    bitness. The package installs both builds; choosing the first existing file
    can select the wrong bitness and raise WinError 193.
    """

    power_meter_averaging: bool = True
    """Put the 1918-C in DC-continuous mode with its 5 Hz analog filter at connection, so readings give time-averaged optical power.

    It returns time-averaged optical power. At 500 Hz and 50 % duty, that is half
    the LED's DC power (manual 1.2). An unfiltered reading samples the waveform
    and can show full power, zero or a fluctuating value. `False` leaves the
    meter unfiltered for instantaneous readings. Both settings use DC-continuous
    mode.
    """

    power_meter_digital_filter: int = 100
    """Number of 1918-C `PM:DIGITALFILTER` samples used to smooth readings after the analog filter when averaging is enabled; zero disables digital filtering.

    `PM:DIGITALFILTER` applies when `power_meter_averaging` is enabled. It smooths
    readings; the analog filter averages the pulses. The default permits LED
    intensity changes to appear within the settle check's 0.5 s polling interval.
    Zero disables the digital filter.
    """

    power_meter_console: str = ""
    """URL of the console providing HTTP access to the 1918-C power meter; empty opens the meter directly in this process.

    An empty URL makes this process open the 1918-C directly. Only one process
    can hold the USB device. A console URL is required when that program holds
    the handle; direct access would fail.
    """

    temperature_address: str = "GPIB0::7::INSTR"
    """VISA address for connecting to the Lake Shore 331 temperature controller.

    It answers at 7 on this bench; an instrument at the factory address 12 needs
    a different setting.
    """

    temperature_max_setpoint_k: float = 350.0
    """Highest setpoint permitted for this cryostat's Lake Shore 331, in K; requests above it are refused.

    Requests above the ceiling are refused. Clamping a request for 400 K to
    350 K would conceal the wrong target.
    """

    temperature_control_loop: int = 1
    """Which Lake Shore 331 loop to drive: 1 is the heater output, 2 the analog voltage output.

    Both loops can be active, so the choice must be explicit. Writing to the
    wrong loop can fail silently.
    """

    temperature_console: str = ""
    """URL of the console providing HTTP access to the Lake Shore 331; empty opens `temperature_address` directly in this process.

    An empty URL makes this process open `temperature_address` directly and
    settle temperature loops through that connection. A running 331 console
    holds the GPIB session; another session would interleave with its poller.

    Both routes enforce the 350 K ceiling. Runs can change the setpoint but only
    read the heater range, PID and ramp.
    """

    # -- bench ceilings ---------------------------------------------------
    max_current_compliance_a: float = 0.05
    """The most any run may ask the Keithley 2400 source meter to deliver, in A.

    Working compliance is a run parameter because it depends on the pixel. This
    bench limit prevents a run from requesting the 2400's 1 A capability, which
    can damage a small device.
    """

    max_voltage_compliance_v: float = 5.0

    def __post_init__(self) -> None:
        # `current_sign` permits only +1 or −1. Another magnitude acts as a
        # hidden gain and silently rescales Q, like an incorrect R_s.
        if self.current_sign not in (1.0, -1.0):
            raise ValueError(
                f"current_sign must be +1 or -1, not {self.current_sign!r}. A "
                "gain belongs in sense_resistor_ohm, where it can be read as one."
            )

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Rig:
    """The rig groups instruments by measurement role.

    Only `bias`, `scope` and `shutter` are required for a standalone transient
    scan, matching the LabVIEW standalone instrument contract. An intensity
    series also requires the LED source, source meter and router.
    """

    bias: BiasSource
    scope: Digitizer
    shutter: Shutter
    config: RigConfig = RigConfig()
    router: Router | None = None
    smu: SourceMeter | None = None
    power: PowerMeter | None = None
    led: LedSource | None = None
    temperature: TemperatureController | None = None
    """The temperature controller provides temperature read-back and settling.

    `None` leaves temperature nodes to pause for the operator. The controller
    can use a direct connection or `RigConfig.temperature_console`. Park does
    not change its setpoint: returning the cryostat to room temperature requires
    an explicit decision.
    """

    def park(self) -> None:
        """Park attempts to disable bias and source-meter outputs and shut the shutter.

        It also parks the router when present. Failures are suppressed so one failed
        action does not prevent the remaining actions. The LED keeps running.
        """
        for dev in (self.bias, self.smu):
            if dev is not None:
                try:
                    dev.disable_output()
                except Exception:
                    pass
        try:
            self.shutter.shut()
        except Exception:
            pass
        if self.router is not None:
            try:
                self.router.park()
            except Exception:
                pass
