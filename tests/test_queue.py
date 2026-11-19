import asyncio
from uuid import uuid4

import pytest

from robotenon.core.queue import TaskQueue
from robotenon.errors import LeaseLostError
from robotenon.schemas import TaskItem, TaskStatus


def test_queue_claim_complete_and_events():
    async def scenario():
        session = uuid4()
        queue = TaskQueue()
        item = await queue.enqueue(TaskItem(session_id=session, task_id="t", goal="pick"))
        claimed = await queue.next_task(session_id=session, owner="workflow")
        done = await queue.complete(item.task_item_id, owner="workflow")
        events = await queue.events(task_item_id=item.task_item_id)
        await queue.close()
        return claimed, done, events

    claimed, done, events = asyncio.run(scenario())
    assert claimed is not None and claimed.status is TaskStatus.RUNNING
    assert done.status is TaskStatus.DONE
    assert [event["event_type"] for event in events] == ["enqueued", "claimed", "completed"]


def test_queue_branch_and_clear():
    async def scenario():
        session = uuid4()
        queue = TaskQueue()
        parent = await queue.enqueue(TaskItem(session_id=session, task_id="t", goal="parent"))
        children = [TaskItem(session_id=session, task_id="t", goal="branch")]
        await queue.branch(parent_id=parent.task_item_id, tasks=children)
        await queue.clear(session_id=session, reason="test")
        snapshot = await queue.snapshot(session_id=session)
        await queue.close()
        return parent, snapshot

    parent, snapshot = asyncio.run(scenario())
    assert snapshot[1].parent_id == parent.task_item_id
    assert all(item.status is TaskStatus.CANCELLED for item in snapshot)


def test_queue_retry_until_limit():
    async def scenario():
        session = uuid4()
        queue = TaskQueue()
        item = await queue.enqueue(TaskItem(session_id=session, task_id="t", goal="retry", max_retries=1))
        await queue.next_task(session_id=session, owner="workflow")
        first = await queue.fail(item.task_item_id, owner="workflow", retry=True)
        await queue.next_task(session_id=session, owner="workflow")
        second = await queue.fail(item.task_item_id, owner="workflow", retry=True)
        await queue.close()
        return first, second

    first, second = asyncio.run(scenario())
    assert first.status is TaskStatus.PENDING
    assert second.status is TaskStatus.FAILED


def test_two_queue_processes_cannot_claim_one_task(tmp_path):
    async def scenario():
        session = uuid4()
        path = tmp_path / "shared-queue.sqlite3"
        first, second = TaskQueue(path), TaskQueue(path)
        item = await first.enqueue(
            TaskItem(session_id=session, task_id="t", goal="pick")
        )
        claimed = await asyncio.gather(
            first.claim_next(session_id=session, owner="workflow-a"),
            second.claim_next(session_id=session, owner="workflow-b"),
        )
        await first.close()
        await second.close()
        return item, claimed

    item, claimed = asyncio.run(scenario())
    assert sum(value is not None for value in claimed) == 1
    winner = next(value for value in claimed if value is not None)
    assert winner.task_item_id == item.task_item_id


def test_two_queue_processes_serialize_failure_and_make_duplicate_transition_idempotent(tmp_path):
    async def scenario():
        session = uuid4()
        path = tmp_path / "shared-failure.sqlite3"
        first, second = TaskQueue(path), TaskQueue(path)
        item = await first.enqueue(
            TaskItem(session_id=session, task_id="t", goal="pick", max_retries=1)
        )
        await first.claim_next(session_id=session, owner="workflow-a")
        results = await asyncio.gather(
            first.fail(item.task_item_id, owner="workflow-a", retry=True),
            second.fail(item.task_item_id, owner="workflow-b", retry=True),
            return_exceptions=True,
        )
        events = await first.events(task_item_id=item.task_item_id)
        duplicate = await second.fail(item.task_item_id, owner="workflow-a", retry=True)
        await first.close()
        await second.close()
        return results, events, duplicate

    results, events, duplicate = asyncio.run(scenario())
    assert all(isinstance(value, (TaskItem, LeaseLostError)) for value in results)
    assert any(isinstance(value, TaskItem) for value in results)
    assert [event["event_type"] for event in events].count("retry") == 1
    assert duplicate.status is TaskStatus.PENDING


def test_owner_cannot_complete_or_fail_an_unclaimed_item():
    async def scenario():
        queue = TaskQueue()
        task = TaskItem(session_id=uuid4(), task_id="owner", goal="guard")
        await queue.enqueue(task)
        try:
            with pytest.raises(LeaseLostError):
                await queue.complete(task.task_item_id, owner="workflow")
            with pytest.raises(LeaseLostError):
                await queue.fail(task.task_item_id, owner="workflow", retry=True)
        finally:
            await queue.close()

    asyncio.run(scenario())
