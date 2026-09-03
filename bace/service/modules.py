"""The catalogue: the modules the service can run, their parameters with
provenance, and the one place a module's parameters become dataclasses.

`/modules` is rendered from here and `/runs` is executed from here, and the
two have to agree exactly -- the number the console shows for `n_averages`
must be the number `RunConfig` gets. The only way to guarantee that is one
table read both ways. So every module is a `ModuleSpec` plus a list of
`ParamSpec`s, mostly read off the real dataclasses with
`params.specs_from_dataclass` so a default cannot drift from the code, and
`build()` is the single function that turns a resolved `{name: value}` dict
into `Axis`, `ScanSpec`, `RunConfig`, `JVConfig`, `LedDrive` and a recorder,
and returns the event generator the worker pumps.

What `build()` refuses, it refuses before an instrument is touched: a `bace`
centred on V_oc with no source in scope, a V_oc measured at a different LED
level than the one about to be pulsed, LED levels the threshold rule
rejects, an axis the geometry rejects, a compliance above the bench ceiling.
Each of those would otherwise produce a full set of plausible files -- the
axis centred on nothing, the charge read against the wrong illumination --
and be discovered, if at all, in analysis. `ModuleError` names the module
and the parameter so the console can put the message on the right field.

Layers, lowest first: dataclass defaults, `run.toml` (mapped per module in
`_TOML`), last-used (the journal), edited (this session), then whatever the
pipeline injects as inherited or derived on the copy it resolves. The
catalogue holds only the edited layer and rebuilds the `ParamSet` fresh on
every `param_set()` call, so a resolver that adds an inherited layer to one
copy cannot leak it into the bench card.

The light. No module switches the LED generator off: a `bace` leaves the
33220A pulsing and a J-V leaves it at DC, and the shutter -- which every
unwind shuts -- is the light switch (operator instruction, 2026-09-02, after
a real run: a generator that is cycled loses its thermal steady state and
the next module waits for it again). After the 33220A goes from DC to pulse
a `bace` opens the shutter and waits for the power meter behind it to read
stable (`_settle_led`) rather than a fixed `led_settle_s`, because the fixed
2 s was seen not to be enough on the rig that day.

Nothing here imports FastAPI, and nothing here sleeps except through
`RunContext.sleep`, so `--fast` and the tests run a 20-loop scan in seconds.
"""
from __future__ import annotations

import dataclasses
import math
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Iterator, Mapping

import numpy as np

from ..config import ConfigError, check_smu_limits
from ..core.axis import Axis, AxisError, ScanSpec
from ..core.illumination import IlluminationError, LedDrive, assert_axis_centre
from ..core.process import ChargeAccumulator, RunningAverage
from ..core.pulses import pulse_levels
from ..drivers.keithley2400 import SourceMeterConfig
from ..experiment import events as E
from ..experiment.jv import (JVConfig, JVCurveDone, illumination_state, run_jv)
from ..experiment.rig import Rig, RigConfig
from ..experiment.transient import RunConfig, resolve_t0_int, run_transient_scan
from ..params import (ParamError, ParamSet, ParamSpec, Source, field_docs,
                      specs_from_dataclass, toml_layer)
from ..storage import jv as jv_storage
from ..storage.jv import JVRecorder
from ..storage.naming import RunMetadata
from ..storage.recorder import RunRecorder, record
from .rigs import apply_led, apply_shutter, power_reading
from .temperature import settle

DEFAULT_SHOT_S = 0.8
"""What one bace shot costs when the journal has never seen one. The
2026-08 archive averaged about that at 20 averages; the cost model says
`t_shot_source = "default"` so the estimate is not mistaken for a measurement."""

JV_POINT_OVERHEAD_S = 0.05
"""Per-point instrument overhead of a Keithley sweep beyond the settle."""

VOC_LEVEL_TOLERANCE_V = 1e-9
"""How close a V_oc source's LED level must be to the level about to be
pulsed. The same number `core.illumination.assert_axis_centre` uses: the
coupling is an equality, and this is only floating-point slack."""

LED_SETTLE_POLL_S = 0.5
"""How often `_settle_led` reads the power meter while the LED settles after
DC -> pulse. Elapsed time is counted on this clock (polls x this), not on
the wall clock, so `--fast` is instant and a test can say exactly how long
the wait took."""

LED_SETTLE_SPAN_S = 10.0
"""How long the meter must read flat before the LED counts as settled.
Three readings in 1.5 s passed on the rig at 14:52 (2026-09-02) while the
LED was still drooping: the scan then watched the intensity fall 20.8 to
17.0 uW over the next 40 s. A slow thermal drift is invisible inside any
window shorter than itself; ten seconds of flat readings is what the
operator's own eyes-on-the-meter check amounts to."""

MODULE_NAMES: tuple[str, ...] = ("jv", "jv_bace", "bace", "light", "power",
                                 "temperature", "park", "wait", "note")

