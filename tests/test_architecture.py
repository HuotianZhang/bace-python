"""The dependency rule, asserted.

`core/` is the validated numerics. It stays validated only as long as nothing
in it reaches out to an instrument, a file format or a web service. This test
is the enforcement — three lines of intent, and it fails the moment someone
imports pyvisa into the physics.
"""
from __future__ import annotations

import ast
import pathlib

PKG = pathlib.Path(__file__).resolve().parents[1] / "bace"
FORBIDDEN_FOR_CORE = {"drivers", "experiment", "storage", "service", "ui"}


def _imported_subpackages(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:                       # relative: ..drivers.foo
                mod = node.module or ""
                head = mod.split(".")[0]
                if node.level >= 2 and head:
                    out.add(head)
                elif node.level >= 2:
                    out.update(a.name.split(".")[0] for a in node.names)
            elif node.module and node.module.startswith("bace."):
                out.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("bace."):
                    out.add(a.name.split(".")[1])
    return out


def test_core_depends_on_nothing_inward() -> None:
    offenders = {}
    for f in sorted((PKG / "core").glob("*.py")):
        bad = _imported_subpackages(f) & FORBIDDEN_FOR_CORE
        if bad:
            offenders[f.name] = sorted(bad)
    assert not offenders, (
        f"core/ must not import from the outer layers, but found: {offenders}. "
        "The numerics are validated against a real archive; keeping them free of "
        "instruments and file formats is what keeps that validation meaningful."
    )


def test_drivers_do_not_depend_on_experiment() -> None:
    offenders = {}
    for f in sorted((PKG / "drivers").rglob("*.py")):
        bad = _imported_subpackages(f) & {"experiment", "storage", "service", "ui"}
        if bad:
            offenders[f.name] = sorted(bad)
    assert not offenders, f"drivers/ reached outward: {offenders}"


def test_every_real_driver_satisfies_its_protocol() -> None:
    from bace.drivers import protocols as P
    from bace.drivers.agilent81150 import Agilent81150
    from bace.drivers.infiniium import Infiniium
    from bace.drivers.routing import BiasRouter
    from bace.drivers.shutter import SimulatedShutter

    class _IO:
        timeout = 0
        def write(self, *a): pass
        def query(self, *a): return "0,"

    class _DIO:
        def set(self, ch, v): pass

    assert isinstance(Agilent81150(_IO()), P.BiasSource)
    assert isinstance(Infiniium(_IO()), P.Digitizer)
    assert isinstance(SimulatedShutter(), P.Shutter)
    assert isinstance(BiasRouter(dio=_DIO()), P.Router)


def test_every_simulated_instrument_satisfies_its_protocol() -> None:
    from bace.drivers import protocols as P
    from bace.drivers.simulated import make_bench

    r = make_bench()
    assert isinstance(r.bias, P.BiasSource)
    assert isinstance(r.scope, P.Digitizer)
    assert isinstance(r.shutter, P.Shutter)
    assert isinstance(r.smu, P.SourceMeter)
    assert isinstance(r.power, P.PowerMeter)
    assert isinstance(r.router, P.Router)


def test_a_digitizer_that_cannot_report_clipping_is_not_a_digitizer() -> None:
    """The 2026-09-01 defect, in the form that would have caught it.

    `transient.py` asked the scope for `clipped`. `SimulatedDigitizer` had it,
    `Infiniium` called the same state `last_autorange_clipped`, and the protocol
    required neither — so `getattr(scope, "clipped", False)` reported no
    clipping for every shot ever taken on hardware, silently, while the
    simulator-backed tests passed.

    `runtime_checkable` protocols check non-method members too, so putting the
    name in `Digitizer` makes the mismatch a failure rather than a default.
    """
    from bace.drivers import protocols as P
    from bace.drivers.infiniium import Infiniium

    class _IO:
        timeout = 0
        def write(self, *a): pass
        def query(self, *a): return "0,"

    class DigitizerWithoutClipped:
        def configure_timebase(self, *a, **k): ...
        def configure_edge_trigger(self, *a, **k): ...
        def acquire(self, *a, **k): ...

    assert not isinstance(DigitizerWithoutClipped(), P.Digitizer), (
        "a driver missing `clipped` must not pass for a Digitizer — that is the "
        "whole point of the name being in the protocol"
    )
    assert isinstance(Infiniium(_IO()), P.Digitizer)
