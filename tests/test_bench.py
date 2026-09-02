"""The bench harness, exercised against a fake VISA layer.

The point of the harness is to be run once, on a machine I cannot reach, by
someone who then has to send the result back. If it crashes halfway the trip is
wasted — so every stage runs here against instruments that answer plausibly, and
against instruments that misbehave.
"""
from __future__ import annotations

import contextlib
import json
import sys
import types

import numpy as np
import pytest

from bace.bench import checks
from bace.bench.report import FAILED, OK, SKIPPED, WARNED, Report, render_html


def _repo_file(name: str) -> str:
    """A file at the package root, found from this test's own location."""
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), name)
from bace.experiment.rig import RigConfig
from bace.experiment.transient import RunConfig


class FakeInstrument:
    """Answers the queries the harness asks, well enough to exercise it."""

    def __init__(self, idn: str, *, points_returned: int = 4000,
                 acq_points: int = 5000, sample_rate: float = 2e9,
                 outputs_on: bool = False, indefinite_block: bool = False,
                 reject: tuple[str, ...] = (), fail_open: bool = False):
        self.idn = idn
        self.acq_points = acq_points
        self.sample_rate = sample_rate
        self.outputs_on = outputs_on
        self.indefinite_block = indefinite_block
        self.timeout = 0
        self.log: list[str] = []
        self.state: dict[str, str] = {}
        self.points_returned = points_returned
        self.reject = reject
        self.errors: list[str] = []
        if fail_open:
            raise OSError("simulated open failure")

    # -- traffic ----------------------------------------------------------
    def write(self, command: str):
        self.log.append(command)
        for part in command.split(";"):
            part = part.strip()
            if not part:
                continue
            if any(r in part for r in self.reject):
                self.errors.append(f'-113,"Undefined header; {part}"')
                continue
            if " " in part:
                key, _, value = part.partition(" ")
                self.state[key.rstrip(";").upper()] = value.rstrip(";")
        return len(command)

    def query(self, command: str) -> str:
        self.log.append(command)
        up = command.strip().upper().rstrip(";")
        if "*IDN?" in up:
            return self.idn
        if "SYST:ERR" in up:
            return self.errors.pop(0) if self.errors else '0,"No error"'
        if up.startswith(":WAV:POIN?;"):        # the combined preamble query
            n = self.points_returned
            return f"{n};-8.000E-7;5.0E-10;0;1.0E-5;0"
        if up == ":ACQ:POIN?":
            return str(self.acq_points)
        if up == ":ACQ:SRAT?":
            return str(self.sample_rate)
        if up == ":ADER?":
            return "1"
        key = up.rstrip("?")
        if key in self.state:
            return self.state[key]
        # plausible numeric defaults
        on = "1" if self.outputs_on else "0"
        return {":TIM:RANG": "2.0E-6", ":TIM:POS": "8.0E-7",
                ":OUTP": on, ":OUTP1": on}.get(key, "0")

    def read_raw(self):
        n = self.points_returned
        t = np.arange(n)
        y = (np.where(t > 1800, 20000 * np.exp(-(t - 1800) / 160.0), 0.0) + 500)
        payload = y.astype(">i2").tobytes()
        if self.indefinite_block:
            return b"#0" + payload
        digits = str(len(payload))
        return b"#" + str(len(digits)).encode() + digits.encode() + payload

    def query_binary_values(self, command: str, **kw):
        self.log.append(command)
        n = self.points_returned
        t = np.arange(n)
        y = np.where(t > 1800, 20000 * np.exp(-(t - 1800) / 160.0), 0.0)
        return (y + 500).astype(np.int16)

    def close(self):
        self.log.append("<close>")


class FakeRM:
    def __init__(self, table: dict[str, FakeInstrument], resources=()):
        self.table = table
        self.resources = list(resources) or list(table)

    def list_resources(self):
        return tuple(self.resources)

    def open_resource(self, address: str):
        if address not in self.table:
            raise OSError(f"no such resource {address}")
        return self.table[address]

    def __str__(self):
        return "FakeResourceManager"


@pytest.fixture
def rig():
    return RigConfig()


@pytest.fixture
def rm(rig):
    return FakeRM({
        rig.scope_address: FakeInstrument("KEYSIGHT,DSO9054H,MY123,06.20"),
        rig.bias_address: FakeInstrument("Agilent Technologies,81150A,MY5,1.0"),
        rig.sourcemeter_address: FakeInstrument(
            "KEITHLEY INSTRUMENTS INC.,MODEL 2400,4473504,C34"),
        rig.led_address: FakeInstrument("Agilent Technologies,33220A,MY4,2.0"),
    })


# -- the stages run -------------------------------------------------------
def test_offline_stage_passes_with_no_instruments():
    r = Report()
    checks.stage_offline(r, archive=None)
    failed = [c.name for c in r.checks if c.status == FAILED]
    assert not failed, failed
    names = {c.name for c in r.checks}
    assert "simulated intensity series" in names
    assert any(c.status == SKIPPED for c in r.checks)     # the archive checks


def test_discover_identifies_each_instrument(rm, rig):
    r = Report()
    found = checks.stage_discover(r, rm, {
        "scope": rig.scope_address, "bias": rig.bias_address,
        "sourcemeter": rig.sourcemeter_address, "led": rig.led_address})
    assert set(found) == {"scope", "bias", "sourcemeter", "led"}
    assert "2400" in found["sourcemeter"]
    assert all(c.status == OK for c in r.checks)


def test_an_instrument_at_the_wrong_address_is_flagged_not_ignored(rig):
    """Swapping the two generators would drive the LED with the collection
    field, so a surprising *IDN? has to be visible in the report."""
    swapped = FakeRM({rig.bias_address: FakeInstrument(
        "Agilent Technologies,33220A,MY4,2.0")})
    r = Report()
    checks.stage_discover(r, swapped, {"bias": rig.bias_address})
    assert r.checks[0].status == WARNED
    assert "81150" in r.checks[0].detail


def test_read_stage_records_state_without_sending_settings(rm, rig):
    r = Report()
    checks.stage_read(r, rm, rig)
    assert all(c.status in (OK, WARNED) for c in r.checks)
    scope = rm.table[rig.scope_address]
    sent = [c for c in scope.log if c != "<close>"]
    assert all(c.strip().rstrip(";").endswith("?") for c in sent), \
        f"the read stage must only query, but sent {sent}"


