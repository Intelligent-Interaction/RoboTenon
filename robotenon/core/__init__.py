"""Lazy compatibility exports; resource construction belongs to bootstrap."""
from importlib import import_module

_EXPORTS = {'ContextConflictError': ('.context', 'ContextConflictError'),
 'ContextStore': ('.context', 'ContextStore'),
 'SQLiteContextStore': ('.context', 'SQLiteContextStore'),
 'SharedContextStore': ('.context', 'SharedContextStore'),
 'TaskQueue': ('.queue', 'TaskQueue'),
 'global_context_store': ('.context', 'global_context_store')}

__all__ = list(_EXPORTS)

def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module, __name__), attribute)
    globals()[name] = value
    return value
