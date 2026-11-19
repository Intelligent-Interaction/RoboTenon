import asyncio
from types import SimpleNamespace
import numpy as np
from robotenon.benchmarks.smoke import probe, simulator_health


def test_smoke_records_action_advancement_and_reports_reset_failures():
    report = asyncio.run(probe('benchmark_fixtures:ContractBenchmark',
                              {'done_after': 10}, {'actions': [1.0]}, steps=3))
    assert report['status'] == 'passed'
    assert report['accepted_steps'] == 3
    assert report['observation_changed']
    assert report['physics_issues'] == []
    assert report['image_changed_each_step']
    assert report['success_signal_readable']
    assert report['terminal_success_matches_metrics']
    assert report['terminal_metrics']['success'] is False
    assert report['steps'][-1]['terminal'] is True
    assert report['steps'][0]['environment_info']['native_step']['done'] is False
    report = asyncio.run(probe('benchmark_fixtures:ContractBenchmark',
                              {}, {'actions': [1.0]}, task_index=999))
    assert report['status'] == 'error'
    assert report['phase'] == 'reset'
    assert report['accepted_steps'] == 0
    report = asyncio.run(probe('missing_benchmark_package:Benchmark', {}, {'actions': [1.0]}))
    assert report['status'] == 'error'
    assert report['phase'] == 'import'


def test_smoke_reports_cached_terminal_image_without_claiming_a_new_frame():
    report = asyncio.run(probe('benchmark_fixtures:ContractBenchmark',
                              {'done_after': 1}, {'actions': [1.0]}, steps=3))
    assert report['success_signal_readable']
    assert report['terminal_metrics']['success'] is True
    assert report['terminal_success_matches_metrics']
    assert report['steps'][0]['observation_refreshed'] is False
    assert not report['image_changed_each_step']


def test_smoke_does_not_ignore_nonfinite_physics_or_mujoco_warnings():
    native = SimpleNamespace(_env=SimpleNamespace(sim=SimpleNamespace(data=SimpleNamespace(
        qpos=np.array([0.]), qvel=np.array([float('nan')]), ctrl=np.array([0.]),
        warning=SimpleNamespace(number=np.array([0, 1]))))))
    issues = simulator_health(native)
    assert any('nonfinite' in item for item in issues)
    assert any('MuJoCo warnings' in item for item in issues)
