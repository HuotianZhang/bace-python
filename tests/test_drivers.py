"""SCPI command sequences, pinned.

These drivers cannot be tested against the instruments from here, so what is
testable is the thing most likely to be got wrong and least likely to be
noticed: *what is sent, and in what order*. A fake resource records every write
and query, and the tests assert on the transcript.

That is not a substitute for a bench smoke test. It is what makes the bench
smoke test short, because by the time it runs the only remaining unknown is
whether the instrument agrees.
"""
from __future__ import annotations

import json

import re

import numpy as np
import pytest

from bace.config import ConfigError, load_rig, load_run
from bace.drivers.agilent33220a import Agilent33220A, LedSourceError
from bace.drivers.keithley2400 import Keithley2400, SourceMeterConfig
from bace.drivers.protocols import PowerMeter, SourceMeter


class FakeIO:
    """Records the transcript and answers queries from a script."""

    def __init__(self, idn="KEITHLEY INSTRUMENTS INC.,MODEL 2400,4473504,C34",
                 reads=None):
        self.timeout = 0
        self.log: list[str] = []
        self.idn = idn
        self.reads = list(reads or [])

    def write(self, cmd):
        self.log.append(cmd)
        # The one piece of state a run path reads back after writing it:
        # the output switch. A fake that answered `0` to `:OUTP1?` after
        # `:OUTP1 ON` would trip the run's own guard (BiasOutputError),
        # which exists because a real generator once appeared to.
        head = cmd.strip().upper()
        if head.startswith((":OUTP1 ON", ":OUTP1 1", ":OUTP ON", ":OUTP 1", "OUTP1 ON", "OUTP 1", "OUTP ON")):
            self.output_on = True
        elif head.startswith((":OUTP1 OFF", ":OUTP1 0", ":OUTP OFF", ":OUTP 0", "OUTP1 OFF", "OUTP 0", "OUTP OFF")):
            self.output_on = False

    def query(self, cmd):
        self.log.append(cmd)
        if "*IDN?" in cmd:
            return self.idn
        if cmd.strip().upper().rstrip(";") in (":OUTP1?", ":OUTP?", "OUTP?", "OUTP1?"):
            return "1" if getattr(self, "output_on", False) else "0"
        if ":SYST:ERR?" in cmd:
            return "0,No error"
        if ":READ?" in cmd:
            return self.reads.pop(0) if self.reads else "0.0"
        return "0"

    def close(self):
        self.log.append("<closed>")

    def index(self, needle):
        for i, c in enumerate(self.log):
            if needle in c:
                return i
        raise AssertionError(f"{needle!r} was never sent. Transcript: {self.log}")


# -- Keithley 2400 --------------------------------------------------------
def test_it_is_a_sourcemeter():
    assert isinstance(Keithley2400(FakeIO()), SourceMeter)


def test_identify_rejects_the_wrong_instrument():
    with pytest.raises(Exception, match="expected a Keithley"):
        Keithley2400(FakeIO(idn="Agilent Technologies,81150A")).identify()


def test_measure_dc_takes_the_three_quantities_in_the_original_order():
    io = FakeIO(reads=["-2.00E-4", "9.06E-1", "-6.00E-4"])
    k = Keithley2400(io)
    dc = k.measure_dc(v_sat=-1.0, settle_s=0.0)

    assert dc.jsc == pytest.approx(-2e-4)
    assert dc.voc == pytest.approx(0.906)
    assert dc.jsat == pytest.approx(-6e-4)
    assert dc.v_sat == -1.0

    # J_sc sources volts, V_oc sources amps, J_sat sources volts again
    modes = [c for c in io.log if ":SOUR:FUNC:MODE" in c]
    assert modes == [":SOUR:FUNC:MODE VOLT;", ":SOUR:FUNC:MODE CURR;",
                     ":SOUR:FUNC:MODE VOLT;"]
    assert io.log.count("*RST") == 3        # each measurement starts clean


def test_compliance_is_set_before_the_output_is_enabled():
    """The recovered routine enabled the output first and set the compliance
    after. Between those two commands the SMU sits at whatever *RST left, which
    is not a limit anyone chose. This is the one place the port deliberately
    reorders the original."""
    io = FakeIO(reads=["-2.00E-4"])
    Keithley2400(io).measure_jsc(settle_ms=0)
    assert io.index(":SENS:CURR:PROT:LEV") < io.index(":OUTP ON")


def test_the_voc_measurement_limits_voltage_not_current():
    """Sourcing 0 A, a current protection does nothing. The voltage compliance
    is what bounds the output if a contact opens mid-measurement — the recovered
    routine set the wrong one."""
    io = FakeIO(reads=["9.06E-1"])
    Keithley2400(io, config=SourceMeterConfig(voltage_compliance_v=2.0)
                 ).measure_voc(settle_ms=0)
    assert any(":SENS:VOLT:PROT:LEV 2" in c for c in io.log)
    assert not any(":SENS:CURR:PROT" in c for c in io.log)


def test_the_output_is_off_again_after_every_measurement():
    """The router will not move the relay while this reports True."""
    io = FakeIO(reads=["-2.00E-4", "9.06E-1", "-6.00E-4"])
    k = Keithley2400(io)
    k.measure_dc(v_sat=-1.0, settle_s=0.0)
    assert k.output_enabled is False
    assert io.log.count(":OUTP OFF;") == 3


