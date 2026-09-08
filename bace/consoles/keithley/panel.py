"""The Keithley 2400 panel: the instrument, and the one thread allowed to
touch it.

No HTTP here and nothing from `bace.service`. What this owns is the rule the
whole repository is built on -- **one thread on the bus** -- and the small
amount of policy a front panel needs on top of `drivers.keithley2400`:

* every instrument call runs on one worker thread, and an HTTP handler (which
  gets a thread of its own from `ThreadingHTTPServer`) submits a job and waits.
  VISA blocks and its sessions are not thread-safe, so this is not a
  performance choice: two handlers writing SCPI at once is two commands
  interleaved on one bus;
* **the display polls between jobs, on that same thread.** A front panel
  free-runs its display, and the way to do that without a second thread on the
  bus is to make the reading what the worker does when it has nothing else to
  do -- so a click never waits behind a reading, and a reading never lands in
  the middle of a click;
* the bench ceilings from `rig.toml` hold **a typed level**, not only a
  compliance. Nowhere else in this project can a level be typed straight onto
  the device: a sweep's ends come from a module's parameters and V_oc/J_sc
  source zero. `max_voltage_compliance_v` bounds a sourced voltage and
  `max_current_compliance_a` a sourced current -- they are the bench's
  statement of what the device may see, whichever end of the instrument it
  arrives from, so no third key is invented to hold the same number twice;
* the output goes **off** when the console stops. A panel that left a source
  driving because somebody closed the window would be the worst thing on this
  bench, and it is the one thing a browser cannot be relied on to do.

Nothing here writes a file, and nothing here measures anything: this is the
instrument's own front panel with a socket in front of it.
"""
from __future__ import annotations

import dataclasses
import math
import queue
import threading
import time
from typing import Any, Callable

from ...drivers.keithley2400 import (PANEL_STATIC, PanelSetup, SourceMeterConfig,
                                     SourceMeterError)

JOB_TIMEOUT_S = 30.0
"""The **floor** on how long a request waits for the worker: enough for any
panel operation that is a handful of SCPI writes.

It is not enough for a reading, and an earlier version of this file claimed it
was. NPLC 10 and a 100-deep filter are both legal on a 2400 and both accepted
here -- a panel should accept what the instrument accepts -- and that pair is
`100 x 4 x 10 / 50 Hz` = 80 s of integration (four apertures per averaged
reading, because `:FUNC:CONC ON` measures both and auto-zeroes each). A read
job therefore waits `Keithley2400.panel_budget_s` instead, which is that model
with the driver's own margin. Waiting a flat 30 s on it would answer a healthy
read with a 409 saying the instrument had stopped -- while the read went on
running, and whatever was queued behind it ran afterwards, on a bench whose
operator had been told the request failed."""

SHUTDOWN_MARGIN_S = 10.0
"""Added to a read's budget when `close` waits for the worker: time for the
in-flight read to end *and* for the queued output-off behind it to run."""

POLL_MIN_S, POLL_MAX_S = 0.1, 60.0
POLL_DEFAULT_S = 1.0
"""What the display refreshes at. Slower than the 2400's own display and
faster than anyone watching a number settle needs: a reading at NPLC 1 is four
apertures, 80 ms on 50 Hz mains, so a tick costs eight percent of the worker
and leaves the rest of it to whoever clicks something."""

SOURCE_FIELDS: tuple[str, ...] = (
    "function", "level", "current_compliance_a", "voltage_compliance_v",
    "nplc", "averaging", "terminals", "four_wire", "source_range")
"""What `POST /api/source` accepts: every `PanelSetup` field, all optional.
What is not sent keeps the value the panel already holds, so moving a level is
a body of one key -- the panel is a state the operator edits, not a form they
resubmit."""

FUNCTION_WORDS: dict[str, str] = {
    "voltage": "voltage", "volt": "voltage", "volts": "voltage", "v": "voltage",
    "current": "current", "amp": "current", "amps": "current", "a": "current",
    "i": "current",
}
"""`V` and `I` because that is what is written on the instrument and on every
axis label in this project; `voltage`/`current` stays the canonical pair."""


class PanelRefused(RuntimeError):
    """Something the panel will not do *now*. `level` is `warn` or `crit`,
    and the text is written for the screen: what happened, and what to do."""

    def __init__(self, text: str, level: str = "warn"):
        super().__init__(text)
        self.text = text
        self.level = level


