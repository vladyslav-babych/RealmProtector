from __future__ import annotations

import logging
from pathlib import Path
from typing import Tuple

import discord

from src.realm_protector.bot import message_checkpoints
from src.realm_protector.infrastructure import credential_store, guild_settings, runtime_state
from src.realm_protector.services import guild_lifecycle

LOGGER = logging.getLogger(__name__)
_PUBLICATION_RUNTIME_KIND = "configuration_panel_publication"
_PUBLICATION_EXTERNAL_ID = "panel"
_PUBLICATION_MARKER_PREFIX = "Realm Protector configuration panel"
_FALLBACK_HISTORY_LIMIT = None
_MESSAGE_CHECKPOINT_CLEANUP_FIELD = "message_checkpoint_removed"


def _publication_marker(guild_id: int) -> str:
    return f"{_PUBLICATION_MARKER_PREFIX}:{int(guild_id)}"


def _message_has_publication_marker(message: object, marker: str) -> bool:
    return message_checkpoints.message_has_checkpoint(message, marker)


def _format_named_role_mentions(guild: discord.Guild, role_names: list[str]) -> str:
    mentions: list[str] = []
    normalized = {role_name.strip().lower() for role_name in role_names if role_name.strip()}
    if not normalized:
        return "Not configured yet"

    for role in guild.roles:
        if role.name.strip().lower() in normalized:
            mentions.append(role.mention)

    return ", ".join(mentions) if mentions else ", ".join(role_names)


def _format_configured_roles(
    guild: discord.Guild,
    role_ids: list[int],
    legacy_names: list[str],
) -> str:
    if role_ids:
        mentions = [
            role.mention
            for role_id in role_ids
            for role in [guild.get_role(role_id)]
            if role is not None
        ]
        return ", ".join(mentions) if mentions else "Configured role(s) no longer exist"
    return _format_named_role_mentions(guild, legacy_names)


def _build_bot_configuration_panel(guild: discord.Guild) -> discord.Embed:
    not_configured = "Not configured yet"
    discord_server_id = guild.id

    configuration = guild_settings.get_configuration(discord_server_id)
    guild_name = configuration.target_guild_name if configuration is not None else not_configured
    caller_role_names = list(configuration.caller_role_names) if configuration else []
    economy_manager_role_names = (
        list(configuration.economy_manager_role_names) if configuration else []
    )
    member_role_name = configuration.member_role_name if configuration else "Member"

    caller_roles = _format_configured_roles(
        guild,
        list(configuration.caller_role_ids) if configuration else [],
        caller_role_names,
    )
    economy_manager_roles = _format_configured_roles(
        guild,
        list(configuration.economy_manager_role_ids) if configuration else [],
        economy_manager_role_names,
    )
    member_role_id = configuration.member_role_id if configuration else None
    member_role = _format_configured_roles(
        guild,
        [member_role_id] if member_role_id else [],
        [member_role_name],
    )
    bot_updates_channel_id = configuration.bot_updates_channel_id if configuration else None
    bot_updates_channel = (
        f"<#{bot_updates_channel_id}>" if bot_updates_channel_id else not_configured
    )

    creds_info = credential_store.get_credentials_info(discord_server_id)
    credentials_file = not_configured
    google_sheet_name = not_configured
    players_worksheet_name = not_configured
    lootsplit_history_worksheet_name = not_configured
    balance_history_worksheet_name = not_configured

    if creds_info:
        credentials_path = creds_info.get("credentials_file")
        if credentials_path:
            credentials_file = Path(str(credentials_path)).name

        google_sheet_name = creds_info.get("google_sheet_name") or not_configured
        players_worksheet_name = creds_info.get("google_worksheet_name") or not_configured
        lootsplit_history_worksheet_name = (
            creds_info.get("lootsplit_history_worksheet_name") or not_configured
        )
        balance_history_worksheet_name = (
            creds_info.get("balance_history_worksheet_name") or not_configured
        )

    embed = discord.Embed(
        title="Bot Configuration",
        description="## :gear: Current server setup and Google Sheets configuration",
    )
    embed.add_field(name="Guild name", value=guild_name, inline=False)
    embed.add_field(name="Caller role(s)", value=caller_roles, inline=False)
    embed.add_field(name="Economy Manager role(s)", value=economy_manager_roles, inline=False)
    embed.add_field(name="Member role", value=member_role, inline=False)
    embed.add_field(name="Bot updates channel", value=bot_updates_channel, inline=False)
    embed.add_field(name="Credentials file", value=credentials_file, inline=False)
    embed.add_field(name="Google Sheet name", value=google_sheet_name, inline=False)
    embed.add_field(name="Players Worksheet name", value=players_worksheet_name, inline=False)
    embed.add_field(
        name="Lootsplit History Worksheet name",
        value=lootsplit_history_worksheet_name,
        inline=False,
    )
    embed.add_field(
        name="Balance History Worksheet name", value=balance_history_worksheet_name, inline=False
    )
    return embed


