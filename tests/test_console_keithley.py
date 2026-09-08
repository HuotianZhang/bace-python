"""The standalone Keithley console — `bace.consoles.keithley`.

Two things this file is here to hold down, both of which are what "standalone"
means rather than what the panel does:

* **it imports nothing from `bace.service` and nothing from `ui/`**, and it
  serves HTTP from the standard library — so it comes up on a checkout with
  neither the `service` extra nor a VISA backend, which is the whole point of
  a console you run in front of an instrument;
* **one thread touches the instrument.** Every request goes through the
  panel's worker, and the free-running display is what that worker does
  between jobs — so a reading can never land in the middle of a click.

Driven over real HTTP against a real server on an ephemeral port, because the
transport is the part a stdlib server is most likely to get wrong (a missing
Content-Length hangs a browser and no unit test would see it).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from bace.consoles.keithley.__main__ import build_parser, is_loopback, main
from bace.consoles.keithley.panel import (JOB_TIMEOUT_S, KeithleyPanel, PanelPending,
                                          PanelRefused, PanelUnavailable, source_args)
from bace.consoles.keithley.server import make_server
from bace.drivers.keithley2400 import PanelSetup, SourceMeterConfig
from bace.drivers.simulated import make_bench

CEILING = {"current_a": 0.05, "voltage_v": 5.0}


def make_panel(*, lit: bool = True, ceiling: dict | None = None,
               defaults: SourceMeterConfig | None = None) -> KeithleyPanel:
    sim = make_bench(seed=1)
    sim.bench.relay = "sourcemeter"
    if lit:
        sim.bench.shutter_open = True
        sim.bench.led_mode = "DC"
        sim.bench.led_drive_v = 1.020
    return KeithleyPanel(sim.smu, ceiling=ceiling or dict(CEILING),
                         defaults=defaults or SourceMeterConfig(),
                         identity="simulated 2400", address="simulated", mode="sim")


@pytest.fixture
def panel():
    p = make_panel()
    p.start()
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def console(panel):
    """The panel behind a real server on an ephemeral port; `(get, post)`."""
    server = make_server(panel, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(method: str, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            base + path, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(request, timeout=10) as answer:
                raw = answer.read()
                if "json" not in (answer.headers.get("Content-Type") or ""):
                    return answer.status, {"body": raw,
                                           "length": answer.headers.get("Content-Length")}
                return answer.status, json.loads(raw or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, (json.loads(raw) if raw.startswith(b"{") else {"body": raw})

    try:
        yield call
    finally:
        server.shutdown()
        server.server_close()


# -- what "standalone" means --------------------------------------------------
def _module_level_imports(module) -> set[str]:
    """Every name imported at the top level of a module, read off its source.

    The AST and not `dir()`: what matters is what importing this module *pulls
    in*, and an import inside a function (which `pyvisa` is) does not.
    """
    import ast
    tree = ast.parse(open(module.__file__, encoding="utf-8").read())
    names: set[str] = set()
    for node in tree.body:                                   # top level only
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


def test_the_console_imports_neither_the_service_nor_a_visa_backend():
    """The claim in the module docstring, asserted rather than left in prose.

    `bace.service` is FastAPI + uvicorn behind an extra and `pyvisa` is behind
    another; a console that pulled in either at import time would not come up
    on the machine it is written for. `pyvisa` *is* imported by `_open_real`,
    inside the function, so a real bench reaches it and `--sim` never does.
    """
    from bace.consoles.keithley import __main__ as cli
    from bace.consoles.keithley import panel as panel_module
    from bace.consoles.keithley import server as server_module

    for module in (panel_module, server_module, cli):
        for name in _module_level_imports(module):
            assert "service" not in name, f"{module.__name__} imports {name}"
            assert name not in ("fastapi", "uvicorn", "pyvisa", "websockets"), name
    assert "pyvisa" not in _module_level_imports(cli), "opened inside `_open_real`"
    assert "pyvisa" in open(cli.__file__, encoding="utf-8").read(), \
        "and the real bench still reaches it"
    # The panel is the instrument's; opening a session is the CLI's job.
    assert "pyvisa" not in open(panel_module.__file__, encoding="utf-8").read()


def test_the_page_is_one_file_that_fetches_nothing_from_a_network():
    """A bench PC may have no route out, and a console whose fonts or scripts
    came from a CDN would come up unstyled or not at all."""
    from bace.consoles.keithley.server import PAGE
    page = open(PAGE, encoding="utf-8").read()
    assert "<script" in page and page.count("<script") == 1
    for forbidden in ("http://", "https://", "//cdn", "import "):
        assert forbidden not in page.replace("http://127.0.0.1", ""), forbidden


# -- the panel ----------------------------------------------------------------
def test_the_panel_opens_on_the_configuration_and_is_not_applied_yet(panel):
    state = panel.state()
    assert state["panel"] is None, "nothing has told the instrument what to source"
    assert state["output"] is False and state["reading"] is None
    assert state["defaults"]["function"] == "voltage" and state["defaults"]["unit"] == "V"
    assert state["instrument"]["mode"] == "sim"


def test_the_defaults_never_exceed_the_bench_ceiling():
    """`run.toml` may name a compliance and `rig.toml` names the ceiling. The
    panel opens under both — a default nobody chose must not be the one value
    on this bench that is over the limit."""
    p = make_panel(ceiling={"current_a": 0.002, "voltage_v": 1.0},
                   defaults=SourceMeterConfig(current_compliance_a=0.05,
                                              voltage_compliance_v=2.0))
    assert p.defaults.current_compliance_a == 0.002
    assert p.defaults.voltage_compliance_v == 1.0


def test_the_output_will_not_switch_on_until_something_has_set_a_source(panel):
    with pytest.raises(PanelRefused) as exc:
        panel.set_output(True)
    assert exc.value.level == "warn" and "set the source first" in exc.value.text
    assert panel.state()["output"] is False


def test_sourcing_zero_amps_reads_the_open_circuit_voltage(panel):
    panel.set_source({"function": "I", "level": 0.0, "voltage_compliance_v": 2.0})
    assert panel.state()["panel"]["unit"] == "A"
    state = panel.set_output(True)
    reading = state["reading"]
    assert 0.8 < reading["volts"] < 1.0, "0 A into a lit cell is V_oc"
    assert reading["compliance"] is False and reading["function"] == "current"


def test_changing_the_function_does_not_carry_the_level_into_the_other_unit(panel):
    """A panel at 1.5 V asked for current would otherwise ask for 1.5 *amps* --
    refused by the ceiling, so a click on `A` fails with a sentence about a
    number the operator never typed. The instrument's own keys keep a level per
    function; 0 is where a change lands here, and it is the safe end of both
    ranges (0 V is J_sc, 0 A is V_oc)."""
    panel.set_source({"function": "V", "level": 1.5, "source_range": 2.0})
    state = panel.set_source({"function": "I"})
    assert state["panel"]["function"] == "current"
    assert state["panel"]["level"] == 0.0
    assert state["panel"]["source_range"] is None, "a range in volts means nothing in amps"
    # A body that says both is taken at its word.
    both = panel.set_source({"function": "V", "level": 0.4})
    assert (both["panel"]["function"], both["panel"]["level"]) == ("voltage", 0.4)
    # And a level moved without touching the function is just a level.
    assert panel.set_source({"level": 0.6})["panel"]["level"] == 0.6


def test_the_display_goes_blank_with_the_output(panel):
    """The last numbers were a measurement of a moment that has passed. Left on
    screen with the source off, nothing would say so."""
    panel.set_source({"function": "I", "level": 0.0})
    assert panel.set_output(True)["reading"] is not None
    assert panel.set_output(False)["reading"] is None


def test_the_compliance_light_is_the_instrument_saying_it_is_clamping(panel):
    panel.set_source({"function": "V", "level": 1.5, "current_compliance_a": 0.01})
    reading = panel.set_output(True)["reading"]
    assert reading["compliance"] is True
    assert abs(reading["amps"]) == pytest.approx(0.01)
    assert reading["volts"] < 1.5, "the source cannot hold its level at that current"


def test_the_ceiling_holds_a_typed_level_and_not_only_a_compliance(panel):
    """Nowhere else in this project can a level be typed straight onto the
    device — a sweep's ends come from a module's parameters and V_oc/J_sc
    source zero — so this is the one surface where `rig.toml`'s two keys have
    to hold a level as well."""
    for body, needle in (({"current_compliance_a": 0.5}, "max_current_compliance_a"),
                         ({"voltage_compliance_v": 9.0}, "max_voltage_compliance_v"),
                         ({"function": "V", "level": 9.0}, "max_voltage_compliance_v"),
                         ({"function": "I", "level": 0.5}, "max_current_compliance_a")):
        with pytest.raises(PanelRefused) as exc:
            panel.set_source(body)
        assert exc.value.level == "crit" and needle in exc.value.text
    assert panel.state()["panel"] is None, "nothing refused reached the instrument"


def test_the_body_is_typed_and_a_bad_value_names_its_field():
    for body, message in (({"nplc": "fast"}, "nplc: 'fast' is not a number"),
                          ({"averaging": 1.5}, "not a whole number"),
                          ({"function": "resistance"}, "function:"),
                          ({"terminals": "SIDE"}, "terminals:"),
                          ({"four_wire": "maybe"}, "neither true nor false"),
                          ({"knob": 1}, "unknown field")):
        with pytest.raises(ValueError, match=message):
            source_args(body)
    assert source_args({"source_range": ""})["source_range"] is None, "empty is autorange"
    assert source_args({"function": "v"})["function"] == "voltage"
    assert source_args({"function": "I"})["function"] == "current"


def test_the_display_free_runs_and_skips_what_it_cannot_read(panel):
    """The worker reads between jobs. **Nothing to read is not a failure**:
    with the output off the 2400 shows dashes and `read_panel` raises, and a
    console that counted that as a fault would be telling the operator an
    instrument that is answering perfectly well had stopped."""
    panel.set_poll(0.1)
    panel.set_source({"function": "I", "level": 0.0})
    time.sleep(0.5)
    quiet = panel.state()["poll"]
    assert quiet["readings"] == 0 and quiet["failures"] == 0, "the output is off"

    panel.set_output(True)
    time.sleep(0.5)
    busy = panel.state()["poll"]
    assert busy["readings"] > quiet["readings"] and busy["failures"] == 0
    assert busy["running"] is True and busy["interval_s"] == 0.1

    assert panel.set_poll(None)["poll"]["running"] is False
    stopped = panel.state()["poll"]["readings"]
    time.sleep(0.3)
    assert panel.state()["poll"]["readings"] == stopped
    assert panel.state()["output"] is True, "stopping a display is not stopping a source"


def test_the_display_starts_the_moment_it_is_switched_on(panel):
    """With no display running the worker is asleep in `queue.get` with no
    timeout — forever, until something is submitted. So switching the display
    on has to wake it (`_Bus.set_idle`), or it would not start until the next
    click and the operator would think the switch had not taken."""
    panel.set_source({"function": "I", "level": 0.0})
    panel.set_output(True)
    before = panel.state()["poll"]["readings"]
    panel.set_poll(0.1)
    time.sleep(0.35)
    assert panel.state()["poll"]["readings"] > before


def test_an_out_of_range_display_interval_is_refused(panel):
    with pytest.raises(ValueError, match="between"):
        panel.set_poll(0.0)
    with pytest.raises(ValueError, match="between"):
        panel.set_poll(3600.0)


# -- what the review of #86 turned up ----------------------------------------
def test_the_output_route_never_coerces_a_value_into_a_live_source(console):
    """`bool("false")` is `True` in Python, so `{"on": "false"}` -- a request
    that plainly means off -- switched the source **on**. This is the one
    route on this console that puts current into somebody's device, so a value
    it does not recognise is a 422 and never a guess."""
    console("POST", "/api/source", {"function": "I", "level": 0.0})
    for body in ({"on": "false"}, {"on": "no"}, {"on": "off"}, {"on": 0}):
        status, out = console("POST", "/api/output", body)
        assert status == 200, body
        assert out["output"] is False, f"{body} must not energise the source"
    for body in ({"on": "maybe"}, {"on": "onn"}, {"on": []}, {"on": None},
                 {"on": "2"}, {"on": {}}):
        status, out = console("POST", "/api/output", body)
        assert status == 422, f"{body} -> {status}"
        assert "on:" in out["error"]
        assert out["output"] is False, "and nothing reached the instrument"
    for body in ({"on": True}, {"on": "true"}, {"on": "on"}, {"on": 1}):
        assert console("POST", "/api/output", body)[1]["output"] is True, body
        console("POST", "/api/output", {"on": False})


def test_a_read_may_take_as_long_as_the_settings_it_was_taken_under(panel):
    """NPLC 10 and a 100-deep filter are both legal on a 2400 and both
    accepted here, and that pair is 80 s of integration (four apertures per
    averaged reading). A flat 30 s wait answers a healthy read with "the
    instrument did not answer" — so the wait is the driver's own budget."""
    panel.set_source({"function": "I", "level": 0.0})
    assert panel.read_budget_s() == pytest.approx(JOB_TIMEOUT_S), "the defaults are quick"
    panel.set_source({"nplc": 10.0, "averaging": 100})
    integration_s = 100 * 4 * 10.0 / 50.0
    assert integration_s == 80.0, "the pair the panel accepts"
    assert panel.read_budget_s() > integration_s, "and the wait clears it"
    assert panel.shutdown_budget_s() > panel.read_budget_s(), "with room for the off"


