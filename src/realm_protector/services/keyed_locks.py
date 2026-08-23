"""Bounded lifecycle management for locks keyed by external identifiers."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Generic, Hashable, TypeVar

KeyT = TypeVar("KeyT", bound=Hashable)


@dataclass
class _Entry:
    lock: asyncio.Lock
    users: int = 0


class KeyedLockPool(Generic[KeyT]):
    """Serialize work per key without retaining every key forever.

    ``defaultdict(asyncio.Lock)`` is convenient but grows for the lifetime of the
    process.  This pool counts holders and waiters and removes an entry after the
    final user leaves it.  A short threading lock protects the bookkeeping only;
    it is never held while awaiting application work.
    """

    def __init__(self) -> None:
        self._entries: dict[KeyT, _Entry] = {}
        self._guard = threading.Lock()

    @asynccontextmanager
    async def hold(self, key: KeyT) -> AsyncIterator[None]:
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(asyncio.Lock())
                self._entries[key] = entry
            entry.users += 1

        try:
            async with entry.lock:
                yield
        finally:
            with self._guard:
                entry.users -= 1
                if entry.users == 0:
                    self._entries.pop(key, None)

    def active_key_count(self) -> int:
        """Expose current cardinality for diagnostics and regression tests."""

        with self._guard:
            return len(self._entries)


__all__ = ["KeyedLockPool"]
