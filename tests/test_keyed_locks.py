import asyncio
import unittest

from src.realm_protector.services.keyed_locks import KeyedLockPool


class KeyedLockPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_key_is_serialized_and_released(self) -> None:
        pool: KeyedLockPool[int] = KeyedLockPool()
        active = 0
        maximum_active = 0

        async def worker() -> None:
            nonlocal active, maximum_active
            async with pool.hold(7):
                active += 1
                maximum_active = max(maximum_active, active)
                await asyncio.sleep(0)
                active -= 1

        await asyncio.gather(*(worker() for _ in range(5)))

        self.assertEqual(1, maximum_active)
        self.assertEqual(0, pool.active_key_count())

    async def test_different_keys_can_run_together(self) -> None:
        pool: KeyedLockPool[int] = KeyedLockPool()
        both_started = asyncio.Event()
        started = 0

        async def worker(key: int) -> None:
            nonlocal started
            async with pool.hold(key):
                started += 1
                if started == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), timeout=1)

        await asyncio.gather(worker(1), worker(2))
        self.assertEqual(0, pool.active_key_count())


if __name__ == "__main__":
    unittest.main()
