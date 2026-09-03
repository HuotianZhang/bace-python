#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
newport1918c.py -- driver for the Newport 1918-C optical power meter.

VENDORED, 2026-09-03, from the meter's console project (D:/1918cPowerMeter),
unchanged except for the two flag names at the bottom of this file. It is the
device truth, written against real hardware; when this file and that project
disagree, they have drifted and one of them is wrong. In this repository the
one process that holds the meter is the BACE service, so "the console" in the
prose below means whichever program owns the device -- here, us.

The single place in this project that touches the instrument. Everything else
(the service, any measurement script) goes through PowerMeter, because only
one process can hold the USB device at a time.

Transport is Newport's usbdll.dll, installed by Computer Interface Software
v3.0.2 -- NOT PMManager, which is the newer Ophir-generation application and
does not support this meter.

Facts below were measured on a 1918-C v2.1.7 (SN10259), not assumed:
  * PM:UNITS 2 is watts (0 A, 1 V, 2 W, 3 W/cm2, 4 J, 5 J/cm2, 6 dBm, 11 Sun).
  * PM:PWS? returns "<value>, <status>" where status is a HEX bitfield.
  * PM:DS:INT is in MILLISECONDS on this firmware. A widely-copied driver
    claims 0.1 ms units; that is wrong here. Re-measure on other hardware.

probe_1918c.py is the standalone first-contact diagnostic and carries its own
copy of the ctypes layer so it can be dropped onto a bare PC by itself. This
module is the library the console and measurement code import. If you change
the transport, change it in both.
"""

from __future__ import annotations

import os
import platform
import struct
import sys
import threading
import time

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

NEWPORT_VID = 0x104D
PID_1918C = 0xCEC7

DLL_CANDIDATES = (
    r"C:\Program Files\Newport\Newport USB Driver\Bin\usbdll.dll",
    r"C:\Program Files\Newport\Newport USB Driver\Bin\x86\usbdll.dll",
    r"C:\Program Files\Newport\Newport USB Driver\Bin\x64\usbdll.dll",
    r"C:\Program Files\Newport\Newport USB Driver\Bin\Win64\usbdll.dll",
    r"C:\Program Files (x86)\Newport\Newport USB Driver\Bin\usbdll.dll",
    r"C:\Program Files (x86)\Newport\Newport USB Driver\Bin\x86\usbdll.dll",
    r"C:\Program Files (x86)\Newport\Newport USB Driver\Bin\x64\usbdll.dll",
)

UNITS_NAMES = {0: "A", 1: "V", 2: "W", 3: "W/cm2", 4: "J", 5: "J/cm2",
               6: "dBm", 11: "Sun"}

MODE_NAMES = {0: "DC continuous", 1: "DC single", 2: "integrate",
              3: "peak-to-peak continuous", 4: "peak-to-peak single",
              5: "pulse continuous", 6: "pulse single", 7: "RMS"}

FILTER_NAMES = {0: "none", 1: "analog", 2: "digital", 3: "analog + digital"}

ANALOG_FILTER_NAMES = {0: "none", 1: "250 kHz", 2: "12.5 kHz", 3: "1 kHz", 4: "5 Hz"}

TERMINATOR = "\r"


class MeterError(RuntimeError):
    """Anything that went wrong talking to the meter."""


# --------------------------------------------------------------------------- #
# Locating the DLL
# --------------------------------------------------------------------------- #

def python_bitness() -> int:
    return struct.calcsize("P") * 8


def pe_bitness(path: str) -> int | None:
    """Read a PE file's machine type. Returns 32, 64 or None."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(0x400)
        pe_offset = int.from_bytes(header[0x3C:0x40], "little")
        machine = int.from_bytes(header[pe_offset + 4:pe_offset + 6], "little")
        return {0x014C: 32, 0x8664: 64}.get(machine)
    except Exception:  # noqa: BLE001
        return None


