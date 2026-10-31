"""The bench owns one `Rig`, its read-back and explicit actions.

`Bench` is built once per process and accessed through the worker. Real
assembly uses the drivers and `bench.checks._dio_backend` shared with the
command-line bench tools. A missing instrument is recorded in `unavailable`
so the service can show which role is missing instead of failing startup.

Read-back reports instrument state. Wrong 33220A polarity or 81150A arming
can produce plausible files with invalid timing. Comparing the trigger
chain with the recipe exposes that hazard. `Bench.read_back` writes nothing,
so observing the bench does not change the state being reported.

`Bench.action` handles explicit changes outside a run and reports the result
for the journal. It refuses LED polarity changes with output on and relay
moves with a live or unreadable source. These actions require an explicit
request (`docs/service-contract.md`, section 4).

Measurement logic belongs to the engine. `Bench.build_real` imports `pyvisa`
and real drivers lazily so `--sim` and tests need no VISA backend."""
from __future__ import annotations

import math
import os
import threading
import time
from typing import Any, Callable

from ..bench.checks import fingerprint
from ..core.illumination import IlluminationError, LedDrive
from ..drivers.keithley2400 import SourceMeterConfig
from ..drivers.lakeshore331 import (ConsoleTemperatureController,
                                    open_temperature_controller)
from ..drivers.simulated import SimulatedRig, make_bench
from ..experiment import events as E
from ..experiment.rig import Rig, RigConfig
from ..experiment.transient import RunConfig
from ..experiment.wire import to_wire

LED_EXPECTED_POLARITY = "INV"
"""The 33220A polarity must make the Sync rising edge mean light off.

With `:OUTP:POL INV`, the waveform is inverted while Sync is unchanged.
The 81150A arms on the Sync rising edge, so extraction follows light off.
NORM makes this edge mean light on. The collection pulse then arrives during
carrier generation but can still produce plausible Q
(`docs/service-contract.md`, section 4)."""

ACTIONS: tuple[str, ...] = (
    "park",
    "set-33220a-pol-inv", "set-33220a-pol-norm",
    "arm-81150a-ext",
    "set-led-pulse", "set-led-dc",
    "led-off", "bias-off", "smu-off",
    "shutter-open", "shutter-shut",
    "relay-to-sourcemeter", "relay-to-amplifier",
    "read-power",
)
"""The bench accepts these action names.

`Bench.action` refuses any other name with `KeyError`; the route returns
404 (`docs/service-contract.md`, section 4)."""

RIG_VALUE_KEYS: tuple[str, ...] = (
    "sense_resistor_ohm", "pulse_amp", "current_sign", "trigger_offset_s",
    "light_path_delay_ns", "probe_attenuation", "led_threshold_v",
    "max_current_compliance_a", "max_voltage_compliance_v",
)
"""The bench card shows the `rig.toml` scaling, sign, timing and ceiling values.

They affect Q or constrain instrument operation. Addresses are omitted:
unavailable instruments already carry an access failure reason."""

ACTION_BEFORE: dict[str, tuple[tuple[str, str], ...]] = {
    "set-33220a-pol-inv": (("led", "polarity"),),
    "set-33220a-pol-norm": (("led", "polarity"),),
    "arm-81150a-ext": (("bias", "arm_source"), ("bias", "arm_slope")),
    "set-led-pulse": (("led", "mode"), ("led", "high_v"), ("led", "low_v"),
                      ("led", "frequency_hz"), ("led", "output")),
    "set-led-dc": (("led", "mode"), ("led", "high_v"), ("led", "output")),
    "led-off": (("led", "output"),),
    "bias-off": (("bias", "output"),),
    "smu-off": (("smu", "output"),),
    "shutter-open": (("shutter", "open"),),
    "shutter-shut": (("shutter", "open"),),
    "relay-to-sourcemeter": (("relay", "position"),),
    "relay-to-amplifier": (("relay", "position"),),
    "park": (("bias", "output"), ("smu", "output"), ("led", "output"), ("shutter", "open")),
}
"""The action fields identify read-back values changed by each action.

`BenchAction` needs both before and after values for a self-contained journal
entry. A client joining later has no preceding snapshot to recover them."""

ACTION_DOCS: dict[str, tuple[str, str, str]] = {
    "park": (
        "81150A, 2400, shutter",
        "Bias and source-meter outputs off, shutter shut. The LED keeps running and the "
        "relay stays where it is. While a run is active it aborts the run and cancels "
        "the queue.",
        "Never."),
    "set-33220a-pol-inv": (
        "33220A",
        "`:OUTP:POL INV`: the Sync's rising edge becomes light off, the edge the "
        "81150A arms on.",
        "The 33220A output is on, or does not answer its output query."),
    "set-33220a-pol-norm": (
        "33220A",
        "`:OUTP:POL NORM`.",
        "As `set-33220a-pol-inv`."),
    "arm-81150a-ext": (
        "81150A",
        "`:ARM:SOUR1 EXT`, rising edge: one bias step per LED cycle.",
        "The 81150A output is on, or does not answer its output query."),
    "set-led-pulse": (
        "33220A",
        "The LED as a pulse at the `bace` parameters in force (`led_v`, `led_low_v`, "
        "`pulse_frequency_hz`, `duty_percent`), or at the values in the request; output "
        "on.",
        "The level is below the LED threshold or the low level is not below it, the "
        "frequency is not positive, or the duty is outside 0–100 %."),
    "set-led-dc": (
        "33220A",
        "The LED on continuously at `led_v` or the requested level, as for a Voc "
        "measurement; output on.",
        "No level is given or in force."),
    "led-off": ("33220A", "Output off.", "Never."),
    "bias-off": ("81150A", "Output off.", "Never."),
    "smu-off": ("2400", "Output off.", "Never."),
    "shutter-open": ("Shutter", "DIO module 0, channel 0 = 1: light on the sample.", "Never."),
    "shutter-shut": ("Shutter", "DIO module 0, channel 0 = 0.", "Never."),
    "relay-to-sourcemeter": (
        "Relay",
        "DIO module 1, channel 0 = 1: the device to the 2400. The 2400 output is "
        "then off.",
        "A source output is on and the relay would move, or a source does not answer "
        "its output query (`crit`)."),
    "relay-to-amplifier": (
        "Relay",
        "DIO module 1, channel 0 = 0: the device to the amplifier and the scope. "
        "The 81150A output is then off.",
        "As `relay-to-sourcemeter`."),
    "read-power": (
        "1918-C",
        "One power reading.",
        "The meter does not answer."),
}
"""The action descriptions list instruments, effects and refusal conditions.

An absent instrument always causes refusal. Each action is followed by
read-back. `tools/build_manual.py` generates manual 4.3 from this table."""
assert tuple(ACTION_DOCS) == ACTIONS