RETIRED: dict[str, str] = {
    "jv_dark": "split on 2026-09-03 because it did two jobs: it swept the "
               "SourceMeter *and* made the bench dark. Use a `light` node with "
               "`shutter = shut` before a `jv`, which is exactly what it did; "
               "or `jv` on its own to sweep under the light as it is found.",
}
"""Module names that existed and no longer do, and what to do instead.

A tree naming one of these is refused — never silently rewritten. `jv_dark`
maps to *two* nodes, so translating it would change the shape of the tree, its
node paths and therefore its folder names; and translating it to `jv` alone
would change what is measured, from "make it dark and sweep" to "sweep under
whatever is there", which is the failure this split exists to prevent. A saved
recipe that names one is the operator's to re-author, and this says how.

Kept because a recipe saved through `/pipelines/save` lives on disk and
outlives the catalogue: without it the answer is `jv_dark: 'jv_dark'`, a
`KeyError` repr that says nothing about what happened or what to do."""

SAMPLE_KEYS: frozenset[str] = frozenset(
    {"sample", "material", "pixel", "temperature_k", "operator", "comment"})
"""What `[sample]` may carry -- the `RunMetadata` fields `config.load_run`
reads from it. Not module parameters: they describe the session."""


def _read_led_state(led) -> dict[str, str]:
    """The LED generator as it answers: `output`, `mode`, `frequency_hz`,
    `high_v`, `low_v`, `polarity`, `sync_output` -- strings, `?` for what it
    will not say. `read_state` is the real driver's (not a protocol member);
    a driver without it reports its cached flags, which is what the
    simulator has."""
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
    # The Sync connector (`OUTP:SYNC?`) is what arms the 81150A, which is
    # what triggers the scope; it has its own front-panel key and nothing
    # else in the chain shows it off. Only the real driver answers it.
    sync = state.get("sync_output", None) if state else None
    out["sync_output"] = "?" if sync is None else ("ON" if sync else "OFF")
    # The error queue, drained: `:VOLT:HIGH` below the LOW in force, or a
    # width that no longer fits the period, is refused by the 33220A with
    # `-221 Settings conflict` and no other symptom, and the run path
    # never reads the queue itself.
    errors = getattr(led, "errors", None)
    if callable(errors):
        try:
            out["errors"] = "; ".join(str(e) for e in errors()) or ""
        except Exception:                               # noqa: BLE001
            out["errors"] = "?"
    return out


def _led_problems(state: dict[str, str], wanted: dict) -> list[str]:
    """What the read-back contradicts in the recipe. `?` is not a problem:
    a driver that cannot answer (the simulator) is not a generator that
    said no."""
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
    # Polarity is deliberately NOT refused here: NORM is the chain card's
    # warn with a one-click fix, and a warn never blocks (the three-tier
    # rule). It is in the read-back so the file says which edge armed.
    errors = state.get("errors", "")
    if errors and errors != "?":
        problems.append(f"the generator rejected a command: {errors}")
    return problems


class ModuleError(ValueError):
    """A module or parameter the catalogue refuses. The message starts with
    the module's name and, where one parameter is at fault, that parameter's
    name after it, so the console can attach it to the right field."""


# -- the shared interface ---------------------------------------------------
@dataclass(frozen=True)
class ModuleSpec:
    """One catalogue entry as the pipeline and the routes see it."""

    name: str
    title: str
    kind: str
    """`measurement` (touches the bus for a while), `observer` (a reading),
    `utility` (the bench, a pause, a note)."""
    status: str
    """`built`, `partial` or `not wired` -- what the console shows on the card."""
    relay: str | None
    """`dc` or `transient` when the module needs the device on that side of
    the relay; the executor draws a transition at every change."""
    provides_voc: bool
    """True for the module whose light curve yields the V_oc a `bace` centres on."""
    needs_voc_param: str | None
    """The bool parameter that, when set, makes a V_oc source mandatory."""
    led_params: tuple[str, ...]
    """Parameters an illumination loop owns; a module inside one may not set them."""
    groups: tuple[str, ...]


@dataclass(frozen=True)
class VocSource:
    """Where the V_oc a `bace` centres on came from.

    `led_v` is the drive the value was measured at -- the number the coupling
    invariant is checked against. `how` is `jv_bace`, `measure_dc` or
    `typed`; only the last is not a measurement and the validator warns on it.
    """

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
    """What a module needs from the run it is part of.

    `led_v`/`led_low_v` are the illumination loop's binding (None outside
    one); `voc` the V_oc source in scope, which `bace` replaces with the one
    it actually centred on; `temperature_k` the context temperature a
    temperature node settled at or an operator typed, with `temperature_how`
    and `temperature_source` saying which -- the `how` and `source` of the
    `Settled` it came from, so the file can tell a reading from a wish.
    `sleep` is a no-op under `--fast`;
    `abort` is the worker's flag. `resolved` seeds the recorder's readbacks
    (the chain from the last read-back). `on_data` receives the full-
    precision arrays at the end so the session can serve `/runs/{id}/data`;
    `folders` collects every recorder folder the module wrote, which is how
    the executor learns them. `wait_for_operator` is the worker's blocking
    wait, handed in because the pause lives in the job, not in the module;
    it is called as `wait_for_operator(detail, on_poll=callable)` and must
    run `on_poll` every few hundred milliseconds while it blocks, which is
    how the 331 is read live during a pause. `emit` delivers an event out of
    band while the module is blocked in that wait, and `stop_mode` says
    which stop was asked for (`worker.StopMode`, or None) so a settle can
    tell an abort from an after_shot; the executor fills both from the job
    when the factory left them unset.
    """

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
        """`(kelvin, how, source)` for a module's metadata: the tree's binding
        when a temperature node made one, the session's typed number
        otherwise. The three travel together on purpose -- a folder named
        `220K` reads the same whether the console settled there or a loop
        merely asked, and only `how` separates them."""
        if self.temperature_k is not None:
            return self.temperature_k, self.temperature_how, self.temperature_source
        return (self.metadata.temperature_k, self.metadata.temperature_how,
                self.metadata.temperature_source)


