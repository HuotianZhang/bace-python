"""Where a parameter's value came from.

The console shows every parameter as `{value, source, editable}`, and the
service's `/modules` endpoint is what fills that in. This module is the generic
machinery underneath: a `ParamSet` holds one layer per `Source`, resolves them
by a fixed precedence, and reports the winner *with its provenance*. The
per-module catalogue -- which dataclass fields make up the `bace` module, what
they are called on the wire -- is service-layer work built on top of this and
lives elsewhere.

**Why provenance at all.** A run is described by ten to forty numbers, and the
same number can arrive by four different roads: the dataclass default, the
recipe file, the value the last run used, or something the operator typed a
minute ago. When those disagree the console has to say which one is in force,
because `n_averages = 20` with no source cannot tell the operator whether they
are about to repeat yesterday's run or the recipe's. The pipeline adds a fifth
road: a loop that injects `led_v` into every child, or a J-V run that produces
the V_oc the next `bace` run centres on. Those must show as what they are. The
failure this prevents is concrete: the operator types V_oc = 0.90, the
illumination loop then runs a J-V that measures 0.86, the transient centres on
0.86, and the console still says "0.90, edited". Every charge in that run is
then read against the wrong axis by anyone who trusts the screen.

**Precedence**, lowest to highest:

    default < run.toml < last-used < edited < inherited < derived

- `default` is the dataclass field default. Always present, never a layer.
- `run.toml` is the recipe file -- the thing that is versioned and reviewed.
- `last-used` is what the previous run of this module actually ran with, so a
  session picks up where it left off. It sits *above* run.toml because a
  recipe edited between two runs of one session would otherwise silently undo
  values the operator had just settled on, and *below* edited because a fresh
  edit is the most recent statement of intent.
- `edited` is what the operator typed in this session.
- `inherited` comes from the pipeline context (a loop's `led_v`). It beats
  edited because the loop is the thing being run: an edited value that a loop
  overrides surfaces as inherited, never silently as edited.
- `derived` comes from a measurement (V_oc from a J-V curve). It beats
  inherited because a measurement taken inside the loop is more recent than the
  loop's own binding. A parameter that is both inherited and derived is a
  pipeline-authoring mistake this module does not police; it reports derived.

Inherited and derived values are not editable on the wire, and `set_edited`
refuses them too. A value the pipeline will overwrite is not the operator's to
type, and a form that accepted it would be lying about what the next run does.
Refusing at the API as well as on the wire matters because a PUT that
succeeded against a locked value would store an edit the screen never showed
-- and the first manual run after the loop let go would use it.

**Values are coerced on the way into a layer**, never on the way out, so a bad
value is refused at the API boundary with the parameter's name attached and
nothing is half-applied. The reason is JSON: a form sends `"20"` for a count,
`"false"` for a flag and `"1.02, 1.06"` for a list, and `bool("false")` is
`True`.

Nothing here imports from `core/`, `drivers/`, `experiment/` or `storage/`.
Binding these specs to the real dataclasses is the layer above, which is why
`specs_from_dataclass` takes any dataclass and asks nothing of it beyond
`dataclasses.fields`.
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
    """A value that cannot enter a layer, a name no spec knows, or a spec that
    contradicts itself. Every message starts with the parameter's name so the
    console can attach it to the right field -- or, when no single parameter
    is at fault (a batch that is not a mapping), with the layer's source."""


class Source(str, Enum):
    """Which road a value arrived by. See the module docstring for precedence."""

    DEFAULT = "default"
    RUN_TOML = "run.toml"
    LAST_USED = "last-used"
    EDITED = "edited"
    INHERITED = "inherited"
    DERIVED = "derived"


PRECEDENCE: tuple[Source, ...] = (Source.DEFAULT, Source.RUN_TOML, Source.LAST_USED,
                                  Source.EDITED, Source.INHERITED, Source.DERIVED)
"""Lowest to highest. `ParamSet.resolve` walks it from the top and stops at the
first layer that holds the name."""

LOCKED: frozenset[Source] = frozenset({Source.INHERITED, Source.DERIVED})
"""Sources that make a value non-editable on the wire, whatever the spec says."""

