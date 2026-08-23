from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from src.realm_protector.bot.character_picker import (
    CharacterSelectionView,
    build_character_selection_embed,
)
from src.realm_protector.bot.common import (
    InteractionMessageAdapter,
    send_followup_lines,
)
from src.realm_protector.infrastructure import albion_api, guild_settings
from src.realm_protector.services import (
    albion_characters,
    battle_participants,
    google_sync,
    guild_lifecycle,
    registration,
    request_limits,
)
from src.realm_protector.services.albion_characters import AlbionCharacterOption
from src.realm_protector.services.authorization import is_admin

if TYPE_CHECKING:
    from src.realm_protector.bot.client import RealmProtectorBot


_BATTLE_LOOKUP_COOLDOWN = request_limits.Cooldown(15)
_REGISTRATION_COOLDOWN = request_limits.Cooldown(30)
_FORCE_REGISTRATION_COOLDOWN = request_limits.Cooldown(10)
LOGGER = logging.getLogger(__name__)


def _retry_seconds(retry_after: float) -> int:
    return max(1, int(retry_after) + 1)


class _RegistrationCharacterSelectionView(CharacterSelectionView):
    def __init__(
        self,
        user_id: int,
        target_guild_name: str,
        expected_generation: int,
        character_options: list[AlbionCharacterOption],
    ) -> None:
        super().__init__(user_id, character_options)
        self._target_guild_name = target_guild_name
        self._expected_generation = expected_generation

    async def on_character_selected(
        self,
        interaction: discord.Interaction,
        selected_character: AlbionCharacterOption,
    ) -> None:
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            await interaction.response.send_message(
                "This selection can only be used inside a server.",
                ephemeral=True,
            )
            return

        self.stop()
        await interaction.response.edit_message(
            content=f"Registering **{selected_character.nickname}**...",
            embed=None,
            view=None,
        )
        context = InteractionMessageAdapter(interaction)
        await registration.register_user(
            context,
            selected_character.nickname,
            selected_character.player_id,
            self._target_guild_name,
            self._expected_generation,
            selected_profile=selected_character.player_profile,
        )


def create_registration_commands(
    bot: "RealmProtectorBot",
) -> list[app_commands.Command]:
    @app_commands.command(
        name="get-participants",
        description="List guild members participating in one or more battle IDs",
    )
    @app_commands.guild_only()
    async def get_participants(
        interaction: discord.Interaction,
        battle_ids: str,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        configuration = guild_settings.get_configuration(interaction.guild.id)
        if configuration is None:
            await interaction.response.send_message(
                "This server is not configured yet. Ask an admin to run **/bot-setup** first.",
                ephemeral=True,
            )
            return
        target_guild_name = configuration.target_guild_name
        retry_after = _BATTLE_LOOKUP_COOLDOWN.claim((interaction.guild.id, interaction.user.id))
        if retry_after:
            await interaction.response.send_message(
                f"Please wait {_retry_seconds(retry_after)} seconds before another battle lookup.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        try:
            result = await battle_participants.collect_battle_participants(
                battle_ids,
                target_guild_name,
                albion_api.get_battle_participants,
            )
        except ValueError as error:
            await interaction.followup.send(str(error))
            return
        if not result.requested_ids:
            await interaction.followup.send("Please provide at least one battle ID.")
            return
        if result.failed_ids:
            await interaction.followup.send(
                f"Could not fetch battle(s): **{', '.join(result.failed_ids)}**. "
                "Check the IDs and try again."
            )
        if not result.participant_names:
            await interaction.followup.send(
                f"No **{target_guild_name}** members found in the provided battle(s)."
            )
            return

        names_list = ",".join(result.participant_names)
        battle_label = ", ".join(result.requested_ids)
        await send_followup_lines(
            interaction,
            [
                f"**{len(result.participant_names)} {target_guild_name} member(s) "
                f"in battle(s) {battle_label}:**",
                names_list,
            ],
        )

    @app_commands.command(name="register", description="Register your Albion character")
    @app_commands.guild_only()
    async def register(
        interaction: discord.Interaction,
        character_name: str,
    ) -> None:
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        normalized_character_name = character_name.strip()
        if not normalized_character_name or len(normalized_character_name) > 40:
            await interaction.response.send_message(
                "`character_name` must contain 1-40 characters.",
                ephemeral=True,
            )
            return
        configuration = guild_settings.get_configuration(interaction.guild.id)
        if configuration is None:
            await interaction.response.send_message(
                "This server is not configured yet. Ask an admin to run **/bot-setup** first.",
                ephemeral=True,
            )
            return
        target_guild_name = configuration.target_guild_name
        if not await asyncio.to_thread(
            google_sync.is_cutover_ready,
            interaction.guild.id,
        ):
            await interaction.response.send_message(
                "The one-time Google Sheet migration is still pending. Registration will be available after it completes.",
                ephemeral=True,
            )
            return
        retry_after = _REGISTRATION_COOLDOWN.claim((interaction.guild.id, interaction.user.id))
        if retry_after:
            await interaction.response.send_message(
                f"Please wait {_retry_seconds(retry_after)} seconds before trying registration again.",
                ephemeral=True,
            )
            return

        expected_generation = guild_lifecycle.generation(interaction.guild.id)
        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            character_options = await albion_characters.search_character_options(
                normalized_character_name,
                raise_on_error=True,
            )
        except albion_api.AlbionAPIError:
            await interaction.edit_original_response(
                content=(
                    "Albion is temporarily unavailable, so I could not search for "
                    "characters. Please try again later."
                ),
                embed=None,
                view=None,
            )
            return
        if not character_options:
            await interaction.edit_original_response(
                content="No characters found. Please check the nickname and try again.",
                embed=None,
                view=None,
            )
            return

        current_configuration = guild_settings.get_configuration(interaction.guild.id)
        if (
            not guild_lifecycle.is_current(interaction.guild.id, expected_generation)
            or current_configuration is None
            or current_configuration.target_guild_name.strip().casefold()
            != target_guild_name.strip().casefold()
        ):
            await interaction.edit_original_response(
                content=(
                    "The server configuration changed while characters were being "
                    "checked. Run **/register** again."
                ),
                embed=None,
                view=None,
            )
            return

        view = _RegistrationCharacterSelectionView(
            interaction.user.id,
            target_guild_name,
            expected_generation,
            character_options,
        )
        await interaction.edit_original_response(
            content=None,
            embed=build_character_selection_embed(character_options),
            view=view,
        )

    @app_commands.command(
        name="force-register",
        description="Reverify and reactivate an existing registered member",
    )
    @app_commands.guild_only()
    async def force_register(
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        guild = interaction.guild
        actor = interaction.user
        if guild is None or not isinstance(actor, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return
        if not await is_admin(actor):
            await interaction.response.send_message(
                "You don't have permission to use this command.",
                ephemeral=True,
            )
            return

        retry_after = _FORCE_REGISTRATION_COOLDOWN.claim((guild.id, actor.id))
        if retry_after:
            await interaction.response.send_message(
                f"Please wait {_retry_seconds(retry_after)} seconds before another forced verification.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            message = await registration.force_register_member(guild, member)
        except Exception:
            LOGGER.exception(
                "Force registration failed for member %s in guild %s",
                member.id,
                guild.id,
            )
            await interaction.followup.send(
                "Failed to update the registration. Try again.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(message, ephemeral=True)

    return [get_participants, register, force_register]
