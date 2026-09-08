"""`examples/keithley_console/`: the 2400's front panel in a browser.

That folder is a standalone example — it imports nothing from `bace/`, so it
can be copied to a machine with the SourceMeter and a Python and nothing else.
Being standalone is the property most easily lost by accident, so it is the
first thing asserted here, in a subprocess.

The rest is what the page depends on, and it is mostly one invariant seen from
different sides: **the console must never show a source as off while it
drives.** Every refusal, every budget, every re-read of `:OUTP?` below is that
one thing. Under it sits the other: **one thread touches the instrument** —
every request goes through the panel's worker, and the free-running display is
what that worker does between jobs, so a reading can never land in the middle
of a click.

Driven over real HTTP against a real server on an ephemeral port, because the
transport is the part a stdlib server is most likely to get wrong (a missing
Content-Length hangs a browser and no unit test would see it).
"""
from __future__ import annotations

import importlib
import json
import pathlib
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

FOLDER = pathlib.Path(__file__).resolve().parents[1] / "examples" / "keithley_console"
# The folder is what a copy of it would be: on `sys.path`, with nothing else
# from this checkout reachable by name. That is also how `keithley_console.py`
# finds its own siblings when it is run as a script.
sys.path.insert(0, str(FOLDER))

from keithley_console import build_parser, is_loopback, main
import panel as panel_module
from panel import (JOB_TIMEOUT_S, OUTPUT_WATCH_S, KeithleyPanel, PanelPending,
                   PanelRefused, PanelUnavailable, source_args)
from server import make_server, serve_forever
from smu import PanelSetup, SourceMeterConfig, panel_budget_for
from simulated import SimulatedSourceMeter

CEILING = {"current_a": 0.05, "voltage_v": 5.0}


def make_panel(*, lit: bool = True, ceiling: dict | None = None,
               defaults: SourceMeterConfig | None = None) -> KeithleyPanel:
    return KeithleyPanel(SimulatedSourceMeter(lit=lit, seed=1),
                         ceiling=ceiling or dict(CEILING),
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

    def call(method: str, path: str, body: dict | None = None, **headers: str):
        # Every POST declares JSON, body or not — that is the rule the console
        # enforces (`CONTROL_CONTENT_TYPE`) and what its own page sends.
        data = b"" if body is None else json.dumps(body).encode()
        head = {"Content-Type": "application/json"} if method == "POST" else {}
        head.update(headers)
        request = urllib.request.Request(
            base + path, data=data if method == "POST" else None,
            method=method, headers=head)
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


def test_it_imports_nothing_from_the_package_or_off_the_shelf() -> None:
    """The property the folder exists for, asserted in a subprocess.

    A fresh interpreter, only the folder on the path, importing the entry
    point the way running the script does. Anything from `bace/` — or numpy,
    or FastAPI, or a VISA backend — appearing in `sys.modules` afterwards means
    somebody reached back into the checkout and the folder no longer runs on a
    machine that has only the instrument and a Python.
    """
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(FOLDER)!r});"
        "import keithley_console;"
        "print(sorted(n for n in sys.modules if n.split('.')[0] in "
        "('bace', 'numpy', 'scipy', 'pyvisa', 'h5py', 'fastapi', 'uvicorn', "
        "'websockets', 'starlette')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", (
        "examples/keithley_console pulled in " + out.stdout.strip() +
        ". The folder must stay copy-and-run: standard library only, and "
        "nothing from bace/.")


def test_the_folder_carries_everything_it_needs() -> None:
    for name in ("keithley_console.py", "panel.py", "server.py", "smu.py",
                 "simulated.py", "page.html", "README.md",
                 "Run Keithley Console.bat"):
        assert (FOLDER / name).is_file(), f"examples/keithley_console/{name} is missing"


def test_pyvisa_is_reached_only_for_a_real_instrument():
    """`pyvisa` is imported *inside* `_open_real`, so a real bench reaches it
    and `--sim` never does — which is what lets the folder come up on a laptop
    with no VISA runtime installed at all."""
    import keithley_console as cli
    import server as server_module

    for module in (panel_module, server_module, cli):
        for name in _module_level_imports(module):
            assert name not in ("fastapi", "uvicorn", "pyvisa", "websockets"), name
    assert "pyvisa" in open(cli.__file__, encoding="utf-8").read(), \
        "and the real bench still reaches it"
    # The panel is the instrument's; opening a session is the entry point's job.
    assert "pyvisa" not in open(panel_module.__file__, encoding="utf-8").read()


