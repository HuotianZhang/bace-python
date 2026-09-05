"""The two instruments the service opens itself: the 1918-C and the 331.

Both used to be reached through a console -- a separate program holding the
device, asked over HTTP. That arrangement had one precondition, that somebody
start the console, and on this rig nobody does; a run therefore recorded no
intensity and every temperature node paused. So the instrument code was
vendored in (`bace/drivers/newport1918c/meter.py`, `bace/drivers/lakeshore331/`)
and the service became the owner.

What is asserted here is the part that would otherwise be rediscovered on
hardware, plus the part that has to keep behaving exactly as the console path
did so nothing above the driver had to change:

* the DLL choice (a 64-bit interpreter must not load the 32-bit build),
* the command echo and the status word, which are why `PM:PWS?` is read
  instead of `PM:P?`,
* the 350 K ceiling refusing rather than clamping, in the *shape* the
  console's HTTP 403 produced,
* provenance: a reading this process took must never be labelled `simulated`,
* and that `[power_meter] console` / `[temperature] console`, when named,
  still send everything through the console client.

`tests/test_drivers.py` covers the console clients themselves against fakes;
this file covers the direct path and the choice between them.
"""
from __future__ import annotations

import struct

import pytest

from bace.drivers.lakeshore331 import (ConsoleTemperatureController,
                                       DirectTemperatureController,
                                       TemperatureError,
                                       open_temperature_controller)
from bace.drivers.lakeshore331 import controller as ls_controller
from bace.drivers.lakeshore331 import protocol as p
from bace.drivers.lakeshore331.transport import SimulatedTransport as Sim331
from bace.drivers.lakeshore331.transport import TransportError
from bace.drivers.newport1918c import (ConsolePowerMeter, DirectPowerMeter,
                                       PowerMeterError, open_power_meter)
from bace.drivers.newport1918c import meter as np
from bace.experiment.rig import RigConfig
from bace.service.rigs import temperature_source


# ===========================================================================
# 1918-C: the facts that only appear on hardware
# ===========================================================================

def test_the_dll_chosen_matches_the_interpreters_bitness(tmp_path, monkeypatch):
    """The Newport package installs both builds. Taking the first path that
    exists picks the wrong one half the time, and the symptom is a bare
    WinError 193 that names nothing -- so the build is chosen by reading the
    PE header, not by the order of the search list."""
    def pe(name: str, machine: int):
        path = tmp_path / name
        header = bytearray(0x400)
        header[0x3C:0x40] = (0x80).to_bytes(4, "little")
        header[0x84:0x86] = machine.to_bytes(2, "little")   # 0x80 + 4
        path.write_bytes(bytes(header))
        return str(path)

    x86, x64 = pe("x86.dll", 0x014C), pe("x64.dll", 0x8664)
    assert np.pe_bitness(x86) == 32 and np.pe_bitness(x64) == 64
    assert np.python_bitness() == struct.calcsize("P") * 8

    # the 32-bit build listed first, as it is in the real search order
    monkeypatch.setattr(np, "DLL_CANDIDATES", (x86, x64))
    want = np.python_bitness()
    assert np.find_dll() == (x64 if want == 64 else x86)

    # only the wrong build installed: None, so the caller can say so, rather
    # than a load that fails with an error naming no cause
    monkeypatch.setattr(np, "DLL_CANDIDATES",
                        (x86 if want == 64 else x64,))
    assert np.find_dll() is None
    # an explicit path is taken as given, but still has to exist
    assert np.find_dll(x86) == x86
    assert np.find_dll(str(tmp_path / "nope.dll")) is None


