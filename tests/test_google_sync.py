from __future__ import annotations

import asyncio
import gc
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import AsyncMock, patch

from src.realm_protector.infrastructure import (
    guild_settings,
    local_repository,
    sqlite_database,
)
from src.realm_protector.services import google_sync


class FakeWorksheet:
    def __init__(self, rows: list[list[Any]], *, worksheet_id: int = 1) -> None:
        self.rows = [list(row) for row in rows]
        self.id = worksheet_id
        self.spreadsheet_id = f"spreadsheet-{worksheet_id}"
        self.batch_updates: list[tuple[list[dict], str]] = []
        self.batch_clears: list[list[str]] = []
        self.updates: list[tuple[str, list[list[Any]]]] = []
        self.appended_rows: list[list[Any]] = []
        self.value_render_options: list[str | None] = []

    def row_values(self, row_index: int) -> list[Any]:
        if row_index <= 0 or row_index > len(self.rows):
            return []
        return list(self.rows[row_index - 1])

    def get_all_values(self, **kwargs) -> list[list[Any]]:
        self.value_render_options.append(kwargs.get("value_render_option"))
        return [list(row) for row in self.rows]

    def col_values(self, column_index: int) -> list[Any]:
        return [row[column_index - 1] if len(row) >= column_index else "" for row in self.rows]

    def update(self, range_name: str, values: list[list[Any]], **_kwargs) -> None:
        self.updates.append((range_name, [list(row) for row in values]))
        if range_name == "F1:G1":
            while len(self.rows[0]) < 7:
                self.rows[0].append("")
            self.rows[0][5:7] = list(values[0])
            return
        if range_name == "F1:F1":
            while len(self.rows[0]) < 6:
                self.rows[0].append("")
            self.rows[0][5] = values[0][0]
            return
        if range_name == "H1:H1":
            while len(self.rows[0]) < 8:
                self.rows[0].append("")
            self.rows[0][7] = values[0][0]

    def batch_update(self, requests: list[dict], *, value_input_option: str) -> None:
        self.batch_updates.append((requests, value_input_option))
        for request in requests:
            start, end = request["range"].split(":")
            start_column = ord(start[0]) - ord("A")
            end_column = ord(end[0]) - ord("A")
            row_index = int(start[1:]) - 1
            while len(self.rows) <= row_index:
                self.rows.append([])
            while len(self.rows[row_index]) <= end_column:
                self.rows[row_index].append("")
            self.rows[row_index][start_column : end_column + 1] = list(request["values"][0])

    def batch_clear(self, ranges: list[str]) -> None:
        self.batch_clears.append(list(ranges))
        for range_name in ranges:
            start, end = range_name.split(":")
            start_column = ord(start) - ord("A")
            end_column = ord(end) - ord("A")
            for row in self.rows:
                for column in range(start_column, end_column + 1):
                    if column < len(row):
                        row[column] = ""

    def append_row(self, row: list[Any], **_kwargs) -> None:
        self.appended_rows.append(list(row))

    def append_rows(self, rows: list[list[Any]], **_kwargs) -> None:
        self.appended_rows.extend(list(row) for row in rows)