def test_a_job_the_caller_gave_up_on_does_not_reach_the_instrument_later(panel):
    """Otherwise a request answered with "the instrument did not answer" still
    lands, minutes afterwards, on a bench whose operator was told it failed."""
    started, release = threading.Event(), threading.Event()

    def block() -> None:
        started.set()
        release.wait(10)

    late = []
    panel._bus.submit(block)
    assert started.wait(2), "the worker took the blocker"
    with pytest.raises(PanelRefused, match="did not answer"):
        panel._bus.do(lambda: late.append("ran"), timeout_s=0.2)
    release.set()
    time.sleep(0.3)
    assert late == [], "the job the caller gave up on was dropped, not deferred"


def test_shutdown_waits_for_a_read_in_flight_and_still_switches_the_output_off():
    """Ctrl-C during a read that legally takes tens of seconds: the queued
    output-off used to be abandoned — the worker finished its read, saw the
    stop flag, and exited without running it, leaving a daemon thread's
    process to end with the source driving."""
    p = make_panel()
    p.start()
    p.set_source({"function": "I", "level": 0.0})
    p.set_output(True)
    assert p.smu.output_enabled is True

    slow = threading.Event()
    p._bus.submit(lambda: slow.wait(1.5))       # a read still in flight
    time.sleep(0.1)
    assert p.close() is True, "close waited for it, then ran the output-off"
    assert p.smu.output_enabled is False


