"""Layered, typed and immutable-at-use experiment parameters.

The parameter boundary is intentionally small:

* a process (or model) loads one overall JSON file;
* an experiment may load a second JSON file whose values override the overall
  values (objects are merged recursively, arrays/scalars replace wholesale);
* application code reads the frozen result with
  ``params.get("policy.timeout_ms", [int, 1000])``.

No component should read two JSON files on its own. Keeping the merge and type
handling here makes an experiment reproducible and gives every module the same
view of its configuration.
"""

from __future__ import annotations

import copy
import hashlib
import json
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import Field

from ..contracts.serialization import jsonable as _jsonable
from ..schemas import RoboModel

_MISSING = object()
_CONTAINER_ORIGINS = {dict, list, tuple, set}


class ParameterError(ValueError):
    """Base error for invalid parameter sources or access contracts."""


class ParameterTypeError(ParameterError):
    """Raised when a configured value does not match ``params.get`` type."""


class ParameterConflictError(ParameterError):
    """Raised when two base configuration layers define the same key."""


def _canonical_json(values: Mapping[str, Any]) -> str:
    return json.dumps(
        _jsonable(values),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge JSON objects; arrays and scalar values replace."""

    merged = copy.deepcopy(dict(base))
    for key, value in override.items():
        previous = merged.get(key, _MISSING)
        if isinstance(previous, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(previous, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def merge_parameter_values(
    base: Mapping[str, Any] | None,
    override: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a defensive recursive merge of two parameter objects.

    ``override`` wins at every leaf; a list is one leaf and is therefore
    replaced rather than concatenated.
    """

    left = base or {}
    right = override or {}
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        raise ParameterError("parameter layers must be JSON objects")
    return _deep_merge(left, right)


def _strict_merge(
    base: Mapping[str, Any],
    layer: Mapping[str, Any],
    *,
    source: str,
    path: str = "",
) -> dict[str, Any]:
    """Merge two base layers and reject duplicate leaf configuration keys.

    Objects may contribute different child keys, but the same leaf (or an
    object-versus-scalar replacement) is always an error. Experiment layers
    use :func:`merge_parameter_values` instead because overrides are explicit.
    """

    result = copy.deepcopy(dict(base))
    for key, value in layer.items():
        current_path = f"{path}.{key}" if path else str(key)
        if key not in result:
            result[key] = copy.deepcopy(value)
            continue
        previous = result[key]
        if isinstance(previous, Mapping) and isinstance(value, Mapping):
            result[key] = _strict_merge(
                previous,
                value,
                source=source,
                path=current_path,
            )
            continue
        raise ParameterConflictError(
            f"duplicate parameter/config key {current_path!r} while loading {source}"
        )
    return result


def _unwrap_parameters(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Accept either a bare JSON object or ``{"parameters": {...}}``."""

    nested = raw.get("parameters")
    return copy.deepcopy(dict(nested if isinstance(nested, Mapping) else raw))


def _read_json(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"parameter config not found: {config_path}")
    if config_path.suffix.lower() != ".json":
        raise ParameterError(f"parameter config must be JSON: {config_path}")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ParameterError(f"invalid parameter JSON {config_path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ParameterError(f"parameter config root must be an object: {config_path}")
    return dict(raw)


def _read_source(source: str | Path | Mapping[str, Any] | None) -> dict[str, Any]:
    if source is None:
        return {}
    raw = _read_json(source) if isinstance(source, (str, Path)) else source
    if not isinstance(raw, Mapping):
        raise ParameterError("parameter config root must be an object")
    return _unwrap_parameters(raw)


def _lookup(values: Mapping[str, Any], name: str) -> Any:
    """Resolve a direct key first, then a dotted object path."""

    if not isinstance(name, str) or not name.strip():
        raise ParameterError("parameter name must be a non-empty string")
    if name in values:
        return copy.deepcopy(values[name])
    current: Any = values
    for part in name.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return copy.deepcopy(current)


def _lookup_with_parameter_section(values: Mapping[str, Any], name: str) -> Any:
    value = _lookup(values, name)
    if value is not _MISSING:
        return value
    nested = values.get("parameters")
    if isinstance(nested, Mapping):
        return _lookup(nested, name)
    return _MISSING


def _parse_contract(contract: Any) -> tuple[Any, Any]:
    """Parse the required ``[type, default]`` access contract."""

    if not isinstance(contract, Sequence) or isinstance(contract, (str, bytes)):
        raise ParameterError(
            "params.get(name, contract) requires contract=[type, default]"
        )
    if len(contract) != 2:
        raise ParameterError(
            "params.get(name, contract) requires exactly [type, default]"
        )
    expected, default = contract[0], contract[1]
    if expected is not Any and expected is not None and not isinstance(expected, type):
        # ``list[str]``, ``Optional[int]`` and ``Literal[...]`` are typing
        # objects rather than classes; they are handled by _coerce_value.
        if get_origin(expected) is None:
            raise ParameterError("the first item in [type, default] must be a type")
    return expected, default


def _coerce_value(value: Any, expected: Any, path: str) -> Any:
    """Validate common JSON values against a Python type contract."""

    if expected is Any or expected is None:
        return copy.deepcopy(value)

    origin = get_origin(expected)
    args = get_args(expected)

    if origin in (Union, types.UnionType):
        errors: list[str] = []
        for candidate in args:
            if candidate is type(None) and value is None:
                return None
            try:
                return _coerce_value(value, candidate, path)
            except ParameterTypeError as exc:
                errors.append(str(exc))
        raise ParameterTypeError(
            f"parameter {path!r} has value {value!r}; expected one of {args}: "
            + "; ".join(errors)
        )

    if origin is Literal:
        if value in args and any(type(value) is type(item) for item in args):
            return copy.deepcopy(value)
        raise ParameterTypeError(
            f"parameter {path!r} must be one of {args}, got {value!r}"
        )

    if expected is bool:
        valid, result = type(value) is bool, value
    elif expected is int:
        valid, result = type(value) is int, value
    elif expected is float:
        valid = type(value) in (int, float) and not isinstance(value, bool)
        result = float(value) if valid else value
    elif expected is str:
        valid, result = type(value) is str, value
    elif expected is dict:
        valid, result = isinstance(value, Mapping), value
        if valid:
            result = dict(value)
    elif expected is list:
        valid, result = isinstance(value, list), value
        if valid:
            result = list(value)
    elif expected is tuple:
        valid, result = isinstance(value, (list, tuple)), value
        if valid:
            result = tuple(value)
    elif expected is set:
        valid, result = isinstance(value, (list, tuple, set)), value
        if valid:
            result = set(value)
    elif isinstance(expected, type) and issubclass(expected, Enum):
        try:
            result, valid = expected(value), True
        except (TypeError, ValueError):
            result, valid = value, False
    elif isinstance(expected, type):
        valid, result = isinstance(value, expected), value
    else:
        raise ParameterError(f"unsupported parameter type for {path!r}: {expected!r}")

    if not valid:
        raise ParameterTypeError(
            f"parameter {path!r} has value {value!r} ({type(value).__name__}); "
            f"expected {expected!r}"
        )

    if origin in _CONTAINER_ORIGINS and args:
        if origin is dict:
            key_type, value_type = args
            result = {
                _coerce_value(key, key_type, f"{path}.<key>"): _coerce_value(
                    item, value_type, f"{path}.{key}"
                )
                for key, item in dict(value).items()
            }
        elif origin is list:
            result = [
                _coerce_value(item, args[0], f"{path}[{index}]")
                for index, item in enumerate(value)
            ]
        elif origin is tuple:
            if len(args) == 2 and args[1] is Ellipsis:
                result = tuple(
                    _coerce_value(item, args[0], f"{path}[{index}]")
                    for index, item in enumerate(value)
                )
            elif len(args) == len(value):
                result = tuple(
                    _coerce_value(item, item_type, f"{path}[{index}]")
                    for index, (item, item_type) in enumerate(zip(value, args))
                )
            else:
                raise ParameterTypeError(
                    f"parameter {path!r} tuple length does not match {expected!r}"
                )
        elif origin is set:
            result = {
                _coerce_value(item, args[0], f"{path}[{index}]")
                for index, item in enumerate(value)
            }
    return copy.deepcopy(result)


def _get_typed(values: Mapping[str, Any], name: str, contract: Any) -> Any:
    expected, default = _parse_contract(contract)
    value = _lookup_with_parameter_section(values, name)
    if value is _MISSING:
        value = copy.deepcopy(default)
    # ``[T, None]`` is the explicit optional/default form.
    if value is None and default is None:
        return None
    return _coerce_value(value, expected, name)


class ParameterSnapshot(RoboModel):
    """Frozen-at-rest experiment parameters with a reproducible hash."""

    name: str = "default"
    values: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    parent_hash: str | None = None

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(_canonical_json(self.values).encode("utf-8")).hexdigest()

    def resolve(self, name: str) -> Any:
        value = _lookup_with_parameter_section(self.values, name)
        return None if value is _MISSING else value

    def get(self, name: str, contract: Any = None) -> Any:
        """Read a value using ``params.get(name, [type, default])``."""

        if contract is None:
            return self.resolve(name)
        return _get_typed(self.values, name, contract)

    def merged(
        self,
        overrides: Mapping[str, Any],
        *,
        name: str | None = None,
    ) -> ParameterSnapshot:
        return ParameterSnapshot(
            name=name or self.name,
            values=merge_parameter_values(self.values, overrides),
            parent_hash=self.content_hash,
        )

    @classmethod
    def from_files(
        cls,
        overall_config: str | Path | Mapping[str, Any],
        experiment_config: str | Path | Mapping[str, Any] | None = None,
        *,
        name: str = "default",
        base_dir: str | Path | None = None,
    ) -> ParameterSnapshot:
        return Params.from_files(
            overall_config, experiment_config, name=name, base_dir=base_dir
        ).snapshot


class Params(Mapping[str, Any]):
    """Read-only access facade over one merged parameter snapshot."""

    def __init__(
        self,
        values: Mapping[str, Any],
        *,
        name: str = "default",
        parent_hash: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        kwargs = {"created_at": created_at} if created_at is not None else {}
        self._snapshot = ParameterSnapshot(
            name=name,
            values=copy.deepcopy(dict(values)),
            parent_hash=parent_hash,
            **kwargs,
        )

    @classmethod
    def from_files(
        cls,
        overall_config: str | Path | Mapping[str, Any],
        experiment_config: str | Path | Mapping[str, Any] | None = None,
        *,
        name: str | None = None,
        base_dir: str | Path | None = None,
    ) -> Params:
        raw_overall = (
            _read_json(overall_config)
            if isinstance(overall_config, (str, Path))
            else dict(overall_config)
        )
        if _contains_config_references(raw_overall):
            # The main config owns the child manifests. Keeping this
            # branch here means callers can use the same ``Params.load`` API
            # for both a plain parameter JSON and the application config.
            return load_config_bundle(
                overall_config,
                experiment_config,
                name=name,
                base_dir=base_dir,
            ).params
        overall = _read_source(overall_config)
        experiment = _read_source(experiment_config)
        resolved_name = name or (
            Path(overall_config).stem
            if isinstance(overall_config, (str, Path))
            else "default"
        )
        return cls(merge_parameter_values(overall, experiment), name=resolved_name)

    @classmethod
    def load(
        cls,
        overall_config: str | Path | Mapping[str, Any],
        experiment_config: str | Path | Mapping[str, Any] | None = None,
        *,
        name: str | None = None,
        base_dir: str | Path | None = None,
    ) -> Params:
        return cls.from_files(
            overall_config, experiment_config, name=name, base_dir=base_dir
        )

    @property
    def snapshot(self) -> ParameterSnapshot:
        return self._snapshot.model_copy(deep=True)

    @property
    def values(self) -> dict[str, Any]:
        return copy.deepcopy(self._snapshot.values)

    @property
    def name(self) -> str:
        return self._snapshot.name

    @property
    def content_hash(self) -> str:
        return self._snapshot.content_hash

    @property
    def parent_hash(self) -> str | None:
        return self._snapshot.parent_hash

    def __getitem__(self, key: str) -> Any:
        value = _lookup_with_parameter_section(self._snapshot.values, key)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def __iter__(self):
        return iter(self._snapshot.values)

    def __len__(self) -> int:
        return len(self._snapshot.values)

    def resolve(self, name: str) -> Any:
        return self._snapshot.resolve(name)

    def get(self, name: str, contract: Any = None) -> Any:
        return self._snapshot.get(name, contract)

    def to_dict(self) -> dict[str, Any]:
        return self.values

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return self._snapshot.model_dump(mode="json")

    def model_dump_json(self, **kwargs: Any) -> str:
        return self._snapshot.model_dump_json(**kwargs)


# Child manifests are all loaded through the same Parameter boundary.  Each
# extension namespace is a peer: module/tool/graph/monitor/body/environment
# keys therefore participate in the same strict duplicate-key check before an
# experiment override is applied.
_CONFIG_SECTIONS = (
    "modules",
    "graphs",
    "tools",
    "monitors",
    "bodies",
    "environments",
    "task_adapters",
)

_MASTER_PARAMETER_SECTIONS = frozenset(
    {
        "annotation",
        "environment",
        "execution",
        "memory",
        "model",
        "observability",
        "policy",
        "render",
        "runtime",
        "service",
        "task",
    }
)


def _reference_aliases(section: str) -> tuple[str, ...]:
    singular = {
        "bodies": "body",
        "environments": "environment",
        "task_adapters": "task_adapter",
    }.get(section, section.removesuffix("s"))
    aliases = [
        f"{section}_config",
        f"{section}_configs",
        f"{singular}_config",
        f"{singular}_configs",
        singular,
        section,
    ]
    if section == "environments":
        aliases.extend(("envs_config", "envs_configs", "envs"))
    return tuple(aliases)


def _looks_like_inline_manifest(section: str, value: Any) -> bool:
    """Return whether a mapping is an inline child manifest declaration."""

    if not isinstance(value, Mapping):
        return False
    keys = {
        # The plural form is a manifest; accepting ``name``/``env_id`` also
        # lets a master config inline one selected Body or Environment item
        # without forcing a throw-away child JSON file.
        "bodies": ("bodies", "body", "name"),
        "environments": ("environments", "envs", "environment", "name", "env_id"),
    }.get(section, (section,))
    return any(key in value for key in keys)


def _find_config_reference(raw: Mapping[str, Any], section: str) -> Any:
    """Find a child-config path in either the root or a ``configs`` object."""

    containers: list[Mapping[str, Any]] = [raw]
    nested = raw.get("configs")
    if isinstance(nested, Mapping):
        containers.append(nested)
    for container in containers:
        for alias in _reference_aliases(section):
            value = container.get(alias)
            # A string is an external JSON path. Mapping values are accepted
            # as inline child configs only when they already look like a
            # complete manifest; a plain parameter object remains a parameter.
            if isinstance(value, (str, Path)) or _looks_like_inline_manifest(
                section, value
            ):
                return value
    return None


def _contains_config_references(raw: Mapping[str, Any]) -> bool:
    return any(
        _find_config_reference(raw, section) is not None for section in _CONFIG_SECTIONS
    )


def _normalise_root(raw: Mapping[str, Any]) -> dict[str, Any]:
    nested = raw.get("robotenon")
    return dict(nested) if isinstance(nested, Mapping) else dict(raw)


def _unknown_config_fields(actual: set[str], allowed: set[str], *, source: str) -> None:
    unknown = sorted(actual - allowed)
    if unknown:
        raise ParameterError(
            f"{source} contains unknown framework fields: {', '.join(unknown)}; "
            "put extension-owned values under parameters/params/metadata"
        )


def _validate_master_config(raw: Mapping[str, Any], *, source: str) -> None:
    """Validate the raw application boundary before fields can be projected away."""

    if "robotenon" in raw:
        if not isinstance(raw["robotenon"], Mapping):
            raise ParameterError(f"{source}.robotenon must be an object")
        _unknown_config_fields(set(raw), {"robotenon"}, source=source)
        root = dict(raw["robotenon"])
    else:
        root = dict(raw)
    reference_fields = {
        alias for section in _CONFIG_SECTIONS for alias in _reference_aliases(section)
    }
    manifest_fields = {
        field for fields in _MANIFEST_FIELDS.values() for field in fields
    }
    allowed = {
        "config_version",
        "configs",
        "parameters",
        "observation_cache",
        "safety_state",
        *reference_fields,
        *manifest_fields,
        *_MASTER_PARAMETER_SECTIONS,
    }
    _unknown_config_fields(set(root), allowed, source=source)
    if "parameters" in root and not isinstance(root["parameters"], Mapping):
        raise ParameterError(f"{source}.parameters must be an object")
    if "configs" in root:
        configs = root["configs"]
        if not isinstance(configs, Mapping):
            raise ParameterError(f"{source}.configs must be an object")
        _unknown_config_fields(
            set(configs), reference_fields, source=f"{source}.configs"
        )


def _validate_child_config(
    section: str, raw: Mapping[str, Any], *, source: str
) -> None:
    """Reject child-manifest typos before aliases/direct items are normalized."""

    if "robotenon" in raw:
        if not isinstance(raw["robotenon"], Mapping):
            raise ParameterError(f"{source}.robotenon must be an object")
        _unknown_config_fields(set(raw), {"robotenon"}, source=source)
        root = dict(raw["robotenon"])
    else:
        root = dict(raw)
    allowed = set(_MANIFEST_FIELDS[section]) | {"parameters"}
    if section == "bodies":
        allowed |= {"name", "id", "version", "kind", "params", "enabled"}
    elif section == "environments":
        allowed |= {
            "name",
            "env_id",
            "id",
            "version",
            "kind",
            "params",
            "enabled",
        }
    _unknown_config_fields(set(root), allowed, source=source)
    if "parameters" in root and not isinstance(root["parameters"], Mapping):
        raise ParameterError(f"{source}.parameters must be an object")


def _main_layer(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Extract ordinary parameters from the master config.

    Path declarations are control metadata and do not become parameter keys.
    A top-level ``parameters`` object is flattened into the same namespace so
    callers can write ``params.get("policy.timeout_ms", [int, 1000])``.
    """

    root = _normalise_root(raw)
    layer: dict[str, Any] = {}
    nested = root.get("parameters")
    if isinstance(nested, Mapping):
        layer = _strict_merge(layer, nested, source="config.json.parameters")
    ref_keys = {
        alias for section in _CONFIG_SECTIONS for alias in _reference_aliases(section)
    }
    for key, value in root.items():
        if key in {"configs", "parameters"}:
            continue
        # A direct ``modules``/``graphs``/``tools`` object or list is a valid
        # inline manifest. Only an external string declaration is metadata.
        if key in ref_keys and (
            isinstance(value, (str, Path))
            or any(
                key == alias and _looks_like_inline_manifest(section, value)
                for section in _CONFIG_SECTIONS
                for alias in _reference_aliases(section)
            )
        ):
            continue
        layer = _strict_merge(layer, {key: value}, source="config.json")
    return layer


def _child_layer(raw: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    """Extract a child manifest, flattening an optional parameters wrapper."""

    root = _normalise_root(raw)
    nested = root.get("parameters")
    if not isinstance(nested, Mapping):
        # ``config_version`` describes the child schema, not a user parameter
        # and therefore must not collide with the master's version key.
        return {
            key: copy.deepcopy(value)
            for key, value in root.items()
            if key != "config_version"
        }
    layer = _strict_merge({}, nested, source=f"{source}.parameters")
    direct = {
        key: value
        for key, value in root.items()
        if key not in {"parameters", "config_version"}
    }
    return _strict_merge(layer, direct, source=source)


def _experiment_layer(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Extract an experiment override; unlike base layers, duplicate keys win."""

    root = _normalise_root(raw)
    nested = root.get("parameters")
    if isinstance(nested, Mapping):
        layer = copy.deepcopy(dict(nested))
        direct = {
            key: value
            for key, value in root.items()
            if key not in {"parameters", "config_version", "configs"}
        }
        return merge_parameter_values(layer, direct)
    return {
        key: copy.deepcopy(value)
        for key, value in root.items()
        if key not in {"config_version", "configs"}
    }


def _source_with_base(
    source: str | Path | Mapping[str, Any],
    *,
    base_dir: str | Path | None = None,
) -> tuple[dict[str, Any], Path]:
    if isinstance(source, (str, Path)):
        path = Path(source)
        return _read_json(path), path.parent
    mapping = dict(source)
    # A plain mapping has no reliable filesystem provenance.  Silently using
    # cwd made ``json.loads(Path(...).read_text())`` resolve child manifests
    # differently from the same file passed as a Path.  Require callers to
    # provide the directory explicitly when path references are present.
    resolved_base = base_dir or getattr(source, "__robotenon_base_dir__", None)
    if resolved_base is None and _contains_config_references(mapping):
        references = [
            _find_config_reference(mapping, section) for section in _CONFIG_SECTIONS
        ]
        if any(isinstance(value, (str, Path)) for value in references):
            raise ParameterError(
                "mapping config contains relative child-config paths; pass "
                "base_dir=... (or provide the config as a file path)"
            )
    return mapping, Path(resolved_base) if resolved_base is not None else Path.cwd()


def _resolve_child_source(value: Any, base_dir: Path) -> tuple[dict[str, Any], str]:
    if isinstance(value, (str, Path)):
        path = Path(value)
        if not path.is_absolute():
            path = base_dir / path
        return _read_json(path), str(path)
    if isinstance(value, Mapping):
        return dict(value), "<inline>"
    raise ParameterError("child config reference must be a JSON path or object")


def _canonical_child_manifest(section: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize singular/direct Body and Environment child manifests.

    The canonical merged config always stores these sections as arrays.  A
    master file may still use the convenient ``body_config: {name: ...}``,
    ``body: {...}``, ``envs: {...}`` or ``environment: {...}`` spellings;
    normalize them before strict merging so unrelated master keys cannot be
    mistaken for constructor parameters of the selected provider.
    """

    root = _normalise_root(raw)
    if section == "bodies":
        plural, aliases, plugin_key = "bodies", ("body",), "plugin_bodies"
        item_keys = {"name", "id", "version", "kind", "params", "enabled"}
    elif section == "environments":
        plural, aliases, plugin_key = (
            "environments",
            ("envs", "environment"),
            "plugin_environments",
        )
        item_keys = {"name", "env_id", "id", "version", "kind", "params", "enabled"}
    else:
        return dict(root)

    if plural in root:
        return dict(root)
    for alias in aliases:
        if alias not in root:
            continue
        normalized = dict(root)
        normalized[plural] = normalized.pop(alias)
        return normalized

    if not any(key in root for key in ("name", "env_id")):
        return dict(root)

    # A direct provider item is allowed only to carry provider fields.  Keep
    # manifest-level plugin/config metadata, but do not promote all master
    # parameters into the item.
    item = {key: root[key] for key in item_keys if key in root}
    if section == "environments" and "name" not in item and "env_id" in item:
        item["name"] = item.pop("env_id")
    normalized = {
        key: value
        for key, value in root.items()
        if key in {"config_version", plugin_key}
    }
    normalized[plural] = [item]
    return normalized


_MANIFEST_FIELDS = {
    "modules": ("config_version", "plugin_modules", "modules"),
    "graphs": ("config_version", "graphs"),
    "tools": ("config_version", "plugin_tools", "tools"),
    "monitors": (
        "config_version",
        "plugin_monitors",
        "monitors",
        "poll_hz",
        "poll_hz_by_id",
        "on_error_level",
        "on_error_level_by_id",
    ),
    "bodies": ("config_version", "plugin_bodies", "bodies", "body"),
    "environments": (
        "config_version",
        "plugin_environments",
        "plugin_envs",
        "environments",
        "envs",
        "environment",
    ),
    "task_adapters": (
        "config_version",
        "plugin_task_adapters",
        "plugin_task_adapt",
        "task_adapters",
        "task_adapter",
    ),
}


@dataclass(frozen=True, slots=True)
class ConfigBundle:
    """One canonical configuration assembled by the Parameter module."""

    params: Params
    config: dict[str, Any]
    sources: dict[str, str]

    def section(self, name: str) -> Any:
        """Return a defensive copy of one merged top-level section."""

        if name not in self.config:
            raise KeyError(name)
        return copy.deepcopy(self.config[name])

    def manifest(self, section: str) -> dict[str, Any]:
        """Return only the canonical fields belonging to one provider manifest."""
        if section not in _MANIFEST_FIELDS:
            raise KeyError(f"unknown configuration manifest section: {section}")
        return {
            key: copy.deepcopy(self.config[key])
            for key in _MANIFEST_FIELDS[section]
            if key in self.config
        }


def load_config_bundle(
    config: str | Path | Mapping[str, Any],
    experiment_config: str | Path | Mapping[str, Any] | None = None,
    *,
    name: str | None = None,
    base_dir: str | Path | None = None,
) -> ConfigBundle:
    """Load all provider manifests into one canonical Params object.

    The master config declares child paths with any of the equivalent names
    ``modules_config``/``modules_configs``/``modules`` (and likewise for
    ``graphs``, ``tools``, ``monitors``, ``bodies`` and ``environments``;
    ``envs_config`` is also accepted). Child base layers are merged with
    strict key conflict detection. The optional experiment layer is applied
    afterwards and is explicitly allowed to replace values.
    """

    root, resolved_base_dir = _source_with_base(config, base_dir=base_dir)
    config_source = str(config) if isinstance(config, (str, Path)) else "<inline>"
    _validate_master_config(root, source=config_source)
    root = _normalise_root(root)
    merged = _strict_merge({}, _main_layer(root), source="config.json")
    sources = {"main": config_source}

    for section in _CONFIG_SECTIONS:
        reference = _find_config_reference(root, section)
        if reference is None:
            continue
        child_raw, source_name = _resolve_child_source(reference, resolved_base_dir)
        _validate_child_config(section, child_raw, source=source_name)
        child_raw = _canonical_child_manifest(section, child_raw)
        child = _child_layer(child_raw, source=source_name)
        merged = _strict_merge(merged, child, source=source_name)
        sources[section] = source_name

    if experiment_config is not None:
        experiment_raw, _ = _source_with_base(experiment_config, base_dir=base_dir)
        experiment_source = (
            str(experiment_config)
            if isinstance(experiment_config, (str, Path))
            else "<experiment-inline>"
        )
        _validate_master_config(experiment_raw, source=experiment_source)
        merged = merge_parameter_values(merged, _experiment_layer(experiment_raw))
        sources["experiment"] = (
            str(experiment_config)
            if isinstance(experiment_config, (str, Path))
            else "<inline>"
        )

    resolved_name = name or (
        Path(experiment_config).stem
        if isinstance(experiment_config, (str, Path))
        else Path(config).stem
        if isinstance(config, (str, Path))
        else "default"
    )
    params = Params(merged, name=resolved_name)
    return ConfigBundle(params=params, config=copy.deepcopy(merged), sources=sources)


def config_manifest(source: Any, section: str) -> dict[str, Any]:
    """Resolve a standalone manifest or project one out of a master config."""
    if section not in _MANIFEST_FIELDS:
        raise KeyError(f"unknown configuration manifest section: {section}")
    if isinstance(source, ConfigBundle):
        return source.manifest(section)

    if isinstance(source, Params):
        original = source.to_dict()
    elif isinstance(source, Mapping):
        original = dict(source)
    elif hasattr(source, "model_dump"):
        original = source.model_dump(mode="json")
    elif hasattr(source, "to_dict") and not isinstance(source, (str, Path)):
        original = source.to_dict()
    else:
        original = dict(_read_json(source))

    nested = original.get("robotenon")
    root = dict(nested) if isinstance(nested, Mapping) else original
    sections_present = {
        name
        for name in _MANIFEST_FIELDS
        if name in root or any(alias in root for alias in _reference_aliases(name))
    }
    master_config = (
        _contains_config_references(root)
        or any(key in root for key in ("configs", "parameters"))
        or len(sections_present) > 1
        or any(
            key in root
            for key in (
                "runtime",
                "model",
                "task",
                "environment",
                "render",
                "service",
                "execution",
                "observability",
                "observation_cache",
                "safety_state",
            )
        )
    )
    if not master_config:
        return root
    if isinstance(source, Params):
        return {
            key: copy.deepcopy(root[key])
            for key in _MANIFEST_FIELDS[section]
            if key in root
        }
    return load_config_bundle(source).manifest(section)


def load_config(
    config: str | Path | Mapping[str, Any],
    experiment_config: str | Path | Mapping[str, Any] | None = None,
    *,
    name: str | None = None,
    base_dir: str | Path | None = None,
) -> ConfigBundle:
    """Short alias for :func:`load_config_bundle`."""

    return load_config_bundle(config, experiment_config, name=name, base_dir=base_dir)


class ParameterService:
    """Small service facade for loading a run's parameter snapshot once."""

    def load(
        self,
        overall_config: str | Path | Mapping[str, Any],
        experiment_config: str | Path | Mapping[str, Any] | None = None,
        *,
        name: str | None = None,
        base_dir: str | Path | None = None,
    ) -> Params:
        return Params.from_files(
            overall_config, experiment_config, name=name, base_dir=base_dir
        )


def load_params(
    overall_config: str | Path | Mapping[str, Any],
    experiment_config: str | Path | Mapping[str, Any] | None = None,
    *,
    name: str | None = None,
    base_dir: str | Path | None = None,
) -> Params:
    """Load and merge overall and experiment JSON configuration."""

    return Params.from_files(
        overall_config, experiment_config, name=name, base_dir=base_dir
    )


def load_parameter_snapshot(
    overall_config: str | Path | Mapping[str, Any],
    experiment_config: str | Path | Mapping[str, Any] | None = None,
    *,
    name: str | None = None,
    base_dir: str | Path | None = None,
) -> ParameterSnapshot:
    return load_params(
        overall_config, experiment_config, name=name, base_dir=base_dir
    ).snapshot


__all__ = [
    "ConfigBundle",
    "ParameterConflictError",
    "ParameterError",
    "ParameterService",
    "ParameterSnapshot",
    "ParameterTypeError",
    "Params",
    "load_config",
    "load_config_bundle",
    "load_parameter_snapshot",
    "load_params",
    "merge_parameter_values",
]
