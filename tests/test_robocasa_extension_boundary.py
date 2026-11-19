"""RoboCasa365 exposes only the native benchmark and unified evaluator."""
import json
from pathlib import Path

from robotenon.bodies.registry import BodyRegistry
from robotenon.envs.base.protocol import EnvironmentRegistry
from robotenon.task_adapters.registry import TaskAdapterRegistry

ROOT = Path(__file__).parents[1]
EXTENSION = ROOT / "extensions" / "benchmarks" / "robocasa365"


def test_extension_has_only_public_protocol_profiles():
    assert {path.name for path in (EXTENSION / "configs").glob("*.json")} == {
        "smoke.json",
        "xiaomi_official.json",
    }
    assert not list((EXTENSION / "configs").glob("robotenon_xr365*.json"))


def test_old_xr365_modules_are_removed():
    package = EXTENSION / "src" / "robotenon_robocasa365"
    for name in ("xiaomi_tool.py", "xiaomi_module.py", "terminal.py", "gpt.py", "deployment.py", "legacy"):
        assert not (package / name).exists()


def test_core_registry_types_start_without_robocasa_providers():
    assert "robocasa365" not in EnvironmentRegistry().names()
    assert "robocasa365_body" not in BodyRegistry().names()
    assert "robocasa365" not in TaskAdapterRegistry().names()


def test_official_profile_has_no_speed_overrides():
    profile = json.loads((EXTENSION / "configs" / "xiaomi_official.json").read_text())
    assert profile["episodes_per_task"] == 50
    assert "max_steps" not in profile
    assert "layout_ids" not in profile["params"]
    assert "style_ids" not in profile["params"]
