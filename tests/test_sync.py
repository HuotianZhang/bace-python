"""Where the extraction pulse sits in the LED cycle.

The question `--sync` exists to answer: is the 81150A armed when the light goes
off, or when it comes on? Half an LED period apart, and both produce a transient
that integrates to a plausible charge.
"""
from __future__ import annotations

import numpy as np
import pytest

from bace.bench import sync
from bace.bench.report import FAILED, OK, WARNED, Report
from bace.bench.sync import stage_sync, sync_phase
from bace.experiment.rig import RigConfig
from bace.experiment.transient import RunConfig
from tests.test_bench import FakeInstrument, FakeRM

DT = 25e-9
T0 = -50e-6
N = 100_000                    # 2.5 ms
PERIOD = 2e-3                  # 500 Hz
LED_HIGH, LED_LOW = 1.020, 0.400


def _t() -> np.ndarray:
    return np.arange(N) * DT + T0


def led_drive(high_first: bool = True, high: float = LED_HIGH) -> np.ndarray:
    """The 33220A output. `high_first` puts the lit half at the start of each
    period, so the LED falling edge is at 1 ms, 3 ms, ..."""
    phase = np.mod(_t(), PERIOD) < PERIOD / 2
    if not high_first:
        phase = ~phase
    return np.where(phase, high, LED_LOW)


def arm_sync(at_s: float, width_s: float = 1e-6) -> np.ndarray:
    """A TTL pulse on the 81150A Sync at `at_s` and every period after."""
    off = np.mod(_t() - at_s, PERIOD)
    return np.where(off < width_s, 3.3, 0.0)


def bias_out(at_s: float, delay_s: float = 135e-9,
             width_s: float = 5e-6) -> np.ndarray:
    off = np.mod(_t() - at_s - delay_s, PERIOD)
    return np.where(off < width_s, 5.0, 0.0)


KW = dict(led_threshold_v=1.0)


# -- the verdict ---------------------------------------------------------
def test_arming_on_the_falling_edge_is_bace():
    """LED on before the arm, off after it: extraction starts once generation
    has stopped, which is what the measurement means."""
    r = sync_phase(led_drive(), arm_sync(1e-3), DT, T0, **KW)
    assert r["verdict"] == "armed on light-off"
    assert r["LED before / after the marker"] == "on / off"
    assert r["LED period (ms)"] == pytest.approx(2.0, abs=0.01)
    assert r["LED duty (%)"] == pytest.approx(50.0, abs=0.5)
    assert r["marker to nearest LED edge (ns)"] == pytest.approx(0.0, abs=50)


def test_arming_on_the_rising_edge_extracts_during_illumination():
    """The failure this stage exists to catch. It is half a period — 1 ms —
    from correct, and nothing downstream would show it: the charge is still a
    number, the trace is still a transient, the files are still written."""
    r = sync_phase(led_drive(), arm_sync(0.0), DT, T0, **KW)
    assert r["verdict"] == "armed on light-on"
    assert r["LED before / after the marker"] == "off / on"
    assert "during carrier generation" in r["explanation"]


def test_a_flat_arm_channel_says_the_generator_is_not_running():
    r = sync_phase(led_drive(), np.zeros(N), DT, T0, **KW)
    assert r["verdict"] == "no arm edge"
    assert "not producing a waveform" in r["explanation"]


def test_an_arm_unrelated_to_the_led_cycle_is_not_called_correct():
    """Armed in the middle of the lit half: the LED is on either side, so the
    81150A is not being armed by the LED at all."""
    r = sync_phase(led_drive(), arm_sync(0.5e-3), DT, T0, **KW)
    assert r["verdict"] == "arm not on an LED edge"
    assert ":ARM:SOUR is EXT" in r["explanation"]


def test_a_drive_below_the_led_threshold_concludes_nothing():
    """0.95 V never lights the diode, so the phase is meaningless — and saying
    'armed on light-off' about a dark cycle would be worse than saying nothing."""
    r = sync_phase(led_drive(high=0.95), arm_sync(1e-3), DT, T0, **KW)
    assert r["verdict"] == "no illumination"
    assert "0.9500" in r["led low / high (V)"]


