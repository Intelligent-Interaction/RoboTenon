"""Independent benchmark extensions expose profiles and generate Graphs."""
import importlib
import json
from pathlib import Path

import pytest
from robotenon_benchmark_support.configure import application_config

ROOT = Path(__file__).resolve().parents[4]
CATALOG = json.loads((ROOT / "docs/benchmark-catalog.json").read_text())


@pytest.mark.parametrize("entry", CATALOG, ids=lambda e: e["id"])
def test_profile_based_extension_boundary(entry, monkeypatch):
    extension = ROOT / entry["path"]
    package_name = "robotenon_" + entry["id"]
    package = extension / "src" / package_name
    monkeypatch.syspath_prepend(str(extension / "src"))
    benchmark = importlib.import_module(package_name + ".benchmark")
    assert getattr(benchmark, entry["class"].split(":", 1)[1])
    profiles = [ROOT / path for path in entry["profiles"]]
    assert profiles and all(path.parent == extension / "configs" for path in profiles)
    assert not (package / "native").exists()
    for path in profiles:
        profile = json.loads(path.read_text())
        config = application_config(profile, policy_factory=package_name + ".vla:create")
        assert config["parameters"]["runtime"]["graph_id"] == ("benchmark_live_rollout" if profile.get("mode") == "live" else "benchmark_rollout")
