from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from robotenon.runtime import resource_limits as limits


@pytest.mark.parametrize('soft,hard,expected', [
    (1024, 1048576, 65536), (1024, 4096, 4096),
    (1024, -1, 65536), (131072, 1048576, 131072), (-1, -1, -1),
])
def test_raise_soft_limit_only(monkeypatch, soft, hard, expected):
    state = [soft, hard]
    def set_limit(_, value):
        state[:] = value
    api = SimpleNamespace(RLIMIT_NOFILE=7, RLIM_INFINITY=-1,
                          getrlimit=lambda _: tuple(state), setrlimit=Mock(side_effect=set_limit))
    monkeypatch.setattr(limits, 'resource', api)
    assert limits.ensure_file_descriptor_limit() == expected
    assert state == [expected, hard]
    assert api.setrlimit.call_count == int(expected != soft)


def test_unsupported_platform(monkeypatch):
    monkeypatch.setattr(limits, 'resource', None)
    assert limits.ensure_file_descriptor_limit() is None


def test_permission_failure_warns(monkeypatch, caplog):
    api = SimpleNamespace(RLIMIT_NOFILE=7, RLIM_INFINITY=-1,
                          getrlimit=lambda _: (1024, 1048576),
                          setrlimit=Mock(side_effect=PermissionError('denied')))
    monkeypatch.setattr(limits, 'resource', api)
    assert limits.ensure_file_descriptor_limit() is None
    assert 'Unable to raise file descriptor limit' in caplog.text


def test_low_hard_limit_warns(monkeypatch, caplog):
    api = SimpleNamespace(RLIMIT_NOFILE=7, RLIM_INFINITY=-1,
                          getrlimit=lambda _: (1024, 1024), setrlimit=Mock())
    monkeypatch.setattr(limits, 'resource', api)
    assert limits.ensure_file_descriptor_limit() == 1024
    api.setrlimit.assert_not_called()
    assert 'system hard limit is 1024' in caplog.text
