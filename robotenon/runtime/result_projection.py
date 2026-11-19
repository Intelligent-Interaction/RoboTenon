"""Explicit episode result contracts and opt-in recovery of saved results."""
from copy import deepcopy

from ..workflow.bindings import resolve_bindings


def require_episode_outputs(graph):
    if "success" not in graph.outputs:
        raise ValueError(
            f"Graph {graph.id!r} 缺少 outputs.success 映射；无法判定任务成功。"
            "请更新旧实验/Fork 的 Graph 配置，显式指定成功信号来源。"
        )


def project_saved_episode(record, graphs):
    """Recover only completed, empty-output results using operator-supplied bindings.

    No node names or success inference are built into recovery. Error/cancelled
    records and already published outputs are never reinterpreted.
    """
    result = record.get("result") or {}
    bindings = graphs.get(result.get("graph_id"))
    if (record.get("status") != "completed" or result.get("graph_status") != "completed"
            or result.get("outputs") != {} or not bindings):
        return record
    context = result.get("context") or {}
    records = (context.get("node_records") or {}).get(context.get("root_scope"), {})
    if not records:
        return record
    outputs = resolve_bindings(bindings, records=records, inputs=context.get("values", {}))
    if type(outputs.get("success")) is not bool:
        raise ValueError("saved result recovery requires a boolean success signal")
    recovered = deepcopy(record)
    recovered["result"]["outputs"] = outputs
    for key, value in {"task_success": outputs["success"],
                       "reason": outputs.get("reason"),
                       "env_steps": outputs.get("env_steps", 0)}.items():
        recovered[key] = recovered["result"][key] = value
    recovered["result"]["output_recovery"] = {"bindings": deepcopy(bindings)}
    return recovered


def project_saved_summary(summary, graphs, load_record):
    recovered = deepcopy(summary)
    for stats in recovered.get("task_adapters", {}).values():
        for record in stats.get("episodes", []):
            if record.get("status") != "completed":
                continue
            full = load_record(record)
            projected = project_saved_episode(full, graphs)
            if not (projected.get("result") or {}).get("output_recovery"):
                continue
            for key in ("task_success", "reason", "env_steps"):
                record[key] = projected[key]
                record.setdefault("result", {})[key] = projected[key]
            record["result"]["outputs"] = projected["result"]["outputs"]
        attempted = [r for r in stats.get("episodes", []) if r.get("status") not in {"pending", "running"}]
        stats["successes"] = sum(r.get("status") == "completed" and r.get("task_success") is True for r in attempted)
        stats["success_rate"] = stats["successes"] / len(attempted) if attempted else None
    recovered["successes"] = sum(s["successes"] for s in recovered.get("task_adapters", {}).values())
    count = recovered.get("num_episodes", 0)
    recovered["episode_success_rate"] = recovered["successes"] / count if count else None
    return recovered
