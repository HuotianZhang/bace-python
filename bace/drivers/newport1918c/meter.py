#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Drive the Newport 1918-C optical power meter through its USB DLL.

All callers share `PowerMeter` because only one process can hold the device.
Newport Computer Interface Software v3.0.2 supplies `usbdll.dll` in 32-bit
and 64-bit builds. Ophir PMManager does not support this meter.

Bench firmware v2.1.7, SN10259, uses `PM:UNITS 2` for W; the other codes are
0 A, 1 V, 3 W/cm2, 4 J, 5 J/cm2, 6 dBm and 11 Sun. `PM:PWS?` returns a
value and hexadecimal status field. `PM:DS:INT` uses ms on this firmware,
not the 0.1 ms claimed by some drivers. Verify intervals on other hardware.

The meter echoes each command before its reply and terminates with CR.
`PowerMeter.query` strips the echo. The standalone diagnostic carries a
separate DLL layer for computers without this package; transport changes
must remain consistent with that diagnostic."""

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
    """Report a power-meter communication or configuration error."""


# --------------------------------------------------------------------------- #
# Locating the DLL
# --------------------------------------------------------------------------- #

def python_bitness() -> int:
    return struct.calcsize("P") * 8


def pe_bitness(path: str) -> int | None:
    """Return PE architecture as 32 or 64, or `None` if unreadable."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(0x400)
        pe_offset = int.from_bytes(header[0x3C:0x40], "little")
        machine = int.from_bytes(header[pe_offset + 4:pe_offset + 6], "little")
        return {0x014C: 32, 0x8664: 64}.get(machine)
    except Exception:  # noqa: BLE001
        return None


def find_dll(explicit: str | None = None) -> str | None:
    """Return the `usbdll.dll` matching this interpreter's bitness.

    The package installs both builds. Selecting by existence alone can load
    the wrong architecture and raise WinError 193."""
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
    """Decode the hexadecimal status field returned by `PM:PWS?`.

    Bits 9 to 7 encode units using `PM:UNITS?` codes; bits 6 to 4 encode
    range. Bit 3 marks detector presence; bit 2 marks ranging during the
    measurement; bit 1 marks detector saturation; bit 0 marks overrange.
    Units and range normally make the word nonzero. Only bits 0 to 2
    indicate a reading problem."""
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
    """Exchange ASCII commands and replies through Newport `usbdll.dll`."""

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
    """Model meter commands without hardware for console and driver checks.

    Replies include hexadecimal units and range status, store intervals
    in ms, and slowly drifting power with noise."""

    def __init__(self) -> None:
        self.description = "SIMULATED 1918-C"
        self.device_id = 0
        self.wavelength = 520
        self.units = 2
        self.range = 2
        self.auto = 0
        self.filt = 0
        self.digital = 0
        self.mode = 0
        self.analog = 0
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
            elif upper.startswith("PM:ANALOGFILTER "):
                self.analog = int(command.split()[-1])
            elif upper.startswith("PM:MODE "):
                self.mode = int(command.split()[-1])
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
            "PM:ANALOGFILTER": str(self.analog),
            "PM:MODE": str(self.mode),
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
    """Provide serialised access to one Newport 1918-C.

    An internal lock keeps background acquisition and request handlers from
    interleaving one caller's command with another caller's reply."""

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
        # The transport holds the exclusive USB handle. If identification raises,
        # close it before propagating the error. A failed constructor leaves no
        # object for the caller to close; retaining the handle blocks retries
        # and other owners for the service lifetime.
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
        mode = self._query_number("PM:MODE?")
        analog = self._query_number("PM:ANALOGFILTER?")
        return {
            # Mode and analog filter determine whether a pulsed LED reading is
            # mean power or a different quantity.
            "mode": mode,
            "modeName": MODE_NAMES.get(int(mode), "?") if mode is not None else "?",
            "analogFilter": analog,
            "analogFilterName": (ANALOG_FILTER_NAMES.get(int(analog), "?")
                                 if analog is not None else "?"),
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
        """Send `PM:Lambda` in whole nm and return wavelength read-back in nm.

        Values outside the detector calibration range raise. Extrapolating
        responsivity can give plausible wrong power values."""
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

    # A 500 Hz LED at 50 % duty needs DC-continuous mode and the 5 Hz analog
    # filter to read mean power. Other modes or unfiltered DC sample a phase
    # of the square wave. Mean power is half the DC level at 50 % duty
    # (manual 1.2, `docs/manual/1.2-bench.md`).

    def set_mode(self, code: int) -> None:
        """Send `PM:MODE` and return `None`.

        Code 0 selects DC-continuous power. `MODE_NAMES` lists the other modes."""
        self.write("PM:MODE %d" % int(code))

    def set_analog_filter(self, code: int) -> None:
        """Send `PM:ANALOGFILTER` and return `None`.

        Code 4 selects 5 Hz to average a 500 Hz pulse train.
        `ANALOG_FILTER_NAMES` lists the other bandwidths."""
        self.write("PM:ANALOGFILTER %d" % int(code))

    def zero(self) -> None:
        """Send `PM:ZEROSTO` to store the dark offset and return `None`.

        The beam must be blocked during this operation."""
        self.write("PM:ZEROSTO")

    def errors(self) -> str:
        return self.query("ERRSTR?")

    # -- reading ------------------------------------------------------------ #

    def read(self) -> dict:
        """Query `PM:PWS?` and return value, decoded status and raw reply.

        Units follow the meter setting. Status exposes saturation and overrange
        so clipping cannot silently enter a log. An unparseable value falls back
        to `PM:P?`, with status unavailable."""
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
        """Configure the store, acquire samples and return data and capture statistics.

        `interval_ms` is in ms on bench firmware v2.1.7. The meter clock sets
        the sampling interval; USB polling cannot guarantee it. `PM:DS:BUFFER 0`
        prevents wrap so the store holds one capture. `fetch=False` returns
        statistics without sample transfer. Time fields are in s except keys
        ending in `Ms`; values retain the meter's configured units."""
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
        """Query `PM:DS:GET?` in chunks and return numeric samples.

        The exact reply format is unspecified. Parse numeric tokens and ignore
        others; stop on no progress rather than retry an empty reply forever."""
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
    """Open the meter, read it and close it; return reading, settings and identity.

    `wavelength` is in nm. The value uses the configured units. No service
    or separate meter console may hold the USB device during this call."""
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