def test_shutdown_says_so_when_it_cannot_confirm_the_output_off():
    """The one case that cannot be fixed from here: a worker inside a VISA
    call that never returns. Interrupting it would mean writing to the bus
    from a second thread, which is what this design forbids — so `close`
    answers False and the caller says it out loud."""
    p = make_panel()
    p.start()
    p.set_source({"function": "I", "level": 0.0})
    p.set_output(True)
    stuck = threading.Event()
    p._bus.submit(lambda: stuck.wait(30))
    time.sleep(0.1)
    assert p.close_with_budget(0.3) is False
    assert p.smu.output_enabled is True, "and it is honest about why"
    stuck.set()


def test_switching_the_output_off_is_never_dropped_behind_a_slow_read(panel):
    """The display may be mid-tick, and a legal tick is 80 s (NPLC 10, a
    100-deep filter). The off used to wait the 30 s floor, time out, and be
    *discarded* by the cancellation added for the previous round — so pressing
    OFF left the source driving. It is never cancelled now: a slow one raises
    `PanelPending` and still runs."""
    panel.set_source({"function": "I", "level": 0.0})
    panel.set_output(True)
    assert panel.smu.output_enabled is True
    assert panel.read_budget_s() >= JOB_TIMEOUT_S

    released = threading.Event()
    panel._bus.submit(lambda: released.wait(3))          # a tick in flight
    time.sleep(0.1)
    with pytest.raises(PanelPending, match="It will run"):
        panel._bus.do(lambda: panel.smu.disable_output(), timeout_s=0.2,
                      cancel_on_timeout=False)
    released.set()
    deadline = time.monotonic() + 3
    while panel.smu.output_enabled and time.monotonic() < deadline:
        time.sleep(0.02)
    assert panel.smu.output_enabled is False, "queued, and it ran"


