"""Generic physical-robot Body provider.

The provider owns action execution and safety-stop delivery.  It intentionally
does not import ROS2 or a vendor SDK; deployments inject a driver object with
``execute`` and, ideally, ``hold``/``stop`` methods.  This keeps the graph and
Body contract stable across a real robot, an RPC bridge and a test double.
"""

from __future__ import annotations

import inspect
import asyncio
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping, MutableSequence
from typing import Any

from ..schemas import Action, EnvironmentFeedback, EnvironmentHealth
from ..services.body import InProcessBodyService
from .registry import register_body


class _UnavailableRobotDriver:
    def __init__(self, robot_name: str) -> None:
        self.robot_name = robot_name

    async def execute(self, action: Action) -> None:
        raise RuntimeError(
            f"real robot driver is not configured for {self.robot_name!r}; "
            "inject a ROS2/SDK/RPC driver before starting the experiment"
        )

    async def health(self) -> EnvironmentHealth:
        return EnvironmentHealth(
            ready=False,
            degraded=True,
            message="driver_not_configured",
        )

    async def hold(self, reason: str = "stop_requested") -> None:
        return None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@register_body(
    "real_robot_body",
    version="1.0.0",
    kind="ros2",
    description="Execute ActionChunks through an injected ROS2/SDK/RPC robot driver",
)
class RealRobotBody(InProcessBodyService):
    """Safety-supervised Body that never executes through EnvironmentSession."""

    def __init__(
        self,
        *,
        robot_name: str = "robot0",
        action_format: str = "ee_delta_gripper",
        control_rate_hz: float = 20.0,
        safe_stop_mode: str = "hold",
        driver: Any = None,
        action_transform: Any = None,
        require_driver: bool = True,
        session: Any = None,
        observation_updates: MutableSequence[EnvironmentFeedback] | None = None,
        executor: Any = None,
        stop_executor: Any = None,
        driver_timeout_s: float = 5.0,
        **kwargs: Any,
    ) -> None:
        if control_rate_hz <= 0:
            raise ValueError("control_rate_hz must be positive")
        if safe_stop_mode not in {"hold", "stop"}:
            raise ValueError("safe_stop_mode must be 'hold' or 'stop'")
        self.robot_name = robot_name
        self.action_format = action_format
        self.control_rate_hz = float(control_rate_hz)
        self.safe_stop_mode = safe_stop_mode
        self.driver = driver or _UnavailableRobotDriver(robot_name)
        self.require_driver = bool(require_driver)
        self.action_transform = action_transform
        self.session = session
        self.observation_updates = observation_updates
        if driver_timeout_s <= 0:
            raise ValueError("driver_timeout_s must be positive")
        self.driver_timeout_s = driver_timeout_s
        self._driver_started = False
        self._pools_closed = False
        self._driver_start_lock = asyncio.Lock()
        self._driver_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="robot-sdk")
        self._control_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="robot-stop")
        # ``executor`` and the environment ``stop_executor`` are accepted so
        # the generic bootstrap can pass runtime values to every Body.  A
        # real robot always routes both normal actions and safety stops to its
        # own driver; EnvironmentSession.command("stop") is not a hardware
        # emergency stop.
        _ = executor, stop_executor
        kwargs.setdefault("idempotency_scope", f"real_robot:{robot_name}:{action_format}")
        super().__init__(
            self._execute_action,
            stop_executor=self._stop_driver,
            **kwargs,
        )

    async def start(self) -> None:
        if self.require_driver and isinstance(self.driver, _UnavailableRobotDriver):
            raise RuntimeError(
                f"real robot body {self.robot_name!r} requires an injected driver"
            )
        async with self._driver_start_lock:
            if not self._driver_started:
                if self._pools_closed:
                    self._driver_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="robot-sdk")
                    self._control_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="robot-stop")
                    self._pools_closed = False
                for name in ("connect", "start"):
                    method = getattr(self.driver, name, None)
                    if callable(method):
                        await asyncio.wait_for(self._driver_call(method), self.driver_timeout_s)
                self._driver_started = True
        await super().start()

    async def _driver_call(self, method, *args, control=False, **kwargs):
        if inspect.iscoroutinefunction(method):
            return await method(*args, **kwargs)
        if not control and self._sync_driver_busy.is_set():
            raise RuntimeError("robot SDK is still completing a previous call")
        if not control:
            self._sync_driver_busy.set()
        def invoke():
            try:
                return method(*args, **kwargs)
            finally:
                if not control:
                    self._sync_driver_busy.clear()
        try:
            future = asyncio.get_running_loop().run_in_executor(
                self._control_pool if control else self._driver_pool, invoke)
        except BaseException:
            if not control:
                self._sync_driver_busy.clear()
            raise
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        # Cancellation releases the event loop, but the guard remains set until
        # the actual SDK call returns. Stops use their own independent thread.
        return await _maybe_await(await asyncio.shield(future))

    async def _execute_action(self, action: Action) -> Any:
        candidate: Any = action
        if callable(self.action_transform):
            candidate = await self._driver_call(self.action_transform, action)
            if isinstance(candidate, Mapping):
                copy_model = getattr(action, "model_copy", None)
                updates = {"payload": dict(candidate)}
                candidate = (
                    copy_model(update=updates)
                    if callable(copy_model)
                    else action.copy(update=updates)
                )
        execute = getattr(self.driver, "execute", None)
        if not callable(execute):
            raise TypeError("real robot driver must implement execute(action)")
        result = await self._driver_call(execute, candidate)
        if (
            isinstance(result, EnvironmentFeedback)
            and self.observation_updates is not None
        ):
            self.observation_updates.append(result)
        return result

    async def _stop_driver(self, reason: str = "stop_requested") -> Any:
        method_name = "hold" if self.safe_stop_mode == "hold" else "stop"
        method = getattr(self.driver, method_name, None)
        if not callable(method):
            method = getattr(self.driver, "stop", None)
        if not callable(method):
            command = getattr(self.driver, "command", None)
            if callable(command):
                return await self._driver_call(command, "stop", reason=reason, control=True)
            raise RuntimeError(
                f"real robot driver has no {method_name}()/stop()/command() "
                "safe-stop API"
            )
        args = (reason,)
        try:
            inspect.signature(method).bind(reason)
        except TypeError:
            args = ()
        except ValueError:
            pass
        return await self._driver_call(method, *args, control=True)

    async def health(self) -> EnvironmentHealth:
        body_health = await super().health()
        driver_health = getattr(self.driver, "health", None)
        if not callable(driver_health):
            return body_health
        try:
            value = await asyncio.wait_for(self._driver_call(driver_health), self.driver_timeout_s)
        except (TimeoutError, RuntimeError) as exc:
            return EnvironmentHealth(ready=False, degraded=True, message=str(exc))
        if isinstance(value, EnvironmentHealth):
            if not value.ready or value.degraded:
                copy_model = getattr(value, "model_copy", None)
                if callable(copy_model):
                    return copy_model(update={"degraded": True})
                return value.copy(update={"degraded": True})
            return body_health
        if isinstance(value, Mapping):
            value = EnvironmentHealth(**dict(value))
            if not value.ready or value.degraded:
                return value
        if value is False:
            return EnvironmentHealth(
                ready=False,
                degraded=True,
                message="driver_not_ready",
            )
        return body_health

    async def close(self) -> None:
        await super().close()
        if self._sync_driver_busy.is_set():
            raise TimeoutError("robot SDK is still running; driver cannot be safely closed")
        close = getattr(self.driver, "close", None)
        if callable(close):
            await asyncio.wait_for(self._driver_call(close), self.driver_timeout_s)
        self._driver_started = False
        self._driver_pool.shutdown(wait=False)
        self._control_pool.shutdown(wait=False)
        self._pools_closed = True


__all__ = ["RealRobotBody"]
