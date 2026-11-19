"""Process-resident observation infrastructure handles.

The cache itself remains replaceable, but this small handle lets the
environment singleton, Workflow, Monitor and Web bootstrap share one
pipeline without passing it through every Module signature.
"""

from __future__ import annotations

import threading
from typing import Any


class ObservationInfrastructure:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: Any = None
        self._pipeline: Any = None
        self._resolver: Any = None

    @property
    def cache(self) -> Any:
        with self._lock:
            return self._cache

    @property
    def pipeline(self) -> Any:
        with self._lock:
            return self._pipeline

    @property
    def resolver(self) -> Any:
        with self._lock:
            return self._resolver

    def bind(self, cache: Any, *, pipeline: Any = None, resolver: Any = None) -> Any:
        if cache is None:
            raise ValueError("observation cache cannot be None")
        with self._lock:
            previous = self._cache
            self._cache = cache
            self._pipeline = pipeline if pipeline is not None else getattr(cache, "pipeline", None)
            if self._pipeline is None:
                from .observation_pipeline import ObservationPipeline

                self._pipeline = ObservationPipeline(cache)
            self._resolver = resolver if resolver is not None else getattr(self._pipeline, "resolver", None)
            return previous

    def clear(self, expected: Any = None) -> Any:
        with self._lock:
            current = self._cache
            if expected is None or current is expected:
                self._cache = None
                self._pipeline = None
                self._resolver = None
            return current


observation_infrastructure = ObservationInfrastructure()
global_observation_infrastructure = observation_infrastructure


def configure_observation_infrastructure(cache: Any, *, pipeline: Any = None, resolver: Any = None) -> Any:
    return observation_infrastructure.bind(cache, pipeline=pipeline, resolver=resolver)


def get_observation_cache() -> Any:
    return observation_infrastructure.cache


def get_observation_pipeline() -> Any:
    return observation_infrastructure.pipeline


def get_observation_resolver() -> Any:
    return observation_infrastructure.resolver


def clear_observation_infrastructure(expected: Any = None) -> Any:
    return observation_infrastructure.clear(expected)


__all__ = [
    "ObservationInfrastructure",
    "clear_observation_infrastructure",
    "configure_observation_infrastructure",
    "get_observation_cache",
    "get_observation_pipeline",
    "get_observation_resolver",
    "global_observation_infrastructure",
    "observation_infrastructure",
]