def test_sweep_unpacks_interleaved_voltage_and_current():
    io = FakeIO(reads=["0.0,-2.0E-4,0.5,-1.5E-4,1.0,1.0E-4"])
    v, i = Keithley2400(io).sweep(0.0, 1.0, 3)
    np.testing.assert_allclose(v, [0.0, 0.5, 1.0])
    np.testing.assert_allclose(i, [-2e-4, -1.5e-4, 1e-4])
    assert any(":SOUR:SWE:POIN 3" in c for c in io.log)


def test_a_one_point_sweep_is_refused():
    with pytest.raises(ValueError, match="at least two points"):
        Keithley2400(FakeIO()).sweep(0.0, 1.0, 1)


# -- Agilent 33220A -------------------------------------------------------
def test_the_led_generator_commands_carry_no_channel_index():
    """That absence is how the 33220A was told apart from the 81150A in the
    first place — every 81150A command in its library is indexed."""
    io = FakeIO(idn="Agilent Technologies,33220A")
    g = Agilent33220A(io)
    g.set_pulse(1.020, 0.4, frequency_hz=500.0, duty_percent=50.0)
    assert io.log == ["FUNC:SHAP PULSE;", "FUNC:PULS:HOLD DCYC;", ":FREQ 500;",
                      ":VOLT:HIGH 1.02;", ":VOLT:LOW 0.4;", "FUNC:PULS:DCYC 50;"]


def test_the_generator_holds_duty_cycle_so_a_frequency_change_is_not_a_conflict():
    """On the rig, `:FREQ 1000` answered -221 "pulse width decreased due to
    period" because the generator was holding the width. Holding the duty cycle
    instead removes a settings conflict that would otherwise be permanent — and
    a warning that is always there is a warning nobody reads."""
    io = FakeIO(idn="Agilent Technologies,33220A")
    Agilent33220A(io).set_pulse(1.020, 0.4, frequency_hz=500.0)
    assert io.index("FUNC:PULS:HOLD DCYC") < io.index(":FREQ")


def test_dc_mode_uses_the_offset():
    io = FakeIO(idn="Agilent Technologies,33220A")
    Agilent33220A(io).set_dc(1.020)
    assert io.log == ["FUNC:SHAP DC;", ":VOLT:OFFS 1.02;"]


def test_the_led_driver_refuses_the_collection_field_generator():
    """Swapping the two addresses would drive the LED with the collection-field
    waveform. Worth catching at *IDN? rather than in the data."""
    io = FakeIO(idn="Agilent Technologies,81150A,MY5111")
    with pytest.raises(LedSourceError, match="two generators are swapped"):
        Agilent33220A(io).identify()


def test_the_illumination_object_drives_the_generator():
    from bace.core.illumination import LedDrive
    io = FakeIO(idn="Agilent Technologies,33220A")
    g = Agilent33220A(io)
    drive = LedDrive(level=1.020, low_level=0.4)
    g.apply_dc(drive)
    g.apply(drive)
    assert io.log[0] == "FUNC:SHAP DC;"
    assert ":VOLT:HIGH 1.02;" in io.log


# -- Agilent 81150A -------------------------------------------------------
def _run_on_a_real_driver(io, **cfg_kw):
    """`run_transient_scan` with the real 81150A driver on a fake resource and
    the simulated scope and shutter. The driver is not mocked: HANDOFF §9 is
    about a test that mocked the driver and hid a real crash."""
    from bace.core.axis import bace_sweep
    from bace.drivers.agilent81150 import Agilent81150
    from bace.drivers.simulated import make_bench
    from bace.experiment.rig import Rig, RigConfig
    from bace.experiment.transient import RunConfig, run_transient_scan

    sim = make_bench(seed=1)
    rig = Rig(bias=Agilent81150(io), scope=sim.scope, shutter=sim.shutter,
              config=RigConfig())
    cfg = RunConfig(n_averages=4, settle_s=0.0, dark_settle_s=0.0,
                    record_length=400, **cfg_kw)
    return list(run_transient_scan(rig, bace_sweep(0.90, 0.90, 0.02, n_loops=1),
                                   cfg, sleep=lambda s: None))


def test_the_run_path_arms_the_generator_before_shaping_the_pulse():
    """Until 2026-09-02 only `tools/scan.py` sent `:ARM:SOUR1 EXT`, as a
    pre-flight side effect. `run_transient_scan` sent nothing, so a run
    started from the bench harness or the service pulsed at whatever arming
    the front panel held -- and a free-running 81150A still triggers the
    scope (which watches the 81150A's own Sync) and still integrates to a
    plausible charge, at a random phase of the LED cycle. The transcript has
    to show the arming, and show it in the recovered VI's order: trigger
    first, then the standard waveform."""
    io = FakeIO(idn="Agilent Technologies,81150A,MY5,1.0")
    _run_on_a_real_driver(io)
    assert io.index(":ARM:SOUR1 EXT") < io.index(":ARM:SLOP POS") < io.index(":FUNC1 PULS")
    # the recovered constants travel with it, and never through the protocol
    assert ":ARM:LEV 1;" in io.log and ":ARM:IMP 10000;" in io.log
    # and the run reads the arming back rather than trusting what it sent
    assert io.index(":ARM:SOUR1?") > io.index(":ARM:SOUR1 EXT")


def test_the_run_can_ask_for_internal_arming_and_says_so():
    io = FakeIO(idn="Agilent Technologies,81150A,MY5,1.0")
    _run_on_a_real_driver(io, external_trigger=False)
    assert ":ARM:SOUR1 IMM;" in io.log
    assert not any(c.startswith(":ARM:SOUR1 EXT") for c in io.log)
    # no slope *set* (a readback query is fine, and expected)
    assert not any(c.startswith(":ARM:SLOP ") for c in io.log)


