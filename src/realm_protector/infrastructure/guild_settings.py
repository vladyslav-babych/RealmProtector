"""SQLite-backed Discord guild configuration.

Runtime reads are row-level: one guild interaction must not scan or rewrite
every other guild's configuration. Compatibility mapping helpers remain only
for legacy migration and fixtures.
"""

from __future__ import annotations

import sqlite3
import threading
import unicodedata
from functools import wraps
from typing import Callable, Optional, TypeVar

from src.realm_protector.domain.models import GuildConfiguration, LeaveAction
from src.realm_protector.domain.policies import coerce_leave_action
from src.realm_protector.infrastructure import (
    credential_store,
    document_store,
    local_repository,
    sqlite_database,
)

_DOCUMENT_NAMESPACE = "guild_settings"
_CONFIG_LOCK = threading.RLock()
_T = TypeVar("_T")


class GuildConfigurationError(RuntimeError):
    """Raised when persisted guild configuration has an invalid shape."""


def _config_transaction(function: Callable[..., _T]) -> Callable[..., _T]:
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _CONFIG_LOCK:
            return function(*args, **kwargs)

    return wrapped


def _validate_guild_id(value: int) -> int:
    parsed = int(value)
    if isinstance(value, bool) or parsed <= 0 or parsed > (1 << 63) - 1:
        raise ValueError("discord_server_id must be a positive signed 64-bit integer")
    return parsed


def _normalize_target_name(value: object) -> tuple[str, str]:
    display = unicodedata.normalize("NFKC", str(value or "")).strip()
    return display, display.casefold()


def _parse_role_ids(raw_role_ids: object) -> list[int]:
    if not isinstance(raw_role_ids, (list, tuple)):
        return []
    parsed_ids: list[int] = []
    for raw_role_id in raw_role_ids:
        try:
            role_id = int(raw_role_id)
        except (TypeError, ValueError):
            continue
        if 0 < role_id <= (1 << 63) - 1 and role_id not in parsed_ids:
            parsed_ids.append(role_id)
    return parsed_ids


def _validated_role_ids(raw_role_ids: list[int], field_name: str) -> list[int]:
    parsed_ids: list[int] = []
    for raw_role_id in raw_role_ids:
        if isinstance(raw_role_id, bool):
            raise ValueError(f"{field_name} contains an invalid role ID")
        try:
            role_id = int(raw_role_id)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field_name} contains an invalid role ID") from error
        if role_id <= 0 or role_id > (1 << 63) - 1:
            raise ValueError(f"{field_name} contains an invalid role ID")
        if role_id not in parsed_ids:
            parsed_ids.append(role_id)
    return parsed_ids


