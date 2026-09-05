"""The bench: which object plays which role, plus the hardware constants.

Two configs, deliberately separate.

`RigConfig` describes **the bench** — addresses, module numbers, the sense
resistor, the amplifier gain, which scope channel carries what. It changes when
someone rewires something, and it is wrong to bury it in a measurement recipe.

`RunConfig` (in `experiment.transient`) describes **the measurement** — averages,
timebase, integration window, settle times. It is copied verbatim into the
output so a result carries its own recipe.

The split exists because the failure they prevent is different: a stale
`RigConfig` silently scales every charge, a stale `RunConfig` merely repeats a
measurement you did not mean to repeat.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from ..drivers.protocols import (BiasSource, Digitizer, LedSource, PowerMeter,
                                 Router, Shutter, SourceMeter, TemperatureController)


@dataclass(frozen=True)
class RigConfig:
    """Hardware constants. Values are the ones confirmed on 'sternwarte'."""

    sense_resistor_ohm: float = 5.192
    """The resistor between the device and the scope input. Absolute charge is
    proportional to 1/R, so this is the single most consequential number here.
    The panel note reads '(50 old big Amp) (5.192 new Amp)'."""

    current_sign: float = -1.0
    """Sign convention of the digitiser chain. Multiplied ONCE, in the
    digitizer fetch, beside the division by `sense_resistor_ohm`.

    Measured 2026-09-02 against the LabVIEW engine on the same device, eight
    minutes apart: this port reported every current positive, LabVIEW every
    one negative, and the two agree in shape and size (dark-trace
    corr(-bare, lv) = 0.9988; peak 6.0 %, tau 2.8 %, Q 4.0 % apart). That is a
    convention about which way round the sense resistor is read, not physics,
    so it is a bench constant and is applied in exactly one place --
    `Infiniium._fetch`, and `SimulatedDigitizer.acquire` so the simulator
    reports in the rig's convention too. `_fetch_volts` stays sign-free: the
    auto-range works in volts at the scope input, and a sign there would
    move the window instead of the number.

    It is NOT the generator's polarity switch and must not be fixed with one.
    `:OUTP1:POL` changes the voltage the device really sees -- which edge of
    the pulse the extraction happens on -- while this changes only the sign
    of the number written down. The value reaches every file through
    `as_dict()` (HDF5 `/config/rig`), so a reader can undo it.
    """

    pulse_amp: float = 4.0
    """Voltage gain of the amplifier between the 81150A and the device. Bias
    levels are divided by it; the measured current does not pass through it."""

    probe_attenuation: float = 1.0
    current_source: str = "CHAN2"
    scope_channel: int = 2
    trigger_source: str = "CHAN3"
    """The 81150A sync. The only in-band record of when the field arrived."""

    trigger_positive: bool = True
    trigger_offset_s: float = 0.0
    """Latency between the 81150A's Sync edge (what the scope triggers on) and
    the field reaching the device, **after** `:PULS:DEL1`: the field arrives
    at `trigger + :PULS:DEL1 + trigger_offset_s`. Measured 47.1 ns on this
    bench (2026-09-03 delay scan, 49 points fitted, 1.5 ns residual), which
    is what `rig.toml` carries.

    It is *not* added to what the generator is told -- `:PULS:DEL1` gets
    `delay_ns` as it is, as the LabVIEW looping path did (2026-09-02). It
    positions the integration window (`experiment.transient.resolve_window`),
    so that `RunConfig.t0_int_s` is measured from the field, not from a
    command. 0 means the latency has not been measured on this bench, and the
    window is then measured from `:PULS:DEL1` itself."""

    light_path_delay_ns: float = 0.0
    """From the 33220A's drive edge to the light actually going off at the
    sample: measured 502 ns on this bench (2026-09-01, `docs/bace-timing.html`),
    419 ns of it the 85 m fibre. Informational -- it does not enter the delay
    axis or the window -- and shown on the rig tab beside `trigger_offset_s` so
    the two constants of the chain's timing are on record together. 0 means
    it has not been measured on this bench."""

    led_threshold_v: float = 1.0
    dio_module_id: int = 9
    shutter_module_nr: int = 0
    relay_module_nr: int = 1

    dio_dll_path: str = ""
    """Which DELIB library to load, when the automatic choice is wrong.

    Empty means "let `drivers.delib` pick the build matching this interpreter":
    Deditec ships 32-bit as `delib.dll` in SysWOW64 and 64-bit as `delib64.dll`
    in System32, and `ctypes` can only load the matching one. Set this only to
    override that -- to test a specific build, or to use a copy that is not in
    either system folder.
    """

    # -- VISA addresses, confirmed on the bench 2026-08-31 ----------------
    scope_address: str = "TCPIP0::PwM-DSO9054H.local::inst0::INSTR"
    """LAN, not GPIB. The instrument also answers on ::hislip0::INSTR and
    ::5025::SOCKET; inst0 is the one the rig has selected."""

    bias_address: str = "GPIB0::12::INSTR"
    """81150A -> x4 amplifier -> device."""

    sourcemeter_address: str = "GPIB0::24::INSTR"
    """Keithley 2400 (MODEL 2400, firmware C34). Note: an older saved panel
    default said GPIB0::20, which is wrong -- 24 is what answers *IDN?."""

    led_address: str = "GPIB0::15::INSTR"
    """33220A driving the LED through a fixed-gain amplifier."""

    power_meter_wavelength_nm: float = 530.0
    power_meter_dll: str = ""
    """`usbdll.dll` for the Newport USB driver. Empty searches the standard
    install paths and picks the build matching this interpreter's bitness --
    the package installs both, and taking the first one that exists picks
    wrong half the time and yields a bare WinError 193."""

    power_meter_averaging: bool = True
    """Put the 1918-C in DC-continuous mode with its 5 Hz analog filter at
    open, so a reading is the time average of the light. The LED is pulsed
    at 500 Hz, 50 % duty for every transient, and an unfiltered meter shows
    one instant of that square wave -- the full level, nothing, or a flicker
    -- where the average (half the DC level) is the only number that is a
    power. `false` leaves the meter unfiltered for a measurement that wants
    the instantaneous value; the mode is set to DC-continuous either way."""

    power_meter_digital_filter: int = 100
    """`PM:DIGITALFILTER` samples on top of the analog filter when averaging
    is on. Light smoothing only -- the analog filter does the averaging --
    and short enough that the LED settle still sees the intensity move
    inside its 0.5 s poll. 0 disables it."""

    power_meter_console: str = ""
    """Empty (the default) means **this process opens the 1918-C itself**.
    Only one process can hold the USB device; on this rig that process is the
    service. Set this to the meter console's URL only if that program is
    running and should keep the handle -- then the service asks it over HTTP
    instead of failing to open a device it cannot have."""

    temperature_address: str = "GPIB0::7::INSTR"
    """Lake Shore 331. Address 7 is what this instrument answers on; the
    manual's factory default is 12, so a fresh box would differ."""

    temperature_max_setpoint_k: float = 350.0
    """The ceiling for this cryostat. A setpoint above it is **refused**, never
    clamped: quietly giving 350 K for a requested 400 K hides the mistake."""

    temperature_control_loop: int = 1
    """Which loop to drive: 1 is the heater output, 2 the analog voltage
    output. A choice, not something to infer -- both loops can be active at
    once and writing to the wrong one fails silently."""

    temperature_console: str = ""
    """Empty (the default) means **this process opens the 331 itself** at
    `temperature_address`, and a temperature loop settles through it. Set this
    to the 331 console's URL only if that program is running: it holds the
    only GPIB session while it does, and a second session from here would
    interleave with its poller on the bus. Either way the 350 K ceiling, the
    heater range, the PID and the ramp stay where they are -- read, never
    driven by a run."""

    # -- bench ceilings ---------------------------------------------------
    max_current_compliance_a: float = 0.05
    """The most any run may ask the SourceMeter to deliver. The working value
    is a *run* setting, because it depends on the pixel; this is the bench's
    hard limit, because a 2400 will happily push 1 A into a small cell and no
    measurement recipe should be able to authorise that."""

    max_voltage_compliance_v: float = 5.0

    def __post_init__(self) -> None:
        # A sign is +1 or -1 and nothing else. Any other magnitude would be a
        # gain hiding under the wrong name, and it would rescale every charge
        # with the same silence a wrong sense resistor does.
        if self.current_sign not in (1.0, -1.0):
            raise ValueError(
                f"current_sign must be +1 or -1, not {self.current_sign!r}. A "
                "gain belongs in sense_resistor_ohm, where it can be read as one."
            )

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Rig:
    """The instruments, by role.

    Only `bias`, `scope` and `shutter` are required — that is exactly the
    standalone measurement (`TDCF-BACE.vi`), which has no relay, no SourceMeter
    and no LED control of its own. The rest appear when the intensity series
    needs them.
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
    """The 331, through its console (`RigConfig.temperature_console`). None
    when no console is named or the named one does not answer: a temperature
    node then pauses for the operator instead of settling on its own. Not
    parked -- the cryostat keeps its setpoint when a run ends, because
    walking it back to room temperature is a decision, not a safe state."""

    def park(self) -> None:
        """Leave the bench safe: outputs off, shutter closed."""
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
