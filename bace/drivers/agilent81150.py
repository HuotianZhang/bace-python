"""Agilent 81150A pulse function generator — the collection-field stimulus.

The call order below is `Agilent 81150StandardWaveformTDCF20131120.vi`, the
custom VI the engine actually uses, not the generic driver's example. Its
sequence and its embedded constants:

    Initialize                       ID Query = FALSE
    if Ext Trig?                     Configure Trigger        source EXT, sense EDGE
                                     Configure External Trigger  level 1.0 V, Zin 10 kohm
    Configure Internal Trigger       :ARM:FREQ  (moot under external arming)
    Configure Standard Waveform      shape PULSE, VOLT:HIGH = prebias, VOLT:LOW = Vcoll, FREQ
    Configure Pulse Waveform         PULS:DCYC, PULS:TRAN = 2.5 ns, PULS:DEL = delay
    Configure Pulse Width            FUNC1:PULS:WIDT
    Configure Output Polarity        NORM / INV
    Enable Output                    ON

Two constants are easy to miss and both matter: the external trigger threshold
is **1.0 V into 10 kohm**, and the pulse leading-edge time is **2.5 ns** -- the
field's actual rise, and a floor on how sharply the collection bias can be
applied.

`prebias` and `vcoll` are the levels at the generator output: already divided
by the amplifier gain by `core.pulses`, and already swapped-and-negated if the
sample architecture is inverted.
"""
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
    """One channel of an 81150A. Not thread-safe."""

    def __init__(self, resource, *, channel: int = 1, timeout_ms: int = 5000):
        self._io = resource
        self.output_polarity = "?"
        """`NORM`, `INV` or `?` -- what `configure_shape` set or found."""
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
        """Arm the generator. Two spellings, one behaviour.

        The bench harness and `tools/scan.py` pass a `TriggerConfig`, which
        also carries the recovered threshold and impedance; the run path
        speaks the `BiasSource` protocol and passes only the two booleans, so
        the 1.0 V / 10 kohm constants never leave this file. Booleans given
        beside a `TriggerConfig` override its fields.
        """
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
        """`BiasSource.trigger_state`: `:ARM:SOUR1?` and `:ARM:SLOP?` read back.

        A readback, not an echo of what `configure_trigger` sent -- the point
        is to record what the instrument holds, which is also what
        `tools/scan.py` checked by hand before this existed.
        """
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
        # `None` = leave the instrument's polarity alone. Which level the device
        # rests at between pulses depends on how the sample is wired, and that is
        # not always known -- writing a polarity then is guessing, and a wrong
        # guess moves the extraction to the other edge of the pulse, 5 us away
        # and outside the record, without any symptom in the data. So a caller
        # may decline to set it; the value found is read back and kept so the run
        # still records which convention it ran under.
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
        """The four numbers that change from one measurement point to the next.

        Order matters on this instrument: set the levels before the width, so
        the width is never briefly applied against the previous amplitude.
        """
        self._io.write(f":VOLT{self.ch}:HIGH {high_v:g};")
        self._io.write(f":VOLT{self.ch}:LOW {low_v:g};")
        self._io.write(f":PULS:DEL{self.ch} {delay_s:e};")
        self._io.write(f":FUNC{self.ch}:PULS:WIDT {width_s:g};")

    def apply(self, levels, *, inverted_output: bool = False) -> None:
        """Take a `core.pulses.PulseLevels` and apply its *light* pair."""
        self.set_levels(levels.high_light, levels.low_light,
                        delay_s=levels.delay_s, width_s=levels.width_s)

    def apply_dark(self, levels) -> None:
        """The reference pair: same swing, referenced to 0 V."""
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
        """Read by `drivers.routing.BiasRouter` before it moves the relay.

        The driver's memory of what it last sent -- or, after `read_output`,
        of what the instrument last answered. The service refreshes it with
        `read_output()` before every relay move and every read-back, because
        a generator left ON by the LabVIEW VI is invisible to a flag that
        starts False."""
        return self._output

    # -- read-back, for the service's bench card --------------------------
    # Not protocol members: the run path never asks these questions, the
    # service does, through `getattr` with a cached fallback. Each one asks
    # the instrument and answers None/"?" when it will not say. The spellings
    # are the ones tools/scan.py checked by hand until 2026-09-02.
    def read_output(self) -> bool | None:
        """`:OUTP1?`, and the cached flag `output_enabled` follows it."""
        on = on_off(ask(self._io, f":OUTP{self.ch}?"))
        if on is not None:
            self._output = on
        return on

    def read_polarity(self) -> str:
        """`:OUTP1:POL?` -> `NORM`, `INV` or `?`; `output_polarity` follows it,
        so the chain can be judged before any run has configured the shape."""
        pol = ask(self._io, f":OUTP{self.ch}:POL?")
        if pol is None:
            return "?"
        pol = pol.upper().rstrip(";")
        self.output_polarity = pol
        return pol

    def read_state(self) -> dict[str, Any]:
        """Everything the bench card shows for this generator, read from the
        instrument: `output`, `polarity`, `high_v`, `low_v`, `frequency_hz`,
        `delay_s`, `width_s`. A value the instrument would not give is None
        (`?` for the polarity)."""
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
