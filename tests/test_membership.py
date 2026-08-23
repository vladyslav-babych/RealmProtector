import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.realm_protector.services import guild_lifecycle, membership


class MembershipLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_tracker_is_cancelled_and_reset_during_shutdown(self) -> None:
        await membership.stop_guild_member_tracker()
        started = asyncio.Event()

        async def worker(_bot):
            started.set()
            await asyncio.Event().wait()

        with patch.object(membership, "_tracker_loop", side_effect=worker):
            membership.start_guild_member_tracker(SimpleNamespace())
            await started.wait()
            task = membership._tracker_task
            await membership.stop_guild_member_tracker()

        self.assertIsNotNone(task)
        self.assertTrue(task.done())
        self.assertIsNone(membership._tracker_task)

    async def test_discord_lookup_failure_does_not_hide_local_albion_truth(self) -> None:
        class PermissionFailure(Exception):
            pass

        player = SimpleNamespace(
            discord_user_id=99,
            nickname="Player",
            albion_player_id="albion-99",
            is_active=True,
            revision=1,
            updated_at="",
        )
        guild = SimpleNamespace(
            id=424242,
            get_member=lambda _member_id: None,
            fetch_member=AsyncMock(side_effect=PermissionFailure()),
        )
        with (
            patch.object(
                membership.local_repository,
                "get_active_ledger_id",
                return_value=777,
            ),
            patch.object(
                membership.local_repository,
                "list_active_players",
                return_value=[player],
            ),
            patch.object(
                membership.local_repository,
                "set_in_guild",
            ) as set_in_guild,
            patch.object(
                membership.local_repository,
                "get_player",
                return_value=player,
            ),
            patch.object(
                membership.external_io,
                "run_albion",
                new=AsyncMock(return_value={"GuildName": "Other Guild"}),
            ) as run_albion,
            patch.object(
                membership.guild_settings,
                "get_configuration",
                return_value=SimpleNamespace(
                    target_guild_name="Realm",
                    leave_action="kick",
                ),
            ),
            patch.object(membership.discord, "Forbidden", PermissionFailure),
            patch.object(membership.asyncio, "sleep", new=AsyncMock()),
        ):
            await membership._process_server_with_local_storage(
                SimpleNamespace(),
                guild,
                "Realm",
                "kick",
                guild_lifecycle.generation(guild.id),
            )

        self.assertEqual(
            [
                (membership.albion_api.get_player_profile_by_id, "albion-99"),
            ]
            * 3,
            [call.args for call in run_albion.await_args_list],
        )
        set_in_guild.assert_called_once_with(777, 99, False)

    async def test_stale_audit_cannot_persist_or_apply_leave_action(self) -> None:
        guild_id = 424243
        captured_generation = guild_lifecycle.generation(guild_id)
        guild_lifecycle.advance(guild_id)

        player = SimpleNamespace(
            discord_user_id=99,
            nickname="Player",
            albion_player_id="albion-99",
            is_active=True,
            revision=1,
            updated_at="",
        )
        member = SimpleNamespace(id=99)
        guild = SimpleNamespace(
            id=guild_id,
            get_member=lambda _member_id: member,
        )

        with (
            patch.object(
                membership.local_repository,
                "get_active_ledger_id",
                return_value=778,
            ),
            patch.object(
                membership.local_repository,
                "list_active_players",
                return_value=[player],
            ),
            patch.object(
                membership.local_repository,
                "set_in_guild",
            ) as set_in_guild,
            patch.object(
                membership.external_io,
                "run_albion",
                new=AsyncMock(return_value={"GuildName": "Other Guild"}),
            ),
            patch.object(
                membership.guild_settings,
                "get_configuration",
                return_value=SimpleNamespace(
                    target_guild_name="Realm",
                    leave_action="kick",
                ),
            ),
            patch.object(
                membership,
                "_apply_leave_action",
                new=AsyncMock(return_value=True),
            ) as apply_action,
            patch.object(membership.asyncio, "sleep", new=AsyncMock()),
        ):
            await membership._process_server_with_local_storage(
                SimpleNamespace(),
                guild,
                "Realm",
                "kick",
                captured_generation,
            )

        set_in_guild.assert_not_called()
        apply_action.assert_not_awaited()

    async def test_departure_requires_repeated_negative_albion_results(self) -> None:
        player = SimpleNamespace(
            discord_user_id=99,
            nickname="Player",
            albion_player_id="albion-99",
            is_active=True,
            revision=1,
            updated_at="",
        )
        run_albion = AsyncMock(
            side_effect=(
                {"GuildName": "Other Guild"},
                {"GuildName": "Realm"},
            )
        )
        with (
            patch.object(membership.external_io, "run_albion", new=run_albion),
            patch.object(membership.asyncio, "sleep", new=AsyncMock()),
            patch.object(membership.local_repository, "set_in_guild") as set_in_guild,
        ):
            await membership._audit_local_player(
                SimpleNamespace(),
                SimpleNamespace(id=42),
                player,
                77,
                "Realm",
                "kick",
                guild_lifecycle.generation(42),
            )

        self.assertEqual(2, run_albion.await_count)
        set_in_guild.assert_not_called()

    async def test_recent_registration_has_a_membership_grace_period(self) -> None:
        player = SimpleNamespace(
            discord_user_id=99,
            nickname="Player",
            albion_player_id="albion-99",
            is_active=True,
            revision=1,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        with patch.object(
            membership.external_io,
            "run_albion",
            new=AsyncMock(),
        ) as run_albion:
            await membership._audit_local_player(
                SimpleNamespace(),
                SimpleNamespace(id=43),
                player,
                78,
                "Realm",
                "kick",
                guild_lifecycle.generation(43),
            )

        run_albion.assert_not_awaited()

    async def test_confirmed_departure_persists_no_before_configured_action(self) -> None:
        guild_id = 424245
        events: list[str] = []
        player = SimpleNamespace(
            discord_user_id=99,
            nickname="Player",
            albion_player_id="albion-99",
            is_active=True,
            revision=4,
            updated_at="",
        )
        member = SimpleNamespace(id=99)
        guild = SimpleNamespace(
            id=guild_id,
            get_member=lambda _member_id: member,
        )

        def set_in_guild(_ledger_id, _user_id, value):
            self.assertFalse(value)
            events.append("local-no")

        async def apply_action(_bot, _guild, _member, action):
            self.assertEqual("remove_roles", action)
            events.append("leave-action")
            return True

        with (
            patch.object(
                membership.external_io,
                "run_albion",
                new=AsyncMock(return_value={"GuildName": "Other Guild"}),
            ),
            patch.object(membership.asyncio, "sleep", new=AsyncMock()),
            patch.object(
                membership.guild_settings,
                "get_configuration",
                return_value=SimpleNamespace(
                    target_guild_name="Realm",
                    leave_action="remove_roles",
                ),
            ),
            patch.object(
                membership.local_repository,
                "get_player",
                return_value=player,
            ),
            patch.object(
                membership.local_repository,
                "set_in_guild",
                side_effect=set_in_guild,
            ),
            patch.object(
                membership,
                "_apply_leave_action",
                new=AsyncMock(side_effect=apply_action),
            ),
        ):
            await membership._audit_local_player(
                SimpleNamespace(),
                guild,
                player,
                79,
                "Realm",
                "remove_roles",
                guild_lifecycle.generation(guild_id),
            )

        self.assertEqual(["local-no", "leave-action"], events)

    async def test_albion_lookups_are_concurrent_but_bounded(self) -> None:
        guild_id = 424244
        players = [
            SimpleNamespace(
                discord_user_id=index,
                nickname=f"Player {index}",
                albion_player_id=f"albion-{index}",
            )
            for index in range(1, 10)
        ]
        guild = SimpleNamespace(id=guild_id)
        active = 0
        maximum_active = 0

        async def run_albion(_function, _player_id):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {"GuildName": "Realm"}

        with (
            patch.object(
                membership.local_repository,
                "get_active_ledger_id",
                return_value=779,
            ),
            patch.object(
                membership.local_repository,
                "list_active_players",
                return_value=players,
            ),
            patch.object(
                membership.external_io,
                "run_albion",
                new=AsyncMock(side_effect=run_albion),
            ),
        ):
            await membership._process_server_with_local_storage(
                SimpleNamespace(),
                guild,
                "Realm",
                "kick",
                guild_lifecycle.generation(guild.id),
            )

        self.assertEqual(membership._MEMBERSHIP_AUDIT_CONCURRENCY, maximum_active)


if __name__ == "__main__":
    unittest.main()