def test_trigger_state_is_a_readback_not_an_echo():
    """`?` when the instrument will not answer, the token when it does, and a
    source that is neither EXT nor IMM passed through as spelt: MAN is
    information, and `?` would hide it."""
    from bace.drivers.agilent81150 import Agilent81150, TriggerConfig

    class Answers(FakeIO):
        def __init__(self, source, slope):
            super().__init__(idn="Agilent Technologies,81150A")
            self.source, self.slope = source, slope

        def query(self, cmd):
            self.log.append(cmd)
            if ":ARM:SOUR1?" in cmd:
                if isinstance(self.source, Exception):
                    raise self.source
                return self.source
            if ":ARM:SLOP?" in cmd:
                return self.slope
            return super().query(cmd)

    assert Agilent81150(Answers("EXT\n", "POS")).trigger_state() == \
        {"arm_source": "EXT", "arm_slope": "POS"}
    assert Agilent81150(Answers("imm", "neg")).trigger_state() == \
        {"arm_source": "IMM", "arm_slope": "NEG"}
    assert Agilent81150(Answers("MAN", "")).trigger_state() == \
        {"arm_source": "MAN", "arm_slope": "?"}
    assert Agilent81150(Answers(OSError("timeout"), "POS")).trigger_state() == \
        {"arm_source": "?", "arm_slope": "POS"}

    # the older spelling still works, for the bench harness and tools/scan.py
    io = FakeIO(idn="Agilent Technologies,81150A")
    Agilent81150(io).configure_trigger(TriggerConfig(external=True, positive_slope=False))
    assert ":ARM:SLOP NEG;" in io.log
    Agilent81150(io).configure_trigger()
    assert io.log.count(":ARM:SOUR1 EXT;") == 2


# -- Newport 1918-C -------------------------------------------------------
class FakeConsole:
    """Stands in for the 1918-C console's HTTP API."""

    def __init__(self, reading=None, capture=None):
        self.log = []
        self.reading = reading or {"value": 1.85e-4, "units": 2, "wavelength": 530,
                                   "status": {"saturated": False, "overrange": False}}
        self.capture = capture or {"collected": 500, "mean": 1.85e-4, "sdev": 7e-7}

    def install(self, meter):
        def request(method, path, body):
            self.log.append((method, path, body))
            if path == "/api/reading":
                return self.reading
            if path == "/api/capture":
                return self.capture
            return {}
        meter._request = request
        return meter


def test_the_power_meter_goes_through_the_console_that_owns_the_device():
    """Only one process can hold the USB meter, and on this rig that is the
    1918-C console. A second driver opening it fails in the confusing way."""
    from bace.drivers.newport1918c import ConsolePowerMeter
    c = FakeConsole()
    m = c.install(ConsolePowerMeter())
    assert isinstance(m, PowerMeter)
    assert m.read_power() == pytest.approx(1.85e-4)
    assert c.log[0][1] == "/api/reading"


def test_a_saturated_reading_is_reported_not_hidden():
    from bace.drivers.newport1918c import ConsolePowerMeter, PowerMeterError
    c = FakeConsole(reading={"value": 8.4e-4, "units": 2, "wavelength": 530,
                             "status": {"saturated": True, "overrange": False}})
    m = c.install(ConsolePowerMeter())
    # by default the run continues -- one bad sample should not abort an hour
    assert m.read_power() == pytest.approx(8.4e-4)
    assert m.last.saturated and not m.last.trustworthy

    strict = c.install(ConsolePowerMeter(strict=True))
    with pytest.raises(PowerMeterError, match="saturated"):
        strict.read_power()


def test_a_meter_left_in_the_wrong_units_is_not_trusted():
    """Amps and dBm both return perfectly plausible numbers."""
    from bace.drivers.newport1918c import ConsolePowerMeter
    c = FakeConsole(reading={"value": 1.85e-4, "units": 0, "wavelength": 530,
                             "status": {"saturated": False, "overrange": False}})
    m = c.install(ConsolePowerMeter())
    m.read_power()
    assert m.last.trustworthy is False


def test_a_short_capture_raises_rather_than_averaging_fewer_samples():
    from bace.drivers.newport1918c import ConsolePowerMeter, PowerMeterError
    c = FakeConsole(capture={"collected": 3, "mean": 1e-4, "sdev": 0.0,
                             "measuredMsPerSample": 900})
    m = c.install(ConsolePowerMeter())
    with pytest.raises(PowerMeterError, match="collected 3 of 100"):
        m.read_statistics(100)


def test_an_unreachable_console_says_what_to_do():
    """Naming a console is now a choice, so the message has two fixes, not
    one: start that program, or stop naming it and let the service open the
    meter. Before 2026-09-03 it said opening the meter here would fail
    anyway, which is exactly wrong when the console is not running."""
    from bace.drivers.newport1918c import ConsolePowerMeter, PowerMeterError
    m = ConsolePowerMeter(base_url="http://127.0.0.1:9", timeout_s=0.2)
    assert m.available() is False
    with pytest.raises(PowerMeterError, match="clear \\[power_meter\\] console"):
        m.read_power()


# -- Lake Shore 331 -------------------------------------------------------
STATE_331 = {"connected": True, "control_temperature": 249.93, "temperature_a": 249.93,
             "temperature_b": 251.1, "setpoint": 250.0, "heater_percent": 31.5,
             "heater_range": 3, "ramping": False, "ramp_on": False, "ramp_rate": 1.0,
             "heater_fault": 0, "status_text": "ok", "elapsed_s": 812.4,
             "max_setpoint_k": 350.0, "last_error": None, "history": [],
             "idn": "LSCI,MODEL331S,331000,1.1", "control_loop": 1, "control_input": "A"}
