"""Client side of the 32-bit bridge, used from the 64-bit service."""
from __future__ import annotations

import socket
import threading
from typing import Any

from .protocol import DEFAULT_HOST, DEFAULT_PORT, decode, encode


class BridgeError(RuntimeError):
    """Raised for transport failures and for errors reported by the helper."""


class BridgeClient:
    """Synchronous request/response over a single localhost connection.

    Thread-safe: calls are serialised, so the acquisition loop and a UI poll can
    share one client without interleaving replies.
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = 5.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: socket.socket | None = None
        self._rx: Any = None
        self._lock = threading.Lock()
        self._next_id = 0

    # -- lifecycle --------------------------------------------------------
    def connect(self) -> "BridgeClient":
        try:
            self._sock = socket.create_connection((self.host, self.port), self.timeout)
        except OSError as exc:
            raise BridgeError(
                f"cannot reach the 32-bit helper at {self.host}:{self.port} — "
                "is it running? (python -m bace.drivers.win32bridge.server, "
                "under a 32-bit interpreter)"
            ) from exc
        self._sock.settimeout(self.timeout)
        self._rx = self._sock.makefile("rb")
        return self

    def close(self) -> None:
        if self._rx is not None:
            self._rx.close()
            self._rx = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def __enter__(self) -> "BridgeClient":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- calls ------------------------------------------------------------
    def call(self, target: str, method: str, **args: Any) -> Any:
        if self._sock is None:
            raise BridgeError("bridge is not connected")
        with self._lock:
            self._next_id += 1
            req = {"id": self._next_id, "target": target, "method": method, "args": args}
            try:
                self._sock.sendall(encode(req))
                line = self._rx.readline()
            except OSError as exc:
                raise BridgeError(f"bridge transport failed during {target}.{method}") from exc
        if not line:
            raise BridgeError("the 32-bit helper closed the connection")
        resp = decode(line)
        if resp.get("id") != req["id"]:
            raise BridgeError(f"reply id {resp.get('id')} does not match request {req['id']}")
        if not resp.get("ok"):
            raise BridgeError(f"{target}.{method} failed in the helper: "
                              f"{resp.get('type', 'error')}: {resp.get('error')}")
        return resp.get("result")

    # -- convenience ------------------------------------------------------
    def ping(self) -> dict[str, Any]:
        return self.call("system", "info")
