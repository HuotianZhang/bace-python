"""Parameter layers resolve values and report their sources to the console.

`ParamSet` holds one layer per non-default `Source`. The service's `/modules`
endpoint reports each parameter's value, source and editability. Module
catalogues bind these generic specs to dataclass fields and wire names.

The source matters when defaults, a recipe, a previous run and operator edits
disagree. Pipeline bindings and measured values also need explicit sources:
if an edited V_oc is 0.90 V and a J–V measurement supplies 0.86 V, displaying
0.90 V as edited would make the console show the wrong axis for Q.

Precedence, from lowest to highest:

    default < run.toml < last-used < edited < inherited < derived

* `default` belongs to the spec and is always present, outside the layers.
* `run.toml` supplies the versioned recipe.
* `last-used` supplies the preceding run's values. It overrides a recipe edit
  between runs, so the session retains the operator's chosen settings.
* `edited` overrides those values with the operator's explicit session edits.
* `inherited` supplies pipeline bindings such as a loop's `led_v`. It overrides
  edits because the loop controls the run; the source must show that override.
* `derived` supplies measurements such as V_oc from J–V. It overrides a binding
  because the measurement is taken inside the loop. A parameter with both
  sources indicates a pipeline-authoring error; this module reports derived
  without rejecting the pipeline.

Inherited and derived values are locked in both the wire representation and
`ParamSet.set_edited`. Accepting an edit to a locked value stores an invisible
setting that could take effect on a manual run after the binding clears.

Coercion happens before values enter a layer. Whole batches are checked before
storage, so errors include the parameter name and leave no partial update.
Forms can send counts as strings, flags as `"false"` and lists as comma-separated
text; Python's `bool("false")` would otherwise give `True`.

This module has no dependency on measurement, driver or storage modules.
`specs_from_dataclass` uses `dataclasses.fields` to bind arbitrary dataclasses.
"""
from __future__ import annotations

import ast
import collections.abc
import dataclasses
import inspect
import math
import numbers
import re
import textwrap
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import Any, Iterable, Literal, Mapping, Union, get_args, get_origin, get_type_hints


class ParamError(ValueError):
    """An invalid parameter value, unknown name or inconsistent specification.

    Messages start with the parameter name so the console can identify the
    field. A batch error without a single parameter, such as a non-mapping
    input, starts with the layer source.
    """


class Source(str, Enum):
    """The source of a parameter value, ordered by the module precedence rules."""

    DEFAULT = "default"
    RUN_TOML = "run.toml"
    LAST_USED = "last-used"
    EDITED = "edited"
    INHERITED = "inherited"
    DERIVED = "derived"


PRECEDENCE: tuple[Source, ...] = (Source.DEFAULT, Source.RUN_TOML, Source.LAST_USED,
                                  Source.EDITED, Source.INHERITED, Source.DERIVED)
"""Sources ordered from lowest to highest precedence.

`ParamSet.resolve` searches from the highest layer for each name.
"""

LOCKED: frozenset[Source] = frozenset({Source.INHERITED, Source.DERIVED})
"""Sources that make a value non-editable regardless of the specification."""

TYPES: tuple[str, ...] = ("float", "int", "bool", "str", "enum", "list[float]", "list[str]")
"""Wire types accepted by `ParamSpec.coerce` and represented in JSON.

These differ from Python annotations: `tuple[float, ...]` maps to
`list[float]` on the wire.
"""


