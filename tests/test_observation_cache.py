from __future__ import annotations

import asyncio
import threading
from uuid import uuid4

import numpy as np

from robotenon.contracts.context import GraphContext
from robotenon.bodies.fake import FakeEnvironmentActionDriver
from robotenon.infra import ObservationCache, ObservationPipeline
from robotenon.schemas import Action, EnvironmentFeedback
from robotenon.services.environment import ObservationEnvironmentService
from robotenon.envs.base.fake import FakeEnvironmentSession
from robotenon.schemas import SessionSpec


def test_pipeline_refs_and_round_trip(tmp_path):
    async def run():
        cache = ObservationCache(tmp_path, workers=1)
        pipeline = ObservationPipeline(cache)
        feedback = await pipeline.ingest(
            {
                "session_id": "session-001",
                "sequence": 0,
                "frames": {
                    "front_camera": np.zeros((2, 3, 3), dtype=np.uint8),
                    "caption": "pick the mug",
                },
            }
        )
        assert feedback.frames["front_camera"].uri.startswith("cache://session-001/")
        assert feedback.frames["front_camera"].shape == [2, 3, 3]
        assert '"data":' not in GraphContext(feedback=feedback).to_json()
        assert np.array_equal(await pipeline.resolver.resolve_ref(feedback.frames["front_camera"]), np.zeros((2, 3, 3), dtype=np.uint8))
        assert await pipeline.resolver.resolve_ref(feedback.frames["caption"]) == "pick the mug"
        await cache.close()

    asyncio.run(run())


def test_async_persistence_snapshots_mutable_numpy_frames(tmp_path):
    """A reused renderer buffer must not corrupt the archived preview."""

    async def run():
        cache = ObservationCache(tmp_path, workers=1)
        started = threading.Event()
        release = threading.Event()
        persist = cache._persist_sync

        def delayed(job):
            started.set()
            assert release.wait(2)
            persist(job)

        cache._persist_sync = delayed
        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        stored = await cache.append(
            EnvironmentFeedback(session_id=uuid4(), sequence_no=0),
            {"front_camera": frame},
        )
        assert await asyncio.to_thread(started.wait, 2)
        frame.fill(255)
        release.set()
        await cache.flush()
        restored = await cache.resolve(stored.frames["front_camera"].uri)
        assert np.array_equal(restored, np.zeros((4, 4, 3), dtype=np.uint8))
        await cache.close()

    asyncio.run(run())


def test_eviction_and_sqlite_recovery(tmp_path):
    async def run():
        session = uuid4()
        cache = ObservationCache(tmp_path, max_records=1, workers=1)
        first = await cache.append(EnvironmentFeedback(session_id=session, sequence_no=0), {"front_camera": b"a"}, durable=True)
        await cache.append(EnvironmentFeedback(session_id=session, sequence_no=1), {"front_camera": b"b"}, durable=True)
        assert (await cache.stats())["hot_records"] == 1
        assert (await cache.get_by_feedback_id(first.feedback_id)).sequence_no == 0
        assert await cache.resolve(first.frames["front_camera"].uri) == b"a"
        await cache.close()
        reopened = ObservationCache(tmp_path, max_records=1, workers=1)
        assert (await reopened.latest(session)).sequence_no == 1
        await reopened.close()

    asyncio.run(run())


def test_environment_service_ingests_reset_and_observe(tmp_path):
    async def run():
        session = FakeEnvironmentSession(SessionSpec(task_id="task", environment_id="fake"))
        cache = ObservationCache(tmp_path, workers=1)
        service = ObservationEnvironmentService(session, ObservationPipeline(cache))
        reset = await service.reset(session.spec)
        # Action execution belongs to Body.  Env only observes the resulting
        # state after the explicit fake Body driver runs.
        driver = FakeEnvironmentActionDriver(session)
        step = await driver.execute(Action(session_id=session.session_id))
        observed = await service.observe()
        assert reset.sequence == 0
        assert step.sequence == 1
        assert observed.sequence == step.sequence
        assert len(await cache.history(session.session_id, length=2)) == 2
        await service.close()
        await cache.close()

    asyncio.run(run())


