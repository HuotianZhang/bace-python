"""Check the bench in stages that expose increasing hardware risk.

The offline, discover and local stages run by default. Other stages are opt-in.
Stages that move hardware need a typed confirmation unless `--yes` skips it.

    offline     check the package without instruments
    discover    enumerate VISA and query `*IDN?`; read-only
    local       check DELIB, the 1918-C and the 331
    read        read back instrument settings; no settings sent
    configure   send configuration commands; outputs stay off
    acquire     acquire one scope record; a running trigger is required
    dio         toggle shutter and relay lines; use a connected device and LED
    outputs     enable outputs; disconnect the device first
    measure     acquire a transient with a device connected

`HYPOTHESIS:` marks assumptions under test. Instrument disagreement produces a
warning rather than a failure where the assumption remains uncertain.
`--force-configure` permits configuration of live outputs only with no device
connected.
"""
from __future__ import annotations

import contextlib
import os
import struct
import tempfile
import time
from datetime import datetime
from typing import Any

from .report import Check, Report
from .session import RecordingResource, quiet

# --------------------------------------------------------------------- offline


def fingerprint(root: str) -> str:
    """Return a short hash of every Python source path and file in the package.

    The edited checkout and the executed checkout can differ. Including the hash
    in each report distinguishes a code mismatch from different bench results
    with identical source files.
    """
    import hashlib

    h = hashlib.sha256()
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            h.update(os.path.relpath(path, root).replace("\\", "/").encode())
            with open(path, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()[:12]


def _newest(root: str) -> str:
    """Return the most recently modified source path and its modification time."""
    newest, when = "", 0.0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            try:
                t = os.path.getmtime(path)
            except OSError:
                continue
            if t > when:
                newest, when = os.path.relpath(path, root), t
    if not newest:
        return "none"
    stamp = datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M")
    return f"{newest.replace(os.sep, '/')} ({stamp})"


def stage_offline(report: Report, archive: str | None) -> None:
    """Run the checks that need no instruments before the hardware stages."""

    def imports(c: Check) -> None:
        import bace  # noqa: F401
        from bace.core import axis, illumination, process, pulses  # noqa: F401
        from bace.drivers import protocols, simulated  # noqa: F401
        from bace.experiment import intensity_series, jv, transient  # noqa: F401
        from bace.storage import legacy_dat, naming, numbers  # noqa: F401
        root = os.path.dirname(bace.__file__)
        c.data["package"] = root
        c.data["code fingerprint"] = fingerprint(root)
        c.data["newest source file"] = _newest(root)
        try:
            import h5py  # noqa: F401
        except ImportError:
            c.warn("h5py is absent — HDF5 output will not be written. "
                   "pip install h5py")

    report.run("package imports", "offline", imports)


    def simulated_transient(c: Check) -> None:
        from bace.core.axis import bace_sweep
        from bace.drivers.simulated import make_bench
        from bace.experiment.events import RunFinished
        from bace.experiment.rig import Rig, RigConfig
        from bace.experiment.transient import RunConfig, run_transient_scan

        sim = make_bench(seed=1)
        sim.led.set_pulse(1.020, 0.4)
        rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
                  config=RigConfig(), power=sim.power)
        cfg = RunConfig(n_averages=64, settle_s=0.0, dark_settle_s=0.0,
                        invert_polarity=True)
        fin = None
        for ev in run_transient_scan(rig, bace_sweep(0.88, 0.92, 0.02, n_loops=2),
                                     cfg, sleep=lambda s: None):
            if isinstance(ev, RunFinished):
                fin = ev
        assert fin is not None, "the simulated run produced no RunFinished"
        c.data["axis"] = [round(float(v), 4) for v in fin.values]
        c.data["Q"] = [f"{q:.4e}" for q in fin.q_mean]
        c.data["samples"] = int(fin.photo_averaged.shape[1])
        c.data["dt_ns"] = round(fin.dt * 1e9, 4)

    report.run("simulated transient run", "offline", simulated_transient)

    def simulated_series(c: Check) -> None:
        from bace.core.axis import bace_at_voc
        from bace.drivers.simulated import make_bench
        from bace.experiment import intensity_series as S
        from bace.experiment.rig import Rig, RigConfig
        from bace.experiment.transient import RunConfig

        sim = make_bench(seed=2)
        rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
                  config=RigConfig(), smu=sim.smu, led=sim.led, power=sim.power,
                  router=sim.router)
        series = S.SeriesConfig(led_start_v=1.020, led_stop_v=1.060,
                                led_step_v=0.040, led_settle_s=0.0)
        pts = [e for e in S.run_intensity_series(
            rig, series, bace_at_voc(2),
            RunConfig(n_averages=32, settle_s=0.0, dark_settle_s=0.0,
                      record_length=400, invert_polarity=True), sleep=lambda s: None)
            if isinstance(e, S.SeriesPointDone)]
        c.data["levels"] = [p.level_v for p in pts]
        c.data["Voc"] = [round(p.voc, 4) for p in pts]
        assert sim.bench.bias_output is False, "the bias was left enabled"
        assert sim.bench.smu_output is False, "the SourceMeter was left enabled"

    report.run("simulated intensity series", "offline", simulated_series)

    def simulated_jv(c: Check) -> None:
        from bace.drivers.simulated import make_bench
        from bace.experiment.jv import JVConfig, JVCurveDone, run_jv
        from bace.experiment.rig import Rig, RigConfig

        sim = make_bench(seed=3)
        rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
                  config=RigConfig(), smu=sim.smu, led=sim.led)
        curves = [e for e in run_jv(rig, JVConfig(dark=True, led_levels_v=(1.020,),
                                                  step_v=0.01),
                                    sleep=lambda s: None)
                  if isinstance(e, JVCurveDone)]
        light = [x for x in curves if x.dark is False][0]
        c.data["Voc"] = round(light.metrics.voc, 4)
        c.data["FF"] = round(light.metrics.fill_factor, 4)

    report.run("simulated J-V scan", "offline", simulated_jv)

    def configs(c: Check) -> None:
        from bace.config import check_smu_limits, load_rig, load_run
        rig_path = _find("rig.toml")
        run_path = _find("run.toml")
        if not rig_path or not run_path:
            c.skip("rig.toml / run.toml not found beside the package")
            return
        rig = load_rig(rig_path)
        spec, run, drive, smu, meta, extras = load_run(run_path)
        check_smu_limits(smu, rig)
        c.data["scope"] = rig.scope_address
        c.data["bias"] = rig.bias_address
        c.data["sourcemeter"] = rig.sourcemeter_address
        c.data["led"] = rig.led_address
        c.data["resistor_ohm"] = rig.sense_resistor_ohm
        c.data["pulse_amp"] = rig.pulse_amp
        c.data["compliance_A"] = smu.current_compliance_a
        c.data["ceiling_A"] = rig.max_current_compliance_a
        c.data["axis"] = str(spec.axis)
        c.data["loops"] = spec.n_loops
        c.data["averages"] = run.n_averages

    report.run("config files", "offline", configs)

    def regression(c: Check) -> None:
        if not archive:
            c.skip("no --archive given; point it at a measurement folder to "
                   "re-run the numerical regression on this machine")
            return
        import numpy as np

        from bace.core.process import charge, photocurrent
        from bace.storage import legacy_dat as L

        need = ("averages_light", "averages_dark", "averages_photocurrent",
                "averages_q")
        folder = _resolve_archive(archive, need, L)
        if folder != archive:
            c.data["descended into"] = os.path.basename(folder)
        archive_folder = folder
        paths = {k: L.find(archive_folder, k) for k in need}
        missing = [k for k, v in paths.items() if not v]
        if missing:
            c.skip(f"archive is missing {', '.join(missing)}")
            return

        # `L.read_matrix` returns corner, columns, row labels and data. Columns
        # are the time axis; row labels are V_pre.
        _, t, vpre, light = L.read_matrix(paths["averages_light"])
        _, _, _, dark = L.read_matrix(paths["averages_dark"])
        _, _, _, photo_ref = L.read_matrix(paths["averages_photocurrent"])
        _, rows = L.read_table(paths["averages_q"])
        dt = float(t[1] - t[0])
        _ = photo_ref

        worst = 0.0
        for i, v in enumerate(vpre):
            mine = charge(photocurrent(light[i], dark[i], dt, offset_correct=True),
                          dt, 3.18e-7)
            rel = abs(mine - rows[i, 3]) / abs(rows[i, 3])
            worst = max(worst, rel)
        c.data["folder"] = os.path.basename(archive_folder.rstrip("\\/"))
        c.data["points"] = len(vpre)
        c.data["samples"] = int(light.shape[1])
        c.data["dt_ns"] = round(dt * 1e9, 4)
        c.data["worst_relative_difference"] = f"{worst:.2e}"
        if worst > 1e-5:
            c.warn(f"recomputed charge differs by {worst:.2e} — expected < 1e-5")

    report.run("archive regression", "offline", regression, )

    def roundtrip(c: Check) -> None:
        if not archive:
            c.skip("no --archive given")
            return
        from bace.storage import legacy_dat as L

        archive_folder = _resolve_archive(
            archive, ("averages_light", "averages_q"), L)
        if archive_folder != archive:
            c.data["descended into"] = os.path.basename(archive_folder)
        results = {}
        for which, fmt in (("averages_photocurrent", "sci"),
                           ("averages_light", "sci"), ("averages_dark", "sci"),
                           ("all_loops_q", "fixed")):
            path = L.find(archive_folder, which)
            if not path:
                results[which] = "absent"
                continue
            corner, cols, labels, data = L.read_matrix(path)
            out = L.render_matrix(corner, cols, labels, data, column_format=fmt)
            results[which] = ("identical"
                              if out == open(path, "rb").read().decode("ascii")
                              else "DIFFERS")
        path = L.find(archive_folder, "averages_q")
        if path:
            header, rows = L.read_table(path)
            results["averages_q"] = ("identical"
                                     if L.render_table(header, rows)
                                     == open(path, "rb").read().decode("ascii")
                                     else "DIFFERS")
        c.data.update(results)
        if all(v == "absent" for v in results.values()):
            c.skip("no legacy files found in that folder — point --archive at a "
                   "single run folder, not the directory that contains them")
            return
        if any(v == "DIFFERS" for v in results.values()):
            c.fail("a legacy file did not round-trip byte-for-byte on this machine")

    report.run("legacy .dat byte round-trip", "offline", roundtrip)

    def storage(c: Check) -> None:
        from datetime import datetime

        from bace.core.axis import bace_sweep
        from bace.drivers.simulated import make_bench
        from bace.experiment.rig import Rig, RigConfig
        from bace.experiment.transient import RunConfig, run_transient_scan
        from bace.storage.naming import RunMetadata
        from bace.storage.recorder import RunRecorder, record

        sim = make_bench(seed=4)
        sim.led.set_pulse(1.020, 0.4)
        rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
                  config=RigConfig(), power=sim.power)
        tmp = tempfile.mkdtemp(prefix="bace-bench-")
        meta = RunMetadata(sample="bench", material="SIM", temperature_k=290,
                           started=datetime.now())
        rec = RunRecorder(tmp, meta)
        list(record(run_transient_scan(
            rig, bace_sweep(0.88, 0.92, 0.02, n_loops=2),
            RunConfig(n_averages=32, settle_s=0.0, dark_settle_s=0.0,
                      record_length=400, invert_polarity=True), sleep=lambda s: None), rec))
        c.data["folder"] = rec.folder
        c.data["files"] = [os.path.basename(p) for p in rec.written]
        assert rec.written, "the recorder wrote nothing"

    report.run("storage writes a run folder", "offline", storage)