def test_the_page_is_one_file_that_fetches_nothing_from_a_network():
    """A bench PC may have no route out, and a console whose fonts or scripts
    came from a CDN would come up unstyled or not at all."""
    from server import PAGE
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
def test_a_page_on_another_site_cannot_drive_the_instrument(console, panel):
    """Binding to loopback keeps a *network* out; it does not keep a browser
    out. Any page the operator has open can `fetch(..., {mode: "no-cors"})` at
    a fixed port on their own machine — it cannot read the answer, and does
    not need to, because the side effect is a source driving a device.

    Reproduced before the fix: a `text/plain` POST from `https://evil.example`
    applied a 2 V source and switched the output on, both answered 200.
    """
    console("POST", "/api/source", {"function": "V", "level": 0.0})

    # What `no-cors` is allowed to send: one of the simple content types.
    for ctype in ("text/plain", "application/x-www-form-urlencoded",
                  "multipart/form-data", ""):
        status, out = console("POST", "/api/output", {"on": True},
                              **{"Content-Type": ctype})
        assert status == 403, f"{ctype!r} -> {status}"
        assert "content-type" in out["error"].lower()
    assert panel.smu.output_enabled is False, "nothing reached the instrument"

    # And a properly-typed request that admits where it came from.
    for origin in ("https://evil.example", "http://192.168.1.9:8924", "null"):
        status, out = console("POST", "/api/output", {"on": True}, Origin=origin)
        assert status == 403, f"{origin} -> {status}"
        assert origin in out["error"] or "came from" in out["error"]
    assert panel.smu.output_enabled is False

    # The console's own page is loopback, by every name it may be opened
    # under. Whatever the bench then says (409 here — the output is off), it
    # is not the door being shut.
    for origin in ("http://127.0.0.1:8924", "http://localhost:8924", "http://[::1]:8924"):
        assert console("POST", "/api/read", None, Origin=origin)[0] != 403, origin


def test_a_refused_cross_site_request_is_told_nothing_about_the_bench(console):
    """403 and no state: a page that may not ask is not told what the bench is
    doing either."""
    status, out = console("POST", "/api/state", {}, **{"Content-Type": "text/plain"})
    assert status == 403
    assert "panel" not in out and "output" not in out


def test_a_port_already_taken_still_switches_a_live_output_off():
    """Starting the console twice is the ordinary way to meet this, and by the
    time the port is refused the instrument is open — `_open_real` may already
    have found its output live, left there by whoever was using it. The server
    was built *outside* the try, so that raised straight out of the program:
    no output-off, no warning, a source driving and a traceback about a
    socket."""
    import socket

    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    taken = held.getsockname()[1]

    p = make_panel()
    p.start()
    p.set_source({"function": "I", "level": 0.0})
    p.set_output(True)
    p._bus.stop()                       # `serve_forever` starts its own worker
    p._bus = panel_module._Bus()
    assert p.smu.output_enabled is True

    try:
        with pytest.raises(OSError):
            serve_forever(p, host="127.0.0.1", port=taken)
    finally:
        held.close()
    assert p.smu.output_enabled is False, "the finally ran even though the bind did not"


def test_a_ceiling_that_is_not_a_number_is_refused_at_construction():
    """TOML accepts `nan`, and every comparison against a NaN is false — so a
    NaN ceiling would wave through any level and any compliance on the one
    surface where a number goes straight onto the device."""
    for bad in (float("nan"), float("inf"), 0.0, -1.0):
        with pytest.raises(ValueError, match="finite positive number"):
            KeithleyPanel(None, ceiling={"current_a": bad, "voltage_v": 5.0},
                          defaults=SourceMeterConfig())
        with pytest.raises(ValueError, match="finite positive number"):
            KeithleyPanel(None, ceiling={"current_a": 0.05, "voltage_v": bad},
                          defaults=SourceMeterConfig())


