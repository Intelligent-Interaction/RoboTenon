"""A generic Graph Tool backed by a run-owned batching service."""

from ..runtime.batch_transport import BatchClient
from .base import BaseTool, ToolResult
from .registry import register_tool


@register_tool("shared_batch_model", kind="model")
class SharedBatchModel(BaseTool):
    def __init__(self, *, service, endpoint=None, timeout_s=120, batch_key="default", history_cache=False):
        if timeout_s <= 0:
            raise ValueError("model request timeout must be positive")
        self.service, self.endpoint = service, endpoint
        self.timeout_s, self.batch_key = timeout_s, batch_key
        self.history_cache = history_cache
        self.clients = {}
        self.busy = set()

    def bind_shared_services(self, endpoints):
        self.endpoint = endpoints[self.service]

    async def start(self):
        if not self.endpoint:
            raise RuntimeError(
                f"shared model service {self.service!r} requires a process execution run or explicit endpoint"
            )

    async def infer(self, session_id, payload, metadata):
        await self.start()
        if session_id is None:
            raise ValueError("shared model inference requires a session")
        key = str(session_id)
        if key in self.busy:
            raise RuntimeError(
                "only one outstanding model request per session is allowed"
            )
        self.busy.add(key)
        try:
            client = self.clients.get(key)
            if client is None:
                client = BatchClient(self.endpoint, timeout_s=self.timeout_s, history_cache=self.history_cache)
                self.clients[key] = client
                try:
                    await client.open(metadata)
                except BaseException:
                    self.clients.pop(key, None)
                    raise
            try:
                return await client.infer(payload, batch_key=self.batch_key)
            except BaseException:
                self.clients.pop(key, None)
                await client.disconnect()
                raise
        finally:
            self.busy.discard(key)

    async def call(self, arguments, context):
        if context.session_id is None:
            raise ValueError("shared model calls require a Graph session")
        payload = {
            key: value for key, value in arguments.items() if key != "observation_view"
        }
        if payload.get("operation") == "end_session":
            await self.end_session(context.session_id, payload.get("result"))
            return ToolResult(output={"ended": True})
        output = await self.infer(
            context.session_id,
            payload,
            {"task": str(getattr(context, "task_item", ""))},
        )
        return ToolResult(output=output)

    async def end_session(self, session_id, result=None):
        client = self.clients.pop(str(session_id), None)
        if client is not None:
            await client.close(result)

    async def close(self):
        import asyncio

        clients, self.clients = list(self.clients.values()), {}
        results = await asyncio.gather(
            *(c.close() for c in clients), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
