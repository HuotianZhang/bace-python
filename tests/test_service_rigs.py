"""The bench as the service owns it: read-back, chain, actions, assembly.

Transcript-style where it matters: a refused action is checked by asserting
the simulated instrument was told nothing, and a relay move by reading the
simulated relay, not by the absence of an exception.
"""
from __future__ import annotations

import io
import json
import sys
import types
import urllib.error
import urllib.request

import pytest

from bace.bench import checks
from bace.drivers.keithley2400 import SourceMeterConfig
from bace.drivers.newport1918c import ConsolePowerMeter
from bace.experiment.rig import RigConfig
from bace.experiment.transient import RunConfig
from bace.service import rigs
from bace.service.rigs import Bench, BenchActionRefused

INSTRUMENT_KEYS = {"relay", "bias", "smu", "shutter", "led", "voc", "power", "temperature"}
RIG_VALUE_KEYS = {"sense_resistor_ohm", "pulse_amp", "current_sign", "trigger_offset_s",
                  "light_path_delay_ns", "probe_attenuation", "led_threshold_v",
                  "max_current_compliance_a", "max_voltage_compliance_v"}


def bench(**kw) -> Bench:
    return Bench.build_simulated(RigConfig(), seed=1, **kw)


def chain_items(snapshot: dict) -> dict[str, dict]:
    return {i["key"]: i for i in snapshot["chain"]["items"]}


# -- read-back ----------------------------------------------------------------
def test_build_simulated_reads_back_every_key_with_sane_values():
    b = bench()
    assert b.mode == "sim" and b.rig.router is not None and b.unavailable == {}
    snap = b.read_back()
    inst = snap["instruments"]
    assert set(inst) == INSTRUMENT_KEYS

    assert inst["relay"] == {"position": "amplifier", "how": "readback"}
    assert inst["bias"]["output"] is False and inst["bias"]["polarity"] == "?"
    assert inst["bias"]["arm_source"] == "IMM" and inst["bias"]["arm_slope"] == "POS"
    assert inst["bias"]["high_v"] == 0.0 and inst["bias"]["frequency_hz"] == 1000.0
    assert inst["smu"]["output"] is False
    assert inst["smu"]["compliance"] == {"current_a": 0.05, "voltage_v": None}
    assert inst["smu"]["ceiling"] == {"current_a": 0.05, "voltage_v": 5.0}
    assert inst["shutter"] == {"open": False, "how": "readback"}
    assert inst["led"]["output"] is False and inst["led"]["polarity"] == "NORM"
    assert inst["led"]["mode"] == "OFF" and inst["led"]["high_v"] is None
    assert inst["voc"] == {"value": None}
    assert inst["power"]["available"] is True and inst["power"]["watts"] >= 0.0
    assert inst["power"]["trustworthy"] is True and inst["power"]["monitor"] is False
    assert inst["power"]["wavelength_nm"] == 530.0
    assert inst["temperature"] == {"wired": False, "kelvin": None, "setpoint_k": None,
                                   "in_band": None, "source": None}

    assert set(snap["rig"]["values"]) == RIG_VALUE_KEYS
    assert snap["rig"]["values"]["current_sign"] == -1.0
    assert len(snap["rig"]["fingerprint"]) == 12
    assert snap["chain"]["total"] == 4 and snap["read_at"] <= snap["chain"]["read_at"] + 1
    assert snap["unavailable"] == {}
    json.dumps(snap)                                   # what /bench serves


def test_the_simulator_takes_the_rigs_constants_and_sign():
    cfg = RigConfig(current_sign=1.0, sense_resistor_ohm=50.0, pulse_amp=2.0)
    b = Bench.build_simulated(cfg)
    assert b.sim.scope.current_sign == 1.0
    assert b.sim.bench.sense_resistor_ohm == 50.0 and b.sim.bench.pulse_amp == 2.0
    assert b.rig.config is cfg


def test_levels_and_frequency_show_once_something_was_set():
    b = bench()
    b.sim.led.set_pulse(1.02, 0.4, frequency_hz=500.0, duty_percent=50.0)
    b.sim.bias.set_levels(0.25, -0.25, delay_s=9e-8, width_s=5e-6)
    inst = b.read_back()["instruments"]
    assert (inst["led"]["high_v"], inst["led"]["low_v"], inst["led"]["frequency_hz"]) == (1.02, 0.4, 500.0)
    assert inst["led"]["mode"] == "PULSE"
    assert (inst["bias"]["high_v"], inst["bias"]["low_v"]) == (0.25, -0.25)


def test_the_voc_source_is_rendered_when_the_session_hands_one_in():
    class Src:
        value, led_v, run_id, node_path, how, ts = 0.906, 1.02, "s-001", "jv_bace", "jv_bace", 1.0

    voc = bench().read_back(voc=Src())["instruments"]["voc"]
    assert voc == {"value": 0.906, "led_v": 1.02,
                   "from": {"run_id": "s-001", "node_path": "jv_bace", "ts": 1.0,
                            "how": "jv_bace"}}


