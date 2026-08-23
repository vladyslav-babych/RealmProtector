import asyncio
import hashlib
import logging
import re
import unicodedata
from collections import Counter
from typing import (
    Any,
    AsyncIterator,
    Optional,
    Protocol,
    SupportsIndex,
    SupportsInt,
    runtime_checkable,
)
from urllib.parse import quote, unquote
from uuid import uuid4

import discord

from src.realm_protector.bot import message_checkpoints
from src.realm_protector.bot.character_picker import (
    CharacterSelectionView,
    build_character_selection_embed,
)
from src.realm_protector.bot.common import allowed_user_mentions
from src.realm_protector.infrastructure import (
    document_store,
    guild_settings,
    runtime_state,
)
from src.realm_protector.services import (
    albion_characters,
    authorization,
    request_limits,
    role_security,
)
from src.realm_protector.services.albion_characters import AlbionCharacterOption
from src.realm_protector.services.keyed_locks import KeyedLockPool

_NO_MENTIONS = discord.AllowedMentions.none()
_ticket_open_locks: KeyedLockPool[tuple[int, str, int]] = KeyedLockPool()
_ticket_close_locks: KeyedLockPool[int] = KeyedLockPool()
_ticket_creation_locks: KeyedLockPool[tuple[int, str]] = KeyedLockPool()
_ticket_lookup_cooldown = request_limits.Cooldown(30)
LOGGER = logging.getLogger(__name__)
_TICKET_RUNTIME_KIND = "ticket"
_TICKET_CREATION_RUNTIME_KIND = "ticket_creation"
_PANEL_PUBLISH_RUNTIME_KIND = "ticket_panel_publish"
_PANEL_PUBLISH_MARKER_PREFIX = "Realm Protector ticket panel publish:"
_ARCHIVE_MARKER_PREFIX = "Realm Protector ticket archive"
_LEGACY_TICKET_CREATION_MARKER_PREFIX = "Realm Protector ticket creation"
_ARCHIVE_HIDDEN_PREFIX = "\u2063\u2060\u2063"
_ARCHIVE_HIDDEN_SUFFIX = "\u2063\u2060\u2060"
_ARCHIVE_HIDDEN_ZERO = "\u200b"
_ARCHIVE_HIDDEN_ONE = "\u200c"
_ARCHIVE_VISIBLE_CONTENT_LIMIT = 2000 - (
    len(_ARCHIVE_HIDDEN_PREFIX) + 64 + len(_ARCHIVE_HIDDEN_SUFFIX)
)
_ARCHIVE_CHECKPOINT_CLEANUP_VERSION = 2
_FALLBACK_HISTORY_LIMIT = None
_ARCHIVE_STATE_HISTORY_LIMIT = None
_PANEL_PAGE_SIZE = 25
_TICKET_CONFIG_NAMESPACE = "tickets"


def _message_is_bot_authored(message: object, bot_user_id: int) -> bool:
    if not bot_user_id:
        return True
    return int(getattr(getattr(message, "author", None), "id", 0) or 0) == int(bot_user_id)


@runtime_checkable
class _MessageFetchingChannel(Protocol):
    async def fetch_message(self, message_id: int, /) -> discord.Message: ...


@runtime_checkable
class _MessageHistoryChannel(Protocol):
    def history(self, *, limit: Optional[int]) -> AsyncIterator[discord.Message]: ...


@runtime_checkable
class _TicketCreationMessageChannel(
    _MessageFetchingChannel,
    _MessageHistoryChannel,
    Protocol,
):
    pass


@runtime_checkable
class _LegacyTicketChannel(Protocol):
    id: int

    async def set_permissions(
        self,
        target: discord.Member,
        **permissions: Optional[bool],
    ) -> None: ...

    async def edit(self, **options: object) -> object: ...

    async def send(self, content: str, **options: object) -> object: ...


