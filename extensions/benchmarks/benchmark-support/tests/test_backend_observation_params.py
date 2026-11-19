from pathlib import Path

import pytest

from robotenon_benchmark_support.backend_loader import observation_params


def test_file_backend_observation_params_are_discovered(tmp_path):
    backend = tmp_path / "vla.py"
    backend.write_text(
        "SPEC={'api_version':1,'supported_benchmarks':['x'],"
        "'supports_batch':True,'history':{'length':1,'interval':1},"
        "'replan_steps':1}\n"
        "OBSERVATION_PARAMS={'camera':True,'state':8}\n"
        "def create(**params): return object()\n"
    )
    assert observation_params(str(backend)) == {"camera": True, "state": 8}


def test_backend_observation_params_must_be_mapping(tmp_path):
    backend = tmp_path / "vla.py"
    backend.write_text(
        "SPEC={'api_version':1,'supported_benchmarks':['x'],"
        "'supports_batch':True,'history':{'length':1,'interval':1},"
        "'replan_steps':1}\n"
        "OBSERVATION_PARAMS=[]\n"
        "def create(**params): return object()\n"
    )
    with pytest.raises(TypeError, match="mapping"):
        observation_params(str(backend))
