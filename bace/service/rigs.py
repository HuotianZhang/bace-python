"""The bench as the service owns it: one Rig, its read-back, and the actions.

`Bench` is what the session builds once per process and every route reaches
through the worker. It exists so three things live in one place instead of
being re-derived by each route:

* **Assembly.** The real rig is put together exactly the way `tools/scan.py`
  and `bench.checks.stage_measure` do it -- same `Infiniium` with the same
  sense resistor, sign and probe attenuation, same `Keithley2400` with the
  recipe's compliance, the shutter and relay through `bench.checks._dio_backend`
  -- so a run started from the console runs on the same objects as one started
  from the command line. A missing instrument is recorded in
  `unavailable[role]`, never raised: the service must come up on a bench with
  the Keithley unplugged so the operator can *see* which instrument is
  missing, rather than get a traceback that names none of them.
* **Read-back.** `/bench` is rendered from what the instruments report, never
  from what the service last sent. The trigger chain the whole measurement
  hangs on -- the 33220A's output polarity, the 81150A's arming -- was
  inherited state until 2026-09-02, and a run that inherits the wrong one
  still writes a full set of plausible files. Reading the chain back and
  comparing it with what the recipe expects is the only way the console can
  say so. `read_back()` writes nothing: a read that changed an output would
  make the snapshot a lie about the state it was taken in.
* **Actions.** Every deliberate change to the bench outside a run goes
  through `action()`, which refuses the ones that are unsafe *now* -- a
  polarity change with the LED on, a relay move with a source live -- and
  returns what it did, so the journal can show it as a by-hand entry. The
  service never performs any of them on its own.

Nothing here decides anything about a measurement. `pyvisa` and the real
drivers are imported inside `build_real` only, so `--sim` works on a machine
with no VISA backend and the tests never touch one.
"""
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
"""What the chain expects of the 33220A, always (03-states section B). With
`:OUTP:POL INV` the waveform is inverted and the Sync is not, so the Sync's
rising edge -- the edge the 81150A arms on -- means light *off*, which is the
edge extraction has to follow. NORM makes it mean light on, and the
collection pulse then lands in the middle of carrier generation while still
producing a transient that integrates to a plausible charge."""

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
"""The bench actions, in the contract's order. `action()` refuses any other
name with `KeyError`, which the route turns into a 404."""

RIG_VALUE_KEYS: tuple[str, ...] = (
    "sense_resistor_ohm", "pulse_amp", "current_sign", "trigger_offset_s",
    "light_path_delay_ns", "probe_attenuation", "led_threshold_v",
    "max_current_compliance_a", "max_voltage_compliance_v",
)
"""The `rig.toml` numbers the bench card shows: the ones that rescale or
re-sign every charge, the two timing constants of the chain, and the two
ceilings. Addresses are not shown; they cannot be wrong without the
instrument being missing, which is reported on its own."""

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
"""Which read-back fields each action changes, so the journal's `BenchAction`
can carry the value *before* it as well as the result: the session log's
"33220A :OUTP:POL NORM -> INV · by hand" needs both halves, and a client that
joins later has no earlier snapshot to take the first from."""

INSTRUMENT_KEYS: tuple[str, ...] = (
    "relay", "bias", "smu", "shutter", "led", "voc", "power", "temperature")

_RELAY_POSITION = {0: "amplifier", 1: "sourcemeter"}

SLEEP_SLICE_S = 0.2
"""How often an interruptible sleep looks at the abort flag. A settle or a
hold is slept in slices of this, so an abort acts within it rather than
after the whole of a 60 s hold or an 1800 s wait."""

_METER_LOCK = threading.Lock()
"""Serialises `power_reading` across threads. The monitor reads the meter
on its own thread beside a run that reads it on the worker, and the
driver keeps its last reading on the instance (`.last`): without the lock
a worker's reading could carry the monitor's trustworthiness flag."""


class BenchActionRefused(RuntimeError):
    """An action the bench will not perform *now*.

    `level` is the verdict tier the refusal is reported at -- `crit` for the
    relay interlock, because moving the relay under a live source is the one
    software mistake that costs hardware; `warn` for everything else -- and
    `text` says what is in the way. This is not an error in the request: the
    same request is fine once the output it names is off, so the route
    answers with the refusal and the operator decides.
    """

    def __init__(self, level: str, text: str):
        super().__init__(text)
        self.level = level
        self.text = text


