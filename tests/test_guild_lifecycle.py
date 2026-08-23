import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.realm_protector.bot import configuration_panel
from src.realm_protector.services import guild_lifecycle


class GuildLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_guild_is_serialized_but_other_guild_is_independent(self) -> None:
        same_entered = asyncio.Event()
        other_entered = asyncio.Event()

        async def enter(guild_id: int, entered: asyncio.Event) -> None:
            async with guild_lifecycle.lock_for(guild_id):
                entered.set()

        async with guild_lifecycle.lock_for(10):
            same_waiter = asyncio.create_task(enter(10, same_entered))
            other_waiter = asyncio.create_task(enter(20, other_entered))
            await asyncio.sleep(0)
            self.assertFalse(same_entered.is_set())
            self.assertTrue(other_entered.is_set())
            self.assertGreaterEqual(guild_lifecycle.active_lock_count(), 1)

        await same_waiter
        await other_waiter
        self.assertTrue(same_entered.is_set())
        self.assertEqual(0, guild_lifecycle.active_lock_count())

    async def test_generation_invalidates_stale_work(self) -> None:
        guild_id = 987654
        captured = guild_lifecycle.generation(guild_id)

        self.assertTrue(guild_lifecycle.is_current(guild_id, captured))
        next_generation = guild_lifecycle.advance(guild_id)

        self.assertEqual(captured + 1, next_generation)
        self.assertFalse(guild_lifecycle.is_current(guild_id, captured))
        self.assertTrue(guild_lifecycle.is_current(guild_id, next_generation))

    async def test_configuration_panel_refresh_uses_the_lifecycle_lock(self) -> None:
        guild_id = 112233
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=guild_id),
            channel=object(),
        )
        refresh = AsyncMock(return_value=(True, "updated"))

        with (
            patch.object(
                configuration_panel.guild_settings,
                "get_configuration",
                return_value=SimpleNamespace(target_guild_name="Kingsblood"),
            ),
            patch.object(
                configuration_panel,
                "_post_or_update_bot_configuration_message_locked",
                refresh,
            ),
        ):
            async with guild_lifecycle.lock_for(guild_id):
                task = asyncio.create_task(
                    configuration_panel.post_or_update_bot_configuration_message(interaction)
                )
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                refresh.assert_not_awaited()

            self.assertEqual((True, "updated"), await task)
            refresh.assert_awaited_once_with(interaction)

    async def test_configuration_panel_is_not_posted_after_removal(self) -> None:
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=445566),
            channel=object(),
        )
        refresh = AsyncMock()

        with (
            patch.object(
                configuration_panel.guild_settings,
                "get_configuration",
                return_value=None,
            ),
            patch.object(
                configuration_panel,
                "_post_or_update_bot_configuration_message_locked",
                refresh,
            ),
        ):
            posted, message = await configuration_panel.post_or_update_bot_configuration_message(
                interaction
            )

        self.assertFalse(posted)
        self.assertIn("removed", message)
        refresh.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
