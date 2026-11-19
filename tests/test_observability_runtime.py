import asyncio
import json
import tempfile

import pytest

from robotenon.infra.metrics import MetricsClient, UnixSocketMetricsSink
from robotenon.infra.trace import TraceClient, TraceDaemon, UnixSocketTraceSink
from robotenon.infra.trace_query import TraceQuery
from robotenon.runtime.observability import (
    ExecutionIdentity,
    ObservabilityRuntime,
    ObservabilitySource,
)
from robotenon.web.query import PortalData


def test_ingest_disconnect_releases_handler_after_committing_batch(tmp_path):
    from types import SimpleNamespace
    from robotenon.infra.ingest_server import IngestServer

    batches = []
    server = IngestServer(tmp_path / "ingest.sock", SimpleNamespace(ingest_batch=batches.append))

    class Reader:
        async def readline(self):
            return b'{"name":"last-event"}\n'

    class Writer:
        def write(self, value):
            pass

        async def drain(self):
            raise ConnectionResetError("worker exited")

        def close(self):
            pass

        async def wait_closed(self):
            raise BrokenPipeError("worker exited")

    asyncio.run(server.handle(Reader(), Writer()))
    assert batches == [[{"name": "last-event"}]]
    assert not server.handlers


def test_portal_does_not_create_or_replay_database(tmp_path):
    root = tmp_path / "not-created"
    data = PortalData(root / "metrics.jsonl", root / "trace.jsonl")
    assert data.traces.snapshot() == []
    assert data.metrics.snapshot() == []
    assert not root.exists()
    store = TraceDaemon(root / "trace.jsonl")
    with TraceClient(store).span("unflushed"):
        pass
    # Malformed/unimported WAL must not be interpreted or indexed by the Web.
    (root / "trace.jsonl").write_text('{"extra":true}\n')
    assert data.traces.sql("SELECT count(*) AS n FROM spans")[0]["n"] == 0
    with pytest.raises(ValueError):
        data.traces.sql("DELETE FROM spans")
    store.close()


def test_shared_source_isolates_attempts_and_keeps_metric_identity_out_of_labels(
    tmp_path,
):
    async def scenario():
        source = ObservabilitySource(
            trace_db=str(tmp_path / "trace.sqlite3"),
            metrics_wal=str(tmp_path / "metrics.jsonl"),
        )
        identities = []
        for _ in range(2):
            identity = ExecutionIdentity.create(
                task_id="task",
                experiment_id="experiment",
                run_id="run",
                episode_id="episode",
            )
            identities.append(identity)
            runtime = await ObservabilityRuntime(source, identity).start()
            with runtime.trace.span("task"):
                pass
            runtime.metrics.counter("completed")
            await runtime.close()
        return identities

    identities = asyncio.run(scenario())
    reader = TraceQuery(tmp_path / "trace.sqlite3")
    for identity in identities:
        rows = reader.episode(
            run_id="run", episode_id="episode", attempt_id=identity.attempt_id
        )
        assert len(rows) == 1
    assert len(reader.snapshot()) == 2
    samples = [
        json.loads(line)
        for line in (tmp_path / "metrics.jsonl").read_text().splitlines()
    ]
    assert samples[0]["run_id"] == "run"
    assert samples[0]["attempt_id"] == identities[0].attempt_id
    assert samples[0]["labels"] == {}


@pytest.mark.parametrize("kind", ["trace", "metrics"])
@pytest.mark.parametrize("reply", [b'{"ok":false}\n', b""])
def test_rejected_or_missing_ack_restores_batch_without_false_drop(
    tmp_path, kind, reply
):
    async def scenario():
        socket_dir = tempfile.TemporaryDirectory(prefix="obs-test-", dir="/tmp")
        socket = str(__import__("pathlib").Path(socket_dir.name) / "sink.sock")

        async def handle(reader, writer):
            await reader.readline()
            writer.write(reply)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(handle, path=socket)
        try:
            client = (
                TraceClient(UnixSocketTraceSink(socket))
                if kind == "trace"
                else MetricsClient(UnixSocketMetricsSink(socket))
            )
            if kind == "trace":
                with client.span("test"):
                    pass
            else:
                client.counter("test")
            assert await client.flush() == 0
            assert len(client.buffer) == 1
            assert client.dropped == 0
            assert client.send_failures == 1
        finally:
            server.close()
            await server.wait_closed()
            socket_dir.cleanup()

    asyncio.run(scenario())


def test_owned_portal_queries_source_without_running_an_executor(tmp_path):
    import urllib.request

    from robotenon.runtime.observability import PortalRuntime
    async def scenario():
        source = ObservabilitySource(trace_db=str(tmp_path / "source.sqlite3"))
        identity = ExecutionIdentity.create(task_id="task", experiment_id="experiment", run_id="run", episode_id="episode")
        producer = await ObservabilityRuntime(source, identity).start()
        with producer.trace.span("visible"):
            pass
        await producer.close()
        portal = await PortalRuntime(source, {"enabled": True, "port": 0}).start()
        try:
            port = portal.server.server_address[1]
            def request():
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/traces?run_id=run&episode_id=episode") as response:
                    return json.load(response)
            payload = await asyncio.to_thread(request)
            assert [row["name"] for row in payload["traces"]] == ["visible"]
        finally:
            await portal.close()
    asyncio.run(scenario())


def test_ingest_server_never_replaces_an_owned_socket(tmp_path):
    from robotenon.infra.ingest_server import IngestServer
    async def scenario():
        with tempfile.TemporaryDirectory(prefix="obs-owned-", dir="/tmp") as directory:
            store = TraceDaemon(tmp_path / "trace.jsonl")
            first = await IngestServer(directory + "/trace.sock", store).start()
            second = IngestServer(directory + "/trace.sock", store)
            try:
                with pytest.raises(OSError):
                    await second.start()
                client = TraceClient(UnixSocketTraceSink(directory + "/trace.sock"))
                with client.span("still-owned"):
                    pass
                assert await client.flush() == 1
            finally:
                await second.close()
                await first.close()
                store.close()
    asyncio.run(scenario())
