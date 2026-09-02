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
    """Latency between the sync edge and the field reaching the device."""

    light_path_delay_ns: float = 0.0
    """From the 33220A's drive edge to the light actually going off at the
    sample: measured 502 ns on this bench (2026-09-01, `docs/bace-timing.html`),
    419 ns of it the 85 m fibre. Informational -- the delay axis is zeroed by
    `trigger_offset_s`, not by this -- and shown on the rig tab beside it so
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
    power_meter_console: str = "http://127.0.0.1:8918"
    """The 1918-C console owns the USB device — only one process can. The BACE
    program asks it over HTTP rather than fighting for the handle."""

    temperature_console: str = ""
    """Lake Shore 331 at GPIB0::7::INSTR, owned by its own console on
    127.0.0.1:8331. Naming the console here attaches it as `Rig.temperature`
    (`service.rigs.Bench.build_real`), and a temperature loop then settles
    through it -- setpoint written, the band waited for, the console's own
    350 K ceiling and heater range left to the console. Empty keeps
    temperature a number an operator types at each pause."""

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