"""`/api/state` as `ls331/service.py` publishes it, keys the service reads
and keys it does not."""


class Fake331Console:
    """Stands in for the 331 console's HTTP API at the request level, with
    the console's own rules: the ceiling refused in its words, never
    clamped; a setpoint read back after the write."""

    def __init__(self, state=None, *, ceiling=350.0, note_ok=True):
        self.log = []
        self.timeouts = []
        self.state = dict(STATE_331 if state is None else state)
        self.ceiling = ceiling
        self.note_ok = note_ok

    def install(self, controller):
        from bace.drivers.lakeshore331 import TemperatureError

        def request(method, path, body, *, timeout_s=None):
            self.log.append((method, path, body))
            self.timeouts.append((path, timeout_s))
            if path == "/api/state":
                return dict(self.state)
            if path == "/api/setpoint":
                kelvin = float(body["kelvin"])
                if kelvin > self.ceiling:
                    message = ("setpoint %.3f K exceeds the %.1f K limit for this cryostat"
                               % (kelvin, self.ceiling))
                    raise TemperatureError(
                        message + " (the 331 console refused POST /api/setpoint with HTTP 403)",
                        refused=True, status=403, console_message=message)
                self.state["setpoint"] = kelvin
                return {"ok": True, "setpoint": kelvin}
            if path == "/api/note":
                if not self.note_ok:
                    raise TemperatureError("empty note (HTTP 400)", refused=True, status=400)
                return {"ok": True}
            return {}
        controller._request = request
        return controller


class _HTTPReply:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_the_331_is_a_client_of_the_console_that_owns_it():
    """One process holds the 331's GPIB session -- its console -- so this is
    an HTTP client and satisfies the protocol without a VISA resource."""
    from bace.drivers.lakeshore331 import ConsoleTemperatureController, TemperatureReading
    from bace.drivers.protocols import TemperatureController

    c = Fake331Console()
    t = c.install(ConsoleTemperatureController())
    assert isinstance(t, TemperatureController) and t.base_url == "http://127.0.0.1:8331"
    r = t.read()
    assert isinstance(r, TemperatureReading) and t.last is r
    assert (r.kelvin, r.setpoint_k, r.ramping, r.heater_range) == (249.93, 250.0, False, 3)
    assert (r.connected, r.status_text, r.elapsed_s, r.max_setpoint_k) == (True, "ok", 812.4, 350.0)
    assert t.set_setpoint(240.0) == 240.0
    assert t.read().setpoint_k == 240.0
    assert t.note("bace 20260902_120000-001 T=240K: setpoint 240 K") is True
    assert [(m, p) for m, p, _ in c.log] == [("GET", "/api/state"), ("POST", "/api/setpoint"),
                                             ("GET", "/api/state"), ("POST", "/api/note")]
    bodies = {p: b for _, p, b in c.log if b is not None}
    assert bodies["/api/setpoint"] == {"kelvin": 240.0}
    assert bodies["/api/note"] == {"text": "bace 20260902_120000-001 T=240K: setpoint 240 K"}


def test_a_state_with_the_instrument_silent_is_still_a_reading():
    """`connected` False is the console's word that the 331 is not answering
    on the bus: the reading comes back with the flag down, the control
    temperature falls back to sensor A, and the console's error is the
    status -- so a caller can say "console up, instrument silent"."""
    from bace.drivers.lakeshore331 import ConsoleTemperatureController

    state = {**STATE_331, "connected": False, "last_error": "VI_ERROR_TMO on KRDG? A",
             "status_text": ""}
    del state["control_temperature"]
    t = Fake331Console(state).install(ConsoleTemperatureController())
    r = t.read()
    assert r.connected is False and r.kelvin == 249.93
    assert r.status_text == "VI_ERROR_TMO on KRDG? A"
    bare = Fake331Console({"connected": False, "max_setpoint_k": 350.0}).install(
        ConsoleTemperatureController())
    r = bare.read()
    assert r.connected is False and r.kelvin != r.kelvin, "no temperature yet: NaN, not a number"
    assert r.setpoint_k is None and r.max_setpoint_k == 350.0


def test_a_setpoint_above_the_ceiling_is_refused_in_the_consoles_words_and_never_clamped(
        monkeypatch):
    """The real `_request` against the console's own 403 body. The ceiling
    is enforced in the console process; here nothing is clamped, nothing
    retried, and the message is the console's."""
    import io as _io
    import urllib.error
    import urllib.request

    from bace.drivers.lakeshore331 import ConsoleTemperatureController, TemperatureError

    posted = []

    def refuse(req, timeout=None):
        posted.append((req.full_url, json.loads(req.data.decode())))
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", {},
            _io.BytesIO(b'{"ok": false, "error": "setpoint 400.000 K exceeds the 350.0 K '
                        b'limit for this cryostat"}'))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    t = ConsoleTemperatureController("http://127.0.0.1:8331")
    with pytest.raises(TemperatureError) as e:
        t.set_setpoint(400.0)
    msg = str(e.value)
    assert msg.startswith("setpoint 400.000 K exceeds the 350.0 K limit for this cryostat")
    assert "HTTP 403" in msg and "cannot reach" not in msg
    assert e.value.refused is True and e.value.status == 403
    assert posted == [("http://127.0.0.1:8331/api/setpoint", {"kelvin": 400.0})], (
        "one request, the refused one; no second write at 350 K")

    def bad_body(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                     _io.BytesIO(b'{"ok": false, "error": "bad request: '
                                                 b'float() argument must be a string"}'))

    monkeypatch.setattr(urllib.request, "urlopen", bad_body)
    with pytest.raises(TemperatureError, match="^bad request: float") as e:
        t.set_setpoint(250.0)
    assert e.value.refused and e.value.status == 400

    def dead(req, timeout=None):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", dead)
    with pytest.raises(TemperatureError, match="cannot reach the 331 console") as e:
        t.read()
    assert e.value.refused is False and e.value.status is None


