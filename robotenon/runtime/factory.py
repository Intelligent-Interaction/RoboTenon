"""Bootstrap helpers for resident Tools, Modules and Workflow."""

from __future__ import annotations

from ..contracts.persistence import SafetyStore

import hashlib
import importlib
import inspect
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..bodies.registry import RegisteredBody, body_registry, load_builtin_bodies
from ..envs import environment_registry, load_builtin_environments
from ..modules.base import Module, ModuleRuntime
from ..monitors import MonitorRuntime, load_builtin_monitors, monitor_registry
from ..monitors.registry import RegisteredMonitor
from ..runtime.safety import GLOBAL_SAFETY_STATE
from ..task_adapters import (
    ConfiguredTaskAdapter,
    load_builtin_task_adapters,
    load_plugin_task_adapters as load_plugin_task_adapters_impl,
    task_adapter_registry,
)
from ..task_adapters.registry import RegisteredTaskAdapter
from ..tools import Tool, ToolRuntime, load_builtin_tools, tool_registry
from ..tools.registry import RegisteredTool
from ..infra.params import merge_parameter_values
from .config import (
    LoadedConfigs,
    BodiesConfig,
    BodyConfig,
    EnvironmentsConfig,
    EnvironmentConfig,
    ModuleConfig,
    ModulesConfig,
    MonitorConfig,
    MonitorsConfig,
    ObservationCacheConfig,
    SafetyStateConfig,
    TaskAdapterConfig,
    TaskAdaptersConfig,
    ToolConfig,
    ToolsConfig,
    load_application_config,
)
from .registry import RegisteredModule, load_builtin_components, module_registry


async def _close_resource(resource: Any) -> Exception | None:
    """Close one optional resident resource and return, not raise, failures."""

    if resource is None:
        return None
    closer = getattr(resource, "close", None)
    if not callable(closer):
        return None
    try:
        result = closer()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        return exc
    return None


@dataclass(slots=True)
class ApplicationRuntime:
    """Resident resources created from one master/experiment config pair."""

    params: Any
    tools: ToolRuntime | None
    modules: ModuleRuntime | None
    graphs: Any
    safety_state: SafetyStore | None = None
    body: Any = None
    monitors: MonitorRuntime | None = None
    observation_cache: Any = None
    observation_pipeline: Any = None
    observation_resolver: Any = None
    observation_previous: Any = None
    owns_tools: bool = True
    context_store: Any = None
    owns_safety_state: bool = False
    _closed: bool = False

    async def close(self) -> None:
        """Close resident Body, monitors, modules and tools safely."""

        if self._closed:
            return
        self._closed = True
        first_error: Exception | None = None
        for resource in (self.body, self.monitors, self.modules, self.tools if self.owns_tools else None,
                         self.context_store, self.safety_state if self.owns_safety_state else None):
            error = await _close_resource(resource)
            if error is not None and first_error is None:
                first_error = error
        if self.observation_cache is not None:
            try:
                await self.observation_cache.close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
            try:
                from ..infra.observation_runtime import (
                    clear_observation_infrastructure,
                    configure_observation_infrastructure,
                    get_observation_cache,
                )

                # Do not restore an older cache over a newer application that
                # was configured after this runtime.  This mirrors the
                # identity-checked environment singleton restoration.
                if get_observation_cache() is self.observation_cache:
                    clear_observation_infrastructure(self.observation_cache)
                    if self.observation_previous is not None:
                        configure_observation_infrastructure(self.observation_previous)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


def load_plugin_modules(modules: list[str] | tuple[str, ...]) -> None:
    for name in modules:
        if not name or not name.strip():
            raise ValueError("plugin_modules cannot contain an empty module name")
        importlib.import_module(name)


def load_plugin_tools(tools: list[str] | tuple[str, ...]) -> None:
    """Import modules that register custom resident Tools."""

    for name in tools:
        if not name or not name.strip():
            raise ValueError("plugin_tools cannot contain an empty tool name")
        importlib.import_module(name)


def load_plugin_bodies(bodies: list[str] | tuple[str, ...]) -> None:
    """Import modules that register custom resident Body providers."""

    for name in bodies:
        if not name or not name.strip():
            raise ValueError("plugin_bodies cannot contain an empty module name")
        importlib.import_module(name)


