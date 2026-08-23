"""Crash-safe removal of one Discord guild's Realm Protector setup."""

from __future__ import annotations

import asyncio
import logging
from typing import Mapping, Optional

import discord

from src.realm_protector.bot import objectives, reaction_roles, tickets
from src.realm_protector.domain.models import GuildConfiguration
from src.realm_protector.infrastructure import credential_store, guild_settings
from src.realm_protector.services import (
    authorization,
    configuration_lifecycle,
    guild_lifecycle,
)

LOGGER = logging.getLogger(__name__)


def _optional_positive_id(value: object) -> Optional[int]:
    try:
        parsed = int(str(value or 0))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


async def _disable_configuration_panel(
    guild: discord.Guild,
    channel_id: Optional[int],
    message_id: Optional[int],
) -> bool:
    if channel_id is None or message_id is None:
        return True
    try:
        channel = guild.get_channel(channel_id) or await guild.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return True
        message = await channel.fetch_message(message_id)
        await message.edit(
            content="Realm Protector configuration has been removed from this server.",
            embed=None,
            view=None,
        )
        return True
    except discord.NotFound:
        return True
    except (discord.Forbidden, discord.HTTPException):
        LOGGER.warning("Could not disable configuration panel for guild %s", guild.id)
        return False


async def _restore_guild_name(guild: discord.Guild, base_name: object) -> bool:
    clean_name = str(base_name or "").strip()
    if not clean_name or guild.name == clean_name:
        return True
    try:
        await guild.edit(
            name=clean_name,
            reason="Realm Protector UTC timer removed",
        )
        return True
    except (discord.Forbidden, discord.HTTPException):
        LOGGER.warning("Could not restore the pre-timer name for guild %s", guild.id)
        return False


async def _remove_credentials(guild_id: int) -> bool:
    try:
        await asyncio.to_thread(
            credential_store.remove_google_sheet_credentials,
            guild_id,
        )
        return True
    except Exception:
        LOGGER.exception("Google credential cleanup failed for guild %s", guild_id)
        return False


def _payload_from_configuration(configuration: GuildConfiguration) -> dict:
    return {
        "target_guild_name": configuration.target_guild_name,
        "configuration_channel_id": configuration.bot_configuration_channel_id,
        "configuration_message_id": configuration.bot_configuration_message_id,
        "utc_timer_guild_name": configuration.utc_timer_guild_name,
    }


async def _cleanup_removal(
    guild: discord.Guild,
    payload: Mapping[str, object],
) -> tuple[bool, list[str]]:
    """Apply idempotent Discord/file cleanup for a retired local setup."""

    cleanup_results = await asyncio.gather(
        tickets.deactivate_guild_ticket_configuration(guild),
        reaction_roles.deactivate_guild_reaction_role_configuration(guild),
        objectives.deactivate_guild_objective_configuration(guild),
        _disable_configuration_panel(
            guild,
            _optional_positive_id(payload.get("configuration_channel_id")),
            _optional_positive_id(payload.get("configuration_message_id")),
        ),
        _restore_guild_name(guild, payload.get("utc_timer_guild_name")),
        _remove_credentials(guild.id),
        return_exceptions=True,
    )

    warnings: list[str] = []
    # Feature cleanup is idempotent: False can mean the feature never existed
    # or was already removed by an earlier attempt. Only exceptions are retryable.
    for result in cleanup_results[:3]:
        if isinstance(result, Exception):
            LOGGER.error(
                "Configuration feature cleanup failed for guild %s: %s",
                guild.id,
                result,
            )
            warnings.append("Some Discord feature panels could not be cleaned up.")

    if any(result is False or isinstance(result, Exception) for result in cleanup_results[3:5]):
        warnings.append("Some Discord panel or UTC timer cleanup will be retried.")
    if cleanup_results[5] is False or isinstance(cleanup_results[5], Exception):
        warnings.append("Google credential cleanup will be retried.")

    success = not warnings
    if success:
        configuration_lifecycle.complete_guild_configuration_removal(guild.id)
    return success, list(dict.fromkeys(warnings))


