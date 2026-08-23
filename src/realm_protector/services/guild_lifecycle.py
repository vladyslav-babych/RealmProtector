"""Per-guild serialization for setup, teardown, and destructive workflows."""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from contextlib import AbstractAsyncContextManager
from weakref import WeakKeyDictionary

from src.realm_protector.services.keyed_locks import KeyedLockPool

_pools_by_loop: "WeakKeyDictionary[asyncio.AbstractEventLoop, KeyedLockPool[int]]" = (
    WeakKeyDictionary()
)
_pools_lock = threading.Lock()
_generations: defaultdict[int, int] = defaultdict(int)
_generation_lock = threading.RLock()


def lock_for(guild_id: int) -> AbstractAsyncContextManager[None]:
    """Return a scoped guild lock that is released from memory after use."""

    loop = asyncio.get_running_loop()
    with _pools_lock:
        pool = _pools_by_loop.get(loop)
        if pool is None:
            pool = KeyedLockPool()
            _pools_by_loop[loop] = pool
    return pool.hold(int(guild_id))


def active_lock_count() -> int:
    """Return active lifecycle keys in this event loop for diagnostics/tests."""

    loop = asyncio.get_running_loop()
    with _pools_lock:
        pool = _pools_by_loop.get(loop)
    return pool.active_key_count() if pool is not None else 0


def generation(guild_id: int) -> int:
    """Return the current in-process lifecycle generation for a guild."""

    with _generation_lock:
        return _generations[int(guild_id)]


def advance(guild_id: int) -> int:
    """Invalidate work that captured an earlier guild lifecycle."""

    with _generation_lock:
        guild_key = int(guild_id)
        _generations[guild_key] += 1
        return _generations[guild_key]


def is_current(guild_id: int, expected_generation: int) -> bool:
    return generation(guild_id) == int(expected_generation)
