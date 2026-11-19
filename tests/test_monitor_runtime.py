import asyncio
from pathlib import Path
from typing import ClassVar
from robotenon.envs.base.fake import FakeEnvironmentAdapter
from robotenon.monitors import register_monitor
from robotenon.runtime.config import MonitorsConfig, load_application_config
from robotenon.runtime.factory import build_monitor_runtime
from robotenon.runtime.safety import SafetyStateStore
from robotenon.schemas import MonitorReport, SafetyLevel, SessionSpec


@register_monitor("test-dual-mode-monitor", replace=True)
class _DualModeMonitor:
    instances: ClassVar[list] = []

    def __init__(self, *, reject: bool = False):
        self.reject = reject
        self.calls = []
        self.started = 0
        self.closed = 0
        self.__class__.instances.append(self)

    async def start(self):
        self.started += 1

    async def close(self):
        self.closed += 1

    async def check(self, feedback=None, *, request=None, context=None):
        self.calls.append((feedback, request, context))
        rejected = self.reject or bool(request and request.get("reject"))
        return MonitorReport(
            session_id=feedback.session_id if feedback else None,
            compliant=not rejected,
            safety_level=SafetyLevel.P3 if rejected else SafetyLevel.P0,
            source="test-dual-mode-monitor",
        )


def test_one_monitor_can_be_resident_and_request_driven():
    async def scenario():
        adapter = FakeEnvironmentAdapter(max_steps=2)
        session = await adapter.open(SessionSpec(task_id="dual", environment_id="fake"))
        feedback = await session.reset(SessionSpec(task_id="dual", environment_id="fake"))
        store = SafetyStateStore()
        runtime = build_monitor_runtime(
            MonitorsConfig(
                monitors=[
                    {
                        "id": "collision",
                        "name": "test-dual-mode-monitor",
                        "modes": ["resident", "request"],
                        "poll_hz": 1000,
                    }
                ]
            ),
            environment_service=session,
            safety_state=store,
        )
        await runtime.start()
        await asyncio.sleep(0.01)
        latest = await runtime.latest("collision")
        report = await runtime.request(
            "collision", {"reject": True}, feedback=feedback
        )
        instance = runtime.resolve("collision")
        assert latest is not None
        assert report.safety_level is SafetyLevel.P3
        assert instance.started == 1
        assert len(instance.calls) >= 2
        assert instance.calls[-1][1] == {"reject": True}
        assert runtime.describe()[0]["modes"] == ["resident", "request"]
        await runtime.close()
        assert instance.closed == 1
        await session.close()

    asyncio.run(scenario())


def test_monitor_config_is_loaded_by_the_canonical_parameter_bundle(tmp_path: Path):
    monitor_path = tmp_path / "monitors.json"
    monitor_path.write_text(
        '{"config_version": 1, "monitors": [{"name": "test-dual-mode-monitor", "mode": "both"}]}',
        encoding="utf-8",
    )
    master_path = tmp_path / "config.json"
    master_path.write_text(
        '{"modules_config": {"modules": []}, "graphs_config": {"graphs": []}, '
        '"tools_config": {"tools": []}, "monitors_config": "monitors.json"}',
        encoding="utf-8",
    )
    loaded = load_application_config(master_path)
    assert loaded.monitors.monitors[0].modes == ("resident", "request")
    assert loaded.params.get("monitors") is not None


def test_request_only_monitor_does_not_require_an_environment():
    async def scenario():
        runtime = build_monitor_runtime(
            MonitorsConfig(
                monitors=[
                    {
                        "name": "test-dual-mode-monitor",
                        "id": "logical",
                        "modes": ["request"],
                    }
                ]
            )
        )
        report = await runtime.request("logical", {"reject": False})
        assert report.safety_level is SafetyLevel.P0
        await runtime.close()

    asyncio.run(scenario())