def test_the_output_flag_is_asked_of_the_instrument_and_not_remembered(panel):
    """The operator has a hand on the 2400's own OUTPUT key. A cached flag
    reports a source off while it drives — and `apply_panel`'s interlock reads
    the same flag, so it would permit a function or wiring change under a live
    output, which is the one thing that check exists to stop.

    `drivers/rigs.py` reaches for `read_output()` for exactly this reason.
    """
    asked: list[str] = []
    truth = {"on": False}

    # A SourceMeter with a front panel somebody else is touching.
    panel.smu.read_output = lambda: (asked.append("?"), truth["on"])[1]
    panel.set_source({"function": "I", "level": 0.0})
    assert asked, "the source change asked before its interlock decided"

    truth["on"] = True                                   # a hand on OUTPUT
    panel.set_poll(0.1)
    deadline = time.monotonic() + 2
    while not panel.state()["output"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert panel.state()["output"] is True, "the display noticed within an interval"



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


def _hurry(monkeypatch, panel, seconds: float = 0.2) -> None:
    """Make the panel's own waits short, so a test can reach a timeout without
    sitting through a read budget. Everything else stays the real code path —
    which is the point: the previous version of the test below drove
    `_Bus.do` directly and so pinned the primitive while `set_output` passed
    it the flag inverted."""
    monkeypatch.setattr(panel_module, "SHUTDOWN_MARGIN_S", 0.0)
    monkeypatch.setattr(panel, "read_budget_s", lambda: seconds)


def test_switching_the_output_off_is_never_dropped_behind_a_slow_read(panel, monkeypatch):
    """The display may be mid-tick, and a legal tick is 80 s (NPLC 10, a
    100-deep filter). The off used to wait the 30 s floor, time out, and be
    *discarded* by the cancellation added a round earlier — so pressing OFF
    left the source driving.

    Through `set_output`, not through the bus underneath it: the flag was
    passed inverted for one commit and a test that went at `_Bus.do` could not
    have seen it.
    """
    panel.set_source({"function": "I", "level": 0.0})
    panel.set_output(True)
    assert panel.smu.output_enabled is True

    _hurry(monkeypatch, panel)
    released = threading.Event()
    panel._bus.submit(lambda: released.wait(3))          # a tick in flight
    time.sleep(0.05)
    with pytest.raises(PanelPending, match="It will run"):
        panel.set_output(False)
    released.set()
    deadline = time.monotonic() + 3
    while panel.smu.output_enabled and time.monotonic() < deadline:
        time.sleep(0.02)
    assert panel.smu.output_enabled is False, "queued, and it ran"


def test_an_output_on_the_caller_gave_up_on_is_cancelled(panel, monkeypatch):
    """The other half of the same flag, and the reason it is not simply
    "never cancel": dropping an ON leaves the source off, which is the safe
    end. An ON that landed minutes after the operator was told it failed is a
    device energised by a request nobody is watching."""
    panel.set_source({"function": "I", "level": 0.0})
    _hurry(monkeypatch, panel)
    released = threading.Event()
    panel._bus.submit(lambda: released.wait(3))
    time.sleep(0.05)
    with pytest.raises(PanelRefused, match="did not answer"):
        panel.set_output(True)
    released.set()
    time.sleep(0.4)
    assert panel.smu.output_enabled is False, "the ON was dropped, not deferred"


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


def test_a_source_change_does_not_leave_the_last_level_s_reading_on_screen(panel):
    """With the display off and the output live, a level moved from 1.5 V to
    0 V left the 1.5 V measurement — and its `Cmpl` annunciator — beside the
    new setup, for ever. The reading belongs to a level the source has left."""
    panel.set_source({"function": "V", "level": 1.5, "current_compliance_a": 0.01})
    live = panel.set_output(True)
    assert live["reading"]["compliance"] is True, "1.5 V into a lit cell clamps"

    moved = panel.set_source({"level": 0.0})
    assert moved["panel"]["level"] == 0.0
    assert moved["reading"] is not None, "still on the worker, so it re-read"
    assert moved["reading"]["level"] == 0.0
    assert moved["reading"]["compliance"] is False, "the old annunciator is gone"
    assert abs(moved["reading"]["volts"]) < 1e-6


def test_a_source_change_with_the_output_off_leaves_the_display_blank(panel):
    """Nothing to read, so nothing is invented — and the stale reading still
    goes, rather than describing a setup that is no longer applied."""
    panel.set_source({"function": "V", "level": 0.0})
    panel.set_output(True)
    assert panel.state()["reading"] is not None
    panel.set_output(False)
    state = panel.set_source({"level": 0.2})
    assert state["reading"] is None
    assert state["poll"]["last_error"] is None, "an output that is off is not a fault"


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


def test_reading_with_nothing_to_read_is_a_refusal_and_not_an_error(console):
    """"The output is off" and "no source is set" are things the bench will
    not do *now*, with a sentence each. They reached the generic 500 until a
    test of the cross-site check happened to ask for a reading with the output
    off — the page disables the button, so nothing had ever asked."""
    status, out = console("POST", "/api/read")
    assert status == 409 and "not applied" in out["error"]

    console("POST", "/api/source", {"function": "I", "level": 0.0})
    status, out = console("POST", "/api/read")
    assert status == 409 and "output is OFF" in out["error"]
    assert out["level"] == "warn" and out["panel"] is not None


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


# -- round six ----------------------------------------------------------------
def test_an_explicit_read_asks_the_instrument_before_it_believes_the_output(panel):
    """`/api/read` was the last bus job deciding on the driver's cached flag.

    The display refreshes it every tick and a source change refreshes it
    before its interlock — but with the display off, an explicit read went
    straight to `_tick`. So an output switched off at the 2400's own OUTPUT
    key still passed `read_panel`'s interlock, and the near-zero a source
    disconnected inside the instrument answers with was filed as a reading of
    the device. That is the exact lie the interlock exists to prevent.
    """
    truth = {"on": True}
    panel.smu.read_output = lambda: truth["on"]
    panel.set_source({"function": "V", "level": 1.0})
    panel.set_output(True)
    assert panel.read()["reading"] is not None, "a live output reads"

    truth["on"] = False                                  # a hand on OUTPUT
    with pytest.raises(PanelRefused, match="output is OFF"):
        panel.read()
    assert panel.state()["output"] is False, "and the console says so"


def test_a_source_change_is_waited_on_for_the_settings_it_asks_for(panel):
    """The job applies the new setup and then reads *under it*. A request that
    moves NPLC and the filter from quick settings to a legal slow pair — 10
    and 100, which is 175 s — was waited on for the panel the instrument still
    held, 30 s. The change had landed and the worker was reading; the operator
    got a 409 saying the source change failed while the source was live.
    """
    waits: list[float] = []
    real_do = panel._bus.do
    panel._bus.do = lambda fn, timeout_s=None, **kw: (
        waits.append(timeout_s), real_do(fn, timeout_s=timeout_s, **kw))[1]

    panel.set_source({"function": "V", "level": 0.5})
    quick = waits[-1]
    panel.set_source({"nplc": 10.0, "averaging": 100})
    slow = waits[-1]

    assert quick == pytest.approx(JOB_TIMEOUT_S), "quick settings keep the floor"
    assert slow >= panel.read_budget_s(), (
        "the wait must cover the read the same job takes under the new setup")
    assert slow == pytest.approx(panel_budget_for(10.0, 100))


def test_the_stop_signals_stay_caught_until_the_output_is_off():
    """`restore()` ran *before* `panel.close()`, so the default handlers were
    live for exactly the window that matters. The first Ctrl-C can land while
    the worker is inside a read that legally takes 80 s — the operator sees
    nothing happen and presses again, and that second one killed the process
    with the source still driving.

    Asserted on the handlers rather than by raising a second signal: `os.kill`
    with SIGTERM on Windows terminates outright, and this must hold there too.
    """
    import signal

    p = make_panel()          # started by serve_forever, not here
    during: dict = {}
    real_close = p.close

    def watched_close():
        for name in ("SIGINT", "SIGTERM"):
            number = getattr(signal, name, None)
            if number is not None:
                during[name] = signal.getsignal(number)
        return real_close()

    p.close = watched_close
    before = {name: signal.getsignal(getattr(signal, name))
              for name in ("SIGINT", "SIGTERM") if hasattr(signal, name)}

    def stop_it(server):
        threading.Thread(target=server.shutdown, daemon=True).start()

    serve_forever(p, host="127.0.0.1", port=0, on_ready=stop_it)

    assert during, "close() ran"
    for name, handler in during.items():
        assert handler is not signal.SIG_DFL, (
            f"{name} was back to its default while the output was still on")
    assert {n: signal.getsignal(getattr(signal, n)) for n in before} == before, (
        "and they are put back afterwards")


def test_a_console_that_cannot_start_does_not_walk_away_holding_the_instrument(capsys):
    """The panel validates the bench ceilings and `run.toml`'s defaults, and by
    then `_open_real` has opened the session and read an output that may be
    live. That construction sits outside `serve_forever`, so it is outside the
    `finally` that switches the output off: a `nan` ceiling exited with the
    source driving and the GPIB session held — and the next console to start
    would blame the cable.
    """
    import keithley_console as entry

    smu = SimulatedSourceMeter(seed=1)
    smu.enable_output(True)                      # left driving by whoever had it
    closed: list[bool] = []
    smu.close = lambda: closed.append(True)

    rig = tmp_rig_with_a_nan_ceiling()
    real_open = entry._open_real
    entry._open_real = lambda address, config, timeout_ms: (smu, "KEITHLEY 2400", "")
    try:
        assert entry.main(["--rig", rig, "--port", "0"]) == 2
    finally:
        entry._open_real = real_open

    assert smu.output_enabled is False, "the output went off on the way out"
    assert closed, "and the session was released"
    assert "finite positive number" in capsys.readouterr().err


def tmp_rig_with_a_nan_ceiling() -> str:
    import tempfile

    path = pathlib.Path(tempfile.mkdtemp()) / "rig.toml"
    path.write_text("[sourcemeter]\naddress = 'GPIB0::24::INSTR'\n"
                    "max_current_compliance_a = nan\nmax_voltage_compliance_v = 5.0\n")
    return str(path)


# -- round seven --------------------------------------------------------------
def test_the_state_route_asks_the_instrument_when_the_display_is_off(panel):
    """`state` touches no instrument by design — it is answered on an HTTP
    thread and must never block behind an 80 s read. But that left the passive
    path stale: with the display off, nothing asked at all, so the page went on
    drawing a source as off for as long as it was left open while somebody had
    switched it on at the 2400's own keys. The ask is queued now, so the next
    refresh a second later carries it.
    """
    truth = {"on": False}
    panel.smu.read_output = lambda: truth["on"]
    panel.set_source({"function": "V", "level": 1.0})
    panel.set_poll(None)                                 # the operator turns it off

    server = make_server(panel, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def get_state():
        with urllib.request.urlopen(base + "/api/state", timeout=5) as answer:
            return json.load(answer)

    try:
        assert get_state()["output"] is False
        truth["on"] = True                               # LOCAL, then OUTPUT, by hand
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            time.sleep(panel_module.OUTPUT_WATCH_S / 4)
            if get_state()["output"] is True:
                break
        assert get_state()["output"] is True, (
            "a refresh of the page must notice a hand on OUTPUT")
    finally:
        server.shutdown()
        server.server_close()


def test_the_state_route_does_not_ask_more_often_than_it_has_to(panel):
    """The page refreshes on a timer, so one query per refresh would be the
    display poll wearing a different name — and while the display *is*
    running, every tick already asks. Counted at the bus rather than at the
    instrument, because the tick's own queries are indistinguishable there.
    """
    submitted: list[str] = []
    real_submit = panel._bus.submit
    panel._bus.submit = lambda fn, **kw: (submitted.append("job"),
                                          real_submit(fn, **kw))[1]

    panel.set_source({"function": "V", "level": 0.5})
    panel.set_poll(None)
    submitted.clear()

    panel.refresh_output_soon()
    assert len(submitted) == 1, "the first ask goes"
    panel.refresh_output_soon()
    panel.refresh_output_soon()
    assert len(submitted) == 1, f"and is throttled to one per {OUTPUT_WATCH_S:g} s"

    panel.set_poll(1.0)
    submitted.clear()
    panel.refresh_output_soon()
    assert submitted == [], "the display owns the refresh while it is running"


def test_switching_the_output_off_is_not_swallowed_by_a_busy_page():
    """The console guarantees on its own side that an output-off is never
    dropped — and the page could throw the click away before it was ever sent.
    `call` returns early while another request is in flight, and a source
    change can legally take 175 s, so an operator clicking `off` on a live
    source got no request, no message, and enabled controls.

    Asserted on the page's source because this file has no test harness: it is
    one HTML file with its script inline, so there is no module to import.
    Verified in a browser as well — the click makes `POST /api/output` now,
    where before it made no request at all.
    """
    from server import PAGE
    page = open(PAGE, encoding="utf-8").read()

    off = [line for line in page.splitlines() if '"/api/output", { on: false }' in line]
    assert len(off) == 1, "one place switches the output off"
    assert "always: true" in off[0], (
        "the off must send even while another request is in flight")

    on = [line for line in page.splitlines() if '"/api/output", { on: true }' in line]
    assert len(on) == 1 and "always" not in on[0], (
        "the on stays guarded: dropping an ON leaves the output off, the safe end")

    # And the guard has to honour it, rather than the option being decorative.
    assert "const guarded = !(options && options.always);" in page
    assert "if (inflight && guarded) return null;" in page


# -- round eight --------------------------------------------------------------
def test_shutdown_waits_for_the_slowest_read_the_panel_will_accept(panel):
    """`close` sizes its wait before it can see what the worker is inside. A
    source change in flight has not published its setup yet — `smu.panel` is
    still the one it is replacing — so quick settings moving to NPLC 10 and a
    100-deep filter sized the wait at ~25 s for a read that then takes 175, and
    the daemon worker was killed with the queued output-off never run.

    The ceiling costs nothing: the wait is a bound, not a sleep.
    """
    panel.set_source({"function": "V", "level": 0.5, "nplc": 1.0, "averaging": 1})
    assert panel.read_budget_s() == pytest.approx(JOB_TIMEOUT_S), "the read wait stays per-setup"
    assert panel.shutdown_budget_s() >= panel_budget_for(10.0, 100), (
        "shutdown must cover the slowest read this panel accepts, whatever is in flight")

    # And it really is the ceiling, not the applied panel's own budget.
    panel.set_source({"nplc": 10.0, "averaging": 100})
    assert panel.shutdown_budget_s() >= panel.read_budget_s()


def test_an_unanswered_output_query_is_unknown_and_never_off(panel):
    """`read_output()` returns None when the instrument was *asked* and did not
    give a usable answer — a timed-out `:OUTP?`, or a reply `on_off` did not
    recognise. Falling back to the cached flag there believed exactly the
    memory the query exists to distrust: the cache says off, the operator
    switched the output on at the 2400 itself, and a wiring-class change went
    through the interlock under a live source.
    """
    panel.set_source({"function": "V", "level": 0.5})
    panel.smu.read_output = lambda: None
    panel.smu._output = False                      # what this process last wrote

    with pytest.raises(PanelRefused, match="did not answer"):
        panel.set_source({"function": "current"})  # a PANEL_STATIC change
    assert panel.state()["output"] is None, "unknown, and not the cache's 'off'"

    # A level still applies: the one thing that must never be blocked is
    # bringing a source down.
    panel.set_source({"level": 0.0})
    assert panel.state()["panel"]["level"] == 0.0

    # And a read says which of the two it is.
    with pytest.raises(PanelRefused, match="did not answer"):
        panel.read()


def test_no_such_query_still_stands_on_the_cache(panel):
    """The simulated SourceMeter has no `read_output` — there is no front panel
    on it for anybody to touch, so what this process last wrote *is* the truth.
    That is the one case the fallback is allowed."""
    assert not hasattr(type(panel.smu), "read_output")
    panel.set_source({"function": "V", "level": 0.5})
    panel.set_output(True)
    assert panel.state()["output"] is True


def test_the_page_says_when_it_cannot_reach_the_console():
    """`refresh`'s catch claimed "the chip says so on the next draw" and did
    neither: no state recorded, no redraw. So a stopped console left the last
    answer on screen for ever — a green "connected" chip, an output, a
    reading — and the state it must never be mistaken for is a source the page
    says is off.

    Asserted on the source, as the other page checks are. Verified in a
    browser too: killing the console flips the chip to "no answer from the
    console", locks the output row, and prints the note; restarting it
    restores all three.
    """
    from server import PAGE
    page = open(PAGE, encoding="utf-8").read()

    assert "let linkDown = null;" in page
    assert "linkDown = null;" in page, "and something clears it again"
    # The three places it has to reach, each of which was wrong on its own.
    assert 'text: "no answer from the console"' in page, "the chip"
    assert 'key: "linkdown"' in page, "the note, with the reason"
    assert "formRev, Boolean(linkDown)" in page, (
        "the controls' render key — without it they stayed enabled over a "
        "console that was no longer there")
    assert "OUTPUT key" in page, "and it names the way out that still works"
