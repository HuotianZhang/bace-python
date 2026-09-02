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

    def query(self, cmd):
        self.log.append(cmd)
        if "*IDN?" in cmd:
            return self.idn
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
    from bace.drivers.newport1918c import ConsolePowerMeter, PowerMeterError
    m = ConsolePowerMeter(base_url="http://127.0.0.1:9", timeout_s=0.2)
    assert m.available() is False
    with pytest.raises(PowerMeterError, match="start it"):
        m.read_power()


# -- config ---------------------------------------------------------------
def test_rig_toml_carries_the_confirmed_addresses():
    r = load_rig("rig.toml")
    assert r.sourcemeter_address == "GPIB0::24::INSTR"
    assert r.led_address == "GPIB0::15::INSTR"
    assert r.bias_address == "GPIB0::12::INSTR"
    assert r.scope_address.startswith("TCPIP0::")
    assert r.sense_resistor_ohm == pytest.approx(5.192)
    assert (r.shutter_module_nr, r.relay_module_nr) == (0, 1)


def test_run_toml_builds_a_usable_plan():
    spec, run, drive, smu, meta, extras = load_run("run.toml")
    plan = spec.plan(0.906)
    assert plan.n_steps == 1 and plan.n_loops == 100      # repeats via loops
    assert plan.setpoints[0].vpre == pytest.approx(0.906)
    assert run.n_averages == 20
    assert drive.level == pytest.approx(1.020)
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
