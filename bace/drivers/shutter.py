"""Optical shutter on a Deditec USB digital-output module.

Driven through Deditec's `delib.dll`, exactly as the LabVIEW program did — the
`Dapi *.vi` wrappers add nothing but the Call Library Node, so `ctypes` talks to
the same entry points directly. The C prototypes below were read out of those
VIs' Call Library Nodes:

    ULONG DapiOpenModule(ULONG moduleID, ULONG nr);
    void  DapiDOSet1(ULONG handle, ULONG ch, ULONG data);
    void  DapiDOSet8(ULONG handle, ULONG ch, ULONG data);
    ULONG DapiCloseModule(ULONG handle);

Rig settings, confirmed against the current VI's front panel: one shutter on
module ID 9, module number 0, channel 0; 1 opens, 0 closes.

The copy on the rig — `D:\\BACE\\shutter\\builds\\data\\delib.dll` — is **32-bit**
(PE32, machine 0x014C). `Shutter` therefore only works in a 32-bit interpreter,
which cannot host the measurement core (SciPy stopped shipping win32 wheels at
Python 3.10). The 64-bit service uses `BridgedShutter` instead, which reaches the
same DLL through the 32-bit helper in `bace.drivers.win32bridge`.
"""
from __future__ import annotations

import ctypes
import struct
import time
from contextlib import contextmanager

DEFAULT_MODULE_ID = 9
DEFAULT_MODULE_NR = 0
DEFAULT_CHANNEL = 0

OPEN = 1
CLOSED = 0


class ShutterError(RuntimeError):
    pass


class Shutter:
    """One digital-output line driving the optical shutter."""

    def __init__(self, dll_path: str | None = None, *,
                 module_id: int = DEFAULT_MODULE_ID,
                 module_nr: int = DEFAULT_MODULE_NR,
                 channel: int = DEFAULT_CHANNEL,
                 settle_s: float = 0.05):
        # `dll_path=None` means "whichever build this interpreter can load".
        # Deditec ships 32-bit as `delib.dll` in SysWOW64 and 64-bit as
        # `delib64.dll` in System32; hard-coding either name makes the driver a
        # property of one machine. See `drivers/delib.py` -- the API and every
        # argument width are identical across the two.
        from .delib import load_candidates
        opener = ctypes.WinDLL if hasattr(ctypes, "WinDLL") else ctypes.CDLL
        tried: list[str] = []
        lib = None
        for path in ([dll_path] if dll_path else load_candidates()):
            try:
                lib = opener(path)
            except OSError as exc:
                tried.append(f"{path} ({exc})")
                continue
            self.dll_path = path
            break
        if lib is None:
            raise ShutterError(
                f"no DELIB library this {struct.calcsize('P') * 8}-bit "
                "interpreter can load. Install the matching DELIB (64-bit "
                "installs as delib64.dll in System32, 32-bit as delib.dll in "
                "SysWOW64), or set `dll_path` under [dio] in rig.toml — or "
                "BACE_DELIB in the environment. Tried: " + "; ".join(tried)
            )
        self._lib = lib
        self._lib.DapiOpenModule.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
        self._lib.DapiOpenModule.restype = ctypes.c_ulong
        self._lib.DapiDOSet1.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        self._lib.DapiDOSet1.restype = None
        self._lib.DapiCloseModule.argtypes = [ctypes.c_ulong]
        self._lib.DapiCloseModule.restype = ctypes.c_ulong
        try:
            self._lib.DapiDOReadback32.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
            self._lib.DapiDOReadback32.restype = ctypes.c_ulong
            self._readback = self._lib.DapiDOReadback32
        except AttributeError:              # older DELIB without the export
            self._readback = None

        self.module_id = module_id
        self.module_nr = module_nr
        self.channel = channel
        self.settle_s = settle_s
        self._handle: int | None = None
        self._state: int | None = None

    # -- lifecycle --------------------------------------------------------
    def open(self) -> "Shutter":
        handle = int(self._lib.DapiOpenModule(self.module_id, self.module_nr))
        if handle == 0:
            raise ShutterError(
                f"DapiOpenModule(id={self.module_id}, nr={self.module_nr}) returned 0 — "
                "module not found, or delib.dll bitness does not match this interpreter"
            )
        self._handle = handle
        return self

    def close(self) -> None:
        if self._handle is not None:
            try:
                self.shut()
            finally:
                self._lib.DapiCloseModule(self._handle)
                self._handle = None

    def release(self) -> None:
        """Close the module handle and leave the line where it is.

        `close()` drives the line low first, which for the shutter is the
        safe state. The relay is driven through this same class on another
        module number, and low there is a relay throw to the amplifier --
        the one move on this rig that must go through the router's
        interlock, never through a shutdown path. Its owner releases the
        handle with this instead.
        """
        if self._handle is not None:
            self._lib.DapiCloseModule(self._handle)
            self._handle = None

    def __enter__(self) -> "Shutter":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- control ----------------------------------------------------------
    def _set(self, value: int) -> None:
        if self._handle is None:
            raise ShutterError("shutter is not open")
        if self._state == value:
            return
        self._lib.DapiDOSet1(self._handle, self.channel, value)
        self._state = value
        if self.settle_s:
            time.sleep(self.settle_s)

    def read_line(self) -> int | None:
        """The line's state as the module reports it, or None if it cannot say.

        `DapiDOReadback32(handle, ch)` returns a word of 32 digital outputs
        starting at `ch` (delib.h, DELIB 2009-2021), so bit `channel` of the
        word read from 0 is this line. Asking the hardware matters where a
        caller has to put a line back where it found it -- the cached `_state`
        only knows what *this* object has set, and is None on a fresh open.
        """
        if self._handle is None or self._readback is None:
            return None
        try:
            word = int(self._readback(self._handle, 0))
        except Exception:
            return None
        return (word >> self.channel) & 1

    def set_line(self, value: int) -> None:
        """Drive the line directly. `unblock`/`shut` are the named forms."""
        self._set(1 if value else 0)

    def unblock(self) -> None:
        """Open the shutter — light reaches the sample."""
        self._set(OPEN)

    def shut(self) -> None:
        """Close the shutter — the dark trace."""
        self._set(CLOSED)

    @property
    def is_open(self) -> bool:
        return self._state == OPEN

    @contextmanager
    def dark(self):
        """Close for the duration of the block, then restore the previous state.

        Used per acquisition step: the light trace is taken with the shutter
        open, the dark trace inside this block.
        """
        was_open = self.is_open
        self.shut()
        try:
            yield
        finally:
            if was_open:
                self.unblock()


