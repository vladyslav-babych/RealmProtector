"""Ticket setup editing and safe refresh of associated, still-live messages."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

import discord

from src.realm_protector.bot import tickets
from src.realm_protector.bot.config_editor import ConfigActionsView, ConfigField, UpdateConfigButton
from src.realm_protector.infrastructure import runtime_state

_NO_MENTIONS = discord.AllowedMentions.none()


async def _channel(guild: discord.Guild, channel_id: object) -> Any:
    parsed_id = tickets._parse_int(channel_id) or 0
    if not parsed_id:
        return None
    return guild.get_channel(parsed_id) or await guild.fetch_channel(parsed_id)


def _bot(guild: discord.Guild) -> discord.Client:
    return guild._state._get_client()


def _require_permissions(channel: Any, member: discord.Member, *names: str) -> None:
    permissions = channel.permissions_for(member)
    missing = [name.replace("_", " ").title() for name in names if not getattr(permissions, name)]
    if missing:
        raise ValueError(f"I need {', '.join(missing)} in {channel.mention}.")


async def _settle_publications(guild: discord.Guild, panel_id: str) -> list[str]:
    """Resolve an interrupted move before attempting another publication."""

    warnings: list[str] = []
    for record in runtime_state.list_records(
        tickets._PANEL_PUBLISH_RUNTIME_KIND, guild_id=guild.id
    ):
        if str(record.payload.get("panel_id")) != panel_id:
            continue
        if tickets._ticket_panel_publication_was_committed(record):
            old_panel = record.payload.get("panel")
            if isinstance(
                old_panel, dict
            ) and not await tickets._disable_previous_ticket_panel_message(guild, old_panel):
                warnings.append("The replaced ticket panel could not yet be disabled.")
                continue
        else:
            message = await tickets._resolve_pending_ticket_panel_message(guild, record)
            if message is not None and not await tickets._compensate_panel_publish_message(message):
                warnings.append("An interrupted ticket-panel publication still needs cleanup.")
                continue
        runtime_state.delete_record(
            tickets._PANEL_PUBLISH_RUNTIME_KIND, guild.id, record.external_id
        )
    return warnings


async def _refresh_public_panel(guild: discord.Guild, panel_id: str) -> list[str]:
    warnings = await _settle_publications(guild, panel_id)
    if warnings:
        return warnings
    panel = ADAPTER.load(guild.id, panel_id)
    if panel is None:
        return []
    destination_id = int(panel.get("panel_destination_channel_id") or 0)
    current_channel_id = int(panel.get("panel_channel_id") or 0)
    if destination_id and destination_id != current_channel_id:
        destination = await _channel(guild, destination_id)
        if not isinstance(destination, discord.TextChannel):
            return ["The new ticket panel destination is missing or is not a text channel."]
        candidate = dict(panel)
        candidate["previous_panel_channel_id"] = current_channel_id
        candidate["previous_panel_message_id"] = int(panel.get("panel_message_id") or 0)
        candidate["panel_channel_id"] = 0
        candidate["panel_message_id"] = 0
        message, operation_id = await tickets._post_pending_ticket_panel(
            _bot(guild), guild, destination, candidate, operation="resend"
        )
        latest = ADAPTER.load(guild.id, panel_id)
        if latest is None or latest != panel:
            await tickets._abort_panel_publish(
                guild.id, operation_id, candidate, message, operation="resend"
            )
            return ["Ticket configuration changed during publication; its refresh will be retried."]
        try:
            tickets._save_panel(guild.id, candidate)
        except Exception:
            await tickets._abort_panel_publish(
                guild.id, operation_id, candidate, message, operation="resend"
            )
            raise
        if not await tickets._disable_previous_ticket_panel_message(guild, candidate):
            tickets._record_panel_publish(
                guild.id, operation_id, candidate, operation="resend", status="old_cleanup_pending"
            )
            return ["The new ticket panel is ready, but the previous panel still needs disabling."]
        tickets._finish_panel_publish(guild.id, operation_id)
        return []

    message_id = int(panel.get("panel_message_id") or 0)
    if not current_channel_id or not message_id:
        return []
    try:
        channel = await _channel(guild, current_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return ["The saved ticket panel channel is unavailable."]
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        # Deleting a panel is not consent to silently publish another one.
        return []
    if not tickets._message_is_bot_authored(message, int(guild.me.id if guild.me else 0)):
        return ["The saved ticket panel message is not authored by this bot."]
    latest = ADAPTER.load(guild.id, panel_id)
    if latest is None or int(latest.get("panel_message_id") or 0) != message_id:
        return []
    await message.edit(
        content=None,
        embed=tickets._build_panel_embed(latest["panel_name"], latest["panel_message"]),
        view=tickets.TicketOpenView(_bot(guild)),
        allowed_mentions=_NO_MENTIONS,
    )
    return []


async def _find_legacy_welcome(
    guild: discord.Guild, channel: discord.TextChannel
) -> discord.Message | None:
    """Recover only an unambiguous bot-owned close panel inside a tracked ticket."""

    candidate: discord.Message | None = None
    bot_user_id = int(guild.me.id if guild.me else 0)
    if not bot_user_id:
        return None
    async for message in channel.history(limit=None):
        if not tickets._message_is_bot_authored(message, bot_user_id) or not message.embeds:
            continue
        if not any(
            getattr(component, "custom_id", None) == "tickets:close"
            for row in message.components
            for component in getattr(row, "children", ())
        ):
            continue
        if candidate is not None:
            return None
        candidate = message
    return candidate


async def _refresh_ticket_welcome(
    guild: discord.Guild, record: runtime_state.RuntimeRecord, panel: dict
) -> list[str]:
    channel_id = int(record.payload.get("channel_id") or record.external_id)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(tickets._ticket_close_locks.hold(channel_id))
        current = runtime_state.get_record(tickets._TICKET_RUNTIME_KIND, guild.id, channel_id)
        if current is None or current.status not in {"open", "creating"}:
            return []
        if current.status == "creating" and current.payload.get("creation_id"):
            await stack.enter_async_context(
                tickets._ticket_creation_locks.hold(
                    (int(guild.id), str(current.payload["creation_id"]))
                )
            )
            current = runtime_state.get_record(tickets._TICKET_RUNTIME_KIND, guild.id, channel_id)
            if current is None or current.status not in {"open", "creating"}:
                return []
        channel = await _channel(guild, channel_id)
        if not isinstance(channel, discord.TextChannel):
            return []
        if str(getattr(channel, "name", "")).startswith(("closed-", "archiving-")):
            return []
        payload = dict(current.payload)
        creation_id = str(payload.get("creation_id") or "")
        creation = (
            runtime_state.get_record(tickets._TICKET_CREATION_RUNTIME_KIND, guild.id, creation_id)
            if creation_id
            else None
        )
        message_id = int(
            payload.get("control_message_id")
            or (creation.payload.get("control_message_id") if creation else 0)
            or 0
        )
        message: discord.Message | None = None
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                return []
        elif creation_id:
            message = await tickets._find_ticket_creation_message(
                channel,
                creation_id,
                "control",
                None,
                bot_user_id=int(guild.me.id if guild.me else 0),
            )
        if message is None and current.status == "open":
            message = await _find_legacy_welcome(guild, channel)
        if message is None:
            if current.status == "creating":
                # Its creation workflow renders the current welcome text when it resumes.
                return []
            return [f"Ticket <#{channel_id}> has no recoverable welcome message ID."]
        if not tickets._message_is_bot_authored(message, int(guild.me.id if guild.me else 0)):
            return [f"The saved welcome message in <#{channel_id}> is not authored by this bot."]
        if not message.embeds:
            return [f"The saved welcome message in <#{channel_id}> has no editable panel."]
        # Modify only the template text. Preserve applicant, character title, fields,
        # statistics, footer, message content, and existing Close Ticket components.
        embeds = [embed.copy() for embed in message.embeds]
        embeds[0].description = panel["ticket_message"]
        if message.embeds[0].description != panel["ticket_message"]:
            await message.edit(embeds=embeds, allowed_mentions=_NO_MENTIONS)
        payload["control_message_id"] = int(message.id)
        payload["ticket_message"] = panel["ticket_message"]
        runtime_state.upsert_record(
            tickets._TICKET_RUNTIME_KIND, guild.id, channel_id, payload, status=current.status
        )
        return []


async def _refresh_config_overview(guild: discord.Guild, panel: dict) -> list[str]:
    message_id = int(panel.get("config_message_id") or 0)
    if not message_id:
        return []
    try:
        channel = await _channel(guild, panel.get("config_channel_id"))
        if not isinstance(channel, discord.TextChannel):
            return []
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        return []
    if not tickets._message_is_bot_authored(message, int(guild.me.id if guild.me else 0)):
        return ["The saved ticket configuration message is not authored by this bot."]
    key = str(panel["id"])
    expected_custom_id = UpdateConfigButton("ticket", key=key).item.custom_id
    if not any(
        getattr(component, "custom_id", None) == expected_custom_id
        for row in message.components
        for component in getattr(row, "children", ())
    ):
        # A setup message can have since been reused for another panel or wizard.
        return []
    await message.edit(
        embed=tickets._build_ticket_config_embed(guild, panel),
        view=ConfigActionsView("ticket", key),
        allowed_mentions=_NO_MENTIONS,
    )
    return []


class TicketConfigAdapter:
    kind = "ticket"
    title = "Ticket configuration"
    fields: tuple[ConfigField, ...] = (
        ConfigField("panel_name", "Panel title", max_length=100),
        ConfigField("management_role_ids", "Management team roles", kind="roles"),
        ConfigField("ticket_category_id", "Open ticket category", kind="category"),
        ConfigField("ticket_archive_channel_id", "Ticket archive channel", kind="channel"),
        ConfigField("panel_destination_channel_id", "Panel destination", kind="channel"),
        ConfigField("panel_message", "Public panel message", max_length=1000),
        ConfigField("ticket_message", "Opening ticket message", max_length=1000),
    )

    def keys(self, guild_id: int) -> list[str]:
        return [str(panel["id"]) for panel in tickets._list_panels(guild_id)]

    def resolve_key(self, guild_id: int, message_id: int) -> str | None:
        panel = tickets._get_panel_by_message_id(guild_id, message_id)
        if panel is None:
            panel = next(
                (
                    candidate
                    for candidate in tickets._list_panels(guild_id)
                    if int(candidate.get("config_message_id") or 0) == message_id
                ),
                None,
            )
        return str(panel["id"]) if panel else None

    def load(self, guild_id: int, key: str) -> dict | None:
        panel = tickets._get_active_panel_by_id(guild_id, key)
        if panel is None:
            return None
        values = dict(panel)
        values["panel_name"] = panel.get("panel_name") or "Ticket panel"
        values["panel_destination_channel_id"] = panel.get(
            "panel_destination_channel_id"
        ) or panel.get("panel_channel_id")
        values["panel_message"] = panel.get("panel_message") or tickets._get_default_panel_message()
        values["ticket_message"] = (
            panel.get("ticket_message") or tickets._get_default_ticket_message()
        )
        return values

    async def validate(self, guild: discord.Guild, key: str, values: dict) -> None:
        if self.load(guild.id, key) is None:
            raise ValueError("This ticket panel is no longer active.")
        for field in self.fields:
            if field.kind == "text":
                text = str(values.get(field.key) or "").strip()
                if not text or len(text) > field.max_length:
                    raise ValueError(f"{field.label} must contain 1-{field.max_length} characters.")
        _, role_error = tickets._resolve_management_roles(
            guild, values.get("management_role_ids") or []
        )
        if role_error:
            raise ValueError(role_error)
        category = await _channel(guild, values.get("ticket_category_id"))
        archive = await _channel(guild, values.get("ticket_archive_channel_id"))
        destination = await _channel(guild, values.get("panel_destination_channel_id"))
        if not isinstance(category, discord.CategoryChannel):
            raise ValueError("Select an existing ticket category in this server.")
        legacy_close = None
        if (
            not isinstance(archive, discord.TextChannel)
            and tickets._get_panel_close_mode(values) == "legacy_category"
        ):
            legacy_close = await _channel(guild, values.get("closed_ticket_category_id"))
        if not isinstance(archive, discord.TextChannel) and not isinstance(
            legacy_close, discord.CategoryChannel
        ):
            raise ValueError("Select an existing text channel for ticket archives.")
        if not isinstance(destination, discord.TextChannel):
            raise ValueError("Select an existing text channel for the public ticket panel.")
        if (
            isinstance(archive, discord.TextChannel)
            and archive.permissions_for(guild.default_role).view_channel
        ):
            raise ValueError("The ticket archive channel must be private from @everyone.")
        member = guild.me
        if member is None:
            raise ValueError("Bot member information is unavailable; please try again.")
        _require_permissions(category, member, "view_channel", "manage_channels")
        if isinstance(archive, discord.TextChannel):
            _require_permissions(
                archive,
                member,
                "view_channel",
                "send_messages",
                "read_message_history",
                "create_public_threads",
                "send_messages_in_threads",
                "embed_links",
                "attach_files",
            )
        elif legacy_close is not None:
            _require_permissions(legacy_close, member, "view_channel", "manage_channels")
        _require_permissions(
            destination,
            member,
            "view_channel",
            "send_messages",
            "embed_links",
            "read_message_history",
        )

    def save(self, guild_id: int, key: str, values: dict) -> None:
        previous = self.load(guild_id, key)
        if previous is None:
            raise ValueError("This ticket panel is no longer active.")
        snapshot = tickets._ticket_lifecycle_snapshot(previous)
        for kind, statuses in (
            (tickets._TICKET_RUNTIME_KIND, ("open", "creating")),
            (
                tickets._TICKET_CREATION_RUNTIME_KIND,
                ("pending", "channel_ready", "control_ready", "messages_ready"),
            ),
        ):
            for record in runtime_state.list_records(kind, guild_id=guild_id, statuses=statuses):
                if str(record.payload.get("panel_id")) != key or record.payload.get(
                    "workflow_owner"
                ):
                    continue
                if isinstance(record.payload.get("lifecycle_config"), dict):
                    continue
                payload = dict(record.payload)
                payload["lifecycle_config"] = snapshot
                runtime_state.upsert_record(
                    kind, guild_id, record.external_id, payload, status=record.status
                )
        updated = dict(previous)
        for field in self.fields:
            if field.key in values:
                updated[field.key] = values[field.key]
        tickets._save_panel(guild_id, updated)

    async def refresh(self, guild: discord.Guild, key: str) -> list[str]:
        panel = self.load(guild.id, key)
        if panel is None:
            return []
        warnings: list[str] = []
        try:
            warnings.extend(await _refresh_public_panel(guild, key))
        except (
            discord.Forbidden,
            discord.HTTPException,
            tickets._PanelPublishError,
            AttributeError,
        ) as error:
            warnings.append(f"The public ticket panel could not be refreshed: {error}")
        latest = self.load(guild.id, key)
        if latest is None:
            return warnings
        panel = latest
        try:
            warnings.extend(await _refresh_config_overview(guild, panel))
        except (discord.Forbidden, discord.HTTPException, AttributeError) as error:
            warnings.append(f"The ticket configuration overview could not be refreshed: {error}")
        for record in runtime_state.list_records(
            tickets._TICKET_RUNTIME_KIND, guild_id=guild.id, statuses=("open", "creating")
        ):
            if str(record.payload.get("panel_id")) != key or record.payload.get("workflow_owner"):
                continue
            try:
                warnings.extend(await _refresh_ticket_welcome(guild, record, panel))
            except discord.NotFound:
                continue
            except (discord.Forbidden, discord.HTTPException, AttributeError) as error:
                warnings.append(f"Ticket <#{record.external_id}> could not be refreshed: {error}")
        return warnings


ADAPTER = TicketConfigAdapter()
