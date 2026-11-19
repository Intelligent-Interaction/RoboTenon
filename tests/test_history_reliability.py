"""Fault, scale, and admission checks for the previous audit's five repairs."""

import asyncio
import fcntl
import gc
import http.client
import json
import os
import sqlite3
import threading
import time
import weakref
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_performance_controls import Cases, manager_job

from robotenon.infra.params import Params
from robotenon.runtime.retention import RetentionPolicy, prune_artifacts
from robotenon.storage.jobs import JobStore
from robotenon.task_adapters.base import ConfiguredTaskAdapter
from robotenon.task_adapters.evaluator import evaluate_task_adapters, write_json
from robotenon.task_adapters.result_index import IndexRecoveryPending, ResultIndex
from robotenon.task_adapters.result_writer import ResultWriter
from robotenon.web.experiments import ExperimentManager
from robotenon.web.server import build_server


def terminal_run(root, name):
    directory = root / name
    (directory / ".control").mkdir(parents=True)
    (directory / ".control/run.lock").touch()
    return {
        "job_id": name,
        "status": "completed",
        "output_root": str(directory),
        "status_path": str(directory / "status.json"),
        "summary_path": str(directory / "summary.json"),
        "submitted_at": "2026-09-20T00:00:00+00:00",
    }


def old_file(path):
    path.write_text("fixture")
    stale = time.time() - 120 * 86400
    os.utime(path, (stale, stale))


def test_retention_resumes_large_metadata_directories_and_rotates_runs(tmp_path):
    store = JobStore(tmp_path)
    a, b = terminal_run(tmp_path, "a"), terminal_run(tmp_path, "b")
    store["a"], store["b"] = a, b
    store.flush()
    for number in range(45):
        (tmp_path / "a" / f"case-{number}.json").write_text("{}")
    old_file(tmp_path / "a" / "old.log")
    old_file(tmp_path / "b" / "old.log")
    reports = []
    for _ in range(30):
        reports.append(
            prune_artifacts(tmp_path, RetentionPolicy(), apply=True, max_entries=8)
        )
        if (
            not (tmp_path / "a/old.log").exists()
            and not (tmp_path / "b/old.log").exists()
        ):
            break
    assert not (tmp_path / "a/old.log").exists()
    assert not (tmp_path / "b/old.log").exists()
    assert any("b/old.log" in report["files"] for report in reports[:4])
    assert len(list((tmp_path / "a").glob("*.json"))) == 45
    assert len(reports) < 30


def test_retention_preview_preserves_cursor_and_rechecks_run_lock(tmp_path):
    store = JobStore(tmp_path)
    store["a"] = terminal_run(tmp_path, "a")
    store.flush()
    for number in range(15):
        (tmp_path / "a" / f"{number}.json").write_text("{}")
    video = tmp_path / "a/old.mp4"
    old_file(video)
    prune_artifacts(tmp_path, RetentionPolicy(), apply=True, max_entries=4)
    before = (tmp_path / ".retention.sqlite3").read_bytes()
    preview = prune_artifacts(tmp_path, RetentionPolicy(min_free_gb=10**8))
    assert preview["disk"]["low_space"] and video.exists()
    assert (tmp_path / ".retention.sqlite3").read_bytes() == before
    with (tmp_path / "a/.control/run.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = prune_artifacts(tmp_path, RetentionPolicy(), apply=True)
        assert result["skipped_runs"] == ["a"] and video.exists()
    prune_artifacts(tmp_path, RetentionPolicy(), apply=True)
    assert not video.exists()


def test_retention_cursor_advances_when_prefix_replay_exceeds_budget(
    tmp_path, monkeypatch
):
    from robotenon.runtime import retention

    store = JobStore(tmp_path)
    store["a"] = terminal_run(tmp_path, "a")
    store.flush()
    for number in range(15):
        (tmp_path / "a" / f"{number}.json").write_text("{}")
    prune_artifacts(tmp_path, RetentionPolicy(), apply=True, max_entries=4)
    with sqlite3.connect(tmp_path / ".retention.sqlite3") as conn:
        before = conn.execute(
            "SELECT position FROM directories WHERE path='.'"
        ).fetchone()[0]
    assert before > 0
    clock = [0.0]
    original_scandir = os.scandir

    @contextmanager
    def slow_scandir(directory):
        with original_scandir(directory) as entries:

            def delayed():
                for number, entry in enumerate(entries):
                    if number == 0:
                        clock[0] += 1.0
                    yield entry

            yield delayed()

    monkeypatch.setattr(
        retention, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0])
    )
    monkeypatch.setattr(retention.os, "scandir", slow_scandir)
    report = prune_artifacts(tmp_path, RetentionPolicy(), apply=True, budget_s=0.2)
    with sqlite3.connect(tmp_path / ".retention.sqlite3") as conn:
        after = conn.execute(
            "SELECT position FROM directories WHERE path='.'"
        ).fetchone()[0]
    assert report["truncated"] and after > before


