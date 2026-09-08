"""The Keithley 2400's front panel, in a browser. One instrument, one port.

    py -3 keithley_console.py --sim        try it with no hardware at all
    py -3 keithley_console.py              GPIB0::24, from rig.toml
    py -3 keithley_console.py --address GPIB0::24::INSTR --port 8925

Then <http://127.0.0.1:8924/>. On Windows, `Run Keithley Console.bat` is the
double-click form.

Source a voltage or a current, hold a compliance, switch the output on, watch
what comes back. No module, no run, no folder, and no other instrument — no
relay, no shutter, no LED, no scope. Nothing here writes a file.

## Standalone on purpose

This folder imports **nothing** from `bace/` — not the drivers, not the config,
not the service, and not the UI. Copy it to a machine with the SourceMeter and
a Python 3.11 and it runs; there is nothing else to install, and under `--sim`
not even a VISA backend. `smu.py` and `simulated.py` are therefore a deliberate
second copy of what `bace/drivers/keithley2400.py` and `bace/drivers/
simulated.py` do properly — go there for the measurement routines, the J-V
sweep and the bench history. `tests/test_keithley_console.py` holds the
standalone property down, in a subprocess, because it is the one most easily
lost by accident.

## One process owns the instrument

The service and this console cannot both hold `GPIB0::24`, and the one that
starts second gets a VISA error that reads like a cable fault. So stop the
service before starting this, and stop this before starting the service. That
constraint is exactly why the 1918-C and the 331 have consoles of their own,
and this is the same shape for the 2400. The port says which: 8924 the way the
1918-C's is 8918 and the 331's is 8331 — the last two digits are the GPIB
address.

## Configuration

`rig.toml` supplies the address and the two bench ceilings; `run.toml`, if it
is there, supplies the compliance, integration time and terminals the panel
opens on. Both are looked for by name in the working directory, then beside
this file, then in every directory above it — so the double-click launcher
finds a checkout's files two levels up, and a copy of this folder on a bench PC
finds whatever sits next to it. Whichever file is used is printed.

A missing file is a printed warning and the built-in defaults; **a file that is
named and missing is an error**, because a typo in `--rig` that silently ran the
defaults would be a bench nobody chose. So is an unrecognised key inside one:
`current_complaince_a` dropped quietly would open the panel on the built-in
0.05 A instead of the 0.001 written down.

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
import tomllib
from dataclasses import dataclass, fields
from typing import Any

from panel import POLL_DEFAULT_S, POLL_MAX_S, POLL_MIN_S, KeithleyPanel
from server import serve_forever
from simulated import SimulatedSourceMeter
from smu import SourceMeterConfig

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8924

HERE = os.path.dirname(os.path.abspath(__file__))


class ConfigError(RuntimeError):
    """A file that was named and could not be used."""


@dataclass(frozen=True)
class Rig:
    """`rig.toml [sourcemeter]` — the bench, not a measurement.

    The two ceilings are the limits no recipe may exceed, because a 2400 will
    happily push 1 A into a small cell. This console is the one surface in the
    project where an operator types a level straight onto the device, so they
    are what stands over that number.
    """

    address: str = "GPIB0::24::INSTR"
    max_current_compliance_a: float = 0.05
    max_voltage_compliance_v: float = 5.0


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
privileged port, not a busy one, so matching it there would explain `--port 80`
as a console that is already running."""


def _port_is_taken(exc: OSError) -> bool:
    """Whether `exc` from `bind` means somebody already has the port."""
    return (getattr(exc, "errno", None) == errno.EADDRINUSE
            or getattr(exc, "winerror", None) in WINDOWS_PORT_TAKEN)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="keithley_console.py",
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
    p.add_argument("--browser", action="store_true",
                   help="open the page once the server is bound")
    return p


def _find(name: str) -> str | None:
    """The working directory first, then this folder and every folder above it.

    Walking up is what makes the double-click launcher work: `Run Keithley
    Console.bat` does `cd /d "%~dp0"`, so in a checkout the working directory
    *is* this folder and the repository's `rig.toml` is two levels above it.
    Looking only here and in the working directory found neither, and the
    console started on its built-in ceilings -- which on this bench happen to
    equal the file's, but that is a coincidence and not a guarantee, and a
    bench that had lowered its ceiling would have had it quietly raised again.

    It works the same way on a machine that has only this folder: drop a
    `rig.toml` beside the script, or in the directory holding it, and it is
    found. Whichever file is used is printed, every time, so the answer to
    "which ceilings am I on" is on the screen and not in this docstring.
    """
    seen = []
    here = HERE
    while True:
        seen.append(here)
        parent = os.path.dirname(here)
        if parent == here:                     # the filesystem root
            break
        here = parent
    for base in [os.getcwd(), *seen]:
        path = os.path.join(base, name)
        if os.path.isfile(path):
            return path
    return None