# -- the chain --------------------------------------------------------------------
def test_the_chain_expects_inv_on_the_33220a_and_external_arming():
    """A cold simulated bench is NORM and IMM, exactly what the real one was
    found at (2026-08-31) -- and exactly the state a run inherits when nobody
    checks. The chain has to say so, with the fix beside it."""
    b = bench()
    items = chain_items(b.read_back())
    led = items["led_polarity"]
    assert (led["value"], led["expected"], led["level"], led["fix"]) == \
        ("NORM", "INV", "warn", "set-33220a-pol-inv")
    assert "during illumination" in led["text"]
    arm = items["bias_arm"]
    assert (arm["value"], arm["expected"], arm["level"], arm["fix"]) == \
        ("IMM", "EXT", "warn", "arm-81150a-ext")
    slope = items["bias_arm_slope"]
    assert (slope["value"], slope["expected"], slope["level"]) == ("POS", "POS", "ok")
    pol = items["bias_polarity"]
    assert pol["value"] == "?" and pol["level"] == "info" and pol["fix"] is None
    assert b.read_back()["chain"]["ok"] == 1

    # the verdicts the read-back implies: one per item that is not ok
    codes = [v["code"] for v in b.read_back()["verdicts"]]
    assert codes == ["chain.led-polarity", "chain.bias-arm", "chain.bias-polarity"]
    assert all(v["level"] in ("warn", "info") for v in b.read_back()["verdicts"])


def test_the_chain_reads_ok_once_the_operator_fixed_it_and_a_run_set_the_shape():
    b = bench()
    b.action("set-33220a-pol-inv", {})
    b.action("arm-81150a-ext", {})
    b.sim.bias.configure_shape(500.0, inverted_output=True)      # what a run does
    snap = b.read_back(run_config=RunConfig(output_polarity="INV"))
    assert snap["chain"]["ok"] == 4 and snap["verdicts"] == []
    items = chain_items(snap)
    assert items["bias_polarity"] == {"key": "bias_polarity", "label": "81150A POL",
                                      "value": "INV", "expected": "INV", "level": "ok",
                                      "fix": None, "text": items["bias_polarity"]["text"]}


def test_bias_polarity_expectation_follows_the_recipe():
    """NORM expected against INV read is a warn; `leave` expects nothing and
    shows the value at `info`, because the recipe deliberately did not say."""
    b = bench()
    b.sim.bias.configure_shape(500.0, inverted_output=True)
    norm = chain_items(b.read_back(run_config=RunConfig(output_polarity="NORM")))["bias_polarity"]
    assert (norm["value"], norm["expected"], norm["level"]) == ("INV", "NORM", "warn")
    leave = chain_items(b.read_back(run_config=RunConfig(output_polarity="leave")))["bias_polarity"]
    assert (leave["value"], leave["expected"], leave["level"]) == ("INV", "leave", "info")
    # auto follows inverted_output
    auto = chain_items(b.read_back(run_config=RunConfig(inverted_output=True)))["bias_polarity"]
    assert (auto["expected"], auto["level"]) == ("INV", "ok")
    free = chain_items(b.read_back(run_config=RunConfig(external_trigger=False)))["bias_arm"]
    assert (free["expected"], free["level"], free["fix"]) == ("IMM", "ok", None)


def test_read_back_writes_to_no_instrument():
    b = bench()
    sim = b.sim
    sim.led.set_pulse(1.02, 0.4)
    sim.led.enable_output(True)
    sim.bias.enable_output(True)
    before = (sim.bench.led_mode, sim.bench.led_drive_v, sim.led.output_enabled,
              sim.bench.bias_output, sim.bench.shutter_open, sim.bench.relay,
              sim.led.polarity(), sim.bias.trigger_state(), sim.bench.shots)
    b.read_back()
    after = (sim.bench.led_mode, sim.bench.led_drive_v, sim.led.output_enabled,
             sim.bench.bias_output, sim.bench.shutter_open, sim.bench.relay,
             sim.led.polarity(), sim.bias.trigger_state(), sim.bench.shots)
    assert after == before


# -- actions ------------------------------------------------------------------------
def test_an_action_is_refused_while_the_relevant_output_is_on():
    b = bench()
    sim = b.sim
    told: list = []
    real = sim.led.set_polarity
    sim.led.set_polarity = lambda inverted: (told.append(inverted), real(inverted))

    sim.led.set_pulse(1.02, 0.4)
    sim.led.enable_output(True)
    with pytest.raises(BenchActionRefused) as exc:
        b.action("set-33220a-pol-inv", {})
    assert exc.value.level == "warn" and "led-off" in exc.value.text
    assert told == [] and sim.led.polarity() == "NORM"

    sim.bias.enable_output(True)
    with pytest.raises(BenchActionRefused, match="bias-off"):
        b.action("arm-81150a-ext", {})
    assert sim.bias.trigger_state()["arm_source"] == "IMM"

    # off again, and the same requests go through
    b.action("led-off", {})
    b.action("bias-off", {})
    assert b.action("set-33220a-pol-inv", {}) == {"polarity": "INV"}
    assert told == [True]
    assert b.action("arm-81150a-ext", {}) == {"arm_source": "EXT", "arm_slope": "POS"}
    assert b.action("set-33220a-pol-norm", {}) == {"polarity": "NORM"}


