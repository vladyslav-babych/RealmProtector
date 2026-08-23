from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, SupportsIndex, SupportsInt
from uuid import uuid4

import discord

from src.realm_protector.bot import composition, message_checkpoints
from src.realm_protector.bot.common import allowed_user_mentions
from src.realm_protector.infrastructure import (
    document_store,
    feature_config_store,
    guild_settings,
    runtime_state,
)
from src.realm_protector.services import authorization, role_security
from src.realm_protector.services.keyed_locks import KeyedLockPool

_OBJECTIVE_TYPE_VORTEX = "Vortex"
_OBJECTIVE_TYPE_CORE = "Core"
_OBJECTIVE_TYPE_NODE = "Node"

_VORTEX_RARITIES = ["Common", "Uncommon", "Epic", "Legendary"]
_NODE_TYPES = ["Wood", "Hide", "Ore", "Fiber"]
_NODE_TIERS = ["4.4", "5.4", "6.4", "7.4", "8.4"]

_VORTEX_RARITY_EMOJI = {
    "Common": "🟢",
    "Uncommon": "🔵",
    "Epic": "🟣",
    "Legendary": "🟡",
}


def _vortex_rarity_display(rarity: Optional[str]) -> str:
    value = (rarity or "").strip()
    if not value:
        return "Not selected yet"
    emoji_char = _VORTEX_RARITY_EMOJI.get(value)
    if not emoji_char:
        return value
    return f"{emoji_char} {value} {emoji_char}"


_NOTIFY_BEFORE_MINUTES_OPTIONS = list(range(5, 61, 5))
_MAX_OBJECTIVE_SUBSCRIBERS = 75
_OBJECTIVES_CONFIG_NAMESPACE = "objectives"


def _notify_before_display(minutes: Optional[int]) -> str:
    try:
        minutes_int = int(minutes) if minutes is not None else 0
    except (TypeError, ValueError):
        minutes_int = 0
    if minutes_int in _NOTIFY_BEFORE_MINUTES_OPTIONS:
        return f"{minutes_int} minutes"
    return "Not selected yet"


_OBJECTIVE_EXPIRY_SECONDS = 60
_SCHEDULER_INTERVAL_SECONDS = 10
_NOTIFY_ROLE_NAME_FIELD = "notify_role_name"
_NOTIFY_ROLE_OWNERSHIP_FIELD = "notify_role_created_by_bot"
_NOTIFICATION_RUNTIME_KIND = "objective_notification"
_CREATION_RUNTIME_KIND = "objective_creation"
_PANEL_PUBLICATION_RUNTIME_KIND = "objective_panel_publication"
_NOTIFICATION_MARKER_PREFIX = "Realm Protector objective notification"
_CREATION_MARKER_PREFIX = "Realm Protector objective creation"
_PANEL_MARKER_PREFIX = "Realm Protector objectives panel"
_MESSAGE_CHECKPOINT_CLEANUP_FIELD = "message_checkpoint_removed"
_NOTIFY_ROLE_NAME_CLEANUP_FIELD = "notify_role_name_cleaned"
_NOTIFY_ROLE_CHECKPOINT_BITS = 40

_scheduler_task: Optional[asyncio.Task] = None
_objective_guild_locks: KeyedLockPool[int] = KeyedLockPool()
_notify_member_locks: KeyedLockPool[tuple[int, int, int]] = KeyedLockPool()
_MARKER_FALLBACK_HISTORY_LIMIT = None


@dataclass(frozen=True)
class _NotifyRoleResolution:
    role_id: int
    role_name: str
    created_by_bot: bool


@dataclass(frozen=True)
class _ObjectiveCreationResult:
    success: bool
    objective: Optional[dict]
    warning: Optional[str] = None