# -- one parameter --------------------------------------------------------
@dataclass(frozen=True)
class ParamSpec:
    """A parameter's wire type, default, display metadata and validation rules.

    `nullable` permits `None`, JSON `null` or an empty field in `coerce`.
    `specs_from_dataclass` infers it from a nullable annotation or `None`
    default. This lets a value such as `SeriesConfig.dc_settle_s` return to
    `None`, meaning the driver's settle times, after holding a number.

    `editable` controls whether the console can submit the parameter. A bench
    ceiling can be visible but read-only. `ParamSet` also locks inherited and
    derived values according to their source.

    `minimum` and `maximum` are inclusive. They apply to `float`, `int` and
    each `list[float]` element; other types reject bounds.

    The default is coerced during construction, so the spec cannot advertise
    a default its own form rejects. Lists become tuples and enum values use
    the canonical choice spelling.
    """

    name: str
    type: str
    default: Any = None
    unit: str = ""
    doc: str = ""
    """One sentence naming the instrument and the parameter's effect.

    The console shows it in the field's tooltip and help line. Names such as `smu_nplc`,
    `trigger_sweep` and `duty_percent` need this context. The description must
    distinguish instrument polarity (`inverted_output`) from arithmetic on
    levels (`invert_polarity`).
    """

    doc_full: str = ""
    """Expandable help describing effects, costs and wrong-value consequences.

    An empty value makes `doc` the complete help. Keeping the full text apart
    lets the console show one line per field with the rest available on demand.
    """

    choices: tuple = ()
    group: str = ""
    editable: bool = True
    minimum: float | None = None
    maximum: float | None = None
    nullable: bool = False
    label: str = ""
    """The name the console shows: a sentence-case noun phrase without a unit,
    such as "Hardware averages". `name` stays the identifier in `run.toml`,
    files and exports. The catalogue labels every module parameter."""

    def __post_init__(self) -> None:
        if self.type not in TYPES:
            raise ParamError(f"{self.name}: unknown type {self.type!r}; "
                             f"one of {', '.join(TYPES)}")
        object.__setattr__(self, "choices", tuple(self.choices))
        if self.type == "enum" and not self.choices:
            raise ParamError(f"{self.name}: an enum needs choices, or it can never "
                             "accept a value")
        if self.type != "enum" and self.choices:
            raise ParamError(f"{self.name}: choices are given but the type is "
                             f"{self.type!r}, not 'enum' -- they would never be checked")
        if (self.minimum is not None and self.maximum is not None
                and self.minimum > self.maximum):
            raise ParamError(f"{self.name}: minimum {self.minimum!r} is above maximum "
                             f"{self.maximum!r}; no value could ever pass")
        if ((self.minimum is not None or self.maximum is not None)
                and self.type not in _BOUNDED):
            raise ParamError(f"{self.name}: a minimum or maximum is given but the type "
                             f"is {self.type!r} -- it would never be checked")
        # Defaults reach `ParamSet.values` without entering a layer, so layer
        # validation cannot protect them. An enum default outside `choices`
        # would leave the dropdown unable to select its own value and make
        # `ParamSet.reset` restore a rejected value. Coercing the default here
        # also canonicalises lists as tuples and enum choice spelling.
        if self.default is None and not self.nullable:
            raise ParamError(f"{self.name}: the default is None but the parameter is "
                             "not nullable; pass nullable=True if None is a value, "
                             "or give a real default")
        try:
            coerced = self.coerce(self.default)
        except ParamError as exc:
            reason = str(exc).removeprefix(f"{self.name}: ")
            raise ParamError(f"{self.name}: the default {self.default!r} would be "
                             f"refused by this spec: {reason}") from None
        object.__setattr__(self, "default", coerced)

    # -- coercion ---------------------------------------------------------
    def coerce(self, value: Any) -> Any:
        """Converts `value` to the parameter's wire type or raises `ParamError`.

        Numbers can arrive as strings, flags as `"true"` or `"false"`, and lists
        as comma-separated text. Coercion rejects booleans as counts, fractional
        integers such as `"20.5"`, non-finite floats and unlisted enum choices.
        It also avoids treating `bool("false")` as a valid true flag.

        NumPy scalars convert to their Python equivalents. HDF5 configuration and
        derived V_oc can supply `np.float64`, `np.int64` and `np.bool_`; rejecting
        these would exclude valid stored or measured values.
        """
        value = _python_scalar(value)
        if value is None or (isinstance(value, str) and value.strip() == ""
                             and self.type not in ("str", "list[float]", "list[str]")):
            if self.nullable:
                return None
            raise ParamError(f"{self.name}: no value given, and this parameter "
                             "cannot be empty")
        convert = getattr(self, "_as_" + self.type.replace("[", "_").rstrip("]"))
        try:
            out = convert(value)
        except ParamError:
            raise
        except (TypeError, ValueError) as exc:
            raise ParamError(f"{self.name}: {value!r} is not a valid {self.type}") from exc
        self._check_bounds(out)
        return out

    def _as_float(self, v: Any) -> float:
        if isinstance(v, bool):
            raise ParamError(f"{self.name}: {v!r} is a flag, not a number")
        if isinstance(v, numbers.Real):
            out = float(v)
        elif isinstance(v, str):
            out = float(v.strip())
        else:
            raise ParamError(f"{self.name}: {v!r} is not a number")
        if not math.isfinite(out):
            # JSON cannot carry NaN or infinity. Rejecting non-finite input also
            # prevents a NaN settle time from skipping the wait without an error.
            raise ParamError(f"{self.name}: {v!r} is not a finite number")
        return out

    def _as_int(self, v: Any) -> int:
        if isinstance(v, bool):
            raise ParamError(f"{self.name}: {v!r} is a flag, not a count")
        if isinstance(v, numbers.Integral):
            return int(v)
        if isinstance(v, numbers.Real):
            if not float(v).is_integer():
                raise ParamError(f"{self.name}: {v!r} is not a whole number")
            return int(v)
        if isinstance(v, str):
            s = v.strip()
            try:
                return int(s)
            except ValueError:
                pass
            f = float(s)                       # `"20.0"` from a numeric form field
            if not math.isfinite(f) or not f.is_integer():
                raise ParamError(f"{self.name}: {v!r} is not a whole number")
            return int(f)
        raise ParamError(f"{self.name}: {v!r} is not a whole number")

    _TRUE = ("true", "1", "yes", "on")
    _FALSE = ("false", "0", "no", "off")

    def _as_bool(self, v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, numbers.Integral) and v in (0, 1):
            # Forms and HDF5 attributes can encode flags as integer 0 or 1.
            # Floats indicate a caller type error. Accepting `1.0` while rejecting
            # `"1.0"` would make numeric and textual inputs disagree.
            return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in self._TRUE:
                return True
            if s in self._FALSE:
                return False
        raise ParamError(f"{self.name}: {v!r} is not a flag; use true or false")

    def _as_str(self, v: Any) -> str:
        if not isinstance(v, str):
            # A number in a text field indicates a caller type error.
            # `str(v)` would hide it by converting 1 to `"1"`.
            raise ParamError(f"{self.name}: {v!r} is not text")
        return v

    def _as_enum(self, v: Any) -> Any:
        if v in self.choices:
            return v
        if isinstance(v, str):
            # Case-insensitive matching agrees with `RunConfig.output_polarity`.
            # Returning the canonical spelling prevents duplicate dropdown options.
            hits = [c for c in self.choices if isinstance(c, str) and c.lower() == v.lower()]
            if len(hits) == 1:
                return hits[0]
        raise ParamError(f"{self.name}: {v!r} is not one of "
                         f"{', '.join(repr(c) for c in self.choices)}")

    def _as_list_float(self, v: Any) -> tuple[float, ...]:
        return tuple(self._as_float(x) for x in self._items(v))

    def _as_list_str(self, v: Any) -> tuple[str, ...]:
        return tuple(self._as_str(x) for x in self._items(v))

    def _items(self, v: Any) -> list:
        """Parses comma-separated text or a non-string iterable into a list.

        The list coercers convert its result to tuples. This keeps a frozen
        dataclass built from `ParamSet.values` hashable and prevents in-place
        growth of its parameter sequences.
        """
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        if isinstance(v, (bytes, Mapping)) or not isinstance(v, collections.abc.Iterable):
            raise ParamError(f"{self.name}: {v!r} is not a list")
        return [_python_scalar(x) for x in v]     # `ndarray` yields NumPy scalars

    def _check_bounds(self, out: Any) -> None:
        if self.type not in _BOUNDED or out is None:
            return
        for x in (out if isinstance(out, tuple) else (out,)):
            if self.minimum is not None and x < self.minimum:
                raise ParamError(f"{self.name}: {x!r} is below the minimum "
                                 f"{self.minimum!r}{_unit(self.unit)}")
            if self.maximum is not None and x > self.maximum:
                raise ParamError(f"{self.name}: {x!r} is above the maximum "
                                 f"{self.maximum!r}{_unit(self.unit)}")