def test_a_note_that_fails_is_swallowed_because_a_log_mark_must_not_stop_a_run():
    from bace.drivers.lakeshore331 import ConsoleTemperatureController

    assert Fake331Console(note_ok=False).install(ConsoleTemperatureController()).note("x") is False
    assert Fake331Console().install(ConsoleTemperatureController()).note("x") is True
    dead = ConsoleTemperatureController("http://127.0.0.1:9", timeout_s=0.2)
    assert dead.note("bace: setpoint 250 K") is False


def test_available_keys_on_the_consoles_own_liveness_key(monkeypatch):
    """`max_setpoint_k` is what `console_server._already_running` looks for
    to recognise itself; something else answering on the port is not the
    console."""
    import urllib.error
    import urllib.request

    from bace.drivers.lakeshore331 import ConsoleTemperatureController

    t = ConsoleTemperatureController("http://127.0.0.1:8331")
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _HTTPReply({"hello": "world"}))
    assert t.available() is False
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _HTTPReply(STATE_331))
    assert t.available() is True

    def dead(req, timeout=None):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", dead)
    assert t.available() is False


def test_the_simulated_331_converges_per_read_and_refuses_above_the_ceiling():
    """One step per `read()`, not per second, so `--fast` converges in a
    handful of polls and a test can count them; the ceiling refused in the
    console's words and nothing clamped."""
    from bace.drivers.lakeshore331 import TemperatureError
    from bace.drivers.protocols import TemperatureController
    from bace.drivers.simulated import make_bench

    t = make_bench().temperature
    assert isinstance(t, TemperatureController)
    assert t.read().kelvin == 294.8, "at rest it stays where it is"
    assert t.set_setpoint(250.0) == 250.0
    reads = 0
    while abs(t.read().kelvin - 250.0) > 0.2:
        reads += 1
        assert reads < 200
    assert reads + 1 <= t.time_constant_reads * 5
    assert t.last.setpoint_k == 250.0 and t.last.connected and not t.last.ramping
    assert t.last.heater_range == 3 and t.last.max_setpoint_k == 350.0
    with pytest.raises(TemperatureError, match="exceeds the 350.0 K limit for this cryostat") as e:
        t.set_setpoint(400.0)
    assert e.value.refused and t.setpoint_k == 250.0 and t.setpoints == [250.0]
    assert t.note("mark") is True and t.notes == ["mark"]
    t.connected = False
    assert t.read().connected is False


# -- config ---------------------------------------------------------------
def test_rig_toml_carries_the_confirmed_addresses():
    r = load_rig("rig.toml")
    assert r.sourcemeter_address == "GPIB0::24::INSTR"
    assert r.led_address == "GPIB0::15::INSTR"
    assert r.bias_address == "GPIB0::12::INSTR"
    assert r.scope_address.startswith("TCPIP0::")
    assert r.sense_resistor_ohm == pytest.approx(5.192)
    assert r.current_sign == -1.0
    assert (r.shutter_module_nr, r.relay_module_nr) == (0, 1)


def test_the_current_sign_is_a_sign_not_a_gain(tmp_path):
    """`current_sign = -1` is an integer to TOML and must come out a float,
    because it multiplies an array. And it must be exactly +1 or -1: anything
    else is a gain under the wrong name, rescaling every charge as silently as
    a wrong sense resistor would."""
    p = tmp_path / "rig.toml"
    p.write_text('[electrical]\ncurrent_sign = -1\n')
    r = load_rig(p)
    assert r.current_sign == -1.0 and isinstance(r.current_sign, float)

    p.write_text('[electrical]\ncurrent_sign = 2\n')
    with pytest.raises(ConfigError, match=r"\+1 or -1"):
        load_rig(p)


def test_run_toml_builds_a_usable_plan():
    spec, run, drive, smu, meta, extras = load_run("run.toml")
    plan = spec.plan(0.906)
    assert plan.n_steps == 1 and plan.n_loops == 100      # repeats via loops
    assert plan.setpoints[0].vpre == pytest.approx(0.906)
    # the validated 2026-09-02 recipe: 200 averages, LED 1.000 V (run.toml header)
    assert run.n_averages == 200
    assert drive.level == pytest.approx(1.000)
    # invert_polarity WITH the generator at NORM: bare.py --invert matched
    # LabVIEW at 02:08 with NORM in force, the 02:20 read-back shows LabVIEW
    # itself finishes at NORM, and every INV run collapsed the photo peak
    # from ~3 mA to ~0.5 mA (the device rests at v_coll through the
    # inverting amplifier, so extraction never stops).
    assert run.invert_polarity and run.output_polarity == "NORM"
    assert run.shutter_settle_s == pytest.approx(5.0)
    assert meta.temperature_k == pytest.approx(290.0)


def test_compliance_is_a_run_setting_under_a_bench_ceiling():
    """Adjustable, because the right compliance depends on the pixel. Bounded,
    because no measurement recipe should be able to authorise 1 A."""
    from bace.config import check_smu_limits
    rig = load_rig("rig.toml")
    _, _, _, smu, _, _ = load_run("run.toml")
    assert smu.current_compliance_a < rig.max_current_compliance_a
    check_smu_limits(smu, rig)

    from bace.drivers.keithley2400 import SourceMeterConfig
    with pytest.raises(ConfigError, match="bench ceiling"):
        check_smu_limits(SourceMeterConfig(current_compliance_a=1.0), rig)


