import json
import sqlite3
import subprocess
from pathlib import Path

from robotenon.web.experiments import ExperimentManager


def fixture_config_root(project_root: Path) -> Path:
    return project_root / "tests" / "fixtures" / "experiment_configs"


def test_experiment_manager_restores_jobs_and_case_episodes(tmp_path: Path):
    output_root = tmp_path / "experiments"
    output_root.mkdir()
    summary_path = output_root / "exp-1" / "summary.json"
    summary_path.parent.mkdir()
    summary_path.write_text(
        json.dumps(
            {
                "task_adapters": {
                    "sim": {
                        "provider": "test",
                        "order": 0,
                        "num_cases": 1,
                        "episodes": [
                            {
                                "task_id": "task-a",
                                "global_index": 0,
                                "status": "completed",
                                "task_success": True,
                                "result": {
                                    "graph_status": "completed",
                                    "graph_success": True,
                                    "context": {
                                        "current_graph": "g",
                                        "current_node": "action",
                                        "history": [
                                            {
                                                "graph": "g",
                                                "node": "reset",
                                                "event": "enter",
                                            },
                                            {
                                                "graph": "g",
                                                "node": "reset",
                                                "event": "exit",
                                            },
                                            {
                                                "graph": "g",
                                                "node": "action",
                                                "event": "enter",
                                            },
                                        ],
                                    },
                                },
                            }
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (output_root / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "job_id": "exp-1",
                    "config": "config.json",
                    "status": "completed",
                    "summary_path": str(summary_path),
                    "status_path": str(output_root / "exp-1" / "status.json"),
                }
            ]
        ),
        encoding="utf-8",
    )

    manager = ExperimentManager(
        project_root=tmp_path,
        config_root=tmp_path / "configs",
        output_root=output_root,
    )

    assert [item["job_id"] for item in manager.list_jobs()] == ["exp-1"]
    episode = manager.list_episodes("exp-1")[0]
    assert episode["task_id"] == "task-a"
    assert episode["graph_runtime"]["graph_id"] == "g"
    assert episode["graph_runtime"]["status"] == "completed"
    assert episode["graph_runtime"]["active_node"] == "action"
    nodes = episode["graph_runtime"]["nodes"]
    assert [(node["id"], node["order"], node["status"]) for node in nodes] == [
        ("reset", 1, "completed"),
        ("action", 2, "running"),
    ]
    assert nodes[0]["events"] == [
        {"graph": "g", "node": "reset", "event": "enter"},
        {"graph": "g", "node": "reset", "event": "exit"},
    ]
    assert [
        (item["order"], item["id"], item["status"])
        for item in episode["graph_runtime"]["sequence"]
    ] == [
        (1, "reset", "completed"),
        (2, "action", "running"),
    ]
    assert "context" not in episode["result"]


def test_experiment_manager_exposes_running_case_and_live_runtime(tmp_path: Path):
    output_root = tmp_path / "experiments"
    job_root = output_root / "exp-live"
    job_root.mkdir(parents=True)
    live_path = job_root / "task.live.json"
    live_path.write_text(
        json.dumps(
            {
                "status": "running",
                "identity": {
                    "experiment_id": "config",
                    "run_id": "run-live",
                    "episode_id": "sim:0:0",
                    "task_id": "task-live",
                    "adapter_id": "sim",
                },
                "session_id": "session-live",
                "context": {
                    "current_graph": "g",
                    "current_node": "body_execute",
                    "status": "running",
                    "history": [
                        {"graph": "g", "node": "reset", "event": "exit", "step": 1},
                        {
                            "graph": "g",
                            "node": "body_execute",
                            "event": "enter",
                            "step": 2,
                        },
                    ],
                    "feedback": {"session_id": "session-live"},
                    "values": {},
                },
                "timeline_spans": [
                    {
                        "span_id": "live-span",
                        "trace_id": "run-live",
                        "name": "node:g:body_execute",
                        "start_time": "2026-09-18T00:00:00+00:00",
                        "end_time": "2026-09-18T00:00:02+00:00",
                        "status": "running",
                        "attributes": {
                            "kind": "node",
                            "graph": "g",
                            "node": "body_execute",
                            "step_index": 2,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    status_path = job_root / "status.json"
    status_path.write_text(
        json.dumps(
            {
                "status": "running",
                "current": {
                    "adapter_id": "sim",
                    "task_id": "task-live",
                    "index": 0,
                    "episode": 0,
                    "global_index": 0,
                    "status": "running",
                    "live_status_path": str(live_path),
                    "identity": {"run_id": "run-live", "episode_id": "sim:0:0"},
                },
                "active": [],
            }
        ),
        encoding="utf-8",
    )
    (output_root / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "job_id": "exp-live",
                    "status": "running",
                    "output_root": str(job_root),
                    "summary_path": str(job_root / "summary.json"),
                    "status_path": str(status_path),
                }
            ]
        ),
        encoding="utf-8",
    )

    manager = ExperimentManager(
        project_root=tmp_path,
        config_root=tmp_path / "configs",
        output_root=output_root,
    )

    episodes = manager.list_episodes("exp-live")
    assert len(episodes) == 1
    assert episodes[0]["status"] == "running"
    assert episodes[0]["graph_runtime"]["active_node"] == "body_execute"

    detail = manager.episode_detail("exp-live", adapter_id="sim", global_index=0)
    assert detail is not None
    assert detail["identifiers"]["run_id"] == "run-live"
    assert detail["media"]["live"]["session_id"] == "session-live"
    assert detail["runtime"]["active_node"] == "body_execute"
    assert detail["timeline"]["spans"][0]["status"] == "running"


def test_experiment_manager_reads_nested_progress_wrapper(tmp_path: Path):
    output_root = tmp_path / "experiments"
    job_root = output_root / "exp-nested"
    job_root.mkdir(parents=True)
    status_path = job_root / "status.json"
    status_path.write_text(
        json.dumps(
            {
                "progress": {
                    "status": "running",
                    "current": {
                        "adapter_id": "sim",
                        "task_id": "task-nested",
                        "global_index": 0,
                        "episode": 0,
                        "status": "running",
                    },
                    "active": [],
                }
            }
        ),
        encoding="utf-8",
    )
    (output_root / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "job_id": "exp-nested",
                    "status": "running",
                    "output_root": "experiments/exp-nested",
                    "status_path": "experiments/exp-nested/status.json",
                    "summary_path": "experiments/exp-nested/summary.json",
                }
            ]
        ),
        encoding="utf-8",
    )
    manager = ExperimentManager(
        project_root=tmp_path,
        config_root=tmp_path / "configs",
        output_root=output_root,
    )

    episodes = manager.list_episodes("exp-nested")
    assert len(episodes) == 1
    assert episodes[0]["task_id"] == "task-nested"
    assert episodes[0]["status"] == "running"


def test_experiment_manager_returns_configured_graph_topology(tmp_path: Path):
    project_root = Path(__file__).parents[1]
    output_root = tmp_path / "experiments"
    output_root.mkdir()
    status_path = output_root / "exp-graph" / "status.json"
    status_path.parent.mkdir()
    status_path.write_text(json.dumps({"status": "running"}), encoding="utf-8")
    (output_root / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "job_id": "exp-graph",
                    "config": "experiment_fixture.json",
                    "config_path": str(fixture_config_root(project_root) / "experiment_fixture.json"),
                    "status_path": str(status_path),
                    "summary_path": str(output_root / "exp-graph" / "summary.json"),
                }
            ]
        ),
        encoding="utf-8",
    )
    manager = ExperimentManager(
        project_root=project_root,
        config_root=fixture_config_root(project_root),
        output_root=output_root,
    )

    snapshot = manager.graph_snapshot("exp-graph")

    assert snapshot["active_graph"] == "fixture_reference"
    assert snapshot["primary_graph_id"] == "fixture_reference"
    assert len(snapshot["primary_graph"]["nodes"]) == 7
    assert (
        snapshot["primary_node_states"]["fixture_reference:plan"] == "pending"
    )
    assert {graph["id"] for graph in snapshot["graphs"]} == {
        "fixture_loop",
        "fixture_reference",
    }
    assert snapshot["node_states"]["fixture_reference:plan"] == "pending"


