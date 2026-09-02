"""Agilent 33220A — the LED drive.

At `GPIB0::15::INSTR`. This is the generator `BACE_Mehrdad.vi` talks to inline,
with no channel index — which is how it was told apart from the 81150A, whose
every command in its driver library carries one (`:VOLT%d:HIGH`, `:FUNC%d`).
The recovered strings, verbatim:

    FUNC:SHAP DC;        :VOLT:OFFS <level>
    FUNC:SHAP PULSE;     :FREQ <f>;:VOLT:HIGH <hi>;:VOLT:LOW <lo>
                         FUNC:PULS:DCYC <duty>
    :OUTP <0|1>

Two modes are used within one intensity point and **the level must be the same
number in both** — DC for the V_oc measurement, then the identical value as the
pulse high level for the transient. `core.illumination` owns that invariant and
refuses a mismatch; this driver deliberately does not decide anything about
levels, it just sends them.

Note what is *not* here: the LED sees a fixed-gain amplifier after this
generator, and the low level is chosen below the LED's turn-on threshold so the
diode is fully off rather than dim. Both are physics, and both live in
`core.illumination`.
"""
from __future__ import annotations


class LedSourceError(RuntimeError):
    pass


class Agilent33220A:
    """One 33220A session. Not thread-safe."""

    def __init__(self, resource, *, timeout_ms: int = 5000):
        self._io = resource
        self._io.timeout = timeout_ms
        self._output = False
        self._mode = "OFF"

    # -- identity ---------------------------------------------------------
    def identify(self) -> str:
        idn = self._io.query("*IDN?").strip()
        if "33220" not in idn:
            raise LedSourceError(
                f"expected a 33220A on this address, got {idn!r} — if this is the "
                "81150A the two generators are swapped, and the LED would be "
                "driven with the collection-field waveform"
            )
        return idn

    def reset(self) -> None:
        self._io.write("*RST")
        self._io.write("*CLS;*ESE 1;*SRE 32;")
        self._output = False
        self._mode = "OFF"

    # -- the two modes ----------------------------------------------------
    def set_dc(self, level_v: float) -> None:
        """Steady illumination, for the V_oc / J_sc / J_sat measurement."""
        self._io.write("FUNC:SHAP DC;")
        self._io.write(f":VOLT:OFFS {level_v:g};")
        self._mode = "DC"
        self._level = float(level_v)

    def set_pulse(self, high_v: float, low_v: float, *, frequency_hz: float = 1000.0,
                  duty_percent: float = 50.0) -> None:
        """On/off square wave for the transient.

        `high_v` must equal the level used for the DC V_oc measurement at this
        intensity point; `low_v` must sit below the LED's turn-on threshold.
        Neither is checked here — `core.illumination.LedDrive` does that, at the
        point where both numbers are known together.
        """
        self._io.write("FUNC:SHAP PULSE;")
        # Hold the DUTY CYCLE, not the width, when the period changes. Without
        # this the generator answers a frequency change with
        #   -221,"Settings conflict; pulse width decreased due to period"
        # because the width it was holding no longer fits — seen on the rig at
        # 2026-08-31, going from 500 Hz to 1 kHz. Harmless here, since the duty
        # cycle is set immediately afterwards and redefines the width, but a
        # settings conflict that is always present is a settings conflict nobody
        # reads, and the next real one would be lost in it.
        self._io.write("FUNC:PULS:HOLD DCYC;")
        self._io.write(f":FREQ {frequency_hz:g};")
        self._io.write(f":VOLT:HIGH {high_v:g};")
        self._io.write(f":VOLT:LOW {low_v:g};")
        self._io.write(f"FUNC:PULS:DCYC {duty_percent:g};")
        self._mode = "PULSE"
        self._level = float(high_v)

    def apply(self, drive) -> None:
        """Take a `core.illumination.LedDrive` and set the pulse it describes."""
        s = drive.pulse_settings()
        self.set_pulse(s["high"], s["low"], frequency_hz=s["frequency"],
                       duty_percent=s["duty"])

    def apply_dc(self, drive) -> None:
        self.set_dc(drive.dc_settings()["offset"])

    # -- output polarity --------------------------------------------------
    def set_polarity(self, inverted: bool) -> None:
        """`:OUTP:POL INV` or `NORM`. The setting the whole experiment hangs on.

        Inverting the waveform flips which half of the cycle the LED is lit for,
        **and leaves the Sync alone** -- 33220A User's Guide p.67: "When a
        waveform is inverted, the Sync signal associated with the waveform is
        not inverted." The 81150A arms on the Sync's rising edge, so INV is what
        makes that edge mean *light off*. With NORM it means light on, and the
        extraction happens in the middle of carrier generation while still
        producing a transient that integrates to a plausible charge.

        The offset is not inverted either, so the DC mode used for the V_oc
        measurement is unaffected -- one setting buys both halves.

        Nothing in this package set this until 2026-09-01: the run inherited
        whatever the last session left on the front panel, and no output
        recorded which it had been.
        """
        self._io.write(f":OUTP:POL {'INV' if inverted else 'NORM'};")
        self._polarity = "INV" if inverted else "NORM"

    def polarity(self) -> str:
        """`INV` or `NORM`, from the instrument."""
        return str(self._io.query(":OUTP:POL?")).strip().upper()

    def off(self) -> None:
        """Dark: output disabled, not merely a low level."""
        self.enable_output(False)
        self._mode = "OFF"

    # -- output -----------------------------------------------------------
    def enable_output(self, on: bool = True) -> None:
        self._io.write(f":OUTP {1 if on else 0:d};")
        self._output = bool(on)

    def disable_output(self) -> None:
        self.enable_output(False)

    @property
    def output_enabled(self) -> bool:
        return self._output

    @property
    def mode(self) -> str:
        """OFF, DC or PULSE — read by the illumination guard when it checks that
        V_oc was measured under the same drive the transient will use."""
        return self._mode

    def errors(self) -> list[str]:
        out: list[str] = []
        while True:
            r = self._io.query(":SYST:ERR?").strip()
            if r.startswith(("0,", "+0,")):
                return out
            out.append(r)
            if len(out) > 20:
                return out

    def close(self) -> None:
        try:
            self.disable_output()
        finally:
            self._io.close()
