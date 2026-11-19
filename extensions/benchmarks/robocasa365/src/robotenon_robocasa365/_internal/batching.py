"""Deprecated Xiaomi import; implementation lives with its model backend."""
from pathlib import Path
from robotenon_benchmark_support.backend_loader import load

_factory, _spec = load(str(Path(__file__).resolve().parents[4] / 'models/xiaomi/vla.py'))
XiaomiBatchVLA = _factory.__globals__['XiaomiBatchVLA']
OBSERVATION_PARAMS = {"camera_size": 256}
RNG_MODES = frozenset({"official_stream", "session_deterministic"})


def create_batch(**params):
    return _factory(**params)