def test_relay_actions_move_the_simulated_router_through_the_interlock():
    b = bench()
    sim = b.sim
    assert sim.bench.relay == "amplifier"
    assert b.action("relay-to-sourcemeter", {}) == {"position": "sourcemeter"}
    assert sim.bench.relay == "sourcemeter"

    sim.smu.enable_output(True)
    with pytest.raises(BenchActionRefused) as exc:
        b.action("relay-to-amplifier", {})
    assert exc.value.level == "crit" and "SourceMeter" in exc.value.text
    assert sim.bench.relay == "sourcemeter", "the interlock must have held"

    sim.smu.disable_output()
    assert b.action("relay-to-amplifier", {}) == {"position": "amplifier"}
    assert sim.bench.relay == "amplifier"
    assert b.read_back()["instruments"]["relay"]["position"] == "amplifier"


def test_set_led_pulse_takes_the_bace_params_in_force_and_refuses_bad_levels():
    b = bench()
    sim = b.sim
    params = {"led_v": 1.02, "led_low_v": 0.4, "pulse_frequency_hz": 500.0,
              "duty_percent": 50.0}
    result = b.action("set-led-pulse", {}, led_params=params)
    assert result == {"mode": "PULSE", "high_v": 1.02, "low_v": 0.4, "frequency_hz": 500.0,
                      "duty_percent": 50.0, "output": True}
    assert sim.bench.led_mode == "PULSE" and sim.bench.led_drive_v == 1.02
    assert sim.led.output_enabled and sim.led.frequency_hz == 500.0

    with pytest.raises(BenchActionRefused, match="threshold"):
        b.action("set-led-pulse", {"low": 1.2}, led_params=params)
    assert sim.led.last_levels == (1.02, 0.4), "a refused request changes nothing"
    with pytest.raises(ValueError, match="unknown argument"):
        b.action("set-led-pulse", {"lvl": 1.0}, led_params=params)
    with pytest.raises(ValueError, match="needs level"):
        b.action("set-led-pulse", {}, led_params={})

    assert b.action("set-led-dc", {"level": 1.04}) == {"mode": "DC", "level_v": 1.04,
                                                       "output": True}
    assert sim.bench.led_mode == "DC" and sim.bench.led_drive_v == 1.04
    assert b.action("led-off", {}) == {"output": False}
    assert not sim.led.output_enabled and sim.bench.led_mode == "OFF"


def test_park_shutter_power_and_unknown_actions():
    b = bench()
    sim = b.sim
    sim.bias.enable_output(True)
    sim.smu.enable_output(True)
    sim.shutter.unblock()
    assert b.action("park", {}) == {"parked": True}
    assert not sim.bench.bias_output and not sim.bench.smu_output
    assert not sim.bench.shutter_open

    assert b.action("shutter-open", {}) == {"open": True} and sim.bench.shutter_open
    assert b.action("shutter-shut", {}) == {"open": False} and not sim.bench.shutter_open
    assert b.action("smu-off", {}) == {"output": False}

    r = b.action("read-power", {})
    assert r["watts"] >= 0.0 and r["trustworthy"] is True and r["source"] == "simulated"
    assert r["wavelength_nm"] == 530.0
    with pytest.raises(KeyError):
        b.action("frobnicate", {})
    with pytest.raises(ValueError, match="unknown argument"):
        b.action("park", {"hard": True})


def test_a_silent_console_is_a_warn_not_a_crash():
    b = bench()

    def dead():
        raise RuntimeError("console gone")

    b.rig.power.read_power = dead
    with pytest.raises(BenchActionRefused) as exc:
        b.action("read-power", {})
    assert exc.value.level == "warn" and "console gone" in exc.value.text
    snap = b.read_back()
    assert snap["instruments"]["power"]["available"] is False
    assert "console gone" in snap["instruments"]["power"]["reason"]
    assert any(v["code"] == "power.console" for v in snap["verdicts"])


def test_shot_diagnostics_and_the_snapshot_stub():
    b = bench()
    assert b.shot_diagnostics() == {"autorange_passes": 0, "autorange_clipped": False}
    stub = b.snapshot_stub()
    assert set(stub) == INSTRUMENT_KEYS
    assert stub["relay"] == {"position": "unknown", "how": "cached"}
    assert stub["bias"]["output"] is None and stub["bias"]["polarity"] == "?"
    assert stub["smu"]["ceiling"] == {"current_a": 0.05, "voltage_v": 5.0}
    assert stub["voc"] == {"value": None}
    assert stub["power"]["available"] is None
    json.dumps(stub)
    assert b.sleep(0.0) is None and b.fast is True
    # not `is time.sleep` any more: every non-fast sleep is the worker's own
    # chance to feed the 331 watchdog, so it goes through `sleeper`'s closure
    # whether or not anything can interrupt it (`_feed_watchdog`)
    import time
    slow = Bench.build_simulated(RigConfig(), fast=False)
    began = time.monotonic()
    slow.sleep(0.05)
    assert time.monotonic() - began >= 0.04, "a non-fast sleep really sleeps"