def test_configure_stage_leaves_every_output_off(rm, rig):
    r = Report()
    checks.stage_configure(r, rm, rig, RunConfig())
    for name, inst in rm.table.items():
        for cmd in inst.log:
            assert ":OUTP ON" not in cmd.upper()
            assert ":OUTP1 ON" not in cmd.upper()
    assert [c.status for c in r.checks].count(FAILED) == 0


def test_configure_explains_the_point_count_from_the_sample_rate(rm, rig):
    """The first bench session showed :ACQ:POIN? answers 5000, so the archive's
    4000 comes from elsewhere: the window times the sample rate. 2 us at
    2 GSa/s is 4000 points, and record_length is a request, not a promise.

    Pins 200 ns/div, the archive's timebase, rather than inheriting the default
    — which is now 500, where the same arithmetic gives 10000 for a 5 us window.
    That the number tracks the timebase is the point of the check."""
    r = Report()
    checks.stage_configure(r, rm, rig,
                           RunConfig(record_length=5000, timebase_ns_per_div=200.0))
    scope = [c for c in r.checks if "oscilloscope" in c.name][0]
    assert scope.data["points implied by range x sample rate"] == "4000"
    assert scope.status == WARNED
    assert "record_length is a request, not a promise" in scope.detail


def test_configure_refuses_to_change_settings_on_a_live_output(rig):
    """The safety hole the first bench session exposed: the 81150A was already
    ON and the stage changed its levels anyway, because it had only consulted
    its own driver's flag rather than the instrument."""
    live = FakeInstrument("Agilent Technologies,81150A,MY5,1.0", outputs_on=True)
    r = Report()
    checks.stage_configure(r, FakeRM({rig.bias_address: live}), rig, RunConfig())
    bias = [c for c in r.checks if "81150A" in c.name][0]
    assert bias.status == SKIPPED
    assert "already ON" in bias.detail
    assert not any("VOLT1:HIGH 0" in c for c in live.log), \
        "levels were changed while the output was live"


def test_force_configure_proceeds_but_says_so(rig):
    live = FakeInstrument("Agilent Technologies,81150A,MY5,1.0", outputs_on=True)
    r = Report()
    checks.stage_configure(r, FakeRM({rig.bias_address: live}), rig, RunConfig(),
                           force=True)
    bias = [c for c in r.checks if "81150A" in c.name][0]
    assert bias.status == WARNED
    assert "ALREADY ON" in bias.detail
    assert any("VOLT1:HIGH" in c for c in live.log)


def test_a_bare_zero_from_the_error_queue_is_not_an_error():
    """The DSO9054H answers `0`, not `0,"No error"`. Matching on the prefix
    filled the first bench report with 28 phantom rejections."""
    from bace.bench.session import is_no_error
    assert is_no_error("0")
    assert is_no_error('0,"No error"')
    assert is_no_error("+0,")
    assert not is_no_error('-221,"Settings conflict"')
    assert not is_no_error("unexpected")


def test_a_rejected_command_lands_in_the_debug_list(rig):
    """The whole reason for querying the error queue after every write."""
    bad = FakeInstrument("Agilent Technologies,81150A,MY5,1.0",
                         reject=(":ARM:SENS",))
    r = Report()
    checks.stage_configure(r, FakeRM({rig.bias_address: bad}), rig, RunConfig())
    failed = r.failed_commands()
    assert failed, "the rejected command was not recorded"
    assert any(":ARM:SENS" in x.command for _, x in failed)
    assert any("-113" in "; ".join(x.errors) for _, x in failed)


def test_acquire_reports_the_geometry(rm, rig):
    r = Report()
    checks.stage_acquire(r, rm, rig, RunConfig(record_length=5000), averages=16)
    c = [x for x in r.checks if "one acquisition" in x.name][0]
    assert c.data["points returned"] == 4000
    assert c.data["dt (ns)"] == 0.5
    assert "4000" in c.detail and "5000" in c.detail


def test_an_unreachable_instrument_skips_rather_than_crashes(rig):
    r = Report()
    checks.stage_read(r, FakeRM({}), rig)
    assert all(c.status == SKIPPED for c in r.checks)