TYPES: tuple[str, ...] = ("float", "int", "bool", "str", "enum", "list[float]", "list[str]")
"""The wire types. What JSON carries and what `ParamSpec.coerce` converts to --
not Python annotations: `tuple[float, ...]` on a dataclass is `list[float]` here."""


# -- one parameter --------------------------------------------------------
@dataclass(frozen=True)
class ParamSpec:
    """One parameter as the console sees it: type, default, and how to show it.

    `nullable` lets `None` (JSON `null`, or an empty field) through `coerce`.
    It is inferred from a `float | None` annotation and from a `None` default.
    Without it a nullable float such as `SeriesConfig.dc_settle_s`, where None
    means "use the driver's own settle times", could never be set back to None
    once it had held a number.

    `editable` is the spec's own say: False for a value the console shows but
    must never take from a form (a bench ceiling, for instance). The wire also
    marks inherited and derived values non-editable, but that is per value and
    decided by `ParamSet`, not here.

    `minimum`/`maximum` are inclusive and apply to `float`, `int` and every
    element of a `list[float]`; on any other type they are refused.

    The default is put through `coerce` when the spec is built and stored in
    its coerced form (a list becomes a tuple, an enum takes the spelling of
    its choice), so a spec cannot advertise a default its own form would
    refuse.
    """

    name: str
    type: str
    default: Any = None
    unit: str = ""
    doc: str = ""
    """One sentence: **which instrument, and what this does to it.** It is what
    the console prints under the field, and for most of these fifty parameters
    it is the only thing standing between the operator and a name that means
    nothing on its own (`smu_nplc`, `trigger_sweep`, `duty_percent`) or, worse,
    something else than it looks (`inverted_output` is the 81150A's output
    polarity; `invert_polarity` is arithmetic on the computed levels)."""

    doc_full: str = ""
    """The whole explanation, for the field's expandable help: what it does,
    what it costs, and what a wrong value produces. Empty means `doc` is the
    whole of it. Kept apart rather than folded into `doc` because the card
    shows one line per field and `ui-rules` §1 wants the density -- the rest
    is a click away, not gone."""

    choices: tuple = ()
    group: str = ""
    editable: bool = True
    minimum: float | None = None
    maximum: float | None = None
    nullable: bool = False

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
        # The default is the one value that reaches `values()` without ever
        # entering a layer, so it is the one road into the dataclass that
        # `coerce` does not guard. Guard it here: a catalogue that lists
        # choices=("trigger", "pulse") for a field whose default is "record"
        # would otherwise put a value in the dropdown that the dropdown cannot
        # select, and `reset` would restore a value the form refuses. Storing
        # the coerced default also canonicalises it in one place: a list
        # becomes a tuple and an enum takes the spelling of its choice.
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
        """Convert `value` to this parameter's wire type, or raise `ParamError`.

        Accepts what JSON and HTML forms actually send: numbers as strings,
        booleans as `"true"`/`"false"`, lists as a comma-separated string.
        Rejects what would otherwise pass by accident: `bool("false")`, a bool
        where a count is wanted, `"20.5"` for an int, NaN for a float, an enum
        value that is not one of the choices.

        Numpy scalars are accepted as the Python number they wrap. A last-used
        layer seeded from a run file's `/config` group, or a derived V_oc from
        measurement code, arrives as `np.float64`/`np.int64`/`np.bool_`, and
        refusing those by name with "is not a number" would be wrong twice.
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
            # JSON cannot carry NaN or inf, so one arriving here was typed on
            # purpose or produced by a broken caller. Either way a run whose
            # settle time is NaN sleeps for no time and looks fine.
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
            f = float(s)                       # "20.0" from a numeric form field
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
            # 0 and 1 are how TOML-less sources (a form checkbox, an HDF5
            # attribute) spell a flag. Floats are not: 1.0 as a flag is a
            # wiring mistake in the caller, and the string "1.0" is refused
            # below, so accepting the number would make the two disagree.
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
            # Not `str(v)`: a number that lands in a text field is a wiring
            # mistake in the caller, and turning 1 into "1" would hide it.
            raise ParamError(f"{self.name}: {v!r} is not text")
        return v

    def _as_enum(self, v: Any) -> Any:
        if v in self.choices:
            return v
        if isinstance(v, str):
            # `RunConfig.output_polarity` accepts "norm" and "NORM" alike, so
            # the console should too -- but it hands back the canonical
            # spelling, so one option never appears twice in a dropdown.
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
        """A comma-separated string or any non-string iterable. Tuples, not
        lists, leave here so a frozen dataclass built from `values()` stays
        hashable and nothing downstream can grow the list in place."""
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        if isinstance(v, (bytes, Mapping)) or not isinstance(v, collections.abc.Iterable):
            raise ParamError(f"{self.name}: {v!r} is not a list")
        return [_python_scalar(x) for x in v]     # an ndarray yields numpy scalars

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
"""The types `minimum`/`maximum` apply to. A bound on any other type is
refused by `ParamSpec`, for the same reason choices on a non-enum are."""


def _unit(u: str) -> str:
    return f" {u}" if u else ""


def _python_scalar(v: Any) -> Any:
    """A 0-d numpy scalar as the Python scalar it wraps; anything else as is.

    Duck-typed on the module name so this file stays free of numpy: `np.bool_`
    and `np.str_` register with neither `numbers.Integral` nor `numbers.Real`,
    so the `numbers` checks alone would still refuse a flag read back from an
    HDF5 attribute. `.item()` is what every numpy scalar offers for exactly
    this.
    """
    if type(v).__module__ == "numpy" and getattr(v, "ndim", None) == 0:
        return v.item()
    return v


# -- one resolved value ---------------------------------------------------
@dataclass(frozen=True)
class ParamValue:
    """A value together with where it came from.

    `detail` is the human-readable part of the provenance: which table of the
    recipe (`"run.toml [acquisition]"`), which run (`"run 20260902_210300-003"`),
    which pipeline node (`"illumination loop"`). Free text, shown next to the
    source tag; never parsed.
    """

    value: Any
    source: Source
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"value": _jsonable(self.value), "source": self.source.value,
                "detail": self.detail}


def _jsonable(v: Any) -> Any:
    """Tuples become lists and enums their values; everything else is already
    what `json.dumps` wants, because `coerce` let nothing else in."""
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, (tuple, list)):
        return [_jsonable(x) for x in v]
    return v


# -- the set --------------------------------------------------------------
class ParamSet:
    """An ordered set of specs and one value layer per non-default `Source`.

    Plain in-memory state, owned by whoever owns the module (the service keeps
    one per module). Not thread-safe; the service's single worker is the only
    writer.

    Every value entering a layer goes through `ParamSpec.coerce`, and a batch
    is coerced in full before any of it is stored, so a bad value in a form
    submission leaves the set exactly as it was. The default layer is not
    settable: the default *is* the spec's, and two places to hold it would be
    two places for it to disagree.
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
        """Replace one layer wholesale. `detail` is shared by every value in it;
        call `update_layer` per table when values come from several places."""
        coerced = self._coerce_all(source, values)
        self._layers[source] = {n: (v, detail) for n, v in coerced.items()}

    def update_layer(self, source: Source, values: Mapping[str, Any], detail: str = "") -> None:
        """Merge into one layer, keeping what is already there."""
        coerced = self._coerce_all(source, values)
        self._layers[source].update({n: (v, detail) for n, v in coerced.items()})

    def clear_layer(self, source: Source) -> None:
        self._check_source(source)
        self._layers[source] = {}

    def layer(self, source: Source) -> dict[str, tuple[Any, str]]:
        """A copy of one layer as `{name: (value, detail)}`, for the journal
        and for tests. Mutating the copy changes nothing."""
        self._check_source(source)
        return dict(self._layers[source])

    def set_edited(self, name: str, value: Any, detail: str = "") -> None:
        """What `PUT /modules/{m}/params` does for one field."""
        self.update_layer(Source.EDITED, {name: value}, detail)

    def reset(self, name: str) -> None:
        """Drop the operator's edit so the next lower layer shows again.

        Only the edited layer: `last-used` and `run.toml` are facts about what
        ran and what the file says, not the operator's to discard from a form,
        and a loop's inherited value is not affected by anything typed.
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
            # A JSON body that is a list or a bare string must come back as
            # the same 422 a bad value does, not as a 500 from `.items()`.
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
        """Refuse an edit against a value that is inherited or derived right
        now. The wire says `editable: false` for it; an API that then took the
        edit anyway would hold a value the screen never confirmed and use it
        the moment the loop released the name. An edit made *before* the loop
        bound the name is untouched: it stays underneath and shows again when
        the layer is cleared."""
        for source in reversed(PRECEDENCE):
            if source in LOCKED and name in self._layers[source]:
                _, detail = self._layers[source][name]
                where = f" ({detail})" if detail else ""
                raise ParamError(f"{name}: not editable while {source.value}{where}")

    # -- resolution -------------------------------------------------------
    def get(self, name: str) -> ParamValue:
        return self._resolve_one(self.spec(name))

    def resolve(self) -> dict[str, ParamValue]:
        """Every parameter with its winning value and source, in spec order."""
        return {n: self._resolve_one(s) for n, s in self._specs.items()}

    def values(self) -> dict[str, Any]:
        """Plain `{name: value}`, for `SomeConfig(**subset)`."""
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
        """What `GET /modules` sends for one module, one dict per parameter in
        spec order. Everything in it survives `json.dumps` as is.

        `editable` here is the per-value answer: the spec's flag *and* the
        source not being inherited or derived. `nullable`, `minimum` and
        `maximum` are included beyond the eleven keys the service contract
        lists, so a form can validate before it submits and knows whether
        "empty" is allowed. They are additive -- a client written to the
        eleven ignores them -- and the contract should list them when the
        service round writes the catalogue.
        """
        out = []
        for spec, pv in zip(self._specs.values(), self.resolve().values()):
            out.append({
                "name": spec.name,
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
    """One `ParamSpec` per constructor field of `cls`, in field order.

    The wire type comes from the annotation (`float`, `int`, `bool`, `str`,
    `float | None`, `tuple[float, ...]`, `Literal[...]`), resolved through
    `typing.get_type_hints` because this repo writes
    `from __future__ import annotations` and every annotation is a string. If
    the hints cannot be resolved the string is parsed directly; if that fails
    too the default value's type is used; and if *that* fails the field is
    refused with a message, because a guessed type is a form that accepts the
    wrong thing. A fixed-length `tuple[float, float]` is one of the refusals:
    it is a pair, and `list[float]` would let a form submit three.

    A field with no default is refused as well, unless `defaults` supplies
    one. The console cannot show a value that does not exist, and inventing
    `None` for it would present `Axis.start` as optional and hand the
    dataclass `None` at run start. Fields with `init=False` are skipped: they
    are never constructor input, and `values()` is meant to be splatted into
    the constructor.

    Every override is keyed by the **dataclass field name**, before `rename`:
    `rename` maps field name to wire name; `units`, `choices`, `editable`,
    `types` and `defaults` decorate; `exclude` drops. A key that names no
    field is an error, for the same reason an unknown TOML key is:
    `units={"n_average": "..."}` would otherwise silently leave `n_averages`
    without a unit.

    Units are never inferred from the name. The `_s`/`_ns`/`_v` suffix
    convention is nearly consistent here, and "nearly" is the problem:
    `intensity_factor_mw_cm2_per_w` ends in `_w` and is not in watts.

    `choices` turns a `str` field into an enum -- `RunConfig` validates
    `t0_int_reference` in `__post_init__` rather than in its annotation, so
    the catalogue says the choices here. Docs come from `field_docs`.
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
    """Internal: the annotation or value did not map to a wire type, so the
    next fallback may try."""


