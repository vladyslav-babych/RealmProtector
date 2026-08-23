import tempfile
import unittest
from pathlib import Path

from src.realm_protector.infrastructure import (
    document_store,
    guild_settings,
    local_repository,
    sqlite_database,
)
from src.realm_protector.services import configuration_lifecycle


class ConfigurationLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_context = sqlite_database.database_path(
            Path(self.temporary_directory.name) / "configuration-lifecycle.sqlite3"
        )
        self.database_context.__enter__()
        local_repository.ensure_schema()

    def tearDown(self) -> None:
        self.database_context.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    def test_removal_atomically_disables_routes_and_records_cleanup_intent(self) -> None:
        guild_settings.set_target_guild(
            10,
            "King's Blood",
            bot_updates_channel_id=90,
        )
        guild_settings.set_bot_configuration_message(10, 91, 92)
        guild_settings.set_utc_timer_guild_name(10, "Base Guild Name")
        document_store.upsert_mapping_entry(
            "tickets",
            10,
            {"panels": {"one": {"active": True}}},
        )
        document_store.upsert_mapping_entry(
            "reaction_roles",
            10,
            {"panels": {"two": {"active": True}}},
        )
        document_store.upsert_mapping_entry(
            "objectives",
            10,
            {"objectives": [{"id": "objective"}]},
        )
        document_store.upsert_google_sheet_link(
            10,
            {"status": "active", "credentials_file": "10_credentials.json"},
        )

        removed = configuration_lifecycle.begin_guild_configuration_removal(10)

        self.assertIsNotNone(removed)
        self.assertIsNone(guild_settings.get_configuration(10))
        self.assertIsNone(local_repository.get_active_ledger(10))
        for namespace in ("tickets", "reaction_roles"):
            entry = document_store.get_mapping_entry(namespace, 10)
            self.assertTrue(entry["disabled"])
            self.assertTrue(next(iter(entry["panels"].values()))["disabled"])
            self.assertFalse(next(iter(entry["panels"].values()))["active"])
        self.assertTrue(document_store.get_mapping_entry("objectives", 10)["disabled"])
        self.assertEqual(
            "quarantined",
            document_store.get_google_sheet_link(10)["status"],
        )
        pending = configuration_lifecycle.get_pending_removal(10)
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(91, pending.payload["configuration_channel_id"])
        self.assertEqual("Base Guild Name", pending.payload["utc_timer_guild_name"])
        self.assertTrue(configuration_lifecycle.complete_guild_configuration_removal(10))
        self.assertIsNone(configuration_lifecycle.get_pending_removal(10))

    def test_invalid_feature_document_rolls_back_the_entire_removal(self) -> None:
        guild_settings.set_target_guild(10, "King's Blood")
        document_store.upsert_mapping_entry("tickets", 10, "not-an-object")

        with self.assertRaises(configuration_lifecycle.ConfigurationRemovalError):
            configuration_lifecycle.begin_guild_configuration_removal(10)

        self.assertEqual("King's Blood", guild_settings.get_target_guild(10))
        self.assertIsNotNone(local_repository.get_active_ledger(10))
        self.assertIsNone(configuration_lifecycle.get_pending_removal(10))


if __name__ == "__main__":
    unittest.main()