def test_shutdown_refuses_new_work_so_a_late_request_cannot_re_energise():
    """`ThreadingHTTPServer` runs each request on a daemon thread and
    `server_close()` does not wait for the ones already accepted. A straggler
    inside `set_output(True)` could put an ON into the queue *behind* the
    shutdown OFF; the drain ran both, the process exited with the source
    driving, and `close` reported the off confirmed — because it had been, a
    moment earlier."""
    p = make_panel()
    p.start()
    p.set_source({"function": "I", "level": 0.0})
    p.set_output(True)

    released = threading.Event()
    p._bus.submit(lambda: released.wait(2))              # a read in flight
    time.sleep(0.05)
    late: list[str] = []

    def straggler() -> None:
        time.sleep(0.15)                                  # lands during close
        try:
            p._bus.submit(lambda: late.append("energised after the off"))
        except PanelUnavailable as exc:
            late.append(f"refused: {exc}")

    thread = threading.Thread(target=straggler, daemon=True)
    thread.start()
    assert p.close() is True
    thread.join(3)
    released.set()
    time.sleep(0.2)
    assert p.smu.output_enabled is False
    assert late and late[0].startswith("refused: "), late
    assert "shutting down" in late[0]


def test_shutdown_drops_work_that_had_not_started():
    """At shutdown the only instrument operation that matters is the output
    going off. A queued source change is work nobody is waiting for any more,
    and running it on the way out is a bench left somewhere nobody chose."""
    p = make_panel()
    p.start()
    p.set_source({"function": "I", "level": 0.0})
    ran: list[str] = []
    released = threading.Event()
    p._bus.submit(lambda: released.wait(2))
    time.sleep(0.05)
    p._bus.submit(lambda: ran.append("should never run"))
    assert p.close() is True
    released.set()
    time.sleep(0.2)
    assert ran == []
    assert p.smu.output_enabled is False


