"""Small local experiment submission manager for the RoboTenon Portal."""

from __future__ import annotations

import json
import logging
import queue
import errno
import hashlib
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
import weakref
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from ..runtime.config import (
    load_application_config,
    freeze_config,
    load_config_snapshot,
)
from ..infra.trace_query import TraceQuery, QueryBudgetExceeded
from ..infra.history import page_window
from ..task_adapters.result_index import ResultIndex, IndexRecoveryPending
from ..storage.jobs import JobStore
from .cache import BoundedCache


MAX_TRAJECTORY_TRACE_ROWS = 1500


class _TraceRows(list):
    """Trace rows with an internal marker for a deliberately bounded result."""

    def __init__(self, values=(), *, truncated: bool = False):
        super().__init__(values)
        self.truncated = truncated


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExperimentManager:
    """Discover trusted configs and run one selected config at a time or in parallel.

    The manager intentionally launches a fixed Python entry point with an
    argument list.  It never evaluates a shell command received from HTTP.
    """

    def __init__(
        self,
        *,
        project_root: str | Path = ".",
        config_root: str | Path = "configs",
        output_root: str | Path = "data/experiments",
        entrypoint: str | Path = "scripts/run_experiment.py",
        python_executable: str | Path | None = None,
        trace_query: TraceQuery | None = None,
        retention_policy=None,
    ) -> None:
        self.trace_query = trace_query
        self.retention_policy = retention_policy
        self._retention_thread = None
        self.retention_report = None
        self._retention_checked_at = 0
        self.project_root = Path(project_root).resolve()
        self.config_root = self._resolve_under_project(config_root)
        self.output_root = self._resolve_under_project(output_root)
        self.entrypoint = self._resolve_under_project(entrypoint)
        self.python_executable = str(python_executable or sys.executable)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.jobs_path = self.output_root / "jobs.json"
        self.control_audit_path = self.output_root / "control-audit.jsonl"
        self._lock = threading.RLock()
        # Status and summary files are read on every Portal refresh. Cache
        # unchanged files so large episode histories are not reparsed each
        # time the UI polls for progress.
        self._json_cache = BoundedCache()
        self._config_cache = BoundedCache(max_entries=64)
        self._stop_threads: dict[str, threading.Thread] = {}
        self._jobs = self._load_jobs()
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        # Detail responses use short opaque artifact ids instead of repeating
        # absolute storage paths in every historical observation frame.
        self._observation_artifacts = BoundedCache(max_entries=4096)
        self._video_faststart_locks = weakref.WeakValueDictionary()
        self._scheduler_stop = threading.Event()
        self._scheduler_thread: threading.Thread | None = None
        self._recovery_queue = queue.Queue(maxsize=32)
        self._recovery_thread = None
        self._recovery_pending = set()
        self._recovery_checks = BoundedCache(max_entries=128)

    def start(self) -> None:
        """Dispatch persisted waiting jobs even when no browser is polling."""
        with self._lock:
            if self._scheduler_thread and self._scheduler_thread.is_alive():
                return
            # Resume an already authorised cleanup after a Portal restart.
            for job_id in self._active_job_keys():
                job = self._jobs[job_id]
                if job.get("status") == "stopping":
                    self.stop_job(
                        job_id,
                        reason=job.get("stop_progress", {}).get("reason", "user_stop"),
                        wait=False,
                        audit=False,
                    )
            self._scheduler_stop.clear()
            self._scheduler_thread = threading.Thread(
                target=self._scheduler_loop,
                name="experiment-queue",
                daemon=True,
            )
            self._scheduler_thread.start()

    def close(self) -> None:
        """Stop dispatching; running experiments survive a Portal restart."""
        self._scheduler_stop.set()
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=5)
        if self._recovery_thread:
            self._recovery_thread.join(timeout=5)
        with self._lock:
            self._persist_jobs()

    def _active_job_keys(self, *, waiting=False):
        if isinstance(self._jobs, JobStore):
            return list(
                dict.fromkeys(
                    [*self._jobs.active_keys(waiting=waiting), *self._processes]
                )
            )
        return list(self._jobs)

    def _scheduler_loop(self) -> None:
        while not self._scheduler_stop.wait(1):
            try:
                with self._lock:
                    self._dispatch_waiting()
                retention_interval = (
                    30 if (self.retention_report or {}).get("truncated") else 3600
                )
                if (
                    self.retention_policy
                    and time.monotonic() - self._retention_checked_at
                    > retention_interval
                ):
                    if (
                        self._retention_thread is None
                        or not self._retention_thread.is_alive()
                    ):
                        self._retention_checked_at = time.monotonic()
                        self._retention_thread = threading.Thread(
                            target=self._prune_history,
                            name="artifact-retention",
                            daemon=True,
                        )
                        self._retention_thread.start()
            except Exception:
                logging.getLogger(__name__).exception("experiment queue refresh failed")

    def _prune_history(self):
        from ..runtime.retention import prune_artifacts

        try:
            self.retention_report = prune_artifacts(
                self.output_root, self.retention_policy, apply=True
            )
        except Exception:
            logging.getLogger(__name__).exception("artifact retention failed")

    def _queue_index_recovery(self, job):
        if job.get("status") not in {"completed", "failed", "cancelled"} or not job.get(
            "resources_released"
        ):
            return
        root = self._job_path(job, "status_path").parent
        if not (root / ".control/run.lock").is_file():
            return
        key = job["job_id"]
        if key in self._recovery_pending:
            return
        if job.get("index_recovery", {}).get("retry_after", 0) > time.time():
            return
        index = ResultIndex(root)
        stamp = index.path.stat().st_mtime_ns if index.path.exists() else None
        if self._recovery_checks.get(key) == (stamp, job.get("ended_at")):
            return
        if not index.needs_recovery():
            self._recovery_checks[key] = (stamp, job.get("ended_at"))
            job.pop("index_recovery", None)
            return
        try:
            self._recovery_queue.put_nowait((key, root))
        except queue.Full:
            raise IndexRecoveryPending(
                "historical result recovery queue is full; retry shortly"
            ) from None
        self._recovery_pending.add(key)
        job["index_recovery"] = {"status": "pending"}
        self._recovery_checks[key] = (stamp, job.get("ended_at"))
        if self._recovery_thread is None or not self._recovery_thread.is_alive():
            self._recovery_thread = threading.Thread(
                target=self._recover_indexes, name="episode-index-recovery", daemon=True
            )
            self._recovery_thread.start()

    def _recover_indexes(self):
        while not self._scheduler_stop.is_set():
            try:
                key, root = self._recovery_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                with self._lock:
                    self._jobs[key]["index_recovery"] = {"status": "running"}
                result = ResultIndex(root).recover()
                state = {"status": "completed", "indexed": result["indexed"]}
            except Exception as exc:
                state = {
                    "status": "failed",
                    "error": str(exc),
                    "retry_after": time.time() + 30,
                }
            with self._lock:
                self._jobs[key]["index_recovery"] = state
                self._recovery_pending.discard(key)
                if state["status"] == "failed":
                    self._recovery_checks.pop(key, None)
                self._persist_jobs()
            self._recovery_queue.task_done()

    def _result_index(self, job):
        recovery = job.get("index_recovery", {})
        if recovery.get("status") in {"pending", "running"}:
            raise IndexRecoveryPending(
                "historical result index is being recovered; retry shortly"
            )
        if recovery.get("status") == "failed":
            raise RuntimeError(
                "historical result index recovery failed: "
                + recovery.get("error", "unknown error")
            )
        return ResultIndex(self._job_path(job, "status_path").parent)

    def _dispatch_waiting(self) -> None:
        # Caller holds _lock: concurrent submissions cannot both claim the slot.
        changed = False
        for job_id in self._active_job_keys():
            job = self._jobs[job_id]
            if self._job_is_running(job) or job_id in self._processes:
                previous = job.get("status")
                self._refresh_job(job_id)
                changed |= previous != self._jobs[job_id].get("status")
                process = self._processes.get(job_id)
                if process is not None and process.poll() is not None:
                    self._processes.pop(job_id, None)
        if changed:
            self._persist_jobs()
        for job_id in self._active_job_keys():
            job = self._jobs[job_id]
            process = self._processes.get(job_id)
            if (process is not None and process.poll() is None) or self._job_is_running(
                job
            ):
                return
        waiting = (
            self._jobs.waiting_keys()
            if isinstance(self._jobs, JobStore)
            else sorted(
                (
                    key
                    for key, job in self._jobs.items()
                    if job.get("status") == "waiting"
                ),
                key=lambda key: self._jobs[key].get("submitted_at", ""),
            )
        )
        for job_id in waiting:
            job = self._jobs[job_id]
            env = os.environ.copy()
            env["PYTHONPATH"] = (
                str(self.project_root) + os.pathsep + env.get("PYTHONPATH", "")
            )
            # The Portal tails this file while the coordinator is alive.  Force
            # Python streams to flush promptly even though stdout is redirected.
            env["PYTHONUNBUFFERED"] = "1"
            registry = self._job_path(job, "output_root") / ".processes"
            env["ROBOTENON_PROCESS_REGISTRY"] = str(registry)
            try:
                with self._job_path(job, "log_path").open("ab") as log_file:
                    process = subprocess.Popen(
                        job["command"],
                        cwd=str(self.project_root),
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
            except Exception as exc:
                job.update(
                    status="failed",
                    returncode=1,
                    ended_at=_now(),
                    error=f"failed to start experiment: {exc}",
                )
                self._persist_jobs()
                continue
            job.update(status="running", pid=process.pid, started_at=_now())
            self._processes[job["job_id"]] = process
            from ..runtime.processes import register_process

            job["process_identity"] = register_process(
                registry, process.pid, role="coordinator"
            )
            self._persist_jobs()
            break

    def _load_jobs(self):
        store = JobStore(self.output_root)
        # Older rerun implementations left terminal metadata on waiting jobs.
        # Repair only the bounded active/waiting set during startup so a legacy
        # jobs.json import becomes consistent without scanning all history.
        terminal_fields = (
            "cancel_reason",
            "cancelled_at",
            "ended_at",
            "termination",
            "resources_released",
            "stop_progress",
            "error",
            "returncode",
        )
        changed = False
        for job_id in store.active_keys(waiting=True):
            job = store[job_id]
            if job.get("status") != "waiting":
                continue
            for key in terminal_fields:
                if key in job:
                    job.pop(key)
                    changed = True
        if changed:
            store.flush()
        return store

    def _persist_jobs(self) -> None:
        if isinstance(self._jobs, JobStore):
            self._jobs.flush()

    @staticmethod
    def _job_is_running(job: Mapping[str, Any]) -> bool:
        """Return whether a job still represents a live experiment run."""

        status = str(job.get("status", "")).lower()
        if status in {"running", "stopping", "stop_failed"}:
            return True
        progress = job.get("progress")
        return (
            isinstance(progress, Mapping)
            and str(progress.get("status", "")).lower() == "running"
        )

    def _pid_is_experiment(
        self,
        pid: int,
        *,
        process: subprocess.Popen[Any] | None = None,
        job_id: str | None = None,
    ) -> bool:
        """Guard persisted-PID signalling against PID reuse.

        A manager may be restarted while an experiment continues running.  In
        that case there is no Popen object to identify the child, so inspect
        Linux' proc command line before sending a signal.  A live Popen object
        was created by this manager and is already trusted.
        """

        if process is not None:
            return process.poll() is None
        from ..runtime.processes import process_table, same_process

        record = self._jobs.get(job_id, {}).get("process_identity")
        if record:
            return same_process(record, process_table().get(pid))
        proc_cmdline = Path(f"/proc/{int(pid)}/cmdline")
        try:
            command = (
                proc_cmdline.read_bytes()
                .replace(b"\x00", b" ")
                .decode("utf-8", errors="replace")
            )
        except OSError:
            # Non-Linux hosts do not expose /proc.  The persisted job record
            # is still the best available identity there.
            return os.name != "posix"
        if not command.strip():
            return False
        if str(self.entrypoint) not in command:
            return False
        # A persisted PID can be reused by a later experiment.  The runner
        # command includes ``--run-id``; use it when available so an old job
        # cannot mistake a newer run for its own live process.
        return job_id is None or str(job_id) in command

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        if int(pid) <= 0:
            return False
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError as exc:
            return exc.errno != errno.ESRCH
        return True

    def _terminate_process_group(
        self,
        job_id,
        job,
        *,
        term_timeout_s=None,
    ):
        from ..runtime.processes import (
            register_process,
            remember_descendants,
            terminate_owned,
        )

        registry = self._job_path(job, "output_root") / ".processes"
        process = self._processes.get(job_id)
        pid = process.pid if process is not None else job.get("pid")
        if pid and self._pid_is_experiment(int(pid), process=process, job_id=job_id):
            register_process(registry, int(pid), role="coordinator")
        remember_descendants(registry)
        if term_timeout_s is None:
            term_timeout_s = 3.0
            try:
                loaded = self._load_job_config(job)
                from ..runtime.execution import ExecutionOptions

                execution = ExecutionOptions.from_params(loaded.params)
                term_timeout_s = (
                    execution.shutdown_timeout_s
                    + max(
                        (
                            t.deployment.shutdown_timeout_s
                            for t in loaded.tools.tools
                            if t.deployment
                        ),
                        default=0,
                    )
                    + 2
                )
            except (ValueError, OSError, AttributeError):
                pass
        self._set_stop_progress(
            job_id, "waiting_for_runner", grace_timeout_s=term_timeout_s
        )
        result = self._terminate_coordinator(job_id, job, term_timeout_s=term_timeout_s)
        self._set_stop_progress(job_id, "releasing_descendants")
        descendants = terminate_owned(registry)
        result["terminated"] = result["terminated"] and descendants["terminated"]
        result["forced"] = result["forced"] or descendants["forced"]
        result["remaining_pids"] = descendants["remaining_pids"]
        if not descendants["terminated"]:
            result["reason"] = "owned_processes_still_alive"
        return result

    def _terminate_coordinator(
        self,
        job_id: str,
        job: Mapping[str, Any],
        *,
        term_timeout_s: float = 3.0,
    ) -> dict[str, Any]:
        """Terminate one experiment's complete process group.

        ``submit`` starts every experiment with ``start_new_session=True``.
        Signalling its process group therefore releases the runner together
        with simulator/render/model client children, while the Portal process
        remains alive.  SIGKILL is only used after the graceful interval.
        """

        process = self._processes.get(job_id)
        raw_pid = process.pid if process is not None else job.get("pid")
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            return {
                "pid": None,
                "terminated": False,
                "forced": False,
                "reason": "missing_pid",
            }
        if (process is not None and process.poll() is not None) or not self._pid_exists(
            pid
        ):
            return {
                "pid": pid,
                "terminated": True,
                "forced": False,
                "reason": "already_exited",
            }
        if not self._pid_is_experiment(pid, process=process, job_id=job_id):
            return {
                "pid": pid,
                "terminated": False,
                "forced": False,
                "reason": "pid_identity_mismatch",
            }

        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return {
                "pid": pid,
                "terminated": True,
                "forced": False,
                "reason": "already_exited",
            }
        except OSError as exc:
            return {
                "pid": pid,
                "terminated": False,
                "forced": False,
                "reason": str(exc),
            }

        # Never allow a malformed/stale record to signal the Portal's own
        # process group.  This is especially important for a global shutdown.
        current_pgid = os.getpgrp()
        if pgid in {current_pgid, os.getpgid(os.getpid())}:
            return {
                "pid": pid,
                "terminated": False,
                "forced": False,
                "reason": "refusing_portal_process_group",
            }

        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return {
                "pid": pid,
                "terminated": True,
                "forced": False,
                "reason": "already_exited",
            }
        except OSError as exc:
            return {
                "pid": pid,
                "terminated": False,
                "forced": False,
                "reason": str(exc),
            }

        forced = False
        deadline = time.monotonic() + max(0.1, float(term_timeout_s))
        while time.monotonic() < deadline:
            if process is not None:
                if process.poll() is not None:
                    break
            elif not self._pid_exists(pid):
                break
            time.sleep(0.05)

        if process is not None and process.poll() is None:
            forced = True
        elif process is None and self._pid_exists(pid):
            forced = True
        if forced:
            self._set_stop_progress(job_id, "forced_cleanup")
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                # The process can exit between the liveness check and SIGKILL.
                pass
            if process is not None:
                try:
                    process.wait(timeout=1.0)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        elif process is not None:
            try:
                process.wait(timeout=0.2)
            except (subprocess.TimeoutExpired, OSError):
                pass
        still_alive = (
            process.poll() is None if process is not None else self._pid_exists(pid)
        )
        return {
            "pid": pid,
            "terminated": not still_alive,
            "forced": forced,
            "reason": "process_still_alive" if still_alive else "terminated",
        }

    def _write_cancelled_status(
        self, job: dict[str, Any], *, reason: str, ended_at: str
    ) -> None:
        """Persist a terminal cancellation status for the Portal and UI."""

        status_path = Path(str(job.get("status_path", "")))
        payload: dict[str, Any] = {}
        if status_path.is_file():
            try:
                value = json.loads(status_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    payload = value
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
        payload.update(
            {
                "run_id": payload.get("run_id", job.get("job_id")),
                "status": "cancelled",
                "cancel_reason": reason,
                "ended_at": ended_at,
            }
        )
        if status_path.parent:
            status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = status_path.with_name(f".{status_path.name}.cancelled.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(status_path)
        self._json_cache.pop(status_path, None)

    def _mark_cancelled(
        self,
        job: dict[str, Any],
        *,
        reason: str,
        termination: Mapping[str, Any],
    ) -> dict[str, Any]:
        ended_at = _now()
        job["status"] = "cancelled"
        job["cancel_reason"] = reason
        job["ended_at"] = ended_at
        job["termination"] = dict(termination)
        job["resources_released"] = bool(termination.get("terminated"))
        if termination.get("pid") is not None and termination.get("forced"):
            job["returncode"] = -int(signal.SIGKILL)
        elif termination.get("pid") is not None:
            job["returncode"] = -int(signal.SIGTERM)
        progress = job.setdefault("progress", {})
        if isinstance(progress, dict):
            progress["status"] = "cancelled"
            progress["ended_at"] = ended_at
        self._write_cancelled_status(job, reason=reason, ended_at=ended_at)
        return job

    def _mark_process_lost(self, job: dict[str, Any], *, reason: str) -> dict[str, Any]:
        """Finalize a job whose runner disappeared without writing a terminal state.

        A Portal restart drops the in-memory ``Popen`` object.  Persisted jobs
        therefore need an explicit liveness check; otherwise their last
        ``status.json`` snapshot can keep every active case in ``running``
        forever.  Keep the active case records in the progress payload, but
        convert them to failed records so the trajectory list remains honest.
        """

        ended_at = _now()
        job["status"] = "failed"
        job["returncode"] = 1
        job["ended_at"] = ended_at
        job["failure_reason"] = reason
        job["error"] = reason

        progress = job.get("progress")
        if isinstance(progress, dict):
            progress["status"] = "failed"
            progress["ended_at"] = ended_at
            progress["error"] = reason
            active = progress.get("active")
            if isinstance(active, list):
                for item in active:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("status", "running")).lower() == "running":
                        item["status"] = "failed"
                        item["error"] = reason
                        item["ended_at"] = ended_at
            current = progress.get("current")
            if (
                isinstance(current, dict)
                and str(current.get("status", "running")).lower() == "running"
            ):
                current["status"] = "failed"
                current["error"] = reason
                current["ended_at"] = ended_at

        raw_status_path = str(job.get("status_path") or "").strip()
        if raw_status_path:
            status_path = self._job_path(job, "status_path")
            payload: dict[str, Any] = {}
            if status_path.is_file():
                try:
                    value = json.loads(status_path.read_text(encoding="utf-8"))
                    if isinstance(value, dict):
                        payload = value
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    payload = {}
            payload.update(
                {
                    "run_id": payload.get("run_id", job.get("job_id")),
                    "status": "failed",
                    "error": reason,
                    "failure_reason": reason,
                    "ended_at": ended_at,
                    "returncode": 1,
                }
            )
            active = payload.get("active")
            if isinstance(active, list):
                for item in active:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("status", "running")).lower() == "running":
                        item["status"] = "failed"
                        item["error"] = reason
                        item["ended_at"] = ended_at
            current = payload.get("current")
            if (
                isinstance(current, dict)
                and str(current.get("status", "running")).lower() == "running"
            ):
                current["status"] = "failed"
                current["error"] = reason
                current["ended_at"] = ended_at
            status_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = status_path.with_name(f".{status_path.name}.lost.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            temporary.replace(status_path)
            self._json_cache.pop(status_path, None)
        return job

    def _resolve_under_project(self, value: str | Path) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = self.project_root / path
        return path.resolve()

    def _read_json_cached(self, path: Path) -> Any:
        stat = path.stat()
        if stat.st_size > 16 * 1024 * 1024:
            raise QueryBudgetExceeded(
                "legacy JSON exceeds 16 MiB; build its episode index before querying"
            )
        signature = (stat.st_mtime_ns, stat.st_size)
        cached = self._json_cache.get(path)
        if cached is not None and cached[0] == signature:
            return cached[1]
        value = json.loads(path.read_text(encoding="utf-8"))
        # Parsed JSON has greater overhead than its on-disk representation.
        self._json_cache.put(path, (signature, value), size=max(256, stat.st_size * 4))
        return value

    def _relative_config(self, path: Path) -> str:
        return path.relative_to(self.config_root).as_posix()

    def _config_path(self, config: str | Path) -> Path:
        candidate = Path(config)
        if candidate.is_absolute():
            path = candidate.resolve()
        else:
            path = (self.config_root / candidate).resolve()
        try:
            path.relative_to(self.config_root)
        except ValueError as exc:
            raise ValueError(
                "config must be located under the configured config root"
            ) from exc
        if path.suffix.lower() != ".json":
            raise ValueError("only JSON configs can be submitted")
        if not path.is_file():
            raise FileNotFoundError(f"config not found: {config}")
        return path

    def _describe(self, path: Path) -> dict[str, Any]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config root must be an object")
        loaded = load_application_config(path)
        return {
            "id": self._relative_config(path),
            "path": str(path),
            "label": self._relative_config(path),
            "valid": True,
            "config_version": raw.get("config_version", 1),
            "task_adapters": [
                {
                    "id": item.id or item.name,
                    "name": item.name,
                    "kind": item.kind,
                    "enabled": item.enabled,
                }
                for item in loaded.task_adapters.task_adapters
                if item.enabled
            ],
        }

    def list_configs(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self.config_root.exists():
            return records
        for path in sorted(self.config_root.rglob("*")):
            if not path.is_file() or path.suffix.lower() != ".json":
                continue
            if not path.resolve().is_relative_to(self.config_root):
                continue
            try:
                records.append(self._describe(path))
            except Exception as exc:
                # Keep every JSON visible even if application validation fails.
                records.append(
                    {
                        "id": self._relative_config(path),
                        "path": str(path),
                        "label": self._relative_config(path),
                        "valid": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "task_adapters": [],
                    }
                )
        return records

    def get_config_document(self, config: str | Path) -> dict[str, Any]:
        """Read any JSON config; application validation belongs to submission."""

        path = self._config_path(config)
        return {
            "id": self._relative_config(path),
            "label": self._relative_config(path),
            "json": json.loads(path.read_text(encoding="utf-8")),
        }

    def _refresh_job(self, job_id: str) -> dict[str, Any]:
        job = self._jobs[job_id]
        if job.get("status") in {"waiting", "stopping", "stop_failed"}:
            return dict(job)
        process = self._processes.get(job_id)
        status_path = self._job_path(job, "status_path")
        if status_path.exists():
            try:
                status = self._read_json_cached(status_path)
                if not isinstance(status, Mapping):
                    status = {}
                status_progress = (
                    status.get("progress") if isinstance(status, Mapping) else None
                )
                if not isinstance(status_progress, Mapping):
                    status_progress = status
                job["progress"] = {
                    key: status_progress.get(key)
                    for key in (
                        "status",
                        "completed_episodes",
                        "total_episodes",
                        "current",
                        "active",
                        "last",
                        "adapter_order",
                        "started_at",
                        "ended_at",
                    )
                }
                if isinstance(status.get("summary"), dict):
                    job["summary"] = status["summary"]
                elif isinstance(status_progress.get("summary"), dict):
                    job["summary"] = status_progress["summary"]
                persisted_status = str(status_progress.get("status") or "").lower()
                if persisted_status in {"running", "starting"}:
                    # A batch can already contain failed episode attempts while
                    # later cases are still executing.  Keep the experiment
                    # itself live until the runner writes a terminal status or
                    # exits; the summary's num_errors is not a run-level
                    # terminal signal.
                    if job.get("status") != "cancelled":
                        job["status"] = "running"
                elif persisted_status in {"completed", "failed", "cancelled"}:
                    job["status"] = persisted_status
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        summary_path = self._job_path(job, "summary_path")
        indexed = ResultIndex(status_path.parent).path.is_file()
        if summary_path.exists() and not indexed:
            try:
                summary = self._read_json_cached(summary_path)
                if isinstance(summary, dict):
                    job["summary"] = self._project_recovered_summary(job, summary)
            except QueryBudgetExceeded as exc:
                # Keep the job visible and queue index recovery below. Legacy
                # summaries can be too large for a request-path JSON read.
                job["summary_error"] = str(exc)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        summary = job.get("summary")
        if (
            not self._job_is_running(job)
            and job.get("status") != "cancelled"
            and isinstance(summary, Mapping)
        ):
            try:
                summary_errors = int(summary.get("num_errors", 0) or 0)
            except (TypeError, ValueError):
                summary_errors = 0
            if summary_errors > 0:
                job["status"] = "failed"
                if job.get("returncode") is None:
                    job["returncode"] = 1
                progress = job.get("progress")
                if isinstance(progress, dict):
                    progress["status"] = "failed"

        # After a Portal restart there is no in-memory Popen object. A
        # persisted running record is only trustworthy while its PID still
        # belongs to this exact experiment runner. Otherwise finalize it so
        # stale active snapshots cannot remain visible forever.
        if process is None and self._job_is_running(job):
            raw_pid = job.get("pid")
            if raw_pid not in (None, ""):
                try:
                    persisted_pid = int(raw_pid)
                except (TypeError, ValueError):
                    persisted_pid = 0
                if not self._pid_is_experiment(persisted_pid, job_id=job_id):
                    reason = (
                        "experiment process exited before writing a terminal status"
                    )
                    updated = self._mark_process_lost(job, reason=reason)
                    self._jobs[job_id] = updated
                    self._persist_jobs()
                    job = updated

        return_code = process.poll() if process is not None else job.get("returncode")
        if return_code is not None:
            job["returncode"] = return_code
            if return_code != 0 and job["status"] != "cancelled":
                job["status"] = "failed"
            elif job["status"] not in {"completed", "cancelled"}:
                job["status"] = "completed"
            job.setdefault("ended_at", _now())
        if "parameters" not in job and job.get("config_path"):
            try:
                loaded = self._load_job_config(job)
                job["parameters"] = loaded.params.values
                job["parameter_hash"] = loaded.params.content_hash
            except Exception as exc:
                # Keep historical jobs visible even if their original config
                # was moved or a child manifest is no longer available.
                job["parameters_error"] = f"{type(exc).__name__}: {exc}"
        if job.get("status") in {"completed", "failed", "cancelled"} and not job.get(
            "resources_released"
        ):
            from ..runtime.processes import owned_processes

            remaining = owned_processes(
                self._job_path(job, "output_root") / ".processes"
            )
            if remaining:
                if job.get("pid") in remaining:
                    job["status"] = "running"
                else:
                    job.update(
                        status="stop_failed",
                        error="experiment exited with owned processes still alive",
                        termination={
                            "terminated": False,
                            "remaining_pids": sorted(remaining),
                        },
                    )
            elif (
                (process is not None and process.poll() is not None)
                or not job.get("pid")
                or not self._pid_exists(int(job["pid"]))
            ):
                job["resources_released"] = True
        if job.get("status") == "running":
            progress = job.get("progress") or {}
            job["runtime_phase"] = "loading"
            if progress.get("status") in {"completed", "failed", "cancelled"}:
                job["runtime_phase"] = "cleanup"
            else:
                active = progress.get("active") or (
                    [progress["current"]] if progress.get("current") else []
                )
                for item in active[:128]:
                    live_path = self._safe_job_artifact(
                        job, item.get("live_status_path")
                    )
                    if live_path and live_path.is_file():
                        try:
                            live = self._read_json_cached(live_path)
                            if live.get("status") == "running":
                                job["runtime_phase"] = "executing"
                                break
                        except (OSError, ValueError):
                            pass
        self._queue_index_recovery(job)
        result = dict(job)
        if isinstance(job.get("summary"), dict):
            job["summary"] = self._compact_summary(job["summary"])
        return result

    def _job_path(self, job: Mapping[str, Any], key: str) -> Path:
        """Resolve a persisted job file path against the project root."""

        raw = Path(str(job.get(key, "")))
        if not raw.is_absolute():
            raw = self.project_root / raw
        return raw.resolve()

    def _load_job_config(self, job: dict[str, Any]):
        if job.get("config_snapshot_path"):
            return load_config_snapshot(self._job_path(job, "config_snapshot_path"))
        config_path = job.get("config_path")
        if not config_path:
            raise ValueError("job does not contain config_path")
        override_path = job.get("experiment_config_path")
        experiment_config = (
            override_path if override_path and Path(override_path).is_file() else None
        )
        cache_key = (
            str(Path(config_path).resolve()),
            str(Path(experiment_config).resolve()) if experiment_config else "",
        )
        loaded = self._config_cache.get(cache_key)
        if loaded is None:
            loaded = load_application_config(config_path, experiment_config)
            self._config_cache[cache_key] = loaded
        return loaded

    def list_episodes(
        self, job_id: str, *, include_runtime: bool = True
    ) -> list[dict[str, Any]]:
        """Return the persisted case records belonging to one experiment.

        ``graph_runtime`` can be expensive to derive for a large batch.  The
        trajectory browser only needs it after a case is selected, so callers
        may request the compact case index without deriving runtime state.
        The default stays enabled for the Python API and existing consumers.
        """

        with self._lock:
            job = self._refresh_job(job_id) if job_id in self._jobs else None
        if job is None:
            return []
        index = self._result_index(job)
        if index.path.is_file():
            episodes, offset = [], 0
            while True:
                page = self.episodes_page(
                    job_id, limit=500, offset=offset, include_runtime=include_runtime
                )
                episodes.extend(page["episodes"])
                if page["next_offset"] is None:
                    return episodes
                offset = page["next_offset"]
        summary = job.get("summary")
        episodes: list[dict[str, Any]] = []
        if isinstance(summary, dict):
            summary_items = self._iter_summary_episodes(summary)
        else:
            summary_items = []
        for adapter_id, episode in summary_items:
            # Derive graph state before dropping the heavy context. The
            # full observation/action payload remains in the episode
            # artifact on disk and is not needed by the trajectory table.
            full_record = dict(episode)
            full_record.setdefault("adapter_id", adapter_id)
            full_record.setdefault(
                "global_index", self._episode_index(full_record, len(episodes))
            )
            record = self._compact_episode(full_record)
            if include_runtime:
                graph_runtime = full_record.get("graph_runtime")
                if not isinstance(graph_runtime, dict):
                    graph_runtime = self._episode_graph_runtime(full_record)
                record["graph_runtime"] = graph_runtime
            else:
                record.pop("graph_runtime", None)
            episodes.append(record)

        # The evaluator publishes the currently executing case in status.json
        # before entering the backend.  Include it in the same compact index so
        # the UI can select a running trajectory before summary.json exists.
        progress = job.get("progress")
        live_items: list[Mapping[str, Any]] = []
        if isinstance(progress, Mapping):
            active = progress.get("active")
            if isinstance(active, list):
                live_items.extend(item for item in active if isinstance(item, Mapping))
            current = progress.get("current")
            if isinstance(current, Mapping):
                live_items.append(current)
        known = {
            (str(item.get("adapter_id", "")), self._episode_index(item, index))
            for index, item in enumerate(episodes)
        }
        for item in live_items:
            adapter_id = str(item.get("adapter_id") or item.get("adapter") or "")
            if not adapter_id:
                continue
            global_index = self._episode_index(item, len(episodes))
            if (adapter_id, global_index) in known:
                episodes = [
                    episode
                    for episode in episodes
                    if (
                        str(episode.get("adapter_id", "")),
                        self._episode_index(episode, -1),
                    )
                    != (adapter_id, global_index)
                ]
            live_record = dict(item)
            live_record["adapter_id"] = adapter_id
            live_record["global_index"] = global_index
            live_status = str(live_record.get("status") or "running").lower()
            live_record["status"] = live_status
            live_record = self._merge_live_record(job, live_record)
            record = self._compact_episode(live_record)
            record["status"] = str(live_record.get("status") or "running").lower()
            if include_runtime:
                graph_runtime = live_record.get("graph_runtime")
                if not isinstance(graph_runtime, dict):
                    graph_runtime = self._episode_graph_runtime(live_record)
                record["graph_runtime"] = graph_runtime
            else:
                record.pop("graph_runtime", None)
            episodes.append(record)
            known.add((adapter_id, global_index))
        episodes.sort(
            key=lambda item: (item.get("global_index", 0), item.get("adapter_id", ""))
        )
        return episodes

    @staticmethod
    def _value_at(value: Any, *paths: str, default: Any = None) -> Any:
        """Read the first existing value from several compatible JSON paths."""

        for path in paths:
            if isinstance(value, Mapping) and path in value and value[path] is not None:
                return value[path]
            current = value
            matched = True
            for part in path.split("."):
                if not isinstance(current, Mapping) or part not in current:
                    matched = False
                    break
                current = current[part]
            if matched and current is not None:
                return current
        return default

    @classmethod
    def _episode_values(cls, value: Any) -> list[dict[str, Any]]:
        """Extract episode records from list, keyed-map, or single-record forms."""

        collection_keys = (
            "episodes",
            "cases",
            "trajectories",
            "items",
            "records",
            "results",
            "episode_results",
        )
        strong_episode_keys = {
            "task_id",
            "task",
            "task_name",
            "task_description",
            "episode_id",
            "trajectory_id",
            "episode_json",
            "global_index",
            "case_index",
            "trajectory_index",
            "episode_index",
            "episode_no",
            "result",
            "output",
            "context",
            "runtime",
            "observations",
            "observation",
            "frames",
            "task_success",
        }
        if isinstance(value, (list, tuple)):
            return [dict(item) for item in value if isinstance(item, Mapping)]
        if not isinstance(value, Mapping):
            return []
        for key in collection_keys:
            if key in value:
                nested = cls._episode_values(value[key])
                if nested:
                    return nested
        if strong_episode_keys.intersection(value):
            return [dict(value)]
        # Some producers use {"0": {...}, "1": {...}} instead of an array.
        nested_records = [item for item in value.values() if isinstance(item, Mapping)]
        if nested_records and all(str(key).isdigit() for key in value):
            return [dict(item) for item in nested_records]
        return []

    @classmethod
    def _iter_summary_episodes(
        cls, summary: Any
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yield cases from the common adapter/episode aggregate variants."""

        if not isinstance(summary, Mapping):
            return
        # Accept both the native summary and wrappers such as
        # {"data": {"adapters": ...}} produced by external runners.
        roots: list[Mapping[str, Any]] = [summary]
        for key in ("data", "result", "output"):
            nested = summary.get(key)
            if isinstance(nested, Mapping):
                roots.append(nested)
        for root in roots:
            containers = root.get("task_adapters", root.get("adapters"))
            yielded = False
            if isinstance(containers, Mapping):
                for adapter_id, stats in containers.items():
                    for episode in cls._episode_values(stats):
                        yielded = True
                        record = dict(episode)
                        record.setdefault("adapter_id", str(adapter_id))
                        yield str(record.get("adapter_id") or adapter_id), record
            elif isinstance(containers, list):
                for item in containers:
                    if not isinstance(item, Mapping):
                        continue
                    values = cls._episode_values(item)
                    default_adapter = str(
                        item.get("adapter_id")
                        or item.get("adapter_name")
                        or item.get("adapter")
                        or item.get("name")
                        or item.get("id")
                        or "adapter"
                    )
                    for episode in values:
                        yielded = True
                        record = dict(episode)
                        adapter_id = str(
                            record.get("adapter_id")
                            or record.get("adapter_name")
                            or record.get("adapter")
                            or default_adapter
                        )
                        record.setdefault("adapter_id", adapter_id)
                        yield adapter_id, record
            if yielded:
                return
            for key in (
                "episodes",
                "cases",
                "trajectories",
                "items",
                "records",
                "results",
            ):
                values = cls._episode_values(root.get(key))
                if not values:
                    continue
                for episode in values:
                    record = dict(episode)
                    adapter_id = str(
                        record.get("adapter_id")
                        or record.get("adapter_name")
                        or record.get("adapter")
                        or "default"
                    )
                    record.setdefault("adapter_id", adapter_id)
                    yield adapter_id, record
                return

    @classmethod
    def _episode_index(cls, record: Mapping[str, Any], fallback: int) -> int:
        value = cls._value_at(
            record,
            "global_index",
            "index",
            "case_index",
            "trajectory_index",
            "episode_index",
            "episode_no",
            default=fallback,
        )
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(fallback)

    def _episode_record(
        self,
        job_id: str,
        *,
        adapter_id: str,
        global_index: int,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Find one case and load its full artifact when it is available."""

        with self._lock:
            if job_id not in self._jobs:
                return None
            job = self._refresh_job(job_id)
        summary = job.get("summary")
        index = self._result_index(job)
        candidate: dict[str, Any] | None = (
            index.get(adapter_id, global_index) if index.path.is_file() else None
        )
        for sequence, (item_adapter_id, item) in enumerate(
            self._iter_summary_episodes(summary) if candidate is None else []
        ):
            if item_adapter_id != adapter_id:
                continue
            if self._episode_index(item, sequence) == int(global_index):
                candidate = dict(item)
                break
        progress = job.get("progress")
        running_items: list[Mapping[str, Any]] = []
        if isinstance(progress, Mapping):
            active = progress.get("active")
            if isinstance(active, list):
                running_items.extend(
                    item for item in active if isinstance(item, Mapping)
                )
            current = progress.get("current")
            if isinstance(current, Mapping):
                running_items.append(current)
        if candidate is None:
            for current in running_items:
                current_adapter = str(current.get("adapter_id", ""))
                current_index = current.get("global_index", current.get("index"))
                if (
                    current_adapter == adapter_id
                    and current_index is not None
                    and int(current_index) == int(global_index)
                ):
                    candidate = dict(current)
                    candidate["status"] = str(
                        candidate.get("status") or "running"
                    ).lower()
                    candidate.setdefault("global_index", int(global_index))
                    break
        if candidate is None:
            return None

        full = dict(candidate)
        episode_path = candidate.get("episode_json")
        safe_episode_path = self._safe_job_artifact(job, episode_path)
        if safe_episode_path is not None and safe_episode_path.is_file():
            try:
                value = self._read_json_cached(safe_episode_path)
                if isinstance(value, dict):
                    full = value
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        full.setdefault("adapter_id", adapter_id)
        full.setdefault("global_index", int(global_index))
        full.setdefault("status", candidate.get("status", "unknown"))
        if safe_episode_path is not None:
            full.setdefault("episode_json", str(safe_episode_path))
        if str(full.get("status", "")).lower() == "running":
            full = self._merge_live_record(job, full)
        elif str(full.get("status", "")).lower() in {"error", "failed", "cancelled"}:
            full = self._recover_failed_episode_media(job, full)
        full = self._project_recovered_episode(job, full)
        return job, full

    def _result_recovery_bindings(self, job):
        path = self._job_path(job, "summary_path").parent / "result_recovery.json"
        return self._read_json_cached(path).get("graphs", {}) if path.is_file() else {}

    def _project_recovered_episode(self, job, record):
        from ..runtime.result_projection import project_saved_episode

        return project_saved_episode(record, self._result_recovery_bindings(job))

    def _project_recovered_summary(self, job, summary):
        from ..runtime.result_projection import project_saved_summary

        graphs = self._result_recovery_bindings(job)
        if not graphs:
            return summary

        def load_record(record):
            path = self._safe_job_artifact(job, record.get("episode_json"))
            if path is not None and path.is_file():
                value = self._read_json_cached(path)
                # A rerun may already have replaced this generation on disk.
                if value.get("generation_started_at_ns") == record.get(
                    "generation_started_at_ns"
                ):
                    return value
            return record

        return project_saved_summary(summary, graphs, load_record)

    def _recover_failed_episode_media(self, job, record):
        """Recover only the latest attempt's artifacts after an abrupt exit."""
        full = dict(record)
        result = full.get("result") or {}
        if result.get("context"):
            return full
        episode = self._safe_job_artifact(job, full.get("episode_json"))
        if episode is None:
            return full
        attempts = []
        for candidate in episode.with_suffix("").glob("attempts/*/status.json"):
            path = self._safe_job_artifact(job, candidate)
            if path is None:
                continue
            try:
                state = self._read_json_cached(path)
                identity = state.get("identity", {})
                if identity.get("run_id") != job.get("job_id"):
                    continue
                modified = path.stat().st_mtime_ns
                if modified < (full.get("generation_started_at_ns") or 0):
                    continue
                attempts.append(
                    (modified, int(state.get("retry", 0)), path.parent, identity)
                )
            except (OSError, ValueError, TypeError):
                continue
        if not attempts:
            return full
        _, _, directory, identity = max(attempts, key=lambda item: item[:2])
        full["identity"] = identity
        video = self._safe_job_artifact(job, directory / "video.mp4")
        if video is not None and video.is_file():
            full["video_path"] = str(video)
        live_path = self._safe_job_artifact(
            job, episode.parent / ".live" / episode.name
        )
        if live_path is not None and live_path.is_file():
            try:
                live = self._read_json_cached(live_path)
                if live.get("identity", {}).get("attempt_id") == identity.get(
                    "attempt_id"
                ):
                    full["live_status_path"] = str(live_path)
                    full = self._merge_live_record(job, full)
                    # Preserve the terminal failure while retaining its last
                    # observations and execution history for inspection.
                    if isinstance(full.get("context"), Mapping):
                        full["context"] = dict(
                            full["context"],
                            status=full["status"],
                            error=full.get("error"),
                        )
            except (OSError, ValueError, TypeError):
                pass
        return full

    def _safe_job_artifact(self, job: dict[str, Any], value: Any) -> Path | None:
        """Resolve an artifact only when it remains inside this job output."""

        if not value:
            return None
        job_root = Path(str(job.get("output_root", "")))
        if not job_root.is_absolute():
            job_root = self.project_root / job_root
        job_root = job_root.resolve()
        if not str(job_root) or not job_root.exists():
            return None
        text = str(value)
        if "://" in text:
            return None
        raw_path = Path(text)
        candidates = (
            [raw_path]
            if raw_path.is_absolute()
            else [self.project_root / raw_path, job_root / raw_path]
        )
        for path in candidates:
            try:
                resolved = path.resolve()
                resolved.relative_to(job_root)
            except (OSError, ValueError):
                continue
            return resolved
        return None

    def _register_observation_artifact(self, job_id: str, path: Path) -> str:
        """Register a validated artifact and return a compact URL token."""

        token = hashlib.sha256(f"{job_id}\x00{path}".encode("utf-8")).hexdigest()[:24]
        with self._lock:
            self._observation_artifacts[token] = (str(job_id), path)
        return token

    def _resolve_observation_artifact(self, job_id: str, token: str) -> Path | None:
        """Resolve a short artifact token, falling back safely after restart."""

        if not token:
            return None
        with self._lock:
            value = self._observation_artifacts.get(str(token))
        if value is None or value[0] != str(job_id):
            return None
        path = value[1]
        return path if path.is_file() else None

    def _merge_live_record(
        self,
        job: Mapping[str, Any],
        record: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Overlay the atomic running-case snapshot onto a case record."""

        merged = dict(record)
        live_value = merged.get("live_status_path")
        live_path = self._safe_job_artifact(dict(job), live_value)
        if live_path is None or not live_path.is_file():
            return merged
        if live_path.stat().st_mtime_ns < (record.get("generation_started_at_ns") or 0):
            return merged
        try:
            live = self._read_json_cached(live_path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return merged
        if not isinstance(live, Mapping):
            return merged
        merged["live"] = dict(live)
        for key in ("identity", "session_id", "feedback", "context", "graph_runtime"):
            value = live.get(key)
            if value is not None:
                if key == "graph_runtime" and isinstance(value, Mapping):
                    # The runner's live header only carries the active node;
                    # derive the full sequence from the live context history.
                    if not ("sequence" in value or "nodes" in value):
                        continue
                merged[key] = value
        timeline_spans = live.get("timeline_spans")
        if isinstance(timeline_spans, list):
            merged["live_timeline_spans"] = timeline_spans
        return merged

    def _observation_artifact_path(
        self,
        job: Mapping[str, Any],
        full: Mapping[str, Any],
        frame: Mapping[str, Any],
    ) -> Path | None:
        """Resolve a frame URI to an artifact stored inside its job output."""

        direct = self._safe_job_artifact(dict(job), frame.get("storage_path"))
        if direct is not None and direct.is_file():
            return direct
        uri = str(frame.get("uri") or "")
        if not uri.startswith("cache://"):
            return None
        parsed = urlsplit(uri)
        session = unquote(parsed.netloc)
        parts = [unquote(item) for item in parsed.path.split("/") if item]
        if not session or len(parts) < 2:
            return None
        epoch = parts[0]
        channel = Path(parts[-1]).name
        if not channel or channel in {".", ".."}:
            return None
        sequence = frame.get("sequence")
        try:
            sequence_text = str(int(sequence))
        except (TypeError, ValueError):
            sequence_text = "0"
        episode_path = self._safe_job_artifact(dict(job), full.get("episode_json"))
        roots: list[Path] = []
        if episode_path is not None:
            roots.append(episode_path.parent)
            episode_dir = episode_path.parent / episode_path.stem
            roots.append(episode_dir)
            attempts_dir = episode_dir / "attempts"
            identity = (
                (full.get("result") or {}).get("identity") or full.get("identity") or {}
            )
            attempt_id = identity.get("attempt_id")
            if attempt_id:
                roots.append(attempts_dir / str(attempt_id))
            elif attempts_dir.is_dir() and not full.get("generation_started_at_ns"):
                roots.extend(path for path in attempts_dir.iterdir() if path.is_dir())
        output_root = job.get("output_root")
        if output_root:
            roots.append(Path(str(output_root)).resolve())
        for root in roots:
            observations = root / "resources" / "observations"
            candidates = [
                observations / session / epoch / sequence_text / f"{channel}.npy",
                observations / session / epoch / sequence_text / channel,
            ]
            for candidate in candidates:
                path = self._safe_job_artifact(dict(job), candidate)
                if path is not None and path.is_file():
                    return path
            # Older writers omit the sequence in the feedback metadata. Search
            # only the URI-derived session/epoch/channel subtree.
            search_root = observations / session / epoch
            for candidate in search_root.glob(f"*/{channel}.npy"):
                path = self._safe_job_artifact(dict(job), candidate)
                if path is not None and path.is_file():
                    return path
        return None

    def _attach_observation_frame_url(
        self,
        job: Mapping[str, Any],
        full: Mapping[str, Any],
        frame: dict[str, Any],
        *,
        job_id: str,
        adapter_id: str,
        global_index: int,
    ) -> dict[str, Any]:
        """Attach a Portal URL to a frame backed by this episode's artifact."""

        storage_path = self._observation_artifact_path(job, full, frame)
        if storage_path is not None:
            frame["storage_path"] = str(storage_path)
            artifact_id = self._register_observation_artifact(job_id, storage_path)
            frame["artifact_id"] = artifact_id
            frame_uri = frame.get("uri")
            if frame_uri:
                frame["url"] = (
                    f"/api/experiments/{quote(str(job_id), safe='')}/observation"
                    f"?adapter_id={quote(str(adapter_id), safe='')}&global_index={int(global_index)}"
                    f"&uri={quote(str(frame_uri), safe='')}"
                    f"&artifact_id={quote(artifact_id, safe='')}&format=jpeg"
                )
        return frame

    @classmethod
    def _feedback_frames(cls, *sources: Any) -> list[dict[str, Any]]:
        """Normalize channels/frames/images from several adapter payload shapes."""

        candidates: list[Mapping[str, Any]] = []
        frame_keys = (
            "observation",
            "observations",
            "frames",
            "channels",
            "images",
            "camera_frames",
            "image",
            "rgb",
            "video_frames",
        )
        artifact_keys = {
            "episode_json",
            "trace_dir",
            "live_status_path",
            "graph_runtime",
            "live",
            "output_path",
            "video_path",
            "trace_sqlite",
            "trace_jsonl",
            "metrics_dir",
            "metrics_wal",
            "memory_path",
            "config",
        }

        def collect(value: Any, hint: str | None = None) -> None:
            if isinstance(value, Mapping):
                uri = cls._value_at(
                    value,
                    "uri",
                    "ref",
                    "observation_uri",
                    "url",
                    "image_url",
                    "artifact_ref.uri",
                )
                if uri:
                    item = dict(value)
                    if hint and not any(
                        item.get(key)
                        for key in (
                            "name",
                            "frame_name",
                            "frame_id",
                            "camera_id",
                            "camera",
                        )
                    ):
                        item["name"] = hint
                    candidates.append(item)
                    return
                found_nested = False
                for key in frame_keys:
                    if key in value:
                        found_nested = True
                        collect(value[key], hint)
                if not found_nested:
                    # Mapping forms such as {"front": {"uri": ...}} or
                    # {"front": "cache://..."} are also common.
                    potential = [
                        (name, item)
                        for name, item in value.items()
                        if str(name) not in artifact_keys
                        if isinstance(item, (Mapping, list, tuple))
                        or (
                            isinstance(item, str)
                            and ("://" in item or item.startswith("/"))
                        )
                    ]
                    for name, item in potential:
                        collect(item, str(name))
            elif isinstance(value, (list, tuple)):
                for item in value:
                    collect(item, hint)
            elif (
                isinstance(value, str)
                and value
                and (
                    "://" in value
                    or value.startswith("/")
                    or value.lower().endswith(
                        (".png", ".jpg", ".jpeg", ".webp", ".npy")
                    )
                )
            ):
                candidates.append({"name": hint or "frame", "uri": value})

        for source in sources:
            collect(source)
        frames: list[dict[str, Any]] = []
        seen: dict[str, int] = {}
        for channel in candidates:
            artifact = (
                channel.get("artifact_ref")
                if isinstance(channel.get("artifact_ref"), Mapping)
                else {}
            )
            uri = cls._value_at(
                channel,
                "uri",
                "ref",
                "observation_uri",
                "url",
                "image_url",
                default=artifact.get("uri"),
            )
            if not uri:
                continue
            name = str(
                cls._value_at(
                    channel,
                    "name",
                    "frame_name",
                    "frame_id",
                    "camera_id",
                    "camera",
                    default="frame",
                )
            )
            key = f"{name}\u0000{uri}"
            frame = {
                "name": name,
                "frame_id": channel.get("frame_id"),
                "uri": str(uri),
                "modality": channel.get("modality", artifact.get("modality")),
                "encoding": channel.get("encoding", artifact.get("encoding")),
                "shape": channel.get("shape", artifact.get("shape")),
                "camera_id": channel.get("camera_id", artifact.get("camera_id")),
                "capture_group_id": channel.get(
                    "capture_group_id", artifact.get("capture_group_id")
                ),
                "sequence": channel.get("sequence", artifact.get("sequence")),
                "timestamp": channel.get("timestamp", artifact.get("timestamp")),
                "metadata": channel.get("metadata") or artifact.get("metadata") or {},
                "storage_path": channel.get("storage_path")
                or artifact.get("storage_path"),
                "url": channel.get("url") or channel.get("image_url"),
            }
            existing_index = seen.get(key)
            if existing_index is not None:
                existing = frames[existing_index]
                for field, value in frame.items():
                    if field in {"name", "uri"}:
                        continue
                    if existing.get(field) in (None, "", [], {}) and value not in (
                        None,
                        "",
                        [],
                        {},
                    ):
                        existing[field] = value
                continue
            seen[key] = len(frames)
            frames.append(frame)
        return frames

    @staticmethod
    def _recent_frames(
        frames: Any,
        *,
        limit: int = 16,
        visual_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Keep the latest frame for each channel instead of every timestep.

        A trajectory can contain hundreds of observations for the same camera.
        The browser needs the current view, not a second copy of the complete
        sensor history; the full frames remain in the persisted episode.
        """

        if not isinstance(frames, (list, tuple)):
            return []
        recent: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for frame in reversed(frames):
            if not isinstance(frame, Mapping):
                continue
            name = str(frame.get("name") or frame.get("frame_id") or "frame")
            if visual_only:
                modality = str(frame.get("modality") or "").lower()
                visual_name = name.lower()
                is_visual = (
                    modality in {"rgb", "image", "video", "depth"}
                    or visual_name.startswith(("video.", "image.", "camera"))
                    or "image" in visual_name
                    or "camera" in visual_name
                )
                if not is_visual:
                    continue
            if name in seen_names:
                continue
            seen_names.add(name)
            recent.append(dict(frame))
            if len(recent) >= limit:
                break
        recent.reverse()
        return recent

    @staticmethod
    def _frame_sequence(
        frames: Any,
        *,
        limit: int = 32,
        visual_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Keep the ordered images from one observation event.

        ``_recent_frames`` intentionally keeps only one latest frame per
        channel for compact trajectory summaries.  A single ``observe`` can
        legitimately return several images for the same camera, however (for
        example a short capture burst).  The trajectory detail API needs that
        ordered sequence so the portal can page through it without dropping
        frames merely because their camera name is identical.
        """

        if not isinstance(frames, (list, tuple)):
            return []
        sequence: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for frame in frames:
            if not isinstance(frame, Mapping):
                continue
            if visual_only:
                modality = str(frame.get("modality") or "").lower()
                visual_name = str(
                    frame.get("name") or frame.get("frame_id") or ""
                ).lower()
                is_visual = (
                    modality in {"rgb", "image", "video", "depth"}
                    or visual_name.startswith(("video.", "image.", "camera"))
                    or "image" in visual_name
                    or "camera" in visual_name
                )
                if not is_visual:
                    continue
            uri = str(frame.get("uri") or frame.get("url") or "")
            name = str(frame.get("name") or frame.get("frame_id") or "frame")
            key = (name, uri)
            if key in seen:
                continue
            seen.add(key)
            sequence.append(dict(frame))
            if len(sequence) >= limit:
                break
        return sequence

    @classmethod
    def _video_value(cls, full: Mapping[str, Any], result: Mapping[str, Any]) -> Any:
        """Read a video artifact from the common result/media layouts."""

        value = cls._value_at(
            result,
            "video_path",
            "video_url",
            "video",
            "artifacts.video",
            "media.video",
            default=cls._value_at(
                full,
                "video_path",
                "video_url",
                "video",
                "artifacts.video",
                "media.video",
                default="",
            ),
        )
        if isinstance(value, Mapping):
            return cls._value_at(
                value, "path", "file", "url", "uri", "href", default=""
            )
        return value

    @staticmethod
    def _trace_category(
        name: str, attributes: Mapping[str, Any]
    ) -> tuple[str, str, str | None]:
        """Map a trace span to the four visual timeline lanes."""

        kind = str(attributes.get("kind") or "").lower()
        operation = str(attributes.get("operation") or "").lower()
        node_type = str(attributes.get("node_type") or "").lower()
        node = str(attributes.get("node") or "")
        node_lower = node.lower()
        if kind == "tool" or name.startswith("tool:"):
            label = str(attributes.get("tool") or name.partition(":")[2] or name)
            return "tool", label, None
        if (
            kind == "monitor"
            or name.startswith("monitor:")
            or operation.startswith("monitor.")
            or node_lower.startswith("monitor")
        ):
            label = str(
                attributes.get("monitor")
                or operation
                or node
                or name.partition(":")[2]
                or name
            )
            return "monitor", label, node or None
        if (
            operation.startswith(("env.", "body."))
            or node_type == "system"
            or node_lower in {"reset", "observe", "body_execute"}
        ):
            label = str(operation or node or name.rsplit(":", 1)[-1] or name)
            return "envs", label, node or None
        if kind in {"node", "module", "graph"} or name.startswith("node:"):
            node = node or name.rsplit(":", 1)[-1] or name
            return "module", node, node
        label = str(
            attributes.get("environment")
            or attributes.get("env")
            or name.partition(":")[2]
            or name
        )
        return "envs", label, None

    @classmethod
    def _timeline_span(
        cls, row: Mapping[str, Any], index: int
    ) -> dict[str, Any] | None:
        """Normalize one SQLite/JSONL span for the browser timeline."""

        start_time = row.get("start_time")
        if not start_time:
            return None
        raw_end_time = row.get("end_time")
        end_time = raw_end_time or start_time
        name = str(row.get("name") or "span")
        raw_attributes = row.get("attributes", row.get("attributes_json", {}))
        if isinstance(raw_attributes, str):
            try:
                raw_attributes = json.loads(raw_attributes)
            except (TypeError, ValueError):
                raw_attributes = {}
        attributes = dict(raw_attributes) if isinstance(raw_attributes, Mapping) else {}
        if attributes.get("kind") == "routing" or name.startswith("routing:"):
            return None
        category, label, node = cls._trace_category(name, attributes)
        step = attributes.get("step_index", attributes.get("step"))
        if isinstance(step, bool):
            step = None
        elif step is not None:
            try:
                step = int(step)
            except (TypeError, ValueError):
                step = None
        attribute_keys = (
            "path",
            "caller_path",
            "caller_node",
            "kind",
            "graph",
            "node",
            "node_type",
            "operation",
            "module",
            "tool",
            "monitor",
            "environment",
            "env",
            "step_index",
            "step",
            "sequence",
            "sequence_no",
            "status",
            "reason",
            "message",
            "error",
            "task_id",
            "adapter_id",
            "worker_id",
            "gpu_id",
        )
        compact_attributes = {
            key: cls._compact_runtime_value(attributes[key], depth=1)
            for key in attribute_keys
            if key in attributes and attributes[key] is not None
        }
        return {
            "index": int(index),
            "span_id": str(row.get("span_id") or ""),
            "trace_id": str(row.get("trace_id") or ""),
            "parent_span_id": str(row.get("parent_span_id") or "") or None,
            "name": name,
            "label": label,
            "node": node,
            "category": category,
            "step": step,
            "start_time": str(start_time),
            "end_time": str(end_time),
            "status": (
                "running"
                if not raw_end_time
                and str(row.get("status") or "").lower() in {"", "unset", "running"}
                else str(row.get("status") or "unknown")
            ),
            "detail": name,
            "attributes": compact_attributes,
        }

    def _episode_timeline(
        self,
        job: Mapping[str, Any],
        full: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        trace_rows: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Query the run-owned Trace source, scoped to the requested attempt.

        Portal-submitted process runs create a shared observability database
        under ``<job>/observability``.  The server-level TraceQuery is still
        useful for legacy/read-only runs, but using it unconditionally leaves
        Portal trajectories with an empty timeline because the worker trace
        lives in the job directory.
        """

        identity = result.get("identity") or full.get("identity") or {}
        trace_query = self._job_trace_query(job)
        trace_truncated = bool(getattr(trace_rows, "truncated", False))
        rows = list(trace_rows or [])
        if (
            trace_rows is None
            and trace_query is not None
            and identity.get("run_id")
            and identity.get("episode_id")
            and (not full.get("generation_started_at_ns") or identity.get("attempt_id"))
        ):
            rows = trace_query.episode(
                run_id=identity["run_id"],
                episode_id=identity["episode_id"],
                attempt_id=identity.get("attempt_id"),
            )
        source = str(trace_query.db_path) if trace_query is not None else None
        spans = []
        for row in rows:
            span = self._timeline_span(row, len(spans))
            if span is not None:
                spans.append(span)
        # A live node has not closed its Trace span yet.  The runner publishes
        # a synthetic, short-lived span in the live snapshot so the timeline
        # still shows the active node between two database flushes.
        live_rows = full.get("live_timeline_spans")
        if not isinstance(live_rows, list):
            live = full.get("live")
            live_rows = live.get("timeline_spans") if isinstance(live, Mapping) else []
        existing_ids = {str(item.get("span_id") or "") for item in spans}
        existing_nodes = {
            (str(item.get("node") or ""), item.get("step"))
            for item in spans
            if item.get("node")
        }
        for row in live_rows:
            if not isinstance(row, Mapping):
                continue
            span = self._timeline_span(row, len(spans))
            if span is None:
                continue
            if str(span.get("span_id") or "") in existing_ids:
                continue
            node_key = (str(span.get("node") or ""), span.get("step"))
            if node_key in existing_nodes:
                continue
            spans.append(span)
            existing_ids.add(str(span.get("span_id") or ""))
            existing_nodes.add(node_key)
        trace_id = str(
            (spans[0].get("trace_id") if spans else None)
            or self._value_at(result, "trace_id", default=None)
            or ""
        )
        return {
            "available": bool(spans),
            "source": source,
            "trace_id": trace_id or None,
            "span_count": len(spans),
            "truncated": trace_truncated,
            "spans": spans,
        }

    def _episode_trace_rows(
        self,
        job: Mapping[str, Any],
        full: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Read the complete trace rows belonging to one episode.

        The episode JSON deliberately keeps the graph history compact.  Trace
        spans are the durable per-node event log and contain the observation
        references that the trajectory viewer needs to reconstruct every
        observation visit.
        """

        identity = result.get("identity") or full.get("identity") or {}
        if not isinstance(identity, Mapping) or (
            full.get("generation_started_at_ns") and not identity.get("attempt_id")
        ):
            return []
        run_id = identity.get("run_id")
        episode_id = identity.get("episode_id")
        trace_query = self._job_trace_query(job)
        if trace_query is None or not run_id or not episode_id:
            return []
        rows = _TraceRows()
        offset = 0
        while len(rows) < MAX_TRAJECTORY_TRACE_ROWS:
            try:
                page = trace_query.episode_page(
                    run_id=run_id,
                    episode_id=episode_id,
                    attempt_id=identity.get("attempt_id"),
                    limit=MAX_TRAJECTORY_TRACE_ROWS - len(rows),
                    offset=offset,
                )
            except (TimeoutError, QueryBudgetExceeded):
                # Runtime data in the episode remains useful even when a
                # damaged or unusually large trace cannot be read completely.
                rows.truncated = True
                break
            except Exception:
                # A partially written/rotated trace must not make a completed
                # episode disappear from the Portal.
                rows.truncated = bool(rows)
                break
            batch = page.get("rows") or []
            rows.extend(item for item in batch if isinstance(item, Mapping))
            next_offset = page.get("next_offset")
            if next_offset is None:
                break
            try:
                next_offset = int(next_offset)
            except (TypeError, ValueError):
                rows.truncated = True
                break
            if next_offset <= offset or not batch:
                rows.truncated = True
                break
            offset = next_offset
        if len(rows) >= MAX_TRAJECTORY_TRACE_ROWS:
            rows.truncated = True
        return rows

    @staticmethod
    def _observation_group_key(group: Mapping[str, Any]) -> str:
        """Return a stable key used when merging persisted and trace frames."""

        event = group.get("event") if isinstance(group.get("event"), Mapping) else {}
        span_id = event.get("span_id")
        if span_id:
            return f"span:{span_id}"
        frames = group.get("frames") if isinstance(group.get("frames"), list) else []
        uris = [
            str(frame.get("uri") or frame.get("url") or "")
            for frame in frames
            if isinstance(frame, Mapping)
        ]
        return json.dumps(
            {"event": event, "uris": uris},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    def _trace_observation_groups(
        self,
        job: Mapping[str, Any],
        full: Mapping[str, Any],
        rows: list[Mapping[str, Any]],
        *,
        job_id: str,
        adapter_id: str,
        global_index: int,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Extract visual observation groups from node Trace spans."""

        groups: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            raw_attributes = row.get("attributes", row.get("attributes_json", {}))
            if isinstance(raw_attributes, str):
                try:
                    raw_attributes = json.loads(raw_attributes)
                except (TypeError, ValueError):
                    raw_attributes = {}
            if not isinstance(raw_attributes, Mapping):
                continue
            attributes = dict(raw_attributes)
            name = str(row.get("name") or "")
            kind = str(attributes.get("kind") or "").lower()
            if kind != "node" and not name.startswith("node:"):
                continue
            node_id = attributes.get("node")
            if not node_id and name.startswith("node:"):
                node_id = name.rsplit(":", 1)[-1]
            if not node_id:
                continue
            raw_frames = self._feedback_frames(
                {"frames": attributes.get("frames", {})},
                {"image_uris": attributes.get("image_uris", {})},
            )
            event_frames = self._frame_sequence(
                raw_frames,
                limit=32,
                visual_only=True,
            ) or self._frame_sequence(raw_frames, limit=32)
            if not event_frames:
                continue
            for frame in event_frames:
                self._attach_observation_frame_url(
                    job,
                    full,
                    frame,
                    job_id=job_id,
                    adapter_id=adapter_id,
                    global_index=global_index,
                )
            event_value = dict(attributes)
            event_value.setdefault("node", str(node_id))
            if row.get("start_time") is not None:
                event_value.setdefault("timestamp", row.get("start_time"))
            if row.get("status") is not None:
                event_value.setdefault("status", row.get("status"))
            event_value.setdefault("event", "observation")
            compact_event = self._compact_node_event(event_value)
            if row.get("span_id"):
                compact_event["span_id"] = str(row["span_id"])
            groups.append(
                (
                    str(node_id),
                    {
                        "event": compact_event,
                        "frames": event_frames,
                        "frame_count": len(event_frames),
                    },
                )
            )
        return groups

    def _restore_trace_observations(
        self,
        job: Mapping[str, Any],
        full: Mapping[str, Any],
        result: Mapping[str, Any],
        runtime: dict[str, Any],
        *,
        trace_rows: list[Mapping[str, Any]],
        job_id: str,
        adapter_id: str,
        global_index: int,
    ) -> dict[str, Any]:
        """Merge durable Trace observations back into graph runtime JSON.

        Older episode records contain node enter/exit history but no frame
        payloads.  Keep that compact history as the source of node state and
        use the episode-scoped Trace only to restore observation groups.
        """

        if not isinstance(runtime, dict) or not trace_rows:
            return runtime
        groups = self._trace_observation_groups(
            job,
            full,
            trace_rows,
            job_id=job_id,
            adapter_id=adapter_id,
            global_index=global_index,
        )
        if not groups:
            return runtime
        sequence = runtime.setdefault("sequence", [])
        nodes = runtime.setdefault("nodes", [])
        if not isinstance(sequence, list) or not isinstance(nodes, list):
            return runtime

        def normalized(value: Any) -> str:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

        def group_matches(entry: Mapping[str, Any], group: Mapping[str, Any]) -> bool:
            key = self._observation_group_key(group)
            existing = entry.get("observation_groups")
            return isinstance(existing, list) and any(
                isinstance(item, Mapping) and self._observation_group_key(item) == key
                for item in existing
            )

        def event_matches(entry: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
            entry_events = entry.get("events")
            if not isinstance(entry_events, list):
                return False
            path = event.get("path")
            step = event.get("step")
            for item in entry_events:
                if not isinstance(item, Mapping):
                    continue
                if path is not None and item.get("path") is not None:
                    if normalized(item.get("path")) == normalized(path):
                        return True
                    continue
                if (
                    step is not None
                    and item.get("step") is not None
                    and str(item.get("step")) == str(step)
                ):
                    return True
            return False

        def append_observation(entry: dict[str, Any], group: dict[str, Any]) -> None:
            if group_matches(entry, group):
                return
            entry.setdefault("observation_groups", []).append(group)
            frames = (
                group.get("frames") if isinstance(group.get("frames"), list) else []
            )
            entry["observation_count"] = int(entry.get("observation_count", 0)) + len(
                frames
            )
            entry["observations"] = self._recent_frames(
                list(entry.get("observations") or []) + frames,
                limit=16,
            )

        for node_id, group in groups:
            event = (
                group.get("event") if isinstance(group.get("event"), Mapping) else {}
            )
            candidates = [
                entry
                for entry in sequence
                if isinstance(entry, dict) and str(entry.get("id")) == node_id
            ]
            entry = next(
                (item for item in candidates if event_matches(item, event)), None
            )
            if entry is None and candidates:
                step = event.get("step")
                # Display order is local to the truncated live history, not
                # the graph's execution step. Only use it for legacy entries
                # that have no execution identity at all.
                legacy_candidates = [
                    item
                    for item in candidates
                    if not any(
                        isinstance(value, Mapping)
                        and (
                            value.get("path") is not None
                            or value.get("step") is not None
                        )
                        for value in item.get("events", [])
                    )
                ]
                entry = next(
                    (
                        item
                        for item in legacy_candidates
                        if step is not None and str(item.get("order")) == str(step)
                    ),
                    legacy_candidates[0]
                    if len(legacy_candidates) == len(candidates) == 1
                    else None,
                )
            if entry is None:
                entry = {
                    "order": len(sequence) + 1,
                    "id": node_id,
                    "status": "completed",
                    "events": [dict(event)],
                    "event_count": 1,
                    "observations": [],
                    "observation_groups": [],
                }
                sequence.append(entry)
            append_observation(entry, group)

            aggregate = next(
                (
                    item
                    for item in nodes
                    if isinstance(item, dict) and str(item.get("id")) == node_id
                ),
                None,
            )
            if aggregate is None:
                aggregate = {
                    "id": node_id,
                    "order": len(nodes) + 1,
                    "status": entry.get("status", "completed"),
                    "events": list(entry.get("events") or []),
                    "event_count": int(entry.get("event_count", 0)),
                    "observations": [],
                    "observation_groups": [],
                }
                nodes.append(aggregate)
            append_observation(aggregate, group)

        # Trace can restore nodes older than the live history window. Merge
        # them chronologically instead of playing them after the latest node.
        def execution_position(entry: Mapping[str, Any], field: str) -> Any:
            values = [
                event[field]
                for event in entry.get("events", [])
                if isinstance(event, Mapping) and event.get(field) is not None
            ]
            try:
                if field == "timestamp":
                    return min(
                        datetime.fromisoformat(str(value)).timestamp()
                        for value in values
                    )
                return min(int(value) for value in values)
            except (ValueError, TypeError, OverflowError):
                return None

        for field in ("timestamp", "step"):
            positions = [execution_position(entry, field) for entry in sequence]
            if all(position is not None for position in positions):
                sequence[:] = [
                    entry
                    for _, entry in sorted(
                        zip(positions, sequence), key=lambda pair: pair[0]
                    )
                ]
                break
        for order, entry in enumerate(sequence, 1):
            entry["order"] = order
        return runtime

    def _job_trace_query(self, job: Mapping[str, Any]) -> TraceQuery | None:
        """Resolve a Portal job's own Trace database before the global source."""

        output_root = job.get("output_root")
        if output_root:
            root = Path(str(output_root)).resolve()
            candidates = [root / "observability" / "trace.sqlite3"]
            manifest = root / "observability.json"
            if manifest.is_file():
                try:
                    value = json.loads(manifest.read_text(encoding="utf-8"))
                    trace_db = (
                        value.get("trace_db") if isinstance(value, Mapping) else None
                    )
                    if trace_db:
                        candidates.insert(0, Path(str(trace_db)).expanduser().resolve())
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    pass
            for candidate in candidates:
                if candidate.is_file():
                    return TraceQuery(candidate)
        return self.trace_query

    def node_detail(
        self, job_id: str, *, adapter_id: str, global_index: int, path: list[str]
    ) -> dict[str, Any] | None:
        """Load one invocation's I/O on demand, never a different loop visit."""
        if len(path) < 3 or not all(isinstance(part, str) for part in path):
            raise ValueError("a complete node execution path is required")
        found = self._episode_record(
            job_id, adapter_id=adapter_id, global_index=global_index
        )
        if found is None:
            return None
        job, full = found
        result = self._value_at(full, "result", "output", "data", default={})
        result = result if isinstance(result, Mapping) else {}
        context = result.get("context", full.get("context", {}))
        context = context if isinstance(context, Mapping) else {}
        output = None
        source = None
        for key, value in (context.get("scoped_outputs") or {}).items():
            try:
                stored_path = json.loads(key)
            except (ValueError, TypeError):
                continue
            if stored_path == path:
                output, source = value, "scoped_output"
                break
        node_span = None
        calls = []
        for row in self._episode_trace_rows(job, full, result):
            attributes = row.get("attributes", row.get("attributes_json", {}))
            if isinstance(attributes, str):
                try:
                    attributes = json.loads(attributes)
                except (ValueError, TypeError):
                    continue
            if not isinstance(attributes, Mapping):
                continue
            if (
                attributes.get("kind") == "node"
                and attributes.get("path", attributes.get("execution_path")) == path
            ):
                node_span = {
                    key: row.get(key)
                    for key in ("span_id", "name", "start_time", "end_time", "status")
                }
                node_span["attributes"] = {
                    key: value
                    for key, value in attributes.items()
                    if key not in {"frames", "image_uris", "output"}
                }
                if attributes.get("output") is not None:
                    output, source = attributes["output"], "node_trace"
            if (
                attributes.get("kind") == "tool"
                and attributes.get("caller_path") == path
            ):
                calls.append(
                    {
                        **{
                            key: row.get(key)
                            for key in (
                                "span_id",
                                "name",
                                "start_time",
                                "end_time",
                                "status",
                            )
                        },
                        **{
                            key: attributes[key]
                            for key in (
                                "tool",
                                "arguments",
                                "result",
                                "result_metadata",
                                "ok",
                                "error",
                            )
                            if key in attributes
                        },
                    }
                )
        return {
            "path": path,
            "node": path[-2],
            "node_span": node_span,
            "input": (node_span or {}).get("attributes", {}).get("input"),
            "output": output,
            "output_source": source,
            "tool_calls": calls,
            "output_note": None
            if source
            else "此轮节点输出尚未保存；可查看下方工具调用的输入与返回结果。",
        }

    def episode_detail(
        self,
        job_id: str,
        *,
        adapter_id: str,
        global_index: int,
        include_runtime: bool = True,
        include_timeline: bool = True,
    ) -> dict[str, Any] | None:
        """Return one trajectory's media, node and subtask details."""

        found = self._episode_record(
            job_id,
            adapter_id=adapter_id,
            global_index=global_index,
        )
        if found is None:
            return None
        job, full = found
        result = self._value_at(full, "result", "output", "data", default={})
        if not isinstance(result, Mapping):
            result = {}
        context = self._value_at(
            result,
            "context",
            "runtime_context",
            "execution_context",
            "runtime",
            "state",
            default=self._value_at(
                full, "context", "runtime", "execution_context", default={}
            ),
        )
        if not isinstance(context, Mapping):
            context = {}
        values = self._value_at(
            context,
            "values",
            "state.values",
            "data",
            default=self._value_at(
                result,
                "values",
                "state.values",
                default=self._value_at(full, "values", default={}),
            ),
        )
        if not isinstance(values, Mapping):
            values = {}
        if isinstance(result.get("outputs"), Mapping):
            values = {**values, **result["outputs"]}
        feedback = self._value_at(
            values,
            "feedback",
            "observation_feedback",
            default=self._value_at(
                context,
                "feedback",
                "observation",
                "latest_observation",
                default=self._value_at(
                    result,
                    "feedback",
                    "observation",
                    default=self._value_at(full, "feedback", "observation", default={}),
                ),
            ),
        )
        if not isinstance(feedback, Mapping):
            feedback = {}
        state = self._value_at(
            feedback, "state", default=self._value_at(context, "state", default={})
        )
        if not isinstance(state, Mapping):
            state = {}
        task_id = str(
            self._value_at(
                full,
                "task_id",
                "task",
                "task_name",
                "case.task_id",
                "episode_id",
                default=self._value_at(
                    result, "task_id", "task", "task_name", default=""
                ),
            )
        )
        task_label = str(
            self._value_at(
                state,
                "annotation.human.task_description",
                "task_description",
                "instruction",
                "goal",
                default=self._value_at(
                    result, "task_description", "instruction", "goal", default=""
                ),
            )
            or task_id
            or self._value_at(full, "environment_id", "env_id", default="")
        )
        stm = self._value_at(
            values,
            "stm",
            "short_term_memory",
            "memory",
            default=self._value_at(result, "stm", "memory", default={}),
        )
        if not isinstance(stm, Mapping):
            stm = {}
        planner = self._value_at(
            values,
            "planner_result",
            "plan",
            "planner",
            default=self._value_at(result, "planner_result", "plan", default={}),
        )
        if not isinstance(planner, Mapping):
            planner = {"value": planner} if planner else {}
        verifier = self._value_at(
            values,
            "verifier_result",
            "verification",
            "verifier",
            default=self._value_at(
                result, "verifier_result", "verification", default={}
            ),
        )
        if not isinstance(verifier, Mapping):
            verifier = {"value": verifier} if verifier else {}
        raw_subtask = self._value_at(
            values,
            "subtask",
            "current_subtask",
            "subgoal",
            default=self._value_at(context, "subtask", "current_subtask", default=""),
        )
        if isinstance(raw_subtask, Mapping):
            subtask_value = self._value_at(
                raw_subtask, "current", "subgoal", "text", "description", default=""
            )
        else:
            subtask_value = raw_subtask
        remaining = stm.get("remaining", [])
        completed = stm.get("completed", [])
        if not isinstance(remaining, list):
            remaining = [remaining] if remaining else []
        if not isinstance(completed, list):
            completed = [completed] if completed else []
        current_subtask = str(
            planner.get("subgoal")
            or planner.get("instruction")
            or subtask_value
            or values.get("instruction")
            or (remaining[0] if remaining else "")
        )
        if include_runtime:
            runtime = full.get("graph_runtime")
            if not isinstance(runtime, dict):
                runtime = self._episode_graph_runtime(full)
            trace_rows = self._episode_trace_rows(job, full, result)
            runtime = self._restore_trace_observations(
                job,
                full,
                result,
                runtime,
                trace_rows=trace_rows,
                job_id=job_id,
                adapter_id=adapter_id,
                global_index=global_index,
            )
            # Live snapshots may already contain the complete per-step history,
            # including repeated sensor payloads.  Compact after trace recovery
            # so both old and new records have the same bounded response shape.
            runtime = self._compact_graph_runtime(runtime)
        else:
            trace_rows = []
            runtime_status = self._value_at(
                result,
                "graph_status",
                "runtime_status",
                "status",
                default=self._value_at(full, "status", default="unknown"),
            )
            runtime = {
                "graph_id": None,
                "status": str(runtime_status),
                "success": self._value_at(
                    result, "graph_success", "success", default=None
                ),
                "active_node": None,
                "errors": [],
                "sequence": [],
                "nodes": [],
            }
        raw_frames = self._feedback_frames(feedback, context, result, full)
        frames = self._recent_frames(
            raw_frames, limit=8, visual_only=True
        ) or self._recent_frames(raw_frames, limit=8)
        for frame in frames:
            self._attach_observation_frame_url(
                job,
                full,
                frame,
                job_id=job_id,
                adapter_id=adapter_id,
                global_index=global_index,
            )
        session_id = self._value_at(
            feedback,
            "session_id",
            "observation.session_id",
            default=self._value_at(
                context,
                "session_id",
                default=self._value_at(full, "session_id", default=""),
            ),
        )
        video_value = self._video_value(full, result)
        episode_path = self._safe_job_artifact(job, full.get("episode_json"))
        video_path = self._safe_job_artifact(job, video_value)
        if video_path is None and episode_path is not None:
            fallback = episode_path.with_suffix(".mp4")
            if fallback.is_file():
                video_path = fallback
        video_text = str(video_value) if video_value else ""
        external_video_url = (
            video_text
            if video_text.startswith(("http://", "https://", "/"))
            and video_path is None
            else None
        )
        timeline = (
            self._episode_timeline(job, full, result, trace_rows=trace_rows)
            if include_timeline
            else {
                "available": False,
                "source": None,
                "trace_id": None,
                "span_count": 0,
                "spans": [],
            }
        )
        compact = self._compact_episode(full)
        compact["task_label"] = task_label
        if not include_runtime:
            # The summary is used for the first paint.  Keep identifiers and
            # status, but do not send the live feedback object twice; media.live
            # already carries the image references needed by the browser.
            compact.pop("feedback", None)
            compact.pop("trace_dir", None)
            compact.pop("live_status_path", None)
        identifiers = {
            "experiment_id": self._value_at(
                full,
                "experiment_id",
                "job_id",
                "identity.experiment_id",
                default=self._value_at(result, "experiment_id", default=None),
            ),
            "run_id": self._value_at(
                full,
                "run_id",
                "run",
                "identity.run_id",
                default=self._value_at(result, "run_id", default=None),
            ),
            "episode_id": self._value_at(
                full,
                "episode_id",
                "trajectory_id",
                "identity.episode_id",
                default=self._value_at(
                    result, "episode_id", "trajectory_id", default=None
                ),
            ),
            "task_id": task_id or None,
            "attempt_id": (result.get("identity") or full.get("identity") or {}).get(
                "attempt_id"
            ),
            "generation_started_at_ns": full.get("generation_started_at_ns"),
            "session_id": str(session_id) if session_id else None,
        }
        return {
            "job_id": job_id,
            "adapter_id": adapter_id,
            "global_index": int(global_index),
            "episode": compact,
            "identifiers": identifiers,
            "runtime": runtime,
            "timeline": timeline,
            "subtask": {
                "goal": stm.get("goal") or task_label,
                "current": current_subtask,
                "completed": completed,
                "remaining": remaining,
                "planner": planner,
                "verifier": verifier,
                "task_item": self._value_at(
                    context,
                    "task_item",
                    "subtask",
                    default=self._value_at(values, "task_item", default=None),
                ),
            },
            "media": {
                "video": {
                    "available": bool(
                        (video_path and video_path.is_file()) or external_video_url
                    ),
                    "filename": video_path.name if video_path is not None else None,
                    "url": external_video_url,
                },
                "live": {
                    "available": bool(session_id and frames),
                    "session_id": str(session_id) if session_id else None,
                    "epoch": feedback.get("epoch"),
                    "sequence": feedback.get("sequence", feedback.get("sequence_no")),
                    "timestamp": feedback.get("timestamp"),
                    "frames": frames,
                },
            },
        }

    def episode_video_path(
        self,
        job_id: str,
        *,
        adapter_id: str,
        global_index: int,
    ) -> Path | None:
        """Return a validated video path for the read-only media endpoint."""

        found = self._episode_record(
            job_id,
            adapter_id=adapter_id,
            global_index=global_index,
        )
        if found is None:
            return None
        job, full = found
        result = self._value_at(full, "result", "output", "data", default={})
        if not isinstance(result, Mapping):
            result = {}
        video_value = self._video_value(full, result)
        path = self._safe_job_artifact(job, video_value)
        if path is None:
            episode_path = self._safe_job_artifact(job, full.get("episode_json"))
            path = (
                episode_path.with_suffix(".mp4") if episode_path is not None else None
            )
        if path is None or not path.is_file():
            return None
        return self._video_faststart_path(path)

    def _video_faststart_path(self, path: Path) -> Path:
        """Return a browser-friendly MP4 with its index at the front.

        ImageIO/FFmpeg can leave the ``moov`` atom at the end of a large
        recording.  That is valid MP4, but a browser connected through the
        Portal tunnel may wait for the whole file before it can start.  Keep
        the original artifact untouched and lazily create a sidecar with
        ``-movflags +faststart`` when the video is first requested.
        """

        if path.suffix.lower() != ".mp4":
            return path
        try:
            with path.open("rb") as handle:
                head = handle.read(4096)
            moov = head.find(b"moov")
            mdat = head.find(b"mdat")
            if moov >= 0 and (mdat < 0 or moov < mdat):
                return path
        except OSError:
            return path
        optimized = path.with_name(f"{path.stem}.faststart{path.suffix}")
        try:
            if (
                optimized.is_file()
                and optimized.stat().st_mtime_ns >= path.stat().st_mtime_ns
            ):
                return optimized
        except OSError:
            return path
        with self._lock:
            lock = self._video_faststart_locks.setdefault(path, threading.Lock())
        with lock:
            try:
                if (
                    optimized.is_file()
                    and optimized.stat().st_mtime_ns >= path.stat().st_mtime_ns
                ):
                    return optimized
            except OSError:
                pass
            ffmpeg = os.environ.get("ROBOTENON_FFMPEG") or shutil.which("ffmpeg")
            if not ffmpeg:
                try:
                    import imageio_ffmpeg

                    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
                except Exception:
                    ffmpeg = None
            if not ffmpeg:
                return path
            temporary = optimized.with_name(
                f".{optimized.stem}.{os.getpid()}.{uuid.uuid4().hex}.tmp.mp4"
            )
            try:
                subprocess.run(
                    [
                        str(ffmpeg),
                        "-y",
                        "-i",
                        str(path),
                        "-c",
                        "copy",
                        "-movflags",
                        "+faststart",
                        str(temporary),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=180,
                )
                temporary.replace(optimized)
                return optimized if optimized.is_file() else path
            except (OSError, subprocess.SubprocessError):
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                return path

    def episode_observation_path(
        self,
        job_id: str,
        *,
        adapter_id: str,
        global_index: int,
        uri: str,
        artifact_id: str | None = None,
        storage_path: str | None = None,
    ) -> Path | None:
        """Return a validated observation artifact belonging to one episode."""

        # The trajectory response already resolved and validated the artifact.
        # Reuse that path for the image request so every camera tile does not
        # have to parse the episode and its Trace history again.  The generic
        # job-root guard still prevents paths from escaping this experiment.
        if artifact_id:
            direct = self._resolve_observation_artifact(job_id, artifact_id)
            if direct is not None:
                return direct
        if storage_path:
            with self._lock:
                job = self._jobs.get(job_id)
            if job is not None:
                direct = self._safe_job_artifact(job, storage_path)
                if direct is not None and direct.is_file():
                    return direct

        found = self._episode_record(
            job_id,
            adapter_id=adapter_id,
            global_index=global_index,
        )
        if found is None:
            return None
        job, full = found
        result = self._value_at(full, "result", "output", "data", default={})
        if not isinstance(result, Mapping):
            result = {}
        context = self._value_at(
            result,
            "context",
            "runtime_context",
            "execution_context",
            "runtime",
            "state",
            default=self._value_at(
                full, "context", "runtime", "execution_context", default={}
            ),
        )
        if not isinstance(context, Mapping):
            context = {}
        values = self._value_at(
            context,
            "values",
            "state.values",
            "data",
            default=self._value_at(
                result,
                "values",
                "state.values",
                default=self._value_at(full, "values", default={}),
            ),
        )
        if not isinstance(values, Mapping):
            values = {}
        feedback = self._value_at(
            values,
            "feedback",
            "observation_feedback",
            default=self._value_at(
                context,
                "feedback",
                "observation",
                "latest_observation",
                default=self._value_at(
                    result,
                    "feedback",
                    "observation",
                    default=self._value_at(full, "feedback", "observation", default={}),
                ),
            ),
        )
        if not isinstance(feedback, Mapping):
            feedback = {}
        frames = self._feedback_frames(feedback, context, result, full)
        target = str(uri)
        for frame in frames:
            if str(frame.get("uri") or "") != target:
                continue
            path = self._observation_artifact_path(job, full, frame)
            if path is not None:
                return path
        # Historical node observations may be present only in the durable
        # episode Trace.  The latest feedback is not sufficient to resolve a
        # carousel URL after the run has completed, so fall back to the same
        # Trace-backed source used by ``episode_detail``.
        trace_rows = self._episode_trace_rows(job, full, result)
        for _node_id, group in self._trace_observation_groups(
            job,
            full,
            trace_rows,
            job_id=job_id,
            adapter_id=adapter_id,
            global_index=global_index,
        ):
            for frame in group.get("frames", []):
                if (
                    not isinstance(frame, Mapping)
                    or str(frame.get("uri") or "") != target
                ):
                    continue
                path = self._observation_artifact_path(job, full, frame)
                if path is not None:
                    return path
        return None

    @classmethod
    def _compact_runtime_value(cls, value: Any, *, depth: int = 2) -> Any:
        """Keep node details useful for the UI without returning sensor buffers."""

        # Trace attributes frequently contain the complete feedback payload as
        # a nested string (for example ``node.observe.output.feedback``).  A
        # trajectory response must remain a metadata view; returning that
        # string verbatim makes one case tens of megabytes and blocks the
        # browser while it parses/re-renders the page.
        if value is None or isinstance(value, (int, float, bool)):
            return value
        if isinstance(value, str):
            text = value
            return text if len(text) <= 512 else text[:509] + "..."
        if depth <= 0:
            text = str(value)
            return text if len(text) <= 512 else text[:509] + "..."
        if isinstance(value, Mapping):
            excluded = {
                "context",
                "observations",
                "frames",
                "actions",
                "raw",
                "history",
                "output",
                "feedback",
                "image_uris",
                "image_urls",
                "sensor_data",
                "observation",
                "payload",
            }
            return {
                str(key): cls._compact_runtime_value(item, depth=depth - 1)
                for key, item in list(value.items())[:40]
                if str(key) not in excluded
            }
        if isinstance(value, (list, tuple)):
            return [
                cls._compact_runtime_value(item, depth=depth - 1)
                for item in list(value)[:40]
            ]
        return str(value)

    @classmethod
    def _compact_runtime_frame(cls, frame: Any) -> dict[str, Any]:
        """Keep only the stable fields needed to request/render one image."""

        if not isinstance(frame, Mapping):
            return {"uri": str(frame)} if frame else {}
        fields = (
            "name",
            "frame_id",
            "uri",
            "url",
            "modality",
            "encoding",
            "shape",
            "camera_id",
            "capture_group_id",
            "sequence",
            "timestamp",
            "timestamp_ns",
            "artifact_id",
        )
        result: dict[str, Any] = {}
        for key in fields:
            value = frame.get(key)
            if value is None:
                continue
            if key == "shape" and isinstance(value, (list, tuple)):
                result[key] = [
                    cls._compact_runtime_value(item, depth=1)
                    for item in list(value)[:8]
                ]
            else:
                result[key] = cls._compact_runtime_value(value, depth=1)
        metadata = frame.get("metadata")
        if metadata is not None:
            result["metadata"] = cls._compact_runtime_value(metadata, depth=2)
        return result

    @classmethod
    def _compact_observation_group(cls, group: Any) -> dict[str, Any] | None:
        """Compact one observation visit while retaining direct image URLs."""

        if not isinstance(group, Mapping):
            return None
        raw_event = (
            group.get("event") if isinstance(group.get("event"), Mapping) else {}
        )
        event = cls._compact_node_event(raw_event)
        if raw_event.get("span_id") is not None:
            event["span_id"] = cls._compact_runtime_value(
                raw_event.get("span_id"), depth=1
            )
        raw_frames = group.get("frames", group.get("observations", []))
        if not isinstance(raw_frames, (list, tuple)):
            raw_frames = [raw_frames] if raw_frames else []
        frames = [
            compact
            for item in list(raw_frames)[:32]
            if (compact := cls._compact_runtime_frame(item))
        ]
        value: dict[str, Any] = {
            "event": event,
            "frames": frames,
            "frame_count": int(group.get("frame_count") or len(raw_frames)),
        }
        for key in ("observation_index", "observation_count"):
            if group.get(key) is not None:
                value[key] = cls._compact_runtime_value(group.get(key), depth=1)
        return value

    @classmethod
    def _compact_runtime_entry(
        cls,
        entry: Any,
        *,
        observation_group_limit: int = 8,
        event_limit: int = 8,
        observation_limit: int = 16,
    ) -> dict[str, Any] | None:
        """Compact one graph sequence/node entry for the trajectory browser."""

        if not isinstance(entry, Mapping):
            return None
        result: dict[str, Any] = {}
        for key in ("order", "id", "status", "event_count", "observation_count"):
            if entry.get(key) is not None:
                result[key] = cls._compact_runtime_value(entry.get(key), depth=1)
        raw_events = entry.get("events")
        if isinstance(raw_events, (list, tuple)):
            result["events"] = [
                cls._compact_node_event(item)
                if isinstance(item, Mapping)
                else cls._compact_runtime_value(item, depth=1)
                for item in list(raw_events)[-max(1, int(event_limit)) :]
            ]
        raw_observations = entry.get("observations", entry.get("frames", []))
        if isinstance(raw_observations, (list, tuple)):
            recent = cls._recent_frames(
                raw_observations, limit=max(1, int(observation_limit))
            )
            result["observations"] = [
                cls._compact_runtime_frame(item) for item in recent
            ]
        raw_groups = entry.get("observation_groups", entry.get("observationGroups", []))
        if isinstance(raw_groups, (list, tuple)):
            groups = [
                cls._compact_observation_group(item)
                for item in list(raw_groups)[-max(1, int(observation_group_limit)) :]
            ]
            result["observation_groups"] = [item for item in groups if item is not None]
        return result

    @classmethod
    def _compact_graph_runtime(cls, runtime: Mapping[str, Any]) -> dict[str, Any]:
        """Bound live/runtime payloads before they cross the Portal HTTP API."""

        result: dict[str, Any] = {}
        for key in ("graph_id", "status", "success", "active_node"):
            if runtime.get(key) is not None:
                result[key] = cls._compact_runtime_value(runtime.get(key), depth=1)
        errors = runtime.get("errors")
        if isinstance(errors, (list, tuple)):
            result["errors"] = [
                cls._compact_node_event(item)
                if isinstance(item, Mapping)
                else cls._compact_runtime_value(item, depth=1)
                for item in list(errors)[-32:]
            ]
        for key in ("sequence", "nodes"):
            value = runtime.get(key)
            if not isinstance(value, (list, tuple)):
                result[key] = []
                continue
            # A sequence entry is rendered as a node/state row.  Keep the
            # latest image group there for playback context; the node
            # aggregate below retains a larger recent window.  This keeps the
            # initial detail response small without removing the live/current
            # observation shown in ``media.live``.
            group_limit = 1 if key == "sequence" else 8
            event_limit = 1 if key == "sequence" else 8
            observation_limit = 3 if key == "sequence" else 16
            compacted = [
                cls._compact_runtime_entry(
                    item,
                    observation_group_limit=group_limit,
                    event_limit=event_limit,
                    observation_limit=observation_limit,
                )
                for item in value
            ]
            result[key] = [item for item in compacted if item is not None]
        return result

    @classmethod
    def _compact_node_event(cls, event: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize one graph event for node-click details."""

        keys = (
            "graph",
            "graph_id",
            "node",
            "node_id",
            "name",
            "event",
            "status",
            "phase",
            "timestamp",
            "time",
            "step",
            "env_step",
            "sequence",
            "sequence_no",
            "path",
            "reason",
            "message",
            "error",
            "condition",
            "when",
        )
        result = {
            key: cls._compact_runtime_value(event[key])
            for key in keys
            if key in event and event[key] is not None
        }
        for key in ("info", "metadata", "details"):
            if key in event and event[key] is not None:
                result[key] = cls._compact_runtime_value(event[key])
        return result

    @classmethod
    def _episode_graph_runtime(cls, record: dict[str, Any]) -> dict[str, Any]:
        """Summarize one case's graph history for the trajectory view."""

        result = cls._value_at(record, "result", "output", "data", default={})
        result = result if isinstance(result, Mapping) else {}
        context = cls._value_at(
            result,
            "context",
            "runtime_context",
            "execution_context",
            "runtime",
            "state",
            default=cls._value_at(record, "context", "runtime", default={}),
        )
        context = context if isinstance(context, Mapping) else {}
        history = cls._value_at(
            context,
            "history",
            "node_history",
            "graph_history",
            default=cls._value_at(result, "history", "node_history", default=[]),
        )
        if isinstance(history, Mapping):
            history = [
                dict(event, node_id=str(node_id))
                if isinstance(event, Mapping)
                else {"node_id": str(node_id), "status": event}
                for node_id, event in history.items()
            ]
        history = list(history) if isinstance(history, (list, tuple)) else []
        node_states_value = cls._value_at(
            context,
            "nodes",
            "node_states",
            "graph_nodes",
            default=cls._value_at(
                result, "nodes", "node_states", "graph_nodes", default=None
            ),
        )
        if isinstance(node_states_value, Mapping):
            history.extend(
                dict(event, node_id=str(node_id))
                if isinstance(event, Mapping)
                else {"node_id": str(node_id), "status": event}
                for node_id, event in node_states_value.items()
            )
        elif isinstance(node_states_value, (list, tuple)):
            history.extend(node_states_value)
        node_states: dict[str, str] = {}
        node_order: list[str] = []
        graph_id = cls._value_at(
            context,
            "current_graph",
            "graph_id",
            "active_graph",
            default=cls._value_at(result, "current_graph", "graph_id", default=None),
        )
        active_node = cls._value_at(
            context,
            "current_node",
            "active_node",
            "node",
            default=cls._value_at(
                result, "current_node", "active_node", "node", default=None
            ),
        )
        sequence: list[dict[str, Any]] = []
        sequence_by_key: dict[str, dict[str, Any]] = {}
        open_sequence_keys: dict[str, str] = {}
        errors: list[dict[str, Any]] = []
        node_events: dict[str, list[dict[str, Any]]] = {}
        node_frames: dict[str, list[dict[str, Any]]] = {}
        node_observation_groups: dict[str, list[dict[str, Any]]] = {}
        node_event_counts: dict[str, int] = {}
        node_observation_counts: dict[str, int] = {}
        for event in history:
            if not isinstance(event, Mapping):
                continue
            event_graph = cls._value_at(event, "graph", "graph_id")
            node_id = cls._value_at(event, "node", "node_id", "name")
            if event_graph and not graph_id:
                graph_id = event_graph
            event_name = str(
                cls._value_at(event, "event", "status", "phase", default="")
            ).lower()
            compact_event = cls._compact_node_event(event)
            if not node_id:
                if event_name in {"error", "failed", "failure"}:
                    errors.append(compact_event)
                continue
            node_id = str(node_id)
            if node_id not in node_states:
                node_order.append(node_id)
                node_states[node_id] = "pending"
            node_event_counts[node_id] = node_event_counts.get(node_id, 0) + 1
            node_events.setdefault(node_id, []).append(compact_event)
            if len(node_events[node_id]) > 8:
                del node_events[node_id][:-8]
            raw_frames = cls._feedback_frames(event)
            frames = cls._recent_frames(
                raw_frames, limit=8, visual_only=True
            ) or cls._recent_frames(raw_frames, limit=8)
            event_frames = cls._frame_sequence(
                raw_frames, limit=32, visual_only=True
            ) or cls._frame_sequence(raw_frames, limit=32)
            if frames:
                node_observation_counts[node_id] = node_observation_counts.get(
                    node_id, 0
                ) + len(raw_frames)
                node_frames[node_id] = cls._recent_frames(
                    node_frames.get(node_id, []) + frames,
                    limit=16,
                )
            path_value = cls._value_at(event, "path", default=None)
            step_value = cls._value_at(event, "step", "sequence", default=None)
            if path_value is not None:
                event_key = str(path_value)
            elif step_value is not None:
                event_key = f"{node_id}:{step_value}"
            elif (
                event_name
                in {"exit", "completed", "complete", "finish", "finished", "success"}
                and node_id in open_sequence_keys
            ):
                event_key = open_sequence_keys.pop(node_id)
            else:
                event_key = f"{node_id}:{len(sequence)}"
                if event_name in {"enter", "running", "start", "started"}:
                    open_sequence_keys[node_id] = event_key
            entry = sequence_by_key.get(event_key)
            if entry is None:
                entry = {
                    "order": len(sequence) + 1,
                    "id": node_id,
                    "status": "pending",
                    "events": [],
                    "observations": [],
                    "observation_groups": [],
                }
                sequence.append(entry)
                sequence_by_key[event_key] = entry
            entry["event_count"] = int(entry.get("event_count", 0)) + 1
            # Sequence entries are rendered for every graph visit. Keep only
            # routing/timing metadata here; the richer recent events live in
            # the per-node aggregate below and are shown when a node is
            # selected.
            sequence_event = {
                key: compact_event[key]
                for key in (
                    "graph",
                    "graph_id",
                    "node",
                    "node_id",
                    "event",
                    "status",
                    "phase",
                    "step",
                    "sequence",
                    "sequence_no",
                    "path",
                    "timestamp",
                    "time",
                    "reason",
                    "message",
                    "error",
                    "condition",
                    "when",
                )
                if key in compact_event
            }
            entry["events"].append(sequence_event)
            if len(entry["events"]) > 8:
                del entry["events"][:-8]
            if frames:
                entry["observation_count"] = int(
                    entry.get("observation_count", 0)
                ) + len(raw_frames)
                entry["observations"] = cls._recent_frames(
                    entry["observations"] + frames,
                    limit=16,
                )
            if event_frames:
                group = {
                    "event": {
                        key: compact_event[key]
                        for key in (
                            "event",
                            "status",
                            "phase",
                            "step",
                            "sequence",
                            "sequence_no",
                            "timestamp",
                            "time",
                            "path",
                        )
                        if key in compact_event
                    },
                    "frames": event_frames,
                    "frame_count": len(event_frames),
                }
                entry["observation_groups"].append(group)
                if len(entry["observation_groups"]) > 8:
                    del entry["observation_groups"][:-8]
                node_observation_groups.setdefault(node_id, []).append(group)
            if event_name in {"enter", "running", "start", "started"}:
                node_states[node_id] = "running"
                entry["status"] = "running"
                active_node = node_id
            elif event_name in {
                "exit",
                "completed",
                "complete",
                "finish",
                "finished",
                "success",
            }:
                node_states[node_id] = "completed"
                entry["status"] = "completed"
                active_node = node_id
            elif event_name in {"error", "failed", "failure"}:
                node_states[node_id] = "failed"
                entry["status"] = "failed"
                active_node = node_id
        status = cls._value_at(
            result,
            "graph_status",
            "runtime_status",
            "status",
            default=cls._value_at(record, "status", default="unknown"),
        )
        success = cls._value_at(result, "graph_success", "success", default=None)
        if success is None:
            success = str(status).lower() == "completed"
        elif isinstance(success, str):
            success = success.strip().lower() in {
                "1",
                "true",
                "yes",
                "success",
                "completed",
                "passed",
            }
        return {
            "graph_id": str(graph_id) if graph_id else None,
            "status": str(status),
            "success": bool(success),
            "active_node": str(active_node) if active_node else None,
            "errors": errors,
            "sequence": sequence,
            "nodes": [
                {
                    "id": node_id,
                    "order": index + 1,
                    "status": node_states[node_id],
                    "events": node_events.get(node_id, []),
                    "observations": node_frames.get(node_id, []),
                    "observation_groups": node_observation_groups.get(node_id, []),
                    "event_count": node_event_counts.get(node_id, 0),
                    "observation_count": node_observation_counts.get(node_id, 0),
                }
                for index, node_id in enumerate(node_order)
            ],
        }

    @classmethod
    def _compact_result(cls, result: Any) -> Any:
        """Keep result metadata while excluding observation/action payloads."""

        if not isinstance(result, dict):
            return result
        excluded = {
            "context",
            "observations",
            "frames",
            "actions",
            "action_chunk",
            "history",
            "raw",
        }
        return {
            key: value
            for key, value in result.items()
            if key not in excluded and not isinstance(value, (dict, list))
        }

    @classmethod
    def _compact_episode(cls, episode: dict[str, Any]) -> dict[str, Any]:
        record = {
            key: value
            for key, value in episode.items()
            if key
            not in {
                "result",
                "context",
                "observations",
                "frames",
                "actions",
                "live",
                "live_timeline_spans",
                # ``runtime`` is returned once at the top level of a detail
                # response.  Keeping the same graph history under episode as
                # well doubles the payload for long-running cases.
                "graph_runtime",
            }
        }
        if "result" in episode:
            record["result"] = cls._compact_result(episode.get("result"))
        return record

    @classmethod
    def _compact_progress(cls, progress: Any) -> Any:
        if not isinstance(progress, dict):
            return progress
        result = dict(progress)
        for key in ("current", "last"):
            value = result.get(key)
            if isinstance(value, dict):
                result[key] = cls._compact_episode(value)
        return result

    @classmethod
    def _compact_summary(cls, summary: Any) -> Any:
        if not isinstance(summary, dict):
            return summary
        result = {
            key: value for key, value in summary.items() if key != "task_adapters"
        }
        adapters = summary.get("task_adapters")
        if isinstance(adapters, dict):
            result["task_adapters"] = {
                adapter_id: {
                    key: value for key, value in stats.items() if key != "episodes"
                }
                for adapter_id, stats in adapters.items()
                if isinstance(stats, dict)
            }
        return result

    @classmethod
    def _public_job(
        cls, job: dict[str, Any], *, include_parameters: bool
    ) -> dict[str, Any]:
        record = dict(job)
        status = record.get("status")
        record["phase"] = {
            "waiting": "queued",
            "stopping": "cleanup",
            "stop_failed": "needs_attention",
        }.get(status, status)
        if status == "running":
            record["phase"] = job.get("runtime_phase", "loading")
        if "progress" in record:
            record["progress"] = cls._compact_progress(record.get("progress"))
            if job.get("status") in {"stopping", "stop_failed"} and isinstance(
                record["progress"], dict
            ):
                record["progress"]["status"] = job["status"]
        if "summary" in record:
            record["summary"] = cls._compact_summary(record.get("summary"))
        if not include_parameters:
            record.pop("parameters", None)
            record.pop("preflight", None)
        return record

    def graph_snapshot(self, job_id: str) -> dict[str, Any]:
        """Return the configured entry graph first, with other graphs available for navigation."""

        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            job = dict(self._jobs[job_id])
        loaded = self._load_job_config(job)
        graphs = list(getattr(loaded.graphs, "graphs", []))
        graph_values = [graph.model_dump(mode="json") for graph in graphs]
        runtime_node_states = {
            f"{graph.id}:{node.id}": "pending"
            for graph in graphs
            for node in graph.nodes
        }
        parameters = job.get("parameters")
        runtime_parameters = (
            parameters.get("runtime", {}) if isinstance(parameters, Mapping) else {}
        )
        configured = runtime_parameters.get("graph_id") or loaded.params.resolve(
            "runtime.graph_id"
        )
        graph_map = {graph.id: graph for graph in graphs}
        if configured:
            if str(configured) not in graph_map:
                raise ValueError(f"unknown configured graph {configured!r}")
            active_graph = str(configured)
        elif len(graphs) == 1:
            active_graph = graphs[0].id
        elif not graphs:
            active_graph = None
        else:
            raise ValueError(
                "the config must set runtime.graph_id when multiple graphs are enabled"
            )
        display_graph = graph_map.get(active_graph)
        display_node_states = (
            {f"{display_graph.id}:{node.id}": "pending" for node in display_graph.nodes}
            if display_graph
            else {}
        )
        if display_graph:
            graph_values.sort(key=lambda graph: graph["id"] != display_graph.id)
        return {
            "job_id": job_id,
            "graphs": graph_values,
            "primary_graph": display_graph.model_dump(mode="json")
            if display_graph is not None
            else None,
            "primary_graph_id": display_graph.id if display_graph is not None else None,
            "configured_graph_id": configured,
            "active_graph": active_graph,
            "active_node": None,
            "node_states": runtime_node_states,
            "primary_node_states": display_node_states,
        }

    def job_trace_query(self, job_id):
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            job = dict(self._jobs[job_id])
        return self._job_trace_query(job)

    def jobs_page(
        self, *, limit=100, offset=0, since=None, until=None, search="", cursor=None
    ):
        page = self._jobs.page(
            limit=limit,
            offset=offset,
            since=since,
            until=until,
            search=search,
            cursor=cursor,
        )
        deadline = time.monotonic() + 1
        with self._lock:
            records = []
            for job_id in page.pop("job_ids"):
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        "experiment query exceeded its time budget; use a smaller page"
                    )
                job = self._jobs[job_id]
                # Terminal runs are immutable. Re-reading their multi-megabyte
                # status and summary files on every history-page request makes
                # the default 20-row Portal page exceed its request budget.
                # Detail endpoints still refresh a selected run on demand.
                if job.get("status") in {"completed", "failed", "cancelled"}:
                    record = dict(job)
                else:
                    record = self._refresh_job(job_id)
                records.append(self._public_job(record, include_parameters=False))
            self._persist_jobs()
        for status in (
            "running",
            "waiting",
            "completed",
            "failed",
            "stop_failed",
            "stopping",
        ):
            page["overview"].setdefault(status, 0)
        return {"experiments": records, **page}

    def episodes_page(
        self,
        job_id,
        *,
        limit=100,
        offset=0,
        since=None,
        until=None,
        include_runtime=False,
    ):
        limit, offset, since, until = page_window(limit, offset, since, until)
        deadline = time.monotonic() + 1
        with self._lock:
            job = self._refresh_job(job_id) if job_id in self._jobs else None
        if job is None:
            return {"job_id": job_id, "episodes": [], "next_offset": None}
        index = self._result_index(job)
        if index.path.is_file():
            page = index.page(limit=limit, offset=offset, since=since, until=until)
        else:
            # Compatibility path for old small summaries. JSON size is capped
            # at the loader; new runs never scan a cumulative summary here.
            records = self.list_episodes(job_id, include_runtime=False)
            records = [
                item
                for item in records
                if (
                    not since
                    or item.get("updated_at", job.get("submitted_at", "")) >= since
                )
                and (
                    not until
                    or item.get("updated_at", job.get("submitted_at", "")) <= until
                )
            ]
            page = {
                "episodes": records[offset : offset + limit],
                "offset": offset,
                "limit": limit,
                "next_offset": offset + limit
                if offset + limit < len(records)
                else None,
            }
        for record in page["episodes"]:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    "episode query exceeded its time budget; use a smaller page"
                )
            if job.get("status") in {"cancelled", "failed"} and record.get(
                "status"
            ) in {"pending", "running"}:
                record["status"] = (
                    "cancelled" if job["status"] == "cancelled" else "error"
                )
            if include_runtime:
                record.setdefault("graph_runtime", self._episode_graph_runtime(record))
            else:
                record.pop("graph_runtime", None)
        return {"job_id": job_id, **page}

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            self._dispatch_waiting()
            records = [
                self._public_job(self._refresh_job(job_id), include_parameters=False)
                for job_id in self._jobs
            ]
        records.sort(key=lambda item: item.get("submitted_at", ""), reverse=True)
        return records

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            if job_id not in self._jobs:
                return None
            record = self._refresh_job(job_id)
            return self._public_job(record, include_parameters=True)

    def terminal_output(
        self, job_id: str, *, offset: int | None = None, limit: int = 64 * 1024
    ) -> dict[str, Any] | None:
        """Return a bounded, resumable chunk of a job's combined stdout/stderr log.

        The first request returns the tail so an old failed run immediately shows
        its final exception.  Later requests pass ``next_offset`` and only read
        newly appended bytes.  If the file is replaced or truncated, ``reset``
        tells the browser to replace its current buffer.
        """

        if offset is not None and offset < 0:
            raise ValueError("terminal offset must be non-negative")
        limit = min(max(int(limit), 1), 64 * 1024)
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            record = dict(job)

        raw_path = str(record.get("log_path") or "").strip()
        status = str(record.get("status") or "unknown")
        base = {
            "job_id": job_id,
            "status": status,
            "complete": status not in {"waiting", "starting", "running", "stopping"},
        }
        if not raw_path:
            return {
                **base,
                "text": "",
                "offset": 0,
                "next_offset": 0,
                "size": 0,
                "truncated": False,
                "reset": False,
                "available": False,
            }

        path = self._job_path(record, "log_path")
        try:
            path.relative_to(self.output_root.resolve())
        except ValueError as exc:
            raise ValueError(
                "terminal log path is outside the experiment output directory"
            ) from exc
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return {
                **base,
                "text": "",
                "offset": 0,
                "next_offset": 0,
                "size": 0,
                "truncated": False,
                "reset": False,
                "available": False,
            }

        reset = offset is not None and offset > size
        if offset is None or reset:
            start = max(0, size - limit)
            truncated = start > 0
        else:
            start = offset
            truncated = False
        with path.open("rb") as stream:
            stream.seek(start)
            payload = stream.read(limit)
        next_offset = start + len(payload)
        return {
            **base,
            "text": payload.decode("utf-8", errors="replace"),
            "offset": start,
            "next_offset": next_offset,
            "size": size,
            "truncated": truncated,
            "reset": reset,
            "available": True,
            "has_more": next_offset < size,
        }

    def _audit_control(
        self,
        *,
        action: str,
        job_id: str,
        source: Mapping[str, Any] | None,
        targets: list[dict[str, Any]] | None,
        reason: str,
    ) -> None:
        entry = {
            "timestamp": _now(),
            "action": action,
            "job_id": job_id,
            "request_source": dict(source or {"type": "internal", "pid": os.getpid()}),
            "target_tasks": targets,
            "reason": reason,
        }
        payload = (
            json.dumps(entry, ensure_ascii=False, default=str, separators=(",", ":"))
            + "\n"
        ).encode()
        descriptor = os.open(
            self.control_audit_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("control audit write made no progress")
                remaining = remaining[written:]
        finally:
            os.close(descriptor)

    def stop_job(
        self,
        job_id: str,
        *,
        adapter_id: str | None = None,
        global_index: int | None = None,
        reason: str = "user_stop",
        tasks: list[dict[str, Any]] | None = None,
        wait: bool = True,
        source: Mapping[str, Any] | None = None,
        audit: bool = True,
    ) -> dict[str, Any]:
        """Request cleanup. Python callers may wait, always outside the manager lock."""
        if tasks is not None or adapter_id is not None or global_index is not None:
            return self._control_tasks(
                job_id,
                "stop",
                tasks,
                adapter_id,
                global_index,
                source=source,
                reason=reason,
                audit=audit,
            )
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            job = self._refresh_job(job_id)
            if audit:
                self._audit_control(
                    action="stop",
                    job_id=job_id,
                    source=source,
                    targets=None,
                    reason=reason,
                )
            if job.get("status") == "waiting":
                self._jobs[job_id] = self._mark_cancelled(
                    job, reason=reason, termination={"pid": None, "terminated": True}
                )
                self._persist_jobs()
                return self._stop_result(job_id, scope="experiment_queue")
            if not self._job_is_running(job):
                return {**self._stop_result(job_id), "already_finished": True}
            thread = self._stop_threads.get(job_id)
            if thread is None or not thread.is_alive():
                job.update(
                    status="stopping",
                    stop_progress={
                        "phase": "cleanup",
                        "requested_at": _now(),
                        "reason": reason,
                    },
                )
                self._jobs[job_id] = job
                self._persist_jobs()
                thread = threading.Thread(
                    target=self._finish_stop,
                    args=(job_id, dict(job), reason),
                    name=f"stop-{job_id}",
                    daemon=True,
                )
                self._stop_threads[job_id] = thread
                thread.start()
        if wait:
            thread.join()
        with self._lock:
            result = self._stop_result(job_id)
        if wait and result["job"].get("status") == "stop_failed":
            if (
                result["job"].get("termination", {}).get("reason")
                == "pid_identity_mismatch"
            ):
                raise ValueError(
                    "refusing to signal a persisted PID that is not the experiment runner"
                )
            raise RuntimeError(
                result["job"].get("error", "experiment process was not fully released")
            )
        return result

    def _stop_result(self, job_id, *, scope="experiment_process"):
        job = self._jobs[job_id]
        return {
            "action": "stop",
            "scope": scope,
            "target": {"adapter_id": None, "global_index": None},
            "pending": job.get("status") == "stopping",
            "released": bool(job.get("termination", {}).get("terminated")),
            "job": self._public_job(job, include_parameters=False),
        }

    def _set_stop_progress(self, job_id, phase, **details):
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.get("status") == "stopping":
                job["stop_progress"] = {
                    **job.get("stop_progress", {}),
                    "phase": phase,
                    "updated_at": _now(),
                    **details,
                }

    def _finish_stop(self, job_id, snapshot, reason):
        # Signals, waits and process-domain verification can take seconds. They
        # must never hold the lock used by Portal reads and queue submission.
        try:
            termination = self._terminate_process_group(job_id, snapshot)
            error = (
                None
                if termination.get("terminated")
                else (
                    "experiment process was not fully released: "
                    + termination.get("reason", "unknown error")
                )
            )
        except Exception as exc:
            termination, error = {"terminated": False}, f"{type(exc).__name__}: {exc}"
        with self._lock:
            job = self._jobs[job_id]
            job["termination"] = termination
            if error:
                job.update(
                    status="stop_failed",
                    error=error,
                    stop_progress={
                        **job.get("stop_progress", {}),
                        "phase": "needs_attention",
                        "error": error,
                    },
                )
            else:
                job = self._mark_cancelled(job, reason=reason, termination=termination)
                job["stop_progress"] = {
                    **job.get("stop_progress", {}),
                    "phase": "released",
                    "ended_at": _now(),
                }
                self._processes.pop(job_id, None)
            self._jobs[job_id] = job
            self._persist_jobs()
            self._stop_threads.pop(job_id, None)

    def _source_submit_values(
        self, source: Mapping[str, Any]
    ) -> tuple[str | Path, dict[str, Any], bool | None, bool | None]:
        """Build the reproducible submission inputs shared by fork/rerun."""

        config = source.get("config_path") or source.get("config")
        if not config:
            raise ValueError("source experiment has no config")
        parameters = source.get("parameters")
        overrides: dict[str, Any] = {}
        if isinstance(parameters, dict):
            overrides["parameters"] = json.loads(
                json.dumps(parameters, ensure_ascii=False, default=str)
            )
        elif source.get("experiment_config_path"):
            fallback = Path(str(source["experiment_config_path"]))
            if fallback.is_file():
                value = json.loads(fallback.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    overrides = value
        summary = (
            source.get("summary") if isinstance(source.get("summary"), dict) else {}
        )
        options = (
            summary.get("options") if isinstance(summary.get("options"), dict) else {}
        )
        save_videos = (
            bool(options.get("save_videos")) if "save_videos" in options else None
        )
        save_traces = (
            bool(options.get("save_traces")) if "save_traces" in options else None
        )
        return config, overrides, save_videos, save_traces

    def _control_tasks(
        self,
        job_id,
        action,
        tasks,
        adapter_id=None,
        global_index=None,
        *,
        source=None,
        reason=None,
        audit=True,
    ):
        from ..runtime.task_control import control_lock
        from ..task_adapters.evaluator import write_json

        if tasks is None and (adapter_id is not None or global_index is not None):
            tasks = [{"adapter_id": adapter_id, "global_index": global_index}]
        if tasks is not None:
            if not isinstance(tasks, list) or not tasks or len(tasks) > 500:
                raise ValueError(
                    "tasks must contain 1..500 targets; use the experiment operation for all tasks"
                )
            normalized = []
            for task in tasks:
                if (
                    not isinstance(task, dict)
                    or not isinstance(task.get("adapter_id"), str)
                    or not task["adapter_id"]
                    or type(task.get("global_index")) is not int
                    or task["global_index"] < 0
                ):
                    raise ValueError(
                        "each task requires adapter_id and integer global_index"
                    )
                target = {
                    "adapter_id": task["adapter_id"],
                    "global_index": task["global_index"],
                }
                if target not in normalized:
                    normalized.append(target)
            tasks = normalized
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            job = self._refresh_job(job_id)
            if action == "rerun" and job.get("index_recovery", {}).get("status") in {
                "pending",
                "running",
            }:
                raise IndexRecoveryPending(
                    "wait for historical result recovery before rerunning"
                )
            if tasks is not None:
                if job.get("status") in {"stopping", "stop_failed"}:
                    raise ValueError("实验资源尚未释放，请先完成整个实验的停止操作")
                index = self._result_index(job)
                if index.path.is_file():
                    valid = index.contains_all(tasks)
                else:
                    available = {
                        (
                            item.get("adapter_id"),
                            self._value_at(item, "global_index", "index"),
                        )
                        for item in self.list_episodes(job_id)
                    }
                    valid = all(
                        (item["adapter_id"], item["global_index"]) in available
                        for item in tasks
                    )
                if not valid:
                    raise ValueError("selected task is not in this experiment")
            control_reason = reason or (
                "user_rerun" if action == "rerun" else "user_stop"
            )
            if audit:
                self._audit_control(
                    action=action,
                    job_id=job_id,
                    source=source,
                    targets=tasks,
                    reason=control_reason,
                )
            root = self._job_path(job, "status_path").parent
            control = root / ".control"
            request = {"action": action, "tasks": tasks}
            with control_lock(control):
                if self._job_is_running(job):
                    ready_path = control / "ready.json"
                    ready = (
                        json.loads(ready_path.read_text())
                        if ready_path.exists()
                        else {}
                    )
                    if ready.get("pid") != job.get("pid"):
                        raise ValueError(
                            "运行器尚未就绪或为旧版本，不支持在线 task 控制；请稍后重试，旧实验可停止后原地重跑"
                        )
                    write_json(
                        control
                        / (
                            "command-"
                            + str(time.time_ns())
                            + "-"
                            + uuid.uuid4().hex
                            + ".json"
                        ),
                        request,
                    )
                elif job.get("status") == "waiting":
                    write_json(
                        control
                        / (
                            "command-"
                            + str(time.time_ns())
                            + "-"
                            + uuid.uuid4().hex
                            + ".json"
                        ),
                        request,
                    )
                elif action == "stop":
                    return {
                        "job_id": job_id,
                        "action": action,
                        "already_finished": True,
                    }
                else:
                    process = self._processes.get(job_id)
                    if (process is not None and process.poll() is None) or (
                        process is None
                        and job.get("pid")
                        and self._pid_is_experiment(int(job["pid"]), job_id=job_id)
                    ):
                        raise ValueError("实验正在释放运行资源，请稍后重试")
                    if not job.get("command"):
                        raise ValueError("experiment has no saved execution command")
                    if not job.get("config_snapshot_path"):
                        loaded = self._load_job_config(job)
                        previous = (
                            self._read_json_cached(root / "status.json")
                            if (root / "status.json").is_file()
                            else {}
                        )
                        expected = job.get("parameter_hash") or previous.get(
                            "config_hash"
                        )
                        if expected != loaded.params.content_hash:
                            raise ValueError(
                                "cannot resume historical run: original configuration cannot be verified"
                            )
                        snapshot_path = root / "config.snapshot.json"
                        write_json(snapshot_path, freeze_config(loaded))
                        job["config_snapshot_path"] = str(snapshot_path)
                        job["command"] = [
                            *job["command"],
                            "--config-snapshot",
                            str(snapshot_path),
                        ]
                        job["parameter_hash"] = expected
                    write_json(control / "resume.json", request)
                    # Stale commands belonged to the terminated coordinator.
                    for path in control.glob("command-*.json"):
                        path.unlink()
                    write_json(
                        root / "status.json",
                        {
                            "run_id": job_id,
                            "status": "waiting",
                            "config_hash": job.get("parameter_hash"),
                        },
                    )
                    job.update(status="waiting", pid=None, returncode=None)
                    for key in (
                        "cancel_reason",
                        "cancelled_at",
                        "ended_at",
                        "termination",
                        "resources_released",
                        "stop_progress",
                        "error",
                        "returncode",
                        "index_recovery",
                    ):
                        job.pop(key, None)
                    self._jobs[job_id] = job
                    self._processes.pop(job_id, None)
                    self._persist_jobs()
            self._dispatch_waiting()
            return {
                "job_id": job_id,
                "action": action,
                "tasks": tasks,
                "scope": "tasks" if tasks is not None else "experiment",
                "job": self._public_job(self._jobs[job_id], include_parameters=False),
            }

    def rerun_job(
        self,
        job_id: str,
        *,
        adapter_id: str | None = None,
        global_index: int | None = None,
        tasks: list[dict[str, Any]] | None = None,
        source: Mapping[str, Any] | None = None,
        reason: str = "user_rerun",
    ) -> dict[str, Any]:
        """Replace results inside this run, retaining original case identities."""
        return self._control_tasks(
            job_id,
            "rerun",
            tasks,
            adapter_id,
            global_index,
            source=source,
            reason=reason,
        )

    def shutdown_all(
        self,
        *,
        reason: str = "global_shutdown",
        wait: bool = True,
        source: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Stop queued jobs first, then request cleanup for every active run."""
        with self._lock:
            job_ids = []
            for key in self._active_job_keys(waiting=True):
                job = self._refresh_job(key)
                if self._job_is_running(job) or job.get("status") == "waiting":
                    job_ids.append(key)
            job_ids.sort(key=lambda key: self._jobs[key].get("status") != "waiting")
            self._audit_control(
                action="shutdown",
                job_id="*",
                source=source,
                targets=[{"job_id": value} for value in job_ids],
                reason=reason,
            )
        stopped, errors = [], []
        for job_id in job_ids:
            try:
                result = self.stop_job(job_id, reason=reason, wait=wait, audit=False)
                stopped.append(
                    {
                        "job_id": job_id,
                        "released": result.get("released", False),
                        "pending": result.get("pending", False),
                    }
                )
            except Exception as exc:
                errors.append(
                    {"job_id": job_id, "error": f"{type(exc).__name__}: {exc}"}
                )
        return {
            "action": "shutdown",
            "scope": "all_experiment_processes",
            "reason": reason,
            "interrupted": stopped,
            "errors": errors,
            "count": len(stopped),
            "pending": any(item["pending"] for item in stopped),
            "requested_at": _now(),
        }

    def fork_draft(self, job_id: str) -> dict[str, Any]:
        """Return an editable, side-effect-free draft for a source experiment."""

        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            source = dict(self._refresh_job(job_id))
        config_path = source.get("config_path")
        config = source.get("config")
        if config_path:
            try:
                config = self._relative_config(Path(str(config_path)).resolve())
            except (OSError, ValueError):
                config = config or config_path
        if not config:
            raise ValueError("source experiment has no config")
        parameters = source.get("parameters")
        overrides: dict[str, Any] = {}
        if isinstance(parameters, dict):
            # ``params.values`` is the effective parameter tree used by the
            # source run.  Put it in the editable override layer so the form
            # reproduces that run before the user changes anything.
            overrides["parameters"] = json.loads(
                json.dumps(parameters, ensure_ascii=False, default=str)
            )
        elif source.get("experiment_config_path"):
            fallback = Path(str(source["experiment_config_path"]))
            if fallback.is_file():
                value = json.loads(fallback.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    overrides = value
        summary = (
            source.get("summary") if isinstance(source.get("summary"), dict) else {}
        )
        options = (
            summary.get("options") if isinstance(summary.get("options"), dict) else {}
        )
        draft = {
            "source_job_id": job_id,
            "config": config,
            "overrides": overrides,
            "parameters": parameters,
            "options": {
                key: options[key]
                for key in ("save_videos", "save_traces")
                if key in options
            },
        }
        try:
            if source.get("config_snapshot_path"):
                draft["config_document"] = {
                    "id": config,
                    "label": config,
                    "json": self._load_job_config(source).bundle.config,
                }
                draft["overrides"] = {}
            else:
                draft["config_document"] = self.get_config_document(config)
        except Exception:
            # The editable form can still use the source config and effective
            # parameters when an old config file is no longer readable.
            draft["config_document"] = None
        return draft

    def fork(self, job_id: str) -> dict[str, Any]:
        """Submit a new experiment with the selected run's effective parameters."""

        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            source = dict(self._refresh_job(job_id))
        config = source.get("config_path") or source.get("config")
        if not config:
            raise ValueError("source experiment has no config")
        parameters = source.get("parameters")
        overrides: dict[str, Any] = {}
        if isinstance(parameters, dict):
            # ``params.values`` is already the effective parameter tree. Keep
            # it as an override layer so the fork reproduces the source run.
            overrides["parameters"] = json.loads(
                json.dumps(parameters, ensure_ascii=False, default=str)
            )
        elif source.get("experiment_config_path"):
            fallback = Path(str(source["experiment_config_path"]))
            if fallback.is_file():
                value = json.loads(fallback.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    overrides = value
        summary = (
            source.get("summary") if isinstance(source.get("summary"), dict) else {}
        )
        options = (
            summary.get("options") if isinstance(summary.get("options"), dict) else {}
        )
        save_videos = (
            bool(options.get("save_videos")) if "save_videos" in options else None
        )
        save_traces = (
            bool(options.get("save_traces")) if "save_traces" in options else None
        )
        child = self.submit(
            config,
            config_snapshot=freeze_config(self._load_job_config(source)),
            save_videos=save_videos,
            save_traces=save_traces,
        )
        with self._lock:
            self._jobs[child["job_id"]]["forked_from"] = job_id
            self._persist_jobs()
            child["forked_from"] = job_id
        return child

    def preflight(self, config, overrides=None):
        from ..runtime.preflight import check_preflight

        loaded = load_application_config(self._config_path(config), overrides or None)
        return check_preflight(loaded, output_root=self.output_root, batch=True)

    def submit(
        self,
        config: str | Path,
        *,
        overrides: dict[str, Any] | None = None,
        save_videos: bool | None = None,
        save_traces: bool | None = None,
        forked_from: str | None = None,
        config_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if forked_from and config_snapshot is None:
            with self._lock:
                if forked_from not in self._jobs:
                    raise ValueError("source experiment does not exist")
                config_snapshot = freeze_config(
                    self._load_job_config(self._jobs[forked_from])
                )
        path = self._config_path(config) if config_snapshot is None else Path(config)
        if not path.is_absolute():
            path = self.config_root / path
        path = path.resolve()
        if config_snapshot is None:
            descriptor = self._describe(path)
        else:
            frozen = load_config_snapshot(config_snapshot)
            descriptor = {
                "id": self._relative_config(path),
                "path": str(path),
                "task_adapters": [
                    {
                        "id": x.id or x.name,
                        "name": x.name,
                        "kind": x.kind,
                        "enabled": x.enabled,
                    }
                    for x in frozen.task_adapters.task_adapters
                    if x.enabled
                ],
            }
        if not descriptor["task_adapters"]:
            raise ValueError("selected config has no enabled task adapter")
        job_id = (
            f"exp-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        )
        override_values = dict(overrides or {})
        override_path = self.output_root / job_id / "experiment_override.json"
        try:
            loaded = (
                load_config_snapshot(config_snapshot)
                if config_snapshot is not None
                else load_application_config(path, override_values or None)
            )
            if config_snapshot is not None and override_values:
                loaded = load_application_config(loaded.bundle.config, override_values)
            from ..runtime.runner import EpisodeRequest, _graph_selector
            from ..runtime.result_projection import require_episode_outputs

            selected = _graph_selector(
                loaded, loaded.params, EpisodeRequest(config=path)
            )
            require_episode_outputs(loaded.graphs.graph_map()[selected])
        except Exception as exc:
            raise ValueError(f"invalid experiment override: {exc}") from exc
        from ..runtime.preflight import check_preflight

        diagnostics = check_preflight(loaded, output_root=self.output_root, batch=True)
        if override_values:
            override_path.parent.mkdir(parents=True, exist_ok=True)
            override_path.write_text(
                json.dumps(override_values, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        effective_descriptor = descriptor
        if override_values:
            effective_adapters = [
                {
                    "id": item.id or item.name,
                    "name": item.name,
                    "kind": item.kind,
                    "enabled": item.enabled,
                }
                for item in loaded.task_adapters.task_adapters
                if item.enabled
            ]
            if not effective_adapters:
                raise ValueError("merged config has no enabled task adapter")
            effective_descriptor = dict(descriptor)
            effective_descriptor["task_adapters"] = effective_adapters
        log_path = self.output_root / f"{job_id}.log"
        snapshot_path = self.output_root / job_id / "config.snapshot.json"
        from ..task_adapters.evaluator import write_json

        write_json(snapshot_path, freeze_config(loaded))
        status_path = self.output_root / job_id / "status.json"
        command = [
            self.python_executable,
            str(self.entrypoint),
            "--batch",
            "--config",
            str(path),
            "--config-snapshot",
            str(snapshot_path),
            "--output-dir",
            str(self.output_root),
            "--run-id",
            job_id,
        ]
        if override_values:
            command.extend(["--experiment-config", str(override_path)])
        if save_videos is True:
            command.append("--save-videos")
        if save_traces is True:
            command.append("--save-traces")
        record = {
            "job_id": job_id,
            "config": effective_descriptor["id"],
            "config_path": effective_descriptor["path"],
            "config_snapshot_path": str(snapshot_path),
            "preflight": diagnostics,
            "task_adapters": effective_descriptor["task_adapters"],
            "experiment_config_path": str(override_path) if override_values else None,
            "status": "waiting",
            "pid": None,
            "returncode": None,
            "submitted_at": _now(),
            "status_path": str(status_path),
            "summary_path": str(self.output_root / job_id / "summary.json"),
            "log_path": str(log_path),
            "output_root": str(self.output_root / job_id),
            "command": command,
            "parameters": loaded.params.values,
            "parameter_hash": loaded.params.content_hash,
        }
        if forked_from:
            record["forked_from"] = str(forked_from)
        with self._lock:
            self._jobs[job_id] = record
            self._persist_jobs()
            self._dispatch_waiting()
        return dict(record)


__all__ = ["ExperimentManager"]
