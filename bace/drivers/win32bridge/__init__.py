"""Bridge to 32-bit-only instrument DLLs from a 64-bit service.

Two of the rig's instruments are reached through vendor DLLs that exist only as
32-bit builds — Deditec's `delib.dll` (the shutter) and Newport's `usbdll.dll`
(the 1918-C power meter). A 32-bit Python cannot be the answer: SciPy stopped
publishing win32 wheels at Python 3.10, so the measurement core could not be
installed there.

So the service stays 64-bit and the DLLs live in a small 32-bit helper process:

    64-bit service  --(JSON lines over 127.0.0.1)-->  32-bit helper  --> DLLs

Both devices are low-rate — a shutter toggle and a buffered power read — so the
round trip costs nothing measurable next to instrument settling times.
"""
from .client import BridgeClient, BridgeError

__all__ = ["BridgeClient", "BridgeError"]
