"""SQLite snapshots for trial configuration, resumable setup and trial lifecycles."""

from __future__ import annotations

from uuid import uuid4

from src.realm_protector.infrastructure import runtime_state, sqlite_database

CONFIG = "trial_configuration"
DRAFT = "trial_setup"
TRIAL = "trial"


def config(guild_id: int):
    return runtime_state.get_record(CONFIG, guild_id, "main")


def save(record, *, status=None, **updates):
    return runtime_state.upsert_record(
        record.kind,
        record.guild_id,
        record.external_id,
        {**record.payload, **updates},
        status=status or record.status,
    )


def begin(guild_id: int, member_id: int, nickname: str, configuration: dict):
    """Reserve a member before Discord writes; concurrent requests cannot duplicate trials."""
    with sqlite_database.transaction() as database:
        exists = database.execute(
            "SELECT 1 FROM runtime_records WHERE kind = ? AND guild_id = ? "
            "AND json_extract(payload_json, '$.member_id') = ? AND status != 'closed'",
            (TRIAL, guild_id, member_id),
        ).fetchone()
        if exists:
            raise ValueError("This player already has an active or pending trial.")
        return runtime_state.upsert_record_in_transaction(
            database,
            TRIAL,
            guild_id,
            uuid4().hex,
            {"member_id": member_id, "nickname": nickname, "config": configuration},
            status="creating",
        )


def for_channel(guild_id: int, channel_id: int):
    return next(
        (
            record
            for record in runtime_state.list_records(TRIAL, guild_id=guild_id)
            if record.payload.get("channel_id") == channel_id
        ),
        None,
    )
