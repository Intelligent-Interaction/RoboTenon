"""Read-only framework audit: all runtime artifacts are created in a temporary directory."""

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import os
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))


def source_hashes():
    return {
        str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
        for folder in ("robosys", "configs", "scripts", "tests")
        for path in (REPO / folder).rglob("*")
        if path.is_file() and path.suffix in {".py", ".json", ".js", ".html"}
    }


def config_typos(root):
    from robosys.runtime.config import ToolConfig
    from robosys.workflow import load_graphs_config

    graph = load_graphs_config(
        {
            "graphs": [
                {
                    "id": "main",
                    "entry": "stop",
                    "max_step": 2,
                    "nodes": [
                        {
                            "id": "stop",
                            "operation": "graph.stop",
                            "inptus": {"value": "inputs.value"},
                        }
                    ],
                }
            ]
        }
    ).graphs[0]
    tool = ToolConfig.model_validate({"name": "audit-provider", "enabeld": False})
    assert graph.max_steps == 1000 and graph.nodes[0].inputs is None and tool.enabled
    return {
        "accepted": True,
        "max_step_typo_effective_max_steps": graph.max_steps,
        "inptus_typo_effective_inputs": graph.nodes[0].inputs,
        "enabeld_false_typo_effective_enabled": tool.enabled,
    }


def cli_mode(root):
    from robosys.cli import main as public_main
    from robosys.runtime.runner_cli import (
        experiment_request_from_args,
        parse_args,
        request_from_args,
    )

    args = parse_args(["--batch", "--graph-id", "alternate"])
    single, batch = request_from_args(args), experiment_request_from_args(args)
    assert single.graph_id == "alternate" and not hasattr(batch, "graph_id")
    error = io.StringIO()
    with contextlib.redirect_stderr(error):
        try:
            public_main(["web", "--max-queries", "4"])
        except SystemExit as exc:
            public_exit = exc.code
        else:
            raise AssertionError(
                "expected the short web entrypoint to reject the capacity flag"
            )
    assert public_exit == 2
    return {
        "parsed_graph_id": args.graph_id,
        "preflight_episode_request_graph_id": single.graph_id,
        "batch_request_has_graph_id": hasattr(batch, "graph_id"),
        "robosys_web_max_queries_exit": public_exit,
        "robosys_web_max_queries_error": error.getvalue(),
    }


def preflight(root):
    from robosys.runtime.config import load_application_config
    from robosys.runtime.experiment import run_experiment
    from robosys.runtime.preflight import check_preflight
    from robosys.runtime.runner_cli import (
        experiment_request_from_args,
        main,
        parse_args,
    )

    config = {
        "modules": [],
        "tools": [],
        "task_adapters": [{"name": "single", "id": "one"}],
        "graphs": [
            {
                "id": "main",
                "entry": "stop",
                "nodes": [{"id": "stop", "operation": "graph.stop"}],
                "outputs": {"success": {"source": "inputs.success", "default": False}},
            }
        ],
        "parameters": {"runtime": {"graph_id": "main"}},
    }
    path = root / "config.json"
    path.write_text(json.dumps(config))
    argv = [
        "--config",
        str(path),
        "--batch",
        "--task-adapter-overrides",
        '{"missing":{}}',
        "--output-dir",
        str(root / "output"),
    ]
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main([*argv, "--preflight"])
    report = json.loads(buffer.getvalue())
    try:
        asyncio.run(run_experiment(experiment_request_from_args(parse_args(argv))))
    except ValueError as exc:
        actual_error = str(exc)
    else:
        raise AssertionError("expected unknown override target to fail")
    assert code == 0 and report["ok"] and "unknown task adapter" in actual_error
    config["parameters"]["model"] = {"host": "127.0.0.1", "port": 70000}
    invalid_port = check_preflight(
        load_application_config(config), output_root=root, probe_gpu=False
    )
    file_parent = root / "file-parent"
    file_parent.write_text("not a directory")
    invalid_output = check_preflight(
        load_application_config(config),
        output_root=file_parent / "result",
        probe_gpu=False,
    )
    assert invalid_port["ok"] and invalid_output["ok"]
    return {
        "unknown_override_preflight_exit": code,
        "unknown_override_preflight_ok": report["ok"],
        "actual_batch_error": actual_error,
        "invalid_port_70000": invalid_port,
        "output_parent_is_file": invalid_output,
    }