INSTRUMENT_KEYS: tuple[str, ...] = (
    "relay", "bias", "smu", "shutter", "led", "voc", "power", "temperature")

_RELAY_POSITION = {0: "amplifier", 1: "sourcemeter"}

SLEEP_SLICE_S = 0.2
"""The sleep slice bounds abort checks in seconds.

Settles and holds check the flag between slices so abort does not wait for
a full 60 s hold or 1800 s wait."""

WATCHDOG_POLL_S = 30.0
"""The worker feeds the 331 heater watchdog at this interval in seconds.

The monitor cannot read while a run holds the bus. Because reads feed the
watchdog, a heater fault during a long run could otherwise remain undetected
for hours. `Bench.sleeper` feeds it while the worker owns the bus and waits.
Two queries per half minute check for thermal runaway, which develops over
minutes. A 0.3 s settle between shots rarely contains a watchdog tick
(`docs/service-contract.md`, section 7)."""

_METER_LOCK = threading.Lock()
"""The meter lock serializes `power_reading` across monitor and worker threads.

The driver retains its last reading in `last`. Without the lock, a worker
reading could carry the monitor reading's trustworthiness flag."""


class BenchActionRefused(RuntimeError):
    """A bench action refusal reports the unsafe or unavailable state.

    `level` is `crit` for the relay interlock: moving a relay under a live source
    can damage hardware. Other refusals use `warn`. `text` explains the reason.
    A valid request can succeed once its required output is off; the route
    reports the refusal for the operator to resolve
    (`docs/service-contract.md`, section 4)."""

    def __init__(self, level: str, text: str):
        super().__init__(text)
        self.level = level
        self.text = text


class _NoCryostat(Exception):
    """The absent-cryostat exception represents a bench configured without a 331.

    Neither a temperature address nor a console is configured. This internal
    `Bench.build_real` condition is not an unavailable-role failure."""


class BenchUnavailable(RuntimeError):
    """The unavailable-instrument error rejects use of a required missing instrument."""


class Unavailable:
    """The unavailable stand-in represents a required instrument that cannot open.

    `Rig` requires `bias`, `scope` and `shutter`. A missing role needs a stand-in
    that fails on first use with the access reason. Using `None` instead would
    raise an uninformative `AttributeError` inside the run. `Rig.park` suppresses
    this failure as it does instrument errors.

    Read-back detects the stand-in with `isinstance(x, Unavailable)`. Its error
    is not `AttributeError`, so `getattr(x, name, default)` cannot silently
    substitute a default for a missing instrument."""

    def __init__(self, role: str, reason: str):
        self.role = role
        self.reason = reason

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):            # copy/pickle/inspect probes
            raise AttributeError(name)
        raise BenchUnavailable(f"{self.role} is not available: {self.reason}")

    def __repr__(self) -> str:
        return f"Unavailable({self.role!r}: {self.reason})"


class _RelayLine:
    """The relay-line adapter supplies `set(channel, value)` to `BiasRouter`.

    The shutter driver opens the same module type at a different module number.
    This adapter hides `unblock` and `shut` so callers cannot bypass the relay
    interlock through shutter methods. `BiasRouter` moves only after both sources
    report off. Passing through `read_line` lets read-back use the module's
    actual position rather than the router's cached position."""

    def __init__(self, line):
        self._line = line

    def set(self, channel: int, value: int) -> None:
        self._line.set_line(value)

    def read_line(self) -> int | None:
        return self._line.read_line()


def _usable(dev: Any) -> bool:
    return dev is not None and not isinstance(dev, Unavailable)


def _try(fn: Callable[[], Any], default: Any = None) -> Any:
    """The guarded read returns its default if the instrument raises.

    A complete snapshot must remain available with a failed instrument.
    An unknown field is represented by `?` or `None`."""
    try:
        return fn()
    except Exception:
        return default


def _no_sleep(seconds: float) -> None:
    return None


def _read_output(dev: Any) -> bool | None:
    """The output state comes from the instrument or the simulator's shared state.

    Real drivers use `read_output`; the simulator uses its cached flag. `None`
    means no answer. A real driver's flag starts false regardless of front-panel
    state. Trusting it without a read could move the relay under a live source.
    `read_output` refreshes that flag so the router's interlock sees the reading
    (`docs/service-contract.md`, section 4)."""
    if not _usable(dev):
        return None
    read = getattr(dev, "read_output", None)
    if callable(read):
        # A failed instrument query must return `None`, not a cached flag.
        # An unanswered output query is not evidence of an off output for
        # the relay interlock.
        on = _try(read)
        return None if on is None else bool(on)
    return _try(lambda: bool(dev.output_enabled))


def _read_state(dev: Any) -> dict:
    """The state read returns `read_state()` or an empty dictionary without raising.

    Real generators provide this method; drivers without it return no fields."""
    read = getattr(dev, "read_state", None)
    if not callable(read):
        return {}
    state = _try(read)
    return dict(state) if isinstance(state, dict) else {}


def action_before(name: str, snapshot: dict | None) -> dict:
    """The action's affected fields use values from the cached read-back.

    The result maps `"<instrument>.<field>"` to its value. It is empty without a
    read-back or when the action changes no displayed field."""
    instruments = (snapshot or {}).get("instruments") or {}
    out: dict[str, Any] = {}
    for instrument, field in ACTION_BEFORE.get(name, ()):
        block = instruments.get(instrument)
        if isinstance(block, dict) and field in block:
            out[f"{instrument}.{field}"] = block[field]
    return out