def _safe_int(value: object, default: int = 0) -> int:
    if not isinstance(
        value,
        (str, bytes, bytearray, SupportsInt, SupportsIndex),
    ):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _durable_marker(prefix: str, guild_id: int, objective_key: str) -> str:
    digest = hashlib.sha256(f"{int(guild_id)}:{objective_key}".encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _creation_marker(guild_id: int, objective_key: str) -> str:
    return _durable_marker(_CREATION_MARKER_PREFIX, guild_id, objective_key)


def _notification_marker(guild_id: int, objective_key: str) -> str:
    return _durable_marker(_NOTIFICATION_MARKER_PREFIX, guild_id, objective_key)


def _panel_marker(guild_id: int) -> str:
    return _durable_marker(_PANEL_MARKER_PREFIX, guild_id, "panel")


def _new_panel_marker(guild_id: int) -> str:
    return f"{_panel_marker(guild_id)}:{uuid4().hex}"


def _message_is_bot_owned(message: object, bot_user_id: int) -> bool:
    """Reject a known non-bot author while tolerating partial test/API objects."""

    if not bot_user_id:
        return True
    author_id = _safe_int(getattr(getattr(message, "author", None), "id", 0))
    return not author_id or author_id == int(bot_user_id)


async def _clean_message_checkpoint(message: discord.Message, marker: str) -> bool:
    """Remove hidden and legacy metadata; return whether cleanup is complete."""

    try:
        await message_checkpoints.clean_message_checkpoint(message, marker)
    except discord.NotFound:
        return True
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return False
    return True


async def _make_notification_role_nonmentionable(role: discord.Role) -> bool:
    if not role.mentionable:
        return True
    try:
        await role.edit(
            mentionable=False,
            reason="Harden objective notification role",
        )
    except (discord.Forbidden, discord.HTTPException):
        return False
    return True


def _load_config() -> dict:
    return feature_config_store.load_objectives()


def _load_guild_entry(guild_id: int) -> Optional[dict]:
    entry = document_store.get_mapping_entry(_OBJECTIVES_CONFIG_NAMESPACE, guild_id)
    return entry if isinstance(entry, dict) else None


def _save_guild_entry(guild_id: int, entry: dict) -> None:
    document_store.upsert_mapping_entry(
        _OBJECTIVES_CONFIG_NAMESPACE,
        guild_id,
        entry,
    )


def _active_objectives_entry(guild_id: int) -> Optional[dict]:
    """Return enabled objective state only while the main bot setup still exists."""

    if not guild_settings.get_target_guild(guild_id):
        return None
    entry = _load_guild_entry(guild_id)
    if not isinstance(entry, dict) or entry.get("disabled"):
        return None
    return entry


def _objectives_panel_is_current(
    guild_id: int,
    channel_id: Optional[int],
    message_id: Optional[int],
) -> bool:
    entry = _active_objectives_entry(guild_id)
    if entry is None:
        return False
    try:
        return int(entry.get("panel_channel_id") or 0) == int(channel_id or 0) and int(
            entry.get("panel_message_id") or 0
        ) == int(message_id or 0)
    except (TypeError, ValueError):
        return False


def _objective_setup_is_authorized(
    guild: discord.Guild,
    member: Optional[discord.Member],
) -> bool:
    return bool(
        guild_settings.get_target_guild(guild.id)
        and member is not None
        and getattr(getattr(member, "guild", None), "id", guild.id) == guild.id
        and authorization.member_is_admin(member)
    )


def _objective_actor_can_manage(
    guild: discord.Guild,
    member: Optional[discord.Member],
) -> bool:
    return bool(
        guild_settings.get_target_guild(guild.id)
        and member is not None
        and getattr(getattr(member, "guild", None), "id", guild.id) == guild.id
        and composition.has_caller_access(
            member,
            guild_settings.get_caller_roles(guild.id),
            guild_settings.get_caller_role_ids(guild.id),
        )
    )


def clear_guild_objective_configuration(guild_id: int) -> bool:
    """Remove stored objective state without guessing which Discord assets to delete.

    Configuration removal has no interaction context or reliable permission
    guarantees. Objective messages and notification roles therefore remain for an
    administrator to review instead of being deleted blindly.
    """
    return document_store.delete_mapping_entry(_OBJECTIVES_CONFIG_NAMESPACE, guild_id) is not None


async def deactivate_guild_objective_configuration(guild: discord.Guild) -> bool:
    """Disable controls and clean notification assets before retiring state."""

    entry = _load_guild_entry(guild.id)
    if not isinstance(entry, dict):
        return False
    entry["disabled"] = True
    _save_guild_entry(guild.id, entry)
    all_assets_clean = True
    bot_user_id = _safe_int(getattr(getattr(guild, "me", None), "id", 0))

    panel_channel_id = entry.get("panel_channel_id")
    panel_message_id = entry.get("panel_message_id")
    try:
        panel_channel_id_int = int(panel_channel_id or 0)
        panel_message_id_int = int(panel_message_id or 0)
    except (TypeError, ValueError):
        panel_channel_id_int = 0
        panel_message_id_int = 0
    if panel_channel_id_int and panel_message_id_int:
        try:
            channel = guild.get_channel(panel_channel_id_int) or await guild.fetch_channel(
                panel_channel_id_int
            )
            if isinstance(channel, discord.TextChannel):
                message = await channel.fetch_message(panel_message_id_int)
                author_id = _safe_int(getattr(getattr(message, "author", None), "id", 0))
                if bot_user_id and author_id == bot_user_id:
                    await message.edit(
                        content="This objectives panel has been disabled.",
                        embed=None,
                        view=None,
                    )
                else:
                    all_assets_clean = False
            else:
                all_assets_clean = False
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            all_assets_clean = False

    for objective in entry.get("objectives", []):
        if not isinstance(objective, dict):
            continue
        if not await _cleanup_objective_notification_assets(
            guild,
            objective,
            panel_channel_id_int or None,
            role_delete_reason="Realm Protector configuration removed",
        ):
            all_assets_clean = False
        try:
            channel_id = int(objective.get("channel_id") or panel_channel_id_int or 0)
            message_id = int(objective.get("message_id") or 0)
        except (TypeError, ValueError):
            continue
        if not channel_id or not message_id:
            continue
        try:
            objective_channel = await _resolve_text_channel(guild, channel_id)
            if objective_channel is None:
                all_assets_clean = False
                continue
            message = await objective_channel.fetch_message(message_id)
            author_id = _safe_int(getattr(getattr(message, "author", None), "id", 0))
            if not bot_user_id or author_id != bot_user_id:
                all_assets_clean = False
                continue
            await message_checkpoints.clean_message_checkpoint_prefixes(
                message,
                (
                    f"{_CREATION_MARKER_PREFIX}:",
                    f"{_NOTIFICATION_MARKER_PREFIX}:",
                ),
            )
            await message.edit(view=None)
        except discord.NotFound:
            continue
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            all_assets_clean = False

    if all_assets_clean:
        return clear_guild_objective_configuration(guild.id)
    latest_entry = _load_guild_entry(guild.id)
    if isinstance(latest_entry, dict):
        latest_entry["disabled"] = True
        _save_guild_entry(guild.id, latest_entry)
    return True


def _guild_entry(entry: Optional[dict]) -> dict:
    if not isinstance(entry, dict):
        entry = {}
    entry.setdefault("panel_channel_id", None)
    entry.setdefault("panel_message_id", None)
    entry.setdefault("objectives", [])
    if not isinstance(entry.get("objectives"), list):
        entry["objectives"] = []
    return entry


def get_objectives_panel_message(guild_id: int) -> tuple[Optional[int], Optional[int]]:
    entry = _active_objectives_entry(guild_id)
    if entry is None:
        return None, None

    channel_id = _safe_int(entry.get("panel_channel_id")) or None
    message_id = _safe_int(entry.get("panel_message_id")) or None

    return channel_id, message_id


def set_objectives_panel_message(guild_id: int, channel_id: int, message_id: int) -> bool:
    existing_entry = _load_guild_entry(guild_id)
    if not guild_settings.get_target_guild(guild_id) or (
        isinstance(existing_entry, dict) and existing_entry.get("disabled")
    ):
        return False
    entry = _guild_entry(existing_entry)
    entry["panel_channel_id"] = str(channel_id)
    entry["panel_message_id"] = str(message_id)
    _save_guild_entry(guild_id, entry)
    return True


def add_objective(
    guild_id: int,
    objective: dict,
    *,
    expected_panel_channel_id: Optional[int] = None,
    expected_panel_message_id: Optional[int] = None,
) -> bool:
    existing_entry = _load_guild_entry(guild_id)
    if (
        not guild_settings.get_target_guild(guild_id)
        or not isinstance(existing_entry, dict)
        or existing_entry.get("disabled")
    ):
        return False
    entry = _guild_entry(existing_entry)
    if expected_panel_channel_id is not None and _safe_int(entry.get("panel_channel_id")) != int(
        expected_panel_channel_id
    ):
        return False
    if expected_panel_message_id is not None and _safe_int(entry.get("panel_message_id")) != int(
        expected_panel_message_id
    ):
        return False
    objectives = entry.setdefault("objectives", [])
    if not isinstance(objectives, list):
        objectives = []
        entry["objectives"] = objectives
    objective_id = str(objective.get("id") or "").strip()
    if objective_id and any(
        isinstance(existing, dict) and str(existing.get("id") or "").strip() == objective_id
        for existing in objectives
    ):
        return True
    objectives.append(objective)
    _save_guild_entry(guild_id, entry)
    return True


def _update_objective(guild_id: int, objective: dict) -> None:
    entry = _guild_entry(_load_guild_entry(guild_id))
    objectives = entry.get("objectives", [])
    if not isinstance(objectives, list):
        return

    obj_id = objective.get("id")
    if not obj_id:
        return

    for idx, item in enumerate(objectives):
        if isinstance(item, dict) and item.get("id") == obj_id:
            objectives[idx] = objective
            _save_guild_entry(guild_id, entry)
            return


def _build_panel_embed(guild: discord.Guild) -> discord.Embed:
    return discord.Embed(title="Active objectives:")


def _format_objective_name(obj: dict) -> str:
    obj_type = str(obj.get("type") or "").strip()
    if obj_type == _OBJECTIVE_TYPE_VORTEX:
        rarity = _vortex_rarity_display(obj.get("rarity"))
        map_name = obj.get("map") or "Unknown map"
        return f"🌀 Vortex ({rarity}) — {map_name}"
    if obj_type == _OBJECTIVE_TYPE_CORE:
        rarity = _vortex_rarity_display(obj.get("rarity"))
        map_name = obj.get("map") or "Unknown map"
        return f"🔷 Core ({rarity}) — {map_name}"
    if obj_type == _OBJECTIVE_TYPE_NODE:
        node_type = str(obj.get("node_type") or "Node").strip() or "Node"
        tier = obj.get("tier") or "?"
        map_name = obj.get("map") or "Unknown map"
        return f"⛏️ {node_type} ({tier}) — {map_name}"
    return "Objective"


def _format_pop_time(pop_at_ts: int, pop_time_utc_hhmm: Optional[str]) -> str:
    if not pop_at_ts:
        return "Time not set yet"
    time_label = str(pop_time_utc_hhmm or "").strip() or "??:??"
    return f"Pops in <t:{pop_at_ts}:R>, at {time_label} UTC"


def _build_objective_embed(obj: dict) -> discord.Embed:
    obj_type = obj.get("type")
    title = "Objective"
    if obj_type == _OBJECTIVE_TYPE_VORTEX:
        title = f"🌀 Vortex ({_vortex_rarity_display(obj.get('rarity'))})"
    elif obj_type == _OBJECTIVE_TYPE_CORE:
        title = f"🔷 Core ({_vortex_rarity_display(obj.get('rarity'))})"
    elif obj_type == _OBJECTIVE_TYPE_NODE:
        title = f"⛏️ Node ({obj.get('node_type', 'Unknown')} {obj.get('tier', '?')})"

    embed = discord.Embed(title=title)
    embed.add_field(name="Map", value=obj.get("map") or "Not set", inline=False)
    pop_at_ts = _safe_int(obj.get("pop_at_ts"))
    remove_at_ts = _safe_int(obj.get("remove_at_ts"))
    if remove_at_ts:
        name = _format_objective_name(obj)
        embed.add_field(
            name="Pop time",
            value=f"{name} has already popped up, it will be removed soon.",
            inline=False,
        )
    else:
        embed.add_field(
            name="Pop time",
            value=_format_pop_time(pop_at_ts, obj.get("pop_time_utc")),
            inline=False,
        )
    notify_before = obj.get("notify_before_minutes")
    embed.add_field(
        name="Notify before pop",
        value=_notify_before_display(notify_before),
        inline=False,
    )
    created_by = obj.get("created_by")
    created_by_id = obj.get("created_by_id")
    added_by_value: Optional[str] = None
    if created_by_id:
        try:
            created_by_id_int = int(created_by_id)
        except (TypeError, ValueError):
            created_by_id_int = None
        if created_by_id_int:
            added_by_value = f"<@{created_by_id_int}>"
    if not added_by_value and created_by:
        added_by_value = str(created_by)
    if added_by_value:
        embed.add_field(name="Added by", value=added_by_value, inline=False)
    return embed


async def _cancel_pending_notification(
    guild,
    record: runtime_state.RuntimeRecord,
) -> None:
    payload = record.payload
    channel_id = _safe_int(payload.get("channel_id"))
    marker = str(payload.get("marker") or _notification_marker(guild.id, record.external_id))
    try:
        channel = await _resolve_text_channel(guild, channel_id) if channel_id else None
        if channel is not None:
            message = await _find_marked_message(
                channel,
                marker,
                message_id=_safe_int(payload.get("message_id")) or None,
                bot_user_id=_safe_int(getattr(getattr(guild, "me", None), "id", 0)),
            )
            if message is not None:
                await message.delete()
    except discord.NotFound:
        pass
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return
    runtime_state.set_status(
        _NOTIFICATION_RUNTIME_KIND,
        guild.id,
        record.external_id,
        "cancelled",
    )


async def _reconcile_pending_notification(
    guild,
    record: runtime_state.RuntimeRecord,
) -> None:
    entry = _active_objectives_entry(guild.id)
    if entry is None:
        await _cancel_pending_notification(guild, record)
        return
    objective = _find_objective_by_key(entry, record.external_id)
    if objective is None:
        await _cancel_pending_notification(guild, record)
        return
    if _safe_int(objective.get("notified_ts")):
        runtime_state.set_status(
            _NOTIFICATION_RUNTIME_KIND,
            guild.id,
            record.external_id,
            "completed",
        )
        completed_record = runtime_state.get_record(
            _NOTIFICATION_RUNTIME_KIND,
            guild.id,
            record.external_id,
        )
        if completed_record is not None:
            await _cleanup_completed_notification_record(guild, completed_record)
        return
    now_ts = int(datetime.now(timezone.utc).timestamp())
    pop_at_ts = _safe_int(objective.get("pop_at_ts"))
    if not pop_at_ts or now_ts >= pop_at_ts or _safe_int(objective.get("remove_at_ts")):
        await _cancel_pending_notification(guild, record)
        return

    payload = dict(record.payload)
    channel_id = _safe_int(
        payload.get("channel_id") or objective.get("channel_id") or entry.get("panel_channel_id")
    )
    marker = str(payload.get("marker") or _notification_marker(guild.id, record.external_id))
    try:
        channel = await _resolve_text_channel(guild, channel_id) if channel_id else None
        message = (
            await _find_marked_message(
                channel,
                marker,
                message_id=_safe_int(payload.get("message_id")) or None,
                bot_user_id=_safe_int(getattr(getattr(guild, "me", None), "id", 0)),
            )
            if channel is not None
            else None
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
        return
    if message is not None:
        notified_ts = _safe_int(payload.get("notified_ts"), now_ts)
        payload.update(
            {
                "channel_id": channel_id,
                "message_id": int(message.id),
                "marker": marker,
                "notified_ts": notified_ts,
            }
        )
        runtime_state.upsert_record(
            _NOTIFICATION_RUNTIME_KIND,
            guild.id,
            record.external_id,
            payload,
            status="sent",
        )
        _apply_objective_deltas(
            guild.id,
            {},
            {record.external_id: notified_ts},
            {record.external_id: int(message.id)},
            set(),
            set(),
        )
        runtime_state.set_status(
            _NOTIFICATION_RUNTIME_KIND,
            guild.id,
            record.external_id,
            "completed",
        )
        completed_record = runtime_state.get_record(
            _NOTIFICATION_RUNTIME_KIND,
            guild.id,
            record.external_id,
        )
        if completed_record is not None:
            await _cleanup_completed_notification_record(guild, completed_record)
        return

    await _send_objective_notification(
        guild,
        objective,
        _safe_int(entry.get("panel_channel_id")) or None,
        _safe_int(payload.get("notify_before_minutes") or objective.get("notify_before_minutes")),
        notified_ts=_safe_int(payload.get("notified_ts"), now_ts),
    )


async def _cleanup_completed_panel_record(
    guild,
    record: runtime_state.RuntimeRecord,
) -> bool:
    channel_id = _safe_int(record.payload.get("target_channel_id"))
    message_id = _safe_int(record.payload.get("message_id"))
    if not channel_id or not message_id:
        current_channel_id, current_message_id = get_objectives_panel_message(guild.id)
        channel_id = channel_id or _safe_int(current_channel_id)
        message_id = message_id or _safe_int(current_message_id)
    marker = str(record.payload.get("marker") or _panel_marker(guild.id))
    return await _clean_record_message_checkpoint(
        guild,
        record,
        channel_id=channel_id,
        message_id=message_id,
        marker=marker,
    )


async def _cleanup_completed_creation_record(
    guild,
    record: runtime_state.RuntimeRecord,
) -> bool:
    payload = dict(record.payload)
    objective_key = str(payload.get("objective_key") or record.external_id)
    stored_objective = dict(payload.get("objective") or {})
    active_objective = _find_objective_by_key(_active_objectives_entry(guild.id), objective_key)
    objective = active_objective if active_objective is not None else stored_objective

    role_cleanup_complete = await _clean_persisted_notify_role_name(guild, objective)
    if role_cleanup_complete:
        if active_objective is not None:
            _update_objective(guild.id, active_objective)
        payload["objective"] = dict(objective)
        payload[_NOTIFY_ROLE_NAME_CLEANUP_FIELD] = True
        record = runtime_state.upsert_record(
            _CREATION_RUNTIME_KIND,
            guild.id,
            record.external_id,
            payload,
            status=record.status,
        )

    channel_id = _safe_int(objective.get("channel_id") or record.payload.get("panel_channel_id"))
    message_id = _safe_int(
        objective.get("message_id") or record.payload.get("objective_message_id")
    )
    marker = str(record.payload.get("marker") or _creation_marker(guild.id, objective_key))
    message_cleanup_complete = await _clean_record_message_checkpoint(
        guild,
        record,
        channel_id=channel_id,
        message_id=message_id,
        marker=marker,
    )
    return role_cleanup_complete and message_cleanup_complete


async def _cleanup_completed_notification_record(
    guild,
    record: runtime_state.RuntimeRecord,
) -> bool:
    objective = _find_objective_by_key(
        _active_objectives_entry(guild.id),
        record.external_id,
    )
    channel_id = _safe_int(
        record.payload.get("channel_id")
        or (objective.get("channel_id") if objective is not None else 0)
    )
    message_id = _safe_int(
        record.payload.get("message_id")
        or (objective.get("notify_message_id") if objective is not None else 0)
    )
    marker = str(record.payload.get("marker") or _notification_marker(guild.id, record.external_id))
    return await _clean_record_message_checkpoint(
        guild,
        record,
        channel_id=channel_id,
        message_id=message_id,
        marker=marker,
    )


async def _clean_active_objective_message_checkpoints(guild: discord.Guild) -> bool:
    """Sweep authoritative active messages when historical action rows are absent."""

    bot_user_id = _safe_int(getattr(getattr(guild, "me", None), "id", 0))
    if not bot_user_id:
        return False
    entry = _active_objectives_entry(guild.id)
    if not isinstance(entry, dict):
        return True

    targets: dict[tuple[int, int], set[str]] = {}

    def add_target(channel_id: object, message_id: object, marker_prefix: str) -> None:
        parsed_channel_id = _safe_int(channel_id)
        parsed_message_id = _safe_int(message_id)
        if parsed_channel_id and parsed_message_id:
            targets.setdefault((parsed_channel_id, parsed_message_id), set()).add(marker_prefix)

    panel_channel_id = _safe_int(entry.get("panel_channel_id"))
    add_target(
        panel_channel_id,
        entry.get("panel_message_id"),
        f"{_PANEL_MARKER_PREFIX}:",
    )
    objectives = entry.get("objectives", [])
    if isinstance(objectives, list):
        for objective in objectives:
            if not isinstance(objective, dict):
                continue
            objective_channel_id = objective.get("channel_id") or panel_channel_id
            add_target(
                objective_channel_id,
                objective.get("message_id"),
                f"{_CREATION_MARKER_PREFIX}:",
            )
            add_target(
                objective_channel_id,
                objective.get("notify_message_id"),
                f"{_NOTIFICATION_MARKER_PREFIX}:",
            )

    all_clean = True
    for (channel_id, message_id), marker_prefixes in targets.items():
        try:
            channel = await _resolve_text_channel(guild, channel_id)
            if channel is None:
                all_clean = False
                continue
            message = await channel.fetch_message(message_id)
            if _safe_int(getattr(getattr(message, "author", None), "id", 0)) != bot_user_id:
                all_clean = False
                continue
            await message_checkpoints.clean_message_checkpoint_prefixes(
                message,
                marker_prefixes,
            )
        except discord.NotFound:
            continue
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            all_clean = False
    return all_clean


async def reconcile_objective_actions(bot: discord.Client) -> None:
    """Converge pending objective Discord side effects from SQLite."""

    for guild in getattr(bot, "guilds", ()):
        async with _objective_guild_locks.hold(int(guild.id)):
            await _reconcile_objective_actions_for_guild(guild)


async def _reconcile_objective_actions_for_guild(guild: discord.Guild) -> None:
    for record in runtime_state.list_records(
        _PANEL_PUBLICATION_RUNTIME_KIND,
        guild_id=guild.id,
        statuses=(
            "pending",
            "message_ready",
            "old_cleanup_pending",
            "compensation_pending",
        ),
    ):
        try:
            if record.status == "compensation_pending":
                await _compensate_panel_publication(guild, record)
            else:
                await _complete_panel_publication(guild, record)
        except Exception:
            logging.exception(
                "Could not reconcile objectives panel publication in guild %s",
                guild.id,
            )

    for record in runtime_state.list_records(
        _CREATION_RUNTIME_KIND,
        guild_id=guild.id,
        statuses=(
            "pending",
            "role_ready",
            "message_ready",
            "compensation_pending",
        ),
    ):
        try:
            entry = _active_objectives_entry(guild.id)
            if _find_objective_by_key(entry, record.external_id) is not None:
                runtime_state.set_status(
                    _CREATION_RUNTIME_KIND,
                    guild.id,
                    record.external_id,
                    "completed",
                )
                completed_record = runtime_state.get_record(
                    _CREATION_RUNTIME_KIND,
                    guild.id,
                    record.external_id,
                )
                if completed_record is not None:
                    await _cleanup_completed_creation_record(guild, completed_record)
            elif record.status == "compensation_pending":
                await _compensate_pending_objective_creation(guild, record)
            else:
                await _complete_pending_objective_creation(guild, record)
        except Exception:
            logging.exception(
                "Could not reconcile pending objective %s in guild %s",
                record.external_id,
                guild.id,
            )

    for record in runtime_state.list_records(
        _NOTIFICATION_RUNTIME_KIND,
        guild_id=guild.id,
        statuses=("pending", "sent"),
    ):
        try:
            await _reconcile_pending_notification(guild, record)
        except Exception:
            logging.exception(
                "Could not reconcile objective notification %s in guild %s",
                record.external_id,
                guild.id,
            )

    for record in runtime_state.list_records(
        _PANEL_PUBLICATION_RUNTIME_KIND,
        guild_id=guild.id,
        statuses=("completed",),
    ):
        if record.status != "completed" or record.payload.get(_MESSAGE_CHECKPOINT_CLEANUP_FIELD):
            continue
        try:
            await _cleanup_completed_panel_record(guild, record)
        except Exception:
            logging.exception(
                "Could not clean completed objectives panel checkpoint in guild %s",
                guild.id,
            )

    for record in runtime_state.list_records(
        _CREATION_RUNTIME_KIND,
        guild_id=guild.id,
        statuses=("completed",),
    ):
        if record.status != "completed" or (
            record.payload.get(_MESSAGE_CHECKPOINT_CLEANUP_FIELD)
            and record.payload.get(_NOTIFY_ROLE_NAME_CLEANUP_FIELD)
        ):
            continue
        try:
            await _cleanup_completed_creation_record(guild, record)
        except Exception:
            logging.exception(
                "Could not clean completed objective %s in guild %s",
                record.external_id,
                guild.id,
            )

    for record in runtime_state.list_records(
        _NOTIFICATION_RUNTIME_KIND,
        guild_id=guild.id,
        statuses=("completed",),
    ):
        if record.status != "completed" or record.payload.get(_MESSAGE_CHECKPOINT_CLEANUP_FIELD):
            continue
        try:
            await _cleanup_completed_notification_record(guild, record)
        except Exception:
            logging.exception(
                "Could not clean completed objective notification %s in guild %s",
                record.external_id,
                guild.id,
            )

    try:
        active_messages_clean = await _clean_active_objective_message_checkpoints(guild)
    except Exception:
        logging.exception(
            "Could not sweep active objective message checkpoints in guild %s",
            guild.id,
        )
    else:
        if not active_messages_clean:
            logging.warning(
                "Some active objective messages still need checkpoint cleanup in guild %s",
                guild.id,
            )

    try:
        await _reconcile_active_notify_role_names(guild)
    except Exception:
        logging.exception(
            "Could not reconcile objective notification role names in guild %s",
            guild.id,
        )


def start_objectives_scheduler(bot: discord.Client) -> None:
    global _scheduler_task
    if _scheduler_task is not None and not _scheduler_task.done():
        return
    _scheduler_task = asyncio.create_task(
        _objectives_scheduler_loop(bot),
        name="realm-protector-objectives",
    )


async def stop_objectives_scheduler() -> None:
    """Stop the process-owned objectives worker during graceful shutdown."""

    global _scheduler_task
    task, _scheduler_task = _scheduler_task, None
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logging.exception("Objectives scheduler ended with an error during shutdown")


async def _objectives_scheduler_loop(bot: discord.Client) -> None:
    while True:
        try:
            now_ts = int(datetime.now(timezone.utc).timestamp())
            await _process_all_guilds(bot, now_ts)
        except Exception:
            logging.exception("Objectives scheduler tick failed")
        await asyncio.sleep(_SCHEDULER_INTERVAL_SECONDS)


async def _process_all_guilds(bot: discord.Client, now_ts: int) -> None:
    config = _load_config()

    for guild_id_str, entry in list(config.items()):
        if not isinstance(entry, dict):
            continue
        try:
            guild_id = int(guild_id_str)
        except ValueError:
            continue

        guild = bot.get_guild(guild_id)
        if guild is None:
            continue
        try:
            await _process_configured_guild(guild, entry, now_ts)
        except Exception:
            logging.exception(
                "Objectives scheduler failed for guild %s; continuing with other guilds",
                guild_id,
            )


async def _process_configured_guild(
    guild: discord.Guild,
    entry: dict,
    now_ts: int,
) -> None:
    async with _objective_guild_locks.hold(int(guild.id)):
        latest_entry = _load_guild_entry(guild.id)
        if isinstance(latest_entry, dict):
            entry = latest_entry
        await _process_configured_guild_locked(guild, entry, now_ts)


async def _process_configured_guild_locked(
    guild: discord.Guild,
    entry: dict,
    now_ts: int,
) -> None:
    if entry.get("disabled") or not guild_settings.get_target_guild(guild.id):
        await deactivate_guild_objective_configuration(guild)
        return

    (
        popped_remove_at_by_key,
        notified_ts_by_key,
        notify_message_id_by_key,
        cleared_notify_assets_keys,
        remove_keys,
        needs_panel_refresh,
    ) = await _process_guild(guild, entry, now_ts)
    if (
        popped_remove_at_by_key
        or notified_ts_by_key
        or notify_message_id_by_key
        or cleared_notify_assets_keys
        or remove_keys
    ):
        _apply_objective_deltas(
            guild.id,
            popped_remove_at_by_key,
            notified_ts_by_key,
            notify_message_id_by_key,
            cleared_notify_assets_keys,
            remove_keys,
        )
    if needs_panel_refresh:
        panel_channel_id, panel_message_id = get_objectives_panel_message(guild.id)
        if panel_channel_id and panel_message_id:
            await _refresh_panel_message(guild, panel_channel_id, panel_message_id)


def _objective_key(obj: dict) -> str:
    """Return the best stable identifier for matching objectives in config."""
    obj_id = str(obj.get("id") or "").strip()
    if obj_id:
        return f"id:{obj_id}"
    try:
        msg_id = int(obj.get("message_id") or 0)
    except (TypeError, ValueError):
        msg_id = 0
    if msg_id:
        return f"msg:{msg_id}"
    return ""


def _find_objective_by_key(entry: Optional[dict], objective_key: str) -> Optional[dict]:
    objectives = entry.get("objectives", []) if isinstance(entry, dict) else []
    for objective in objectives if isinstance(objectives, list) else ():
        if isinstance(objective, dict) and _objective_key(objective) == objective_key:
            return objective
    return None


async def _resolve_text_channel(
    guild: discord.Guild,
    channel_id: int,
) -> Optional[discord.TextChannel]:
    cached_channel = guild.get_channel(channel_id)
    if cached_channel is not None:
        return cached_channel if isinstance(cached_channel, discord.TextChannel) else None
    fetched_channel = await guild.fetch_channel(channel_id)
    return fetched_channel if isinstance(fetched_channel, discord.TextChannel) else None


async def _find_marked_message(
    channel: discord.TextChannel,
    marker: str,
    *,
    message_id: Optional[int] = None,
    bot_user_id: int = 0,
) -> Optional[discord.Message]:
    if message_id:
        try:
            message = await channel.fetch_message(int(message_id))
        except discord.NotFound:
            message = None
        if message is not None and _message_is_bot_owned(message, bot_user_id):
            return message
    async for message in channel.history(
        limit=_MARKER_FALLBACK_HISTORY_LIMIT,
        oldest_first=False,
    ):
        if not _message_is_bot_owned(message, bot_user_id):
            continue
        if message_checkpoints.message_has_checkpoint(message, marker):
            return message
    return None


def _runtime_payload(
    kind: str,
    guild_id: int,
    external_id: str,
    updates: dict,
) -> dict:
    record = runtime_state.get_record(kind, guild_id, external_id)
    payload = dict(record.payload) if record is not None else {}
    payload.update(updates)
    return payload


def _persist_record_cleanup_flag(
    kind: str,
    guild_id: int,
    external_id: str,
    payload: dict,
    *,
    status: str,
    field: str = _MESSAGE_CHECKPOINT_CLEANUP_FIELD,
) -> runtime_state.RuntimeRecord:
    updated_payload = dict(payload)
    updated_payload[field] = True
    return runtime_state.upsert_record(
        kind,
        guild_id,
        external_id,
        updated_payload,
        status=status,
    )


async def _clean_and_mark_message_checkpoint(
    kind: str,
    guild,
    external_id: str,
    payload: dict,
    *,
    status: str,
    message,
    marker: str,
) -> bool:
    if payload.get(_MESSAGE_CHECKPOINT_CLEANUP_FIELD):
        return True
    bot_user_id = _safe_int(getattr(getattr(guild, "me", None), "id", 0))
    if not _message_is_bot_owned(message, bot_user_id):
        logging.warning(
            "Refusing to clean objective checkpoint %s in guild %s because message ownership changed",
            external_id,
            guild.id,
        )
        return False
    if not await _clean_message_checkpoint(message, marker):
        return False
    try:
        _persist_record_cleanup_flag(
            kind,
            guild.id,
            external_id,
            payload,
            status=status,
        )
    except Exception:
        logging.exception(
            "Could not persist objective checkpoint cleanup for %s in guild %s",
            external_id,
            guild.id,
        )
        return False
    return True


async def _clean_record_message_checkpoint(
    guild,
    record: runtime_state.RuntimeRecord,
    *,
    channel_id: int,
    message_id: int,
    marker: str,
) -> bool:
    """Clean a committed message once and durably remember the migration."""

    if record.payload.get(_MESSAGE_CHECKPOINT_CLEANUP_FIELD):
        return True
    if not channel_id or not message_id:
        try:
            _persist_record_cleanup_flag(
                record.kind,
                record.guild_id,
                record.external_id,
                record.payload,
                status=record.status,
            )
        except Exception:
            return False
        else:
            return True
    try:
        channel = await _resolve_text_channel(guild, channel_id)
        if channel is None:
            return False
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        try:
            _persist_record_cleanup_flag(
                record.kind,
                record.guild_id,
                record.external_id,
                record.payload,
                status=record.status,
            )
        except Exception:
            return False
        else:
            return True
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return False
    return await _clean_and_mark_message_checkpoint(
        record.kind,
        guild,
        record.external_id,
        record.payload,
        status=record.status,
        message=message,
        marker=marker,
    )


def _apply_objective_deltas(
    guild_id: int,
    popped_remove_at_by_key: dict[str, int],
    notified_ts_by_key: dict[str, int],
    notify_message_id_by_key: dict[str, int],
    cleared_notify_assets_keys: set[str],
    remove_keys: set[str],
) -> None:
    """Apply scheduler changes to the *latest* config to avoid clobbering concurrent writes."""
    entry = _guild_entry(_load_guild_entry(guild_id))
    objectives = entry.get("objectives", [])
    if not isinstance(objectives, list) or not objectives:
        return

    new_objectives: list[dict] = []
    for obj in objectives:
        if not isinstance(obj, dict):
            continue
        key = _objective_key(obj)
        if key and key in remove_keys:
            continue
        if key and key in popped_remove_at_by_key:
            obj["remove_at_ts"] = int(popped_remove_at_by_key[key])
        if key and key in notified_ts_by_key:
            obj["notified_ts"] = int(notified_ts_by_key[key])
        if key and key in notify_message_id_by_key:
            obj["notify_message_id"] = int(notify_message_id_by_key[key])
        if key and key in cleared_notify_assets_keys:
            _clear_notify_role_reference(obj)
            obj.pop("notify_message_id", None)
        new_objectives.append(obj)

    entry["objectives"] = new_objectives
    _save_guild_entry(guild_id, entry)


async def _process_guild(
    guild: discord.Guild,
    entry: dict,
    now_ts: int,
) -> tuple[dict[str, int], dict[str, int], dict[str, int], set[str], set[str], bool]:
    objectives = entry.get("objectives")
    if not isinstance(objectives, list) or not objectives:
        return {}, {}, {}, set(), set(), False

    panel_channel_id = entry.get("panel_channel_id")
    try:
        panel_channel_id_int = int(panel_channel_id) if panel_channel_id else None
    except (TypeError, ValueError):
        panel_channel_id_int = None

    popped_remove_at_by_key: dict[str, int] = {}
    notified_ts_by_key: dict[str, int] = {}
    notify_message_id_by_key: dict[str, int] = {}
    cleared_notify_assets_keys: set[str] = set()
    remove_keys: set[str] = set()
    needs_panel_refresh = False

    for obj in list(objectives):
        if not isinstance(obj, dict):
            continue

        key = _objective_key(obj)
        try:
            legacy_role_id = int(obj.get("notify_role_id") or 0)
        except (TypeError, ValueError):
            legacy_role_id = 0
        legacy_role = guild.get_role(legacy_role_id) if legacy_role_id else None
        if (
            legacy_role is not None
            and legacy_role.mentionable
            and _notify_role_ownership_error(guild, obj, legacy_role) is None
        ):
            await _make_notification_role_nonmentionable(legacy_role)

        pop_at_ts = _safe_int(obj.get("pop_at_ts"))
        remove_at_ts = _safe_int(obj.get("remove_at_ts"))
        notified_ts = _safe_int(obj.get("notified_ts"))

        notify_before_minutes = obj.get("notify_before_minutes")
        try:
            notify_before_minutes_int = (
                int(notify_before_minutes) if notify_before_minutes is not None else 0
            )
        except (TypeError, ValueError):
            notify_before_minutes_int = 0

        if (
            notify_before_minutes_int in _NOTIFY_BEFORE_MINUTES_OPTIONS
            and pop_at_ts
            and not remove_at_ts
            and not notified_ts
        ):
            notify_at_ts = _safe_int(
                obj.get("notify_at_ts") or (pop_at_ts - notify_before_minutes_int * 60)
            )
            if notify_at_ts > 0 and now_ts >= notify_at_ts and now_ts < pop_at_ts:
                notify_msg_id = await _send_objective_notification(
                    guild,
                    obj,
                    panel_channel_id_int,
                    notify_before_minutes_int,
                    notified_ts=now_ts,
                )
                if key and not obj.get("notify_role_id"):
                    cleared_notify_assets_keys.add(key)
                if notify_msg_id:
                    obj["notified_ts"] = int(now_ts)
                    if key:
                        notified_ts_by_key[key] = int(now_ts)
                        notify_message_id_by_key[key] = int(notify_msg_id)

        # If the objective already popped (remove_at_ts set) but notify assets still exist,
        # keep trying to clean them up until the objective is removed.
        if (
            pop_at_ts
            and now_ts >= pop_at_ts
            and remove_at_ts
            and (obj.get("notify_role_id") or obj.get("notify_message_id"))
        ):
            if await _cleanup_objective_notification_assets(guild, obj, panel_channel_id_int):
                if key:
                    cleared_notify_assets_keys.add(key)

        if remove_at_ts and now_ts >= remove_at_ts:
            assets_clean = not (
                obj.get("notify_role_id") or obj.get("notify_message_id")
            ) or await _cleanup_objective_notification_assets(
                guild,
                obj,
                panel_channel_id_int,
            )
            message_deleted = await _delete_objective_message(
                guild,
                obj,
                panel_channel_id_int,
            )
            if key and assets_clean and message_deleted:
                remove_keys.add(key)
                needs_panel_refresh = True
            continue

        if pop_at_ts and now_ts >= pop_at_ts and not remove_at_ts:
            new_remove_at = now_ts + _OBJECTIVE_EXPIRY_SECONDS
            obj["remove_at_ts"] = new_remove_at
            if key:
                popped_remove_at_by_key[key] = int(new_remove_at)
            needs_panel_refresh = True
            if await _cleanup_objective_notification_assets(guild, obj, panel_channel_id_int):
                if key:
                    cleared_notify_assets_keys.add(key)
            await _mark_objective_popped(guild, obj, panel_channel_id_int)

    return (
        popped_remove_at_by_key,
        notified_ts_by_key,
        notify_message_id_by_key,
        cleared_notify_assets_keys,
        remove_keys,
        needs_panel_refresh,
    )


async def _send_objective_notification(
    guild: discord.Guild,
    obj: dict,
    fallback_channel_id: Optional[int],
    notify_before_minutes: int,
    *,
    notified_ts: Optional[int] = None,
) -> Optional[int]:
    role_id = obj.get("notify_role_id")
    try:
        role_id_int = int(role_id) if role_id else None
    except (TypeError, ValueError):
        role_id_int = None
    if not role_id_int:
        return None

    channel_id = obj.get("channel_id") or fallback_channel_id
    try:
        channel_id_int = int(channel_id) if channel_id else None
    except (TypeError, ValueError):
        channel_id_int = None
    if not channel_id_int:
        return None

    try:
        ch = await _resolve_text_channel(guild, channel_id_int)
        if ch is None:
            return None
        pop_at_ts = _safe_int(obj.get("pop_at_ts"))
        pop_part = _format_pop_time(pop_at_ts, obj.get("pop_time_utc"))
        role = guild.get_role(role_id_int)
        if role is None:
            _clear_notify_role_reference(obj)
            return None
        ownership_error = _notify_role_ownership_error(guild, obj, role)
        if ownership_error:
            logging.warning(
                "Skipping unsafe objective notification role %s in guild %s: %s",
                role_id_int,
                guild.id,
                ownership_error,
            )
            _clear_notify_role_reference(obj)
            return None
        if not await _make_notification_role_nonmentionable(role):
            return None
        subscribers = [member for member in role.members if not member.bot][
            :_MAX_OBJECTIVE_SUBSCRIBERS
        ]
        mentions = " ".join(member.mention for member in subscribers)
        content = f"{mentions} {_format_objective_name(obj)}. {pop_part}".strip()
        objective_key = _objective_key(obj)
        guild_id = _safe_int(getattr(guild, "id", 0))
        if not objective_key or not guild_id:
            direct_message = await ch.send(
                content,
                allowed_mentions=allowed_user_mentions(member.id for member in subscribers),
            )
            return int(direct_message.id)

        record = runtime_state.get_record(
            _NOTIFICATION_RUNTIME_KIND,
            guild_id,
            objective_key,
        )
        payload = dict(record.payload) if record is not None else {}
        notified_ts_value = _safe_int(
            payload.get("notified_ts") or notified_ts,
            int(datetime.now(timezone.utc).timestamp()),
        )
        marker = _notification_marker(guild_id, objective_key)
        payload.pop(_MESSAGE_CHECKPOINT_CLEANUP_FIELD, None)
        payload.update(
            {
                "objective_key": objective_key,
                "channel_id": int(channel_id_int),
                "role_id": int(role_id_int),
                "notify_before_minutes": int(notify_before_minutes),
                "notified_ts": int(notified_ts_value),
                "marker": marker,
            }
        )
        runtime_state.upsert_record(
            _NOTIFICATION_RUNTIME_KIND,
            guild_id,
            objective_key,
            payload,
            status="pending",
        )

        known_message_id = _safe_int(payload.get("message_id")) or None
        bot_user_id = _safe_int(getattr(getattr(guild, "me", None), "id", 0))
        sent_message = (
            await _find_marked_message(
                ch,
                marker,
                message_id=known_message_id,
                bot_user_id=bot_user_id,
            )
            if record is not None
            else None
        )
        if sent_message is None:
            sent_message = await ch.send(
                message_checkpoints.content_with_checkpoint(content, marker),
                nonce=message_checkpoints.stable_nonce(marker),
                allowed_mentions=allowed_user_mentions(member.id for member in subscribers),
            )

        payload["message_id"] = int(sent_message.id)
        runtime_state.upsert_record(
            _NOTIFICATION_RUNTIME_KIND,
            guild_id,
            objective_key,
            payload,
            status="sent",
        )
        latest_entry = _load_guild_entry(guild_id)
        if _find_objective_by_key(latest_entry, objective_key) is not None:
            _apply_objective_deltas(
                guild_id,
                {},
                {objective_key: int(notified_ts_value)},
                {objective_key: int(sent_message.id)},
                set(),
                set(),
            )
            runtime_state.set_status(
                _NOTIFICATION_RUNTIME_KIND,
                guild_id,
                objective_key,
                "completed",
            )
            await _clean_and_mark_message_checkpoint(
                _NOTIFICATION_RUNTIME_KIND,
                guild,
                objective_key,
                payload,
                status="completed",
                message=sent_message,
                marker=marker,
            )
        return int(sent_message.id)
    except (discord.Forbidden, discord.HTTPException, discord.NotFound):
        return None


def _clear_notify_role_reference(obj: dict) -> None:
    obj.pop("notify_role_id", None)
    obj.pop(_NOTIFY_ROLE_NAME_FIELD, None)
    obj.pop(_NOTIFY_ROLE_OWNERSHIP_FIELD, None)
    obj.pop(_NOTIFY_ROLE_NAME_CLEANUP_FIELD, None)


def _persist_notify_role_detachment(guild_id: int, obj: dict) -> None:
    key = _objective_key(obj)
    if not key:
        return
    _apply_objective_deltas(
        guild_id,
        {},
        {},
        {},
        {key},
        set(),
    )


def _notify_role_ownership_error(
    guild: discord.Guild,
    obj: dict,
    role: discord.Role,
) -> Optional[str]:
    """Return why a stored role must no longer be mutated or deleted by the bot."""

    if obj.get(_NOTIFY_ROLE_OWNERSHIP_FIELD) is not True:
        return "the objective has no bot-ownership marker"

    expected_names = _known_notify_role_names(guild.id, obj)
    stored_name = str(obj.get(_NOTIFY_ROLE_NAME_FIELD) or "").strip()
    if not stored_name or stored_name not in expected_names:
        return "the stored role fingerprint no longer matches the objective"
    if str(getattr(role, "name", "")) not in expected_names:
        return "the Discord role was renamed or repurposed"

    security_state = role_security.collect_role_security_state(guild.id)
    role_error = role_security.self_assignment_error(role, guild, security_state)
    if role_error:
        return role_error

    objective_id = str(obj.get("id") or "").strip()
    expected_source = f"objective notification {objective_id}" if objective_id else None
    role_sources = set(security_state.self_assignable_id_sources.get(int(role.id), ()))
    unexpected_sources = role_sources - ({expected_source} if expected_source else set())
    if unexpected_sources:
        return "the role is also used by another self-assignment workflow: " + ", ".join(
            sorted(unexpected_sources)
        )
    return None


async def _cleanup_objective_notification_assets(
    guild: discord.Guild,
    obj: dict,
    fallback_channel_id: Optional[int],
    *,
    role_delete_reason: str = "Objective completed",
) -> bool:
    """Delete notify role + notify ping message (if any). Returns True if fully cleaned or already missing."""
    all_clean = True

    # Delete the notification ping message.
    notify_message_id = obj.get("notify_message_id")
    objective_message_id = obj.get("message_id")
    try:
        notify_message_id_int = int(notify_message_id) if notify_message_id else None
    except (TypeError, ValueError):
        notify_message_id_int = None
    try:
        objective_message_id_int = int(objective_message_id) if objective_message_id else None
    except (TypeError, ValueError):
        objective_message_id_int = None

    if notify_message_id_int and notify_message_id_int != objective_message_id_int:
        channel_id = obj.get("channel_id") or fallback_channel_id
        try:
            channel_id_int = int(channel_id) if channel_id else None
        except (TypeError, ValueError):
            channel_id_int = None
        if channel_id_int:
            try:
                ch = await _resolve_text_channel(guild, channel_id_int)
                if ch is not None:
                    msg = await ch.fetch_message(notify_message_id_int)
                    await msg.delete()
                    obj.pop("notify_message_id", None)
                else:
                    all_clean = False
            except discord.NotFound:
                obj.pop("notify_message_id", None)
            except (discord.Forbidden, discord.HTTPException):
                all_clean = False
        else:
            all_clean = False

    # Delete the notification role.
    role_id = obj.get("notify_role_id")
    try:
        role_id_int = int(role_id) if role_id else None
    except (TypeError, ValueError):
        role_id_int = None
    if role_id_int:
        role_reference_count = 0
        entry = _load_guild_entry(guild.id)
        stored_objectives = entry.get("objectives", []) if isinstance(entry, dict) else []
        for stored_objective in stored_objectives:
            if not isinstance(stored_objective, dict):
                continue
            try:
                stored_role_id = int(stored_objective.get("notify_role_id") or 0)
            except (TypeError, ValueError):
                continue
            if stored_role_id == role_id_int:
                role_reference_count += 1

        # Older state could share a notification role between objectives. Detach
        # this objective but keep the role until the final reference is cleaned.
        if role_reference_count > 1:
            _clear_notify_role_reference(obj)
            return all_clean

        role = guild.get_role(role_id_int)
        if role is None:
            _clear_notify_role_reference(obj)
        else:
            ownership_error = _notify_role_ownership_error(guild, obj, role)
            if ownership_error:
                logging.warning(
                    "Leaving objective notification role %s in guild %s untouched: %s",
                    role_id_int,
                    guild.id,
                    ownership_error,
                )
                _clear_notify_role_reference(obj)
                return all_clean
            try:
                await role.delete(reason=role_delete_reason)
                _clear_notify_role_reference(obj)
            except (discord.Forbidden, discord.HTTPException):
                all_clean = False

    return all_clean


async def _refresh_panel_message(guild: discord.Guild, channel_id: int, message_id: int) -> None:
    try:
        ch = await _resolve_text_channel(guild, channel_id)
        if ch is None:
            return
        msg = await ch.fetch_message(message_id)
        await msg.edit(embed=_build_panel_embed(guild), view=ObjectivesPanelView())
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return


async def _mark_objective_popped(
    guild: discord.Guild, obj: dict, fallback_channel_id: Optional[int]
) -> None:
    await _edit_objective_message(guild, obj, fallback_channel_id)


async def _edit_objective_message(
    guild: discord.Guild, obj: dict, fallback_channel_id: Optional[int]
) -> None:
    message_id = obj.get("message_id")
    channel_id = obj.get("channel_id") or fallback_channel_id
    try:
        message_id_int = int(message_id) if message_id else None
        channel_id_int = int(channel_id) if channel_id else None
    except (TypeError, ValueError):
        return

    if not message_id_int or not channel_id_int:
        return

    try:
        ch = await _resolve_text_channel(guild, channel_id_int)
        if ch is None:
            return
        msg = await ch.fetch_message(message_id_int)
        await msg.edit(
            embed=_build_objective_embed(obj),
            view=ObjectiveMessageView(),
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return


async def _delete_objective_message(
    guild: discord.Guild,
    obj: dict,
    fallback_channel_id: Optional[int],
) -> bool:
    message_id = obj.get("message_id")
    channel_id = obj.get("channel_id") or fallback_channel_id
    try:
        message_id_int = int(message_id) if message_id else None
        channel_id_int = int(channel_id) if channel_id else None
    except (TypeError, ValueError):
        return True

    if not message_id_int or not channel_id_int:
        return True

    try:
        ch = await _resolve_text_channel(guild, channel_id_int)
        if ch is None:
            return False
        msg = await ch.fetch_message(message_id_int)
        await msg.delete()
        return True
    except discord.NotFound:
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def _send_ephemeral_notice(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


def _persist_panel_publication(
    guild_id: int,
    target_channel_id: int,
    *,
    status: str,
    message_id: Optional[int] = None,
    previous_channel_id: Optional[int] = None,
    previous_message_id: Optional[int] = None,
) -> runtime_state.RuntimeRecord:
    existing = runtime_state.get_record(
        _PANEL_PUBLICATION_RUNTIME_KIND,
        guild_id,
        "panel",
    )
    payload = dict(existing.payload) if existing is not None else {}
    payload["target_channel_id"] = int(target_channel_id)
    if status == "pending":
        payload["marker"] = _new_panel_marker(guild_id)
        payload.pop(_MESSAGE_CHECKPOINT_CLEANUP_FIELD, None)
    else:
        payload.setdefault("marker", _panel_marker(guild_id))
    if message_id:
        payload["message_id"] = int(message_id)
    elif status == "pending":
        payload.pop("message_id", None)
    if previous_channel_id and previous_message_id:
        payload["previous_channel_id"] = int(previous_channel_id)
        payload["previous_message_id"] = int(previous_message_id)
    elif status == "pending":
        payload.pop("previous_channel_id", None)
        payload.pop("previous_message_id", None)
    return runtime_state.upsert_record(
        _PANEL_PUBLICATION_RUNTIME_KIND,
        guild_id,
        "panel",
        payload,
        status=status,
    )


async def _compensate_panel_publication(
    guild,
    record: runtime_state.RuntimeRecord,
) -> bool:
    channel_id = _safe_int(record.payload.get("target_channel_id"))
    marker = str(record.payload.get("marker") or _panel_marker(guild.id))
    all_clean = True
    if channel_id:
        try:
            channel = await _resolve_text_channel(guild, channel_id)
            if channel is not None:
                message = await _find_marked_message(
                    channel,
                    marker,
                    message_id=_safe_int(record.payload.get("message_id")) or None,
                    bot_user_id=_safe_int(getattr(getattr(guild, "me", None), "id", 0)),
                )
                if message is not None:
                    await message.delete()
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            all_clean = False
    runtime_state.set_status(
        _PANEL_PUBLICATION_RUNTIME_KIND,
        guild.id,
        "panel",
        "cancelled" if all_clean else "compensation_pending",
    )
    return all_clean


async def _disable_previous_objectives_panel_message(
    guild: discord.Guild,
    record: runtime_state.RuntimeRecord,
) -> bool:
    channel_id = _safe_int(record.payload.get("previous_channel_id"))
    message_id = _safe_int(record.payload.get("previous_message_id"))
    if not channel_id or not message_id:
        return True
    try:
        channel = await _resolve_text_channel(guild, channel_id)
        if channel is None:
            return False
        message = await channel.fetch_message(message_id)
        try:
            await message.delete()
        except discord.Forbidden:
            await message.edit(
                content="This objectives panel has been replaced and is no longer active.",
                embed=None,
                view=None,
            )
        return True
    except discord.NotFound:
        return True
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return False


async def _complete_panel_publication(
    guild,
    record: runtime_state.RuntimeRecord,
    *,
    actor: Optional[discord.Member] = None,
    discover_existing: bool = True,
):
    target_channel_id = _safe_int(record.payload.get("target_channel_id"))
    if not target_channel_id:
        await _compensate_panel_publication(guild, record)
        return None
    stored_entry = _load_guild_entry(guild.id)
    if not guild_settings.get_target_guild(guild.id) or (
        isinstance(stored_entry, dict) and stored_entry.get("disabled")
    ):
        await _compensate_panel_publication(guild, record)
        return None
    try:
        channel = await _resolve_text_channel(guild, target_channel_id)
        if channel is None:
            return None
        marker = str(record.payload.get("marker") or _panel_marker(guild.id))
        known_message_id = _safe_int(record.payload.get("message_id")) or None
        message = (
            await _find_marked_message(
                channel,
                marker,
                message_id=known_message_id,
                bot_user_id=_safe_int(getattr(getattr(guild, "me", None), "id", 0)),
            )
            if discover_existing or known_message_id
            else None
        )
        if message is None:
            message = await channel.send(
                content=message_checkpoints.hidden_checkpoint(marker),
                embed=_build_panel_embed(guild),
                view=ObjectivesPanelView(),
                nonce=message_checkpoints.stable_nonce(marker),
            )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
        return None

    record = _persist_panel_publication(
        guild.id,
        target_channel_id,
        status="message_ready",
        message_id=int(message.id),
    )
    if actor is not None and not _objective_setup_is_authorized(guild, actor):
        await _compensate_panel_publication(guild, record)
        return None
    if not set_objectives_panel_message(guild.id, target_channel_id, int(message.id)):
        await _compensate_panel_publication(guild, record)
        return None
    if not await _disable_previous_objectives_panel_message(guild, record):
        final_status = "old_cleanup_pending"
    else:
        final_status = "completed"
    runtime_state.set_status(
        _PANEL_PUBLICATION_RUNTIME_KIND,
        guild.id,
        "panel",
        final_status,
    )
    await _clean_and_mark_message_checkpoint(
        _PANEL_PUBLICATION_RUNTIME_KIND,
        guild,
        "panel",
        record.payload,
        status=final_status,
        message=message,
        marker=marker,
    )
    return int(message.id)


async def post_or_update_objectives_panel(
    guild: discord.Guild,
    channel: discord.abc.Messageable,
    actor: discord.Member,
) -> tuple[bool, str]:
    async with _objective_guild_locks.hold(int(guild.id)):
        return await _post_or_update_objectives_panel_locked(guild, channel, actor)


async def _post_or_update_objectives_panel_locked(
    guild: discord.Guild,
    channel: discord.abc.Messageable,
    actor: discord.Member,
) -> tuple[bool, str]:
    if not _objective_setup_is_authorized(guild, actor):
        return False, "This server is no longer configured or you are no longer an Administrator."
    stored_entry = _load_guild_entry(guild.id)
    if isinstance(stored_entry, dict) and stored_entry.get("disabled"):
        await deactivate_guild_objective_configuration(guild)
        remaining_entry = _load_guild_entry(guild.id)
        if isinstance(remaining_entry, dict) and remaining_entry.get("disabled"):
            return (
                False,
                "Previous objective cleanup is still pending. Fix my channel/role permissions and try again.",
            )

    embed = _build_panel_embed(guild)
    panel_channel_id, panel_message_id = get_objectives_panel_message(guild.id)

    target_channel_id = getattr(channel, "id", None)
    if not isinstance(target_channel_id, int):
        target_channel_id = None

    if panel_channel_id and panel_message_id:
        if target_channel_id is not None and int(panel_channel_id) == int(target_channel_id):
            try:
                existing_channel = await _resolve_text_channel(guild, panel_channel_id)

                if existing_channel is not None:
                    existing_message = await existing_channel.fetch_message(panel_message_id)
                    await existing_message.edit(embed=embed, view=ObjectivesPanelView())
                    if not _objective_setup_is_authorized(
                        guild, actor
                    ) or not _objectives_panel_is_current(
                        guild.id,
                        panel_channel_id,
                        panel_message_id,
                    ):
                        try:
                            await existing_message.edit(
                                content="This objectives panel has been disabled.",
                                embed=None,
                                view=None,
                            )
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            pass
                        return False, "Setup changed while the objectives panel was being updated."
                    return True, "Objectives panel updated."
            except discord.NotFound:
                pass
            except discord.Forbidden:
                return False, "Missing permission to update the existing objectives panel."
            except discord.HTTPException:
                return False, "Failed to update the existing objectives panel."
    if target_channel_id is None:
        return False, "The target channel has no usable Discord ID."
    publication_record = _persist_panel_publication(
        guild.id,
        target_channel_id,
        status="pending",
        previous_channel_id=panel_channel_id,
        previous_message_id=panel_message_id,
    )

    completed_message_id = await _complete_panel_publication(
        guild,
        publication_record,
        actor=actor,
        discover_existing=False,
    )
    if not completed_message_id:
        return (
            False,
            "The objectives panel could not be completed. Its pending state will be reconciled after restart.",
        )
    return True, "Objectives panel posted."


async def handle_set_objectivess_panel(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    channel = interaction.channel
    if guild is None or channel is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.", ephemeral=True
        )
        return
    if not isinstance(channel, discord.abc.Messageable):
        await interaction.response.send_message(
            "This command must be used in a message channel.",
            ephemeral=True,
        )
        return

    if not guild_settings.get_target_guild(guild.id):
        await interaction.response.send_message(
            "This server is not configured yet. Run **/bot-setup** first.",
            ephemeral=True,
        )
        return

    member = interaction.user
    if not isinstance(member, discord.Member) or not await authorization.is_admin(member):
        await interaction.response.send_message(
            "You don't have permission to use this command.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    ok, message = await post_or_update_objectives_panel(
        guild,
        channel,
        member,
    )
    await interaction.followup.send(
        message if ok else f"Error: {message}",
        ephemeral=True,
    )


class ObjectivesPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(_AddObjectiveButton())


class _AddObjectiveButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Add Objective", style=discord.ButtonStyle.primary, custom_id="obj:add"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await _send_ephemeral_notice(interaction, "This can only be used inside a server.")
            return
        if interaction.message is None or not _objectives_panel_is_current(
            interaction.guild.id,
            getattr(interaction.channel, "id", None),
            interaction.message.id,
        ):
            await _send_ephemeral_notice(
                interaction,
                "This objectives panel is no longer active. Ask an admin to post a current panel.",
            )
            return
        if not isinstance(interaction.user, discord.Member) or not _objective_actor_can_manage(
            interaction.guild,
            interaction.user,
        ):
            await _send_ephemeral_notice(
                interaction,
                "Only an administrator or configured Caller can add objectives.",
            )
            return

        view = ObjectiveWizardView(interaction.user.id)
        embed = _build_wizard_embed(view)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


def register_persistent_views(bot) -> None:
    bot.add_view(ObjectivesPanelView())
    bot.add_view(ObjectiveMessageView())


class ObjectiveMessageView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(_NotifyMeButton())
        self.add_item(_RemoveObjectiveButton())


def _find_objective_by_message_id(guild_id: int, message_id: int) -> Optional[dict]:
    entry = _active_objectives_entry(guild_id)
    if entry is None:
        return None
    objectives = entry.get("objectives", [])
    if not isinstance(objectives, list) or not objectives:
        return None

    for obj in objectives:
        if not isinstance(obj, dict):
            continue
        try:
            obj_msg_id = int(obj.get("message_id") or 0)
        except (TypeError, ValueError):
            obj_msg_id = 0
        if obj_msg_id == int(message_id):
            return obj

    return None


class _NotifyMeButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Notify Me", style=discord.ButtonStyle.primary, custom_id="obj:notifyme"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await _send_ephemeral_notice(interaction, "This can only be used inside a server.")
            return

        if not isinstance(interaction.user, discord.Member):
            await _send_ephemeral_notice(interaction, "Unable to assign roles for this user.")
            return

        if interaction.message is None:
            await _send_ephemeral_notice(interaction, "Unable to locate objective message.")
            return

        objective = _find_objective_by_message_id(interaction.guild.id, interaction.message.id)
        if not objective:
            await _send_ephemeral_notice(
                interaction, "Objective not found (it may have been removed already)."
            )
            return

        role_id = objective.get("notify_role_id")
        try:
            role_id_int = int(role_id) if role_id else None
        except (TypeError, ValueError):
            role_id_int = None
        if not role_id_int:
            await _send_ephemeral_notice(
                interaction,
                "This objective has no notification role (role creation may have failed).",
            )
            return

        await interaction.response.defer(ephemeral=True)
        await _subscribe_member_to_objective(
            interaction,
            message_id=int(interaction.message.id),
            role_id=role_id_int,
        )


async def _subscribe_member_to_objective(
    interaction: discord.Interaction,
    *,
    message_id: int,
    role_id: int,
) -> None:
    guild = interaction.guild
    if guild is None:
        await _send_ephemeral_notice(interaction, "This can only be used inside a server.")
        return
    async with _objective_guild_locks.hold(int(guild.id)):
        await _subscribe_member_to_objective_locked(
            interaction,
            message_id=message_id,
            role_id=role_id,
        )


async def _subscribe_member_to_objective_locked(
    interaction: discord.Interaction,
    *,
    message_id: int,
    role_id: int,
) -> None:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await _send_ephemeral_notice(interaction, "Unable to assign roles for this user.")
        return
    lock_key = (int(guild.id), int(member.id), int(role_id))
    async with _notify_member_locks.hold(lock_key):
        objective = _find_objective_by_message_id(guild.id, message_id)
        if not objective or _safe_int(objective.get("notify_role_id")) != int(role_id):
            await _send_ephemeral_notice(
                interaction,
                "Objective not found (it may have been removed already).",
            )
            return
        role = guild.get_role(role_id)
        if role is None:
            _persist_notify_role_detachment(guild.id, objective)
            await _send_ephemeral_notice(interaction, "Notification role not found on this server.")
            return
        ownership_error = _notify_role_ownership_error(
            guild,
            objective,
            role,
        )
        if ownership_error:
            _persist_notify_role_detachment(guild.id, objective)
            await _send_ephemeral_notice(
                interaction,
                "This objective's notification role was changed or reused, so I will not assign it. "
                "Ask an admin to recreate the objective.",
            )
            return
        if not await _make_notification_role_nonmentionable(role):
            await _send_ephemeral_notice(
                interaction,
                "I could not secure this legacy notification role. Ask an admin to make it non-mentionable or fix my Manage Roles permission.",
            )
            return
        if (
            role not in member.roles
            and len([member for member in role.members if not member.bot])
            >= _MAX_OBJECTIVE_SUBSCRIBERS
        ):
            await _send_ephemeral_notice(
                interaction,
                "This objective already has the maximum number of notification subscribers.",
            )
            return
        try:
            if role not in member.roles:
                await member.add_roles(role, reason="Objective notify-me")
        except discord.Forbidden:
            await _send_ephemeral_notice(
                interaction,
                "Missing permission to assign that role. Make sure the bot has Manage Roles and is above the role.",
            )
            return
        except discord.HTTPException:
            await _send_ephemeral_notice(interaction, "Failed to assign notification role.")
            return
        await _send_ephemeral_notice(
            interaction, "Done. You will be pinged before this objective pops."
        )


class _RemoveObjectiveButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Remove Objective", style=discord.ButtonStyle.danger, custom_id="obj:remove"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await _send_ephemeral_notice(interaction, "This can only be used inside a server.")
            return

        if not isinstance(interaction.user, discord.Member):
            await _send_ephemeral_notice(
                interaction, "You don't have permission to remove objectives."
            )
            return

        caller_roles = guild_settings.get_caller_roles(interaction.guild.id)
        if not composition.has_caller_access(
            interaction.user,
            caller_roles,
            guild_settings.get_caller_role_ids(interaction.guild.id),
        ):
            await _send_ephemeral_notice(
                interaction, "You don't have permission to remove objectives."
            )
            return

        if interaction.message is None:
            await _send_ephemeral_notice(interaction, "Unable to locate objective message.")
            return

        objective = _find_objective_by_message_id(interaction.guild.id, interaction.message.id)
        if objective is None:
            await _send_ephemeral_notice(
                interaction, "Objective not found (it may have been removed already)."
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with _objective_guild_locks.hold(int(interaction.guild.id)):
            objective = _find_objective_by_message_id(
                interaction.guild.id,
                interaction.message.id,
            )
            if objective is None:
                await _send_ephemeral_notice(
                    interaction,
                    "Objective not found (it may have been removed already).",
                )
                return
            panel_channel_id, panel_message_id = get_objectives_panel_message(interaction.guild.id)
            assets_cleaned = await _cleanup_objective_notification_assets(
                interaction.guild,
                objective,
                panel_channel_id,
                role_delete_reason=f"Objective removed by {interaction.user}",
            )
            if not assets_cleaned:
                await _send_ephemeral_notice(
                    interaction,
                    "I couldn't remove the objective notification message or role. "
                    "The objective was kept so you can fix my permissions and try again.",
                )
                return

            removed = _remove_objective_by_message_id(interaction.guild.id, interaction.message.id)
            if not removed:
                await _send_ephemeral_notice(
                    interaction, "Objective not found (it may have been removed already)."
                )
                return

            try:
                await interaction.message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

            if panel_channel_id and panel_message_id:
                await _refresh_panel_message(interaction.guild, panel_channel_id, panel_message_id)

            await _send_ephemeral_notice(interaction, "Objective removed.")


def _remove_objective_by_message_id(guild_id: int, message_id: int) -> bool:
    entry = _guild_entry(_load_guild_entry(guild_id))
    objectives = entry.get("objectives", [])
    if not isinstance(objectives, list) or not objectives:
        return False

    for obj in list(objectives):
        if not isinstance(obj, dict):
            continue
        try:
            obj_msg_id = int(obj.get("message_id") or 0)
        except (TypeError, ValueError):
            obj_msg_id = 0
        if obj_msg_id == int(message_id):
            try:
                objectives.remove(obj)
            except ValueError:
                pass
            entry["objectives"] = objectives
            _save_guild_entry(guild_id, entry)
            return True

    return False


@dataclass
class _WizardState:
    objective_type: Optional[str] = None
    vortex_rarity: Optional[str] = None
    node_type: Optional[str] = None
    node_tier: Optional[str] = None
    pop_time_utc: Optional[str] = None
    pop_at_ts: Optional[int] = None
    map_name: Optional[str] = None
    notify_before_minutes: Optional[int] = None


class ObjectiveWizardView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.step = 1
        self.state = _WizardState()
        self._submitting = False
        self._build_items()

    def _total_steps(self) -> int:
        if self.state.objective_type == _OBJECTIVE_TYPE_NODE:
            return 7
        if self.state.objective_type in (_OBJECTIVE_TYPE_VORTEX, _OBJECTIVE_TYPE_CORE):
            return 6
        return 1

    def _build_items(self) -> None:
        self.clear_items()

        if self.step == 1:
            self.add_item(_ObjectiveTypeSelect())
            self.add_item(_WizardSaveContinueButton())
            self.add_item(_WizardCancelButton())
            return

        if self.state.objective_type == _OBJECTIVE_TYPE_VORTEX:
            self._build_vortex_items()
        elif self.state.objective_type == _OBJECTIVE_TYPE_CORE:
            self._build_core_items()
        elif self.state.objective_type == _OBJECTIVE_TYPE_NODE:
            self._build_node_items()
        else:
            self.step = 1
            self._build_items()

    def _build_core_items(self) -> None:
        if self.step == 2:
            self.add_item(_WizardBackButton())
            self.add_item(_CoreRaritySelect())
            self.add_item(_WizardSaveContinueButton())
            return

        if self.step == 3:
            self.add_item(_WizardBackButton())
            self.add_item(_SetPopTimeButton())
            self.add_item(_WizardSaveContinueButton())
            return

        if self.step == 4:
            self.add_item(_WizardBackButton())
            self.add_item(_SetMapButton())
            self.add_item(_WizardSaveContinueButton())
            return

        if self.step == 5:
            self.add_item(_WizardBackButton())
            self.add_item(_NotifyBeforeSelect())
            self.add_item(_WizardSaveContinueButton())
            return

        if self.step == 6:
            self.add_item(_WizardBackButton())
            self.add_item(_WizardConfirmButton())
            self.add_item(_WizardCancelButton())
            return

        self.step = 1
        self._build_items()

    def _build_vortex_items(self) -> None:
        if self.step == 2:
            self.add_item(_WizardBackButton())
            self.add_item(_VortexRaritySelect())
            self.add_item(_WizardSaveContinueButton())
            return

        if self.step == 3:
            self.add_item(_WizardBackButton())
            self.add_item(_SetPopTimeButton())
            self.add_item(_WizardSaveContinueButton())
            return

        if self.step == 4:
            self.add_item(_WizardBackButton())
            self.add_item(_SetMapButton())
            self.add_item(_WizardSaveContinueButton())
            return

        if self.step == 5:
            self.add_item(_WizardBackButton())
            self.add_item(_NotifyBeforeSelect())
            self.add_item(_WizardSaveContinueButton())
            return

        if self.step == 6:
            self.add_item(_WizardBackButton())
            self.add_item(_WizardConfirmButton())
            self.add_item(_WizardCancelButton())
            return

        self.step = 1
        self._build_items()

    def _build_node_items(self) -> None:
        if self.step == 2:
            self.add_item(_WizardBackButton())
            self.add_item(_NodeTypeSelect())
            self.add_item(_WizardSaveContinueButton())
            return

        if self.step == 3:
            self.add_item(_WizardBackButton())
            self.add_item(_NodeTierSelect())
            self.add_item(_WizardSaveContinueButton())
            return

        if self.step == 4:
            self.add_item(_WizardBackButton())
            self.add_item(_SetPopTimeButton())
            self.add_item(_WizardSaveContinueButton())
            return

        if self.step == 5:
            self.add_item(_WizardBackButton())
            self.add_item(_SetMapButton())
            self.add_item(_WizardSaveContinueButton())
            return

        if self.step == 6:
            self.add_item(_WizardBackButton())
            self.add_item(_NotifyBeforeSelect())
            self.add_item(_WizardSaveContinueButton())
            return

        if self.step == 7:
            self.add_item(_WizardBackButton())
            self.add_item(_WizardConfirmButton())
            self.add_item(_WizardCancelButton())
            return

        self.step = 1
        self._build_items()


def _build_wizard_embed(view: ObjectiveWizardView) -> discord.Embed:
    total_steps = view._total_steps()
    step = view.step
    embed = discord.Embed(title=f"Add Objective — Step {step}/{total_steps}")

    if step == 1:
        embed.description = "## :dart: Select objective type"
        embed.add_field(
            name="Selected type",
            value=view.state.objective_type or "Not selected yet",
            inline=False,
        )
        return embed

    if view.state.objective_type == _OBJECTIVE_TYPE_VORTEX:
        if step == 2:
            embed.description = "## :sparkles: Select vortex rarity"
            embed.add_field(
                name="Selected rarity",
                value=_vortex_rarity_display(view.state.vortex_rarity),
                inline=False,
            )
        elif step == 3:
            embed.description = "## :alarm_clock: Set pop time in UTC"
            embed.add_field(
                name="Pop time preview",
                value=_format_pop_time(view.state.pop_at_ts or 0, view.state.pop_time_utc)
                if view.state.pop_time_utc
                else "Time not set yet",
                inline=False,
            )
        elif step == 4:
            embed.description = "## :map: Set objective map"
            embed.add_field(
                name="Map preview",
                value=view.state.map_name or "Map not set yet",
                inline=False,
            )
        elif step == 5:
            embed.description = "## :bell: Notify before objective pop"
            embed.add_field(
                name="Notify before pop",
                value=_notify_before_display(view.state.notify_before_minutes),
                inline=False,
            )
        else:
            embed.description = "## :clipboard: Final objective preview and confirmation"
            embed.add_field(name="Type", value=_OBJECTIVE_TYPE_VORTEX, inline=True)
            embed.add_field(
                name="Rarity", value=_vortex_rarity_display(view.state.vortex_rarity), inline=True
            )
            embed.add_field(
                name="Map", value=view.state.map_name or "Map not set yet", inline=False
            )
            embed.add_field(
                name="Notify before pop",
                value=_notify_before_display(view.state.notify_before_minutes),
                inline=False,
            )
            embed.add_field(
                name="Pop time",
                value=_format_pop_time(view.state.pop_at_ts or 0, view.state.pop_time_utc)
                if view.state.pop_time_utc
                else "Time not set yet",
                inline=False,
            )
        return embed

    if view.state.objective_type == _OBJECTIVE_TYPE_CORE:
        if step == 2:
            embed.description = "## :sparkles: Select core rarity"
            embed.add_field(
                name="Selected rarity",
                value=_vortex_rarity_display(view.state.vortex_rarity),
                inline=False,
            )
        elif step == 3:
            embed.description = "## :alarm_clock: Set pop time in UTC"
            embed.add_field(
                name="Pop time preview",
                value=_format_pop_time(view.state.pop_at_ts or 0, view.state.pop_time_utc)
                if view.state.pop_time_utc
                else "Time not set yet",
                inline=False,
            )
        elif step == 4:
            embed.description = "## :map: Set objective map"
            embed.add_field(
                name="Map preview",
                value=view.state.map_name or "Map not set yet",
                inline=False,
            )
        elif step == 5:
            embed.description = "## :bell: Notify before objective pop"
            embed.add_field(
                name="Notify before pop",
                value=_notify_before_display(view.state.notify_before_minutes),
                inline=False,
            )
        else:
            embed.description = "## :clipboard: Final objective preview and confirmation"
            embed.add_field(name="Type", value=_OBJECTIVE_TYPE_CORE, inline=True)
            embed.add_field(
                name="Rarity", value=_vortex_rarity_display(view.state.vortex_rarity), inline=True
            )
            embed.add_field(
                name="Map", value=view.state.map_name or "Map not set yet", inline=False
            )
            embed.add_field(
                name="Notify before pop",
                value=_notify_before_display(view.state.notify_before_minutes),
                inline=False,
            )
            embed.add_field(
                name="Pop time",
                value=_format_pop_time(view.state.pop_at_ts or 0, view.state.pop_time_utc)
                if view.state.pop_time_utc
                else "Time not set yet",
                inline=False,
            )
        return embed

    if view.state.objective_type == _OBJECTIVE_TYPE_NODE:
        if step == 2:
            embed.description = "## :sparkles: Select node type"
            embed.add_field(
                name="Selected type", value=view.state.node_type or "Not selected yet", inline=False
            )
        elif step == 3:
            embed.description = "## :sparkles: Select node tier"
            embed.add_field(
                name="Selected tier", value=view.state.node_tier or "Not selected yet", inline=False
            )
        elif step == 4:
            embed.description = "## :alarm_clock: Set pop time in UTC"
            embed.add_field(
                name="Pop time preview",
                value=_format_pop_time(view.state.pop_at_ts or 0, view.state.pop_time_utc)
                if view.state.pop_time_utc
                else "Time not set yet",
                inline=False,
            )
        elif step == 5:
            embed.description = "## :map: Set objective map"
            embed.add_field(
                name="Map preview", value=view.state.map_name or "Map not set yet", inline=False
            )
        elif step == 6:
            embed.description = "## :bell: Notify before objective pop"
            embed.add_field(
                name="Notify before pop",
                value=_notify_before_display(view.state.notify_before_minutes),
                inline=False,
            )
        else:
            embed.description = "## :clipboard: Final objective preview and confirmation"
            embed.add_field(name="Type", value=_OBJECTIVE_TYPE_NODE, inline=True)
            embed.add_field(
                name="Node", value=view.state.node_type or "Not selected yet", inline=True
            )
            embed.add_field(
                name="Tier", value=view.state.node_tier or "Not selected yet", inline=True
            )
            embed.add_field(
                name="Map", value=view.state.map_name or "Map not set yet", inline=False
            )
            embed.add_field(
                name="Notify before pop",
                value=_notify_before_display(view.state.notify_before_minutes),
                inline=False,
            )
            embed.add_field(
                name="Pop time",
                value=_format_pop_time(view.state.pop_at_ts or 0, view.state.pop_time_utc)
                if view.state.pop_time_utc
                else "Time not set yet",
                inline=False,
            )
        return embed

    embed.description = "## :dart: Select objective type"
    return embed


class _ObjectiveTypeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=_OBJECTIVE_TYPE_VORTEX, value=_OBJECTIVE_TYPE_VORTEX),
            discord.SelectOption(label=_OBJECTIVE_TYPE_CORE, value=_OBJECTIVE_TYPE_CORE),
            discord.SelectOption(label=_OBJECTIVE_TYPE_NODE, value=_OBJECTIVE_TYPE_NODE),
        ]
        super().__init__(
            placeholder="Select objective type", options=options, min_values=1, max_values=1
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ObjectiveWizardView):
            return
        if interaction.user.id != view.user_id:
            return

        view.state.objective_type = self.values[0]
        view._build_items()
        await interaction.response.edit_message(embed=_build_wizard_embed(view), view=view)


class _VortexRaritySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=_vortex_rarity_display(r), value=r) for r in _VORTEX_RARITIES
        ]
        super().__init__(
            placeholder="Select vortex rarity", options=options, min_values=1, max_values=1
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ObjectiveWizardView):
            return
        if interaction.user.id != view.user_id:
            return
        view.state.vortex_rarity = self.values[0]
        await interaction.response.edit_message(embed=_build_wizard_embed(view), view=view)


class _CoreRaritySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=_vortex_rarity_display(r), value=r) for r in _VORTEX_RARITIES
        ]
        super().__init__(
            placeholder="Select core rarity", options=options, min_values=1, max_values=1
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ObjectiveWizardView):
            return
        if interaction.user.id != view.user_id:
            return
        view.state.vortex_rarity = self.values[0]
        await interaction.response.edit_message(embed=_build_wizard_embed(view), view=view)