def test_the_status_word_is_hexadecimal_and_only_three_bits_are_problems():
    """`PM:PWS?` carries units, range and the health bits; `PM:P?` does not,
    and silently clipped data is the classic way to ruin a long log. A large
    status is normal -- the units and range fields alone are usually non-zero
    -- so only bits 0-2 may be read as trouble."""
    healthy = np.decode_status("108")          # units 2 (W), range 0, detector
    assert healthy["units"] == 2 and healthy["unitsName"] == "W"
    assert healthy["detector"] and healthy["healthy"]
    assert not healthy["saturated"] and not healthy["overrange"]

    assert np.decode_status("10A")["saturated"] is True
    assert np.decode_status("109")["overrange"] is True
    assert np.decode_status("10A")["healthy"] is False
    # hexadecimal, not decimal: "10" is 16, so the range field is 1
    assert np.decode_status("10")["range"] == 1
    assert np.decode_status("zz")["ok"] is False


def test_the_meter_echoes_the_command_before_its_reply():
    """A naive query returns the command. The driver strips it; this pins
    that, because the failure mode is a float() of "PM:LAMBDA?"."""
    class Echo:
        description = "echo"

        def open(self): pass

        def close(self): pass

        def write(self, command): self._last = command

        def read(self, timeout=1.0): return self._last + "\r530\r"

    m = np.PowerMeter(Echo())
    assert m.query("PM:Lambda?") == "530"


# ===========================================================================
# 1918-C: the service's adapter
# ===========================================================================

def test_the_direct_meter_reads_watts_and_says_where_they_came_from():
    m = DirectPowerMeter(simulate=True, wavelength_nm=530.0)
    try:
        m.set_units_watts()
        r = m.read()
        assert r.watts > 0 and r.units == 2 and r.trustworthy
        assert m.last is r, "the bench read-back reads `last`, not the meter"
        assert m.read_power() > 0
        assert m.last is not r and m.last.watts > 0, "every read replaces it"
        assert m.available() is True
        # `simulated` because it is: the property exists so a *real* reading
        # is never labelled that way by the fall-through in `power_reading`
        assert m.source == "simulated"
        m.info["simulated"] = False
        assert m.source == "usb"
    finally:
        m.close()


def test_a_short_capture_is_refused_rather_than_averaged():
    """Statistics come off the meter's own clock through its data store; a
    polled loop over USB cannot give a deterministic rate. A capture that
    collected fewer samples than asked is a rate the meter did not keep, and
    that must not survive into a published number."""
    m = DirectPowerMeter(simulate=True)
    try:
        mean, sdev = m.read_statistics(20)
        assert mean > 0 and sdev >= 0

        m._meter.capture = lambda *a, **k: {"collected": 3, "mean": 1.0,
                                            "sdev": 0.0,
                                            "measuredMsPerSample": 91.0}
        with pytest.raises(PowerMeterError, match="collected 3 of 20"):
            m.read_statistics(20)
    finally:
        m.close()


def test_the_usb_handle_is_given_back_on_every_failed_open(monkeypatch):
    """The device is exclusive: whoever holds it, holds it. So a failure
    after `transport.open()` has to close the transport, or the meter stays
    held by a process that has no object for it -- unavailable to a retry, to
    the meter's own console, and to anything else until the interpreter
    exits. A console would be restarted; this service runs for the day.

    Three places can raise after the handle is taken: `identify()` inside
    `PowerMeter.open`, and the units and wavelength writes that configure it.
    """
    closed: list[int] = []

    class Handle(np.SimulatedTransport):
        def close(self):
            closed.append(1)

    monkeypatch.setattr(np, "SimulatedTransport", Handle)

    # 1. identification fails
    monkeypatch.setattr(np.PowerMeter, "identify",
                        lambda self: (_ for _ in ()).throw(np.MeterError("timeout")))
    with pytest.raises(np.MeterError):
        np.PowerMeter.open(simulate=True)
    assert closed == [1], "the transport was closed before the error escaped"

    # 2. the wavelength is refused, in the adapter's constructor
    monkeypatch.undo()
    monkeypatch.setattr(np, "SimulatedTransport", Handle)
    closed.clear()
    monkeypatch.setattr(np.PowerMeter, "set_wavelength",
                        lambda self, nm: (_ for _ in ()).throw(
                            np.MeterError("400 nm is outside 450-1100 nm")))
    with pytest.raises(PowerMeterError, match="outside"):
        DirectPowerMeter(simulate=True, wavelength_nm=400.0)
    assert closed == [1]

    # 3. the wavelength is refused, in open_power_meter's configuration step
    closed.clear()
    with pytest.raises(PowerMeterError, match="outside"):
        open_power_meter(RigConfig(power_meter_wavelength_nm=400.0), simulate=True)
    assert closed == [1], (
        "build_real registers a closer only on what open_power_meter returns")


