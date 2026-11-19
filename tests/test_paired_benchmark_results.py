import json
from pathlib import Path
import sqlite3

import pytest

from robotenon_benchmark_support.paired_results import compare, paired_statistics, unique


def write_run(root, outcomes, *, seed=7):
    root.mkdir()
    (root / 'protocol.json').write_text(json.dumps({
        'benchmark':'example', 'effective_profile':{'seed':seed},
        'backend_config':{}, 'backend_spec':{}, 'rng_mode':'session_deterministic',
        'execution':{'run_id':'evaluation'}}))
    plan=[{'adapter_id':'example','task_id':task,'episode':episode,'seed':seed}
          for task in ('a','b') for episode in range(2)]
    (root / 'episode-plan.json').write_text(json.dumps({'episodes':plan,'expected_episodes':4}))
    directory=root/'runs/evaluation';directory.mkdir(parents=True)
    db=sqlite3.connect(directory/'episodes.sqlite3')
    db.execute('create table episodes(record_json TEXT)')
    for row, outcome in zip(plan, outcomes):
        db.execute('insert into episodes values (?)', (json.dumps({**row,'status':'completed','task_success':outcome}),))
    db.commit();db.close()


def test_partial_completed_subset_does_not_masquerade_as_paired_accuracy(tmp_path):
    write_run(tmp_path/'a',[True,True,False,False])
    write_run(tmp_path/'b',[True])
    result=compare(tmp_path/'a',tmp_path/'b')
    assert result['complete'] is False
    assert 'system_success_rate' not in result
    assert result['system_completed']==1


def test_complete_pair_uses_identity_and_reports_both_directions(tmp_path):
    write_run(tmp_path/'a',[True,True,False,False])
    write_run(tmp_path/'b',[True,False,True,True])
    result=compare(tmp_path/'a',tmp_path/'b')
    assert result['complete']
    assert result['both']==1 and result['system_only']==2 and result['baseline_only']==1
    assert result['difference']==.25
    assert result['baseline_success_rate']==.5 and result['system_success_rate']==.75
    assert result['task_cluster_bootstrap_95_interval'][0] <= 0
    assert compare(tmp_path/'a',tmp_path/'b') == result


def test_different_environment_seeds_cannot_be_called_paired(tmp_path):
    write_run(tmp_path/'a',[True]*4)
    write_run(tmp_path/'b',[True]*4,seed=8)
    with pytest.raises(ValueError,match='protocol mismatch'):
        compare(tmp_path/'a',tmp_path/'b')


def test_duplicate_identities_are_rejected():
    row={'adapter_id':'a','task_id':'t','episode':0,'seed':7}
    with pytest.raises(ValueError,match='duplicate'):
        unique([row,row])


def test_equal_success_totals_do_not_hide_changed_episode_outcomes(tmp_path):
    write_run(tmp_path/'a', [True, False, True, False])
    write_run(tmp_path/'b', [False, True, True, False])
    result = compare(tmp_path/'a', tmp_path/'b')
    assert result['difference'] == 0
    task = next(row for row in result['per_task'] if row['task'] == 'a')
    assert task['baseline_successes'] == task['system_successes'] == 1
    assert task['system_only'] == task['baseline_only'] == 1
    assert task['both'] == task['neither'] == 0
    assert result['baseline_success_retention_rate'] == .5
    assert result['baseline_failure_recovery_rate'] == .5
    for name in ('both', 'system_only', 'baseline_only', 'neither'):
        assert sum(row[name] for row in result['per_task']) == result[name]


@pytest.mark.parametrize('baseline,retention,recovery', [
    (True, .5, None),
    (False, None, .5),
])
def test_conditional_rates_have_explicit_empty_denominators(tmp_path, baseline, retention, recovery):
    write_run(tmp_path/'a', [baseline]*4)
    write_run(tmp_path/'b', [True, False, True, False])
    result = compare(tmp_path/'a', tmp_path/'b')
    assert result['baseline_success_retention_rate'] == retention
    assert result['baseline_failure_recovery_rate'] == recovery