class _NodeTypeSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=t, value=t) for t in _NODE_TYPES]
        super().__init__(
            placeholder="Select node type", options=options, min_values=1, max_values=1
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ObjectiveWizardView):
            return
        if interaction.user.id != view.user_id:
            return
        view.state.node_type = self.values[0]
        await interaction.response.edit_message(embed=_build_wizard_embed(view), view=view)


class _NodeTierSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=t, value=t) for t in _NODE_TIERS]
        super().__init__(
            placeholder="Select node tier", options=options, min_values=1, max_values=1
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ObjectiveWizardView):
            return
        if interaction.user.id != view.user_id:
            return
        view.state.node_tier = self.values[0]
        await interaction.response.edit_message(embed=_build_wizard_embed(view), view=view)


class _NotifyBeforeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=f"{m} minutes", value=str(m))
            for m in _NOTIFY_BEFORE_MINUTES_OPTIONS
        ]
        super().__init__(
            placeholder="Notify before pop (minutes)",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ObjectiveWizardView):
            return
        if interaction.user.id != view.user_id:
            return
        try:
            minutes = int(self.values[0])
        except (TypeError, ValueError):
            minutes = 0
        if minutes not in _NOTIFY_BEFORE_MINUTES_OPTIONS:
            return
        view.state.notify_before_minutes = minutes
        await interaction.response.edit_message(embed=_build_wizard_embed(view), view=view)