def load_plugin_environments(environments: list[str] | tuple[str, ...]) -> None:
    """Import modules that register custom EnvironmentAdapter providers."""

    for name in environments:
        if not name or not name.strip():
            raise ValueError(
                "plugin_environments cannot contain an empty module name"
            )
        importlib.import_module(name)


def load_plugin_monitors(monitors: list[str] | tuple[str, ...]) -> None:
    """Import modules that register custom resident/request monitors."""

    for name in monitors:
        if not name or not name.strip():
            raise ValueError("plugin_monitors cannot contain an empty module name")
        importlib.import_module(name)


def load_plugin_task_adapters(adapters: list[str] | tuple[str, ...]) -> None:
    """Import modules that register custom Task Adapt providers."""

    load_plugin_task_adapters_impl(adapters)


def _check_version(spec: ModuleConfig, registered: RegisteredModule[Any]) -> None:
    if spec.version is not None and spec.version != registered.version:
        raise ValueError(
            f"module {spec.name!r} requires version {spec.version!r}, but registry provides {registered.version!r}"
        )
    if spec.kind is not None and spec.kind != registered.kind:
        raise ValueError(
            f"module {spec.name!r} declares kind {spec.kind!r}, but registry provides {registered.kind!r}"
        )


def _check_tool_version(spec: ToolConfig, registered: RegisteredTool[Any]) -> None:
    if spec.version is not None and spec.version != registered.version:
        raise ValueError(
            f"tool {spec.name!r} requires version {spec.version!r}, but registry provides {registered.version!r}"
        )
    if spec.kind is not None and spec.kind != registered.kind:
        raise ValueError(
            f"tool {spec.name!r} declares kind {spec.kind!r}, but registry provides {registered.kind!r}"
        )


def _check_body_version(spec: BodyConfig, registered: RegisteredBody[Any]) -> None:
    if spec.version is not None and spec.version != registered.version:
        raise ValueError(
            f"body {spec.name!r} requires version {spec.version!r}, but registry provides {registered.version!r}"
        )
    if spec.kind is not None and spec.kind != registered.kind:
        raise ValueError(
            f"body {spec.name!r} declares kind {spec.kind!r}, but registry provides {registered.kind!r}"
        )


def _select_single_configured(
    items: list[Any],
    *,
    label: str,
    selected_id: str | None = None,
) -> Any:
    """Select one enabled service manifest and reject ambiguous startup."""

    enabled = [item for item in items if getattr(item, "enabled", True)]
    if selected_id is not None:
        enabled = [
            item
            for item in enabled
            if getattr(item, "id", None) == selected_id
            or getattr(item, "name", None) == selected_id
        ]
    if not enabled:
        selector = f" {selected_id!r}" if selected_id is not None else ""
        raise ValueError(f"no enabled {label} configuration{selector}")
    if len(enabled) > 1:
        names = ", ".join(
            str(getattr(item, "id", None) or getattr(item, "name", ""))
            for item in enabled
        )
        raise ValueError(
            f"multiple enabled {label} configurations ({names}); "
            f"select one explicitly"
        )
    return enabled[0]


def build_body_service(
    config: BodiesConfig | BodyConfig | str | Path | Mapping[str, Any],
    *,
    body_id: str | None = None,
    runtime: Mapping[str, Any] | None = None,
    safety_state: SafetyStore | None = None,
    executor: Any = None,
    stop_executor: Any = None,
) -> Any:
    """Build the one configured resident Body service.

    JSON ``params`` are constructor configuration.  ``runtime`` contains
    process-owned dependencies that cannot be represented in JSON, such as a
    Python action driver or a shared safety store.  The registry rejects key
    collisions between the two sources.
    """

    if isinstance(config, BodyConfig):
        spec = config
        plugin_bodies: list[str] = []
    else:
        if not isinstance(config, BodiesConfig):
            from .config import load_bodies_config

            config = load_bodies_config(config)
        plugin_bodies = list(config.plugin_bodies)
        spec = _select_single_configured(
            list(config.bodies), label="body", selected_id=body_id
        )
    load_builtin_bodies()
    load_plugin_bodies(plugin_bodies)
    registered = body_registry.get(spec.name)
    _check_body_version(spec, registered)
    runtime_values = dict(runtime or {})
    if safety_state is not None:
        runtime_values.setdefault("safety_state", safety_state)
    if executor is not None:
        runtime_values.setdefault("executor", executor)
    if stop_executor is not None:
        runtime_values.setdefault("stop_executor", stop_executor)
    instance = body_registry.build(spec.name, spec.params, **runtime_values)
    required = ("start", "execute", "interrupt", "health", "close")
    missing = [name for name in required if not callable(getattr(instance, name, None))]
    if missing:
        raise TypeError(
            f"body {spec.name!r} must implement BodyService methods: "
            + ", ".join(missing)
        )
    return instance


