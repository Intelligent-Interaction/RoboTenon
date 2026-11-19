from robotenon.extensions.datasets import DatasetBuilder
from robotenon.extensions.experiment import freeze_experiment
from robotenon.extensions.memory import MemoryStore
from robotenon.schemas import ExperimentSpec, MemoryRecord, RunManifest, Task
from robotenon.extensions.skills import SkillRegistry
from robotenon.infra.visualization import LiveGateway
from robotenon.web.portal import PortalService


def test_parameter_freeze_memory_skill_and_dataset(tmp_path):
    spec = ExperimentSpec(experiment_id="e", task=Task(task_id="t", instruction="do"), environment_id="fake",
                          parameters={"x": 1})
    frozen = freeze_experiment(spec, defaults={"y": 2}, overrides={"x": 3})
    assert frozen.parameters.values == {"y": 2, "x": 3}
    assert len(frozen.content_hash) == 64

    memory = MemoryStore(tmp_path / "memory.sqlite3")
    memory.add(MemoryRecord(task_id="t", summary="done", outcome="success"))
    assert len(memory.search(task_id="t", outcome="success")) == 1
    memory.close()

    registry = SkillRegistry()
    from robotenon.schemas import SkillDefinition
    registry.register(SkillDefinition(skill_id="pick", name="Pick", description="pick", version="1", entrypoint="x:y"))
    assert registry.heads()[0].skill_id == "pick"

    episodes = tmp_path / "episodes" / "a"
    episodes.mkdir(parents=True)
    manifest = RunManifest(run_id=__import__("uuid").uuid4(), session_id=__import__("uuid").uuid4(), task_id="t",
                           environment_id="fake", started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                           status="succeeded", success=True)
    (episodes / "manifest.json").write_text(manifest.model_dump_json())
    snapshot = DatasetBuilder(tmp_path / "episodes", tmp_path / "datasets").build(dataset_id="d", version="1")
    assert snapshot.source_run_ids == [manifest.run_id]

    portal = PortalService(LiveGateway())
    assert portal.grafana_url("http://grafana", "main", variables={"run_id": "r1"}) == "http://grafana/d/main?var-run_id=r1"


def test_grafana_variables_match_dashboard_dimensions():
    variables = LiveGateway().grafana_variables(environment_id="lab-a", metric_group="actions", action_type="gripper")
    assert variables == {"environment": "lab-a", "metric_group": "actions", "action_type": "gripper"}
