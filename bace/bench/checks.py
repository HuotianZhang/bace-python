"""The staged checks.

Ordered so that nothing capable of damaging a sample runs before everything
harmless has been tried. Each stage is opt-in past the first two:

    offline     no instruments at all -- does the package work on this machine
    discover    enumerate VISA, *IDN? each configured address. Read-only
    read        read back what each instrument is currently set to. Read-only
    configure   send the configuration commands. Outputs stay OFF
    acquire     one scope acquisition. Needs the trigger to be running
    dio         toggle the shutter and relay lines. Audible; nothing is driven
    outputs     enable instrument outputs. Requires no sample connected
    measure     one tiny real transient scan. Requires a sample

The hypotheses this session is meant to settle are marked in the code with
`HYPOTHESIS:` and reported as `warned` rather than `failed` when the instrument
disagrees -- a disagreement is information, not a fault.
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
    """A short hash over every .py in the package, path and content.

    There is more than one copy of this tree on the bench: the code is edited in
    one folder and executed from another, and on 2026-09-01 the executed copy
    was ten files behind for about an hour without anything saying so. A report
    that carries a fingerprint makes that a one-line comparison instead of an
    hour, and two reports that disagree about a result are immediately
    distinguishable from two reports of the same code.
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
    """Which source file was touched last, and when. Names a partial copy."""
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
    """Everything checkable with no instruments. Run this first, always."""

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
        cfg = RunConfig(n_averages=64, settle_s=0.0, dark_settle_s=0.0)
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
                      record_length=400), sleep=lambda s: None)
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
        light = [x for x in curves if not x.dark][0]
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

        # read_matrix returns (corner, columns, row labels, data): the columns
        # are the time axis and the row labels are V_pre.
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
                      record_length=400), sleep=lambda s: None), rec))
        c.data["folder"] = rec.folder
        c.data["files"] = [os.path.basename(p) for p in rec.written]
        assert rec.written, "the recorder wrote nothing"

    report.run("storage writes a run folder", "offline", storage)


def _resolve_archive(folder: str, need, L) -> str:
    """Accept either a run folder or the directory that holds them.

    The first bench session was pointed at `D:\\DATA\\TDCF-BACE\\data`, which
    contains run folders rather than data files, and the check skipped. Looking
    one level down costs nothing and removes a whole class of wasted trip.
    """
    if any(L.find(folder, k) for k in need):
        return folder
    try:
        subs = sorted(os.path.join(folder, d) for d in os.listdir(folder)
                      if os.path.isdir(os.path.join(folder, d)))
    except OSError:
        return folder
    for sub in reversed(subs):                  # newest name last, try it first
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
    """Return a pyvisa ResourceManager, or None with the reason recorded."""
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
    """`*IDN?` each configured address. Returns role -> identity."""
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
    """Read back what each instrument is currently set to. Sends no settings."""

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
    """The things that are not VISA: the DIO DLL and the two consoles."""
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
            # Prove it rather than infer it: a matching PE header is not the
            # same as a library that opens. A missing dependency, a blocked
            # file, a wrong-arch import in the DLL itself all show up here, and
            # would otherwise surface for the first time mid-run.
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

        # No DLL this interpreter can load. The fix is the 32-bit helper, but
        # that needs a 32-bit Python -- and telling someone to run `py -3-32`
        # without knowing whether one is installed is how a wasted trip to the
        # bench happens. Find out here, in the same report.
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

    def power_console(c: Check) -> None:
        from bace.drivers.newport1918c import ConsolePowerMeter
        m = ConsolePowerMeter(rig_config.power_meter_console, timeout_s=3.0)
        if not m.available():
            c.warn(f"the 1918-C console is not answering at "
                   f"{rig_config.power_meter_console} — start it, or intensity "
                   "readings will be skipped")
            return
        r = m.read()
        c.data["watts"] = r.watts
        c.data["units"] = r.units
        c.data["wavelength_nm"] = r.wavelength_nm
        c.data["saturated"] = r.saturated
        c.data["trustworthy"] = r.trustworthy
        if not r.trustworthy:
            c.warn("the meter reading is not trustworthy (saturated, overrange, "
                   "or not in watts)")

    def temperature_console(c: Check) -> None:
        import json
        import urllib.request
        url = rig_config.temperature_console or "http://127.0.0.1:8331"
        try:
            with urllib.request.urlopen(url + "/api/state", timeout=3.0) as r:
                state = json.loads(r.read().decode())
        except Exception as exc:
            c.skip(f"331 console not answering at {url} ({type(exc).__name__}) "
                   "— not needed yet, this is for the next step")
            return
        for k in ("control_temperature", "temperature", "setpoint", "elapsed_s"):
            if k in state:
                c.data[k] = state[k]
        c.data["keys"] = sorted(state)[:20]

    report.run("DELIB (shutter and relay)", "local", delib)
    report.run("1918-C power meter console", "local", power_console)
    report.run("Lake Shore 331 console", "local", temperature_console)


