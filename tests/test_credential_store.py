import inspect
import json
import sqlite3
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.realm_protector.infrastructure import credential_store, sqlite_database

VALID_CREDENTIALS = json.dumps(
    {
        "client_email": "realm-protector@example.test",
        "private_key": "-----BEGIN PRIVATE KEY-----\ntest-only\n-----END PRIVATE KEY-----\n",
        "project_id": "realm-protector-tests",
    }
)


class CredentialStoreTests(unittest.IsolatedAsyncioTestCase):
    async def _resolve(self, value):
        if inspect.isawaitable(value):
            return await value
        return value

    def test_sanitized_guild_name_cannot_contain_a_path_traversal(self) -> None:
        sanitized = credential_store._sanitize_guild_name("../../King's\\Blood")

        self.assertTrue(sanitized)
        self.assertNotIn("/", sanitized)
        self.assertNotIn("\\", sanitized)
        self.assertNotIn(sanitized, {".", ".."})

    def test_credentials_file_validation_rejects_directories(self) -> None:
        for value in ("../escape.json", "nested/escape.json", "/tmp/escape.json", ".", ".."):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    credential_store._validated_credentials_file_name(value)

    async def test_credentials_stay_inside_store_and_are_owner_readable_only(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            credentials_directory = Path(temporary_directory) / "credentials"
            links_file = credentials_directory / "credentials_links.json"
            database_file = Path(temporary_directory) / "state.sqlite3"

            with (
                patch.object(
                    credential_store,
                    "_CREDENTIALS_DIR",
                    credentials_directory,
                ),
                patch.object(credential_store, "_LINKS_FILE", links_file),
                sqlite_database.database_path(database_file),
            ):
                success, _message = await self._resolve(
                    credential_store.link_google_sheet_credentials(
                        discord_server_id=42,
                        guild_name="../../Realm/Protector",
                        credentials_text=VALID_CREDENTIALS,
                        google_sheet_name="Realm Ledger",
                        google_worksheet_name="Players",
                    )
                )
                links = credential_store._load_links()

            self.assertTrue(success)
            filename = links["42"]["credentials_file"]
            credential_path = credentials_directory / filename

            self.assertEqual(filename, Path(filename).name)
            self.assertEqual(credentials_directory.resolve(), credential_path.resolve().parent)
            self.assertTrue(credential_path.is_file())
            self.assertEqual(0o600, stat.S_IMODE(credential_path.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(database_file.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(credentials_directory.stat().st_mode))
            self.assertFalse(links_file.exists())

    async def test_invalid_json_writes_no_credentials(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            credentials_directory = Path(temporary_directory) / "credentials"
            links_file = credentials_directory / "credentials_links.json"
            database_file = Path(temporary_directory) / "state.sqlite3"

            with (
                patch.object(
                    credential_store,
                    "_CREDENTIALS_DIR",
                    credentials_directory,
                ),
                patch.object(credential_store, "_LINKS_FILE", links_file),
                sqlite_database.database_path(database_file),
            ):
                success, _message = await self._resolve(
                    credential_store.link_google_sheet_credentials(
                        discord_server_id=42,
                        guild_name="Realm Protector",
                        credentials_text="not-json",
                    )
                )

            self.assertFalse(success)
            self.assertFalse(credentials_directory.exists())

    async def test_link_metadata_failure_rolls_back_new_credentials(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            credentials_directory = Path(temporary_directory) / "credentials"
            links_file = credentials_directory / "credentials_links.json"
            database_file = Path(temporary_directory) / "state.sqlite3"

            with (
                patch.object(credential_store, "_CREDENTIALS_DIR", credentials_directory),
                patch.object(credential_store, "_LINKS_FILE", links_file),
                patch.object(credential_store, "_upsert_link", side_effect=OSError),
                sqlite_database.database_path(database_file),
            ):
                success, _message = await self._resolve(
                    credential_store.link_google_sheet_credentials(
                        discord_server_id=42,
                        guild_name="Realm Protector",
                        credentials_text=VALID_CREDENTIALS,
                    )
                )

            self.assertFalse(success)
            self.assertEqual([], list(credentials_directory.glob("*.json")))

    async def test_link_metadata_failure_restores_existing_credentials(self) -> None:
        replacement_credentials = json.dumps(
            {
                "client_email": "replacement@example.test",
                "private_key": "replacement-key",
                "project_id": "replacement-project",
            }
        )
        with TemporaryDirectory() as temporary_directory:
            credentials_directory = Path(temporary_directory) / "credentials"
            links_file = credentials_directory / "credentials_links.json"
            database_file = Path(temporary_directory) / "state.sqlite3"

            with (
                patch.object(credential_store, "_CREDENTIALS_DIR", credentials_directory),
                patch.object(credential_store, "_LINKS_FILE", links_file),
                sqlite_database.database_path(database_file),
            ):
                success, _message = await self._resolve(
                    credential_store.link_google_sheet_credentials(
                        discord_server_id=42,
                        guild_name="Realm Protector",
                        credentials_text=VALID_CREDENTIALS,
                    )
                )
                self.assertTrue(success)
                links_before = credential_store._load_links()
                credential_path = next(credentials_directory.glob("*.json"))
                credentials_before = json.loads(credential_path.read_text(encoding="utf-8"))

                with patch.object(
                    credential_store,
                    "_upsert_link",
                    side_effect=OSError,
                ):
                    success, _message = await self._resolve(
                        credential_store.link_google_sheet_credentials(
                            discord_server_id=42,
                            guild_name="Realm Protector",
                            credentials_text=replacement_credentials,
                        )
                    )
                self.assertEqual(links_before, credential_store._load_links())

            self.assertFalse(success)
            self.assertEqual(
                credentials_before,
                json.loads(credential_path.read_text(encoding="utf-8")),
            )

    def test_malformed_link_entry_is_ignored(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            credentials_directory = Path(temporary_directory) / "credentials"
            links_file = credentials_directory / "credentials_links.json"
            database_file = Path(temporary_directory) / "state.sqlite3"

            with (
                patch.object(credential_store, "_CREDENTIALS_DIR", credentials_directory),
                patch.object(credential_store, "_LINKS_FILE", links_file),
                sqlite_database.database_path(database_file),
            ):
                credential_store._save_links({"42": "malformed"})

                self.assertEqual({}, credential_store.get_credentials_info(42))
                credential_store.remove_google_sheet_credentials(42)
                self.assertEqual({}, credential_store._load_links())

    async def test_same_named_guilds_receive_server_isolated_credentials(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            credentials_directory = Path(temporary_directory) / "credentials"
            links_file = credentials_directory / "credentials_links.json"
            database_file = Path(temporary_directory) / "state.sqlite3"

            with (
                patch.object(
                    credential_store,
                    "_CREDENTIALS_DIR",
                    credentials_directory,
                ),
                patch.object(credential_store, "_LINKS_FILE", links_file),
                sqlite_database.database_path(database_file),
            ):
                first_success, _ = await self._resolve(
                    credential_store.link_google_sheet_credentials(
                        discord_server_id=10,
                        guild_name="Shared Guild Name",
                        credentials_text=VALID_CREDENTIALS,
                    )
                )
                second_success, _ = await self._resolve(
                    credential_store.link_google_sheet_credentials(
                        discord_server_id=20,
                        guild_name="Shared Guild Name",
                        credentials_text=VALID_CREDENTIALS,
                    )
                )

                links = credential_store._load_links()
                first_file = links["10"]["credentials_file"]
                second_file = links["20"]["credentials_file"]

                self.assertTrue(first_success)
                self.assertTrue(second_success)
                self.assertNotEqual(first_file, second_file)
                self.assertTrue(first_file.startswith("10_"))
                self.assertTrue(second_file.startswith("20_"))
                self.assertTrue((credentials_directory / first_file).is_file())
                self.assertTrue((credentials_directory / second_file).is_file())

                credential_store.remove_google_sheet_credentials(10)

                self.assertFalse((credentials_directory / first_file).exists())
                self.assertTrue((credentials_directory / second_file).is_file())

    async def test_guild_identity_resource_ids_and_quarantine_are_preserved(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            credentials_directory = Path(temporary_directory) / "credentials"
            database_file = Path(temporary_directory) / "state.sqlite3"
            with (
                patch.object(
                    credential_store,
                    "_CREDENTIALS_DIR",
                    credentials_directory,
                ),
                sqlite_database.database_path(database_file),
            ):
                success, _ = await self._resolve(
                    credential_store.link_google_sheet_credentials(
                        discord_server_id=42,
                        guild_name="King's Blood",
                        credentials_text=VALID_CREDENTIALS,
                    )
                )
                self.assertTrue(success)
                self.assertTrue(
                    credential_store.record_google_resource_ids(
                        42,
                        spreadsheet_id="sheet-key",
                        worksheet_type="players",
                        worksheet_id=123,
                    )
                )
                active = credential_store.get_credentials_info(42)
                self.assertEqual("King's Blood", active["guild_name"])
                self.assertEqual("sheet-key", active["spreadsheet_id"])
                self.assertEqual(123, active["players_worksheet_id"])

                with sqlite_database.transaction() as database:
                    quarantined = credential_store.quarantine_google_sheet_link(
                        42,
                        "target_guild_changed",
                        database=database,
                    )

                self.assertEqual("quarantined", quarantined["status"])
                self.assertEqual({}, credential_store.get_credentials_info(42))
                linked = credential_store._load_links()["42"]
                credential_file = credentials_directory / linked["credentials_file"]
                self.assertTrue(credential_file.is_file())

                credential_store.remove_google_sheet_credentials(42)
                self.assertFalse(credential_file.exists())

    async def test_removal_preserves_a_legacy_file_still_referenced_by_another_guild(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            credentials_directory = Path(temporary_directory) / "credentials"
            links_file = credentials_directory / "credentials_links.json"
            database_file = Path(temporary_directory) / "state.sqlite3"

            with (
                patch.object(
                    credential_store,
                    "_CREDENTIALS_DIR",
                    credentials_directory,
                ),
                patch.object(credential_store, "_LINKS_FILE", links_file),
                sqlite_database.database_path(database_file),
            ):
                shared_filename = "legacy_shared_credentials.json"
                shared_path = credentials_directory / shared_filename
                credential_store._write_private_json(
                    shared_path,
                    json.loads(VALID_CREDENTIALS),
                )
                credential_store._save_links(
                    {
                        "10": {"credentials_file": shared_filename},
                        "20": {"credentials_file": shared_filename},
                    }
                )

                credential_store.remove_google_sheet_credentials(10)

                self.assertTrue(shared_path.is_file())
                remaining_links = credential_store._load_links()
                self.assertEqual({"20"}, set(remaining_links))

                credential_store.remove_google_sheet_credentials(20)

                self.assertFalse(shared_path.exists())

    async def test_relink_update_and_remove_preserve_other_guild_metadata(self) -> None:
        replacement_credentials = json.dumps(
            {
                "client_email": "replacement@example.test",
                "private_key": "replacement-key",
                "project_id": "replacement-project",
            }
        )
        with TemporaryDirectory() as temporary_directory:
            credentials_directory = Path(temporary_directory) / "credentials"
            database_file = Path(temporary_directory) / "state.sqlite3"

            with (
                patch.object(
                    credential_store,
                    "_CREDENTIALS_DIR",
                    credentials_directory,
                ),
                sqlite_database.database_path(database_file),
            ):
                for guild_id, sheet_name in ((10, "First"), (20, "Second")):
                    success, _ = await self._resolve(
                        credential_store.link_google_sheet_credentials(
                            discord_server_id=guild_id,
                            guild_name=f"Guild {guild_id}",
                            credentials_text=VALID_CREDENTIALS,
                            google_sheet_name=sheet_name,
                        )
                    )
                    self.assertTrue(success)

                success, _ = await self._resolve(
                    credential_store.link_google_sheet_credentials(
                        discord_server_id=10,
                        guild_name="Guild 10",
                        credentials_text=replacement_credentials,
                        google_sheet_name="First replacement",
                    )
                )
                self.assertTrue(success)
                updated, _ = credential_store.update_credentials_link_field(
                    10,
                    "google_worksheet_name",
                    "Registered Players",
                )
                self.assertTrue(updated)

                links = credential_store._load_links()
                self.assertEqual({"10", "20"}, set(links))
                self.assertEqual("First replacement", links["10"]["google_sheet_name"])
                self.assertEqual(
                    "Registered Players",
                    links["10"]["google_worksheet_name"],
                )
                self.assertEqual("Second", links["20"]["google_sheet_name"])

                credential_store.remove_google_sheet_credentials(10)
                remaining = credential_store._load_links()
                self.assertEqual({"20"}, set(remaining))
                self.assertEqual("Second", remaining["20"]["google_sheet_name"])

    async def test_database_write_failure_rolls_back_secret_without_losing_other_guilds(
        self,
    ) -> None:
        replacement_credentials = json.dumps(
            {
                "client_email": "replacement@example.test",
                "private_key": "replacement-key",
                "project_id": "replacement-project",
            }
        )
        with TemporaryDirectory() as temporary_directory:
            credentials_directory = Path(temporary_directory) / "credentials"
            database_file = Path(temporary_directory) / "state.sqlite3"

            with (
                patch.object(
                    credential_store,
                    "_CREDENTIALS_DIR",
                    credentials_directory,
                ),
                sqlite_database.database_path(database_file),
            ):
                for guild_id in (10, 20):
                    success, _ = await self._resolve(
                        credential_store.link_google_sheet_credentials(
                            discord_server_id=guild_id,
                            guild_name=f"Guild {guild_id}",
                            credentials_text=VALID_CREDENTIALS,
                        )
                    )
                    self.assertTrue(success)

                links_before = credential_store._load_links()
                guild_10_path = credentials_directory / links_before["10"]["credentials_file"]
                secret_before = guild_10_path.read_text(encoding="utf-8")
                with patch.object(
                    credential_store,
                    "_upsert_link",
                    side_effect=sqlite3.OperationalError("database unavailable"),
                ):
                    success, _ = await self._resolve(
                        credential_store.link_google_sheet_credentials(
                            discord_server_id=10,
                            guild_name="Guild 10",
                            credentials_text=replacement_credentials,
                        )
                    )

                self.assertFalse(success)
                self.assertEqual(links_before, credential_store._load_links())
                self.assertEqual(secret_before, guild_10_path.read_text(encoding="utf-8"))

    def test_database_read_failure_is_not_treated_as_a_missing_link(self) -> None:
        with patch.object(
            credential_store,
            "_get_link",
            side_effect=sqlite3.OperationalError("database unavailable"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                credential_store.get_credentials_info(42)
            with self.assertRaises(sqlite3.OperationalError):
                credential_store.update_credentials_link_field(
                    42,
                    "google_sheet_name",
                    "Realm",
                )

        with patch.object(
            credential_store.document_store,
            "load_google_sheet_links",
            side_effect=sqlite3.OperationalError("database unavailable"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                credential_store._load_links()

    async def test_reference_check_failure_retains_the_secret_after_unlink(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            credentials_directory = Path(temporary_directory) / "credentials"
            database_file = Path(temporary_directory) / "state.sqlite3"

            with (
                patch.object(
                    credential_store,
                    "_CREDENTIALS_DIR",
                    credentials_directory,
                ),
                sqlite_database.database_path(database_file),
            ):
                success, _ = await self._resolve(
                    credential_store.link_google_sheet_credentials(
                        discord_server_id=42,
                        guild_name="Realm Protector",
                        credentials_text=VALID_CREDENTIALS,
                    )
                )
                self.assertTrue(success)
                link = credential_store._load_links()["42"]
                secret_path = credentials_directory / link["credentials_file"]

                with patch.object(
                    credential_store.document_store,
                    "is_google_credentials_file_referenced",
                    side_effect=sqlite3.OperationalError("database unavailable"),
                ):
                    with self.assertRaises(sqlite3.OperationalError):
                        credential_store.remove_google_sheet_credentials(42)

                self.assertTrue(secret_path.is_file())
                self.assertEqual({}, credential_store._load_links())


if __name__ == "__main__":
    unittest.main()
