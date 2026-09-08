"""Private trial channels and restart-safe Discord side effects.

Every external write follows a SQLite intent. The guild lifecycle lock serializes
commands and recovery; channel topics and message nonces bridge interrupted writes.
"""

from __future__ import annotations

import asyncio
import logging
import re

import discord

from src.realm_protector.bot import tickets
from src.realm_protector.bot.message_checkpoints import (
    content_with_checkpoint,
    message_has_checkpoint,
    stable_nonce,
)
from src.realm_protector.infrastructure import local_repository, runtime_state, trial_store
from src.realm_protector.services import authorization, guild_lifecycle

LOGGER = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()
CONFIGURATION_STATUSES = ("active", "publishing")


def can_manage(member, configuration: dict) -> bool:
    if authorization.member_is_admin(member):
        return True
    manager_ids = set(configuration.get("manager_role_ids", []))
    return any(
        role.id in manager_ids
        and authorization.authorization_role_configuration_error(role, member.guild) is None
        for role in member.roles
    )


def validate_configuration(guild, configuration: dict) -> None:
    category = guild.get_channel(configuration.get("category_id", 0))
    if not isinstance(category, discord.CategoryChannel):
        raise ValueError("Select an existing trial category.")
    role = guild.get_role(configuration.get("role_id", 0))
    error = authorization.automatic_role_assignment_error(role, guild)
    if error:
        raise ValueError(f"Trial role: {error}")
    manager_ids = configuration.get("manager_role_ids", [])
    if not manager_ids:
        raise ValueError("Select at least one Trial Manager role.")
    if configuration["role_id"] in manager_ids:
        raise ValueError(
            "The Trial role cannot also be a Trial Manager role: that would expose other players' trials."
        )
    for role_id in manager_ids:
        error = authorization.authorization_role_configuration_error(guild.get_role(role_id), guild)
        if error:
            raise ValueError(f"Trial Manager role: {error}")
    archive = guild.get_channel(configuration.get("archive_channel_id", 0))
    if not isinstance(archive, discord.TextChannel):
        raise ValueError("Select a text channel for trial archives.")
    if archive.permissions_for(guild.default_role).view_channel:
        raise ValueError("The archive channel must be hidden from @everyone, like ticket archives.")
    bot_member = guild.me
    if bot_member is None or not bot_member.guild_permissions.manage_roles:
        raise ValueError("The bot needs Manage Roles.")
    if not category.permissions_for(bot_member).manage_channels:
        raise ValueError("The bot needs Manage Channels in the trial category.")
    permissions = archive.permissions_for(bot_member)
    if not all(
        getattr(permissions, name)
        for name in (
            "view_channel",
            "send_messages",
            "read_message_history",
            "create_public_threads",
            "send_messages_in_threads",
            "embed_links",
            "attach_files",
        )
    ):
        raise ValueError(
            "The bot needs View Channel, Send Messages, Read History, Create Public Threads, Send in Threads, Embed Links and Attach Files in the archive channel."
        )
    if (
        not str(configuration.get("title", "")).strip()
        or not str(configuration.get("message", "")).strip()
    ):
        raise ValueError("Set both the trial panel title and message.")


def configuration_embed(configuration: dict) -> discord.Embed:
    embed = discord.Embed(
        title="Trial Configuration",
        description="## :gear: Current player trial configuration",
    )
    embed.add_field(name="Trial category", value=f"<#{configuration['category_id']}>", inline=False)
    embed.add_field(name="Trial role", value=f"<@&{configuration['role_id']}>", inline=False)
    embed.add_field(
        name="Trial Manager role(s)",
        value=", ".join(f"<@&{role_id}>" for role_id in configuration["manager_role_ids"]),
        inline=False,
    )
    embed.add_field(
        name="Trial archive channel",
        value=f"<#{configuration['archive_channel_id']}>",
        inline=False,
    )
    embed.add_field(name="Panel title", value=configuration["title"], inline=False)
    for offset in range(0, len(configuration["message"]), 1024):
        embed.add_field(
            name="Panel message" if offset == 0 else "Panel message (continued)",
            value=configuration["message"][offset : offset + 1024],
            inline=False,
        )
    embed.set_footer(text="Admins and Trial Managers: /trial-add @User • Admins: /trial-setup")
    return embed