async def _send_ephemeral_notice(
    interaction: discord.Interaction,
    text: str,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def _edit_component_message(
    interaction: discord.Interaction,
    **kwargs,
) -> None:
    if interaction.response.is_done():
        await interaction.edit_original_response(**kwargs)
    else:
        await interaction.response.edit_message(**kwargs)


class _PanelPublishError(RuntimeError):
    pass


def _load_ticket_entry(guild_id: int) -> Optional[dict]:
    entry = document_store.get_mapping_entry(_TICKET_CONFIG_NAMESPACE, guild_id)
    return entry if isinstance(entry, dict) else None


def _save_ticket_entry(guild_id: int, entry: dict) -> None:
    # Guild-scoped writes avoid replacing another guild's row with a stale
    # full-document snapshot.
    document_store.upsert_mapping_entry(_TICKET_CONFIG_NAMESPACE, guild_id, entry)


def _panel_publish_marker(operation_id: str) -> str:
    return f"{_PANEL_PUBLISH_MARKER_PREFIX}{operation_id}"


def _record_panel_publish(
    guild_id: int,
    operation_id: str,
    panel: dict,
    *,
    operation: str,
    status: str,
) -> None:
    runtime_state.upsert_record(
        _PANEL_PUBLISH_RUNTIME_KIND,
        guild_id,
        operation_id,
        {
            "operation": str(operation),
            "panel_id": str(panel.get("id") or ""),
            "panel": dict(panel),
        },
        status=status,
    )


def _message_has_panel_publish_marker(message: object, operation_id: str) -> bool:
    return message_checkpoints.message_has_checkpoint(
        message,
        _panel_publish_marker(operation_id),
    )


async def _compensate_panel_publish_message(message: discord.Message) -> bool:
    """Delete a non-committed panel, or at minimum remove its controls."""

    try:
        await message.delete()
        return True
    except discord.NotFound:
        return True
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        pass

    try:
        await message.edit(
            content="This incomplete ticket panel has been disabled.",
            embed=None,
            view=None,
        )
        return True
    except discord.NotFound:
        return True
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return False


async def _abort_panel_publish(
    guild_id: int,
    operation_id: str,
    panel: dict,
    message: Optional[discord.Message],
    *,
    operation: str,
) -> None:
    try:
        _record_panel_publish(
            guild_id,
            operation_id,
            panel,
            operation=operation,
            status="cleanup_pending",
        )
    except Exception:
        LOGGER.exception(
            "Could not mark ticket-panel publication %s for cleanup",
            operation_id,
        )

    if message is not None and not await _compensate_panel_publish_message(message):
        return
    try:
        runtime_state.delete_record(
            _PANEL_PUBLISH_RUNTIME_KIND,
            guild_id,
            operation_id,
        )
    except Exception:
        LOGGER.exception(
            "Could not clear compensated ticket-panel publication %s",
            operation_id,
        )


async def _post_pending_ticket_panel(
    bot,
    guild: discord.Guild,
    destination_channel: discord.TextChannel,
    panel: dict,
    *,
    operation: str,
) -> tuple[discord.Message, str]:
    """Make a ticket panel functional only after its Discord ID is durable."""

    operation_id = uuid4().hex
    try:
        _record_panel_publish(
            guild.id,
            operation_id,
            panel,
            operation=operation,
            status="prepared",
        )
    except Exception as error:
        raise _PanelPublishError(
            "Local storage is unavailable, so no Discord panel was posted."
        ) from error

    message: Optional[discord.Message] = None
    try:
        marker = _panel_publish_marker(operation_id)
        message = await destination_channel.send(
            content=message_checkpoints.content_with_checkpoint(
                "Preparing ticket panel.",
                marker,
            ),
            nonce=message_checkpoints.stable_nonce(marker),
            allowed_mentions=_NO_MENTIONS,
        )
        panel["panel_channel_id"] = int(message.channel.id)
        panel["panel_message_id"] = int(message.id)
        _record_panel_publish(
            guild.id,
            operation_id,
            panel,
            operation=operation,
            status="placeholder_created",
        )
        await message.edit(
            content=None,
            embed=_build_panel_embed(
                str(panel.get("panel_name") or "Panel Name"),
                str(panel.get("panel_message") or _get_default_panel_message()),
            ),
            view=TicketOpenView(bot),
        )
        _record_panel_publish(
            guild.id,
            operation_id,
            panel,
            operation=operation,
            status="ready_to_commit",
        )
        return message, operation_id
    except Exception as error:
        await _abort_panel_publish(
            guild.id,
            operation_id,
            panel,
            message,
            operation=operation,
        )
        if isinstance(error, _PanelPublishError):
            raise
        raise _PanelPublishError(
            "The ticket panel could not be published or recorded safely."
        ) from error


async def _clean_committed_ticket_panel_checkpoints_for_guild(guild: discord.Guild) -> bool:
    """Sweep retained panel messages whose publication action rows no longer exist."""

    bot_user_id = int(getattr(getattr(guild, "me", None), "id", 0) or 0)
    if not bot_user_id:
        return False
    entry = _load_ticket_entry(guild.id)
    panels = entry.get("panels", {}) if isinstance(entry, dict) else {}
    if not isinstance(panels, dict):
        return True

    all_clean = True
    for panel in panels.values():
        if not isinstance(panel, dict):
            continue
        try:
            channel_id = int(panel.get("panel_channel_id") or 0)
            message_id = int(panel.get("panel_message_id") or 0)
        except (TypeError, ValueError):
            continue
        if not channel_id or not message_id:
            continue
        try:
            channel = guild.get_channel(channel_id) or await guild.fetch_channel(channel_id)
            if not isinstance(channel, _MessageFetchingChannel):
                all_clean = False
                continue
            message = await channel.fetch_message(message_id)
            if not _message_is_bot_authored(message, bot_user_id):
                all_clean = False
                continue
            await message_checkpoints.clean_message_checkpoint_prefixes(
                message,
                (_PANEL_PUBLISH_MARKER_PREFIX,),
            )
        except discord.NotFound:
            continue
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            all_clean = False
    return all_clean


def _finish_panel_publish(guild_id: int, operation_id: str) -> None:
    try:
        runtime_state.delete_record(
            _PANEL_PUBLISH_RUNTIME_KIND,
            guild_id,
            operation_id,
        )
    except Exception:
        LOGGER.exception(
            "Could not finalize ticket-panel publication %s",
            operation_id,
        )


async def _disable_previous_ticket_panel_message(
    guild: discord.Guild,
    panel: dict,
) -> bool:
    try:
        channel_id = int(panel.get("previous_panel_channel_id") or 0)
        message_id = int(panel.get("previous_panel_message_id") or 0)
    except (TypeError, ValueError):
        return True
    if not channel_id or not message_id:
        return True
    try:
        channel: discord.abc.GuildChannel | discord.Thread | None = guild.get_channel(channel_id)
        if channel is None:
            channel = await guild.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return False
        message = await channel.fetch_message(message_id)
        await message.edit(
            content="This ticket panel has been replaced and is no longer active.",
            embed=None,
            view=None,
        )
    except discord.NotFound:
        return True
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return False
    return True


def _ticket_runtime_payload(
    channel,
    metadata: dict[str, str],
    *,
    stats: Optional[dict] = None,
    pve_total: Optional[int] = None,
    extra: Optional[dict] = None,
) -> dict:
    payload = {
        "channel_id": int(channel.id),
        "channel_name": str(getattr(channel, "name", "") or ""),
        "panel_id": str(metadata.get("panel_id") or ""),
        "opener_id": str(metadata.get("opener_id") or ""),
        "opener_slug": str(metadata.get("opener_slug") or ""),
        "character": str(metadata.get("character") or ""),
        "albion_id": str(metadata.get("albion_id") or ""),
    }
    if stats is not None:
        payload["character_stats"] = dict(stats)
    if pve_total is not None:
        payload["pve_total"] = int(pve_total)
    if extra:
        payload.update(extra)
    return payload


def _persist_ticket(
    channel,
    metadata: dict[str, str],
    *,
    status: str,
    stats: Optional[dict] = None,
    pve_total: Optional[int] = None,
    extra: Optional[dict] = None,
) -> None:
    guild = getattr(channel, "guild", None)
    if guild is None:
        return
    existing = runtime_state.get_record(_TICKET_RUNTIME_KIND, guild.id, channel.id)
    payload = dict(existing.payload) if existing is not None else {}
    payload.update(
        _ticket_runtime_payload(
            channel,
            metadata,
            stats=stats,
            pve_total=pve_total,
            extra=extra,
        )
    )
    runtime_state.upsert_record(
        _TICKET_RUNTIME_KIND,
        guild.id,
        channel.id,
        payload,
        status=status,
    )


def _ensure_guild_entry(entry: Optional[dict]) -> dict:
    if not isinstance(entry, dict):
        entry = {"panels": {}}

    entry.setdefault("panels", {})
    return entry


def _list_panels(guild_id: int) -> list[dict]:
    entry = _ensure_guild_entry(_load_ticket_entry(guild_id))
    panels = entry.get("panels", {})
    if not isinstance(panels, dict):
        return []
    return [
        panel for panel in panels.values() if isinstance(panel, dict) and panel.get("active", True)
    ]


async def deactivate_guild_ticket_configuration(guild: discord.Guild) -> bool:
    """Disable ticket panels while retaining metadata needed by open tickets."""

    entry = _load_ticket_entry(guild.id)
    if not isinstance(entry, dict):
        return False
    panels = entry.get("panels", {})
    if not isinstance(panels, dict):
        return False

    for panel in panels.values():
        if not isinstance(panel, dict):
            continue
        panel["active"] = False
    _save_ticket_entry(guild.id, entry)

    for panel in panels.values():
        if not isinstance(panel, dict):
            continue
        try:
            channel_id = int(panel.get("panel_channel_id") or 0)
            message_id = int(panel.get("panel_message_id") or 0)
        except (TypeError, ValueError):
            continue
        if not channel_id or not message_id:
            continue
        try:
            channel = guild.get_channel(channel_id) or await guild.fetch_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                message = await channel.fetch_message(message_id)
                await message.edit(
                    content="This ticket panel has been disabled.",
                    embed=None,
                    view=None,
                )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            continue

    return True


def _get_panel_by_id(guild_id: int, panel_id: str) -> Optional[dict]:
    entry = _ensure_guild_entry(_load_ticket_entry(guild_id))
    panels = entry.get("panels", {})
    if not isinstance(panels, dict):
        return None
    panel = panels.get(panel_id)
    return panel if isinstance(panel, dict) else None


def _get_active_panel_by_id(guild_id: int, panel_id: str) -> Optional[dict]:
    if not guild_settings.get_target_guild(guild_id):
        return None
    panel = _get_panel_by_id(guild_id, panel_id)
    if panel is None or not panel.get("active", True):
        return None
    return panel


def _get_panel_by_message_id(guild_id: int, message_id: int) -> Optional[dict]:
    for panel in _list_panels(guild_id):
        if int(panel.get("panel_message_id", 0) or 0) == message_id:
            return panel
    return None


def _save_panel(guild_id: int, panel: dict) -> None:
    entry = _ensure_guild_entry(_load_ticket_entry(guild_id))
    panels = entry.setdefault("panels", {})
    panels[str(panel["id"])] = panel
    _save_ticket_entry(guild_id, entry)


def _delete_panel(guild_id: int, panel_id: str) -> None:
    """Hide a panel while retaining data required to close existing tickets."""

    entry = _load_ticket_entry(guild_id)
    if not isinstance(entry, dict):
        return
    panels = entry.get("panels", {})
    if isinstance(panels, dict):
        panel = panels.get(str(panel_id))
        if not isinstance(panel, dict):
            return
        panel["active"] = False
    _save_ticket_entry(guild_id, entry)


def _format_role_mentions(guild: discord.Guild, role_ids: list[int]) -> str:
    mentions = []
    for role_id in role_ids:
        role = guild.get_role(int(role_id))
        if role is not None:
            mentions.append(role.mention)
    return ", ".join(mentions) if mentions else "Not selected"


def _format_role_names(guild: discord.Guild, role_ids: list[int]) -> str:
    names = []
    for role_id in role_ids:
        role = guild.get_role(int(role_id))
        if role is not None:
            names.append(role.name)
    return ", ".join(names) if names else "Not selected"


def _format_category_name(guild: discord.Guild, category_id: Optional[int]) -> str:
    if not category_id:
        return "Not selected"
    category = guild.get_channel(int(category_id))
    return category.name if category is not None else "Not selected"


def _parse_int(value: object) -> Optional[int]:
    if not isinstance(
        value,
        (str, bytes, bytearray, SupportsInt, SupportsIndex),
    ):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _has_configured_channel_id(value: object) -> bool:
    parsed = _parse_int(value)
    return parsed is not None and parsed > 0


def _get_panel_close_mode(panel: dict) -> str:
    """Return the close workflow supported by a stored panel schema.

    Archive channels are the current schema. ``closed_ticket_category_id`` is
    retained as a compatibility fallback for panels created before archives were
    introduced; it must not be rewritten because a category ID is not a channel
    ID.
    """
    if _has_configured_channel_id(panel.get("ticket_archive_channel_id")):
        return "archive"
    if _has_configured_channel_id(panel.get("closed_ticket_category_id")):
        return "legacy_category"
    return "unconfigured"


def _slugify_channel_component(value: str, *, fallback: str = "user", max_length: int = 50) -> str:
    if not value:
        return fallback

    normalized = unicodedata.normalize("NFKD", str(value))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_value = ascii_value.lower().strip()
    ascii_value = re.sub(r"[\s_]+", "-", ascii_value)
    ascii_value = re.sub(r"[^a-z0-9-]", "", ascii_value)
    ascii_value = re.sub(r"-+", "-", ascii_value).strip("-")
    if not ascii_value:
        ascii_value = fallback
    return ascii_value[:max_length]


def _format_channel_mention(guild: discord.Guild, channel_id: Optional[int]) -> str:
    if not channel_id:
        return "Not selected"
    channel = guild.get_channel(int(channel_id))
    return channel.mention if channel is not None else "Not selected"


def _has_management_access(member: discord.Member, management_role_ids: list[int]) -> bool:
    return authorization.member_is_admin(member) or role_security.member_has_safe_privileged_role(
        member,
        role_ids=management_role_ids,
    )


def _resolve_management_roles(
    guild: discord.Guild,
    role_ids: list[int],
) -> tuple[list[discord.Role], Optional[str]]:
    if not role_ids:
        return [], "Select at least one management team role."
    roles: list[discord.Role] = []
    seen_role_ids: set[int] = set()
    for raw_role_id in role_ids:
        try:
            role_id = int(raw_role_id)
        except (TypeError, ValueError):
            return [], "A configured management role ID is invalid."
        if role_id in seen_role_ids:
            continue
        role = guild.get_role(role_id)
        role_error = role_security.privileged_assignment_error(role, guild)
        if role is None:
            return [], role_error or "A configured management role was not found."
        if role_error:
            return [], role_error
        seen_role_ids.add(role_id)
        roles.append(role)
    return roles, None


def _build_ticket_topic(panel_id: str, opener_id: int) -> str:
    return f"panel_id={panel_id};opener_id={opener_id}"


def _build_ticket_topic_with_slug(
    panel_id: str,
    opener_id: int,
    opener_slug: str,
) -> str:
    base = _build_ticket_topic(panel_id, opener_id)
    return f"{base};opener_slug={opener_slug}"


def _build_ticket_topic_with_character(
    panel_id: str,
    opener_id: int,
    opener_slug: str,
    character_nickname: str,
    albion_player_id: Optional[str],
    creation_id: Optional[str] = None,
) -> str:
    base = _build_ticket_topic_with_slug(panel_id, opener_id, opener_slug)
    nickname_safe = quote((character_nickname or "").strip()[:80])
    player_id_safe = quote((albion_player_id or "").strip()[:80])
    topic = f"{base};character={nickname_safe};albion_id={player_id_safe}"
    if creation_id:
        marker = _ticket_creation_marker(str(creation_id), "channel")
        topic += ";" + message_checkpoints.hidden_checkpoint(
            marker,
            nonce=_ticket_creation_nonce(str(creation_id), "channel"),
        )
    return topic


def _get_ticket_character_nickname(metadata: dict[str, str]) -> str:
    raw = metadata.get("character") or ""
    try:
        value = unquote(raw)
    except Exception:
        value = raw
    return (value or "").strip()


def _build_ticket_channel_name(status: str, opener_slug: str) -> str:
    max_slug_length = max(1, 100 - len(status) - 1)
    opener_slug = _slugify_channel_component(opener_slug, max_length=max_slug_length)
    return f"{status}-{opener_slug}"


def _parse_ticket_topic(topic: Optional[str]) -> dict[str, str]:
    if not topic:
        return {}

    result = {}
    for part in topic.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        result[key.strip()] = value.strip()
    return result


class _StoredTicketChannelReference:
    def __init__(self, channel_id: int):
        self.id = int(channel_id)
        self.mention = f"<#{self.id}>"


async def _find_existing_open_ticket_channel(
    guild: discord.Guild,
    panel_id: str,
    opener_id: int,
):
    for record in runtime_state.list_records(
        _TICKET_RUNTIME_KIND,
        guild_id=guild.id,
        statuses=("open",),
    ):
        if str(record.payload.get("panel_id") or "") != str(panel_id):
            continue
        if str(record.payload.get("opener_id") or "") != str(opener_id):
            continue
        try:
            channel_id = int(record.external_id)
        except ValueError:
            continue
        try:
            channel = await _fetch_guild_channel(guild, channel_id)
        except discord.NotFound:
            runtime_state.set_status(
                _TICKET_RUNTIME_KIND,
                guild.id,
                record.external_id,
                "missing",
            )
            continue
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            # A transient cache/API failure must not permit a duplicate ticket.
            return _StoredTicketChannelReference(channel_id)
        if channel is not None:
            return channel

    # Compatibility fallback for tickets created before runtime persistence.
    for channel in guild.text_channels:
        if channel.name.startswith("closed-"):
            continue

        metadata = _parse_ticket_topic(channel.topic)
        if metadata.get("panel_id") != str(panel_id):
            continue
        if metadata.get("opener_id") != str(opener_id):
            continue
        return channel

    return None


def _get_default_panel_message() -> str:
    return "Click the button below to open a ticket."


def _get_default_ticket_message() -> str:
    return "Use this channel for the guild application. Management team can close it when review is complete."


def _build_panel_embed(panel_name: str, panel_message: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(
        title=panel_name, description=panel_message or _get_default_panel_message()
    )
    return embed


def _build_setup_embed(view: "TicketPanelSetupView") -> discord.Embed:
    embed = discord.Embed(title=f"Ticket Panel Setup - Step {view.step}/7")
    state = view.state
    guild = view.guild

    if view.step == 1:
        embed.description = "## :pencil: Set the panel name"
        embed.add_field(name="Panel name", value=state["panel_name"], inline=False)
    elif view.step == 2:
        embed.description = "## :tickets: Select the management team role(s)"
        embed.add_field(
            name="Selected management team roles",
            value=_format_role_mentions(guild, state["management_role_ids"]),
            inline=False,
        )
    elif view.step == 3:
        embed.description = "## :open_file_folder: Select the open ticket category"
        embed.add_field(
            name="Selected category",
            value=_format_category_name(guild, state["ticket_category_id"]),
            inline=False,
        )
    elif view.step == 4:
        embed.description = "## :file_folder: Select the ticket archive channel"
        embed.add_field(
            name="Selected archive channel",
            value=_format_channel_mention(guild, state["ticket_archive_channel_id"]),
            inline=False,
        )
    elif view.step == 5:
        embed.description = "## :dart: Select the panel destination channel"
        embed.add_field(
            name="Selected panel destination",
            value=_format_channel_mention(guild, state["panel_destination_channel_id"]),
            inline=False,
        )
    elif view.step == 6:
        embed.description = (
            "## :speech_balloon: Set the panel message and the opening ticket message"
        )
        embed.add_field(name="Panel message", value=state["panel_message"], inline=False)
        embed.add_field(name="Ticket message", value=state["ticket_message"], inline=False)
    else:
        embed.description = "## :clipboard: Review the summary and finish panel creation"
        embed.add_field(name="Panel name", value=state["panel_name"], inline=False)
        embed.add_field(
            name="Management team role(s)",
            value=_format_role_mentions(guild, state["management_role_ids"]),
            inline=False,
        )
        embed.add_field(
            name="Ticket category",
            value=_format_category_name(guild, state["ticket_category_id"]),
            inline=False,
        )
        embed.add_field(
            name="Ticket archive channel",
            value=_format_channel_mention(guild, state["ticket_archive_channel_id"]),
            inline=False,
        )
        embed.add_field(
            name="Panel destination",
            value=_format_channel_mention(guild, state["panel_destination_channel_id"]),
            inline=False,
        )
        embed.add_field(name="Panel message", value=state["panel_message"], inline=False)
        embed.add_field(name="Ticket message", value=state["ticket_message"], inline=False)
        embed.add_field(
            name="Panel preview",
            value=f"**{state['panel_name']}**\n{state['panel_message']}",
            inline=False,
        )

    return embed


def _build_home_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="Tickets Setup", description="Choose how you want to configure the ticket system."
    )
    embed.add_field(name="Guild", value=guild.name, inline=False)
    return embed


def _build_manage_embed(
    guild: discord.Guild, panels: list[dict], selected_panel_id: Optional[str]
) -> discord.Embed:
    embed = discord.Embed(title="Manage Ticket Panels")
    if not panels:
        embed.description = "No ticket panels are configured yet."
        return embed

    selected_panel = None
    for panel in panels:
        if panel.get("id") == selected_panel_id:
            selected_panel = panel
            break
    if selected_panel is None:
        selected_panel = panels[0]

    embed.description = "Select a panel to resend or delete it."
    embed.add_field(
        name="Panel name", value=selected_panel.get("panel_name", "Unknown"), inline=False
    )
    embed.add_field(
        name="Management team role(s)",
        value=_format_role_mentions(guild, selected_panel.get("management_role_ids", [])),
        inline=False,
    )
    embed.add_field(
        name="Ticket category",
        value=_format_category_name(guild, selected_panel.get("ticket_category_id")),
        inline=False,
    )
    embed.add_field(
        name="Ticket archive channel",
        value=_format_channel_mention(guild, selected_panel.get("ticket_archive_channel_id")),
        inline=False,
    )
    if _get_panel_close_mode(selected_panel) == "legacy_category":
        embed.add_field(
            name="Legacy closed category",
            value=_format_category_name(
                guild,
                selected_panel.get("closed_ticket_category_id"),
            ),
            inline=False,
        )
        embed.add_field(
            name="Migration required",
            value=(
                "This legacy panel still closes tickets in its closed category. "
                "Create an archive-channel panel to replace it, then delete this "
                "panel. Existing tickets will remain closable."
            ),
            inline=False,
        )
    embed.add_field(
        name="Panel destination",
        value=_format_channel_mention(
            guild,
            selected_panel.get("panel_destination_channel_id")
            or selected_panel.get("panel_channel_id"),
        ),
        inline=False,
    )
    embed.add_field(
        name="Panel message",
        value=selected_panel.get("panel_message") or _get_default_panel_message(),
        inline=False,
    )
    embed.add_field(
        name="Ticket message",
        value=selected_panel.get("ticket_message") or _get_default_ticket_message(),
        inline=False,
    )
    embed.add_field(
        name="Panel channel",
        value=_format_channel_mention(guild, selected_panel.get("panel_channel_id")),
        inline=False,
    )
    return embed


class PanelNameModal(discord.ui.Modal, title="Set Panel Name"):
    panel_name: discord.ui.TextInput["PanelNameModal"] = discord.ui.TextInput(
        label="Panel name",
        required=True,
        max_length=100,
        default="Panel Name",
    )

    def __init__(self, parent_view: "TicketPanelSetupView"):
        super().__init__()
        self.parent_view = parent_view
        self.panel_name.default = parent_view.state["panel_name"]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.parent_view.state["panel_name"] = str(self.panel_name).strip() or "Panel Name"
        if self.parent_view.host_message is not None:
            await self.parent_view.host_message.edit(
                embed=_build_setup_embed(self.parent_view), view=self.parent_view
            )
        await interaction.response.send_message("Panel name updated.", ephemeral=True)


class PanelMessagesModal(discord.ui.Modal, title="Set Ticket Messages"):
    panel_message: discord.ui.TextInput["PanelMessagesModal"] = discord.ui.TextInput(
        label="Panel message",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )
    ticket_message: discord.ui.TextInput["PanelMessagesModal"] = discord.ui.TextInput(
        label="Ticket opening message",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    def __init__(self, parent_view: "TicketPanelSetupView"):
        super().__init__()
        self.parent_view = parent_view
        self.panel_message.default = parent_view.state["panel_message"]
        self.ticket_message.default = parent_view.state["ticket_message"]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.parent_view.state["panel_message"] = (
            str(self.panel_message).strip() or _get_default_panel_message()
        )
        self.parent_view.state["ticket_message"] = (
            str(self.ticket_message).strip() or _get_default_ticket_message()
        )
        if self.parent_view.host_message is not None:
            await self.parent_view.host_message.edit(
                embed=_build_setup_embed(self.parent_view), view=self.parent_view
            )
        await interaction.response.send_message("Ticket messages updated.", ephemeral=True)


class ManagementRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(
            placeholder="Select all roles for management team", min_values=1, max_values=25
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, TicketPanelSetupView):
            return
        if not await view.ensure_owner(interaction):
            return
        for role in self.values:
            role_error = role_security.privileged_assignment_error(
                role,
                view.guild,
            )
            if role_error:
                await interaction.response.send_message(role_error, ephemeral=True)
                return
        view.state["management_role_ids"] = [role.id for role in self.values]
        await interaction.response.edit_message(embed=_build_setup_embed(view), view=view)


class TicketCategorySelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="Select the ticket category",
            channel_types=[discord.ChannelType.category],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, TicketPanelSetupView):
            return
        if not await view.ensure_owner(interaction):
            return
        selected = self.values[0]
        view.state["ticket_category_id"] = selected.id
        await interaction.response.edit_message(embed=_build_setup_embed(view), view=view)


class TicketArchiveChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="Select the ticket archive channel",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, TicketPanelSetupView):
            return
        if not await view.ensure_owner(interaction):
            return
        selected = self.values[0]
        archive_channel = view.guild.get_channel(selected.id)
        if not isinstance(archive_channel, discord.TextChannel):
            await interaction.response.send_message(
                "Select a text channel from this server.",
                ephemeral=True,
            )
            return
        if archive_channel.permissions_for(view.guild.default_role).view_channel:
            await interaction.response.send_message(
                "The archive channel must be private from @everyone because ticket transcripts can contain sensitive content.",
                ephemeral=True,
            )
            return
        view.state["ticket_archive_channel_id"] = archive_channel.id
        await interaction.response.edit_message(embed=_build_setup_embed(view), view=view)


class PanelDestinationChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="Select panel destination channel",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, TicketPanelSetupView):
            return
        if not await view.ensure_owner(interaction):
            return
        selected = self.values[0]
        view.state["panel_destination_channel_id"] = selected.id
        await interaction.response.edit_message(embed=_build_setup_embed(view), view=view)


