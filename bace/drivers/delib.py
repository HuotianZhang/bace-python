"""Finding the DELIB DLL this interpreter can actually load.

Deditec ships the library under **two names**, and which one is present is a
property of the machine, not of the code:

* `delib.dll`   — 32-bit, installed into `C:\\Windows\\SysWOW64`
* `delib64.dll` — 64-bit, installed into `C:\\Windows\\System32`

`ctypes` cannot load the wrong one: a 64-bit interpreter given the 32-bit file
raises `OSError: [WinError 193] %1 is not a valid Win32 application`, which is
how the bench PC spent an evening unable to run `--dio` at all. So rather than
hard-coding a name, this module looks at what is installed, reads each
candidate's PE header, and returns the one whose machine type matches the
running interpreter.

The API is identical across the two. Deditec's own header (`delib.h`, DELIB
2009-2021) declares

    #ifdef _WIN64
      #define DAPI_FUNCTION_PRE64  extern "C" _declspec(dllexport)
    #else
      #define DAPI_FUNCTION_PRE64  extern
    #endif
    ...
    DAPI_FUNCTION_PRE64 ULONG DAPI_FUNCTION_PRE DapiOpenModule(ULONG moduleID, ULONG nr);
    DAPI_FUNCTION_PRE64 ULONG DAPI_FUNCTION_PRE DapiCloseModule(ULONG handle);
    DAPI_FUNCTION_PRE64 void  DAPI_FUNCTION_PRE DapiDOSet1(ULONG handle, ULONG ch, ULONG data);

so the x64 build exports **undecorated `extern "C"` names**, and every argument
and the handle stay `ULONG` — 32 bits on Windows in both builds. `c_ulong`
argtypes are therefore right for both, and `__stdcall` is moot on x64 where
there is only one calling convention. Nothing in `Shutter` changes with the
bitness except which file it opens.
"""
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

#: Also looked for beside the package and in the working directory, so a DLL
#: simply dropped next to the .bat files is found.
LOCAL_NAMES: tuple[str, ...] = ("delib64.dll", "delib.dll")

#: PE machine types, from the COFF header.
_MACHINE = {0x014C: 32, 0x8664: 64, 0xAA64: 64}


def interpreter_bits() -> int:
    return struct.calcsize("P") * 8


def pe_bitness(path: str) -> int | None:
    """32 or 64 from a Windows PE header, or None if it is not readable as one."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(0x400)
        offset = int.from_bytes(head[0x3C:0x40], "little")
        return _MACHINE.get(int.from_bytes(head[offset + 4:offset + 6], "little"))
    except Exception:
        return None


def survey(explicit: str | None = None) -> list[tuple[str, int | None]]:
    """Every candidate that exists, as `(path, bitness)`, search order preserved.

    `explicit` (from `rig.toml`, or `BACE_DELIB` in the environment) goes first,
    so a deliberate choice always wins over the installed defaults.
    """
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    local = [os.path.join(d, n) for d in (os.getcwd(), here) for n in LOCAL_NAMES]

    paths: list[str] = []
    for p in (explicit, os.environ.get("BACE_DELIB"), *SEARCH_PATHS, *local):
        if p and p not in paths:
            paths.append(p)
    return [(p, pe_bitness(p)) for p in paths if os.path.isfile(p)]


def bare_name() -> str:
    """What to hand `ctypes` when no file was found in the known places.

    Windows searches its own DLL path, so a library installed somewhere this
    module does not know about still loads by name -- and gets the bitness right
    by itself, since `System32` and `SysWOW64` are each redirected to the build
    matching the calling process.
    """
    return "delib64.dll" if interpreter_bits() == 64 else "delib.dll"


def load_candidates(explicit: str | None = None) -> list[str]:
    """Everything worth handing `ctypes`, best first.

    Files whose PE header matches come first, then files we could not read a
    header from, then the bare name as a last resort.
    """
    bits = interpreter_bits()
    found = survey(explicit)
    out = [p for p, machine in found if machine == bits]
    out += [p for p, machine in found if machine is None]
    out.append(bare_name())
    return out


def resolve(explicit: str | None = None) -> str | None:
    """The first candidate this interpreter can load, or None.

    A candidate whose PE header cannot be read is still offered -- `pe_bitness`
    returning None means "unrecognised file", not "wrong architecture", and
    letting `ctypes` have a go at it beats refusing on a header we failed to
    parse.
    """
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
# Reading the DIO half of rig.toml belongs here rather than in `bace.config`,
# which is the whole bench and reaches `core.axis` -- and so imports numpy.
# `tools/shutter.py`, which drives a DIO line by hand, has to run under
# whichever interpreter can load the DELIB build on the machine, and that one
# may have nothing installed at all. So: `tomllib`, the five keys, and the two
# refusals, with no other import.

DIO_DEFAULTS: dict[str, object] = {
    "module_id": 9,
    "shutter_module_nr": 0,
    "relay_module_nr": 1,
    "channel": 0,
    "dll_path": "",
}
"""What `[dio]` means when a key -- or the whole file -- is absent. The same
values `RigConfig` and `bace.config.load_rig` apply, repeated because neither
can be imported here; `tests/test_shutter_tool.py` asserts they still agree,
so drift fails a test rather than pointing a tool at the wrong line."""


class DioError(RuntimeError):
    """A `[dio]` block that will not be acted on."""


def find_rig(explicit: str | None = None) -> str | None:
    """rig.toml as `bench.checks._find` finds it: a named path, else the working
    directory, else beside the package.

    A *named* file that is missing is an error -- a typo in `--rig` that
    silently ran the built-in defaults would be a bench nobody described.
    """
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
    """The `[dio]` block with its defaults filled in, or `DioError`.

    The two refusals are the DIO half of `bace.config.load_rig`'s validation.
    A rig.toml the service refuses to start on must not be one a by-hand tool
    acts on: the point of the file is that every program on this bench agrees
    about which line is the shutter.
    """
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
    """One DIO line from a `[dio]` dict -- built, not opened.

    Beside `dio_settings` because they are read together: the block says which
    module, this turns it into a line, and the caller keeps its own error
    handling around `open()`. `Shutter` is imported inside rather than at
    module scope: it reaches back into this module for the library search.

    The standalone console in `examples/shutter_console/` deliberately does
    none of this -- it carries its own `ctypes`, so that folder can be copied
    to a machine without the package.
    """
    from .shutter import DEFAULT_CHANNEL, Shutter

    return Shutter(dll_path or cfg["dll_path"] or None,
                   module_id=int(cfg["module_id"]),
                   module_nr=int(cfg["shutter_module_nr"] if module_nr is None
                                 else module_nr),
                   channel=DEFAULT_CHANNEL if channel is None else int(channel),
                   settle_s=settle_s)
