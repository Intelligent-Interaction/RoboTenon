"""Bind a benchmark's native protocol to RoboTenon providers.

The Graph owns the rollout; Body is the only action boundary. This module
contains no simulator imports and no independent evaluation loop.
"""
from __future__ import annotations

from copy import deepcopy
import importlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from robotenon.bodies.registry import register_body
from robotenon.envs.base import BaseEnvironmentAdapter, BaseEnvironmentSession, register_environment
from robotenon.schemas import (ActionStatus, ActionType, EnvironmentDescriptor)
from robotenon.services.body import InProcessBodyService
from robotenon.task_adapters.base import BaseTaskAdapter, TaskAdapterDescriptor, TaskCase
from robotenon.task_adapters.registry import register_task_adapter
from .codec import json_value, pack_observation
from .recording import EpisodeRecorder
from .render import apply_render_mode, normalize_render_mode


def load_class(reference: str):
    module, sep, name = reference.partition(":")
    if not sep:
        raise ValueError("benchmark class must be module:Class")
    return getattr(importlib.import_module(module), name)


def task_manifest(benchmark):
    """Preserve native task data where serializable; rehydrate opaque objects locally."""
    tasks = benchmark.get_tasks()
    records = []
    for index, task in enumerate(tasks):
        fields, opaque = {}, False
        for key, value in task.items():
            if key in {"episode_count", "episode_max_steps"}:
                continue
            try:
                fields[key] = json_value(value)
            except (TypeError, ValueError):
                opaque = True
        record = {"index": index, "name": str(task["name"]),
                  "fields": fields, "rehydrate": opaque}
        episode_count = task.get("episode_count")
        if episode_count is not None:
            if type(episode_count) is not int or episode_count < 1:
                raise ValueError("task episode_count must be a positive integer")
            record["episode_count"] = episode_count
        episode_max_steps = task.get("episode_max_steps")
        if episode_max_steps is not None:
            if type(episode_max_steps) is not int or episode_max_steps < 1:
                raise ValueError("task episode_max_steps must be a positive integer")
            record["episode_max_steps"] = episode_max_steps
        records.append(record)
    return {"tasks": records, "metadata": json_value(benchmark.get_metadata()),
            "metric_keys": benchmark.get_metric_keys()}


class BenchmarkEnvironment(BaseEnvironmentAdapter):
    benchmark_class = None
    benchmark_id = ""

    def __init__(self, *, params=None, render="gpu", **options):
        super().__init__()
        self.params = dict(params or {})
        self.render = normalize_render_mode(render)
        self.options = options

    def _create_session(self, spec):
        options = {**self.options, **spec.parameters}
        native_params = {**self.params, **options.get("native_params", {})}
        from .observation_params import merge_observation_params
        native_params = merge_observation_params(self.benchmark_class, native_params, options.get("vla_factory"))
        from .execution import active_pool
        pool = active_pool()
        if pool is None:
            apply_render_mode(self.benchmark_class, self.render, self.benchmark_id)
            native = self.benchmark_class(**native_params)
            tasks = None
        else:
            native, tasks = pool.acquire(self.benchmark_id, self.benchmark_class, native_params, self.render)
        return BenchmarkSession(spec, native, self.benchmark_id, options, pool=pool, tasks=tasks)