def _parse_optional_id(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if 0 < parsed <= (1 << 63) - 1 else None


def _parse_role_names(value: object, default: str) -> tuple[str, ...]:
    names = tuple(name.strip() for name in str(value or "").split(",") if name.strip())
    return names or (default,)


def _coerce_entry(raw_entry: object) -> dict:
    if isinstance(raw_entry, str):
        return {"guild_name": raw_entry, "member_role_name": "Member"}
    if isinstance(raw_entry, dict):
        return dict(raw_entry)
    raise GuildConfigurationError(
        "Persisted guild configuration must be an object or legacy guild-name string."
    )


def _configuration_from_entry(
    discord_server_id: int,
    raw_entry: object,
) -> GuildConfiguration:
    entry = _coerce_entry(raw_entry)
    target_name, target_key = _normalize_target_name(entry.get("guild_name"))
    if not target_key:
        raise GuildConfigurationError(
            f"Guild configuration {discord_server_id} has no Albion guild name."
        )

    member_role_name = str(entry.get("member_role_name") or "Member").strip()
    return GuildConfiguration(
        discord_server_id=discord_server_id,
        target_guild_name=target_name,
        member_role_name=member_role_name or "Member",
        caller_role_names=_parse_role_names(entry.get("caller_role_name"), "Caller"),
        economy_manager_role_names=_parse_role_names(
            entry.get("economy_manager_role_name"),
            "Economy Manager",
        ),
        leave_action=coerce_leave_action(
            entry.get("leave_action"),
            default=LeaveAction.REMOVE_ROLES,
        ),
        member_role_id=_parse_optional_id(entry.get("member_role_id")),
        caller_role_ids=tuple(_parse_role_ids(entry.get("caller_role_ids"))),
        economy_manager_role_ids=tuple(_parse_role_ids(entry.get("economy_manager_role_ids"))),
        bot_configuration_channel_id=_parse_optional_id(entry.get("bot_config_channel_id")),
        bot_configuration_message_id=_parse_optional_id(entry.get("bot_config_message_id")),
        bot_updates_channel_id=_parse_optional_id(entry.get("bot_updates_channel_id")),
        utc_timer_guild_name=(str(entry.get("utc_timer_guild_name") or "").strip() or None),
    )


def _load_config() -> dict[str, dict]:
    """Compatibility bulk read used by migration and list operations."""

    data = document_store.load_mapping(_DOCUMENT_NAMESPACE)
    return {str(key): _coerce_entry(value) for key, value in data.items()}


def get_configuration(
    discord_server_id: int,
    *,
    database: Optional[sqlite3.Connection] = None,
) -> Optional[GuildConfiguration]:
    """Return one immutable configuration snapshot, or ``None`` if absent."""

    guild_id = _validate_guild_id(discord_server_id)
    entry = document_store.get_mapping_entry(
        _DOCUMENT_NAMESPACE,
        guild_id,
        database=database,
    )
    if entry is None:
        return None
    return _configuration_from_entry(guild_id, entry)


@_config_transaction
def set_target_guild(
    discord_server_id: int,
    target_guild_name: str,
    member_role_name: str = "Member",
    caller_role_name: str = "Caller",
    economy_manager_role_name: str = "Economy Manager",
    leave_action: Optional[str] = None,
    *,
    member_role_id: Optional[int] = None,
    caller_role_ids: Optional[list[int]] = None,
    economy_manager_role_ids: Optional[list[int]] = None,
    bot_updates_channel_id: Optional[int] = None,
) -> None:
    guild_id = _validate_guild_id(discord_server_id)
    clean_target_name, target_key = _normalize_target_name(target_guild_name)
    if not target_key:
        raise ValueError("target_guild_name must not be blank")

    local_repository.ensure_schema()
    with sqlite_database.transaction() as database:
        raw_entry = document_store.get_mapping_entry(
            _DOCUMENT_NAMESPACE,
            guild_id,
            database=database,
        )
        existing_entry = _coerce_entry(raw_entry) if raw_entry is not None else {}
        base_entry = dict(existing_entry)
        _, previous_target_key = _normalize_target_name(existing_entry.get("guild_name"))

        base_entry["guild_name"] = clean_target_name
        base_entry["member_role_name"] = member_role_name.strip() or "Member"
        base_entry["caller_role_name"] = caller_role_name.strip() or "Caller"
        base_entry["economy_manager_role_name"] = (
            economy_manager_role_name.strip() or "Economy Manager"
        )
        if member_role_id is not None:
            parsed_member_role_id = _parse_optional_id(member_role_id)
            if parsed_member_role_id is None:
                raise ValueError("member_role_id must be a positive signed 64-bit integer")
            base_entry["member_role_id"] = str(parsed_member_role_id)
        if caller_role_ids is not None:
            base_entry["caller_role_ids"] = [
                str(role_id)
                for role_id in _validated_role_ids(
                    caller_role_ids,
                    "caller_role_ids",
                )
            ]
        if economy_manager_role_ids is not None:
            base_entry["economy_manager_role_ids"] = [
                str(role_id)
                for role_id in _validated_role_ids(
                    economy_manager_role_ids,
                    "economy_manager_role_ids",
                )
            ]
        if bot_updates_channel_id is not None:
            parsed_updates_channel_id = _parse_optional_id(bot_updates_channel_id)
            if parsed_updates_channel_id is None:
                raise ValueError("bot_updates_channel_id must be a positive signed 64-bit integer")
            base_entry["bot_updates_channel_id"] = str(parsed_updates_channel_id)
        requested_leave_action = str(leave_action or "").strip()
        base_entry["leave_action"] = coerce_leave_action(
            requested_leave_action or base_entry.get("leave_action"),
            default=LeaveAction.REMOVE_ROLES,
        ).value

        # Ledger authority and visible configuration change in one transaction.
        local_repository.activate_ledger_in_transaction(
            database,
            guild_id,
            clean_target_name,
        )
        if previous_target_key and previous_target_key != target_key:
            credential_store.quarantine_google_sheet_link(
                guild_id,
                "target_guild_changed",
                database=database,
            )
        document_store.upsert_mapping_entry(
            _DOCUMENT_NAMESPACE,
            guild_id,
            base_entry,
            database=database,
        )


def get_leave_action(discord_server_id: int) -> str:
    configuration = get_configuration(discord_server_id)
    return (
        configuration.leave_action.value
        if configuration is not None
        else LeaveAction.REMOVE_ROLES.value
    )


def get_target_guild(discord_server_id: int) -> Optional[str]:
    configuration = get_configuration(discord_server_id)
    return configuration.target_guild_name if configuration is not None else None


def get_member_role(discord_server_id: int) -> str:
    configuration = get_configuration(discord_server_id)
    return configuration.member_role_name if configuration is not None else "Member"


def get_member_role_id(discord_server_id: int) -> Optional[int]:
    configuration = get_configuration(discord_server_id)
    return configuration.member_role_id if configuration is not None else None


def get_caller_role_ids(discord_server_id: int) -> list[int]:
    configuration = get_configuration(discord_server_id)
    return list(configuration.caller_role_ids) if configuration is not None else []


def get_economy_manager_role_ids(discord_server_id: int) -> list[int]:
    configuration = get_configuration(discord_server_id)
    return list(configuration.economy_manager_role_ids) if configuration is not None else []


def get_caller_roles(discord_server_id: int) -> list[str]:
    configuration = get_configuration(discord_server_id)
    return list(configuration.caller_role_names) if configuration is not None else ["Caller"]


def get_economy_manager_roles(discord_server_id: int) -> list[str]:
    configuration = get_configuration(discord_server_id)
    return (
        list(configuration.economy_manager_role_names)
        if configuration is not None
        else ["Economy Manager"]
    )


def _update_entry(discord_server_id: int, update: Callable[[dict], None]) -> bool:
    guild_id = _validate_guild_id(discord_server_id)
    with sqlite_database.transaction() as database:
        raw_entry = document_store.get_mapping_entry(
            _DOCUMENT_NAMESPACE,
            guild_id,
            database=database,
        )
        if raw_entry is None:
            return False
        entry = _coerce_entry(raw_entry)
        update(entry)
        document_store.upsert_mapping_entry(
            _DOCUMENT_NAMESPACE,
            guild_id,
            entry,
            database=database,
        )
    return True


@_config_transaction
def set_bot_configuration_message(
    discord_server_id: int,
    channel_id: int,
    message_id: int,
) -> bool:
    parsed_channel_id = _parse_optional_id(channel_id)
    parsed_message_id = _parse_optional_id(message_id)
    if parsed_channel_id is None or parsed_message_id is None:
        raise ValueError("channel_id and message_id must be positive integers")
    return _update_entry(
        discord_server_id,
        lambda entry: entry.update(
            {
                "bot_config_channel_id": str(parsed_channel_id),
                "bot_config_message_id": str(parsed_message_id),
            }
        ),
    )


def get_bot_configuration_message(
    discord_server_id: int,
) -> tuple[Optional[int], Optional[int]]:
    configuration = get_configuration(discord_server_id)
    if configuration is None:
        return None, None
    return (
        configuration.bot_configuration_channel_id,
        configuration.bot_configuration_message_id,
    )


@_config_transaction
def set_bot_updates_channel(discord_server_id: int, channel_id: int) -> bool:
    parsed_channel_id = _parse_optional_id(channel_id)
    if parsed_channel_id is None:
        raise ValueError("channel_id must be a positive integer")
    return _update_entry(
        discord_server_id,
        lambda entry: entry.update({"bot_updates_channel_id": str(parsed_channel_id)}),
    )


def get_all_bot_updates_channels() -> dict[int, int]:
    result: dict[int, int] = {}
    for raw_guild_id, entry in _load_config().items():
        try:
            configuration = _configuration_from_entry(int(raw_guild_id), entry)
        except (GuildConfigurationError, TypeError, ValueError):
            continue
        if configuration.bot_updates_channel_id is not None:
            result[configuration.discord_server_id] = configuration.bot_updates_channel_id
    return result


@_config_transaction
def clear_utc_timer_channel(discord_server_id: int) -> bool:
    return _update_entry(
        discord_server_id,
        lambda entry: entry.pop("utc_timer_channel_id", None),
    )


@_config_transaction
def set_utc_timer_guild_name(discord_server_id: int, guild_name: str) -> bool:
    clean_name = str(guild_name or "").strip()
    if not clean_name:
        raise ValueError("guild_name must not be blank")
    return _update_entry(
        discord_server_id,
        lambda entry: entry.update({"utc_timer_guild_name": clean_name}),
    )


def get_utc_timer_guild_name(discord_server_id: int) -> Optional[str]:
    configuration = get_configuration(discord_server_id)
    return configuration.utc_timer_guild_name if configuration is not None else None


def get_all_utc_timer_guild_names() -> dict[int, str]:
    result: dict[int, str] = {}
    for raw_guild_id, entry in _load_config().items():
        try:
            configuration = _configuration_from_entry(int(raw_guild_id), entry)
        except (GuildConfigurationError, TypeError, ValueError):
            continue
        if configuration.utc_timer_guild_name:
            result[configuration.discord_server_id] = configuration.utc_timer_guild_name
    return result


def get_all_configured_server_ids() -> list[int]:
    result: list[int] = []
    for raw_server_id, entry in _load_config().items():
        try:
            configuration = _configuration_from_entry(int(raw_server_id), entry)
        except (GuildConfigurationError, TypeError, ValueError):
            continue
        result.append(configuration.discord_server_id)
    return result


@_config_transaction
def reconcile_ledger_generations() -> int:
    """Bind imported legacy server mappings to their active ledger generation."""

    reconciled = 0
    for raw_server_id, entry in _load_config().items():
        try:
            configuration = _configuration_from_entry(int(raw_server_id), entry)
        except (GuildConfigurationError, TypeError, ValueError):
            continue
        local_repository.activate_ledger(
            configuration.discord_server_id,
            configuration.target_guild_name,
        )
        reconciled += 1
    return reconciled


def get_server_id_by_target_guild(target_guild_name: str) -> Optional[str]:
    _, target_key = _normalize_target_name(target_guild_name)
    if not target_key:
        return None
    local_repository.ensure_schema()
    with sqlite_database.connection() as database:
        row = database.execute(
            """
            SELECT discord_guild_id
            FROM guild_ledger_generations
            WHERE target_guild_key = ? AND status = 'active'
            LIMIT 1
            """,
            (target_key,),
        ).fetchone()
    return str(int(row["discord_guild_id"])) if row is not None else None


def remove_target_guild_in_transaction(
    database: sqlite3.Connection,
    discord_server_id: int,
) -> Optional[GuildConfiguration]:
    """Archive and remove one guild mapping inside the caller's transaction."""

    guild_id = _validate_guild_id(discord_server_id)
    configuration = get_configuration(guild_id, database=database)
    if configuration is None:
        return None
    local_repository.archive_active_ledger_in_transaction(database, guild_id)
    document_store.delete_mapping_entry(
        _DOCUMENT_NAMESPACE,
        guild_id,
        database=database,
    )
    return configuration


__all__ = [
    "GuildConfigurationError",
    "clear_utc_timer_channel",
    "get_all_bot_updates_channels",
    "get_all_configured_server_ids",
    "get_all_utc_timer_guild_names",
    "get_bot_configuration_message",
    "get_caller_role_ids",
    "get_caller_roles",
    "get_configuration",
    "get_economy_manager_role_ids",
    "get_economy_manager_roles",
    "get_leave_action",
    "get_member_role",
    "get_member_role_id",
    "get_server_id_by_target_guild",
    "get_target_guild",
    "get_utc_timer_guild_name",
    "reconcile_ledger_generations",
    "remove_target_guild_in_transaction",
    "set_bot_configuration_message",
    "set_bot_updates_channel",
    "set_target_guild",
    "set_utc_timer_guild_name",
]