def test_the_81150a_delay_is_measured_against_the_arm():
    """`trigger_offset_s = 47e-9` was recovered from a binary and has never been
    confirmed by a measurement. Captured together with `delay`, this is where it
    becomes checkable."""
    r = sync_phase(led_drive(), arm_sync(1e-3), DT, T0,
                   bias=bias_out(1e-3, delay_s=135e-9), **KW)
    assert r["arm to 81150A output edge (ns)"] == pytest.approx(135, abs=DT * 1e9 * 2)


def test_edges_are_found_at_the_midpoint_not_the_turn_on_threshold():
    """The drive levels are 0.400 and 1.020 V against a 1.0 V threshold, so a
    threshold-crossing test decides every edge on 20 mV. The midpoint is 310 mV
    from either level. Adding noise that would break the threshold test must
    leave the verdict alone."""
    rng = np.random.default_rng(0)
    noisy = led_drive() + rng.normal(0.0, 0.015, N)
    r = sync_phase(noisy, arm_sync(1e-3), DT, T0, **KW)
    assert r["verdict"] == "armed on light-off"


# -- the stage, end to end ------------------------------------------------
class SyncScope(FakeInstrument):
    """A scope that answers with a different waveform per channel."""

    def __init__(self, traces: dict[str, np.ndarray]):
        super().__init__("KEYSIGHT TECHNOLOGIES,DSO9054H,MY5,06.74")
        self.traces = traces
        self.source = "CHAN1"
        self.yinc = 2e-4

    def write(self, command: str):
        for part in command.split(";"):
            if part.strip().upper().startswith(":WAV:SOUR"):
                self.source = part.split()[-1].strip().upper()
        return super().write(command)

    def query(self, command: str) -> str:
        up = command.strip().upper().rstrip(";")
        if up.startswith(":WAV:POIN?;"):
            return f"{N};{T0:g};{DT:g};0;{self.yinc:g};0"
        return super().query(command)

    def query_binary_values(self, command: str, **kw):
        self.log.append(command)
        y = self.traces.get(self.source, np.zeros(N))
        return np.round(y / self.yinc).astype(np.int16)


def _rm(traces, *, led_state=None, smu_on=False):
    rig = RigConfig()
    scope = SyncScope(traces)
    led = FakeInstrument("Agilent Technologies,33220A,MY4,2.00")
    led.state.update(led_state or {})
    smu = FakeInstrument("KEITHLEY INSTRUMENTS INC.,MODEL 2400,4,C34")
    smu.state[":OUTP"] = "1" if smu_on else "0"
    bias = FakeInstrument("Agilent Technologies,81150A,MY4,2.0")
    rm = FakeRM({rig.scope_address: scope, rig.bias_address: bias,
                 rig.led_address: led, rig.sourcemeter_address: smu})
    return rig, scope, rm, led, bias, smu


def test_the_stage_reports_the_verdict_and_puts_the_scope_back():
    """It runs with a sample connected, so every setting it changes has to come
    back — the timebase, the sweep mode, the trigger source, and the two
    channels it switched on."""
    rig, scope, rm, *_ = _rm({"CHAN1": led_drive(),
                              "CHAN3": arm_sync(1e-3),
                              "CHAN4": bias_out(1e-3)})
    # The state a real scope is found in, mid-session: the run's own 5 us
    # window, its CHAN3 trigger, averaging on, and the two extra channels dark.
    scope.state.update({
        ":TIM:RANG": "5.0E-6", ":TIM:POS": "2.0E-6", ":ACQ:POIN": "5000",
        ":ACQ:AVER": "1", ":ACQ:AVER:COUN": "20",
        ":TRIG:SWE": "AUTO", ":TRIG:EDGE:SOUR": "CHAN3",
        ":TRIG:EDGE:SLOP": "POS",
        ":CHAN1:DISP": "0", ":CHAN1:INP": "DC50",
        ":CHAN1:RANG": "1.0", ":CHAN1:OFFS": "0.0",
        ":CHAN4:DISP": "0", ":CHAN4:INP": "DC50",
        ":CHAN4:RANG": "1.0", ":CHAN4:OFFS": "0.0",
    })
    before = dict(scope.state)
    report = Report()
    stage_sync(report, rm, rig, RunConfig())

    phase = [c for c in report.checks if "LED cycle" in c.name][0]
    assert phase.status == OK
    assert phase.data["verdict"] == "armed on light-off"
    assert phase.data["points / dt (ns)"] == f"{N} / 25"

    for key, was in before.items():
        assert scope.state[key] == was, f"{key} was not restored"
    assert scope.state[":TRIG:EDGE:SOUR"] == "CHAN3", "the run's trigger source"
    assert scope.state[":TRIG:SWE"] == "AUTO"
    assert scope.state[":CHAN1:DISP"] == "0", "the tee channel was left switched on"