def test_the_meter_open_failure_names_the_console_as_the_likely_thief(monkeypatch):
    """Opening the device while the meter's console holds it fails with a
    bare "no Newport device found", which reads like a cable problem and has
    cost hours. The direct driver says what it actually means."""
    monkeypatch.setattr(np, "find_dll", lambda explicit=None: None)
    with pytest.raises(PowerMeterError) as exc:
        DirectPowerMeter()
    assert "usbdll.dll not found" in str(exc.value)
    assert "holds the USB handle" in str(exc.value)


# ===========================================================================
# 331: the safety rule, in the shape the console produced
# ===========================================================================

def _direct331(monkeypatch, **kw) -> DirectTemperatureController:
    monkeypatch.setattr(ls_controller, "open_transport",
                        lambda conn, simulated=False, simulate_loop=1: Sim331())
    return DirectTemperatureController(**kw)


def test_a_setpoint_above_the_ceiling_is_refused_not_clamped(monkeypatch):
    """350 K is this cryostat's limit. Quietly giving 350 K for a requested
    400 K hides the mistake, so it is refused -- and refused in the same
    shape the console's HTTP 403 had (`refused`, `status`, `console_message`),
    because `service.temperature` and every verdict that renders the
    controller's own sentence were written against that shape."""
    t = _direct331(monkeypatch)
    try:
        assert t.set_setpoint(240.0) == pytest.approx(240.0)
        with pytest.raises(TemperatureError) as exc:
            t.set_setpoint(400.0)
        assert exc.value.refused is True and exc.value.status == 403
        assert "exceeds the 350.0 K limit" in exc.value.console_message
        assert t.read().setpoint_k == pytest.approx(240.0), "nothing was clamped"
    finally:
        t.close()

    lower = _direct331(monkeypatch, max_setpoint_k=300.0)
    try:
        assert lower.read().max_setpoint_k == 300.0
        with pytest.raises(TemperatureError, match="exceeds the 300.0 K limit"):
            lower.set_setpoint(320.0)
    finally:
        lower.close()


def test_a_bus_error_reads_as_disconnected_rather_than_raising(monkeypatch):
    """The monitor and the bench card poll this on their own threads and were
    written against a console client that reports `connected: false` instead
    of failing. A driver that raised instead would turn a silent instrument
    into a broken read-back."""
    t = _direct331(monkeypatch)
    try:
        def dead(*a, **k):
            raise TransportError("query 'KRDG? A' failed: timeout")

        t.device.kelvin = dead
        r = t.read()
        assert r.connected is False and "timeout" in r.status_text
        assert r.kelvin != r.kelvin, "NaN, not a stale number"
        assert t.probe()["connected"] is False and t.available() is False
    finally:
        t.close()


def test_a_331_that_never_answers_does_not_produce_a_controller(monkeypatch):
    """Construction discovers the loop wiring, so a controller that exists is
    one that answered. The bench turns this into a line on the card."""
    def refuse(conn, simulated=False, simulate_loop=1):
        raise TransportError("could not open GPIB0::7::INSTR: no such resource")

    monkeypatch.setattr(ls_controller, "open_transport", refuse)
    with pytest.raises(TemperatureError, match="no such resource"):
        DirectTemperatureController()