def _ceiling(name: str, value: Any) -> float:
    """A bench ceiling, or a refusal to start.

    TOML accepts `nan`, and every comparison against a NaN is false -- so a
    `max_current_compliance_a = nan` in `rig.toml` would make `_check_ceiling`
    wave through any level and any compliance the operator typed, on the one
    surface where a number goes straight onto the device. A limit that permits
    everything is worse than no limit, because it looks like one.
    """
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(
            f"rig.toml [sourcemeter] {name}: a bench ceiling must be a finite "
            f"positive number, not {value!r}. Everything is under a ceiling that "
            "is not one.")
    return number


class PanelPending(RuntimeError):
    """The job is queued and **will** run, but the caller stopped waiting.

    The one operation that gets this rather than a refusal is switching the
    output *off*. A refusal would be a lie -- the off is in the queue, behind
    a read that has not finished -- and, worse, an off that was dropped
    because the caller gave up is a source left driving. So the off is never
    cancelled, and a caller that waited long enough is told it is coming.

    `bace/service/session.py` answers a slow `park` the same way, for the same
    reason: "a park that vanished because the operator's click timed out would
    leave the bench where the aborted run's own unwind put it".
    """


class PanelUnavailable(RuntimeError):
    """There is no instrument to drive, or the console is shutting down.
    Carries why."""


# -- the worker -------------------------------------------------------------
_WAKE = object()
"""Put in the queue to make the worker re-read its idle setting rather than
sleep out a timeout computed from the old one."""


