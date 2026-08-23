import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.realm_protector.bot import economy_commands


class EconomyMentionSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_lootsplit_allows_only_credited_participant_mentions(self) -> None:
        class FakeMember:
            def __init__(self, member_id: int, display_name: str) -> None:
                self.id = member_id
                self.display_name = display_name

        actor = FakeMember(10, "Officer")
        caller = FakeMember(20, "Caller")
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=30),
            user=actor,
            response=SimpleNamespace(
                send_message=AsyncMock(),
                defer=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        result = SimpleNamespace(
            credits=(
                SimpleNamespace(
                    nickname="Credited",
                    discord_user_id=111,
                ),
                SimpleNamespace(
                    nickname="Also credited",
                    discord_user_id=222,
                ),
            ),
            missing_nicknames=("<@999>",),
        )

        lootsplit = next(
            command
            for command in economy_commands.create_economy_commands(SimpleNamespace())
            if command.name == "lootsplit"
        )
        with (
            patch.object(economy_commands.discord, "Member", FakeMember),
            patch.object(
                economy_commands.economy_access,
                "has_economy_access",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                economy_commands,
                "_ensure_local_ledger_ready",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                economy_commands.local_repository,
                "get_player",
                return_value=None,
            ),
            patch.object(
                economy_commands.local_repository,
                "apply_lootsplit",
                return_value=result,
            ),
            patch.object(
                economy_commands,
                "_active_ledger_id",
                return_value=30,
            ),
            patch.object(
                economy_commands,
                "send_followup_lines",
                new=AsyncMock(),
            ) as send_lines,
            patch.object(
                economy_commands,
                "_project_linked_players_after_commit",
                new=AsyncMock(return_value=None),
            ) as project_players,
        ):
            await lootsplit.callback(
                interaction,
                "123",
                "Content <@999>",
                caller,
                "Credited,<@999>",
                "100",
                None,
            )

        send_lines.assert_awaited_once()
        project_players.assert_awaited_once_with(30, (111, 222))
        allowed_mentions = send_lines.await_args.kwargs["allowed_mentions"]
        self.assertEqual([111, 222], [user.id for user in allowed_mentions.users])
        self.assertFalse(allowed_mentions.roles)
        self.assertFalse(allowed_mentions.everyone)
        self.assertFalse(allowed_mentions.replied_user)


class EconomyProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_balance_change_projects_linked_player_after_local_commit(
        self,
    ) -> None:
        class FakeMember:
            def __init__(self, member_id: int, name: str) -> None:
                self.id = member_id
                self.display_name = name
                self.mention = f"<@{member_id}>"

        actor = FakeMember(10, "Officer")
        target = FakeMember(20, "Player")
        interaction = SimpleNamespace(
            id=99,
            guild=SimpleNamespace(id=30),
            user=actor,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        result = SimpleNamespace(
            actual_delta=3,
            previous_balance=0,
            updated_balance=3,
        )
        pending = economy_commands.google_sync.SyncResult(
            False,
            "pending",
            incomplete=True,
        )
        command = next(
            item
            for item in economy_commands.create_economy_commands(SimpleNamespace())
            if item.name == "bal-add"
        )
        with (
            patch.object(economy_commands.discord, "Member", FakeMember),
            patch.object(
                economy_commands.economy_access,
                "has_economy_access",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                economy_commands,
                "_ensure_local_ledger_ready",
                new=AsyncMock(return_value=True),
            ),
            patch.object(economy_commands, "_active_ledger_id", return_value=30),
            patch.object(
                economy_commands,
                "_resolve_member_local_name",
                return_value="Officer",
            ),
            patch.object(
                economy_commands.local_repository,
                "change_balance",
                return_value=result,
            ) as change_balance,
            patch.object(
                economy_commands,
                "_project_linked_players_after_commit",
                new=AsyncMock(return_value=pending),
            ) as project_players,
        ):
            await command.callback(interaction, target, "3", "Manual")

        change_balance.assert_called_once()
        project_players.assert_awaited_once_with(30, (20,))
        embed = interaction.followup.send.await_args.kwargs["embed"]
        self.assertIn("Google Sheet update is queued", embed.footer.text)

    async def test_negative_siphon_reads_only_current_active_local_cache(self) -> None:
        class FakeMember:
            id = 10

        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=30),
            user=FakeMember(),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        command = next(
            item
            for item in economy_commands.create_economy_commands(SimpleNamespace())
            if item.name == "get-negative-siphon"
        )
        with (
            patch.object(economy_commands.discord, "Member", FakeMember),
            patch.object(
                economy_commands.economy_access,
                "has_economy_access",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                economy_commands,
                "_ensure_local_ledger_ready",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                economy_commands.credential_store,
                "get_credentials_info",
                return_value={"status": "active"},
            ),
            patch.object(economy_commands, "_active_ledger_id", return_value=30),
            patch.object(
                economy_commands.local_repository,
                "list_negative_siphon",
                return_value=[],
            ) as list_negative,
            patch.object(
                economy_commands.google_sync,
                "sync_siphon_from_sheet",
                new=AsyncMock(),
            ) as remote_sync,
        ):
            await command.callback(interaction)

        list_negative.assert_called_once_with(30, active_only=True)
        remote_sync.assert_not_awaited()
        self.assertIn("/sync-siphon", interaction.followup.send.await_args.args[0])

    async def test_negative_siphon_does_not_use_cache_without_an_active_sheet(self) -> None:
        class FakeMember:
            id = 10

        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=30),
            user=FakeMember(),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        command = next(
            item
            for item in economy_commands.create_economy_commands(SimpleNamespace())
            if item.name == "get-negative-siphon"
        )
        with (
            patch.object(economy_commands.discord, "Member", FakeMember),
            patch.object(
                economy_commands.economy_access,
                "has_economy_access",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                economy_commands,
                "_ensure_local_ledger_ready",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                economy_commands.credential_store,
                "get_credentials_info",
                return_value=None,
            ),
            patch.object(
                economy_commands.local_repository,
                "list_negative_siphon",
            ) as list_negative,
        ):
            await command.callback(interaction)

        list_negative.assert_not_called()
        self.assertIn(
            "active Google Sheet link",
            interaction.followup.send.await_args.args[0],
        )


class EconomyDisplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefix_balance_shows_own_balance_and_all_time_earnings(self) -> None:
        author = SimpleNamespace(
            id=20,
            mention="<@20>",
            display_avatar=SimpleNamespace(url="https://example.com/avatar.png"),
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            author=author,
            send=AsyncMock(),
        )
        snapshot = SimpleNamespace(
            silver=75,
            all_time_earnings=125,
            siphon=None,
            siphon_revision=None,
            revision=2,
            siphon_synced_at=None,
        )
        balance = next(
            command
            for command in economy_commands.create_prefix_economy_commands(SimpleNamespace())
            if command.name == "bal"
        )
        with (
            patch.object(
                economy_commands,
                "_local_ledger_unavailable_message",
                return_value=None,
            ),
            patch.object(
                economy_commands._balance_lookup_cooldown,
                "claim",
                return_value=0,
            ),
            patch.object(economy_commands, "_active_ledger_id", return_value=10),
            patch.object(
                economy_commands.local_repository,
                "get_balance_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                economy_commands.local_repository,
                "get_silver_leaderboard_position",
                return_value=4,
            ) as get_position,
            patch.object(
                economy_commands.credential_store,
                "get_credentials_info",
                return_value=None,
            ),
        ):
            await balance.callback(ctx)

        embed = ctx.send.await_args.kwargs["embed"]
        self.assertEqual("### <@20> balance:", embed.description)
        self.assertEqual(
            ["Balance", "Siphon", "All-time earnings", "Raw balance"],
            [field.name for field in embed.fields],
        )
        self.assertTrue(embed.fields[0].inline)
        self.assertTrue(embed.fields[1].inline)
        self.assertFalse(embed.fields[2].inline)
        self.assertFalse(embed.fields[3].inline)
        self.assertEqual("125 :coin:", embed.fields[2].value)
        self.assertEqual("Leaderboard position: #4", embed.footer.text)
        get_position.assert_called_once_with(10, 20)

    async def test_slash_balance_shows_self_or_selected_member_position(self) -> None:
        invoker = SimpleNamespace(
            id=20,
            mention="<@20>",
            display_avatar=SimpleNamespace(url="https://example.com/invoker.png"),
        )
        selected_member = SimpleNamespace(
            id=30,
            mention="<@30>",
            display_avatar=SimpleNamespace(url="https://example.com/member.png"),
        )
        snapshot = SimpleNamespace(
            silver=75,
            all_time_earnings=125,
            siphon=None,
            siphon_revision=None,
            revision=2,
            siphon_synced_at=None,
        )
        balance = next(
            command
            for command in economy_commands.create_economy_commands(SimpleNamespace())
            if command.name == "bal"
        )

        for selected, expected_target, position in (
            (None, invoker, 4),
            (selected_member, selected_member, 7),
        ):
            with self.subTest(target_id=expected_target.id):
                interaction = SimpleNamespace(
                    guild=SimpleNamespace(id=10),
                    user=invoker,
                    response=SimpleNamespace(
                        send_message=AsyncMock(),
                        defer=AsyncMock(),
                    ),
                    followup=SimpleNamespace(send=AsyncMock()),
                )
                with (
                    patch.object(
                        economy_commands,
                        "_ensure_local_ledger_ready",
                        new=AsyncMock(return_value=True),
                    ),
                    patch.object(
                        economy_commands._balance_lookup_cooldown,
                        "claim",
                        return_value=0,
                    ),
                    patch.object(economy_commands, "_active_ledger_id", return_value=10),
                    patch.object(
                        economy_commands.local_repository,
                        "get_balance_snapshot",
                        return_value=snapshot,
                    ),
                    patch.object(
                        economy_commands.local_repository,
                        "get_silver_leaderboard_position",
                        return_value=position,
                    ) as get_position,
                    patch.object(
                        economy_commands.credential_store,
                        "get_credentials_info",
                        return_value=None,
                    ),
                ):
                    await balance.callback(interaction, selected)

                embed = interaction.followup.send.await_args.kwargs["embed"]
                self.assertEqual(
                    f"### {expected_target.mention} balance:",
                    embed.description,
                )
                self.assertEqual(
                    f"Leaderboard position: #{position}",
                    embed.footer.text,
                )
                get_position.assert_called_once_with(10, expected_target.id)

    async def test_prefix_leaderboard_has_ten_rows_navigation_and_page_counter(
        self,
    ) -> None:
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            author=SimpleNamespace(id=20),
            send=AsyncMock(),
        )
        players = tuple(
            SimpleNamespace(discord_user_id=index, silver=1_000 - index) for index in range(1, 11)
        )
        page = local_page = SimpleNamespace(
            players=players,
            total_players=12,
            limit=10,
            offset=0,
        )
        leaderboard = next(
            command
            for command in economy_commands.create_prefix_economy_commands(SimpleNamespace())
            if command.name == "lb"
        )
        with (
            patch.object(
                economy_commands,
                "_local_ledger_unavailable_message",
                return_value=None,
            ),
            patch.object(economy_commands, "_active_ledger_id", return_value=10),
            patch.object(
                economy_commands.local_repository,
                "get_silver_leaderboard",
                return_value=local_page,
            ),
        ):
            await leaderboard.callback(ctx)

        kwargs = ctx.send.await_args.kwargs
        self.assertEqual(10, len(kwargs["embed"].description.splitlines()))
        self.assertEqual("1. <@1> - :coin: 999", kwargs["embed"].description.splitlines()[0])
        self.assertEqual("Page 1/2", kwargs["embed"].footer.text)
        self.assertEqual(2, len(kwargs["view"].children))
        self.assertTrue(kwargs["view"].children[0].disabled)
        self.assertFalse(kwargs["view"].children[1].disabled)
        self.assertIs(page, local_page)

    async def test_leaderboard_next_button_reads_and_edits_the_persisted_page(
        self,
    ) -> None:
        first_page = SimpleNamespace(
            players=(),
            total_players=12,
            limit=10,
            offset=0,
        )
        second_page = SimpleNamespace(
            players=(SimpleNamespace(discord_user_id=12, silver=50),),
            total_players=12,
            limit=10,
            offset=10,
        )
        message = SimpleNamespace(
            id=99,
            embeds=[
                economy_commands._build_leaderboard_embed(
                    first_page,
                    page_number=1,
                )
            ],
            edit=AsyncMock(),
        )
        message.channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            message=message,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with (
            patch.object(economy_commands, "_active_ledger_id", return_value=10),
            patch.object(
                economy_commands.local_repository,
                "get_silver_leaderboard",
                side_effect=(first_page, second_page),
            ) as get_page,
        ):
            await economy_commands._navigate_leaderboard(interaction, 1)

        interaction.response.defer.assert_awaited_once_with()
        self.assertEqual(0, get_page.call_args_list[0].kwargs["offset"])
        self.assertEqual(10, get_page.call_args_list[1].kwargs["offset"])
        edited = message.edit.await_args.kwargs
        self.assertEqual("Page 2/2", edited["embed"].footer.text)
        self.assertEqual("11. <@12> - :coin: 50", edited["embed"].description)
        self.assertFalse(edited["view"].children[0].disabled)
        self.assertTrue(edited["view"].children[1].disabled)


if __name__ == "__main__":
    unittest.main()