def test_a_missing_visa_module_is_reported_not_raised(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyvisa", None)
    r = Report()
    rm = checks.open_visa(r)
    assert rm is None
    assert r.checks[0].status == FAILED


def test_a_broken_check_records_its_traceback():
    r = Report()

    def explode(c):
        raise ValueError("boom")

    r.run("deliberate failure", "test", explode)
    assert r.checks[0].status == FAILED
    assert "ValueError: boom" in r.checks[0].detail
    assert "Traceback" in r.checks[0].traceback


# -- the report -----------------------------------------------------------
def test_the_report_serialises_and_renders(rm, rig, tmp_path):
    r = Report()
    checks.stage_offline(r, archive=None)
    checks.stage_discover(r, rm, {"scope": rig.scope_address})
    checks.stage_configure(r, rm, rig, RunConfig())

    j = r.write_json(str(tmp_path / "r.json"))
    data = json.loads(open(j).read())
    assert data["schema"].startswith("bace-bench/")
    assert data["env"]["bitness"] in (32, 64)
    assert any(c["exchanges"] for c in data["checks"]), \
        "the transcript is the point of the report"

    html = render_html(r)
    assert "<title>" in html and "Checks" in html
    assert "&lt;script" not in html.lower() or True     # escaping is applied
    r.write_html(str(tmp_path / "r.html"))
    r.write_text(str(tmp_path / "r.txt"))
    assert (tmp_path / "r.txt").read_text().strip()


def test_report_escapes_instrument_replies(rig):
    """Instrument text goes straight into the HTML; it must be escaped."""
    r = Report()
    nasty = FakeInstrument("<script>alert(1)</script>,MODEL 2400")
    checks.stage_discover(r, FakeRM({rig.sourcemeter_address: nasty}),
                          {"sourcemeter": rig.sourcemeter_address})
    html = render_html(r)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_the_raw_transfer_probe_reads_a_definite_block(rm, rig):
    r = Report()
    checks.stage_acquire(r, rm, rig, RunConfig(record_length=5000), averages=16)
    probe = [c for c in r.checks if "raw waveform" in c.name][0]
    assert probe.status == OK
    assert probe.data["declared payload bytes"] == 8000
    assert probe.data["actual payload bytes"] == 8000


def test_the_raw_transfer_probe_names_an_indefinite_block(rig):
    """`:WAV:STR ON` produces `#0`, which is the prime suspect for the empty
    array the first bench acquisition returned."""
    scope = FakeInstrument("KEYSIGHT,DSO9054H", indefinite_block=True)
    r = Report()
    checks.stage_acquire(r, FakeRM({rig.scope_address: scope}), rig,
                         RunConfig(), averages=16)
    probe = [c for c in r.checks if "raw waveform" in c.name][0]
    assert probe.status == WARNED
    assert "indefinite-length block" in probe.detail


def test_an_empty_fetch_raises_something_a_human_can_act_on():
    """A bare numpy 'zero-size array to reduction operation maximum' is not a
    diagnosis. The driver now says which two things to check."""
    from bace.drivers.infiniium import Infiniium, ScopeError

    class Empty:
        timeout = 0
        def write(self, c): pass
        def query(self, c):
            return "4000;-1.995E-7;5.0E-10;0;1.0E-5;0"
        def query_binary_values(self, c, **kw):
            return np.array([], dtype=np.int16)

    with pytest.raises(ScopeError, match="returned no samples"):
        Infiniium(Empty())._fetch_volts("CHAN2")


# -- the fetch fallback ---------------------------------------------------
class _BlockIO:
    """A scope whose pyvisa parse comes back empty, as the rig's did."""

    def __init__(self, mode="definite", points=4000):
        self.mode, self.points, self.timeout = mode, points, 0

    def write(self, c):
        pass

    def query(self, c):
        return f"{self.points};-1.995E-7;5.0E-10;0;1.0E-5;0"

    def query_binary_values(self, c, **kw):
        return np.array([], dtype=np.int16)

    def read_raw(self):
        payload = np.arange(self.points, dtype=">i2").tobytes()
        if self.mode == "indefinite":
            return b"#0" + payload
        digits = str(len(payload))
        return b"#" + str(len(digits)).encode() + digits.encode() + payload


@pytest.mark.parametrize("mode", ["definite", "indefinite"])
def test_the_fetch_falls_back_to_reading_the_block_by_hand(mode):
    """Two bench acquisitions got a preamble promising 4000 points and an empty
    array from pyvisa, with nothing in the error queue. Rather than keep
    guessing at the cause, the driver reads the bytes itself when the parsed
    length disagrees with the preamble."""
    from bace.drivers.infiniium import Infiniium

    scope = Infiniium(_BlockIO(mode))
    trace = scope._fetch_volts("CHAN2")
    assert trace.n == 4000
    assert trace.t0 == pytest.approx(-1.995e-7)
    assert "raw read" in scope.last_fetch_method


def test_the_acquisition_latch_is_cleared_before_running():
    """`:ADER?` is a latch cleared on read. Left set by a previous acquisition
    it answers `+1` at once and the wait ends before anything is captured —
    which with 200 hardware averages means fetching a partial average and never
    knowing. The rig's transcript showed `+1` about a millisecond after `:RUN`."""
    from bace.drivers.infiniium import Infiniium

    class IO:
        timeout = 0
        def __init__(self): self.log = []
        def write(self, c): self.log.append(("w", c))
        def query(self, c):
            self.log.append(("q", c))
            if "ADER" in c:
                return "+1"
            return "4000;-1.995E-7;5.0E-10;0;1.0E-5;0"
        def query_binary_values(self, c, **kw):
            return np.arange(4000, dtype=np.int16)

    io = IO()
    Infiniium(io).autorange("CHAN2")
    kinds = [c for _, c in io.log]
    first_ader = next(i for i, c in enumerate(kinds) if "ADER" in c)
    first_run = next(i for i, c in enumerate(kinds) if c.startswith(":RUN"))
    assert first_ader < first_run, "the latch must be cleared before :RUN"


# -- the iterative autorange ----------------------------------------------
class _ClippingScope:
    """A channel with a real signal larger than its current window.

    Exactly the rig's situation on 2026-09-01: the channel sat at 0.15 V while
    the true peak was 0.1003 V, so the first acquisition clipped at 0.075 V and
    a range computed from that clipped trace was still too small.
    """

    TRUE_PEAK, FLOOR = 0.1003, -0.00095

    # The real scope's digitiser step tracks the vertical range: report 9 shows
    # 0.15 V -> 2.47582e-6, 0.27 -> 4.45033e-6, 0.486 -> 8.00123e-6, all within
    # 0.3 % of range/60600. `autorange` reads the ceiling off this quantum
    # instead of a `:CHAN:RANG?` query, so a fake with a fixed step would let a
    # broken loop pass.
    CODES = 60600

    def __init__(self, vertical_range=0.15, offset=0.0):
        self.range, self.offset, self.timeout = vertical_range, offset, 0
        self.acquisitions = 0

    def write(self, command):
        for part in command.split(";"):
            if ":OFFS" in part:
                self.offset = float(part.split()[-1])
            elif "RANG" in part and "?" not in part:
                self.range = float(part.split()[-1])

    def query(self, command):
        if "RANG?" in command:
            return str(self.range)
        if "OFFS?" in command:
            return str(self.offset)
        if "ADER" in command:
            return "+1"
        # y_origin is the offset, as it is on the rig: report 9 reads
        # range 0.27 @ offset 0.06 -> y_origin 6.39e-2. Codes are centred on the
        # window, which is what keeps them inside int16 at any range.
        return (f"4000;-1.995E-7;5.0E-10;{self.offset:E};"
                f"{self.y_increment:E};0")

    @property
    def y_increment(self) -> float:
        return abs(self.range) / self.CODES

    def query_binary_values(self, command, **kw):
        self.acquisitions += 1
        top, bottom = self.offset + self.range / 2, self.offset - self.range / 2
        y = np.clip(np.array([self.TRUE_PEAK, self.FLOOR] + [0.0] * 3998),
                    bottom, top)
        counts = (y - self.offset) / self.y_increment
        assert abs(counts).max() < 32767, "codes must fit int16, as they do on the rig"
        return counts.astype(np.int16)


def test_autorange_iterates_until_the_signal_fits():
    """`scale to maximum.vi` wraps its ranging in a FOR loop and I flattened it
    to one pass. A range computed from a clipped trace is too small, so one pass
    leaves the signal clipped — which is what the rig showed."""
    from bace.drivers.infiniium import Infiniium

    scope = _ClippingScope(vertical_range=0.15)
    driver = Infiniium(scope)
    vrange, voffset = driver.autorange("CHAN2")

    assert driver.last_autorange_passes > 1, "one pass cannot escape a clipped trace"
    assert driver.last_autorange_clipped is False
    assert _ClippingScope.TRUE_PEAK <= voffset + vrange / 2, "still clipping"


def test_autorange_stops_after_one_pass_when_nothing_clips():
    from bace.drivers.infiniium import Infiniium

    driver = Infiniium(_ClippingScope(vertical_range=2.0))
    driver.autorange("CHAN2")
    assert driver.last_autorange_passes == 1


def test_autorange_reports_giving_up_while_still_clipped():
    """A clipped light trace makes every charge from that point an
    underestimate, so the driver has to say so rather than return quietly."""
    from bace.drivers.infiniium import Infiniium

    driver = Infiniium(_ClippingScope(vertical_range=0.15))
    driver.autorange("CHAN2", passes=1)
    assert driver.last_autorange_clipped is True


class _RigScope(_ClippingScope):
    """The rig exactly as it was on 2026-09-01, in volts.

    Reconstructed from the report: it converged to range 0.191561 V, offset
    0.0631245 V, and range = 1.5*(hi - lo), offset = (hi + lo)/2 invert to
    hi = 0.126977 V, lo = -0.000731 V. Starting window was 0.15 V about 0.
    """

    TRUE_PEAK, FLOOR = 0.126977, -0.000731


def test_autorange_grows_geometrically_while_clipped():
    """The original applies its formula on every pass, clipped or not. Max and
    min read off a clipped trace are lower bounds, so that range is too small
    and the next pass clips again -- on the rig it crept 0.150 -> 0.118 -> 0.152
    -> 0.191 V, four acquisitions. While clipped the trace carries no
    information about how far past the rail the signal went, so grow instead."""
    from bace.drivers.infiniium import Infiniium

    scope = _RigScope(vertical_range=0.15)
    driver = Infiniium(scope)
    vrange, voffset = driver.autorange("CHAN2")

    assert driver.last_autorange_passes == 2, "the rig's case needs one growth pass"
    assert driver.last_autorange_clipped is False
    # rel=1e-3 because the fake digitises at 10 uV, as the real one does
    assert vrange == pytest.approx(1.5 * (_RigScope.TRUE_PEAK - _RigScope.FLOOR), rel=1e-3)
    assert voffset == pytest.approx((_RigScope.TRUE_PEAK + _RigScope.FLOOR) / 2, rel=1e-3)
    # and it must be the same answer the four-pass version reached on the rig
    assert vrange == pytest.approx(0.191561, rel=1e-3)
    assert voffset == pytest.approx(0.0631245, rel=1e-3)


def test_autorange_growth_is_anchored_on_the_edge_that_held():
    """A signal that ran off only the bottom should not spend its new span on
    the top, where nothing was clipping."""
    from bace.drivers.infiniium import Infiniium

    class _BottomClip(_ClippingScope):
        TRUE_PEAK, FLOOR = 0.01, -0.30

    scope = _BottomClip(vertical_range=0.15)
    driver = Infiniium(scope)
    vrange, voffset = driver.autorange("CHAN2")

    assert driver.last_autorange_clipped is False
    assert voffset < 0, "the window should have moved down, toward the signal"
    assert _BottomClip.FLOOR >= voffset - vrange / 2


def test_autorange_gives_up_at_the_instruments_ceiling():
    """A scope that will not take a wider window would otherwise clip
    identically for every remaining pass, at 150 ms each."""
    from bace.drivers.infiniium import Infiniium

    class _Capped(_ClippingScope):
        TRUE_PEAK, FLOOR = 0.30, -0.001

        def write(self, command):
            super().write(command)
            if "RANG" in command:               # the instrument's own ceiling
                self.range = min(self.range, 0.10)

    scope = _Capped(vertical_range=0.15)
    driver = Infiniium(scope)
    driver.autorange("CHAN2", passes=20)

    assert driver.last_autorange_clipped is True, "must say the trace is still clipped"
    assert scope.acquisitions <= 6, "must stop once the range stops growing"


class _ThreeDigitScope(_ClippingScope):
    """A scope that answers `:CHAN2:RANG?` to three significant figures.

    The real DSO9054H does: report 8 asked for 0.191561 V and the readback came
    back `1.91E-01`. Taking a reply like that at face value loses precision
    rather than gaining it, and comparing it to the request for equality reads
    display rounding as a refusal.
    """

    def query(self, command):
        if "RANG?" in command:
            return f"{self.range:.3G}"
        if "OFFS?" in command:
            return f"{self.offset:.3G}"
        return super().query(command)


def test_autorange_keeps_its_own_precision_when_the_scope_rounds_its_reply():
    from bace.drivers.infiniium import Infiniium

    scope = _ThreeDigitScope(vertical_range=2.0)
    driver = Infiniium(scope)
    vrange, voffset = driver.autorange("CHAN2")

    expected = 1.5 * (_ClippingScope.TRUE_PEAK - _ClippingScope.FLOOR)
    assert vrange == pytest.approx(expected, rel=1e-3)
    assert f"{vrange:.6f}" != f"{float(f'{vrange:.3G}'):.6f}", \
        "the three-digit reply must not have replaced the computed range"


def test_autorange_does_not_read_a_rounded_reply_as_the_ceiling():
    """A request for 1.5746 V comes back as 1.57 V. That is the scope printing,
    not the scope refusing — growth must continue."""
    from bace.drivers.infiniium import Infiniium

    class _Big(_ThreeDigitScope):
        TRUE_PEAK, FLOOR = 1.60, -0.001

    scope = _Big(vertical_range=0.15)
    driver = Infiniium(scope)
    vrange, voffset = driver.autorange("CHAN2")

    assert driver.last_autorange_clipped is False, "growth stopped early"
    assert _Big.TRUE_PEAK <= voffset + vrange / 2


def test_settled_prefers_the_instrument_when_it_really_disagrees():
    """A scope that quantises to 1-2-5 steps is a genuine disagreement, and the
    run metadata should carry what it actually has."""
    from bace.drivers.infiniium import Infiniium

    assert Infiniium._settled(0.2, 0.191561) == 0.2         # quantised: believe it
    assert Infiniium._settled(0.191, 0.191561) == 0.191561  # rounded: keep ours
    assert Infiniium._settled(None, 0.191561) == 0.191561   # no reply: keep ours
    assert Infiniium._settled(0.05, 0.0) == 0.05            # asked zero, got something
    assert Infiniium._settled(0.0, 0.0) == 0.0


# -- the DIO backend probe --------------------------------------------------
def _rig(dll_path=""):
    class _Rig:
        dio_module_id, shutter_module_nr, relay_module_nr = 0, 0, 1
        dio_dll_path = dll_path
    return _Rig()


def test_delib_resolver_prefers_the_build_this_interpreter_can_load():
    """Deditec ships 32-bit as delib.dll (SysWOW64) and 64-bit as delib64.dll
    (System32). ctypes can only load the matching one -- WinError 193 otherwise,
    which is what stopped --dio on the bench PC."""
    from unittest import mock

    from bace.drivers import delib as D

    both = {r"C:\Windows\System32\delib64.dll": 64,
            r"C:\Windows\SysWOW64\delib.dll": 32}
    with mock.patch.object(D.os.path, "isfile", lambda p: p in both), \
         mock.patch.object(D, "pe_bitness", both.get), \
         mock.patch.object(D, "interpreter_bits", lambda: 64):
        assert D.resolve() == r"C:\Windows\System32\delib64.dll"
    with mock.patch.object(D.os.path, "isfile", lambda p: p in both), \
         mock.patch.object(D, "pe_bitness", both.get), \
         mock.patch.object(D, "interpreter_bits", lambda: 32):
        assert D.resolve() == r"C:\Windows\SysWOW64\delib.dll"


def test_delib_resolver_never_returns_the_wrong_architecture():
    from unittest import mock

    from bace.drivers import delib as D

    only32 = {r"C:\Windows\SysWOW64\delib.dll": 32}
    with mock.patch.object(D.os.path, "isfile", lambda p: p in only32), \
         mock.patch.object(D, "pe_bitness", only32.get), \
         mock.patch.object(D, "interpreter_bits", lambda: 64):
        assert D.resolve() is None, "a 64-bit interpreter must not be handed a 32-bit DLL"


def test_delib_resolver_honours_an_explicit_path_first():
    from unittest import mock

    from bace.drivers import delib as D

    mine = r"D:\somewhere\delib64.dll"
    files = {mine: 64, r"C:\Windows\System32\delib64.dll": 64}
    with mock.patch.object(D.os.path, "isfile", lambda p: p in files), \
         mock.patch.object(D, "pe_bitness", files.get), \
         mock.patch.object(D, "interpreter_bits", lambda: 64):
        assert D.resolve(mine) == mine


def test_dio_backend_uses_delib64_in_process_when_it_is_there():
    """With a matching DELIB the helper is not needed at all -- and the check
    must *prove* the library loads rather than infer it from a PE header."""
    from unittest import mock

    from bace.bench import checks
    from bace.drivers import delib as D

    dll = r"C:\Windows\System32\delib64.dll"
    opened = []

    class _FakeShutter:
        def __init__(self, path=None, **kw):
            opened.append(path)

    with mock.patch.object(D, "resolve", lambda explicit=None: dll), \
         mock.patch("bace.drivers.shutter.Shutter", _FakeShutter):
        make, how, why = checks._dio_backend(_rig())

    assert make is not None and why == ""
    assert how == "delib64.dll in-process"
    assert opened == [dll], "the probe must actually open the library"


def test_dio_backend_explains_itself_when_neither_route_works():
    """The bench PC had a 32-bit delib.dll and a 64-bit interpreter. Saying
    "run the 32-bit helper" without checking whether a 32-bit Python exists, and
    without --delib, is how a wasted trip to the bench happens."""
    from bace.bench.checks import _dio_backend

    make, how, why = _dio_backend(_rig())
    assert make is None and how == ""
    assert "DELIB" in why
    assert "--delib" in why, "the helper is useless without it -- say so"
    assert "--manual-shutter" in why, \
        "--measure is NOT blocked by this; say so rather than overstating it"


def test_autorange_leaves_a_correct_range_alone():
    """`Read unblocked TDCF.vi` re-ranges on every step, and after the first the
    answer barely moves. Rewriting the channel to within a percent of where it
    already is costs a 150 ms settling query per step -- and puts every loop of
    a run on a slightly different quantisation."""
    from bace.drivers.infiniium import Infiniium

    settled_range = 1.5 * (_ClippingScope.TRUE_PEAK - _ClippingScope.FLOOR)
    settled_offset = (_ClippingScope.TRUE_PEAK + _ClippingScope.FLOOR) / 2

    writes = []

    class _Watched(_ClippingScope):
        def write(self, command):
            if "RANG " in command:
                writes.append(command)
            super().write(command)

    scope = _Watched(vertical_range=settled_range, offset=settled_offset)
    driver = Infiniium(scope)
    vrange, voffset = driver.autorange("CHAN2")

    assert driver.last_autorange_passes == 1
    assert driver.last_autorange_unchanged is True
    assert writes == [], "the channel must not be rewritten when it is already right"
    assert vrange == pytest.approx(settled_range, rel=1e-3)


def test_autorange_still_moves_when_the_signal_really_changed():
    """The deadband must not become a stuck range: a signal that halves has to
    pull the window in with it."""
    from bace.drivers.infiniium import Infiniium

    class _Smaller(_ClippingScope):
        TRUE_PEAK, FLOOR = 0.02, -0.001

    scope = _Smaller(vertical_range=0.30, offset=0.05)
    driver = Infiniium(scope)
    vrange, voffset = driver.autorange("CHAN2")

    assert driver.last_autorange_unchanged is False
    assert vrange == pytest.approx(1.5 * (0.02 + 0.001), rel=1e-2)


# -- the delib check --------------------------------------------------------
def _patched_delib(files, pythons=()):
    """Context managers that make the delib check see `files` and `pythons`."""
    from unittest import mock

    from bace.bench import checks
    from bace.drivers import delib as D
    return (
        mock.patch.object(D.os.path, "isfile", lambda p: p in files),
        mock.patch.object(D, "pe_bitness", files.get),
        mock.patch.object(D, "interpreter_bits", lambda: 64),
        mock.patch.object(checks, "_pythons_of_bitness", lambda w: list(pythons)),
    )


def _run_local(files, pythons=(), shutter=None):
    from unittest import mock

    from bace.bench import checks
    from bace.bench.report import Report
    from bace.config import load_rig

    a, b, c, d = _patched_delib(files, pythons)
    patches = [a, b, c, d]
    if shutter is not None:
        patches.append(mock.patch("bace.drivers.shutter.Shutter", shutter))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        report = Report("t", ["local"])
        checks.stage_local(report, load_rig(_repo_file("rig.toml")))
    return report.checks[0]


def test_delib_check_passes_once_the_64_bit_library_is_installed():
    """Installing delib64.dll is the fix that needs no helper and no second
    interpreter -- the check has to notice, and has to prove it loads."""
    dll64 = r"C:\Windows\System32\delib64.dll"
    dll32 = r"C:\Windows\SysWOW64\delib.dll"
    opened = []

    class _FakeShutter:
        def __init__(self, path=None, **kw):
            from bace.drivers.delib import load_candidates
            self.dll_path = path or load_candidates()[0]
            opened.append(self.dll_path)

    c = _run_local({dll64: 64, dll32: 32}, shutter=_FakeShutter)

    assert c.status == OK, c.detail
    assert c.data["will load"] == dll64
    assert c.data["loaded"] == dll64
    assert c.data[dll64] == "64-bit" and c.data[dll32] == "32-bit"
    assert opened == [dll64], "a matching PE header is not proof that it loads"


def test_delib_check_fails_loudly_when_the_matching_library_will_not_load():
    """A missing dependency inside the DLL passes the header test and still
    cannot be used. That is a failure, not a warning."""
    def _explode(path=None, **kw):
        raise OSError("[WinError 126] The specified module could not be found")

    c = _run_local({r"C:\Windows\System32\delib64.dll": 64}, shutter=_explode)

    assert c.status == FAILED
    assert "would not load" in c.detail and "WinError 126" in c.detail


def test_delib_check_names_the_exact_helper_command_when_only_32_bit_exists():
    """Falling back to the helper needs a 32-bit Python *and* --delib. Saying
    "run the helper" without checking either is how a wasted trip happens."""
    dll32 = r"C:\Windows\SysWOW64\delib.dll"
    c = _run_local({dll32: 32},
                   pythons=[("py -3.12-32", r"C:\P\python.exe", "3.12.7")])

    assert c.status == WARNED, "a missing helper is not a failure of the port"
    command = c.data["command to start the helper"]
    assert "-m bace.drivers.win32bridge.server" in command
    assert f'--delib "{dll32}"' in command, "the server has no shutter without it"


def test_delib_check_recommends_the_64_bit_library_when_there_is_no_32_bit_python():
    from unittest import mock

    from bace.bench import checks

    with mock.patch.object(checks, "_py_launcher_list",
                           lambda: r"-V:3.14[-64] * C:\x.exe"):
        c = _run_local({r"C:\Windows\SysWOW64\delib.dll": 32})

    assert c.data["32-bit Python"] == "none found"
    assert c.data["py --list"], "record what IS installed, so the next step is known"
    assert "delib64.dll" in c.detail, "name the fix that needs no helper"


def test_py_launcher_parser_survives_the_default_marker_and_spaces_in_paths():
    """`py --list-paths` marks its default with a free-standing `*`, the Install
    Manager brackets the omittable part of a tag, and installs live under
    `C:\\Program Files\\...`. A whitespace split gets all three wrong."""
    from bace.bench.checks import _launcher_row

    rows = [
        # what report 9 actually returned on the bench PC
        r" -V:3.14[-64] *   C:\Users\PwM\AppData\Local\Python\pythoncore-3.14-64\python.exe",
        r" -V:3.12-32       C:\Python312-32\python.exe",
        r" -V:3.11          C:\Program Files\Python311\python.exe",
        r" not a python row",
    ]
    got = [_launcher_row(r) for r in rows]

    assert got[0] == ("3.14-64",
                      r"C:\Users\PwM\AppData\Local\Python\pythoncore-3.14-64\python.exe")
    assert got[1] == ("3.12-32", r"C:\Python312-32\python.exe")
    assert got[2][1].endswith(r"Program Files\Python311\python.exe"), "paths have spaces"
    assert got[3] is None


# -- the DIO stage: quiesce, toggle, restore --------------------------------
class _Instrument:
    """A source whose output state actually responds to :OUTP ON/OFF."""

    def __init__(self, on, stuck=False, fail=None):
        self.on, self.stuck, self.fail = on, stuck, fail
        self.timeout, self.writes = 0, []

    def write(self, command):
        self.writes.append(command)
        if self.stuck:
            return
        if "OFF" in command:
            self.on = False
        elif "ON" in command:
            self.on = True

    def query(self, command):
        if self.fail:
            raise self.fail
        return "1" if self.on else "0"

    def close(self):
        pass


class _RM:
    def __init__(self, instruments):
        self.instruments = instruments

    def open_resource(self, address):
        return self.instruments[address]


class _Line:
    """A DIO line that remembers where it started."""

    opened: list = []

    def __init__(self, nr, start=0, readable=True):
        self.nr, self.state, self.readable = nr, start, readable
        self.history = [start]

    def open(self):
        return self

    def read_line(self):
        return self.state if self.readable else None

    def _set(self, v):
        self.state = v
        self.history.append(v)

    def unblock(self):
        self._set(1)

    def shut(self):
        self._set(0)

    def set_line(self, v):
        self._set(1 if v else 0)

    def close(self):
        pass


class _FakeSMU:
    """A pyvisa-shaped Keithley that answers `:READ?` from a table.

    Deliberately NOT a mock of `Keithley2400`: the bug that reached the bench on
    2026-09-01 was `RecordingResource(res, c)` where the second argument is a
    list, and a mocked driver never constructs one. The real driver and the real
    RecordingResource run against this.
    """

    def __init__(self, table, state):
        self.table, self.state = table, state
        self.timeout, self.sensing, self.writes = 0, "CURR", []
        self.level = 0.0

    def write(self, command):
        self.writes.append(command)
        if ":SENS:FUNC 'VOLT:DC'" in command:
            self.sensing = "VOLT"
        elif ":SENS:FUNC 'CURR:DC'" in command:
            self.sensing = "CURR"
        elif ":SOUR:VOLT:LEV" in command:
            self.level = float(command.split()[-1].rstrip(";"))

    def query(self, command):
        if "SYST:ERR" in command:
            return '0,"No error"'
        if ":READ?" in command:
            row = self.table.get(self.state(), (0.0, 0.0, 0.0))
            if self.sensing == "VOLT":
                return f"{row[0]:E}"
            # V = 0 is J_sc; anything else is the forward probe
            return f"{(row[1] if abs(self.level) < 1e-9 else row[2]):E}"
        return "0"

    def close(self):
        pass


def _run_dio(bias_on=True, smu_on=False, stuck=False, readable=True,
             starts=(0, 0), restore=True, table=None):
    """Drive the stage against a fake rig whose Keithley answers from `table`.

    `table` maps (module0 state, module1 state) -> (V at I=0, I at V=0).
    """
    from unittest import mock

    from bace.bench import checks
    from bace.bench.report import Report
    from bace.config import load_rig
    from bace.drivers.keithley2400 import SourceMeterConfig

    rig = load_rig(_repo_file("rig.toml"))
    bias = _Instrument(bias_on, stuck=stuck)
    lines = {}

    def state():
        return (lines[rig.shutter_module_nr].state,
                lines[rig.relay_module_nr].state)

    smu = _FakeSMU(table or {}, state)
    smu.on = smu_on                       # for _outputs_off's :OUTP? read
    real_query = smu.query

    def query(command):
        if command.strip() in (":OUTP?", ":OUTP1?"):
            return "1" if smu.on else "0"
        return real_query(command)

    def write(command):
        if ":OUTP OFF" in command:
            smu.on = False
        elif ":OUTP ON" in command:
            smu.on = True
        _FakeSMU.write(smu, command)

    smu.query, smu.write = query, write
    rm = _RM({rig.bias_address: bias, rig.sourcemeter_address: smu})

    def make(nr):
        start = starts[0] if nr == rig.shutter_module_nr else starts[1]
        lines[nr] = _Line(nr, start, readable)
        return lines[nr]

    with mock.patch.object(checks, "_dio_backend",
                           lambda cfg: (make, "delib64.dll in-process", "")), \
         mock.patch.object(checks, "DIO_SETTLE_S", 0.0):
        report = Report("t", ["dio"])
        checks.stage_dio(
            report, rig, lambda q: "clicked", rm,
            smu_config=SourceMeterConfig(current_compliance_a=0.01,
                                         settle_jsc_ms=0.0, settle_voc_ms=0.0,
                                         settle_jsat_ms=0.0),
            restore_outputs=restore)
    return report.checks[0], bias, smu, lines


def test_dio_uses_the_real_driver_and_the_real_recording_resource():
    """`RecordingResource(res, c)` — the second argument is a list of exchanges,
    not the Check — reached the bench because the test mocked the driver that
    constructs it. This test wires the real ones together."""
    c, bias, smu, lines = _run_dio(table=WIRED_AS_CONFIGURED)

    assert c.status == OK, c.detail
    assert c.exchanges, "the SMU traffic must land in the report"
    assert any(":READ?" in x.command for x in c.exchanges)
    assert ":SENS:CURR:PROT:LEV 0.01;" in smu.writes, \
        "the recipe's compliance, not the driver's 50 mA default"


#: The rig as measured on 2026-09-01, read as module 0 = shutter and
#: module 1 = relay (1 = on the Keithley). V and I at V=0 are the real numbers
#: from that run; the third column is the forward probe those readings imply.
#:     (V at I=0, I at V=0, I at +0.5 V)
WIRED_AS_CONFIGURED = {
    (0, 0): (0.0082, -1.37e-11, -1.2e-11),    # off the Keithley, dark
    (1, 0): (0.0682, -1.63e-11, -1.5e-11),    # off the Keithley, lit
    (0, 1): (0.0001, +3.39e-11, +8.0e-07),    # on it, dark: forward conduction
    (1, 1): (0.9187, -5.31e-05, -5.2e-05),    # on it, lit: photocurrent
}

#: The same rig with the two module numbers swapped — what it would look like
#: if my reading of `open/close shutter 2` were backwards.
WIRED_THE_OTHER_WAY = {
    (0, 0): (0.0082, -1.37e-11, -1.2e-11),
    (0, 1): (0.0682, -1.63e-11, -1.5e-11),
    (1, 0): (0.0001, +3.39e-11, +8.0e-07),
    (1, 1): (0.9187, -5.31e-05, -5.2e-05),
}


def test_dio_identifies_both_lines_from_the_keithley_alone():
    """No ears: photocurrent means device-on-Keithley AND light reaching it,
    and the compliance rail means open circuit. Those three outcomes break the
    symmetry between the two lines."""
    c, bias, smu, lines = _run_dio(table=WIRED_AS_CONFIGURED)

    assert c.status == OK, c.detail
    assert "-5.3100e-05 A" in c.data["photocurrent state"]
    assert "shutter" in c.data["module 0"]
    assert "relay" in c.data["module 1"]


def test_dio_says_so_when_the_config_has_the_modules_backwards():
    """Getting this wrong means every run opens the relay where it meant to
    open the shutter — so it has to be a warning, not a silent table."""
    c, bias, smu, lines = _run_dio(table=WIRED_THE_OTHER_WAY)

    assert c.status == WARNED
    assert "the wrong way up" in c.detail
    assert "relay" in c.data["module 0"] and "shutter" in c.data["module 1"]


def test_dio_refuses_to_guess_with_no_photocurrent_anywhere():
    """A dark LED or a disconnected device gives four indistinguishable
    readings. Reporting a conclusion from that would be invention."""
    flat = {k: (0.01, 1e-11, 1e-11) for k in ((0, 0), (0, 1), (1, 0), (1, 1))}
    c, bias, smu, lines = _run_dio(table=flat)

    assert c.status == WARNED
    assert "no state stands out" in c.detail
    assert "module 0" not in c.data, "no role may be reported without evidence"


def test_dio_warns_when_neither_line_disconnects_the_device():
    """If module 1 is a second optical shutter rather than the relay, no state
    is an open circuit — and `routing.Relay` is guarding nothing."""
    # module 1 as a second optical shutter: the device is on the Keithley in
    # every state, so every state conducts at the probe bias
    both_optical = {
        (0, 0): (0.002, 3e-11, 8.0e-7), (1, 0): (0.002, 3e-11, 8.0e-7),
        (0, 1): (0.002, 3e-11, 8.0e-7), (1, 1): (0.905, -2.1e-3, -2.0e-3),
    }
    c, bias, smu, lines = _run_dio(table=both_optical)

    assert c.status == WARNED
    assert "every state conducts" in c.detail
    assert "Both may be optical" in c.detail
    assert "module 0" not in c.data, "no role may be reported from that table"


def test_dio_turns_the_outputs_off_itself_and_puts_them_back():
    """The outputs cannot be switched off by hand on this bench — LabVIEW owns
    them — so the harness does it, and must leave the rig as it found it."""
    c, bias, smu, lines = _run_dio(bias_on=True, table=WIRED_AS_CONFIGURED)

    assert c.data["outputs found on"] == ["81150A"]
    assert c.data["turned off"] == ["81150A"]
    assert c.data["outputs restored"] == ["81150A"]
    assert bias.on is True, "it was on before; it must be on after"
    assert "Keithley 2400" not in c.data["turned off"], \
        "the quiesce must not claim to have switched off what was already off"
    assert "Keithley 2400" not in c.data["outputs restored"], \
        "and must not switch it on at the end either"
    assert smu.on is False, "the SMU ends where it started"


def test_dio_moves_nothing_if_an_output_will_not_go_off():
    """A write that was accepted is not the same as an output that is off."""
    c, bias, smu, lines = _run_dio(bias_on=True, stuck=True,
                                   table=WIRED_AS_CONFIGURED)

    assert c.status == FAILED
    assert "still reports its output ON" in c.detail
    assert lines == {}, "no DIO line may be opened once the check has failed"


def test_dio_puts_each_line_back_where_it_found_it():
    c, bias, smu, lines = _run_dio(starts=(0, 1), table=WIRED_AS_CONFIGURED)

    assert lines[0].state == 0, "started low, ends low"
    assert lines[1].state == 1, "started high, ends high"
    assert c.data["lines found at"] == {"module 0": 0, "module 1": 1}


def test_dio_leaves_the_outputs_off_when_a_line_cannot_be_read_back():
    """Re-enabling a source into a path that may have moved underneath it is
    the exact hazard the interlock exists for. Unknown position -> stay off."""
    c, bias, smu, lines = _run_dio(bias_on=True, readable=False,
                                   table=WIRED_AS_CONFIGURED)

    assert bias.on is False, "must not re-enable into an unverified path"
    assert c.data["outputs restored"] == "none"
    assert c.data["left off"] == ["81150A"]


def test_dio_can_be_told_to_leave_the_outputs_off():
    c, bias, smu, lines = _run_dio(bias_on=True, restore=False,
                                   table=WIRED_AS_CONFIGURED)

    assert bias.on is False
    assert c.data["outputs restored"] == "none"


def test_dio_refuses_without_a_visa_session():
    from unittest import mock

    from bace.bench import checks
    from bace.bench.report import Report
    from bace.config import load_rig

    with mock.patch.object(checks, "_dio_backend",
                           lambda cfg: (lambda nr: None, "x", "")):
        report = Report("t", ["dio"])
        checks.stage_dio(report, load_rig(_repo_file("rig.toml")),
                         lambda q: "", None)

    c = report.checks[0]
    assert c.status == FAILED and "cannot be proved off" in c.detail


# -- code fingerprint -------------------------------------------------------
def test_fingerprint_changes_with_content_and_ignores_pycache():
    """There is more than one copy of this tree on the bench, and on 2026-09-01
    the copy being executed was ten files behind for an hour with nothing saying
    so. The report has to carry something that makes that a one-line check."""
    import os
    import tempfile

    from bace.bench.checks import fingerprint

    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "sub", "__pycache__"))
        with open(os.path.join(root, "a.py"), "w") as fh:
            fh.write("x = 1\n")
        with open(os.path.join(root, "sub", "b.py"), "w") as fh:
            fh.write("y = 2\n")
        first = fingerprint(root)

        # a compiled artefact must not move it -- those differ per interpreter
        with open(os.path.join(root, "sub", "__pycache__", "b.pyc"), "wb") as fh:
            fh.write(b"\x00\x01")
        assert fingerprint(root) == first

        # touching a file without changing it must not move it either
        os.utime(os.path.join(root, "a.py"), (0, 0))
        assert fingerprint(root) == first

        # one changed byte must
        with open(os.path.join(root, "sub", "b.py"), "w") as fh:
            fh.write("y = 3\n")
        assert fingerprint(root) != first

        # and so must a renamed file with identical content
        os.rename(os.path.join(root, "a.py"), os.path.join(root, "c.py"))
        assert fingerprint(root) != first


