"""`python -m bace.service`: one service process on the lab PC.

    python -m bace.service --sim --fast --port 8900          # develop off the bench
    py -3 -m bace.service --rig rig.toml --run run.toml      # the lab PC

The desk needs the `service` extra (`pip install -e .[service]`); the lab PC
needs the rig's VISA stack as well (`pip install -e .[lab]`, which is
`rig` + `service`). Configs are found the way `bace.bench` finds them -- an
explicit path, else `rig.toml`/`run.toml` in the working directory or
beside the package -- and a missing file falls back to the built-in
defaults with a printed warning, so the service comes up on a fresh
checkout; a file that is *named* and missing is an error, because a typo in
`--run` that silently ran the defaults would be a recipe nobody chose.
`--fast` is refused without `--sim`: it makes every settle a no-op, which on
the real rig is a measurement taken before the device has settled, with no
symptom in the data. `--host` is refused unless it is a loopback address:
there is no authentication, and that is acceptable only where nobody but
this machine can connect.

`pyvisa` is imported only by `Bench.build_real`, so `--sim` works on a
machine with no VISA backend, and a real bench with no VISA runtime comes up
with its four VISA roles marked unavailable rather than not at all;
`uvicorn` is imported only in `main`, first thing, so a Python without the
extra is told which extra before anything touches the bench.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from typing import Any, Callable

from ..bench.checks import _find
from ..config import ConfigError, check_smu_limits, load_rig, load_run
from ..drivers.keithley2400 import SourceMeterConfig
from ..experiment.rig import RigConfig
from ..experiment.transient import RunConfig
from ..params import run_toml_layer
from .monitors import MAX_INTERVAL_S, MIN_INTERVAL_S, TEMPERATURE_MONITOR_S
from .session import Session

DEFAULT_HOST = "127.0.0.1"
"""Fixed unless overridden -- and overridable only with another loopback
address: there is no auth (the plan's "no auth", decided *because* the binding
is 127.0.0.1), so a `--host` that reaches the LAN would hand every instrument
on the bench to anyone on it."""

DEFAULT_PORT = 8900

SERVICE_EXTRA_HINT = ("the service needs the 'service' extra: pip install -e .[service] "
                      "(the lab PC needs the rig too: pip install -e .[lab])")


def is_loopback(host: str) -> bool:
    """`127.0.0.0/8`, `::1` or the name `localhost`; nothing else."""
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:
        return False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m bace.service",
        description="The BACE service: the bench, the modules, the runs and the "
                    "pipeline tree over HTTP and WebSocket on one port.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n", 1)[1] if "\n\n" in __doc__ else "")
    ap.add_argument("--sim", action="store_true",
                    help="build the rig from the simulated drivers (no VISA)")
    ap.add_argument("--fast", action="store_true",
                    help="with --sim: every sleep in the service's generators is a no-op")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--out", default="runs",
                    help="where run folders and the journal go (default: runs)")
    ap.add_argument("--rig", default=None, help="path to rig.toml")
    ap.add_argument("--run", default=None, help="path to run.toml")
    ap.add_argument("--ui", default=None,
                    help="a directory of static files to serve at /ui")
    ap.add_argument("--seed", type=int, default=0, help="the simulator's seed (--sim)")
    ap.add_argument("--power-monitor", type=float, default=None, metavar="SECONDS",
                    help="start the 1918-C monitor at this interval as the service comes "
                         "up, so the meter is watched whether or not a console is open "
                         "(the console's own switch starts and stops it too)")
    ap.add_argument("--temperature-monitor", type=float, default=TEMPERATURE_MONITOR_S,
                    metavar="SECONDS",
                    help=f"how often the 331 is read beside the bench (default: "
                         f"{TEMPERATURE_MONITOR_S:g} s). The temperature monitor is on "
                         f"unless --no-temperature-monitor says otherwise; on a bench "
                         f"with no 331 nothing is started and the card says why")
    ap.add_argument("--no-temperature-monitor", action="store_true",
                    help="do not read the 331 unless a run or POST /monitors/temperature "
                         "asks: the card then shows the last read-back and its age")
    return ap.parse_args(argv)


def load_configs(rig_path: str | None, run_path: str | None, *,
                 warn: Callable[[str], Any] = print) -> dict[str, Any]:
    """The two config files as the session wants them.

    Returns `rig_config`, `run_toml` (the raw tables, for provenance),
    `smu_config`, `run_config`, `rig_path`, `run_path`. A path given and not
    found raises `ConfigError`; a path not given and not found falls back to
    the defaults through `warn`.
    """
    p_rig = rig_path or _find("rig.toml")
    p_run = run_path or _find("run.toml")
    if rig_path and not os.path.isfile(rig_path):
        raise ConfigError(f"--rig: no such file: {rig_path}")
    if run_path and not os.path.isfile(run_path):
        raise ConfigError(f"--run: no such file: {run_path}")

    rig_config = RigConfig()
    run_toml: dict[str, Any] = {}
    smu_config, run_config = SourceMeterConfig(), RunConfig()
    if p_rig:
        rig_config = load_rig(p_rig)
    else:
        warn("rig.toml not found; using the built-in bench defaults")
    if p_run:
        # `load_run` refuses unknown keys and builds the dataclasses; the raw
        # tables are what the catalogue attributes values to.
        _spec, run_config, _drive, smu_config, _meta, _extras = load_run(p_run)
        run_toml = run_toml_layer(p_run)
    else:
        warn("run.toml not found; using the built-in recipe defaults")
    check_smu_limits(smu_config, rig_config)
    return {"rig_config": rig_config, "run_toml": run_toml, "smu_config": smu_config,
            "run_config": run_config, "rig_path": p_rig, "run_path": p_run}


def build_session(a: argparse.Namespace, *, warn: Callable[[str], Any] = print) -> Session:
    """A `Session` from the parsed arguments. Nothing runs until the app's
    startup hook starts it."""
    cfg = load_configs(a.rig, a.run, warn=warn)
    return Session(cfg["rig_config"], cfg["run_toml"], out=a.out,
                   mode="sim" if a.sim else "rig", fast=a.fast,
                   smu_config=cfg["smu_config"], run_config_defaults=cfg["run_config"],
                   rig_path=cfg["rig_path"], run_path=cfg["run_path"], seed=a.seed,
                   power_monitor_s=getattr(a, "power_monitor", None),
                   temperature_monitor_s=(None if getattr(a, "no_temperature_monitor", False)
                                          else getattr(a, "temperature_monitor",
                                                       TEMPERATURE_MONITOR_S)))


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    # Everything that can refuse is refused before the session is built:
    # `build_session` opens every VISA resource, resets the scope and writes
    # the journal's header, and a `py -3` without the service extra used to
    # do all of that and then die on `import uvicorn` with a traceback that
    # named no extra.
    try:
        import uvicorn

        from .app import create_app
    except ImportError as exc:
        print(f"{SERVICE_EXTRA_HINT}  (missing: {getattr(exc, 'name', None) or exc})")
        return 2
    if a.fast and not a.sim:
        print("--fast makes every settle a no-op; that is for --sim only. On the "
              "real rig it would measure before the device has settled.")
        return 2
    if a.ui is not None and not os.path.isdir(a.ui):
        print(f"--ui: {a.ui!r} is not a directory")
        return 2
    if a.power_monitor is not None and not 0.01 <= a.power_monitor <= 3600:
        print(f"--power-monitor {a.power_monitor:g}: the interval is in seconds, between "
              "0.01 and 3600")
        return 2
    if not MIN_INTERVAL_S <= a.temperature_monitor <= MAX_INTERVAL_S:
        print(f"--temperature-monitor {a.temperature_monitor:g}: the interval is in "
              f"seconds, between {MIN_INTERVAL_S:g} and {MAX_INTERVAL_S:g}")
        return 2
    if not is_loopback(a.host):
        print(f"--host {a.host!r} is not a loopback address. The service has no "
              "authentication, which is safe only because it listens where this "
              "machine alone can reach it; bind 127.0.0.1 and put a proxy in front "
              "if the console must be reached from elsewhere.")
        return 2
    try:
        session = build_session(a)
    except ConfigError as exc:
        print(f"config: {exc}")
        return 2

    app = create_app(session, ui_dir=a.ui)
    url = f"http://{a.host}:{a.port}/"
    print(f"bace service  {session.mode}{' fast' if session.fast else ''}  "
          f"session {session.session_id}")
    print(f"  rig.toml   {session.rig_path or '(defaults)'}")
    print(f"  run.toml   {session.run_path or '(defaults)'}")
    print(f"  out        {os.path.abspath(session.out)}")
    print(f"  journal    {os.path.abspath(session.journal.path)}")
    print(f"  url        {url}{'ui/' if a.ui else ''}")
    for role, why in session.bench.unavailable.items():
        print(f"  unavailable  {role}: {why}")
    for write in session.bench.startup_writes:
        print(f"  startup write  {write}")
    sys.stdout.flush()
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