class _WizardCancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ObjectiveWizardView):
            return
        if interaction.user.id != view.user_id:
            return

        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)


class _WizardBackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ObjectiveWizardView):
            return
        if interaction.user.id != view.user_id:
            return

        if view.step > 1:
            view.step -= 1
        view._build_items()
        await interaction.response.edit_message(embed=_build_wizard_embed(view), view=view)


class _WizardSaveContinueButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Save and Continue", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ObjectiveWizardView):
            return
        if interaction.user.id != view.user_id:
            return

        error = _validate_step(view)
        if error:
            await _send_ephemeral_notice(interaction, error)
            return

        view.step += 1
        view._build_items()
        await interaction.response.edit_message(embed=_build_wizard_embed(view), view=view)


def _validate_step(view: ObjectiveWizardView) -> Optional[str]:
    if view.step == 1:
        if view.state.objective_type not in (
            _OBJECTIVE_TYPE_VORTEX,
            _OBJECTIVE_TYPE_CORE,
            _OBJECTIVE_TYPE_NODE,
        ):
            return "Please select objective type first."
        return None

    if view.state.objective_type == _OBJECTIVE_TYPE_VORTEX:
        if view.step == 2 and view.state.vortex_rarity not in _VORTEX_RARITIES:
            return "Please select vortex rarity first."
        if view.step == 3 and not view.state.pop_time_utc:
            return "Please set pop time first."
        if view.step == 4 and not view.state.map_name:
            return "Please set objective map first."
        if (
            view.step == 5
            and view.state.notify_before_minutes not in _NOTIFY_BEFORE_MINUTES_OPTIONS
        ):
            return "Please select when to notify before pop."
        return None

    if view.state.objective_type == _OBJECTIVE_TYPE_CORE:
        if view.step == 2 and view.state.vortex_rarity not in _VORTEX_RARITIES:
            return "Please select core rarity first."
        if view.step == 3 and not view.state.pop_time_utc:
            return "Please set pop time first."
        if view.step == 4 and not view.state.map_name:
            return "Please set objective map first."
        if (
            view.step == 5
            and view.state.notify_before_minutes not in _NOTIFY_BEFORE_MINUTES_OPTIONS
        ):
            return "Please select when to notify before pop."
        return None

    if view.state.objective_type == _OBJECTIVE_TYPE_NODE:
        if view.step == 2 and view.state.node_type not in _NODE_TYPES:
            return "Please select node type first."
        if view.step == 3 and view.state.node_tier not in _NODE_TIERS:
            return "Please select node tier first."
        if view.step == 4 and not view.state.pop_time_utc:
            return "Please set pop time first."
        if view.step == 5 and not view.state.map_name:
            return "Please set objective map first."
        if (
            view.step == 6
            and view.state.notify_before_minutes not in _NOTIFY_BEFORE_MINUTES_OPTIONS
        ):
            return "Please select when to notify before pop."
        return None

    return "Please select objective type first."