class _Refused(_Unknown):
    """Internal: the annotation *was* understood and is deliberately not a
    wire type -- a fixed pair, a tuple of ints. No fallback may try: the
    default `(0.0, 1.0)` looks exactly like a float list, and guessing from
    it would undo the refusal. The message is the reason, for the caller."""


_SIMPLE = {float: "float", int: "int", bool: "bool", str: "str"}
_SEQUENCES = (tuple, list, collections.abc.Sequence)


def _default_of(f: dataclasses.Field) -> Any:
    """The field's default, or `dataclasses.MISSING` when it has none. Not
    `None`: None is a value a nullable field can hold, and conflating the two
    is how a required field would come to look optional on the wire."""
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:   # type: ignore[misc]
        return f.default_factory()                     # type: ignore[misc]
    return dataclasses.MISSING


def _kind_of(ann: Any) -> tuple[str, tuple, bool]:
    """(wire type, enum choices, nullable) from a resolved annotation."""
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
        # `tuple[float, ...]` is a list; `tuple[float, float]` is a pair and
        # `tuple[float]` a singleton, and neither may become an unbounded
        # list that lets a form submit three values for two. `list[float]`
        # and `Sequence[float]` carry exactly one argument by construction.
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
    # `tuple[int, ...]` is deliberately not `list[float]`: the values would
    # come back as floats and the dataclass would hold 1.0 where it expects 1.
    raise _Refused(f"{ann!r} is a sequence of {elem!r}, and only float and str "
                   "elements have a wire type")


