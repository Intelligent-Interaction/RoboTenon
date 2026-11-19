"""Persistent spawn workers. Only serializable job data crosses the boundary."""

from __future__ import annotations

import asyncio
import faulthandler
import multiprocessing
import os
import signal
import time
import traceback
from contextlib import AsyncExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4


async def _wait_readable(connection, timeout):
    """Wait for an IPC notification without a fixed polling delay.

    POSIX selector loops watch the Pipe descriptor. Alternate event loops use
    one bounded blocking poll in a thread; no thread outlives its timeout.
    """
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    descriptor = connection.fileno()

    def readable():
        if not ready.done():
            ready.set_result(True)

    try:
        loop.add_reader(descriptor, readable)
    except (AttributeError, NotImplementedError):
        return await asyncio.to_thread(connection.poll, timeout)
    try:
        try:
            return await asyncio.wait_for(ready, timeout)
        except asyncio.TimeoutError:
            return False
    finally:
        loop.remove_reader(descriptor)


class WorkerLost(RuntimeError):
    pass


def is_native_render_crash(error):
    """Only switch renderers for an observed native pixel-read crash."""
    message = str(error)
    return (
        any(signal in message for signal in ("signal=SIGABRT", "signal=SIGSEGV"))
        and "crash_site=" in message
        and "binding_utils.py" in message
        and "in read_pixels" in message
    )


