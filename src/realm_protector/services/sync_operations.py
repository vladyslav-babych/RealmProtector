"""Administrative operations for the optional Google Sheets projection.

Discord handlers use this module instead of coordinating repository and Google
state directly. SQLite remains authoritative; every operation is either a
read-only health snapshot or an idempotent repair of the derived projection.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Mapping, Optional

from src.realm_protector.infrastructure import (
    credential_store,
    document_store,
    guild_settings,
    local_repository,
)
from src.realm_protector.services import google_sync


@dataclass(frozen=True)
class SyncHealth:
    discord_guild_id: int
    target_guild_name: str
    ledger_id: Optional[int]
    google_link_status: str
    google_credentials_readable: bool
    cutover_ready: bool
    active_players: int
    current_siphon_players: int
    latest_siphon_sync_at: Optional[str]
    outbox: Optional[local_repository.OutboxStatus]
    quarantine_reason: str = ""


@dataclass(frozen=True)
class SyncRecoveryResult:
    retried_dead_letters: int
    projection: google_sync.SyncResult


def _link_status(link: object) -> tuple[str, str]:
    if link is None:
        return "not linked", ""
    if not isinstance(link, Mapping):
        raise TypeError("Google Sheet link metadata must be an object.")
    status = str(link.get("status") or "active").strip().casefold()
    if status not in {"active", "disabled", "quarantined"}:
        status = "invalid"
    reason = str(link.get("quarantine_reason") or "").strip()
    return status, reason[:500]


def get_sync_health(discord_guild_id: int) -> SyncHealth:
    """Read one internally consistent-enough operational snapshot.

    Individual values may advance while this function runs, which is acceptable
    for an operator display. Each database read is itself consistent and no
    credentials or raw event payloads are exposed.
    """

    configuration = guild_settings.get_configuration(discord_guild_id)
    target_name = configuration.target_guild_name if configuration is not None else ""
    ledger = local_repository.get_active_ledger(
        discord_guild_id,
        create_if_missing=False,
    )
    link = document_store.get_google_sheet_link(discord_guild_id)
    link_status, quarantine_reason = _link_status(link)
    credentials_readable = bool(credential_store.get_credentials_info(discord_guild_id))

    if ledger is None:
        return SyncHealth(
            discord_guild_id=discord_guild_id,
            target_guild_name=target_name,
            ledger_id=None,
            google_link_status=link_status,
            google_credentials_readable=credentials_readable,
            cutover_ready=google_sync.is_cutover_ready(discord_guild_id),
            active_players=0,
            current_siphon_players=0,
            latest_siphon_sync_at=None,
            outbox=None,
            quarantine_reason=quarantine_reason,
        )

    players = local_repository.list_active_players(ledger.ledger_id)
    current_siphon_players = sum(
        player.siphon is not None
        and player.siphon_revision == player.revision
        and bool(player.siphon_synced_at)
        for player in players
    )
    latest_siphon_sync_at = max(
        (player.siphon_synced_at for player in players if player.siphon_synced_at),
        default=None,
    )
    return SyncHealth(
        discord_guild_id=discord_guild_id,
        target_guild_name=target_name,
        ledger_id=ledger.ledger_id,
        google_link_status=link_status,
        google_credentials_readable=credentials_readable,
        cutover_ready=google_sync.is_cutover_ready(discord_guild_id),
        active_players=len(players),
        current_siphon_players=current_siphon_players,
        latest_siphon_sync_at=latest_siphon_sync_at,
        outbox=local_repository.get_outbox_status(ledger.ledger_id),
        quarantine_reason=quarantine_reason,
    )


async def retry_dead_letters_and_flush(
    discord_guild_id: int,
    *,
    limit: int = 100,
) -> SyncRecoveryResult:
    """Restore a bounded dead-letter batch, then replay the projection queue."""

    if not await asyncio.to_thread(
        credential_store.get_credentials_info,
        discord_guild_id,
    ):
        return SyncRecoveryResult(
            retried_dead_letters=0,
            projection=google_sync.SyncResult(
                False,
                "Google Sheet credentials are not linked, active, or readable.",
                incomplete=True,
            ),
        )
    ledger = await asyncio.to_thread(
        local_repository.get_active_ledger,
        discord_guild_id,
        create_if_missing=False,
    )
    if ledger is None:
        return SyncRecoveryResult(
            retried_dead_letters=0,
            projection=google_sync.SyncResult(
                False,
                "No active local ledger is configured for this server.",
                incomplete=True,
            ),
        )
    retried = await asyncio.to_thread(
        local_repository.retry_dead_letter_outbox_for_guild,
        ledger.ledger_id,
        limit=limit,
        reset_attempts=True,
    )
    projection = await google_sync.flush_outbox(discord_guild_id, limit=limit)
    return SyncRecoveryResult(retried, projection)


__all__ = [
    "SyncHealth",
    "SyncRecoveryResult",
    "get_sync_health",
    "retry_dead_letters_and_flush",
]
