"""Check renderer selection before a native benchmark is constructed."""
import asyncio

from robotenon_benchmark_support.smoke import probe
from parity_fixtures import StatefulBenchmark


def test_cpu_probe_configures_renderer_before_constructing_simulator(monkeypatch):
    configured = []
    original_init = StatefulBenchmark.__init__

    def configure(cls, mode):
        configured.append(mode)
        return {}

    def construct(self, **kwargs):
        assert configured == ["cpu"]
        original_init(self, **kwargs)

    monkeypatch.setattr(StatefulBenchmark, "render_backends", frozenset({"gpu", "cpu"}))
    monkeypatch.setattr(StatefulBenchmark, "configure_render", classmethod(configure))
    monkeypatch.setattr(StatefulBenchmark, "__init__", construct)
    report = asyncio.run(probe("parity_fixtures:StatefulBenchmark",
                               {"done_after": 10}, {"actions": [1.]}, render="cpu"))
    assert report["status"] == "passed", report
    assert report["render"] == "cpu"
    assert report["accepted_steps"] == 3
