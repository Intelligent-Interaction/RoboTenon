"""Configurable persistence providers; callers depend on store protocols."""
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts.providers import ProviderRegistry

store_registries = {
    "safety": ProviderRegistry("safety store", ("snapshot", "update", "reconcile", "clear",
        "heartbeat", "clear_heartbeat", "recover", "observe_feedback", "raise_level")),
    "context": ProviderRegistry("context store", ("load", "save")),
    "actions": ProviderRegistry("action store", ("load", "save")),
    "observation": ProviderRegistry("observation store", ("start", "close", "append", "read_uri_bytes")),
}


@dataclass(frozen=True)
class StoreContext:
    config: dict[str, Any]
    base_dir: Path | None = None
    metrics: Any = None

    def path(self, value):
        path = Path(value)
        return self.base_dir / path if self.base_dir is not None and not path.is_absolute() else path


def register_store(kind, name, factory=None, *, capabilities=()):
    return store_registries[kind].register(name, factory, capabilities=capabilities)


def build_store(kind, config, *, base_dir=None, metrics=None):
    if not isinstance(config, Mapping):
        config = config.model_dump(mode="json")
    config = dict(config)
    registry = store_registries[kind]
    registry.load(config.get("plugin_stores", []))
    name = config["backend"]
    provider = registry.get(name)
    if config.get("require_shared") and "shared" not in provider.capabilities:
        raise ValueError(f"{kind}.require_shared=true but store {name!r} does not declare shared storage")
    return registry.build(name, params=config.get("params"), context=StoreContext(
        config, Path(base_dir) if base_dir is not None else None, metrics,
    ))


@register_store("safety", "memory")
def _memory_safety(*, context):
    from ..runtime.safety import SafetyStateStore
    return SafetyStateStore()


@register_store("safety", "sqlite", capabilities={"shared"})
def _sqlite_safety(*, context):
    from .safety import SQLiteSafetyStateStore
    return SQLiteSafetyStateStore(context.path(context.config["path"]))


@register_store("context", "sqlite", capabilities={"shared"})
def _sqlite_context(*, context):
    from .context import SQLiteContextStore
    return SQLiteContextStore(context.path(context.config["path"]))


@register_store("actions", "sqlite", capabilities={"shared"})
def _sqlite_actions(*, context):
    from .actions import SQLiteActionExecutionStore
    return SQLiteActionExecutionStore(context.path(context.config["path"]))


@register_store("observation", "hybrid")
def _hybrid_observation(*, context):
    from ..infra.observation_cache import ObservationCache
    config = context.config
    hot, persist, retention, storage = (config[key] for key in ("hot", "persist", "retention", "storage"))
    return ObservationCache(
        context.path(storage["root"]),
        index_path=context.path(storage["index"]) if storage.get("index") else None,
        max_records=hot["max_records"], max_bytes=hot["max_bytes"], ttl_seconds=hot["ttl_seconds"],
        queue_size=persist["queue_size"], workers=persist["workers"], on_queue_full=persist["on_queue_full"],
        async_persist=persist.get("async_enabled", persist.get("async", True)),
        keep_sessions=retention["keep_sessions"], keep_days=retention["keep_days"],
        uri_scheme=config["uri_scheme"], metrics=context.metrics,
    )


# Historical selectors used the same hybrid implementation. Retain aliases.
register_store("observation", "memory", _hybrid_observation)
register_store("observation", "filesystem", _hybrid_observation)


@register_store("observation", "remote", capabilities={"shared"})
def _remote_observation(*, context, socket_path, **options):
    from ..services.ipc import ObservationCacheServiceClient
    return ObservationCacheServiceClient(socket_path, **options)
