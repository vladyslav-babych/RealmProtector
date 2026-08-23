"""Bounded retention for replaceable SQLite synchronization artifacts."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.realm_protector.infrastructure import local_repository

LOGGER = logging.getLogger(__name__)
MAINTENANCE_INTERVAL_SECONDS = 24 * 60 * 60
OUTBOX_RETENTION_DAYS = 30
SHEET_SNAPSHOT_RETENTION_DAYS = 30
_PRUNE_BATCH_LIMIT = 1_000
_MAX_BATCHES_PER_TICK = 10
_maintenance_task: Optional[asyncio.Task] = None


@dataclass(frozen=True)
class RetentionResult:
    completed_outbox_events: int
    applied_sheet_snapshots: int


def _drain_bounded(prune, cutoff: datetime) -> int:
    removed = 0
    for _ in range(_MAX_BATCHES_PER_TICK):
        batch = prune(cutoff, limit=_PRUNE_BATCH_LIMIT)
        removed += batch
        if batch < _PRUNE_BATCH_LIMIT:
            break
    return removed


def run_local_retention(*, now: Optional[datetime] = None) -> RetentionResult:
    """Prune derived delivery artifacts while retaining business audit history."""

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    outbox_cutoff = current - timedelta(days=OUTBOX_RETENTION_DAYS)
    snapshot_cutoff = current - timedelta(days=SHEET_SNAPSHOT_RETENTION_DAYS)
    return RetentionResult(
        completed_outbox_events=_drain_bounded(
            local_repository.prune_completed_outbox,
            outbox_cutoff,
        ),
        applied_sheet_snapshots=_drain_bounded(
            local_repository.prune_applied_sheet_snapshots,
            snapshot_cutoff,
        ),
    )


async def _maintenance_loop() -> None:
    while True:
        try:
            result = await asyncio.to_thread(run_local_retention)
            if result.completed_outbox_events or result.applied_sheet_snapshots:
                LOGGER.info(
                    "Local retention pruned %s completed projection events and %s applied Sheet snapshots",
                    result.completed_outbox_events,
                    result.applied_sheet_snapshots,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Local retention tick failed")
        await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)


def start_local_maintenance() -> None:
    global _maintenance_task
    if _maintenance_task is not None and not _maintenance_task.done():
        return
    _maintenance_task = asyncio.create_task(
        _maintenance_loop(),
        name="realm-protector-local-maintenance",
    )


async def stop_local_maintenance() -> None:
    global _maintenance_task
    task, _maintenance_task = _maintenance_task, None
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        LOGGER.exception("Local retention worker failed during shutdown")


__all__ = [
    "MAINTENANCE_INTERVAL_SECONDS",
    "OUTBOX_RETENTION_DAYS",
    "SHEET_SNAPSHOT_RETENTION_DAYS",
    "RetentionResult",
    "run_local_retention",
    "start_local_maintenance",
    "stop_local_maintenance",
]
