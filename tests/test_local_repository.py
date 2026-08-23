from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.realm_protector.infrastructure import (
    guild_settings,
    local_repository,
    sqlite_database,
)
from src.realm_protector.services import configuration_lifecycle


class LocalRepositoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "bot.sqlite3"
        local_repository.ensure_schema(self.database_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def activate_test_ledger(self, guild_id: int) -> None:
        local_repository.activate_ledger(
            guild_id,
            f"Test Albion Guild {guild_id}",
            database_path=self.database_path,
        )


class RegistrationRepositoryTests(LocalRepositoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.activate_test_ledger(10)
        self.activate_test_ledger(11)

    def test_case_insensitive_nickname_uniqueness_and_reactivation_preserve_silver(
        self,
    ) -> None:
        created = local_repository.register_player(
            10,
            20,
            "Éowyn",
            "albion-20",
            database_path=self.database_path,
        )
        self.assertEqual(local_repository.RegistrationStatus.CREATED, created.status)

        conflict = local_repository.register_player(
            10,
            21,
            "éOWYN",
            database_path=self.database_path,
        )
        self.assertEqual(
            local_repository.RegistrationStatus.NICKNAME_CONFLICT,
            conflict.status,
        )
        self.assertEqual(20, conflict.conflicting_discord_user_id)

        balance = local_repository.change_balance(
            10,
            20,
            500,
            reason="Initial credit",
            idempotency_key="balance-1",
            database_path=self.database_path,
        )
        self.assertEqual(500, balance.updated_balance)
        inactive = local_repository.set_in_guild(
            10,
            20,
            False,
            database_path=self.database_path,
        )
        self.assertFalse(inactive.is_active)
        self.assertEqual(500, inactive.silver)
        self.assertEqual(500, inactive.all_time_earnings)
        self.assertEqual("Éowyn", inactive.nickname)

        reactivated = local_repository.register_player(
            10,
            20,
            "ÉOWYN",
            "albion-20",
            database_path=self.database_path,
        )
        self.assertEqual(
            local_repository.RegistrationStatus.REACTIVATED,
            reactivated.status,
        )
        self.assertEqual(500, reactivated.player.silver)
        self.assertTrue(reactivated.player.is_in_guild)

    def test_google_bootstrap_never_overwrites_an_existing_local_player(self) -> None:
        local_repository.register_player(
            10,
            20,
            "LocalName",
            database_path=self.database_path,
        )
        local_repository.change_balance(
            10,
            20,
            300,
            idempotency_key="local-credit",
            database_path=self.database_path,
        )
        local_player = local_repository.get_player(
            10,
            20,
            database_path=self.database_path,
        )
        local_repository.cache_siphon(
            10,
            20,
            -10,
            expected_revision=local_player.revision,
            database_path=self.database_path,
        )

        result = local_repository.import_player(
            10,
            20,
            "SheetName",
            is_active=False,
            silver=999,
            siphon=-45,
            database_path=self.database_path,
        )

        self.assertEqual(
            local_repository.PlayerImportStatus.LOCAL_PRESERVED,
            result.status,
        )
        self.assertEqual("LocalName", result.player.nickname)
        self.assertEqual(300, result.player.silver)
        self.assertTrue(result.player.is_active)
        self.assertEqual(-10, result.player.siphon)
        self.assertEqual(result.player.revision, result.player.siphon_revision)

    def test_albion_ids_are_unique_case_insensitively_within_a_guild(self) -> None:
        local_repository.register_player(
            10,
            20,
            "First",
            "ABC-123",
            database_path=self.database_path,
        )
        conflict = local_repository.register_player(
            10,
            21,
            "Second",
            "abc-123",
            database_path=self.database_path,
        )
        other_guild = local_repository.register_player(
            11,
            21,
            "Second",
            "abc-123",
            database_path=self.database_path,
        )

        self.assertEqual(
            local_repository.RegistrationStatus.ALBION_ID_CONFLICT,
            conflict.status,
        )
        self.assertEqual(
            local_repository.RegistrationStatus.CREATED,
            other_guild.status,
        )


class LedgerGenerationTests(LocalRepositoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.database_context = sqlite_database.database_path(self.database_path)
        self.database_context.__enter__()

    def tearDown(self) -> None:
        self.database_context.__exit__(None, None, None)
        super().tearDown()

    def test_remove_archives_old_ledger_and_new_target_starts_empty(self) -> None:
        guild_settings.set_target_guild(10, "First Albion Guild")
        first_ledger = local_repository.get_active_ledger(10)
        assert first_ledger is not None
        self.assertEqual(10, first_ledger.ledger_id)
        local_repository.register_player(first_ledger.ledger_id, 20, "Veteran")
        local_repository.change_balance(
            first_ledger.ledger_id,
            20,
            750,
            idempotency_key="old-ledger-balance",
        )
        first_import = local_repository.begin_sheet_import(
            first_ledger.ledger_id,
            "google-bootstrap-v1",
            "old-sheet",
        )
        local_repository.complete_sheet_import(first_import.import_id)

        removed = configuration_lifecycle.begin_guild_configuration_removal(10)
        assert removed is not None
        self.assertEqual("First Albion Guild", removed.target_guild_name)
        self.assertIsNone(local_repository.get_active_ledger(10, create_if_missing=False))
        archived_player = local_repository.get_player(first_ledger.ledger_id, 20)
        assert archived_player is not None
        self.assertEqual(750, archived_player.silver)

        guild_settings.set_target_guild(10, "Second Albion Guild")
        second_ledger = local_repository.get_active_ledger(10)
        assert second_ledger is not None
        self.assertNotEqual(first_ledger.ledger_id, second_ledger.ledger_id)
        self.assertEqual(2, second_ledger.generation)
        self.assertEqual([], local_repository.list_players(second_ledger.ledger_id))
        self.assertFalse(
            local_repository.has_completed_sheet_import(
                second_ledger.ledger_id,
                "google-bootstrap-v1",
            )
        )
        self.assertFalse(local_repository.has_incomplete_outbox(guild_id=second_ledger.ledger_id))
        self.assertTrue(local_repository.has_incomplete_outbox(guild_id=first_ledger.ledger_id))
        generations = local_repository.list_ledger_generations(10)
        self.assertEqual([False, True], [entry.is_active for entry in generations])

    def test_target_change_rotates_but_role_only_update_keeps_generation(self) -> None:
        guild_settings.set_target_guild(10, "First Albion Guild")
        first = local_repository.get_active_ledger(10)
        assert first is not None
        local_repository.register_player(first.ledger_id, 20, "Veteran")

        guild_settings.set_target_guild(
            10,
            "first albion guild",
            member_role_name="Guild Member",
        )
        role_update = local_repository.get_active_ledger(10)
        assert role_update is not None
        self.assertEqual(first.ledger_id, role_update.ledger_id)

        guild_settings.set_target_guild(10, "Different Albion Guild")
        changed_target = local_repository.get_active_ledger(10)
        assert changed_target is not None
        self.assertNotEqual(first.ledger_id, changed_target.ledger_id)
        self.assertEqual([], local_repository.list_players(changed_target.ledger_id))
        self.assertIsNotNone(local_repository.get_player(first.ledger_id, 20))

    def test_ledger_lookup_and_mutations_fail_closed_without_configuration(self) -> None:
        self.assertIsNone(local_repository.get_active_ledger(99))
        with self.assertRaisesRegex(ValueError, "target_guild_name"):
            local_repository.get_active_ledger(99, create_if_missing=True)
        with self.assertRaises(local_repository.LedgerNotActiveError):
            local_repository.register_player(99, 20, "Unowned")

    def test_normalized_target_can_have_only_one_active_discord_owner(self) -> None:
        guild_settings.set_target_guild(10, "Élite Guild")

        with self.assertRaises(local_repository.TargetGuildConflictError):
            guild_settings.set_target_guild(11, "E\u0301LITE GUILD")

        self.assertIsNone(guild_settings.get_target_guild(11))
        self.assertIsNone(local_repository.get_active_ledger(11))


class EconomyRepositoryTests(LocalRepositoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.activate_test_ledger(10)
        local_repository.register_player(
            10,
            20,
            "Treasurer",
            database_path=self.database_path,
        )

    def test_balance_is_clamped_audited_immutable_and_idempotent(self) -> None:
        local_repository.change_balance(
            10,
            20,
            100,
            idempotency_key="credit",
            database_path=self.database_path,
        )
        removal = local_repository.change_balance(
            10,
            20,
            -250,
            actor_discord_user_id=99,
            actor_name="Officer",
            reason="Charge",
            idempotency_key="remove",
            database_path=self.database_path,
        )
        replay = local_repository.change_balance(
            10,
            20,
            -250,
            actor_discord_user_id=99,
            actor_name="Officer",
            reason="Charge",
            idempotency_key="remove",
            database_path=self.database_path,
        )

        self.assertEqual(100, removal.previous_balance)
        self.assertEqual(-250, removal.requested_delta)
        self.assertEqual(-100, removal.actual_delta)
        self.assertEqual(0, removal.updated_balance)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(0, replay.player.silver)
        self.assertEqual(100, replay.player.all_time_earnings)
        self.assertEqual(
            100,
            local_repository.get_balance_snapshot(
                10,
                20,
                database_path=self.database_path,
            ).all_time_earnings,
        )
        history = list(
            local_repository.iter_balance_history(
                10,
                discord_user_id=20,
                database_path=self.database_path,
            )
        )
        self.assertEqual(2, len(history))

        with self.assertRaises(sqlite3.IntegrityError):
            with sqlite_database.transaction(self.database_path) as connection:
                connection.execute(
                    "UPDATE balance_history SET reason = 'tampered' WHERE event_id = ?",
                    (removal.event_id,),
                )

    def test_lootsplit_is_atomic_case_insensitive_and_replay_safe(self) -> None:
        local_repository.register_player(
            10,
            21,
            "Caller",
            database_path=self.database_path,
        )
        result = local_repository.apply_lootsplit(
            10,
            ["treasurer", "Missing", "TREASURER", "missing"],
            75,
            battleboard_ids=["123", "456"],
            actor_discord_user_id=99,
            actor_name="Invoker",
            officer_discord_user_id=98,
            officer_name="Operational Officer",
            caller_discord_user_id=21,
            caller_name="Caller",
            content_name="Avalonian road",
            idempotency_key="interaction-500",
            database_path=self.database_path,
        )
        replay = local_repository.apply_lootsplit(
            10,
            ["treasurer", "Missing", "TREASURER", "missing"],
            75,
            battleboard_ids=["123", "456"],
            actor_discord_user_id=99,
            actor_name="Invoker",
            officer_discord_user_id=98,
            officer_name="Operational Officer",
            caller_discord_user_id=21,
            caller_name="Caller",
            content_name="Avalonian road",
            idempotency_key="interaction-500",
            database_path=self.database_path,
        )

        self.assertEqual(("Missing",), result.missing_nicknames)
        self.assertEqual([75, 150], [credit.updated_balance for credit in result.credits])
        self.assertEqual(
            150,
            local_repository.get_player(
                10,
                20,
                database_path=self.database_path,
            ).silver,
        )
        self.assertEqual(
            150,
            local_repository.get_player(
                10,
                20,
                database_path=self.database_path,
            ).all_time_earnings,
        )
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(98, result.officer_discord_user_id)
        self.assertEqual("Operational Officer", replay.officer_name)
        self.assertEqual(result.credits, replay.credits)
        history = list(
            local_repository.iter_balance_history(
                10,
                discord_user_id=20,
                database_path=self.database_path,
            )
        )
        self.assertEqual(2, len(history))

    def test_silver_leaderboard_is_ranked_deterministically_and_paginated(self) -> None:
        players = [(20, "Treasurer", 50), (21, "Alpha", 100), (22, "Beta", 100)]
        players.extend((user_id, f"Player {user_id}", 99 - user_id) for user_id in range(23, 32))
        for user_id, nickname, silver in players:
            if user_id != 20:
                local_repository.register_player(
                    10,
                    user_id,
                    nickname,
                    database_path=self.database_path,
                )
            local_repository.change_balance(
                10,
                user_id,
                silver,
                idempotency_key=f"leaderboard-{user_id}",
                database_path=self.database_path,
            )

        first_page = local_repository.get_silver_leaderboard(
            10,
            limit=10,
            database_path=self.database_path,
        )
        second_page = local_repository.get_silver_leaderboard(
            10,
            limit=10,
            offset=10,
            database_path=self.database_path,
        )

        self.assertEqual(12, first_page.total_players)
        self.assertEqual(10, len(first_page.players))
        self.assertEqual(2, len(second_page.players))
        self.assertEqual([21, 22], [player.discord_user_id for player in first_page.players[:2]])
        ranked_silver = [player.silver for player in (*first_page.players, *second_page.players)]
        self.assertEqual(sorted(ranked_silver, reverse=True), ranked_silver)
        self.assertEqual(
            1,
            local_repository.get_silver_leaderboard_position(
                10,
                21,
                database_path=self.database_path,
            ),
        )
        self.assertEqual(
            2,
            local_repository.get_silver_leaderboard_position(
                10,
                22,
                database_path=self.database_path,
            ),
        )
        self.assertEqual(
            12,
            local_repository.get_silver_leaderboard_position(
                10,
                20,
                database_path=self.database_path,
            ),
        )
        self.assertIsNone(
            local_repository.get_silver_leaderboard_position(
                10,
                999,
                database_path=self.database_path,
            )
        )

    def test_siphon_cache_rejects_stale_formula_results_and_sorts_negatives(self) -> None:
        second = local_repository.register_player(
            10,
            21,
            "Second",
            database_path=self.database_path,
        ).player
        first = local_repository.get_player(
            10,
            20,
            database_path=self.database_path,
        )
        local_repository.change_balance(
            10,
            20,
            1,
            idempotency_key="revision-change",
            database_path=self.database_path,
        )

        stale = local_repository.cache_siphon(
            10,
            20,
            -5,
            expected_revision=first.revision,
            database_path=self.database_path,
        )
        accepted_first = local_repository.cache_siphon(
            10,
            20,
            -5,
            expected_revision=first.revision + 1,
            database_path=self.database_path,
        )
        accepted_second = local_repository.cache_siphon(
            10,
            21,
            -50,
            expected_revision=second.revision,
            database_path=self.database_path,
        )

        self.assertEqual(local_repository.SiphonCacheStatus.STALE_REVISION, stale.status)
        self.assertEqual(local_repository.SiphonCacheStatus.UPDATED, accepted_first.status)
        self.assertEqual(local_repository.SiphonCacheStatus.UPDATED, accepted_second.status)
        negatives = local_repository.list_negative_siphon(
            10,
            database_path=self.database_path,
        )
        self.assertEqual([21, 20], [player.discord_user_id for player in negatives])

    def test_siphon_snapshot_is_cached_atomically_with_per_row_revision_checks(
        self,
    ) -> None:
        second = local_repository.register_player(
            10,
            21,
            "Second",
            database_path=self.database_path,
        ).player
        first = local_repository.get_player(
            10,
            20,
            database_path=self.database_path,
        )
        results = local_repository.cache_siphons(
            10,
            [
                local_repository.SiphonUpdate(20, -10, first.revision),
                local_repository.SiphonUpdate(21, 5, second.revision + 1),
                local_repository.SiphonUpdate(22, -20, 1),
            ],
            database_path=self.database_path,
        )

        self.assertEqual(
            [
                local_repository.SiphonCacheStatus.UPDATED,
                local_repository.SiphonCacheStatus.STALE_REVISION,
                local_repository.SiphonCacheStatus.NOT_FOUND,
            ],
            [result.status for result in results],
        )
        self.assertEqual(
            -10,
            local_repository.get_player(
                10,
                20,
                database_path=self.database_path,
            ).siphon,
        )
        self.assertIsNone(
            local_repository.get_player(
                10,
                21,
                database_path=self.database_path,
            ).siphon
        )

    def test_revision_changes_invalidate_siphon_for_every_player_mutation(self) -> None:
        def cache_current() -> local_repository.PlayerRecord:
            player = local_repository.get_player(
                10,
                20,
                database_path=self.database_path,
            )
            cached = local_repository.cache_siphon(
                10,
                20,
                -25,
                expected_revision=player.revision,
                database_path=self.database_path,
            ).player
            self.assertEqual(cached.revision, cached.siphon_revision)
            return cached

        before_membership = cache_current()
        inactive = local_repository.set_in_guild(
            10,
            20,
            False,
            database_path=self.database_path,
        )
        self.assertEqual(before_membership.revision + 1, inactive.revision)
        self.assertIsNone(inactive.siphon)
        self.assertIsNone(inactive.siphon_revision)
        self.assertIsNone(inactive.siphon_synced_at)

        before_reactivation = cache_current()
        reactivated = local_repository.register_player(
            10,
            20,
            "Treasurer",
            database_path=self.database_path,
        ).player
        self.assertEqual(before_reactivation.revision + 1, reactivated.revision)
        self.assertIsNone(reactivated.siphon)

        before_balance = cache_current()
        changed = local_repository.change_balance(
            10,
            20,
            1,
            idempotency_key="invalidate-siphon-balance",
            database_path=self.database_path,
        ).player
        self.assertEqual(before_balance.revision + 1, changed.revision)
        self.assertIsNone(changed.siphon)

        before_lootsplit = cache_current()
        local_repository.apply_lootsplit(
            10,
            ["Treasurer"],
            1,
            idempotency_key="invalidate-siphon-lootsplit",
            database_path=self.database_path,
        )
        after_lootsplit = local_repository.get_player(
            10,
            20,
            database_path=self.database_path,
        )
        self.assertEqual(before_lootsplit.revision + 1, after_lootsplit.revision)
        self.assertIsNone(after_lootsplit.siphon)

    def test_replacement_snapshot_clears_unseen_and_rejected_cached_values(self) -> None:
        first = local_repository.get_player(
            10,
            20,
            database_path=self.database_path,
        )
        second = local_repository.register_player(
            10,
            21,
            "Second",
            database_path=self.database_path,
        ).player
        local_repository.cache_siphons(
            10,
            [
                local_repository.SiphonUpdate(20, -10, first.revision),
                local_repository.SiphonUpdate(21, -20, second.revision),
            ],
            database_path=self.database_path,
        )

        results = local_repository.cache_siphons(
            10,
            [local_repository.SiphonUpdate(20, -30, first.revision)],
            replace_snapshot=True,
            database_path=self.database_path,
        )

        self.assertEqual(local_repository.SiphonCacheStatus.UPDATED, results[0].status)
        refreshed = local_repository.get_player(
            10,
            20,
            database_path=self.database_path,
        )
        unseen = local_repository.get_player(
            10,
            21,
            database_path=self.database_path,
        )
        self.assertEqual(-30, refreshed.siphon)
        self.assertEqual(refreshed.revision, refreshed.siphon_revision)
        self.assertIsNone(unseen.siphon)
        self.assertIsNone(unseen.siphon_revision)
        self.assertIsNone(unseen.siphon_synced_at)

    def test_negative_siphon_requires_current_revision_and_optional_freshness(self) -> None:
        player = local_repository.get_player(
            10,
            20,
            database_path=self.database_path,
        )
        old_snapshot = datetime.now(timezone.utc) - timedelta(hours=1)
        local_repository.cache_siphon(
            10,
            20,
            -25,
            expected_revision=player.revision,
            synced_at=old_snapshot,
            database_path=self.database_path,
        )
        self.assertEqual(
            [20],
            [
                snapshot.discord_user_id
                for snapshot in local_repository.list_negative_siphon(
                    10,
                    database_path=self.database_path,
                )
            ],
        )
        self.assertEqual(
            [],
            local_repository.list_negative_siphon(
                10,
                max_age_seconds=60,
                database_path=self.database_path,
            ),
        )

        with sqlite_database.transaction(self.database_path) as connection:
            connection.execute(
                """
                UPDATE registered_players
                SET revision = revision + 1
                WHERE guild_id = 10 AND discord_user_id = 20
                """
            )
        self.assertEqual(
            [],
            local_repository.list_negative_siphon(
                10,
                database_path=self.database_path,
            ),
        )


class OutboxAndImportRepositoryTests(LocalRepositoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.activate_test_ledger(10)
        self.activate_test_ledger(11)

    def test_outbox_events_can_be_leased_failed_retried_and_acknowledged(self) -> None:
        local_repository.register_player(
            10,
            20,
            "Player",
            database_path=self.database_path,
        )
        claimed = local_repository.claim_pending_outbox(
            "worker-a",
            database_path=self.database_path,
        )
        self.assertEqual(1, len(claimed))
        self.assertEqual("processing", claimed[0].status)
        self.assertFalse(
            local_repository.ack_outbox(
                claimed[0].event_id,
                worker_id="wrong-worker",
                database_path=self.database_path,
            )
        )
        self.assertTrue(
            local_repository.fail_outbox(
                claimed[0].event_id,
                "temporary quota",
                worker_id="worker-a",
                retry_after_seconds=0,
                database_path=self.database_path,
            )
        )
        retried = local_repository.claim_pending_outbox(
            "worker-b",
            database_path=self.database_path,
        )
        self.assertEqual(1, retried[0].attempts)
        self.assertTrue(
            local_repository.ack_outbox(
                retried[0].event_id,
                worker_id="worker-b",
                database_path=self.database_path,
            )
        )
        self.assertEqual(
            [],
            local_repository.list_pending_outbox(
                database_path=self.database_path,
            ),
        )
        self.assertFalse(
            local_repository.has_incomplete_outbox(
                guild_id=10,
                database_path=self.database_path,
            )
        )

    def test_poison_event_can_be_dead_lettered_and_retried(self) -> None:
        local_repository.register_player(
            10,
            20,
            "Player",
            database_path=self.database_path,
        )
        event = local_repository.claim_pending_outbox(
            "worker-a",
            guild_id=10,
            database_path=self.database_path,
        )[0]

        self.assertTrue(
            local_repository.dead_letter_outbox(
                event.event_id,
                "invalid projection payload",
                worker_id="worker-a",
                database_path=self.database_path,
            )
        )
        self.assertFalse(
            local_repository.has_incomplete_outbox(
                guild_id=10,
                database_path=self.database_path,
            )
        )
        dead_letters = local_repository.list_dead_letter_outbox(
            guild_id=10,
            database_path=self.database_path,
        )
        self.assertEqual([event.event_id], [item.event_id for item in dead_letters])
        self.assertEqual(1, dead_letters[0].attempts)

        self.assertTrue(
            local_repository.retry_dead_letter_outbox(
                event.event_id,
                database_path=self.database_path,
            )
        )
        retried = local_repository.claim_pending_outbox(
            "worker-b",
            guild_id=10,
            database_path=self.database_path,
        )
        self.assertEqual([event.event_id], [item.event_id for item in retried])
        self.assertEqual(
            [],
            local_repository.list_dead_letter_outbox(
                guild_id=10,
                database_path=self.database_path,
            ),
        )

    def test_outbox_status_and_bounded_guild_dead_letter_retry(self) -> None:
        local_repository.register_player(
            10,
            20,
            "Player",
            database_path=self.database_path,
        )
        completed = local_repository.claim_pending_outbox(
            "worker-a",
            guild_id=10,
            database_path=self.database_path,
        )[0]
        self.assertTrue(
            local_repository.ack_outbox(
                completed.event_id,
                worker_id="worker-a",
                database_path=self.database_path,
            )
        )

        local_repository.change_balance(
            10,
            20,
            5,
            idempotency_key="status-balance",
            database_path=self.database_path,
        )
        first_dead = local_repository.claim_pending_outbox(
            "worker-b",
            guild_id=10,
            database_path=self.database_path,
        )[0]
        self.assertTrue(
            local_repository.dead_letter_outbox(
                first_dead.event_id,
                "first poison event",
                worker_id="worker-b",
                database_path=self.database_path,
            )
        )

        local_repository.set_in_guild(
            10,
            20,
            False,
            database_path=self.database_path,
        )
        second_dead = local_repository.claim_pending_outbox(
            "worker-c",
            guild_id=10,
            database_path=self.database_path,
        )[0]
        self.assertTrue(
            local_repository.dead_letter_outbox(
                second_dead.event_id,
                "second poison event",
                worker_id="worker-c",
                database_path=self.database_path,
            )
        )

        status = local_repository.get_outbox_status(
            10,
            database_path=self.database_path,
        )
        self.assertEqual(0, status.queued_events)
        self.assertEqual(2, status.incomplete_events)
        self.assertEqual(1, status.completed_events)
        self.assertEqual(2, status.dead_letter_events)
        self.assertEqual("second poison event", status.latest_error)

        restored = local_repository.retry_dead_letter_outbox_for_guild(
            10,
            limit=1,
            database_path=self.database_path,
        )
        self.assertEqual(1, restored)
        status = local_repository.get_outbox_status(
            10,
            database_path=self.database_path,
        )
        self.assertEqual(1, status.pending_events)
        self.assertEqual(1, status.dead_letter_events)
        self.assertEqual(2, status.incomplete_events)
        retried = local_repository.claim_pending_outbox(
            "worker-d",
            guild_id=10,
            database_path=self.database_path,
        )
        self.assertEqual([first_dead.event_id], [event.event_id for event in retried])

    def test_completed_outbox_retention_is_bounded(self) -> None:
        for discord_id in (20, 21):
            local_repository.register_player(
                10,
                discord_id,
                f"Player {discord_id}",
                database_path=self.database_path,
            )
            event = local_repository.claim_pending_outbox(
                f"worker-{discord_id}",
                guild_id=10,
                database_path=self.database_path,
            )[0]
            self.assertTrue(
                local_repository.ack_outbox(
                    event.event_id,
                    worker_id=f"worker-{discord_id}",
                    database_path=self.database_path,
                )
            )

        cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
        self.assertEqual(
            1,
            local_repository.prune_completed_outbox(
                cutoff,
                limit=1,
                database_path=self.database_path,
            ),
        )
        self.assertEqual(
            1,
            local_repository.get_outbox_status(
                10,
                database_path=self.database_path,
            ).completed_events,
        )

        local_repository.register_player(
            11,
            30,
            "Other Guild Player",
            database_path=self.database_path,
        )
        other_event = local_repository.claim_pending_outbox(
            "worker-other",
            guild_id=11,
            database_path=self.database_path,
        )[0]
        self.assertTrue(
            local_repository.ack_outbox(
                other_event.event_id,
                worker_id="worker-other",
                database_path=self.database_path,
            )
        )
        scoped_cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
        self.assertEqual(
            1,
            local_repository.prune_completed_outbox(
                scoped_cutoff,
                guild_id=10,
                limit=10,
                database_path=self.database_path,
            ),
        )
        self.assertEqual(
            1,
            local_repository.get_outbox_status(
                11,
                database_path=self.database_path,
            ).completed_events,
        )

    def test_signed_64_bit_values_are_rejected_before_sqlite_binding(self) -> None:
        with self.assertRaisesRegex(ValueError, "signed 64-bit"):
            local_repository.import_player(
                10,
                20,
                "Player",
                is_active=True,
                silver=1 << 80,
                database_path=self.database_path,
            )

    def test_valid_json_with_the_wrong_repository_shape_is_corruption(self) -> None:
        local_repository.register_player(
            10,
            20,
            "Player",
            database_path=self.database_path,
        )
        with sqlite_database.transaction(self.database_path) as connection:
            connection.execute(
                "UPDATE google_sync_outbox SET payload_json = '[]' WHERE guild_id = 10"
            )

        with self.assertRaises(local_repository.RepositoryCorruptionError):
            local_repository.list_pending_outbox(
                guild_id=10,
                database_path=self.database_path,
            )

    def test_outbox_claims_only_each_guild_head_and_never_skips_a_retry(self) -> None:
        local_repository.register_player(
            10,
            20,
            "FirstGuild",
            database_path=self.database_path,
        )
        local_repository.change_balance(
            10,
            20,
            10,
            idempotency_key="first-guild-second-event",
            database_path=self.database_path,
        )
        local_repository.register_player(
            11,
            21,
            "SecondGuild",
            database_path=self.database_path,
        )

        first_head = local_repository.claim_pending_outbox(
            "worker-a",
            guild_id=10,
            limit=10,
            database_path=self.database_path,
        )
        self.assertEqual(1, len(first_head))
        self.assertEqual(
            [],
            local_repository.claim_pending_outbox(
                "worker-b",
                guild_id=10,
                limit=10,
                database_path=self.database_path,
            ),
        )

        other_guild = local_repository.claim_pending_outbox(
            "worker-b",
            limit=10,
            database_path=self.database_path,
        )
        self.assertEqual([11], [event.guild_id for event in other_guild])
        self.assertTrue(
            local_repository.ack_outbox(
                other_guild[0].event_id,
                worker_id="worker-b",
                database_path=self.database_path,
            )
        )

        self.assertTrue(
            local_repository.fail_outbox(
                first_head[0].event_id,
                "quota",
                retry_after_seconds=3600,
                worker_id="worker-a",
                database_path=self.database_path,
            )
        )
        self.assertEqual(
            [],
            local_repository.claim_pending_outbox(
                "worker-c",
                guild_id=10,
                limit=10,
                database_path=self.database_path,
            ),
        )
        self.assertTrue(
            local_repository.has_incomplete_outbox(
                guild_id=10,
                database_path=self.database_path,
            )
        )

    def test_sheet_import_rows_and_issues_are_idempotent(self) -> None:
        sheet_import = local_repository.begin_sheet_import(
            10,
            "players",
            "snapshot-sha256",
            metadata={"sheet": "Guild Economy"},
            database_path=self.database_path,
        )
        inserted = local_repository.record_sheet_import_row(
            sheet_import.import_id,
            "players",
            2,
            "row-sha256",
            ["20", "Player", "YES", "100", "-2"],
            database_path=self.database_path,
        )
        duplicate = local_repository.record_sheet_import_row(
            sheet_import.import_id,
            "players",
            2,
            "row-sha256",
            ["20", "Player", "YES", "100", "-2"],
            database_path=self.database_path,
        )
        completed = local_repository.complete_sheet_import(
            sheet_import.import_id,
            database_path=self.database_path,
        )

        self.assertTrue(inserted)
        self.assertFalse(duplicate)
        self.assertEqual(local_repository.SheetImportStatus.COMPLETED, completed.status)
        self.assertEqual(1, completed.row_count)
        self.assertTrue(
            local_repository.has_completed_sheet_import(
                10,
                "players",
                database_path=self.database_path,
            )
        )
        self.assertFalse(
            local_repository.has_completed_sheet_import(
                10,
                "another-source",
                database_path=self.database_path,
            )
        )

        issue_one = local_repository.record_migration_issue(
            guild_id=10,
            source="players",
            source_reference="row 2",
            code="duplicate_nickname",
            message="Nickname is duplicated.",
            payload={"nickname": "Player"},
            database_path=self.database_path,
        )
        issue_two = local_repository.record_migration_issue(
            guild_id=10,
            source="players",
            source_reference="row 2",
            code="duplicate_nickname",
            message="Nickname is duplicated.",
            payload={"nickname": "Player"},
            database_path=self.database_path,
        )
        self.assertEqual(issue_one.issue_id, issue_two.issue_id)
        self.assertEqual(
            1,
            len(
                local_repository.list_migration_issues(
                    guild_id=10,
                    database_path=self.database_path,
                )
            ),
        )
        self.assertTrue(
            local_repository.resolve_migration_issue(
                issue_one.issue_id,
                database_path=self.database_path,
            )
        )

    def test_history_imports_are_idempotent_and_do_not_replay_balances(self) -> None:
        local_repository.import_player(
            10,
            20,
            "Known",
            is_active=True,
            silver=700,
            database_path=self.database_path,
        )
        balance_row = local_repository.import_balance_history(
            10,
            "Known",
            500,
            source_key="balance:row:2:occurrence:1",
            occurred_at="2025-01-01 10:00 UTC",
            reason="Legacy adjustment",
            actor_name="Officer",
            database_path=self.database_path,
        )
        balance_replay = local_repository.import_balance_history(
            10,
            "Known",
            500,
            source_key="balance:row:2:occurrence:1",
            occurred_at="2025-01-01 10:00 UTC",
            reason="Legacy adjustment",
            actor_name="Officer",
            database_path=self.database_path,
        )
        unmatched_lootsplit = local_repository.import_lootsplit_history(
            10,
            "FormerMember",
            250,
            source_key="lootsplit:row:9:occurrence:1",
            occurred_at="2025-01-02 10:00 UTC",
            battleboard_ids=["123"],
            actor_name="Officer",
            content_name="Roads",
            caller_name="Caller",
            database_path=self.database_path,
        )

        self.assertEqual(local_repository.HistoryImportStatus.IMPORTED, balance_row.status)
        self.assertEqual(
            local_repository.HistoryImportStatus.ALREADY_IMPORTED,
            balance_replay.status,
        )
        self.assertEqual(500, balance_replay.player.all_time_earnings)
        self.assertEqual(balance_row.event_id, balance_replay.event_id)
        self.assertIsNone(unmatched_lootsplit.player)
        self.assertEqual(
            700,
            local_repository.get_player(
                10,
                20,
                database_path=self.database_path,
            ).silver,
        )
        self.assertEqual(
            1,
            len(
                list(
                    local_repository.iter_balance_history(
                        10,
                        database_path=self.database_path,
                    )
                )
            ),
        )


class LocalRepositoryMigrationTests(unittest.TestCase):
    def test_all_time_earnings_migration_rebuilds_only_positive_local_credits(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "earnings.sqlite3"
            local_repository.ensure_schema(database_path)
            local_repository.activate_ledger(
                10,
                "Test Guild",
                database_path=database_path,
            )
            local_repository.register_player(
                10,
                20,
                "Known",
                database_path=database_path,
            )
            local_repository.change_balance(
                10,
                20,
                100,
                idempotency_key="migration-credit",
                database_path=database_path,
            )
            local_repository.change_balance(
                10,
                20,
                -30,
                idempotency_key="migration-debit",
                database_path=database_path,
            )
            local_repository.apply_lootsplit(
                10,
                ["Known"],
                40,
                idempotency_key="migration-lootsplit",
                database_path=database_path,
            )
            local_repository.import_balance_history(
                10,
                "Known",
                500,
                source_key="legacy-credit",
                occurred_at="2025-01-01T00:00:00+00:00",
                database_path=database_path,
            )

            with sqlite_database.transaction(database_path) as connection:
                connection.execute("UPDATE registered_players SET all_time_earnings = 0")
                connection.execute(
                    "DELETE FROM local_repository_schema_migrations WHERE version = 6"
                )
            with sqlite_database.connection(database_path) as connection:
                local_repository._migrate_all_time_earnings(connection)

            player = local_repository.get_player(
                10,
                20,
                database_path=database_path,
            )
            self.assertEqual(110, player.silver)
            self.assertEqual(640, player.all_time_earnings)

    def test_siphon_revision_migration_invalidates_unproven_legacy_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "legacy.sqlite3"
            sqlite_database.initialize_database(database_path)
            with sqlite_database.connection(database_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE registered_players (
                        guild_id INTEGER NOT NULL,
                        discord_user_id INTEGER NOT NULL,
                        nickname TEXT NOT NULL,
                        nickname_key TEXT NOT NULL,
                        albion_player_id TEXT,
                        is_active INTEGER NOT NULL,
                        silver INTEGER NOT NULL,
                        revision INTEGER NOT NULL,
                        siphon INTEGER,
                        siphon_synced_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (guild_id, discord_user_id),
                        UNIQUE (guild_id, nickname_key),
                        UNIQUE (guild_id, albion_player_id)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO registered_players (
                        guild_id, discord_user_id, nickname, nickname_key,
                        is_active, silver, revision, siphon, siphon_synced_at,
                        created_at, updated_at
                    ) VALUES (10, 20, 'Legacy', 'legacy', 1, 100, 4, -25,
                        '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00')
                    """
                )

            local_repository.ensure_schema(database_path)

            player = local_repository.get_player(
                10,
                20,
                database_path=database_path,
            )
            self.assertIsNone(player.siphon)
            self.assertIsNone(player.siphon_revision)
            self.assertIsNone(player.siphon_synced_at)
            self.assertEqual(0, player.all_time_earnings)
            with sqlite_database.connection(database_path) as connection:
                migration = connection.execute(
                    """
                    SELECT name FROM local_repository_schema_migrations
                    WHERE version = 2
                    """
                ).fetchone()
            self.assertIsNotNone(migration)


if __name__ == "__main__":
    unittest.main()
