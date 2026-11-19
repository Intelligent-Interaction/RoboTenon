"""Real XR-1 + RoboCasa365 throughput comparison across separate Python stacks.

Run from repository root with the model Python. Shared assets are read-only;
all configs, traces, initialization audits and measurements go under --output.
"""

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from robotenon_benchmark_support.configure import (
    application_config,
    enable_batch_execution,
    write_application_config,
)

from robotenon.runtime.batch_services import SharedBatchServices


async def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    modes = [
        ("serial", 1, "single"),
        ("parallel_single", args.workers, "single"),
        ("parallel_batch", args.workers, "batch"),
    ]
    if args.only:
        modes = [item for item in modes if item[0] == args.only]
    for repeat in range(args.repeats):
        for label, workers, mode in modes if repeat % 2 == 0 else list(reversed(modes)):
            name = f"{label}-{repeat}"
            directory = args.output / name
            directory.mkdir(parents=True, exist_ok=True)
            service_config = [
                {
                    "name": "vla",
                    "factory": "robotenon.models.vla:VLAProvider",
                    "params": {
                        "factory": "robotenon_robocasa365._internal.batching:create_batch",
                        "interface": "batch",
                        "params": {
                            "model_path": args.model_path,
                            "metrics_dir": str(directory / "model-forwards"),
                            "warmup_observations": args.warmup_observations,
                            "warmup_batch_size": args.batch_size,
                        },
                    },
                    "devices": args.model_devices.split(","),
                    "mode": mode,
                    "max_batch_size": args.batch_size,
                    "max_wait_ms": args.wait_ms,
                }
            ]
            (directory / "model-service.json").write_text(
                json.dumps(service_config, indent=2)
            )
            services = SharedBatchServices(
                service_config,
                directory / "batch-metrics",
                startup_timeout_s=600,
                shutdown_timeout_s=30,
            )
            started = time.perf_counter()
            try:
                await services.start()
                startup = time.perf_counter() - started
                # External endpoints permit simulator workers to use a different
                # interpreter. Spread Workers evenly across model replicas; each
                # replica owns its own dynamic batch queue on one GPU.
                endpoints = [path for _, path in services.endpoints["vla"]]
                worker_endpoints = [
                    endpoints[index % len(endpoints)] for index in range(workers)
                ]
                simulation_devices = args.sim_device.split(",")
                worker_devices = [
                    simulation_devices[index % len(simulation_devices)]
                    for index in range(workers)
                ]
                profile = {
                    "benchmark_id": "xiaomi_speed_benchmark",
                    "episodes_per_task": args.episodes,
                    "max_steps": args.steps,
                    "params": {
                        "tasks": args.tasks.split(","),
                        "seed": 0,
                        "camera_size": 256,
                        "max_steps": args.steps,
                        "success_check_interval": 16,
                        "audit_dir": str(directory / "initialization"),
                    },
                }
                config = application_config(
                    profile,
                    policy_factory="robotenon_robocasa365._internal.batching:create_batch",
                )
                for field in (
                    "plugin_environments",
                    "plugin_bodies",
                    "plugin_task_adapters",
                ):
                    config[field] = ["robotenon_robocasa365._internal.speed_benchmark"]
                config["safety_state"] = {"backend": "memory", "require_shared": False}
                enable_batch_execution(
                    config,
                    workers=workers,
                    worker_devices=worker_devices,
                    inference_mode=mode,
                    max_batch_size=args.batch_size,
                    max_wait_ms=args.wait_ms,
                )
                execution = config["parameters"]["runtime"]["execution"]
                execution.update(
                    batch_services=[],
                    external_batch_services={"vla": worker_endpoints},
                    egl_device_ids=worker_devices,
                    startup_timeout_s=240,
                    worker_startup_parallelism=min(
                        args.startup_parallelism, workers
                    ),
                    native_reset_parallelism=min(
                        args.startup_parallelism, workers
                    ),
                    native_reset_gate=str(args.output / ".native-startup-gate"),
                    task_timeout_s=900,
                    shutdown_timeout_s=15,
                )
                config["plugin_tools"].append("robotenon_robocasa365._internal.runtime")
                config["tools"][0]["name"] = "xiaomi_native_batch"
                config["tools"][0]["params"].update(
                    endpoint=endpoints[0], timeout_s=180
                )
                path = directory / "config.json"
                write_application_config(config, path)
                environment = dict(
                    os.environ,
                    CUDA_VISIBLE_DEVICES=args.sim_device,
                    MUJOCO_GL="egl",
                    PYOPENGL_PLATFORM="egl",
                )
                start = time.perf_counter()
                with (directory / "simulation.log").open("w") as log:
                    process = await asyncio.create_subprocess_exec(
                        args.sim_python,
                        "-m",
                        "robotenon_benchmark_support.run",
                        "--config",
                        str(path),
                        "--output-dir",
                        str(directory / "runs"),
                        "--run-id",
                        "evaluation",
                        env=environment,
                        stdout=log,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    try:
                        code = await asyncio.wait_for(process.wait(), 1800)
                    finally:
                        if process.returncode is None:
                            process.terminate()
                            await process.wait()
                wall = time.perf_counter() - start
                if code:
                    raise RuntimeError(
                        f"{name} simulation failed; see {directory}/simulation.log"
                    )
            finally:
                await services.close()
            report = json.loads(
                (directory / "runs/evaluation/summary.json").read_text()
            )
            if report["num_errors"]:
                raise RuntimeError(f"{name} has evaluation errors")
            records = [
                json.loads(p.read_text())
                for p in (directory / "model-forwards").glob("*.json")
            ]
            forwards = [item for items in records for item in items]
            samples = sum(item["batch_size"] for item in forwards)
            steps = args.episodes * len(args.tasks.split(",")) * args.steps
            row = {
                "name": name,
                "mode": mode,
                "workers": workers,
                "model_devices": args.model_devices.split(","),
                "model_replicas": len(args.model_devices.split(",")),
                "simulation_devices": args.sim_device.split(","),
                "repeat": repeat,
                "model_startup_sec": startup,
                "evaluation_wall_sec": wall,
                "episodes": report["num_episodes"],
                "successes": report["successes"],
                "environment_steps": steps,
                "steps_per_sec": steps / wall,
                "episodes_per_hour": report["num_episodes"] * 3600 / wall,
                "model_forward_calls": len(forwards),
                "model_forward_samples": samples,
                "model_inference_sec": sum(f["inference_sec"] for f in forwards),
                "model_total_sec": sum(f["total_sec"] for f in forwards),
                "model_batch_sizes": [f["batch_size"] for f in forwards],
            }
            rows.append(row)
            (args.output / "measurements.json").write_text(json.dumps(rows, indent=2))
            print(json.dumps(row), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--warmup-observations", required=True)
    parser.add_argument("--sim-python", required=True)
    parser.add_argument("--model-devices", default="1")
    parser.add_argument("--sim-device", default="2")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--wait-ms", type=float, default=10)
    parser.add_argument(
        "--startup-parallelism",
        type=int,
        default=8,
        help="Workers allowed to import and initialize native simulators at once",
    )
    parser.add_argument("--episodes", type=int, default=3, help="Episodes per task")
    parser.add_argument("--steps", type=int, default=64)
    parser.add_argument("--tasks", default="CloseBlenderLid,OpenDrawer")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument(
        "--only", choices=["serial", "parallel_single", "parallel_batch"]
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.startup_parallelism < 1:
        parser.error("--startup-parallelism must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
