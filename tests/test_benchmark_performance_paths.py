import json
from pathlib import Path

import numpy as np
import pytest

from robotenon.runtime.batch_transport import encode, pack_message, unpack_message
from robotenon_benchmark_support.codec import pack_observation, unpack_observation
from robotenon_benchmark_support.modules import validate_native_action

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / 'docs/benchmark-catalog.json').read_text())


@pytest.mark.parametrize('entry', CATALOG, ids=lambda e: e['id'])
def test_all_benchmark_fake_actions_use_common_binary_transport(entry):
    action = json.loads((ROOT / 'extensions/benchmarks/models/fake/configs' / (entry['id']+'.json')).read_text())['action']
    normalized = validate_native_action(action)
    result = unpack_message(pack_message({'benchmark': entry['id'], 'action': normalized}))
    assert result == {'benchmark': entry['id'], 'action': normalized}


@pytest.mark.parametrize('dtype', ['uint8', 'float32', '>f8', 'int64', 'bool'])
def test_binary_history_preserves_values_and_isolates_input(dtype):
    a = np.arange(72).reshape(4, 6, 3).astype(dtype)[:, ::2, :]
    payload = {'history': [{'images': {'camera': a}, 'state': np.zeros((0, 2))}], 't': 'array'}
    result = unpack_message(pack_message(payload))
    image = result['history'][0]['images']['camera']
    assert image.dtype == a.dtype
    np.testing.assert_array_equal(image, a)
    assert image.flags.writeable
    image[:] = 0
    assert np.any(a)
    assert result['t'] == 'array'
    old = json.dumps(encode(payload)).encode()
    np.testing.assert_array_equal(unpack_message(old)['history'][0]['images']['camera'], a)


def test_borrowed_history_is_readonly_and_survives_simulator_buffer_reuse():
    a = np.arange(48, dtype=np.uint8).reshape(4,4,3)
    original = a.copy()
    snapshot = pack_observation({'images': {'camera': a}})
    borrowed = unpack_observation(snapshot, copy=False)['images']['camera']
    owned = unpack_observation(snapshot)['images']['camera']
    assert np.shares_memory(borrowed, snapshot.frames[0].data)
    assert not np.shares_memory(owned, snapshot.frames[0].data)
    a[:] = 0  # simulator reuses its buffer on the next step
    owned[:] = 0  # public decoder still owns its writable copy
    del snapshot
    np.testing.assert_array_equal(borrowed, original)
    with pytest.raises(ValueError): borrowed[:] = 0


def test_wire_rejects_invalid_arrays_and_scalar_nan(monkeypatch):
    from robotenon.runtime import batch_transport as wire
    with pytest.raises(TypeError):pack_message({'a': np.array([object()])})
    with pytest.raises(ValueError):pack_message({'a': float('nan')})
    monkeypatch.setattr(wire, 'MAX_MESSAGE_BYTES', 10)
    with pytest.raises(ValueError):pack_message({'long': 'x'*100})
    with pytest.raises(ValueError):unpack_message(b'x'*11)
