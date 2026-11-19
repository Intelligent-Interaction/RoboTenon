"""Human-friendly terminal reporting for the configuration-driven runner.

The runner deliberately keeps progress output separate from its machine-readable
result: log lines are written to ``stderr`` and the final JSON remains on
``stdout``.  This makes the CLI useful both interactively and from shell
scripts that pipe or parse the final result.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO


def format_duration(seconds: Any) -> str:
    """Format a duration compactly enough for a progress line."""

    try:
        value = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return "-"
    if value < 60:
        return f"{value:.1f}s"
    minutes, remainder = divmod(value, 60)
    if minutes < 60:
        return f"{int(minutes)}m{remainder:04.1f}s"
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours}h{minutes:02d}m{int(remainder):02d}s"


def format_rate(successes: Any, total: Any) -> str:
    """Format a count and percentage without raising on incomplete runs."""

    try:
        success_count = int(successes)
        total_count = int(total)
    except (TypeError, ValueError):
        return "-"
    if total_count <= 0:
        return f"{success_count}/0 (-)"
    return f"{success_count}/{total_count} ({success_count / total_count:.1%})"


class TerminalLogger:
    """Small dependency-free terminal reporter used by the experiment CLI."""

    _RESET = "\033[0m"
    _DIM = "\033[2m"
    _CYAN = "\033[36m"
    _BLUE = "\033[34m"
    _GREEN = "\033[32m"
    _YELLOW = "\033[33m"
    _RED = "\033[31m"
    _MAGENTA = "\033[35m"

    def __init__(
        self,
        *,
        enabled: bool = True,
        color: bool | None = None,
        verbose: bool = False,
        stream: TextIO | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.verbose = bool(verbose)
        self.stream = stream or sys.stderr
        self.graph_events = False
        if color is None:
            is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
            color = is_tty and not bool(os.environ.get("NO_COLOR"))
        self.color = bool(color) and self.enabled

    def _paint(self, text: str, color: str) -> str:
        if not self.color:
            return text
        return f"{color}{text}{self._RESET}"

    def _write(self, marker: str, message: str, color: str) -> None:
        if not self.enabled:
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
        stamp = self._paint(f"[{timestamp}]", self._DIM)
        label = self._paint(f"{marker:<7}", color)
        self.stream.write(f"{stamp} {label} {message}\n")
        self.stream.flush()

    def _detail(self, message: str) -> None:
        if not self.enabled or not self.verbose:
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
        stamp = self._paint(f"[{timestamp}]", self._DIM)
        self.stream.write(f"{stamp}         {message}\n")
        self.stream.flush()

    def banner(self, title: str) -> None:
        if not self.enabled:
            return
        line = "─" * max(36, min(88, len(title) + 12))
        self.stream.write(f"\n{self._paint(line, self._CYAN)}\n")
        self.stream.write(f"{self._paint(' RoboTenon ', self._CYAN)} {title}\n")
        self.stream.write(f"{self._paint(line, self._CYAN)}\n")
        self.stream.flush()

    def info(self, message: str) -> None:
        self._write("INFO", message, self._BLUE)

    def success(self, message: str) -> None:
        self._write("OK", message, self._GREEN)

    def warning(self, message: str) -> None:
        self._write("WARN", message, self._YELLOW)

    def error(self, message: str) -> None:
        self._write("ERROR", message, self._RED)

    def start_single(self, request: Any) -> None:
        self.graph_events = True
        self.banner("single episode")
        self.info(f"config={Path(str(request.config)).resolve()}")
        overrides = []
        for name in (
            "task_id",
            "env_id",
            "seed",
            "horizon",
            "graph_id",
            "model_path",
        ):
            value = getattr(request, name, None)
            if value is not None:
                overrides.append(f"{name}={value}")
        if overrides:
            self.info("overrides: " + ", ".join(overrides))
        if getattr(request, "dry_run", None):
            self.warning("dry-run enabled")

    def start_batch(self, request: Any) -> None:
        self.graph_events = False
        self.banner("batch experiment")
        self.info(f"config={Path(str(request.config)).resolve()}")
        values = []
        for name in ("task_set", "max_tasks", "num_trials", "split", "run_id"):
            value = getattr(request, name, None)
            if value is not None:
                values.append(f"{name}={value}")
        if values:
            self.info("overrides: " + ", ".join(values))
        if getattr(request, "dry_run", None):
            self.warning("dry-run enabled")

    def plan(self, *, config: Path, graph_id: Any, env_id: Any, adapters: list[str]) -> None:
        graph = graph_id or "(config/default)"
        environment = env_id or "(config/default)"
        self.info(f"graph={graph}  environment={environment}")
        self.info("task adapters: " + (" -> ".join(adapters) if adapters else "(none)"))

    def case_start(self, completed: int, total: int, case: Mapping[str, Any]) -> None:
        number = completed + 1
        task_id = case.get("task_id", "?")
        adapter_id = case.get("adapter_id", "?")
        episode = case.get("episode", 0)
        seed = case.get("seed")
        seed_text = f", seed={seed}" if seed is not None else ""
        self._write(
            "RUN",
            f"[{number:>{len(str(max(total, 1)))}}/{total}] "
            f"{adapter_id}/{task_id}  episode={episode}{seed_text}",
            self._CYAN,
        )

    def case_complete(self, completed: int, total: int, record: Mapping[str, Any]) -> None:
        status = str(record.get("status", "unknown"))
        succeeded = bool(record.get("task_success", False))
        if status == "error":
            marker, color, outcome = "ERROR", self._RED, "error"
        elif succeeded:
            marker, color, outcome = "OK", self._GREEN, "success"
        else:
            marker, color, outcome = "FAIL", self._YELLOW, "failed"
        task = f"{record.get('adapter_id', '?')}/{record.get('task_id', '?')}"
        details = [
            f"[{completed}/{total}]",
            task,
            f"episode={record.get('episode', 0)}",
            outcome,
            f"steps={record.get('env_steps', 0)}",
            f"elapsed={format_duration(record.get('elapsed_s'))}",
        ]
        if record.get("reason"):
            details.append(f"reason={record['reason']}")
        if record.get("error"):
            details.append(f"error={record['error']}")
        self._write(marker, "  ".join(details), color)
        result = record.get("result")
        if isinstance(result, Mapping):
            artifacts = []
            for key in ("video_path", "trace_dir", "trace_sqlite", "metrics_wal"):
                value = result.get(key)
                if value:
                    artifacts.append(f"{key}={value}")
            if record.get("episode_json"):
                artifacts.append(f"episode_json={record['episode_json']}")
            if artifacts:
                self._detail("artifacts: " + "  ".join(artifacts))

    def graph_event(
        self,
        _context: Any,
        event: Mapping[str, Any],
        _result: Any = None,
    ) -> None:
        """Print a compact node lifecycle line for an interactive episode."""

        if not self.graph_events:
            return
        event_name = str(event.get("event", ""))
        if event_name not in {"enter", "exit", "error"}:
            return
        graph = event.get("graph", "?")
        node = event.get("node", "?")
        step = event.get("step", "?")
        if event_name == "enter":
            self._write("NODE", f"step={step}  {graph}/{node}  -> running", self._MAGENTA)
        elif event_name == "error":
            self._write(
                "ERROR",
                f"step={step}  {graph}/{node}  -> error: {event.get('error', 'unknown error')}",
                self._RED,
            )
        else:
            status = event.get("status") or "completed"
            success = event.get("success")
            outcome = f"{status}" if success is None else f"{status}, success={success}"
            self._write("NODE", f"step={step}  {graph}/{node}  -> {outcome}", self._GREEN)

    def single_complete(self, result: Mapping[str, Any]) -> None:
        succeeded = bool(result.get("task_success", False))
        status = "success" if succeeded else "failed"
        self._write(
            "OK" if succeeded else "FAIL",
            f"{result.get('task_id', '?')}  {status}  "
            f"graph={result.get('graph_id', '?')}  "
            f"env={result.get('env_id', '?')}  "
            f"steps={result.get('env_steps', 0)}  "
            f"elapsed={format_duration(result.get('elapsed_s'))}",
            self._GREEN if succeeded else self._YELLOW,
        )
        if result.get("reason"):
            self.info(f"reason={result['reason']}")
        artifacts = []
        for key in (
            "output_path",
            "video_path",
            "trace_dir",
            "trace_jsonl",
            "trace_sqlite",
            "metrics_dir",
            "metrics_wal",
            "memory_path",
        ):
            value = result.get(key)
            if value:
                artifacts.append(f"{key}={value}")
        if artifacts:
            self.info("artifacts: " + "  ".join(artifacts))

    def batch_complete(self, result: Mapping[str, Any]) -> None:
        total = result.get("num_episodes", 0)
        successes = result.get("successes", 0)
        errors = result.get("num_errors", 0)
        self.banner("experiment finished")
        message = (
            f"success={format_rate(successes, total)}  "
            f"attempted={total}  errors={errors}"
        )
        all_successful = int(errors or 0) == 0 and int(successes or 0) == int(total or 0)
        self._write(
            "OK" if all_successful else "WARN",
            message,
            self._GREEN if all_successful else self._YELLOW,
        )
        run_id = result.get("run_id")
        output_dir = (result.get("options") or {}).get("output_dir")
        if run_id:
            self.info(f"run_id={run_id}")
        if output_dir:
            self.info(f"artifacts={Path(str(output_dir)) / str(run_id) if run_id else output_dir}")
        if self.verbose:
            for adapter_id, stats in (result.get("task_adapters") or {}).items():
                self.info(
                    f"adapter={adapter_id}  "
                    f"success={format_rate(stats.get('successes', 0), stats.get('num_cases', 0))}"
                )

    def failed(self, exc: BaseException) -> None:
        self.error(f"{type(exc).__name__}: {exc}")


__all__ = ["TerminalLogger", "format_duration", "format_rate"]