def test_the_stage_fails_the_run_when_the_arm_is_on_the_wrong_edge():
    """A warning is not enough. Every charge taken this way is a charge of
    something else, so the report has to come back FAILED."""
    rig, scope, rm, *_ = _rm({"CHAN1": led_drive(),
                              "CHAN3": arm_sync(0.0),
                              "CHAN4": bias_out(0.0)})
    report = Report()
    stage_sync(report, rm, rig, RunConfig())
    phase = [c for c in report.checks if "LED cycle" in c.name][0]
    assert phase.status == FAILED
    assert phase.data["verdict"] == "armed on light-on"


def test_the_state_check_asks_both_spellings_of_the_arm_slope():
    """`:ARM:SLOP` is written with no channel index while every neighbouring ARM
    command carries one. If only the indexed form answers, the slope has never
    been set and the instrument has been running on its own default."""
    rig, scope, rm, *_ = _rm({"CHAN1": led_drive(), "CHAN3": arm_sync(1e-3)})
    report = Report()
    stage_sync(report, rm, rig, RunConfig())
    state = [c for c in report.checks if "instrument state" in c.name][0]
    assert "81150A :ARM:SLOP?" in state.data
    assert "81150A :ARM:SLOP1?" in state.data
    assert "33220A :OUTP:POL?" in state.data


# -- --sync-drive: putting the generators back after DC work --------------
_JV_STATE = {"FUNC:SHAP": "DC", ":VOLT:OFFS": "1.020", ":OUTP": "1"}


def test_the_led_goes_to_pulse_at_the_runs_frequency_and_back_to_dc():
    """The situation this exists for: a J-V scan has just left the 33220A in DC
    (`FUNC:SHAP DC`), and nothing in the harness puts it back. --configure sets
    pulse but leaves the output off, --outputs enables it for one query, and
    --measure never touches it.

    The frequency has to be the run's, not a driver default. The 81150A is armed
    by this generator's Sync, so a LED left at 1 kHz against an 81150A at 500 Hz
    is a rig where every second arm is missing."""
    rig, scope, rm, led, bias, smu = _rm(
        {"CHAN1": led_drive(), "CHAN3": arm_sync(1e-3)}, led_state=dict(_JV_STATE))
    report = Report()
    stage_sync(report, rm, rig, RunConfig(pulse_frequency_hz=500.0),
               led_drive=(1.020, 0.4), drive=True, confirm=lambda q: False)

    joined = " ".join(led.log)
    assert "FUNC:SHAP PULSE;" in joined
    assert ":FREQ 500;" in joined, "the LED must run at the 81150A's frequency"
    assert ":VOLT:HIGH 1.02;" in joined and ":VOLT:LOW 0.4;" in joined

    # and put back exactly as found
    assert led.state["FUNC:SHAP"] == "DC"
    assert led.state[":VOLT:OFFS"] == "1.020"
    assert led.state[":OUTP"] == "1"


def test_the_81150a_stays_as_found_when_the_relay_cannot_be_proven_safe(monkeypatch):
    """Its amplifier feeds the device whenever the relay is on that side, so
    'I could not read the relay' has to mean 'I did not enable it'. The read
    is made to fail here rather than assumed to: on the lab PC DELIB loads
    and the real relay answers, and this test then read the bench instead of
    the case that matters."""
    import bace.drivers.shutter as shutter_mod

    def no_dio(*a, **k):
        raise OSError("no DELIB this interpreter can load")

    monkeypatch.setattr(shutter_mod, "Shutter", no_dio)
    rig, scope, rm, led, bias, smu = _rm(
        {"CHAN1": led_drive(), "CHAN3": arm_sync(1e-3)}, led_state=dict(_JV_STATE))
    report = Report()
    stage_sync(report, rm, rig, RunConfig(), drive=True, confirm=lambda q: False)

    phase = [c for c in report.checks if "LED cycle" in c.name][0]
    assert phase.status == WARNED
    assert "could not be read back" in phase.detail
    assert phase.data["81150A output"] == "left as found"
    assert not any("OUTP1 ON" in cmd.upper() for cmd in bias.log)