def test_a_typo_in_the_rig_file_is_an_error_not_a_default(tmp_path):
    p = tmp_path / "rig.toml"
    p.write_text('[electrical]\nsense_resistor_ohms = 5.192\n')   # trailing s
    with pytest.raises(ConfigError, match="sense_resistor_ohms"):
        load_rig(p)


def test_the_shutter_and_relay_cannot_share_a_module(tmp_path):
    p = tmp_path / "rig.toml"
    p.write_text('[dio]\nmodule_id = 9\nshutter_module_nr = 1\n'
                 'relay_module_nr = 1\nchannel = 0\n')
    with pytest.raises(ConfigError, match="both on DIO module 1"):
        load_rig(p)


def test_the_led_polarity_can_be_set_not_only_read():
    """`:OUTP:POL INV` is what makes the 33220A Sync's rising edge mean *light
    off* -- inverting the waveform flips the lit half of the cycle while leaving
    the Sync alone (33220A User's Guide p.67). Nothing in this package set it
    until 2026-09-01; the run inherited whatever the front panel held, and no
    output recorded which."""
    from bace.drivers.agilent33220a import Agilent33220A

    class IO:
        timeout = 0
        def __init__(self): self.log = []
        def write(self, c): self.log.append(c)
        def query(self, c):
            self.log.append(c)
            return "INV"

    io_ = IO()
    g = Agilent33220A(io_)
    g.set_polarity(True)
    assert ":OUTP:POL INV;" in io_.log
    g.set_polarity(False)
    assert ":OUTP:POL NORM;" in io_.log
    assert g.polarity() == "INV"


def test_the_led_polarity_readback_says_unknown_rather_than_raising():
    """`LedSource.polarity` promises `NORM`, `INV` or `?`. The `?` branch is
    what lets a run read the polarity into its file without a dead GPIB link
    masking an exception already propagating, and what makes `tools/scan.py`'s
    pre-flight refuse (`?` is not `INV`) instead of crash. Same shape as the
    81150A `trigger_state` readback."""
    from bace.drivers.agilent33220a import Agilent33220A

    class Silent(FakeIO):
        def __init__(self, reply):
            super().__init__(idn="Agilent Technologies,33220A,MY4,2.0")
            self.reply = reply

        def query(self, cmd):
            self.log.append(cmd)
            if ":OUTP:POL?" in cmd:
                if isinstance(self.reply, Exception):
                    raise self.reply
                return self.reply
            return super().query(cmd)

    assert Agilent33220A(Silent(OSError("VI_ERROR_TMO"))).polarity() == "?"
    assert Agilent33220A(Silent("")).polarity() == "?"
    assert Agilent33220A(Silent("\n")).polarity() == "?"
    assert Agilent33220A(Silent("norm\n")).polarity() == "NORM"


def test_the_console_post_bodies_use_the_keys_the_console_actually_reads():
    """The console does `int(body.get("code"))` for units, `float(body.get("nm"))`
    for the wavelength and `body.get("size")/("intervalMs")` for a capture. This
    driver sent `{"units": 2}`, so the console got `int(None)` and answered HTTP
    400 -- on the rig, 2026-09-01, the first time anything called it for real.
    The fakes in the other tests replace `_request` wholesale, so nothing here
    had ever checked a body key against the service on the other end."""
    from bace.drivers.newport1918c import ConsolePowerMeter, WATTS
    c = FakeConsole()
    m = c.install(ConsolePowerMeter())
    m.set_units_watts()
    m.set_wavelength(530.0)
    m.read_statistics(500, interval_ms=10)
    bodies = {path: body for _, path, body in c.log}
    assert bodies["/api/units"] == {"code": WATTS}
    assert bodies["/api/wavelength"] == {"nm": 530.0}
    assert bodies["/api/capture"] == {"size": 500, "intervalMs": 10}


def test_a_console_that_answers_with_an_error_is_not_reported_as_unreachable():
    """`HTTPError` is a subclass of `URLError`. Catching only `URLError` turned
    every rejected request into "cannot reach the console" -- which is the one
    thing a 400 proves is false, and it cost an evening."""
    import urllib.error

    from bace.drivers.newport1918c import ConsolePowerMeter, PowerMeterError

    m = ConsolePowerMeter("http://127.0.0.1:8918")

    import io as _io
    import urllib.request as ur

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                     _io.BytesIO(b'{"error": "code missing"}'))

    old, ur.urlopen = ur.urlopen, boom
    try:
        with pytest.raises(PowerMeterError) as e:
            m.set_units_watts()
    finally:
        ur.urlopen = old
    msg = str(e.value)
    assert "HTTP 400" in msg
    assert "refused" in msg
    assert "cannot reach" not in msg


# -- read-back: what the instrument holds, not what the driver remembers ----
class ScriptedIO(FakeIO):
    """Answers each query from a table keyed on the command; a query not in
    the table raises, as a dead GPIB link would."""

    def __init__(self, replies: dict[str, str], idn: str = "x"):
        super().__init__(idn=idn)
        self.replies = replies

    def query(self, cmd):
        self.log.append(cmd)
        key = cmd.strip().upper().rstrip(";")
        if key in self.replies:
            return self.replies[key]
        raise OSError(f"VI_ERROR_TMO on {cmd!r}")


