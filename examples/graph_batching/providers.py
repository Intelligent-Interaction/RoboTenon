"""CPU-only example: generic Graphs share one batch-capable model provider."""

from robotenon.modules.base import BaseModule
from robotenon.runtime.registry import register_module


class EchoModel:
    def __init__(self):
        self.sessions = set()

    def open_session(self, session_id, metadata):
        self.sessions.add(session_id)

    def infer_batch(self, requests):
        return [
            {
                "request_id": r["request_id"],
                "output": {
                    "answer": r["observation"]["question"],
                    "batch_size": len(requests),
                },
            }
            for r in requests
        ]

    def close_session(self, session_id, result):
        self.sessions.remove(session_id)


@register_module("batch_example", kind="agent")
class AskModel(BaseModule):
    async def run(self, ctx):
        result = await ctx.call_tool("model", {"question": "hello"})
        if not result.ok:
            raise RuntimeError(result.error)
        return {"success": result.output["answer"] == "hello", **result.output}
