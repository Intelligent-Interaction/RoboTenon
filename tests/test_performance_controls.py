"""Availability and bounded-resource regressions with no robot or GPU dependency."""

import asyncio
import fcntl
import http.client
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from robotenon.infra.params import Params
from robotenon.infra.trace_query import QueryBudgetExceeded, TraceQuery
from robotenon.runtime.retention import RetentionPolicy, prune_artifacts
from robotenon.task_adapters import evaluator
from robotenon.task_adapters.base import BaseTaskAdapter, ConfiguredTaskAdapter, TaskCase
from robotenon.task_adapters.result_index import ResultIndex
from robotenon.web.cache import BoundedCache
from robotenon.web.experiments import ExperimentManager
from robotenon.web.server import build_server


def manager_job(tmp_path, status="running"):
    manager = ExperimentManager(project_root=tmp_path, output_root=tmp_path / "runs")
    root = tmp_path / "runs" / "run"
    root.mkdir()
    job = {
        "job_id": "run",
        "status": status,
        "submitted_at": "2026-09-20T00:00:00+00:00",
        "output_root": str(root),
        "status_path": str(root / "status.json"),
        "summary_path": str(root / "summary.json"),
    }
    manager._jobs["run"] = job
    return manager, root


def test_http_stop_returns_accepted_and_reads_remain_available_until_release(
    tmp_path, monkeypatch
):
    manager, _ = manager_job(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def terminate(*args):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return {"terminated": True, "pid": None}

    monkeypatch.setattr(manager, "_terminate_process_group", terminate)
    server = build_server(
        "127.0.0.1", 0, SimpleNamespace(traces=None), experiment_manager=manager
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=1)
        try:
            conn.request(
                method,
                path,
                body="{}" if method == "POST" else None,
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    try:
        status, value = request("POST", "/api/experiments/run/stop")
        assert entered.wait(1)
        assert status == 202 and value["pending"] and not value["released"]
        assert (
            request("GET", "/api/experiments?limit=1")[1]["experiments"][0]["phase"]
            == "cleanup"
        )
        assert request("POST", "/api/experiments/run/stop")[0] == 202
        assert len(calls) == 1
        assert manager._job_is_running(manager._jobs["run"])
        release.set()
        value = manager.stop_job("run")
        assert value["released"] and not value["pending"]
        assert manager.get_job("run")["status"] == "cancelled"
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


def test_stop_failure_stays_busy_and_can_retry(tmp_path, monkeypatch):
    manager, _ = manager_job(tmp_path)
    monkeypatch.setattr(
        manager,
        "_terminate_process_group",
        lambda *args: {"terminated": False, "reason": "still_alive"},
    )
    with pytest.raises(RuntimeError, match="fully released"):
        manager.stop_job("run")
    job = manager.get_job("run")
    assert job["phase"] == "needs_attention" and manager._job_is_running(job)
    monkeypatch.setattr(
        manager, "_terminate_process_group", lambda *args: {"terminated": True}
    )
    assert manager.stop_job("run")["released"]


def test_restart_resumes_authorized_cleanup(tmp_path, monkeypatch):
    manager, _ = manager_job(tmp_path, "stopping")
    manager._persist_jobs()
    resumed = ExperimentManager(project_root=tmp_path, output_root=tmp_path / "runs")
    done = threading.Event()

    def terminate(*args):
        done.set()
        return {"terminated": True}

    monkeypatch.setattr(resumed, "_terminate_process_group", terminate)
    resumed.start()
    try:
        assert done.wait(1)
        assert resumed.stop_job("run")["released"]
    finally:
        resumed.close()


class Cases(BaseTaskAdapter):
    def __init__(self, count):
        self.count = count

    def enumerate_cases(self, *, adapter_id, params):
        for index in range(self.count):
            yield TaskCase(
                adapter_id=adapter_id,
                task_id="test",
                environment_id="fake",
                index=index,
                episode=index,
            )


def test_incremental_results_queryable_before_summary_and_resume_replaces_one_case(
    tmp_path, monkeypatch
):
    summaries, original = [], evaluator.write_json

    def write(path, value):
        if path.name == "summary.json":
            summaries.append(json.dumps(value))
        original(path, value)

    monkeypatch.setattr(evaluator, "write_json", write)

    async def run():
        release = asyncio.Event()
        calls = []

        class Backend:
            max_workers = 1

            async def execute(self, configured, case, context):
                calls.append(case.index)
                if case.index == 1:
                    await release.wait()
                return {"task_success": case.index != 2}

        options = {
            "config_path": tmp_path / "config.json",
            "params": Params({}),
            "adapters": [
                ConfiguredTaskAdapter("a", SimpleNamespace(name="fake"), Cases(3))
            ],
            "run_episode": None,
            "output_dir": tmp_path,
            "run_id": "batch",
            "execution_backend": Backend(),
        }
        task = asyncio.create_task(evaluator.evaluate_task_adapters(**options))
        async with asyncio.timeout(3):
            while len(calls) < 2:
                await asyncio.sleep(0.001)
        root = tmp_path / "batch"
        index = ResultIndex(root)
        assert [item["status"] for item in index.page()["episodes"]] == [
            "completed",
            "running",
            "pending",
        ]
        running = json.loads((root / "status.json").read_text())
        assert running["completed_episodes"] == 1
        assert running["summary"]["successes"] == 1
        assert "episodes" not in json.loads(summaries[0])["task_adapters"]["a"]
        release.set()
        final = await task
        assert (
            len(summaries) == 2
            and final["num_episodes"] == 3
            and final["successes"] == 2
        )
        evaluator.write_json(
            root / ".control/resume.json",
            {"action": "rerun", "tasks": [{"adapter_id": "a", "global_index": 2}]},
        )
        untouched = index.get("a", 0)
        await evaluator.evaluate_task_adapters(**options)
        assert index.get("a", 0) == untouched
        assert calls == [0, 1, 2, 2]
        assert len(index.page()["episodes"]) == 3

    asyncio.run(run())


def test_portal_index_reads_never_open_full_summary(tmp_path, monkeypatch):
    manager, root = manager_job(tmp_path, "completed")
    index = ResultIndex(root)
    index.initialize(
        [
            {"adapter_id": "a", "global_index": i, "status": "completed"}
            for i in range(30)
        ]
    )
    (root / "summary.json").write_text("invalid JSON must not be opened")
    page = manager.episodes_page("run", limit=10, offset=10)
    assert [x["global_index"] for x in page["episodes"]] == list(range(10, 20))
    assert page["next_offset"] == 20
    assert manager.get_job("run")["status"] == "completed"


def test_job_page_only_refreshes_requested_subset(tmp_path, monkeypatch):
    manager, _ = manager_job(tmp_path, "waiting")
    for i in range(100):
        manager._jobs[str(i)] = {**manager._jobs["run"], "job_id": str(i)}
    seen = []

    def refresh(key):
        seen.append(key)
        return manager._jobs[key]

    monkeypatch.setattr(manager, "_refresh_job", refresh)
    page = manager.jobs_page(limit=5, offset=10)
    assert len(seen) == 5 and page["total"] == 101 and page["next_offset"] == 15
    assert manager.jobs_page(search="run")["total"] == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 501},
        {"offset": -1},
        {"offset": 100001},
        {"since": "2026-09-20"},
        {"since": "2026-09-21T00:00:00Z", "until": "2026-09-20T00:00:00Z"},
    ],
)
def test_history_invalid_windows_rejected(tmp_path, kwargs):
    manager, _ = manager_job(tmp_path)
    with pytest.raises(ValueError):
        manager.jobs_page(**kwargs)


