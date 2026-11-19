import asyncio
import importlib
import json
from pathlib import Path

import pytest
from benchmark_fixtures import VLA_EVENTS
from robotenon_benchmark_support.configure import (
    application_config,
    write_application_config,
)

from robotenon.runtime.config import load_application_config
from robotenon.runtime.runner import EpisodeRequest, run_episode

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / "docs/benchmark-catalog.json").read_text())


@pytest.mark.parametrize("entry", CATALOG, ids=lambda e: e["id"])
def test_extension_layout_and_profiles_generate_graph(entry, monkeypatch, tmp_path):
    extension = ROOT / entry["path"]
    module_name = "robotenon_" + entry["id"]
    package = extension / "src" / module_name
    assert {"benchmark.py", "benchmark_plugin.py", "evaluate.py", "vla.py", "_internal"} <= {p.name for p in package.iterdir()}
    assert not (package / "native").exists()
    source = json.loads((extension / "benchmark-source.json").read_text())
    assert source["class"] == entry["class"]
    assert all((extension / item["target"]).is_file() for item in source["files"])
    assert all("/native/" not in item["target"] for item in source["files"])
    assert set(entry["profiles"]) == {str(Path(entry["path"]) / "configs" / p.name) for p in (extension / "configs").glob("*.json")}
    monkeypatch.syspath_prepend(str(extension / "src"))
    module = importlib.import_module(module_name + ".vla")
    fake = module.create()
    fake.start_episode({"task": {}})
    assert fake.act({"task_description": "test"})["actions"]
    fake.end_episode({"metrics": {"success": False}, "steps": 1})
    fake.close()
    profile = json.loads((ROOT / entry["default_profile"]).read_text())
    config = application_config(profile, policy_factory=module_name + ".vla:create", policy_interface="vla")
    path = tmp_path / (entry["id"] + ".json")
    write_application_config(config, path)
    loaded = load_application_config(path)
    graph = loaded.bundle.config["graphs"][0]
    assert [n["id"] for n in graph["nodes"]] == ["reset", "policy", "execute", "observe", "terminal"]
    assert config["environments"][0]["name"] == entry["registry"]


@pytest.mark.parametrize('factory,failed', [('create_recording_vla',False),('create_failing_vla',True),('fake',False)])
def test_graph_vla_lifecycle_and_drop_in_fake(tmp_path, factory, failed):
    VLA_EVENTS.clear()
    cfg=application_config({'benchmark_id':'contract_benchmark','max_steps':3},
        policy_factory='robotenon.benchmarks.fake_vla:create' if factory=='fake' else 'benchmark_fixtures:'+factory,
        policy_params={'action':{'actions':[1.0]}} if factory=='fake' else {},policy_interface='vla')
    for key in ('plugin_environments','plugin_bodies','plugin_task_adapters'):cfg[key]=['benchmark_fixtures']
    cfg['safety_state']={'backend':'memory','require_shared':False}
    path=tmp_path/'config.json';write_application_config(cfg, path)
    result=asyncio.run(run_episode(EpisodeRequest(config=path,output=tmp_path/'result.json')))
    if failed:
        assert result['graph_status'] != 'completed'
        assert len([e for e in VLA_EVENTS if e[0]=='end'])==1
        assert next(e[1] for e in VLA_EVENTS if e[0]=='end')['status']=='aborted'
    else:
        assert result['graph_status']=='completed',result
        assert result['outputs']['native_metrics']=={'success':True,'completed_subtasks':3}
        if factory!='fake':
            assert [e[0] for e in VLA_EVENTS]==['start','act','act','act','end','close']
            assert VLA_EVENTS[0][1]['task']['episode_idx']==0
            assert VLA_EVENTS[-2][1]['steps']==3
            assert VLA_EVENTS[-2][1]['metrics']['success'] is True
            assert VLA_EVENTS[-2][1]['elapsed_sec'] >= 0


def test_native_cli_treats_safety_stop_as_failure_but_task_failure_as_valid():
    from robotenon.benchmarks.run import experiment_failed
    report={'num_errors':0,'task_adapters':{'test':{'episodes':[
        {'result':{'graph_status':'safety_stop'},'task_success':False}]}}}
    assert experiment_failed(report)
    report['task_adapters']['test']['episodes'][0]['result']['graph_status']='completed'
    assert not experiment_failed(report)


def test_vla_restarts_for_each_episode(tmp_path):
    from benchmark_fixtures import ContractBenchmark

    from robotenon.benchmarks.integration import task_manifest
    from robotenon.runtime.experiment import ExperimentRequest, run_experiment
    VLA_EVENTS.clear()
    cfg=application_config({'benchmark_id':'contract_benchmark','max_steps':3,'episodes_per_task':2},
        policy_factory='benchmark_fixtures:create_recording_vla',policy_interface='vla')
    for key in ('plugin_environments','plugin_bodies','plugin_task_adapters'):cfg[key]=['benchmark_fixtures']
    cfg['task_adapters'][0]['params'].pop('vla_factory', None)
    cfg['task_adapters'][0]['params']['manifest']=task_manifest(ContractBenchmark())
    cfg['safety_state']={'backend':'memory','require_shared':False}
    cfg['parameters']['runtime']['execution']={'backend':'in_process','max_workers':1,'memory_mode':'disabled'}
    path=tmp_path/'config.json';path.write_text(json.dumps(cfg))
    result=asyncio.run(run_experiment(ExperimentRequest(config=path,output_dir=tmp_path/'runs',run_id='vla')))
    assert result['num_errors']==0
    assert result['num_episodes']==4
    assert [e[0] for e in VLA_EVENTS].count('start')==4
    assert [e[0] for e in VLA_EVENTS].count('end')==4