# -- the temperature console --------------------------------------------------------
class _Reply:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_naming_a_console_attaches_the_simulated_331_and_the_read_back_reports_it():
    """`wired` means a controller is attached. Under `--sim` any console name
    attaches the stand-in; the block then carries what the console reports
    (connected, ramping, heater range, ceiling) beside the contract's keys."""
    b = Bench.build_simulated(RigConfig(temperature_console="sim"))
    assert b.rig.temperature is b.sim.temperature and b.unavailable == {}
    t = b.read_back()["instruments"]["temperature"]
    assert t == {"wired": True, "kelvin": 294.8, "setpoint_k": 294.8, "in_band": None,
                 "source": "simulated", "connected": True, "ramping": False,
                 "heater_range": 3, "status_text": "ok", "max_setpoint_k": 350.0,
                 "console": "sim"}
    assert b.snapshot_stub()["temperature"]["wired"] is True

    # the console up, the instrument behind it silent: wired and not connected
    b.sim.temperature.connected = False
    t = b.read_back()["instruments"]["temperature"]
    assert t["wired"] is True and t["connected"] is False and t["kelvin"] == 294.8
    assert t["status_text"] == "the instrument is not answering"

    # the default rig.toml names none: the contract's unwired block, as before
    plain = bench()
    assert plain.rig.temperature is None
    assert plain.read_back()["instruments"]["temperature"] == {
        "wired": False, "kelvin": None, "setpoint_k": None, "in_band": None, "source": None}
    assert plain.snapshot_stub()["temperature"]["wired"] is False


def test_a_console_url_is_read_through_the_331_driver(monkeypatch):
    """The monitor's path: a URL becomes a `ConsoleTemperatureController`
    read, and a console that does not answer is wired-but-silent with the
    error rather than unwired."""
    asked = []

    def fake_urlopen(req, timeout=None):
        asked.append((req.full_url, timeout))
        return _Reply({"connected": True, "control_temperature": 249.9, "temperature_a": 249.9,
                       "temperature_b": 251.3, "setpoint": 250.0, "ramping": True,
                       "heater_range": 2, "status_text": "ok", "elapsed_s": 12.0,
                       "max_setpoint_k": 350.0})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    t = rigs.read_temperature_console("http://127.0.0.1:8331/")
    assert t == {"wired": True, "kelvin": 249.9, "setpoint_k": 250.0, "in_band": None,
                 "source": "console", "connected": True, "ramping": True, "heater_range": 2,
                 "status_text": "ok", "max_setpoint_k": 350.0}
    assert asked == [("http://127.0.0.1:8331/api/state", 2.0)]

    def silent(req, timeout=None):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", silent)
    t = rigs.read_temperature_console("http://127.0.0.1:8331")
    assert t["wired"] is True and t["kelvin"] is None and t["connected"] is False
    assert t["error"].startswith("TemperatureError: cannot reach the 331 console")
    assert rigs.read_temperature_console("") == {"wired": False, "kelvin": None, "setpoint_k": None,
                                                  "in_band": None, "source": None}


# -- build_real, with no VISA at all --------------------------------------------------
def _no_meter(monkeypatch):
    """Keep the real 1918-C out of the assembly. `build_real` opens the USB
    device itself since 2026-09-03, so on a PC with the Newport driver and
    the meter attached these tests would open it and write its units and
    wavelength -- the lab PC did, 2026-09-04. Both routes are shut: the
    console answers nothing, and the direct open fails the way a desk with
    no driver fails."""
    from bace.drivers import newport1918c

    class NoMeter:
        def __init__(self, *a, **k):
            raise newport1918c.PowerMeterError(
                "usbdll.dll not found (or none matching this 64-bit Python)")

    monkeypatch.setattr(ConsolePowerMeter, "available", lambda self: False)
    monkeypatch.setattr(newport1918c, "DirectPowerMeter", NoMeter)


def _fake_visa(monkeypatch, rig: RigConfig, *, with_smu: bool):
    # `tests/` has no __init__.py, so pytest imports its modules by basename.
    from test_bench import FakeInstrument, FakeRM

    table = {rig.scope_address: FakeInstrument("KEYSIGHT,DSO9054H,MY123,06.20"),
             rig.bias_address: FakeInstrument("Agilent Technologies,81150A,MY5,1.0"),
             rig.led_address: FakeInstrument("Agilent Technologies,33220A,MY4,2.0")}
    if with_smu:
        table[rig.sourcemeter_address] = FakeInstrument(
            "KEITHLEY INSTRUMENTS INC.,MODEL 2400,4473504,C34")
    monkeypatch.setitem(sys.modules, "pyvisa",
                        types.SimpleNamespace(ResourceManager=lambda: FakeRM(table)))
    monkeypatch.setattr(checks, "_dio_backend",
                        lambda rig_config: (None, "", "no DELIB this interpreter can load"))
    _no_meter(monkeypatch)
    return table