def find_dll(explicit: str | None = None) -> str | None:
    """Return the usbdll.dll matching this interpreter's bitness.

    The driver package installs both builds, so taking the first path that
    exists picks the wrong one half the time and yields a bare WinError 193.
    """
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    present = [p for p in DLL_CANDIDATES if os.path.isfile(p)]
    want = python_bitness()
    for path in present:
        if pe_bitness(path) == want:
            return path
    return None


# --------------------------------------------------------------------------- #
# Status word
# --------------------------------------------------------------------------- #

def decode_status(word: str) -> dict:
    """Decode the status field of a PM:PWS? reply.

    It is a bitfield in HEXADECIMAL:
        bits 9-7  units   (PM:UNITS? codes)
        bits 6-4  range   (PM:RANge? index)
        bit  3    detector present
        bit  2    measurement taken while ranging
        bit  1    detector saturated
        bit  0    overrange
    A large non-zero status is normal -- the units and range fields alone are
    usually non-zero. Only bits 0-2 indicate a problem.
    """
    try:
        value = int(str(word).strip(), 16)
    except (ValueError, TypeError):
        return {"raw": word, "ok": False, "error": "unparsable"}
    units = (value >> 7) & 0b111
    return {
        "raw": "0x%X" % value,
        "ok": True,
        "units": units,
        "unitsName": UNITS_NAMES.get(units, "?"),
        "range": (value >> 4) & 0b111,
        "detector": bool(value & 0b1000),
        "ranging": bool(value & 0b100),
        "saturated": bool(value & 0b10),
        "overrange": bool(value & 0b1),
        "healthy": bool(value & 0b1000) and not (value & 0b111),
    }


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #

class DllTransport:
    """ctypes wrapper over Newport's usbdll.dll."""

    def __init__(self, dll_path: str, product_id: int = PID_1918C):
        import ctypes
        from ctypes import c_bool, c_int, c_long, c_ulong

        self._ctypes = ctypes
        self.dll_path = dll_path
        self.product_id = product_id
        self.device_id: int | None = None
        self.description = ""
        self._open = False

        if not hasattr(ctypes, "WinDLL"):
            raise MeterError("usbdll.dll can only be loaded on Windows")
        try:
            self.lib = ctypes.WinDLL(dll_path)
        except OSError as exc:
            hint = ""
            if "193" in str(exc):
                hint = (" -- bitness mismatch: this Python is %d-bit and that DLL is not"
                        % python_bitness())
            raise MeterError("could not load %s: %s%s" % (dll_path, exc, hint))

        lib = self.lib
        lib.newp_usb_init_system.restype = c_long
        lib.newp_usb_init_system.argtypes = []
        lib.newp_usb_uninit_system.restype = None
        lib.newp_usb_uninit_system.argtypes = []
        lib.newp_usb_open_devices.restype = c_long
        lib.newp_usb_open_devices.argtypes = [c_int, c_bool, ctypes.POINTER(c_int)]
        lib.newp_usb_get_device_info.restype = c_long
        lib.newp_usb_get_device_info.argtypes = [ctypes.c_char_p]
        lib.GetInstrumentList.restype = c_long
        lib.GetInstrumentList.argtypes = [ctypes.POINTER(c_int)] * 4
        lib.newp_usb_send_ascii.restype = c_long
        lib.newp_usb_send_ascii.argtypes = [c_long, ctypes.c_char_p, c_ulong]
        lib.newp_usb_get_ascii.restype = c_long
        lib.newp_usb_get_ascii.argtypes = [
            c_long, ctypes.c_char_p, c_ulong, ctypes.POINTER(c_ulong)]

    # -- open / close ------------------------------------------------------- #

    def open(self) -> None:
        from ctypes import byref, c_bool, c_int, create_string_buffer

        self.lib.newp_usb_init_system()
        buf = create_string_buffer(2048)
        self.lib.newp_usb_get_device_info(buf)
        info = buf.value.decode("ascii", errors="replace").strip()
        if info:
            first = info.replace(";", "\n").splitlines()[0]
            ident, _, desc = first.partition(",")
            try:
                self.device_id = int(ident.strip())
                self.description = desc.strip()
                self._open = True
                return
            except ValueError:
                pass

        self.lib.newp_usb_uninit_system()
        num = c_int(0)
        self.lib.newp_usb_open_devices(c_int(self.product_id), c_bool(True), byref(num))
        if num.value < 1:
            raise MeterError(
                "no Newport device found. Check the meter is powered and plugged in, "
                "and that nothing else holds it -- only one process can.")
        size = c_int(32)
        instruments = (c_int * 32)()
        models = (c_int * 32)()
        serials = (c_int * 32)()
        self.lib.GetInstrumentList(instruments, models, serials, byref(size))
        if size.value < 1:
            raise MeterError("the driver opened a device but listed none")
        self.device_id = int(instruments[0])
        self.description = "model %d serial %d" % (models[0], serials[0])
        self._open = True

    def close(self) -> None:
        if self._open:
            try:
                self.lib.newp_usb_uninit_system()
            finally:
                self._open = False

    # -- traffic ------------------------------------------------------------ #

    def write(self, command: str) -> None:
        from ctypes import c_long, c_ulong, create_string_buffer, sizeof
        if self.device_id is None:
            raise MeterError("no device open")
        payload = create_string_buffer((command + TERMINATOR).encode("ascii"))
        status = self.lib.newp_usb_send_ascii(
            c_long(self.device_id), payload, c_ulong(sizeof(payload)))
        if status != 0:
            raise MeterError("send failed for %r (status %d)" % (command, status))

    def read(self, timeout: float = 1.0) -> str:
        from ctypes import byref, c_long, c_ulong, create_string_buffer
        if self.device_id is None:
            raise MeterError("no device open")
        deadline = time.time() + timeout
        buf = create_string_buffer(4096)
        nread = c_ulong(0)
        while time.time() < deadline:
            self.lib.newp_usb_get_ascii(
                c_long(self.device_id), buf, c_ulong(4096), byref(nread))
            if nread.value:
                return buf.raw[:nread.value].decode("ascii", errors="replace").strip("\r\n\0 ")
            time.sleep(0.005)
        raise MeterError("timed out waiting for a reply")