class _SetPopTimeButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Set pop time", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ObjectiveWizardView):
            return
        if interaction.user.id != view.user_id:
            return

        await interaction.response.send_modal(_PopTimeModal(view))


class _PopTimeModal(discord.ui.Modal, title="Set pop time (UTC)"):
    time_input: discord.ui.TextInput["_PopTimeModal"] = discord.ui.TextInput(
        label="Time (HH:MM)",
        placeholder="e.g. 17:34",
        required=True,
        max_length=5,
    )

    def __init__(self, parent_view: ObjectiveWizardView):
        super().__init__()
        self._parent_view = parent_view
        if parent_view.state.pop_time_utc:
            self.time_input.default = parent_view.state.pop_time_utc

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.time_input).strip()
        parsed = _parse_utc_hhmm(raw)
        if parsed is None:
            await interaction.response.send_message(
                "Invalid time format. Use HH:MM (00:00-23:59).",
                ephemeral=True,
            )
            return

        hhmm, pop_at_ts = parsed
        self._parent_view.state.pop_time_utc = hhmm
        self._parent_view.state.pop_at_ts = pop_at_ts
        self._parent_view._build_items()
        await interaction.response.edit_message(
            embed=_build_wizard_embed(self._parent_view), view=self._parent_view
        )


_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _parse_utc_hhmm(raw_value: str) -> Optional[tuple[str, int]]:
    match = _HHMM_RE.match((raw_value or "").strip())
    if not match:
        return None

    try:
        hour = int(match.group(1))
        minute = int(match.group(2))
    except ValueError:
        return None

    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None

    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    hhmm = f"{hour:02d}:{minute:02d}"
    return hhmm, int(target.timestamp())


