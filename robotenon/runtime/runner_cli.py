"""Thin unified CLI adapter for the runtime experiment APIs."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
from pathlib import Path
from typing import Any

from .experiment import ExperimentRequest, run_experiment
from .runner import EpisodeRequest, run_episode
from .terminal import TerminalLogger


def _json_object(value: str) -> dict[str, Any]:
    try:
        result = json.loads(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a JSON object") from exc
    if not isinstance(result, dict):
        raise argparse.ArgumentTypeError("expected a JSON object")
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="Tenon run",
        description="Run a single episode or an ordered Task Adapter experiment from one RoboTenon config."
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="print read-only startup diagnostics without starting services",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="run all enabled Task Adapters in config order",
    )
    parser.add_argument("--config", default="configs/config.json")
    parser.add_argument(
        "--config-snapshot",
        default=None,
        help="frozen merged configuration for this run",
    )
    parser.add_argument("--experiment-config", default=None)
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--env-id", default=None)
    parser.add_argument("--split", default=None, help="provider-defined split name")
    parser.add_argument(
        "--environment-parameters",
        type=_json_object,
        default=None,
        help="JSON object of provider-owned session options",
    )
    parser.add_argument(
        "--task-adapter-overrides",
        type=_json_object,
        default=None,
        help="batch JSON object mapping adapter id/name to constructor overrides",
    )
    parser.add_argument("--task-set", default=None)
    parser.add_argument("--task-name", action="append", default=None)
    parser.add_argument("--max-tasks", type=int, default=None)
    parser.add_argument("--num-trials", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--horizon", type=int, default=None)
    parser.add_argument("--model-path", default=None)
    parser.add_argument("--model-host", default=None)
    parser.add_argument("--model-port", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--save-videos", action="store_true")
    parser.add_argument("--save-traces", action="store_true")
    parser.add_argument("--video-path", default=None)
    parser.add_argument("--video-stride", type=int, default=None)
    parser.add_argument("--video-fps", type=int, default=None)
    parser.add_argument("--camera-width", type=int, default=None)
    parser.add_argument("--camera-height", type=int, default=None)
    parser.add_argument("--trace-dir", default=None)
    parser.add_argument(
        "--metrics-dir",
        default=None,
        help="persist custom MetricsClient samples under this directory",
    )
    parser.add_argument("--memory", default=None)
    parser.add_argument("--graph-id", default=None)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress human-readable progress logs; keep the final JSON output",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI colors in terminal logs",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="include per-case artifact paths and per-adapter summaries",
    )
    args = parser.parse_args(argv)
    if args.task_adapter_overrides is not None and not args.batch:
        parser.error("--task-adapter-overrides requires --batch")
    return args


def request_from_args(args: argparse.Namespace) -> EpisodeRequest:
    return EpisodeRequest(
        config=args.config,
        config_snapshot=args.config_snapshot,
        environment_parameters=getattr(args, "environment_parameters", None),
        experiment_config=args.experiment_config,
        task_id=args.task_id,
        env_id=args.env_id,
        split=args.split,
        seed=args.seed,
        horizon=args.horizon,
        model_path=args.model_path,
        model_host=args.model_host,
        model_port=args.model_port,
        dry_run=args.dry_run,
        output=args.output,
        video_path=args.video_path,
        video_stride=args.video_stride,
        video_fps=args.video_fps,
        camera_width=args.camera_width,
        camera_height=args.camera_height,
        trace_dir=args.trace_dir,
        metrics_dir=args.metrics_dir,
        memory=args.memory,
        graph_id=args.graph_id,
    )


def experiment_request_from_args(args: argparse.Namespace) -> ExperimentRequest:
    return ExperimentRequest(
        config=args.config,
        config_snapshot=args.config_snapshot,
        environment_parameters=getattr(args, "environment_parameters", None),
        task_adapter_overrides=getattr(args, "task_adapter_overrides", None),
        experiment_config=args.experiment_config,
        model_path=args.model_path,
        model_host=args.model_host,
        model_port=args.model_port,
        split=args.split,
        task_set=args.task_set,
        task_names=list(args.task_name) if args.task_name else None,
        max_tasks=args.max_tasks,
        num_trials=args.num_trials,
        seed=args.seed,
        horizon=args.horizon,
        dry_run=args.dry_run,
        save_videos=args.save_videos,
        save_traces=args.save_traces,
        video_stride=args.video_stride,
        video_fps=args.video_fps,
        camera_width=args.camera_width,
        camera_height=args.camera_height,
        output_dir=args.output_dir,
        metrics_dir=args.metrics_dir,
        memory=args.memory,
        run_id=args.run_id,
        graph_id=args.graph_id,
    )


def terminal_from_args(args: argparse.Namespace) -> TerminalLogger:
    return TerminalLogger(
        enabled=not bool(getattr(args, "quiet", False)),
        color=False if bool(getattr(args, "no_color", False)) else None,
        verbose=bool(getattr(args, "verbose", False)),
    )


async def run(
    args: argparse.Namespace,
    *,
    terminal: TerminalLogger | None = None,
) -> dict[str, Any]:
    terminal = terminal or terminal_from_args(args)
    if args.batch:
        request = experiment_request_from_args(args)
        terminal.start_batch(request)
        result = await run_experiment(request, terminal=terminal)
        terminal.batch_complete(result)
        return result
    request = request_from_args(args)
    terminal.start_single(request)
    result = await run_episode(request, terminal=terminal)
    terminal.single_complete(result)
    return result


async def _run_with_signals(args, terminal):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    installed = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, task.cancel)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        return await run(args, terminal=terminal)
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.preflight:
        from dataclasses import replace

        from .config import load_request_config
        from .preflight import check_preflight
        from .runner import _apply_request_overrides

        episode_request = request_from_args(args)
        batch_request = None
        output_file = None
        if args.batch:
            batch_request = experiment_request_from_args(args)
            loaded = load_request_config(batch_request)
            output_root = (
                batch_request.output_dir
                or loaded.params.resolve("runtime.output_dir")
                or "data/runs/eval"
            )
        else:
            loaded = load_request_config(episode_request)
            output_file = (
                args.output
                or loaded.params.resolve("runtime.output_path")
                or "data/runs/result.json"
            )
            output_root = Path(output_file).expanduser().parent
        loaded = replace(
            loaded,
            params=_apply_request_overrides(loaded.params, episode_request),
        )
        result = check_preflight(
            loaded,
            output_root=output_root,
            graph_id=episode_request.graph_id,
            task_adapter_overrides=(
                batch_request.task_adapter_overrides
                if batch_request is not None
                else None
            ),
            experiment_request=batch_request,
            batch=args.batch,
            output_file=output_file,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    terminal = terminal_from_args(args)
    try:
        result = asyncio.run(_run_with_signals(args, terminal))
    except Exception as exc:
        terminal.failed(exc)
        raise
    if args.batch:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("num_errors", 0) == 0 else 1
    output = Path(str(result["output_path"]))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "context"},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0 if result["task_success"] or result["dry_run"] else 1


__all__ = [
    "experiment_request_from_args",
    "main",
    "parse_args",
    "request_from_args",
    "run",
    "terminal_from_args",
]