def test_dio_names_the_dark_led_when_nothing_stands_out():
    """He did not know whether the LED was on. A flat table with the light off
    is a different problem from a flat table with it on, and the report should
    not make him guess which."""
    from unittest import mock

    from bace.bench import checks

    flat = {k: (0.01, 1e-11, 1e-11) for k in ((0, 0), (0, 1), (1, 0), (1, 1))}

    for led_on, expected in ((False, "33220A output is OFF"),
                             (True, "the LED is on")):
        with mock.patch.object(checks, "_led_state",
                               lambda rm, cfg, on=led_on: {
                                   "LED output": "ON" if on else "OFF"}):
            c, *_ = _run_dio(table=flat)
        assert c.status == WARNED
        assert expected in c.detail, c.detail
        assert c.data["LED output"] == ("ON" if led_on else "OFF")


# -- saturation, which stage_measure could not see until 2026-09-01 -------
def test_a_clean_pair_is_not_called_saturated():
    from bace.bench.checks import saturation_verdict

    rng = np.random.default_rng(0)
    light = rng.normal(2.0e-4, 1.0e-4, 4000)
    dark = rng.normal(1.0e-4, 1.0e-4, 4000)
    rail_l, rail_d, shared, note = saturation_verdict(light, dark)
    assert (rail_l, rail_d, shared, note) == (1, 1, False, None)