def test_the_watchdog_cuts_the_heater_after_repeated_sensor_faults(monkeypatch):
    """The console's poller counted bad control-sensor reads and cut the
    heater at `max_consecutive_faults` (`ls331/service.py::_check_faults`).
    That poller did not come with the instrument code, so for one commit
    `emergency_stop` sat in `instrument.py` with nothing calling it while a
    run could leave the heater energised behind a dead sensor. The direct
    owner counts them now, per read -- which is what the monitor and the
    settle both do."""
    bad = p.parse_reading_status("16")          # temperature under-range
    good = p.parse_reading_status("0")
    assert bad.ok is False and good.ok is True

    t = _direct331(monkeypatch)
    writes: list[str] = []
    try:
        t.device._write = lambda msg, reason="": writes.append(msg)
        t.device.reading_status = lambda channel="A": bad

        t.read(); t.read()
        assert writes == [] and t.heater_cut is None, "two is under the limit"
        t.read()
        assert "RANGE 0" in writes, "three consecutive faults cut the heater"
        assert t.heater_cut and "3 consecutive reads" in t.heater_cut

        # the count resets on a good reading, so an intermittent sensor does
        # not accumulate its way to a cut over an hour
        writes.clear()
        t.device.reading_status = lambda channel="A": good
        t.read()
        t.device.reading_status = lambda channel="A": bad
        t.read(); t.read()
        assert writes == []
    finally:
        t.close()

    off = _direct331(monkeypatch, watchdog=False)
    try:
        writes = []
        off.device._write = lambda msg, reason="": writes.append(msg)
        off.device.reading_status = lambda channel="A": bad
        for _ in range(5):
            off.read()
        assert writes == [] and off.heater_cut is None
    finally:
        off.close()


def test_the_watchdog_counts_heater_faults_and_not_only_the_sensor(monkeypatch):
    """An open or shorted heater load while the control sensor reads
    perfectly well: `status.ok` is True on every poll, so a sensor-only
    watchdog resets its count forever and the drive is never cut. The
    vendored `check_faults()` was written to look at both -- and was dead
    code until this counted both too."""
    t = _direct331(monkeypatch)
    writes: list[str] = []
    try:
        t.device._write = lambda msg, reason="": writes.append(msg)
        t.device.reading_status = lambda channel="A": p.parse_reading_status("0")
        t.device.heater_fault = lambda: p.HeaterFault.OPEN_LOAD

        t.read(); t.read()
        assert writes == []
        t.read()
        assert "RANGE 0" in writes
        assert t.heater_cut and "heater reports open load" in t.heater_cut
    finally:
        t.close()


def test_the_worker_feeds_the_watchdog_while_a_run_holds_the_bus(monkeypatch):
    """The gap the bus lock opened. The monitor cannot read while a run holds
    the bus and the watchdog rides on reads, so a heater fault beginning
    after a temperature settles -- with hours of measurement left -- would go
    unseen until the run ended. The worker feeds it from its own sleeps,
    where it already holds the bus."""
    import time as _time

    from bace.service.rigs import Bench

    t = _direct331(monkeypatch)
    try:
        b = Bench.build_simulated(RigConfig(), fast=False)
        b.rig.temperature = t
        polls: list[int] = []
        monkeypatch.setattr(t, "poll_faults", lambda: polls.append(1))

        sleep = b.sleeper()
        sleep(0.01)
        assert polls == [1], "the first sleep of a run feeds it"

        # rate-limited on a bench-wide clock: a run of many short settles is
        # not a stream of GPIB queries
        for _ in range(5):
            sleep(0.01)
        assert polls == [1]

        # once the interval has elapsed it polls again, exactly once: the
        # limiter still holds for the rest of that same sleep
        b._watchdog_at = _time.monotonic() - 1000.0
        sleep(0.01)
        assert polls == [1, 1]
    finally:
        t.close()

    # a console-backed 331 has no `poll_faults` and the bench must not care
    b = Bench.build_simulated(RigConfig(), fast=False)
    b.rig.temperature = ConsoleTemperatureController("http://x:8331")
    b.sleeper()(0.01)                       # must not raise