class BridgedShutter(Shutter):
    """Same interface as `Shutter`, driven through the 32-bit helper process.

    This is what the 64-bit service uses. The helper owns the DLL and the module
    handle; this class holds only the cached state, so `dark()` and `is_open`
    behave identically to the direct backend.
    """

    def read_line(self) -> int | None:
        """The helper does not expose a readback; the cached state is all there is."""
        return self._state

    def __init__(self, client, *,
                 module_id: int = DEFAULT_MODULE_ID,
                 module_nr: int = DEFAULT_MODULE_NR,
                 channel: int = DEFAULT_CHANNEL,
                 settle_s: float = 0.05):
        self._client = client
        self.module_id = module_id
        self.module_nr = module_nr
        self.channel = channel
        self.settle_s = settle_s
        self._handle = None
        self._state = None

    def open(self) -> "BridgedShutter":
        self._handle = self._client.call("shutter", "open",
                                         module_id=self.module_id,
                                         module_nr=self.module_nr)
        return self

    def close(self) -> None:
        if self._handle is not None:
            try:
                self.shut()
            finally:
                self._client.call("shutter", "close")
                self._handle = None

    def release(self) -> None:
        if self._handle is not None:
            self._client.call("shutter", "close")
            self._handle = None

    def _set(self, value: int) -> None:
        if self._handle is None:
            raise ShutterError("shutter is not open")
        if self._state == value:
            return
        self._client.call("shutter", "set", channel=self.channel, value=value)
        self._state = value
        if self.settle_s:
            time.sleep(self.settle_s)


class SimulatedShutter(Shutter):
    """Stand-in for offline work — no DLL, no hardware."""

    def read_line(self) -> int | None:
        return self._state

    def __init__(self, **kw):
        self.module_id = kw.get("module_id", DEFAULT_MODULE_ID)
        self.module_nr = kw.get("module_nr", DEFAULT_MODULE_NR)
        self.channel = kw.get("channel", DEFAULT_CHANNEL)
        self.settle_s = 0.0
        self._handle = None
        self._state = None

    def open(self) -> "SimulatedShutter":
        self._handle = 1
        return self

    def close(self) -> None:
        self._handle = None

    def release(self) -> None:
        self._handle = None

    def _set(self, value: int) -> None:
        self._state = value