def test_job_registry_migration_is_once_and_writes_only_changed_rows(tmp_path):
    legacy = tmp_path / "jobs.json"
    legacy.write_text(
        json.dumps([{"job_id": "old", "status": "completed", "progress": {"a": 1}}])
    )
    original = legacy.read_bytes()
    store = JobStore(tmp_path, cache_size=8)
    for number in range(1000):
        key = f"job-{number:04}"
        store[key] = {
            "job_id": key,
            "status": "completed",
            "submitted_at": "2026-09-20T00:00:00+00:00",
        }
    store.flush()
    assert len(store) == 1001 and len(store._cache) <= 8
    with sqlite3.connect(store.path) as conn:
        conn.executescript(
            "CREATE TABLE edits(id TEXT); CREATE TRIGGER changed AFTER UPDATE ON jobs BEGIN INSERT INTO edits VALUES (new.job_id); END;"
        )
    store["old"]["progress"]["a"] = 2
    store.flush()
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT id FROM edits").fetchall() == [("old",)]
    assert legacy.read_bytes() == original
    assert JobStore(tmp_path)["old"]["progress"]["a"] == 2


def test_job_cursor_does_not_repeat_records_after_new_submission(tmp_path):
    store = JobStore(tmp_path)
    for number in range(8):
        key = f"j{number}"
        store[key] = {
            "job_id": key,
            "status": "completed",
            "submitted_at": "2026-09-20T00:00:00+00:00",
        }
    first = store.page(limit=3)
    store["new"] = {
        "job_id": "new",
        "status": "waiting",
        "submitted_at": "2026-09-21T00:00:00+00:00",
    }
    second = store.page(limit=3, cursor=first["next_cursor"])
    assert first["job_ids"] == ["j7", "j6", "j5"]
    assert second["job_ids"] == ["j4", "j3", "j2"]
    with pytest.raises(ValueError, match="cursor"):
        store.page(cursor=first["next_cursor"], search="different")


def test_scheduler_does_not_refresh_terminal_history(tmp_path, monkeypatch):
    manager, _ = manager_job(tmp_path, "completed")
    for number in range(200):
        key = str(number)
        manager._jobs[key] = {
            **manager._jobs["run"],
            "job_id": key,
            "resources_released": True,
        }
    manager._jobs["active"] = {
        **manager._jobs["run"],
        "job_id": "active",
        "status": "running",
    }
    seen = []
    monkeypatch.setattr(
        manager, "_refresh_job", lambda key: seen.append(key) or manager._jobs[key]
    )
    manager._dispatch_waiting()
    assert seen == ["active"]
    assert len(manager._jobs._cache) <= 128


def episode(root, index, **extra):
    path = root / "task" / f"episode_{index:03}.json"
    record = {
        "adapter_id": "a",
        "global_index": index,
        "status": "completed",
        "task_success": True,
        "episode_json": str(path),
        **extra,
    }
    write_json(path, record)
    return record


