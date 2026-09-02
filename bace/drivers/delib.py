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
