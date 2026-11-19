from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'extensions/benchmarks/benchmark-support/src'))
sys.path.insert(0, str(Path(__file__).parent))

import pytest

@pytest.fixture(autouse=True)
def isolate_resident_adapters(monkeypatch):
    from robotenon.envs.base import environment_registry
    monkeypatch.setattr(environment_registry, '_adapters', {})
    monkeypatch.setattr(environment_registry, '_adapter_params', {})