def test_an_output_on_whose_first_read_fails_still_answers_with_the_state(panel):
    """The ON has already landed, so a bare 500 carrying no state would leave
    the page showing a source that is off while it is driving. The reading is
    a nicety; the output change is the operation."""
    def broken():
        raise RuntimeError("the 2400 did not answer")

    panel.set_source({"function": "I", "level": 0.0})
    panel.smu.read_panel = broken
    state = panel.set_output(True)
    assert state["output"] is True, "the caller is told the source is live"
    assert state["reading"] is None
    assert "did not answer" in state["poll"]["last_error"]
    assert panel.smu.output_enabled is True


def test_every_answer_carries_the_panel_even_when_something_broke(console, panel):
    """Including the generic 500. A failure that left the caller unable to see
    whether the source is live is worse than the failure."""
    console("POST", "/api/source", {"function": "I", "level": 0.0})
    panel.smu.read_panel = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    status, out = console("POST", "/api/output", {"on": True})
    assert status == 200 and out["output"] is True
    assert "boom" in out["poll"]["last_error"]

    status, out = console("POST", "/api/read")
    assert status == 500 and "boom" in out["error"]
    assert out["output"] is True, "the 500 says the source is on"
    assert "panel" in out


def test_closing_the_console_switches_the_output_off():
    """Whatever ends this program — Ctrl-C, a closed terminal, an exception —
    the source goes off on the way out. A browser tab closing is not something
    to rely on for it."""
    p = make_panel()
    p.start()
    p.set_source({"function": "I", "level": 0.0})
    p.set_output(True)
    assert p.smu.output_enabled is True
    p.close()
    assert p.smu.output_enabled is False