def test_a_loop_two_emergency_stop_drops_the_loop_to_open_loop(monkeypatch):
    """`MOUT 2,0` alone kills nothing: the manual output is only used in open
    loop, so a Loop 2 still in PID goes on driving the analog output from its
    setpoint. The console project's copy stopped after the zero, which its
    own docstring already contradicted."""
    t = _direct331(monkeypatch, control_loop=2)
    writes: list[str] = []
    try:
        t.device._write = lambda msg, reason="": writes.append(msg)
        t.device.emergency_stop("test")
        assert writes[0] == "RANGE 0"
        assert writes[1].startswith("MOUT 2,"), "zero before the mode change"
        assert writes[2] == "CMODE 2,%d" % int(p.ControlMode.OPEN_LOOP), (
            "switching to open loop first would drive a stale MOUT")
    finally:
        t.close()

    # loop 1 is the current rig: RANGE 0 is the whole kill path
    one = _direct331(monkeypatch)
    writes = []
    try:
        one.device._write = lambda msg, reason="": writes.append(msg)
        one.device.emergency_stop("test")
        assert writes == ["RANGE 0"]
    finally:
        one.close()


def test_a_watchdog_that_cannot_cut_says_so_instead_of_breaking_the_read(monkeypatch):
    """A watchdog that raised out of `read()` would take the reading with it,
    and the monitor would report a dead instrument rather than a live one
    with a bad sensor. The failure is recorded and the read still returns."""
    t = _direct331(monkeypatch)
    try:
        t.device.reading_status = lambda channel="A": p.parse_reading_status("16")

        def refuse(reason):
            raise TransportError("bus gone")

        t.device.emergency_stop = refuse
        for _ in range(3):
            r = t.read()
        assert r.connected is True, "the reading survived"
        assert t.heater_cut and "RANGE 0 failed" in t.heater_cut
    finally:
        t.close()


def test_notes_and_writes_reach_the_audit_hook(monkeypatch):
    """The console kept its own audit file because it was the only program on
    the instrument. Here the session journal is where an operator looks, so
    every write and every note goes to the hook the service installs."""
    seen: list[str] = []
    t = _direct331(monkeypatch, audit=seen.append)
    try:
        t.set_setpoint(250.0)
        assert any("SETP" in line and "BACE service" in line for line in seen)
        assert t.note("bace 20260903_101500-001 T=250K") is True
        assert seen[-1].endswith("T=250K")

        def explode(_):
            raise RuntimeError("journal closed")

        t.audit = explode
        t.set_setpoint(251.0)       # a broken hook must not break a write
        assert t.read().setpoint_k == pytest.approx(251.0)
    finally:
        t.close()


# ===========================================================================
# Which path a rig.toml selects, and what a reading is then called
# ===========================================================================

def test_provenance_separates_the_instrument_from_the_console_and_the_stand_in(
        monkeypatch):
    """`simulated` must never be the label on a measured number. The
    fall-through is last on purpose: a driver that is neither a URL nor a
    GPIB resource is a stand-in."""
    t = _direct331(monkeypatch)
    try:
        assert temperature_source(t) == "instrument"
    finally:
        t.close()
    assert temperature_source(ConsoleTemperatureController("http://x:8331")) == "console"
    assert temperature_source(object()) == "simulated"


def test_the_settle_path_and_the_bench_agree_on_where_a_temperature_came_from(
        monkeypatch):
    """There were two classifiers. The bench read-back used the newer,
    `resource`-aware one and the settle path -- the one whose answer reaches
    `RunMetadata.temperature_source` and the HDF5 -- still tested only
    `base_url`, so every reading off a real 331 was written down as
    `simulated`. They are one function now; this fails if a second appears."""
    from bace.service import temperature as svc_temperature

    t = _direct331(monkeypatch)
    try:
        for controller, expected in ((t, "instrument"),
                                     (ConsoleTemperatureController("http://x:8331"),
                                      "console"),
                                     (object(), "simulated")):
            assert svc_temperature.source_of(controller) == expected
            assert svc_temperature.source_of(controller) == temperature_source(controller)
    finally:
        t.close()


