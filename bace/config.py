"""Loading `rig.toml` and `run.toml`.

Two files because they fail differently. A stale `rig.toml` silently rescales
every charge — the sense resistor and the amplifier gain are multiplicative and
leave no trace in the data. A stale `run.toml` merely repeats a measurement you
did not mean to repeat, which you notice immediately. So the bench and the
recipe are edited, versioned and reviewed separately, and only the recipe is
expected to change from run to run.

Unknown keys are an error, not a shrug. A typo in `sense_resistor_ohm` that
falls back to the default would produce data wrong by a factor of five with no
symptom, and the whole point of a config file is that someone can read what the
run actually used.
"""
from __future__ import annotations

import tomllib
from dataclasses import fields
from pathlib import Path
from typing import Any

from .core.axis import Axis, ScanSpec
from .core.illumination import LedDrive
from .drivers.keithley2400 import SourceMeterConfig
from .experiment.rig import RigConfig
from .experiment.transient import RunConfig
from .storage.naming import RunMetadata


class ConfigError(ValueError):
    pass


def _load(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"no such config file: {p}")
    with open(p, "rb") as fh:
        return tomllib.load(fh)


def _check(table: dict, allowed: set[str], where: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(
            f"unknown key(s) in [{where}]: {', '.join(sorted(unknown))}. "
            "Refusing to fall back to defaults — a typo here is a silent "
            "rescaling of the result."
        )


def load_rig(path: str | Path = "rig.toml") -> RigConfig:
    d = _load(path)
    e, sc = d.get("electrical", {}), d.get("scope", {})
    smu, led = d.get("sourcemeter", {}), d.get("led", {})
    pm, dio, bias = d.get("power_meter", {}), d.get("dio", {}), d.get("bias", {})
    timing, t = d.get("timing", {}), d.get("temperature", {})

    _check(e, {"sense_resistor_ohm", "pulse_amp", "probe_attenuation",
               "trigger_offset_s", "current_sign"}, "electrical")
    _check(timing, {"light_path_delay_ns"}, "timing")
    _check(sc, {"address", "current_source", "channel", "trigger_source",
                "trigger_positive"}, "scope")
    _check(dio, {"module_id", "shutter_module_nr", "relay_module_nr", "channel",
                 "dll_path"}, "dio")

    _check(smu, {"address", "max_current_compliance_a",
                 "max_voltage_compliance_v"}, "sourcemeter")
    # Both of these gained keys when the two instruments moved in-process, and
    # both have a `console` that now means "do NOT open the device here". A
    # typo in either is a rig that silently has no meter or no cryostat.
    _check(pm, {"wavelength_nm", "on_beam_splitter", "dll_path", "console",
                "averaging", "digital_filter"},
           "power_meter")
    _check(t, {"address", "max_setpoint_k", "control_loop", "console"},
           "temperature")

    try:
        cfg = RigConfig(
            sense_resistor_ohm=e.get("sense_resistor_ohm", 5.192),
            # float(): TOML reads `-1` as an integer, and the sign is a multiplier
            # on a float array, not a count.
            current_sign=float(e.get("current_sign", -1.0)),
            pulse_amp=e.get("pulse_amp", 4.0),
            probe_attenuation=e.get("probe_attenuation", 1.0),
            trigger_offset_s=e.get("trigger_offset_s", 0.0),
            light_path_delay_ns=float(timing.get("light_path_delay_ns", 0.0)),
            current_source=sc.get("current_source", "CHAN2"),
            scope_channel=sc.get("channel", 2),
            trigger_source=sc.get("trigger_source", "CHAN3"),
            trigger_positive=sc.get("trigger_positive", True),
            led_threshold_v=led.get("threshold_v", 1.0),
            dio_module_id=dio.get("module_id", 9),
            shutter_module_nr=dio.get("shutter_module_nr", 0),
            relay_module_nr=dio.get("relay_module_nr", 1),
            dio_dll_path=dio.get("dll_path", ""),
            scope_address=sc.get("address", RigConfig.scope_address),
            bias_address=bias.get("address", RigConfig.bias_address),
            sourcemeter_address=smu.get("address", RigConfig.sourcemeter_address),
            led_address=led.get("address", RigConfig.led_address),
            power_meter_wavelength_nm=pm.get("wavelength_nm", 530.0),
            power_meter_dll=pm.get("dll_path", RigConfig.power_meter_dll),
            power_meter_console=pm.get("console", RigConfig.power_meter_console),
            power_meter_averaging=bool(pm.get("averaging", RigConfig.power_meter_averaging)),
            power_meter_digital_filter=int(pm.get("digital_filter",
                                                  RigConfig.power_meter_digital_filter)),
            temperature_address=t.get("address", RigConfig.temperature_address),
            temperature_max_setpoint_k=t.get("max_setpoint_k",
                                             RigConfig.temperature_max_setpoint_k),
            temperature_control_loop=t.get("control_loop",
                                           RigConfig.temperature_control_loop),
            temperature_console=t.get("console", RigConfig.temperature_console),
            max_current_compliance_a=smu.get("max_current_compliance_a", 0.05),
            max_voltage_compliance_v=smu.get("max_voltage_compliance_v", 5.0),
        )
    except ValueError as exc:                 # RigConfig's own validation
        raise ConfigError(str(exc)) from exc
    if cfg.shutter_module_nr == cfg.relay_module_nr:
        raise ConfigError(
            f"shutter and relay are both on DIO module {cfg.shutter_module_nr}. "
            "Driving the shutter line as the relay would leave the device "
            "connected to whichever source the relay happens to be set to."
        )
    if cfg.relay_module_nr == 0:
        # `drivers.routing.BiasRouter` refuses module 0 as well; refused here
        # it is a config error with the file named, not a traceback from the
        # service's assembly.
        raise ConfigError(
            "[dio] relay_module_nr = 0: module 0 is the shutter (measured on the "
            "rig 2026-09-01), not the relay. The relay is module 1."
        )
    return cfg


def load_run(path: str | Path = "run.toml"
             ) -> tuple[ScanSpec, RunConfig, LedDrive, SourceMeterConfig, RunMetadata, dict]:
    """Returns (scan spec, run config, LED drive, SMU config, metadata, extras).

    `extras` carries the few values that belong to no dataclass — `v_sat` for
    the SourceMeter, and `store_shots` for the recorder.
    """
    d = _load(path)
    ax, pin = d.get("axis", {}), d.get("pinned", {})
    acq, ill, sam = d.get("acquisition", {}), d.get("illumination", {}), d.get("sample", {})

    _check(ax, {"name", "start", "stop", "step", "centre_on_voc"}, "axis")
    _check(pin, {"vpre", "vcoll", "delay_ns"}, "pinned")

    axis = Axis(name=ax.get("name", "vpre"), start=ax.get("start", 0.0),
                stop=ax.get("stop", 0.0), step=ax.get("step", 0.0),
                centre_on_voc=ax.get("centre_on_voc", False))
    spec = ScanSpec(axis=axis, vpre=pin.get("vpre", 0.0),
                    vcoll=pin.get("vcoll", -1.0),
                    delay_ns=pin.get("delay_ns", 90.0),
                    n_loops=acq.get("n_loops", 1))

    if "t0_int_reference" in acq:
        raise ConfigError(
            "[acquisition] t0_int_reference is gone: the integration window is "
            "always measured from the field's arrival at the device (`:PULS:DEL1` "
            "plus the rig's trigger_offset_s), so it travels with the delay. "
            f"This file says {acq['t0_int_reference']!r} with t0_int_s = "
            f"{acq.get('t0_int_s', 'unset')}. Convert: from `pulse`, subtract the "
            "rig's trigger_offset_s; from `trigger`, subtract delay_ns and "
            "trigger_offset_s; from `record`, also subtract where the trigger sits "
            "in the record (timebase_ns_per_div, at :TIM:POS of four divisions). "
            "docs/integration-window.md has the worked numbers.")
    known = {f.name for f in fields(RunConfig)}
    _check(acq, known | {"n_loops", "store_shots"}, "acquisition")
    try:
        run = RunConfig(**{k: v for k, v in acq.items() if k in known})
    except ValueError as exc:                 # RunConfig's own validation
        raise ConfigError(f"[acquisition] {exc}") from exc

    drive = LedDrive(level=ill.get("level_v", 1.0),
                     low_level=ill.get("low_level_v", 0.4),
                     frequency_hz=run.pulse_frequency_hz,
                     duty_percent=run.duty_percent,
                     threshold_v=1.0)

    # Typed, and nothing on this path can make it anything else: `tools/scan.py`
    # opens no temperature controller. The service is where a number can be
    # settled or read, and it says so there.
    typed_k = sam.get("temperature_k")
    meta = RunMetadata(sample=sam.get("sample", ""), material=sam.get("material", ""),
                       pixel=sam.get("pixel", ""),
                       temperature_k=typed_k,
                       temperature_how="typed" if typed_k is not None else "",
                       led_drive_v=drive.level,
                       offset_corrected=run.offset_correct,
                       operator=sam.get("operator", ""),
                       comment=sam.get("comment", ""))

    smu_table = d.get("sourcemeter", {})
    smu_known = {f.name for f in fields(SourceMeterConfig)}
    _check(smu_table, smu_known, "sourcemeter")
    smu = SourceMeterConfig(**{k: v for k, v in smu_table.items() if k in smu_known})

    extras = {"v_sat": ill.get("v_sat", -1.0),
              "store_shots": acq.get("store_shots", False)}
    return spec, run, drive, smu, meta, extras


def check_smu_limits(smu: SourceMeterConfig, rig: RigConfig) -> None:
    """Refuse a run whose compliance exceeds the bench ceiling.

    The working compliance is adjustable because it depends on the pixel. The
    ceiling is not, because no measurement recipe should be able to authorise
    a current that damages a device — and a typo in a run file is a much more
    likely route to 1 A than a deliberate decision.
    """
    if smu.current_compliance_a > rig.max_current_compliance_a:
        raise ConfigError(
            f"run asks for {smu.current_compliance_a:g} A compliance but the bench "
            f"ceiling in rig.toml is {rig.max_current_compliance_a:g} A. Raise the "
            "ceiling deliberately if the device really tolerates it."
        )
    if smu.voltage_compliance_v > rig.max_voltage_compliance_v:
        raise ConfigError(
            f"run asks for {smu.voltage_compliance_v:g} V compliance but the bench "
            f"ceiling is {rig.max_voltage_compliance_v:g} V."
        )