def _persist_publication(
    guild_id: int,
    channel_id: int,
    *,
    status: str,
    message_id: int | None = None,
    checkpoints_removed: bool | None = None,
) -> runtime_state.RuntimeRecord:
    payload = {
        "channel_id": int(channel_id),
        "marker": _publication_marker(guild_id),
    }
    existing = runtime_state.get_record(
        _PUBLICATION_RUNTIME_KIND,
        guild_id,
        _PUBLICATION_EXTERNAL_ID,
    )
    if existing is not None:
        payload = dict(existing.payload) | payload
    if message_id:
        payload["message_id"] = int(message_id)
    elif status == "pending":
        payload.pop("message_id", None)
    if checkpoints_removed is not None:
        payload[_MESSAGE_CHECKPOINT_CLEANUP_FIELD] = bool(checkpoints_removed)
    elif status == "pending":
        payload.pop(_MESSAGE_CHECKPOINT_CLEANUP_FIELD, None)
    return runtime_state.upsert_record(
        _PUBLICATION_RUNTIME_KIND,
        guild_id,
        _PUBLICATION_EXTERNAL_ID,
        payload,
        status=status,
    )


async def _resolve_publication_channel(
    guild: discord.Guild,
    channel_id: int,
) -> discord.TextChannel:
    channel = guild.get_channel(channel_id)
    if channel is None:
        fetched_channel = await guild.fetch_channel(channel_id)
        if not isinstance(fetched_channel, discord.TextChannel):
            raise TypeError("Configuration panel channel is not a text channel")
        return fetched_channel
    if not isinstance(channel, discord.TextChannel):
        raise TypeError("Configuration panel channel is not a text channel")
    return channel


async def _find_publication_message(
    channel: discord.TextChannel,
    marker: str,
    *,
    message_id: int | None = None,
    bot_user_id: int = 0,
) -> discord.Message | None:
    if not bot_user_id:
        bot_user_id = int(
            getattr(
                getattr(getattr(channel, "guild", None), "me", None),
                "id",
                0,
            )
            or 0
        )

    def is_bot_authored(message: object) -> bool:
        if not bot_user_id:
            return True
        return int(getattr(getattr(message, "author", None), "id", 0) or 0) == bot_user_id

    if message_id:
        try:
            message = await channel.fetch_message(int(message_id))
        except discord.NotFound:
            message = None
        # Once persisted, the Discord message ID is authoritative; the remote
        # checkpoint exists solely for the send-before-ID crash window.
        if message is not None and is_bot_authored(message):
            return message
    async for message in channel.history(limit=_FALLBACK_HISTORY_LIMIT):
        if is_bot_authored(message) and _message_has_publication_marker(message, marker):
            return message
    return None


async def _cancel_publication(
    guild: discord.Guild,
    record: runtime_state.RuntimeRecord,
) -> None:
    cleaned = True
    try:
        channel_id = int(record.payload.get("channel_id") or 0)
        if channel_id:
            channel = await _resolve_publication_channel(guild, channel_id)
            message = await _find_publication_message(
                channel,
                str(record.payload.get("marker") or _publication_marker(guild.id)),
                message_id=int(record.payload.get("message_id") or 0) or None,
            )
            if message is not None:
                try:
                    await message.delete()
                except discord.Forbidden:
                    await message.edit(
                        content="This incomplete configuration panel has been disabled.",
                        embed=None,
                        view=None,
                    )
    except discord.NotFound:
        pass
    except (discord.Forbidden, discord.HTTPException, AttributeError, TypeError, ValueError):
        cleaned = False
    runtime_state.set_status(
        _PUBLICATION_RUNTIME_KIND,
        guild.id,
        _PUBLICATION_EXTERNAL_ID,
        "cancelled" if cleaned else "cleanup_pending",
    )