def _py_launcher_list() -> str:
    """What the Windows `py` launcher says it has. Empty string elsewhere."""
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
    """One `py --list-paths` row as `(tag, executable)`, or None.

    Split on the drive letter rather than on whitespace: the launcher marks its
    default with a free-standing `*`, and install paths contain spaces
    (`C:\\Program Files\\...`). The Python Install Manager also brackets the part
    of a tag you may omit -- report 9 came back `-V:3.14[-64] *` -- and dropping
    the brackets leaves `3.14-64`, which is what you can actually type.
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
    """Interpreters the `py` launcher knows about that are `want`-bit.

    Each entry is `(launcher tag, executable, version)`; the tag is what you can
    actually type (`py -3.12-32`), the executable is the fallback when the tag
    is missing. Every candidate is *run* to read its real pointer size, because
    a `-32` in the tag is a naming convention and not a promise.
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
                    force: bool = False) -> None:
    """Send every configuration command. **No output is enabled here.**

    This is the stage that earns the whole harness: each recovered SCPI string
    is sent once and the error queue is read immediately, so the report comes
    back with an exact list of what the instruments rejected.
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

            # HYPOTHESIS: timebase is ns/div, so 200 -> 2 us full screen and a
            # trigger position of 800 ns. Recovered from the driver's own
            # arithmetic and from the panel note "timebase*10"; never confirmed
            # against the instrument.
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

            # The 5000 -> 4000 question. The first session showed :ACQ:POIN?
            # answers 5000, so the reduction is not there: the record the scope
            # can actually deliver is the time window times the sample rate, and
            # 2 us at 2 GSa/s is 4000 points. Ask the sample rate and say so.
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
            lv = pulse_levels(0.9, -1.0, rig_config.pulse_amp, 88.0,
                              run_config.pulse_width_ns,
                              trigger_offset_s=rig_config.trigger_offset_s)
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
            # The source/sense setup without enabling the output: exactly the
            # commands measure_jsc would send, minus :OUTP ON.
            res.write("*RST")
            res.write(":SOUR:FUNC:MODE VOLT;")
            res.write(":SOUR:VOLT:LEV 0;")
            res.write(":SENS:FUNC 'CURR:DC';")
            res.write(":SENS:CURR:PROT:LEV 0.01;")
            res.write(":SENS:CURR:NPLC 1;")
            res.write(":FORM:ELEM CURR;")
            c.data[":OUTP?"] = str(res.query(":OUTP?")).strip()
            # and the V_oc branch, whose protection command this port corrected
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
            # The 81150A is armed by this generator's Sync, so the two have to
            # agree on the period. Hard-coding 1 kHz here left the LED at twice
            # the rig's 500 Hz after every --configure.
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
    """Refuse to reconfigure an instrument whose output is already enabled.

    The first bench session found the 81150A sitting at `:OUTP1? = 1`, left on
    by the LabVIEW program, and the configure stage changed its levels anyway —
    from 1.263 V to 0.225 V high, which at the device is 5.05 V to 0.9 V, into
    whatever was connected. The stage had only checked *its own* driver's flag,
    which was of course False because this session had never enabled anything.

    Ask the instrument, not the object.
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
    """One real acquisition. Needs whatever normally triggers the scope to be running."""

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
                with quiet(res):             # don't distort the acquisition timing
                    vrange, voffset = s.autorange(rig_config.current_source,
                                                  channel=rig_config.scope_channel,
                                                  verify=True)
                    trace = s.acquire(averages, source=rig_config.current_source,
                                      autorange_first=False, timeout_s=60.0)
            except ScopeError as exc:
                # Not a fault: with nothing driving the sync there is nothing to
                # trigger on. Worth distinguishing from a broken command, so it
                # is a warning with the next thing to try.
                c.warn(f"{exc}. Is the 81150A running and its SYNC connected to "
                       f"{rig_config.trigger_source}? Alternatively the "
                       ":ADER? poll may not work on this firmware — the report "
                       "transcript shows what it returned.")
                c.data[":ADER? replies"] = [x.reply for x in c.exchanges
                                            if "ADER" in x.command][-5:]
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
                # t0_int is measured from the first sample, but the trigger is
                # ~200 ns in, so this is the number that decides whether the
                # integral starts before the transient does.
                after_trigger = (run_config.t0_int_s + trace.t0) * 1e9
                c.data["t0_int, relative to the trigger (ns)"] = round(after_trigger, 1)
            expected_dt = (run_config.timebase_ns_per_div / 1e8) / max(trace.n, 1)
            c.data["dt expected (ns)"] = round(expected_dt * 1e9, 4)
            if abs(trace.dt - expected_dt) > expected_dt * 0.05:
                c.warn("dt does not match full-screen/points — the timebase "
                       "interpretation needs re-checking")
        finally:
            res.close()

    def transfer(c: Check) -> None:
        """Look at the raw bytes of `:WAV:DATA?`.

        The first bench acquisition got a preamble saying 4000 points and then
        an empty array. That can be an indefinite-length block header the parser
        does not accept, a read-termination character inside the binary, or a
        record fetched while the scope was still running. Rather than guess
        again, read the block by hand and report what it actually looks like.
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
    """Find out what DIO module 0 and module 1 actually do — by measurement.

    What is already known, and how well:

    * **Module 0 is the shutter.** Not an inference: `shutter_lv2012.vi` — the
      VI called "shutter", whose only control is `open/close shutter` — hard-codes
      `module nr = 0`, `module ID = 9`, `ch = 0` and a 0.1 s delay.
    * **Module 1 is something else on the same module ID.** `BACE_Mehrdad.vi`
      drives it at four sites, and the label the binary gives it is
      `open/close shutter 2` — *not* "relay". One of those sites sits beside
      `:OUTP %g;` and `:VOLT:OFFS %g;`, which is what made me read it as the
      amplifier/Keithley relay. That reading is mine, not the file's.

    The difference matters: `routing.Relay` treats module 1 as the relay and
    guards it accordingly, and if it is really a second optical shutter then
    that guard protects nothing and the real path switch is somewhere else.

    Listening to it settles nothing anyway — a click does not say whether a line
    switched an optical path or an electrical one. The Keithley does:

        LED on, and photocurrent at the Keithley means the relay is on the
        Keithley *and* the shutter is open.

    So this walks all four states of the two lines and, in each, measures V at
    I = 0 and I at V = 0. Three outcomes are distinguishable, which is what
    breaks the symmetry between the two lines:

    ==========================  ==================  ====================
    state                       V at I = 0          I at V = 0
    ==========================  ==================  ====================
    connected, light            V_oc (~0.9 V)       J_sc (mA) <- the one
    connected, dark             ~0 V                ~0
    not connected               the voltage         ~0
                                compliance rail
    ==========================  ==================  ====================

    Nothing is forced into the device: `measure_voc` sources 0 A and
    `measure_jsc` sources 0 V, and each enables the output only for its own
    reading. Both generator outputs are off for the whole stage — moving a path
    switch under load is the one mistake here that costs hardware — and are put
    back at the end.
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
"""Above this at `DIO_PROBE_V`, the device is on this path.

Two decades above the open-input leakage measured on the rig (~1.5e-11 A) and
orders below any real cell's forward current. If `smallest probe current` in a
report ever approaches this, the constant is the thing to revisit.
"""

