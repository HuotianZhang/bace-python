"""The catalogue defines runnable modules and resolves their parameters.

The `/modules` response and `/runs` execution share one table, so the console's
`n_averages` is the value passed to `RunConfig`. Each module has a `ModuleSpec`
and `ParamSpec` entries. `params.specs_from_dataclass` reads most defaults from
the dataclasses to prevent drift. `Catalogue.build` converts resolved values
into `Axis`, `ScanSpec`, `RunConfig`, `JVConfig`, `LedDrive` and a recorder,
then returns the event generator.

The builder refuses missing V_oc sources, mismatched LED levels, unsafe LED
levels, invalid axis geometry and compliance above the bench ceiling before
touching an instrument. These checks prevent plausible files with an axis
centred on the wrong V_oc or Q measured under the wrong illumination.
`ModuleError` names the module and parameter for the console's field error.

Layers, lowest first: dataclass defaults, `run.toml` through `_TOML`, last
used from the journal, edited in this session, then inherited or derived
pipeline values on a copy. `Catalogue.param_set` builds a separate `ParamSet`
on each call, so pipeline bindings cannot leak into the bench card
(`docs/service-contract.md`, section 5).

Measurement modules keep the LED generator running: `bace` leaves the 33220A
pulsing and J-V leaves it at DC. The shutter shuts on unwind. Cycling the
generator loses its thermal steady state and requires another settle.
After DC changes to pulse, `_settle_led` waits for stable power behind the
open shutter. A 2 s wait alone is insufficient on this bench.

FastAPI is not imported here. Sleeps use `RunContext.sleep`, so `--fast`
and tests can run a 20-loop scan in seconds."""
from __future__ import annotations

import dataclasses
import math
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Iterable, Iterator, Mapping

import numpy as np

from ..config import ConfigError, check_smu_limits
from ..core.axis import Axis, AxisError, ScanSpec, voc_flags
from ..core.illumination import IlluminationError, LedDrive, assert_axis_centre
from ..core.process import ChargeAccumulator, RunningAverage
from ..core.pulses import pulse_levels
from ..drivers.keithley2400 import SourceMeterConfig
from ..experiment import events as E
from ..experiment.jv import (JVConfig, JVCurveDone, illumination_state, run_jv)
from ..experiment.rig import Rig, RigConfig
from ..experiment.transient import RunConfig, run_transient_scan
from ..params import (ParamError, ParamSet, ParamSpec, Source, field_docs,
                      specs_from_dataclass, toml_layer)
from ..storage import jv as jv_storage
from ..storage.jv import JVRecorder
from ..storage.naming import RunMetadata
from ..storage.recorder import RunRecorder, record
from .rigs import apply_led, apply_shutter, power_reading
from .temperature import settle

DEFAULT_SHOT_S = 0.8
"""The default shot cost applies when the journal has no measured interval.

Bench data at N_avg = 20 support this approximate value. The cost model
reports `t_shot_source = "default"` to distinguish it from a measured interval."""

JV_POINT_OVERHEAD_S = 0.05
"""The Keithley sweep overhead adds this many seconds per point beyond settling."""

VOC_LEVEL_TOLERANCE_V = 1e-9
"""The V_oc source and pulse LED levels must agree within this tolerance.

`core.illumination.assert_axis_centre` uses the same tolerance. The coupling
requires equality; this allowance covers floating-point rounding only."""

LED_SETTLE_POLL_S = 0.5
"""The LED settle poll interval is in seconds.

Elapsed time is polls times this interval, so `--fast` takes no wall time
and tests can determine the wait exactly."""

LED_SETTLE_SPAN_S = 10.0
"""The LED settle requires this many seconds of stable meter readings.

Measured on this bench, a three-reading, 1.5 s window can pass while power
falls from 20.8 to 17.0 µW over 40 s. A short window misses slow thermal
drift. The 10 s span provides a longer stability check."""

MODULE_NAMES: tuple[str, ...] = ("jv", "jv_bace", "bace", "light", "power",
                                 "temperature", "park", "wait", "note")

RETIRED: dict[str, str] = {
    "jv_dark": "split into two because it did two jobs: it swept the "
               "SourceMeter *and* made the bench dark. Use a `light` node with "
               "`shutter = shut` before a `jv`, which is exactly what it did; "
               "or `jv` on its own to sweep under the light as it is found.",
}
"""Unsupported module names carry explicit replacement instructions.

A tree naming one is refused rather than rewritten. Replacing `jv_dark`
requires two nodes; automatic translation changes tree shape, node paths and
folder names. Replacing it with `jv` alone changes a dark sweep into a sweep
under whatever illumination is present. The operator must edit the recipe.

Saved recipes persist on disk independently of the catalogue. These
instructions explain the refusal instead of exposing an unhelpful `KeyError`."""

SAMPLE_KEYS: frozenset[str] = frozenset(
    {"sample", "material", "pixel", "temperature_k", "operator", "comment"})
"""The sample keys are the `RunMetadata` fields read by `config.load_run`.

They describe the session rather than module parameters."""


def coerce_sample(values: Mapping[str, Any]) -> dict:
    """The sample coercer validates and types one `[sample]` block.

    The catalogue and `Session.set_sample` share it so file and console edits
    receive the same refusal. Fields are strings except `temperature_k`, which
    is a float or absent. `RunMetadata` requires temperature as a number so
    HDF5 readers can compare the attribute numerically."""
    unknown = sorted(set(values) - SAMPLE_KEYS)
    if unknown:
        raise ValueError(f"[sample]: unknown key(s) {', '.join(unknown)}; "
                         f"accepted: {', '.join(sorted(SAMPLE_KEYS))}")
    out: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        if key == "temperature_k":
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"[sample] temperature_k: {value!r} is not a number") from None
        else:
            out[key] = str(value)
    return out


def _read_led_state(led) -> dict[str, str]:
    """The LED read-back returns generator state as strings.

    Fields are `output`, `mode`, `frequency_hz`, `high_v`, `low_v`, `polarity`
    and `sync_output`. An unanswered query gives `?`. Real drivers expose
    `read_state` outside the protocol. Drivers without it use cached flags,
    which represent the simulator's shared state."""
    read = getattr(led, "read_state", None)
    state: dict = {}
    if callable(read):
        try:
            state = dict(read())
        except Exception:                               # noqa: BLE001
            state = {}
    out = {}
    on = state.get("output", None) if state else bool(getattr(led, "output_enabled", False))
    out["output"] = "?" if on is None else ("ON" if on else "OFF")
    out["mode"] = str(state.get("mode", getattr(led, "mode", "?")) or "?")
    for key in ("frequency_hz", "high_v", "low_v"):
        v = state.get(key)
        out[key] = "?" if v is None else f"{float(v):g}"
    pol = state.get("polarity")
    if pol is None:
        try:
            pol = led.polarity()
        except Exception:                               # noqa: BLE001
            pol = "?"
    out["polarity"] = str(pol or "?").upper()
    # The 33220A Sync output (`OUTP:SYNC?`) arms the 81150A, whose Sync
    # triggers the scope. Sync has an independent front-panel control;
    # no other chain state reveals it is off. Only the real driver queries it.
    sync = state.get("sync_output", None) if state else None
    out["sync_output"] = "?" if sync is None else ("ON" if sync else "OFF")
    # The 33220A rejects a high level below its current low level or a
    # width exceeding the period with `-221 Settings conflict`. Draining
    # the error queue exposes this otherwise silent refusal; the run path
    # does not read the queue.
    errors = getattr(led, "errors", None)
    if callable(errors):
        try:
            out["errors"] = "; ".join(str(e) for e in errors()) or ""
        except Exception:                               # noqa: BLE001
            out["errors"] = "?"
    return out


def _led_problems(state: dict[str, str], wanted: dict) -> list[str]:
    """The LED check reports contradictions between read-back and requested state.

    `?` is not a contradiction: an unqueryable simulator field does not mean
    the generator refuses the setting."""
    problems = []
    if state.get("output") == "OFF":
        problems.append("output is OFF (:OUTP? = 0)")
    if state.get("sync_output") == "OFF":
        problems.append("Sync output is OFF (OUTP:SYNC? = 0): nothing arms the 81150A")
    mode = state.get("mode", "?").upper()
    if mode not in ("PULSE", "?", "PULS"):
        problems.append(f"shape is {mode}, not PULSE: a DC output has no Sync edge to arm the 81150A")
    freq = state.get("frequency_hz", "?")
    if freq != "?" and abs(float(freq) - float(wanted["frequency"])) > 1.0:
        problems.append(f"frequency reads {freq} Hz against {wanted['frequency']:g} Hz asked")
    # `Catalogue._build_bace` writes polarity before the pulse, so it is
    # not refused here. Read-back records which edge arms the run.
    errors = state.get("errors", "")
    if errors and errors != "?":
        problems.append(f"the generator rejected a command: {errors}")
    return problems


class ModuleError(ValueError):
    """A module error identifies a refused module or parameter.

    The message starts with the module name and, for a field error, the
    parameter name. The console uses these names to attach the error."""


# -- the shared interface ---------------------------------------------------
@dataclass(frozen=True)
class ModuleSpec:
    """A module specification describes one entry for the pipeline and routes."""

    name: str
    title: str
    kind: str
    """The kind distinguishes measurement bus jobs, observer readings and utilities."""
    status: str
    """The status is `built`, `partial` or `not wired` on the console card."""
    relay: str | None
    """The relay requirement is `dc` or `transient` when a module needs that side.

    The executor marks a transition whenever the requirement changes."""
    provides_voc: bool
    """The V_oc provider flag identifies a light curve used to centre `bace`."""
    needs_voc_param: str | None
    """The named boolean parameter requires a V_oc source when enabled."""
    led_params: tuple[str, ...]
    """The illumination loop owns these parameters and forbids module overrides."""
    groups: tuple[str, ...]


@dataclass(frozen=True)
class VocSource:
    """A V_oc source records its value and illumination.

    `led_v` is the drive level in V at which V_oc is measured and against which
    coupling is checked. `how` is `jv_bace`, `measure_dc` or `typed`. Only `typed`
    is not a measurement, so the validator warns on it."""

    value: float
    led_v: float
    run_id: str
    node_path: str
    how: str
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _never() -> bool:
    return False


