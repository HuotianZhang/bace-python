"""Find a DELIB DLL that matches the running interpreter.

Deditec installs `delib.dll` for 32-bit Python in `C:/Windows/SysWOW64`
and `delib64.dll` for 64-bit Python in `C:/Windows/System32`. A wrong
architecture raises `OSError` with WinError 193. The search reads each PE
header and prefers the matching build rather than assuming a machine layout.

Both builds expose the same API: `DapiOpenModule` and `DapiCloseModule`
return `ULONG`; `DapiDOSet1` returns no value. Handles and arguments remain
32-bit `ULONG` on Windows, so `ctypes.c_ulong` applies to both. The x64
exports have undecorated C names and one calling convention; `__stdcall`
does not change them. Only the DLL choice depends on interpreter bitness."""
from __future__ import annotations

import os
import struct

#: Where Deditec's installers put each build, most specific first.
SEARCH_PATHS: tuple[str, ...] = (
    r"C:\Windows\System32\delib64.dll",
    r"C:\Windows\SysWOW64\delib64.dll",
    r"C:\Windows\System32\delib.dll",
    r"C:\Windows\SysWOW64\delib.dll",
    r"D:\BACE\DELIB\delib64.dll",
    r"D:\BACE\shutter\builds\data\delib.dll",
)

#: Search beside the package and in the working directory so a DLL beside
#: launcher files can be loaded.
LOCAL_NAMES: tuple[str, ...] = ("delib64.dll", "delib.dll")

#: PE machine types, from the COFF header.
_MACHINE = {0x014C: 32, 0x8664: 64, 0xAA64: 64}


def interpreter_bits() -> int:
    return struct.calcsize("P") * 8


def pe_bitness(path: str) -> int | None:
    """Return 32 or 64 from a PE header, or `None` if the header is unreadable."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(0x400)
        offset = int.from_bytes(head[0x3C:0x40], "little")
        return _MACHINE.get(int.from_bytes(head[offset + 4:offset + 6], "little"))
    except Exception:
        return None


def survey(explicit: str | None = None) -> list[tuple[str, int | None]]:
    """Return existing `(path, bitness)` candidates in search order.

    `explicit`, from `rig.toml` or `BACE_DELIB`, precedes installed defaults
    so a configured DLL takes priority."""
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    local = [os.path.join(d, n) for d in (os.getcwd(), here) for n in LOCAL_NAMES]

    paths: list[str] = []
    for p in (explicit, os.environ.get("BACE_DELIB"), *SEARCH_PATHS, *local):
        if p and p not in paths:
            paths.append(p)
    return [(p, pe_bitness(p)) for p in paths if os.path.isfile(p)]


def bare_name() -> str:
    """Return the DLL name for Windows to resolve through its search path.

    This permits installations outside the known locations. Windows redirects
    system directories according to process bitness."""
    return "delib64.dll" if interpreter_bits() == 64 else "delib.dll"


def load_candidates(explicit: str | None = None) -> list[str]:
    """Return DLL candidates in preferred load order.

    Matching PE headers precede unreadable headers, followed by a bare name."""
    bits = interpreter_bits()
    found = survey(explicit)
    out = [p for p, machine in found if machine == bits]
    out += [p for p, machine in found if machine is None]
    out.append(bare_name())
    return out


def resolve(explicit: str | None = None) -> str | None:
    """Return the first loadable DLL candidate, or `None`.

    An unreadable PE header does not prove the architecture is wrong.
    `ctypes` still attempts those candidates rather than rejecting them."""
    bits = interpreter_bits()
    found = survey(explicit)
    for path, machine in found:
        if machine == bits:
            return path
    for path, machine in found:
        if machine is None:
            return path
    return None


# ------------------------------------------------------------ [dio] in rig.toml
# DIO configuration stays independent of `bace.config`, which imports NumPy
# through `bace.core.axis`. `tools/shutter.py` must run with any interpreter
# that can load DELIB, including one without measurement dependencies.
# Only TOML parsing, DIO defaults and DIO validation are needed here.

DIO_DEFAULTS: dict[str, object] = {
    "module_id": 9,
    "shutter_module_nr": 0,
    "relay_module_nr": 1,
    "channel": 0,
    "dll_path": "",
}
"""Provide DIO defaults when a key or the file is absent.

These match `bace.config.RigConfig` and `bace.config.load_rig` without
importing their dependencies. `tests/test_shutter_tool.py` checks agreement
so a standalone tool cannot silently select a different line."""


class DioError(RuntimeError):
    """Report DIO settings that cannot safely select a hardware line."""


def find_rig(explicit: str | None = None) -> str | None:
    """Find `rig.toml` by explicit path, working directory or package directory.

    An explicit missing path raises: silently using defaults after a path
    typo can select hardware the operator does not intend."""
    if explicit:
        if not os.path.isfile(explicit):
            raise DioError(f"no such file: {explicit}")
        return explicit
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for base in (os.getcwd(), here):
        p = os.path.join(base, "rig.toml")
        if os.path.isfile(p):
            return p
    return None


def dio_settings(path: str | None) -> dict:
    """Return validated DIO settings with defaults, or raise `DioError`.

    Validation matches `bace.config.load_rig` so the service and standalone
    tools agree about the shutter line and reject the same unsafe settings."""
    import tomllib

    block: dict = {}
    if path:
        try:
            with open(path, "rb") as fh:
                block = tomllib.load(fh).get("dio", {}) or {}
        except tomllib.TOMLDecodeError as exc:
            raise DioError(f"{path}: {exc}") from exc
    try:
        cfg = {k: (str(block.get(k, v) or "") if isinstance(v, str)
                   else int(block.get(k, v)))
               for k, v in DIO_DEFAULTS.items()}
    except (TypeError, ValueError) as exc:
        raise DioError(f"[dio]: {exc}") from exc
    if cfg["shutter_module_nr"] == cfg["relay_module_nr"]:
        raise DioError(
            f"[dio] puts the shutter and the relay both on module "
            f"{cfg['shutter_module_nr']}. Driving the relay line as a shutter "
            "moves the device between the amplifier and the Keithley with no "
            "interlock in the way.")
    if cfg["relay_module_nr"] == 0:
        raise DioError(
            "[dio] relay_module_nr = 0: module 0 is the shutter (measured on "
            "the rig 2026-09-01), not the relay. The relay is module 1.")
    return cfg


def make_line(cfg: dict, *, module_nr: int | None = None, channel: int | None = None,
              dll_path: str | None = None, settle_s: float = 0.4):
    """Build a DIO line from settings without opening it.

    The caller owns errors from `open`. `Shutter` imports locally because
    it also imports this module for DLL discovery. The standalone console
    in `examples/shutter_console/` carries a separate `ctypes` implementation
    so it can run without the package."""
    from .shutter import DEFAULT_CHANNEL, Shutter

    return Shutter(dll_path or cfg["dll_path"] or None,
                   module_id=int(cfg["module_id"]),
                   module_nr=int(cfg["shutter_module_nr"] if module_nr is None
                                 else module_nr),
                   channel=DEFAULT_CHANNEL if channel is None else int(channel),
                   settle_s=settle_s)