async def initialize_body_service(
    config: BodiesConfig | BodyConfig | str | Path | Mapping[str, Any],
    *,
    body_id: str | None = None,
    runtime: Mapping[str, Any] | None = None,
    safety_state: SafetyStore | None = None,
    executor: Any = None,
    stop_executor: Any = None,
) -> Any:
    """Build and start a registered Body service exactly once."""

    service = build_body_service(
        config,
        body_id=body_id,
        runtime=runtime,
        safety_state=safety_state,
        executor=executor,
        stop_executor=stop_executor,
    )
    try:
        result = service.start()
        if inspect.isawaitable(result):
            await result
    except BaseException:
        close = getattr(service, "close", None)
        if callable(close):
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass
        raise
    return service


def build_environment_adapter(
    config: EnvironmentsConfig | EnvironmentConfig | str | Path | Mapping[str, Any],
    *,
    environment_id: str | None = None,
) -> Any:
    """Resolve one resident EnvironmentAdapter from the env registry.

    Adapter construction is cached by ``environment_registry``.  This helper
    only selects/configures the adapter; a per-run ``SessionSpec`` is still
    required to create a session with ``adapter.open(spec)``.
    """

    if isinstance(config, EnvironmentConfig):
        spec = config
        plugin_environments: list[str] = []
    else:
        if not isinstance(config, EnvironmentsConfig):
            from .config import load_environments_config

            config = load_environments_config(config)
        plugin_environments = list(config.plugin_environments)
        spec = _select_single_configured(
            list(config.environments),
            label="environment",
            selected_id=environment_id,
        )
    load_builtin_environments()
    load_plugin_environments(plugin_environments)
    adapter = environment_registry.build(spec.name, spec.params)
    descriptor = getattr(adapter, "descriptor", None)
    if spec.version is not None and descriptor is not None:
        actual = getattr(descriptor, "env_version", None)
        if actual != spec.version:
            raise ValueError(
                f"environment {spec.name!r} requires version {spec.version!r}, "
                f"but adapter provides {actual!r}"
            )
    return adapter


async def initialize_environment_adapter(
    config: EnvironmentsConfig | EnvironmentConfig | str | Path | Mapping[str, Any],
    *,
    environment_id: str | None = None,
    connect: bool = True,
) -> Any:
    """Resolve a registered EnvironmentAdapter and optionally connect it."""

    adapter = build_environment_adapter(config, environment_id=environment_id)
    if connect:
        try:
            result = adapter.connect()
            if inspect.isawaitable(result):
                await result
        except BaseException:
            # A failed connect must not leave a partially initialized adapter
            # in a caller-owned service.  The registry may still cache the
            # instance; closing here makes the failure deterministic and lets
            # a later explicit retry decide how to recreate it.
            closer = getattr(adapter, "close", None)
            if callable(closer):
                try:
                    result = closer()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    pass
            raise
    return adapter


def _check_task_adapter_version(
    spec: TaskAdapterConfig,
    registered: RegisteredTaskAdapter[Any],
) -> None:
    if spec.version is not None and spec.version != registered.version:
        raise ValueError(
            f"task adapter {spec.name!r} requires version {spec.version!r}, "
            f"but registry provides {registered.version!r}"
        )
    if spec.kind is not None and spec.kind != registered.kind:
        raise ValueError(
            f"task adapter {spec.name!r} declares kind {spec.kind!r}, "
            f"but registry provides {registered.kind!r}"
        )


