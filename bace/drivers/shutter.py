"""Control an optical shutter through a Deditec digital-output line.

The DLL calls match the LabVIEW `Dapi *.vi` interface:
`DapiOpenModule(ULONG moduleID, ULONG nr)` returns a `ULONG` handle;
`DapiDOSet1(ULONG handle, ULONG ch, ULONG data)` and `DapiDOSet8` return
no value; `DapiCloseModule(ULONG handle)` returns `ULONG`.

The bench uses module ID 9, module number 0, channel 0; 1 opens the
shutter and 0 shuts it (`docs/notes.md`).

The bench DLL at `D:/BACE/shutter/builds/data/delib.dll` is PE32,
machine 0x014C, and requires a 32-bit interpreter. `Shutter` can also load
a matching 64-bit DELIB installation through `bace.drivers.delib`.
`BridgedShutter` lets the 64-bit service use a 32-bit DLL through the helper.
The measurement core requires a 64-bit environment with SciPy wheels."""
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
    """Control one digital-output line for an optical shutter."""

    def __init__(self, dll_path: str | None = None, *,
                 module_id: int = DEFAULT_MODULE_ID,
                 module_nr: int = DEFAULT_MODULE_NR,
                 channel: int = DEFAULT_CHANNEL,
                 settle_s: float = 0.05):
        # `dll_path=None` selects a DLL matching this interpreter. Deditec's
        # 32-bit `delib.dll` and 64-bit `delib64.dll` share the API and argument
        # widths. `bace.drivers.delib` handles discovery across installations.
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
        except AttributeError:              # DELIB build without this export
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
        """Close the module handle without changing the output line; return `None`.

        `close` first drives low, which shuts the shutter. The same class drives
        the relay on another module; low moves it to the amplifier. That move
        requires the router interlock, including during shutdown, so the relay
        owner releases its handle without changing the line."""
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
        """Read the DIO line and return 0 or 1, or `None` if unavailable.

        `DapiDOReadback32(handle, ch)` returns 32 outputs starting at `ch`.
        Bit `channel` of a word read from 0 describes this line. Read-back is
        needed to restore a line's prior state: `_state` only records this
        object's writes and starts as `None`."""
        if self._handle is None or self._readback is None:
            return None
        try:
            word = int(self._readback(self._handle, 0))
        except Exception:
            return None
        return (word >> self.channel) & 1

    def set_line(self, value: int) -> None:
        """Drive the line with `DapiDOSet1` and return `None`.

        `unblock` and `shut` name the shutter states."""
        self._set(1 if value else 0)

    def unblock(self) -> None:
        """Drive the line high to open the shutter and return `None`."""
        self._set(OPEN)

    def shut(self) -> None:
        """Drive the line low to shut the shutter and return `None`."""
        self._set(CLOSED)

    @property
    def is_open(self) -> bool:
        return self._state == OPEN

    @contextmanager
    def dark(self):
        """Shut the shutter for the context and restore its preceding state.

        The light trace uses an open shutter; the dark trace uses this context."""
        was_open = self.is_open
        self.shut()
        try:
            yield
        finally:
            if was_open:
                self.unblock()


class BridgedShutter(Shutter):
    """Control a DIO line through the 32-bit helper process.

    The helper owns the DLL and handle. This class caches the state so
    `dark` and `is_open` follow the direct backend's interface."""

    def read_line(self) -> int | None:
        """Return cached state because the helper exposes no hardware read-back."""
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
    """Model a shutter line without a DLL or connected hardware."""

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