def test_build_real_hands_the_digitiser_the_rigs_sign_and_records_what_is_missing(monkeypatch):
    """The assembly is where the sense resistor, the sign and the attenuation
    reach the digitiser. A bench with no Keithley and no DELIB must still come
    up, say what is missing, and read back without crashing."""
    from bace.drivers.agilent33220a import Agilent33220A
    from bace.drivers.agilent81150 import Agilent81150
    from bace.drivers.infiniium import Infiniium

    rig = RigConfig(current_sign=1.0, sense_resistor_ohm=50.0, probe_attenuation=10.0)
    table = _fake_visa(monkeypatch, rig, with_smu=False)
    b = Bench.build_real(rig, SourceMeterConfig(current_compliance_a=0.01),
                         run_config=RunConfig(), rig_path="rig.toml")

    assert b.mode == "rig" and b.sim is None
    scope = b.rig.scope
    assert isinstance(scope, Infiniium)
    assert (scope.current_sign, scope.sense_resistor_ohm, scope.probe_attenuation) == (1.0, 50.0, 10.0)
    assert table[rig.scope_address].log, "default_setup must have run on the scope"
    assert isinstance(b.rig.bias, Agilent81150) and isinstance(b.rig.led, Agilent33220A)
    # each driver sets its own VISA timeout in its constructor, as on the rig
    assert table[rig.scope_address].timeout == 20000
    assert table[rig.bias_address].timeout == table[rig.led_address].timeout == 5000

    assert b.rig.smu is None and rig.sourcemeter_address in b.unavailable["smu"]
    assert b.rig.router is None and "DELIB" in b.unavailable["relay"]
    assert isinstance(b.rig.shutter, rigs.Unavailable) and "DELIB" in b.unavailable["shutter"]
    # nothing named a console, so the bench tried to open the meter itself
    assert b.rig.power is None and "usbdll.dll not found" in b.unavailable["power"]
    assert b.rig.temperature is None and "temperature" in b.unavailable

    snap = b.read_back()
    inst = snap["instruments"]
    assert inst["smu"]["output"] is None and inst["relay"]["how"] == "unavailable"
    assert inst["shutter"] == {"open": None, "how": "cached"}
    assert inst["power"]["available"] is False
    assert inst["bias"]["arm_source"] == "0"          # the fake answers "0": passed through
    assert set(snap["unavailable"]) == {"smu", "shutter", "relay", "power",
                                        "temperature"}
    assert snap["rig"]["path"] == "rig.toml" and snap["rig"]["values"]["current_sign"] == 1.0

    with pytest.raises(BenchActionRefused, match="no smu"):
        b.action("smu-off", {})
    with pytest.raises(BenchActionRefused, match="no router"):
        b.action("relay-to-sourcemeter", {})
    with pytest.raises(rigs.BenchUnavailable, match="shutter is not available"):
        b.rig.shutter.unblock()
    assert b.shot_diagnostics() == {"autorange_passes": 0, "autorange_clipped": False}
    b.close()
    assert "<close>" in table[rig.scope_address].log


def test_build_real_gives_the_keithley_the_recipes_compliance(monkeypatch):
    from bace.drivers.keithley2400 import Keithley2400

    rig = RigConfig()
    _fake_visa(monkeypatch, rig, with_smu=True)
    smu_cfg = SourceMeterConfig(current_compliance_a=0.01, voltage_compliance_v=2.0)
    b = Bench.build_real(rig, smu_cfg, run_config=RunConfig())
    assert isinstance(b.rig.smu, Keithley2400) and b.rig.smu.config is smu_cfg
    assert "smu" not in b.unavailable
    smu = b.read_back()["instruments"]["smu"]
    assert smu["compliance"] == {"current_a": 0.01, "voltage_v": 2.0}
    assert smu["output"] is False
    b.close()


