import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.realm_protector.infrastructure import albion_api, local_repository, runtime_state
from src.realm_protector.services import registration


def _context_and_role():
    role = SimpleNamespace(id=30, name="Member")
    member = SimpleNamespace(id=20, roles=[])
    guild = SimpleNamespace(
        id=10,
        get_role=lambda _role_id: role,
        roles=[role],
    )
    context = SimpleNamespace(
        author=member,
        guild=guild,
        send=AsyncMock(),
    )
    return context, role


class RegistrationMutationSafetyTests(unittest.IsolatedAsyncioTestCase):
    def _base_patches(self, role, result, projection=None):
        projection_mock = projection if projection is not None else AsyncMock(return_value=None)
        return (
            patch.object(
                registration,
                "_get_player_profile_with_retries",
                new=AsyncMock(
                    return_value={
                        "Id": "same-albion-id",
                        "Name": "Player",
                        "GuildName": "Realm",
                    }
                ),
            ),
            patch.object(
                registration,
                "_commit_registration",
                new=AsyncMock(return_value=result),
            ),
            patch.object(
                registration.guild_settings,
                "get_configuration",
                return_value=SimpleNamespace(
                    target_guild_name="Realm",
                    member_role_id=role.id,
                    member_role_name="Member",
                ),
            ),
            patch.object(
                registration.guild_lifecycle,
                "is_current",
                return_value=True,
            ),
            patch.object(registration, "self_assignment_error", return_value=None),
            patch.object(
                registration,
                "_save_side_effect_intent",
                new=AsyncMock(),
            ),
            patch.object(
                registration,
                "_delete_side_effect_intent",
                new=AsyncMock(),
            ),
            patch.object(
                registration,
                "_project_registered_player_after_commit",
                new=projection_mock,
            ),
        )

    async def test_albion_id_conflict_never_changes_discord_member(self) -> None:
        context, role = _context_and_role()
        result = SimpleNamespace(
            status=local_repository.RegistrationStatus.ALBION_ID_CONFLICT,
            player=None,
        )
        project_player = AsyncMock(return_value=None)
        base_patches = self._base_patches(role, result, project_player)
        with ExitStack() as stack:
            for patcher in base_patches:
                stack.enter_context(patcher)
            apply_effects = stack.enter_context(
                patch.object(
                    registration,
                    "_apply_registration_side_effects",
                    new=AsyncMock(),
                )
            )
            await registration.register_user(
                context,
                "Player",
                "same-albion-id",
                "Realm",
                registration.guild_lifecycle.generation(context.guild.id),
            )

        apply_effects.assert_not_awaited()
        project_player.assert_not_awaited()
        context.send.assert_awaited_once_with("Character **Player** is already registered.")

    async def test_already_registered_does_not_promise_nickname_retry(self) -> None:
        context, role = _context_and_role()
        player = SimpleNamespace(
            nickname="Canonical Player",
            albion_player_id="same-albion-id",
        )
        result = SimpleNamespace(
            status=local_repository.RegistrationStatus.ALREADY_REGISTERED,
            player=player,
        )
        base_patches = self._base_patches(role, result)
        with ExitStack() as stack:
            for patcher in base_patches:
                stack.enter_context(patcher)
            apply_effects = stack.enter_context(
                patch.object(
                    registration,
                    "_apply_registration_side_effects",
                    new=AsyncMock(return_value=(False, True)),
                )
            )
            record_effects = stack.enter_context(
                patch.object(
                    registration,
                    "_record_side_effect_result",
                    new=AsyncMock(),
                )
            )
            await registration.register_user(
                context,
                "Player",
                "same-albion-id",
                "Realm",
                registration.guild_lifecycle.generation(context.guild.id),
            )

        apply_effects.assert_awaited_once_with(
            context.author,
            role,
            "Canonical Player",
        )
        record_effects.assert_awaited_once()
        context.send.assert_awaited_once_with(
            "You are already registered.\nI could not update your Discord nickname."
        )

    async def test_created_registration_projects_the_player_after_local_success(self) -> None:
        context, role = _context_and_role()
        player = SimpleNamespace(
            nickname="Canonical Player",
            albion_player_id="same-albion-id",
        )
        result = SimpleNamespace(
            status=local_repository.RegistrationStatus.CREATED,
            player=player,
        )
        project_player = AsyncMock(
            return_value=registration.google_sync.SyncResult(True, "projected")
        )
        base_patches = self._base_patches(role, result, project_player)
        with ExitStack() as stack:
            for patcher in base_patches:
                stack.enter_context(patcher)
            stack.enter_context(
                patch.object(
                    registration,
                    "_apply_registration_side_effects",
                    new=AsyncMock(return_value=(True, True)),
                )
            )
            stack.enter_context(
                patch.object(
                    registration,
                    "_record_side_effect_result",
                    new=AsyncMock(),
                )
            )
            await registration.register_user(
                context,
                "Player",
                "same-albion-id",
                "Realm",
                registration.guild_lifecycle.generation(context.guild.id),
            )

        project_player.assert_awaited_once_with(10, 20)
        context.send.assert_awaited_once_with("**Canonical Player** was registered successfully.")

    async def test_registration_survives_a_failed_google_projection(self) -> None:
        context, role = _context_and_role()
        player = SimpleNamespace(
            nickname="Canonical Player",
            albion_player_id="same-albion-id",
        )
        result = SimpleNamespace(
            status=local_repository.RegistrationStatus.CREATED,
            player=player,
        )
        project_player = AsyncMock(
            return_value=registration.google_sync.SyncResult(
                False,
                "temporary failure",
                incomplete=True,
            )
        )
        base_patches = self._base_patches(role, result, project_player)
        with ExitStack() as stack:
            for patcher in base_patches:
                stack.enter_context(patcher)
            stack.enter_context(
                patch.object(
                    registration,
                    "_apply_registration_side_effects",
                    new=AsyncMock(return_value=(True, True)),
                )
            )
            stack.enter_context(
                patch.object(
                    registration,
                    "_record_side_effect_result",
                    new=AsyncMock(),
                )
            )
            await registration.register_user(
                context,
                "Player",
                "same-albion-id",
                "Realm",
                registration.guild_lifecycle.generation(context.guild.id),
            )

        self.assertIn(
            "Google Sheet update remains queued",
            context.send.await_args.args[0],
        )

    async def test_projection_helper_contains_an_unexpected_google_failure(self) -> None:
        with (
            patch.object(
                registration.google_sync,
                "project_linked_players",
                new=AsyncMock(side_effect=RuntimeError("offline")),
            ) as project_players,
            self.assertLogs(registration.LOGGER, level="ERROR"),
        ):
            result = await registration._project_registered_player_after_commit(10, 20)

        project_players.assert_awaited_once_with(10, (20,))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.success)

    async def test_force_register_reactivates_without_resetting_local_player(self) -> None:
        role = SimpleNamespace(id=30, name="Member")
        member = SimpleNamespace(id=20, mention="<@20>", roles=[])
        guild = SimpleNamespace(
            id=10,
            get_role=lambda _role_id: role,
            roles=[role],
        )
        stored_player = SimpleNamespace(
            discord_user_id=20,
            nickname="Canonical Player",
            albion_player_id="stable-id",
            is_active=False,
        )
        canonical_player = SimpleNamespace(
            nickname="Canonical Player",
            albion_player_id="stable-id",
        )
        registration_result = SimpleNamespace(
            status=local_repository.RegistrationStatus.REACTIVATED,
            player=canonical_player,
        )
        configuration = SimpleNamespace(
            target_guild_name="Realm",
            member_role_id=role.id,
            member_role_name="Member",
        )

        project_player = AsyncMock(return_value=registration.google_sync.SyncResult(True, "done"))
        with (
            patch.object(
                registration.guild_settings,
                "get_configuration",
                return_value=configuration,
            ),
            patch.object(
                registration.local_repository,
                "get_active_ledger_id",
                return_value=77,
            ),
            patch.object(
                registration.local_repository,
                "get_player",
                return_value=stored_player,
            ),
            patch.object(
                registration,
                "_get_registered_player_profile_with_retries",
                new=AsyncMock(
                    return_value={
                        "Id": "stable-id",
                        "Name": "Canonical Player",
                        "GuildName": "Realm",
                    }
                ),
            ),
            patch.object(registration.guild_lifecycle, "is_current", return_value=True),
            patch.object(registration, "self_assignment_error", return_value=None),
            patch.object(
                registration,
                "_save_side_effect_intent",
                new=AsyncMock(),
            ),
            patch.object(
                registration.local_repository,
                "register_player",
                return_value=registration_result,
            ) as register_player,
            patch.object(
                registration,
                "_apply_registration_side_effects",
                new=AsyncMock(return_value=(True, True)),
            ) as apply_effects,
            patch.object(
                registration,
                "_record_side_effect_result",
                new=AsyncMock(),
            ) as record_effects,
            patch.object(
                registration,
                "_project_registered_player_after_commit",
                new=project_player,
            ),
        ):
            message = await registration.force_register_member(guild, member)

        register_player.assert_called_once_with(
            77,
            20,
            "Canonical Player",
            "stable-id",
        )
        apply_effects.assert_awaited_once_with(member, role, "Canonical Player")
        record_effects.assert_awaited_once()
        project_player.assert_awaited_once_with(10, 20)
        self.assertIn("marked **in guild**", message)

    async def test_force_register_does_not_reactivate_wrong_albion_guild(self) -> None:
        member = SimpleNamespace(id=20, mention="<@20>")
        guild = SimpleNamespace(id=10)
        stored_player = SimpleNamespace(
            nickname="Canonical Player",
            albion_player_id="stable-id",
        )
        with (
            patch.object(
                registration.guild_settings,
                "get_configuration",
                return_value=SimpleNamespace(target_guild_name="Realm"),
            ),
            patch.object(
                registration.local_repository,
                "get_active_ledger_id",
                return_value=77,
            ),
            patch.object(
                registration.local_repository,
                "get_player",
                return_value=stored_player,
            ),
            patch.object(
                registration,
                "_get_registered_player_profile_with_retries",
                new=AsyncMock(
                    return_value={
                        "Id": "stable-id",
                        "Name": "Canonical Player",
                        "GuildName": "Other Guild",
                    }
                ),
            ),
            patch.object(
                registration.local_repository,
                "register_player",
            ) as register_player,
        ):
            message = await registration.force_register_member(guild, member)

        register_player.assert_not_called()
        self.assertIn("registration was not changed", message)

    async def test_nickname_failure_is_not_retried_when_role_succeeds(self) -> None:
        payload = {"attempts": 2, "discord_user_id": 20}
        with (
            patch.object(
                registration,
                "_save_side_effect_intent",
                new=AsyncMock(),
            ) as save_intent,
            patch.object(
                registration,
                "_delete_side_effect_intent",
                new=AsyncMock(),
            ) as delete_intent,
        ):
            await registration._record_side_effect_result(
                10,
                "20:player-id",
                payload,
                role_added=True,
            )

        delete_intent.assert_awaited_once_with(10, "20:player-id")
        save_intent.assert_not_awaited()

    async def test_failed_member_role_remains_durable_for_restart(self) -> None:
        payload = {
            "attempts": 2,
            "discord_user_id": 20,
            "nickname_synced": False,
        }
        with (
            patch.object(
                registration,
                "_save_side_effect_intent",
                new=AsyncMock(),
            ) as save_intent,
            patch.object(
                registration,
                "_delete_side_effect_intent",
                new=AsyncMock(),
            ) as delete_intent,
        ):
            await registration._record_side_effect_result(
                10,
                "20:player-id",
                payload,
                role_added=False,
            )

        delete_intent.assert_not_awaited()
        saved_payload = save_intent.await_args.args[2]
        self.assertEqual(3, saved_payload["attempts"])
        self.assertNotIn("nickname_synced", saved_payload)
        self.assertFalse(saved_payload["role_assigned"])

    async def test_restart_reconciler_repairs_by_local_canonical_player(self) -> None:
        role = SimpleNamespace(id=30, name="Member")
        member = SimpleNamespace(id=20, roles=[])
        guild = SimpleNamespace(
            id=10,
            get_role=lambda _role_id: role,
            get_member=lambda _member_id: member,
            fetch_member=AsyncMock(),
            roles=[role],
        )
        bot = SimpleNamespace(get_guild=lambda _guild_id: guild)
        record = runtime_state.RuntimeRecord(
            kind="registration_side_effect",
            guild_id=10,
            external_id="20:player-id",
            payload={
                "discord_user_id": 20,
                "albion_player_id": "player-id",
                "nickname": "Old Search Name",
            },
            status="pending",
            updated_at="now",
        )
        local_player = SimpleNamespace(
            is_active=True,
            nickname="Canonical Player",
            albion_player_id="player-id",
        )

        with (
            patch.object(
                registration.runtime_state,
                "list_records",
                return_value=[record],
            ) as list_records,
            patch.object(
                registration.local_repository,
                "get_active_ledger_id",
                return_value=77,
            ) as get_ledger,
            patch.object(
                registration.local_repository,
                "get_player",
                return_value=local_player,
            ) as get_player,
            patch.object(
                registration.guild_settings,
                "get_configuration",
                return_value=SimpleNamespace(
                    target_guild_name="Realm",
                    member_role_id=role.id,
                    member_role_name="Member",
                ),
            ),
            patch.object(registration, "self_assignment_error", return_value=None),
            patch.object(
                registration,
                "add_member_role",
                new=AsyncMock(return_value=True),
            ) as add_role,
            patch.object(
                registration,
                "sync_discord_nickname",
                new=AsyncMock(),
            ) as sync_nickname,
            patch.object(
                registration,
                "_delete_side_effect_intent",
                new=AsyncMock(),
            ) as delete_intent,
        ):
            await registration.reconcile_registration_side_effects(bot)

        list_records.assert_called_once_with(
            "registration_side_effect",
            statuses=("pending", "applying"),
        )
        get_ledger.assert_called_once_with(10, create_if_missing=False)
        get_player.assert_called_once_with(77, 20)
        add_role.assert_awaited_once_with(member, role)
        sync_nickname.assert_not_awaited()
        delete_intent.assert_awaited_once_with(10, "20:player-id")


class RegistrationEffectIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_matching_nickname_is_not_edited_again(self) -> None:
        member = SimpleNamespace(nick="Player", edit=AsyncMock())

        self.assertTrue(await registration.sync_discord_nickname(member, "Player"))

        member.edit.assert_not_awaited()

    async def test_existing_role_is_not_added_again(self) -> None:
        role = SimpleNamespace(id=30, name="Member")
        member = SimpleNamespace(
            roles=[SimpleNamespace(id=30)],
            add_roles=AsyncMock(),
        )

        self.assertTrue(await registration.add_member_role(member, role))

        member.add_roles.assert_not_awaited()


class StableAlbionVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_retry_keeps_using_selected_player_id(self) -> None:
        run_albion = AsyncMock(
            side_effect=[
                albion_api.AlbionTransientError("retry"),
                {"Id": "stable-id", "Name": "Player", "GuildName": "Realm"},
            ]
        )
        with (
            patch.object(
                registration.external_io,
                "run_albion",
                new=run_albion,
            ),
            patch.object(registration.asyncio, "sleep", new=AsyncMock()),
        ):
            profile = await registration._get_player_profile_with_retries("stable-id")

        self.assertEqual("stable-id", profile["Id"])
        self.assertEqual(
            ["stable-id", "stable-id"],
            [call.args[1] for call in run_albion.await_args_list],
        )

    async def test_membership_propagation_retries_the_same_stable_id(self) -> None:
        run_albion = AsyncMock(
            side_effect=[
                {"Id": "stable-id", "Name": "Player", "GuildName": "Old Guild"},
                {"Id": "stable-id", "Name": "Player", "GuildName": "Realm"},
            ]
        )
        with (
            patch.object(
                registration.external_io,
                "run_albion",
                new=run_albion,
            ),
            patch.object(registration.asyncio, "sleep", new=AsyncMock()),
        ):
            profile = await registration._get_player_profile_with_retries(
                "stable-id",
                "Realm",
            )

        self.assertEqual("Realm", profile["GuildName"])
        self.assertEqual(
            ["stable-id", "stable-id"],
            [call.args[1] for call in run_albion.await_args_list],
        )


if __name__ == "__main__":
    unittest.main()
