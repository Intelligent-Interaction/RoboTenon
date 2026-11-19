"""System Tool compatibility entry for the standalone Xiaomi model backend."""
from pathlib import Path
from robotenon_benchmark_support.backend_loader import load

_factory, SPEC = load(str(Path(__file__).resolve().parents[4] / "models/xiaomi/vla.py"))
XiaomiBatchVLA = _factory.__globals__["XiaomiBatchVLA"]


def create(**params):
    return _factory(**params)