def test_build_real_opens_the_331_itself_and_uses_a_console_only_when_named(
        monkeypatch):
    """Who owns the GPIB session, and what the card says when nobody does.

    Default: this process opens `[temperature] address` and a loop settles
    through the driver -- the arrangement the rig actually has, since nobody
    starts the 331 console. Named console: that program owns the bus and the
    service asks it over HTTP instead. Neither answering: `rig.temperature`
    is None and the reason is on the card, which is the difference between
    "no cryostat on this bench" and "go and start something".
    """
    from bace.drivers.lakeshore331 import (ConsoleTemperatureController,
                                           DirectTemperatureController)
    from bace.drivers.lakeshore331 import controller as ctl
    from bace.drivers.lakeshore331.transport import SimulatedTransport

    # -- 1. no console named: the service opens the instrument ---------------
    rig = RigConfig()
    _fake_visa(monkeypatch, rig, with_smu=True)
    monkeypatch.setattr(ctl, "open_transport",
                        lambda conn, simulated=False, simulate_loop=1: SimulatedTransport())
    b = Bench.build_real(rig, SourceMeterConfig(), run_config=RunConfig())
    assert isinstance(b.rig.temperature, DirectTemperatureController)
    assert b.rig.temperature.resource == "GPIB0::7::INSTR"
    assert "temperature" not in b.unavailable
    assert rigs.temperature_source(b.rig.temperature) == "instrument", (
        "a reading this process took off the bus is measured, not simulated")
    # the ceiling travels with the driver, so a 400 K node is refused here
    assert b.rig.temperature.settings.limits.max_setpoint_k == 350.0
    assert any("331 GPIB0::7::INSTR" in w for w in b.startup_writes)
    b.close()

    # -- 2. no console named, no instrument either --------------------------
    monkeypatch.setattr(ctl, "open_transport", _raises_transport_error)
    b = Bench.build_real(rig, SourceMeterConfig(), run_config=RunConfig())
    assert b.rig.temperature is None
    assert "temperature loops pause for a manual set" in b.unavailable["temperature"]
    assert b.snapshot_stub()["temperature"]["wired"] is False
    b.close()

    # -- 3. a console named and answering: it owns the bus, we ask it -------
    console = RigConfig(temperature_console="http://127.0.0.1:8331")
    _fake_visa(monkeypatch, console, with_smu=True)
    monkeypatch.setattr(ConsoleTemperatureController, "probe",
                        lambda self: {"connected": True, "max_setpoint_k": 350.0})
    b = Bench.build_real(console, SourceMeterConfig(), run_config=RunConfig())
    assert isinstance(b.rig.temperature, ConsoleTemperatureController)
    assert b.rig.temperature.base_url == "http://127.0.0.1:8331"
    assert (b.rig.temperature.timeout_s, b.rig.temperature.write_timeout_s) == (3.0, 10.0)
    assert "temperature" not in b.unavailable
    assert b.snapshot_stub()["temperature"] == {
        "wired": True, "kelvin": None, "setpoint_k": None, "in_band": None,
        "source": "console", "connected": None}, "attached, not read back yet"
    b.close()

    # -- 4. a console named and silent --------------------------------------
    monkeypatch.setattr(ConsoleTemperatureController, "probe", lambda self: None)
    b = Bench.build_real(console, SourceMeterConfig(), run_config=RunConfig())
    assert b.rig.temperature is None
    assert b.unavailable["temperature"].startswith(
        "the 331 console is not answering at http://127.0.0.1:8331")
    assert "clear [temperature] console" in b.unavailable["temperature"], (
        "the fix is now to stop naming it, not to go and start it")
    t = b.read_back()["instruments"]["temperature"]
    assert t["wired"] is False and t["kelvin"] is None
    assert t["console"] == "http://127.0.0.1:8331"
    assert t["reason"] == b.unavailable["temperature"]
    b.close()

    # -- 5. the console up before its first successful poll ------------------
    # `{"connected": false}`, no ceiling yet: the program is running, so
    # telling the operator to start anything would be wrong.
    monkeypatch.setattr(ConsoleTemperatureController, "probe",
                        lambda self: {"connected": False})
    b = Bench.build_real(console, SourceMeterConfig(), run_config=RunConfig())
    assert b.rig.temperature is None
    assert b.unavailable["temperature"].startswith(
        "the 331 console at http://127.0.0.1:8331 is up but has not read its "
        "instrument yet")
    b.close()


def _raises_transport_error(connection, simulated=False, simulate_loop=1):
    from bace.drivers.lakeshore331.transport import TransportError
    raise TransportError("could not open GPIB0::7::INSTR: no such resource")

