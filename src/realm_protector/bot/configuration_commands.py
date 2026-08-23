from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from src.realm_protector.bot.configuration_remove import handle_bot_remove_slash
from src.realm_protector.bot.configuration_setup import (
    BotSetupStepView,
    _build_bot_setup_step_embed,
)
from src.realm_protector.bot.configuration_update import (
    UpdateConfigView,
    _build_update_config_embed,
)
from src.realm_protector.bot.google_sheet_link import (
    GoogleSheetLinkStepView,
    _build_google_sheet_link_step_embed,
)
from src.realm_protector.infrastructure import guild_settings
from src.realm_protector.services.authorization import is_admin

if TYPE_CHECKING:
    from src.realm_protector.bot.client import RealmProtectorBot


def create_configuration_commands(
    bot: "RealmProtectorBot",
) -> list[app_commands.Command]:
    @app_commands.command(name="bot-setup", description="Open setup modal for this server")
    @app_commands.guild_only()
    async def bot_setup(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return
        if not await is_admin(interaction.user):
            await interaction.response.send_message(
                "You don't have permission to use this command.",
                ephemeral=True,
            )
            return

        setup_view = BotSetupStepView(guild, interaction.user.id)
        await interaction.response.send_message(
            embed=_build_bot_setup_step_embed(setup_view),
            view=setup_view,
        )
        setup_view.host_message = await interaction.original_response()

    @app_commands.command(
        name="bot-link-google-sheet",
        description="Link Google credentials JSON to this server",
    )
    @app_commands.guild_only()
    async def link_google_sheet(interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not await is_admin(interaction.user):
            await interaction.response.send_message(
                "You don't have permission to use this command.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None or not guild_settings.get_target_guild(guild.id):
            await interaction.response.send_message(
                "This server is not configured yet. Run **/bot-setup** first.",
                ephemeral=True,
            )
            return

        setup_view = GoogleSheetLinkStepView(guild, interaction.user.id)
        await interaction.response.send_message(
            embed=_build_google_sheet_link_step_embed(setup_view),
            view=setup_view,
        )
        setup_view.host_message = await interaction.original_response()

    @app_commands.command(name="update-config", description="Update bot configuration values")
    @app_commands.guild_only()
    async def update_configuration(interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not await is_admin(interaction.user):
            await interaction.response.send_message(
                "You don't have permission to use this command.",
                ephemeral=True,
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        update_view = UpdateConfigView(interaction.guild, interaction.user.id)
        await interaction.response.send_message(
            embed=_build_update_config_embed(update_view),
            view=update_view,
        )
        update_view.host_message = await interaction.original_response()

    @app_commands.command(
        name="bot-remove",
        description="Remove this server configuration from the bot",
    )
    @app_commands.guild_only()
    async def remove_configuration(interaction: discord.Interaction) -> None:
        await handle_bot_remove_slash(interaction)

    @app_commands.command(
        name="clear",
        description="Clear the last 100 messages in this channel (admin only)",
    )
    @app_commands.guild_only()
    async def clear_messages(interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not await is_admin(interaction.user):
            await interaction.response.send_message(
                "You don't have permission to use this command.",
                ephemeral=True,
            )
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "This command can only be used in a text channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await interaction.channel.purge(limit=100)
        except discord.Forbidden:
            await interaction.followup.send(
                "Missing permission to delete messages in this channel.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.followup.send(
                "Failed to clear messages (Discord API error).",
                ephemeral=True,
            )
            return
        await interaction.followup.send("Cleared the last 100 messages.", ephemeral=True)

    return [
        bot_setup,
        link_google_sheet,
        update_configuration,
        remove_configuration,
        clear_messages,
    ]
