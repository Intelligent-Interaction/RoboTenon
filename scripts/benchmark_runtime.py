"""Repeatable synthetic runtime/Portal baseline; never connects to hardware.

Run from the checkout being measured. Output records raw samples and workload
parameters; wall-clock results are local measurements, not GPU capacity claims.
"""

import argparse
import asyncio
import http.client
import json
import math
import platform
import sqlite3
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path.cwd()))


def percentiles(samples):
    values = sorted(samples)
    return {
        label: round(
            values[min(len(values) - 1, max(0, math.ceil(len(values) * fraction) - 1))],
            3,
        )
        for label, fraction in [("p50", 0.50), ("p95", 0.95), ("p99", 0.99)]
    }


async def batch_benchmark(root, count, workers):
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
                    task_id="bench",
                    environment_id="fake",
                    index=i,
                    episode=i,
                )

    class Backend:
        max_workers = workers

        async def execute(self, configured, case, context):
            await asyncio.sleep(0.001)
            return {"task_success": True, "env_steps": 1}

    writes = {"bytes": 0, "calls": 0, "summary_bytes": 0, "summary_calls": 0}
    original = evaluator.write_json

    def measured(path, value):
        original(path, value)
        size = path.stat().st_size
        writes["bytes"] += size
        writes["calls"] += 1
        if path.name == "summary.json":
            writes["summary_bytes"] += size
            writes["summary_calls"] += 1

    evaluator.write_json = measured
    started = time.perf_counter()
    backlog = []
    try:
        result = await evaluator.evaluate_task_adapters(
            config_path=root / "config.json",
            params=Params({}),
            adapters=[
                ConfiguredTaskAdapter("bench", SimpleNamespace(name="bench"), Adapter())
            ],
            output_dir=root,
            run_id="batch",
            run_episode=None,
            execution_backend=Backend(),
            options={"memory_mode": "disabled"},
            on_case_complete=lambda done, total, record: backlog.append(
                [round(time.perf_counter() - started, 4), total - done]
            ),
        )
    finally:
        evaluator.write_json = original
    elapsed = time.perf_counter() - started
    return {
        "cases": count,
        "workers": workers,
        "elapsed_s": elapsed,
        "tasks_per_s": count / elapsed,
        "completed": result["num_episodes"],
        "writes": writes,
        "writes_scope": "JSON serialization only; SQLite writes excluded",
        "backlog": backlog,
        "index_retained_bytes": sum(
            p.stat().st_size for p in (root / "batch").glob("episodes.sqlite3*")
        ),
        "retained_bytes": sum(
            p.stat().st_size for p in (root / "batch").rglob("*") if p.is_file()
        ),
    }


async def action_benchmark(count=200):
    from robotenon.runtime.safety import SafetyStateStore
    from robotenon.schemas import Action, ActionChunk
    from robotenon.services.body import InProcessBodyService

    async def driver(action):
        await asyncio.sleep(0.001)

    body = InProcessBodyService(driver, safety_state=SafetyStateStore())
    sid, samples = uuid4(), []
    try:
        for _ in range(count):
            chunk = ActionChunk(session_id=sid, actions=[Action(session_id=sid)])
            started = time.perf_counter()
            result = await body.execute(chunk)
            assert result.status == "succeeded"
            samples.append((time.perf_counter() - started) * 1000)
    finally:
        await body.close()
    return {"samples_ms": samples, **percentiles(samples)}


