"""Bounded MessagePack RPC on private local Unix sockets; never unpickle input."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import struct
import msgpack
from uuid import uuid4

MAX_MESSAGE_BYTES = 256 * 1024 * 1024


def encode(value, *, binary=False):
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise TypeError("batch payload mapping keys must be strings")
        return {"t": "dict", "v": {k: encode(v, binary=binary) for k, v in value.items()}}
    if isinstance(value, (tuple, list)):
        return {"t": "list", "v": [encode(v, binary=binary) for v in value]}
    if isinstance(value, bytes):
        return {"t": "bytes", "v": value if binary else base64.b64encode(value).decode("ascii")}
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite batch scalar")
        return value
    # NumPy remains optional for generic graph tools.
    if type(value).__module__.split(".")[0] == "numpy":
        import numpy as np

        if isinstance(value, np.generic):
            return encode(value.item(), binary=binary)
        if isinstance(value, np.ndarray) and value.dtype.kind in "buif":
            return {
                "t": "array",
                "dtype": value.dtype.str,
                "shape": list(value.shape),
                "v": value.tobytes(order="C") if binary else base64.b64encode(value.tobytes(order="C")).decode("ascii"),
            }
    raise TypeError(f"unsupported batch payload type: {type(value).__name__}")


def decode(value):
    if not isinstance(value, dict):
        return value
    if value["t"] == "bytes":
        return value["v"] if isinstance(value["v"], bytes) else base64.b64decode(value["v"], validate=True)
    if value["t"] == "dict":
        return {k: decode(v) for k, v in value["v"].items()}
    if value["t"] == "list":
        return [decode(v) for v in value["v"]]
    if value["t"] == "array":
        import numpy as np

        dtype = np.dtype(value["dtype"])
        if dtype.kind not in "buif":
            raise ValueError("unsupported array dtype")
        return (
            np.frombuffer(value["v"] if isinstance(value["v"], bytes)
                          else base64.b64decode(value["v"], validate=True), dtype=dtype)
            .reshape(value["shape"])
            .copy()
        )
    raise ValueError("unsupported batch payload tag")


def pack_message(value):
    data = b"RBM1" + msgpack.packb(encode(value, binary=True), use_bin_type=True)
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError("batch message exceeds size limit")
    return data


def unpack_message(data):
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError("batch message exceeds size limit")
    # Read old JSON messages for diagnostics/rolling client migration. New
    # writers require a new reader; upgrade model and worker processes together.
    raw = msgpack.unpackb(data[4:], raw=False) if data.startswith(b"RBM1") else json.loads(data)
    return decode(raw)


async def write_message(writer, value):
    data = pack_message(value)
    writer.write(struct.pack("!I", len(data)) + data)
    await writer.drain()


async def read_message(reader):
    size = struct.unpack("!I", await reader.readexactly(4))[0]
    if size > MAX_MESSAGE_BYTES:
        raise ValueError("batch message exceeds size limit")
    return unpack_message(await reader.readexactly(size))


class BatchClient:
    """One connection owns one unique model session; disconnect aborts it."""

    def __init__(self, endpoint, *, timeout_s=120, history_cache=False):
        self.endpoint, self.timeout = endpoint, timeout_s
        self.reader = self.writer = None
        self.session_id = uuid4().hex
        self.lock = asyncio.Lock()
        from .history_transport import HistoryCache
        self.history_cache = HistoryCache() if history_cache else None

    async def _rpc(self, operation, **values):
        async with self.lock:
            try:

                async def exchange():
                    request_id = uuid4().hex
                    await write_message(
                        self.writer, {"id": request_id, "op": operation, **values}
                    )
                    response = await read_message(self.reader)
                    if response["id"] != request_id:
                        raise RuntimeError("unexpected batch response identity")
                    if not response["ok"]:
                        raise RuntimeError(response["error"])
                    return response.get("output")

                return await asyncio.wait_for(exchange(), self.timeout)
            except BaseException:
                await self.disconnect()
                raise

    async def open(self, metadata):
        if self.writer is not None:
            raise RuntimeError("batch session is already open")
        self.session_id = uuid4().hex
        if self.history_cache is not None:
            self.history_cache.clear()
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self.endpoint), self.timeout
        )
        await self._rpc("open", session_id=self.session_id, metadata=metadata)

    async def infer(self, payload, *, batch_key="default"):
        if self.history_cache is not None:
            message = self.history_cache.prepare(payload)
            if message is not None:
                output = await self._rpc("infer_delta", payload=message, batch_key=batch_key)
                self.history_cache.acknowledge(message)
                return output
            self.history_cache.clear()
        return await self._rpc("infer", payload=payload, batch_key=batch_key)

    async def close(self, result=None):
        try:
            if self.writer is not None:
                await self._rpc("close", result=result or {"status": "aborted"})
        finally:
            await self.disconnect()

    async def disconnect(self):
        if self.history_cache is not None:
            self.history_cache.clear()
        writer, self.writer = self.writer, None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


async def serve_connection(engine, reader, writer):
    session_id = None
    from .history_transport import HistoryCache
    history_cache = HistoryCache()
    try:
        while True:
            message = await read_message(reader)
            try:
                op = message["op"]
                if op == "open" and session_id is None:
                    proposed = message["session_id"]
                    await engine.open_session(proposed, message["metadata"])
                    session_id = proposed
                    output = None
                elif op in {"infer", "infer_delta"} and session_id is not None:
                    if op == "infer_delta":
                        payload = history_cache.restore(message["payload"])
                    else:
                        history_cache.clear()
                        payload = message["payload"]
                    # Detect worker death / timeout while inference is queued.
                    inference = asyncio.create_task(
                        engine.infer(
                            session_id,
                            payload,
                            batch_key=message["batch_key"],
                        )
                    )
                    disconnect = asyncio.create_task(reader.read(1))
                    try:
                        done, _ = await asyncio.wait(
                            {inference, disconnect}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if disconnect in done:
                            raise ConnectionError(
                                "client disconnected or sent overlapping requests"
                            )
                        output = inference.result()
                    finally:
                        disconnect.cancel()
                        if not inference.done():
                            inference.cancel()
                        await asyncio.gather(
                            inference, disconnect, return_exceptions=True
                        )
                elif op == "close" and session_id is not None:
                    await engine.close_session(session_id, message["result"])
                    session_id = None
                    output = None
                else:
                    raise ValueError("invalid batch session operation")
                await write_message(
                    writer, {"id": message["id"], "ok": True, "output": output}
                )
                if op == "close":
                    break
            except (ConnectionError, BrokenPipeError):
                raise
            except Exception as exc:  # noqa: BLE001 - serialize provider errors across RPC
                await write_message(
                    writer,
                    {
                        "id": message["id"],
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        pass
    finally:
        history_cache.clear()
        writer.close()
        if session_id is not None:
            await engine.close_session(session_id)
