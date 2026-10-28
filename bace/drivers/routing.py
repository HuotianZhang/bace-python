"""Route the shared device terminals to one source at a time.

The relay selects the Keithley 2400 or the bias amplifier driven by the
81150A. Moving it while either source drives can damage hardware.
`BiasRouter` owns the line and permits movement only when both outputs
report disabled.

DIO module ID 9, module 0, channel 0 is the shutter: 1 opens and 0 shuts.
Module 1 is the relay: 1 selects the Keithley and 0 the amplifier
(`docs/notes.md`). The LabVIEW sequence holds module 1 high throughout J-V;
for transient measurement it drives module 1 low after DC measurements
and source-meter output disable. The shutter sequence waits 0.1 s.

The undriven relay state is the amplifier path. It is safe at power-up
only with the 81150A output disabled."""
from __future__ import annotations

from contextlib import contextmanager

MODULE_ID = 9
MODULE_NR = 1        # Module 0 is the shutter; module 1 is the relay.
CHANNEL = 0

TO_AMPLIFIER = 0     # High selects the source meter; low selects the amplifier.
TO_SOURCEMETER = 1


class RoutingError(RuntimeError):
    pass


class BiasRouter:
    """Provide exclusive access to the device terminals.

    `dio` supplies `set(channel, value)` through the direct, bridged or
    simulated DIO backend."""

    def __init__(self, dio, *, sourcemeter=None, pulse_path=None,
                 module_nr: int = MODULE_NR, channel: int = CHANNEL,
                 settle_s: float = 0.1):
        if module_nr == 0:
            raise RoutingError(
                "module 0 is the shutter (measured on this bench: "
                "setting it high is what makes photocurrent appear, while the "
                "device stays connected either way), not the relay. "
                "The relay is module 1 "
                "(ID 9, channel 0). Driving the shutter here would leave the "
                "device on whichever source the relay happens to be set to."
            )
        self._dio = dio
        self._sourcemeter = sourcemeter
        self._pulse_path = pulse_path
        self.module_nr = module_nr
        self.channel = channel
        self.settle_s = settle_s
        self._position: int | None = None

    # -- interlock --------------------------------------------------------
    def _assert_quiet(self) -> None:
        live = []
        for name, dev in (("SourceMeter", self._sourcemeter),
                          ("pulse generator", self._pulse_path)):
            if dev is None:
                continue
            enabled = getattr(dev, "output_enabled", None)
            if enabled is None:
                raise RoutingError(
                    f"{name} does not report its output state; the router cannot "
                    "prove it is safe to switch. Give the driver an "
                    "`output_enabled` property."
                )
            if enabled:
                live.append(name)
        if live:
            raise RoutingError(
                f"refusing to move the relay while {' and '.join(live)} "
                f"{'is' if len(live) == 1 else 'are'} still driving. Disable the "
                "output first."
            )

    def _move(self, position: int) -> None:
        if self._position == position:
            return
        self._assert_quiet()
        self._dio.set(self.channel, position)
        self._position = position
        if self.settle_s:
            import time
            time.sleep(self.settle_s)

    # -- the two positions -------------------------------------------------
    @contextmanager
    def dc(self):
        """Connect the source meter and disable its output on context exit.

        Leave the relay in position on exit. Output disable provides safety;
        unnecessary relay movement consumes its finite operation count. A run
        of 300 points otherwise adds 600 movements and their settle time."""
        self._move(TO_SOURCEMETER)
        try:
            yield
        finally:
            if self._sourcemeter is not None:
                try:
                    self._sourcemeter.disable_output()
                except Exception:
                    pass

    @contextmanager
    def transient(self):
        """Connect the bias amplifier after checking both source outputs are off."""
        self._move(TO_AMPLIFIER)
        try:
            yield
        finally:
            if self._pulse_path is not None:
                try:
                    self._pulse_path.disable_output()
                except Exception:
                    pass

    @property
    def position(self) -> str:
        return {None: "unknown", TO_AMPLIFIER: "amplifier",
                TO_SOURCEMETER: "sourcemeter"}[self._position]

    def park(self) -> None:
        """Disable both source outputs and leave the relay in position."""
        for dev in (self._sourcemeter, self._pulse_path):
            if dev is not None:
                try:
                    dev.disable_output()
                except Exception:
                    pass