def test_the_81150a_read_back_asks_the_instrument_and_refreshes_the_interlock_flag():
    """A generator left ON by the LabVIEW VI: a driver constructed a minute
    ago says `output_enabled` is False, and the relay would be thrown under
    a live source. `read_output` asks, and the flag the router reads follows
    the answer. The spellings are the ones tools/scan.py sent by hand."""
    from bace.drivers.agilent81150 import Agilent81150

    io = ScriptedIO({":OUTP1?": "1\n", ":OUTP1:POL?": "INV\n", ":VOLT1:HIGH?": "+2.50000E-01",
                     ":VOLT1:LOW?": "-2.50000E-01", ":FREQ1?": "+5.00000E+02",
                     ":PULS:DEL1?": "+9.00000E-08", ":FUNC1:PULS:WIDT?": "+5.00000E-06"})
    g = Agilent81150(io)
    assert g.output_enabled is False and g.output_polarity == "?"
    state = g.read_state()
    assert state == {"output": True, "polarity": "INV", "high_v": 0.25, "low_v": -0.25,
                     "frequency_hz": 500.0, "delay_s": 9e-8, "width_s": 5e-6}
    assert g.output_enabled is True, "the interlock now sees the front panel"
    assert g.output_polarity == "INV", "the chain can be judged before any run"
    assert [c for c in io.log if c.endswith("?")] == [
        ":OUTP1?", ":OUTP1:POL?", ":VOLT1:HIGH?", ":VOLT1:LOW?", ":FREQ1?",
        ":PULS:DEL1?", ":FUNC1:PULS:WIDT?"]
    assert not any(c for c in io.log if not c.endswith("?")), "a read-back writes nothing"


def test_the_81150a_read_back_says_unknown_when_the_instrument_will_not_answer():
    from bace.drivers.agilent81150 import Agilent81150

    g = Agilent81150(ScriptedIO({":OUTP1?": "0"}))
    g.enable_output(True)                      # what the driver remembers
    state = g.read_state()
    assert state["output"] is False and g.output_enabled is False, "the answer wins"
    assert state["polarity"] == "?" and state["high_v"] is None and state["delay_s"] is None

    dead = Agilent81150(ScriptedIO({}))
    dead.enable_output(True)
    assert dead.read_output() is None
    assert dead.output_enabled is True, "no answer changes nothing"
    assert dead.read_polarity() == "?" and dead.output_polarity == "?"


def test_the_33220a_read_back_reports_mode_levels_and_polarity_from_the_instrument():
    """The levels were checked by nobody until 2026-09-01: a recipe that
    changes the LED level and a generator that keeps the old one is a
    charge scaled by the wrong intensity with no symptom in the data."""
    from bace.drivers.agilent33220a import Agilent33220A

    io = ScriptedIO({":OUTP?": "1", ":OUTP:POL?": "NORM", "FUNC:SHAP?": "PULS",
                     ":VOLT:HIGH?": "+1.02000E+00", ":VOLT:LOW?": "+4.00000E-01",
                     ":VOLT:OFFS?": "+7.10000E-01", ":FREQ?": "+5.00000E+02",
                     "OUTP:SYNC?": "1"})
    g = Agilent33220A(io)
    assert g.output_enabled is False and g.mode == "OFF"
    state = g.read_state()
    assert state == {"output": True, "polarity": "NORM", "mode": "PULSE", "shape": "PULS",
                     "high_v": 1.02, "low_v": 0.4, "offset_v": 0.71, "frequency_hz": 500.0,
                     "sync_output": True}
    assert g.output_enabled is True and g.mode == "PULSE"

    off = Agilent33220A(ScriptedIO({":OUTP?": "0", "FUNC:SHAP?": "DC", ":OUTP:POL?": "INV"}))
    state = off.read_state()
    assert state["mode"] == "OFF" and state["shape"] == "DC", "an output that is off is dark"
    assert state["high_v"] is None and state["polarity"] == "INV"

    odd = Agilent33220A(ScriptedIO({":OUTP?": "1", "FUNC:SHAP?": "SIN"}))
    assert odd.read_state()["mode"] == "SIN", "a shape this driver never sets is reported as spelt"
    assert odd.mode == "OFF", "and the cached mode is not overwritten with one it cannot mean"

    dead = Agilent33220A(ScriptedIO({}))
    assert dead.read_state() == {"output": None, "polarity": "?", "mode": "?", "shape": "?",
                                 "high_v": None, "low_v": None, "offset_v": None,
                                 "frequency_hz": None, "sync_output": None}


def test_the_33220a_read_back_asks_for_the_sync_output():
    """The Sync connector is what arms the 81150A, which is what triggers the
    scope; it has its own front-panel key, and a scan run with it off measures
    noise with no other symptom in the chain. So the read-back asks
    `OUTP:SYNC?` and reports it as `sync_output`; a generator that will not
    answer gives None, never a plausible True."""
    from bace.drivers.agilent33220a import Agilent33220A

    io = ScriptedIO({":OUTP?": "1", "FUNC:SHAP?": "PULS", "OUTP:SYNC?": "0\n"})
    g = Agilent33220A(io)
    state = g.read_state()
    assert state["sync_output"] is False and state["output"] is True
    assert "OUTP:SYNC?" in io.log, "the Sync was asked, not assumed"
    assert not any(c for c in io.log if not c.endswith("?")), "a read-back writes nothing"
    assert Agilent33220A(ScriptedIO({"OUTP:SYNC?": "ON"})).read_state()["sync_output"] is True
    assert Agilent33220A(ScriptedIO({":OUTP?": "1"})).read_state()["sync_output"] is None