@dataclass
class RunContext:
    """The run context supplies bindings and callbacks to a module.

    `led_v` and `led_low_v` are illumination-loop bindings, or `None` outside a
    loop. `voc` is the V_oc source in scope; `bace` replaces it with the source
    it uses. `temperature_k` is the settled or typed temperature in K.
    `temperature_how` and `temperature_source` carry the `Settled` provenance,
    so a requested temperature is distinguishable from a reading.

    `sleep` is a no-op under `--fast`; `abort` is the worker's flag. `resolved`
    seeds the recorder's read-back values. `on_data` receives full arrays for
    `/runs/{id}/data`. `folders` collects recorder folders for the executor.

    `wait_for_operator(detail, on_poll=callable)` belongs to the job and blocks
    while waiting for the operator. It must call `on_poll` every few hundred
    milliseconds so the 331 remains readable during a pause. `emit` sends events
    while that wait blocks. `stop_mode` identifies `worker.StopMode` or `None`,
    so a settle distinguishes abort from `after_shot`. The executor supplies
    `emit` and `stop_mode` from the job when the factory leaves them unset."""

    run_id: str
    node_path: str
    out_folder: str
    metadata: RunMetadata
    led_v: float | None = None
    led_low_v: float | None = None
    voc: VocSource | None = None
    temperature_k: float | None = None
    temperature_how: str = ""
    temperature_source: str = ""
    sleep: Callable[[float], None] = time.sleep
    abort: Callable[[], bool] = _never
    resolved: dict[str, str] = field(default_factory=dict)
    on_data: Callable[[str, dict], None] | None = None
    wait_for_operator: Callable[..., dict] | None = None
    folders: list[str] = field(default_factory=list)
    emit: Callable[[E.Event], None] | None = None
    stop_mode: Callable[[], str | None] | None = None

    def temperature(self) -> tuple[float | None, str, str]:
        """The temperature metadata returns `(kelvin, how, source)` together.

        A temperature-node binding takes precedence over the session's typed value.
        A folder such as `220K` cannot distinguish a requested setpoint from a
        settled value. `how` records that distinction."""
        if self.temperature_k is not None:
            return self.temperature_k, self.temperature_how, self.temperature_source
        return (self.metadata.temperature_k, self.metadata.temperature_how,
                self.metadata.temperature_source)


# -- the specs --------------------------------------------------------------
RUN_CHOICES: dict[str, tuple[str, ...]] = {
    "dark_reference": ("translated", "same"),
    "light_shutter": ("open", "shut"),
    "offset_region": ("tail", "pre-trigger"),
    "output_polarity": ("auto", "NORM", "INV", "leave"),
    "trigger_sweep": ("AUTO", "TRIG"),
}
"""The catalogue constraints match `RunConfig.__post_init__` and the scope driver.

The dataclass validates these constraints in code rather than annotations,
so the catalogue declares them explicitly."""

RUN_UNITS: dict[str, str] = {
    "timebase_ns_per_div": "ns/div", "t0_int_s": "s", "t_int_width_s": "s",
    "pulse_width_ns": "ns",
    "pulse_frequency_hz": "Hz", "duty_percent": "%", "settle_s": "s",
    "dark_settle_s": "s", "shutter_settle_s": "s", "acquisition_timeout_s": "s",
}

RUN_GROUPS: dict[str, str] = {
    **{f: "acquisition" for f in ("n_averages", "timebase_ns_per_div", "record_length",
                                  "read_intensity", "acquisition_timeout_s",
                                  "light_shutter", "alternate_order")},
    **{f: "processing" for f in ("t0_int_s", "t_int_width_s", "offset_correct",
                                 "offset_region", "invert_polarity", "dark_reference")},
    **{f: "timing" for f in ("settle_s", "dark_settle_s", "shutter_settle_s",
                             "pulse_width_ns", "pulse_frequency_hz", "duty_percent")},
    **{f: "trigger" for f in ("external_trigger", "trigger_slope_positive",
                              "trigger_sweep", "calibrate_trigger")},
    **{f: "output" for f in ("output_polarity", "inverted_output")},
}
"""The field groups place every `RunConfig` field under a console heading.

Catalogue construction refuses an ungrouped field so it cannot appear on
the card without a label."""

SMU_UNITS: dict[str, str] = {
    "current_compliance_a": "A", "voltage_compliance_v": "V", "settle_jsc_ms": "ms",
    "settle_voc_ms": "ms", "settle_jsat_ms": "ms",
}

AXIS_DOCS: dict[str, str] = {
    "axis_name": "The scan parameter -- which quantity the scan range steps through: "
                 "vpre (BACE), delay_ns (TDCF) or vcoll (field dependence). The other "
                 "two stay pinned at the values below.",
    "axis_start": "First value of the swept quantity; an offset from V_oc when "
                   "centre_on_voc is on.",
    "axis_stop": "Last value of the swept quantity; start = stop is one point "
                  "(repeats come from n_loops).",
    "axis_step": "Spacing of the swept values; the point count is rounded from the "
                  "span, never truncated.",
    "centre_on_voc": "With vpre as the scan parameter: read the scan range as offsets "
                     "from V_oc rather than as volts, so -0.01 .. +0.01 straddles V_oc at "
                     "whatever illumination is in force and 0 .. 0 is BACE at V_oc. The "
                     "V_oc comes from jv_bace at this led_v, measure_dc, or a typed voc. "
                     "Applies to a swept vpre only; with another scan parameter the flag "
                     "is ignored (the pinned vpre has vpre_on_voc instead).",
}

PINNED_DOCS: dict[str, str] = {
    "vpre": "Prebias at the device, sourced by the 81150A -- `core.pulses` divides it "
            "by the amplifier gain before it is written -- when it is not the swept "
            "axis; an offset from V_oc when vpre_on_voc is set.",
    "vcoll": "Collection bias at the device, sourced by the 81150A through the same "
             "gain division, when it is not the swept axis.",
    "delay_ns": "The wait from the LED falling edge to the collection pulse, as the "
                "axis programs it: the 81150A is given `delay_ns` as `:PULS:DEL1`. "
                "The field reaches the device `trigger_offset_s` later, and the "
                "integration window is measured from there, so it travels with "
                "this value.",
    "n_loops": "How many times the axis is swept; the statistics tighten with each.",
}

VPRE_ON_VOC_SPEC = ParamSpec(
    "vpre_on_voc", "bool", False, group="pinned",
    doc="Pin the prebias relative to V_oc: vpre becomes an offset from the V_oc in "
        "scope (TDCF at V_oc is vpre_on_voc with vpre = 0). Only when vpre is not the "
        "swept axis; a swept vpre follows V_oc through centre_on_voc.")
"""The pinned V_pre offset is relative to V_oc when delay or V_coll is scanned.

`Catalogue._build_bace` adds the offset to the V_oc source selected for
`centre_on_voc`, under the same LED-level coupling check. The engine receives
an absolute V_pre (`docs/service-contract.md`, section 5)."""


def _regroup(specs: list[ParamSpec], groups: Mapping[str, str], cls: type) -> list[ParamSpec]:
    out = []
    for s in specs:
        if s.name not in groups:
            raise ModuleError(
                f"catalogue: {cls.__name__}.{s.name} has no group; add it to the "
                "group table so it does not land on the card unlabelled")
        out.append(replace(s, group=groups[s.name]))
    return out


LABELS: dict[str, str] = {
    # J-V sweep and source meter
    "start_v": "Sweep start", "stop_v": "Sweep stop", "step_v": "Sweep step",
    "settle_s": "Dwell per point", "both_directions": "Both directions",
    "pixel_area_cm2": "Pixel area",
    "smu_current_compliance_a": "Current compliance",
    "smu_voltage_compliance_v": "Voltage compliance",
    "smu_settle_jsc_ms": "J_sc settle", "smu_settle_voc_ms": "V_oc settle",
    "smu_settle_jsat_ms": "J_sat settle", "smu_nplc": "NPLC",
    "smu_averaging": "Readings averaged", "smu_terminals": "Terminals",
    "smu_four_wire": "Four-wire sensing",
    # illumination
    "led_v": "LED level", "led_start_v": "LED start", "led_stop_v": "LED stop",
    "led_step_v": "LED step", "led_low_v": "LED low level", "led_settle_s": "LED settle",
    "led_settle_max_s": "LED settle limit", "led_settle_tolerance": "LED settle tolerance",
    "dark": "Dark curve", "led_mode": "LED mode", "shutter": "Shutter",
    # bace axis and pinned values
    "axis_name": "Scan parameter", "axis_start": "Scan start", "axis_stop": "Scan stop",
    "axis_step": "Scan step", "centre_on_voc": "Centre on V_oc", "vpre": "V_pre",
    "vcoll": "V_coll", "delay_ns": "Delay", "vpre_on_voc": "V_pre from V_oc",
    "n_loops": "Loops", "voc": "V_oc", "measure_dc": "DC metrics",
    "v_sat": "J_sat bias",
    # bace acquisition
    "n_averages": "Hardware averages", "timebase_ns_per_div": "Timebase",
    "record_length": "Record length", "t0_int_s": "Integration start",
    "t_int_width_s": "Integration width", "output_polarity": "Output polarity",
    "pulse_width_ns": "Pulse width", "pulse_frequency_hz": "Pulse frequency",
    "duty_percent": "Duty cycle", "offset_correct": "Baseline correction",
    "offset_region": "Baseline region", "alternate_order": "Alternate order",
    "invert_polarity": "Invert levels", "inverted_output": "Inverted output",
    "dark_settle_s": "Dark settle", "dark_reference": "Dark reference",
    "light_shutter": "Light-trace shutter", "shutter_settle_s": "Shutter settle",
    "read_intensity": "Power per shot", "acquisition_timeout_s": "Acquisition timeout",
    "external_trigger": "External trigger", "trigger_slope_positive": "Rising-edge trigger",
    "trigger_sweep": "Trigger sweep", "calibrate_trigger": "Calibrate trigger",
    "store_shots": "Store every shot",
    # power, temperature, wait, note
    "wavelength_nm": "Wavelength", "samples": "Samples", "setpoint_k": "Setpoint",
    "tolerance_k": "Tolerance", "hold_s": "Hold", "timeout_s": "Timeout",
    "seconds": "Duration", "text": "Text",
}
"""The console's label for each parameter name (`ParamSpec.label`)."""

MODULE_LABELS: dict[tuple[str, str], str] = {
    ("bace", "settle_s"): "Light settle",
    ("light", "settle_s"): "Settle after",
}
"""Labels for a name whose meaning differs by module; `LABELS` holds the rest."""


def _labelled(module: str, specs: Iterable[ParamSpec]) -> tuple[ParamSpec, ...]:
    out = []
    for s in specs:
        label = MODULE_LABELS.get((module, s.name)) or LABELS.get(s.name)
        if not label:
            raise ModuleError(
                f"catalogue: {module}.{s.name} has no label; add it to LABELS so the "
                "card does not show the bare code name")
        out.append(replace(s, label=label))
    return tuple(out)


def _with_docs(specs: list[ParamSpec], docs: Mapping[str, str]) -> list[ParamSpec]:
    return [replace(s, doc=docs.get(s.name, s.doc)) for s in specs]


def _smu_specs() -> list[ParamSpec]:
    names = [f.name for f in dataclasses.fields(SourceMeterConfig)]
    return specs_from_dataclass(
        SourceMeterConfig, group="sourcemeter",
        rename={n: "smu_" + n for n in names}, units=SMU_UNITS,
        choices={"terminals": ("FRON", "REAR")})


_JV_NOT_A_PARAM: tuple[str, ...] = ("dark", "led_levels_v", "led_settle_s",
                                    "light_control")
