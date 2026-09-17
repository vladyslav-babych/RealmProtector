import asyncio
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.realm_protector.infrastructure import document_store, guild_settings, sqlite_database
from src.realm_protector.services import utc_timer


class UtcTimerFormattingTests(unittest.TestCase):
    def test_clock_displays_each_minute_in_utc(self) -> None:
        for minute in range(60):
            with self.subTest(minute=minute), patch.object(utc_timer, "datetime") as clock:
                clock.now.return_value = datetime(2026, 9, 17, 14, minute, 47, tzinfo=timezone.utc)
                self.assertEqual(f"Realm [14:{minute:02d}]", utc_timer._format_guild_name("Realm"))
                clock.now.assert_called_once_with(timezone.utc)

    def test_next_tick_is_on_the_next_minute_including_midnight(self) -> None:
        for hour, minute, second, microsecond, expected in (
            (14, 38, 0, 0, 60.0),
            (14, 38, 27, 500000, 32.5),
            (23, 59, 59, 0, 1.0),
        ):
            with self.subTest(hour=hour, minute=minute, second=second):
                with patch.object(utc_timer, "datetime") as clock:
                    clock.now.return_value = datetime(
                        2026, 9, 17, hour, minute, second, microsecond, tzinfo=timezone.utc
                    )
                    self.assertEqual(expected, utc_timer._seconds_until_next_update())

    def test_long_names_keep_the_complete_time_suffix(self) -> None:
        with patch.object(utc_timer, "_format_utc_time", return_value="14:38"):
            result = utc_timer._format_guild_name("x" * 100)
        self.assertEqual("x" * 92 + " [14:38]", result)


class UtcTimerCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database_file = Path(self.directory.name) / "utc-timer.sqlite3"
        database_context = sqlite_database.database_path(self.database_file)
        database_context.__enter__()
        self.addCleanup(database_context.__exit__, None, None, None)
        guild_settings.set_target_guild(42, "Albion Guild")
        guild_settings.set_bot_updates_channel(42, 100)
        self.bot_member = SimpleNamespace(guild_permissions=discord.Permissions(manage_guild=True))
        self.guild = SimpleNamespace(
            id=42,
            name="Realm",
            edit=AsyncMock(),
            get_member=lambda user_id: self.bot_member,
        )
        self.member = MagicMock(spec=discord.Member)
        self.member.guild_permissions = discord.Permissions(administrator=True)
        self.interaction = SimpleNamespace(
            guild=self.guild,
            user=self.member,
            client=SimpleNamespace(user=SimpleNamespace(id=99)),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        clock_patch = patch.object(utc_timer, "_format_utc_time", return_value="14:38")
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def enable_timer(self, original_name: str = "Realm") -> None:
        guild_settings.set_utc_timer_guild_name(42, original_name)
        self.guild.name = utc_timer._format_guild_name(original_name)

    async def test_add_saves_original_name_and_uses_current_minute(self) -> None:
        await utc_timer.handle_add_utc_timer_slash(self.interaction)

        self.assertEqual("Realm", guild_settings.get_utc_timer_guild_name(42))
        self.guild.edit.assert_awaited_once_with(name="Realm [14:38]", reason="UTC timer update")
        self.interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        self.assertTrue(self.interaction.followup.send.call_args.kwargs["ephemeral"])

    async def test_repeated_add_keeps_original_name_without_another_edit(self) -> None:
        self.enable_timer()

        await utc_timer.handle_add_utc_timer_slash(self.interaction)

        self.guild.edit.assert_not_awaited()
        self.assertEqual("Realm", guild_settings.get_utc_timer_guild_name(42))
        self.assertIn("already configured", self.interaction.followup.send.call_args.args[0])

    async def test_add_failure_is_reported_and_keeps_config_for_retry(self) -> None:
        self.guild.edit.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="forbidden"), "forbidden"
        )

        with self.assertLogs(level="WARNING"):
            await utc_timer.handle_add_utc_timer_slash(self.interaction)

        self.assertEqual("Realm", guild_settings.get_utc_timer_guild_name(42))
        message = self.interaction.followup.send.call_args.args[0]
        self.assertIn("could not update", message)
        self.assertNotIn("already configured", message)

    async def test_remove_restores_name_and_only_clears_timer_settings(self) -> None:
        before = document_store.get_mapping_entry("guild_settings", 42)
        self.enable_timer("x" * 100)

        # The saved base must survive until the Discord operation succeeds.
        async def restore(**kwargs):
            self.assertEqual("x" * 100, guild_settings.get_utc_timer_guild_name(42))

        self.guild.edit.side_effect = restore

        await utc_timer.handle_remove_utc_timer_slash(self.interaction)

        self.guild.edit.assert_awaited_once_with(
            name="x" * 100, reason="Realm Protector UTC timer removed"
        )
        self.assertEqual(before, document_store.get_mapping_entry("guild_settings", 42))
        self.assertIsNone(guild_settings.get_utc_timer_guild_name(42))
        self.interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        self.assertTrue(self.interaction.followup.send.call_args.kwargs["ephemeral"])

        # A fresh database scope/startup refresh cannot re-enable the timer.
        self.guild.edit.reset_mock()
        with sqlite_database.database_path(self.database_file):
            await utc_timer.refresh_utc_timer_channels(
                SimpleNamespace(get_guild=lambda guild_id: self.guild)
            )
        self.guild.edit.assert_not_awaited()

    async def test_repeated_remove_does_not_rename_an_unconfigured_timer(self) -> None:
        await utc_timer.handle_remove_utc_timer_slash(self.interaction)

        self.guild.edit.assert_not_awaited()
        self.assertIn("no UTC timer", self.interaction.followup.send.call_args.args[0])

    async def test_remove_confirms_restoration_even_if_cached_name_matches(self) -> None:
        self.enable_timer()
        # The gateway may still show the original name after a timer edit.
        self.guild.name = "Realm"

        await utc_timer.handle_remove_utc_timer_slash(self.interaction)

        self.guild.edit.assert_awaited_once_with(
            name="Realm", reason="Realm Protector UTC timer removed"
        )
        self.assertIsNone(guild_settings.get_utc_timer_guild_name(42))

    async def test_failed_remove_preserves_saved_original_and_supports_retry(self) -> None:
        self.enable_timer()
        before = document_store.get_mapping_entry("guild_settings", 42)
        for error_type, status in ((discord.Forbidden, 403), (discord.HTTPException, 500)):
            with self.subTest(status=status):
                self.guild.edit.side_effect = error_type(
                    SimpleNamespace(status=status, reason="failed"), "failed"
                )
                with self.assertLogs(level="WARNING"):
                    await utc_timer.handle_remove_utc_timer_slash(self.interaction)

                self.assertEqual(before, document_store.get_mapping_entry("guild_settings", 42))
                self.assertIn("still enabled", self.interaction.followup.send.call_args.args[0])

        self.guild.edit.side_effect = None
        await utc_timer.handle_remove_utc_timer_slash(self.interaction)
        self.assertIsNone(guild_settings.get_utc_timer_guild_name(42))

    async def test_remove_requires_administrator(self) -> None:
        self.enable_timer()
        self.member.guild_permissions = discord.Permissions(manage_guild=True)

        await utc_timer.handle_remove_utc_timer_slash(self.interaction)

        self.guild.edit.assert_not_awaited()
        self.assertEqual("Realm", guild_settings.get_utc_timer_guild_name(42))
        self.assertIn("permission", self.interaction.response.send_message.call_args.args[0])

    async def test_remove_requires_manage_server_for_the_bot(self) -> None:
        self.enable_timer()
        self.bot_member.guild_permissions = discord.Permissions.none()

        await utc_timer.handle_remove_utc_timer_slash(self.interaction)

        self.guild.edit.assert_not_awaited()
        self.assertEqual("Realm", guild_settings.get_utc_timer_guild_name(42))
        self.assertIn("Manage Server", self.interaction.response.send_message.call_args.args[0])

    async def test_remove_rechecks_administrator_after_deferring(self) -> None:
        self.enable_timer()
        with patch.object(utc_timer.authorization, "is_admin", side_effect=[True, False]):
            await utc_timer.handle_remove_utc_timer_slash(self.interaction)

        self.guild.edit.assert_not_awaited()
        self.assertEqual("Realm", guild_settings.get_utc_timer_guild_name(42))
        self.assertIn("permission changed", self.interaction.followup.send.call_args.args[0])

    async def test_remove_is_guild_only(self) -> None:
        self.interaction.guild = None

        await utc_timer.handle_remove_utc_timer_slash(self.interaction)

        self.guild.edit.assert_not_awaited()
        self.assertIn("inside a server", self.interaction.response.send_message.call_args.args[0])

    async def test_stale_timer_snapshot_cannot_reenable_a_removed_timer(self) -> None:
        self.enable_timer()
        with patch.object(
            guild_settings, "get_all_utc_timer_guild_names", return_value={42: "Realm"}
        ):
            await utc_timer.handle_remove_utc_timer_slash(self.interaction)
            self.guild.edit.reset_mock()
            await utc_timer.refresh_utc_timer_channels(
                SimpleNamespace(get_guild=lambda guild_id: self.guild)
            )

        self.guild.edit.assert_not_awaited()


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
