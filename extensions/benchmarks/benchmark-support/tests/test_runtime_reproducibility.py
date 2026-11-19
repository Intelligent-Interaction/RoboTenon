"""Regression tests for opt-in RNG isolation."""
import random
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'extensions' / 'benchmarks' / 'vlabench' / 'src'))

from robotenon_vlabench.benchmark import seeded_load_env, VLABenchBenchmark


def fake_constructor(*, random_state=None):
    return np.random.RandomState(random_state).rand(3)


LM4ManipDMEnv = fake_constructor


def fake_loader(task, robot='franka'):
    return random.random(), np.random.rand(), LM4ManipDMEnv()


def test_private_composer_seed_is_repeatable_and_restores_rngs():
    py_state, np_state = random.getstate(), np.random.get_state()
    a = seeded_load_env(fake_loader, 7, 'task', 'franka')
    b = seeded_load_env(fake_loader, 7, 'task', 'franka')
    c = seeded_load_env(fake_loader, 8, 'task', 'franka')
    assert a[:2] == b[:2]
    np.testing.assert_array_equal(a[2], b[2])
    assert not np.array_equal(a[2], c[2])
    assert random.getstate() == py_state
    current = np.random.get_state()
    assert current[0] == np_state[0] and current[2:] == np_state[2:]
    np.testing.assert_array_equal(current[1], np_state[1])
    assert fake_loader.__globals__['LM4ManipDMEnv'] is fake_constructor


def test_seed_restores_rngs_on_failure():
    def failing(task, robot='franka'):
        random.random()
        np.random.rand()
        raise RuntimeError('failed reset')
    before = random.getstate()
    numpy_before = np.random.get_state()
    with pytest.raises(RuntimeError, match='failed reset'):
        seeded_load_env(failing, 5, 'task', 'franka')
    assert random.getstate() == before
    after = np.random.get_state()
    assert after[0] == numpy_before[0] and after[2:] == numpy_before[2:]
    np.testing.assert_array_equal(after[1], numpy_before[1])


@pytest.mark.parametrize('seed', [-1, 2**32, True, 1.2])
def test_invalid_seeds_rejected(seed):
    with pytest.raises(ValueError):
        VLABenchBenchmark(seed=seed)