"""These `JVConfig` fields are excluded from the common J-V form.

`light_control` defines module identity: `jv` leaves illumination alone and
`jv_bace` manages it. Making it editable would let the two names describe
the same module. The other three fields belong only to `jv_bace`, which
supplies them through `_jv_led_specs`."""


def _jv_common_specs() -> list[ParamSpec]:
    sweep = specs_from_dataclass(
        JVConfig, group="axis",
        exclude=("settle_s", "both_directions", "pixel_area_cm2") + _JV_NOT_A_PARAM,
        units={"start_v": "V", "stop_v": "V", "step_v": "V"})
    acq = specs_from_dataclass(
        JVConfig, group="acquisition",
        exclude=("start_v", "stop_v", "step_v") + _JV_NOT_A_PARAM,
        units={"settle_s": "s", "pixel_area_cm2": "cm2"})
    return sweep, acq


def _jv_led_specs() -> list[ParamSpec]:
    jv_docs = field_docs(JVConfig)
    return [
        ParamSpec("led_v", "float", None, unit="V", group="led", nullable=True,
                  doc="One light curve at this 33220A drive level: inherited from an "
                      "illumination loop, or typed. Empty means sweep led_start_v to "
                      "led_stop_v."),
        ParamSpec("led_start_v", "float", 1.020, unit="V", group="led",
                  doc="First LED drive level, at the 33220A output."),
        ParamSpec("led_stop_v", "float", 1.020, unit="V", group="led",
                  doc="Last LED drive level, at the 33220A output; equal to "
                      "led_start_v for one curve."),
        ParamSpec("led_step_v", "float", 0.020, unit="V", group="led", minimum=0.0,
                  doc="Spacing of the 33220A drive levels; the count is rounded "
                      "from the span."),
        ParamSpec("led_low_v", "float", 0.4, unit="V", group="led",
                  doc="The 33220A pulse low level. Not used by this module -- "
                      "`jv_bace` drives the LED DC -- but carried so the rail can "
                      "show it."),
        ParamSpec("led_settle_s", "float", 2.0, unit="s", group="led", minimum=0.0,
                  doc=jv_docs.get("led_settle_s", "After changing the LED level.")),
        ParamSpec("dark", "bool", True, group="led",
                  doc=jv_docs.get("dark", "Include a dark scan with the LED output off.")),
    ]


def _bace_specs(rig_config: RigConfig) -> list[ParamSpec]:
    axis = _with_docs(specs_from_dataclass(
        Axis, group="axis",
        rename={"name": "axis_name", "start": "axis_start", "stop": "axis_stop",
                "step": "axis_step"},
        defaults={"name": "vpre", "start": 0.0, "stop": 0.0, "step": 0.0}), AXIS_DOCS)
    pinned = _with_docs(specs_from_dataclass(
        ScanSpec, group="pinned", exclude=("axis", "n_loops"),
        units={"vpre": "V", "vcoll": "V", "delay_ns": "ns"}), PINNED_DOCS)
    pinned = pinned + [VPRE_ON_VOC_SPEC]
    loops = _with_docs(specs_from_dataclass(
        ScanSpec, group="acquisition", exclude=("axis", "vpre", "vcoll", "delay_ns")),
        PINNED_DOCS)
    loops = [replace(s, minimum=1) for s in loops]
    illumination = [
        ParamSpec("led_v", "float", 1.0, unit="V", group="illumination",
                  doc="33220A pulse high level: the same number the V_oc was measured at."),
        ParamSpec("led_low_v", "float", 0.4, unit="V", group="illumination",
                  doc="The 33220A pulse low level, below the LED threshold so the "
                      "dark half-cycle is dark."),
        ParamSpec("voc", "float", None, unit="V", group="illumination", nullable=True,
                  doc="V_oc the axis is centred on. Derived from jv_bace or measure_dc; "
                      "typed by hand only as a last resort, and the validator says so."),
        ParamSpec("measure_dc", "bool", False, group="illumination",
                  doc="Measure V_oc / J_sc / J_sat on the Keithley under the LED first, "
                      "as the intensity series does."),
        ParamSpec("v_sat", "float", -1.0, unit="V", group="illumination",
                  doc="The bias the 2400 holds while it reads J_sat (measure_dc "
                      "only)."),
        ParamSpec("led_settle_s", "float", 2.0, unit="s", group="illumination",
                  minimum=0.0,
                  doc="The least time after the LED is set to pulse before anything is "
                      "measured; with a power meter the wait goes on past it until the "
                      "meter reads stable (measure_dc: also the DC settle before V_oc)."),
        ParamSpec("led_settle_max_s", "float", 60.0, unit="s", group="illumination",
                  minimum=0.0,
                  doc="The most time to wait for the power meter to read stable after "
                      "DC -> pulse; past it the scan goes on with a warning. A fixed "
                      "2 s wait is not enough on this bench."),
        ParamSpec("led_settle_tolerance", "float", 0.02, group="illumination",
                  minimum=0.0,
                  doc="How flat the 1918-C must read before the LED counts as "
                      "settled: the spread over a 10 s window, as a fraction of its "
                      "mean. Every one of its 20 polls must agree -- a shorter "
                      "window is blind to the slow thermal droop it exists to "
                      "catch."),
    ]
    run = _regroup(specs_from_dataclass(RunConfig, choices=RUN_CHOICES, units=RUN_UNITS),
                   RUN_GROUPS, RunConfig)
    store = [ParamSpec("store_shots", "bool", False, group="acquisition",
                       doc="Keep every single-shot photocurrent in the HDF5 (163 MB "
                           "for 51 points x 100 loops).")]
    return axis + pinned + loops + illumination + run + store + _smu_specs()


def _jv_specs() -> list[ParamSpec]:
    """The `jv` parameter list excludes illumination controls.

    The module sweeps under the illumination present, touches neither LED nor
    shutter, and records read-back. A preceding `light` node or a bench action
    controls illumination. Unlike `jv_bace`, this form has no `dark`, LED-level
    or `led_settle_s` parameters (`docs/service-contract.md`, section 5)."""
    sweep, acq = _jv_common_specs()
    return sweep + acq + _smu_specs()


def _light_specs() -> list[ParamSpec]:
    """The light node controls the shutter and LED independently.

    Every value is a request; the following read-back reports the result. `leave`
    on either half avoids touching it. The shutter can therefore move without
    cycling a generator at thermal steady state."""
    return [
        ParamSpec("shutter", "enum", "leave", group="illumination",
                  choices=("open", "shut", "leave"),
                  doc="Open, shut, or leave the shutter where it is. The shutter is "
                      "the light switch: it is what decides whether light reaches "
                      "the sample, whatever the generator is doing."),
        ParamSpec("led_mode", "enum", "leave", group="illumination",
                  choices=("dc", "pulse", "off", "leave"),
                  doc="`dc` for a steady level (a J-V under light, a V_oc), `pulse` "
                      "for the transient's square wave, `off` to disable the output, "
                      "`leave` to touch nothing. Prefer the shutter over `off`: a "
                      "generator that is cycled loses its thermal steady state and "
                      "the next module waits for it all over again."),
        ParamSpec("led_v", "float", 1.0, unit="V",
                  group="illumination",
                  doc="The 33220A drive level: its DC offset under `dc`, its pulse "
                      "high level under `pulse`. The same number the V_oc must be "
                      "measured at and `bace` must pulse at -- the coupling invariant "
                      "is one level, in one place."),
        ParamSpec("led_low_v", "float", 0.4, unit="V",
                  group="illumination",
                  doc="The 33220A pulse low level, below the LED threshold so the "
                      "dark half-cycle is dark. `pulse` only."),
        ParamSpec("pulse_frequency_hz", "float", 500.0, unit="Hz", group="timing",
                  minimum=0.0,
                  doc="The rate the 33220A chops the LED at, and the only "
                      "instrument this value reaches (`pulse` only -- the node "
                      "itself still moves the shutter). 500 Hz is the bench's "
                      "operating rate. It holds only until something else "
                      "sets it: a `bace` writes its own frequency to both "
                      "generators, so this does not constrain the scan after it."),
        ParamSpec("duty_percent", "float", 50.0, unit="%", group="timing",
                  minimum=0.0, maximum=100.0,
                  doc="The 33220A's duty cycle: the fraction of the period at the "
                      "waveform's high level, so at 50 % the device gets 1 ms of "
                      "light then 1 ms of dark at 500 Hz (`pulse` only). Away from "
                      "50 %, read it inverted -- this rig runs the generator at "
                      "`:OUTP:POL INV`, which flips which half is lit, so the lit "
                      "fraction is `100 - duty_percent` and raising this shortens "
                      "the illumination. Whether the lit half is long enough for "
                      "the device to reach its light V_oc is the operator's call: "
                      "nothing checks it."),
        ParamSpec("settle_s", "float", 0.0, unit="s", group="timing", minimum=0.0,
                  doc="Wait after the light is set, before the node finishes -- so "
                      "the step that follows starts under a settled lamp. 0 does not "
                      "wait; the power meter's own agreement check belongs to `bace`, "
                      "which is the module that needs it."),
    ]


def _jv_bace_specs() -> list[ParamSpec]:
    sweep, acq = _jv_common_specs()
    return sweep + _jv_led_specs() + acq + _smu_specs()


def _power_specs(rig_config: RigConfig) -> list[ParamSpec]:
    return [
        ParamSpec("wavelength_nm", "float", rig_config.power_meter_wavelength_nm,
                  unit="nm", group="acquisition", minimum=0.0,
                  doc="The wavelength the 1918-C is told to assume: its "
                      "responsivity is wavelength dependent, so this is written "
                      "before the meter is read."),
        ParamSpec("samples", "int", 1, group="acquisition", minimum=1,
                  doc="How many readings the 1918-C is asked for: 1 reads one "
                      "value, more asks the meter for a mean over that many, on its "
                      "own clock."),
    ]


def _temperature_specs() -> list[ParamSpec]:
    return [
        ParamSpec("setpoint_k", "float", 295.0, unit="K", group="timing",
                  doc="The temperature to reach: written to the 331 console when one is "
                      "wired (its 350 K ceiling applies), else set by the operator at "
                      "the pause."),
        ParamSpec("tolerance_k", "float", 0.2, unit="K", group="timing", minimum=0.0,
                  doc="How close the 331's reading must be to `setpoint_k` to "
                      "count as in band."),
        ParamSpec("hold_s", "float", 60.0, unit="s", group="timing", minimum=0.0,
                  doc="Dwell inside the band before measuring: from the reading entering "
                      "it when the 331 settles, after the resume when the operator does."),
        ParamSpec("timeout_s", "float", 1800.0, unit="s", group="timing", minimum=0.0,
                  doc="How long the 331 may take to reach the band before the run pauses "
                      "and asks the operator, on the poll clock; 0 pauses at the first "
                      "reading outside the band. Not used on the manual pause."),
    ]


