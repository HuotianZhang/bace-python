"""The relay that decides which instrument is connected to the device.

The device terminals are shared. A relay connects them either to the ×4
amplifier fed by the 81150A (transient path) or to the Keithley 2400 (DC path).
Only one at a time, and switching while either source is driving is the one
software mistake on this rig that can damage hardware rather than a dataset.

So this module does not expose a bare "set the relay" call. `BiasRouter` owns
the relay line and will only move it when both sources report their outputs
disabled; the sources are registered with it, not commanded around it.

    router = BiasRouter(dio, keithley=k, pulse_path=fgen)
    with router.dc():          # Keithley connected, 81150A output off
        voc = k.measure_voc()
    with router.transient():   # amplifier connected, Keithley output off
        trace = scope.acquire(...)

RESOLVED from the binaries. Two independent lines of evidence:

1. `shutter_lv2012.vi` hard-codes its own target: module nr **0**, module ID 9,
   channel 0, values 1/0, plus a 0.1 s settling wait. Its only input is the
   boolean `open/close shutter`. And `TDCF-BACE.vi` -- the standalone
   measurement, which has a shutter and no relay -- calls that VI and nothing
   else on the DIO. So module 0 is the shutter.

2. In `BACE_Mehrdad.vi`, tracing every `Dapi DO Set1` by event frame and depth:

       J-V frame    entry   module 0 <- 1,  module 1 <- 1
                            ... Keithley sweep ...
                            (module 1 is never driven low: the SourceMeter
                             stays connected for the whole J-V measurement)

       BACE frame   entry   module 0 <- 1,  module 1 <- 1
                            Enable Output -> measJsc,Voc -> Enable Output off
                            module 1 <- 0            <-- immediately after the
                                                          Keithley block
                            ... engine runs the transients ...

   Module 1 goes low exactly once, in the BACE frame, between the SourceMeter
   measurement and the transient. That is the relay.

So: module 0 = shutter, module 1 = relay, and the polarity is

       1 = Keithley 2400        0 = amplifier

Note the un-driven state (0) leaves the device on the amplifier, so the rig
powers up on the transient path with the 81150A output off. That is safe but
worth knowing.
"""
from __future__ import annotations

from contextlib import contextmanager

MODULE_ID = 9
MODULE_NR = 1        # confirmed: the shutter is module 0, the relay is module 1
CHANNEL = 0

TO_AMPLIFIER = 0     # confirmed from the BACE frame write order
TO_SOURCEMETER = 1


class RoutingError(RuntimeError):
    pass


class BiasRouter:
    """Exclusive access to the device terminals.

    `dio` is any object with `set(channel, value)` -- the Deditec backend, the
    bridged one, or the simulated one from `bace.drivers.shutter`.
    """

    def __init__(self, dio, *, sourcemeter=None, pulse_path=None,
                 module_nr: int = MODULE_NR, channel: int = CHANNEL,
                 settle_s: float = 0.1):
        if module_nr == 0:
            raise RoutingError(
                "module 0 is the shutter (measured on the rig 2026-09-01: "
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
        """Device connected to the SourceMeter.

        On exit the SourceMeter output is disabled but the relay is left where
        it is. Safety comes from the outputs being off, not from the relay
        position, and a mechanical relay has a finite operation count -- a run
        of 300 points would otherwise cost 600 needless throws plus their
        settling time.
        """
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
        """Device connected to the amplifier driven by the 81150A."""
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
        """Leave the rig in the safe state: both outputs off, relay untouched."""
        for dev in (self._sourcemeter, self._pulse_path):
            if dev is not None:
                try:
                    dev.disable_output()
                except Exception:
                    pass