_BOUNDED: tuple[str, ...] = ("float", "int", "list[float]")
"""Wire types that accept `minimum` and `maximum`.

`ParamSpec` rejects bounds on other types so constraints cannot be silently
ignored, as with choices on a non-enum type.
"""


def _unit(u: str) -> str:
    return f" {u}" if u else ""


def _python_scalar(v: Any) -> Any:
    """Converts a zero-dimensional NumPy value to its Python scalar.

    Other inputs pass through unchanged. Module-name detection avoids a NumPy
    dependency. `np.bool_` and `np.str_` satisfy neither `numbers.Integral`
    nor `numbers.Real`, so numeric checks alone reject valid HDF5 read-back.
    NumPy scalars provide `item` for this conversion.
    """
    if type(v).__module__ == "numpy" and getattr(v, "ndim", None) == 0:
        return v.item()
    return v


# -- one resolved value ---------------------------------------------------
@dataclass(frozen=True)
class ParamValue:
    """A resolved parameter value with its source and display detail.

    `detail` identifies a recipe table, run or pipeline node. It appears beside
    the source tag as free text and is never parsed.
    """

    value: Any
    source: Source
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"value": _jsonable(self.value), "source": self.source.value,
                "detail": self.detail}


def _jsonable(v: Any) -> Any:
    """Converts tuples to JSON lists and enums to their values.

    Other values already satisfy JSON because `ParamSpec.coerce` restricts
    what enters a layer.
    """
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, (tuple, list)):
        return [_jsonable(x) for x in v]
    return v