_TUPLE_STRING = re.compile(r"^tuple\[\s*(\w+)\s*,\s*\.\.\.\s*\]$")
_LIST_STRING = re.compile(r"^(?:list|Sequence|typing\.Sequence|collections\.abc\.Sequence)"
                          r"\[\s*(\w+)\s*\]$")
"""The same rule as `_kind_of`, on the unresolved string: a tuple must be
variadic (`tuple[float, ...]`) to be a list; a fixed pair is refused."""


def _kind_of_string(s: str) -> tuple[str, tuple, bool]:
    """The fallback when `get_type_hints` cannot resolve the module's names."""
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
    """Last resort: the default's own type. `bool` before `int`, because a
    bool is an int in Python and a flag is not a count."""
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
"""A digit starts a sentence too ("Zero by default. 200 is the panel value.");
a lowercase letter does not, because after an abbreviation the sentence simply
continues ("approx. two divisions") and a period inside a number is never
followed by whitespace, so the digit case cannot misfire on `0.2`."""
_NOT_AN_END = ("e.g.", "i.e.", "vs.", "cf.")


def field_docs(cls: type, *, full: bool = False) -> dict[str, str]:
    """`{field_name: doc}` for fields documented the way this repo documents
    them: a bare string literal on the line after the field.

        n_averages: int = 200
        \"\"\"Hardware averages per trace. Noise falls as 1/sqrt(n).\"\"\"

    Python attaches no meaning to that string, so it is read back from the
    source with `inspect.getsource` and `ast`. By default the first sentence
    of the first paragraph is returned -- what fits a tooltip; `full=True`
    returns the whole string, cleaned. Classes with no such docs give `{}`,
    and so does anything whose source cannot be read (a builtin, a class made
    in a REPL). Never raises: a missing tooltip is not a reason to refuse to
    list a module.
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
    """The first sentence of the first paragraph, whitespace collapsed.

    A heuristic, and it says so: a sentence ends at `.`, `!` or `?` followed
    by whitespace and something that starts a sentence (a capital, a digit, a
    backtick, a quote, a bracket). `e.g.` and friends do not count, and
    neither does a period inside `1/sqrt(n)` or `0.2`, because no whitespace
    follows them.
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
    """Pick values out of a parsed TOML dict by dotted path.

    `mapping` is `{"acquisition.n_averages": "n_averages", ...}` -- path in
    the file to name on the wire. Paths that do not resolve are skipped, so a
    recipe that leaves a key out simply lets the lower layer show; nothing is
    invented. This is a picker, not a validator: the file has already been
    through `config.load_run`, which refuses unknown keys, by the time the
    service attributes values to it.
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
    """The raw nested dict of a recipe file, for `toml_layer` to pick from.

    Returns the tables as written rather than the dataclasses `config.load_run`
    builds from them, because provenance needs the *table*: the console says
    "run.toml [acquisition]", and by the time a value is a `RunConfig` field it
    no longer knows which table it came from. The mapping is the caller's --
    it is the catalogue that knows `acquisition.n_averages` is `n_averages`.
    """
    p = Path(path)
    if not p.exists():
        raise ParamError(f"no such run file: {p}")
    with open(p, "rb") as fh:
        return tomllib.load(fh)