def _package_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _float_or_none(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# -- observers ------------------------------------------------------------
def power_reading(meter, *, samples: int = 1) -> E.PowerReading:
    """The power read returns one `PowerReading` from a real or simulated meter.

    Read-back, bench actions, the `power` module and the monitor share it so
    trustworthiness has one meaning. Drivers retaining a verdict supply it from
    `last`, as `ConsolePowerMeter` does. Other drivers report true. Meter errors
    propagate; callers decide whether they mean refusal, warning or NaN."""
    with _METER_LOCK:
        if samples > 1:
            watts, _std = meter.read_statistics(int(samples))
        else:
            watts = meter.read_power()
        last = getattr(meter, "last", None)
    trustworthy = True if last is None else bool(getattr(last, "trustworthy", True))
    wavelength = None
    averaged = None
    if last is not None:
        wavelength = _float_or_none(getattr(last, "wavelength_nm", None))
        averaged = getattr(last, "averaged", None)
    if wavelength is None:
        wavelength = _float_or_none(getattr(meter, "wavelength_nm", None))
    if averaged is None:
        # The simulated meter keeps no `Reading`; it says on itself.
        averaged = getattr(meter, "averaged", None)
    if averaged is not None:
        averaged = bool(averaged)
    meter_range = getattr(last, "range", None) if last is not None else None
    if meter_range is None:
        meter_range = getattr(meter, "range", None)
    auto_range = getattr(meter, "auto_range", None)
    # Check `source` before `base_url`: the direct driver has no URL.
    # Otherwise a real power reading could be labelled simulated.
    source = (getattr(meter, "source", None) or getattr(meter, "base_url", None)
              or "simulated")
    return E.PowerReading(watts=float(watts), trustworthy=trustworthy,
                          wavelength_nm=wavelength, source=str(source),
                          averaged=averaged, meter_range=meter_range,
                          auto_range=auto_range)


def apply_led(led, mode: str, *, level: float | None = None,
              low: float | None = None, frequency_hz: float | None = None,
              duty_percent: float | None = None,
              threshold_v: float | None = None) -> dict:
    """The LED control requests `dc`, `pulse` or `off` and reports the request.

    The caller's following read-back determines the result. Bench actions and
    `light` nodes share this implementation, so they drive the LED identically.
    `LedDrive` enforces level rules, including a low level below threshold.
    Callers translate its `IlluminationError` into their refusal."""
    mode = str(mode).lower()
    if mode == "off":
        led.off()
        return {"mode": "OFF", "output": False}
    if mode == "dc":
        if level is None:
            raise ValueError("dc needs a level")
        led.set_dc(float(level))
        led.enable_output(True)
        return {"mode": "DC", "level_v": float(level), "output": True}
    if mode == "pulse":
        if level is None or low is None or frequency_hz is None or duty_percent is None:
            raise ValueError("pulse needs level, low, frequency_hz and duty_percent")
        drive = LedDrive(level=float(level), low_level=float(low),
                         frequency_hz=float(frequency_hz),
                         duty_percent=float(duty_percent),
                         threshold_v=threshold_v)
        s = drive.pulse_settings()
        led.set_pulse(s["high"], s["low"], frequency_hz=s["frequency"],
                      duty_percent=s["duty"])
        led.enable_output(True)
        return {"mode": "PULSE", "high_v": s["high"], "low_v": s["low"],
                "frequency_hz": s["frequency"], "duty_percent": s["duty"],
                "output": True}
    raise ValueError(f"unknown LED mode {mode!r}: dc, pulse or off")


def apply_shutter(shutter, open_it: bool) -> dict:
    """The shutter control requests a position and returns the driver's read-back."""
    if open_it:
        shutter.unblock()
    else:
        shutter.shut()
    return {"open": bool(shutter.is_open)}


def temperature_source(controller: Any) -> str:
    """The temperature source distinguishes instrument, console and simulator.

    `instrument` means this process owns the 331 GPIB session. `console` means
    an HTTP request to the console that owns it. Both are measured; `simulated`
    is not. The simulator is the final fallback so a measured value cannot be
    labelled simulated (`docs/service-contract.md`, section 7)."""
    if getattr(controller, "base_url", None):
        return "console"
    if getattr(controller, "resource", None):
        return "instrument"
    return "simulated"


def read_temperature_console(source: Any, *, timeout_s: float = 2.0) -> dict:
    """The temperature snapshot describes the attached controller and its reading.

    `source` is the rig's `TemperatureController`, a console URL for a monitor,
    or empty. A monitor uses the configured console even if startup cannot reach
    it. `wired` means a controller is attached: direct, console-backed or
    simulated. It determines whether a temperature node can settle automatically
    or must pause. The bench adds the reason for a configured console that fails
    to attach, so the card distinguishes absence from an unavailable console.
    `connected` reports whether the instrument behind the controller answers.
    A running console with a silent 331 is wired but not connected. `in_band`
    is `None` because tolerance belongs to the waiting loop, not the bench."""
    if not source:
        return {"wired": False, "kelvin": None, "setpoint_k": None,
                "in_band": None, "source": None}
    controller = (ConsoleTemperatureController(source, timeout_s=timeout_s)
                  if isinstance(source, str) else source)
    origin = temperature_source(controller)
    try:
        r = controller.read()
    except Exception as exc:                                # noqa: BLE001
        return {"wired": True, "kelvin": None, "setpoint_k": None, "in_band": None,
                "source": origin, "connected": False, "ramping": None, "heater_range": None,
                "status_text": None, "max_setpoint_k": None,
                "error": f"{type(exc).__name__}: {exc}"}
    kelvin = _float_or_none(r.kelvin)
    if kelvin is not None and not math.isfinite(kelvin):
        kelvin = None
    return {"wired": True, "kelvin": kelvin, "setpoint_k": _float_or_none(r.setpoint_k),
            "in_band": None, "source": origin, "connected": bool(r.connected),
            "ramping": r.ramping, "heater_range": r.heater_range,
            "status_text": r.status_text, "max_setpoint_k": _float_or_none(r.max_setpoint_k)}


# -- the bench --------------------------------------------------------------
class Bench:
    """The bench owns one `Rig` and its service state outside runs.

    `Bench.build_simulated` and `Bench.build_real` are the only assembly paths.
    They supply R_s, current sign and probe attenuation to the digitiser.
    Another assembly path could apply different scaling conventions."""

    def __init__(self, *, rig: Rig, rig_config: RigConfig, mode: str,
                 run_config: RunConfig | None = None,
                 unavailable: dict[str, str] | None = None,
                 rig_path: str | None = None, sim: SimulatedRig | None = None,
                 fast: bool = False, relay_line: _RelayLine | None = None,
                 closers: tuple[Callable[[], None], ...] = (),
                 startup_writes: tuple[str, ...] = ()):
        self.rig = rig
        self.rig_config = rig_config
        self.mode = mode
        self.startup_writes: tuple[str, ...] = tuple(startup_writes)
        """Startup writes list instrument changes made before the worker starts.

        The scope setup and meter units and wavelength belong in `SessionStarted`.
        They are neither run writes nor explicit bench actions
        (`docs/service-contract.md`, section 9)."""
        self.run_config = run_config if run_config is not None else RunConfig()
        """The active recipe supplies expected trigger-chain values.

        The session replaces it when a run uses another `RunConfig`."""
        self.unavailable: dict[str, str] = dict(unavailable or {})
        """The unavailable-role map records the access failure reason per instrument."""
        self.rig_path = rig_path
        self.loaded_at = time.time()
        self.sim = sim
        """The simulated bench state lets tests inspect shared instrument state.

        It is `None` on a real rig."""
        self.fast = fast
        self.fingerprint = fingerprint(_package_root())
        self._watchdog_at = 0.0
        self._relay_line = relay_line
        self._closers = list(closers)

    # -- construction -----------------------------------------------------
    @classmethod
    def build_simulated(cls, rig_config: RigConfig, *, seed: int = 0,
                        fast: bool = True, run_config: RunConfig | None = None,
                        rig_path: str | None = None) -> "Bench":
        """The simulated rig shares one bench state across its instruments.

        `RigConfig` supplies `current_sign`, R_s and amplifier gain, so simulated
        and real files under the same `rig.toml` use the same conventions. A
        nonempty temperature console setting, such as `"sim"`, attaches the simulated
        331. The address setting does not attach it. Under `--sim`, a cryostat is a
        chosen scenario, not an instrument to discover. An empty console selects the
        operator-pause path by default so the console must handle that path
        (`docs/service-contract.md`, section 7)."""
        sim = make_bench(seed=seed, current_sign=rig_config.current_sign)
        sim.bench.sense_resistor_ohm = rig_config.sense_resistor_ohm
        sim.bench.pulse_amp = rig_config.pulse_amp
        sim.bench.field_latency_s = rig_config.trigger_offset_s
        rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
                  config=rig_config, router=sim.router, smu=sim.smu,
                  power=sim.power, led=sim.led,
                  temperature=sim.temperature if rig_config.temperature_console else None)
        return cls(rig=rig, rig_config=rig_config, mode="sim",
                   run_config=run_config, sim=sim, fast=fast, rig_path=rig_path)

    @classmethod
    def build_real(cls, rig_config: RigConfig, smu_config: SourceMeterConfig, *,
                   run_config: RunConfig, rig_path: str | None = None) -> "Bench":
        """The real bench builder opens VISA instruments, DIO lines and configured
        consoles.

        Imports remain local so the module needs no VISA backend to load. Each
        instrument opens independently. Failure records its address and error in
        `unavailable`. Optional roles use `None`; required roles use `Unavailable`.
        Missing `pyvisa`, NI-VISA or pyvisa-py leaves all four VISA roles
        unavailable while still trying DIO and consoles. The service must display
        missing roles instead of ending startup with a traceback.

        The scope's `default_setup` runs here, as in `tools/scan.py`, because it is
        session setup. `startup_writes` records it and the meter units and
        wavelength. Each generator and the source-meter is read once to refresh the
        cached output flags used by the relay interlock before the first job
        (`docs/service-contract.md`, section 9)."""
        from ..bench.checks import _dio_backend
        from ..drivers.agilent33220a import Agilent33220A
        from ..drivers.agilent81150 import Agilent81150
        from ..drivers.infiniium import Infiniium
        from ..drivers.keithley2400 import Keithley2400
        from ..drivers.newport1918c import open_power_meter
        from ..drivers.routing import BiasRouter, RoutingError

        unavailable: dict[str, str] = {}
        closers: list[Callable[[], None]] = []
        writes: list[str] = []
        rm = None
        no_visa: str | None = None
        try:
            import pyvisa
        except ImportError as exc:
            no_visa = f"no VISA: pyvisa is not installed ({exc}); pip install -e .[rig]"
        else:
            try:
                rm = pyvisa.ResourceManager()
            except Exception as exc:                        # noqa: BLE001
                no_visa = (f"no VISA: {type(exc).__name__}: {exc} (install NI-VISA, or "
                           "pyvisa-py from the rig extra)")
        rm_close = getattr(rm, "close", None)
        if callable(rm_close):
            closers.append(rm_close)

        def open_role(role: str, address: str, make: Callable[[Any], Any]):
            if rm is None:
                unavailable[role] = f"{address}: {no_visa}"
                return None
            try:
                res = rm.open_resource(address)
            except Exception as exc:                        # noqa: BLE001
                unavailable[role] = f"{address}: {type(exc).__name__}: {exc}"
                return None
            try:
                res.timeout = 20000
                dev = make(res)
            except Exception as exc:                        # noqa: BLE001
                unavailable[role] = f"{address}: {type(exc).__name__}: {exc}"
                _try(res.close)
                return None
            closers.append(res.close)
            return dev

        scope = open_role("scope", rig_config.scope_address, lambda r: Infiniium(
            r, sense_resistor_ohm=rig_config.sense_resistor_ohm,
            current_sign=rig_config.current_sign,
            probe_attenuation=rig_config.probe_attenuation))
        bias = open_role("bias", rig_config.bias_address, Agilent81150)
        led = open_role("led", rig_config.led_address, Agilent33220A)
        smu = open_role("smu", rig_config.sourcemeter_address,
                        lambda r: Keithley2400(r, config=smu_config))
        # Seed the cached flags from the instruments (a read, not a write):
        # the interlock behind the first relay move reads them.
        for dev in (bias, led):
            _read_state(dev)
        _read_output(smu)

        # Both DIO lines share `bench.checks._dio_backend` with the bench
        # checks, using direct DELIB or the 32-bit helper. The service must
        # use the same access path as the harness.
        shutter = None
        relay_line: _RelayLine | None = None
        make_line, how, why = _dio_backend(rig_config)
        if make_line is None:
            unavailable["shutter"] = why
            unavailable["relay"] = why
        else:
            try:
                shutter = make_line(rig_config.shutter_module_nr).open()
                closers.append(shutter.close)
            except Exception as exc:                        # noqa: BLE001
                unavailable["shutter"] = f"{how}: {type(exc).__name__}: {exc}"
            try:
                line = make_line(rig_config.relay_module_nr).open()
                # Release the relay line without calling `close()`, which drives
                # it low and throws to the amplifier without an interlock.
                # `Rig.park()` runs first but suppresses errors turning sources
                # off. A live source may remain, so shutdown must leave the relay
                # at its last interlocked position.
                closers.append(line.release)
                relay_line = _RelayLine(line)
            except Exception as exc:                        # noqa: BLE001
                unavailable["relay"] = f"{how}: {type(exc).__name__}: {exc}"
        router = None
        if relay_line is not None:
            try:
                router = BiasRouter(relay_line, sourcemeter=smu, pulse_path=bias,
                                    module_nr=rig_config.relay_module_nr)
            except RoutingError as exc:
                # `config.load_rig` also refuses module 0. A hand-built
                # `RigConfig` must leave the relay unavailable with a visible
                # reason, without preventing service startup.
                unavailable["relay"] = f"{type(exc).__name__}: {exc}"
                relay_line = None

        # Open the meter directly unless `[power_meter] console` names
        # another owner; then read over HTTP. A missing meter leaves power
        # unrecorded but does not block `bace`. `modules._settle_led` uses
        # the configured wait instead of stable meter readings.
        power = None
        try:
            power = open_power_meter(rig_config)
            how = ("the 1918-C console" if rig_config.power_meter_console
                   else "1918-C (this process owns the USB device)")
            writes.append(f"{how}: units watts")
            writes.append(f"{how}: wavelength "
                          f"{rig_config.power_meter_wavelength_nm:g} nm")
            closers.append(getattr(power, "close", lambda: None))
        except Exception as exc:                            # noqa: BLE001
            unavailable["power"] = f"{type(exc).__name__}: {exc}"

        # Open the 331 GPIB session unless `[temperature] console` names
        # its owner. An attached, answering controller settles temperature;
        # otherwise the run pauses for the operator. A bench without a
        # cryostat is valid. Its absence must not prevent startup.
        temperature = None
        try:
            # Neither an address nor a console: this bench has no cryostat.
            # Deliberately absent, so no reason is recorded -- an operator
            # who cleared both does not need a line telling them so, and a
            # run that asks for no temperature never notices.
            if not (rig_config.temperature_address or rig_config.temperature_console):
                raise _NoCryostat
            candidate = open_temperature_controller(rig_config)
            state = candidate.probe()
            if state is not None and state.get("max_setpoint_k") is not None:
                temperature = candidate
                closers.append(getattr(candidate, "close", lambda: None))
                if not rig_config.temperature_console:
                    writes.append(
                        f"331 {rig_config.temperature_address}: configuration read "
                        f"back (ceiling {rig_config.temperature_max_setpoint_k:g} K, "
                        f"loop {rig_config.temperature_control_loop})")
            elif state is not None and "connected" in state:
                # The named console is up but has no instrument reading
                # (`connected` is false). Asking the operator to start the console
                # would misidentify the fault.
                unavailable["temperature"] = (
                    f"the 331 console at {rig_config.temperature_console} is up but "
                    "has not read its instrument yet (check the 331 and its GPIB "
                    "cable, then restart the service); temperature loops pause for "
                    "a manual set")
            else:
                unavailable["temperature"] = (
                    f"the 331 console is not answering at "
                    f"{rig_config.temperature_console}; clear [temperature] console "
                    "in rig.toml to let the service open the instrument itself. "
                    "Temperature loops pause for a manual set")
        except _NoCryostat:
            pass
        except Exception as exc:                            # noqa: BLE001
            unavailable["temperature"] = (
                f"{type(exc).__name__}: {exc}; temperature loops pause for a "
                "manual set")

        if scope is not None:
            try:
                scope.default_setup()
                writes.append(f"scope {rig_config.scope_address}: default_setup")
            except Exception as exc:                        # noqa: BLE001
                unavailable["scope"] = (f"{rig_config.scope_address}: default "
                                        f"setup failed: {type(exc).__name__}: {exc}")
                scope = None

        def required(role: str, dev: Any) -> Any:
            return dev if dev is not None else Unavailable(role, unavailable[role])

        rig = Rig(bias=required("bias", bias), scope=required("scope", scope),
                  shutter=required("shutter", shutter), config=rig_config,
                  router=router, smu=smu, power=power, led=led, temperature=temperature)
        return cls(rig=rig, rig_config=rig_config, mode="rig",
                   run_config=run_config, unavailable=unavailable,
                   rig_path=rig_path, relay_line=relay_line,
                   closers=tuple(closers), startup_writes=tuple(writes))

    # -- lifecycle --------------------------------------------------------
    @property
    def sleep(self) -> Callable[[float], None]:
        """The default sleep uses `time.sleep` or takes no wall time under `--fast`."""
        return self.sleeper()

    def sleeper(self, interrupt: Callable[[], bool] | None = None, *,
                slice_s: float = SLEEP_SLICE_S) -> Callable[[float], None]:
        """The run sleep returns early when `interrupt()` is true.

        Slices of `slice_s` bound abort latency during a temperature hold, `wait`
        module or LED settle. The session supplies the abort flag rather than any
        stop flag. `after_shot` must retain the specified settle because the shot in
        progress is kept; shortening it would change that shot. Under `--fast`, the
        sleep takes no wall time (`docs/service-contract.md`, section 7)."""
        if self.fast:
            return _no_sleep

        def sleep(seconds: float) -> None:
            end = time.monotonic() + float(seconds)
            while interrupt is None or not interrupt():
                self._feed_watchdog()
                left = end - time.monotonic()
                if left <= 0:
                    return
                time.sleep(min(slice_s, left))

        return sleep

    def _feed_watchdog(self) -> None:
        """The watchdog tick polls at most once per `WATCHDOG_POLL_S` for a direct 331.

        Only the worker's sleeps call it during a run, while the worker owns the bus.
        A bench-wide clock limits polling across successive short settles. It never
        raises: `poll_faults` suppresses its errors so a watchdog query cannot fail
        a run (`docs/service-contract.md`, section 7)."""
        controller = self.rig.temperature
        poll = getattr(controller, "poll_faults", None)
        if poll is None or not getattr(controller, "watchdog", False):
            return                  # console-backed, simulated, or disabled
        now = time.monotonic()
        if now - self._watchdog_at < WATCHDOG_POLL_S:
            return
        self._watchdog_at = now
        poll()

    def close(self) -> None:
        """The bench close parks and releases resources in reverse opening order."""
        _try(self.rig.park)
        for close in reversed(self._closers):
            _try(close)
        self._closers.clear()

    # -- read-back --------------------------------------------------------
    def read_back(self, *, run_config: RunConfig | None = None, voc: Any = None,
                  monitor: bool = False) -> dict:
        """The read-back returns instruments, chain, rig values and verdicts for
        `/bench`.

        Every access is a read. `run_config` supplies expected chain values or falls
        back to the bench recipe. The session owns `voc` and supplies it for the
        snapshot. `monitor` supplies power-monitor status, which the bench does not
        track. The result also carries `unavailable` and `read_at`. `verdicts`
        contains wire dictionaries; `Bench.chain_verdicts` supplies dataclasses for
        the journal (`docs/service-contract.md`, section 4)."""
        cfg = run_config if run_config is not None else self.run_config
        read_at = time.time()
        instruments = {
            "relay": self._relay_state(),
            "bias": self._bias_state(),
            "smu": self._smu_state(),
            "shutter": self._shutter_state(),
            "led": self._led_state(),
            "voc": _voc_wire(voc),
            "power": self._power_state(monitor),
            "temperature": self._temperature_state(),
        }
        chain = self.chain(instruments, cfg, read_at=read_at)
        snapshot = {"read_at": read_at, "instruments": instruments,
                    "chain": chain, "rig": self.rig_info(),
                    "unavailable": dict(self.unavailable)}
        snapshot["verdicts"] = [to_wire(v)[0] for v in self.chain_verdicts(snapshot)]
        return snapshot

    def rig_info(self) -> dict:
        return {"path": self.rig_path, "loaded_at": self.loaded_at,
                "fingerprint": self.fingerprint,
                "values": {k: getattr(self.rig_config, k) for k in RIG_VALUE_KEYS}}

    def snapshot_stub(self) -> dict:
        """The initial instrument snapshot contains unknown values before read-back.

        `None` and `?` make the client display blanks rather than unobserved states."""
        return {
            "relay": {"position": "unknown", "how": "cached"},
            "bias": {"output": None, "polarity": "?", "arm_source": "?",
                     "arm_slope": "?", "high_v": None, "low_v": None,
                     "frequency_hz": None},
            "smu": {"output": None,
                    "compliance": {"current_a": None, "voltage_v": None},
                    "ceiling": self._ceiling()},
            "shutter": {"open": None, "how": "cached"},
            "led": {"output": None, "polarity": "?", "mode": "?", "high_v": None,
                    "low_v": None, "frequency_hz": None},
            "voc": {"value": None},
            "power": {"available": None, "watts": None, "trustworthy": None,
                      "wavelength_nm": None, "monitor": False},
            # `connected` is `None` before the attached controller is read.
            # Dry run assumes a pause without evidence that it answers
            # (`docs/service-contract.md`, section 7).
            "temperature": {"wired": self.rig.temperature is not None,
                            "kelvin": None, "setpoint_k": None, "in_band": None,
                            "source": (temperature_source(self.rig.temperature)
                                       if self.rig.temperature is not None else None),
                            "connected": None},
        }

    def shot_diagnostics(self) -> dict:
        """The shot diagnostics report the digitiser's last auto-range state.

        The live shot verdict uses it. A driver without diagnostics gives no fields."""
        scope = self.rig.scope
        if not _usable(scope):
            return {}
        out: dict[str, Any] = {}
        for key, attr in (("autorange_passes", "last_autorange_passes"),
                          ("autorange_clipped", "last_autorange_clipped")):
            if hasattr(scope, attr):
                out[key] = getattr(scope, attr)
        return out

    def _ceiling(self) -> dict:
        return {"current_a": self.rig_config.max_current_compliance_a,
                "voltage_v": self.rig_config.max_voltage_compliance_v}

    def _relay_state(self) -> dict:
        router = self.rig.router
        if router is None:
            return {"position": "unknown", "how": "unavailable"}
        if self._relay_line is not None:
            line = _try(self._relay_line.read_line)
            if line is not None:
                return {"position": _RELAY_POSITION.get(int(line), "unknown"),
                        "how": "readback"}
        position = _try(lambda: str(router.position), "unknown")
        # The simulated router reads the shared bench, which *is* the relay;
        # `BiasRouter.position` is its memory of the last move.
        return {"position": position, "how": "readback" if self.sim else "cached"}

    def _bias_state(self) -> dict:
        """The bias snapshot reports the 81150A output, polarity, levels and frequency.

        Real instruments supply `read_state`; `--sim` supplies shared bench state.
        Querying polarity lets the chain compare the recipe with the instrument
        between runs, even before a run configures its shape."""
        bias = self.rig.bias
        out = {"output": None, "polarity": "?", "arm_source": "?", "arm_slope": "?",
               "high_v": None, "low_v": None, "frequency_hz": None}
        if not _usable(bias):
            return out
        arm = _try(bias.trigger_state) or {}
        state = _read_state(bias)
        if state:
            hi, lo = _float_or_none(state.get("high_v")), _float_or_none(state.get("low_v"))
            frequency = _float_or_none(state.get("frequency_hz"))
            output = state.get("output")
            polarity = str(state.get("polarity") or "?")
        else:
            hi, lo = _levels(bias)
            frequency = _float_or_none(getattr(bias, "frequency_hz", None))
            output = _try(lambda: bool(bias.output_enabled))
            polarity = _try(lambda: str(bias.output_polarity), "?") or "?"
        out.update({"output": None if output is None else bool(output),
                    "polarity": polarity, "polarity_read": bool(state),
                    "arm_source": str(arm.get("arm_source", "?")),
                    "arm_slope": str(arm.get("arm_slope", "?")),
                    "high_v": hi, "low_v": lo, "frequency_hz": frequency})
        return out

    def _smu_state(self) -> dict:
        smu = self.rig.smu
        out = {"output": None, "compliance": {"current_a": None, "voltage_v": None},
               "ceiling": self._ceiling()}
        if not _usable(smu):
            return out
        cfg = getattr(smu, "config", None)
        if isinstance(cfg, SourceMeterConfig):
            compliance = {"current_a": cfg.current_compliance_a,
                          "voltage_v": cfg.voltage_compliance_v}
        else:
            compliance = {"current_a": _float_or_none(getattr(smu, "compliance_a", None)),
                          "voltage_v": None}
        out.update({"output": _read_output(smu), "compliance": compliance})
        return out

    def _shutter_state(self) -> dict:
        shutter = self.rig.shutter
        if not _usable(shutter):
            return {"open": None, "how": "cached"}
        read_line = getattr(shutter, "read_line", None)
        if callable(read_line):
            line = _try(read_line)
            if line is not None:
                return {"open": bool(line), "how": "readback"}
        is_open = _try(lambda: bool(shutter.is_open))
        return {"open": is_open, "how": "readback" if self.sim else "cached"}

    def _led_state(self) -> dict:
        """The LED snapshot reports the 33220A shape, levels, frequency and output.

        Real instruments supply `read_state`, including polarity, so the card can
        compare the LED state with the recipe. `--sim` supplies shared bench state."""
        led = self.rig.led
        out = {"output": None, "polarity": "?", "mode": "?", "high_v": None,
               "low_v": None, "frequency_hz": None}
        if not _usable(led):
            return out
        state = _read_state(led)
        if state:
            output = state.get("output")
            out.update({"output": None if output is None else bool(output),
                        "polarity": str(state.get("polarity") or "?"),
                        "mode": str(state.get("mode") or "?"),
                        "high_v": _float_or_none(state.get("high_v")),
                        "low_v": _float_or_none(state.get("low_v")),
                        "frequency_hz": _float_or_none(state.get("frequency_hz"))})
            if state.get("offset_v") is not None:
                out["offset_v"] = _float_or_none(state.get("offset_v"))
            return out
        hi, lo = _levels(led)
        out.update({"output": _try(lambda: bool(led.output_enabled)),
                    "polarity": _try(led.polarity, "?") or "?",
                    "mode": _try(lambda: str(led.mode), "?") or "?",
                    "high_v": hi, "low_v": lo,
                    "frequency_hz": _float_or_none(getattr(led, "frequency_hz", None))})
        return out

    def _power_state(self, monitor: bool) -> dict:
        power = self.rig.power
        out = {"available": False, "watts": None, "trustworthy": None,
               "wavelength_nm": None, "averaged": None, "meter_range": None,
               "auto_range": None, "monitor": bool(monitor)}
        if not _usable(power):
            out["reason"] = self.unavailable.get("power", "no power meter on this bench")
            return out
        try:
            ev = power_reading(power)
        except Exception as exc:                            # noqa: BLE001
            out["reason"] = f"{type(exc).__name__}: {exc}"
            return out
        out.update({"available": True, "watts": ev.watts,
                    "trustworthy": ev.trustworthy, "wavelength_nm": ev.wavelength_nm,
                    "averaged": ev.averaged, "meter_range": ev.meter_range,
                    "auto_range": ev.auto_range})
        return out

    def _temperature_state(self) -> dict:
        """The temperature snapshot reports the attached 331 controller.

        Without a controller it includes the unwired block, the configured console
        and the attachment failure reason. The card can distinguish an unavailable
        console from a bench without a controller."""
        out = read_temperature_console(self.rig.temperature)
        if self.rig_config.temperature_console:
            out["console"] = self.rig_config.temperature_console
        if self.rig.temperature is None and "temperature" in self.unavailable:
            out["reason"] = self.unavailable["temperature"]
        return out

    # -- the chain --------------------------------------------------------
    def chain(self, instruments: dict, run_config: RunConfig, *,
              read_at: float | None = None) -> dict:
        """The chain check compares four trigger settings with the active recipe.

        `led_polarity` expects INV. `bias_arm` expects EXT for external triggering.
        `bias_arm_slope` follows `trigger_slope_positive`. `bias_polarity` expects
        `RunConfig.polarity_instruction()`; `leave` shows the value as `info` without
        an expected setting.

        An unknown 81150A polarity is `info` only for a driver that cannot query it.
        The simulator learns it when the run configures a shape, unless instructed
        to leave it. Real read-back asks `:OUTP1:POL?` each time and sets
        `polarity_read`; its `?` indicates a failed query and gives `warn`
        (`docs/service-contract.md`, section 4)."""
        led, bias = instruments["led"], instruments["bias"]
        items = [
            _item("led_polarity", "33220A POL", led["polarity"], LED_EXPECTED_POLARITY,
                  fix="set-33220a-pol-inv",
                  ok="INV: the Sync's rising edge means light off, the edge extraction follows",
                  bad="NORM makes the Sync's rising edge mean light ON, so extraction "
                      "would happen during illumination"),
            _item("bias_arm", "81150A ARM", bias["arm_source"],
                  "EXT" if run_config.external_trigger else "IMM",
                  fix="arm-81150a-ext" if run_config.external_trigger else None,
                  ok="armed from the 33220A Sync" if run_config.external_trigger
                     else "free-running, as the recipe asks (external_trigger = false)",
                  bad="IMM free-runs: the collection pulse sits at a random phase of "
                      "the LED cycle and still triggers the scope"),
            _item("bias_arm_slope", "SLOP", bias["arm_slope"],
                  "POS" if run_config.trigger_slope_positive else "NEG",
                  fix="arm-81150a-ext" if run_config.external_trigger else None,
                  ok="arms on the rising Sync edge, which with the 33220A at INV is light off",
                  bad="NEG arms on the falling Sync edge, which with the 33220A at INV "
                      "is light on: extraction in the middle of generation"),
        ]
        instruction = run_config.polarity_instruction()
        value = bias["polarity"]
        if instruction is None:
            items.append({"key": "bias_polarity", "label": "81150A POL", "value": value,
                          "expected": "leave", "level": "info", "fix": None,
                          "text": "the recipe leaves :OUTP1:POL as found; this is what "
                                  "the instrument holds"})
        elif value == "?" and not bias.get("polarity_read"):
            items.append({"key": "bias_polarity", "label": "81150A POL", "value": value,
                          "expected": "INV" if instruction else "NORM", "level": "info",
                          "fix": None,
                          "text": "not read yet: the 81150A reports its polarity once a "
                                  "run configures the pulse shape, and the run writes "
                                  f"{'INV' if instruction else 'NORM'} then"})
        else:
            items.append(_item(
                "bias_polarity", "81150A POL", value, "INV" if instruction else "NORM",
                fix=None,
                ok="what the recipe's output_polarity asks for",
                bad="the device would rest at the other level between pulses, and "
                    "extraction would happen on the other edge of the pulse -- 5 us "
                    "away, outside the record -- while still integrating to a "
                    "plausible charge"))
        return {"read_at": read_at if read_at is not None else time.time(),
                "ok": sum(1 for i in items if i["level"] == "ok"),
                "total": len(items), "items": items}

    def chain_verdicts(self, snapshot: dict) -> list[E.Verdict]:
        """The chain verdicts translate read-back problems for `/bench` and the journal.

        Each non-ok chain item gives one `chain.*` verdict; a silent meter gives
        `power.console`. Read-back alone never gives `crit`. The validator decides
        whether a live output blocks the requested run
        (`docs/service-contract.md`, section 4)."""
        codes = {"led_polarity": "chain.led-polarity", "bias_arm": "chain.bias-arm",
                 "bias_arm_slope": "chain.bias-arm", "bias_polarity": "chain.bias-polarity"}
        out: list[E.Verdict] = []
        for item in snapshot["chain"]["items"]:
            if item["level"] == "ok":
                continue
            out.append(E.Verdict(
                level=item["level"], code=codes[item["key"]],
                text=f"{item['label']} reads {item['value']}: {item['text']}",
                data={"value": item["value"], "expected": item["expected"],
                      "fix": item["fix"]}))
        power = snapshot["instruments"]["power"]
        if not power.get("available"):
            out.append(E.Verdict(
                level="warn", code="power.console",
                text="the 1918-C console is not answering, so intensity will be NaN",
                data={"reason": power.get("reason"),
                      "console": self.rig_config.power_meter_console}))
        return out

    # -- actions ------------------------------------------------------------
    def action(self, name: str, args: dict | None = None, *,
               led_params: dict | None = None) -> dict:
        """The bench action performs one request and returns its result.

        `args` is the body. `led_params` supplies the active `bace` values for
        `led_v`, `led_low_v`, `pulse_frequency_hz` and `duty_percent`, unless the
        `set-led-pulse` or `set-led-dc` body overrides them. Unknown names raise
        `KeyError` (404), invalid bodies raise `ValueError` (422), and state-based
        refusals raise `BenchActionRefused`. The session reads back after each action,
        so the journal records the resulting instrument state
        (`docs/service-contract.md`, section 4)."""
        if name not in ACTIONS:
            raise KeyError(name)
        args = dict(args or {})
        handler = getattr(self, "_act_" + name.replace("-", "_"))
        return handler(args, dict(led_params or {}))

    def _need(self, role: str) -> Any:
        dev = getattr(self.rig, role)
        if not _usable(dev):
            reason = self.unavailable.get(role)
            raise BenchActionRefused(
                "warn", f"no {role} on this bench" + (f": {reason}" if reason else ""))
        return dev

    @staticmethod
    def _refuse_if_live(dev: Any, what: str, why: str) -> None:
        """The output check requires an instrument reading before a configuration
        change.

        `_read_output` detects a front-panel output that the driver's cached flag
        can miss. An unreadable output is refused because safety cannot be
        established (`docs/service-contract.md`, section 4)."""
        live = _read_output(dev)
        if live:
            raise BenchActionRefused("warn", f"{what} output is ON; {why}")
        if live is None:
            raise BenchActionRefused(
                "warn", f"{what} did not answer its output query, so it cannot be "
                        f"proven off; {why}")

    @staticmethod
    def _only(args: dict, *allowed: str) -> None:
        unknown = sorted(set(args) - set(allowed))
        if unknown:
            raise ValueError(f"unknown argument(s): {', '.join(unknown)}"
                             + (f"; accepted: {', '.join(allowed)}" if allowed else ""))

    def _act_park(self, args: dict, led: dict) -> dict:
        self._only(args)
        self.rig.park()
        return {"parked": True}

    def _set_led_polarity(self, inverted: bool) -> dict:
        led = self._need("led")
        self._refuse_if_live(
            led, "the 33220A",
            "the chain fix is made with the LED off -- led-off first, set the "
            "polarity, then set-led-pulse")
        led.set_polarity(inverted)
        return {"polarity": led.polarity()}

    def _act_set_33220a_pol_inv(self, args: dict, led: dict) -> dict:
        self._only(args)
        return self._set_led_polarity(True)

    def _act_set_33220a_pol_norm(self, args: dict, led: dict) -> dict:
        self._only(args)
        return self._set_led_polarity(False)

    def _act_arm_81150a_ext(self, args: dict, led: dict) -> dict:
        self._only(args)
        bias = self._need("bias")
        self._refuse_if_live(bias, "the 81150A",
                             "the arming is changed with the output off -- bias-off first")
        bias.configure_trigger(external=True, positive_slope=True)
        return dict(bias.trigger_state())

    def _act_set_led_pulse(self, args: dict, params: dict) -> dict:
        self._only(args, "level", "low", "frequency_hz", "duty_percent")
        led = self._need("led")
        level = args.get("level", params.get("led_v"))
        low = args.get("low", params.get("led_low_v"))
        frequency = args.get("frequency_hz", params.get("pulse_frequency_hz"))
        duty = args.get("duty_percent", params.get("duty_percent"))
        if level is None or low is None or frequency is None or duty is None:
            raise ValueError("set-led-pulse needs level, low, frequency_hz and "
                             "duty_percent: from the bace parameters in force, or "
                             "in the body")
        try:
            out = apply_led(led, "pulse", level=level, low=low,
                            frequency_hz=frequency, duty_percent=duty,
                            threshold_v=self.rig_config.led_threshold_v)
        except IlluminationError as exc:
            raise BenchActionRefused("warn", str(exc)) from None
        return {"mode": out["mode"], "high_v": out["high_v"], "low_v": out["low_v"],
                "frequency_hz": out["frequency_hz"], "duty_percent": out["duty_percent"],
                "output": True}

    def _act_set_led_dc(self, args: dict, params: dict) -> dict:
        self._only(args, "level")
        led = self._need("led")
        level = args.get("level", params.get("led_v"))
        if level is None:
            raise ValueError("set-led-dc needs a level: from the bace parameters in "
                             "force, or in the body")
        return apply_led(led, "dc", level=level)

    def _act_led_off(self, args: dict, led: dict) -> dict:
        self._only(args)
        self._need("led").off()
        return {"output": False}

    def _act_bias_off(self, args: dict, led: dict) -> dict:
        self._only(args)
        self._need("bias").disable_output()
        return {"output": False}

    def _act_smu_off(self, args: dict, led: dict) -> dict:
        self._only(args)
        self._need("smu").disable_output()
        return {"output": False}

    def _act_shutter_open(self, args: dict, led: dict) -> dict:
        self._only(args)
        return apply_shutter(self._need("shutter"), True)

    def _act_shutter_shut(self, args: dict, led: dict) -> dict:
        self._only(args)
        return apply_shutter(self._need("shutter"), False)

    def _move_relay(self, enter: str) -> dict:
        """The relay move enters and immediately exits the router's interlocked context.

        Both source outputs are read first. The real drivers refresh the cached
        `output_enabled` flags used by the router's interlock through `read_output`.
        A live source can damage hardware during a relay move, so refusal is `crit`
        and names the source. An unreadable source is also refused: lack of evidence
        is not an off state (`docs/service-contract.md`, section 4)."""
        router = self._need("router")
        for role, label in (("smu", "the Keithley 2400"), ("bias", "the 81150A")):
            dev = getattr(self.rig, role, None)
            if _usable(dev) and _read_output(dev) is None:
                raise BenchActionRefused(
                    "crit", f"nothing moved: {label} did not answer its output query, "
                            "so the interlock cannot prove it is off")
        try:
            with getattr(router, enter)():
                pass
        except Exception as exc:                            # noqa: BLE001
            raise BenchActionRefused("crit", f"nothing moved: {exc}") from None
        return {"position": router.position}

    def _act_relay_to_sourcemeter(self, args: dict, led: dict) -> dict:
        self._only(args)
        return self._move_relay("dc")

    def _act_relay_to_amplifier(self, args: dict, led: dict) -> dict:
        self._only(args)
        return self._move_relay("transient")

    def _act_read_power(self, args: dict, led: dict) -> dict:
        self._only(args, "samples")
        power = self._need("power")
        try:
            ev = power_reading(power, samples=int(args.get("samples", 1)))
        except Exception as exc:                            # noqa: BLE001
            raise BenchActionRefused(
                "warn", f"the 1918-C console is silent: {exc}") from None
        return {"watts": ev.watts, "trustworthy": ev.trustworthy,
                "wavelength_nm": ev.wavelength_nm, "source": ev.source,
                "averaged": ev.averaged, "meter_range": ev.meter_range,
                "auto_range": ev.auto_range}