# -- the set --------------------------------------------------------------
class ParamSet:
    """An ordered specification set with one layer per non-default `Source`.

    The service owns one instance per module. This in-memory state is not
    thread-safe; the single service worker is its only writer.

    Every incoming value passes `ParamSpec.coerce`. A batch is coerced before
    any storage, so one invalid value leaves the whole set unchanged.
    Defaults belong only to specs; a separate mutable default layer would
    permit contradictory values.
    """

    def __init__(self, specs: Iterable[ParamSpec]):
        self._specs: dict[str, ParamSpec] = {}
        for s in specs:
            if s.name in self._specs:
                raise ParamError(f"{s.name}: declared twice; the second spec would "
                                 "shadow the first with no warning")
            self._specs[s.name] = s
        self._layers: dict[Source, dict[str, tuple[Any, str]]] = {
            s: {} for s in PRECEDENCE if s is not Source.DEFAULT}

    # -- specs ------------------------------------------------------------
    @property
    def specs(self) -> tuple[ParamSpec, ...]:
        return tuple(self._specs.values())

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def spec(self, name: str) -> ParamSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise ParamError(f"{name}: no such parameter") from None

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __iter__(self):
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    # -- layers -----------------------------------------------------------
    def set_layer(self, source: Source, values: Mapping[str, Any], detail: str = "") -> None:
        """Replaces a layer after validating the entire batch.

        `detail` applies to every value. Separate calls to `update_layer` let
        different recipe tables retain their own source descriptions.
        """
        coerced = self._coerce_all(source, values)
        self._layers[source] = {n: (v, detail) for n, v in coerced.items()}

    def update_layer(self, source: Source, values: Mapping[str, Any], detail: str = "") -> None:
        """Merges validated values into one layer, retaining its other values."""
        coerced = self._coerce_all(source, values)
        self._layers[source].update({n: (v, detail) for n, v in coerced.items()})

    def clear_layer(self, source: Source) -> None:
        self._check_source(source)
        self._layers[source] = {}

    def layer(self, source: Source) -> dict[str, tuple[Any, str]]:
        """Returns a layer copy as `{name: (value, detail)}` for the journal or tests.

        Changing the returned mapping does not change the layer.
        """
        self._check_source(source)
        return dict(self._layers[source])

    def set_edited(self, name: str, value: Any, detail: str = "") -> None:
        """Validates and store one field from `PUT /modules/{m}/params`."""
        self.update_layer(Source.EDITED, {name: value}, detail)

    def reset(self, name: str) -> None:
        """Removes an operator edit so the next lower layer supplies the value.

        `last-used` and `run.toml` retain the run and recipe settings; a form reset
        does not discard them. Inherited loop bindings also remain unaffected.
        """
        self.spec(name)
        self._layers[Source.EDITED].pop(name, None)

    def _check_source(self, source: Source) -> None:
        if not isinstance(source, Source):
            raise ParamError(f"{source!r} is not a Source")
        if source is Source.DEFAULT:
            raise ParamError("the default layer is the specs' own and cannot be set, "
                             "cleared or read as a layer; change the spec instead")

    def _coerce_all(self, source: Source, values: Mapping[str, Any]) -> dict[str, Any]:
        self._check_source(source)
        if not isinstance(values, Mapping):
            # A list or bare string is invalid API input and needs HTTP 422,
            # rather than HTTP 500 from calling `items` on the wrong type.
            raise ParamError(f"{source.value}: expected a mapping of name -> value, "
                             f"got {type(values).__name__}")
        out: dict[str, Any] = {}
        for name, value in values.items():
            spec = self.spec(name)
            if source is Source.EDITED:
                if not spec.editable:
                    raise ParamError(f"{name}: not editable; the spec marks it read-only")
                self._check_not_locked(name)
            out[name] = spec.coerce(value)
        return out

    def _check_not_locked(self, name: str) -> None:
        """Rejects edits to values controlled by inherited or derived layers.

        The wire marks these values `editable: false`. Accepting an edit would
        store an invisible value that takes effect when the binding clears.
        Edits made before the binding remain underneath and return when the layer
        clears.
        """
        for source in reversed(PRECEDENCE):
            if source in LOCKED and name in self._layers[source]:
                _, detail = self._layers[source][name]
                where = f" ({detail})" if detail else ""
                raise ParamError(f"{name}: not editable while {source.value}{where}")

    # -- resolution -------------------------------------------------------
    def get(self, name: str) -> ParamValue:
        return self._resolve_one(self.spec(name))

    def resolve(self) -> dict[str, ParamValue]:
        """Returns each winning value and source in specification order."""
        return {n: self._resolve_one(s) for n, s in self._specs.items()}

    def values(self) -> dict[str, Any]:
        """Returns `{name: value}` for constructing configuration dataclasses."""
        return {n: pv.value for n, pv in self.resolve().items()}

    def _resolve_one(self, spec: ParamSpec) -> ParamValue:
        for source in reversed(PRECEDENCE):
            if source is Source.DEFAULT:
                return ParamValue(spec.default, Source.DEFAULT, "")
            hit = self._layers[source].get(spec.name)
            if hit is not None:
                value, detail = hit
                return ParamValue(value, source, detail)
        raise AssertionError("unreachable: PRECEDENCE ends in DEFAULT")

    # -- the wire ---------------------------------------------------------
    def as_wire(self) -> list[dict[str, Any]]:
        """Returns JSON-ready parameter records for `GET /modules`, in spec order.

        `editable` requires both an editable spec and an unlocked source.
        `nullable`, `minimum` and `maximum` let the form validate empty fields
        and bounds before submitting. Clients that consume only the base contract
        keys can ignore these additional fields.
        """
        out = []
        for spec, pv in zip(self._specs.values(), self.resolve().values()):
            out.append({
                "name": spec.name,
                "label": spec.label,
                "value": _jsonable(pv.value),
                "source": pv.source.value,
                "detail": pv.detail,
                "editable": bool(spec.editable and pv.source not in LOCKED),
                "type": spec.type,
                "unit": spec.unit,
                "default": _jsonable(spec.default),
                "doc": spec.doc,
                "doc_full": spec.doc_full,
                "choices": list(spec.choices),
                "group": spec.group,
                "nullable": spec.nullable,
                "minimum": spec.minimum,
                "maximum": spec.maximum,
            })
        return out