class _SetMapButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Set objective map", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ObjectiveWizardView):
            return
        if interaction.user.id != view.user_id:
            return

        await interaction.response.send_modal(_MapModal(view))


class _MapModal(discord.ui.Modal, title="Set objective map"):
    map_input: discord.ui.TextInput["_MapModal"] = discord.ui.TextInput(
        label="Map name",
        placeholder="e.g. Morgana's Rest",
        required=True,
        max_length=100,
    )

    def __init__(self, parent_view: ObjectiveWizardView):
        super().__init__()
        self._parent_view = parent_view
        if parent_view.state.map_name:
            self.map_input.default = parent_view.state.map_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = str(self.map_input).strip()
        self._parent_view.state.map_name = value
        self._parent_view._build_items()
        await interaction.response.edit_message(
            embed=_build_wizard_embed(self._parent_view), view=self._parent_view
        )


def _notify_role_descriptive_base(obj: dict) -> str:
    obj_type = (obj.get("type") or "").strip() or "Objective"
    pop_time_utc = (obj.get("pop_time_utc") or "").strip() or "??:??"
    if obj_type in (_OBJECTIVE_TYPE_VORTEX, _OBJECTIVE_TYPE_CORE):
        rarity = (obj.get("rarity") or "").strip() or "?"
        return f"{obj_type}-{rarity}-{pop_time_utc}"
    elif obj_type == _OBJECTIVE_TYPE_NODE:
        node_type = (obj.get("node_type") or "Node").strip() or "Node"
        tier = (obj.get("tier") or "").strip() or "?"
        return f"{node_type}-{tier}-{pop_time_utc}"
    return f"{obj_type}-{pop_time_utc}"


