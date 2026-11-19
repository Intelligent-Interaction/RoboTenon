"""Run the service-oriented JSON graph with the deterministic fake env.

This example uses exactly the same boundaries that a RoboLab/RoboDojo adapter
would implement.  Replace the fake adapter and body driver; keep the graph and
module code unchanged.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from robotenon.envs import bind_environment, environment
from robotenon.runtime.config import BodyConfig, EnvironmentConfig, load_application_config
from robotenon.runtime.factory import (
    build_body_service,
    build_environment_adapter,
    build_module_runtime,
    build_safety_state,
    build_tool_runtime,
)
from robotenon.schemas import SessionSpec
from robotenon.services import (
    SafetyMonitorService,
    ServiceSupervisor,
)
from robotenon.workflow import GraphContext, GraphRuntime


async def main() -> None:
    loaded = load_application_config("configs/config.json")
    tools = build_tool_runtime(loaded.tools)
    modules = build_module_runtime(loaded.modules, tools=tools, params=loaded.params)
    adapter = build_environment_adapter(
        EnvironmentConfig(name="fake", params={"max_steps": 3})
    )
    session = await adapter.open(SessionSpec(task_id="service-demo", environment_id="fake"))
    # The environment session is the resident envs service boundary.  The
    # body receives actions through its own service and returns feedback via
    # the environment; there is no second in-process environment wrapper.
    env = session
    config_base = Path("configs/config.json").parent
    safety = build_safety_state(loaded.safety_state, base_dir=config_base)

    # A real body driver replaces this adapter callback.  The body service
    # stays resident and its watchdog keeps polling the shared safety store.
    body = build_body_service(
        BodyConfig(name="in-process", kind="local", params={"poll_hz": 100}),
        executor=env.apply_action,
        safety_state=safety,
    )
    monitor = SafetyMonitorService(env, safety_state=safety, poll_hz=20)
    supervisor = ServiceSupervisor(
        {"env": env, "body": body, "monitor": monitor},
        safety_state=safety,
        poll_hz=10,
    )
    runtime = GraphRuntime(
        loaded.graphs.graph_map(), modules, tools=tools, params=loaded.params,
        safety_state=safety, backend="fallback"
    )
    ctx = GraphContext(task_id="service-demo")
    previous_environment = bind_environment(env)

    try:
        await supervisor.start()
        result = await runtime.run(
            "embodied_loop",
            ctx=ctx,
            body=body,
            monitor=monitor,
        )
        print(result.status, result.success, result.context.feedback)
    finally:
        try:
            await supervisor.close()
        finally:
            environment.restore(previous_environment)
        await modules.close()
        await tools.close()


if __name__ == "__main__":
    asyncio.run(main())