def test_cross_process_sequence_reservation_rejects_conflicting_writers(tmp_path):
    async def run():
        session = uuid4()
        first = ObservationCache(tmp_path, workers=1)
        second = ObservationCache(tmp_path, workers=1)
        try:
            feedback_a = EnvironmentFeedback(session_id=session, sequence_no=0)
            feedback_b = EnvironmentFeedback(session_id=session, sequence_no=0)
            results = await asyncio.gather(
                first.append(feedback_a, {"front_camera": b"a"}),
                second.append(feedback_b, {"front_camera": b"b"}),
                return_exceptions=True,
            )
            assert sum(isinstance(item, EnvironmentFeedback) for item in results) == 1
            errors = [item for item in results if isinstance(item, Exception)]
            assert len(errors) == 1
            assert "sequence" in str(errors[0]).lower()
        finally:
            await first.close()
            await second.close()

    asyncio.run(run())


def test_sequence_reservation_is_idempotent_for_same_feedback_id(tmp_path):
    async def run():
        session = uuid4()
        cache = ObservationCache(tmp_path, workers=1)
        feedback = EnvironmentFeedback(session_id=session, sequence_no=0)
        try:
            stored = await cache.append(feedback, {"front_camera": b"a"})
            repeated = await cache.append(feedback, {"front_camera": b"a"})
            assert repeated.feedback_id == stored.feedback_id
            assert repeated.sequence_no == stored.sequence_no
        finally:
            await cache.close()

    asyncio.run(run())


def test_retention_removes_sequence_reservations(tmp_path):
    async def run():
        first_session, retained_session = uuid4(), uuid4()
        cache = ObservationCache(
            tmp_path,
            workers=1,
            keep_sessions=1,
            keep_days=None,
        )
        try:
            await cache.append(
                EnvironmentFeedback(session_id=first_session, sequence_no=0),
                {"front_camera": b"old"},
                durable=True,
            )
            await cache.append(
                EnvironmentFeedback(session_id=retained_session, sequence_no=0),
                {"front_camera": b"new"},
                durable=True,
            )
        finally:
            await cache.close()

        reopened = ObservationCache(
            tmp_path,
            workers=1,
            keep_sessions=1,
            keep_days=None,
        )
        try:
            # The old session was removed by retention, so sequence zero may
            # be allocated again when a new feedback id is used.
            replacement = await reopened.append(
                EnvironmentFeedback(session_id=first_session, sequence_no=0),
                {"front_camera": b"replacement"},
            )
            assert replacement.sequence_no == 0
        finally:
            await reopened.close()

    asyncio.run(run())


def test_resolve_during_pending_to_disk_handoff(tmp_path):
    """Commit after a reader's disk miss, before it inspects pending bytes."""
    async def run():
        cache = ObservationCache(tmp_path, workers=1)
        started, release = threading.Event(), threading.Event()
        persist = cache._persist_sync

        def delayed(job):
            started.set()
            assert release.wait(5)
            persist(job)

        cache._persist_sync = delayed
        try:
            stored = await cache.append(
                EnvironmentFeedback(session_id=uuid4(), sequence_no=0),
                {"front_camera": b"camera-frame"},
            )
            assert await asyncio.to_thread(started.wait, 5)
            query = cache._query_frame
            first = True

            def handoff(uri):
                nonlocal first
                row = query(uri)
                if first:
                    first = False
                    assert row is None
                    release.set()
                    # Wait for commit AND pending removal, without timing sleeps.
                    asyncio.run_coroutine_threadsafe(cache.flush(), loop).result(5)
                return row

            loop = asyncio.get_running_loop()
            cache._query_frame = handoff
            assert await cache.resolve(stored.frames["front_camera"].uri) == b"camera-frame"
        finally:
            release.set()
            await cache.close()

    asyncio.run(run())
