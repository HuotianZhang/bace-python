"""The Agilent 33220A drives the LED.

The LabVIEW command sequence uses no channel index: `FUNC:SHAP DC` and
`:VOLT:OFFS` for DC; `FUNC:SHAP PULSE`, `:FREQ`, `:VOLT:HIGH`, `:VOLT:LOW`
and `FUNC:PULS:DCYC` for pulses; `:OUTP` for output control.

`read_state` queries the output, shape, levels, offset, frequency, polarity
and Sync output. Sync arms the 81150A. Disabling it on the front panel leaves
a scan measuring noise without another indication of the missing stimulus.

The DC level for V_oc and the pulse high level at one point must match.
`bace.core.illumination.LedDrive` enforces this constraint; the driver sends
the supplied levels. The LED has an amplifier of constant gain after this
generator. The pulse low level must be below the LED turn-on threshold so
the LED switches off fully (manual 3.2, `docs/manual/3.2-correspondence.md`)."""
from __future__ import annotations

from typing import Any

from .readback import ask, number, on_off, word

SHAPES: dict[str, str] = {"PULS": "PULSE", "DC": "DC"}
"""Map shape read-back to the two supported modes.

Other shapes, including `SIN`, `SQU` and `RAMP`, retain the instrument's
spelling so read-back exposes settings this driver does not select."""


class LedSourceError(RuntimeError):
    pass


class Agilent33220A:
    """Control one 33220A session without concurrent access."""

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
        """Send `FUNC:SHAP DC` and `:VOLT:OFFS` with `level_v` in V.

        The output provides steady light for DC measurements. Return `None`."""
        self._io.write("FUNC:SHAP DC;")
        self._io.write(f":VOLT:OFFS {level_v:g};")
        self._mode = "DC"
        self._level = float(level_v)

    def set_pulse(self, high_v: float, low_v: float, *, frequency_hz: float = 1000.0,
                  duty_percent: float = 50.0) -> None:
        """Send the pulse shape, frequency in Hz, levels in V and duty in percent.

        `high_v` must match the DC level for V_oc at this point. `low_v` must be
        below the LED turn-on threshold. `bace.core.illumination.LedDrive` checks
        these constraints. Return `None`."""
        self._io.write("FUNC:SHAP PULSE;")
        # Holding duty cycle avoids error -221, "Settings conflict; pulse width
        # decreased due to period", when increasing frequency from 500 Hz to 1 kHz
        # on this bench. A held width no longer fits the shorter period. Setting
        # duty afterwards resolves the width, but repeated harmless queue entries
        # can hide a real settings conflict.
        self._io.write("FUNC:PULS:HOLD DCYC;")
        self._io.write(f":FREQ {frequency_hz:g};")
        self._io.write(f":VOLT:HIGH {high_v:g};")
        self._io.write(f":VOLT:LOW {low_v:g};")
        self._io.write(f"FUNC:PULS:DCYC {duty_percent:g};")
        self._mode = "PULSE"
        self._level = float(high_v)

    def apply(self, drive) -> None:
        """Send pulse settings from `bace.core.illumination.LedDrive`; return `None`.

        Levels are in V, frequency in Hz and duty in percent."""
        s = drive.pulse_settings()
        self.set_pulse(s["high"], s["low"], frequency_hz=s["frequency"],
                       duty_percent=s["duty"])

    def apply_dc(self, drive) -> None:
        self.set_dc(drive.dc_settings()["offset"])

    # -- output polarity --------------------------------------------------
    def set_polarity(self, inverted: bool) -> None:
        """Send `:OUTP:POL INV` or `NORM` and return `None`.

        Inversion changes the LED phase but leaves Sync unchanged. With `INV`,
        the Sync rising edge arms the 81150A at light off (manual 3.2,
        `docs/manual/3.2-correspondence.md`). With `NORM`, extraction occurs during
        carrier generation and can still give a plausible Q.

        The DC offset is also unchanged by inversion on this bench, so the V_oc
        measurement keeps its drive level. Explicit configuration avoids relying
        on the front-panel state."""
        self._io.write(f":OUTP:POL {'INV' if inverted else 'NORM'};")
        self._polarity = "INV" if inverted else "NORM"

    def polarity(self) -> str:
        """Query `:OUTP:POL?` and return `INV`, `NORM` or `?`.

        A failed query returns `?` under the `LedSource` contract. Raising during
        cleanup can mask the pending exception; a default polarity can put false
        information in the file."""
        try:
            reply = str(self._io.query(":OUTP:POL?")).strip().upper()
        except Exception:
            return "?"
        return reply or "?"

    def off(self) -> None:
        """Disable LED output with `:OUTP 0` and return `None`."""
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
        """Return the cached `OFF`, `DC` or `PULSE` mode.

        The illumination guard uses it to check that V_oc and the light trace
        use the same drive."""
        return self._mode

    # -- read-back, for the service's bench card --------------------------
    # The service calls these methods through `getattr`. Read-back compares
    # instrument settings with the requested settings because the generator
    # retains front-panel state across sessions.
    def read_output(self) -> bool | None:
        """Query `:OUTP?`, return a boolean or `None`, and update `output_enabled`."""
        on = on_off(ask(self._io, ":OUTP?"))
        if on is not None:
            self._output = on
            if not on:
                self._mode = "OFF"
        return on

    def read_state(self) -> dict[str, Any]:
        """Query and return the LED generator state.

        Levels and offset are in V; frequency is in Hz. Missing replies give
        `None` or `?`. `mode` is `OFF` if output is disabled; otherwise it is
        `DC`, `PULSE` or the reported shape. `OUTP:SYNC?` checks the connector
        that arms the 81150A: disabling Sync leaves the scan measuring noise."""
        on = self.read_output()
        shape = word(ask(self._io, "FUNC:SHAP?"), tuple(SHAPES))
        mode = "?" if shape == "?" else SHAPES.get(shape, shape)
        if on is False:
            mode = "OFF"
        if mode in ("OFF", "DC", "PULSE"):
            self._mode = mode
        return {
            "output": on,
            "polarity": self.polarity(),
            "mode": mode,
            "shape": shape,
            "high_v": number(ask(self._io, ":VOLT:HIGH?")),
            "low_v": number(ask(self._io, ":VOLT:LOW?")),
            "offset_v": number(ask(self._io, ":VOLT:OFFS?")),
            "frequency_hz": number(ask(self._io, ":FREQ?")),
            "sync_output": on_off(ask(self._io, "OUTP:SYNC?")),
        }

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
