"""Resident resources; one task at a time, independent of deployment type."""

from __future__ import annotations

import asyncio
import os
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from .config import load_request_config
from .factory import initialize_tool_runtime


class WorkerRuntime:
    def __init__(self, request, *, loaded=None, terminal=None):
        self.request = request
        self.loaded = deepcopy(loaded) if loaded is not None else load_request_config(request)
        self.terminal = terminal
        self.tools = None
        self._busy = False
        self._poisoned = False
        self._cleanup_failure = None
        self._close_lock = asyncio.Lock()
        self._environment = {}
        self.worker_id = os.environ.get("ROBOTENON_WORKER_ID") or str(uuid4())

    async def start(self):
        if self._poisoned:
            raise RuntimeError("worker resources could not be cleaned up; worker must be replaced")
        if self.tools is not None:
            return
        from .runner import _apply_request_overrides, _graph_selector
        from .result_projection import require_episode_outputs

        params = _apply_request_overrides(self.loaded.params, self.request)
        graph_id = _graph_selector(self.loaded, params, self.request)
        require_episode_outputs(self.loaded.graphs.graph_map()[graph_id])
        values = {}
        from .execution import ExecutionOptions

        execution = ExecutionOptions.from_params(params)
        execution.validate_tools(self.loaded.tools)
        if "local_worker" in execution.capabilities and execution.gpu_ids:
            values["CUDA_VISIBLE_DEVICES"] = ",".join(execution.worker_gpus(0))
        self._environment = {key: os.environ.get(key) for key in values}
        for key, value in values.items():
            if value is not None:
                os.environ[key] = str(value)
        try:
            output = (
                self.request.output
                or params.resolve("runtime.output_path")
                or "data/runs/result.json"
            )
            deployment_root = Path(output).resolve().parent / "workers" / self.worker_id
            self.tools = await initialize_tool_runtime(
                self.loaded.tools, deployment_root=deployment_root, runtime_params=params,
                shared_model_endpoints=self.request.shared_model_endpoints
            )
        except BaseException:
            await self.close()
            raise

    def check_health(self):
        if self.tools is not None:
            from .model_services import DeployedTool

            for tool in self.tools.values():
                if isinstance(tool, DeployedTool):
                    tool.service.check_health()

    async def run_task(self, request, *, case=None):
        if self._busy:
            raise RuntimeError("a WorkerRuntime can execute only one task at a time")
        if (
            request.config,
            request.experiment_config,
            request.model_path,
            request.model_host,
            request.model_port,
            request.dry_run,
        ) != (
            self.request.config,
            self.request.experiment_config,
            self.request.model_path,
            self.request.model_host,
            self.request.model_port,
            self.request.dry_run,
        ):
            raise ValueError("task runtime profile differs from resident worker")
        self._busy = True
        try:
            await self.start()
            self.check_health()
            from .runner import execute_episode

            return await execute_episode(
                request,
                case=case,
                loaded=self.loaded,
                terminal=self.terminal,
                tools=self.tools,
            )
        except BaseException as error:
            # A failed/cancelled task may have left provider session state behind.
            # Retire it before another task can reuse the resident tools.
            self._poisoned = True
            try:
                await self.close()
            except BaseException as cleanup_error:
                self._poisoned = True
                if hasattr(error, "add_note"):
                    error.add_note(f"worker cleanup failed: {cleanup_error}")
            raise
        finally:
            self._busy = False

    async def close(self):
        async with self._close_lock:
            if self._cleanup_failure is not None:
                raise RuntimeError("worker cleanup failed; replacement process required") from self._cleanup_failure
            try:
                if self.tools is not None:
                    from .execution import ExecutionOptions
                    timeout = ExecutionOptions.from_params(self.loaded.params).shutdown_timeout_s
                    cleanup = asyncio.create_task(self.tools.close())
                    cleanup.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
                    try:
                        done, _ = await asyncio.wait({cleanup}, timeout=timeout)
                        if not done:
                            raise TimeoutError("worker tool cleanup deadline exceeded")
                        cleanup.result()
                    finally:
                        if not cleanup.done():
                            cleanup.cancel()
                    self.tools = None
                self._poisoned = False
            except BaseException as exc:
                self._cleanup_failure = exc
                self._poisoned = True
                raise
            finally:
                for key, value in self._environment.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                self._environment.clear()