@pytest.mark.parametrize("damaged", [False, True])
def test_recovery_uses_case_artifacts_without_final_summary(tmp_path, damaged):
    root = tmp_path / "run"
    terminal_run(tmp_path, "run")
    records = [episode(root, 0), episode(root, 1, status="error", task_success=False)]
    index = ResultIndex(root)
    index.initialize([records[0]], clean=False)
    write_json(root / "summary.json", {"num_episodes": 0, "task_adapters": {}})
    write_json(root / "status.json", {"status": "running", "config_hash": "preserved"})
    write_json(root / "task/attempts/stale/episode_099.json", records[0])
    if damaged:
        index.path.write_bytes(b"invalid sqlite database")
    result = index.recover()
    assert result["indexed"] == 2 and not index.needs_recovery()
    assert [item["global_index"] for item in index.page()["episodes"]] == [0, 1]
    status = json.loads((root / "status.json").read_text())
    assert status["status"] == "failed" and status["config_hash"] == "preserved"
    assert status["summary"]["num_errors"] == 1 and status["summary"]["successes"] == 1


def test_recovery_invalid_artifact_preserves_existing_index(tmp_path):
    root = tmp_path / "run"
    terminal_run(tmp_path, "run")
    record = episode(root, 0)
    index = ResultIndex(root)
    index.initialize([record], clean=False)
    (root / "task/episode_001.json").write_text("{broken")
    with pytest.raises(ValueError):
        index.recover()
    assert index.get("a", 0)["status"] == "completed"
    assert index.needs_recovery()
    assert not list(root.glob(".episodes-recovery-*"))


