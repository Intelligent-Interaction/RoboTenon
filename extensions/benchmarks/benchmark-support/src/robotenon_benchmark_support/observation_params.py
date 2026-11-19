"""Local equivalent of upstream model observation-parameter negotiation.

The user's vla.py may expose OBSERVATION_PARAMS without constructing a model.
Explicit benchmark profile values win; unsupported constructor keys are ignored
with a warning, matching the pinned orchestrator's merge policy.
"""
import importlib
import inspect
import logging
from collections.abc import Mapping


def merge_observation_params(cls, configured, factory=None):
    merged = dict(configured)
    if factory is None:
        return merged
    module, sep, _ = factory.partition(':')
    if not sep:
        raise ValueError('VLA factory must be module:factory')
    proposed = getattr(importlib.import_module(module), 'OBSERVATION_PARAMS', {})
    if not isinstance(proposed, Mapping):
        raise TypeError('vla.py OBSERVATION_PARAMS must be a mapping')
    accepted = set()
    for base in cls.__mro__:
        init = base.__dict__.get('__init__')
        if init is not None:
            try:
                accepted.update(k for k,p in inspect.signature(init).parameters.items()
                                if k != 'self' and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY))
            except (TypeError, ValueError):
                pass
    for key,value in proposed.items():
        if key in merged:
            continue
        if key in accepted:
            merged[key] = value
        else:
            logging.getLogger(__name__).warning('Ignoring unsupported model observation parameter %r for %s', key, cls.__qualname__)
    return merged