def test_experiment_manager_normalizes_alternate_trajectory_shapes(tmp_path: Path):
    output_root = tmp_path / "experiments"
    job_root = output_root / "exp-shape"
    job_root.mkdir(parents=True)
    episode_path = job_root / "hardware" / "case.json"
    episode_path.parent.mkdir()
    episode_path.write_text(
        json.dumps(
            {
                "adapter": "hardware",
                "case_index": 0,
                "task": "Open the fridge",
                "output": {
                    "runtime": {
                        "graph_id": "hardware_graph",
                        "current_node": "subtask",
                        "history": [
                            {"node_id": "observe", "status": "completed"},
                            {"node_id": "subtask", "status": "running"},
                        ],
                    },
                    "state": {
                        "values": {
                            "subtask": "Open the fridge door",
                            "feedback": {
                                "session_id": "real-robot-session",
                                "observation": {
                                    "frames": [
                                        {
                                            "camera": "front",
                                            "url": "/camera/front.jpg",
                                            "modality": "rgb",
                                        }
                                    ]
                                },
                            },
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    summary_path = job_root / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "adapters": [
                    {
                        "id": "hardware",
                        "cases": [
                            {
                                "adapter": "hardware",
                                "case_index": 0,
                                "task": "Open the fridge",
                                "episode_json": str(episode_path),
                                "status": "completed",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (job_root / "status.json").write_text(
        json.dumps({"status": "completed"}), encoding="utf-8"
    )
    (output_root / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "job_id": "exp-shape",
                    "status": "completed",
                    "output_root": str(job_root),
                    "summary_path": str(summary_path),
                    "status_path": str(job_root / "status.json"),
                }
            ]
        ),
        encoding="utf-8",
    )

    manager = ExperimentManager(
        project_root=tmp_path,
        config_root=tmp_path / "configs",
        output_root=output_root,
    )

    detail = manager.episode_detail("exp-shape", adapter_id="hardware", global_index=0)
    assert detail is not None
    assert detail["episode"]["task_label"] == "Open the fridge"
    assert detail["subtask"]["current"] == "Open the fridge door"
    assert detail["runtime"]["active_node"] == "subtask"
    assert detail["media"]["live"]["session_id"] == "real-robot-session"
    assert detail["media"]["live"]["frames"][0]["name"] == "front"


def test_episode_detail_uses_explicit_source_and_identity(tmp_path: Path):
    output_root = tmp_path / "experiments"
    job_root = output_root / "exp-trace"
    trace_dir = job_root / "task.trace"
    trace_dir.mkdir(parents=True)
    trace_path = trace_dir / "trace.sqlite3"
    connection = sqlite3.connect(trace_path)
    connection.execute(
        """CREATE TABLE spans (
           span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_span_id TEXT,
           name TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT,
           status TEXT NOT NULL, experiment_id TEXT, run_id TEXT, episode_id TEXT,
           attributes_json TEXT NOT NULL)"""
    )
    connection.executemany(
        "INSERT INTO spans(span_id, trace_id, parent_span_id, name, start_time, end_time, status, attributes_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "s-node",
                "trace-1",
                None,
                "node:observe",
                "2026-09-18T00:00:00+00:00",
                "2026-09-18T00:00:01+00:00",
                "ok",
                json.dumps({"kind": "node", "node": "observe", "step_index": 1}),
            ),
            (
                "s-tool",
                "trace-1",
                "s-node",
                "tool:Xiaomi-Robotics-1",
                "2026-09-18T00:00:01+00:00",
                "2026-09-18T00:00:02+00:00",
                "ok",
                json.dumps({"kind": "tool", "tool": "Xiaomi-Robotics-1"}),
            ),
        ],
    )
    connection.execute(
        "UPDATE spans SET run_id='run-not-trace', episode_id='episode-1'"
    )
    connection.execute(
        "INSERT INTO spans SELECT 'other', 'trace-other', NULL, 'other', start_time, end_time, status, experiment_id, 'other-run', episode_id, attributes_json FROM spans LIMIT 1"
    )
    connection.commit()
    connection.close()

    episode_path = job_root / "episode.json"
    episode_path.write_text(
        json.dumps(
            {
                "adapter_id": "sim",
                "global_index": 0,
                "task_id": "task",
                "result": {
                    "trace_sqlite": str(trace_path),
                    "identity": {"run_id": "run-not-trace", "episode_id": "episode-1"},
                },
            }
        ),
        encoding="utf-8",
    )
    summary_path = job_root / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "adapters": [
                    {
                        "id": "sim",
                        "cases": [
                            {
                                "adapter": "sim",
                                "case_index": 0,
                                "task_id": "task",
                                "episode_json": str(episode_path),
                                "status": "completed",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    status_path = job_root / "status.json"
    status_path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    (output_root / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "job_id": "exp-trace",
                    "status": "completed",
                    "output_root": str(job_root),
                    "summary_path": str(summary_path),
                    "status_path": str(status_path),
                }
            ]
        ),
        encoding="utf-8",
    )

    from robotenon.infra.trace_query import TraceQuery

    manager = ExperimentManager(
        trace_query=TraceQuery(trace_path),
        project_root=tmp_path,
        config_root=tmp_path / "configs",
        output_root=output_root,
    )

    detail = manager.episode_detail("exp-trace", adapter_id="sim", global_index=0)

    assert detail is not None
    timeline = detail["timeline"]
    assert timeline["available"] is True
    assert timeline["source"] == str(trace_path)
    assert timeline["trace_id"] == "trace-1"
    assert timeline["span_count"] == 2
    assert [span["category"] for span in timeline["spans"]] == ["envs", "tool"]
    assert timeline["spans"][0]["node"] == "observe"

    # A different/missing configured source never falls back to the episode file.
    manager.trace_query = TraceQuery(tmp_path / "missing.sqlite3")
    assert (
        manager.episode_detail("exp-trace", adapter_id="sim", global_index=0)[
            "timeline"
        ]["available"]
        is False
    )


def test_portal_episode_uses_job_trace_and_observation_artifact(tmp_path: Path):
    output_root = tmp_path / "experiments"
    job_root = output_root / "exp-portal"
    task_root = job_root / "OpenDoor" / "episode_000_seed_7"
    attempt_root = task_root / "attempts" / "attempt-1"
    observation_path = (
        attempt_root
        / "resources"
        / "observations"
        / "session-1"
        / "epoch-0"
        / "0"
        / "video.front.npy"
    )
    observation_path.parent.mkdir(parents=True)
    observation_path.write_bytes(b"npy-placeholder")

    trace_path = job_root / "observability" / "trace.sqlite3"
    trace_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(trace_path)
    connection.execute(
        """CREATE TABLE spans (
           span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_span_id TEXT,
           name TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT,
           status TEXT NOT NULL, experiment_id TEXT, run_id TEXT, episode_id TEXT,
           attributes_json TEXT NOT NULL)"""
    )
    connection.execute(
        "INSERT INTO spans(span_id, trace_id, name, start_time, end_time, status, run_id, episode_id, attributes_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "span-portal",
            "trace-portal",
            "node:observe",
            "2026-09-18T00:00:00+00:00",
            "2026-09-18T00:00:01+00:00",
            "ok",
            "run-portal",
            "episode-portal",
            json.dumps(
                {
                    "kind": "node",
                    "node": "observe",
                    "graph": "g",
                    "step": 1,
                    "path": ["g", "observe", "1"],
                    "frames": {
                        "front": {
                            "uri": "cache://session-1/epoch-0/feedback-trace/video.front",
                            "modality": "rgb",
                            "encoding": "npy",
                            "sequence": 0,
                            "storage_path": str(observation_path),
                        }
                    },
                }
            ),
        ),
    )
    connection.commit()
    connection.close()

    episode_path = job_root / "OpenDoor" / "episode_000_seed_7.json"
    episode_path.parent.mkdir(parents=True, exist_ok=True)
    episode_path.write_text(
        json.dumps(
            {
                "adapter_id": "sim",
                "global_index": 0,
                "task_id": "open-door",
                "identity": {"run_id": "run-portal", "episode_id": "episode-portal"},
                "context": {
                    "values": {
                        "feedback": {
                            "session_id": "session-1",
                            "observation": {
                                "frames": [
                                    {
                                        "frame_id": "video.front",
                                        "modality": "rgb",
                                        "artifact_ref": {
                                            "uri": "cache://session-1/epoch-0/feedback-1/video.front",
                                            "encoding": "npy",
                                            "sequence": 0,
                                        },
                                    }
                                ]
                            },
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    summary_path = job_root / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "adapters": [
                    {
                        "id": "sim",
                        "cases": [
                            {
                                "adapter": "sim",
                                "case_index": 0,
                                "episode_json": str(episode_path),
                                "status": "completed",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (job_root / "status.json").write_text(
        json.dumps({"status": "completed"}), encoding="utf-8"
    )
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "job_id": "exp-portal",
                    "status": "completed",
                    "output_root": str(job_root),
                    "summary_path": str(summary_path),
                    "status_path": str(job_root / "status.json"),
                }
            ]
        ),
        encoding="utf-8",
    )

    manager = ExperimentManager(
        project_root=tmp_path,
        config_root=tmp_path / "configs",
        output_root=output_root,
    )

    detail = manager.episode_detail("exp-portal", adapter_id="sim", global_index=0)

    assert detail is not None
    assert detail["timeline"]["available"] is True
    assert detail["timeline"]["source"] == str(trace_path)
    frame = detail["media"]["live"]["frames"][0]
    assert frame["storage_path"] == str(observation_path)
    assert frame["url"].startswith("/api/experiments/exp-portal/observation?")
    assert "artifact_id=" in frame["url"]
    assert "format=jpeg" in frame["url"]
    runtime_node = next(
        node for node in detail["runtime"]["nodes"] if node["id"] == "observe"
    )
    assert runtime_node["observation_count"] == 1
    assert runtime_node["observation_groups"][0]["frame_count"] == 1
    assert runtime_node["observation_groups"][0]["frames"][0]["artifact_id"]
    assert runtime_node["observation_groups"][0]["frames"][0]["url"].startswith(
        "/api/experiments/exp-portal/observation?"
    )
    trace_frame = runtime_node["observation_groups"][0]["frames"][0]
    assert (
        manager.episode_observation_path(
            "exp-portal",
            adapter_id="sim",
            global_index=0,
            uri=trace_frame["uri"],
        )
        == observation_path
    )
    assert (
        manager.episode_observation_path(
            "exp-portal",
            adapter_id="sim",
            global_index=0,
            uri=frame["uri"],
        )
        == observation_path
    )
    # The URL already carries the validated artifact path.  Reusing it avoids
    # reparsing the episode and Trace database for every camera tile.
    assert (
        manager.episode_observation_path(
            "exp-portal",
            adapter_id="sim",
            global_index=0,
            uri="cache://unused",
            storage_path=str(observation_path),
        )
        == observation_path
    )


def test_trace_category_separates_environment_and_monitor_system_nodes():
    environment = ExperimentManager._timeline_span(
        {
            "span_id": "env-span",
            "trace_id": "trace",
            "name": "node:g:body_execute",
            "start_time": "2026-09-18T00:00:00+00:00",
            "end_time": "2026-09-18T00:00:01+00:00",
            "status": "ok",
            "attributes": {
                "kind": "node",
                "node": "body_execute",
                "node_type": "system",
                "operation": "body.execute",
            },
        },
        0,
    )
    monitor = ExperimentManager._timeline_span(
        {
            "span_id": "monitor-span",
            "trace_id": "trace",
            "name": "node:g:monitor",
            "start_time": "2026-09-18T00:00:00+00:00",
            "end_time": "2026-09-18T00:00:01+00:00",
            "status": "ok",
            "attributes": {
                "kind": "node",
                "node": "monitor",
                "node_type": "system",
                "operation": "monitor.check",
            },
        },
        1,
    )

    assert environment is not None
    assert (environment["category"], environment["label"], environment["node"]) == (
        "envs",
        "body.execute",
        "body_execute",
    )
    assert monitor is not None
    assert monitor["category"] == "monitor"


def test_experiment_manager_returns_selected_master_config_document(tmp_path: Path):
    project_root = Path(__file__).parents[1]
    manager = ExperimentManager(
        project_root=project_root,
        config_root=fixture_config_root(project_root),
        output_root=tmp_path / "experiments",
    )

    document = manager.get_config_document("experiment_fixture.json")

    assert document["id"] == "experiment_fixture.json"
    assert (
        document["json"]["task_adapters_config"] == "experiment_fixture_task_adapters.json"
    )
    assert document["json"]["parameters"]["task"]["seed"] == 7


def test_experiment_manager_marks_nonzero_persisted_returncode_as_failed(
    tmp_path: Path,
):
    output_root = tmp_path / "experiments"
    job_root = output_root / "exp-failed"
    job_root.mkdir(parents=True)
    status_path = job_root / "status.json"
    summary_path = job_root / "summary.json"
    status_path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    summary_path.write_text(
        json.dumps(
            {
                "num_episodes": 0,
                "num_errors": 1,
                "successes": 0,
                "task_adapters": {"sim": {"num_cases": 1, "successes": 0}},
            }
        ),
        encoding="utf-8",
    )
    (output_root / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "job_id": "exp-failed",
                    "status": "completed",
                    "returncode": 1,
                    "summary_path": str(summary_path),
                    "status_path": str(status_path),
                    "output_root": str(job_root),
                }
            ]
        ),
        encoding="utf-8",
    )

    manager = ExperimentManager(
        project_root=tmp_path,
        config_root=tmp_path / "configs",
        output_root=output_root,
    )

    record = manager.list_jobs()[0]

    assert record["status"] == "failed"
    assert record["returncode"] == 1


def test_experiment_manager_finalizes_orphaned_running_job_after_portal_restart(
    tmp_path: Path,
):
    output_root = tmp_path / "experiments"
    job_root = output_root / "exp-orphan"
    job_root.mkdir(parents=True)
    status_path = job_root / "status.json"
    status_path.write_text(
        json.dumps(
            {
                "run_id": "exp-orphan",
                "status": "running",
                "completed_episodes": 1,
                "total_episodes": 3,
                "active": [
                    {
                        "adapter_id": "sim",
                        "global_index": 1,
                        "task_id": "task-1",
                        "status": "running",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (output_root / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "job_id": "exp-orphan",
                    "status": "running",
                    "pid": 99999999,
                    "returncode": None,
                    "output_root": str(job_root),
                    "status_path": str(status_path),
                }
            ]
        ),
        encoding="utf-8",
    )

    manager = ExperimentManager(
        project_root=tmp_path,
        config_root=tmp_path / "configs",
        output_root=output_root,
    )

    record = manager.list_jobs()[0]

    assert record["status"] == "failed"
    assert record["returncode"] == 1
    assert (
        record["failure_reason"]
        == "experiment process exited before writing a terminal status"
    )
    assert record["progress"]["status"] == "failed"
    assert record["progress"]["active"][0]["status"] == "failed"
    persisted = json.loads(status_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "failed"
    assert persisted["active"][0]["status"] == "failed"
    episodes = manager.list_episodes("exp-orphan", include_runtime=False)
    assert episodes[0]["status"] == "failed"


def test_experiment_manager_uses_summary_errors_after_portal_restart(tmp_path: Path):
    output_root = tmp_path / "experiments"
    job_root = output_root / "exp-summary-error"
    job_root.mkdir(parents=True)
    status_path = job_root / "status.json"
    summary_path = job_root / "summary.json"
    status_path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    summary_path.write_text(
        json.dumps({"num_episodes": 0, "num_errors": 1, "successes": 0}),
        encoding="utf-8",
    )
    (output_root / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "job_id": "exp-summary-error",
                    "status": "completed",
                    "summary_path": str(summary_path),
                    "status_path": str(status_path),
                    "output_root": str(job_root),
                }
            ]
        ),
        encoding="utf-8",
    )

    manager = ExperimentManager(
        project_root=tmp_path,
        config_root=tmp_path / "configs",
        output_root=output_root,
    )

    record = manager.list_jobs()[0]

    assert record["status"] == "failed"
    assert record["returncode"] == 1


def test_experiment_manager_marks_progress_failed_when_summary_has_errors(
    tmp_path: Path,
):
    output_root = tmp_path / "experiments"
    job_root = output_root / "exp-summary-progress"
    job_root.mkdir(parents=True)
    status_path = job_root / "status.json"
    summary_path = job_root / "summary.json"
    status_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "completed_episodes": 1,
                "total_episodes": 1,
            }
        ),
        encoding="utf-8",
    )
    summary_path.write_text(
        json.dumps({"num_episodes": 1, "num_errors": 1, "successes": 0}),
        encoding="utf-8",
    )
    (output_root / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "job_id": "exp-summary-progress",
                    "status": "completed",
                    "returncode": None,
                    "summary_path": str(summary_path),
                    "status_path": str(status_path),
                    "output_root": str(job_root),
                }
            ]
        ),
        encoding="utf-8",
    )

    manager = ExperimentManager(
        project_root=tmp_path,
        config_root=tmp_path / "configs",
        output_root=output_root,
    )

    record = manager.list_jobs()[0]

    assert record["status"] == "failed"
    assert record["returncode"] == 1
    assert record["progress"]["status"] == "failed"


def test_experiment_manager_keeps_live_job_running_with_partial_summary_errors(
    tmp_path: Path, monkeypatch
):
    output_root = tmp_path / "experiments"
    job_root = output_root / "exp-live-errors"
    job_root.mkdir(parents=True)
    status_path = job_root / "status.json"
    summary_path = job_root / "summary.json"
    status_path.write_text(
        json.dumps(
            {
                "run_id": "exp-live-errors",
                "status": "running",
                "completed_episodes": 3,
                "total_episodes": 10,
                "active": [],
            }
        ),
        encoding="utf-8",
    )
    summary_path.write_text(
        json.dumps({"num_episodes": 3, "num_errors": 1, "successes": 1}),
        encoding="utf-8",
    )
    (output_root / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "job_id": "exp-live-errors",
                    "status": "running",
                    "pid": 12345,
                    "returncode": None,
                    "summary_path": str(summary_path),
                    "status_path": str(status_path),
                    "output_root": str(job_root),
                }
            ]
        ),
        encoding="utf-8",
    )

    manager = ExperimentManager(
        project_root=tmp_path,
        config_root=tmp_path / "configs",
        output_root=output_root,
    )
    monkeypatch.setattr(manager, "_pid_is_experiment", lambda *args, **kwargs: True)

    record = manager.list_jobs()[0]

    assert record["status"] == "running"
    assert record["progress"]["status"] == "running"
    assert record["returncode"] is None