async def _remove_guild_configuration(
    guild: discord.Guild,
) -> tuple[Optional[str], list[str]]:
    """Retire local state and then execute its durable external cleanup intent."""

    configuration = configuration_lifecycle.begin_guild_configuration_removal(guild.id)
    if configuration is None:
        return None, []
    guild_lifecycle.advance(guild.id)
    _, warnings = await _cleanup_removal(
        guild,
        _payload_from_configuration(configuration),
    )
    return configuration.target_guild_name, warnings


async def reconcile_configuration_removals(bot: discord.Client) -> None:
    """Retry every persisted teardown after restart and during periodic repair."""

    for record in configuration_lifecycle.list_pending_removals():
        guild = bot.get_guild(record.guild_id)
        if guild is None:
            # The bot no longer belongs to the server, so Discord resources are
            # inaccessible and inert from its perspective. Secret cleanup is
            # still mandatory and locally retryable.
            if await _remove_credentials(record.guild_id):
                configuration_lifecycle.complete_guild_configuration_removal(record.guild_id)
            continue

        async with guild_lifecycle.lock_for(record.guild_id):
            if guild_settings.get_configuration(record.guild_id) is not None:
                # Never let an old teardown damage a newly configured server.
                LOGGER.error(
                    "Pending teardown for guild %s conflicts with a live setup; "
                    "manual review is required.",
                    record.guild_id,
                )
                continue
            await _cleanup_removal(guild, record.payload)


class BotRemoveConfirmView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id

    async def _ensure_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the admin who started this action can use these controls.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="YES, remove", style=discord.ButtonStyle.danger)
    async def confirm_remove(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self._ensure_owner(interaction):
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return
        if not isinstance(
            interaction.user,
            discord.Member,
        ) or not await authorization.is_admin(interaction.user):
            await interaction.response.send_message(
                "Your Administrator permission was removed; configuration was kept.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        guild_id = interaction.guild.id
        async with guild_lifecycle.lock_for(guild_id):
            if not isinstance(
                interaction.user,
                discord.Member,
            ) or not await authorization.is_admin(interaction.user):
                await interaction.edit_original_response(
                    content=("Your Administrator permission was removed; configuration was kept."),
                    embed=None,
                    view=None,
                )
                return
            try:
                removed_guild_name, cleanup_warnings = await _remove_guild_configuration(
                    interaction.guild
                )
            except configuration_lifecycle.ConfigurationRemovalError:
                LOGGER.exception(
                    "Local configuration retirement failed for guild %s",
                    guild_id,
                )
                await interaction.edit_original_response(
                    content=(
                        "The saved configuration is invalid, so nothing was removed. "
                        "Review the bot log before retrying."
                    ),
                    embed=None,
                    view=None,
                )
                return

        if not removed_guild_name:
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="Bot Remove",
                    description="This server is not set up.",
                ),
                view=None,
            )
            return

        warning_text = ""
        if cleanup_warnings:
            warning_text = "\n\nLocal routing was disabled safely. " + " ".join(cleanup_warnings)

        await interaction.edit_original_response(
            embed=discord.Embed(
                title="Bot Remove",
                description=(
                    f"Setup for Discord server **{guild_id}** and Albion guild "
                    f"**{removed_guild_name}** was removed. Historical ledger "
                    f"records remain archived for audit.{warning_text}"
                ),
            ),
            view=None,
        )

    @discord.ui.button(label="NO, keep", style=discord.ButtonStyle.success)
    async def cancel_remove(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self._ensure_owner(interaction):
            return
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Bot Remove",
                description="Removal cancelled. Configuration was kept.",
            ),
            view=None,
        )


async def handle_bot_remove_slash(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return
    if not isinstance(
        interaction.user,
        discord.Member,
    ) or not await authorization.is_admin(interaction.user):
        await interaction.response.send_message(
            "You don't have permission to use this command.",
            ephemeral=True,
        )
        return

    configuration = guild_settings.get_configuration(interaction.guild.id)
    if configuration is None:
        await interaction.response.send_message(
            "This server is not set up.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        embed=discord.Embed(
            title="Bot Remove",
            description=(
                "## :warning: This disables all Realm Protector panels and "
                f"removes the live setup for **{configuration.target_guild_name}**. "
                "Historical ledger records will remain archived."
            ),
        ),
        view=BotRemoveConfirmView(interaction.user.id),
    )


__all__ = [
    "BotRemoveConfirmView",
    "handle_bot_remove_slash",
    "reconcile_configuration_removals",
]