class _NoCryostat(Exception):
    """`[temperature]` names neither an address nor a console: there is no 331
    on this bench, which is a configuration and not a failure. Internal to
    `build_real`, and never reported as an unavailable role."""


class BenchUnavailable(RuntimeError):
    """Raised on any use of a required instrument that could not be opened."""


class Unavailable:
    """Stand-in for a required role whose instrument could not be opened.

    `Rig` requires `bias`, `scope` and `shutter`, so a bench with the scope
    unplugged still needs something in the slot. `None` would fail deep inside
    a run with an `AttributeError` that names nothing; this fails on the first
    use with the reason the instrument is missing, and `Rig.park()`'s
    try/except swallows it exactly as it would a dead instrument.

    `isinstance(x, Unavailable)` is how the read-back tells it apart.
    `getattr(x, name, default)` does not: the error is deliberately not an
    `AttributeError`, because a default silently taken for a missing scope is
    the failure the stand-in exists to prevent.
    """

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
    """The relay's DIO line, as `BiasRouter` wants it: `set(channel, value)`.

    The line is opened with the shutter driver (it is the same module type on
    a different module number), but this adapter deliberately does not expose
    `unblock`/`shut`: the relay is not a shutter, and a caller that reached
    for those spellings would be moving the device node around the interlock.
    `BiasRouter` owns the line and moves it only after both sources report
    their outputs off. `read_line()` is passed through so the read-back can
    report the relay's *actual* position from the module rather than the
    router's memory of what it last set.
    """

    def __init__(self, line):
        self._line = line

    def set(self, channel: int, value: int) -> None:
        self._line.set_line(value)

    def read_line(self) -> int | None:
        return self._line.read_line()


def _usable(dev: Any) -> bool:
    return dev is not None and not isinstance(dev, Unavailable)


def _try(fn: Callable[[], Any], default: Any = None) -> Any:
    """A readback that raises becomes its default. The snapshot has to be
    complete on a bench where one instrument is dead, and `?`/None for that
    one is more honest than no snapshot at all."""
    try:
        return fn()
    except Exception:
        return default


def _no_sleep(seconds: float) -> None:
    return None


def _read_output(dev: Any) -> bool | None:
    """The output state *from the instrument* when the driver can ask
    (`read_output`, the real drivers), else the cached flag (the simulator,
    whose flag is the bench). None when neither answers.

    Read, do not remember: a real driver's cached flag starts False in its
    constructor whatever the front panel says, and the relay interlock that
    reads it would move the relay under a generator the LabVIEW VI left ON.
    A driver's `read_output` refreshes its own cached flag, so the router,
    which reads the flag, sees the answer too.
    """
    if not _usable(dev):
        return None
    read = getattr(dev, "read_output", None)
    if callable(read):
        # The instrument's answer, or None: a driver that can ask and got
        # no reply must not fall back to its memory, because "did not
        # answer" and "off" are different facts to an interlock.
        on = _try(read)
        return None if on is None else bool(on)
    return _try(lambda: bool(dev.output_enabled))


def _read_state(dev: Any) -> dict:
    """`read_state()` from a driver that has one (the real generators),
    else `{}`; never raises."""
    read = getattr(dev, "read_state", None)
    if not callable(read):
        return {}
    state = _try(read)
    return dict(state) if isinstance(state, dict) else {}


