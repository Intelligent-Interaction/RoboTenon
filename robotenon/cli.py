"""Command-line interface for RoboTenon."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from importlib import import_module
from pathlib import Path

_COMMANDS = {
    "run": (".runtime.runner_cli", "run a configured episode or task batch"),
    "evaluate": ("robotenon_benchmark_support.evaluate", "evaluate a model or system on a benchmark"),
    "web": (".web.server", "start the lightweight web portal"),
    "metricsd": (".daemons.metrics_daemon", "start the metrics service"),
    "traced": (".daemons.trace_daemon", "start the trace service"),
    "observation-cached": (".daemons.observation_cache_daemon", "start the observation cache service"),
}


async def run_fake(
    output: str,
    modules_config: str | None = None,
    tools_config: str | None = None,
    *,
    config: str = "./configs/config.json",
    experiment_config: str | None = None,
) -> dict:
    """Run the local Action/Feedback smoke environment once.

    The master config is the normal entry point.  The two manifest arguments
    remain as a migration path for callers of the original CLI and are used
    only when explicitly supplied.
    """
    from .runtime.safety import SafetyStateStore
    from .schemas import SessionSpec
    from .runtime.config import (
        BodyConfig,
        EnvironmentConfig,
        load_application_config,
        load_modules_config,
        load_tools_config,
    )
    from .workflow import load_graphs_config
    from .runtime.factory import (
        build_body_service,
        build_environment_adapter,
        build_safety_state,
        build_graph_runtime,
        initialize_module_runtime,
        initialize_tool_runtime,
    )
    from .services import SafetyMonitorService, ServiceSupervisor
    from .workflow import GraphContext
    from .envs import bind_environment, environment
    from .bodies.fake import FakeEnvironmentActionDriver

    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    if modules_config is not None or tools_config is not None:
        module_manifest = load_modules_config(
            modules_config or "./configs/modules_configs.json"
        )
        tool_manifest = load_tools_config(
            tools_config or "./configs/tools_configs.json"
        )
        graphs = load_graphs_config("./configs/graphs.json")
        params = None
        safety_state = SafetyStateStore()
    else:
        loaded = load_application_config(config, experiment_config)
        module_manifest = loaded.modules
        tool_manifest = loaded.tools
        params = loaded.params
        main_source = loaded.bundle.sources.get("main")
        config_base = (
            Path(main_source).parent
            if main_source and not str(main_source).startswith("<")
            else None
        )
        safety_state = build_safety_state(loaded.safety_state, base_dir=config_base)
        graphs = loaded.graphs
    tools = await initialize_tool_runtime(tool_manifest, runtime_params=params)
    modules = await initialize_module_runtime(
        module_manifest,
        tools=tools,
        params=params,
    )
    adapter = build_environment_adapter(
        EnvironmentConfig(name="fake", params={"max_steps": 3})
    )
    session = await adapter.open(SessionSpec(task_id="demo", environment_id="fake"))
    action_driver = FakeEnvironmentActionDriver(session)
    body = build_body_service(
        BodyConfig(name="in-process", kind="local"),
        executor=action_driver.execute,
        safety_state=safety_state,
    )
    monitor = SafetyMonitorService(session, safety_state=safety_state, poll_hz=20)
    supervisor = ServiceSupervisor(
        {"env": session, "body": body, "monitor": monitor},
        safety_state=safety_state,
        poll_hz=10,
    )
    runtime = build_graph_runtime(
        graphs,
        modules,
        backend="fallback",
        safety_state=safety_state,
        tools=tools,
        params=params,
    )
    previous_environment = bind_environment(session)
    try:
        await supervisor.start()
        context = GraphContext(task_id="demo")
        result = await runtime.run(
            "embodied_loop",
            ctx=context,
            body=body,
            monitor=monitor,
        )
        return {
            "session_id": str(result.context.session_id),
            "success": result.success,
            "steps": result.steps,
            "final_sequence_no": result.context.feedback.sequence_no if result.context.feedback else None,
            "workflow_status": result.status,
            "modules": modules.names(),
        }
    finally:
        try:
            await supervisor.close()
        finally:
            environment.restore(previous_environment)
        await modules.close()
        await tools.close()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in _COMMANDS:
        command = argv[0]
        module_name, _ = _COMMANDS[command]
        try:
            module = import_module(module_name, package=__package__)
        except ModuleNotFoundError as exc:
            if command != "evaluate" or exc.name != "robotenon_benchmark_support":
                raise
            print(
                "The evaluate command requires the optional benchmark support package. "
                "From the repository root, run: python -m pip install -e ./extensions/benchmarks/benchmark-support",
                file=sys.stderr,
            )
            return 2
        status = module.main(argv[1:])
        return 0 if status is None else status

    parser = argparse.ArgumentParser(prog="Tenon")
    sub = parser.add_subparsers(dest="command", required=True)

    for command, (_, help_text) in _COMMANDS.items():
        sub.add_parser(command, help=help_text)

    fake = sub.add_parser("run-fake", help="run the local framework smoke environment")
    fake.add_argument("--output", default="./runs/demo")
    fake.add_argument("--config", default="./configs/config.json")
    fake.add_argument("--experiment-config", default=None)
    fake.add_argument("--modules-config", default=None, help="deprecated manifest override")
    fake.add_argument("--tools-config", default=None, help="deprecated manifest override")

    modules_check = sub.add_parser("modules-check", help="validate the resident module manifest")
    modules_check.add_argument("--config", default="./configs/config.json")

    tools_check = sub.add_parser("tools-check", help="validate the resident tool manifest")
    tools_check.add_argument("--config", default="./configs/config.json")

    graphs_check = sub.add_parser("graphs-check", help="validate a JSON graph manifest")
    graphs_check.add_argument("--config", default="./configs/config.json")

    monitors_check = sub.add_parser("monitors-check", help="validate the monitor registry manifest")
    monitors_check.add_argument("--config", default="./configs/config.json")

    modules = sub.add_parser("modules", help="list registered modules")
    modules.add_argument("--module", action="append", default=[], dest="plugin_modules")

    tools = sub.add_parser("tools", help="list registered tools")
    tools.add_argument("--tool", action="append", default=[], dest="plugin_tools")

    args = parser.parse_args(argv)
    if args.command == "run-fake":
        print(json.dumps(asyncio.run(run_fake(
            args.output,
            args.modules_config,
            args.tools_config,
            config=args.config,
            experiment_config=args.experiment_config,
        )), indent=2))
    elif args.command == "modules-check":
        from .runtime.config import load_modules_config
        from .runtime.factory import build_module_runtime

        runtime = build_module_runtime(load_modules_config(args.config))
        print(json.dumps({"modules": runtime.describe(), "status": "ok"}, indent=2))
    elif args.command == "graphs-check":
        from .workflow import load_graphs_config

        config = load_graphs_config(args.config)
        print(json.dumps({"graphs": [graph.id for graph in config.graphs], "status": "ok"}, indent=2))
    elif args.command == "tools-check":
        from .runtime.config import load_application_config
        from .runtime.factory import build_tool_runtime

        loaded = load_application_config(args.config)
        runtime = build_tool_runtime(loaded.tools, runtime_params=loaded.params)
        print(json.dumps({"tools": runtime.describe(), "status": "ok"}, indent=2, default=str))
    elif args.command == "monitors-check":
        from .runtime.config import load_application_config, load_monitors_config
        from .runtime.factory import build_monitor_runtime

        # A master config is preferred; a direct monitor manifest remains
        # convenient for plugin development.
        try:
            loaded = load_application_config(args.config)
            manifest = loaded.monitors
            params = loaded.params
        except (ValueError, FileNotFoundError):
            manifest = load_monitors_config(args.config)
            params = None
        runtime = build_monitor_runtime(manifest, params=params)
        print(json.dumps({"monitors": runtime.describe(), "status": "ok"}, indent=2, default=str))
    elif args.command == "modules":
        from .runtime.factory import load_plugin_modules
        from .runtime.registry import load_builtin_components, module_registry

        load_builtin_components()
        load_plugin_modules(args.plugin_modules)
        print(json.dumps({"modules": module_registry.describe()}, indent=2))
    elif args.command == "tools":
        from .runtime.factory import load_plugin_tools
        from .tools import load_builtin_tools, tool_registry

        load_builtin_tools()
        load_plugin_tools(args.plugin_tools)
        print(json.dumps({"tools": tool_registry.describe()}, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
