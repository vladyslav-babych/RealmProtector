"""Bounded executors for blocking Albion and Google client adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import TypeVar
from weakref import WeakKeyDictionary

T = TypeVar("T")

_ALBION_WORKERS = 8
_GOOGLE_WORKERS = 4
_ALBION_EXECUTOR = ThreadPoolExecutor(
    max_workers=_ALBION_WORKERS,
    thread_name_prefix="realm-albion",
)
_GOOGLE_EXECUTOR = ThreadPoolExecutor(
    max_workers=_GOOGLE_WORKERS,
    thread_name_prefix="realm-google",
)
_albion_slots: "WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    WeakKeyDictionary()
)
_google_slots: "WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    WeakKeyDictionary()
)


def _slots_for_current_loop(
    slots: "WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]",
    limit: int,
) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = slots.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        slots[loop] = semaphore
    return semaphore


async def _run_bounded(
    executor: ThreadPoolExecutor,
    slots: "WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]",
    limit: int,
    function: Callable[..., T],
    *args,
    **kwargs,
) -> T:
    loop = asyncio.get_running_loop()
    semaphore = _slots_for_current_loop(slots, limit)
    async with semaphore:
        operation = partial(function, *args, **kwargs)
        future = loop.run_in_executor(executor, operation)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # A running thread cannot be cancelled. Keep its slot reserved until
            # completion so repeated cancelled requests cannot grow the queue.
            try:
                await asyncio.shield(future)
            finally:
                raise


async def run_albion(function: Callable[..., T], *args, **kwargs) -> T:
    """Run one blocking Albion request without growing the default executor."""

    return await _run_bounded(
        _ALBION_EXECUTOR,
        _albion_slots,
        _ALBION_WORKERS,
        function,
        *args,
        **kwargs,
    )


async def run_google(function: Callable[..., T], *args, **kwargs) -> T:
    """Run one blocking Google/worksheet operation with a bounded queue."""

    return await _run_bounded(
        _GOOGLE_EXECUTOR,
        _google_slots,
        _GOOGLE_WORKERS,
        function,
        *args,
        **kwargs,
    )
