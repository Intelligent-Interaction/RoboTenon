"""Adapter for extension vla.py factories; no simulator dependency."""

import asyncio
import inspect

from . import load_factory


def call(method, *args):
    result = method(*args)
    return (
        asyncio.get_event_loop().run_until_complete(result)
        if inspect.isawaitable(result)
        else result
    )


class VLAProvider:
    """Native batch VLA or explicit per-session legacy compatibility.

    Batch VLA: open_session(id, task_info), act_batch(requests),
    close_session(id, result), close(). act_batch returns request_id/action.
    Legacy VLA: create a separate object for each session. This does NOT
    promise weight sharing or a batched forward pass.
    """

    def __init__(self, factory, params=None, interface="batch"):
        if interface not in {"batch", "legacy"}:
            raise ValueError("VLA interface must be batch or legacy")
        self.factory = load_factory(factory)
        self.params = dict(params or {})
        self.interface = interface
        self.model = self.factory(**self.params) if interface == "batch" else None
        self.sessions = {}

    def open_session(self, session_id, metadata):
        if self.model is not None:
            call(self.model.open_session, session_id, metadata)
        else:
            policy = self.factory(**self.params)
            self.sessions[session_id] = policy
            try:
                call(policy.start_episode, metadata)
            except BaseException:
                self.sessions.pop(session_id, None)
                if hasattr(policy, "close"):
                    call(policy.close)
                raise

    def batch_key(self, request):
        hook = getattr(self.model, "batch_key", None)
        return hook(request) if hook else None

    def infer_batch(self, requests):
        if self.model is not None:
            results = call(self.model.act_batch, requests)
            return [
                {"request_id": item["request_id"], "output": item["action"]}
                for item in results
            ]
        return [
            {
                "request_id": item["request_id"],
                "output": call(
                    self.sessions[item["session_id"]].act, item["observation"]
                ),
            }
            for item in requests
        ]

    def close_session(self, session_id, result):
        if self.model is not None:
            call(self.model.close_session, session_id, result)
        else:
            policy = self.sessions.pop(session_id, None)
            if policy is not None:
                try:
                    call(policy.end_episode, result)
                finally:
                    if hasattr(policy, "close"):
                        call(policy.close)

    def close(self):
        if self.model is not None and hasattr(self.model, "close"):
            call(self.model.close)