class _Bus:
    """One thread, and everything that touches the instrument runs on it.

    `do(fn)` submits and waits; `idle(fn, every)` is what the thread does when
    nothing has been submitted. Deliberately not a thread pool and not a lock
    around a shared session: a lock would let two handlers take turns *inside*
    one logical operation -- a write, then somebody else's write, then the read
    that belonged to the first -- which is the failure a bus lock is supposed
    to prevent and the one that is hardest to see afterwards.
    """

    def __init__(self, name: str = "keithley-console"):
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._stop = threading.Event()
        self._idle: tuple[Callable[[], None], float] | None = None
        self._next_idle = 0.0
        self._sealed = False
        self._lock = threading.Lock()

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> bool:
        """Ask the thread to finish what it is doing, run what is still
        queued, and end. Returns whether it ended inside `timeout_s`."""
        self._stop.set()
        self._q.put(_WAKE)
        if self._thread.is_alive():
            self._thread.join(timeout_s)
        return not self._thread.is_alive()

    def set_idle(self, fn: Callable[[], None] | None, every: float = 1.0) -> None:
        """What to do between jobs, and how often. None switches it off.

        The wake is not decoration. The thread is asleep in `queue.get` on a
        timeout computed from the *old* setting -- with no idle at all that
        timeout is `None`, i.e. forever -- so without something in the queue,
        switching the display on would not start it until the next click, and
        switching it off would leave one more tick already decided.
        """
        with self._lock:
            self._idle = None if fn is None else (fn, float(every))
            self._next_idle = time.monotonic()
        self._q.put(_WAKE)

    def submit(self, fn: Callable[[], Any], *, force: bool = False) -> None:
        """Queue `fn` and do not wait. `KeithleyPanel.close` uses it for the
        output-off, which has to run *after* whatever the worker is in the
        middle of and must not be waited for from inside the shutdown path.

        `force` is that one job's exemption from the seal below.
        """
        with self._lock:
            if self._sealed and not force:
                raise PanelUnavailable(
                    "the console is shutting down; no further instrument work is "
                    "accepted")
            self._q.put(fn)

    def seal(self) -> int:
        """Accept no more work, and drop what has not started. Returns how
        many queued jobs were dropped.

        Shutdown's first act, and it has to be atomic with respect to
        `submit`: `ThreadingHTTPServer` runs each request on a daemon thread
        and `server_close()` does not wait for the ones already accepted, so a
        straggler handler can be inside `set_output(True)` while the console is
        closing. Sealed, that handler is told the console is shutting down;
        unsealed, its ON landed *after* the shutdown OFF and the process
        exited with the source driving -- while `close` reported the off
        confirmed, because it had been.

        Dropping the rest is deliberate too. At shutdown the only instrument
        operation that matters is the output going off; running a queued
        source change on the way out is work nobody is waiting for any more.
        """
        with self._lock:
            self._sealed = True
            dropped = 0
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
                dropped += 1
            return dropped

    def do(self, fn: Callable[[], Any], *, timeout_s: float = JOB_TIMEOUT_S,
           cancel_on_timeout: bool = True) -> Any:
        """Run `fn` on the worker and return what it returned, or raise what it
        raised -- on the *caller's* thread, so a handler sees a `PanelRefused`
        as an exception and not as a status code somebody remembered to check.

        **A job the caller gave up on does not run.** Without that, a request
        answered with "the instrument did not answer" would still reach the
        instrument, later, after whatever was ahead of it -- so a source could
        be applied minutes after the operator was told the attempt failed.
        There is one window left, between the check and `fn`: a job that has
        already started cannot be recalled, and this does not pretend to. What
        it removes is every job still waiting its turn.

        **`cancel_on_timeout=False` opts out**, and switching the output off
        is what opts out. Dropping that one because the caller stopped waiting
        is a source left driving; it stays queued and the caller gets
        `PanelPending`.
        """
        if not self._thread.is_alive():
            raise PanelUnavailable("the console's instrument thread is not running")
        done = threading.Event()
        gave_up = threading.Event()
        box: dict[str, Any] = {}

        def job() -> None:
            if gave_up.is_set():
                return
            try:
                box["value"] = fn()
            except BaseException as exc:                     # noqa: BLE001
                box["error"] = exc
            finally:
                done.set()

        self.submit(job)
        if not done.wait(timeout_s):
            if not cancel_on_timeout:
                raise PanelPending(
                    f"queued: the instrument has not answered within {timeout_s:g} s, "
                    "so this is waiting behind whatever it is doing. It will run.")
            gave_up.set()
            raise PanelRefused(
                f"the instrument did not answer within {timeout_s:g} s; the bus is "
                "busy or the 2400 has stopped responding", "crit")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _loop(self) -> None:
        try:
            self._run()
        finally:
            # On **every** way out, including a stop noticed at the top of the
            # loop while a job sits in the queue. `close` puts the output-off
            # there, and a worker that ended without running it would leave the
            # source driving -- the one thing this program must not do.
            self._drain()

    def _drain(self) -> None:
        while True:
            try:
                job = self._q.get_nowait()
            except queue.Empty:
                return
            if callable(job):
                try:
                    job()
                except Exception:                            # noqa: BLE001
                    pass    # one bad job must not strand the ones behind it

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                idle = self._idle
                due = self._next_idle
            wait = None if idle is None else max(0.0, due - time.monotonic())
            try:
                job = self._q.get(timeout=wait)
            except queue.Empty:
                job = None
            if callable(job):
                job()
                continue
            if self._stop.is_set():
                return
            # Whether to tick is decided *here*, under the lock, and not from
            # what was read before the wait: the display may have been switched
            # off while this thread was asleep, and a tick already decided on
            # is a reading after the operator turned the display off.
            with self._lock:
                if self._idle is None or time.monotonic() < self._next_idle:
                    continue
                idle, every = self._idle
                self._next_idle = time.monotonic() + every
            # A job that arrived while the tick was being decided still wins:
            # a click must not wait behind a reading.
            if not self._q.empty():
                continue
            try:
                idle()
            except Exception:                                # noqa: BLE001
                pass    # the reader records its own failures; see `_tick`


