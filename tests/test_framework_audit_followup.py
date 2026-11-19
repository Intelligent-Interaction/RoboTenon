"""Regressions for the follow-up audit of the 2026-09-21 repairs."""

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from robotenon.infra.params import ParameterError
from robotenon.infra.run_index import (
    DIRTY_FILENAME,
    RunManifestIndex,
    register_if_indexed,
)
from robotenon.runtime.config import load_application_config
from robotenon.runtime.experiment import ExperimentRequest
from robotenon.runtime.preflight import check_preflight
from robotenon.web.query import PortalData

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_raw_master_and_child_typos_are_rejected_before_projection():
    config = json.loads((PROJECT_ROOT / "configs/config.json").read_text())
    config["runtme"] = {"graph_id": "ignored"}
    with pytest.raises(ParameterError, match="runtme"):
        load_application_config(config, base_dir=PROJECT_ROOT / "configs")

    config = json.loads((PROJECT_ROOT / "configs/config.json").read_text())
    modules = json.loads((PROJECT_ROOT / "configs/modules_configs.json").read_text())
    modules["moduels"] = []
    config["modules_config"] = modules
    with pytest.raises(ParameterError, match="moduels"):
        load_application_config(config, base_dir=PROJECT_ROOT / "configs")


def test_run_index_removes_deleted_rows_and_reconciles_failed_registration(
    tmp_path, monkeypatch
):
    first = tmp_path / "first" / "manifest.json"
    first.parent.mkdir()
    first.write_text(json.dumps({"run_id": "first", "started_at": "2026-01-01"}))
    index = RunManifestIndex(tmp_path)
    assert [item["run_id"] for item in index.list()] == ["first"]
    first.unlink()
    assert index.list() == []

    second = tmp_path / "second" / "manifest.json"
    second.parent.mkdir()
    second.write_text(json.dumps({"run_id": "second", "started_at": "2026-01-02"}))
    original = RunManifestIndex.register

    def fail_register(self, path):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(RunManifestIndex, "register", fail_register)
    assert not register_if_indexed(second)
    assert (tmp_path / DIRTY_FILENAME).is_file()
    monkeypatch.setattr(RunManifestIndex, "register", original)
    assert [item["run_id"] for item in index.list()] == ["second"]
    assert not (tmp_path / DIRTY_FILENAME).exists()


def test_observation_history_caps_return_rows(tmp_path):
    database = tmp_path / "observations.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE observation_records("
        "session_id TEXT, epoch INTEGER, sequence INTEGER, feedback_json TEXT)"
    )
    session_id = str(uuid4())
    connection.executemany(
        "INSERT INTO observation_records VALUES (?, 0, ?, ?)",
        [(session_id, index, "{}") for index in range(1001)],
    )
    connection.commit()
    connection.close()
    data = PortalData(
        tmp_path / "metrics.jsonl",
        tmp_path / "trace.jsonl",
        run_root=tmp_path / "runs",
        observation_index=database,
    )
    with pytest.raises(ValueError, match="result limit"):
        data.observation_history(session_id, length=1001, interval=1)
    assert len(data.observation_history(session_id, length=1000, interval=1)) == 1000


def test_task_item_queries_use_catalog_without_recursive_rediscovery(
    tmp_path, monkeypatch
):
    queue_path = tmp_path / "run" / "queue.sqlite3"
    queue_path.parent.mkdir()
    connection = sqlite3.connect(queue_path)
    connection.execute("CREATE TABLE task_items(task_item_id TEXT, updated_at TEXT)")
    connection.executemany(
        "INSERT INTO task_items VALUES (?, ?)", [("one", "1"), ("two", "2")]
    )
    connection.commit()
    connection.close()
    data = PortalData(
        tmp_path / "metrics.jsonl",
        tmp_path / "trace.jsonl",
        run_root=tmp_path,
    )
    assert data.task_item_count() == 2

    def reject_glob(*args, **kwargs):
        raise AssertionError("request path must not recursively scan the run tree")

    monkeypatch.setattr(Path, "glob", reject_glob)
    assert data.task_item_count() == 2
    assert [item["task_item_id"] for item in data.task_items()] == ["two", "one"]
