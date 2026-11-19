"""Unregistered backend factories, loaded only in the model environment."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
import types
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path


def resolve(reference):
    """Resolve file paths before changing processes; module references stay intact."""
    if reference.endswith('.py') or Path(reference).is_file():
        path = Path(reference).expanduser().resolve(strict=True)
        return str(path)
    module, separator, factory = reference.partition(':')
    if not separator or not module or not factory:
        raise ValueError('backend must be a vla.py path or module:factory')
    return reference


def load(reference):
    reference = resolve(reference)
    if reference.endswith('.py'):
        path = Path(reference)
        package = '_robotenon_backend_' + hashlib.sha256(str(path.parent).encode()).hexdigest()
        if package not in sys.modules:
            initializer = path.parent / '__init__.py'
            if initializer.is_file():
                package_spec = importlib.util.spec_from_file_location(
                    package, initializer, submodule_search_locations=[str(path.parent)])
                root = importlib.util.module_from_spec(package_spec)
                sys.modules[package] = root
                try:
                    package_spec.loader.exec_module(root)
                except BaseException:
                    sys.modules.pop(package, None)
                    raise
            else:
                root = types.ModuleType(package)
                root.__path__ = [str(path.parent)]
                root.__package__ = package
                sys.modules[package] = root
        name = package + '.' + path.stem
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(name, None)
                raise
        module, factory_name = sys.modules[name], 'create'
    else:
        name, factory_name = reference.split(':', 1)
        module = importlib.import_module(name)
    factory = getattr(module, factory_name)
    if not callable(factory):
        raise TypeError('backend factory must be callable')
    spec = dict(getattr(module, 'SPEC'))
    validate_spec(spec)
    return factory, spec


def observation_params(reference):
    """Return lightweight benchmark observation requirements for a backend."""
    reference = resolve(reference)
    load(reference)
    if reference.endswith('.py'):
        path = Path(reference)
        package = '_robotenon_backend_' + hashlib.sha256(str(path.parent).encode()).hexdigest()
        module = sys.modules[package + '.' + path.stem]
    else:
        module = importlib.import_module(reference.split(':', 1)[0])
    params = getattr(module, 'OBSERVATION_PARAMS', {})
    if not isinstance(params, Mapping):
        raise TypeError('backend OBSERVATION_PARAMS must be a mapping')
    return deepcopy(dict(params))


def resolve_spec(spec, benchmark):
    """Resolve benchmark-specific history/chunk settings before startup.

    Backends normally expose one static history and replanning contract.  A
    multi-benchmark backend may additionally provide ``benchmark_overrides``
    for protocols whose checkpoints use a different temporal contract.  The
    resolved declaration is what both the simulator runtime and model service
    receive, so they cannot silently disagree about history or action chunks.
    """
    resolved = deepcopy(spec)
    overrides = resolved.pop('benchmark_overrides', {})
    if overrides:
        if not isinstance(overrides, dict):
            raise ValueError('benchmark_overrides must be a mapping')
        selected = overrides.get(benchmark, overrides.get(benchmark + '_benchmark', {}))
        if selected:
            if not isinstance(selected, dict) or set(selected) - {'history', 'replan_steps'}:
                raise ValueError('benchmark override may only set history and replan_steps')
            resolved.update(deepcopy(selected))
    resolved['benchmark'] = benchmark
    return resolved


def validate_spec(spec, benchmark=None, batch_size=1):
    if type(spec.get('api_version')) is not int or spec['api_version'] != 1:
        raise ValueError('backend requires api_version=1')
    supported = spec.get('supported_benchmarks')
    if not isinstance(supported, list) or not supported or not all(isinstance(x, str) for x in supported):
        raise ValueError('supported_benchmarks must be a nonempty list of names')
    if benchmark and benchmark not in supported and benchmark + '_benchmark' not in supported and '*' not in supported:
        raise ValueError(f'backend does not support {benchmark}')
    if type(spec.get('supports_batch')) is not bool:
        raise ValueError('supports_batch must be boolean')
    if batch_size > 1 and not spec['supports_batch']:
        raise ValueError('backend supports_batch=False requires batch_size=1')
    candidate = resolve_spec(spec, benchmark) if benchmark and 'benchmark_overrides' in spec else spec
    history = candidate.get('history', {})
    for value in (history.get('length'), history.get('interval'), candidate.get('replan_steps')):
        if type(value) is not int or value < 1:
            raise ValueError('history length/interval and replan_steps must be positive integers')
    overrides = spec.get('benchmark_overrides', {})
    if overrides:
        if not isinstance(overrides, dict):
            raise ValueError('benchmark_overrides must be a mapping')
        for name in overrides:
            if name not in supported and name.removesuffix('_benchmark') not in supported:
                raise ValueError(f'benchmark override {name!r} is not declared as supported')
            validate_spec(resolve_spec(spec, name), name, batch_size)


def create(*, backend, backend_params, declaration):
    factory, spec = load(backend)
    benchmark = declaration.get('benchmark')
    expected = resolve_spec(spec, benchmark) if benchmark else spec
    if expected != declaration:
        raise ValueError('backend SPEC changed between validation and model startup')
    model = factory(**backend_params)
    try:
        for method in ('open_session', 'act_batch', 'close_session', 'close'):
            if not callable(getattr(model, method, None)):
                raise TypeError(f'backend must implement {method}')
    except BaseException:
        if callable(getattr(model, 'close', None)):
            model.close()
        raise
    return model