class BenchmarkSession(BaseEnvironmentSession):
    def __init__(self, spec, native, benchmark_id, options, *, pool=None, tasks=None):
        super().__init__(spec)
        self.native = native
        self.benchmark_id = benchmark_id
        self.options = options
        self.pool, self.tasks = pool, tasks
        self.native_observation = {}
        self.steps = 0
        self.result = None
        self.recorder = EpisodeRecorder()
        self.horizon = None
        self.dimensions = dict(options.get("native_dimensions") or {})

    async def reset(self, context):
        self._reset_lifecycle(context)
        self.steps, self.result = 0, None
        self.elapsed_sec = None
        self.recorder = EpisodeRecorder()
        self.last_fresh_action = None
        self.step_wall_sec = 0.0
        record = self.options.get("native_task")
        if record is not None and self.tasks is None and not self.options.get("rehydrate_task", False):
            task = deepcopy(record)
        else:
            tasks = self.tasks if self.tasks is not None else self.native.get_tasks()
            index = int(self.options.get("native_task_index", 0))
            if not 0 <= index < len(tasks):
                raise ValueError(f"task index {index} outside benchmark")
            task = dict(tasks[index])
            if record is not None:
                if task.get("name") != record.get("name"):
                    raise ValueError("native task enumeration changed since manifest creation")
                if self.tasks is None:
                    task.update(deepcopy(record))
        metadata = self.native.get_metadata()
        self.horizon = self.options.get("native_max_steps")
        if self.horizon is None:
            self.horizon = metadata.get("max_steps", 300)
        if isinstance(self.horizon, bool) or int(self.horizon) < 1:
            raise ValueError("native_max_steps must be positive")
        self.horizon = int(self.horizon)
        episode = int(self.options.get("native_episode", 0))
        cap = metadata.get("max_episodes_per_task")
        task["episode_idx"] = episode % cap if cap and self.options.get("throughput_mode", False) else episode
        self.policy_task = {}
        for key, value in task.items():
            if isinstance(value, (str, int, float, bool, list)):
                self.policy_task[key] = json_value(value)
        await self.native.start_episode(task, recorder=self.recorder)
        if self.options.get("native_live"):
            # Reject undeclared hold semantics before the first real-time tick.
            from .modules import validate_native_action
            validate_native_action(self.native.get_hold_action(None))
        # make_obs may consume first-frame flags/video history: exactly once per tick.
        self.native_observation = await self.native.get_observation()
        # Worker bootstrap is complete only when the simulator has produced
        # its first usable observation. This releases the next queued Worker.
        from robotenon.runtime.startup_gate import mark_worker_initialized

        mark_worker_initialized()
        return self._feedback(terminal=False)

    async def observe(self, request=None):
        self._ensure_open()
        if self.latest_feedback is None:
            raise RuntimeError("benchmark must be reset before observe")
        return self.latest_feedback

    async def execute_native(self, action):
        self._ensure_open()
        if self.paused or self.stopped or (self.latest_feedback and self.latest_feedback.terminal):
            raise RuntimeError("benchmark session cannot accept actions")
        if self.latest_feedback is None:
            raise RuntimeError("benchmark must be reset before executing")
        if action.session_id != self.session_id:
            raise ValueError("benchmark action belongs to another session")
        if action.action_type != ActionType.CUSTOM or action.payload.get("format") != self.benchmark_id:
            raise ValueError("benchmark action type/format mismatch")
        payload = action.payload.get("native_action")
        if not isinstance(payload, dict) or not payload:
            raise ValueError("native_action must be a nonempty mapping")
        # Every Body action represents one native tick, not a hidden chunk.
        from .modules import validate_native_action
        validate_native_action(payload)
        import time
        started = time.monotonic()
        await self.native.apply_action(payload)
        self.step_wall_sec = time.monotonic() - started
        if not action.payload.get("native_stale", False):
            self.last_fresh_action = payload
        self.steps += 1
        done = bool(await self.native.is_done())
        if not done:
            self.native_observation = await self.native.get_observation()
        terminal = done or self.steps >= self.horizon
        if terminal:
            self.elapsed_sec = round(float(await self.native.get_time()), 3)
            self.result = json_value(await self.native.get_result())
        return self._feedback(terminal=terminal, action=action,
                              reason="environment_terminal" if done else "horizon" if terminal else "continue")

    def _feedback(self, *, terminal, action=None, reason="reset"):
        observation = pack_observation(self.native_observation)
        metrics = self.result or {}
        return self._new_feedback(
            action=action, status=ActionStatus.SUCCEEDED if action else ActionStatus.UNKNOWN,
            observation=observation, terminal=terminal,
            success=bool(metrics.get("success", False)) if terminal else None,
            info={"benchmark_metrics": metrics,
                  "metric_keys": self.native.get_metric_keys(),
                  "native_step": json_value(self.recorder.fields),
                  "env_steps": self.steps, "elapsed_sec": self.elapsed_sec, "reason": reason,
                  "step_wall_sec": getattr(self, "step_wall_sec", 0.0)},
            metadata={"benchmark": self.benchmark_id,
                      **({"hold_action": json_value(self.native.get_hold_action(self.last_fresh_action))}
                         if self.options.get("native_live") else {}),
                      "native_task": self.policy_task,
                      "native_dimensions": self.dimensions,
                      "native_case": f"{self.benchmark_id}:{self.options.get('native_task_index', 0)}:{self.options.get('native_episode', 0)}",
                      "instruction": self.native_observation.get("task_description", ""),
                      "action_spec": {k: v.to_dict() for k, v in self.native.get_action_spec().items()},
                      "observation_spec": {k: v.to_dict() for k, v in self.native.get_observation_spec().items()}},
        )

    async def _close_impl(self, reason=None):
        if self.pool is None:
            self.native.cleanup()
        else:
            self.pool.release(self.benchmark_id)