def test_a_live_keithley_stops_the_stage_before_anything_is_driven():
    """With the relay on the SourceMeter side a live Keithley is a source into
    the device. Nothing else gets enabled on top of it."""
    rig, scope, rm, led, bias, smu = _rm(
        {"CHAN1": led_drive(), "CHAN3": arm_sync(1e-3)}, smu_on=True)
    report = Report()
    stage_sync(report, rm, rig, RunConfig(), drive=True, confirm=lambda q: True)

    phase = [c for c in report.checks if "LED cycle" in c.name][0]
    assert phase.status == FAILED
    assert "Keithley output is ON" in phase.detail
    assert not any(":OUTP 1" in cmd for cmd in led.log)


def test_without_the_flag_the_generators_are_never_touched():
    """--sync on its own is scope-only, so it is safe to run at any time."""
    rig, scope, rm, led, bias, smu = _rm(
        {"CHAN1": led_drive(), "CHAN3": arm_sync(1e-3)}, led_state=dict(_JV_STATE))
    report = Report()
    stage_sync(report, rm, rig, RunConfig())
    assert not any(cmd.startswith("FUNC:SHAP") for cmd in led.log)
    assert not any("OUTP1 ON" in cmd.upper() for cmd in bias.log)


# -- the enable question, which has to be answerable ----------------------
_BIAS_LEVELS = {":OUTP1": "0", ":VOLT1:HIGH": "1.2650", ":VOLT1:LOW": "0.0"}


def _with_bias(levels=None, on=False):
    rig, scope, rm, led, bias, smu = _rm(
        {"CHAN1": led_drive(), "CHAN3": arm_sync(1e-3)},
        led_state=dict(_JV_STATE))
    bias.state.update(levels or _BIAS_LEVELS)
    bias.state[":OUTP1"] = "1" if on else "0"
    return rig, scope, rm, led, bias, smu


def test_the_question_says_what_the_81150a_would_put_on_the_device():
    """'Enable anyway?' is unanswerable without the levels. The generator keeps
    whatever the last session left, and this rig has been found free-running at
    +5.06 V at the device — so the prompt carries the number, converted through
    the x4 amplifier."""
    rig, scope, rm, led, bias, smu = _with_bias()
    asked = []
    report = Report()
    stage_sync(report, rm, rig, RunConfig(), drive=True,
               confirm=lambda q: asked.append(q) or "no")

    phase = [c for c in report.checks if "LED cycle" in c.name][0]
    assert phase.data["81150A levels (at the device)"] == "+5.06 / +0 V"
    assert len(asked) == 1 and "+5.06" in asked[0]


def test_answering_no_leaves_the_81150a_off():
    rig, scope, rm, led, bias, smu = _with_bias()
    report = Report()
    stage_sync(report, rm, rig, RunConfig(), drive=True, confirm=lambda q: "no")
    assert not any("OUTP1 ON" in c.upper() for c in bias.log)
    phase = [c for c in report.checks if "LED cycle" in c.name][0]
    assert phase.data["81150A output"] == "left as found"


def test_an_explicit_yes_enables_it_warns_and_puts_it_back():
    """A human override is allowed — with the levels in front of them — but it
    still has to come back off, and the report has to say it happened."""
    rig, scope, rm, led, bias, smu = _with_bias()
    report = Report()
    stage_sync(report, rm, rig, RunConfig(), drive=True, confirm=lambda q: "yes")

    phase = [c for c in report.checks if "LED cycle" in c.name][0]
    assert phase.status == WARNED
    assert "explicit yes" in phase.detail
    assert phase.data["81150A enable, asked"] == "yes"
    assert any("OUTP1 ON" in c.upper() for c in bias.log)
    assert bias.state[":OUTP1"] == "0", "it has to come back off"


def test_an_already_running_81150a_is_left_exactly_alone():
    """If its output is already on, the sample is already seeing it: there is
    nothing to authorise and nothing to restore, and turning it off at the end
    would be a change nobody asked for."""
    rig, scope, rm, led, bias, smu = _with_bias(on=True)
    asked = []
    report = Report()
    stage_sync(report, rm, rig, RunConfig(), drive=True,
               confirm=lambda q: asked.append(q) or "no")

    phase = [c for c in report.checks if "LED cycle" in c.name][0]
    assert phase.data["81150A output"] == "already on, left alone"
    assert asked == [], "nothing to ask: it is already driving"
    assert bias.state[":OUTP1"] == "1"