IGNORED_RUN_KEYS = frozenset({"settle_jsc_ms", "settle_voc_ms", "settle_jsat_ms"})
"""`run.toml [sourcemeter]` keys this console reads and does nothing with.

They are the three settle times the package's measurement routines wait out
between sourcing and reading. A panel has no such step -- the operator is the
one who decides when the number has settled -- so they are accepted and
dropped. Named rather than ignored wholesale, because the check below has to be
able to tell "a field this program does not use" from "a field somebody
misspelled"."""


def _table(path: str, section: str, allowed: frozenset[str]) -> dict:
    """One table out of a TOML file, with every key checked.

    **An unrecognised key is an error, never a silent default.** A typo like
    `current_complaince_a = 0.001` would otherwise be dropped and the panel
    would open on the built-in 0.05 A -- fifty times the current the operator
    wrote down, on the one surface in this project where a level goes straight
    onto the device. The package's loader has refused unknown keys since long
    before this console existed (`bace/config.py`, `_check`); this folder does
    not import that, so it carries the same rule itself.
    """
    try:
        with open(path, "rb") as handle:
            table = dict(tomllib.load(handle).get(section, {}))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from None
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(
            f"{path}: unknown key(s) in [{section}]: {', '.join(sorted(unknown))}. "
            "Refusing to fall back to defaults -- a misspelt compliance here is "
            "a silently raised limit, and a misspelt ceiling is no limit at all.")
    return table


def _load_config(a: argparse.Namespace) -> tuple[Rig, SourceMeterConfig]:
    """`rig.toml` and `run.toml`, named or found.

    Deliberately narrow: only `[sourcemeter]` out of each, and only the keys a
    panel opens on. The package reads these files properly, validating every
    section against the whole recipe; a console that refused to start because
    an *oscilloscope* stanza was malformed would be refusing for a reason it
    cannot act on.
    """
    rig, config = Rig(), SourceMeterConfig()

    if a.rig and not os.path.isfile(a.rig):
        raise ConfigError(f"--rig {a.rig!r}: no such file")
    path = a.rig or _find("rig.toml")
    if path:
        t = _table(path, "sourcemeter", frozenset(f.name for f in fields(Rig)))
        rig = Rig(address=str(t.get("address", rig.address)),
                  max_current_compliance_a=float(
                      t.get("max_current_compliance_a", rig.max_current_compliance_a)),
                  max_voltage_compliance_v=float(
                      t.get("max_voltage_compliance_v", rig.max_voltage_compliance_v)))
        print(f"rig.toml   {path}")
    else:
        print("rig.toml   not found; using the built-in bench ceilings "
              f"({rig.max_current_compliance_a:g} A / {rig.max_voltage_compliance_v:g} V)")

    if a.run and not os.path.isfile(a.run):
        raise ConfigError(f"--run {a.run!r}: no such file")
    path = a.run or _find("run.toml")
    if path:
        used = frozenset(f.name for f in fields(SourceMeterConfig))
        t = _table(path, "sourcemeter", used | IGNORED_RUN_KEYS)
        try:
            config = SourceMeterConfig(**{k: v for k, v in t.items() if k in used})
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{path} [sourcemeter]: {exc}") from None
        print(f"run.toml   {path}")
    else:
        print("run.toml   not found; the panel opens on the driver's defaults")
    return rig, config


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

    Lit unless `--sim-dark`, so sourcing 0 A shows a plausible V_oc the moment
    the output goes on and the panel demonstrates something rather than a row
    of zeroes.
    """
    return SimulatedSourceMeter(lit=not dark, seed=0)


def _open_real(address: str, config: SourceMeterConfig, timeout_ms: int):
    """`(smu, identity, reason it is missing)`. Never raises: a console that
    would not start because the instrument is off is a console that cannot
    tell you the instrument is off."""
    try:
        import pyvisa
    except ImportError as exc:
        return None, "", (f"pyvisa is not installed ({exc}); pip install pyvisa, "
                          "or run with --sim")
    from smu import Keithley2400
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

    address = a.address or rig.address
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
        # Deliberately every exception, not just the `ValueError` the ceilings
        # raise today: what must not depend on which exception a later edit
        # introduces here is that the instrument is put down. By this point
        # `_open_real` has identified it and read an output that may be live.
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
        url = f"http://{a.host}:{server.server_port}/"
        print(f"\nthe panel  {url}   (Ctrl-C to stop; the output goes off on "
              "the way out)")
        if a.browser:
            import webbrowser
            webbrowser.open(url)

    try:
        serve_forever(panel, host=a.host, port=a.port, on_ready=ready)
    except OSError as exc:
        # Almost always the console started twice. A traceback about a socket
        # would send somebody looking at the instrument — and the instrument
        # is fine; `serve_forever`'s own `finally` has already switched its
        # output off on the way out.
        print(f"cannot listen on {a.host}:{a.port}: {exc}", file=sys.stderr)
        if _port_is_taken(exc):
            print(f"  a Keithley console is already running — its page is at "
                  f"http://{a.host}:{a.port}/", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(main())
