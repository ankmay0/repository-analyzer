"""A tiny in-process TTL cache.

Values live in this worker's memory only: they are not shared between
processes and are lost on restart. There is no Redis.
"""

import time
from collections.abc import Callable
from typing import Any

_store: dict[str, tuple[float, Any]] = {}


def cached(key: str, ttl_seconds: int, load: Callable[[], Any]) -> Any:
    """Return the cached value for ``key`` or call ``load`` and keep it for ``ttl_seconds``."""
    now = time.monotonic()
    hit = _store.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    value = load()
    _store[key] = (now + ttl_seconds, value)
    return value


def invalidate(prefix: str) -> None:
    """Drop every entry whose key starts with ``prefix``."""
    for key in [k for k in _store if k.startswith(prefix)]:
        del _store[key]