# -- the panel --------------------------------------------------------------
class KeithleyPanel:
    """One SourceMeter, driven by hand.

    `smu` is anything with the driver's panel interface -- `Keithley2400`, or
    `drivers.simulated.SimulatedSourceMeter` under `--sim`. `ceiling` is
    `rig.toml`'s two limits and `defaults` the `SourceMeterConfig` the panel
    opens on.
    """

    def __init__(self, smu: Any, *, ceiling: dict, defaults: SourceMeterConfig,
                 identity: str = "", address: str = "", mode: str = "rig",
                 unavailable: str = ""):
        self.smu = smu
        self.ceiling = {"current_a": _ceiling("current_a", ceiling["current_a"]),
                        "voltage_v": _ceiling("voltage_v", ceiling["voltage_v"])}
        self.defaults = PanelSetup.from_config(dataclasses.replace(
            defaults,
            current_compliance_a=min(defaults.current_compliance_a, self.ceiling["current_a"]),
            voltage_compliance_v=min(defaults.voltage_compliance_v, self.ceiling["voltage_v"])))
        """The panel a cold instrument opens on: the configuration in force,
        sourcing 0 V, with either compliance brought under the bench ceiling --
        never above `rig.toml` even when `run.toml` is."""
        self.identity = identity
        self.address = address
        self.mode = mode
        self.unavailable = unavailable
        self._bus = _Bus()
        self._lock = threading.Lock()
        self._reading: dict | None = None
        self._output: bool | None = None
        """What the instrument last answered to `:OUTP?`, on the worker. None
        until something has asked. Kept here rather than read off the driver
        at answer time because a driver's cached flag is only as fresh as the
        last thing this process did to it (`_fresh_output`)."""
        self._poll_s: float | None = None
        self._readings = 0
        self._failures = 0
        self._last_error: str | None = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._bus.start()

    def close(self) -> bool:
        """Output off, then stop the thread. **True when the output is
        confirmed off**, so a caller can say something when it is not.

        The output is the only part of this that matters: a console that
        exited leaving the 2400 driving a cell is the failure this whole
        program exists to make less likely, and a browser tab closing is not
        something to rely on for it.

        Which is why the off is *queued* rather than waited for here. Ctrl-C
        can arrive while the worker is inside a read that legally takes 80 s
        (`panel_budget_s`); an earlier version waited five seconds for the
        off, gave up, and stopped the thread -- which then finished its read,
        saw the stop flag, and exited without ever running the off, leaving a
        daemon thread's process to end with the source still driving. Now the
        off goes into the queue, the worker drains the queue on its way out
        whatever ended it, and the wait here is long enough for the read in
        front of it plus the off itself.

        A worker still inside a VISA call when even that runs out cannot be
        interrupted from here without writing to the bus from a second thread,
        which is the one thing this design forbids. That case returns False,
        and the caller says so out loud rather than exiting quietly.
        """
        return self.close_with_budget(self.shutdown_budget_s())

    def close_with_budget(self, timeout_s: float) -> bool:
        """`close`, with the wait named. Its own method so a test can prove
        the honest answer on the case that cannot be waited out."""
        # Seal before anything else: no handler may queue instrument work
        # from here on, and whatever had not started is dropped. Without it a
        # request thread still running past `server_close()` could put an ON
        # into the queue behind the shutdown OFF, and the drain would run
        # both -- ending with the source driving and `close` reporting the off
        # confirmed, which it had been, a moment earlier.
        self._bus.seal()
        self._bus.set_idle(None)
        confirmed = threading.Event()
        if self.available:
            def switch_off() -> None:
                self.smu.disable_output()
                confirmed.set()
            self._bus.submit(switch_off, force=True)
        self._bus.stop(timeout_s)
        return confirmed.is_set() if self.available else True

    def shutdown_budget_s(self) -> float:
        """How long `close` waits: the longest a read in flight can take, plus
        room for the queued output-off behind it."""
        budget = getattr(self.smu, "panel_budget_s", None)
        return (float(budget()) if callable(budget) else JOB_TIMEOUT_S) + SHUTDOWN_MARGIN_S

    def read_budget_s(self) -> float:
        """How long a read job may take before the caller gives up on it. The
        driver's own model, so the console and the instrument agree about what
        counts as slow."""
        budget = getattr(self.smu, "panel_budget_s", None)
        return max(JOB_TIMEOUT_S, float(budget()) if callable(budget) else 0.0)

    @property
    def available(self) -> bool:
        return self.smu is not None

    def _need(self) -> Any:
        if self.smu is None:
            raise PanelUnavailable(self.unavailable or "no SourceMeter on this console")
        return self.smu

    def _fresh_output(self, smu: Any) -> bool:
        """Ask the instrument whether its output is on, and cache the answer.

        **On the worker, and before anything decides on it.** `output_enabled`
        is the driver's cached flag, set when this process last wrote or read
        it -- and the operator has a hand on the 2400's own OUTPUT key. Trusted
        blind, that flag lets `/api/state` report a source off while it drives,
        and lets `apply_panel`'s interlock permit a function or wiring change
        under a live output, which is the one thing that check exists to stop.

        `drivers/rigs.py` reaches for `read_output()` for exactly this reason
        and says so: "the refusal exists for the generator somebody left ON
        before the service started, which a cached flag reports as off". The
        simulated SourceMeter has no such query -- there is no front panel on
        it to touch -- so its cached flag *is* the truth.
        """
        read = getattr(smu, "read_output", None)
        answer = read() if callable(read) else None
        if answer is None:
            answer = getattr(smu, "output_enabled", False)
        with self._lock:
            self._output = bool(answer)
        return bool(answer)

    # -- what the page draws ------------------------------------------------
    def state(self) -> dict:
        """The whole panel as one JSON object. Touches no instrument: the
        panel and the last reading are held here, and `panel` is the driver's
        own record of what it configured, which `*RST` clears there."""
        with self._lock:
            reading = dict(self._reading) if self._reading else None
            poll = {"running": self._poll_s is not None, "interval_s": self._poll_s,
                    "readings": self._readings, "failures": self._failures,
                    "last_error": self._last_error}
        panel = getattr(self.smu, "panel", None) if self.available else None
        output: bool | None = None
        if self.available:
            output = (self._output if self._output is not None
                      else bool(getattr(self.smu, "output_enabled", False)))
        return {
            "instrument": {"identity": self.identity, "address": self.address,
                           "mode": self.mode, "available": self.available,
                           "unavailable": self.unavailable or None},
            "ceiling": dict(self.ceiling),
            "defaults": panel_wire(self.defaults),
            "panel": panel_wire(panel),
            "output": output,
            "reading": reading,
            "poll": poll,
            "at": time.time(),
        }

    # -- the three things a panel does --------------------------------------
    def set_source(self, body: dict) -> dict:
        """Set the source. Every field optional and merged onto the panel the
        instrument already holds, so moving a level is a body of one key.

        **Changing the function resets the level and the range**, unless the
        same body sets them. `level` is volts or amps depending on `function`,
        so carrying a number across the change is carrying it into a different
        unit: a panel at 1.5 V, asked for current, would ask the instrument to
        source 1.5 *amps* -- which on this bench is refused by the ceiling, so
        what the operator sees is a click on `A` failing with a sentence about
        a number they did not type. The instrument's own keys keep a level per
        function; 0 is the value this opens on and the safe end of both ranges
        (0 V is J_sc and 0 A is V_oc), so it is what a change lands on.
        `source_range` follows for the same reason -- a range in volts means
        nothing in amps -- and `None` is the autorange a panel wants anyway.
        """
        values = source_args(body)
        def job() -> None:
            smu = self._need()
            # Before `apply_panel`, whose `PANEL_STATIC` interlock reads this.
            self._fresh_output(smu)
            base = getattr(smu, "panel", None) or self.defaults
            if values.get("function", base.function) != base.function:
                values.setdefault("level", 0.0)
                values.setdefault("source_range", None)
            setup = dataclasses.replace(base, **values)      # ValueError -> 422
            self._check_ceiling(setup)
            try:
                smu.apply_panel(setup)
            except SourceMeterError as exc:
                raise PanelRefused(str(exc)) from None
            # The reading on screen was taken at the level the source has just
            # left. Kept, it sits beside the new setup for as long as nobody
            # reads again -- for ever, with the display switched off -- and it
            # brings its compliance annunciator with it, so a panel moved from
            # 1.5 V down to 0 V goes on showing `Cmpl` from the old level.
            with self._lock:
                self._reading = None
            # Still on the worker, so a fresh one costs nothing extra. It is a
            # nicety, as it is after an output-on: the source change is the
            # operation and a read that fails must not undo it.
            if getattr(smu, "output_enabled", False):
                try:
                    self._tick()
                except Exception as exc:                     # noqa: BLE001
                    with self._lock:
                        self._failures += 1
                        self._last_error = f"{type(exc).__name__}: {exc}"
        self._bus.do(job, timeout_s=self.read_budget_s())
        return self.state()

    def set_output(self, on: bool) -> dict:
        """Output on or off.

        On is refused until something has set a source: after a `*RST` -- which
        every measurement routine in this project opens with -- the 2400 sits
        at 0 V on a 100 uA compliance, and an output switched on there is not
        the source the panel is drawing.
        """
        def job() -> None:
            smu = self._need()
            if on and getattr(smu, "panel", None) is None:
                raise PanelRefused(
                    "nothing has told the SourceMeter what to source: set the source "
                    "first, then switch the output on")
            smu.enable_output(bool(on))
            self._fresh_output(smu)                          # confirm, not assume
        # The wait is a read's budget, not the 30 s floor: the display may be
        # mid-tick, and a legal tick is 80 s.
        #
        # `cancel_on_timeout=on`, and the direction is the whole point. **An
        # ON that the caller gave up on is cancelled**: dropping it leaves the
        # source off, which is the safe end. **An OFF is never cancelled**:
        # dropping it leaves the source driving, which is the thing this
        # console exists to prevent, so it stays queued and the caller gets
        # `PanelPending`.
        #
        # This read `not on` for one commit -- the comment above it said
        # exactly what the code was failing to do, which is how a review
        # caught it and no test did: the test went at `_Bus.do` directly and
        # so pinned the primitive rather than this call.
        self._bus.do(job, timeout_s=self.read_budget_s() + SHUTDOWN_MARGIN_S,
                     cancel_on_timeout=on)
        if not on:
            with self._lock:
                # The display goes blank with the output, rather than keeping
                # the last numbers on screen: they were a measurement of a
                # moment that has passed, and nothing would say so.
                self._reading = None
            return self.state()
        # The output is on **now**, whatever happens next. So the first
        # reading is taken defensively: it is a nicety, and letting it throw
        # would answer an energised bench with a bare 500 carrying no state,
        # leaving the page showing the source off while it is driving. The
        # failure is recorded where the panel already reports a bad read.
        try:
            self._bus.do(self._tick, timeout_s=self.read_budget_s())
        except Exception as exc:                             # noqa: BLE001
            with self._lock:
                self._failures += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
        return self.state()

    def read(self) -> dict:
        """One reading now, whatever the display is doing.

        "The output is off" and "nothing has set a source" are things the
        bench will not do *now*, with a sentence each — a 409, like every
        other refusal here. They reached the generic 500 until a test of the
        cross-site check happened to ask for a reading with the output off.
        """
        def job() -> None:
            try:
                self._tick()
            except SourceMeterError as exc:
                raise PanelRefused(str(exc)) from None
        self._bus.do(job, timeout_s=self.read_budget_s())
        return self.state()

    def errors(self) -> list[str]:
        """Drain `:SYST:ERR?`. The one query here that is not a measurement --
        a panel that refuses something the *instrument* rejected has to be able
        to show what it said."""
        def job() -> list[str]:
            smu = self._need()
            reader = getattr(smu, "errors", None)
            return list(reader()) if callable(reader) else []
        return self._bus.do(job)

    def set_poll(self, interval_s: float | None) -> dict:
        """Start, restart or stop the free-running display."""
        if interval_s is not None:
            interval_s = float(interval_s)
            if not POLL_MIN_S <= interval_s <= POLL_MAX_S:
                raise ValueError(f"interval_s is between {POLL_MIN_S:g} and "
                                 f"{POLL_MAX_S:g} s, not {interval_s:g}")
        with self._lock:
            self._poll_s = interval_s
        self._bus.set_idle(None if interval_s is None else self._tick_quietly,
                           interval_s or 1.0)
        return self.state()

    # -- the reading --------------------------------------------------------
    def _tick(self) -> None:
        """One reading, on the worker. Raises what the driver raises."""
        smu = self._need()
        reading = smu.read_panel()
        with self._lock:
            self._readings += 1
            self._last_error = None
            self._reading = {"volts": reading.volts, "amps": reading.amps,
                             "ohms": reading.ohms, "compliance": reading.compliance,
                             "function": reading.function, "level": reading.level,
                             "at": time.time()}

    def _tick_quietly(self) -> None:
        """The display's own tick. **Nothing to read is not a failure**: with
        the output off, or before a source has been set, the 2400 shows dashes
        and `read_panel` raises -- so this skips instead of counting a fault
        and telling the operator an instrument that is answering has stopped.
        """
        if not self.available or getattr(self.smu, "panel", None) is None:
            return
        # The display is also what keeps the output flag honest: every tick
        # asks the instrument, so a hand on the 2400's OUTPUT key is noticed
        # within one interval rather than never. With the display off, the
        # flag is only as fresh as the last thing the operator clicked -- and
        # every one of those refreshes it too.
        try:
            if not self._fresh_output(self.smu):
                return
            self._tick()
        except Exception as exc:                             # noqa: BLE001
            with self._lock:
                self._failures += 1
                self._last_error = f"{type(exc).__name__}: {exc}"

    # -- the ceilings -------------------------------------------------------
    def _check_ceiling(self, setup: PanelSetup) -> None:
        if setup.current_compliance_a > self.ceiling["current_a"]:
            raise PanelRefused(
                f"current compliance {setup.current_compliance_a:g} A is above this "
                f"bench's ceiling of {self.ceiling['current_a']:g} A "
                "(rig.toml [sourcemeter] max_current_compliance_a)", "crit")
        if setup.voltage_compliance_v > self.ceiling["voltage_v"]:
            raise PanelRefused(
                f"voltage compliance {setup.voltage_compliance_v:g} V is above this "
                f"bench's ceiling of {self.ceiling['voltage_v']:g} V "
                "(rig.toml [sourcemeter] max_voltage_compliance_v)", "crit")
        limit, unit, key = ((self.ceiling["voltage_v"], "V", "max_voltage_compliance_v")
                            if setup.function == "voltage"
                            else (self.ceiling["current_a"], "A", "max_current_compliance_a"))
        if abs(setup.level) > limit:
            raise PanelRefused(
                f"sourcing {setup.level:g} {unit} is past this bench's ceiling of "
                f"{limit:g} {unit} (rig.toml [sourcemeter] {key})", "crit")