async def publish_message(channel, record, *, embed, view=None):
    """Recover an uncertain send, save its ID, then remove the invisible checkpoint."""
    marker = f"realm:trial:{record.kind}:{record.guild_id}:{record.external_id}:{record.payload.get('publication_id', '')}"
    message_id = record.payload.get("message_id")
    message = None
    if message_id:
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            pass
    if message is None:
        async for candidate in channel.history(limit=None):
            if candidate.author.id == channel.guild.me.id and message_has_checkpoint(
                candidate, marker
            ):
                message = candidate
                break
    if message is None:
        message = await channel.send(
            content=content_with_checkpoint("", marker),
            embed=embed,
            view=view,
            nonce=stable_nonce(marker),
            allowed_mentions=NO_MENTIONS,
        )
    record = trial_store.save(record, message_id=message.id)
    await message.edit(content=None, embed=embed, view=view, allowed_mentions=NO_MENTIONS)
    return record


async def publish_configuration(guild, record):
    from src.realm_protector.bot.config_editor import ConfigActionsView

    channel = await guild.fetch_channel(record.payload["panel_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        raise ValueError("The configuration panel destination must be a text channel.")
    record = await publish_message(
        channel,
        record,
        embed=configuration_embed(record.payload),
        view=ConfigActionsView("trial", "main"),
    )
    return trial_store.save(record, status="active")


def trial_embed(payload: dict) -> discord.Embed:
    """Render a trial from its durable snapshot without fetching or changing its member."""
    configuration = payload["config"]
    embed = discord.Embed(
        title=configuration["title"],
        description=configuration["message"],
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Player", value=f"<@{payload['member_id']}> • {payload['nickname']}", inline=False
    )
    return embed


async def create_trial(guild, record):
    configuration = record.payload["config"]
    validate_configuration(guild, configuration)
    member = await guild.fetch_member(record.payload["member_id"])
    role = guild.get_role(configuration["role_id"])
    if role not in member.roles:
        await member.add_roles(role, reason="Registered player added to trial")
    channel_id = record.payload.get("channel_id")
    if channel_id:
        # A known but missing source must never create a second, empty trial.
        channel = await guild.fetch_channel(channel_id)
    else:
        marker = f"realm-trial:{record.external_id}"
        channel = next(
            (
                channel
                for channel in await guild.fetch_channels()
                if isinstance(channel, discord.TextChannel) and channel.topic == marker
            ),
            None,
        )
        if channel is None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_channels=True,
                    manage_roles=True,
                    attach_files=True,
                    embed_links=True,
                ),
            }
            for role_id in configuration["manager_role_ids"]:
                overwrites[guild.get_role(role_id)] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                )
            slug = (
                re.sub(r"[^a-z0-9-]", "-", record.payload["nickname"].lower()).strip("-")
                or "player"
            )
            channel = await guild.create_text_channel(
                name=f"{slug[:94]}-trial",
                category=guild.get_channel(configuration["category_id"]),
                overwrites=overwrites,
                topic=marker,
                reason="Start registered player's trial",
            )
        record = trial_store.save(record, channel_id=channel.id)
    if not isinstance(channel, discord.TextChannel):
        raise ValueError("The trial source is not a text channel.")
    record = await publish_message(
        channel, record, embed=trial_embed(record.payload), view=TrialView()
    )
    if channel.topic == f"realm-trial:{record.external_id}":
        await channel.edit(topic="", reason="Trial channel recorded in SQLite")
    return trial_store.save(record, status="active", last_error=None)


async def end_trial(guild, record, bot_user_id: int):
    if record.status == "ending":
        if not await tickets.archive_trial_channel(
            guild, record.payload["channel_id"], record.payload, bot_user_id
        ):
            raise ValueError(
                "Archive is not complete. Check the archive channel and bot permissions; recovery will retry."
            )
        record = trial_store.save(record, status="removing_role")
    try:
        member = await guild.fetch_member(record.payload["member_id"])
    except discord.NotFound:
        member = None
    role = guild.get_role(record.payload["config"]["role_id"])
    if member is not None and role is not None and role in member.roles:
        await member.remove_roles(role, reason="Trial ended and conversation archived")
    return trial_store.save(record, status="closed", last_error=None)


