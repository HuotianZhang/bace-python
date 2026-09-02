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

from ..drivers.protocols import (BiasSource, Digitizer, PowerMeter, Router,
                                 Shutter, SourceMeter)


@dataclass(frozen=True)
class RigConfig:
    """Hardware constants. Values are the ones confirmed on 'sternwarte'."""

    sense_resistor_ohm: float = 5.192
    """The resistor between the device and the scope input. Absolute charge is
    proportional to 1/R, so this is the single most consequential number here.
    The panel note reads '(50 old big Amp) (5.192 new Amp)'."""

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
    127.0.0.1:8331. Empty until temperature is integrated, which is the step
    after this one; the field exists so nothing has to move when it is."""

    # -- bench ceilings ---------------------------------------------------
    max_current_compliance_a: float = 0.05
    """The most any run may ask the SourceMeter to deliver. The working value
    is a *run* setting, because it depends on the pixel; this is the bench's
    hard limit, because a 2400 will happily push 1 A into a small cell and no
    measurement recipe should be able to authorise that."""

    max_voltage_compliance_v: float = 5.0

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
    led: object | None = None

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
