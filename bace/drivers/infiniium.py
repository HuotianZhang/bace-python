"""The Agilent 90000-series Infiniium acquires transient current traces.

The timebase follows the LabVIEW command sequence: ns per division divided
by 1e8 sets `:TIM:RANG` for ten divisions; four divisions set `:TIM:POS`.
`:ACQ:POIN` receives the requested record length.

On this bench, `:TIM:POS` denotes the record centre. At 200 ns/div the
record spans -200 to +1800 ns relative to the trigger; `:WAV:XOR?` reads
-1.995e-7 s. Field arrival is delay plus `trigger_offset_s`. The latency
is 35.1 ns, measured with a capacitor in the device's place (`docs/notes.md`).
At delay 90 ns the field arrives about
325 ns into this record. The LabVIEW reference at delay 88 ns starts integration at
318 ns, about 5 ns before its field arrival. Record origin is needed
to place the integration window relative to the field.

The requested record length can differ from the delivered length. On this
bench `:ACQ:POIN?` reads 5000 while `:WAV:POIN?` reads 4000 and
`:WAV:XINC?` reads 5.0e-10 s: 2 us at 2 GSa/s gives 4000 samples.

The DSO9054H retains averages across `:STOP`/`:RUN`, the LabVIEW double
configuration of counts 64 and N_avg, and a rewrite of the same range.
A changed range clears them. Without clearing, dark counts exceed light
counts by about 27, mixing light into dark; a point within the range
deadband also inherits the preceding dark trace. Q can scale as 25/count,
falling to about one half or one third of its value. `:CDIS` clears a count
of 200 to 0; averaging off and on clears 1 to 0. `:DIG` with `*OPC?` waits
for N_avg: 32 averages take 0.42 s. While running, `:WAV:COUN?` reports the
configured count. Counts must be read stopped. These measurements are in
`docs/analysis/2026-09-06/README.md`.

Each averaged acquisition therefore stops, disables averaging, sends
`:CDIS`, enables averaging and rejects a nonzero count. Autorange uses
unaveraged acquisitions. `:DIG` and `*OPC?` complete the average; `:ADER?`
only marks an individual acquisition. The fetched count must reach N_avg.

Only the light trace sets the range. Both traces of a shot share digitiser
scaling and range-dependent offset calibration for subtraction. Fetching
converts V to A with R_s and applies `current_sign` once, matching the
LabVIEW current convention."""
from __future__ import annotations

import time

import numpy as np

from .protocols import Trace

# LabVIEW source-ring index to SCPI source token.
SOURCE = {0: "CHAN1", 1: "CHAN1", 2: "CHAN2", 3: "CHAN3", 4: "CHAN4",
          5: "FUNC1", 6: "FUNC2", 7: "FUNC3", 8: "FUNC4",
          9: "WMEM1", 10: "WMEM2", 11: "WMEM3", 12: "WMEM4"}


class ScopeError(RuntimeError):
    pass


