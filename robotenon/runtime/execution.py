"""Task execution contracts shared by in-process and persistent-process backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ExecutionOptions:
    backend: str = "in_process"
    max_workers: int = 1
    gpu_ids: tuple[str, ...] = ()
    egl_device_ids: tuple[str, ...] = ()
    resource_ids: tuple[str, ...] = ()
    max_retries: int = 0
    startup_timeout_s: float = 120
    task_timeout_s: float = 3600
    shutdown_timeout_s: float = 10
    recycle_after_tasks: int = 50
    memory_mode: str = "snapshot"
    worker_threads: int = 2
    # Bound concurrent task-adapter enumeration before Workers are launched.
    enumeration_parallelism: int = 1
    # Bound native Worker initialization without limiting steady-state work.
    # Zero preserves the historical behavior of starting up to max_workers.
    worker_startup_parallelism: int = 0
    # Host-wide gate held from before Worker/plugin imports through the first
    # native reset and observation. Zero disables the gate.
    native_reset_parallelism: int = 0
    native_reset_gate: str | None = None
    render_retry_backend: str | None = None
    plugin_backends: tuple[str, ...] = ()
    backend_params: dict[str, Any] = field(default_factory=dict)
    # Per-worker logical or physical device mapping, allowing shared devices.
    worker_devices: tuple[str, ...] = ()
    virtual_gpu_map: dict[str, str] = field(default_factory=dict)
    batch_services: tuple[dict[str, Any], ...] = ()
    # Service endpoints owned by another Python environment.  Each service
    # accepts either one endpoint shared by every Worker or one per Worker.
    external_batch_services: dict[str, tuple[str, ...]] = field(default_factory=dict)
    worker_contexts: tuple[str, ...] = ()

    @property
    def capabilities(self):
        from .execution_registry import execution_provider

        return execution_provider(self).capabilities

    @classmethod
    def from_params(cls, params):
        values = dict(params.resolve("runtime.execution") or {})
        for key in ("gpu_ids", "egl_device_ids", "resource_ids", "worker_devices"):
            if key in values:
                values[key] = tuple(str(x).strip() for x in values[key])
        if "external_batch_services" in values:
            values["external_batch_services"] = {
                str(name): tuple(str(endpoint) for endpoint in endpoints)
                for name, endpoints in values["external_batch_services"].items()
            }
        from .devices import validate_gpu_map, physical_device
        values["virtual_gpu_map"] = validate_gpu_map(values.get("virtual_gpu_map", {}))
        result = cls(**values)
        for device in (*result.gpu_ids, *result.worker_devices,
                       *(d for c in result.batch_services for d in c.get("devices", []))):
            physical_device(device, result.virtual_gpu_map)
        if (
            result.worker_contexts or result.worker_devices
        ) and "managed_workers" not in result.capabilities:
            raise ValueError(
                "worker_contexts and worker_devices require a managed_workers execution backend"
            )
        if result.worker_devices:
            if result.gpu_ids or len(result.worker_devices) != result.max_workers:
                raise ValueError(
                    "worker_devices requires one device per worker and cannot mix with gpu_ids"
                )
            if any(not d or "," in d for d in result.worker_devices):
                raise ValueError("worker_devices entries must identify one GPU")
        if result.batch_services:
            if "managed_workers" not in result.capabilities:
                raise ValueError("run-owned batch_services require a managed_workers execution backend")
            from .batch_services import BatchServiceConfig

            configs = [BatchServiceConfig.parse(c) for c in result.batch_services]
            if len({c.name for c in configs}) != len(configs):
                raise ValueError("batch service names must be unique")
        if result.external_batch_services:
            if "managed_workers" not in result.capabilities:
                raise ValueError("external_batch_services require a managed_workers execution backend")
            owned = {str(c.get("name")) for c in result.batch_services}
            overlap = owned.intersection(result.external_batch_services)
            if overlap:
                raise ValueError(
                    f"batch services cannot be both owned and external: {sorted(overlap)}"
                )
            for name, endpoints in result.external_batch_services.items():
                if not name or len(endpoints) not in {1, result.max_workers}:
                    raise ValueError(
                        "external_batch_services requires one endpoint or one per Worker"
                    )
                if any(not endpoint for endpoint in endpoints):
                    raise ValueError("external batch service endpoints cannot be empty")
        if result.render_retry_backend not in {None, "osmesa"}:
            raise ValueError("render_retry_backend must be null or osmesa")
        capabilities = result.capabilities
        if (
            result.max_workers < 1
            or result.max_retries < 0
            or result.recycle_after_tasks < 1
            or result.worker_threads < 1
            or result.enumeration_parallelism < 1
            or result.worker_startup_parallelism < 0
            or result.worker_startup_parallelism > result.max_workers
            or result.native_reset_parallelism < 0
            or result.native_reset_parallelism > result.max_workers
        ):
            raise ValueError("invalid worker/retry/recycle count")
        if result.native_reset_parallelism and not result.native_reset_gate:
            raise ValueError(
                "native_reset_gate is required when native_reset_parallelism is set"
            )
        if (
            min(
                result.startup_timeout_s,
                result.task_timeout_s,
                result.shutdown_timeout_s,
            )
            <= 0
        ):
            raise ValueError("execution timeouts must be positive")
        if "local_worker" in capabilities and result.max_workers != 1:
            raise ValueError("in_process execution requires max_workers=1")
        if any(not value or "," in value for value in result.gpu_ids):
            raise ValueError(
                "gpu_ids must contain individual non-empty device identifiers"
            )
        if result.gpu_ids and (
            len(result.gpu_ids) % result.max_workers != 0
            or len(set(result.gpu_ids)) != len(result.gpu_ids)
        ):
            raise ValueError("gpu_ids must be unique and divide evenly among workers")
        if result.virtual_gpu_map and result.gpu_ids and len(result.gpu_ids) != result.max_workers:
            raise ValueError("virtual GPU execution requires one logical GPU per Worker")
        if result.egl_device_ids and len(result.egl_device_ids) != result.max_workers:
            raise ValueError(
                "egl_device_ids requires one explicit EGL device per worker"
            )
        if result.resource_ids and len(set(result.resource_ids)) < result.max_workers:
            raise ValueError("each concurrent worker requires an exclusive resource")
        if result.memory_mode not in {"snapshot", "disabled", "sequential"}:
            raise ValueError("memory_mode must be snapshot, disabled or sequential")
        if result.max_workers > 1 and result.memory_mode == "sequential":
            raise ValueError("sequential memory requires max_workers=1")
        return result

    @property
    def resource_gpu_ids(self):
        model_devices = [
            str(d) for c in self.batch_services for d in c.get("devices", [])
        ]
        return tuple(
            dict.fromkeys(self.physical_device(d) for d in [*self.gpu_ids, *self.worker_devices, *model_devices])
        )

    def physical_device(self, device):
        from .devices import physical_device
        return physical_device(device, self.virtual_gpu_map)

    def worker_gpus(self, index):
        return tuple(dict.fromkeys(self.physical_device(d) for d in self.worker_logical_gpus(index)))

    def worker_virtual_gpu(self, index):
        return next((d for d in self.worker_logical_gpus(index)
                     if d in self.virtual_gpu_map), None)

    def worker_egl_device(self, index):
        if self.egl_device_ids:
            return self.egl_device_ids[index]
        physical = self.worker_gpus(index)
        if self.virtual_gpu_map and len(physical) == 1 and physical[0].isdigit():
            return physical[0]
        return None

    def worker_logical_gpus(self, index):
        if self.worker_devices:
            return (self.worker_devices[index],)
        if not self.gpu_ids:
            return ()
        width = len(self.gpu_ids) // self.max_workers
        return self.gpu_ids[index * width : (index + 1) * width]

    def validate_tools(self, tools):
        width = 1 if self.worker_devices else len(self.gpu_ids) // self.max_workers
        startup_budget = 0
        for spec in tools.tools:
            if not spec.enabled or spec.deployment is None:
                continue
            if not width or max(spec.deployment.gpu_ids) >= width:
                raise ValueError(
                    f"tool {spec.id or spec.name}: deployment GPU index exceeds "
                    f"the {width} GPUs assigned to each Worker; configure runtime.execution.gpu_ids"
                )
            from .model_services import service_command

            service_command(spec.deployment, "127.0.0.1", 0)
            startup_budget += spec.deployment.startup_timeout_s
        if (
            "managed_workers" in self.capabilities
            and startup_budget
            and startup_budget >= self.startup_timeout_s
        ):
            raise ValueError(
                "Worker startup_timeout_s must exceed the sum of tool deployment startup timeouts"
            )


class SequentialExecutionBackend:
    max_workers = 1

    async def execute(self, configured, case, context):
        return await configured.adapter.run_case(case, context)

    async def close(self):
        pass


class ResourceLease:
    """Host-local exclusive resource reservation, independent of provider type."""

    def __init__(self, resource_ids, *, share_with_children=False):
        self.resource_ids = sorted(set(resource_ids))
        self.handles = []
        self.share_with_children = share_with_children
        self._context_token = None

    def acquire(self):
        import fcntl
        import hashlib
        import os
        from pathlib import Path

        # Keep device leases shared with processes started before the project rename.
        directory = Path("/tmp") / f"robosys-resources-{os.getuid()}"
        directory.mkdir(mode=0o700, exist_ok=True)
        try:
            for resource in self.resource_ids:
                path = directory / hashlib.sha256(resource.encode()).hexdigest()
                handle = path.open("a+")
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                    raise RuntimeError(
                        f"resource already reserved: {resource}"
                    ) from None
                self.handles.append(handle)
            if self.share_with_children:
                from .processes import LEASE_FDS, resource_fds

                self._context_token = LEASE_FDS.set(
                    (*resource_fds(), *(h.fileno() for h in self.handles))
                )
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._context_token is not None:
            from .processes import LEASE_FDS

            LEASE_FDS.reset(self._context_token)
            self._context_token = None
        for handle in self.handles:
            handle.close()
        self.handles.clear()
