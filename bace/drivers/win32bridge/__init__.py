"""Bridge a 64-bit service to instrument DLLs loaded by a 32-bit helper.

A bench installation can provide only a 32-bit DLL. The measurement core
uses a 64-bit environment for SciPy; the helper loads the DLL independently.
The service exchanges JSON lines with it over `127.0.0.1`. Shutter writes
and buffered power reads are low-rate operations, so instrument settling
dominates their round-trip time on this bench.

Matching 64-bit DELIB and Newport installations can use direct drivers;
the bridge is required only for DLLs that cannot load in the service."""
from .client import BridgeClient, BridgeError

__all__ = ["BridgeClient", "BridgeError"]
