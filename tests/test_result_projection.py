from copy import deepcopy

import pytest

from robotenon.runtime.result_projection import (
    project_saved_episode, project_saved_summary, require_episode_outputs,
)
from robotenon.workflow.definition import GraphDefinition


def example(success=True):
    bindings = {'g': {key: f'nodes.renamed_checker.outputs.{key}'
                       for key in ('success', 'reason', 'env_steps')}}
    record = {'status': 'completed', 'task_success': False, 'env_steps': 0, 'reason': None,
              'result': {'graph_id': 'g', 'graph_status': 'completed', 'outputs': {},
                         'context': {'root_scope': 'root', 'node_records': {
                             'root': {'renamed_checker': {'output': {
                                 'success': success, 'reason': 'environment_success' if success else 'horizon',
                                 'env_steps': 119}}},
                             'other': {'renamed_checker': {'output': {'success': not success}}}}}}}
    return record, bindings


@pytest.mark.parametrize('success', [True, False])
def test_recovery_uses_explicit_scoped_signal_and_preserves_source(success):
    record, bindings = example(success)
    original = deepcopy(record)
    fixed = project_saved_episode(record, bindings)
    assert fixed['task_success'] is success
    assert fixed['env_steps'] == 119
    assert fixed['result']['outputs']['success'] is success
    assert fixed['result']['output_recovery']['bindings'] == bindings['g']
    assert record == original
    assert project_saved_episode(fixed, bindings) == fixed


@pytest.mark.parametrize('change', ['error', 'cancelled', 'running', 'outputs', 'no_opt_in'])
def test_recovery_does_not_reinterpret_unaffected_results(change):
    record, bindings = example()
    if change == 'outputs': record['result']['outputs'] = {'success': False}
    elif change == 'no_opt_in': bindings = {}
    else: record['status'] = change
    assert project_saved_episode(record, bindings) is record


def test_recovery_rejects_non_boolean_signal():
    record, bindings = example('true')
    with pytest.raises(ValueError, match='boolean'):
        project_saved_episode(record, bindings)


def test_summary_recovery_counts_completed_only_and_handles_later_results():
    full, bindings = example()
    compact = deepcopy(full); compact['result'].pop('context')
    summary = {'num_episodes': 2, 'successes': 0, 'task_adapters': {
        'a': {'episodes': [compact, {'status': 'error', 'task_success': False}]}}}
    fixed = project_saved_summary(summary, bindings, lambda r: full)
    assert fixed['successes'] == 1
    assert fixed['episode_success_rate'] == .5
    assert fixed['task_adapters']['a']['episodes'][0]['env_steps'] == 119
    assert summary['successes'] == 0
    repaired = project_saved_episode(full, bindings)
    assert project_saved_summary(summary, bindings, lambda r: repaired) == fixed


def test_missing_success_mapping_rejected_before_episode_runs():
    graph = GraphDefinition.model_validate({'id': 'g', 'entry': 'n', 'nodes': [
        {'id': 'n', 'type': 'system', 'operation': 'env.reset'}]})
    with pytest.raises(ValueError, match='outputs.success'):
        require_episode_outputs(graph)
    graph.outputs = {'success': 'nodes.n.outputs.success'}
    require_episode_outputs(graph)


def test_portal_recovers_list_and_detail_after_old_coordinator_overwrites_summary(tmp_path):
    import json
    from robotenon.web.experiments import ExperimentManager
    root = tmp_path / 'runs'; run = root / 'exp-test'; run.mkdir(parents=True)
    full, bindings = example()
    full.update(adapter_id='a', task_id='t', global_index=0, generation_started_at_ns=123)
    path = run / 'episode.json'; full['episode_json'] = str(path)
    path.write_text(json.dumps(full))
    compact = deepcopy(full); compact['result'].pop('context')
    summary = {'num_episodes': 1, 'successes': 0, 'task_adapters': {'a': {'episodes': [compact]}}}
    (run / 'summary.json').write_text(json.dumps(summary))
    (run / 'result_recovery.json').write_text(json.dumps({'graphs': bindings}))
    (root / 'jobs.json').write_text(json.dumps([{'job_id': 'exp-test', 'status': 'completed',
        'summary_path': str(run / 'summary.json'), 'status_path': str(run / 'status.json')}]))
    manager = ExperimentManager(project_root=tmp_path, config_root=tmp_path / 'configs', output_root=root)
    for i in range(2):
        if i: (run / 'summary.json').write_text(json.dumps(summary) + '\n')
        assert manager.list_jobs()[0]['summary']['successes'] == 1
        assert manager.list_episodes('exp-test', include_runtime=False)[0]['task_success'] is True
        _, record = manager._episode_record('exp-test', adapter_id='a', global_index=0)
        assert record['env_steps'] == 119
        assert record['result']['outputs']['success'] is True
    # A different generation must not supply evidence for the older summary.
    full['generation_started_at_ns'] = 456
    path.write_text(json.dumps(full))
    assert manager.list_jobs()[0]['summary']['successes'] == 0


def test_experiment_rejects_missing_mapping_before_building_adapters(tmp_path, monkeypatch):
    import asyncio
    import json
    from test_execution_backends import make_config
    from robotenon.runtime.experiment import ExperimentRequest, run_experiment
    path = make_config(tmp_path)
    config = json.loads(path.read_text()); config['graphs'][0].pop('outputs')
    path.write_text(json.dumps(config))
    def forbidden(*args, **kwargs):
        pytest.fail('must reject before allocating experiment resources')
    monkeypatch.setattr('robotenon.runtime.experiment.build_task_adapters', forbidden)
    with pytest.raises(ValueError, match='outputs.success'):
        asyncio.run(run_experiment(ExperimentRequest(config=path)))