def test_recovery_refuses_live_or_queued_run(tmp_path):
    root = tmp_path / "run"
    terminal_run(tmp_path, "run")
    episode(root, 0)
    index = ResultIndex(root)
    with (root / ".control/run.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="active"):
            index.recover()
    (root / ".control/resume.json").write_text("{}")
    with pytest.raises(RuntimeError, match="resume"):
        index.recover()
    assert not index.path.exists()


def test_portal_recovers_dirty_index_after_restart(tmp_path):
    manager, root = manager_job(tmp_path, "failed")
    (root / ".control").mkdir()
    (root / ".control/run.lock").touch()
    records = [episode(root, 0), episode(root, 1)]
    ResultIndex(root).initialize(records[:1], clean=False)
    manager._jobs["run"]["index_recovery"] = {"status": "pending"}
    manager._persist_jobs()
    restored = ExperimentManager(project_root=tmp_path, output_root=tmp_path / "runs")
    try:
        restored.get_job("run")
        deadline = time.monotonic() + 3
        while True:
            try:
                result = restored.episodes_page("run")
                break
            except IndexRecoveryPending:
                assert time.monotonic() < deadline
                time.sleep(0.01)
        assert len(result["episodes"]) == 2
        assert restored.get_job("run")["summary"]["successes"] == 2
    finally:
        restored.close()


def test_writer_cancellation_waits_for_accepted_writes_and_keeps_dirty_marker(tmp_path):
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        def slow_write(path, value):
            entered.set()
            assert release.wait(3)
            write_json(path, value)

        writer = ResultWriter(tmp_path, slow_write, capacity=1)
        await writer.start()
        task = asyncio.create_task(writer.write(tmp_path / "one.json", {"value": 1}))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        closing = asyncio.create_task(writer.close())
        await asyncio.sleep(0.02)
        assert not closing.done()
        release.set()
        await closing
        assert json.loads((tmp_path / "one.json").read_text()) == {"value": 1}
        assert writer.index.needs_recovery()

    asyncio.run(scenario())


def test_batch_releases_full_results_but_keeps_artifacts_and_memory(tmp_path):
    class LargeContext(dict):
        pass

    refs, peaks = [], []

    class Backend:
        max_workers = 1

        async def execute(self, configured, case, context):
            gc.collect()
            peaks.append(sum(ref() is not None for ref in refs))
            payload = LargeContext(observations=["x" * 100000])
            refs.append(weakref.ref(payload))
            return {
                "task_success": True,
                "context": payload,
                "ltm_summary": {"case": case.index},
            }

    result = asyncio.run(
        evaluate_task_adapters(
            config_path=tmp_path / "config.json",
            params=Params({}),
            adapters=[
                ConfiguredTaskAdapter("a", SimpleNamespace(name="fake"), Cases(20))
            ],
            output_dir=tmp_path,
            run_id="batch",
            run_episode=None,
            execution_backend=Backend(),
            options={"memory_mode": "snapshot"},
        )
    )
    assert max(peaks) <= 1 and result["num_episodes"] == 20
    assert (
        len(
            json.loads((tmp_path / "batch/long_term_memory.json").read_text())[
                "records"
            ]
        )
        == 20
    )
    assert (
        len(
            [
                path
                for path in (tmp_path / "batch/test").glob("episode_*.json")
                if not path.name.endswith(".memory.json")
            ]
        )
        == 20
    )
    assert not ResultIndex(tmp_path / "batch").needs_recovery()


def request(server, method, path):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        conn.request(
            method,
            path,
            body="{}" if method == "POST" else None,
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), json.loads(response.read())
    finally:
        conn.close()


def test_query_overload_preserves_health_and_stop_capacity(tmp_path, monkeypatch):
    manager, _ = manager_job(tmp_path, "waiting")
    entered, release = threading.Event(), threading.Event()

    def blocked_page(**kwargs):
        entered.set()
        assert release.wait(3)
        return {"experiments": []}

    monkeypatch.setattr(manager, "jobs_page", blocked_page)
    server = build_server(
        "127.0.0.1",
        0,
        SimpleNamespace(traces=None),
        experiment_manager=manager,
        max_queries=1,
        control_slots=1,
        max_connections=4,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    active = threading.Thread(target=lambda: request(server, "GET", "/api/experiments"))
    active.start()
    try:
        assert entered.wait(1)
        status, headers, _ = request(server, "GET", "/api/experiments")
        assert status == 503 and headers["Retry-After"] == "1"
        assert request(server, "GET", "/api/health")[0] == 200
        assert request(server, "POST", "/api/experiments/run/stop")[0] == 200
        assert manager.get_job("run")["status"] == "cancelled"
    finally:
        release.set()
        active.join()
        server.shutdown()
        server.server_close()
        thread.join()


def test_http_connection_count_is_bounded_and_permits_are_released():
    import socket

    server = build_server(
        "127.0.0.1",
        0,
        SimpleNamespace(traces=None),
        max_queries=1,
        control_slots=1,
        max_connections=2,
    )
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    sockets = []
    try:
        for _ in range(2):
            connection = socket.create_connection(
                ("127.0.0.1", server.server_port), timeout=1
            )
            connection.sendall(b"GET /api/health HTTP/1.1\r\n")
            sockets.append(connection)
        deadline = time.monotonic() + 1
        while server.admission_snapshot()["active"]["connections"] < 2:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        status, headers, _ = request(server, "GET", "/api/health")
        assert status == 503 and headers["Retry-After"] == "1"
        for connection in sockets:
            connection.close()
        sockets.clear()
        deadline = time.monotonic() + 1
        while server.admission_snapshot()["active"]["connections"]:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert request(server, "GET", "/api/health")[0] == 200
    finally:
        for connection in sockets:
            connection.close()
        server.shutdown()
        server.server_close()
        serving.join()


def test_recovery_status_write_failure_remains_retryable(tmp_path, monkeypatch):
    from robotenon.task_adapters import evaluator

    root = tmp_path / "run"
    terminal_run(tmp_path, "run")
    episode(root, 0)
    index = ResultIndex(root)
    original = evaluator.write_json

    def fail_status(path, value):
        if path.name == "status.json":
            raise OSError("simulated disk error")
        original(path, value)

    monkeypatch.setattr(evaluator, "write_json", fail_status)
    with pytest.raises(OSError, match="disk error"):
        index.recover()
    assert index.needs_recovery()
    monkeypatch.setattr(evaluator, "write_json", original)
    assert index.recover()["indexed"] == 1
    assert not index.needs_recovery()


def test_stop_during_initial_case_write_still_records_cancelled_case(
    tmp_path, monkeypatch
):
    from robotenon.task_adapters import evaluator

    entered, release = threading.Event(), threading.Event()
    calls = []
    original = evaluator.write_json

    def slow_initial(path, value):
        if path.name.startswith("episode_") and value.get("status") == "running":
            entered.set()
            assert release.wait(3)
        original(path, value)

    monkeypatch.setattr(evaluator, "write_json", slow_initial)

    class Backend:
        max_workers = 1

        async def execute(self, *args):
            calls.append("executed")
            return {"task_success": True}

    async def scenario():
        task = asyncio.create_task(
            evaluator.evaluate_task_adapters(
                config_path=tmp_path / "config.json",
                params=Params({}),
                adapters=[
                    ConfiguredTaskAdapter("a", SimpleNamespace(name="fake"), Cases(1))
                ],
                output_dir=tmp_path,
                run_id="run",
                run_episode=None,
                execution_backend=Backend(),
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            original(
                tmp_path / "run/.control/command-stop.json",
                {"action": "stop", "tasks": None},
            )
            await asyncio.sleep(0.15)
        finally:
            release.set()
        result = await asyncio.wait_for(task, 3)
        assert result["num_cancelled"] == 1 and result["num_episodes"] == 1
        assert calls == []
        assert ResultIndex(tmp_path / "run").get("a", 0)["status"] == "cancelled"

    asyncio.run(scenario())


def test_queue_longer_than_cache_keeps_started_job_durable(tmp_path, monkeypatch):
    manager, root = manager_job(tmp_path, "completed")
    for number in range(160):
        key = f"queued-{number:03}"
        manager._jobs[key] = {
            **manager._jobs["run"],
            "job_id": key,
            "status": "waiting",
            "submitted_at": f"{number:06}",
            "command": ["fake-runner"],
            "log_path": str(root / f"{key}.log"),
        }
    process = SimpleNamespace(pid=987654, poll=lambda: None)
    monkeypatch.setattr(
        "robotenon.web.experiments.subprocess.Popen", lambda *args, **kwargs: process
    )
    monkeypatch.setattr(
        "robotenon.runtime.processes.register_process",
        lambda *args, **kwargs: {"pid": process.pid},
    )
    manager._dispatch_waiting()
    restored = JobStore(manager.output_root)
    assert restored["queued-000"]["status"] == "running"
    assert restored["queued-000"]["pid"] == process.pid
    assert restored["queued-001"]["status"] == "waiting"


def test_portal_retries_recovery_after_bad_artifact_is_repaired(tmp_path):
    manager, root = manager_job(tmp_path, "failed")
    (root / ".control").mkdir()
    (root / ".control/run.lock").touch()
    record = episode(root, 0)
    bad = root / "task/episode_001.json"
    bad.write_text("{invalid")
    ResultIndex(root).initialize([record], clean=False)
    try:
        manager.get_job("run")
        manager._recovery_queue.join()
        assert manager._jobs["run"]["index_recovery"]["status"] == "failed"
        episode(root, 1)
        manager._jobs["run"]["index_recovery"]["retry_after"] = 0
        manager.get_job("run")
        manager._recovery_queue.join()
        assert len(manager.episodes_page("run")["episodes"]) == 2
    finally:
        manager.close()


def test_registry_byte_budget_includes_unicode_and_unflushed_records(tmp_path):
    store = JobStore(tmp_path, cache_size=128, cache_bytes=4000)
    for number in range(6):
        key = str(number)
        store[key] = {"job_id": key, "status": "completed", "label": "汉" * 1000}
        assert len(store._cache) == 1
    store.flush()
    assert len(store) == 6
    assert store["0"]["label"] == "汉" * 1000
    assert len(store._cache) == 1
