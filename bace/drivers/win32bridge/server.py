"""32-bit helper process: owns delib.dll and Newport's usbdll.dll.

Run under a **32-bit** Python on the measurement PC:

    py -3.11-32 -m bace.drivers.win32bridge.server \
        --delib "D:/BACE/shutter/builds/data/delib.dll" \
        --newport "C:/Program Files (x86)/Newport/Newport USB Driver/Bin/usbdll.dll"

Either DLL may be omitted; its target then reports as unavailable rather than
taking the whole helper down. Binds to loopback only.
"""
from __future__ import annotations

import argparse
import ctypes
import platform
import socketserver
import struct
import sys
import threading
import traceback

from .protocol import DEFAULT_HOST, DEFAULT_PORT, decode, encode

# --------------------------------------------------------------------------
# Deditec shutter
# --------------------------------------------------------------------------


class DelibTarget:
    """Deditec digital output. Prototypes read from the Dapi *.vi Call Library
    Nodes:  ULONG DapiOpenModule(ULONG moduleID, ULONG nr);
            void  DapiDOSet1(ULONG handle, ULONG ch, ULONG data);
            ULONG DapiCloseModule(ULONG handle);"""

    def __init__(self, dll_path: str):
        self._lib = ctypes.WinDLL(dll_path)
        self._lib.DapiOpenModule.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
        self._lib.DapiOpenModule.restype = ctypes.c_ulong
        self._lib.DapiDOSet1.argtypes = [ctypes.c_ulong] * 3
        self._lib.DapiDOSet1.restype = None
        self._lib.DapiCloseModule.argtypes = [ctypes.c_ulong]
        self._lib.DapiCloseModule.restype = ctypes.c_ulong
        self._handle = 0
        self._lock = threading.Lock()

    def open(self, module_id: int = 9, module_nr: int = 0) -> int:
        with self._lock:
            if self._handle:
                return self._handle
            h = int(self._lib.DapiOpenModule(int(module_id), int(module_nr)))
            if h == 0:
                raise RuntimeError(
                    f"DapiOpenModule(id={module_id}, nr={module_nr}) returned 0 — "
                    "module not connected, or driver not installed"
                )
            self._handle = h
            return h

    def set(self, channel: int = 0, value: int = 0) -> int:
        with self._lock:
            if not self._handle:
                raise RuntimeError("module is not open")
            self._lib.DapiDOSet1(self._handle, int(channel), int(value))
            return int(value)

    def close(self) -> bool:
        with self._lock:
            if self._handle:
                self._lib.DapiCloseModule(self._handle)
                self._handle = 0
            return True


# --------------------------------------------------------------------------
# Newport 1918-C
# --------------------------------------------------------------------------


class NewportTarget:
    """Newport usbdll.dll. ASCII commands go through send/get, not VISA."""

    PRODUCT_ID = 0xCEC7

    def __init__(self, dll_path: str):
        self._lib = ctypes.WinDLL(dll_path)
        self._lib.newp_usb_open_devices.argtypes = [
            ctypes.c_int, ctypes.c_bool, ctypes.POINTER(ctypes.c_int)]
        self._lib.newp_usb_open_devices.restype = ctypes.c_int
        self._lib.newp_usb_send_ascii.argtypes = [
            ctypes.c_long, ctypes.c_char_p, ctypes.c_int]
        self._lib.newp_usb_send_ascii.restype = ctypes.c_int
        self._lib.newp_usb_get_ascii.argtypes = [
            ctypes.c_long, ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self._lib.newp_usb_get_ascii.restype = ctypes.c_int
        self._lib.newp_usb_uninit_system.argtypes = []
        self._lib.newp_usb_uninit_system.restype = ctypes.c_int
        self._device_id = 1
        self._open = False
        self._lock = threading.Lock()

    def open(self, device_id: int = 1) -> bool:
        with self._lock:
            n = ctypes.c_int(0)
            rc = self._lib.newp_usb_open_devices(self.PRODUCT_ID, False, ctypes.byref(n))
            if rc != 0 or n.value < 1:
                raise RuntimeError(
                    f"newp_usb_open_devices returned {rc}, {n.value} device(s) — "
                    "meter not connected, or the Newport USB driver is not installed"
                )
            self._device_id = int(device_id)
            self._open = True
            return True

    def write(self, command: str) -> int:
        with self._lock:
            return self._write_locked(command)

    def _write_locked(self, command: str) -> int:
        if not self._open:
            raise RuntimeError("meter is not open")
        payload = (command + "\r\n").encode("ascii")
        return int(self._lib.newp_usb_send_ascii(
            self._device_id, ctypes.c_char_p(payload), len(payload)))

    def query(self, command: str, buffer_size: int = 1024) -> str:
        with self._lock:
            self._write_locked(command)
            buf = ctypes.create_string_buffer(buffer_size)
            got = ctypes.c_int(0)
            self._lib.newp_usb_get_ascii(
                self._device_id, buf, buffer_size, ctypes.byref(got))
            return buf.value[:got.value].decode("ascii", "replace").strip()

    def power(self) -> float:
        return float(self.query("PM:Power?"))

    def close(self) -> bool:
        with self._lock:
            if self._open:
                self._lib.newp_usb_uninit_system()
                self._open = False
            return True


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------

TARGETS: dict[str, object] = {}


class _SystemTarget:
    def info(self) -> dict:
        return {
            "python": sys.version.split()[0],
            "bits": struct.calcsize("P") * 8,
            "machine": platform.machine(),
            "targets": sorted(k for k in TARGETS if k != "system"),
        }

    def ping(self) -> str:
        return "pong"


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        for line in self.rfile:
            line = line.strip()
            if not line:
                continue
            try:
                req = decode(line)
            except Exception as exc:
                self.wfile.write(encode({"id": None, "ok": False,
                                         "type": type(exc).__name__,
                                         "error": f"malformed request: {exc}"}))
                continue
            rid = req.get("id")
            try:
                target = TARGETS.get(req.get("target", ""))
                if target is None:
                    raise LookupError(f"no such target {req.get('target')!r}; "
                                      f"available: {sorted(TARGETS)}")
                name = req.get("method", "")
                if name.startswith("_") or not hasattr(target, name):
                    raise LookupError(f"no such method {name!r} on {req.get('target')!r}")
                result = getattr(target, name)(**(req.get("args") or {}))
                self.wfile.write(encode({"id": rid, "ok": True, "result": result}))
            except Exception as exc:
                traceback.print_exc()
                self.wfile.write(encode({"id": rid, "ok": False,
                                         "type": type(exc).__name__,
                                         "error": str(exc)}))


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--delib", help="path to delib.dll (Deditec shutter)")
    ap.add_argument("--newport", help="path to usbdll.dll (Newport 1918-C)")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args(argv)

    bits = struct.calcsize("P") * 8
    TARGETS["system"] = _SystemTarget()
    if a.delib:
        TARGETS["shutter"] = DelibTarget(a.delib)
    if a.newport:
        TARGETS["powermeter"] = NewportTarget(a.newport)

    print(f"bace 32-bit bridge — {bits}-bit Python {sys.version.split()[0]}")
    if bits != 32:
        print("  warning: this interpreter is not 32-bit; loading a 32-bit DLL will fail")
    print(f"  targets: {sorted(k for k in TARGETS if k != 'system')}")
    print(f"  listening on {a.host}:{a.port}  (Ctrl-C to stop)")

    with _Server((a.host, a.port), _Handler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nstopping")
    for name, t in TARGETS.items():
        if hasattr(t, "close"):
            try:
                t.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
