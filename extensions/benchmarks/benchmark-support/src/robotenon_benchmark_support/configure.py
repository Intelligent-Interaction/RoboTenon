"""Generate a RoboTenon application config from an extension's protocol profile.

python -m robotenon_benchmark_support.configure --profile ... --policy-factory module:create --output ...
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path


def application_config(profile, *, policy_factory=None, policy_params=None, policy_interface="vla"):
    if policy_interface not in {"callable", "vla"}:
        raise ValueError("policy_interface must be callable or vla")
    profile = deepcopy(profile)
    mode = profile.get("mode", "sync")
    if mode not in {"sync", "live"}:
        raise ValueError("native Graph mode must be sync or live")
    if mode == "live":
        import math
        for key, default in (("hz", 10.0), ("pace", 1.0)):
            value = profile.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value) or (key == "hz" and not math.isfinite(value)) or value <= 0:
                raise ValueError(f"live {key} must be positive (hz must also be finite)")
        if type(profile.get("wait_first_action", False)) is not bool:
            raise ValueError("wait_first_action must be boolean")
    allowed = {"benchmark_id", "params", "episodes_per_task", "max_steps", "tasks", "max_tasks", "mode",
               "source_config", "source_commit", "render", "hz", "pace", "paced", "wait_first_action", "recording", "name", "throughput_mode"}
    unknown = profile.keys() - allowed
    if unknown:
        raise ValueError(f"unmapped protocol profile fields: {sorted(unknown)}")
    if profile.get("recording"):
        raise ValueError("configure RoboTenon recording explicitly; native recording overrides are not silently ignored")
    name = profile["benchmark_id"]
    benchmark = name.removesuffix("_benchmark")
    policy_factory = policy_factory or f"robotenon_{benchmark}.vla:create"
    plugin = f"robotenon_{benchmark}.benchmark_plugin"
    task_params = {k: profile[k] for k in ("params", "episodes_per_task", "max_steps", "tasks", "max_tasks", "render", "throughput_mode") if k in profile}
    task_params["vla_factory"] = policy_factory
    if mode == "live":
        task_params["live"] = True
    graph = {"id": "benchmark_rollout", "entry": "reset", "max_steps": 1000000,
        "nodes": [
            {"id": "reset", "type": "system", "operation": "env.reset", "next": [{"node": "policy"}]},
            {"id": "policy", "type": "module", "module": "native_benchmark_policy", "predecessors": ["reset"], "next": [{"node": "execute"}]},
            {"id": "execute", "type": "system", "operation": "body.execute", "predecessors": ["policy"], "next": [{"node": "observe"}]},
            {"id": "observe", "type": "system", "operation": "env.observe", "predecessors": ["execute"], "next": [{"node": "terminal"}]},
            {"id": "terminal", "type": "module", "module": "native_benchmark_terminal", "predecessors": ["observe"],
             "default": "END", "goto": [{"node": "policy", "when": "ctx.results['terminal']['continue'] == true"}]},
        ], "outputs": {k: {"source": f"nodes.terminal.outputs.{k}"} for k in
                        ("success", "reason", "env_steps", "elapsed_sec", "native_metrics", "native_dimensions", "metric_keys", "native_case")}}
    if mode == "live":
        graph["id"] = "benchmark_live_rollout"
        observe = next(n for n in graph["nodes"] if n["id"] == "observe")
        observe["next"] = [{"node": "live_tick"}]
        graph["nodes"].insert(-1, {"id": "live_tick", "type": "module",
            "module": "native_benchmark_live_tick", "predecessors": ["observe"],
            "next": [{"node": "terminal"}]})
        graph["nodes"][-1]["predecessors"] = ["live_tick"]
        graph["outputs"]["rt_metrics"] = {"source": "nodes.terminal.outputs.rt_metrics"}
    config = {
        "config_version": 1, "plugin_modules": ["robotenon_benchmark_support.modules"],
        "modules": [{"id": "native_benchmark_policy", "name": "native_benchmark_policy", "kind": "agent"},
                    {"id": "native_benchmark_terminal", "name": "native_benchmark_terminal", "kind": "evaluator"}],
        "graphs": [graph], "plugin_environments": [plugin],
        "environments": [{"id": name, "name": name, "params": {"params": profile.get("params", {}), "render": profile.get("render", "gpu")}}],
        "plugin_bodies": [plugin], "bodies": [{"id": name, "name": name, "kind": "local",
                                                "params": {"heartbeat_ttl_s": 120.0}}],
        "plugin_task_adapters": [plugin], "task_adapters": [{"id": name, "name": name, "params": task_params}],
        "plugin_tools": ["robotenon_benchmark_support.policy"],
        "tools": [{"id": "policy", "name": "native_benchmark_vla" if policy_interface == "vla" else "native_benchmark_callable", "kind": "model",
                   "params": {"factory": policy_factory, "params": policy_params or {}}}],
        "monitors": [],
        "parameters": {"runtime": {"profile": name, "environment_id": name, "graph_id": graph["id"],
                                    "execution": {"backend": "native_sync", "max_workers": 1, "max_retries": 0,
                                                  "memory_mode": "disabled", "plugin_backends": ["robotenon_benchmark_support.execution"]},
                                    "monitor_enabled": False, "dry_run": False,
                                    "horizon": profile.get("max_steps", 300)},
                       "model": {"vla_tool": "policy"}, "policy": {"action_timeout_ms": 120000, "interface": policy_interface},
                       "environment": {"parameters": {"vla_factory": policy_factory}},
                       "task": {"task_id": "selected_by_task_adapter", "env_id": name},
                       "benchmark_protocol": profile},
        "observation_cache": {"enabled": True, "backend": "memory"},
    }

    if mode == "live":
        config["modules"].append({"id": "native_benchmark_live_tick", "name": "native_benchmark_live_tick", "kind": "evaluator"})
    return config


def write_application_config(config, output):
    """Write a master config plus discoverable Graph and Module manifests."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    master = deepcopy(config)
    for section, fields in (("graphs", ("graphs",)),
                            ("modules", ("plugin_modules", "modules"))):
        manifest = {"config_version": master.get("config_version", 1)}
        manifest.update({key: master.pop(key) for key in fields})
        child = output.with_name(f"{output.stem}_{section}.json")
        child.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        master[f"{section}_config"] = child.name
    output.write_text(json.dumps(master, indent=2, ensure_ascii=False) + "\n")
    return master