def test_the_wavelength_travels_with_every_direct_reading():
    """Watts mean nothing without the wavelength that set the responsivity.
    The console client got it free in each `/api/reading`; the direct adapter
    has to read it back and keep it, or the power monitor's events and the
    `read-power` action carry a number nobody can interpret."""
    m = DirectPowerMeter(simulate=True, wavelength_nm=530.0)
    try:
        assert m.wavelength_nm == 530.0
        assert m.read().wavelength_nm == 530.0
        m.set_wavelength(505.0)
        assert m.wavelength_nm == 505.0 and m.read().wavelength_nm == 505.0

        from bace.service.rigs import power_reading
        assert power_reading(m).wavelength_nm == 505.0
    finally:
        m.close()

    # a caller that sets none gets whatever the meter was left holding,
    # rather than None -- the meter is stateful and somebody set it once
    left = DirectPowerMeter(simulate=True)
    try:
        assert left.wavelength_nm is not None
        assert left.read().wavelength_nm == left.wavelength_nm
    finally:
        left.close()


def test_a_power_reading_is_never_labelled_simulated_when_it_is_not():
    """`power_reading` used to read `base_url or "simulated"`. The direct
    driver has no URL, so that fall-through would have stamped every real watt
    reading on the rig as simulated -- in the event stream, the journal and
    the file. It asks the meter for its own `source` first."""
    from bace.service.rigs import power_reading

    m = DirectPowerMeter(simulate=True)
    try:
        assert power_reading(m).source == "simulated"
        m.info["simulated"] = False
        assert power_reading(m).source == "usb"
    finally:
        m.close()

    class StandIn:                      # no source, no base_url
        def read_power(self):
            return 1e-4

    assert power_reading(StandIn()).source == "simulated"


def test_an_empty_console_opens_the_instrument_and_a_named_one_does_not(monkeypatch):
    """The whole point of the change: with `console` empty the service owns
    the device. Naming a console is the explicit escape hatch, and then the
    service must not open anything itself -- two owners is the failure the
    console existed to prevent in the first place."""
    monkeypatch.setattr(ls_controller, "open_transport",
                        lambda conn, simulated=False, simulate_loop=1: Sim331())
    direct = open_temperature_controller(
        RigConfig(temperature_address="GPIB0::9::INSTR",
                  temperature_max_setpoint_k=320.0))
    try:
        assert isinstance(direct, DirectTemperatureController)
        assert direct.resource == "GPIB0::9::INSTR"
        assert direct.read().max_setpoint_k == 320.0
    finally:
        direct.close()

    opened = []
    monkeypatch.setattr(ls_controller, "open_transport",
                        lambda *a, **k: opened.append(1))
    via = open_temperature_controller(
        RigConfig(temperature_console="http://127.0.0.1:8331"))
    assert isinstance(via, ConsoleTemperatureController)
    assert opened == [], "a named console means the bus is not ours to open"

    # and the third state: neither, meaning no cryostat on this bench
    with pytest.raises(TemperatureError, match="no 331 on this bench"):
        open_temperature_controller(RigConfig(temperature_address=""))
    assert opened == []


def test_a_bench_with_no_cryostat_records_no_reason_at_all(monkeypatch):
    """Clearing both `address` and `console` says there is no 331 here. That
    is a configuration, not a failure, so nothing is opened and nothing lands
    in `unavailable` -- an operator who cleared it does not need a line on the
    card telling them what they just did. A temperature node still pauses."""
    from bace.drivers.keithley2400 import SourceMeterConfig
    from bace.experiment.transient import RunConfig
    from bace.service.rigs import Bench

    monkeypatch.setattr(ls_controller, "open_transport",
                        lambda *a, **k: pytest.fail("nothing may be opened"))
    rig = RigConfig(temperature_address="", temperature_console="")
    b = Bench.build_real(rig, SourceMeterConfig(), run_config=RunConfig())
    try:
        assert b.rig.temperature is None
        assert "temperature" not in b.unavailable
        assert b.snapshot_stub()["temperature"]["wired"] is False
    finally:
        b.close()