def build_task_adapters(
    config: TaskAdaptersConfig | TaskAdapterConfig | str | Path | Mapping[str, Any],
    *,
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[ConfiguredTaskAdapter]:
    """Build enabled Task Adapt providers in manifest order.

    Unlike resident providers, Task Adapt instances are scoped to one
    experiment submission.  The returned list intentionally preserves config
    order because that is the execution order for multi-adapter experiments.
    ``overrides`` is used by the compatibility CLI and by trusted submission
    callers; ordinary web submissions should put values in the config itself.
    """

    if isinstance(config, TaskAdapterConfig):
        config = TaskAdaptersConfig(task_adapters=[config])
    elif not isinstance(config, TaskAdaptersConfig):
        from .config import load_task_adapters_config

        config = load_task_adapters_config(config)
    load_builtin_task_adapters()
    load_plugin_task_adapters_impl(config.plugin_task_adapters)
    built: list[ConfiguredTaskAdapter] = []
    seen_ids: set[str] = set()
    override_values = overrides or {}
    for spec in config.task_adapters:
        if not spec.enabled:
            continue
        instance_id = spec.id or spec.name
        if instance_id in seen_ids:
            raise ValueError(f"duplicate configured task adapter id: {instance_id}")
        registered = task_adapter_registry.get(spec.name)
        _check_task_adapter_version(spec, registered)
        values = dict(spec.params)
        selected = override_values.get(instance_id, override_values.get(spec.name, {}))
        if selected:
            if not isinstance(selected, Mapping):
                raise TypeError(f"task adapter override for {instance_id!r} must be an object")
            values = merge_parameter_values(values, selected)
        instance = task_adapter_registry.build(spec.name, values)
        built.append(
            ConfiguredTaskAdapter(
                instance_id=instance_id,
                config=TaskAdapterConfig.model_validate({**spec.model_dump(), "params": values}),
                adapter=instance,
            )
        )
        seen_ids.add(instance_id)
    if not built:
        raise ValueError("no enabled task adapter configuration")
    return built


# ``BodyRuntime`` is intentionally not a second container: the Body service
# itself is the resident runtime boundary.  Keep these aliases for callers
# that use the same build/initialize vocabulary as ModuleRuntime/ToolRuntime.
build_body_runtime = build_body_service
initialize_body_runtime = initialize_body_service


def _check_monitor_version(
    spec: MonitorConfig,
    registered: RegisteredMonitor[Any],
) -> None:
    if spec.version is not None and spec.version != registered.version:
        raise ValueError(
            f"monitor {spec.name!r} requires version {spec.version!r}, but registry provides {registered.version!r}"
        )
    if spec.kind is not None and spec.kind != registered.kind:
        raise ValueError(
            f"monitor {spec.name!r} declares kind {spec.kind!r}, but registry provides {registered.kind!r}"
        )

def build_tool_runtime(
    config: ToolsConfig | str | Path | Mapping[str, Any],
    *,
    observation_resolver: Any = None,
    deployment_root: str | Path | None = None,
    runtime_params: Any = None,
) -> ToolRuntime:
    """Construct every enabled Tool exactly once from its manifest."""

    if not isinstance(config, ToolsConfig):
        from .config import load_tools_config

        config = load_tools_config(config)
    load_builtin_tools()
    load_plugin_tools(config.plugin_tools)
    instances: dict[str, Tool] = {}
    aliases: dict[str, str] = {}
    metadata: dict[str, dict[str, Any]] = {}
    seen_names: set[str] = set()
    for spec in config.tools:
        if not spec.enabled:
            continue
        instance_id = spec.id or spec.name
        if instance_id in instances:
            raise ValueError(f"duplicate configured tool id: {instance_id}")
        registered = tool_registry.get(spec.name)
        _check_tool_version(spec, registered)
        values = dict(spec.params)
        if spec.parameter_bindings and runtime_params is None:
            raise ValueError(f"tool {instance_id!r} requires runtime_params for parameter_bindings")
        for argument, path in spec.parameter_bindings.items():
            value = runtime_params.resolve(path)
            if value is not None:
                values[argument] = value
        instance = registered.factory(**values)
        if not callable(getattr(instance, "call", None)) and not callable(
            getattr(instance, "invoke", None)
        ) and not callable(getattr(instance, "run", None)):
            raise TypeError(
                f"tool {spec.name!r} must implement call(arguments, context), invoke() or run(ctx)"
            )
        if spec.deployment is not None:
            if deployment_root is None:
                raise ValueError("managed tools require a Worker deployment_root")
            from .model_services import DeployedTool
            directory = Path(deployment_root) / hashlib.sha256(instance_id.encode()).hexdigest()[:12]
            instance = DeployedTool(instance, spec.deployment, directory, tool_id=instance_id)
        instances[instance_id] = instance
        if spec.name not in seen_names:
            aliases[spec.name] = instance_id
            seen_names.add(spec.name)
        else:
            aliases.pop(spec.name, None)
        metadata[instance_id] = {
            "name": spec.name,
            "version": registered.version,
            "kind": spec.kind or registered.kind,
            "role": spec.role,
            "config_hash": hashlib.sha256(
                json.dumps(spec.model_dump(mode="json"), sort_keys=True).encode()
            ).hexdigest(),
        }
    return ToolRuntime(
        instances,
        aliases=aliases,
        metadata=metadata,
        observation_resolver=observation_resolver,
    )


def build_observation_cache(
    config: ObservationCacheConfig | Mapping[str, Any] | None = None,
    *,
    metrics: Any = None,
    base_dir: str | Path | None = None,
) -> Any:
    """Construct the resident observation cache from merged config values.

    Relative storage paths are interpreted relative to the master config when
    bootstrap knows that directory.  This keeps a CLI started from another
    working directory from writing observations into an unrelated ``./data``.
    """

    from ..storage.registry import build_store

    if config is None:
        config = ObservationCacheConfig()
    if not isinstance(config, ObservationCacheConfig):
        if isinstance(config, Mapping) and isinstance(config.get("observation_cache"), Mapping):
            from .config import load_observation_cache_config

            config = load_observation_cache_config(config)
        else:
            config = ObservationCacheConfig.model_validate(config)
    if not config.enabled:
        return None
    return build_store("observation", config, base_dir=base_dir, metrics=metrics)



def build_observation_pipeline(cache: Any) -> Any:
    """Create the normaliser/resolver pair around one resident cache."""

    if cache is None:
        return None
    from ..infra.observation_pipeline import ObservationPipeline

    return ObservationPipeline(cache)


def build_safety_state(
    config: SafetyStateConfig | Mapping[str, Any] | None = None,
    *,
    base_dir: str | Path | None = None,
) -> SafetyStore:
    """Build the configured safety store once during application bootstrap.

    The default global facade remains available for backwards compatibility,
    but application construction should call this helper and inject the
    resulting store into every Environment/Monitor/Workflow/Body boundary.
    Relative SQLite paths are resolved against the master config directory
    when it is known, rather than against whichever process happens to start
    first.
    """

    if config is None:
        return GLOBAL_SAFETY_STATE  # type: ignore[return-value]
    if not isinstance(config, SafetyStateConfig):
        config = SafetyStateConfig.model_validate(config)
    from ..storage.registry import build_store
    return build_store("safety", config, base_dir=base_dir)



async def initialize_tool_runtime(
    config: ToolsConfig | str | Path | Mapping[str, Any],
    *,
    observation_resolver: Any = None,
    deployment_root: str | Path | None = None,
    runtime_params: Any = None,
    shared_model_endpoints: Mapping[str, str] | None = None,
) -> ToolRuntime:
    """Construct and start the resident ToolRuntime."""

    runtime = build_tool_runtime(config, observation_resolver=observation_resolver, deployment_root=deployment_root, runtime_params=runtime_params)
    if shared_model_endpoints:
        for tool in runtime.values():
            hook = getattr(tool, 'bind_shared_services', None)
            if hook is not None:
                hook(shared_model_endpoints)
    await runtime.start()
    return runtime


def build_monitor_runtime(
    config: MonitorsConfig | str | Path | Mapping[str, Any],
    *,
    environment_service: Any = None,
    observation_resolver: Any = None,
    safety_state: SafetyStore | None = None,
    params: Any = None,
) -> MonitorRuntime:
    """Construct configured monitor instances exactly once.

    ``modes`` is a per-instance set.  Consequently one registry provider can
    be configured as both resident and request-driven without creating two
    model instances or two independent caches.
    """

    if not isinstance(config, MonitorsConfig):
        from .config import load_monitors_config

        config = load_monitors_config(config)
    load_builtin_monitors()
    load_plugin_monitors(config.plugin_monitors)
    instances: dict[str, Any] = {}
    modes: dict[str, set[str]] = {}
    aliases: dict[str, str] = {}
    metadata: dict[str, dict[str, Any]] = {}
    poll_hz_by_id: dict[str, float | None] = dict(config.poll_hz_by_id)
    on_error_level_by_id = dict(config.on_error_level_by_id)
    seen_names: set[str] = set()
    for spec in config.monitors:
        if not spec.enabled:
            continue
        instance_id = spec.id or spec.name
        if instance_id in instances:
            raise ValueError(f"duplicate configured monitor id: {instance_id}")
        registered = monitor_registry.get(spec.name)
        _check_monitor_version(spec, registered)
        instance = registered.factory(**dict(spec.params))
        if (
            not callable(getattr(instance, "check", None))
            and not callable(getattr(instance, "evaluate", None))
            and not callable(instance)
        ):
            raise TypeError(
                f"monitor {spec.name!r} must implement check(feedback, request, context), evaluate() or __call__()"
            )
        instances[instance_id] = instance
        modes[instance_id] = set(spec.modes)
        if spec.poll_hz is not None:
            poll_hz_by_id[instance_id] = spec.poll_hz
        if spec.on_error_level is not None:
            on_error_level_by_id[instance_id] = spec.on_error_level
        if spec.name not in seen_names:
            aliases[spec.name] = instance_id
            seen_names.add(spec.name)
        else:
            aliases.pop(spec.name, None)
        metadata[instance_id] = {
            "name": spec.name,
            "version": registered.version,
            "kind": spec.kind or registered.kind,
            "description": registered.description,
            "modes": list(spec.modes),
            "config_hash": hashlib.sha256(
                json.dumps(spec.model_dump(mode="json"), sort_keys=True).encode()
            ).hexdigest(),
        }
    return MonitorRuntime(
        instances,
        modes=modes,
        aliases=aliases,
        metadata=metadata,
        environment_service=environment_service,
        observation_resolver=observation_resolver,
        safety_state=(
            safety_state if safety_state is not None else GLOBAL_SAFETY_STATE
        ),
        params=params,
        poll_hz=config.poll_hz,
        poll_hz_by_id=poll_hz_by_id,
        on_error_level=config.on_error_level,
        on_error_level_by_id=on_error_level_by_id,
    )


async def initialize_monitor_runtime(
    config: MonitorsConfig | str | Path | Mapping[str, Any],
    *,
    environment_service: Any = None,
    observation_resolver: Any = None,
    safety_state: SafetyStore | None = None,
    params: Any = None,
) -> MonitorRuntime:
    """Construct and start the resident/request monitor runtime."""

    runtime = build_monitor_runtime(
        config,
        environment_service=environment_service,
        observation_resolver=observation_resolver,
        safety_state=safety_state,
        params=params,
    )
    await runtime.start()
    return runtime


def build_module_runtime(
    config: ModulesConfig | str | Path | Mapping[str, Any],
    *,
    tools: ToolRuntime | None = None,
    params: Any = None,
) -> ModuleRuntime:
    """Construct every enabled module exactly once."""
    if not isinstance(config, ModulesConfig):
        from .config import load_modules_config

        config = load_modules_config(config)
    load_builtin_components()
    load_plugin_modules(config.plugin_modules)
    instances: dict[str, Module] = {}
    aliases: dict[str, str] = {}
    metadata: dict[str, dict[str, Any]] = {}
    seen_names: set[str] = set()
    for spec in config.modules:
        if not spec.enabled:
            continue
        instance_id = spec.id or spec.name
        if instance_id in instances:
            raise ValueError(f"duplicate configured module id: {instance_id}")
        registered = module_registry.get(spec.name)
        _check_version(spec, registered)
        instance = registered.factory(**dict(spec.params))
        if not callable(getattr(instance, "invoke", None)) and not callable(
            getattr(instance, "run", None)
        ):
            raise TypeError(
                f"module {spec.name!r} must implement invoke(arguments, context) or run(ctx)"
            )
        instances[instance_id] = instance
        if spec.name not in seen_names:
            aliases[spec.name] = instance_id
            seen_names.add(spec.name)
        else:
            aliases.pop(spec.name, None)
        metadata[instance_id] = {
            "name": spec.name,
            "version": registered.version,
            "kind": spec.kind or registered.kind,
            "config_hash": hashlib.sha256(
                json.dumps(spec.model_dump(mode="json"), sort_keys=True).encode()
            ).hexdigest(),
        }
    return ModuleRuntime(
        instances,
        aliases=aliases,
        metadata=metadata,
        tools=tools,
        params=params,
    )


async def initialize_module_runtime(
    config: ModulesConfig | str | Path | Mapping[str, Any],
    *,
    tools: ToolRuntime | None = None,
    params: Any = None,
) -> ModuleRuntime:
    # Tools own model weights, pools and stores that module ``start`` hooks
    # may need.  Start them first, but keep lifecycle ownership with the
    # caller so a resident ToolRuntime can outlive one ModuleRuntime.
    if not isinstance(config, ModulesConfig):
        from .config import load_modules_config

        config = load_modules_config(config)
    if tools is not None:
        await tools.start()
    runtime = build_module_runtime(config, tools=tools, params=params)
    await runtime.start()
    return runtime


async def initialize_resident_runtimes(
    modules_config: ModulesConfig | str | Path,
    tools_config: ToolsConfig | str | Path,
    *,
    runtime_params: Any = None,
) -> tuple[ToolRuntime, ModuleRuntime]:
    """Start Tools first and Modules second as one bootstrap transaction.

    The returned tuple is ordered ``(tools, modules)`` to make the ownership
    and shutdown order explicit.  If module startup fails, already-started
    modules and tools are closed before the original exception is re-raised.
    """

    if isinstance(modules_config, (str, Path)):
        from .config import load_modules_config

        modules_config = load_modules_config(modules_config)
    tools = await initialize_tool_runtime(tools_config, runtime_params=runtime_params)
    try:
        modules = await initialize_module_runtime(modules_config, tools=tools)
    except BaseException:
        try:
            await tools.close()
        except Exception:
            pass
        raise
    return tools, modules


async def initialize_application(
    config: str | Path | Mapping[str, Any],
    experiment_config: str | Path | Mapping[str, Any] | None = None,
    *,
    environment_service: Any = None,
    body_service: Any = None,
    body_id: str | None = None,
    body_runtime: Mapping[str, Any] | None = None,
    start_body: bool = True,
    safety_state: SafetyStore | None = None,
    start_monitors: bool | None = None,
    metrics: Any = None,
    base_dir: str | Path | None = None,
    tools: ToolRuntime | None = None,
    resource_root: str | Path | None = None,
    loaded: LoadedConfigs | None = None,
) -> ApplicationRuntime:
    """Load every config through Parameter and start all resident resources.

    This is the preferred entry point for a new application. It returns the
    single merged ``params`` view together with typed graph/module/tool/monitor
    manifests and their resident runtimes.  Pass ``environment_service`` when
    resident monitors should begin polling immediately.  If ``body_service``
    is omitted and the body manifest contains one enabled item, the selected
    registered Body is built and started here; Python-only dependencies such
    as an action ``executor`` belong in ``body_runtime``.
    """

    config_base_dir = (
        Path(base_dir)
        if base_dir is not None
        else Path(config).parent
        if isinstance(config, (str, Path))
        else None
    )
    # Task-specific resource paths must never mutate the resident snapshot.
    loaded = deepcopy(loaded) if loaded is not None else load_application_config(
        config, experiment_config, base_dir=config_base_dir
    )
    # Resolve the safety backend before starting any resident component.  All
    # services built by this application receive the same concrete instance.
    main_source = loaded.bundle.sources.get("main")
    config_base_dir = (
        Path(main_source).parent
        if main_source and not str(main_source).startswith("<")
        else config_base_dir
    )
    if resource_root is not None:
        resource_root = Path(resource_root).resolve()
        if loaded.safety_state.scope == "task":
            loaded.safety_state.path = str(resource_root / "safety.sqlite3")
        loaded.observation_cache.storage.root = str(resource_root / "observations")
        loaded.observation_cache.storage.index = str(resource_root / "observations" / "index.sqlite3")
    app = ApplicationRuntime(
        params=loaded.params, tools=tools, modules=None, graphs=loaded.graphs,
        body=body_service, owns_tools=tools is None,
        safety_state=safety_state, owns_safety_state=safety_state is None,
    )
    try:
        if app.safety_state is None:
            app.safety_state = build_safety_state(loaded.safety_state, base_dir=config_base_dir)
        # Assign each owned resource before starting it, so partial startup
        # uses the same cleanup path as normal shutdown.
        app.observation_cache = build_observation_cache(
            loaded.observation_cache, metrics=metrics, base_dir=config_base_dir,
        )
        app.observation_pipeline = build_observation_pipeline(app.observation_cache)
        app.observation_resolver = getattr(app.observation_pipeline, "resolver", None)
        if app.observation_cache is not None:
            from ..infra.observation_runtime import configure_observation_infrastructure
            app.observation_previous = configure_observation_infrastructure(
                app.observation_cache, pipeline=app.observation_pipeline,
                resolver=app.observation_resolver,
            )
            await app.observation_cache.start()

        body_configured = any(spec.enabled for spec in loaded.bodies.bodies)
        if app.body is None and body_configured:
            app.body = build_body_service(
                loaded.bodies, body_id=body_id, runtime=body_runtime,
                safety_state=app.safety_state,
            )
            result = app.body.start()
            if inspect.isawaitable(result):
                await result
        elif app.body is not None and start_body:
            starter = getattr(app.body, "start", None)
            if callable(starter):
                result = starter()
                if inspect.isawaitable(result):
                    await result

        if app.tools is None:
            app.tools = build_tool_runtime(
                loaded.tools, observation_resolver=app.observation_resolver,
                runtime_params=loaded.params,
            )
        else:
            app.tools.set_observation_resolver(app.observation_resolver)
        await app.tools.start()
        app.modules = build_module_runtime(loaded.modules, tools=app.tools, params=loaded.params)
        await app.modules.start()

        monitor_environment = environment_service
        if app.observation_pipeline is not None and environment_service is not None:
            from ..services.environment import ObservationEnvironmentService
            if getattr(environment_service, "observation_pipeline", None) is None:
                monitor_environment = ObservationEnvironmentService(environment_service, app.observation_pipeline)
        app.monitors = build_monitor_runtime(
            loaded.monitors, environment_service=monitor_environment,
            observation_resolver=app.observation_resolver, safety_state=app.safety_state,
            params=loaded.params,
        )
        resident_configured = any(
            "resident" in spec.modes for spec in loaded.monitors.monitors if spec.enabled
        )
        from ..envs.runtime import environment
        environment_available = environment_service is not None or environment.current is not None
        should_start = start_monitors if start_monitors is not None else (
            environment_available or not resident_configured
        )
        if should_start:
            await app.monitors.start()
        store_config = loaded.params.resolve("runtime.context_store")
        if store_config is not None:
            from ..storage.registry import build_store
            app.context_store = build_store("context", store_config, base_dir=config_base_dir)
        return app
    except BaseException:
        try:
            await app.close()
        except Exception:
            pass
        raise


def build_graph_runtime(
    graph_config,
    runtime: ModuleRuntime,
    *,
    backend: str = "langgraph",
    safety_state=None,
    stop_on_unhandled_safety: bool = True,
    context_store=None,
    tools: ToolRuntime | None = None,
    params=None,
    metrics=None,
    trace=None,
):
    """Load a JSON graph manifest and bind it to the already resident modules."""
    from ..workflow import GraphRuntime, load_graphs_config

    config = (
        load_graphs_config(graph_config)
        if isinstance(graph_config, (str, Path))
        else graph_config
    )
    graphs = config.graph_map() if hasattr(config, "graph_map") else config
    kwargs = {
        "backend": backend,
        "stop_on_unhandled_safety": stop_on_unhandled_safety,
        "context_store": context_store,
    }
    if safety_state is not None:
        kwargs["safety_state"] = safety_state
    return GraphRuntime(
        graphs,
        runtime,
        tools=tools,
        params=params,
        metrics=metrics,
        trace=trace,
        **kwargs,
    )


__all__ = [
    "ApplicationRuntime",
    "build_body_service",
    "build_body_runtime",
    "build_environment_adapter",
    "build_graph_runtime",
    "build_module_runtime",
    "build_monitor_runtime",
    "build_observation_cache",
    "build_observation_pipeline",
    "build_tool_runtime",
    "initialize_application",
    "initialize_body_service",
    "initialize_body_runtime",
    "initialize_environment_adapter",
    "initialize_module_runtime",
    "initialize_monitor_runtime",
    "initialize_resident_runtimes",
    "initialize_tool_runtime",
    "load_plugin_modules",
    "load_plugin_bodies",
    "load_plugin_environments",
    "load_plugin_monitors",
    "load_plugin_tools",
]
