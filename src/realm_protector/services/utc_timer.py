from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord

from src.realm_protector.infrastructure import guild_settings
from src.realm_protector.services import authorization, guild_lifecycle

_TIMER_TASK: Optional[asyncio.Task] = None
# Guild-name edits are one of Discord's most aggressively rate-limited writes.
# Five-minute precision keeps the timer useful while reducing writes by 80% and
# staying within the common two-edits-per-ten-minutes bucket.
_TIMER_DISPLAY_INTERVAL_MINUTES = 5
_UTC_SUFFIX_PATTERN = re.compile(r"\s\[\d{1,2}:\d{2}\]$")


def start_utc_timer_scheduler(bot: discord.Client) -> None:
    global _TIMER_TASK
    if _TIMER_TASK is not None and not _TIMER_TASK.done():
        return
    _TIMER_TASK = asyncio.create_task(
        _utc_timer_loop(bot),
        name="realm-protector-utc-timer",
    )


async def stop_utc_timer_scheduler() -> None:
    """Stop the singleton scheduler and make repeated shutdown calls harmless."""

    global _TIMER_TASK
    task = _TIMER_TASK
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logging.exception("UTC timer scheduler failed while stopping")
    finally:
        if _TIMER_TASK is task:
            _TIMER_TASK = None


async def refresh_utc_timer_channels(bot: discord.Client) -> None:
    await _refresh_all_timer_guilds(bot)


async def handle_add_utc_timer_slash(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.", ephemeral=True
        )
        return

    if not isinstance(interaction.user, discord.Member) or not await authorization.is_admin(
        interaction.user
    ):
        await interaction.response.send_message(
            "You don't have permission to use this command.", ephemeral=True
        )
        return
    if not guild_settings.get_target_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This server is not configured yet. Run **/bot-setup** first.",
            ephemeral=True,
        )
        return

    client_user = interaction.client.user
    if client_user is None:
        await interaction.response.send_message(
            "Bot member information is unavailable. Please try again.",
            ephemeral=True,
        )
        return
    me = interaction.guild.get_member(client_user.id)
    if me is None:
        try:
            me = await interaction.guild.fetch_member(client_user.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            me = None

    if me is not None and not me.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "The bot needs the Manage Server permission to update the server name with UTC time.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    async with guild_lifecycle.lock_for(interaction.guild.id):
        if (
            not isinstance(interaction.user, discord.Member)
            or not await authorization.is_admin(interaction.user)
            or not guild_settings.get_target_guild(interaction.guild.id)
        ):
            await interaction.followup.send(
                "The server setup or your Administrator permission changed; the UTC timer was not enabled.",
                ephemeral=True,
            )
            return
        base_name = guild_settings.get_utc_timer_guild_name(
            interaction.guild.id
        ) or _extract_base_guild_name(interaction.guild.name)
        guild_settings.set_utc_timer_guild_name(interaction.guild.id, base_name)
        guild_settings.clear_utc_timer_channel(interaction.guild.id)
        name_changed = await _sync_guild_name(interaction.guild, base_name)

    if name_changed:
        await interaction.followup.send(
            f"UTC timer is now configured in the server name: {_format_guild_name(base_name)}",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        "UTC timer is already configured in the server name.",
        ephemeral=True,
    )


async def _utc_timer_loop(bot: discord.Client) -> None:
    await bot.wait_until_ready()

    while True:
        try:
            await _refresh_all_timer_guilds(bot)
        except Exception:
            logging.exception("UTC timer tick failed")
        await asyncio.sleep(_seconds_until_next_update())


async def _refresh_all_timer_guilds(bot: discord.Client) -> None:
    for guild_id, base_name in guild_settings.get_all_utc_timer_guild_names().items():
        guild = bot.get_guild(guild_id)
        if guild is None:
            try:
                guild = await bot.fetch_guild(guild_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue

        async with guild_lifecycle.lock_for(guild_id):
            current_base_name = guild_settings.get_utc_timer_guild_name(guild_id)
            if not guild_settings.get_target_guild(guild_id) or current_base_name != base_name:
                continue
            await _sync_guild_name(guild, current_base_name)


async def _sync_guild_name(guild: discord.Guild, base_name: str) -> bool:
    expected_name = _format_guild_name(base_name)
    if guild.name == expected_name:
        return False

    try:
        await guild.edit(name=expected_name, reason="UTC timer update")
    except (discord.Forbidden, discord.HTTPException):
        logging.warning("Failed to update UTC timer in guild name for guild %s", guild.id)
        return False

    return True


def _format_utc_time() -> str:
    now = datetime.now(timezone.utc)
    rounded_minute = (
        now.minute // _TIMER_DISPLAY_INTERVAL_MINUTES
    ) * _TIMER_DISPLAY_INTERVAL_MINUTES
    rounded_time = now.replace(minute=rounded_minute, second=0, microsecond=0)
    return rounded_time.strftime("%H:%M")


def _format_guild_name(base_name: str) -> str:
    suffix = f" [{_format_utc_time()}]"
    # Discord guild names are limited to 100 characters. Keep the timer suffix
    # intact and trim only the stored base name.
    return f"{base_name.strip()[: 100 - len(suffix)]}{suffix}"


def _extract_base_guild_name(current_name: str) -> str:
    return _UTC_SUFFIX_PATTERN.sub("", current_name).strip()


def _seconds_until_next_update() -> float:
    now = datetime.now(timezone.utc)
    next_boundary = now.replace(second=0, microsecond=0)
    minutes_until_update = _TIMER_DISPLAY_INTERVAL_MINUTES - (
        next_boundary.minute % _TIMER_DISPLAY_INTERVAL_MINUTES
    )
    if minutes_until_update == 0:
        minutes_until_update = _TIMER_DISPLAY_INTERVAL_MINUTES
    next_update = next_boundary + timedelta(minutes=minutes_until_update)
    return max((next_update - now).total_seconds(), 1.0)