def _wait_specs() -> list[ParamSpec]:
    return [ParamSpec("seconds", "float", 1.0, unit="s", group="timing", minimum=0.0,
                      doc="How long to sleep; a no-op under --fast.")]


def _note_specs() -> list[ParamSpec]:
    return [ParamSpec("text", "str", "", group="",
                      doc="Goes to the journal and the log; changes nothing on the bench.")]


# -- run.toml mapping ----------------------------------------------------------
def _run_fields() -> list[str]:
    return [f.name for f in dataclasses.fields(RunConfig)]


def _smu_fields() -> list[str]:
    return [f.name for f in dataclasses.fields(SourceMeterConfig)]


_SMU_MAP = ("run.toml [sourcemeter]",
            {f"sourcemeter.{f}": f"smu_{f}" for f in _smu_fields()})

_TOML: dict[str, list[tuple[str, dict[str, str]]]] = {
    "bace": [
        ("run.toml [axis]", {"axis.name": "axis_name", "axis.start": "axis_start",
                             "axis.stop": "axis_stop", "axis.step": "axis_step",
                             "axis.centre_on_voc": "centre_on_voc"}),
        ("run.toml [pinned]", {"pinned.vpre": "vpre", "pinned.vcoll": "vcoll",
                               "pinned.delay_ns": "delay_ns"}),
        ("run.toml [acquisition]", {**{f"acquisition.{f}": f for f in _run_fields()},
                                    "acquisition.n_loops": "n_loops",
                                    "acquisition.store_shots": "store_shots"}),
        ("run.toml [illumination]", {"illumination.level_v": "led_v",
                                     "illumination.low_level_v": "led_low_v",
                                     "illumination.v_sat": "v_sat"}),
        _SMU_MAP,
    ],
    "jv": [
        _SMU_MAP,
        ("run.toml [jv]", {f"jv.{k}": k for k in ("start_v", "stop_v", "step_v",
                                                  "settle_s", "both_directions",
                                                  "pixel_area_cm2")}),
    ],
    "light": [
        ("run.toml [illumination]", {"illumination.level_v": "led_v",
                                     "illumination.low_level_v": "led_low_v"}),
        ("run.toml [acquisition]", {"acquisition.pulse_frequency_hz": "pulse_frequency_hz",
                                    "acquisition.duty_percent": "duty_percent"}),
    ],
    "jv_bace": [
        _SMU_MAP,
        # Both ends of the `jv_bace` range use the recipe's LED level by default.
        # This supplies the bench's `bace` card with V_oc at the same illumination
        # without requiring duplicate level edits.
        ("run.toml [illumination]", {"illumination.level_v": "led_start_v",
                                     "illumination.low_level_v": "led_low_v"}),
        ("run.toml [illumination]", {"illumination.level_v": "led_stop_v"}),
        ("run.toml [jv]", {f"jv.{k}": k for k in ("start_v", "stop_v", "step_v",
                                                  "settle_s", "both_directions",
                                                  "pixel_area_cm2", "led_start_v",
                                                  "led_stop_v", "led_step_v",
                                                  "led_low_v", "led_settle_s", "dark")}),
    ],
}
"""The recipe mapping connects TOML keys to module parameters and layer detail.

`config.load_run` does not read the optional `[jv]` table. The catalogue
checks its keys so a typo cannot silently select a default J-V range."""

def recipe_keys(name: str) -> dict[str, list[str]]:
    """The recipe-key lookup returns `run.toml` keys by parameter for one module."""
    out: dict[str, list[str]] = {}
    for _detail, mapping in _TOML.get(name, []):
        for key, param in mapping.items():
            if key not in out.setdefault(param, []):
                out[param].append(key)
    return out


_JV_TABLE_KEYS: frozenset[str] = frozenset(
    k.split(".", 1)[1] for k in _TOML["jv_bace"][-1][1])
"""The allowed `[jv]` keys cover the `jv_bace` mapping and its `jv` subset."""


