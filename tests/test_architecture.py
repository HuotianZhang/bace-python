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
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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


def test_experiment_and_storage_do_not_depend_on_service() -> None:
    """Dependencies point inward: the engine and the file formats must never
    know the service exists, or a route change could reach into a
    measurement. The service wraps them; they do not call back."""
    offenders = {}
    for sub in ("experiment", "storage"):
        for f in sorted((PKG / sub).rglob("*.py")):
            bad = _imported_subpackages(f) & {"service", "ui"}
            if bad:
                offenders[f"{sub}/{f.name}"] = sorted(bad)
    assert not offenders, f"experiment/ or storage/ reached out to the service: {offenders}"


def _all_import_heads(path: pathlib.Path, *, module_level_only: bool) -> set[str]:
    """Top-level names imported by a file: every import statement anywhere
    (functions included), or only the ones at module level."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    nodes = ast.walk(tree) if not module_level_only else _module_level(tree)
    out: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.add(node.module.split(".")[0])
    return out


def _module_level(tree: ast.Module):
    """Statements that run at import time: the module body and the bodies
    of `if`/`try` blocks in it, but nothing inside a def or class."""
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.If, ast.Try)):
            for attr in ("body", "orelse", "finalbody"):
                stack.extend(getattr(node, attr, []) or [])
            for h in getattr(node, "handlers", []) or []:
                stack.extend(h.body)


WEB = {"fastapi", "uvicorn", "starlette", "pydantic"}


def test_only_the_app_and_the_cli_import_fastapi() -> None:
    """FastAPI and uvicorn are an optional extra. `pipeline`, `modules`,
    `journal`, `worker`, `executor` and the rest are the service's logic and
    must import on a machine that has only numpy -- the tests run them
    without a web stack, and so does anyone scripting a session."""
    offenders = {}
    for f in sorted((PKG / "service").glob("*.py")):
        if f.name in ("app.py", "__main__.py"):
            continue
        bad = _all_import_heads(f, module_level_only=False) & WEB
        if bad:
            offenders[f.name] = sorted(bad)
    assert not offenders, f"the service core imports the web stack: {offenders}"


def test_pyvisa_is_imported_lazily_by_the_service() -> None:
    """`--sim` must work with no VISA backend, so no module in the service
    imports pyvisa at import time; `rigs.build_real` does it inside the
    function, on the lab PC only."""
    offenders = [f.name for f in sorted((PKG / "service").glob("*.py"))
                 if "pyvisa" in _all_import_heads(f, module_level_only=True)]
    assert not offenders, f"pyvisa imported at module level in the service: {offenders}"


def test_the_service_core_imports_without_fastapi_or_pyvisa() -> None:
    """The same rule, proven by running it: a fresh interpreter with
    `fastapi`, `uvicorn`, `starlette` and `pyvisa` made unimportable must
    still import every service module but `app` and `__main__`."""
    import subprocess
    import sys

    code = (
        "import sys\n"
        "for name in ('fastapi', 'uvicorn', 'starlette', 'pyvisa'):\n"
        "    sys.modules[name] = None\n"
        "import bace.service.pipeline, bace.service.modules, bace.service.journal\n"
        "import bace.service.worker, bace.service.executor, bace.service.session\n"
        "import bace.service.rigs, bace.service.monitors, bace.service.wire\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('fastapi', 'uvicorn', 'starlette', 'pyvisa') and sys.modules[m] is not None)\n"
        "print('loaded', loaded)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=str(PKG.parent), timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "loaded []" in proc.stdout, proc.stdout


def test_every_real_driver_satisfies_its_protocol() -> None:
    from bace.drivers import protocols as P
    from bace.drivers.agilent33220a import Agilent33220A
    from bace.drivers.agilent81150 import Agilent81150
    from bace.drivers.infiniium import Infiniium
    from bace.drivers.lakeshore331 import ConsoleTemperatureController
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
    assert isinstance(Agilent33220A(_IO()), P.LedSource)
    # the 331 is a client of its console, not a VISA driver: no resource
    assert isinstance(ConsoleTemperatureController(), P.TemperatureController)


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
    assert isinstance(r.led, P.LedSource)
    assert isinstance(r.temperature, P.TemperatureController)


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