def oversized_summary(root):
    from robosys.infra.trace_query import QueryBudgetExceeded
    from robosys.web.experiments import ExperimentManager

    output = root / "runs"
    manager = ExperimentManager(project_root=root, output_root=output)
    run = output / "old"
    (run / ".control").mkdir(parents=True)
    (run / ".control/run.lock").touch()
    (run / "episode_000.json").write_text(
        json.dumps(
            {
                "adapter_id": "one",
                "global_index": 0,
                "status": "completed",
                "task_success": True,
            }
        )
    )
    summary = run / "summary.json"
    summary.write_text(json.dumps({"padding": "x" * (16 * 1024 * 1024)}))
    manager._jobs["old"] = {
        "job_id": "old",
        "status": "completed",
        "resources_released": True,
        "submitted_at": "2026-09-21T00:00:00+00:00",
        "output_root": str(run),
        "status_path": str(run / "status.json"),
        "summary_path": str(summary),
    }
    try:
        try:
            manager.get_job("old")
        except QueryBudgetExceeded as exc:
            error = str(exc)
        else:
            raise AssertionError("expected oversized legacy summary rejection")
        state = manager._jobs["old"].get("index_recovery")
        assert state is None and manager._recovery_queue.empty()
        return {
            "summary_bytes": summary.stat().st_size,
            "error": error,
            "index_recovery": state,
            "recovery_queue_empty": manager._recovery_queue.empty(),
        }
    finally:
        manager.close()


def observation_window(root):
    from robosys.infra.observation_cache import _as_uuid
    from robosys.web.query import PortalData

    path = root / "observations.sqlite3"
    sid = str(_as_uuid("audit-observations"))
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE observation_records(session_id TEXT, epoch INTEGER, sequence INTEGER, feedback_json TEXT);"
            "CREATE INDEX observations_order ON observation_records(session_id,epoch,sequence);"
        )
        conn.executemany(
            "INSERT INTO observation_records VALUES (?,?,?,?)",
            (
                (sid, 1, i, json.dumps({"sequence": i, "payload": "x" * 256}))
                for i in range(5000)
            ),
        )
    data = PortalData(
        root / "metrics.jsonl", root / "trace.jsonl", observation_index=path
    )
    original = data._observation_query
    fetched = []

    def count(query, params):
        rows = original(query, params)
        fetched.append(len(rows))
        return rows

    data._observation_query = count
    tracemalloc.start()
    start = time.perf_counter()
    result = data.observation_history(sid, length=50, interval=100)
    elapsed = time.perf_counter() - start
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert fetched == [5000] and len(result) == 50
    return {
        "length": 50,
        "interval": 100,
        "fetched_and_decoded_rows": fetched[0],
        "returned_rows": len(result),
        "elapsed_s": elapsed,
        "python_peak_bytes": peak,
        "server_max_length_times_interval": 1000 * 1000,
    }


def metrics_rotation(root):
    from robosys.infra.metrics import MetricsDaemon
    from robosys.schemas import MetricSample

    path = root / "metrics.jsonl"
    sample = MetricSample(name="audit.gauge", kind="gauge", value=1)
    first = sample.model_dump_json() + "\n"
    second = sample.model_copy(update={"value": 2.0}).model_dump_json() + "\n"
    assert len(first.encode()) == len(second.encode())
    path.write_text(first)
    daemon = MetricsDaemon(path, read_only=True)
    old_inode = path.stat().st_ino
    replacement = root / "replacement.jsonl"
    replacement.write_text(second)
    os.replace(replacement, path)
    actual = daemon.snapshot()[-1].value
    assert actual == 1 and path.stat().st_ino != old_inode
    return {
        "file_replaced": True,
        "same_size": True,
        "new_file_value": 2,
        "reported_value": actual,
    }


