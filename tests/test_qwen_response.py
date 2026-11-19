import json

import pytest

from robotenon.tools.qwen35_9b import Qwen35_9BTool


def response(content, reason="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": reason}]}


def test_truncated_json_retries_with_larger_budget(monkeypatch):
    tool = Qwen35_9BTool(max_completion_tokens=1024)
    replies = iter([
        response('{"decision":"act","nested":{}', "length"),
        response('{"decision":"act"}'),
    ])
    budgets = []

    def request(body):
        budgets.append(json.loads(body)["max_tokens"])
        return next(replies)

    monkeypatch.setattr(tool, "_request_payload", request)
    assert tool._request_json(b'{"max_tokens":1024}') == {"decision": "act"}
    assert budgets == [1024, 2048]


def test_persistent_truncation_is_bounded_and_never_accepted(monkeypatch):
    tool = Qwen35_9BTool()
    budgets = []

    def request(body):
        budgets.append(json.loads(body)["max_tokens"])
        return response('{"decision":"stop"}', "length")

    monkeypatch.setattr(tool, "_request_payload", request)
    with pytest.raises(RuntimeError, match="after 3 attempts.*token budget"):
        tool._request_json(b'{"max_tokens":1024}')
    assert budgets == [1024, 2048, 4096]


def test_invalid_json_retries_and_valid_response_does_not(monkeypatch):
    tool = Qwen35_9BTool()
    replies = iter([response("invalid"), response('```json\n{"decision":"act"}\n```')])
    monkeypatch.setattr(tool, "_request_payload", lambda body: next(replies))
    assert tool._request_json(b'{"max_tokens":1024}') == {"decision": "act"}
    with pytest.raises(StopIteration):
        next(replies)


def test_retry_can_be_disabled(monkeypatch):
    tool = Qwen35_9BTool(max_response_retries=0)
    monkeypatch.setattr(tool, "_request_payload", lambda body: response("invalid"))
    with pytest.raises(RuntimeError, match="after 1 attempts"):
        tool._request_json(b'{"max_tokens":1024}')


def test_nested_object_inside_truncated_response_is_not_a_result():
    from robotenon.tools.qwen35_9b import _extract_json

    with pytest.raises(ValueError, match="incomplete or invalid"):
        _extract_json('{"decision":"act","extra":{"valid":true}')
    assert _extract_json('Result: {"decision":"act"}') == {"decision": "act"}
