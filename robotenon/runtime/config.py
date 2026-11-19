"""JSON/TOML configuration for all resident providers and Workflow wiring."""

from __future__ import annotations

import json
import hashlib
from copy import deepcopy
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef,import-not-found]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

from pydantic import Field, StrictInt

from ..infra.params import ConfigBundle, Params, config_manifest, load_config_bundle
from ..schemas import ConfigRoboModel, RoboModel, SafetyLevel, field_validator


class ModuleConfig(ConfigRoboModel):
    """One registered Module instance kept resident for the process lifetime."""

    name: str
    id: str | None = None
    version: str | None = None
    kind: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ToolDeploymentConfig(ConfigRoboModel):
    """Worker-owned model service; GPU indices are relative to that Worker."""

    backend: str
    gpu_ids: list[StrictInt]
    model_path: str | None = None
    executable: str | None = None
    command: list[str] = Field(default_factory=list)
    extra_args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    startup_timeout_s: float = Field(default=600, gt=0)
    shutdown_timeout_s: float = Field(default=10, gt=0)
    health_path: str | None = None

    @field_validator("gpu_ids")
    @classmethod
    def validate_gpu_ids(cls, values):
        if not values or any(value < 0 for value in values) or len(set(values)) != len(values):
            raise ValueError("deployment.gpu_ids requires distinct non-negative Worker-local indices")
        return values


class ToolConfig(ConfigRoboModel):
    """One registered Tool instance kept resident for the process lifetime.

    ``name`` is the registry string, while ``id`` is the instance name used by
    application code.  Multiple instances of one provider are allowed (for
    example, two model endpoints or separate memory databases).
    """

    name: str
    id: str | None = None
    version: str | None = None
    kind: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    parameter_bindings: dict[str, str] = Field(default_factory=dict)
    role: str = "tool"
    enabled: bool = True
    deployment: ToolDeploymentConfig | None = None