# -- specs from a dataclass -----------------------------------------------
def specs_from_dataclass(cls: type, *, group: str = "", exclude: Iterable[str] = (),
                         rename: Mapping[str, str] | None = None,
                         units: Mapping[str, str] | None = None,
                         choices: Mapping[str, Iterable] | None = None,
                         editable: Mapping[str, bool] | None = None,
                         types: Mapping[str, str] | None = None,
                         defaults: Mapping[str, Any] | None = None) -> list[ParamSpec]:
    """Builds one `ParamSpec` per constructor field of `cls`, in field order.

    `typing.get_type_hints` resolves annotations, including nullable types,
    variadic tuples and `Literal` choices. If resolution fails, string parsing
    is tried, then the default's type. An unknown type is rejected rather than
    guessed, because the wire type controls which values the form accepts.
    Recognised unsupported types do not fall back: `tuple[float, float]` is a
    pair, and `list[float]` would incorrectly accept three values.

    A required field needs an entry in `defaults` or is rejected. Inventing
    `None` would make a required value such as `Axis.start` appear optional.
    Fields with `init=False` are excluded because `ParamSet.values` supplies
    constructor arguments.

    Override keys use dataclass field names before `rename` maps them to wire
    names. `units`, `choices`, `editable`, `types` and `defaults` supply field
    metadata; `exclude` omits fields. Unknown keys raise an error: a misspelt
    unit key must not silently leave a parameter without its unit.

    Units are explicit. A suffix cannot reliably imply them:
    `intensity_factor_mw_cm2_per_w` ends in `_w` but is not in W.
    `choices` can turn a string field into an enum when validation lives in
    the dataclass rather than its annotation, as for `RunConfig.dark_reference`.
    `field_docs` supplies the documentation.
    """
    if not dataclasses.is_dataclass(cls):
        raise ParamError(f"{cls!r} is not a dataclass")
    rename, units = dict(rename or {}), dict(units or {})
    choices, editable, types_ = dict(choices or {}), dict(editable or {}), dict(types or {})
    defaults_ = dict(defaults or {})
    exclude = tuple(exclude)

    fields = [f for f in dataclasses.fields(cls) if f.init]
    field_names = {f.name for f in fields}
    for label, keys in (("exclude", exclude), ("rename", rename), ("units", units),
                        ("choices", choices), ("editable", editable), ("types", types_),
                        ("defaults", defaults_)):
        unknown = sorted(set(keys) - field_names)
        if unknown:
            raise ParamError(f"{cls.__name__}: {label} names no such field(s): "
                             f"{', '.join(unknown)}. Refusing rather than silently "
                             "ignoring them")

    docs = field_docs(cls)
    docs_full = field_docs(cls, full=True)
    try:
        hints = get_type_hints(cls)
    except Exception:              # a forward reference the module cannot resolve
        hints = {}

    out: list[ParamSpec] = []
    for f in fields:
        if f.name in exclude:
            continue
        default = defaults_[f.name] if f.name in defaults_ else _default_of(f)
        if default is dataclasses.MISSING:
            raise ParamError(
                f"{cls.__name__}.{f.name}: has no default, and the console cannot show "
                f"a value that does not exist; pass defaults={{{f.name!r}: ...}} or "
                "exclude it")
        annotation = hints.get(f.name, f.type)
        if f.name in types_:
            kind, opts, nullable = types_[f.name], (), False
        else:
            try:
                kind, opts, nullable = _kind_of(annotation)
            except _Refused as exc:
                raise ParamError(
                    f"{cls.__name__}.{f.name}: cannot infer a wire type: {exc}; "
                    f"pass types={{{f.name!r}: ...}} or exclude it") from None
            except _Unknown:
                try:
                    kind, opts, nullable = _kind_of_value(default)
                except _Unknown:
                    raise ParamError(
                        f"{cls.__name__}.{f.name}: cannot infer a wire type from "
                        f"{annotation!r}; pass types={{{f.name!r}: ...}} or exclude "
                        "it") from None
        if f.name in choices:
            kind, opts = "enum", tuple(choices[f.name])
        if default is None:
            nullable = True
        out.append(ParamSpec(name=rename.get(f.name, f.name), type=kind, default=default,
                             unit=units.get(f.name, ""), doc=docs.get(f.name, ""),
                             doc_full=docs_full.get(f.name, ""),
                             choices=opts, group=group,
                             editable=editable.get(f.name, True), nullable=nullable))
    return out


