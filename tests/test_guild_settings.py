import tempfile
import unittest
from pathlib import Path

from src.realm_protector.domain.models import LeaveAction
from src.realm_protector.infrastructure import (
    document_store,
    guild_settings,
    local_repository,
    sqlite_database,
)


class GuildSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_context = sqlite_database.database_path(
            Path(self.temporary_directory.name) / "guild-settings.sqlite3"
        )
        self.database_context.__enter__()
        local_repository.ensure_schema()

    def tearDown(self) -> None:
        self.database_context.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    def test_returns_one_validated_immutable_configuration_snapshot(self) -> None:
        guild_settings.set_target_guild(
            10,
            "  King's Blood  ",
            "Member",
            "Caller, Raid Lead",
            "Treasurer",
            LeaveAction.KICK.value,
            member_role_id=100,
            caller_role_ids=[200, 201],
            economy_manager_role_ids=[300],
        )
        self.assertTrue(guild_settings.set_bot_updates_channel(10, 400))
        self.assertTrue(guild_settings.set_bot_configuration_message(10, 401, 402))

        configuration = guild_settings.get_configuration(10)

        self.assertIsNotNone(configuration)
        assert configuration is not None
        self.assertEqual("King's Blood", configuration.target_guild_name)
        self.assertEqual(("Caller", "Raid Lead"), configuration.caller_role_names)
        self.assertEqual((200, 201), configuration.caller_role_ids)
        self.assertEqual(LeaveAction.KICK, configuration.leave_action)
        self.assertEqual(400, configuration.bot_updates_channel_id)
        self.assertEqual((401, 402), guild_settings.get_bot_configuration_message(10))

    def test_target_ownership_conflict_is_database_enforced_and_atomic(self) -> None:
        guild_settings.set_target_guild(10, "King's Blood")

        with self.assertRaises(local_repository.TargetGuildConflictError):
            guild_settings.set_target_guild(11, "  KING'S BLOOD ")

        self.assertIsNone(guild_settings.get_configuration(11))
        self.assertEqual("10", guild_settings.get_server_id_by_target_guild("king's blood"))

    def test_target_change_archives_old_ledger_and_quarantines_sheet_link(self) -> None:
        guild_settings.set_target_guild(10, "Old Guild")
        first_ledger = local_repository.get_active_ledger(10)
        document_store.upsert_google_sheet_link(
            10,
            {
                "status": "active",
                "guild_name": "Old Guild",
                "credentials_file": "10_old.json",
            },
        )

        guild_settings.set_target_guild(10, "New Guild")

        generations = local_repository.list_ledger_generations(10)
        self.assertEqual(2, len(generations))
        assert first_ledger is not None
        self.assertFalse(
            next(item for item in generations if item.ledger_id == first_ledger.ledger_id).is_active
        )
        self.assertEqual("New Guild", local_repository.get_active_ledger(10).target_guild_name)
        link = document_store.get_google_sheet_link(10)
        self.assertEqual("quarantined", link["status"])
        self.assertEqual("target_guild_changed", link["quarantine_reason"])

    def test_corrupt_json_is_not_treated_as_missing_configuration(self) -> None:
        with sqlite_database.transaction() as database:
            database.execute(
                """
                INSERT INTO configuration_documents (
                    namespace, guild_id, payload_json
                ) VALUES ('guild_settings', '10', ?)
                """,
                ("{not-json",),
            )

        with self.assertRaises(document_store.DocumentCorruptionError):
            guild_settings.get_configuration(10)

    def test_row_update_does_not_rewrite_another_guild(self) -> None:
        guild_settings.set_target_guild(10, "First")
        guild_settings.set_target_guild(11, "Second")
        second_before = document_store.get_mapping_entry("guild_settings", 11)

        self.assertTrue(guild_settings.set_utc_timer_guild_name(10, "Base Name"))

        self.assertEqual(
            second_before,
            document_store.get_mapping_entry("guild_settings", 11),
        )
        self.assertFalse(guild_settings.set_bot_updates_channel(12, 500))
        self.assertIsNone(document_store.get_mapping_entry("guild_settings", 12))


if __name__ == "__main__":
    unittest.main()
