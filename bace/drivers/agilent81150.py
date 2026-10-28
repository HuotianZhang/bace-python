"""The Agilent 81150A supplies the bias step.

The LabVIEW sequence disables the initialization identity query. It configures
external edge arming, pulse shape, levels, frequency, duty, leading-edge
time, delay, width, polarity and output.
Its external trigger threshold is 1.0 V into 10 kohm. The leading-edge time
is 2.5 ns, a lower limit on the applied edge time. Internal `:ARM:FREQ` has
no effect under external arming.

`bace.core.pulses` divides V_pre and V_coll by the bias amplifier gain.
It swaps and negates the levels for inverted device polarity. The driver
receives levels in V at the generator output, not at the device."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .readback import ask, on_off, number

TRIGGER_LEVEL_V = 1.0
TRIGGER_INPUT_IMPEDANCE_OHM = 10_000.0
LEADING_EDGE_TIME_S = 2.5e-9


@dataclass(frozen=True)
class TriggerConfig:
    external: bool = True
    positive_slope: bool = True
    level_v: float = TRIGGER_LEVEL_V
    impedance_ohm: float = TRIGGER_INPUT_IMPEDANCE_OHM


class Agilent81150:
    """Control one 81150A channel without concurrent access."""

    def __init__(self, resource, *, channel: int = 1, timeout_ms: int = 5000):
        self._io = resource
        self.output_polarity = "?"
        """Record the `NORM`, `INV` or `?` polarity set or read by `configure_shape`."""
        self._io.timeout = timeout_ms
        self.ch = int(channel)
        self._output = False

    # -- identity ---------------------------------------------------------
    def identify(self) -> str:
        idn = self._io.query("*IDN?").strip()
        if "81150" not in idn:
            raise RuntimeError(f"expected an 81150A, got {idn!r}")
        return idn

    def reset(self) -> None:
        self._io.write("*RST")
        self._io.write("*CLS;*ESE 1;*SRE 32;")
        self._output = False

    # -- session-level setup, sent once ----------------------------------
    def configure_trigger(self, cfg: TriggerConfig | None = None, *,
                          external: bool | None = None,
                          positive_slope: bool | None = None) -> None:
        """Send immediate or external edge arming settings and return `None`.

        `TriggerConfig` supplies the threshold in V and impedance in ohm. Explicit
        boolean arguments override its fields. The `BiasSource` interface uses
        only booleans and retains the LabVIEW sequence's 1.0 V and 10 kohm defaults."""
        base = cfg if cfg is not None else TriggerConfig()
        ext = base.external if external is None else bool(external)
        pos = base.positive_slope if positive_slope is None else bool(positive_slope)
        if not ext:
            self._io.write(f":ARM:SOUR{self.ch} IMM;")
            return
        self._io.write(f":ARM:SOUR{self.ch} EXT;")
        self._io.write(f":ARM:SENS{self.ch} EDGE;")
        self._io.write(f":ARM:SLOP {'POS' if pos else 'NEG'};")
        self._io.write(f":ARM:LEV {base.level_v:g};")
        self._io.write(f":ARM:IMP {base.impedance_ohm:g};")

    def trigger_state(self) -> dict[str, str]:
        """Query `:ARM:SOUR{ch}?` and `:ARM:SLOP?` and return their string values.

        Read-back records the instrument state, including sources other than
        `EXT` and `IMM`. A failed or empty reply gives `?`."""
        def ask(query: str, known: tuple[str, ...]) -> str:
            try:
                reply = str(self._io.query(query)).strip().upper()
            except Exception:
                return "?"
            if not reply:
                return "?"
            for token in known:
                if reply.startswith(token):
                    return token
            return reply                      # MAN, INT2, ...: say so
        return {"arm_source": ask(f":ARM:SOUR{self.ch}?", ("EXT", "IMM")),
                "arm_slope": ask(":ARM:SLOP?", ("POS", "NEG"))}

    def configure_shape(self, frequency_hz: float, *, duty_percent: float = 50.0,
                        edge_time_s: float = LEADING_EDGE_TIME_S,
                        inverted_output: bool = False) -> None:
        self._io.write(f":FUNC{self.ch} PULS;")
        self._io.write(f":FREQ{self.ch} {frequency_hz:g};")
        self._io.write(f":FUNC{self.ch}:PULS:DCYC {duty_percent:g};")
        self._io.write(f":FUNC{self.ch}:PULS:TRAN {edge_time_s:g};")
        # `None` preserves polarity when device wiring is uncertain. Wrong polarity
        # moves extraction to the other pulse edge, 5 us away and outside the record,
        # without an obvious data error. Read-back preserves the convention in the
        # run record even when the caller leaves the setting unchanged.
        if inverted_output is None:
            self.output_polarity = str(
                self._io.query(f":OUTP{self.ch}:POL?")).strip().upper()
        else:
            pol = "INV" if inverted_output else "NORM"
            self._io.write(f":OUTP{self.ch}:POL {pol};")
            self.output_polarity = pol

    # -- per axis point ---------------------------------------------------
    def set_levels(self, high_v: float, low_v: float, *,
                   delay_s: float, width_s: float) -> None:
        """Send high and low levels in V, delay in s and width in s; return `None`.

        Levels precede width so the width is not briefly applied at the previous
        amplitude."""
        self._io.write(f":VOLT{self.ch}:HIGH {high_v:g};")
        self._io.write(f":VOLT{self.ch}:LOW {low_v:g};")
        self._io.write(f":PULS:DEL{self.ch} {delay_s:e};")
        self._io.write(f":FUNC{self.ch}:PULS:WIDT {width_s:g};")

    def apply(self, levels, *, inverted_output: bool = False) -> None:
        """Send the light trace settings from `bace.core.pulses.PulseLevels`.

        Levels are in V and delay and width in s. Return `None`."""
        self.set_levels(levels.high_light, levels.low_light,
                        delay_s=levels.delay_s, width_s=levels.width_s)

    def apply_dark(self, levels) -> None:
        """Send the dark trace levels in V and delay and width in s; return `None`.

        The supplied dark reference determines the voltage step."""
        self.set_levels(levels.high_dark, levels.low_dark,
                        delay_s=levels.delay_s, width_s=levels.width_s)

    # -- output -----------------------------------------------------------
    def enable_output(self, on: bool = True) -> None:
        self._io.write(f":OUTP{self.ch} {'ON' if on else 'OFF'};")
        self._output = bool(on)

    def disable_output(self) -> None:
        self.enable_output(False)

    @property
    def output_enabled(self) -> bool:
        """Return the cached output state used by `bace.drivers.routing.BiasRouter`.

        The service refreshes it with `read_output` before relay moves and
        read-back. A generator enabled outside this object is invisible to its
        initial `False` flag."""
        return self._output

    # -- read-back, for the service's bench card --------------------------
    # The service calls these methods through `getattr` with a cached fallback.
    # Each queries the instrument and returns `None` or `?` for a missing reply.
    def read_output(self) -> bool | None:
        """Query output, update `output_enabled` and return a boolean or `None`.

        The command is `:OUTP{ch}?`."""
        on = on_off(ask(self._io, f":OUTP{self.ch}?"))
        if on is not None:
            self._output = on
        return on

    def read_polarity(self) -> str:
        """Query `:OUTP{ch}:POL?` and return the polarity or `?`.

        Update `output_polarity` so read-back can check the chain before a run
        configures its shape."""
        pol = ask(self._io, f":OUTP{self.ch}:POL?")
        if pol is None:
            return "?"
        pol = pol.upper().rstrip(";")
        self.output_polarity = pol
        return pol

    def read_state(self) -> dict[str, Any]:
        """Query and return output, polarity, levels, frequency, delay and width.

        Levels are in V, frequency in Hz and times in s. Missing replies give
        `None`, or `?` for polarity."""
        ch = self.ch
        return {
            "output": self.read_output(),
            "polarity": self.read_polarity(),
            "high_v": number(ask(self._io, f":VOLT{ch}:HIGH?")),
            "low_v": number(ask(self._io, f":VOLT{ch}:LOW?")),
            "frequency_hz": number(ask(self._io, f":FREQ{ch}?")),
            "delay_s": number(ask(self._io, f":PULS:DEL{ch}?")),
            "width_s": number(ask(self._io, f":FUNC{ch}:PULS:WIDT?")),
        }

    def errors(self) -> list[str]:
        out = []
        while True:
            r = self._io.query(":SYST:ERR?").strip()
            if r.startswith("0,") or r.startswith("+0,"):
                return out
            out.append(r)
            if len(out) > 20:
                return out

    def close(self) -> None:
        try:
            self.disable_output()
        finally:
            self._io.close()