DIO_PROBE_V = 0.5
"""A small forward bias, used only to ask whether the device is on this path.

Below the ~0.92 V V_oc the cell shows, so it is an ordinary point on its own
J-V curve rather than a stress, and the recipe's compliance still applies.
"""

DIO_SETTLE_S = 0.3
"""After a line moves. `shutter_lv2012.vi` waits 0.1 s; a relay and a cell
recovering from the switch want more, and this runs four times, once."""


def _walk_dio_states(c: Check, make, rig_config, rm, confirm, notes: dict, *,
                     smu_config=None, listen: bool = False) -> None:
    """All four states of the two lines, each measured on the Keithley."""
    from bace.drivers.keithley2400 import Keithley2400, SourceMeterConfig

    # The recipe's compliance, not the driver's 50 mA default. `run.toml` sets
    # 10 mA on this bench and `check_smu_limits` enforces a ceiling on it; a
    # diagnostic has no business quietly running at five times that.
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

    # Was the light even on? Without it no state shows photocurrent and the
    # whole check comes back inconclusive -- so say so in the report rather
    # than leaving it to be guessed afterwards.
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
    """The 33220A's output state and levels, read from the instrument."""
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
    """Read the four measurements back as roles, and say when they do not.

    Two independent signatures, and both are needed:

    * **photocurrent** (I at V = 0) marks the one state where the device is on
      the Keithley *and* light reaches it. Huotian's rule, and it fixes the
      *values* of the two lines but not which line is which -- both are set the
      same way in that state, so the pair is symmetric.
    * **forward conduction** (I at `DIO_PROBE_V`) marks every state where the
      device is on the Keithley at all, light or no light. Flipping one line at
      a time from the photocurrent state then breaks the symmetry: the flip that
      stops conduction moved the electrical path; the flip that keeps it but
      loses the photocurrent moved the light.

    The first version of this used the SMU's voltage compliance as the
    disconnect signature and was simply wrong -- see `stage_dio`.
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

    # Conducting at a small forward bias means the device is on this path.
    #
    # This has to be an *absolute* threshold, not a relative one. The tempting
    # move is to split the four readings at their largest gap, but that fails
    # exactly where it matters: if both lines are optical the device conducts in
    # all four states and the largest gap falls between the lit state and the
    # dark ones, which would report three disconnections that never happened.
    # What separates connected from disconnected is an instrument property, not
    # a device one -- an open input reads the SMU's leakage, ~1.5e-11 A on this
    # rig on 2026-09-01, while any real cell at +0.5 V conducts orders above it.
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

    # modules[0] is whatever rig.toml calls the shutter. If it does not behave
    # like one, the config is upside down and every run would open the relay
    # where it meant to open the shutter.
    if roles[modules[0]] != "switches the light (shutter)":
        c.warn(f"module {modules[0]} is configured as the shutter but behaves "
               f"as the path switch, and module {modules[1]} the other way "
               "round. rig.toml has them the wrong way up — swap "
               "`shutter_module_nr` and `relay_module_nr` before any run.")


SOURCES = (("81150A", "bias_address", ":OUTP1", ":OUTP1?"),
           ("Keithley 2400", "sourcemeter_address", ":OUTP", ":OUTP?"))
"""The two things that can put a voltage on the device node, and how each
answers for and controls its output. The 33220A drives the LED, not the node,
so it is deliberately absent."""


@contextlib.contextmanager
def _outputs_off(rm, rig_config, notes: dict, restore: bool = True):
    """Turn both sources on the device node off for the duration, then back.

    The relay cannot be moved safely while either is driving, and on this bench
    the outputs cannot be switched off by hand -- the LabVIEW program owns them.
    So the harness does it, and the ordering is the safety argument:

    * **off, then verify by reading the instrument.** A write that was accepted
      is not the same as an output that is off; if either still answers 1 this
      raises and nothing moves.
    * **restore only what was on**, and only after the caller has finished. An
      output that was already off stays off.
    * **`notes` records every state change**, so the report says exactly what
      the harness did to the rig rather than leaving it to be discovered.

    The caller may set `notes["dio restored"] = False` to veto the restore --
    re-enabling a source into a signal path that could not be put back where it
    was found is precisely the move this whole interlock exists to prevent.
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
    """Last component of a path that may use either separator.

    `os.path.basename` splits on the *host's* separator, so a Windows path read
    on any other machine comes back whole -- which is exactly what happens when
    these checks are exercised in CI.
    """
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _dio_backend(rig_config):
    """How to reach the DIO lines on *this* machine, or why we cannot.

    Deditec ships DELIB under two names -- 32-bit `delib.dll` in SysWOW64 and
    64-bit `delib64.dll` in System32 -- and `ctypes` can load only the one
    matching the interpreter. Which is installed is a property of the machine,
    so `drivers.delib` decides and this only has to say what happened. Neither
    present is not a fault to report as a failure: it is a deployment fact with
    two fixes, and the check should name the one to apply rather than raise
    `OSError: [WinError 193]` twice and call it a stage.

    Returns `(factory, description, reason_if_unavailable)`.
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
            # Prove it opens. A matching PE header is not the same as a library
            # that loads: a missing dependency or a blocked file fails here.
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
    """Enable each output briefly, with **no sample connected**.

    Everything before this point is either read-only or leaves the outputs off.
    This is the first stage that could put a voltage on the device node, which
    is why it is gated behind an explicit flag and a typed confirmation.
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
            # The 81150A is armed by this generator's Sync, so the two have to
            # agree on the period. Hard-coding 1 kHz here left the LED at twice
            # the rig's 500 Hz after every --configure.
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
    """A shutter operated by a person.

    `delib.dll` is 32-bit, so a 64-bit bench session cannot drive the real
    shutter without the helper process running. Rather than skip first light
    entirely, this asks the operator to open and close it. Four prompts for a
    one-point, two-loop run — tedious, and it does prove the acquisition and the
    subtraction end to end.
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
    """How many samples of `a` sit on its own most extreme value.

    A digitiser that runs out of window returns the same code for every sample
    past it, so a rail shows up as an extremum repeated dozens of times. A real
    averaged analogue trace essentially never repeats its extremum: with 16
    hardware averages the odds of two samples landing on the same 16-bit code
    *at the very edge of the distribution* are negligible.

    This is the check that was missing on 2026-09-01. In report 033251 the light
    and dark traces of the INV polarity run shared a minimum of -5.83608e-03 A,
    repeated about seventy times in four thousand samples. Both were railed, so
    `light - dark` was identically zero across the transient and the peak the
    run was judged on was the residue of the baseline correction.
    """
    import numpy as _np

    a = _np.asarray(a)
    if a.size == 0:
        return 0
    return int(max((a == a.min()).sum(), (a == a.max()).sum()))


def saturation_verdict(light, dark) -> tuple[int, int, bool, str | None]:
    """Was the light/dark pair taken inside the digitiser's window?

    Returns `(rail_light, rail_dark, shared_extreme, warning or None)`.

    Two independent signals, and the second is the decisive one. The auto-range
    reports what it *concluded*; this reports what the samples *show*, which is
    what catches a window set somewhere else, or a loop that stopped early.

    A shared extremum is close to proof. Two independently averaged acquisitions
    do not agree on their most extreme sample to full floating-point precision
    unless both are resting on the same hardware limit — and when they are,
    `light - dark` is identically zero across that stretch, so the transient is
    subtracted away rather than measured.
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
    """One tiny real transient scan — first light for the whole chain.

    A single axis point, a couple of loops, few averages. Enough to answer the
    only question the simulator cannot: does an acquisition triggered by the
    real 81150A sync, through the real sense resistor, produce a photocurrent
    that integrates to a sensible charge.
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

        # -- the trigger chain, as found before the run ------------------------
        # States this run depends on, read before it starts. Until 2026-09-02
        # no run set any of them: the 81150A's arming source and slope and the
        # 33220A's output polarity were inherited from whatever the last
        # session left behind, and none reached the output -- which is how a
        # run that extracts *during* illumination looks exactly like one that
        # extracts after it. Since then `run_transient_scan` arms the 81150A
        # itself (`configure_trigger`, before the pulse shape) and reads the
        # arming back into `InstrumentState.bias_arm_source` / `bias_arm_slope`,
        # which the recorder writes to /config/resolved. So the two :ARM lines
        # below say what the front panel held *before* this run, not what the
        # run ran under; for that, open the run folder this stage writes. The
        # 33220A polarity is still only read, never written, by a run. See
        # claude/bace-trigger-chain.md.
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
                # Recorded through the ordinary writer, so this first-light run
                # produces a real run folder that can be opened by the same
                # analysis as any other measurement.
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
        c.data["t0_int (ns)"] = round(cfg.t0_int_s * 1e9, 2)
        c.data["t0_int reference"] = cfg.t0_int_reference
        from bace.experiment.transient import resolve_t0_int
        t0_rec = resolve_t0_int(cfg, steps[0].light.t0)
        c.data["t0_int in record time (ns)"] = round(t0_rec * 1e9, 2)
        c.data["record t0 :WAV:XOR? (ns)"] = round(steps[0].light.t0 * 1e9, 2)

        # -- was the window big enough to hold the trace? --------------------
        # `stage_acquire` has reported this since the first bench session.
        # `stage_measure` did not, which is why report 033251 could compare two
        # output polarities on a pair of railed traces and produce a charge that
        # looked like the other one. Two independent signals: what the
        # auto-range concluded, and what the samples show.
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

        # Downsampled traces, so the report can be plotted without being huge.
        step = max(1, light.size // 400)
        c.data["light[::%d]" % step] = [float(f"{v:.5e}") for v in light[::step]]
        c.data["dark[::%d]" % step] = [float(f"{v:.5e}") for v in dark[::step]]
        c.data["photo[::%d]" % step] = [float(f"{v:.5e}") for v in photo[::step]]

        if abs(photo).max() < 3 * float(np.std(photo[int(photo.size * 0.9):])):
            c.warn("the photocurrent peak is within three standard deviations of "
                   "the tail noise — is the light reaching the sample, and is "
                   "the trigger arriving?")
        # Against the *resolved* window, not `cfg.t0_int_s` — that number is in
        # whatever units `t0_int_reference` names, and comparing a record-time
        # peak against a trigger-referenced setting compares two different clocks.
        if int(np.argmax(np.abs(photo))) * fin.dt < t0_rec:
            c.warn("the peak falls before t0_int, so part of the transient is "
                   "outside the integration window")

        c.data["run folder"] = rec.folder
        c.data["files written"] = [os.path.basename(p) for p in rec.written]

    report.run("first light: one real transient", "measure", measure)
