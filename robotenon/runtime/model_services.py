"""Worker-owned model servers with explicit device and endpoint binding."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .execution import ResourceLease


def physical_devices(local_ids, visible=None):
    visible = os.environ.get("CUDA_VISIBLE_DEVICES", "") if visible is None else visible
    devices = [value.strip() for value in visible.split(",") if value.strip()]
    if not devices or max(local_ids) >= len(devices):
        raise ValueError(
            "tool deployment requires valid indices into the Worker's CUDA_VISIBLE_DEVICES"
        )
    return tuple(devices[index] for index in local_ids)


def service_command(config, host, port):
    values = {
        "host": host,
        "port": str(port),
        "model_path": config.model_path or "",
        "tensor_parallel_size": str(len(config.gpu_ids)),
    }
    if config.backend == "command":
        if not config.command:
            raise ValueError("command deployment requires a non-empty argv list")
        return [arg.format_map(values) for arg in config.command]
    import importlib

    module, separator, attribute = config.backend.partition(":")
    if not separator or not module or not attribute:
        raise ValueError("deployment.backend must be 'command' or a module:callable")
    provider = getattr(importlib.import_module(module), attribute)
    if not callable(provider):
        raise TypeError("deployment backend must be callable")
    command = provider(config, host, port)
    if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
        raise TypeError("deployment backend must return a non-empty argv list of strings")
    return command


class ModelService:
    def __init__(self, config, directory, *, tool_id=None):
        self.config = config
        self.tool_id = tool_id
        self.directory = Path(directory)
        self.host = "127.0.0.1"
        self.port = None
        self.process = None
        self.log = None
        self.lease = None
        self.own_group = not bool(os.environ.get("ROBOTENON_WORKER_ID"))

    def _reserve_port(self):
        for _ in range(20):
            with socket.socket() as reservation:
                reservation.bind((self.host, 0))
                port = reservation.getsockname()[1]
                lease = ResourceLease([f"model-port:{port}"])
                try:
                    lease.acquire()
                except RuntimeError:
                    continue
                self.port, self.lease = port, lease
                return
        raise RuntimeError("unable to reserve a model service port")

    def check_health(self):
        if self.process is not None and self.process.poll() is not None:
            raise RuntimeError(
                f"model service exited ({self.process.returncode}); see {self.directory / 'server.log'}"
            )

    def _ready(self):
        path = self.config.health_path
        try:
            if path:
                if not path.startswith("/"):
                    raise ValueError("health_path must start with /")
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(
                    f"http://{self.host}:{self.port}{path}", timeout=0.5
                ) as response:
                    return response.status == 200
            with socket.create_connection((self.host, self.port), timeout=0.5):
                return True
        except (OSError, urllib.error.URLError):
            return False

    async def start(self):
        if self.process is not None:
            self.check_health()
            return
        devices = physical_devices(self.config.gpu_ids)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._reserve_port()
        try:
            command = service_command(self.config, self.host, self.port)
            env = dict(os.environ)
            env.update(self.config.env)
            env["CUDA_VISIBLE_DEVICES"] = ",".join(devices)
            # Do not inherit an unrelated torchrun/rendezvous context.
            for key in (
                "RANK",
                "LOCAL_RANK",
                "WORLD_SIZE",
                "LOCAL_WORLD_SIZE",
                "MASTER_ADDR",
                "MASTER_PORT",
            ):
                env.pop(key, None)
            self.log = (self.directory / "server.log").open("ab", buffering=0)
            from .processes import resource_fds, register_process
            self.process = subprocess.Popen(
                command,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=self.log,
                stderr=subprocess.STDOUT,
                start_new_session=self.own_group,
                pass_fds=resource_fds(),
            )
            registry = os.environ.get("ROBOTENON_PROCESS_REGISTRY")
            if registry:
                register_process(registry, self.process.pid, role="model")
            manifest = {
                "tool_id": self.tool_id,
                "pid": self.process.pid,
                "backend": self.config.backend,
                "host": self.host,
                "port": self.port,
                "local_gpu_ids": self.config.gpu_ids,
                "physical_gpu_ids": devices,
                "model_path": self.config.model_path,
                "status": "starting",
            }
            (self.directory / "deployment.json").write_text(
                json.dumps(manifest, indent=2)
            )
            deadline = time.monotonic() + self.config.startup_timeout_s
            while time.monotonic() < deadline:
                self.check_health()
                if await asyncio.to_thread(self._ready):
                    self.check_health()
                    manifest["status"] = "ready"
                    (self.directory / "deployment.json").write_text(
                        json.dumps(manifest, indent=2)
                    )
                    return
                await asyncio.sleep(0.1)
            raise TimeoutError(
                f"model startup timed out; see {self.directory / 'server.log'}"
            )
        except BaseException:
            await self.close()
            raise

    def _signal(self, sig):
        try:
            if self.own_group:
                os.killpg(self.process.pid, sig)
            elif self.process.poll() is None:
                self.process.send_signal(sig)
        except ProcessLookupError:
            pass

    async def close(self):
        try:
            if self.process is not None:
                self._signal(signal.SIGTERM)
                deadline = time.monotonic() + self.config.shutdown_timeout_s
                while self.process.poll() is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                # Reap remaining descendants in our own group even if the leader exited.
                self._signal(signal.SIGKILL)
                await asyncio.to_thread(self.process.wait)
                self.process = None
        finally:
            if self.log is not None:
                self.log.close()
                self.log = None
            if self.lease is not None:
                self.lease.close()
                self.lease = None


class DeployedTool:
    """Keep provider-specific inference in the Tool, process ownership here."""

    def __init__(self, tool, config, directory, *, tool_id=None):
        if not callable(getattr(tool, "bind_deployment", None)):
            raise TypeError(
                "managed Tool must implement bind_deployment(host, port, model_path)"
            )
        self.tool = tool
        self.service = ModelService(config, directory, tool_id=tool_id)

    def __getattr__(self, name):
        return getattr(self.tool, name)

    async def start(self):
        try:
            if not getattr(self.tool, "dry_run", False):
                await self.service.start()
                self.tool.bind_deployment(
                    self.service.host, self.service.port, self.service.config.model_path
                )
            await self.tool.start()
        except BaseException:
            await self.close()
            raise

    async def close(self):
        try:
            await asyncio.wait_for(
                self.tool.close(), timeout=self.service.config.shutdown_timeout_s
            )
        finally:
            await self.service.close()
