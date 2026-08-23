"""Crash-safe lifecycle for removing a guild's live bot configuration."""

from __future__ import annotations

from copy import deepcopy
from typing import Optional

from src.realm_protector.domain.models import GuildConfiguration
from src.realm_protector.infrastructure import (
    credential_store,
    document_store,
    guild_settings,
    local_repository,
    runtime_state,
    sqlite_database,
)

TEARDOWN_RECORD_KIND = "guild_configuration_teardown"
TEARDOWN_RECORD_ID = "current"
_FEATURE_NAMESPACES = ("tickets", "reaction_roles", "objectives")


class ConfigurationRemovalError(RuntimeError):
    """Raised when feature state cannot be retired without losing evidence."""


def _disabled_feature_entry(namespace: str, raw_entry: object) -> dict:
    if not isinstance(raw_entry, dict):
        raise ConfigurationRemovalError(f"Persisted {namespace} configuration must be an object.")
    entry = deepcopy(raw_entry)
    entry["disabled"] = True
    panels = entry.get("panels")
    if isinstance(panels, dict):
        for panel in panels.values():
            if isinstance(panel, dict):
                panel["active"] = False
                panel["disabled"] = True
    return entry


def _teardown_payload(configuration: GuildConfiguration) -> dict:
    return {
        "target_guild_name": configuration.target_guild_name,
        "configuration_channel_id": configuration.bot_configuration_channel_id,
        "configuration_message_id": configuration.bot_configuration_message_id,
        "utc_timer_guild_name": configuration.utc_timer_guild_name,
    }


def begin_guild_configuration_removal(
    discord_server_id: int,
) -> Optional[GuildConfiguration]:
    """Retire all local routing atomically and persist external cleanup intent."""

    local_repository.ensure_schema()
    with sqlite_database.transaction() as database:
        configuration = guild_settings.get_configuration(
            discord_server_id,
            database=database,
        )
        if configuration is None:
            return None

        for namespace in _FEATURE_NAMESPACES:
            entry = document_store.get_mapping_entry(
                namespace,
                discord_server_id,
                database=database,
            )
            if entry is None:
                continue
            document_store.upsert_mapping_entry(
                namespace,
                discord_server_id,
                _disabled_feature_entry(namespace, entry),
                database=database,
            )

        credential_store.quarantine_google_sheet_link(
            discord_server_id,
            "guild_configuration_removed",
            database=database,
        )
        removed = guild_settings.remove_target_guild_in_transaction(
            database,
            discord_server_id,
        )
        assert removed is not None
        runtime_state.upsert_record_in_transaction(
            database,
            TEARDOWN_RECORD_KIND,
            discord_server_id,
            TEARDOWN_RECORD_ID,
            _teardown_payload(configuration),
            status="pending",
        )
    return configuration


def get_pending_removal(discord_server_id: int) -> Optional[runtime_state.RuntimeRecord]:
    record = runtime_state.get_record(
        TEARDOWN_RECORD_KIND,
        discord_server_id,
        TEARDOWN_RECORD_ID,
    )
    return record if record is not None and record.status == "pending" else None


def list_pending_removals() -> list[runtime_state.RuntimeRecord]:
    return runtime_state.list_records(
        TEARDOWN_RECORD_KIND,
        statuses=("pending",),
    )


def complete_guild_configuration_removal(discord_server_id: int) -> bool:
    return runtime_state.set_status(
        TEARDOWN_RECORD_KIND,
        discord_server_id,
        TEARDOWN_RECORD_ID,
        "completed",
    )


__all__ = [
    "TEARDOWN_RECORD_ID",
    "TEARDOWN_RECORD_KIND",
    "ConfigurationRemovalError",
    "begin_guild_configuration_removal",
    "complete_guild_configuration_removal",
    "get_pending_removal",
    "list_pending_removals",
]