class BodyConfig(ConfigRoboModel):
    """One configured resident Body service.

    ``name`` is the Body registry string and ``id`` is the optional
    deployment label.  A process normally selects one enabled Body service;
    keeping the manifest item explicit still lets different deployments use
    different registered providers without changing Workflow code.
    """

    name: str
    id: str | None = None
    version: str | None = None
    kind: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class EnvironmentConfig(ConfigRoboModel):
    """One EnvironmentAdapter selected from the independent env registry."""

    name: str
    id: str | None = None
    version: str | None = None
    kind: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class MonitorConfig(ConfigRoboModel):
    """One configured monitor instance.

    ``modes`` controls *how* the instance is invoked and is independent from
    ``kind`` (the registry category such as ``safety`` or ``perception``).
    A single instance may list both ``resident`` and ``request``; the runtime
    then shares the same object/model state for its timer and workflow calls.
    """

    name: str
    id: str | None = None
    version: str | None = None
    kind: str | None = None
    modes: tuple[str, ...] = ("request",)
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    poll_hz: float | None = Field(default=None, gt=0.0)
    on_error_level: SafetyLevel | None = None

    @field_validator("modes", mode="before")
    @classmethod
    def _normalize_modes(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            value = ("request",)
        if isinstance(value, str):
            value = value.strip().lower()
            if value == "both":
                value = ("resident", "request")
            elif value in {"periodic", "poll", "resident"}:
                value = ("resident",)
            elif value in {"on_request", "on-request", "request"}:
                value = ("request",)
            else:
                raise ValueError(
                    "monitor modes must be resident, request or both"
                )
        elif isinstance(value, (list, tuple, set)):
            value = tuple(str(item).strip().lower() for item in value)
        else:
            raise TypeError("monitor modes must be a string or array")
        normalized: list[str] = []
        for mode in value:
            if mode in {"periodic", "poll"}:
                mode = "resident"
            elif mode in {"on_request", "on-request"}:
                mode = "request"
            if mode not in {"resident", "request"}:
                raise ValueError(
                    f"unsupported monitor mode {mode!r}; use resident/request"
                )
            if mode not in normalized:
                normalized.append(mode)
        if not normalized:
            raise ValueError("each monitor must enable resident and/or request mode")
        return tuple(normalized)

    @property
    def mode(self) -> str:
        """Compatibility spelling: ``resident``, ``request`` or ``both``."""

        return "both" if len(self.modes) == 2 else self.modes[0]


def _normalize_monitor_item(item: Any) -> dict[str, Any]:
    """Normalize legacy singular/boolean mode spellings for one item."""

    if not isinstance(item, Mapping):
        raise ValueError("each monitor config must be an object")
    value = dict(item)
    legacy_type = value.pop("type", None)
    if legacy_type is not None:
        normalized_type = str(legacy_type).strip().lower()
        if normalized_type in {"resident", "periodic", "request", "on_request", "both"}:
            if "modes" not in value and "mode" not in value:
                value["modes"] = legacy_type
        elif "kind" not in value:
            value["kind"] = legacy_type
    if "modes" not in value:
        if "mode" in value:
            value["modes"] = value.pop("mode")
        else:
            enabled_modes = []
            if bool(value.pop("resident", value.pop("periodic", False))):
                enabled_modes.append("resident")
            if bool(value.pop("request", value.pop("on_request", False))):
                enabled_modes.append("request")
            if enabled_modes:
                value["modes"] = enabled_modes
            elif str(value.get("kind", "")).lower() in {
                "resident",
                "periodic",
                "request",
                "on_request",
                "both",
            }:
                # Early drafts used ``kind`` for invocation mode.  Keep
                # those manifests readable while reserving kind for the
                # registry's semantic category.
                value["modes"] = value["kind"]
                value["kind"] = None
    return value


class MonitorsConfig(ConfigRoboModel):
    """Registry manifest for resident and request-capable monitors."""

    config_version: int = Field(default=1, ge=1)
    plugin_monitors: list[str] = Field(default_factory=list)
    monitors: list[MonitorConfig] = Field(default_factory=list)
    poll_hz: float = Field(default=20.0, gt=0.0)
    poll_hz_by_id: dict[str, float] = Field(default_factory=dict)
    on_error_level: SafetyLevel = SafetyLevel.P3
    on_error_level_by_id: dict[str, SafetyLevel] = Field(default_factory=dict)

    @field_validator("monitors", mode="before")
    @classmethod
    def _normalize_monitor_items(cls, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("monitors config must contain a 'monitors' array")
        return [_normalize_monitor_item(item) for item in value]


class ModulesConfig(ConfigRoboModel):
    """The single resident-module manifest."""

    config_version: int = Field(default=1, ge=1)
    plugin_modules: list[str] = Field(default_factory=list)
    modules: list[ModuleConfig] = Field(default_factory=list)


class ToolsConfig(ConfigRoboModel):
    """The single resident-tool manifest."""

    config_version: int = Field(default=1, ge=1)
    plugin_tools: list[str] = Field(default_factory=list)
    tools: list[ToolConfig] = Field(default_factory=list)


class BodiesConfig(ConfigRoboModel):
    """Manifest for registered Body providers.

    Body services are process boundaries rather than Graph Modules.  The
    factory returns one selected resident service and runtime dependencies
    (executor, safety store, or IPC options) are injected by bootstrap.
    """

    config_version: int = Field(default=1, ge=1)
    plugin_bodies: list[str] = Field(default_factory=list)
    bodies: list[BodyConfig] = Field(default_factory=list)

    @field_validator("bodies", mode="before")
    @classmethod
    def _normalize_bodies(cls, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if isinstance(value, Mapping):
            return [dict(value)]
        if not isinstance(value, list):
            raise ValueError("bodies config must contain a 'bodies' array")
        return [dict(item) if isinstance(item, Mapping) else item for item in value]


class EnvironmentsConfig(ConfigRoboModel):
    """Manifest for registered EnvironmentAdapter providers."""

    config_version: int = Field(default=1, ge=1)
    plugin_environments: list[str] = Field(default_factory=list)
    environments: list[EnvironmentConfig] = Field(default_factory=list)

    @field_validator("environments", mode="before")
    @classmethod
    def _normalize_environments(cls, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if isinstance(value, Mapping):
            value = [value]
        if not isinstance(value, list):
            raise ValueError("environments config must contain an 'environments' array")
        normalized: list[Any] = []
        for item in value:
            if isinstance(item, Mapping):
                candidate = dict(item)
                # ``env_id`` is a natural spelling for adapter manifests;
                # normalize it to the common registry ``name`` field.
                if "name" not in candidate and "env_id" in candidate:
                    candidate["name"] = candidate.pop("env_id")
                normalized.append(candidate)
            else:
                normalized.append(item)
        return normalized


class TaskAdapterConfig(ConfigRoboModel):
    """One ordered Task Adapt provider instance.

    The order of items in :class:`TaskAdaptersConfig.task_adapters` is the
    execution order.  ``params`` belongs to the adapter and is deliberately
    kept separate from the resident Environment configuration.
    """

    name: str
    id: str | None = None
    version: str | None = None
    kind: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class TaskAdaptersConfig(ConfigRoboModel):
    """Ordered manifest for registered Task Adapt providers."""

    config_version: int = Field(default=1, ge=1)
    plugin_task_adapters: list[str] = Field(default_factory=list)
    task_adapters: list[TaskAdapterConfig] = Field(default_factory=list)

    @field_validator("task_adapters", mode="before")
    @classmethod
    def _normalize_task_adapters(cls, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if isinstance(value, Mapping):
            value = [value]
        if not isinstance(value, list):
            raise ValueError("task adapters config must contain a 'task_adapters' array")
        return [dict(item) if isinstance(item, Mapping) else item for item in value]


class ObservationHotConfig(ConfigRoboModel):
    max_records: int = Field(default=64, gt=0)
    max_bytes: int = Field(default=268_435_456, gt=0)
    ttl_seconds: float = Field(default=300.0, gt=0.0)


class ObservationStorageConfig(ConfigRoboModel):
    root: str = "./data/observations"
    index: str | None = None


class ObservationPersistConfig(ConfigRoboModel):
    async_enabled: bool = Field(default=True, alias="async")
    queue_size: int = Field(default=256, gt=0)
    workers: int = Field(default=2, gt=0)
    on_queue_full: Literal["block", "drop_oldest", "drop_newest", "error"] = "drop_oldest"

    @property
    def asynchronous(self) -> bool:
        return self.async_enabled


class ObservationRetentionConfig(ConfigRoboModel):
    keep_sessions: int = Field(default=20, gt=0)
    keep_days: int = Field(default=30, gt=0)


class ObservationCacheConfig(ConfigRoboModel):
    """Typed view of the infrastructure-level observation cache settings."""

    enabled: bool = True
    backend: str = "hybrid"
    plugin_stores: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    hot: ObservationHotConfig = Field(default_factory=ObservationHotConfig)
    storage: ObservationStorageConfig = Field(default_factory=ObservationStorageConfig)
    persist: ObservationPersistConfig = Field(default_factory=ObservationPersistConfig)
    retention: ObservationRetentionConfig = Field(default_factory=ObservationRetentionConfig)
    uri_scheme: str = "cache"

    @field_validator("persist", mode="before")
    @classmethod
    def _normalize_persist(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "on_persist_queue_full" in value and "on_queue_full" not in value:
            value = dict(value)
            value["on_queue_full"] = value.pop("on_persist_queue_full")
        return value


class SafetyStateConfig(ConfigRoboModel):
    """Configuration for the process/shared P0-P4 safety state.

    ``memory`` is convenient for an in-process unit test.  Any deployment
    where Environment, Monitor, Workflow or Body run in different processes
    must use a provider declaring shared storage, such as ``sqlite``, and
    configure all processes to use the same backing store.
    Keeping this choice in the canonical config prevents a process from
    silently falling back to an isolated in-memory store.
    """

    backend: str = "memory"
    plugin_stores: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    path: str = "./data/runtime/safety.sqlite3"
    # Shared resources retain their configured path across task attempts.
    scope: Literal["shared", "task"] = "shared"
    # When enabled, bootstrap code refuses to construct an in-memory store.
    # This is useful for service deployments where a missing shared path must
    # fail closed rather than degrade safety propagation.
    require_shared: bool = False


@dataclass(frozen=True, slots=True)
class LoadedConfigs:
    """Typed views derived from one canonical Parameter snapshot."""

    params: Params
    modules: ModulesConfig
    graphs: Any
    tools: ToolsConfig
    monitors: MonitorsConfig
    bundle: ConfigBundle
    bodies: BodiesConfig = field(default_factory=BodiesConfig)
    environments: EnvironmentsConfig = field(default_factory=EnvironmentsConfig)
    task_adapters: TaskAdaptersConfig = field(default_factory=TaskAdaptersConfig)
    observation_cache: ObservationCacheConfig = field(default_factory=ObservationCacheConfig)
    safety_state: SafetyStateConfig = field(default_factory=SafetyStateConfig)


def _read_raw(path: str | Path) -> Mapping[str, Any]:
    config_path = Path(path)
    text = config_path.read_text(encoding="utf-8")
    suffix = config_path.suffix.lower()
    if suffix == ".json":
        value = json.loads(text)
    elif suffix == ".toml":
        if tomllib is None:
            raise RuntimeError("TOML config on Python 3.10 requires the tomli package")
        value = tomllib.loads(text)
    else:
        raise ValueError("RoboTenon config must be .json or .toml")
    if not isinstance(value, Mapping):
        raise ValueError("RoboTenon config root must be an object/table")
    return value


def _config_mapping(source: Any) -> Mapping[str, Any]:
    """Resolve a direct manifest, Params, or master config into one mapping."""

    if isinstance(source, ConfigBundle):
        return source.config
    if isinstance(source, Params):
        return source.to_dict()
    if isinstance(source, RoboModel):
        return source.model_dump(mode="json")
    if isinstance(source, Mapping):
        raw = dict(source)
    else:
        raw = dict(_read_raw(source))
    # A master config has child paths (or inline manifests) and must be
    # resolved by the Parameter module before schema validation.  Perform the
    # check for mappings too, so callers can build a config object in Python
    # without bypassing the single merge path.
    section_singular = {
        "modules": "module",
        "graphs": "graph",
        "tools": "tool",
        "monitors": "monitor",
        "bodies": "body",
        "environments": "environment",
        "task_adapters": "task_adapter",
    }
    inline_keys = {
        "bodies": ("bodies", "body", "name"),
        "environments": ("environments", "envs", "environment", "name", "env_id"),
    }
    aliases_by_section: dict[str, tuple[str, ...]] = {}
    for section, singular in section_singular.items():
        aliases = [
            f"{section}_config",
            f"{section}_configs",
            f"{singular}_config",
            f"{singular}_configs",
            singular,
        ]
        if section == "environments":
            aliases.extend(("envs_config", "envs_configs", "envs"))
        aliases.append(section)
        aliases_by_section[section] = tuple(aliases)
    if any(
        isinstance(raw.get(alias), str)
        or (
            isinstance(raw.get(alias), Mapping)
            and any(
                key in raw.get(alias, {})
                for key in inline_keys.get(section, (section,))
            )
        )
        for section in section_singular
        for alias in aliases_by_section[section]
    ) or isinstance(raw.get("configs"), Mapping):
        raw = load_config_bundle(source).config
    nested = raw.get("robotenon")
    return dict(nested) if isinstance(nested, Mapping) else raw


def load_modules_config(source: Any) -> ModulesConfig:
    """Load and validate the resident-module manifest."""
    raw = config_manifest(source, "modules")
    if not isinstance(raw.get("modules"), list):
        raise ValueError("modules config must contain a 'modules' array")
    return ModulesConfig.model_validate(raw)


def load_tools_config(source: Any) -> ToolsConfig:
    """Load and validate the resident-tool manifest."""

    raw = config_manifest(source, "tools")
    if not isinstance(raw.get("tools"), list):
        raise ValueError("tools config must contain a 'tools' array")
    return ToolsConfig.model_validate(raw)


def load_bodies_config(source: Any) -> BodiesConfig:
    """Load and validate the resident Body service manifest."""

    raw = config_manifest(source, "bodies")
    direct_item = not any(key in raw for key in ("bodies", "body")) and "name" in raw
    values = raw.get("bodies", raw.get("body", raw if "name" in raw else []))
    if values is None:
        values = []
    if direct_item:
        values = {
            key: value
            for key, value in raw.items()
            if key not in {"config_version", "plugin_bodies"}
        }
        raw = {
            key: raw[key] for key in ("config_version", "plugin_bodies") if key in raw
        }
    else:
        raw.pop("body", None)
    if isinstance(values, Mapping):
        values = [values]
    if not isinstance(values, list):
        raise ValueError("bodies config must contain a 'bodies' array")
    raw["bodies"] = values
    return BodiesConfig.model_validate(raw)


def load_environments_config(source: Any) -> EnvironmentsConfig:
    """Load and validate the independent EnvironmentAdapter manifest."""

    raw = config_manifest(source, "environments")
    direct_item = not any(
        key in raw for key in ("environments", "envs", "environment")
    ) and ("name" in raw or "env_id" in raw)
    values = raw.get(
        "environments",
        raw.get(
            "envs",
            raw.get("environment", raw if "name" in raw or "env_id" in raw else []),
        ),
    )
    if values is None:
        values = []
    if direct_item:
        values = {
            key: value
            for key, value in raw.items()
            if key not in {"config_version", "plugin_environments", "plugin_envs"}
        }
        raw = {
            key: raw[key]
            for key in ("config_version", "plugin_environments", "plugin_envs")
            if key in raw
        }
    else:
        raw.pop("envs", None)
        raw.pop("environment", None)
    if isinstance(values, Mapping):
        values = [values]
    if not isinstance(values, list):
        raise ValueError("environments config must contain an 'environments' array")
    raw["environments"] = values
    plugins = raw.get("plugin_environments", raw.get("plugin_envs"))
    if plugins is not None:
        raw["plugin_environments"] = plugins
    raw.pop("plugin_envs", None)
    return EnvironmentsConfig.model_validate(raw)


def load_task_adapters_config(source: Any) -> TaskAdaptersConfig:
    """Load the ordered Task Adapt manifest from the canonical bundle."""

    raw = config_manifest(source, "task_adapters")
    values = raw.get("task_adapters", raw.get("task_adapter", []))
    if values is None:
        values = []
    if isinstance(values, Mapping):
        values = [values]
    if not isinstance(values, list):
        raise ValueError("task adapters config must contain a 'task_adapters' array")
    raw["task_adapters"] = values
    plugins = raw.get("plugin_task_adapters", raw.get("plugin_task_adapt"))
    if plugins is not None:
        raw["plugin_task_adapters"] = plugins
    raw.pop("task_adapter", None)
    raw.pop("plugin_task_adapt", None)
    return TaskAdaptersConfig.model_validate(raw)


def load_monitors_config(source: Any) -> MonitorsConfig:
    """Load and validate the monitor registry/mode manifest.

    ``mode`` (singular), ``periodic`` and ``both`` are accepted as migration
    conveniences.  The canonical form written back to configs is
    ``modes: ["resident", "request"]``.
    """

    raw = config_manifest(source, "monitors")
    values = raw.get("monitors", [])
    if values is None:
        values = []
    if not isinstance(values, list):
        raise ValueError("monitors config must contain a 'monitors' array")
    raw["monitors"] = [_normalize_monitor_item(item) for item in values]
    return MonitorsConfig.model_validate(raw)


def load_observation_cache_config(source: Any) -> ObservationCacheConfig:
    """Load observation-cache settings from the same merged Params bundle."""

    raw = dict(_config_mapping(source))
    value = raw.get("observation_cache", {})
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError("observation_cache config must be an object")
    return ObservationCacheConfig.model_validate(value)


def load_safety_state_config(source: Any) -> SafetyStateConfig:
    """Load safety-state settings from the canonical merged config."""

    raw = dict(_config_mapping(source))
    value = raw.get("safety_state", {})
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError("safety_state config must be an object")
    return SafetyStateConfig.model_validate(value)


def load_application_config(
    config: str | Path | Mapping[str, Any],
    experiment_config: str | Path | Mapping[str, Any] | None = None,
    *,
    base_dir: str | Path | None = None,
) -> LoadedConfigs:
    """Load the master config and derive all extension manifests from one Params."""

    bundle = load_config_bundle(config, experiment_config, base_dir=base_dir)
    return _loaded_bundle(bundle)


def freeze_config(loaded: LoadedConfigs) -> dict[str, Any]:
    """Portable, merged config: consumers never reopen its source files."""
    from ..contracts.serialization import jsonable
    value = jsonable({"version": 1, "config": loaded.bundle.config, "sources": loaded.bundle.sources,
                     "name": loaded.params.name})
    value["sha256"] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return value


def load_config_snapshot(snapshot: str | Path | Mapping[str, Any]) -> LoadedConfigs:
    value = deepcopy(dict(snapshot)) if isinstance(snapshot, Mapping) else json.loads(Path(snapshot).read_text())
    digest = value.pop("sha256", None)
    expected = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if value.get("version") != 1 or digest != expected:
        raise ValueError("invalid or modified configuration snapshot")
    config = value["config"]
    return _loaded_bundle(ConfigBundle(params=Params(config, name=value.get("name", "default")),
                                      config=config, sources=value["sources"]))


def load_request_config(request) -> LoadedConfigs:
    snapshot = getattr(request, "config_snapshot", None)
    return (load_config_snapshot(snapshot) if snapshot is not None
            else load_application_config(request.config, request.experiment_config))


def _loaded_bundle(bundle: ConfigBundle) -> LoadedConfigs:
    # Imported lazily to keep runtime.config independent from workflow import
    # side effects during package discovery.
    from ..workflow.definition import load_graphs_config

    return LoadedConfigs(
        params=bundle.params,
        modules=load_modules_config(bundle),
        graphs=load_graphs_config(bundle),
        tools=load_tools_config(bundle),
        monitors=load_monitors_config(bundle),
        bundle=bundle,
        bodies=load_bodies_config(bundle),
        environments=load_environments_config(bundle),
        task_adapters=load_task_adapters_config(bundle),
        observation_cache=load_observation_cache_config(bundle),
        safety_state=load_safety_state_config(bundle),
    )


__all__ = [
    "LoadedConfigs",
    "BodyConfig",
    "BodiesConfig",
    "EnvironmentConfig",
    "EnvironmentsConfig",
    "TaskAdapterConfig",
    "TaskAdaptersConfig",
    "ModuleConfig",
    "ModulesConfig",
    "MonitorConfig",
    "MonitorsConfig",
    "ObservationCacheConfig",
    "ObservationHotConfig",
    "ObservationPersistConfig",
    "ObservationRetentionConfig",
    "ObservationStorageConfig",
    "SafetyStateConfig",
    "ToolConfig",
    "ToolsConfig",
    "load_application_config",
    "load_bodies_config",
    "load_environments_config",
    "load_task_adapters_config",
    "load_modules_config",
    "load_monitors_config",
    "load_observation_cache_config",
    "load_safety_state_config",
    "load_tools_config",
]