def _notify_role_identity(obj: dict) -> str:
    objective_identity = str(obj.get("id") or "").strip()
    if objective_identity:
        return objective_identity
    return json.dumps(
        {
            "type": obj.get("type"),
            "rarity": obj.get("rarity"),
            "node_type": obj.get("node_type"),
            "tier": obj.get("tier"),
            "map": obj.get("map"),
            "pop_at_ts": obj.get("pop_at_ts"),
            "created_at_ts": obj.get("created_at_ts"),
            "created_by_id": obj.get("created_by_id"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _build_notify_role_name(obj: dict) -> str:
    """Return the clean, member-visible notification role name."""

    return _notify_role_descriptive_base(obj)[:100].rstrip(" -") or "Objective"


def _build_legacy_notify_role_name(obj: dict) -> str:
    """Reproduce the old visible hash suffix for safe migration only."""

    base_name = _notify_role_descriptive_base(obj)
    objective_identity = _notify_role_identity(obj)
    identity_suffix = hashlib.sha256(objective_identity.encode("utf-8")).hexdigest()[:10]
    max_base_length = 100 - len(identity_suffix) - 1
    trimmed_base = base_name[:max_base_length].rstrip(" -") or "Objective"
    return f"{trimmed_base}-{identity_suffix}"


def _notify_role_checkpoint_marker(guild_id: int, objective: dict) -> str:
    objective_key = _objective_key(objective) or _notify_role_identity(objective)
    return f"{_creation_marker(guild_id, objective_key)}:role"


def _build_pending_notify_role_name(guild_id: int, objective: dict) -> str:
    checkpoint = message_checkpoints.hidden_checkpoint(
        _notify_role_checkpoint_marker(guild_id, objective),
        bit_count=_NOTIFY_ROLE_CHECKPOINT_BITS,
    )
    max_base_length = 100 - len(checkpoint)
    visible_base = _build_notify_role_name(objective)[:max_base_length].rstrip(" -") or "Objective"
    return f"{visible_base}{checkpoint}"


def _recoverable_notify_role_names(guild_id: int, objective: dict) -> set[str]:
    return {
        _build_pending_notify_role_name(guild_id, objective),
        _build_legacy_notify_role_name(objective),
    }


def _known_notify_role_names(guild_id: int, objective: dict) -> set[str]:
    return {
        _build_notify_role_name(objective),
        *_recoverable_notify_role_names(guild_id, objective),
    }


async def _ensure_notify_role(
    guild: discord.Guild,
    objective: dict,
) -> Optional[_NotifyRoleResolution]:
    role_name = _build_notify_role_name(objective)
    try:
        stored_role_id = int(objective.get("notify_role_id") or 0)
    except (TypeError, ValueError):
        stored_role_id = 0
    if stored_role_id and objective.get(_NOTIFY_ROLE_OWNERSHIP_FIELD) is True:
        stored_role = guild.get_role(stored_role_id)
        if (
            stored_role is not None
            and _notify_role_ownership_error(guild, objective, stored_role) is None
        ):
            return _NotifyRoleResolution(
                role_id=int(stored_role.id),
                role_name=role_name,
                created_by_bot=True,
            )

    # Only transient zero-width or legacy hashed names are discoverable without
    # an authoritative role ID. A clean descriptive name is never adopted by name.
    recoverable_names = _recoverable_notify_role_names(guild.id, objective)
    matching_roles = [
        role
        for role in getattr(guild, "roles", ())
        if str(getattr(role, "name", "")) in recoverable_names
    ]
    if len(matching_roles) == 1:
        matching_role = matching_roles[0]
        if role_security.self_assignment_error(matching_role, guild) is None:
            return _NotifyRoleResolution(
                role_id=int(matching_role.id),
                role_name=role_name,
                created_by_bot=True,
            )
    elif len(matching_roles) > 1:
        logging.error(
            "Refusing to adopt ambiguous objective roles named %s in guild %s",
            sorted(recoverable_names),
            guild.id,
        )
        return None

    try:
        role = await guild.create_role(
            name=_build_pending_notify_role_name(guild.id, objective),
            mentionable=False,
            reason="Objective notification role",
        )
        if role_security.self_assignment_error(role, guild):
            try:
                await role.delete(reason="Unsafe objective notification role")
            except (discord.Forbidden, discord.HTTPException):
                pass
            return None
        return _NotifyRoleResolution(
            role_id=int(role.id),
            role_name=role_name,
            created_by_bot=True,
        )
    except (discord.Forbidden, discord.HTTPException):
        return None


async def _clean_persisted_notify_role_name(guild, objective: dict) -> bool:
    """Rename a verified bot-owned recovery role after its ID is durable."""

    if objective.get(_NOTIFY_ROLE_NAME_CLEANUP_FIELD):
        return True
    role_id = _safe_int(objective.get("notify_role_id"))
    if not role_id:
        objective[_NOTIFY_ROLE_NAME_CLEANUP_FIELD] = True
        return True
    role = guild.get_role(role_id)
    if role is None:
        return False
    ownership_error = _notify_role_ownership_error(guild, objective, role)
    if ownership_error:
        logging.warning(
            "Leaving objective notification role %s in guild %s untouched during name cleanup: %s",
            role_id,
            guild.id,
            ownership_error,
        )
        return False

    clean_name = _build_notify_role_name(objective)
    if str(getattr(role, "name", "")) != clean_name:
        try:
            await role.edit(
                name=clean_name,
                reason="Finalize objective notification role name",
            )
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            return False
    objective[_NOTIFY_ROLE_NAME_FIELD] = clean_name
    objective[_NOTIFY_ROLE_NAME_CLEANUP_FIELD] = True
    return True


async def _reconcile_active_notify_role_names(guild) -> None:
    """Migrate verified legacy/transient role names and retry permission failures."""

    entry = _active_objectives_entry(guild.id)
    if not isinstance(entry, dict):
        return
    objectives = entry.get("objectives", [])
    if not isinstance(objectives, list):
        return
    changed = False
    for objective in objectives:
        if not isinstance(objective, dict) or objective.get(_NOTIFY_ROLE_NAME_CLEANUP_FIELD):
            continue
        before = dict(objective)
        if await _clean_persisted_notify_role_name(guild, objective) and objective != before:
            changed = True
    if changed:
        _save_guild_entry(guild.id, entry)


def _persist_creation_state(
    guild_id: int,
    objective_key: str,
    objective: dict,
    panel_channel_id: int,
    panel_message_id: int,
    *,
    status: str,
    extra: Optional[dict] = None,
) -> runtime_state.RuntimeRecord:
    payload = _runtime_payload(
        _CREATION_RUNTIME_KIND,
        guild_id,
        objective_key,
        {
            "objective_key": objective_key,
            "objective": dict(objective),
            "panel_channel_id": int(panel_channel_id),
            "panel_message_id": int(panel_message_id),
            "marker": _creation_marker(guild_id, objective_key),
        },
    )
    if status == "pending":
        payload.pop(_MESSAGE_CHECKPOINT_CLEANUP_FIELD, None)
        payload.pop(_NOTIFY_ROLE_NAME_CLEANUP_FIELD, None)
    if extra:
        payload.update(extra)
    return runtime_state.upsert_record(
        _CREATION_RUNTIME_KIND,
        guild_id,
        objective_key,
        payload,
        status=status,
    )


def _pending_creation_role(guild, objective: dict):
    stored_role_id = _safe_int(objective.get("notify_role_id"))
    if stored_role_id:
        role = guild.get_role(stored_role_id)
        if role is not None and str(getattr(role, "name", "")) in _known_notify_role_names(
            guild.id,
            objective,
        ):
            return role
    recoverable_names = _recoverable_notify_role_names(guild.id, objective)
    matches = [
        role
        for role in getattr(guild, "roles", ())
        if str(getattr(role, "name", "")) in recoverable_names
    ]
    return matches[0] if len(matches) == 1 else None


async def _compensate_pending_objective_creation(
    guild,
    record: runtime_state.RuntimeRecord,
) -> bool:
    payload = record.payload
    objective = dict(payload.get("objective") or {})
    objective_key = str(payload.get("objective_key") or record.external_id)
    marker = str(payload.get("marker") or _creation_marker(guild.id, objective_key))
    all_clean = True

    channel_id = _safe_int(objective.get("channel_id") or payload.get("panel_channel_id"))
    if channel_id:
        try:
            channel = await _resolve_text_channel(guild, channel_id)
            if channel is not None:
                message = await _find_marked_message(
                    channel,
                    marker,
                    message_id=_safe_int(
                        objective.get("message_id") or payload.get("objective_message_id")
                    )
                    or None,
                    bot_user_id=_safe_int(getattr(getattr(guild, "me", None), "id", 0)),
                )
                if message is not None:
                    await message.delete()
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            all_clean = False

    role = _pending_creation_role(guild, objective)
    if role is not None:
        expected_names = _known_notify_role_names(guild.id, objective)
        if str(getattr(role, "name", "")) not in expected_names:
            all_clean = False
        else:
            try:
                await role.delete(reason="Incomplete objective creation cleanup")
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                all_clean = False
    else:
        stored_role = guild.get_role(_safe_int(objective.get("notify_role_id")))
        recoverable_names = _recoverable_notify_role_names(guild.id, objective)
        matching_roles = [
            candidate
            for candidate in getattr(guild, "roles", ())
            if str(getattr(candidate, "name", "")) in recoverable_names
        ]
        if stored_role is not None or matching_roles:
            all_clean = False

    runtime_state.set_status(
        _CREATION_RUNTIME_KIND,
        guild.id,
        record.external_id,
        "cancelled" if all_clean else "compensation_pending",
    )
    return all_clean


async def _complete_pending_objective_creation(
    guild,
    record: runtime_state.RuntimeRecord,
    *,
    discover_existing: bool = True,
) -> _ObjectiveCreationResult:
    payload = dict(record.payload)
    objective = dict(payload.get("objective") or {})
    objective_key = str(payload.get("objective_key") or record.external_id)
    panel_channel_id = _safe_int(payload.get("panel_channel_id"))
    panel_message_id = _safe_int(payload.get("panel_message_id"))
    if not objective or not objective_key or not panel_channel_id or not panel_message_id:
        await _compensate_pending_objective_creation(guild, record)
        return _ObjectiveCreationResult(False, None, "Stored objective intent is invalid.")

    entry = _active_objectives_entry(guild.id)
    existing = _find_objective_by_key(entry, objective_key)
    if existing is not None:
        runtime_state.set_status(
            _CREATION_RUNTIME_KIND,
            guild.id,
            record.external_id,
            "completed",
        )
        completed_record = runtime_state.get_record(
            _CREATION_RUNTIME_KIND,
            guild.id,
            record.external_id,
        )
        if completed_record is not None:
            await _cleanup_completed_creation_record(guild, completed_record)
        return _ObjectiveCreationResult(True, existing)
    if not _objectives_panel_is_current(
        guild.id,
        panel_channel_id,
        panel_message_id,
    ):
        await _compensate_pending_objective_creation(guild, record)
        return _ObjectiveCreationResult(False, None, "Objectives panel changed during creation.")

    try:
        panel_channel = await _resolve_text_channel(guild, panel_channel_id)
        if panel_channel is None:
            raise TypeError("Objectives panel channel is not a text channel")
        panel_message = await panel_channel.fetch_message(panel_message_id)
    except discord.NotFound:
        await _compensate_pending_objective_creation(guild, record)
        return _ObjectiveCreationResult(False, None, "Objectives panel no longer exists.")
    except (discord.Forbidden, discord.HTTPException, AttributeError, TypeError):
        return _ObjectiveCreationResult(False, None, "Objectives panel is temporarily unavailable.")

    warning: Optional[str] = None
    if not payload.get("role_creation_attempted"):
        role_resolution = await _ensure_notify_role(guild, objective)
        if role_resolution is not None:
            objective["notify_role_id"] = int(role_resolution.role_id)
            objective[_NOTIFY_ROLE_NAME_FIELD] = role_resolution.role_name
            objective[_NOTIFY_ROLE_OWNERSHIP_FIELD] = bool(role_resolution.created_by_bot)
        else:
            warning = "Objective posted, but I couldn't create the notification role (missing Manage Roles permission?)."
        record = _persist_creation_state(
            guild.id,
            objective_key,
            objective,
            panel_channel_id,
            panel_message_id,
            status="role_ready",
            extra={"role_creation_attempted": True},
        )
        payload = dict(record.payload)
    else:
        stored_role_id = _safe_int(objective.get("notify_role_id"))
        if stored_role_id and _pending_creation_role(guild, objective) is None:
            _clear_notify_role_reference(objective)
            warning = "Objective posted, but its pending notification role disappeared."

    if await _clean_persisted_notify_role_name(guild, objective):
        record = _persist_creation_state(
            guild.id,
            objective_key,
            objective,
            panel_channel_id,
            panel_message_id,
            status="role_ready",
            extra={
                "role_creation_attempted": True,
                _NOTIFY_ROLE_NAME_CLEANUP_FIELD: True,
            },
        )
        payload = dict(record.payload)
    elif objective.get("notify_role_id"):
        warning = (warning + " " if warning else "") + (
            "The notification role was created, but its name could not be finalized yet; "
            "the bot will retry automatically."
        )

    marker = str(payload.get("marker") or _creation_marker(guild.id, objective_key))
    try:
        known_message_id = (
            _safe_int(objective.get("message_id") or payload.get("objective_message_id")) or None
        )
        posted = (
            await _find_marked_message(
                panel_channel,
                marker,
                message_id=known_message_id,
                bot_user_id=_safe_int(getattr(getattr(guild, "me", None), "id", 0)),
            )
            if discover_existing or known_message_id
            else None
        )
        if posted is None:
            posted = await panel_channel.send(
                content=message_checkpoints.hidden_checkpoint(marker),
                embed=_build_objective_embed(objective),
                view=ObjectiveMessageView(),
                nonce=message_checkpoints.stable_nonce(marker),
            )
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return _ObjectiveCreationResult(False, None, "Objective message could not be posted.")

    objective["channel_id"] = int(panel_channel.id)
    objective["message_id"] = int(posted.id)
    record = _persist_creation_state(
        guild.id,
        objective_key,
        objective,
        panel_channel_id,
        panel_message_id,
        status="message_ready",
        extra={
            "role_creation_attempted": True,
            "objective_message_id": int(posted.id),
        },
    )
    if not _objectives_panel_is_current(
        guild.id,
        panel_channel_id,
        panel_message_id,
    ) or not add_objective(
        guild.id,
        objective,
        expected_panel_channel_id=panel_channel_id,
        expected_panel_message_id=panel_message_id,
    ):
        await _compensate_pending_objective_creation(guild, record)
        return _ObjectiveCreationResult(False, None, "Objective setup changed during creation.")

    runtime_state.set_status(
        _CREATION_RUNTIME_KIND,
        guild.id,
        objective_key,
        "completed",
    )
    await _clean_and_mark_message_checkpoint(
        _CREATION_RUNTIME_KIND,
        guild,
        objective_key,
        record.payload,
        status="completed",
        message=posted,
        marker=marker,
    )
    try:
        await panel_message.edit(
            embed=_build_panel_embed(guild),
            view=ObjectivesPanelView(),
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        warning = (warning + " " if warning else "") + "The panel summary could not be refreshed."
    return _ObjectiveCreationResult(True, objective, warning)


class _WizardConfirmButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Confirm and post objective", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ObjectiveWizardView):
            return
        if interaction.user.id != view.user_id:
            return
        if interaction.guild is None:
            await _send_ephemeral_notice(interaction, "This can only be used inside a server.")
            return
        if not isinstance(interaction.user, discord.Member) or not _objective_actor_can_manage(
            interaction.guild,
            interaction.user,
        ):
            await _send_ephemeral_notice(
                interaction,
                "Only an administrator or configured Caller can add objectives.",
            )
            return

        error = _validate_final(view)
        if error:
            await _send_ephemeral_notice(interaction, error)
            return

        channel_id, message_id = get_objectives_panel_message(interaction.guild.id)
        if not channel_id or not message_id:
            await _send_ephemeral_notice(
                interaction,
                "The objectives panel is not configured. Ask an admin to run **/set-objective-panel**.",
            )
            return

        if view._submitting:
            await _send_ephemeral_notice(
                interaction,
                "This objective is already being posted.",
            )
            return
        view._submitting = True
        self.disabled = True
        await interaction.response.defer()
        async with _objective_guild_locks.hold(int(interaction.guild.id)):
            try:
                panel_channel = await _resolve_text_channel(interaction.guild, channel_id)
                if panel_channel is None:
                    raise TypeError("Objectives panel channel is not a text channel")
                await panel_channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError):
                await interaction.edit_original_response(
                    content="The configured objectives panel could not be accessed. Ask an admin to post it again.",
                    embed=None,
                    view=None,
                )
                return

            if not _objective_actor_can_manage(
                interaction.guild,
                interaction.user,
            ) or not _objectives_panel_is_current(
                interaction.guild.id,
                channel_id,
                message_id,
            ):
                await interaction.edit_original_response(
                    content="The objectives panel was disabled while this objective was being prepared.",
                    embed=None,
                    view=None,
                )
                return

            objective = _build_objective_payload(view, interaction.user)
            objective["id"] = (
                f"{interaction.guild.id}-{interaction.id}-{int(datetime.now(timezone.utc).timestamp())}"
            )
            objective_key = _objective_key(objective)
            record = _persist_creation_state(
                interaction.guild.id,
                objective_key,
                objective,
                channel_id,
                message_id,
                status="pending",
                extra={"actor_id": int(interaction.user.id)},
            )
            result = await _complete_pending_objective_creation(
                interaction.guild,
                record,
                discover_existing=False,
            )
        view.stop()
        await interaction.edit_original_response(
            content=(
                result.warning or "Objective posted."
                if result.success
                else (
                    result.warning
                    or "The objective could not be completed. Its pending state will be reconciled after restart."
                )
            ),
            embed=None,
            view=None,
        )


def _validate_final(view: ObjectiveWizardView) -> Optional[str]:
    if view.state.objective_type == _OBJECTIVE_TYPE_VORTEX:
        if view.state.vortex_rarity not in _VORTEX_RARITIES:
            return "Please select vortex rarity."
        if not view.state.pop_time_utc or not view.state.pop_at_ts:
            return "Please set pop time."
        if not view.state.map_name:
            return "Please set objective map."
        if view.state.notify_before_minutes not in _NOTIFY_BEFORE_MINUTES_OPTIONS:
            return "Please select when to notify before pop."
        return None

    if view.state.objective_type == _OBJECTIVE_TYPE_CORE:
        if view.state.vortex_rarity not in _VORTEX_RARITIES:
            return "Please select core rarity."
        if not view.state.pop_time_utc or not view.state.pop_at_ts:
            return "Please set pop time."
        if not view.state.map_name:
            return "Please set objective map."
        if view.state.notify_before_minutes not in _NOTIFY_BEFORE_MINUTES_OPTIONS:
            return "Please select when to notify before pop."
        return None

    if view.state.objective_type == _OBJECTIVE_TYPE_NODE:
        if view.state.node_type not in _NODE_TYPES:
            return "Please select node type."
        if view.state.node_tier not in _NODE_TIERS:
            return "Please select node tier."
        if not view.state.pop_time_utc or not view.state.pop_at_ts:
            return "Please set pop time."
        if not view.state.map_name:
            return "Please set objective map."
        if view.state.notify_before_minutes not in _NOTIFY_BEFORE_MINUTES_OPTIONS:
            return "Please select when to notify before pop."
        return None

    return "Please select objective type."


def _build_objective_payload(view: ObjectiveWizardView, user: discord.abc.User) -> dict:
    payload: dict = {
        "type": view.state.objective_type,
        "map": view.state.map_name,
        "pop_time_utc": view.state.pop_time_utc,
        "pop_at_ts": view.state.pop_at_ts,
        "notify_before_minutes": int(view.state.notify_before_minutes)
        if view.state.notify_before_minutes is not None
        else None,
        "notify_at_ts": int(view.state.pop_at_ts - int(view.state.notify_before_minutes) * 60)
        if view.state.pop_at_ts
        and view.state.notify_before_minutes in _NOTIFY_BEFORE_MINUTES_OPTIONS
        else None,
        "created_at_ts": int(datetime.now(timezone.utc).timestamp()),
        "created_by": str(user),
        "created_by_id": int(user.id),
    }

    if view.state.objective_type in (_OBJECTIVE_TYPE_VORTEX, _OBJECTIVE_TYPE_CORE):
        payload["rarity"] = view.state.vortex_rarity
    elif view.state.objective_type == _OBJECTIVE_TYPE_NODE:
        payload["node_type"] = view.state.node_type
        payload["tier"] = view.state.node_tier

    return payload
