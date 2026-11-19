"""Optional static episode affinity, reusing core worker lifecycle and retries."""
import asyncio
from contextvars import ContextVar

from robotenon.runtime.execution_registry import register_execution_backend
from robotenon.runtime.parallel import PersistentExecutionBackend

_SLOT = ContextVar('benchmark_static_slot')


class SlotQueues:
    def __init__(self, slots):
        self.indices = {id(slot): i for i, slot in enumerate(slots)}
        self.queues = [asyncio.Queue() for _ in slots]
        for slot in slots:
            self.put_nowait(slot)

    async def get(self):
        return await self.queues[_SLOT.get()].get()

    def put_nowait(self, slot):
        self.queues[self.indices[id(slot)]].put_nowait(slot)


@register_execution_backend('benchmark_static', capabilities={'shared_observability', 'managed_workers'})
class StaticExecution(PersistentExecutionBackend):
    def __init__(self, *, context):
        super().__init__(context.options, context.profile, context.make_request)
        self.available = SlotQueues(self.slots)

    async def execute(self, configured, case, context):
        token = _SLOT.set(case.index % len(self.slots))
        try:
            return await super().execute(configured, case, context)
        finally:
            _SLOT.reset(token)