def test_build_real_reads_the_outputs_and_levels_from_the_instruments(monkeypatch):
    """The blocker of 2026-09-02: a generator left ON by the LabVIEW VI was
    invisible, because the read-back reported the drivers' cached flags --
    False in every constructor -- and the relay interlock read the same
    flags. The read-back now asks the instruments, the cached flags follow,
    and the levels and the 81150A's polarity are real before any run."""
    from test_bench import FakeInstrument, FakeRM

    rig = RigConfig()
    table = {rig.scope_address: FakeInstrument("KEYSIGHT,DSO9054H,MY123,06.20"),
             rig.bias_address: FakeInstrument("Agilent Technologies,81150A,MY5,1.0",
                                              outputs_on=True),
             rig.led_address: FakeInstrument("Agilent Technologies,33220A,MY4,2.0",
                                             outputs_on=True),
             rig.sourcemeter_address: FakeInstrument(
                 "KEITHLEY INSTRUMENTS INC.,MODEL 2400,4473504,C34", outputs_on=True)}
    # what the front panel holds: the VI's pulse on the LED, INV on the 81150A
    table[rig.led_address].write("FUNC:SHAP PULS;:FREQ 500;:VOLT:HIGH 1.02;:VOLT:LOW 0.4;"
                                 ":OUTP:POL NORM;")
    table[rig.bias_address].write(":OUTP1:POL INV;:VOLT1:HIGH 0.25;:VOLT1:LOW -0.25;:FREQ1 500;")
    monkeypatch.setitem(sys.modules, "pyvisa",
                        types.SimpleNamespace(ResourceManager=lambda: FakeRM(table)))
    monkeypatch.setattr(checks, "_dio_backend",
                        lambda rig_config: (None, "", "no DELIB this interpreter can load"))
    _no_meter(monkeypatch)
    b = Bench.build_real(rig, SourceMeterConfig(), run_config=RunConfig())

    assert b.rig.bias.output_enabled and b.rig.led.output_enabled and b.rig.smu.output_enabled, (
        "the flags the interlock reads were seeded from the instruments at assembly")
    assert b.rig.bias.output_polarity == "INV"
    inst = b.read_back()["instruments"]
    assert inst["bias"]["output"] is True and inst["bias"]["polarity"] == "INV"
    assert (inst["bias"]["high_v"], inst["bias"]["low_v"], inst["bias"]["frequency_hz"]) == \
        (0.25, -0.25, 500.0)
    assert inst["bias"]["polarity_read"] is True
    assert inst["led"]["output"] is True and inst["led"]["mode"] == "PULSE"
    assert (inst["led"]["high_v"], inst["led"]["low_v"], inst["led"]["frequency_hz"]) == \
        (1.02, 0.4, 500.0)
    assert inst["led"]["polarity"] == "NORM" and inst["smu"]["output"] is True
    assert b.startup_writes == (f"scope {rig.scope_address}: default_setup",)

    # the chain fix is refused against the *instrument's* state, and the
    # 81150A's polarity is judged, not "not read yet"
    with pytest.raises(BenchActionRefused, match="output is ON"):
        b.action("set-33220a-pol-inv", {})
    items = chain_items(b.read_back(run_config=RunConfig(output_polarity="NORM")))
    assert (items["bias_polarity"]["value"], items["bias_polarity"]["level"]) == ("INV", "warn")

    # switched off by hand at the front panel: the next read-back sees it
    for res in (table[rig.bias_address], table[rig.led_address], table[rig.sourcemeter_address]):
        res.outputs_on = False
    inst = b.read_back()["instruments"]
    assert inst["bias"]["output"] is False and not b.rig.bias.output_enabled
    assert inst["led"]["output"] is False and inst["led"]["mode"] == "OFF"
    assert b.action("set-33220a-pol-inv", {}) == {"polarity": "INV"}
    b.close()


def test_a_bench_with_no_visa_comes_up_with_the_visa_roles_unavailable(monkeypatch):
    """Two ways to have no VISA -- pyvisa not installed, or installed with no
    backend behind it -- and one answer: the service comes up, the four VISA
    roles say why, and the DIO lines, the meter and the 331 are still tried."""
    monkeypatch.setitem(sys.modules, "pyvisa", None)              # ImportError
    monkeypatch.setattr(checks, "_dio_backend",
                        lambda rig_config: (None, "", "no DELIB this interpreter can load"))
    _no_meter(monkeypatch)
    rig = RigConfig()
    b = Bench.build_real(rig, SourceMeterConfig(), run_config=RunConfig())
    assert set(b.unavailable) == {"scope", "bias", "led", "smu", "shutter", "relay",
                                  "power", "temperature"}
    for role in ("scope", "bias", "led", "smu"):
        assert "pyvisa is not installed" in b.unavailable[role]
        assert "[rig]" in b.unavailable[role], "the fix is named"
    assert isinstance(b.rig.bias, rigs.Unavailable) and b.rig.smu is None
    snap = b.read_back()
    assert snap["instruments"]["bias"]["output"] is None and snap["instruments"]["smu"]["output"] is None
    json.dumps(snap)
    b.close()

    def no_backend():
        raise ValueError("Could not locate a VISA implementation. Install either the NI "
                         "binary or pyvisa-py.")

    monkeypatch.setitem(sys.modules, "pyvisa", types.SimpleNamespace(ResourceManager=no_backend))
    b = Bench.build_real(rig, SourceMeterConfig(), run_config=RunConfig())
    assert "Could not locate a VISA implementation" in b.unavailable["bias"]
    assert "pyvisa-py" in b.unavailable["scope"]
    assert b.startup_writes == ()
    b.close()


