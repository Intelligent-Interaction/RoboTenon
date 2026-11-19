"""One bounded writer per evaluation, with an owned SQLite connection."""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path

from .result_index import ResultIndex


class ResultWriter:
    def __init__(self, root, write_json, *, capacity=16):
        self.index = ResultIndex(root)
        self.write_json = write_json
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="episode-writer"
        )
        self.slots = asyncio.Semaphore(capacity)
        self.connection = None
        self.closed = False

    async def run(self, function, *args):
        await self.slots.acquire()
        try:
            if self.closed:
                raise RuntimeError("result writer is closed")
            future = asyncio.get_running_loop().run_in_executor(
                self.executor, function, *args
            )
        except BaseException:
            self.slots.release()
            raise
        future.add_done_callback(
            lambda done: (
                self.slots.release(),
                done.exception() if not done.cancelled() else None,
            )
        )
        return await asyncio.shield(future)

    def _start(self):
        self.index.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.index.path, timeout=1)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.index.schema(self.connection)
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO index_meta VALUES ('clean','0')"
            )

    async def start(self):
        await self.run(self._start)

    def _initialize(self, records):
        with self.connection:
            self.connection.execute("DELETE FROM episodes")
            self.connection.executemany(
                "INSERT INTO episodes VALUES (?,?,?,?)",
                (self.index._row(item) for item in records),
            )

    async def initialize(self, records):
        await self.run(self._initialize, records)

    def _write(self, path, value, record):
        self.write_json(path, value)
        if record is not None:
            with self.connection:
                self.connection.execute(
                    "INSERT OR REPLACE INTO episodes VALUES (?,?,?,?)",
                    self.index._row(record),
                )

    async def write(self, path, value, *, record=None):
        await self.run(self._write, path, value, record)

    def _finish(self, clean):
        if self.connection is not None:
            try:
                if clean:
                    with self.connection:
                        self.connection.execute(
                            "INSERT OR REPLACE INTO index_meta VALUES ('clean','1')"
                        )
            finally:
                self.connection.close()
                self.connection = None

    async def close(self, *, clean=False):
        if self.closed:
            return
        self.closed = True
        future = asyncio.get_running_loop().run_in_executor(
            self.executor, self._finish, clean
        )
        cancelled = False
        try:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    # Keep run ownership until every accepted write has finished.
                    cancelled = True
            future.result()
        finally:
            self.executor.shutdown(wait=False)
        if cancelled:
            raise asyncio.CancelledError


def with_result_writer(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        root = Path(kwargs["output_dir"]) / kwargs["run_id"]
        writer = ResultWriter(root, function.__globals__["write_json"])
        clean = False
        try:
            await writer.start()
            result = await function(*args, _writer=writer, **kwargs)
            clean = True
            return result
        finally:
            await writer.close(clean=clean)

    return wrapped