class GoogleSyncTests(unittest.TestCase):
    @staticmethod
    def _batch_resolver(resolver):
        def get_worksheets(
            guild_id: int,
            worksheet_types=("players", "lootsplit_history", "balance_history"),
        ):
            return {
                worksheet_type: resolver(guild_id, worksheet_type)
                for worksheet_type in worksheet_types
            }

        return get_worksheets

    @staticmethod
    def _projection_worksheets(players: FakeWorksheet) -> dict[str, FakeWorksheet]:
        return {
            "players": players,
            "balance_history": FakeWorksheet(
                [["Date", "Reason", "Officer", "Nickname", "Amount"]],
                worksheet_id=2,
            ),
            "lootsplit_history": FakeWorksheet(
                [
                    [
                        "Battleboard ID",
                        "Date",
                        "Officer",
                        "Content name",
                        "Caller",
                        "Participant",
                        "Lootsplit",
                    ]
                ],
                worksheet_id=3,
            ),
        }

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "bot.sqlite3"
        self.database_context = sqlite_database.database_path(self.database_path)
        self.database_context.__enter__()
        local_repository.ensure_schema(self.database_path)
        guild_settings.set_target_guild(10, "Test Albion Guild")
        self.credentials = {
            "guild_name": "Test Albion Guild",
            "credentials_file": str(Path(self.temporary_directory.name) / "creds.json"),
            "google_sheet_name": "Realm",
            "google_worksheet_name": "Players",
            "lootsplit_history_worksheet_name": "Lootsplit History",
            "balance_history_worksheet_name": "Balance History",
        }

    def tearDown(self) -> None:
        self.database_context.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    @staticmethod
    def _complete_cutover() -> None:
        cutover = local_repository.begin_sheet_import(
            10,
            "google-bootstrap-v1",
            "manual-siphon-test-cutover",
        )
        local_repository.complete_sheet_import(cutover.import_id)

    def test_unformatted_numeric_zero_is_not_treated_as_a_blank_siphon(self) -> None:
        self.assertEqual(0, google_sync._parse_integer(0, allow_blank=True))
        self.assertEqual(12, google_sync._parse_integer(12.0, allow_blank=True))

    def test_sync_lock_pool_serializes_a_guild_and_releases_unused_locks(self) -> None:
        guild_id = 999
        start = threading.Barrier(2)
        state_guard = threading.Lock()
        active = 0
        maximum_active = 0

        def operation() -> None:
            nonlocal active, maximum_active
            start.wait()
            with google_sync._sync_lock(guild_id):
                with state_guard:
                    active += 1
                    maximum_active = max(maximum_active, active)
                time.sleep(0.02)
                with state_guard:
                    active -= 1

        threads = [threading.Thread(target=operation) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(1, maximum_active)
        gc.collect()
        self.assertNotIn(guild_id, google_sync._SYNC_LOCKS)

    def test_stop_google_sync_cancels_awaits_and_resets_the_worker(self) -> None:
        async def scenario() -> None:
            cancelled = False

            async def worker() -> None:
                nonlocal cancelled
                try:
                    await asyncio.Future()
                finally:
                    cancelled = True

            task = asyncio.create_task(worker())
            google_sync._sync_task = task
            await asyncio.sleep(0)

            await google_sync.stop_google_sync()
            await google_sync.stop_google_sync()

            self.assertTrue(cancelled)
            self.assertTrue(task.cancelled())
            self.assertIsNone(google_sync._sync_task)

        asyncio.run(scenario())

    def test_background_worker_flushes_projection_without_importing_siphon(self) -> None:
        async def scenario() -> None:
            with (
                patch.object(
                    google_sync.document_store,
                    "load_google_sheet_links",
                    return_value={10: {"status": "active"}},
                ),
                patch.object(
                    google_sync,
                    "bootstrap_guild",
                    new=AsyncMock(return_value=google_sync.SyncResult(True, "ready")),
                ),
                patch.object(
                    google_sync,
                    "flush_outbox",
                    new=AsyncMock(return_value=google_sync.SyncResult(True, "done")),
                ) as flush_outbox,
                patch.object(
                    google_sync,
                    "sync_siphon_from_sheet",
                    new=AsyncMock(),
                ) as sync_siphon,
                patch.object(
                    google_sync.asyncio,
                    "sleep",
                    new=AsyncMock(side_effect=asyncio.CancelledError),
                ),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await google_sync._sync_loop()

            flush_outbox.assert_awaited_once_with(10)
            sync_siphon.assert_not_awaited()

        asyncio.run(scenario())

    def test_immediate_player_projection_is_a_noop_without_a_google_link(self) -> None:
        with (
            patch.object(
                google_sync.document_store,
                "get_google_sheet_link",
                return_value=None,
            ),
            patch.object(
                google_sync,
                "_project_linked_players_unlocked",
            ) as project_players,
        ):
            result = google_sync.project_linked_players_sync(10, (20,))

        self.assertIsNone(result)
        project_players.assert_not_called()

    def test_immediate_player_projection_bypasses_blocked_outbox_and_writes_numeric_silver(
        self,
    ) -> None:
        local_repository.register_player(10, 20, "Player")
        self._complete_cutover()
        older_event = local_repository.claim_pending_outbox(
            "blocked-worker",
            guild_id=10,
        )[0]
        local_repository.fail_outbox(
            older_event.event_id,
            "temporary history failure",
            retry_after_seconds=3600,
            worker_id="blocked-worker",
        )
        local_repository.change_balance(10, 20, 3, idempotency_key="credit")
        worksheet = FakeWorksheet(
            [
                [
                    "Discord ID",
                    "Albion Nickname",
                    "Is In Guild",
                    "Silver",
                    "Siphon",
                    "Custom",
                ],
                ["20", "Manual name", "NO", "'0", -9, "keep me"],
            ]
        )
        with (
            patch.object(
                google_sync.document_store,
                "get_google_sheet_link",
                return_value={"status": "active"},
            ),
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
                return_value={"players": worksheet},
            ),
        ):
            result = google_sync.project_linked_players_sync(10, (20,))

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.success)
        self.assertEqual(["20", "Player", "YES", 3, -9, "keep me"], worksheet.rows[1])
        self.assertIsInstance(worksheet.rows[1][3], int)
        self.assertTrue(local_repository.has_incomplete_outbox(guild_id=10))

    def test_immediate_player_projection_writes_every_requested_lootsplit_player(
        self,
    ) -> None:
        local_repository.register_player(10, 20, "First")
        local_repository.register_player(10, 21, "Second")
        self._complete_cutover()
        lootsplit = local_repository.apply_lootsplit(
            10,
            ("First", "Second"),
            7,
            idempotency_key="lootsplit",
        )
        worksheet = FakeWorksheet(
            [
                ["Discord ID", "Albion Nickname", "Is In Guild", "Silver", "Siphon"],
                ["20", "First", "YES", 0, -1],
                ["21", "Second", "YES", 0, -2],
            ]
        )
        with (
            patch.object(
                google_sync.document_store,
                "get_google_sheet_link",
                return_value={"status": "active"},
            ),
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
                return_value={"players": worksheet},
            ),
        ):
            result = google_sync.project_linked_players_sync(
                10,
                tuple(credit.discord_user_id for credit in lootsplit.credits),
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.success)
        self.assertEqual(7, worksheet.rows[1][3])
        self.assertEqual(7, worksheet.rows[2][3])
        self.assertEqual(-1, worksheet.rows[1][4])
        self.assertEqual(-2, worksheet.rows[2][4])

    def test_immediate_player_projection_rejects_duplicate_discord_id_rows(self) -> None:
        local_repository.register_player(10, 20, "Player")
        self._complete_cutover()
        worksheet = FakeWorksheet(
            [
                ["Discord ID", "Albion Nickname", "Is In Guild", "Silver", "Siphon"],
                ["20", "Player", "YES", 0, -1],
                ["20", "Duplicate", "YES", 0, -2],
            ]
        )
        with (
            patch.object(
                google_sync.document_store,
                "get_google_sheet_link",
                return_value={"status": "active"},
            ),
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
                return_value={"players": worksheet},
            ),
        ):
            result = google_sync.project_linked_players_sync(10, (20,))

        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.success)
        self.assertIn("duplicate Discord ID 20", result.message)
        self.assertEqual([], worksheet.batch_updates)

    def test_bootstrap_imports_players_and_histories_once(self) -> None:
        players = FakeWorksheet(
            [
                ["Discord ID", "Albion Nickname", "Is In Guild", "Silver", "Siphon"],
                ["20", "Player", "YES", "500", "-10"],
            ]
        )
        balance = FakeWorksheet(
            [
                ["Date", "Reason", "Officer", "Nickname", "Amount"],
                ["03/24/26 17:44 UTC", "Payout", "Officer", "Player", "-50"],
            ]
        )
        lootsplit = FakeWorksheet(
            [
                [
                    "Battleboard ID",
                    "Date",
                    "Officer",
                    "Content name",
                    "Caller",
                    "Participant",
                    "Lootsplit",
                ],
                ["123", "03/24/26 17:44 UTC", "Officer", "Fight", "Caller", "Player", "100"],
            ]
        )

        def get_worksheet(_guild_id: int, worksheet_type: str = "players"):
            return {
                "players": players,
                "balance_history": balance,
                "lootsplit_history": lootsplit,
            }[worksheet_type]

        with (
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
                side_effect=self._batch_resolver(get_worksheet),
            ),
        ):
            first = google_sync.bootstrap_guild_sync(10)
            second = google_sync.bootstrap_guild_sync(10)

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        player = local_repository.get_player(10, 20)
        self.assertIsNotNone(player)
        assert player is not None
        self.assertEqual(500, player.silver)
        self.assertEqual(-10, player.siphon)
        self.assertEqual(["20", "Player", "YES", 500, "-10"], players.rows[1])
        with sqlite_database.connection() as database:
            self.assertEqual(
                1,
                database.execute(
                    "SELECT COUNT(*) FROM balance_history WHERE event_kind = 'sheet_import'"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                database.execute("SELECT COUNT(*) FROM imported_lootsplit_history").fetchone()[0],
            )

    def test_projection_header_adoption_clears_only_owned_legacy_columns(self) -> None:
        worksheet = FakeWorksheet(
            [
                [
                    "Discord ID",
                    "Albion Nickname",
                    "Is In Guild",
                    "Silver",
                    "Siphon",
                    "Realm Registration ID",
                    "Realm Revision",
                    "Operator Notes",
                ],
                ["20", "Player", "YES", 3, -1, "10:20", "4", "keep"],
            ]
        )

        google_sync._ensure_players_projection_headers(worksheet)

        self.assertEqual("Siphon", worksheet.rows[0][4])
        self.assertEqual([["F:F", "G:G"]], worksheet.batch_clears)
        self.assertEqual(["", "", "Operator Notes"], worksheet.rows[0][5:8])
        self.assertEqual(["", "", "keep"], worksheet.rows[1][5:8])

    def test_projection_header_adoption_preserves_repurposed_trailing_columns(self) -> None:
        worksheet = FakeWorksheet(
            [
                [
                    "Discord ID",
                    "Albion Nickname",
                    "Is In Guild",
                    "Silver",
                    "Siphon",
                    "Custom F",
                    "Custom G",
                ]
            ]
        )

        google_sync._ensure_players_projection_headers(worksheet)

        self.assertEqual([], worksheet.batch_clears)
        self.assertEqual("Custom F", worksheet.rows[0][5])
        self.assertEqual("Custom G", worksheet.rows[0][6])

    def test_player_projection_does_not_reuse_a_row_with_custom_trailing_data(self) -> None:
        local_repository.register_player(10, 20, "Player")
        worksheet = FakeWorksheet(
            [
                [
                    "Discord ID",
                    "Albion Nickname",
                    "Is In Guild",
                    "Silver",
                    "Siphon",
                    "Operator Notes",
                ],
                ["", "", "", "", "", "keep this row"],
            ]
        )

        google_sync._reconcile_player_projection(worksheet, 10)

        self.assertEqual(["", "", "", "", "", "keep this row"], worksheet.rows[1])
        self.assertEqual("20", worksheet.rows[2][0])

    def test_linked_guild_identity_mismatch_fails_before_sheet_access(self) -> None:
        mismatched = dict(self.credentials, guild_name="Another Albion Guild")
        with (
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=mismatched,
            ),
            patch.object(google_sync.google_sheets, "get_worksheets") as get_sheets,
        ):
            result = google_sync.bootstrap_guild_sync(10)

        self.assertFalse(result.success)
        self.assertIn("different configured Albion guild", result.message)
        get_sheets.assert_not_called()

    def test_oversized_sheet_integer_is_quarantined_without_blocking_cutover(self) -> None:
        players = FakeWorksheet(
            [
                ["Discord ID", "Albion Nickname", "Is In Guild", "Silver", "Siphon"],
                [str(1 << 80), "Too Large", "YES", "10", "-1"],
                ["20", "Valid", "YES", "25", "-2"],
            ]
        )
        worksheets = self._projection_worksheets(players)

        with (
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
                return_value=worksheets,
            ),
        ):
            result = google_sync.bootstrap_guild_sync(10)

        self.assertTrue(result.success)
        self.assertIsNotNone(local_repository.get_player(10, 20))
        issues = local_repository.list_migration_issues(guild_id=10)
        self.assertEqual(["invalid_player_row"], [issue.code for issue in issues])

    def test_interrupted_bootstrap_reuses_immutable_staged_snapshot(self) -> None:
        players = FakeWorksheet(
            [
                ["Discord ID", "Albion Nickname", "Is In Guild", "Silver", "Siphon"],
                ["20", "Frozen Player", "YES", "500", "-10"],
            ]
        )
        balance = FakeWorksheet(
            [
                ["Date", "Reason", "Officer", "Nickname", "Amount"],
                ["03/24/26 17:44 UTC", "Frozen", "Officer", "Frozen Player", "-50"],
            ]
        )
        lootsplit = FakeWorksheet(
            [
                [
                    "Battleboard ID",
                    "Date",
                    "Officer",
                    "Content name",
                    "Caller",
                    "Participant",
                    "Lootsplit",
                ]
            ]
        )

        def get_worksheet(_guild_id: int, worksheet_type: str = "players"):
            return {
                "players": players,
                "balance_history": balance,
                "lootsplit_history": lootsplit,
            }[worksheet_type]

        with (
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
                side_effect=self._batch_resolver(get_worksheet),
            ),
            patch.object(
                google_sync,
                "_import_history_rows",
                side_effect=RuntimeError("simulated process interruption"),
            ),
        ):
            interrupted = google_sync.bootstrap_guild_sync(10)
        self.assertFalse(interrupted.success)
        staged = local_repository.get_staged_sheet_snapshot(
            10,
            "google-bootstrap-v1",
        )
        self.assertIsNotNone(staged)

        # These edits happen after the crash and must not become part of the
        # in-progress cutover on restart.
        players.rows[1] = ["21", "Edited Player", "YES", "999", "-99"]
        balance.rows[1][4] = "-999"
        players.rows[0][0] = "Edited Header"
        with (
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
                side_effect=self._batch_resolver(get_worksheet),
            ),
        ):
            resumed = google_sync.bootstrap_guild_sync(10)

        # Projection is still rejected because the live Sheet schema is bad,
        # but the authoritative local cutover consumes the staged snapshot
        # before consulting that changed schema.
        self.assertFalse(resumed.success)
        self.assertIsNotNone(local_repository.get_player(10, 20))
        self.assertIsNone(local_repository.get_player(10, 21))
        imported_history = list(local_repository.iter_balance_history(10))
        self.assertEqual([-50], [row["actual_delta"] for row in imported_history])
        self.assertIsNone(
            local_repository.get_staged_sheet_snapshot(
                10,
                "google-bootstrap-v1",
            )
        )
        players.rows[0][0] = "Discord ID"
        with (
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
                side_effect=self._batch_resolver(get_worksheet),
            ),
        ):
            projected = google_sync.bootstrap_guild_sync(10)
        self.assertTrue(projected.success)

    def test_outbox_projects_player_and_is_acknowledged(self) -> None:
        local_repository.register_player(10, 20, "Player")
        players = FakeWorksheet(
            [["Discord ID", "Albion Nickname", "Is In Guild", "Silver", "Siphon"]]
        )
        with (
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
                return_value=self._projection_worksheets(players),
            ),
        ):
            result = google_sync.flush_outbox_sync(10)

        self.assertTrue(result.success)
        self.assertEqual(1, result.processed_events)
        self.assertEqual(1, len(players.batch_updates))
        self.assertIsInstance(players.rows[1][3], int)
        self.assertEqual(0, players.rows[1][3])
        self.assertEqual([], local_repository.list_pending_outbox(guild_id=10))

    def test_player_reconciliation_reads_sheet_rows_once_for_the_whole_batch(self) -> None:
        for discord_id in (20, 21, 22):
            local_repository.register_player(10, discord_id, f"Player {discord_id}")
        players = FakeWorksheet(
            [["Discord ID", "Albion Nickname", "Is In Guild", "Silver", "Siphon"]]
        )

        google_sync._reconcile_player_projection(players, 10)

        self.assertEqual([None], players.value_render_options)
        self.assertEqual(1, len(players.batch_updates))
        self.assertEqual(3, len(players.batch_updates[0][0]))

    def test_projection_rebuild_repairs_managed_rows_without_acknowledging_outbox(
        self,
    ) -> None:
        local_repository.register_player(10, 20, "Player")
        local_repository.apply_lootsplit(
            10,
            ["Player"],
            25,
            actor_name="Invoker",
            officer_name="Officer",
            idempotency_key="rebuild-lootsplit",
        )
        cutover = local_repository.begin_sheet_import(
            10,
            "google-bootstrap-v1",
            "rebuild-test-cutover",
        )
        local_repository.complete_sheet_import(cutover.import_id)
        players = FakeWorksheet(
            [
                ["Discord ID", "Albion Nickname", "Is In Guild", "Silver", "Siphon"],
                ["20", "Stale", "NO", "0", "=ARRAYFORMULA(...)"],
            ]
        )
        worksheets = self._projection_worksheets(players)

        with (
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
                return_value=worksheets,
            ) as get_worksheets,
        ):
            result = google_sync.rebuild_projection_sync(10)

        self.assertTrue(result.success)
        get_worksheets.assert_called_once_with(10)
        current = local_repository.get_player(10, 20)
        assert current is not None
        self.assertEqual(
            [
                "20",
                "Player",
                "YES",
                25,
                "=ARRAYFORMULA(...)",
            ],
            players.rows[1],
        )
        self.assertEqual(
            1,
            len(worksheets["lootsplit_history"].appended_rows),
        )
        self.assertEqual(
            1,
            len(worksheets["balance_history"].appended_rows),
        )
        self.assertTrue(local_repository.has_incomplete_outbox(guild_id=10))

    def test_projection_rebuild_refuses_to_bypass_initial_sheet_cutover(self) -> None:
        with (
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
            ) as get_worksheets,
        ):
            result = google_sync.rebuild_projection_sync(10)

        self.assertFalse(result.success)
        self.assertTrue(result.incomplete)
        self.assertIn("cutover", result.message)
        get_worksheets.assert_not_called()

    def test_projection_rebuild_does_not_duplicate_original_legacy_history(self) -> None:
        players = FakeWorksheet(
            [
                ["Discord ID", "Albion Nickname", "Is In Guild", "Silver", "Siphon"],
                ["20", "Player", "YES", "500", "-10"],
            ],
            worksheet_id=31,
        )
        balance = FakeWorksheet(
            [
                ["Date", "Reason", "Officer", "Nickname", "Amount"],
                ["03/24/26 17:44 UTC", "Payout", "Officer", "Player", "-50"],
            ],
            worksheet_id=32,
        )
        lootsplit = FakeWorksheet(
            [
                [
                    "Battleboard ID",
                    "Date",
                    "Officer",
                    "Content name",
                    "Caller",
                    "Participant",
                    "Lootsplit",
                ],
                [
                    "123",
                    "03/24/26 17:44 UTC",
                    "Officer",
                    "Fight",
                    "Caller",
                    "Player",
                    "100",
                ],
            ],
            worksheet_id=33,
        )
        worksheets = {
            "players": players,
            "balance_history": balance,
            "lootsplit_history": lootsplit,
        }
        with (
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
                return_value=worksheets,
            ),
        ):
            cutover = google_sync.bootstrap_guild_sync(10)
            rebuilt = google_sync.rebuild_projection_sync(10)

        self.assertTrue(cutover.success)
        self.assertTrue(rebuilt.success)
        self.assertEqual([], balance.appended_rows)
        self.assertEqual([], lootsplit.appended_rows)

    def test_manual_siphon_sync_matches_discord_id_and_current_silver(self) -> None:
        local_repository.register_player(10, 20, "Player")
        local_repository.change_balance(10, 20, 500, idempotency_key="credit")
        self._complete_cutover()
        headers = [
            "Discord ID",
            "Albion Nickname",
            "Is In Guild",
            "Silver",
            "Siphon",
        ]
        worksheet = FakeWorksheet(
            [
                headers,
                [20.0, "Player", "YES", 499.0, -20.0],
            ]
        )
        with (
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
                return_value={"players": worksheet},
            ),
        ):
            stale = google_sync.sync_siphon_from_sheet_sync(10)
            worksheet.rows[1][3] = 500.0
            fresh = google_sync.sync_siphon_from_sheet_sync(10)

        self.assertEqual(0, stale.updated_siphon_rows)
        self.assertEqual(1, fresh.updated_siphon_rows)
        self.assertEqual(-20, local_repository.get_player(10, 20).siphon)
        self.assertIn("UNFORMATTED_VALUE", worksheet.value_render_options)

    def test_duplicate_discord_id_is_rejected_and_replaces_old_cached_siphon(self) -> None:
        player = local_repository.register_player(10, 20, "Player").player
        assert player is not None
        self._complete_cutover()
        local_repository.cache_siphon(
            10,
            20,
            -9,
            expected_revision=player.revision,
        )
        worksheet = FakeWorksheet(
            [
                [
                    "Discord ID",
                    "Albion Nickname",
                    "Is In Guild",
                    "Silver",
                    "Siphon",
                ],
                ["20", "Player", "YES", 0, -11],
                ["20", "Duplicate", "YES", 0, -12],
            ]
        )
        with (
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
                return_value={"players": worksheet},
            ),
        ):
            result = google_sync.sync_siphon_from_sheet_sync(10)

        self.assertFalse(result.success)
        self.assertTrue(result.incomplete)
        self.assertEqual(1, result.rejected_siphon_rows)
        self.assertEqual(0, result.updated_siphon_rows)
        self.assertIsNone(local_repository.get_player(10, 20).siphon)

    def test_blank_sheet_silver_is_not_accepted_as_zero_during_siphon_sync(self) -> None:
        player = local_repository.register_player(10, 20, "Player").player
        assert player is not None
        self._complete_cutover()
        local_repository.cache_siphon(
            10,
            20,
            -9,
            expected_revision=player.revision,
        )
        worksheet = FakeWorksheet(
            [
                ["Discord ID", "Albion Nickname", "Is In Guild", "Silver", "Siphon"],
                ["20", "Player", "YES", "", -12],
            ]
        )
        with (
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
                return_value={"players": worksheet},
            ),
        ):
            result = google_sync.sync_siphon_from_sheet_sync(10)

        self.assertFalse(result.success)
        self.assertEqual(0, result.updated_siphon_rows)
        self.assertIsNone(local_repository.get_player(10, 20).siphon)

    def test_manual_siphon_sync_does_not_flush_or_wait_for_the_projection_queue(self) -> None:
        local_repository.register_player(10, 20, "Player")
        self._complete_cutover()
        head = local_repository.claim_pending_outbox("test", guild_id=10)[0]
        local_repository.fail_outbox(
            head.event_id,
            "quota",
            retry_after_seconds=3600,
            worker_id="test",
        )
        local_repository.change_balance(10, 20, 50, idempotency_key="newer")
        worksheet = FakeWorksheet(
            [
                ["Discord ID", "Albion Nickname", "Is In Guild", "Silver", "Siphon"],
                ["20", "Player", "YES", 50, -7],
            ]
        )
        with (
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
                return_value={"players": worksheet},
            ),
        ):
            result = google_sync.sync_siphon_from_sheet_sync(10)

        self.assertTrue(result.success)
        self.assertEqual(-7, local_repository.get_player(10, 20).siphon)
        self.assertTrue(local_repository.has_incomplete_outbox(guild_id=10))

    def test_manual_siphon_sync_returns_an_actionable_schema_failure(self) -> None:
        local_repository.register_player(10, 20, "Player")
        self._complete_cutover()
        worksheet = FakeWorksheet([["Wrong", "Players", "Headers", "Here"]])
        with (
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
                return_value={"players": worksheet},
            ),
        ):
            result = google_sync.sync_siphon_from_sheet_sync(10)

        self.assertFalse(result.success)
        self.assertTrue(result.incomplete)
        self.assertIn("Players headers must start with", result.message)

    def test_relink_is_export_only_and_does_not_duplicate_legacy_history(self) -> None:
        first_players = FakeWorksheet(
            [
                ["Discord ID", "Albion Nickname", "Is In Guild", "Silver", "Siphon"],
                ["20", "Player", "YES", "500", "-10"],
            ],
            worksheet_id=1,
        )
        first_balance = FakeWorksheet(
            [
                ["Date", "Reason", "Officer", "Nickname", "Amount"],
                ["03/24/26 17:44 UTC", "Payout", "Officer", "Player", "-50"],
            ],
            worksheet_id=2,
        )
        first_lootsplit = FakeWorksheet(
            [
                [
                    "Battleboard ID",
                    "Date",
                    "Officer",
                    "Content name",
                    "Caller",
                    "Participant",
                    "Lootsplit",
                ],
                [
                    "123",
                    "03/24/26 17:44 UTC",
                    "Officer",
                    "Fight",
                    "Caller",
                    "Player",
                    "100",
                ],
            ],
            worksheet_id=3,
        )

        current = {
            "players": first_players,
            "balance_history": first_balance,
            "lootsplit_history": first_lootsplit,
        }

        def get_worksheet(_guild_id: int, worksheet_type: str = "players"):
            return current[worksheet_type]

        with (
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
                side_effect=self._batch_resolver(get_worksheet),
            ),
        ):
            self.assertTrue(google_sync.bootstrap_guild_sync(10).success)

            relinked_credentials = dict(self.credentials, google_sheet_name="Other")
            current = {
                "players": FakeWorksheet(
                    [
                        ["Discord ID", "Albion Nickname", "Is In Guild", "Silver", "Siphon"],
                        ["21", "Intruder", "YES", "999", "-99"],
                    ],
                    worksheet_id=11,
                ),
                "balance_history": FakeWorksheet(
                    [
                        ["Date", "Reason", "Officer", "Nickname", "Amount"],
                        ["03/24/26 17:44 UTC", "Payout", "Officer", "Intruder", "-77"],
                    ],
                    worksheet_id=12,
                ),
                "lootsplit_history": FakeWorksheet(
                    [
                        [
                            "Battleboard ID",
                            "Date",
                            "Officer",
                            "Content name",
                            "Caller",
                            "Participant",
                            "Lootsplit",
                        ]
                    ],
                    worksheet_id=13,
                ),
            }
            with patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=relinked_credentials,
            ):
                relink = google_sync.bootstrap_guild_sync(10)
                repeated_relink = google_sync.bootstrap_guild_sync(10)

        self.assertTrue(relink.success)
        self.assertTrue(repeated_relink.success)
        self.assertIsNone(local_repository.get_player(10, 21))
        self.assertIsNotNone(local_repository.get_player(10, 20))
        self.assertTrue(any(row and row[0] == "20" for row in current["players"].rows[1:]))
        balance_event_ids = {
            str(row["event_id"]) for row in local_repository.iter_balance_history(10)
        }
        lootsplit_event_ids = {
            str(row["event_id"]) for row in local_repository.iter_lootsplit_history(10)
        }
        self.assertEqual(
            balance_event_ids,
            {row[-1] for row in current["balance_history"].appended_rows},
        )
        self.assertEqual(
            lootsplit_event_ids,
            {row[-1] for row in current["lootsplit_history"].appended_rows},
        )
        self.assertEqual(1, len(current["balance_history"].appended_rows))
        self.assertEqual(1, len(current["lootsplit_history"].appended_rows))
        with sqlite_database.connection() as database:
            self.assertEqual(
                1,
                database.execute(
                    "SELECT COUNT(*) FROM balance_history WHERE event_kind = 'sheet_import'"
                ).fetchone()[0],
            )

    def test_historical_outbox_payload_cannot_regress_player_projection(self) -> None:
        local_repository.register_player(10, 20, "Player")
        local_repository.change_balance(10, 20, 500, idempotency_key="credit")
        players = FakeWorksheet(
            [["Discord ID", "Albion Nickname", "Is In Guild", "Silver", "Siphon"]]
        )
        with (
            patch.object(
                google_sync.credential_store,
                "get_credentials_info",
                return_value=self.credentials,
            ),
            patch.object(
                google_sync.google_sheets,
                "get_worksheets",
                return_value=self._projection_worksheets(players),
            ),
        ):
            result = google_sync.flush_outbox_sync(10, limit=1)

        self.assertFalse(result.success)
        self.assertEqual(500, players.rows[1][3])


if __name__ == "__main__":
    unittest.main()