class _Unknown(Exception):
    """An unrecognised annotation or value that permits the next type fallback."""


class _Refused(_Unknown):
    """A recognised annotation that cannot be represented by a wire type.

    Fixed pairs and integer tuples must not fall back to the default's type.
    For example, `(0.0, 1.0)` could otherwise turn a fixed pair into a list.
    The exception message gives the caller the reason.
    """


_SIMPLE = {float: "float", int: "int", bool: "bool", str: "str"}
_SEQUENCES = (tuple, list, collections.abc.Sequence)


def _default_of(f: dataclasses.Field) -> Any:
    """Returns a field default, or `dataclasses.MISSING` if none exists.

    `None` is a valid nullable value. Using it for a missing default would
    make a required field appear optional on the wire.
    """
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:   # type: ignore[misc]
        return f.default_factory()                     # type: ignore[misc]
    return dataclasses.MISSING


def _kind_of(ann: Any) -> tuple[str, tuple, bool]:
    """Returns `(wire_type, enum_choices, nullable)` from a resolved annotation."""
    if isinstance(ann, str):
        return _kind_of_string(ann)
    origin = get_origin(ann)
    if origin is Union or origin is UnionType:
        args = get_args(ann)
        rest = [a for a in args if a is not type(None)]
        if len(rest) != 1:
            raise _Unknown(ann)
        kind, opts, _ = _kind_of(rest[0])
        return kind, opts, len(rest) < len(args)
    if origin is Literal:
        return "enum", tuple(get_args(ann)), False
    if origin in _SEQUENCES:
        args = get_args(ann)
        # Fixed tuples require a fixed count. Mapping a pair or singleton
        # to an unbounded list would let the form submit too many values.
        # `list[float]` and `Sequence[float]` have one element-type argument.
        if origin is tuple:
            if len(args) != 2 or args[1] is not Ellipsis:
                raise _Refused(f"{ann!r} is a fixed-length tuple, not a list")
        elif len(args) != 1:
            raise _Refused(f"{ann!r} is not a sequence of one element type")
        return _list_kind(args[0], ann), (), False
    if ann in _SIMPLE:
        return _SIMPLE[ann], (), False
    raise _Unknown(ann)