class TicketPanelSetupView(discord.ui.View):
    def __init__(
        self,
        bot,
        guild: discord.Guild,
        user_id: int,
        setup_channel: discord.abc.Messageable,
        state: Optional[dict] = None,
        step: int = 1,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.guild = guild
        self.user_id = user_id
        self.setup_channel = setup_channel
        self.step = step
        self.state = state or {
            "panel_name": "Panel Name",
            "management_role_ids": [],
            "ticket_category_id": None,
            "ticket_archive_channel_id": None,
            "panel_destination_channel_id": None,
            "panel_message": _get_default_panel_message(),
            "ticket_message": _get_default_ticket_message(),
        }
        self.host_message: Optional[discord.Message] = None
        self._build_items()

    async def ensure_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await _send_ephemeral_notice(
                interaction, "Only the admin who started this setup can use these controls."
            )
            return False
        if (
            interaction.guild is None
            or interaction.guild.id != self.guild.id
            or not isinstance(interaction.user, discord.Member)
            or not await authorization.is_admin(interaction.user)
        ):
            await _send_ephemeral_notice(
                interaction,
                "Your Administrator permission was removed; this setup can no longer be used.",
            )
            return False
        return True

    def _build_items(self) -> None:
        self.clear_items()
        self.add_item(SetupBackButton())
        if self.step == 1:
            self.add_item(SetPanelNameButton())
            self.add_item(SetupContinueButton())
            self.add_item(CancelSetupButton())
        elif self.step == 2:
            self.add_item(ManagementRoleSelect())
            self.add_item(SetupContinueButton())
        elif self.step == 3:
            self.add_item(TicketCategorySelect())
            self.add_item(SetupContinueButton())
        elif self.step == 4:
            self.add_item(TicketArchiveChannelSelect())
            self.add_item(SetupContinueButton())
        elif self.step == 5:
            self.add_item(PanelDestinationChannelSelect())
            self.add_item(SetupContinueButton())
        elif self.step == 6:
            self.add_item(SetPanelMessagesButton())
            self.add_item(SetupContinueButton())
        else:
            self.add_item(FinishPanelButton())

    def next_step(self) -> None:
        self.step += 1
        self._build_items()

    def previous_step(self) -> None:
        self.step = max(1, self.step - 1)
        self._build_items()


class SetupBackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, TicketPanelSetupView):
            return
        if not await view.ensure_owner(interaction):
            return
        view.previous_step()
        await interaction.response.edit_message(embed=_build_setup_embed(view), view=view)


class SetPanelNameButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Set panel name", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, TicketPanelSetupView):
            return
        if not await view.ensure_owner(interaction):
            return
        await interaction.response.send_modal(PanelNameModal(view))


class SetPanelMessagesButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Set messages", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, TicketPanelSetupView):
            return
        if not await view.ensure_owner(interaction):
            return
        await interaction.response.send_modal(PanelMessagesModal(view))


class CancelSetupButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel Setup", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, TicketPanelSetupView):
            return
        if not await view.ensure_owner(interaction):
            return
        await interaction.response.edit_message(
            embed=_build_home_embed(view.guild),
            view=TicketsSetupHomeView(view.bot, view.user_id, view.guild),
        )


class SetupContinueButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Save and Continue", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, TicketPanelSetupView):
            return
        if not await view.ensure_owner(interaction):
            return

        if view.step == 2 and not view.state["management_role_ids"]:
            await interaction.response.send_message(
                "Select at least one management team role.", ephemeral=True
            )
            return
        if view.step == 3 and not view.state["ticket_category_id"]:
            await interaction.response.send_message("Select a ticket category.", ephemeral=True)
            return
        if view.step == 4 and not view.state["ticket_archive_channel_id"]:
            await interaction.response.send_message(
                "Select a ticket archive channel.", ephemeral=True
            )
            return
        if view.step == 5 and not view.state["panel_destination_channel_id"]:
            await interaction.response.send_message(
                "Select a panel destination channel.", ephemeral=True
            )
            return

        view.next_step()
        await interaction.response.edit_message(embed=_build_setup_embed(view), view=view)


class FinishPanelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Finish", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, TicketPanelSetupView):
            return
        if not await view.ensure_owner(interaction):
            return
        if not isinstance(interaction.user, discord.Member) or not await authorization.is_admin(
            interaction.user
        ):
            await interaction.response.send_message(
                "Your Administrator permission was removed; the panel was not created.",
                ephemeral=True,
            )
            return
        if not guild_settings.get_target_guild(view.guild.id):
            await interaction.response.send_message(
                "This server is no longer configured. Run **/bot-setup** first.",
                ephemeral=True,
            )
            return

        open_category = view.guild.get_channel(int(view.state.get("ticket_category_id") or 0))
        if not isinstance(open_category, discord.CategoryChannel):
            await interaction.response.send_message(
                "Configured ticket category was not found.", ephemeral=True
            )
            return

        archive_channel = view.guild.get_channel(
            int(view.state.get("ticket_archive_channel_id") or 0)
        )
        if not isinstance(archive_channel, discord.TextChannel):
            await interaction.response.send_message(
                "Configured ticket archive channel was not found.", ephemeral=True
            )
            return
        if archive_channel.permissions_for(view.guild.default_role).view_channel:
            await interaction.response.send_message(
                "The ticket archive channel must be private from @everyone.",
                ephemeral=True,
            )
            return

        _management_roles, management_role_error = _resolve_management_roles(
            view.guild,
            view.state.get("management_role_ids", []),
        )
        if management_role_error:
            await interaction.response.send_message(
                f"Management role configuration error: {management_role_error}",
                ephemeral=True,
            )
            return

        panel_destination_channel = view.guild.get_channel(
            int(view.state["panel_destination_channel_id"] or 0)
        )
        if not isinstance(panel_destination_channel, discord.TextChannel):
            await interaction.response.send_message(
                "Configured panel destination channel was not found.", ephemeral=True
            )
            return

        bot_member: Optional[discord.Member] = getattr(view.guild, "me", None)
        if bot_member is None:
            await interaction.response.send_message(
                "Bot member information is unavailable. Please try again.", ephemeral=True
            )
            return

        channel_permissions = panel_destination_channel.permissions_for(bot_member)
        if (
            not channel_permissions.view_channel
            or not channel_permissions.send_messages
            or not channel_permissions.embed_links
        ):
            await interaction.response.send_message(
                f"I don't have enough permissions to post in {panel_destination_channel.mention}. Please grant View Channel, Send Messages, and Embed Links.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        panel_id = uuid4().hex[:10]
        panel = {
            "id": panel_id,
            "panel_name": view.state["panel_name"],
            "management_role_ids": view.state["management_role_ids"],
            "ticket_category_id": view.state["ticket_category_id"],
            "ticket_archive_channel_id": view.state["ticket_archive_channel_id"],
            "panel_destination_channel_id": view.state["panel_destination_channel_id"],
            "panel_message": view.state["panel_message"],
            "ticket_message": view.state["ticket_message"],
            "panel_channel_id": 0,
            "panel_message_id": 0,
            "active": True,
        }
        try:
            panel_message, publish_operation_id = await _post_pending_ticket_panel(
                view.bot,
                view.guild,
                panel_destination_channel,
                panel,
                operation="create",
            )
        except _PanelPublishError as error:
            await _send_ephemeral_notice(interaction, str(error))
            return

        post_send_error: Optional[str] = None
        if not isinstance(interaction.user, discord.Member) or not await authorization.is_admin(
            interaction.user
        ):
            post_send_error = (
                "Your Administrator permission was removed while the panel was being posted."
            )
        elif not guild_settings.get_target_guild(view.guild.id):
            post_send_error = "The bot setup was removed while the panel was being posted."
        else:
            _management_roles, management_role_error = _resolve_management_roles(
                view.guild,
                view.state.get("management_role_ids", []),
            )
            if management_role_error:
                post_send_error = (
                    f"Management role configuration changed while posting: {management_role_error}"
                )
        if post_send_error:
            await _abort_panel_publish(
                view.guild.id,
                publish_operation_id,
                panel,
                panel_message,
                operation="create",
            )
            await _send_ephemeral_notice(interaction, post_send_error)
            return

        try:
            _save_panel(view.guild.id, panel)
        except Exception:
            await _abort_panel_publish(
                view.guild.id,
                publish_operation_id,
                panel,
                panel_message,
                operation="create",
            )
            await _send_ephemeral_notice(
                interaction,
                "The panel could not be saved locally, so its Discord message was disabled.",
            )
            return
        _finish_panel_publish(view.guild.id, publish_operation_id)

        await _edit_component_message(
            interaction,
            embed=discord.Embed(
                title="Ticket panel created",
                description=f"Panel **{panel['panel_name']}** was posted in {panel_destination_channel.mention}.",
            ),
            view=TicketsSetupHomeView(view.bot, view.user_id, view.guild),
        )


class ManagePanelSelect(discord.ui.Select):
    def __init__(self, panels: list[dict], selected_panel_id: Optional[str]):
        options = [
            discord.SelectOption(
                label=panel.get("panel_name", "Panel"),
                value=str(panel.get("id")),
                default=str(panel.get("id")) == str(selected_panel_id),
            )
            for panel in panels
        ]
        super().__init__(placeholder="Select panel", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ManagePanelsView):
            return
        if not await view.ensure_owner(interaction):
            return
        view.selected_panel_id = self.values[0]
        view._build_items()
        await interaction.response.edit_message(
            embed=_build_manage_embed(view.guild, view.panels, view.selected_panel_id), view=view
        )


class ManagePanelsView(discord.ui.View):
    def __init__(
        self,
        bot,
        guild: discord.Guild,
        user_id: int,
        panels: list[dict],
        selected_panel_id: Optional[str] = None,
        page: int = 0,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.guild = guild
        self.user_id = user_id
        self.panels = panels
        self.selected_panel_id = selected_panel_id or (str(panels[0].get("id")) if panels else None)
        self.page = max(0, int(page))
        if self.selected_panel_id:
            for index, panel in enumerate(self.panels):
                if str(panel.get("id")) == str(self.selected_panel_id):
                    self.page = index // _PANEL_PAGE_SIZE
                    break
        self._build_items()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.panels) + _PANEL_PAGE_SIZE - 1) // _PANEL_PAGE_SIZE)

    def _visible_panels(self) -> list[dict]:
        self.page = min(self.page, self.page_count - 1)
        start = self.page * _PANEL_PAGE_SIZE
        return self.panels[start : start + _PANEL_PAGE_SIZE]

    async def ensure_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await _send_ephemeral_notice(
                interaction, "Only the admin who opened panel management can use these controls."
            )
            return False
        if (
            interaction.guild is None
            or interaction.guild.id != self.guild.id
            or not isinstance(interaction.user, discord.Member)
            or not await authorization.is_admin(interaction.user)
        ):
            await _send_ephemeral_notice(
                interaction,
                "Your Administrator permission was removed; panel management can no longer be used.",
            )
            return False
        return True

    def _get_selected_panel(self) -> Optional[dict]:
        if self.selected_panel_id is None:
            return None
        return _get_active_panel_by_id(
            self.guild.id,
            str(self.selected_panel_id),
        )

    def _build_items(self) -> None:
        self.clear_items()
        if self.panels:
            visible = self._visible_panels()
            if not any(str(panel.get("id")) == str(self.selected_panel_id) for panel in visible):
                self.selected_panel_id = str(visible[0].get("id")) if visible else None
            self.add_item(ManagePanelSelect(visible, self.selected_panel_id))
            self.add_item(ResendPanelButton())
            self.add_item(DeletePanelButton())
            if self.page_count > 1:
                self.add_item(ManagePreviousPageButton(disabled=self.page <= 0))
                self.add_item(ManageNextPageButton(disabled=self.page >= self.page_count - 1))
        self.add_item(ManageBackButton())


class ManagePreviousPageButton(discord.ui.Button):
    def __init__(self, *, disabled: bool):
        super().__init__(label="Previous", style=discord.ButtonStyle.secondary, disabled=disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ManagePanelsView) or not await view.ensure_owner(interaction):
            return
        view.page = max(0, view.page - 1)
        view.selected_panel_id = None
        view._build_items()
        await interaction.response.edit_message(
            embed=_build_manage_embed(view.guild, view.panels, view.selected_panel_id),
            view=view,
        )


class ManageNextPageButton(discord.ui.Button):
    def __init__(self, *, disabled: bool):
        super().__init__(label="Next", style=discord.ButtonStyle.secondary, disabled=disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ManagePanelsView) or not await view.ensure_owner(interaction):
            return
        view.page = min(view.page_count - 1, view.page + 1)
        view.selected_panel_id = None
        view._build_items()
        await interaction.response.edit_message(
            embed=_build_manage_embed(view.guild, view.panels, view.selected_panel_id),
            view=view,
        )


class ResendPanelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Send Panel Again", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ManagePanelsView):
            return
        if not await view.ensure_owner(interaction):
            return

        panel = view._get_selected_panel()
        if panel is None:
            await interaction.response.send_message("No panel selected.", ephemeral=True)
            return

        panel_channel = (
            view.guild.get_channel(int(panel.get("panel_destination_channel_id", 0) or 0))
            or view.guild.get_channel(int(panel.get("panel_channel_id", 0) or 0))
            or interaction.channel
        )
        if not isinstance(panel_channel, discord.TextChannel):
            await interaction.response.send_message(
                "The configured panel destination channel was not found.",
                ephemeral=True,
            )
            return
        candidate_panel = dict(panel)
        candidate_panel["previous_panel_channel_id"] = int(panel.get("panel_channel_id") or 0)
        candidate_panel["previous_panel_message_id"] = int(panel.get("panel_message_id") or 0)
        candidate_panel["panel_channel_id"] = 0
        candidate_panel["panel_message_id"] = 0
        await interaction.response.defer()
        try:
            panel_message, publish_operation_id = await _post_pending_ticket_panel(
                view.bot,
                view.guild,
                panel_channel,
                candidate_panel,
                operation="resend",
            )
        except _PanelPublishError as error:
            await _send_ephemeral_notice(interaction, str(error))
            return

        if not await view.ensure_owner(interaction):
            await _abort_panel_publish(
                view.guild.id,
                publish_operation_id,
                candidate_panel,
                panel_message,
                operation="resend",
            )
            return
        fresh_panel = _get_active_panel_by_id(
            view.guild.id,
            str(panel.get("id")),
        )
        if fresh_panel is None:
            await _abort_panel_publish(
                view.guild.id,
                publish_operation_id,
                candidate_panel,
                panel_message,
                operation="resend",
            )
            await _send_ephemeral_notice(
                interaction,
                "This panel was removed or disabled while it was being sent.",
            )
            return
        fresh_panel["panel_message_id"] = candidate_panel["panel_message_id"]
        fresh_panel["panel_channel_id"] = candidate_panel["panel_channel_id"]
        try:
            _save_panel(view.guild.id, fresh_panel)
        except Exception:
            await _abort_panel_publish(
                view.guild.id,
                publish_operation_id,
                candidate_panel,
                panel_message,
                operation="resend",
            )
            await _send_ephemeral_notice(
                interaction,
                "The panel could not be saved locally, so its new Discord message was disabled.",
            )
            return
        old_message_disabled = await _disable_previous_ticket_panel_message(
            view.guild,
            candidate_panel,
        )
        if old_message_disabled:
            _finish_panel_publish(view.guild.id, publish_operation_id)
        else:
            _record_panel_publish(
                view.guild.id,
                publish_operation_id,
                candidate_panel,
                operation="resend",
                status="old_cleanup_pending",
            )
        view.panels = _list_panels(view.guild.id)
        await _edit_component_message(
            interaction,
            embed=_build_manage_embed(view.guild, view.panels, view.selected_panel_id),
            view=view,
        )


class DeletePanelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Delete Panel", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ManagePanelsView):
            return
        if not await view.ensure_owner(interaction):
            return

        panel = view._get_selected_panel()
        if panel is None:
            await interaction.response.send_message("No panel selected.", ephemeral=True)
            return

        await interaction.response.defer()
        channel_id = int(panel.get("panel_channel_id", 0) or 0)
        message_id = int(panel.get("panel_message_id", 0) or 0)
        target_channel = view.guild.get_channel(channel_id)
        if isinstance(target_channel, discord.TextChannel) and message_id:
            try:
                target_message = await target_channel.fetch_message(message_id)
                if not await view.ensure_owner(interaction):
                    return
                await target_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        if not await view.ensure_owner(interaction):
            return
        if _get_active_panel_by_id(view.guild.id, str(panel.get("id"))) is None:
            await _send_ephemeral_notice(
                interaction,
                "This panel has already been removed or disabled.",
            )
            return

        _delete_panel(view.guild.id, str(panel.get("id")))
        view.panels = _list_panels(view.guild.id)
        view.selected_panel_id = str(view.panels[0].get("id")) if view.panels else None
        await _edit_component_message(
            interaction,
            embed=_build_manage_embed(view.guild, view.panels, view.selected_panel_id),
            view=view,
        )


class ManageBackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ManagePanelsView):
            return
        if not await view.ensure_owner(interaction):
            return
        await interaction.response.edit_message(
            embed=_build_home_embed(view.guild),
            view=TicketsSetupHomeView(view.bot, view.user_id, view.guild),
        )


class CreatePanelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Create Panel", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, TicketsSetupHomeView):
            return
        if not await view.ensure_owner(interaction):
            return

        setup_channel = interaction.channel
        if not isinstance(setup_channel, discord.abc.Messageable):
            await interaction.response.send_message(
                "Ticket setup must be opened from a messageable server channel.",
                ephemeral=True,
            )
            return
        setup_view = TicketPanelSetupView(
            view.bot,
            view.guild,
            view.user_id,
            setup_channel,
        )
        setup_view.host_message = interaction.message
        await interaction.response.edit_message(
            embed=_build_setup_embed(setup_view), view=setup_view
        )


class OpenManagePanelsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Manage Panels", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, TicketsSetupHomeView):
            return
        if not await view.ensure_owner(interaction):
            return

        panels = _list_panels(view.guild.id)
        manage_view = ManagePanelsView(view.bot, view.guild, view.user_id, panels)
        await interaction.response.edit_message(
            embed=_build_manage_embed(view.guild, panels, manage_view.selected_panel_id),
            view=manage_view,
        )


class TicketsSetupHomeView(discord.ui.View):
    def __init__(self, bot, user_id: int, guild: discord.Guild):
        super().__init__(timeout=900)
        self.bot = bot
        self.user_id = user_id
        self.guild = guild
        self.add_item(CreatePanelButton())
        self.add_item(OpenManagePanelsButton())

    async def ensure_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await _send_ephemeral_notice(
                interaction, "Only the admin who opened ticket setup can use these controls."
            )
            return False
        if (
            interaction.guild is None
            or interaction.guild.id != self.guild.id
            or not isinstance(interaction.user, discord.Member)
            or not await authorization.is_admin(interaction.user)
        ):
            await _send_ephemeral_notice(
                interaction,
                "Your Administrator permission was removed; ticket setup can no longer be used.",
            )
            return False
        return True


class TicketOpenView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Open Ticket", style=discord.ButtonStyle.success, custom_id="tickets:open"
    )
    async def open_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This button can only be used inside a server.", ephemeral=True
            )
            return
        if not guild_settings.get_target_guild(interaction.guild.id):
            await interaction.response.send_message(
                "This ticket panel is no longer active.",
                ephemeral=True,
            )
            return
        if interaction.message is None:
            await interaction.response.send_message(
                "Ticket panel message was not found.",
                ephemeral=True,
            )
            return

        panel = _get_panel_by_message_id(interaction.guild.id, interaction.message.id)
        if panel is None:
            await interaction.response.send_message(
                "Ticket panel configuration was not found.", ephemeral=True
            )
            return
        if _get_panel_close_mode(panel) == "unconfigured":
            await interaction.response.send_message(
                "This ticket panel has no close destination configured. Ask an admin to recreate it.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            _OpenTicketNicknameModal(self.bot, panel_id=str(panel.get("id")))
        )


def _format_int(value: object) -> str:
    try:
        parsed = _parse_int(value)
    except Exception:
        return "0"
    return f"{parsed:,}" if parsed is not None else "0"


def _build_general_info_embed(search_profile: dict, pve_total: int) -> discord.Embed:
    nickname = search_profile.get("Name") or "Unknown"
    guild_name = search_profile.get("GuildName") or "(no guild)"
    kill_fame = search_profile.get("KillFame") or 0
    death_fame = search_profile.get("DeathFame") or 0
    fame_ratio = search_profile.get("FameRatio")

    embed = discord.Embed(title="General Info")
    embed.add_field(name="Nickname", value=str(nickname), inline=False)
    embed.add_field(name="Current guild", value=str(guild_name), inline=False)
    embed.add_field(name="Kill Fame", value=_format_int(kill_fame), inline=True)
    embed.add_field(name="Death Fame", value=_format_int(death_fame), inline=True)
    embed.add_field(
        name="Fame Ratio", value=str(fame_ratio if fame_ratio is not None else "0"), inline=True
    )
    embed.add_field(name="PvE Fame", value=_format_int(pve_total), inline=True)
    return embed


def _ticket_creation_marker(operation_id: str, part: str) -> str:
    """Return the stable key used by hidden checkpoints and legacy cleanup."""

    return f"{_LEGACY_TICKET_CREATION_MARKER_PREFIX}:{operation_id}:{part}"


def _ticket_creation_nonce(operation_id: str, part: str) -> int:
    """Build stable Discord metadata without adding user-visible ticket text."""

    digest = hashlib.blake2b(
        f"realm-protector:ticket:{operation_id}:{part}".encode("utf-8"),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big")


def _persist_ticket_creation(
    guild_id: int,
    operation_id: str,
    payload: dict,
    *,
    status: str,
) -> runtime_state.RuntimeRecord:
    return runtime_state.upsert_record(
        _TICKET_CREATION_RUNTIME_KIND,
        guild_id,
        operation_id,
        payload,
        status=status,
    )


async def _find_ticket_creation_message(
    channel: _TicketCreationMessageChannel,
    operation_id: str,
    part: str,
    message_id: Optional[int],
    *,
    bot_user_id: int = 0,
) -> Optional[discord.Message]:
    if not bot_user_id:
        bot_user_id = int(
            getattr(
                getattr(getattr(channel, "guild", None), "me", None),
                "id",
                0,
            )
            or 0
        )
    if message_id:
        try:
            message = await channel.fetch_message(int(message_id))
        except discord.NotFound:
            message = None
        if message is not None and _message_is_bot_authored(message, bot_user_id):
            return message
    marker = _ticket_creation_marker(operation_id, part)
    nonce = _ticket_creation_nonce(operation_id, part)
    async for message in channel.history(limit=_FALLBACK_HISTORY_LIMIT):
        if not _message_is_bot_authored(message, bot_user_id):
            continue
        if str(getattr(message, "nonce", "")) == str(
            nonce
        ) or message_checkpoints.message_has_checkpoint(message, marker, nonce=nonce):
            return message
    return None


async def _remove_legacy_ticket_creation_footer(
    message: discord.Message,
    operation_id: str,
    part: str,
) -> None:
    """Remove hidden and legacy markers without changing visible ticket data."""

    marker = _ticket_creation_marker(operation_id, part)
    await message_checkpoints.clean_message_checkpoint(
        message,
        marker,
        nonce=_ticket_creation_nonce(operation_id, part),
    )


async def _clean_ticket_creation_message_checkpoints(
    channel: _TicketCreationMessageChannel,
    operation_id: str,
    payload: dict,
) -> bool:
    """Remove every discoverable old footer from one open ticket."""

    for part, payload_key in (
        ("control", "control_message_id"),
        ("stats", "stats_message_id"),
    ):
        parsed_message_id = _parse_int(payload.get(payload_key))
        message_id = parsed_message_id if parsed_message_id and parsed_message_id > 0 else None
        message = await _find_ticket_creation_message(
            channel,
            operation_id,
            part,
            message_id,
        )
        if message is None:
            continue
        await _remove_legacy_ticket_creation_footer(message, operation_id, part)
    return True


async def _find_ticket_creation_channel(
    guild: discord.Guild,
    operation_id: str,
) -> Optional[discord.TextChannel]:
    marker = _ticket_creation_marker(operation_id, "channel")
    hidden_marker = message_checkpoints.hidden_checkpoint(
        marker,
        nonce=_ticket_creation_nonce(operation_id, "channel"),
    )
    for channel in getattr(guild, "text_channels", ()):
        topic = str(getattr(channel, "topic", "") or "")
        metadata = _parse_ticket_topic(topic)
        if (
            unquote(str(metadata.get("creation_id") or "")) == operation_id
            or hidden_marker in topic
        ):
            return channel
    return None


async def _remove_ticket_creation_topic_checkpoint(
    channel: discord.TextChannel,
    operation_id: str,
) -> bool:
    """Scrub the transient channel-creation identity after its ID is durable."""

    original_topic = str(getattr(channel, "topic", "") or "")
    marker = _ticket_creation_marker(operation_id, "channel")
    hidden_marker = message_checkpoints.hidden_checkpoint(
        marker,
        nonce=_ticket_creation_nonce(operation_id, "channel"),
    )
    retained_parts = []
    for part in original_topic.split(";"):
        key, separator, value = part.partition("=")
        if separator and key.strip() == "creation_id" and unquote(value.strip()) == operation_id:
            continue
        cleaned_part = part.replace(hidden_marker, "")
        if cleaned_part:
            retained_parts.append(cleaned_part)
    cleaned_topic = ";".join(retained_parts)
    if cleaned_topic == original_topic:
        return False
    await channel.edit(
        topic=cleaned_topic,
        reason="Finalize ticket channel metadata",
    )
    return True


async def _create_ticket_channel_from_intent(
    guild: discord.Guild,
    payload: dict,
) -> Optional[discord.TextChannel]:
    panel_id = str(payload.get("panel_id") or "")
    panel = _get_active_panel_by_id(guild.id, panel_id)
    if panel is None:
        return None
    category = guild.get_channel(int(payload.get("category_id") or 0))
    if not isinstance(category, discord.CategoryChannel):
        return None
    opener_id = int(payload.get("opener_id") or 0)
    try:
        opener = guild.get_member(opener_id) or await guild.fetch_member(opener_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
        return None
    bot_member: Optional[discord.Member] = getattr(guild, "me", None)
    if bot_member is None:
        return None
    management_roles, management_error = _resolve_management_roles(
        guild,
        payload.get("management_role_ids") or [],
    )
    if management_error:
        return None
    overwrites: dict[
        discord.Role | discord.Member | discord.Object,
        discord.PermissionOverwrite,
    ] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        opener: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        ),
        bot_member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            manage_messages=True,
            read_message_history=True,
        ),
    }
    for role in management_roles:
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        )
    return await guild.create_text_channel(
        name=str(payload.get("channel_name") or "open-user")[:100],
        category=category,
        overwrites=overwrites,
        topic=str(payload.get("topic") or "")[:1024],
    )


async def _complete_ticket_creation(
    bot,
    guild: discord.Guild,
    record: runtime_state.RuntimeRecord,
) -> tuple[Optional[discord.TextChannel], Optional[str]]:
    lock_key = (int(guild.id), str(record.external_id))
    async with _ticket_creation_locks.hold(lock_key):
        return await _complete_ticket_creation_locked(bot, guild, record)


