"""Synthetic history scale and batch-memory comparison; no models or hardware.

Run this script with cwd set to the checkout being measured. Legacy import is
prepared once; registry startup measures subsequent restarts, not migration.
"""

import argparse
import asyncio
import gc
import json
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path.cwd()))


def registry(root, count):
    from robotenon.web.experiments import ExperimentManager

    root.mkdir()
    jobs = [
        {
            "job_id": f"job-{i:06}",
            "status": "completed",
            "resources_released": True,
            "submitted_at": f"2026-09-20T00:00:{i % 60:02}+00:00",
            "parameters": {"payload": "x" * 2048},
            "output_root": str(root / str(i)),
            "summary_path": str(root / str(i) / "summary.json"),
            "status_path": str(root / str(i) / "status.json"),
        }
        for i in range(count)
    ]
    legacy = root / "jobs.json"
    legacy.write_text(json.dumps(jobs))
    del jobs
    # Prepare the one-time migration before measuring a normal Portal restart.
    first = ExperimentManager(project_root=root, output_root=root)
    first.close()
    del first
    gc.collect()
    tracemalloc.start()
    start = time.perf_counter()
    manager = ExperimentManager(project_root=root, output_root=root)
    startup = (time.perf_counter() - start) * 1000
    allocated = tracemalloc.get_traced_memory()[0]
    manager._refresh_job = lambda key: manager._jobs[key]
    samples = []
    for _ in range(5):
        start = time.perf_counter()
        page = manager.jobs_page(limit=25)
        samples.append((time.perf_counter() - start) * 1000)
        assert page["total"] == count and len(page["experiments"]) == 25
    original_size = legacy.stat().st_size
    manager._jobs["job-000000"]["status"] = "cancelled"
    start = time.perf_counter()
    manager._persist_jobs()
    update = (time.perf_counter() - start) * 1000
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    manager.close()
    return {
        "records": count,
        "startup_ms": startup,
        "startup_allocated_bytes": allocated,
        "peak_allocated_bytes": peak,
        "list_25_median_ms": statistics.median(samples),
        "list_25_samples_ms": samples,
        "single_update_ms": update,
        "legacy_json_bytes": original_size,
        "sqlite_registry": (root / "jobs.sqlite3").exists(),
    }


async def memory(root, count):
    from robotenon.infra.params import Params
    from robotenon.task_adapters import evaluator
    from robotenon.task_adapters.base import (
        BaseTaskAdapter,
        ConfiguredTaskAdapter,
        TaskCase,
    )

    class Adapter(BaseTaskAdapter):
        def enumerate_cases(self, *, adapter_id, params):
            for i in range(count):
                yield TaskCase(
                    adapter_id=adapter_id,
                    task_id="memory",
                    environment_id="fake",
                    index=i,
                    episode=i,
                )

    class Backend:
        max_workers = 4

        async def execute(self, configured, case, context):
            await asyncio.sleep(0.001)
            return {
                "task_success": True,
                "context": {"raw_output": str(case.index % 10) * (256 * 1024)},
            }

    gc.collect()
    tracemalloc.start()
    start = time.perf_counter()
    result = await evaluator.evaluate_task_adapters(
        config_path=root / "config.json",
        params=Params({}),
        adapters=[ConfiguredTaskAdapter("a", SimpleNamespace(name="fake"), Adapter())],
        output_dir=root,
        run_id="batch",
        run_episode=None,
        execution_backend=Backend(),
        options={"memory_mode": "disabled"},
    )
    elapsed = time.perf_counter() - start
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert result["num_episodes"] == count
    return {
        "cases": count,
        "payload_bytes_per_case": 256 * 1024,
        "workers": 4,
        "elapsed_s": elapsed,
        "peak_allocated_bytes": peak,
        "final_allocated_bytes": current,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--history", type=int, default=20000)
    parser.add_argument("--cases", type=int, default=100)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="rc-history-benchmark-") as tmp:
        root = Path(tmp).resolve()
        result = {
            "checkout": str(Path.cwd()),
            "workload": "synthetic registry and 256 KiB fake results; tracemalloc enabled",
            "registry": registry(root / "registry", args.history),
            "memory": asyncio.run(memory(root, args.cases)),
        }
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
