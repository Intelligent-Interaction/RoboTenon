"""Bounded Graph/job dispatch shared by evaluation and other run launchers."""

import asyncio


def refill_slots(pending, running, max_workers, execute, key):
    """Start queued jobs immediately in free slots; no batch-wide barrier."""
    if max_workers < 1:
        raise ValueError("max_workers must be positive")
    while pending and len(running) < max_workers:
        job = pending.pop(0)
        identity = key(job)
        if identity in running:
            raise ValueError(f"duplicate running job: {identity}")
        running[identity] = asyncio.create_task(execute(job))


async def run_bounded(jobs, execute, *, max_workers):
    """Run independent jobs with refill; results retain input order.

    execute may launch a Graph in an isolated worker. This helper itself does
    not make process-global environment/Body resources safe for concurrency.
    A raised job error cancels and joins the remaining work.
    """
    if max_workers < 1:
        raise ValueError("max_workers must be positive")
    pending = list(enumerate(jobs))
    results, running = [None] * len(pending), {}

    async def dispatch(item):
        return await execute(item[1])

    try:
        while pending or running:
            refill_slots(pending, running, max_workers, dispatch, lambda item: item[0])
            done, _ = await asyncio.wait(
                running.values(), return_when=asyncio.FIRST_COMPLETED
            )
            for index, task in list(running.items()):
                if task in done:
                    results[index] = task.result()
                    del running[index]
        return results
    finally:
        for task in running.values():
            task.cancel()
        await asyncio.gather(*running.values(), return_exceptions=True)
