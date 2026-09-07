import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.realm_protector.bot import registration_commands
from src.realm_protector.bot.common import InteractionMessageAdapter
from src.realm_protector.services.albion_characters import AlbionCharacterOption


def _character_options() -> list[AlbionCharacterOption]:
    return [
        AlbionCharacterOption(
            {
                "Id": f"player-{position}",
                "Name": f"Player {position}",
                "GuildName": "Realm",
                "KillFame": position * 1_000,
                "DeathFame": position * 100,
                "FameRatio": position,
            },
            position * 10_000,
            {
                "Id": f"player-{position}",
                "Name": f"Player {position}",
                "GuildName": "Realm",
            },
        )
        for position in range(1, 4)
    ]


class RegistrationCharacterPickerTests(unittest.IsolatedAsyncioTestCase):
    async def test_force_register_is_admin_only_and_rechecks_selected_member(self) -> None:
        class FakeMember:
            def __init__(self, member_id: int) -> None:
                self.id = member_id
                self.mention = f"<@{member_id}>"

        actor = FakeMember(404)
        target = FakeMember(405)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=707, fetch_member=AsyncMock(return_value=target)),
            user=actor,
            response=SimpleNamespace(
                send_message=AsyncMock(),
                defer=AsyncMock(),
                edit_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        force_register = next(
            command
            for command in registration_commands.create_registration_commands(SimpleNamespace())
            if command.name == "force-register"
        )
        with (
            patch.object(registration_commands.discord, "Member", FakeMember),
            patch.object(
                registration_commands.guild_settings,
                "get_configuration",
                return_value=SimpleNamespace(target_guild_name="Realm"),
            ),
            patch.object(registration_commands.google_sync, "is_cutover_ready", return_value=True),
            patch.object(
                registration_commands.albion_characters,
                "search_character_options",
                new=AsyncMock(return_value=_character_options()),
            ) as search,
            patch.object(
                registration_commands,
                "is_admin",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                registration_commands._FORCE_REGISTRATION_COOLDOWN,
                "claim",
                return_value=0,
            ),
            patch.object(
                registration_commands.registration,
                "force_register_member",
                new=AsyncMock(return_value="Registration repaired."),
            ) as repair,
        ):
            await force_register.callback(interaction, target, " Player ")
            repair.assert_not_awaited()
            search.assert_awaited_once_with("Player", raise_on_error=True)
            panel = interaction.edit_original_response.await_args.kwargs
            view = panel["view"]
            self.assertEqual("Select your character", panel["embed"].title)
            self.assertEqual(["1", "2", "3", "Cancel"], [button.label for button in view.children])
            await view.select(interaction, 1)
            await view.select(interaction, 0)  # Repeated clicks cannot register a second character.

        interaction.response.defer.assert_awaited_once_with(
            thinking=True,
            ephemeral=True,
        )
        repair.assert_awaited_once_with(
            interaction.guild,
            target,
            "Player 2",
            albion_player_id="player-2",
            expected_generation=registration_commands.guild_lifecycle.generation(707),
            expected_target_guild_name="Realm",
        )
        interaction.guild.fetch_member.assert_awaited_once_with(405)
        self.assertEqual("Registration repaired.", interaction.followup.send.await_args.args[0])
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])

    async def test_force_register_rejects_non_admin(self) -> None:
        class FakeMember:
            def __init__(self, member_id: int) -> None:
                self.id = member_id

        actor = FakeMember(404)
        target = FakeMember(405)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=707),
            user=actor,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        )
        force_register = next(
            command
            for command in registration_commands.create_registration_commands(SimpleNamespace())
            if command.name == "force-register"
        )
        with (
            patch.object(registration_commands.discord, "Member", FakeMember),
            patch.object(
                registration_commands,
                "is_admin",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                registration_commands.registration,
                "force_register_member",
                new=AsyncMock(),
            ) as repair,
        ):
            await force_register.callback(interaction, target, "Player")

        interaction.response.send_message.assert_awaited_once_with(
            "You don't have permission to use this command.",
            ephemeral=True,
        )
        repair.assert_not_awaited()

    async def test_register_is_gated_while_linked_sheet_cutover_is_pending(self) -> None:
        class FakeMember:
            id = 404

        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=707),
            user=FakeMember(),
            response=SimpleNamespace(
                send_message=AsyncMock(),
                defer=AsyncMock(),
            ),
        )
        register_command = next(
            command
            for command in registration_commands.create_registration_commands(SimpleNamespace())
            if command.name == "register"
        )
        with (
            patch.object(registration_commands.discord, "Member", FakeMember),
            patch.object(
                registration_commands.guild_settings,
                "get_configuration",
                return_value=SimpleNamespace(target_guild_name="Realm"),
            ),
            patch.object(
                registration_commands.google_sync,
                "is_cutover_ready",
                return_value=False,
            ),
            patch.object(
                registration_commands.albion_characters,
                "search_character_options",
                new=AsyncMock(),
            ) as search,
        ):
            await register_command.callback(interaction, "Player")

        interaction.response.send_message.assert_awaited_once()
        self.assertIn(
            "migration is still pending",
            interaction.response.send_message.await_args.args[0],
        )
        search.assert_not_awaited()

    async def test_register_command_displays_private_three_character_picker(self) -> None:
        class FakeMember:
            id = 404

        options = _character_options()
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=707),
            user=FakeMember(),
            response=SimpleNamespace(
                send_message=AsyncMock(),
                defer=AsyncMock(),
            ),
            edit_original_response=AsyncMock(),
        )
        register_command = next(
            command
            for command in registration_commands.create_registration_commands(SimpleNamespace())
            if command.name == "register"
        )

        with (
            patch.object(registration_commands.discord, "Member", FakeMember),
            patch.object(
                registration_commands.guild_settings,
                "get_configuration",
                return_value=SimpleNamespace(target_guild_name="Realm"),
            ),
            patch.object(
                registration_commands._REGISTRATION_COOLDOWN,
                "claim",
                return_value=0,
            ),
            patch.object(
                registration_commands.google_sync,
                "is_cutover_ready",
                return_value=True,
            ),
            patch.object(
                registration_commands.guild_lifecycle,
                "generation",
                return_value=12,
            ),
            patch.object(
                registration_commands.guild_lifecycle,
                "is_current",
                return_value=True,
            ),
            patch.object(
                registration_commands.albion_characters,
                "search_character_options",
                new=AsyncMock(return_value=options),
            ) as search,
        ):
            await register_command.callback(interaction, " Player ")

        interaction.response.defer.assert_awaited_once_with(
            thinking=True,
            ephemeral=True,
        )
        interaction.edit_original_response.assert_awaited_once()
        picker_kwargs = interaction.edit_original_response.await_args.kwargs
        self.assertEqual("Select your character", picker_kwargs["embed"].title)
        self.assertEqual(
            ["1", "2", "3", "Cancel"],
            [item.label for item in picker_kwargs["view"].children],
        )
        search.assert_awaited_once_with(
            "Player",
            raise_on_error=True,
        )

    async def test_register_reports_albion_outage_instead_of_not_found(self) -> None:
        class FakeMember:
            id = 404

        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=707),
            user=FakeMember(),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        register_command = next(
            command
            for command in registration_commands.create_registration_commands(SimpleNamespace())
            if command.name == "register"
        )

        with (
            patch.object(registration_commands.discord, "Member", FakeMember),
            patch.object(
                registration_commands.guild_settings,
                "get_configuration",
                return_value=SimpleNamespace(target_guild_name="Realm"),
            ),
            patch.object(
                registration_commands.google_sync,
                "is_cutover_ready",
                return_value=True,
            ),
            patch.object(
                registration_commands._REGISTRATION_COOLDOWN,
                "claim",
                return_value=0,
            ),
            patch.object(
                registration_commands.albion_characters,
                "search_character_options",
                new=AsyncMock(
                    side_effect=registration_commands.albion_api.AlbionTransientError("offline")
                ),
            ),
        ):
            await register_command.callback(interaction, "Player")

        content = interaction.edit_original_response.await_args.kwargs["content"]
        self.assertIn("temporarily unavailable", content)

    async def test_second_button_registers_the_second_selected_character(self) -> None:
        class FakeMember:
            id = 404

        options = _character_options()
        view = registration_commands._RegistrationCharacterSelectionView(
            user_id=404,
            target_guild_name="Realm",
            expected_generation=12,
            character_options=options,
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=707),
            user=FakeMember(),
            response=SimpleNamespace(
                edit_message=AsyncMock(),
                send_message=AsyncMock(),
                is_done=lambda: True,
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        second_button = next(item for item in view.children if item.label == "2")

        with (
            patch.object(registration_commands.discord, "Member", FakeMember),
            patch.object(
                registration_commands.registration,
                "register_user",
                new=AsyncMock(),
            ) as register_user,
        ):
            await second_button.callback(interaction)

        interaction.response.edit_message.assert_awaited_once_with(
            content="Registering **Player 2**...",
            embed=None,
            view=None,
        )
        register_user.assert_awaited_once()
        context, nickname, player_id, target_guild, generation = register_user.await_args.args
        self.assertEqual("Player 2", nickname)
        self.assertEqual("player-2", player_id)
        self.assertEqual("Realm", target_guild)
        self.assertEqual(12, generation)
        self.assertEqual(
            options[1].player_profile,
            register_user.await_args.kwargs["selected_profile"],
        )
        self.assertIs(interaction.user, context.author)
        self.assertFalse(context._ephemeral)

    async def test_character_buttons_reject_a_different_user(self) -> None:
        options = _character_options()
        view = registration_commands._RegistrationCharacterSelectionView(
            user_id=404,
            target_guild_name="Realm",
            expected_generation=12,
            character_options=options,
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=999),
            response=SimpleNamespace(
                send_message=AsyncMock(),
                edit_message=AsyncMock(),
            ),
        )
        first_button = next(item for item in view.children if item.label == "1")

        with patch.object(
            registration_commands.registration,
            "register_user",
            new=AsyncMock(),
        ) as register_user:
            await first_button.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            "Only the user who searched for these characters can make this selection.",
            ephemeral=True,
        )
        register_user.assert_not_awaited()

    async def test_registration_result_adapter_sends_a_public_followup(self) -> None:
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=707),
            user=SimpleNamespace(id=404),
            response=SimpleNamespace(is_done=lambda: True),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        context = InteractionMessageAdapter(interaction)

        await context.send("Registered.")

        interaction.followup.send.assert_awaited_once_with("Registered.")


class ForcePickerSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.actor = SimpleNamespace(id=404)
        self.guild = SimpleNamespace(id=707, fetch_member=AsyncMock())
        self.interaction = SimpleNamespace(
            user=self.actor,
            guild=self.guild,
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        self.view = registration_commands._ForceRegistrationCharacterSelectionView(
            404, 707, 405, "Realm", 0, _character_options()
        )

    async def test_other_user_cannot_submit_selection(self):
        self.actor.id = 999
        await self.view.select(self.interaction, 0)
        self.guild.fetch_member.assert_not_awaited()
        self.assertTrue(self.interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_cancel_prevents_even_queued_selection_from_registering(self):
        await self.view.cancel(self.interaction)
        await self.view.select(self.interaction, 0)
        self.guild.fetch_member.assert_not_awaited()
        self.assertEqual(
            "Cancelled.", self.interaction.response.edit_message.await_args.kwargs["content"]
        )

    async def test_admin_permission_is_rechecked_on_selection(self):
        with (
            patch.object(registration_commands.discord, "Member", SimpleNamespace),
            patch.object(registration_commands, "is_admin", new=AsyncMock(return_value=False)),
        ):
            await self.view.select(self.interaction, 0)
        self.guild.fetch_member.assert_not_awaited()
        self.assertIn("administrator", self.interaction.response.send_message.await_args.args[0])

    async def test_member_who_left_is_not_registered(self):
        error = registration_commands.discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "gone"
        )
        self.guild.fetch_member.side_effect = error
        with (
            patch.object(registration_commands.discord, "Member", SimpleNamespace),
            patch.object(registration_commands, "is_admin", new=AsyncMock(return_value=True)),
            patch.object(
                registration_commands.registration, "force_register_member", new=AsyncMock()
            ) as register,
        ):
            await self.view.select(self.interaction, 0)
        register.assert_not_awaited()
        self.assertIn(
            "no longer in this Discord server", self.interaction.followup.send.await_args.args[0]
        )

    def test_missing_results_disable_numbered_buttons(self):
        view = registration_commands._ForceRegistrationCharacterSelectionView(
            404, 707, 405, "Realm", 0, _character_options()[:1]
        )
        self.assertEqual([False, True, True, False], [button.disabled for button in view.children])


if __name__ == "__main__":
    unittest.main()
