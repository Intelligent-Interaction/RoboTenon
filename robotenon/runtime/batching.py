"""Model-independent dynamic batching; no graph or simulator dependencies."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4


class BatchEngine:
    """One provider replica, isolated sessions, at most one request per session.

    Synchronous model hooks run on one dedicated thread (including lifecycle
    hooks). This keeps CUDA/model state serialized without blocking IPC.
    Providers implement open_session, infer_batch, close_session and optionally
    batch_key and close. Responses are keyed by request_id, never list position.
    """

    def __init__(self, provider, *, max_batch_size=8, max_wait_ms=5, max_pending=256):
        if max_batch_size < 1 or max_wait_ms < 0 or max_pending < max_batch_size:
            raise ValueError("invalid batch size, wait window or queue capacity")
        self.provider = provider
        self.max_batch_size, self.max_wait = max_batch_size, max_wait_ms / 1000
        self.capacity = asyncio.Semaphore(max_pending)
        self.sessions, self.pending = {}, deque()
        self.wake = asyncio.Event()
        self.lock = asyncio.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="model")
        self.model_loop = None
        self.task = None
        self.closed = False
        self.metrics = {
            "batches": 0,
            "requests": 0,
            "batch_sizes": {},
            "queue_wait_sec": 0.0,
            "inference_sec": 0.0,
            "pending_peak": 0,
        }

    def _call(self, method, args):
        if self.model_loop is None:
            self.model_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.model_loop)
        value = method(*args)
        return (
            self.model_loop.run_until_complete(value)
            if inspect.isawaitable(value)
            else value
        )

    async def create_provider(self, factory, params):
        self.provider = await asyncio.get_running_loop().run_in_executor(
            self.executor, self._call, lambda: factory(**params), ()
        )

    async def invoke(self, name, *args):
        method = getattr(self.provider, name, None)
        if method is None:
            return None
        return await asyncio.get_running_loop().run_in_executor(
            self.executor, self._call, method, args
        )

    async def open_session(self, session_id, metadata):
        if self.closed or session_id in self.sessions:
            raise RuntimeError("closed engine or duplicate session")
        state = {"busy": False, "future": None, "sequence": 0}
        self.sessions[session_id] = state
        try:
            async with self.lock:
                await self.invoke("open_session", session_id, metadata)
        except BaseException:
            self.sessions.pop(session_id, None)
            raise
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def infer(self, session_id, payload, *, batch_key="default"):
        state = self.sessions.get(session_id)
        if self.closed or state is None or state["busy"]:
            raise RuntimeError("unknown, closed or already busy session")
        state["busy"] = True
        try:
            async with self.capacity:
                if self.closed or self.sessions.get(session_id) is not state:
                    raise RuntimeError("session ended while waiting for queue capacity")
                future = asyncio.get_running_loop().create_future()
                state["future"] = future
                request = {
                    "request_id": uuid4().hex,
                    "session_id": session_id,
                    "step_id": state["sequence"],
                    "observation": payload,
                }
                state["sequence"] += 1
                # Provider keys must be cheap/pure, not model inference.
                provider_key = getattr(self.provider, "batch_key", lambda r: None)(
                    request
                )
                key = (batch_key, provider_key)
                hash(key)
                self.pending.append((time.monotonic(), key, request, future))
                self.metrics["pending_peak"] = max(self.metrics["pending_peak"], len(self.pending))
                self.wake.set()
                return await future
        finally:
            state["busy"] = False
            state["future"] = None

    async def close_session(self, session_id, result=None):
        state = self.sessions.pop(session_id, None)
        if state is None:
            return
        future = state["future"]
        if future is not None and not future.done():
            future.set_exception(RuntimeError("session closed"))
        self.wake.set()
        # Do not mutate model history during an in-flight forward pass.
        async with self.lock:
            await self.invoke(
                "close_session", session_id, result or {"status": "aborted"}
            )

    async def _run(self):
        while not self.closed:
            self.pending = deque(item for item in self.pending if not item[3].done())
            if not self.pending:
                self.wake.clear()
                await self.wake.wait()
                continue
            oldest, key, _, _ = self.pending[0]
            matching = [item for item in self.pending if item[1] == key]
            delay = oldest + self.max_wait - time.monotonic()
            if len(matching) < self.max_batch_size and delay > 0:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), delay)
                except asyncio.TimeoutError:
                    pass
                continue
            batch = matching[: self.max_batch_size]
            ids = {item[2]["request_id"] for item in batch}
            self.pending = deque(
                item for item in self.pending if item[2]["request_id"] not in ids
            )
            try:
                async with self.lock:
                    batch = [item for item in batch if not item[3].done()]
                    if not batch:
                        continue
                    start = time.monotonic()
                    responses = await self.invoke(
                        "infer_batch", [item[2] for item in batch]
                    )
                    expected = {item[2]["request_id"] for item in batch}
                    by_id = {r["request_id"]: r for r in responses}
                    if len(by_id) != len(responses) or set(by_id) != expected:
                        raise ValueError(
                            "batch response IDs must match requests exactly once"
                        )
                    for _, _, request, future in batch:
                        response = by_id[request["request_id"]]
                        if "output" not in response:
                            raise ValueError("batch response requires output")
                    elapsed = time.monotonic() - start
                    self.metrics["batches"] += 1
                    self.metrics["requests"] += len(batch)
                    counts = self.metrics["batch_sizes"]
                    counts[str(len(batch))] = counts.get(str(len(batch)), 0) + 1
                    self.metrics["inference_sec"] += elapsed
                    self.metrics["queue_wait_sec"] += sum(
                        start - item[0] for item in batch
                    )
                    for _, _, request, future in batch:
                        if not future.done():
                            future.set_result(by_id[request["request_id"]]["output"])
            except Exception as error:  # noqa: BLE001 - isolate provider failures to this batch
                import traceback
                traceback.print_exc()
                for *_, future in batch:
                    if not future.done():
                        future.set_exception(error)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.wake.set()
        try:
            for session_id in list(self.sessions):
                await self.close_session(session_id)
            if self.task is not None:
                await self.task
            await self.invoke("close")
        finally:
            if self.model_loop is not None:
                await asyncio.get_running_loop().run_in_executor(
                    self.executor, self.model_loop.close
                )
            self.executor.shutdown(wait=False, cancel_futures=True)