async def _complete_ticket_creation_locked(
    bot,
    guild: discord.Guild,
    record: runtime_state.RuntimeRecord,
) -> tuple[Optional[discord.TextChannel], Optional[str]]:
    """Resume a ticket channel and its two crash-safe opening messages."""

    payload = dict(record.payload)
    operation_id = str(record.external_id)
    panel = _get_active_panel_by_id(guild.id, str(payload.get("panel_id") or ""))
    channel: Optional[discord.TextChannel] = None
    try:
        channel_id = int(payload.get("channel_id") or 0)
    except (TypeError, ValueError):
        channel_id = 0
    if channel_id:
        try:
            candidate_channel = await _fetch_guild_channel(guild, channel_id)
            if isinstance(candidate_channel, discord.TextChannel):
                channel = candidate_channel
        except discord.NotFound:
            channel = None
    if channel is None:
        channel = await _find_ticket_creation_channel(guild, operation_id)
    if panel is None:
        if channel is not None:
            try:
                await channel.delete(reason="Ticket panel disabled during recovery")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        runtime_state.set_status(
            _TICKET_CREATION_RUNTIME_KIND,
            guild.id,
            operation_id,
            "cancelled",
        )
        return None, "The ticket panel is no longer active."
    try:
        if channel is None:
            channel = await _create_ticket_channel_from_intent(guild, payload)
        if channel is None:
            return None, "The ticket channel could not be created yet."
        payload["channel_id"] = int(channel.id)
        record = _persist_ticket_creation(
            guild.id,
            operation_id,
            payload,
            status="channel_ready",
        )
        metadata = _parse_ticket_topic(getattr(channel, "topic", None))
        _persist_ticket(
            channel,
            metadata,
            status="creating",
            stats=dict(payload.get("character_stats") or {}),
            pve_total=int(payload.get("pve_total") or 0),
            extra={"creation_id": operation_id},
        )

        control = await _find_ticket_creation_message(
            channel,
            operation_id,
            "control",
            int(payload.get("control_message_id") or 0) or None,
        )
        if control is None:
            control_marker = _ticket_creation_marker(operation_id, "control")
            control_nonce = _ticket_creation_nonce(operation_id, "control")
            control_embed = discord.Embed(
                title=f"Ticket: {payload.get('character_nickname') or 'Unknown'}",
                description=panel.get("ticket_message") or _get_default_ticket_message(),
            )
            control_embed.add_field(
                name="Applicant",
                value=f"<@{int(payload.get('opener_id') or 0)}>",
                inline=False,
            )
            control_embed.add_field(
                name="Management team",
                value=_format_role_names(guild, panel.get("management_role_ids", [])),
                inline=False,
            )
            control = await channel.send(
                content=message_checkpoints.content_with_checkpoint(
                    f"<@{int(payload.get('opener_id') or 0)}>",
                    control_marker,
                    nonce=control_nonce,
                ),
                embed=control_embed,
                view=TicketCloseView(bot),
                nonce=control_nonce,
                allowed_mentions=allowed_user_mentions([int(payload.get("opener_id") or 0)]),
            )
        payload["control_message_id"] = int(control.id)
        record = _persist_ticket_creation(
            guild.id,
            operation_id,
            payload,
            status="control_ready",
        )

        stats_message = await _find_ticket_creation_message(
            channel,
            operation_id,
            "stats",
            int(payload.get("stats_message_id") or 0) or None,
        )
        if stats_message is None:
            stats_marker = _ticket_creation_marker(operation_id, "stats")
            stats_nonce = _ticket_creation_nonce(operation_id, "stats")
            stats_embed = _build_general_info_embed(
                dict(payload.get("character_stats") or {}),
                int(payload.get("pve_total") or 0),
            )
            stats_message = await channel.send(
                content=message_checkpoints.content_with_checkpoint(
                    None,
                    stats_marker,
                    nonce=stats_nonce,
                ),
                embed=stats_embed,
                nonce=stats_nonce,
                allowed_mentions=_NO_MENTIONS,
            )
        payload["stats_message_id"] = int(stats_message.id)
        _persist_ticket_creation(
            guild.id,
            operation_id,
            payload,
            status="messages_ready",
        )
        _persist_ticket(
            channel,
            metadata,
            status="open",
            stats=dict(payload.get("character_stats") or {}),
            pve_total=int(payload.get("pve_total") or 0),
            extra={"creation_id": operation_id},
        )
        payload["discord_checkpoints_removed"] = False
        _persist_ticket_creation(
            guild.id,
            operation_id,
            payload,
            status="completed",
        )
        try:
            await _clean_ticket_creation_message_checkpoints(
                channel,
                operation_id,
                payload,
            )
            await _remove_ticket_creation_topic_checkpoint(channel, operation_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
            LOGGER.warning(
                "Completed ticket %s still has transient checkpoint metadata",
                operation_id,
            )
        else:
            payload["visible_markers_removed"] = True
            payload["discord_checkpoints_removed"] = True
            _persist_ticket_creation(
                guild.id,
                operation_id,
                payload,
                status="completed",
            )
            _persist_ticket(
                channel,
                metadata,
                status="open",
                extra={
                    "creation_id": operation_id,
                    "creation_checkpoints_removed": True,
                },
            )
        return channel, None
    except (discord.Forbidden, discord.HTTPException, AttributeError, OSError) as error:
        LOGGER.warning(
            "Ticket creation %s remains pending in guild %s: %s",
            operation_id,
            guild.id,
            error,
        )
        return channel, "Ticket creation was interrupted and will be retried automatically."


async def _clean_completed_ticket_creation_checkpoints(
    guild: discord.Guild,
    record: runtime_state.RuntimeRecord,
) -> None:
    """Clean transient and legacy checkpoints, including on closed channels."""

    payload = dict(record.payload)
    if payload.get("discord_checkpoints_removed"):
        return
    channel_id = _parse_int(payload.get("channel_id"))
    channel = None
    if channel_id and channel_id > 0:
        try:
            channel = await _fetch_guild_channel(guild, channel_id)
        except discord.NotFound:
            channel = None
    if channel is not None:
        if isinstance(channel, discord.TextChannel):
            await _remove_ticket_creation_topic_checkpoint(
                channel,
                str(record.external_id),
            )
        if not isinstance(channel, _TicketCreationMessageChannel):
            raise AttributeError("Ticket channel cannot fetch or search its opening messages")
        await _clean_ticket_creation_message_checkpoints(
            channel,
            str(record.external_id),
            payload,
        )
    payload["visible_markers_removed"] = True
    payload["discord_checkpoints_removed"] = True
    _persist_ticket_creation(
        guild.id,
        str(record.external_id),
        payload,
        status="completed",
    )


async def _clean_active_ticket_creation_checkpoints(
    channel,
    record: runtime_state.RuntimeRecord,
) -> bool:
    """Sweep an authoritative open ticket when its creation action row is absent."""

    if record.payload.get("creation_checkpoints_removed"):
        return True
    operation_id = str(record.payload.get("creation_id") or "")
    if not operation_id:
        return True
    if isinstance(channel, discord.TextChannel):
        await _remove_ticket_creation_topic_checkpoint(channel, operation_id)
    if not isinstance(channel, _TicketCreationMessageChannel):
        return False
    await _clean_ticket_creation_message_checkpoints(
        channel,
        operation_id,
        dict(record.payload),
    )
    payload = dict(record.payload)
    payload["creation_checkpoints_removed"] = True
    runtime_state.upsert_record(
        _TICKET_RUNTIME_KIND,
        record.guild_id,
        record.external_id,
        payload,
        status=record.status,
    )
    return True


class _OpenTicketNicknameModal(discord.ui.Modal, title="Open Ticket"):
    character_nickname: discord.ui.TextInput["_OpenTicketNicknameModal"] = discord.ui.TextInput(
        label="Enter your character ingame nickname",
        required=True,
        max_length=40,
        placeholder="ING",
    )

    def __init__(self, bot, panel_id: str):
        super().__init__()
        self._bot = bot
        self._panel_id = panel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This can only be used inside a server.", ephemeral=True
            )
            return
        if _get_active_panel_by_id(interaction.guild.id, self._panel_id) is None:
            await interaction.response.send_message(
                "This ticket panel is no longer active.",
                ephemeral=True,
            )
            return

        nickname = str(self.character_nickname).strip()
        if not nickname:
            await interaction.response.send_message(
                "Please enter a character nickname.", ephemeral=True
            )
            return

        retry_after = _ticket_lookup_cooldown.claim((interaction.guild.id, interaction.user.id))
        if retry_after:
            await interaction.response.send_message(
                f"Please wait {max(1, int(retry_after) + 1)} seconds before checking another character.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        existing_channel = await _find_existing_open_ticket_channel(
            interaction.guild,
            self._panel_id,
            interaction.user.id,
        )
        if existing_channel is not None:
            await interaction.edit_original_response(
                content=f"You already have an open ticket: {existing_channel.mention}",
                embed=None,
                view=None,
            )
            return
        character_options = await albion_characters.search_character_options(nickname)
        if not character_options:
            await interaction.edit_original_response(
                content="No characters found. Please check the nickname and try again.",
                embed=None,
                view=None,
            )
            return

        if _get_active_panel_by_id(interaction.guild.id, self._panel_id) is None:
            await interaction.edit_original_response(
                content="This ticket panel was disabled while the character was being checked.",
                embed=None,
                view=None,
            )
            return

        view = _TicketCharacterSelectionView(
            self._bot,
            user_id=interaction.user.id,
            panel_id=self._panel_id,
            character_options=character_options,
        )
        await interaction.edit_original_response(
            content=None,
            embed=build_character_selection_embed(character_options),
            view=view,
        )


async def _create_confirmed_ticket(
    bot,
    interaction: discord.Interaction,
    panel: dict,
    selected_character: AlbionCharacterOption,
) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send(
            "This ticket can only be created inside a server.",
            ephemeral=True,
        )
        return
    active_panel = _get_active_panel_by_id(guild.id, str(panel.get("id")))
    if active_panel is None:
        await interaction.followup.send(
            "This ticket panel was removed or disabled.",
            ephemeral=True,
        )
        return
    panel = active_panel
    if _get_panel_close_mode(panel) == "unconfigured":
        await interaction.followup.send(
            "This ticket panel has no close destination configured. Ask an admin to recreate it.",
            ephemeral=True,
        )
        return
    category = guild.get_channel(int(panel.get("ticket_category_id", 0) or 0))
    if not isinstance(category, discord.CategoryChannel):
        await interaction.followup.send(
            "Configured ticket category was not found.",
            ephemeral=True,
        )
        return
    management_roles, management_role_error = _resolve_management_roles(
        guild,
        panel.get("management_role_ids", []),
    )
    if management_role_error:
        await interaction.followup.send(
            f"Management role configuration error: {management_role_error}",
            ephemeral=True,
        )
        return

    search_profile = selected_character.search_profile
    character_nickname = (search_profile.get("Name") or "user").strip() or "user"
    albion_id = search_profile.get("Id")
    opener_slug = _slugify_channel_component(character_nickname, max_length=95)
    ticket_name = _build_ticket_channel_name("open", opener_slug)

    operation_id = uuid4().hex
    topic = _build_ticket_topic_with_character(
        str(panel.get("id")),
        interaction.user.id,
        opener_slug,
        character_nickname,
        str(albion_id) if albion_id else None,
        creation_id=operation_id,
    )
    payload = {
        "panel_id": str(panel.get("id") or ""),
        "opener_id": int(interaction.user.id),
        "opener_slug": opener_slug,
        "character_nickname": character_nickname,
        "albion_id": str(albion_id or ""),
        "category_id": int(category.id),
        "management_role_ids": [int(role.id) for role in management_roles],
        "channel_name": ticket_name,
        "topic": topic,
        "character_stats": dict(search_profile),
        "pve_total": int(selected_character.pve_total),
    }
    try:
        record = _persist_ticket_creation(
            guild.id,
            operation_id,
            payload,
            status="pending",
        )
    except Exception:
        LOGGER.exception(
            "Could not persist ticket creation intent in guild %s",
            guild.id,
        )
        await interaction.followup.send(
            "Local storage is unavailable, so no ticket channel was created.",
            ephemeral=True,
        )
        return
    channel, warning = await _complete_ticket_creation(
        bot,
        guild,
        record,
    )
    if channel is None:
        await interaction.followup.send(
            warning
            or "The ticket could not be completed yet. Saved progress will be retried automatically.",
            ephemeral=True,
        )
        return
    message = f"Ticket created: {channel.mention}"
    if warning:
        message += f"\n{warning}"
    await interaction.followup.send(message, ephemeral=True)


class _TicketCharacterSelectionView(CharacterSelectionView):
    def __init__(
        self,
        bot,
        user_id: int,
        panel_id: str,
        character_options: list[AlbionCharacterOption],
    ):
        super().__init__(user_id, character_options)
        self._bot = bot
        self._panel_id = str(panel_id)

    async def on_character_selected(
        self,
        interaction: discord.Interaction,
        selected_character: AlbionCharacterOption,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This can only be used inside a server.",
                ephemeral=True,
            )
            return

        panel = _get_active_panel_by_id(interaction.guild.id, self._panel_id)
        if panel is None:
            await interaction.response.send_message(
                "Ticket panel configuration was not found or has been disabled.",
                ephemeral=True,
            )
            return

        category = interaction.guild.get_channel(int(panel.get("ticket_category_id", 0) or 0))
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "Configured ticket category was not found.",
                ephemeral=True,
            )
            return

        lock_key = (
            interaction.guild.id,
            str(panel.get("id")),
            interaction.user.id,
        )
        await interaction.response.defer()
        async with _ticket_open_locks.hold(lock_key):
            panel = _get_active_panel_by_id(interaction.guild.id, self._panel_id)
            if panel is None:
                await _send_ephemeral_notice(
                    interaction,
                    "Ticket panel configuration was not found or has been disabled.",
                )
                return
            category = interaction.guild.get_channel(int(panel.get("ticket_category_id", 0) or 0))
            if not isinstance(category, discord.CategoryChannel):
                await _send_ephemeral_notice(
                    interaction,
                    "Configured ticket category was not found.",
                )
                return
            existing_ticket_channel = await _find_existing_open_ticket_channel(
                interaction.guild,
                str(panel.get("id")),
                interaction.user.id,
            )
            if existing_ticket_channel is not None:
                await _send_ephemeral_notice(
                    interaction,
                    f"You already have an open ticket: {existing_ticket_channel.mention}",
                )
                return

            self.stop()
            await _edit_component_message(
                interaction,
                content="Opening ticket...",
                embed=None,
                view=None,
            )
            await _create_confirmed_ticket(
                self._bot,
                interaction,
                panel,
                selected_character,
            )


def _archive_marker(guild_id: int, source_channel_id: int, part: str) -> str:
    return f"{_ARCHIVE_MARKER_PREFIX}:{int(guild_id)}:{int(source_channel_id)}:{part}"


