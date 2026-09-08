"""The Keithley 2400 console: one instrument, one process, one port.

    python -m bace.consoles.keithley --sim          # no hardware, no VISA
    python -m bace.consoles.keithley                # GPIB0::24, from rig.toml
    python -m bace.consoles.keithley --address GPIB0::24::INSTR --port 8925

Then open http://127.0.0.1:8924/.

**This is not `bace.service` and does not import it.** It opens the
SourceMeter itself, serves one page from the standard library, and touches no
other instrument -- no relay, no shutter, no LED, no scope -- and writes no
file. It is what to run *instead of* the service when the Keithley is what you
want by hand.

**One process owns the instrument.** The two cannot both hold `GPIB0::24`, and
the one that starts second gets a VISA error that reads like a cable fault. So
stop the service before starting this, and stop this before starting the
service. That constraint is exactly why the 1918-C and the 331 have consoles
of their own (`docs/service-plan.md`), and this is the same shape for the 2400.

`rig.toml` supplies the address and the two bench ceilings; `run.toml`, if it
is there, supplies the compliance, integration time and terminals the panel
opens on. Both are found by name in the working directory as everything else
in this project finds them, and a missing file is a printed warning and the
built-in defaults -- **a file that is named and missing is an error**, because
a typo in `--rig` that silently ran the defaults would be a bench nobody chose.

`--host` is refused unless it is a loopback address. There is no
authentication and this program drives a source into somebody's device, which
is acceptable only where nobody but this machine can connect.
"""
from __future__ import annotations

import argparse
import errno
import ipaddress
import os
import sys
from typing import Any

from ...bench.checks import _find
from ...config import ConfigError, load_rig, load_run
from ...drivers.keithley2400 import SourceMeterConfig
from ...experiment.rig import RigConfig
from .panel import POLL_DEFAULT_S, POLL_MAX_S, POLL_MIN_S, KeithleyPanel
from .server import serve_forever

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8924
"""8924 the way the 1918-C's console is on 8918 and the 331's on 8331: the
last two digits are the instrument's GPIB address, so the port says which
instrument answers on it."""


def is_loopback(host: str) -> bool:
    """`127.0.0.0/8`, `::1` or the name `localhost`; nothing else."""
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:
        return False


WINDOWS_PORT_TAKEN = (10013, 10048)
"""`WSAEACCES` and `WSAEADDRINUSE`. Windows does not answer a bound port with
`EADDRINUSE`: a second `bind` raises 10013. Matched on `winerror` rather than
`errno`, which Python maps 10013 to `EACCES` -- and `EACCES` on POSIX is a
privileged port, not a busy one."""


def _port_is_taken(exc: OSError) -> bool:
    """Whether `exc` from `bind` means somebody already has the port."""
    return (getattr(exc, "errno", None) == errno.EADDRINUSE
            or getattr(exc, "winerror", None) in WINDOWS_PORT_TAKEN)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m bace.consoles.keithley",
        description="The Keithley 2400's front panel, in a browser. One "
                    "instrument, no runs, no files.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default=DEFAULT_HOST,
                   help="loopback address to bind (default %(default)s)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help="default %(default)s -- 24 is the 2400's GPIB address")
    p.add_argument("--sim", action="store_true",
                   help="a simulated SourceMeter; no VISA and no instrument")
    p.add_argument("--sim-dark", action="store_true",
                   help="with --sim, an unlit cell (sourcing 0 A then reads 0 V)")
    p.add_argument("--address", default=None,
                   help="VISA address, overriding rig.toml [sourcemeter] address")
    p.add_argument("--rig", default=None, metavar="PATH",
                   help="rig.toml: the address and the two bench ceilings")
    p.add_argument("--run", default=None, metavar="PATH",
                   help="run.toml: the compliance and timing the panel opens on")
    p.add_argument("--poll", type=float, default=POLL_DEFAULT_S, metavar="SECONDS",
                   help=f"display refresh, {POLL_MIN_S:g}-{POLL_MAX_S:g} s; "
                        "0 leaves it off (default %(default)s)")
    p.add_argument("--timeout-ms", type=int, default=20000,
                   help="VISA timeout for the session (default %(default)s)")
    return p


def _load_config(a: argparse.Namespace) -> tuple[RigConfig, SourceMeterConfig]:
    """`rig.toml` and `run.toml`, named or found. A named file that is not
    there is an error; a file nobody named is a warning and the defaults."""
    rig, smu = RigConfig(), SourceMeterConfig()
    path = a.rig or _find("rig.toml")
    if a.rig and not os.path.isfile(a.rig):
        raise ConfigError(f"--rig {a.rig!r}: no such file")
    if path:
        rig = load_rig(path)
        print(f"rig.toml   {path}")
    else:
        print("rig.toml   not found; using the built-in bench ceilings "
              f"({rig.max_current_compliance_a:g} A / {rig.max_voltage_compliance_v:g} V)")
    path = a.run or _find("run.toml")
    if a.run and not os.path.isfile(a.run):
        raise ConfigError(f"--run {a.run!r}: no such file")
    if path:
        smu = load_run(path)[3]
        print(f"run.toml   {path}")
    else:
        print("run.toml   not found; the panel opens on the driver's defaults")
    return rig, smu


def _release(smu: Any) -> None:
    """Put down an instrument nothing is going to serve.

    Between `_open_real` and `serve_forever` the SourceMeter is open -- and
    possibly driving -- with none of the shutdown the server's `finally`
    provides. Anything failing in that window has to do this much itself, and
    in this order: the output first, because that is the part that matters,
    and closing a session on a driving source would leave it driving with
    nothing left in this process able to reach it.

    Every step is best-effort. This runs on a path that is already failing,
    and a second exception here would replace a sentence about the
    configuration with a traceback about VISA.
    """
    for step in ("disable_output", "close"):
        try:
            method = getattr(smu, step, None)
            if callable(method):
                method()
        except Exception:                                    # noqa: BLE001
            pass