def _list_kind(elem: Any, ann: Any) -> str:
    if elem is float:
        return "list[float]"
    if elem is str:
        return "list[str]"
    # Mapping `tuple[int, ...]` to `list[float]` would return 1.0 where
    # the dataclass requires an integer 1.
    raise _Refused(f"{ann!r} is a sequence of {elem!r}, and only float and str "
                   "elements have a wire type")


_TUPLE_STRING = re.compile(r"^tuple\[\s*(\w+)\s*,\s*\.\.\.\s*\]$")
_LIST_STRING = re.compile(r"^(?:list|Sequence|typing\.Sequence|collections\.abc\.Sequence)"
                          r"\[\s*(\w+)\s*\]$")
"""Unresolved sequence annotations follow the `_kind_of` length rules.

Only a variadic tuple such as `tuple[float, ...]` maps to a list; a fixed
pair is rejected.
"""


def _kind_of_string(s: str) -> tuple[str, tuple, bool]:
    """Parses a wire type when `get_type_hints` cannot resolve module names."""
    parts = [p.strip() for p in s.strip().split("|")]
    nullable = "None" in parts
    parts = [p for p in parts if p != "None"]
    if len(parts) != 1:
        raise _Unknown(s)
    p = parts[0]
    if p.startswith("Optional[") and p.endswith("]"):
        kind, opts, _ = _kind_of_string(p[len("Optional["):-1])
        return kind, opts, True
    if p in ("float", "int", "bool", "str"):
        return p, (), nullable
    if p.startswith("Literal[") and p.endswith("]"):
        opts = ast.literal_eval("(" + p[len("Literal["):-1] + ",)")
        return "enum", tuple(opts), nullable
    m = _TUPLE_STRING.match(p) or _LIST_STRING.match(p)
    if m:
        elem = {"float": float, "int": int, "str": str}.get(m.group(1))
        return _list_kind(elem, s), (), nullable
    if _SEQUENCE_HEAD.match(p):
        raise _Refused(f"{s!r} is a sequence, but not `tuple[float, ...]`, "
                       "`list[float]` or their str forms")
    raise _Unknown(s)


_SEQUENCE_HEAD = re.compile(r"^(?:tuple|list|Sequence|typing\.Sequence|collections\.abc\.Sequence)\[")


