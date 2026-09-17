import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.realm_protector.bot import economy_commands
from src.realm_protector.infrastructure import guild_settings, local_repository, sqlite_database

TARGET_ID = 936012266321608744


class BalanceRemovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = sqlite_database.database_path(Path(directory.name) / "balances.sqlite3")
        database.__enter__()
        self.addCleanup(database.__exit__, None, None, None)
        guild_settings.set_target_guild(10, "Realm")
        guild_settings.set_target_guild(11, "Other Guild")
        for guild_id in (10, 11):
            local_repository.register_player(guild_id, TARGET_ID, "Mamaliga")
            local_repository.change_balance(guild_id, TARGET_ID, 1000)
        local_repository.cache_siphon(10, TARGET_ID, 25)
        self.guild = SimpleNamespace(id=10)
        self.actor = MagicMock(spec=discord.Member)
        self.actor.id = 99
        self.actor.mention = "<@99>"
        self.actor.display_name = "Officer"
        self.actor.guild_permissions = discord.Permissions(administrator=True)
        self.member = MagicMock(spec=discord.Member)
        self.member.id = TARGET_ID
        self.member.mention = f"<@{TARGET_ID}>"
        commands = economy_commands.create_economy_commands(SimpleNamespace())
        self.remove = next(command for command in commands if command.name == "bal-remove")
        self.add = next(command for command in commands if command.name == "bal-add")
        self.next_interaction_id = 100
        ready_patch = patch.object(
            economy_commands, "_ensure_local_ledger_ready", new=AsyncMock(return_value=True)
        )
        ready_patch.start()
        self.addCleanup(ready_patch.stop)
        projection_patch = patch.object(
            economy_commands,
            "_project_linked_players_after_commit",
            new=AsyncMock(return_value=None),
        )
        self.project = projection_patch.start()
        self.addCleanup(projection_patch.stop)

    def interaction(self):
        self.next_interaction_id += 1
        return SimpleNamespace(
            id=self.next_interaction_id,
            guild=self.guild,
            user=self.actor,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    def player(self):
        return local_repository.get_player(10, TARGET_ID)

    async def test_all_three_selectors_update_only_the_current_server_and_preserve_siphon(self):
        original = self.player()
        for index, target in enumerate(
            (
                {"member": self.member},
                {"discord_id": f" {TARGET_ID} "},
                {"albion_nickname": " mAMALIGA "},
            ),
            start=1,
        ):
            with self.subTest(target=next(iter(target))):
                interaction = self.interaction()
                await self.remove.callback(interaction, "100", **target)

                self.assertEqual(1000 - index * 100, self.player().silver)
                self.assertEqual(1000, self.player().all_time_earnings)
                self.assertEqual(original.siphon, self.player().siphon)
                self.assertEqual(original.siphon_synced_at, self.player().siphon_synced_at)
                self.assertEqual(1000, local_repository.get_player(11, TARGET_ID).silver)
                embed = interaction.followup.send.await_args.kwargs["embed"]
                self.assertIn(f"from <@{TARGET_ID}>", embed.description)
                self.assertEqual("Payout", embed.fields[0].value)
                self.project.assert_awaited_with(10, (TARGET_ID,))

    async def test_id_and_nickname_work_for_registered_players_who_left(self):
        local_repository.set_in_guild(10, TARGET_ID, False)
        for target in ({"discord_id": str(TARGET_ID)}, {"albion_nickname": "Mamaliga"}):
            await self.remove.callback(self.interaction(), "100", **target)
        self.assertEqual(800, self.player().silver)
        self.assertFalse(self.player().is_active)

    async def test_missing_or_multiple_selectors_are_rejected_without_mutation(self):
        for target in (
            {},
            {"member": self.member, "discord_id": str(TARGET_ID)},
            {"discord_id": str(TARGET_ID), "albion_nickname": "Mamaliga"},
            {"member": self.member, "albion_nickname": "Mamaliga"},
        ):
            with self.subTest(target=list(target)):
                interaction = self.interaction()
                await self.remove.callback(interaction, "100", **target)
                self.assertIn("exactly one", interaction.response.send_message.call_args.args[0])
                self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])
                self.assertEqual(1000, self.player().silver)
        self.project.assert_not_awaited()

    async def test_invalid_id_and_blank_nickname_are_rejected(self):
        for target in (
            *(
                {"discord_id": value}
                for value in ("", "0", "-1", "1.5", "abc", "9" * 20, str(1 << 63))
            ),
            {"albion_nickname": "   "},
        ):
            with self.subTest(target=target):
                interaction = self.interaction()
                await self.remove.callback(interaction, "100", **target)
                interaction.response.send_message.assert_awaited_once()
                interaction.response.defer.assert_not_awaited()
                self.assertEqual(1000, self.player().silver)
        self.project.assert_not_awaited()

    async def test_unregistered_id_or_nickname_never_falls_back_to_another_player(self):
        local_repository.register_player(11, 123, "OtherServerOnly")
        for target in (
            {"discord_id": "123"},
            {"albion_nickname": "OtherServerOnly"},
            {"albion_nickname": "Mamal"},
        ):
            with self.subTest(target=target):
                interaction = self.interaction()
                await self.remove.callback(interaction, "100", **target)
                self.assertIn("registered", interaction.followup.send.call_args.args[0])
                self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])
                self.assertEqual(1000, self.player().silver)
        self.project.assert_not_awaited()

    async def test_old_ledger_is_not_used_after_reconfiguration(self):
        guild_settings.set_target_guild(10, "New Realm")
        interaction = self.interaction()
        await self.remove.callback(interaction, "100", albion_nickname="Mamaliga")
        self.assertIn("No registered player", interaction.followup.send.call_args.args[0])
        self.assertEqual(1000, self.player().silver)
        self.project.assert_not_awaited()

    async def test_authorization_is_required_and_rechecked_after_deferring(self):
        for access in ([False], [True, False]):
            with (
                self.subTest(access=access),
                patch.object(
                    economy_commands.economy_access,
                    "has_economy_access",
                    new=AsyncMock(side_effect=access),
                ),
            ):
                interaction = self.interaction()
                await self.remove.callback(interaction, "100", discord_id=str(TARGET_ID))
                response = (
                    interaction.response.send_message
                    if len(access) == 1
                    else interaction.followup.send
                )
                self.assertIn("permission", response.call_args.args[0])
                self.assertEqual(1000, self.player().silver)
        self.project.assert_not_awaited()

    async def test_amount_validation_is_preserved(self):
        for amount in ("0", "-1", "1.5", "abc", str(economy_commands.MAX_SILVER_TRANSACTION + 1)):
            with self.subTest(amount=amount):
                interaction = self.interaction()
                await self.remove.callback(interaction, amount, albion_nickname="Mamaliga")
                self.assertIn("remove_silver", interaction.response.send_message.call_args.args[0])
                self.assertEqual(1000, self.player().silver)
        self.project.assert_not_awaited()

    async def test_sqlite_commit_audit_clamping_and_idempotency_are_preserved(self):
        async def project_after_commit(guild_id, discord_user_ids):
            self.assertEqual(0, self.player().silver)
            return None

        self.project.side_effect = project_after_commit
        interaction = self.interaction()
        for _ in range(2):
            await self.remove.callback(
                interaction, "1500", discord_id=str(TARGET_ID), reason="Withdrawal"
            )
        self.assertEqual(0, self.player().silver)
        self.assertEqual(1000, self.player().all_time_earnings)
        with sqlite_database.connection() as database:
            history = database.execute(
                "SELECT * FROM balance_history WHERE guild_id = 10 AND reason = 'Withdrawal'"
            ).fetchall()
        self.assertEqual(1, len(history))
        self.assertEqual(TARGET_ID, history[0]["discord_user_id"])
        self.assertEqual("Mamaliga", history[0]["nickname_snapshot"])
        self.assertEqual(99, history[0]["actor_discord_user_id"])
        self.assertEqual(-1500, history[0]["requested_delta"])
        self.assertEqual(-1000, history[0]["actual_delta"])
        embed = interaction.followup.send.await_args.kwargs["embed"]
        self.assertIn("removed 1,000", embed.description)

    async def test_bal_add_still_uses_member_picker(self):
        interaction = self.interaction()
        await self.add.callback(interaction, self.member, "100")
        self.assertEqual(1100, self.player().silver)
        self.assertEqual(1100, self.player().all_time_earnings)
        embed = interaction.followup.send.await_args.kwargs["embed"]
        self.assertIn(f"to <@{TARGET_ID}>", embed.description)
        self.assertEqual("Manual", embed.fields[0].value)
