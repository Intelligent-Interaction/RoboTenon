"""Run-owned model processes. Each service replica has its own device binding."""

from __future__ import annotations

import asyncio
import json
import math
import multiprocessing
import os
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class BatchServiceConfig:
    name: str
    factory: str
    params: dict = field(default_factory=dict)
    devices: tuple[str, ...] = ()
    mode: str = "batch"
    max_batch_size: int = 8
    max_wait_ms: float = 5
    max_pending: int = 256

    @classmethod
    def parse(cls, values):
        config = cls(**values)
        if config.mode not in {"single", "batch"}:
            raise ValueError("batch service mode must be single or batch")
        if not config.name or ":" not in config.factory:
            raise ValueError("batch service requires a name and module:factory")
        if (
            any(
                isinstance(v, bool) or not isinstance(v, int) or v < 1
                for v in (config.max_batch_size, config.max_pending)
            )
            or not math.isfinite(config.max_wait_ms)
            or config.max_wait_ms < 0
            or config.max_pending < config.max_batch_size
        ):
            raise ValueError("invalid batch service limits")
        if not isinstance(config.devices, (list, tuple)):
            raise TypeError("model devices must be a list")
        if len(set(config.devices)) != len(config.devices):
            raise ValueError("duplicate model replica device")
        if any(not str(d).strip() or "," in str(d) for d in config.devices):
            raise ValueError("each model replica device must identify one GPU")
        return config


def _serve(config, socket_path, ready, stop, metrics_path, lease_fds=()):
    os.setsid()
    from .processes import register_process

    registry = Path(metrics_path).parent.parent / ".processes"
    os.environ["ROBOTENON_PROCESS_REGISTRY"] = str(registry)
    os.environ["ROBOTENON_RESOURCE_FDS"] = json.dumps(lease_fds)
    register_process(registry, os.getpid(), role="shared-model")
    from ..models import load_factory
    from .batch_transport import serve_connection
    from .batching import BatchEngine

    async def main():
        engine = BatchEngine(
            None,
            max_batch_size=config.max_batch_size if config.mode == "batch" else 1,
            max_wait_ms=config.max_wait_ms if config.mode == "batch" else 0,
            max_pending=config.max_pending,
        )
        await engine.create_provider(load_factory(config.factory), config.params)
        if not callable(getattr(engine.provider, "infer_batch", None)):
            await engine.close()
            raise TypeError("batch provider must implement infer_batch")
        clients = set()

        def connect(reader, writer):
            task = asyncio.create_task(serve_connection(engine, reader, writer))
            clients.add(task)

            def finished(task):
                clients.discard(task)
                if not task.cancelled() and task.exception():
                    traceback.print_exception(task.exception())

            task.add_done_callback(finished)

        server = await asyncio.start_unix_server(connect, path=socket_path)
        os.chmod(socket_path, 0o600)
        def snapshot(status):
            # Small aggregate only: no growing per-request history. Atomic
            # replacement lets progress readers inspect a running service.
            path = Path(metrics_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            value = {**engine.metrics, "status": status, "updated_at": time.time(),
                     "pending": len(engine.pending), "sessions": len(engine.sessions),
                     "physical_gpu_id": os.environ.get("CUDA_VISIBLE_DEVICES"),
                     "virtual_gpu_id": os.environ.get("ROBOTENON_VIRTUAL_GPU_ID") or None}
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(value, indent=2))
            temporary.replace(path)

        def safe_snapshot(status):
            # Metrics are diagnostic only. A transient/full metrics filesystem
            # must not terminate a healthy model service and strand simulators.
            try:
                snapshot(status)
            except OSError:
                traceback.print_exc()

        last_snapshot = time.monotonic()
        try:
            snapshot("running")
            ready.send(("ready", None))
            while not stop.is_set():
                await asyncio.sleep(0.1)
                if time.monotonic() - last_snapshot >= 5:
                    safe_snapshot("running")
                    last_snapshot = time.monotonic()
        finally:
            server.close()
            await server.wait_closed()
            for client in list(clients):
                client.cancel()
            await asyncio.gather(*clients, return_exceptions=True)
            await engine.close()
            safe_snapshot("closed")

    try:
        asyncio.run(main())
    except BaseException:
        error = traceback.format_exc()
        try:
            ready.send(("error", error))
        except (OSError, BrokenPipeError):
            pass
        raise
    finally:
        ready.close()
        for fd in lease_fds:
            os.close(fd)


