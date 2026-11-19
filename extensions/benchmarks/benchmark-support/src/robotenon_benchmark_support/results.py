"""Native metric aggregation from RoboTenon episode records.

Errors count as unsuccessful attempts. Missing optional metrics remain missing,
matching the reviewed protocol; an unfinished suite is explicitly partial.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def aggregate(records, metric_keys, *, expected_episodes=None):
    functions = {"mean": lambda v: sum(v)/len(v), "sum": sum, "min": min, "max": max}
    unknown = set(metric_keys.values()) - functions.keys()
    if unknown:
        raise ValueError(f"unsupported metric aggregations: {sorted(unknown)}")
    # Prefer the last attempt by explicit case identity, never by task name alone.
    unique = {}
    for record in records:
        identity = record.get("case_key")
        if identity is None:
            raise ValueError("native metric records require case_key")
        attempt = int(record.get("attempt", 0))
        old = unique.get(identity)
        if old is not None and attempt == int(old.get("attempt", 0)):
            raise ValueError("duplicate case/attempt; provide explicit attempt order")
        if old is None or attempt >= int(old.get("attempt", 0)):
            unique[identity] = record
    terminal = [r for r in unique.values() if r.get("status") not in {"pending", "running"}]
    episodes = []
    for record in terminal:
        metrics = dict(record.get("metrics") or {})
        if record.get("status") != "completed" or record.get("failure_reason"):
            metrics["success"] = False
        episodes.append({**record, "metrics": metrics})

    def summary(items):
        result = {"num_episodes": len(items), "num_errors": sum(
            r.get("status") != "completed" or bool(r.get("failure_reason")) for r in items)}
        for key, method in metric_keys.items():
            values = [r["metrics"][key] for r in items if isinstance(r["metrics"].get(key), (int, float, bool))]
            if values:
                result[f"{method}_{key}"] = round(functions[method](values), 4)
        success_values = [bool(r["metrics"].get("success", False)) for r in items]
        result["num_successes"] = sum(success_values)
        result["success_rate"] = round(sum(success_values) / len(items), 4) if items else None
        return result

    task_groups = {}
    for episode in episodes:
        task_groups.setdefault(episode["task"], []).append(episode)
    by_task = [
        {"task": task, **summary(items), "episodes": items}
        for task, items in task_groups.items()
    ]
    dimension_groups = {}
    for episode in episodes:
        for dimension, value in (episode.get("dimensions") or {}).items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            dimension_groups.setdefault(dimension, {}).setdefault(value, []).append(episode)
    by_dimension = {}
    for dimension, values in sorted(dimension_groups.items()):
        by_dimension[dimension] = [
            {"value": value, **summary(items)}
            for value, items in sorted(values.items(), key=lambda item: str(item[0]))
        ]
    overall = summary(episodes)
    return {**overall, "overall": overall, "metric_keys": metric_keys,
            "partial": len(terminal) < (expected_episodes if expected_episodes is not None else len(unique)),
            "expected_episodes": expected_episodes,
            "tasks": by_task, "by_task": by_task, "by_dimension": by_dimension}


def from_episode_files(paths):
    """Read full per-attempt RoboTenon records, preserving optional native metrics."""
    records, keys = [], None
    for path in paths:
        record = json.loads(Path(path).read_text())
        result = record.get("result", record)
        outputs = result.get("outputs", {})
        current_keys = outputs.get("metric_keys")
        if current_keys:
            if keys is not None and keys != current_keys:
                raise ValueError("cannot aggregate different benchmark metric contracts")
            keys = current_keys
        case_key = record.get("key") or record.get("case_key")
        if case_key is None and "adapter_id" in record and "global_index" in record:
            case_key = f"{record['adapter_id']}:{record['global_index']}"
        if case_key is None:
            case = record.get("case", {})
            case_key = case.get("key") or outputs.get("native_case")
        if case_key is None:
            raise ValueError(f"{path}: missing stable TaskCase key")
        case = record.get("case", {})
        dimensions = outputs.get("native_dimensions")
        if dimensions is None:
            dimensions = record.get("dimensions") or case.get("dimensions") or case.get("metadata", {}).get("dimensions") or {}
        records.append({"case_key": case_key, "attempt": record.get("attempt", 0),
                        "task": record.get("task_id", result.get("task_id")),
                        "status": record.get("status", result.get("graph_status", "failed")),
                        "metrics": outputs.get("native_metrics", {}),
                        "dimensions": dimensions,
                        "failure_reason": record.get("error")})
    if keys is None:
        raise ValueError("no native metric contract in input records")
    return records, keys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episodes", nargs="+", type=Path)
    parser.add_argument("--expected-episodes", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records, keys = from_episode_files(args.episodes)
    result = aggregate(records, keys, expected_episodes=args.expected_episodes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False)+"\n")


if __name__ == "__main__":
    main()
