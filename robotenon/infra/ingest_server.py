"""A single-writer Unix socket endpoint with explicit ownership."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path


class IngestServer:
    def __init__(self, path, store, *, close_timeout_s=2.0, max_connections=64):
        if close_timeout_s <= 0:
            raise ValueError("close_timeout_s must be positive")
        if max_connections <= 0:
            raise ValueError("max_connections must be positive")
        self.path = Path(path)
        self.store = store
        self.close_timeout_s = close_timeout_s
        self.max_connections = int(max_connections)
        self.server = None
        self.handlers = set()
        self.writers = set()
        self.writes = set()
        self._closing = False
        self._close_lock = asyncio.Lock()

    async def start(self):
        self._closing = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Binding ourselves prevents asyncio from unlinking an active endpoint.
            sock.bind(str(self.path))
        except BaseException:
            sock.close()
            raise
        try:
            sock.setblocking(False)
            self.server = await asyncio.start_unix_server(
                self.handle, sock=sock, limit=8 * 1024 * 1024
            )
            os.chmod(self.path, 0o600)
            return self
        except BaseException:
            sock.close()
            self.path.unlink(missing_ok=True)
            raise

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        # A timed-out producer can disconnect while its SQLite/WAL write is
        # still queued in a worker thread.  Without admission control those
        # server-side sockets accumulate across episodes until accept() hits
        # RLIMIT_NOFILE.  Reject excess clients before retaining their writer.
        if len(self.handlers) >= self.max_connections:
            try:
                writer.write(b'{"ok":false,"error":"observability ingest busy"}\n')
                await writer.drain()
            except OSError:
                pass
            finally:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), self.close_timeout_s)
                except (OSError, asyncio.TimeoutError):
                    pass
            return
        self.handlers.add(task)
        self.writers.add(writer)
        try:
            while not self._closing:
                line = await reader.readline()
                if not line:
                    break
                try:
                    payload = json.loads(line)
                    batch = payload if isinstance(payload, list) else [payload]
                    write = asyncio.create_task(
                        asyncio.to_thread(self.store.ingest_batch, batch)
                    )
                    self.writes.add(write)
                    try:
                        # Cancelling a handler cannot stop an in-flight disk write.
                        await asyncio.shield(write)
                    finally:
                        if write.done():
                            self.writes.discard(write)
                    response = {"ok": True}
                except Exception as exc:
                    response = {"ok": False, "error": str(exc)}
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except OSError:
            # A crashed worker may disconnect after its batch was committed.
            pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), self.close_timeout_s)
            except (OSError, asyncio.TimeoutError):
                pass
            finally:
                self.handlers.discard(task)
                self.writers.discard(writer)

    async def close(self):
        async with self._close_lock:
            self._closing = True
            server = self.server
            if server is not None:
                server.close()
                self.server = None
                self.path.unlink(missing_ok=True)
            for writer in tuple(self.writers):
                writer.close()
            if server is not None:
                await asyncio.wait_for(
                    server.wait_closed(), timeout=self.close_timeout_s
                )
            tasks = self.handlers | self.writes
            if not tasks:
                return
            done, pending = await asyncio.wait(tasks, timeout=self.close_timeout_s)
            for task in done:
                if not task.cancelled():
                    task.exception()  # consume errors after client disconnection
            self.writes.difference_update(done)
            for task in pending & self.handlers:
                task.cancel()
            if pending & self.handlers:
                await asyncio.wait(
                    pending & self.handlers, timeout=self.close_timeout_s
                )
            if any(not task.done() for task in self.handlers):
                raise TimeoutError("observation ingest handlers did not close")
            if any(not task.done() for task in self.writes):
                raise TimeoutError(
                    "observation ingest write is still running after shutdown deadline"
                )
