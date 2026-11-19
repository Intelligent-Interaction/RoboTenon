"""Compose an ordinary framework system config with a benchmark-owned protocol."""
from copy import deepcopy

from robotenon.infra.params import load_config_bundle, merge_parameter_values
from robotenon.runtime.batch_services import BatchServiceConfig


def load_system(path):
    system = load_config_bundle(path).config
    owned = {'environments', 'plugin_environments', 'bodies', 'plugin_bodies',
             'task_adapters', 'plugin_task_adapters', 'environment', 'task',
             'benchmark_protocol', 'execution', 'observation_cache', 'safety_state'}
    unexpected = system.keys() & owned
    if unexpected:
        raise ValueError(f'system config cannot override benchmark sections: {sorted(unexpected)}')
    if not system.get('graphs'):
        raise ValueError('system config requires graphs')
    if set(system.get('runtime', {})) - {'graph_id', 'monitor_enabled'}:
        raise ValueError('system runtime may only select graph_id and monitor_enabled; use execution-config for resources')
    if system.get('model', {}).get('vla_tool', 'policy') != 'policy':
        raise ValueError('the managed VLA Tool has id policy')
    return system


def compose_system(config, system):
    """Reuse framework manifests; never replace Environment, Body or TaskAdapter."""
    config = deepcopy(config)
    for name in ('modules', 'graphs', 'monitors'):
        if name in system:
            config[name] = deepcopy(system[name])
    for name in ('plugin_modules', 'plugin_tools', 'plugin_monitors'):
        config[name] = list(dict.fromkeys(config.get(name, []) + system.get(name, [])))
    for tool in system.get('tools', []):
        if tool['id'] in {t['id'] for t in config['tools']}:
            raise ValueError(f'duplicate/reserved Tool id: {tool["id"]}')
        config['tools'].append(deepcopy(tool))
    manifest_fields = {'config_version', 'modules', 'graphs', 'tools', 'monitors',
                       'plugin_modules', 'plugin_tools', 'plugin_monitors'}
    config['parameters'] = merge_parameter_values(config['parameters'],
        {name: system[name] for name in system.keys() - manifest_fields})
    graph_id = system.get('runtime', {}).get('graph_id', system['graphs'][0]['id'])
    if graph_id not in {g['id'] for g in system['graphs']}:
        raise ValueError(f'unknown system graph_id: {graph_id}')
    config['parameters']['runtime']['graph_id'] = graph_id
    return config


def additional_services(values):
    result = deepcopy(values)
    names = {'vla'}
    for item in result:
        service = BatchServiceConfig.parse(item)
        if service.name in names:
            raise ValueError(f'duplicate/reserved service name: {service.name}')
        names.add(service.name)
    return result


def validate_routes(config, names):
    for tool in config['tools']:
        service = tool.get('params', {}).get('service')
        if service and service not in names:
            raise ValueError(f'Tool {tool["id"]} refers to missing service {service}')