def test_a_relay_on_module_zero_is_an_unavailable_router_not_a_traceback(monkeypatch):
    from test_bench import FakeInstrument, FakeRM

    class Line:
        def __init__(self, nr):
            self.nr = nr

        def open(self):
            return self

        def set_line(self, v):
            pass

        def read_line(self):
            return 0

        def release(self):
            pass

        def close(self):
            pass

    rig = RigConfig(shutter_module_nr=1, relay_module_nr=0)      # RigConfig itself allows it
    table = {rig.scope_address: FakeInstrument("KEYSIGHT,DSO9054H,MY123,06.20"),
             rig.bias_address: FakeInstrument("Agilent Technologies,81150A,MY5,1.0"),
             rig.led_address: FakeInstrument("Agilent Technologies,33220A,MY4,2.0")}
    monkeypatch.setitem(sys.modules, "pyvisa",
                        types.SimpleNamespace(ResourceManager=lambda: FakeRM(table)))
    monkeypatch.setattr(checks, "_dio_backend", lambda rig_config: (Line, "direct", ""))
    _no_meter(monkeypatch)
    b = Bench.build_real(rig, SourceMeterConfig(), run_config=RunConfig())
    assert b.rig.router is None and "module 0 is the shutter" in b.unavailable["relay"]
    assert "shutter" not in b.unavailable
    assert b.read_back()["instruments"]["relay"] == {"position": "unknown", "how": "unavailable"}
    b.close()


def test_the_relay_move_reads_both_sources_before_the_interlock_decides():
    """`BiasRouter` reads each driver's cached flag. On the real rig that
    flag is refreshed by `read_output`; a source that will not answer is a
    refusal, because unproven is not off."""
    class Source:
        def __init__(self, answers):
            self.answers = list(answers)
            self.output_enabled = False
            self.asked = 0

        def read_output(self):
            self.asked += 1
            a = self.answers.pop(0)
            if a is not None:
                self.output_enabled = a
            return a

        def disable_output(self):
            self.output_enabled = False

    b = bench()
    sim = b.sim
    live = Source([True, False])
    b.rig.smu = live
    sim.router._smu = live
    assert sim.bench.relay == "amplifier"
    with pytest.raises(BenchActionRefused) as exc:
        b.action("relay-to-sourcemeter", {})
    assert exc.value.level == "crit" and "SourceMeter" in exc.value.text
    assert live.asked == 1 and sim.bench.relay == "amplifier", "read, refused, nothing moved"
    assert b.action("relay-to-sourcemeter", {}) == {"position": "sourcemeter"}

    b.rig.smu = Source([None])
    with pytest.raises(BenchActionRefused, match="did not answer"):
        b.action("relay-to-amplifier", {})
    assert sim.bench.relay == "sourcemeter"


def test_action_before_names_the_fields_an_action_changes():
    snap = bench().read_back()
    assert rigs.action_before("set-33220a-pol-inv", snap) == {"led.polarity": "NORM"}
    assert rigs.action_before("park", snap) == {"bias.output": False, "smu.output": False,
                                                "led.output": False, "shutter.open": False}
    assert rigs.action_before("relay-to-sourcemeter", snap) == {"relay.position": "amplifier"}
    assert rigs.action_before("read-power", snap) == {}
    assert rigs.action_before("park", None) == {}


def test_the_snapshot_stub_says_attached_and_not_read_back_yet():
    """Before the first read-back the stub knows only that a controller is
    attached: `connected` None, which the Dry run counts as a pause until a
    read-back says the console's instrument answers."""
    b = Bench.build_simulated(RigConfig(temperature_console="sim"))
    assert b.snapshot_stub()["temperature"] == {
        "wired": True, "kelvin": None, "setpoint_k": None, "in_band": None,
        "source": "simulated", "connected": None}
    assert bench().snapshot_stub()["temperature"] == {
        "wired": False, "kelvin": None, "setpoint_k": None, "in_band": None,
        "source": None, "connected": None}


def test_the_simulated_meter_reads_the_average_of_a_pulsed_led_and_says_so():
    """The stand-in plays the filtered meter `open_power_meter` leaves on the
    rig: under a 50 % pulse it reads half the DC level, and the reading
    carries `averaged` so the console can say the number is a mean. With
    the filter off it reads whichever phase it sampled -- the flicker the
    operator saw, kept so this test can show why the filter exists."""
    from bace.service.rigs import power_reading
    b = Bench.build_simulated(RigConfig(), seed=3, fast=True)
    sim = b.sim
    sim.shutter.unblock()
    sim.led.set_dc(1.02)
    sim.led.enable_output(True)
    dc = power_reading(sim.power)
    assert dc.averaged is True and dc.watts > 0

    sim.led.set_pulse(1.02, 0.4, frequency_hz=500.0, duty_percent=50.0)
    pulsed = [power_reading(sim.power).watts for _ in range(20)]
    for w in pulsed:
        assert w == pytest.approx(dc.watts * 0.5, rel=0.02), "the duty-weighted mean, steady"

    sim.power.set_averaging(False)
    raw = [power_reading(sim.power) for _ in range(40)]
    assert all(r.averaged is False for r in raw)
    highs = [r.watts for r in raw if r.watts > dc.watts * 0.5]
    lows = [r.watts for r in raw if r.watts < dc.watts * 0.01]
    assert highs and lows and len(highs) + len(lows) == len(raw), "one phase or the other, never the mean"

    snap = b.read_back()
    assert snap["instruments"]["power"]["averaged"] is False
    sim.power.set_averaging(True)
    assert b.read_back()["instruments"]["power"]["averaged"] is True
