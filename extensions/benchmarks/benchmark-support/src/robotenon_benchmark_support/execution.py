"""Extension-owned native lifetime; RoboTenon still executes every Graph node.

One ordered stream per experiment, one native instance per benchmark. No episode
loop is hidden here: execute delegates one TaskCase to the existing runner.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from robotenon.runtime.execution_registry import register_execution_backend
from .render import apply_render_mode

_ACTIVE_POOL = ContextVar('native_benchmark_pool', default=None)


def active_pool():
    return _ACTIVE_POOL.get()


class NativePool:
    def __init__(self):
        # One benchmark registration may be used with several native parameter
        # sets (for example the four LIBERO suites in one shuffled run).  Cache
        # their immutable task manifests, but keep at most one native runtime
        # active for a registration so switching suites cannot retain several
        # MuJoCo environments in every Worker.
        self.entries = {}
        self.active = {}
        self.leases = {}
        self.closed = False

    def acquire(self, key, cls, params, render):
        if self.closed:
            raise RuntimeError('native resource pool is closed')
        signature = (cls, deepcopy(params), render)
        if key in self.leases:
            raise RuntimeError('native benchmark cannot interleave episodes')
        entries = self.entries.setdefault(key, [])
        entry = next((item for item in entries if item['signature'] == signature), None)
        if entry is None:
            apply_render_mode(cls, render, key)
            native = cls(**params)
            try:
                # The reference constructs once, queries metadata, then tasks once.
                native.get_metadata()
                tasks = native.get_tasks()
            except BaseException:
                native.cleanup()
                raise
            entry = {'signature': signature, 'native': native, 'tasks': tasks,
                     'runtime_active': False}
            entries.append(entry)
        previous = self.active.get(key)
        if previous is not None and previous is not entry:
            previous['native'].cleanup()
            previous['runtime_active'] = False
        self.active[key] = entry
        self.leases[key] = entry
        entry['runtime_active'] = True
        return entry['native'], entry['tasks']

    def release(self, key):
        self.leases.pop(key)

    def close(self):
        if self.closed:
            return
        self.closed = True
        error = None
        for entries in self.entries.values():
            for entry in entries:
                if not entry['runtime_active']:
                    continue
                try:
                    entry['native'].cleanup()
                except BaseException as exc:
                    error = error or exc
        self.entries.clear()
        self.active.clear()
        self.leases.clear()
        if error is not None:
            raise error


@register_execution_backend('native_sync', capabilities={'local_worker'})
class NativeSyncBackend:
    max_workers = 1

    def __init__(self, *, context):
        if context.options.max_workers != 1 or context.options.max_retries:
            raise ValueError('native_sync requires one ordered worker and no retries')
        self.pool = NativePool()
        self.busy = False

    async def execute(self, configured, case, context):
        if self.busy:
            raise RuntimeError('native_sync cannot interleave task cases')
        self.busy = True
        token = _ACTIVE_POOL.set(self.pool)
        try:
            return await configured.adapter.run_case(case, context)
        finally:
            _ACTIVE_POOL.reset(token)
            self.busy = False

    async def close(self):
        self.pool.close()


@contextmanager
def native_worker_context():
    """One native pool per isolated process; Graph still owns every step."""
    pool = NativePool()
    token = _ACTIVE_POOL.set(pool)
    try:
        yield pool
    finally:
        _ACTIVE_POOL.reset(token)
        pool.close()
