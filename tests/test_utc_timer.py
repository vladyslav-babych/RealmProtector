import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.realm_protector.services import utc_timer


class UtcTimerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_scheduler_is_idempotent_and_resets_singleton(self) -> None:
        previous_task = utc_timer._TIMER_TASK
        blocker = asyncio.Event()
        task = asyncio.create_task(blocker.wait())
        utc_timer._TIMER_TASK = task
        try:
            await asyncio.gather(
                utc_timer.stop_utc_timer_scheduler(),
                utc_timer.stop_utc_timer_scheduler(),
            )
            await utc_timer.stop_utc_timer_scheduler()

            self.assertTrue(task.cancelled())
            self.assertIsNone(utc_timer._TIMER_TASK)
        finally:
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            utc_timer._TIMER_TASK = previous_task

    async def test_scheduler_does_not_rename_an_unconfigured_guild(self) -> None:
        guild = SimpleNamespace(id=42)
        bot = SimpleNamespace(get_guild=lambda guild_id: guild)

        with (
            patch.object(
                utc_timer.guild_settings,
                "get_all_utc_timer_guild_names",
                return_value={42: "Realm"},
            ),
            patch.object(
                utc_timer.guild_settings,
                "get_target_guild",
                return_value=None,
            ),
            patch.object(
                utc_timer.guild_settings,
                "get_utc_timer_guild_name",
                return_value="Realm",
            ),
            patch.object(
                utc_timer,
                "_sync_guild_name",
                new=AsyncMock(),
            ) as sync_name,
        ):
            await utc_timer._refresh_all_timer_guilds(bot)

        sync_name.assert_not_awaited()

    async def test_scheduler_renames_only_the_current_configured_timer(self) -> None:
        guild = SimpleNamespace(id=42)
        bot = SimpleNamespace(get_guild=lambda guild_id: guild)

        with (
            patch.object(
                utc_timer.guild_settings,
                "get_all_utc_timer_guild_names",
                return_value={42: "Realm"},
            ),
            patch.object(
                utc_timer.guild_settings,
                "get_target_guild",
                return_value="Albion Guild",
            ),
            patch.object(
                utc_timer.guild_settings,
                "get_utc_timer_guild_name",
                return_value="Realm",
            ),
            patch.object(
                utc_timer,
                "_sync_guild_name",
                new=AsyncMock(),
            ) as sync_name,
        ):
            await utc_timer._refresh_all_timer_guilds(bot)

        sync_name.assert_awaited_once_with(guild, "Realm")


if __name__ == "__main__":
    unittest.main()