def legacy_run_scan(root):
    from robosys.web.query import PortalData

    runs = root / "legacy-runs"
    for i in range(30):
        directory = runs / str(i)
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(
            json.dumps({"started_at": f"{i:03}", "payload": "x" * 256})
        )
    data = PortalData(root / "absent-metrics", root / "absent-trace", run_root=runs)
    read = []
    original = Path.read_text

    def counted(path, *args, **kwargs):
        if path.name == "manifest.json":
            read.append(path)
        return original(path, *args, **kwargs)

    with patch.object(Path, "read_text", counted):
        result = data.runs(limit=1)
    assert len(read) == 30 and len(result) == 1
    return {"requested_limit": 1, "files_read": len(read), "returned_rows": len(result)}


def graph_payload(root):
    from robosys.modules import ModuleRuntime
    from robosys.runtime.safety import SafetyStateStore
    from robosys.workflow import GraphRuntime
    from robosys.workflow.definition import GraphDefinition

    async def run(size):
        count, ticks = 0, []

        class Work:
            async def run(self, ctx):
                nonlocal count
                if ctx.current_node == "producer":
                    return {"unused_payload": list(range(size))}
                count += 1
                ticks.append(time.perf_counter())
                return {"iteration": count}

        graph = GraphDefinition.model_validate(
            {
                "id": "main",
                "entry": "producer",
                "max_steps": 50,
                "nodes": [
                    {"id": "producer", "module": "work", "next": "tick"},
                    {
                        "id": "tick",
                        "module": "work",
                        "predecessors": ["producer"],
                        "inputs": {},
                        "loop": {"when": "ctx.results['tick']['iteration'] < 40"},
                    },
                ],
            }
        )
        runtime = GraphRuntime(
            [graph],
            ModuleRuntime({"work": Work()}),
            safety_state=SafetyStateStore(),
            backend="fallback",
        )
        assert runtime.context_store is None
        start = time.perf_counter()
        result = await runtime.run("main")
        elapsed = time.perf_counter() - start
        assert result.success and count == 40, result.error
        intervals = sorted((b - a) * 1000 for a, b in pairwise(ticks))
        return {
            "elapsed_s": elapsed,
            "loop_interval_p95_ms": intervals[int(len(intervals) * 0.95)],
            "steps": result.steps,
        }

    samples = {"empty": [], "unused_10000_integers": []}
    with patch.dict(os.environ):
        os.environ.pop("ROBOSYS_CONTEXT_DB", None)
        for _ in range(3):
            for name, size in (("empty", 0), ("unused_10000_integers", 10000)):
                samples[name].append(asyncio.run(run(size)))
    return {
        "context_store": None,
        "unrelated_output_json_bytes": len(json.dumps(list(range(10000))).encode()),
        "samples": samples,
        "medians": {
            name: {
                key: statistics.median(row[key] for row in rows)
                for key in ("elapsed_s", "loop_interval_p95_ms")
            }
            for name, rows in samples.items()
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    before = source_hashes()
    results = {"platform": platform.platform(), "python": sys.version, "checks": {}}
    with tempfile.TemporaryDirectory(prefix="roboclaw-readonly-audit-") as tmp:
        for check in (
            config_typos,
            cli_mode,
            preflight,
            oversized_summary,
            observation_window,
            metrics_rotation,
            legacy_run_scan,
            graph_payload,
        ):
            folder = Path(tmp) / check.__name__
            folder.mkdir()
            results["checks"][check.__name__] = check(folder)
            print(check.__name__ + " reproduced", flush=True)
    after = source_hashes()
    results["source_unchanged"] = before == after
    results["source_hashes"] = after
    assert results["source_unchanged"]
    Path(args.output).write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
