"""`tools/shutter.py`: the shutter on its own, and nothing else touched.

The tool exists so a person at the bench can move the shutter without the
service, without VISA and without an interpreter that has numpy in it. Three
things have to hold for that to keep being true, and none of them is visible
from reading the driver:

* it imports nothing heavy -- asserted in a subprocess, because this one has
  numpy loaded already;
* its copy of the `[dio]` defaults still matches `RigConfig`;
* it refuses the rig.toml the service refuses, and the relay's module.

The line itself is a `SimulatedShutter`, so the moves are asserted without a
DLL: what is under test is the tool's decisions, not Deditec's.
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import pytest

from bace.config import ConfigError, load_rig
from bace.drivers.shutter import SimulatedShutter
from bace.experiment.rig import RigConfig

TOOL = pathlib.Path(__file__).resolve().parents[1] / "tools" / "shutter.py"


def _cli():
    spec = importlib.util.spec_from_file_location("shutter_cli", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _cli()


class Line(SimulatedShutter):
    """A simulated line that remembers every write, and starts where it is put.

    `SimulatedShutter.open()` does not touch `_state`, so a line handed to the
    tool already open stays open until the tool moves it -- which is the case
    that matters: reading must not close somebody's open shutter.
    """

    def __init__(self, state=None, **kw):
        super().__init__(**kw)
        self._state = state
        self.writes: list[int] = []
        self.released = False

    def _set(self, value: int) -> None:
        self.writes.append(value)
        super()._set(value)

    def release(self) -> None:
        self.released = True
        super().release()


def _run(argv, line):
    return cli.main(argv, make=lambda cfg, **kw: line)


# -- what it depends on ---------------------------------------------------
def test_it_imports_nothing_the_rig_interpreter_may_not_have() -> None:
    """The whole point of the file: it runs under the 32-bit Python that can
    load the rig's DELIB, which has no numpy, no scipy and no pyvisa."""
    code = (
        "import importlib.util, sys;"
        f"spec = importlib.util.spec_from_file_location('t', {str(TOOL)!r});"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "print(sorted(n for n in sys.modules "
        "if n in ('numpy', 'scipy', 'pyvisa', 'h5py', 'fastapi')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", (
        "tools/shutter.py pulled in " + out.stdout.strip() + ". It is the one "
        "program here that must import nothing but the standard library and "
        "the shutter driver.")


def test_its_defaults_still_match_the_rig_config() -> None:
    """Repeated constants, because `RigConfig` imports numpy. This is the
    thing that fails instead of the tool driving the wrong line."""
    assert cli.DEFAULT_SHUTTER_MODULE_NR == RigConfig.shutter_module_nr
    assert cli.DEFAULT_RELAY_MODULE_NR == RigConfig.relay_module_nr


def test_it_reads_the_rigs_own_dio_block() -> None:
    cfg = cli.read_dio(str(pathlib.Path(cli.REPO_ROOT) / "rig.toml"))
    rig = load_rig(str(pathlib.Path(cli.REPO_ROOT) / "rig.toml"))
    assert cfg["module_id"] == rig.dio_module_id
    assert cfg["shutter_module_nr"] == rig.shutter_module_nr
    assert cfg["relay_module_nr"] == rig.relay_module_nr


@pytest.mark.parametrize("dio", [
    "[dio]\nshutter_module_nr = 1\nrelay_module_nr = 1\n",
    "[dio]\nshutter_module_nr = 1\nrelay_module_nr = 0\n",
])
def test_a_rig_the_service_refuses_is_one_this_refuses(tmp_path, dio) -> None:
    """Both programs read the same file; if only one of them refuses it, the
    bench has two opinions about which line is the shutter."""
    path = tmp_path / "rig.toml"
    path.write_text(dio, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_rig(str(path))
    with pytest.raises(cli.DioError):
        cli.read_dio(str(path))


def test_a_named_rig_that_is_missing_is_an_error(tmp_path) -> None:
    with pytest.raises(cli.DioError):
        cli.find_rig(str(tmp_path / "nope.toml"))


# -- what it does ---------------------------------------------------------
def test_reading_moves_nothing_and_leaves_the_shutter_open(capsys) -> None:
    line = Line(state=1)
    assert _run([], line) == 0
    assert line.writes == []
    assert line._state == 1
    assert line.released
    assert "open" in capsys.readouterr().out


def test_open_and_shut_drive_the_line(capsys) -> None:
    line = Line(state=0)
    assert _run(["--open"], line) == 0
    assert line.writes == [1] and line._state == 1
    assert "moved              = 0 -> 1" in capsys.readouterr().out

    line = Line(state=1)
    assert _run(["--shut"], line) == 0
    assert line.writes == [0] and line._state == 0


def test_a_line_already_there_is_not_written_to(capsys) -> None:
    line = Line(state=1)
    assert _run(["--open"], line) == 0
    assert line.writes == []
    assert "nothing to do" in capsys.readouterr().out


def test_a_line_that_cannot_be_read_back_is_still_driven() -> None:
    """`DapiDOReadback32` is missing from older DELIB builds. Unknown is not
    'already there': the move has to happen anyway."""
    line = Line(state=None)
    assert _run(["--open"], line) == 0
    assert line.writes == [1]


def test_it_refuses_the_relays_module(capsys) -> None:
    line = Line(state=0)
    assert _run(["--open", "--module-nr", "1"], line) == 2
    assert line.writes == []
    assert not line.released, "the module was never opened, so nothing to release"
    assert "REFUSING" in capsys.readouterr().out


def test_force_is_the_way_past_that(capsys) -> None:
    line = Line(state=0)
    assert _run(["--open", "--module-nr", "1", "--force"], line) == 0
    assert line.writes == [1]
    assert "is the RELAY" in capsys.readouterr().out


def test_reading_the_relays_line_is_allowed(capsys) -> None:
    """Reading is free and always safe -- `tools/relay.py` does exactly this."""
    line = Line(state=1)
    assert _run(["--module-nr", "1"], line) == 0
    assert line.writes == []


def test_the_module_is_released_not_closed() -> None:
    """`Shutter.close()` shuts the line first. After `--open` that would undo
    the only thing the operator asked for."""
    line = Line(state=0)
    assert _run(["--open"], line) == 0
    assert line._state == 1 and line._handle is None and line.released
