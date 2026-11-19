"""Projection of RoboDojo's official result artifacts into RoboTenon results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def find_result_file(
    root: str | Path,
    *,
    task_name: str,
    policy_name: str,
    run_id: str,
) -> Path:
    """Return the result JSON produced by one explicitly identified run.

    RoboDojo result roots may contain many historical evaluations. Matching a
    caller-provided run id is deliberate: selecting the newest file can attach
    a stale result when a process exits before writing its own artifact.
    """

    root = Path(root)
    candidates = [
        path
        for path in root.glob(f"RoboDojo/{task_name}/{policy_name}/**/{run_id}/*.json")
        if not path.name.startswith("_resume_") and path.name != "manifest.json"
    ]
    if not candidates:
        raise FileNotFoundError(
            f"no RoboDojo result JSON for task={task_name!r}, policy={policy_name!r}, "
            f"run_id={run_id!r} under {root}"
        )
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def project_result(path: str | Path) -> dict[str, Any]:
    """Normalize one RoboDojo result without discarding the native artifact."""

    path = Path(path)
    native = json.loads(path.read_text(encoding="utf-8"))
    success_rate = float(native.get("success_rate", 0.0) or 0.0)
    eval_time = int(native.get("eval_time", 0) or 0)
    score = float(native.get("score", 0.0) or 0.0)
    details = native.get("details") if isinstance(native.get("details"), dict) else {}
    successes = sum(
        bool(item.get("success"))
        for item in details.values()
        if isinstance(item, dict)
    )
    # A TaskCase represents an official evaluation invocation. It succeeds
    # when at least one requested trial succeeds; the exact benchmark metric
    # remains available as success_rate and score.
    return {
        "task_success": success_rate > 0.0,
        "reason": "robodojo_official_result",
        "env_steps": 0,
        "metrics": {
            "success_rate": success_rate,
            "score": score,
            "eval_time": eval_time,
            "successes": successes,
        },
        "artifacts": {"robodojo_result": str(path.resolve())},
        "native_result": native,
    }


__all__ = ["find_result_file", "project_result"]
