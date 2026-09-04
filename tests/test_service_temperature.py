"""The settle helper on its own: what each failure of the 331 console is
called, the ramp guard, the heater-off warning, and the readings taken
while a pause lasts.

Transcript-style: a scripted controller stands in for the console, the
events the generator yields are listed in order, and the `Settled` it
returns is checked beside them. The executor and module tests cover the
same helper inside a run; here are the cases a run cannot script without a
console that misbehaves on cue -- a refusal against an outage, a write that
landed after the client gave up, a console that dies mid-settle.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from bace.drivers.lakeshore331 import TemperatureError
from bace.drivers.protocols import TemperatureReading
from bace.experiment import events as E
from bace.experiment.rig import RigConfig
from bace.service import temperature as T
from bace.service.modules import RunContext
from bace.service.rigs import Bench
from bace.service.worker import AbortNow, StopMode
from bace.storage.naming import RunMetadata

NO_SLEEP = lambda s: None                                      # noqa: E731
RUN_ID = "20260902_230000-001"
POLL = T.TEMPERATURE_POLL_S
DETAIL = {"setpoint_k": 250.0, "tolerance_k": 0.2, "hold_s": 0.0, "timeout_s": 600.0}
ANSWER = lambda detail, on_poll=None: {}                       # noqa: E731


def reading(kelvin: float, *, setpoint: float | None = None, connected: bool = True,
            ramping: bool = False, heater_range: int = 3, status: str = "ok"):
    return TemperatureReading(kelvin=kelvin, setpoint_k=setpoint, ramping=ramping,
                              heater_range=heater_range, connected=connected,
                              status_text=status, elapsed_s=None, max_setpoint_k=350.0)


class Scripted:
    """A controller that answers `read()` from a script -- a reading, or an
    exception to raise; the last entry repeats -- and writes as told. A
    reading with no setpoint of its own reports the last one written."""

    base_url = "http://127.0.0.1:8331"

    def __init__(self, reads, *, write: BaseException | None = None):
        self.reads = list(reads)
        self.write = write
        self.setpoint_k: float | None = None
        self.setpoints: list[float] = []
        self.notes: list[str] = []

    def read(self):
        item = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        if isinstance(item, BaseException):
            raise item
        if item.setpoint_k is None and self.setpoint_k is not None:
            return replace(item, setpoint_k=self.setpoint_k)
        return item

    def set_setpoint(self, kelvin: float) -> float:
        if self.write is not None:
            raise self.write
        self.setpoint_k = float(kelvin)
        self.setpoints.append(float(kelvin))
        return float(kelvin)

    def note(self, text: str) -> bool:
        self.notes.append(str(text))
        return True


def ctx(**kw) -> RunContext:
    kw.setdefault("sleep", NO_SLEEP)
    return RunContext(run_id=RUN_ID, node_path="T=250K", out_folder=".",
                      metadata=RunMetadata(sample="s4", material="SIM", pixel="a"), **kw)


def wired(**kw) -> Bench:
    return Bench.build_simulated(RigConfig(temperature_console="sim"), seed=1, fast=True, **kw)


def rig_with(controller):
    b = wired()
    b.rig.temperature = controller
    return b.rig


def drive(gen):
    """Every event the generator yields, and the `Settled` it returns."""
    events = []
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            return events, stop.value


def kinds(events) -> list[str]:
    return [type(e).__name__ for e in events]


# -- a refusal is not an outage ---------------------------------------------------------
def test_a_refusal_is_the_consoles_sentence_and_an_outage_is_not_a_refusal():
    """The driver tells the two apart (`TemperatureError.refused`) and so
    does the settle: a 403 is `temperature.refused`, its text the console's
    own sentence and the driver's full text in the data; a write nothing
    answered is `temperature.timeout` with `reason = "unreachable"` -- start
    the console, do not reconsider the setpoint. Neither names the subtree
    after the refused or unwritten number: the reading does."""
    bare = "setpoint 400.000 K exceeds the 350.0 K limit for this cryostat"
    full = bare + (" (the 331 console at http://127.0.0.1:8331 refused POST /api/setpoint "
                   "with HTTP 403; it is running -- this is the request it did not accept)")
    c = Scripted([reading(294.8, setpoint=294.8)],
                 write=TemperatureError(full, refused=True, status=403, console_message=bare))
    asked = []
    events, out = drive(T.settle(
        rig_with(c), ctx(wait_for_operator=lambda d, on_poll=None: (asked.append(d), {"note": "no"})[1]),
        {**DETAIL, "setpoint_k": 400.0}, node_path="T=400K"))
    assert kinds(events) == ["TemperatureRead", "Verdict", "NeedsOperator", "OperatorResumed"]
    assert (events[0].kelvin, events[0].setpoint_k, events[0].in_band) == (294.8, 400.0, False)
    crit = events[1]
    assert (crit.level, crit.code, crit.text) == ("crit", "temperature.refused", bare)
    assert crit.data["error"] == full and crit.data["status"] == 403
    assert crit.data["console"] == "http://127.0.0.1:8331" and crit.data["source"] == "console"
    assert events[2].what == "temperature refused" and events[2].detail["error"] == bare
    assert asked[0]["setpoint_k"] == 400.0
    assert c.setpoints == [] and c.notes == [
        f"bace {RUN_ID} T=400K: setpoint 400 K +/-0.2 K hold 0 s",
        f"bace {RUN_ID} T=400K: operator resumed at 294.800 K (no)"]
    assert out.how == "operator" and out.temperature_k == 294.8, (
        "the reading names the subtree, never the refused number")

    dead = TemperatureError("cannot reach the 331 console at http://127.0.0.1:8331 -- start it")
    c = Scripted([reading(294.8, setpoint=294.8)], write=dead)
    events, out = drive(T.settle(rig_with(c), ctx(wait_for_operator=ANSWER), DETAIL,
                                 node_path="T=250K"))
    assert kinds(events) == ["TemperatureRead", "Verdict", "NeedsOperator", "OperatorResumed"]
    warn = events[1]
    assert (warn.level, warn.code) == ("warn", "temperature.timeout")
    assert warn.data["reason"] == "unreachable" and warn.data["written"] is False
    assert warn.data["error"] == str(dead) and warn.data["polls"] == 1
    assert warn.text == (f"250 K: the 331 did not answer the setpoint write ({dead}); "
                         "last reading 294.80 K -- pausing for the operator; fix the "
                         "connection, then resume or stop")
    assert "no instrument behind it" not in warn.text
    need = events[2]
    assert need.what == "temperature timeout"
    assert (need.detail["reason"], need.detail["written"], need.detail["error"]) == \
        ("unreachable", False, str(dead))
    assert out.how == "operator" and out.temperature_k == 294.8 and out.polls == 1


def test_a_write_the_console_did_not_answer_but_applied_settles_with_a_notice():
    """The console serves the write on its bus thread and can apply it after
    the client stopped waiting: the state is read back once, and a setpoint
    that landed is a slow console, not a dead one -- a notice, then the
    settle goes on as if the write had answered."""
    class Slow(Scripted):
        def set_setpoint(self, kelvin: float) -> float:
            super().set_setpoint(kelvin)
            raise TemperatureError("cannot reach the 331 console at http://127.0.0.1:8331 "
                                   "(TimeoutError: timed out)")

    c = Slow([reading(250.1)])
    events, out = drive(T.settle(rig_with(c), ctx(), DETAIL, node_path="T=250K"))
    assert kinds(events) == ["TemperatureRead", "Notice", "Verdict"]
    notice = events[1]
    assert notice.level == "warning"
    assert notice.text.startswith("T=250K: the 331 did not answer the setpoint write in "
                                  "time but reports 250 K in force -- settling (cannot reach")
    assert events[2].code == "temperature.settled" and events[2].data["polls"] == 1
    assert out.how == "settled" and out.temperature_k == 250.1 and c.setpoints == [250.0]


# -- silent, before and after the write ---------------------------------------------------
def test_a_silent_instrument_pauses_before_anything_is_written():
    """Console up, `connected` false: three polls, no reading worth yielding,
    no setpoint written into a console with nothing behind it -- the pause
    the Dry run promised, with the setpoint standing for the subtree."""
    b = wired()
    b.sim.temperature.connected = False
    slept = []
    events, out = drive(T.settle(b.rig, ctx(sleep=slept.append, wait_for_operator=ANSWER),
                                 DETAIL, node_path="T=250K"))
    assert kinds(events) == ["Verdict", "NeedsOperator", "OperatorResumed"]
    warn = events[0]
    assert (warn.level, warn.code, warn.node_path) == ("warn", "temperature.timeout", "T=250K")
    assert (warn.data["reason"], warn.data["polls"], warn.data["written"]) == ("silent", 3, False)
    assert warn.data["kelvin"] is None and warn.data["elapsed_s"] == 2 * POLL
    assert warn.text == ("250 K: the 331 console answered 3 polls with no instrument behind it "
                         "before the setpoint was written (the instrument is not answering) "
                         "-- pausing for the operator")
    assert (events[1].detail["reason"], events[1].detail["written"]) == ("silent", False)
    assert b.sim.temperature.setpoints == [] and b.sim.temperature.notes == [
        f"bace {RUN_ID} T=250K: operator resumed"]
    assert slept == [POLL, POLL, 0.0], "two waits between the three polls, then the hold"
    assert out.how == "operator" and out.temperature_k is None, "nothing usable read: the setpoint stands"
    assert out.polls == 3 and out.settle_s >= 2 * POLL


def test_a_console_that_stops_answering_mid_settle_says_so_not_instrument_silent():
    """After the write the console process dies: the reads raise, and the
    verdict says the console did not answer -- not that it answered with no
    instrument behind it, which would send the operator to the GPIB cable."""
    dead = TemperatureError("cannot reach the 331 console at http://127.0.0.1:8331 -- start it")
    c = Scripted([reading(280.0), reading(279.0), dead])
    events, out = drive(T.settle(rig_with(c), ctx(wait_for_operator=ANSWER), DETAIL,
                                 node_path="T=250K"))
    assert kinds(events) == ["TemperatureRead", "TemperatureRead", "Verdict", "NeedsOperator",
                             "OperatorResumed"]
    warn = events[2]
    assert (warn.data["reason"], warn.data["polls"], warn.data["written"]) == ("unreachable", 5, True)
    assert warn.data["kelvin"] == 279.0 and warn.data["error"] == str(dead)
    assert warn.text == (f"250 K: the 331 did not answer 3 polls ({dead}); last reading "
                         "279.00 K -- pausing for the operator; fix the connection, then "
                         "resume or stop")
    assert c.setpoints == [250.0]
    assert out.how == "operator" and out.temperature_k == 279.0 and out.polls == 5

    # the instrument going silent after the write keeps the old wording
    c = Scripted([reading(280.0), reading(279.0, connected=False, status="VI_ERROR_TMO on KRDG? A")])
    events, out = drive(T.settle(rig_with(c), ctx(wait_for_operator=ANSWER), DETAIL,
                                 node_path="T=250K"))
    warn = events[1]
    assert warn.data["reason"] == "silent" and warn.data["written"] is True
    assert warn.text == ("250 K: the 331 console answered 3 polls with no instrument behind it "
                         "(VI_ERROR_TMO on KRDG? A); last reading 280.00 K -- pausing for the "
                         "operator")


# -- the ramp guard and the heater range --------------------------------------------------
def test_a_reading_inside_the_band_does_not_count_while_the_ramp_walks_the_setpoint():
    """RAMPST? up: the 331 is still walking its setpoint, so a reading in the
    band is on its way, not settled. Without the guard this settles at the
    first poll; with it, at the first poll after the ramp is done."""
    b = wired()
    t = b.sim.temperature
    t.kelvin = t.setpoint_k = 250.0
    t.ramping = True
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:
            t.ramping = False

    events, out = drive(T.settle(b.rig, ctx(sleep=sleep), DETAIL, node_path="T=250K"))
    reads = [e for e in events if isinstance(e, E.TemperatureRead)]
    assert len(reads) == 4 and all(r.in_band for r in reads), (
        "in band at every poll, settled only once the ramp is done")
    settled = events[-1]
    assert settled.code == "temperature.settled" and settled.data["polls"] == 4
    assert settled.data["settle_s"] == 3 * POLL == out.settle_s
    assert sleeps == [POLL] * 3 and out.how == "settled"


def test_the_heater_off_is_said_at_once_and_only_when_heat_is_needed():
    """A heater range of 0 with the setpoint above the reading: the SETP is
    accepted and nothing will drive toward it, so it is said at the first
    reading (once), not discovered at the timeout -- and never changed from
    here. Cooling with the range off is what the cryogen does: no warning."""
    b = wired()
    t = b.sim.temperature
    t.heater_range = 0
    events, out = drive(T.settle(b.rig, ctx(wait_for_operator=ANSWER),
                                 {**DETAIL, "setpoint_k": 300.0, "timeout_s": 10.0},
                                 node_path="T=300K"))
    assert kinds(events)[:2] == ["TemperatureRead", "Verdict"], "said at the first reading"
    verdicts = [e for e in events if isinstance(e, E.Verdict)]
    assert [v.code for v in verdicts] == ["temperature.heater-off", "temperature.timeout"]
    off = verdicts[0]
    assert (off.level, off.node_path) == ("warn", "T=300K")
    assert off.data == {"setpoint_k": 300.0, "kelvin": 294.8, "heater_range": 0,
                        "source": "simulated", "console": None}
    assert off.text == ("300 K asked for with the heater range off on the 331 (reading "
                        "294.80 K): the setpoint is written but nothing will drive toward it "
                        "until the range is raised on the front panel")
    assert t.setpoints == [300.0] and t.heater_range == 0 and t.kelvin == 294.8
    assert verdicts[1].data["reason"] == "timeout" and out.how == "operator"

    b = wired()
    b.sim.temperature.heater_range = 0
    events, out = drive(T.settle(b.rig, ctx(), DETAIL, node_path="T=250K"))
    assert [e.code for e in events if isinstance(e, E.Verdict)] == ["temperature.settled"]
    assert out.how == "settled" and abs(out.temperature_k - 250.0) <= 0.2


# -- readings while a pause lasts, and the module path's hooks ---------------------------
def test_readings_polled_while_a_pause_lasts_go_to_the_hook_or_are_yielded_after(monkeypatch):
    """With an `emit` hook (the context's, here) every reading polled during
    the wait goes out as it is taken and none is yielded; without one the
    last `MAX_POLLED_READS` are yielded after the resume. Either way the
    subtree is measured at the last reading polled when nothing is typed."""
    monkeypatch.setattr(T, "TEMPERATURE_POLL_S", 0.0)       # every on_poll reads
    monkeypatch.setattr(T, "MAX_POLLED_READS", 3)

    def wait(detail, on_poll=None):
        for _ in range(5):
            on_poll()
        return {"note": "ok"}

    b = wired()
    b.sim.temperature.time_constant_reads = 1000
    detail = {**DETAIL, "timeout_s": 0.0}                    # pause at the first reading out of band
    events, out = drive(T.settle(b.rig, ctx(wait_for_operator=wait), detail, node_path="T=250K"))
    assert kinds(events) == ["TemperatureRead", "Verdict", "NeedsOperator", "TemperatureRead",
                             "TemperatureRead", "TemperatureRead", "OperatorResumed"]
    polled = events[3:6]
    assert b.sim.temperature.reads == 6 and polled[0].kelvin > polled[-1].kelvin
    assert out.temperature_k == polled[-1].kelvin == b.sim.temperature.kelvin, (
        "the last of the five polls, three of which were kept")
    assert out.how == "operator" and out.source == "simulated"

    b = wired()
    b.sim.temperature.time_constant_reads = 1000
    live = []
    events, out = drive(T.settle(b.rig, ctx(wait_for_operator=wait, emit=live.append), detail,
                                 node_path="T=250K"))
    assert kinds(events) == ["TemperatureRead", "Verdict", "NeedsOperator", "OperatorResumed"]
    assert len(live) == 5 and all(isinstance(r, E.TemperatureRead) for r in live)
    assert {r.source for r in live} == {"simulated"} and {r.setpoint_k for r in live} == {250.0}
    assert out.temperature_k == live[-1].kelvin


def test_without_a_job_the_contexts_stop_mode_tells_an_abort_from_a_stop():
    """The executor fills `ctx.stop_mode` from the job on the module path:
    an abort raises `AbortNow` at the next poll (the worker reports
    `aborted`), an after_shot returns a stopped `Settled`; with only the
    flag, a stop."""
    b = wired()
    events, out = drive(T.settle(b.rig, ctx(stop_mode=lambda: StopMode.AFTER_SHOT), DETAIL,
                                 node_path="T=250K"))
    assert events == [] and out.stopped and out.polls == 0
    with pytest.raises(AbortNow, match="abort requested while T=250K settled"):
        next(T.settle(b.rig, ctx(stop_mode=lambda: StopMode.ABORT), DETAIL, node_path="T=250K"))
    events, out = drive(T.settle(b.rig, ctx(abort=lambda: True), DETAIL, node_path="T=250K"))
    assert events == [] and out.stopped
    assert b.sim.temperature.setpoints == [], "stopped before anything was written"


def test_the_keys_a_detail_leaves_out_take_the_trees_defaults():
    """A hand-built detail with only the setpoint: the tolerance, hold and
    timeout are the tree's defaults, not zeros -- a zero timeout would pause
    at the first reading outside the band."""
    from bace.service.pipeline import TEMPERATURE_DEFAULTS

    b = wired()
    events, out = drive(T.settle(b.rig, ctx(), {"setpoint_k": 250.0}, node_path="T=250K"))
    settled = events[-1]
    assert settled.code == "temperature.settled" and out.how == "settled"
    assert settled.data["tolerance_k"] == TEMPERATURE_DEFAULTS["tolerance_k"] == 0.2
    assert settled.data["hold_s"] == TEMPERATURE_DEFAULTS["hold_s"] == 60.0
    assert settled.data["held_s"] == 60.0


def test_a_node_nobody_gave_a_temperature_reads_the_331_once_and_says_so():
    """The recipes typed 290 K until 2026-09-04, and a run at 220 K was filed
    as `290 K · typed`. Nothing typed and a controller on the bench: the
    node reads it as it starts, `how = "read"`, the source classified as
    the settle classifies it, bound onto the context so the executor's
    `NodeDone` agrees with the file."""
    from bace.service.modules import _bind_temperature

    rig = rig_with(Scripted([reading(220.4, setpoint=220.0)]))
    c = ctx()
    assert c.temperature() == (None, "", "")
    assert _bind_temperature(c, rig) == (220.4, "read", "console")
    assert c.temperature() == (220.4, "read", "console"), "bound, so NodeDone says the same"

    # typed wins: nobody re-reads a number the operator chose
    typed = replace(ctx(), metadata=replace(ctx().metadata, temperature_k=290.0,
                                            temperature_how="typed"))
    assert _bind_temperature(typed, rig) == (290.0, "typed", "")

    # a controller that does not answer leaves it not recorded
    silent = rig_with(Scripted([reading(float("nan"), connected=False)]))
    assert _bind_temperature(ctx(), silent) == (None, "", "")

    # no controller at all: the same
    assert _bind_temperature(ctx(), rig_with(None)) == (None, "", "")


def test_closing_the_331_transport_leaves_the_shared_resource_manager_open(monkeypatch):
    """`pyvisa.ResourceManager()` is one cached instance per process. The
    vendored transport used to close it with the 331's session, which on the
    lab PC (2026-09-04) closed the bench harness's scope, generator and
    Keithley sessions too: pass 2 reported all four "not reachable" two
    seconds after pass 1 had identified them. The service's failure path at
    start-up did the same to the instruments it had just opened."""
    import sys
    import types

    from bace.drivers.lakeshore331.config import Connection
    from bace.drivers.lakeshore331.transport import VisaTransport

    class Inst:
        closed = False

        def close(self):
            self.closed = True

    class RM:
        closed = False
        opened: list = []

        def open_resource(self, resource):
            inst = Inst()
            self.opened.append(inst)
            return inst

        def close(self):
            self.closed = True

    shared = RM()
    monkeypatch.setitem(sys.modules, "pyvisa",
                        types.SimpleNamespace(ResourceManager=lambda: shared))
    t = VisaTransport(Connection(resource="GPIB0::7::INSTR"))
    t.close()
    assert shared.opened[0].closed, "the 331's own session is released"
    assert not shared.closed, "the ResourceManager is everyone's and stays open"


def test_a_bench_skip_keeps_the_reason_a_warn_recorded_first():
    from bace.bench.report import SKIPPED, Check

    c = Check(name="81150A state", stage="read")
    c.warn("cannot open GPIB0::12::INSTR: VI_ERROR_INV_OBJECT")
    c.skip("not reachable")
    assert c.status == SKIPPED
    assert c.detail == "not reachable (cannot open GPIB0::12::INSTR: VI_ERROR_INV_OBJECT)"
    assert Check(name="x", stage="read").skip("plain").detail == "plain"