def test_a_panel_with_no_instrument_says_so_rather_than_refusing_to_start():
    """A console that would not come up because the instrument is off is a
    console that cannot tell you the instrument is off."""
    p = KeithleyPanel(None, ceiling=dict(CEILING), defaults=SourceMeterConfig(),
                      unavailable="GPIB0::24::INSTR: VisaIOError: timeout")
    p.start()
    try:
        state = p.state()
        assert state["instrument"]["available"] is False
        assert "timeout" in state["instrument"]["unavailable"]
        assert state["panel"] is None and state["output"] is None
    finally:
        p.close()


# -- over HTTP ----------------------------------------------------------------
def test_every_route_answers_the_whole_panel(console):
    """One object, from every route. A client that had to merge four different
    answers into one screen would be four chances to draw a bench that never
    existed."""
    status, page = console("GET", "/api/state")
    assert status == 200 and set(page) >= {"instrument", "ceiling", "defaults",
                                           "panel", "output", "reading", "poll"}

    status, out = console("POST", "/api/source", {"function": "I", "level": 0.0})
    assert status == 200 and out["panel"]["function"] == "current"

    status, out = console("POST", "/api/output", {"on": True})
    assert status == 200 and out["output"] is True and out["reading"] is not None

    status, out = console("POST", "/api/read")
    assert status == 200 and out["reading"]["volts"] > 0.8

    status, out = console("POST", "/api/poll", {"interval_s": 0.5})
    assert status == 200 and out["poll"] == {"running": True, "interval_s": 0.5,
                                             **{k: out["poll"][k] for k in
                                                ("readings", "failures", "last_error")}}

    status, out = console("POST", "/api/errors")
    assert status == 200 and out["errors"] == [] and "panel" in out


def test_the_status_code_says_which_kind_of_no_it_is(console):
    """409 the bench will not do this now (with the sentence and the remedy),
    422 a value the console cannot read, 503 no instrument at all."""
    status, out = console("POST", "/api/output", {"on": True})
    assert status == 409 and out["level"] == "warn" and out["refused"] is True
    assert "set the source first" in out["error"]

    status, out = console("POST", "/api/source", {"current_compliance_a": 0.5})
    assert status == 409 and out["level"] == "crit"

    status, out = console("POST", "/api/source", {"nplc": 50})
    assert status == 422 and "nplc" in out["error"]

    status, out = console("POST", "/api/output", {})
    assert status == 422 and "on" in out["error"]

    assert console("GET", "/api/nope")[0] == 404
    assert console("POST", "/api/nope")[0] == 404


def test_the_page_is_served_with_a_length_so_a_browser_can_finish_it(console):
    status, page = console("GET", "/")
    assert status == 200
    assert b"Keithley 2400" in page["body"]
    # Without this a browser waits for a body that has already arrived.
    assert page["length"] == str(len(page["body"]))


# -- the command line ---------------------------------------------------------
def test_the_host_must_be_a_loopback_address(capsys):
    """No authentication, and this program drives a source into somebody's
    device: that is acceptable only where nobody but this machine can connect."""
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost")
    assert not is_loopback("0.0.0.0") and not is_loopback("192.168.1.4")
    assert main(["--host", "0.0.0.0"]) == 2
    assert "loopback" in capsys.readouterr().err


def test_the_defaults_are_the_ones_the_documentation_names():
    a = build_parser().parse_args([])
    assert (a.host, a.port, a.sim) == ("127.0.0.1", 8924, False)
    assert a.poll == 1.0, "the display opens running, as a front panel does"


def test_a_named_file_that_is_missing_is_an_error_not_a_silent_default(capsys):
    """A typo in `--rig` that quietly ran the built-in ceilings would be a
    bench nobody chose."""
    assert main(["--sim", "--rig", "no-such-rig.toml"]) == 2
    assert "no such file" in capsys.readouterr().err