# -- the wire ---------------------------------------------------------------
def panel_wire(setup: PanelSetup | None) -> dict | None:
    """A `PanelSetup` as the page reads it, with the two things a screen would
    otherwise re-derive from `function`: which unit the level is in, and which
    of the two compliances is the one that bites."""
    if setup is None:
        return None
    out = dataclasses.asdict(setup)
    out["unit"] = setup.unit
    out["limit"] = setup.limit
    return out


def source_args(body: dict) -> dict:
    """`POST /api/source`'s body as `PanelSetup` fields.

    JSON has no integers and no enums, and the page's fields are typed by
    hand, so `4` and `"4"` both arrive for `averaging` and both mean four. A
    value that is not the shape of its field raises `ValueError`, which the
    server answers with a 422 carrying this sentence -- and the sentence is
    what the operator reads, so it names the field and quotes what they typed.
    """
    unknown = sorted(set(body) - set(SOURCE_FIELDS))
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(unknown)}; accepted: "
                         + ", ".join(SOURCE_FIELDS))
    out: dict[str, Any] = {}
    for name, raw in body.items():
        if name == "function":
            word = str(raw).strip().lower()
            if word not in FUNCTION_WORDS:
                raise ValueError(f"function: {raw!r} is neither -- a 2400 sources "
                                 "voltage or current")
            out[name] = FUNCTION_WORDS[word]
        elif name == "terminals":
            word = str(raw).strip().upper()[:4]
            if word not in ("FRON", "REAR"):
                raise ValueError(f"terminals: {raw!r} is neither FRON nor REAR")
            out[name] = word
        elif name == "four_wire":
            out[name] = as_bool(name, raw)
        elif name == "averaging":
            out[name] = _as_int(name, raw)
        elif name == "source_range":
            # An empty box is the instrument's autorange, not a zero range.
            out[name] = (None if raw is None or (isinstance(raw, str) and not raw.strip())
                         else _as_float(name, raw))
        else:
            out[name] = _as_float(name, raw)
    return out