def _open_simulated(dark: bool):
    """A simulated SourceMeter with a cell in front of it.

    The relay is put on the SourceMeter here because there is no relay on this
    console to move -- the simulated bench models one (`drivers/simulated.py`),
    and off the SourceMeter's side the panel would read an open circuit for a
    reason no button on this page could fix. Lit unless `--sim-dark`, so
    sourcing 0 A shows a plausible V_oc the moment the output goes on and the
    panel demonstrates something.
    """
    from ...drivers.simulated import make_bench
    sim = make_bench(seed=0)
    sim.bench.relay = "sourcemeter"
    if not dark:
        sim.bench.shutter_open = True
        sim.bench.led_mode = "DC"
        sim.bench.led_drive_v = 1.020
    return sim.smu


def _open_real(address: str, config: SourceMeterConfig, timeout_ms: int):
    """`(smu, identity, reason it is missing)`. Never raises: a console that
    would not start because the instrument is off is a console that cannot
    tell you the instrument is off."""
    try:
        import pyvisa
    except ImportError as exc:
        return None, "", (f"pyvisa is not installed ({exc}); pip install -e .[rig], "
                          "or run with --sim")
    from ...drivers.keithley2400 import Keithley2400
    try:
        rm = pyvisa.ResourceManager()
        res = rm.open_resource(address)
    except Exception as exc:                                 # noqa: BLE001
        return None, "", (f"{address}: {type(exc).__name__}: {exc}. If the measurement "
                          "service is running it holds this session -- one process owns "
                          "the instrument; stop it first.")
    try:
        res.timeout = timeout_ms
        smu = Keithley2400(res, config=config, timeout_ms=timeout_ms)
        identity = smu.identify()
        smu.read_output()
    except Exception as exc:                                 # noqa: BLE001
        try:
            res.close()
        except Exception:                                    # noqa: BLE001
            pass
        return None, "", f"{address}: {type(exc).__name__}: {exc}"
    return smu, identity, ""


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if not is_loopback(a.host):
        print(f"--host {a.host!r} is not a loopback address. This console has no "
              "authentication and drives a source into a device, so it binds where "
              "this machine alone can reach it; bind 127.0.0.1 and put a proxy in "
              "front if it has to be reachable.", file=sys.stderr)
        return 2
    if a.sim_dark and not a.sim:
        print("--sim-dark is about the simulated cell; it needs --sim.", file=sys.stderr)
        return 2
    if a.poll and not POLL_MIN_S <= a.poll <= POLL_MAX_S:
        print(f"--poll is between {POLL_MIN_S:g} and {POLL_MAX_S:g} s, or 0 for off.",
              file=sys.stderr)
        return 2
    try:
        rig, smu_config = _load_config(a)
    except ConfigError as exc:
        print(f"configuration: {exc}", file=sys.stderr)
        return 2

    address = a.address or rig.sourcemeter_address
    if a.sim:
        smu, identity, why = _open_simulated(a.sim_dark), "simulated 2400", ""
        address = "simulated"
    else:
        smu, identity, why = _open_real(address, smu_config, a.timeout_ms)
    if why:
        print(f"no SourceMeter: {why}", file=sys.stderr)

    try:
        panel = KeithleyPanel(
            smu, ceiling={"current_a": rig.max_current_compliance_a,
                          "voltage_v": rig.max_voltage_compliance_v},
            defaults=smu_config, identity=identity, address=address,
            mode="sim" if a.sim else "rig", unavailable=why)
    except Exception as exc:                                 # noqa: BLE001
        # The panel validates the bench ceilings and the defaults `run.toml`
        # opens on, and by here the instrument is already open -- `_open_real`
        # has identified it and read its output, which may be live, left that
        # way by whoever had it before. This construction is outside
        # `serve_forever` and therefore outside the `finally` that switches
        # that output off, so a `nan` ceiling or an out-of-range NPLC exited
        # with the source driving and the session held.
        # Deliberately every exception, not just the `ValueError` the ceilings
        # raise today: what must not depend on which exception a later edit
        # introduces here is that the instrument is put down. `_load_config`
        # already owns the "configuration:" prefix, so this says which step
        # failed rather than guessing at a cause.
        _release(smu)
        print(f"the panel could not be built: {exc}", file=sys.stderr)
        return 2

    print(f"instrument {identity or 'unavailable'}  ({address})")
    print(f"ceilings   {rig.max_current_compliance_a:g} A / "
          f"{rig.max_voltage_compliance_v:g} V  (rig.toml [sourcemeter])")
    if a.sim:
        print("**simulated** — every number this console shows is a model, not a "
              "measurement.")

    def ready(server) -> None:
        if a.poll:
            panel.set_poll(a.poll)
        print(f"\nthe panel  http://{a.host}:{server.server_port}/   (Ctrl-C to stop; "
              "the output goes off on the way out)")

    try:
        serve_forever(panel, host=a.host, port=a.port, on_ready=ready)
    except OSError as exc:
        # Almost always the console started twice. A traceback about a socket
        # would send somebody looking at the instrument — and the instrument
        # is fine; `serve_forever`'s own `finally` has already switched its
        # output off on the way out. Same treatment, and the same reasoning,
        # as `examples/shutter_console`.
        print(f"cannot listen on {a.host}:{a.port}: {exc}", file=sys.stderr)
        if _port_is_taken(exc):
            print(f"  a Keithley console is already running — its page is at "
                  f"http://{a.host}:{a.port}/", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(main())