def test_the_meter_path_is_chosen_the_same_way_and_units_are_set_either_way(
        monkeypatch):
    """Watts and the wavelength are set on whichever meter is returned: a
    meter left in amps or dBm reads plausibly and wrongly, and the wavelength
    decides the responsivity behind the number."""
    rig = RigConfig(power_meter_wavelength_nm=530.0)
    monkeypatch.setattr(np.PowerMeter, "open",
                        classmethod(lambda cls, dll=None, simulate=False:
                                    cls(np.SimulatedTransport())))
    m = open_power_meter(rig)
    try:
        assert isinstance(m, DirectPowerMeter)
        assert m._meter.query("PM:UNITS?") == "2"
        assert m._meter.query("PM:Lambda?") == "530"
    finally:
        m.close()

    calls: list[str] = []
    monkeypatch.setattr(ConsolePowerMeter, "available", lambda self: True)
    monkeypatch.setattr(ConsolePowerMeter, "set_units_watts",
                        lambda self: calls.append("units"))
    monkeypatch.setattr(ConsolePowerMeter, "set_wavelength",
                        lambda self, nm: calls.append(f"lambda {nm:g}"))
    monkeypatch.setattr(ConsolePowerMeter, "set_averaging",
                        lambda self, on=True, **kw: calls.append(f"averaging {on}") or True)
    via = open_power_meter(RigConfig(power_meter_console="http://127.0.0.1:8918"))
    assert isinstance(via, ConsolePowerMeter)
    assert calls == ["units", "lambda 530", "averaging True"]
    # The direct path set it too, and read it back off the (simulated) meter.
    assert m.averaged is True and m.averaging["analog_filter_name"] == "5 Hz"

    monkeypatch.setattr(ConsolePowerMeter, "available", lambda self: False)
    with pytest.raises(PowerMeterError, match="clear \\[power_meter\\] console"):
        open_power_meter(RigConfig(power_meter_console="http://127.0.0.1:8918"))


# ===========================================================================
# 1918-C: averaging, so a pulsed LED reads as a power (2026-09-05)
# ===========================================================================

def test_averaging_puts_the_meter_in_dc_continuous_with_the_5hz_filter():
    """The rig pulses the LED at 500 Hz, 50 % duty. A 1918-C sampling that
    square wave at one instant shows whichever phase it landed in -- the
    operator saw the rail flicker between the level and nothing. DC-continuous
    mode with the analog 5 Hz filter is what integrates over the cycles and
    reads the mean, half the DC level; that is what `open_power_meter` sets,
    and every reading says whether it was on."""
    m = DirectPowerMeter(simulate=True)
    try:
        assert m.averaged is None and m.read().averaged is None, "nothing claimed before it is set"
        got = m.set_averaging(True, digital_samples=100)
        assert m._meter.query("PM:MODE?") == "0"
        assert m._meter.query("PM:ANALOGFILTER?") == "4"
        assert m._meter.query("PM:FILT?") == "3"
        assert m._meter.query("PM:DIGITALFILTER?") == "100"
        assert got["mode_name"] == "DC continuous" and got["analog_filter_name"] == "5 Hz"
        assert got["filter_name"] == "analog + digital" and got["digital_filter"] == 100
        assert m.averaged is True
        assert m.read().averaged is True and m.last.averaged is True

        # Off: unfiltered, and still DC-continuous -- a meter someone left in
        # *pulse* on the front panel is wrong under both.
        m._meter.write("PM:MODE 5")
        m.set_averaging(False)
        assert m._meter.query("PM:MODE?") == "0"
        assert m._meter.query("PM:FILT?") == "0" and m._meter.query("PM:ANALOGFILTER?") == "0"
        assert m.averaged is False and m.read().averaged is False

        # Analog only when no digital samples are asked for.
        m.set_averaging(True, digital_samples=0)
        assert m._meter.query("PM:FILT?") == "1" and m.averaged is True
    finally:
        m.close()