# -- the specs --------------------------------------------------------------
RUN_CHOICES: dict[str, tuple[str, ...]] = {
    "t0_int_reference": ("record", "trigger", "pulse"),
    "dark_reference": ("translated", "same"),
    "output_polarity": ("auto", "NORM", "INV", "leave"),
    "trigger_sweep": ("AUTO", "TRIG"),
}
"""What `RunConfig.__post_init__` and the scope driver accept. The dataclass
validates in code rather than in its annotations, so the catalogue says it."""

RUN_UNITS: dict[str, str] = {
    "timebase_ns_per_div": "ns/div", "t0_int_s": "s", "pulse_width_ns": "ns",
    "pulse_frequency_hz": "Hz", "duty_percent": "%", "settle_s": "s",
    "dark_settle_s": "s", "shutter_settle_s": "s", "acquisition_timeout_s": "s",
}

RUN_GROUPS: dict[str, str] = {
    **{f: "acquisition" for f in ("n_averages", "timebase_ns_per_div", "record_length",
                                  "read_intensity", "acquisition_timeout_s")},
    **{f: "processing" for f in ("t0_int_s", "t0_int_reference", "offset_correct",
                                 "invert_polarity", "dark_reference")},
    **{f: "timing" for f in ("settle_s", "dark_settle_s", "shutter_settle_s",
                             "pulse_width_ns", "pulse_frequency_hz", "duty_percent")},
    **{f: "trigger" for f in ("external_trigger", "trigger_slope_positive",
                              "trigger_sweep", "calibrate_trigger")},
    **{f: "output" for f in ("output_polarity", "inverted_output")},
}
"""Every `RunConfig` field, by the group the card folds it under. A field
added to `RunConfig` and not placed here is refused when the catalogue is
built, so it cannot land on the card unlabelled."""

SMU_UNITS: dict[str, str] = {
    "current_compliance_a": "A", "voltage_compliance_v": "V", "settle_jsc_ms": "ms",
    "settle_voc_ms": "ms", "settle_jsat_ms": "ms",
}

AXIS_DOCS: dict[str, str] = {
    "axis_name": "Which quantity is swept: vpre (BACE), delay_ns (TDCF) or vcoll.",
    "axis_start": "First value of the swept quantity; an offset from V_oc when "
                   "centre_on_voc is on.",
    "axis_stop": "Last value of the swept quantity; start = stop is one point "
                  "(repeats come from n_loops).",
    "axis_step": "Spacing of the swept values; the point count is rounded from the "
                  "span, never truncated.",
    "centre_on_voc": "Make start/stop offsets from the V_oc measured under the "
                     "illumination in force (vpre only).",
}

PINNED_DOCS: dict[str, str] = {
    "vpre": "Prebias at the device, sourced by the 81150A -- `core.pulses` divides it "
            "by the amplifier gain before it is written -- when it is not the swept "
            "axis; an offset from V_oc when vpre_on_voc is set.",
    "vcoll": "Collection bias at the device, sourced by the 81150A through the same "
             "gain division, when it is not the swept axis.",
    "delay_ns": "How long the 81150A waits after the LED falling edge before it fires "
                "the collection pulse (`:PULS:DEL1`).",
    "n_loops": "How many times the axis is swept; the statistics tighten with each.",
}

VPRE_ON_VOC_SPEC = ParamSpec(
    "vpre_on_voc", "bool", False, group="pinned",
    doc="Pin the prebias relative to V_oc: vpre becomes an offset from the V_oc in "
        "scope (TDCF at V_oc is vpre_on_voc with vpre = 0). Only when vpre is not the "
        "swept axis; a swept vpre follows V_oc through centre_on_voc.")
"""The design's inherited "vpre = V_oc + 0.000 V" when delay_ns or vcoll is
the swept axis. The engine takes an absolute pinned prebias, so the offset
is resolved in `_build_bace` from the same V_oc source `centre_on_voc` uses,
under the same coupling check, once that V_oc is known."""


