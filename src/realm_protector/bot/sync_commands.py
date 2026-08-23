"""Administrator-only commands for observing and repairing Sheet projection."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from src.realm_protector.services import google_sync, sync_operations
from src.realm_protector.services.authorization import is_admin

if TYPE_CHECKING:
    from src.realm_protector.bot.client import RealmProtectorBot


LOGGER = logging.getLogger(__name__)


async def _require_admin_guild(interaction: discord.Interaction) -> int | None:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return None
    if not await is_admin(member):
        await interaction.response.send_message(
            "You don't have permission to use this command.",
            ephemeral=True,
        )
        return None
    return guild.id


def _format_sync_health(health: sync_operations.SyncHealth) -> str:
    outbox = health.outbox
    if outbox is None:
        queue_lines = ["Local ledger: missing", "Projection queue: unavailable"]
    else:
        queue_lines = [
            f"Local ledger: `{health.ledger_id}`",
            (
                "Projection queue: "
                f"{outbox.pending_events} pending, "
                f"{outbox.processing_events} processing, "
                f"{outbox.dead_letter_events} quarantined"
            ),
            f"Last projected event: {outbox.last_completed_at or 'never'}",
        ]
        if outbox.latest_error:
            queue_lines.append(f"Latest projection error: {outbox.latest_error[:500]}")

    link_state = health.google_link_status
    if link_state == "active" and not health.google_credentials_readable:
        link_state = "active metadata, credentials unreadable"
    lines = [
        "## Google synchronization status",
        f"Albion guild: **{health.target_guild_name or 'not configured'}**",
        f"Google link: **{link_state}**",
        f"Initial Sheet cutover ready: **{'yes' if health.cutover_ready else 'no'}**",
        *queue_lines,
        (
            "Current Siphon cache: "
            f"{health.current_siphon_players}/{health.active_players} active players"
        ),
        f"Latest Siphon refresh: {health.latest_siphon_sync_at or 'never'}",
    ]
    if health.quarantine_reason:
        lines.append(f"Link quarantine reason: {health.quarantine_reason}")
    return "\n".join(lines)


def create_sync_commands(bot: "RealmProtectorBot") -> list[app_commands.Command]:
    @app_commands.command(
        name="sync-status",
        description="Show local-to-Google projection health (admin only)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def sync_status(interaction: discord.Interaction) -> None:
        guild_id = await _require_admin_guild(interaction)
        if guild_id is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            health = await asyncio.to_thread(sync_operations.get_sync_health, guild_id)
        except Exception:
            LOGGER.exception("Could not read Google sync status for guild %s", guild_id)
            await interaction.followup.send(
                "Synchronization status could not be read. Check the bot logs.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            _format_sync_health(health),
            ephemeral=True,
        )

    @app_commands.command(
        name="sync-retry",
        description="Retry quarantined Google projection events (admin only)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def sync_retry(interaction: discord.Interaction) -> None:
        guild_id = await _require_admin_guild(interaction)
        if guild_id is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            recovery = await sync_operations.retry_dead_letters_and_flush(guild_id)
        except Exception:
            LOGGER.exception("Google sync recovery failed for guild %s", guild_id)
            await interaction.followup.send(
                "Synchronization recovery failed unexpectedly. Check the bot logs.",
                ephemeral=True,
            )
            return
        outcome = "completed" if recovery.projection.success else "incomplete"
        await interaction.followup.send(
            (
                f"Synchronization recovery **{outcome}**. "
                f"Retried {recovery.retried_dead_letters} quarantined event(s); "
                f"projected {recovery.projection.processed_events} event(s).\n"
                f"{recovery.projection.message}"
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="sync-rebuild",
        description="Rebuild the Google projection from local SQLite (admin only)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def sync_rebuild(interaction: discord.Interaction) -> None:
        guild_id = await _require_admin_guild(interaction)
        if guild_id is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await google_sync.rebuild_projection(guild_id)
        except Exception:
            LOGGER.exception("Google projection rebuild failed for guild %s", guild_id)
            await interaction.followup.send(
                "Projection rebuild failed unexpectedly. Check the bot logs.",
                ephemeral=True,
            )
            return
        outcome = "completed" if result.success else "failed"
        await interaction.followup.send(
            f"Projection rebuild **{outcome}**. {result.message}",
            ephemeral=True,
        )

    return [sync_status, sync_retry, sync_rebuild]


__all__ = ["create_sync_commands"]