def _resolve_archive(folder: str, need, L) -> str:
    """Resolve a run folder from that folder or its parent directory.

    The archive path can name the directory containing runs. Searching one
    level down lets the regression find data rather than skip the check.
    """
    if any(L.find(folder, k) for k in need):
        return folder
    try:
        subs = sorted(os.path.join(folder, d) for d in os.listdir(folder)
                      if os.path.isdir(os.path.join(folder, d)))
    except OSError:
        return folder
    for sub in reversed(subs):                  # Reverse chronology
        if all(L.find(sub, k) for k in need):
            return sub
    return folder


def _find(name: str) -> str | None:
    import bace
    here = os.path.dirname(os.path.dirname(os.path.abspath(bace.__file__)))
    for base in (os.getcwd(), here):
        p = os.path.join(base, name)
        if os.path.isfile(p):
            return p
    return None


# -------------------------------------------------------------------- discover
def open_visa(report: Report):
    """Return a PyVISA resource manager, or `None` with the reason recorded."""
    rm = None

    def check(c: Check) -> None:
        nonlocal rm
        import pyvisa
        try:
            rm = pyvisa.ResourceManager()
        except Exception:
            rm = pyvisa.ResourceManager("@py")
            c.warn("the vendor VISA backend failed; fell back to pyvisa-py, "
                   "which cannot talk to GPIB")
        c.data["backend"] = str(rm)
        try:
            c.data["resources"] = list(rm.list_resources())
        except Exception as exc:
            c.warn(f"list_resources failed: {exc}")

    report.run("VISA backend", "discover", check)
    return rm


def stage_discover(report: Report, rm, addresses: dict[str, str]) -> dict[str, str]:
    """Query `*IDN?` at each configured address and map roles to identities."""
    found: dict[str, str] = {}
    for role, address in addresses.items():
        def check(c: Check, role=role, address=address) -> None:
            c.data["address"] = address
            if rm is None:
                c.skip("no VISA backend")
                return
            res = None
            try:
                res = rm.open_resource(address)
                res.timeout = 5000
                idn = str(res.query("*IDN?")).strip()
                c.data["idn"] = idn
                found[role] = idn
                _expect(c, role, idn)
            finally:
                if res is not None:
                    try:
                        res.close()
                    except Exception:
                        pass
        report.run(f"identify {role}", "discover", check)
    return found


EXPECTED = {"scope": ("DSO9054", "INFINIIUM", "AGILENT", "KEYSIGHT"),
            "bias": ("81150",), "sourcemeter": ("2400", "KEITHLEY"),
            "led": ("33220",)}


def _expect(c: Check, role: str, idn: str) -> None:
    want = EXPECTED.get(role, ())
    if want and not any(w.upper() in idn.upper() for w in want):
        c.warn(f"expected one of {want} at this address — the instruments may "
               "be at different addresses than rig.toml says")


# ------------------------------------------------------------------------ read
def stage_read(report: Report, rm, rig_config) -> None:
    """Read back instrument settings without sending settings."""

    def scope(c: Check) -> None:
        res = _wrap(c, rm, rig_config.scope_address, ":SYST:ERR?")
        if res is None:
            c.skip("scope not reachable")
            return
        try:
            for q in (":TIM:RANG?", ":TIM:POS?", ":ACQ:POIN?", ":ACQ:SRAT?",
                      ":ACQ:AVER?",
                      ":ACQ:AVER:COUN?", ":TRIG:MODE?", ":TRIG:EDGE:SOUR?",
                      ":CHAN2:RANG?", ":CHAN2:OFFS?", ":CHAN3:RANG?",
                      ":WAV:SOUR?", ":WAV:FORM?"):
                try:
                    c.data[q] = str(res.query(q)).strip()
                except Exception as exc:
                    c.data[q] = f"<{type(exc).__name__}>"
                    c.warn(f"{q} failed")
        finally:
            res.close()

    def simple(c: Check, address: str, queries: tuple[str, ...],
               error_query: str | None = ":SYST:ERR?") -> None:
        res = _wrap(c, rm, address, error_query)
        if res is None:
            c.skip("not reachable")
            return
        try:
            for q in queries:
                try:
                    c.data[q] = str(res.query(q)).strip()
                except Exception as exc:
                    c.data[q] = f"<{type(exc).__name__}>"
                    c.warn(f"{q} failed")
        finally:
            res.close()

    report.run("oscilloscope state", "read", scope)
    report.run("81150A state", "read", simple, rig_config.bias_address,
               (":FUNC1?", ":FREQ1?", ":VOLT1:HIGH?", ":VOLT1:LOW?",
                ":PULS:DEL1?", ":FUNC1:PULS:WIDT?", ":ARM:SOUR1?", ":OUTP1?"))
    report.run("Keithley 2400 state", "read", simple,
               rig_config.sourcemeter_address,
               (":SOUR:FUNC:MODE?", ":SOUR:VOLT:LEV?", ":SENS:FUNC?",
                ":SENS:CURR:PROT:LEV?", ":ROUT:TERM?", ":SYST:RSEN?", ":OUTP?"))
    report.run("33220A state", "read", simple, rig_config.led_address,
               ("FUNC:SHAP?", ":FREQ?", ":VOLT:HIGH?", ":VOLT:LOW?",
                ":VOLT:OFFS?", "FUNC:PULS:DCYC?", ":OUTP?"))


def _wrap(c: Check, rm, address: str, error_query: str | None) -> RecordingResource | None:
    if rm is None:
        return None
    try:
        res = rm.open_resource(address)
        res.timeout = 10000
    except Exception as exc:
        c.warn(f"cannot open {address}: {exc}")
        return None
    return RecordingResource(res, c.exchanges, error_query=error_query,
                             label=address)