class Infiniium:
    """Control one Infiniium VISA session without concurrent access."""

    def __init__(self, resource, *, sense_resistor_ohm: float = 5.192,
                 current_sign: float = -1.0,
                 probe_attenuation: float = 1.0, timeout_ms: int = 20000):
        self._io = resource                       # a pyvisa resource
        self._io.timeout = timeout_ms
        self.sense_resistor_ohm = sense_resistor_ohm
        self.current_sign = float(current_sign)
        """Apply the rig's current sign in `_fetch` only.

        The default -1 matches the bench. Explicit construction from
        `RigConfig.current_sign` keeps tests and runs in the same convention."""
        self.probe_attenuation = probe_attenuation
        self._ranged = False
        self.window: tuple[float, float] | None = None
        self.last_fetch_method = ""
        self.last_autorange_passes = 0
        self.last_autorange_clipped = False
        self.last_autorange_windows: list[tuple[float, float, bool]] = []
        self.last_autorange_unchanged = False
        self.last_y_increment = 0.0
        self.last_count_after_reset: int | None = None

    # -- session ----------------------------------------------------------
    def identify(self) -> str:
        return self._io.query("*IDN?").strip()

    def default_setup(self) -> None:
        self._io.write(":SYST:LONG OFF;\n:SYST:HEAD OFF;\n*CLS;\n*ESE 1;\n*SRE 32;")
        # Streaming serves records above 999,999,999 bytes; a 4000-sample WORD
        # record needs only 8000 bytes. A definite-length header lets
        # `_fetch_volts` detect short transfers. On this bench PyVISA 1.16.2 also
        # parses indefinite `#0` headers, so streaming does not explain an empty
        # fetch. `_read_waveform` handles a short PyVISA result independently.
        self._io.write(":WAV:BYT MSBF;\n:WAV:FORM WORD;\n:WAV:VIEW MAIN;\n:WAV:STR OFF;")

    def configure_channel(self, channel: int, *, vertical_range: float,
                          offset: float = 0.0, display: bool = True) -> None:
        ch = f":CHAN{channel}"
        self._io.write(f"{ch}:DISP {'ON' if display else 'OFF'};")
        self._io.write(f"{ch}:OFFS {offset:g};RANG {vertical_range:g};")
        self._io.write(f"{ch}:PROB:EXT ON;")
        self._io.write(f"{ch}:PROB:EXT:GAIN {self.probe_attenuation:g};")

    def configure_timebase(self, timebase_ns_per_div: float, record_length: int) -> None:
        """Send the ten-division range, centre position and requested sample count.

        `timebase_ns_per_div` is in ns/div. The range and position commands use s.
        Return `None`; the delivered sample count can differ from the request."""
        full_screen_s = timebase_ns_per_div / 1e8          # ns -> s, x10 divisions
        position_s = timebase_ns_per_div * 1e-9 * 4.0      # four divisions in
        self._io.write(f":TIM:RANG {full_screen_s:g};")
        self._io.write(f":TIM:POS {position_s:g};")
        self._io.write(f":ACQ:POIN {int(record_length):d};")

    def configure_edge_trigger(self, source: str = "CHAN3", *, positive: bool = True,
                               high_threshold: float | None = None,
                               level: float | None = None,
                               sweep: str = "AUTO") -> None:
        """Send edge source, slope, sweep and optional thresholds in V; return `None`.

        `AUTO` matches the LabVIEW sequence but acquires without trigger edges.
        A loose Sync cable can therefore yield noise and a plausible near-zero Q.
        `TRIG` waits for an edge, making a missing trigger a timeout."""
        self._io.write(":TRIG:MODE EDGE;")
        if source.upper().startswith("CHAN"):
            # `:DIG` acquires displayed channels. The trigger channel must remain
            # visible so the same record contains both Sync and current. A channel
            # list such as `:DIG CHAN2` disables other channel displays on this bench.
            self._io.write(f":{source}:DISP ON;")
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

    def _reset_averager(self, count: int) -> None:
        """Stop, clear and configure N_avg as `count`; return `None`.

        On the DSO9054H, `:STOP`/`:RUN`, count changes (52 to 125 under the
        LabVIEW double configuration) and an unchanged range retain averages.
        `:CDIS` clears 200 to 0; averaging off and on clears 1 to 0
        (`docs/analysis/2026-09-06/README.md`). Both clearing operations precede
        the stopped count check. A nonzero `last_count_after_reset` means the
        buffer can mix a preceding acquisition into this trace and raises."""
        self._io.write(":STOP;")
        self._configure_acquisition(averaging=False, count=count)
        self._io.write(":CDIS;")
        self._configure_acquisition(averaging=True, count=count)
        self.last_count_after_reset = self._query_count()
        if self.last_count_after_reset:
            raise ScopeError(
                f"the averager did not empty: :WAV:COUN? reads "
                f"{self.last_count_after_reset} after :CDIS and averaging off/on, "
                "so the buffer still holds the previous acquisition and this one "
                "would be folded into it"
            )

    def _digitize(self, timeout_s: float) -> None:
        """Send `:DIG`, wait on `*OPC?` and leave the scope stopped; return `None`.

        `timeout_s` is in s. With averaging enabled, completion covers N_avg:
        32 averages take 0.42 s on this bench, with a stopped count of 32
        (`docs/analysis/2026-09-06/README.md`). `:ADER?` marks the first
        acquisition and can allow a fetch at about 25 of 200 averages."""
        previous = self._io.timeout
        self._io.timeout = max(1000, int(timeout_s * 1000))
        try:
            # Bare `:DIG` acquires all displayed channels. A channel list disables
            # the others on this bench, removing CHAN3 Sync from the record fetched
            # beside current. `configure_edge_trigger` keeps that source displayed.
            self._io.write(":DIG;")
            self._io.query("*OPC?")
        except Exception as exc:                            # noqa: BLE001
            self._io.write(":STOP;")
            raise ScopeError(
                f"acquisition did not complete within {timeout_s:g} s "
                f"({type(exc).__name__}) — check that the trigger source is actually "
                "receiving edges"
            ) from exc
        finally:
            self._io.timeout = previous

    def _single_shot(self, timeout_s: float) -> None:
        """Clear the display and send one unaveraged `:DIG`; return `None`.

        `timeout_s` is in s. Autorange needs signal extrema in the active range.
        A retained average can instead contain the preceding point's dark trace."""
        self._io.write(":STOP;:ACQ:AVER OFF;:CDIS;")
        self._digitize(timeout_s)

    def _query_count(self) -> int | None:
        """Query stopped `:WAV:COUN?` and return an integer or `None`.

        Zero means an empty average. While running, the DSO9054H reports the
        configured count rather than progress
        (`docs/analysis/2026-09-06/README.md`)."""
        try:
            return int(float(self._io.query(":WAV:COUN?").strip()))
        except Exception:                                   # noqa: BLE001
            return None
    def _clear_acquisition_flag(self) -> None:
        """Read and discard the `:ADER?` latch; return `None`.

        Reading clears the latch. A retained flag can end a wait before an
        acquisition completes, allowing a partial average to be fetched.
        On this bench a retained flag answers `+1` about 1 ms after `:RUN`."""
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
        """Acquire unaveraged traces and set a range and offset in V that fit them.

        Return `(range, offset)` in V. For an unclipped trace the LabVIEW
        `scale to maximum.vi` sequence gives range = |`scale_factor` * (max - min)|
        and offset = (max + min) / `offset_divisor`. Its panel uses factors 1.5
        and 2. A span below 1 mV substitutes 10 mV to avoid range collapse.

        Clipped extrema bound the excursion but cannot reveal its full size.
        On this bench a 0.15 V range clips a 0.127 V peak at 0.075 V. Applying
        the fit formula to clipped data gives ranges 0.150, 0.118, 0.152 and
        0.191 V over four acquisitions. Geometric growth spends the added span
        on the clipped side, anchored on the other edge, and reaches the same
        fit in two acquisitions. At about 150 ms per acquisition, saving two
        passes at each of 300 points saves about three minutes on this bench.

        Stop after an unclipped pass, `passes` attempts, `max_range_v`, or the
        instrument's range ceiling. `last_autorange_passes` and
        `last_autorange_clipped` report the outcome. Clipping a light trace
        underestimates the magnitude of Q.

        Only the light trace ranges. This matches the LabVIEW light/dark
        sequence: the dark trace inherits the range. Equal ranges preserve
        quantisation and prevent per-range offset calibration from adding a
        step to light minus dark."""
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

            self._single_shot(10.0)
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

                # The deadband avoids near-identical range writes and settling queries.
                # `:CHAN2:RANG?` takes 145, 149 and 135 ms on this bench while the front
                # end settles, about a minute over 300 points. Holding range across
                # points also keeps loops on one quantisation scale, extending the
                # shared scaling required for light and dark traces.
                moved = (abs(new_range - vrange) > deadband * abs(vrange)
                         or abs(new_offset - voffset) > deadband * abs(new_range))
                if not moved:
                    self.last_autorange_unchanged = True
                    break

                vrange, voffset = new_range, new_offset
                self.configure_channel(channel, vertical_range=vrange, offset=voffset)
                if verify:
                    # Read-back replaces metadata only for a material difference.
                    # On this bench a 0.191561 V request reads `1.91E-01`, rounded
                    # to three significant figures. Using it directly loses precision.
                    #
                    # Verification is optional: samples use preamble `yinc` and `yor`,
                    # independently of this metadata. Quantised settings cannot corrupt
                    # sample scaling here. The query costs about 150 ms per point on
                    # this bench, so verification is reserved for bench checks.
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

            # Read-back is required after growth: `top` and `bottom` must describe
            # the applied range, including any instrument clamp or quantisation.
            # Otherwise a scope capped at 0.10 V can appear unclipped against a
            # larger requested window.
            #
            # No growth means further passes clip identically at the range ceiling.
            # Compare with the preceding range, not the request: this scope reports
            # three significant figures, so a 1.5746 V request reads 1.57 V. Exact
            # equality to the request would mistake rounding for refusal.
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
        self.window = (float(vrange), float(voffset))
        return self.window

    @staticmethod
    def _settled(reported: float | None, asked: float,
                 tolerance: float = 0.02) -> float:
        """Return the reported setting only when it differs beyond `tolerance`.

        A 1-2-5 range step differs by at least 25 %, while three-significant-
        figure display rounding differs by at most 0.5 %. The tolerance keeps
        rounding from discarding request precision."""
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
        """Query `:WAV:DATA?` and return raw integer samples.

        On this bench, `query_binary_values` can return an empty array for a
        4000-sample preamble without an instrument error-queue entry. PyVISA
        1.16.2 parses indefinite `#0` blocks, so that header does not explain
        the empty transfer. A wrong-length result triggers a raw IEEE-block
        read. `last_fetch_method` records whether the fallback supplies data."""
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
        """Query `:WAV:DATA?` as bytes and return decoded integer samples.

        Parse the IEEE block header directly. Missing or unreadable data returns
        an empty array. Samples are signed 16-bit, most-significant byte first."""
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
        """Query the waveform preamble and data and return a `Trace` in V.

        `dt` and `t0` are in s. The LabVIEW range sequence leaves `50 Ohm?`
        unwired, so it also works in input V. Dividing by R_s before calculating
        a range makes the range too small by that factor and clips light traces."""
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
        # `yinc` in V per code tracks the applied vertical range. Keeping it
        # allows scaling checks without a separate `:CHAN:RANG?` query, which
        # takes about 150 ms on this bench.
        self.last_y_increment = yinc
        volts = (raw.astype(float) - yref) * yinc + yor
        return Trace(y=volts, dt=xinc, t0=xor)

    def _fetch(self, source: str) -> Trace:
        """Fetch a waveform and return a `Trace` in A with `dt` and `t0` in s.

        The LabVIEW light and dark fetches use `50 Ohm? = TRUE` and
        `Resistor = 5.192`, so their arrays are currents. Division by R_s and
        `current_sign` occur here once. The bench convention makes extraction
        current negative, matching LabVIEW. `_fetch_volts` remains in V for
        range calculations; `bace.core.process` receives currents."""
        tr = self._fetch_volts(source)
        return Trace(y=self.current_sign * tr.y / self.sense_resistor_ohm,
                     dt=tr.dt, t0=tr.t0)

    @property
    def clipped(self) -> bool:
        """Return whether autorange ends with the light trace on a rail.

        This exposes `last_autorange_clipped` through `Digitizer.clipped`.
        Both traces share that range; the dark trace does not update this flag.
        The protocol requires the property so a missing name cannot silently
        become `False` through a caller's default.

        The flag describes autorange's conclusion. A range set elsewhere or an
        early stop can still leave samples on a rail. Bench checks therefore
        also count samples at the extreme code."""
        return bool(self.last_autorange_clipped)

    def fetch_volts(self, source: str) -> Trace:
        """Fetch a channel from the stopped record and return its `Trace` in V.

        `dt` and `t0` are in s. No averaging, ranging, resistor division or sign
        change occurs. Sync and LED drive channels carry voltage; the current
        channel's conversion through R_s belongs in `acquire`."""
        return self._fetch_volts(source)

    def single_acquisition(self, timeout_s: float = 30.0) -> None:
        """Clear `:ADER?`, send `:RUN`, wait and send `:STOP`; return `None`.

        `timeout_s` is in s. The caller configures the range and fetches channels
        from this record. Clearing the latch prevents immediate false completion.
        Stopping before fetch avoids a partial or empty record on this bench.
        This method does not configure averaging or autorange."""
        self._clear_acquisition_flag()
        self._io.write(":RUN;")
        self._wait_for_acquisition(timeout_s)
        self._io.write(":STOP;")

    def acquire(self, n_averages: int, *, source: str = "CHAN2",
                autorange_first: bool = False, timeout_s: float = 30.0,
                window: tuple[float, float] | None = None) -> Trace:
        """Send averaging and acquisition commands and return a current `Trace` in A.

        `dt` and `t0` are in s. `n_averages` sets N_avg and `timeout_s` is in s.
        `autorange_first=True` ranges the light trace only. For a dark trace
        taken first, `window` supplies the light trace's range and offset in V.
        `:DIG` and `*OPC?` wait for completion; the stopped count must reach
        N_avg when the instrument supplies it."""
        self._io.write(":RUN;:WAV:FORM WORD;")
        if autorange_first:
            self.autorange(source)
        elif window is not None:
            channel = int(str(source).upper().removeprefix("CHAN"))
            self.configure_channel(channel, vertical_range=window[0], offset=window[1])
            self.window = (float(window[0]), float(window[1]))
            self._ranged = True
        elif not self._ranged:
            raise ScopeError(
                "no vertical range has been set — call acquire(..., autorange_first=True) "
                "for the light trace before acquiring the dark one, or configure the "
                "channel explicitly"
            )
        self._reset_averager(n_averages)
        self._digitize(timeout_s)           # stopped when it returns
        trace = self._fetch(source)
        count = self._averages_folded()
        if count is not None and count < n_averages:
            raise ScopeError(
                f"the digitiser reported done with {count} of {n_averages} "
                "acquisitions folded -- :DIG did not wait for the average on this "
                "firmware, and the trace would be a partial one"
            )
        self._io.write(":MEAS:CLE;")
        return Trace(y=trace.y, dt=trace.dt, t0=trace.t0, count=count)

    def _averages_folded(self) -> int | None:
        """Query `:WAV:COUN?` and return the folded count or `None` for zero or failure.

        Read after fetch while stopped. `acquire` rejects a count below N_avg:
        `:ADER?` indicates an acquisition, not completion of the average."""
        n = self._query_count()
        return n if n else None

    def close(self) -> None:
        try:
            self._io.write(":STOP;")
        finally:
            self._io.close()