def test_the_keithley_read_back_refreshes_the_interlock_flag():
    io = ScriptedIO({":OUTP?": "1"}, idn="KEITHLEY INSTRUMENTS INC.,MODEL 2400,4473504,C34")
    k = Keithley2400(io)
    assert k.output_enabled is False
    assert k.read_output() is True and k.output_enabled is True
    assert io.log[-1] == ":OUTP?"
    assert Keithley2400(ScriptedIO({})).read_output() is None


def test_the_setpoint_write_waits_longer_than_the_consoles_bus_job_and_reads_do_not():
    """`POST /api/setpoint` is served on the console's bus thread and waits
    up to 8 s for the job; a client that gives up sooner reports a setpoint
    the console then applies as a failure. Reads answer from the cache and
    keep the short timeout."""
    from bace.drivers.lakeshore331 import (READ_TIMEOUT_S, WRITE_TIMEOUT_S,
                                           ConsoleTemperatureController)

    c = Fake331Console()
    t = c.install(ConsoleTemperatureController())
    t.read()
    t.set_setpoint(240.0)
    t.note("x")
    assert c.timeouts == [("/api/state", None), ("/api/setpoint", WRITE_TIMEOUT_S),
                          ("/api/note", None)]
    assert (t.timeout_s, t.write_timeout_s) == (READ_TIMEOUT_S, WRITE_TIMEOUT_S) == (3.0, 10.0)
    assert WRITE_TIMEOUT_S > 8.0, "the console's submit() waits 8 s for the bus job"


def test_the_consoles_own_sentence_is_kept_apart_from_the_transport_text(monkeypatch):
    import io as _io
    import urllib.error
    import urllib.request

    from bace.drivers.lakeshore331 import ConsoleTemperatureController, TemperatureError

    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", {},
            _io.BytesIO(b'{"ok": false, "error": "setpoint 400.000 K exceeds the 350.0 K '
                        b'limit for this cryostat"}'))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    t = ConsoleTemperatureController("http://127.0.0.1:8331")
    with pytest.raises(TemperatureError) as e:
        t.set_setpoint(400.0)
    assert e.value.console_message == "setpoint 400.000 K exceeds the 350.0 K limit for this cryostat"
    assert str(e.value).startswith(e.value.console_message) and "HTTP 403" in str(e.value)
    assert "HTTP 403" not in e.value.console_message

    def bare(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {},
                                     _io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", bare)
    with pytest.raises(TemperatureError) as e:
        t.set_setpoint(250.0)
    assert e.value.console_message == "HTTP 500" and e.value.refused and e.value.status == 500


def test_a_socket_that_dies_while_the_reply_is_read_is_an_unreachable_console(monkeypatch):
    """`r.read()` is outside urlopen's own wrapping: a timeout or a closed
    socket there must still be the driver's error, so `available()` keeps
    its bool and `read()` its contract."""
    import socket
    import urllib.request

    from bace.drivers.lakeshore331 import ConsoleTemperatureController, TemperatureError

    class Dying:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            raise socket.timeout("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: Dying())
    t = ConsoleTemperatureController("http://127.0.0.1:8331")
    assert t.available() is False and t.probe() is None
    with pytest.raises(TemperatureError, match="cannot reach the 331 console") as e:
        t.read()
    assert e.value.refused is False and "timed out" in str(e.value)
    assert t.note("x") is False


def test_probe_tells_a_console_before_its_first_poll_from_no_console(monkeypatch):
    """The console's state before one successful poll is `{"connected":
    false}` with no ceiling: not `available`, but answered by something --
    the bench words its reason from that."""
    import urllib.request

    from bace.drivers.lakeshore331 import ConsoleTemperatureController

    t = ConsoleTemperatureController("http://127.0.0.1:8331")
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _HTTPReply({"connected": False}))
    assert t.probe() == {"connected": False} and t.available() is False
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _HTTPReply([1, 2]))
    assert t.probe() is None and t.available() is False


def test_a_long_sweep_sizes_the_visa_timeout_from_its_own_arithmetic():
    """The whole J-V sweep is one `:READ?`: the 2400 steps, settles, integrates
    and averages every point before it answers. With the validated recipe
    (71 points, 50 ms source delay, averaging 10, NPLC 1) that is ~18 s, and
    the session's blanket 20 s VISA timeout cut it off on the rig
    (VI_ERROR_TMO, session 20260902_143927). The driver now raises the
    resource timeout for the read -- sized from the sweep, factor two -- and
    puts it back afterwards."""

    class Timed(FakeIO):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.timeout = 20000
            self.timeout_at_read = None

        def query(self, cmd):
            if ":READ?" in cmd:
                self.timeout_at_read = self.timeout
            return super().query(cmd)

    io_ = Timed(reads=["0.0,-2.0E-4,0.5,-1.5E-4"])
    k = Keithley2400(io_, config=SourceMeterConfig(averaging=10, nplc=1.0))
    k.sweep(0.0, 0.5, 71, settle_s=0.05)
    # 71 x (0.05 + 10 x 0.02) = 17.75 s of instrument time; the budget is
    # 10 + 2 x that, in ms
    assert io_.timeout_at_read >= 45000
    assert io_.timeout == 20000, "restored, so the next quick query fails fast"

    # a quick sweep leaves the session's timeout alone
    io_ = Timed(reads=["0.0,-2.0E-4,0.5,-1.5E-4"])
    Keithley2400(io_, config=SourceMeterConfig(averaging=1, nplc=1.0)).sweep(
        0.0, 0.5, 3, settle_s=0.0)
    assert io_.timeout_at_read == 20000