def _kind_of_value(v: Any) -> tuple[str, tuple, bool]:
    """Infers a wire type from the default as the final fallback.

    Booleans are checked before integers: Python treats `bool` as an `int`,
    but a flag must not become a count.
    """
    if isinstance(v, bool):
        return "bool", (), False
    if isinstance(v, int):
        return "int", (), False
    if isinstance(v, float):
        return "float", (), False
    if isinstance(v, str):
        return "str", (), False
    if isinstance(v, (tuple, list)) and v:
        if all(isinstance(x, float) for x in v):
            return "list[float]", (), False
        if all(isinstance(x, str) for x in v):
            return "list[str]", (), False
    raise _Unknown(v)


# -- docs from the source -------------------------------------------------
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`*\"'(])")
"""Sentence starts can be capitals, digits or opening markup characters.

A lowercase word after an abbreviation continues the sentence. A decimal
point in `0.2` has no following whitespace, so it cannot split a sentence.
"""
_NOT_AN_END = ("e.g.", "i.e.", "vs.", "cf.")


def field_docs(cls: type, *, full: bool = False) -> dict[str, str]:
    """Returns field documentation from bare strings after annotated fields.

    For example:

        n_averages: int = 200
        \"\"\"Hardware averages per trace. Noise falls as 1/sqrt(n).\"\"\"

    Python does not attach these strings to fields, so `inspect.getsource`
    and `ast` read them from source. The default result contains the first
    sentence of the first paragraph for tooltips. `full=True` returns the
    cleaned full text. Missing documentation or unreadable source, including
    builtins and interactive classes, gives `{}`. The function never raises;
    a missing tooltip must not prevent listing a module.
    """
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
    except Exception:
        return {}
    node = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.ClassDef) and n.name == cls.__name__), None)
    if node is None:
        return {}
    docs: dict[str, str] = {}
    body = node.body
    for stmt, nxt in zip(body, body[1:]):
        if (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                and isinstance(nxt, ast.Expr) and isinstance(nxt.value, ast.Constant)
                and isinstance(nxt.value.value, str)):
            text = inspect.cleandoc(nxt.value.value)
            docs[stmt.target.id] = text if full else first_sentence(text)
    return docs


def first_sentence(text: str) -> str:
    """Returns the first paragraph's first sentence with whitespace collapsed.

    The heuristic recognises `.`, `!` or `?` followed by whitespace and a
    capital, digit, backtick, quote or bracket. Abbreviations in `_NOT_AN_END`
    do not end sentences. A period inside `1/sqrt(n)` or `0.2` has no following
    whitespace and does not split the text.
    """
    paragraph = re.split(r"\n\s*\n", text.strip(), maxsplit=1)[0]
    flat = " ".join(paragraph.split())
    for m in _SENTENCE_END.finditer(flat):
        head = flat[:m.start()]
        if not head.endswith(_NOT_AN_END):
            return head
    return flat


# -- TOML ------------------------------------------------------------------
def toml_layer(table: Mapping[str, Any], mapping: Mapping[str, str]) -> dict[str, Any]:
    """Selects values from a parsed TOML mapping by dotted path.

    `mapping` maps file paths such as `acquisition.n_averages` to wire names
    such as `n_averages`. Missing paths are skipped so lower layers apply.
    This function does not validate: `config.load_run` rejects unknown keys
    before the service attributes parameter values to the recipe.
    """
    out: dict[str, Any] = {}
    for path, name in mapping.items():
        node: Any = table
        for key in path.split("."):
            if not isinstance(node, Mapping) or key not in node:
                break
            node = node[key]
        else:
            out[name] = node
    return out


def run_toml_layer(path: str | Path) -> dict[str, Any]:
    """Reads a recipe's nested table mapping for `toml_layer`.

    The raw tables preserve source details such as `run.toml [acquisition]`.
    Dataclasses from `config.load_run` retain values without table names.
    The caller supplies the file-to-wire mapping because the catalogue defines
    which recipe path supplies each parameter.
    """
    p = Path(path)
    if not p.exists():
        raise ParamError(f"no such run file: {p}")
    with open(p, "rb") as fh:
        return tomllib.load(fh)
