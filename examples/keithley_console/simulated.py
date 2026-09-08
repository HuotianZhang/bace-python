"""A SourceMeter with a solar cell in front of it, and no hardware anywhere.

**A deliberate second copy**, like `smu.py`: `bace/drivers/simulated.py` models
a whole bench — a relay, a shutter, an LED driver, a scope, a cryostat — and a
console that drives one instrument needs none of it. What is here is the cell
and the source, in pure standard library. No numpy, so the folder runs under
whatever Python is on the machine.

**Every number this produces is a model.** It exists so the page can be opened,
clicked and read on a laptop, and so this folder's tests can drive a real HTTP
server against something that answers. A V_oc from here must never be written
down as a measurement, which is why `--sim` says so on the page and why the
console's `mode` is on every answer it gives.

The differences from the package's simulator, all of them because there is no
bench here to model:

* no relay, so the cell is simply connected — on the real bench the SourceMeter
  sits on the far side of a relay and reads an open circuit when it is thrown
  the other way;
* no shutter and no LED driver, so illumination is one flag (`--sim-dark`)
  rather than a drive voltage through an LED curve;
* no `read_output`, deliberately. A simulated instrument has no front panel for
  anybody to reach over and touch, so what this process last wrote *is* the
  truth, and the console's fallback to its cached flag is correct here and only
  here.
"""
from __future__ import annotations

import math
import random

from smu import (PANEL_STATIC, PanelReading, PanelSetup, SourceMeterError,
                 panel_budget_for)

KT_Q_290K = 8.617333e-5 * 290.0
"""kT/q at 290 K, in volts. The bench runs near room temperature and nothing
here sweeps it."""


class SimulatedCell:
    """Single-diode I(V): a diode, a shunt, and a photocurrent offset.

    No series resistance, which is what keeps I(V) explicit — adding it makes
    the equation implicit and this exists to exercise a panel, not to stand in
    for a device. A curve from here should never be fitted.
    """

    voc_ref = 0.906          # V, illuminated
    jsc_ref = 2.0e-4         # A, illuminated
    diode_n = 1.5            # ideality
    shunt_ohm = 2.0e5        # Ω

    @property
    def i0(self) -> float:
        """Saturation current, derived so the diode equation reproduces
        `voc_ref` rather than being a second number that can disagree with
        it. V_oc = n kT ln(I_ph/I_0), so I_0 = I_ph exp(-V_oc / n kT)."""
        return self.jsc_ref * math.exp(-self.voc_ref / (self.diode_n * KT_Q_290K))

    def current(self, v: float, lit: bool) -> float:
        """Amps at `v`. Positive is injection, so an illuminated cell at 0 V
        reads **negative** — the sign the instrument actually returns, which
        this console shows without flipping."""
        i_ph = self.jsc_ref if lit else 0.0
        arg = max(-50.0, min(50.0, v / (self.diode_n * KT_Q_290K)))
        return self.i0 * (math.exp(arg) - 1.0) + v / self.shunt_ohm - i_ph


class SimulatedSourceMeter:
    """The panel half of `smu.Keithley2400`, answered from the model above.

    The same refusals as the real one, in the same order and with the same
    sentences: a panel applied under an output somebody else left on, and a
    `PANEL_STATIC` change under a live output. A console tested against a
    simulator that were more permissive than the instrument would be a console
    whose interlocks are only tested where they do not matter.
    """

    def __init__(self, *, lit: bool = True, seed: int = 1, noise_a: float = 5e-7):
        self.cell = SimulatedCell()
        self.lit = bool(lit)
        self.noise_a = float(noise_a)
        self._rng = random.Random(seed)
        self._output = False
        self._panel: PanelSetup | None = None

    # -- identity and output ------------------------------------------------
    def identify(self) -> str:
        return "SIMULATED,MODEL 2400,0,none"

    def enable_output(self, on: bool = True) -> None:
        self._output = bool(on)

    def disable_output(self) -> None:
        self.enable_output(False)

    @property
    def output_enabled(self) -> bool:
        return self._output

    # No `read_output`: see the module docstring. The console distinguishes
    # "this driver cannot be asked" from "it was asked and did not answer",
    # and only the first may stand on the cached flag.

    # -- the panel ----------------------------------------------------------
    @property
    def panel(self) -> PanelSetup | None:
        return self._panel

    def apply_panel(self, setup: PanelSetup) -> PanelSetup:
        was = self._panel
        if was is None and self._output:
            raise SourceMeterError(
                "the output is ON and this instrument is not on the panel -- "
                "something else is driving it. Switch the output off first; "
                "applying a panel resets the instrument, which would drop it "
                "with nothing said")
        if was is not None and self._output:
            changed = [f for f in PANEL_STATIC if getattr(setup, f) != getattr(was, f)]
            if changed:
                raise SourceMeterError(
                    f"the output is ON: {', '.join(changed)} cannot be changed under "
                    "it -- switch the output off, change it, switch it back on")
        self._panel = setup
        return setup

    def panel_budget_s(self) -> float:
        """The real driver's number, not a shorter one. Nothing here
        integrates, but what a caller plans around must not depend on which
        bench it is talking to — the console's waits are tested here and run
        against the instrument."""
        panel = self._panel
        return panel_budget_for(panel.nplc if panel is not None else 1.0,
                                panel.averaging if panel is not None else 1)

    def read_panel(self) -> PanelReading:
        panel = self._panel
        if panel is None:
            raise SourceMeterError("the panel is not applied to this instrument")
        if not self._output:
            raise SourceMeterError("the output is OFF: there is nothing to read")

        noise = self._rng.gauss(0.0, self.noise_a)
        if panel.function == "voltage":
            volts = float(panel.level)
            amps = self.cell.current(volts, self.lit) + noise
            compliance = False
            if abs(amps) > panel.current_compliance_a:
                # The source cannot hold its level at that current, so it holds
                # the current instead and the voltage falls where the device
                # puts it. That is what the `Cmpl` annunciator means.
                amps = math.copysign(panel.current_compliance_a, amps)
                volts, _ = self._solve_v(amps, panel.voltage_compliance_v)
                compliance = True
        else:
            target = float(panel.level)
            volts, compliance = self._solve_v(target, panel.voltage_compliance_v)
            amps = (self.cell.current(volts, self.lit) + noise if compliance
                    else target + noise)
        return PanelReading(volts=float(volts), amps=float(amps),
                            compliance=compliance, function=panel.function,
                            level=float(panel.level))

    def _solve_v(self, target_a: float, bound_v: float) -> tuple[float, bool]:
        """`(volts, at the limit)` — the voltage at which the cell draws
        `target_a`, bisected inside ±`bound_v`.

        `SimulatedCell.current` is strictly increasing in V, so a bisection is
        exact to the float and needs no derivative. Landing on the bound means
        the cell will not take that current inside the compliance, which *is*
        the compliance.
        """
        bound = abs(float(bound_v))
        lo, hi = -bound, bound
        if self.cell.current(lo, self.lit) >= target_a:
            return lo, True
        if self.cell.current(hi, self.lit) <= target_a:
            return hi, True
        for _ in range(80):
            mid = 0.5 * (lo + hi)
            if self.cell.current(mid, self.lit) < target_a:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi), False

    # -- the rest of what the console asks for ------------------------------
    def errors(self) -> list[str]:
        """`:SYST:ERR?` on the real one. A model has no error queue, and an
        empty list is the honest answer rather than an invented entry."""
        return []

    def close(self) -> None:
        self.disable_output()
