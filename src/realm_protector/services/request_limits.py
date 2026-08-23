"""Small in-process cooldowns for public, external-API-heavy workflows."""

from __future__ import annotations

import threading
import time
from collections.abc import Hashable
from typing import Optional


class Cooldown:
    """Atomically claim one operation per key within a rolling time window."""

    def __init__(self, seconds: float, *, max_entries: int = 10_000) -> None:
        if seconds <= 0:
            raise ValueError("seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self.seconds = float(seconds)
        self.max_entries = int(max_entries)
        self._deadlines: dict[Hashable, float] = {}
        self._lock = threading.Lock()

    def claim(self, key: Hashable, *, now: Optional[float] = None) -> float:
        """Return zero when accepted, otherwise seconds until the next claim."""

        current = time.monotonic() if now is None else float(now)
        with self._lock:
            deadline = self._deadlines.get(key, 0.0)
            if deadline > current:
                return deadline - current

            if len(self._deadlines) >= self.max_entries:
                self._deadlines = {
                    stored_key: stored_deadline
                    for stored_key, stored_deadline in self._deadlines.items()
                    if stored_deadline > current
                }
                if len(self._deadlines) >= self.max_entries:
                    oldest_key = min(self._deadlines, key=self._deadlines.__getitem__)
                    self._deadlines.pop(oldest_key, None)

            self._deadlines[key] = current + self.seconds
            return 0.0