def _regroup(specs: list[ParamSpec], groups: Mapping[str, str], cls: type) -> list[ParamSpec]:
    out = []
    for s in specs:
        if s.name not in groups:
            raise ModuleError(
                f"catalogue: {cls.__name__}.{s.name} has no group; add it to the "
                "group table so it does not land on the card unlabelled")
        out.append(replace(s, group=groups[s.name]))
    return out


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
"""`JVConfig` fields that are never a form field on either J-V card.

`light_control` is the module's identity, not a choice inside it: `jv` leaves
the light alone and `jv_bace` manages it, and a form that let either be
switched would be one module wearing two names. The other three belong to
`jv_bace`, which declares them itself in `_jv_led_specs`."""


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
                      "DC -> pulse; past it the scan goes on with a warning. A 2 s fixed "
                      "wait was seen not to be enough (2026-09-02)."),
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
    """`jv` has no illumination parameters at all, and that is the module.

    It sweeps under whatever light it finds, touching neither shutter nor
    LED, and records what it read back. The light is set -- when it is set --
    by a `light` step before it, or by the operator on the bench card. The
    absence of `dark`, `led_*` and `led_settle_s` from this list is the whole
    difference from `jv_bace`.
    """
    sweep, acq = _jv_common_specs()
    return sweep + acq + _smu_specs()


def _light_specs() -> list[ParamSpec]:
    """The bench's two light switches as one node: the shutter, and the LED.

    Every value is a *request*; the read-back that follows is the answer, and
    the node reports both. `leave` on either half means "do not touch this
    one", so a node can move the shutter without disturbing a generator that
    is already at its thermal steady state -- the operator instruction of
    2026-09-02, which is why the shutter is the light switch and the
    generator is not cycled.
    """
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
                  doc="The rate the 33220A chops the LED at; the value reaches no "
                      "other instrument (`pulse` only -- the node itself still moves "
                      "the shutter). 500 Hz is what the rig was found at "
                      "(2026-08-31), and a `bace` after this must pulse at the same "
                      "rate -- it is armed by this generator's Sync."),
        ParamSpec("duty_percent", "float", 50.0, unit="%", group="timing",
                  minimum=0.0, maximum=100.0,
                  doc="The 33220A's light/dark split within one period, so 50 % gives "
                      "the device equal light and dark halves (`pulse` only). Only "
                      "sensible near 50 %: a `bace` takes its two traces either side "
                      "of that boundary."),
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
        # The recipe's illumination level is the level jv_bace measures at by
        # default -- both ends of the range -- so a bench click on jv_bace
        # produces the V_oc the bench's bace card then needs, at the same
        # level, without anyone retyping 1.020 in two places.
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
"""Which recipe table feeds which parameter, with the detail the console
shows. `[jv]` is optional and is not read by `config.load_run`; its keys are
checked here instead, because a typo that fell back to a default would be a
J-V range nobody chose."""

_JV_TABLE_KEYS: frozenset[str] = frozenset(
    k.split(".", 1)[1] for k in _TOML["jv_bace"][-1][1])
"""Every key `[jv]` may carry: the jv_bace mapping's, which is a superset of
`jv`'s."""


