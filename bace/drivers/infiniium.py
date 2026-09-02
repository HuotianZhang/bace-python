"""Agilent 90000-series Infiniium — transient acquisition.

Every command below is the literal format string recovered from the LabVIEW
driver in `instr.lib/Agilent 90000 Series`. Three behaviours in that driver are
not obvious and are reproduced deliberately:

1.  **Timebase is nanoseconds per division.** The panel control `Timebase
    (200 ns)` is divided by 1e8 before it reaches `:TIM:RANG` -- that is a ns
    to seconds conversion times ten divisions -- so 200 becomes a 2 us full
    screen. `Timebase * 1e-9 * 4` goes to `:TIM:POS`. `Record Length` goes to
    `:ACQ:POIN` unmodified.

    **Measured on the rig, 2026-09-01.** `:TIM:POS` is the time at the
    *centre* of the screen, so the record spans `TIM:POS +/- range/2`. With
    200 ns/div that is -200 ns to +1800 ns about the trigger, and the scope
    duly reported `:WAV:XOR? = -1.995e-7`. So **the trigger sits ~200 ns into
    the record**, not at its start.

    That is what makes the integration window make sense. `t0_int = 318 ns` is
    in record time, so it is 118 ns after the trigger, while the field arrives
    `delay + 47 ns` = 135 ns after it, i.e. 334 ns into the record. The
    integral therefore starts just before the transient does -- which is what
    it should do, and could not be checked until the scope said where its
    record begins.

    **And `:ACQ:POIN` is a request, not a promise.** The scope reported
    `:ACQ:POIN? = 5000` while `:WAV:POIN?` returned **4000**, with
    `:WAV:XINC? = 5.0e-10`. The record it can deliver is the window times the
    sample rate: 2 us at 2 GSa/s is 4000 points. That is where the archive's
    4000 comes from.

2.  **The averager is restarted by a double configure.** Averaging is set once
    with a count of 64, then again with the real count. Setting the count alone
    does not reliably restart accumulation on these scopes.

3.  **Range once, on the light trace; never between light and dark.** The
    light acquisition auto-ranges (`scale to maximum`) and the dark one does
    not. Re-ranging between the pair would change the digitiser scaling and
    invalidate the subtraction.

The fetch divides the scaled voltage by the sense resistor, so the arrays this
returns are **currents in amps**, matching `Fetch (Waveform).vi` with
`50 Ohm? = TRUE` -- and multiplies by `current_sign`, the one place the rig's
sign convention is applied (see `RigConfig.current_sign`).
"""
from __future__ import annotations

import time

import numpy as np

from .protocols import Trace

# ring index -> SCPI source token, decoded from the driver's own array constant
SOURCE = {0: "CHAN1", 1: "CHAN1", 2: "CHAN2", 3: "CHAN3", 4: "CHAN4",
          5: "FUNC1", 6: "FUNC2", 7: "FUNC3", 8: "FUNC4",
          9: "WMEM1", 10: "WMEM2", 11: "WMEM3", 12: "WMEM4"}


class ScopeError(RuntimeError):
    pass


