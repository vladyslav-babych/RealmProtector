"""Durable state for Discord resources that outlive a bot process.

Discord message, channel, and thread IDs are external identifiers.  Keeping a
small local snapshot lets startup reconciliation recover legacy resources once,
then makes SQLite the source used by subsequent bot runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from src.realm_protector.infrastructure import sqlite_database


@dataclass(frozen=True)
class RuntimeRecord:
    kind: str
    guild_id: int
    external_id: str
    payload: dict[str, Any]
    status: str
    updated_at: str


def _clean_identifier(value: object, field_name: str) -> str:
    clean_value = str(value or "").strip()
    if not clean_value or len(clean_value) > 200:
        raise ValueError(f"{field_name} must contain 1-200 characters.")
    return clean_value


def _decode(row) -> RuntimeRecord:
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return RuntimeRecord(
        kind=str(row["kind"]),
        guild_id=int(row["guild_id"]),
        external_id=str(row["external_id"]),
        payload=payload if isinstance(payload, dict) else {},
        status=str(row["status"]),
        updated_at=str(row["updated_at"]),
    )


def upsert_record_in_transaction(
    database,
    kind: str,
    guild_id: int,
    external_id: object,
    payload: dict[str, Any],
    *,
    status: str = "active",
) -> RuntimeRecord:
    """Create or replace a snapshot inside the caller's SQLite transaction."""

    clean_kind = _clean_identifier(kind, "kind")
    clean_external_id = _clean_identifier(external_id, "external_id")
    clean_status = _clean_identifier(status, "status")
    parsed_guild_id = int(guild_id)
    if parsed_guild_id <= 0:
        raise ValueError("guild_id must be a positive integer.")
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dictionary.")
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    database.execute(
        """
        INSERT INTO runtime_records (
            kind, guild_id, external_id, payload_json, status, updated_at
        ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(kind, guild_id, external_id) DO UPDATE SET
            payload_json = excluded.payload_json,
            status = excluded.status,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            clean_kind,
            parsed_guild_id,
            clean_external_id,
            payload_json,
            clean_status,
        ),
    )
    row = database.execute(
        """
        SELECT kind, guild_id, external_id, payload_json, status, updated_at
        FROM runtime_records
        WHERE kind = ? AND guild_id = ? AND external_id = ?
        """,
        (clean_kind, parsed_guild_id, clean_external_id),
    ).fetchone()
    assert row is not None
    return _decode(row)


def upsert_record(
    kind: str,
    guild_id: int,
    external_id: object,
    payload: dict[str, Any],
    *,
    status: str = "active",
) -> RuntimeRecord:
    """Create or replace one durable Discord-runtime snapshot."""

    with sqlite_database.transaction() as database:
        return upsert_record_in_transaction(
            database,
            kind,
            guild_id,
            external_id,
            payload,
            status=status,
        )


def get_record(
    kind: str,
    guild_id: int,
    external_id: object,
) -> Optional[RuntimeRecord]:
    with sqlite_database.connection() as database:
        row = database.execute(
            """
            SELECT kind, guild_id, external_id, payload_json, status, updated_at
            FROM runtime_records
            WHERE kind = ? AND guild_id = ? AND external_id = ?
            """,
            (str(kind), int(guild_id), str(external_id)),
        ).fetchone()
    return _decode(row) if row is not None else None


def list_records(
    kind: str,
    *,
    guild_id: Optional[int] = None,
    statuses: Optional[Iterable[str]] = None,
) -> list[RuntimeRecord]:
    clauses = ["kind = ?"]
    parameters: list[object] = [str(kind)]
    if guild_id is not None:
        clauses.append("guild_id = ?")
        parameters.append(int(guild_id))
    normalized_statuses = tuple(str(value) for value in (statuses or ()))
    if normalized_statuses:
        placeholders = ",".join("?" for _ in normalized_statuses)
        clauses.append(f"status IN ({placeholders})")
        parameters.extend(normalized_statuses)
    query = (
        "SELECT kind, guild_id, external_id, payload_json, status, updated_at "
        "FROM runtime_records WHERE " + " AND ".join(clauses) + " ORDER BY guild_id, external_id"
    )
    with sqlite_database.connection() as database:
        rows = database.execute(query, parameters).fetchall()
    return [_decode(row) for row in rows]


def set_status(
    kind: str,
    guild_id: int,
    external_id: object,
    status: str,
) -> bool:
    clean_status = _clean_identifier(status, "status")
    with sqlite_database.transaction() as database:
        cursor = database.execute(
            """
            UPDATE runtime_records
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE kind = ? AND guild_id = ? AND external_id = ?
            """,
            (clean_status, str(kind), int(guild_id), str(external_id)),
        )
    return cursor.rowcount > 0


def delete_record(kind: str, guild_id: int, external_id: object) -> bool:
    with sqlite_database.transaction() as database:
        cursor = database.execute(
            """
            DELETE FROM runtime_records
            WHERE kind = ? AND guild_id = ? AND external_id = ?
            """,
            (str(kind), int(guild_id), str(external_id)),
        )
    return cursor.rowcount > 0


__all__ = [
    "RuntimeRecord",
    "delete_record",
    "get_record",
    "list_records",
    "set_status",
    "upsert_record",
    "upsert_record_in_transaction",
]
