import json
import os
import sqlite3
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.realm_protector.infrastructure import (
    guild_settings,
    local_repository,
    runtime_state,
    sqlite_database,
)
from src.realm_protector.infrastructure import (
    startup_repairs as repairs,
)


class StartupRegistrationRepairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "state.sqlite3"
        self.context = sqlite_database.database_path(self.path)
        self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        guild_settings.set_target_guild(repairs.DISCORD_GUILD_ID, "TEAM CASUALTY")
        local_repository.register_player(
            repairs.LEDGER_ID, repairs.DISCORD_USER_ID, repairs.NICKNAME, repairs.WRONG_ALBION_ID
        )
        local_repository.change_balance(repairs.LEDGER_ID, repairs.DISCORD_USER_ID, 2834628)
        with sqlite_database.transaction() as database:
            database.execute(
                "UPDATE registered_players SET siphon = 57, siphon_revision = revision, "
                "siphon_synced_at = updated_at WHERE guild_id = ? AND discord_user_id = ?",
                (repairs.LEDGER_ID, repairs.DISCORD_USER_ID),
            )

    def player(self):
        with sqlite_database.connection() as database:
            return dict(
                database.execute(
                    "SELECT * FROM registered_players WHERE guild_id = ? AND discord_user_id = ?",
                    (repairs.LEDGER_ID, repairs.DISCORD_USER_ID),
                ).fetchone()
            )

    def marker(self):
        return runtime_state.get_record(
            repairs.REPAIR_KIND, repairs.DISCORD_GUILD_ID, repairs.JIMMYCOACAZA_REPAIR_ID
        )

    def execute(self, query, parameters=()):
        with sqlite_database.transaction() as database:
            return database.execute(query, parameters).rowcount

    def table_rows(self, table):
        self.assertIn(table, {"balance_history", "google_sync_outbox", "registered_players"})
        with sqlite_database.connection() as database:
            return [dict(row) for row in database.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def test_changes_only_character_id_and_keeps_complete_before_after_audit(self):
        before = self.player()
        history = self.table_rows("balance_history")
        outbox = self.table_rows("google_sync_outbox")
        with self.assertLogs(repairs.LOGGER, level="WARNING") as logs:
            (result,) = repairs.run_startup_repairs(self.path)
        self.assertEqual("applied", result.status)
        self.assertIn("applied", logs.output[0])
        self.assertEqual({**before, "albion_player_id": repairs.CORRECT_ALBION_ID}, self.player())
        self.assertEqual(history, self.table_rows("balance_history"))
        self.assertEqual(outbox, self.table_rows("google_sync_outbox"))
        marker = self.marker()
        self.assertEqual("completed", marker.status)
        self.assertEqual(before, marker.payload["before"])
        self.assertEqual(self.player(), marker.payload["after"])
        self.assertIsNone(local_repository.get_player(repairs.LEDGER_ID, 936012266321608744))

    def test_repeated_startups_are_noops_and_preserve_original_audit(self):
        repairs.run_startup_repairs(self.path)
        marker = self.marker()
        original = self.player()
        (result,) = repairs.run_startup_repairs(self.path)
        self.assertEqual("already_applied", result.status)
        self.assertEqual(marker, self.marker())
        self.assertEqual(original, self.player())

    def test_preview_does_not_write_players_or_completion_marker(self):
        before = self.player()
        (result,) = repairs.run_startup_repairs(self.path, dry_run=True)
        self.assertEqual("would_apply", result.status)
        self.assertEqual(before, self.player())
        self.assertIsNone(self.marker())

    def test_already_correct_record_is_recorded_without_player_changes(self):
        self.execute(
            "UPDATE registered_players SET albion_player_id = ?", (repairs.CORRECT_ALBION_ID,)
        )
        before = self.player()
        (result,) = repairs.run_startup_repairs(self.path)
        self.assertEqual("already_correct", result.status)
        self.assertEqual(before, self.player())
        self.assertEqual("completed", self.marker().status)

    def test_another_current_character_id_is_not_overwritten(self):
        self.execute("UPDATE registered_players SET albion_player_id = ?", ("different-character",))
        before = self.player()
        with self.assertLogs(repairs.LOGGER, level="ERROR"):
            (result,) = repairs.run_startup_repairs(self.path)
        self.assertEqual("blocked", result.status)
        self.assertEqual(before, self.player())
        self.assertEqual("blocked", self.marker().status)

    def test_changed_nickname_is_not_overwritten(self):
        self.execute(
            "UPDATE registered_players SET nickname = 'SomeoneElse', nickname_key = 'someoneelse'"
        )
        before = self.player()
        with self.assertLogs(repairs.LOGGER, level="ERROR"):
            (result,) = repairs.run_startup_repairs(self.path)
        self.assertEqual("blocked", result.status)
        self.assertEqual(before, self.player())

    def test_correct_id_owned_by_someone_else_blocks_repair(self):
        local_repository.register_player(
            repairs.LEDGER_ID, 888, "OtherAccount", repairs.CORRECT_ALBION_ID
        )
        before = self.table_rows("registered_players")
        with self.assertLogs(repairs.LOGGER, level="ERROR"):
            (result,) = repairs.run_startup_repairs(self.path)
        self.assertEqual("blocked", result.status)
        self.assertIn("888", result.message)
        self.assertEqual(before, self.table_rows("registered_players"))

    def test_inactive_player_stays_inactive(self):
        self.execute("UPDATE registered_players SET is_active = 0")
        before = self.player()
        repairs.run_startup_repairs(self.path)
        self.assertEqual({**before, "albion_player_id": repairs.CORRECT_ALBION_ID}, self.player())

    def test_archived_original_ledger_is_not_repaired(self):
        self.execute("UPDATE guild_ledger_generations SET status = 'archived'")
        before = self.player()
        with self.assertLogs(repairs.LOGGER, level="ERROR"):
            (result,) = repairs.run_startup_repairs(self.path)
        self.assertEqual("blocked", result.status)
        self.assertEqual(before, self.player())

    def test_wrong_discord_server_is_not_repaired(self):
        self.execute("UPDATE guild_ledger_generations SET discord_guild_id = 777")
        before = self.player()
        with self.assertLogs(repairs.LOGGER, level="ERROR"):
            (result,) = repairs.run_startup_repairs(self.path)
        self.assertEqual("blocked", result.status)
        self.assertEqual(before, self.player())

    def test_other_databases_are_unchanged_and_not_marked_completed(self):
        other_path = self.root / "unaffected.sqlite3"
        local_repository.ensure_schema(other_path)
        (result,) = repairs.run_startup_repairs(other_path)
        self.assertEqual("not_applicable", result.status)
        with sqlite_database.connection(other_path) as database:
            self.assertEqual(
                0, database.execute("SELECT COUNT(*) FROM runtime_records").fetchone()[0]
            )
        self.assertEqual(repairs.WRONG_ALBION_ID, self.player()["albion_player_id"])

    def test_completion_record_failure_rolls_back_character_change(self):
        before = self.player()
        with patch.object(
            repairs.runtime_state,
            "upsert_record_in_transaction",
            side_effect=RuntimeError("disk failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "disk failure"):
                repairs.run_startup_repairs(self.path)
        self.assertEqual(before, self.player())
        self.assertIsNone(self.marker())

    def test_trigger_changing_balance_rolls_back_entire_repair(self):
        self.execute("""CREATE TRIGGER unexpected_money_change AFTER UPDATE OF albion_player_id ON registered_players
            BEGIN UPDATE registered_players SET silver = silver + 1
            WHERE guild_id = NEW.guild_id AND discord_user_id = NEW.discord_user_id; END""")
        before = self.player()
        with self.assertRaisesRegex(RuntimeError, "fields other than"):
            repairs.run_startup_repairs(self.path)
        self.assertEqual(before, self.player())
        self.assertIsNone(self.marker())

    def test_two_simultaneous_runs_apply_exactly_once(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(lambda _: repairs.run_startup_repairs(self.path)[0].status, range(2))
            )
        self.assertCountEqual(["applied", "already_applied"], results)
        self.assertEqual(
            repairs.WRONG_ALBION_ID, self.marker().payload["before"]["albion_player_id"]
        )

    def test_repaired_link_frees_mamaliga_without_transferring_balances(self):
        repairs.run_startup_repairs(self.path)
        registered = local_repository.register_player(
            repairs.LEDGER_ID, 936012266321608744, "Mamaliga", repairs.WRONG_ALBION_ID
        )
        self.assertEqual(local_repository.RegistrationStatus.CREATED, registered.status)
        self.assertEqual(0, registered.player.silver)
        self.assertEqual(2834628, self.player()["silver"])

    def test_normal_storage_initialization_runs_repair(self):
        from main import initialize_local_storage

        report = initialize_local_storage(self.root)
        self.assertFalse(report.failed)
        self.assertEqual(repairs.CORRECT_ALBION_ID, self.player()["albion_player_id"])
        self.assertEqual("completed", self.marker().status)

    def test_script_defaults_to_preview_and_respects_project_environment_path(self):
        script_path = Path(__file__).resolve().parents[1] / "scripts/repair_registration_links.py"
        (self.root / ".env").write_text(
            "REALM_PROTECTOR_DATABASE_PATH=state.sqlite3\n", encoding="utf-8"
        )
        environment = os.environ.copy()
        environment.pop("REALM_PROTECTOR_DATABASE_PATH", None)
        command = [sys.executable, str(script_path), "--project-root", str(self.root)]
        completed = subprocess.run(
            command, env=environment, capture_output=True, text=True, check=False
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertEqual(str(self.path.resolve()), output["database_path"])
        self.assertTrue(output["dry_run"])
        self.assertEqual("would_apply", output["repairs"][0]["status"])
        self.assertIsNone(self.marker())
        applied = subprocess.run(
            [*command, "--apply"], env=environment, capture_output=True, text=True, check=False
        )
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertEqual("applied", json.loads(applied.stdout)["repairs"][0]["status"])
        self.assertEqual("completed", self.marker().status)

    def test_missing_or_symlink_database_is_rejected_without_creation(self):
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(ValueError):
            repairs.run_startup_repairs(missing)
        self.assertFalse(missing.exists())
        linked = self.root / "linked.sqlite3"
        linked.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            repairs.run_startup_repairs(linked)

    def test_preview_reads_committed_wal_rows(self):
        # Keep the writer connected so its committed changes remain in WAL.
        writer = sqlite3.connect(self.path)
        try:
            writer.execute("PRAGMA wal_autocheckpoint = 0")
            writer.execute(
                "UPDATE registered_players SET albion_player_id = ?", (repairs.CORRECT_ALBION_ID,)
            )
            writer.commit()
            (result,) = repairs.run_startup_repairs(self.path, dry_run=True)
            self.assertEqual("already_correct", result.status)
            self.assertIsNone(self.marker())
        finally:
            writer.close()