class Infiniium:
    """One scope session. Not thread-safe -- VISA sessions never are."""

    def __init__(self, resource, *, sense_resistor_ohm: float = 5.192,
                 current_sign: float = -1.0,
                 probe_attenuation: float = 1.0, timeout_ms: int = 20000):
        self._io = resource                       # a pyvisa resource
        self._io.timeout = timeout_ms
        self.sense_resistor_ohm = sense_resistor_ohm
        self.current_sign = float(current_sign)
        """`RigConfig.current_sign`. Applied in `_fetch` only. Every
        construction site passes it from the rig config explicitly; the
        default here matches the bench so a bare `Infiniium(res)` in a test
        still reports in the rig's convention."""
        self.probe_attenuation = probe_attenuation
        self._ranged = False
        self.last_fetch_method = ""
        self.last_autorange_passes = 0
        self.last_autorange_clipped = False
        self.last_autorange_windows: list[tuple[float, float, bool]] = []
        self.last_autorange_unchanged = False
        self.last_y_increment = 0.0

    # -- session ----------------------------------------------------------
    def identify(self) -> str:
        return self._io.query("*IDN?").strip()

    def default_setup(self) -> None:
        self._io.write(":SYST:LONG OFF;\n:SYST:HEAD OFF;\n*CLS;\n*ESE 1;\n*SRE 32;")
        # :WAV:STR OFF, not ON. The LabVIEW driver turned streaming on, which is
        # for records too large for a definite-length block header (>999,999,999
        # bytes); a 4000-point WORD record is 8000 bytes and needs none of it.
        #
        # This was my first suspect for the empty fetch on the rig, on the
        # theory that pyvisa could not parse the indefinite `#0` header it
        # produces. That theory is wrong -- pyvisa 1.16.2 parses `#0` correctly.
        # Streaming off is still right, because a definite-length header lets
        # the reader know how many bytes to expect and lets `_fetch_volts`
        # notice a short transfer, but the empty fetch has another cause and
        # `_read_waveform` no longer depends on knowing what it is.
        self._io.write(":WAV:BYT MSBF;\n:WAV:FORM WORD;\n:WAV:VIEW MAIN;\n:WAV:STR OFF;")

    def configure_channel(self, channel: int, *, vertical_range: float,
                          offset: float = 0.0, display: bool = True) -> None:
        ch = f":CHAN{channel}"
        self._io.write(f"{ch}:DISP {'ON' if display else 'OFF'};")
        self._io.write(f"{ch}:OFFS {offset:g};RANG {vertical_range:g};")
        self._io.write(f"{ch}:PROB:EXT ON;")
        self._io.write(f"{ch}:PROB:EXT:GAIN {self.probe_attenuation:g};")

    def configure_timebase(self, timebase_ns_per_div: float, record_length: int) -> None:
        """`timebase_ns_per_div` is the panel number, e.g. 200."""
        full_screen_s = timebase_ns_per_div / 1e8          # ns -> s, x10 divisions
        position_s = timebase_ns_per_div * 1e-9 * 4.0      # four divisions in
        self._io.write(f":TIM:RANG {full_screen_s:g};")
        self._io.write(f":TIM:POS {position_s:g};")
        self._io.write(f":ACQ:POIN {int(record_length):d};")

    def configure_edge_trigger(self, source: str = "CHAN3", *, positive: bool = True,
                               high_threshold: float | None = None,
                               level: float | None = None,
                               sweep: str = "AUTO") -> None:
        """`sweep` is `AUTO` or `TRIG`, and the choice has consequences.

        The recovered driver used **AUTO**, so that is the default here. But
        AUTO means the scope sweeps anyway when no trigger arrives: if the
        81150A sync came loose, every trace would be untriggered noise, the dark
        subtraction would cancel it, and Q would come out near zero — a
        plausible-looking result from a disconnected cable.

        **TRIG** instead waits, so a missing trigger becomes a timeout with a
        message. That is the safer setting for a measurement and the reason this
        is exposed rather than fixed; it is a change to acquisition behaviour,
        so it is Huotian's to make, not mine.
        """
        self._io.write(":TRIG:MODE EDGE;")
        self._io.write(f":TRIG:EDGE:SOUR {source};")
        self._io.write(f":TRIG:EDGE:SLOP {'POS' if positive else 'NEG'};")
        self._io.write(f":TRIG:SWE {sweep};")
        if high_threshold is not None:
            self._io.write(f":TRIG:HTHR {source},{high_threshold:g};")
            self._io.write(f":TRIG:LTHR {source},0;")
        if level is not None:
            self._io.write(f":TRIG:LEV {source},{level:g};")

    # -- acquisition ------------------------------------------------------
    def _configure_acquisition(self, averaging: bool, count: int) -> None:
        self._io.write(":ACQ:MODE RTIM;"
                       f"{':ACQ:AVER ON;' if averaging else ':ACQ:AVER OFF;'}"
                       f":ACQ:AVER:COUN {int(count):d};"
                       ":ACQ:INT OFF;")

    def _restart_averager(self, count: int) -> None:
        """The double-configure the LabVIEW driver relies on."""
        self._configure_acquisition(averaging=True, count=64)
        self._io.write(":RUN;")
        self._configure_acquisition(averaging=True, count=count)

    def _clear_acquisition_flag(self) -> None:
        """Read and discard `:ADER?` so the next read means *this* acquisition.

        The Acquisition Done Event Register is a latch, cleared on read. Left
        set by a previous acquisition it returns `+1` immediately and the wait
        ends before anything has been captured -- which with 200 hardware
        averages would mean fetching a partial average and never knowing. The
        bench transcript showed `:ADER?` answering `+1` about a millisecond
        after `:RUN`, which is what prompted looking.
        """
        try:
            self._io.query(":ADER?;")
        except Exception:
            pass

    def _wait_for_acquisition(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if int(self._io.query(":ADER?;").strip() or 0):
                return
            time.sleep(0.2)
        raise ScopeError(
            f"acquisition did not complete within {timeout_s:g} s — check that the "
            "trigger source is actually receiving edges"
        )

    def autorange(self, source: str = "CHAN2", *, scale_factor: float = 1.5,
                  offset_divisor: float = 2.0, channel: int = 2,
                  passes: int = 6, growth: float = 1.8,
                  max_range_v: float = 8.0, deadband: float = 0.02,
                  verify: bool = False) -> tuple[float, float]:
        """Iterate to a vertical range that holds the whole signal.

        Once the trace fits inside the window, the range and offset are exactly
        what `scale to maximum.vi` computes:

            vertical range  = |scale_factor * (max - min)|      (volts)
            vertical offset = (max + min) / offset_divisor      (volts)

        Recovered from the wire graph: Array Max & Min -> Subtract -> Divide by
        `Scale off.` -> Multiply by `Scale_fac.` -> Absolute Value -> the
        Vertical Range input of Configure Channel; and Add -> Divide by
        `Scale off.` -> the Vertical Offset. The panel supplies Scale_fac. = 1.5
        and Scale off. = 2, and a peak-to-peak under 1 mV falls back to a 10 mV
        span rather than collapsing the range.

        **It has to iterate, and the original does.** `scale to maximum.vi`
        wraps all of that in a FOR loop, which I first flattened to a single
        pass. The rig showed why on 2026-09-01: the channel sat at 0.15 V while
        the true peak was 0.127 V, so the first acquisition clipped at 0.075 V.

        **Deviation from the original, deliberate.** The original applies the
        formula above on every pass, clipped or not. That is wrong in a way that
        only costs time: max and min read off a clipped trace are *lower bounds*
        on the real excursion, so the range they produce is too small and the
        next pass clips again. On the rig that formula crept 0.150 -> 0.118 ->
        0.152 -> 0.191 V, four acquisitions to reach a window it could have
        reached in two. So while the trace is clipped this ignores the numbers
        the trace reports -- they carry no information about how far past the
        rail the signal went -- and instead grows the window geometrically by
        `growth`, anchored on whichever edge did *not* clip so the new span is
        spent on the side the signal actually ran off. The formula is applied
        once, to the first unclipped acquisition. Same answer, fewer passes: at
        ~150 ms per acquisition and 300 steps a run, two saved passes is about
        three minutes.

        Stops as soon as a pass comes back **unclipped**, after `passes`
        attempts, or when the instrument stops honouring a larger range (it is
        at its own ceiling, or `max_range_v`). `last_autorange_passes` and
        `last_autorange_clipped` record how many it took and whether it gave up
        while still clipping -- the second is worth reporting, because a clipped
        light trace makes every charge from that point an underestimate.

        **Light trace only.** `Read unblocked TDCF.vi` calls this
        unconditionally; `Read blocked.vi` contains the same call behind a
        hard-coded FALSE, so the dark trace inherits whatever range the light
        trace left on the instrument. That is not merely a convention -- the two
        ranges must match for the pair to share a quantisation, and the
        instrument's per-range offset calibration would otherwise put a small
        step into the difference.
        """
        vrange = self._channel_range(channel) or 1.0
        voffset = self._channel_offset(channel) or 0.0
        self.last_autorange_passes = 0
        self.last_autorange_clipped = False
        # (range, offset, clipped) for each acquisition, so a bench report can
        # show what the loop actually tried rather than only where it landed.
        self.last_autorange_windows = []
        self.last_autorange_unchanged = False

        for attempt in range(max(1, passes)):
            # The window in force for the acquisition about to be taken.
            top, bottom = voffset + vrange / 2.0, voffset - vrange / 2.0

            self._clear_acquisition_flag()
            self._io.write(":RUN;:WAV:FORM WORD;")
            self._wait_for_acquisition(10.0)
            self._io.write(":STOP;")            # a finished record, not a live one
            tr = self._fetch_volts(source)      # volts, not amps -- see _fetch_volts
            hi, lo = float(tr.y.max()), float(tr.y.min())

            # Did the trace touch the limits? If so `hi` and `lo` are lower
            # bounds on the real excursion and tell us nothing about how much
            # more range is needed -- only that more is.
            edge = max(abs(vrange) * 1e-3, 1e-9)
            hit_top = hi >= top - edge
            hit_bottom = lo <= bottom + edge
            clipped = hit_top or hit_bottom

            self.last_autorange_passes = attempt + 1
            self.last_autorange_clipped = clipped
            self.last_autorange_windows.append((float(vrange), float(voffset), clipped))

            if not clipped:
                pk = hi - lo
                if abs(pk) < 1e-3:
                    pk = 1e-2
                new_range = abs(scale_factor * pk)
                new_offset = (hi + lo) / offset_divisor

                # Deadband. `Read unblocked TDCF.vi` re-ranges on every step, and
                # after the first one the answer barely moves -- so on nearly
                # every step of a real run this would rewrite the channel to
                # within a percent of where it already is, and then spend 150 ms
                # reading it back. Report 9 timed that `:CHAN2:RANG?`: 145, 149
                # and 135 ms, because the query blocks while the front end
                # settles. Over 300 steps that is a minute of nothing.
                #
                # Leaving it alone is also better physics. The light and dark
                # traces of a step must share a quantisation, which is why the
                # dark trace inherits the light trace's range; holding the range
                # still across *steps* extends the same property to the whole
                # run, so loops are averaged on one scale rather than 100
                # slightly different ones.
                moved = (abs(new_range - vrange) > deadband * abs(vrange)
                         or abs(new_offset - voffset) > deadband * abs(new_range))
                if not moved:
                    self.last_autorange_unchanged = True
                    break

                vrange, voffset = new_range, new_offset
                self.configure_channel(channel, vertical_range=vrange, offset=voffset)
                if verify:
                    # Report what the instrument took *if it disagrees* -- a
                    # scope that quantises its ranges would otherwise put a
                    # fiction in the run metadata. But only if it disagrees:
                    # this one answers `:CHAN2:RANG? = 1.91E-01` to a request
                    # for 0.191561, three significant figures of its own
                    # display, and taking that at face value would throw away
                    # precision rather than gain it.
                    #
                    # Off by default, because the pair is metadata and nothing
                    # else: every sample is scaled by the preamble's own
                    # y_increment and y_origin at fetch time, never by these. So
                    # a quantising scope could not corrupt a single data point
                    # through them -- and the query costs ~150 ms per step. The
                    # bench harness turns it on; a 300-step run does not.
                    vrange = self._settled(self._channel_range(channel), vrange)
                    voffset = self._settled(self._channel_offset(channel), voffset)
                break

            # Clipped: grow geometrically, anchored on the edge that held.
            previous = abs(vrange)
            grown = min(previous * growth, max_range_v)
            if hit_top and hit_bottom:
                pass                              # both rails: grow about centre
            elif hit_top:
                voffset = bottom + grown / 2.0    # keep the floor, raise the ceiling
            else:
                voffset = top - grown / 2.0       # keep the ceiling, drop the floor
            vrange = grown
            self.configure_channel(channel, vertical_range=vrange, offset=voffset)

            # Read back what the instrument actually took. This is not
            # bookkeeping: the clip test above compares the trace to `top` and
            # `bottom`, which are computed from these two numbers, so a scope
            # that clamped or quantised the request would leave the driver
            # testing against a window that does not exist -- and a clipped
            # trace would come back looking clean. A test with a scope capped at
            # 0.10 V caught exactly that when this readback was briefly removed.
            #
            # A range that did not grow means the instrument is at its ceiling;
            # every remaining pass would clip identically. Compare against the
            # range *before* the request, not against the request itself: this
            # scope replies to three significant figures, so 1.5746 comes back
            # as 1.57 and an equality test would read that as a refusal.
            applied = self._channel_range(channel)
            if applied is not None:
                vrange = applied
                reported = self._channel_offset(channel)
                if reported is not None:
                    voffset = reported
                if applied <= previous * 1.001:
                    break

        self._io.write(":RUN;")
        self._ranged = True
        return float(vrange), float(voffset)

    @staticmethod
    def _settled(reported: float | None, asked: float,
                 tolerance: float = 0.02) -> float:
        """Which of the two to believe about a setting just written.

        The instrument's reply wins only when it differs by more than
        `tolerance` -- enough to see a quantised range (a 1-2-5 step is at least
        25 % away) and to ignore a reply rounded for display (three significant
        figures is at most 0.5 % away).
        """
        if reported is None:
            return float(asked)
        scale = max(abs(asked), abs(reported))
        if scale and abs(reported - asked) > tolerance * scale:
            return float(reported)
        return float(asked)

    def _channel_range(self, channel: int) -> float | None:
        return self._query_float(f":CHAN{channel}:RANG?")

    def _channel_offset(self, channel: int) -> float | None:
        return self._query_float(f":CHAN{channel}:OFFS?")

    def _query_float(self, command: str) -> float | None:
        try:
            return float(str(self._io.query(command)).strip())
        except Exception:
            return None

    def _read_waveform(self, expected_points: int) -> np.ndarray:
        """`:WAV:DATA?`, by whichever route actually delivers the samples.

        The first two bench acquisitions got a preamble promising 4000 points
        and then an empty array from `query_binary_values`, with no error in the
        instrument's queue. Several explanations were plausible and the obvious
        one (an indefinite `#0` block from `:WAV:STR ON`) turned out to be
        wrong, so this stopped depending on the diagnosis: take pyvisa's answer
        when it is the right length, and otherwise read the bytes and parse the
        IEEE block here.

        `last_fetch_method` records which route was used, so a bench report says
        whether the fallback was needed.
        """
        try:
            raw = self._io.query_binary_values(":WAV:DATA?", datatype="h",
                                               is_big_endian=True,
                                               container=np.array)
            self.last_fetch_method = "query_binary_values"
        except Exception as exc:
            raw = np.array([], dtype=np.int16)
            self.last_fetch_method = f"query_binary_values raised {type(exc).__name__}"

        if expected_points and raw.size == expected_points:
            return raw

        manual = self._read_waveform_raw()
        if manual.size:
            self.last_fetch_method = (
                f"raw read ({self.last_fetch_method} gave {raw.size} of "
                f"{expected_points})")
            return manual
        return raw

    def _read_waveform_raw(self) -> np.ndarray:
        """Read `:WAV:DATA?` as bytes and parse the IEEE block by hand."""
        try:
            self._io.write(":WAV:DATA?")
            data = bytes(self._io.read_raw())
        except Exception:
            return np.array([], dtype=np.int16)

        start = data.find(b"#")
        if start < 0:
            return np.array([], dtype=np.int16)
        try:
            digits = int(data[start + 1:start + 2])
        except ValueError:
            return np.array([], dtype=np.int16)
        if digits == 0:                       # indefinite block: run to the end
            payload = data[start + 2:]
        else:
            head = start + 2 + digits
            declared = int(data[start + 2:head])
            payload = data[head:head + declared]
        payload = payload[:len(payload) // 2 * 2]
        return np.frombuffer(payload, dtype=">i2")

    def _fetch_volts(self, source: str) -> Trace:
        """The raw channel trace, in **volts at the scope input**.

        This is what `scale to maximum.vi` works in. Its `Fetch (Waveform).vi`
        call leaves the `50 Ohm?` boolean unwired, so no resistor division
        happens there, and the range it computes goes straight back to
        `:CHAN:RANG` -- which is a volts setting. Computing the range from
        resistor-divided data would set it a factor R too small and clip every
        light trace.
        """
        self._io.write(f":WAV:SOUR {source};")
        pre = self._io.query(":WAV:POIN?;:WAV:XOR?;:WAV:XINC?;"
                             ":WAV:YOR?;:WAV:YINC?;:WAV:YREF?;")
        npts, xor, xinc, yor, yinc, yref = [float(v) for v in pre.strip().split(";") if v]
        raw = self._read_waveform(int(npts) if npts else 0)
        if raw.size == 0:
            raise ScopeError(
                f"{source} returned no samples, though the preamble said "
                f"{npts:g} points. The acquisition was not stopped before the "
                "fetch, or the block header could not be parsed."
            )
        if npts and raw.size != int(npts):
            raise ScopeError(
                f"{source} returned {raw.size} samples but the preamble said "
                f"{int(npts)} — the transfer was truncated, most likely by a "
                "read termination character inside the binary block."
            )
        # The digitiser step is proportional to the vertical range in force, so
        # it is a free readback of that range -- see `autorange`, which uses it
        # instead of a `:CHAN:RANG?` query that costs 150 ms on this scope.
        self.last_y_increment = yinc
        volts = (raw.astype(float) - yref) * yinc + yor
        return Trace(y=volts, dt=xinc, t0=xor)

    def _fetch(self, source: str) -> Trace:
        """The trace in **amps**, as `Read (Single Waveform).vi` returns it.

        That VI passes `50 Ohm? = TRUE` and `Resistor = 5.192` into the fetch,
        so the engine's arrays are currents. Both `Read blocked.vi` and
        `Read unblocked TDCF.vi` wire the constant TRUE explicitly.

        `current_sign` is applied here and nowhere else. It is the digitiser
        chain's convention (this port read every current positive, the LabVIEW
        engine every one negative, 2026-09-02), so it belongs beside the
        resistor division that is the rest of that chain -- not in
        `_fetch_volts`, which the auto-range uses to compute a window in volts,
        and not in `core.process`, which is validated numerics.
        """
        tr = self._fetch_volts(source)
        return Trace(y=self.current_sign * tr.y / self.sense_resistor_ohm,
                     dt=tr.dt, t0=tr.t0)

    @property
    def clipped(self) -> bool:
        """`Digitizer.clipped` — the auto-range gave up while still on a rail.

        An alias for `last_autorange_clipped`, which is the name this driver has
        always used and the reason `StepDone.clipped` read `False` on every real
        shot until 2026-09-01: `transient.py` asked the scope for `clipped`,
        only the simulator had it, and the `getattr` default swallowed the
        difference. The property is now in the `Digitizer` protocol, so the two
        cannot drift apart again without a test failing.

        Only the light trace updates it — the dark trace inherits the light
        trace's window by design — so this describes the window *both* traces of
        a step were taken in, which is what a consumer wants.

        It reports what the auto-range concluded, not what the samples show. A
        trace can sit on a rail without the auto-range noticing (the range was
        set elsewhere, or the loop stopped early), so the bench check separately
        counts samples resting on the extreme code.
        """
        return bool(self.last_autorange_clipped)

    def fetch_volts(self, source: str) -> Trace:
        """One channel's trace in **volts at the input**, with no sense-resistor
        division and no averaging or ranging of its own.

        `acquire` returns amps because the channel it is built around carries a
        current through the 5.192 ohm resistor. The sync lines and the LED drive
        are voltages, and a caller reading several channels out of one record
        wants them as they are. Public because `bench.sync` needs exactly this
        and going through the private name would be pretending it does not.
        """
        return self._fetch_volts(source)

    def single_acquisition(self, timeout_s: float = 30.0) -> None:
        """Arm, wait for one complete acquisition, stop. Nothing else.

        No averaging, no auto-range, no fetch — for a caller that has configured
        the window itself and wants several channels out of the same record. It
        exists so that sequence is written once: clear `:ADER?` first because it
        is a latch cleared on read and a stale one ends the wait immediately,
        and `:STOP` before any fetch because reading a running scope returns a
        partial record or none. Both were bench defects (#5 and #4).
        """
        self._clear_acquisition_flag()
        self._io.write(":RUN;")
        self._wait_for_acquisition(timeout_s)
        self._io.write(":STOP;")

    def acquire(self, n_averages: int, *, source: str = "CHAN2",
                autorange_first: bool = False, timeout_s: float = 30.0) -> Trace:
        """One hardware-averaged acquisition.

        `autorange_first=True` for the light trace, False for the dark one —
        see the module docstring.
        """
        self._io.write(":RUN;:WAV:FORM WORD;")
        if autorange_first:
            self.autorange(source)
        elif not self._ranged:
            raise ScopeError(
                "no vertical range has been set — call acquire(..., autorange_first=True) "
                "for the light trace before acquiring the dark one, or configure the "
                "channel explicitly"
            )
        self._restart_averager(n_averages)
        self._clear_acquisition_flag()
        self._io.write(":RUN;")
        self._wait_for_acquisition(timeout_s)
        # Stop first. Reading a channel while the scope is still acquiring can
        # return a partial record or none at all, and the average is only
        # complete once acquisition has ended anyway.
        self._io.write(":STOP;")
        trace = self._fetch(source)
        self._io.write(":MEAS:CLE;")
        return trace

    def close(self) -> None:
        try:
            self._io.write(":STOP;")
        finally:
            self._io.close()