# -- the catalogue ---------------------------------------------------------------
class Catalogue:
    """The runnable modules, their parameters with provenance, and `build()`.

    `run_toml` is the recipe file's raw tables (`params.run_toml_layer`), so
    provenance can name the table; `history` answers `last_used_params`,
    `settle_history` and `shot_time_s` (the journal, or a fake in tests);
    `sample` is the recipe's `[sample]` table, which becomes the session's
    `RunMetadata` and is not a module parameter.
    """

    def __init__(self, *, rig_config: RigConfig, run_toml: dict | None = None,
                 history: Any = None, sample: dict | None = None):
        self.rig_config = rig_config
        self.run_toml: dict = dict(run_toml or {})
        self.history = history
        self.sample: dict = dict(sample or {})
        unknown = sorted(set(self.sample) - SAMPLE_KEYS)
        if unknown:
            raise ModuleError(f"[sample]: unknown key(s) {', '.join(unknown)}; "
                              f"accepted: {', '.join(sorted(SAMPLE_KEYS))}")
        jv_table = self.run_toml.get("jv", {})
        unknown = sorted(set(jv_table) - _JV_TABLE_KEYS) if isinstance(jv_table, Mapping) else []
        if unknown:
            raise ConfigError(
                f"unknown key(s) in [jv]: {', '.join(unknown)}. Refusing to fall back "
                "to defaults -- a typo here is a J-V range nobody chose.")

        self._params: dict[str, tuple[ParamSpec, ...]] = {
            "jv": tuple(_jv_specs()),
            "light": tuple(_light_specs()),
            "jv_bace": tuple(_jv_bace_specs()),
            "bace": tuple(_bace_specs(rig_config)),
            "power": tuple(_power_specs(rig_config)),
            "temperature": tuple(_temperature_specs()),
            "park": (),
            "wait": tuple(_wait_specs()),
            "note": tuple(_note_specs()),
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
        # A recipe value the form would refuse surfaces now, at startup, not
        # on the first GET /modules an hour later.
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
            # A name that used to be a module says so, and says what replaces
            # it: a recipe saved through `/pipelines/save` is a file on disk
            # and outlives the catalogue, so this is the message the operator
            # gets when they open one from before the split.
            if name in RETIRED:
                raise KeyError(f"{name} is no longer a module. It was "
                               f"{RETIRED[name]}") from None
            raise KeyError(name) from None

    def base_metadata(self) -> RunMetadata:
        """The session's `RunMetadata` from `[sample]`; a module fills in the
        LED level, the V_oc and the temperature it ran at."""
        s = self.sample
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
    def param_set(self, name: str) -> ParamSet:
        """A fresh `ParamSet`: default < run.toml < last-used < edited. Fresh
        every call, so a resolver can add inherited/derived layers to its
        copy without touching what the bench card shows."""
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
                # One name at a time, and a name that no longer exists or a
                # value the spec now refuses is skipped: the journal is a
                # record of what ran, and a parameter renamed since must not
                # make the module unlistable.
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
        """`PUT /modules/{m}/params`: set the edited layer, coerced, all or
        nothing. A `None` value drops that parameter's edit so the layer
        below shows again."""
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
        """Drop one parameter's edit, or the whole edited layer."""
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
        """Every parameter of `name`: `params` where given (coerced), the
        catalogue's resolved value otherwise. Unknown names are refused --
        a misspelt override that was quietly dropped would run the module
        with a value the caller never saw."""
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
        """The `/modules` entry. `last` is None here; the session fills it
        from the run registry. `bench` is the cached snapshot, read for what
        is unavailable; `session_voc` the session's V_oc source."""
        spec = self.spec(name)
        ps = self.param_set(name)
        p = ps.values()
        if name == "bace" and session_voc is not None and p.get("voc") is None \
                and not p.get("measure_dc") and p.get("led_v") is not None \
                and abs(float(session_voc.led_v) - float(p["led_v"])) <= VOC_LEVEL_TOLERANCE_V:
            # The rule `pipeline._Resolver._voc` applies to a manual run,
            # applied to the card too: the run will centre on the session's
            # V_oc at this level, so the card says so -- value, source and
            # provenance, and not editable -- instead of `default · null`.
            # On this fresh copy only; the catalogue's own layers are untouched.
            ps.set_layer(Source.DERIVED, {"voc": session_voc.value},
                         f"{session_voc.how} {session_voc.node_path} (this session)")
        return {"name": name, "title": spec.title, "status": spec.status,
                "kind": spec.kind, "relay": spec.relay,
                "estimate_s": self.estimate_s(name, p),
                "estimate_text": self.estimate_text(name, p),
                "needs": self.needs(name, p, bench=bench, session_voc=session_voc),
                "last": None, "params": ps.as_wire()}

    def needs(self, name: str, params: Mapping[str, Any], *, bench: dict | None = None,
              session_voc: VocSource | None = None) -> list[dict]:
        """What the module cannot run without, as `{code, text}` -- the
        card's "needs" line. Resource-shaped only: a V_oc source, a
        console, an instrument. Geometry and level checks are the validator's."""
        p = self._complete(name, params)
        unavailable: dict = dict((bench or {}).get("unavailable") or {})
        power = ((bench or {}).get("instruments") or {}).get("power") or {}
        power_silent = bool(bench) and not power.get("available", True)
        out: list[dict] = []

        def missing(role: str, why: str) -> None:
            if role in unavailable:
                out.append({"code": role, "text": f"{why}: {unavailable[role]}"})

        if name == "bace":
            needs_voc = bool(p["centre_on_voc"]) or bool(p.get("vpre_on_voc"))
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
                # Not a `missing`: `jv` runs perfectly well on a bench with
                # neither, and refusing would make the one module that needs
                # nothing the fussiest. It is the *label* that suffers, so
                # the warning says exactly that and nothing more.
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
                # The read itself failed: what answered at Start is not
                # answering now -- the link, not the instrument behind it.
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
        """`(t_shot, source)`: the journal's median shot interval or the default."""
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
        """Seconds, per the contract's cost model; 0.0 when the parameters
        do not make a runnable module (the validator says why)."""
        return self._estimate(name, params)[0]

    def estimate_text(self, name: str, params: Mapping[str, Any]) -> str:
        return self._estimate(name, params)[1]

    def _estimate(self, name: str, params: Mapping[str, Any]) -> tuple[float, str]:
        try:
            p = self._complete(name, params)
        except ModuleError as exc:
            return 0.0, f"cannot estimate: {exc}"
        if name == "bace":
            t_shot, source = self.shot_time()
            try:
                n_steps = _axis_of(p).n_points
            except (AxisError, ModuleError) as exc:
                return 0.0, f"cannot estimate: {exc}"
            n_loops = int(p["n_loops"])
            total = n_loops * n_steps * t_shot
            tag = "" if source == "journal" else " (default)"
            return total, (f"one shot ≈ {t_shot:.1f} s{tag} · {n_loops} loops × "
                           f"{n_steps} pts ≈ {_duration(total)}")
        if name in ("jv", "jv_bace"):
            try:
                n_points = int(_jv_points(p).size)
                levels = self._jv_levels(p, None) if name == "jv_bace" else ()
            except (ModuleError, ValueError) as exc:
                return 0.0, f"cannot estimate: {exc}"
            # `jv` is one curve per direction: it does not set the light, so
            # there is no dark-plus-levels plan to count, and no LED settle
            # because nothing was changed to settle after.
            dark = bool(p["dark"]) if name == "jv_bace" else True
            n_curves = (int(dark) + len(levels)) * (2 if p["both_directions"] else 1)
            led_settle = float(p.get("led_settle_s", JVConfig().led_settle_s))
            if name == "jv":
                n_curves, led_settle = (2 if p["both_directions"] else 1), 0.0
            total = n_curves * (n_points * float(p["settle_s"])
                                + n_points * JV_POINT_OVERHEAD_S + led_settle)
            return total, f"{n_curves} curves × {n_points} pts ≈ {_duration(total)}"
        if name == "power":
            total = 0.1 * max(1, int(p["samples"]))
            return total, "one reading" if p["samples"] <= 1 else f"{p['samples']} samples"
        if name == "temperature":
            hold = float(p["hold_s"])
            return hold, f"settle — (331 or operator) + hold {_duration(hold)}"
        if name == "wait":
            return float(p["seconds"]), _duration(float(p["seconds"]))
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
                f" + {_duration(settle)}" if settle > 0 else "")
        if name == "park":
            return 1.0, "outputs off, shutter shut"
        return 0.0, "journal entry"

    def _jv_levels(self, p: Mapping[str, Any], ctx: RunContext | None) -> tuple[float, ...]:
        """jv_bace's light levels: the inherited or typed `led_v` -> one level,
        else the range, with the point count rounded as `SeriesConfig.levels`
        rounds it (truncation dropped the last level often enough to matter)."""
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
        """The module's event generator, already wrapped in its recorder and
        its router context. Everything refusable is refused here, before the
        generator exists, so nothing has touched an instrument when a
        `ModuleError` comes back."""
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
        # `jv` leaves the light alone, so its `dark` and `led_settle_s` are
        # never read; jv_bace always has at least one level (a range collapses
        # to its start), so "nothing to measure" cannot arise there and
        # run_jv's own refusal stays the only one.
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
        temperature, temp_how, temp_source = ctx.temperature()
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
        vpre_on_voc = bool(p.get("vpre_on_voc", False))
        if vpre_on_voc and axis.name == "vpre":
            raise ModuleError(f"{name}: vpre_on_voc: vpre is the swept axis here, so there "
                              "is no pinned prebias to offset from V_oc; centre_on_voc is "
                              "the flag for a swept vpre")
        needs_voc = axis.centre_on_voc or vpre_on_voc
        try:
            spec = ScanSpec(axis=axis, vpre=p["vpre"], vcoll=p["vcoll"],
                            delay_ns=p["delay_ns"], n_loops=int(p["n_loops"]))
            pulse_levels(spec.vpre, spec.vcoll, self.rig_config.pulse_amp, spec.delay_ns,
                         run_cfg.pulse_width_ns, invert=run_cfg.invert_polarity,
                         trigger_offset_s=self.rig_config.trigger_offset_s)
        except (AxisError, ValueError) as exc:
            raise ModuleError(f"{name}: {exc}") from None
        try:
            drive = LedDrive(level=led_v, low_level=led_low_v,
                             frequency_hz=run_cfg.pulse_frequency_hz,
                             duty_percent=run_cfg.duty_percent,
                             threshold_v=self.rig_config.led_threshold_v)
        except IlluminationError as exc:
            raise ModuleError(f"{name}: led_v: {exc}") from None
        smu_cfg = self._smu_config(name, p)
        measure_dc = bool(p["measure_dc"])
        if measure_dc and rig.smu is None:
            raise ModuleError(f"{name}: measure_dc: this bench has no SourceMeter")
        if measure_dc and rig.router is None:
            raise ModuleError(f"{name}: measure_dc: no Router -- the SourceMeter and "
                              "the amplifier share the device node, and without the "
                              "relay both would be connected at once")

        # The V_oc source, in the contract's order for a manual run
        # (section 5): a value typed by hand, then the one in scope (the
        # session's jv_bace, or the pipeline's), then measure_dc. In the
        # pipeline the resolver has already picked one -- a schedule that
        # chose a measurement clears `voc`, one that kept a typed value
        # leaves `ctx.voc` unset -- so the two never both arrive; this order
        # governs only a direct build() with both set, and there the typed
        # number the operator just entered wins.
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

        temperature, temp_how, temp_source = ctx.temperature()
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
                    # V_oc under steady light, the way the intensity series
                    # takes it: the 33220A at DC on the level the pulse will
                    # have, the shutter open so light reaches the sample
                    # (`Rig.park()` shut it before this job), and the device
                    # given `led_settle_s` to reach its light steady state.
                    # A Keithley reading the cell under the 500 Hz square
                    # wave would integrate over on and off phases and report
                    # a time average that is nobody's V_oc; with the shutter
                    # shut it reports the dark one. Either centres the axis
                    # on the wrong number, and the coupling check -- which
                    # compares drive levels -- cannot tell.
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
                rig.led.set_pulse(s["high"], s["low"], frequency_hz=s["frequency"],
                                  duty_percent=s["duty"])
                rig.led.enable_output(True)
                # The 33220A is the master clock: its Sync arms the 81150A,
                # whose Sync triggers the scope. Read it back (the real
                # driver asks :OUTP? / FUNC:SHAP? / :FREQ? / OUTP:SYNC?)
                # and refuse a generator that is off, in DC, at another
                # frequency, or with its Sync off before the scan calibrates
                # against a silent CHAN3. The first real service run
                # (2026-09-02, session 115857) had nothing in its file
                # saying whether either generator was on; the reading goes
                # into the file either way. Before the settle below, so a
                # generator that took nothing is refused at once and not
                # after a minute spent watching a meter read stable dark.
                led_state = _read_led_state(rig.led)
                yield E.InstrumentState({f"led_{k}": v for k, v in led_state.items()})
                problems = _led_problems(led_state, s)
                if problems:
                    raise ModuleError(f"{name}: the 33220A is not driving the LED as "
                                      f"the recipe asks: {'; '.join(problems)}")

                # Then the light: the shutter open, so the meter behind it
                # sees the LED and the device is lit, as in the LabVIEW
                # program, which keeps the light on; and the LED given
                # until the meter reads stable, not a fixed led_settle_s.
                # The shutter stays open: the scan's first shot unblocks it
                # anyway and its finally shuts it.
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
                    # The pinned prebias as the engine wants it: absolute,
                    # the offset the card shows added to the V_oc just
                    # resolved (a jv_bace's, measure_dc's, or a typed one).
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
                # The LED is left pulsing on purpose: the shutter is the
                # light switch (operator instruction, 2026-09-02), and a
                # generator switched off here would have to be waited for
                # again by the next module. run_transient_scan's finally
                # shuts the shutter once the scan has started; this one
                # covers a walk-away during the settle, when the shutter
                # was opened above and the scan never began.
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
        """The same settle a temperature loop makes (`service.temperature`):
        through the 331 console when the rig has one, else the pause. The
        pause needs the context's `wait_for_operator`; with a controller the
        hook is only reached after a timeout or a refusal, and a run that
        has none then fails at that point rather than here. The settle reads
        the context's `emit` and `stop_mode` (the executor fills them from
        the job), so a pause here polls the console live and an abort
        unwinds, as a loop's does. What it settles at is left on
        `ctx.temperature_k`; the executor binds it for the nodes after."""
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
        """Set the shutter and/or the LED, then say what the bench read back.

        Both halves go through `rigs.apply_led` / `rigs.apply_shutter`, which
        are the same functions the `set-led-*`, `led-off` and `shutter-*`
        bench actions call: a `light` node in a pipeline and a click on the
        bench card drive the LED through one implementation, and cannot come
        to disagree about what `pulse` means.

        Refusals are raised now, before the generator exists, so a node that
        cannot run has touched nothing: no LED for a mode that needs one, no
        shutter for a position, and `LedDrive`'s own rules on the levels.
        """
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

            # **The shutter closes first and opens last.** Both orders are the
            # same order for the same reason: the sample must not see light
            # nobody asked it to see. Closing first means the generator writes
            # that follow happen behind a shut shutter, where the node's whole
            # point may be to prepare a *dark* measurement -- an exposure
            # during those writes can change the sample before it is measured.
            # Opening last means the level is already set when light first
            # reaches it, rather than the previous node's level arriving for
            # the moment between the two calls.
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
            # The read-back is the answer, and it is the same one `jv` labels
            # its curve from -- so what this node says it did and what the
            # next node records having found are one function's opinion.
            found = illumination_state(rig)
            yield E.InstrumentState({
                "shutter": found["shutter"] or "?",
                "illumination": ("unknown" if found["lit"] is None
                                 else "light" if found["lit"] else "dark"),
                "led_mode": found["led_mode"] or "?",
                "led_level_v": found["led_level_v"],
                # The output flag too, or `LiveState` overlays mode and
                # level onto the *Start* snapshot's stale one and the rail
                # shows the LED off through an illuminated sweep.
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
    """What the node did, and what the bench then read. Both, always: the
    request is what the operator asked for and the read-back is what the
    instruments say, and `ui-rules` §6 wants the second rendered, never the
    first dressed up as it."""
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
    """What `GET /runs/{id}/data` serves for a bace node, accumulated from the
    events as they pass. Never read out of the recorder: its job is the files,
    and a run that is aborted before the recorder flushes still has every
    shot that completed here."""

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
        """The last shot with its running integral from `t0_int` -- the
        LabVIEW "Integrated PhotoCurrent" plot. The window starts where
        `core.process.charge` starts it (`t > t0` in record time), so the
        curve's last value is the shot's `q`."""
        sp = ev.setpoint
        try:
            delay_s = pulse_levels(sp.vpre, sp.vcoll, self.rig_cfg.pulse_amp, sp.delay_ns,
                                   self.run_cfg.pulse_width_ns,
                                   invert=self.run_cfg.invert_polarity,
                                   trigger_offset_s=self.rig_cfg.trigger_offset_s).delay_s
        except ValueError:
            delay_s = 0.0
        t0 = resolve_t0_int(self.run_cfg, ev.light.t0, pulse_delay_s=delay_s)
        dt = ev.light.dt
        after = (np.arange(ev.photo.size) * dt) > t0
        return {"light": ev.light.y, "dark": ev.dark.y, "photo": ev.photo,
                "cumulative_q": np.cumsum(ev.photo[after]) * dt,
                "t0_int_record_s": float(t0), "index": ev.index, "loop": ev.loop,
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


class _JVData:
    def __init__(self):
        self.curves: list[dict] = []

    def handle(self, ev: E.Event) -> None:
        if isinstance(ev, JVCurveDone):
            self.curves.append({
                # `dark` is carried, not coerced: `bool(None)` is False, which
                # would have the data endpoint report a curve nobody could read
                # as a *known light* one. `illumination` says the same thing
                # the file's own attribute says (`bace-jv/3`), so a reader of
                # either does not have to know the tri-state convention.
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
    """The `on_data` dict as JSON: arrays to lists, numpy scalars to Python
    ones, NaN and infinity to None (JSON has no spelling for them). Full
    precision -- nothing is decimated here; that is the wire policy's call."""
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
    """Wait for the LED to reach its steady state after DC -> pulse, on the
    power meter when the rig has one, on the clock otherwise.

    The operator watched a real run on 2026-09-02 and saw the fixed 2 s
    `led_settle_s` was not enough after the 33220A switched from DC to
    pulse: the LED's output was still moving when the scan began, and every
    charge in the first steps was taken under an intensity that was not the
    one recorded. The 1918-C behind the shutter sees exactly the light the
    device does, so it is asked: `read_power()` every `LED_SETTLE_POLL_S`
    until every reading of the last `LED_SETTLE_SPAN_S` agrees within
    `tolerance` (spread over mean) and at least `settle_s` has passed. A
    short window is blind to a slow drift -- three readings in 1.5 s
    passed at 14:52 on the rig while the intensity went on to fall 18 %
    over the next 40 s -- so the window is a span of time, not a count. A
    meter that never agrees gives up at `max_s` with a warning and the scan
    goes on -- a bace that never starts is worse than one that says its
    light may not have been steady. Elapsed time is counted as polls times
    `LED_SETTLE_POLL_S`, never on the wall clock, so `--fast` is instant
    and a test can say when it settled.

    A meter that raises is treated as absent -- one warning, then the fixed
    wait -- because a dead console must not stop a scan that does not need
    it. The stop flag is not polled here: an `after_shot` stop never cuts a
    settle short (the README's rule, and the scan reports the stop before
    its first step), and an `abort` closes the generator at its next yield.
    """
    meter = rig.power
    if meter is None:
        ctx.sleep(settle_s)
        return
    readings: list[float] = []
    elapsed = 0.0
    while True:
        ctx.sleep(LED_SETTLE_POLL_S)
        elapsed += LED_SETTLE_POLL_S
        # `abort` means now: the session's sleep returns early on it and
        # this is the next chance to act. `after_shot` is left alone -- a
        # settle in progress is not a shot, and the scan's own check
        # before its first step honours it without acquiring anything.
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
    """The readings of the last `LED_SETTLE_SPAN_S`, newest last."""
    return readings[-_SPAN_POLLS:]


def _agree(readings: list[float], tolerance: float) -> bool:
    """A full span of readings whose spread is within `tolerance` of
    their mean. Fewer readings than the window is not agreement; a mean of
    zero (the shutter shut, the LED dark) agrees only if every reading is
    exactly zero, which a real meter never returns."""
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
                    centre_on_voc=bool(p["centre_on_voc"]))
    except AxisError as exc:
        raise ModuleError(f"bace: axis: {exc}") from None


def _jv_points(p: Mapping[str, Any]) -> np.ndarray:
    return JVConfig(start_v=float(p["start_v"]), stop_v=float(p["stop_v"]),
                    step_v=float(p["step_v"])).points()


def _apply_smu_config(rig: Rig, cfg: SourceMeterConfig) -> None:
    """Hand the run's compliance to a driver that takes one. The real
    `Keithley2400` keeps a `SourceMeterConfig`; the simulated one has no such
    knob and is left alone, which is what the contract says."""
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


class _null:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False