class TrialView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="End Trial", style=discord.ButtonStyle.danger, custom_id="realm:trial:end"
    )
    async def end(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None or interaction.channel_id is None or interaction.client.user is None:
            return
        await interaction.response.defer(ephemeral=True)
        async with guild_lifecycle.lock_for(guild.id):
            record = trial_store.for_channel(guild.id, interaction.channel_id)
            if record is None or record.status == "closed":
                await interaction.followup.send(
                    "This trial is already closed or no longer tracked.", ephemeral=True
                )
                return
            if not can_manage(interaction.user, record.payload["config"]):
                await interaction.followup.send(
                    "Only administrators and this trial's Trial Managers can end it.",
                    ephemeral=True,
                )
                return
            if record.status not in {"active", "ending", "removing_role"}:
                await interaction.followup.send(
                    "This trial is still being created. Please try again shortly.", ephemeral=True
                )
                return
            if record.status == "active":
                record = trial_store.save(record, status="ending")
            try:
                await end_trial(guild, record, interaction.client.user.id)
            except Exception as error:
                latest = runtime_state.get_record(record.kind, guild.id, record.external_id)
                trial_store.save(latest or record, last_error=str(error))
                LOGGER.exception("Trial end failed for %s", record.external_id)
                await interaction.followup.send(
                    "Trial end is saved and will retry automatically. An administrator should check bot permissions and logs if it remains pending.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                "Trial ended, conversation archived, and Trial role removed.", ephemeral=True
            )


async def handle_trial_add(interaction: discord.Interaction, member: discord.Member):
    guild = interaction.guild
    if guild is None:
        return
    await interaction.response.defer(ephemeral=True)
    async with guild_lifecycle.lock_for(guild.id):
        configuration = trial_store.config(guild.id)
        if configuration is None or configuration.status not in CONFIGURATION_STATUSES:
            await interaction.followup.send(
                "An administrator must run /trial-setup first.", ephemeral=True
            )
            return
        if not can_manage(interaction.user, configuration.payload):
            await interaction.followup.send(
                "Only administrators and configured Trial Managers can add players.", ephemeral=True
            )
            return
        record = None
        try:
            ledger_id = await asyncio.to_thread(
                local_repository.get_active_ledger_id, guild.id, create_if_missing=False
            )
            player = (
                await asyncio.to_thread(local_repository.get_player, ledger_id, member.id)
                if ledger_id
                else None
            )
            if player is None:
                raise ValueError("This player is not registered. They must use /register first.")
            if member.bot:
                raise ValueError("Bots cannot be added to trials.")
            validate_configuration(guild, configuration.payload)
            record = trial_store.begin(guild.id, member.id, player.nickname, configuration.payload)
            record = await create_trial(guild, record)
        except Exception as error:
            if record is not None:
                latest = runtime_state.get_record(record.kind, guild.id, record.external_id)
                trial_store.save(latest or record, last_error=str(error))
                LOGGER.exception("Trial creation failed for %s", record.external_id)
                message = "Trial creation is saved and will retry automatically. Check bot permissions and logs if it remains pending."
            else:
                message = (
                    str(error)
                    if isinstance(error, ValueError)
                    else "Unable to start a trial. Please check the bot logs."
                )
            await interaction.followup.send(message, ephemeral=True, allowed_mentions=NO_MENTIONS)
            return
        await interaction.followup.send(
            f"Trial started for {member.mention}: <#{record.payload['channel_id']}>",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )


async def reconcile_trials(bot):
    for guild in bot.guilds:
        async with guild_lifecycle.lock_for(guild.id):
            configuration = trial_store.config(guild.id)
            if configuration is not None and configuration.status == "publishing":
                try:
                    await publish_configuration(guild, configuration)
                except Exception:
                    LOGGER.exception("Trial configuration publication failed in guild %s", guild.id)
            for record in runtime_state.list_records(
                trial_store.TRIAL,
                guild_id=guild.id,
                statuses=("creating", "ending", "removing_role"),
            ):
                try:
                    if record.status == "creating":
                        await create_trial(guild, record)
                    else:
                        await end_trial(guild, record, bot.user.id)
                except Exception as error:
                    latest = runtime_state.get_record(record.kind, guild.id, record.external_id)
                    trial_store.save(latest or record, last_error=str(error))
                    LOGGER.exception("Trial recovery failed for %s", record.external_id)


def register_persistent_views(bot):
    bot.add_view(TrialView())
    from src.realm_protector.bot.trial_setup import SetupView

    for step in range(7):
        bot.add_view(SetupView(step))
