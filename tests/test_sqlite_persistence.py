import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.realm_protector.infrastructure import (
    document_store,
    guild_settings,
    legacy_migration,
    sqlite_database,
)


class SQLiteDatabaseTests(unittest.TestCase):
    def test_initialization_applies_security_pragmas_and_core_schema(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "private" / "state.sqlite3"

            sqlite_database.initialize_database(database_path)

            self.assertEqual(0o600, stat.S_IMODE(database_path.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(database_path.parent.stat().st_mode))
            with sqlite_database.connection(database_path) as database:
                self.assertEqual("wal", database.execute("PRAGMA journal_mode").fetchone()[0])
                self.assertEqual(1, database.execute("PRAGMA foreign_keys").fetchone()[0])
                self.assertEqual(
                    sqlite_database.BUSY_TIMEOUT_MILLISECONDS,
                    database.execute("PRAGMA busy_timeout").fetchone()[0],
                )
                self.assertEqual(2, database.execute("PRAGMA synchronous").fetchone()[0])
                tables = {
                    row["name"]
                    for row in database.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                schema_versions = database.execute(
                    "SELECT version, name FROM schema_migrations"
                ).fetchall()

            self.assertTrue(
                {
                    "schema_migrations",
                    "legacy_imports",
                    "configuration_documents",
                    "google_sheet_links",
                    "runtime_records",
                }.issubset(tables)
            )
            self.assertEqual(
                [
                    (1, "core persistence tables"),
                    (2, "track ignored legacy source changes"),
                ],
                [tuple(row) for row in schema_versions],
            )

    def test_database_path_context_restores_the_previous_configuration(self) -> None:
        previous_path = sqlite_database.get_database_path()
        with TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory) / "temporary.sqlite3"
            with sqlite_database.database_path(temporary_path):
                self.assertEqual(temporary_path, sqlite_database.get_database_path())

        self.assertEqual(previous_path, sqlite_database.get_database_path())

    def test_relative_database_path_cannot_escape_project_root(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory) / "project"
            project_root.mkdir()
            with self.assertRaisesRegex(ValueError, "inside the project root"):
                sqlite_database.resolve_project_database_path(
                    project_root,
                    "../outside.sqlite3",
                )
            self.assertEqual(
                project_root.resolve() / "data/state.sqlite3",
                sqlite_database.resolve_project_database_path(
                    project_root,
                    "data/state.sqlite3",
                ),
            )

    def test_database_and_data_directory_symbolic_links_are_rejected(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target_database = root / "target.sqlite3"
            target_database.touch()
            linked_database = root / "linked.sqlite3"
            linked_database.symlink_to(target_database)

            with self.assertRaisesRegex(OSError, "symbolic link"):
                sqlite_database.initialize_database(linked_database)

            target_directory = root / "target-data"
            target_directory.mkdir()
            linked_directory = root / "linked-data"
            linked_directory.symlink_to(target_directory, target_is_directory=True)
            with self.assertRaisesRegex(OSError, "symbolic link"):
                sqlite_database.initialize_database(linked_directory / "state.sqlite3")

    def test_migration_script_honors_project_environment_database_path(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "scripts" / "migrate_to_sqlite.py"
        with TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            (project_root / ".env").write_text(
                "REALM_PROTECTOR_DATABASE_PATH=custom/state.sqlite3\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.pop("REALM_PROTECTOR_DATABASE_PATH", None)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(script_path),
                    "--project-root",
                    str(project_root),
                    "--skip-google",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            report = json.loads(completed.stdout)
            expected_path = project_root.resolve() / "custom" / "state.sqlite3"
            self.assertEqual(str(expected_path), report["database_path"])
            self.assertTrue(expected_path.is_file())
            self.assertFalse((project_root / "data" / "realm_protector.sqlite3").exists())


class SQLiteConfigurationAdapterTests(unittest.TestCase):
    def test_existing_configuration_apis_read_and_write_only_sqlite(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "state.sqlite3"
            with sqlite_database.database_path(database_path):
                guild_settings.set_target_guild(
                    42,
                    "King's Blood",
                    member_role_id=100,
                    caller_role_ids=[200],
                )
                document_store.upsert_mapping_entry(
                    "tickets",
                    42,
                    {"panels": [{"id": "applications"}]},
                )

                self.assertEqual("King's Blood", guild_settings.get_target_guild(42))
                self.assertEqual(100, guild_settings.get_member_role_id(42))
                self.assertEqual([200], guild_settings.get_caller_role_ids(42))
                self.assertEqual(
                    {"42": {"panels": [{"id": "applications"}]}},
                    document_store.load_mapping("tickets"),
                )

            self.assertTrue(database_path.is_file())

    def test_row_level_entries_share_an_outer_atomic_transaction(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "state.sqlite3"
            sqlite_database.initialize_database(database_path)

            with self.assertRaises(RuntimeError):
                with sqlite_database.transaction(database_path) as database:
                    document_store.upsert_mapping_entry(
                        "tickets",
                        10,
                        {"panel": "applications"},
                        database=database,
                    )
                    document_store.upsert_mapping_entry(
                        "objectives",
                        10,
                        {"channel_id": 20},
                        database=database,
                    )
                    raise RuntimeError("rollback")

            self.assertIsNone(
                document_store.get_mapping_entry(
                    "tickets",
                    10,
                    database_path=database_path,
                )
            )
            self.assertIsNone(
                document_store.get_mapping_entry(
                    "objectives",
                    10,
                    database_path=database_path,
                )
            )

    def test_corrupt_configuration_json_is_reported_not_hidden(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "state.sqlite3"
            with sqlite_database.transaction(database_path) as database:
                database.execute(
                    """
                    INSERT INTO configuration_documents (
                        namespace, guild_id, payload_json
                    ) VALUES ('tickets', '10', '{not-json')
                    """
                )

            with self.assertRaises(document_store.DocumentCorruptionError):
                document_store.load_mapping(
                    "tickets",
                    database_path=database_path,
                )


class LegacyConfigurationMigrationTests(unittest.TestCase):
    @staticmethod
    def _write_json(path: Path, document: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    def test_import_is_idempotent_and_never_overwrites_local_rows(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "data" / "state.sqlite3"
            guilds_path = root / "configs" / "guilds_config.json"
            links_path = root / "google_sheet_credentials" / "credentials_links.json"
            guilds = {"42": {"guild_name": "Legacy Guild"}}
            links = {
                "42": {
                    "credentials_file": "42_credentials.json",
                    "google_sheet_name": "Legacy Ledger",
                }
            }
            self._write_json(guilds_path, guilds)
            self._write_json(links_path, links)
            original_links_bytes = links_path.read_bytes()

            first_report = legacy_migration.migrate_legacy_storage(
                root,
                database_path=database_path,
            )

            self.assertFalse(first_report.failed)
            self.assertEqual(2, first_report.imported_records)
            self.assertEqual(original_links_bytes, links_path.read_bytes())
            self.assertEqual(
                guilds,
                document_store.load_mapping(
                    "guild_settings",
                    database_path=database_path,
                ),
            )
            self.assertEqual(
                links,
                document_store.load_google_sheet_links(
                    database_path=database_path,
                ),
            )

            document_store.save_mapping(
                "guild_settings",
                {"42": {"guild_name": "Local Authority"}},
                database_path=database_path,
            )
            changed_legacy = {
                "42": {"guild_name": "Stale Legacy Value"},
                "84": {"guild_name": "New Legacy Guild"},
            }
            self._write_json(guilds_path, changed_legacy)
            second_report = legacy_migration.migrate_legacy_storage(
                root,
                database_path=database_path,
            )
            stored_guilds = document_store.load_mapping(
                "guild_settings",
                database_path=database_path,
            )

            self.assertFalse(second_report.failed)
            self.assertEqual(
                {"guild_name": "Local Authority"},
                stored_guilds["42"],
            )
            self.assertNotIn("84", stored_guilds)
            changed_guild_result = next(
                source
                for source in second_report.sources
                if source.source_key == "legacy-json:guild-settings"
            )
            self.assertEqual("already_imported", changed_guild_result.status)
            self.assertIn("ignored", changed_guild_result.message)

            third_report = legacy_migration.migrate_legacy_storage(
                root,
                database_path=database_path,
            )
            existing_source_statuses = {
                source.source_key: source.status
                for source in third_report.sources
                if source.source_path in {str(guilds_path), str(links_path)}
            }
            self.assertEqual(
                {
                    "legacy-json:guild-settings": "already_imported",
                    "legacy-json:google-sheet-links": "already_imported",
                },
                existing_source_statuses,
            )
            with sqlite_database.connection(database_path) as database:
                ledger_count = database.execute("SELECT COUNT(*) FROM legacy_imports").fetchone()[0]
            self.assertEqual(2, ledger_count)

    def test_invalid_source_is_reported_without_blocking_other_sources(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "state.sqlite3"
            guilds_path = root / "configs" / "guilds_config.json"
            tickets_path = root / "configs" / "tickets_config.json"
            guilds_path.parent.mkdir(parents=True)
            guilds_path.write_text("not-json", encoding="utf-8")
            self._write_json(tickets_path, {"42": {"panels": []}})

            report = legacy_migration.migrate_legacy_storage(
                root,
                database_path=database_path,
            )

            statuses = {source.source_key: source.status for source in report.sources}
            self.assertTrue(report.failed)
            self.assertEqual("failed", statuses["legacy-json:guild-settings"])
            self.assertEqual("imported", statuses["legacy-json:tickets"])
            self.assertEqual(
                {"42": {"panels": []}},
                document_store.load_mapping(
                    "tickets",
                    database_path=database_path,
                ),
            )

    def test_malformed_backup_after_success_cannot_break_restart(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "state.sqlite3"
            guilds_path = root / "configs" / "guilds_config.json"
            self._write_json(guilds_path, {"42": {"guild_name": "Realm"}})

            first = legacy_migration.migrate_legacy_storage(
                root,
                database_path=database_path,
            )
            guilds_path.write_text("not-json-anymore", encoding="utf-8")
            second = legacy_migration.migrate_legacy_storage(
                root,
                database_path=database_path,
            )

            self.assertFalse(first.failed)
            self.assertFalse(second.failed)
            guild_result = next(
                source
                for source in second.sources
                if source.source_key == "legacy-json:guild-settings"
            )
            self.assertEqual("already_imported", guild_result.status)
            self.assertIn("malformed", guild_result.message)
            self.assertEqual(
                {"42": {"guild_name": "Realm"}},
                document_store.load_mapping(
                    "guild_settings",
                    database_path=database_path,
                ),
            )


if __name__ == "__main__":
    unittest.main()