def test_the_restore_never_recombines_two_func_headers():
    """The rig rejected `FUNC:SHAP PULSE;FUNC:PULS:HOLD DCYC;` with
    -113,"Undefined header" on 2026-09-01: after the first command the next
    mnemonic continues at the same subsystem level, so the second is parsed as
    `FUNC:FUNC:PULS:HOLD` and the rest of the message is discarded. The driver
    has always split them; the restore path had recombined them."""
    rig, scope, rm, led, bias, smu = _rm(
        {"CHAN1": led_drive(), "CHAN3": arm_sync(1e-3)},
        led_state={"FUNC:SHAP": "PULS", ":VOLT:HIGH": "1.020",
                   ":VOLT:LOW": "0.400", ":FREQ": "500", ":OUTP": "1"})
    report = Report()
    stage_sync(report, rm, rig, RunConfig(), drive=True, confirm=lambda q: "no")

    for cmd in led.log:
        body = cmd.strip()
        assert body.count("FUNC:") <= 1, (
            f"{body!r} puts two FUNC: headers in one message; the second is "
            "parsed relative to the first and rejected")
    assert any(c.strip() == "FUNC:PULS:HOLD DCYC;" for c in led.log)


# -- a channel with nothing on it must not produce a verdict --------------
def test_a_flat_sync_channel_falls_back_to_the_81150a_output():
    """What the rig actually handed back on 2026-09-01. CHAN3 was not carrying
    the Sync and swung 2.3 mV of noise; the midpoint of noise-with-an-outlier
    sits below the noise band, so every sample read as 'high', exactly one
    rising edge was found just after the outlier, and a verdict came out of it.
    The outlier was the LED edge coupling into an unterminated input — which is
    why the phantom edge landed 320 ns from a real LED edge and looked right.

    The 81150A's own output answers the same question and is better evidence:
    it is the field, not a sync standing in for it."""
    rng = np.random.default_rng(3)
    flat = rng.normal(0.0, 5e-4, N)
    flat[int((1e-3 - T0) / DT) + 5] = -0.0047        # the LED-edge pickup
    r = sync_phase(led_drive(), flat, DT, T0, bias=bias_out(1e-3), **KW)

    assert r["marker"].startswith("81150A output")
    assert "noise, not a logic line" in r["marker note"]
    assert r["arm channel swing (V)"] < 0.01
    assert r["verdict"] == "armed on light-off"
    assert "arm to 81150A output edge (ns)" not in r, \
        "that number is meaningless when one of the two channels is flat"


def test_both_channels_flat_says_nothing_rather_than_guessing():
    rng = np.random.default_rng(4)
    r = sync_phase(led_drive(), rng.normal(0.0, 5e-4, N), DT, T0,
                   bias=rng.normal(0.0, 5e-4, N), **KW)
    assert r["verdict"] == "no arm edge"
    assert "nor the 81150A output carries a two-level signal" in r["explanation"]


def test_a_real_sync_is_still_preferred_over_the_output():
    r = sync_phase(led_drive(), arm_sync(1e-3), DT, T0,
                   bias=bias_out(1e-3, delay_s=135e-9), **KW)
    assert r["marker"] == "81150A Sync"
    assert r["arm to 81150A output edge (ns)"] == pytest.approx(135, abs=DT * 1e9 * 2)


def test_a_narrow_pulse_lost_to_decimation_is_named_as_a_suspect():
    """The rig, 2026-09-01: CHAN3 came back as 2-87 mV of nothing at 40 ns a
    point, and the same channel showed a clean pulse the moment the timebase was
    made faster. `:ACQ:MODE RTIM` decimates by dropping samples, so a sync pulse
    narrower than one sample interval is not attenuated — it is absent. A flat
    channel at a coarse sample rate is a suspect, not a conclusion."""
    rig, scope, rm, led, bias, smu = _rm({"CHAN1": led_drive(),
                                          "CHAN3": np.zeros(N),
                                          "CHAN4": bias_out(1e-3)})
    report = Report()
    stage_sync(report, rm, rig, RunConfig())          # DT here is 25 ns > 20 ns
    phase = [c for c in report.checks if "LED cycle" in c.name][0]
    assert phase.data["verdict"] == "armed on light-off"
    assert "--sync-points" in phase.data["marker note"]
    assert phase.data["points requested / delivered"].endswith(f"/ {N}")