def _as_float(name: str, raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: {raw!r} is not a number") from None
    if not math.isfinite(value):
        raise ValueError(f"{name}: {raw!r} is not a finite number")
    return value


def _as_int(name: str, raw: Any) -> int:
    value = _as_float(name, raw)
    if value != int(value):
        raise ValueError(f"{name}: {raw!r} is not a whole number")
    return int(value)


def as_bool(name: str, raw: Any) -> bool:
    """A JSON boolean, or a word that plainly is one. Never `bool()`.

    Python's truthiness makes every non-empty string true, so `bool("false")`
    is `True` -- and on the output route that turns a request meaning *off*
    into a live source. Anything this does not recognise raises, which the
    server answers with a 422: a control that energises a device does not
    guess.
    """
    if isinstance(raw, bool):
        return raw
    word = str(raw).strip().lower()
    if word in ("true", "1", "on", "yes"):
        return True
    if word in ("false", "0", "off", "no"):
        return False
    raise ValueError(f"{name}: {raw!r} is neither true nor false")


__all__ = ["KeithleyPanel", "PanelPending", "PanelRefused", "PanelUnavailable",
           "PanelSetup",
           "SOURCE_FIELDS", "POLL_DEFAULT_S", "POLL_MIN_S", "POLL_MAX_S",
           "panel_wire", "source_args", "as_bool", "PANEL_STATIC",
           "JOB_TIMEOUT_S", "SHUTDOWN_MARGIN_S"]