class SimulatedTransport:
    """A fake meter, so the console can be developed and demonstrated dry.

    Values mirror the real instrument closely enough to be useful: hex status
    word built from its own units and range, PM:DS:INT in milliseconds, a
    slowly drifting power with noise on top.
    """

    def __init__(self) -> None:
        self.description = "SIMULATED 1918-C"
        self.device_id = 0
        self.wavelength = 520
        self.units = 2
        self.range = 2
        self.auto = 0
        self.filt = 0
        self.digital = 0
        self._t0 = time.time()
        self._ds = {"size": 0, "int": 10, "start": None, "collected": 0, "served": 0}
        self._zero = 0.0

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def _power(self) -> float:
        import math
        import random
        drift = 1.0 + 0.03 * math.sin((time.time() - self._t0) / 11.0)
        noise = random.gauss(0, 0.004)
        return max(0.0, 1.85e-4 * drift * (1 + noise) - self._zero)

    def write(self, command: str) -> None:
        upper = command.upper().strip()
        try:
            if upper.startswith("PM:LAMBDA "):
                self.wavelength = int(float(command.split()[-1]))
            elif upper.startswith("PM:UNITS "):
                self.units = int(command.split()[-1])
            elif upper.startswith("PM:RAN"):
                self.range = int(command.split()[-1])
            elif upper.startswith("PM:AUTO "):
                self.auto = int(command.split()[-1])
            elif upper.startswith("PM:DIGITALFILTER "):
                self.digital = int(command.split()[-1])
            elif upper.startswith("PM:FILT "):
                self.filt = int(command.split()[-1])
            elif upper.startswith("PM:ZEROSTO"):
                self._zero = self._power()
            elif upper.startswith("PM:DS:SIZE"):
                self._ds["size"] = int(command.split()[-1])
            elif upper.startswith("PM:DS:INT"):
                self._ds["int"] = max(1, int(command.split()[-1]))
            elif upper.startswith("PM:DS:CLEAR"):
                self._ds.update(start=None, collected=0, served=0)
            elif upper.startswith("PM:DS:EN"):
                if command.split()[-1] == "1":
                    self._ds.update(start=time.time(), collected=0, served=0)
                else:
                    self._ds_count()          # freeze the count
                    self._ds["start"] = None
        except (ValueError, IndexError):
            pass

    def _ds_count(self) -> int:
        if self._ds["start"] is None:
            return self._ds["collected"]
        elapsed = time.time() - self._ds["start"]
        count = min(int(elapsed / (self._ds["int"] * 1e-3)), self._ds["size"])
        self._ds["collected"] = count
        return count

    def read(self, timeout: float = 1.0) -> str:  # noqa: ARG002 - same signature
        return self._pending

    def query(self, command: str) -> str:
        upper = command.upper().rstrip("?").strip()
        table = {
            "*IDN": "NEWPORT 1918-C v2.1.7 09/05/08 SN00000 SIMULATED",
            "PM:DETSN": "10642",
            "PM:LAMBDA": str(self.wavelength),
            "PM:MIN:LAMBDA": "200",
            "PM:MAX:LAMBDA": "1100",
            "PM:UNITS": str(self.units),
            "PM:AUTO": str(self.auto),
            "PM:RAN": str(self.range),
            "PM:MAX:POWER": "0.000838",
            "PM:FILT": str(self.filt),
            "PM:DIGITALFILTER": str(self.digital),
            "ERRSTR": "0,No Error",
        }
        if upper in table:
            return table[upper]
        if upper == "PM:DS:COUNT":
            return str(self._ds_count())
        if upper.startswith("PM:STAT:"):
            base = self._power()
            what = upper.split(":")[-1]
            return {"MEAN": "%.6E" % base, "SDEV": "%.6E" % (base * 0.004),
                    "MAX": "%.6E" % (base * 1.012), "MIN": "%.6E" % (base * 0.988),
                    }.get(what, "0")
        if upper.startswith("PM:DS:GET"):
            asked = 10
            for token in upper.replace("+", " ").split():
                if token.isdigit():
                    asked = int(token)
            remaining = max(0, self._ds["collected"] - self._ds["served"])
            give = min(asked, remaining) if remaining else 0
            self._ds["served"] += give
            return ",".join("%.6E" % self._power() for _ in range(give))
        if upper in ("PM:P", "PM:POWER"):
            return "%.6E" % self._power()
        if upper == "PM:PWS":
            status = (self.units << 7) | (self.range << 4) | 0b1000
            return "%.6E, %X" % (self._power(), status)
        return "0"


