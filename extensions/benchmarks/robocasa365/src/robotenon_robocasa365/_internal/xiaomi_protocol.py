"""Compatibility import for preprocessing now owned by the Xiaomi backend."""
from importlib import import_module
from .batching import XiaomiBatchVLA

_protocol = import_module(XiaomiBatchVLA.__module__.rsplit('.', 1)[0] + '.preprocessing')


def __getattr__(name):
    return getattr(_protocol, name)
