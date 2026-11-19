"""Generate a RoboTenon application config from an extension's protocol profile.

python -m robotenon.benchmarks.configure --profile ... --policy-factory module:create --output ...
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path


def application_config(profile, *, policy_factory, policy_params=None, policy_interface="callable"):
    if policy_interface not in {"callable", "vla"}:
        raise ValueError("policy_interface must be callable or vla")
    profile = deepcopy(profile)
    mode = profile.get("mode", "sync")
    if mode != "sync":
        raise ValueError("native Graph profile currently supports sync only; live timing requires a dedicated Graph")
    allowed = {"benchmark_id", "params", "episodes_per_task", "max_steps", "tasks", "max_tasks", "mode",
               "source_config", "source_commit", "render", "hz", "pace", "paced", "wait_first_action", "recording", "name"}
    unknown = profile.keys() - allowed
    if unknown:
        raise ValueError(f"unmapped protocol profile fields: {sorted(unknown)}")
    if profile.get("recording"):
        raise ValueError("configure RoboTenon recording explicitly; native recording overrides are not silently ignored")
    name = profile["benchmark_id"]
    benchmark = name.removesuffix("_benchmark")
    plugin = f"robotenon_{benchmark}.benchmark_plugin"
    task_params = {k: profile[k] for k in ("params", "episodes_per_task", "max_steps", "tasks", "max_tasks", "render") if k in profile}
    graph = {"id": "benchmark_rollout", "entry": "reset", "max_steps": 1000000,
        "nodes": [
            {"id": "reset", "type": "system", "operation": "env.reset", "next": [{"node": "policy"}]},
            {"id": "policy", "type": "module", "module": "benchmark_policy", "predecessors": ["reset"], "next": [{"node": "execute"}]},
            {"id": "execute", "type": "system", "operation": "body.execute", "predecessors": ["policy"], "next": [{"node": "observe"}]},
            {"id": "observe", "type": "system", "operation": "env.observe", "predecessors": ["execute"], "next": [{"node": "terminal"}]},
            {"id": "terminal", "type": "module", "module": "benchmark_terminal", "predecessors": ["observe"],
             "default": "END", "goto": [{"node": "policy", "when": "ctx.results['terminal']['continue'] == true"}]},
        ], "outputs": {k: {"source": f"nodes.terminal.outputs.{k}"} for k in
                        ("success", "reason", "env_steps", "elapsed_sec", "native_metrics", "metric_keys", "native_case")}}
    return {
        "config_version": 1, "plugin_modules": ["robotenon.benchmarks.modules"],
        "modules": [{"id": "benchmark_policy", "name": "benchmark_policy", "kind": "agent"},
                    {"id": "benchmark_terminal", "name": "benchmark_terminal", "kind": "evaluator"}],
        "graphs": [graph], "plugin_environments": [plugin],
        "environments": [{"id": name, "name": name, "params": {"params": profile.get("params", {}), "render": profile.get("render", "gpu")}}],
        "plugin_bodies": [plugin], "bodies": [{"id": name, "name": name, "kind": "local",
                                                "params": {"heartbeat_ttl_s": 120.0}}],
        "plugin_task_adapters": [plugin], "task_adapters": [{"id": name, "name": name, "params": task_params}],
        "plugin_tools": ["robotenon.benchmarks.policy"],
        "tools": [{"id": "policy", "name": "benchmark_vla" if policy_interface == "vla" else "benchmark_callable", "kind": "model",
                   "params": {"factory": policy_factory, "params": policy_params or {}}}],
        "monitors": [],
        "parameters": {"runtime": {"profile": name, "environment_id": name, "graph_id": "benchmark_rollout",
                                    "monitor_enabled": False, "dry_run": False,
                                    "horizon": profile.get("max_steps", 300)},
                       "model": {"vla_tool": "policy"}, "policy": {"action_timeout_ms": 120000, "interface": policy_interface},
                       "task": {"task_id": "selected_by_task_adapter", "env_id": name},
                       "benchmark_protocol": profile},
        "observation_cache": {"enabled": True, "backend": "memory"},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--policy-factory", required=True)
    parser.add_argument("--policy-params", default="{}")
    parser.add_argument("--policy-interface", choices=["callable", "vla"], default="callable")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    config = application_config(json.loads(args.profile.read_text()), policy_factory=args.policy_factory,
                                policy_params=json.loads(args.policy_params), policy_interface=args.policy_interface)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(config, indent=2, ensure_ascii=False)+"\n")


if __name__ == "__main__":
    main()