async def _complete_publication(
    guild: discord.Guild,
    record: runtime_state.RuntimeRecord,
) -> int | None:
    if guild_settings.get_configuration(guild.id) is None:
        await _cancel_publication(guild, record)
        return None
    try:
        channel_id = int(record.payload.get("channel_id") or 0)
        if not channel_id:
            await _cancel_publication(guild, record)
            return None
        channel = await _resolve_publication_channel(guild, channel_id)
        marker = str(record.payload.get("marker") or _publication_marker(guild.id))
        message = await _find_publication_message(
            channel,
            marker,
            message_id=int(record.payload.get("message_id") or 0) or None,
        )
        message_was_created = message is None
        if message is None:
            message = await channel.send(
                content=message_checkpoints.content_with_checkpoint(None, marker),
                embed=_build_bot_configuration_panel(guild),
                nonce=message_checkpoints.stable_nonce(marker),
            )
        record = _persist_publication(
            guild.id,
            channel_id,
            status="message_ready",
            message_id=int(message.id),
        )
        await message_checkpoints.clean_message_checkpoint(message, marker)
        if not message_was_created:
            await message.edit(
                content=None,
                embed=_build_bot_configuration_panel(guild),
            )
        record = _persist_publication(
            guild.id,
            channel_id,
            status="message_ready",
            message_id=int(message.id),
            checkpoints_removed=True,
        )
        if guild_settings.get_configuration(guild.id) is None:
            await _cancel_publication(guild, record)
            return None
        guild_settings.set_bot_configuration_message(
            guild.id,
            channel_id,
            int(message.id),
        )
        if guild_settings.get_bot_configuration_message(guild.id) != (
            channel_id,
            int(message.id),
        ):
            await _cancel_publication(guild, record)
            return None
        runtime_state.set_status(
            _PUBLICATION_RUNTIME_KIND,
            guild.id,
            _PUBLICATION_EXTERNAL_ID,
            "completed",
        )
        return int(message.id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
        return None


async def _clean_completed_publication_checkpoint(
    guild: discord.Guild,
    record: runtime_state.RuntimeRecord,
) -> bool:
    """Remove hidden and legacy markers from a completed configuration panel."""

    if record.payload.get(_MESSAGE_CHECKPOINT_CLEANUP_FIELD):
        return True
    try:
        channel_id = int(record.payload.get("channel_id") or 0)
        message_id = int(record.payload.get("message_id") or 0) or None
    except (TypeError, ValueError):
        return False
    if not channel_id:
        return False
    marker = str(record.payload.get("marker") or _publication_marker(guild.id))
    try:
        channel = await _resolve_publication_channel(guild, channel_id)
        message = await _find_publication_message(
            channel,
            marker,
            message_id=message_id,
        )
    except discord.NotFound:
        message = None
    if message is not None:
        await message_checkpoints.clean_message_checkpoint(message, marker)
    _persist_publication(
        guild.id,
        channel_id,
        status="completed",
        message_id=message_id,
        checkpoints_removed=True,
    )
    return True


async def _clean_active_configuration_panel_checkpoint(guild: discord.Guild) -> bool:
    """Clean the authoritative panel even if its historical action row is absent."""

    channel_id, message_id = guild_settings.get_bot_configuration_message(guild.id)
    if not channel_id or not message_id:
        return True
    bot_user_id = int(getattr(getattr(guild, "me", None), "id", 0) or 0)
    if not bot_user_id:
        return False
    try:
        channel = await _resolve_publication_channel(guild, int(channel_id))
        message = await channel.fetch_message(int(message_id))
    except discord.NotFound:
        return True
    except (discord.Forbidden, discord.HTTPException, AttributeError, TypeError, ValueError):
        return False
    author_id = int(getattr(getattr(message, "author", None), "id", 0) or 0)
    if author_id != bot_user_id:
        return False
    try:
        await message_checkpoints.clean_message_checkpoint(
            message,
            _publication_marker(guild.id),
        )
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return False
    return True


async def post_or_update_bot_configuration_message(
    interaction: discord.Interaction,
) -> Tuple[bool, str]:
    if interaction.guild is None or interaction.channel is None:
        return False, "Cannot access channel in this context."

    guild_id = interaction.guild.id
    async with guild_lifecycle.lock_for(guild_id):
        if guild_settings.get_configuration(guild_id) is None:
            return False, "Server configuration was removed before the panel could refresh."
        return await _post_or_update_bot_configuration_message_locked(interaction)


async def _post_or_update_bot_configuration_message_locked(
    interaction: discord.Interaction,
) -> Tuple[bool, str]:
    """Refresh the panel while the caller holds this guild's lifecycle lock."""

    if interaction.guild is None or interaction.channel is None:
        return False, "Cannot access channel in this context."

    embed = _build_bot_configuration_panel(interaction.guild)
    configuration = guild_settings.get_configuration(interaction.guild.id)
    channel_id = configuration.bot_configuration_channel_id if configuration else None
    message_id = configuration.bot_configuration_message_id if configuration else None

    if channel_id and message_id:
        try:
            target_channel = await _resolve_publication_channel(
                interaction.guild,
                channel_id,
            )
            existing_message = await target_channel.fetch_message(message_id)
            await message_checkpoints.clean_message_checkpoint(
                existing_message,
                _publication_marker(interaction.guild.id),
            )
            await existing_message.edit(content=None, embed=embed)
            try:
                _persist_publication(
                    interaction.guild.id,
                    channel_id,
                    status="completed",
                    message_id=message_id,
                    checkpoints_removed=True,
                )
            except Exception:
                LOGGER.exception(
                    "Could not persist configuration-panel checkpoint cleanup in guild %s",
                    interaction.guild.id,
                )
            return True, "Bot configuration message updated."
        except discord.NotFound:
            pass
        except discord.Forbidden:
            return False, "Missing permission to edit the existing bot configuration message."
        except (discord.HTTPException, TypeError):
            return False, "Failed to update the existing bot configuration message."

    try:
        record = _persist_publication(
            interaction.guild.id,
            interaction.channel.id,
            status="pending",
        )
    except Exception:
        LOGGER.exception(
            "Could not persist configuration-panel publication in guild %s",
            interaction.guild.id,
        )
        return False, "Local storage is unavailable, so no configuration panel was posted."

    message_id = await _complete_publication(interaction.guild, record)
    if message_id:
        return True, "Bot configuration message posted."
    return (
        False,
        "Failed to post the bot configuration message. Its saved publication will be retried automatically.",
    )


async def reconcile_configuration_panels(bot: discord.Client) -> None:
    """Complete or compensate configuration-panel publications from SQLite."""

    for record in runtime_state.list_records(
        _PUBLICATION_RUNTIME_KIND,
        statuses=("pending", "message_ready", "cleanup_pending"),
    ):
        guild = bot.get_guild(record.guild_id)
        if guild is None:
            continue
        try:
            if record.status == "cleanup_pending":
                await _cancel_publication(guild, record)
            else:
                await _complete_publication(guild, record)
        except Exception:
            LOGGER.exception(
                "Configuration-panel reconciliation failed in guild %s",
                record.guild_id,
            )

    for record in runtime_state.list_records(
        _PUBLICATION_RUNTIME_KIND,
        statuses=("completed",),
    ):
        if getattr(record, "status", None) != "completed" or record.payload.get(
            _MESSAGE_CHECKPOINT_CLEANUP_FIELD
        ):
            continue
        guild = bot.get_guild(record.guild_id)
        if guild is None:
            continue
        try:
            await _clean_completed_publication_checkpoint(guild, record)
        except (discord.Forbidden, discord.HTTPException, AttributeError, TypeError):
            LOGGER.warning(
                "Configuration-panel checkpoint still needs cleanup in guild %s",
                record.guild_id,
            )
        except Exception:
            LOGGER.exception(
                "Configuration-panel checkpoint cleanup failed in guild %s",
                record.guild_id,
            )

    for guild in getattr(bot, "guilds", ()):
        try:
            cleaned = await _clean_active_configuration_panel_checkpoint(guild)
        except Exception:
            LOGGER.exception(
                "Active configuration-panel checkpoint cleanup failed in guild %s",
                guild.id,
            )
            continue
        if not cleaned:
            LOGGER.warning(
                "Active configuration-panel checkpoint still needs cleanup in guild %s",
                guild.id,
            )


__all__ = [
    "post_or_update_bot_configuration_message",
    "reconcile_configuration_panels",
]