def enable_batch_execution(config, *, workers, worker_devices=(), model_devices=(),
                           max_batch_size=8, max_wait_ms=5, interface='batch', inference_mode='batch', egl_device_ids=None):
    """Opt-in parallel profile; does not rewrite the native Graph protocol."""
    if interface not in {'batch', 'legacy'}:
        raise ValueError('batch interface must be batch or legacy')
    tool = next(t for t in config['tools'] if t['id'] == 'policy')
    old_params = tool['params']
    tool['name'] = 'native_benchmark_batch_vla'
    tool['params'] = {'service': 'vla', 'timeout_s': 120}
    execution = config['parameters']['runtime']['execution']
    if egl_device_ids is None:
        # robosuite validates MUJOCO_EGL_DEVICE_ID against the physical IDs in
        # CUDA_VISIBLE_DEVICES before it initializes EGL. Keep both bindings
        # identical for ordinary numeric GPU IDs. Virtual GPU mappings are
        # handled by the unified evaluator after this configuration is built.
        egl_device_ids = [str(d) for d in worker_devices] if all(
            str(d).isdigit() for d in worker_devices
        ) else []
    execution.update(backend='process', max_workers=workers,
        egl_device_ids=list(egl_device_ids),
        worker_devices=list(worker_devices),
        worker_contexts=['robotenon_benchmark_support.execution:native_worker_context'],
        batch_services=[{'name': 'vla', 'factory': 'robotenon.models.vla:VLAProvider',
            'params': {'factory': old_params['factory'], 'params': old_params.get('params', {}),
                       'interface': interface},
            'devices': list(model_devices), 'mode': inference_mode, 'max_batch_size': max_batch_size,
            'max_wait_ms': max_wait_ms}])
    from robotenon.infra.params import Params
    from robotenon.runtime.execution import ExecutionOptions
    ExecutionOptions.from_params(Params(config['parameters']))
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--policy-factory", help="Defaults to robotenon_<benchmark>.vla:create")
    parser.add_argument("--policy-params", default="{}")
    parser.add_argument("--policy-interface", choices=["callable", "vla"], default="vla")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--worker-devices", help="One physical GPU per worker, e.g. 0,0,1,1")
    parser.add_argument("--egl-device-ids", help="Optional per-worker EGL device IDs; defaults to numeric worker devices")
    parser.add_argument("--model-devices", help="Model replica GPUs, e.g. 0,1; omitted uses one CPU/default-device replica")
    parser.add_argument("--inference-mode", choices=["single", "batch"], default="batch")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--batch-wait-ms", type=float, default=5)
    parser.add_argument("--batch-interface", choices=["batch", "legacy"], help="Enable shared inference; legacy uses one policy object per session")
    args = parser.parse_args()
    profile = json.loads(args.profile.read_text())
    if profile.get("mode") == "live":
        parser.error("live profiles require robotenon_benchmark_support.evaluate and a backend SPEC")
    if not args.policy_factory and args.policy_interface != "vla":
        parser.error("--policy-interface callable requires --policy-factory")
    factory = args.policy_factory or f"robotenon_{profile['benchmark_id'].removesuffix('_benchmark')}.vla:create"
    if not args.policy_factory and args.batch_interface == "batch":
        factory = factory.removesuffix(":create") + ":create_batch"
    config = application_config(profile, policy_factory=factory,
                                policy_params=json.loads(args.policy_params), policy_interface=args.policy_interface)
    if args.batch_interface:
        if args.policy_interface != 'vla':
            parser.error('shared batch configuration requires --policy-interface vla')
        enable_batch_execution(config, workers=args.workers,
            worker_devices=args.worker_devices.split(',') if args.worker_devices else [],
            model_devices=args.model_devices.split(',') if args.model_devices else [],
            max_batch_size=args.batch_size, max_wait_ms=args.batch_wait_ms,
            interface=args.batch_interface, inference_mode=args.inference_mode,
            egl_device_ids=args.egl_device_ids.split(",") if args.egl_device_ids else None)
    elif args.workers != 1 or args.worker_devices or args.model_devices:
        parser.error('parallel options require --batch-interface')
    write_application_config(config, args.output)


if __name__ == "__main__":
    main()
