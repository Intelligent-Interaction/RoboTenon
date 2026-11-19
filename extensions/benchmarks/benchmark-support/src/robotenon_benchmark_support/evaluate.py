"""Evaluate one backend or a framework system Graph against one benchmark protocol."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from copy import deepcopy

from .backend_loader import resolve, load, resolve_spec, validate_spec
from .configure import application_config, enable_batch_execution, write_application_config


def profile_path(benchmark, value):
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    if candidate.suffix or len(candidate.parts) != 1:
        raise FileNotFoundError(value)
    # Resolve without importing the simulator package in the model environment.
    root = Path(__file__).resolve().parents[3] / benchmark
    candidates = [root / 'configs' / (value + '.json')]
    spec = importlib.util.find_spec('robotenon_' + benchmark)
    if spec and spec.submodule_search_locations:
        for location in spec.submodule_search_locations:
            candidates.append(Path(location).parents[1] / 'configs' / (value + '.json'))
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError(f'profile {value!r} not found for {benchmark}')


def build_config(profile, declaration, endpoints, args):
    from .backend_loader import validate_spec
    validate_spec(declaration, args.benchmark, args.batch_size)
    devices = args.model_devices.split(',') if args.model_devices else [None]
    workers_per_device = getattr(args, "workers_per_device", None)
    workers_per_device = workers_per_device if workers_per_device is not None else (args.workers_per_gpu if args.workers_per_gpu is not None else 1)
    workers = len(devices) * workers_per_device
    # Interleave GPUs so even small subsets can use all model replicas.
    bindings = [d for _ in range(workers_per_device) for d in devices]
    routes = [endpoints[i % len(devices)] for i in range(workers)]
    profiles = profile if isinstance(profile, list) else [profile]
    profile = profiles[0]
    config = application_config(profile, policy_factory='robotenon_benchmark_support.backend_loader:create')
    if len(profiles) > 1:
        prototype = config['task_adapters'][0]
        adapters = []
        for index, selected in enumerate(profiles):
            task_params = {
                key: deepcopy(selected[key])
                for key in ("params", "episodes_per_task", "max_steps", "tasks", "max_tasks", "render", "throughput_mode")
                if key in selected
            }
            task_params['vla_factory'] = prototype['params']['vla_factory']
            adapter_id = selected.get('name') or selected.get('params', {}).get('suite') or f'profile-{index}'
            adapters.append({
                'id': adapter_id,
                'name': prototype['name'],
                'params': task_params,
            })
        if len({item['id'] for item in adapters}) != len(adapters):
            raise ValueError('combined profiles require unique names or suite values')
        config['task_adapters'] = adapters
        config['parameters']['benchmark_protocol'] = {'profiles': deepcopy(profiles)}
    # Native simulators execute synchronous physics and rendering calls in the
    # Worker event loop.  Under heavy host contention one such call can exceed
    # the Body service's normal heartbeat interval even though the simulator is
    # healthy.  Keep the fail-safe beyond the enclosing episode timeout; the
    # process backend remains the authoritative watchdog for a wedged Worker.
    for body in config.get('bodies', []):
        body.setdefault('params', {})['heartbeat_ttl_s'] = max(
            120.0, float(args.task_timeout) + 60.0)
    # A simulator action is guarded by the enclosing process task timeout.
    # Keep Body's transport deadline beyond that watchdog: at high parallelism
    # a freshly produced action may spend minutes behind durable checkpoint I/O
    # before Body receives it, which is latency rather than a stuck simulator.
    # The process backend still kills a genuinely wedged episode at
    # ``task_timeout``.
    config['parameters']['policy']['action_timeout_ms'] = max(
        config['parameters']['policy']['action_timeout_ms'],
        math.ceil((float(args.task_timeout) + 60.0) * 1000.0),
    )
    # This neutral module has no model imports or observation negotiation. The
    # resolved declaration, not a backend import, is passed into simulation.
    enable_batch_execution(config, workers=workers,
        worker_devices=bindings if devices != [None] else [],
        model_devices=[], max_batch_size=args.batch_size, max_wait_ms=args.wait_ms)
    config['parameters']['runtime']['trace_mode'] = getattr(args, 'trace_mode', 'summary')
    config['parameters']['runtime']['context_history_limit'] = args.context_history_limit
    config['parameters']['runtime']['context_outputs_limit'] = args.context_outputs_limit
    execution = config['parameters']['runtime']['execution']
    execution['virtual_gpu_map'] = getattr(args, 'virtual_gpu_map', {})
    if execution['virtual_gpu_map']:
        from robotenon.runtime.devices import physical_device
        physical = [physical_device(d, execution['virtual_gpu_map']) for d in bindings]
        # Workers expose one resolved physical GPU each, which is device zero
        # inside that process after CUDA_VISIBLE_DEVICES remapping.
        execution['egl_device_ids'] = (
            ['0'] * len(physical)
            if all(d is not None and d.isdigit() for d in physical)
            else []
        )
    if getattr(args, 'egl_device_ids', ''):
        execution['egl_device_ids'] = args.egl_device_ids.split(',')
    execution.update(batch_services=[], external_batch_services={'vla': routes},
        worker_threads=max(1, int(os.environ.get('OMP_NUM_THREADS', '2'))),
        enumeration_parallelism=args.enumeration_parallelism,
        max_retries=args.max_retries, startup_timeout_s=args.worker_startup_timeout,
        worker_startup_parallelism=min(args.startup_parallelism, workers),
        native_reset_parallelism=min(args.startup_parallelism, workers),
        native_reset_gate=str(args.output / '.native-startup-gate'),
        task_timeout_s=args.task_timeout, shutdown_timeout_s=60,
        recycle_after_tasks=args.recycle_after_tasks)
    if args.scheduling_strategy == 'static':
        execution['backend'] = 'benchmark_static'
        execution['plugin_backends'].append('robotenon_benchmark_support.static_execution')
    for adapter in config['task_adapters']:
        adapter['params']['scheduling'] = {
            'strategy': args.scheduling_strategy,
            'shuffle': not args.no_shuffle,
            'seed': args.scheduler_seed,
            **({'scope': 'global'} if len(profiles) > 1 else {}),
        }
        adapter['params']['episode_plan_path'] = str(args.output / 'episode-plan.json')
    config['plugin_tools'].append('robotenon_benchmark_support.runtime')
    tool = config['tools'][0]
    tool['name'] = 'benchmark_backend'
    if profile.get('mode') == 'live':
        config['plugin_tools'].append('robotenon_benchmark_support.live_runtime')
        tool['name'] = 'benchmark_live_backend'
        tool['params']['live_config'] = {k: profile[k] for k in ('hz', 'pace', 'wait_first_action') if k in profile}
    history_mode = getattr(args, 'history_cache', 'auto')
    history = declaration['history']
    history_cache = history_mode == 'on' or (history_mode == 'auto' and
        history['length'] > 1 and declaration['replan_steps'] <= (history['length']-1)*history['interval'])
    tool['params'].update(declaration=declaration, endpoint=endpoints[0], timeout_s=args.inference_timeout,
                          history_cache=history_cache)
    if getattr(args, 'system_config', None):
        if profile.get('mode') == 'live':
            raise ValueError('custom system Graph currently supports sync profiles only')
        from .system_config import compose_system, load_system
        config = compose_system(config, load_system(args.system_config))
    return config


async def run(args):
    from robotenon.runtime.batch_services import SharedBatchServices
    from .backend_loader import observation_params
    profile_values = args.profiles or [args.profile]
    profile_files = [profile_path(args.benchmark, value) for value in profile_values]
    profiles = [json.loads(path.read_text()) for path in profile_files]
    if any(profile['benchmark_id'] != args.benchmark + '_benchmark' for profile in profiles):
        raise ValueError('profile benchmark_id does not match --benchmark')
    original = json.loads(json.dumps(profiles if len(profiles) > 1 else profiles[0]))
    overrides = {}
    for name in ('tasks', 'max_tasks', 'episodes_per_task'):
        value = getattr(args, name)
        if value is not None:
            overrides[name] = value
            for profile in profiles:
                profile[name] = value
    # Reject unsupported protocol semantics before importing/loading a model or
    # spawning expensive GPU services. This is also checked by build_config.
    for profile in profiles:
        application_config(profile, policy_factory='robotenon_benchmark_support.backend_loader:create')
    _, raw_declaration = load(args.backend)
    model_observation_params = observation_params(args.backend)
    for profile in profiles:
        native_params = profile.setdefault('params', {})
        for name, value in model_observation_params.items():
            native_params.setdefault(name, value)
    from .system_config import additional_services, load_system, validate_routes
    system = load_system(args.system_config) if args.system_config else None
    if system and any(profile.get('mode') == 'live' for profile in profiles):
        raise ValueError('custom system Graph currently supports sync profiles only')
    extra_services = additional_services(args.additional_services)
    declaration = (
        resolve_spec(raw_declaration, args.benchmark)
        if "benchmark_overrides" in raw_declaration
        else raw_declaration
    )
    validate_spec(declaration, args.benchmark, args.batch_size)
    backend_params = json.loads(Path(args.backend_config).read_text()) if args.backend_config else {}
    if not isinstance(backend_params, dict):
        raise ValueError('backend-config must contain a JSON object')
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / 'runs' / args.run_id).exists():
        raise FileExistsError('run output already exists; choose a new output or run-id')
    devices = args.model_devices.split(',') if args.model_devices else []
    service_config = [{
        'name': 'vla', 'factory': 'robotenon.models.vla:VLAProvider',
        'params': {'factory': 'robotenon_benchmark_support.backend_loader:create', 'interface': 'batch',
            'params': {'backend': args.backend, 'backend_params': backend_params, 'declaration': declaration}},
        'devices': devices, 'mode': 'batch', 'max_batch_size': args.batch_size,
        'max_wait_ms': args.wait_ms, 'max_pending': max(256, args.workers_per_gpu * 4, args.batch_size)}]
    service_config.extend(extra_services)
    selected_profiles = profiles if len(profiles) > 1 else profiles[0]
    preview = build_config(selected_profiles, declaration, ['pending'] * max(1, len(devices)), args)
    validate_routes(preview, {s['name'] for s in service_config} | set(args.external_services))
    from robotenon.runtime.config import load_application_config
    load_application_config(preview)  # Validate Graph edges and manifests before loading weights.
    metadata = {'optimizations': {'trace_mode': preview['parameters']['runtime']['trace_mode'],
        'history_cache_enabled': preview['tools'][0]['params']['history_cache'],
        'context_history_limit': preview['parameters']['runtime']['context_history_limit'],
        'context_outputs_limit': preview['parameters']['runtime']['context_outputs_limit'],
        'action_timeout_ms': preview['parameters']['policy']['action_timeout_ms']},
        'system_config': system, 'model_services': service_config, 'benchmark': args.benchmark,
        'profile_path': str(profile_files[0]) if len(profile_files) == 1 else None,
        'profile_paths': [str(path) for path in profile_files],
        'protocol_scope': 'overridden' if overrides else ('combined' if len(profiles) > 1 else ('builtin' if args.profile == profile_files[0].stem else 'external')),
        'original_profile': original, 'effective_profile': selected_profiles, 'overrides': overrides,
        'selection': 'combined' if len(profiles) > 1 else ('subset' if any(k in profiles[0] for k in ('tasks', 'max_tasks')) or profiles[0].get('params', {}).get('tasks') else 'profile'),
        'backend': args.backend, 'backend_config': backend_params, 'backend_spec': declaration,
        'model_observation_params': model_observation_params,
        'rng_mode': backend_params.get('rng_mode', 'backend_defined'),
        'execution': {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        'scheduling': {'strategy': args.scheduling_strategy, 'shuffle': not args.no_shuffle, 'seed': args.scheduler_seed},
        'model_python': sys.executable, 'status': 'starting'}
    def record():
        (args.output / 'protocol.json').write_text(json.dumps(metadata, indent=2) + '\n')
    record()
    (args.output / 'model-service.json').write_text(json.dumps(service_config, indent=2) + '\n')
    local_runtime = Path(os.environ.get('ROBOTENON_LOCAL_RUNTIME_ROOT',
        '/tmp/robotenon-benchmark')).expanduser() / args.run_id
    metadata['local_runtime'] = str(local_runtime)
    record()
    services = SharedBatchServices(service_config, local_runtime / 'batch-metrics',
        startup_timeout_s=args.model_startup_timeout, shutdown_timeout_s=60,
        virtual_gpu_map=getattr(args, 'virtual_gpu_map', {}))
    services.validate_worker_devices(devices)
    process = None
    started = time.perf_counter()
    try:
        await services.start()
        endpoints = [p for _, p in services.endpoints['vla']]
        config = build_config(selected_profiles, declaration, endpoints, args)
        execution = config['parameters']['runtime']['execution']
        devices_for_workers = [d for _ in range(args.workers_per_gpu) for d in (devices or [None])]
        routes = {name: [] for name in services.endpoints}
        for index, device in enumerate(devices_for_workers):
            for name, endpoint in services.for_worker(index, device).items():
                routes[name].append(endpoint)
        routes.update(args.external_services)
        execution['external_batch_services'] = routes
        path = args.output / 'config.json'
        write_application_config(config, path)
        environment = dict(os.environ)
        # No model site-packages are injected into the simulation environment.
        if profiles[0].get('render', 'gpu') == 'gpu':
            environment.update(MUJOCO_GL='egl', PYOPENGL_PLATFORM='egl')
        environment.pop('MUJOCO_EGL_DEVICE_ID', None)
        metadata['status'] = 'running'; record()
        with (args.output / 'simulation.log').open('w') as log:
            process = await asyncio.create_subprocess_exec(args.sim_python, '-m',
                'robotenon_benchmark_support.run', '--config', str(path),
                '--output-dir', str(args.output / 'runs'), '--run-id', args.run_id,
                env=environment, stdout=log, stderr=asyncio.subprocess.STDOUT)
            simulation_wait = asyncio.create_task(process.wait())
            while not simulation_wait.done():
                services.check_health()
                try:
                    await asyncio.wait_for(asyncio.shield(simulation_wait), 2)
                except asyncio.TimeoutError:
                    pass
            code = await simulation_wait
        if code:
            raise RuntimeError(f'simulation exited with {code}; see {args.output / "simulation.log"}')
        result_root = args.output / 'runs' / args.run_id
        status = json.loads((result_root / 'status.json').read_text())
        summary = json.loads((result_root / 'summary.json').read_text())
        # Task adapter enumeration defines the denominator, never model identity.
        expected = status['total_episodes']
        if summary.get('num_episodes') != expected or summary.get('num_errors') or summary.get('num_cancelled'):
            raise RuntimeError('evaluation incomplete or contains episode errors')
        metadata.update(status='completed', expected_episodes=expected, summary=str(result_root / 'summary.json'))
        return summary
    except BaseException as error:
        metadata.update(status='failed', error=str(error))
        raise
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 60)
            except asyncio.TimeoutError:
                process.kill(); await process.wait()
        # The simulation coordinator owns multiprocessing Workers which each
        # create a separate process group.  If the coordinator is interrupted,
        # those groups can otherwise survive as orphans and retain EGL/CUDA
        # contexts.  The registry stores PID start identities, so this cleanup
        # cannot target an unrelated process after PID reuse.
        from robotenon.runtime.processes import terminate_owned
        await asyncio.to_thread(
            terminate_owned,
            args.output / 'runs' / args.run_id / '.processes',
            timeout_s=10.0,
        )
        await services.close()
        metadata['wall_sec'] = time.perf_counter() - started
        record()


def parser():
    p = argparse.ArgumentParser(prog="Tenon evaluate", description=__doc__)
    p.add_argument('--benchmark', required=True)
    profiles = p.add_mutually_exclusive_group(required=True)
    profiles.add_argument('--profile')
    profiles.add_argument('--profiles', nargs='+')
    p.add_argument('--backend')
    p.add_argument('--system-config', type=Path)
    p.add_argument('--execution-config', type=Path)
    p.add_argument('--backend-config')
    p.add_argument('--model-python', default=sys.executable)
    p.add_argument('--sim-python', default=sys.executable)
    p.add_argument('--model-devices', default='')
    p.add_argument('--workers-per-gpu', type=int, default=None)
    p.add_argument('--workers-per-device', type=int, default=None)
    p.add_argument('--virtual-gpu-map', type=json.loads, default={})
    p.add_argument('--egl-device-ids', default='', help='Explicit physical EGL indices, one per Worker')
    p.add_argument('--batch-size', type=int, default=1)
    p.add_argument('--wait-ms', type=float, default=50)
    p.add_argument('--trace-mode', choices=['full', 'summary'], default='summary')
    p.add_argument('--history-cache', choices=['auto', 'on', 'off'], default='auto')
    p.add_argument('--context-history-limit', type=int, default=64,
                   help='Recent diagnostic Graph history records retained in each checkpoint')
    p.add_argument('--context-outputs-limit', type=int, default=64,
                   help='Recent path-scoped node outputs retained in each checkpoint')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--run-id', default='evaluation')
    p.add_argument('--scheduling-strategy', choices=['global_queue', 'static'], default='global_queue')
    p.add_argument('--scheduler-seed', type=int, default=7)
    p.add_argument('--no-shuffle', action='store_true')
    p.add_argument('--tasks', nargs='+')
    p.add_argument('--max-tasks', type=int)
    p.add_argument('--episodes-per-task', type=int)
    p.add_argument('--startup-parallelism', type=int, default=8)
    p.add_argument('--enumeration-parallelism', type=int, default=1)
    p.add_argument('--max-retries', type=int, default=1)
    p.add_argument('--task-timeout', type=float, default=14400)
    p.add_argument('--inference-timeout', type=float, default=600)
    p.add_argument('--model-startup-timeout', type=float, default=1800)
    p.add_argument('--worker-startup-timeout', type=float, default=1800)
    p.add_argument('--recycle-after-tasks', type=int, default=1000)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = parser()
    preliminary, _ = p.parse_known_args(argv)
    defaults = {}
    if preliminary.execution_config:
        source = preliminary.execution_config.resolve()
        defaults = json.loads(source.read_text())
        allowed = {a.dest for a in p._actions} - {'help', 'benchmark', 'profile', 'profiles', 'output', 'execution_config', 'system_config', 'tasks', 'max_tasks', 'episodes_per_task'}
        unknown = defaults.keys() - allowed - {'batch_services', 'external_batch_services'}
        if unknown:
            p.error(f'unknown execution config keys: {sorted(unknown)}')
        for key in ('backend', 'backend_config'):
            if defaults.get(key) and (key == 'backend_config' or defaults[key].endswith('.py')):
                defaults[key] = str((source.parent / defaults[key]).resolve())
        p.set_defaults(**{k: v for k, v in defaults.items() if k in allowed})
    args = p.parse_args(argv)
    from robotenon.runtime.devices import validate_gpu_map, physical_device
    try:
        args.virtual_gpu_map = validate_gpu_map(args.virtual_gpu_map)
        for device in args.model_devices.split(',') if args.model_devices else []:
            physical_device(device, args.virtual_gpu_map)
    except ValueError as error:
        p.error(str(error))
    if args.workers_per_device is not None and args.workers_per_gpu is not None and args.workers_per_device != args.workers_per_gpu:
        p.error('workers-per-device and workers-per-gpu must agree')
    args.workers_per_gpu = args.workers_per_device if args.workers_per_device is not None else (args.workers_per_gpu if args.workers_per_gpu is not None else 1)
    if args.trace_mode not in {'full', 'summary'} or args.history_cache not in {'auto', 'on', 'off'}:
        p.error('invalid trace_mode or history_cache in execution config')
    args.additional_services = defaults.get('batch_services', [])
    args.external_services = defaults.get('external_batch_services', {})
    if 'vla' in args.external_services or set(args.external_services) & {s['name'] for s in args.additional_services}:
        p.error('external services must not duplicate managed services')
    for name, endpoints in args.external_services.items():
        workers = max(1, len(args.model_devices.split(',')) if args.model_devices else 1) * args.workers_per_gpu
        if not isinstance(endpoints, list) or len(endpoints) not in (1, workers) or not all(isinstance(e, str) and e for e in endpoints):
            p.error(f'external service {name} requires one endpoint or one per Worker')
        if len(endpoints) == 1:
            args.external_services[name] = endpoints * workers
    if not args.backend:
        p.error('--backend is required (or set backend in --execution-config)')
    # Re-execute the coordinator in the chosen model environment; spawned
    # SharedBatchServices inherit that executable. Only simulation uses sim-python.
    import shutil
    args.model_python = shutil.which(args.model_python) or args.model_python
    if os.environ.get('ROBOTENON_MODEL_ENV_ACTIVE') != args.model_python and os.path.abspath(args.model_python) != os.path.abspath(sys.executable):
        command = [args.model_python, '-m', 'robotenon_benchmark_support.evaluate', *argv,
                   '--model-python', args.model_python, '--sim-python', args.sim_python]
        raise SystemExit(subprocess.call(command, env=dict(os.environ, ROBOTENON_MODEL_ENV_ACTIVE=args.model_python)))
    if min(args.workers_per_gpu, args.batch_size, args.startup_parallelism,
           args.enumeration_parallelism,
           args.recycle_after_tasks, args.context_history_limit,
           args.context_outputs_limit) < 1 or args.max_retries < 0:
        p.error('worker, batch, startup and recycle counts must be positive; retries nonnegative')
    if args.wait_ms < 0 or min(args.task_timeout, args.inference_timeout, args.model_startup_timeout, args.worker_startup_timeout) <= 0:
        p.error('invalid timeout')
    devices = args.model_devices.split(',') if args.model_devices else []
    if len(set(devices)) != len(devices) or any(not d.strip() for d in devices):
        p.error('model-devices must be unique nonempty device IDs')
    args.backend = resolve(args.backend)
    args.output = args.output.resolve()
    async def run_with_signals():
        import signal

        loop = asyncio.get_running_loop()
        task = asyncio.create_task(run(args))

        def cancel():
            if not task.done():
                task.cancel()

        installed = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, cancel)
                installed.append(sig)
            except NotImplementedError:
                pass
        try:
            return await task
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)

    print(json.dumps(asyncio.run(run_with_signals()), indent=2))


if __name__ == '__main__':
    main()