@contextmanager
def _worker_thread_environment(
    count, *, gpu_id=None, egl_device_id=None, render_backend=None, virtual_gpu_id=None
):
    values = dict.fromkeys(
        (
            "OMP_NUM_THREADS",
            "MKL_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
        ),
        str(count),
    )
    # sitecustomize and spawn's module imports run before _worker_main.
    # Native libraries must see the final device assignment from startup.
    values["ROBOTENON_VIRTUAL_GPU_ID"] = virtual_gpu_id or ""
    if gpu_id is not None:
        values["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    if egl_device_id is not None:
        values["MUJOCO_EGL_DEVICE_ID"] = str(egl_device_id)
    if render_backend is not None:
        values.update(MUJOCO_GL=render_backend, PYOPENGL_PLATFORM=render_backend)
        if render_backend == "osmesa":
            # Mesa must see the system C++ ABI before spawn imports extensions.
            # Re-exec inside a spawn worker would lose multiprocessing pipes.
            library = "/usr/lib/x86_64-linux-gnu/libstdc++.so.6"
            if Path(library).is_file():
                preload = os.environ.get("LD_PRELOAD", "").split(os.pathsep)
                values["LD_PRELOAD"] = os.pathsep.join(
                    dict.fromkeys([library, *filter(None, preload)])
                )
            values["LP_NUM_THREADS"] = str(count)
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _worker_main(
    connection,
    profile,
    gpu_id,
    egl_device_id,
    worker_id,
    worker_threads=2,
    lease_fds=(),
    native_reset_parallelism=0,
    native_reset_gate=None,
):
    # The Worker and its model children share a killable process group.
    os.setsid()
    import json
    from .processes import register_process

    registry = (
        Path(profile.output or "data/runs/result.json").resolve().parent / ".processes"
    )
    os.environ["ROBOTENON_PROCESS_REGISTRY"] = str(registry)
    os.environ["ROBOTENON_RESOURCE_FDS"] = json.dumps(lease_fds)
    register_process(registry, os.getpid(), role="worker")
    # Also applies when a pre-update coordinator spawns this entry point.
    for key in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ[key] = str(worker_threads)
    diagnostic_root = (
        Path(profile.output or "data/runs/result.json").resolve().parent
        / "workers"
        / worker_id
    )
    diagnostic_root.mkdir(parents=True, exist_ok=True)
    diagnostic_file = (diagnostic_root / "worker-fault.log").open("a", buffering=1)
    faulthandler.enable(file=diagnostic_file, all_threads=True)
    os.environ["ROBOTENON_WORKER_ID"] = worker_id
    if gpu_id is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = gpu_id
    if egl_device_id is not None:
        os.environ["MUJOCO_EGL_DEVICE_ID"] = egl_device_id
    # Enter the host-wide gate before provider and plugin imports can load
    # native libraries. The benchmark adapter releases it after its first
    # reset and first observation are complete.
    from .startup_gate import acquire_startup_gate, release_startup_gate

    acquire_startup_gate(native_reset_parallelism, native_reset_gate)
    # All provider/model initialization is after device binding.
    from ..infra.params import Params
    from ..task_adapters.base import TaskAdapterRunContext
    from .config import TaskAdapterConfig, TaskAdaptersConfig
    from .factory import build_task_adapters
    from .worker import WorkerRuntime

    async def serve():
        worker = WorkerRuntime(profile)
        task = None
        scopes = AsyncExitStack()

        async def execute(job):
            request, case = job["request"], job["case"]
            try:
                configs = TaskAdaptersConfig(
                    task_adapters=[TaskAdapterConfig.model_validate(job["adapter"])],
                    plugin_task_adapters=worker.loaded.task_adapters.plugin_task_adapters,
                )
                adapter = build_task_adapters(configs)[0]

                async def run_case(selected, output, trace_dir, memory):
                    effective = replace(
                        request,
                        task_id=selected.task_id,
                        env_id=selected.environment_id,
                        seed=selected.seed,
                        horizon=selected.horizon,
                        output=output,
                        trace_dir=trace_dir,
                        memory=memory,
                    )
                    return await worker.run_task(effective, case=selected)

                context = TaskAdapterRunContext(
                    params=Params(job["params"]),
                    config_path=Path(request.config),
                    output_root=Path(job["output_root"]),
                    run_id=request.identity.run_id,
                    memory_path=Path(request.memory) if request.memory else None,
                    run_episode=run_case,
                    options=job["options"],
                    case_output=Path(request.output),
                    case_trace_dir=request.trace_dir,
                )
                result = dict(await adapter.adapter.run_case(case, context))
                result["identity"] = request.identity.to_dict()
                connection.send(("result", request.identity.attempt_id, result))
            except asyncio.CancelledError:
                connection.send(("cancelled", request.identity.attempt_id, None))
                raise
            except Exception as exc:
                traceback.print_exc(file=diagnostic_file)
                connection.send(
                    (
                        "error",
                        request.identity.attempt_id,
                        f"{type(exc).__name__}: {exc}",
                    )
                )
            finally:
                # Non-native adapters and failures before reset must never
                # strand the other Workers behind the bootstrap gate.
                release_startup_gate()

        try:
            from .execution import ExecutionOptions
            from ..models import load_factory

            execution_options = ExecutionOptions.from_params(worker.loaded.params)
            for reference in execution_options.worker_contexts:
                scope = load_factory(reference)()
                if hasattr(scope, "__aenter__"):
                    await scopes.enter_async_context(scope)
                else:
                    scopes.enter_context(scope)
            await worker.start()
            connection.send(("ready", None, None))
            heartbeat = time.monotonic()
            next_health = heartbeat
            while True:
                now = time.monotonic()
                if now >= next_health:
                    worker.check_health()
                    next_health = now + 0.25
                # Commands wake immediately. Health checks and heartbeats have
                # their own deadlines and no longer run on every 20 ms tick.
                if await _wait_readable(
                    connection, max(0.001, min(next_health - now, heartbeat + 1 - now))
                ):
                    command, payload = connection.recv()
                    if command == "shutdown":
                        if task is not None:
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                        break
                    if command == "cancel":
                        if task is not None:
                            task.cancel()
                    elif command == "execute":
                        if task is not None and not task.done():
                            raise RuntimeError("worker received overlapping tasks")
                        task = asyncio.create_task(execute(payload))
                if time.monotonic() - heartbeat >= 1:
                    connection.send(("heartbeat", None, None))
                    heartbeat = time.monotonic()
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            try:
                await worker.close()
            finally:
                try:
                    await scopes.aclose()
                finally:
                    connection.close()

    try:
        asyncio.run(serve())
    except (EOFError, BrokenPipeError):
        pass
    except BaseException:
        traceback.print_exc(file=diagnostic_file)
        raise
    finally:
        release_startup_gate()
        faulthandler.disable()
        diagnostic_file.close()
        for fd in lease_fds:
            os.close(fd)


class ProcessSlot:
    def __init__(self, profile, gpu_id, options, egl_device_id=None, virtual_gpu_id=None):
        self.profile, self.gpu_id, self.options = profile, gpu_id, options
        self.egl_device_id = egl_device_id
        self.virtual_gpu_id = virtual_gpu_id
        self.process = self.connection = None
        self.process_group_id = None
        self.worker_id = None
        self.completed = 0
        self.render_backend = None
        # A slot can be closed by the task error path and by backend shutdown
        # at the same time.  Serialize cleanup so one caller cannot close a
        # Process object while another caller is still joining it.
        self._close_lock = asyncio.Lock()

    def _lost_error(self, reason):
        code = self.process.exitcode
        details = [
            f"pid={self.process.pid}",
            f"exitcode={code}",
            f"worker_id={self.worker_id}",
        ]
        if code is not None and code < 0:
            try:
                details.append(f"signal={signal.Signals(-code).name}")
            except ValueError:
                pass
        fault_log = (
            Path(self.profile.output or "data/runs/result.json").resolve().parent
            / "workers"
            / str(self.worker_id)
            / "worker-fault.log"
        )
        if fault_log.is_file():
            details.append(f"fault_log={fault_log}")
            # Keep the crash site visible in episode errors without copying
            # every background thread's stack into the experiment summary.
            try:
                with fault_log.open(errors="replace") as stream:
                    current = False
                    for line in stream:
                        if line.startswith("Current thread"):
                            current = True
                        elif current and line.lstrip().startswith('File "'):
                            details.append(f"crash_site={line.strip()}")
                            break
            except OSError:
                pass
        return WorkerLost(f"{reason} ({', '.join(details)})")

    async def start(self):
        if self.process is not None:
            return
        parent, child = multiprocessing.get_context("spawn").Pipe()
        self.worker_id = str(uuid4())
        from .processes import resource_fds, SharedDescriptor

        self.process = multiprocessing.get_context("spawn").Process(
            target=_worker_main,
            args=(
                child,
                self.profile,
                self.gpu_id,
                self.egl_device_id,
                self.worker_id,
                self.options.worker_threads,
                tuple(SharedDescriptor(fd) for fd in resource_fds()),
                self.options.native_reset_parallelism,
                self.options.native_reset_gate,
            ),
        )
        self.connection = parent
        # Configure native libraries before Python imports them in the child.
        # There is no await here, so other slots cannot interleave this scope.
        with _worker_thread_environment(
            self.options.worker_threads,
            gpu_id=self.gpu_id,
            virtual_gpu_id=self.virtual_gpu_id,
            egl_device_id=self.egl_device_id,
            render_backend=self.render_backend,
        ):
            self.process.start()
        child.close()
        try:
            kind, _, _ = await self.receive(self.options.startup_timeout_s)
            if kind != "ready":
                raise WorkerLost("worker failed to initialize")
            # _worker_main calls setsid() before loading any provider.  Keep
            # the resulting PGID so a crashed Worker can still release model
            # grandchildren after the leader itself has exited.
            try:
                self.process_group_id = os.getpgid(self.process.pid)
            except (OSError, ProcessLookupError):
                self.process_group_id = self.process.pid
        except BaseException:
            await self.close()
            raise

    async def receive(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if await _wait_readable(
                    self.connection, min(0.25, max(0.001, deadline - time.monotonic()))
                ):
                    message = self.connection.recv()
                    if message[0] != "heartbeat":
                        return message
            except (EOFError, OSError) as exc:
                # EOF may arrive just before the OS publishes the exit status.
                await asyncio.to_thread(self.process.join, 0.2)
                raise self._lost_error("worker connection closed") from exc
            if not self.process.is_alive():
                raise self._lost_error("worker exited")
        raise WorkerLost("worker exceeded execution deadline")

    def _signal_group(self, sig):
        import signal

        pgid = self.process_group_id or self.process.pid
        if pgid is None:
            return
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            # Startup can fail before the child establishes its session.
            if self.process.is_alive():
                if sig == signal.SIGKILL:
                    self.process.kill()
                else:
                    self.process.terminate()

    async def close(self):
        async with self._close_lock:
            await self._close_unlocked()

    async def _close_unlocked(self):
        import signal

        process = self.process
        connection = self.connection
        if process is None:
            return
        try:
            if process.is_alive():
                try:
                    if connection is not None:
                        connection.send(("shutdown", None))
                except (OSError, EOFError):
                    pass
                deadline = time.monotonic() + self.options.shutdown_timeout_s
                while process.is_alive() and time.monotonic() < deadline:
                    # Drain final results so a full pipe cannot prevent cleanup.
                    if connection is not None and connection.poll():
                        try:
                            connection.recv()
                        except (EOFError, OSError):
                            break
                    await asyncio.sleep(0.02)
                if process.is_alive():
                    self._signal_group(signal.SIGTERM)
                    await asyncio.to_thread(process.join, 1)
                if process.is_alive():
                    self._signal_group(signal.SIGKILL)
                    # The worker creates its own session.  Kill the Process
                    # object directly as a fallback if the process group was
                    # already reaped or no longer has the expected leader.
                    try:
                        process.kill()
                    except (AttributeError, OSError, ProcessLookupError):
                        pass
            # A crashed Worker may have left model grandchildren alive.  The
            # stored PGID remains valid even after the Worker leader exits.
            if self.process_group_id is not None:
                self._signal_group(signal.SIGKILL)
                # Always reap before calling Process.close().  Process.close()
                # raises ValueError while the child is alive, which used to
                # hide the actual task timeout and turn it into a misleading
                # episode-level failure.
                if process.is_alive():
                    await asyncio.to_thread(process.join)
                else:
                    await asyncio.to_thread(process.join, 0)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except (OSError, ValueError):
                    pass
            # A started multiprocessing child is guaranteed to be reaped by
            # the join above.  Keep the guard for startup failures and for
            # alternate Process implementations used by embedders/tests.
            if not process.is_alive():
                try:
                    process.close()
                except (OSError, ValueError):
                    # A concurrent finalizer may have closed it already.
                    pass
            self.connection = self.process = None
            self.process_group_id = None
            self.completed = 0


class PersistentExecutionBackend:
    def __init__(self, options, profile, request_factory):
        from .resource_limits import ensure_file_descriptor_limit

        ensure_file_descriptor_limit()
        self.options, self.profile, self.request_factory = (
            options,
            profile,
            request_factory,
        )
        self.max_workers = options.max_workers
        external = options.external_batch_services
        self.slots = [
            ProcessSlot(
                replace(
                    profile,
                    shared_model_endpoints={
                        **(profile.shared_model_endpoints or {}),
                        **{
                            name: endpoints[0] if len(endpoints) == 1 else endpoints[i]
                            for name, endpoints in external.items()
                        },
                    },
                )
                if external
                else profile,
                ",".join(options.worker_gpus(i)) if options.worker_gpus(i) else None,
                options,
                egl_device_id=options.worker_egl_device(i),
                virtual_gpu_id=options.worker_virtual_gpu(i),
            )
            for i in range(self.max_workers)
        ]
        from .batch_services import SharedBatchServices

        self.model_services = SharedBatchServices(
            options.batch_services,
            Path(profile.output or "data/runs/result.json").resolve().parent
            / "batch-metrics",
            virtual_gpu_map=options.virtual_gpu_map,
            startup_timeout_s=options.startup_timeout_s,
            shutdown_timeout_s=options.shutdown_timeout_s,
        )
        self.model_services.validate_worker_devices(
            [slot.virtual_gpu_id or slot.gpu_id for slot in self.slots])
        self._services_lock = asyncio.Lock()
        self._services_ready = False
        self._worker_startup = asyncio.Semaphore(
            options.worker_startup_parallelism or options.max_workers
        )
        self.available = asyncio.Queue()
        for slot in self.slots:
            self.available.put_nowait(slot)

    async def execute(self, configured, case, context):
        from ..task_adapters.evaluator import write_json

        if self.options.batch_services:
            async with self._services_lock:
                await self.model_services.start()
                if not self._services_ready:
                    for index, worker_slot in enumerate(self.slots):
                        routes = self.model_services.for_worker(
                            index, worker_slot.virtual_gpu_id or worker_slot.gpu_id
                        )
                        worker_slot.profile = replace(
                            worker_slot.profile, shared_model_endpoints=routes
                        )
                    self._services_ready = True
        slot = await self.available.get()
        base = self.request_factory(
            case, context.case_output, context.case_trace_dir, context.memory_path
        )
        try:
            for retry in range(self.options.max_retries + 1):
                identity = replace(base.identity, attempt_id=str(uuid4()))
                directory = (
                    Path(context.case_output).with_suffix("")
                    / "attempts"
                    / identity.attempt_id
                )
                directory.mkdir(parents=True, exist_ok=True)
                request = replace(
                    base,
                    identity=identity,
                    output=directory / "episode.json",
                    video_path=directory / "video.mp4" if base.video_path else None,
                    resource_root=directory / "resources",
                )
                if (
                    base.memory is not None
                    and context.options.get("memory_mode") == "snapshot"
                ):
                    snapshot = Path(context.output_root) / "memory_snapshot.json"
                    memory = directory / "memory.json"
                    memory.write_bytes(snapshot.read_bytes())
                    request = replace(request, memory=memory)
                state = {
                    "identity": identity.to_dict(),
                    "status": "assigned",
                    "retry": retry,
                    "render_backend": slot.render_backend
                    or os.environ.get("MUJOCO_GL")
                    or "auto",
                }
                write_json(directory / "status.json", state)
                try:
                    if slot.process is None:
                        async with self._worker_startup:
                            await slot.start()
                    else:
                        await slot.start()
                    request = replace(
                        request,
                        identity=replace(
                            identity, worker_id=slot.worker_id, gpu_id=slot.gpu_id
                        ),
                    )
                    state.update(identity=request.identity.to_dict(), status="running",
                                 virtual_gpu_id=slot.virtual_gpu_id, physical_gpu_id=slot.gpu_id)
                    write_json(directory / "status.json", state)
                    # Publish the exact attempt identity before the worker
                    # starts emitting spans.  The evaluator only knows the
                    # stable run/episode pair; this fills in retry/worker/GPU
                    # fields for the live Portal view.
                    if request.live_status_path is not None:
                        write_json(
                            Path(request.live_status_path),
                            {
                                "status": "running",
                                "identity": request.identity.to_dict(),
                                "updated_at": time.time(),
                            },
                        )
                    slot.connection.send(
                        (
                            "execute",
                            {
                                "request": request,
                                "case": case,
                                "adapter": configured.config.model_dump(mode="json"),
                                "params": context.params.to_dict(),
                                "output_root": str(context.output_root),
                                "options": dict(context.options),
                            },
                        )
                    )
                    kind, attempt_id, payload = await slot.receive(
                        self.options.task_timeout_s
                    )
                    if attempt_id != identity.attempt_id:
                        raise WorkerLost("unexpected attempt result")
                    if kind == "error":
                        state.update(status="failed", error=payload)
                        write_json(directory / "status.json", state)
                        raise RuntimeError(payload)
                    if kind != "result":
                        raise WorkerLost(f"unexpected worker response: {kind}")
                    write_json(directory / "episode.json", payload)
                    graph_failed = payload.get("graph_status") in {"failed", "error"}
                    if graph_failed:
                        state.update(
                            status="failed",
                            error=payload.get("error") or "Graph execution failed",
                        )
                    else:
                        state["status"] = "succeeded"
                    write_json(directory / "status.json", state)
                    # A Graph failure can be transient (for example a model
                    # transport timeout).  Honor the execution retry budget in
                    # the same way as a lost Worker, while preserving the final
                    # failed payload when the budget is exhausted.
                    if graph_failed and retry < self.options.max_retries:
                        await slot.close()
                        continue
                    slot.completed += 1
                    if slot.completed >= self.options.recycle_after_tasks:
                        await slot.close()
                    return payload
                except WorkerLost as exc:
                    state.update(status="lost", error=str(exc))
                    write_json(directory / "status.json", state)
                    await slot.close()
                    if retry == self.options.max_retries:
                        raise
                    fallback = getattr(self.options, "render_retry_backend", None)
                    current_backend = slot.render_backend or os.environ.get("MUJOCO_GL")
                    if (
                        fallback
                        and current_backend != fallback
                        and is_native_render_crash(exc)
                    ):
                        slot.render_backend = fallback
                        state["retry_render_backend"] = fallback
                        write_json(directory / "status.json", state)
                except asyncio.CancelledError:
                    state["status"] = "cancelled"
                    write_json(directory / "status.json", state)
                    await slot.close()
                    raise
                except Exception:
                    # A provider or cleanup failure may poison reusable state.
                    await slot.close()
                    raise
        finally:
            # Software rendering applies to this recovery attempt only.
            try:
                if slot.render_backend is not None:
                    await slot.close()
                    slot.render_backend = None
            finally:
                self.available.put_nowait(slot)

    async def close(self):
        try:
            await asyncio.gather(*(slot.close() for slot in self.slots))
        finally:
            await self.model_services.close()