class BenchmarkBody(InProcessBodyService):
    def __init__(self, *, session, observation_updates=None, **kwargs):
        self.session = session
        self.observation_updates = observation_updates
        # Body factory can inject these optional executor overrides.
        kwargs.pop("executor", None)
        stop_executor = kwargs.pop("stop_executor", None)
        super().__init__(self._execute_native, stop_executor=stop_executor or session.stop, **kwargs)

    async def _execute_native(self, action):
        feedback = await self.session.execute_native(action)
        if self.observation_updates is not None:
            self.observation_updates.append(feedback)
        return feedback


class BenchmarkTasks(BaseTaskAdapter):
    benchmark_class = None
    benchmark_id = ""
    dimension_spec = {}

    def __init__(self, *, params=None, episodes_per_task=1, max_steps=None,
                 tasks=None, max_tasks=None, render="gpu", manifest=None,
                 enumeration_timeout_s=1800, throughput_mode=False, vla_factory=None, scheduling=None, episode_plan_path=None, live=False):
        self.params = dict(params or {})
        self.episodes = episodes_per_task
        self.max_steps = max_steps
        self.tasks = tasks
        self.max_tasks = max_tasks
        self.render = normalize_render_mode(render)
        self.manifest = manifest
        self.enumeration_timeout_s = enumeration_timeout_s
        self.throughput_mode = throughput_mode
        self.vla_factory = vla_factory
        self.live = live
        self.episode_plan_path = episode_plan_path
        self.scheduling = scheduling if scheduling is not None else {"shuffle": False}

    def validate(self, params):
        for label, value in (("episodes_per_task", self.episodes), ("max_steps", self.max_steps),
                             ("max_tasks", self.max_tasks)):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{label} must be a positive integer")
        if self.enumeration_timeout_s <= 0:
            raise ValueError("enumeration_timeout_s must be positive")

    def _manifest(self):
        if self.manifest is not None:
            return deepcopy(self.manifest)
        # Enumeration can boot a GPU simulator. Isolate it from the coordinator.
        from .observation_params import merge_observation_params
        request = {"class": f"{self.benchmark_class.__module__}:{self.benchmark_class.__name__}",
                   "params": merge_observation_params(self.benchmark_class, self.params, self.vla_factory), "render": self.render}
        # Simulator C/C++ libraries may write directly to stdout, bypassing
        # Python's redirect_stdout. Keep the manifest on a separate channel.
        with tempfile.TemporaryDirectory(prefix="robotenon-benchmark-") as directory:
            output = Path(directory) / "manifest.json"
            request["output"] = str(output)
            result = subprocess.run(
                [sys.executable, "-m", "robotenon_benchmark_support.enumerate"],
                input=json.dumps(request), capture_output=True, text=True,
                timeout=self.enumeration_timeout_s, check=False,
            )
            if result.returncode:
                raise RuntimeError(f"benchmark enumeration failed: {result.stderr[-4000:]}")
            return json.loads(output.read_text())

    def enumerate_cases(self, *, adapter_id, params):
        cases = list(self._ordered_cases(adapter_id=adapter_id, params=params))
        # Multi-profile runs defer ordering until every adapter has enumerated
        # its cases, so one deterministic shuffle covers the complete run.
        if self.scheduling.get("scope") != "global":
            from .scheduling import order_cases
            cases = order_cases(cases, self.scheduling)
        if self.episode_plan_path and self.scheduling.get("scope") != "global":
            destination = Path(self.episode_plan_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps({"scheduling": self.scheduling,
                "expected_episodes": len(cases), "episodes": [c.to_dict() for c in cases]}, indent=2) + "\n")
        yield from cases

    def _ordered_cases(self, *, adapter_id, params):
        self.validate(params)
        manifest = self._manifest()
        records = manifest["tasks"]
        if self.tasks:
            records = [r for r in records if r["name"] in self.tasks or r["fields"].get("suite") in self.tasks]
        if self.max_tasks:
            records = records[:self.max_tasks]
        if not records:
            raise ValueError("benchmark selection contains no tasks")
        default_horizon = manifest["metadata"].get("max_steps", 300)
        case_index = 0
        for record in records:
            horizon = (
                self.max_steps
                if self.max_steps is not None
                else record.get("episode_max_steps", default_horizon)
            )
            from .dimensions import extract_dimensions
            dimensions = extract_dimensions(self.dimension_spec, record["fields"], self.params)
            episode_count = record.get("episode_count", self.episodes)
            if type(episode_count) is not int or episode_count < 1:
                raise ValueError("manifest task episode_count must be a positive integer")
            task_episodes = min(self.episodes, episode_count)
            for episode in range(task_episodes):
                options = {"native_task": record["fields"], "native_task_index": record["index"],
                           "rehydrate_task": record["rehydrate"], "native_episode": episode,
                           "native_params": self.params, "native_max_steps": horizon,
                           "throughput_mode": self.throughput_mode, "vla_factory": self.vla_factory,
                           "native_dimensions": dimensions, "native_live": self.live}
                yield TaskCase(
                    adapter_id=adapter_id, task_id=record["name"], environment_id=self.benchmark_id,
                    index=case_index, episode=episode,
                    seed=record["fields"].get("seed", self.params.get("seed")), horizon=horizon,
                    parameters={"environment_parameters": options},
                    metadata={"native_task_index": record["index"],
                              "metric_keys": manifest["metric_keys"],
                              "dimensions": dimensions},
                )
                case_index += 1


def register_benchmark(name, native_class, *, module, dimensions=None):
    descriptor = EnvironmentDescriptor(
        env_id=name, display_name=name, backend_kind="local",
        capabilities={"reset", "observe", "native_evaluation"},
        action_types={ActionType.CUSTOM}, observation_modalities={"rgb", "state"},
    )
    env = type("Environment", (BenchmarkEnvironment,), {
        "__module__": module, "benchmark_id": name, "benchmark_class": native_class,
        "descriptor": descriptor})
    body = type("Body", (BenchmarkBody,), {"__module__": module})
    tasks = type("Tasks", (BenchmarkTasks,), {
        "__module__": module, "benchmark_id": name, "benchmark_class": native_class,
        "dimension_spec": dict(dimensions or {}),
        "descriptor": TaskAdapterDescriptor(adapter_id=name, kind="benchmark")})
    register_environment(name)(env)
    register_body(name, kind="local")(body)
    register_task_adapter(name, kind="benchmark")(tasks)
    return env, body, tasks