def test_experiment_manager_persists_effective_run_parameters(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("robotenon.runtime.processes.process_table", lambda: {})
    project_root = Path(__file__).parents[1]

    class FakeProcess:
        pid = 12345

        def poll(self):
            return None

    monkeypatch.setattr(
        "robotenon.web.experiments.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    manager = ExperimentManager(
        project_root=project_root,
        config_root=fixture_config_root(project_root),
        output_root=tmp_path / "experiments",
    )

    job = manager.submit(
        "experiment_fixture.json",
        overrides={"parameters": {"task": {"seed": 42}}},
    )

    assert job["parameters"]["task"]["seed"] == 42
    assert job["parameter_hash"]
    assert "--experiment-config" in job["command"]
    override_path = Path(job["experiment_config_path"])
    assert (
        json.loads(override_path.read_text(encoding="utf-8"))["parameters"]["task"][
            "seed"
        ]
        == 42
    )

    forked = manager.fork(job["job_id"])
    assert forked["forked_from"] == job["job_id"]
    assert forked["parameters"] == job["parameters"]
    assert (
        json.loads(Path(forked["config_snapshot_path"]).read_text(encoding="utf-8"))[
            "config"
        ]
        == job["parameters"]
    )


def test_experiment_manager_accepts_wrapped_and_keyed_episode_shapes():
    summary = {
        "data": {
            "adapters": [
                {
                    "name": "physical",
                    "cases": {
                        "0": {"task_id": "open-door"},
                        "1": {"task_id": "close-door"},
                    },
                }
            ]
        }
    }

    records = list(ExperimentManager._iter_summary_episodes(summary))
    assert [(adapter, item["task_id"]) for adapter, item in records] == [
        ("physical", "open-door"),
        ("physical", "close-door"),
    ]
    frames = ExperimentManager._feedback_frames(
        {"observation": {"channels": {"front": {"uri": "cache://front"}}}}
    )
    assert frames[0]["name"] == "front"
    assert frames[0]["uri"] == "cache://front"


def test_observation_sequence_preserves_multiple_images_from_one_camera():
    frames = ExperimentManager._feedback_frames(
        {
            "observation": {
                "frames": {
                    "front": [
                        {"uri": "cache://front-001", "modality": "rgb"},
                        {"uri": "cache://front-002", "modality": "rgb"},
                    ],
                    "left": {"uri": "cache://left-001", "modality": "rgb"},
                }
            }
        }
    )

    sequence = ExperimentManager._frame_sequence(frames, visual_only=True)

    assert [frame["uri"] for frame in sequence] == [
        "cache://front-001",
        "cache://front-002",
        "cache://left-001",
    ]

    runtime = ExperimentManager._episode_graph_runtime(
        {
            "result": {
                "context": {
                    "history": [
                        {
                            "node": "observe",
                            "event": "completed",
                            "observation": {
                                "frames": {
                                    "front": [
                                        {"uri": "cache://front-001", "modality": "rgb"},
                                        {"uri": "cache://front-002", "modality": "rgb"},
                                    ],
                                    "left": {
                                        "uri": "cache://left-001",
                                        "modality": "rgb",
                                    },
                                }
                            },
                        }
                    ]
                }
            }
        }
    )

    assert runtime["sequence"][0]["observation_groups"][0]["frame_count"] == 3


def test_observation_groups_keep_all_observe_visits_for_a_node():
    runtime = ExperimentManager._episode_graph_runtime(
        {
            "result": {
                "context": {
                    "history": [
                        {
                            "node": "observe",
                            "event": "completed",
                            "step": 1,
                            "observation": {
                                "frames": {
                                    "front": {
                                        "uri": "cache://front-1",
                                        "modality": "rgb",
                                    },
                                    "wrist": {
                                        "uri": "cache://wrist-1",
                                        "modality": "rgb",
                                    },
                                    "left": {
                                        "uri": "cache://left-1",
                                        "modality": "rgb",
                                    },
                                }
                            },
                        },
                        {
                            "node": "observe",
                            "event": "completed",
                            "step": 2,
                            "observation": {
                                "frames": {
                                    "front": {
                                        "uri": "cache://front-2",
                                        "modality": "rgb",
                                    },
                                    "wrist": {
                                        "uri": "cache://wrist-2",
                                        "modality": "rgb",
                                    },
                                    "left": {
                                        "uri": "cache://left-2",
                                        "modality": "rgb",
                                    },
                                }
                            },
                        },
                    ]
                }
            }
        }
    )

    observe_node = next(node for node in runtime["nodes"] if node["id"] == "observe")
    assert len(observe_node["observation_groups"]) == 2
    assert [
        group["frames"][0]["uri"] for group in observe_node["observation_groups"]
    ] == [
        "cache://front-1",
        "cache://front-2",
    ]
    assert [group["frame_count"] for group in observe_node["observation_groups"]] == [
        3,
        3,
    ]


def test_stop_job_releases_the_runner_process_group_and_persists_cancelled(
    tmp_path: Path,
):
    output_root = tmp_path / "experiments"
    manager = ExperimentManager(
        project_root=tmp_path,
        config_root=tmp_path / "configs",
        output_root=output_root,
    )
    job_id = "exp-running"
    job_root = output_root / job_id
    job_root.mkdir(parents=True)
    status_path = job_root / "status.json"
    summary_path = job_root / "summary.json"
    status_path.write_text(json.dumps({"status": "running"}), encoding="utf-8")
    process = subprocess.Popen(
        [manager.python_executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    manager._jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "pid": process.pid,
        "status_path": str(status_path),
        "summary_path": str(summary_path),
        "output_root": str(job_root),
    }
    manager._processes[job_id] = process

    result = manager.stop_job(job_id)

    assert result["action"] == "stop"
    assert result["released"] is True
    assert result["target"] == {"adapter_id": None, "global_index": None}
    assert process.poll() is not None
    assert manager._jobs[job_id]["status"] == "cancelled"
    persisted = json.loads(status_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "cancelled"
    assert persisted["cancel_reason"] == "user_stop"
    restored = ExperimentManager(project_root=tmp_path, output_root=output_root)
    assert restored._jobs[job_id]["status"] == "cancelled"


def test_stop_job_does_not_hide_a_failed_release(tmp_path: Path, monkeypatch):
    output_root = tmp_path / "experiments"
    manager = ExperimentManager(
        project_root=tmp_path,
        config_root=tmp_path / "configs",
        output_root=output_root,
    )
    job_id = "exp-unreleased"
    job_root = output_root / job_id
    job_root.mkdir(parents=True)
    status_path = job_root / "status.json"
    status_path.write_text(json.dumps({"status": "running"}), encoding="utf-8")
    manager._jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "status_path": str(status_path),
        "summary_path": str(job_root / "summary.json"),
        "output_root": str(job_root),
    }
    monkeypatch.setattr(
        manager,
        "_terminate_process_group",
        lambda *args, **kwargs: {
            "pid": 123,
            "terminated": False,
            "forced": False,
            "reason": "refusing_portal_process_group",
        },
    )

    try:
        manager.stop_job(job_id)
    except RuntimeError as exc:
        assert "not fully released" in str(exc)
    else:
        raise AssertionError("stop_job must fail when the process group remains live")

    assert manager._jobs[job_id]["status"] == "stop_failed"
    assert json.loads(status_path.read_text(encoding="utf-8"))["status"] == "running"


def test_shutdown_all_only_interrupts_running_jobs(tmp_path: Path, monkeypatch):
    manager = ExperimentManager(
        project_root=tmp_path,
        config_root=tmp_path / "configs",
        output_root=tmp_path / "experiments",
    )
    manager._jobs = {
        "exp-running": {"job_id": "exp-running"},
        "exp-completed": {"job_id": "exp-completed"},
    }
    statuses = {
        "exp-running": {"status": "running", "progress": {"status": "running"}},
        "exp-completed": {"status": "completed", "progress": {"status": "completed"}},
    }
    monkeypatch.setattr(manager, "_refresh_job", lambda job_id: statuses[job_id])
    stopped: list[tuple[str, str]] = []

    def fake_stop(job_id: str, *, reason: str, wait: bool = True, **kwargs):
        stopped.append((job_id, reason))
        return {"released": True}

    monkeypatch.setattr(manager, "stop_job", fake_stop)

    result = manager.shutdown_all(reason="portal_shutdown")

    assert stopped == [("exp-running", "portal_shutdown")]
    assert result["count"] == 1
    assert result["interrupted"] == [
        {"job_id": "exp-running", "released": True, "pending": False}
    ]


def test_rerun_selected_robocasa_case_narrows_the_adapter_override(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("robotenon.runtime.processes.process_table", lambda: {})
    project_root = Path(__file__).parents[1]

    class FakeProcess:
        next_pid = 20000

        def __init__(self):
            self.pid = FakeProcess.next_pid
            FakeProcess.next_pid += 1

        def poll(self):
            return None

    monkeypatch.setattr(
        "robotenon.web.experiments.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    manager = ExperimentManager(
        project_root=project_root,
        config_root=fixture_config_root(project_root),
        output_root=tmp_path / "experiments",
    )
    source = manager.submit("experiment_fixture.json")
    source_root = Path(source["output_root"])
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "status.json").write_text(
        json.dumps({"status": "completed"}), encoding="utf-8"
    )
    (source_root / "summary.json").write_text(
        json.dumps(
            {
                "task_adapters": {
                    "robocasa365": {
                        "episodes": [
                            {
                                "task_id": "CloseBlenderLid",
                                "global_index": 0,
                                "seed": 7,
                                "status": "completed",
                            }
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    manager._processes.pop(source["job_id"], None)
    manager._jobs[source["job_id"]]["pid"] = None
    stale_fields = {
        "status": "cancelled",
        "cancel_reason": "user_stop",
        "cancelled_at": "yesterday",
        "ended_at": "yesterday",
        "termination": {"terminated": True},
        "resources_released": True,
        "stop_progress": {"phase": "released"},
        "error": "old failure",
        "returncode": 1,
    }
    manager._jobs[source["job_id"]].update(stale_fields)
    (source_root / "status.json").write_text(
        json.dumps({"status": "cancelled"}), encoding="utf-8"
    )
    rerun = manager.rerun_job(
        source["job_id"],
        adapter_id="robocasa365",
        global_index=0,
        source={"type": "test", "remote_address": "127.0.0.1"},
    )

    assert rerun["job_id"] == source["job_id"]
    assert rerun["scope"] == "tasks"
    assert len(manager._jobs) == 1
    resume = json.loads((source_root / ".control" / "resume.json").read_text())
    assert resume["tasks"] == [{"adapter_id": "robocasa365", "global_index": 0}]
    assert manager._jobs[source["job_id"]]["command"] == source["command"]
    for key in stale_fields:
        if key != "status":
            assert key not in manager._jobs[source["job_id"]]
    audit = [
        json.loads(line) for line in manager.control_audit_path.read_text().splitlines()
    ]
    assert audit[-1] == {
        "timestamp": audit[-1]["timestamp"],
        "action": "rerun",
        "job_id": source["job_id"],
        "request_source": {"type": "test", "remote_address": "127.0.0.1"},
        "target_tasks": [{"adapter_id": "robocasa365", "global_index": 0}],
        "reason": "user_rerun",
    }


def test_manager_repairs_terminal_fields_on_waiting_legacy_job(tmp_path: Path):
    output_root = tmp_path / "experiments"
    output_root.mkdir()
    legacy = {
        "job_id": "exp-waiting",
        "submitted_at": "2026-09-21T00:00:00Z",
        "status": "waiting",
        "cancel_reason": "user_stop",
        "cancelled_at": "2026-09-20T00:00:00Z",
        "ended_at": "2026-09-20T00:00:00Z",
        "termination": {"terminated": True},
        "resources_released": True,
        "stop_progress": {"phase": "released"},
        "error": "old failure",
        "returncode": 1,
    }
    (output_root / "jobs.json").write_text(json.dumps([legacy]), encoding="utf-8")

    manager = ExperimentManager(project_root=tmp_path, output_root=output_root)
    repaired = manager._jobs["exp-waiting"]

    assert repaired["status"] == "waiting"
    for key in (
        "cancel_reason",
        "cancelled_at",
        "ended_at",
        "termination",
        "resources_released",
        "stop_progress",
        "error",
        "returncode",
    ):
        assert key not in repaired


def test_video_faststart_path_keeps_already_indexed_mp4(tmp_path: Path):
    manager = ExperimentManager(
        project_root=tmp_path,
        config_root=tmp_path / "configs",
        output_root=tmp_path / "experiments",
    )
    video = tmp_path / "video.mp4"
    # A minimal atom ordering check is enough here; no codec dependency is
    # needed to verify that already fast-start files are served in place.
    video.write_bytes(
        b"\x00\x00\x00\x18ftypisom" + b"\x00\x00\x00\x10moov" + b"\x00\x00\x00\x10mdat"
    )

    assert manager._video_faststart_path(video) == video


def test_waiting_queue_serializes_concurrent_submissions_and_recovers(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("robotenon.runtime.processes.process_table", lambda: {})
    from concurrent.futures import ThreadPoolExecutor

    processes = []

    class FakeProcess:
        returncode = None

        def __init__(self):
            self.pid = 40000 + len(processes)
            processes.append(self)

        def poll(self):
            return self.returncode

    monkeypatch.setattr(
        "robotenon.web.experiments.subprocess.Popen", lambda *a, **kw: FakeProcess()
    )
    root = Path(__file__).parents[1]
    manager = ExperimentManager(
        project_root=root,
        config_root=fixture_config_root(root),
        output_root=tmp_path,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(lambda _: manager.submit("experiment_fixture.json"), range(2)))
    assert sorted(j["status"] for j in jobs) == ["running", "waiting"]
    assert len(processes) == 1
    waiting = next(j for j in jobs if j["status"] == "waiting")
    assert waiting["pid"] is None
    restored = ExperimentManager(
        project_root=root,
        config_root=fixture_config_root(root),
        output_root=tmp_path,
    )
    monkeypatch.setattr(restored, "_pid_is_experiment", lambda *a, **kw: True)
    assert sum(j["status"] == "waiting" for j in restored.list_jobs()) == 1
    assert len(processes) == 1
    monkeypatch.setattr(restored, "_pid_is_experiment", lambda *a, **kw: False)
    restored.list_jobs()
    assert len(processes) == 2
    assert restored.get_job(waiting["job_id"])["status"] == "running"


def test_waiting_queue_cancellation_and_background_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr("robotenon.runtime.processes.process_table", lambda: {})
    import time

    processes = []

    class FakeProcess:
        pid = 45000
        returncode = None

        def poll(self):
            return self.returncode

    def launch(*args, **kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr("robotenon.web.experiments.subprocess.Popen", launch)
    project_root = Path(__file__).parents[1]
    manager = ExperimentManager(
        project_root=project_root,
        config_root=fixture_config_root(project_root),
        output_root=tmp_path,
    )
    first = manager.submit("experiment_fixture.json")
    cancelled = manager.submit("experiment_fixture.json")
    last = manager.submit("experiment_fixture.json")
    assert manager.stop_job(cancelled["job_id"])["job"]["status"] == "cancelled"
    processes[0].returncode = 0
    manager.start()
    try:
        deadline = time.monotonic() + 4
        while len(processes) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(processes) == 2
        assert manager.get_job(last["job_id"])["status"] == "running"
        assert manager.get_job(first["job_id"])["status"] == "completed"
    finally:
        manager.close()


def test_timeline_excludes_routing_checks_but_keeps_model_calls():
    common = {
        "start_time": "2026-09-20T00:00:00.000100+00:00",
        "end_time": "2026-09-20T00:00:00.000200+00:00",
        "status": "ok",
    }
    assert (
        ExperimentManager._timeline_span(
            dict(
                common,
                name="routing:plan",
                attributes={"kind": "routing", "node": "plan"},
            ),
            0,
        )
        is None
    )
    span = ExperimentManager._timeline_span(
        dict(common, name="node:g:plan", attributes={"kind": "node", "node": "plan"}), 1
    )
    assert span["category"] == "module"
    assert span["label"] == "plan"


def test_queue_runs_real_children_in_submission_order(tmp_path):
    import time

    runner = tmp_path / "runner.py"
    runner.write_text("sleep 1\n")
    project_root = Path(__file__).parents[1]
    manager = ExperimentManager(
        project_root=project_root,
        config_root=fixture_config_root(project_root),
        output_root=tmp_path / "runs",
        entrypoint=runner,
        python_executable="/bin/sh",
    )
    first = manager.submit("experiment_fixture.json")
    second = manager.submit("experiment_fixture.json")
    assert second["status"] == "waiting"
    manager.start()
    try:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            record = manager.get_job(second["job_id"])
            if record["status"] == "completed":
                break
            time.sleep(0.05)
        assert record["status"] == "completed"
        assert manager.get_job(first["job_id"])["ended_at"] <= record["started_at"]
    finally:
        manager.close()


def test_node_detail_keeps_loop_visits_separate_and_restores_scoped_output(
    tmp_path, monkeypatch
):
    manager = ExperimentManager(project_root=tmp_path, output_root=tmp_path / "runs")
    first, second = ["g", "plan", "2"], ["g", "plan", "9"]
    record = {
        "result": {
            "context": {
                "scoped_outputs": {
                    json.dumps(first): {"plan": {"subgoal": "first goal"}},
                    json.dumps(second): {"plan": {"subgoal": "second goal"}},
                }
            }
        }
    }
    rows = [
        {
            "name": "node:g:plan",
            "attributes": {"kind": "node", "path": first, "output": None},
        },
        {
            "name": "tool:qwen",
            "attributes": {
                "kind": "tool",
                "caller_path": first,
                "arguments": {"prompt": "first prompt"},
                "result": {"subgoal": "first goal"},
            },
        },
        {
            "name": "tool:qwen",
            "attributes": {
                "kind": "tool",
                "caller_path": second,
                "arguments": {"prompt": "second prompt"},
                "result": {"subgoal": "second goal"},
            },
        },
    ]
    monkeypatch.setattr(manager, "_episode_record", lambda *a, **kw: ({}, record))
    monkeypatch.setattr(manager, "_episode_trace_rows", lambda *a: rows)
    detail = manager.node_detail("job", adapter_id="sim", global_index=0, path=first)
    assert detail["output"]["plan"]["subgoal"] == "first goal"
    assert detail["output_source"] == "scoped_output"
    assert len(detail["tool_calls"]) == 1
    assert detail["tool_calls"][0]["arguments"]["prompt"] == "first prompt"
    missing = manager.node_detail(
        "job", adapter_id="sim", global_index=0, path=["g", "plan", "100"]
    )
    assert missing["output"] is None and missing["tool_calls"] == []
    assert missing["output_note"]


def test_episode_trace_rows_continue_across_byte_budget_pages(tmp_path, monkeypatch):
    manager = ExperimentManager(project_root=tmp_path, output_root=tmp_path / "runs")

    class PagedTrace:
        def __init__(self):
            self.offsets = []

        def episode_page(self, *, offset, **kwargs):
            self.offsets.append(offset)
            pages = {
                0: {"rows": [{"span_id": "a"}, {"span_id": "b"}], "next_offset": 2},
                2: {"rows": [{"span_id": "c"}], "next_offset": None},
            }
            return pages[offset]

    reader = PagedTrace()
    monkeypatch.setattr(manager, "_job_trace_query", lambda job: reader)
    rows = manager._episode_trace_rows(
        {},
        {},
        {
            "identity": {
                "run_id": "run",
                "episode_id": "episode",
                "attempt_id": "attempt",
            }
        },
    )

    assert [row["span_id"] for row in rows] == ["a", "b", "c"]
    assert reader.offsets == [0, 2]
    assert rows.truncated is False


def test_crashed_episode_recovers_latest_attempt_media_only(tmp_path):
    root = tmp_path / "exp-crash"
    root.mkdir()
    manager = ExperimentManager(
        project_root=tmp_path,
        config_root=tmp_path / "configs",
        output_root=tmp_path / "jobs",
    )
    episode = root / "Task" / "episode.json"
    for retry in (0, 1):
        directory = episode.with_suffix("") / "attempts" / str(retry)
        directory.mkdir(parents=True)
        (directory / "status.json").write_text(
            json.dumps(
                {
                    "retry": retry,
                    "identity": {"run_id": "exp-crash", "attempt_id": str(retry)},
                }
            )
        )
        (directory / "video.mp4").write_bytes(b"video")
    live = episode.parent / ".live" / episode.name
    live.parent.mkdir()
    snapshot = {
        "identity": {"run_id": "exp-crash", "attempt_id": "1"},
        "context": {
            "status": "running",
            "history": [{"node": "action", "event": "enter"}],
        },
        "feedback": {"session_id": "session", "success": False},
    }
    live.write_text(json.dumps(snapshot))
    job = {"job_id": "exp-crash", "output_root": str(root)}
    record = {"episode_json": str(episode), "status": "error", "error": "SIGABRT"}
    recovered = manager._recover_failed_episode_media(job, record)
    assert recovered["video_path"].endswith("/attempts/1/video.mp4")
    assert recovered["status"] == "error"
    assert recovered["context"]["status"] == "error"
    assert recovered["feedback"]["session_id"] == "session"
    # A stale prior attempt snapshot must never supply the retry's images.
    snapshot["identity"]["attempt_id"] = "0"
    live.write_text(json.dumps(snapshot))
    recovered = manager._recover_failed_episode_media(job, record)
    assert "feedback" not in recovered
    (episode.with_suffix("") / "attempts/1/video.mp4").unlink()
    recovered = manager._recover_failed_episode_media(job, record)
    assert "video_path" not in recovered


def test_terminal_output_tails_resumes_and_recovers_after_truncation(tmp_path):
    output_root = tmp_path / "experiments"
    output_root.mkdir()
    log_path = output_root / "exp-terminal.log"
    log_path.write_bytes(b"first line\nsecond line\nfailure detail\n")
    manager = ExperimentManager(project_root=tmp_path, output_root=output_root)
    manager._jobs["exp-terminal"] = {
        "job_id": "exp-terminal",
        "status": "failed",
        "log_path": str(log_path),
    }

    initial = manager.terminal_output("exp-terminal", limit=15)
    assert initial is not None
    assert initial["text"] == "failure detail\n"
    assert initial["truncated"] is True
    assert initial["complete"] is True

    with log_path.open("ab") as stream:
        stream.write(b"traceback\n")
    resumed = manager.terminal_output(
        "exp-terminal", offset=initial["next_offset"], limit=15
    )
    assert resumed is not None
    assert resumed["text"] == "traceback\n"
    assert resumed["reset"] is False

    log_path.write_bytes(b"new log\n")
    reset = manager.terminal_output(
        "exp-terminal", offset=resumed["next_offset"], limit=15
    )
    assert reset is not None
    assert reset["text"] == "new log\n"
    assert reset["reset"] is True
    assert manager.terminal_output("missing") is None


def test_trace_restoration_orders_truncated_history_and_separates_loop_visits(
    tmp_path, monkeypatch
):
    import copy

    manager = ExperimentManager(
        project_root=tmp_path, output_root=tmp_path / "experiments"
    )

    def event(node, step):
        return {
            "node": node,
            "step": step,
            "path": ["g", node, str(step)],
            "timestamp": f"2026-09-20T09:39:{step:02d}+00:00",
        }

    groups = [
        (
            node,
            {"event": event(node, step), "frames": [{"uri": f"cache://frame-{step}"}]},
        )
        for node, step in [("reset", 1), ("action", 2), ("action", 4)]
    ]
    # Only a recent visit survives the live history window. Its display order
    # collides with reset's real step, and it is the sole action candidate.
    live = {
        "sequence": [
            {
                "id": "action",
                "order": 1,
                "status": "completed",
                "events": [event("action", 4)],
            }
        ],
        "nodes": [],
    }
    for with_timestamps in (True, False):
        runtime, restored_groups = copy.deepcopy(live), copy.deepcopy(groups)
        if not with_timestamps:
            runtime["sequence"][0]["events"][0].pop("timestamp")
            for _, group in restored_groups:
                group["event"].pop("timestamp")
        monkeypatch.setattr(
            manager, "_trace_observation_groups", lambda *a, **kw: restored_groups
        )
        for _ in range(2):  # Repeated restoration must not duplicate visits.
            manager._restore_trace_observations(
                {},
                {},
                {},
                runtime,
                trace_rows=[{}],
                job_id="exp",
                adapter_id="sim",
                global_index=0,
            )
        sequence = runtime["sequence"]
        assert [
            (item["order"], item["id"], item["events"][0]["step"]) for item in sequence
        ] == [(1, "reset", 1), (2, "action", 2), (3, "action", 4)]
        assert [
            [frame["uri"] for frame in item["observations"]] for item in sequence
        ] == [["cache://frame-1"], ["cache://frame-2"], ["cache://frame-4"]]
