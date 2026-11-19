"""Body/cerebellum extension namespace.

Body implementations are resident services receiving ``ActionChunk``
objects.  Register a provider with :func:`register_body`, select it in the
body manifest, and let runtime bootstrap own its lifecycle.
"""

from .base import BaseBodyService
from .registry import (
    BodyRegistry,
    RegisteredBody,
    body_registry,
    load_builtin_bodies,
    register_body,
)

__all__ = [
    "BaseBodyService",
    "BodyRegistry",
    "RegisteredBody",
    "body_registry",
    "load_builtin_bodies",
    "register_body",
]