def portal_benchmark(root, history=300):
    from robotenon.web.experiments import ExperimentManager
    from robotenon.web.query import PortalData
    from robotenon.web.server import build_server

    manager = ExperimentManager(project_root=root, output_root=root / "portal")
    manager._dispatch_waiting = lambda: None
    for i in range(history):
        run = root / "portal" / str(i)
        manager._jobs[str(i)] = {
            "job_id": str(i),
            "status": "waiting",
            "submitted_at": str(i).zfill(8),
            "output_root": str(run),
            "status_path": str(run / "status.json"),
            "summary_path": str(run / "summary.json"),
        }
    target = root / "portal/stop"
    manager._jobs["stop"] = {
        "job_id": "stop",
        "status": "running",
        "output_root": str(target),
        "status_path": str(target / "status.json"),
        "summary_path": str(target / "summary.json"),
    }
    entered = threading.Event()

    def terminate(*args, **kwargs):
        entered.set()
        time.sleep(0.5)
        return {"terminated": True, "forced": False, "pid": None}

    manager._terminate_process_group = terminate
    data = PortalData(root / "metrics.jsonl", root / "trace.jsonl")
    server = build_server("127.0.0.1", 0, data, experiment_manager=manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path):
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        started = time.perf_counter()
        try:
            client.request(
                method,
                path,
                body="{}" if method == "POST" else None,
                headers={"Content-Type": "application/json"},
            )
            response = client.getresponse()
            payload = response.read()
            assert response.status in {200, 202}, payload
            return (
                (time.perf_counter() - started) * 1000,
                len(payload),
                json.loads(payload),
            )
        finally:
            client.close()

    stop_result = []
    stop = threading.Thread(
        target=lambda: stop_result.append(request("POST", "/api/experiments/stop/stop"))
    )
    try:
        before = [request("GET", "/api/experiments?limit=25") for _ in range(5)]
        stop.start()
        assert entered.wait(2)
        during = [request("GET", "/api/experiments?limit=25") for _ in range(8)]
        stop.join(3)
        deadline = time.monotonic() + 3
        while (
            manager._jobs["stop"]["status"] == "stopping"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        return {
            "history": history,
            "normal_ms": percentiles([x[0] for x in before]),
            "during_stop_ms": percentiles([x[0] for x in during]),
            "during_stop_samples_ms": [x[0] for x in during],
            "stop_response_ms": stop_result[0][0],
            "list_bytes": before[0][1],
            "list_items": len(before[0][2]["experiments"]),
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        manager.close()
        data.traces.close()


def cache_benchmark(root, rounds=3, files=400):
    from robotenon.web.experiments import ExperimentManager

    manager = ExperimentManager(project_root=root, output_root=root / "cache")
    tracemalloc.start()
    samples = []
    for cycle in range(rounds):
        for i in range(files):
            path = root / "cache" / f"{cycle}-{i}.json"
            path.write_text(json.dumps({"payload": "x" * 8192}))
            manager._read_json_cached(path)
        current, peak = tracemalloc.get_traced_memory()
        samples.append(
            {
                "round": cycle + 1,
                "cached_entries": len(manager._json_cache),
                "allocated_bytes": current,
                "peak_bytes": peak,
                "disk_bytes": sum(
                    p.stat().st_size for p in (root / "cache").glob("*.json")
                ),
            }
        )
    tracemalloc.stop()
    return {"rounds": rounds, "files_per_round": files, "samples": samples}


def trace_benchmark(root):
    from robotenon.infra.trace_query import TraceQuery

    db = root / "trace-query.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE samples(id INTEGER PRIMARY KEY, value TEXT)")
    connection.executemany(
        "INSERT INTO samples VALUES (?,?)", ((i, "x" * 256) for i in range(20000))
    )
    connection.commit()
    connection.close()
    started = time.perf_counter()
    reader = TraceQuery(db)
    page = (
        reader.sql_page("SELECT * FROM samples", limit=1000)
        if hasattr(reader, "sql_page")
        else {"rows": reader.sql("SELECT * FROM samples"), "truncated": False}
    )
    rows = page["rows"]
    return {
        "elapsed_ms": (time.perf_counter() - started) * 1000,
        "returned_rows": len(rows),
        "returned_bytes": len(json.dumps(rows)),
        "truncated": page["truncated"],
        "requested_limit": 1000,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--cases", type=int, default=300)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="robotenon-bench-", dir="/tmp") as directory:
        root = Path(directory)
        results = {
            "platform": platform.platform(),
            "python": sys.version,
            "cwd": str(Path.cwd()),
            "workload": "synthetic fake tasks/driver, loopback HTTP, temporary local files; no GPU or hardware",
            "batch": asyncio.run(batch_benchmark(root, args.cases, args.workers)),
            "action": asyncio.run(action_benchmark()),
            "portal": portal_benchmark(root),
            "cache_growth": cache_benchmark(root),
            "trace": trace_benchmark(root),
        }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: value
                for key, value in results.items()
                if key not in {"batch", "action", "cache_growth"}
            },
            indent=2,
        )
    )
    print(
        json.dumps(
            {
                "tasks_per_s": results["batch"]["tasks_per_s"],
                "writes": results["batch"]["writes"],
                "action_ms": {
                    key: results["action"][key] for key in ("p50", "p95", "p99")
                },
                "cache_growth": results["cache_growth"],
            }
        )
    )


if __name__ == "__main__":
    main()