# -------------------------------------------------------------------- non-VISA
def stage_local(report: Report, rig_config) -> None:
    """Check the DIO library, power meter and temperature controller."""
    from bace.drivers.shutter import Shutter

    def delib(c: Check) -> None:
        from bace.drivers import delib as D

        found = D.survey(getattr(rig_config, "dio_dll_path", "") or None)
        c.data.update({p: f"{b}-bit" if b else "unrecognised" for p, b in found})
        bits = D.interpreter_bits()
        c.data["this interpreter"] = f"{bits}-bit"
        if not found:
            c.fail("no DELIB library found — the shutter and relay cannot be "
                   "driven. Deditec ships it as delib.dll (32-bit, SysWOW64) or "
                   "delib64.dll (64-bit, System32)")
            return

        usable = D.resolve(getattr(rig_config, "dio_dll_path", "") or None)
        if usable is not None:
            c.data["will load"] = usable
            # A matching PE header does not prove the DLL can load. Opening it
            # also tests dependencies, file blocking and internal imports, so
            # these failures appear before a run.
            try:
                sh = Shutter(module_id=rig_config.dio_module_id,
                             module_nr=rig_config.shutter_module_nr)
            except Exception as exc:
                c.fail(f"{usable} matches this interpreter but would not load: "
                       f"{type(exc).__name__}: {exc}")
                return
            c.data["loaded"] = getattr(sh, "dll_path", usable)
            c.data["loads in-process"] = "yes — --dio needs no helper"
            return

        # The 32-bit helper requires a 32-bit Python. Check installed
        # interpreters before recommending a launcher command that may
        # have no matching interpreter.
        thirty_two = _pythons_of_bitness(32)
        dll = next((path for path, machine in found if machine == 32), "")
        if thirty_two:
            tag, exe, version = thirty_two[0]
            c.data["32-bit Python"] = f"{tag or exe} (Python {version})"
            c.data["command to start the helper"] = (
                f'{tag or exe} -m bace.drivers.win32bridge.server --delib "{dll}"')
            c.warn(f"no {bits}-bit DELIB, but a 32-bit Python is installed "
                   "— start the helper with the command above, in its own "
                   "window, from this folder, then re-run with --dio")
        else:
            c.data["32-bit Python"] = "none found"
            c.data["py --list"] = _py_launcher_list()
            c.warn(f"no {bits}-bit DELIB and no 32-bit Python to run the "
                   "helper on. Either install a 32-bit Python "
                   "(`py install cpython-3.12-x86`) or install the 64-bit "
                   "DELIB, which lands as delib64.dll in "
                   "C:\\Windows\\System32 — the second is the better fix, "
                   "since it needs no second interpreter and no helper process")

    def power_meter(c: Check) -> None:
        """Open the 1918-C through the same path a run uses.

        With `[power_meter] console` empty, this process opens the USB device.
        A separate console can hold the handle while the service runs, making
        the device appear absent even with its cable connected.
        """
        from bace.drivers.newport1918c import PowerMeterError, open_power_meter
        c.data["through"] = (rig_config.power_meter_console or
                             "this process (USB)")
        try:
            m = open_power_meter(rig_config)
        except PowerMeterError as exc:
            c.warn(f"{exc}; intensity readings will be skipped")
            return
        except Exception as exc:                            # noqa: BLE001
            c.warn(f"{type(exc).__name__}: {exc}; intensity readings will be "
                   "skipped")
            return
        r = m.read()
        c.data["watts"] = r.watts
        c.data["units"] = r.units
        c.data["wavelength_nm"] = r.wavelength_nm
        c.data["saturated"] = r.saturated
        c.data["trustworthy"] = r.trustworthy
        c.data["averaged"] = r.averaged
        c.data["averaging"] = dict(getattr(m, "averaging", {}) or {})
        if not r.trustworthy:
            c.warn("the meter reading is not trustworthy (saturated, overrange, "
                   "or not in watts)")
        if not rig_config.power_meter_console:
            try:
                m.close()
            except Exception:                               # noqa: BLE001
                pass    # Release the handle even if the check fails.

    def temperature(c: Check) -> None:
        """Open the 331 through the same path a run uses.

        A missing cryostat skips the check: room-temperature runs do not need it.
        If the controller is present, the report records its setpoint ceiling so
        a refused setpoint can be checked against that limit.
        """
        from bace.drivers.lakeshore331 import (TemperatureError,
                                               open_temperature_controller)
        through = rig_config.temperature_console or rig_config.temperature_address
        c.data["through"] = through
        try:
            t = open_temperature_controller(rig_config)
        except TemperatureError as exc:
            c.skip(f"no 331 at {through} ({exc}) — temperature loops pause "
                   "for a manual set, and a run that asks for no temperature "
                   "does not need one")
            return
        except Exception as exc:                            # noqa: BLE001
            c.skip(f"no 331 at {through} ({type(exc).__name__}: {exc})")
            return
        try:
            r = t.read()
            c.data["kelvin"] = r.kelvin
            c.data["setpoint_k"] = r.setpoint_k
            c.data["ramping"] = r.ramping
            c.data["heater_range"] = r.heater_range
            c.data["max_setpoint_k"] = r.max_setpoint_k
            c.data["connected"] = r.connected
            if not r.connected:
                c.warn(f"the 331 at {through} is reachable but not reading "
                       f"({r.status_text or 'no status'})")
        finally:
            if not rig_config.temperature_console:
                try:
                    t.close()
                except Exception:                           # noqa: BLE001
                    pass    # Release the GPIB session even if the check fails.

    report.run("DELIB (shutter and relay)", "local", delib)
    report.run("1918-C power meter", "local", power_meter)
    report.run("Lake Shore 331", "local", temperature)


def _py_launcher_list() -> str:
    """Return the Windows Python launcher listing, or an empty string elsewhere."""
    import subprocess
    for args in (["py", "--list-paths"], ["py", "-0p"]):
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return ""


def _launcher_row(line: str) -> tuple[str, str] | None:
    """Parse one `py --list-paths` row as `(tag, executable)`, or return `None`.

    The launcher marks its default with a separate `*`, and executable paths
    can contain spaces. Splitting at the drive letter preserves both. The
    Python Install Manager can bracket optional parts of a tag, as in
    `-V:3.14[-64] *`. Removing the brackets gives the usable tag `3.14-64`.
    """
    import re
    m = re.search(r"[A-Za-z]:\\", line)
    if not m:
        return None
    tag = (line[:m.start()].strip().lstrip("-").removeprefix("V:")
           .strip(" *\t").replace("[", "").replace("]", ""))
    path = line[m.start():].strip().strip('"')
    if not path.lower().endswith(".exe"):
        return None
    return tag, path


