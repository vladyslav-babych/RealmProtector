import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.realm_protector.bot import sync_commands
from src.realm_protector.infrastructure import local_repository
from src.realm_protector.services import google_sync, sync_operations


class SyncOperationsTests(unittest.IsolatedAsyncioTestCase):
    def test_health_uses_active_ledger_and_reports_current_siphon_only(self) -> None:
        outbox = local_repository.OutboxStatus(
            guild_id=71,
            pending_events=2,
            processing_events=1,
            completed_events=8,
            dead_letter_events=3,
            oldest_incomplete_at="2026-08-22T10:00:00+00:00",
            last_completed_at="2026-08-22T10:01:00+00:00",
            latest_error="network",
        )
        players = [
            SimpleNamespace(
                siphon=-5,
                siphon_revision=4,
                revision=4,
                siphon_synced_at="2026-08-22T10:02:00+00:00",
            ),
            SimpleNamespace(
                siphon=10,
                siphon_revision=2,
                revision=3,
                siphon_synced_at="2026-08-22T10:03:00+00:00",
            ),
        ]
        with (
            patch.object(
                sync_operations.guild_settings,
                "get_configuration",
                return_value=SimpleNamespace(target_guild_name="Realm"),
            ),
            patch.object(
                sync_operations.local_repository,
                "get_active_ledger",
                return_value=SimpleNamespace(ledger_id=71),
            ),
            patch.object(
                sync_operations.document_store,
                "get_google_sheet_link",
                return_value={"status": "active"},
            ),
            patch.object(
                sync_operations.credential_store,
                "get_credentials_info",
                return_value={"guild_name": "Realm"},
            ),
            patch.object(
                sync_operations.google_sync,
                "is_cutover_ready",
                return_value=True,
            ),
            patch.object(
                sync_operations.local_repository,
                "list_active_players",
                return_value=players,
            ),
            patch.object(
                sync_operations.local_repository,
                "get_outbox_status",
                return_value=outbox,
            ),
        ):
            health = sync_operations.get_sync_health(7)

        self.assertEqual(71, health.ledger_id)
        self.assertEqual(2, health.active_players)
        self.assertEqual(1, health.current_siphon_players)
        self.assertEqual("2026-08-22T10:03:00+00:00", health.latest_siphon_sync_at)
        self.assertIs(outbox, health.outbox)

    async def test_retry_restores_dead_letters_for_ledger_then_flushes(self) -> None:
        projection = google_sync.SyncResult(True, "done", processed_events=4)
        with (
            patch.object(
                sync_operations.credential_store,
                "get_credentials_info",
                return_value={"guild_name": "Realm"},
            ),
            patch.object(
                sync_operations.local_repository,
                "get_active_ledger",
                return_value=SimpleNamespace(ledger_id=71),
            ),
            patch.object(
                sync_operations.local_repository,
                "retry_dead_letter_outbox_for_guild",
                return_value=3,
            ) as retry,
            patch.object(
                sync_operations.google_sync,
                "flush_outbox",
                new=AsyncMock(return_value=projection),
            ) as flush,
        ):
            result = await sync_operations.retry_dead_letters_and_flush(7)

        self.assertEqual(3, result.retried_dead_letters)
        self.assertIs(projection, result.projection)
        retry.assert_called_once_with(71, limit=100, reset_attempts=True)
        flush.assert_awaited_once_with(7, limit=100)

    async def test_retry_does_not_move_dead_letters_without_credentials(self) -> None:
        with (
            patch.object(
                sync_operations.credential_store,
                "get_credentials_info",
                return_value={},
            ),
            patch.object(
                sync_operations.local_repository,
                "retry_dead_letter_outbox_for_guild",
            ) as retry,
        ):
            result = await sync_operations.retry_dead_letters_and_flush(7)

        self.assertFalse(result.projection.success)
        retry.assert_not_called()


class SyncCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_siphon_uses_economy_access_and_reports_ephemerally(self) -> None:
        class FakeMember:
            id = 9

        result = google_sync.SyncResult(
            True,
            "Siphon synchronized from Google Sheets.",
            updated_siphon_rows=3,
            expected_siphon_rows=3,
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=7),
            user=FakeMember(),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        command = next(
            item
            for item in sync_commands.create_sync_commands(SimpleNamespace())
            if item.name == "sync-siphon"
        )
        with (
            patch.object(sync_commands.discord, "Member", FakeMember),
            patch.object(
                sync_commands.economy_access,
                "has_economy_access",
                new=AsyncMock(return_value=True),
            ) as has_access,
            patch.object(
                sync_commands.google_sync,
                "sync_siphon_from_sheet",
                new=AsyncMock(return_value=result),
            ) as sync_siphon,
            patch.object(
                sync_commands._SIPHON_SYNC_COOLDOWN,
                "claim",
                return_value=0,
            ),
        ):
            await command.callback(interaction)

        has_access.assert_awaited_once_with(interaction.user, 7)
        sync_siphon.assert_awaited_once_with(7)
        interaction.response.defer.assert_awaited_once_with(
            ephemeral=True,
            thinking=True,
        )
        interaction.followup.send.assert_awaited_once()
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        self.assertIn("3/3", interaction.followup.send.await_args.args[0])

    async def test_status_is_admin_only_and_ephemeral(self) -> None:
        class FakeMember:
            id = 9

        health = sync_operations.SyncHealth(
            discord_guild_id=7,
            target_guild_name="Realm",
            ledger_id=None,
            google_link_status="not linked",
            google_credentials_readable=False,
            cutover_ready=True,
            active_players=0,
            current_siphon_players=0,
            latest_siphon_sync_at=None,
            outbox=None,
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=7),
            user=FakeMember(),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        command = next(
            item
            for item in sync_commands.create_sync_commands(SimpleNamespace())
            if item.name == "sync-status"
        )
        with (
            patch.object(sync_commands.discord, "Member", FakeMember),
            patch.object(sync_commands, "is_admin", new=AsyncMock(return_value=True)),
            patch.object(
                sync_commands.sync_operations,
                "get_sync_health",
                return_value=health,
            ),
        ):
            await command.callback(interaction)

        interaction.response.defer.assert_awaited_once_with(
            ephemeral=True,
            thinking=True,
        )
        interaction.followup.send.assert_awaited_once()
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        self.assertIn(
            "Google synchronization status",
            interaction.followup.send.await_args.args[0],
        )


if __name__ == "__main__":
    unittest.main()