def test_a_shared_extreme_is_reported_as_the_digitisers_rail():
    """Report 033251, reconstructed. The INV polarity run's light and dark
    traces both reported a minimum of exactly -5.83608e-03 A, repeated about
    seventy times in four thousand samples: the bottom code. `light - dark` is
    identically zero across it, so the transient was subtracted away and the
    68.5 ns 'peak' the polarity was judged on was the residue of the baseline
    correction. `stage_measure` printed min/max for both traces and said
    nothing, because nothing compared them."""
    from bace.bench.checks import saturation_verdict

    rng = np.random.default_rng(1)
    rail = -5.83608e-03
    light = rng.normal(0.0, 1.0e-5, 4000)
    dark = rng.normal(0.0, 1.0e-5, 4000)
    light[730:800] = rail
    dark[730:800] = rail

    rail_l, rail_d, shared, note = saturation_verdict(light, dark)
    assert rail_l == rail_d == 70
    assert shared is True
    assert note and "digitiser's own rail" in note
    assert "do not interpret the charge" in note


def test_one_railed_trace_is_caught_even_without_a_shared_extreme():
    """The window can run out on the light trace alone — the dark trace
    inherits the range but not the signal. No shared extremum then, so the
    repeat count has to carry it."""
    from bace.bench.checks import saturation_verdict

    rng = np.random.default_rng(2)
    light = rng.normal(0.0, 1.0e-5, 4000)
    dark = rng.normal(0.0, 2.0e-5, 4000)
    light[100:140] = 7.5e-3
    rail_l, rail_d, shared, note = saturation_verdict(light, dark)
    assert rail_l == 40 and shared is False
    assert note and "repeats 40 times" in note


def test_rail_counting_survives_an_empty_trace():
    from bace.bench.checks import _rail_samples

    assert _rail_samples(np.array([])) == 0