def _pythons_of_bitness(want: int) -> list[tuple[str, str, str]]:
    """Return launcher interpreters whose pointer size matches `want` bits.

    Each entry contains the launcher tag, executable and version. The executable
    is the fallback if the tag is missing. Each candidate runs to report its
    pointer size: a `-32` suffix alone does not guarantee a 32-bit interpreter.
    """
    import re
    import subprocess

    listing = _py_launcher_list()
    found = []
    for line in listing.splitlines():
        row = _launcher_row(line)
        if row is None:
            continue
        tag, path = row
        try:
            out = subprocess.run(
                [path, "-c", "import struct,sys;"
                             "print(struct.calcsize('P')*8, sys.version.split()[0])"],
                capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        parts = out.stdout.split()
        if out.returncode == 0 and len(parts) == 2 and parts[0] == str(want):
            found.append((f"py -{tag}" if tag else "", path, parts[1]))
    return found



# ------------------------------------------------------------------- configure
def stage_configure(report: Report, rm, rig_config, run_config, *,
                    led_drive: tuple[float, float] = (1.020, 0.4),
                    pinned: tuple[float, float, float] = (0.9, -1.0, 88.0),
                    force: bool = False) -> None:
    """Send configuration commands without enabling outputs.

    `pinned` contains V_pre, V_coll and delay at the device, in V, V and ns,
    from `run.toml` or the archive defaults of 0.9 V, -1 V and 88 ns. These
    values must match the first point of the run being checked.

    The stage checks the LabVIEW SCPI sequences by sending each command and
    reading the error queue immediately. The report identifies instrument
    rejections. A live output blocks configuration unless `force` is true;
    that override requires no device connected.
    """

    def scope(c: Check) -> None:
        from bace.drivers.infiniium import Infiniium
        res = _wrap(c, rm, rig_config.scope_address, ":SYST:ERR?")
        if res is None:
            c.skip("scope not reachable")
            return
        try:
            s = Infiniium(res, sense_resistor_ohm=rig_config.sense_resistor_ohm,
                          current_sign=rig_config.current_sign,
                          probe_attenuation=rig_config.probe_attenuation)
            s.default_setup()
            s.configure_timebase(run_config.timebase_ns_per_div,
                                 run_config.record_length)
            s.configure_channel(rig_config.scope_channel, vertical_range=0.15,
                                offset=0.0)
            s.configure_edge_trigger(rig_config.trigger_source,
                                     positive=rig_config.trigger_positive)

            # HYPOTHESIS: the panel timebase is ns/div: 200 gives a 2 µs record
            # and an 800 ns screen-centre position. This follows the LabVIEW
            # panel's `timebase*10` convention. Read-back tests the instrument's
            # interpretation rather than assuming it.
            want_range = run_config.timebase_ns_per_div / 1e8
            want_pos = run_config.timebase_ns_per_div * 1e-9 * 4.0
            got_range = _f(res.query(":TIM:RANG?"))
            got_pos = _f(res.query(":TIM:POS?"))
            c.data["timebase panel value"] = run_config.timebase_ns_per_div
            c.data[":TIM:RANG expected/got"] = f"{want_range:g} / {got_range}"
            c.data[":TIM:POS expected/got"] = f"{want_pos:g} / {got_pos}"
            if got_range is not None and abs(got_range - want_range) > 1e-12:
                c.warn("the scope did not take the timebase we computed — the "
                       "ns/div interpretation may be wrong")

            # `:ACQ:POIN?` can report the 5000 requested samples while a 2 µs
            # record at 2 GSa/s contains 4000 (docs/notes.md). The delivered
            # record depends on duration and sample rate; report both.
            got_points = _f(res.query(":ACQ:POIN?"))
            srate = _f(res.query(":ACQ:SRAT?"))
            c.data[":ACQ:POIN asked/reports"] = f"{run_config.record_length} / {got_points}"
            c.data[":ACQ:SRAT?"] = srate
            if srate and want_range:
                implied = want_range * srate
                c.data["points implied by range x sample rate"] = f"{implied:g}"
                if abs(implied - run_config.record_length) > 1:
                    c.warn(f"the window ({want_range:g} s) at {srate:g} Sa/s holds "
                           f"{implied:g} points, not the {run_config.record_length} "
                           "asked for — this is where the archive's 4000 comes from, "
                           "and it means record_length is a request, not a promise")
        finally:
            res.close()

    def bias(c: Check) -> None:
        from bace.core.pulses import pulse_levels
        from bace.drivers.agilent81150 import Agilent81150
        res = _wrap(c, rm, rig_config.bias_address, ":SYST:ERR?")
        if res is None:
            c.skip("81150A not reachable")
            return
        try:
            g = Agilent81150(res)
            g.identify()
            if _output_is_live(c, res, ":OUTP1?", "81150A", force):
                return
            g.configure_trigger()
            g.configure_shape(run_config.pulse_frequency_hz,
                              duty_percent=run_config.duty_percent)
            vpre, vcoll, delay_ns = pinned
            lv = pulse_levels(vpre, vcoll, rig_config.pulse_amp, delay_ns,
                              run_config.pulse_width_ns,
                              invert=run_config.invert_polarity)
            c.data["pinned (vpre / vcoll / delay_ns at the device)"] = (
                f"{vpre:g} V / {vcoll:g} V / {delay_ns:g} ns"
                + (", invert_polarity" if run_config.invert_polarity else ""))
            g.set_levels(lv.high_light, lv.low_light, delay_s=lv.delay_s,
                         width_s=lv.width_s)
            c.data["set high/low (V at generator)"] = f"{lv.high_light:g} / {lv.low_light:g}"
            c.data["delay_s"] = f"{lv.delay_s:g}"
            for q, want in ((":VOLT1:HIGH?", lv.high_light),
                            (":VOLT1:LOW?", lv.low_light),
                            (":PULS:DEL1?", lv.delay_s),
                            (":FUNC1:PULS:WIDT?", lv.width_s)):
                got = _f(res.query(q))
                c.data[f"{q} wanted/got"] = f"{want:g} / {got}"
                if got is not None and abs(got - want) > max(1e-9, abs(want) * 1e-4):
                    c.warn(f"{q} did not take the value we set")
            assert not g.output_enabled, "the 81150A output must stay off here"
        finally:
            res.close()

    def sourcemeter(c: Check) -> None:
        from bace.drivers.keithley2400 import Keithley2400, SourceMeterConfig
        res = _wrap(c, rm, rig_config.sourcemeter_address, ":SYST:ERR?")
        if res is None:
            c.skip("Keithley not reachable")
            return
        try:
            k = Keithley2400(res, config=SourceMeterConfig())
            c.data["idn"] = k.identify()
            if _output_is_live(c, res, ":OUTP?", "Keithley 2400", force):
                return
            k.default_setup()
            # Use the source and sense sequence of `Keithley2400.measure_jsc`
            # without enabling the output.
            res.write("*RST")
            res.write(":SOUR:FUNC:MODE VOLT;")
            res.write(":SOUR:VOLT:LEV 0;")
            res.write(":SENS:FUNC 'CURR:DC';")
            res.write(":SENS:CURR:PROT:LEV 0.01;")
            res.write(":SENS:CURR:NPLC 1;")
            res.write(":FORM:ELEM CURR;")
            c.data[":OUTP?"] = str(res.query(":OUTP?")).strip()
            # V_oc sensing requires voltage protection, not current protection.
            res.write(":SOUR:FUNC:MODE CURR;")
            res.write(":SOUR:CURR:LEV 0;")
            res.write(":SENS:FUNC 'VOLT:DC';")
            res.write(":SENS:VOLT:PROT:LEV 2;")
            res.write(":FORM:ELEM VOLT;")
        finally:
            res.close()

    def led(c: Check) -> None:
        from bace.drivers.agilent33220a import Agilent33220A
        res = _wrap(c, rm, rig_config.led_address, ":SYST:ERR?")
        if res is None:
            c.skip("33220A not reachable")
            return
        try:
            g = Agilent33220A(res)
            c.data["idn"] = g.identify()
            if _output_is_live(c, res, ":OUTP?", "33220A", force):
                return
            g.set_dc(led_drive[0])
            c.data[":VOLT:OFFS?"] = str(res.query(":VOLT:OFFS?")).strip()
            # The 33220A Sync arms the 81150A, so both generators must use the
            # run's period. A 1 kHz driver default doubles the bench's 500 Hz
            # frequency (manual 1.2).
            drive = led_drive
            g.set_pulse(drive[0], drive[1],
                        frequency_hz=run_config.pulse_frequency_hz,
                        duty_percent=run_config.duty_percent)
            for q in (":VOLT:HIGH?", ":VOLT:LOW?", ":FREQ?", "FUNC:PULS:DCYC?"):
                c.data[q] = str(res.query(q)).strip()
            assert not g.output_enabled, "the 33220A output must stay off here"
        finally:
            res.close()

    report.run("configure oscilloscope", "configure", scope)
    report.run("configure 81150A (output off)", "configure", bias)
    report.run("configure Keithley 2400 (output off)", "configure", sourcemeter)
    report.run("configure 33220A (output off)", "configure", led)


def _output_is_live(c: Check, res, query: str, name: str, force: bool) -> bool:
    """Block configuration if the instrument reports an enabled output.

    Changing live generator levels changes the device voltage immediately.
    Levels measured on this bench include 1.263 V at the generator, about
    5.05 V at the device; a change to 0.225 V gives 0.9 V at the device.
    The driver's output flag cannot establish safety because another program,
    including LabVIEW, can enable the output. The instrument read-back is
    required. The `force` override requires no device connected.
    """
    try:
        live = _f(res.query(query))
    except Exception as exc:
        c.warn(f"could not read {query} ({exc}); refusing to configure {name}")
        return True
    c.data[f"{query} before configuring"] = live
    if not live:
        return False
    if force:
        c.warn(f"{name} output was ALREADY ON and --force-configure was given; "
               "its levels were changed while live")
        return False
    c.skip(f"{name} output is already ON ({query} = {live:g}). Refusing to change "
           "its settings while it is driving — that is exactly the mistake this "
           "harness exists to avoid. Turn the output off (or stop the LabVIEW "
           "program) and run again, or pass --force-configure if you are sure "
           "nothing is connected.")
    return True


def _f(x) -> float | None:
    try:
        return float(str(x).strip().split(",")[0])
    except Exception:
        return None


# --------------------------------------------------------------------- acquire
def stage_acquire(report: Report, rm, rig_config, run_config, averages: int) -> None:
    """Acquire one scope record with the normal trigger source running."""

    def acquire(c: Check) -> None:
        import numpy as np

        from bace.drivers.infiniium import Infiniium
        res = _wrap(c, rm, rig_config.scope_address, ":SYST:ERR?")
        if res is None:
            c.skip("scope not reachable")
            return
        from bace.drivers.infiniium import ScopeError
        try:
            s = Infiniium(res, sense_resistor_ohm=rig_config.sense_resistor_ohm,
                          current_sign=rig_config.current_sign,
                          probe_attenuation=rig_config.probe_attenuation)
            s.default_setup()
            s.configure_timebase(run_config.timebase_ns_per_div,
                                 run_config.record_length)
            s.configure_edge_trigger(rig_config.trigger_source,
                                     positive=rig_config.trigger_positive)
            try:
                with quiet(res):             # Preserve timing
                    vrange, voffset = s.autorange(rig_config.current_source,
                                                  channel=rig_config.scope_channel,
                                                  verify=True)
                    trace = s.acquire(averages, source=rig_config.current_source,
                                      autorange_first=False, timeout_s=60.0)
            except ScopeError as exc:
                # A missing Sync signal leaves nothing to trigger on. Report a
                # warning with a remedy to distinguish it from a rejected command.
                c.warn(f"{exc}. Is the 81150A running and its SYNC connected to "
                       f"{rig_config.trigger_source}? Alternatively the "
                       ":ADER? poll may not work on this firmware — the report "
                       "transcript shows what it returned.")
                c.data[":ADER? replies"] = [x.reply for x in c.exchanges
                                            if "ADER" in x.command][-5:]
                c.data[":WAV:COUN? replies"] = [x.reply for x in c.exchanges
                                                if "WAV:COUN" in x.command][-5:]
                return
            c.data["autorange range/offset (V)"] = f"{vrange:g} / {voffset:g}"
            c.data["fetch route"] = s.last_fetch_method
            c.data["autorange passes"] = s.last_autorange_passes
            if s.last_autorange_unchanged:
                c.data["autorange"] = ("range already correct -- left alone "
                                       "(deadband), so no 150 ms settling query")
            c.data["autorange windows tried"] = [
                f"{r:.4f} @ {o:+.4f} -> {'clipped' if clip else 'fits'}"
                for r, o, clip in s.last_autorange_windows]
            if s.last_autorange_clipped:
                c.warn("the auto-range gave up while the trace was still "
                       "clipping — every charge measured at this range would be "
                       "an underestimate. Raise `passes`, or start the channel "
                       "wider")
            c.data["points returned"] = trace.n
            c.data["record length asked"] = run_config.record_length
            c.data["dt (ns)"] = round(trace.dt * 1e9, 4)
            c.data["t0 (ns)"] = round(trace.t0 * 1e9, 3)
            c.data["duration (us)"] = round(trace.duration_s * 1e6, 4)
            y = np.asarray(trace.y)
            c.data["current min/max (A)"] = f"{y.min():.4e} / {y.max():.4e}"
            c.data["tail mean, last 10% (A)"] = f"{y[int(y.size * 0.9):].mean():.4e}"
            c.data["first 8 samples (A)"] = [f"{v:.4e}" for v in y[:8]]

            if trace.n != run_config.record_length:
                c.warn(f"asked for {run_config.record_length} points and got "
                       f"{trace.n}: the record is the window times the sample "
                       f"rate ({run_config.timebase_ns_per_div / 1e8:g} s at "
                       f"{1 / trace.dt:.3g} Sa/s), so record_length is a request")
            c.data["trigger sits this far into the record (ns)"] = round(
                -trace.t0 * 1e9, 1)
            if trace.t0 < 0:
                # The trigger is about 200 ns into the record (docs/notes.md).
                # Convert `t0_int_s` from field arrival to record time, as
                # `bace.core.process.charge` requires. This stage drives no
                # generator, so it shows the integration window for zero programmed
                # delay. A scan adds its delay to the field arrival and window.
                from bace.experiment.transient import resolve_window
                t0_rec, t1_rec = resolve_window(run_config, trace.t0, 0.0,
                                                rig_config.trigger_offset_s)
                c.data["integration window in record time at delay 0 (ns)"] = (
                    f"{t0_rec * 1e9:.1f} .. {t1_rec * 1e9:.1f}")
                after_trigger = (t0_rec + trace.t0) * 1e9
                c.data["t0_int, relative to the trigger (ns)"] = round(after_trigger, 1)
                if t1_rec > (trace.n - 1) * trace.dt:
                    c.warn(f"the integration window ends at {t1_rec * 1e9:.0f} ns, "
                           f"past the {(trace.n - 1) * trace.dt * 1e9:.0f} ns record")
            expected_dt = (run_config.timebase_ns_per_div / 1e8) / max(trace.n, 1)
            c.data["dt expected (ns)"] = round(expected_dt * 1e9, 4)
            if abs(trace.dt - expected_dt) > expected_dt * 0.05:
                c.warn("dt does not match full-screen/points — the timebase "
                       "interpretation needs re-checking")
        finally:
            res.close()

    def transfer(c: Check) -> None:
        """Inspect the raw bytes returned by `:WAV:DATA?`.

        A preamble can report 4000 samples while the parsed array is empty.
        An indefinite-length block header, a termination byte inside binary data
        or a fetch during acquisition can cause this mismatch. The raw block
        distinguishes these cases; a fetch during acquisition is documented in
        docs/notes.md.
        """
        res = _wrap(c, rm, rig_config.scope_address, ":SYST:ERR?")
        if res is None:
            c.skip("scope not reachable")
            return
        try:
            res.write(":SYST:HEAD OFF;")
            c.data[":WAV:STR?"] = str(res.query(":WAV:STR?")).strip()
            c.data[":TRIG:SWE?"] = str(res.query(":TRIG:SWE?")).strip()
            c.data[":CHAN2:DISP?"] = str(res.query(":CHAN2:DISP?")).strip()
            c.data["read_termination"] = repr(getattr(res, "read_termination", None))
            res.write(":RUN;")
            time.sleep(0.5)
            res.write(":STOP;")
            res.write(f":WAV:SOUR {rig_config.current_source};")
            c.data["preamble"] = str(res.query(
                ":WAV:POIN?;:WAV:XOR?;:WAV:XINC?;:WAV:YOR?;:WAV:YINC?;:WAV:YREF?;"
            )).strip()
            with quiet(res):
                res._io.write(":WAV:DATA?")
                raw = res._io.read_raw()
            c.data["bytes returned"] = len(raw)
            c.data["first 24 bytes"] = raw[:24].hex(" ")
            c.data["header as text"] = repr(raw[:12].decode("ascii", "replace"))
            c.data["last 8 bytes"] = raw[-8:].hex(" ")
            if raw[:1] != b"#":
                c.fail("the reply does not start with '#', so it is not an IEEE "
                       "block at all — something else is being read")
            elif raw[1:2] == b"0":
                c.warn("indefinite-length block ('#0'): this is what :WAV:STR ON "
                       "produces, and it is why the parser returned nothing. "
                       "Streaming is now switched off in default_setup")
            else:
                ndigits = int(raw[1:2])
                declared = int(raw[2:2 + ndigits])
                c.data["declared payload bytes"] = declared
                c.data["actual payload bytes"] = len(raw) - 2 - ndigits
                if len(raw) - 2 - ndigits < declared:
                    c.warn("the block is shorter than its header declares — the "
                           "transfer was truncated, most likely by a read "
                           "termination character inside the binary data")
        finally:
            res.close()

    report.run("raw waveform transfer", "acquire", transfer)
    report.run(f"one acquisition, {averages} averages", "acquire", acquire)


# ------------------------------------------------------------------------- dio
def stage_dio(report: Report, rig_config, confirm, rm=None, *,
              smu_config=None, restore_outputs: bool = True,
              listen: bool = False) -> None:
    """Identify the shutter and relay lines through electrical measurements.

    Module 0, channel 0 of module ID 9 controls the shutter; module 1 controls
    the relay (manual 1.2). The LabVIEW shutter sequence specifies the same
    module 0 target and a 0.1 s settle time. Module 1's LabVIEW label,
    `open/close shutter 2`, alone does not establish its electrical role.
    `bace.drivers.routing.BiasRouter` relies on that line selecting the
    amplifier or Keithley path. If it controls an optical shutter instead,
    the interlock does not protect the electrical path.

    A click cannot distinguish those roles. With the LED on, photocurrent
    at the Keithley requires both an open shutter and a connected device.
    The stage walks all four line states and measures V at I = 0, I at V = 0,
    and I at `DIO_PROBE_V` in each state.

    A connected, illuminated device gives V_oc of about 0.9 V and photocurrent
    of order mA, measured on this bench. A connected dark device gives about
    0 V and little current at V = 0. These readings alone cannot establish
    a disconnection: sourcing 0 A into an open circuit is a degenerate control
    loop and need not reach voltage compliance (docs/notes.md). Forward
    conduction at `DIO_PROBE_V` identifies the connected electrical path
    independently of illumination.

    `Keithley2400.measure_voc` sources 0 A and `Keithley2400.measure_jsc`
    sources 0 V. `Keithley2400.measure_jsat` applies the small forward probe
    voltage with the recipe's compliance. Each reading enables the Keithley
    only for that reading. The 81150A stays off throughout; the Keithley
    output is off during line changes. Switching the electrical path under
    load can damage hardware. The line states must be restored before
    outputs are restored. An unreadable initial line state vetoes output
    restoration; `notes["dio restored"] = False` also vetoes it.
    """

    def identify(c: Check) -> None:
        make, how, why = _dio_backend(rig_config)
        if make is None:
            c.skip(why)
            return
        c.data["backend"] = how
        if rm is None:
            c.fail("no VISA session, so the outputs cannot be proved off. "
                   "Refusing to move either line.")
            return

        notes: dict[str, Any] = {}
        try:
            with _outputs_off(rm, rig_config, notes, restore=restore_outputs):
                _walk_dio_states(c, make, rig_config, rm, confirm, notes,
                                 smu_config=smu_config, listen=listen)
        except Exception as exc:
            c.fail(f"{type(exc).__name__}: {exc}")
        finally:
            c.data.update(notes)

    report.run("what the DIO lines do", "dio", identify)


DIO_CONDUCTION_A = 1e-9
"""Current above this threshold at `DIO_PROBE_V` identifies a connected device.

The threshold is two decades above the open-input leakage of about
1.5e-11 A measured on this bench, and far below the device's forward
current. Recheck it if the report's smallest probe current approaches it.
"""

DIO_PROBE_V = 0.5
"""Probe the Keithley path with a small forward voltage, in V.

The probe is below V_oc of about 0.92 V measured on this bench. It tests
conduction on the device's J–V curve while retaining the recipe's
current compliance.
"""

DIO_SETTLE_S = 0.3
"""Wait after each DIO line change, in s.
The LabVIEW shutter sequence waits 0.1 s. The longer wait allows relay
and device recovery after switching; it applies to each of the four states.
"""


def _walk_dio_states(c: Check, make, rig_config, rm, confirm, notes: dict, *,
                     smu_config=None, listen: bool = False) -> None:
    """Measure all four shutter and relay line states with the Keithley."""
    from bace.drivers.keithley2400 import Keithley2400, SourceMeterConfig

    # Use the recipe's current compliance: `run.toml` specifies 10 mA on
    # this bench and `bace.config.check_smu_limits` enforces its ceiling.
    # The driver's 50 mA default would raise the diagnostic limit fivefold.
    smu_config = smu_config or SourceMeterConfig()
    c.data["compliance (A)"] = smu_config.current_compliance_a

    modules = (rig_config.shutter_module_nr, rig_config.relay_module_nr)
    lines, started = {}, {}
    for nr in modules:
        lines[nr] = make(nr)
        lines[nr].open()
        started[nr] = lines[nr].read_line()
    c.data["lines found at"] = {f"module {nr}": (v if v is not None else "unreadable")
                                for nr, v in started.items()}
    if any(v is None for v in started.values()):
        notes["dio restored"] = False

    # Without LED light, no state gives photocurrent and the roles remain
    # inconclusive. Record the LED state to expose that condition.
    led = _led_state(rm, rig_config)
    c.data.update(led)

    table = {}
    try:
        with contextlib.closing(
                rm.open_resource(rig_config.sourcemeter_address)) as res:
            res.timeout = 20000
            smu = Keithley2400(
                RecordingResource(res, c.exchanges,
                                  label=rig_config.sourcemeter_address),
                config=smu_config)
            for a in (0, 1):
                for b in (0, 1):
                    lines[modules[0]].set_line(a)
                    lines[modules[1]].set_line(b)
                    time.sleep(DIO_SETTLE_S)
                    volts = smu.measure_voc()
                    amps = smu.measure_jsc()
                    probe = smu.measure_jsat(DIO_PROBE_V)
                    table[(a, b)] = (volts, amps, probe)
                    c.data[f"module {modules[0]}={a}, {modules[1]}={b}"] = (
                        f"V(I=0) {volts:+.4f} V   I(V=0) {amps:+.4e} A   "
                        f"I({DIO_PROBE_V:+g}V) {probe:+.4e} A")
                    if listen:
                        c.data[f"heard at {a}{b}"] = confirm(
                            f"module {modules[0]} = {a}, "
                            f"module {modules[1]} = {b}. What moved?")
    finally:
        for nr in modules:
            try:
                if started[nr] is not None:
                    lines[nr].set_line(started[nr])
            finally:
                lines[nr].close()

    _classify_dio(c, table, modules, led_on=led.get("LED output") == "ON")


def _led_state(rm, rig_config) -> dict:
    """Read back the 33220A output state and levels."""
    out = {}
    try:
        with contextlib.closing(
                rm.open_resource(rig_config.led_address)) as res:
            res.timeout = 5000
            on = float(res.query(":OUTP?").strip().split(",")[0])
            out["LED output"] = "ON" if on else "OFF"
            out["LED levels (V)"] = (f"{float(res.query(':VOLT:HIGH?')):g} / "
                                     f"{float(res.query(':VOLT:LOW?')):g}")
    except Exception as exc:
        out["LED output"] = f"unreadable ({type(exc).__name__})"
    return out


def _classify_dio(c: Check, table: dict, modules, *, led_on: bool = True) -> None:
    """Infer shutter and relay roles from four sets of measurements.

    Two independent signatures are required. Photocurrent at V = 0 identifies
    the state with both the electrical path connected to the Keithley and the
    shutter open. It determines the two line values but cannot distinguish
    their roles.

    Forward conduction at `DIO_PROBE_V` identifies the connected electrical
    path with or without light. From the photocurrent state, changing the line
    that removes conduction switches the electrical path. Changing the line
    that retains conduction but removes photocurrent shuts the shutter.

    Voltage compliance at I = 0 is not a reliable disconnection signature;
    `stage_dio` explains the open-circuit limitation.
    """
    if len(table) != 4 or any(len(v) != 3 for v in table.values()):
        return
    lit = max(table, key=lambda k: abs(table[k][1]))
    lit_i = abs(table[lit][1])
    others = [abs(v[1]) for k, v in table.items() if k != lit]
    if lit_i < 1e-9 or lit_i < 10 * max(max(others), 1e-15):
        why = ("the 33220A output is OFF, so no light reached the device — "
               "turn the LED on and run this again" if not led_on else
               "the LED is on, so either the device is not connected on any "
               "path, or neither line reaches it")
        c.warn(f"no state stands out with a photocurrent, so the roles cannot "
               f"be read off: {why}. The four readings are above and are the "
               "evidence either way.")
        return
    c.data["photocurrent state"] = (
        f"module {modules[0]}={lit[0]}, {modules[1]}={lit[1]}  "
        f"-> {table[lit][1]:+.4e} A, V_oc {table[lit][0]:+.4f} V. "
        "Device on the Keithley, light reaching it.")

    # Forward conduction indicates a connected electrical path.
    #
    # The threshold must be absolute. Splitting at the largest gap between
    # four readings fails if both lines control light: the device conducts
    # in every state, but the gap between illuminated and dark readings
    # would imply three disconnections. Open-input leakage measured on this
    # bench is about 1.5e-11 A; device conduction at +0.5 V is orders larger.
    # The distinction depends on instrument leakage, not relative device
    # currents.
    probes = {k: abs(v[2]) for k, v in table.items()}
    connected = {k for k, i in probes.items() if i >= DIO_CONDUCTION_A}
    c.data["conducting at the probe bias"] = (
        sorted(f"{modules[0]}={k[0]},{modules[1]}={k[1]}" for k in connected)
        or "none")
    c.data["smallest probe current (A)"] = f"{min(probes.values()):.3e}"

    if lit not in connected:
        c.warn("the state with photocurrent does not conduct at the probe bias, "
               "which is self-contradictory — treat the whole table as suspect "
               "rather than the roles below.")
        return
    if len(connected) == 4:
        c.warn("every state conducts, so no line disconnects the device: "
               "neither is the path switch, and `routing.Relay` is guarding a "
               "line that does not do what it thinks. Both may be optical.")
        return

    roles = {}
    for i, nr in enumerate(modules):
        flipped = (1 - lit[0], lit[1]) if i == 0 else (lit[0], 1 - lit[1])
        stays = flipped in connected
        roles[nr] = ("switches the light (shutter)" if stays
                     else "switches the electrical path (relay)")
        c.data[f"module {nr}"] = (
            f"{roles[nr]} — flipping it gives I({DIO_PROBE_V:+g}V) "
            f"{table[flipped][2]:+.4e} A against {table[lit][2]:+.4e} A")

    if len(set(roles.values())) == 1:
        c.warn(f"both lines came out as '{next(iter(roles.values()))}', which "
               "cannot be right. The table above is the evidence; do not act "
               "on the two lines above it.")
        return

    # `modules[0]` is the configured shutter line. If its measured role differs,
    # correct `rig.toml`: a run could otherwise move the relay when opening
    # the shutter.
    if roles[modules[0]] != "switches the light (shutter)":
        c.warn(f"module {modules[0]} is configured as the shutter but behaves "
               f"as the path switch, and module {modules[1]} the other way "
               "round. rig.toml has them the wrong way up — swap "
               "`shutter_module_nr` and `relay_module_nr` before any run.")


SOURCES = (("81150A", "bias_address", ":OUTP1", ":OUTP1?"),
           ("Keithley 2400", "sourcemeter_address", ":OUTP", ":OUTP?"))
"""Describe output controls for sources that can put voltage on the device.
The 33220A drives the LED and therefore has no entry here.
"""


@contextlib.contextmanager
def _outputs_off(rm, rig_config, notes: dict, restore: bool = True):
    """Disable device-node sources during an operation and restore enabled outputs.

    Moving the relay while either source drives can damage hardware. The
    LabVIEW program can own the outputs, so software must disable them and
    verify each instrument's read-back before moving a line. An accepted write
    alone does not prove that an output is off. If either still reports on,
    the context raises before the caller can move the relay.

    Only outputs enabled on entry are eligible for restoration, after the
    caller finishes. `notes` records each state change for the report.
    The caller can set `notes["dio restored"] = False` to veto restoration.
    A source must stay off if the signal path cannot be restored or its
    initial state cannot be read. `restore=False` also leaves outputs off.
    """
    was: dict[str, bool] = {}
    with contextlib.ExitStack() as stack:
        sessions = {}
        for name, attr, _, query in SOURCES:
            res = stack.enter_context(
                contextlib.closing(rm.open_resource(getattr(rig_config, attr))))
            res.timeout = 5000
            sessions[name] = res
            was[name] = bool(float(res.query(query).strip().split(",")[0]))
        notes["outputs found on"] = ([n for n, on in was.items() if on]
                                     or "none")

        for name, _, root, query in SOURCES:
            if not was[name]:
                continue
            sessions[name].write(f"{root} OFF;")
            time.sleep(0.2)
            still = float(sessions[name].query(query).strip().split(",")[0])
            if still:
                raise RuntimeError(
                    f"{name} still reports its output ON after {root} OFF. "
                    "Refusing to move the relay.")
            notes.setdefault("turned off", []).append(name)

        try:
            yield
        finally:
            back = []
            if restore and notes.get("dio restored", True):
                for name, _, root, query in SOURCES:
                    if was[name]:
                        sessions[name].write(f"{root} ON;")
                        back.append(name)
            notes["outputs restored"] = back or "none"
            if was and not back and any(was.values()):
                notes["left off"] = [n for n, on in was.items() if on]


def _basename(path: str) -> str:
    """Return the last path component with either slash convention.

    `os.path.basename` uses the host separator. A Windows path must also split
    correctly on a non-Windows test host.
    """
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _dio_backend(rig_config):
    """Return a DIO backend or explain why neither route is available.

    DELIB uses `delib.dll` for 32-bit Python and `delib64.dll` for 64-bit
    Python (docs/notes.md). `ctypes` requires a library matching the interpreter.
    `bace.drivers.delib` selects the installed library. If no usable library
    or helper is available, the result names the deployment remedy rather
    than reporting the same architecture error as multiple stage failures.

    The result is `(factory, description, reason_if_unavailable)`.
    """
    from bace.drivers import delib as D
    from bace.drivers.shutter import BridgedShutter, Shutter

    explicit = getattr(rig_config, "dio_dll_path", "") or None
    path = D.resolve(explicit)

    if path is not None:
        def direct(nr):
            return Shutter(path, module_id=rig_config.dio_module_id,
                           module_nr=nr, settle_s=0.4)
        try:
            # A matching PE header does not prove the DLL loads. Opening it also
            # tests missing dependencies and blocked files.
            Shutter(path, module_id=rig_config.dio_module_id,
                    module_nr=rig_config.shutter_module_nr)
            return direct, f"{_basename(path)} in-process", ""
        except Exception as exc:
            load_error = f"{path}: {type(exc).__name__}: {exc}"
    else:
        seen = ", ".join(f"{p} ({b}-bit)" if b else p
                         for p, b in D.survey(explicit))
        load_error = (f"no {D.interpreter_bits()}-bit DELIB"
                      + (f"; found only {seen}" if seen else "; none installed"))

    try:
        from bace.drivers.win32bridge.client import BridgeClient
        client = BridgeClient().connect()
        client.ping()
    except Exception as exc:
        thirty_two = _pythons_of_bitness(32)
        dll32 = next((p for p, b in D.survey(explicit) if b == 32), "")
        if thirty_two and dll32:
            tag, exe, version = thirty_two[0]
            how_to = (f"start it in its own window from this folder:  "
                      f"{tag or exe} -m bace.drivers.win32bridge.server "
                      f'--delib "{dll32}"   (found Python {version})')
        else:
            how_to = ("install the 64-bit DELIB -- it lands as delib64.dll in "
                      "C:\\Windows\\System32 and needs no helper at all")
        return None, "", (
            f"{load_error}, and the 32-bit helper is not answering "
            f"({type(exc).__name__}). {how_to}. The helper needs --delib: "
            "without it the server starts with no shutter target and the call "
            "fails anyway. `--measure` is not blocked by this -- run it with "
            "--manual-shutter and block the beam by hand. What is blocked is "
            "unattended running, and the relay that moves the device between "
            "the amplifier and the Keithley.")

    def bridged(nr):
        return BridgedShutter(client, module_id=rig_config.dio_module_id,
                              module_nr=nr, settle_s=0.4)

    return bridged, "32-bit helper (win32bridge)", ""


# --------------------------------------------------------------------- outputs
def stage_outputs(report: Report, rm, rig_config, run_config=None, *,
                  led_drive: tuple[float, float] = (1.020, 0.4)) -> None:
    """Enable each output briefly with no device connected.

    The stage can put a voltage on the device node, so it requires `--outputs`
    and a typed confirmation unless `--yes` skips it. Disconnect the device
    before proceeding.
    """
    from bace.experiment.transient import RunConfig
    run_config = run_config or RunConfig()

    def bias(c: Check) -> None:
        from bace.drivers.agilent81150 import Agilent81150
        res = _wrap(c, rm, rig_config.bias_address, ":SYST:ERR?")
        if res is None:
            c.skip("81150A not reachable")
            return
        g = Agilent81150(res)
        try:
            g.configure_trigger()
            g.configure_shape(1000.0)
            g.set_levels(0.0, 0.0, delay_s=1.35e-7, width_s=5e-6)   # 0 V both
            g.enable_output(True)
            c.data[":OUTP1? while enabled"] = str(res.query(":OUTP1?")).strip()
            c.data["errors"] = g.errors()
        finally:
            try:
                g.disable_output()
            finally:
                c.data[":OUTP1? after"] = str(res.query(":OUTP1?")).strip()
                res.close()

    def smu(c: Check) -> None:
        from bace.drivers.keithley2400 import Keithley2400, SourceMeterConfig
        res = _wrap(c, rm, rig_config.sourcemeter_address, ":SYST:ERR?")
        if res is None:
            c.skip("Keithley not reachable")
            return
        k = Keithley2400(res, config=SourceMeterConfig(current_compliance_a=0.001))
        try:
            value = k.measure_jsc(settle_ms=200)
            c.data["current at 0 V, open circuit (A)"] = f"{value:.4e}"
            c.data["errors"] = k.errors()
            if abs(value) > 1e-6:
                c.warn("more than 1 uA at 0 V into what should be an open "
                       "circuit — is something still connected?")
        finally:
            try:
                k.disable_output()
            finally:
                res.close()

    def led(c: Check) -> None:
        from bace.drivers.agilent33220a import Agilent33220A
        res = _wrap(c, rm, rig_config.led_address, ":SYST:ERR?")
        if res is None:
            c.skip("33220A not reachable")
            return
        g = Agilent33220A(res)
        try:
            # The 33220A Sync arms the 81150A, so both generators must use the
            # run's period. A 1 kHz driver default doubles the bench's 500 Hz
            # frequency (manual 1.2).
            drive = led_drive
            g.set_pulse(drive[0], drive[1],
                        frequency_hz=run_config.pulse_frequency_hz,
                        duty_percent=run_config.duty_percent)
            g.enable_output(True)
            c.data[":OUTP? while enabled"] = str(res.query(":OUTP?")).strip()
            c.data["errors"] = g.errors()
        finally:
            try:
                g.off()
            finally:
                c.data[":OUTP? after"] = str(res.query(":OUTP?")).strip()
                res.close()

    report.run("81150A output on/off at 0 V", "outputs", bias)
    report.run("Keithley output on/off at 0 V", "outputs", smu)
    report.run("33220A output on/off", "outputs", led)


# --------------------------------------------------------------------- measure
class PromptShutter:
    """Prompt the operator to open and close the shutter.

    A 64-bit session cannot load 32-bit `delib.dll`. When neither a compatible
    library nor a helper is available, manual operation still allows the
    acquisition and subtraction chain to be checked. A one-point, two-loop
    run requires four shutter prompts.
    """

    def __init__(self, ask):
        self.ask = ask
        self._open = False

    def unblock(self) -> None:
        if not self._open:
            self.ask("OPEN the shutter (light on the sample), then press Enter")
            self._open = True

    def shut(self) -> None:
        if self._open:
            self.ask("CLOSE the shutter (sample dark), then press Enter")
            self._open = False

    @property
    def is_open(self) -> bool:
        return self._open


def _rail_samples(a) -> int:
    """Count samples equal to the minimum or maximum of `a`.

    Clipping repeats the digitiser's limiting code. Repetition at an extremum
    therefore checks the acquired samples independently of auto-range status.
    With N_avg = 16, repeated extrema at the same 16-bit code are suspicious.
    A light trace and dark trace measured on this bench share a minimum of
    -5.83608e-03 A, repeated about 70 times in 4000 samples. Where both clip
    to the same value, their difference is zero and the apparent transient
    can consist only of the baseline correction.

    This count does not test adjacency. Consecutive repeated samples within
    one trace are stronger clipping evidence than a shared extremum between
    traces (docs/notes.md).
    """
    import numpy as _np

    a = _np.asarray(a)
    if a.size == 0:
        return 0
    return int(max((a == a.min()).sum(), (a == a.max()).sum()))


def saturation_verdict(light, dark) -> tuple[int, int, bool, str | None]:
    """Check the light trace and dark trace for possible digitiser saturation.

    Return `(rail_light, rail_dark, shared_extreme, warning or None)`.
    The sample check complements auto-range status: a range set elsewhere or
    an incomplete auto-range loop can leave clipping in the acquired data.

    The check warns on a shared extremum or more than eight repeated extreme
    samples. If both traces clip at the same value, their difference is zero
    there and removes the transient. A shared extremum alone does not prove
    clipping; consecutive repeats within one trace provide stronger evidence
    (docs/notes.md). The warning is conservative.
    """
    rail_light, rail_dark = _rail_samples(light), _rail_samples(dark)
    shared = bool(light.min() == dark.min() or light.max() == dark.max())
    if shared:
        return rail_light, rail_dark, True, (
            "the light and dark traces share an extreme value exactly. Two "
            "independently averaged acquisitions do not agree on an extremum to "
            "full precision — that is the digitiser's own rail, so both traces "
            "are saturated, `light - dark` is identically zero wherever they "
            "are, and the transient is subtracted away rather than measured. "
            "Widen the range or reduce the swing and rerun; do not interpret "
            "the charge below")
    worst = max(rail_light, rail_dark)
    if worst > 8:
        return rail_light, rail_dark, False, (
            f"an extreme value repeats {worst} times — a real averaged trace "
            "does not sit on one value, so the window is probably too small "
            "even though the auto-range did not say so")
    return rail_light, rail_dark, False, None


def stage_measure(report: Report, rm, rig_config, run_config, *, averages: int,
                  loops: int, vpre: float, vcoll: float, delay_ns: float,
                  manual_shutter, ask, folder: str,
                  invert_output: bool = False) -> None:
    """Acquire a short transient run through the complete bench chain.

    One point, a few loops and small N_avg test whether the 81150A Sync
    triggers an acquisition through R_s with a photocurrent that integrates
    to plausible Q. A simulator cannot establish that hardware behaviour.
    `--measure` requires a connected device and typed confirmation unless
    `--yes` skips it.
    """

    def measure(c: Check) -> None:
        import numpy as np

        from bace.core.axis import Axis, ScanSpec
        from bace.drivers.agilent81150 import Agilent81150
        from bace.drivers.infiniium import Infiniium
        from bace.experiment.events import RunFinished, StepDone
        from bace.experiment.rig import Rig
        from bace.experiment.transient import run_transient_scan

        scope_res = _wrap(c, rm, rig_config.scope_address, ":SYST:ERR?")
        bias_res = _wrap(c, rm, rig_config.bias_address, ":SYST:ERR?")
        if scope_res is None or bias_res is None:
            c.skip("scope or 81150A not reachable")
            return

        shutter: Any
        if manual_shutter:
            shutter = PromptShutter(ask)
            c.data["shutter"] = "operated by hand"
        else:
            from bace.drivers.shutter import Shutter
            shutter = Shutter(module_id=rig_config.dio_module_id,
                              module_nr=rig_config.shutter_module_nr).open()
            c.data["shutter"] = f"DIO module {rig_config.shutter_module_nr}"

        rig = Rig(bias=Agilent81150(bias_res),
                  scope=Infiniium(scope_res,
                                  sense_resistor_ohm=rig_config.sense_resistor_ohm,
                                  current_sign=rig_config.current_sign,
                                  probe_attenuation=rig_config.probe_attenuation),
                  shutter=shutter, config=rig_config)

        spec = ScanSpec(axis=Axis("vpre", vpre, vpre), vcoll=vcoll,
                        delay_ns=delay_ns, n_loops=loops)
        cfg = type(run_config)(**{**run_config.as_dict(), "n_averages": averages,
                                  "calibrate_trigger": True,
                                  "inverted_output": invert_output})
        c.data["Vpre / Vcoll / delay"] = f"{vpre:g} V / {vcoll:g} V / {delay_ns:g} ns"
        c.data[":OUTP1:POL"] = "INV" if invert_output else "NORM"
        c.data["held between pulses at"] = ("Vpre — stepped to Vcoll (extraction "
                                            "on the step)" if invert_output else
                                            "Vcoll — stepped to Vpre")
        c.data["averages / loops"] = f"{averages} / {loops}"

        # -- trigger-chain read-back before the run ----------------------------
        # `run_transient_scan` configures the 81150A arming source and slope
        # before the pulse shape. `InstrumentState.values` carries their
        # read-back as `bias_arm_source` and `bias_arm_slope`; the recorder
        # stores these in `/config/resolved`. The queries here describe the
        # state before setup. The run folder records the state used for the
        # measurement. A run reads the 33220A polarity without setting it.
        # Checking phase matters: extraction during illumination can still
        # produce plausible Q (manual 2.2).
        for label, query in ((":ARM:SOUR1? (as found, before the run)", ":ARM:SOUR1?"),
                             (":ARM:SLOP? (as found, before the run)", ":ARM:SLOP?"),
                             (":OUTP1:POL? (read back)", ":OUTP1:POL?")):
            try:
                c.data["81150A " + label] = str(bias_res.query(query)).strip()
            except Exception as exc:
                c.data["81150A " + label] = f"unreadable ({type(exc).__name__})"

        led_res = _wrap(c, rm, rig_config.led_address, ":SYST:ERR?")
        if led_res is not None:
            try:
                pol = str(led_res.query(":OUTP:POL?")).strip()
                c.data["33220A :OUTP:POL?"] = pol
                if not pol.upper().startswith("INV"):
                    c.warn(
                        "the 33220A output is NORM, so its Sync goes high when the "
                        "LED is ON and its rising edge means 'light on'. The 81150A "
                        "arms on that rising edge, so extraction would happen "
                        "during illumination rather than after it. BACE needs INV: "
                        "inverting the waveform flips the LED phase while leaving "
                        "the Sync alone (33220A manual p.67), which is what makes a "
                        "rising edge mean 'light off'")
            except Exception as exc:
                c.data["33220A :OUTP:POL?"] = f"unreadable ({type(exc).__name__})"
            finally:
                try:
                    led_res.close()
                except Exception:
                    pass

        from datetime import datetime

        from bace.storage.naming import RunMetadata
        from bace.storage.recorder import RunRecorder, record
        rec = RunRecorder(folder, RunMetadata(sample="benchcheck",
                                              material="FIRSTLIGHT",
                                              temperature_k=None,
                                              started=datetime.now()))

        steps, fin = [], None
        try:
            with quiet(scope_res), quiet(bias_res):
                # The ordinary recorder writes this run, so the same analysis can
                # open its run folder and any other measurement folder.
                for ev in record(run_transient_scan(rig, spec, cfg), rec):
                    if isinstance(ev, StepDone):
                        steps.append(ev)
                    elif isinstance(ev, RunFinished):
                        fin = ev
        finally:
            try:
                shutter.shut()
            except Exception:
                pass
            for r in (bias_res, scope_res):
                try:
                    r.close()
                except Exception:
                    pass

        if fin is None:
            c.fail("the run produced no RunFinished")
            return

        light = np.asarray(steps[0].light.y)
        dark = np.asarray(steps[0].dark.y)
        photo = np.asarray(steps[0].photo)
        c.data["Q per loop (C)"] = [f"{s.q:.5e}" for s in steps]
        c.data["Q mean +- sd (C)"] = f"{fin.q_mean[0]:.5e} +- {fin.q_std[0]:.2e}"
        c.data["points / dt (ns)"] = f"{light.size} / {fin.dt * 1e9:g}"
        c.data["light min/max (A)"] = f"{light.min():.4e} / {light.max():.4e}"
        c.data["dark  min/max (A)"] = f"{dark.min():.4e} / {dark.max():.4e}"
        c.data["photo min/max (A)"] = f"{photo.min():.4e} / {photo.max():.4e}"
        c.data["photo tail, last 10% (A)"] = \
            f"{photo[int(photo.size * 0.9):].mean():.4e}"
        c.data["peak sample index"] = int(np.argmax(np.abs(photo)))
        c.data["peak time in record (ns)"] = round(
            float(np.argmax(np.abs(photo))) * fin.dt * 1e9, 2)
        c.data["t0_int from the field (ns)"] = round(cfg.t0_int_s * 1e9, 2)
        c.data["t_int_width (ns)"] = round(cfg.t_int_width_s * 1e9, 2)
        c.data["trigger_offset_s, sync to field (ns)"] = round(
            rig.config.trigger_offset_s * 1e9, 2)
        t0_rec = steps[0].t0_int_record_s
        t1_rec = steps[0].t1_int_record_s
        c.data["integration window in record time (ns)"] = (
            f"{t0_rec * 1e9:.2f} .. {t1_rec * 1e9:.2f}")
        c.data["record t0 :WAV:XOR? (ns)"] = round(steps[0].light.t0 * 1e9, 2)

        # -- digitiser saturation --------------------------------------------
        # Check both auto-range status and acquired samples. A plausible Q
        # does not establish that the light trace and dark trace fit within
        # the digitiser range; clipping can cancel their difference.
        scope = rig.scope
        c.data["autorange passes"] = getattr(scope, "last_autorange_passes", "?")
        windows = getattr(scope, "last_autorange_windows", [])
        if windows:
            c.data["autorange windows tried"] = [
                f"{r:.4f} @ {o:+.4f} -> {'clipped' if clip else 'fits'}"
                for r, o, clip in windows]
        if scope.clipped:
            c.warn("the auto-range gave up while the light trace was still "
                   "clipped — every charge from this run is an underestimate")

        rail_light, rail_dark, shared_extreme, note = saturation_verdict(light, dark)
        c.data["samples on an extreme (light / dark)"] = f"{rail_light} / {rail_dark}"
        c.data["light and dark share an extreme"] = "yes" if shared_extreme else "no"
        if note:
            c.warn(note)

        # Downsample traces to keep the report small enough to plot.
        step = max(1, light.size // 400)
        c.data["light[::%d]" % step] = [float(f"{v:.5e}") for v in light[::step]]
        c.data["dark[::%d]" % step] = [float(f"{v:.5e}") for v in dark[::step]]
        c.data["photo[::%d]" % step] = [float(f"{v:.5e}") for v in photo[::step]]

        if abs(photo).max() < 3 * float(np.std(photo[int(photo.size * 0.9):])):
            c.warn("the photocurrent peak is within three standard deviations of "
                   "the tail noise — is the light reaching the sample, and is "
                   "the trigger arriving?")
        # Compare the peak with the resolved integration window. `cfg.t0_int_s`
        # is relative to field arrival; the peak is in record time. Comparing
        # them directly mixes time origins.
        if int(np.argmax(np.abs(photo))) * fin.dt < t0_rec:
            c.warn("the peak falls before t0_int, so part of the transient is "
                   "outside the integration window")

        c.data["run folder"] = rec.folder
        c.data["files written"] = [os.path.basename(p) for p in rec.written]

    report.run("first light: one real transient", "measure", measure)