def action_before(name: str, snapshot: dict | None) -> dict:
    """The read-back values an action is about to change, as
    `{"<instrument>.<field>": value}`, from the cached snapshot. Empty when
    nothing was read yet or the action changes nothing the card shows."""
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
    """One `PowerReading` from any `PowerMeter`, real or simulated.

    Shared by the read-back, the `read-power` action, the `power` module and
    the monitor, so the four cannot disagree about what "trustworthy" means:
    it is the meter's own verdict when the driver keeps one (`.last` on
    `ConsolePowerMeter`), and True for a driver that has no notion of it.
    Raises whatever the meter raises; the callers decide whether a silent
    console is a refusal, a warning or a NaN.
    """
    with _METER_LOCK:
        if samples > 1:
            watts, _std = meter.read_statistics(int(samples))
        else:
            watts = meter.read_power()
        last = getattr(meter, "last", None)
    trustworthy = True if last is None else bool(getattr(last, "trustworthy", True))
    wavelength = None
    if last is not None:
        wavelength = _float_or_none(getattr(last, "wavelength_nm", None))
    if wavelength is None:
        wavelength = _float_or_none(getattr(meter, "wavelength_nm", None))
    # `source` before `base_url`: the direct driver has no URL, and falling
    # through to "simulated" would render a real watt reading as a made-up
    # one -- the exact confusion this field exists to prevent.
    source = (getattr(meter, "source", None) or getattr(meter, "base_url", None)
              or "simulated")
    return E.PowerReading(watts=float(watts), trustworthy=trustworthy,
                          wavelength_nm=wavelength, source=str(source))


def temperature_source(controller: Any) -> str:
    """Where a temperature came from: `instrument` when this process holds the
    331's GPIB session (the default), `console` when the 331 console holds it
    and we asked over HTTP, `simulated` for the stand-in.

    The first two are measured and the third is not, and a UI must never let
    a simulated number read as a measured one (`docs/ui-kickoff.md`), so the
    fall-through is deliberately last: a driver that is neither is a
    stand-in.
    """
    if getattr(controller, "base_url", None):
        return "console"
    if getattr(controller, "resource", None):
        return "instrument"
    return "simulated"