# --------------------------------------------------------------------------- #
# The meter
# --------------------------------------------------------------------------- #

class PowerMeter:
    """High-level, thread-safe access to one 1918-C.

    Every method serialises on an internal lock, so a background acquisition
    thread and an HTTP handler can share one instance without interleaving
    a command with somebody else's reply.
    """

    def __init__(self, transport):
        self._t = transport
        self._lock = threading.RLock()
        self.info: dict = {}

    # -- construction ------------------------------------------------------- #

    @classmethod
    def open(cls, dll: str | None = None, simulate: bool = False) -> "PowerMeter":
        if simulate:
            transport = SimulatedTransport()
        else:
            path = find_dll(dll)
            if path is None:
                raise MeterError(
                    "usbdll.dll not found (or none matching this %d-bit Python). "
                    "Install Newport Computer Interface Software v3.0.2, or run "
                    "with --sim." % python_bitness())
            transport = DllTransport(path)
        transport.open()
        # DIVERGES from the console project, 2026-09-03: identification runs
        # inside try/finally there and did not here. `transport.open()` has
        # taken the *exclusive* USB handle by this point, so an `identify()`
        # that times out left the device held by a process that has no object
        # for it -- unavailable to a retry, to the meter's own console, and to
        # anything else until the interpreter exits. That mattered little in a
        # console which would be restarted; the BACE service is long-lived, so
        # one bad query at start-up would have cost the meter for the session.
        try:
            meter = cls(transport)
            meter.info = meter.identify()
        except BaseException:
            try:
                transport.close()
            except Exception:                       # noqa: BLE001
                pass        # the open failure is the one worth reporting
            raise
        return meter

    def close(self) -> None:
        with self._lock:
            self._t.close()

    # -- primitives --------------------------------------------------------- #

    def query(self, command: str, timeout: float = 1.0) -> str:
        with self._lock:
            if hasattr(self._t, "query"):          # simulated transport
                return self._t.query(command)
            self._t.write(command)
            answer = self._t.read(timeout)
            if answer.upper().startswith(command.upper()):
                answer = answer[len(command):].strip("\r\n ")
            return answer

    def write(self, command: str) -> None:
        with self._lock:
            self._t.write(command)

    def _query_number(self, command: str, default=None):
        try:
            return float(self.query(command))
        except (MeterError, ValueError):
            return default

    # -- identity and settings ---------------------------------------------- #

    def identify(self) -> dict:
        return {
            "idn": self.query("*IDN?"),
            "detector": self.query("PM:DETSN?"),
            "lambdaMin": self._query_number("PM:MIN:Lambda?"),
            "lambdaMax": self._query_number("PM:MAX:Lambda?"),
            "transport": getattr(self._t, "description", ""),
            "simulated": isinstance(self._t, SimulatedTransport),
        }

    def settings(self) -> dict:
        units = self._query_number("PM:UNITS?")
        filt = self._query_number("PM:FILT?")
        return {
            "wavelength": self._query_number("PM:Lambda?"),
            "units": units,
            "unitsName": UNITS_NAMES.get(int(units), "?") if units is not None else "?",
            "auto": self._query_number("PM:AUTO?"),
            "range": self._query_number("PM:RAN?"),
            "fullScale": self._query_number("PM:MAX:Power?"),
            "filter": filt,
            "filterName": FILTER_NAMES.get(int(filt), "?") if filt is not None else "?",
            "digitalFilter": self._query_number("PM:DIGITALFILTER?"),
        }

    def set_wavelength(self, nm: float) -> dict:
        """Set wavelength, refusing values outside the head's calibration.

        Outside the calibrated range the responsivity is an extrapolation, so
        the meter would return a confident, wrong number.
        """
        target = int(round(nm))
        low = self.info.get("lambdaMin")
        high = self.info.get("lambdaMax")
        if low is not None and high is not None and not (low <= target <= high):
            raise MeterError(
                "%d nm is outside this detector's calibrated range %g-%g nm"
                % (target, low, high))
        self.write("PM:Lambda %d" % target)
        time.sleep(0.05)
        return {"wavelength": self._query_number("PM:Lambda?")}

    def set_units(self, code: int) -> None:
        self.write("PM:UNITS %d" % int(code))

    def set_auto(self, on: bool) -> None:
        self.write("PM:AUTO %d" % (1 if on else 0))

    def set_range(self, index: int) -> None:
        self.write("PM:AUTO 0")
        self.write("PM:RAN %d" % int(index))

    def set_filter(self, filt: int, digital: int | None = None) -> None:
        self.write("PM:FILT %d" % int(filt))
        if digital is not None:
            self.write("PM:DIGITALFILTER %d" % int(digital))

    def zero(self) -> None:
        """Store the dark offset. The beam must be blocked when this runs."""
        self.write("PM:ZEROSTO")

    def errors(self) -> str:
        return self.query("ERRSTR?")

    # -- reading ------------------------------------------------------------ #

    def read(self) -> dict:
        """One reading, with the status word decoded.

        PM:PWS? is preferred over PM:P? because it carries the status field --
        silently clipped data is the classic way to ruin a long log.
        """
        raw = self.query("PM:PWS?")
        value = None
        status = None
        if "," in raw:
            head, _, tail = raw.partition(",")
            try:
                value = float(head)
                status = decode_status(tail)
            except ValueError:
                value = None
        if value is None:
            try:
                value = float(self.query("PM:P?"))
            except (MeterError, ValueError) as exc:
                raise MeterError("could not read power: %s" % exc)
        return {"t": time.time(), "value": value, "status": status, "raw": raw}

    # -- buffered capture --------------------------------------------------- #

    def capture(self, size: int, interval_ms: int, fetch: bool = True,
                progress=None) -> dict:
        """Fill the instrument's data store and read it back.

        PM:DS:INT is in milliseconds on firmware v2.1.7 -- measured, not
        assumed. Sampling happens on the meter's own clock, which is the whole
        point: a polled loop cannot give a deterministic rate over USB.
        """
        interval_ms = max(1, int(interval_ms))
        size = max(1, int(size))
        self.write("PM:DS:CLEAR")
        self.write("PM:DS:SIZE %d" % size)
        self.write("PM:DS:INT %d" % interval_ms)
        self.write("PM:DS:BUFFER 0")           # fill once, do not wrap
        started = time.time()
        self.write("PM:DS:EN 1")

        count = 0
        expected = size * interval_ms / 1000.0
        deadline = started + expected * 3 + 10
        while count < size and time.time() < deadline:
            try:
                count = int(float(self.query("PM:DS:COUNT?")))
            except (MeterError, ValueError):
                break
            if progress:
                progress(count, size)
            time.sleep(min(0.05, max(0.005, expected / 50)))
        elapsed = time.time() - started
        try:
            self.write("PM:DS:EN 0")
        except MeterError:
            pass

        result = {
            "requested": size,
            "collected": count,
            "intervalMs": interval_ms,
            "elapsed": elapsed,
            "measuredMsPerSample": (elapsed / count * 1000.0) if count else None,
            "mean": self._query_number("PM:STAT:MEAN?"),
            "sdev": self._query_number("PM:STAT:SDEV?"),
            "max": self._query_number("PM:STAT:MAX?"),
            "min": self._query_number("PM:STAT:MIN?"),
            "samples": [],
        }
        if fetch and count:
            result["samples"] = self._fetch_samples(count)
        try:
            self.write("PM:DS:CLEAR")
        except MeterError:
            pass
        return result

    def _fetch_samples(self, count: int, chunk: int = 100) -> list:
        """Pull the buffer back in chunks.

        The reply format is not documented precisely, so parse permissively:
        take every token that looks like a number and ignore the rest.
        """
        samples: list[float] = []
        # Loop on actual progress, not on the number asked for: if the meter
        # returns fewer values than requested, asking again for the remainder
        # is right, and a reply with no numbers at all ends it rather than
        # spinning.
        while len(samples) < count:
            take = min(chunk, count - len(samples))
            try:
                reply = self.query("PM:DS:GET? +%d" % take, timeout=5.0)
            except MeterError:
                break
            found = 0
            for token in reply.replace("\r", " ").replace("\n", " ").split(","):
                token = token.strip()
                if not token:
                    continue
                try:
                    samples.append(float(token))
                    found += 1
                except ValueError:
                    continue
            if not found:
                break
        return samples[:count]


# --------------------------------------------------------------------------- #
# Convenience for measurement scripts
# --------------------------------------------------------------------------- #

def read_once(dll: str | None = None, simulate: bool = False,
              wavelength: float | None = None) -> dict:
    """Open, read, close. For a script that wants a single number.

    Only use this when nothing else holds the meter -- not while the service
    is up, which owns the device, and not while the meter's own console is
    running, which owns it instead.
    """
    meter = PowerMeter.open(dll=dll, simulate=simulate)
    try:
        if wavelength is not None:
            meter.set_wavelength(wavelength)
        reading = meter.read()
        reading["settings"] = meter.settings()
        reading["info"] = meter.info
        return reading
    finally:
        meter.close()


if __name__ == "__main__":
    simulate = "--sim" in sys.argv or "--simulate" in sys.argv
    print("newport1918c on %s, Python %s (%d-bit)"
          % (platform.system(), platform.python_version(), python_bitness()))
    data = read_once(simulate=simulate)
    print("idn      :", data["info"]["idn"])
    print("settings :", data["settings"])
    print("reading  : %.6g %s" % (data["value"], data["settings"]["unitsName"]))
    print("status   :", data["status"])