def trace_database(tmp_path):
    db = tmp_path / "trace.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE samples(id INTEGER, value TEXT)")
        conn.executemany(
            "INSERT INTO samples VALUES (?,?)", [(i, "x" * 100) for i in range(30)]
        )
    return db


def test_trace_pages_are_complete_without_unbounded_default(tmp_path):
    db = trace_database(tmp_path)
    reader = TraceQuery(db, max_rows=10)
    page = reader.sql_page("SELECT * FROM samples ORDER BY id", limit=6, offset=6)
    assert [row["id"] for row in page["rows"]] == list(range(6, 12))
    assert page["next_offset"] == 12
    with pytest.raises(QueryBudgetExceeded):
        reader.sql("SELECT * FROM samples")
    rows, offset = [], 0
    while True:
        page = reader.sql_page(
            "SELECT * FROM samples ORDER BY id", limit=6, offset=offset
        )
        rows.extend(page["rows"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert len(rows) == 30 and len({row["id"] for row in rows}) == 30


def test_trace_query_deadline_aborts_expensive_select(tmp_path):
    reader = TraceQuery(trace_database(tmp_path), timeout_s=0.01)
    with pytest.raises(TimeoutError):
        reader.sql(
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) SELECT SUM(x) FROM n"
        )
    assert len(reader.sql("SELECT * FROM samples LIMIT 1")) == 1


def test_trace_byte_budget_signals_more_pages_and_rejects_oversized_row(tmp_path):
    reader = TraceQuery(trace_database(tmp_path), max_bytes=500)
    page = reader.sql_page("SELECT * FROM samples")
    assert page["truncated"] and 1 <= len(page["rows"]) < 10
    with pytest.raises(QueryBudgetExceeded):
        reader.sql("SELECT printf('%01000d', 1) AS value")


def test_cache_lru_bytes_and_invalidation(tmp_path):
    cache = BoundedCache(max_entries=2, max_bytes=10)
    cache.put("a", 1, size=4)
    cache.put("b", 2, size=4)
    assert cache.get("a") == 1
    cache.put("c", 3, size=4)
    assert cache.get("b") is None and cache.bytes == 8
    cache.put("large", 1, size=11)
    assert len(cache) == 2
    cache.pop("a")
    assert cache.bytes == 4
    manager, root = manager_job(tmp_path)
    path = root / "sample.json"
    path.write_text('{"a":1}')
    assert manager._read_json_cached(path) == {"a": 1}
    path.write_text('{"a":123}')
    assert manager._read_json_cached(path) == {"a": 123}


@pytest.mark.parametrize(
    "held,status,age,expected",
    [
        (False, "completed", 40, True),
        (True, "completed", 40, False),
        (False, "failed", 40, False),
        (False, "failed", 100, True),
        (False, "running", 100, False),
    ],
)
def test_retention_respects_leases_failed_run_retention_and_dry_run(
    tmp_path, held, status, age, expected
):
    manager, root = manager_job(tmp_path, status)
    control = root / ".control"
    control.mkdir()
    lock = (control / "run.lock").open("a+")
    if held:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    video = root / "episode.mp4"
    video.write_bytes(b"video")
    outside = tmp_path / "outside.log"
    outside.write_text("preserve")
    (root / "link.log").symlink_to(outside)
    summary = root / "summary.json"
    summary.write_text("{}")
    old = time.time() - age * 86400
    os.utime(video, (old, old))
    os.utime(outside, (old, old))
    manager._persist_jobs()
    try:
        preview = prune_artifacts(manager.output_root, RetentionPolicy())
        assert bool(preview["files"]) is expected and video.exists()
        result = prune_artifacts(manager.output_root, RetentionPolicy(), apply=True)
        assert bool(result["files"]) is expected
        assert video.exists() is not expected
        assert outside.read_text() == "preserve" and summary.exists()
    finally:
        lock.close()


def test_preflight_reports_missing_local_model_without_starting_services(tmp_path):
    from robotenon.runtime.config import ToolDeploymentConfig, load_application_config
    from robotenon.runtime.preflight import check_preflight

    loaded = load_application_config(
        Path(__file__).resolve().parents[1] / "configs/config.json"
    )
    loaded.tools.tools[0].deployment = ToolDeploymentConfig(
        backend="command",
        gpu_ids=[0],
        command=["definitely-missing-runtime"],
        model_path=str(tmp_path / "missing-model"),
    )
    # Config validation may reject allocation first; either outcome is an actionable diagnostic.
    result = check_preflight(loaded, output_root=tmp_path, probe_gpu=False)
    assert not result["ok"] and any(
        item["status"] == "error" for item in result["checks"]
    )


def test_pipe_notifications_handle_ready_timeout_eof_and_cancelled_wait():
    import multiprocessing

    from robotenon.runtime.parallel import _wait_readable

    async def scenario():
        parent, child = multiprocessing.Pipe()
        try:
            assert not await _wait_readable(parent, 0.01)
            pending = asyncio.create_task(_wait_readable(parent, 5))
            await asyncio.sleep(0)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            child.send(("ready", None))
            assert await _wait_readable(parent, 0.1)
            assert parent.recv() == ("ready", None)
            child.close()
            assert await _wait_readable(parent, 0.1)
            with pytest.raises(EOFError):
                parent.recv()
        finally:
            parent.close()
            child.close()

    asyncio.run(scenario())


def test_preflight_shared_cli_and_portal_check_model_path(tmp_path):
    from test_execution_backends import make_config

    from robotenon.runtime.config import load_application_config
    from robotenon.runtime.preflight import check_preflight
    from robotenon.runtime.runner_cli import main

    config = make_config(tmp_path)
    value = json.loads(config.read_text())
    value["parameters"]["model"] = {"path": str(tmp_path / "missing-model")}
    config.write_text(json.dumps(value))
    report = check_preflight(
        load_application_config(config), output_root=tmp_path, probe_gpu=False
    )
    assert any(
        item["name"] == "model.path" and item["status"] == "error"
        for item in report["checks"]
    )
    assert (
        main(["--preflight", "--config", str(config), "--output-dir", str(tmp_path)])
        == 1
    )
    manager = ExperimentManager(
        project_root=tmp_path, config_root=tmp_path, output_root=tmp_path / "runs"
    )
    assert not manager.preflight(config)["ok"]
    assert not manager._jobs


def test_runtime_phase_waits_for_worker_ready_snapshot(tmp_path):
    manager, root = manager_job(tmp_path)
    live = root / "live.json"
    status = {
        "status": "running",
        "active": [
            {"adapter_id": "a", "global_index": 0, "live_status_path": str(live)}
        ],
    }
    (root / "status.json").write_text(json.dumps(status))
    assert manager.get_job("run")["phase"] == "loading"
    live.write_text(json.dumps({"status": "running"}))
    assert manager.get_job("run")["phase"] == "executing"


def test_bulk_controls_validate_identities_without_listing_full_history(
    tmp_path, monkeypatch
):
    manager, root = manager_job(tmp_path, "completed")
    index = ResultIndex(root)
    index.initialize(
        {"adapter_id": "a", "global_index": i, "status": "completed"}
        for i in range(1000)
    )
    monkeypatch.setattr(
        manager,
        "list_episodes",
        lambda *a, **k: pytest.fail("must use indexed identity lookup"),
    )
    assert index.contains_all([{"adapter_id": "a", "global_index": 999}])
    assert not index.contains_all([{"adapter_id": "a", "global_index": 1000}])
    result = manager.stop_job("run", tasks=[{"adapter_id": "a", "global_index": 999}])
    assert result["already_finished"]