def read_temperature_console(source: Any, *, timeout_s: float = 2.0) -> dict:
    """The `temperature` block of the snapshot, through the 331 driver.

    `source` is the rig's `TemperatureController` (the read-back), a console
    URL (the monitor, which reads the console the rig names whether or not
    it answered at start-up), or empty. `wired` means *a controller is
    attached* -- the 331 console named in `rig.toml` and answering, or the
    simulator's stand-in -- because that is what decides whether a
    temperature node settles on its own or pauses for the operator; the
    bench adds the reason when a console is named but not attached, so the
    difference between "not integrated" and "go and start it" is on the
    card. `connected` is the console's own word on the instrument behind
    it: a console up with the 331 silent on the bus is wired and not
    connected. `in_band` stays None here because the tolerance belongs to
    the loop that is waiting, not to the bench.
    """
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
    """One Rig and everything the service knows about it that is not a run.

    Build with `build_simulated` or `build_real`; never construct a Rig for
    the service anywhere else, because the assembly is where the sense
    resistor, the sign and the probe attenuation are handed to the digitiser
    and two assemblies would be two places for them to disagree.
    """

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
        """Every instrument write the assembly made before the worker started
        (the scope's default setup, the meter's units and wavelength). Not a
        run and not a by-hand action, so they land in the journal's
        `SessionStarted` header rather than nowhere."""
        self.run_config = run_config if run_config is not None else RunConfig()
        """The recipe in force, for the chain's expected values. The session
        replaces it when a run starts with a different `RunConfig`."""
        self.unavailable: dict[str, str] = dict(unavailable or {})
        """`role -> reason` for every instrument that could not be opened."""
        self.rig_path = rig_path
        self.loaded_at = time.time()
        self.sim = sim
        """The `SimulatedRig` behind a `--sim` bench, so tests can read the
        shared bench state. None on the real rig."""
        self.fast = fast
        self.fingerprint = fingerprint(_package_root())
        self._relay_line = relay_line
        self._closers = list(closers)

    # -- construction -----------------------------------------------------
    @classmethod
    def build_simulated(cls, rig_config: RigConfig, *, seed: int = 0,
                        fast: bool = True, run_config: RunConfig | None = None,
                        rig_path: str | None = None) -> "Bench":
        """`--sim`: the simulated instruments on one shared bench.

        The simulator takes the rig's `current_sign`, sense resistor and
        amplifier gain from the `RigConfig` in force, so a simulated file and
        a real one written under the same `rig.toml` carry the same
        conventions. The simulated 331 is attached only when the config names
        a console (any value -- `"sim"` will do), and deliberately *not* from
        `[temperature] address`, which on the real bench now decides whether
        this process opens the instrument. There is no instrument to answer
        under `--sim`, so whether this bench has a cryostat is a scenario to
        choose rather than a fact to discover: empty is the operator-pause
        path, which is the one a console has to handle well and the one worth
        getting by default.
        """
        sim = make_bench(seed=seed, current_sign=rig_config.current_sign)
        sim.bench.sense_resistor_ohm = rig_config.sense_resistor_ohm
        sim.bench.pulse_amp = rig_config.pulse_amp
        rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
                  config=rig_config, router=sim.router, smu=sim.smu,
                  power=sim.power, led=sim.led,
                  temperature=sim.temperature if rig_config.temperature_console else None)
        return cls(rig=rig, rig_config=rig_config, mode="sim",
                   run_config=run_config, sim=sim, fast=fast, rig_path=rig_path)

    @classmethod
    def build_real(cls, rig_config: RigConfig, smu_config: SourceMeterConfig, *,
                   run_config: RunConfig, rig_path: str | None = None) -> "Bench":
        """The lab PC: pyvisa, the real drivers, the DIO lines, the console.

        Imports are inside the function so the module imports without a VISA
        backend. Each instrument is opened on its own; one that fails lands
        in `unavailable` with the address and the error, and the Rig gets
        None for an optional role or an `Unavailable` stand-in for a required
        one. A machine with no VISA at all -- `pyvisa` not installed, or
        installed with no NI-VISA and no pyvisa-py behind it -- is the same
        case four times over: the four VISA roles are unavailable with the
        reason, and the DIO lines and the consoles are still tried, because
        the operator has to *see* what is missing and a traceback names
        nothing. The scope's `default_setup()` runs here, as `tools/scan.py`
        does it, because it is session-level setup and not part of any run;
        it and the meter's units/wavelength are recorded in `startup_writes`.
        Each generator and the SourceMeter are then read back once, so the
        cached flags the relay interlock reads hold what the front panel
        holds before the first run, not what a fresh constructor assumed.
        """
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

        # The DIO lines. Both go through the backend the bench check found --
        # direct DELIB or the 32-bit helper -- so the service cannot end up on
        # a different route from the harness that proved the route works.
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
                # Released, not closed: the driver's close() drives the
                # line low, which here is a relay throw to the amplifier
                # with no interlock in the way. `Rig.park()` runs first at
                # shutdown but swallows a source that did not take
                # `:OUTP OFF`, so the throw would land under whatever is
                # still live. The relay stays where the last interlocked
                # move put it.
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
                # `config.load_rig` refuses this too; a RigConfig built by
                # hand can still say module 0, and the answer is the same:
                # no router, the reason on the card, the service up.
                unavailable["relay"] = f"{type(exc).__name__}: {exc}"
                relay_line = None

        # The power meter. This process opens the USB device unless
        # `[power_meter] console` names the meter's own console, in which case
        # that program holds the handle and we ask it instead. No meter = no
        # intensity, not a refusal: a bace still runs, it just waits on the
        # clock instead of on a flat reading (`modules._settle_led`).
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

        # The 331. This process opens its GPIB session unless `[temperature]
        # console` names the 331 console, which owns the bus while it runs.
        # Attached and answering = a temperature loop settles through it;
        # absent = the loop pauses and the operator types the number, exactly
        # as before this instrument moved in-process. A cryostat that is not
        # on the bench is the ordinary case, not a fault, so its absence is a
        # line on the card and never a refusal to start.
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
                # Named console, up, but it has not polled its instrument yet
                # (`ls331/service.py`, `{"connected": False}`). Telling the
                # operator to start it would be wrong.
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
        """What a `RunContext.sleep` should be on this bench when nothing
        can interrupt it: a no-op under `--fast`, `time.sleep` otherwise."""
        return self.sleeper()

    def sleeper(self, interrupt: Callable[[], bool] | None = None, *,
                slice_s: float = SLEEP_SLICE_S) -> Callable[[float], None]:
        """A `RunContext.sleep` that returns early once `interrupt()` is True.

        Sleeps in slices of `slice_s`, looking at the flag between them, so
        an abort during a temperature hold, a `wait` module or an LED settle
        acts within a slice instead of after the whole sleep. The session
        hands in "abort requested" and not "any stop requested": an
        `after_shot` lets the shot in flight finish *as specified*, settle
        included, because that shot is kept -- a settle cut short would
        make it a shot the operator asked for and did not get. A no-op
        under `--fast`, as before.
        """
        if self.fast:
            return _no_sleep
        if interrupt is None:
            return time.sleep

        def sleep(seconds: float) -> None:
            end = time.monotonic() + float(seconds)
            while not interrupt():
                left = end - time.monotonic()
                if left <= 0:
                    return
                time.sleep(min(slice_s, left))

        return sleep

    def close(self) -> None:
        """Park, then release every resource in reverse order of opening."""
        _try(self.rig.park)
        for close in reversed(self._closers):
            _try(close)
        self._closers.clear()

    # -- read-back --------------------------------------------------------
    def read_back(self, *, run_config: RunConfig | None = None, voc: Any = None,
                  monitor: bool = False) -> dict:
        """The `instruments`, `chain`, `rig` and `verdicts` parts of `/bench`.

        Reads every instrument and writes to none. `run_config` is the recipe
        whose expectations the chain is checked against (the bench's own when
        omitted); `voc` is the session's V_oc source, rendered here so the
        snapshot is complete but owned by the session; `monitor` says whether
        the power monitor is running, which the bench cannot know.

        Also carries `unavailable` (role -> reason) and `read_at`. The
        `verdicts` are wire dicts; `chain_verdicts()` gives the dataclasses
        for journaling.
        """
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
        """The instruments dict before the first read-back: every value None
        or `?`, so a client rendering it draws blanks rather than a state the
        bench was never seen in."""
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
            # `connected` None: a controller attached and not read back yet,
            # which the Dry run counts as a pause until the first read-back
            # says otherwise (contract section 7).
            "temperature": {"wired": self.rig.temperature is not None,
                            "kelvin": None, "setpoint_k": None, "in_band": None,
                            "source": (temperature_source(self.rig.temperature)
                                       if self.rig.temperature is not None else None),
                            "connected": None},
        }

    def shot_diagnostics(self) -> dict:
        """What the digitiser says about its last auto-range, for the per-shot
        verdict on the live card. Empty for a driver that has no such state."""
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
        """The 81150A from the instrument (`read_state`) on the real rig --
        output, polarity, levels, frequency -- and from the simulator's
        shared bench under `--sim`. `polarity` is queried here, so the chain
        item compares the instrument with the recipe between runs too,
        rather than reading `?` until a run has configured the shape."""
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
        """The 33220A from the instrument (`read_state`: shape, levels,
        frequency, output, polarity) on the real rig, so the card can compare
        the LED's levels with the recipe's -- the check nobody made until
        2026-09-01 -- and from the simulator's bench under `--sim`."""
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
               "wavelength_nm": None, "monitor": bool(monitor)}
        if not _usable(power):
            out["reason"] = self.unavailable.get("power", "no power meter on this bench")
            return out
        try:
            ev = power_reading(power)
        except Exception as exc:                            # noqa: BLE001
            out["reason"] = f"{type(exc).__name__}: {exc}"
            return out
        out.update({"available": True, "watts": ev.watts,
                    "trustworthy": ev.trustworthy, "wavelength_nm": ev.wavelength_nm})
        return out

    def _temperature_state(self) -> dict:
        """The 331 through the attached controller; with none attached, the
        contract's unwired block plus the console named and the reason it
        was not attached, so the card can say "start it" rather than "not
        integrated"."""
        out = read_temperature_console(self.rig.temperature)
        if self.rig_config.temperature_console:
            out["console"] = self.rig_config.temperature_console
        if self.rig.temperature is None and "temperature" in self.unavailable:
            out["reason"] = self.unavailable["temperature"]
        return out

    # -- the chain --------------------------------------------------------
    def chain(self, instruments: dict, run_config: RunConfig, *,
              read_at: float | None = None) -> dict:
        """The four trigger-chain items against what the recipe expects.

        `led_polarity` expects INV always; `bias_arm` expects EXT when the
        recipe arms externally; `bias_arm_slope` follows
        `trigger_slope_positive`; `bias_polarity` expects the recipe's
        `polarity_instruction()`, and `leave` means there is nothing to
        expect, so the value is shown at level `info`. A `?` on the 81150A's
        polarity is `info` only on a driver that cannot query it (the
        simulator learns it when a run configures the shape, and a run does
        write it unless told to leave it); the real driver asks `:OUTP1:POL?`
        at every read-back (`polarity_read`), so its `?` is a query that
        failed, which is a `warn` like any other.
        """
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
        """The bench-level `Verdict`s a read-back implies, for the journal and
        for `/bench`: one `chain.*` per chain item that is not `ok`, and
        `power.console` when the meter is silent. Never `crit`: nothing a
        read-back sees is a safety block on its own -- a live output at Start
        is the validator's call, with the run in hand."""
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
        """Perform one bench action and return what it did (`BenchAction.result`).

        `args` is the request body; `led_params` are the bace parameters in
        force (`led_v`, `led_low_v`, `pulse_frequency_hz`, `duty_percent`),
        which `set-led-pulse`/`set-led-dc` use unless the body overrides them.
        Unknown action -> `KeyError` (404); a bad body -> `ValueError` (422);
        an action the bench refuses right now -> `BenchActionRefused`. The
        read-back that follows every action is the session's job, so the
        journal entry shows the state the action left, not the state it
        claims.
        """
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
        """Asks the instrument (`_read_output`), not the driver's memory: the
        refusal exists for the generator somebody left ON before the service
        started, which a cached flag reports as off. Unreadable is refused
        too -- a change made against an output nobody can see is a guess."""
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
            drive = LedDrive(level=float(level), low_level=float(low),
                             frequency_hz=float(frequency), duty_percent=float(duty),
                             threshold_v=self.rig_config.led_threshold_v)
        except IlluminationError as exc:
            raise BenchActionRefused("warn", str(exc)) from None
        s = drive.pulse_settings()
        led.set_pulse(s["high"], s["low"], frequency_hz=s["frequency"],
                      duty_percent=s["duty"])
        led.enable_output(True)
        return {"mode": "PULSE", "high_v": s["high"], "low_v": s["low"],
                "frequency_hz": s["frequency"], "duty_percent": s["duty"],
                "output": True}

    def _act_set_led_dc(self, args: dict, params: dict) -> dict:
        self._only(args, "level")
        led = self._need("led")
        level = args.get("level", params.get("led_v"))
        if level is None:
            raise ValueError("set-led-dc needs a level: from the bace parameters in "
                             "force, or in the body")
        led.set_dc(float(level))
        led.enable_output(True)
        return {"mode": "DC", "level_v": float(level), "output": True}

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
        shutter = self._need("shutter")
        shutter.unblock()
        return {"open": bool(shutter.is_open)}

    def _act_shutter_shut(self, args: dict, led: dict) -> dict:
        self._only(args)
        shutter = self._need("shutter")
        shutter.shut()
        return {"open": bool(shutter.is_open)}

    def _move_relay(self, enter: str) -> dict:
        """Enter the router's context manager and leave it at once, so the
        move goes through the interlock and nothing else. A refusal is `crit`:
        the relay under a live source is the one mistake that costs hardware,
        and the router says which source is driving.

        Both sources are read back first. The interlock reads each driver's
        cached `output_enabled`, and on the real rig that flag is refreshed
        only by a read (`read_output`); the throw the interlock exists to
        prevent is the one under a source the driver never saw switched on.
        A source that will not answer is a refusal too: unproven is not off.
        """
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
                "wavelength_nm": ev.wavelength_nm, "source": ev.source}


# -- helpers ------------------------------------------------------------
def _levels(dev: Any) -> tuple[float | None, float | None]:
    """`(high, low)` from a driver that keeps its last levels, else Nones.
    The simulated drivers do; the real ones report only what the protocol
    asks of them, and a read-back must not query the instrument for more."""
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