# -- helpers ------------------------------------------------------------
def _levels(dev: Any) -> tuple[float | None, float | None]:
    """The cached levels return `(high, low)` or two `None` values.

    Simulated drivers retain levels. Real drivers expose only protocol fields;
    read-back must not add instrument queries to obtain more."""
    lv = _try(lambda: getattr(dev, "last_levels", None))
    if not lv:
        return None, None
    try:
        hi, lo = lv
    except (TypeError, ValueError):
        return None, None
    return _float_or_none(hi), _float_or_none(lo)


def _item(key: str, label: str, value: str, expected: str, *, fix: str | None,
          ok: str, bad: str) -> dict:
    value = "?" if value is None else str(value)
    if value == "?":
        level, text = "warn", "the instrument did not answer the query"
    elif value == expected:
        level, text = "ok", ok
    else:
        level, text = "warn", bad
    return {"key": key, "label": label, "value": value, "expected": expected,
            "level": level, "fix": fix, "text": text}


def _voc_wire(voc: Any) -> dict:
    if voc is None:
        return {"value": None}
    return {"value": _float_or_none(getattr(voc, "value", None)),
            "led_v": _float_or_none(getattr(voc, "led_v", None)),
            "from": {"run_id": getattr(voc, "run_id", None),
                     "node_path": getattr(voc, "node_path", None),
                     "ts": _float_or_none(getattr(voc, "ts", None)),
                     "how": getattr(voc, "how", None)}}