# -- the catalogue ---------------------------------------------------------------
class Catalogue:
    """The catalogue resolves module parameters and builds event generators.

    `run_toml` contains raw recipe tables from `params.run_toml_layer`, retaining
    table names for layer detail. `history` supplies `last_used_params`,
    `settle_history` and `shot_time_s`, through the journal or a test substitute.
    `sample` supplies `[sample]` for session `RunMetadata`, outside module params."""

    def __init__(self, *, rig_config: RigConfig, run_toml: dict | None = None,
                 history: Any = None, sample: dict | None = None):
        self.rig_config = rig_config
        self.run_toml: dict = dict(run_toml or {})
        self.history = history
        try:
            self.sample: dict = coerce_sample(sample or {})
        except ValueError as exc:
            raise ModuleError(str(exc)) from None
        jv_table = self.run_toml.get("jv", {})
        unknown = sorted(set(jv_table) - _JV_TABLE_KEYS) if isinstance(jv_table, Mapping) else []
        if unknown:
            raise ConfigError(
                f"unknown key(s) in [jv]: {', '.join(unknown)}. Refusing to fall back "
                "to defaults -- a typo here is a J-V range nobody chose.")

        self._params: dict[str, tuple[ParamSpec, ...]] = {
            "jv": _labelled("jv", _jv_specs()),
            "light": _labelled("light", _light_specs()),
            "jv_bace": _labelled("jv_bace", _jv_bace_specs()),
            "bace": _labelled("bace", _bace_specs(rig_config)),
            "power": _labelled("power", _power_specs(rig_config)),
            "temperature": _labelled("temperature", _temperature_specs()),
            "park": (),
            "wait": _labelled("wait", _wait_specs()),
            "note": _labelled("note", _note_specs()),
        }
        self._specs: dict[str, ModuleSpec] = {
            # No `led_params`: this module is the one that does not touch the
            # light, so there is nothing for the bench's LED actions to take
            # its levels from.
            "jv": self._spec("jv", "J-V", "measurement", "built", "dc",
                             provides_voc=False, needs_voc_param=None, led_params=()),
            "jv_bace": self._spec("jv_bace", "J-V light", "measurement", "built", "dc",
                                  provides_voc=True, needs_voc_param=None,
                                  led_params=("led_v", "led_start_v", "led_stop_v",
                                              "led_step_v", "led_low_v")),
            "bace": self._spec("bace", "bace", "measurement", "built", "transient",
                               provides_voc=False, needs_voc_param="centre_on_voc",
                               led_params=("led_v", "led_low_v")),
            "light": self._spec("light", "light", "utility", "built", None,
                                provides_voc=False, needs_voc_param=None,
                                led_params=("led_v", "led_low_v")),
            "power": self._spec("power", "power", "observer", "built", None,
                                provides_voc=False, needs_voc_param=None, led_params=()),
            # `partial`: works both ways. Settles through the 331 console
            # when `rig.temperature` holds one (setpoint written, band
            # waited for, the console's ceiling and heater range left to
            # it), pauses for a manual set when it does not -- and `needs`
            # says which on the card.
            "temperature": self._spec("temperature", "temperature", "utility",
                                      "partial", None, provides_voc=False,
                                      needs_voc_param=None, led_params=()),
            "park": self._spec("park", "park", "utility", "built", None,
                               provides_voc=False, needs_voc_param=None, led_params=()),
            "wait": self._spec("wait", "wait", "utility", "built", None,
                               provides_voc=False, needs_voc_param=None, led_params=()),
            "note": self._spec("note", "note", "utility", "built", None,
                               provides_voc=False, needs_voc_param=None, led_params=()),
        }
        self._edited: dict[str, dict[str, Any]] = {n: {} for n in MODULE_NAMES}
        # Invalid recipe values must fail at startup rather than on the
        # first `GET /modules`.
        for name in MODULE_NAMES:
            self._layered(name, with_history=False)

    def _spec(self, name: str, title: str, kind: str, status: str, relay: str | None, *,
              provides_voc: bool, needs_voc_param: str | None,
              led_params: tuple[str, ...]) -> ModuleSpec:
        groups: list[str] = []
        for s in self._params[name]:
            if s.group and s.group not in groups:
                groups.append(s.group)
        return ModuleSpec(name=name, title=title, kind=kind, status=status, relay=relay,
                          provides_voc=provides_voc, needs_voc_param=needs_voc_param,
                          led_params=led_params, groups=tuple(groups))

    # -- listing ----------------------------------------------------------
    def names(self) -> list[str]:
        return list(MODULE_NAMES)

    def spec(self, name: str) -> ModuleSpec:
        try:
            return self._specs[name]
        except KeyError:
            # Unsupported names need replacement instructions. Saved recipes
            # persist independently of the catalogue and require explicit edits.
            if name in RETIRED:
                raise KeyError(f"{name} is no longer a module. It was "
                               f"{RETIRED[name]}") from None
            raise KeyError(name) from None

    def base_metadata(self, sample: Mapping[str, Any] | None = None) -> RunMetadata:
        """The session metadata supplies the device identity from `[sample]`.

        A module adds its LED level, V_oc and temperature. `sample` overrides the
        session block with the copy captured at queue time (`RunRecord.sample`).
        Changing the session identity during a run must not relabel its remaining
        nodes or contradict `RunQueued.sample` (`docs/service-contract.md`, section
        3a)."""
        s = self.sample if sample is None else sample
        # The session's own number is typed -- in `[sample]` or in the console's
        # metadata field, which is the same slot. A temperature node replaces it
        # for its subtree, and `how`/`source` with it.
        typed_k = _float_or_none(s.get("temperature_k"))
        return RunMetadata(sample=str(s.get("sample", "")), material=str(s.get("material", "")),
                           pixel=str(s.get("pixel", "")),
                           temperature_k=typed_k,
                           temperature_how="typed" if typed_k is not None else "",
                           operator=str(s.get("operator", "")),
                           comment=str(s.get("comment", "")))

    # -- parameters -------------------------------------------------------
    def param_specs(self, name: str) -> tuple[ParamSpec, ...]:
        """The parameter specs follow the order displayed on the module card."""
        self.spec(name)
        return self._params[name]

    def param_set(self, name: str) -> ParamSet:
        """The parameter set resolves default < `run.toml` < last used < edited.

        Each call returns a separate copy, so inherited or derived bindings cannot
        change the bench card (`docs/service-contract.md`, section 5)."""
        return self._layered(name, with_history=True)

    def _layered(self, name: str, *, with_history: bool) -> ParamSet:
        self.spec(name)
        ps = ParamSet(self._params[name])
        try:
            for detail, mapping in _TOML.get(name, []):
                ps.update_layer(Source.RUN_TOML, toml_layer(self.run_toml, mapping),
                                detail=detail)
        except ParamError as exc:
            raise ModuleError(f"{name}: {exc}") from None
        if with_history and self.history is not None:
            last = self.history.last_used_params(name)
            for key, value in (last or {}).items():
                # Skip unknown journal parameters and values the spec refuses, one
                # at a time. A record of a run must not make the module unlistable
                # when its parameter names or accepted values differ.
                if key not in ps:
                    continue
                try:
                    ps.update_layer(Source.LAST_USED, {key: value}, detail="previous run")
                except ParamError:
                    continue
        if self._edited[name]:
            ps.update_layer(Source.EDITED, self._edited[name])
        return ps

    def edit(self, name: str, values: Mapping[str, Any]) -> ParamSet:
        """The catalogue edit coerces and applies the edited layer atomically.

        `None` removes that parameter's edit and exposes the layer below
        (`docs/service-contract.md`, section 5)."""
        if not isinstance(values, Mapping):
            raise ModuleError(f"{name}: expected a mapping of parameter -> value, "
                              f"got {type(values).__name__}")
        ps = self.param_set(name)
        sets = {k: v for k, v in values.items() if v is not None}
        resets = [k for k, v in values.items() if v is None]
        try:
            for k in resets:
                ps.spec(k)
            ps.update_layer(Source.EDITED, sets)
            for k in resets:
                ps.reset(k)
        except ParamError as exc:
            raise ModuleError(f"{name}: {exc}") from None
        self._edited[name] = {k: v for k, (v, _detail) in ps.layer(Source.EDITED).items()}
        return ps

    def reset(self, name: str, param: str | None = None) -> None:
        """The reset removes one parameter edit or the whole edited layer."""
        self.spec(name)
        if param is None:
            self._edited[name] = {}
            return
        if param not in self._params_by_name(name):
            raise ModuleError(f"{name}: {param}: no such parameter")
        self._edited[name].pop(param, None)

    def _params_by_name(self, name: str) -> dict[str, ParamSpec]:
        return {s.name: s for s in self._params[name]}

    def _complete(self, name: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Each parameter uses a coerced override or the catalogue's resolved value.

        Unknown names are refused. Silently dropping a misspelt override would run
        with a value the caller does not see."""
        specs = self._params_by_name(name)
        unknown = sorted(set(params) - set(specs))
        if unknown:
            raise ModuleError(f"{name}: no such parameter(s): {', '.join(unknown)}")
        if set(params) == set(specs):
            base: dict[str, Any] = {}
        else:
            base = self.param_set(name).values()
        out = dict(base)
        for k, v in params.items():
            try:
                out[k] = specs[k].coerce(v)
            except ParamError as exc:
                raise ModuleError(f"{name}: {exc}") from None
        return out

    # -- the wire ---------------------------------------------------------
    def as_wire(self, name: str, *, bench: dict | None = None,
                session_voc: VocSource | None = None) -> dict:
        """The module response combines parameters, requirements and cached state.

        The session fills `last` from its run registry; it is `None` here. `bench`
        provides unavailable roles, and `session_voc` provides the session V_oc
        source."""
        spec = self.spec(name)
        ps = self.param_set(name)
        p = ps.values()
        if name == "bace" and session_voc is not None and p.get("voc") is None \
                and not p.get("measure_dc") and p.get("led_v") is not None \
                and abs(float(session_voc.led_v) - float(p["led_v"])) <= VOC_LEVEL_TOLERANCE_V:
            # Apply `pipeline._Resolver._voc` to the card's manual-run values.
            # The session V_oc at this level is the run's source, so the card shows
            # its value and derived layer as noneditable instead of a null default.
            # Only this separate copy receives the binding.
            ps.set_layer(Source.DERIVED, {"voc": session_voc.value},
                         f"{session_voc.how} {session_voc.node_path} (this session)")
        keys = recipe_keys(name)
        params = [{**row, "recipe": keys.get(row["name"], [])} for row in ps.as_wire()]
        seconds, text, parts = self._estimate(name, p)
        return {"name": name, "title": spec.title, "status": spec.status,
                "kind": spec.kind, "relay": spec.relay,
                "estimate_s": seconds,
                "estimate_text": text,
                "estimate": {"seconds": seconds, **parts},
                "needs": self.needs(name, p, bench=bench, session_voc=session_voc),
                "last": None, "params": params}

    def needs(self, name: str, params: Mapping[str, Any], *, bench: dict | None = None,
              session_voc: VocSource | None = None) -> list[dict]:
        """The needs list reports required resources as `{code, text}` entries.

        It names V_oc sources, consoles and instruments for the card's needs line.
        Geometry and level checks belong to validation."""
        p = self._complete(name, params)
        unavailable: dict = dict((bench or {}).get("unavailable") or {})
        power = ((bench or {}).get("instruments") or {}).get("power") or {}
        power_silent = bool(bench) and not power.get("available", True)
        out: list[dict] = []

        def missing(role: str, why: str) -> None:
            if role in unavailable:
                out.append({"code": role, "text": f"{why}: {unavailable[role]}"})

        if name == "bace":
            needs_voc = any(voc_flags(p))
            if needs_voc and p["voc"] is None and not p["measure_dc"]:
                led_v = p["led_v"]
                if session_voc is None:
                    out.append({"code": "voc", "text": "none · run jv_bace first"})
                elif abs(session_voc.led_v - led_v) > VOC_LEVEL_TOLERANCE_V:
                    out.append({"code": "voc",
                                "text": f"measured at {session_voc.led_v:.3f} V, not "
                                        f"{led_v:.3f} V · run jv_bace at this level"})
            for role in ("scope", "bias", "shutter", "led"):
                missing(role, "not on this bench")
            if p["measure_dc"]:
                missing("smu", "measure_dc needs the Keithley")
                missing("relay", "measure_dc needs the relay")
            if p["read_intensity"] and power_silent:
                out.append({"code": "power", "text": "console silent · intensity will be NaN"})
        elif name in ("jv", "jv_bace"):
            missing("smu", "a J-V scan needs the Keithley")
            if name == "jv_bace":
                missing("led", "a light curve needs the LED")
                missing("shutter", "a light curve needs the shutter")
            else:
                # Without shutter or LED read-back, `jv` can run but cannot label
                # the illumination. Warn about the unknown label without blocking it.
                blind = [r for r in ("shutter", "led") if r in unavailable]
                if blind:
                    out.append({"code": "illumination",
                                "text": f"no {' or '.join(blind)} to read · the curve "
                                        "will be recorded as unknown, not as dark"})
        elif name == "light":
            # Only what this node actually sets, the same test `_build_light`
            # makes: the point of the module is that either half can be left
            # alone, so a shutter-only node on a bench with no LED is
            # perfectly runnable and must not be blocked by one.
            if p["led_mode"] != "leave":
                missing("led", f"led_mode {p['led_mode']}, and the LED is not on this bench")
            if p["shutter"] != "leave":
                missing("shutter", f"shutter {p['shutter']}, and the shutter is not "
                                   "on this bench")
        elif name == "power":
            if "power" in unavailable:
                out.append({"code": "power", "text": unavailable["power"]})
            elif power_silent:
                out.append({"code": "power",
                            "text": "console silent · " + str(power.get("reason") or
                                                             "no reading")})
        elif name == "temperature":
            # Advisory, not a missing instrument: the module runs either
            # way (`pipeline.ADVISORY_NEEDS`), the card just says which way.
            temperature = ((bench or {}).get("instruments") or {}).get("temperature") or {}
            if bench and not temperature.get("wired", False):
                text = "331 not wired · pauses for a manual set"
                if "temperature" in unavailable:
                    text += ": " + str(unavailable["temperature"])
                out.append({"code": "temperature", "text": text})
            elif bench and temperature.get("error"):
                # A failed read means the console link is unavailable, even if it
                # answers at Start. It does not establish an instrument fault.
                out.append({"code": "temperature",
                            "text": "331 attached at Start, not answering now · "
                                    "pauses for a manual set"})
            elif bench and temperature.get("connected") is False:
                out.append({"code": "temperature",
                            "text": "331 reachable, instrument silent · pauses for a "
                                    "manual set"})
        return out

    # -- the cost model ------------------------------------------------------
    def shot_time(self) -> tuple[float, str]:
        """The shot-time estimate returns the journal median or default with its
        source."""
        t = None
        if self.history is not None:
            try:
                t = self.history.shot_time_s("bace")
            except Exception:                               # noqa: BLE001
                t = None
        if t is None or not math.isfinite(float(t)) or float(t) <= 0:
            return DEFAULT_SHOT_S, "default"
        return float(t), "journal"

    def estimate_s(self, name: str, params: Mapping[str, Any]) -> float:
        """The module cost estimates seconds under the service cost model.

        Unrunnable parameters give 0.0; validation explains the refusal
        (`docs/service-contract.md`, section 7)."""
        return self._estimate(name, params)[0]

    def estimate_text(self, name: str, params: Mapping[str, Any]) -> str:
        return self._estimate(name, params)[1]

    def _estimate(self, name: str, params: Mapping[str, Any]) -> tuple[float, str, dict]:
        """Seconds, the card's sentence and its parts: `shot_s`, `shot_source`,
        `loops`, `points` and `curves` where the module has them."""
        try:
            p = self._complete(name, params)
        except ModuleError as exc:
            return 0.0, f"cannot estimate: {exc}", {}
        if name == "bace":
            t_shot, source = self.shot_time()
            try:
                n_steps = _axis_of(p).n_points
            except (AxisError, ModuleError) as exc:
                return 0.0, f"cannot estimate: {exc}", {}
            n_loops = int(p["n_loops"])
            total = n_loops * n_steps * t_shot
            tag = "" if source == "journal" else " (default)"
            return total, (f"one shot ≈ {t_shot:.1f} s{tag} · {_count(n_loops, 'loop')} × "
                           f"{_count(n_steps, 'pt')} ≈ {_duration(total)}"), {
                "shot_s": t_shot, "shot_source": source, "loops": n_loops, "points": n_steps}
        if name in ("jv", "jv_bace"):
            try:
                n_points = int(_jv_points(p).size)
                levels = self._jv_levels(p, None) if name == "jv_bace" else ()
            except (ModuleError, ValueError) as exc:
                return 0.0, f"cannot estimate: {exc}", {}
            # `jv` takes one curve per direction without changing illumination.
            # There is no dark-plus-levels plan or LED settle to count.
            dark = bool(p["dark"]) if name == "jv_bace" else True
            n_curves = (int(dark) + len(levels)) * (2 if p["both_directions"] else 1)
            led_settle = float(p.get("led_settle_s", JVConfig().led_settle_s))
            if name == "jv":
                n_curves, led_settle = (2 if p["both_directions"] else 1), 0.0
            total = n_curves * (n_points * float(p["settle_s"])
                                + n_points * JV_POINT_OVERHEAD_S + led_settle)
            return total, (f"{_count(n_curves, 'curve')} × {_count(n_points, 'pt')} ≈ "
                           f"{_duration(total)}"), {"curves": n_curves, "points": n_points}
        if name == "power":
            total = 0.1 * max(1, int(p["samples"]))
            return total, "one reading" if p["samples"] <= 1 else f"{p['samples']} samples", {}
        if name == "temperature":
            hold = float(p["hold_s"])
            return hold, f"settle — (331 or operator) + hold {_duration(hold)}", {}
        if name == "wait":
            return float(p["seconds"]), _duration(float(p["seconds"])), {}
        if name == "light":
            asks = []
            if p["shutter"] != "leave":
                asks.append(f"shutter {p['shutter']}")
            if p["led_mode"] == "off":
                asks.append("LED off")
            elif p["led_mode"] != "leave":
                asks.append(f"LED {p['led_mode'].upper()} {float(p['led_v']):g} V")
            settle = float(p["settle_s"])
            return settle + 0.2, (" · ".join(asks) or "nothing to set") + (
                f" + {_duration(settle)}" if settle > 0 else ""), {}
        if name == "park":
            return 1.0, "outputs off, shutter shut", {}
        return 0.0, "journal entry", {}

    def _jv_levels(self, p: Mapping[str, Any], ctx: RunContext | None) -> tuple[float, ...]:
        """The `jv_bace` light levels use one `led_v` or the configured range.

        An inherited or typed `led_v` selects one level. Otherwise the point count
        is rounded as in `SeriesConfig.levels`; truncation can omit the last level."""
        led_v = ctx.led_v if (ctx is not None and ctx.led_v is not None) else p["led_v"]
        if led_v is not None:
            return (float(led_v),)
        start, stop, step = float(p["led_start_v"]), float(p["led_stop_v"]), float(p["led_step_v"])
        if start == stop:
            return (start,)
        if step <= 0:
            raise ModuleError("jv_bace: led_step_v: must be positive for a range of levels")
        n = int(round(abs(stop - start) / step)) + 1
        return tuple(float(x) for x in np.linspace(start, stop, n))

    # -- build ----------------------------------------------------------------
    def build(self, name: str, params: Mapping[str, Any], ctx: RunContext,
              rig: Rig) -> Iterator[E.Event]:
        """The builder returns a module generator wrapped in its recorder and router.

        Refusals precede generator creation, so a `ModuleError` reaches the caller
        before any instrument access (`docs/service-contract.md`, section 5)."""
        self.spec(name)
        p = self._complete(name, params)
        builder = getattr(self, "_build_" + name)
        return builder(p, ctx, rig)

    # jv ---------------------------------------------------------------------
    def _build_jv(self, p, ctx, rig):
        return self._build_jv_run("jv", p, ctx, rig, light=False)

    def _build_jv_bace(self, p, ctx, rig):
        return self._build_jv_run("jv_bace", p, ctx, rig, light=True)

    def _build_jv_run(self, name: str, p: dict, ctx: RunContext, rig: Rig, *,
                      light: bool) -> Iterator[E.Event]:
        if rig.smu is None:
            raise ModuleError(f"{name}: a J-V scan needs a SourceMeter, and this bench "
                              "has none")
        levels = self._jv_levels(p, ctx) if light else ()
        if light and rig.led is None:
            raise ModuleError(f"{name}: a light curve needs the LED source, and this "
                              "bench has none")
        smu_cfg = self._smu_config(name, p)
        # `jv` leaves illumination alone, so `dark` and `led_settle_s` are
        # not read. `jv_bace` has at least one level, even for a collapsed
        # range; `run_jv` handles the empty-measurement refusal itself.
        dark = bool(p["dark"]) if light else True
        try:
            cfg = JVConfig(start_v=p["start_v"], stop_v=p["stop_v"], step_v=p["step_v"],
                           settle_s=p["settle_s"], both_directions=p["both_directions"],
                           light_control="manage" if light else "leave",
                           dark=dark, led_levels_v=tuple(levels),
                           pixel_area_cm2=p["pixel_area_cm2"],
                           led_settle_s=float(p.get("led_settle_s", JVConfig().led_settle_s)))
            cfg.points()
        except ValueError as exc:
            raise ModuleError(f"{name}: step_v: {exc}") from None
        temperature, temp_how, temp_source = _bind_temperature(ctx, rig)
        meta = replace(ctx.metadata, temperature_k=temperature,
                       temperature_how=temp_how, temperature_source=temp_source,
                       led_drive_v=levels[0] if len(levels) == 1 else None,
                       voc_v=None, offset_corrected=False, started=datetime.now())
        folder = os.path.join(ctx.out_folder, meta.folder_name())
        resolved = dict(ctx.resolved)
        if light:
            resolved.setdefault("led_levels_v", "/".join(f"{lv:g}" for lv in levels)
                                + f"/{p['led_low_v']:g}")
        rig_cfg = self.rig_config.as_dict()

        def run() -> Iterator[E.Event]:
            _apply_smu_config(rig, smu_cfg)
            rec = JVRecorder(folder, meta.stamp, meta.as_dict(), rig_cfg, resolved=resolved)
            ctx.folders.append(folder)
            acc = _JVData()
            try:
                for ev in jv_storage.record(run_jv(rig, cfg, abort=ctx.abort,
                                                   sleep=ctx.sleep), rec):
                    acc.handle(ev)
                    yield ev
            finally:
                if ctx.on_data is not None:
                    ctx.on_data(ctx.node_path, acc.data())

        return run()

    # bace -------------------------------------------------------------------
    def _build_bace(self, p: dict, ctx: RunContext, rig: Rig) -> Iterator[E.Event]:
        name = "bace"
        led_v = float(ctx.led_v if ctx.led_v is not None else p["led_v"])
        led_low_v = float(ctx.led_low_v if ctx.led_low_v is not None else p["led_low_v"])
        if rig.led is None:
            raise ModuleError(f"{name}: led_v: this bench has no LED source, and bace "
                              "pulses the LED itself")
        try:
            run_cfg = RunConfig(**{f: p[f] for f in _run_fields()})
        except ValueError as exc:
            raise ModuleError(f"{name}: {exc}") from None
        axis = _axis_of(p)
        # `voc_flags`: each flag applies to one side of `axis_name` only, and
        # the other is inert rather than refused -- the console hides it.
        _, vpre_on_voc = voc_flags(p)
        needs_voc = axis.centre_on_voc or vpre_on_voc
        try:
            spec = ScanSpec(axis=axis, vpre=p["vpre"], vcoll=p["vcoll"],
                            delay_ns=p["delay_ns"], n_loops=int(p["n_loops"]))
            pulse_levels(spec.vpre, spec.vcoll, self.rig_config.pulse_amp, spec.delay_ns,
                         run_cfg.pulse_width_ns, invert=run_cfg.invert_polarity)
        except (AxisError, ValueError) as exc:
            raise ModuleError(f"{name}: {exc}") from None
        try:
            drive = LedDrive(level=led_v, low_level=led_low_v,
                             frequency_hz=run_cfg.pulse_frequency_hz,
                             duty_percent=run_cfg.duty_percent,
                             threshold_v=self.rig_config.led_threshold_v)
        except IlluminationError as exc:
            raise ModuleError(f"{name}: led_v: {exc}") from None
        measure_dc = bool(p["measure_dc"])
        # A `bace` run uses the source-meter only when `measure_dc` is set.
        # Its configuration and compliance ceiling apply only then. Checking
        # unused compliance would block a run from a field marked not read.
        smu_cfg = self._smu_config(name, p) if measure_dc else None
        if measure_dc and rig.smu is None:
            raise ModuleError(f"{name}: measure_dc: this bench has no SourceMeter")
        if measure_dc and rig.router is None:
            raise ModuleError(f"{name}: measure_dc: no Router -- the SourceMeter and "
                              "the amplifier share the device node, and without the "
                              "relay both would be connected at once")

        # Manual V_oc priority is typed `voc`, then `ctx.voc`, then
        # `measure_dc` (`docs/service-contract.md`, section 5). The pipeline
        # resolver selects first: measurements clear `voc`, and typed sources
        # leave `ctx.voc` unset. Only a direct `Catalogue.build` call can
        # supply both; the typed value wins there.
        source: VocSource | None = None
        if p["voc"] is not None:
            source = VocSource(value=float(p["voc"]), led_v=led_v, run_id=ctx.run_id,
                               node_path=ctx.node_path, how="typed")
        elif ctx.voc is not None:
            source = ctx.voc
        if needs_voc and source is None and not measure_dc:
            flag = "centre_on_voc" if axis.centre_on_voc else "vpre_on_voc"
            raise ModuleError(
                f"{name}: {flag}: no V_oc source in scope at {led_v:.3f} V -- "
                "run jv_bace at this level first, set measure_dc, or type voc")
        if needs_voc and source is not None:
            # The coupling invariant, checked with nothing touched: a V_oc
            # from another drive level would centre the axis on the wrong
            # number and bias every charge with no symptom in the data.
            try:
                assert_axis_centre(drive, voc_measured_at=source.led_v)
            except IlluminationError as exc:
                raise ModuleError(f"{name}: voc: {exc}") from None

        temperature, temp_how, temp_source = _bind_temperature(ctx, rig)
        store_shots = bool(p["store_shots"])
        v_sat = float(p["v_sat"])
        led_settle_s = float(p["led_settle_s"])
        led_settle_max_s = float(p["led_settle_max_s"])
        led_settle_tolerance = float(p["led_settle_tolerance"])
        rig_cfg = self.rig_config

        def run() -> Iterator[E.Event]:
            voc_src = source
            acc = _BaceData(run_cfg, rig_cfg)
            try:
                if measure_dc:
                    # V_oc requires steady illumination: the 33220A at DC at the pulse's
                    # high level, the shutter open, and `led_settle_s` for the device.
                    # `Rig.park()` shuts the shutter before the job. Under a 500 Hz
                    # pulse, the Keithley averages light and dark phases; behind a shut
                    # shutter, it reads dark V_oc. Either centres the axis incorrectly,
                    # and the coupling check compares levels only, so it cannot detect
                    # this.
                    _apply_smu_config(rig, smu_cfg)
                    dc_settings = drive.dc_settings()
                    rig.led.set_dc(dc_settings["offset"])
                    rig.led.enable_output(True)
                    rig.shutter.unblock()
                    ctx.sleep(led_settle_s)
                    try:
                        with rig.router.dc():
                            dc = rig.smu.measure_dc(v_sat=v_sat)
                    finally:
                        rig.shutter.shut()
                    yield E.DCMeasured(led_drive_v=led_v, dc=dc)
                    if voc_src is None:
                        voc_src = VocSource(value=float(dc.voc), led_v=led_v,
                                            run_id=ctx.run_id, node_path=ctx.node_path,
                                            how="measure_dc")

                s = drive.pulse_settings()
                # The 81150A must arm on light off: the rising Sync edge under INV.
                # Enforce this before extraction. NORM extracts during illumination
                # and can still yield plausible Q. Inverting the waveform does not
                # invert Sync or affect the preceding DC V_oc measurement.
                if run_cfg.external_trigger:
                    rig.led.set_polarity(run_cfg.trigger_slope_positive)
                rig.led.set_pulse(s["high"], s["low"], frequency_hz=s["frequency"],
                                  duty_percent=s["duty"])
                rig.led.enable_output(True)
                # The 33220A Sync arms the 81150A, whose Sync triggers the scope.
                # Read back `:OUTP?`, `FUNC:SHAP?`, `:FREQ?` and `OUTP:SYNC?` before
                # calibration. Refuse output off, DC, a different frequency or Sync
                # off rather than calibrate against silent CHAN3. Record the reading
                # whether accepted or refused. Check before settling to avoid waiting
                # for stable darkness from a generator that rejects its settings.
                led_state = _read_led_state(rig.led)
                yield E.InstrumentState({f"led_{k}": v for k, v in led_state.items()})
                problems = _led_problems(led_state, s)
                if problems:
                    raise ModuleError(f"{name}: the 33220A is not driving the LED as "
                                      f"the recipe asks: {'; '.join(problems)}")

                # Open the shutter so the meter and device see the LED, matching
                # the LabVIEW illumination sequence (`docs/service-contract.md`,
                # section 5). Wait for stable power rather than `led_settle_s` alone.
                # The shutter remains open for the scan; its cleanup shuts it.
                rig.shutter.unblock()
                yield from _settle_led(rig, ctx, settle_s=led_settle_s,
                                       max_s=led_settle_max_s,
                                       tolerance=led_settle_tolerance)

                if voc_src is not None:
                    ctx.voc = voc_src
                voc = voc_src.value if voc_src is not None else None
                if needs_voc:
                    assert_axis_centre(drive, voc_measured_at=voc_src.led_v)
                scan = spec
                if vpre_on_voc:
                    # The engine requires absolute V_pre. Add the card's offset to
                    # V_oc from `jv_bace`, `measure_dc` or a typed source.
                    scan = replace(spec, vpre=float(voc_src.value) + float(p["vpre"]))

                meta = replace(ctx.metadata, led_drive_v=led_v, voc_v=voc,
                               temperature_k=temperature,
                               temperature_how=temp_how, temperature_source=temp_source,
                               offset_corrected=run_cfg.offset_correct,
                               started=datetime.now())
                rec = RunRecorder(ctx.out_folder, meta, store_shots=store_shots,
                                  resolved=dict(ctx.resolved))
                router_ctx = rig.router.transient() if rig.router is not None else _null()
                with router_ctx:
                    for ev in record(run_transient_scan(rig, scan, run_cfg, voc=voc,
                                                        abort=ctx.abort, sleep=ctx.sleep),
                                     rec):
                        if isinstance(ev, E.RunStarted) and rec.folder:
                            ctx.folders.append(rec.folder)
                        acc.handle(ev)
                        yield ev
            finally:
                # Keep the LED pulsing to preserve thermal steady state; the shutter
                # controls exposure. `run_transient_scan` shuts it on unwind after
                # scan start. This cleanup also covers exit during the preceding
                # settle, when the shutter is open but the scan has not started.
                try:
                    rig.shutter.shut()
                except Exception:
                    pass
                if ctx.on_data is not None and acc.started:
                    ctx.on_data(ctx.node_path, acc.data())

        return run()

    def _smu_config(self, name: str, p: Mapping[str, Any]) -> SourceMeterConfig:
        cfg = SourceMeterConfig(**{f: p["smu_" + f] for f in _smu_fields()})
        try:
            check_smu_limits(cfg, self.rig_config)
        except ConfigError as exc:
            raise ModuleError(f"{name}: smu_current_compliance_a: {exc}") from None
        return cfg

    # observers and utilities ------------------------------------------------
    def _build_power(self, p: dict, ctx: RunContext, rig: Rig) -> Iterator[E.Event]:
        if rig.power is None:
            raise ModuleError("power: no power meter on this bench -- the 1918-C "
                              "console is not answering")
        wavelength = float(p["wavelength_nm"])
        samples = int(p["samples"])

        def run() -> Iterator[E.Event]:
            rig.power.set_wavelength(wavelength)
            yield power_reading(rig.power, samples=samples)

        return run()

    def _build_temperature(self, p: dict, ctx: RunContext, rig: Rig) -> Iterator[E.Event]:
        """The temperature builder uses the same settle helper as a temperature loop.

        `temperature.settle` uses an attached 331 or pauses for the operator. A pause
        requires `ctx.wait_for_operator`. With a controller, that callback is needed
        only after timeout or refusal; a missing callback fails then, not at build.
        The executor supplies `ctx.emit` and `ctx.stop_mode` from the job, enabling
        live console polls during a pause and abort handling. The settled value
        remains in `ctx.temperature_k` for following nodes
        (`docs/service-contract.md`, section 7)."""
        if rig.temperature is None and ctx.wait_for_operator is None:
            raise ModuleError("temperature: the 331 is not wired, so this module waits "
                              "for an operator -- and the context has no "
                              "wait_for_operator hook to wait with")
        detail = {"setpoint_k": float(p["setpoint_k"]), "tolerance_k": float(p["tolerance_k"]),
                  "hold_s": float(p["hold_s"]), "timeout_s": float(p["timeout_s"])}

        def run() -> Iterator[E.Event]:
            outcome = yield from settle(rig, ctx, detail, node_path=ctx.node_path)
            if outcome.stopped:
                yield E.RunAborted(reason="requested", done=0, total=1)

        return run()

    def _build_light(self, p: dict, ctx: RunContext, rig: Rig) -> Iterator[E.Event]:
        """The light builder controls the shutter and LED and reports read-back.

        `rigs.apply_led` and `rigs.apply_shutter` also implement the corresponding
        bench actions, so a pipeline node and a bench action use the same controls.

        Refusals precede generator creation and any instrument access. A requested
        LED mode needs an LED, a shutter position needs a shutter, and the levels
        must satisfy `LedDrive`."""
        shutter_ask, led_ask = str(p["shutter"]), str(p["led_mode"])
        if shutter_ask != "leave" and rig.shutter is None:
            raise ModuleError(f"light: shutter {shutter_ask}, and this bench has no "
                              "shutter")
        if led_ask != "leave" and rig.led is None:
            raise ModuleError(f"light: led_mode {led_ask}, and this bench has no LED "
                              "source")
        if shutter_ask == "leave" and led_ask == "leave":
            raise ModuleError("light: nothing to do -- shutter and led_mode are both "
                              "'leave'. A step that changes nothing is a step somebody "
                              "meant to fill in.")
        try:
            if led_ask == "pulse":
                LedDrive(level=float(p["led_v"]), low_level=float(p["led_low_v"]),
                         frequency_hz=float(p["pulse_frequency_hz"]),
                         duty_percent=float(p["duty_percent"]),
                         threshold_v=self.rig_config.led_threshold_v)
        except IlluminationError as exc:
            raise ModuleError(f"light: {exc}") from None
        settle_s = float(p["settle_s"])

        def run() -> Iterator[E.Event]:
            asked: dict[str, Any] = {}

            def set_led() -> None:
                asked.update(apply_led(
                    rig.led, led_ask, level=p["led_v"], low=p["led_low_v"],
                    frequency_hz=p["pulse_frequency_hz"], duty_percent=p["duty_percent"],
                    threshold_v=self.rig_config.led_threshold_v))

            def set_shutter() -> None:
                asked.update(apply_shutter(rig.shutter, shutter_ask == "open"))

            # Shut the shutter before changing the generator and open it last.
            # This prevents unrequested exposure from changing the sample before
            # a dark measurement. Opening last ensures the requested level is
            # set before light reaches the sample, avoiding a brief exposure at
            # the preceding node's level.
            if shutter_ask == "shut":
                set_shutter()
                if led_ask != "leave":
                    set_led()
            else:
                if led_ask != "leave":
                    set_led()
                if shutter_ask != "leave":
                    set_shutter()
            if settle_s > 0:
                ctx.sleep(settle_s)
            # Use the same read-back as `jv` illumination labels, so this
            # node's reported result agrees with the following curve's label.
            found = illumination_state(rig)
            yield E.InstrumentState({
                "shutter": found["shutter"] or "?",
                "illumination": ("unknown" if found["lit"] is None
                                 else "light" if found["lit"] else "dark"),
                "led_mode": found["led_mode"] or "?",
                "led_level_v": found["led_level_v"],
                # Include the output flag. Otherwise `LiveState` overlays mode and
                # level onto the stale Start flag, displaying off during illumination.
                "led_output": found["led_output"],
            })
            yield E.Notice("info", "light: " + _light_summary(asked, found))

        return run()

    def _build_park(self, p: dict, ctx: RunContext, rig: Rig) -> Iterator[E.Event]:
        def run() -> Iterator[E.Event]:
            rig.park()
            yield E.Notice("info", "parked: outputs off, shutter shut, relay left where it is")

        return run()

    def _build_wait(self, p: dict, ctx: RunContext, rig: Rig) -> Iterator[E.Event]:
        seconds = float(p["seconds"])

        def run() -> Iterator[E.Event]:
            ctx.sleep(seconds)
            yield E.Notice("info", f"waited {_duration(seconds)}")

        return run()

    def _build_note(self, p: dict, ctx: RunContext, rig: Rig) -> Iterator[E.Event]:
        text = str(p["text"])

        def run() -> Iterator[E.Event]:
            yield E.Notice("info", text)

        return run()


def _light_summary(asked: dict, found: dict) -> str:
    """The light result contains both the request and the instrument read-back.

    The console must display the read-back as the result. A requested state is
    not evidence that the instrument reached it."""
    parts = []
    if "mode" in asked:
        level = asked.get("level_v", asked.get("high_v"))
        parts.append(f"LED {asked['mode']}"
                     + ("" if level is None else f" {float(level):g} V"))
    if "open" in asked:
        parts.append("shutter " + ("open" if asked["open"] else "shut"))
    lit = found.get("lit")
    read = ("light reaching the sample" if lit else
            "dark at the sample" if lit is not None else
            "cannot tell whether light reaches the sample: "
            + ", ".join(found.get("unread") or ["no reason given"]))
    return (" · ".join(parts) or "nothing set") + f" → {read}"


# -- the data endpoint's arrays ------------------------------------------
class _BaceData:
    """The transient data store collects full arrays from events for one node.

    It serves `GET /runs/{id}/data` independently of the recorder. An abort
    before recorder flush must still retain every completed shot in memory."""

    def __init__(self, run_cfg: RunConfig, rig_cfg: RigConfig):
        self.run_cfg, self.rig_cfg = run_cfg, rig_cfg
        self.started = False
        self.requested = 0
        self.kept = 0
        self.n_loops = self.n_steps = 0
        self.axis: dict | None = None
        self.values: np.ndarray | None = None
        self.voc: float | None = None
        self.charges: ChargeAccumulator | None = None
        self.light: RunningAverage | None = None
        self.dark: RunningAverage | None = None
        self.photo: np.ndarray | None = None
        self.dt = float("nan")
        self.last_shot: dict | None = None
        self.finished: E.RunFinished | None = None

    def handle(self, ev: E.Event) -> None:
        if isinstance(ev, E.RunStarted):
            self.started = True
            self.n_loops, self.n_steps = ev.n_loops, ev.n_steps
            self.requested = ev.n_shots
            self.charges = ChargeAccumulator(n_loops=ev.n_loops, n_steps=ev.n_steps)
        elif isinstance(ev, E.AxisResolved):
            a = ev.axis
            self.axis = {"name": a.name, "unit": a.unit, "start": a.start, "stop": a.stop,
                         "step": a.step, "centre_on_voc": bool(a.centre_on_voc)}
            self.values = np.asarray(ev.values, dtype=float)
            self.voc = ev.voc
        elif isinstance(ev, E.StepDone):
            n = ev.light.n
            if self.light is None:
                self.light = RunningAverage(self.n_steps, n)
                self.dark = RunningAverage(self.n_steps, n)
                self.photo = np.full((self.n_steps, n), np.nan)
            self.dt = ev.light.dt
            self.light.update(ev.step, ev.loop, ev.light.y)
            self.dark.update(ev.step, ev.loop, ev.dark.y)
            self.photo[ev.step - 1] = ev.photo_averaged
            if self.charges is not None:
                self.charges.add(ev.loop, ev.step, ev.q)
            self.kept += 1
            self.last_shot = self._shot(ev)
        elif isinstance(ev, E.RunFinished):
            self.finished = ev

    def _shot(self, ev: E.StepDone) -> dict:
        """The shot data includes a running Q integral over its integration window.

        It matches the LabVIEW Integrated PhotoCurrent plot. The window uses this
        shot's `StepDone.t0_int_record_s` and `StepDone.t1_int_record_s`, relative
        to the pulse. The final integral equals the shot's Q
        (`docs/service-contract.md`, section 6)."""
        dt = ev.light.dt
        t0 = ev.t0_int_record_s
        t1 = ev.t1_int_record_s
        t = np.arange(ev.photo.size) * dt
        inside = t > (t0 if t0 is not None else -np.inf)
        if t1 is not None:
            inside &= t <= t1
        return {"light": ev.light.y, "dark": ev.dark.y, "photo": ev.photo,
                "cumulative_q": np.cumsum(ev.photo[inside]) * dt,
                "t0_int_record_s": None if t0 is None else float(t0),
                "t1_int_record_s": None if t1 is None else float(t1),
                "index": ev.index, "loop": ev.loop,
                "step": ev.step, "axis_value": ev.axis_value, "q": ev.q}

    def data(self) -> dict:
        if self.finished is not None:
            values = np.asarray(self.finished.values, dtype=float)
            q_mean, q_std, q_all = self.finished.q_mean, self.finished.q_std, self.finished.q_all
        else:
            values = self.values if self.values is not None else np.empty(0)
            if self.charges is not None:
                q_mean, q_std = self.charges.summary()
                q_all = self.charges.all_charges
            else:
                q_mean = q_std = np.empty(0)
                q_all = np.empty((0, 0))
        n = self.photo.shape[1] if self.photo is not None else 0
        empty = np.empty((self.n_steps, 0))
        return {"axis": self.axis, "values": values, "q_mean": q_mean, "q_std": q_std,
                "q_all": q_all, "time_s": np.arange(n) * self.dt if n else np.empty(0),
                "light": self.light.traces if self.light is not None else empty,
                "dark": self.dark.traces if self.dark is not None else empty,
                "photo": self.photo if self.photo is not None else empty,
                "last_shot": self.last_shot, "kept": self.kept,
                "requested": self.requested, "voc": self.voc, "dt": self.dt}


def _bind_temperature(ctx: RunContext, rig: Rig) -> tuple[float | None, str, str]:
    """The node temperature uses a binding or a controller reading.

    `ctx.temperature()` supplies a tree binding or the session's typed value.
    Without either, the controller is read once at node start. The result uses
    `how = "read"` and the same source classifier as settling. Binding it to
    `ctx` keeps the executor's `NodeDone` consistent with the file. An absent or
    unresponsive controller leaves temperature unrecorded rather than assigning
    an unmeasured value (`docs/service-contract.md`, section 7)."""
    kelvin, how, source = ctx.temperature()
    if kelvin is not None or getattr(rig, "temperature", None) is None:
        return kelvin, how, source
    try:
        reading = rig.temperature.read()
    except Exception:                                       # noqa: BLE001
        return kelvin, how, source
    k = getattr(reading, "kelvin", None)
    if not getattr(reading, "connected", False) or k is None or not math.isfinite(k):
        return kelvin, how, source
    from .temperature import source_of
    ctx.temperature_k = float(k)
    ctx.temperature_how = "read"
    ctx.temperature_source = source_of(rig.temperature)
    return ctx.temperature()


class _JVData:
    def __init__(self):
        self.curves: list[dict] = []

    def handle(self, ev: E.Event) -> None:
        if isinstance(ev, JVCurveDone):
            self.curves.append({
                # Preserve tri-state `dark`: `bool(None)` would label unknown
                # illumination as light. `illumination` matches the HDF5 attribute
                # so API and file readers need not infer this convention.
                "label": ev.label, "dark": ev.dark, "led_level_v": ev.led_level_v,
                "illumination": ("unknown" if ev.dark is None
                                 else "dark" if ev.dark else "light"),
                "direction": ev.direction, "voltage": np.asarray(ev.voltage, dtype=float),
                "current": np.asarray(ev.current, dtype=float),
                "density": None if ev.density is None else np.asarray(ev.density, dtype=float),
                "metrics": ev.metrics.as_dict(), "intensity_w": ev.intensity_w})

    def data(self) -> dict:
        return {"curves": list(self.curves)}


def jsonable(value: Any) -> Any:
    """The JSON conversion preserves full precision for `on_data`.

    Arrays become lists, NumPy scalars become Python values, and nonfinite values
    become `None`, because JSON has no representation for them. Decimation
    belongs to the wire policy."""
    if isinstance(value, np.ndarray):
        return jsonable(value.tolist())
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, np.generic):
        return jsonable(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


# -- helpers ----------------------------------------------------------------
def _settle_led(rig: Rig, ctx: RunContext, *, settle_s: float, max_s: float,
                tolerance: float) -> Iterator[E.Event]:
    """The LED settle waits for stable power after DC changes to pulse.

    A 2 s wait is insufficient on this bench: changing LED power during the
    first points makes their Q correspond to illumination other than the recorded
    level. The 1918-C behind the open shutter sees the device's illumination.
    `read_power()` is polled every `LED_SETTLE_POLL_S`. Every reading over the
    last `LED_SETTLE_SPAN_S` must agree within `tolerance` (spread over mean),
    and at least `settle_s` must elapse.

    Measured on this bench, three readings over 1.5 s can agree while intensity
    falls 18 % over the following 40 s. The window spans time rather than a
    reading count to reduce sensitivity to slow drift. At `max_s`, a warning
    allows the scan to proceed with uncertain stability rather than wait forever.
    Elapsed time is polls times `LED_SETTLE_POLL_S`, so `--fast` takes no wall
    time and tests can determine the settle time.

    A meter exception produces one warning and the configured wait without meter
    feedback. A failed console must not block a scan that does not require it.
    The stop flag is not polled here. `after_shot` keeps a settle intact; the
    scan checks the stop before its first point. Abort closes the generator at
    its next yield (`docs/service-contract.md`, section 2)."""
    meter = rig.power
    if meter is None:
        ctx.sleep(settle_s)
        return
    readings: list[float] = []
    elapsed = 0.0
    while True:
        ctx.sleep(LED_SETTLE_POLL_S)
        elapsed += LED_SETTLE_POLL_S
        # Act on abort after the session's sleep returns early. Leave
        # `after_shot` for the scan's check before its first point; a settle
        # is not a shot and need not start an acquisition to honor the stop.
        if ctx.stop_mode is not None and ctx.stop_mode() == "abort":
            from .worker import AbortNow
            raise AbortNow("LED settle abandoned: abort requested")
        try:
            readings.append(float(meter.read_power()))
        except Exception as exc:                        # noqa: BLE001
            yield E.Notice("warning", f"power meter not read during the LED settle "
                                      f"({exc}); waiting the fixed {settle_s:g} s instead")
            ctx.sleep(max(0.0, settle_s - elapsed))
            return
        span = _span_readings(readings)
        if elapsed >= settle_s and _agree(span, tolerance):
            yield E.Notice("info", f"LED settled in {elapsed:g} s at {readings[-1]:.2e} W "
                                   f"({len(span)} readings over {LED_SETTLE_SPAN_S:g} s "
                                   f"within {tolerance * 100:g} %)")
            break
        if elapsed >= max_s:
            last = ", ".join(f"{r:.2e}" for r in readings[-3:])
            yield E.Notice("warning", f"LED did not stabilise within {max_s:g} s: last "
                                      f"readings {last} W; going on")
            break
    yield E.InstrumentState({"led_power_w": f"{readings[-1]:.2e}",
                             "led_settle_s": f"{elapsed:g}"})


_SPAN_POLLS = max(2, int(round(LED_SETTLE_SPAN_S / LED_SETTLE_POLL_S)))


def _span_readings(readings: list[float]) -> list[float]:
    """The settle readings cover the last `LED_SETTLE_SPAN_S`, latest last."""
    return readings[-_SPAN_POLLS:]


def _agree(readings: list[float], tolerance: float) -> bool:
    """The stability check requires a full span within `tolerance` of its mean.

    The spread is relative to the mean. A shorter span does not establish
    stability. A zero mean agrees only if every reading is exactly zero, which
    a real meter does not return."""
    if len(readings) < _SPAN_POLLS:
        return False
    mean = sum(readings) / len(readings)
    if mean == 0.0:
        return all(r == 0.0 for r in readings)
    return (max(readings) - min(readings)) / abs(mean) <= tolerance


def _axis_of(p: Mapping[str, Any]) -> Axis:
    try:
        return Axis(name=p["axis_name"], start=float(p["axis_start"]),
                    stop=float(p["axis_stop"]), step=float(p["axis_step"]),
                    centre_on_voc=voc_flags(p)[0])
    except AxisError as exc:
        raise ModuleError(f"bace: axis: {exc}") from None


def _jv_points(p: Mapping[str, Any]) -> np.ndarray:
    return JVConfig(start_v=float(p["start_v"]), stop_v=float(p["stop_v"]),
                    step_v=float(p["step_v"])).points()


def _apply_smu_config(rig: Rig, cfg: SourceMeterConfig) -> None:
    """The run compliance is applied to drivers that accept a configuration.

    The real `Keithley2400` stores a `SourceMeterConfig`. The simulator has no
    configuration control and is left unchanged."""
    smu = rig.smu
    if smu is not None and isinstance(getattr(smu, "config", None), SourceMeterConfig):
        smu.config = cfg


def _float_or_none(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} s" if seconds >= 10 else f"{seconds:.1f} s"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} h"


def _count(n: int, one: str) -> str:
    """`1 curve`, `2 curves`: a count with its noun in the matching number."""
    return f"{n} {one}" if n == 1 else f"{n} {one}s"


class _null:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False