def _archive_nonce(marker: str) -> int:
    """Encode one archive checkpoint as non-rendered Discord metadata."""

    digest = hashlib.blake2b(
        f"realm-protector:archive:{marker}".encode("utf-8"),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big")


def _archive_nonce_key(marker: str) -> str:
    return f"nonce:{_archive_nonce(marker)}"


def _archive_hidden_checkpoint(marker: str) -> str:
    """Encode restart-safe metadata that is removed after archive completion."""

    encoded = format(_archive_nonce(marker), "064b")
    bits = "".join(_ARCHIVE_HIDDEN_ONE if bit == "1" else _ARCHIVE_HIDDEN_ZERO for bit in encoded)
    return f"{_ARCHIVE_HIDDEN_PREFIX}{bits}{_ARCHIVE_HIDDEN_SUFFIX}"


def _archive_content_with_checkpoint(content: Optional[str], marker: str) -> str:
    checkpoint = _archive_hidden_checkpoint(marker)
    visible_content = str(content or "")
    if len(visible_content) > _ARCHIVE_VISIBLE_CONTENT_LIMIT:
        raise ValueError("archive content must be chunked before adding its checkpoint")
    return f"{visible_content}{checkpoint}"


def _archive_content_chunks(content: Optional[str]) -> list[str]:
    value = str(content or "")
    if not value:
        return [""]
    return [
        value[offset : offset + _ARCHIVE_VISIBLE_CONTENT_LIMIT]
        for offset in range(0, len(value), _ARCHIVE_VISIBLE_CONTENT_LIMIT)
    ]


def _hidden_archive_checkpoint_keys(content: object) -> set[str]:
    value = str(content or "")
    keys: set[str] = set()
    offset = 0
    while True:
        prefix_index = value.find(_ARCHIVE_HIDDEN_PREFIX, offset)
        if prefix_index < 0:
            break
        body_start = prefix_index + len(_ARCHIVE_HIDDEN_PREFIX)
        suffix_index = value.find(_ARCHIVE_HIDDEN_SUFFIX, body_start)
        if suffix_index < 0:
            break
        body = value[body_start:suffix_index]
        if len(body) == 64 and all(
            bit in {_ARCHIVE_HIDDEN_ZERO, _ARCHIVE_HIDDEN_ONE} for bit in body
        ):
            binary = "".join("1" if bit == _ARCHIVE_HIDDEN_ONE else "0" for bit in body)
            keys.add(f"nonce:{int(binary, 2)}")
        offset = suffix_index + len(_ARCHIVE_HIDDEN_SUFFIX)
    return keys


def _strip_hidden_archive_checkpoints(content: object) -> str:
    """Remove every well-formed archive token after durable completion."""

    value = str(content or "")
    retained: list[str] = []
    offset = 0
    while True:
        prefix_index = value.find(_ARCHIVE_HIDDEN_PREFIX, offset)
        if prefix_index < 0:
            retained.append(value[offset:])
            break
        retained.append(value[offset:prefix_index])
        body_start = prefix_index + len(_ARCHIVE_HIDDEN_PREFIX)
        suffix_index = value.find(_ARCHIVE_HIDDEN_SUFFIX, body_start)
        if suffix_index < 0:
            retained.append(value[prefix_index:])
            break
        body = value[body_start:suffix_index]
        if len(body) in {63, 64} and all(
            bit in {_ARCHIVE_HIDDEN_ZERO, _ARCHIVE_HIDDEN_ONE} for bit in body
        ):
            offset = suffix_index + len(_ARCHIVE_HIDDEN_SUFFIX)
            continue
        retained.append(value[prefix_index:body_start])
        offset = body_start
    return "".join(retained)


def _archive_checkpoint_exists(marker: str, checkpoints: set[str]) -> bool:
    return marker in checkpoints or _archive_nonce_key(marker) in checkpoints


def _message_archive_markers(message) -> set[str]:
    markers: set[str] = set()
    for embed in getattr(message, "embeds", ()) or ():
        footer = getattr(embed, "footer", None)
        text = str(getattr(footer, "text", "") or "")
        if text.startswith(f"{_ARCHIVE_MARKER_PREFIX}:"):
            markers.add(text)
    return markers


def _message_archive_checkpoints(message) -> set[str]:
    checkpoints = _message_archive_markers(message)
    checkpoints.update(_hidden_archive_checkpoint_keys(getattr(message, "content", "")))
    nonce = getattr(message, "nonce", None)
    if nonce is not None:
        checkpoints.add(f"nonce:{nonce}")
    return checkpoints


async def _clean_archive_message_checkpoints(message: discord.Message) -> bool:
    """Remove all completed checkpoint artifacts while preserving the transcript."""

    retained_embeds: list[discord.Embed] = []
    embeds_changed = False
    for original in message.embeds:
        footer = getattr(original, "footer", None)
        footer_text = str(getattr(footer, "text", "") or "")
        if footer_text.startswith(
            (
                f"{_ARCHIVE_MARKER_PREFIX}:",
                f"{_LEGACY_TICKET_CREATION_MARKER_PREFIX}:",
            )
        ):
            embeds_changed = True
            cleaned_embed = original.copy()
            cleaned_embed.remove_footer()
            embed_data = cleaned_embed.to_dict()
            embed_data.pop("type", None)
            if not embed_data.get("flags"):
                embed_data.pop("flags", None)
            if not embed_data:
                continue
            retained_embeds.append(cleaned_embed)
        else:
            retained_embeds.append(original.copy())
    original_content = str(getattr(message, "content", "") or "")
    cleaned_content = _strip_hidden_archive_checkpoints(original_content)
    content_changed = cleaned_content != original_content
    if content_changed and embeds_changed:
        await message.edit(
            content=cleaned_content or None,
            embeds=retained_embeds,
            allowed_mentions=_NO_MENTIONS,
        )
    elif content_changed:
        await message.edit(
            content=cleaned_content or None,
            allowed_mentions=_NO_MENTIONS,
        )
    elif embeds_changed:
        await message.edit(embeds=retained_embeds)
    return content_changed or embeds_changed


async def _clean_completed_archive_checkpoints(
    archive_message: Optional[discord.Message],
    thread: Optional[discord.Thread],
) -> bool:
    """Strip transient and legacy checkpoints after transcript completion."""

    cleanup_succeeded = True
    if archive_message is not None:
        try:
            await _clean_archive_message_checkpoints(archive_message)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            cleanup_succeeded = False
    if thread is not None:
        async for message in thread.history(limit=None, oldest_first=False):
            try:
                await _clean_archive_message_checkpoints(message)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                cleanup_succeeded = False
    return cleanup_succeeded


async def _clean_completed_archive_for_source(
    source_channel,
    metadata: dict[str, str],
    archive_message: Optional[discord.Message],
    thread: Optional[discord.Thread],
) -> bool:
    """Remove legacy panels after completion and remember the migration."""

    try:
        cleanup_succeeded = await _clean_completed_archive_checkpoints(
            archive_message,
            thread,
        )
    except discord.NotFound:
        # A deleted archive resource cannot contain a visible legacy panel.
        cleanup_succeeded = True
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        cleanup_succeeded = False
    if not cleanup_succeeded:
        LOGGER.warning(
            "Legacy archive checkpoints still need cleanup for ticket %s in guild %s",
            source_channel.id,
            source_channel.guild.id,
        )
        return False
    try:
        _persist_ticket(
            source_channel,
            metadata,
            status="archived_source_remaining",
            extra={
                "archive_checkpoint_embeds_removed": True,
                "archive_checkpoints_removed": True,
                "archive_checkpoint_cleanup_version": _ARCHIVE_CHECKPOINT_CLEANUP_VERSION,
            },
        )
    except Exception:
        LOGGER.exception(
            "Could not persist archive checkpoint cleanup for ticket %s in guild %s",
            source_channel.id,
            source_channel.guild.id,
        )
        return False
    return True


def _metadata_from_ticket_record(record: runtime_state.RuntimeRecord) -> dict[str, str]:
    return {
        key: str(record.payload.get(key) or "")
        for key in (
            "panel_id",
            "opener_id",
            "opener_slug",
            "character",
            "albion_id",
        )
    }


async def _fetch_guild_channel(
    guild: discord.Guild,
    channel_id: int,
) -> discord.abc.GuildChannel | discord.Thread:
    channel = guild.get_channel(channel_id)
    if channel is not None:
        return channel
    get_thread = getattr(guild, "get_thread", None)
    if get_thread is not None:
        thread = get_thread(channel_id)
        if thread is not None:
            return thread
    return await guild.fetch_channel(channel_id)


async def _find_archive_anchor(
    archive_channel,
    *,
    guild_id: int,
    source_channel_id: int,
    archive_message_id: Optional[int],
    bot_user_id: int,
):
    if archive_message_id:
        try:
            message = await archive_channel.fetch_message(archive_message_id)
        except discord.NotFound:
            message = None
        if message is not None and _message_is_bot_authored(message, bot_user_id):
            return message

    anchor_marker = _archive_marker(guild_id, source_channel_id, "anchor")
    async for message in archive_channel.history(
        limit=_FALLBACK_HISTORY_LIMIT,
        oldest_first=False,
    ):
        if not _message_is_bot_authored(message, bot_user_id):
            continue
        if _archive_checkpoint_exists(
            anchor_marker,
            _message_archive_checkpoints(message),
        ):
            return message
    return None


async def _resolve_archive_thread(guild, archive_message, thread_id: Optional[int]):
    thread = getattr(archive_message, "thread", None)
    if isinstance(thread, discord.Thread):
        return thread
    possible_ids = []
    if thread_id:
        possible_ids.append(int(thread_id))
    # A thread created from a message normally shares the starter message ID.
    message_id = int(getattr(archive_message, "id", 0) or 0)
    if message_id and message_id not in possible_ids:
        possible_ids.append(message_id)
    for possible_id in possible_ids:
        try:
            candidate = await _fetch_guild_channel(guild, possible_id)
        except discord.NotFound:
            continue
        if isinstance(candidate, discord.Thread):
            return candidate
    return None


async def _ensure_archive_container(
    source_channel,
    metadata: dict[str, str],
    archive_channel,
    *,
    bot_user_id: int,
):
    """Resolve or create one deterministic archive anchor and its thread."""

    guild = source_channel.guild
    record = runtime_state.get_record(
        _TICKET_RUNTIME_KIND,
        guild.id,
        source_channel.id,
    )
    payload = record.payload if record is not None else {}
    archive_message_id = payload.get("archive_message_id")
    archive_thread_id = payload.get("archive_thread_id")
    try:
        parsed_archive_message_id = int(archive_message_id) if archive_message_id else None
    except (TypeError, ValueError):
        parsed_archive_message_id = None
    try:
        parsed_archive_thread_id = int(archive_thread_id) if archive_thread_id else None
    except (TypeError, ValueError):
        parsed_archive_thread_id = None

    archive_message = await _find_archive_anchor(
        archive_channel,
        guild_id=guild.id,
        source_channel_id=source_channel.id,
        archive_message_id=parsed_archive_message_id,
        bot_user_id=bot_user_id,
    )
    character_nickname = _get_ticket_character_nickname(metadata) or "unknown"
    if archive_message is None:
        anchor_marker = _archive_marker(guild.id, source_channel.id, "anchor")
        archive_message = await archive_channel.send(
            content=_archive_content_with_checkpoint(
                str(character_nickname),
                anchor_marker,
            ),
            nonce=_archive_nonce(anchor_marker),
            allowed_mentions=_NO_MENTIONS,
        )

    _persist_ticket(
        source_channel,
        metadata,
        status="closing",
        extra={
            "archive_channel_id": int(archive_channel.id),
            "archive_message_id": int(archive_message.id),
        },
    )
    thread = await _resolve_archive_thread(
        guild,
        archive_message,
        parsed_archive_thread_id,
    )
    if thread is None:
        thread = await archive_message.create_thread(
            name=(character_nickname or "unknown").strip()[:100] or "unknown",
            auto_archive_duration=10080,
        )

    _persist_ticket(
        source_channel,
        metadata,
        status="archiving",
        extra={
            "archive_channel_id": int(archive_channel.id),
            "archive_message_id": int(archive_message.id),
            "archive_thread_id": int(thread.id),
        },
    )
    return archive_message, thread


async def _read_archive_thread_state(
    thread,
    *,
    guild_id: int,
    source_channel_id: int,
) -> tuple[set[str], Counter[str]]:
    marker_root = f"{_ARCHIVE_MARKER_PREFIX}:{guild_id}:{source_channel_id}:"
    markers: set[str] = set()
    legacy_contents: Counter[str] = Counter()
    async for message in thread.history(
        limit=_ARCHIVE_STATE_HISTORY_LIMIT,
        oldest_first=False,
    ):
        message_markers = {
            marker for marker in _message_archive_markers(message) if marker.startswith(marker_root)
        }
        checkpoints = _message_archive_checkpoints(message)
        markers.update(message_markers)
        markers.update(checkpoint for checkpoint in checkpoints if checkpoint.startswith("nonce:"))
        has_hidden_or_nonce_checkpoint = any(
            checkpoint.startswith("nonce:") for checkpoint in checkpoints
        )
        if not message_markers and not has_hidden_or_nonce_checkpoint:
            content = str(getattr(message, "content", "") or "")
            if content:
                legacy_contents[content] += 1
    return markers, legacy_contents


async def _send_archive_piece_once(
    thread,
    content: str,
    marker: str,
    markers: set[str],
    legacy_contents: Counter[str],
) -> None:
    if _archive_checkpoint_exists(marker, markers):
        # Older releases used the base marker even when the checkpoint forced
        # visible content truncation. Recover any missing overflow once.
        chunks = _archive_content_chunks(content)
        for chunk_index, chunk in enumerate(chunks[1:], start=1):
            await _send_archive_piece_once(
                thread,
                chunk,
                f"{marker}:chunk:{chunk_index}",
                markers,
                legacy_contents,
            )
        return
    if legacy_contents[content] > 0:
        legacy_contents[content] -= 1
        markers.add(marker)
        return
    chunks = _archive_content_chunks(content)
    if len(chunks) > 1:
        for chunk_index, chunk in enumerate(chunks):
            await _send_archive_piece_once(
                thread,
                chunk,
                f"{marker}:chunk:{chunk_index}",
                markers,
                legacy_contents,
            )
        return
    # Archives interrupted before markers were introduced can still resume
    # without repeating byte-identical transcript chunks.
    await thread.send(
        _archive_content_with_checkpoint(content, marker),
        nonce=_archive_nonce(marker),
        allowed_mentions=_NO_MENTIONS,
    )
    markers.add(_archive_nonce_key(marker))


async def _freeze_ticket_source(
    source_channel,
    metadata: dict[str, str],
) -> bool:
    """Atomically make the source read-only before taking the transcript snapshot."""

    guild = source_channel.guild
    bot_member = getattr(guild, "me", None)
    if bot_member is None:
        return False
    overwrites = dict(getattr(source_channel, "overwrites", {}) or {})
    targets: list[object] = [guild.default_role]
    opener_id = str(metadata.get("opener_id") or "")
    if opener_id.isdigit():
        opener = guild.get_member(int(opener_id))
        if opener is not None:
            targets.append(opener)
    panel = _get_panel_by_id(guild.id, str(metadata.get("panel_id") or ""))
    if panel is not None:
        for role_id in panel.get("management_role_ids", []) or []:
            role = guild.get_role(int(role_id))
            if role is not None:
                targets.append(role)
    try:
        for target in targets:
            overwrite = overwrites.get(target)
            if overwrite is None:
                overwrite = source_channel.overwrites_for(target)
            overwrite.update(
                send_messages=False,
                add_reactions=False,
                create_public_threads=False,
                create_private_threads=False,
                send_messages_in_threads=False,
            )
            overwrites[target] = overwrite
        bot_overwrite = overwrites.get(bot_member)
        if bot_overwrite is None:
            bot_overwrite = source_channel.overwrites_for(bot_member)
        bot_overwrite.update(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
        )
        overwrites[bot_member] = bot_overwrite
        current_slug = str(metadata.get("opener_slug") or "ticket")
        await source_channel.edit(
            name=_build_ticket_channel_name("archiving", current_slug),
            overwrites=overwrites,
            reason="Freeze ticket before transcript archive",
        )
    except (discord.Forbidden, discord.HTTPException, AttributeError, TypeError):
        return False
    return True


async def _send_archive_artifact_once(
    thread,
    marker: str,
    markers: set[str],
    *,
    content: Optional[str] = None,
    embeds: Optional[list[discord.Embed]] = None,
    file=None,
) -> None:
    if _archive_checkpoint_exists(marker, markers):
        # Complete overflow that an older base-marker message could not retain.
        chunks = _archive_content_chunks(content)
        for chunk_index, chunk in enumerate(chunks[1:], start=1):
            await _send_archive_artifact_once(
                thread,
                f"{marker}:chunk:{chunk_index}",
                markers,
                content=chunk,
            )
        return
    chunks = _archive_content_chunks(content)
    if len(chunks) > 1:
        for chunk_index, chunk in enumerate(chunks):
            await _send_archive_artifact_once(
                thread,
                f"{marker}:chunk:{chunk_index}",
                markers,
                content=chunk,
                embeds=embeds if chunk_index == 0 else None,
                file=file if chunk_index == 0 else None,
            )
        return
    kwargs: dict[str, Any] = {
        "content": _archive_content_with_checkpoint(content, marker),
        "nonce": _archive_nonce(marker),
        "allowed_mentions": _NO_MENTIONS,
    }
    if embeds:
        kwargs["embeds"] = list(embeds)
    if file is not None:
        kwargs["file"] = file
    await thread.send(**kwargs)
    markers.add(_archive_nonce_key(marker))


def _sticker_archive_description(sticker: object) -> str:
    name = str(getattr(sticker, "name", "") or "Sticker")
    url = str(getattr(sticker, "url", "") or "")
    return f"Sticker: {name}" + (f"\n{url}" if url else "")


def _reaction_archive_description(message: object) -> str:
    parts = []
    for reaction in getattr(message, "reactions", ()) or ():
        count = int(getattr(reaction, "count", 0) or 0)
        parts.append(f"{getattr(reaction, 'emoji', '')} x {count}")
    return "Reactions: " + ", ".join(parts) if parts else ""


def _poll_archive_description(message: object) -> str:
    poll = getattr(message, "poll", None)
    if poll is None:
        return ""
    question = getattr(getattr(poll, "question", None), "text", None)
    if not question:
        question = str(getattr(poll, "question", "") or "Poll")
    answers = []
    for answer in getattr(poll, "answers", ()) or ():
        text = getattr(getattr(answer, "media", None), "text", None)
        answer_text = str(text or getattr(answer, "text", "") or "Answer")
        vote_count = int(getattr(answer, "vote_count", 0) or 0)
        answers.append(f"{answer_text} ({vote_count} vote{'s' if vote_count != 1 else ''})")
    return (
        "Poll: " + str(question) + ("\n" + "\n".join(f"- {a}" for a in answers) if answers else "")
    )


async def _copy_ticket_to_archive_once(
    source_channel,
    thread,
    metadata: dict[str, str],
    *,
    actor_label: str,
) -> None:
    guild_id = int(source_channel.guild.id)
    source_channel_id = int(source_channel.id)
    markers, legacy_contents = await _read_archive_thread_state(
        thread,
        guild_id=guild_id,
        source_channel_id=source_channel_id,
    )
    complete_marker = _archive_marker(guild_id, source_channel_id, "complete")
    legacy_complete = "Archive complete. Deleting ticket channel."
    if _archive_checkpoint_exists(complete_marker, markers) or legacy_contents[legacy_complete] > 0:
        # Deletion is allowed only after the durable state reflects that the
        # transcript has completed, including recovery from a crash between the
        # completion message and the SQLite update.
        _persist_ticket(
            source_channel,
            metadata,
            status="archived_source_remaining",
        )
        return

    intro = f"Archived from {source_channel.mention} by {actor_label}."
    intro_marker = _archive_marker(guild_id, source_channel_id, "intro")
    if not _archive_checkpoint_exists(intro_marker, markers):
        legacy_intro_prefix = f"Archived from {source_channel.mention} by "
        for legacy_content, count in tuple(legacy_contents.items()):
            if count > 0 and legacy_content.startswith(legacy_intro_prefix):
                legacy_contents[legacy_content] -= 1
                markers.add(intro_marker)
                break
    await _send_archive_piece_once(
        thread,
        intro,
        intro_marker,
        markers,
        legacy_contents,
    )
    record = runtime_state.get_record(
        _TICKET_RUNTIME_KIND,
        guild_id,
        source_channel_id,
    )
    checkpoint_id = 0
    if record is not None:
        try:
            checkpoint_id = int(record.payload.get("archive_checkpoint_message_id") or 0)
        except (TypeError, ValueError):
            checkpoint_id = 0
    after = discord.Object(id=checkpoint_id) if checkpoint_id else None
    async for message in source_channel.history(
        limit=None,
        oldest_first=True,
        after=after,
    ):
        author = getattr(message.author, "display_name", None) or str(message.author)
        timestamp = message.created_at.strftime("%Y-%m-%d %H:%M UTC") if message.created_at else ""
        lines = [f"[{timestamp}] {author}:"]
        if (message.content or "").strip():
            lines.append(message.content)
        payload = "\n".join(lines).strip()
        chunks = [payload[index : index + 1900] for index in range(0, len(payload), 1900)]
        for chunk_index, chunk in enumerate(chunks):
            await _send_archive_piece_once(
                thread,
                chunk,
                _archive_marker(
                    guild_id,
                    source_channel_id,
                    f"message:{int(message.id)}:{chunk_index}",
                ),
                markers,
                legacy_contents,
            )
            await asyncio.sleep(0.2)

        for embed_index, original_embed in enumerate(getattr(message, "embeds", ()) or ()):
            try:
                archived_embed = discord.Embed.from_dict(original_embed.to_dict())
            except (AttributeError, TypeError, ValueError):
                continue
            await _send_archive_artifact_once(
                thread,
                _archive_marker(
                    guild_id,
                    source_channel_id,
                    f"message:{int(message.id)}:embed:{embed_index}",
                ),
                markers,
                embeds=[archived_embed],
            )

        for attachment_index, attachment in enumerate(getattr(message, "attachments", ()) or ()):
            marker = _archive_marker(
                guild_id,
                source_channel_id,
                f"message:{int(message.id)}:attachment:{attachment_index}",
            )
            if _archive_checkpoint_exists(marker, markers):
                continue
            filename = str(getattr(attachment, "filename", "") or "attachment")
            url = str(getattr(attachment, "url", "") or "")
            try:
                attachment_file = await attachment.to_file(use_cached=True)
            except (AttributeError, discord.HTTPException, OSError):
                attachment_file = None
            attachment_description = f"Attachment: {filename}" + (f"\n{url}" if url else "")
            try:
                await _send_archive_artifact_once(
                    thread,
                    marker,
                    markers,
                    content=attachment_description,
                    file=attachment_file,
                )
            except discord.HTTPException:
                # Oversized or otherwise non-uploadable files must not make the
                # entire archive permanently unrecoverable. Preserve the filename
                # and original CDN URL as the durable fallback artifact.
                await _send_archive_artifact_once(
                    thread,
                    marker,
                    markers,
                    content=attachment_description,
                )

        for sticker_index, sticker in enumerate(getattr(message, "stickers", ()) or ()):
            await _send_archive_artifact_once(
                thread,
                _archive_marker(
                    guild_id,
                    source_channel_id,
                    f"message:{int(message.id)}:sticker:{sticker_index}",
                ),
                markers,
                content=_sticker_archive_description(sticker),
            )

        reaction_description = _reaction_archive_description(message)
        if reaction_description:
            await _send_archive_artifact_once(
                thread,
                _archive_marker(
                    guild_id,
                    source_channel_id,
                    f"message:{int(message.id)}:reactions",
                ),
                markers,
                content=reaction_description,
            )
        poll_description = _poll_archive_description(message)
        if poll_description:
            await _send_archive_artifact_once(
                thread,
                _archive_marker(
                    guild_id,
                    source_channel_id,
                    f"message:{int(message.id)}:poll",
                ),
                markers,
                content=poll_description[:2000],
            )
        _persist_ticket(
            source_channel,
            metadata,
            status="archiving",
            extra={"archive_checkpoint_message_id": int(message.id)},
        )

    await _send_archive_piece_once(
        thread,
        legacy_complete,
        complete_marker,
        markers,
        legacy_contents,
    )
    _persist_ticket(
        source_channel,
        metadata,
        status="archived_source_remaining",
    )


async def _delete_archived_ticket_source(
    source_channel,
    metadata: dict[str, str],
    thread=None,
) -> bool:
    try:
        await source_channel.delete(reason="Ticket archived")
    except discord.NotFound:
        _persist_ticket(source_channel, metadata, status="closed")
        return True
    except (discord.Forbidden, discord.HTTPException):
        _persist_ticket(
            source_channel,
            metadata,
            status="archived_source_remaining",
        )
        if thread is not None:
            await thread.send(
                "Archive succeeded, but the original ticket channel could not be deleted.",
                allowed_mentions=_NO_MENTIONS,
            )
        return False
    else:
        _persist_ticket(source_channel, metadata, status="closed")
        return True


async def _archive_ticket(
    interaction: discord.Interaction,
    panel: dict,
    metadata: dict[str, str],
) -> None:
    guild = interaction.guild
    source_channel = interaction.channel
    if guild is None or not isinstance(source_channel, discord.TextChannel):
        await interaction.followup.send(
            "This button can only be used inside a ticket channel.",
            ephemeral=True,
        )
        return

    archive_channel_id = panel.get("ticket_archive_channel_id")
    archive_channel = (
        guild.get_channel(int(archive_channel_id or 0)) if archive_channel_id else None
    )
    if not isinstance(archive_channel, discord.TextChannel):
        await interaction.followup.send(
            "Ticket archive channel is not configured or not found. Ask an admin to recreate/update this panel.",
            ephemeral=True,
        )
        return
    if archive_channel.permissions_for(guild.default_role).view_channel:
        await interaction.followup.send(
            "The configured archive channel is visible to @everyone. Make it private before archiving this ticket.",
            ephemeral=True,
        )
        return

    bot_member: Optional[discord.Member] = getattr(guild, "me", None)
    if bot_member is None:
        await interaction.followup.send(
            "Bot member information unavailable. Try again.",
            ephemeral=True,
        )
        return
    permissions = archive_channel.permissions_for(bot_member)
    if not (
        permissions.view_channel
        and permissions.send_messages
        and permissions.read_message_history
        and permissions.create_public_threads
        and permissions.send_messages_in_threads
    ):
        await interaction.followup.send(
            f"I need permissions in {archive_channel.mention} to archive tickets (send messages + create threads).",
            ephemeral=True,
        )
        return

    try:
        _persist_ticket(
            source_channel,
            metadata,
            status="closing",
            extra={"archive_channel_id": int(archive_channel.id)},
        )
    except Exception:
        LOGGER.exception(
            "Could not persist closing state for ticket channel %s",
            source_channel.id,
        )
        await interaction.followup.send(
            "The ticket state could not be saved locally, so it was not archived. Try again.",
            ephemeral=True,
        )
        return
    if not await _freeze_ticket_source(source_channel, metadata):
        await interaction.followup.send(
            "I couldn't freeze this ticket before copying it. Grant Manage Channels and try again; no transcript was created.",
            ephemeral=True,
        )
        return
    try:
        _persist_ticket(
            source_channel,
            metadata,
            status="closing",
            extra={
                "archive_channel_id": int(archive_channel.id),
                "source_frozen": True,
            },
        )
    except Exception:
        LOGGER.exception(
            "Could not persist frozen source state for ticket %s",
            source_channel.id,
        )
        await interaction.followup.send(
            "The ticket was frozen, but recovery state could not be saved. Try closing it again.",
            ephemeral=True,
        )
        return
    try:
        archive_message, thread = await _ensure_archive_container(
            source_channel,
            metadata,
            archive_channel,
            bot_user_id=int(getattr(bot_member, "id", 0) or 0),
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "Could not create or resume the ticket archive (missing permissions). The saved closing state will be retried after restart.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        await interaction.followup.send(
            "Discord API error while creating or resuming the ticket archive. The saved closing state will be retried after restart.",
            ephemeral=True,
        )
        return
    except Exception:
        LOGGER.exception(
            "Could not persist or resolve archive IDs for ticket channel %s",
            source_channel.id,
        )
        await interaction.followup.send(
            "The archive could not be safely resumed. The original ticket was kept for a retry.",
            ephemeral=True,
        )
        return

    try:
        await _copy_ticket_to_archive_once(
            source_channel,
            thread,
            metadata,
            actor_label=interaction.user.mention,
        )
    except (discord.Forbidden, discord.HTTPException):
        await interaction.followup.send(
            "Discord interrupted the archive copy. Progress was saved and will resume without duplicating completed entries after restart.",
            ephemeral=True,
        )
        return
    except Exception:
        LOGGER.exception(
            "Could not complete ticket archive for channel %s",
            source_channel.id,
        )
        await interaction.followup.send(
            "The archive copy was interrupted. Its durable progress will be retried after restart.",
            ephemeral=True,
        )
        return

    await _clean_completed_archive_for_source(
        source_channel,
        metadata,
        archive_message,
        thread,
    )
    await interaction.followup.send(
        f"Ticket archived to {thread.mention}. Deleting the ticket channel now.",
        ephemeral=True,
    )
    await _delete_archived_ticket_source(source_channel, metadata, thread)


async def _close_legacy_ticket(
    interaction: discord.Interaction,
    panel: dict,
    metadata: dict[str, str],
) -> None:
    """Close a ticket created by the pre-archive category-based schema."""
    guild = interaction.guild
    channel: object = interaction.channel
    if guild is None or not isinstance(channel, _LegacyTicketChannel):
        await interaction.followup.send(
            "This button can only be used inside a ticket channel.",
            ephemeral=True,
        )
        return

    opener_id_raw = str(metadata.get("opener_id") or "0")
    opener_member = guild.get_member(int(opener_id_raw)) if opener_id_raw.isdigit() else None
    opener_display_name = (
        getattr(opener_member, "display_name", None) if opener_member is not None else None
    ) or "user"
    resolved_slug = metadata.get("opener_slug") or _slugify_channel_component(opener_display_name)

    new_name = _build_ticket_channel_name("closed", resolved_slug)

    closed_category: Optional[discord.CategoryChannel] = None
    closed_category_id = panel.get("closed_ticket_category_id")
    parsed_closed_category_id = _parse_int(closed_category_id)
    if parsed_closed_category_id is not None and parsed_closed_category_id > 0:
        channel_obj = guild.get_channel(parsed_closed_category_id)
        if isinstance(channel_obj, discord.CategoryChannel):
            closed_category = channel_obj

    # Lock the applicant before marking the channel closed. If this fails, the
    # manager can safely retry without leaving a closed-looking writable ticket.
    if opener_member is not None:
        try:
            await channel.set_permissions(
                opener_member,
                send_messages=False,
                add_reactions=False,
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "I couldn't revoke the applicant's ticket permissions. The ticket was not closed.",
                ephemeral=True,
            )
            return

    try:
        if closed_category is not None:
            try:
                await channel.edit(
                    name=new_name,
                    category=closed_category,
                )
            except (discord.Forbidden, discord.HTTPException):
                # Preserve the old workflow's rename-only fallback when the bot
                # cannot move the channel into the configured category.
                await channel.edit(name=new_name)
        else:
            await channel.edit(name=new_name)
    except (discord.Forbidden, discord.HTTPException):
        await interaction.followup.send(
            "I couldn't rename or move this ticket channel. Please check my channel permissions and try again.",
            ephemeral=True,
        )
        return

    try:
        await channel.send(
            f"Ticket closed by {interaction.user.mention}.",
            allowed_mentions=_NO_MENTIONS,
        )
    except (discord.Forbidden, discord.HTTPException):
        await interaction.followup.send(
            "Ticket closed, but I couldn't post the closing notice.",
            ephemeral=True,
        )
        return

    try:
        _persist_ticket(
            channel,
            metadata,
            status="closed",
        )
    except Exception:
        LOGGER.exception(
            "Legacy ticket %s closed in Discord but was not persisted",
            channel.id,
        )
        await interaction.followup.send(
            "Ticket closed. Its local record will be repaired during startup reconciliation.",
            ephemeral=True,
        )
        return
    await interaction.followup.send("Ticket closed.", ephemeral=True)


class TicketCloseView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="tickets:close"
    )
    async def close_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "This button can only be used inside a ticket channel.", ephemeral=True
            )
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "You don't have permission to close this ticket.", ephemeral=True
            )
            return

        metadata = _parse_ticket_topic(interaction.channel.topic)
        panel_id = metadata.get("panel_id")
        if not panel_id:
            await interaction.response.send_message(
                "Ticket configuration was not found.", ephemeral=True
            )
            return
        panel = _get_panel_by_id(interaction.guild.id, panel_id)
        if panel is None:
            await interaction.response.send_message(
                "Ticket configuration was not found.", ephemeral=True
            )
            return

        if not _has_management_access(interaction.user, panel.get("management_role_ids", [])):
            await interaction.response.send_message(
                "Only the management team can close this ticket.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        async with _ticket_close_locks.hold(int(interaction.channel.id)):
            record = runtime_state.get_record(
                _TICKET_RUNTIME_KIND,
                interaction.guild.id,
                interaction.channel.id,
            )
            if interaction.channel.name.startswith(("closed-", "archiving-")) or (
                record is not None
                and record.status in {"closing", "archiving", "archived_source_remaining", "closed"}
            ):
                await _send_ephemeral_notice(
                    interaction,
                    "This ticket is already being closed.",
                )
                return
            fresh_panel = _get_panel_by_id(interaction.guild.id, panel_id)
            if fresh_panel is None:
                await _send_ephemeral_notice(
                    interaction,
                    "Ticket configuration was not found.",
                )
                return
            if not _has_management_access(
                interaction.user,
                fresh_panel.get("management_role_ids", []),
            ):
                await _send_ephemeral_notice(
                    interaction,
                    "Only the management team can close this ticket.",
                )
                return
            close_mode = _get_panel_close_mode(fresh_panel)
            if close_mode == "archive":
                await _archive_ticket(interaction, fresh_panel, metadata)
            elif close_mode == "legacy_category":
                await _close_legacy_ticket(interaction, fresh_panel, metadata)
            else:
                await _send_ephemeral_notice(
                    interaction,
                    "This ticket panel has no close destination configured. Ask an admin to recreate it.",
                )


async def handle_tickets_setup(bot, interaction: discord.Interaction):
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

    home_view = TicketsSetupHomeView(bot, interaction.user.id, interaction.guild)
    await interaction.response.send_message(
        embed=_build_home_embed(interaction.guild), view=home_view
    )


def _record_archive_channel_id(record: runtime_state.RuntimeRecord) -> Optional[int]:
    value = record.payload.get("archive_channel_id")
    if not value:
        panel = _get_panel_by_id(
            record.guild_id,
            str(record.payload.get("panel_id") or ""),
        )
        value = panel.get("ticket_archive_channel_id") if panel is not None else None
    parsed = _parse_int(value)
    if parsed is None:
        return None
    return parsed if parsed > 0 else None


async def _resolve_record_archive_resources(
    guild,
    record: runtime_state.RuntimeRecord,
    bot_user_id: int,
):
    archive_channel_id = _record_archive_channel_id(record)
    if archive_channel_id is None:
        return None, None, None
    archive_channel = await _fetch_guild_channel(guild, archive_channel_id)
    if not isinstance(archive_channel, discord.TextChannel):
        return None, None, None
    try:
        archive_message_id = int(record.payload.get("archive_message_id") or 0) or None
    except (TypeError, ValueError):
        archive_message_id = None
    try:
        archive_thread_id = int(record.payload.get("archive_thread_id") or 0) or None
    except (TypeError, ValueError):
        archive_thread_id = None
    archive_message = await _find_archive_anchor(
        archive_channel,
        guild_id=guild.id,
        source_channel_id=int(record.external_id),
        archive_message_id=archive_message_id,
        bot_user_id=bot_user_id,
    )
    if archive_message is None:
        if archive_thread_id is not None:
            try:
                candidate = await _fetch_guild_channel(guild, archive_thread_id)
            except discord.NotFound:
                candidate = None
            if isinstance(candidate, discord.Thread):
                return archive_channel, None, candidate
        return archive_channel, None, None
    thread = await _resolve_archive_thread(
        guild,
        archive_message,
        archive_thread_id,
    )
    return archive_channel, archive_message, thread


def _mark_archive_checkpoint_embeds_removed(
    record: runtime_state.RuntimeRecord,
) -> None:
    payload = dict(record.payload)
    payload["archive_checkpoint_embeds_removed"] = True
    payload["archive_checkpoints_removed"] = True
    payload["archive_checkpoint_cleanup_version"] = _ARCHIVE_CHECKPOINT_CLEANUP_VERSION
    runtime_state.upsert_record(
        _TICKET_RUNTIME_KIND,
        record.guild_id,
        record.external_id,
        payload,
        status=record.status,
    )


def _archive_checkpoint_cleanup_is_current(
    record: runtime_state.RuntimeRecord,
) -> bool:
    try:
        version = int(record.payload.get("archive_checkpoint_cleanup_version") or 0)
    except (TypeError, ValueError):
        return False
    return version >= _ARCHIVE_CHECKPOINT_CLEANUP_VERSION


async def _clean_completed_archive_record(
    guild,
    record: runtime_state.RuntimeRecord,
    bot_user_id: int,
) -> bool:
    """Clean a completed archive whose source channel may no longer exist."""

    if _archive_checkpoint_cleanup_is_current(record):
        return True
    if _record_archive_channel_id(record) is None:
        return False
    try:
        _archive_channel, archive_message, thread = await _resolve_record_archive_resources(
            guild,
            record,
            bot_user_id,
        )
        cleanup_succeeded = await _clean_completed_archive_checkpoints(
            archive_message,
            thread,
        )
    except discord.NotFound:
        cleanup_succeeded = True
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        cleanup_succeeded = False
    if not cleanup_succeeded:
        return False
    _mark_archive_checkpoint_embeds_removed(record)
    return True


async def _archive_is_complete_without_source(
    guild,
    record: runtime_state.RuntimeRecord,
    bot_user_id: int,
) -> Optional[bool]:
    try:
        _archive_channel, _archive_message, thread = await _resolve_record_archive_resources(
            guild, record, bot_user_id
        )
    except discord.NotFound:
        return False
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return None
    if thread is None:
        return False
    markers, legacy_contents = await _read_archive_thread_state(
        thread,
        guild_id=guild.id,
        source_channel_id=int(record.external_id),
    )
    complete_marker = _archive_marker(
        guild.id,
        int(record.external_id),
        "complete",
    )
    return bool(
        _archive_checkpoint_exists(complete_marker, markers)
        or legacy_contents["Archive complete. Deleting ticket channel."] > 0
    )


async def _resume_ticket_archive(
    guild,
    source_channel,
    record: runtime_state.RuntimeRecord,
    bot_user_id: int,
) -> None:
    archive_channel_id = _record_archive_channel_id(record)
    if archive_channel_id is None:
        LOGGER.warning(
            "Cannot resume ticket %s in guild %s without an archive channel ID",
            record.external_id,
            guild.id,
        )
        return
    archive_channel = await _fetch_guild_channel(guild, archive_channel_id)
    if not isinstance(archive_channel, discord.TextChannel):
        LOGGER.warning(
            "Cannot resume ticket %s: archive channel %s is unavailable",
            record.external_id,
            archive_channel_id,
        )
        return
    if archive_channel.permissions_for(guild.default_role).view_channel:
        LOGGER.error(
            "Refusing to resume ticket %s because archive channel %s is visible to @everyone",
            record.external_id,
            archive_channel_id,
        )
        return
    bot_member = getattr(guild, "me", None)
    if bot_member is None:
        LOGGER.warning(
            "Cannot resume ticket %s because the bot member is unavailable",
            record.external_id,
        )
        return
    permissions = archive_channel.permissions_for(bot_member)
    if not (
        permissions.view_channel
        and permissions.send_messages
        and permissions.read_message_history
        and permissions.create_public_threads
        and permissions.send_messages_in_threads
    ):
        LOGGER.warning(
            "Cannot resume ticket %s because archive channel permissions are incomplete",
            record.external_id,
        )
        return
    metadata = _metadata_from_ticket_record(record)
    if not await _freeze_ticket_source(source_channel, metadata):
        LOGGER.warning(
            "Cannot resume ticket %s because its source could not be frozen",
            record.external_id,
        )
        return
    _persist_ticket(
        source_channel,
        metadata,
        status="closing",
        extra={"source_frozen": True},
    )
    archive_message, thread = await _ensure_archive_container(
        source_channel,
        metadata,
        archive_channel,
        bot_user_id=bot_user_id,
    )
    await _copy_ticket_to_archive_once(
        source_channel,
        thread,
        metadata,
        actor_label="restart recovery",
    )
    await _clean_completed_archive_for_source(
        source_channel,
        metadata,
        archive_message,
        thread,
    )
    await _delete_archived_ticket_source(source_channel, metadata, thread)


def _ticket_panel_publication_was_committed(
    record: runtime_state.RuntimeRecord,
) -> bool:
    panel = record.payload.get("panel")
    if not isinstance(panel, dict):
        return False
    panel_id = str(record.payload.get("panel_id") or panel.get("id") or "")
    if not panel_id:
        return False
    stored = _get_panel_by_id(record.guild_id, panel_id)
    if not isinstance(stored, dict):
        return False
    try:
        return (
            int(stored.get("panel_channel_id") or 0) == int(panel.get("panel_channel_id") or 0)
            and int(stored.get("panel_message_id") or 0) == int(panel.get("panel_message_id") or 0)
            and int(panel.get("panel_message_id") or 0) > 0
        )
    except (TypeError, ValueError):
        return False


async def _resolve_pending_ticket_panel_message(
    guild: discord.Guild,
    record: runtime_state.RuntimeRecord,
) -> Optional[discord.Message]:
    panel = record.payload.get("panel")
    if not isinstance(panel, dict):
        return None
    try:
        channel_id = int(
            panel.get("panel_channel_id") or panel.get("panel_destination_channel_id") or 0
        )
        message_id = int(panel.get("panel_message_id") or 0)
    except (TypeError, ValueError):
        return None
    if not channel_id:
        return None

    channel: discord.abc.GuildChannel | discord.Thread | None = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except discord.NotFound:
            return None
    if message_id:
        if not isinstance(channel, _MessageFetchingChannel):
            raise AttributeError("Ticket panel channel cannot fetch messages")
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            return None
        bot_user_id = int(getattr(getattr(guild, "me", None), "id", 0) or 0)
        return message if _message_is_bot_authored(message, bot_user_id) else None

    if not isinstance(channel, _MessageHistoryChannel):
        raise AttributeError("Ticket panel channel has no message history")
    bot_user_id = int(getattr(getattr(guild, "me", None), "id", 0) or 0)
    async for message in channel.history(limit=_FALLBACK_HISTORY_LIMIT):
        if _message_is_bot_authored(
            message,
            bot_user_id,
        ) and _message_has_panel_publish_marker(message, record.external_id):
            return message
    return None


async def reconcile_ticket_panel_publications(bot: discord.Client) -> None:
    """Remove interrupted panel publishes without touching committed panels."""

    for record in runtime_state.list_records(_PANEL_PUBLISH_RUNTIME_KIND):
        if getattr(record, "kind", None) != _PANEL_PUBLISH_RUNTIME_KIND:
            continue
        try:
            if _ticket_panel_publication_was_committed(record):
                panel = record.payload.get("panel")
                guild = bot.get_guild(record.guild_id)
                if (
                    str(record.payload.get("operation") or "") == "resend"
                    and isinstance(panel, dict)
                    and guild is not None
                    and not await _disable_previous_ticket_panel_message(guild, panel)
                ):
                    runtime_state.set_status(
                        _PANEL_PUBLISH_RUNTIME_KIND,
                        record.guild_id,
                        record.external_id,
                        "old_cleanup_pending",
                    )
                    continue
                runtime_state.delete_record(
                    _PANEL_PUBLISH_RUNTIME_KIND,
                    record.guild_id,
                    record.external_id,
                )
                continue
            guild = bot.get_guild(record.guild_id)
            if guild is None:
                continue
            message = await _resolve_pending_ticket_panel_message(guild, record)
            if message is not None and not await _compensate_panel_publish_message(message):
                runtime_state.set_status(
                    _PANEL_PUBLISH_RUNTIME_KIND,
                    record.guild_id,
                    record.external_id,
                    "cleanup_pending",
                )
                continue
            runtime_state.delete_record(
                _PANEL_PUBLISH_RUNTIME_KIND,
                record.guild_id,
                record.external_id,
            )
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            LOGGER.warning(
                "Ticket-panel publication %s still needs startup cleanup",
                record.external_id,
            )
        except Exception:
            LOGGER.exception(
                "Ticket-panel publication reconciliation failed for %s",
                record.external_id,
            )

    for guild in getattr(bot, "guilds", ()):
        try:
            cleaned = await _clean_committed_ticket_panel_checkpoints_for_guild(guild)
        except Exception:
            LOGGER.exception(
                "Committed ticket-panel checkpoint cleanup failed in guild %s",
                guild.id,
            )
            continue
        if not cleaned:
            LOGGER.warning(
                "Some committed ticket panels still need checkpoint cleanup in guild %s",
                guild.id,
            )


async def reconcile_ticket_creations(bot: discord.Client) -> None:
    """Resume ticket creation and remove all transient or legacy checkpoints."""

    pending_statuses = {"pending", "channel_ready", "control_ready", "messages_ready"}
    for record in runtime_state.list_records(
        _TICKET_CREATION_RUNTIME_KIND,
        statuses=tuple(pending_statuses),
    ):
        # Defensive for test adapters and alternative stores that do not apply the
        # requested status filter themselves.
        if getattr(record, "status", None) not in pending_statuses:
            continue
        get_guild = getattr(bot, "get_guild", None)
        guild = (
            get_guild(record.guild_id)
            if callable(get_guild)
            else next(
                (
                    candidate
                    for candidate in getattr(bot, "guilds", ())
                    if int(getattr(candidate, "id", 0) or 0) == int(record.guild_id)
                ),
                None,
            )
        )
        if guild is None:
            continue
        try:
            await _complete_ticket_creation(bot, guild, record)
        except Exception:
            LOGGER.exception(
                "Ticket creation %s could not be reconciled in guild %s",
                record.external_id,
                record.guild_id,
            )

    for record in runtime_state.list_records(
        _TICKET_CREATION_RUNTIME_KIND,
        statuses=("completed",),
    ):
        if getattr(record, "status", None) != "completed" or record.payload.get(
            "discord_checkpoints_removed"
        ):
            continue
        get_guild = getattr(bot, "get_guild", None)
        guild = (
            get_guild(record.guild_id)
            if callable(get_guild)
            else next(
                (
                    candidate
                    for candidate in getattr(bot, "guilds", ())
                    if int(getattr(candidate, "id", 0) or 0) == int(record.guild_id)
                ),
                None,
            )
        )
        if guild is None:
            continue
        try:
            await _clean_completed_ticket_creation_checkpoints(guild, record)
        except (
            discord.Forbidden,
            discord.HTTPException,
            AttributeError,
        ):
            LOGGER.warning(
                "Legacy ticket creation footers still need cleanup for %s in guild %s",
                record.external_id,
                record.guild_id,
            )
        except Exception:
            LOGGER.exception(
                "Legacy ticket creation footer cleanup failed for %s in guild %s",
                record.external_id,
                record.guild_id,
            )


async def reconcile_tickets(bot: discord.Client) -> None:
    """Converge durable ticket workflows, then discover untracked legacy tickets."""

    await reconcile_ticket_panel_publications(bot)
    await reconcile_ticket_creations(bot)
    bot_user_id = int(getattr(getattr(bot, "user", None), "id", 0) or 0)
    for guild in getattr(bot, "guilds", ()):
        records = runtime_state.list_records(_TICKET_RUNTIME_KIND, guild_id=guild.id)
        tracked_channel_ids: set[int] = set()
        for record in records:
            try:
                channel_id = int(record.external_id)
            except ValueError:
                continue
            tracked_channel_ids.add(channel_id)
            has_archive_resource = any(
                record.payload.get(key)
                for key in (
                    "archive_channel_id",
                    "archive_message_id",
                    "archive_thread_id",
                )
            )
            if (
                record.status in {"closed", "archived_source_remaining"}
                and has_archive_resource
                and not _archive_checkpoint_cleanup_is_current(record)
            ):
                try:
                    await _clean_completed_archive_record(
                        guild,
                        record,
                        bot_user_id,
                    )
                except Exception:
                    LOGGER.exception(
                        "Legacy archive checkpoint cleanup failed for ticket %s in guild %s",
                        record.external_id,
                        guild.id,
                    )
            needs_creation_cleanup = bool(record.payload.get("creation_id")) and not bool(
                record.payload.get("creation_checkpoints_removed")
            )
            if record.status == "closed" and not needs_creation_cleanup:
                continue
            if record.status not in {
                "open",
                "missing",
                "closing",
                "archiving",
                "archived_source_remaining",
                "closed",
            }:
                continue

            try:
                source_channel = await _fetch_guild_channel(guild, channel_id)
            except discord.NotFound:
                source_channel = None
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                LOGGER.warning(
                    "Could not resolve ticket channel %s in guild %s during reconciliation",
                    channel_id,
                    guild.id,
                )
                continue

            if source_channel is not None and needs_creation_cleanup:
                try:
                    await _clean_active_ticket_creation_checkpoints(source_channel, record)
                except (discord.Forbidden, discord.HTTPException, AttributeError):
                    LOGGER.warning(
                        "Ticket %s still needs creation-checkpoint cleanup in guild %s",
                        record.external_id,
                        guild.id,
                    )
                except Exception:
                    LOGGER.exception(
                        "Active ticket checkpoint cleanup failed for %s in guild %s",
                        record.external_id,
                        guild.id,
                    )

            if record.status == "closed":
                continue

            if record.status == "archived_source_remaining":
                if source_channel is None:
                    runtime_state.set_status(
                        _TICKET_RUNTIME_KIND,
                        guild.id,
                        record.external_id,
                        "closed",
                    )
                else:
                    await _delete_archived_ticket_source(
                        source_channel,
                        _metadata_from_ticket_record(record),
                    )
                continue

            if source_channel is None:
                if record.status in {"closing", "archiving"}:
                    completion = await _archive_is_complete_without_source(
                        guild,
                        record,
                        bot_user_id,
                    )
                    if completion is None:
                        continue
                    if completion and has_archive_resource:
                        try:
                            await _clean_completed_archive_record(
                                guild,
                                record,
                                bot_user_id,
                            )
                        except Exception:
                            LOGGER.exception(
                                "Legacy archive checkpoint cleanup failed for ticket %s in guild %s",
                                record.external_id,
                                guild.id,
                            )
                    new_status = "closed" if completion else "archive_incomplete_source_missing"
                else:
                    new_status = "missing"
                runtime_state.set_status(
                    _TICKET_RUNTIME_KIND,
                    guild.id,
                    record.external_id,
                    new_status,
                )
                continue

            if record.status in {"closing", "archiving"}:
                try:
                    await _resume_ticket_archive(
                        guild,
                        source_channel,
                        record,
                        bot_user_id,
                    )
                except (discord.Forbidden, discord.HTTPException, AttributeError):
                    LOGGER.warning(
                        "Discord prevented resuming ticket archive %s in guild %s",
                        record.external_id,
                        guild.id,
                    )
                except Exception:
                    LOGGER.exception(
                        "Could not resume ticket archive %s in guild %s",
                        record.external_id,
                        guild.id,
                    )
                continue

            metadata = _parse_ticket_topic(getattr(source_channel, "topic", None))
            if metadata.get("panel_id") and metadata.get("opener_id"):
                _persist_ticket(source_channel, metadata, status="open")

        # Discover pre-SQLite tickets from the cache, but never overwrite a
        # durable record that was already reconciled above.
        for channel in getattr(guild, "text_channels", ()):
            if int(channel.id) in tracked_channel_ids:
                continue
            metadata = _parse_ticket_topic(getattr(channel, "topic", None))
            if not metadata.get("panel_id") or not metadata.get("opener_id"):
                continue
            status = "closed" if str(getattr(channel, "name", "")).startswith("closed-") else "open"
            _persist_ticket(channel, metadata, status=status)


def register_persistent_views(bot) -> None:
    bot.add_view(TicketOpenView(bot))
    bot.add_view(TicketCloseView(bot))