class SharedBatchServices:
    def __init__(self, configs, root, *, startup_timeout_s=120, shutdown_timeout_s=10, virtual_gpu_map=None):
        from .devices import validate_gpu_map, physical_device
        self.virtual_gpu_map = validate_gpu_map({} if virtual_gpu_map is None else virtual_gpu_map)
        self.configs = [BatchServiceConfig.parse(c) for c in configs]
        for config in self.configs:
            for device in config.devices:
                physical_device(device, self.virtual_gpu_map)
        if len({c.name for c in self.configs}) != len(self.configs):
            raise ValueError("batch service names must be unique")
        self.root = Path(root)
        self.startup_timeout, self.shutdown_timeout = (
            startup_timeout_s,
            shutdown_timeout_s,
        )
        self.processes, self.endpoints = [], {}
        self.directory = None
        self.lock = asyncio.Lock()
        self.started = False

    def validate_worker_devices(self, devices):
        for device in devices:
            if device in self.virtual_gpu_map:
                for config in self.configs:
                    if device not in config.devices:
                        raise ValueError(f"service {config.name} has no replica for virtual GPU {device}")

    async def start(self):
        async with self.lock:
            if self.started:
                self.check_health()
                return
            from .devices import physical_device
            from .parallel import _worker_thread_environment
            from .processes import SharedDescriptor, resource_fds

            from .resource_limits import ensure_file_descriptor_limit

            ensure_file_descriptor_limit()
            self.directory = tempfile.TemporaryDirectory(prefix="rc-batch-")
            self.root.mkdir(parents=True, exist_ok=True)
            pending = []
            try:
                for service_index, config in enumerate(self.configs):
                    entries = []
                    self.endpoints[config.name] = entries
                    for replica, device in enumerate(config.devices or (None,)):
                        ctx = multiprocessing.get_context("spawn")
                        parent, child = ctx.Pipe(duplex=False)
                        stop = ctx.Event()
                        socket_path = str(
                            Path(self.directory.name)
                            / f"{service_index}-{replica}.sock"
                        )
                        process = ctx.Process(
                            target=_serve,
                            args=(
                                config,
                                socket_path,
                                child,
                                stop,
                                str(self.root / f"{service_index}-{replica}.json"),
                                tuple(SharedDescriptor(fd) for fd in resource_fds()),
                            ),
                        )
                        with _worker_thread_environment(
                            1, gpu_id=physical_device(device, self.virtual_gpu_map),
                            virtual_gpu_id=device if device in self.virtual_gpu_map else None
                        ):
                            process.start()
                        child.close()
                        self.processes.append((process, stop))
                        entries.append(
                            (str(device) if device is not None else None, socket_path)
                        )
                        pending.append((config.name, process, parent))

                async def wait_ready(name, process, parent):
                    try:
                        deadline = (
                            asyncio.get_running_loop().time() + self.startup_timeout
                        )
                        while not parent.poll():
                            if not process.is_alive():
                                raise RuntimeError(
                                    f"model service {name} exited during startup"
                                )
                            if asyncio.get_running_loop().time() >= deadline:
                                raise TimeoutError(
                                    f"model service {name} startup timed out"
                                )
                            await asyncio.sleep(0.02)
                        kind, error = parent.recv()
                        if kind != "ready":
                            raise RuntimeError(error)
                    finally:
                        parent.close()

                tasks = [
                    asyncio.create_task(wait_ready(name, process, parent))
                    for name, process, parent in pending
                ]
                try:
                    await asyncio.gather(*tasks)
                except BaseException:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
                self.started = True
            except BaseException:
                await self.close()
                raise

    def for_worker(self, index, device=None):
        routes = {}
        for name, entries in self.endpoints.items():
            matched = [path for gpu, path in entries if gpu == device]
            if self.virtual_gpu_map and device in self.virtual_gpu_map and not matched:
                raise ValueError(f"service {name} has no replica for virtual GPU {device}")
            routes[name] = matched[0] if matched else entries[index % len(entries)][1]
        return routes

    def check_health(self):
        for process, _ in self.processes:
            if not process.is_alive():
                raise RuntimeError(
                    f"shared model process exited: pid={process.pid}, code={process.exitcode}"
                )

    async def close(self):
        for _, stop in self.processes:
            stop.set()
        for process, _ in self.processes:
            await asyncio.to_thread(process.join, self.shutdown_timeout)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 1)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join)
            process.close()
        self.processes.clear()
        self.endpoints.clear()
        self.started = False
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None


async def _run_standalone(args):
    """Explicit endpoints allow simulator and model to use different Python envs."""
    import signal

    configs = json.loads(Path(args.config).read_text())
    services = SharedBatchServices(
        configs,
        args.metrics_dir,
        virtual_gpu_map=json.loads(args.virtual_gpu_map),
        startup_timeout_s=args.startup_timeout,
        shutdown_timeout_s=args.shutdown_timeout,
    )
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    ready = Path(args.ready_file)
    try:
        await services.start()
        ready.write_text(json.dumps(services.endpoints))
        while not stopped.is_set():
            services.check_health()
            try:
                await asyncio.wait_for(stopped.wait(), 0.5)
            except asyncio.TimeoutError:
                pass
    finally:
        ready.unlink(missing_ok=True)
        await services.close()


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Serve configured shared model replicas from this Python environment"
    )
    parser.add_argument(
        "--config", required=True, help="JSON list of BatchServiceConfig objects"
    )
    parser.add_argument("--virtual-gpu-map", default="{}", help="JSON alias to physical GPU mapping")
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--metrics-dir", required=True)
    parser.add_argument("--startup-timeout", type=float, default=600)
    parser.add_argument("--shutdown-timeout", type=float, default=10)
    asyncio.run(_run_standalone(parser.parse_args()))


if __name__ == "__main__":
    main()