def test_a_meter_that_does_not_take_the_filter_is_refused_not_trusted(monkeypatch):
    """`averaged` is what the meter *reports*, never what was asked: an
    instrument that ignores `PM:ANALOGFILTER` would otherwise be labelled as
    averaging while showing the flicker."""
    m = DirectPowerMeter(simulate=True)
    try:
        real = m._meter._t.write

        def deaf(command):
            if not command.upper().startswith("PM:ANALOGFILTER"):
                real(command)
        monkeypatch.setattr(m._meter._t, "write", deaf)
        with pytest.raises(PowerMeterError, match="did not take the averaging"):
            m.set_averaging(True)
        assert m.averaged is False
    finally:
        m.close()


def test_open_power_meter_sets_averaging_from_rig_toml(monkeypatch):
    monkeypatch.setattr(np.PowerMeter, "open",
                        classmethod(lambda cls, dll=None, simulate=False:
                                    cls(np.SimulatedTransport())))
    m = open_power_meter(RigConfig(power_meter_wavelength_nm=530.0))
    try:
        assert m.averaged is True and m.averaging["digital_filter"] == 100
        assert m._meter.query("PM:MODE?") == "0"
    finally:
        m.close()
    m = open_power_meter(RigConfig(power_meter_averaging=False, power_meter_digital_filter=0))
    try:
        assert m.averaged is False and m._meter.query("PM:FILT?") == "0"
        assert m._meter.query("PM:MODE?") == "0", "the mode is written either way"
    finally:
        m.close()


def test_the_console_client_says_when_it_could_not_set_averaging(monkeypatch):
    """The console's filter route is assumed, not verified. A console with no
    such route answers 404: reported as False and no reading claims the
    average; any other refusal is the console saying no, and is raised."""
    c = ConsolePowerMeter("http://127.0.0.1:8918")
    posted = []

    def missing(self, path, body):
        posted.append((path, body))
        raise PowerMeterError("the 1918-C console refused POST /api/filter with HTTP 404")
    monkeypatch.setattr(ConsolePowerMeter, "_post", missing)
    assert c.set_averaging(True) is False and c.averaged is None
    assert posted == [("/api/filter", {"filter": 3, "analogFilter": 4,
                                       "digitalFilter": 100, "mode": 0})]

    monkeypatch.setattr(ConsolePowerMeter, "_post", lambda self, path, body: {})
    assert c.set_averaging(True) is True and c.averaged is True
    monkeypatch.setattr(ConsolePowerMeter, "_get", lambda self, path: {
        "value": 2.0e-5, "units": 2, "wavelength": 530.0, "status": {}})
    assert c.read().averaged is True

    def refused(self, path, body):
        raise PowerMeterError("refused POST /api/filter with HTTP 400: bad body")
    monkeypatch.setattr(ConsolePowerMeter, "_post", refused)
    with pytest.raises(PowerMeterError, match="HTTP 400"):
        c.set_averaging(True)


def test_open_power_meter_warns_once_when_the_console_cannot_average(monkeypatch):
    import warnings
    monkeypatch.setattr(ConsolePowerMeter, "available", lambda self: True)
    monkeypatch.setattr(ConsolePowerMeter, "set_units_watts", lambda self: None)
    monkeypatch.setattr(ConsolePowerMeter, "set_wavelength", lambda self, nm: None)
    monkeypatch.setattr(ConsolePowerMeter, "set_averaging", lambda self, on=True, **kw: False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        open_power_meter(RigConfig(power_meter_console="http://127.0.0.1:8918"))
    assert len(caught) == 1 and "no /api/filter route" in str(caught[0].message)
