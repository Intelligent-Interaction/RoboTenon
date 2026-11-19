"""Measure real resident Worker idle CPU/RSS and the parent IPC receive delay.

No model or robot is started. Requires psutil. Run from the checkout measured.
"""

import argparse
import asyncio
import json
import multiprocessing
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path.cwd()))


def fake_config(root):
    value = {
        "modules": [{"name": "echo", "id": "echo"}],
        "tools": [],
        "monitors": [],
        "bodies": [],
        "environments": [{"name": "fake", "id": "env"}],
        "task_adapters": [{"name": "single-task"}],
        "graphs": [
            {
                "id": "test",
                "entry": "echo",
                "nodes": [{"id": "echo", "module": "echo"}],
                "outputs": {
                    "success": {
                        "source": "nodes.echo.outputs.success",
                        "default": False,
                    }
                },
            }
        ],
        "parameters": {
            "runtime": {
                "graph_id": "test",
                "environment_id": "env",
                "monitor_enabled": False,
            }
        },
        "observation_cache": {"enabled": False},
        "safety_state": {"backend": "memory"},
    }
    path = root / "config.json"
    path.write_text(json.dumps(value))
    return path


async def idle(root, count, seconds):
    import psutil

    from robotenon.runtime.execution import ExecutionOptions
    from robotenon.runtime.parallel import ProcessSlot
    from robotenon.runtime.runner import EpisodeRequest

    profile = EpisodeRequest(config=fake_config(root), output=root / "result.json")
    options = ExecutionOptions(
        backend="process", max_workers=count, shutdown_timeout_s=1
    )
    slots = [ProcessSlot(profile, None, options) for _ in range(count)]
    try:
        await asyncio.gather(*(slot.start() for slot in slots))
        procs = [psutil.Process(slot.process.pid) for slot in slots]
        await asyncio.sleep(0.3)

        def measure():
            return [
                {
                    "cpu_s": p.cpu_times().user + p.cpu_times().system,
                    "rss": p.memory_info().rss,
                }
                for p in procs
            ]

        before, start = measure(), time.perf_counter()
        await asyncio.sleep(seconds)
        after, elapsed = measure(), time.perf_counter() - start
        cpu = sum(b["cpu_s"] - a["cpu_s"] for a, b in zip(before, after))
        return {
            "workers": count,
            "idle_s": elapsed,
            "cpu_s": cpu,
            "cpu_core_percent": 100 * cpu / elapsed,
            "rss_before": sum(p["rss"] for p in before),
            "rss_after": sum(p["rss"] for p in after),
            "receive_mode": "connection_readiness"
            if hasattr(
                __import__("robotenon.runtime.parallel", fromlist=["_wait_readable"]),
                "_wait_readable",
            )
            else "20ms_polling",
        }
    finally:
        await asyncio.gather(*(slot.close() for slot in slots))


async def parent_receive(samples=50):
    from benchmark_runtime import percentiles

    from robotenon.runtime.parallel import ProcessSlot

    parent, child = multiprocessing.Pipe()
    slot = ProcessSlot(SimpleNamespace(output=None), None, None)
    slot.connection = parent
    slot.process = SimpleNamespace(is_alive=lambda: True)
    delays = []
    try:
        for _ in range(samples):

            async def send():
                await asyncio.sleep(0.003)
                child.send(("result", None, time.perf_counter()))

            producer = asyncio.create_task(send())
            message = await slot.receive(1)
            delays.append((time.perf_counter() - message[2]) * 1000)
            await producer
    finally:
        parent.close()
        child.close()
    return {
        "samples_ms": delays,
        **percentiles(delays),
        "scope": "actual parent receive path; synthetic Pipe sender",
    }


async def main_async(args):
    with tempfile.TemporaryDirectory(prefix="rc-worker-bench-") as directory:
        root = Path(directory)
        measured = [await idle(root, count, args.idle_seconds) for count in (1, 4, 8)]
        return {
            "idle": measured,
            "parent_receive": await parent_receive(),
            "scope": "real idle WorkerRuntime/process slots, fake configuration, no model/GPU workload",
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--idle-seconds", type=float, default=3)
    args = parser.parse_args()
    result = asyncio.run(main_async(args))
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["idle"], indent=2))
